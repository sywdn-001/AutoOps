/**
 * 扩展模块（`.mod.ops_ext`）API 与归一化工具。
 *
 * 契约见 `docs/modules/CONTRACT.md`：后端返回的模块对象/导航项字段名是约定好的，
 * 但为了兼容「旧版本后端少下发字段」和 `upload-check` 只返回 description 的情况，
 * 这里统一做一层容错归一化，页面只消费归一化后的类型。
 */
import { request } from '@umijs/max';
import { isEnglish } from '@/i18n';
import { unwrap } from './client';
import type { ApiData } from './types';
import type {
  ModuleImage,
  ModuleItem,
  ModuleNavEntry,
  ModuleNavItem,
  ModulePreview,
  ModulePreviewIssue,
  ModuleSetting,
  ModuleText,
} from './types';

/** 实验性模块必须逐字输入、后端二次校验的确认语（契约 §2）。 */
export const EXPERIMENTAL_ACK_PHRASE = '我知道这是实验性功能，使用不当的话也许会造成系统损毁';

type UnknownRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is UnknownRecord =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const asRecord = (value: unknown): UnknownRecord => (isRecord(value) ? value : {});

const asString = (value: unknown): string | undefined => {
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    return String(value);
  }
  return undefined;
};

const asNumber = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined;

const asFlag = (value: unknown): boolean => {
  if (typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') {
    return value !== 0;
  }
  if (typeof value === 'string') {
    return ['1', 'true', 'yes', 'on', 'y', 't'].includes(value.trim().toLowerCase());
  }
  return false;
};

const asStringArray = (value: unknown): string[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => asString(item))
    .filter((item): item is string => Boolean(item && item.trim() !== ''));
};

/** 覆盖列表响应可能是数组、`{items}`、`{modules}` 的几种形态。 */
const asList = (value: unknown): unknown[] => {
  if (Array.isArray(value)) {
    return value;
  }
  if (isRecord(value)) {
    for (const key of ['items', 'list', 'modules', 'nav']) {
      if (Array.isArray(value[key])) {
        return value[key] as unknown[];
      }
    }
  }
  return [];
};

const toModuleText = (value: unknown, fallback = ''): ModuleText => {
  if (typeof value === 'string') {
    return value.trim() || fallback;
  }
  if (isRecord(value)) {
    const zh = asString(value.zh) ?? '';
    const en = asString(value.en) ?? '';
    if (zh || en) {
      return { zh: zh || en, en: en || zh };
    }
  }
  return fallback;
};

/** 按当前界面语言取模块双语文本；后端也可能直接下发字符串。 */
export const moduleText = (text: ModuleText | null | undefined, fallback = ''): string => {
  if (text === null || text === undefined) {
    return fallback;
  }
  if (typeof text === 'string') {
    return text.trim() || fallback;
  }
  const zh = (text.zh ?? '').trim();
  const en = (text.en ?? '').trim();
  return (isEnglish() ? en || zh : zh || en) || fallback;
};

/** upload-check 结果暂存到 localStorage 的 key 前缀；预览窗口靠 hash 里的 token 取用。 */
export const MODULE_PREVIEW_STORAGE_PREFIX = 'module-preview:';

/** 预览窗口安装成功后写入的信号前缀：主窗口监听 storage 事件刷新列表。 */
export const MODULE_INSTALLED_STORAGE_PREFIX = 'module-installed:';

export const modulePreviewStorageKey = (token: string): string =>
  `${MODULE_PREVIEW_STORAGE_PREFIX}${token}`;

