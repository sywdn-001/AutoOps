/**
 * 路由级鉴权（需求③：只有管理员可以添加机器、设置访问权限和其他设置）。
 *
 * 数据来源：`GET /api/currentUser` 返回的 `permissions` 权限码数组（超管为 `['*']`）。
 * 后端每个接口都有二次校验，前端这份只负责「菜单/按钮可见性」与体验，
 * 不是安全边界——绕过前端也拿不到数据。
 */
import type { CurrentUser } from '@/services/bastion/types';

export default function access(
  initialState: { currentUser?: API.CurrentUser } | undefined,
) {
  const user = initialState?.currentUser as CurrentUser | undefined;
  const permissions = user?.permissions ?? [];
  const has = (code: string) =>
    permissions.includes('*') || permissions.includes(code);

  return {
    /** 已登录 */
    isLogin: Boolean(user),
    /** 超管或 admin 角色 */
    canAdmin: Boolean(user?.isAdmin || user?.access === 'admin'),
    /** 概览 */
    canDashboard: has('dashboard:view'),
    /** 资产模块（有任意资产查看权限即显示菜单） */
    canAssetView: has('host:view') || has('account:view') || has('group:view'),
    /** 身份模块 */
    canIdentityView: has('user:view') || has('role:view'),
    /** 审计模块 */
    canAuditModule:
      has('session:view') ||
      has('command:view') ||
      has('file:use') ||
      has('filepolicy:view') ||
      has('audit:view'),
    /** 资产：主机 / 账号 / 分组 */
    canHostView: has('host:view'),
    canHostManage: has('host:manage'),
    canAccountView: has('account:view') || has('host:view'),
    canAccountManage: has('account:manage') || has('host:manage'),
    canGroupView: has('group:view') || has('host:view'),
    canGroupManage: has('group:manage') || has('host:manage'),
    /** 身份 */
    canUserView: has('user:view'),
    canUserManage: has('user:manage'),
    canRoleView: has('role:view') || has('user:view'),
    canRoleManage: has('role:manage') || has('user:manage'),
    /** 授权与策略 */
    canGrantView: has('grant:view'),
    canGrantManage: has('grant:manage'),
    canPolicyView: has('policy:view'),
    canPolicyManage: has('policy:manage'),
    /** 审计 */
    canSessionView: has('session:view'),
    canSessionViewAll: has('session:view_all'),
    canSessionReplay: has('session:replay'),
    canSessionTerminate: has('session:terminate'),
    canCommandView: has('command:view'),
    canCommandViewAll: has('command:view_all'),
    canAuditView: has('audit:view'),
    /** 网页终端 */
    canTerminalUse: has('terminal:use'),
    /** 文件管理器（SFTP）：能不能开窗口，与「能改哪些路径」由后端策略决定 */
    canFileUse: has('file:use'),
    /** 文件策略（哪条路径上能做哪些操作） */
    canFilePolicyView: has('filepolicy:view'),
    canFilePolicyManage: has('filepolicy:manage'),
    /** AI 运维：能不能进对话页、能不能看别人的对话与工具调用、能不能改 AI 配置 */
    canAiView: has('ai:view'),
    canAiUse: has('ai:use'),
    canAiViewAll: has('ai:view_all'),
    canAiManage: has('ai:manage'),
    /** 系统设置（仅管理员） */
    canSettingView: has('setting:view'),
    canSettingManage: has('setting:manage'),
  };
}
