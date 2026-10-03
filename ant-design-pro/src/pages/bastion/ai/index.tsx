/**
 * AI 运维（AIOps）对话页。
 *
 * 需求对应关系：
 * - **对话即审计**：左侧历史来自 `GET /api/ai/conversations`；每轮的用户输入、模型输出、
 *   思考过程、工具调用与确认结果都落库（`ai_conversations` / `ai_messages` / `ai_tool_calls`），
 *   审计中心的「AI 对话审计」读的是同一份数据，页面不许自己造状态。
 * - **流式 Markdown**：`POST /api/ai/chat` 是 SSE，这里用 `@ant-design/x-markdown` 边收边渲染。
 * - **卡片**：模型在正文里插入 ```ai-card 代码块（JSON），这里解析成 antd 表格/键值/告警/步骤卡片。
 * - **敏感操作**：后端不会直接执行，而是回 `confirm_required`；前端弹管理员账号密码框，
 *   校验通过后带 `{resume: true}` 续一条流把挂起的操作执行完（密码只走表单，不落前端状态）。
 * - **工具权限**：AI 用的是调用者自己的 JWT，工具目录按登录人权限过滤；这里展示的
 *   「AI 能用什么」就是 `GET /api/ai/tools` 的返回，和后端执行时可用集合完全一致。
 */
import {
  ClearOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { Bubble, Conversations, Sender } from '@ant-design/x';
import { XMarkdown } from '@ant-design/x-markdown';
import { PageContainer } from '@ant-design/pro-components';
import { history, useAccess } from '@umijs/max';
import {
  Alert,
  Avatar,
  Button,
  Collapse,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  AiCard,
  AiConversationRecord,
  AiStatus,
  AiStreamEvent,
  AiToolCategory,
  AiToolInfo,
} from '@/services/bastion/ai';
import {
  confirmAiOperation,
  deleteAiConversation,
  fetchAiStatus,
  fetchAiTools,
  getAiConversation,
  listAiConversations,
  streamAiChat,
} from '@/services/bastion/ai';
import { JsonCards } from '@/components/Bastion';
import { AiCardView } from './cards';
import './ai.css';

const { Text, Paragraph } = Typography;

/** 一次工具调用的前端状态（后端事件字段一一对应，不做二次加工） */
interface ToolCallState {
  callId: string;
  name: string;
  args?: Record<string, unknown>;
  description?: string;
  permission?: string;
  sensitive?: boolean;
  status: string;
  summary?: string;
  preview?: string;
  durationMs?: number;
  confirmedBy?: string;
  card?: AiCard | null;
}

/** 一条气泡：user = 用户提问，assistant = AI 回复（含工具调用与卡片） */
interface ChatTurn {
  key: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning: string;
  cards: AiCard[];
  toolCalls: ToolCallState[];
  streaming?: boolean;
  error?: string;
  status?: string;
  elapsedMs?: number;
  usage?: Record<string, number>;
}

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  running: { color: 'processing', text: '执行中' },
  pending: { color: 'warning', text: '待管理员确认' },
  approved: { color: 'processing', text: '已确认，执行中' },
  success: { color: 'success', text: '成功' },
  failure: { color: 'error', text: '失败' },
  denied: { color: 'error', text: '无权限' },
  rejected: { color: 'default', text: '已拒绝' },
};

const nextKey = (() => {
  let seq = 0;
  return (prefix: string) => {
    seq += 1;
    return `${prefix}-${seq}`;
  };
})();

const asRecord = (value: unknown): Record<string, unknown> | undefined =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;

/** 把 Markdown 代码块的 children 摊平成字符串（块级代码就是一整段文本） */
const flattenText = (value: React.ReactNode): string => {
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(flattenText).join('');
  if (value && typeof value === 'object' && 'props' in (value as { props?: unknown })) {
    return flattenText((value as { props?: { children?: React.ReactNode } }).props?.children);
  }
  return '';
};

const parseCard = (children: React.ReactNode): AiCard | null => {
  const raw = flattenText(children).trim();
  if (!raw.startsWith('{')) return null;
  try {
    const data = JSON.parse(raw) as AiCard;
    return data && typeof data === 'object' && 'type' in data ? data : null;
  } catch {
    return null;
  }
};

