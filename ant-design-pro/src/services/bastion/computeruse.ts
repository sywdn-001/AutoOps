/**
 * ComputerUse（Windows-MCP）部署接口封装。
 *
 * 覆盖 `/api/computer-use` 下的一组接口：镜像版本清单、安装包缓存、可选主机、
 * 部署记录（创建/查询/取消/删除）与实时状态；另外提供部署日志的 SSE 帮助函数
 * （`EventSource` 把 JWT 放在查询串 `?token=`，后端同时从 header 与 query 取 token）。
 * 所有函数返回**解包后的业务数据**，失败时抛出的错误与其它 bastion 服务一致。
 */
import { request } from '@umijs/max';
import { getToken, unwrap } from './client';
import type { ApiData } from './types';

/** 接口前缀：与后端蓝图 `bp = Blueprint(..., url_prefix="/api/computer-use")` 对应 */
export const COMPUTER_USE_BASE = '/api/computer-use';

/** 部署阶段 key（顺序固定，与后端 `app/computeruse/deploy.py::STAGES` 一致） */
export type DeploymentStage =
  | 'queued'
  | 'prepare'
  | 'upload'
  | 'install'
  | 'pip'
  | 'vbs'
  | 'expose'
  | 'verify';

/** 部署状态 */
export type DeploymentStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'cancelled';

/** 日志级别 */
export type LogLevel = 'info' | 'success' | 'warning' | 'error';

/** 单个阶段的进度锚点（阶段 key + 中文名 + 该阶段**完成时**的百分比） */
export type StageMeta = {
  key: DeploymentStage;
  label: string;
  progress: number;
};

/** 某次部署的完整阶段定义（后端 `deployment_snapshot()` 下发的 `stages`） */
export type StageEntry = {
  key: string;
  label: string;
  progress: number;
};

/**
 * 七个真实阶段的进度锚点（不含 `queued`）。
 *
 * 百分比是「该阶段做完时」的值，与后端 `STAGES` 的 `progress` 字段逐一对齐：
 * 6 / 28 / 48 / 68 / 82 / 92 / 100。
 */
export const STAGE_SEQUENCE: StageMeta[] = [
  { key: 'prepare', label: '连接目标机 / 环境体检', progress: 6 },
  { key: 'upload', label: '上传 Python 安装包到 C:\\Installer', progress: 28 },
  { key: 'install', label: '静默安装 Python', progress: 48 },
  { key: 'pip', label: '安装 windows-mcp', progress: 68 },
  { key: 'vbs', label: '写开机自启 VBS 并启动 MCP', progress: 82 },
  { key: 'expose', label: '开 portproxy + 防火墙', progress: 92 },
  { key: 'verify', label: '探活 MCP（tools/list）', progress: 100 },
];

/** 镜像清单里的一条 Python 版本（真的存在 amd64 安装包才会出现） */
export type PythonVersionItem = {
  version: string;
  filename: string;
  url: string;
  size: number;
  /** 镜像站上的发布日期文本（华为云目录页给的字符串） */
  released: string;
  /** Python >= 3.13：windows-mcp 的硬要求 */
  mcpCompatible: boolean;
  cached: boolean;
  cachedAt: string | null;
  /** 本地缓存文件算出来的 SHA256（未缓存为空串） */
  sha256: string;
  localSize: number;
};

/** 已经缓存到堡垒机的安装包 */
export type CachedInstaller = {
  filename: string;
  version: string;
  size: number;
  sha256: string;
  cachedAt: string | null;
  path: string;
};

/** `GET /versions` 的返回 */
export type VersionsResult = {
  mirror: string;
  /** 后端要求的最低 Python 版本（如 `3.13`） */
  minPython: string;
  fetchedAt: number;
  items: PythonVersionItem[];
  cached: CachedInstaller[];
};

/** `POST /installers` 的返回（`skipped=true` 表示本来就缓存过） */
export type CacheInstallerResult = {
  path: string;
  filename: string;
  size: number;
  sha256: string;
  version: string;
  cachedAt?: string | null;
  skipped: boolean;
};

