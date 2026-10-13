/**
 * WebRDP 右侧的「ComputerUse AI 操作台」侧边栏。
 *
 * 目标机上部署了 Windows-MCP（streamable-http）之后，AI 才能通过它去操作那台机器的桌面：
 * 本面板负责展示部署状态、看一眼屏幕（Screenshot）、工具清单，以及与 AI 的流式对话
 * （工具调用与管理员确认弹窗，复用 `@/services/bastion/ai` 的同一套流与接口）。
 *
 * 两个必须记住的前提：
 * 1. **只有部署成功（`ready === true`）才渲染**：默认接口不可用时整块侧边栏不出现，
 *    绝不占着 RDP 画面又给一堆报错。
 * 2. **UI 自动化与截图是按登录会话隔离的**：开机自启的 MCP 跑在目标机的控制台会话，
 *    而当前 WebRDP 可能是另一个 RDP 会话 —— 操作员在自己的窗口里未必看得见 AI 的动作，
 *    所以截图必须内联渲染出来当证据。
 *
 * 本文件自包含：不引用 `src/pages/bastion/computer-use/**` 与
 * `src/services/bastion/computeruse.ts`，只依赖通用服务层（`ai.ts` / `client.ts`）。
 */
import {
  CameraOutlined,
  CloseOutlined,
  LeftOutlined,
  ReloadOutlined,
  RightOutlined,
  RobotOutlined,
} from '@ant-design/icons';
import { XMarkdown } from '@ant-design/x-markdown';
import { request } from '@umijs/max';
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type { CSSProperties, ReactNode } from 'react';
import { Component, useCallback, useEffect, useRef, useState } from 'react';
import { JsonCards } from '@/components/Bastion';
import type { AiCard } from '@/services/bastion/ai';
import { confirmAiOperation, streamAiChat } from '@/services/bastion/ai';
import { unwrap } from '@/services/bastion/client';
import type { ApiData } from '@/services/bastion/types';

const { Text } = Typography;

/* ------------------------------------------------------------------ *
 * 常量与类型（与后端契约一一对应，见任务说明里的接口清单）
 * ------------------------------------------------------------------ */

/** 展开/收起状态在 localStorage 里的键名 */
export const PANEL_OPEN_KEY = 'bastion_computeruse_panel_open';

/** 截图小画册最多保留几张 */
export const MAX_SCREENSHOTS = 3;

/** Screenshot 请求的超时：目标机 MCP 挂住时不能让按钮一直转圈 */
export const MCP_CALL_TIMEOUT_MS = 60000;

/** 部署状态轮询间隔：窗口开着的时候正好把机器部署完，也要能自己亮起来 */
const STATUS_POLL_MS = 30000;

/** 工具调用状态 → 标签（与 AI 对话页保持同一套说法） */
export const COMPUTER_USE_STATUS: Record<
  string,
  { color: string; text: string }
> = {
  running: { color: 'processing', text: '执行中' },
  pending: { color: 'warning', text: '待管理员确认' },
  approved: { color: 'processing', text: '已确认，执行中' },
  success: { color: 'success', text: '成功' },
  failure: { color: 'error', text: '失败' },
  denied: { color: 'error', text: '无权限' },
  rejected: { color: 'default', text: '已拒绝' },
};

export interface ComputerUseTool {
  name: string;
  description?: string;
  mutating?: boolean;
  observeOnly?: boolean;
}

export interface ComputerUseStatus {
  hostId?: number | string;
  hostName?: string;
  hostAddress?: string;
  ready?: boolean;
  deploymentId?: number | string;
  mcpUrl?: string;
  mcpHost?: string;
  mcpPort?: number | string;
  mcpVersion?: string;
  pythonPath?: string;
  toolCount?: number;
  tools?: ComputerUseTool[];
  hasAuthKey?: boolean;
  portproxy?: unknown;
  startupPath?: string;
}

/** 一次 Screenshot 的结果（内联渲染的证据） */
export interface ScreenCapture {
  key: string;
  /** `data:image/png;base64,…`，直接塞进 `<img src>` */
  images: string[];
  /** `data.text`：这次看屏幕的说明文字 */
  note?: string;
}

export interface ComputerUseToolCall {
  callId: string;
  name: string;
  args?: Record<string, unknown>;
  description?: string;
  status: string;
  summary?: string;
  preview?: string;
  durationMs?: number;
  confirmedBy?: string;
  card?: AiCard | null;
}

export interface ComputerUseTurn {
  key: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning: string;
  cards: AiCard[];
  toolCalls: ComputerUseToolCall[];
  streaming?: boolean;
  error?: string;
  /** message_end 带回来的收口状态（如 max_rounds：已达单轮工具调用上限） */
  status?: string;
  elapsedMs?: number;
}

/** 一条流事件的归类结果：纯函数产出，组件只按 kind 改状态 */
export type ChatEventKind =
  | 'start'
  | 'reasoning'
  | 'content'
  | 'tool_call'
  | 'tool_result'
  | 'cards'
  | 'message_end'
  | 'error'
  | 'confirm_required'
  | 'unknown';

export interface ChatEvent {
  kind: ChatEventKind;
  conversationId?: number;
  delta?: string;
  toolCall?: ComputerUseToolCall;
  callId?: string;
  pendingCalls?: ComputerUseToolCall[];
  reason?: string;
  cards?: AiCard[];
  status?: string;
  elapsedMs?: number;
  error?: string;
}

