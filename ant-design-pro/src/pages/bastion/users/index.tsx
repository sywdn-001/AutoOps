/**
 * 用户管理页（身份与权限 - 用户）。
 *
 * 一眼看清每个账号的登录入口（网关 / 网页终端）、状态与锁定情况；
 * 新增、编辑、重置密码、解锁、删除五个写操作统一受 access.canUserManage 控制。
 * 权限边界说明：能用哪些主机、能跑哪些命令由「访问授权」页决定，本页只管账号本身。
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
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Switch,
  Tag,
} from 'antd';
import type React from 'react';
import { useEffect, useRef, useState } from 'react';
import { BoolTag, TimeCell } from '@/components/Bastion';
import { formatDateTime, USER_STATUS_META } from '@/services/bastion/constants';
import {
  roleApi,
  type UserPayload,
  userApi,
} from '@/services/bastion/endpoints';
import type { OptionItem, UserItem } from '@/services/bastion/types';

/** 用户表单字段（password 仅新增时出现） */
type UserFormValues = {
  username: string;
  displayName?: string;
  roleId?: number;
  password?: string;
  email?: string;
  phone?: string;
  remark?: string;
  status: 'active' | 'disabled';
  gatewayEnabled: boolean;
  webtermEnabled: boolean;
  mustChangePassword: boolean;
  isSuperuser: boolean;
};

/** 新增用户的默认值：能登录、能开网页终端、首次登录必须改密码、不授予超管 */
const INITIAL_VALUES: Partial<UserFormValues> = {
  status: 'active',
  gatewayEnabled: true,
  webtermEnabled: true,
  mustChangePassword: true,
  isSuperuser: false,
};

/** 状态下拉项：constants 未提供 USER_STATUS_OPTIONS，按 USER_STATUS_META 生成 */
const STATUS_OPTIONS = Object.entries(USER_STATUS_META).map(
  ([value, meta]) => ({
    value,
    label: meta.text,
  }),
);

/** 密码强度：至少 8 位，且包含字母、数字、符号中的至少两类 */
const PASSWORD_PATTERN =
  /^(?:(?=.*[A-Za-z])(?=.*\d)|(?=.*[A-Za-z])(?=.*[^A-Za-z0-9])|(?=.*\d)(?=.*[^A-Za-z0-9]))[\s\S]+$/;

const PASSWORD_RULES = [
  { required: true, message: '请输入初始密码' },
  { min: 8, message: '密码至少 8 位' },
  {
    pattern: PASSWORD_PATTERN,
    message: '密码需包含字母、数字、符号中的至少两类',
  },
];

const fromApiError = (err: unknown, fallback: string): string =>
  (err as { response?: { data?: { message?: string } } })?.response?.data
    ?.message || fallback;

/** 用户记录 → 表单值（编辑时不含 password，后端不会收到空密码） */
const toFormValues = (row: UserItem): Partial<UserFormValues> => ({
  username: row.username,
  displayName: row.displayName,
  roleId: row.roleId,
  email: row.email,
  phone: row.phone,
  remark: row.remark,
  status: row.status,
  gatewayEnabled: row.gatewayEnabled,
  webtermEnabled: row.webtermEnabled,
  mustChangePassword: row.mustChangePassword,
  isSuperuser: row.isSuperuser,
});

