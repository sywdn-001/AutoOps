/**
 * ComputerUse 部署日志流（SSE）的测试：帧解析 + 订阅地址拼接 + `openLogStream` 的接线。
 *
 * 为什么值得单独测：日志台是第 3 步唯一的「事实来源」，后端四种帧（hello/log/status/end）
 * 一旦解析错，界面就会卡在旧阶段、进度条不动、失败原因显示不出来。这些解析全是纯函数，
 * 用假 `EventSource` 就能覆盖，不需要真实后端与真实网络。
 */
import { describe, expect, it, vi } from 'vitest';
import {
  buildLogStreamUrl,
  normalizeLogLevel,
  openLogStream,
  parseLogStreamMessage,
} from '@/services/bastion/computeruse';

/** 假 EventSource：只记下地址，方便断言订阅参数是 `?since=&token=` */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  /** 测试里手动推一帧进来 */
  emit(data: string) {
    this.onmessage?.({ data } as MessageEvent<string>);
  }
}

describe('normalizeLogLevel', () => {
  it('只认后端四个级别，其它（含空值）一律当 info', () => {
    expect(normalizeLogLevel('info')).toBe('info');
    expect(normalizeLogLevel('success')).toBe('success');
    expect(normalizeLogLevel('WARNING')).toBe('warning');
    expect(normalizeLogLevel(' Error ')).toBe('error');
    expect(normalizeLogLevel('debug')).toBe('info');
    expect(normalizeLogLevel('')).toBe('info');
    expect(normalizeLogLevel(null)).toBe('info');
    expect(normalizeLogLevel(undefined)).toBe('info');
  });
});

describe('buildLogStreamUrl', () => {
  it('基本形态是指向后端日志流的绝对路径', () => {
    expect(buildLogStreamUrl(7, undefined, 'jwt-token')).toBe(
      '/api/computer-use/deployments/7/logs?token=jwt-token',
    );
  });

  it('since > 0 时带上，用于断线重连补日志', () => {
    expect(buildLogStreamUrl(7, 12, 'jwt')).toBe(
      '/api/computer-use/deployments/7/logs?since=12&token=jwt',
    );
    // since 为 0/负数/非数字时不带（从第一行重放）
    expect(buildLogStreamUrl(7, 0, 'jwt')).toBe(
      '/api/computer-use/deployments/7/logs?token=jwt',
    );
    expect(buildLogStreamUrl(7, -3, 'jwt')).toBe(
      '/api/computer-use/deployments/7/logs?token=jwt',
    );
  });

  it('token 里的特殊字符会被转义（JWT 只有点号，但别让它破坏查询串）', () => {
    expect(buildLogStreamUrl(7, undefined, 'a.b+c/d=')).toBe(
      '/api/computer-use/deployments/7/logs?token=a.b%2Bc%2Fd%3D',
    );
  });

  it('没有 token 时只留 since，不会拼出 ?token=', () => {
    expect(buildLogStreamUrl(7, 5, '')).toBe(
      '/api/computer-use/deployments/7/logs?since=5',
    );
  });
});

