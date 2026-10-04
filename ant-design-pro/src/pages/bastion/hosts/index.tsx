/**
 * 资产管理 → 主机列表。
 *
 * 数据来自 hostApi（`GET /api/hosts`，支持 keyword / groupId / status 过滤）。
 * 行内「账号管理」抽屉用 `hostApi.accounts(hostId)` 拉账号，`accountApi` 增删改，
 * 并可对账号发起真实 SSH 连通性测试（`POST /api/hosts/<id>/accounts/<aid>/test`）。
 *
 * 后端契约要点（bastion-backend/app/api/hosts.py）：
 * - 协议支持 ssh（网页终端 / SSH 网关）与 rdp（Windows 远程桌面，走浏览器里的 WebRDP），
 *   认证方式只支持 password / key；
 * - 主机 / 账号 / 分组的写接口都是 admin_required（host:manage）；
 * - 删除主机或账号时若还有在线会话，后端返回 409，这里直接展示它的 message；
 * - 账号口令与私钥以 Fernet 加密落库，仅具备 host:manage 权限时回显。
 */
import {
  CloudServerOutlined,
  LaptopOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import {
  type ActionType,
  DrawerForm,
  ModalForm,
  PageContainer,
  type ProColumns,
  ProFormDependency,
  ProFormDigit,
  ProFormSelect,
  ProFormText,
  ProFormTextArea,
  ProTable,
} from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  App,
  Button,
  Drawer,
  Popconfirm,
  Space,
  Table,
  type TableColumnsType,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type React from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  BoolTag,
  CopyText,
  MonoCell,
  OsTag,
  ProtocolTag,
  TimeCell,
} from '@/components/Bastion';
import {
  AUTH_TYPE_OPTIONS,
  HOST_STATUS_META,
} from '@/services/bastion/constants';
import {
  type AccountPayload,
  accountApi,
  type HostPayload,
  hostApi,
  hostGroupApi,
} from '@/services/bastion/endpoints';
import type { HostAccountItem, HostItem } from '@/services/bastion/types';

const { Text } = Typography;

/** 主机列表查询参数（对应后端 list_hosts 的 keyword / groupId / status） */
type HostQuery = {
  keyword?: string;
  groupId?: number;
  status?: string;
};

type HostFormValues = {
  name: string;
  address: string;
  port: number;
  protocol: string;
  rdpSecurity?: string;
  osType: string;
  groupId?: number;
  status: string;
  tags?: string[];
  description?: string;
};

type AccountFormValues = {
  name: string;
  username: string;
  authType: 'password' | 'key';
  password?: string;
  privateKey?: string;
  passphrase?: string;
  sudoCommand?: string;
  description?: string;
};

/**
 * 后端接受 ssh（网页终端 / SSH 网关）与 rdp（Windows 远程桌面，走浏览器里的 WebRDP）
 */
const PROTOCOL_OPTIONS = [
  { label: 'SSH（Linux / Unix 命令行）', value: 'ssh' },
  { label: 'RDP（Windows 远程桌面）', value: 'rdp' },
];

const OS_TYPE_OPTIONS = [
  { label: 'Linux', value: 'linux' },
  { label: 'Windows', value: 'windows' },
  { label: 'Unix', value: 'unix' },
];

/**
 * RDP 安全层：浏览器里的 WebRDP 客户端默认按「自动」协商（会带 NLA/CredSSP）。
 * 少数老系统（Windows Server 2003 / XP 一类）在 NLA 阶段一个字节不回就断开，
 * 这时改成「强制 SSL」让它退回标准 RDP 安全层（只对协议为 rdp 的主机生效）。
 */
const RDP_SECURITY_OPTIONS = [
  { label: '自动（默认，按客户端协商，含 NLA）', value: 'auto' },
  { label: '强制 SSL（不使用 NLA，兼容老系统）', value: 'ssl' },
];

