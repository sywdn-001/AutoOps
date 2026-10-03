/**
 * 审计中心 · 会话记录
 * 展示会话列表、当前在线会话，并提供会话详情（概要 / 命令记录 / 录像回放）与中断操作。
 */

import type {
  ActionType,
  ProColumns,
  ProFormInstance,
} from '@ant-design/pro-components';
import { PageContainer, ProTable } from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import type { TableProps } from 'antd';
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tabs,
  Typography,
} from 'antd';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActionTag,
  CopyText,
  MonoCell,
  RiskTag,
  SessionSourceTag,
  SessionStatusTag,
  TimeCell,
} from '@/components/Bastion';
import {
  formatBytes,
  formatDuration,
  formatLatency,
  RISK_OPTIONS,
  SESSION_SOURCE_META,
  SESSION_STATUS_META,
  toOptions,
} from '@/services/bastion/constants';
import type { PurgePayload } from '@/services/bastion/endpoints';
import { hostApi, sessionApi } from '@/services/bastion/endpoints';
import type { CommandItem, SessionItem } from '@/services/bastion/types';

/** 与后端 `_session_query_with` 同名：清除时只透传这些筛选字段。 */
const PURGE_FILTER_KEYS = [
  'keyword',
  'username',
  'hostId',
  'source',
  'status',
] as const;

const { Text } = Typography;

/** 会话来源候选项（文案取自常量表，保持单一来源） */
const SOURCE_FILTER_OPTIONS = [
  { label: SESSION_SOURCE_META.gateway.text, value: 'gateway' },
  { label: SESSION_SOURCE_META.web.text, value: 'web' },
];

/** 会话状态候选项 */
const STATUS_FILTER_OPTIONS = [
  { label: SESSION_STATUS_META.active.text, value: 'active' },
  { label: SESSION_STATUS_META.closed.text, value: 'closed' },
  { label: SESSION_STATUS_META.failed.text, value: 'failed' },
  { label: SESSION_STATUS_META.terminated.text, value: 'terminated' },
  { label: SESSION_STATUS_META.denied.text, value: 'denied' },
];

/** 在线会话表格列 */
const onlineColumns: TableProps<SessionItem>['columns'] = [
  {
    title: '会话号',
    dataIndex: 'sid',
    width: 200,
    render: (_, record) => <CopyText text={record.sid} />,
  },
  { title: '用户', dataIndex: 'username', width: 120 },
  { title: '主机', dataIndex: 'hostName', width: 160 },
  { title: '地址', dataIndex: 'hostAddress', width: 150 },
  {
    title: '来源',
    dataIndex: 'source',
    width: 120,
    render: (_, record) => <SessionSourceTag source={record.source} />,
  },
  { title: '客户端 IP', dataIndex: 'clientIp', width: 150 },
  {
    title: '开始时间',
    dataIndex: 'startedAt',
    width: 180,
    render: (_, record) => <TimeCell value={record.startedAt} />,
  },
  { title: '命令数', dataIndex: 'commandCount', width: 90 },
  {
    title: '拦截数',
    dataIndex: 'deniedCount',
    width: 90,
    render: (_, record) =>
      record.deniedCount > 0 ? (
        <Text type="danger">{record.deniedCount}</Text>
      ) : (
        record.deniedCount
      ),
  },
];

/** 会话详情内「命令记录」表格列 */
const sessionCommandColumns: ProColumns<CommandItem>[] = [
  { title: '序号', dataIndex: 'seq', width: 70 },
  {
    title: '命令',
    dataIndex: 'command',
    width: 320,
    render: (_, record) => <MonoCell text={record.command} max={120} />,
  },
  {
    title: '动作',
    dataIndex: 'action',
    width: 90,
    render: (_, record) => <ActionTag action={record.action} />,
  },
  {
    title: '风险',
    dataIndex: 'riskLevel',
    width: 90,
    render: (_, record) => <RiskTag level={record.riskLevel} />,
  },
  {
    title: '命中规则',
    dataIndex: 'matchedRulePattern',
    width: 220,
    render: (_, record) => (
      <MonoCell text={record.matchedRulePattern} max={60} />
    ),
  },
  {
    title: '耗时',
    dataIndex: 'durationMs',
    width: 100,
    render: (_, record) => formatLatency(record.durationMs),
  },
  {
    title: '执行时间',
    dataIndex: 'startedAt',
    width: 180,
    render: (_, record) => <TimeCell value={record.startedAt} />,
  },
];