describe('parseLogStreamMessage', () => {
  it('hello 帧取出整条部署记录', () => {
    const message = parseLogStreamMessage(
      JSON.stringify({
        type: 'hello',
        deployment: { id: 7, status: 'running', stage: 'prepare' },
      }),
    );
    expect(message?.type).toBe('hello');
    if (message?.type === 'hello') {
      expect(message.deployment.id).toBe(7);
      expect(message.deployment.stage).toBe('prepare');
    }
  });

  it('hello 帧缺 deployment 时丢掉（返回 null），不炸界面', () => {
    expect(parseLogStreamMessage(JSON.stringify({ type: 'hello' }))).toBeNull();
  });

  it('log 帧逐字段解析，后端给的时间原样用', () => {
    const message = parseLogStreamMessage(
      JSON.stringify({
        type: 'log',
        event: {
          seq: 12,
          ts: 1730000000,
          time: '12:00:01',
          level: 'success',
          stage: 'upload',
          stageLabel: '上传 Python 安装包到 C:\\Installer',
          message: '上传完成',
          progress: 28,
        },
      }),
    );
    expect(message).toEqual({
      type: 'log',
      event: {
        seq: 12,
        ts: 1730000000,
        time: '12:00:01',
        level: 'success',
        stage: 'upload',
        stageLabel: '上传 Python 安装包到 C:\\Installer',
        message: '上传完成',
        progress: 28,
      },
    });
  });

  it('log 帧缺 time 时用 ts 拼出 HH:MM:SS', () => {
    const ts = new Date(2025, 0, 2, 9, 8, 7).getTime() / 1000;
    const message = parseLogStreamMessage(
      JSON.stringify({ type: 'log', event: { seq: 1, ts, message: '开始' } }),
    );
    expect(message?.type).toBe('log');
    if (message?.type === 'log') {
      expect(message.event.time).toBe('09:08:07');
    }
  });

  it('log 帧完全没有时间与 ts 时给空串，交给展示层补占位符', () => {
    const message = parseLogStreamMessage(
      JSON.stringify({ type: 'log', event: { seq: 1, message: '开始' } }),
    );
    if (message?.type === 'log') {
      expect(message.event.time).toBe('');
    }
  });

  it('log 帧缺 level 时从消息里的 [error] 标记兜底', () => {
    const message = parseLogStreamMessage(
      JSON.stringify({
        type: 'log',
        event: { seq: 2, time: '12:00:02', message: '[error] 探活失败' },
      }),
    );
    if (message?.type === 'log') {
      expect(message.event.level).toBe('error');
    }
  });

  it('log 帧缺 seq/progress 时给安全默认值 0', () => {
    const message = parseLogStreamMessage(
      JSON.stringify({ type: 'log', event: { message: 'x' } }),
    );
    if (message?.type === 'log') {
      expect(message.event.seq).toBe(0);
      expect(message.event.progress).toBe(0);
      expect(message.event.level).toBe('info');
    }
  });

  it('log 帧没有 event 对象时丢掉', () => {
    expect(parseLogStreamMessage(JSON.stringify({ type: 'log' }))).toBeNull();
  });

  it('status 帧把字段归一化（缺字段给安全默认值）', () => {
    const message = parseLogStreamMessage(
      JSON.stringify({ type: 'status', id: 7, status: 'running', live: true }),
    );
    expect(message).toEqual({
      type: 'status',
      id: 7,
      status: 'running',
      stage: '',
      stageLabel: '',
      progress: 0,
      error: '',
      lastSeq: 0,
      live: true,
    });
  });

  it('status 的 status 只认已知值，未知值当 pending', () => {
    const message = parseLogStreamMessage(
      JSON.stringify({ type: 'status', status: 'weird' }),
    );
    if (message?.type === 'status') {
      expect(message.status).toBe('pending');
    }
  });

  it('end 帧只带终态', () => {
    expect(
      parseLogStreamMessage(JSON.stringify({ type: 'end', status: 'success' })),
    ).toEqual({ type: 'end', status: 'success' });
    expect(
      parseLogStreamMessage(
        JSON.stringify({ type: 'end', status: 'cancelled' }),
      ),
    ).toEqual({ type: 'end', status: 'cancelled' });
  });

  it('坏帧（空文本/非 JSON/未知类型/数组）全部返回 null', () => {
    expect(parseLogStreamMessage('')).toBeNull();
    expect(parseLogStreamMessage('   ')).toBeNull();
    expect(parseLogStreamMessage('{ 这不是 JSON')).toBeNull();
    expect(parseLogStreamMessage('null')).toBeNull();
    expect(parseLogStreamMessage('[]')).toBeNull();
    expect(parseLogStreamMessage('"文本"')).toBeNull();
    expect(parseLogStreamMessage(JSON.stringify({ type: 'ping' }))).toBeNull();
    // 后端 15 秒一次的注释心跳不该被当成事件
    expect(parseLogStreamMessage(': ping')).toBeNull();
  });
});

describe('openLogStream', () => {
  it('用拼好的地址建连接，把每帧交给回调，close 时关闭连接', () => {
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
    const received: string[] = [];
    const handle = openLogStream(7, (message) => {
      received.push(message.type);
    });
    const source = FakeEventSource.instances[0];
    expect(source.url).toBe('/api/computer-use/deployments/7/logs');

    source.emit(JSON.stringify({ type: 'end', status: 'success' }));
    source.emit('坏帧');
    expect(received).toEqual(['end']);

    handle.close();
    expect(source.closed).toBe(true);
    vi.unstubAllGlobals();
  });

  it('onError 透传给 EventSource 的 onerror', () => {
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
    const onError = vi.fn();
    openLogStream(9, () => {}, { since: 4, onError });
    const source = FakeEventSource.instances[0];
    expect(source.url).toBe('/api/computer-use/deployments/9/logs?since=4');
    source.onerror?.();
    expect(onError).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it('不传 onError 时也不会因为连接报错抛异常', () => {
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
    const handle = openLogStream(9, () => {});
    expect(() => FakeEventSource.instances[0].onerror?.()).not.toThrow();
    handle.close();
    vi.unstubAllGlobals();
  });
});