/** 目标主机上的一个资产账号（后端只给口令认证的账号） */
export type ComputerUseAccount = {
  id: number;
  name: string;
  username: string;
  authType: string;
};

/** 一条部署记录 */
export type Deployment = {
  id: number;
  hostId: number;
  hostName: string;
  hostAddress: string;
  accountId: number | null;
  accountUsername: string;
  status: DeploymentStatus;
  stage: string;
  stageLabel: string;
  progress: number;
  stages?: StageEntry[];
  pythonVersion: string;
  installerName: string;
  installerSize: number;
  installerSha256: string;
  pythonPath: string;
  mcpVersion: string;
  mcpExe: string;
  mcpHost: string;
  mcpPort: number;
  mcpUrl: string;
  mcpToolCount: number;
  mcpTools: unknown[];
  hasAuthKey: boolean;
  vbsPath: string;
  startupPath: string;
  portproxy: boolean;
  options: Record<string, unknown>;
  bastionUrl: string;
  deployedBy: string;
  logTail: string;
  error: string;
  startedAt: string | null;
  finishedAt: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  /** 日志流里已经发到第几个事件（用于断线重连的 `since`） */
  lastSeq?: number;
  /** 服务端此刻是否还有 runner 在跑 */
  live?: boolean;
};

/** `GET /hosts` 的一台候选主机 */
export type ComputerUseHost = {
  id: number;
  name: string;
  address: string;
  osType: string;
  /** 有没有 WinRM 端点；没有的主机不能部署 */
  hasWinrm: boolean;
  winrmPort: number | null;
  winrmTransport: string;
  accounts: ComputerUseAccount[];
  /** 最近一次部署记录（可能失败/取消） */
  deployment: Deployment | null;
  /** 最近一次部署成功且探活通过 */
  ready: boolean;
};

/** `GET /hosts` 的返回 */
export type HostsResult = {
  items: ComputerUseHost[];
  minPython: string;
};

/** 部署时可选参数（对应后端 `options`） */
export type DeployOptions = {
  mcpPort?: number;
  pipIndex?: string;
  bindAll?: boolean;
  authKey?: string;
};

/** `POST /deployments` 的请求体 */
export type DeployPayload = {
  hostId: number;
  accountId: number;
  pythonVersion: string;
  installerName: string;
  bastionUrl?: string;
  options: DeployOptions;
};

/** 一条部署日志 */
export type DeploymentLogEvent = {
  seq: number;
  ts?: number;
  /** 服务端拼好的 `HH:MM:SS` */
  time: string;
  level: LogLevel;
  stage: string;
  stageLabel: string;
  message: string;
  /** 该日志对应阶段的完成百分比（后端给的是「已完成」语义） */
  progress: number;
};

/** 流里的 `status` 消息 */
export type DeploymentStatusEvent = {
  id: number;
  status: DeploymentStatus;
  stage: string;
  stageLabel: string;
  progress: number;
  error: string;
  lastSeq: number;
  live: boolean;
};

/** 主机上 MCP 的一个工具 */
export type McpTool = {
  name: string;
  description: string;
  mutating: boolean;
  observeOnly: boolean;
};

/** `GET /status` 的返回 */
export type ComputerUseStatus = {
  hostId: number;
  hostName: string;
  hostAddress: string;
  ready: boolean;
  deployment: Deployment | null;
  deploymentId?: number;
  mcpUrl?: string;
  mcpHost?: string;
  mcpPort?: number;
  mcpVersion?: string;
  pythonPath?: string;
  toolCount?: number;
  tools?: McpTool[];
  hasAuthKey?: boolean;
  portproxy?: boolean;
  startupPath?: string;
};

/** SSE 帧类型 */
export type LogStreamMessage =
  | { type: 'hello'; deployment: Deployment }
  | { type: 'log'; event: DeploymentLogEvent }
  | { type: 'status' } & DeploymentStatusEvent
  | { type: 'end'; status: DeploymentStatus };

// ------------------------------------------------------------- SSE 帧解析（纯函数）

const LEVEL_PATTERN = /\[(info|success|warning|error)\]/;

