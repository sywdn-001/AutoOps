/**
 * ComputerUse 侧边栏的单元测试。
 *
 * 只测**不依赖真实后端**的部分：就绪判定、收起状态持久化、工具/截图/探活结果解析、
 * SSE 事件归类与气泡合并；再用 mock 把接口与 AI 服务层挡住，验证组件级的几条硬约束：
 * - 没就绪（ready !== true）整块不渲染；
 * - 就绪后默认折叠成窄条，点一下就展开并落盘；
 * - 「看一眼屏幕」把 Screenshot 的图片内联出来，失败给可读错误；
 * - 面板里的 AI 对话必须带上 hostId，并把思考过程 / 工具调用过程展示出来；
 * - 工具清单不再占版面（按用户要求删掉），欢迎态的示例问题点一下就发出去。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/** umi request：测试里按 url 给固定信封，顺便记录调用参数 */
const requestMock = vi.fn();
/** AI 流式对话：只记录 payload，不真的开流 */
const streamAiChatMock = vi.fn();
const confirmAiOperationMock = vi.fn();

vi.mock('@umijs/max', () => ({
  request: (...args: unknown[]) => requestMock(...args),
}));

vi.mock('@/services/bastion/ai', () => ({
  streamAiChat: (...args: unknown[]) => streamAiChatMock(...args),
  confirmAiOperation: (...args: unknown[]) => confirmAiOperationMock(...args),
}));

// JsonCards 会拉进一堆表格渲染，这里换成轻量替身：被测的是面板逻辑而不是 JSON 渲染器
vi.mock('@/components/Bastion', () => ({
  JsonCards: ({ value }: { value: unknown }) => (
    <pre data-testid="json-cards">{JSON.stringify(value)}</pre>
  ),
}));

vi.mock('@ant-design/x-markdown', () => ({
  XMarkdown: ({ content }: { content?: string }) => (
    <div data-testid="xmarkdown">{content}</div>
  ),
}));

import ComputerUsePanel, {
  applyChatEvent,
  asNumber,
  asRecord,
  asText,
  buildProbeRequest,
  buildScreenshotRequest,
  buildToolsRequest,
  type ComputerUseStatus,
  classifyChatEvent,
  describeApiError,
  describeToolMeta,
  isComputerUseReady,
  isToolMutating,
  isToolReadOnly,
  PANEL_OPEN_KEY,
  parseProbeResult,
  parseScreenshotImages,
  parseScreenshotNote,
  parseScreenshotResult,
  parseToolList,
  readPanelOpen,
  resolveHostLabel,
  resolveMcpEndpoint,
  resolveToolCount,
  truncateText,
  WELCOME_HINTS,
  writePanelOpen,
} from './computerUsePanel';

const READY_STATUS: ComputerUseStatus = {
  hostId: 7,
  hostName: 'win-01',
  hostAddress: '192.168.1.20',
  ready: true,
  mcpUrl: 'http://192.168.1.20:8765/mcp',
  mcpHost: '192.168.1.20',
  mcpPort: 8765,
  toolCount: 2,
  tools: [
    {
      name: 'Screenshot',
      description: '截屏',
      mutating: false,
      observeOnly: true,
    },
    { name: 'Click', description: '点击', mutating: true, observeOnly: false },
  ],
};

const ok = (data: unknown) => Promise.resolve({ success: true, data });

