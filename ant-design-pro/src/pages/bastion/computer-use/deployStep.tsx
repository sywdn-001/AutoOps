/**
 * 部署向导第 3 步：执行部署（阶段进度 + 实时日志）。
 *
 * 渲染两件事：竖向的阶段列表（当前阶段高亮，百分比绑定后端 `progress`）与终端风格的
 * 日志台（深色底、等宽、自动滚到底；info 浅灰 / success 绿 / warning 橙 / error 红）。
 * 数据来自第 1 步创建的部署记录的 SSE 流（由页面订阅后把状态传进来），
 * 本组件只管展示与「取消部署 / 查看记录 / 重新部署」这几个动作。
 */
import {
  CloseCircleOutlined,
  FileSearchOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Modal,
  Progress,
  Space,
  Steps,
  Tag,
  Typography,
} from 'antd';
import React, { useEffect, useRef, useState } from 'react';
import { CopyText } from '@/components/Bastion';
import { t } from '@/i18n';
import type { DeployStreamState } from './helpers';
import {
  buildStages,
  displayProgress,
  logLineColor,
  optionsSummary,
  stripLogDecorations,
} from './helpers';
import { DEPLOY_STATUS_META, LOGGER_STYLE } from './types';

const { Text, Paragraph } = Typography;

export type DeployStepProps = {
  state: DeployStreamState;
  /** 前一步选中的主机名（部署记录还没回来时先用它占位） */
  hostName: string;
  hostAddress: string;
  accountUsername: string;
  /** 日志台要显示的行（父级按 seq 去重后传进来） */
  logLines: string[];
  onCancel: () => Promise<void> | void;
  onRedeploy: () => void;
  /** 「查看记录」：打开最近 50 条部署记录 */
  onOpenRecords: () => void;
  onFinish: () => void;
};

