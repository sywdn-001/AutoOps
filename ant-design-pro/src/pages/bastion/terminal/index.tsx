/**
 * 网页终端入口：可访问资产列表（**一台主机一行，行内每个协议一个入口**）。
 *
 * 对齐 JumpServer 的形态——**点「连接」用 `window.open()` 弹出一个独立窗口**跑一条会话，
 * 而不是把多个会话（和实时审计）挤在同一页里。本页只负责选资产、选账号、开窗口。
 *
 * 关于「一台主机多协议」：后端 `/api/terminal/targets` 是**一个协议端点返回一条**
 * （同一台机器可能同时有 ssh / winrm / rdp 三个入口），本页按 `hostId` 把它们合并成
 * 一行：地址列列出每个端点的 `地址:端口`，操作列每个端点一颗按钮（ssh/winrm 弹网页终端、
 * rdp 弹远程桌面），账号下拉取所有端点的账号并集。这样同一台 Windows 机器不会再出现
 * 「两行看起来差不多」的观感问题。
 */
import {
  ExportOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import type { ActionType, ProColumns } from '@ant-design/pro-components';
import { PageContainer, ProTable } from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import { Alert, Button, message, Select, Space, Tooltip } from 'antd';
import { useCallback, useRef, useState } from 'react';
import { OsTag } from '@/components/Bastion';
import { buildRdpConsoleUrl } from '@/pages/bastion/rdp/types';
import { terminalApi } from '@/services/bastion/endpoints';
import type { TerminalTarget } from '@/services/bastion/types';
import './console.css';
import {
  buildConsoleUrl,
  buildFileConsoleUrl,
  consoleWindowFeatures,
} from './types';

/**
 * 这一台机器可以用哪些资产账号开窗口（所有端点账号的并集，按 id 去重）。
 */
const mergeAccounts = (endpoints: TerminalTarget[]) => {
  const seen = new Map<
    number,
    { id: number; name: string; username: string; authType?: string }
  >();
  for (const endpoint of endpoints) {
    for (const account of endpoint.accounts ?? []) {
      if (!seen.has(account.id)) {
        seen.set(account.id, account);
      }
    }
  }
  return [...seen.values()];
};

/**
 * 合并后的列表行：一台主机一行，`endpoints` 里是它所有协议入口。
 */
type LauncherRow = {
  hostId: number;
  hostName: string;
  address: string;
  osType: string;
  groupName: string;
  description: string;
  policyName: string;
  filePolicyName: string;
  maxSessions: number;
  canWebterm: boolean;
  canSftp: boolean;
  accounts: { id: number; name: string; username: string; authType?: string }[];
  endpoints: TerminalTarget[];
};

/** 把「一个端点一条」的目标列表按 hostId 收成「一台主机一行」。 */
const mergeTargets = (targets: TerminalTarget[]): LauncherRow[] => {
  const rows = new Map<number, LauncherRow>();
  for (const target of targets ?? []) {
    const existing = rows.get(target.hostId);
    if (existing) {
      existing.endpoints.push(target);
      existing.canWebterm = existing.canWebterm || target.canWebterm;
      existing.canSftp = existing.canSftp || target.canSftp;
      existing.maxSessions = Math.max(existing.maxSessions, target.maxSessions);
      if (!existing.policyName && target.policyName) {
        existing.policyName = target.policyName;
      }
      continue;
    }
    rows.set(target.hostId, {
      hostId: target.hostId,
      hostName: target.hostName,
      address: target.address,
      osType: target.osType,
      groupName: target.groupName,
      description: target.description,
      policyName: target.policyName,
      filePolicyName: target.filePolicyName,
      maxSessions: target.maxSessions,
      canWebterm: target.canWebterm,
      canSftp: target.canSftp,
      accounts: [],
      endpoints: [target],
    });
  }
  return [...rows.values()].map((row) => ({
    ...row,
    endpoints: [...row.endpoints].sort((a, b) =>
      a.protocol.localeCompare(b.protocol),
    ),
    accounts: mergeAccounts(row.endpoints),
  }));
};

/**
 * 这台机器可以用哪些资产账号开窗口。
 *
 * 远程桌面只认口令账号（CredSSP/NLA 要在浏览器侧算 NTLM 应答，密钥账号根本没口令可算），
 * WinRM 同样只认「用户名 + 口令」（WinRS 不支持私钥登录），所以在合并列表里先把密钥
 * 账号滤掉，用户不会选到一个注定失败的账号。
 */
const needsPasswordAccount = (protocol: string) =>
  protocol === 'rdp' || protocol === 'winrm';

const usableAccounts = (endpoints: TerminalTarget[]) => {
  const all = mergeAccounts(endpoints);
  if (endpoints.some((endpoint) => !needsPasswordAccount(endpoint.protocol))) {
    return all;
  }
  return all.filter(
    (account) => (account.authType || 'password') === 'password',
  );
};

/** 每个协议入口在操作列里的按钮文案 */
const PROTOCOL_BUTTON_TEXT: Record<string, string> = {
  ssh: '连接',
  winrm: 'WinRM',
  rdp: '远程桌面',
};

/**
 * 地址后面缀一句通道说明：同一台机器可能同时有 ssh / winrm / rdp 入口，
 * 光看 `地址:端口` 分不清哪个是哪个。
 */
const protocolHint = (protocol: string) => {
  if (protocol === 'winrm') {
    return ' · WinRM 网页终端';
  }
  if (protocol === 'rdp') {
    return ' · 远程桌面';
  }
  if (protocol === 'ssh') {
    return ' · SSH 网页终端';
  }
  return '';
};

const protocolButtonText = (protocol: string) =>
  PROTOCOL_BUTTON_TEXT[protocol] ?? '连接';

/**
 * 操作列里那颗按钮的 Tooltip：远程桌面要额外说明「口令会下发到浏览器」。
 */
const protocolButtonTip = (
  protocol: string,
  disabled: boolean,
  reason: string,
) => {
  if (disabled) {
    return reason;
  }
  if (protocol === 'rdp') {
    return '弹出独立远程桌面窗口。CredSSP/NLA 必须在浏览器侧完成，所以该资产账号的口令会下发到你的浏览器（服务端会单独留一条审计），请只在自己信得过的设备上使用';
  }
  if (protocol === 'winrm') {
    return '弹出独立终端窗口（Windows PowerShell / WinRM 字符会话）';
  }
  return '弹出独立终端窗口';
};

const TerminalLauncherPage = () => {
  const access = useAccess();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [picked, setPicked] = useState<Record<number, number>>({});
  // 一台主机多协议：loading 状态要按「主机 + 协议」区分，不能只按 hostId
  const [opening, setOpening] = useState<string>();
  const [openingFiles, setOpeningFiles] = useState<string>();

  /** 开一个独立的文件管理器窗口（SFTP），与终端窗口同形态、互不影响。 */
  const openFiles = useCallback(
    (endpoint: TerminalTarget, accountId: number) => {
      setOpeningFiles(`${endpoint.hostId}:${endpoint.protocol}`);
      const url = buildFileConsoleUrl({
        hostId: endpoint.hostId,
        accountId,
        hostName: endpoint.hostName,
      });
      const name = `bastion-files-${endpoint.hostId}-${accountId}-${Date.now()}`;
      const opened = window.open(url, name, consoleWindowFeatures());
      if (opened) {
        opened.focus();
      } else {
        message.warning(
          '浏览器拦截了文件管理器弹窗，请允许本站点弹出窗口后重试。',
        );
      }
      setOpeningFiles(undefined);
    },
    [],
  );

  const openConsole = useCallback(
    (endpoint: TerminalTarget, accountId: number) => {
      const isRdp = endpoint.protocol === 'rdp';
      setOpening(`${endpoint.hostId}:${endpoint.protocol}`);
      const url = isRdp
        ? buildRdpConsoleUrl({
            hostId: endpoint.hostId,
            accountId,
            hostName: endpoint.hostName,
          })
        : buildConsoleUrl({
            hostId: endpoint.hostId,
            accountId,
            hostName: endpoint.hostName,
            // 一台主机多协议：必须告诉控制台窗口要开哪个端点（ssh / winrm）
            protocol: endpoint.protocol,
          });
      // 用 window.open(url, name, features) 的第三参数弹出**独立窗口**（不给 features 就只是开标签页）。
      // name 带时间戳：每次点「连接」都是一个新窗口 = 一条新会话，跟之前「一点一个新标签页」的行为对齐。
      const name = isRdp
        ? `bastion-rdp-${endpoint.hostId}-${accountId}-${Date.now()}`
        : `bastion-console-${endpoint.hostId}-${accountId}-${Date.now()}`;
      // 坑：'noopener' 是 feature 而不是 name，且带它时 window.open 按规范**恒返回 null**，
      // 拿返回值判断「是否被拦截」会误报；另外 `opened.opener = null` 会让弹窗里的「资产列表」
      // 按钮无法聚焦回原窗口。本页与弹窗同源且都是自家代码，保留 opener 更实用。
      const opened = window.open(url, name, consoleWindowFeatures());
      if (opened) {
        opened.focus();
      } else {
        message.warning(
          isRdp
            ? '浏览器拦截了远程桌面弹窗，请允许本站点弹出窗口后重试。'
            : '浏览器拦截了终端弹窗，请允许本站点弹出窗口后重试。',
        );
      }
      setOpening(undefined);
    },
    [],
  );

  const columns: ProColumns<LauncherRow>[] = [
    {
      title: '资产',
      dataIndex: 'hostName',
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <OsTag osType={record.osType} />
            <span className="bastion-launcher-host">{record.hostName}</span>
          </Space>
          {record.endpoints.map((endpoint) => (
            <span key={endpoint.protocol} className="bastion-launcher-addr">
              {record.address}:{endpoint.port}
              {protocolHint(endpoint.protocol)}
            </span>
          ))}
        </Space>
      ),
    },
    {
      title: '分组',
      dataIndex: 'groupName',
      width: 120,
      render: (_, record) => record.groupName || '—',
    },
    {
      title: '命令策略',
      dataIndex: 'policyName',
      width: 180,
      render: (_, record) => record.policyName || '系统默认策略',
    },
    {
      title: '并发上限',
      dataIndex: 'maxSessions',
      width: 100,
      render: (_, record) =>
        record.maxSessions > 0 ? `${record.maxSessions} 条` : '不限',
    },
    {
      title: '资产账号',
      key: 'account',
      width: 220,
      render: (_, record) => {
        const accounts = usableAccounts(record.endpoints);
        if (!accounts.length) {
          return <span className="bastion-launcher-warn">无可用账号</span>;
        }
        const value = picked[record.hostId] ?? accounts[0]?.id;
        return (
          <Select
            size="small"
            style={{ width: '100%' }}
            value={value}
            options={accounts.map((account) => ({
              label: `${account.name}（${account.username}）`,
              value: account.id,
            }))}
            onChange={(next) =>
              setPicked((prev) => ({
                ...prev,
                [record.hostId]: next as number,
              }))
            }
          />
        );
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 240,
      render: (_, record) => {
        const accounts = usableAccounts(record.endpoints);
        const accountId = picked[record.hostId] ?? accounts[0]?.id;
        return (
          <Space size={4} wrap>
            {record.endpoints.map((endpoint) => {
              // 每个端点单独判可用性：远程桌面 / WinRM 只认口令账号，ssh 两种都行
              const endpointAccounts = needsPasswordAccount(endpoint.protocol)
                ? accounts.filter(
                    (account) =>
                      (account.authType || 'password') === 'password',
                  )
                : accounts;
              const pickedOk = endpointAccounts.some(
                (account) => account.id === accountId,
              );
              const disabled = !endpoint.canWebterm || !accountId || !pickedOk;
              const reason = !endpoint.canWebterm
                ? '该资产的授权未开放交互式登录'
                : !pickedOk
                  ? needsPasswordAccount(endpoint.protocol)
                    ? '该协议只认口令账号（远程桌面 / WinRM 不支持密钥登录），请在账号列选一个口令账号'
                    : '该主机下没有可用资产账号'
                  : '该主机下没有可用资产账号';
              const key = `${record.hostId}:${endpoint.protocol}`;
              // WinRM 与远程桌面都没有 SFTP 通道（WinRS 只跑命令、RDP 只给画面），
              // 别给一个点了才报错的入口 —— 「文件」只挂在 ssh 端点上。
              const filesDisabled =
                !endpoint.canSftp || !accountId || !pickedOk;
              const filesReason = !endpoint.canSftp
                ? '该资产的授权未开放 SFTP 文件管理'
                : filesDisabled
                  ? '该主机下没有可用资产账号'
                  : '弹出独立文件管理器窗口';
              return (
                <Space size={4} key={key}>
                  <Tooltip
                    title={protocolButtonTip(
                      endpoint.protocol,
                      disabled,
                      reason,
                    )}
                  >
                    <Button
                      type="primary"
                      size="small"
                      icon={<ExportOutlined />}
                      disabled={disabled}
                      loading={opening === key}
                      onClick={() => openConsole(endpoint, accountId as number)}
                    >
                      {protocolButtonText(endpoint.protocol)}
                    </Button>
                  </Tooltip>
                  {endpoint.protocol === 'ssh' ? (
                    <Tooltip
                      title={
                        endpoint.canSftp && !filesDisabled
                          ? '弹出独立文件管理器窗口'
                          : filesReason
                      }
                    >
                      <Button
                        size="small"
                        icon={<FolderOpenOutlined />}
                        disabled={filesDisabled}
                        loading={openingFiles === key}
                        onClick={() => openFiles(endpoint, accountId as number)}
                      >
                        文件
                      </Button>
                    </Tooltip>
                  ) : null}
                </Space>
              );
            })}
          </Space>
        );
      },
    },
  ];

  // 只有远程桌面权限、没有终端权限的账号也要能用本页（列表里只有 Windows 主机）
  if (!access.canTerminalUse && !access.canRdpUse) {
    return (
      <PageContainer title="网页终端">
        <Alert
          type="warning"
          showIcon
          title="没有网页终端权限"
          description="当前账号缺少 terminal:use（网页终端）与 rdp:use（Windows 远程桌面）权限。请联系管理员在角色里勾选「网页终端」或「Windows 远程桌面」后再试。"
        />
      </PageContainer>
    );
  }

  return (
    <PageContainer title="网页终端">
      <ProTable<LauncherRow>
        rowKey="hostId"
        actionRef={actionRef}
        columns={columns}
        search={false}
        pagination={false}
        cardBordered
        headerTitle="可访问资产"
        options={{ reload: true, density: false, setting: false }}
        toolBarRender={() => [
          <Button
            key="reload"
            icon={<ReloadOutlined />}
            onClick={() => actionRef.current?.reload()}
          >
            刷新
          </Button>,
        ]}
        locale={{
          emptyText: (
            <Space direction="vertical" size={4}>
              <span>没有可连接的资产</span>
              <span className="bastion-launcher-addr">
                需要管理员在「访问授权」里为你的账号配置「用户 × 主机 ×
                账号」授权，并勾选交互式登录（Linux 走网页终端，Windows 走 WinRM
                网页终端或远程桌面）。
              </span>
            </Space>
          ),
        }}
        request={async () => {
          const data = await terminalApi.targets();
          // 后端是「一个协议端点一条」，这里按 hostId 收成「一台主机一行」
          return { data: mergeTargets(data ?? []), success: true };
        }}
      />
    </PageContainer>
  );
};

export default TerminalLauncherPage;
