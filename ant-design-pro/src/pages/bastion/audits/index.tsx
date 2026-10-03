/**
 * 审计中心 · 操作日志
 * 展示登录登出、资产与授权变更、策略变更、用户与角色变更、会话与命令的审计流水。
 */

import type {
  ActionType,
  ProColumns,
  ProFormInstance,
} from '@ant-design/pro-components';
import { PageContainer, ProTable } from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  Button,
  Descriptions,
  Drawer,
  message,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  DescParagraph,
  JsonBlock,
  MonoCell,
  TimeCell,
} from '@/components/Bastion';
import type { PurgePayload } from '@/services/bastion/endpoints';
import { auditApi } from '@/services/bastion/endpoints';
import { humanAuditMessage } from '@/services/bastion/constants';
import type { AuditItem } from '@/services/bastion/types';

const { Text } = Typography;

type AuditOptionPayload = Awaited<ReturnType<typeof auditApi.options>>;

const RESULT_OPTIONS = [
  { label: '成功', value: 'success' },
  { label: '失败', value: 'failure' },
];

/**
 * 与后端 `_filter_audits` 同名：清除时只透传这些筛选字段，
 * 避免把 `current`/`pageSize` 之类分页参数当成删除条件。
 */
const PURGE_FILTER_KEYS = [
  'category',
  'action',
  'result',
  'actorUsername',
  'targetType',
  'keyword',
] as const;

const renderResult = (result: string) => {
  if (result === 'success') {
    return <Tag color="success">success</Tag>;
  }
  if (result === 'failure') {
    return <Tag color="error">failure</Tag>;
  }
  return <Tag>{result || '-'}</Tag>;
};

