/**
 * ComputerUse 部署向导的纯逻辑：阶段↔百分比、部署参数组装、日志分级着色、
 * SSE 帧解析与部署状态机（前后端事件 → 界面状态）。
 *
 * 这个文件**不 import React、不碰 DOM、不直接调接口**（只用 `globalThis.crypto`
 * 生成随机密钥），所以可以用 vitest 直接跑：向导最容易出错的地方就是「阶段百分比
 * 跟后端对不上」「参数拼错导致后端 400」「日志级别配色错」「SSE 事件分错类」。
 */

import { t } from '@/i18n';
import type {
  CachedInstaller,
  Deployment,
  DeploymentLogEvent,
  DeploymentStatus,
  DeployOptions,
  DeployPayload,
  LogLevel,
  LogStreamMessage,
  PythonVersionItem,
} from '@/services/bastion/computeruse';
import {
  normalizeLogLevel,
  STAGE_SEQUENCE,
} from '@/services/bastion/computeruse';
import type {
  DeployLoggerState,
  DeployOptionDraft,
  DeployResult,
} from './types';
import { DEFAULT_DEPLOY_OPTIONS, LOG_LEVEL_META } from './types';

// SSE 帧解析住在服务层（`openLogStream` 自己要用），这里 re-export 出去，
// 让纯逻辑测试与页面都从同一个位置拿（`parseLogStreamMessage` 的坏帧行为也一并测）。
export { parseLogStreamMessage } from '@/services/bastion/computeruse';

// ------------------------------------------------------------------ 阶段与进度

/**
 * 某个阶段**完成时**对应多少百分比（未收录的 key，如 `queued`，返回 0）。
 *
 * 与后端 `app/computeruse/deploy.py::STAGES` 的值一一对应：6/28/48/68/82/92/96/100。
 */
export function stageProgress(stage?: string | null): number {
  const meta = STAGE_SEQUENCE.find((item) => item.key === stage);
  return meta ? meta.progress : 0;
}

/** 阶段在固定顺序里的下标；`queued` 之类不在序列里的返回 -1 */
export function stageIndex(stage?: string | null): number {
  if (!stage) return -1;
  return STAGE_SEQUENCE.findIndex((item) => item.key === stage);
}

/** 阶段的中文名（后端没给 `stageLabel` 时的兜底） */
export function stageLabel(stage?: string | null): string {
  const meta = STAGE_SEQUENCE.find((item) => item.key === stage);
  return meta ? meta.label : (stage ?? '');
}

/** 竖向阶段列表里每一步的状态 */
export type StageState = 'finish' | 'process' | 'error' | 'wait';

/**
 * 「当前阶段 + 状态」→ 阶段在 antd Steps 里的状态。
 *
 * 已经过去的阶段算 `finish`，当前阶段在跑是 `process`；部署失败时停下来的那一步
 * 标成 `error`，好让人一眼看出是哪一步挂的。
 */
export function stepStatus(
  current: string | null | undefined,
  status: DeploymentStatus | string | null | undefined,
  target: string,
): StageState {
  const index = stageIndex(current);
  const own = stageIndex(target);
  if (index < 0 || own < 0) return 'wait';
  if (own < index) return 'finish';
  if (own > index) return 'wait';
  if (status === 'failed') return 'error';
  if (status === 'cancelled') return 'wait';
  return 'process';
}

/**
 * 阶段进度条要显示的百分比。
 *
 * 后端在「刚进这个阶段、还没跑完」时给的是 0，而阶段锚点（6/28/.../100）代表的是
 * 「该阶段做完」的值 —— 直接用 0 会让进度条从 0 重新爬，看起来像退回去了。
 * 所以 0/缺失都退回阶段锚点，只有后端给了**有效且不为 0** 的数才用它。
 */
export function displayProgress(
  progress?: number | null,
  stage?: string | null,
): number {
  if (
    typeof progress === 'number' &&
    Number.isFinite(progress) &&
    progress > 0
  ) {
    return Math.min(100, Math.max(0, Math.round(progress)));
  }
  return stageProgress(stage);
}

/** 阶段列表项（供竖向 Steps 渲染） */
export type StageDescriptor = {
  key: string;
  label: string;
  progress: number;
  state: StageState;
};

