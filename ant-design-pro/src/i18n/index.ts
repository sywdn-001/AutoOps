/**
 * 界面文案的「中文即键」翻译层。
 *
 * 本项目的界面文案一直直接写在代码里（中文），为了支持中英双语又不把 2000 多条
 * 文案全部改造成 `intl.formatMessage({id})` 的键值对形式，这里采用**以中文原文为键**
 * 的做法：
 *
 *   t('账号管理')   // zh-CN → '账号管理'；en-US → 'Account management'
 *
 * - 中文模式（zh-CN）：`t()` 原样返回，行为与改造前完全一致，零风险；
 * - 英文模式（en-US）：查 `dict/en-US.json`，查不到就回退中文（漏译会显示中文，
 *   一眼能看出来，绝不会出现 `i18n.xxx` 这种裸键）；
 * - 含变量的句子用 `{name}` 占位：`t('共 {total} 条', { total })`。
 *
 * 语言切换走顶部栏的小地球（`components/RightContent/LangDropdown.tsx`），切换时
 * 调 `setLocale(locale, true)` 整页重载 —— `t()` 读的是 Umi 的全局 locale，
 * 不重载的话已经渲染好的页面不会自己变。
 */
import { getLocale } from '@umijs/max';
import dict from './dict/en-US.json';

const EN = dict as Record<string, string>;

/**
 * 当前语言。
 *
 * 优先用 Umi 的 `getLocale()`，但**必须能扛住测试环境**：单测里 `@umijs/max`
 * 常常被 `vi.mock` 掉，那里没有 `getLocale`，直接调用会整片炸掉。所以再兜一层
 * localStorage（Umi 切语言时会把 `umi_locale` 写进去），最后默认 zh-CN。
 */
function currentLocale(): string {
  try {
    const value = typeof getLocale === 'function' ? getLocale() : undefined;
    if (value) return value;
  } catch {
    // 测试里 mock 掉 @umijs/max 时会抛，忽略
  }
  try {
    return globalThis.localStorage?.getItem('umi_locale') || 'zh-CN';
  } catch {
    return 'zh-CN';
  }
}

/** 当前是否英文界面。 */
export const isEnglish = (): boolean => currentLocale() === 'en-US';

/**
 * 翻译一条中文文案。
 *
 * @param zh 中文原文（同时是词典的键）
 * @param vars 可选占位符，替换 `{key}`
 */
export function t(
  zh: string,
  vars?: Record<string, string | number | boolean | null | undefined>,
): string {
  const raw = isEnglish() ? (EN[zh] ?? zh) : zh;
  if (!vars) {
    return raw;
  }
  return raw.replace(/\{(\w+)\}/g, (whole, key: string) =>
    key in vars ? String(vars[key] ?? '') : whole,
  );
}

/** 有没有对应的英文译文（用于自检脚本，界面上不用）。 */
export const hasTranslation = (zh: string): boolean => Boolean(EN[zh]);

/** 看一眼词库规模（自检用）。 */
export const dictionarySize = (): number => Object.keys(EN).length;

/** 菜单/面包屑用：把路由里的中文字面量翻成当前语言。 */
export const menuName = (name?: string): string | undefined =>
  name ? t(name) : name;

/**
 * 整棵菜单数据递归翻译（ProLayout 的 `menuDataRender` 用）。
 * 不修改入参，返回新对象，避免污染路由缓存。
 */
export function translateMenuData<T extends { name?: string; children?: T[] }>(
  menuData: T[],
): T[] {
  return menuData.map((item) => ({
    ...item,
    name: menuName(item.name),
    ...(item.children
      ? { children: translateMenuData(item.children as T[]) }
      : {}),
  })) as T[];
}