const UsersPage: React.FC = () => {
  const { message } = App.useApp();
  const access = useAccess();
  const canManage = Boolean(access.canUserManage);
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [form] = Form.useForm<UserFormValues>();
  const [resetForm] = Form.useForm<{ password: string }>();
  const [roleOptions, setRoleOptions] = useState<OptionItem[]>([]);
  const [formOpen, setFormOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [editing, setEditing] = useState<UserItem | null>(null);
  const [resetTarget, setResetTarget] = useState<UserItem | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setRoleOptions(await roleApi.options());
      } catch (err) {
        message.error(fromApiError(err, '加载角色选项失败'));
      }
    })();
  }, [message]);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue(INITIAL_VALUES);
    setFormOpen(true);
  };

  const openEdit = (row: UserItem) => {
    setEditing(row);
    form.resetFields();
    form.setFieldsValue(toFormValues(row));
    setFormOpen(true);
  };

  const openReset = (row: UserItem) => {
    setResetTarget(row);
    resetForm.resetFields();
    setResetOpen(true);
  };

  const submitForm = async (values: UserFormValues) => {
    setSubmitting(true);
    try {
      const payload: UserPayload = {
        username: values.username,
        displayName: values.displayName,
        roleId: values.roleId,
        email: values.email,
        phone: values.phone,
        remark: values.remark,
        status: values.status,
        gatewayEnabled: values.gatewayEnabled,
        webtermEnabled: values.webtermEnabled,
        mustChangePassword: values.mustChangePassword,
        isSuperuser: values.isSuperuser,
      };
      if (editing) {
        await userApi.update(editing.id, payload);
        message.success('用户已更新');
      } else {
        payload.password = values.password;
        await userApi.create(payload);
        message.success('用户已创建');
      }
      setFormOpen(false);
      actionRef.current?.reload();
    } catch (err) {
      message.error(
        fromApiError(err, editing ? '更新用户失败' : '创建用户失败'),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const submitReset = async (values: { password: string }) => {
    if (!resetTarget) {
      return;
    }
    setSubmitting(true);
    try {
      await userApi.resetPassword(resetTarget.id, values.password);
      message.success('已重置，用户下次登录必须改密码');
      setResetOpen(false);
    } catch (err) {
      message.error(fromApiError(err, '重置密码失败'));
    } finally {
      setSubmitting(false);
    }
  };

  const unlockUser = async (row: UserItem) => {
    try {
      await userApi.unlock(row.id);
      message.success(`已解除「${row.username}」的登录锁定`);
      actionRef.current?.reload();
    } catch (err) {
      message.error(fromApiError(err, '解锁失败'));
    }
  };

  const removeUser = async (row: UserItem) => {
    try {
      await userApi.remove(row.id);
      message.success('用户已删除');
      actionRef.current?.reload();
    } catch (err) {
      message.error(fromApiError(err, '删除用户失败'));
    }
  };

  const columns: ProColumns<UserItem>[] = [
    {
      title: '用户名',
      dataIndex: 'username',
      width: 140,
      search: false,
    },
    {
      title: '姓名',
      dataIndex: 'displayName',
      width: 120,
      search: false,
      render: (_, row) => row.displayName || '-',
    },
    {
      title: '角色',
      dataIndex: 'roleName',
      width: 130,
      search: false,
      render: (_, row) =>
        row.roleName ? <Tag color="blue">{row.roleName}</Tag> : '-',
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      search: false,
      render: (_, row) => {
        const meta = USER_STATUS_META[row.status] ?? {
          text: row.status,
          color: 'default',
        };
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: '网关登录',
      dataIndex: 'gatewayEnabled',
      width: 100,
      search: false,
      render: (_, row) => <BoolTag value={row.gatewayEnabled} />,
    },
    {
      title: '网页终端',
      dataIndex: 'webtermEnabled',
      width: 100,
      search: false,
      render: (_, row) => <BoolTag value={row.webtermEnabled} />,
    },
    {
      title: '超级管理员',
      dataIndex: 'isSuperuser',
      width: 110,
      search: false,
      render: (_, row) => <BoolTag value={row.isSuperuser} yes="是" no="否" />,
    },
    {
      title: '最后登录',
      dataIndex: 'lastLoginAt',
      width: 180,
      search: false,
      render: (_, row) => <TimeCell value={row.lastLoginAt} />,
    },
    {
      title: '最后登录 IP',
      dataIndex: 'lastLoginIp',
      width: 150,
      search: false,
      render: (_, row) => row.lastLoginIp || '-',
    },
    {
      title: '锁定状态',
      dataIndex: 'lockedUntil',
      width: 220,
      search: false,
      render: (_, row) =>
        row.lockedUntil ? (
          <Tag color="red">已锁定至 {formatDateTime(row.lockedUntil)}</Tag>
        ) : (
          <Tag color="default">正常</Tag>
        ),
    },
    {
      title: '操作',
      valueType: 'option',
      key: 'option',
      fixed: 'right',
      width: 210,
      render: (_, row) => {
        if (!canManage) {
          return [
            <span key="none" style={{ color: 'rgba(0, 0, 0, 0.25)' }}>
              -
            </span>,
          ];
        }
        return [
          <a key="edit" onClick={() => void openEdit(row)}>
            编辑
          </a>,
          <a key="reset" onClick={() => void openReset(row)}>
            重置密码
          </a>,
          row.lockedUntil ? (
            <a key="unlock" onClick={() => void unlockUser(row)}>
              解锁
            </a>
          ) : null,
          <Popconfirm
            key="delete"
            title="确认删除该用户？"
            description="删除后该用户将无法再登录，其授权记录也会失效。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => removeUser(row)}
          >
            <a style={{ color: '#cf1322' }}>删除</a>
          </Popconfirm>,
        ];
      },
    },
    {
      title: '关键词',
      dataIndex: 'keyword',
      key: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: '用户名 / 姓名 / 邮箱' },
    },
    {
      title: '角色',
      dataIndex: 'roleId',
      key: 'searchRoleId',
      valueType: 'select',
      hideInTable: true,
      fieldProps: {
        showSearch: true,
        optionFilterProp: 'label',
        options: roleOptions,
        placeholder: '全部角色',
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'searchStatus',
      valueType: 'select',
      hideInTable: true,
      fieldProps: {
        options: STATUS_OPTIONS,
        placeholder: '全部状态',
      },
    },
  ];

  return (
    <PageContainer
      header={{
        title: '用户管理',
        subTitle: '账号、角色、登录入口与锁定状态',
      }}
    >
      <ProTable<UserItem>
        rowKey="id"
        actionRef={actionRef}
        headerTitle="用户列表"
        search={{ labelWidth: 'auto' }}
        pagination={{ defaultPageSize: 10 }}
        scroll={{ x: 'max-content' }}
        request={async (params) => {
          const res = await userApi.list(params);
          return { data: res.data, total: res.total, success: res.success };
        }}
        toolBarRender={() =>
          canManage
            ? [
                <Button
                  key="create"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={openCreate}
                >
                  新增用户
                </Button>,
              ]
            : []
        }
        columns={columns}
      />

      <Modal
        title={editing ? `编辑用户「${editing.username}」` : '新增用户'}
        open={formOpen}
        width={640}
        onCancel={() => setFormOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={submitting}
        destroyOnHidden
        mask={{ closable: false }}
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
            name="username"
            label="用户名"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input placeholder="登录名，例如 zhangsan" disabled={!!editing} />
          </Form.Item>
          <Form.Item name="displayName" label="姓名">
            <Input placeholder="真实姓名，选填" />
          </Form.Item>
          <Form.Item
            name="roleId"
            label="角色"
            rules={[{ required: true, message: '请选择角色' }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择该用户的角色"
              options={roleOptions}
            />
          </Form.Item>
          {!editing && (
            <Form.Item
              name="password"
              label="初始密码"
              rules={PASSWORD_RULES}
              extra="至少 8 位，且包含字母、数字、符号中的至少两类"
            >
              <Input.Password
                placeholder="请输入初始密码"
                autoComplete="new-password"
              />
            </Form.Item>
          )}
          <Form.Item
            name="email"
            label="邮箱"
            rules={[{ type: 'email', message: '邮箱格式不正确' }]}
          >
            <Input placeholder="选填" />
          </Form.Item>
          <Form.Item name="phone" label="手机号">
            <Input placeholder="选填" />
          </Form.Item>
          <Form.Item
            name="status"
            label="状态"
            rules={[{ required: true, message: '请选择状态' }]}
          >
            <Select options={STATUS_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="gatewayEnabled"
            label="网关登录"
            valuePropName="checked"
          >
            <Switch checkedChildren="允许" unCheckedChildren="禁止" />
          </Form.Item>
          <Form.Item
            name="webtermEnabled"
            label="网页终端"
            valuePropName="checked"
          >
            <Switch checkedChildren="允许" unCheckedChildren="禁止" />
          </Form.Item>
          <Form.Item
            name="mustChangePassword"
            label="下次登录必须改密码"
            valuePropName="checked"
          >
            <Switch checkedChildren="是" unCheckedChildren="否" />
          </Form.Item>
          <Form.Item
            name="isSuperuser"
            label="超级管理员"
            valuePropName="checked"
            extra="超级管理员拥有全部权限，请谨慎授予"
          >
            <Switch checkedChildren="是" unCheckedChildren="否" />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input.TextArea rows={3} placeholder="选填" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={
          resetTarget ? `重置「${resetTarget.username}」的密码` : '重置密码'
        }
        open={resetOpen}
        width={440}
        onCancel={() => setResetOpen(false)}
        onOk={() => resetForm.submit()}
        confirmLoading={submitting}
        destroyOnHidden
        okText="重置"
        cancelText="取消"
      >
        <Form
          form={resetForm}
          layout="vertical"
          onFinish={(values) => void submitReset(values)}
        >
          <Form.Item
            name="password"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 8, message: '新密码至少 8 位' },
              {
                pattern: PASSWORD_PATTERN,
                message: '新密码需包含字母、数字、符号中的至少两类',
              },
            ]}
            extra="重置成功后，该用户下次登录必须修改密码"
          >
            <Input.Password
              placeholder="请输入新密码"
              autoComplete="new-password"
            />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
};

export default UsersPage;
