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
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  DescParagraph,
  JsonBlock,
  MonoCell,
  TimeCell,
} from '@/components/Bastion';
import type { PurgePayload } from '@/services/bastion/endpoints';
import { auditApi } from '@/services/bastion/endpoints';
import { humanAuditMessage } from '@/services/bastion/constants';
import type {
  AuditChainStatus,
  AuditChainVerifyResult,
  AuditItem,
} from '@/services/bastion/types';

const { Text, Paragraph, Title } = Typography;

type AuditOptionPayload = Awaited<ReturnType<typeof auditApi.options>>;

const RESULT_OPTIONS = [
  { label: '成功', value: 'success' },
  { label: '失败', value: 'failure' },
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
  auth: { text: '认证', color: 'geekblue' },
  user: { text: '用户', color: 'purple' },
  role: { text: '角色', color: 'magenta' },
  host: { text: '资产', color: 'cyan' },
  hostgroup: { text: '资产组', color: 'cyan' },
  host_group: { text: '资产组', color: 'cyan' },
  account: { text: '账号', color: 'blue' },
  grant: { text: '授权', color: 'gold' },
  policy: { text: '策略', color: 'volcano' },
  file_policy: { text: '文件策略', color: 'volcano' },
  session: { text: '会话', color: 'green' },
  command: { text: '命令', color: 'lime' },
  file: { text: '文件', color: 'orange' },
  audit: { text: '审计', color: 'red' },
  gateway: { text: '网关', color: 'geekblue' },
  ai: { text: 'AI', color: 'purple' },
  settings: { text: '设置', color: 'default' },
  system: { text: '系统', color: 'default' },
  purge: { text: '清理', color: 'red' },
  delete: { text: '删除', color: 'red' },
};

const ACTION_META: Record<string, { text: string; color: string }> = {
  login: { text: '登录', color: 'geekblue' },
  logout: { text: '登出', color: 'default' },
  create: { text: '创建', color: 'green' },
  update: { text: '更新', color: 'blue' },
  delete: { text: '删除', color: 'red' },
  remove: { text: '移除', color: 'red' },
  add: { text: '添加', color: 'green' },
  grant: { text: '授权', color: 'gold' },
  revoke: { text: '撤回', color: 'orange' },
  change: { text: '变更', color: 'blue' },
  enable: { text: '启用', color: 'green' },
  disable: { text: '禁用', color: 'default' },
  connect: { text: '连接', color: 'cyan' },
  disconnect: { text: '断开', color: 'default' },
  start: { text: '开始', color: 'green' },
  stop: { text: '停止', color: 'default' },
  exec: { text: '执行', color: 'blue' },
  upload: { text: '上传', color: 'cyan' },
  download: { text: '下载', color: 'cyan' },
  read: { text: '读取', color: 'geekblue' },
  write: { text: '写入', color: 'geekblue' },
  purge: { text: '清理', color: 'red' },
  verify: { text: '校验', color: 'green' },
  refresh: { text: '刷新', color: 'blue' },
  sync: { text: '同步', color: 'purple' },
  request: { text: '请求', color: 'geekblue' },
  response: { text: '响应', color: 'default' },
  success: { text: '成功', color: 'success' },
  failure: { text: '失败', color: 'error' },
};

const TABLE_LABEL: Record<
  'audit_logs' | 'command_logs' | 'file_logs',
  string
