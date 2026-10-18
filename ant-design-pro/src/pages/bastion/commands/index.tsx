/**
 * 审计中心 · 命令记录
 * 按会话维度平铺全部命令，支持过滤与查看单条命令的完整输入输出。
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
  Typography,
} from 'antd';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActionTag,
  CopyText,
  MonoCell,
  OutputBlock,
  RiskTag,
  TimeCell,
} from '@/components/Bastion';
import { t } from '@/i18n';
import {
  ACTION_OPTIONS,
  formatLatency,
  RISK_OPTIONS,
  toOptions,
} from '@/services/bastion/constants';
import type { PurgePayload } from '@/services/bastion/endpoints';
import { commandApi, hostApi } from '@/services/bastion/endpoints';
import type { CommandItem } from '@/services/bastion/types';

const { Text } = Typography;

/** 与后端 `_filter_commands` 同名：清除时只透传这些筛选字段。 */
const PURGE_FILTER_KEYS = [
  'sessionId',
  'username',
  'hostId',
  'action',
  'riskLevel',
  'keyword',
] as const;

const COMMAND_PRE_STYLE: React.CSSProperties = {
  margin: 0,
  padding: 12,
  background: 'rgba(0,0,0,0.03)',
  borderRadius: 6,
  maxHeight: 240,
  overflow: 'auto',
  fontSize: 12,
  lineHeight: 1.6,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-all',
};

