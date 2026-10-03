import type { RequestOptions } from '@@/plugin-request/request';
import type { RequestConfig } from '@umijs/max';
import { getIntl, history } from '@umijs/max';
import { message, notification } from 'antd';
import { clearToken, getToken } from '@/services/bastion/client';

// 错误处理方案： 错误类型
enum ErrorShowType {
  SILENT = 0,
  WARN_MESSAGE = 1,
  ERROR_MESSAGE = 2,
  NOTIFICATION = 3,
  REDIRECT = 9,
}
// 与后端约定的响应数据格式
interface ResponseStructure {
  success: boolean;
  data: unknown;
  /** 堡垒机后端错误码，例如 NOT_FOUND / FORBIDDEN / WEAK_PASSWORD */
  code?: string;
  message?: string;
  // Pro 模板自带的字段名，保持兼容
  errorCode?: number;
  errorMessage?: string;
  showType?: ErrorShowType;
}

const loginPath = '/user/login';

/** 401 统一跳登录页（并带上回跳地址），避免每个页面各写一遍 */
function redirectToLogin() {
  clearToken();
  const { pathname, search, hash } = history.location;
  if (pathname === loginPath) return;
  history.replace(
    `${loginPath}?redirect=${encodeURIComponent(pathname + search + hash)}`,
  );
}

/**
 * @name 错误处理
 * pro 自带的错误处理， 可以在这里做自己的改动
 * @doc https://umijs.org/docs/max/request#配置
 */
export const errorConfig: RequestConfig = {
  // 错误处理： umi@3 的错误处理方案。
  errorConfig: {
    // 错误抛出
    errorThrower: (res) => {
      const { success, data, errorCode, errorMessage, showType } =
        res as unknown as ResponseStructure;
      if (!success) {
        const error: any = new Error(errorMessage);
        error.name = 'BizError';
        error.info = { errorCode, errorMessage, showType, data };
        throw error; // 抛出自制的错误
      }
    },
    // 错误接收及处理
    errorHandler: (error: any, opts: any) => {
      if (opts?.skipErrorHandler) throw error;
      // 我们的 errorThrower 抛出的错误。
      if (error.name === 'BizError') {
        const errorInfo: ResponseStructure | undefined = error.info;
        if (errorInfo) {
          const { errorMessage, errorCode } = errorInfo;
          switch (errorInfo.showType) {
            case ErrorShowType.SILENT:
              // do nothing
              break;
            case ErrorShowType.WARN_MESSAGE:
              message.warning(errorMessage);
              break;
            case ErrorShowType.ERROR_MESSAGE:
              message.error(errorMessage);
              break;
            case ErrorShowType.NOTIFICATION:
              notification.open({
                title: errorCode,
                description: errorMessage,
              });
              break;
            case ErrorShowType.REDIRECT:
              redirectToLogin();
              break;
            default:
              message.error(errorMessage);
          }
        }
      } else if (error.response) {
        // 堡垒机后端在 4xx/5xx 里返回 { success:false, message, code }，优先展示它的 message
        const status: number = error.response.status;
        const payload = (error.response.data ?? {}) as ResponseStructure;
        const text = payload.message || `请求失败（HTTP ${status}）`;
        if (status === 401) {
          message.error(text || '登录状态已失效，请重新登录');
          redirectToLogin();
        } else if (status === 403) {
          message.error(text || '当前账号没有该操作权限');
        } else if (status >= 500) {
          notification.error({ title: '服务端异常', description: text });
        } else {
          message.error(text);
        }
      } else if (typeof navigator !== 'undefined' && !navigator.onLine) {
        message.error(
          getIntl().formatMessage({
            id: 'app.request.offline',
            defaultMessage:
              'Network unavailable. Please check your connection and try again.',
          }),
        );
      } else if (error.request) {
        message.error('后端无响应，请确认 Flask 服务已启动（python run.py）');
      } else {
        message.error('请求异常，请重试');
      }
    },
  },

  // 请求拦截器：带上堡垒机 JWT
  requestInterceptors: [
    (config: RequestOptions) => {
      const token = getToken();
      if (token) {
        config.headers = {
          ...config.headers,
          Authorization: `Bearer ${token}`,
        };
      }
      return config;
    },
  ],

  // 响应拦截器
  responseInterceptors: [],
};