/* ------------------------------------------------------------------ *
 * 纯逻辑（导出给单元测试；组件的行为都从这里推导）
 * ------------------------------------------------------------------ */

/** 把未知值收成非空字符串（SSE 事件里的字段都是 unknown） */
export const asText = (value: unknown): string | undefined =>
  typeof value === 'string' && value.trim() ? value : undefined;

/** 把未知值收成有限数字 */
export const asNumber = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined;

/** 只把真正的对象（不是数组、不是 null）当成对象 */
export const asRecord = (
  value: unknown,
): Record<string, unknown> | undefined =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;

/** 长文本截断（工具参数预览不要撑破 400px 的面板） */
export const truncateText = (text: string, max = 240): string =>
  text.length <= max ? text : `${text.slice(0, max)}…`;

let toolCallSeq = 0;

/** 生成工具调用的稳定 key（后端没给 callId 时的兜底） */
export const nextToolCallId = (prefix = 'call'): string => {
  toolCallSeq += 1;
  return `${prefix}-${toolCallSeq}`;
};

/** 就绪判定：**只有** ready === true 才让侧边栏出现 */
export const isComputerUseReady = (
  status?: ComputerUseStatus | null,
): boolean => status?.ready === true;

/** AI 的写操作（会改变机器状态）必须管理员当场确认 */
export const isToolMutating = (tool: ComputerUseTool): boolean =>
  tool.mutating === true;

/**
 * 只读工具（AI 可以直接调，不用管理员确认）。
 *
 * 后端只对「会改机器状态」的工具标 `mutating: true`，其余不标 —— 所以判定是
 * 「没有明确标会改状态，就算是只读」；但 `mutating: true` 是**压倒一切**的：
 * 真出现「既标了 observeOnly 又标了 mutating」的矛盾数据，一律按写操作处理，
 * 宁可多要一次管理员确认，也不能让改机器的动作漏掉确认。
 */
export const isToolReadOnly = (tool: ComputerUseTool): boolean =>
  tool.mutating !== true;

/** 工具清单展示信息：把四个字段翻成「名字 + 标签」 */
export const describeToolMeta = (tool: ComputerUseTool) => ({
  name: tool.name,
  description: tool.description ?? '',
  readOnly: isToolReadOnly(tool),
  label: isToolReadOnly(tool) ? '只读' : '会改机器状态',
  color: isToolReadOnly(tool) ? 'green' : 'orange',
});

/** 折叠状态读盘：只在明确写了 `'1'` 时展开，其余一律按折叠处理 */
export const readPanelOpen = (
  storage: Pick<Storage, 'getItem'> | undefined = globalThis.localStorage,
): boolean => {
  try {
    return storage?.getItem(PANEL_OPEN_KEY) === '1';
  } catch {
    // 隐私模式下 localStorage 可能直接抛错：按折叠处理
    return false;
  }
};

/** 折叠状态落盘（写失败不影响使用） */
export const writePanelOpen = (
  open: boolean,
  storage: Pick<Storage, 'setItem'> | undefined = globalThis.localStorage,
): void => {
  try {
    storage?.setItem(PANEL_OPEN_KEY, open ? '1' : '0');
  } catch {
    // 忽略
  }
};

/** MCP 地址展示：先看接口给的，再自己用 host/port 拼 */
export const resolveMcpEndpoint = (
  status?: ComputerUseStatus | null,
): string => {
  if (!status) return '';
  const direct = asText(status.mcpUrl);
  if (direct) return direct;
  const host = asText(status.mcpHost);
  const port = asNumber(status.mcpPort);
  return host && port ? `http://${host}:${port}` : '';
};

/** 工具数量展示：数出来的比接口写着的更诚实 */
export const resolveToolCount = (
  status?: ComputerUseStatus | null,
  tools?: ComputerUseTool[],
): number =>
  tools && tools.length > 0 ? tools.length : (status?.toolCount ?? 0);

/** 目标主机展示名 */
export const resolveHostLabel = (
  hostId: number,
  status?: ComputerUseStatus | null,
): string => {
  const name = asText(status?.hostName);
  if (name) return name;
  const address = asText(status?.hostAddress);
  return address || `主机 #${hostId}`;
};

/** POST /api/computer-use/mcp/call（看一眼屏幕） */
export const buildScreenshotRequest = (hostId: number) => ({
  url: '/api/computer-use/mcp/call',
  body: { hostId, tool: 'Screenshot', arguments: {} },
});

/** GET /api/computer-use/mcp/tools?live=1 */
export const buildToolsRequest = (hostId: number) => ({
  url: '/api/computer-use/mcp/tools',
  params: { hostId, live: 1 },
});

/** POST /api/computer-use/mcp/probe */
export const buildProbeRequest = (hostId: number) => ({
  url: '/api/computer-use/mcp/probe',
  body: { hostId },
});

/** 解包后从各种信封里捞出 data（后端有的接口直接返回 data，有的再包一层） */
const unwrapData = (payload: unknown): Record<string, unknown> => {
  const outer = asRecord(payload);
  if (!outer) return {};
  return asRecord(outer.data) ?? outer;
};

/** 取接口里的 images 数组 → dataUrl 列表 */
export const parseScreenshotImages = (payload: unknown): string[] => {
  const data = unwrapData(payload);
  const raw = data.images;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item) => asText(asRecord(item)?.dataUrl))
    .filter((url): url is string => Boolean(url));
};