/**
 * 组装阶段列表：优先用后端下发的 `stages`（它带着中文名与百分比），
 * 后端没给时退回前端这份固定顺序，保证界面不会空白。
 */
export function buildStages(
  stages: Deployment['stages'] | undefined,
  current: string | null | undefined,
  status: string | null | undefined,
): StageDescriptor[] {
  const source =
    stages && stages.length > 0
      ? stages.map((item) => ({
          key: item.key,
          label: item.label,
          progress: item.progress,
        }))
      : STAGE_SEQUENCE.map((item) => ({
          key: item.key,
          label: item.label,
          progress: item.progress,
        }));
  const currentIndex = source.findIndex((item) => item.key === current);
  return source.map((item, index) => ({
    ...item,
    state:
      currentIndex < 0
        ? 'wait'
        : index < currentIndex
          ? 'finish'
          : index > currentIndex
            ? 'wait'
            : status === 'failed'
              ? 'error'
              : status === 'cancelled'
                ? 'wait'
                : 'process',
  }));
}

// ------------------------------------------------------------------ 部署参数组装

/** `buildDeployPayload` 的入参 */
export type DeployPayloadInput = {
  hostId: number | null | undefined;
  accountId: number | null | undefined;
  pythonVersion: string;
  installerName: string;
  bastionUrl?: string;
  options: DeployOptionDraft;
  authKey?: string;
};

/** 参数不齐时是否已经可以发请求（hostId 必须 ≥1，accountId 必须 ≥1） */
export function canDeploy(input: DeployPayloadInput): boolean {
  return (
    typeof input.hostId === 'number' &&
    input.hostId > 0 &&
    typeof input.accountId === 'number' &&
    input.accountId > 0 &&
    Boolean(input.pythonVersion) &&
    Boolean(input.installerName)
  );
}

/**
 * 表单草稿 → `POST /deployments` 的请求体。
 *
 * 规则：
 * - `mcpPort` 非法（<1 或 >65535）时退回 8000；
 * - `pipIndex` 首尾空白会 trim，**清空就不带这个字段**（让 pip 用默认源）；
 * - `authKey` 只在开启访问密钥且有值时带上；
 * - `bindAll` 恒为布尔值（后端据此决定跳不跳 portproxy）。
 */
export function buildDeployPayload(input: DeployPayloadInput): DeployPayload {
  const port = Math.trunc(Number(input.options.mcpPort));
  const options: DeployOptions = {
    mcpPort: Number.isFinite(port) && port >= 1 && port <= 65535 ? port : 8000,
    bindAll: Boolean(input.options.bindAll),
  };
  const pipIndex = (input.options.pipIndex ?? '').trim();
  if (pipIndex) options.pipIndex = pipIndex;
  const authKey = (input.authKey ?? '').trim();
  if (input.options.useAuthKey && authKey) options.authKey = authKey;

  const payload: DeployPayload = {
    hostId: Number(input.hostId),
    accountId: Number(input.accountId),
    pythonVersion: input.pythonVersion,
    installerName: input.installerName,
    options,
  };
  const bastionUrl = (input.bastionUrl ?? '').trim();
  if (bastionUrl) payload.bastionUrl = bastionUrl;
  return payload;
}

/** 该主机的 MCP 是否可以被侧边栏使用（`GET /status` 的 ready） */
export function isHostReady(
  host: { ready?: boolean; deployment?: Deployment | null },
  status?: { ready?: boolean } | null,
): boolean {
  if (status) return Boolean(status.ready);
  return Boolean(host.ready);
}

/** 从部署记录还原「再部署一台」要用的入口信息（主机/账号/密钥之外的部分） */
export function deployEntryFromDeployment(deployment: Deployment): {
  hostId: number;
  accountId: number | null;
  hostName: string;
} {
  return {
    hostId: deployment.hostId,
    accountId: deployment.accountId,
    hostName: deployment.hostName,
  };
}

// ------------------------------------------------------------------ 密钥与格式化

const KEY_ALPHABET =
  'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';

