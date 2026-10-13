/**
 * 部署向导第 2 步：选择目标主机、资产账号与部署选项。
 *
 * 只列后端 `/hosts` 给的主机；没有 WinRM 端点的主机不能选（要先在目标机上启用
 * Windows 命令行）。选项里除了端口与 pip 源，还有两个安全相关的开关：
 * 「访问密钥」（开启时在前端生成 32 位随机密钥，只展示这一次）与「监听所有网卡」
 * （不推荐：会跳过 portproxy，MCP 暴露在网络上）。
 */
import {
  CopyOutlined,
  ReloadOutlined,
  RocketOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Divider,
  Form,
  Input,
  InputNumber,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { OsTag } from '@/components/Bastion';
import {
  type ComputerUseAccount,
  type ComputerUseHost,
  computerUseApi,
} from '@/services/bastion/computeruse';
import { backendMessage, humanSize } from './helpers';
import type { DeployOptionDraft, SelectedInstaller } from './types';

const { Text, Paragraph } = Typography;

export type HostStepProps = {
  /** 第 1 步选好的安装包摘要 */
  installer: SelectedInstaller | null;
  selectedHostId: number | null;
  selectedAccountId: number | null;
  options: DeployOptionDraft;
  /** 访问密钥（由父级生成并保存，这里只展示与复制） */
  authKey: string;
  onSelectHost: (host: ComputerUseHost) => void;
  onSelectAccount: (accountId: number) => void;
  onChangeOptions: (options: DeployOptionDraft) => void;
  onDeploy: () => void;
  deploying: boolean;
};

const HostStep: React.FC<HostStepProps> = ({
  installer,
  selectedHostId,
  selectedAccountId,
  options,
  authKey,
  onSelectHost,
  onSelectAccount,
  onChangeOptions,
  onDeploy,
  deploying,
}) => {
  const { message } = App.useApp();
  const [items, setItems] = useState<ComputerUseHost[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await computerUseApi.hosts();
      setItems(result.items);
    } catch (error) {
      message.error(backendMessage(error, '主机列表加载失败，请稍后重试'));
    } finally {
      setLoading(false);
    }
  }, [message]);

  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    loadRef.current?.();
  }, []);

  const selectedHost = items.find((item) => item.id === selectedHostId) ?? null;
  const accounts: ComputerUseAccount[] = selectedHost?.accounts ?? [];
  const canDeploy = Boolean(selectedHost) && Boolean(selectedAccountId);

  const columns: ColumnsType<ComputerUseHost> = [
    {
      title: '主机',
      dataIndex: 'name',
      width: 180,
      render: (value: string, record) => (
        <Space orientation="vertical" size={0}>
          <Text strong>{value}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {record.address}
          </Text>
        </Space>
      ),
    },
    {
      title: '系统',
      dataIndex: 'osType',
      width: 110,
      render: (value: string) => <OsTag osType={value} />,
    },
    {
      title: 'WinRM',
      dataIndex: 'hasWinrm',
      width: 170,
      render: (value: boolean, record) =>
        value ? (
          <Text>
            {record.winrmPort ?? '-'} 端口
            <Text type="secondary" style={{ marginLeft: 6 }}>
              {record.winrmTransport || 'ntlm'}
            </Text>
          </Text>
        ) : (
          <Tooltip title="需要先在主机上启用 Windows 命令行（WinRM），否则堡垒机连不上目标机">
            <Tag color="error">需要先在主机上启用 Windows 命令行（WinRM）</Tag>
          </Tooltip>
        ),
    },
    {
      title: '账号数',
      dataIndex: 'accounts',
      width: 90,
      align: 'right',
      render: (value: ComputerUseAccount[]) => value.length,
    },
    {
      title: '当前状态',
      dataIndex: 'ready',
      width: 240,
      render: (_value: boolean, record) => {
        if (record.ready && record.deployment) {
          return (
            <Space size={6} wrap>
              <Tag color="success">已部署</Tag>
              <Text code style={{ fontSize: 12 }}>
                {record.deployment.mcpUrl || '-'}
              </Text>
              <Text type="secondary">
                {record.deployment.mcpToolCount} 个工具
              </Text>
            </Space>
          );
        }
        if (record.deployment) {
          return (
            <Text type="secondary">
              最近一次：
              {record.deployment.stageLabel || record.deployment.stage}（
              {record.deployment.status}）
            </Text>
          );
        }
        return <Text type="secondary">未部署</Text>;
      },
    },
  ];

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        title="第 2 步：挑一台目标 Windows 主机"
        description="部署会以所选资产账号登录目标机，静默安装 Python、pip 安装 windows-mcp、写自启 VBS，并只放行堡垒机访问 MCP 端口。"
      />

      <Card
        title="候选主机"
        extra={
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
            刷新主机
          </Button>
        }
      >
        <Table<ComputerUseHost>
          rowKey="id"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={items}
          pagination={false}
          scroll={{ x: 900 }}
          rowSelection={{
            type: 'radio',
            selectedRowKeys: selectedHostId ? [selectedHostId] : [],
            getCheckboxProps: (record) => ({ disabled: !record.hasWinrm }),
            onChange: (keys) => {
              const key = Number(keys[0] ?? 0);
              const hit = items.find((item) => item.id === key);
              if (hit) onSelectHost(hit);
            },
          }}
          locale={{
            emptyText:
              '没有可用主机，请先在「资产管理」里添加 Windows 主机并配 WinRM 端点',
          }}
        />
      </Card>

      <Card title="部署目标与选项">
        <Descriptions
          column={{ xs: 1, sm: 1, lg: 2 }}
          bordered
          size="middle"
          items={[
            {
              key: 'installer',
              label: '第 1 步选择的安装包',
              children: installer ? (
                <Space orientation="vertical" size={0}>
                  <Text strong>Python {installer.version}</Text>
                  <Text code style={{ fontSize: 12 }}>
                    {installer.filename}（{humanSize(installer.size)}）
                  </Text>
                </Space>
              ) : (
                <Text type="secondary">未选择</Text>
              ),
            },
            {
              key: 'host',
              label: '目标主机',
              children: selectedHost ? (
                <Space orientation="vertical" size={0}>
                  <Text strong>{selectedHost.name}</Text>
                  <Text type="secondary">
                    {selectedHost.address}
                    {selectedHost.hasWinrm
                      ? ` · WinRM ${selectedHost.winrmPort ?? '-'}`
                      : ' · 未启用 WinRM'}
                  </Text>
                </Space>
              ) : (
                <Text type="secondary">未选择</Text>
              ),
            },
          ]}
        />
        <Divider style={{ margin: '16px 0' }} />
        <Form layout="vertical">
          <Form.Item label="资产账号（只用后端给出的口令账号）" required>
            <Space wrap size={8}>
              {accounts.length ? (
                accounts.map((account) => (
                  <Button
                    key={account.id}
                    type={
                      account.id === selectedAccountId ? 'primary' : 'default'
                    }
                    onClick={() => onSelectAccount(account.id)}
                  >
                    {account.name}（{account.username}）
                  </Button>
                ))
              ) : (
                <Text type="secondary">
                  {selectedHost
                    ? '这台主机没有可用的口令账号，请先在主机详情里添加'
                    : '请先选择一台主机'}
                </Text>
              )}
            </Space>
          </Form.Item>

          <Space size={16} wrap align="start">
            <Form.Item label="MCP 端口">
              <InputNumber
                min={1}
                max={65535}
                value={options.mcpPort}
                onChange={(value) =>
                  onChangeOptions({
                    ...options,
                    mcpPort: Number(value) || 8000,
                  })
                }
                style={{ width: 140 }}
              />
            </Form.Item>
            <Form.Item
              label="pip 镜像源"
              tooltip="清空表示不指定，pip 会用它自己的默认源"
            >
              <Input
                value={options.pipIndex}
                placeholder="留空则不指定"
                onChange={(event) =>
                  onChangeOptions({ ...options, pipIndex: event.target.value })
                }
                style={{ width: 360 }}
                allowClear
              />
            </Form.Item>
          </Space>

          <Divider style={{ margin: '4px 0 16px' }} />

          <Form.Item
            label={
              <Space size={6}>
                <SafetyCertificateOutlined />
                <Text>启用访问密钥（推荐）</Text>
              </Space>
            }
            tooltip="开启后 MCP 只接受带 Bearer 密钥的请求，堡垒机侧边栏会用这条密钥调用"
          >
            <Switch
              checked={options.useAuthKey}
              onChange={(checked) =>
                onChangeOptions({ ...options, useAuthKey: checked })
              }
            />
          </Form.Item>

          {options.useAuthKey ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              title="这条访问密钥只展示这一次，请立即保存"
              description={
                <Space wrap size={8}>
                  <Text code style={{ fontSize: 13 }}>
                    {authKey || '（生成中…）'}
                  </Text>
                  <Tooltip title="复制密钥">
                    <Button
                      size="small"
                      icon={<CopyOutlined />}
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(authKey);
                          message.success('已复制到剪贴板');
                        } catch {
                          message.warning('浏览器未授权剪贴板，请手动复制');
                        }
                      }}
                    >
                      复制
                    </Button>
                  </Tooltip>
                </Space>
              }
            />
          ) : null}

          <Form.Item
            label={
              <Space size={6}>
                <Text>让 MCP 监听所有网卡（不推荐，跳过 portproxy）</Text>
              </Space>
            }
            tooltip="默认只监听目标机本机并由堡垒机做端口转发；开启后目标网络上任何机器都能直接连 MCP"
          >
            <Switch
              checked={options.bindAll}
              onChange={(checked) =>
                onChangeOptions({ ...options, bindAll: checked })
              }
            />
          </Form.Item>

          {options.bindAll ? (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              title="不推荐：跳过 portproxy 会让 MCP 直接暴露在目标机网络上"
              description="除非你清楚目标网的访问控制，否则请关掉这个开关。"
            />
          ) : null}

          <Space>
            <Button
              type="primary"
              icon={<RocketOutlined />}
              disabled={!canDeploy}
              loading={deploying}
              onClick={onDeploy}
            >
              开始部署
            </Button>
            {!canDeploy ? (
              <Text type="secondary">请先选择主机与资产账号，再开始部署。</Text>
            ) : (
              <Text type="secondary">
                部署过程会实时显示阶段进度与日志，期间可以取消。
              </Text>
            )}
          </Space>
        </Form>
      </Card>

      {selectedHost?.ready && selectedHost.deployment ? (
        <Alert
          type="success"
          showIcon
          title={`这台主机上已经有一份可用的部署：MCP ${selectedHost.deployment.mcpUrl || '-'}，${selectedHost.deployment.mcpToolCount} 个工具`}
          description={
            <Paragraph style={{ marginBottom: 0 }}>
              再次部署会覆盖目标机上的自启脚本与端口映射。只想看现有部署的话，
              可以返回列表页用「查看记录」。
            </Paragraph>
          }
        />
      ) : null}
    </Space>
  );
};

export default HostStep;
