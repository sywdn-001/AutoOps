/**
 * 审计中心 · 录像回放（**弹出式窗口**）。
 *
 * 审计中心的「远程桌面录像」列表点「回看」时 `window.open()` 打开本页，一个窗口一段录像。
 * 为什么单独开窗口而不是弹 Modal：录像动辄几百 MB，Modal 关掉就断流；独立窗口可以缩放、
 * 全屏、拖进度条，也方便审计人员一边看一边对着别处核对（形态与终端/远程桌面窗口一致）。
 *
 * 回放鉴权：`<video>` 发不出 `Authorization` 头，所以本页先 `POST /api/rdp/recordings/<id>/ticket`
 * 换一张 10 分钟、绑定「当前用户 + 这段录像」的一次性票据（后端签票时写一条 `rdp_recording_viewed`），
 * 再让浏览器带 `?ticket=` 边流边放；票据过期就用「重新换票」按钮再来一张（并再记一条审计）。
 */
import { ReloadOutlined, StopOutlined } from '@ant-design/icons';
import { Alert, Button, Space, Spin, Typography } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { rdpApi } from '@/services/bastion/endpoints';
import './rdp.css';

const { Text } = Typography;

const RdpPlayPage = () => {
  const params = new URLSearchParams(window.location.search);
  const recordingId = Number(params.get('recordingId') || 0);
  const title = params.get('title') || '';
  const meta = [
    params.get('user') ? `操作人 ${params.get('user')}` : '',
    params.get('account') ? `资产账号 ${params.get('account')}` : '',
    params.get('duration') ? `时长 ${params.get('duration')}` : '',
    params.get('size') ? `体积 ${params.get('size')}` : '',
    params.get('time') ? `录制于 ${params.get('time')}` : '',
  ].filter(Boolean);

  const [url, setUrl] = useState<string>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);

  /** 换一张一次性回放票据；`<video>` 只认 URL，所以票据必须拼在 query 上。 */
  const loadTicket = useCallback(async () => {
    if (!recordingId) {
      setError('地址里缺少录像编号（recordingId），无法回放');
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(undefined);
    try {
      const ticket = await rdpApi.recordingTicket(recordingId);
      setUrl(
        ticket.path.includes('?')
          ? ticket.path
          : `${ticket.path}?ticket=${encodeURIComponent(ticket.ticket)}`,
      );
    } catch (err) {
      setUrl(undefined);
      setError(err instanceof Error ? err.message : '换取回放票据失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    document.title = `录像回放 · #${recordingId}${title ? ` ${title}` : ''}`;
    void loadTicket();
  }, [loadTicket, recordingId, title]);

  return (
    <div className="bastion-rdp-play">
      <div className="bastion-rdp-play-bar">
        <Space size={8} wrap>
          <span className="bastion-rdp-play-title">
            录像回放 · #{recordingId}
            {title ? ` ${title}` : ''}
          </span>
          {meta.length > 0 ? (
            <Text type="secondary" className="bastion-rdp-play-meta">
              {meta.join(' · ')}
            </Text>
          ) : null}
        </Space>
        <Space size={4}>
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => void loadTicket()}
          >
            重新换票
          </Button>
          <Button
            size="small"
            icon={<StopOutlined />}
            onClick={() => window.close()}
          >
            关闭窗口
          </Button>
        </Space>
      </div>

      <div className="bastion-rdp-play-stage">
        {error ? (
          <Alert
            type="error"
            showIcon
            message="回放失败"
            description={error}
            action={
              <Button size="small" onClick={() => void loadTicket()}>
                重试
              </Button>
            }
          />
        ) : null}
        {!error && !url ? (
          <Spin tip="正在换取回放票据…">
            <div className="bastion-rdp-play-placeholder" />
          </Spin>
        ) : null}
        {url ? (
          /* biome-ignore lint/a11y/useMediaCaption: 远程桌面录像只录画面（canvas.captureStream 没有音轨），不存在需要对白的字幕 */
          <video
            key={url}
            src={url}
            controls
            autoPlay
            className="bastion-rdp-video"
            onError={() =>
              setError('播放器读取录像失败：票据可能已过期，点「重新换票」再试')
            }
          />
        ) : null}
      </div>
    </div>
  );
};

export default RdpPlayPage;
