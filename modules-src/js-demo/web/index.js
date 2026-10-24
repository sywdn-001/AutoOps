/**
 * js-demo：AutoOps 堡垒机 js 模式扩展示例。
 *
 * 这个文件被宿主以 <script type="module"> 直接加载，所以：
 * 1. 不写任何 import 语句（浏览器裸 ESM 解析不了 react / antd 这类裸模块名）；
 * 2. React / ReactDOM / antd / request / getToken / message 全部从宿主注入的
 *    window.AutoOpsSDK 取；
 * 3. 不用 JSX，全部用 React.createElement，避免要求模块自带构建链。
 *
 * 宿主加载脚本后会查找 window.AutoOpsModules['js-demo'] 并调用 mount()。
 */

const MODULE_ID = 'js-demo';
const HEALTH_URL = '/api/health';

let root = null;
let hostEl = null;
let disposed = false;

function renderNotice(el, text) {
  el.replaceChildren();
  const box = document.createElement('div');
  box.style.cssText = [
    'padding:24px',
    'border:1px solid #ffccc7',
    'border-radius:8px',
    'background:#fff2f0',
    'color:#a8071a',
    'font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif',
  ].join(';');
  box.textContent = text;
  el.appendChild(box);
}

function describeError(error, tr = (zh) => zh) {
  const response = error && error.response;
  const status = response && response.status;
  const payload = response && response.data;
  let detail = '';

  if (payload && typeof payload === 'object') {
    detail = payload.message || payload.error || '';
  } else if (typeof payload === 'string') {
    detail = payload;
  }
  if (!detail) {
    detail = (error && error.message) || tr('未知错误');
  }

  if (status === 401) {
    return tr('HTTP 401：登录状态已失效，请重新登录后再试。');
  }
  if (status === 403) {
    return tr('HTTP 403：{detail}', { detail });
  }
  return status ? tr('HTTP {status}：{detail}', { status, detail }) : detail;
}

/**
 * 挂载模块卡片。
 *
 * 宿主只调用、不 await 这个函数，所以这里先同步占位，再等待 antd（兼容
 * 宿主把 antd 注入成 Promise 的情况）。真正的 React root 创建完成后，
 * 页面会自动渲染出来。
 */
