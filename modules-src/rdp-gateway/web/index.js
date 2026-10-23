/**
 * RDP 网关模块页面（js 模式）。
 *
 * 复用宿主注入的 antd 组件与请求封装，不自带任何 CSS/构建产物。
 * 页面标题、面包屑、官方/实验性标签由宿主 host.tsx 的 PageContainer 负责。
 */

const MODULE_ID = 'rdp-gateway';
const API_BASE = '/api/modules/rdp-gateway/api';

let mountedRoot = null;
let mountedHost = null;
let mountedReactDOM = null;

function pickData(body) {
  if (body && typeof body === 'object' && 'data' in body) {
    return body.data;
  }
  return body;
}

function errorStatus(error) {
  if (!error) return 0;
  if (error.response && error.response.status) return error.response.status;
  if (error.status) return error.status;
  return 0;
}

function errorText(error) {
  const status = errorStatus(error);
  if (status === 401) return '登录状态已失效，请重新登录后再试。';
  if (status === 403) return '权限不足：当前账号无权执行该操作。';
  const body = (error && error.response && error.response.data) || (error && error.data) || error;
  const message = (body && body.message) || (error && error.message) || '请求失败';
  const code = body && body.code ? `（${body.code}）` : '';
  return `${message}${code}`;
}

function formatTime(value) {
  if (!value) return '—';
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', { hour12: false });
  } catch (error) {
    return String(value);
  }
}

function formatBytes(size) {
  const bytes = Number(size);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function copyText(text, messageApi) {
  const value = String(text || '');
  if (!value) return;
  const done = () => messageApi && messageApi.success && messageApi.success('已复制');
  const fail = () => messageApi && messageApi.warning && messageApi.warning('复制失败，请手动选择文本');
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(value).then(done).catch(fail);
    return;
  }
  try {
    const area = document.createElement('textarea');
    area.value = value;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    document.body.removeChild(area);
    done();
  } catch (error) {
    fail();
  }
}

function goSettings(history) {
  const target = '/system/settings';
  try {
    if (history && typeof history.push === 'function') {
      history.push(target);
      return;
    }
  } catch (error) {
    // 回退到整页跳转
  }
  window.location.assign(target);
}