const AuditsPage: React.FC = () => {
  const access = useAccess();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const [auditOptions, setAuditOptions] = useState<
    AuditOptionPayload | undefined
  >(undefined);
  const [detail, setDetail] = useState<AuditItem | undefined>(undefined);
  const [detailLoading, setDetailLoading] = useState(false);
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [total, setTotal] = useState(0);
  const [purging, setPurging] = useState(false);

  /** 当前搜索条件（只取后端认识的字段）。 */
  const currentFilters = useCallback((): PurgePayload => {
    const values = (formRef.current?.getFieldsValue?.() ?? {}) as Record<
      string,
      unknown
    >;
    const payload: PurgePayload = {};
    for (const key of PURGE_FILTER_KEYS) {
      const value = values[key];
      if (value !== undefined && value !== null && value !== '') {
        payload[key] = value;
      }
    }
    return payload;
  }, []);

  const runPurge = useCallback(async (payload: PurgePayload) => {
    setPurging(true);
    try {
      const result = await auditApi.purge(payload);
      message.success(`已清除 ${result.deleted} 条审计日志`);
      setSelectedKeys([]);
      actionRef.current?.reload();
    } catch (error) {
      console.debug('purge audits failed', error);
    } finally {
      setPurging(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadOptions = async () => {
      try {
        const payload = await auditApi.options();
        if (!cancelled) {
          setAuditOptions(payload);
        }
      } catch (error) {
        console.debug('load audit options failed', error);
      }
    };
    void loadOptions();
    return () => {
      cancelled = true;
    };
  }, []);

  const openDetail = useCallback(async (id: number) => {
    setDetailLoading(true);
    setDetail(undefined);
    try {
      const item = await auditApi.get(id);
      setDetail(item);
    } catch (error) {
      console.debug('load audit detail failed', error);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const categoryOptions =
    auditOptions?.categories.map((item) => ({ label: item, value: item })) ??
    [];
  const actionOptions =
    auditOptions?.actions.map((item) => ({ label: item, value: item })) ?? [];

  const columns: ProColumns<AuditItem>[] = [
    {
      title: '时间',
      dataIndex: 'ts',
      width: 180,
      search: false,
      render: (_, record) => <TimeCell value={record.ts} />,
    },
    categoryOptions.length > 0
      ? {
          title: '类别',
          dataIndex: 'category',
          width: 140,
          valueType: 'select',
          fieldProps: { options: categoryOptions, placeholder: '全部类别' },
        }
      : {
          title: '类别',
          dataIndex: 'category',
          width: 140,
          fieldProps: { placeholder: '类别' },
        },
    actionOptions.length > 0
      ? {
          title: '动作',
          dataIndex: 'action',
          width: 130,
          valueType: 'select',
          fieldProps: { options: actionOptions, placeholder: '全部动作' },
        }
      : {
          title: '动作',
          dataIndex: 'action',
          width: 130,
          fieldProps: { placeholder: '动作' },
        },
    {
      title: '操作人',
      dataIndex: 'actorUsername',
      width: 170,
      fieldProps: { placeholder: '操作人' },
      render: (_, record) =>
        record.actorRole
          ? `${record.actorUsername}（${record.actorRole}）`
          : record.actorUsername,
    },
    {
      title: '目标',
      key: 'target',
      width: 220,
      search: false,
      render: (_, record) => (
        <span>
          {record.targetName || '-'}
          {record.targetType ? (
            <Text type="secondary"> · {record.targetType}</Text>
          ) : null}
        </span>
      ),
    },
    {
      title: '目标类型',
      dataIndex: 'targetType',
      hideInTable: true,
      fieldProps: { placeholder: '目标类型' },
    },
    {
      title: '结果',
      dataIndex: 'result',
      width: 100,
      valueType: 'select',
      fieldProps: { options: RESULT_OPTIONS, placeholder: '全部结果' },
      render: (_, record) => renderResult(record.result),
    },
    {
      title: '消息',
      dataIndex: 'message',
      width: 320,
      search: false,
      render: (_, record) => <MonoCell text={humanAuditMessage(record.message)} max={80} />,
    },
    { title: 'IP', dataIndex: 'ip', width: 150, search: false },
    {
      title: '关键字',
      dataIndex: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: '消息 / 目标 / 操作人' },
    },
    {
      title: '操作',
      valueType: 'option',
      width: 90,
      fixed: 'right',
      render: (_, record) => [
        <Button
          key="detail"
          type="link"
          size="small"
          onClick={() => void openDetail(record.id)}
        >
          详情
        </Button>,
      ],
    },
  ];

  return (
    <PageContainer>
      <ProTable<AuditItem>
        rowKey="id"
        headerTitle="操作日志"
        actionRef={actionRef}
        formRef={formRef}
        columns={columns}
        search={{ labelWidth: 'auto' }}
        pagination={{ defaultPageSize: 10 }}
        scroll={{ x: 'max-content' }}
        rowSelection={
          access.canAdmin
            ? {
                selectedRowKeys: selectedKeys,
                onChange: setSelectedKeys,
                preserveSelectedRowKeys: true,
              }
            : undefined
        }
        request={async (params) => {
          const res = await auditApi.list(params);
          setTotal(res.total ?? 0);
          return { data: res.data, total: res.total, success: res.success };
        }}
        toolBarRender={() => [
          <Button key="refresh" onClick={() => actionRef.current?.reload()}>
            刷新
          </Button>,
          access.canAdmin ? (
            <Popconfirm
              key="purge-selected"
              title="删除选中的审计日志？"
              description={`将永久删除 ${selectedKeys.length} 条记录，无法恢复。`}
              okText="删除"
              okButtonProps={{ danger: true }}
              disabled={selectedKeys.length === 0}
              onConfirm={() =>
                runPurge({ ids: selectedKeys.map((key) => Number(key)) })
              }
            >
              <Button
                danger
                disabled={selectedKeys.length === 0}
                loading={purging}
              >
                删除选中
                {selectedKeys.length > 0 ? `（${selectedKeys.length}）` : ''}
              </Button>
            </Popconfirm>
          ) : null,
          access.canAdmin ? (
            <Popconfirm
              key="purge-filtered"
              title="清除当前筛选结果？"
              description={
                <>
                  将按当前搜索条件永久删除匹配的全部审计日志（当前共 {total}{' '}
                  条），无法恢复。未设置筛选条件时等同于清空全部。
                  <br />
                  清除动作本身会写入一条审计留痕。
                </>
              }
              okText="清除"
              okButtonProps={{ danger: true }}
              onConfirm={() => runPurge({ all: true, ...currentFilters() })}
            >
              <Button danger type="primary" ghost loading={purging}>
                清除筛选结果
              </Button>
            </Popconfirm>
          ) : null,
        ]}
      />
      <Drawer
        width={760}
        open={detailLoading || Boolean(detail)}
        title={detail ? `审计详情 · ${detail.category}` : '审计详情'}
        onClose={() => setDetail(undefined)}
      >
        {detailLoading && !detail ? <Spin /> : null}
        {detail ? (
          <Space orientation="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions
              column={2}
              bordered
              size="small"
              items={[
                { key: 'id', label: '日志 ID', children: detail.id },
                {
                  key: 'ts',
                  label: '时间',
                  children: <TimeCell value={detail.ts} />,
                },
                {
                  key: 'category',
                  label: '类别',
                  children: detail.category || '-',
                },
                {
                  key: 'action',
                  label: '动作',
                  children: detail.action || '-',
                },
                {
                  key: 'actorUsername',
                  label: '操作人',
                  children: detail.actorUsername || '-',
                },
                {
                  key: 'actorRole',
                  label: '操作人角色',
                  children: detail.actorRole || '-',
                },
                {
                  key: 'actorId',
                  label: '操作人 ID',
                  children: detail.actorId ?? '-',
                },
                {
                  key: 'targetType',
                  label: '目标类型',
                  children: detail.targetType || '-',
                },
                {
                  key: 'targetId',
                  label: '目标 ID',
                  children: detail.targetId ?? '-',
                },
                {
                  key: 'targetName',
                  label: '目标名称',
                  children: detail.targetName || '-',
                },
                {
                  key: 'result',
                  label: '结果',
                  children: renderResult(detail.result),
                },
                { key: 'ip', label: '来源 IP', children: detail.ip || '-' },
                {
                  key: 'message',
                  label: '消息',
                  span: 2,
                  children: <DescParagraph value={humanAuditMessage(detail.message)} />,
                },
                {
                  key: 'userAgent',
                  label: 'User-Agent',
                  span: 2,
                  children: <DescParagraph value={detail.userAgent} />,
                },
              ]}
            />
            <div>
              <Text strong>详情数据</Text>
              <div style={{ marginTop: 8 }}>
                <JsonBlock value={detail.detail} />
              </div>
            </div>
          </Space>
        ) : null}
      </Drawer>
    </PageContainer>
  );
};

export default AuditsPage;
