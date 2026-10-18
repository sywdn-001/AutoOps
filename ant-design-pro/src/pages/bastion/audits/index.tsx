/**
 * 审计中心 · 操作日志
 * 展示登录登出、资产与授权变更、策略变更、用户与角色变更、会话与命令的审计流水。
 */

import type {
  ActionType,
  ProColumns,
  ProFormInstance,
} from '@ant-design/pro-components';
import { PageContainer, ProTable } from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Drawer,
  Grid,
  message,
  Popconfirm,
  Row,
  Skeleton,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  DescParagraph,
  JsonBlock,
  MonoCell,
  TimeCell,
} from '@/components/Bastion';
import { t as i18nT } from '@/i18n';
import { humanAuditMessage } from '@/services/bastion/constants';
import type { PurgePayload } from '@/services/bastion/endpoints';
import { auditApi } from '@/services/bastion/endpoints';
import type {
  AuditChainStatus,
  AuditChainVerifyResult,
  AuditItem,
} from '@/services/bastion/types';

const { Text, Paragraph, Title } = Typography;

type AuditOptionPayload = Awaited<ReturnType<typeof auditApi.options>>;

const RESULT_OPTIONS = [
  { label: i18nT('成功'), value: 'success' },
  { label: i18nT('失败'), value: 'failure' },
];

const PURGE_FILTER_KEYS = [
  'category',
  'action',
  'result',
  'actorUsername',
  'targetType',
  'keyword',
] as const;

// --------------------------------------------------------------------- 中文映射

const CATEGORY_META: Record<string, { text: string; color: string }> = {
  auth: { text: i18nT('认证'), color: 'geekblue' },
  user: { text: i18nT('用户'), color: 'purple' },
  role: { text: i18nT('角色'), color: 'magenta' },
  host: { text: i18nT('资产'), color: 'cyan' },
  hostgroup: { text: i18nT('资产组'), color: 'cyan' },
  host_group: { text: i18nT('资产组'), color: 'cyan' },
  account: { text: i18nT('账号'), color: 'blue' },
  grant: { text: i18nT('授权'), color: 'gold' },
  policy: { text: i18nT('策略'), color: 'volcano' },
  file_policy: { text: i18nT('文件策略'), color: 'volcano' },
  session: { text: i18nT('会话'), color: 'green' },
  command: { text: i18nT('命令'), color: 'lime' },
  file: { text: i18nT('文件'), color: 'orange' },
  audit: { text: i18nT('审计'), color: 'red' },
  gateway: { text: i18nT('网关'), color: 'geekblue' },
  ai: { text: 'AI', color: 'purple' },
  settings: { text: i18nT('设置'), color: 'default' },
  system: { text: i18nT('系统'), color: 'default' },
  purge: { text: i18nT('清理'), color: 'red' },
  delete: { text: i18nT('删除'), color: 'red' },
};

const ACTION_META: Record<string, { text: string; color: string }> = {
  login: { text: i18nT('登录'), color: 'geekblue' },
  logout: { text: i18nT('登出'), color: 'default' },
  create: { text: i18nT('创建'), color: 'green' },
  update: { text: i18nT('更新'), color: 'blue' },
  delete: { text: i18nT('删除'), color: 'red' },
  remove: { text: i18nT('移除'), color: 'red' },
  add: { text: i18nT('添加'), color: 'green' },
  grant: { text: i18nT('授权'), color: 'gold' },
  revoke: { text: i18nT('撤回'), color: 'orange' },
  change: { text: i18nT('变更'), color: 'blue' },
  enable: { text: i18nT('启用'), color: 'green' },
  disable: { text: i18nT('禁用'), color: 'default' },
  connect: { text: i18nT('连接'), color: 'cyan' },
  disconnect: { text: i18nT('断开'), color: 'default' },
  start: { text: i18nT('开始'), color: 'green' },
  stop: { text: i18nT('停止'), color: 'default' },
  exec: { text: i18nT('执行'), color: 'blue' },
  upload: { text: i18nT('上传'), color: 'cyan' },
  download: { text: i18nT('下载'), color: 'cyan' },
  read: { text: i18nT('读取'), color: 'geekblue' },
  write: { text: i18nT('写入'), color: 'geekblue' },
  purge: { text: i18nT('清理'), color: 'red' },
  verify: { text: i18nT('校验'), color: 'green' },
  refresh: { text: i18nT('刷新'), color: 'blue' },
  sync: { text: i18nT('同步'), color: 'purple' },
  request: { text: i18nT('请求'), color: 'geekblue' },
  response: { text: i18nT('响应'), color: 'default' },
  success: { text: i18nT('成功'), color: 'success' },
  failure: { text: i18nT('失败'), color: 'error' },
};