/** 取接口里的说明文字 */
export const parseScreenshotNote = (payload: unknown): string | undefined => {
  const data = unwrapData(payload);
  return asText(data.text) ?? asText(data.summary);
};

/** 解析一次 Screenshot 的返回：没有图片也没有说明时，回一句可读的原因 */
export const parseScreenshotResult = (
  payload: unknown,
): { images: string[]; note?: string; error?: string } => {
  const images = parseScreenshotImages(payload);
  const note = parseScreenshotNote(payload);
  if (images.length === 0 && !note) {
    return { images: [], error: describeApiError(payload, '没拿到截图') };
  }
  return { images, note };
};

/** 解析工具清单：`data.items` 优先，其次 `data.tools` */
export const parseToolList = (payload: unknown): ComputerUseTool[] => {
  const data = unwrapData(payload);
  const raw = Array.isArray(data.items)
    ? data.items
    : Array.isArray(data.tools)
      ? data.tools
      : [];
  return raw.map((item, index) => {
    const record = asRecord(item) ?? {};
    return {
      name: asText(record.name) ?? `工具 ${index + 1}`,
      description: asText(record.description),
      mutating: record.mutating === true,
      observeOnly: record.observeOnly === true,
    };
  });
};

/** 解析探活结果 */
export const parseProbeResult = (
  payload: unknown,
): { ok: boolean; text: string; toolCount: number } => {
  const data = unwrapData(payload);
  const ok = data.ok === true;
  const toolCount = Array.isArray(data.tools)
    ? data.tools.length
    : asNumber(data.toolCount) || 0;
  if (ok) {
    const latency = asNumber(data.latencyMs);
    return {
      ok,
      toolCount,
      text: `MCP 探活成功${
        latency === undefined ? '' : `（${latency}ms）`
      } · ${toolCount} 个工具`,
    };
  }
  return {
    ok,
    toolCount,
    text: asText(data.error) ?? '探活失败：目标机上的 MCP 没有响应',
  };
};

/** 从后端信封里取可以直接给用户看的错误文案 */
export const describeApiError = (error: unknown, fallback: string): string => {
  const payload = asRecord(asRecord(error)?.response);
  const data = asRecord(payload?.data);
  const text =
    asText(data?.message) ??
    asText(asRecord(payload?.data)?.error) ??
    asText(asRecord(error)?.message) ??
    (typeof error === 'string' ? asText(error) : undefined);
  return text ?? fallback;
};

/** 流事件归类：把后端的 SSE 事件翻成组件唯一认得的 ChatEvent */
export const classifyChatEvent = (event: unknown): ChatEvent => {
  const record = asRecord(event);
  if (!record) return { kind: 'unknown' };
  const type = asText(record.type) ?? '';
  const delta = asText(record.delta);
  switch (type) {
    case 'start':
      return { kind: 'start', conversationId: asNumber(record.conversationId) };
    case 'reasoning':
      return { kind: 'reasoning', delta };
    case 'content':
      return { kind: 'content', delta };
    case 'tool_call':
      return {
        kind: 'tool_call',
        toolCall: {
          callId: asText(record.callId) ?? nextToolCallId(),
          name: asText(record.name) ?? '未知工具',
          args: asRecord(record.args),
          description: asText(record.description),
          status: asText(record.status) ?? 'running',
        },
      };
    case 'tool_result':
      return {
        kind: 'tool_result',
        callId: asText(record.callId),
        toolCall: {
          callId: asText(record.callId) ?? nextToolCallId(),
          name: asText(record.name) ?? '',
          status: record.ok === false ? 'failure' : 'success',
          summary: asText(record.summary),
          preview: asText(record.preview),
          durationMs: asNumber(record.durationMs),
          confirmedBy: asText(record.confirmedBy),
          card: (record.card as AiCard | null) ?? null,
        },
      };
    case 'cards':
      return {
        kind: 'cards',
        cards: Array.isArray(record.cards) ? (record.cards as AiCard[]) : [],
      };
    case 'message_end':
      return {
        kind: 'message_end',
        status: asText(record.status),
        elapsedMs: asNumber(record.elapsedMs),
      };
    case 'error':
      return {
        kind: 'error',
        error: asText(record.message) ?? 'AI 执行失败',
      };
    case 'confirm_required': {
      const rawList = Array.isArray(record.toolCalls) ? record.toolCalls : [];
      const pendingCalls = rawList.map((item) => {
        const call = asRecord(item) ?? {};
        return {
          callId: asText(call.callId) ?? nextToolCallId(),
          name: asText(call.name) ?? '未知工具',
          args: asRecord(call.args),
          description: asText(call.description),
          status: 'pending',
        };
      });
      return {
        kind: 'confirm_required',
        pendingCalls,
        reason: asText(record.reason) ?? '该操作需要管理员确认',
      };
    }
    default:
      return { kind: 'unknown' };
  }
};

