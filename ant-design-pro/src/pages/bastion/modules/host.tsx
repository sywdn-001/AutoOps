/**
 * 扩展模块宿主页：`/m/:moduleId/*`。
 *
 * - 优先用 `moduleApi.detail` 拿完整模块对象（需要 `setting:view`）；
 * - 普通用户没有该权限时会 403，此时回退到 `/api/modules/nav`（已按账号权限过滤，
 *   且带 `entry` / `moduleId` / `moduleName`，足够渲染页面）；
 * - `iframe` 模式直接同源加载 `entry`（模块页里可以读 `localStorage.bastion_token`）；
 * - `js` 模式注入 `<script type="module">`，把 React/antd/request 等运行时挂到
 *   `window.AutoOpsSDK`，脚本加载后调用 `window.AutoOpsModules[moduleId].mount(el, sdk)`。
 */
import { PageContainer } from '@ant-design/pro-components';
import { history, useLocation, useParams } from '@umijs/max';
import { Alert, App, Button, Flex, Result, Space, Spin, Tag } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { t } from '@/i18n';
import { ensureAutoOpsSdk } from '@/modules/sdk';
import {
  matchModuleNav,
  moduleApi,
  moduleAssetUrl,
  moduleText,
} from '@/services/bastion/modules';
import type { ModuleItem, ModuleNavItem } from '@/services/bastion/types';

type HostStatus =
  | 'loading'
  | 'ready'
  | 'missing'
  | 'unavailable'
  | 'disabled'
  | 'no-entry'
  | 'load-failed';

type ResultConfig = {
  status: '403' | '404' | '500';
  title: string;
  subTitle?: string;
};

const errorMessage = (error: unknown, fallback: string): string =>
  (error instanceof Error ? error.message : '') || fallback;

/** 详情 403 后回退 nav；nav 里没有该模块时给出统一的「不可用」结果页。 */
const resolveNavFallback = async (
  moduleId: string,
  pathname: string,
): Promise<{ nav?: ModuleNavItem; status: HostStatus }> => {
  const entries = await moduleApi.nav().catch(() => []);
  const mine = entries.filter((entry) => entry.moduleId === moduleId);
  if (mine.length === 0) {
    return { status: 'unavailable' };
  }
  const matched = matchModuleNav(mine, pathname);
  return matched ? { nav: matched, status: 'ready' } : { status: 'no-entry' };
};

