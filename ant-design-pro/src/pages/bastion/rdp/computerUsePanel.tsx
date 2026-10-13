/**
 * WebRDP 右侧的「ComputerUse AI 操作台」侧边栏。
 *
 * 目标机上部署了 Windows-MCP（streamable-http）之后，AI 才能通过它去操作那台机器的桌面：
 * 本面板负责展示部署状态、重启目标机上的 MCP，以及与 AI 的流式对话（思考过程、工具调用、
 * 管理员确认弹窗，复用 `@/services/bastion/ai` 的同一套流与接口）。
 *
 * 三个必须记住的前提：
 * 1. **只有部署成功（`ready === true`）才渲染**：默认接口不可用时整块侧边栏不出现，
 *    绝不占着 RDP 画面又给一堆报错。
 * 2. **UI 自动化与截图是按登录会话隔离的**：开机自启的 MCP 跑在目标机的登录会话里，
 *    而当前 WebRDP 可能是另一个 RDP 会话 —— 操作员在自己的窗口里未必看得见 AI 的动作，
 *    所以截图会跟着工具调用一起、以图片卡片回到对话里（后端把 MCP 的图变成 `image` 卡片）。
 * 3. **卡片与 Markdown 与「AI 运维」页共用同一份实现**：结构类名沿用 `bastion-ai-*`
 *    （`ai.css` / `cards.tsx`），antd 部件统一走 `ConfigProvider` 的深色算法 ——
 *    深色 RDP 主题下不能有黑字，也不能出现两套长得不一样的卡片。
 *
 * 本文件自包含：不引用 `src/pages/bastion/computer-use/**` 与
 * `src/services/bastion/computeruse.ts`，只依赖通用服务层（`ai.ts` / `client.ts`）
 * 与 AI 页的卡片渲染（`../ai/cards`）。
 */
