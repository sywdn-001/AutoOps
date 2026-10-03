/**
 * 参数设置（系统设置 → 参数设置）。
 *
 * 后端 `/api/settings` 下发「设置项定义 + 当前值」，前端**按 items 动态渲染**控件：
 *   bool → Switch（值 1/true 为开）、int → InputNumber、其余 → Input。
 * 保存时把整份表单回写成 `Record<string, string>` 交给 `settingApi.update`。
 * 附带两项维护操作：残留在线会话清理（reconcile）与内置数据重建（seed）。
 */
import { ReloadOutlined, SaveOutlined, UndoOutlined } from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  App,
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Row,
  Space,
  Spin,
  Switch,
  Tooltip,
  Typography,
} from 'antd';
import React, { useCallback, useEffect, useState } from 'react';
import { settingApi } from '@/services/bastion/endpoints';
import type { SettingItem, SettingsPayload } from '@/services/bastion/types';

const { Text, Paragraph } = Typography;

/** 表单控件的取值类型（Switch 是 boolean，InputNumber 是 number，Input 是 string） */
type FormValue = string | number | boolean | undefined;

/** 后端字符串值 → 表单控件值 */
const toFormValue = (item: SettingItem, raw: string): FormValue => {
  if (item.type === 'bool') return raw === '1' || raw === 'true';
  if (item.type === 'int') {
    const parsed = Number.parseInt(raw, 10);
    return Number.isNaN(parsed) ? undefined : parsed;
  }
  return raw;
};

/** 表单控件值 → 后端字符串值（bool 统一回写为 '1' / '0'） */
const toPayloadValue = (item: SettingItem, raw: FormValue): string => {
  if (item.type === 'bool') return raw ? '1' : '0';
  if (raw === undefined || raw === null) return '';
  return String(raw);
};

/** 后端错误 → 可读提示（统一信封里的 message） */
const backendMessage = (error: unknown, fallback: string): string =>
  (error as { response?: { data?: { message?: string } } })?.response?.data
    ?.message || fallback;

