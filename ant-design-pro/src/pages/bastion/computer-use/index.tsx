/**
 * ComputerUse 部署向导（系统设置 → ComputerUse 部署）。
 *
 * 四步分步向导：① 选择并缓存 Python 安装包 ② 选择目标主机/账号与部署选项
 * ③ 执行部署（阶段进度 + SSE 实时日志）④ 成功结果页。每一步一个视图，
 * 上一步的前置条件不满足就不让往下走（没有缓存好的包 / 没选主机）。
 *
 * 本文件负责编排与数据流：拉数据、建部署、订阅日志流、把状态喂给四个步骤组件；
 * 纯逻辑（阶段↔百分比、参数组装、SSE 帧解析、状态机）都在 `./helpers` 里，可单测。
 */
import {
  DesktopOutlined,
  HistoryOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { history } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Drawer,
  Space,
  Steps,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  type ComputerUseHost,
  computerUseApi,
  type Deployment,
  openLogStream,
  type PythonVersionItem,
} from '@/services/bastion/computeruse';
import DeployStep from './deployStep';
import {
  backendMessage,
  buildDeployPayload,
  buildDeployResult,
  canDeploy,
  type DeployStreamState,
  deployStreamReducer,
  generateAuthKey,
  initialStreamState,
} from './helpers';
import HostStep from './hostStep';
import InstallerStep from './installerStep';
import SuccessStep from './successStep';
import {
  DEFAULT_DEPLOY_OPTIONS,
  type DeployOptionDraft,
  type DeployResult,
  type SelectedInstaller,
  WIZARD_STEPS,
} from './types';

const { Text } = Typography;