const CommandsPage: React.FC = () => {
  const access = useAccess();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const formRef = useRef<ProFormInstance | undefined>(undefined);
  const [hostOptions, setHostOptions] = useState<
    { label: string; value: unknown }[]
  >([]);
  const [deniedOnPage, setDeniedOnPage] = useState(0);
  const [detail, setDetail] = useState<CommandItem | undefined>(undefined);
  const [detailLoading, setDetailLoading] = useState(false);
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [total, setTotal] = useState(0);
  const [purging, setPurging] = useState(false);

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
      const result = await commandApi.purge(payload);
      const extra =
        result.commands !== undefined
          ? t('，命令 {0} 条', { 0: result.commands })
          : '';
      message.success(
        t('已清除 {0} 条命令记录{1}', { 0: result.deleted, 1: extra }),
      );
      setSelectedKeys([]);
      actionRef.current?.reload();
    } catch (error) {
      console.debug('purge commands failed', error);
    } finally {
      setPurging(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadHostOptions = async () => {
      try {
        const rows = await hostApi.options();
        if (!cancelled) {
          setHostOptions(toOptions(rows));
        }
      } catch (error) {
        console.debug('load host options failed', error);
      }
    };
    void loadHostOptions();
    return () => {
      cancelled = true;
    };
  }, []);

  const openOutput = useCallback(async (id: number) => {
    setDetailLoading(true);
    setDetail(undefined);
    try {
      const item = await commandApi.get(id);
      setDetail(item);
    } catch (error) {
      console.debug('load command detail failed', error);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const columns: ProColumns<CommandItem>[] = [
    {
      title: t('时间'),
      dataIndex: 'startedAt',
      width: 180,
      search: false,
      render: (_, record) => <TimeCell value={record.startedAt} />,
    },
    {
      title: t('用户'),
      dataIndex: 'username',
      width: 120,
      fieldProps: { placeholder: t('用户名') },
    },
    { title: t('主机'), dataIndex: 'hostName', width: 160, search: false },
    {
      title: t('命令'),
      dataIndex: 'command',
      width: 320,
      search: false,
      render: (_, record) => <MonoCell text={record.command} max={80} />,
    },
    {
      title: t('动作'),
      dataIndex: 'action',
      width: 90,
      valueType: 'select',
      fieldProps: { options: ACTION_OPTIONS, placeholder: t('全部动作') },
      render: (_, record) => <ActionTag action={record.action} />,
    },
    {
      title: t('风险'),
      dataIndex: 'riskLevel',
      width: 100,
      valueType: 'select',
      fieldProps: { options: RISK_OPTIONS, placeholder: t('全部风险等级') },
      render: (_, record) => <RiskTag level={record.riskLevel} />,
    },
    {
      title: t('命中规则'),
      dataIndex: 'matchedRulePattern',
      width: 220,
      search: false,
      render: (_, record) => (
        <MonoCell text={record.matchedRulePattern} max={60} />
      ),
    },
    {
      title: t('耗时'),
      dataIndex: 'durationMs',
      width: 100,
      search: false,
      render: (_, record) => formatLatency(record.durationMs),
    },
    {
      title: t('会话号'),
      dataIndex: 'sid',
      width: 190,
      search: false,
      render: (_, record) => <CopyText text={record.sid} />,
    },
    {
      title: t('关键字'),
      dataIndex: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: t('命令内容 / 命中规则') },
    },
    {
      title: t('主机筛选'),
      dataIndex: 'hostId',
      hideInTable: true,
      valueType: 'select',
      fieldProps: {
        options: hostOptions,
        showSearch: true,
        optionFilterProp: 'label',
        allowClear: true,
        placeholder: t('请选择主机'),
      },
    },
    {
      title: t('操作'),
      valueType: 'option',
      width: 110,
      fixed: 'right',
      render: (_, record) => [
        <Button
          key="output"
          type="link"
          size="small"
          onClick={() => void openOutput(record.id)}
        >
          {t('查看输出')}
        </Button>,
      ],
    },
  ];

  return (
    <PageContainer>
      <ProTable<CommandItem>
        rowKey="id"
        headerTitle={t('命令记录')}
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
          const res = await commandApi.list(params);
          const denied = res.data.filter(
            (item) => item.action === 'deny',
          ).length;
          setDeniedOnPage((prev) => (prev === denied ? prev : denied));
          setTotal(res.total ?? 0);
          return { data: res.data, total: res.total, success: res.success };
        }}
        toolBarRender={() => [
          <Text key="stat" type="secondary">
            {t('本页拦截 {0} 条', { 0: deniedOnPage })}
          </Text>,
          <Button key="refresh" onClick={() => actionRef.current?.reload()}>
            {t('刷新')}
          </Button>,
          access.canAdmin ? (
            <Popconfirm
              key="purge-selected"
              title={t('删除选中的命令记录？')}
              description={t('将永久删除 {0} 条记录，无法恢复。', {
                0: selectedKeys.length,
              })}
              okText={t('删除')}
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
                {t('删除选中 {0}', {
                  0:
                    selectedKeys.length > 0 ? `（${selectedKeys.length}）` : '',
                })}
              </Button>
            </Popconfirm>
          ) : null,
          access.canAdmin ? (
            <Popconfirm
              key="purge-filtered"
              title={t('清除当前筛选结果？')}
              description={
                <>
                  {t(
                    '将按当前搜索条件永久删除匹配的全部命令记录（当前共 {0}{1} 条），无法恢复。未设置筛选条件时等同于清空全部。',
                    { 0: total, 1: ' ' },
                  )}
                  <br />
                  {t('清除动作本身会写入一条审计留痕，会话录像不受影响。')}
                </>
              }
              okText={t('清除')}
              okButtonProps={{ danger: true }}
              onConfirm={() => runPurge({ all: true, ...currentFilters() })}
            >
              <Button danger type="primary" ghost loading={purging}>
                {t('清除筛选结果')}
              </Button>
            </Popconfirm>
          ) : null,
        ]}
      />
      <Drawer
        width={900}
        open={detailLoading || Boolean(detail)}
        title={
          detail ? t('命令输出 · 序号 {0}', { 0: detail.seq }) : t('命令输出')
        }
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
                {
                  key: 'sid',
                  label: t('会话号'),
                  children: <CopyText text={detail.sid} />,
                },
                {
                  key: 'username',
                  label: t('用户'),
                  children: detail.username,
                },
                {
                  key: 'hostName',
                  label: t('主机'),
                  children: detail.hostName,
                },
                {
                  key: 'action',
                  label: t('动作'),
                  children: <ActionTag action={detail.action} />,
                },
                {
                  key: 'riskLevel',
                  label: t('风险等级'),
                  children: <RiskTag level={detail.riskLevel} />,
                },
                {
                  key: 'matchedRulePattern',
                  label: t('命中规则'),
                  children: (
                    <MonoCell text={detail.matchedRulePattern} max={80} />
                  ),
                },
                {
                  key: 'matchedRuleId',
                  label: t('规则 ID'),
                  children: detail.matchedRuleId ?? '-',
                },
                {
                  key: 'reason',
                  label: t('判定原因'),
                  children: detail.reason || '-',
                },
                {
                  key: 'startedAt',
                  label: t('执行时间'),
                  children: <TimeCell value={detail.startedAt} />,
                },
                {
                  key: 'endedAt',
                  label: t('结束时间'),
                  children: <TimeCell value={detail.endedAt} />,
                },
                {
                  key: 'durationMs',
                  label: t('耗时'),
                  children: formatLatency(detail.durationMs),
                },
                {
                  key: 'exitStatus',
                  label: t('退出状态'),
                  children:
                    detail.exitStatus === null ? '-' : detail.exitStatus,
                },
                {
                  key: 'truncated',
                  label: t('输出被截断'),
                  children: detail.truncated ? t('是') : t('否'),
                },
              ]}
            />
            <div>
              <Text strong>{t('完整命令')}</Text>
              <div style={{ marginTop: 8 }}>
                <CopyText text={detail.command} label={t('复制命令')} />
              </div>
              <pre style={{ ...COMMAND_PRE_STYLE, marginTop: 8 }}>
                {detail.command || '-'}
              </pre>
            </div>
            <div>
              <Text strong>{t('命令输出')}</Text>
              <div style={{ marginTop: 8 }}>
                <OutputBlock text={detail.output} maxHeight={480} />
              </div>
            </div>
          </Space>
        ) : null}
      </Drawer>
    </PageContainer>
  );
};

export default CommandsPage;
