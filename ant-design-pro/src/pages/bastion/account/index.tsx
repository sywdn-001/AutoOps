/**
 * 个人设置（模板 /account/* 的堡垒机替代实现）
 *
 * 替换原因：模板自带的 `/account/center`（假文章/应用/项目计数）与 `/account/settings`
 * 会调用后端不存在的 Pro 接口，页面会弹「接口不存在」。
 * 这里改为堡垒机自己的三项内容：身份信息、改密码（对应需求①的身份审计）、最近操作。
 *
 * 数据来源：`authApi.me()`（GET /api/auth/me，返回 UserItem + isAdmin + recentActivities）。
 * 注意不能只用 `initialState.currentUser`——那是 Pro 兼容的 /api/currentUser 形状，
 * 没有 displayName/roleName/lastLoginAt/lastLoginIp 等业务字段。
 */

import { PageContainer } from '@ant-design/pro-components';
import { history, useSearchParams } from '@umijs/max';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  Row,
  Skeleton,
  Space,
  Tabs,
  Tag,
  Timeline,
  Typography,
} from 'antd';
import React, { useCallback, useEffect, useState } from 'react';
import { t } from '@/i18n';
import { authApi } from '@/services/bastion/endpoints';
import type { AuditItem, UserItem } from '@/services/bastion/types';

const { Text, Paragraph } = Typography;

type MeInfo = UserItem & { isAdmin?: boolean };

const ACTION_LABEL: Record<string, string> = {
  login: t('登录'),
  login_failed: t('登录失败'),
  login_locked: t('账号被锁定'),
  logout: t('退出登录'),
  gateway_login: t('SSH 网关登录'),
  change_password: t('修改密码'),
  reset_password: t('重置密码'),
  unlock: t('解锁账号'),
  session_open: t('打开会话'),
  session_close: t('关闭会话'),
  session_terminate: t('中断会话'),
  connect_failed: t('连接目标机失败'),
  create: t('新增'),
  update: t('修改'),
  delete: t('删除'),
};

/** 基本资料：展示 /api/auth/me 返回的身份与权限快照 */
const ProfilePane: React.FC<{ user: MeInfo }> = ({ user }) => {
  const permissions = user.permissions ?? [];
  const isAll = permissions.includes('*');
  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      {user.mustChangePassword ? (
        <Alert
          type="warning"
          showIcon
          title={t('当前账号处于「首次登录必须改密码」状态')}
          description={t(
            '请切换到「安全设置」修改密码；SSH 网关登录时也会强制要求修改。',
          )}
        />
      ) : null}
      <Descriptions
        column={{ xs: 1, sm: 2 }}
        bordered
        size="middle"
        items={[
          {
            key: 'username',
            label: t('登录账号'),
            children: user.username || '-',
          },
          {
            key: 'displayName',
            label: t('姓名'),
            children: user.displayName || '-',
          },
          {
            key: 'role',
            label: t('角色'),
            children: (
              <Space size={6}>
                <Tag color="blue">{user.roleName || '-'}</Tag>
                <Text type="secondary" code>
                  {user.roleCode || '-'}
                </Text>
              </Space>
            ),
          },
          {
            key: 'isAdmin',
            label: t('管理员'),
            children:
              user.isAdmin || user.isSuperuser ? (
                <Tag color="red">{t('是')}</Tag>
              ) : (
                <Tag>{t('否')}</Tag>
              ),
          },
          { key: 'email', label: t('邮箱'), children: user.email || '-' },
          { key: 'phone', label: t('手机'), children: user.phone || '-' },
          {
            key: 'status',
            label: t('账号状态'),
            children:
              user.status === 'active' ? (
                <Tag color="green">{t('正常')}</Tag>
              ) : (
                <Tag color="red">{t('已停用')}</Tag>
              ),
          },
          {
            key: 'gateway',
            label: t('SSH 网关'),
            children: user.gatewayEnabled ? (
              <Tag color="green">{t('已开放')}</Tag>
            ) : (
              <Tag>{t('未开放')}</Tag>
            ),
          },
          {
            key: 'webterm',
            label: t('网页终端'),
            children: user.webtermEnabled ? (
              <Tag color="green">{t('已开放')}</Tag>
            ) : (
              <Tag>{t('未开放')}</Tag>
            ),
          },
          {
            key: 'lastLoginAt',
            label: t('最近登录时间'),
            children: user.lastLoginAt
              ? String(user.lastLoginAt).replace('T', ' ').slice(0, 19)
              : '-',
          },
          {
            key: 'lastLoginIp',
            label: t('最近登录 IP'),
            children: user.lastLoginIp || '-',
          },
        ]}
      />
      <Card
        size="small"
        title={t('权限清单（决定你能碰哪些机器、能做什么操作）')}
      >
        {isAll ? (
          <Tag color="red">{t('超级管理员：全部权限（*）')}</Tag>
        ) : permissions.length === 0 ? (
          <Text type="secondary">{t('暂无权限，请联系管理员分配角色')}</Text>
        ) : (
          <Space size={[8, 8]} wrap>
            {permissions.map((code) => (
              <Tag key={code} color="geekblue">
                {code}
              </Tag>
            ))}
          </Space>
        )}
      </Card>
    </Space>
  );
};

