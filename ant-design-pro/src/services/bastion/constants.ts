/**
 * 堡垒机 UI 公共常量与展示映射（页面只从这里取，避免各页各写一套颜色/文案）。
 */
import type { CommandAction, RiskLevel, SessionSource, SessionStatus } from './types';

export type TagMeta = { text: string; color: string };

/** 星期（后端 weekdays 用 Monday=0 的 weekday()，与 JS 的 getDay() 差 1） */
export const WEEKDAYS = [
  { value: 0, label: '周一' },
  { value: 1, label: '周二' },
  { value: 2, label: '周三' },
  { value: 3, label: '周四' },
  { value: 4, label: '周五' },
  { value: 5, label: '周六' },
  { value: 6, label: '周日' },
];

export const WEEKDAY_OPTIONS = WEEKDAYS.map((item) => ({
  label: item.label,
  value: item.value,
}));

/** [0..6] → 「每天」/「周一、周三」；空数组表示不限 */
export const weekdayLabel = (days?: number[] | null): string => {
  if (!days || days.length === 0) return '每天';
  if (days.length >= 7) return '每天';
  return [...days]
    .sort((a, b) => a - b)
    .map((day) => WEEKDAYS[day]?.label ?? `星期${day}`)
    .join('、');
};

export const RISK_META: Record<RiskLevel, TagMeta> = {
  low: { text: '低', color: 'default' },
  medium: { text: '中', color: 'blue' },
  high: { text: '高', color: 'orange' },
  critical: { text: '致命', color: 'red' },
};

export const RISK_ORDER: RiskLevel[] = ['low', 'medium', 'high', 'critical'];

export const RISK_OPTIONS = RISK_ORDER.map((level) => ({
  label: RISK_META[level].text,
  value: level,
}));

export const ACTION_META: Record<string, TagMeta> = {
  allow: { text: '放行', color: 'success' },
  deny: { text: '拦截', color: 'error' },
  confirm: { text: '需确认', color: 'warning' },
};

export const ACTION_OPTIONS = [
  { label: '放行', value: 'allow' },
  { label: '拦截', value: 'deny' },
];

export const SESSION_STATUS_META: Record<SessionStatus, TagMeta> = {
  active: { text: '在线', color: 'processing' },
  closed: { text: '已结束', color: 'default' },
  failed: { text: '连接失败', color: 'error' },
  terminated: { text: '已被中断', color: 'warning' },
  denied: { text: '已拒绝', color: 'error' },
};

/**
 * 会话结束原因的人话化。
 *
 * 后端现在就把原因写成人话了，但历史数据里还有套接字错误原文（例如「读取目标机失败：
 * [WinError 10054] 远程主机强迫关闭了一个现有的连接。」），目标机偶尔也会直接甩英文，
 * 所以展示时统一再过一道。
 */
const END_REASON_RULES: Array<[RegExp, string]> = [
  [
    /WinError 10054|forcibly closed|连接被对方重置|远程主机强迫关闭/i,
    '目标主机断开了连接（对方可能重启、注销或网络中断）',
  ],
  [
    /WinError 10061|积极拒绝|refused/i,
    '目标主机拒绝了连接（远程桌面服务可能没启动，或端口不通）',
  ],
  [/WinError 10060|timed out|连接超时/i, '连接目标主机超时（网络不通或对方没有响应）'],
  [/目标机关闭了连接/, '目标主机结束了远程桌面会话'],
  [/客户端已断开/, '浏览器侧关闭了窗口'],
];

export const humanizeEndReason = (reason?: string | null): string => {
  const text = (reason ?? '').trim();
  if (!text) {
    return '-';
  }
  const colon = text.indexOf('：');
  const probe = colon >= 0 ? text.slice(colon + 1) : text;
  for (const [pattern, message] of END_REASON_RULES) {
    if (pattern.test(probe)) {
      return message;
    }
  }
  return text;
};

export const SESSION_SOURCE_META: Record<SessionSource, TagMeta> = {
  gateway: { text: 'SSH 网关', color: 'geekblue' },
  web: { text: '网页终端', color: 'green' },
};

