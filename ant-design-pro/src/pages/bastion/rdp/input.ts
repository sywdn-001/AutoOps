/**
 * 键盘 / 鼠标事件 → RDP 输入事件。
 *
 * 表与逻辑照抄 ironrdp-wasm 官方 example 的 `example/lib/input.js`（MIT）：
 * - `SCANCODE_MAP` 是 `KeyboardEvent.code` → RDP 扫描码，**不要改里面的值**；
 * - 每个事件包一个 `InputTransaction` 再 `session.applyInputs(tx)`；
 * - 鼠标坐标要按「画布像素 / 显示像素」的比例换算（画布分辨率由目标机决定，通常 1280×720 或更大）。
 */
import type * as IronRdp from 'ironrdp-wasm';

/** `KeyboardEvent.code` → RDP 扫描码（0xE0 前缀的键展开成 0xE0xx）。 */
export const SCANCODE_MAP: Record<string, number> = {
  Escape: 0x01,
  Digit1: 0x02,
  Digit2: 0x03,
  Digit3: 0x04,
  Digit4: 0x05,
  Digit5: 0x06,
  Digit6: 0x07,
  Digit7: 0x08,
  Digit8: 0x09,
  Digit9: 0x0a,
  Digit0: 0x0b,
  Minus: 0x0c,
  Equal: 0x0d,
  Backspace: 0x0e,
  Tab: 0x0f,
  KeyQ: 0x10,
  KeyW: 0x11,
  KeyE: 0x12,
  KeyR: 0x13,
  KeyT: 0x14,
  KeyY: 0x15,
  KeyU: 0x16,
  KeyI: 0x17,
  KeyO: 0x18,
  KeyP: 0x19,
  BracketLeft: 0x1a,
  BracketRight: 0x1b,
  Enter: 0x1c,
  ControlLeft: 0x1d,
  KeyA: 0x1e,
  KeyS: 0x1f,
  KeyD: 0x20,
  KeyF: 0x21,
  KeyG: 0x22,
  KeyH: 0x23,
  KeyJ: 0x24,
  KeyK: 0x25,
  KeyL: 0x26,
  Semicolon: 0x27,
  Quote: 0x28,
  Backquote: 0x29,
  ShiftLeft: 0x2a,
  Backslash: 0x2b,
  KeyZ: 0x2c,
  KeyX: 0x2d,
  KeyC: 0x2e,
  KeyV: 0x2f,
  KeyB: 0x30,
  KeyN: 0x31,
  KeyM: 0x32,
  Comma: 0x33,
  Period: 0x34,
  Slash: 0x35,
  ShiftRight: 0x36,
  NumpadMultiply: 0x37,
  AltLeft: 0x38,
  Space: 0x39,
  CapsLock: 0x3a,
  F1: 0x3b,
  F2: 0x3c,
  F3: 0x3d,
  F4: 0x3e,
  F5: 0x3f,
  F6: 0x40,
  F7: 0x41,
  F8: 0x42,
  F9: 0x43,
  F10: 0x44,
  NumLock: 0x45,
  ScrollLock: 0x46,
  Numpad7: 0x47,
  Numpad8: 0x48,
  Numpad9: 0x49,
  NumpadSubtract: 0x4a,
  Numpad4: 0x4b,
  Numpad5: 0x4c,
  Numpad6: 0x4d,
  NumpadAdd: 0x4e,
  Numpad1: 0x4f,
  Numpad2: 0x50,
  Numpad3: 0x51,
  Numpad0: 0x52,
  NumpadDecimal: 0x53,
  F11: 0x57,
  F12: 0x58,
  NumpadEnter: 0xe01c,
  ControlRight: 0xe01d,
  NumpadDivide: 0xe035,
  PrintScreen: 0xe037,
  AltRight: 0xe038,
  Home: 0xe047,
  ArrowUp: 0xe048,
  PageUp: 0xe049,
  ArrowLeft: 0xe04b,
  ArrowRight: 0xe04d,
  End: 0xe04f,
  ArrowDown: 0xe050,
  PageDown: 0xe051,
  Insert: 0xe052,
  Delete: 0xe053,
  MetaLeft: 0xe05b,
  MetaRight: 0xe05c,
  ContextMenu: 0xe05d,
  Pause: 0xe11d45,
};

/**
 * 把画布上的显示坐标换算成目标机桌面坐标（画布分辨率 = 目标机分辨率）。
 *
 * 不能只用 `canvas.width / rect.width`：那个比例假设「画布内容被拉伸铺满整个元素矩形」。
 * 实际有两种情况会让它算错：
 * 1. 浏览器全屏：`:fullscreen` 的 UA 样式把元素撑到整屏，同时按 `object-fit: contain` 居中内容，
 *    元素矩形比真实画面大（左右或上下有黑边）——旧算法把黑边也算进比例，鼠标就整体偏移；
 * 2. CSS 给画布设了固定的 `width/height`（与后备缓冲尺寸不一致）时，X/Y 缩放比可能不同。
 *
 * 所以这里按「contain」求出画面真实占据的矩形（等比缩放 + 居中偏移），再换算坐标。
 * 现在控制台让画布尺寸恒等于舞台尺寸（1:1），这条路径的结果与旧算法一致；全屏、缩放等
 * 场景则由它兜底。
 *
 * 导出是为了让 `input.test.ts` 直接钉住换算结果 —— 这条路径的错误表现（全屏后鼠标整体
 * 偏移）没法靠看截图稳定判定，只有断言坐标才算证据。
 */