/** 把归类后的事件并进一条气泡（纯函数，便于测试与排障） */
export const applyChatEvent = (
  turn: ComputerUseTurn,
  event: ChatEvent,
): ComputerUseTurn => {
  switch (event.kind) {
    case 'reasoning':
      return event.delta
        ? { ...turn, reasoning: turn.reasoning + event.delta }
        : turn;
    case 'content':
      return event.delta
        ? { ...turn, content: turn.content + event.delta }
        : turn;
    case 'tool_call':
      return event.toolCall
        ? { ...turn, toolCalls: [...turn.toolCalls, event.toolCall] }
        : turn;
    case 'tool_result': {
      const patch = event.toolCall;
      if (!patch) return turn;
      return {
        ...turn,
        toolCalls: turn.toolCalls.map((item) =>
          item.callId === event.callId
            ? {
                ...item,
                status: patch.status,
                summary: patch.summary ?? item.summary,
                preview: patch.preview ?? item.preview,
                durationMs: patch.durationMs ?? item.durationMs,
                confirmedBy: patch.confirmedBy ?? item.confirmedBy,
                card: patch.card ?? item.card,
              }
            : item,
        ),
      };
    }
    case 'cards':
      return { ...turn, cards: event.cards ?? [] };
    case 'message_end':
      return {
        ...turn,
        status: event.status,
        elapsedMs: event.elapsedMs,
        streaming: false,
      };
    case 'error':
      return { ...turn, error: event.error ?? 'AI 执行失败' };
    case 'confirm_required':
      return {
        ...turn,
        toolCalls: turn.toolCalls.map((item) => {
          const found = (event.pendingCalls ?? []).find(
            (call) => call.callId === item.callId,
          );
          return found ? { ...item, status: 'pending' } : item;
        }),
      };
    default:
      return turn;
  }
};

/* ------------------------------------------------------------------ *
 * 面板样式：自包含地注入 —— 不改 rdp.css（那是别人维护的文件）
 * ------------------------------------------------------------------ */

const PANEL_CSS = `
.bastion-cu-strip {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
  width: 44px;
  padding: 10px 0;
  background: #0f172a;
  border-left: 1px solid #1f2937;
  color: #94a3b8;
}
.bastion-cu-strip-text {
  writing-mode: vertical-rl;
  letter-spacing: 2px;
  font-size: 12px;
  color: #cbd5e1;
}
.bastion-cu-panel {
  display: flex;
  flex-direction: column;
  flex: 0 0 auto;
  width: 400px;
  height: 100%;
  min-height: 0;
  background: #0f172a;
  border-left: 1px solid #1f2937;
  color: #cbd5e1;
  font-size: 12px;
}
.bastion-cu-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid #1f2937;
  flex: 0 0 auto;
}
.bastion-cu-title {
  font-size: 13px;
  font-weight: 600;
  color: #e6edf3;
}
.bastion-cu-meta {
  padding: 6px 10px;
  border-bottom: 1px solid #1f2937;
  color: #94a3b8;
  line-height: 1.7;
  flex: 0 0 auto;
}
.bastion-cu-meta code {
  color: #93c5fd;
  font-family: "SFMono-Regular", "JetBrains Mono", Consolas, monospace;
}
.bastion-cu-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bastion-cu-section-title {
  font-weight: 600;
  color: #e6edf3;
}
.bastion-cu-note {
  color: #64748b;
  line-height: 1.6;
}
.bastion-cu-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.bastion-cu-shots {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.bastion-cu-shot img {
  display: block;
  width: 100%;
  border: 1px solid #1f2937;
  border-radius: 4px;
  background: #000;
}
.bastion-cu-chat {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bastion-cu-msg-user {
  align-self: flex-end;
  max-width: 90%;
  padding: 6px 10px;
  border-radius: 8px;
  background: #1d4ed8;
  color: #eff6ff;
  white-space: pre-wrap;
  word-break: break-word;
}
.bastion-cu-msg-ai {
  align-self: stretch;
  word-break: break-word;
}
.bastion-cu-reasoning {
  margin: 4px 0 0;
  padding: 6px 8px;
  max-height: 180px;
  overflow: auto;
  background: #020617;
  border: 1px solid #1f2937;
  border-radius: 4px;
  color: #94a3b8;
  font-size: 11px;
  white-space: pre-wrap;
}
.bastion-cu-tool {
  margin-top: 6px;
  padding: 6px 8px;
  border: 1px solid #1f2937;
  border-radius: 4px;
  background: #0b1220;
}
.bastion-cu-tool-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.bastion-cu-tool-args {
  margin-top: 4px;
}
.bastion-cu-tool-summary,
.bastion-cu-tool-preview {
  margin-top: 4px;
  color: #94a3b8;
  white-space: pre-wrap;
  word-break: break-word;
}
.bastion-cu-msg-error {
  margin-top: 6px;
  color: #ffa39e;
  white-space: pre-wrap;
}
.bastion-cu-input {
  flex: 0 0 auto;
  padding: 8px 10px;
  border-top: 1px solid #1f2937;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.bastion-cu-input .ant-input,
.bastion-cu-input textarea {
  background: #020617 !important;
  border-color: #334155 !important;
  color: #e6edf3 !important;
}
.bastion-cu-foot {
  flex: 0 0 auto;
  padding: 6px 10px;
  border-top: 1px solid #1f2937;
  color: #64748b;
  line-height: 1.6;
}
`;

const PanelStyles = () => (
  // biome-ignore lint/security/noDangerouslySetInnerHtml: 面板样式自包含注入，内容是本文件里的静态字符串
  <style dangerouslySetInnerHTML={{ __html: PANEL_CSS }} />
);

