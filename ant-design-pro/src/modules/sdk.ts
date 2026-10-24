/**
 * js 模式扩展模块的运行时 SDK。
 *
 * 模块的入口脚本（`<script type="module">`）不能 import 裸包名 `react` / `antd`，
 * 所以宿主页在注入脚本前把同一份运行时挂到 `window.AutoOpsSDK`，模块从参数里取：
 *
 * ```js
 * window.AutoOpsModules = window.AutoOpsModules ?? {};
 * window.AutoOpsModules['my-module'] = {
 *   mount(el, sdk) {
 *     sdk.ReactDOM.createRoot(el).render(sdk.React.createElement(...));
 *   },
 *   unmount(el) { ... },
 * };
 * ```
 *
 * React / ReactDOM 走静态 import（本来就是主包依赖）；antd 用动态 import 单独切 chunk，
 * 只有真的打开 js 模式模块时才加载，避免主包体积再涨 2MB+。
 */
import { history, request } from '@umijs/max';
import type { App } from 'antd';
import * as React from 'react';
import * as ReactDOM from 'react-dom';
import * as ReactDOMClient from 'react-dom/client';
import { isEnglish, t } from '@/i18n';
import { getToken } from '@/services/bastion/client';

type MessageApi = ReturnType<typeof App.useApp>['message'];

export type AutoOpsModuleHandle = {
  /** 挂载模块；返回的函数会在卸载时调用（可选，和 `unmount` 二选一即可） */
  mount?: (el: HTMLElement, sdk: AutoOpsSDK) => unknown;
  /** 卸载模块（宿主组件卸载时调用） */
  unmount?: (el?: HTMLElement) => void;
};

export type AutoOpsSDK = {
  React: typeof React;
  ReactDOM: typeof ReactDOM & typeof ReactDOMClient;
  antd: typeof import('antd');
  request: typeof request;
  getToken: typeof getToken;
  message: MessageApi;
  t: typeof t;
  /**
   * 当前界面是否为英文。每次调用都读最新语言，模块无需在语言切换后重挂。
   * 典型用法：`sdk.isEnglish() ? enText : zhText`（例如双语 README、模块自带的英文文档）。
   */
  isEnglish: typeof isEnglish;
  /** 宿主路由实例：模块内跳转用 `sdk.history.push('/system/settings')`，不整页刷新。 */
  history: typeof history;
};

declare global {
  interface Window {
    AutoOpsSDK?: AutoOpsSDK;
    AutoOpsModules?: Record<string, AutoOpsModuleHandle>;
  }
}

let sdkPromise: Promise<AutoOpsSDK> | undefined;

/**
 * 构建（并注入）模块共用的 SDK。`message` 每次刷新为当前 App 上下文里的实例，
 * 避免模块拿到已经卸载的 Provider 上的静态 message。
 */
export const ensureAutoOpsSdk = async (
  message: MessageApi,
): Promise<AutoOpsSDK> => {
  if (!sdkPromise) {
    const created = (async () => {
      const antd = await import('antd');
      const sdk: AutoOpsSDK = {
        React,
        ReactDOM: { ...ReactDOM, ...ReactDOMClient },
        antd,
        request,
        getToken,
        message,
        t,
        isEnglish,
        history,
      };
      window.AutoOpsSDK = sdk;
      return sdk;
    })();
    // 加载失败时不要把 rejected promise 永久缓存，下次还能重试
    sdkPromise = created.catch((error: unknown) => {
      sdkPromise = undefined;
      throw error;
    });
  }

  const sdk = await sdkPromise;
  sdk.message = message;
  window.AutoOpsSDK = sdk;
  return sdk;
};
