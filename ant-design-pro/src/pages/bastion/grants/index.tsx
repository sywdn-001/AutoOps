/**
 * 访问授权页（需求：谁能碰哪台机器）。
 *
 * 页签 1「授权列表」：授权记录增删改查 + 批量授权。
 * 页签 2「授权矩阵」：用户 × 主机 的授权全景图，一眼看出谁没被授权到哪台机器。
 */
import { PlusOutlined } from '@ant-design/icons';
import {
  type ActionType,
  PageContainer,
  type ProColumns,
  ProTable,
} from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  App,
  Button,
  Checkbox,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tabs,
  Tag,
  TimePicker,
  Tooltip,
  Typography,
} from 'antd';
import type { Dayjs } from 'dayjs';
import dayjs from 'dayjs';
import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BoolTag, CopyText, TimeCell, WindowText } from '@/components/Bastion';
import { WEEKDAY_OPTIONS } from '@/services/bastion/constants';
import {
  filePolicyApi,
  type GrantPayload,
  grantApi,
  hostApi,
  policyApi,
  userApi,
} from '@/services/bastion/endpoints';
import type {
  GrantItem,
  GrantMatrixRow,
  HostAccountItem,
  OptionItem,
} from '@/services/bastion/types';

const { Text } = Typography;

const DATE_FORMAT = 'YYYY-MM-DD';
const TIME_FORMAT = 'HH:mm';

/** 表单字段结构（时段用 Dayjs，星期用字符串数组，提交时再转后端格式） */
type GrantFormValues = {
  userId: number;
  hostId: number;
  hostIds?: number[];
  hostAccountId?: number;
  policyId?: number;
  filePolicyId?: number;
  canLogin: boolean;
  canSftp: boolean;
  canUpload: boolean;
  canDownload: boolean;
  canFileWrite: boolean;
  canPortForward: boolean;
  canWebterm: boolean;
  timeRange?: [Dayjs, Dayjs];
  weekdays?: number[];
  expireAt?: Dayjs;
  maxSessions: number;
  enabled: boolean;
  remark?: string;
};

const INITIAL_VALUES: Partial<GrantFormValues> = {
  canLogin: true,
  canSftp: false,
  canUpload: false,
  canDownload: false,
  canFileWrite: false,
  canPortForward: false,
  canWebterm: false,
  weekdays: [],
  maxSessions: 0,
  enabled: true,
};

/** 授权记录 → 表单值（编辑回填） */
const toFormValues = (row: GrantItem): GrantFormValues => {
  const toTime = (value?: string | null) =>
    value ? dayjs(value, TIME_FORMAT) : undefined;
  const start = toTime(row.timeStart);
  const end = toTime(row.timeEnd);
  return {
    userId: row.userId,
    hostId: row.hostId,
    hostAccountId: row.hostAccountId ?? undefined,
    policyId: row.policyId ?? undefined,
    filePolicyId: row.filePolicyId ?? undefined,
    canLogin: row.canLogin,
    canSftp: row.canSftp,
    canUpload: row.canUpload,
    canDownload: row.canDownload,
    canFileWrite: row.canFileWrite,
    canPortForward: row.canPortForward,
    canWebterm: row.canWebterm,
    timeRange: start && end ? [start, end] : undefined,
    weekdays: row.weekdays ?? [],
    expireAt: row.expireAt ? dayjs(row.expireAt) : undefined,
    maxSessions: row.maxSessions ?? 0,
    enabled: row.enabled,
    remark: row.remark,
  };
};

/** 表单值 → 后端 payload（单条与批量共用；userId/hostId 由调用方补齐） */
const toPayload = (values: GrantFormValues): GrantPayload => {
  const [timeStart, timeEnd] = values.timeRange ?? [];
  return {
    userId: values.userId,
    hostAccountId: values.hostAccountId ?? null,
    policyId: values.policyId ?? null,
    filePolicyId: values.filePolicyId ?? null,
    canLogin: values.canLogin,
    canSftp: values.canSftp,
    canUpload: values.canUpload,
    canDownload: values.canDownload,
    canFileWrite: values.canFileWrite,
    canPortForward: values.canPortForward,
    canWebterm: values.canWebterm,
    timeStart: timeStart ? timeStart.format(TIME_FORMAT) : null,
    timeEnd: timeEnd ? timeEnd.format(TIME_FORMAT) : null,
    weekdays: values.weekdays ?? [],
    expireAt: values.expireAt ? values.expireAt.format(DATE_FORMAT) : null,
    maxSessions: values.maxSessions ?? 0,
    enabled: values.enabled,
    remark: values.remark,
  };
};

