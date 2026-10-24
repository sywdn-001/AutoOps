/**
 * hello-bastion：AutoOps 扩展模块示例（js 模式）。
 *
 * 这个文件由宿主以 `<script type="module">` 直接加载，约束和 js-demo 一样：
 * 1. 不写 import（浏览器裸 ESM 解析不了 react / antd 这类裸模块名）；
 * 2. React / ReactDOM / antd / request / history 全部从宿主注入的
 *    `window.AutoOpsSDK` 取，保证和堡垒机其它页面是同一份运行时、同一套主题；
 * 3. 不用 JSX，全部 React.createElement，模块不需要自带构建链。
 *
 * 页面的标题、面包屑、官方 / 实验性标签由宿主 host.tsx 的 PageContainer 负责，
 * 这里只负责页面内容：全部用 antd 组件拼，不写一行自定义 CSS。
 */

const MODULE_ID = 'hello-bastion';
const API_BASE = `/api/modules/${MODULE_ID}/api`;

let root = null;
let hostEl = null;
let disposed = false;

/** 从宿主 request 的响应里取出业务数据：兼容「统一信封」和「已解包」两种形态。 */
function pickData(body) {
  if (body && typeof body === 'object' && body.data !== undefined) {
    return body.data;
  }
  return body;
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
  return status ? tr('HTTP {status}：{detail}', { status, detail }) : detail;
}

function renderNotice(el, text) {
  el.replaceChildren();
  const box = document.createElement('div');
  box.style.cssText = [
    'padding:16px 24px',
    'border:1px solid #ffccc7',
    'border-radius:8px',
    'background:#fff2f0',
    'color:#a8071a',
    'font:14px/1.6 inherit',
  ].join(';');
  box.textContent = text;
  el.appendChild(box);
}