const TABLE_LABEL: Record<'audit_logs' | 'command_logs' | 'file_logs', string> =
  {
    audit_logs: i18nT('操作日志'),
    command_logs: i18nT('命令流水'),
    file_logs: i18nT('文件流水'),
  };

const tagFromMeta = (
  raw: string | null | undefined,
  meta: Record<string, { text: string; color: string }>,
) => {
  if (!raw) return <Tag>-</Tag>;
  const hit = meta[raw];
  if (hit) return <Tag color={hit.color}>{hit.text}</Tag>;
  const slash = raw.indexOf('/');
  if (slash > 0) {
    const prefix = raw.slice(0, slash);
    const suffix = raw.slice(slash + 1);
    const pm = meta[prefix] ?? meta[suffix];
    if (pm) return <Tag color={pm.color}>{pm.text}</Tag>;
  }
  return <Tag>{raw}</Tag>;
};

const renderResult = (result: string) => {
  if (result === 'success') return <Tag color="success">{i18nT('成功')}</Tag>;
  if (result === 'failure') return <Tag color="error">{i18nT('失败')}</Tag>;
  return <Tag>{result || '-'}</Tag>;
};

// --------------------------------------------------------------------- 哈希展示

const shortHash = (hash?: string) => {
  if (!hash) return '-';
  if (hash.length <= 16) return hash;
  return `${hash.slice(0, 8)}…${hash.slice(-8)}`;
};

const HashCell: React.FC<{ value?: string; label?: string }> = ({
  value,
  label,
}) => {
  if (!value) return <Text type="secondary">—</Text>;
  return (
    <Tooltip
      title={
        <Space direction="vertical" size={0} style={{ maxWidth: 520 }}>
          {label ? <Text type="secondary">{label}</Text> : null}
          <code style={{ wordBreak: 'break-all', color: 'inherit' }}>
            {value}
          </code>
        </Space>
      }
    >
      <Text code style={{ letterSpacing: 0.2 }}>
        {shortHash(value)}
      </Text>
    </Tooltip>
  );
};

// --------------------------------------------------------------------- 链徽标

type ChainBadgeState = {
  status: AuditChainStatus | null;
  verify: AuditChainVerifyResult | null;
  loading: boolean;
  verifying: boolean;
  error: string | null;
};