/* ------------------------------------------------------------------ *
 * 小部件
 * ------------------------------------------------------------------ */

const metaLine: CSSProperties = { display: 'block' };

/** 一行「标签：值」 */
const MetaLine = ({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) => (
  <span style={metaLine}>
    {label}：{children}
  </span>
);

/** 单条工具调用卡片（状态 + 参数 + 结果） */
const ToolCallView = ({ call }: { call: ComputerUseToolCall }) => {
  const tag = COMPUTER_USE_STATUS[call.status] ?? {
    color: 'default',
    text: call.status,
  };
  return (
    <div className="bastion-cu-tool">
      <div className="bastion-cu-tool-head">
        <Tag color={tag.color}>{tag.text}</Tag>
        <Text strong style={{ color: '#e6edf3' }}>
          {call.name}
        </Text>
        {call.durationMs ? (
          <Text type="secondary">{call.durationMs}ms</Text>
        ) : null}
        {call.confirmedBy ? (
          <Text type="secondary">确认人 {call.confirmedBy}</Text>
        ) : null}
      </div>
      {call.description ? (
        <div className="bastion-cu-note">{call.description}</div>
      ) : null}
      {call.args && Object.keys(call.args).length > 0 ? (
        <div className="bastion-cu-tool-args">
          <JsonCards value={call.args} compact />
        </div>
      ) : null}
      {call.summary ? (
        <div className="bastion-cu-tool-summary">
          {truncateText(call.summary, 600)}
        </div>
      ) : null}
      {call.preview ? (
        <div className="bastion-cu-tool-preview">
          <JsonCards value={call.preview} compact />
        </div>
      ) : null}
    </div>
  );
};

/* ------------------------------------------------------------------ *
 * 组件
 * ------------------------------------------------------------------ */

export interface ComputerUsePanelProps {
  /** 目标主机 id：所有接口都要带它 */
  hostId: number;
  /** 收起状态变化回调（父级要据此重算画布尺寸时可挂） */
  onToggle?: (open: boolean) => void;
}

const ComputerUsePanel = ({ hostId, onToggle }: ComputerUsePanelProps) => {
  /** undefined = 还在查；null = 查失败或没就绪 */
  const [status, setStatus] = useState<ComputerUseStatus | null | undefined>(
    undefined,
  );
  const [open, setOpen] = useState(() => readPanelOpen());
  const [tools, setTools] = useState<ComputerUseTool[]>([]);
  const [probeText, setProbeText] = useState<string>();
  const [probeOk, setProbeOk] = useState<boolean>();
  const [refreshing, setRefreshing] = useState(false);
  const [shooting, setShooting] = useState(false);
  const [shots, setShots] = useState<ScreenCapture[]>([]);
  const [shotError, setShotError] = useState<string>();
  const [turns, setTurns] = useState<ComputerUseTurn[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmCalls, setConfirmCalls] = useState<ComputerUseToolCall[]>([]);
  const [confirmReason, setConfirmReason] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState<string>();

  const streamRef = useRef<AbortController | undefined>(undefined);
  const conversationIdRef = useRef<number | undefined>(undefined);
  /** 收起状态回调放 ref：父级每次渲染传新函数也不该触发副作用 */
  const onToggleRef = useRef(onToggle);
  onToggleRef.current = onToggle;
  const [confirmForm] = Form.useForm<{
    adminUsername?: string;
    adminPassword: string;
  }>();

  /** 拉部署状态；返回是否就绪 */
  const loadStatus = useCallback(async (): Promise<boolean> => {
    try {
      const data = await unwrap(
        request<ApiData<ComputerUseStatus>>('/api/computer-use/status', {
          method: 'GET',
          params: { hostId },
          // 没部署时后端会回 404：这里必须静默，绝不能让请求层弹 toast 打扰操作员
          skipErrorHandler: true,
        }),
      );
      const next: ComputerUseStatus =
        (data as ComputerUseStatus | undefined) ?? {};
      setStatus(next);
      if (Array.isArray(next.tools)) {
        setTools(next.tools);
      }
      return isComputerUseReady(next);
    } catch {
      // 没部署（或接口不可用）就是整块不出现，不做任何打扰
      setStatus(null);
      return false;
    }
  }, [hostId]);

  /** 刷新工具清单 */
  const loadTools = useCallback(async (): Promise<void> => {
    const { url, params } = buildToolsRequest(hostId);
    try {
      const body = await request<unknown>(url, {
        method: 'GET',
        params,
        // 工具清单拉不到也不打扰用户（面板里自己有一句提示）
        skipErrorHandler: true,
      });
      const list = parseToolList(body);
      if (list.length > 0) {
        setTools(list);
      }
    } catch {
      // 工具清单拿不到不影响「看一眼屏幕」与对话，静默即可
    }
  }, [hostId]);

  // 首屏 + 低频轮询部署状态（窗口开着时正好部署完，也要能自己亮起来）
  useEffect(() => {
    let alive = true;
    void loadStatus().then((ready) => {
      if (alive && ready) void loadTools();
    });
    const id = window.setInterval(() => {
      void loadStatus();
    }, STATUS_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [loadStatus, loadTools]);

  useEffect(() => () => streamRef.current?.abort(), []);

  /** 展开/收起并落盘 */
  const togglePanel = useCallback((next: boolean) => {
    setOpen(next);
    writePanelOpen(next);
    onToggleRef.current?.(next);
  }, []);

  /** 刷新：探活一次，顺带拉一份实时工具清单 */
  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const { url, body } = buildProbeRequest(hostId);
      const payload = await request<unknown>(url, {
        method: 'POST',
        data: body,
      });
      const result = parseProbeResult(payload);
      setProbeOk(result.ok);
      setProbeText(result.text);
      if (result.ok) {
        await loadTools();
        await loadStatus();
      }
    } catch (error) {
      setProbeOk(false);
      setProbeText(describeApiError(error, '探活失败'));
    } finally {
      setRefreshing(false);
    }
  }, [hostId, loadStatus, loadTools]);

  /** 看一眼屏幕：把 Screenshot 的图片内联出来当证据 */
  const takeScreenshot = useCallback(async () => {
    setShooting(true);
    setShotError(undefined);
    const { url, body } = buildScreenshotRequest(hostId);
    try {
      const payload = await request<unknown>(url, {
        method: 'POST',
        data: body,
        timeout: MCP_CALL_TIMEOUT_MS,
      });
      const parsed = parseScreenshotResult(payload);
      if (parsed.error) {
        setShotError(parsed.error);
        return;
      }
      const capture: ScreenCapture = {
        // 自增序号做 key：同一毫秒连拍两张也不会撞
        key: nextToolCallId('shot'),
        images: parsed.images,
        note: parsed.note ?? '已取回当前屏幕',
      };
      // 只留最近 MAX_SCREENSHOTS 张，避免长会话把面板撑爆
      setShots((prev) => [capture, ...prev].slice(0, MAX_SCREENSHOTS));
    } catch (error) {
      setShotError(describeApiError(error, '截图失败'));
    } finally {
      setShooting(false);
    }
  }, [hostId]);

  /** 按归类结果把事件写进气泡 */
  const patchTurn = useCallback((key: string, event: ChatEvent) => {
    setTurns((prev) =>
      prev.map((turn) =>
        turn.key === key ? applyChatEvent(turn, event) : turn,
      ),
    );
  }, []);

  const applyEvent = useCallback(
    (key: string, raw: unknown) => {
      const event = classifyChatEvent(raw);
      if (event.kind === 'start') {
        if (event.conversationId) {
          conversationIdRef.current = event.conversationId;
        }
        return;
      }
      if (event.kind === 'confirm_required') {
        setConfirmCalls(event.pendingCalls ?? []);
        setConfirmReason(event.reason ?? '该操作需要管理员确认');
        setConfirmError(undefined);
        setConfirmOpen(true);
      }
      patchTurn(key, event);
    },
    [patchTurn],
  );

  /** 开一条流：新对话 or 续流（resume） */
  const runStream = useCallback(
    async (
      key: string,
      payload: { message?: string; conversationId?: number; resume?: boolean },
    ) => {
      streamRef.current?.abort();
      const controller = new AbortController();
      streamRef.current = controller;
      setStreaming(true);
      try {
        // 流式对话复用 AI 服务层：与 AI 运维页同一套 SSE 解析
        await streamAiChat(
          {
            ...payload,
            // 每次都带上当前目标机 id，AI 才知道操作的是哪台机器
            hostId,
            source: 'rdp-console',
          },
          (event) => applyEvent(key, event),
          controller.signal,
        );
      } catch (error) {
        if ((error as Error)?.name !== 'AbortError') {
          patchTurn(key, {
            kind: 'error',
            error: error instanceof Error ? error.message : 'AI 请求失败',
          });
        }
      } finally {
        setStreaming(false);
        setTurns((prev) =>
          prev.map((turn) =>
            turn.key === key ? { ...turn, streaming: false } : turn,
          ),
        );
      }
    },
    [applyEvent, hostId, patchTurn],
  );

  /** 发送：回车发送、Shift+回车换行（在输入框上处理） */
  const handleSend = useCallback(() => {
    const value = input.trim();
    if (!value || streaming) return;
    setInput('');
    const userKey = nextToolCallId('user');
    const assistantKey = nextToolCallId('assistant');
    setTurns((prev) => [
      ...prev,
      {
        key: userKey,
        role: 'user',
        content: value,
        reasoning: '',
        cards: [],
        toolCalls: [],
      },
      {
        key: assistantKey,
        role: 'assistant',
        content: '',
        reasoning: '',
        cards: [],
        toolCalls: [],
        streaming: true,
      },
    ]);
    void runStream(assistantKey, {
      message: value,
      conversationId: conversationIdRef.current,
    });
  }, [input, runStream, streaming]);

  /** 管理员确认通过 → 续流执行挂起的写操作 */
  const submitConfirm = useCallback(async () => {
    setConfirmError(undefined);
    let values: { adminUsername?: string; adminPassword: string };
    try {
      values = await confirmForm.validateFields();
    } catch {
      // 表单校验失败：antd 已把提示挂在字段上，这里不能把 reject 冒成未处理异常
      return;
    }
    const conversationId = conversationIdRef.current;
    if (!conversationId) {
      setConfirmError('缺少对话 id：请重新发送一次请求');
      return;
    }
    setConfirming(true);
    try {
      await confirmAiOperation({
        conversationId,
        adminUsername: values.adminUsername,
        adminPassword: values.adminPassword,
        approve: true,
      });
      setConfirmOpen(false);
      confirmForm.resetFields();
      await runStream(`resume-${conversationId}`, {
        conversationId,
        resume: true,
      });
    } catch (error) {
      setConfirmError(describeApiError(error, '确认失败'));
    } finally {
      setConfirming(false);
    }
  }, [confirmForm, runStream]);

  /** 拒绝：也要续流，让模型知道被拒绝并给替代方案 */
  const rejectConfirm = useCallback(async () => {
    const conversationId = conversationIdRef.current;
    if (!conversationId) {
      setConfirmOpen(false);
      return;
    }
    setConfirming(true);
    try {
      await confirmAiOperation({
        conversationId,
        approve: false,
        reason: '操作员在 ComputerUse 面板里拒绝了该操作',
      });
      setConfirmOpen(false);
      confirmForm.resetFields();
      await runStream(`resume-${conversationId}`, {
        conversationId,
        resume: true,
      });
    } catch (error) {
      setConfirmError(describeApiError(error, '拒绝失败'));
    } finally {
      setConfirming(false);
    }
  }, [confirmForm, runStream]);

  /** status === undefined 时还在查：先不占地方 */
  if (status === undefined) {
    return null;
  }

  /** 核心约束：没就绪就整块不渲染 */
  if (!isComputerUseReady(status)) {
    return null;
  }

  const hostLabel = resolveHostLabel(hostId, status);
  const endpoint = resolveMcpEndpoint(status);
  const toolCount = resolveToolCount(status, tools);

  if (!open) {
    return (
      <>
        <PanelStyles />
        <aside className="bastion-cu-strip" data-testid="computeruse-strip">
          <Tooltip title="展开 ComputerUse AI 操作台" placement="left">
            <Button
              size="small"
              type="text"
              aria-label="展开 ComputerUse AI 操作台"
              icon={<RightOutlined />}
              onClick={() => togglePanel(true)}
            />
          </Tooltip>
          <Tooltip title="ComputerUse AI 操作台" placement="left">
            <RobotOutlined />
          </Tooltip>
          <span className="bastion-cu-strip-text">ComputerUse</span>
        </aside>
      </>
    );
  }

  return (
    <>
      <PanelStyles />
      <aside
        className="bastion-cu-panel"
        data-testid="computeruse-panel"
        aria-label="ComputerUse AI 操作台"
      >
        <div className="bastion-cu-head">
          <Space size={6} align="center">
            <RobotOutlined />
            <span className="bastion-cu-title">ComputerUse AI 操作台</span>
          </Space>
          <Space size={4}>
            <Tooltip title="探活并刷新工具清单">
              <Button
                size="small"
                type="text"
                aria-label="刷新 ComputerUse 状态"
                icon={<ReloadOutlined />}
                loading={refreshing}
                onClick={() => void refresh()}
              />
            </Tooltip>
            <Tooltip title="收起面板">
              <Button
                size="small"
                type="text"
                aria-label="收起 ComputerUse 面板"
                icon={<LeftOutlined />}
                onClick={() => {
                  togglePanel(false);
                  // 收起后把焦点还给桌面画布，别让键盘输入留在面板里
                  const canvas = document.querySelector<HTMLCanvasElement>(
                    '.bastion-rdp-canvas',
                  );
                  canvas?.focus();
                }}
              />
            </Tooltip>
          </Space>
        </div>

        <div className="bastion-cu-meta">
          <MetaLine label="目标主机">
            <Text strong style={{ color: '#e6edf3' }}>
              {hostLabel}
            </Text>
          </MetaLine>
          {endpoint ? (
            <MetaLine label="MCP 地址">
              <code data-testid="computeruse-mcp-url">{endpoint}</code>
            </MetaLine>
          ) : null}
          <MetaLine label="可用工具">{toolCount} 个</MetaLine>
          {probeText ? (
            <MetaLine label="探活">
              <Text type={probeOk ? 'success' : 'danger'}>{probeText}</Text>
            </MetaLine>
          ) : null}
        </div>

        <div className="bastion-cu-body">
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Button
              type="primary"
              size="small"
              icon={<CameraOutlined />}
              aria-label="看一眼屏幕"
              loading={shooting}
              onClick={() => void takeScreenshot()}
            >
              看一眼屏幕
            </Button>
            <div className="bastion-cu-note">
              截图直接来自目标机 MCP，是操作是否生效的唯一凭据。
            </div>
            {shotError ? (
              <Alert type="error" showIcon message={shotError} />
            ) : null}
            {shots.length > 0 ? (
              <div className="bastion-cu-shots" data-testid="computeruse-shots">
                {shots.map((shot) => (
                  <div className="bastion-cu-shot" key={shot.key}>
                    {shot.note ? (
                      <div className="bastion-cu-note">{shot.note}</div>
                    ) : null}
                    {shot.images.map((src, index) => {
                      // 同一张图里多屏时按序号区分；列表顺序固定，用序号做 key 是安全的
                      const imageKey = `${shot.key}:${index}`;
                      return (
                        <img
                          key={imageKey}
                          src={src}
                          alt={`${hostLabel} 屏幕截图 ${index + 1}`}
                        />
                      );
                    })}
                  </div>
                ))}
              </div>
            ) : null}
          </Space>

          <div>
            <div className="bastion-cu-section-title">工具清单</div>
            {tools.length > 0 ? (
              <div className="bastion-cu-tools" data-testid="computeruse-tools">
                {tools.map((tool) => {
                  const meta = describeToolMeta(tool);
                  return (
                    <Tooltip
                      key={tool.name}
                      title={meta.description || '暂无说明'}
                    >
                      <Tag
                        color={meta.color}
                      >{`${meta.name} · ${meta.label}`}</Tag>
                    </Tooltip>
                  );
                })}
              </div>
            ) : (
              <div className="bastion-cu-note">
                还没有拿到工具清单，点右上角刷新试试。
              </div>
            )}
            <div className="bastion-cu-note">
              标「只读」的工具 AI 可以直接调用；标「会改机器状态」的工具由 AI
              调用时需要管理员当场确认。
            </div>
          </div>

          <div className="bastion-cu-chat" data-testid="computeruse-chat">
            <div className="bastion-cu-section-title">AI 对话</div>
            {turns.length === 0 ? (
              <div className="bastion-cu-note">
                例如：「打开记事本，输入今天的巡检结论」「把当前屏幕截一张给我看」。
              </div>
            ) : null}
            {turns.map((turn) =>
              turn.role === 'user' ? (
                <div className="bastion-cu-msg-user" key={turn.key}>
                  {turn.content}
                </div>
              ) : (
                <div className="bastion-cu-msg-ai" key={turn.key}>
                  {turn.reasoning ? (
                    <pre className="bastion-cu-reasoning">{turn.reasoning}</pre>
                  ) : null}
                  {turn.content ? (
                    <XMarkdown
                      content={turn.content}
                      streaming={{ hasNextChunk: Boolean(turn.streaming) }}
                    />
                  ) : turn.streaming ? (
                    <span>
                      <Spin size="small" /> 正在思考…
                    </span>
                  ) : null}
                  {turn.toolCalls.map((call) => (
                    <ToolCallView key={call.callId} call={call} />
                  ))}
                  {turn.error ? (
                    <div className="bastion-cu-msg-error">{turn.error}</div>
                  ) : null}
                </div>
              ),
            )}
          </div>
        </div>

        <div className="bastion-cu-input">
          {streaming ? (
            <Button
              size="small"
              icon={<CloseOutlined />}
              onClick={() => {
                streamRef.current?.abort();
                setStreaming(false);
              }}
            >
              停止生成
            </Button>
          ) : null}
          <Input.TextArea
            value={input}
            autoSize={{ minRows: 1, maxRows: 4 }}
            placeholder="告诉 AI 下一步做什么（回车发送，Shift+回车换行）"
            aria-label="ComputerUse AI 指令"
            onChange={(event) => setInput(event.target.value)}
            onPressEnter={(event) => {
              if (event.shiftKey) {
                return;
              }
              event.preventDefault();
              handleSend();
            }}
          />
          <Button
            type="primary"
            size="small"
            block
            aria-label="发送"
            disabled={!input.trim() || streaming}
            onClick={handleSend}
          >
            发送
          </Button>
        </div>

        <div className="bastion-cu-foot">
          AI 操作的是目标机的控制台会话；与当前 RDP
          会话可能是两个桌面，请以截图为准。
        </div>

        <Modal
          title="敏感操作需要管理员确认"
          open={confirmOpen}
          onCancel={() => {
            setConfirmOpen(false);
            confirmForm.resetFields();
          }}
          footer={null}
          destroyOnHidden
        >
          <Alert
            type="warning"
            showIcon
            message={confirmReason}
            description={confirmCalls.map((call) => (
              <div key={call.callId}>
                <Text strong>{call.name}</Text>
                {call.description ? <div>{call.description}</div> : null}
                {call.args && Object.keys(call.args).length > 0 ? (
                  <JsonCards value={call.args} />
                ) : null}
              </div>
            ))}
          />
          <Form form={confirmForm} layout="vertical">
            <Form.Item
              name="adminUsername"
              label="管理员账号（留空则用当前账号）"
            >
              <Input
                allowClear
                placeholder="留空则用当前登录账号"
                autoComplete="off"
              />
            </Form.Item>
            <Form.Item
              name="adminPassword"
              label="管理员密码"
              rules={[{ required: true, message: '请输入管理员密码' }]}
            >
              <Input.Password
                placeholder="请输入密码以确认执行"
                autoComplete="new-password"
              />
            </Form.Item>
          </Form>
          {confirmError ? (
            <Alert type="error" showIcon message={confirmError} />
          ) : null}
          <Space>
            <Button
              danger
              loading={confirming}
              onClick={() => void rejectConfirm()}
            >
              拒绝执行
            </Button>
            <Button
              type="primary"
              loading={confirming}
              onClick={() => void submitConfirm()}
            >
              确认执行
            </Button>
          </Space>
        </Modal>
      </aside>
    </>
  );
};

interface BoundaryProps {
  children: ReactNode;
}

interface BoundaryState {
  failed: boolean;
}

/**
 * 极简错误边界：侧边栏自身崩了只吞掉这一块，
 * 绝不能让远程桌面画布跟着整页白屏。
 */
export class ComputerUsePanelBoundary extends Component<
  BoundaryProps,
  BoundaryState
> {
  state: BoundaryState = { failed: false };

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: unknown): void {
    console.error('[computeruse] 侧边栏渲染失败，已折叠为空', error);
  }

  render(): ReactNode {
    return this.state.failed ? null : this.props.children;
  }
}

export default ComputerUsePanel;