export const createModulePreviewToken = (): string => {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return `mp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
};

export const writeModulePreview = (
  token: string,
  preview: ModulePreview,
): boolean => {
  try {
    window.localStorage.setItem(
      modulePreviewStorageKey(token),
      JSON.stringify(preview),
    );
    return true;
  } catch {
    return false;
  }
};

export const readModulePreview = (token: string): ModulePreview | null => {
  try {
    const raw = window.localStorage.getItem(modulePreviewStorageKey(token));
    return raw ? normalizePreview(JSON.parse(raw)) : null;
  } catch {
    return null;
  }
};

/**
 * 通知同源的主窗口「这个模块装好了」（storage 事件只在其它窗口写入时触发）。
 * 写失败不影响安装结果，返回是否写入成功。
 */
export const notifyModuleInstalled = (moduleId: string): boolean => {
  try {
    window.localStorage.setItem(
      `${MODULE_INSTALLED_STORAGE_PREFIX}${moduleId}`,
      String(Date.now()),
    );
    return true;
  } catch {
    return false;
  }
};

/** 预览阶段的静态资源地址（后端按 preview token 提供短期有效的 images/web 资源）。 */
export const modulePreviewAssetUrl = (
  token: string,
  path: string,
): string => {
  const relative = (path ?? '')
    .trim()
    .replace(/^\/+/, '')
    .replace(/^(\.\/)+/, '');
  const encoded = relative
    .split('/')
    .filter((segment) => segment !== '')
    .map((segment) => encodeURIComponent(segment))
    .join('/');
  return `/api/modules/preview/${encodeURIComponent(token)}/asset/${encoded}`;
};

/** 预览图片地址：优先用后端下发的短期 URL，缺失时按 token + 包内路径兜底。 */
export const modulePreviewImageUrl = (
  token: string,
  image: ModuleImage,
): string => {
  if (image.url) {
    return image.url;
  }
  return modulePreviewAssetUrl(token, image.path);
};

/** 把 nav.path 补成最终 URL（后端正常已带 `/m/<id>` 前缀，这里只兜底）。 */
const normalizeModulePath = (value: unknown, moduleId: string): string => {
  const raw = (asString(value) ?? '').trim().replace(/\\/g, '/');
  if (!raw) {
    return moduleId ? `/m/${moduleId}/` : '/';
  }
  if (raw.startsWith('/m/')) {
    return raw;
  }
  const absolute = raw.startsWith('/') ? raw : `/${raw}`;
  return moduleId ? `/m/${moduleId}${absolute}` : absolute;
};

const normalizeImages = (value: unknown): ModuleImage[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  const images: ModuleImage[] = [];
  for (const item of value) {
    if (typeof item === 'string') {
      if (item.trim()) {
        images.push({ path: item });
      }
      continue;
    }
    const record = asRecord(item);
    const path = asString(record.path) ?? '';
    if (!path.trim()) {
      continue;
    }
    const url = asString(record.url);
    images.push(url ? { path, url } : { path });
  }
  return images;
};

const normalizeNavItem = (
  value: unknown,
  fallback: { moduleId?: string; moduleName?: ModuleText },
): ModuleNavItem => {
  const record = asRecord(value);
  const moduleId = asString(record.moduleId) ?? fallback.moduleId ?? '';
  const moduleName =
    record.moduleName !== undefined
      ? toModuleText(record.moduleName, moduleId)
      : (fallback.moduleName ?? toModuleText(record.name, moduleId));
  return {
    moduleId,
    moduleName,
    key: asString(record.key) ?? moduleId,
    title: toModuleText(record.title ?? record.name, moduleId),
    path: normalizeModulePath(record.path, moduleId),
    entry: asString(record.entry),
    type: record.type === 'js' ? 'js' : 'iframe',
    icon: asString(record.icon),
    permission: asString(record.permission),
    loaded: typeof record.loaded === 'boolean' ? record.loaded : undefined,
  };
};

const normalizeSettings = (value: unknown): ModuleSetting[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  const settings: ModuleSetting[] = [];
  for (const item of value) {
    const record = asRecord(item);
    const key = asString(record.key);
    if (!key) {
      continue;
    }
    const type = record.type === 'int' || record.type === 'bool' ? record.type : 'string';
    settings.push({
      key,
      rawKey: asString(record.rawKey),
      label: toModuleText(record.label, key),
      type,
      default: asString(record.default),
      min: asNumber(record.min) ?? null,
      max: asNumber(record.max) ?? null,
      description:
        record.description === undefined ? undefined : toModuleText(record.description),
      group: asString(record.group),
    });
  }
  return settings;
};

/** 后端 `issueItems` / `warningItems`：`{key, params}`，`key` 是中文模板 */
const normalizeIssueItems = (raw: unknown): ModulePreviewIssue[] => {
  if (!Array.isArray(raw)) {
    return [];
  }
  const items: ModulePreviewIssue[] = [];
  for (const item of raw) {
    const record = asRecord(item);
    const key = asString(record.key);
    if (!key) {
      continue;
    }
    const paramsRecord = asRecord(record.params);
    const params: Record<string, string> = {};
    for (const [name, value] of Object.entries(paramsRecord)) {
      params[name] = String(value);
    }
    items.push({ key, params });
  }
  return items;
};

const normalizeModule = (raw: unknown): ModuleItem => {  const record = asRecord(raw);
  const id = asString(record.id) ?? '';
  const name = toModuleText(record.name, id);
  const tags = asStringArray(record.tags);
  const experimental =
    asFlag(record.experimental) || tags.some((tag) => tag.trim().toLowerCase() === 'experimental');
  const author = asString(record.author) ?? '';
  const official = asFlag(record.official) || author.trim().toLowerCase() === 'autoops';
  const entry = asRecord(record.entry);
  const nav = (Array.isArray(record.nav) ? record.nav : []).map((item) =>
    normalizeNavItem(item, { moduleId: id, moduleName: name }),
  );

  return {
    id,
    name,
    version: asString(record.version) ?? '',
    author,
    official,
    description: toModuleText(record.description),
    tags,
    experimental,
    icon: asString(record.icon),
    license: asString(record.license),
    homepage: asString(record.homepage),
    minBastionVersion: asString(record.minBastionVersion),
    images: normalizeImages(record.images),
    entry: { web: asString(entry.web) ?? 'web/index.html' },
    nav,
    settings: normalizeSettings(record.settings),
    permissions: asStringArray(record.permissions),
    requires: asStringArray(record.requires),
    reserved: typeof record.reserved === 'boolean' ? record.reserved : undefined,
    enabled: asFlag(record.enabled),
    loaded: asFlag(record.loaded),
    installedAt: asString(record.installedAt),
    updatedAt: asString(record.updatedAt),
    size: asNumber(record.size) ?? 0,
    errors: asStringArray(record.errors),
  };
};

const normalizePreview = (raw: unknown): ModulePreview => {
  const record = asRecord(raw);
  const module = normalizeModule(record);
  return {
    ...module,
    installable: record.installable === undefined ? true : asFlag(record.installable),
    issues: asStringArray(record.issues),
    warnings: asStringArray(record.warnings),
    issueItems: normalizeIssueItems(record.issueItems),
    warningItems: normalizeIssueItems(record.warningItems),
    readme: asString(record.readme) ?? '',
    readmeName: asString(record.readmeName) ?? '',
    readmeEn: asString(record.readmeEn) ?? '',
    readmeEnName: asString(record.readmeEnName) ?? '',
    fileCount: asNumber(record.fileCount),
    folders: asStringArray(record.folders),
    previewToken: asString(record.previewToken),
  };
};

/**
 * `/api/modules/nav` 的响应。契约规定是**扁平 nav 项数组**；这里也容忍
 * 「模块对象数组 + 每项 nav[]」的旧形态，避免后端调整时整块菜单消失。
 */
const normalizeNavEntries = (raw: unknown): ModuleNavEntry[] => {
  const flat: ModuleNavItem[] = [];
  for (const item of asList(raw)) {
    const record = asRecord(item);
    const groupedNav = Array.isArray(record.nav) ? record.nav : [];
    if (groupedNav.length > 0) {
      const moduleId = asString(record.id) ?? asString(record.moduleId) ?? '';
      const moduleName = toModuleText(record.name ?? record.moduleName, moduleId);
      for (const entry of groupedNav) {
        flat.push(normalizeNavItem(entry, { moduleId, moduleName }));
      }
      continue;
    }
    flat.push(normalizeNavItem(item, {}));
  }

  const seen = new Set<string>();
  const entries: ModuleNavEntry[] = [];
  for (const item of flat) {
    const moduleId = item.moduleId ?? '';
    if (!moduleId || !item.path || seen.has(`${moduleId}:${item.path}`)) {
      continue;
    }
    seen.add(`${moduleId}:${item.path}`);
    entries.push({
      ...item,
      moduleId,
      moduleName: item.moduleName ?? toModuleText(item.title, moduleId),
    });
  }
  return entries;
};

/** 把接口错误里的 HTTP 状态码取出来（宿主页区分 403/404 用）。 */
export const moduleErrorStatus = (error: unknown): number | undefined => {
  const status = (error as { response?: { status?: number } })?.response?.status;
  return typeof status === 'number' ? status : undefined;
};

/** 模块静态资源地址（契约 §6：免登录、no-cache）。 */
export const moduleAssetUrl = (moduleId: string, entry?: string): string => {
  const relative =
    (entry ?? '').trim().replace(/^\/+/, '') || 'web/index.html';
  const encoded = relative
    .split('/')
    .filter((segment) => segment !== '')
    .map((segment) => encodeURIComponent(segment))
    .join('/');
  return `/api/modules/${encodeURIComponent(moduleId)}/asset/${encoded}`;
};

/**
 * 按当前浏览器地址匹配 nav 项：精确匹配优先，其次取最长前缀
 * （例如 `/m/foo/` 与 `/m/foo/stats` 同时存在时，`/m/foo/stats` 胜出）。
 */
export const matchModuleNav = <T extends { path: string }>(
  nav: T[],
  pathname: string,
): T | undefined => {
  const normalize = (value: string): string => {
    const stripped = value.trim().replace(/\/+$/, '');
    return stripped || '/';
  };
  const target = normalize(pathname);
  let best: T | undefined;
  let bestLength = -1;
  for (const item of nav) {
    const path = normalize(item.path);
    if (path === target) {
      return item;
    }
    if (target.startsWith(`${path}/`) && path.length > bestLength) {
      best = item;
      bestLength = path.length;
    }
  }
  return best;
};

export const moduleApi = {
  /** 已安装模块列表（需要 `setting:view`）。 */
  list: async (): Promise<ModuleItem[]> => {
    const raw = await unwrap(
      request<ApiData<unknown>>('/api/modules', { method: 'GET' }),
    );
    return asList(raw).map((item) => normalizeModule(item));
  },

  /** 模块详情（需要 `setting:view`；宿主页无权限时改用 `nav()`）。 */
  detail: async (id: string): Promise<ModuleItem> => {
    const raw = await unwrap(
      request<ApiData<unknown>>(`/api/modules/${encodeURIComponent(id)}`, {
        method: 'GET',
        // 普通账号访问宿主页时 403 是预期分支，交给页面降级，不要弹全局错误
        skipErrorHandler: true,
      }),
    );
    return normalizeModule(raw);
  },

  /** 当前账号可见的模块导航（登录即可；未登录 401，无权时返回空）。 */
  nav: async (): Promise<ModuleNavEntry[]> => {
    const raw = await unwrap(
      request<ApiData<unknown>>('/api/modules/nav', {
        method: 'GET',
        // 导航只是附加能力：401/403 由调用方静默忽略，不弹全局提示
        skipErrorHandler: true,
      }),
    );
    return normalizeNavEntries(raw);
  },

  /** 安装前预览：只解析 description.json，不落盘、不执行（需要管理员）。 */
  uploadCheck: async (file: File): Promise<ModulePreview> => {
    const data = new FormData();
    data.append('file', file);
    return normalizePreview(
      await unwrap(
        request<ApiData<unknown>>('/api/modules/upload-check', {
          method: 'POST',
          data,
        }),
      ),
    );
  },

  /** 安装扩展包；实验性模块必须带逐字确认语 `ack`（需要管理员）。 */
  install: async (file: File, ack?: string): Promise<ModuleItem> => {
    const data = new FormData();
    data.append('file', file);
    if (ack !== undefined) {
      data.append('ack', ack);
    }
    return normalizeModule(
      await unwrap(
        request<ApiData<unknown>>('/api/modules/install', {
          method: 'POST',
          data,
        }),
      ),
    );
  },

  /** 在预览窗口里安装：用 upload-check 产生的 preview token 落盘安装。 */
  installPreview: async (
    previewToken: string,
    ack?: string,
  ): Promise<ModuleItem> => {
    return normalizeModule(
      await unwrap(
        request<ApiData<unknown>>(
          `/api/modules/preview/${encodeURIComponent(previewToken)}/install`,
          {
            method: 'POST',
            // 后端用 request.get_json() 读 ack；非实验性模块也显式传空串。
            data: { ack: ack ?? '' },
          },
        ),
      ),
    );
  },

  /** 启用模块（需要管理员）。 */
  enable: async (id: string): Promise<ModuleItem> => {
    return normalizeModule(
      await unwrap(
        request<ApiData<unknown>>(
          `/api/modules/${encodeURIComponent(id)}/enable`,
          { method: 'POST' },
        ),
      ),
    );
  },

  /** 禁用模块（需要管理员）。 */
  disable: async (id: string): Promise<ModuleItem> => {
    return normalizeModule(
      await unwrap(
        request<ApiData<unknown>>(
          `/api/modules/${encodeURIComponent(id)}/disable`,
          { method: 'POST' },
        ),
      ),
    );
  },

  /** 卸载模块（需要管理员；后端要求先 disable，页面会先禁用再删除）。 */
  remove: async (id: string): Promise<void> => {
    await unwrap(
      request<ApiData<unknown>>(`/api/modules/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }),
    );
  },

  /** 最近 200 行模块日志（需要 `setting:view`），新的在后。 */
  logs: async (id: string): Promise<string[]> => {
    const raw = await unwrap(
      request<ApiData<unknown>>(
        `/api/modules/${encodeURIComponent(id)}/logs`,
        { method: 'GET' },
      ),
    );
    return asStringArray(raw);
  },
};
