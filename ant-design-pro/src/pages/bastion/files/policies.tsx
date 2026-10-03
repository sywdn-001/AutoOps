/**
 * 文件策略页（需求：谁能在哪些路径上做哪些文件操作）。
 *
 * 上半部分：文件策略列表 + 策略 CRUD + 规则管理抽屉（规则 CRUD）。
 * 下半部分：文件操作试算器——选操作 + 路径，实时看命中哪条规则、放行还是拦截。
 *
 * 规则语义：按「优先级」从小到大逐条匹配，第一条命中的规则生效；都没命中走策略的默认动作。
 * 改名/移动/复制会同时拿源路径与目标路径过策略，任一被拦即拒绝。
 */
import { type ActionType, PageContainer, ProTable } from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Switch,
  Table,
  type TableColumnsType,
  Tag,
  Typography,
} from 'antd';
import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActionTag, MonoCell, RiskTag, TimeCell } from '@/components/Bastion';
import { RISK_OPTIONS } from '@/services/bastion/constants';
import {
  type FilePolicyPayload,
  type FileRulePayload,
  filePolicyApi,
  fileRuleApi,
} from '@/services/bastion/endpoints';
import type {
  FileDecision,
  FileMatchType,
  FilePolicyAction,
  FilePolicyItem,
  FilePolicyOption,
  FileRuleItem,
} from '@/services/bastion/types';

const { Text, Paragraph } = Typography;

const DEFAULT_RULE_PRIORITY = 100;

/** 文件操作候选（`GET /api/file-policies/operations` 返回 `[{value,label}]`，value 是操作名） */
type FileNameOption = { value: string; label: string };

const FILE_MATCH_OPTIONS: { label: string; value: FileMatchType }[] = [
  { label: '通配符（* 可跨目录）', value: 'glob' },
  { label: '正则表达式', value: 'regex' },
  { label: '前缀匹配', value: 'prefix' },
  { label: '包含匹配', value: 'contains' },
];

const FILE_ACTION_OPTIONS: { label: string; value: FilePolicyAction }[] = [
  { label: '放行', value: 'allow' },
  { label: '拦截', value: 'deny' },
];

/** 匹配方式示例（放在输入框下方，避免写出不起作用的规则） */
const MATCH_EXAMPLES: Record<string, string> = {
  glob: '示例：/etc/** 匹配 /etc 下任意层级；/home/*/data 只匹配一层目录；* 可跨 / 匹配。',
  regex: '示例：^/var/log/.*\\.log$（正则里的特殊字符需要转义）。',
  prefix: '示例：/srv/app 匹配 /srv/app 与其下所有路径。',
  contains: '示例：.sql 匹配路径里任何含 .sql 的文件。',
};

type RuleFormValues = {
  priority?: number;
  action?: FilePolicyAction;
  operation?: string;
  matchType?: FileMatchType;
  pathPattern?: string;
  riskLevel?: string;
  description?: string;
  enabled?: boolean;
};