export const COMMAND_ACTION_META: Record<CommandAction, TagMeta> = {
  allow: { text: '放行', color: 'success' },
  deny: { text: '拦截', color: 'error' },
};

export const MATCH_TYPE_OPTIONS = [
  { label: '正则表达式', value: 'regex' },
  { label: '前缀匹配', value: 'prefix' },
  { label: '精确匹配', value: 'exact' },
  { label: '包含匹配', value: 'contains' },
];

export const POLICY_ACTION_OPTIONS = [
  { label: '放行', value: 'allow' },
  { label: '拦截', value: 'deny' },
];

export const USER_STATUS_META: Record<string, TagMeta> = {
  active: { text: '正常', color: 'success' },
  disabled: { text: '已停用', color: 'default' },
};

export const HOST_STATUS_META: Record<string, TagMeta> = {
  active: { text: '启用', color: 'success' },
  disabled: { text: '停用', color: 'default' },
};

export const AUTH_TYPE_OPTIONS = [
  { label: '密码认证', value: 'password' },
  { label: '密钥认证', value: 'key' },
];

/** 秒 → 「1 天 2 小时 3 分」 */
export const formatDuration = (seconds?: number | null): string => {
  const total = Math.max(0, Math.floor(seconds || 0));
  if (total < 60) return `${total} 秒`;
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const parts: string[] = [];
  if (days) parts.push(`${days} 天`);
  if (hours) parts.push(`${hours} 小时`);
  if (minutes) parts.push(`${minutes} 分`);
  if (!days && !hours && secs) parts.push(`${secs} 秒`);
  return parts.join(' ') || '0 秒';
};

/** 毫秒 → 「123 ms」/「1.2 s」 */
export const formatLatency = (ms?: number | null): string => {
  const value = Math.max(0, ms || 0);
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(2)} s`;
};

export const formatBytes = (bytes?: number | null): string => {
  const value = Math.max(0, bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(2)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
};

/** ISO8601 字符串 → 本地时间文本（后端存的是 UTC，带 Z） */
export const formatDateTime = (value?: string | null): string => {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
};

/** 通用：后端 options 接口 → antd Select 选项 */
export const toOptions = (
  rows: { value: unknown; label: string }[],
): { label: string; value: unknown }[] =>
  (rows || []).map((row) => ({ label: row.label, value: row.value }));

/** 截断长文本（表格里显示命令/输出用） */
export const truncate = (text?: string | null, max = 60): string => {
  const value = (text ?? '').replace(/\s+/g, ' ').trim();
  if (value.length <= max) return value;
  return `${value.slice(0, max)}…`;
};

/** 历史审计记录里以机器词开头的文案 → 中文直白说法（只替换开头那个词） */
const MACHINE_WORDS: Record<string, string> = {
  ok: '成功',
  okay: '成功',
  success: '成功',
  succeeded: '成功',
  done: '成功',
  finished: '成功',
  true: '成功',
};

/**
 * 审计文案归一：**给人看的正文中文直白，给系统看的字段保持英文**。
 *
 * `category`/`action`/权限码/工具名/`code` 这些给机器看的字段一律原样，只有人能读的
 * 那句话走这里。新写入的记录后端已经写成中文（`OK` → `成功`），本函数兜的是历史记录里
 * 那批老文案（`OK（共 4 条）`、`Ok`、`success`），否则同一条时间线上中英混排、看着像两个人写的。
 * 只动开头的机器词，后面的括号说明和异常原文一字不改 —— 审计记录本身不可回改。
 */
export const humanAuditMessage = (message?: string | null): string => {
  const text = (message ?? '').trim();
  if (!text) return '';
  const matched = /^([A-Za-z_]+)([\s:：,，-]*)([\s\S]*)$/.exec(text);
  if (!matched) return text;
  const [, word, gap, rest] = matched;
  const chinese = MACHINE_WORDS[word.toLowerCase()];
  if (!chinese) return text;
  return rest ? `${chinese}${gap}${rest}` : chinese;
};
