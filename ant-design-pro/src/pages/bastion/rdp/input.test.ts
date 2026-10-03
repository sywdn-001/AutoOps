/**
 * 鼠标坐标换算的单元测试（需求①：全屏时鼠标位置偏移）。
 *
 * 这条缺陷的现场表现是「全屏后鼠标整体偏移」，靠看截图或肉眼看画布无法稳定判定，
 * 所以把 `toDesktopPoint` 的换算结果钉成断言：
 * 画布后备缓冲（= 目标机桌面分辨率）与元素矩形的宽高比不一致时，浏览器按
 * `object-fit: contain` 居中并留下黑边，黑边**不属于画面**，换算时必须先减掉。
 */
import { describe, expect, it } from 'vitest';
import { toDesktopPoint } from './input';

/** 造一个只有 `width/height/getBoundingClientRect` 的假画布（函数只用到这三样）。 */
const canvasOf = (
  width: number,
  height: number,
  rect: { left: number; top: number; width: number; height: number },
) =>
  ({
    width,
    height,
    getBoundingClientRect: () => rect as DOMRect,
  }) as HTMLCanvasElement;

describe('toDesktopPoint（画布显示坐标 → 目标机桌面坐标）', () => {
  it('1:1 显示时按原样换算', () => {
    const canvas = canvasOf(1280, 720, {
      left: 100,
      top: 50,
      width: 1280,
      height: 720,
    });
    expect(toDesktopPoint(canvas, 100 + 640, 50 + 360)).toEqual({
      x: 640,
      y: 360,
    });
    expect(toDesktopPoint(canvas, 100, 50)).toEqual({ x: 0, y: 0 });
    expect(toDesktopPoint(canvas, 100 + 1280, 50 + 720)).toEqual({
      x: 1280,
      y: 720,
    });
  });

  it('等比放大（宽高比相同）时只按比例缩放', () => {
    const canvas = canvasOf(1280, 720, {
      left: 0,
      top: 0,
      width: 1920,
      height: 1080,
    });
    expect(toDesktopPoint(canvas, 960, 540)).toEqual({ x: 640, y: 360 });
    expect(toDesktopPoint(canvas, 1920, 1080)).toEqual({ x: 1280, y: 720 });
  });

  it('左右有黑边（元素比画面更宽）时先减掉居中偏移', () => {
    // 元素 1920x1000，画面 1280x720(16:9)：等比缩放取 1000/720，左右各留 71.1px 黑边
    const canvas = canvasOf(1280, 720, {
      left: 0,
      top: 0,
      width: 1920,
      height: 1000,
    });
    // 视觉中心仍是画面中心
    expect(toDesktopPoint(canvas, 960, 500)).toEqual({ x: 640, y: 360 });
    // 画面左上角（= 黑边结束处）
    expect(toDesktopPoint(canvas, 71, 0)).toEqual({ x: 0, y: 0 });
    // 点在黑边上：钳到 0，不会给出「画面里其实不存在的坐标」
    expect(toDesktopPoint(canvas, 10, 500)).toEqual({ x: 0, y: 360 });
    // 旧算法（rect.width / canvas.width）在同样的点上给出 7，这正是偏移的来源
    expect(Math.round((10 - 0) / (1920 / 1280))).toBe(7);
    expect(toDesktopPoint(canvas, 10, 500).x).not.toBe(7);
  });

  it('上下有黑边（元素比画面更高）+ 元素有偏移时同样成立', () => {
    // 元素 800x600，画面 1280x720：等比缩放取 800/1280 = 0.625，上下各留 75px 黑边
    const canvas = canvasOf(1280, 720, {
      left: 10,
      top: 20,
      width: 800,
      height: 600,
    });
    expect(toDesktopPoint(canvas, 10, 95)).toEqual({ x: 0, y: 0 });
    expect(toDesktopPoint(canvas, 10 + 800, 95 + 450)).toEqual({
      x: 1280,
      y: 720,
    });
    expect(toDesktopPoint(canvas, 10 + 400, 95 + 225)).toEqual({
      x: 640,
      y: 360,
    });
    // 上黑边里的点被钳到 0
    expect(toDesktopPoint(canvas, 10 + 400, 40)).toEqual({ x: 640, y: 0 });
  });

  it('元素更大时也钳在桌面范围内（不产生越界坐标）', () => {
    const canvas = canvasOf(1280, 720, {
      left: 0,
      top: 0,
      width: 1920,
      height: 1000,
    });
    expect(toDesktopPoint(canvas, 5000, 5000)).toEqual({ x: 1280, y: 720 });
    expect(toDesktopPoint(canvas, -500, -500)).toEqual({ x: 0, y: 0 });
  });
});
