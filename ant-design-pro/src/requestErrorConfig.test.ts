import { history } from '@umijs/max';
import { message, notification } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { clearToken, setToken } from '@/services/bastion/client';
import { errorConfig } from './requestErrorConfig';

/**
 * 堡垒机前端错误处理用例。
 *
 * 与模板自带版本的差异：这里测的是**改造后的真实行为** —— 后端信封 message 优先、
 * 401 清 token 跳登录、403 提示权限、5xx 走通知、后端未启动给出明确指引、
 * 请求拦截器注入 `Authorization: Bearer <JWT>`。
 */
vi.mock('antd', () => ({
  message: {
    warning: vi.fn(),
    error: vi.fn(),
  },
  notification: {
    open: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock('@umijs/max', () => ({
  // client.ts 里 `export const rawRequest = request` 需要这个导出存在
  request: vi.fn(),
  getIntl: vi.fn(() => ({
    formatMessage: vi.fn(({ defaultMessage }) => defaultMessage),
  })),
  history: {
    location: { pathname: '/dashboard', search: '', hash: '' },
    replace: vi.fn(),
  },
}));

const historyMock = history as unknown as {
  location: { pathname: string; search: string; hash: string };
  replace: ReturnType<typeof vi.fn>;
};

/**
 * 造一个「带附加字段的错误对象」。
 *
 * 不能写成 `const e: Record<string, unknown> = new Error(...)` —— ``Error`` 没有索引签名，
 * tsc 会报 TS2322；而 errorHandler 的入参本身就是 ``any``，这里只需要能挂任意字段。
 */
const makeLooseError = (message: string): Record<string, unknown> =>
  new Error(message) as unknown as Record<string, unknown>;

const errorThrower = errorConfig.errorConfig?.errorThrower as (
  res: unknown,
) => void;
const errorHandler = errorConfig.errorConfig?.errorHandler as (
  error: unknown,
  opts: unknown,
) => void;

beforeEach(() => {
  vi.clearAllMocks();
  clearToken();
  historyMock.location = { pathname: '/dashboard', search: '', hash: '' };
});

describe('errorThrower（business error 转 BizError）', () => {
  it('success=false 时抛出 BizError 并带上 errorMessage', () => {
    expect(() => {
      errorThrower({
        success: false,
        data: null,
        errorCode: 400,
        errorMessage: '参数不合法',
        showType: 2,
      });
    }).toThrow('参数不合法');
  });

  it('success=true 时不抛错', () => {
    expect(() => {
      errorThrower({ success: true, data: { id: 1 } });
    }).not.toThrow();
  });

  it('抛出的 BizError 保留 errorCode / showType / data', () => {
    try {
      errorThrower({
        success: false,
        data: { detail: 'more info' },
        errorCode: 403,
        errorMessage: 'Forbidden',
        showType: 3,
      });
      throw new Error('应当抛出 BizError');
    } catch (error) {
      const err = error as {
        name: string;
        info: { errorCode: number; showType: number; data: unknown };
      };
      expect(err.name).toBe('BizError');
      expect(err.info.errorCode).toBe(403);
      expect(err.info.showType).toBe(3);
      expect(err.info.data).toEqual({ detail: 'more info' });
    }
  });
});

describe('errorHandler（BizError 分支）', () => {
  const bizError = (showType: number, errorMessage = '业务错误') => {
    const error = makeLooseError(errorMessage);
    error.name = 'BizError';
    error.info = { errorCode: 1001, errorMessage, showType };
    return error;
  };

  it('skipErrorHandler 时原样抛出', () => {
    expect(() => {
      errorHandler(new Error('Test error'), { skipErrorHandler: true });
    }).toThrow('Test error');
  });

  it('SILENT 不打扰用户', () => {
    errorHandler(bizError(0), {});
    expect(message.warning).not.toHaveBeenCalled();
    expect(message.error).not.toHaveBeenCalled();
    expect(notification.open).not.toHaveBeenCalled();
  });

  it('WARN_MESSAGE 走 warning', () => {
    errorHandler(bizError(1, '注意'), {});
    expect(message.warning).toHaveBeenCalledWith('注意');
  });

  it('ERROR_MESSAGE 走 error', () => {
    errorHandler(bizError(2, '出错了'), {});
    expect(message.error).toHaveBeenCalledWith('出错了');
  });

  it('NOTIFICATION 走通知', () => {
    errorHandler(bizError(3, '通知内容'), {});
    expect(notification.open).toHaveBeenCalledWith({
      title: 1001,
      description: '通知内容',
    });
  });

  it('REDIRECT 跳登录页并带上回跳地址', () => {
    historyMock.location = {
      pathname: '/audit/logs',
      search: '?page=2',
      hash: '',
    };
    errorHandler(bizError(9), {});
    expect(historyMock.replace).toHaveBeenCalledWith(
      '/user/login?redirect=%2Faudit%2Flogs%3Fpage%3D2',
    );
  });
});

describe('errorHandler（HTTP 分支）', () => {
  const httpError = (status: number, data: unknown = {}) => {
    const error = makeLooseError('http');
    error.response = { status, data };
    return error;
  };

  it('401 提示后清 token 并跳登录', () => {
    setToken('jwt-abc');
    errorHandler(httpError(401, { message: '登录状态已失效' }), {});
    expect(message.error).toHaveBeenCalledWith('登录状态已失效');
    expect(historyMock.replace).toHaveBeenCalled();
    expect(localStorage.getItem('bastion_token')).toBeNull();
  });

  it('403 展示后端权限提示', () => {
    errorHandler(httpError(403, { message: '当前账号没有该操作权限' }), {});
    expect(message.error).toHaveBeenCalledWith('当前账号没有该操作权限');
  });

  it('后端信封 message 优先于通用文案（400）', () => {
    errorHandler(httpError(400, { message: '用户名或密码错误' }), {});
    expect(message.error).toHaveBeenCalledWith('用户名或密码错误');
  });

  it('没有 message 时回落 HTTP 状态文案', () => {
    errorHandler(httpError(418, {}), {});
    expect(message.error).toHaveBeenCalledWith('请求失败（HTTP 418）');
  });

  it('5xx 走 notification.error', () => {
    errorHandler(httpError(500, { message: '服务端异常：boom' }), {});
    expect(notification.error).toHaveBeenCalledWith({
      title: '服务端异常',
      description: '服务端异常：boom',
    });
  });

  it('后端未启动时给出可执行指引', () => {
    const error = makeLooseError('network');
    error.request = {};
    errorHandler(error, {});
    expect(message.error).toHaveBeenCalledWith(
      '后端无响应，请确认 Flask 服务已启动（python run.py）',
    );
  });

  it('未知错误给出兜底文案', () => {
    errorHandler(new Error('boom'), {});
    expect(message.error).toHaveBeenCalledWith('请求异常，请重试');
  });
});

describe('requestInterceptors（JWT 注入）', () => {
  const interceptor = errorConfig.requestInterceptors?.[0] as (config: {
    url?: string;
    headers?: Record<string, string>;
  }) => { url?: string; headers?: Record<string, string> };

  it('已登录时注入 Authorization 头', () => {
    setToken('jwt-token-1');
    const result = interceptor({ url: '/api/hosts' });
    expect(result.headers?.Authorization).toBe('Bearer jwt-token-1');
  });

  it('未登录时不动请求头', () => {
    const result = interceptor({ url: '/api/health' });
    expect(result.headers?.Authorization).toBeUndefined();
  });
});
