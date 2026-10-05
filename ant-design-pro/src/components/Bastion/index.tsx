/**
 * 堡垒机业务公共展示组件（表格标签、JSON 块、命令回显等）。
 * 页面统一 import from '@/components/Bastion'。
 */
import { CopyOutlined } from '@ant-design/icons';
import { App, Button, Empty, Space, Tag, Tooltip, Typography } from 'antd';
import React from 'react';
import {
  ACTION_META,
  formatDateTime,
  RISK_META,
  SESSION_SOURCE_META,
  SESSION_STATUS_META,
  truncate,
  weekdayLabel,
} from '@/services/bastion/constants';
import type {
  CommandAction,
  RiskLevel,
  SessionSource,
  SessionStatus,
} from '@/services/bastion/types';
import { RawJson } from './jsonCards';

const { Text, Paragraph } = Typography;

/** 风险等级标签 */
export const RiskTag: React.FC<{ level?: RiskLevel | null }> = ({ level }) => {
  if (!level) return <Tag>-</Tag>;
  const meta = RISK_META[level] ?? { text: level, color: 'default' };
  return <Tag color={meta.color}>{meta.text}</Tag>;
};

/** 命令动作标签（放行/拦截） */
export const ActionTag: React.FC<{
  action?: CommandAction | string | null;
}> = ({ action }) => {
  if (!action) return <Tag>-</Tag>;
  const meta = ACTION_META[action] ?? { text: action, color: 'default' };
  return <Tag color={meta.color}>{meta.text}</Tag>;
};

/** 会话状态标签 */
export const SessionStatusTag: React.FC<{ status?: SessionStatus | null }> = ({
  status,
}) => {
  if (!status) return <Tag>-</Tag>;
  const meta = SESSION_STATUS_META[status] ?? {
    text: status,
    color: 'default',
  };
  return <Tag color={meta.color}>{meta.text}</Tag>;
};

/** 会话来源标签（网关 / 网页终端） */
export const SessionSourceTag: React.FC<{ source?: SessionSource | null }> = ({
  source,
}) => {
  if (!source) return <Tag>-</Tag>;
  const meta = SESSION_SOURCE_META[source] ?? {
    text: source,
    color: 'default',
  };
  return <Tag color={meta.color}>{meta.text}</Tag>;
};

/** 布尔值展示 */
export const BoolTag: React.FC<{
  value?: boolean | null;
  yes?: string;
  no?: string;
}> = ({ value, yes = '允许', no = '禁止' }) =>
  value ? <Tag color="success">{yes}</Tag> : <Tag color="default">{no}</Tag>;

/** 授权时段文本 */
export const WindowText: React.FC<{
  weekdays?: number[] | null;
  timeStart?: string | null;
  timeEnd?: string | null;
}> = ({ weekdays, timeStart, timeEnd }) => {
  const dayText = weekdayLabel(weekdays);
  if (!timeStart && !timeEnd)
    return <Text type="secondary">{dayText} · 全天</Text>;
  return (
    <Text type="secondary">
      {dayText} · {timeStart || '00:00'}-{timeEnd || '23:59'}
    </Text>
  );
};

/** 命令/输出单元格：单行截断 + Tooltip 全文 */
export const MonoCell: React.FC<{
  text?: string | null;
  max?: number;
  width?: number;
}> = ({ text, max = 60, width }) => {
  const value = text ?? '';
  if (!value) return <Text type="secondary">-</Text>;
  return (
    <Tooltip
      title={
        <pre style={{ margin: 0, maxHeight: 320, overflow: 'auto' }}>
          {value}
        </pre>
      }
    >
      <Text code style={{ maxWidth: width ?? 320, display: 'inline-block' }}>
        {truncate(value, max)}
      </Text>
    </Tooltip>
  );
};

/** 可复制的等宽文本（主机地址、令牌、指纹等） */
export const CopyText: React.FC<{ text?: string | null; label?: string }> = ({
  text,
  label,
}) => {
  const { message } = App.useApp();
  const value = text ?? '';
  if (!value) return <Text type="secondary">-</Text>;
  return (
    <Space size={4}>
      <Text code>{label ?? value}</Text>
      <Tooltip title="复制">
        <Button
          type="text"
          size="small"
          icon={<CopyOutlined />}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(value);
              message.success('已复制到剪贴板');
            } catch {
              message.warning('浏览器未授权剪贴板，请手动复制');
            }
          }}
        />
      </Tooltip>
    </Space>
  );
};

/** JSON 详情块（审计 detail、设置项等）—— 原样展示，可视化版本见 `JsonCards` */
export const JsonBlock: React.FC<{ value?: unknown; empty?: string }> = ({
  value,
  empty = '暂无详情',
}) => {
  if (value === null || value === undefined) {
    return <Text type="secondary">{empty}</Text>;
  }
  if (typeof value === 'object' && Object.keys(value as object).length === 0) {
    return <Text type="secondary">{empty}</Text>;
  }
  return <RawJson value={value} />;
};

export { JsonCards, JsonCell, parseMaybeJson, RawJson } from './jsonCards';
export { repairTruncatedJson, splitToolMessage } from './jsonText';
export { OsDot, OsTag, osLabel, osMeta, ProtocolTag } from './osMeta';

/** 终端回放/输出展示块 */
export const OutputBlock: React.FC<{
  text?: string | null;
  maxHeight?: number;
  empty?: string;
}> = ({ text, maxHeight = 420, empty = '（无输出）' }) => {
  const value = text ?? '';
  if (!value.trim())
    return <Empty description={empty} image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  return (
    <pre
      style={{
        margin: 0,
        padding: 12,
        background: '#0b1021',
        color: '#d6e1ff',
        borderRadius: 6,
        maxHeight,
        overflow: 'auto',
        fontSize: 12,
        lineHeight: 1.6,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
      }}
    >
      {value}
    </pre>
  );
};

/** 时间单元格 */
export const TimeCell: React.FC<{ value?: string | null }> = ({ value }) => (
  <Text style={{ whiteSpace: 'nowrap' }}>{formatDateTime(value)}</Text>
);

/** 描述项里用的正文段落 */
export const DescParagraph: React.FC<{ value?: string | null }> = ({
  value,
}) => (
  <Paragraph style={{ marginBottom: 0 }} type={value ? undefined : 'secondary'}>
    {value || '-'}
  </Paragraph>
);