export const toDesktopPoint = (
  canvas: HTMLCanvasElement,
  clientX: number,
  clientY: number,
) => {
  const rect = canvas.getBoundingClientRect();
  const ratioX =
    rect.width > 0 && canvas.width > 0 ? rect.width / canvas.width : 1;
  const ratioY =
    rect.height > 0 && canvas.height > 0 ? rect.height / canvas.height : 1;
  // 等比缩放（contain）：取较小的那个比例，另一个方向就会留黑边
  const scale = Math.min(ratioX, ratioY) || 1;
  const shownWidth = canvas.width * scale;
  const shownHeight = canvas.height * scale;
  const originX = rect.left + (rect.width - shownWidth) / 2;
  const originY = rect.top + (rect.height - shownHeight) / 2;
  const x = Math.round((clientX - originX) / scale);
  const y = Math.round((clientY - originY) / scale);
  return {
    x: Math.max(0, Math.min(canvas.width, x)),
    y: Math.max(0, Math.min(canvas.height, y)),
  };
};

/**
 * 给画布装上输入通道，返回解绑函数（断开 / 关窗时必须调用，否则监听器会留着）。
 */
export const attachInput = (
  canvas: HTMLCanvasElement,
  session: IronRdp.Session,
  rdp: typeof IronRdp,
): (() => void) => {
  const send = (event: IronRdp.DeviceEvent) => {
    const tx = new rdp.InputTransaction();
    tx.addEvent(event);
    session.applyInputs(tx);
  };

  const onKeyDown = (event: KeyboardEvent) => {
    const scancode = SCANCODE_MAP[event.code];
    if (scancode === undefined) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    send(rdp.DeviceEvent.keyPressed(scancode));
  };

  const onKeyUp = (event: KeyboardEvent) => {
    const scancode = SCANCODE_MAP[event.code];
    if (scancode === undefined) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    send(rdp.DeviceEvent.keyReleased(scancode));
  };

  const onMouseMove = (event: MouseEvent) => {
    const point = toDesktopPoint(canvas, event.clientX, event.clientY);
    send(rdp.DeviceEvent.mouseMove(point.x, point.y));
  };

  const onMouseDown = (event: MouseEvent) => {
    event.preventDefault();
    canvas.focus();
    const point = toDesktopPoint(canvas, event.clientX, event.clientY);
    send(rdp.DeviceEvent.mouseMove(point.x, point.y));
    send(rdp.DeviceEvent.mouseButtonPressed(event.button));
  };

  const onMouseUp = (event: MouseEvent) => {
    event.preventDefault();
    send(rdp.DeviceEvent.mouseButtonReleased(event.button));
  };

  const onWheel = (event: WheelEvent) => {
    event.preventDefault();
    if (event.deltaY !== 0) {
      // 第三个参数 1 = RotationUnit.Pixel；向上滚是负值
      send(rdp.DeviceEvent.wheelRotations(true, event.deltaY > 0 ? -1 : 1, 1));
    }
    if (event.deltaX !== 0) {
      send(rdp.DeviceEvent.wheelRotations(false, event.deltaX > 0 ? -1 : 1, 1));
    }
  };

  const onContextMenu = (event: Event) => {
    event.preventDefault();
  };

  // 失焦时把按下的键全放开，避免切窗口回来「Ctrl 一直按住」
  const onBlur = () => {
    try {
      session.releaseAllInputs();
    } catch {
      // 会话已经结束时会抛，这里不需要处理
    }
  };

  canvas.addEventListener('keydown', onKeyDown);
  canvas.addEventListener('keyup', onKeyUp);
  canvas.addEventListener('mousemove', onMouseMove);
  canvas.addEventListener('mousedown', onMouseDown);
  canvas.addEventListener('mouseup', onMouseUp);
  canvas.addEventListener('wheel', onWheel, { passive: false });
  canvas.addEventListener('contextmenu', onContextMenu);
  window.addEventListener('blur', onBlur);

  return () => {
    canvas.removeEventListener('keydown', onKeyDown);
    canvas.removeEventListener('keyup', onKeyUp);
    canvas.removeEventListener('mousemove', onMouseMove);
    canvas.removeEventListener('mousedown', onMouseDown);
    canvas.removeEventListener('mouseup', onMouseUp);
    canvas.removeEventListener('wheel', onWheel);
    canvas.removeEventListener('contextmenu', onContextMenu);
    window.removeEventListener('blur', onBlur);
  };
};
