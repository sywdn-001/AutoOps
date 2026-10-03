/**
 * WebRDP（Windows 远程桌面）控制台的类型、常量与工具函数。
 *
 * 形态与网页终端一致：**一次连接 = 一个 `window.open()` 弹出的独立浏览器窗口**
 * （`/rdp/console`，`layout: false` 不进菜单）。区别只在协议：终端是 SSH 字符流，
 * 这里是 RDP 图形流 —— 浏览器里的 ironrdp-wasm 直连堡垒机的 `/api/rdp/ws` 隧道，
 * 隧道另一头才是目标机的 3389。
 */

/** 连接状态机（比终端少两态：RDP 没有「选主机」这一步，进窗口就已经选定）。 */
export type RdpStatus = 'connecting' | 'connected' | 'disconnected' | 'failed';

export const RDP_STATUS_META: Record<
  RdpStatus,
  { text: string; color: string; badge: string }
> = {
  connecting: { text: '正在协商', color: 'processing', badge: 'processing' },
  connected: { text: '已连接', color: 'success', badge: 'success' },
  disconnected: { text: '已断开', color: 'default', badge: 'default' },
  failed: { text: '连接失败', color: 'error', badge: 'error' },
};

/**
 * ironrdp-wasm 的错误码翻译（`IronErrorKind`）。
 *
 * 原始错误（英文 + 错误码）会跟在中文说明后面一起显示 —— 排障要用原文，
 * 但第一眼必须让人看懂「到底哪一步不行」。
 */
const IRON_ERROR_TEXT: Record<number, string> = {
  0: '客户端内部错误',
  1: '账号或口令不正确',
  2: 'Windows 登录被拒绝（账号可能没有远程登录权限）',
  3: '目标机拒绝访问',
  4: 'RDCleanPath 隧道协商失败',
  5: '堡垒机连不上目标机的 3389 端口',
  6: '安全层协商失败（目标机可能要求 NLA/SSL）',
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null;

const numberValue = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined;

/** 调一个可能不存在、也可能抛异常的方法（wasm 包装类的方法会 `free()` 后失效）。 */
const callValue = (source: Record<string, unknown>, key: string): unknown => {
  const fn = source[key];
  if (typeof fn !== 'function') {
    return undefined;
  }
  try {
    return (fn as () => unknown).call(source);
  } catch {
    return undefined;
  }
};

const callNumber = (
  source: Record<string, unknown>,
  key: string,
): number | undefined => numberValue(callValue(source, key));

const callText = (source: Record<string, unknown>, key: string): string => {
  const value = callValue(source, key);
  return typeof value === 'string' ? value.trim() : '';
};

const kindFromText = (text: string): number | undefined => {
  const hit =
    /IronErrorKind[::\s]*([0-9]+)/i.exec(text) ??
    /kind[:\s]+([0-9]+)/i.exec(text);
  return hit ? Number(hit[1]) : undefined;
};

/** 兜底：至少把对象里有什么字段说清楚，别再出现 `[object Object]`。 */
const describeShape = (error: unknown): string => {
  if (error === null || error === undefined) {
    return String(error);
  }
  if (isRecord(error)) {
    const names = Object.getOwnPropertyNames(error)
      .filter((name) => name !== 'free' && name !== 'constructor')
      .slice(0, 6);
    return names.length > 0 ? names.join('、') : '空对象';
  }
  return String(error);
};

/**
 * 把 wasm 抛出的错误翻成「中文说明 + 原始信息」。
 *
 * `ironrdp-wasm` 的 `IronError` 是 wasm-bindgen 包装的类，**不是 JS 的 `Error` 子类**：
 * 错误码要调 `kind()`，细节要调 `backtrace()` / `rdcleanpathDetails()`，
 * 直接 `String(error)` 只会得到 `[object Object]`（这正是线上第一次联调时看到的报错）。
 */
export const describeRdpError = (error: unknown): string => {
  const record = isRecord(error) ? error : {};
  const message =
    error instanceof Error
      ? error.message.trim()
      : typeof error === 'string'
        ? error.trim()
        : '';
  const kind = callNumber(record, 'kind') ?? kindFromText(message);
  const friendly = kind === undefined ? undefined : IRON_ERROR_TEXT[kind];
  const rawDetails = isRecord(error)
    ? callValue(record, 'rdcleanpathDetails')
    : undefined;
  const details = isRecord(rawDetails) ? rawDetails : {};
  const http =
    numberValue(details.httpStatusCode) ?? numberValue(details.errorCode);
  const origin =
    message ||
    callText(record, 'backtrace') ||
    `未知错误（${describeShape(error)}）`;
  if (http === 401 || http === 403) {
    return `堡垒机拒绝了这次连接：${origin}`;
  }
  if (http === 502 || http === 503) {
    return `堡垒机连不上目标机，或与目标机的安全层协商失败（HTTP ${http}）：${origin}`;
  }
  if (http !== undefined && http >= 400) {
    return `RDCleanPath 隧道握手失败（HTTP ${http}）：${origin}`;
  }
  if (friendly) {
    return `${friendly}（${origin}）`;
  }
  return origin;
};

/**
 * 控制台页地址。远程桌面列表点「连接」时用它 `window.open(url, name, features)` 弹独立窗口；
 * 窗口自己会再调 `POST /api/rdp/sessions` 复验一次权限并换票（URL 可以被手工改，前端参数不作数）。
 */
export const buildRdpConsoleUrl = (options: {
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
  return `/rdp/console?${params.toString()}`;
};

/** WebSocket 隧道地址：同源，协议随页面（https → wss）。 */
export const buildRdpWsUrl = (wsPath: string): string => {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${window.location.host}${wsPath}`;
};
