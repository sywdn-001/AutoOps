/**
 * 部署向导第 4 步：成功页。
 *
 * 部署成功后要一眼确认「MCP 在哪、用什么 Python、能调哪些工具、开机自启落在哪」，
 * 所以这里用 Descriptions + 工具标签把结论摊开，并给出下一步入口：进 WebRDP 控制台
 * 看 ComputerUse 侧边栏，或者接着给另一台机器部署。
 */
import {
  CheckCircleFilled,
  DesktopOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import { history } from '@umijs/max';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Space,
  Tag,
  Typography,
} from 'antd';
import React from 'react';
import { CopyText } from '@/components/Bastion';
import type { DeployResult } from './types';

const { Text, Paragraph, Title } = Typography;

export type SuccessStepProps = {
  result: DeployResult;
  /** 重新开始一轮向导（回到第 1 步选另一台主机） */
  onAgain: () => void;
};

const SuccessStep: React.FC<SuccessStepProps> = ({ result, onAgain }) => {
  const openConsole = () => {
    const params = new URLSearchParams({
      hostId: String(result.hostId),
      title: result.hostName || result.hostAddress || 'Windows 主机',
    });
    // 用 umi 的 history（项目里其它页面都这么跳），别用 window.location 整页刷新
    history.push(`/rdp/console?${params.toString()}`);
  };

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Card>
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Title level={4} style={{ margin: 0 }}>
            <CheckCircleFilled style={{ color: '#52c41a', marginRight: 8 }} />
            ComputerUse 部署成功
          </Title>
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            {result.hostName}（{result.hostAddress}） 上的 windows-mcp
            已经跑起来并通过探活，开机自启也已经写好。
          </Paragraph>
        </Space>
      </Card>

      <Card title="部署结果">
        <Descriptions
          column={{ xs: 1, sm: 1, lg: 2 }}
          bordered
          size="middle"
          items={[
            {
              key: 'host',
              label: '目标主机',
              children: `${result.hostName}（${result.hostAddress}）`,
            },
            {
              key: 'mcpUrl',
              label: 'MCP 地址',
              children: result.mcpUrl ? (
                <CopyText text={result.mcpUrl} />
              ) : (
                <Text type="secondary">未返回</Text>
              ),
            },
            {
              key: 'python',
              label: 'Python 路径',
              children: result.pythonPath ? (
                <Text code style={{ fontSize: 12 }}>
                  {result.pythonPath}
                </Text>
              ) : (
                '-'
              ),
            },
            {
              key: 'pythonVersion',
              label: 'Python 版本',
              children: result.pythonVersion || '-',
            },
            {
              key: 'mcpVersion',
              label: 'windows-mcp 版本',
              children: result.mcpVersion || '-',
            },
            {
              key: 'mcpPort',
              label: 'MCP 端口',
              children: `${result.mcpPort}（${
                result.portproxy ? '经 portproxy 转发' : '未开 portproxy'
              }）`,
            },
            {
              key: 'vbs',
              label: '开机自启 VBS',
              children: result.vbsPath ? (
                <Text code style={{ fontSize: 12 }}>
                  {result.vbsPath}
                </Text>
              ) : (
                '-'
              ),
            },
            {
              key: 'startup',
              label: '自启快捷方式',
              children: result.startupPath ? (
                <Text code style={{ fontSize: 12 }}>
                  {result.startupPath}
                </Text>
              ) : (
                '-'
              ),
            },
            {
              key: 'tools',
              label: `MCP 工具（${result.mcpToolCount} 个）`,
              span: 'filled',
              children: result.toolNames.length ? (
                <Space size={[6, 6]} wrap>
                  {result.toolNames.map((name) => (
                    <Tag key={name} color="blue">
                      {name}
                    </Tag>
                  ))}
                </Space>
              ) : (
                <Text type="secondary">
                  后端没有返回工具名清单，可进 WebRDP 控制台让 AI 列一次工具。
                </Text>
              ),
            },
          ]}
        />
      </Card>

      <Alert
        type="info"
        showIcon
        icon={<DesktopOutlined />}
        title="ComputerUse 侧边栏已出现在 WebRDP 窗口右侧"
        description="AI 操作的是目标机的控制台会话，可通过侧边栏截图查看画面。打开下面这个入口就能看到侧边栏与实时画面。"
      />

      <Space wrap>
        <Button type="primary" icon={<DesktopOutlined />} onClick={openConsole}>
          打开 WebRDP 控制台
        </Button>
        <Button icon={<PlusOutlined />} onClick={onAgain}>
          再部署一台
        </Button>
      </Space>
    </Space>
  );
};

export default SuccessStep;
