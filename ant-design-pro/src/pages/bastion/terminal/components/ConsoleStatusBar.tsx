/**
 * 底部状态条（对齐 JumpServer `statusFooter.vue`，单会话形态）：
 * 左侧登录态圆点 + 登录账号；右侧资产名、协议、连接状态与**连接时长**。
 * 审计不在这里展示 —— 每条命令与输出都落在审计中心，终端页只负责操作。
 */
import { Tag } from 'antd';
import { useEffect, useState } from 'react';
import { formatDuration, STATUS_META, type TerminalSession } from '../types';

const ConsoleStatusBar = ({
  session,
  username,
  channelOnline,
}: {
  session?: TerminalSession;
  username?: string;
  channelOnline?: boolean;
}) => {
  const meta = session ? STATUS_META[session.status] : undefined;
  const connectedAt = session?.connectedAt;
  const [now, setNow] = useState(() => Date.now());
  // 连接时长每秒跳动（JumpServer 右面板的时长口径，这里收到状态条上）
  useEffect(() => {
    if (!connectedAt) {
      return;
    }
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [connectedAt]);
  const live = session?.status === 'connected' && connectedAt;
  return (
    <div className="bastion-statusbar">
      <div className="bastion-statusbar-left">
        <span
          className={`bastion-status-dot${channelOnline ? '' : ' bastion-status-dot-offline'}`}
          title={channelOnline ? '终端通道已连接' : '终端通道未连接'}
        />
        <span>{username || '未登录'}</span>
      </div>
      <div className="bastion-statusbar-right">
        {session ? (
          <>
            <span className="bastion-status-asset" title={session.hostName}>
              {session.hostName}
            </span>
            <Tag style={{ marginInlineEnd: 0 }}>
              {session.protocol.toUpperCase()}
            </Tag>
            <span
              className={
                session.status === 'connected'
                  ? 'bastion-status-count-ok'
                  : session.status === 'failed'
                    ? 'bastion-status-count-failed'
                    : session.status === 'connecting' ||
                        session.status === 'ready'
                      ? 'bastion-status-count-pending'
                      : undefined
              }
            >
              {meta?.text ?? session.status}
            </span>
            {session.closedReason ? (
              <span title={session.closedReason}>{session.closedReason}</span>
            ) : null}
            <span className="bastion-status-duration" title="连接时长">
              {live ? formatDuration(now - (connectedAt as number)) : '-'}
            </span>
          </>
        ) : (
          <span>未建立会话</span>
        )}
      </div>
    </div>
  );
};

export default ConsoleStatusBar;
