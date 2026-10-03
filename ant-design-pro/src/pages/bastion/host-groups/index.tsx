/**
 * 资产管理 → 主机分组。
 *
 * 数据来自 hostGroupApi（`GET /api/host-groups`，后端支持 keyword 过滤分组名，默认每页 50 条）。
 * 后端契约要点（bastion-backend/app/api/hosts.py）：
 * - 新增分组：名称必填，重名返回 409（「分组名称已存在」）；
 * - 删除分组：该分组下仍有主机时返回 409 INVALID_OPERATION
 *   （「该分组下还有 N 台主机，请先迁移」），这里把后端 message 原样展示；
 * - 分组的写接口都是 admin_required（group:manage）。
 */
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import {
  type ActionType,
  ModalForm,
  PageContainer,
  type ProColumns,
  ProFormText,
  ProFormTextArea,
  ProTable,
} from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import { App, Button, Popconfirm, Space } from 'antd';
import type React from 'react';
import { useRef, useState } from 'react';
import { TimeCell } from '@/components/Bastion';
import {
  type HostGroupPayload,
  hostGroupApi,
} from '@/services/bastion/endpoints';
import type { HostGroupItem } from '@/services/bastion/types';

type GroupFormValues = {
  name: string;
  description?: string;
};

/** 后端错误信封 { success:false, message, code } → 可读提示 */
const fromApiError = (error: unknown, fallback: string): string =>
  (error as { response?: { data?: { message?: string } } })?.response?.data
    ?.message || fallback;

const HostGroups: React.FC = () => {
  const { message } = App.useApp();
  const access = useAccess();
  const canManage = Boolean(access.canGroupManage);

  const actionRef = useRef<ActionType | undefined>(undefined);

  const [editing, setEditing] = useState<HostGroupItem>();
  const [formOpen, setFormOpen] = useState(false);
  const [formKey, setFormKey] = useState(0);

  const openForm = (row?: HostGroupItem) => {
    setEditing(row);
    setFormKey((key) => key + 1);
    setFormOpen(true);
  };

  const submitGroup = async (values: GroupFormValues): Promise<boolean> => {
    const payload: HostGroupPayload = {
      name: values.name,
      description: values.description ?? '',
    };
    try {
      if (editing) {
        await hostGroupApi.update(editing.id, payload);
        message.success('分组已更新');
      } else {
        await hostGroupApi.create(payload);
        message.success('分组已创建');
      }
      actionRef.current?.reload();
      return true;
    } catch (error) {
      message.error(fromApiError(error, '保存分组失败'));
      return false;
    }
  };

  const removeGroup = async (row: HostGroupItem) => {
    try {
      await hostGroupApi.remove(row.id);
      message.success('分组已删除');
      actionRef.current?.reload();
    } catch (error) {
      // 分组下仍有主机时后端返回 409，message 里带待迁移的主机数量
      message.error(fromApiError(error, '删除分组失败'));
    }
  };

  const columns: ProColumns<HostGroupItem>[] = [
    {
      title: '关键字',
      dataIndex: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: '分组名称' },
    },
    { title: '分组名', dataIndex: 'name' },
    {
      title: '描述',
      dataIndex: 'description',
      search: false,
      ellipsis: true,
      render: (_, row) => row.description || '-',
    },
    { title: '主机数', dataIndex: 'hostCount', search: false, width: 100 },
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
      width: 140,
      render: (_, row) =>
        canManage ? (
          <Space size={0}>
            <Button type="link" size="small" onClick={() => openForm(row)}>
              编辑
            </Button>
            <Popconfirm
              title={`确认删除分组「${row.name}」？`}
              description="分组下仍有主机时后端会拒绝删除。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => removeGroup(row)}
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
        title: '主机分组',
        subTitle: '按业务或环境组织主机，用于授权与批量运维',
      }}
    >
      <ProTable<HostGroupItem, { keyword?: string }>
        rowKey="id"
        actionRef={actionRef}
        headerTitle="分组"
        search={{ labelWidth: 'auto' }}
        pagination={{ defaultPageSize: 10 }}
        request={async (params) => {
          const res = await hostGroupApi.list(params);
          return { data: res.data, total: res.total, success: res.success };
        }}
        toolBarRender={() => [
          canManage && (
            <Button
              key="create"
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => openForm()}
            >
              新增分组
            </Button>
          ),
          <Button
            key="reload"
            icon={<ReloadOutlined />}
            onClick={() => actionRef.current?.reload()}
          >
            刷新
          </Button>,
        ]}
        columns={columns}
      />

      <ModalForm<GroupFormValues>
        key={formKey}
        title={editing ? `编辑分组：${editing.name}` : '新增分组'}
        width={520}
        open={formOpen}
        onOpenChange={setFormOpen}
        initialValues={{
          name: editing?.name,
          description: editing?.description,
        }}
        modalProps={{ destroyOnHidden: true, maskClosable: false }}
        submitter={{ searchConfig: { submitText: '保存' } }}
        onFinish={submitGroup}
      >
        <ProFormText
          name="name"
          label="分组名称"
          rules={[{ required: true, message: '请输入分组名称' }]}
        />
        <ProFormTextArea
          name="description"
          label="描述"
          fieldProps={{ rows: 3 }}
          placeholder="例如：生产环境 / 办公网 / 数据库"
        />
      </ModalForm>
    </PageContainer>
  );
};

export default HostGroups;