/** 安全设置：修改自己的密码（后端 POST /api/auth/password，成功后必须用新密码重新登录） */
const SecurityPane: React.FC = () => {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [submitting, setSubmitting] = useState(false);

  const submit = useCallback(
    async (values: {
      oldPassword: string;
      newPassword: string;
      confirmPassword: string;
    }) => {
      if (values.newPassword !== values.confirmPassword) {
        message.error(t('两次输入的新密码不一致'));
        return;
      }
      setSubmitting(true);
      try {
        await authApi.changePassword({
          oldPassword: values.oldPassword,
          newPassword: values.newPassword,
        });
        message.success(t('密码已修改，请使用新密码重新登录'));
        form.resetFields();
      } catch (error) {
        // 全局 errorHandler 已弹过后端 message（弱密码 WEAK_PASSWORD / 原密码错误 INVALID_PASSWORD），
        // 这里只兜底非 HTTP 异常，避免双弹。
        if (!(error as { response?: unknown })?.response) {
          message.error(t('修改密码失败，请稍后重试'));
        }
      } finally {
        setSubmitting(false);
      }
    },
    [form, message],
  );

  return (
    <Row gutter={24}>
      <Col xs={24} md={12}>
        <Form
          form={form}
          layout="vertical"
          onFinish={submit}
          requiredMark={false}
        >
          <Form.Item
            name="oldPassword"
            label={t('当前密码')}
            rules={[{ required: true, message: t('请输入当前密码') }]}
          >
            <Input.Password
              autoComplete="current-password"
              placeholder={t('当前登录密码')}
            />
          </Form.Item>
          <Form.Item
            name="newPassword"
            label={t('新密码')}
            rules={[
              { required: true, message: t('请输入新密码') },
              { min: 8, message: t('长度不得少于 8 位') },
            ]}
          >
            <Input.Password
              autoComplete="new-password"
              placeholder={t('至少 8 位，需包含字母/数字等两类字符')}
            />
          </Form.Item>
          <Form.Item
            name="confirmPassword"
            label={t('确认新密码')}
            dependencies={['newPassword']}
            rules={[{ required: true, message: t('请再次输入新密码') }]}
          >
            <Input.Password
              autoComplete="new-password"
              placeholder={t('再次输入新密码')}
            />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={submitting}>
                {t('修改密码')}
              </Button>
              <Button
                onClick={() => {
                  form.resetFields();
                }}
              >
                {t('重置表单')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Col>
      <Col xs={24} md={12}>
        <Alert
          type="info"
          showIcon
          title={t('密码规则')}
          description={
            <Space orientation="vertical" size={4}>
              <Text>
                {t(
                  '· 长度至少 8 位，且至少包含两类字符（大写 / 小写 / 数字 / 符号）',
                )}
              </Text>
              <Text>
                {t(
                  '· 修改成功后旧密码立即失效，SSH 网关与网页终端都改用新密码',
                )}
              </Text>
              <Text>
                {t('· 连续多次登录失败会临时锁定账号，可联系管理员解锁')}
              </Text>
            </Space>
          }
        />
      </Col>
    </Row>
  );
};

/** 最近操作：数据来自 /api/auth/me 的 recentActivities（最近 10 条审计记录） */
const ActivityPane: React.FC<{ activities: AuditItem[]; loading: boolean }> = ({
  activities,
  loading,
}) => {
  if (!loading && activities.length === 0) {
    return <Text type="secondary">{t('暂无操作记录')}</Text>;
  }
  return (
    <Timeline
      pending={loading ? t('加载中…') : undefined}
      items={activities.map((item) => ({
        key: item.id,
        color:
          item.result === 'failure'
            ? 'red'
            : item.result === 'denied'
              ? 'orange'
              : 'green',
        children: (
          <Space orientation="vertical" size={2}>
            <Text strong>
              {ACTION_LABEL[item.action] ?? item.action}
              {item.message ? `：${item.message}` : ''}
            </Text>
            <Text type="secondary">
              {item.category} · {item.result}
              {item.targetName ? t(' · 目标 {0}', { 0: item.targetName }) : ''}
              {item.ip ? ` · ${item.ip}` : ''}
            </Text>
            <Text type="secondary">
              {item.ts ? String(item.ts).replace('T', ' ').slice(0, 19) : ''}
            </Text>
          </Space>
        ),
      }))}
    />
  );
};

const AccountSettings: React.FC = () => {
  const [me, setMe] = useState<MeInfo | null>(null);
  const [activities, setActivities] = useState<AuditItem[]>([]);
  const [loading, setLoading] = useState(true);

  const [searchParams] = useSearchParams();
  const tab = searchParams.get('tab') ?? 'profile';

  useEffect(() => {
    let alive = true;
    setLoading(true);
    authApi
      .me()
      .then((data) => {
        if (alive) {
          setMe(data);
          setActivities(data.recentActivities ?? []);
        }
      })
      .catch(() => {
        // 全局 errorHandler 已提示；不阻断页面渲染
      })
      .finally(() => {
        if (alive) {
          setLoading(false);
        }
      });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <PageContainer
      header={{ title: t('个人设置'), breadcrumb: {} }}
      content={
        <Paragraph type="secondary">
          {t('查看自己的身份与权限，并维护登录密码。')}
        </Paragraph>
      }
    >
      <Card>
        <Tabs
          activeKey={tab}
          onChange={(key) => {
            history.replace(`/account/settings?tab=${key}`);
          }}
          items={[
            {
              key: 'profile',
              label: t('基本资料'),
              children:
                loading || !me ? (
                  <Skeleton active paragraph={{ rows: 8 }} />
                ) : (
                  <ProfilePane user={me} />
                ),
            },
            {
              key: 'security',
              label: t('安全设置'),
              children: <SecurityPane />,
            },
            {
              key: 'activity',
              label: t('最近操作'),
              children: (
                <ActivityPane activities={activities} loading={loading} />
              ),
            },
          ]}
        />
      </Card>
    </PageContainer>
  );
};

export default AccountSettings;
