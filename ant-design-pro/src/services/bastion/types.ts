/**
 * 堡垒机后端接口的数据结构（与 Flask 后端逐一对应）。
 *
 * 后端统一信封：
 *   成功单条： { success: true, message: string, data: T }
 *   成功列表： { success: true, message: 'ok', data: T[], total, page, pageSize, ...extra }
 *   失败：     { success: false, message: string, code: string, data: null } + HTTP 4xx/5xx
 * 分页请求参数：page（兼容 current）/ pageSize
 */

export type ApiResult<T> = {
  success: boolean;
  message: string;
  code?: string;
  data: T;
};

export type ApiData<T> = ApiResult<T>;

export type ApiList<T> = ApiResult<T[]> & {
  total: number;
  page: number;
  pageSize: number;
};

export type PageParams = {
  page?: number;
  pageSize?: number;
  keyword?: string;
  [key: string]: unknown;
};

/** 会话来源：网关审计 shell / 网页终端 */
export type SessionSource = 'gateway' | 'web';
/** 会话状态 */
export type SessionStatus = 'active' | 'closed' | 'failed' | 'terminated' | 'denied';
/** 命令动作 */
export type CommandAction = 'allow' | 'deny';
/** 风险等级 */
export type RiskLevel = 'low' | 'medium' | 'high' | 'critical';
/** 策略命中动作 */
export type PolicyAction = 'allow' | 'deny' | 'confirm';
/** 规则匹配方式 */
export type MatchType = 'regex' | 'prefix' | 'exact' | 'contains';

// ---------------------------------------------------------------- 身份域

export type UserItem = {
  id: number;
  username: string;
  displayName: string;
  email: string;
  phone: string;
  remark: string;
  roleId: number;
  roleCode: string;
  roleName: string;
  status: 'active' | 'disabled';
  isSuperuser: boolean;
  gatewayEnabled: boolean;
  webtermEnabled: boolean;
  mustChangePassword: boolean;
  lastLoginAt: string | null;
  lastLoginIp: string;
  lockedUntil: string | null;
  permissions: string[];
  createdAt: string;
  updatedAt: string;
};

export type RoleItem = {
  id: number;
  code: string;
  name: string;
  description: string;
  isBuiltin: boolean;
  permissions: string[];
  userCount: number;
  createdAt: string;
  updatedAt: string;
};

export type PermissionGroup = {
  group: string;
  items: { code: string; label: string }[];
};

// ---------------------------------------------------------------- 资产域

export type HostItem = {
  id: number;
  name: string;
  address: string;
  port: number;
  protocol: string;
  osType: string;
  description: string;
  groupId: number | null;
  groupName: string;
  status: 'active' | 'disabled';
  tags: string[];
  accountCount: number;
  grantCount: number;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
};

export type HostAccountItem = {
  id: number;
  hostId: number;
  hostName: string;
  name: string;
  username: string;
  authType: 'password' | 'key';
  description: string;
  sudoCommand: string;
  hasSecret: boolean;
  hasPrivateKey: boolean;
  hasPassphrase: boolean;
  /** 仅在拥有 host:manage 权限时返回明文 */
  secret?: string;
  privateKey?: string;
  passphrase?: string;
  grantCount: number;
  sessionCount: number;
  createdAt: string;
  updatedAt: string;
};

export type HostGroupItem = {
  id: number;
  name: string;
  description: string;
  hostCount: number;
  createdAt: string;
};

export type OptionItem<T = number> = {
  value: T;
  label: string;
  [key: string]: unknown;
};

// ---------------------------------------------------------------- 授权域

export type GrantItem = {
  id: number;
  userId: number;
  username: string;
  displayName: string;
  hostId: number;
  hostName: string;
  hostAddress: string;
  hostAccountId: number | null;
  accountName: string;
  accountUsername: string;
  policyId: number | null;
  policyName: string;
  /** 文件策略（文件管理器里能操作哪些路径） */
  filePolicyId: number | null;
  filePolicyName: string;
  canLogin: boolean;
  canSftp: boolean;
  canUpload: boolean;
  canDownload: boolean;
  /** 允许在文件管理器里做修改类操作（上传/编辑/新建/改名/移动/复制/删除/改权限） */
  canFileWrite: boolean;
  canPortForward: boolean;
  canWebterm: boolean;
  timeStart: string | null;
  timeEnd: string | null;
  weekdays: number[];
  expireAt: string | null;
  maxSessions: number;
  enabled: boolean;
  remark: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  /** 当前是否在授权时间窗内（后端实时计算） */
  inWindow: boolean;
  windowReason: string;
  activeSessions: number;
};

export type GrantMatrixCell = {
  hostId: number;
  hostName: string;
  granted: boolean;
  enabled: boolean;
  canWebterm: boolean;
  policyName: string;
  grantId: number | null;
};