const ComputerUsePage: React.FC = () => {
  const { message } = App.useApp();

  // ---- 第 1 步
  const [installer, setInstaller] = useState<PythonVersionItem | null>(null);

  // ---- 第 2 步
  const [host, setHost] = useState<ComputerUseHost | null>(null);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [options, setOptions] = useState<DeployOptionDraft>(() => ({
    ...DEFAULT_DEPLOY_OPTIONS,
  }));
  const [authKey, setAuthKey] = useState('');

  // ---- 第 3/4 步
  const [deploying, setDeploying] = useState(false);
  const [stream, setStream] = useState<DeployStreamState | null>(null);
  const [streamKey, setStreamKey] = useState(0);
  const [result, setResult] = useState<DeployResult | null>(null);
  const [recordsOpen, setRecordsOpen] = useState(false);
  const [records, setRecords] = useState<Deployment[]>([]);
  const [recordsLoading, setRecordsLoading] = useState(false);

  // ---- 向导导航
  const [step, setStep] = useState(0);

  const enterStep = useCallback((index: number) => {
    setStep(index);
  }, []);

  // 开启访问密钥时生成一次 32 位随机密钥（关掉再开就换一把新的）
  useEffect(() => {
    if (!options.useAuthKey) return;
    setAuthKey((current) => current || generateAuthKey(32));
  }, [options.useAuthKey]);

  const selectedInstaller: SelectedInstaller | null = installer
    ? {
        version: installer.version,
        filename: installer.filename,
        size: installer.size,
        sha256: installer.sha256,
      }
    : null;

  // ---------------------------------------------------------------- 订阅日志流
  useEffect(() => {
    if (!deploying || !stream?.deployment) return;
    const deploymentId = stream.deployment.id;
    const handle = openLogStream(deploymentId, (event) => {
      setStream((current) => {
        if (!current || current.deployment?.id !== deploymentId) return current;
        const next = deployStreamReducer(current, event);
        if (event.type === 'end' && event.status === 'success') {
          // 流以 success 结束 → 自动进第 4 步（失败则留在日志页看原因）
          setDeploying(false);
          setStep(3);
        }
        if (event.type === 'end' && event.status !== 'success') {
          setDeploying(false);
        }
        if (
          event.type === 'status' &&
          !event.live &&
          event.status !== 'running'
        ) {
          setDeploying(false);
        }
        return next;
      });
    });
    return () => handle.close();
  }, [deploying, stream?.deployment?.id, streamKey]);

  // 成功后补齐 MCP 工具清单（部署记录里只有一个数字，工具名要问 /status）
  useEffect(() => {
    if (step !== 3) return;
    const deployment = stream?.deployment;
    if (deployment?.status !== 'success') return;
    let cancelled = false;
    computerUseApi
      .status(deployment.hostId)
      .then((status) => {
        if (cancelled) return;
        setResult(
          buildDeployResult(
            deployment,
            (status.tools ?? []).map((t) => t.name),
          ),
        );
      })
      .catch(() => {
        if (!cancelled) setResult(buildDeployResult(deployment, []));
      });
    return () => {
      cancelled = true;
    };
  }, [step, stream?.deployment]);

  // ---------------------------------------------------------------- 加载记录
  const loadRecords = useCallback(async () => {
    setRecordsLoading(true);
    try {
      setRecords(await computerUseApi.deployments());
    } catch (error) {
      message.error(backendMessage(error, '部署记录加载失败，请稍后重试'));
    } finally {
      setRecordsLoading(false);
    }
  }, [message]);

  const openRecords = useCallback(() => {
    setRecordsOpen(true);
    loadRecords();
  }, [loadRecords]);

  // ---------------------------------------------------------------- 动作
  const startDeploy = async () => {
    if (!host || !accountId || !selectedInstaller) {
      message.warning('请先选择目标主机与资产账号');
      return;
    }
    const payload = buildDeployPayload({
      hostId: host.id,
      accountId,
      pythonVersion: selectedInstaller.version,
      installerName: selectedInstaller.filename,
      options,
      authKey,
    });
    setDeploying(true);
    setResult(null);
    try {
      const deployment = await computerUseApi.createDeployment(payload);
      const next = initialStreamState(deployment);
      setStream(next);
      setStreamKey((key) => key + 1);
      enterStep(2);
    } catch (error) {
      setDeploying(false);
      message.error(backendMessage(error, '部署创建失败，请检查参数后重试'));
    }
  };

  const cancelDeploy = async () => {
    const id = stream?.deployment?.id;
    if (!id) return;
    await computerUseApi.cancelDeployment(id);
  };

  const redeploy = () => {
    setStream(null);
    setResult(null);
    setDeploying(false);
    enterStep(1);
  };

  const openConsole = () => {
    const deployment = stream?.deployment;
    if (!deployment) return;
    const params = new URLSearchParams({
      hostId: String(deployment.hostId),
      title: deployment.hostName || deployment.hostAddress || 'Windows 主机',
    });
    // 与第 4 步的跳转保持一致，用 umi history
    history.push(`/rdp/console?${params.toString()}`);
  };

  const freshStart = () => {
    setInstaller(null);
    setHost(null);
    setAccountId(null);
    setStream(null);
    setResult(null);
    setDeploying(false);
    enterStep(0);
  };

  // ---------------------------------------------------------------- 步骤可用性
  const installerReady = Boolean(installer?.cached);
  const payloadReady = canDeploy({
    hostId: host?.id ?? null,
    accountId,
    pythonVersion: selectedInstaller?.version ?? '',
    installerName: selectedInstaller?.filename ?? '',
    options,
    authKey,
  });

  const stepReady = useMemo(
    () => [true, installerReady, payloadReady, false],
    [installerReady, payloadReady],
  );

  const onStepChange = (target: number) => {
    if (target > step && !stepReady[target - 1]) return;
    enterStep(target);
  };

  const accountUsername =
    host?.accounts.find((item) => item.id === accountId)?.username ?? '';

  // ---------------------------------------------------------------- 渲染
  const body = (() => {
    if (step === 0) {
      return (
        <InstallerStep
          selected={installer}
          onSelect={setInstaller}
          onCached={() => undefined}
        />
      );
    }
    if (step === 1) {
      return (
        <HostStep
          installer={selectedInstaller}
          selectedHostId={host?.id ?? null}
          selectedAccountId={accountId}
          options={options}
          authKey={authKey}
          onSelectHost={(item) => {
            setHost(item);
            setAccountId(item.accounts[0]?.id ?? null);
          }}
          onSelectAccount={setAccountId}
          onChangeOptions={setOptions}
          onDeploy={startDeploy}
          deploying={deploying}
        />
      );
    }
    if (step === 2 && stream) {
      return (
        <DeployStep
          state={stream}
          hostName={stream.deployment?.hostName || host?.name || ''}
          hostAddress={stream.deployment?.hostAddress || host?.address || ''}
          accountUsername={
            stream.deployment?.accountUsername || accountUsername
          }
          logLines={stream.logs}
          onCancel={cancelDeploy}
          onRedeploy={redeploy}
          onOpenRecords={openRecords}
          onFinish={() => enterStep(3)}
        />
      );
    }
    if (step === 3 && result) {
      return <SuccessStep result={result} onAgain={freshStart} />;
    }
    return (
      <Card>
        <Alert
          type="warning"
          showIcon
          title="这一步还没有数据"
          description="请按顺序完成前面的步骤；如果刚刷新过页面，部署记录可以在右上角「查看记录」里找回。"
        />
      </Card>
    );
  })();

  const recordColumns: ColumnsType<Deployment> = [
    { title: '#', dataIndex: 'id', width: 70 },
    { title: '主机', dataIndex: 'hostName', width: 160 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (value: string) => <Tag>{value}</Tag>,
    },
    { title: '阶段', dataIndex: 'stageLabel', width: 200 },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 80,
      align: 'right',
      render: (value: number) => `${value}%`,
    },
    { title: 'Python', dataIndex: 'pythonVersion', width: 90 },
    {
      title: 'MCP 地址',
      dataIndex: 'mcpUrl',
      render: (value: string) =>
        value ? <Text code>{value}</Text> : <Text type="secondary">-</Text>,
    },
    { title: '部署人', dataIndex: 'deployedBy', width: 110 },
    { title: '创建时间', dataIndex: 'createdAt', width: 180 },
  ];

  return (
    <PageContainer
      title="ComputerUse 部署"
      subTitle="把 Windows-MCP 部署到纳管的 Windows 主机，供 AI 侧边栏操作目标机控制台"
      extra={
        <Space>
          <Button icon={<HistoryOutlined />} onClick={openRecords}>
            查看记录
          </Button>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => window.location.reload()}
          >
            重新开始
          </Button>
        </Space>
      }
    >
      <Card style={{ marginBottom: 16 }}>
        <Steps
          current={step}
          onChange={onStepChange}
          items={WIZARD_STEPS.map((item, index) => ({
            title: item.title,
            description: index === step ? item.description : undefined,
            disabled: index > 0 && !stepReady[index],
          }))}
        />
      </Card>

      {body}

      <Drawer
        title="最近的部署记录"
        width={880}
        open={recordsOpen}
        onClose={() => setRecordsOpen(false)}
        extra={
          <Button
            icon={<ReloadOutlined />}
            loading={recordsLoading}
            onClick={loadRecords}
          >
            刷新
          </Button>
        }
      >
        <Table<Deployment>
          rowKey="id"
          size="small"
          loading={recordsLoading}
          columns={recordColumns}
          dataSource={records}
          pagination={false}
          scroll={{ x: 760 }}
          locale={{ emptyText: '还没有部署记录' }}
        />
      </Drawer>

      <Space style={{ marginTop: 16 }}>
        <Button
          icon={<DesktopOutlined />}
          onClick={openConsole}
          disabled={!stream?.deployment?.mcpUrl}
        >
          打开 WebRDP 控制台
        </Button>
        <Text type="secondary">
          第 2 步的「开始部署」在第 3
          步会实时显示进度与日志；控制台要在部署成功后才能打开。
        </Text>
      </Space>
    </PageContainer>
  );
};

export default ComputerUsePage;
