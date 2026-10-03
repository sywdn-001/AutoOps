/**
 * 审计中心 · AI 对话审计（需求②：与 AI 的对话也要入审计）。
 *
 * 两个面：
 * - **AI 对话**：谁在什么时候问了什么、AI 回了什么（含思考过程与卡片）、用了哪些工具；
 *   展开一行还能看到逐条消息原文，避免"只有摘要、无法复盘"。
 * - **工具调用**：每一次工具调用的入参、结果、是否敏感、审批状态、审批人是谁——
 *   尤其敏感操作，`pending → approved/rejected` 与 `confirmed_by` 都在这里能追。
 *
 * **结构化数据一律走卡片**（`JsonCards` / `AiCards`）：工具入参、工具返回、`role="tool"`
 * 的消息正文、模型给的 ```ai-card 都是 JSON，糊成一大坨 `<pre>` 谁都读不下去；
 * 卡片只是换一种排法，每层都保留「原始 JSON」入口，留痕不改写。
 *
 * 数据面权限由后端控制：默认只能看自己的对话，`ai:view_all` 才能看全部。
 */
import { ReloadOutlined } from '@ant-design/icons';
import type { ActionType, ProColumns } from '@ant-design/pro-components';
import { PageContainer, ProTable } from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  Alert,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Popover,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useCallback, useRef, useState } from 'react';
import { JsonCards, MonoCell, splitToolMessage, TimeCell } from '@/components/Bastion';
import type {
  AiConversationRecord,
  AiMessageRecord,
  AiToolCallRecord,
} from '@/services/bastion/ai';
import { getAiConversation, listAiConversations, listAiToolCalls } from '@/services/bastion/ai';
import { humanAuditMessage } from '@/services/bastion/constants';
import { AiCards } from './cards';

const { Text, Paragraph } = Typography;

const ROLE_LABEL: Record<string, { color: string; text: string }> = {
  user: { color: 'blue', text: '用户' },
  assistant: { color: 'geekblue', text: 'AI' },
  tool: { color: 'default', text: '工具' },
  system: { color: 'purple', text: '系统' },
};

const CALL_STATUS: Record<string, { color: string; text: string }> = {
  pending: { color: 'warning', text: '待确认' },
  approved: { color: 'processing', text: '已确认' },
  rejected: { color: 'default', text: '已拒绝' },
  running: { color: 'processing', text: '执行中' },
  success: { color: 'success', text: '成功' },
  failure: { color: 'error', text: '失败' },
};

const sourceText = (source?: string) =>
  source === 'shell' ? 'SSH 网关 /ask-ai' : source === 'web' ? '网页对话' : source || '-';

/**
 * 工具返回内容 / 工具入参：交给 `JsonCards` 摊成卡片，纯文本才按原文显示。
 *
 * 工具（尤其是 `list_ai_tools` 这类元工具）返回的就是结构化数据，直接糊一坨 JSON
 * 既看不出来也看不下去；卡片只是**换了个更好读的排法**，`JsonCards` 里始终留着
 * 「原始 JSON」入口，审计要复核原文时不会丢东西。
 *
 * @param inline 放在表格单元格/气泡里（限宽限高、不留原始 JSON 入口，详情抽屉里再看原文）
 */
const PayloadBlock = ({ value, inline = false }: { value?: unknown; inline?: boolean }) => {
  if (value === null || value === undefined || value === '') {
    return <Text type="secondary">-</Text>;
  }
  if (!inline) return <JsonCards value={value} />;
  return (
    <div style={{ maxWidth: 560, maxHeight: 460, overflow: 'auto' }}>
      <JsonCards value={value} compact />
    </div>
  );
};

/**
 * `role="tool"` 的消息正文是 `[工具 名字] 成功：摘要\n<JSON>`（JSON 可能被后端按字符截断）：
 * 给人看的那一行留在外面，载荷交给卡片，几 KB 的 JSON 不再直接糊在时间线上。
 */
const ToolMessage = ({ content }: { content?: string | null }) => {
  const { head, payloadText, json } = splitToolMessage(content ?? '');
  if (!payloadText) {
    return (
      <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{head || '（空）'}</Paragraph>
    );
  }
  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      <Text>{head}</Text>
      {json ? (
        <JsonCards value={payloadText} />
      ) : (
        <Paragraph
          style={{ whiteSpace: 'pre-wrap', marginBottom: 0, fontSize: 12 }}
          ellipsis={{ rows: 4, expandable: true, symbol: '展开原文' }}
        >
          {payloadText}
        </Paragraph>
      )}
    </Space>
  );
};