export type GrantMatrixRow = {
  userId: number;
  username: string;
  displayName: string;
  roleCode: string;
  status: string;
  grantCount: number;
  cells: GrantMatrixCell[];
};

export type GrantMatrixResult = ApiList<GrantMatrixRow> & {
  hosts: { id: number; name: string }[];
};

export type PolicyItem = {
  id: number;
  name: string;
  description: string;
  defaultAction: PolicyAction;
  isDefault: boolean;
  ruleCount: number;
  grantCount: number;
  createdAt: string;
  updatedAt: string;
  rules?: PolicyRuleItem[];
};

export type PolicyRuleItem = {
  id: number;
  policyId: number;
  priority: number;
  matchType: MatchType;
  pattern: string;
  action: PolicyAction;
  riskLevel: RiskLevel;
  description: string;
  enabled: boolean;
  createdAt: string;
};

export type PolicyDecision = {
  policyId: number | null;
  policyName: string;
  allowed: boolean;
  action: PolicyAction;
  riskLevel: RiskLevel;
  reason: string;
  ruleId: number | null;
  rulePattern: string | null;
  segments: {
    segment: string;
    allowed: boolean;
    action: PolicyAction;
    riskLevel: RiskLevel;
    ruleId: number | null;
    rulePattern: string | null;
    reason: string;
  }[];
};

// ---------------------------------------------------------------- 审计域

export type SessionItem = {
  id: number;
  sid: string;
  userId: number;
  username: string;
  roleCode: string;
  hostId: number;
  hostName: string;
  hostAddress: string;
  accountId: number | null;
  accountUsername: string;
  grantId: number | null;
  source: SessionSource;
  protocol: string;
  clientIp: string;
  clientPort: number;
  status: SessionStatus;
  endReason: string;
  startedAt: string;
  endedAt: string | null;
  durationSeconds: number;
  commandCount: number;
  deniedCount: number;
  bytesIn: number;
  bytesOut: number;
  riskLevel: RiskLevel;
  hasTranscript?: boolean;
};

export type CommandItem = {
  id: number;
  sessionId: number;
  sid: string;
  seq: number;
  userId: number;
  username: string;
  hostId: number;
  hostName: string;
  command: string;
  action: CommandAction;
  riskLevel: RiskLevel;
  matchedRuleId: number | null;
  matchedRulePattern: string;
  reason: string;
  startedAt: string;
  endedAt: string | null;
  durationMs: number;
  exitStatus: number | null;
  truncated: boolean;
  output: string;
};

export type AuditItem = {
  id: number;
  ts: string;
  category: string;
  action: string;
  actorId: number | null;
  actorUsername: string;
  actorRole: string;
  targetType: string;
  targetId: string;
  targetName: string;
  result: string;
  message: string;
  detail: Record<string, unknown>;
  ip: string;
  userAgent: string;
};

export type DashboardOverview = {
  cards: {
    users: number;
    hosts: number;
    hostAccounts: number;
    grants: number;
    policies: number;
    onlineSessions: number;
    sessions24h: number;
    sessions7d: number;
    commands24h: number;
    denied24h: number;
    risky24h: number;
    audits24h: number;
    filePolicies: number;
    fileOps24h: number;
    fileDenied24h: number;
  };
  trend: { date: string; sessions: number; commands: number; denied: number }[];
  topHosts: { hostName: string; count: number }[];
  recentCommands: CommandItem[];
  recentAudits: AuditItem[];
  recentFiles: FileLogItem[];
};

export type DashboardMine = {
  targetCount: number;
  targets: TerminalTarget[];
  onlineSessions: SessionItem[];
  recentSessions: SessionItem[];
  recentCommands: CommandItem[];
  denied7d: number;
};

// ---------------------------------------------------------------- 系统域

export type SettingItem = {
  key: string;
  label: string;
  type: 'string' | 'int' | 'bool';
  value: string;
  default: string;
};

export type SettingsPayload = {
  items: SettingItem[];
  values: Record<string, string>;
};

export type GatewayStatus = {
  enabled: boolean;
  running: boolean;
  listenHost: string;
  listenPort: number;
  suggestedHost: string;
  connectCommand: string;
  allowPassword: boolean;
  allowPublicKey: boolean;
  hostKeyFingerprint: string;
  onlineSessions: number;
  gatewaySessions: number;
  banner: string;
  serverVersion: string;
  clientVersion: string;
  hostname: string;
};

// ---------------------------------------------------------------- 网页终端

export type TerminalTarget = {
  hostId: number;
  hostName: string;
  address: string;
  port: number;
  groupName: string;
  description: string;
  osType: string;
  canSftp: boolean;
  canUpload: boolean;
  canDownload: boolean;
  canFileWrite: boolean;
  canWebterm: boolean;
  policyName: string;
  filePolicyName: string;
  maxSessions: number;
  accounts: { id: number; name: string; username: string }[];
};

