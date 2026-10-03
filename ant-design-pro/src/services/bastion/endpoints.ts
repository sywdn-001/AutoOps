/**
 * 堡垒机后端接口集合（唯一入口）。
 *
 * 每个域一个对象，页面只从这里取数据，避免各页各写一套 URL / 参数名。
 * 所有函数返回解包后的业务数据；失败由 umi request 抛出（含后端 message）。
 */
import { request } from '@umijs/max';
import { createResource, getToken, pageParams, unwrap } from './client';
import type {
  ApiData,
  ApiList,
  AuditChainStatus,
  AuditChainVerifyResult,
  AuditItem,
  CommandItem,
  DashboardMine,
  DashboardOverview,
  FileCapabilities,
  FileDecision,
  FileListResult,
  FileLogItem,
  FileLogOptions,
  FileMatchType,
  FilePolicyAction,
  FilePolicyItem,
  FilePolicyOption,
  FileReadResult,
  FileRuleItem,
  FileSessionItem,
  FileStatResult,
  FileUploadResult,
  GatewayStatus,
  GrantItem,
  GrantMatrixResult,
  HostAccountItem,
  HostGroupItem,
  HostItem,
  OptionItem,
  PageParams,
  PermissionGroup,
  PolicyDecision,
  PolicyItem,
  PolicyRuleItem,
  RiskLevel,
  RoleItem,
  SessionItem,
  SettingsPayload,
  TargetCheckResult,
  TerminalTarget,
  UserItem,
} from './types';

// ---------------------------------------------------------------- 认证

/** Pro 兼容登录信封（登录页直接用，token 在 data.token） */
export type LoginEnvelope = ApiData<{
  status: string;
  type: string;
  currentAuthority: string;
  token: string;
  user?: UserItem;
}>;

export const authApi = {
  /** Pro 兼容登录：POST /api/login/account */
  proLogin: (body: { username: string; password: string; type?: string }) =>
    request<LoginEnvelope>('/api/login/account', { method: 'POST', data: body }),
  /** 原生登录：POST /api/auth/login */
  login: (body: { username: string; password: string }) =>
    unwrap(
      request<ApiData<{ token: string; expiresIn: number; user: UserItem }>>(
        '/api/auth/login',
        { method: 'POST', data: body },
      ),
    ),
  logout: () => unwrap(request<ApiData<null>>('/api/auth/logout', { method: 'POST' })),
  me: () =>
    unwrap(
      request<
        ApiData<UserItem & { isAdmin: boolean; recentActivities: AuditItem[] }>
      >('/api/auth/me', { method: 'GET' }),
    ),
  changePassword: (body: { oldPassword: string; newPassword: string }) =>
    unwrap(request<ApiData<null>>('/api/auth/password', { method: 'POST', data: body })),
  proLogout: () =>
    unwrap(request<ApiData<null>>('/api/login/outLogin', { method: 'POST' })),
  notices: () =>
    unwrap(
      request<
        ApiData<{
          list: {
            id: number;
            title: string;
            datetime: string;
            type: string;
            read: boolean;
            extra?: string;
          }[];
          total: number;
        }>
      >('/api/notices', { method: 'GET' }),
    ),
};

// ---------------------------------------------------------------- 用户与角色

export type UserPayload = {
  username?: string;
  displayName?: string;
  password?: string;
  roleId?: number;
  roleCode?: string;
  email?: string;
  phone?: string;
  remark?: string;
  status?: string;
  isSuperuser?: boolean;
  gatewayEnabled?: boolean;
  webtermEnabled?: boolean;
  mustChangePassword?: boolean;
};

const userResource = createResource<UserItem, UserPayload>('/api/users');

export const userApi = {
  ...userResource,
  options: () =>
    unwrap(request<ApiData<OptionItem[]>>('/api/users/options', { method: 'GET' })),
  resetPassword: (id: number, password: string) =>
    unwrap(
      request<ApiData<null>>(`/api/users/${id}/password`, {
        method: 'POST',
        data: { password },
      }),
    ),
  unlock: (id: number) =>
    unwrap(request<ApiData<UserItem>>(`/api/users/${id}/unlock`, { method: 'POST' })),
};

export type RolePayload = {
  code?: string;
  name?: string;
  description?: string;
  permissions?: string[];
};

const roleResource = createResource<RoleItem, RolePayload>('/api/roles');

