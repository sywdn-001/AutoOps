/**
 * 操作系统 / 协议的「一眼可辨」标识。
 *
 * 每个平台配一个 antd 图标 + 中文名 + 自己的颜色，资产管理、远程桌面、网页终端三个列表
 * 共用这一份口径（不要各写一套）。图标都在 `@ant-design/icons` 里实测存在：
 * `LinuxOutlined` / `WindowsOutlined` / `AppleOutlined` / `AndroidOutlined` /
 * `CloudServerOutlined` / `DesktopOutlined`。
 */
import {
  AndroidOutlined,
  AppleOutlined,
  CloudServerOutlined,
  CodeOutlined,
  DesktopOutlined,
  LinuxOutlined,
  WindowsOutlined,
} from '@ant-design/icons';
import { Tag } from 'antd';
import React from 'react';

const ICON_STYLE = { fontSize: 13 } as const;
const DEFAULT_COLOR = '#8c8c8c';

export type OsMeta = {
  /** 展示名（中文）。 */
  label: string;
  /** 标签颜色（与平台惯用色一致的十六进制）。 */
  color: string;
  /** 图标节点。 */
  icon: React.ReactNode;
};

const OS_META: Record<string, OsMeta> = {
  linux: {
    label: 'Linux',
    color: '#d48806',
    icon: <LinuxOutlined style={ICON_STYLE} />,
  },
  windows: {
    label: 'Windows',
    color: '#1677ff',
    icon: <WindowsOutlined style={ICON_STYLE} />,
  },
  unix: {
    label: 'Unix',
    color: '#13c2c2',
    icon: <CloudServerOutlined style={ICON_STYLE} />,
  },
  macos: {
    label: 'macOS',
    color: '#595959',
    icon: <AppleOutlined style={ICON_STYLE} />,
  },
  darwin: {
    label: 'macOS',
    color: '#595959',
    icon: <AppleOutlined style={ICON_STYLE} />,
  },
  android: {
    label: 'Android',
    color: '#52c41a',
    icon: <AndroidOutlined style={ICON_STYLE} />,
  },
  other: {
    label: '其它',
    color: DEFAULT_COLOR,
    icon: <DesktopOutlined style={ICON_STYLE} />,
  },
};

/** 取某个 `os_type` 的展示口径；未登记的平台保留原始文字（不吞信息）并退回通用图标。 */
export const osMeta = (osType?: string | null): OsMeta => {
  const key = (osType || '').trim().toLowerCase();
  const hit = OS_META[key];
  if (hit) {
    return hit;
  }
  return {
    label: (osType || '').trim() || '未知系统',
    color: DEFAULT_COLOR,
    icon: <DesktopOutlined style={ICON_STYLE} />,
  };
};

export const osLabel = (osType?: string | null): string => osMeta(osType).label;

const TAG_STYLE = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  marginInlineEnd: 0,
} as const;

/** 资产列表里的「什么系统」：图标 + 中文名，颜色按平台区分。 */
export const OsTag: React.FC<{
  osType?: string | null;
  withLabel?: boolean;
}> = ({ osType, withLabel = true }) => {
  const meta = osMeta(osType);
  return (
    <Tag color={meta.color} style={TAG_STYLE}>
      {meta.icon}
      {withLabel ? meta.label : null}
    </Tag>
  );
};

/**
 * 协议标识：`ssh` = 命令行（网页终端 / SSH 网关），`winrm` = Windows 命令行
 * （网页终端里的 PowerShell，走 WinRS），`rdp` = Windows 远程桌面（图形）。
 *
 * 为什么要在列表里显式标出来：同一个资产在不同入口能做的事完全不同 ——
 * rdp 主机**不会**出现在网页终端与 SSH 网关的菜单里（那两条通道只跑 shell）；
 * 反过来 winrm/ssh 主机也不会出现在远程桌面入口里。
 */
export const ProtocolTag: React.FC<{ protocol?: string | null }> = ({
  protocol,
}) => {
  const key = (protocol || 'ssh').trim().toLowerCase();
  if (key === 'rdp') {
    return (
      <Tag color="geekblue" style={TAG_STYLE}>
        <DesktopOutlined style={ICON_STYLE} />
        RDP
      </Tag>
    );
  }
  if (key === 'winrm') {
    return (
      <Tag color="green" style={TAG_STYLE}>
        <WindowsOutlined style={ICON_STYLE} />
        WinRM
      </Tag>
    );
  }
  return (
    <Tag color="blue" style={TAG_STYLE}>
      <CodeOutlined style={ICON_STYLE} />
      SSH
    </Tag>
  );
};

/** 卡片 / 列表里的小圆点 + 系统名（比整块 Tag 轻，适合塞进标题行）。 */
export const OsDot: React.FC<{ osType?: string | null }> = ({ osType }) => {
  const meta = osMeta(osType);
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        color: meta.color,
      }}
    >
      {meta.icon}
      <span style={{ color: 'inherit' }}>{meta.label}</span>
    </span>
  );
};
