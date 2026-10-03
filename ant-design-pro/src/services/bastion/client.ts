/**
 * 堡垒机前端请求底座：JWT 存取、统一信封解包、通用 CRUD 工厂。
 *
 * 约定：所有对外函数返回**解包后的业务数据**（成功信封里的 data），
 * 失败时抛出的错误来自 umi request（HTTP 4xx/5xx）或 `success: false` 信封。
 */
import { request } from '@umijs/max';
import type { ApiData, ApiList, PageParams } from './types';

const TOKEN_KEY = 'bastion_token';

export const getToken = (): string =>
  typeof localStorage === 'undefined' ? '' : localStorage.getItem(TOKEN_KEY) || '';

export const setToken = (token: string): void => {
  localStorage.setItem(TOKEN_KEY, token);
};

export const clearToken = (): void => {
  localStorage.removeItem(TOKEN_KEY);
};

/** 列表请求：把 ProTable 的 current/pageSize 统一成后端的 page/pageSize */
export const pageParams = (params: PageParams = {}): PageParams => {
  const { current, pageSize, ...rest } = params as PageParams & {
    current?: number;
    pageSize?: number;
  };
  return {
    ...rest,
    page: current ?? params.page ?? 1,
    pageSize: pageSize ?? params.pageSize ?? 20,
  };
};

/** 解包单条/操作信封 */
export async function unwrap<T>(promise: Promise<ApiData<T>>): Promise<T> {
  const body = await promise;
  return body?.data as T;
}

/** 通用 REST 资源客户端（列表 / 详情 / 新增 / 修改 / 删除） */
export function createResource<T, P = Record<string, unknown>>(base: string) {
  return {
    list: async (params: PageParams = {}): Promise<ApiList<T>> =>
      request<ApiList<T>>(base, { method: 'GET', params: pageParams(params) }),
    get: async (id: number | string): Promise<T> =>
      unwrap(request<ApiData<T>>(`${base}/${id}`, { method: 'GET' })),
    create: async (data: P): Promise<T> =>
      unwrap(request<ApiData<T>>(base, { method: 'POST', data })),
    update: async (id: number | string, data: Partial<P>): Promise<T> =>
      unwrap(request<ApiData<T>>(`${base}/${id}`, { method: 'PUT', data })),
    remove: async (id: number | string): Promise<void> => {
      await unwrap(request<ApiData<null>>(`${base}/${id}`, { method: 'DELETE' }));
    },
  };
}

/** 直接返回信封（需要读取 message / total / extra 字段时用） */
export const rawRequest = request;
export { unwrap as unwrapData };