export type TargetCheckResult = {
  allowed: boolean;
  reason: string;
  hostId: number;
  hostName: string;
  address: string;
  port: number;
};

/**
 * `/api/currentUser` 返回的用户信息：Pro 模板自带的 API.CurrentUser
 * 之外，后端额外下发了权限码等字段（见 app/api/auth.py::pro_current_user）。
 */
export type CurrentUser = API.CurrentUser & {
  username?: string;
  roleCode?: string;
  permissions?: string[];
  isAdmin?: boolean;
  mustChangePassword?: boolean;
  gatewayEnabled?: boolean;
  webtermEnabled?: boolean;
};

// ---------------------------------------------------------------- 文件域（SFTP 文件管理器）

export type FilePolicyAction = 'allow' | 'deny';
export type FileMatchType = 'glob' | 'regex' | 'prefix' | 'contains';
export type FileLogResult = 'success' | 'failure' | 'denied';

export type FileEntry = {
  name: string;
  path: string;
  type: 'file' | 'dir' | 'link' | string;
  isDir: boolean;
  size: number;
  /** ISO8601 字符串（UTC，带 `Z`）；空串表示后端拿不到时间 */
  mtime: string;
  /** `-rw-r--r--` 这类符号模式 */
  mode: string;
  /** `0644` 这类八进制串 */
  modeOctal: string;
  uid: number;
  gid: number;
};

export type FileListResult = {
  path: string;
  parent: string;
  entries: FileEntry[];
  count: number;
  truncated: boolean;
};

export type FileStatResult = FileEntry;

export type FileReadResult = {
  path: string;
  content: string;
  size: number;
  readBytes: number;
  truncated: boolean;
  encoding: string;
  mtime: number;
  modeOctal: string;
};

/** `POST /api/files/sessions` 与 `GET /api/files/sessions/<sid>` 的 data 结构。 */
export type FileCapabilities = {
  sid: string;
  hostId: number;
  hostName: string;
  hostAddress: string;
  accountUsername: string;
  grantId: number | null;
  policyName: string;
  homeDir: string;
  canSftp: boolean;
  canUpload: boolean;
  canDownload: boolean;
  canFileWrite: boolean;
  /** 操作名清单（list/read/download/upload/write/mkdir/rename/move/copy/delete/archive/chmod） */
  operations: string[];
  limits: {
    maxTextBytes: number;
    maxListEntries: number;
    maxArchiveFiles: number;
    maxArchiveBytes: number;
  };
};

export type FileSessionItem = FileCapabilities & {
  clientIp: string;
  openedAt: string;
  lastActive: string;
  closed: boolean;
};

/** 单个「路径 + 操作」的访问控制试算结果（`/check` 与策略页试算器共用）。 */
export type FileDecision = {
  allowed: boolean;
  action: FilePolicyAction | string;
  riskLevel: RiskLevel;
  ruleId: number | null;
  rulePattern: string;
  reason: string;
  policyId: number | null;
  policyName: string;
  operation: string;
  path: string;
};

export type FileUploadResult = {
  path: string;
  size: number;
  created: boolean;
  name: string;
};

export type FileArchiveResult = {
  entries: number;
  bytes: number;
};

export type FileLogItem = {
  id: number;
  sessionId: number | null;
  sid: string;
  seq: number;
  userId: number | null;
  username: string;
  hostId: number | null;
  hostName: string;
  operation: string;
  path: string;
  targetPath: string;
  action: FilePolicyAction | string;
  riskLevel: RiskLevel;
  matchedRuleId: number | null;
  matchedRulePattern: string;
  reason: string;
  result: FileLogResult | string;
  message: string;
  size: number;
  fileCount: number;
  startedAt: string;
  endedAt: string | null;
  durationMs: number;
};

export type FileRuleItem = {
  id: number;
  policyId: number;
  priority: number;
  action: FilePolicyAction;
  /** `*` 或具体操作名 */
  operation: string;
  matchType: FileMatchType;
  pathPattern: string;
  riskLevel: RiskLevel;
  description: string;
  enabled: boolean;
  createdAt: string;
};

export type FilePolicyItem = {
  id: number;
  name: string;
  description: string;
  defaultAction: FilePolicyAction;
  isDefault: boolean;
  ruleCount: number;
  grantCount: number;
  operations: { value: string; label: string }[];
  rules?: FileRuleItem[];
  createdAt: string;
  updatedAt: string;
};

export type FilePolicyOption = {
  label: string;
  value: number;
  defaultAction: FilePolicyAction;
  isDefault: boolean;
};

export type FileLogOptions = {
  operations: { value: string; label: string }[];
  actions: { value: string; label: string }[];
  results: { value: string; label: string }[];
  riskLevels: { value: string; label: string }[];
  usernames: string[];
  hosts: string[];
};
