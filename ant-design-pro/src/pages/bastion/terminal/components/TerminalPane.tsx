/**
 * 单个控制台窗格的 xterm 实现。
 *
 * 对齐 JumpServer 控制台（luna）的这几个细节：
 *  - 渲染参数：`cursorBlink`、`cursorStyle: 'block'`、`scrollback: 5000`、
 *    `minimumContrastRatio: 4.5`、`rightClickSelectsWord`（`useTerminalSocket.ts:431-455`）；
 *  - 行高 1.2、字号默认 13 且夹取 5~50（`ui/koko/utils/guard.ts`）；
 *  - 搜索高亮色 `#ff8c00 / #ffa500`（`ui/koko/components/SearchInput/index.vue:9-20`）；
 *  - resize 三板斧：ResizeObserver + 80ms 防抖 fit、200ms 防抖上报、字号变化后重新 fit
 *    （`useTerminalSocket.ts:153-184,480-491`）；
 *  - 线程内边距单独放在 `term.open()` 的宿主元素上（`Terminal/index.vue:188-200` 的坑位注释）；
 *  - 键盘双通道：xterm 内 `attachCustomKeyEventHandler` + 全局捕获监听
 *    （`useTerminalInput.ts:184-218`；`keyboard.ts:33-41`）。
 */
import {
  CopyOutlined,
  ExpandOutlined,
  LinkOutlined,
  MinusOutlined,
  PlusOutlined,
  SelectOutlined,
} from '@ant-design/icons';
import { FitAddon } from '@xterm/addon-fit';
import { type ISearchOptions, SearchAddon } from '@xterm/addon-search';
import { WebLinksAddon } from '@xterm/addon-web-links';
import type { ITheme } from '@xterm/xterm';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { Dropdown, type MenuProps, message } from 'antd';
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

/** 深色终端主题（xterm 不解析 CSS 变量，必须给字面量）。 */
export const DARK_TERMINAL_THEME: ITheme = {
  background: '#0b1021',
  foreground: '#e6edf3',
  cursor: '#52c41a',
  // 选中高亮用中性蓝：之前用品牌绿，和绿色光标/提示符糊在一起，框选后看不清选中范围。
  selectionBackground: 'rgba(96,165,250,0.32)',
  selectionForeground: '#ffffff',
  black: '#0b1021',
  red: '#ff7875',
  green: '#95de64',
  yellow: '#ffd666',
  blue: '#69b1ff',
  magenta: '#b37feb',
  cyan: '#5cdbd3',
  white: '#e6edf3',
};

export const TERMINAL_FONT_FAMILY =
  '"JetBrains Mono", "Cascadia Code", Menlo, Monaco, Consolas, "Courier New", monospace';

export type TerminalHandle = {
  write: (data: string) => void;
  writeln: (data: string) => void;
  clear: () => void;
  focus: () => void;
  fit: () => void;
  findNext: (term: string, options?: ISearchOptions) => void;
  findPrevious: (term: string, options?: ISearchOptions) => void;
  clearSearch: () => void;
  getSelection: () => string;
  selectAll: () => void;
  scrollToBottom: () => void;
  getSize: () => { cols: number; rows: number };
};

type Props = {
  /** 是否为当前激活标签（隐藏时跳过 fit）。 */
  active: boolean;
  fontSize: number;
  /** 就绪前不回传按键（服务端还会用 early_input 兜底）。 */
  inputEnabled: boolean;
  connected: boolean;
  onData: (data: string) => void;
  onResize: (size: { cols: number; rows: number }) => void;
  onReady: (handle: TerminalHandle) => void;
  onSearchRequest: () => void;
  onFontSizeChange: (fontSize: number) => void;
  onSwitchTab: (offset: number) => void;
  onFullscreenToggle: () => void;
  onFocusModeToggle: () => void;
  onDisconnect: () => void;
  onReconnect: () => void;
};

const FIT_DEBOUNCE_MS = 80;
const RESIZE_REPORT_DEBOUNCE_MS = 200;