/**
 * Markdown 代码块渲染器：普通代码原样交给 `<pre>`，```ai-card 则换成 antd 卡片。
 *
 * 卡片在**原位**渲染（就落在这个代码块的位置），所以正文里的表格/告警/步骤不会跑到别处去。
 * 流式过程中 JSON 还没拼完时给一个「正在生成卡片…」的占位，避免把半截 JSON 甩给用户看。
 */
const AiCode = (
  props: React.HTMLAttributes<HTMLElement> & {
    block?: boolean;
    lang?: string;
    streamStatus?: 'loading' | 'done';
  },
) => {
  const { block, lang, children, streamStatus } = props;
  if (block && typeof lang === 'string' && lang.startsWith('ai-card')) {
    const card = parseCard(children);
    if (card) return <AiCardView card={card} />;
    if (streamStatus === 'loading') {
      return <span className="bastion-ai-card-pending">正在生成卡片…</span>;
    }
    return <code className="bastion-ai-code">{flattenText(children)}</code>;
  }
  return <code className={block ? 'bastion-ai-code' : undefined}>{children}</code>;
};

const asString = (value: unknown): string | undefined =>
  typeof value === 'string' && value ? value : undefined;

const asNumber = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined;

/** 后端错误信封（{success:false,message,code}）里可以直接给用户看的中文 message */
const apiErrorMessage = (error: unknown): string | undefined => {
  const payload = (error as { response?: { data?: { message?: unknown } } } | undefined)?.response
    ?.data;
  const text = payload?.message;
  return typeof text === 'string' && text.trim() ? text : undefined;
};

/**
 * 页面兜底错误提示。
 *
 * umi request 的 errorHandler（`src/requestErrorConfig.ts`）在 4xx/5xx 时已经弹过后端信封里的
 * 中文 message 了（能拿到 `error.response` 就说明它跑过，见 `@umijs/plugins/dist/request.js`
 * 里 "先 handler 再 reject" 的顺序）。页面再弹一次 `error.message` 只会多出一句
 * 「Request failed with status code 400」——这正是用户在敏感操作确认弹窗里看到的那句。
 * 所以：有 response 就交给请求层，没有（流式 fetch / 断网）才由页面提示。
 */
const notifyError = (error: unknown, fallback: string) => {
  if (apiErrorMessage(error)) return;
  message.error(error instanceof Error && error.message ? error.message : fallback);
};

