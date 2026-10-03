/**
 * 单条审计会话的 Socket.IO 生命周期。
 *
 * 一个 `window.open()` 弹出的终端窗口 = 一条会话（对齐 JumpServer：点资产后新开控制台窗口）。
 * 服务端 `app/webterm/events.py` 的 `_CLIENTS[request.sid]` 只保存一条会话上下文，
 * 所以「再开一个会话」在 UI 上就是「再点一次连接、再弹一个窗口」。
 * 断线策略对齐 JumpServer：**不自动无限重连**，断线时把原因写进终端并给出「重新连接」。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { io, type Socket } from 'socket.io-client';
import type { TerminalTarget } from '@/services/bastion/types';
import type { TerminalHandle } from './components/TerminalPane';
import {
  type ConnectionStatus,
  clampFontSize,
  DEFAULT_FONT_SIZE,
  describeCloseReason,
  type TerminalSession,
} from './types';

const tokenOf = () => localStorage.getItem('bastion_token') ?? '';

type ConnectInput = {
  target: TerminalTarget;
  accountId: number;
  accountUsername?: string;
  fontSize?: number;
};

export type TerminalSessionApi = ReturnType<typeof useTerminalSession>;

export const useTerminalSession = (options?: {
  /** 会话被服务端关闭时回调（用于提示条/标题更新）。 */
  onClosed?: (reason: string) => void;
}) => {
  const [session, setSession] = useState<TerminalSession>();
  const socketRef = useRef<Socket | undefined>(undefined);
  const handleRef = useRef<TerminalHandle | undefined>(undefined);
  const inputRef = useRef<ConnectInput | undefined>(undefined);
  const onClosedRef = useRef(options?.onClosed);
  onClosedRef.current = options?.onClosed;

  const patch = useCallback((partial: Partial<TerminalSession>) => {
    setSession((prev) => (prev ? { ...prev, ...partial } : prev));
  }, []);

  const write = useCallback((data: string) => {
    handleRef.current?.write(data);
  }, []);

  const teardown = useCallback((notifyServer: boolean) => {
    const socket = socketRef.current;
    socketRef.current = undefined;
    if (!socket) {
      return;
    }
    if (notifyServer) {
      try {
        socket.emit('terminal:close');
      } catch (error) {
        console.debug('emit terminal:close failed', error);
      }
    }
    socket.removeAllListeners();
    socket.disconnect();
  }, []);

  /** 打开会话（本页只会有一条）。 */
  const connect = useCallback(
    ({ target, accountId, accountUsername, fontSize }: ConnectInput) => {
      inputRef.current = {
        target,
        accountId,
        accountUsername,
        fontSize: fontSize ?? DEFAULT_FONT_SIZE,
      };
      const size = clampFontSize(fontSize ?? DEFAULT_FONT_SIZE);
      teardown(true);
      setSession({
        hostId: target.hostId,
        accountId,
        hostName: target.hostName,
        address: target.address,
        port: target.port,
        accountUsername: accountUsername ?? '',
        policyName: target.policyName,
        protocol: 'ssh',
        status: 'connecting',
        fontSize: size,
      });

      const socket = io('/', {
        auth: { token: tokenOf() },
        query: { token: tokenOf() },
        transports: ['websocket', 'polling'],
        reconnection: false,
      });
      socketRef.current = socket;

      socket.on('connect', () => {
        socket.emit('terminal:open', {
          hostId: target.hostId,
          accountId,
          cols: handleRef.current?.getSize().cols ?? 120,
          rows: handleRef.current?.getSize().rows ?? 32,
        });
      });

      socket.on('connect_error', (error: Error) => {
        const text = `终端通道连接失败：${error?.message || '未知原因'}`;
        patch({ status: 'failed', error: text });
        write(`\r\n\x1b[31m${text}\x1b[0m\r\n`);
      });

      socket.on('terminal:ready', () => {
        setSession((prev) =>
          prev && (prev.status === 'connecting' || prev.status === 'selecting')
            ? { ...prev, status: 'ready' as ConnectionStatus }
            : prev,
        );
      });

      socket.on(
        'terminal:opened',
        (payload: {
          sid?: string;
          hostName?: string;
          address?: string;
          accountUsername?: string;
          policyName?: string;
          segmented?: boolean;
        }) => {
          if (!payload?.sid) {
            return;
          }
          const accountName = payload.accountUsername || accountUsername || '';
          patch({
            sid: payload.sid,
            status: 'connected',
            connectedAt: Date.now(),
            hostName: payload.hostName || target.hostName,
            address: payload.address || `${target.address}:${target.port}`,
            accountUsername: accountName,
            policyName: payload.policyName ?? target.policyName,
            segmented: payload.segmented,
            error: undefined,
          });
          write(
            `\r\n\x1b[32m[堡垒机] 已进入审计会话（账号 ${accountName}，会话号 ${payload.sid}）\x1b[0m\r\n`,
          );
          const handle = handleRef.current;
          handle?.fit();
          const nextSize = handle?.getSize();
          if (nextSize) {
            socket.emit('terminal:resize', nextSize);
          }
        },
      );

      socket.on('terminal:output', (payload: { data?: string }) => {
        if (payload?.data) {
          write(payload.data);
        }
      });

      // 系统提示（如提示符改写失败降级为原始录制）只在终端里以黄字提示一次。
      // 命令被拒绝的提示不在这里——桥接层已经把它写进终端输出流，这里再写一行就是重复。
      socket.on('terminal:notice', (payload: { message?: string }) => {
        if (payload?.message) {
          write(`\r\n\x1b[33m[堡垒机] ${payload.message}\x1b[0m\r\n`);
        }
      });

      socket.on('terminal:closed', (payload: { reason?: string }) => {
        const reason = describeCloseReason(payload?.reason);
        patch({ status: 'disconnected', closedReason: reason });
        write(`\r\n\x1b[31m=== 会话已结束（${reason}）===\x1b[0m\r\n`);
        onClosedRef.current?.(reason);
      });

      socket.on('terminal:error', (payload: { message?: string }) => {
        const text = payload?.message || '终端通道返回未知错误';
        setSession((prev) =>
          prev
            ? {
                ...prev,
                status: prev.status === 'connected' ? prev.status : 'failed',
                error: text,
              }
            : prev,
        );
        write(`\r\n\x1b[31m${text}\x1b[0m\r\n`);
      });

      socket.on('disconnect', (reason: string) => {
        setSession((prev) => {
          if (!prev) {
            return prev;
          }
          if (prev.status === 'connected') {
            return {
              ...prev,
              status: 'disconnected' as ConnectionStatus,
              closedReason: describeCloseReason(reason),
            };
          }
          if (prev.status === 'connecting' || prev.status === 'ready') {
            return {
              ...prev,
              status: 'failed' as ConnectionStatus,
              error: `终端通道已关闭：${reason}`,
            };
          }
          return prev;
        });
      });
    },
    [patch, teardown, write],
  );

  /** 用户主动断开会话（保留终端内容，便于回看刚才的输出）。 */
  const disconnect = useCallback(
    (reason = '用户主动断开') => {
      teardown(true);
      patch({ status: 'disconnected', closedReason: reason });
      write(`\r\n\x1b[33m=== 已断开连接（${reason}）===\x1b[0m\r\n`);
    },
    [patch, teardown, write],
  );

  /** 用同一主机/账号重新建连（保留字号）。 */
  const reconnect = useCallback(() => {
    const input = inputRef.current;
    if (input) {
      connect(input);
    }
  }, [connect]);

  const setFontSize = useCallback(
    (fontSize: number) => {
      patch({ fontSize: clampFontSize(fontSize) });
    },
    [patch],
  );

  const sendInput = useCallback((data: string) => {
    socketRef.current?.emit('terminal:input', { data });
  }, []);

  const sendResize = useCallback((size: { cols: number; rows: number }) => {
    socketRef.current?.emit('terminal:resize', size);
  }, []);

  const registerHandle = useCallback((handle: TerminalHandle) => {
    handleRef.current = handle;
  }, []);

  const getHandle = useCallback(() => handleRef.current, []);

  // 关闭窗口/卸载组件时告诉服务端收尾，避免留下僵尸会话
  useEffect(
    () => () => {
      teardown(true);
      handleRef.current = undefined;
    },
    [teardown],
  );

  return {
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
  };
};
