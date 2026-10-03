/**
 * 命令策略页（需求：谁能执行什么命令）。
 *
 * 上半部分：策略列表 + 策略 CRUD + 规则管理抽屉（规则 CRUD）。
 * 下半部分：命令试算器——输入一条命令，实时看命中哪条规则、放行还是拦截。
 */
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons';
import {
  type ActionType,
  PageContainer,
  type ProColumns,
  ProTable,
} from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
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
  Tag,
  Typography,
} from 'antd';
import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActionTag, MonoCell, RiskTag, TimeCell } from '@/components/Bastion';
import {
  MATCH_TYPE_OPTIONS,
  POLICY_ACTION_OPTIONS,
  RISK_OPTIONS,
} from '@/services/bastion/constants';
import {
  type PolicyPayload,
  policyApi,
  type RulePayload,
  ruleApi,
} from '@/services/bastion/endpoints';
import type {
  OptionItem,
  PolicyDecision,
  PolicyItem,
  PolicyRuleItem,
} from '@/services/bastion/types';

const { Text, Paragraph } = Typography;

const DEFAULT_RULE_PRIORITY = 100;

/** 匹配方式示例说明（放在规则表单输入框下方，避免用户瞎写正则） */
const MATCH_EXAMPLES: Record<string, string> = {
  exact:
    '整条命令完全等于 pattern 才命中。例：pattern=whoami 只拦 `whoami`，不拦 `sudo whoami`。',
  prefix:
    '命令以 pattern 开头才命中（不要求单词边界）。例：pattern=rm 会连 `rmdir` 一起命中；pattern=`rm -rf` 更精确。',
  contains: '命令任意位置包含 pattern 即命中。例：pattern=`/etc/shadow`。',
  regex:
    '按 Python 正则匹配整条命令。例：`^\\s*rm\\s+-rf\\s+/` 拦截递归强删根目录；注意转义与误伤范围。',
};

type PolicyFormValues = {
  name: string;
  description?: string;
  defaultAction: 'allow' | 'deny';
  initialRules?: {
    priority: number;
    matchType: string;
    pattern: string;
    action: string;
  }[];
};

type RuleFormValues = {
  priority: number;
  matchType: string;
  pattern: string;
  action: string;
  riskLevel: string;
  description?: string;
  enabled: boolean;
};

const INITIAL_POLICY_VALUES: Partial<PolicyFormValues> = {
  defaultAction: 'allow',
};

const INITIAL_RULE_VALUES: Partial<RuleFormValues> = {
  priority: DEFAULT_RULE_PRIORITY,
  matchType: 'regex',
  action: 'deny',
  riskLevel: 'high',
  enabled: true,
};

const fromApiError = (err: unknown, fallback: string): string =>
  (err as { response?: { data?: { message?: string } } })?.response?.data
    ?.message || fallback;

