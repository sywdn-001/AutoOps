/**
 * 堡垒机后台路由与菜单。
 *
 * - `name` 用中文字面量：ProLayout 的 formatMessage 找不到 key 时会回落成字面量，
 *   这样即使切换语言也不会出现 `menu.xxx` 这种裸 key。
 * - `access` 对应 src/access.ts 里的开关，无权限时菜单自动隐藏；
 *   后端每个接口还会再校验一遍，前端只是体验层。
 */
export default [
  {
    path: '/user',
    layout: false,
    routes: [
      {
        name: '登录',
        path: '/user/login',
        component: './user/login',
      },
    ],
  },
  {
    path: '/dashboard',
    name: '概览',
    icon: 'DashboardOutlined',
    access: 'canDashboard',
    component: './bastion/dashboard',
  },
  {
    path: '/ai',
    name: 'AI 运维',
    icon: 'RobotOutlined',
    access: 'canAiUse',
    component: './bastion/ai',
  },
  {
    path: '/terminal',
    name: '网页终端',
    icon: 'CodeOutlined',
    // 字符终端与 Windows 远程桌面共用一个入口页，所以两种权限有一个就显示菜单
    access: 'canTerminalLauncher',
    component: './bastion/terminal',
  },
  {
    // 终端控制台：资产列表点「连接」后**新开标签页**进来，一个标签页一条会话，
    // 所以不带后台框架（layout: false），也不进菜单。
    path: '/terminal/console',
    name: '终端控制台',
    access: 'canTerminalUse',
    layout: false,
    hideInMenu: true,
    component: './bastion/terminal/console',
  },
  {
    // 文件管理器控制台：终端窗口工具条点「文件管理」后**新开标签页**进来，
    // 一个标签页一条 SFTP 会话，所以同样不带后台框架、不进菜单。
    path: '/files/console',
    name: '文件管理器',
    access: 'canFileUse',
    layout: false,
    hideInMenu: true,
    component: './bastion/files/console',
  },
  {
    // 远程桌面控制台：网页终端列表点「连接」后**新开标签页**进来，一个标签页一条 RDP 会话，
    // 同样不带后台框架、不进菜单。
    // 注意：远程桌面没有独立的列表菜单 —— Windows 主机和 Linux 主机一起列在「网页终端」里，
    // 点「连接」时按主机的 protocol 决定弹哪种窗口（见 src/pages/bastion/terminal/index.tsx）。
    path: '/rdp/console',
    name: '远程桌面控制台',
    access: 'canRdpUse',
    layout: false,
    hideInMenu: true,
    component: './bastion/rdp/console',
  },
  {
    // 录像回放：审计中心「远程桌面录像」列表点「回看」后**弹独立窗口**进来，一个窗口一段录像。
    // 同样不带后台框架、不进菜单（票据在页面里换，URL 上的参数不作数）。
    path: '/rdp/play',
    name: '录像回放',
    access: 'canRdpRecordings',
    layout: false,
    hideInMenu: true,
    component: './bastion/rdp/play',
  },
  {
    path: '/assets',
    name: '资产管理',
    icon: 'CloudServerOutlined',
    access: 'canAssetView',
    routes: [
      {
        path: '/assets',
        redirect: '/assets/hosts',
      },
      {
        name: '主机列表',
        path: '/assets/hosts',
        component: './bastion/hosts',
      },
      {
        name: '主机分组',
        path: '/assets/groups',
        access: 'canGroupView',
        component: './bastion/host-groups',
      },
    ],
  },
  {
    path: '/identity',
    name: '身份与权限',
    icon: 'TeamOutlined',
    access: 'canIdentityView',
    routes: [
      {
        path: '/identity',
        redirect: '/identity/users',
      },
      {
        name: '用户管理',
        path: '/identity/users',
        component: './bastion/users',
      },
      {
        name: '角色管理',
        path: '/identity/roles',
        component: './bastion/roles',
      },
    ],
  },
  {
    path: '/grants',
    name: '访问授权',
    icon: 'SafetyCertificateOutlined',
    access: 'canGrantView',
    component: './bastion/grants',
  },
  {
    path: '/policies',
    name: '命令策略',
    icon: 'FileProtectOutlined',
    access: 'canPolicyView',
    component: './bastion/policies',
  },
  {
    // 文件策略：命令策略管「能执行什么命令」，文件策略管「能碰哪些路径、能做什么操作」。
    path: '/file-policies',
    name: '文件策略',
    icon: 'FolderOpenOutlined',
    access: 'canFilePolicyView',
    component: './bastion/files/policies',
  },
  {
    path: '/audit',
    name: '审计中心',
    icon: 'AuditOutlined',
    access: 'canAuditModule',
    routes: [
      {
        path: '/audit',
        redirect: '/audit/sessions',
      },
      {
        name: '会话记录',
        path: '/audit/sessions',
        component: './bastion/sessions',
      },
      {
        name: '命令记录',
        path: '/audit/commands',
        component: './bastion/commands',
      },
      {
        name: '文件记录',
        path: '/audit/files',
        component: './bastion/files/audits',
      },
      {
        // AI 对话与工具调用审计：用户说了什么、AI 回了什么、调了哪些工具、
        // 敏感操作是谁批的，全部可查（数据来自 ai_conversations / ai_messages / ai_tool_calls）。
        name: 'AI 对话审计',
        path: '/audit/ai',
        access: 'canAiView',
        component: './bastion/ai/audits',
      },
      {
        // 远程桌面录像：Windows 资产的远程操作全程录像，随时回看（需求⑤）。
        // 审计人员看得到所有人的录像，普通用户只看得到自己上传的那些（后端按权限过滤）。
        name: '远程桌面录像',
        path: '/audit/recordings',
        access: 'canRdpRecordings',
        component: './bastion/rdp/recordings',
      },
      {
        name: '操作日志',
        path: '/audit/logs',
        access: 'canAuditView',
        component: './bastion/audits',
      },
    ],
  },
  {
    path: '/system',
    name: '系统设置',
    icon: 'SettingOutlined',
    access: 'canSettingView',
    routes: [
      {
        path: '/system',
        redirect: '/system/settings',
      },
      {
        name: '参数设置',
        path: '/system/settings',
        component: './bastion/settings',
      },
      {
        name: 'SSH 网关',
        path: '/system/gateway',
        component: './bastion/gateway',
      },
    ],
  },
  {
    // 模板自带的 /account/center（假文章/应用/项目）与 /account/settings（调用后端不存在的
    // Pro 接口，会弹「接口不存在」）已替换为堡垒机自己的「个人设置」：展示身份信息 + 改密码。
    path: '/account',
    name: '个人设置',
    icon: 'UserOutlined',
    hideInMenu: true,
    routes: [
      {
        path: '/account',
        redirect: '/account/settings',
      },
      {
        name: '个人设置',
        path: '/account/settings',
        component: './bastion/account',
      },
    ],
  },
  {
    path: '/',
    redirect: '/dashboard',
  },
  {
    path: '*',
    layout: false,
    component: './exception/404',
  },
];