export const roleApi = {
  ...roleResource,
  options: () =>
    unwrap(request<ApiData<OptionItem[]>>('/api/roles/options', { method: 'GET' })),
  permissions: () =>
    unwrap(
      request<ApiData<PermissionGroup[]>>('/api/roles/permissions', { method: 'GET' }),
    ),
};

// ---------------------------------------------------------------- 主机与账号

export type HostPayload = {
  name?: string;
  address?: string;
  port?: number;
  protocol?: string;
  osType?: string;
  groupId?: number | null;
  description?: string;
  status?: string;
  tags?: string[];
};

export type HostDetail = HostItem & {
  accounts: HostAccountItem[];
  grants: GrantItem[];
};

const hostResource = createResource<HostItem, HostPayload>('/api/hosts');

export const hostApi = {
  ...hostResource,
  getDetail: (id: number) =>
    unwrap(request<ApiData<HostDetail>>(`/api/hosts/${id}`, { method: 'GET' })),
  options: () =>
    unwrap(request<ApiData<OptionItem[]>>('/api/hosts/options', { method: 'GET' })),
  accounts: (hostId: number) =>
    unwrap(
      request<ApiData<HostAccountItem[]>>(`/api/hosts/${hostId}/accounts`, {
        method: 'GET',
      }),
    ),
};

export type AccountPayload = {
  name?: string;
  username?: string;
  authType?: 'password' | 'key';
  password?: string;
  privateKey?: string;
  passphrase?: string;
  sudoCommand?: string;
  description?: string;
};

export type AccountTestResult = {
  ok: boolean;
  message: string;
  hostName: string;
  accountName: string;
  exitStatus: number | null;
  serverVersion: string;
  clientVersion: string;
  fingerprint: string;
  output: string;
  elapsedMs: number;
};

export const accountApi = {
  create: (hostId: number, data: AccountPayload) =>
    unwrap(
      request<ApiData<HostAccountItem>>(`/api/hosts/${hostId}/accounts`, {
        method: 'POST',
        data,
      }),
    ),
  update: (hostId: number, id: number, data: AccountPayload) =>
    unwrap(
      request<ApiData<HostAccountItem>>(`/api/hosts/${hostId}/accounts/${id}`, {
        method: 'PUT',
        data,
      }),
    ),
  remove: async (hostId: number, id: number) => {
    await unwrap(
      request<ApiData<null>>(`/api/hosts/${hostId}/accounts/${id}`, { method: 'DELETE' }),
    );
  },
  /** 真实 SSH 连通性测试（跑 uname -a） */
  test: (hostId: number, id: number) =>
    unwrap(
      request<ApiData<AccountTestResult>>(
        `/api/hosts/${hostId}/accounts/${id}/test`,
        { method: 'POST' },
      ),
    ),
};

export type HostGroupPayload = { name?: string; description?: string };

const groupResource = createResource<HostGroupItem, HostGroupPayload>(
  '/api/host-groups',
);

export const hostGroupApi = { ...groupResource };

// ---------------------------------------------------------------- 授权

export type GrantPayload = {
  userId?: number;
  hostId?: number;
  hostAccountId?: number | null;
  policyId?: number | null;
  filePolicyId?: number | null;
  canLogin?: boolean;
  canSftp?: boolean;
  canUpload?: boolean;
  canDownload?: boolean;
  canFileWrite?: boolean;
  canPortForward?: boolean;
  canWebterm?: boolean;
  timeStart?: string | null;
  timeEnd?: string | null;
  weekdays?: number[];
  expireAt?: string | null;
  maxSessions?: number;
  enabled?: boolean;
  remark?: string;
};

const grantResource = createResource<GrantItem, GrantPayload>('/api/grants');

export const grantApi = {
  ...grantResource,
  batch: (payload: GrantPayload & { hostIds: number[] }) =>
    unwrap(
      request<ApiData<{ created: number[]; skipped: { hostId: number; reason: string }[] }>>(
        '/api/grants/batch',
        { method: 'POST', data: payload },
      ),
    ),
  preview: (payload: { policyId?: number; command: string }) =>
    unwrap(
      request<ApiData<PolicyDecision>>('/api/grants/preview', {
        method: 'POST',
        data: payload,
      }),
    ),
  matrix: (params: PageParams = {}) =>
    request<GrantMatrixResult>('/api/grants/matrix', {
      method: 'GET',
      params: pageParams(params),
    }),
};

