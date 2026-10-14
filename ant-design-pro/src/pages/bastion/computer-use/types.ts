/**
 * ComputerUse 部署向导的组件级类型与中文文案表。
 *
 * 接口数据结构统一来自 `@/services/bastion/computeruse`，这里只放**界面自己**需要
 * 的东西：四个向导步骤的标题/说明、部署选项草稿、日志台状态、部署状态与日志级别的
 * 展示映射（颜色、文案）。放在独立文件里是为了让被拆出去的步骤组件共用同一份定义，
 * 避免各步骤各写一套中文说明。
 */
import type {
  DeploymentStatus,
  LogLevel,
  PythonVersionItem,
} from '@/services/bastion/computeruse';

/** 向导的四个步骤 */
export type WizardStepKey = 'installer' | 'host' | 'deploy' | 'success';

export type WizardStepMeta = {
  key: WizardStepKey;
  title: string;
  description: string;
};

/** antd Steps 的四项（顺序固定） */
export const WIZARD_STEPS: WizardStepMeta[] = [
  {
    key: 'installer',
    title: '选择 Python 安装包',
    description: '从堡垒机镜像里挑一个 ≥3.13 的版本并缓存到堡垒机',
  },
  {
    key: 'host',
    title: '选择目标主机',
    description: '挑一台启用了 WinRM 的 Windows 主机与资产账号',
  },
  {
    key: 'deploy',
    title: '执行部署',
    description: '看阶段进度与实时日志，失败可重新部署',
  },
  {
    key: 'success',
    title: '完成',
    description: '确认 MCP 地址与工具清单，去 WebRDP 控制台验证',
  },
];

/** 第 3 步之后需要展示的结果摘要（成功页用） */
export type DeployResult = {
  hostId: number;
  hostName: string;
  hostAddress: string;
  mcpUrl: string;
  mcpVersion: string;
  mcpToolCount: number;
  toolNames: string[];
  pythonPath: string;
  pythonVersion: string;
  vbsPath: string;
  startupPath: string;
  portproxy: boolean;
  /** 目标机入站防火墙有没有真的放行 MCP 端口（部署时读回核对的结果） */
  firewall: boolean;
  mcpPort: number;
};

/** 部署选项表单（第 2 步） */
export type DeployOptionDraft = {
  /** MCP 端口，默认 8000 */
  mcpPort: number;
  /** pip 镜像源；清空表示不指定（走后端/pip 自己的默认） */
  pipIndex: string;
  /** 是否给 MCP 生成访问密钥（推荐） */
  useAuthKey: boolean;
  /** 是否让 MCP 监听所有网卡（不推荐，会跳过 portproxy） */
  bindAll: boolean;
};

/** 部署选项的默认值 */
export const DEFAULT_DEPLOY_OPTIONS: DeployOptionDraft = {
  mcpPort: 8000,
  pipIndex: 'https://pypi.tuna.tsinghua.edu.cn/simple',
  useAuthKey: true,
  bindAll: false,
};

/** 日志台状态：日志按 seq 递增排列，`paused` 时不再自动滚动到底 */
export type DeployLoggerState = {
  lines: string[];
  /** 已收到的最后一个事件序号（重连时作为 `since`） */
  lastSeq: number;
  /** 用户手动把日志台滚上去了，暂停自动跟随 */
  paused: boolean;
  /** SSE 连接是否活着 */
  connected: boolean;
};

/** 部署状态的展示映射（标签文案 + 颜色 + 结果页用不用它） */
export const DEPLOY_STATUS_META: Record<
  DeploymentStatus,
  { text: string; color: string }
> = {
  pending: { text: '排队中', color: 'default' },
  running: { text: '部署中', color: 'processing' },
  success: { text: '部署成功', color: 'success' },
  failed: { text: '部署失败', color: 'error' },
  cancelled: { text: '已取消', color: 'warning' },
};

/** 日志级别 → 颜色与中文名（终端配色：浅灰/绿/橙/红） */
export const LOG_LEVEL_META: Record<LogLevel, { color: string; text: string }> =
  {
    info: { color: '#c9d1d9', text: '信息' },
    success: { color: '#52c41a', text: '成功' },
    warning: { color: '#faad14', text: '警告' },
    error: { color: '#ff4d4f', text: '错误' },
  };

/** 终端日志台的外观（深色底 + 等宽字体） */
export const LOGGER_STYLE = {
  background: '#0b1021',
  color: '#c9d1d9',
  fontFamily:
    "'JetBrains Mono', 'Cascadia Mono', Consolas, 'Courier New', monospace",
  fontSize: 12,
  lineHeight: 1.7,
  borderRadius: 6,
  padding: 12,
  height: 360,
  overflow: 'auto' as const,
  whiteSpace: 'pre-wrap' as const,
  wordBreak: 'break-all' as const,
};

/** 表格里展示选中的安装包用的一行摘要 */
export type SelectedInstaller = {
  version: string;
  filename: string;
  size: number;
  sha256: string;
};

/** 从版本清单里取出展示用的选中摘要 */
export function toSelectedInstaller(
  item: PythonVersionItem,
): SelectedInstaller {
  return {
    version: item.version,
    filename: item.filename,
    size: item.size,
    sha256: item.sha256,
  };
}
