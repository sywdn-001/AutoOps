import {
  CheckCircleFilled,
  CloseCircleFilled,
  PlayCircleOutlined,
  PoweroffOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import { Alert, App, Button, Card, Descriptions, Space, Spin, Tag, Typography } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { t } from '@/i18n';
import {
  rdpGatewayApi,
  type RdpGatewayDiagnose,
  type RdpGatewayStatus,
} from '@/services/bastion/rdpGateway';

const { Text, Paragraph } = Typography;

/**
 * 系统设置 → RDP 网关。
 *
 * Windows 的 `mstsc` 连堡垒机的这个端口，登录后网关自己画一个「你有权连的主机」选单。
 * 真正的 RDP 服务端是 `bastion-rdp-gateway`（Rust），这里只管「看状态 / 启停 / 诊断」。
 */
const RdpGatewayPage = () => {
  const access = useAccess();
  const { message } = App.useApp();
  const [status, setStatus] = useState<RdpGatewayStatus>();
  const [diagnose, setDiagnose] = useState<RdpGatewayDiagnose>();
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      setStatus(await rdpGatewayApi.status());
    } catch (error) {
      // 关键：接口失败要**说出来**，不能让页面假装「未运行」
      setLoadError(error instanceof Error ? error.message : String(error));
      setStatus(undefined);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDiagnose = useCallback(async () => {
    try {
      setDiagnose(await rdpGatewayApi.diagnose());
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error);
      message.error(t('诊断接口也失败了：{0}', { 0: text }));
    }
  }, [message]);

  useEffect(() => {
    void load();
    void loadDiagnose();
  }, [load, loadDiagnose]);

  const run = async (action: 'start' | 'stop') => {
    setBusy(true);
    try {
      if (action === 'start') {
        const result = await rdpGatewayApi.start();
        message.success(
          result.started ? t('网关已启动（端口 {0}）', { 0: result.port }) : t('网关本来就在运行'),
        );
      } else {
        const result = await rdpGatewayApi.stop();
        message.success(result.stopped ? t('网关已停止') : t('网关没有在运行'));
      }
      await load();
      await loadDiagnose();
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error);
      message.error(
        t('{0} 失败：{1}', { 0: action === 'start' ? t('启动') : t('停止'), 1: text }),
      );
    } finally {
      setBusy(false);
    }
  };

  const running = status?.listening;
  const binaryOk = status?.binaryExists ?? diagnose?.binaryExists;

  return (
    <PageContainer
      title={t('RDP 网关')}
      subTitle={t(
        'Windows 的 mstsc 连堡垒机的这个端口：先登录堡垒机账号，再从「你有权连的主机」里挑一台',
      )}
    >
      {loadError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          title={t('读不到网关状态：{0}', { 0: loadError })}
          description={t(
            '常见原因：后端还是改动前的旧进程（重启一次后端即可），或者当前账号没有 rdp:use 权限。点「刷新」重试。',
          )}
          action={
            <Button size="small" onClick={load}>
              {t('刷新')}
            </Button>
          }
        />
      )}

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
              <Text code>{status?.port ?? diagnose?.port ?? '-'}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('连接命令')} span={2}>
              <Text code>{`mstsc /v:<${t('堡垒机地址')}>:${status?.port ?? diagnose?.port ?? 4489}`}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t('网关程序')} span={2}>
              <Text code>{status?.binary ?? diagnose?.binary ?? '-'}</Text>
              {binaryOk === false && (
                <Text type="danger">
                  {' '}
                  {t('（未编译，先在 bastion-rdp-gateway 执行 cargo build --release）')}
                </Text>
              )}
            </Descriptions.Item>
            <Descriptions.Item label={t('日志')} span={2}>
              <Text code>{status?.logFile ?? '-'}</Text>
            </Descriptions.Item>
          </Descriptions>
        )}
      </Card>

      <Card
        title={t('诊断')}
        style={{ marginTop: 16 }}
        extra={
          <Button size="small" icon={<ReloadOutlined />} onClick={loadDiagnose}>
            {t('重新诊断')}
          </Button>
        }
      >
        {diagnose ? (
          <>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('后端代码')}>
                <Text code>{diagnose.moduleFile}</Text>
              </Descriptions.Item>
              <Descriptions.Item label={t('工作目录')}>
                <Text code>{diagnose.cwd}</Text>
              </Descriptions.Item>
              <Descriptions.Item label={t('解析到的网关程序')}>
                <Text code>{diagnose.binary}</Text>
                {diagnose.binaryExists ? (
                  <Tag color="success" style={{ marginLeft: 8 }}>
                    {t('存在')}
                  </Tag>
                ) : (
                  <Tag color="error" style={{ marginLeft: 8 }}>
                    {t('不存在')}
                  </Tag>
                )}
              </Descriptions.Item>
            </Descriptions>
            <Paragraph type="secondary" style={{ marginBottom: 4 }}>
              {t('候选路径（逐个检查，第一个存在的就用它）：')}
            </Paragraph>
            <pre style={{ maxHeight: 220, overflow: 'auto', fontSize: 12 }}>
              {diagnose.candidates
                .map((item: { exists: boolean; path: string }) => `${item.exists ? '✓' : '·'} ${item.path}`)
                .join('\n')}
            </pre>
          </>
        ) : (
          <Text type="secondary">{t('诊断接口不可用（后端可能是改动前的旧进程）')}</Text>
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
