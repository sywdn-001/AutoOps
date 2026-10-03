/**
 * 纯文本层的 JSON 处理（不依赖 React/antd，方便单测）。
 *
 * 审计页看到的两类文本都来自后端落库的原文：
 * 1. `role="tool"` 的消息正文：`[工具 名字] 成功：摘要\n<JSON>` —— 正文里的 JSON 可能被
 *    后端按字符截断（`…（结果过长，已截断，共 N 字符）`），已经不是一个合法 JSON；
 * 2. 工具调用的入参 / 结果预览，同样是「JSON，但可能被截断」。
 *
 * 所以这里做两件事：**只读地把截断的 JSON 尽量修回可解析**（修不回来就退回原文，绝不编数据），
 * 以及**把工具消息正文拆成「人看的一行」和「结构化载荷」**，让页面能上卡片而不是堆 JSON。
 */
import { humanAuditMessage } from '@/services/bastion/constants';

/** 后端截断提示：`…（结果过长，已截断，共 63909 字符）` */
const TRUNCATED_MARK = /…（结果过长，已截断[^）]*）/u;

const closersOf = (stack: string[]): string =>
  stack
    .slice()
    .reverse()
    .map((char) => (char === '{' ? '}' : ']'))
    .join('');

/** 扫一遍括号结构（字符串里的括号不算），得到未闭合容器与「是否停在字符串中间」 */
const scanStructure = (text: string): { stack: string[]; inString: boolean } => {
  const stack: string[] = [];
  let inString = false;
  let escaped = false;
  for (const char of text) {
    if (inString) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') inString = true;
    else if (char === '{' || char === '[') stack.push(char);
    else if (char === '}' || char === ']') stack.pop();
  }
  return { stack, inString };
};

const parseOrUndefined = (text: string): unknown | undefined => {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
};

/**
 * 最后一个「值已完整收尾」的位置：闭括号后面（忽略空白）跟着逗号或闭括号。
 *
 * 为什么不用「退到上一个逗号」：截断点常常落在一个条目的中间（`"count": 7` 后面就断了），
 * 退到逗号会把**没写完的条目**当成完整条目，读的人会以为 `7` 就是真值。只认闭括号收尾，
 * 宁可少渲染一条，也不给人看假数据。
 */
const lastSafeEnd = (text: string): number => {
  let inString = false;
  let escaped = false;
  let safe = -1;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') {
      inString = true;
      continue;
    }
    if (char === '}' || char === ']') {
      let next = index + 1;
      while (next < text.length && /\s/u.test(text[next])) next += 1;
      const following = text[next];
      if (next >= text.length || following === ',' || following === '}' || following === ']') {
        safe = index + 1;
      }
    }
  }
  return safe;
};

/**
 * 尽力把被截断的 JSON 修回可解析（**只用于展示**）。
 *
 * 做法：去掉截断标记 → 找到最后一个完整收尾的位置 → 补上未闭合的括号；
 * 修不回来就返回 `undefined`，调用方退回原文展示，绝不凭空补半条数据。
 */
export const repairTruncatedJson = (
  text: string,
): { data: unknown; truncated: boolean } | undefined => {
  const cleaned = text.replace(TRUNCATED_MARK, '').trim();
  if (!cleaned) return undefined;
  const direct = parseOrUndefined(cleaned);
  if (direct !== undefined) return { data: direct, truncated: cleaned !== text.trim() };

  const safeEnd = lastSafeEnd(cleaned);
  if (safeEnd <= 0) return undefined;
  const head = cleaned.slice(0, safeEnd).replace(/[,\s]+$/u, '');
  if (scanStructure(head).inString) return undefined;
  const parsed = parseOrUndefined(`${head}${closersOf(scanStructure(head).stack)}`);
  return parsed === undefined ? undefined : { data: parsed, truncated: true };
};

/**
 * 字符串里塞 JSON 是常态，这里统一判一次：能被解析（含截断修复）就按 JSON 处理，
 * 否则原样当文本，绝不因为解析失败把内容吞掉。
 */
export const parseMaybeJson = (
  value: unknown,
): { data: unknown; json: boolean; truncated: boolean } => {
  if (typeof value !== 'string') {
    return { data: value, json: Boolean(value) && typeof value === 'object', truncated: false };
  }
  const text = value.trim();
  if (!text.startsWith('{') && !text.startsWith('[')) {
    return { data: value, json: false, truncated: false };
  }
  const direct = parseOrUndefined(text);
  if (direct !== undefined) return { data: direct, json: true, truncated: false };
  const repaired = repairTruncatedJson(text);
  if (repaired) return { data: repaired.data, json: true, truncated: true };
  return { data: value, json: false, truncated: false };
};

/** `[工具 名字] 成功：摘要` 这一行（后端的 `_tool_message` 就是拼的这个） */
const TOOL_HEAD = /^\[工具\s+([^\]]+)\]\s*([^\n:：]*)[:：]?\s*([\s\S]*)$/u;

/**
 * 把 `role="tool"` 的消息正文拆开：
 * 前半是给人看的一行（工具名 + 成功/失败 + 摘要），后半是结构化载荷。
 *
 * - JSON 藏在正文中间时（失败消息里还有一行 `HTTP 403`）也能找到起点；
 * - 摘要里的机器词（老记录里的 `ok`/`OK`）在展示层翻成中文，`成功：成功` 这种重复会被吸收成
 *   `成功`；给机器看的工具名、权限码、HTTP 码原样保留；
 * - 拿不到 JSON 时 `payload` 为空，调用方按普通文本渲染。
 */
export const splitToolMessage = (
  content: string,
): {
  head: string;
  status: string;
  summary: string;
  payloadText: string;
  payload: unknown;
  json: boolean;
  truncated: boolean;
} => {
  const text = content ?? '';
  const lines = text.split('\n');
  /** 载荷起点：第一行以 `{`/`[` 开头的行（`[工具 …]` 那一行要排除掉） */
  const start = lines.findIndex((line) => {
    const trimmed = line.trimStart();
    if (trimmed.startsWith('[工具')) return false;
    return trimmed.startsWith('{') || trimmed.startsWith('[');
  });
  const headText = (start > 0 ? lines.slice(0, start).join('\n') : text).trim();
  const payloadText = start > 0 ? lines.slice(start).join('\n').trim() : '';

  const matched = TOOL_HEAD.exec(headText);
  const name = matched ? matched[1] : '';
  const status = matched ? matched[2].trim() : '';
  const rawSummary = matched ? matched[3].trim() : '';
  let summary = humanAuditMessage(rawSummary);
  /** 后端历史记录里出现过 `失败：失败：权限不足…` 这种重复状态词，展示层吃掉一层 */
  if (status && summary.startsWith(status)) {
    summary = summary.slice(status.length).replace(/^[：:\s]+/u, '').trim();
  }
  let head = headText;
  if (name) {
    const label = status ? ` ${status}` : '';
    if (!summary || summary === status) head = `[工具 ${name}]${label}`;
    else if (summary.startsWith('（') || summary.startsWith('(')) head = `[工具 ${name}]${label}${summary}`;
    else head = `[工具 ${name}]${label}：${summary}`;
  }

  const parsed = payloadText
    ? parseMaybeJson(payloadText)
    : { data: undefined, json: false, truncated: false };
  return {
    head,
    status,
    summary,
    payloadText,
    payload: parsed.data,
    json: parsed.json,
    truncated: parsed.truncated,
  };
};
