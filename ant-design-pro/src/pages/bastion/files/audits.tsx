/**
 * 文件记录页（需求：文件管理器里的每一次操作都要留痕）。
 *
 * 数据来自 `GET /api/audits/files`：浏览目录、读取、下载、上传、编辑、新建、改名/移动、
 * 复制、删除、改权限、打包下载 —— 放行的、失败的、被策略拒绝的都会有一条记录。
 */
import {
  type ActionType,
  PageContainer,
  type ProColumns,
  type ProFormInstance,
  ProTable,
} from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  App,
  Button,
  Descriptions,
  Drawer,
  Popconfirm,
  Space,
  Tag,
  Typography,
} from 'antd';
import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActionTag, MonoCell, RiskTag, TimeCell } from '@/components/Bastion';
import { type PurgePayload, fileAuditApi } from '@/services/bastion/endpoints';
import { humanAuditMessage } from '@/services/bastion/constants';
import type { FileLogItem, FileLogOptions } from '@/services/bastion/types';

const { Text } = Typography;

/** 后端返回的候选项形状（`value` 是操作名 / 动作名这类字符串） */
type NameOption = { value: string; label: string };

/**
 * 与后端 `_filter_file_logs` 同名：清除时只透传这些筛选字段，
 * 避免把 `current`/`pageSize` 当删除条件。
 */
const PURGE_FILTER_KEYS = [
  'operation',
  'action',
  'result',
  'riskLevel',
  'username',
  'hostName',
  'sid',
  'keyword',
] as const;

