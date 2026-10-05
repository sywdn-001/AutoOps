/**
 * `uploadProgress.ts` 的单元测试：钉住上传进度条的百分比与速率文案。
 *
 * 关键判据是「发送完成前最多 99%」——最后 1% 留给服务端把字节真正写进目标机，
 * 否则浏览器显示 100% 时文件其实还没落盘，用户会以为已经传完。
 */
import { describe, expect, it } from 'vitest';
import { formatRate, uploadPercent } from './uploadProgress';

describe('uploadPercent', () => {
  it('按已发字节给出百分比', () => {
    expect(uploadPercent(0, 1000)).toBe(0);
    expect(uploadPercent(250, 1000)).toBe(25);
    expect(uploadPercent(999, 1000)).toBe(99);
  });

  it('发送完成前最多 99%：最后 1% 留给服务端写进目标机', () => {
    expect(uploadPercent(1000, 1000)).toBe(99);
    expect(uploadPercent(1200, 1000)).toBe(99);
    expect(uploadPercent(5000, 4000)).toBe(99);
  });

  it('总长为 0 或数据异常时返回 0，不出现 NaN/负数', () => {
    expect(uploadPercent(100, 0)).toBe(0);
    expect(uploadPercent(100, -5)).toBe(0);
    expect(uploadPercent(Number.NaN, 100)).toBe(0);
    expect(uploadPercent(100, Number.NaN)).toBe(0);
    expect(uploadPercent(-10, 100)).toBe(0);
  });

  it('向下取整，不做四舍五入', () => {
    expect(uploadPercent(199, 1000)).toBe(19);
    expect(uploadPercent(999, 2000)).toBe(49);
  });

  it('封顶值可调整（默认 99，真正落地后可用 100）', () => {
    expect(uploadPercent(1000, 1000, 100)).toBe(100);
  });
});

describe('formatRate', () => {
  const format = (size: number) => `${Math.round(size / 1024)} KB`;

  it('速率可读时给出 每秒 文本', () => {
    expect(formatRate(2048, format)).toBe('2 KB/s');
  });

  it('还没测出速率（0/负数/NaN）时返回空串，避免出现 “0 B/s”', () => {
    expect(formatRate(0, format)).toBe('');
    expect(formatRate(-1, format)).toBe('');
    expect(formatRate(Number.NaN, format)).toBe('');
  });
});
