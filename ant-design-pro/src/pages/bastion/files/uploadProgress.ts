/**
 * 上传统计的小工具（纯函数，便于单测钉住口径）。
 *
 * 进度条读的是**浏览器发送进度**（axios 的 `onUploadProgress`）。发送完成之后，
 * 服务端还要把文件写进目标机（SFTP），这段时间没有再上报进度 —— 所以发送完成
 * 之前最多显示 99%，最后 1% 留给服务端落地，避免「进度条 100% 了却还在等」。
 */

export type UploadProgress = {
  loaded: number;
  total: number;
  rate: number;
};

/** 已发字节 → 百分比（0~cap），总长为 0 或数据异常时返回 0 */
export const uploadPercent = (
  loaded: number,
  total: number,
  cap = 99,
): number => {
  if (!Number.isFinite(total) || total <= 0) {
    return 0;
  }
  if (!Number.isFinite(loaded) || loaded <= 0) {
    return 0;
  }
  return Math.min(cap, Math.floor((loaded / total) * 100));
};

/** 字节/秒 → 便于阅读的速率文本，0 表示还没测出来 */
export const formatRate = (
  rate: number,
  format: (size: number) => string,
): string => (Number.isFinite(rate) && rate > 0 ? `${format(rate)}/s` : '');