const formatSize = (bytes?: number): string => {
  const value = Number(bytes || 0);
  if (!value) {
    return '0 B';
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  let size = value;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${index === 0 ? size : size.toFixed(size >= 100 ? 0 : 1)} ${units[index]}`;
};

const renderResult = (result: string) => {
  if (result === 'success') {
    return <Tag color="success">成功</Tag>;
  }
  if (result === 'denied') {
    return <Tag color="error">被拦截</Tag>;
  }
  if (result === 'failure') {
    return <Tag color="warning">失败</Tag>;
  }
  return <Tag>{result || '-'}</Tag>;
};

const FileAuditsPage: React.FC = () => {
  const { message } = App.useApp();
  const access = useAccess();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const [options, setOptions] = useState<FileLogOptions | undefined>(undefined);
  const [detail, setDetail] = useState<FileLogItem | undefined>(undefined);
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [purging, setPurging] = useState(false);

  const canPurge = Boolean(access.canCommandViewAll);

  useEffect(() => {
    let cancelled = false;
    fileAuditApi
      .options()
      .then((payload) => {
        if (!cancelled) {
          setOptions(payload);
        }
      })
      .catch((error) => console.debug('load file log options failed', error));
    return () => {
      cancelled = true;
    };
  }, []);

  const operationLabel = useCallback(
    (value: string) =>
      options?.operations?.find((item) => item.value === value)?.label || value || '-',
    [options],
  );

  /** 当前搜索条件（只取后端认识的字段）。 */
  const currentFilters = useCallback((): PurgePayload => {
    const values = (formRef.current?.getFieldsValue?.() ?? {}) as Record<string, unknown>;
    const payload: PurgePayload = {};
    for (const key of PURGE_FILTER_KEYS) {
      const value = values[key];
      if (value !== undefined && value !== null && value !== '') {
        payload[key] = value;
      }
    }
    return payload;
  }, []);

  const runPurge = useCallback(
    async (payload: PurgePayload) => {
      setPurging(true);
      try {
        const result = await fileAuditApi.purge(payload);
        message.success(`已清除 ${result.deleted} 条文件操作记录`);
        setSelectedKeys([]);
        actionRef.current?.reload();
      } catch (error) {
        message.error((error as Error)?.message || '清除文件记录失败');
      } finally {
        setPurging(false);
      }
    },
    [message],
  );

  const columns = useMemo<ProColumns<FileLogItem>[]>(() => {
    const toValueOptions = (items?: NameOption[]) =>
      (items ?? []).map((item) => ({ label: item.label, value: item.value }));
    const toTextOptions = (items?: string[]) =>
      (items ?? []).map((item) => ({ label: item, value: item }));

    return [
      {
        title: '时间',
        dataIndex: 'startedAt',
        valueType: 'dateTime',
        width: 180,
        search: false,
        render: (_, row) => <TimeCell value={row.startedAt} />,
      },
      {
        title: '用户',
        dataIndex: 'username',
        width: 120,
        valueType: 'select',
        fieldProps: { options: toTextOptions(options?.usernames), showSearch: true },
      },
      {
        title: '主机',
        dataIndex: 'hostName',
        width: 150,
        valueType: 'select',
        fieldProps: { options: toTextOptions(options?.hosts), showSearch: true },
      },
      {
        title: '操作',
        dataIndex: 'operation',
        width: 130,
        valueType: 'select',
        fieldProps: { options: toValueOptions(options?.operations) },
        render: (_, row) => operationLabel(row.operation),
      },
      {
        title: '路径',
        dataIndex: 'path',
        search: false,
        render: (_, row) => (
          <Space direction="vertical" size={0}>
            <MonoCell text={row.path} />
            {row.targetPath ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                → <Text code>{row.targetPath}</Text>
              </Text>
            ) : null}
          </Space>
        ),
      },
      {
        title: '动作',
        dataIndex: 'action',
        width: 90,
        valueType: 'select',
        fieldProps: { options: toValueOptions(options?.actions) },
        render: (_, row) => <ActionTag action={row.action} />,
      },
      {
        title: '风险',
        dataIndex: 'riskLevel',
        width: 90,
        valueType: 'select',
        fieldProps: { options: toValueOptions(options?.riskLevels) },
        render: (_, row) => <RiskTag level={row.riskLevel} />,
      },
      {
        title: '结果',
        dataIndex: 'result',
        width: 90,
        valueType: 'select',
        fieldProps: { options: toValueOptions(options?.results) },
        render: (_, row) => renderResult(row.result),
      },
      {
        title: '大小',
        dataIndex: 'size',
        width: 90,
        search: false,
        render: (_, row) => formatSize(row.size),
      },
      {
        title: '说明',
        dataIndex: 'message',
        ellipsis: true,
        search: false,
        render: (_, row) => humanAuditMessage(row.message) || row.reason || '-',
      },
      {
        title: '会话号',
        dataIndex: 'sid',
        search: false,
        width: 140,
        render: (_, row) => <MonoCell text={row.sid} />,
      },
      {
        title: '关键字',
        dataIndex: 'keyword',
        hideInTable: true,
        fieldProps: { placeholder: '路径 / 说明 / 用户 / 主机' },
      },
      {
        title: '操作',
        valueType: 'option',
        width: 80,
        render: (_, row) => [
          <Button key="detail" type="link" size="small" onClick={() => setDetail(row)}>
            详情
          </Button>,
        ],
      },
    ];
  }, [operationLabel, options]);

  return (
    <PageContainer title="文件记录" subTitle="文件管理器里的每一次操作">
      <ProTable<FileLogItem>
        rowKey="id"
        actionRef={actionRef}
        formRef={formRef}
        search={{ labelWidth: 'auto' }}
        options={false}
        cardBordered
        pagination={{ pageSize: 20, showSizeChanger: true }}
        rowSelection={{
          selectedRowKeys: selectedKeys,
          onChange: setSelectedKeys,
        }}
        toolbar={{
          actions: canPurge
            ? [
                <Popconfirm
                  key="purge-selected"
                  title={`清除选中的 ${selectedKeys.length} 条记录？`}
                  description="清除动作本身也会写一条审计日志。"
                  disabled={!selectedKeys.length}
                  onConfirm={() => runPurge({ ids: selectedKeys as number[] })}
                >
                  <Button danger loading={purging} disabled={!selectedKeys.length}>
                    清除选中
                  </Button>
                </Popconfirm>,
                <Popconfirm
                  key="purge-filtered"
                  title="清除当前筛选结果？"
                  description="不选筛选条件时会清空全部文件操作记录（含会话录像无关，仅记录）。"
                  onConfirm={() => runPurge({ all: true, ...currentFilters() })}
                >
                  <Button danger loading={purging}>
                    清除筛选结果
                  </Button>
                </Popconfirm>,
              ]
            : [],
        }}
        columns={columns}
        request={async (params) => {
          try {
            const res = await fileAuditApi.list(params);
            return { data: res.data ?? [], total: res.total ?? 0, success: true };
          } catch (error) {
            message.error((error as Error)?.message || '加载文件记录失败');
            return { data: [], total: 0, success: false };
          }
        }}
      />

      <Drawer
        open={Boolean(detail)}
        width={680}
        title="文件操作详情"
        onClose={() => setDetail(undefined)}
        destroyOnHidden
      >
        {detail ? (
          <Descriptions
            column={1}
            size="small"
            bordered
            items={[
              { key: 'time', label: '时间', children: <TimeCell value={detail.startedAt} /> },
              { key: 'user', label: '用户', children: detail.username || '-' },
              { key: 'host', label: '主机', children: detail.hostName || '-' },
              {
                key: 'operation',
                label: '操作',
                children: operationLabel(detail.operation),
              },
              { key: 'path', label: '路径', children: <MonoCell text={detail.path} /> },
              ...(detail.targetPath
                ? [
                    {
                      key: 'target',
                      label: '目标路径',
                      children: <MonoCell text={detail.targetPath} />,
                    },
                  ]
                : []),
              { key: 'action', label: '策略动作', children: <ActionTag action={detail.action} /> },
              { key: 'risk', label: '风险等级', children: <RiskTag level={detail.riskLevel} /> },
              { key: 'result', label: '执行结果', children: renderResult(detail.result) },
              {
                key: 'rule',
                label: '命中规则',
                children: detail.matchedRuleId ? (
                  <Space size={8} wrap>
                    <Tag color="purple">#{detail.matchedRuleId}</Tag>
                    <MonoCell text={detail.matchedRulePattern} />
                  </Space>
                ) : (
                  <Text type="secondary">未命中规则</Text>
                ),
              },
              { key: 'reason', label: '判定说明', children: detail.reason || '-' },
              { key: 'message', label: '执行信息', children: humanAuditMessage(detail.message) || '-' },
              {
                key: 'size',
                label: '大小',
                children: `${formatSize(detail.size)}${detail.fileCount > 1 ? ` · ${detail.fileCount} 个对象` : ''}`,
              },
              { key: 'duration', label: '耗时', children: `${detail.durationMs} ms` },
              { key: 'sid', label: '会话号', children: <MonoCell text={detail.sid} /> },
              { key: 'seq', label: '会话内序号', children: detail.seq },
            ]}
          />
        ) : null}
      </Drawer>
    </PageContainer>
  );
};

export default FileAuditsPage;
