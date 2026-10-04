/**
 * 网页终端控制台的类型与常量。
 *
 * 形态对齐 JumpServer：**一次连接 = 一个 `window.open()` 弹出的独立浏览器窗口**
 * （luna 里点资产后新开控制台窗口），所以这里描述的是「一条会话」而不是「一页里的多个标签」。
 * 状态机沿用 JumpServer `ui/composables/useWorkspaceTabs.ts:21` 的
 * `selecting | connecting | ready | connected | disconnected | failed`。
 */

/** 连接状态机（六态）。 */
export type ConnectionStatus =
  | 'selecting'
  | 'connecting'
  | 'ready'
  | 'connected'
  | 'disconnected'
  | 'failed';

/** 当前窗口里唯一的那条会话。 */
export type TerminalSession = {
  hostId: number;
  accountId: number;
  hostName: string;
  /** 展示用地址（服务端 `terminal:opened` 会给「主机:端口」）。 */
  address: string;
  port: number;
  accountUsername: string;
  policyName?: string;
  /**
   * 会话协议：`ssh` = Linux 命令行，`winrm` = Windows 命令行（服务端按主机协议
   * 决定用 SSH channel 还是 WinRS，前端只用来在状态条上显示通道类型）。
   */
  protocol: string;
  status: ConnectionStatus;
  /** 连接建立时刻（毫秒），用于状态条上的「连接时长」每秒跳动。 */
  connectedAt?: number;
  /** 目标机提示符改写是否生效（false = 降级为原始录制模式）。 */
  segmented?: boolean;
  /** 会话号（服务端 session sid）。 */
  sid?: string;
  /** 失败原因（失败浮层与状态条共用）。 */
  error?: string;
  /** 断开原因（已翻译成中文）。 */
  closedReason?: string;
  /** 终端字号（JumpServer 默认 13，夹取 5~50）。 */
  fontSize: number;
};

export const STATUS_META: Record<
  ConnectionStatus,
  { text: string; color: string; badge: string }
> = {
  selecting: { text: '未连接', color: 'default', badge: 'default' },
  connecting: { text: '连接中', color: 'processing', badge: 'processing' },
  ready: { text: '连接中', color: 'processing', badge: 'processing' },
  connected: { text: '已连接', color: 'success', badge: 'success' },
  disconnected: { text: '已断开', color: 'default', badge: 'default' },
  failed: { text: '连接失败', color: 'error', badge: 'error' },
};

/**
 * 关闭原因映射（JumpServer `ui/koko/composables/terminal/protocol.ts:79-113`
 * 的 closeReasonKeys 等价物）。后端可能给中文原因，也可能给短码。
 */
const CLOSE_REASON_TEXT: Record<string, string> = {
  idle_disconnect: '超过最大空闲时间',
  max_session_timeout: '达到会话最大时长',
  permission_expired: '连接权限已过期',
  admin_terminate: '管理员终止会话',
  connect_disconnect: '资产侧连接已结束',
  connect_failed: '连接建立失败',
  initialization_failed: '会话初始化失败',
  read_timeout: '读取超时',
  write_timeout: '写入超时',
  write_failed: '写入失败',
  request_canceled: '服务已关闭该会话',
  share_removed: '已从共享会话移除',
};

export const describeCloseReason = (reason?: string): string => {
  if (!reason) {
    return '会话已结束';
  }
  const key = reason.trim();
  const mapped = CLOSE_REASON_TEXT[key] ?? CLOSE_REASON_TEXT[key.split(':')[0]];
  return mapped ? `${mapped}（${key}）` : key;
};

/** 连接时长格式：>1h 用 H:MM:SS，否则 MM:SS（各段补零）。 */
export const formatDuration = (milliseconds: number): string => {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (value: number) => String(value).padStart(2, '0');
  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(seconds)}`
    : `${pad(minutes)}:${pad(seconds)}`;
};

export const MIN_FONT_SIZE = 5;
export const MAX_FONT_SIZE = 50;
export const DEFAULT_FONT_SIZE = 13;
export const clampFontSize = (value: number): number =>
  Math.min(MAX_FONT_SIZE, Math.max(MIN_FONT_SIZE, Math.round(value)));

/**
 * 控制台页地址。资产列表点「连接」时用它 `window.open(url, name, features)` 弹出一个独立窗口，
 * 窗口自己再去 `terminalApi.check()` 复验一次权限（URL 可以被手工改）。
 */
export const buildConsoleUrl = (options: {
  hostId: number;
  accountId: number;
  hostName?: string;
}): string => {
  const params = new URLSearchParams({
    hostId: String(options.hostId),
    accountId: String(options.accountId),
  });
  if (options.hostName) {
    params.set('title', options.hostName);
  }
  return `/terminal/console?${params.toString()}`;
};

/**
 * 文件管理器窗口地址（终端窗口工具条上的「文件管理」按钮用它弹窗）。
 *
 * 同样是 `layout: false` 的独立窗口，一个窗口一条 SFTP 会话；窗口自己会再调
 * `fileApi.open()` 复验一次访问控制（URL 可以被手工改，前端参数不作数）。
 */
export const buildFileConsoleUrl = (options: {
  hostId: number;
  accountId?: number | null;
  hostName?: string;
}): string => {
  const params = new URLSearchParams({ hostId: String(options.hostId) });
  if (options.accountId) {
    params.set('accountId', String(options.accountId));
  }
  if (options.hostName) {
    params.set('title', options.hostName);
  }
  return `/files/console?${params.toString()}`;
};

/**
 * 弹出式窗口的尺寸：随屏幕收放，留出边距，避免在小屏上超出可视区。
 * 终端窗口与文件管理器窗口共用同一套尺寸口径。
 */
export const consoleWindowFeatures = (): string => {
  const availW = window.screen?.availWidth || window.innerWidth || 1280;
  const availH = window.screen?.availHeight || window.innerHeight || 800;
  const width = Math.max(800, Math.min(1440, Math.round(availW * 0.82)));
  const height = Math.max(520, Math.min(900, Math.round(availH * 0.85)));
  const left = Math.max(0, Math.round((availW - width) / 2));
  const top = Math.max(0, Math.round((availH - height) / 2));
  return [
    'popup=yes',
    `width=${width}`,
    `height=${height}`,
    `left=${left}`,
    `top=${top}`,
    'menubar=no',
    'toolbar=no',
    'location=no',
    'status=no',
    'resizable=yes',
    'scrollbars=yes',
  ].join(',');
};