/** 试算结论展示 */
const DecisionView: React.FC<{ decision: PolicyDecision }> = ({ decision }) => (
  <Space orientation="vertical" size={12} style={{ width: '100%' }}>
    <Space size={8} wrap>
      {decision.allowed ? (
        <Tag color="success">放行</Tag>
      ) : (
        <Tag color="error">拦截</Tag>
      )}
      <Text type="secondary">命中动作</Text>
      <ActionTag action={decision.action} />
      <Text type="secondary">风险等级</Text>
      <RiskTag level={decision.riskLevel} />
      <Text type="secondary">
        生效策略：{decision.policyName || '默认策略'}
        {decision.policyId ? ` #${decision.policyId}` : ''}
      </Text>
    </Space>
    <Descriptions size="small" column={1} bordered>
      <Descriptions.Item label="命中规则">
        {decision.ruleId ? (
          <Space size={6}>
            <Text>#{decision.ruleId}</Text>
            <Text code>{decision.rulePattern || '-'}</Text>
          </Space>
        ) : (
          <Text type="secondary">未命中任何规则</Text>
        )}
      </Descriptions.Item>
      <Descriptions.Item label="判定原因">
        <Paragraph style={{ marginBottom: 0 }}>
          {decision.reason || '-'}
        </Paragraph>
      </Descriptions.Item>
    </Descriptions>
    <Table
      size="small"
      rowKey={(row) => `${row.segment}-${row.rulePattern ?? ''}-${row.reason}`}
      dataSource={decision.segments ?? []}
      pagination={false}
      locale={{ emptyText: '该命令没有分段信息' }}
      columns={[
        {
          title: '命令分段',
          dataIndex: 'segment',
          width: 240,
          render: (_, row) => (
            <MonoCell text={row.segment} max={48} width={200} />
          ),
        },
        {
          title: '结论',
          dataIndex: 'allowed',
          width: 90,
          render: (_, row) =>
            row.allowed ? (
              <Tag color="success">放行</Tag>
            ) : (
              <Tag color="error">拦截</Tag>
            ),
        },
        {
          title: '动作',
          dataIndex: 'action',
          width: 90,
          render: (_, row) => <ActionTag action={row.action} />,
        },
        {
          title: '风险等级',
          dataIndex: 'riskLevel',
          width: 100,
          render: (_, row) => <RiskTag level={row.riskLevel} />,
        },
        {
          title: '规则 ID',
          dataIndex: 'ruleId',
          width: 90,
          render: (_, row) =>
            row.ruleId ? (
              <Text code>#{row.ruleId}</Text>
            ) : (
              <Text type="secondary">-</Text>
            ),
        },
        {
          title: '命中规则',
          dataIndex: 'rulePattern',
          width: 220,
          render: (_, row) => (
            <MonoCell text={row.rulePattern} max={36} width={180} />
          ),
        },
        {
          title: '原因',
          dataIndex: 'reason',
          render: (_, row) => <Text type="secondary">{row.reason || '-'}</Text>,
        },
      ]}
    />
  </Space>
);

/** 命令试算器（只读，policy:view 即可用） */
const CommandEvaluator: React.FC<{
  policyOptions: OptionItem[];
  defaultPolicyId?: number;
}> = ({ policyOptions, defaultPolicyId }) => {
  const { message } = App.useApp();
  const [command, setCommand] = useState('rm -rf /');
  const [policyId, setPolicyId] = useState<number | undefined>(defaultPolicyId);
  const [decision, setDecision] = useState<PolicyDecision | null>(null);
  const [testing, setTesting] = useState(false);

  const evaluate = async () => {
    const target = policyId ?? defaultPolicyId;
    setTesting(true);
    try {
      const result =
        target !== undefined
          ? await policyApi.test(target, command)
          : await policyApi.evaluate(command);
      setDecision(result);
    } catch (err) {
      message.error(fromApiError(err, '命令试算失败'));
    } finally {
      setTesting(false);
    }
  };

  return (
    <Card
      title="命令试算器"
      extra={
        <Text type="secondary">
          输入一条命令，先看策略会不会拦，再决定要不要上线
        </Text>
      }
    >
      <Space orientation="vertical" size={12} style={{ width: '100%' }}>
        <Input.TextArea
          rows={3}
          value={command}
          onChange={(event) => setCommand(event.target.value)}
          placeholder="例如：rm -rf /"
        />
        <Space size={8} wrap>
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ width: 260 }}
            value={policyId}
            onChange={(value) => setPolicyId(value)}
            placeholder="留空 = 使用默认策略"
            options={policyOptions}
          />
          <Button
            type="primary"
            loading={testing}
            onClick={() => void evaluate()}
          >
            试算
          </Button>
        </Space>
        {decision ? (
          <DecisionView decision={decision} />
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有试算结果"
          />
        )}
      </Space>
    </Card>
  );
};

