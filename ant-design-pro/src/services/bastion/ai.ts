/**
 * AI 运维（AIOps）服务层。
 *
 * 关键点：
 * 1. 对话接口是 **SSE 流**，不能用 umi request 的 JSON 解包，必须自己 fetch + 读流；
 * 2. 工具调用、卡片、思考过程都是流里的独立事件，页面按事件增量渲染；
 * 3. 敏感操作会先收到 `confirm_required`，此时要弹管理员密码框，确认后带
 *    `{conversationId, resume: true}` 再开一条流把挂起的操作执行完。
 */
import { request } from '@umijs/max';
import { getToken, pageParams } from './client';
import type { ApiData, ApiList, PageParams } from './types';

/** 卡片：与后端提示词里的 ```ai-card 协议一一对应 */
export interface AiCardTable {
  type: 'table';
  title?: string;
  columns?: { key: string; title?: string }[];
  rows?: Record<string, unknown>[];
}
export interface AiCardKeyValue {
  type: 'keyvalue';
  title?: string;
  items?: { label: string; value?: unknown; status?: string }[];
}
export interface AiCardAlert {
  type: 'alert';
  level?: 'info' | 'warning' | 'error' | 'success';
  title?: string;
  text?: string;
}
export interface AiCardSteps {
  type: 'steps';
  title?: string;
  items?: { title: string; description?: string; status?: string }[];
}
export type AiCard = AiCardTable | AiCardKeyValue | AiCardAlert | AiCardSteps;

export interface AiStatus {
  enabled: boolean;
  configured: boolean;
  model: string;
  baseUrl?: string;
  envFile?: string;
  canUse: boolean;
  canViewAll: boolean;
  canManage: boolean;
  confirmTtl: number;
  maxToolRounds: number;
  historyLimit: number;
  toolCount: number;
  totalToolCount: number;
  permissionLabels?: Record<string, string>;
}

export interface AiToolInfo {
  name: string;
  description: string;
  method: string;
  path: string;
  permission: string;
  permissionLabel?: string;
  category: string;
  sensitive: boolean;
  args?: { name: string; type: string; description?: string; required?: boolean }[];
}

export interface AiToolCategory {
  category: string;
  count: number;
  tools: AiToolInfo[];
}

export interface AiToolCallRecord {
  id: number;
  conversationId?: number;
  messageId?: number;
  callId: string;
  username?: string;
  toolName: string;
  arguments?: Record<string, unknown>;
  requiredPermission?: string;
  sensitive?: boolean;
  status: string;
  confirmedBy?: string;
  confirmedAt?: string;
  resultSummary?: string;
  resultPreview?: string;
  error?: string;
  durationMs?: number;
  createdAt?: string;
}

export interface AiMessageRecord {
  id: number;
  role: 'user' | 'assistant' | 'tool' | 'system';
  content: string;
  reasoning?: string;
  cards?: AiCard[];
  toolCalls?: Record<string, unknown>[];
  toolCallId?: string;
  status?: string;
  model?: string;
  elapsedMs?: number;
  promptTokens?: number;
  completionTokens?: number;
  reasoningTokens?: number;
  createdAt?: string;
}

export interface AiConversationRecord {
  id: number;
  title: string;
  username?: string;
  source?: string;
  model?: string;
  hostId?: number;
  hostName?: string;
  hostAddress?: string;
  sid?: string;
  messageCount?: number;
  toolCount?: number;
  tokenCount?: number;
  createdAt?: string;
  updatedAt?: string;
  messages?: AiMessageRecord[];
  toolCalls?: AiToolCallRecord[];
}

/** 流事件：type 决定字段，见后端 app/ai/service.py 的 run_turn */
export interface AiStreamEvent {
  type: string;
  [key: string]: unknown;
}

export const fetchAiStatus = async (): Promise<AiStatus> =>
  (await request<ApiData<AiStatus>>('/api/ai/status', { method: 'GET' })).data;

export const fetchAiTools = async (): Promise<{
  tools: AiToolInfo[];
  groups: AiToolCategory[];
  total: number;
}> => {
  const body = await request<
    ApiData<{ tools: AiToolInfo[]; groups: AiToolCategory[]; total: number }>
  >('/api/ai/tools', { method: 'GET' });
  return body.data;
};

export const listAiConversations = (params: PageParams = {}) =>
  request<ApiList<AiConversationRecord>>('/api/ai/conversations', {
    method: 'GET',
    params: pageParams(params),
  });

export const getAiConversation = async (id: number): Promise<AiConversationRecord> =>
  (
    await request<ApiData<AiConversationRecord>>(`/api/ai/conversations/${id}`, {
      method: 'GET',
    })
  ).data;

export const deleteAiConversation = async (id: number): Promise<void> => {
  await request<ApiData<null>>(`/api/ai/conversations/${id}`, { method: 'DELETE' });
};

export const listAiToolCalls = (params: PageParams = {}) =>
  request<ApiList<AiToolCallRecord>>('/api/ai/tool-calls', {
    method: 'GET',
    params: pageParams(params),
  });

/** 敏感操作确认：校验管理员账号密码，通过后挂起的操作才允许执行 */
export const confirmAiOperation = async (payload: {
  conversationId: number;
  toolCallId?: number;
  approve?: boolean;
  adminUsername?: string;
  adminPassword?: string;
  reason?: string;
}): Promise<{ approved: number; rejected: number }> =>
  (
    await request<ApiData<{ approved: number; rejected: number }>>('/api/ai/confirm', {
      method: 'POST',
      data: payload,
    })
  ).data;

export interface AiChatPayload {
  message?: string;
  conversationId?: number;
  hostId?: number;
  sid?: string;
  source?: string;
  resume?: boolean;
}

/**
 * 发起一次流式对话。返回一个可取消的 Promise；每个 SSE 事件都会回调 onEvent。
 *
 * 注意：不能用 umi request（它会等整个响应体），必须用 fetch 读 ReadableStream，
 * 这样 Markdown 才能边生成边渲染。
 */
export async function streamAiChat(
  payload: AiChatPayload,
  onEvent: (event: AiStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch('/api/ai/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      Authorization: `Bearer ${getToken()}`,
    },
    body: JSON.stringify(payload),
    signal,
  });

  if (!response.ok && !response.body) {
    throw new Error(`对话失败：HTTP ${response.status}`);
  }
  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  const flush = (chunk: string) => {
    for (const block of chunk.split('\n\n')) {
      const piece = block.trim();
      if (!piece || piece.startsWith(':')) continue;
      for (const line of piece.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const raw = line.slice(5).trim();
        if (!raw) continue;
        try {
          onEvent(JSON.parse(raw) as AiStreamEvent);
        } catch {
          // 半截 JSON（理论上不会发生）直接忽略，避免整条流被打断
        }
      }
    }
  };

  for (;;) {
    // biome-ignore lint/performance/noAwaitInLoops: 流式读取必须顺序 await
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const boundary = buffer.lastIndexOf('\n\n');
    if (boundary >= 0) {
      flush(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) flush(buffer);
}