const ModuleHostPage = () => {
  const { moduleId = '' } = useParams<{ moduleId: string }>();
  const location = useLocation();
  const { message } = App.useApp();

  const [status, setStatus] = useState<HostStatus>('loading');
  const [module, setModule] = useState<ModuleItem | null>(null);
  const [nav, setNav] = useState<ModuleNavItem | null>(null);
  const [error, setError] = useState('');
  const [jsReady, setJsReady] = useState(false);

  const jsHostRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    if (!moduleId) {
      setStatus('missing');
      return;
    }
    setStatus('loading');
    setError('');
    setModule(null);
    setNav(null);

    try {
      const detail = await moduleApi.detail(moduleId);
      if (!detail.enabled) {
        setStatus('disabled');
        return;
      }
      if (!detail.loaded) {
        setError(detail.errors.join('\n'));
        setStatus('load-failed');
        return;
      }
      const matched = matchModuleNav(detail.nav, location.pathname);
      if (!matched) {
        setStatus('no-entry');
        return;
      }
      setModule(detail);
      setNav(matched);
      setStatus('ready');
    } catch (_detailError) {
      // 没有 setting:view 的普通账号走这里；nav 是登录即可访问的降级路径。
      const fallback = await resolveNavFallback(moduleId, location.pathname);
      if (fallback.nav) {
        if (fallback.nav.loaded === false) {
          setStatus('load-failed');
          return;
        }
        setNav(fallback.nav);
        setStatus('ready');
        return;
      }
      setStatus(fallback.status);
    }
  }, [location.pathname, moduleId]);

  useEffect(() => {
    void load();
  }, [load]);

  const entry = nav?.entry ?? module?.entry?.web ?? 'web/index.html';
  const assetUrl = moduleAssetUrl(moduleId, entry);
  const moduleTitle = module
    ? moduleText(module.name, moduleId)
    : moduleText(nav?.title ?? nav?.moduleName, moduleId);

  // js 模式：注入模块入口脚本，交给模块自己 mount；组件卸载时卸载并移除脚本。
  useEffect(() => {
    if (status !== 'ready' || nav?.type !== 'js') {
      return;
    }
    const host = jsHostRef.current;
    if (!host) {
      return;
    }

    let disposed = false;
    let mounted = false;
    let script: HTMLScriptElement | null = null;
    let cleanup: (() => void) | undefined;

    setJsReady(false);

    const boot = async () => {
      try {
        const sdk = await ensureAutoOpsSdk(message);
        if (disposed) {
          return;
        }
        script = document.createElement('script');
        script.type = 'module';
        script.src = assetUrl;
        script.dataset.moduleId = moduleId;
        script.onload = () => {
          if (disposed) {
            return;
          }
          const handle = window.AutoOpsModules?.[moduleId];
          if (!handle?.mount) {
            setError(t('模块脚本已加载，但没有注册 mount()'));
            setStatus('load-failed');
            return;
          }
          const result = handle.mount(host, sdk);
          if (typeof result === 'function') {
            const cleanupFn = result as () => void;
            cleanup = () => {
              cleanupFn();
            };
          }
          mounted = true;
          setJsReady(true);
        };
        script.onerror = () => {
          if (!disposed) {
            setError(t('模块入口脚本加载失败'));
            setStatus('load-failed');
          }
        };
        document.head.appendChild(script);
      } catch (bootError) {
        if (!disposed) {
          setError(errorMessage(bootError, t('模块运行时初始化失败')));
          setStatus('load-failed');
        }
      }
    };

    void boot();

    return () => {
      disposed = true;
      const handle = window.AutoOpsModules?.[moduleId];
      try {
        if (handle?.unmount) {
          handle.unmount(host);
        } else if (mounted) {
          cleanup?.();
        }
      } catch (_error) {
        // 模块卸载抛错不应阻塞页面切换
      }
      script?.remove();
      host.replaceChildren();
    };
  }, [assetUrl, message, moduleId, nav?.type, status]);

  if (status === 'loading') {
    return (
      <PageContainer>
        <Flex justify="center" style={{ paddingTop: 120 }}>
          <Spin size="large" />
        </Flex>
      </PageContainer>
    );
  }

  if (status !== 'ready') {
    const config: ResultConfig = (() => {
      switch (status) {
        case 'disabled':
          return {
            status: '403',
            title: t('模块已被禁用'),
            subTitle: t(
              '管理员在「系统设置 → 扩展模块」中启用后才能访问该页面。',
            ),
          };
        case 'missing':
          return {
            status: '404',
            title: t('模块不存在'),
            subTitle: t('该模块未安装或已被卸载。'),
          };
        case 'no-entry':
          return {
            status: '404',
            title: t('模块没有这个页面'),
            subTitle: t(
              '当前地址没有匹配到模块的导航入口，请从侧边栏菜单进入。',
            ),
          };
        case 'load-failed':
          return {
            status: '500',
            title: t('模块加载失败'),
            subTitle: error || t('模块没有正常加载，请查看模块日志后重试。'),
          };
        default:
          return {
            status: '404',
            title: t('模块不可用'),
            subTitle: t('模块不存在、已被禁用，或当前账号无权访问。'),
          };
      }
    })();

    return (
      <PageContainer>
        <Result
          status={config.status}
          title={config.title}
          subTitle={
            config.subTitle ? (
              <span style={{ whiteSpace: 'pre-wrap' }}>{config.subTitle}</span>
            ) : undefined
          }
          extra={
            <Space wrap>
              <Button type="primary" onClick={() => history.push('/dashboard')}>
                {t('返回首页')}
              </Button>
              <Button onClick={() => history.push('/system/modules')}>
                {t('扩展模块管理')}
              </Button>
            </Space>
          }
        />
      </PageContainer>
    );
  }

  const moduleMeta = module ? (
    <Space size={6} wrap>
      {module.version ? <Tag>{`v${module.version}`}</Tag> : null}
      {module.official ? <Tag color="gold">{t('官方')}</Tag> : null}
      {module.experimental ? <Tag color="magenta">{t('实验性')}</Tag> : null}
    </Space>
  ) : null;

  const subTitle: string =
    (module?.description && moduleText(module.description, '')) ||
    (nav?.type === 'js'
      ? t('扩展模块提供的页面，与宿主共用同一套 React / antd 运行时。')
      : t('扩展模块提供的独立页面。'));

  // 模块描述往往很长，直接当 subTitle 会把标题挤成省略号；这里给副标题设上限，
  // 让它自己省略，标题保持完整。
  const subTitleNode = subTitle ? (
    <span
      title={subTitle}
      style={{
        display: 'inline-block',
        maxWidth: '48vw',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        verticalAlign: 'bottom',
      }}
    >
      {subTitle}
    </span>
  ) : undefined;

  return (
    <PageContainer
      title={moduleTitle || t('扩展模块')}
      subTitle={subTitleNode}
      extra={moduleMeta}
    >
      {error ? (
        <Alert
          type="warning"
          showIcon
          banner
          message={error}
          style={{ marginBottom: 8 }}
        />
      ) : null}
      {nav?.type === 'js' ? (
        <Spin spinning={!jsReady} tip={t('正在加载模块…')}>
          <div
            ref={jsHostRef}
            data-module-id={moduleId}
            style={{ minHeight: 'calc(100vh - 180px)' }}
          />
        </Spin>
      ) : (
        <iframe
          title={moduleTitle || t('扩展模块')}
          src={assetUrl}
          style={{
            width: '100%',
            height: 'calc(100vh - 180px)',
            border: 0,
            borderRadius: 8,
            background: '#fff',
          }}
        />
      )}
    </PageContainer>
  );
};

export default ModuleHostPage;
