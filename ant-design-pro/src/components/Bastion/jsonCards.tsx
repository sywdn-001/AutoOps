/**
 * JSON → 卡片（审计页、对话详情用）。
 *
 * 为什么不是 `<pre>{JSON.stringify(...)}</pre>`：审计页上「工具入参 / 工具结果 / 消息正文」
 * 往往是几 KB 的 JSON，直接堆在页面上人只能肉眼解析，翻十屏也看不出重点。这里按数据结构
 * 挑最合适的 antd 组件：
 *
 * - 键值对象 → `Descriptions`（标量进表格，嵌套项单独成块）；
 * - 对象数组 → `Table`（列取并集，嵌套单元格用气泡展开）；
 * - 标量数组 → `Tag` 组；
 * - 过长文本 → 截断 + 悬浮看全文；
 * - 层数超限 → 原样 JSON，不递归到天荒地老。
 *
 * **审计口径不因为好看而让步**：任何一层都能点「原始 JSON」看到未经改写的原文，
 * 可视化只是换个看法，不隐藏证据。
 */
import {
  Descriptions,
  Popover,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import React from 'react';
import { parseMaybeJson } from './jsonText';

export {
  parseMaybeJson,
  repairTruncatedJson,
  splitToolMessage,
} from './jsonText';

const { Text, Paragraph } = Typography;

/** 递归层数上限，超过就退回原始 JSON */
const MAX_DEPTH = 4;
/** 单元格里字符串超过这个长度就截断 + 悬浮看全文 */
const CELL_TEXT_LIMIT = 80;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);

const isScalar = (value: unknown): boolean =>
  value === null || ['string', 'number', 'boolean'].includes(typeof value);

const scalarText = (value: unknown): string => {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
};

/** 原样 JSON 块（`JsonBlock` 的实现底座，审计兜底视图） */
export const RawJson: React.FC<{ value: unknown; maxHeight?: number }> = ({
  value,
  maxHeight = 360,
}) => (
  <pre
    style={{
      margin: 0,
      padding: 12,
      background: 'rgba(0,0,0,0.03)',
      borderRadius: 6,
      maxHeight,
      overflow: 'auto',
      fontSize: 12,
      lineHeight: 1.6,
    }}
  >
    {value === undefined ? 'undefined' : JSON.stringify(value, null, 2)}
  </pre>
);

/** 渲染用稳定 key：优先业务标识，退化成位置 + 内容摘要（避免用数组下标当 key） */
const rowKey = (row: unknown, position: string): string => {
  if (isRecord(row)) {
    for (const field of [
      'id',
      'key',
      'code',
      'name',
      'username',
      'title',
      'label',
    ]) {
      const value = row[field];
      if (value !== undefined && value !== null && isScalar(value))
        return `${field}:${String(value)}`;
    }
  }
  return `#${position}:${scalarText(row).slice(0, 24)}`;
};

const LongText: React.FC<{ value: string; lines?: number }> = ({
  value,
  lines,
}) => {
  if (value.length <= CELL_TEXT_LIMIT || lines === undefined) {
    return (
      <Tooltip title={value.length > CELL_TEXT_LIMIT ? value : undefined}>
        <Text style={{ wordBreak: 'break-all' }}>{value}</Text>
      </Tooltip>
    );
  }
  return (
    <Paragraph
      style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}
      ellipsis={{ rows: lines, expandable: true }}
    >
      {value}
    </Paragraph>
  );
};

/** 表格单元格 / 键值项里的单个值 */
export const JsonCell: React.FC<{ value: unknown }> = ({ value }) => {
  if (value === null || value === undefined || value === '')
    return <Text type="secondary">-</Text>;
  if (typeof value === 'boolean')
    return (
      <Tag color={value ? 'success' : 'default'}>{value ? '是' : '否'}</Tag>
    );
  if (isScalar(value)) return <LongText value={String(value)} />;
  const size = Array.isArray(value)
    ? value.length
    : Object.keys(value as object).length;
  const label = Array.isArray(value)
    ? `数组 · ${size} 项`
    : `对象 · ${size} 个字段`;
  return (
    <Popover
      trigger="click"
      placement="right"
      title="嵌套内容"
      content={
        <div style={{ maxWidth: 560, maxHeight: 420, overflow: 'auto' }}>
          <JsonCards value={value} compact />
        </div>
      }
    >
      <a style={{ fontSize: 12 }}>{label}</a>
    </Popover>
  );
};