// ---------------------------------------------------------------- 命令策略

export type PolicyPayload = {
  name?: string;
  description?: string;
  defaultAction?: string;
  rules?: RulePayload[];
};

export type RulePayload = {
  policyId?: number;
  priority?: number;
  matchType?: string;
  pattern?: string;
  action?: string;
  riskLevel?: string;
  description?: string;
  enabled?: boolean;
};

const policyResource = createResource<PolicyItem, PolicyPayload>('/api/policies');

export const policyApi = {
  ...policyResource,
  getDetail: (id: number) =>
    unwrap(request<ApiData<PolicyItem>>(`/api/policies/${id}`, { method: 'GET' })),
  options: () =>
    unwrap(request<ApiData<OptionItem[]>>('/api/policies/options', { method: 'GET' })),
  resetBuiltin: () =>
    unwrap(request<ApiData<null>>('/api/policies/reset-builtin', { method: 'POST' })),
  test: (id: number, command: string) =>
    unwrap(
      request<ApiData<PolicyDecision>>(`/api/policies/${id}/test`, {
        method: 'POST',
        data: { command },
      }),
    ),
  evaluate: (command: string, policyId?: number) =>
    unwrap(
      request<ApiData<PolicyDecision>>('/api/policies/evaluate', {
        method: 'POST',
        data: { command, policyId },
      }),
    ),
};

const ruleResource = createResource<PolicyRuleItem, RulePayload>('/api/policy-rules');

export const ruleApi = {
  ...ruleResource,
  listByPolicy: (policyId?: number, params: PageParams = {}) =>
    request<ApiList<PolicyRuleItem>>('/api/policy-rules', {
      method: 'GET',
      params: pageParams({ ...params, policyId }),
    }),
};

// ---------------------------------------------------------------- 审计

// ---------------------------------------------------------------- 审计清除
/**
 * 清除请求体：`ids` 删除勾选的行；`all: true` 清空（可叠加与列表一致的筛选字段与 `before`）。
 * 后端要求范围**显式声明**（空 body / 只给筛选条件一律 400），且清除动作本身会写一条审计。
 */
export type PurgePayload = {
  ids?: number[];
  all?: boolean;
  before?: string;
  [key: string]: unknown;
};

export type PurgeResult = {
  deleted: number;
  scope: string;
  commands?: number;
  transcripts?: number;
  skippedActive?: number;
};

export const sessionApi = {
  list: (params: PageParams = {}) =>
    request<ApiList<SessionItem>>('/api/sessions', {
      method: 'GET',
      params: pageParams(params),
    }),
  // 会话清除：连带删除该会话的命令日志与录像文件；进行中的会话会被跳过
  purge: (payload: PurgePayload) =>
    unwrap(
      request<ApiData<PurgeResult>>('/api/sessions/delete', { method: 'POST', data: payload }),
    ),
  online: () =>
    unwrap(request<ApiData<SessionItem[]>>('/api/sessions/online', { method: 'GET' })),
  get: (id: number) =>
    unwrap(request<ApiData<SessionItem>>(`/api/sessions/${id}`, { method: 'GET' })),
  commands: (id: number, params: PageParams = {}) =>
    request<ApiList<CommandItem>>(`/api/sessions/${id}/commands`, {
      method: 'GET',
      params: pageParams(params),
    }),
  transcript: (id: number, params: { offset?: number; limit?: number; commandOnly?: boolean } = {}) =>
    unwrap(
      request<
        ApiData<{
          /**
           * 录制事件：后端把 JSONL 行**原样**返回，字段是 ``t``/``ts`` 与 snake_case
           * （``risk_level``/``rule_id``/``duration_ms``），不是驼峰的 ``type``/``riskLevel``。
           * t 取值：session_start / input / output / command / deny / notice / resize /
           * interactive_input / session_end。
           */
          events: {
            t?: string;
            ts?: string;
            seq?: number;
            command?: string;
            output?: string;
            output_bytes?: number;
            action?: string;
            risk_level?: string;
            reason?: string;
            rule_id?: number | null;
            data?: string;
            message?: string;
            cols?: number;
            rows?: number;
            sid?: string;
            host?: string;
            segmented?: boolean;
            duration_ms?: number;
            truncated?: boolean;
            uncertain?: boolean;
          }[];
          /** 下一页应传的 offset（按行计）；eof 表示本页没有新进展 */
          nextOffset: number;
          eof: boolean;
          size: number;
          stats?: {
            commands?: number;
            denied?: number;
            bytes?: number;
            first_ts?: string;
            last_ts?: string;
          };
        }>
      >(`/api/sessions/${id}/transcript`, { method: 'GET', params }),
    ),
  terminate: (id: number) =>
    unwrap(request<ApiData<null>>(`/api/sessions/${id}/terminate`, { method: 'POST' })),
};

