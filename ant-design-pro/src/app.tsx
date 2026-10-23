/**
 * Umi 运行时配置（应用外壳）。
 *
 * 三件事：① `getInitialState()` 拉当前登录用户与权限，供 `useAccess()` 使用，
 * 未登录时把请求引到登录页；② `layout` 配置 ProLayout（品牌名、菜单、右侧头像下拉、
 * `footerRender: false` —— 页面底部不显示版权与标语）；③ `request` 统一处理后端信封
 * （`{success,message,data}`）的错误提示与 401 跳登录。
 */
import type {
  Settings as LayoutSettings,
  MenuDataItem,
} from '@ant-design/pro-components';
import { SettingDrawer } from '@ant-design/pro-components';
import type { RequestConfig, RunTimeLayoutConfig } from '@umijs/max';
import { history, Link } from '@umijs/max';
import { Button, Result } from 'antd';
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import React from 'react';

// Initialize dayjs plugins globally
dayjs.extend(relativeTime);

import {
  AvatarDropdown,
  ErrorBoundary,
  LangDropdown,
  OfflineBanner,
} from '@/components';
import { t, translateMenuData } from '@/i18n';
import { resolveModuleIcon } from '@/modules/icons';
import { currentUser as queryCurrentUser } from '@/services/ant-design-pro/api';
import { moduleApi, moduleText } from '@/services/bastion/modules';
import type { ModuleNavEntry } from '@/services/bastion/types';
import defaultSettings from '../config/defaultSettings';
import { errorConfig } from './requestErrorConfig';

const loginPath = '/user/login';

/**
 * 拉模块导航。未登录 / 无权限 / 后端模块服务不可用时静默返回空数组：
 * 扩展模块只是附加能力，绝不能因为它挂了导致首页打不开。
 */
const loadModuleNav = async (): Promise<ModuleNavEntry[]> => {
  try {
    return await moduleApi.nav();
  } catch (_error) {
    return [];
  }
};

/** 模块 nav 项 → ProLayout 菜单项（插在「系统设置」之前）。 */
const buildModuleMenuData = (nav: ModuleNavEntry[]): MenuDataItem[] => {
  const seen = new Set<string>();
  const menus: MenuDataItem[] = [];
  for (const item of nav) {
    if (!item.path || seen.has(item.path)) {
      continue;
    }
    seen.add(item.path);
    menus.push({
      path: item.path,
      name: moduleText(item.title ?? item.moduleName, item.key),
      icon: resolveModuleIcon(item.icon),
    });
  }
  return menus;
};

/**
 * @see https://umijs.org/docs/api/runtime-config#getinitialstate
 * */
export async function getInitialState(): Promise<{
  settings?: Partial<LayoutSettings>;
  currentUser?: API.CurrentUser;
  loading?: boolean;
  fetchUserInfo?: () => Promise<API.CurrentUser | undefined>;
  settingDrawerOpen?: boolean;
  /** 当前账号可见的扩展模块导航（登录后才拉） */
  moduleNav?: ModuleNavEntry[];
  /** 与 `moduleNav` 同值：契约 §7 里叫 modules，页面按这个字段消费 */
  modules?: ModuleNavEntry[];
}> {
  const fetchUserInfo = async () => {
    try {
      const msg = await queryCurrentUser({
        skipErrorHandler: true,
      });
      return msg.data;
    } catch (_error) {
      const { pathname, search, hash } = history.location;
      history.replace(
        `${loginPath}?redirect=${encodeURIComponent(pathname + search + hash)}`,
      );
    }
    return undefined;
  };
  // 如果不是登录页面，执行
  const { location } = history;
  if (
    ![loginPath, '/user/register', '/user/register-result'].includes(
      location.pathname,
    )
  ) {
    const currentUser = await fetchUserInfo();
    const moduleNav = currentUser ? await loadModuleNav() : [];
    return {
      fetchUserInfo,
      currentUser,
      modules: moduleNav,
      moduleNav,
      settings: defaultSettings as Partial<LayoutSettings>,
      settingDrawerOpen: false,
    };
  }
  return {
    fetchUserInfo,
    settings: defaultSettings as Partial<LayoutSettings>,
    settingDrawerOpen: false,
  };
}

