/**
 * 后端下发的侧边栏图标是 antd 图标组件的**名字符串**（如 `DesktopOutlined`）。
 * `@ant-design/icons` 没有运行时注册表，要支持任意名字只能整包命名空间导入；
 * 找不到 / 不是合法组件时回退 `AppstoreOutlined`，保证菜单不会出现空白图标。
 *
 * 注意：命名空间导入会把整套图标打进构建产物（源码约 1.7MB，压缩后约 150KB）。
 * 若后续对包体积敏感，可改成白名单映射，但需要同步限制模块作者可用的图标名。
 */
import * as AntIcons from '@ant-design/icons';
import { AppstoreOutlined } from '@ant-design/icons';
import type { ComponentType, CSSProperties, ReactNode } from 'react';

type IconProps = {
  className?: string;
  style?: CSSProperties;
  spin?: boolean;
  rotate?: number;
};

type IconComponent = ComponentType<IconProps>;

const iconRegistry = AntIcons as unknown as Record<string, unknown>;

const isIconComponent = (value: unknown): value is IconComponent => {
  if (typeof value === 'function') {
    return true;
  }
  return (
    typeof value === 'object' &&
    value !== null &&
    '$$typeof' in (value as Record<string, unknown>)
  );
};

/** 按名字解析 antd 图标；解析不到时回退 `AppstoreOutlined`。 */
export const resolveModuleIcon = (name?: string | null): ReactNode => {
  const key = (name ?? '').trim();
  if (key) {
    const candidate = iconRegistry[key];
    if (isIconComponent(candidate)) {
      const Icon = candidate;
      return <Icon />;
    }
  }
  return <AppstoreOutlined />;
};