/** 归一化日志级别：只认后端给的四个值，其它一律当 `info` */
export function normalizeLogLevel(value?: string | null): LogLevel {
  const text = (value ?? '').trim().toLowerCase();
  if (
    text === 'info' ||
    text === 'success' ||
    text === 'warning' ||
    text === 'error'
  ) {
    return text;
  }
  return 'info';
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function toStatus(value: unknown): DeploymentStatus {
  const text = typeof value === 'string' ? value : '';
  if (text === 'success' || text === 'failed' || text === 'cancelled') {
    return text;
  }
  if (text === 'running') return 'running';
  return 'pending';
}

function toNumber(value: unknown, fallback = 0): number {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
}

function toText(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** 本地时间戳 → `HH:MM:SS`（只用于后端漏给 `time` 时的兜底） */
function formatClock(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return '';
  const date = new Date(ts * 1000);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * 把 SSE 的一帧 `data:` 文本解析成已归类的事件；坏帧返回 `null`（丢掉，不炸页面）。
 *
 * 后端只有四类帧：`hello`（第一条，带整条部署记录）、`log`、`status`、`end`。
 * 除了 `hello`，其它帧的字段都用容错读法（缺字段 / 类型不对也不会抛异常）。
 */
export function parseLogStreamMessage(raw: string): LogStreamMessage | null {
  const text = (raw ?? '').trim();
  if (!text) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  const record = asRecord(parsed);
  if (!record) return null;
  const type = toText(record.type);

  if (type === 'log') {
    const event = asRecord(record.event);
    if (!event) return null;
    const message = toText(event.message);
    const rawLevel = toText(event.level);
    const rawTime = toText(event.time);
    const ts = Number(event.ts);
    return {
      type: 'log',
      event: {
        seq: toNumber(event.seq),
        ts: Number.isFinite(ts) && ts > 0 ? ts : undefined,
        // 后端按 `%H:%M:%S` 给好 time；万一缺了就用 ts 自己格式化
        time: rawTime || formatClock(ts),
        // 后端偶尔漏 level 时从消息里的 `[error]` 标记兜底
        level: rawLevel
          ? normalizeLogLevel(rawLevel)
          : normalizeLogLevel(LEVEL_PATTERN.exec(message)?.[1]),
        stage: toText(event.stage),
        stageLabel: toText(event.stageLabel),
        message,
        progress: toNumber(event.progress),
      },
    };
  }

  if (type === 'hello') {
    const deployment = asRecord(record.deployment);
    if (!deployment) return null;
    return { type: 'hello', deployment: deployment as unknown as Deployment };
  }

  if (type === 'status') {
    return {
      type: 'status',
      id: toNumber(record.id),
      status: toStatus(record.status),
      stage: toText(record.stage),
      stageLabel: toText(record.stageLabel),
      progress: toNumber(record.progress),
      error: toText(record.error),
      lastSeq: toNumber(record.lastSeq),
      live: Boolean(record.live),
    };
  }

  if (type === 'end') {
    return { type: 'end', status: toStatus(record.status) };
  }

  return null;
}

export const computerUseApi = {
  /** 镜像里「真的能下到 amd64 安装包」的 Python 版本清单（refresh=true 时绕过 1 小时缓存） */
  versions: async (refresh = false): Promise<VersionsResult> =>
    unwrap(
      request<ApiData<VersionsResult>>(`${COMPUTER_USE_BASE}/versions`, {
        method: 'GET',
        params: { refresh: refresh ? 1 : 0 },
      }),
    ),

  /** 已经缓存到堡垒机的安装包 */
  installers: async (): Promise<{ items: CachedInstaller[]; dir: string }> =>
    unwrap(
      request<ApiData<{ items: CachedInstaller[]; dir: string }>>(
        `${COMPUTER_USE_BASE}/installers`,
        { method: 'GET' },
      ),
    ),

  /** 把安装包缓存到堡垒机（约 28 MB，可能要一两分钟） */
  cacheInstaller: async (payload: {
    version: string;
    filename: string;
  }): Promise<CacheInstallerResult> =>
    unwrap(
      request<ApiData<CacheInstallerResult>>(
        `${COMPUTER_USE_BASE}/installers`,
        { method: 'POST', data: payload },
      ),
    ),

  /** 删除一个已缓存的安装包 */
  removeInstaller: async (filename: string): Promise<void> => {
    await unwrap(
      request<ApiData<null>>(
        `${COMPUTER_USE_BASE}/installers/${encodeURIComponent(filename)}`,
        { method: 'DELETE' },
      ),
    );
  },

  /** 可部署的主机（带账号与最近一次部署） */
  hosts: async (): Promise<HostsResult> =>
    unwrap(
      request<ApiData<HostsResult>>(`${COMPUTER_USE_BASE}/hosts`, {
        method: 'GET',
      }),
    ),

  /** 建一条部署并立刻起后台线程（拿 id 去订阅日志流） */
  createDeployment: async (payload: DeployPayload): Promise<Deployment> =>
    unwrap(
      request<ApiData<Deployment>>(`${COMPUTER_USE_BASE}/deployments`, {
        method: 'POST',
        data: payload,
      }),
    ),

  /** 部署记录（不传 hostId 则返回最近 50 条） */
  deployments: async (hostId?: number): Promise<Deployment[]> =>
    unwrap(
      request<ApiData<{ items: Deployment[] }>>(
        `${COMPUTER_USE_BASE}/deployments`,
        { method: 'GET', params: hostId ? { hostId } : undefined },
      ),
    ).then((data) => data.items),

  /** 单条部署详情 */
  deployment: async (id: number): Promise<Deployment> =>
    unwrap(
      request<ApiData<Deployment>>(
        `${COMPUTER_USE_BASE}/deployments/${id}`,
        { method: 'GET' },
      ),
    ),

  /** 取消正在跑的部署（接口要求 2xx，不代表线程立刻停） */
  cancelDeployment: async (id: number): Promise<Deployment> =>
    unwrap(
      request<ApiData<Deployment>>(
        `${COMPUTER_USE_BASE}/deployments/${id}/cancel`,
        { method: 'POST' },
      ),
    ),

  /** 删除一条部署记录 */
  removeDeployment: async (id: number): Promise<void> => {
    await unwrap(
      request<ApiData<null>>(`${COMPUTER_USE_BASE}/deployments/${id}`, {
        method: 'DELETE',
      }),
    );
  },

  /** 主机上的 MCP 实时状态（探活结果 + 工具清单） */
  status: async (hostId: number): Promise<ComputerUseStatus> =>
    unwrap(
      request<ApiData<ComputerUseStatus>>(`${COMPUTER_USE_BASE}/status`, {
        method: 'GET',
        params: { hostId },
      }),
    ),
};

/**
 * 拼部署日志流的地址（`EventSource` 不能自定义请求头，所以 JWT 放查询串）。
 *
 * `since` 用于断线重连时补日志：只取比它更新的行；不传则从第一行开始重放。
 */
export function buildLogStreamUrl(
  deploymentId: number,
  since?: number,
  token?: string,
): string {
  const params = new URLSearchParams();
  if (typeof since === 'number' && Number.isFinite(since) && since > 0) {
    params.set('since', String(since));
  }
  const jwt = token ?? getToken();
  if (jwt) params.set('token', jwt);
  const query = params.toString();
  const base = `${COMPUTER_USE_BASE}/deployments/${deploymentId}/logs`;
  return query ? `${base}?${query}` : base;
}

/** 部署日志订阅句柄 */
export type LogStreamHandle = { close: () => void };

/**
 * 订阅一条部署的日志流。
 *
 * 后端在 `end` 帧之后主动关流，浏览器自带的自动重连会带着 `?since=` 重新连上——
 * 此时记录已经不在跑，服务端会把最后一条 `end` 再发一次，所以回调是幂等的。
 */
export function openLogStream(
  deploymentId: number,
  onMessage: (message: LogStreamMessage) => void,
  options: { since?: number; onError?: () => void } = {},
): LogStreamHandle {
  const { since, onError } = options;
  const source = new EventSource(buildLogStreamUrl(deploymentId, since));
  source.onmessage = (event: MessageEvent<string>) => {
    const message = parseLogStreamMessage(event.data);
    if (message) onMessage(message);
  };
  if (onError) source.onerror = () => onError();
  return {
    close: () => source.close(),
  };
}