const Settings: React.FC = () => {
  const [form] = Form.useForm();
  const access = useAccess();
  const { message, modal } = App.useApp();
  const [payload, setPayload] = useState<SettingsPayload>();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [seeding, setSeeding] = useState(false);
  const canManage = Boolean(access.canSettingManage);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await settingApi.get();
      setPayload(data);
      const initial: Record<string, FormValue> = {};
      for (const item of data?.items ?? []) {
        const raw =
          data?.values?.[item.key] ?? item.value ?? item.default ?? '';
        initial[item.key] = toFormValue(item, raw);
      }
      form.setFieldsValue(initial);
    } catch (error) {
      message.error(backendMessage(error, '设置加载失败，请稍后重试'));
    } finally {
      setLoading(false);
    }
  }, [form, message]);

  useEffect(() => {
    load();
  }, [load]);

  const items = payload?.items ?? [];

  const handleSave = async () => {
    try {
      const fields = (await form.validateFields()) as Record<string, FormValue>;
      const values: Record<string, string> = {};
      for (const item of items) {
        values[item.key] = toPayloadValue(item, fields[item.key]);
      }
      setSaving(true);
      await settingApi.update(values);
      message.success('设置已保存并立即生效');
      await load();
    } catch (error) {
      // 校验失败时 antd 已在字段上标红，这里只提示接口错误
      if ((error as { errorFields?: unknown })?.errorFields) return;
      message.error(backendMessage(error, '保存失败，请稍后重试'));
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    setResetting(true);
    try {
      await settingApi.reset();
      message.success('已恢复内置默认设置');
      await load();
    } catch (error) {
      message.error(backendMessage(error, '恢复默认失败，请稍后重试'));
    } finally {
      setResetting(false);
    }
  };

  const handleReconcile = async () => {
    setReconciling(true);
    try {
      const result = await settingApi.reconcile();
      modal.info({
        title: '残留会话清理完成',
        content: `已把 ${result?.reconciled ?? 0} 条仍标记为「在线」的会话记录更新为「已中断」。`,
      });
    } catch (error) {
      message.error(backendMessage(error, '清理失败，请稍后重试'));
    } finally {
      setReconciling(false);
    }
  };

  const handleSeed = async () => {
    setSeeding(true);
    try {
      const result = await settingApi.seed();
      const detail = Object.entries(result ?? {})
        .map(([key, value]) => `${key}：${String(value)}`)
        .join('，');
      modal.success({
        title: '内置数据已重建',
        content: detail || '内置角色、命令策略与参数设置已重新写入。',
      });
    } catch (error) {
      message.error(backendMessage(error, '重建失败，请稍后重试'));
    } finally {
      setSeeding(false);
    }
  };

  const renderControl = (item: SettingItem) => {
    if (item.type === 'bool') return <Switch />;
    if (item.type === 'int') {
      return <InputNumber style={{ width: '100%' }} step={1} precision={0} />;
    }
    return <Input allowClear />;
  };

  const manageTip = canManage
    ? undefined
    : '当前账号没有「设置管理」权限，仅可查看';

  return (
    <PageContainer
      title="参数设置"
      subTitle="命令超时、单用户最大会话数、登录失败锁定阈值等全局参数"
      extra={
        <Space wrap>
          <Tooltip title={manageTip}>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              disabled={!canManage}
              loading={saving}
              onClick={handleSave}
            >
              保存
            </Button>
          </Tooltip>
          {canManage ? (
            <Popconfirm
              title="恢复默认设置"
              description="所有参数将回到内置默认值，当前修改会丢失，确定继续？"
              okText="恢复默认"
              cancelText="取消"
              onConfirm={handleReset}
            >
              <Button icon={<UndoOutlined />} loading={resetting}>
                恢复默认
              </Button>
            </Popconfirm>
          ) : (
            <Tooltip title={manageTip}>
              <Button icon={<UndoOutlined />} disabled>
                恢复默认
              </Button>
            </Tooltip>
          )}
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
            刷新
          </Button>
        </Space>
      }
    >
      <Card title="全局参数" style={{ marginBottom: 16 }}>
        <Spin spinning={loading && items.length === 0}>
          {items.length ? (
            <Form form={form} layout="vertical">
              <Row gutter={16}>
                {items.map((item) => (
                  <Col key={item.key} xs={24} lg={12}>
                    <Form.Item
                      name={item.key}
                      label={item.label || item.key}
                      valuePropName={item.type === 'bool' ? 'checked' : 'value'}
                      extra={
                        <Space size={12} wrap>
                          <Text type="secondary">
                            配置项 <Text code>{item.key}</Text>
                          </Text>
                          <Text type="secondary">
                            默认：
                            {item.default === '' ? '（空）' : item.default}
                          </Text>
                        </Space>
                      }
                    >
                      {renderControl(item)}
                    </Form.Item>
                  </Col>
                ))}
              </Row>
            </Form>
          ) : (
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              {loading
                ? '正在加载设置项…'
                : '后端未下发任何设置项，请点击「刷新」重试。'}
            </Paragraph>
          )}
        </Spin>
      </Card>

      <Card title="维护操作">
        <Space orientation="vertical" size={16} style={{ width: '100%' }}>
          <Space align="start" size={12} wrap>
            <Tooltip title={manageTip}>
              <Button
                disabled={!canManage}
                loading={reconciling}
                onClick={handleReconcile}
              >
                清理残留在线会话
              </Button>
            </Tooltip>
            <Text type="secondary" style={{ maxWidth: 640 }}>
              服务异常退出后可能残留仍为「在线」状态的会话记录，这里把它们标记为已中断，避免审计中心长期显示假在线。
            </Text>
          </Space>
          <Space align="start" size={12} wrap>
            {canManage ? (
              <Popconfirm
                title="重建内置数据"
                description="将按内置模板重新写入角色、命令策略与参数设置，确定继续？"
                okText="重建"
                cancelText="取消"
                onConfirm={handleSeed}
              >
                <Button loading={seeding}>重建内置数据</Button>
              </Popconfirm>
            ) : (
              <Tooltip title={manageTip}>
                <Button disabled>重建内置数据</Button>
              </Tooltip>
            )}
            <Text type="secondary" style={{ maxWidth: 640 }}>
              按内置模板重新写入内置角色、命令策略与默认参数，用于初始化或修复被误删的内置数据。
            </Text>
          </Space>
        </Space>
      </Card>
    </PageContainer>
  );
};

export default Settings;