/** 会话详情内「命令记录」表格（按会话分页拉取） */
const SessionCommandsTable: React.FC<{ sessionId: number }> = ({
  sessionId,
}) => (
  <ProTable<CommandItem>
    rowKey="id"
    size="small"
    search={false}
    options={false}
    pagination={{ defaultPageSize: 10 }}
    scroll={{ x: 'max-content' }}
    request={async (params) => {
      const res = await sessionApi.commands(sessionId, params);
      return { data: res.data, total: res.total, success: res.success };
    }}
    columns={sessionCommandColumns}
  />
);

type TranscriptResult = Awaited<ReturnType<typeof sessionApi.transcript>>;
type TranscriptEvent = TranscriptResult['events'][number];

const TRANSCRIPT_BATCH = 800;

/** 录制事件类型（后端 ``t`` 字段）→ 中文标签 */
const TRANSCRIPT_LABEL: Record<string, string> = {
  session_start: '会话开始',
  session_end: '会话结束',
  command: '命令',
  deny: '命令被拦截',
  output: '输出',
  input: '输入',
  interactive_input: '交互输入',
  notice: '提示',
  resize: '窗口调整',
};

/** 渲染单条回放事件（终端风格时间线） */
const renderTranscriptEvent = (event: TranscriptEvent, index: number) => {
  const type = event.t ?? '';
  const key = `${type}-${event.seq ?? index}-${index}`;
  if (type === 'command' || type === 'deny') {
    const denied = type === 'deny' || event.action === 'deny';
    return (
      <div
        key={key}
        style={{
          color: denied ? '#ff7875' : '#d6e1ff',
          fontWeight: denied ? 600 : 400,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
        }}
      >
        <span style={{ color: '#58a6ff' }}>$ </span>
        {event.command ?? ''}
        {denied ? (
          <span style={{ marginLeft: 8 }}>
            [已拦截{event.reason ? `：${event.reason}` : ''}]
          </span>
        ) : null}
      </div>
    );
  }
  if (type === 'output' || type === 'input' || type === 'interactive_input') {
    return (
      <div
        key={key}
        style={{
          color: type === 'output' ? '#c9d5f0' : '#8aa0c8',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
        }}
      >
        {event.data ?? ''}
      </div>
    );
  }
  if (type === 'session_start') {
    return (
      <div key={key} style={{ color: '#7ee787' }}>
        —— 会话开始{event.host ? `（${event.host}）` : ''} ——
      </div>
    );
  }
  if (type === 'session_end') {
    return (
      <div key={key} style={{ color: '#7ee787' }}>
        —— 会话结束{event.reason ? `：${event.reason}` : ''} ——
      </div>
    );
  }
  if (type === 'resize') {
    return (
      <div key={key} style={{ color: '#8aa0c8' }}>
        [窗口调整 {event.cols ?? '?'}×{event.rows ?? '?'}]
      </div>
    );
  }
  return (
    <div key={key} style={{ color: '#8aa0c8' }}>
      {event.message ??
        event.command ??
        event.data ??
        TRANSCRIPT_LABEL[type] ??
        type ??
        '未知事件'}
    </div>
  );
};

/** 会话详情内「录像回放」面板 */
const TranscriptPane: React.FC<{ sessionId: number }> = ({ sessionId }) => {
  const [events, setEvents] = useState<TranscriptEvent[]>([]);
  const [nextOffset, setNextOffset] = useState(0);
  const [eof, setEof] = useState(true);
  const [size, setSize] = useState(0);
  const [stats, setStats] = useState<TranscriptResult['stats']>({});
  const [commandOnly, setCommandOnly] = useState(false);
  const [loading, setLoading] = useState(false);

  const loadTranscript = useCallback(
    async (offset: number, onlyCommand: boolean) => {
      setLoading(true);
      try {
        const res = await sessionApi.transcript(sessionId, {
          offset,
          limit: TRANSCRIPT_BATCH,
          commandOnly: onlyCommand,
        });
        const batch = res.events ?? [];
        setEvents((prev) => (offset === 0 ? batch : [...prev, ...batch]));
        // 后端契约：nextOffset 是下一页应传的 offset（按行计）；eof = 本页没有新进展。
        const next = res.nextOffset ?? offset + batch.length;
        setNextOffset(next);
        setEof(Boolean(res.eof) || next <= offset);
        setSize(res.size ?? 0);
        setStats(res.stats ?? {});
      } catch (error) {
        console.debug('load session transcript failed', error);
      } finally {
        setLoading(false);
      }
    },
    [sessionId],
  );

  useEffect(() => {
    setNextOffset(0);
    setEof(false);
    void loadTranscript(0, commandOnly);
  }, [loadTranscript, commandOnly]);

  const hasMore = !eof && nextOffset > 0;

  return (
    <Space orientation="vertical" size={12} style={{ width: '100%' }}>
      <Space size={16} wrap>
        <Space size={6}>
          <Switch
            size="small"
            checked={commandOnly}
            onChange={setCommandOnly}
          />
          <Text>只看命令</Text>
        </Space>
        <Text type="secondary">
          已加载 {events.length} 条事件 · 命令 {stats?.commands ?? 0} 次 / 拦截{' '}
          {stats?.denied ?? 0} 次 · 录像 {formatBytes(size)}
        </Text>
      </Space>
      <div
        style={{
          background: '#0b1021',
          color: '#d6e1ff',
          borderRadius: 6,
          padding: '12px 16px',
          maxHeight: 520,
          overflow: 'auto',
          fontSize: 12,
          lineHeight: 1.8,
          fontFamily: 'Menlo, Consolas, monospace',
        }}
      >
        {events.length === 0 && !loading ? (
          <span style={{ color: '#8aa0c8' }}>暂无回放事件</span>
        ) : (
          events.map((event, index) => renderTranscriptEvent(event, index))
        )}
      </div>
      <Space size={12}>
        <Button
          size="small"
          loading={loading}
          disabled={!hasMore}
          onClick={() => void loadTranscript(nextOffset, commandOnly)}
        >
          加载更多
        </Button>
        <Text type="secondary">每批加载 {TRANSCRIPT_BATCH} 条</Text>
      </Space>
    </Space>
  );
};

