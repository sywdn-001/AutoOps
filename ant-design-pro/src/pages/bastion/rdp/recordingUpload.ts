/**
 * 「边录边传」的分片队列与任务号生成。
 *
 * 为什么需要它：`MediaRecorder` 每 5 秒吐一片（`ondataavailable`），而服务端是**按 seq
 * 顺序追加**的（乱序直接 409，因为拼错了就是坏视频）。浏览器这边发请求是异步的，直接
 * `forEach` 发出去就会并发 + 乱序，所以这里做成一条**单通道串行队列**：上一片确认落盘
 * 才发下一片；某片失败就地重试；收尾时 `drain()` 等最后一片确认完，再交给 finalize。
 *
 * 队列只负责「按序、可靠、可等的发送」，不关心网络实现（测试里注入假 sender），
 * 也不吞失败：`drain()` 返回失败片数，由调用方决定是改走整包上传兜底，还是如实报错。
 */

export type ChunkSender = (chunk: Blob, seq: number) => Promise<unknown>;

export type ChunkQueueOptions = {
  /** 每片最多重试几次（默认 2 ⇒ 最多发 3 次） */
  retries?: number;
  /** 重试前的等待毫秒；第 n 次重试等 `retryDelayMs * n`（默认 300） */
  retryDelayMs?: number;
  /** 每次状态变化回调（已确认 / 失败 / 排队中），用于界面提示 */
  onProgress?: (state: {
    sent: number;
    failed: number;
    pending: number;
  }) => void;
  /** 注入等待实现（测试里传 `() => Promise.resolve()`，免得真等） */
  sleep?: (ms: number) => Promise<void>;
};

/** 队列快照：`sent` 已确认落盘，`failed` 彻底失败（重试也没成），`pending` 还在排队 */
export type ChunkQueueStats = { sent: number; failed: number; pending: number };

const defaultSleep = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

export class ChunkQueue {
  private readonly sender: ChunkSender;

  private readonly retries: number;

  private readonly retryDelayMs: number;

  private readonly onProgress?: ChunkQueueOptions['onProgress'];

  private readonly sleep: (ms: number) => Promise<void>;

  private pending: Array<{ blob: Blob; seq: number }> = [];

  private running: Promise<void> | null = null;

  private sent = 0;

  private failed = 0;

  private lastError: unknown = null;

  constructor(sender: ChunkSender, options: ChunkQueueOptions = {}) {
    this.sender = sender;
    this.retries = Math.max(0, options.retries ?? 2);
    this.retryDelayMs = Math.max(0, options.retryDelayMs ?? 300);
    this.onProgress = options.onProgress;
    this.sleep = options.sleep ?? defaultSleep;
  }

  /** 入队一片。同步返回 —— `ondataavailable` 里不能 await，否则会堵住录制线程 */
  enqueue(blob: Blob, seq: number): void {
    this.pending.push({ blob, seq });
    this.pump();
  }

  /** 还没确认落盘的片数 */
  get size(): number {
    return this.pending.length;
  }

  /** 最后一片失败的原因（给界面/日志用） */
  get error(): unknown {
    return this.lastError;
  }

  stats(): ChunkQueueStats {
    return {
      sent: this.sent,
      failed: this.failed,
      pending: this.pending.length,
    };
  }

  /**
   * 等所有已入队的片都结束（含正在重试的那片），返回**彻底失败**的片数。
   *
   * 返回值 > 0 表示这段录像在服务端不完整：调用方应当改走整包上传兜底（老路径），
   * 并且别再 finalize —— 否则会得到一段缺了中间几片的坏视频。
   */
  async drain(): Promise<number> {
    while (this.running || this.pending.length) {
      // 先推一下（可能因为上一片刚结束而空转），再等当前这片
      this.pump();
      if (this.running) {
        await this.running;
      }
    }
    return this.failed;
  }

  private pump(): void {
    if (this.running) {
      return;
    }
    const item = this.pending.shift();
    if (!item) {
      return;
    }
    this.emit();
    this.running = this.send(item).finally(() => {
      this.running = null;
      this.emit();
      this.pump();
    });
  }

  private async send(item: { blob: Blob; seq: number }): Promise<void> {
    for (let attempt = 0; attempt <= this.retries; attempt += 1) {
      try {
        await this.sender(item.blob, item.seq);
        this.sent += 1;
        return;
      } catch (error) {
        this.lastError = error;
        if (attempt === this.retries) {
          this.failed += 1;
          return;
        }
        await this.sleep(this.retryDelayMs * (attempt + 1));
      }
    }
  }

  private emit(): void {
    this.onProgress?.(this.stats());
  }
}

/**
 * 生成一个上传任务号：32 位十六进制（正好落在后端 `^[0-9a-f]{8,64}$` 里）。
 *
 * `crypto.randomUUID()` 只在**安全上下文**（https / localhost）有 —— 用局域网 IP 访问
 * 堡垒机时它是 `undefined`，所以这里留了 `getRandomValues` 的回退，避免整条录像链路
 * 因为「拿不到 uuid」而直接崩掉。
 */
export function newUploadId(): string {
  const webCrypto = globalThis.crypto;
  if (typeof webCrypto?.randomUUID === 'function') {
    return webCrypto.randomUUID().replace(/-/g, '');
  }
  const bytes = new Uint8Array(16);
  if (typeof webCrypto?.getRandomValues === 'function') {
    webCrypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join(
    '',
  );
}
