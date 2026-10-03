import {
  LockOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@ant-design/icons';
import {
  LoginForm,
  ProFormCheckbox,
  ProFormText,
} from '@ant-design/pro-components';
import { Helmet, SelectLang, useModel } from '@umijs/max';
import { Alert, App } from 'antd';
import { createStyles } from 'antd-style';
import React, { startTransition, useState } from 'react';
import { Footer } from '@/components';
import { setToken } from '@/services/bastion/client';
import { authApi } from '@/services/bastion/endpoints';
import Settings from '../../../../config/defaultSettings';

type LoginValues = {
  username: string;
  password: string;
  autoLogin?: boolean;
};

/**
 * Validate redirect URL to prevent open redirect attacks.
 * Only allow same-origin relative paths starting with '/'.
 */
const getSafeRedirectUrl = (redirect: string | null): string => {
  if (!redirect?.startsWith('/')) return '/';
  if (redirect.startsWith('//')) return '/';
  try {
    const parsed = new URL(redirect, window.location.origin);
    if (parsed.origin !== window.location.origin) return '/';
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return '/';
  }
};

const useStyles = createStyles(({ token }) => {
  return {
    lang: {
      width: 42,
      height: 42,
      lineHeight: '42px',
      position: 'fixed',
      right: 16,
      borderRadius: token.borderRadius,
      ':hover': {
        backgroundColor: token.colorBgTextHover,
      },
    },
    container: {
      display: 'flex',
      flexDirection: 'column',
      height: '100vh',
      overflow: 'auto',
      // 用同色系本地渐变替代模板的外链背景图（内网/离线部署取不到 alipayobjects.com）
      backgroundImage: `linear-gradient(135deg, ${token.colorPrimaryBg} 0%, ${token.colorBgLayout} 100%)`,
    },
  };
});

const Lang = () => {
  const { styles } = useStyles();
  return (
    <div className={styles.lang} data-lang>
      {SelectLang && <SelectLang />}
    </div>
  );
};

const Login: React.FC = () => {
  const [errorMessage, setErrorMessage] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const { initialState, setInitialState } = useModel('@@initialState');
  const { styles } = useStyles();
  const { message } = App.useApp();

  const fetchUserInfo = async () => {
    const userInfo = await initialState?.fetchUserInfo?.();
    if (userInfo) {
      startTransition(() => {
        setInitialState((s) => ({ ...s, currentUser: userInfo }));
      });
    }
    return userInfo;
  };

  const handleSubmit = async (values: LoginValues) => {
    setErrorMessage('');
    setLoading(true);
    try {
      const body = await authApi.proLogin({
        username: values.username?.trim(),
        password: values.password,
        type: 'account',
      });
      const data = body?.data;
      if (!data?.token) {
        setErrorMessage(body?.message || '登录失败：后端未返回令牌');
        return;
      }
      // 先把令牌落到 localStorage，后续请求拦截器才能带上 Authorization
      setToken(data.token);
      message.success(`欢迎回来，${data.user?.displayName || values.username}`);
      const userInfo = await fetchUserInfo();
      const urlParams = new URL(window.location.href).searchParams;
      const mustChange = (
        userInfo as API.CurrentUser & { mustChangePassword?: boolean }
      )?.mustChangePassword;
      // 首次登录/被管理员重置后必须先改密码，否则不允许继续操作
      const redirectUrl = mustChange
        ? '/account/settings?tab=security'
        : getSafeRedirectUrl(urlParams.get('redirect'));
      window.location.href = redirectUrl;
    } catch (error) {
      const text =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message ||
        (error as Error)?.message ||
        '登录失败，请检查用户名与密码';
      setErrorMessage(text);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={styles.container}>
      <Helmet>
        <title>{`登录 - ${Settings.title}`}</title>
      </Helmet>
      <Lang />
      <div
        style={{
          flex: '1',
          padding: '32px 0',
        }}
      >
        <LoginForm<LoginValues>
          contentStyle={{
            minWidth: 280,
            maxWidth: '75vw',
          }}
          logo={<img alt="logo" src="/logo.svg" />}
          title="AutoOps 堡垒机"
          subTitle="统一运维入口 · 全程操作审计 · 命令级策略管控"
          initialValues={{
            autoLogin: true,
          }}
          submitter={{
            searchConfig: { submitText: '登 录' },
            submitButtonProps: { loading, size: 'large', block: true },
          }}
          onFinish={async (values) => {
            await handleSubmit(values as LoginValues);
          }}
        >
          {errorMessage ? (
            <Alert
              style={{ marginBottom: 24 }}
              title={errorMessage}
              type="error"
              showIcon
            />
          ) : null}
          {process.env.NODE_ENV === 'development' ? (
            <Alert
              style={{ marginBottom: 24 }}
              type="info"
              showIcon
              icon={<SafetyCertificateOutlined />}
              title="首次部署默认管理员：admin / admin123（首次登录后请立即修改密码）"
            />
          ) : null}
          <ProFormText
            name="username"
            fieldProps={{
              size: 'large',
              prefix: <UserOutlined />,
              autoComplete: 'username',
            }}
            placeholder="堡垒机账号"
            rules={[{ required: true, message: '请输入堡垒机账号！' }]}
          />
          <ProFormText.Password
            name="password"
            fieldProps={{
              size: 'large',
              prefix: <LockOutlined />,
              autoComplete: 'current-password',
            }}
            placeholder="登录密码"
            rules={[{ required: true, message: '请输入登录密码！' }]}
          />
          <div
            style={{
              marginBottom: 24,
            }}
          >
            <ProFormCheckbox noStyle name="autoLogin">
              保持登录状态
            </ProFormCheckbox>
            <span
              style={{
                float: 'right',
                color: 'rgba(0,0,0,0.45)',
              }}
            >
              忘记密码请联系管理员重置
            </span>
          </div>
        </LoginForm>
      </div>
      <Footer />
    </div>
  );
};

export default Login;
