/**
 * `recordingUpload.ts` 的单元测试：钉住录像分片上传队列的行为。
 *
 * 录像丢了就再也补不回来，所以这里断言的是「宁慢勿乱」：分片严格串行（乱序即坏视频）、
 * 失败自动重试两次、重试用尽只记失败且后续分片照发、`drain()` 等待期间新入队的分片也要等完、
 * `newUploadId()` 在非安全上下文（局域网 IP 没有 `crypto.randomUUID`）下逐级回退且始终是
 * 8–64 位小写十六进制。
 */
import { describe, expect, it, vi } from 'vitest';

import { ChunkQueue, newUploadId } from './recordingUpload';

const noSleep = () => Promise.resolve();

/** 造一片假数据（内容无所谓，队列只看顺序与成败） */
const chunk = (label: string) =>
  new Blob([label], { type: 'application/octet-stream' });

/** 记录调用顺序的 sender：返回 (调用的 seq 列表, sender) */
function recorder(options: { failSeqs?: number[]; failTimes?: number } = {}) {
  const calls: number[] = [];
  const attempts = new Map<number, number>();
  const failSeqs = new Set(options.failSeqs ?? []);
  const sender = async (_blob: Blob, seq: number) => {
    calls.push(seq);
    const times = (attempts.get(seq) ?? 0) + 1;
    attempts.set(seq, times);
    if (
      failSeqs.has(seq) &&
      times <= (options.failTimes ?? Number.POSITIVE_INFINITY)
    ) {
      throw new Error(`boom ${seq}`);
    }
    return { seq };
  };
  return { calls, sender };
}

describe('ChunkQueue', () => {
  it('按入队顺序逐片发送，绝不并发（服务端按 seq 追加，乱序就拼坏）', async () => {
    const order: string[] = [];
    const sender = async (_blob: Blob, seq: number) => {
      order.push(`start-${seq}`);
      await Promise.resolve();
      order.push(`end-${seq}`);
      return seq;
    };
    const queue = new ChunkQueue(sender, { sleep: noSleep });
    queue.enqueue(chunk('a'), 0);
    queue.enqueue(chunk('b'), 1);
    queue.enqueue(chunk('c'), 2);

    const failed = await queue.drain();
    expect(failed).toBe(0);
    expect(order).toEqual([
      'start-0',
      'end-0',
      'start-1',
      'end-1',
      'start-2',
      'end-2',
    ]);
    expect(queue.stats()).toEqual({ sent: 3, failed: 0, pending: 0 });
  });

  it('某片失败就地重试，成功也算过（不丢片）', async () => {
    const { calls, sender } = recorder({ failSeqs: [1], failTimes: 1 });
    const queue = new ChunkQueue(sender, { sleep: noSleep, retries: 2 });
    queue.enqueue(chunk('a'), 0);
    queue.enqueue(chunk('b'), 1);
    queue.enqueue(chunk('c'), 2);

    expect(await queue.drain()).toBe(0);
    expect(calls).toEqual([0, 1, 1, 2]); // 序号 1 发了两次
    expect(queue.stats()).toEqual({ sent: 3, failed: 0, pending: 0 });
  });

  it('重试次数用尽仍失败：drain 返回失败片数（调用方据此改走整包兜底）', async () => {
    const { calls, sender } = recorder({ failSeqs: [1] });
    const queue = new ChunkQueue(sender, { sleep: noSleep, retries: 2 });
    queue.enqueue(chunk('a'), 0);
    queue.enqueue(chunk('b'), 1);
    queue.enqueue(chunk('c'), 2);

    expect(await queue.drain()).toBe(1);
    // 失败的那片发了 3 次（1 + 2 次重试），后面的片照常发 —— 不因为一片坏掉就整段放弃
    expect(calls).toEqual([0, 1, 1, 1, 2]);
    expect(queue.stats()).toEqual({ sent: 2, failed: 1, pending: 0 });
    expect(queue.error).toBeInstanceOf(Error);
  });

  it('drain 期间新入队的片也会被等完（录制还在继续时收尾不能漏片）', async () => {
    const sent: number[] = [];
    const queue = new ChunkQueue(
      async (_blob, seq) => {
        sent.push(seq);
        if (seq === 0) {
          // 第一片落盘的过程中又来了两片（模拟 ondataavailable 还在回调）
          queue.enqueue(chunk('b'), 1);
          queue.enqueue(chunk('c'), 2);
        }
        return seq;
      },
      { sleep: noSleep },
    );
    queue.enqueue(chunk('a'), 0);

    expect(await queue.drain()).toBe(0);
    expect(sent).toEqual([0, 1, 2]);
    expect(queue.size).toBe(0);
  });

  it('onProgress 每次状态变化都回调（界面用来显示「已上传 N 片」）', async () => {
    const snapshots: Array<{ sent: number; failed: number; pending: number }> =
      [];
    const { sender } = recorder();
    const queue = new ChunkQueue(sender, {
      sleep: noSleep,
      onProgress: (state) => snapshots.push(state),
    });
    queue.enqueue(chunk('a'), 0);
    queue.enqueue(chunk('b'), 1);
    await queue.drain();

    expect(snapshots.at(-1)).toEqual({ sent: 2, failed: 0, pending: 0 });
    expect(snapshots.length).toBeGreaterThanOrEqual(2);
  });

  it('空队列 drain 立刻返回 0（没有录像时不空转）', async () => {
    const queue = new ChunkQueue(vi.fn(), { sleep: noSleep });
    expect(await queue.drain()).toBe(0);
    expect(queue.stats()).toEqual({ sent: 0, failed: 0, pending: 0 });
  });
});

describe('newUploadId', () => {
  it('是 8-64 位小写十六进制（后端 UPLOAD_ID_PATTERN 的硬要求）', () => {
    for (let index = 0; index < 20; index += 1) {
      expect(newUploadId()).toMatch(/^[0-9a-f]{8,64}$/);
    }
  });

  it('每次都不一样（同一台机器上多段录像不能撞任务号）', () => {
    const ids = new Set(Array.from({ length: 50 }, () => newUploadId()));
    expect(ids.size).toBe(50);
  });

  it('走局域网 IP（非安全上下文，crypto.randomUUID 不存在）时也有回退', () => {
    const original = globalThis.crypto;
    // 模拟 http://192.168.x.x 下的 window.crypto：只有 getRandomValues
    Object.defineProperty(globalThis, 'crypto', {
      value: { getRandomValues: (bytes: Uint8Array) => bytes.fill(7) },
      configurable: true,
      writable: true,
    });
    try {
      expect(newUploadId()).toBe('07'.repeat(16));
    } finally {
      Object.defineProperty(globalThis, 'crypto', {
        value: original,
        configurable: true,
        writable: true,
      });
    }
  });
});