const AiAuditsPage = () => {
  const access = useAccess();
  const [tab, setTab] = useState('conversations');
  const conversationRef = useRef<ActionType | undefined>(undefined);
  const toolRef = useRef<ActionType | undefined>(undefined);

  const [detail, setDetail] = useState<AiConversationRecord>();
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);

  const openDetail = useCallback(async (id: number) => {
    setDetailOpen(true);
    setDetailLoading(true);
    try {
      setDetail(await getAiConversation(id));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const conversationColumns: ProColumns<AiConversationRecord>[] = [
    {
      title: '对话',
      dataIndex: 'title',
      ellipsis: true,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{row.title || '未命名对话'}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            #{row.id} · {sourceText(row.source)} · {row.model || '未知模型'}
          </Text>
        </Space>
      ),
    },
    {
      title: '发起人',
      dataIndex: 'username',
      width: 120,
      render: (_, row) => <MonoCell text={row.username} />,
    },
    {
      title: '关联主机',
      dataIndex: 'hostName',
      width: 180,
      render: (_, row) =>
        row.hostName ? (
          <Space direction="vertical" size={0}>
            <Text>{row.hostName}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.hostAddress || '-'}
            </Text>
          </Space>
        ) : (
          '-'
        ),
    },
    {
      title: '消息',
      dataIndex: 'messageCount',
      width: 80,
      render: (_, row) => row.messageCount ?? 0,
    },
    {
      title: '工具调用',
      dataIndex: 'toolCount',
      width: 90,
      render: (_, row) => (
        <Tag color={(row.toolCount ?? 0) > 0 ? 'geekblue' : 'default'}>{row.toolCount ?? 0}</Tag>
      ),
    },
    {
      title: '最近活动',
      dataIndex: 'updatedAt',
      width: 180,
      render: (_, row) => <TimeCell value={row.updatedAt} />,
    },
    {
      title: '操作',
      valueType: 'option',
      width: 90,
      render: (_, row) => [
        <a key="detail" onClick={() => void openDetail(row.id)}>
          查看详情
        </a>,
      ],
    },
  ];

  const toolColumns: ProColumns<AiToolCallRecord>[] = [
    { title: '时间', dataIndex: 'createdAt', width: 180, render: (_, row) => <TimeCell value={row.createdAt} /> },
    {
      title: '调用人',
      dataIndex: 'username',
      width: 110,
      render: (_, row) => <MonoCell text={row.username} />,
    },
    {
      title: '工具',
      dataIndex: 'toolName',
      width: 200,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{row.toolName}</Text>
          <Space size={4}>
            {row.sensitive ? <Tag color="red">敏感</Tag> : null}
            {row.requiredPermission ? <Tag>{row.requiredPermission}</Tag> : null}
          </Space>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (_, row) => {
        const meta = CALL_STATUS[row.status] ?? { color: 'default', text: row.status };
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: '确认人',
      dataIndex: 'confirmedBy',
      width: 110,
      render: (_, row) => row.confirmedBy || '-',
    },
    {
      title: '入参',
      dataIndex: 'arguments',
      width: 320,
      ellipsis: true,
      render: (_, row) => <PayloadBlock value={row.arguments} inline />,
    },
    {
      title: '结果',
      dataIndex: 'resultSummary',
      ellipsis: true,
      render: (_, row) => (
        <Space direction="vertical" size={0} style={{ maxWidth: 420 }}>
          <Text type={row.error ? 'danger' : undefined}>
            {humanAuditMessage(row.resultSummary) || row.error || '-'}
          </Text>
          <Space size={8}>
            {row.durationMs ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                耗时 {row.durationMs}ms
              </Text>
            ) : null}
            {row.resultPreview ? (
              <Popover
                trigger="click"
                placement="left"
                title="工具返回原文"
                content={<PayloadBlock value={row.resultPreview} inline />}
              >
                <a style={{ fontSize: 12 }}>查看输出</a>
              </Popover>
            ) : null}
          </Space>
        </Space>
      ),
    },
  ];

  const renderMessage = (item: AiMessageRecord) => {
    const meta = ROLE_LABEL[item.role] ?? { color: 'default', text: item.role };
    return (
      <Card
        key={item.id}
        size="small"
        title={
          <Space>
            <Tag color={meta.color}>{meta.text}</Tag>
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              #{item.id} · {item.createdAt || '-'}
            </Text>
          </Space>
        }
        style={{ marginBottom: 12 }}
      >
        {item.reasoning ? (
          <Paragraph type="secondary" style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
            {item.reasoning}
          </Paragraph>
        ) : null}
        {item.role === 'tool' ? (
          <ToolMessage content={item.content} />
        ) : (
          <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
            {item.content || '（空）'}
          </Paragraph>
        )}
        {item.cards && item.cards.length > 0 ? <AiCards cards={item.cards} /> : null}
      </Card>
    );
  };

  return (
    <PageContainer
      title="AI 对话审计"
      subTitle="用户说了什么、AI 回了什么、调用了什么工具——与人工操作同一套留痕口径"
      extra={[
        <Tag key="scope" color={access.canAiViewAll ? 'red' : 'default'}>
          {access.canAiViewAll ? '可查看全部人的对话（ai:view_all）' : '仅可查看自己的对话'}
        </Tag>,
        <Tooltip key="reload" title="刷新">
          <a
            onClick={() => {
              void conversationRef.current?.reload();
              void toolRef.current?.reload();
            }}
          >
            <ReloadOutlined /> 刷新
          </a>
        </Tooltip>,
      ]}
    >
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: 'conversations',
            label: 'AI 对话',
            children: (
              <ProTable<AiConversationRecord>
                rowKey="id"
                actionRef={conversationRef}
                columns={conversationColumns}
                search={{ labelWidth: 'auto' }}
                pagination={{ pageSize: 10, showSizeChanger: true }}
                options={false}
                request={async (params) => {
                  const body = await listAiConversations({
                    page: params.current,
                    pageSize: params.pageSize,
                    keyword: params.title as string | undefined,
                    username: params.username as string | undefined,
                  });
                  return { data: body.data ?? [], success: true, total: body.total ?? 0 };
                }}
              />
            ),
          },
          {
            key: 'tools',
            label: '工具调用',
            children: (
              <ProTable<AiToolCallRecord>
                rowKey="id"
                actionRef={toolRef}
                columns={toolColumns}
                search={{ labelWidth: 'auto' }}
                pagination={{ pageSize: 10, showSizeChanger: true }}
                options={false}
                request={async (params) => {
                  const body = await listAiToolCalls({
                    page: params.current,
                    pageSize: params.pageSize,
                    toolName: params.toolName as string | undefined,
                    status: params.status as string | undefined,
                    sensitive:
                      params.sensitive === undefined ? undefined : Boolean(params.sensitive),
                  });
                  return { data: body.data ?? [], success: true, total: body.total ?? 0 };
                }}
              />
            ),
          },
        ]}
      />

      <Drawer
        title={detail ? `对话详情 · ${detail.title || `#${detail.id}`}` : '对话详情'}
        width={880}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
      >
        {detailLoading ? (
          <div style={{ padding: 32, textAlign: 'center' }}>
            <Spin />
          </div>
        ) : !detail ? (
          <Empty description="没有取到对话内容" />
        ) : (
          <>
            <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label="对话 ID">{detail.id}</Descriptions.Item>
              <Descriptions.Item label="发起人">{detail.username || '-'}</Descriptions.Item>
              <Descriptions.Item label="来源">{sourceText(detail.source)}</Descriptions.Item>
              <Descriptions.Item label="模型">{detail.model || '-'}</Descriptions.Item>
              <Descriptions.Item label="关联主机">
                {detail.hostName ? `${detail.hostName}（${detail.hostAddress || '-'}）` : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="会话 ID">{detail.sid || '-'}</Descriptions.Item>
              <Descriptions.Item label="创建时间">{detail.createdAt || '-'}</Descriptions.Item>
              <Descriptions.Item label="最近活动">{detail.updatedAt || '-'}</Descriptions.Item>
            </Descriptions>
            {(detail.messages ?? []).length === 0 ? (
              <Alert type="info" showIcon message="这条对话还没有落库的消息" />
            ) : (
              (detail.messages ?? []).map(renderMessage)
            )}
            {(detail.toolCalls ?? []).length > 0 ? (
              <>
                <Paragraph strong style={{ marginTop: 16 }}>
                  本次对话的工具调用
                </Paragraph>
                <Table<AiToolCallRecord>
                  size="small"
                  rowKey="id"
                  pagination={false}
                  dataSource={detail.toolCalls ?? []}
                  columns={[
                    { title: '工具', dataIndex: 'toolName', width: 160 },
                    {
                      title: '状态',
                      dataIndex: 'status',
                      width: 100,
                      render: (value: string) => {
                        const meta = CALL_STATUS[value] ?? { color: 'default', text: value };
                        return <Tag color={meta.color}>{meta.text}</Tag>;
                      },
                    },
                    { title: '确认人', dataIndex: 'confirmedBy', width: 100, render: (v: string) => v || '-' },
                    {
                      title: '入参',
                      dataIndex: 'arguments',
                      width: 320,
                      render: (v: unknown) => <PayloadBlock value={v} inline />,
                    },
                    {
                      title: '结果',
                      dataIndex: 'resultSummary',
                      render: (v: string, row: AiToolCallRecord) => (
                        <Space direction="vertical" size={4} style={{ width: '100%' }}>
                          <Text type={row.error ? 'danger' : undefined}>
                            {humanAuditMessage(v) || row.error || '-'}
                          </Text>
                          {row.resultPreview ? <PayloadBlock value={row.resultPreview} /> : null}
                        </Space>
                      ),
                    },
                  ]}
                />
              </>
            ) : null}
          </>
        )}
      </Drawer>
    </PageContainer>
  );
};

export default AiAuditsPage;