// ProLayout 支持的api https://procomponents.ant.design/components/layout
export const layout: RunTimeLayoutConfig = ({
  initialState,
  setInitialState,
}) => {
  return {
    // 菜单/面包屑的中文名在路由里是字面量（见 config/routes.ts 的说明），
    // 这里统一过一遍 t()，切到英文时整棵菜单一起变。
    // 扩展模块菜单按契约 §7 插在「系统设置」分组之前。
    menuDataRender: (menuData) => {
      const moduleMenus = buildModuleMenuData(initialState?.moduleNav ?? []);
      if (moduleMenus.length === 0) {
        return translateMenuData(menuData);
      }
      const merged = [...menuData];
      const systemIndex = merged.findIndex((item) => item.path === '/system');
      merged.splice(
        systemIndex < 0 ? merged.length : systemIndex,
        0,
        ...moduleMenus,
      );
      return translateMenuData(merged);
    },
    menuItemRender: (item, dom) => {
      if (item.path) {
        return (
          <Link to={item.path} prefetch>
            {dom}
          </Link>
        );
      }
      return dom;
    },
    actionsRender: () => {
      // `locale: false` opts out of the language switcher. ProLayout's own
      // `locale` prop is a locale string, so narrow to the boolean toggle here.
      const localeEnabled =
        (initialState?.settings as { locale?: boolean })?.locale !== false;
      return [localeEnabled && <LangDropdown key="lang" />].filter(Boolean);
    },
    avatarProps: {
      src: initialState?.currentUser?.avatar || undefined,
      title: initialState?.currentUser?.name || t('未登录'),
      render: (_, avatarChildren) => (
        <AvatarDropdown>{avatarChildren}</AvatarDropdown>
      ),
    },
    // 审计水印：所有页面叠加当前登录账号，防止截图外泄后无法溯源
    waterMarkProps: {
      content: initialState?.currentUser?.name,
      fontColor: 'rgba(0,0,0,0.06)',
    },
    // 用户要求：页面底部不再显示版权与标语（原先的 <Footer /> 已整体移除）
    footerRender: false,
    onPageChange: () => {
      const { location } = history;
      // 如果没有登录，重定向到 login
      if (!initialState?.currentUser && location.pathname !== loginPath) {
        history.replace(
          `${loginPath}?redirect=${encodeURIComponent(location.pathname + location.search + location.hash)}`,
        );
      }
    },
    // 生产环境与开发环境都走同源（开发由 config/proxy.ts 转发到 Flask，
    // 生产由 Flask / Nginx 同源提供静态资源 + /api）。
    links: [],
    // Replace ProLayout's default ErrorBoundary with our offline-aware version,
    // so chunk load errors show friendly messages instead of "Something went wrong."
    ErrorBoundary,
    menuHeaderRender: undefined,
    // 无权限访问路由时展示的页面（配合 config/routes.ts 里的 access 字段）
    unAccessible: (
      <Result
        status="403"
        title="403"
        subTitle={t('抱歉，当前账号没有访问该页面的权限，请联系管理员开通。')}
        extra={
          <Button type="primary" onClick={() => history.push('/')}>
            {t('返回首页')}
          </Button>
        }
      />
    ),
    // 增加一个 loading 的状态
    childrenRender: (children) => {
      // if (initialState?.loading) return <PageLoading />;
      return (
        <>
          {children}
          <SettingDrawer
            disableUrlParams
            enableDarkTheme
            collapse={initialState?.settingDrawerOpen}
            onCollapseChange={(open) => {
              setInitialState((s) => ({
                ...s,
                settingDrawerOpen: open,
              }));
            }}
            settings={initialState?.settings}
            onSettingChange={(settings) => {
              setInitialState((s) => ({
                ...s,
                settings,
              }));
            }}
          />
        </>
      );
    },
    ...initialState?.settings,
    // 品牌名跟着语言走（Chinese: AutoOps 堡垒机 / English: AutoOps Bastion）。
    // 必须放在 spread 之后，否则会被 initialState.settings 里的原值盖回去。
    title: t(String(initialState?.settings?.title ?? 'AutoOps 堡垒机')),
  };
};

/**
 * @name request 配置，可以配置错误处理
 * 它基于 axios 提供了一套统一的网络请求和错误处理方案。
 * @doc https://umijs.org/docs/max/request#配置
 */
export const request: RequestConfig = {
  // 同源：开发环境由 umi 代理转发到 Flask，生产环境由 Flask/Nginx 同源提供
  baseURL: '',
  ...errorConfig,
};

export function rootContainer(container: React.ReactNode) {
  return (
    <>
      <OfflineBanner />
      <ErrorBoundary>{container}</ErrorBoundary>
    </>
  );
}