const ChainBadge: React.FC<{
  state: ChainBadgeState;
  onRefresh: () => void;
  onVerify: () => void;
}> = ({ state, onRefresh, onVerify }) => {
  const { xs } = Grid.useBreakpoint();
  const { status, verify, loading, verifying, error } = state;

  const verifyErrors = useMemo(() => {
    if (!verify) return null;
    const firstTable = (
      Object.keys(verify.tables) as Array<keyof typeof verify.tables>
    ).find((k) => !verify.tables[k].ok);
    if (!firstTable) return null;
    const t = verify.tables[firstTable];
    return {
      table: firstTable,
      firstBad: t.firstBad,
      sample: t.errors.slice(0, 3),
    };
  }, [verify]);

  const healthy = status?.healthy ?? false;

  return (
    <Card
      bordered={false}
      style={{
        boxShadow: '0 1px 2px rgba(15, 23, 42, 0.04)',
        borderRadius: 12,
        border: '1px solid #e6f0ff',
        background:
          'linear-gradient(180deg, #f5f9ff 0%, #ffffff 52%, #ffffff 100%)',
        marginBottom: 16,
        overflow: 'hidden',
      }}
      styles={{
        body: {
          padding: xs ? 16 : 20,
          position: 'relative' as const,
        },
      }}
    >
      <div
        style={{
          position: 'absolute',
          right: -60,
          top: -70,
          width: 200,
          height: 200,
          borderRadius: '50%',
          background:
            'radial-gradient(circle at center, rgba(22, 119, 255, 0.07) 0%, rgba(22, 119, 255, 0) 70%)',
          pointerEvents: 'none',
        }}
      />
      <Row
        gutter={[24, 16]}
        align="middle"
        justify="space-between"
        style={{ position: 'relative' }}
      >
        <Col xs={24} md={10} lg={9}>
          <Space size={16} align="center">
            <div
              style={{
                width: 56,
                height: 56,
                borderRadius: 14,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: healthy
                  ? 'linear-gradient(135deg, #1677ff 0%, #69b1ff 100%)'
                  : 'linear-gradient(135deg, #ff4d4f 0%, #ff7875 100%)',
                boxShadow: healthy
                  ? '0 6px 16px rgba(22, 119, 255, 0.28)'
                  : '0 6px 16px rgba(255, 77, 79, 0.24)',
                color: '#fff',
                fontSize: 26,
                fontFamily:
                  'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
                letterSpacing: 1,
              }}
            >
              {healthy ? '✓' : '!'}
            </div>
            <div>
              <Text
                style={{
                  fontSize: 11,
                  letterSpacing: 2,
                  color: healthy ? '#1677ff' : '#cf1322',
                  fontWeight: 600,
                }}
              >
                CHAIN INTEGRITY
              </Text>
              <Title
                level={4}
                style={{
                  margin: '2px 0 0 0',
                  color: healthy ? '#0f172a' : '#cf1322',
                  fontWeight: 700,
                }}
              >
                {healthy
                  ? i18nT('链式哈希 · 完整可信')
                  : i18nT('链式哈希 · 存在异常')}
              </Title>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {i18nT('HMAC-SHA256 · 每表独立挂链 · 可随时校验')}
              </Text>
            </div>
          </Space>
        </Col>

        <Col xs={24} md={14} lg={16}>
          {loading && !status ? (
            <Skeleton active paragraph={{ rows: 2 }} title={false} />
          ) : (
            <Row gutter={[12, 12]}>
              {status
                ? (
                    Object.keys(status.counts) as Array<
                      keyof typeof status.counts
                    >
                  ).map((key) => {
                    const c = status.counts[key];
                    const h = status.heads[key];
                    // 健康度看**链内**空洞：升级前遗留的空哈希行（pendingPrefix）是合法的，不该把链判红
                    const pendingInside = c.pendingInside ?? c.pending ?? 0;
                    const pendingPrefix = c.pendingPrefix ?? 0;
                    const ok =
                      verify?.tables?.[key]?.ok ??
                      (pendingInside === 0 &&
                        Boolean(h.verifiedHead ?? h.lastEntryHash));
                    return (
                      <Col xs={24} sm={8} key={key}>
                        <div
                          style={{
                            height: '100%',
                            padding: '12px 14px',
                            borderRadius: 10,
                            background: '#ffffff',
                            border: `1px solid ${
                              ok ? '#e6f0ff' : 'rgba(255, 77, 79, 0.28)'
                            }`,
                          }}
                        >
                          <Space
                            size={8}
                            align="center"
                            style={{ marginBottom: 6 }}
                          >
                            <Badge
                              status={ok ? 'processing' : 'error'}
                              text={
                                <Text
                                  strong
                                  style={{ fontSize: 13, color: '#0f172a' }}
                                >
                                  {TABLE_LABEL[key]}
                                </Text>
                              }
                            />
                          </Space>
                          <Space
                            size={12}
                            wrap
                            style={{ fontSize: 12, lineHeight: 1.8 }}
                          >
                            <Text type="secondary">
                              {i18nT('总数')} <Text strong>{c.total}</Text>
                            </Text>
                            <Text
                              style={{
                                color:
                                  pendingInside > 0
                                    ? '#cf1322'
                                    : c.hashed === c.total
                                      ? '#1677ff'
                                      : '#d48806',
                              }}
                            >
                              {i18nT('已哈希{0}', { 0: ' ' })}
                              <Text strong>
                                {c.hashed}
                                {c.pending > 0 ? ` / ${c.total}` : ''}
                              </Text>
                            </Text>
                            {pendingPrefix > 0 ? (
                              <Text type="secondary">
                                {i18nT('升级前遗留')}{' '}
                                <Text strong>{pendingPrefix}</Text>
                                {i18nT('{0} 行（无哈希 · 不影响完整性）', {
                                  0: ' ',
                                })}
                              </Text>
                            ) : null}
                          </Space>
                          <div style={{ marginTop: 6 }}>
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              {i18nT('链尾 id {0}', { 0: h.lastId ?? '-' })}
                            </Text>
                            <div style={{ marginTop: 2 }}>
                              <HashCell
                                value={h.verifiedHead ?? h.lastEntryHash}
                              />
                            </div>
                          </div>
                        </div>
                      </Col>
                    );
                  })
                : null}
            </Row>
          )}
        </Col>
      </Row>

      <Row style={{ marginTop: 14, position: 'relative' }} gutter={[12, 12]}>
        <Col xs={24} md={18}>
          {error ? (
            <Alert
              showIcon
              type="warning"
              message={i18nT('链状态读取失败')}
              description={error}
            />
          ) : null}
          {verifyErrors ? (
            <Alert
              showIcon
              type="error"
              message={i18nT('{0} 检出哈希异常', {
                0: TABLE_LABEL[verifyErrors.table],
              })}
              description={
                <Space direction="vertical" size={4}>
                  <Text>
                    {i18nT('首条坏记录 id ={0}', { 0: ' ' })}
                    <Text code>{verifyErrors.firstBad ?? '-'}</Text>
                  </Text>
                  {verifyErrors.sample.length > 0 ? (
                    <ul
                      style={{
                        margin: 0,
                        paddingLeft: 18,
                        fontSize: 12,
                        color: '#1f2937',
                      }}
                    >
                      {verifyErrors.sample.map((e) => (
                        <li key={`${e.id ?? 'anchor'}-${e.reason}`}>
                          <Text code>#{e.id ?? i18nT('锚点')}</Text> —{' '}
                          {e.reason}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </Space>
              }
            />
          ) : null}
          {verify?.ok && !verifyErrors ? (
            <Alert
              showIcon
              type="info"
              message={i18nT('全表校验通过')}
              description={i18nT(
                '三张流水表的链式哈希重算全部自洽，未发现字段篡改、哈希清空或链内断裂。注意链的固有边界：整段删尾行在库内是自洽的，要抓它得比对库外锚点（CLI --print-head / --expect-head）。',
              )}
            />
          ) : null}
        </Col>
        <Col xs={24} md={6} style={{ textAlign: xs ? 'left' : 'right' }}>
          <Space size={8} wrap>
            <Button onClick={onRefresh} loading={loading}>
              {i18nT('刷新状态')}
            </Button>
            <Button
              type="primary"
              onClick={onVerify}
              loading={verifying}
              style={{
                border: 'none',
                boxShadow: '0 6px 14px rgba(22, 119, 255, 0.22)',
              }}
            >
              {i18nT('逐行校验哈希')}
            </Button>
          </Space>
        </Col>
      </Row>
    </Card>
  );
};

// --------------------------------------------------------------------- 页面主体

const AuditsPage: React.FC = () => {
  const access = useAccess();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const [auditOptions, setAuditOptions] = useState<
    AuditOptionPayload | undefined
  >(undefined);
  const [detail, setDetail] = useState<AuditItem | undefined>(undefined);
  const [detailLoading, setDetailLoading] = useState(false);
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [total, setTotal] = useState(0);
  const [purging, setPurging] = useState(false);

  const [chain, setChain] = useState<ChainBadgeState>({
    status: null,
    verify: null,
    loading: true,
    verifying: false,
    error: null,
  });

  const loadChainStatus = useCallback(async () => {
    setChain((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await auditApi.chainStatus();
      setChain((s) => ({ ...s, status: data, loading: false }));
    } catch (e) {
      setChain((s) => ({
        ...s,
        loading: false,
        error: (e as Error)?.message ?? i18nT('读取链状态失败'),
      }));
    }
  }, []);

  const runVerify = useCallback(async () => {
    setChain((s) => ({ ...s, verifying: true }));
    try {
      const data = await auditApi.chainVerify();
      setChain((s) => ({ ...s, verify: data, verifying: false }));
      if (data.ok) message.success(i18nT('三张流水表哈希校验全部通过'));
      else message.warning(i18nT('检出哈希异常，详情见上方卡片'));
    } catch (e) {
      setChain((s) => ({ ...s, verifying: false }));
      message.error(
        i18nT('哈希校验失败：{0}', {
          0: (e as Error)?.message ?? i18nT('未知错误'),
        }),
      );
    }
  }, []);

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
        const result = await auditApi.purge(payload);
        message.success(i18nT('已清除 {0} 条审计日志', { 0: result.deleted }));
        setSelectedKeys([]);
        actionRef.current?.reload();
        void loadChainStatus();
      } catch (error) {
        console.debug('purge audits failed', error);
      } finally {
        setPurging(false);
      }
    },
    [loadChainStatus],
  );

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [opt] = await Promise.all([
          auditApi.options(),
          loadChainStatus(),
        ]);
        if (!cancelled) setAuditOptions(opt);
      } catch (e) {
        console.debug('load audit options failed', e);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [loadChainStatus]);

  const openDetail = useCallback(async (id: number) => {
    setDetailLoading(true);
    setDetail(undefined);
    try {
      const item = await auditApi.get(id);
      setDetail(item);
    } catch (error) {
      console.debug('load audit detail failed', error);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const categoryOptions =
    auditOptions?.categories.map((item) => ({
      label: CATEGORY_META[item]?.text ?? item,
      value: item,
    })) ?? [];
  const actionOptions =
    auditOptions?.actions.map((item) => ({
      label: ACTION_META[item]?.text ?? item,
      value: item,
    })) ?? [];

  const columns: ProColumns<AuditItem>[] = [
    {
      title: i18nT('时间'),
      dataIndex: 'ts',
      width: 180,
      search: false,
      render: (_, record) => <TimeCell value={record.ts} />,
    },
    categoryOptions.length > 0
      ? {
          title: i18nT('类别'),
          dataIndex: 'category',
          width: 140,
          valueType: 'select',
          fieldProps: {
            options: categoryOptions,
            placeholder: i18nT('全部类别'),
            showSearch: true,
            optionFilterProp: 'label',
          },
          render: (_, record) => tagFromMeta(record.category, CATEGORY_META),
        }
      : {
          title: i18nT('类别'),
          dataIndex: 'category',
          width: 140,
          fieldProps: { placeholder: i18nT('类别') },
          render: (_, record) => tagFromMeta(record.category, CATEGORY_META),
        },
    actionOptions.length > 0
      ? {
          title: i18nT('动作'),
          dataIndex: 'action',
          width: 130,
          valueType: 'select',
          fieldProps: {
            options: actionOptions,
            placeholder: i18nT('全部动作'),
            showSearch: true,
            optionFilterProp: 'label',
          },
          render: (_, record) => tagFromMeta(record.action, ACTION_META),
        }
      : {
          title: i18nT('动作'),
          dataIndex: 'action',
          width: 130,
          fieldProps: { placeholder: i18nT('动作') },
          render: (_, record) => tagFromMeta(record.action, ACTION_META),
        },
    {
      title: i18nT('操作人'),
      dataIndex: 'actorUsername',
      width: 170,
      fieldProps: { placeholder: i18nT('操作人') },
      render: (_, record) =>
        record.actorRole
          ? `${record.actorUsername}（${record.actorRole}）`
          : record.actorUsername,
    },
    {
      title: i18nT('目标'),
      key: 'target',
      width: 220,
      search: false,
      render: (_, record) => (
        <span>
          {record.targetName || '-'}
          {record.targetType ? (
            <Text type="secondary"> · {record.targetType}</Text>
          ) : null}
        </span>
      ),
    },
    {
      title: i18nT('目标类型'),
      dataIndex: 'targetType',
      hideInTable: true,
      fieldProps: { placeholder: i18nT('目标类型') },
    },
    {
      title: i18nT('结果'),
      dataIndex: 'result',
      width: 100,
      valueType: 'select',
      fieldProps: { options: RESULT_OPTIONS, placeholder: i18nT('全部结果') },
      render: (_, record) => renderResult(record.result),
    },
    {
      title: i18nT('消息'),
      dataIndex: 'message',
      width: 320,
      search: false,
      render: (_, record) => (
        <MonoCell text={humanAuditMessage(record.message)} max={80} />
      ),
    },
    { title: 'IP', dataIndex: 'ip', width: 150, search: false },
    {
      title: i18nT('关键字'),
      dataIndex: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: i18nT('消息 / 目标 / 操作人') },
    },
    {
      title: i18nT('操作'),
      valueType: 'option',
      width: 90,
      fixed: 'right',
      render: (_, record) => [
        <Button
          key="detail"
          type="link"
          size="small"
          onClick={() => void openDetail(record.id)}
        >
          {i18nT('详情')}
        </Button>,
      ],
    },
  ];

  return (
    <PageContainer style={{ paddingTop: 8 }} pageHeaderRender={false}>
      <div style={{ marginBottom: 8 }}>
        <Space direction="vertical" size={0} style={{ width: '100%' }}>
          <Title level={3} style={{ margin: 0, color: '#0f172a' }}>
            {i18nT('审计中心 · 操作日志')}
          </Title>
          <Text type="secondary" style={{ fontSize: 13 }}>
            {i18nT(
              '全量流水已启用 HMAC-SHA256 链式哈希，可随时校验记录是否被篡改。',
            )}
          </Text>
        </Space>
      </div>

      <ChainBadge
        state={chain}
        onRefresh={loadChainStatus}
        onVerify={runVerify}
      />

      <Card
        bordered={false}
        style={{
          borderRadius: 12,
          boxShadow: '0 1px 4px rgba(15, 23, 42, 0.06)',
        }}
        styles={{ body: { padding: 0 } }}
      >
        <ProTable<AuditItem>
          rowKey="id"
          headerTitle={null}
          actionRef={actionRef}
          formRef={formRef}
          columns={columns}
          search={{ labelWidth: 'auto', span: 6, collapsed: false }}
          pagination={{
            defaultPageSize: 10,
            showSizeChanger: true,
            showTotal: (t, range) =>
              i18nT('第 {0}-{1} 条，共 {2} 条', {
                0: range[0],
                1: range[1],
                2: t,
              }),
          }}
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
          tableStyle={{ paddingInline: 16 }}
          options={{
            density: true,
            fullScreen: true,
            setting: true,
          }}
          request={async (params) => {
            const res = await auditApi.list(params);
            setTotal(res.total ?? 0);
            return { data: res.data, total: res.total, success: res.success };
          }}
          toolBarRender={() => [
            <Button key="refresh" onClick={() => actionRef.current?.reload()}>
              {i18nT('刷新')}
            </Button>,
            access.canAdmin ? (
              <Popconfirm
                key="purge-selected"
                title={i18nT('删除选中的审计日志？')}
                description={i18nT('将永久删除 {0} 条记录，无法恢复。', {
                  0: selectedKeys.length,
                })}
                okText={i18nT('删除')}
                okButtonProps={{ danger: true }}
                disabled={selectedKeys.length === 0}
                onConfirm={() =>
                  runPurge({ ids: selectedKeys.map((k) => Number(k)) })
                }
              >
                <Button
                  danger
                  disabled={selectedKeys.length === 0}
                  loading={purging}
                >
                  {i18nT('删除选中 {0}', {
                    0:
                      selectedKeys.length > 0
                        ? `（${selectedKeys.length}）`
                        : '',
                  })}
                </Button>
              </Popconfirm>
            ) : null,
            access.canAdmin ? (
              <Popconfirm
                key="purge-filtered"
                title={i18nT('清除当前筛选结果？')}
                description={
                  <>
                    {i18nT(
                      '将按当前搜索条件永久删除匹配的全部审计日志（当前共 {0}{1} 条），无法恢复。未设置筛选条件时等同于清空全部。',
                      { 0: total, 1: ' ' },
                    )}
                    <br />
                    {i18nT('清除动作本身会写入一条审计留痕。')}
                  </>
                }
                okText={i18nT('清除')}
                okButtonProps={{ danger: true }}
                onConfirm={() => runPurge({ all: true, ...currentFilters() })}
              >
                <Button danger type="primary" ghost loading={purging}>
                  {i18nT('清除筛选结果')}
                </Button>
              </Popconfirm>
            ) : null,
          ]}
        />
      </Card>

      <Drawer
        width={820}
        open={detailLoading || Boolean(detail)}
        title={
          <Space size={10} align="center">
            <Title level={5} style={{ margin: 0 }}>
              {i18nT('审计详情')}
            </Title>
            {detail ? (
              <Tag color="geekblue" style={{ borderRadius: 4 }}>
                #{detail.id} ·{' '}
                {CATEGORY_META[detail.category]?.text ?? detail.category} /{' '}
                {ACTION_META[detail.action]?.text ?? detail.action}
              </Tag>
            ) : null}
          </Space>
        }
        onClose={() => setDetail(undefined)}
        styles={{ body: { paddingTop: 8 } }}
      >
        {detailLoading && !detail ? <Spin /> : null}
        {detail ? (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              column={2}
              bordered
              size="small"
              items={[
                { key: 'id', label: i18nT('日志 ID'), children: detail.id },
                {
                  key: 'ts',
                  label: i18nT('时间'),
                  children: <TimeCell value={detail.ts} />,
                },
                {
                  key: 'category',
                  label: i18nT('类别'),
                  children: tagFromMeta(detail.category, CATEGORY_META),
                },
                {
                  key: 'action',
                  label: i18nT('动作'),
                  children: tagFromMeta(detail.action, ACTION_META),
                },
                {
                  key: 'actorUsername',
                  label: i18nT('操作人'),
                  children: detail.actorUsername || '-',
                },
                {
                  key: 'actorRole',
                  label: i18nT('操作人角色'),
                  children: detail.actorRole || '-',
                },
                {
                  key: 'actorId',
                  label: i18nT('操作人 ID'),
                  children: detail.actorId ?? '-',
                },
                {
                  key: 'targetType',
                  label: i18nT('目标类型'),
                  children: detail.targetType || '-',
                },
                {
                  key: 'targetId',
                  label: i18nT('目标 ID'),
                  children: detail.targetId ?? '-',
                },
                {
                  key: 'targetName',
                  label: i18nT('目标名称'),
                  children: detail.targetName || '-',
                },
                {
                  key: 'result',
                  label: i18nT('结果'),
                  children: renderResult(detail.result),
                },
                {
                  key: 'ip',
                  label: i18nT('来源 IP'),
                  children: detail.ip || '-',
                },
                {
                  key: 'message',
                  label: i18nT('消息'),
                  span: 2,
                  children: (
                    <DescParagraph value={humanAuditMessage(detail.message)} />
                  ),
                },
                {
                  key: 'userAgent',
                  label: 'User-Agent',
                  span: 2,
                  children: <DescParagraph value={detail.userAgent} />,
                },
              ]}
            />

            <Divider
              titlePlacement="start"
              style={{ margin: '8px 0 12px' }}
              orientationMargin={0}
            >
              <Text strong style={{ fontSize: 13 }}>
                {i18nT('链式哈希')}
              </Text>
            </Divider>

            <Card
              size="small"
              bordered={false}
              style={{
                borderRadius: 10,
                border: '1px solid #e6f0ff',
                background: '#f7faff',
              }}
              styles={{ body: { padding: '14px 16px' } }}
            >
              <Space
                direction="vertical"
                size={10}
                style={{ width: '100%', fontSize: 13 }}
              >
                <Row gutter={[12, 8]}>
                  <Col xs={24} sm={6}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {i18nT('前块哈希')}
                    </Text>
                  </Col>
                  <Col xs={24} sm={18}>
                    <HashCell
                      value={detail.prevHash}
                      label={`prev_hash · ${detail.prevHash ? detail.id - 1 : 'genesis'}`}
                    />
                    {!detail.prevHash ? (
                      <Tag
                        color="default"
                        style={{ marginLeft: 8, borderRadius: 4 }}
                      >
                        {i18nT('链起点 / 历史遗留')}
                      </Tag>
                    ) : null}
                  </Col>
                </Row>
                <Row gutter={[12, 8]}>
                  <Col xs={24} sm={6}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {i18nT('本块哈希')}
                    </Text>
                  </Col>
                  <Col xs={24} sm={18}>
                    <HashCell
                      value={detail.entryHash}
                      label={`entry_hash · #${detail.id}`}
                    />
                    {!detail.entryHash ? (
                      <Tag
                        color="warning"
                        style={{ marginLeft: 8, borderRadius: 4 }}
                      >
                        {i18nT('未哈希（升级前数据）')}
                      </Tag>
                    ) : (
                      <Tag
                        color="blue"
                        style={{ marginLeft: 8, borderRadius: 4 }}
                      >
                        HMAC-SHA256
                      </Tag>
                    )}
                  </Col>
                </Row>
                <Paragraph
                  type="secondary"
                  style={{ marginBottom: 0, fontSize: 12 }}
                >
                  {i18nT(
                    '哈希由服务器 SECRET_KEY 派生出的专用 HMAC 密钥计算：本块哈希 =\n                  HMAC(chain_key, 表名 + 前块哈希 + 规范化字段序列)。\n                  改字段、清空哈希、动连接关系，逐行校验都会点名到具体行；但链只能证明\n                  「手上这串是连续的」——整段删尾行要拿库外锚点（CLI --print-head\n                  / --expect-head）比对才抓得住。',
                  )}
                </Paragraph>
              </Space>
            </Card>

            <Divider
              titlePlacement="start"
              style={{ margin: '4px 0 12px' }}
              orientationMargin={0}
            >
              <Text strong style={{ fontSize: 13 }}>
                {i18nT('原始详情数据')}
              </Text>
            </Divider>
            <JsonBlock value={detail.detail} />
          </Space>
        ) : null}
      </Drawer>
    </PageContainer>
  );
};

export default AuditsPage;