const STATUS_OPTIONS = Object.entries(HOST_STATUS_META).map(
  ([value, meta]) => ({
    label: meta.text,
    value,
  }),
);

/** 搜索表单的状态下拉（展示文案与标签共用 HOST_STATUS_META） */
const STATUS_VALUE_ENUM: Record<string, { text: string }> = Object.fromEntries(
  Object.entries(HOST_STATUS_META).map(([value, meta]) => [
    value,
    { text: meta.text },
  ]),
);

const authTypeText = (value: HostAccountItem['authType']): string =>
  AUTH_TYPE_OPTIONS.find((item) => item.value === value)?.label ?? value;

/** 后端错误信封 { success:false, message, code } → 可读提示 */
const fromApiError = (error: unknown, fallback: string): string =>
  (error as { response?: { data?: { message?: string } } })?.response?.data
    ?.message || fallback;

const Hosts: React.FC = () => {
  const { message, modal } = App.useApp();
  const access = useAccess();
  const canManageHost = Boolean(access.canHostManage);
  const canManageAccount = Boolean(access.canAccountManage);
  const canViewAccount = Boolean(access.canAccountView);

  const actionRef = useRef<ActionType | undefined>(undefined);

  const [groupOptions, setGroupOptions] = useState<
    { label: string; value: number }[]
  >([]);

  // 主机表单
  const [hostFormOpen, setHostFormOpen] = useState(false);
  const [editing, setEditing] = useState<HostItem>();
  const [hostFormKey, setHostFormKey] = useState(0);

  // 账号抽屉
  const [accountHost, setAccountHost] = useState<HostItem>();
  const [accounts, setAccounts] = useState<HostAccountItem[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [editingAccount, setEditingAccount] = useState<HostAccountItem>();
  const [accountFormOpen, setAccountFormOpen] = useState(false);
  const [accountFormKey, setAccountFormKey] = useState(0);
  const [testingId, setTestingId] = useState<number>();

  const loadGroupOptions = useCallback(async () => {
    try {
      // hostGroupApi 没有 options()，按契约用 list 取全量再映射成下拉项
      const res = await hostGroupApi.list({ pageSize: 200 });
      setGroupOptions(
        res.data.map((group) => ({ label: group.name, value: group.id })),
      );
    } catch (error) {
      message.error(fromApiError(error, '主机分组选项加载失败'));
    }
  }, [message]);

  const loadAccounts = useCallback(
    async (hostId: number) => {
      setAccountsLoading(true);
      try {
        setAccounts(await hostApi.accounts(hostId));
      } catch (error) {
        message.error(fromApiError(error, '账号列表加载失败'));
      } finally {
        setAccountsLoading(false);
      }
    },
    [message],
  );

  useEffect(() => {
    loadGroupOptions();
  }, [loadGroupOptions]);

  const refreshAll = () => {
    actionRef.current?.reload();
    loadGroupOptions();
  };

  const openHostForm = (row?: HostItem) => {
    setEditing(row);
    setHostFormKey((key) => key + 1);
    setHostFormOpen(true);
  };

  const submitHost = async (values: HostFormValues): Promise<boolean> => {
    const payload: HostPayload = {
      name: values.name,
      address: values.address,
      port: values.port,
      protocol: values.protocol,
      rdpSecurity: values.rdpSecurity ?? 'auto',
      osType: values.osType,
      groupId: values.groupId ?? null,
      description: values.description ?? '',
      status: values.status,
      tags: values.tags ?? [],
    };
    try {
      if (editing) {
        await hostApi.update(editing.id, payload);
        message.success('主机已更新');
      } else {
        await hostApi.create(payload);
        message.success('主机已创建');
      }
      actionRef.current?.reload();
      return true;
    } catch (error) {
      message.error(fromApiError(error, '保存主机失败'));
      return false;
    }
  };

  const removeHost = async (row: HostItem) => {
    try {
      await hostApi.remove(row.id);
      message.success('主机已删除');
      actionRef.current?.reload();
    } catch (error) {
      // 后端在有在线会话时返回 409 SESSION_ACTIVE，这里把它的 message 原样展示
      message.error(fromApiError(error, '删除主机失败'));
    }
  };

  const openAccounts = (row: HostItem) => {
    setAccountHost(row);
    setAccounts([]);
    loadAccounts(row.id);
  };

  const openAccountForm = (row?: HostAccountItem) => {
    setEditingAccount(row);
    setAccountFormKey((key) => key + 1);
    setAccountFormOpen(true);
  };

  const submitAccount = async (values: AccountFormValues): Promise<boolean> => {
    if (!accountHost) return false;
    const payload: AccountPayload = {
      name: values.name,
      username: values.username,
      authType: values.authType,
      sudoCommand: values.sudoCommand ?? '',
      description: values.description ?? '',
    };
    // 编辑时口令/私钥留空表示不修改：不下发该字段，后端保留原值
    if (values.authType === 'password') {
      if (values.password) payload.password = values.password;
    } else {
      if (values.privateKey) payload.privateKey = values.privateKey;
      if (values.passphrase) payload.passphrase = values.passphrase;
    }
    try {
      if (editingAccount) {
        await accountApi.update(accountHost.id, editingAccount.id, payload);
        message.success('账号已更新');
      } else {
        await accountApi.create(accountHost.id, payload);
        message.success('账号已创建');
      }
      await loadAccounts(accountHost.id);
      actionRef.current?.reload();
      return true;
    } catch (error) {
      message.error(fromApiError(error, '保存账号失败'));
      return false;
    }
  };

  const testAccount = async (row: HostAccountItem) => {
    if (!accountHost) return;
    setTestingId(row.id);
    try {
      const result = await accountApi.test(accountHost.id, row.id);
      modal.info({
        title: `连接测试通过：${row.name}`,
        width: 720,
        content: (
          <div>
            <p>
              <CloudServerOutlined
                style={{ color: '#2E7BFF', marginRight: 6 }}
              />
              服务器版本：{result.serverVersion || '-'}
            </p>
            <p>
              <LaptopOutlined style={{ color: '#12B8C8', marginRight: 6 }} />
              客户端版本：{result.clientVersion || '-'}
            </p>
            <p>主机密钥指纹：{result.fingerprint || '-'}</p>
            <p>退出码：{result.exitStatus ?? '-'}</p>
            <pre style={{ margin: 0, maxHeight: 320, overflow: 'auto' }}>
              {result.output || '(无输出)'}
            </pre>
          </div>
        ),
      });
    } catch (error) {
      // 后端 SSH 失败返回 400 SSH_CONNECT_FAILED，message 里带具体原因
      modal.error({
        title: '连接测试失败',
        content: fromApiError(error, '连接测试失败'),
      });
    } finally {
      setTestingId(undefined);
    }
  };

  const removeAccount = async (row: HostAccountItem) => {
    if (!accountHost) return;
    try {
      await accountApi.remove(accountHost.id, row.id);
      message.success('账号已删除');
      await loadAccounts(accountHost.id);
      actionRef.current?.reload();
    } catch (error) {
      message.error(fromApiError(error, '删除账号失败'));
    }
  };

  const hostInitialValues: Partial<HostFormValues> = editing
    ? {
        name: editing.name,
        address: editing.address,
        port: editing.port,
        protocol: editing.protocol,
        rdpSecurity: editing.rdpSecurity ?? 'auto',
        osType: editing.osType,
        groupId: editing.groupId ?? undefined,
        status: editing.status,
        tags: editing.tags ?? [],
        description: editing.description,
      }
    : {
        port: 22,
        protocol: 'ssh',
        rdpSecurity: 'auto',
        osType: 'linux',
        status: 'active',
        tags: [],
      };

  // 编辑时不回显口令/私钥（后端仅在 host:manage 下回显，且留空即不修改）
  const accountInitialValues: Partial<AccountFormValues> = editingAccount
    ? {
        name: editingAccount.name,
        username: editingAccount.username,
        authType: editingAccount.authType,
        sudoCommand: editingAccount.sudoCommand,
        description: editingAccount.description,
      }
    : { authType: 'password' };

  const hostColumns: ProColumns<HostItem>[] = [
    {
      title: '关键字',
      dataIndex: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: '主机名 / 地址 / 描述' },
    },
    {
      title: '主机名',
      dataIndex: 'name',
      render: (_, row) => <Text strong>{row.name}</Text>,
    },
    {
      title: '地址',
      dataIndex: 'address',
      search: false,
      render: (_, row) => <CopyText text={`${row.address}:${row.port}`} />,
    },
    {
      title: '协议/系统',
      dataIndex: 'protocol',
      search: false,
      render: (_, row) => (
        <Space size={4}>
          <ProtocolTag protocol={row.protocol} />
          <OsTag osType={row.osType} />
          {row.protocol === 'rdp' && row.rdpSecurity === 'ssl' ? (
            <Tooltip title="这台主机的 RDP 安全层被设成「强制 SSL」：连接时不走 NLA/CredSSP，改用标准 RDP 安全层（用于 Windows Server 2003 / XP 一类老系统）">
              <Tag color="orange">强制 SSL</Tag>
            </Tooltip>
          ) : null}
        </Space>
      ),
    },
    {
      title: '分组',
      dataIndex: 'groupId',
      valueType: 'select',
      fieldProps: {
        options: groupOptions,
        allowClear: true,
        placeholder: '全部分组',
      },
      render: (_, row) => row.groupName || '-',
    },
    {
      title: '状态',
      dataIndex: 'status',
      valueType: 'select',
      valueEnum: STATUS_VALUE_ENUM,
      render: (_, row) => {
        const meta = HOST_STATUS_META[row.status];
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: '账号数',
      dataIndex: 'accountCount',
      search: false,
      width: 90,
    },
    { title: '授权数', dataIndex: 'grantCount', search: false, width: 90 },
    {
      title: '描述',
      dataIndex: 'description',
      search: false,
      ellipsis: true,
      render: (_, row) => row.description || '-',
    },
    {
      title: '创建时间',
      dataIndex: 'createdAt',
      search: false,
      width: 170,
      render: (_, row) => <TimeCell value={row.createdAt} />,
    },
    {
      title: '操作',
      key: 'option',
      valueType: 'option',
      fixed: 'right',
      width: 300,
      render: (_, row) => {
        const nodes: React.ReactNode[] = [];
        if (canViewAccount) {
          nodes.push(
            <Button
              key="accounts"
              type="link"
              size="small"
              onClick={() => openAccounts(row)}
            >
              账号管理
            </Button>,
          );
        }
        if (canManageHost) {
          nodes.push(
            <Button
              key="edit"
              type="link"
              size="small"
              onClick={() => openHostForm(row)}
            >
              编辑
            </Button>,
            <Popconfirm
              key="delete"
              title={`确认删除主机「${row.name}」？`}
              description="该主机下的账号与授权记录会被一并删除。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => removeHost(row)}
            >
              <Button type="link" size="small" danger>
                删除
              </Button>
            </Popconfirm>,
          );
        }
        return nodes.length ? <Space size={0}>{nodes}</Space> : '-';
      },
    },
  ];

  const accountColumns: TableColumnsType<HostAccountItem> = [
    {
      title: '名称',
      dataIndex: 'name',
      render: (_, row) => <Text strong>{row.name}</Text>,
    },
    {
      title: '登录用户名',
      dataIndex: 'username',
      render: (_, row) => <MonoCell text={row.username} max={32} />,
    },
    {
      title: '认证方式',
      dataIndex: 'authType',
      width: 110,
      render: (_, row) => (
        <Tag color={row.authType === 'key' ? 'purple' : 'blue'}>
          {authTypeText(row.authType)}
        </Tag>
      ),
    },
    {
      title: '是否有口令',
      dataIndex: 'hasSecret',
      width: 110,
      render: (_, row) => (
        <BoolTag value={row.hasSecret} yes="已配置" no="未配置" />
      ),
    },
    {
      title: 'sudo 命令',
      dataIndex: 'sudoCommand',
      render: (_, row) => <MonoCell text={row.sudoCommand} max={40} />,
    },
    { title: '授权数', dataIndex: 'grantCount', width: 80 },
    {
      title: '操作',
      key: 'option',
      width: 200,
      render: (_, row) =>
        canManageAccount ? (
          <Space size={0}>
            <Button
              type="link"
              size="small"
              onClick={() => openAccountForm(row)}
            >
              编辑
            </Button>
            <Button
              type="link"
              size="small"
              loading={testingId === row.id}
              onClick={() => testAccount(row)}
            >
              测试连接
            </Button>
            <Popconfirm
              title={`确认删除账号「${row.name}」？`}
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => removeAccount(row)}
            >
              <Button type="link" size="small" danger>
                删除
              </Button>
            </Popconfirm>
          </Space>
        ) : (
          '-'
        ),
    },
  ];

  return (
    <PageContainer
      header={{
        title: '主机列表',
        subTitle: '被纳管的服务器资产：地址、协议、分组、账号与授权',
      }}
    >
      <ProTable<HostItem, HostQuery>
        rowKey="id"
        actionRef={actionRef}
        headerTitle="主机"
        search={{ labelWidth: 'auto' }}
        pagination={{ defaultPageSize: 10 }}
        scroll={{ x: 'max-content' }}
        request={async (params) => {
          const res = await hostApi.list(params);
          return { data: res.data, total: res.total, success: res.success };
        }}
        toolBarRender={() => [
          canManageHost && (
            <Button
              key="create"
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => openHostForm()}
            >
              新增主机
            </Button>
          ),
          <Button key="reload" icon={<ReloadOutlined />} onClick={refreshAll}>
            刷新
          </Button>,
        ]}
        columns={hostColumns}
      />

      <DrawerForm<HostFormValues>
        key={hostFormKey}
        title={editing ? `编辑主机：${editing.name}` : '新增主机'}
        width={560}
        open={hostFormOpen}
        onOpenChange={setHostFormOpen}
        initialValues={hostInitialValues}
        drawerProps={{ destroyOnHidden: true }}
        submitter={{ searchConfig: { submitText: '保存' } }}
        onFinish={submitHost}
      >
        <ProFormText
          name="name"
          label="主机别名"
          rules={[{ required: true, message: '请输入主机别名' }]}
        />
        <ProFormText
          name="address"
          label="主机地址"
          fieldProps={{ placeholder: '192.168.1.10 或 host.example.com' }}
          rules={[{ required: true, message: '请输入主机地址' }]}
        />
        <ProFormDigit
          name="port"
          label="端口"
          min={1}
          max={65535}
          rules={[{ required: true, message: '请输入端口' }]}
        />
        <ProFormSelect
          name="protocol"
          label="协议"
          options={PROTOCOL_OPTIONS}
          rules={[{ required: true, message: '请选择协议' }]}
          extra="ssh 走网页终端 / SSH 网关；rdp 走浏览器里的 Windows 远程桌面"
        />
        <ProFormSelect
          name="rdpSecurity"
          label="RDP 安全层"
          options={RDP_SECURITY_OPTIONS}
          initialValue="auto"
          extra="只对协议 rdp 的主机生效：默认「自动」按客户端协议协商（含 NLA）；老系统（Windows Server 2003 / XP 一类）在 NLA 阶段被直接断开时，改成「强制 SSL」退回标准 RDP 安全层"
        />
        <ProFormSelect
          name="osType"
          label="操作系统"
          options={OS_TYPE_OPTIONS}
          rules={[{ required: true, message: '请选择操作系统' }]}
        />
        <ProFormSelect
          name="groupId"
          label="所属分组"
          options={groupOptions}
          fieldProps={{ allowClear: true, placeholder: '不选则不属于任何分组' }}
        />
        <ProFormSelect
          name="status"
          label="状态"
          options={STATUS_OPTIONS}
          rules={[{ required: true, message: '请选择状态' }]}
        />
        <ProFormSelect
          name="tags"
          label="标签"
          fieldProps={{ mode: 'tags', tokenSeparators: [','] }}
          placeholder="输入后回车添加标签"
        />
        <ProFormTextArea
          name="description"
          label="描述"
          fieldProps={{ rows: 3 }}
        />
      </DrawerForm>

      <Drawer
        title={accountHost ? `账号管理：${accountHost.name}` : '账号管理'}
        size={900}
        open={Boolean(accountHost)}
        onClose={() => setAccountHost(undefined)}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Text type="secondary">
            {accountHost
              ? `${accountHost.address}:${accountHost.port} · ${accountHost.osType} · ${accountHost.groupName || '未分组'}`
              : ''}
            。口令与私钥会用 Fernet 加密落库，只在具备 host:manage 权限时回显。
          </Text>
          {canManageAccount ? (
            <div>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => openAccountForm()}
              >
                新增账号
              </Button>
            </div>
          ) : null}
          <Table<HostAccountItem>
            rowKey="id"
            size="small"
            loading={accountsLoading}
            columns={accountColumns}
            dataSource={accounts}
            pagination={false}
            scroll={{ x: 'max-content' }}
          />
        </div>
      </Drawer>

      <ModalForm<AccountFormValues>
        key={accountFormKey}
        title={editingAccount ? `编辑账号：${editingAccount.name}` : '新增账号'}
        width={560}
        open={accountFormOpen}
        onOpenChange={setAccountFormOpen}
        initialValues={accountInitialValues}
        modalProps={{ destroyOnHidden: true, maskClosable: false }}
        submitter={{ searchConfig: { submitText: '保存' } }}
        onFinish={submitAccount}
      >
        <ProFormText
          name="name"
          label="账号别名"
          rules={[{ required: true, message: '请输入账号别名' }]}
        />
        <ProFormText
          name="username"
          label="登录用户名"
          rules={[{ required: true, message: '请输入登录用户名' }]}
        />
        <ProFormSelect
          name="authType"
          label="认证方式"
          options={AUTH_TYPE_OPTIONS}
          rules={[{ required: true, message: '请选择认证方式' }]}
          extra="口令与私钥会用 Fernet 加密落库，只在具备 host:manage 权限时回显"
        />
        <ProFormDependency name={['authType']}>
          {({ authType }: { authType?: 'password' | 'key' }) =>
            authType === 'key' ? (
              <>
                <ProFormTextArea
                  name="privateKey"
                  label="私钥内容"
                  fieldProps={{ rows: 6 }}
                  rules={
                    editingAccount
                      ? []
                      : [{ required: true, message: '请粘贴私钥内容' }]
                  }
                  extra="编辑时留空表示不修改已保存的私钥"
                />
                <ProFormText
                  name="passphrase"
                  label="私钥口令"
                  extra="私钥没有口令时留空即可"
                />
              </>
            ) : (
              <ProFormText.Password
                name="password"
                label="登录密码"
                rules={
                  editingAccount
                    ? []
                    : [{ required: true, message: '请输入登录密码' }]
                }
                extra="编辑时留空表示不修改已保存的口令"
              />
            )
          }
        </ProFormDependency>
        <ProFormText
          name="sudoCommand"
          label="sudo 命令"
          fieldProps={{ placeholder: 'sudo su -' }}
          extra="登录后自动执行的提权命令，不需要时留空"
        />
        <ProFormTextArea
          name="description"
          label="描述"
          fieldProps={{ rows: 2 }}
        />
      </ModalForm>
    </PageContainer>
  );
};

export default Hosts;