export const commandApi = {
  list: (params: PageParams = {}) =>
    request<ApiList<CommandItem>>('/api/commands', {
      method: 'GET',
      params: pageParams(params),
    }),
  get: (id: number) =>
    unwrap(request<ApiData<CommandItem>>(`/api/commands/${id}`, { method: 'GET' })),
  purge: (payload: PurgePayload) =>
    unwrap(
      request<ApiData<PurgeResult>>('/api/commands/delete', { method: 'POST', data: payload }),
    ),
};

export const auditApi = {
  list: (params: PageParams = {}) =>
    request<ApiList<AuditItem>>('/api/audits', {
      method: 'GET',
      params: pageParams(params),
    }),
  get: (id: number) =>
    unwrap(request<ApiData<AuditItem>>(`/api/audits/${id}`, { method: 'GET' })),
  options: () =>
    unwrap(
      request<ApiData<{ categories: string[]; actions: string[] }>>(
        '/api/audits/options',
        { method: 'GET' },
      ),
    ),
  purge: (payload: PurgePayload) =>
    unwrap(request<ApiData<PurgeResult>>('/api/audits/delete', { method: 'POST', data: payload })),
  chainStatus: () =>
    unwrap(request<ApiData<AuditChainStatus>>('/api/audits/chain', { method: 'GET' })),
  chainVerify: (table?: 'audit_logs' | 'command_logs' | 'file_logs') =>
    unwrap(
      request<ApiData<AuditChainVerifyResult>>('/api/audits/chain/verify', {
        method: 'POST',
        params: table ? { table } : undefined,
      }),
    ),
};

export const dashboardApi = {
  overview: () =>
    unwrap(request<ApiData<DashboardOverview>>('/api/dashboard/overview', { method: 'GET' })),
  mine: () =>
    unwrap(request<ApiData<DashboardMine>>('/api/dashboard/mine', { method: 'GET' })),
};

// ---------------------------------------------------------------- 系统设置

export const settingApi = {
  get: () =>
    unwrap(request<ApiData<SettingsPayload>>('/api/settings', { method: 'GET' })),
  update: (values: Record<string, string | number | boolean>) =>
    unwrap(
      request<ApiData<SettingsPayload>>('/api/settings', {
        method: 'PUT',
        data: { values },
      }),
    ),
  reset: () =>
    unwrap(request<ApiData<SettingsPayload>>('/api/settings/reset', { method: 'POST' })),
  gateway: () =>
    unwrap(request<ApiData<GatewayStatus>>('/api/settings/gateway', { method: 'GET' })),
  reconcile: () =>
    unwrap(
      request<ApiData<{ reconciled: number }>>('/api/settings/maintenance/reconcile', {
        method: 'POST',
      }),
    ),
  seed: () =>
    unwrap(
      request<ApiData<Record<string, unknown>>>('/api/settings/maintenance/seed', {
        method: 'POST',
      }),
    ),
};

// ---------------------------------------------------------------- 网页终端

export const terminalApi = {
  targets: () =>
    unwrap(request<ApiData<TerminalTarget[]>>('/api/terminal/targets', { method: 'GET' })),
  check: (hostId: number, accountId?: number) =>
    unwrap(
      request<ApiData<TargetCheckResult>>(`/api/terminal/targets/${hostId}/check`, {
        method: 'POST',
        data: { accountId },
      }),
    ),
};

// ---------------------------------------------------------------- 文件管理器（SFTP）