const AiPage = () => {
  const access = useAccess();
  const [status, setStatus] = useState<AiStatus>();
  const [tools, setTools] = useState<{ tools: AiToolInfo[]; groups: AiToolCategory[] }>({
    tools: [],
    groups: [],
  });
  const [toolsOpen, setToolsOpen] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [conversations, setConversations] = useState<AiConversationRecord[]>([]);
  const [activeId, setActiveId] = useState<number>();
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);

  // 敏感操作确认
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmCalls, setConfirmCalls] = useState<ToolCallState[]>([]);
  const [confirmReason, setConfirmReason] = useState('');
  const [confirming, setConfirming] = useState(false);
  /** 后端返回的确认失败原因（如「管理员账号或密码错误」），就地显示在弹窗里 */
  const [confirmError, setConfirmError] = useState<string>();
  const [confirmForm] = Form.useForm<{ adminUsername?: string; adminPassword: string }>();

  const streamRef = useRef<AbortController | undefined>(undefined);
  const conversationIdRef = useRef<number | undefined>(undefined);
  /** resume 时要往哪条气泡里追加内容（通常是刚才那条 assistant） */
  const resumeTurnKeyRef = useRef<string | undefined>(undefined);

  const loadConversations = useCallback(async () => {
    try {
      const body = await listAiConversations({ page: 1, pageSize: 50 });
      setConversations(body.data ?? []);
    } catch {
      // 首屏拉列表失败不打断使用，发送时会再报错
    }
  }, []);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const info = await fetchAiStatus();
        if (!alive) return;
        setStatus(info);
        if (info.canUse) {
          const catalog = await fetchAiTools();
          if (alive) setTools({ tools: catalog.tools ?? [], groups: catalog.groups ?? [] });
          await loadConversations();
        }
      } catch (error) {
        if (alive) {
          notifyError(error, 'AI 状态读取失败');
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [loadConversations]);

  useEffect(
    () => () => {
      streamRef.current?.abort();
    },
    [],
  );

  const patchTurn = useCallback((key: string, patch: (turn: ChatTurn) => ChatTurn) => {
    setTurns((prev) => prev.map((turn) => (turn.key === key ? patch(turn) : turn)));
  }, []);

  /** message_end：收口这条气泡（状态、耗时、token 用量） */
  const markMessageEnd = useCallback(
    (key: string, event: AiStreamEvent) => {
      patchTurn(key, (turn) => ({
        ...turn,
        status: asString(event.status),
        elapsedMs: asNumber(event.elapsedMs),
        usage: asRecord(event.usage) as Record<string, number> | undefined,
        streaming: false,
      }));
    },
    [patchTurn],
  );

  /** 把后端流事件写进气泡 */
  const applyEvent = useCallback(
    (key: string, event: AiStreamEvent) => {
      const delta = asString(event.delta);
      switch (event.type) {
        case 'start': {
          const conversationId = asNumber(event.conversationId);
          if (conversationId) {
            conversationIdRef.current = conversationId;
          }
          break;
        }
        case 'reasoning': {
          if (delta) {
            patchTurn(key, (turn) => ({ ...turn, reasoning: turn.reasoning + delta }));
          }
          break;
        }
        case 'content': {
          if (delta) {
            patchTurn(key, (turn) => ({ ...turn, content: turn.content + delta }));
          }
          break;
        }
        case 'tool_call': {
          const call: ToolCallState = {
            callId: asString(event.callId) ?? nextKey('call'),
            name: asString(event.name) ?? '未知工具',
            args: asRecord(event.args),
            description: asString(event.description),
            permission: asString(event.permission),
            sensitive: Boolean(event.sensitive),
            status: asString(event.status) ?? 'running',
          };
          patchTurn(key, (turn) => ({ ...turn, toolCalls: [...turn.toolCalls, call] }));
          break;
        }
        case 'tool_result': {
          const callId = asString(event.callId);
          patchTurn(key, (turn) => ({
            ...turn,
            toolCalls: turn.toolCalls.map((item) =>
              item.callId === callId
                ? {
                    ...item,
                    status: event.ok === false ? 'failure' : 'success',
                    summary: asString(event.summary) ?? item.summary,
                    preview: asString(event.preview) ?? item.preview,
                    durationMs: asNumber(event.durationMs) ?? item.durationMs,
                    confirmedBy: asString(event.confirmedBy) ?? item.confirmedBy,
                    card: (event.card as AiCard | null) ?? item.card,
                  }
                : item,
            ),
          }));
          break;
        }
        case 'cards': {
          const cards = Array.isArray(event.cards) ? (event.cards as AiCard[]) : [];
          patchTurn(key, (turn) => ({ ...turn, cards }));
          break;
        }
        case 'message_end': {
          markMessageEnd(key, event);
          break;
        }
        case 'error': {
          patchTurn(key, (turn) => ({
            ...turn,
            error: asString(event.message) ?? 'AI 执行失败',
          }));
          break;
        }
        case 'confirm_required': {
          const calls = Array.isArray(event.toolCalls) ? event.toolCalls : [];
          const pendingCalls: ToolCallState[] = calls.map((raw) => {
            const item = asRecord(raw) ?? {};
            return {
              callId: asString(item.callId) ?? nextKey('call'),
              name: asString(item.name) ?? '未知工具',
              args: asRecord(item.args),
              description: asString(item.description),
              permission: asString(item.permission),
              sensitive: true,
              status: 'pending',
            };
          });
          patchTurn(key, (turn) => ({
            ...turn,
            toolCalls: turn.toolCalls.map((item) => {
              const found = pendingCalls.find((call) => call.callId === item.callId);
              return found ? { ...item, status: 'pending' } : item;
            }),
          }));
          resumeTurnKeyRef.current = key;
          setConfirmCalls(pendingCalls);
          setConfirmReason(asString(event.reason) ?? '该操作需要管理员确认');
          setConfirmError(undefined);
          setConfirmOpen(true);
          break;
        }
        default:
          break;
      }
    },
    [markMessageEnd, patchTurn],
  );

  /** 开一条流：新对话 or 续流（resume） */
  const runStream = useCallback(
    async (key: string, payload: Parameters<typeof streamAiChat>[0]) => {
      streamRef.current?.abort();
      const controller = new AbortController();
      streamRef.current = controller;
      setStreaming(true);
      try {
        await streamAiChat(payload, (event) => applyEvent(key, event), controller.signal);
      } catch (error) {
        if ((error as Error)?.name !== 'AbortError') {
          patchTurn(key, (turn) => ({
            ...turn,
            error: error instanceof Error ? error.message : 'AI 请求失败',
          }));
        }
      } finally {
        setStreaming(false);
        patchTurn(key, (turn) => ({ ...turn, streaming: false }));
        await loadConversations();
      }
    },
    [applyEvent, loadConversations, patchTurn],
  );

  const handleSend = useCallback(
    (text: string) => {
      const value = text.trim();
      if (!value || streaming) return;
      setInput('');
      const userKey = nextKey('user');
      const assistantKey = nextKey('assistant');
      setTurns((prev) => [
        ...prev,
        { key: userKey, role: 'user', content: value, reasoning: '', cards: [], toolCalls: [] },
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
        source: 'web',
      });
    },
    [runStream, streaming],
  );

  /** 管理员确认通过 → 续流执行挂起的敏感操作 */
  const submitConfirm = useCallback(async () => {
    setConfirmError(undefined);
    let values: { adminUsername?: string; adminPassword: string };
    try {
      values = await confirmForm.validateFields();
    } catch {
      // 表单自身校验没过（密码为空/账号格式）：antd 已经把提示挂在字段下面了，这里直接返回，
      // 不能让 validateFields 的 reject 冒成 unhandled rejection
      return;
    }
    const conversationId = conversationIdRef.current;
    if (!conversationId) return;
    setConfirming(true);
    try {
      await confirmAiOperation({
        conversationId,
        // 账号留空＝用当前登录账号复验口令（后端 /api/ai/confirm 已支持，见 ai.py:306-312）
        adminUsername: values.adminUsername,
        adminPassword: values.adminPassword,
        approve: true,
      });
      setConfirmOpen(false);
      confirmForm.resetFields();
      const key = resumeTurnKeyRef.current ?? nextKey('assistant');
      await runStream(key, { conversationId, resume: true });
    } catch (error) {
      // 后端的中文原因（如「管理员账号或密码错误」）就地显示在弹窗里：
      // 请求层的 toast 一闪而过，且绝不能再把 axios 的 "Request failed with status code 400" 当提示
      setConfirmError(
        apiErrorMessage(error) ?? (error instanceof Error ? error.message : '确认失败'),
      );
    } finally {
      setConfirming(false);
    }
  }, [confirmForm, runStream]);

  /** 拒绝：也要续流，让模型知道被拒绝并给出替代方案 */
  const rejectConfirm = useCallback(async () => {
    const conversationId = conversationIdRef.current;
    if (!conversationId) return;
    setConfirming(true);
    try {
      await confirmAiOperation({ conversationId, approve: false, reason: '用户在页面上拒绝了该操作' });
      setConfirmOpen(false);
      confirmForm.resetFields();
      const key = resumeTurnKeyRef.current ?? nextKey('assistant');
      await runStream(key, { conversationId, resume: true });
    } catch (error) {
      notifyError(error, '操作失败');
    } finally {
      setConfirming(false);
    }
  }, [confirmForm, runStream]);

  const openConversation = useCallback(async (id: number) => {
    setLoadingHistory(true);
    try {
      const detail = await getAiConversation(id);
      conversationIdRef.current = id;
      setActiveId(id);
      const restored: ChatTurn[] = [];
      for (const item of detail.messages ?? []) {
        if (item.role === 'user') {
          restored.push({
            key: `m-${item.id}`,
            role: 'user',
            content: item.content,
            reasoning: '',
            cards: [],
            toolCalls: [],
          });
        } else if (item.role === 'assistant') {
          const calls = (detail.toolCalls ?? []).filter(
            (call) => call.messageId === item.id || call.conversationId === id,
          );
          restored.push({
            key: `m-${item.id}`,
            role: 'assistant',
            content: item.content,
            reasoning: item.reasoning ?? '',
            cards: item.cards ?? [],
            toolCalls: calls.map((call) => ({
              callId: call.callId,
              name: call.toolName,
              args: call.arguments,
              permission: call.requiredPermission,
              sensitive: call.sensitive,
              status: call.status,
              summary: call.resultSummary,
              preview: call.resultPreview,
              durationMs: call.durationMs,
              confirmedBy: call.confirmedBy,
            })),
            status: item.status,
            elapsedMs: item.elapsedMs,
          });
        }
      }
      setTurns(restored);
    } catch (error) {
      notifyError(error, '对话详情读取失败');
    } finally {
      setLoadingHistory(false);
    }
  }, []);

  const startNewConversation = useCallback(() => {
    streamRef.current?.abort();
    conversationIdRef.current = undefined;
    setActiveId(undefined);
    setTurns([]);
    setInput('');
  }, []);

  const removeConversation = useCallback(
    async (id: number) => {
      try {
        await deleteAiConversation(id);
        message.success('已删除该对话（连带消息与工具调用记录）');
        if (activeId === id) startNewConversation();
        await loadConversations();
      } catch (error) {
        notifyError(error, '删除失败');
      }
    },
    [activeId, loadConversations, startNewConversation],
  );

  const conversationItems = useMemo(
    () =>
      conversations.map((item) => ({
        key: String(item.id),
        label: (
          <span className="bastion-ai-conv-label">
            {/* 标题单行省略：列表项改成自适应高度之后，长标题不能再靠固定高度裁掉 */}
            <span className="bastion-ai-conv-title">{item.title || '未命名对话'}</span>
            <span className="bastion-ai-conv-meta">
              {item.messageCount ?? 0} 条 · 工具 {item.toolCount ?? 0}
            </span>
          </span>
        ),
      })),
    [conversations],
  );

  const filteredGroups = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return tools.groups;
    return tools.groups
      .map((group) => ({
        ...group,
        tools: group.tools.filter(
          (tool) =>
            tool.name.toLowerCase().includes(kw) ||
            tool.description.toLowerCase().includes(kw) ||
            tool.path.toLowerCase().includes(kw),
        ),
      }))
      .filter((group) => group.tools.length > 0);
  }, [keyword, tools.groups]);

  /** 单条气泡内容 */
  const renderTurn = (turn: ChatTurn) => {
    if (turn.role === 'user') {
      return <div className="bastion-ai-user-text">{turn.content}</div>;
    }
    const pendingCalls = turn.toolCalls.filter((call) => call.status === 'pending');
    const items = [];
    if (turn.reasoning) {
      items.push({
        key: 'reasoning',
        label: <span className="bastion-ai-collapse-label">思考过程</span>,
        children: <pre className="bastion-ai-reasoning">{turn.reasoning}</pre>,
      });
    }
    if (turn.toolCalls.length > 0) {
      items.push({
        key: 'tools',
        label: (
          <span className="bastion-ai-collapse-label">
            工具调用 {turn.toolCalls.length} 次
            {pendingCalls.length > 0 ? <Tag color="warning">待确认 {pendingCalls.length}</Tag> : null}
          </span>
        ),
        children: <div className="bastion-ai-tools">{turn.toolCalls.map(renderToolCall)}</div>,
      });
    }
    return (
      <div className="bastion-ai-turn">
        {items.length > 0 ? (
          <Collapse
            ghost
            size="small"
            className="bastion-ai-collar"
            defaultActiveKey={pendingCalls.length > 0 ? ['tools'] : undefined}
            items={items}
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
        ) : null}
        {turn.streaming && !turn.content && !turn.reasoning ? (
          <span className="bastion-ai-thinking">
            <Spin size="small" /> 正在思考…
          </span>
        ) : null}
        {turn.error ? (
          <Alert className="bastion-ai-error" type="error" showIcon message={turn.error} />
        ) : null}
        {turn.elapsedMs ? (
          <div className="bastion-ai-meta">
            {turn.status === 'max_rounds' ? '已达单轮工具调用上限 · ' : ''}
            用时 {(turn.elapsedMs / 1000).toFixed(1)}s
            {turn.usage?.completion_tokens ? ` · 输出 ${turn.usage.completion_tokens} tokens` : ''}
          </div>
        ) : null}
      </div>
    );
  };

  const renderToolCall = (call: ToolCallState) => {
    const tag = STATUS_TAG[call.status] ?? { color: 'default', text: call.status };
    return (
      <div className="bastion-ai-tool" key={call.callId}>
        <div className="bastion-ai-tool-head">
          <Tag color={tag.color}>{tag.text}</Tag>
          <Text strong>{call.name}</Text>
          {call.sensitive ? <Tag color="red">敏感操作</Tag> : null}
          {call.permission ? <Tag>{call.permission}</Tag> : null}
          {call.durationMs ? <Text type="secondary">{call.durationMs}ms</Text> : null}
          {call.confirmedBy ? <Text type="secondary">确认人 {call.confirmedBy}</Text> : null}
        </div>
        {call.description ? (
          <div className="bastion-ai-tool-desc">{call.description}</div>
        ) : null}
        {call.args && Object.keys(call.args).length > 0 ? (
          <div className="bastion-ai-tool-args">
            <JsonCards value={call.args} compact />
          </div>
        ) : null}
        {call.summary ? <div className="bastion-ai-tool-summary">{call.summary}</div> : null}
        {call.preview ? (
          <div className="bastion-ai-tool-preview">
            <JsonCards value={call.preview} compact />
          </div>
        ) : null}
        {call.card ? <AiCardView card={call.card} /> : null}
      </div>
    );
  };

  const bubbleItems = turns.map((turn) => ({
    key: turn.key,
    role: turn.role,
    content: renderTurn(turn),
    loading: turn.streaming && !turn.content && !turn.reasoning,
  }));

  const disabledReason = !status?.enabled
    ? '管理员尚未开启 AI 运维功能（参数设置里的 AI 开关）'
    : !status?.configured
      ? '尚未配置 DEEPSEEK_API_KEY（写入 bastion-backend/.env 后重启服务）'
      : undefined;

  return (
    <PageContainer
      // ghost：去掉 PageContainer 自带的内容白卡片——用户说的「卡片套卡片」最外面那一层就是它。
      // 内容区高度按视口算好，交给下面的 .bastion-ai 做「左侧列表 + 右侧对话」两栏平铺。
      ghost
      className="bastion-ai-page"
      title="AI 运维"
      childrenContentStyle={{
        paddingBlock: 0,
        height: 'calc(100vh - 160px)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
      extra={[
        <Tag key="model" icon={<RobotOutlined />} color="blue">
          {status?.model || '未配置模型'}
        </Tag>,
        <Tag key="tools" icon={<ThunderboltOutlined />} color="geekblue">
          {status?.toolCount ?? 0} 个工具可用
        </Tag>,
        <Button key="catalog" onClick={() => setToolsOpen(true)}>
          AI 能用什么
        </Button>,
        access.canAiView ? (
          <Button key="audit" onClick={() => history.push('/audit/ai')}>
            对话与工具审计
          </Button>
        ) : null,
        <Tooltip key="reload" title="刷新对话列表">
          <Button icon={<ReloadOutlined />} onClick={() => void loadConversations()} />
        </Tooltip>,
      ]}
    >
      {disabledReason ? (
        <Alert className="bastion-ai-disabled" type="warning" showIcon message={disabledReason} />
      ) : null}
      <div className="bastion-ai">
        {/* 左侧：贴页全高的平铺列表，只有一条右侧分隔线，没有任何浮层/圆角/阴影外壳 */}
        <aside className="bastion-ai-side">
          <div className="bastion-ai-side-head">
            <Button block type="primary" icon={<PlusOutlined />} onClick={startNewConversation}>
              新建对话
            </Button>
          </div>
          <div className="bastion-ai-side-list">
            {conversations.length === 0 ? (
              <Empty
                className="bastion-ai-empty"
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="还没有对话记录"
              />
            ) : (
              <Conversations
                items={conversationItems}
                activeKey={activeId ? String(activeId) : undefined}
                onActiveChange={(key) => void openConversation(Number(key))}
                menu={(item) => ({
                  items: [
                    {
                      key: 'delete',
                      label: '删除对话',
                      icon: <DeleteOutlined />,
                      danger: true,
                    },
                  ],
                  onClick: () => void removeConversation(Number(item.key)),
                })}
              />
            )}
          </div>
        </aside>
        <section className="bastion-ai-main">
          <div className="bastion-ai-stream">
            {loadingHistory ? (
              <div className="bastion-ai-loading">
                <Spin /> 正在读取历史对话…
              </div>
            ) : bubbleItems.length === 0 ? (
              <div className="bastion-ai-welcome">
                <RobotOutlined className="bastion-ai-welcome-icon" />
                <div className="bastion-ai-welcome-title">告诉我要做什么，我来操作并给你证据</div>
                <div className="bastion-ai-welcome-hints">
                  {[
                    '列出当前所有在线会话',
                    '有哪些主机？把最近新增的三台列出来',
                    '在 web-01 上执行 uptime 和 df -h',
                    '新建一个只读用户，只给查看权限',
                    '审计一下最近的敏感命令',
                  ].map((hint) => (
                    <Tag
                      key={hint}
                      className="bastion-ai-hint"
                      onClick={() => handleSend(hint)}
                    >
                      {hint}
                    </Tag>
                  ))}
                </div>
              </div>
            ) : (
              <Bubble.List
                className="bastion-ai-bubbles"
                autoScroll
                items={bubbleItems}
                role={{
                  user: { placement: 'end', variant: 'filled' },
                  assistant: {
                    placement: 'start',
                    // borderless：AI 的正文直接落在页面上，不再被套进一层描边的「气泡卡片」里
                    // —— 工具调用块、AI 卡片都在它里面，外面再包一层就等于「卡片套卡片」
                    variant: 'borderless',
                    avatar: <Avatar icon={<RobotOutlined />} style={{ background: '#1677ff' }} />,
                  },
                }}
              />
            )}
          </div>
          <div className="bastion-ai-input">
            {streaming ? (
              <Button
                className="bastion-ai-stop"
                icon={<ClearOutlined />}
                onClick={() => {
                  streamRef.current?.abort();
                  setStreaming(false);
                }}
              >
                停止生成
              </Button>
            ) : null}
            <Sender
              value={input}
              loading={streaming}
              placeholder="例如：在 web-01 上执行 uptime（敏感操作会要管理员密码确认）"
              onChange={(value) => setInput(value)}
              onSubmit={(value) => handleSend(value ?? input)}
            />
          </div>
        </section>
      </div>

      <Drawer
        title={`AI 可用工具（${tools.tools.length} 个）`}
        width={720}
        open={toolsOpen}
        onClose={() => setToolsOpen(false)}
      >
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="按名称 / 说明 / 接口搜索"
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          className="bastion-ai-tool-search"
        />
        <Paragraph type="secondary" className="bastion-ai-tool-tip">
          工具目录按当前登录账号的权限过滤：AI 只能用你本人能用的能力，调用时复用你的 JWT，
          后端仍会再校验一次权限并写审计。
        </Paragraph>
        <Collapse
          accordion
          items={filteredGroups.map((group) => ({
            key: group.category,
            label: `${group.category}（${group.tools.length}）`,
            children: (
              <div className="bastion-ai-tool-list">
                {group.tools.map((tool) => (
                  <div className="bastion-ai-tool-item" key={tool.name}>
                    <div className="bastion-ai-tool-item-head">
                      <Text strong>{tool.name}</Text>
                      {tool.sensitive ? <Tag color="red">敏感</Tag> : null}
                      <Tag>{tool.permission}</Tag>
                      <Text type="secondary" code>
                        {tool.method} {tool.path}
                      </Text>
                    </div>
                    <div className="bastion-ai-tool-item-desc">{tool.description}</div>
                  </div>
                ))}
              </div>
            ),
          }))}
        />
      </Drawer>

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
          description={
            <div className="bastion-ai-confirm-list">
              {confirmCalls.map((call) => (
                <div key={call.callId}>
                  <Text strong>{call.name}</Text>
                  {call.description ? <div>{call.description}</div> : null}
                  {call.args && Object.keys(call.args).length > 0 ? (
                    <div className="bastion-ai-tool-args">
                      <JsonCards value={call.args} />
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          }
        />
        <Form form={confirmForm} layout="vertical" className="bastion-ai-confirm-form">
          <Form.Item name="adminUsername" label="管理员账号（留空则用当前账号）">
            <Input allowClear placeholder="留空则用当前登录账号" autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="adminPassword"
            label="管理员密码"
            rules={[{ required: true, message: '请输入管理员密码' }]}
          >
            <Input.Password placeholder="请输入密码以确认执行" autoComplete="new-password" />
          </Form.Item>
        </Form>
        {/* 功能性错误提示（后端拒绝原因，如「管理员账号或密码错误」），不是说明性提示条 */}
        {confirmError ? (
          <Alert className="bastion-ai-confirm-error" type="error" showIcon message={confirmError} />
        ) : null}
        <Space>
          <Popconfirm
            title="拒绝这次操作？"
            description="AI 会收到「用户已拒绝」，并给出替代方案。"
            okText="拒 绝"
            cancelText="取 消"
            onConfirm={() => void rejectConfirm()}
          >
            <Button danger loading={confirming}>
              拒绝执行
            </Button>
          </Popconfirm>
          <Button type="primary" loading={confirming} onClick={() => void submitConfirm()}>
            确认执行
          </Button>
        </Space>
      </Modal>
    </PageContainer>
  );
};

export default AiPage;
