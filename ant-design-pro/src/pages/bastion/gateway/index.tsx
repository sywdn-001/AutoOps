/**
 * SSH 网关（系统设置 → SSH 网关）。
 *
 * 数据来自 `settingApi.gateway()`（`GET /api/settings/gateway`）：
 * 运行状态、监听地址、在线会话数、接入命令、认证方式、主机密钥指纹与登录横幅。
 * 本页只读展示，端口/横幅等可写参数统一在「参数设置」里修改。
 */
import {
  CloudServerOutlined,
  LaptopOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Row,
  Space,
  Statistic,
  Typography,
} from 'antd';
import React, { useCallback, useEffect, useState } from 'react';
import { BoolTag, CopyText } from '@/components/Bastion';
import { t } from '@/i18n';
import { settingApi } from '@/services/bastion/endpoints';
import type { GatewayStatus } from '@/services/bastion/types';

const { Text, Paragraph } = Typography;

/** 后端错误 → 可读提示（统一信封里的 message） */
const backendMessage = (error: unknown, fallback: string): string =>
  (error as { response?: { data?: { message?: string } } })?.response?.data
    ?.message || fallback;

const Gateway: React.FC = () => {
  const { message } = App.useApp();
  const [status, setStatus] = useState<GatewayStatus>();
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await settingApi.gateway());
    } catch (error) {
      message.error(backendMessage(error, t('网关状态加载失败，请稍后重试')));
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    load();
  }, [load]);

  const running = Boolean(status?.running);
  const listen = status ? `${status.listenHost}:${status.listenPort}` : '-';

  const paramItems = [
    {
      key: 'serverVersion',
      label: t('服务器版本'),
      children: (
        <Space size={6}>
          <CloudServerOutlined style={{ color: '#2E7BFF' }} />
          <Text>{status?.serverVersion || '-'}</Text>
        </Space>
      ),
    },
    {
      key: 'clientVersion',
      label: t('客户端版本'),
      children: (
        <Space size={6}>
          <LaptopOutlined style={{ color: '#12B8C8' }} />
          <Text>{status?.clientVersion || '-'}</Text>
        </Space>
      ),
    },
    { key: 'hostname', label: t('主机名'), children: status?.hostname || '-' },
    {
      key: 'allowPassword',
      label: t('密码认证'),
      children: <BoolTag value={status?.allowPassword} />,
    },
    {
      key: 'allowPublicKey',
      label: t('公钥认证'),
      children: <BoolTag value={status?.allowPublicKey} />,
    },
    {
      key: 'hostKeyFingerprint',
      label: t('主机密钥指纹'),
      children: <CopyText text={status?.hostKeyFingerprint} />,
    },
    {
      key: 'banner',
      label: t('登录横幅'),
      span: 'filled' as const,
      children: (
        <Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
          {status?.banner || '-'}
        </Paragraph>
      ),
    },
  ];

  return (
    <PageContainer
      title={t('SSH 网关')}
      subTitle={t('运维人员通过 ssh 进入审计 shell，逐条命令留痕')}
      extra={
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
          {t('刷新')}
        </Button>
      }
    >
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={loading && !status}>
            <Statistic
              title={t('运行状态')}
              value={status ? (running ? t('运行中') : t('未运行')) : '-'}
              styles={{ content: { color: running ? '#52c41a' : '#ff4d4f' } }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={loading && !status}>
            <Statistic title={t('监听地址')} value={listen} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={loading && !status}>
            <Statistic
              title={t('网关在线会话')}
              value={status ? status.gatewaySessions : '-'}
              suffix={t('个')}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card loading={loading && !status}>
            <Statistic
              title={t('全部在线会话')}
              value={status ? status.onlineSessions : '-'}
              suffix={t('个')}
            />
          </Card>
        </Col>
      </Row>

      {status && !status.running && status.enabled ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          title={t('网关未监听，请确认后端启动日志中 SSH 网关是否报端口占用')}
        />
      ) : null}

      {!status && !loading ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          title={t('未获取到网关状态')}
          description={t(
            '请确认已登录且后端 SSH 网关配置可用，然后点击「刷新」重试。',
          )}
        />
      ) : null}

      {status ? (
        <>
          <Card title={t('如何接入')} style={{ marginBottom: 16 }}>
            <Space orientation="vertical" size={12} style={{ width: '100%' }}>
              <Alert
                type="info"
                showIcon
                title={
                  <Space wrap size={8}>
                    <Text>{t('运维侧接入命令：')}</Text>
                    <CopyText text={status.connectCommand} />
                  </Space>
                }
              />
              <Typography>
                <ol style={{ margin: 0, paddingLeft: 22, lineHeight: 2 }}>
                  <li>
                    {t(
                      '在目标 Linux 主机上配置好资产账号（主机列表 → 账号管理）。',
                    )}
                  </li>
                  <li>{t('在「身份与权限」里给用户开启「网关登录」开关。')}</li>
                  <li>
                    {t('在「访问授权」里把主机授权给该用户并绑定命令策略。')}
                  </li>
                  <li>
                    {t(
                      '运维人员执行上面的 ssh 命令，输入堡垒机账号密码后进入审计\n                    shell，按菜单序号选择主机；每条命令与输出都会被记录到「审计中心」。',
                    )}
                  </li>
                </ol>
              </Typography>
            </Space>
          </Card>

          <Card title={t('网关参数')} style={{ marginBottom: 16 }}>
            <Descriptions
              column={{ xs: 1, sm: 1, lg: 2 }}
              bordered
              size="middle"
              items={paramItems}
            />
          </Card>
        </>
      ) : null}

      <Alert
        type="info"
        showIcon
        title={t(
          '修改网关端口/横幅请到『参数设置』；主机密钥指纹应在首次部署时分发给运维人员，以防中间人攻击',
        )}
      />
    </PageContainer>
  );
};

export default Gateway;
