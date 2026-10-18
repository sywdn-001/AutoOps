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
import { t } from '@/i18n';
import {
  formatBytes,
  formatDuration,
  formatLatency,
  humanizeEndReason,
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
    title: t('会话号'),
    dataIndex: 'sid',
    width: 200,
    render: (_, record) => <CopyText text={record.sid} />,
  },
  { title: t('用户'), dataIndex: 'username', width: 120 },
  { title: t('主机'), dataIndex: 'hostName', width: 160 },
  { title: t('地址'), dataIndex: 'hostAddress', width: 150 },
  {
    title: t('来源'),
    dataIndex: 'source',
    width: 120,
    render: (_, record) => <SessionSourceTag source={record.source} />,
  },
  { title: t('客户端 IP'), dataIndex: 'clientIp', width: 150 },
  {
    title: t('开始时间'),
    dataIndex: 'startedAt',
    width: 180,
    render: (_, record) => <TimeCell value={record.startedAt} />,
  },
  { title: t('命令数'), dataIndex: 'commandCount', width: 90 },
  {
    title: t('拦截数'),
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
  { title: t('序号'), dataIndex: 'seq', width: 70 },
  {
    title: t('命令'),
    dataIndex: 'command',
    width: 320,
    render: (_, record) => <MonoCell text={record.command} max={120} />,
  },
  {
    title: t('动作'),
    dataIndex: 'action',
    width: 90,
    render: (_, record) => <ActionTag action={record.action} />,
  },
  {
    title: t('风险'),
    dataIndex: 'riskLevel',
    width: 90,
    render: (_, record) => <RiskTag level={record.riskLevel} />,
  },
  {
    title: t('命中规则'),
    dataIndex: 'matchedRulePattern',
    width: 220,
    render: (_, record) => (
      <MonoCell text={record.matchedRulePattern} max={60} />
    ),
  },
  {
    title: t('耗时'),
    dataIndex: 'durationMs',
    width: 100,
    render: (_, record) => formatLatency(record.durationMs),
  },
  {
    title: t('执行时间'),
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
  session_start: t('会话开始'),
  session_end: t('会话结束'),
  command: '命令',
  deny: t('命令被拦截'),
  output: t('输出'),
  input: t('输入'),
  interactive_input: t('交互输入'),
  notice: t('提示'),
  resize: t('窗口调整'),
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
            {t('[已拦截{0}]', { 0: event.reason ? `：${event.reason}` : '' })}
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
        {t('—— 会话开始{0} ——', { 0: event.host ? `（${event.host}）` : '' })}
      </div>
    );
  }
  if (type === 'session_end') {
    return (
      <div key={key} style={{ color: '#7ee787' }}>
        {t('—— 会话结束{0} ——', { 0: event.reason ? `：${event.reason}` : '' })}
      </div>
    );
  }
  if (type === 'resize') {
    return (
      <div key={key} style={{ color: '#8aa0c8' }}>
        {t('[窗口调整 {0}×{1}]', {
          0: event.cols ?? '?',
          1: event.rows ?? '?',
        })}
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
        t('未知事件')}
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
          <Text>{t('只看命令')}</Text>
        </Space>
        <Text type="secondary">
          {t('已加载 {0} 条事件 · 命令 {1} 次 / 拦截{2} {3} 次 · 录像 {4}', {
            0: events.length,
            1: stats?.commands ?? 0,
            2: ' ',
            3: stats?.denied ?? 0,
            4: formatBytes(size),
          })}
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
          <span style={{ color: '#8aa0c8' }}>{t('暂无回放事件')}</span>
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
          {t('加载更多')}
        </Button>
        <Text type="secondary">
          {t('每批加载 {0} 条', { 0: TRANSCRIPT_BATCH })}
        </Text>
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
      { key: 'id', label: t('会话 ID'), children: item.id },
      {
        key: 'sid',
        label: t('会话号'),
        children: <CopyText text={item.sid} />,
      },
      { key: 'username', label: t('用户'), children: item.username },
      { key: 'roleCode', label: t('角色'), children: item.roleCode },
      { key: 'hostName', label: t('主机名称'), children: item.hostName },
      { key: 'hostAddress', label: t('主机地址'), children: item.hostAddress },
      {
        key: 'accountUsername',
        label: t('登录账号'),
        children: item.accountUsername,
      },
      { key: 'protocol', label: t('协议'), children: item.protocol },
      {
        key: 'source',
        label: t('来源'),
        children: <SessionSourceTag source={item.source} />,
      },
      {
        key: 'status',
        label: t('状态'),
        children: <SessionStatusTag status={item.status} />,
      },
      {
        key: 'clientIp',
        label: t('客户端 IP'),
        children: item.clientPort
          ? `${item.clientIp}:${item.clientPort}`
          : item.clientIp,
      },
      {
        key: 'startedAt',
        label: t('开始时间'),
        children: <TimeCell value={item.startedAt} />,
      },
      {
        key: 'endedAt',
        label: t('结束时间'),
        children: <TimeCell value={item.endedAt} />,
      },
      {
        key: 'durationSeconds',
        label: t('时长'),
        children: formatDuration(item.durationSeconds),
      },
      { key: 'commandCount', label: t('命令数'), children: item.commandCount },
      { key: 'deniedCount', label: t('拦截数'), children: item.deniedCount },
      {
        key: 'bytes',
        label: t('流量（↑ 上行 / ↓ 下行）'),
        children: `↑ ${formatBytes(item.bytesIn)} / ↓ ${formatBytes(item.bytesOut)}`,
      },
      {
        key: 'endReason',
        label: t('结束原因'),
        children: humanizeEndReason(item.endReason),
      },
      {
        key: 'riskLevel',
        label: t('风险等级'),
        children: <RiskTag level={item.riskLevel} />,
      },
      {
        key: 'hasTranscript',
        label: t('录像回放'),
        children: item.hasTranscript ? t('可用') : t('无'),
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
          t('已清除 {0} 条会话记录（命令 {1} 条、录像 {2} 个）{3}', {
            0: result.deleted,
            1: result.commands ?? 0,
            2: result.transcripts ?? 0,
            3:
              skipped > 0 ? t('；跳过 {0} 条进行中的会话', { 0: skipped }) : '',
          }),
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
        message.success(t('已中断该会话'));
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
      title: t('会话号'),
      dataIndex: 'sid',
      width: 200,
      search: false,
      render: (_, record) => <CopyText text={record.sid} />,
    },
    {
      title: t('用户'),
      dataIndex: 'username',
      width: 120,
      fieldProps: { placeholder: t('用户名') },
    },
    { title: t('主机'), dataIndex: 'hostName', width: 160, search: false },
    { title: t('地址'), dataIndex: 'hostAddress', width: 150, search: false },
    {
      title: t('来源'),
      dataIndex: 'source',
      width: 120,
      valueType: 'select',
      fieldProps: {
        options: SOURCE_FILTER_OPTIONS,
        placeholder: t('全部来源'),
      },
      render: (_, record) => <SessionSourceTag source={record.source} />,
    },
    {
      title: t('状态'),
      dataIndex: 'status',
      width: 110,
      valueType: 'select',
      fieldProps: {
        options: STATUS_FILTER_OPTIONS,
        placeholder: t('全部状态'),
      },
      render: (_, record) => <SessionStatusTag status={record.status} />,
    },
    {
      title: t('开始时间'),
      dataIndex: 'startedAt',
      width: 180,
      search: false,
      render: (_, record) => <TimeCell value={record.startedAt} />,
    },
    {
      title: t('时长'),
      dataIndex: 'durationSeconds',
      width: 100,
      search: false,
      render: (_, record) => formatDuration(record.durationSeconds),
    },
    {
      title: t('命令数'),
      dataIndex: 'commandCount',
      width: 90,
      search: false,
    },
    {
      title: t('拦截数'),
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
      title: t('流量'),
      key: 'traffic',
      width: 180,
      search: false,
      render: (_, record) =>
        `↑ ${formatBytes(record.bytesIn)} / ↓ ${formatBytes(record.bytesOut)}`,
    },
    {
      title: t('风险'),
      dataIndex: 'riskLevel',
      width: 100,
      valueType: 'select',
      fieldProps: { options: RISK_OPTIONS, placeholder: t('全部风险等级') },
      render: (_, record) => <RiskTag level={record.riskLevel} />,
    },
    {
      title: t('关键字'),
      dataIndex: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: t('会话号 / 命令 / 主机') },
    },
    {
      title: t('主机筛选'),
      dataIndex: 'hostId',
      hideInTable: true,
      valueType: 'select',
      fieldProps: {
        options: hostOptions,
        showSearch: true,
        optionFilterProp: 'label',
        allowClear: true,
        placeholder: t('请选择主机'),
      },
    },
    {
      title: t('操作'),
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
          {t('详情')}
        </Button>,
        access.canSessionTerminate && record.status === 'active' ? (
          <Popconfirm
            key="terminate"
            title={t('确认中断该会话？')}
            description={t('中断后该会话的转发通道会立即关闭。')}
            okText={t('中断')}
            cancelText={t('取消')}
            onConfirm={() => void terminateSession(record.id)}
          >
            <Button type="link" size="small" danger>
              {t('中断')}
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
          title={t('你只能看到自己有权限范围内的会话')}
          description={t(
            '如需查看全部会话，请联系管理员授予 session:view_all 权限。',
          )}
        />
      )}
      <Card
        title={t('当前在线会话')}
        style={{ marginBottom: 16 }}
        styles={{ body: { padding: 0 } }}
        extra={
          <Space size={8}>
            <Text type="secondary">{t('建议每 30 秒手动刷新一次')}</Text>
            <Button
              size="small"
              loading={onlineLoading}
              onClick={() => void loadOnline()}
            >
              {t('刷新')}
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
                description={t('当前没有在线会话')}
              />
            ),
          }}
        />
      </Card>
      <ProTable<SessionItem>
        rowKey="id"
        headerTitle={t('会话记录')}
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
            {t('刷新')}
          </Button>,
          access.canAdmin ? (
            <Popconfirm
              key="purge-selected"
              title={t('删除选中的会话记录？')}
              description={t(
                '将连同该会话的命令日志与录像文件一起永久删除（{0} 条），无法恢复；进行中的会话会被跳过。',
                { 0: selectedKeys.length },
              )}
              okText={t('删除')}
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
                {t('删除选中 {0}', {
                  0:
                    selectedKeys.length > 0 ? `（${selectedKeys.length}）` : '',
                })}
              </Button>
            </Popconfirm>
          ) : null,
          access.canAdmin ? (
            <Popconfirm
              key="purge-filtered"
              title={t('清除当前筛选结果？')}
              description={
                <>
                  {t(
                    '将按当前搜索条件永久删除匹配的全部会话（当前共 {0}{1} 条）及其命令日志与录像文件，无法恢复。未设置筛选条件时等同于清空全部； 进行中的会话会被跳过（请先中断）。',
                    { 0: total, 1: ' ' },
                  )}
                  <br />
                  {t('清除动作本身会写入一条审计留痕。')}
                </>
              }
              okText={t('清除')}
              okButtonProps={{ danger: true }}
              onConfirm={() => runPurge({ all: true, ...currentFilters() })}
            >
              <Button danger type="primary" ghost loading={purging}>
                {t('清除筛选结果')}
              </Button>
            </Popconfirm>
          ) : null,
        ]}
      />
      <Drawer
        width={1100}
        open={Boolean(detail)}
        title={detail ? t('会话详情 · {0}', { 0: detail.sid }) : t('会话详情')}
        onClose={() => setDetail(undefined)}
      >
        {detail ? (
          <Tabs
            items={[
              {
                key: 'summary',
                label: t('概要'),
                children: <SessionSummary item={detail} />,
              },
              {
                key: 'commands',
                label: t('命令记录'),
                children: <SessionCommandsTable sessionId={detail.id} />,
              },
              {
                key: 'transcript',
                label: t('录像回放'),
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
