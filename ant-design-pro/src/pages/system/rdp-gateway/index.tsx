import {
  CheckCircleFilled,
  CloseCircleFilled,
  PlayCircleOutlined,
  PoweroffOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { Alert, App, Button, Card, Descriptions, Space, Spin, Tag, Typography } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { useAccess } from '@umijs/max';
import { t } from '@/i18n';
import { rdpGatewayApi, type RdpGatewayStatus } from '@/services/bastion/rdpGateway';

const { Text } = Typography;

/**
 * 系统设置 → RDP 网关。
 *
 * Windows 的 `mstsc` 连堡垒机的这个端口，登录后网关自己画一个「你有权连的主机」选单，
 * 选中后把目标机的画面接进来。真正的 RDP 服务端是 `bastion-rdp-gateway`（Rust），
 * 这里只管「看状态 / 启停 / 改端口与开关」。
 */
const RdpGatewayPage = () => {
  const access = useAccess();
  const { message } = App.useApp();
  const [status, setStatus] = useState<RdpGatewayStatus>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await rdpGatewayApi.status());
    } catch (error) {
      message.error(t('读取 RDP 网关状态失败'));
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (action: 'start' | 'stop') => {
    setBusy(true);
    try {
      if (action === 'start') {
        const result = await rdpGatewayApi.start();
        message.success(
          result.started
            ? t('网关已启动（端口 {0}）', { 0: result.port })
            : t('网关本来就在运行'),
        );
      } else {
        const result = await rdpGatewayApi.stop();
        message.success(result.stopped ? t('网关已停止') : t('网关没有在运行'));
      }
      await load();
    } catch (error) {
      message.error(
        action === 'start' ? t('启动失败，详见后端日志') : t('停止失败，详见后端日志'),
      );
    } finally {
      setBusy(false);
    }
  };

  const running = status?.listening;

  return (
    <PageContainer
      title={t('RDP 网关')}
      subTitle={t(
        'Windows 的 mstsc 连堡垒机的这个端口：先登录堡垒机账号，再从「你有权连的主机」里挑一台',
      )}
    >
      <Card
        title={t('运行状态')}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
              {t('刷新')}
            </Button>
            {access.canSettingManage && (
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                disabled={running}
                loading={busy}
                onClick={() => run('start')}
              >
                {t('启动')}
              </Button>
            )}
            {access.canSettingManage && (
              <Button
                danger
                icon={<PoweroffOutlined />}
                disabled={!running}
                loading={busy}
                onClick={() => run('stop')}
              >
                {t('停止')}
              </Button>
            )}
          </Space>
        }
      >
        {loading && !status ? (
          <Spin />
        ) : (
          <Descriptions column={2} size="small">
            <Descriptions.Item label={t('状态')}>
              {running ? (
                <Tag icon={<CheckCircleFilled />} color="success">
                  {t('运行中')}
                </Tag>
              ) : (
                <Tag icon={<CloseCircleFilled />} color="default">
                  {t('未运行')}
                </Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label={t('监听端口')}>
              <Text code>{status?.port ?? '-'}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('连接命令')} span={2}>
              <Text code>
                {`mstsc /v:<${t('堡垒机地址')}>:${status?.port ?? 4489}`}
              </Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('网关程序')} span={2}>
              <Text code>{status?.binary ?? '-'}</Text>
              {status && !status.binaryExists && (
                <Text type="danger"> {t('（未编译，先在 bastion-rdp-gateway 执行 cargo build --release）')}</Text>
              )}
            </Descriptions.Item>
            <Descriptions.Item label={t('日志')} span={2}>
              <Text code>{status?.logFile ?? '-'}</Text>
            </Descriptions.Item>
          </Descriptions>
        )}
      </Card>

      <Card title={t('配置')} style={{ marginTop: 16 }}>
        <Alert
          type="info"
          showIcon
          title={t(
            '开关与端口在「系统设置 → 参数设置」里改：rdp_gateway_enabled / rdp_gateway_port，保存后在这里点「停止 → 启动」生效。',
          )}
        />
      </Card>
    </PageContainer>
  );
};

export default RdpGatewayPage;