const TerminalPane: React.FC<Props> = (props) => {
  const propsRef = useRef(props);
  propsRef.current = props;

  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const searchRef = useRef<SearchAddon | null>(null);
  const fitTimer = useRef<number | undefined>(undefined);
  const reportTimer = useRef<number | undefined>(undefined);
  const [hasSelection, setHasSelection] = useState(false);

  // 注意：不能叫 `fit`——biome 的 lint/suspicious/noFocusedTests 会把 `fit(...)` 当成
  // jasmine/vitest 的「只跑这个测试」误报，所以内部实现统一叫 fitTerminal，
  // 对外的 TerminalHandle.fit 属性名保持不变。
  const fitTerminal = useCallback(() => {
    const host = hostRef.current;
    const fitAddon = fitRef.current;
    if (!host || !fitAddon || !host.clientWidth || !host.clientHeight) {
      return;
    }
    try {
      fitAddon.fit();
    } catch (error) {
      console.debug('terminal fit failed', error);
    }
  }, []);

  const debouncedFit = useCallback(() => {
    window.clearTimeout(fitTimer.current);
    fitTimer.current = window.setTimeout(fitTerminal, FIT_DEBOUNCE_MS);
  }, [fitTerminal]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) {
      return;
    }
    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: 'block',
      fontFamily: TERMINAL_FONT_FAMILY,
      fontSize: propsRef.current.fontSize,
      lineHeight: 1.2,
      scrollback: 5000,
      scrollOnUserInput: true,
      minimumContrastRatio: 4.5,
      rightClickSelectsWord: true,
      convertEol: false,
      theme: DARK_TERMINAL_THEME,
    });
    const fitAddon = new FitAddon();
    const searchAddon = new SearchAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(searchAddon);
    term.loadAddon(new WebLinksAddon());
    term.open(host);
    termRef.current = term;
    fitRef.current = fitAddon;
    searchRef.current = searchAddon;

    // 键盘双通道之「xterm 内拦截」：返回 false 表示吞掉该按键，不再回传给 pty。
    term.attachCustomKeyEventHandler((event) => {
      if (event.type !== 'keydown') {
        return true;
      }
      const key = event.key.toLowerCase();
      const mod = event.ctrlKey || event.metaKey;

      // Ctrl/Cmd+F：打开终端搜索（JumpServer useTerminalInput.ts:196-202）
      if (mod && !event.shiftKey && key === 'f') {
        event.preventDefault();
        propsRef.current.onSearchRequest();
        return false;
      }
      // Ctrl/Cmd+Shift+F：全屏切换（useWorkspaceFullscreenShortcuts.ts:32-39）
      if (mod && event.shiftKey && key === 'f') {
        event.preventDefault();
        propsRef.current.onFullscreenToggle();
        return false;
      }
      // Ctrl/Cmd+Shift+P：纯净模式
      if (mod && event.shiftKey && key === 'p') {
        event.preventDefault();
        propsRef.current.onFocusModeToggle();
        return false;
      }
      // Ctrl/Cmd+Shift+C 或 Cmd+C：复制选中（keyboard.ts:33-36）
      if (
        !event.altKey &&
        key === 'c' &&
        ((event.metaKey && !event.ctrlKey) || (event.ctrlKey && event.shiftKey))
      ) {
        const selection = term.getSelection();
        if (selection) {
          event.preventDefault();
          void navigator.clipboard?.writeText(selection).catch(() => {
            message.warning('浏览器拒绝了剪贴板写入，请手动选择后复制');
          });
          return false;
        }
        return true;
      }
      // 纯 Ctrl+C：交给远端发送中断 \x03（keyboard.ts:39-41）
      if (
        event.ctrlKey &&
        !event.metaKey &&
        !event.shiftKey &&
        !event.altKey &&
        key === 'c'
      ) {
        const selection = term.getSelection();
        if (selection) {
          // 有选中内容时按「复制」语义处理，与浏览器习惯一致
          event.preventDefault();
          void navigator.clipboard?.writeText(selection).catch(() => undefined);
          return false;
        }
        propsRef.current.onData('\x03');
        return false;
      }
      // Ctrl/Cmd+V：放行给浏览器 paste 事件，由 xterm 自己写回（useTerminalInput.ts:217）
      if (mod && key === 'v') {
        return false;
      }
      // Ctrl/Cmd +/-：字号（JumpServer 默认 13，5~50 夹取）
      if (mod && (key === '+' || key === '=')) {
        event.preventDefault();
        propsRef.current.onFontSizeChange(propsRef.current.fontSize + 1);
        return false;
      }
      if (mod && key === '-') {
        event.preventDefault();
        propsRef.current.onFontSizeChange(propsRef.current.fontSize - 1);
        return false;
      }
      // Alt+Shift+←/→：切换标签（useTerminalInput.ts:189-193）
      if (event.altKey && event.shiftKey && event.key === 'ArrowLeft') {
        event.preventDefault();
        propsRef.current.onSwitchTab(-1);
        return false;
      }
      if (event.altKey && event.shiftKey && event.key === 'ArrowRight') {
        event.preventDefault();
        propsRef.current.onSwitchTab(1);
        return false;
      }
      return true;
    });

    const dataDisposable = term.onData((data) => {
      if (!propsRef.current.inputEnabled) {
        return;
      }
      propsRef.current.onData(data);
    });

    const resizeDisposable = term.onResize(({ cols, rows }) => {
      window.clearTimeout(reportTimer.current);
      reportTimer.current = window.setTimeout(() => {
        propsRef.current.onResize({ cols, rows });
      }, RESIZE_REPORT_DEBOUNCE_MS);
    });

    const selectionDisposable = term.onSelectionChange(() => {
      setHasSelection(Boolean(term.getSelection()));
    });

    const handle: TerminalHandle = {
      write: (data) => term.write(data),
      writeln: (data) => term.writeln(data),
      clear: () => {
        term.clear();
      },
      focus: () => term.focus(),
      fit: fitTerminal,
      findNext: (value, options) => {
        searchAddon.findNext(value, options);
      },
      findPrevious: (value, options) => {
        searchAddon.findPrevious(value, options);
      },
      clearSearch: () => {
        searchAddon.clearDecorations();
      },
      getSelection: () => term.getSelection(),
      selectAll: () => term.selectAll(),
      scrollToBottom: () => term.scrollToBottom(),
      getSize: () => ({ cols: term.cols, rows: term.rows }),
    };
    propsRef.current.onReady(handle);

    const observer = new ResizeObserver(() => {
      debouncedFit();
    });
    observer.observe(host);

    return () => {
      observer.disconnect();
      window.clearTimeout(fitTimer.current);
      window.clearTimeout(reportTimer.current);
      dataDisposable.dispose();
      resizeDisposable.dispose();
      selectionDisposable.dispose();
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
      searchRef.current = null;
    };
    // 只创建一次：动态参数通过 propsRef 读取
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedFit, fitTerminal]);

  // 字号变化 → 立即生效并重新 fit（useTerminalSocket.ts:480-491）
  useEffect(() => {
    const term = termRef.current;
    if (!term) {
      return;
    }
    term.options.fontSize = props.fontSize;
    const timer = window.setTimeout(() => {
      fitTerminal();
    }, FIT_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [fitTerminal, props.fontSize]);

  // 标签切回可见时重新 fit 并聚焦（隐藏时 clientWidth 为 0，fit 会跳过）
  useEffect(() => {
    if (!props.active) {
      return;
    }
    const timer = window.setTimeout(() => {
      fitTerminal();
      termRef.current?.focus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [fitTerminal, props.active]);

  const copySelection = useCallback(() => {
    const selection = termRef.current?.getSelection() ?? '';
    if (!selection) {
      message.info('请先选中要复制的内容');
      return;
    }
    void navigator.clipboard?.writeText(selection).then(
      () => message.success('已复制选中内容'),
      () => message.warning('浏览器拒绝了剪贴板写入'),
    );
  }, []);

  const paste = useCallback(() => {
    const readText = navigator.clipboard?.readText?.();
    if (!readText) {
      message.info('当前浏览器不允许读取剪贴板，请使用 Ctrl+V 粘贴');
      return;
    }
    void readText.then(
      (text) => {
        if (text) {
          propsRef.current.onData(text);
        }
      },
      () => message.warning('浏览器拒绝了剪贴板读取，请使用 Ctrl+V 粘贴'),
    );
  }, []);

  const contextItems = useMemo<MenuProps['items']>(
    () => [
      {
        key: 'copy',
        icon: <CopyOutlined />,
        label: '复制',
        disabled: !hasSelection,
        onClick: copySelection,
      },
      {
        key: 'paste',
        icon: <LinkOutlined />,
        label: '粘贴',
        disabled: !props.connected,
        onClick: paste,
      },
      {
        key: 'select-all',
        icon: <SelectOutlined />,
        label: '全选',
        onClick: () => termRef.current?.selectAll(),
      },
      { type: 'divider' },
      {
        key: 'clear',
        icon: <MinusOutlined />,
        label: '清空屏幕',
        onClick: () => termRef.current?.clear(),
      },
      {
        key: 'font-plus',
        icon: <PlusOutlined />,
        label: '放大字号',
        onClick: () =>
          propsRef.current.onFontSizeChange(propsRef.current.fontSize + 1),
      },
      {
        key: 'font-minus',
        icon: <MinusOutlined />,
        label: '缩小字号',
        onClick: () =>
          propsRef.current.onFontSizeChange(propsRef.current.fontSize - 1),
      },
      { type: 'divider' },
      {
        key: 'fullscreen',
        icon: <ExpandOutlined />,
        label: '全屏 / 退出全屏',
        onClick: () => propsRef.current.onFullscreenToggle(),
      },
      props.connected
        ? {
            key: 'disconnect',
            label: '断开会话',
            danger: true,
            onClick: () => propsRef.current.onDisconnect(),
          }
        : {
            key: 'reconnect',
            label: '重新连接',
            onClick: () => propsRef.current.onReconnect(),
          },
    ],
    [copySelection, hasSelection, paste, props.connected],
  );

  return (
    <div
      className="bastion-terminal-host"
      style={{
        display: props.active ? 'block' : 'none',
        height: '100%',
        width: '100%',
        background: DARK_TERMINAL_THEME.background,
        // xterm 滚动条上下留白（Terminal/index.vue:188-200）
        ['--xterm-scrollbar-top' as string]: '4px',
        ['--xterm-scrollbar-bottom' as string]: '4px',
      }}
    >
      <Dropdown menu={{ items: contextItems }} trigger={['contextMenu']}>
        <div
          ref={hostRef}
          style={{
            height: '100%',
            width: '100%',
            boxSizing: 'border-box',
            padding: '8px 2px 4px 8px',
            overflow: 'hidden',
          }}
        />
      </Dropdown>
    </div>
  );
};

export default TerminalPane;