> = {
  audit_logs: '操作日志',
  command_logs: '命令流水',
  file_logs: '文件流水',
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
  if (result === 'success') return <Tag color="success">成功</Tag>;
  if (result === 'failure') return <Tag color="error">失败</Tag>;
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
      (Object.keys(verify.tables) as Array<keyof typeof verify.tables>).find(
        (k) => !verify.tables[k].ok,
      )
    );
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
        boxShadow: '0 1px 4px rgba(15, 23, 42, 0.06)',
        borderRadius: 12,
        background: healthy
          ? 'linear-gradient(135deg, #ecfdf5 0%, #f0fdfa 50%, #eff6ff 100%)'
          : 'linear-gradient(135deg, #fef2f2 0%, #fff7ed 50%, #fff1f2 100%)',
        marginBottom: 16,
        overflow: 'hidden',
      }}
      styles={{
        body: {
          padding: xs ? 16 : 24,
          position: 'relative' as const,
        },
      }}
    >
      <div
        style={{
          position: 'absolute',
          right: -40,
          top: -40,
          width: 180,
          height: 180,
          borderRadius: '50%',
          background: healthy
            ? 'radial-gradient(circle at center, rgba(16,185,129,0.18) 0%, rgba(16,185,129,0) 70%)'
            : 'radial-gradient(circle at center, rgba(244,63,94,0.16) 0%, rgba(244,63,94,0) 70%)',
          pointerEvents: 'none',
        }}
      />
      <Row
        gutter={[24, 16]}
        align="middle"
        justify="space-between"
        style={{ position: 'relative' }}
      >
        <Col xs={24} md={10} lg={8}>
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
                  ? 'linear-gradient(135deg, #10b981 0%, #0ea5e9 100%)'
                  : 'linear-gradient(135deg, #f43f5e 0%, #f97316 100%)',
                boxShadow: healthy
                  ? '0 8px 20px rgba(16,185,129,0.35)'
                  : '0 8px 20px rgba(244,63,94,0.32)',
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
                  color: healthy ? '#047857' : '#be123c',
                  fontWeight: 600,
                }}
              >
                CHAIN INTEGRITY
              </Text>
              <Title
                level={4}
                style={{
                  margin: '2px 0 0 0',
                  color: healthy ? '#065f46' : '#881337',
                  fontWeight: 700,
                }}
              >
                {healthy ? '链式哈希 · 完整可信' : '链式哈希 · 存在异常'}
              </Title>
              <Text type="secondary" style={{ fontSize: 12 }}>
                HMAC-SHA256 · 每张流水表独立挂链 · 可随时校验完整性
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
                ? (Object.keys(status.counts) as Array<
                    keyof typeof status.counts
                  >).map((key) => {
                    const c = status.counts[key];
                    const h = status.heads[key];
                    const ok =
                      verify?.tables?.[key]?.ok ??
                      (c.pending === 0 && Boolean(h.lastEntryHash));
                    return (
                      <Col xs={24} sm={8} key={key}>
                        <div
                          style={{
                            padding: '12px 14px',
                            borderRadius: 10,
                            background: ok
                              ? 'rgba(255,255,255,0.7)'
                              : 'rgba(255,255,255,0.9)',
                            border: `1px solid ${
                              ok
                                ? 'rgba(16,185,129,0.25)'
                                : 'rgba(244,63,94,0.25)'
                            }`,
                            backdropFilter: 'blur(6px)',
                          }}
                        >
                          <Space
                            size={8}
                            align="center"
                            style={{ marginBottom: 6 }}
                          >
                            <Badge
                              status={ok ? 'success' : 'error'}
                              text={
                                <Text strong style={{ fontSize: 13 }}>
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
                              总数 <Text strong>{c.total}</Text>
                            </Text>
                            <Text
                              type={c.hashed === c.total ? 'success' : 'warning'}
                            >
                              已哈希{' '}
                              <Text strong>
                                {c.hashed}
                                {c.pending > 0 ? ` / ${c.total}` : ''}
                              </Text>
                            </Text>
                          </Space>
                          <div style={{ marginTop: 6 }}>
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              末尾 id {h.lastId ?? '-'} ·{' '}
                            </Text>
                            <HashCell value={h.lastEntryHash} />
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
              message="链状态读取失败"
              description={error}
            />
          ) : null}
          {verifyErrors ? (
            <Alert
              showIcon
              type="error"
              message={`${TABLE_LABEL[verifyErrors.table]} 检出哈希异常`}
              description={
                <Space direction="vertical" size={4}>
                  <Text>
                    首条坏记录 id ={' '}
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
                      {verifyErrors.sample.map((e, i) => (
                        <li key={i}>
                          <Text code>#{e.id}</Text> — {e.reason}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </Space>
              }
            />
          ) : null}
          {verify && verify.ok && !verifyErrors ? (
            <Alert
              showIcon
              type="success"
              message="全表校验通过"
              description="三张流水表的链式哈希重算全部自洽，未发现篡改或断裂。"
            />
          ) : null}
        </Col>
        <Col xs={24} md={6} style={{ textAlign: xs ? 'left' : 'right' }}>
          <Space size={8} wrap>
            <Button onClick={onRefresh} loading={loading}>
              刷新状态
            </Button>
            <Button
              type="primary"
              onClick={onVerify}
              loading={verifying}
              style={{
                background: healthy
                  ? 'linear-gradient(135deg, #10b981 0%, #0ea5e9 100%)'
                  : 'linear-gradient(135deg, #f43f5e 0%, #f97316 100%)',
                border: 'none',
                boxShadow: healthy
                  ? '0 6px 14px rgba(16,185,129,0.35)'
                  : '0 6px 14px rgba(244,63,94,0.32)',
              }}
            >
              逐行校验哈希
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
        error: (e as Error)?.message ?? '读取链状态失败',
      }));
    }
  }, []);

  const runVerify = useCallback(async () => {
    setChain((s) => ({ ...s, verifying: true }));
    try {
      const data = await auditApi.chainVerify();
      setChain((s) => ({ ...s, verify: data, verifying: false }));
      if (data.ok) message.success('三张流水表哈希校验全部通过');
      else message.warning('检出哈希异常，详情见上方卡片');
    } catch (e) {
      setChain((s) => ({ ...s, verifying: false }));
      message.error('哈希校验失败：' + ((e as Error)?.message ?? '未知错误'));
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

  const runPurge = useCallback(async (payload: PurgePayload) => {
    setPurging(true);
    try {
      const result = await auditApi.purge(payload);
      message.success(`已清除 ${result.deleted} 条审计日志`);
      setSelectedKeys([]);
      actionRef.current?.reload();
      void loadChainStatus();
    } catch (error) {
      console.debug('purge audits failed', error);
    } finally {
      setPurging(false);
    }
  }, [loadChainStatus]);

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
      title: '时间',
      dataIndex: 'ts',
      width: 180,
      search: false,
      render: (_, record) => <TimeCell value={record.ts} />,
    },
    categoryOptions.length > 0
      ? {
          title: '类别',
          dataIndex: 'category',
          width: 140,
          valueType: 'select',
          fieldProps: {
            options: categoryOptions,
            placeholder: '全部类别',
            showSearch: true,
            optionFilterProp: 'label',
          },
          render: (_, record) =>
            tagFromMeta(record.category, CATEGORY_META),
        }
      : {
          title: '类别',
          dataIndex: 'category',
          width: 140,
          fieldProps: { placeholder: '类别' },
          render: (_, record) =>
            tagFromMeta(record.category, CATEGORY_META),
        },
    actionOptions.length > 0
      ? {
          title: '动作',
          dataIndex: 'action',
          width: 130,
          valueType: 'select',
          fieldProps: {
            options: actionOptions,
            placeholder: '全部动作',
            showSearch: true,
            optionFilterProp: 'label',
          },
          render: (_, record) => tagFromMeta(record.action, ACTION_META),
        }
      : {
          title: '动作',
          dataIndex: 'action',
          width: 130,
          fieldProps: { placeholder: '动作' },
          render: (_, record) => tagFromMeta(record.action, ACTION_META),
        },
    {
      title: '操作人',
      dataIndex: 'actorUsername',
      width: 170,
      fieldProps: { placeholder: '操作人' },
      render: (_, record) =>
        record.actorRole
          ? `${record.actorUsername}（${record.actorRole}）`
          : record.actorUsername,
    },
    {
      title: '目标',
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
      title: '目标类型',
      dataIndex: 'targetType',
      hideInTable: true,
      fieldProps: { placeholder: '目标类型' },
    },
    {
      title: '结果',
      dataIndex: 'result',
      width: 100,
      valueType: 'select',
      fieldProps: { options: RESULT_OPTIONS, placeholder: '全部结果' },
      render: (_, record) => renderResult(record.result),
    },
    {
      title: '消息',
      dataIndex: 'message',
      width: 320,
      search: false,
      render: (_, record) => (
        <MonoCell text={humanAuditMessage(record.message)} max={80} />
      ),
    },
    { title: 'IP', dataIndex: 'ip', width: 150, search: false },
    {
      title: '关键字',
      dataIndex: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: '消息 / 目标 / 操作人' },
    },
    {
      title: '操作',
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
          详情
        </Button>,
      ],
    },
  ];

  return (
    <PageContainer
      style={{ paddingTop: 8 }}
      pageHeaderRender={false}
    >
      <div style={{ marginBottom: 8 }}>
        <Space direction="vertical" size={0} style={{ width: '100%' }}>
          <Title level={3} style={{ margin: 0, color: '#0f172a' }}>
            审计中心 · 操作日志
          </Title>
          <Text type="secondary" style={{ fontSize: 13 }}>
            全量流水已启用 HMAC-SHA256 链式哈希，可随时校验记录是否被篡改。
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
              `第 ${range[0]}-${range[1]} 条，共 ${t} 条`,
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
            <Button
              key="refresh"
              onClick={() => actionRef.current?.reload()}
            >
              刷新
            </Button>,
            access.canAdmin ? (
              <Popconfirm
                key="purge-selected"
                title="删除选中的审计日志？"
                description={`将永久删除 ${selectedKeys.length} 条记录，无法恢复。`}
                okText="删除"
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
                  删除选中
                  {selectedKeys.length > 0
                    ? `（${selectedKeys.length}）`
                    : ''}
                </Button>
              </Popconfirm>
            ) : null,
            access.canAdmin ? (
              <Popconfirm
                key="purge-filtered"
                title="清除当前筛选结果？"
                description={
                  <>
                    将按当前搜索条件永久删除匹配的全部审计日志（当前共{' '}
                    {total} 条），无法恢复。未设置筛选条件时等同于清空全部。
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
      </Card>

      <Drawer
        width={820}
        open={detailLoading || Boolean(detail)}
        title={
          <Space size={10} align="center">
            <Title level={5} style={{ margin: 0 }}>
              审计详情
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
                { key: 'id', label: '日志 ID', children: detail.id },
                {
                  key: 'ts',
                  label: '时间',
                  children: <TimeCell value={detail.ts} />,
                },
                {
                  key: 'category',
                  label: '类别',
                  children: tagFromMeta(detail.category, CATEGORY_META),
                },
                {
                  key: 'action',
                  label: '动作',
                  children: tagFromMeta(detail.action, ACTION_META),
                },
                {
                  key: 'actorUsername',
                  label: '操作人',
                  children: detail.actorUsername || '-',
                },
                {
                  key: 'actorRole',
                  label: '操作人角色',
                  children: detail.actorRole || '-',
                },
                {
                  key: 'actorId',
                  label: '操作人 ID',
                  children: detail.actorId ?? '-',
                },
                {
                  key: 'targetType',
                  label: '目标类型',
                  children: detail.targetType || '-',
                },
                {
                  key: 'targetId',
                  label: '目标 ID',
                  children: detail.targetId ?? '-',
                },
                {
                  key: 'targetName',
                  label: '目标名称',
                  children: detail.targetName || '-',
                },
                {
                  key: 'result',
                  label: '结果',
                  children: renderResult(detail.result),
                },
                { key: 'ip', label: '来源 IP', children: detail.ip || '-' },
                {
                  key: 'message',
                  label: '消息',
                  span: 2,
                  children: (
                    <DescParagraph
                      value={humanAuditMessage(detail.message)}
                    />
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
              orientation="left"
              style={{ margin: '8px 0 12px' }}
              orientationMargin={0}
            >
              <Text strong style={{ fontSize: 13 }}>
                🔗 链式哈希
              </Text>
            </Divider>

            <Card
              size="small"
              bordered
              style={{
                borderRadius: 10,
                background:
                  'linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%)',
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
                      前块哈希
                    </Text>
                  </Col>
                  <Col xs={24} sm={18}>
                    <HashCell
                      value={detail.prevHash}
                      label={`prev_hash · ${detail.id - 1 ?? 'genesis'}`}
                    />
                    {!detail.prevHash ? (
                      <Tag
                        color="default"
                        style={{ marginLeft: 8, borderRadius: 4 }}
                      >
                        链起点 / 历史遗留
                      </Tag>
                    ) : null}
                  </Col>
                </Row>
                <Row gutter={[12, 8]}>
                  <Col xs={24} sm={6}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      本块哈希
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
                        未哈希（升级前数据）
                      </Tag>
                    ) : (
                      <Tag
                        color="success"
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
                  哈希由服务器 SECRET_KEY 派生出的专用 HMAC 密钥计算：本块哈希
                  = HMAC(chain_key, 表名 + 前块哈希 + 规范化字段序列)。
                  任意行字段、哈希或连接关系被改动，逐行校验都会立即检出。
                </Paragraph>
              </Space>
            </Card>

            <Divider
              orientation="left"
              style={{ margin: '4px 0 12px' }}
              orientationMargin={0}
            >
              <Text strong style={{ fontSize: 13 }}>
                原始详情数据
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
