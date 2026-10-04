/**
 * 网页终端入口：可访问资产列表（**字符终端与 Windows 远程桌面共用一个入口**）。
 *
 * 对齐 JumpServer 的形态——**点「连接」用 `window.open()` 弹出一个独立窗口**跑一条会话，
 * 而不是把多个会话（和实时审计）挤在同一页里。本页只负责选资产、选账号、开窗口。
 *
 * 关于 Windows 主机：以前远程桌面有过一个独立菜单（`/rdp`），现在合并到本页 —— 列表里
 * 既有 Linux 也有 Windows，点「连接」时按主机的 `protocol` 决定弹哪种窗口：
 * `ssh` → `/terminal/console`（网页终端），`rdp` → `/rdp/console`（远程桌面）。
 * 页面上不额外标注哪台是远程桌面：系统图标已经说明了平台，能力差异由按钮状态体现。
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
 * 这台机器可以用哪些资产账号开窗口。
 *
 * 远程桌面只认口令账号（CredSSP/NLA 要在浏览器侧算 NTLM 应答，密钥账号根本没口令可算），
 * 所以在合并列表里先把密钥账号滤掉，用户不会选到一个注定失败的账号。
 */
const usableAccounts = (target: TerminalTarget) =>
  target.protocol === 'rdp'
    ? target.accounts.filter(
        (account) => (account.authType || 'password') === 'password',
      )
    : target.accounts;

const TerminalLauncherPage = () => {
  const access = useAccess();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [picked, setPicked] = useState<Record<number, number>>({});
  const [opening, setOpening] = useState<number>();
  const [openingFiles, setOpeningFiles] = useState<number>();

  /** 开一个独立的文件管理器窗口（SFTP），与终端窗口同形态、互不影响。 */
  const openFiles = useCallback((target: TerminalTarget, accountId: number) => {
    setOpeningFiles(target.hostId);
    const url = buildFileConsoleUrl({
      hostId: target.hostId,
      accountId,
      hostName: target.hostName,
    });
    const name = `bastion-files-${target.hostId}-${accountId}-${Date.now()}`;
    const opened = window.open(url, name, consoleWindowFeatures());
    if (opened) {
      opened.focus();
    } else {
      message.warning(
        '浏览器拦截了文件管理器弹窗，请允许本站点弹出窗口后重试。',
      );
    }
    setOpeningFiles(undefined);
  }, []);

  const openConsole = useCallback(
    (target: TerminalTarget, accountId: number) => {
      const isRdp = target.protocol === 'rdp';
      setOpening(target.hostId);
      const url = isRdp
        ? buildRdpConsoleUrl({
            hostId: target.hostId,
            accountId,
            hostName: target.hostName,
          })
        : buildConsoleUrl({
            hostId: target.hostId,
            accountId,
            hostName: target.hostName,
          });
      // 用 window.open(url, name, features) 的第三参数弹出**独立窗口**（不给 features 就只是开标签页）。
      // name 带时间戳：每次点「连接」都是一个新窗口 = 一条新会话，跟之前「一点一个新标签页」的行为对齐。
      const name = isRdp
        ? `bastion-rdp-${target.hostId}-${accountId}-${Date.now()}`
        : `bastion-console-${target.hostId}-${accountId}-${Date.now()}`;
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

  const columns: ProColumns<TerminalTarget>[] = [
    {
      title: '资产',
      dataIndex: 'hostName',
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <OsTag osType={record.osType} />
            <span className="bastion-launcher-host">{record.hostName}</span>
          </Space>
          <span className="bastion-launcher-addr">
            {record.address}:{record.port}
          </span>
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
        const accounts = usableAccounts(record);
        if (!accounts.length) {
          return (
            <span className="bastion-launcher-warn">
              {record.protocol === 'rdp' ? '无口令账号' : '无可用账号'}
            </span>
          );
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
      width: 200,
      render: (_, record) => {
        const isRdp = record.protocol === 'rdp';
        const accounts = usableAccounts(record);
        const accountId = picked[record.hostId] ?? accounts[0]?.id;
        const disabled = !record.canWebterm || !accountId;
        const reason = !record.canWebterm
          ? '该资产的授权未开放交互式登录'
          : isRdp
            ? '该主机下没有可用的口令账号（远程桌面需要口令认证）'
            : '该主机下没有可用资产账号';
        const filesDisabled = !record.canSftp || !accountId;
        const filesReason = !record.canSftp
          ? '该资产的授权未开放 SFTP 文件管理'
          : '该主机下没有可用资产账号';
        return (
          <Space size={4}>
            <Tooltip
              title={
                disabled
                  ? reason
                  : isRdp
                    ? '弹出独立远程桌面窗口。CredSSP/NLA 必须在浏览器侧完成，所以该资产账号的口令会下发到你的浏览器（服务端会单独留一条审计），请只在自己信得过的设备上使用'
                    : '弹出独立终端窗口'
              }
            >
              <Button
                type="primary"
                size="small"
                icon={<ExportOutlined />}
                disabled={disabled}
                loading={opening === record.hostId}
                onClick={() => openConsole(record, accountId as number)}
              >
                连接
              </Button>
            </Tooltip>
            {/* 远程桌面没有 SFTP 通道，索性不渲染「文件」按钮，不给点了才报错的入口 */}
            {isRdp ? null : (
              <Tooltip
                title={filesDisabled ? filesReason : '弹出独立文件管理器窗口'}
              >
                <Button
                  size="small"
                  icon={<FolderOpenOutlined />}
                  disabled={filesDisabled}
                  loading={openingFiles === record.hostId}
                  onClick={() => openFiles(record, accountId as number)}
                >
                  文件
                </Button>
              </Tooltip>
            )}
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
      <ProTable<TerminalTarget>
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
                账号」授权，并勾选交互式登录（Linux 走网页终端，Windows
                走远程桌面）。
              </span>
            </Space>
          ),
        }}
        request={async () => {
          const data = await terminalApi.targets();
          return { data: data ?? [], success: true };
        }}
      />
    </PageContainer>
  );
};

export default TerminalLauncherPage;