/** 触发浏览器下载一个 Blob（下载接口需要 Bearer 头，所以走 fetch 自己接二进制）。 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // 立刻 revoke 会让部分浏览器来不及开始下载，留一点时间再回收
  window.setTimeout(() => URL.revokeObjectURL(url), 4000);
}

/** 从 Content-Disposition 里解析文件名（后端是 RFC5987 的 `filename*=UTF-8''…`）。 */
function filenameFrom(response: Response, fallback: string): string {
  const raw = response.headers.get('content-disposition') || '';
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(raw);
  if (utf8) {
    try {
      return decodeURIComponent(utf8[1]);
    } catch {
      return fallback;
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(raw);
  return plain ? plain[1] : fallback;
}

async function fetchBinary(url: string, fallbackName: string, sid: string): Promise<void> {
  const token = getToken();
  const response = await fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    // 失败的响应体是 JSON 信封，把后端的拒绝原因（谁拦的、命中哪条规则）原样抛出去
    let message = `下载失败（HTTP ${response.status}）`;
    try {
      const body = await response.json();
      message = body?.message || message;
    } catch {
      /* 保持默认文案 */
    }
    throw new Error(message);
  }
  const headerPath = response.headers.get('x-file-path');
  const name = headerPath ? decodeURIComponent(headerPath).split('/').pop() : '';
  saveBlob(await response.blob(), filenameFrom(response, name || fallbackName));
  void sid;
}

export const fileApi = {
  /** 打开一条文件管理器会话（一个窗口一条会话，和终端一样） */
  open: (body: { hostId: number; accountId?: number | null }) =>
    unwrap(
      request<ApiData<FileCapabilities>>('/api/files/sessions', { method: 'POST', data: body }),
    ),
  sessions: () =>
    unwrap(request<ApiData<FileSessionItem[]>>('/api/files/sessions', { method: 'GET' })),
  session: (sid: string) =>
    unwrap(request<ApiData<FileCapabilities>>(`/api/files/sessions/${sid}`, { method: 'GET' })),
  close: (sid: string) =>
    unwrap(
      request<ApiData<{ sid: string }>>(`/api/files/sessions/${sid}`, { method: 'DELETE' }),
    ),
  /** 访问控制试算：某个操作能不能作用在这个路径上（前端不复刻策略逻辑） */
  check: (sid: string, body: { operation: string; path?: string; targetPath?: string }) =>
    unwrap(
      request<ApiData<FileDecision>>(`/api/files/sessions/${sid}/check`, {
        method: 'POST',
        data: body,
      }),
    ),
  list: (sid: string, path?: string) =>
    unwrap(
      request<ApiData<FileListResult>>(`/api/files/sessions/${sid}/list`, {
        method: 'GET',
        params: { path },
      }),
    ),
  stat: (sid: string, path: string) =>
    unwrap(
      request<ApiData<FileStatResult>>(`/api/files/sessions/${sid}/stat`, {
        method: 'GET',
        params: { path },
      }),
    ),
  read: (sid: string, path: string) =>
    unwrap(
      request<ApiData<FileReadResult>>(`/api/files/sessions/${sid}/read`, {
        method: 'GET',
        params: { path },
      }),
    ),
  write: (sid: string, body: { path: string; content: string; expectedMtime?: number }) =>
    unwrap(
      request<ApiData<{ path: string; size: number; created: boolean }>>(
        `/api/files/sessions/${sid}/write`,
        { method: 'POST', data: body },
      ),
    ),
  mkdir: (sid: string, body: { path: string; mode?: string; parents?: boolean }) =>
    unwrap(
      request<ApiData<{ path: string; created: boolean }>>(
        `/api/files/sessions/${sid}/mkdir`,
        { method: 'POST', data: body },
      ),
    ),
  rename: (sid: string, body: { path: string; newName?: string; targetPath?: string }) =>
    unwrap(
      request<ApiData<{ operation: string; path: string; targetPath: string; moved: number }>>(
        `/api/files/sessions/${sid}/rename`,
        { method: 'POST', data: body },
      ),
    ),
  copy: (sid: string, body: { path: string; targetPath: string; overwrite?: boolean }) =>
    unwrap(
      request<ApiData<{ path: string; targetPath: string; copied: number }>>(
        `/api/files/sessions/${sid}/copy`,
        { method: 'POST', data: body },
      ),
    ),
  remove: (sid: string, body: { paths: string[]; recursive?: boolean }) =>
    unwrap(
      request<ApiData<{ deleted: string[]; failed: { path: string; message: string }[] }>>(
        `/api/files/sessions/${sid}/delete`,
        { method: 'POST', data: body },
      ),
    ),
  chmod: (sid: string, body: { path: string; mode: string }) =>
    unwrap(
      request<ApiData<{ path: string; before: string; after: string }>>(
        `/api/files/sessions/${sid}/chmod`,
        { method: 'POST', data: body },
      ),
    ),
  upload: (sid: string, directory: string, file: File, overwrite = true) => {
    const data = new FormData();
    data.append('file', file);
    data.append('path', directory);
    data.append('overwrite', overwrite ? 'true' : 'false');
    return unwrap(
      request<ApiData<{ uploaded: FileUploadResult[]; failed: { name: string; message: string }[] }>>(
        `/api/files/sessions/${sid}/upload`,
        { method: 'POST', data },
      ),
    );
  },
  download: (sid: string, path: string) =>
    fetchBinary(
      `/api/files/sessions/${sid}/download?path=${encodeURIComponent(path)}`,
      path.split('/').pop() || 'download',
      sid,
    ),
  archive: (sid: string, paths: string[]) =>
    fetchBinary(
      `/api/files/sessions/${sid}/archive`,
      paths.length > 1 ? 'files.zip' : `${paths[0]?.split('/').pop() || 'files'}.zip`,
      sid,
    ),
};

// ---------------------------------------------------------------- 文件策略

export type FilePolicyPayload = {
  name?: string;
  description?: string;
  defaultAction?: FilePolicyAction;
};

export type FileRulePayload = {
  policyId?: number;
  priority?: number;
  action?: FilePolicyAction;
  operation?: string;
  matchType?: FileMatchType;
  pathPattern?: string;
  riskLevel?: RiskLevel;
  description?: string;
  enabled?: boolean;
};

export const filePolicyApi = {
  list: (params: PageParams = {}) =>
    request<ApiList<FilePolicyItem>>('/api/file-policies', {
      method: 'GET',
      params: pageParams(params),
    }),
  options: () =>
    unwrap(request<ApiData<FilePolicyOption[]>>('/api/file-policies/options', { method: 'GET' })),
  operations: () =>
    unwrap(
      request<ApiData<{ value: string; label: string }[]>>('/api/file-policies/operations', {
        method: 'GET',
      }),
    ),
  get: (id: number) =>
    unwrap(request<ApiData<FilePolicyItem>>(`/api/file-policies/${id}`, { method: 'GET' })),
  create: (data: FilePolicyPayload) =>
    unwrap(request<ApiData<FilePolicyItem>>('/api/file-policies', { method: 'POST', data })),
  update: (id: number, data: FilePolicyPayload) =>
    unwrap(request<ApiData<FilePolicyItem>>(`/api/file-policies/${id}`, { method: 'PUT', data })),
  remove: (id: number) =>
    unwrap(request<ApiData<null>>(`/api/file-policies/${id}`, { method: 'DELETE' })),
  resetBuiltin: () =>
    unwrap(request<ApiData<Record<string, unknown>>>('/api/file-policies/reset-builtin', {
      method: 'POST',
    })),
  test: (id: number, body: { operation: string; path: string; targetPath?: string }) =>
    unwrap(
      request<ApiData<FileDecision>>(`/api/file-policies/${id}/test`, {
        method: 'POST',
        data: body,
      }),
    ),
  evaluate: (body: { operation: string; path: string; policyId?: number; targetPath?: string }) =>
    unwrap(
      request<ApiData<FileDecision>>('/api/file-policies/evaluate', { method: 'POST', data: body }),
    ),
};

export const fileRuleApi = {
  list: (policyId?: number) =>
    unwrap(
      request<ApiData<FileRuleItem[]>>('/api/file-rules', {
        method: 'GET',
        params: policyId ? { policyId } : undefined,
      }),
    ),
  create: (data: FileRulePayload) =>
    unwrap(request<ApiData<FileRuleItem>>('/api/file-rules', { method: 'POST', data })),
  update: (id: number, data: FileRulePayload) =>
    unwrap(request<ApiData<FileRuleItem>>(`/api/file-rules/${id}`, { method: 'PUT', data })),
  remove: (id: number) =>
    unwrap(request<ApiData<null>>(`/api/file-rules/${id}`, { method: 'DELETE' })),
};

export const fileAuditApi = {
  list: (params: PageParams = {}) =>
    request<ApiList<FileLogItem>>('/api/audits/files', {
      method: 'GET',
      params: pageParams(params),
    }),
  options: () =>
    unwrap(request<ApiData<FileLogOptions>>('/api/audits/files/options', { method: 'GET' })),
  purge: (payload: PurgePayload) =>
    unwrap(
      request<ApiData<PurgeResult>>('/api/audits/files/delete', { method: 'POST', data: payload }),
    ),
};