function buildApp(antd, runtime) {
  const React = runtime.React;
  const e = React.createElement;
  const {
    Alert,
    Badge,
    Button,
    Card,
    Col,
    Descriptions,
    Divider,
    Empty,
    Flex,
    Input,
    Modal,
    Popconfirm,
    Row,
    Select,
    Space,
    Spin,
    Statistic,
    Table,
    Tag,
    Tooltip,
    Typography,
  } = antd;
  const Text = Typography.Text;
  const messageApi = runtime.message || antd.message;
  const api = async (path, options) => {
    const body = await runtime.request(`${API_BASE}${path}`, {
      method: 'GET',
      ...(options || {}),
      headers: Object.assign({ 'Cache-Control': 'no-cache' }, (options && options.headers) || {}),
    });
    return pickData(body);
  };

  function RdpGatewayPage() {
    const [status, setStatus] = React.useState(null);
    const [hosts, setHosts] = React.useState([]);
    const [sessions, setSessions] = React.useState([]);
    const [loading, setLoading] = React.useState(true);
    const [refreshing, setRefreshing] = React.useState(false);
    const [pageError, setPageError] = React.useState('');
    const [actionError, setActionError] = React.useState('');
    const [actionLoading, setActionLoading] = React.useState('');
    const [accountChoice, setAccountChoice] = React.useState({});
    const [connectingId, setConnectingId] = React.useState(null);
    const [closingSid, setClosingSid] = React.useState('');
    const [activeSession, setActiveSession] = React.useState(null);
    const [sessionOpen, setSessionOpen] = React.useState(false);
    const [diagnoseOpen, setDiagnoseOpen] = React.useState(false);
    const [diagnose, setDiagnose] = React.useState(null);
    const [diagnoseError, setDiagnoseError] = React.useState('');
    const [logs, setLogs] = React.useState(null);
    const [logsError, setLogsError] = React.useState('');
    const [logsLoading, setLogsLoading] = React.useState(false);
    const logRef = React.useRef(null);

    const loadStatus = React.useCallback(async () => {
      const data = await api('/status');
      setStatus(data || null);
      return data;
    }, []);

    const loadHosts = React.useCallback(async () => {
      const data = await api('/hosts');
      setHosts(Array.isArray(data && data.hosts) ? data.hosts : []);
      return data;
    }, []);

    const refresh = React.useCallback(
      async (quiet) => {
        if (!quiet) setRefreshing(true);
        setPageError('');
        try {
          await Promise.all([loadStatus(), loadHosts()]);
        } catch (error) {
          setPageError(errorText(error));
        } finally {
          if (!quiet) setRefreshing(false);
          setLoading(false);
        }
      },
      [loadStatus, loadHosts],
    );

    const loadLogs = React.useCallback(async () => {
      setLogsLoading(true);
      setLogsError('');
      try {
        const data = await api('/logs?lines=200');
        setLogs(data || null);
      } catch (error) {
        setLogsError(errorText(error));
      } finally {
        setLogsLoading(false);
      }
    }, []);

    React.useEffect(() => {
      refresh(false);
    }, [refresh]);

    React.useEffect(() => {
      const timer = window.setInterval(() => {
        loadStatus().catch(() => {});
      }, 15000);
      return () => window.clearInterval(timer);
    }, [loadStatus]);

    React.useEffect(() => {
      loadLogs();
    }, [loadLogs]);

    React.useEffect(() => {
      const ref = logRef.current;
      const el = ref && (ref.resizableTextArea ? ref.resizableTextArea.textArea : ref);
      if (el) {
        el.scrollTop = el.scrollHeight;
      }
    }, [logs]);

    const runAction = React.useCallback(
      async (name, action, successText) => {
        setActionLoading(name);
        setActionError('');
        try {
          const result = await action();
          if (messageApi && messageApi.success) {
            messageApi.success(
              typeof successText === 'function' ? successText(result) : successText,
            );
          }
          await refresh(true);
          return result;
        } catch (error) {
          const text = errorText(error);
          setActionError(text);
          if (messageApi && messageApi.error) messageApi.error(text);
          return null;
        } finally {
          setActionLoading('');
        }
      },
      [refresh],
    );

    const startGateway = React.useCallback(
      () =>
        runAction(
          'start',
          () => api('/start', { method: 'POST', data: {} }),
          (result) => (result && result.message) || '网关已启动',
        ),
      [runAction],
    );

    const stopGateway = React.useCallback(
      () =>
        runAction(
          'stop',
          () => api('/stop', { method: 'POST', data: {} }),
          (result) => (result && result.message) || '网关已停止',
        ),
      [runAction],
    );

    const openDiagnose = React.useCallback(async () => {
      setDiagnoseOpen(true);
      setDiagnose(null);
      setDiagnoseError('');
      setActionLoading('diagnose');
      try {
        const data = await api('/diagnose');
        setDiagnose(data || null);
      } catch (error) {
        setDiagnoseError(errorText(error));
      } finally {
        setActionLoading('');
      }
    }, []);

    const connectHost = React.useCallback(
      async (host) => {
        const accounts = Array.isArray(host.accounts) ? host.accounts : [];
        const accountId =
          accountChoice[host.hostId] !== undefined
            ? accountChoice[host.hostId]
            : accounts.length
              ? accounts[0].accountId
              : null;
        if (accountId === null || accountId === undefined) {
          if (messageApi && messageApi.warning) {
            messageApi.warning('该主机没有可用的授权账号');
          }
          return;
        }
        setConnectingId(host.hostId);
        setActionError('');
        try {
          const data = await api('/sessions', {
            method: 'POST',
            data: { hostId: host.hostId, accountId },
          });
          const row = {
            sid: (data && data.sid) || '',
            hostId: host.hostId,
            hostName: (data && data.hostName) || host.hostName || '',
            accountUsername: (data && data.accountUsername) || '',
            user: (status && status.currentUser) || '当前账号',
            startedAt: new Date().toISOString(),
            credential: (data && data.credential) || null,
          };
          setSessions((previous) => [row].concat(previous.filter((item) => item.sid !== row.sid)));
          setActiveSession(row);
          setSessionOpen(true);
          if (messageApi && messageApi.success) messageApi.success(`已创建会话 ${row.sid}`);
          loadStatus().catch(() => {});
        } catch (error) {
          const text = errorText(error);
          setActionError(text);
          if (messageApi && messageApi.error) messageApi.error(text);
        } finally {
          setConnectingId(null);
        }
      },
      [accountChoice, status, loadStatus],
    );

    const disconnectSession = React.useCallback(
      async (record) => {
        setClosingSid(record.sid);
        setActionError('');
        try {
          await api(`/sessions/${encodeURIComponent(record.sid)}/close`, {
            method: 'POST',
            data: {},
          });
          setSessions((previous) => previous.filter((item) => item.sid !== record.sid));
          if (messageApi && messageApi.success) messageApi.success('会话已断开');
          loadStatus().catch(() => {});
        } catch (error) {
          const text = errorText(error);
          setActionError(text);
          if (messageApi && messageApi.error) messageApi.error(text);
        } finally {
          setClosingSid('');
        }
      },
      [loadStatus],
    );

    if (loading && !status) {
      return e(
        Card,
        { title: '网关状态' },
        e(
          'div',
          { style: { padding: '32px 0', textAlign: 'center' } },
          e(Spin, { size: 'large' }),
        ),
      );
    }

    const running = Boolean(status && status.running);
    const isAdmin = Boolean(status && status.isAdmin);
    const enabled = status ? Boolean(status.enabled) : true;
    const port =
      (status && (status.connectPort || status.port)) || (status && status.port) || 4489;
    const command = (status && status.connectCommand) || `mstsc /v:<本机IP>:${port}`;
    const binary = (status && status.binary) || '';
    const logFile = (status && status.logFile) || '';
    const onlineSessions =
      status && typeof status.activeSessions === 'number'
        ? status.activeSessions
        : sessions.length;

    const hostColumns = [
      {
        title: '名称',
        dataIndex: 'hostName',
        key: 'hostName',
        width: 180,
        render: (value, record) =>
          e(
            Flex,
            { vertical: true, gap: 2 },
            e(
              Text,
              { strong: true, style: { whiteSpace: 'nowrap' } },
              value || `主机 #${record.hostId}`,
            ),
            record.groupName
              ? e(Tag, { style: { marginInlineEnd: 0 } }, record.groupName)
              : null,
          ),
      },
      {
        title: '地址',
        dataIndex: 'address',
        key: 'address',
        width: 150,
        render: (value) =>
          e(Text, { code: true, style: { whiteSpace: 'nowrap' } }, value || '—'),
      },
      {
        title: '端口',
        dataIndex: 'port',
        key: 'port',
        width: 70,
      },
      {
        title: '协议',
        key: 'protocol',
        width: 70,
        render: () => e(Tag, { color: 'blue' }, 'RDP'),
      },
      {
        title: '在线',
        key: 'online',
        width: 70,
        render: () =>
          e(
            Tag,
            { color: running ? 'green' : 'default' },
            running ? '在线' : '离线',
          ),
      },
      {
        title: '登录账号',
        key: 'account',
        width: 170,
        render: (value, record) => {
          const accounts = Array.isArray(record.accounts) ? record.accounts : [];
          if (!accounts.length) return e(Text, { type: 'secondary' }, '未授权账号');
          const options = accounts.map((account) => ({
            value: account.accountId,
            label: account.username || account.name || `账号 #${account.accountId}`,
          }));
          const selected =
            accountChoice[record.hostId] !== undefined
              ? accountChoice[record.hostId]
              : options[0].value;
          return e(Select, {
            size: 'small',
            style: { minWidth: 130, maxWidth: 160 },
            value: selected,
            options,
            onChange: (next) =>
              setAccountChoice((previous) =>
                Object.assign({}, previous, { [record.hostId]: next }),
              ),
          });
        },
      },
      {
        title: '操作',
        key: 'action',
        width: 80,
        render: (value, record) => {
          const hasAccount =
            Array.isArray(record.accounts) && record.accounts.length > 0;
          const disabled = !running || !hasAccount;
          return e(
            Tooltip,
            { title: !running ? '请先启动网关' : !hasAccount ? '没有可用账号' : '' },
            e(
              Button,
              {
                type: 'link',
                size: 'small',
                disabled,
                loading: connectingId === record.hostId,
                onClick: () => connectHost(record),
              },
              '直连',
            ),
          );
        },
      },
    ];

    const sessionColumns = [
      {
        title: '用户',
        dataIndex: 'user',
        key: 'user',
        width: 120,
      },
      {
        title: '目标机',
        dataIndex: 'hostName',
        key: 'hostName',
      },
      {
        title: '登录账号',
        dataIndex: 'accountUsername',
        key: 'accountUsername',
        width: 150,
      },
      {
        title: '开始时间',
        dataIndex: 'startedAt',
        key: 'startedAt',
        width: 190,
        render: (value) => formatTime(value),
      },
      {
        title: '操作',
        key: 'action',
        width: 90,
        render: (value, record) =>
          e(
            Popconfirm,
            {
              title: '断开该会话？',
              description: '会关闭对应的远程桌面画面桥。',
              onConfirm: () => disconnectSession(record),
              okButtonProps: { danger: true },
            },
            e(
              Button,
              {
                type: 'link',
                size: 'small',
                danger: true,
                loading: closingSid === record.sid,
              },
              '断开',
            ),
          ),
      },
    ];

    const statusItems = [
      {
        key: 'command',
        label: '连接地址',
        span: 2,
        children: e(
          Space,
          { size: 4, wrap: true },
          e(Text, { code: true }, command),
          e(
            Button,
            {
              type: 'link',
              size: 'small',
              onClick: () => copyText(command, messageApi),
            },
            '复制',
          ),
        ),
      },
      {
        key: 'localIp',
        label: '本机地址',
        children: (status && status.localIp) || '—',
      },
      {
        key: 'pid',
        label: '进程 PID',
        children: running && status && status.pid ? String(status.pid) : '—',
      },
      {
        key: 'startedAt',
        label: '最近启动',
        children: formatTime(status && status.startedAt),
      },
      {
        key: 'version',
        label: '模块版本',
        children: (status && status.moduleVersion) || '1.2.0',
      },
      {
        key: 'binary',
        label: '网关程序',
        span: 2,
        children: e(
          Text,
          { code: true, ellipsis: { tooltip: binary } },
          binary || '未找到',
        ),
      },
      {
        key: 'logFile',
        label: '日志文件',
        span: 2,
        children: e(
          Text,
          {
            code: true,
            copyable: { text: logFile },
            style: { wordBreak: 'break-all' },
          },
          logFile || '—',
        ),
      },
    ];

    const sessionCredential = activeSession && activeSession.credential;
    const sessionCommand =
      sessionCredential && sessionCredential.address
        ? `mstsc /v:${sessionCredential.address}:${sessionCredential.port || 3389}`
        : '';

    const content = [];

    if (pageError) {
      content.push(
        e(Alert, {
          key: 'page-error',
          type: 'error',
          showIcon: true,
          closable: true,
          message: '加载失败',
          description: pageError,
          onClose: () => setPageError(''),
        }),
      );
    }

    if (actionError) {
      content.push(
        e(Alert, {
          key: 'action-error',
          type: 'error',
          showIcon: true,
          closable: true,
          message: '操作失败',
          description: actionError,
          onClose: () => setActionError(''),
        }),
      );
    }

    if (status && !isAdmin) {
      content.push(
        e(Alert, {
          key: 'admin-tip',
          type: 'info',
          showIcon: true,
          message: '当前账号不是管理员',
          description: '可以查看网关状态、目标机与日志；启动、停止和诊断需要管理员权限。',
        }),
      );
    }

    if (status && !enabled) {
      content.push(
        e(Alert, {
          key: 'disabled-tip',
          type: 'warning',
          showIcon: true,
          message: 'RDP 网关已在模块设置中停用',
          description: '在宿主「系统设置 → 参数设置」中启用后再回到本页启动。',
        }),
      );
    }

    if (status && !status.binaryExists) {
      content.push(
        e(Alert, {
          key: 'binary-tip',
          type: 'error',
          showIcon: true,
          message: '找不到网关可执行文件',
          description:
            '请确认模块包内包含 bin/bastion-rdp-gateway.exe，或设置 BASTION_RDP_GATEWAY_BIN 指向已编译的程序。',
        }),
      );
    }

    content.push(
      e(
        Card,
        {
          key: 'status',
          size: 'small',
          title: '网关状态',
          extra: e(
            Tag,
            { color: running ? 'green' : 'default' },
            running ? '运行中' : '已停止',
          ),
        },
        e(
          Row,
          { gutter: [16, 16] },
          e(
            Col,
            { xs: 12, sm: 6 },
            e(Statistic, {
              title: '运行状态',
              value: running ? '运行中' : '已停止',
            }),
          ),
          e(
            Col,
            { xs: 12, sm: 6 },
            e(Statistic, { title: '监听端口', value: port }),
          ),
          e(
            Col,
            { xs: 12, sm: 6 },
            e(Statistic, { title: '在线会话', value: onlineSessions }),
          ),
          e(
            Col,
            { xs: 12, sm: 6 },
            e(Statistic, { title: '目标机', value: hosts.length }),
          ),
        ),
        e(Divider, { style: { margin: '16px 0' } }),
        e(Descriptions, {
          size: 'small',
          column: { xs: 1, md: 2 },
          items: statusItems,
        }),
        e(Typography.Paragraph, {
          type: 'secondary',
          style: { marginTop: 12, marginBottom: 0 },
        }, '在 Windows 上打开「远程桌面连接」，粘贴上面的命令即可通过网关连接目标机。'),
      ),
    );

    content.push(
      e(
        Card,
        {
          key: 'hosts',
          size: 'small',
          title: '目标机',
          extra: e(
            Space,
            { size: 8 },
            e(Tag, null, `${hosts.length} 台`),
            e(
              Button,
              { type: 'link', size: 'small', onClick: () => refresh(false) },
              '刷新',
            ),
          ),
        },
        e(Table, {
          size: 'small',
          rowKey: 'hostId',
          dataSource: hosts,
          loading: refreshing,
          pagination: false,
          locale: {
            emptyText: e(Empty, { description: '当前账号没有可用的 RDP 资产' }),
          },
          columns: hostColumns,
        }),
      ),
    );

    content.push(
      e(
        Card,
        {
          key: 'sessions',
          size: 'small',
          title: '会话',
          extra: sessions.length ? e(Tag, { color: 'blue' }, `${sessions.length} 条`) : null,
        },
        e(Table, {
          size: 'small',
          rowKey: 'sid',
          dataSource: sessions,
          pagination: false,
          locale: {
            emptyText: e(Empty, {
              description: '还没有打开的会话，点击目标机的「直连」创建',
            }),
          },
          columns: sessionColumns,
        }),
      ),
    );

    content.push(
      e(
        Card,
        {
          key: 'settings',
          size: 'small',
          title: '参数设置',
          extra: e(
            Button,
            {
              type: 'link',
              size: 'small',
              onClick: () => goSettings(runtime.history),
            },
            '去参数设置修改',
          ),
        },
        e(Descriptions, {
          size: 'small',
          column: 1,
          items: [
            {
              key: 'enabled',
              label: '启用',
              children: e(Badge, {
                status: status && status.enabled ? 'success' : 'default',
                text: status && status.enabled ? '已启用' : '已停用',
              }),
            },
            {
              key: 'port',
              label: '监听端口',
              children: e(Text, { code: true }, String((status && status.port) || 4489)),
            },
            {
              key: 'autoStart',
              label: '开机自动启动',
              children: e(Badge, {
                status: status && status.autoStart ? 'success' : 'default',
                text: status && status.autoStart ? '已开启' : '已关闭',
              }),
            },
          ],
        }),
        e(
          Typography.Paragraph,
          { type: 'secondary', style: { marginTop: 12, marginBottom: 0 } },
          '这些值来自模块设置（mod.rdp-gateway.*），由宿主「系统设置 → 参数设置」写入。',
        ),
      ),
    );

    content.push(
      e(
        Card,
        {
          key: 'logs',
          size: 'small',
          title: '日志',
          extra: e(
            Space,
            { size: 8 },
            e(Text, { type: 'secondary' }, logs && logs.path ? 'gateway.log' : ''),
            e(
              Button,
              {
                type: 'link',
                size: 'small',
                loading: logsLoading,
                onClick: loadLogs,
              },
              '刷新日志',
            ),
          ),
        },
        logsError
          ? e(Alert, {
              type: 'error',
              showIcon: true,
              message: '日志读取失败',
              description: logsError,
              style: { marginBottom: 12 },
            })
          : null,
        e(Input.TextArea, {
          ref: logRef,
          value: (logs && logs.lines ? logs.lines : []).join('\n'),
          readOnly: true,
          rows: 12,
          placeholder: '暂无日志；启动网关后这里会显示 gateway.log 的末尾内容。',
          style: { fontFamily: 'Consolas, Menlo, monospace' },
        }),
        e(
          Text,
          { type: 'secondary' },
          logs && logs.updatedAt
            ? `读取时间：${formatTime(logs.updatedAt)} · 文件大小：${formatBytes(logs.bytes)}`
            : '日志文件位于模块 data 目录。',
        ),
      ),
    );

    const quickCard = e(
      Card,
      {
        key: 'quick',
        size: 'small',
        title: '快捷操作',
      },
      e(
        Space,
        { direction: 'vertical', size: 12, style: { width: '100%' } },
        e(
          Space,
          { wrap: true },
          e(
            Popconfirm,
            {
              title: '启动 RDP 网关？',
              description: `将在端口 ${port} 上监听远程桌面连接。`,
              onConfirm: startGateway,
              disabled: !isAdmin || running,
            },
            e(
              Button,
              {
                type: 'primary',
                disabled: !isAdmin || running,
                loading: actionLoading === 'start',
              },
              '启动',
            ),
          ),
          e(
            Popconfirm,
            {
              title: '停止 RDP 网关？',
              description: '正在连接的 mstsc 会话会被断开。',
              onConfirm: stopGateway,
              okButtonProps: { danger: true },
              disabled: !isAdmin || !running,
            },
            e(
              Button,
              {
                danger: true,
                disabled: !isAdmin || !running,
                loading: actionLoading === 'stop',
              },
              '停止',
            ),
          ),
          e(
            Button,
            {
              onClick: () => refresh(false),
              loading: refreshing,
            },
            '刷新',
          ),
          e(
            Button,
            {
              disabled: !isAdmin,
              loading: actionLoading === 'diagnose',
              onClick: openDiagnose,
            },
            '诊断',
          ),
        ),
        e(
          Button,
          {
            block: true,
            onClick: () => copyText(command, messageApi),
          },
          '复制连接地址',
        ),
        e(
          Text,
          {
            type: 'secondary',
            ellipsis: { tooltip: command },
            style: { maxWidth: '100%' },
          },
          command,
        ),
      ),
    );

    const cards = {};
    const alerts = [];
    content.forEach((item) => {
      if (item && ['status', 'hosts', 'sessions', 'settings', 'logs'].includes(item.key)) {
        cards[item.key] = item;
      } else {
        alerts.push(item);
      }
    });

    const layout = [
      ...alerts,
      cards.status,
      e(
        Row,
        { gutter: [16, 16] },
        e(
          Col,
          { xs: 24, xl: 16 },
          e(
            Space,
            { direction: 'vertical', size: 16, style: { width: '100%' } },
            cards.hosts,
            cards.sessions,
          ),
        ),
        e(
          Col,
          { xs: 24, xl: 8 },
          e(
            Space,
            { direction: 'vertical', size: 16, style: { width: '100%' } },
            cards.settings,
            quickCard,
          ),
        ),
      ),
      cards.logs,
    ];

    const diagnoseContent = diagnose
      ? e(
          Space,
          { direction: 'vertical', size: 12, style: { width: '100%' } },
          e(Descriptions, {
            size: 'small',
            bordered: true,
            column: 1,
            items: [
              {
                key: 'moduleFile',
                label: '模块文件',
                children: e(Text, { code: true }, diagnose.moduleFile || '—'),
              },
              {
                key: 'packageDir',
                label: '包目录',
                children: e(Text, { code: true }, diagnose.packageDir || '—'),
              },
              {
                key: 'dataDir',
                label: '数据目录',
                children: e(Text, { code: true }, diagnose.dataDir || '—'),
              },
              {
                key: 'binary',
                label: '网关程序',
                children: e(Text, { code: true }, diagnose.binary || '—'),
              },
              {
                key: 'platform',
                label: '运行平台',
                children: `${diagnose.platform || '—'} · Python ${diagnose.python || '—'}`,
              },
              {
                key: 'settings',
                label: '设置',
                children: `enabled=${diagnose.enabledSetting} · port=${diagnose.portSetting} · autoStart=${diagnose.autoStartSetting}`,
              },
            ],
          }),
          e(Text, { strong: true }, '可执行文件候选路径'),
          e(Table, {
            size: 'small',
            rowKey: 'path',
            pagination: false,
            dataSource: Array.isArray(diagnose.candidates) ? diagnose.candidates : [],
            columns: [
              {
                title: '路径',
                dataIndex: 'path',
                key: 'path',
                render: (value) => e(Text, { code: true }, value),
              },
              {
                title: '存在',
                dataIndex: 'exists',
                key: 'exists',
                width: 80,
                render: (value) =>
                  e(Tag, { color: value ? 'green' : 'default' }, value ? '是' : '否'),
              },
            ],
          }),
          e(Text, { strong: true }, '日志尾部'),
          e(Input.TextArea, {
            value: (diagnose.logTail || []).join('\n'),
            readOnly: true,
            rows: 6,
            style: { fontFamily: 'Consolas, Menlo, monospace' },
          }),
        )
      : null;

    return e(
      'div',
      null,
      e(
        Space,
        { direction: 'vertical', size: 16, style: { width: '100%' } },
        layout,
      ),
      e(
        Modal,
        {
          title: '网关诊断',
          open: diagnoseOpen,
          width: 760,
          destroyOnClose: true,
          onCancel: () => setDiagnoseOpen(false),
          footer: e(
            Button,
            { onClick: () => setDiagnoseOpen(false) },
            '关闭',
          ),
        },
        diagnoseError
          ? e(Alert, {
              type: 'error',
              showIcon: true,
              message: '诊断失败',
              description: diagnoseError,
            })
          : diagnose
            ? diagnoseContent
            : e(
                'div',
                { style: { padding: '24px 0', textAlign: 'center' } },
                e(Spin, { size: 'large' }),
              ),
      ),
      e(
        Modal,
        {
          title: '会话已创建',
          open: sessionOpen,
          onCancel: () => {
            setSessionOpen(false);
            setActiveSession(null);
          },
          footer: [
            e(
              Button,
              {
                key: 'copy',
                type: 'primary',
                disabled: !sessionCommand,
                onClick: () => copyText(sessionCommand, messageApi),
              },
              '复制连接命令',
            ),
            e(
              Button,
              {
                key: 'close',
                onClick: () => {
                  setSessionOpen(false);
                  setActiveSession(null);
                },
              },
              '关闭',
            ),
          ],
        },
        activeSession
          ? e(
              Space,
              { direction: 'vertical', size: 12, style: { width: '100%' } },
              e(Descriptions, {
                size: 'small',
                bordered: true,
                column: 1,
                items: [
                  {
                    key: 'sid',
                    label: '会话 ID',
                    children: e(
                      Text,
                      { code: true, copyable: { text: activeSession.sid } },
                      activeSession.sid || '—',
                    ),
                  },
                  {
                    key: 'host',
                    label: '目标机',
                    children: activeSession.hostName || '—',
                  },
                  {
                    key: 'account',
                    label: '登录账号',
                    children:
                      (activeSession.credential && activeSession.credential.username) ||
                      activeSession.accountUsername ||
                      '—',
                  },
                  {
                    key: 'password',
                    label: '登录口令',
                    children:
                      activeSession.credential && activeSession.credential.password
                        ? e(
                            Text,
                            {
                              code: true,
                              copyable: {
                                text: activeSession.credential.password,
                              },
                            },
                            activeSession.credential.password,
                          )
                        : '（空口令）',
                  },
                  {
                    key: 'command',
                    label: '连接命令',
                    children: e(
                      Text,
                      { code: true, copyable: { text: sessionCommand } },
                      sessionCommand || '—',
                    ),
                  },
                ],
              }),
              e(Alert, {
                type: 'info',
                showIcon: true,
                message: '在 Windows 上运行上面的 mstsc 命令即可连接',
                description:
                  '登录口令只在这里显示一次；关闭弹窗后如需再次连接，请重新点击「直连」。',
              }),
            )
          : null,
      ),
    );
  }

  return RdpGatewayPage;
}