beforeEach(() => {
  localStorage.clear();
  requestMock.mockReset();
  streamAiChatMock.mockReset();
  confirmAiOperationMock.mockReset();
  streamAiChatMock.mockResolvedValue(undefined);
  confirmAiOperationMock.mockResolvedValue({ approved: 1, rejected: 0 });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('就绪判定', () => {
  it('只有 ready === true 才认为部署成功', () => {
    expect(isComputerUseReady({ ready: true })).toBe(true);
    expect(isComputerUseReady({ ready: false })).toBe(false);
    expect(isComputerUseReady({})).toBe(false);
    expect(isComputerUseReady(null)).toBe(false);
    expect(isComputerUseReady(undefined)).toBe(false);
    // 字段是字符串 'true' 也不能当就绪（后端契约是布尔）
    expect(isComputerUseReady({ ready: 'true' } as never)).toBe(false);
  });
});

describe('收起状态持久化', () => {
  it('默认折叠，只有明确写了 1 才展开', () => {
    expect(readPanelOpen(localStorage)).toBe(false);
    writePanelOpen(true, localStorage);
    expect(readPanelOpen(localStorage)).toBe(true);
    writePanelOpen(false, localStorage);
    expect(readPanelOpen(localStorage)).toBe(false);
    expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0');
  });

  it('localStorage 抛异常时不炸（隐私模式）', () => {
    const broken = {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('denied');
      },
    };
    expect(readPanelOpen(broken as never)).toBe(false);
    expect(() => writePanelOpen(true, broken as never)).not.toThrow();
  });
});

describe('基础解析工具', () => {
  it('asText 只认非空字符串', () => {
    expect(asText(' a ')).toBe(' a ');
    expect(asText('   ')).toBeUndefined();
    expect(asText(1)).toBeUndefined();
    expect(asText(null)).toBeUndefined();
  });

  it('asNumber 过滤 NaN / Infinity', () => {
    expect(asNumber(12)).toBe(12);
    expect(asNumber(Number.NaN)).toBeUndefined();
    expect(asNumber(Number.POSITIVE_INFINITY)).toBeUndefined();
    expect(asNumber('12')).toBeUndefined();
  });

  it('asRecord 不把数组当对象', () => {
    expect(asRecord({ a: 1 })).toEqual({ a: 1 });
    expect(asRecord([1])).toBeUndefined();
    expect(asRecord(null)).toBeUndefined();
  });

  it('truncateText 只在超长时加省略号', () => {
    expect(truncateText('abc', 5)).toBe('abc');
    expect(truncateText('abcdef', 3)).toBe('abc…');
  });
});

describe('状态展示', () => {
  it('MCP 地址优先用接口给的 mcpUrl，其次用 host:port 拼', () => {
    expect(resolveMcpEndpoint(READY_STATUS)).toBe(
      'http://192.168.1.20:8765/mcp',
    );
    expect(resolveMcpEndpoint({ mcpHost: '10.0.0.5', mcpPort: 9000 })).toBe(
      'http://10.0.0.5:9000',
    );
    expect(resolveMcpEndpoint({ mcpHost: '10.0.0.5' })).toBe('');
    expect(resolveMcpEndpoint(null)).toBe('');
  });

  it('工具数量以实际拿到的清单为准', () => {
    expect(resolveToolCount(READY_STATUS, READY_STATUS.tools)).toBe(2);
    expect(resolveToolCount(READY_STATUS, [])).toBe(2);
    expect(resolveToolCount({ toolCount: 5 }, undefined)).toBe(5);
  });

  it('主机名缺失时退回地址，再退回 hostId', () => {
    expect(resolveHostLabel(7, READY_STATUS)).toBe('win-01');
    expect(resolveHostLabel(7, { hostAddress: '10.0.0.1' })).toBe('10.0.0.1');
    expect(resolveHostLabel(7, {})).toBe('主机 #7');
  });
});

describe('工具只读 / 会改机器状态', () => {
  it('没标 mutating 就当只读；标了 mutating 一律按写操作（矛盾数据也不放行）', () => {
    expect(isToolReadOnly({ name: 'Screenshot', observeOnly: true })).toBe(
      true,
    );
    expect(isToolReadOnly({ name: 'Screenshot', mutating: false })).toBe(true);
    // 后端只标「会改机器状态」的那些，其余不标 → 不标就是只读
    expect(isToolReadOnly({ name: 'Wait' })).toBe(true);
    expect(isToolReadOnly({ name: 'Click', mutating: true })).toBe(false);
    // 矛盾数据（既 observeOnly 又 mutating）按写操作处理，宁多要一次确认
    expect(
      isToolReadOnly({ name: 'Click', mutating: true, observeOnly: true }),
    ).toBe(false);
  });

  it('mutating === true 才是写操作', () => {
    expect(isToolMutating({ name: 'Click', mutating: true })).toBe(true);
    expect(isToolMutating({ name: 'Click' })).toBe(false);
  });

  it('展示信息给出中文标签与颜色', () => {
    expect(
      describeToolMeta({ name: 'Screenshot', observeOnly: true }).label,
    ).toBe('只读');
    expect(describeToolMeta({ name: 'Click', mutating: true }).label).toBe(
      '会改机器状态',
    );
    expect(describeToolMeta({ name: 'Click', mutating: true }).color).toBe(
      'orange',
    );
    // 没有 mutating 标记的工具按只读展示（后端只标写操作）
    expect(describeToolMeta({ name: 'Wait' }).label).toBe('只读');
    expect(describeToolMeta({ name: 'Wait' }).color).toBe('green');
  });
});

describe('接口请求构造', () => {
  it('看一眼屏幕用 Screenshot + 空参数', () => {
    expect(buildScreenshotRequest(7)).toEqual({
      url: '/api/computer-use/mcp/call',
      body: { hostId: 7, tool: 'Screenshot', arguments: {} },
    });
  });

  it('工具清单带 live=1，探活带 hostId', () => {
    expect(buildToolsRequest(7).params).toEqual({ hostId: 7, live: 1 });
    expect(buildProbeRequest(7).body).toEqual({ hostId: 7 });
  });
});

describe('Screenshot 结果解析', () => {
  const envelope = {
    success: true,
    data: {
      tool: 'Screenshot',
      text: '当前桌面：记事本已打开',
      images: [
        { mime: 'image/png', dataUrl: 'data:image/png;base64,AAA' },
        { mime: 'image/png', dataUrl: 'data:image/png;base64,BBB' },
      ],
    },
  };

  it('取出 dataUrl 列表与说明文字', () => {
    expect(parseScreenshotImages(envelope)).toEqual([
      'data:image/png;base64,AAA',
      'data:image/png;base64,BBB',
    ]);
    expect(parseScreenshotNote(envelope)).toBe('当前桌面：记事本已打开');
  });

  it('也吃得下没有再包一层 data 的返回', () => {
    const flat = {
      images: [{ dataUrl: 'data:image/png;base64,CCC' }],
      text: 'ok',
    };
    expect(parseScreenshotImages(flat)).toEqual(['data:image/png;base64,CCC']);
    expect(parseScreenshotNote(flat)).toBe('ok');
  });

  it('脏数据被丢掉，而不是渲染成破图', () => {
    const dirty = {
      data: { images: [{ dataUrl: '' }, { mime: 'image/png' }, null, 'x'] },
    };
    expect(parseScreenshotImages(dirty)).toEqual([]);
  });

  it('没有图片也没有说明 → 给可读错误', () => {
    const parsed = parseScreenshotResult({ data: { images: [] } });
    expect(parsed.error).toBeTruthy();
    expect(parsed.images).toEqual([]);
  });

  it('有图时不算错误', () => {
    expect(parseScreenshotResult(envelope).error).toBeUndefined();
  });
});

describe('工具清单 / 探活结果解析', () => {
  it('优先 data.items，其次 data.tools', () => {
    const items = parseToolList({
      data: {
        items: [{ name: 'Screenshot', observeOnly: true, description: '截屏' }],
        tools: [{ name: 'ignored' }],
      },
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toEqual({
      name: 'Screenshot',
      description: '截屏',
      mutating: false,
      observeOnly: true,
    });
    expect(parseToolList({ data: { tools: [{ name: 'Wait' }] } })[0].name).toBe(
      'Wait',
    );
  });

  it('缺名字的工具给兜底名，不会渲染成空标签', () => {
    expect(parseToolList({ data: { items: [{}] } })[0].name).toBe('工具 1');
  });

  it('探活成功带延时与工具数', () => {
    const result = parseProbeResult({
      data: { ok: true, latencyMs: 42, tools: [{ name: 'Screenshot' }] },
    });
    expect(result.ok).toBe(true);
    expect(result.toolCount).toBe(1);
    expect(result.text).toContain('42ms');
  });

  it('探活失败给后端原因，没有原因也给中文兜底', () => {
    expect(
      parseProbeResult({ data: { ok: false, error: 'connection refused' } })
        .text,
    ).toBe('connection refused');
    expect(parseProbeResult({ data: { ok: false } }).text).toContain(
      '没有响应',
    );
  });
});

describe('错误文案', () => {
  it('从后端信封里取 message', () => {
    const error = {
      response: { data: { success: false, message: '目标机没开机' } },
    };
    expect(describeApiError(error, '兜底')).toBe('目标机没开机');
  });

  it('取不到就给兜底，绝不渲染 [object Object]', () => {
    expect(describeApiError({}, '兜底')).toBe('兜底');
    const text = describeApiError({ foo: 1 }, '兜底');
    expect(text).toBe('兜底');
    expect(text).not.toContain('[object Object]');
  });
});

describe('SSE 事件归类', () => {
  it('start 带出 conversationId', () => {
    expect(classifyChatEvent({ type: 'start', conversationId: 9 })).toEqual({
      kind: 'start',
      conversationId: 9,
    });
  });

  it('content / reasoning 只认非空 delta', () => {
    expect(classifyChatEvent({ type: 'content', delta: '你好' })).toEqual({
      kind: 'content',
      delta: '你好',
    });
    expect(
      classifyChatEvent({ type: 'content', delta: '' }).delta,
    ).toBeUndefined();
    expect(classifyChatEvent({ type: 'reasoning', delta: '想一下' }).kind).toBe(
      'reasoning',
    );
  });

  it('tool_call 缺 callId 时自己补一个稳定的', () => {
    const first = classifyChatEvent({ type: 'tool_call', name: 'Click' });
    const second = classifyChatEvent({ type: 'tool_call', name: 'Click' });
    expect(first.toolCall?.name).toBe('Click');
    expect(first.toolCall?.callId).toBeTruthy();
    expect(second.toolCall?.callId).not.toBe(first.toolCall?.callId);
  });

  it('tool_result 按 ok 翻成 success / failure', () => {
    expect(
      classifyChatEvent({
        type: 'tool_result',
        callId: 'c1',
        ok: true,
        summary: '已点击',
        durationMs: 12,
      }).toolCall?.status,
    ).toBe('success');
    expect(
      classifyChatEvent({ type: 'tool_result', callId: 'c1', ok: false })
        .toolCall?.status,
    ).toBe('failure');
  });

  it('confirm_required 归成待确认的写操作', () => {
    const event = classifyChatEvent({
      type: 'confirm_required',
      reason: '该操作会改机器状态',
      toolCalls: [{ callId: 'c9', name: 'Click', args: { x: 1 } }],
    });
    expect(event.kind).toBe('confirm_required');
    expect(event.reason).toBe('该操作会改机器状态');
    expect(event.pendingCalls?.[0]).toMatchObject({
      callId: 'c9',
      name: 'Click',
      status: 'pending',
    });
  });

  it('error 事件带出中文原因，缺省也有兜底', () => {
    expect(
      classifyChatEvent({ type: 'error', message: '模型超时' }).error,
    ).toBe('模型超时');
    expect(classifyChatEvent({ type: 'error' }).error).toBe('AI 执行失败');
  });

  it('未知类型与脏数据都归为 unknown，不抛错', () => {
    expect(classifyChatEvent({ type: 'nope' }).kind).toBe('unknown');
    expect(classifyChatEvent(null).kind).toBe('unknown');
    expect(classifyChatEvent('x').kind).toBe('unknown');
  });
});

describe('事件合并进气泡', () => {
  const base = {
    key: 'a1',
    role: 'assistant' as const,
    content: '',
    reasoning: '',
    cards: [],
    toolCalls: [],
  };

  it('content / reasoning 是增量拼接', () => {
    const one = applyChatEvent(base, { kind: 'content', delta: '你' });
    const two = applyChatEvent(one, { kind: 'content', delta: '好' });
    expect(two.content).toBe('你好');
    const reasoned = applyChatEvent(two, { kind: 'reasoning', delta: '嗯' });
    expect(reasoned.reasoning).toBe('嗯');
    expect(reasoned.content).toBe('你好');
  });

  it('tool_call 追加，tool_result 按 callId 回填', () => {
    const withCall = applyChatEvent(base, {
      kind: 'tool_call',
      toolCall: { callId: 'c1', name: 'Click', status: 'running' },
    });
    expect(withCall.toolCalls).toHaveLength(1);
    const done = applyChatEvent(withCall, {
      kind: 'tool_result',
      callId: 'c1',
      toolCall: {
        callId: 'c1',
        name: '',
        status: 'success',
        summary: '点完了',
        durationMs: 30,
      },
    });
    expect(done.toolCalls[0]).toMatchObject({
      status: 'success',
      summary: '点完了',
      durationMs: 30,
      name: 'Click',
    });
  });

  it('confirm_required 把对应调用标成待确认', () => {
    const withCall = applyChatEvent(base, {
      kind: 'tool_call',
      toolCall: { callId: 'c1', name: 'Click', status: 'running' },
    });
    const pending = applyChatEvent(withCall, {
      kind: 'confirm_required',
      pendingCalls: [{ callId: 'c1', name: 'Click', status: 'pending' }],
      reason: '需要确认',
    });
    expect(pending.toolCalls[0].status).toBe('pending');
  });

  it('message_end 收掉流式标记', () => {
    const end = applyChatEvent(
      { ...base, streaming: true },
      { kind: 'message_end', elapsedMs: 1200 },
    );
    expect(end.streaming).toBe(false);
    expect(end.elapsedMs).toBe(1200);
  });

  it('error 写进气泡', () => {
    expect(applyChatEvent(base, { kind: 'error', error: '崩了' }).error).toBe(
      '崩了',
    );
  });

  it('unknown 事件原样返回，不做任何改动', () => {
    expect(applyChatEvent(base, { kind: 'unknown' })).toBe(base);
  });
});

describe('组件行为（mock 后端）', () => {
  it('没就绪（ready !== true）时整块侧边栏不渲染', async () => {
    requestMock.mockImplementation(() =>
      ok({ hostId: 7, hostName: 'win-01', ready: false }),
    );
    const { container } = render(<ComputerUsePanel hostId={7} />);
    await waitFor(() => expect(requestMock).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.queryByTestId('computeruse-panel')).toBeNull();
      expect(screen.queryByTestId('computeruse-strip')).toBeNull();
    });
    expect(container.firstChild).toBeNull();
  });

  it('状态接口失败时也不渲染（部署没完成就不该占地方）', async () => {
    requestMock.mockRejectedValue(new Error('404'));
    const { container } = render(<ComputerUsePanel hostId={7} />);
    await waitFor(() => expect(requestMock).toHaveBeenCalled());
    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  it('就绪后默认折叠成窄条，点展开按钮变成面板并落盘', async () => {
    requestMock.mockImplementation((url: string) => {
      if (url.includes('/status')) return ok(READY_STATUS);
      if (url.includes('/tools'))
        return ok({ data: { items: READY_STATUS.tools } });
      return ok({});
    });
    render(<ComputerUsePanel hostId={7} />);

    const strip = await screen.findByTestId('computeruse-strip');
    expect(strip.textContent).toContain('ComputerUse');
    expect(screen.queryByTestId('computeruse-panel')).toBeNull();

    fireEvent.click(screen.getByLabelText('展开 ComputerUse AI 操作台'));

    expect(await screen.findByTestId('computeruse-panel')).toBeTruthy();
    expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('1');
    // 头部展示主机名 / MCP 地址 / 工具数量
    expect(screen.getByText('win-01')).toBeTruthy();
    expect(screen.getByTestId('computeruse-mcp-url').textContent).toBe(
      'http://192.168.1.20:8765/mcp',
    );

    fireEvent.click(screen.getByLabelText('收起 ComputerUse 面板'));
    await waitFor(() => {
      expect(screen.queryByTestId('computeruse-panel')).toBeNull();
      expect(screen.getByTestId('computeruse-strip')).toBeTruthy();
    });
    expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0');
  });

  it('「看一眼屏幕」把 Screenshot 的图片内联渲染出来', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '1');
    requestMock.mockImplementation((url: string) => {
      if (url.includes('/status')) return ok(READY_STATUS);
      if (url.includes('/tools'))
        return ok({ data: { items: READY_STATUS.tools } });
      if (url.includes('/mcp/call')) {
        return ok({
          tool: 'Screenshot',
          text: '当前桌面',
          images: [{ mime: 'image/png', dataUrl: 'data:image/png;base64,AAA' }],
        });
      }
      return ok({});
    });
    render(<ComputerUsePanel hostId={7} />);

    fireEvent.click(await screen.findByLabelText('看一眼屏幕'));

    const gallery = await screen.findByTestId('computeruse-shots');
    const image = gallery.querySelector('img');
    expect(image?.getAttribute('src')).toBe('data:image/png;base64,AAA');
    expect(screen.getByText('当前桌面')).toBeTruthy();
    // 请求体必须是契约上的那一份
    const call = requestMock.mock.calls.find((args) =>
      String(args[0]).includes('/mcp/call'),
    );
    expect(call).toBeTruthy();
    const [url, options] = call as [string, { data: unknown }];
    expect(url).toBe('/api/computer-use/mcp/call');
    expect(options.data).toEqual({
      hostId: 7,
      tool: 'Screenshot',
      arguments: {},
    });
  });

  it('截图失败时给出可读错误，而不是空画面', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '1');
    requestMock.mockImplementation((url: string) => {
      if (url.includes('/status')) return ok(READY_STATUS);
      if (url.includes('/tools')) return ok({ data: { items: [] } });
      if (url.includes('/mcp/call')) {
        return Promise.reject({
          response: { data: { message: '目标机 MCP 未响应' } },
        });
      }
      return ok({});
    });
    render(<ComputerUsePanel hostId={7} />);

    fireEvent.click(await screen.findByLabelText('看一眼屏幕'));

    expect(await screen.findByText('目标机 MCP 未响应')).toBeTruthy();
    expect(screen.queryByTestId('computeruse-shots')).toBeNull();
  });

  it('工具清单不再占版面，但头部仍报出可用工具数', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '1');
    requestMock.mockImplementation((url: string) => {
      if (url.includes('/status')) return ok(READY_STATUS);
      if (url.includes('/tools'))
        return ok({ data: { items: READY_STATUS.tools } });
      return ok({});
    });
    render(<ComputerUsePanel hostId={7} />);

    expect(await screen.findByTestId('computeruse-chat')).toBeTruthy();
    expect(screen.queryByText('工具清单')).toBeNull();
    expect(screen.queryByTestId('computeruse-tools')).toBeNull();
    expect(screen.queryByText('Screenshot · 只读')).toBeNull();
    expect(screen.getByText(/2 个/)).toBeTruthy();
  });

  it('欢迎态的示例问题点一下就发出去', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '1');
    requestMock.mockImplementation((url: string) =>
      url.includes('/status')
        ? ok(READY_STATUS)
        : ok({ data: { items: READY_STATUS.tools } }),
    );
    render(<ComputerUsePanel hostId={7} />);

    fireEvent.click(await screen.findByText(WELCOME_HINTS[0]));

    await waitFor(() => expect(streamAiChatMock).toHaveBeenCalled());
    const payload = streamAiChatMock.mock.calls[0][0] as {
      hostId: number;
      message: string;
    };
    expect(payload.hostId).toBe(7);
    expect(payload.message).toBe(WELCOME_HINTS[0]);
    await waitFor(() =>
      expect(
        screen.queryByText('告诉 AI 在这台机器上做什么，它来操作并给你截图'),
      ).toBeNull(),
    );
  });

  it('思考过程与工具调用过程都显示出来（对齐主界面 AI 运维）', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '1');
    requestMock.mockImplementation((url: string) =>
      url.includes('/status')
        ? ok(READY_STATUS)
        : ok({ data: { items: READY_STATUS.tools } }),
    );
    streamAiChatMock.mockImplementation(
      (_payload: unknown, onEvent: (event: unknown) => void) => {
        onEvent({ type: 'start', conversationId: 42 });
        onEvent({ type: 'reasoning', delta: '先看一眼桌面' });
        onEvent({
          type: 'tool_call',
          callId: 'call-1',
          name: 'Screenshot',
          args: {},
          description: '截屏',
        });
        onEvent({
          type: 'tool_result',
          callId: 'call-1',
          ok: true,
          summary: '已截到 1024x768',
        });
        onEvent({ type: 'content', delta: '桌面正常。' });
        onEvent({ type: 'message_end', status: 'done', elapsedMs: 1200 });
        return Promise.resolve();
      },
    );
    render(<ComputerUsePanel hostId={7} />);

    const box = await screen.findByLabelText('ComputerUse AI 指令');
    fireEvent.change(box, { target: { value: '看一眼' } });
    fireEvent.click(screen.getByLabelText('发送'));

    expect(await screen.findByText('思考过程')).toBeTruthy();
    expect(screen.getByText('先看一眼桌面')).toBeTruthy();
    expect(screen.getByText('工具调用 1 次')).toBeTruthy();
    expect(screen.getByText('已截到 1024x768')).toBeTruthy();
    expect(screen.getByTestId('xmarkdown').textContent).toBe('桌面正常。');
    expect(screen.getByText(/用时 1\.2s/)).toBeTruthy();
  });

  it('发送对话时带上当前 hostId（AI 才知道操作哪台机器）', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '1');
    requestMock.mockImplementation((url: string) => {
      if (url.includes('/status')) return ok(READY_STATUS);
      if (url.includes('/tools'))
        return ok({ data: { items: READY_STATUS.tools } });
      return ok({});
    });
    render(<ComputerUsePanel hostId={7} />);

    const box = await screen.findByLabelText('ComputerUse AI 指令');
    fireEvent.change(box, { target: { value: '打开记事本' } });
    fireEvent.click(screen.getByLabelText('发送'));

    await waitFor(() => expect(streamAiChatMock).toHaveBeenCalled());
    const payload = streamAiChatMock.mock.calls[0][0] as {
      hostId: number;
      message: string;
    };
    expect(payload.hostId).toBe(7);
    expect(payload.message).toBe('打开记事本');
  });

  it('面板里始终写着「控制台会话 / 以截图为准」的前提提示', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '1');
    requestMock.mockImplementation((url: string) =>
      url.includes('/status')
        ? ok(READY_STATUS)
        : ok({ data: { items: READY_STATUS.tools } }),
    );
    render(<ComputerUsePanel hostId={7} />);
    expect(await screen.findByText(/请以截图为准/)).toBeTruthy();
  });
});