const RawToggle: React.FC<{ value: unknown }> = ({ value }) => (
  <Popover
    trigger="click"
    placement="bottom"
    title="原始 JSON（未经渲染改写）"
    content={
      <div style={{ maxWidth: 640, maxHeight: 480, overflow: 'auto' }}>
        <RawJson value={value} maxHeight={460} />
      </div>
    }
  >
    <a style={{ fontSize: 12 }}>原始 JSON</a>
  </Popover>
);

const columnsFromRows = (rows: unknown[]): { key: string; title: string }[] => {
  const keys: string[] = [];
  let widestArray = 0;
  for (const row of rows) {
    if (isRecord(row)) {
      for (const key of Object.keys(row))
        if (!keys.includes(key)) keys.push(key);
    } else if (Array.isArray(row)) {
      widestArray = Math.max(widestArray, row.length);
    }
  }
  if (keys.length === 0)
    return Array.from({ length: widestArray }, (_, i) => ({
      key: String(i),
      title: `[${i}]`,
    }));
  return keys.map((key) => ({ key, title: key }));
};

/**
 * 把任意 JSON 渲染成卡片。
 *
 * @param value   JSON 值，或装着 JSON 的字符串
 * @param compact 紧凑模式（放在单元格/气泡里），不显示标题与原始 JSON 入口
 */
export const JsonCards: React.FC<{
  value: unknown;
  title?: React.ReactNode;
  empty?: string;
  compact?: boolean;
  depth?: number;
}> = ({ value, title, empty = '（空）', compact = false, depth = 0 }) => {
  const { data, json, truncated } = parseMaybeJson(value);

  if (data === null || data === undefined || data === '') {
    return <Text type="secondary">{empty}</Text>;
  }

  if (!json) {
    if (typeof data === 'string') {
      return depth > 0 ? (
        <LongText value={data} lines={4} />
      ) : (
        <LongText value={data} lines={12} />
      );
    }
    return <Text>{scalarText(data)}</Text>;
  }

  if (depth >= MAX_DEPTH) {
    return (
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          层级过深，改为原样展示
        </Text>
        <RawJson value={data} maxHeight={240} />
      </Space>
    );
  }

  const header = (
    <Space size={8} wrap>
      {title ? <Text strong>{title}</Text> : null}
      {truncated ? (
        <Tag color="warning">后端已截断，这里按完整条目渲染</Tag>
      ) : null}
      {compact ? null : <RawToggle value={data} />}
    </Space>
  );

  const body = (() => {
    if (Array.isArray(data)) {
      if (data.length === 0) return <Text type="secondary">{empty}</Text>;
      if (data.every((item) => isScalar(item))) {
        return (
          <Space size={4} wrap>
            {Object.entries(data).map(([position, item]) => (
              <Tag key={`${position}:${scalarText(item)}`}>
                {scalarText(item)}
              </Tag>
            ))}
          </Space>
        );
      }
      const columns = columnsFromRows(data);
      return (
        <Table
          size="small"
          rowKey={(row, index) => rowKey(row, String(index))}
          columns={columns.map((column) => ({
            title: column.title,
            dataIndex: column.key,
            key: column.key,
            ellipsis: true,
            render: (cell: unknown) => <JsonCell value={cell} />,
          }))}
          dataSource={data.map((row, index) =>
            isRecord(row)
              ? { __key: String(index), ...row }
              : { __key: String(index), value: row },
          )}
          pagination={
            data.length > 10 ? { pageSize: 10, size: 'small' } : false
          }
          scroll={{ x: 'max-content' }}
        />
      );
    }

    const record = data as Record<string, unknown>;
    const entries = Object.entries(record);
    if (entries.length === 0) return <Text type="secondary">{empty}</Text>;
    const scalars = entries.filter(([, item]) => isScalar(item));
    const nested = entries.filter(([, item]) => !isScalar(item));
    return (
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        {scalars.length > 0 ? (
          <Descriptions
            size="small"
            column={1}
            bordered={!compact}
            items={scalars.map(([label, item]) => ({
              key: label,
              label: <Text code>{label}</Text>,
              children: <JsonCell value={item} />,
            }))}
          />
        ) : null}
        {nested.map(([label, item]) => (
          <div
            key={label}
            style={{
              borderLeft: '2px solid rgba(0,0,0,0.06)',
              paddingLeft: 12,
            }}
          >
            <JsonCards value={item} title={label} depth={depth + 1} />
          </div>
        ))}
      </Space>
    );
  })();

  return (
    <div style={{ width: '100%' }}>
      {header}
      <div style={{ marginTop: compact ? 4 : 8 }}>{body}</div>
    </div>
  );
};