const FilePoliciesPage: React.FC = () => {
  const { message } = App.useApp();
  const access = useAccess();
  const actionRef = useRef<ActionType | undefined>(undefined);

  const [operations, setOperations] = useState<FileNameOption[]>([]);
  const [policyOptions, setPolicyOptions] = useState<FilePolicyOption[]>([]);
  const [rulesFor, setRulesFor] = useState<FilePolicyItem | null>(null);
  const [rules, setRules] = useState<FileRuleItem[]>([]);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [policyModal, setPolicyModal] = useState<{ open: boolean; target?: FilePolicyItem }>({
    open: false,
  });
  const [ruleModal, setRuleModal] = useState<{ open: boolean; target?: FileRuleItem }>({
    open: false,
  });
  const [saving, setSaving] = useState(false);
  const [policyForm] = Form.useForm<FilePolicyPayload>();
  const [ruleForm] = Form.useForm<RuleFormValues>();

  const canManage = Boolean(access.canFilePolicyManage);
  const matchType = Form.useWatch('matchType', ruleForm);

  const loadOptions = useCallback(async () => {
    try {
      const [ops, options] = await Promise.all([
        filePolicyApi.operations(),
        filePolicyApi.options(),
      ]);
      setOperations(ops ?? []);
      setPolicyOptions(options ?? []);
    } catch (err) {
      message.error((err as Error)?.message || '加载文件策略选项失败');
    }
  }, [message]);

  useEffect(() => {
    void loadOptions();
  }, [loadOptions]);

  const loadRules = useCallback(
    async (policy: FilePolicyItem) => {
      setRulesLoading(true);
      try {
        const data = await fileRuleApi.list(policy.id);
        setRules(data ?? []);
      } catch (err) {
        message.error((err as Error)?.message || '加载规则失败');
        setRules([]);
      } finally {
        setRulesLoading(false);
      }
    },
    [message],
  );

  const openRules = useCallback(
    (policy: FilePolicyItem) => {
      setRulesFor(policy);
      void loadRules(policy);
    },
    [loadRules],
  );

  const operationLabel = useCallback(
    (value: string) => operations.find((item) => item.value === value)?.label || value,
    [operations],
  );

  const openPolicyModal = useCallback(
    (target?: FilePolicyItem) => {
      setPolicyModal({ open: true, target });
      policyForm.setFieldsValue({
        name: target?.name || '',
        description: target?.description || '',
        defaultAction: target?.defaultAction || 'allow',
      });
    },
    [policyForm],
  );

  const submitPolicy = useCallback(async () => {
    const values = await policyForm.validateFields();
    setSaving(true);
    try {
      if (policyModal.target) {
        await filePolicyApi.update(policyModal.target.id, values);
        message.success('策略已更新');
      } else {
        await filePolicyApi.create(values);
        message.success('策略已创建');
      }
      setPolicyModal({ open: false });
      await loadOptions();
      actionRef.current?.reload();
    } catch (err) {
      message.error((err as Error)?.message || '保存策略失败');
    } finally {
      setSaving(false);
    }
  }, [actionRef, loadOptions, message, policyForm, policyModal.target]);

  const removePolicy = useCallback(
    async (policy: FilePolicyItem) => {
      try {
        await filePolicyApi.remove(policy.id);
        message.success('策略已删除');
        if (rulesFor?.id === policy.id) {
          setRulesFor(null);
        }
        await loadOptions();
        actionRef.current?.reload();
      } catch (err) {
        message.error((err as Error)?.message || '删除策略失败');
      }
    },
    [actionRef, loadOptions, message, rulesFor?.id],
  );

  const openRuleModal = useCallback(
    (target?: FileRuleItem) => {
      if (!rulesFor) {
        return;
      }
      setRuleModal({ open: true, target });
      ruleForm.setFieldsValue({
        priority: target?.priority ?? DEFAULT_RULE_PRIORITY,
        action: target?.action ?? 'deny',
        operation: target?.operation ?? '*',
        matchType: target?.matchType ?? 'glob',
        pathPattern: target?.pathPattern ?? '',
        riskLevel: target?.riskLevel ?? 'medium',
        description: target?.description ?? '',
        enabled: target?.enabled ?? true,
      });
    },
    [ruleForm, rulesFor],
  );

  const submitRule = useCallback(async () => {
    if (!rulesFor) {
      return;
    }
    const values = await ruleForm.validateFields();
    setSaving(true);
    try {
      if (ruleModal.target) {
        await fileRuleApi.update(ruleModal.target.id, values as FileRulePayload);
        message.success('规则已更新');
      } else {
        await fileRuleApi.create({ ...values, policyId: rulesFor.id } as FileRulePayload);
        message.success('规则已添加');
      }
      setRuleModal({ open: false });
      await loadRules(rulesFor);
      actionRef.current?.reload();
    } catch (err) {
      message.error((err as Error)?.message || '保存规则失败');
    } finally {
      setSaving(false);
    }
  }, [actionRef, loadRules, message, ruleForm, ruleModal.target, rulesFor]);

  const removeRule = useCallback(
    async (rule: FileRuleItem) => {
      try {
        await fileRuleApi.remove(rule.id);
        message.success('规则已删除');
        if (rulesFor) {
          await loadRules(rulesFor);
        }
        actionRef.current?.reload();
      } catch (err) {
        message.error((err as Error)?.message || '删除规则失败');
      }
    },
    [actionRef, loadRules, message, rulesFor],
  );

  const ruleColumns = useMemo<TableColumnsType<FileRuleItem>>(
    () => [
      {
        title: '优先级',
        dataIndex: 'priority',
        width: 88,
        render: (_, row) => <Text strong>{row.priority}</Text>,
      },
      {
        title: '动作',
        dataIndex: 'action',
        width: 90,
        render: (_, row) => <ActionTag action={row.action} />,
      },
      {
        title: '操作',
        dataIndex: 'operation',
        width: 140,
        render: (_, row) =>
          row.operation === '*' ? <Tag>全部操作</Tag> : <span>{operationLabel(row.operation)}</span>,
      },
      {
        title: '匹配方式',
        dataIndex: 'matchType',
        width: 120,
        render: (_, row) =>
          FILE_MATCH_OPTIONS.find((item) => item.value === row.matchType)?.label || row.matchType,
      },
      {
        title: '路径 / 模式',
        dataIndex: 'pathPattern',
        render: (_, row) => <MonoCell text={row.pathPattern} />,
      },
      {
        title: '风险',
        dataIndex: 'riskLevel',
        width: 90,
        render: (_, row) => <RiskTag level={row.riskLevel} />,
      },
      { title: '说明', dataIndex: 'description', ellipsis: true },
      {
        title: '启用',
        dataIndex: 'enabled',
        width: 80,
        render: (_, row) => (row.enabled ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
      },
      {
        title: '操作',
        key: 'option',
        width: 130,
        render: (_, row) => [
          <Button
            key="edit"
            type="link"
            size="small"
            disabled={!canManage}
            onClick={() => openRuleModal(row)}
          >
            编辑
          </Button>,
          <Popconfirm
            key="remove"
            title="删除这条规则？"
            onConfirm={() => removeRule(row)}
            disabled={!canManage}
          >
            <Button type="link" size="small" danger disabled={!canManage}>
              删除
            </Button>
          </Popconfirm>,
        ],
      },
    ],
    [canManage, openRuleModal, operationLabel, removeRule],
  );

  return (
    <PageContainer
      title="文件策略"
      subTitle="谁能操作哪些路径上的文件"
      extra={[
        <Popconfirm
          key="reset"
          title="重置内置文件策略？"
          description="内置策略的规则会还原为出厂设置，自建策略不受影响。"
          disabled={!canManage}
          onConfirm={async () => {
            try {
              await filePolicyApi.resetBuiltin();
              message.success('内置文件策略已还原');
              await loadOptions();
              actionRef.current?.reload();
            } catch (err) {
              message.error((err as Error)?.message || '重置失败');
            }
          }}
        >
          <Button disabled={!canManage}>重置内置策略</Button>
        </Popconfirm>,
        <Button key="create" type="primary" disabled={!canManage} onClick={() => openPolicyModal()}>
          新建策略
        </Button>,
      ]}
    >
      <ProTable<FilePolicyItem>
        rowKey="id"
        actionRef={actionRef}
        search={{ labelWidth: 'auto' }}
        options={false}
        cardBordered
        pagination={{ pageSize: 20, showSizeChanger: true }}
        columns={[
          {
            title: '策略名称',
            dataIndex: 'name',
            width: 240,
            render: (_, row) => (
              <Space size={6}>
                <Text strong>{row.name}</Text>
                {row.isDefault ? <Tag color="blue">默认</Tag> : null}
              </Space>
            ),
          },
          { title: '说明', dataIndex: 'description', ellipsis: true },
          {
            title: '默认动作',
            dataIndex: 'defaultAction',
            width: 110,
            render: (_, row) => <ActionTag action={row.defaultAction} />,
          },
          {
            title: '规则数',
            dataIndex: 'ruleCount',
            width: 90,
            render: (_, row) => `${row.ruleCount} 条`,
          },
          {
            title: '引用授权',
            dataIndex: 'grantCount',
            width: 100,
            render: (_, row) => `${row.grantCount} 条`,
          },
          {
            title: '更新时间',
            dataIndex: 'updatedAt',
            width: 180,
            render: (_, row) => <TimeCell value={row.updatedAt} />,
          },
          {
            title: '操作',
            valueType: 'option',
            width: 230,
            render: (_, row) => [
              <Button key="rules" type="link" size="small" onClick={() => openRules(row)}>
                规则配置
              </Button>,
              <Button
                key="edit"
                type="link"
                size="small"
                disabled={!canManage}
                onClick={() => openPolicyModal(row)}
              >
                编辑
              </Button>,
              <Popconfirm
                key="remove"
                title="删除这个策略？"
                onConfirm={() => removePolicy(row)}
                disabled={!canManage}
              >
                <Button type="link" size="small" danger disabled={!canManage}>
                  删除
                </Button>
              </Popconfirm>,
            ],
          },
        ]}
        request={async (params) => {
          try {
            const res = await filePolicyApi.list({
              current: params.current,
              pageSize: params.pageSize,
              keyword: params.name,
            });
            return { data: res.data ?? [], total: res.total ?? 0, success: true };
          } catch (err) {
            message.error((err as Error)?.message || '加载文件策略失败');
            return { data: [], total: 0, success: false };
          }
        }}
      />

      <Evaluator operations={operations} policies={policyOptions} onNeedOptions={loadOptions} />

      <Drawer
        open={Boolean(rulesFor)}
        width={1080}
        title={rulesFor ? `规则管理 · ${rulesFor.name}` : '规则管理'}
        onClose={() => setRulesFor(null)}
        destroyOnHidden
        extra={
          <Button type="primary" disabled={!canManage} onClick={() => openRuleModal()}>
            新增规则
          </Button>
        }
      >
        <Table<FileRuleItem>
          rowKey="id"
          size="small"
          loading={rulesLoading}
          dataSource={rules}
          columns={ruleColumns}
          pagination={false}
        />
      </Drawer>

      <Modal
        open={policyModal.open}
        title={policyModal.target ? '编辑文件策略' : '新建文件策略'}
        confirmLoading={saving}
        onCancel={() => setPolicyModal({ open: false })}
        onOk={submitPolicy}
        destroyOnHidden
      >
        <Form form={policyForm} layout="vertical" preserve={false}>
          <Form.Item
            name="name"
            label="策略名称"
            rules={[{ required: true, message: '请输入策略名称' }]}
          >
            <Input placeholder="例如：生产环境只读策略" maxLength={64} />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={2} maxLength={200} />
          </Form.Item>
          <Form.Item
            name="defaultAction"
            label="未命中任何规则时"
            rules={[{ required: true }]}
            extra="默认放行适合「只拦敏感路径」，默认拦截适合「只放行白名单路径」。"
          >
            <Radio.Group>
              <Radio.Button value="allow">默认放行</Radio.Button>
              <Radio.Button value="deny">默认拦截</Radio.Button>
            </Radio.Group>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={ruleModal.open}
        title={ruleModal.target ? '编辑规则' : '新增规则'}
        confirmLoading={saving}
        onCancel={() => setRuleModal({ open: false })}
        onOk={submitRule}
        width={680}
        destroyOnHidden
      >
        <Form form={ruleForm} layout="vertical" preserve={false}>
          <Space size={12} align="start" wrap>
            <Form.Item name="priority" label="优先级" rules={[{ required: true }]}>
              <InputNumber min={1} max={9999} style={{ width: 110 }} />
            </Form.Item>
            <Form.Item name="operation" label="适用操作" rules={[{ required: true }]}>
              <Select
                style={{ width: 200 }}
                options={[{ value: '*', label: '全部操作' }, ...operations]}
              />
            </Form.Item>
            <Form.Item name="action" label="动作" rules={[{ required: true }]}>
              <Select style={{ width: 110 }} options={FILE_ACTION_OPTIONS} />
            </Form.Item>
            <Form.Item name="riskLevel" label="风险等级" rules={[{ required: true }]}>
              <Select style={{ width: 120 }} options={RISK_OPTIONS} />
            </Form.Item>
          </Space>
          <Space size={12} align="start" wrap>
            <Form.Item name="matchType" label="匹配方式" rules={[{ required: true }]}>
              <Select style={{ width: 200 }} options={FILE_MATCH_OPTIONS} />
            </Form.Item>
            <Form.Item
              name="pathPattern"
              label="路径 / 模式"
              rules={[{ required: true, message: '请输入路径或匹配模式' }]}
            >
              <Input style={{ width: 380 }} placeholder="例如：/etc" />
            </Form.Item>
          </Space>
          <Paragraph style={{ marginTop: -8 }}>
            <Text type="secondary">
              {MATCH_EXAMPLES[matchType || 'glob'] || MATCH_EXAMPLES.glob}
            </Text>
          </Paragraph>
          <Form.Item name="description" label="说明">
            <Input maxLength={120} placeholder="例如：禁止修改系统目录下的文件" />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
};

/** 文件操作试算器：给「操作 + 路径」看会命中哪条规则、放行还是拦截。 */
const Evaluator: React.FC<{
  operations: FileNameOption[];
  policies: FilePolicyOption[];
  onNeedOptions: () => Promise<void>;
}> = ({ operations, policies, onNeedOptions }) => {
  const { message } = App.useApp();
  const [form] = Form.useForm<{
    operation?: string;
    path?: string;
    targetPath?: string;
    policyId?: number;
  }>();
  const [decision, setDecision] = useState<FileDecision | null>(null);
  const [loading, setLoading] = useState(false);
  const operation = Form.useWatch('operation', form);

  useEffect(() => {
    if (!operations.length || !policies.length) {
      void onNeedOptions();
    }
  }, [onNeedOptions, operations.length, policies.length]);

  const needsTarget = operation === 'rename' || operation === 'move' || operation === 'copy';

  const submit = useCallback(async () => {
    const values = await form.validateFields();
    setLoading(true);
    try {
      const data = await filePolicyApi.evaluate({
        operation: values.operation as string,
        path: values.path as string,
        targetPath: values.targetPath || undefined,
        policyId: values.policyId || undefined,
      });
      setDecision(data);
    } catch (err) {
      setDecision(null);
      message.error((err as Error)?.message || '试算失败');
    } finally {
      setLoading(false);
    }
  }, [form, message]);

  return (
    <Card title="操作试算" style={{ marginTop: 16 }}>
      <Form form={form} layout="inline" initialValues={{ operation: 'delete' }}>
        <Form.Item name="operation" label="操作" rules={[{ required: true }]}>
          <Select style={{ width: 200 }} options={operations} placeholder="选择文件操作" />
        </Form.Item>
        <Form.Item name="path" label="路径" rules={[{ required: true, message: '请输入路径' }]}>
          <Input style={{ width: 240 }} placeholder="/etc/passwd" />
        </Form.Item>
        {needsTarget ? (
          <Form.Item name="targetPath" label="目标路径" rules={[{ required: true }]}>
            <Input style={{ width: 240 }} placeholder="/tmp/backup" />
          </Form.Item>
        ) : null}
        <Form.Item name="policyId" label="策略">
          <Select
            allowClear
            style={{ width: 200 }}
            placeholder="默认文件策略"
            options={policies.map((item) => ({ value: item.value, label: item.label }))}
          />
        </Form.Item>
        <Form.Item>
          <Button type="primary" loading={loading} onClick={submit}>
            试算
          </Button>
        </Form.Item>
      </Form>

      {decision ? (
        <Alert
          style={{ marginTop: 16 }}
          type={decision.allowed ? 'success' : 'error'}
          showIcon
          message={
            <Space size={8} wrap>
              <span>{decision.allowed ? '允许执行' : '拒绝执行'}</span>
              <ActionTag action={decision.action} />
              <RiskTag level={decision.riskLevel} />
              <Tag>{decision.policyName}</Tag>
            </Space>
          }
          description={
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="判定说明">{decision.reason}</Descriptions.Item>
              <Descriptions.Item label="命中规则">
                {decision.ruleId ? (
                  <Space size={8} wrap>
                    <Tag color="purple">#{decision.ruleId}</Tag>
                    <MonoCell text={decision.rulePattern} />
                  </Space>
                ) : (
                  <Text type="secondary">未命中任何规则，按默认动作处理</Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="试算路径">
                <MonoCell text={decision.path} />
              </Descriptions.Item>
            </Descriptions>
          }
        />
      ) : null}
    </Card>
  );
};

export default FilePoliciesPage;