const PoliciesPage: React.FC = () => {
  const access = useAccess();
  const canManage = Boolean(access.canPolicyManage);
  const { message } = App.useApp();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const rulesActionRef = useRef<ActionType | undefined>(undefined);
  const [policyForm] = Form.useForm<PolicyFormValues>();
  const [ruleForm] = Form.useForm<RuleFormValues>();
  const [policyOptions, setPolicyOptions] = useState<OptionItem[]>([]);
  const [policyOpen, setPolicyOpen] = useState(false);
  const [ruleOpen, setRuleOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingPolicy, setEditingPolicy] = useState<PolicyItem | null>(null);
  const [editingRule, setEditingRule] = useState<PolicyRuleItem | null>(null);
  const [currentPolicy, setCurrentPolicy] = useState<PolicyItem | null>(null);
  const [drawerRules, setDrawerRules] = useState<PolicyRuleItem[]>([]);
  const [submitting, setSubmitting] = useState(false);

  const loadPolicyOptions = useCallback(async () => {
    try {
      setPolicyOptions(await policyApi.options());
    } catch (err) {
      message.error(fromApiError(err, '加载策略选项失败'));
    }
  }, [message]);

  const openCreatePolicy = () => {
    setEditingPolicy(null);
    policyForm.resetFields();
    policyForm.setFieldsValue(INITIAL_POLICY_VALUES);
    setPolicyOpen(true);
  };

  const openEditPolicy = (row: PolicyItem) => {
    setEditingPolicy(row);
    policyForm.resetFields();
    policyForm.setFieldsValue({
      name: row.name,
      description: row.description,
      defaultAction: row.defaultAction === 'deny' ? 'deny' : 'allow',
    });
    setPolicyOpen(true);
  };

  const submitPolicy = async (values: PolicyFormValues) => {
    setSubmitting(true);
    try {
      const rules: RulePayload[] = (values.initialRules ?? []).map((rule) => ({
        priority: rule.priority,
        matchType: rule.matchType,
        pattern: rule.pattern,
        action: rule.action,
      }));
      const payload: PolicyPayload = {
        name: values.name,
        description: values.description,
        defaultAction: values.defaultAction,
        ...(rules.length > 0 ? { rules } : {}),
      };
      if (editingPolicy) {
        await policyApi.update(editingPolicy.id, payload);
        message.success('策略已更新');
      } else {
        await policyApi.create(payload);
        message.success('策略已创建');
      }
      setPolicyOpen(false);
      actionRef.current?.reload();
      await loadPolicyOptions();
    } catch (err) {
      message.error(
        fromApiError(err, editingPolicy ? '更新策略失败' : '创建策略失败'),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const removePolicy = async (row: PolicyItem) => {
    try {
      await policyApi.remove(row.id);
      message.success('策略已删除');
      actionRef.current?.reload();
      await loadPolicyOptions();
    } catch (err) {
      message.error(fromApiError(err, '删除策略失败'));
    }
  };

  const resetBuiltin = async () => {
    try {
      await policyApi.resetBuiltin();
      message.success('内置策略已重建');
      actionRef.current?.reload();
      await loadPolicyOptions();
    } catch (err) {
      message.error(fromApiError(err, '恢复内置策略失败'));
    }
  };

  const refreshDrawerRules = async (policyId: number) => {
    try {
      const detail = await policyApi.getDetail(policyId);
      setCurrentPolicy(detail);
      setDrawerRules(detail.rules ?? []);
    } catch (err) {
      message.error(fromApiError(err, '加载策略规则失败'));
    }
  };

  const openRules = async (row: PolicyItem) => {
    setCurrentPolicy(row);
    setDrawerRules(row.rules ?? []);
    setDrawerOpen(true);
    await refreshDrawerRules(row.id);
  };

  const openCreateRule = () => {
    setEditingRule(null);
    ruleForm.resetFields();
    ruleForm.setFieldsValue(INITIAL_RULE_VALUES);
    setRuleOpen(true);
  };

  const openEditRule = (row: PolicyRuleItem) => {
    setEditingRule(row);
    ruleForm.resetFields();
    ruleForm.setFieldsValue({
      priority: row.priority,
      matchType: row.matchType,
      pattern: row.pattern,
      action: row.action,
      riskLevel: row.riskLevel,
      description: row.description,
      enabled: row.enabled,
    });
    setRuleOpen(true);
  };

  const submitRule = async (values: RuleFormValues) => {
    if (!currentPolicy) return;
    setSubmitting(true);
    try {
      const payload: RulePayload = {
        policyId: currentPolicy.id,
        priority: values.priority,
        matchType: values.matchType,
        pattern: values.pattern,
        action: values.action,
        riskLevel: values.riskLevel,
        description: values.description,
        enabled: values.enabled,
      };
      if (editingRule) {
        await ruleApi.update(editingRule.id, payload);
        message.success('规则已更新');
      } else {
        await ruleApi.create(payload);
        message.success('规则已创建');
      }
      setRuleOpen(false);
      await refreshDrawerRules(currentPolicy.id);
      rulesActionRef.current?.reload();
      actionRef.current?.reload();
    } catch (err) {
      message.error(
        fromApiError(err, editingRule ? '更新规则失败' : '创建规则失败'),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const removeRule = async (row: PolicyRuleItem) => {
    try {
      await ruleApi.remove(row.id);
      message.success('规则已删除');
      if (currentPolicy) {
        await refreshDrawerRules(currentPolicy.id);
      }
      actionRef.current?.reload();
    } catch (err) {
      message.error(fromApiError(err, '删除规则失败'));
    }
  };

  const toggleRule = async (row: PolicyRuleItem, enabled: boolean) => {
    try {
      await ruleApi.update(row.id, { enabled });
      message.success(enabled ? '规则已启用' : '规则已停用');
      setDrawerRules((prev) =>
        prev.map((item) => (item.id === row.id ? { ...item, enabled } : item)),
      );
    } catch (err) {
      message.error(fromApiError(err, '切换规则状态失败'));
    }
  };

  const ruleColumns: ProColumns<PolicyRuleItem>[] = useMemo(
    () => [
      {
        title: '规则 ID',
        dataIndex: 'id',
        width: 90,
        search: false,
        // 拦截提示里会写「命中规则 #13」，这里必须能看到编号，否则管理员只能靠说明猜是哪条。
        render: (_, row) => <Text code>#{row.id}</Text>,
      },
      { title: '优先级', dataIndex: 'priority', width: 80, search: false },
      {
        title: '匹配方式',
        dataIndex: 'matchType',
        width: 120,
        valueType: 'select',
        fieldProps: { options: MATCH_TYPE_OPTIONS },
        render: (_, row) =>
          MATCH_TYPE_OPTIONS.find((item) => item.value === row.matchType)
            ?.label ?? row.matchType,
      },
      {
        title: '模式',
        dataIndex: 'pattern',
        width: 240,
        render: (_, row) => (
          <MonoCell text={row.pattern} max={40} width={200} />
        ),
      },
      {
        title: '动作',
        dataIndex: 'action',
        width: 90,
        valueType: 'select',
        fieldProps: { options: POLICY_ACTION_OPTIONS },
        render: (_, row) => <ActionTag action={row.action} />,
      },
      {
        title: '风险等级',
        dataIndex: 'riskLevel',
        width: 100,
        valueType: 'select',
        fieldProps: { options: RISK_OPTIONS },
        render: (_, row) => <RiskTag level={row.riskLevel} />,
      },
      {
        title: '说明',
        dataIndex: 'description',
        width: 200,
        search: false,
        render: (_, row) => (
          <Text type="secondary">{row.description || '-'}</Text>
        ),
      },
      {
        title: '启用',
        dataIndex: 'enabled',
        width: 80,
        search: false,
        render: (_, row) =>
          canManage ? (
            <Switch
              size="small"
              checked={row.enabled}
              onChange={(checked) => void toggleRule(row, checked)}
            />
          ) : (
            <Tag color={row.enabled ? 'success' : 'default'}>
              {row.enabled ? '启用' : '停用'}
            </Tag>
          ),
      },
      {
        title: '操作',
        valueType: 'option',
        key: 'option',
        width: 110,
        render: (_, row) => {
          if (!canManage)
            return [
              <Text key="none" type="secondary">
                -
              </Text>,
            ];
          return [
            <a key="edit" onClick={() => openEditRule(row)}>
              编辑
            </a>,
            <Popconfirm
              key="delete"
              title="确认删除该规则？"
              description="删除后该模式将不再被这条策略拦截。"
              okText="删除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={() => removeRule(row)}
            >
              <a style={{ color: '#cf1322' }}>删除</a>
            </Popconfirm>,
          ];
        },
      },
    ],
    [canManage, currentPolicy?.id],
  );

  useEffect(() => {
    // 试算器的策略下拉：只在挂载时拉一次，CRUD 后再刷新
    void loadPolicyOptions();
  }, [loadPolicyOptions]);

  return (
    <PageContainer
      header={{
        title: '命令策略',
        subTitle: '默认动作定基调，规则按优先级逐条命中，试算器先验证再上线',
      }}
    >
      <Space orientation="vertical" size={16} style={{ width: '100%' }}>
        <ProTable<PolicyItem>
          rowKey="id"
          actionRef={actionRef}
          headerTitle="策略列表"
          search={{ labelWidth: 'auto' }}
          pagination={{ defaultPageSize: 10 }}
          scroll={{ x: 'max-content' }}
          request={async (params) => {
            const res = await policyApi.list(params);
            return { data: res.data, total: res.total, success: res.success };
          }}
          toolBarRender={() =>
            canManage
              ? [
                  <Popconfirm
                    key="reset"
                    title="确认恢复内置策略？"
                    description="会按内置定义重建默认策略与规则，手工改动可能被覆盖。"
                    okText="恢复"
                    cancelText="取消"
                    onConfirm={resetBuiltin}
                  >
                    <Button>恢复内置策略</Button>
                  </Popconfirm>,
                  <Button
                    key="create"
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={openCreatePolicy}
                  >
                    新增策略
                  </Button>,
                ]
              : []
          }
          columns={[
            { title: '策略名', dataIndex: 'name', width: 180 },
            {
              title: '描述',
              dataIndex: 'description',
              width: 240,
              search: false,
              render: (_, row) => (
                <Text type="secondary">{row.description || '-'}</Text>
              ),
            },
            {
              title: '默认动作',
              dataIndex: 'defaultAction',
              width: 110,
              search: false,
              render: (_, row) => <ActionTag action={row.defaultAction} />,
            },
            {
              title: '是否默认',
              dataIndex: 'isDefault',
              width: 110,
              search: false,
              render: (_, row) =>
                row.isDefault ? (
                  <Tag color="blue">默认策略</Tag>
                ) : (
                  <Text type="secondary">-</Text>
                ),
            },
            {
              title: '规则数',
              dataIndex: 'ruleCount',
              width: 90,
              search: false,
              render: (_, row) => <Text>{row.ruleCount}</Text>,
            },
            {
              title: '被授权引用',
              dataIndex: 'grantCount',
              width: 110,
              search: false,
              render: (_, row) =>
                row.grantCount > 0 ? (
                  <Tag color="processing">{row.grantCount} 条授权</Tag>
                ) : (
                  <Text type="secondary">0</Text>
                ),
            },
            {
              title: '更新时间',
              dataIndex: 'updatedAt',
              width: 170,
              search: false,
              render: (_, row) => <TimeCell value={row.updatedAt} />,
            },
            {
              title: '操作',
              valueType: 'option',
              key: 'option',
              width: 180,
              render: (_, row) => {
                if (!canManage)
                  return [
                    <Text key="none" type="secondary">
                      -
                    </Text>,
                  ];
                return [
                  <a key="rules" onClick={() => void openRules(row)}>
                    规则管理
                  </a>,
                  <a key="edit" onClick={() => openEditPolicy(row)}>
                    编辑
                  </a>,
                  <Popconfirm
                    key="delete"
                    title="确认删除该策略？"
                    description="仍被授权引用的策略删除后，相关授权会回落到默认策略。"
                    okText="删除"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() => removePolicy(row)}
                  >
                    <a style={{ color: '#cf1322' }}>删除</a>
                  </Popconfirm>,
                ];
              },
            },
          ]}
        />

        <CommandEvaluator policyOptions={policyOptions} />
      </Space>

      <Modal
        title={editingPolicy ? `编辑策略 #${editingPolicy.id}` : '新增策略'}
        open={policyOpen}
        width={640}
        onCancel={() => setPolicyOpen(false)}
        onOk={() => policyForm.submit()}
        confirmLoading={submitting}
        destroyOnHidden
        okText="保存"
        cancelText="取消"
      >
        <Form
          form={policyForm}
          layout="vertical"
          initialValues={INITIAL_POLICY_VALUES}
          onFinish={(values) => void submitPolicy(values)}
        >
          <Form.Item
            name="name"
            label="策略名"
            rules={[{ required: true, message: '请输入策略名' }]}
          >
            <Input placeholder="例如：生产库高危命令拦截" maxLength={64} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} maxLength={200} showCount />
          </Form.Item>
          <Form.Item
            name="defaultAction"
            label="默认动作"
            rules={[{ required: true, message: '请选择默认动作' }]}
            extra="规则都没命中时，用这个动作兜底；漏配的规则就靠它保命"
          >
            <Radio.Group>
              <Space orientation="vertical" size={4}>
                <Radio value="allow">
                  放行 —— 黑名单模式：只拦截命中的规则，其余命令一律放行
                </Radio>
                <Radio value="deny">
                  拦截 —— 白名单模式：只放行命中的规则，其余命令一律拦截
                </Radio>
              </Space>
            </Radio.Group>
          </Form.Item>
          {!editingPolicy && (
            <Form.List name="initialRules">
              {(fields, { add, remove }) => (
                <Space
                  orientation="vertical"
                  size={8}
                  style={{ width: '100%' }}
                >
                  <Space size={8}>
                    <Text strong>初始规则（可选）</Text>
                    <Button
                      size="small"
                      icon={<PlusOutlined />}
                      onClick={() =>
                        add({ priority: DEFAULT_RULE_PRIORITY, action: 'deny' })
                      }
                    >
                      添加一条
                    </Button>
                  </Space>
                  {fields.length === 0 && (
                    <Text type="secondary">
                      不填也可以，创建后在「规则管理」里慢慢加。
                    </Text>
                  )}
                  {fields.map((field) => (
                    <Space
                      key={field.key}
                      align="baseline"
                      size={8}
                      style={{ width: '100%' }}
                    >
                      <Form.Item name={[field.name, 'priority']} noStyle>
                        <InputNumber min={1} max={9999} style={{ width: 90 }} />
                      </Form.Item>
                      <Form.Item name={[field.name, 'matchType']} noStyle>
                        <Select
                          style={{ width: 130 }}
                          options={MATCH_TYPE_OPTIONS}
                          placeholder="匹配方式"
                        />
                      </Form.Item>
                      <Form.Item
                        name={[field.name, 'pattern']}
                        noStyle
                        rules={[{ required: true, message: '请输入模式' }]}
                      >
                        <Input
                          style={{ width: 240 }}
                          placeholder="模式，如 ^\\s*rm\\s+-rf"
                        />
                      </Form.Item>
                      <Form.Item name={[field.name, 'action']} noStyle>
                        <Select
                          style={{ width: 100 }}
                          options={POLICY_ACTION_OPTIONS}
                          placeholder="动作"
                        />
                      </Form.Item>
                      <Button
                        type="text"
                        danger
                        icon={<MinusCircleOutlined />}
                        onClick={() => remove(field.name)}
                      />
                    </Space>
                  ))}
                </Space>
              )}
            </Form.List>
          )}
        </Form>
      </Modal>

      <Modal
        title={editingRule ? `编辑规则 #${editingRule.id}` : '新增规则'}
        open={ruleOpen}
        width={680}
        onCancel={() => setRuleOpen(false)}
        onOk={() => ruleForm.submit()}
        confirmLoading={submitting}
        destroyOnHidden
        okText="保存"
        cancelText="取消"
      >
        <Form
          form={ruleForm}
          layout="vertical"
          initialValues={INITIAL_RULE_VALUES}
          onFinish={(values) => void submitRule(values)}
        >
          <Form.Item
            name="priority"
            label="优先级"
            extra="数字越小越先命中，命中即结束；建议高危规则给 10～50"
            rules={[{ required: true, message: '请输入优先级' }]}
          >
            <InputNumber min={1} max={9999} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="matchType"
            label="匹配方式"
            rules={[{ required: true, message: '请选择匹配方式' }]}
          >
            <Select options={MATCH_TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="pattern"
            label="模式"
            rules={[{ required: true, message: '请输入模式' }]}
          >
            <Input.TextArea rows={2} placeholder="例如：^\\s*rm\\s+-rf\\s+/" />
          </Form.Item>
          <Form.Item
            noStyle
            shouldUpdate={(prev, next) => prev.matchType !== next.matchType}
          >
            {({ getFieldValue }) => {
              const matchType = getFieldValue('matchType') as
                | string
                | undefined;
              return (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  title={MATCH_EXAMPLES[matchType ?? 'regex']}
                />
              );
            }}
          </Form.Item>
          <Form.Item
            name="action"
            label="动作"
            rules={[{ required: true, message: '请选择动作' }]}
          >
            <Select options={POLICY_ACTION_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="riskLevel"
            label="风险等级"
            rules={[{ required: true, message: '请选择风险等级' }]}
          >
            <Select options={RISK_OPTIONS} />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={2} maxLength={200} showCount />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={`规则管理 · ${currentPolicy?.name ?? ''}`}
        width={1000}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        destroyOnHidden
        extra={
          <Space size={8}>
            <Text type="secondary">默认动作</Text>
            <ActionTag action={currentPolicy?.defaultAction} />
          </Space>
        }
      >
        <Space orientation="vertical" size={16} style={{ width: '100%' }}>
          <ProTable<PolicyRuleItem>
            rowKey="id"
            key={currentPolicy?.id ?? 0}
            actionRef={rulesActionRef}
            headerTitle="规则列表"
            search={{ labelWidth: 'auto' }}
            pagination={{ defaultPageSize: 10 }}
            scroll={{ x: 'max-content' }}
            params={{ policyId: currentPolicy?.id }}
            request={async (params) => {
              const res = await ruleApi.listByPolicy(currentPolicy?.id, params);
              return { data: res.data, total: res.total, success: res.success };
            }}
            toolBarRender={() =>
              canManage
                ? [
                    <Button
                      key="create"
                      type="primary"
                      icon={<PlusOutlined />}
                      onClick={openCreateRule}
                    >
                      新增规则
                    </Button>,
                  ]
                : []
            }
            columns={ruleColumns}
          />
          <Card size="small" title="规则数（本策略）">
            <Text type="secondary">
              共 {drawerRules.length} 条规则，其中启用{' '}
              {drawerRules.filter((rule) => rule.enabled).length} 条。
            </Text>
          </Card>
        </Space>
      </Drawer>
    </PageContainer>
  );
};

export default PoliciesPage;