export async function mount(el, injectedSdk) {
  if (!el) {
    throw new Error('js-demo: mount(el) 需要一个挂载节点');
  }

  // 幂等：如果同一模块被重复 mount，先清掉上一份 root 和 DOM。
  unmount();
  hostEl = el;
  disposed = false;
  el.replaceChildren();

  const runtime = injectedSdk || window.AutoOpsSDK || {};
  const tr = typeof runtime.t === 'function' ? runtime.t : (zh) => zh;
  if (!runtime || !runtime.React || !runtime.ReactDOM) {
    renderNotice(
      el,
      tr('window.AutoOpsSDK 未注入，或缺少 React/ReactDOM。请确认宿主版本支持 js 模式。'),
    );
    return;
  }

  let antd;
  try {
    // 有的宿主注入的是对象，有的是动态 import() 得到的 Promise；两种都兼容。
    const loaded = await Promise.resolve(runtime.antd);
    antd = loaded && loaded.default && loaded.default.Card ? loaded.default : loaded;
  } catch (error) {
    if (!disposed && hostEl === el) {
      renderNotice(el, tr('antd 加载失败：{message}', { message: describeError(error, tr) }));
    }
    return;
  }

  if (disposed || hostEl !== el) {
    return;
  }
  if (!antd || !antd.Card) {
    renderNotice(el, tr('window.AutoOpsSDK.antd 不可用：缺少 antd 组件。'));
    return;
  }

  const { React, ReactDOM, request, getToken, message } = runtime;
  if (typeof request !== 'function') {
    renderNotice(el, tr('window.AutoOpsSDK.request 不可用，无法调用 /api/health。'));
    return;
  }

  const e = React.createElement;
  const { useEffect, useState } = React;
  const { Alert, Button, Card, Space, Statistic, Switch, Tag, Typography } = antd;

  function Demo() {
    const [health, setHealth] = useState(null);
    const [healthError, setHealthError] = useState('');
    const [healthLoading, setHealthLoading] = useState(false);
    const [counterOn, setCounterOn] = useState(false);
    const [count, setCount] = useState(0);

    let tokenPresent = false;
    try {
      tokenPresent = Boolean(getToken && getToken());
    } catch (_error) {
      tokenPresent = false;
    }

    // Switch 打开时每秒 +1；关闭或组件卸载时清理定时器。
    useEffect(() => {
      if (!counterOn) {
        return undefined;
      }
      const timer = window.setInterval(() => {
        setCount((value) => value + 1);
      }, 1000);
      return () => window.clearInterval(timer);
    }, [counterOn]);

    const loadHealth = async () => {
      setHealthLoading(true);
      setHealthError('');
      setHealth(null);
      try {
        const body = await request(HEALTH_URL, { method: 'GET' });
        const payload = body && typeof body === 'object' && body.data ? body.data : body;
        setHealth({
          service: (payload && payload.service) || 'bastion-backend',
          gateway: payload && payload.gateway_enabled ? tr('已启用') : tr('未启用'),
          port: payload && payload.gateway_port != null ? String(payload.gateway_port) : '-',
        });
      } catch (error) {
        setHealthError(describeError(error, tr));
      } finally {
        setHealthLoading(false);
      }
    };

    const notify = () => {
      const api = message && typeof message.success === 'function' ? message : antd.message;
      if (api && typeof api.success === 'function') {
        api.success(tr('js-demo：React/antd 来自宿主共享运行时'));
      } else {
        window.alert(tr('js-demo：React/antd 来自宿主共享运行时'));
      }
    };

    const healthDescription = health
      ? tr('service={service}；gateway={gateway}；port={port}', {
          service: health.service,
          gateway: health.gateway,
          port: health.port,
        })
      : '';

    return e(
      'div',
      { style: { padding: '24px', maxWidth: 780 } },
      e(
        Card,
        {
          title: e(
            'span',
            null,
            tr('JS 模式示例'),
            ' ',
            e(Typography.Text, { type: 'secondary' }, 'JS Mode Demo'),
          ),
          extra: e(Tag, { color: 'blue' }, 'js mode'),
        },
        e(
          Space,
          { direction: 'vertical', size: 'middle', style: { width: '100%' } },
          e(
            Typography.Paragraph,
            { type: 'secondary', style: { marginBottom: 0 } },
            tr(
              '没有 import、没有 JSX、没有第二份 React。这个卡片直接复用宿主的 React/ReactDOM/antd。',
            ),
          ),
          e(
            Space,
            { wrap: true, align: 'center' },
            e(
              Button,
              { type: 'primary', loading: healthLoading, onClick: loadHealth },
              tr('调用 GET /api/health'),
            ),
            e(
              Tag,
              { color: tokenPresent ? 'green' : 'orange' },
              tokenPresent ? tr('JWT 已注入') : tr('未找到 bastion_token'),
            ),
          ),
          healthError
            ? e(Alert, {
                type: 'error',
                showIcon: true,
                message: tr('健康检查失败'),
                description: healthError,
              })
            : null,
          health
            ? e(Alert, {
                type: 'success',
                showIcon: true,
                message: tr('后端健康检查成功'),
                description: healthDescription,
              })
            : null,
          e(
            'div',
            {
              style: {
                display: 'flex',
                gap: 16,
                alignItems: 'center',
                flexWrap: 'wrap',
                padding: '12px 0',
                borderTop: '1px solid rgba(5, 5, 5, 0.06)',
                borderBottom: '1px solid rgba(5, 5, 5, 0.06)',
              },
            },
            e(
              'div',
              null,
              e(Typography.Text, { strong: true }, tr('antd Switch 计数器')),
              e('div', null, e(Typography.Text, { type: 'secondary' }, tr('打开开关后每秒 +1'))),
            ),
            e(Switch, {
              checked: counterOn,
              onChange: setCounterOn,
              checkedChildren: tr('计数中'),
              unCheckedChildren: tr('已暂停'),
            }),
            e(Statistic, { title: tr('计数'), value: count, style: { minWidth: 120 } }),
            e(Button, { onClick: () => setCount(0) }, tr('重置')),
          ),
          e(
            Space,
            { wrap: true, align: 'center' },
            e(Button, { onClick: notify }, tr('触发 message.success()')),
            e(
              Typography.Text,
              { type: 'secondary' },
              tr('弹提示也走宿主 App 上下文的 message，不另起一份 antd。'),
            ),
          ),
        ),
      ),
    );
  }

  try {
    if (typeof ReactDOM.createRoot === 'function') {
      // React 18/19：react-dom/client 的 createRoot，由宿主合并进 ReactDOM。
      root = ReactDOM.createRoot(el);
      root.render(e(Demo));
    } else if (typeof ReactDOM.render === 'function') {
      // 兼容更老的 React / react-dom；React 19 已移除 render。
      ReactDOM.render(e(Demo), el);
      root = {
        unmount() {
          if (typeof ReactDOM.unmountComponentAtNode === 'function') {
            ReactDOM.unmountComponentAtNode(el);
          } else {
            el.replaceChildren();
          }
        },
      };
    } else {
      renderNotice(el, tr('ReactDOM 既不提供 createRoot 也不提供 render，无法挂载。'));
    }
  } catch (error) {
    if (!disposed && hostEl === el) {
      renderNotice(el, tr('模块挂载失败：{message}', { message: describeError(error, tr) }));
    }
  }
}

/** 卸载：清理定时器（React effect cleanup）、React root 和宿主容器。幂等。 */
export function unmount() {
  disposed = true;

  if (root) {
    const current = root;
    root = null;
    try {
      current.unmount();
    } catch (_error) {
      // 模块卸载抛错不应阻塞宿主页面切换。
    }
  }

  if (hostEl) {
    hostEl.replaceChildren();
    hostEl = null;
  }
}

const api = { mount, unmount };

// 宿主不直接 import 这个 ESM，而是读取这个全局注册表；两套导出都保留。
window.AutoOpsModules = window.AutoOpsModules || {};
window.AutoOpsModules[MODULE_ID] = api;

export default api;
