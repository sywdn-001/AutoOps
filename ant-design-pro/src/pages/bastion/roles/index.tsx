/**
 * 角色管理页（身份与权限 - 角色）。
 *
 * 内置角色与自定义角色共存：内置角色的 code 不可改、也不可删除（后端会拒绝）；
 * 权限项由 roleApi.permissions() 按业务域分组下发，这里按组渲染并支持全选/清空。
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
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type React from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { t } from '@/i18n';
import { type RolePayload, roleApi } from '@/services/bastion/endpoints';
import type { PermissionGroup, RoleItem } from '@/services/bastion/types';

const { Text } = Typography;

/** 角色表单字段（permissions 单独用受控状态收集，提交时合并） */
type RoleFormValues = {
  code: string;
  name: string;
  description?: string;
};

/** 角色编码：只能英文、数字、下划线 */
const CODE_PATTERN = /^[A-Za-z0-9_]+$/;

const fromApiError = (err: unknown, fallback: string): string =>
  (err as { response?: { data?: { message?: string } } })?.response?.data
    ?.message || fallback;

const RolesPage: React.FC = () => {
  const { message } = App.useApp();
  const access = useAccess();
  const canManage = Boolean(access.canRoleManage);
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [form] = Form.useForm<RoleFormValues>();
  const [groups, setGroups] = useState<PermissionGroup[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<RoleItem | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setGroups(await roleApi.permissions());
      } catch (err) {
        message.error(fromApiError(err, t('加载权限项失败')));
      }
    })();
  }, [message]);

  const allCodes = useMemo(
    () => groups.flatMap((group) => group.items.map((item) => item.code)),
    [groups],
  );

  const openCreate = () => {
    setEditing(null);
    setSelected([]);
    form.resetFields();
    setFormOpen(true);
  };

  const openEdit = (row: RoleItem) => {
    setEditing(row);
    setSelected(row.permissions ?? []);
    form.resetFields();
    form.setFieldsValue({
      code: row.code,
      name: row.name,
      description: row.description,
    });
    setFormOpen(true);
  };

  /** 整组勾选 / 清空 */
  const toggleGroup = (group: PermissionGroup, checked: boolean) => {
    const codes = group.items.map((item) => item.code);
    setSelected((prev) =>
      checked
        ? Array.from(new Set([...prev, ...codes]))
        : prev.filter((code) => !codes.includes(code)),
    );
  };

  /** 单组内的勾选变化：只替换该组的编码，保留其它组已选项 */
  const handleGroupChange = (group: PermissionGroup, next: string[]) => {
    const codes = group.items.map((item) => item.code);
    setSelected((prev) => [
      ...prev.filter((code) => !codes.includes(code)),
      ...next,
    ]);
  };

  const submitForm = async (values: RoleFormValues) => {
    if (selected.length === 0) {
      message.error(t('请至少勾选一项权限'));
      return;
    }
    setSubmitting(true);
    try {
      const payload: RolePayload = {
        code: values.code,
        name: values.name,
        description: values.description,
        permissions: selected,
      };
      if (editing) {
        await roleApi.update(editing.id, payload);
        message.success(t('角色已更新'));
      } else {
        await roleApi.create(payload);
        message.success(t('角色已创建'));
      }
      setFormOpen(false);
      actionRef.current?.reload();
    } catch (err) {
      message.error(
        fromApiError(err, editing ? t('更新角色失败') : t('创建角色失败')),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const removeRole = async (row: RoleItem) => {
    try {
      await roleApi.remove(row.id);
      message.success(t('角色已删除'));
      actionRef.current?.reload();
    } catch (err) {
      message.error(fromApiError(err, t('删除角色失败')));
    }
  };

  /** 删除按钮的提示：内置角色与仍被引用的角色后端会拒绝 */
  const deleteTip = (row: RoleItem): string => {
    if (row.isBuiltin) {
      return t('内置角色不可删除，系统依赖其权限定义');
    }
    if (row.userCount > 0) {
      return t('仍有 {0} 个用户使用该角色，需先为用户改派角色', {
        0: row.userCount,
      });
    }
    return '';
  };

  const columns: ProColumns<RoleItem>[] = [
    {
      title: t('角色编码'),
      dataIndex: 'code',
      width: 150,
      search: false,
      render: (_, row) => <Text code>{row.code}</Text>,
    },
    {
      title: t('角色名称'),
      dataIndex: 'name',
      width: 150,
      search: false,
    },
    {
      title: t('描述'),
      dataIndex: 'description',
      search: false,
      ellipsis: true,
      render: (_, row) => row.description || '-',
    },
    {
      title: t('内置角色'),
      dataIndex: 'isBuiltin',
      width: 110,
      search: false,
      render: (_, row) =>
        row.isBuiltin ? (
          <Tag color="gold">{t('内置')}</Tag>
        ) : (
          <Tag color="default">{t('自定义')}</Tag>
        ),
    },
    {
      title: t('权限数量'),
      dataIndex: 'permissions',
      width: 110,
      search: false,
      render: (_, row) => (
        <Tag color="blue">
          {t('{0} 项', { 0: row.permissions?.length ?? 0 })}
        </Tag>
      ),
    },
    {
      title: t('关联用户'),
      dataIndex: 'userCount',
      width: 110,
      search: false,
      render: (_, row) =>
        row.userCount > 0 ? (
          <Tag color="processing">{t('{0} 人', { 0: row.userCount })}</Tag>
        ) : (
          '-'
        ),
    },
    {
      title: t('操作'),
      valueType: 'option',
      key: 'option',
      fixed: 'right',
      width: 130,
      render: (_, row) => {
        if (!canManage) {
          return [
            <span key="none" style={{ color: 'rgba(0, 0, 0, 0.25)' }}>
              -
            </span>,
          ];
        }
        const tip = deleteTip(row);
        const deleteNode = (
          <Popconfirm
            key="delete"
            title={t('确认删除该角色？')}
            description={t('删除后不可恢复，使用该角色的用户会失去对应权限。')}
            okText={t('删除')}
            okButtonProps={{ danger: true }}
            cancelText={t('取消')}
            onConfirm={() => removeRole(row)}
          >
            <a style={{ color: '#cf1322' }}>{t('删除')}</a>
          </Popconfirm>
        );
        return [
          <Tooltip
            key="edit"
            title={
              row.isBuiltin
                ? t('内置角色：可调整名称、描述与权限，code 不可修改')
                : t('编辑角色与权限')
            }
          >
            <a onClick={() => void openEdit(row)}>{t('编辑')}</a>
          </Tooltip>,
          tip ? (
            <Tooltip key="delete" title={tip}>
              {deleteNode}
            </Tooltip>
          ) : (
            deleteNode
          ),
        ];
      },
    },
    {
      title: t('关键词'),
      dataIndex: 'keyword',
      key: 'keyword',
      hideInTable: true,
      fieldProps: { placeholder: t('角色编码 / 名称') },
    },
  ];

  return (
    <PageContainer
      header={{
        title: t('角色管理'),
        subTitle: t('角色定义与功能权限勾选，用户通过角色获得权限'),
      }}
    >
      <ProTable<RoleItem>
        rowKey="id"
        actionRef={actionRef}
        headerTitle={t('角色列表')}
        search={{ labelWidth: 'auto' }}
        pagination={{ defaultPageSize: 10 }}
        scroll={{ x: 'max-content' }}
        request={async (params) => {
          const res = await roleApi.list(params);
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
                  {t('新增角色')}
                </Button>,
              ]
            : []
        }
        columns={columns}
      />

      <Modal
        title={
          editing ? t('编辑角色「{0}」', { 0: editing.name }) : t('新增角色')
        }
        open={formOpen}
        width={720}
        onCancel={() => setFormOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={submitting}
        destroyOnHidden
        mask={{ closable: false }}
        okText={t('保存')}
        cancelText={t('取消')}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => void submitForm(values)}
        >
          <Form.Item
            name="code"
            label={t('角色编码')}
            rules={[
              { required: true, message: t('请输入角色编码') },
              {
                pattern: CODE_PATTERN,
                message: t('只能包含英文、数字与下划线'),
              },
            ]}
            extra={
              editing
                ? t('角色编码创建后不可修改')
                : t('例如 operator、auditor')
            }
          >
            <Input
              placeholder={t('英文 / 数字 / 下划线')}
              disabled={!!editing}
              autoComplete="off"
            />
          </Form.Item>
          <Form.Item
            name="name"
            label={t('角色名称')}
            rules={[{ required: true, message: t('请输入角色名称') }]}
          >
            <Input placeholder={t('例如：运维操作员')} />
          </Form.Item>
          <Form.Item name="description" label={t('描述')}>
            <Input.TextArea rows={3} placeholder={t('选填')} />
          </Form.Item>
          <Form.Item label={t('权限')} required>
            <div
              style={{
                maxHeight: 340,
                overflow: 'auto',
                border: '1px solid #f0f0f0',
                borderRadius: 6,
                padding: '8px 12px',
              }}
            >
              <Space size={12} style={{ marginBottom: 8 }}>
                <Text type="secondary">
                  {t('已选 {0} / {1} 项', {
                    0: selected.length,
                    1: allCodes.length,
                  })}
                </Text>
                <a onClick={() => setSelected(allCodes)}>{t('全选')}</a>
                <a onClick={() => setSelected([])}>{t('清空')}</a>
              </Space>
              {groups.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={t('暂无权限项')}
                />
              ) : (
                groups.map((group) => {
                  const codes = group.items.map((item) => item.code);
                  const groupSelected = codes.filter((code) =>
                    selected.includes(code),
                  );
                  return (
                    <div key={group.group} style={{ marginBottom: 12 }}>
                      <Space size={12} style={{ marginBottom: 6 }}>
                        <Text strong>{group.group}</Text>
                        <Text type="secondary">
                          {groupSelected.length}/{codes.length}
                        </Text>
                        <a onClick={() => toggleGroup(group, true)}>
                          {t('全选')}
                        </a>
                        <a onClick={() => toggleGroup(group, false)}>
                          {t('清空')}
                        </a>
                      </Space>
                      <div>
                        <Checkbox.Group
                          options={group.items.map((item) => ({
                            label: item.label,
                            value: item.code,
                          }))}
                          value={groupSelected}
                          onChange={(next) =>
                            handleGroupChange(
                              group,
                              next.map((code) => String(code)),
                            )
                          }
                        />
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
};

export default RolesPage;