const fromApiError = (err: unknown, fallback: string): string =>
  (err as { response?: { data?: { message?: string } } })?.response?.data
    ?.message || fallback;

/** 七个权限开关的紧凑展示 */
const PermissionTags: React.FC<{ row: GrantItem }> = ({ row }) => (
  <Space size={2} wrap={false}>
    <BoolTag value={row.canLogin} yes="登录" no="禁登录" />
    <BoolTag value={row.canSftp} yes="SFTP" no="禁SFTP" />
    <BoolTag value={row.canUpload} yes="上传" no="禁上传" />
    <BoolTag value={row.canDownload} yes="下载" no="禁下载" />
    <BoolTag value={row.canFileWrite} yes="改文件" no="只读文件" />
    <BoolTag value={row.canPortForward} yes="转发" no="禁转发" />
    <BoolTag value={row.canWebterm} yes="网页" no="禁网页" />
  </Space>
);

/** 单条 / 批量授权表单的公共字段区（批量不支持账号与启停，后端批量接口固定整机 + 启用） */
const GrantFormFields: React.FC<{
  accountOptions: HostAccountItem[];
  policyOptions: OptionItem[];
  filePolicyOptions: { label: string; value: number }[];
  showAccount?: boolean;
  showEnabled?: boolean;
}> = ({
  accountOptions,
  policyOptions,
  filePolicyOptions,
  showAccount = true,
  showEnabled = true,
}) => (
  <>
    {showAccount && (
      <Form.Item
        name="hostAccountId"
        label="主机账号"
        extra="留空表示该主机的所有账号都可使用"
      >
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder="留空 = 整机所有账号"
          options={accountOptions.map((account) => ({
            label: `${account.name}（${account.username}）`,
            value: account.id,
          }))}
        />
      </Form.Item>
    )}
    <Form.Item
      name="policyId"
      label="命令策略"
      extra="留空表示使用系统默认策略"
    >
      <Select
        allowClear
        showSearch
        optionFilterProp="label"
        placeholder="默认策略"
        options={policyOptions}
      />
    </Form.Item>
    <Form.Item
      name="filePolicyId"
      label="文件策略"
      extra="留空表示使用系统默认文件策略；权限开关决定能不能用，文件策略决定能碰哪些路径"
    >
      <Select
        allowClear
        showSearch
        optionFilterProp="label"
        placeholder="默认文件策略"
        options={filePolicyOptions}
      />
    </Form.Item>
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
        columnGap: 12,
      }}
    >
      <Form.Item name="canLogin" label="允许登录" valuePropName="checked">
        <Switch />
      </Form.Item>
      <Form.Item name="canSftp" label="SFTP 传输" valuePropName="checked">
        <Switch />
      </Form.Item>
      <Form.Item name="canUpload" label="允许上传" valuePropName="checked">
        <Switch />
      </Form.Item>
      <Form.Item name="canDownload" label="允许下载" valuePropName="checked">
        <Switch />
      </Form.Item>
      <Form.Item
        name="canFileWrite"
        label="允许改文件"
        valuePropName="checked"
        extra="上传 / 编辑 / 新建 / 改名 / 移动 / 复制 / 删除 / 改权限"
      >
        <Switch />
      </Form.Item>
      <Form.Item name="canPortForward" label="端口转发" valuePropName="checked">
        <Switch />
      </Form.Item>
      <Form.Item
        name="canWebterm"
        label="网页终端"
        valuePropName="checked"
        extra="开启后可直接在浏览器里打开该主机终端"
      >
        <Switch />
      </Form.Item>
    </div>
    <Form.Item name="timeRange" label="每日时段" extra="留空表示全天可用">
      <TimePicker.RangePicker format={TIME_FORMAT} style={{ width: '100%' }} />
    </Form.Item>
    <Form.Item name="weekdays" label="生效星期" extra="不选表示每天生效">
      <Checkbox.Group options={WEEKDAY_OPTIONS} />
    </Form.Item>
    <Form.Item name="expireAt" label="到期日期" extra="留空表示长期有效">
      <DatePicker style={{ width: '100%' }} format={DATE_FORMAT} />
    </Form.Item>
    <Form.Item name="maxSessions" label="最大并发会话" extra="0 表示不限制">
      <InputNumber min={0} max={999} style={{ width: '100%' }} />
    </Form.Item>
    {showEnabled && (
      <Form.Item name="enabled" label="启用" valuePropName="checked">
        <Switch />
      </Form.Item>
    )}
    <Form.Item name="remark" label="备注">
      <Input.TextArea rows={2} maxLength={200} showCount />
    </Form.Item>
  </>
);

