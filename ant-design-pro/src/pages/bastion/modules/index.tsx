/**
 * 扩展模块管理页：`/system/modules`。
 *
 * 安装流程：选择 `.mod.ops_ext` → 同步打开独立预览窗口 → upload-check 结果写入
 * localStorage 后由预览窗口展示详情并完成安装（含实验性确认语）。主窗口不再弹
 * 安装预览 Modal，只通过 storage 事件刷新列表/侧边栏。列表里的启用/卸载也会
 * 同时刷新侧边栏（`@@initialState.moduleNav`），不需要用户手动刷新页面。
 */
import {
  DeleteOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  ReloadOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { PageContainer } from '@ant-design/pro-components';
import { useAccess, useModel } from '@umijs/max';
import type { TableColumnsType } from 'antd';
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Flex,
  Input,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import dayjs from 'dayjs';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { t } from '@/i18n';
import { resolveModuleIcon } from '@/modules/icons';
import {
  MODULE_INSTALLED_STORAGE_PREFIX,
  moduleApi,
  moduleText,
} from '@/services/bastion/modules';
import type { ModuleItem, ModuleText } from '@/services/bastion/types';

const { Text } = Typography;

const TAG_LABELS: Record<string, string> = {
  experimental: '实验性',
  security: '安全',
  network: '网络',
  rdp: 'RDP',
  ssh: 'SSH',
  monitor: '监控',
  audit: '审计',
  storage: '存储',
  file: '文件',
  ai: 'AI',
};

const TAG_COLORS: Record<string, string> = {
  experimental: 'magenta',
  security: 'red',
  network: 'blue',
  rdp: 'blue',
  ssh: 'blue',
  monitor: 'cyan',
  audit: 'cyan',
  storage: 'green',
  file: 'green',
  ai: 'purple',
};

const backendMessage = (error: unknown, fallback: string): string => {
  const response = (error as { response?: { data?: { message?: string } } })
    ?.response;
  return (
    response?.data?.message ||
    (error instanceof Error ? error.message : '') ||
    fallback
  );
};

const formatBytes = (bytes: number): string => {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return '-';
  }
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${index === 0 || value >= 10 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
};

const formatTime = (value?: string): string => {
  if (!value) {
    return '-';
  }
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm') : value;
};

const textOf = (value: ModuleText): string =>
  typeof value === 'string' ? value : `${value.zh ?? ''} ${value.en ?? ''}`;

const tagLabel = (tag: string): string => {
  const label = TAG_LABELS[tag.trim().toLowerCase()];
  return label ? t(label) : tag;
};

const tagColor = (tag: string): string =>
  TAG_COLORS[tag.trim().toLowerCase()] ?? 'default';

const renderTags = (item: { tags: string[]; experimental: boolean }) => {
  const tags = [...item.tags];
  if (
    item.experimental &&
    !tags.some((tag) => tag.trim().toLowerCase() === 'experimental')
  ) {
    tags.unshift('experimental');
  }
  if (tags.length === 0) {
    return <Text type="secondary">-</Text>;
  }
  return (
    <Space size={[4, 4]} wrap>
      {tags.map((tag) => (
        <Tag
          key={tag}
          color={tagColor(tag)}
          icon={
            tag.trim().toLowerCase() === 'experimental' ? (
              <ExperimentOutlined />
            ) : undefined
          }
        >
          {tagLabel(tag)}
        </Tag>
      ))}
    </Space>
  );
};

const renderModuleTitle = (name: ModuleText, id: string): string =>
  moduleText(name, id);

const ModuleManagerPage = () => {
  const access = useAccess();
  const canAdmin = Boolean(access.canAdmin);
  const { setInitialState } = useModel('@@initialState');
  const { message } = App.useApp();

  const [items, setItems] = useState<ModuleItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [keyword, setKeyword] = useState('');
  const [togglingId, setTogglingId] = useState('');
  const [removingId, setRemovingId] = useState('');

  const [logItem, setLogItem] = useState<ModuleItem | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [logLoading, setLogLoading] = useState(false);

  const refreshNav = useCallback(async () => {
    const nav = await moduleApi.nav().catch(() => []);
    setInitialState?.((state) =>
      state ? { ...state, modules: nav, moduleNav: nav } : state,
    );
  }, [setInitialState]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await moduleApi.list();
      setItems(data);
      setLoadError('');
    } catch (error) {
      setItems([]);
      setLoadError(
        backendMessage(error, t('加载扩展模块列表失败，请稍后重试')),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const refreshAfterChange = useCallback(async () => {
    await Promise.all([load(), refreshNav()]);
  }, [load, refreshNav]);

  const filteredItems = useMemo(() => {
    const query = keyword.trim().toLowerCase();
    if (!query) {
      return items;
    }
    return items.filter((item) => {
      const haystack = [
        item.id,
        textOf(item.name),
        textOf(item.description),
        item.author,
        ...item.tags,
        ...item.nav.map((entry) => textOf(entry.title)),
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [items, keyword]);

  /**
   * 点击「安装扩展模块」：只做一件事 —— 在用户手势里同步打开预览窗口。
   *
   * 选文件必须在窗口内部完成：`window.open()` 会消耗掉此次点击的「瞬时用户激活」，
   * 同一手势里紧跟着的 `input.click()` 拉不起文件选择器（实测浏览器直接忽略），
   * 这就是「弹出窗口但没有文件对话框」的原因。窗口里的按钮是一次新的手势，才能拉起选择器。
   */
  const handleInstallClick = () => {
    if (!canAdmin) {
      message.error(t('只有管理员可以安装扩展模块'));
      return;
    }
    const win = window.open(
      `${window.location.origin}/module-preview#pick`,
      '_blank',
      'width=1040,height=820',
    );
    if (!win) {
      message.warning(t('浏览器拦截了弹窗，请允许本站弹窗后重试'));
    }
  };

  // 预览窗口安装成功后会写入 module-installed:<id>；storage 事件只在其它窗口写入时触发，
  // 因此这里正好接收“弹窗装好了”的信号，刷新列表与侧边栏。
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (!event.key?.startsWith(MODULE_INSTALLED_STORAGE_PREFIX)) {
        return;
      }
      const moduleId = event.key.slice(MODULE_INSTALLED_STORAGE_PREFIX.length);
      void refreshAfterChange();
      message.success(
        moduleId
          ? t('扩展模块「{0}」安装成功', { 0: moduleId })
          : t('扩展模块安装成功'),
      );
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [message, refreshAfterChange]);

  const handleToggle = async (item: ModuleItem, checked: boolean) => {
    setTogglingId(item.id);
    try {
      if (checked) {
        await moduleApi.enable(item.id);
      } else {
        await moduleApi.disable(item.id);
      }
      message.success(
        checked
          ? t('已启用「{0}」', { 0: renderModuleTitle(item.name, item.id) })
          : t('已禁用「{0}」', { 0: renderModuleTitle(item.name, item.id) }),
      );
      await refreshAfterChange();
    } catch (error) {
      message.error(backendMessage(error, t('操作失败，请稍后重试')));
    } finally {
      setTogglingId('');
    }
  };

  const handleRemove = async (item: ModuleItem) => {
    setRemovingId(item.id);
    try {
      // 后端要求先禁用再卸载
      if (item.enabled) {
        await moduleApi.disable(item.id);
      }
      await moduleApi.remove(item.id);
      message.success(
        t('已卸载「{0}」', { 0: renderModuleTitle(item.name, item.id) }),
      );
      await refreshAfterChange();
    } catch (error) {
      message.error(backendMessage(error, t('卸载失败，请稍后重试')));
    } finally {
      setRemovingId('');
    }
  };

  const openLogs = async (item: ModuleItem) => {
    setLogItem(item);
    setLogLines([]);
    setLogLoading(true);
    try {
      setLogLines(await moduleApi.logs(item.id));
    } catch (error) {
      setLogLines([backendMessage(error, t('日志加载失败'))]);
    } finally {
      setLogLoading(false);
    }
  };

  const columns: TableColumnsType<ModuleItem> = [
    {
      title: t('模块'),
      dataIndex: 'id',
      key: 'name',
      width: 280,
      render: (_value, item) => (
        <Space align="start" size={10}>
          <span style={{ fontSize: 20, lineHeight: 1 }}>
            {resolveModuleIcon(item.icon)}
          </span>
          <Space direction="vertical" size={2}>
            <Space size={8} wrap>
              <Text strong>{renderModuleTitle(item.name, item.id)}</Text>
            </Space>
            <Text type="secondary" code>
              {item.id}
            </Text>
            {item.errors.length > 0 ? (
              <Space direction="vertical" size={0}>
                {item.errors.map((error) => (
                  <Text key={error} type="danger" style={{ fontSize: 12 }}>
                    {error}
                  </Text>
                ))}
              </Space>
            ) : null}
          </Space>
        </Space>
      ),
    },
    {
      title: t('版本'),
      dataIndex: 'version',
      key: 'version',
      width: 100,
      render: (value: string) => <Text>{value || '-'}</Text>,
    },
    {
      title: t('作者'),
      dataIndex: 'author',
      key: 'author',
      width: 130,
      render: (value: string, item) =>
        item.official ? (
          <Tag color="gold">{t('官方')}</Tag>
        ) : (
          <Text>{value || '-'}</Text>
        ),
    },
    {
      title: t('标签'),
      dataIndex: 'tags',
      key: 'tags',
      width: 200,
      render: (_value, item) => renderTags(item),
    },
    {
      title: t('启用'),
      dataIndex: 'enabled',
      key: 'enabled',
      width: 100,
      render: (_value, item) => (
        <Space direction="vertical" size={2}>
          <Switch
            size="small"
            checked={item.enabled}
            loading={togglingId === item.id}
            disabled={!canAdmin}
            onChange={(checked) => void handleToggle(item, checked)}
          />
          <Text
            type={item.loaded ? 'secondary' : 'warning'}
            style={{ fontSize: 12 }}
          >
            {item.loaded ? t('已加载') : t('未加载')}
          </Text>
        </Space>
      ),
    },
    {
      title: t('体积'),
      dataIndex: 'size',
      key: 'size',
      width: 100,
      render: (value: number) => <Text>{formatBytes(value)}</Text>,
    },
    {
      title: t('安装时间'),
      dataIndex: 'installedAt',
      key: 'installedAt',
      width: 160,
      render: (value?: string) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {formatTime(value)}
        </Text>
      ),
    },
    {
      title: t('操作'),
      key: 'actions',
      width: 180,
      fixed: 'right',
      render: (_value, item) => (
        <Space size={0} wrap>
          <Button
            type="link"
            size="small"
            icon={<FileTextOutlined />}
            onClick={() => void openLogs(item)}
          >
            {t('日志')}
          </Button>
          <Popconfirm
            title={t('确定卸载「{0}」？', {
              0: renderModuleTitle(item.name, item.id),
            })}
            description={t(
              '卸载后模块页面和菜单会立即消失，需要重新安装才能使用。',
            )}
            okText={t('卸载')}
            cancelText={t('取消')}
            okButtonProps={{ danger: true }}
            disabled={!canAdmin || Boolean(item.reserved)}
            onConfirm={() => void handleRemove(item)}
          >
            <Tooltip
              title={item.reserved ? t('系统保留模块不可卸载') : undefined}
            >
              <Button
                type="link"
                size="small"
                danger
                icon={<DeleteOutlined />}
                disabled={!canAdmin || Boolean(item.reserved)}
                loading={removingId === item.id}
              >
                {t('卸载')}
              </Button>
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <PageContainer
      title={t('扩展模块')}
      subTitle={t('安装、启用、卸载以 .mod.ops_ext 打包的扩展功能')}
      extra={
        <Space wrap>
          <Button
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => void load()}
          >
            {t('刷新')}
          </Button>
          <Button
            type="primary"
            icon={<UploadOutlined />}
            disabled={!canAdmin}
            onClick={handleInstallClick}
          >
            {t('安装扩展模块')}
          </Button>
        </Space>
      }
    >
      {loadError ? (
        <Alert
          type="error"
          showIcon
          message={loadError}
          style={{ marginBottom: 16 }}
          action={
            <Button size="small" onClick={() => void load()}>
              {t('重试')}
            </Button>
          }
        />
      ) : null}

      <Card>
        <Flex
          justify="space-between"
          align="center"
          gap={12}
          wrap
          style={{ marginBottom: 16 }}
        >
          <Input.Search
            allowClear
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder={t('按名称、id 或标签搜索')}
            style={{ maxWidth: 320 }}
          />
          <Text type="secondary">
            {t('共 {0} 个模块', { 0: items.length })}
          </Text>
        </Flex>
        <Table<ModuleItem>
          rowKey="id"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={filteredItems}
          pagination={false}
          scroll={{ x: 1080 }}
          locale={{
            emptyText: <Empty description={t('尚未安装任何扩展模块')} />,
          }}
        />
      </Card>

      <Modal
        title={
          logItem
            ? t('模块日志：{0}', {
                0: renderModuleTitle(logItem.name, logItem.id),
              })
            : t('模块日志')
        }
        open={Boolean(logItem)}
        width={860}
        destroyOnHidden
        onCancel={() => setLogItem(null)}
        footer={[
          <Button
            key="refresh"
            icon={<ReloadOutlined />}
            loading={logLoading}
            onClick={() => {
              if (logItem) {
                void openLogs(logItem);
              }
            }}
          >
            {t('刷新')}
          </Button>,
          <Button key="close" type="primary" onClick={() => setLogItem(null)}>
            {t('关闭')}
          </Button>,
        ]}
      >
        <Spin spinning={logLoading}>
          <pre
            style={{
              maxHeight: '60vh',
              overflow: 'auto',
              margin: 0,
              padding: 12,
              background: '#fafafa',
              borderRadius: 6,
              fontSize: 12,
              lineHeight: 1.6,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {logLines.length > 0 ? logLines.join('\n') : t('暂无日志')}
          </pre>
        </Spin>
      </Modal>
    </PageContainer>
  );
};

export default ModuleManagerPage;
