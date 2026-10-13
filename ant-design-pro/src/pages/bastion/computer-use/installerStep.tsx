/**
 * 部署向导第 1 步：选择 Python 安装包。
 *
 * 列出堡垒机镜像里「真的能下到 python-<版本>-amd64.exe」的版本，按 windows-mcp 兼容性
 * （Python ≥ 3.13）标注；选中后先把包缓存到堡垒机（约 28 MB，可能要一两分钟），
 * 缓存完成才允许进入第 2 步 —— 后端部署时也是从这份缓存里取包，拿不到就直接 400。
 */
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { Alert, App, Button, Card, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { BoolTag, MonoCell } from '@/components/Bastion';
import {
  computerUseApi,
  type PythonVersionItem,
  type VersionsResult,
} from '@/services/bastion/computeruse';
import { backendMessage, humanSize } from './helpers';

const { Text, Paragraph } = Typography;

export type InstallerStepProps = {
  /** 当前选中的版本（父级持有，切回来还在） */
  selected: PythonVersionItem | null;
  onSelect: (item: PythonVersionItem | null) => void;
  /** 缓存成功后把「堡机上现在有哪些包」回传给父级 */
  onCached: (cached: VersionsResult['cached']) => void;
};

const InstallerStep: React.FC<InstallerStepProps> = ({
  selected,
  onSelect,
  onCached,
}) => {
  const { message } = App.useApp();
  const [data, setData] = useState<VersionsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [caching, setCaching] = useState(false);

  const load = useCallback(
    async (refresh: boolean) => {
      if (refresh) setRefreshing(true);
      else setLoading(true);
      try {
        const result = await computerUseApi.versions(refresh);
        setData(result);
        onCached(result.cached);
        // 缓存状态可能因为别的会话变化，这里重新对齐选中行
        if (selected) {
          const fresh = result.items.find(
            (item) => item.filename === selected.filename,
          );
          if (fresh) onSelect(fresh);
        }
      } catch (error) {
        message.error(
          backendMessage(
            error,
            '镜像列表加载失败，请稍后重试或点「刷新镜像列表」',
          ),
        );
      } finally {
        setRefreshing(false);
        setLoading(false);
      }
    },
    [message, onCached, onSelect, selected],
  );

  /** 用 ref 保存最新的 load，首次进入这一步时拉一次即可 */
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    loadRef.current?.(false);
    // 只在首次进入这一步时拉一次，后续由「刷新镜像列表」按钮触发
    // （用 ref 保存最新的 load，避免把选中行写进依赖导致来回重拉）
  }, []);

  const cacheSelected = async () => {
    const item = selected;
    if (!item) return;
    setCaching(true);
    message.info(`正在从镜像缓存 ${item.filename} 到堡垒机，约 28 MB，请稍候…`);
    try {
      const result = await computerUseApi.cacheInstaller({
        version: item.version,
        filename: item.filename,
      });
      message.success(
        result.skipped
          ? `${result.filename} 之前已缓存（${humanSize(result.size)}），直接可用`
          : `已缓存到堡垒机：${result.filename}（${humanSize(result.size)}）`,
      );
      await load(false);
    } catch (error) {
      message.error(backendMessage(error, '安装包缓存失败，请稍后重试'));
    } finally {
      setCaching(false);
    }
  };

  const columns: ColumnsType<PythonVersionItem> = [
    {
      title: '版本',
      dataIndex: 'version',
      width: 100,
      render: (value: string) => <Text strong>{value}</Text>,
    },
    {
      title: '安装包文件名',
      dataIndex: 'filename',
      width: 240,
      render: (value: string) => <MonoCell text={value} max={30} width={220} />,
    },
    {
      title: '大小',
      dataIndex: 'size',
      width: 100,
      align: 'right',
      render: (value: number, record) =>
        humanSize(record.localSize > 0 ? record.localSize : value),
    },
    {
      title: '支持 Windows-MCP',
      dataIndex: 'mcpCompatible',
      width: 150,
      render: (value: boolean) => (
        <BoolTag value={value} yes="支持（≥3.13）" no="不支持" />
      ),
    },
    {
      title: '已缓存到堡垒机',
      dataIndex: 'cached',
      width: 150,
      render: (value: boolean, record) =>
        value ? (
          <Tag color="success">已缓存（{humanSize(record.localSize)}）</Tag>
        ) : (
          <Tag color="default">未缓存</Tag>
        ),
    },
    {
      title: '镜像发布',
      dataIndex: 'released',
      width: 130,
      render: (value: string) =>
        value ? (
          <Text type="secondary">{value}</Text>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
  ];

  const cachedCount = data?.cached.length ?? 0;

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        title="第 1 步：先把 Python 安装包缓存到堡垒机"
        description={
          <Space orientation="vertical" size={4}>
            <Text>
              部署时堡垒机会把这个安装包推送到目标机的
              C:\Installer，所以包必须先落到堡垒机 本地。windows-mcp 要求 Python
              ≥ {data?.minPython || '3.13'}，低于这个版本的 一行请不要选。
            </Text>
            <Text type="secondary">
              镜像地址：{data?.mirror || '（加载中）'}
            </Text>
            <Text type="secondary">
              缓存一个包约 28
              MB，视镜像速度可能要一两分钟，期间按钮会转圈，请不要关页面。
            </Text>
          </Space>
        }
      />

      <Card
        title="可用的 Python 版本"
        extra={
          <Space>
            <Text type="secondary">堡垒机已缓存 {cachedCount} 个安装包</Text>
            <Button
              icon={<ReloadOutlined />}
              loading={refreshing}
              onClick={() => load(true)}
            >
              刷新镜像列表
            </Button>
          </Space>
        }
      >
        <Table<PythonVersionItem>
          rowKey="filename"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={data?.items ?? []}
          pagination={false}
          scroll={{ x: 900 }}
          rowSelection={{
            type: 'radio',
            selectedRowKeys: selected ? [selected.filename] : [],
            getCheckboxProps: (record) => ({ disabled: !record.mcpCompatible }),
            onChange: (keys) => {
              const key = String(keys[0] ?? '');
              const hit = (data?.items ?? []).find(
                (item) => item.filename === key,
              );
              onSelect(hit ?? null);
            },
          }}
          locale={{
            emptyText:
              '镜像里没找到带 amd64 安装包的版本，试试「刷新镜像列表」',
          }}
        />
        <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          不支持 Windows-MCP 的版本已置灰，无法选中。
        </Paragraph>
      </Card>

      <Card title="缓存到堡垒机" size="small">
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          {selected ? (
            <Text>
              已选择：<Text strong>Python {selected.version}</Text>
              <Text code style={{ marginLeft: 8 }}>
                {selected.filename}
              </Text>
              <Text type="secondary" style={{ marginLeft: 8 }}>
                {humanSize(selected.size)}
              </Text>
            </Text>
          ) : (
            <Text type="secondary">
              请在上表选中一个「支持 Windows-MCP」的版本。
            </Text>
          )}
          <Space wrap>
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              disabled={!selected}
              loading={caching}
              onClick={cacheSelected}
            >
              {caching
                ? '正在缓存（可能要一两分钟）…'
                : selected?.cached
                  ? '重新缓存到堡垒机'
                  : '缓存到堡垒机'}
            </Button>
            {selected?.cached ? (
              <Text type="success">已缓存，可以直接进入第 2 步。</Text>
            ) : (
              <Text type="secondary">缓存完成后「下一步」才会亮起。</Text>
            )}
          </Space>
        </Space>
      </Card>
    </Space>
  );
};

export default InstallerStep;
