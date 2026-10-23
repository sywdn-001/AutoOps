/**
 * 扩展模块管理页：`/system/modules`。
 *
 * 流程：选择/拖入 `.mod.ops_ext` → `upload-check` 预览 → 实验性模块还要手敲风险确认语
 * → `install`。列表里的启用/卸载会同时刷新侧边栏（`@@initialState.moduleNav`），
 * 不需要用户手动刷新页面。
 */
import {
  DeleteOutlined,
  ExperimentOutlined,
  FileImageOutlined,
  FileTextOutlined,
  InboxOutlined,
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
  Descriptions,
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
  Upload,
} from 'antd';
import dayjs from 'dayjs';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { t } from '@/i18n';
import { resolveModuleIcon } from '@/modules/icons';
import {
  EXPERIMENTAL_ACK_PHRASE,
  moduleApi,
  moduleText,
} from '@/services/bastion/modules';
import type {
  ModuleItem,
  ModulePreview,
  ModuleText,
} from '@/services/bastion/types';

const { Text, Paragraph } = Typography;

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

const ISSUE_LABELS: Record<string, string> = {
  MODULE_EXISTS: '同名模块已安装，请先卸载旧版本',
  MODULE_RESERVED_ID: '模块 id 是系统保留 id，不能安装',
  EXPERIMENTAL_NOT_ACKED: '需要确认实验性功能风险',
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

const issueText = (issue: string): string => {
  const label = ISSUE_LABELS[issue.trim().toUpperCase()];
  return label ? t(label) : issue;
};

const renderTags = (item: ModuleItem | ModulePreview) => {
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

  const [installOpen, setInstallOpen] = useState(false);
  const [pickedFile, setPickedFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ModulePreview | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState('');
  const [installing, setInstalling] = useState(false);

  const [ackOpen, setAckOpen] = useState(false);
  const [ack, setAck] = useState('');

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

  const resetInstall = useCallback(() => {
    setPickedFile(null);
    setPreview(null);
    setCheckError('');
    setChecking(false);
    setAck('');
    setAckOpen(false);
  }, []);

  const openInstall = () => {
    resetInstall();
    setInstallOpen(true);
  };

  const handlePick = async (file: File) => {
    if (!file.name.toLowerCase().endsWith('.mod.ops_ext')) {
      message.error(t('请选择 .mod.ops_ext 扩展包'));
      return;
    }
    setPickedFile(file);
    setPreview(null);
    setCheckError('');
    setChecking(true);
    try {
      setPreview(await moduleApi.uploadCheck(file));
    } catch (error) {
      setCheckError(
        backendMessage(error, t('扩展包校验失败，请确认文件完整后重试')),
      );
    } finally {
      setChecking(false);
    }
  };

  const doInstall = async () => {
    if (!pickedFile || !preview) {
      return;
    }
    setInstalling(true);
    try {
      await moduleApi.install(
        pickedFile,
        preview.experimental ? ack.trim() : undefined,
      );
      message.success(
        t('扩展模块「{0}」安装成功', {
          0: renderModuleTitle(preview.name, preview.id),
        }),
      );
      setInstallOpen(false);
      resetInstall();
      await refreshAfterChange();
    } catch (error) {
      message.error(
        backendMessage(error, t('安装失败，请检查扩展包或后端日志')),
      );
    } finally {
      setInstalling(false);
    }
  };

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

  const installDisabled =
    !preview || checking || installing || preview.installable === false;

  const handleInstallClick = () => {
    if (!preview || installDisabled) {
      return;
    }
    if (preview.experimental) {
      setAck('');
      setAckOpen(true);
      return;
    }
    void doInstall();
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
              {item.builtin ? <Tag>{t('内置')}</Tag> : null}
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
            disabled={
              !canAdmin || Boolean(item.builtin) || Boolean(item.reserved)
            }
            onConfirm={() => void handleRemove(item)}
          >
            <Tooltip
              title={
                item.builtin
                  ? t('内置模块不可卸载')
                  : item.reserved
                    ? t('系统保留模块不可卸载')
                    : undefined
              }
            >
              <Button
                type="link"
                size="small"
                danger
                icon={<DeleteOutlined />}
                disabled={
                  !canAdmin || Boolean(item.builtin) || Boolean(item.reserved)
                }
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
      subTitle={t(
        '安装、启用、卸载以 .mod.ops_ext 打包的扩展功能；实验性模块需要确认风险后使用',
      )}
      extra={
        <Space wrap>
          <Button
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => void load()}
          >
            {t('刷新')}
          </Button>
          <Tooltip
            title={canAdmin ? undefined : t('只有管理员可以安装扩展模块')}
          >
            <Button
              type="primary"
              icon={<UploadOutlined />}
              disabled={!canAdmin}
              onClick={openInstall}
            >
              {t('安装扩展模块')}
            </Button>
          </Tooltip>
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
      {!canAdmin ? (
        <Alert
          type="info"
          showIcon
          message={t(
            '当前账号只能查看扩展模块，安装、启用与卸载需要管理员权限',
          )}
          style={{ marginBottom: 16 }}
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
        title={t('安装扩展模块')}
        open={installOpen}
        width={760}
        destroyOnHidden
        maskClosable={false}
        onCancel={() => {
          setInstallOpen(false);
          resetInstall();
        }}
        footer={[
          <Button
            key="cancel"
            onClick={() => {
              setInstallOpen(false);
              resetInstall();
            }}
          >
            {t('取消')}
          </Button>,
          <Button
            key="install"
            type="primary"
            loading={installing}
            disabled={installDisabled}
            onClick={handleInstallClick}
          >
            {t('安装')}
          </Button>,
        ]}
      >
        {!preview && !checking ? (
          <Upload.Dragger
            accept=".mod.ops_ext"
            multiple={false}
            maxCount={1}
            fileList={[]}
            showUploadList={false}
            disabled={checking || installing}
            beforeUpload={(file) => {
              void handlePick(file);
              return Upload.LIST_IGNORE;
            }}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">
              {t('点击或拖拽 .mod.ops_ext 扩展包到这里')}
            </p>
            <p className="ant-upload-hint">
              {t(
                '上传后只会解析 description.json 做安装前预览，不会执行模块代码',
              )}
            </p>
          </Upload.Dragger>
        ) : null}

        {checking ? (
          <Flex justify="center" align="center" style={{ padding: '32px 0' }}>
            <Spin />
            <Text style={{ marginLeft: 12 }}>{t('正在校验扩展包…')}</Text>
          </Flex>
        ) : null}

        {checkError ? (
          <Alert
            type="error"
            showIcon
            message={t('扩展包校验未通过')}
            description={checkError}
            style={{ marginTop: 16 }}
          />
        ) : null}

        {preview ? (
          <div>
            <Flex justify="space-between" align="flex-start" gap={16} wrap>
              <Space direction="vertical" size={4}>
                <Space size={8} wrap>
                  <Text strong style={{ fontSize: 16 }}>
                    {renderModuleTitle(preview.name, preview.id)}
                  </Text>
                  <Tag>{`v${preview.version || '?'}`}</Tag>
                  {preview.experimental ? (
                    <Tag color="magenta" icon={<ExperimentOutlined />}>
                      {t('实验性')}
                    </Tag>
                  ) : null}
                  {preview.official ? (
                    <Tag color="gold">{t('官方')}</Tag>
                  ) : null}
                </Space>
                <Text type="secondary" code>
                  {preview.id}
                </Text>
              </Space>
              <Button
                size="small"
                onClick={() => {
                  setPickedFile(null);
                  setPreview(null);
                  setCheckError('');
                }}
              >
                {t('重新选择')}
              </Button>
            </Flex>

            <Descriptions
              size="small"
              column={2}
              style={{ marginTop: 16 }}
              items={[
                {
                  key: 'author',
                  label: t('作者'),
                  children: preview.author || '-',
                },
                {
                  key: 'license',
                  label: t('许可证'),
                  children: preview.license || '-',
                },
                {
                  key: 'minBastionVersion',
                  label: t('最低堡垒机版本'),
                  children: preview.minBastionVersion || '-',
                },
                {
                  key: 'size',
                  label: t('包体积'),
                  children: formatBytes(preview.size),
                },
              ]}
            />

            {preview.description ? (
              <Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
                {moduleText(preview.description)}
              </Paragraph>
            ) : null}

            <div style={{ marginTop: 12 }}>{renderTags(preview)}</div>

            {preview.images.length > 0 ? (
              <div style={{ marginTop: 16 }}>
                <Text type="secondary">
                  {t('包内截图（安装前只解析描述文件，暂不能预览图片）')}
                </Text>
                <Flex gap={8} wrap style={{ marginTop: 8 }}>
                  {preview.images.map((image) => (
                    <Flex
                      key={image.path}
                      vertical
                      align="center"
                      justify="center"
                      gap={4}
                      style={{
                        width: 148,
                        padding: '16px 8px',
                        border: '1px dashed #d9d9d9',
                        borderRadius: 8,
                      }}
                    >
                      <FileImageOutlined
                        style={{ fontSize: 22, color: '#bfbfbf' }}
                      />
                      <Text
                        type="secondary"
                        style={{ fontSize: 12, textAlign: 'center' }}
                      >
                        {image.path.split('/').pop() || image.path}
                      </Text>
                    </Flex>
                  ))}
                </Flex>
              </div>
            ) : null}

            {preview.issues && preview.issues.length > 0 ? (
              <Alert
                type={preview.installable === false ? 'error' : 'warning'}
                showIcon
                style={{ marginTop: 16 }}
                message={
                  preview.installable === false
                    ? t('这个扩展包不能安装')
                    : t('安装前检查发现问题')
                }
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {preview.issues.map((issue) => (
                      <li key={issue}>{issueText(issue)}</li>
                    ))}
                  </ul>
                }
              />
            ) : null}
          </div>
        ) : null}
      </Modal>

      <Modal
        title={t('确认安装实验性模块')}
        open={ackOpen}
        destroyOnHidden
        maskClosable={false}
        okText={t('安装')}
        cancelText={t('取消')}
        confirmLoading={installing}
        okButtonProps={{
          danger: true,
          disabled: ack.trim() !== EXPERIMENTAL_ACK_PHRASE,
        }}
        onOk={() => void doInstall()}
        onCancel={() => setAckOpen(false)}
      >
        <Alert
          type="error"
          showIcon
          message={t('这是实验性功能')}
          description={t(
            '实验性模块可能修改系统行为，使用不当也许会造成系统损毁。请确认你了解并愿意承担风险。',
          )}
          style={{ marginBottom: 16 }}
        />
        <Paragraph style={{ marginBottom: 8 }}>
          {t('请逐字输入下面的确认语（不支持粘贴）：')}
        </Paragraph>
        <Paragraph
          style={{
            padding: '8px 12px',
            background: 'rgba(0, 0, 0, 0.04)',
            borderRadius: 6,
            marginBottom: 16,
          }}
        >
          <Text strong>{EXPERIMENTAL_ACK_PHRASE}</Text>
        </Paragraph>
        <Input.TextArea
          value={ack}
          autoSize={{ minRows: 2, maxRows: 4 }}
          placeholder={t('在这里逐字输入确认语')}
          status={
            ack && ack.trim() !== EXPERIMENTAL_ACK_PHRASE ? 'error' : undefined
          }
          onPaste={(event) => event.preventDefault()}
          onDrop={(event) => event.preventDefault()}
          onChange={(event) => setAck(event.target.value)}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t('首尾空白会被忽略，但文字必须与上方完全一致。')}
        </Text>
      </Modal>

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
