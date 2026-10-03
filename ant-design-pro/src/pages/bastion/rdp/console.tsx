/**
 * Windows 远程桌面（WebRDP）控制台窗口。
 *
 * 一个窗口 = 一条 RDP 会话（`layout: false`，由网页终端列表 `window.open()` 弹出）。
 * 数据通路：
 *   canvas（ironrdp-wasm，浏览器里跑 RDP 协议栈）
 *        ↕  同源 WebSocket /api/rdp/ws?ticket=…（一次性票据）
 *   堡垒机 RDCleanPath 网关（app/rdp/proxy.py，进程内，只做字节中继）
 *        ↕  TLS
 *   目标机 192.168.x.x:3389
 *
 * 前端负责四件事：换票据（复验权限）、把 wasm 接上画布、把键鼠喂给会话、把画面录成视频留档。
 * 审计与访问控制在服务端（`POST /api/rdp/sessions` 与网关上各有一道闸）。
 *
 * 几个刻意为之的设计：
 * - **分辨率跟随窗口**：画布的 `width/height`（= 远端桌面分辨率）始终等于舞台的 CSS 像素尺寸，
 *   窗口一变大就通过 `session.resize()` 告诉被控主机「屏幕变大了」。这样画面永远是 1:1 显示，
 *   既不会把 1280×720 拉成满屏（糊），也不会因 `object-fit` 居中留黑边而让鼠标坐标偏移。
 * - **录像在浏览器侧**：`canvas.captureStream()` + `MediaRecorder` 录 WebM，会话结束（或用户点断开）
 *   时上传给堡垒机落盘；审计里能查到「谁在哪台 Windows 机器上操作了多久」以及录像本体。
 * - **会话结束后 10 秒倒计时关窗**：与 Linux 网页终端断开后的行为一致；录像还在上传时先不关窗。
 */
