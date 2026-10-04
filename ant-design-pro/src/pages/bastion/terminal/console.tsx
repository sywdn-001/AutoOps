/**
 * 网页终端控制台（**`window.open()` 弹出的独立窗口**，路由 `/terminal/console`，`layout: false`）。
 *
 * 形态对齐 JumpServer：资产列表里点「连接」后弹出一个独立窗口，一个窗口只跑一条
 * 审计会话（不再把多个会话挤在同一页里，也不再实时铺审计/命令日志——那些去审计中心看）。
 * 本页只做三件事：进终端、给快捷键、把会话状态摆在状态条上。
 */
import {
  DisconnectOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  FolderOpenOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  HomeOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from '@ant-design/icons';
import { history, useAccess, useModel, useSearchParams } from '@umijs/max';
import {
  Button,
  Checkbox,
  Input,
  type InputRef,
  Modal,
  message,
  Result,
  Space,
  Spin,
  Tag,
  Tooltip,
} from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { terminalApi } from '@/services/bastion/endpoints';
import ConsoleStatusBar from './components/ConsoleStatusBar';
import TerminalPane, { type TerminalHandle } from './components/TerminalPane';
import './console.css';
import {
  buildFileConsoleUrl,
  clampFontSize,
  consoleWindowFeatures,
  MAX_FONT_SIZE,
  MIN_FONT_SIZE,
  STATUS_META,
} from './types';
import { useTerminalSession } from './useTerminalSession';

/** 会话断开后，本窗口自动关闭的倒计时秒数。 */
const CLOSE_COUNTDOWN_SECONDS = 10;

const SHORTCUTS: Array<[string, string]> = [
  ['Ctrl / Cmd + F', '搜索终端输出'],
  ['Ctrl / Cmd + Shift + C', '复制选中内容'],
  ['Ctrl（单独按）', '发送中断信号（SIGINT）'],
  ['Ctrl / Cmd + V', '粘贴（走浏览器剪贴板权限）'],
  ['Ctrl / Cmd + Shift + F', '工作区全屏 / 退出全屏'],
  ['Ctrl / Cmd + Shift + P', '纯净模式（隐藏状态条）'],
  ['Ctrl / Cmd + Shift + A', '回到资产列表（关闭本窗口）'],
  ['Ctrl / Cmd + +/-', '字号放大 / 缩小'],
  ['Esc（长按 800ms）', '退出纯净模式与全屏'],
];

/**
 * 回到资产列表。
 *
 * 终端页是前台用 `window.open()` 弹出的窗口，`window.opener` 指向原来的资产列表窗口。
 * 历史实现只调 `opener.focus()`：浏览器常常忽略这种跨窗口聚焦请求，用户点了「资产列表」
 * 屏幕上什么都不会发生（看起来按钮坏了）。现在改成「能关就关，关不掉就把本窗口变成列表」，
 * 两种路径都一定有可见结果：
 *
 * 1. 有 opener → 聚焦原窗口并关闭本窗口（回到列表窗口，占位不重复）；
 * 2. `window.close()` 被拒（浏览器只允许关闭脚本打开的窗口，或用户手动开的本页）→
 *    本窗口直接跳到资产列表页；
 * 3. 没有 opener（手工复制链接打开）→ 同样在本窗口跳到资产列表页，不再新开第三个窗口。
 */
const backToLauncher = () => {
  const opener = window.opener as Window | null;
  if (opener && !opener.closed) {
    try {
      opener.focus();
      window.close();
      // 关闭成功时下面这行随页面卸载一起消失；被拒时才生效，兜底本窗口跳转。
      window.setTimeout(() => window.location.assign('/terminal'), 200);
      return;
    } catch {
      // 跨窗口访问被拒，走本窗口跳转
    }
  }
  window.location.assign('/terminal');
};

/**
 * 工作区全屏开关。
 *
 * 终端页是 `window.open()` 弹出的独立窗口，页面本来就铺满整个视口，只切 CSS class
 * （`position: fixed; inset: 0`）在视觉上没有任何变化，用户点了「全屏」会以为没反应。
 * 这里优先调用浏览器全屏 API 真正把窗口切进全屏；浏览器不支持或拒绝时，才退回 CSS 工作区全屏。
 */
const toggleWorkspaceFullscreen = (
  setCssFullscreen: (updater: (prev: boolean) => boolean) => void,
) => {
  if (typeof document === 'undefined') {
    return;
  }
  if (document.fullscreenElement) {
    void document.exitFullscreen().catch(() => undefined);
    return;
  }
  const root = document.documentElement;
  if (typeof root.requestFullscreen === 'function') {
    void root.requestFullscreen().catch(() => {
      setCssFullscreen((prev) => !prev);
    });
    return;
  }
  setCssFullscreen((prev) => !prev);
};

const TerminalConsolePage = () => {
  const access = useAccess();
  const [params] = useSearchParams();
  const hostId = Number(params.get('hostId') ?? '');
  const accountId = Number(params.get('accountId') ?? '');
  const urlTitle = params.get('title') ?? '';
  // 一台主机多协议：入口页会把要开的端点写在 URL 上（ssh / winrm）；
  // 不带就交给服务端按主机的主端点挑（老链接、手改 URL 都不能炸）。
  const urlProtocol = (params.get('protocol') ?? '').trim().toLowerCase();
  const username = useModel('@@initialState').initialState?.currentUser?.name;

  const [preparing, setPreparing] = useState(true);
  const [error, setError] = useState<string>();
  const [fullscreen, setFullscreen] = useState(false);
  const [focusMode, setFocusMode] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [term, setTerm] = useState('');
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [wholeWord, setWholeWord] = useState(false);
  const [regex, setRegex] = useState(false);
  const searchRef = useRef<InputRef>(null);
  const escTimer = useRef<number | undefined>(undefined);
  const countdownTimer = useRef<number | undefined>(undefined);
  // useTerminalSession 的 onClosed 在 hook 调用时就绑定了，用 ref 转发到下面定义的倒计时
  const startCloseCountdownRef = useRef<() => void>(() => undefined);

  const {
    session,
    connect,
    disconnect,
    reconnect,
    setFontSize,
    sendInput,
    sendResize,
    registerHandle,
    getHandle,
    write,
  } = useTerminalSession({
    onClosed: (reason) => {
      message.info(`会话已结束：${reason}`);
      // 会话在服务端结束（超时、被管理员踢掉、目标机断开）同样走关窗倒计时
      startCloseCountdownRef.current();
    },
  });

  // 准入校验 + 建连（只在 URL 参数变化时跑一次；URL 可以被手工改，所以后端再验一遍）
  useEffect(() => {
    let cancelled = false;
    const prepare = async () => {
      if (!access.canTerminalUse) {
        setError('当前账号没有网页终端权限（terminal:use），请联系管理员。');
        setPreparing(false);
        return;
      }
      if (!(hostId > 0) || !(accountId > 0)) {
        setError(
          '缺少连接受参（hostId / accountId），请从「网页终端」资产列表点「连接」打开本页。',
        );
        setPreparing(false);
        return;
      }
      try {
        const targets = await terminalApi.targets();
        // 一台主机多协议时后端是「一个端点一条」，这里要按 URL 上的 protocol 挑那一条
        const visible = targets.filter((item) => item.hostId === hostId);
        const target = urlProtocol
          ? visible.find(
              (item) => (item.protocol || 'ssh').toLowerCase() === urlProtocol,
            )
          : visible[0];
        if (!target) {
          throw new Error(
            urlProtocol
              ? `这台主机没有「${urlProtocol}」这个入口（hostId=${hostId}）。`
              : `主机不在你的授权范围内（hostId=${hostId}）。`,
          );
        }
        const account = target.accounts.find((item) => item.id === accountId);
        if (!account) {
          throw new Error('该主机下找不到指定的资产账号，可能授权已被回收。');
        }
        const check = await terminalApi.check(
          hostId,
          accountId,
          target.protocol,
        );
        if (!check.allowed) {
          throw new Error(check.reason || '准入校验未通过。');
        }
        if (cancelled) {
          return;
        }
        connect({ target, accountId, accountUsername: account.username });
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) {
          setPreparing(false);
        }
      }
    };
    void prepare();
    return () => {
      cancelled = true;
    };
  }, [hostId, accountId, urlProtocol, access.canTerminalUse]);

  // 标题：方便在多个终端窗口之间辨认（对齐 JumpServer 的标签标题口径）
  useEffect(() => {
    const name = session?.hostName || urlTitle;
    document.title = name
      ? `${name} · 网页终端 · AutoOps 堡垒机`
      : '网页终端 · AutoOps 堡垒机';
  }, [session?.hostName, urlTitle]);

  // 全屏状态也可能被浏览器自己改掉（F11、Esc 退出全屏），同步回按钮图标与工作区 class
  useEffect(() => {
    const sync = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', sync);
    return () => document.removeEventListener('fullscreenchange', sync);
  }, []);

  // 搜索浮层打开后自动聚焦
  useEffect(() => {
    if (showSearch) {
      searchRef.current?.focus();
    }
  }, [showSearch]);

  // Ctrl/Cmd+Shift+F / +P、Esc 长按（capture 阶段，避免被 xterm 吞掉）
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const mod = event.ctrlKey || event.metaKey;
      if (mod && event.shiftKey && event.code === 'KeyF') {
        event.preventDefault();
        event.stopImmediatePropagation();
        toggleWorkspaceFullscreen(setFullscreen);
        return;
      }
      if (mod && event.shiftKey && event.code === 'KeyP') {
        event.preventDefault();
        event.stopImmediatePropagation();
        setFocusMode((prev) => !prev);
        return;
      }
      if (mod && event.shiftKey && event.code === 'KeyA') {
        event.preventDefault();
        event.stopImmediatePropagation();
        backToLauncher();
        return;
      }
      if (event.key === 'Escape' && !event.repeat) {
        escTimer.current = window.setTimeout(() => {
          setFocusMode(false);
          if (document.fullscreenElement) {
            void document.exitFullscreen().catch(() => undefined);
          }
          setFullscreen(false);
        }, 800);
      }
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && escTimer.current) {
        window.clearTimeout(escTimer.current);
        escTimer.current = undefined;
      }
    };
    window.addEventListener('keydown', onKeyDown, true);
    window.addEventListener('keyup', onKeyUp, true);
    return () => {
      window.removeEventListener('keydown', onKeyDown, true);
      window.removeEventListener('keyup', onKeyUp, true);
      if (escTimer.current) {
        window.clearTimeout(escTimer.current);
      }
    };
  }, []);

  /**
   * 断开后的关窗倒计时（需求：会话断开后终端里显示「本窗口将在 10s 后关闭」并逐秒倒数）。
   *
   * 用 `\r\x1b[K` 原地刷新同一行，看起来是慢慢倒数而不是刷满屏幕；数到 0 再 `window.close()`。
   * 浏览器只允许关闭脚本自己打开的窗口，关不掉时在终端补一行提示，让用户知道该手动关，
   * 而不是以为程序卡死。
   */
  const startCloseCountdown = useCallback(() => {
    if (countdownTimer.current !== undefined) {
      return;
    }
    let remain = CLOSE_COUNTDOWN_SECONDS;
    const tick = () => {
      write(
        `\r\x1b[K\x1b[33m[堡垒机] 本窗口将在 ${remain} 秒后自动关闭…\x1b[0m`,
      );
      getHandle()?.scrollToBottom();
      if (remain <= 0) {
        countdownTimer.current = undefined;
        write(
          '\r\x1b[K\x1b[33m[堡垒机] 倒计时结束，正在关闭本窗口…\x1b[0m\r\n',
        );
        getHandle()?.scrollToBottom();
        window.setTimeout(() => {
          window.close();
          window.setTimeout(() => {
            write(
              '\r\n\x1b[33m[堡垒机] 浏览器拒绝自动关闭，请手动关闭本窗口（Ctrl/Cmd + W）。\x1b[0m\r\n',
            );
            getHandle()?.scrollToBottom();
          }, 600);
        }, 200);
        return;
      }
      remain -= 1;
      countdownTimer.current = window.setTimeout(tick, 1000);
    };
    tick();
  }, [getHandle, write]);

  /** 用户改主意点了「重新连接」：停掉倒计时，别把正在用的窗口关了。 */
  const cancelCloseCountdown = useCallback(() => {
    if (countdownTimer.current === undefined) {
      return;
    }
    window.clearTimeout(countdownTimer.current);
    countdownTimer.current = undefined;
    write('\r\x1b[K\x1b[33m[堡垒机] 已取消自动关闭本窗口。\x1b[0m\r\n');
    getHandle()?.scrollToBottom();
  }, [getHandle, write]);

  useEffect(() => {
    startCloseCountdownRef.current = startCloseCountdown;
  }, [startCloseCountdown]);

  // 关窗/切页时清掉倒计时，不留悬挂定时器
  useEffect(
    () => () => {
      window.clearTimeout(countdownTimer.current);
      countdownTimer.current = undefined;
    },
    [],
  );

  const searchOptions = useCallback(
    () => ({ caseSensitive, wholeWord, regex }),
    [caseSensitive, wholeWord, regex],
  );
  const runSearch = useCallback(
    (direction: 1 | -1) => {
      if (!term) {
        getHandle()?.clearSearch();
        return;
      }
      if (direction === 1) {
        getHandle()?.findNext(term, searchOptions());
      } else {
        getHandle()?.findPrevious(term, searchOptions());
      }
    },
    [getHandle, searchOptions, term],
  );

  const closeSearch = useCallback(() => {
    setShowSearch(false);
    getHandle()?.clearSearch();
  }, [getHandle]);

  const onReady = useCallback(
    (handle: TerminalHandle) => {
      registerHandle(handle);
      handle.focus();
    },
    [registerHandle],
  );

  const changeFont = useCallback(
    (delta: number) => {
      if (!session) {
        return;
      }
      setFontSize(session.fontSize + delta);
    },
    [session, setFontSize],
  );

  /**
   * 打开文件管理器：**新开一个独立窗口**（和本终端窗口同形态），窗口里自己再去
   * 后端开一条 SFTP 会话。这里只是换一个窗口，不做任何文件操作。
   */
  const openFileManager = useCallback(() => {
    const url = buildFileConsoleUrl({
      hostId,
      accountId,
      hostName: session?.hostName || urlTitle,
    });
    const name = `bastion-files-${hostId}-${accountId}-${Date.now()}`;
    const opened = window.open(url, name, consoleWindowFeatures());
    if (opened) {
      opened.focus();
    } else {
      message.warning(
        '浏览器拦截了文件管理器弹窗，请允许本站点弹出窗口后重试。',
      );
    }
  }, [hostId, accountId, session?.hostName, urlTitle]);

  const status = session ? STATUS_META[session.status] : undefined;
  const connected = session?.status === 'connected';
  // 就绪前也要把按键送出去：服务端在 `open_session()` 返回之前会把按键攒进 early_input、
  // 就绪后按序回放；客户端若在这里丢弃，用户「窗口一打开就敲」的第一条命令会静默消失
  // （表现就是「敲了命令没有任何输出」）。原先只认 `connected`，Windows（WinRM）主机
  // 建连更慢，所以更容易踩到。
  const inputEnabled =
    session !== undefined &&
    session.status !== 'failed' &&
    session.status !== 'disconnected';
  // 会话刚建立时把焦点收回终端：本页是 `window.open()` 弹出的窗口，用户开始敲键盘时
  // 焦点可能还停在别处 —— 表现同样是「敲了没反应」。
  useEffect(() => {
    if (session?.status === 'connected') {
      getHandle()?.focus();
    }
  }, [getHandle, session?.status]);
  const fontSize = session?.fontSize ?? 13;
  // WinRM（Windows）会话没有 SFTP 通道：文件管理器按钮直接不渲染，
  // 而不是留一颗永远点不动的灰按钮。
  const isWinrm = session?.protocol === 'winrm';
  const filesTip = isWinrm
    ? 'Windows（WinRM）会话不支持文件传输，需要传文件请改用 Linux 主机或远程桌面'
    : !access.canFileUse
      ? '当前账号没有文件管理器权限（file:use）'
      : !session?.sid
        ? '终端会话建立后才能打开文件管理器'
        : '在同一台资产上新开一个文件管理器窗口（SFTP）';

  if (error) {
    return (
      <div className="bastion-console-page bastion-console-centered">
        <Result
          status="warning"
          title="无法建立终端会话"
          subTitle={error}
          extra={
            <Space>
              <Button type="primary" onClick={() => history.push('/terminal')}>
                返回资产列表
              </Button>
              <Button onClick={() => window.close()}>关闭本窗口</Button>
            </Space>
          }
        />
      </div>
    );
  }

  return (
    <div
      className={[
        'bastion-console-page',
        fullscreen ? 'bastion-console-fullscreen' : '',
        focusMode ? 'bastion-console-focus' : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className="bastion-toolbar">
        <Tooltip title="回到资产列表（关闭本窗口）">
          <Button
            className="bastion-toolbar-back"
            size="small"
            type="text"
            icon={<HomeOutlined />}
            onClick={backToLauncher}
          >
            资产列表
          </Button>
        </Tooltip>
        <span className="bastion-toolbar-asset" title={session?.hostName}>
          {session?.hostName || urlTitle || '连接中…'}
        </span>
        {status ? <Tag color={status.color}>{status.text}</Tag> : null}
        {session?.accountUsername ? (
          <span className="bastion-toolbar-chip">
            账号 {session.accountUsername}
          </span>
        ) : null}
        {connected && session?.sid ? (
          <span className="bastion-toolbar-chip" title="会话号">
            {session.sid}
          </span>
        ) : null}
        {isWinrm ? null : (
          <Tooltip title={filesTip}>
            <Button
              className="bastion-toolbar-files"
              size="small"
              type="text"
              icon={<FolderOpenOutlined />}
              disabled={!access.canFileUse || !session?.sid}
              onClick={openFileManager}
            >
              文件管理
            </Button>
          </Tooltip>
        )}
        <span className="bastion-toolbar-spacer" />
        <Tooltip title="重新连接">
          <Button
            size="small"
            type="text"
            icon={<ReloadOutlined />}
            onClick={() => {
              cancelCloseCountdown();
              reconnect();
            }}
          />
        </Tooltip>
        <Tooltip title="搜索（Ctrl/Cmd + F）">
          <Button
            size="small"
            type="text"
            icon={<SearchOutlined />}
            onClick={() => setShowSearch((prev) => !prev)}
          />
        </Tooltip>
        <Tooltip title="缩小字号">
          <Button
            size="small"
            type="text"
            icon={<ZoomOutOutlined />}
            disabled={fontSize <= MIN_FONT_SIZE}
            onClick={() => changeFont(-1)}
          />
        </Tooltip>
        <span className="bastion-toolbar-chip">{fontSize}px</span>
        <Tooltip title="放大字号">
          <Button
            size="small"
            type="text"
            icon={<ZoomInOutlined />}
            disabled={fontSize >= MAX_FONT_SIZE}
            onClick={() => changeFont(1)}
          />
        </Tooltip>
        <Tooltip title={focusMode ? '退出纯净模式' : '纯净模式'}>
          <Button
            size="small"
            type="text"
            icon={focusMode ? <EyeInvisibleOutlined /> : <EyeOutlined />}
            onClick={() => setFocusMode((prev) => !prev)}
          />
        </Tooltip>
        <Tooltip
          title={fullscreen ? '退出全屏（Ctrl/Cmd + Shift + F）' : '工作区全屏'}
        >
          <Button
            className="bastion-toolbar-fullscreen"
            size="small"
            type="text"
            icon={
              fullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />
            }
            onClick={() => toggleWorkspaceFullscreen(setFullscreen)}
          />
        </Tooltip>
        <Tooltip title="快捷键说明">
          <Button
            size="small"
            type="text"
            icon={<QuestionCircleOutlined />}
            onClick={() => setShowHelp(true)}
          />
        </Tooltip>
        <Tooltip title="断开会话">
          <Button
            className="bastion-toolbar-disconnect"
            size="small"
            type="text"
            danger
            icon={<DisconnectOutlined />}
            disabled={!connected}
            onClick={() => {
              disconnect();
              startCloseCountdown();
            }}
          >
            断开
          </Button>
        </Tooltip>
      </div>

      {showSearch ? (
        <div className="bastion-searchbar">
          <Input
            ref={searchRef}
            size="small"
            allowClear
            placeholder="在终端输出里搜索（Enter 下一个 / Shift+Enter 上一个）"
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            onPressEnter={(event) => runSearch(event.shiftKey ? -1 : 1)}
          />
          <Button size="small" onClick={() => runSearch(-1)}>
            上一个
          </Button>
          <Button size="small" onClick={() => runSearch(1)}>
            下一个
          </Button>
          <Checkbox
            checked={caseSensitive}
            onChange={(event) => setCaseSensitive(event.target.checked)}
          >
            区分大小写
          </Checkbox>
          <Checkbox
            checked={wholeWord}
            onChange={(event) => setWholeWord(event.target.checked)}
          >
            全词
          </Checkbox>
          <Checkbox
            checked={regex}
            onChange={(event) => setRegex(event.target.checked)}
          >
            正则
          </Checkbox>
          <Button size="small" type="text" onClick={closeSearch}>
            关闭
          </Button>
        </div>
      ) : null}

      <div className="bastion-terminal-area">
        {preparing ? (
          <div className="bastion-terminal-empty">
            <Spin />
            <span className="bastion-terminal-empty-title">
              正在校验权限并建立审计会话…
            </span>
          </div>
        ) : null}
        {session ? (
          <TerminalPane
            active
            fontSize={session.fontSize}
            inputEnabled={inputEnabled}
            connected={connected}
            onData={sendInput}
            onResize={sendResize}
            onReady={onReady}
            onSearchRequest={() => setShowSearch(true)}
            onFontSizeChange={(next) => setFontSize(clampFontSize(next))}
            onSwitchTab={() => undefined}
            onFullscreenToggle={() => toggleWorkspaceFullscreen(setFullscreen)}
            onFocusModeToggle={() => setFocusMode((prev) => !prev)}
            onDisconnect={() => {
              disconnect();
              startCloseCountdown();
            }}
            onReconnect={() => {
              cancelCloseCountdown();
              reconnect();
            }}
          />
        ) : null}
      </div>

      <ConsoleStatusBar
        session={session}
        username={username}
        channelOnline={connected || session?.status === 'connecting'}
      />

      <Modal
        open={showHelp}
        title="控制台快捷键"
        footer={null}
        onCancel={() => setShowHelp(false)}
      >
        <ul className="bastion-shortcut-list">
          {SHORTCUTS.map(([keys, text]) => (
            <li key={keys}>
              <code>{keys}</code>
              <span>{text}</span>
            </li>
          ))}
        </ul>
        <p className="bastion-shortcut-tip">
          在终端里点右键还能复制、粘贴、全选、清空屏幕与调整字号。退出会话请点工具栏「断开」，
          断开后终端会显示 10
          秒倒计时并自动关闭本窗口；倒计时结束前点「重新连接」可以继续使用本窗口。
        </p>
      </Modal>
    </div>
  );
};

export default TerminalConsolePage;
