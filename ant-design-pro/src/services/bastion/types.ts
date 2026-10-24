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

/**
 * 一台主机上的一个协议端点。
 *
 * 同一台机器可以有多个入口（Linux 走 ssh、Windows 走 winrm、要画面走 rdp），
 * 端口与 WinRM 认证方式属于**端点**而不是主机，所以挂在端点上。
 */
export type HostProtocolItem = {
  id?: number;
  hostId?: number;
  protocol: string;
  port: number;
  /** 只对 winrm 端点有意义：ntlm（默认）/ basic（明文，需目标机 AllowUnencrypted） */
  winrmTransport?: 'ntlm' | 'basic' | string;
  status: 'active' | 'disabled';
};

export type HostItem = {
  id: number;
  name: string;
  address: string;
  port: number;
  protocol: string;
  /** 协议端点表：这台机器可用的入口（列表与表单都按它渲染） */
  protocols?: HostProtocolItem[];
  /** RDP 安全层偏好：auto（默认，按客户端协商，含 NLA）/ ssl（强制标准 RDP 安全层） */
  rdpSecurity?: 'auto' | 'ssl' | string;
  /** WinRM 认证方式：ntlm（默认）/ basic（明文，需目标机 AllowUnencrypted） */
  winrmTransport?: 'ntlm' | 'basic' | string;
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
  prevHash: string;
  entryHash: string;
};

/**
 * 链式哈希计数。
 * - `pendingPrefix`：链起步**之前**的空哈希行（升级前遗留，合法，不影响健康度）
 * - `pendingInside`：链**之内**的空哈希行（异常：有人把哈希清空冒充遗留来藏篡改）
 * - `pending`：`total - hashed`，保留给旧调用方；健康度只看 `pendingInside`
 */