export async function mount(el, injectedSdk) {
  if (!el) return;
  if (mountedHost) {
    // 宿主重复挂载时先卸载旧根，避免 React 根节点残留
    unmount();
  }
  const runtime = injectedSdk || window.AutoOpsSDK || {};
  const loadedAntd = await Promise.resolve(runtime.antd);
  const antd = loadedAntd && loadedAntd.default && loadedAntd.default.Card ? loadedAntd.default : loadedAntd;
  if (!runtime.React || !runtime.ReactDOM || !antd) {
    throw new Error('宿主没有注入 React/ReactDOM/antd，无法挂载 RDP 网关页面');
  }

  mountedHost = el;
  mountedReactDOM = runtime.ReactDOM;
  const App = buildApp(antd, runtime);
  const element = runtime.React.createElement(App);

  if (typeof runtime.ReactDOM.createRoot === 'function') {
    mountedRoot = runtime.ReactDOM.createRoot(el);
    mountedRoot.render(element);
  } else {
    runtime.ReactDOM.render(element, el);
    mountedRoot = null;
  }
}

export function unmount() {
  try {
    if (mountedRoot && typeof mountedRoot.unmount === 'function') {
      mountedRoot.unmount();
    } else if (mountedReactDOM && typeof mountedReactDOM.unmountComponentAtNode === 'function' && mountedHost) {
      mountedReactDOM.unmountComponentAtNode(mountedHost);
    }
  } catch (error) {
    // 卸载失败时继续清空容器，避免宿主残留旧页面
  }
  if (mountedHost) {
    mountedHost.innerHTML = '';
  }
  mountedRoot = null;
  mountedHost = null;
  mountedReactDOM = null;
}

if (typeof window !== 'undefined') {
  window.AutoOpsModules = window.AutoOpsModules || {};
  window.AutoOpsModules[MODULE_ID] = { mount, unmount };
}
