/**
 * 审计中心 · 远程桌面录像（WebRDP 录像留档）。
 *
 * 需求⑤：「谁登录了哪台 Windows 机器，远程操作视频录下来，随时可以回看审计」。
 *
 * 录像本体在浏览器侧录（控制台里 `canvas.captureStream()` + `MediaRecorder`），会话结束时上传；
 * 这里负责列出录像、点开回看、必要时删除。回看走**一次性票据**：
 * `<video>` 带不了 `Authorization` 头，而录像动辄几百 MB、不能整包拉成 blob 再播，
 * 所以先 `POST /api/rdp/recordings/<id>/ticket` 换票，再让浏览器带 `?ticket=` 边流边放
 * （后端支持 Range，进度条能拖）。签票时后端会写一条 `rdp_recording_viewed` 审计。
 *
 * 可见范围由后端决定：有 `session:view_all` / `audit:view` 的人看得到所有人的录像，
 * 其他人只看得到自己上传的那些。
 */
import {
  DeleteOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons';
import type { ActionType, ProColumns } from '@ant-design/pro-components';
import { PageContainer, ProTable } from '@ant-design/pro-components';
import { useAccess } from '@umijs/max';
import {
  Alert,
  Button,
  Modal,
  message,
  Popconfirm,
  Space,
  Tag,
  Typography,
} from 'antd';
import { useCallback, useRef, useState } from 'react';
import { rdpApi } from '@/services/bastion/endpoints';
import type { RdpRecording } from '@/services/bastion/types';
import './rdp.css';

const { Text } = Typography;

/** 秒 → mm:ss（超过一小时按 hh:mm:ss）。 */
const formatDuration = (seconds: number): string => {
  const total = Math.max(0, Math.round(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value: number) => String(value).padStart(2, '0');
  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(secs)}`
    : `${pad(minutes)}:${pad(secs)}`;
};

/** 字节 → 人类可读体积。 */
const formatSize = (bytes: number): string => {
  const value = Number(bytes || 0);
  if (value >= 1024 * 1024 * 1024) {
    return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }
  if (value >= 1024 * 1024) {
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }
  if (value >= 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${value} B`;
};

const formatTime = (value?: string): string => {
  if (!value) {
    return '—';
  }
  return value.replace('T', ' ').slice(0, 19);
};

const RdpRecordingsPage = () => {
  const access = useAccess();
  const actionRef = useRef<ActionType | undefined>(undefined);
  const [playing, setPlaying] = useState<{
    recording: RdpRecording;
    url: string;
  }>();
  const [loadingPlay, setLoadingPlay] = useState<number>();
  const [removing, setRemoving] = useState<number>();

  /** 换票并打开回放窗口（票据 10 分钟有效，够看完一段录像）。 */
  const play = useCallback(async (recording: RdpRecording) => {
    setLoadingPlay(recording.id);
    try {
      const ticket = await rdpApi.recordingTicket(recording.id);
      const url = ticket.path.includes('?')
        ? ticket.path
        : `${ticket.path}?ticket=${encodeURIComponent(ticket.ticket)}`;
      setPlaying({ recording, url });
    } catch (err) {
      message.error(err instanceof Error ? err.message : '打开录像失败');
    } finally {
      setLoadingPlay(undefined);
    }
  }, []);

  const remove = useCallback(async (recording: RdpRecording) => {
    setRemoving(recording.id);
    try {
      await rdpApi.removeRecording(recording.id);
      message.success('录像已删除');
      actionRef.current?.reload();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '删除失败');
    } finally {
      setRemoving(undefined);
    }
  }, []);

  const columns: ProColumns<RdpRecording>[] = [
    {
      title: '录像',
      dataIndex: 'id',
      width: 90,
      render: (_, record) => (
        <Space size={6}>
          <VideoCameraOutlined />
          <span className="bastion-rdp-rec-id">#{record.id}</span>
        </Space>
      ),
    },
    {
      title: '目标主机',
      dataIndex: 'hostName',
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <span className="bastion-launcher-host">
            {record.hostName || `主机 #${record.hostId}`}
          </span>
          <span className="bastion-launcher-addr">{record.hostAddress}</span>
        </Space>
      ),
    },
    {
      title: '操作人',
      dataIndex: 'username',
      width: 140,
      render: (_, record) => record.username || '—',
    },
    {
      title: '资产账号',
      dataIndex: 'accountUsername',
      width: 140,
      render: (_, record) => record.accountUsername || '—',
    },
    {
      title: '时长',
      dataIndex: 'durationSeconds',
      width: 100,
      render: (_, record) => formatDuration(record.durationSeconds),
    },
    {
      title: '体积',
      dataIndex: 'sizeBytes',
      width: 110,
      render: (_, record) => formatSize(record.sizeBytes),
    },
    {
      title: '录制时间',
      dataIndex: 'createdAt',
      width: 180,
      render: (_, record) => (
        <Text type="secondary">{formatTime(record.createdAt)}</Text>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 170,
      render: (_, record) => (
        <Space size={4}>
          <Button
            size="small"
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={loadingPlay === record.id}
            onClick={() => void play(record)}
          >
            回看
          </Button>
          <Popconfirm
            title="删除这条录像？"
            description="录像文件会从堡垒机上删除，审计里只留下「已删除」的记录。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => void remove(record)}
          >
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              loading={removing === record.id}
            >
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <PageContainer title="远程桌面录像">
      <Alert
        className="bastion-launcher-note"
        type="info"
        showIcon
        message="登录 Windows 资产的远程操作全过程都在这里"
        description={
          access.canSessionViewAll || access.canAuditView
            ? '你能看到所有人的录像；每次点「回看」都会在审计里记一条查看记录。'
            : '你只能看到自己上传的录像；每次点「回看」都会在审计里记一条查看记录。'
        }
      />
      <ProTable<RdpRecording>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        search={false}
        cardBordered
        headerTitle="录像列表"
        options={{ reload: true, density: false, setting: false }}
        toolBarRender={() => [
          <Button
            key="reload"
            icon={<ReloadOutlined />}
            onClick={() => actionRef.current?.reload()}
          >
            刷新
          </Button>,
        ]}
        locale={{
          emptyText: (
            <Space direction="vertical" size={4}>
              <span>还没有录像</span>
              <span className="bastion-launcher-addr">
                在「网页终端」里连接一台 Windows
                主机并操作，会话结束时录像会自动上传到这里。
              </span>
            </Space>
          ),
        }}
        request={async (params) => {
          const page = await rdpApi.recordings({
            current: params.current,
            pageSize: params.pageSize,
          });
          return {
            data: page.data ?? [],
            total: page.total ?? 0,
            success: true,
          };
        }}
      />

      <Modal
        open={playing !== undefined}
        title={
          playing
            ? `录像回放 · #${playing.recording.id} ${playing.recording.hostName || ''}`
            : '录像回放'
        }
        width={880}
        destroyOnHidden
        onCancel={() => setPlaying(undefined)}
        footer={[
          <Button key="close" onClick={() => setPlaying(undefined)}>
            关闭
          </Button>,
        ]}
      >
        {playing ? (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {/* biome-ignore lint/a11y/useMediaCaption: 远程桌面录像只录画面（canvas.captureStream 没有音轨），不存在需要对白的字幕 */}
            <video
              key={playing.url}
              src={playing.url}
              controls
              autoPlay
              className="bastion-rdp-video"
            />
            <Text type="secondary">
              操作人 {playing.recording.username || '—'} · 资产账号{' '}
              {playing.recording.accountUsername || '—'} · 时长{' '}
              {formatDuration(playing.recording.durationSeconds)} · 体积{' '}
              {formatSize(playing.recording.sizeBytes)} · 录制于{' '}
              {formatTime(playing.recording.createdAt)}
            </Text>
            <Tag color="blue">
              回放链接 10 分钟后失效，重新打开需再次换票（并再记一条审计）
            </Tag>
          </Space>
        ) : null}
      </Modal>
    </PageContainer>
  );
};

export default RdpRecordingsPage;