import {
  CloseOutlined,
  LeftOutlined,
  ReloadOutlined,
  RightOutlined,
  RobotOutlined,
  SendOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { XMarkdown } from '@ant-design/x-markdown';
import { request } from '@umijs/max';
import type { CollapseProps } from 'antd';
import {
  Alert,
  Button,
  Collapse,
  ConfigProvider,
  Form,
  Input,
  Modal,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  theme,
} from 'antd';
import type { CSSProperties, ReactNode } from 'react';
import { Component, useCallback, useEffect, useRef, useState } from 'react';
import { JsonCards } from '@/components/Bastion';
import type { AiCard } from '@/services/bastion/ai';
import { confirmAiOperation, streamAiChat } from '@/services/bastion/ai';
import { unwrap } from '@/services/bastion/client';
import type { ApiData } from '@/services/bastion/types';
import { AiCards, AiCardView, AiCode } from '../ai/cards';
import '../ai/ai.css';

const { Text } = Typography;

/* ------------------------------------------------------------------ *
 * 常量与类型（与后端契约一一对应，见任务说明里的接口清单）
 * ------------------------------------------------------------------ */

/** 展开/收起状态在 localStorage 里的键名 */
export const PANEL_OPEN_KEY = 'bastion_computeruse_panel_open';

/** 部署状态轮询间隔：窗口开着的时候正好把机器部署完，也要能自己亮起来 */
const STATUS_POLL_MS = 30000;

/** 欢迎态里的示例问题：点一下就发出去（与 AI 运维页同一套交互） */
export const WELCOME_HINTS = [
  '打开记事本，输入今天的巡检结论',
  '把当前屏幕截一张给我看',
  '这台机器装的是什么版本的 Python？',
  '列一下正在运行的程序',
];

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

/** 一条工具调用的结果（含后端回传的图片卡片，截图就靠它回到对话里） */
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

/**
 * 流式增量的专用取值：**不能**像 asText 那样「空白即空」。
 *
 * 上游是按 token 吐的，`- ` 会分成 `-` 和 ` ` 两段、`win-75 上` 也会在空格处断开。
 * 只要把纯空白的增量当成空丢掉，markdown 的列表标记就退化成 `-文字`（列表塌成一整段），
 * 中英文之间的空格也会凭空消失 —— 这正是「侧边栏里 Markdown 根本没被渲染」的真因。
 * 主界面 AI 运维页的 asString 就是只判 `value` 不判 `value.trim()`，这里对齐它。
 */
export const asStreamDelta = (value: unknown): string | undefined =>
  typeof value === 'string' && value ? value : undefined;

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

/** POST /api/computer-use/mcp/restart（把目标机上的 MCP 重启掉：卡住/截图空白时的自救） */
export const buildRestartRequest = (hostId: number) => ({
  url: '/api/computer-use/mcp/restart',
  body: { hostId },
});

/** 后端会把 MCP 的报错原样带回来：这里补一句「为什么没图、怎么救」 */
export const screenshotHint = (text?: string): string | undefined => {
  if (!text) return undefined;
  const lower = text.toLowerCase();
  if (
    lower.includes('screen grab failed') ||
    lower.includes('error capturing screenshot')
  ) {
    return '目标机当前没有已登录的桌面会话（MCP 跑在会话 0），抓不到桌面像素。在目标机上登录一次桌面，再点上面的「重启 MCP」把服务接管到你的桌面会话。';
  }
  if (lower.includes('没有返回结果') || lower.includes('卡住')) {
    return '目标机上的 MCP 没在超时内回话（多半是取 UI 元素树把它卡住了）：点上面的「重启 MCP」就能恢复，之后的调用不会再带会卡死的参数。';
  }
  return undefined;
};

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
  const delta = asStreamDelta(record.delta);
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
  /* 「AI 运维」页那套平面配色的四个变量（ai.css 里定义在 .bastion-ai-page 上，这里要自己带上） */
  --ai-line: rgba(148, 163, 184, 0.3);
  --ai-fill: rgba(148, 163, 184, 0.14);
  --ai-fill-strong: rgba(148, 163, 184, 0.22);
  --ai-accent: #1677ff;
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
  /* 深色面板上的次要文字：slate-400，别用 slate-500（#64748b 在 #0f172a 上对比度只有 4.6:1，看着发灰） */
  color: #94a3b8;
  line-height: 1.6;
}
.bastion-cu-welcome {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 4px 0;
}
.bastion-cu-welcome-icon {
  font-size: 20px;
  color: #60a5fa;
}
.bastion-cu-welcome-title {
  font-weight: 600;
  color: #e6edf3;
}
.bastion-cu-welcome-hints {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.bastion-cu-hint {
  cursor: pointer;
}
/* 对话区：AI 轮次结构与「AI 运维」页共用同一套类名（样式来自 ../ai/ai.css） */
.bastion-cu-chat {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bastion-cu-chat .bastion-ai-collar {
  margin-bottom: 0;
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
/* 截图空白/服务卡住时给一句「为什么、怎么办」 */
.bastion-cu-tip {
  margin-top: 6px;
  color: #fbbf24;
  line-height: 1.6;
}
.bastion-cu-input {
  flex: 0 0 auto;
  padding: 8px 10px;
  border-top: 1px solid #1f2937;
  display: flex;
  flex-direction: row;
  align-items: flex-end;
  gap: 6px;
}
.bastion-cu-input-box {
  flex: 1 1 auto;
  min-width: 0;
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
  /* 页脚保留（用户要求），但颜色提到 slate-400，深色底上看得清 */
  color: #94a3b8;
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

/** 单条工具调用（结构与「AI 运维」页的 renderToolCall 一致） */
const ToolCallView = ({ call }: { call: ComputerUseToolCall }) => {
  const tag = COMPUTER_USE_STATUS[call.status] ?? {
    color: 'default',
    text: call.status,
  };
  const hint = screenshotHint(call.summary) ?? screenshotHint(call.preview);
  return (
    <div className="bastion-ai-tool">
      <div className="bastion-ai-tool-head">
        <Tag color={tag.color}>{tag.text}</Tag>
        <Text strong>{call.name}</Text>
        {call.durationMs ? (
          <Text type="secondary">{call.durationMs}ms</Text>
        ) : null}
        {call.confirmedBy ? (
          <Text type="secondary">确认人 {call.confirmedBy}</Text>
        ) : null}
      </div>
      {call.description ? (
        <div className="bastion-ai-tool-desc">{call.description}</div>
      ) : null}
      {call.args && Object.keys(call.args).length > 0 ? (
        <div className="bastion-ai-tool-args">
          <JsonCards value={call.args} compact />
        </div>
      ) : null}
      {call.summary ? (
        <div className="bastion-ai-tool-summary">
          {truncateText(call.summary, 600)}
        </div>
      ) : null}
      {call.preview ? (
        <div className="bastion-ai-tool-preview">
          <JsonCards value={call.preview} compact />
        </div>
      ) : null}
      {call.card ? <AiCardView card={call.card} /> : null}
      {hint ? <div className="bastion-cu-tip">{hint}</div> : null}
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
  const [restarting, setRestarting] = useState(false);
  const [restartError, setRestartError] = useState<string>();
  // 「思考过程」/「工具调用 N 次」默认展开，但工具调用往往比思考过程晚一步出现，
  // 而 Collapse 的 defaultActiveKey 只在挂载时读一次 —— 所以改成受控：只记住用户手动收起的键。
  const [collapsedCollars, setCollapsedCollars] = useState<
    Record<string, true>
  >({});
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

  /** 重启目标机上的 MCP：卡住或截图空白时的自救，失败也只给可读文案 */
  const restartMcp = useCallback(async () => {
    setRestarting(true);
    setRestartError(undefined);
    const { url, body } = buildRestartRequest(hostId);
    try {
      const data = await unwrap(
        request<ApiData<Record<string, unknown>>>(url, {
          method: 'POST',
          data: body,
          // 目标机上要「停掉旧进程 → 起新进程 → 等端口重新监听」，得留够时间
          timeout: 180000,
        }),
      );
      if (data?.ok === false) {
        setRestartError(asText(data.error) ?? '目标机上的 MCP 没能重新起来');
        return;
      }
      const probe = asRecord(data?.probe);
      const count = asNumber(probe?.toolCount);
      setProbeOk(true);
      setProbeText(
        count === undefined ? 'MCP 已重启' : `MCP 已重启（${count} 个工具）`,
      );
      await loadStatus();
      await loadTools();
    } catch (error) {
      setRestartError(describeApiError(error, '重启 MCP 失败'));
    } finally {
      setRestarting(false);
    }
  }, [hostId, loadStatus, loadTools]);

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

  /** 真正发一条消息：输入框回车、发送按钮、欢迎语里的示例问题都走这里 */
  const send = useCallback(
    (raw: string) => {
      const value = raw.trim();
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
    },
    [runStream, streaming],
  );

  /** 发送：回车发送、Shift+回车换行（在输入框上处理） */
  const handleSend = useCallback(() => {
    send(input);
  }, [input, send]);

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
      <ConfigProvider theme={{ algorithm: theme.darkAlgorithm }}>
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
      </ConfigProvider>
    );
  }

  return (
    <ConfigProvider theme={{ algorithm: theme.darkAlgorithm }}>
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
            <Tooltip title="重启目标机上的 MCP 服务（卡住或截图空白时点它）">
              <Button
                size="small"
                type="text"
                aria-label="重启 MCP"
                icon={<SyncOutlined />}
                loading={restarting}
                onClick={() => void restartMcp()}
              />
            </Tooltip>
            <Tooltip title="探活并刷新状态">
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
          {restartError ? (
            <Alert
              type="error"
              showIcon
              closable
              message={restartError}
              onClose={() => setRestartError(undefined)}
            />
          ) : null}

          <div className="bastion-cu-chat" data-testid="computeruse-chat">
            <div className="bastion-cu-section-title">AI 对话</div>
            {turns.length === 0 ? (
              <div className="bastion-cu-welcome">
                <RobotOutlined className="bastion-cu-welcome-icon" />
                <div className="bastion-cu-welcome-title">
                  告诉 AI 在这台机器上做什么，它来操作并给你截图
                </div>
                <div className="bastion-cu-welcome-hints">
                  {WELCOME_HINTS.map((hint) => (
                    <Tag
                      key={hint}
                      className="bastion-cu-hint"
                      onClick={() => send(hint)}
                    >
                      {hint}
                    </Tag>
                  ))}
                </div>
              </div>
            ) : null}
            {turns.map((turn) => {
              if (turn.role === 'user') {
                return (
                  <div className="bastion-cu-msg-user" key={turn.key}>
                    {turn.content}
                  </div>
                );
              }
              // 思考过程与工具调用折叠成两块、默认展开 —— 与 AI 运维页同一套阅读顺序。
              // 用受控 activeKey：新长出来的块（比如后到的「工具调用 N 次」）也要自动展开。
              const pending = turn.toolCalls.filter(
                (call) => call.status === 'pending',
              ).length;
              const collarItems: CollapseProps['items'] = [];
              if (turn.reasoning) {
                collarItems.push({
                  key: 'reasoning',
                  label: (
                    <span className="bastion-ai-collapse-label">思考过程</span>
                  ),
                  children: (
                    <pre className="bastion-ai-reasoning">{turn.reasoning}</pre>
                  ),
                });
              }
              if (turn.toolCalls.length > 0) {
                collarItems.push({
                  key: 'tools',
                  label: (
                    <span className="bastion-ai-collapse-label">
                      工具调用 {turn.toolCalls.length} 次
                      {pending > 0 ? (
                        <Tag color="warning">待确认 {pending}</Tag>
                      ) : null}
                    </span>
                  ),
                  children: (
                    <div className="bastion-ai-tools">
                      {turn.toolCalls.map((call) => (
                        <ToolCallView key={call.callId} call={call} />
                      ))}
                    </div>
                  ),
                });
              }
              return (
                <div className="bastion-ai-turn" key={turn.key}>
                  {collarItems.length > 0 ? (
                    <Collapse
                      ghost
                      size="small"
                      className="bastion-ai-collar"
                      items={collarItems}
                      activeKey={collarItems
                        .map((item) => String(item?.key))
                        .filter(
                          (key) => !collapsedCollars[`${turn.key}:${key}`],
                        )}
                      onChange={(keys) => {
                        const next = Array.isArray(keys) ? keys : [keys];
                        setCollapsedCollars((prev) => {
                          const map = { ...prev };
                          for (const item of collarItems) {
                            const key = `${turn.key}:${String(item?.key)}`;
                            if (next.includes(String(item?.key))) {
                              delete map[key];
                            } else {
                              map[key] = true;
                            }
                          }
                          return map;
                        });
                      }}
                    />
                  ) : null}
                  {turn.content ? (
                    <XMarkdown
                      className="bastion-ai-markdown"
                      content={turn.content}
                      openLinksInNewTab
                      streaming={{ hasNextChunk: Boolean(turn.streaming) }}
                      components={{ code: AiCode }}
                    />
                  ) : turn.streaming ? (
                    <span className="bastion-ai-thinking">
                      <Spin size="small" /> 正在思考…
                    </span>
                  ) : null}
                  {turn.cards.length > 0 ? (
                    <AiCards cards={turn.cards} />
                  ) : null}
                  {turn.elapsedMs ? (
                    <div className="bastion-ai-meta">
                      用时 {(turn.elapsedMs / 1000).toFixed(1)}s
                      {turn.status === 'max_rounds'
                        ? ' · 已达单轮工具调用上限'
                        : ''}
                    </div>
                  ) : null}
                  {turn.error ? (
                    <Alert
                      className="bastion-ai-error"
                      type="error"
                      showIcon
                      message={turn.error}
                    />
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>

        <div className="bastion-cu-input">
          <div className="bastion-cu-input-box">
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
          </div>
          {streaming ? (
            <Button
              size="small"
              icon={<CloseOutlined />}
              aria-label="停止生成"
              onClick={() => {
                streamRef.current?.abort();
                setStreaming(false);
              }}
            >
              停止
            </Button>
          ) : (
            <Button
              type="primary"
              size="small"
              icon={<SendOutlined />}
              aria-label="发送"
              disabled={!input.trim()}
              onClick={handleSend}
            >
              发送
            </Button>
          )}
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
    </ConfigProvider>
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