/** 授权列表页签 */
const GrantListTab: React.FC<{ canManage: boolean }> = ({ canManage }) => {
  const { message, modal } = App.useApp();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [form] = Form.useForm<GrantFormValues>();
  const [batchForm] = Form.useForm<GrantFormValues>();
  const [userOptions, setUserOptions] = useState<OptionItem[]>([]);
  const [hostOptions, setHostOptions] = useState<OptionItem[]>([]);
  const [policyOptions, setPolicyOptions] = useState<OptionItem[]>([]);
  const [filePolicyOptions, setFilePolicyOptions] = useState<
    { label: string; value: number }[]
  >([]);
  const [accountOptions, setAccountOptions] = useState<HostAccountItem[]>([]);
  const [formOpen, setFormOpen] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [editing, setEditing] = useState<GrantItem | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const [users, hosts, policies] = await Promise.all([
          userApi.options(),
          hostApi.options(),
          policyApi.options(),
        ]);
        setUserOptions(users);
        setHostOptions(hosts);
        setPolicyOptions(policies);
      } catch (err) {
        message.error(fromApiError(err, '加载下拉选项失败'));
      }
    })();
  }, [message]);

  useEffect(() => {
    void (async () => {
      try {
        const options = await filePolicyApi.options();
        setFilePolicyOptions(
          options.map((item) => ({ label: item.label, value: item.value })),
        );
      } catch (err) {
        console.debug('load file policy options failed', err);
      }
    })();
  }, []);

  const loadAccounts = useCallback(
    async (hostId?: number) => {
      if (!hostId) {
        setAccountOptions([]);
        return;
      }
      try {
        setAccountOptions(await hostApi.accounts(hostId));
      } catch (err) {
        setAccountOptions([]);
        message.error(fromApiError(err, '加载主机账号失败'));
      }
    },
    [message],
  );

  const openCreate = () => {
    setEditing(null);
    setAccountOptions([]);
    form.resetFields();
    form.setFieldsValue(INITIAL_VALUES);
    setFormOpen(true);
  };

  const openEdit = async (row: GrantItem) => {
    setEditing(row);
    form.resetFields();
    form.setFieldsValue(toFormValues(row));
    setFormOpen(true);
    await loadAccounts(row.hostId);
  };

  const submitForm = async (values: GrantFormValues) => {
    const [timeStart, timeEnd] = values.timeRange ?? [];
    if (timeStart && timeEnd && !timeEnd.isAfter(timeStart)) {
      message.error('结束时间必须晚于开始时间');
      return;
    }
    setSubmitting(true);
    try {
      const payload: GrantPayload = {
        ...toPayload(values),
        hostId: values.hostId,
      };
      if (editing) {
        await grantApi.update(editing.id, payload);
        message.success('授权已更新');
      } else {
        await grantApi.create(payload);
        message.success('授权已创建');
      }
      setFormOpen(false);
      actionRef.current?.reload();
    } catch (err) {
      message.error(
        fromApiError(err, editing ? '更新授权失败' : '创建授权失败'),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const submitBatch = async (values: GrantFormValues) => {
    const hostIds = values.hostIds ?? [];
    if (hostIds.length === 0) {
      message.error('请至少选择一台主机');
      return;
    }
    setSubmitting(true);
    try {
      const result = await grantApi.batch({ ...toPayload(values), hostIds });
      const skipped = result.skipped ?? [];
      modal.info({
        title: '批量授权完成',
        width: 560,
        content: (
          <Space orientation="vertical" size={8} style={{ width: '100%' }}>
            <Text>
              成功 <Text strong>{result.created.length}</Text> 台，跳过{' '}
              <Text strong>{skipped.length}</Text> 台。
            </Text>
            {skipped.length > 0 && (
              <div style={{ maxHeight: 260, overflow: 'auto' }}>
                {skipped.map((item) => (
                  <div key={item.hostId}>
                    <Text type="secondary">
                      主机 #{item.hostId}：{item.reason}
                    </Text>
                  </div>
                ))}
              </div>
            )}
          </Space>
        ),
      });
      setBatchOpen(false);
      actionRef.current?.reload();
    } catch (err) {
      message.error(fromApiError(err, '批量授权失败'));
    } finally {
      setSubmitting(false);
    }
  };

  const removeGrant = async (row: GrantItem) => {
    try {
      await grantApi.remove(row.id);
      message.success('授权已删除');
      actionRef.current?.reload();
    } catch (err) {
      message.error(fromApiError(err, '删除授权失败'));
    }
  };

  const columns: ProColumns<GrantItem>[] = [
    {
      title: '用户',
      dataIndex: 'username',
      width: 160,
      render: (_, row) => (
        <Space orientation="vertical" size={0}>
          <Text strong>{row.displayName || row.username}</Text>
          <Text type="secondary">{row.username}</Text>
        </Space>
      ),
    },
    { title: '主机', dataIndex: 'hostName', width: 150 },
    {
      title: '主机地址',
      dataIndex: 'hostAddress',
      width: 200,
      search: false,
      render: (_, row) => <CopyText text={row.hostAddress} />,
    },
    {
      title: '账号',
      dataIndex: 'accountName',
      width: 180,
      search: false,
      render: (_, row) => {
        const wholeMachine =
          !row.accountName || row.accountName === '*' || !row.hostAccountId;
        if (wholeMachine) {
          return (
            <Space size={4}>
              <Tag color="blue">全部账号</Tag>
              {row.accountUsername ? (
                <Text type="secondary">{row.accountUsername}</Text>
              ) : null}
            </Space>
          );
        }
        return (
          <Space size={4}>
            <Text>{row.accountName}</Text>
            <Text type="secondary">{row.accountUsername}</Text>
          </Space>
        );
      },
    },
    {
      title: '策略',
      dataIndex: 'policyName',
      width: 140,
      search: false,
      render: (_, row) =>
        row.policyName ? (
          <Tag>{row.policyName}</Tag>
        ) : (
          <Text type="secondary">默认策略</Text>
        ),
    },
    {
      title: '权限开关',
      dataIndex: 'canLogin',
      width: 320,
      search: false,
      render: (_, row) => <PermissionTags row={row} />,
    },
    {
      title: '时段',
      dataIndex: 'timeStart',
      width: 180,
      search: false,
      render: (_, row) => (
        <WindowText
          weekdays={row.weekdays}
          timeStart={row.timeStart}
          timeEnd={row.timeEnd}
        />
      ),
    },
    {
      title: '有效期',
      dataIndex: 'expireAt',
      width: 170,
      search: false,
      render: (_, row) => <TimeCell value={row.expireAt} />,
    },
    {
      title: '最大会话',
      dataIndex: 'maxSessions',
      width: 100,
      search: false,
      render: (_, row) =>
        row.maxSessions > 0 ? (
          <Text>{row.maxSessions} 个</Text>
        ) : (
          <Text type="secondary">不限</Text>
        ),
    },
    {
      title: '是否在窗口内',
      dataIndex: 'inWindow',
      width: 130,
      search: false,
      render: (_, row) => {
        if (!row.enabled) return <Tag color="default">已停用</Tag>;
        if (row.inWindow) return <Tag color="success">生效中</Tag>;
        return (
          <Tooltip title={row.windowReason || '不在授权时段内'}>
            <Tag color="warning">窗口外</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: '在线会话',
      dataIndex: 'activeSessions',
      width: 100,
      search: false,
      render: (_, row) =>
        row.activeSessions > 0 ? (
          <Tag color="processing">{row.activeSessions} 个</Tag>
        ) : (
          <Text type="secondary">0</Text>
        ),
    },
    {
      title: '操作',
      valueType: 'option',
      key: 'option',
      fixed: 'right',
      width: 120,
      render: (_, row) => {
        if (!canManage)
          return [
            <Text key="none" type="secondary">
              -
            </Text>,
          ];
        return [
          <a key="edit" onClick={() => void openEdit(row)}>
            编辑
          </a>,
          <Popconfirm
            key="delete"
            title="确认删除该授权？"
            description="删除后该用户将立即失去这台机器的访问权限。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => removeGrant(row)}
          >
            <a style={{ color: '#cf1322' }}>删除</a>
          </Popconfirm>,
        ];
      },
    },
    {
      title: '用户',
      dataIndex: 'userId',
      valueType: 'select',
      hideInTable: true,
      fieldProps: {
        showSearch: true,
        optionFilterProp: 'label',
        options: userOptions,
        placeholder: '按用户筛选',
      },
    },
    {
      title: '主机',
      dataIndex: 'hostId',
      valueType: 'select',
      hideInTable: true,
      fieldProps: {
        showSearch: true,
        optionFilterProp: 'label',
        options: hostOptions,
        placeholder: '按主机筛选',
      },
    },
    {
      title: '启用状态',
      dataIndex: 'enabled',
      valueType: 'select',
      hideInTable: true,
      valueEnum: {
        true: { text: '已启用' },
        false: { text: '已停用' },
      },
    },
  ];

  return (
    <>
      <ProTable<GrantItem>
        rowKey="id"
        actionRef={actionRef}
        headerTitle="授权记录"
        search={{ labelWidth: 'auto' }}
        pagination={{ defaultPageSize: 10 }}
        scroll={{ x: 'max-content' }}
        request={async (params) => {
          const res = await grantApi.list(params);
          return { data: res.data, total: res.total, success: res.success };
        }}
        toolBarRender={() =>
          canManage
            ? [
                <Button key="batch" onClick={() => setBatchOpen(true)}>
                  批量授权
                </Button>,
                <Button
                  key="create"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={openCreate}
                >
                  新增授权
                </Button>,
              ]
            : []
        }
        columns={columns}
      />

      <Modal
        title={editing ? `编辑授权 #${editing.id}` : '新增授权'}
        open={formOpen}
        width={720}
        onCancel={() => setFormOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={submitting}
        destroyOnHidden
        okText="保存"
        cancelText="取消"
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={INITIAL_VALUES}
          onFinish={(values) => void submitForm(values)}
        >
          <Form.Item
            name="userId"
            label="用户"
            rules={[{ required: true, message: '请选择用户' }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择被授权的用户"
              options={userOptions}
            />
          </Form.Item>
          <Form.Item
            name="hostId"
            label="主机"
            rules={[{ required: true, message: '请选择主机' }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择目标主机"
              options={hostOptions}
              onChange={(value) => void loadAccounts(value)}
            />
          </Form.Item>
          <GrantFormFields
            accountOptions={accountOptions}
            policyOptions={policyOptions}
            filePolicyOptions={filePolicyOptions}
          />
        </Form>
      </Modal>

      <Modal
        title="批量授权"
        open={batchOpen}
        width={720}
        onCancel={() => setBatchOpen(false)}
        onOk={() => batchForm.submit()}
        confirmLoading={submitting}
        destroyOnHidden
        okText="提交授权"
        cancelText="取消"
      >
        <Form
          form={batchForm}
          layout="vertical"
          initialValues={INITIAL_VALUES}
          onFinish={(values) => void submitBatch(values)}
        >
          <Form.Item
            name="userId"
            label="用户"
            rules={[{ required: true, message: '请选择用户' }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择被授权的用户"
              options={userOptions}
            />
          </Form.Item>
          <Form.Item
            name="hostIds"
            label="主机（可多选）"
            rules={[{ required: true, message: '请至少选择一台主机' }]}
            extra="同一套权限/时段逐台下发；已存在相同授权的机器会被跳过并给出原因"
          >
            <Select
              mode="multiple"
              showSearch
              optionFilterProp="label"
              placeholder="选择一台或多台主机"
              options={hostOptions}
            />
          </Form.Item>
          <GrantFormFields
            accountOptions={[]}
            policyOptions={policyOptions}
            filePolicyOptions={filePolicyOptions}
            showAccount={false}
            showEnabled={false}
          />
        </Form>
      </Modal>
    </>
  );
};

/** 授权矩阵页签 */
const GrantMatrixTab: React.FC = () => {
  const { message } = App.useApp();
  const [hosts, setHosts] = useState<{ id: number; name: string }[]>([]);
  const [matrixStat, setMatrixStat] = useState({ total: 0, granted: 0 });

  const hostColumns = useMemo<ProColumns<GrantMatrixRow>[]>(
    () =>
      hosts.map((host) => ({
        title: host.name,
        key: `host-${host.id}`,
        width: 110,
        align: 'center',
        render: (_, row) => {
          const cell = row.cells.find((item) => item.hostId === host.id);
          if (!cell?.granted) {
            return (
              <Tooltip title="未授权">
                <Text type="secondary">—</Text>
              </Tooltip>
            );
          }
          const tooltipText = [
            `策略：${cell.policyName || '默认策略'}`,
            `网页终端：${cell.canWebterm ? '允许' : '禁止'}`,
            `状态：${cell.enabled ? '已启用' : '已停用'}`,
          ].join('；');
          return (
            <Tooltip title={tooltipText}>
              <Tag color={cell.enabled ? 'success' : 'default'}>✓</Tag>
            </Tooltip>
          );
        },
      })),
    [hosts],
  );

  const columns = useMemo<ProColumns<GrantMatrixRow>[]>(
    () => [
      {
        title: '用户',
        dataIndex: 'keyword',
        hideInTable: true,
        fieldProps: { placeholder: '按用户名 / 姓名过滤' },
      },
      {
        title: '用户',
        dataIndex: 'username',
        fixed: 'left',
        width: 240,
        render: (_, row) => (
          <Space orientation="vertical" size={0}>
            <Space size={6}>
              <Text strong>{row.username}</Text>
              {row.roleCode ? <Tag color="blue">{row.roleCode}</Tag> : null}
            </Space>
            <Text type="secondary">
              {row.displayName || '-'} · 已授权 {row.grantCount} 台
            </Text>
          </Space>
        ),
      },
      ...hostColumns,
    ],
    [hostColumns],
  );

  return (
    <ProTable<GrantMatrixRow>
      rowKey="userId"
      headerTitle="授权矩阵"
      search={{ labelWidth: 'auto' }}
      pagination={{ defaultPageSize: 10 }}
      scroll={{ x: 'max-content' }}
      options={{ density: false }}
      toolBarRender={() => [
        <Text key="stat" type="secondary">
          共 {matrixStat.total} 个用户 / {hosts.length} 台主机，已授权{' '}
          {matrixStat.granted} 格
        </Text>,
      ]}
      request={async (params) => {
        try {
          const res = await grantApi.matrix(params);
          const rows = res.data ?? [];
          setHosts(res.hosts ?? []);
          setMatrixStat({
            total: res.total ?? 0,
            granted: rows.reduce(
              (sum, row) =>
                sum + row.cells.filter((cell) => cell.granted).length,
              0,
            ),
          });
          return { data: rows, total: res.total, success: res.success };
        } catch (err) {
          message.error(fromApiError(err, '加载授权矩阵失败'));
          return { data: [], total: 0, success: false };
        }
      }}
      columns={columns}
    />
  );
};

const GrantsPage: React.FC = () => {
  const access = useAccess();

  return (
    <PageContainer
      header={{
        title: '访问授权',
        subTitle: '谁能登录哪台机器、能用哪些账号、在什么时间窗内',
      }}
    >
      <Tabs
        items={[
          {
            key: 'list',
            label: '授权列表',
            children: (
              <GrantListTab canManage={Boolean(access.canGrantManage)} />
            ),
          },
          {
            key: 'matrix',
            label: '授权矩阵',
            children: <GrantMatrixTab />,
          },
        ]}
      />
    </PageContainer>
  );
};

export default GrantsPage;