const DeployStep: React.FC<DeployStepProps> = ({
  state,
  hostName,
  hostAddress,
  accountUsername,
  logLines,
  onCancel,
  onRedeploy,
  onOpenRecords,
  onFinish,
}) => {
  const { message } = App.useApp();
  const [cancelling, setCancelling] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  // 日志自动滚到底（新行进来就跟随）
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    box.scrollTop = box.scrollHeight;
  }, [logLines.length]);

  const steps = buildStages(state.stages, state.stage, state.status);
  const percent = displayProgress(state.progress, state.stage);
  const meta = DEPLOY_STATUS_META[state.status];
  const failed = state.status === 'failed';
  const cancelled = state.status === 'cancelled';
  const success = state.status === 'success';

  const confirmCancel = () => {
    Modal.confirm({
      title: t('确认取消这次部署？'),
      content: t(
        '已经做过的步骤不会回滚（Python 可能已经装好），只是停止后续阶段。要重来请再点「重新部署」。',
      ),
      okText: t('取消部署'),
      okButtonProps: { danger: true },
      cancelText: t('继续部署'),
      onOk: async () => {
        setCancelling(true);
        try {
          await onCancel();
          message.success(t('已提交取消请求'));
        } catch (error) {
          message.error(t('取消失败，可能这次部署已经结束了'));
          throw error;
        } finally {
          setCancelling(false);
        }
      },
    });
  };

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title={
          <Space size={8} wrap>
            <Text strong>
              {t('正在向 {0} 部署 ComputerUse', {
                0: hostName || t('目标主机'),
              })}
            </Text>
            <Tag color={meta.color}>{meta.text}</Tag>
            {state.live ? (
              <Tag color="processing">{t('日志流已连接')}</Tag>
            ) : null}
          </Space>
        }
        extra={
          <Space>
            <Button icon={<FileSearchOutlined />} onClick={onOpenRecords}>
              {t('查看记录')}
            </Button>
            <Button
              danger
              icon={<CloseCircleOutlined />}
              loading={cancelling}
              disabled={state.ended || !state.live}
              onClick={confirmCancel}
            >
              {t('取消部署')}
            </Button>
          </Space>
        }
      >
        <Progress
          percent={percent}
          status={failed ? 'exception' : cancelled ? 'normal' : 'active'}
          strokeColor={failed ? undefined : '#2E7BFF'}
        />
        <Paragraph type="secondary" style={{ marginTop: 8 }}>
          {t('当前阶段：{0} {1}', {
            0: state.stageLabel || t('等待开始'),
            1: state.lastSeq ? t(' · 日志事件 {0}', { 0: state.lastSeq }) : '',
          })}
        </Paragraph>
        <Steps
          direction="vertical"
          size="small"
          current={steps.findIndex((item) => item.state === 'process')}
          status={failed ? 'error' : undefined}
          items={steps.map((item) => ({
            title: item.label,
            status: item.state,
            description:
              item.state === 'finish'
                ? t('已完成（{0}%）', { 0: item.progress })
                : item.state === 'process'
                  ? t('进行中（目标 {0}%）', { 0: item.progress })
                  : item.state === 'error'
                    ? t('这一步失败了')
                    : t('等待（{0}%）', { 0: item.progress }),
          }))}
        />
      </Card>

      <Card
        title={t('实时日志')}
        size="small"
        extra={
          <Text type="secondary">
            {t('级别配色：浅灰=信息 / 绿=成功 / 橙=警告 / 红=错误')}
          </Text>
        }
      >
        <div ref={boxRef} style={LOGGER_STYLE}>
          {logLines.length ? (
            logLines.map((line) => (
              <div
                // 行里带着事件序号与时间，天然唯一（同序号的行已被状态机去重）
                key={line}
                style={{ color: logLineColor(line), whiteSpace: 'pre-wrap' }}
              >
                {stripLogDecorations(line)}
              </div>
            ))
          ) : (
            <Text style={{ color: '#6b7a99' }}>
              {t('等待目标机第一条日志…（连接目标机可能要几秒）')}
            </Text>
          )}
        </div>
      </Card>

      {failed || cancelled ? (
        <Alert
          type={failed ? 'error' : 'warning'}
          showIcon
          title={failed ? t('部署失败') : t('部署已取消')}
          description={
            <Space orientation="vertical" size={8} style={{ width: '100%' }}>
              <Text>
                {state.error ||
                  (failed
                    ? t(
                        '后端没有给出具体原因，请看上面的日志定位失败的那一步。',
                      )
                    : t('部署被取消了，目标机上可能只完成了一部分步骤。'))}
              </Text>
              <Space>
                <Button
                  type="primary"
                  icon={<ReloadOutlined />}
                  onClick={onRedeploy}
                >
                  {t('重新部署')}
                </Button>
                <Button onClick={onOpenRecords}>{t('查看记录')}</Button>
              </Space>
            </Space>
          }
        />
      ) : null}

      {success ? (
        <Alert
          type="success"
          showIcon
          title={t('部署成功，MCP 探活通过')}
          action={
            <Button type="primary" onClick={onFinish}>
              {t('下一步：查看结果')}
            </Button>
          }
        />
      ) : null}

      {state.deployment ? (
        <Card title={t('这次部署的参数')} size="small">
          <Descriptions
            column={{ xs: 1, sm: 1, lg: 2 }}
            size="small"
            items={[
              {
                key: 'host',
                label: t('目标主机'),
                children: `${hostName}（${hostAddress}）`,
              },
              {
                key: 'account',
                label: t('资产账号'),
                children: accountUsername || '-',
              },
              {
                key: 'installer',
                label: t('安装包'),
                children: (
                  <Text code style={{ fontSize: 12 }}>
                    {state.deployment.installerName || '-'}
                  </Text>
                ),
              },
              {
                key: 'options',
                label: t('部署选项'),
                children: optionsSummary(state.deployment),
              },
              {
                key: 'by',
                label: t('部署人'),
                children: state.deployment.deployedBy || '-',
              },
              {
                key: 'mcp',
                label: t('MCP 地址'),
                children: state.deployment.mcpUrl ? (
                  <CopyText text={state.deployment.mcpUrl} />
                ) : (
                  <Text type="secondary">{t('尚未探活')}</Text>
                ),
              },
            ]}
          />
        </Card>
      ) : null}
    </Space>
  );
};

export default DeployStep;