/** 会话详情内「概要」面板 */
const SessionSummary: React.FC<{ item: SessionItem }> = ({ item }) => (
  <Descriptions
    column={2}
    bordered
    size="small"
    items={[
      { key: 'id', label: '会话 ID', children: item.id },
      { key: 'sid', label: '会话号', children: <CopyText text={item.sid} /> },
      { key: 'username', label: '用户', children: item.username },
      { key: 'roleCode', label: '角色', children: item.roleCode },
      { key: 'hostName', label: '主机名称', children: item.hostName },
      { key: 'hostAddress', label: '主机地址', children: item.hostAddress },
      {
        key: 'accountUsername',
        label: '登录账号',
        children: item.accountUsername,
      },
      { key: 'protocol', label: '协议', children: item.protocol },
      {
        key: 'source',
        label: '来源',
        children: <SessionSourceTag source={item.source} />,
      },
      {
        key: 'status',
        label: '状态',
        children: <SessionStatusTag status={item.status} />,
      },
      {
        key: 'clientIp',
        label: '客户端 IP',
        children: item.clientPort
          ? `${item.clientIp}:${item.clientPort}`
          : item.clientIp,
      },
      {
        key: 'startedAt',
        label: '开始时间',
        children: <TimeCell value={item.startedAt} />,
      },
      {
        key: 'endedAt',
        label: '结束时间',
        children: <TimeCell value={item.endedAt} />,
      },
      {
        key: 'durationSeconds',
        label: '时长',
        children: formatDuration(item.durationSeconds),
      },
      { key: 'commandCount', label: '命令数', children: item.commandCount },
      { key: 'deniedCount', label: '拦截数', children: item.deniedCount },
      {
        key: 'bytes',
        label: '流量（↑ 上行 / ↓ 下行）',
        children: `↑ ${formatBytes(item.bytesIn)} / ↓ ${formatBytes(item.bytesOut)}`,
      },
      { key: 'endReason', label: '结束原因', children: item.endReason || '-' },
      {
        key: 'riskLevel',
        label: '风险等级',
        children: <RiskTag level={item.riskLevel} />,
      },
      {
        key: 'hasTranscript',
        label: '录像回放',
        children: item.hasTranscript ? '可用' : '无',
      },
    ]}
  />
);