export async function mount(el, injectedSdk) {
  if (!el) {
    throw new Error('hello-bastion: mount(el) 需要一个挂载节点');
  }

  unmount();
  hostEl = el;
  disposed = false;
  el.replaceChildren();

  const runtime = injectedSdk || window.AutoOpsSDK || {};
  const tr = typeof runtime.t === 'function' ? runtime.t : (zh) => zh;
  if (!runtime || !runtime.React || !runtime.ReactDOM) {
    renderNotice(el, tr('window.AutoOpsSDK 未注入，缺少 React/ReactDOM，无法渲染。'));
    return;
  }

  let antd;
  try {
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

  const { React, ReactDOM, request, history } = runtime;
  if (typeof request !== 'function') {
    renderNotice(el, tr('window.AutoOpsSDK.request 不可用，无法调用模块接口。'));
    return;
  }

  const e = React.createElement;
  const { useCallback, useEffect, useMemo, useRef, useState } = React;
  const {
    Alert,
    Badge,
    Button,
    Card,
    Col,
    Descriptions,
    Divider,
    Flex,
    InputNumber,
    Row,
    Space,
    Statistic,
    Table,
    Tag,
    Tooltip,
    Typography,
  } = antd;
  const Text = Typography.Text;
  const Paragraph = Typography.Paragraph;

  /** GET 模块接口，返回业务数据（自动剥统一信封）。 */
  const apiGet = async (path) => {
    const body = await request(API_BASE + path, { method: 'GET' });
    return pickData(body);
  };

  const goSettings = () => {
    if (history && typeof history.push === 'function') {
      history.push('/system/settings');
      return;
    }
    window.location.assign('/system/settings');
  };

  function HelloBastion() {
    const [stats, setStats] = useState(null);
    const [statsError, setStatsError] = useState('');
    const [loading, setLoading] = useState(false);
    const [updatedAt, setUpdatedAt] = useState('');

    const [hostId, setHostId] = useState(null);
    const [accountId, setAccountId] = useState(null);
    const [credential, setCredential] = useState(null);
    const [credentialError, setCredentialError] = useState('');
    const [credentialLoading, setCredentialLoading] = useState(false);

    const timerRef = useRef(null);

    const loadStats = useCallback(
      async (silent) => {
        if (!silent) {
          setLoading(true);
        }
        setStatsError('');
        try {
          const data = await apiGet('/stats');
          if (disposed) {
            return;
          }
          setStats(data);
          setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
        } catch (error) {
          if (!disposed) {
            setStatsError(describeError(error, tr));
          }
        } finally {
          if (!disposed && !silent) {
            setLoading(false);
          }
        }
      },
      [],
    );

    const settings = (stats && stats.settings) || {};
    const refreshSeconds = Number(settings.refreshSeconds) || 30;

    // 按模块设置里的 refresh_seconds 自动轮询 /stats。
    useEffect(() => {
      void loadStats(false);
      return undefined;
    }, [loadStats]);

    useEffect(() => {
      if (timerRef.current) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
      timerRef.current = window.setInterval(() => {
        void loadStats(true);
      }, Math.max(5, refreshSeconds) * 1000);
      return () => {
        if (timerRef.current) {
          window.clearInterval(timerRef.current);
          timerRef.current = null;
        }
      };
    }, [loadStats, refreshSeconds]);

    const loadCredential = async () => {
      setCredentialError('');
      setCredential(null);
      if (!hostId) {
        setCredentialError(tr('请先填一个资产 ID（堡垒机「资产管理」里的主机 id）。'));
        return;
      }
      setCredentialLoading(true);
      try {
        const query = accountId
          ? `?host_id=${hostId}&account_id=${accountId}`
          : `?host_id=${hostId}`;
        const data = await apiGet(`/credentials-sample${query}`);
        if (!disposed) {
          setCredential(data);
        }
      } catch (error) {
        if (!disposed) {
          setCredentialError(describeError(error, tr));
        }
      } finally {
        if (!disposed) {
          setCredentialLoading(false);
        }
      }
    };

    const overviewItems = useMemo(
      () => [
        { title: tr('主机总数'), value: stats ? stats.hostTotal : 0 },
        { title: tr('在线会话'), value: stats ? stats.activeSessions : 0 },
        { title: tr('自动刷新间隔（秒）'), value: refreshSeconds },
        { title: tr('最近刷新'), value: updatedAt || '—' },
      ],
      [refreshSeconds, stats, updatedAt],
    );

    const settingsRows = [
      { key: 'show_greeting', label: tr('显示问候语'), value: settings.showGreeting },
      { key: 'refresh_seconds', label: tr('自动刷新间隔（秒）'), value: refreshSeconds },
      { key: 'greeting_text', label: tr('问候语'), value: settings.greetingText },
    ];

    return e(
      Space,
      { orientation: 'vertical', size: 16, style: { width: '100%' } },

      // ---- 运行概况 ----
      e(
        Card,
        {
          title: tr('运行概况'),
          extra: e(
            Space,
            { size: 8 },
            e(Tag, { color: 'blue' }, tr('每 {seconds} 秒自动刷新', { seconds: refreshSeconds })),
            e(
              Button,
              { onClick: () => loadStats(false), loading },
              tr('立即刷新'),
            ),
          ),
        },
        e(
          Flex,
          { vertical: true, gap: 16 },
          statsError
            ? e(Alert, {
                type: 'error',
                showIcon: true,
                message: tr('读取 /stats 失败'),
                description: statsError,
              })
            : null,
          settings.showGreeting && settings.greetingText
            ? e(Alert, {
                type: 'success',
                showIcon: true,
                message: settings.greetingText,
                description: tr(
                  '这段文案来自模块设置 greeting_text，可在「系统设置 → 参数设置」里改。',
                ),
              })
            : null,
          e(
            Row,
            { gutter: [16, 16] },
            overviewItems.map((item) =>
              e(
                Col,
                { key: item.title, xs: 24, sm: 12, xl: 6 },
                e(Statistic, { title: item.title, value: item.value }),
              ),
            ),
          ),
          e(
            Text,
            { type: 'secondary' },
            tr(
              '数据来自模块自己的 GET /stats 接口：主机数与在线会话数由宿主 SDK（autoops.bastion）异步查询。',
            ),
          ),
        ),
      ),

      // ---- 模块设置 ----
      e(
        Card,
        {
          title: tr('模块设置'),
          extra: e(
            Button,
            { type: 'link', onClick: goSettings },
            tr('去参数设置修改'),
          ),
        },
        e(
          Descriptions,
          {
            column: { xs: 1, md: 2 },
            items: settingsRows.map((row) => ({
              key: row.key,
              label: row.label,
              children:
                row.key === 'show_greeting'
                  ? e(
                      Badge,
                      {
                        status: row.value ? 'success' : 'default',
                        text: row.value ? tr('已开启') : tr('已关闭'),
                      },
                    )
                  : e(Text, { code: true }, String(row.value ?? '—')),
            })),
          },
        ),
        e(Divider, { style: { margin: '16px 0 12px' } }),
        e(
          Table,
          {
            size: 'small',
            pagination: false,
            rowKey: 'key',
            dataSource: [
              { key: 'mod.hello-bastion.show_greeting', value: tr('bool，默认 1') },
              { key: 'mod.hello-bastion.refresh_seconds', value: tr('int，5-600，默认 30') },
              { key: 'mod.hello-bastion.greeting_text', value: tr('string，默认 Hello, Bastion!') },
            ],
            columns: [
              { title: tr('配置项'), dataIndex: 'key', render: (value) => e(Text, { code: true }, value) },
              { title: tr('类型与默认值'), dataIndex: 'value' },
            ],
          },
        ),
        e(
          Paragraph,
          { type: 'secondary', style: { marginTop: 12, marginBottom: 0 } },
          tr(
            '模块设置由 description.json 的 settings[] 声明，宿主在安装时自动并入参数页，键名前缀为 mod.<模块 id>.。',
          ),
        ),
      ),

      // ---- 异步 SDK 演示 ----
      e(
        Card,
        {
          title: tr('异步 SDK 演示：读取资产凭证摘要'),
          extra: e(Tag, { color: 'purple' }, 'autoops.bastion'),
        },
        e(
          Flex,
          { vertical: true, gap: 12 },
          e(
            Paragraph,
            { type: 'secondary', style: { marginBottom: 0 } },
            tr(
              '模块里的 bastion.credentials.get() 是 async 函数；下面的按钮调用模块自己的 GET /credentials-sample，只返回用户名与口令长度，明文永远不出后端。',
            ),
          ),
          e(
            Space,
            { wrap: true, align: 'center' },
            e(InputNumber, {
              placeholder: tr('资产 ID（必填）'),
              style: { width: 180 },
              min: 1,
              value: hostId,
              onChange: setHostId,
            }),
            e(InputNumber, {
              placeholder: tr('账号 ID（可选）'),
              style: { width: 180 },
              min: 1,
              value: accountId,
              onChange: setAccountId,
            }),
            e(
              Button,
              { type: 'primary', loading: credentialLoading, onClick: loadCredential },
              tr('读取凭证摘要'),
            ),
            e(
              Tooltip,
              { title: 'GET /api/modules/hello-bastion/api/credentials-sample?host_id=1' },
              e(Text, { type: 'secondary' }, tr('查看接口')),
            ),
          ),
          credentialError
            ? e(Alert, {
                type: 'error',
                showIcon: true,
                message: tr('读取失败'),
                description: credentialError,
              })
            : null,
          credential
            ? e(
                Descriptions,
                {
                  bordered: true,
                  size: 'small',
                  column: { xs: 1, md: 2 },
                  items: [
                    { key: 'hostId', label: tr('资产 ID'), children: credential.hostId },
                    { key: 'accountId', label: tr('账号 ID'), children: credential.accountId ?? '—' },
                    { key: 'username', label: tr('用户名'), children: e(Text, { code: true }, credential.username || '—') },
                    { key: 'passwordLength', label: tr('口令长度'), children: tr('{length} 位', { length: credential.passwordLength ?? 0 }) },
                  ],
                },
              )
            : null,
        ),
      ),

      // ---- 关于 ----
      e(
        Card,
        { title: tr('关于本示例') },
        e(
          Descriptions,
          {
            column: { xs: 1, md: 2 },
            items: [
              {
                key: 'id',
                label: tr('模块 ID'),
                children: e(Text, { code: true }, (stats && stats.module && stats.module.id) || MODULE_ID),
              },
              {
                key: 'version',
                label: tr('版本'),
                children: (stats && stats.module && stats.module.version) || '—',
              },
              { key: 'type', label: tr('页面模式'), children: e(Tag, { color: 'blue' }, tr('js（宿主注入 React / antd）')) },
              { key: 'api', label: tr('接口前缀'), children: e(Text, { code: true }, API_BASE) },
              { key: 'author', label: tr('作者'), children: tr('AutoOps（官方）') },
              { key: 'generated', label: tr('本次数据时间'), children: (stats && stats.generatedAt) || '—' },
            ],
          },
        ),
        e(Divider, { style: { margin: '16px 0 12px' } }),
        e(
          Space,
          { wrap: true },
          e(
            Button,
            {
              onClick: () => {
                if (history && typeof history.push === 'function') {
                  history.push('/system/modules');
                } else {
                  window.location.assign('/system/modules');
                }
              },
            },
            tr('打开扩展模块管理'),
          ),
          e(
            Button,
            {
              onClick: () => {
                const { message } = runtime;
                if (message && typeof message.success === 'function') {
                  message.success(
                    tr('{text}：来自 antd message', { text: settings.greetingText || 'Hello, Bastion' }),
                  );
                }
              },
            },
            tr('弹一条 antd 提示'),
          ),
          e(Text, { type: 'secondary' }, tr('页面只用 antd 组件，没有自定义 CSS。')),
        ),
      ),
    );
  }

  try {
    if (typeof ReactDOM.createRoot === 'function') {
      root = ReactDOM.createRoot(el);
      root.render(e(HelloBastion));
    } else if (typeof ReactDOM.render === 'function') {
      ReactDOM.render(e(HelloBastion), el);
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

export function unmount() {
  disposed = true;
  if (root) {
    const current = root;
    root = null;
    try {
      current.unmount();
    } catch (_error) {
      // 卸载报错不影响宿主切页
    }
  }
  if (hostEl) {
    hostEl.replaceChildren();
    hostEl = null;
  }
}

const api = { mount, unmount };
window.AutoOpsModules = window.AutoOpsModules || {};
window.AutoOpsModules[MODULE_ID] = api;

export default api;
