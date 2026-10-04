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
  Button,
  message,
  Popconfirm,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useCallback, useRef, useState } from 'react';
import { buildRdpPlayUrl } from '@/pages/bastion/rdp/types';
import { consoleWindowFeatures } from '@/pages/bastion/terminal/types';
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
  const [removing, setRemoving] = useState<number>();

  /**
   * 回看：**弹独立窗口**（与终端/远程桌面窗口同形态），窗口自己换票再流式播放。
   * 票据 10 分钟有效、且只在打开窗口的那一刻换一次（换票时后端写一条 `rdp_recording_viewed`）。
   */
  const play = useCallback((recording: RdpRecording) => {
    const url = buildRdpPlayUrl({
      recordingId: recording.id,
      hostName: recording.hostName || `主机 #${recording.hostId}`,
      username: recording.username,
      accountUsername: recording.accountUsername,
      duration: formatDuration(recording.durationSeconds),
      size: formatSize(recording.sizeBytes),
      createdAt: formatTime(recording.createdAt),
    });
    const opened = window.open(
      url,
      `rdp-play-${recording.id}`,
      consoleWindowFeatures(),
    );
    if (!opened) {
      message.warning('浏览器拦截了回放窗口，请允许本页弹出窗口后重试');
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
      width: 130,
      render: (_, record) => (
        <Space size={6}>
          <VideoCameraOutlined />
          <span className="bastion-rdp-rec-id">#{record.id}</span>
          {record.recovered ? (
            <Tooltip title="浏览器窗口被强行关掉（直接关标签页 / 崩溃 / 断网）时，服务端凭边录边传已经收到的分片自动收口的录像 —— 只录到窗口关闭那一刻，不是完整的操作过程">
              <Tag color="orange">未正常结束</Tag>
            </Tooltip>
          ) : null}
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
          <Tooltip title="在独立窗口里回放（会换一张 10 分钟有效的一次性票据，并在审计里记一条查看记录）">
            <Button
              size="small"
              type="primary"
              icon={<PlayCircleOutlined />}
              onClick={() => play(record)}
            >
              回看
            </Button>
          </Tooltip>
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
      <ProTable<RdpRecording>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        search={false}
        cardBordered
        headerTitle={
          <Space size={10} wrap>
            <span>录像列表</span>
            <Text
              type="secondary"
              style={{ fontSize: 12, fontWeight: 'normal' }}
            >
              {access.canSessionViewAll || access.canAuditView
                ? '你能看到所有人的录像；每次「回看」都会在审计里记一条查看记录。'
                : '你只能看到自己上传的录像；每次「回看」都会在审计里记一条查看记录。'}
            </Text>
          </Space>
        }
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
    </PageContainer>
  );
};

export default RdpRecordingsPage;