const SessionsPage: React.FC = () => {
  const access = useAccess();
  const { message } = App.useApp();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const [detail, setDetail] = useState<SessionItem | undefined>(undefined);
  const [onlineSessions, setOnlineSessions] = useState<SessionItem[]>([]);
  const [onlineLoading, setOnlineLoading] = useState(false);
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [total, setTotal] = useState(0);
  const [purging, setPurging] = useState(false);
  const [hostOptions, setHostOptions] = useState<
    { label: string; value: unknown }[]
  >([]);

  const currentFilters = useCallback((): PurgePayload => {
    const values = (formRef.current?.getFieldsValue?.() ?? {}) as Record<
      string,
      unknown
    >;
    const payload: PurgePayload = {};
    for (const key of PURGE_FILTER_KEYS) {
      const value = values[key];
      if (value !== undefined && value !== null && value !== '') {
        payload[key] = value;
      }
    }
    return payload;
  }, []);

  const runPurge = useCallback(
    async (payload: PurgePayload) => {
      setPurging(true);
      try {
        const result = await sessionApi.purge(payload);
        const skipped = result.skippedActive ?? 0;
        message.success(
          `已清除 ${result.deleted} 条会话记录（命令 ${result.commands ?? 0} 条、录像 ${result.transcripts ?? 0} 个）${
            skipped > 0 ? `；跳过 ${skipped} 条进行中的会话` : ''
          }`,
        );
        setSelectedKeys([]);
        actionRef.current?.reload();
      } catch (error) {
        console.debug('purge sessions failed', error);
      } finally {
        setPurging(false);
      }
    },
    [message],
  );

  const loadOnline = useCallback(async () => {
    setOnlineLoading(true);
    try {
      const rows = await sessionApi.online();
      setOnlineSessions(rows ?? []);
    } catch (error) {
      console.debug('load online sessions failed', error);
    } finally {
      setOnlineLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadHostOptions = async () => {
      try {
        const rows = await hostApi.options();
        if (!cancelled) {
          setHostOptions(toOptions(rows));
        }
      } catch (error) {
        console.debug('load host options failed', error);
      }
    };
    void loadHostOptions();
    void loadOnline();
    return () => {
      cancelled = true;
    };
  }, [loadOnline]);

  const terminateSession = useCallback(
    async (id: number) => {
      try {
        await sessionApi.terminate(id);
        message.success('已中断该会话');
        actionRef.current?.reload();
        void loadOnline();
      } catch (error) {
        console.debug('terminate session failed', error);
      }
    },
    [loadOnline, message],
  );

  const columns: ProColumns<SessionItem>[] = [
    {
      title: '会话号',
      dataIndex: 'sid',
      width: 200,
      search: false,
      render: (_, record) => <CopyText text={record.sid} />,
    },
    {
      title: '用户',
      dataIndex: 'username',
      width: 120,
      fieldProps: { placeholder: '用户名' },
    },
    { title: '主机', dataIndex: 'hostName', width: 160, search: false },
    { title: '地址', dataIndex: 'hostAddress', width: 150, search: false },
    {
      title: '来源',
      dataIndex: 'source',
      width: 120,
      valueType: 'select',
      fieldProps: { options: SOURCE_FILTER_OPTIONS, placeholder: '全部来源' },
      render: (_, record) => <SessionSourceTag source={record.source} />,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      valueType: 'select',
      fieldProps: { options: STATUS_FILTER_OPTIONS, placeholder: '全部状态' },
      render: (_, record) => <SessionStatusTag status={record.status} />,
    },
    {
      title: '开始时间',
      dataIndex: 'startedAt',
      width: 180,
      search: false,
      render: (_, record) => <TimeCell value={record.startedAt} />,
    },
    {
      title: '时长',
      dataIndex: 'durationSeconds',
      width: 100,
      search: false,
      render: (_, record) => formatDuration(record.durationSeconds),
    },
    {
      title: '命令数',
      dataIndex: 'commandCount',
      width: 90,
      search: false,
    },
    {
      title: '拦截数',
      dataIndex: 'deniedCount',
      width: 90,
      search: false,
      render: (_, record) =>
        record.deniedCount > 0 ? (
          <Text type="danger">{record.deniedCount}</Text>
        ) : (
          record.deniedCount
        ),
    },
    {
      title: '流量',
      key: 'traffic',
      width: 180,
      search: false,
      render: (_, record) =>
        `↑ ${formatBytes(record.bytesIn)} / ↓ ${formatBytes(record.bytesOut)}`,
    },
    {
      title: '风险',
      dataIndex: 'riskLevel',
      width: 100,
      valueType: 'select',
      fieldProps: { options: RISK_OPTIONS, placeholder: '全部风险等级' },
      render: (_, record) => <RiskTag level={record.riskLevel} />,
    },
    {
      title: '关键字',
      dataIndex: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: '会话号 / 命令 / 主机' },
    },
    {
      title: '主机筛选',
      dataIndex: 'hostId',
      hideInTable: true,
      valueType: 'select',
      fieldProps: {
        options: hostOptions,
        showSearch: true,
        optionFilterProp: 'label',
        allowClear: true,
        placeholder: '请选择主机',
      },
    },
    {
      title: '操作',
      valueType: 'option',
      width: 120,
      fixed: 'right',
      render: (_, record) => [
        <Button
          key="detail"
          type="link"
          size="small"
          onClick={() => setDetail(record)}
        >
          详情
        </Button>,
        access.canSessionTerminate && record.status === 'active' ? (
          <Popconfirm
            key="terminate"
            title="确认中断该会话？"
            description="中断后该会话的转发通道会立即关闭。"
            okText="中断"
            cancelText="取消"
            onConfirm={() => void terminateSession(record.id)}
          >
            <Button type="link" size="small" danger>
              中断
            </Button>
          </Popconfirm>
        ) : null,
      ],
    },
  ];

  return (
    <PageContainer>
      {access.canSessionViewAll ? null : (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          title="你只能看到自己有权限范围内的会话"
          description="如需查看全部会话，请联系管理员授予 session:view_all 权限。"
        />
      )}
      <Card
        title="当前在线会话"
        style={{ marginBottom: 16 }}
        styles={{ body: { padding: 0 } }}
        extra={
          <Space size={8}>
            <Text type="secondary">建议每 30 秒手动刷新一次</Text>
            <Button
              size="small"
              loading={onlineLoading}
              onClick={() => void loadOnline()}
            >
              刷新
            </Button>
          </Space>
        }
      >
        <Table<SessionItem>
          rowKey="id"
          size="small"
          loading={onlineLoading}
          dataSource={onlineSessions}
          columns={onlineColumns}
          pagination={false}
          scroll={{ x: 'max-content' }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="当前没有在线会话"
              />
            ),
          }}
        />
      </Card>
      <ProTable<SessionItem>
        rowKey="id"
        headerTitle="会话记录"
        actionRef={actionRef}
        formRef={formRef}
        columns={columns}
        search={{ labelWidth: 'auto' }}
        pagination={{ defaultPageSize: 10 }}
        scroll={{ x: 'max-content' }}
        rowSelection={
          access.canAdmin
            ? {
                selectedRowKeys: selectedKeys,
                onChange: setSelectedKeys,
                preserveSelectedRowKeys: true,
              }
            : undefined
        }
        request={async (params) => {
          const res = await sessionApi.list(params);
          setTotal(res.total ?? 0);
          return { data: res.data, total: res.total, success: res.success };
        }}
        toolBarRender={() => [
          <Button key="refresh" onClick={() => actionRef.current?.reload()}>
            刷新
          </Button>,
          access.canAdmin ? (
            <Popconfirm
              key="purge-selected"
              title="删除选中的会话记录？"
              description={`将连同该会话的命令日志与录像文件一起永久删除（${selectedKeys.length} 条），无法恢复；进行中的会话会被跳过。`}
              okText="删除"
              okButtonProps={{ danger: true }}
              disabled={selectedKeys.length === 0}
              onConfirm={() =>
                runPurge({ ids: selectedKeys.map((key) => Number(key)) })
              }
            >
              <Button
                danger
                disabled={selectedKeys.length === 0}
                loading={purging}
              >
                删除选中
                {selectedKeys.length > 0 ? `（${selectedKeys.length}）` : ''}
              </Button>
            </Popconfirm>
          ) : null,
          access.canAdmin ? (
            <Popconfirm
              key="purge-filtered"
              title="清除当前筛选结果？"
              description={
                <>
                  将按当前搜索条件永久删除匹配的全部会话（当前共 {total}{' '}
                  条）及其命令日志与录像文件，无法恢复。未设置筛选条件时等同于清空全部；
                  进行中的会话会被跳过（请先中断）。
                  <br />
                  清除动作本身会写入一条审计留痕。
                </>
              }
              okText="清除"
              okButtonProps={{ danger: true }}
              onConfirm={() => runPurge({ all: true, ...currentFilters() })}
            >
              <Button danger type="primary" ghost loading={purging}>
                清除筛选结果
              </Button>
            </Popconfirm>
          ) : null,
        ]}
      />
      <Drawer
        width={1100}
        open={Boolean(detail)}
        title={detail ? `会话详情 · ${detail.sid}` : '会话详情'}
        onClose={() => setDetail(undefined)}
      >
        {detail ? (
          <Tabs
            items={[
              {
                key: 'summary',
                label: '概要',
                children: <SessionSummary item={detail} />,
              },
              {
                key: 'commands',
                label: '命令记录',
                children: <SessionCommandsTable sessionId={detail.id} />,
              },
              {
                key: 'transcript',
                label: '录像回放',
                children: <TranscriptPane sessionId={detail.id} />,
              },
            ]}
          />
        ) : null}
      </Drawer>
    </PageContainer>
  );
};

export default SessionsPage;
