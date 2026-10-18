/**
 * 概览首页（登录后的落地页）。
 *
 * 数据全部来自堡垒机自己的接口：`dashboardApi.overview()`（资产/授权/会话/审计计数）、
 * `dashboardApi.mine()`（我这边的待办与我的会话）、有权限时再拉 `sessionApi.online()`
 * 看当前在线会话；计数卡与进度条按 `useAccess()` 的权限裁剪，无权时降级成空态，
 * 卡片上的按钮直接 `history.push` 跳到对应页面（`/terminal`、`/audit/commands`、`/audit/sessions` …）。
 */
import {
  ApartmentOutlined,
  AuditOutlined,
  CloudServerOutlined,
  DashboardOutlined,
  FileProtectOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { history, useAccess, useModel } from '@umijs/max';
import {
  Button,
  Card,
  Col,
  Empty,
  Progress,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from 'antd';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ActionTag,
  RiskTag,
  SessionSourceTag,
  TimeCell,
} from '@/components/Bastion';
import { t } from '@/i18n';
import {
  formatDateTime,
  humanAuditMessage,
  truncate,
} from '@/services/bastion/constants';
import { dashboardApi, sessionApi } from '@/services/bastion/endpoints';
import type {
  CommandItem,
  DashboardMine,
  DashboardOverview,
  SessionItem,
  TerminalTarget,
} from '@/services/bastion/types';

const { Text } = Typography;

/** 14 天趋势：不引第三方图表库，用内联 SVG 画柱状图（会话量/命令量/拦截量） */
const TrendChart: React.FC<{
  data: { date: string; sessions: number; commands: number; denied: number }[];
}> = ({ data }) => {
  if (!data?.length) {
    return (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('暂无数据')} />
    );
  }
  const width = 720;
  const height = 180;
  const padding = { top: 12, right: 8, bottom: 24, left: 32 };
  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;
  const max = Math.max(
    1,
    ...data.map((item) => Math.max(item.sessions, item.commands, item.denied)),
  );
  const slot = innerWidth / data.length;
  const barWidth = Math.max(3, Math.min(10, slot / 3.6));
  const scale = (value: number) => (value / max) * innerHeight;

  return (
    <div style={{ overflowX: 'auto' }}>
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={t('近 14 天会话与命令趋势')}
        style={{ minWidth: width }}
      >
        {[0, 0.5, 1].map((ratio) => (
          <g key={ratio}>
            <line
              x1={padding.left}
              x2={padding.left + innerWidth}
              y1={padding.top + innerHeight * (1 - ratio)}
              y2={padding.top + innerHeight * (1 - ratio)}
              stroke="rgba(0,0,0,0.08)"
              strokeDasharray={ratio === 0 ? undefined : '4 4'}
            />
            <text
              x={padding.left - 6}
              y={padding.top + innerHeight * (1 - ratio) + 4}
              textAnchor="end"
              fontSize={10}
              fill="rgba(0,0,0,0.45)"
            >
              {Math.round(max * ratio)}
            </text>
          </g>
        ))}
        {data.map((item, index) => {
          const x = padding.left + index * slot + slot / 2;
          return (
            <g key={item.date}>
              <rect
                x={x - barWidth * 1.6}
                y={padding.top + innerHeight - scale(item.sessions)}
                width={barWidth}
                height={scale(item.sessions)}
                fill="#1677ff"
                rx={2}
              >
                <title>
                  {t('{0} 会话 {1}', { 0: item.date, 1: item.sessions })}
                </title>
              </rect>
              <rect
                x={x - barWidth / 2}
                y={padding.top + innerHeight - scale(item.commands)}
                width={barWidth}
                height={scale(item.commands)}
                fill="#52c41a"
                rx={2}
              >
                <title>
                  {t('{0} 命令 {1}', { 0: item.date, 1: item.commands })}
                </title>
              </rect>
              <rect
                x={x + barWidth * 0.6}
                y={padding.top + innerHeight - scale(item.denied)}
                width={barWidth}
                height={scale(item.denied)}
                fill="#ff4d4f"
                rx={2}
              >
                <title>
                  {t('{0} 拦截 {1}', { 0: item.date, 1: item.denied })}
                </title>
              </rect>
              {index % 2 === 0 ? (
                <text
                  x={x}
                  y={height - 6}
                  textAnchor="middle"
                  fontSize={10}
                  fill="rgba(0,0,0,0.45)"
                >
                  {item.date.slice(5)}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
      <Space size={16}>
        <Text type="secondary">
          <Tag color="#1677ff">■</Tag>
          {t('会话')}
        </Text>
        <Text type="secondary">
          <Tag color="#52c41a">■</Tag>
          {t('命令')}
        </Text>
        <Text type="secondary">
          <Tag color="#ff4d4f">■</Tag>
          {t('拦截')}
        </Text>
      </Space>
    </div>
  );
};

const Dashboard: React.FC = () => {
  const access = useAccess();
  const { initialState } = useModel('@@initialState');
  const [overview, setOverview] = useState<DashboardOverview>();
  const [mine, setMine] = useState<DashboardMine>();
  const [online, setOnline] = useState<SessionItem[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [data, mineData, onlineData] = await Promise.all([
        dashboardApi.overview(),
        dashboardApi.mine().catch(() => undefined),
        access.canSessionView
          ? sessionApi.online().catch(() => [])
          : Promise.resolve([]),
      ]);
      setOverview(data);
      setMine(mineData);
      setOnline(onlineData ?? []);
    } catch (error) {
      console.debug('dashboard load failed', error);
    } finally {
      setLoading(false);
    }
  }, [access.canSessionView]);

  useEffect(() => {
    load();
  }, [load]);

  const cards = overview?.cards;
  const quickActions = useMemo(
    () =>
      [
        {
          key: 'hosts',
          label: t('添加主机'),
          icon: <CloudServerOutlined />,
          path: '/assets/hosts',
          show: access.canHostManage,
        },
        {
          key: 'grants',
          label: t('配置授权'),
          icon: <SafetyCertificateOutlined />,
          path: '/grants',
          show: access.canGrantManage,
        },
        {
          key: 'policies',
          label: t('命令策略'),
          icon: <FileProtectOutlined />,
          path: '/policies',
          show: access.canPolicyManage,
        },
        {
          key: 'users',
          label: t('用户管理'),
          icon: <TeamOutlined />,
          path: '/identity/users',
          show: access.canUserManage,
        },
        {
          key: 'terminal',
          label: t('打开网页终端'),
          icon: <ThunderboltOutlined />,
          path: '/terminal',
          show: access.canTerminalUse,
        },
        {
          key: 'audits',
          label: t('审计中心'),
          icon: <AuditOutlined />,
          path: '/audit/sessions',
          show: access.canSessionView,
        },
      ].filter((item) => item.show),
    [access],
  );

  const commandColumns = [
    {
      title: t('时间'),
      dataIndex: 'startedAt',
      width: 170,
      render: (_: unknown, row: CommandItem) => (
        <TimeCell value={row.startedAt} />
      ),
    },
    {
      title: t('用户'),
      dataIndex: 'username',
      width: 100,
    },
    {
      title: t('主机'),
      dataIndex: 'hostName',
      width: 140,
      ellipsis: true,
    },
    {
      title: t('命令'),
      dataIndex: 'command',
      ellipsis: true,
      render: (_: unknown, row: CommandItem) => (
        <Tooltip title={<pre style={{ margin: 0 }}>{row.command}</pre>}>
          <Text code>{truncate(row.command, 46)}</Text>
        </Tooltip>
      ),
    },
    {
      title: t('动作'),
      dataIndex: 'action',
      width: 80,
      render: (_: unknown, row: CommandItem) => (
        <ActionTag action={row.action} />
      ),
    },
    {
      title: t('风险'),
      dataIndex: 'riskLevel',
      width: 80,
      render: (_: unknown, row: CommandItem) => (
        <RiskTag level={row.riskLevel} />
      ),
    },
  ];

  return (
    <PageContainer
      title={t('运维概览')}
      subTitle={t('你好，{0}——今天也要把每一条命令盯住', {
        0: initialState?.currentUser?.name ?? t('运维同学'),
      })}
      extra={
        <Space>
          <Button onClick={load} loading={loading}>
            {t('刷新')}
          </Button>
        </Space>
      }
    >
      {quickActions.length ? (
        <Card
          style={{ marginBottom: 16 }}
          styles={{ body: { padding: '12px 16px' } }}
        >
          <Space wrap size={12}>
            <Text type="secondary">{t('快捷入口：')}</Text>
            {quickActions.map((item) => (
              <Button
                key={item.key}
                icon={item.icon}
                onClick={() => history.push(item.path)}
              >
                {item.label}
              </Button>
            ))}
          </Space>
        </Card>
      ) : null}

      <Row gutter={[16, 16]}>
        <Col xs={12} sm={8} lg={4}>
          <Card loading={loading && !cards} styles={{ body: { padding: 16 } }}>
            <Statistic
              title={t('在线会话')}
              value={cards?.onlineSessions ?? 0}
              styles={{
                content: {
                  color:
                    (cards?.onlineSessions ?? 0) > 0 ? '#52c41a' : undefined,
                },
              }}
              prefix={<ThunderboltOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card loading={loading && !cards} styles={{ body: { padding: 16 } }}>
            <Statistic
              title={t('24h 会话')}
              value={cards?.sessions24h ?? 0}
              prefix={<AuditOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card loading={loading && !cards} styles={{ body: { padding: 16 } }}>
            <Statistic
              title={t('24h 命令')}
              value={cards?.commands24h ?? 0}
              prefix={<DashboardOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card loading={loading && !cards} styles={{ body: { padding: 16 } }}>
            <Statistic
              title={t('24h 拦截')}
              value={cards?.denied24h ?? 0}
              styles={{
                content: {
                  color: (cards?.denied24h ?? 0) > 0 ? '#ff4d4f' : undefined,
                },
              }}
              prefix={<SafetyCertificateOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card loading={loading && !cards} styles={{ body: { padding: 16 } }}>
            <Statistic
              title={t('主机')}
              value={cards?.hosts ?? 0}
              prefix={<CloudServerOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} sm={8} lg={4}>
          <Card loading={loading && !cards} styles={{ body: { padding: 16 } }}>
            <Statistic
              title={t('授权')}
              value={cards?.grants ?? 0}
              prefix={<ApartmentOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={16}>
          <Card
            title={t('近 14 天趋势')}
            extra={<Text type="secondary">{t('会话 / 命令 / 拦截')}</Text>}
            style={{ height: '100%' }}
          >
            <TrendChart data={overview?.trend ?? []} />
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title={t('主机会话 TOP')} style={{ height: '100%' }}>
            {overview?.topHosts?.length ? (
              <Space orientation="vertical" style={{ width: '100%' }} size={10}>
                {overview.topHosts.map((item) => {
                  const maxTop = Math.max(
                    ...overview.topHosts.map((row) => row.count),
                    1,
                  );
                  return (
                    <div key={item.hostName}>
                      <Space
                        style={{
                          width: '100%',
                          justifyContent: 'space-between',
                        }}
                      >
                        <Text ellipsis style={{ maxWidth: 160 }}>
                          {item.hostName}
                        </Text>
                        <Text type="secondary">
                          {t('{0} 次', { 0: item.count })}
                        </Text>
                      </Space>
                      <Progress
                        percent={Math.round((item.count / maxTop) * 100)}
                        showInfo={false}
                        size="small"
                      />
                    </div>
                  );
                })}
              </Space>
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={t('近 7 天暂无会话')}
              />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={16}>
          <Card
            title={t('最近命令（含拦截）')}
            extra={
              access.canCommandView ? (
                <Button
                  type="link"
                  onClick={() => history.push('/audit/commands')}
                >
                  {t('查看全部')}
                </Button>
              ) : null
            }
          >
            <Table<CommandItem>
              rowKey="id"
              size="small"
              pagination={false}
              loading={loading}
              dataSource={overview?.recentCommands ?? []}
              columns={commandColumns}
            />
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title={t('最近审计事件')} style={{ height: '100%' }}>
            {overview?.recentAudits?.length ? (
              <Timeline
                items={overview.recentAudits.map((item) => ({
                  color: item.result === 'failure' ? 'red' : 'green',
                  children: (
                    <div>
                      <Space size={6} wrap>
                        <Tag>{item.category}</Tag>
                        <Text strong>{item.action}</Text>
                        <Text type="secondary">
                          {item.actorUsername || t('系统')}
                        </Text>
                      </Space>
                      <div>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {truncate(
                            humanAuditMessage(item.message) ||
                              item.targetName ||
                              '-',
                            60,
                          )}
                        </Text>
                      </div>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {formatDateTime(item.ts)}
                      </Text>
                    </div>
                  ),
                }))}
              />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={t('暂无审计事件')}
              />
            )}
          </Card>
        </Col>
      </Row>

      {mine && !access.canSessionViewAll ? (
        <Card
          title={t('我的可访问主机')}
          style={{ marginTop: 16 }}
          extra={
            access.canTerminalUse ? (
              <Button type="primary" onClick={() => history.push('/terminal')}>
                {t('打开网页终端')}
              </Button>
            ) : null
          }
        >
          {mine.targets?.length ? (
            <Table<TerminalTarget>
              rowKey="hostId"
              size="small"
              pagination={false}
              dataSource={mine.targets}
              columns={[
                { title: t('主机'), dataIndex: 'hostName' },
                {
                  title: t('地址'),
                  dataIndex: 'address',
                  render: (_, row) => `${row.address}:${row.port}`,
                },
                { title: t('分组'), dataIndex: 'groupName' },
                { title: t('命令策略'), dataIndex: 'policyName' },
                {
                  title: t('可用账号'),
                  dataIndex: 'accounts',
                  render: (_, row) =>
                    row.accounts?.map((item) => item.username).join('、') ||
                    '-',
                },
              ]}
            />
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t(
                '你还没有被授权任何主机，请联系管理员在『访问授权』里配置',
              )}
            />
          )}
        </Card>
      ) : null}

      {access.canSessionView ? (
        <Card
          title={t('当前在线会话')}
          style={{ marginTop: 16 }}
          extra={
            <Button type="link" onClick={() => history.push('/audit/sessions')}>
              {t('进入审计中心')}
            </Button>
          }
        >
          {online.length ? (
            <Table<SessionItem>
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={online}
              columns={[
                { title: t('会话号'), dataIndex: 'sid', width: 200 },
                { title: t('用户'), dataIndex: 'username', width: 120 },
                { title: t('主机'), dataIndex: 'hostName', width: 160 },
                {
                  title: t('来源'),
                  dataIndex: 'source',
                  width: 110,
                  render: (_: unknown, row: SessionItem) => (
                    <SessionSourceTag source={row.source} />
                  ),
                },
                { title: t('客户端'), dataIndex: 'clientIp', width: 140 },
                {
                  title: t('已执行命令'),
                  dataIndex: 'commandCount',
                  width: 110,
                  render: (_: unknown, row: SessionItem) => (
                    <Space size={4}>
                      <Text>{row.commandCount}</Text>
                      {row.deniedCount > 0 ? (
                        <Text type="danger">
                          {t('（拦 {0}）', { 0: row.deniedCount })}
                        </Text>
                      ) : null}
                    </Space>
                  ),
                },
                {
                  title: t('开始时间'),
                  dataIndex: 'startedAt',
                  render: (_: unknown, row: SessionItem) => (
                    <TimeCell value={row.startedAt} />
                  ),
                },
              ]}
            />
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t('当前没有在线会话')}
            />
          )}
        </Card>
      ) : null}
    </PageContainer>
  );
};

export default Dashboard;
