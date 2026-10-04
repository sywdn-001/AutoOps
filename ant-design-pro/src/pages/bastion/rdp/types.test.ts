/**
 * `describeRdpError` 的单元测试。
 *
 * 这条链路上「错误长什么样」直接决定用户能不能自己解决，所以把几种真实形态钉成断言：
 * wasm 的 `IronError`（要调 `kind()` / `backtrace()` 才拿得到内容）、RDCleanPath 的
 * HTTP 状态（403 是堡垒机拒绝，502 是连不上目标机）、以及老目标机连上之后
 * 浏览器端解不开 PDU 的 `[decode error] general error`（实测 Windows Server 2003）。
 */
import { describe, expect, it } from 'vitest';
import { describeRdpError } from './types';

describe('describeRdpError', () => {
  it('老目标机解码失败时直接给出两条出路（强制 SSL / 改用系统远程桌面）', () => {
    const text = describeRdpError('[decode error] general error');
    expect(text).toContain('解析不了');
    expect(text).toContain('强制 SSL');
    expect(text).toContain('远程桌面连接');
    // 原始信息必须留在文案里，排障要用原文
    expect(text).toContain('[decode error] general error');
  });

  it('认证阶段被掐断（WebSocket connection failed）也要给中文原因与出路', () => {
    const text = describeRdpError(
      '[read frame by hint] custom error: WebSocket connection failed',
    );
    expect(text).toContain('认证阶段就断了');
    expect(text).toContain('强制 SSL');
    expect(text).toContain('远程桌面连接');
    expect(text).toContain('WebSocket connection failed');
  });

  it('wasm 错误码翻成中文，并保留 backtrace 原文', () => {
    const text = describeRdpError({
      kind: () => 1,
      backtrace: () => 'wrong password',
    });
    expect(text).toBe('账号或口令不正确（wrong password）');
  });

  it('RDCleanPath 403 说清是「堡垒机拒绝」，不是目标机拒绝', () => {
    const text = describeRdpError({
      rdcleanpathDetails: () => ({ httpStatusCode: 403 }),
      backtrace: () => 'denied',
    });
    expect(text).toBe('堡垒机拒绝了这次连接：denied');
  });

  it('502 说清是「连不上目标机或安全层协商失败」', () => {
    const text = describeRdpError({
      rdcleanpathDetails: () => ({ httpStatusCode: 502 }),
      backtrace: () => 'connect failed',
    });
    expect(text).toContain('堡垒机连不上目标机');
    expect(text).toContain('安全层协商失败');
    expect(text).toContain('connect failed');
  });

  it('普通字符串错误原样返回', () => {
    expect(describeRdpError('boom')).toBe('boom');
  });

  it('空对象不再渲染成 [object Object]', () => {
    const text = describeRdpError({});
    expect(text).not.toContain('[object Object]');
    expect(text).toContain('未知错误');
  });
});