import {
  CloseOutlined,
  DisconnectOutlined,
  DownOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  ReloadOutlined,
  UpOutlined,
} from '@ant-design/icons';
import { useLocation } from '@umijs/max';
import {
  Alert,
  Button,
  Modal,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { OsTag } from '@/components/Bastion';
import { rdpApi } from '@/services/bastion/endpoints';
import type { RdpSessionInfo } from '@/services/bastion/types';
import { attachInput } from './input';
import './rdp.css';
import {
  buildRdpWsUrl,
  describeRdpError,
  RDP_STATUS_META,
  type RdpStatus,
} from './types';

const { Text } = Typography;

/** 协商超时：目标机防火墙丢包时 WebSocket 会一直安静地挂着，不能让界面永远转圈。 */
const NEGOTIATION_TIMEOUT_MS = 45000;

/** 会话结束后自动关窗的倒计时秒数（与 Linux 网页终端保持一致）。 */
const CLOSE_COUNTDOWN_SECONDS = 10;

/** 窗口尺寸变化后重新协商远端分辨率的最小间隔（拖动窗口时不要每个像素都发一次）。 */
const VIEWPORT_DEBOUNCE_MS = 200;

/** 远端桌面的下限：比这更小的分辨率 Windows 桌面会挤成一团。 */
const MIN_VIEWPORT_WIDTH = 800;
const MIN_VIEWPORT_HEIGHT = 600;

/** 录像参数：12fps 足够审计看清操作过程，码率压住体积（1 小时约 1GB 量级）。 */
const RECORD_FPS = 12;
const RECORD_BITRATE = 2_000_000;
/** 每 5 秒切一个分片：长会话不会把所有数据攒在内存里，异常中断也留得下已录部分。 */
const RECORD_SLICE_MS = 5000;
/** 小于这个体积认为是「没录到东西」（纯黑或极短会话）。 */
const RECORD_MIN_BYTES = 4096;

const RECORDER_MIMES = [
  'video/webm;codecs=vp9',
  'video/webm;codecs=vp8',
  'video/webm',
];

type WasmModule = typeof import('ironrdp-wasm');

/** 录像状态：idle → recording → uploading → saved / failed / skipped。 */
type RecordingState =
  | 'idle'
  | 'recording'
  | 'uploading'
  | 'saved'
  | 'failed'
  | 'skipped';

const parseQuery = (search: string) => {
  const params = new URLSearchParams(search);
  const hostId = Number(params.get('hostId') || 0);
  const accountId = Number(params.get('accountId') || 0);
  return {
    hostId,
    accountId: accountId > 0 ? accountId : undefined,
    title: params.get('title') || '',
  };
};

const pickRecorderMime = (): string | undefined => {
  if (typeof MediaRecorder === 'undefined') {
    return undefined;
  }
  return RECORDER_MIMES.find((type) => MediaRecorder.isTypeSupported(type));
};

const fileNameFor = (hostId: number) =>
  `rdp-${hostId}-${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;

const RdpConsolePage = () => {
  const location = useLocation();
  const query = useRef(parseQuery(location.search)).current;

  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const sessionRef = useRef<import('ironrdp-wasm').Session | null>(null);
  const detachRef = useRef<(() => void) | null>(null);
  const timerRef = useRef<number | undefined>(undefined);

  /** 已经发给远端的视口尺寸，避免重复发 resize。 */
  const viewportRef = useRef<{ width: number; height: number } | null>(null);
  const resizeTimerRef = useRef<number | undefined>(undefined);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const recStartedAtRef = useRef(0);
  /** 有录像正在收尾/上传：倒计时到点时先别关窗，否则录像会丢。 */
  const recPendingRef = useRef(false);

  const countdownRef = useRef<number | undefined>(undefined);
  const remainRef = useRef(0);

  const [status, setStatus] = useState<RdpStatus>('connecting');
  const [error, setError] = useState<string>();
  const [info, setInfo] = useState<RdpSessionInfo>();
  const [elapsed, setElapsed] = useState(0);
  const [viewSize, setViewSize] = useState<{ width: number; height: number }>();
  const [barOpen, setBarOpen] = useState(true);
  const [fullscreen, setFullscreen] = useState(false);
  const [recState, setRecState] = useState<RecordingState>('idle');
  const [recNote, setRecNote] = useState<string>();
  const [endReason, setEndReason] = useState<string>();
  const [remain, setRemain] = useState<number>();
  const [closeRequested, setCloseRequested] = useState(false);
  const [keepOpen, setKeepOpen] = useState(false);
  const [closeHint, setCloseHint] = useState<string>();
  const startedAt = useRef<number>(0);

  /**
   * 把画布尺寸对齐到舞台尺寸，并让远端跟着改分辨率。
   *
   * `force` 用于刚连上时：那时 wasm 已把画布按协商出的 1280×720 摆好，必须无条件覆盖一次。
   */
  const applyViewport = useCallback((force = false) => {
    const stage = stageRef.current;
    const canvas = canvasRef.current;
    if (!stage || !canvas) {
      return;
    }
    const width = Math.max(MIN_VIEWPORT_WIDTH, Math.floor(stage.clientWidth));
    const height = Math.max(
      MIN_VIEWPORT_HEIGHT,
      Math.floor(stage.clientHeight),
    );
    if (
      !force &&
      viewportRef.current?.width === width &&
      viewportRef.current?.height === height
    ) {
      return;
    }
    viewportRef.current = { width, height };
    setViewSize({ width, height });
    // 画布后备缓冲尺寸 = 远端桌面分辨率；CSS 尺寸也是 100%，所以是 1:1 像素映射
    canvas.width = width;
    canvas.height = height;
    const session = sessionRef.current;
    if (!session) {
      return;
    }
    try {
      // 第三参数是 DPI 缩放（null = 100%）；这里只关心逻辑分辨率
      session.resize(width, height, null);
    } catch (err) {
      // 目标机可能不支持动态分辨率（老系统/被策略关掉），此时画面仍是可用的，只是不跟随窗口
      console.error('[rdp] 通知远端调整分辨率失败', err);
    }
  }, []);

  /** 窗口尺寸变化：延后合并处理，避免拖动窗口时每个像素都发一次 resize。 */
  const scheduleViewport = useCallback(() => {
    window.clearTimeout(resizeTimerRef.current);
    resizeTimerRef.current = window.setTimeout(
      () => applyViewport(),
      VIEWPORT_DEBOUNCE_MS,
    );
  }, [applyViewport]);

  /** 开始录像（失败不影响会话本身，只标注「本次没有录像」）。 */
  const startRecording = useCallback((canvas: HTMLCanvasElement) => {
    const mimeType = pickRecorderMime();
    if (!mimeType) {
      setRecState('skipped');
      setRecNote('当前浏览器不支持 MediaRecorder，本次会话没有录像');
      return;
    }
    try {
      const stream = canvas.captureStream(RECORD_FPS);
      const recorder = new MediaRecorder(stream, {
        mimeType,
        videoBitsPerSecond: RECORD_BITRATE,
      });
      chunksRef.current = [];
      recorder.ondataavailable = (event: BlobEvent) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };
      recorder.onerror = () => {
        console.error('[rdp] 录像中断');
      };
      recorder.start(RECORD_SLICE_MS);
      recorderRef.current = recorder;
      recStartedAtRef.current = Date.now();
      setRecState('recording');
      setRecNote(
        `录像中（${mimeType.replace('video/', '')}，${RECORD_FPS}fps）`,
      );
    } catch (err) {
      console.error('[rdp] 无法开始录像', err);
      setRecState('failed');
      setRecNote(`录像未能开始：${describeRdpError(err)}`);
    }
  }, []);

  /**
   * 收尾录像并上传，返回「上传已结束」的 Promise（倒计时关窗要等它）。
   *
   * 二进制走 multipart 交给堡垒机落盘，后端建 `RdpRecording` 行 + 写审计；
   * 上传失败不影响审计链（会话本身的开/关两条审计早在服务端写好了）。
   */
  const finishRecording = useCallback((): Promise<void> => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === 'inactive') {
      return Promise.resolve();
    }
    const durationSeconds = Math.max(
      1,
      Math.round((Date.now() - recStartedAtRef.current) / 1000),
    );
    const startedAtIso = new Date(recStartedAtRef.current).toISOString();
    const hostId = query.hostId;
    recPendingRef.current = true;
    return new Promise<void>((resolve) => {
      const settle = () => {
        recPendingRef.current = false;
        resolve();
      };
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, {
          type: recorder.mimeType || 'video/webm',
        });
        chunksRef.current = [];
        recorderRef.current = null;
        if (blob.size < RECORD_MIN_BYTES) {
          setRecState('skipped');
          setRecNote('会话过短，没有录到有效画面');
          settle();
          return;
        }
        setRecState('uploading');
        setRecNote(
          `正在上传录像（${(blob.size / 1048576).toFixed(1)} MB，${durationSeconds} 秒）…`,
        );
        const canvas = canvasRef.current;
        rdpApi
          .uploadRecording(blob, {
            filename: fileNameFor(hostId),
            hostId,
            durationSeconds,
            startedAt: startedAtIso,
            width: canvas?.width,
            height: canvas?.height,
          })
          .then((saved) => {
            setRecState('saved');
            setRecNote(
              `录像已保存（#${saved.id}，${durationSeconds} 秒，可在「审计中心 · 远程桌面录像」回看）`,
            );
          })
          .catch((err: unknown) => {
            console.error('[rdp] 录像上传失败', err);
            setRecState('failed');
            setRecNote(`录像上传失败：${describeRdpError(err)}`);
          })
          .finally(settle);
      };
      try {
        recorder.stop();
      } catch (err) {
        console.error('[rdp] 停止录像失败', err);
        recorderRef.current = null;
        settle();
      }
    });
  }, [query.hostId]);

  /** 关不掉窗口时（浏览器只允许关闭脚本打开的窗口）给出提示，别让用户以为卡住了。 */
  const closeWindow = useCallback(() => {
    window.clearInterval(countdownRef.current);
    countdownRef.current = undefined;
    setRemain(0);
    window.close();
    window.setTimeout(() => {
      setCloseHint('浏览器拒绝自动关闭本窗口，请手动关闭（Ctrl/Cmd + W）。');
    }, 600);
  }, []);

  /** 会话断开后的关窗倒计时（与 Linux 终端断开后的行为一致）。 */
  const startCloseCountdown = useCallback(() => {
    if (countdownRef.current !== undefined) {
      return;
    }
    remainRef.current = CLOSE_COUNTDOWN_SECONDS;
    setRemain(remainRef.current);
    countdownRef.current = window.setInterval(() => {
      remainRef.current -= 1;
      setRemain(remainRef.current);
      if (remainRef.current > 0) {
        return;
      }
      window.clearInterval(countdownRef.current);
      countdownRef.current = undefined;
      setCloseRequested(true);
    }, 1000);
  }, []);

  /** 用户点了「留在本窗口」：停掉倒计时，窗口保持打开（还能点重新连接）。 */
  const cancelCloseCountdown = useCallback(() => {
    window.clearInterval(countdownRef.current);
    countdownRef.current = undefined;
    remainRef.current = 0;
    setRemain(undefined);
    setCloseRequested(false);
    setKeepOpen(true);
  }, []);

  /** 断开当前会话（幂等）：解绑输入、关掉 wasm 会话、清掉定时器。 */
  const teardown = useCallback(() => {
    window.clearTimeout(timerRef.current);
    detachRef.current?.();
    detachRef.current = null;
    const session = sessionRef.current;
    sessionRef.current = null;
    if (session) {
      try {
        session.shutdown();
      } catch {
        // 会话已结束时 shutdown 会抛，忽略
      }
    }
  }, []);

  /** 会话结束（正常结束、目标机断开、用户主动断开）的统一出口。 */
  const handleSessionEnd = useCallback(
    (reason: string) => {
      setStatus('disconnected');
      setEndReason(reason);
      void finishRecording();
      startCloseCountdown();
    },
    [finishRecording, startCloseCountdown],
  );

  const connect = useCallback(async () => {
    const canvas = canvasRef.current;
    if (!canvas || !query.hostId) {
      setStatus('failed');
      setError('缺少 hostId 参数：请从「网页终端」列表点「连接」进入本窗口。');
      return;
    }

    // 重新连接：清掉上一轮的倒计时/关窗弹窗与录像状态（正在上传的录像不重置，让它自己落地）
    window.clearInterval(countdownRef.current);
    countdownRef.current = undefined;
    remainRef.current = 0;
    setRemain(undefined);
    setCloseRequested(false);
    setKeepOpen(false);
    setCloseHint(undefined);
    setEndReason(undefined);
    if (!recPendingRef.current) {
      setRecState('idle');
      setRecNote(undefined);
    }
    viewportRef.current = null;

    teardown();
    setError(undefined);
    setStatus('connecting');

    window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      setStatus((prev) => {
        if (prev === 'connecting') {
          setError(
            '协商超时（45 秒）：目标机可能没开远程桌面、3389 被防火墙拦了，或堡垒机到目标机的网络不通。',
          );
          return 'failed';
        }
        return prev;
      });
      teardown();
    }, NEGOTIATION_TIMEOUT_MS);

    try {
      // 1) 换票：服务端在这一步复验权限 / 授权开关 / 账号，并写下审计
      const session = await rdpApi.create(query.hostId, query.accountId);
      setInfo(session);

      // 2) 载入 wasm（4MB，按需加载）并显式指定 wasm 路径：打包后 import.meta.url 会指向产物目录
      const rdp = (await import('ironrdp-wasm')) as WasmModule;
      await rdp.default({ module_or_path: '/rdp_client_bg.wasm' });
      rdp.setup('info');

      // 3) 组会话：proxyAddress 指向堡垒机的 RDCleanPath 隧道
      const builder = new rdp.SessionBuilder();
      builder
        .username(session.credential.username)
        .password(session.credential.password)
        .destination(`${session.host.address}:${session.host.port}`)
        .proxyAddress(buildRdpWsUrl(session.wsPath))
        .authToken('none')
        .desktopSize(new rdp.DesktopSize(1280, 720))
        .extension(new rdp.Extension('enable_credssp', true))
        .renderCanvas(canvas);
      builder.setCursorStyleCallbackContext(canvas);
      builder.setCursorStyleCallback((style: string | undefined) => {
        canvas.style.cursor = style || 'default';
      });

      const live = await builder.connect();
      sessionRef.current = live;

      // 4) 画布对齐窗口大小：先按远端协商结果摆好，再要求远端改成窗口大小（分辨率跟随窗口）
      const desktop = live.desktopSize();
      canvas.width = desktop.width;
      canvas.height = desktop.height;
      applyViewport(true);
      canvas.focus();

      detachRef.current = attachInput(canvas, live, rdp);
      startRecording(canvas);

      window.clearTimeout(timerRef.current);
      startedAt.current = Date.now();
      setStatus('connected');

      live
        .run()
        .then((termination: { reason: () => string }) => {
          const reason = (() => {
            try {
              return termination?.reason?.() || '';
            } catch {
              return '';
            }
          })();
          handleSessionEnd(reason ? `会话已结束：${reason}` : '会话已结束');
          teardown();
        })
        .catch((err: unknown) => {
          // wasm 的错误对象只在这里“见得到原形”，留着方便排障（界面上给的是人话）
          console.error('[rdp] 会话异常', err);
          const text = describeRdpError(err);
          setError(text);
          setStatus('failed');
          void finishRecording();
          teardown();
        });
    } catch (err) {
      window.clearTimeout(timerRef.current);
      console.error('[rdp] 连接失败', err);
      setError(describeRdpError(err));
      setStatus('failed');
      teardown();
    }
  }, [
    applyViewport,
    finishRecording,
    handleSessionEnd,
    query,
    startRecording,
    teardown,
  ]);

  useEffect(() => {
    connect();
    return () => {
      window.clearTimeout(timerRef.current);
      window.clearTimeout(resizeTimerRef.current);
      window.clearInterval(countdownRef.current);
      detachRef.current?.();
      detachRef.current = null;
      const session = sessionRef.current;
      sessionRef.current = null;
      if (session) {
        try {
          session.shutdown();
        } catch {
          // 关窗时无需处理
        }
      }
    };
    // 只在进入窗口时连一次；重连走「重新连接」按钮
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 舞台尺寸变化（拖动窗口、进出全屏）→ 让被控主机也跟着改分辨率
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || typeof ResizeObserver === 'undefined') {
      return undefined;
    }
    const observer = new ResizeObserver(() => scheduleViewport());
    observer.observe(stage);
    return () => observer.disconnect();
  }, [scheduleViewport]);

  // 全屏切换后舞台尺寸会变，补一次对齐（并同步按钮图标）
  useEffect(() => {
    const onChange = () => {
      setFullscreen(Boolean(document.fullscreenElement));
      scheduleViewport();
    };
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, [scheduleViewport]);

  useEffect(() => {
    if (status !== 'connected') {
      return undefined;
    }
    const tick = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt.current) / 1000));
    }, 1000);
    return () => window.clearInterval(tick);
  }, [status]);

  // 倒计时数到 0 → 关窗；但录像还在上传时先等它落地，别把审计证据丢了
  useEffect(() => {
    if (closeRequested && !recPendingRef.current) {
      closeWindow();
    }
  }, [closeRequested, recState, closeWindow]);

  /** 全屏：对整个控制台窗口全屏（工具条留着，方便退出全屏），而不是只放大画布。 */
  const toggleFullscreen = useCallback(() => {
    const target = wrapperRef.current;
    if (!target) {
      return;
    }
    if (document.fullscreenElement) {
      void document.exitFullscreen?.();
      return;
    }
    void target.requestFullscreen?.().catch((err: unknown) => {
      console.error('[rdp] 全屏被拒绝', err);
    });
  }, []);

  const badge = RDP_STATUS_META[status];
  const duration = `${String(Math.floor(elapsed / 60)).padStart(2, '0')}:${String(elapsed % 60).padStart(2, '0')}`;

  return (
    <div className="bastion-rdp" ref={wrapperRef}>
      <div className={`bastion-rdp-bar${barOpen ? '' : ' is-collapsed'}`}>
        {barOpen ? (
          <>
            <Space size={12} wrap>
              <span className="bastion-rdp-title">
                远程桌面 ·{' '}
                {query.title || info?.host.hostName || `主机 #${query.hostId}`}
              </span>
              <OsTag osType={info?.host.osType || 'windows'} />
              <Text type="secondary">
                {info ? `${info.host.address}:${info.host.port}` : ''}
                {info ? ` · 账号 ${info.account.username}` : ''}
              </Text>
              {viewSize ? (
                <Text type="secondary">
                  {viewSize.width}×{viewSize.height}
                </Text>
              ) : null}
            </Space>
            <Space size={8}>
              <Tag color={badge.color}>{badge.text}</Tag>
              {recState === 'recording' ? <Tag color="red">录制中</Tag> : null}
              {status === 'connected' ? (
                <Text type="secondary">已连接 {duration}</Text>
              ) : null}
              <Tooltip title={fullscreen ? '退出全屏' : '全屏显示远端桌面'}>
                <Button
                  size="small"
                  icon={
                    fullscreen ? (
                      <FullscreenExitOutlined />
                    ) : (
                      <FullscreenOutlined />
                    )
                  }
                  disabled={status !== 'connected'}
                  onClick={toggleFullscreen}
                />
              </Tooltip>
              <Tooltip title="收起工具条">
                <Button
                  size="small"
                  icon={<UpOutlined />}
                  onClick={() => {
                    setBarOpen(false);
                    // 收起后舞台变大，重新对齐一次
                    window.setTimeout(() => scheduleViewport(), 0);
                  }}
                />
              </Tooltip>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => {
                  cancelCloseCountdown();
                  void connect();
                }}
                disabled={status === 'connecting'}
              >
                重新连接
              </Button>
              <Button
                size="small"
                danger
                icon={<DisconnectOutlined />}
                disabled={status !== 'connected'}
                onClick={() => {
                  teardown();
                  handleSessionEnd('你已主动断开远程桌面');
                }}
              >
                断开
              </Button>
              <Button
                size="small"
                icon={<CloseOutlined />}
                onClick={() => window.close()}
              >
                关闭窗口
              </Button>
            </Space>
          </>
        ) : (
          <Space size={6}>
            <Tooltip title="展开工具条">
              <Button
                size="small"
                type="text"
                icon={<DownOutlined />}
                onClick={() => {
                  setBarOpen(true);
                  window.setTimeout(() => scheduleViewport(), 0);
                }}
              />
            </Tooltip>
            <Tag color={badge.color}>{badge.text}</Tag>
            {recState === 'recording' ? <Tag color="red">录制中</Tag> : null}
          </Space>
        )}
      </div>

      {error ? (
        <Alert
          className="bastion-rdp-alert"
          type="error"
          showIcon
          message="连接失败"
          description={error}
          action={
            <Button
              size="small"
              onClick={() => {
                cancelCloseCountdown();
                void connect();
              }}
            >
              重试
            </Button>
          }
        />
      ) : null}

      {keepOpen && endReason ? (
        <Alert
          className="bastion-rdp-alert"
          type="warning"
          showIcon
          message={endReason}
          description="已取消自动关闭。本窗口会一直留着，可以点「重新连接」继续，或点「关闭窗口」结束。"
          action={
            <Button
              size="small"
              onClick={() => {
                cancelCloseCountdown();
                void connect();
              }}
            >
              重新连接
            </Button>
          }
        />
      ) : null}

      <div className="bastion-rdp-stage" ref={stageRef}>
        {status === 'connecting' ? (
          <div className="bastion-rdp-overlay">
            <Spin />
            <span>正在与 {info?.host.address || '目标机'} 协商 RDP 会话…</span>
          </div>
        ) : null}
        <canvas ref={canvasRef} tabIndex={0} className="bastion-rdp-canvas" />
      </div>

      <Modal
        open={endReason !== undefined && !keepOpen}
        title="远程桌面会话已结束"
        closable={false}
        maskClosable={false}
        keyboard={false}
        footer={[
          <Button key="stay" onClick={cancelCloseCountdown}>
            留在本窗口
          </Button>,
          <Button key="close" type="primary" danger onClick={closeWindow}>
            立即关闭
          </Button>,
        ]}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Text>{endReason}</Text>
          <Text type="secondary">
            本窗口将在 {remain ?? 0} 秒后自动关闭（与 Linux
            网页终端断开后的行为一致）。
          </Text>
          {recNote ? <Text type="secondary">录像：{recNote}</Text> : null}
          {closeHint ? <Text type="danger">{closeHint}</Text> : null}
        </Space>
      </Modal>
    </div>
  );
};

export default RdpConsolePage;