/**
 * 生成 32 位随机访问密钥（前后端都会用它做 Bearer 校验）。
 *
 * `crypto.getRandomValues` 在非安全上下文（http + 局域网 IP）也可能缺失，
 * 那时退回 `Math.random` —— 只是给不出强随机，总比整页崩掉好。
 */
export function generateAuthKey(
  length = 32,
  cryptoSource: Pick<Crypto, 'getRandomValues'> | undefined = globalThis.crypto,
): string {
  const size = Math.max(1, Math.trunc(length));
  const bytes = new Uint8Array(size);
  if (typeof cryptoSource?.getRandomValues === 'function') {
    cryptoSource.getRandomValues(bytes);
  } else {
    for (let index = 0; index < size; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  let key = '';
  for (let index = 0; index < size; index += 1) {
    const byte = bytes[index] ?? 0;
    key += KEY_ALPHABET[byte % KEY_ALPHABET.length];
  }
  return key;
}

/** 字节数 → 人类可读（后端给的是字节，界面统一用这个） */
export function humanSize(bytes?: number | null): string {
  const value = Math.max(0, Number(bytes) || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024)
    return `${(value / 1024 / 1024).toFixed(2)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 版本号比较：a ≥ b 返回 true（只按数字段比，`3.13.0` vs `3.13` 也成立） */
export function versionAtLeast(a: string, b: string): boolean {
  const parse = (value: string) =>
    (value || '')
      .split('.')
      .map((part) => Number.parseInt(part, 10))
      .map((num) => (Number.isFinite(num) ? num : 0));
  const left = parse(a);
  const right = parse(b);
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const l = left[index] ?? 0;
    const r = right[index] ?? 0;
    if (l !== r) return l > r;
  }
  return true;
}

/** 这一行安装包本地缓存了多少字节（没缓存返回 0） */
export function localSizeOf(
  filename: string,
  cached: CachedInstaller[],
): number {
  const hit = cached.find((item) => item.filename === filename);
  return hit ? Number(hit.size) || 0 : 0;
}

/** 后端 Error → 可读中文（统一信封里的 `message`） */
export function backendMessage(error: unknown, fallback: string): string {
  const response = (error as { response?: { data?: { message?: string } } })
    ?.response;
  const message = response?.data?.message;
  if (typeof message === 'string' && message.trim()) return message;
  if (error instanceof Error && error.message) {
    return `${fallback}（${error.message}）`;
  }
  return fallback;
}

// ------------------------------------------------------------------ 日志分级与着色

const LEVEL_PATTERN = /\[(info|success|warning|error)\]/;

/** 日志行的展示级别：优先用后端字段，缺失时从 `logTail` 摘要里的 `[level]` 猜 */
export function detectLogLevel(
  level?: string | null,
  text?: string | null,
): LogLevel {
  const normalized = (level ?? '').trim().toLowerCase();
  if (
    normalized === 'info' ||
    normalized === 'success' ||
    normalized === 'warning' ||
    normalized === 'error'
  ) {
    return normalized;
  }
  return normalizeLogLevel(LEVEL_PATTERN.exec(text ?? '')?.[1]);
}

/**
 * 一条日志 → 日志台里的一行。
 *
 * 行首的 `seq|` 只用来给状态机去重（`logTail` 回放与 SSE 补发会重复），
 * `[level] ` 只用来着色 —— 两者在渲染前都要剥掉。后端自己的 `logTail` 老格式里
 * 没有这两个前缀，所以展示侧都做了兼容判断。
 */
export function formatLogEntry(event: {
  seq?: number;
  time?: string;
  level?: string | null;
  stageLabel?: string;
  message?: string;
}): string {
  const level = detectLogLevel(event.level, event.message);
  const body = formatLogLine(event);
  const seq = Number(event.seq);
  return seq > 0 ? `${seq}|[${level}] ${body}` : `[${level}] ${body}`;
}

/** 日志台一行的最终文案：`<time> [阶段] 消息` */
export function formatLogLine(event: {
  time?: string;
  stageLabel?: string;
  message?: string;
}): string {
  const time = (event.time ?? '').trim() || '--:--:--';
  const label = (event.stageLabel ?? '').trim();
  const text = (event.message ?? '').trim();
  return label ? `${time} [${label}] ${text}` : `${time} ${text}`;
}

/** 后端 `logTail` 摘要里的一行 → 日志台行（级别从行内 `[level]` 猜） */
export function parseTailLines(tail?: string | null): string[] {
  return (tail ?? '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

const LEVEL_PREFIX = /^\[(info|success|warning|error)\]\s*/;

/** 剥掉前端加的两个前缀（`seq|` 与 `[level] `），拿到给人看的正文 */
export function stripLogDecorations(line: string): string {
  return dropSeq(line).replace(LEVEL_PREFIX, '');
}

/** 一行的颜色（终端配色：info 浅灰 / success 绿 / warning 橙 / error 红） */
export function logLineColor(line: string): string {
  const matched = LEVEL_PREFIX.exec(dropSeq(line));
  return LOG_LEVEL_META[detectLogLevel(matched?.[1], line)].color;
}

/** 只剥 `seq|`，保留 `[level]`（配色要看它） */
function dropSeq(line: string): string {
  const index = line.indexOf('|');
  if (index <= 0) return line;
  return /^\d+$/.test(line.slice(0, index)) ? line.slice(index + 1) : line;
}

// ------------------------------------------------------------------ 部署状态机

/** 向导第 3 步的全部状态 */
export type DeployStreamState = {
  deployment: Deployment | null;
  /** 后端下发的阶段表（阶段列表按它渲染） */
  stages: Deployment['stages'];
  status: DeploymentStatus;
  stage: string;
  stageLabel: string;
  progress: number;
  error: string;
  lastSeq: number;
  /** 流是否还活着（后端 runner 还在跑） */
  live: boolean;
  logs: string[];
  /** 已经见过 `end` 帧 */
  ended: boolean;
};

/** 新建部署成功后、订阅日志流之前的状态 */
export function initialStreamState(deployment: Deployment): DeployStreamState {
  return {
    deployment,
    stages: deployment.stages,
    status: deployment.status,
    stage: deployment.stage,
    stageLabel: deployment.stageLabel || stageLabel(deployment.stage),
    progress: displayProgress(deployment.progress, deployment.stage),
    error: deployment.error || '',
    lastSeq: deployment.lastSeq ?? 0,
    live: deployment.live ?? false,
    logs: parseTailLines(deployment.logTail).map((line) =>
      LEVEL_PREFIX.test(line) ? line : `[info] ${line}`,
    ),
    ended: false,
  };
}

const MAX_LOG_LINES = 2000;

/** 追加日志：`seq` 已见过就丢掉（重连补日志时会重复） */
function appendLog(
  state: DeployStreamState,
  event: DeploymentLogEvent,
): DeployStreamState {
  if (event.seq > 0 && event.seq <= state.lastSeq) return state;
  const lines = [...state.logs, formatLogEntry(event)];
  return {
    ...state,
    logs: lines.length > MAX_LOG_LINES ? lines.slice(-MAX_LOG_LINES) : lines,
    lastSeq: event.seq > 0 ? event.seq : state.lastSeq,
  };
}

/**
 * 状态机：SSE 事件 → 界面状态。
 *
 * - `hello`：整条部署记录（首次订阅的第一帧，用来补齐阶段表与已有日志）；
 * - `log`：追加一行（去重靠 `seq`），阶段与百分比也跟着推进；
 * - `status`：权威状态（stage/progress/error/live），**error 只在非空时覆盖**，
 *   免得一次中间的 status 帧把已经记下的失败原因清掉；
 * - `end`：标记结束并落到终态。
 */
export function deployStreamReducer(
  state: DeployStreamState,
  message: LogStreamMessage,
): DeployStreamState {
  if (message.type === 'hello') {
    const deployment = message.deployment;
    return {
      ...state,
      deployment,
      stages: deployment.stages ?? state.stages,
      stage: deployment.stage || state.stage,
      stageLabel:
        deployment.stageLabel ||
        state.stageLabel ||
        stageLabel(deployment.stage),
      progress: displayProgress(deployment.progress, deployment.stage),
      error: deployment.error || state.error,
      lastSeq: Math.max(state.lastSeq, deployment.lastSeq ?? 0),
      live: deployment.live ?? state.live,
      status: deployment.status || state.status,
    };
  }

  if (message.type === 'log') {
    const event = message.event;
    const next = appendLog(state, event);
    return {
      ...next,
      stage: event.stage || next.stage,
      stageLabel: event.stageLabel || next.stageLabel,
      progress: Math.max(
        next.progress,
        displayProgress(event.progress, event.stage),
      ),
      status: next.ended ? next.status : 'running',
      live: true,
    };
  }

  if (message.type === 'status') {
    return {
      ...state,
      status: message.status,
      stage: message.stage || state.stage,
      stageLabel: message.stageLabel || state.stageLabel,
      progress: displayProgress(message.progress, message.stage || state.stage),
      error: message.error.trim() ? message.error : state.error,
      lastSeq: Math.max(state.lastSeq, message.lastSeq),
      live: message.live,
    };
  }

  // end：终态就是后端给的 status（success/failed/cancelled）
  return { ...state, status: message.status, live: false, ended: true };
}

/**
 * 日志台状态机（滚动条是否跟随 + 连接状态），与部署状态分开，
 * 这样「用户手动往上翻日志」不会被后面来的日志强行拽回底部。
 */
export function loggerReducer(
  state: DeployLoggerState,
  action:
    | { type: 'push'; line: string }
    | { type: 'reset' }
    | { type: 'pause'; paused: boolean }
    | { type: 'connected'; connected: boolean },
): DeployLoggerState {
  if (action.type === 'reset') return { ...state, lines: [] };
  if (action.type === 'pause') return { ...state, paused: action.paused };
  if (action.type === 'connected')
    return { ...state, connected: action.connected };
  const lines = [...state.lines, action.line];
  return {
    ...state,
    lines: lines.length > MAX_LOG_LINES ? lines.slice(-MAX_LOG_LINES) : lines,
  };
}

// ------------------------------------------------------------------ 结果摘要

/** 部署记录 + MCP 工具清单 → 成功页要展示的结果摘要 */
export function buildDeployResult(
  deployment: Deployment,
  toolNames?: string[] | null,
): DeployResult {
  const tools = (toolNames ?? []).filter((name) => Boolean(name));
  return {
    hostId: deployment.hostId,
    hostName: deployment.hostName,
    hostAddress: deployment.hostAddress,
    mcpUrl: deployment.mcpUrl,
    mcpVersion: deployment.mcpVersion,
    mcpToolCount:
      typeof deployment.mcpToolCount === 'number' && deployment.mcpToolCount > 0
        ? deployment.mcpToolCount
        : tools.length,
    toolNames: tools,
    pythonPath: deployment.pythonPath,
    pythonVersion: deployment.pythonVersion,
    vbsPath: deployment.vbsPath,
    startupPath: deployment.startupPath,
    portproxy: Boolean(deployment.portproxy),
    firewall: Boolean(deployment.firewall),
    mcpPort: Number(deployment.mcpPort) || 8000,
  };
}

/** 版本清单里挑出默认选中项：最新且支持 windows-mcp 的那个（没有则 null） */
export function defaultVersionItem(
  items: PythonVersionItem[],
): PythonVersionItem | null {
  const compatible = items.filter((item) => item.mcpCompatible);
  return compatible[0] ?? null;
}

/** 表单草稿的初始值（便于测试与「再部署一台」时复位） */
export function freshOptions(
  overrides: Partial<DeployOptionDraft> = {},
): DeployOptionDraft {
  return { ...DEFAULT_DEPLOY_OPTIONS, ...overrides };
}

/** 从部署记录里读回当时用的 pip 镜像/端口（「查看记录」面板用） */
export function optionsSummary(deployment: Deployment): string {
  const options = (deployment.options ?? {}) as Record<string, unknown>;
  const parts: string[] = [];
  const port = Number(options.mcpPort);
  if (Number.isFinite(port) && port > 0)
    parts.push(t('MCP 端口 {0}', { 0: port }));
  if (options.pipIndex)
    parts.push(t('pip 源 {0}', { 0: String(options.pipIndex) }));
  parts.push(options.bindAll ? t('监听所有网卡') : t('仅本机/堡垒机可达'));
  parts.push(options.authKey ? t('已启用访问密钥') : t('未启用访问密钥'));
  return parts.join(' · ');
}