export type AuditChainCounts = {
  total: number;
  hashed: number;
  pending: number;
  pendingPrefix?: number;
  pendingInside?: number;
};
export type AuditChainHead = {
  lastId: number | null;
  lastEntryHash: string;
  /** 链尾**实际**校验通过的 entry_hash（比 lastEntryHash 更可靠：后者只是最后一行存储值） */
  verifiedHead?: string;
};
export type AuditChainStatus = {
  healthy: boolean;
  counts: Record<'audit_logs' | 'command_logs' | 'file_logs', AuditChainCounts>;
  heads: Record<'audit_logs' | 'command_logs' | 'file_logs', AuditChainHead>;
};
export type AuditChainError = {
  id: number | null;
  reason: string;
  expected?: string;
  actual?: string;
};
export type AuditChainTableResult = {
  ok: boolean;
  total: number;
  verified: number;
  firstBad: number | null;
  errors: AuditChainError[];
  /** 链起步前的遗留空哈希行数 */
  prefix?: number;
  /** 链内的空哈希行数（异常信号） */
  pendingInside?: number;
  /** 链尾校验通过的 entry_hash（可抄作库外锚点） */
  head?: string;
  /** 本次是否比对了库外锚点 */
  anchorChecked?: boolean;
};
export type AuditChainVerifyResult = {
  ok: boolean;
  tables: Record<'audit_logs' | 'command_logs' | 'file_logs', AuditChainTableResult>;
  heads?: Record<'audit_logs' | 'command_logs' | 'file_logs', AuditChainHead>;
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
  /** 整型配置的合法区间（后端下发，用于输入框 min/max；后端仍会独立校验） */
  min?: number;
  max?: number;
  /** 模块设置项：归属模块 id；全局参数不下发该字段 */
  moduleId?: string;
  /** 模块设置项：`module`；全局参数不下发该字段 */
  scope?: 'module' | string;
  /** 模块设置项的分组显示名（模块中文名优先，缺省英文名/id；后端暂不下发双语） */
  group?: string;
  /** 模块 description.json 里的原始配置键（下发 key 已带 `mod.<id>.` 前缀） */
  rawKey?: string;
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
  /**
   * 连接协议：`ssh` = Linux 字符终端，`winrm` = Windows 字符终端（WinRM/PowerShell），
   * `rdp` = Windows 远程桌面（`/rdp/console`）。**已合并进同一张「可访问资产」列表**：
   * 同一个入口、同一颗「连接」按钮，按协议决定弹哪种窗口（`rdp` 弹远程桌面窗口，
   * `ssh`/`winrm` 都弹终端窗口），所以页面上不再有「远程桌面」专栏。
   */
  protocol: string;
  /**
   * 一台主机多协议：这**一个端点**的协议与端口（`/api/terminal/targets` 现在
   * 一个端点返回一条，前端按 `hostId` 合并成一行、行内每个协议一个入口按钮）。
   */
  protocols?: string[];
  endpoints?: HostProtocolItem[];
  canSftp: boolean;
  canUpload: boolean;
  canDownload: boolean;
  canFileWrite: boolean;
  canWebterm: boolean;
  policyName: string;
  filePolicyName: string;
  maxSessions: number;
  accounts: { id: number; name: string; username: string; authType?: string }[];
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

// ---------------------------------------------------------------- WebRDP（Windows 远程桌面）

/**
 * `/api/rdp/targets` 的一条记录。
 *
 * 与网页终端的目标同形状（后端同一个 `serialize_target()`），差别只在协议：
 * 只有 `protocol === 'rdp'` 的主机会出现在这里。
 */
export type RdpTarget = TerminalTarget;

/**
 * `POST /api/rdp/sessions` 的返回：一次性票据 + WebSocket 地址 + 该账号口令。
 *
 * 口令为什么在前端：CredSSP/NLA 必须在 RDP 客户端（浏览器里的 ironrdp-wasm）侧计算，
 * 服务端无法代算。这条通道后端会单独写一条 `rdp_credential_reveal` 审计。
 */
export type RdpSessionInfo = {
  ticket: string;
  expiresIn: number;
  wsPath: string;
  protocol: string;
  host: RdpTarget;
  account: { id: number; name: string; username: string };
  credential: { username: string; password: string; domain: string };
};

/**
 * 一条远程桌面录像（`RdpRecording`）：谁、在哪台 Windows 机器上、录了多久、多大。
 *
 * 录像由**浏览器侧**录制（`canvas.captureStream()` + `MediaRecorder`），会话结束时
 * 上传到堡垒机落盘；`url` 是回放地址（后端支持 Range，所以 `<video>` 能拖动进度条）。
 */
export type RdpRecording = {
  id: number;
  sessionId?: number | null;
  hostId: number;
  hostName: string;
  hostAddress: string;
  /** 堡垒机登录人（谁在操作） */
  username: string;
  /** 被登录的资产账号（登的是哪台机器上的谁） */
  accountUsername: string;
  sizeBytes: number;
  durationSeconds: number;
  mimeType: string;
  width?: number | null;
  height?: number | null;
  startedAt?: string | null;
  createdAt: string;
  url: string;
  /**
   * 这条录像是**服务端自动收口**的：浏览器窗口被强行关掉（直接关标签页 / 崩溃 / 断网）时，
   * 边录边传已经攒下的部分由后端转正 —— 只录到窗口关闭那一刻，界面要标「未正常结束」，
   * 免得审计把「只录到一半」当成「正常结束」。
   */
  recovered?: boolean;
  /** 边录边传的任务号（整包上传的老录像为 null） */
  uploadId?: string | null;
};

/**
 * 一个「边录边传」上传任务的当前状态（`POST /api/rdp/recordings/chunk` 的返回）。
 *
 * 前端只关心进度与 `lastSeq`：`sizeBytes`/`chunks` 用来做「已上传 xx MB」的提示，
 * 真正的完整性由后端的序号校验（乱序 409）保证。
 */
export type RdpRecordingUploadState = {
  uploadId: string;
  hostId: number;
  hostName: string;
  hostAddress: string;
  username: string;
  accountUsername: string;
  mimeType: string;
  width?: number | null;
  height?: number | null;
  /** 服务端已收到的片数 */
  chunks: number;
  /** 服务端已收到的最后一片序号（-1 = 还没收到） */
  lastSeq: number;
  sizeBytes: number;
  startedAt?: string | null;
  lastSeenAt?: string | null;
  createdAt?: string | null;
  /** 该片是重传（服务端幂等收下，不重复追加） */
  duplicate?: boolean;
  /** 这次请求带上去的序号（回显，方便前端对账） */
  seq?: number;
};

// ---------------------------------------------------------------- 扩展模块域

/** 模块里可双语显示的文本。归一化后通常是 `{zh,en}`，也兼容后端直接给字符串。 */
export type ModuleText = string | { zh?: string; en?: string };

export type ModuleImage = {
  /** 包内相对路径，如 `images/demo-1.png` */
  path: string;
  /** 可访问 URL；`upload-check` 阶段不落盘，没有该字段 */
  url?: string;
};

export type ModuleNavType = 'iframe' | 'js';

export type ModuleNavItem = {
  /** 归属模块 id；`detail().nav[]` 与 `/api/modules/nav` 都会带 */
  moduleId?: string;
  /** 模块双语名；用于菜单/标题兜底 */
  moduleName?: ModuleText;
  key: string;
  title: ModuleText;
  /** 最终 URL，形如 `/m/<id>/<rel>`（后端已带模块前缀） */
  path: string;
  /** 包内相对入口，缺省回退模块 `entry.web` */
  entry?: string;
  type: ModuleNavType;
  icon?: string;
  permission?: string;
  loaded?: boolean;
};

export type ModuleSettingType = 'bool' | 'int' | 'string';

export type ModuleSetting = {
  /** 已加 `mod.<id>.` 前缀的 SystemSetting key */
  key: string;
  /** description.json 里的原始配置键 */
  rawKey?: string;
  label: ModuleText;
  type: ModuleSettingType;
  default?: string;
  min?: number | null;
  max?: number | null;
  description?: ModuleText;
  group?: string;
};

export type ModuleItem = {
  id: string;
  name: ModuleText;
  version: string;
  author: string;
  /** author === 'AutoOps'（忽略大小写/首尾空格） */
  official: boolean;
  description: ModuleText;
  tags: string[];
  experimental: boolean;
  icon?: string;
  license?: string;
  homepage?: string;
  minBastionVersion?: string;
  images: ModuleImage[];
  /** 包内入口（后端归一化时增补） */
  entry?: { web?: string };
  nav: ModuleNavItem[];
  settings: ModuleSetting[];
  permissions: string[];
  requires: string[];
  /** 保留 id（core/system/admin/modules），不可安装/卸载 */
  reserved?: boolean;
  enabled: boolean;
  loaded: boolean;
  installedAt?: string;
  updatedAt?: string;
  /** 包体积（字节） */
  size: number;
  errors: string[];
};

/** `POST /api/modules/upload-check` 的预览：归一化 description + 可安装性 */
/** upload-check 里可翻译的提示项：`key` 是中文模板（如 `模块 {id} 已安装`） */
export type ModulePreviewIssue = {
  key: string;
  params?: Record<string, string>;
};

export type ModulePreview = ModuleItem & {
  /** 后端预检结论；false 时安装按钮应禁用（如已存在/保留 id） */
  installable?: boolean;
  /** 非致命的预检问题码（MODULE_EXISTS、MODULE_RESERVED_ID 等） */
  issues?: string[];
  /** 附加提醒（例如实验性模块需要逐字确认），不影响 installable */
  warnings?: string[];
  /** `issues` 的可翻译版本（硬阻塞项），英文界面优先用它 */
  issueItems?: ModulePreviewIssue[];
  /** `warnings` 的可翻译版本 */
  warningItems?: ModulePreviewIssue[];
  /** 包内 README 全文；没有则空串 */
  readme?: string;
  /** 找到的 README 文件名，如 `README.md` */
  readmeName?: string;
  /** 包内英文 README 全文（`README.en.md`）；没有则空串 */
  readmeEn?: string;
  /** 找到的英文 README 文件名，如 `README.en.md` */
  readmeEnName?: string;
  /** 包内文件数 */
  fileCount?: number;
  /** 包内一级目录，如 `web/`、`modules/` */
  folders?: string[];
  /**
   * 服务端预览 token。后端若在 upload-check 时下发，则安装/图片资源优先用它；
   * 缺省时使用预览窗口 URL hash 里的客户端 token。
   */
  previewToken?: string;
};

/** `GET /api/modules/nav` 的扁平导航项（按当前账号权限过滤） */
export type ModuleNavEntry = ModuleNavItem & {
  moduleId: string;
  moduleName: ModuleText;
};
