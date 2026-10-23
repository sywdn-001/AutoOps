/**
 * 扩展模块安装前详情页：`/module-preview`（layout: false，独立窗口打开）。
 *
 * 安装页在用户手势里先 `window.open('about:blank')`，upload-check 成功后把结果写入
 * `localStorage['module-preview:<token>']`，再把新窗口导航到 `/module-preview#<token>`。
 * 本页只读 localStorage，不调用需要权限的接口，也不加载 `/asset/`（模块尚未安装）。
 */
import { ExperimentOutlined } from '@ant-design/icons';
import type { TableColumnsType } from 'antd';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Divider,
  Empty,
  Flex,
  Image,
  Input,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ReactNode } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { t } from '@/i18n';
import { resolveModuleIcon } from '@/modules/icons';
import {
  createModulePreviewToken,
  EXPERIMENTAL_ACK_PHRASE,
  moduleApi,
  modulePreviewAssetUrl,
  modulePreviewImageUrl,
  moduleText,
  notifyModuleInstalled,
  readModulePreview,
  writeModulePreview,
} from '@/services/bastion/modules';
import type {
  ModuleNavItem,
  ModulePreview,
  ModuleSetting,
} from '@/services/bastion/types';

const { Text, Paragraph, Title, Link } = Typography;

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

const tagText = (tag: string): string => {
  const label = TAG_LABELS[tag.trim().toLowerCase()];
  return label ? t(label) : tag;
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

const formatRange = (setting: ModuleSetting): string => {
  const { min, max } = setting;
  if (typeof min === 'number' && typeof max === 'number') {
    return `${min} ~ ${max}`;
  }
  if (typeof min === 'number') {
    return `≥ ${min}`;
  }
  if (typeof max === 'number') {
    return `≤ ${max}`;
  }
  return '-';
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

/** README 里允许少量行内 HTML：`<br>` 转成换行，其它标签直接去掉只留文本。 */
const cleanInlineHtml = (text: string): string =>
  text.replace(/<br\s*\/?>/gi, '\n').replace(/<\/?[a-zA-Z][^>]*>/g, '');

const isRelativeUrl = (url: string): boolean =>
  !/^(?:[a-z][a-z0-9+.-]*:|#|\/)/i.test(url);

/**
 * 行内 markdown：图片 / **加粗** / `代码` / *斜体* / [文字](链接)。
 * `assetUrl` 用于把 README 里的相对图片路径换成本次预览的临时 asset 地址。
 */
const renderInline = (
  raw: string,
  keyPrefix: string,
  assetUrl?: (path: string) => string,
): ReactNode[] => {
  const tokens = cleanInlineHtml(raw)
    .split(
      /(!\[[^\]]*\]\([^)]+\)|\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|\[[^\]]+\]\([^)]+\))/g,
    )
    .filter((part) => part !== '');
  return tokens.map((part, index) => {
    const key = `${keyPrefix}-${index}`;
    const image = /^!\[([^\]]*)\]\(([^)]+)\)$/.exec(part);
    if (image) {
      const src =
        assetUrl && isRelativeUrl(image[2]) ? assetUrl(image[2]) : image[2];
      return (
        <Image
          key={key}
          src={src}
          alt={image[1]}
          width={220}
          style={{ borderRadius: 6, maxWidth: '100%' }}
        />
      );
    }
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) {
      return (
        <Text key={key} strong>
          {part.slice(2, -2)}
        </Text>
      );
    }
    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
      return (
        <Text key={key} code>
          {part.slice(1, -1)}
        </Text>
      );
    }
    if (part.length > 2 && part.startsWith('*') && part.endsWith('*')) {
      return (
        <Text key={key} italic>
          {part.slice(1, -1)}
        </Text>
      );
    }
    const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part);
    if (link) {
      return (
        <Link key={key} href={link[2]} target="_blank" rel="noreferrer">
          {link[1]}
        </Link>
      );
    }
    return (
      <Text key={key} style={{ whiteSpace: 'pre-wrap' }}>
        {part}
      </Text>
    );
  });
};

const splitTableRow = (line: string): string[] =>
  line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());

const isTableSeparator = (line: string): boolean =>
  /^\|?[\s:|-]*-{2,}[\s:|-]*\|?$/.test(line.trim());

type ListEntry = { indent: number; ordered: boolean; text: string };

type ListNode = {
  id: number;
  text: string;
  ordered: boolean;
  children: ListNode[];
};

/** 按缩进把连续的列表行组装成树，至少让 2 空格缩进的子列表有层级。 */
const buildListTree = (entries: ListEntry[]): ListNode[] => {
  const roots: ListNode[] = [];
  const stack: { indent: number; children: ListNode[] }[] = [
    { indent: -1, children: roots },
  ];
  let seq = 0;
  for (const entry of entries) {
    while (stack.length > 1 && entry.indent <= stack[stack.length - 1].indent) {
      stack.pop();
    }
    const node: ListNode = {
      id: seq,
      text: entry.text,
      ordered: entry.ordered,
      children: [],
    };
    seq += 1;
    stack[stack.length - 1].children.push(node);
    stack.push({ indent: entry.indent, children: node.children });
  }
  return roots;
};

const renderListNodes = (
  nodes: ListNode[],
  keyPrefix: string,
  assetUrl?: (path: string) => string,
): ReactNode => {
  const style = { margin: '8px 0', paddingLeft: 22 };
  const items = nodes.map((node) => {
    const nodeKey = `${keyPrefix}-${node.id}`;
    return (
      <li key={nodeKey}>
        {renderInline(node.text, nodeKey, assetUrl)}
        {node.children.length > 0
          ? renderListNodes(node.children, nodeKey, assetUrl)
          : null}
      </li>
    );
  });
  const ordered = nodes[0]?.ordered;
  return ordered ? (
    <ol key={keyPrefix} style={style}>
      {items}
    </ol>
  ) : (
    <ul key={keyPrefix} style={style}>
      {items}
    </ul>
  );
};

/** 极简 README 渲染：标题 / 列表 / 表格 / 代码块 / 引用 / 分隔线 / 段落，不引入 markdown 依赖。 */
const renderReadme = (
  readme: string,
  skipTitles: string[] = [],
  assetUrl?: (path: string) => string,
): ReactNode[] => {
  const lines = readme.replace(/\r\n?/g, '\n').split('\n');
  const nodes: ReactNode[] = [];
  const skip = new Set(
    skipTitles.map((item) => item.trim().toLowerCase()).filter(Boolean),
  );
  let cursor = 0;
  let nodeIndex = 0;
  let skippedTitle = false;
  const nextKey = () => `readme-${nodeIndex++}`;

  while (cursor < lines.length) {
    const line = lines[cursor];

    if (line.trim().startsWith('```')) {
      const code: string[] = [];
      cursor += 1;
      while (cursor < lines.length && !lines[cursor].trim().startsWith('```')) {
        code.push(lines[cursor]);
        cursor += 1;
      }
      if (cursor < lines.length) {
        cursor += 1;
      }
      nodes.push(
        <pre
          key={nextKey()}
          style={{
            margin: '8px 0',
            padding: 12,
            background: '#fafafa',
            borderRadius: 6,
            fontSize: 12,
            lineHeight: 1.6,
            overflowX: 'auto',
          }}
        >
          <code>{code.join('\n')}</code>
        </pre>,
      );
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const text = heading[2].trim();
      // README 通常以模块名开头，和页面标题重复，跳过第一个同名的一级标题。
      if (
        !skippedTitle &&
        heading[1].length === 1 &&
        skip.has(text.toLowerCase())
      ) {
        skippedTitle = true;
        cursor += 1;
        continue;
      }
      skippedTitle = true;
      const level = Math.min(heading[1].length, 5) as 1 | 2 | 3 | 4 | 5;
      nodes.push(
        <Title
          key={nextKey()}
          level={level}
          style={{ marginTop: level === 1 ? 8 : 16 }}
        >
          {renderInline(text, nextKey(), assetUrl)}
        </Title>,
      );
      cursor += 1;
      continue;
    }

    // 表格：| a | b | + |---|---|
    if (
      line.trim().startsWith('|') &&
      cursor + 1 < lines.length &&
      isTableSeparator(lines[cursor + 1])
    ) {
      const header = splitTableRow(line);
      cursor += 2;
      const rows: string[][] = [];
      while (cursor < lines.length && lines[cursor].trim().startsWith('|')) {
        rows.push(splitTableRow(lines[cursor]));
        cursor += 1;
      }
      const columns: TableColumnsType<Record<string, string>> = header.map(
        (cell, index) => ({
          title: renderInline(cell || `#${index + 1}`, `th-${index}`, assetUrl),
          dataIndex: `c${index}`,
          key: `c${index}`,
          render: (value: string, _record, rowIndex: number) =>
            renderInline(
              String(value ?? ''),
              `td-${index}-${rowIndex}`,
              assetUrl,
            ),
        }),
      );
      const dataSource = rows.map((cells, rowIndex) => {
        const record: Record<string, string> = { key: `r${rowIndex}` };
        header.forEach((_, cellIndex) => {
          record[`c${cellIndex}`] = cells[cellIndex] ?? '';
        });
        return record;
      });
      nodes.push(
        <Table
          key={nextKey()}
          size="small"
          bordered
          pagination={false}
          columns={columns}
          dataSource={dataSource}
          style={{ margin: '8px 0' }}
          scroll={{ x: 'max-content' }}
        />,
      );
      continue;
    }

    // 分隔线：--- / *** / ___
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      nodes.push(<Divider key={nextKey()} style={{ margin: '12px 0' }} />);
      cursor += 1;
      continue;
    }

    // 有序/无序列表（含 2 空格以上缩进的子列表）。
    if (/^(\s*)(?:[-*]|\d+[.)])\s+/.test(line)) {
      const entries: ListEntry[] = [];
      while (cursor < lines.length) {
        const match = /^(\s*)([-*]|\d+[.)])\s+(.*)$/.exec(lines[cursor]);
        if (!match) {
          break;
        }
        entries.push({
          indent: match[1].replace(/\t/g, '  ').length,
          ordered: /^\d/.test(match[2]),
          text: match[3],
        });
        cursor += 1;
      }
      nodes.push(renderListNodes(buildListTree(entries), nextKey(), assetUrl));
      continue;
    }

    // 引用块：> 开头连续行
    if (/^\s*>\s?/.test(line)) {
      const quote: string[] = [];
      while (cursor < lines.length && /^\s*>\s?/.test(lines[cursor])) {
        quote.push(lines[cursor].replace(/^\s*>\s?/, ''));
        cursor += 1;
      }
      nodes.push(
        <Paragraph
          key={nextKey()}
          type="secondary"
          style={{
            margin: '8px 0',
            paddingLeft: 12,
            borderLeft: '3px solid #f0f0f0',
            whiteSpace: 'pre-wrap',
          }}
        >
          {renderInline(quote.join('\n'), 'quote', assetUrl)}
        </Paragraph>,
      );
      continue;
    }

    if (!line.trim()) {
      cursor += 1;
      continue;
    }

    const paragraph: string[] = [];
    while (
      cursor < lines.length &&
      lines[cursor].trim() &&
      !/^(\s*)(?:[-*]|\d+[.)])\s+/.test(lines[cursor]) &&
      !/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(lines[cursor]) &&
      !/^\s*>\s?/.test(lines[cursor]) &&
      !/^(#{1,6})\s+/.test(lines[cursor]) &&
      !lines[cursor].trim().startsWith('```') &&
      !lines[cursor].trim().startsWith('|')
    ) {
      paragraph.push(lines[cursor].trim());
      cursor += 1;
    }
    nodes.push(
      <Paragraph
        key={nextKey()}
        style={{ marginBottom: 8, whiteSpace: 'pre-wrap' }}
      >
        {renderInline(paragraph.join('\n'), 'para', assetUrl)}
      </Paragraph>,
    );
  }

  return nodes;
};

const PreviewImage = ({
  src,
  alt,
  width = 180,
}: {
  src: string;
  alt: string;
  width?: number;
}) => {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={<Text type="secondary">{alt}</Text>}
        style={{ margin: 0 }}
      />
    );
  }
  return (
    <Image
      src={src}
      alt={alt}
      width={width}
      style={{ borderRadius: 6, objectFit: 'cover' }}
      onError={() => setFailed(true)}
    />
  );
};

const ModulePreviewPage = () => {
  const [token, setToken] = useState(() =>
    window.location.hash.replace(/^#/, ''),
  );
  const [preview, setPreview] = useState<ModulePreview | null>(() =>
    token ? readModulePreview(token) : null,
  );
  const [ack, setAck] = useState('');
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState('');
  const [installed, setInstalled] = useState(false);

  useEffect(() => {
    const sync = () => {
      const next = window.location.hash.replace(/^#/, '');
      setToken(next);
      setPreview(next ? readModulePreview(next) : null);
    };
    window.addEventListener('hashchange', sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener('hashchange', sync);
      window.removeEventListener('storage', sync);
    };
  }, []);

  useEffect(() => {
    if (preview) {
      document.title = `${moduleText(preview.name, preview.id)} - ${t('模块详情')}`;
    } else if (!token || token === 'pick') {
      document.title = t('选择扩展包');
    }
  }, [preview, token]);

  // 主页面只负责用 window.open 打开本窗口（#pick）——window.open 会消耗掉那次点击的
  // 瞬时用户激活，同一手势里再 input.click() 拉不起文件选择器。所以选文件放在这里，
  // 由本窗口里的按钮点一次，那是一次全新的用户手势。
  const pickerRef = useRef<HTMLInputElement | null>(null);
  const [picking, setPicking] = useState(false);
  const [pickError, setPickError] = useState('');

  const handlePick = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    event.target.value = '';
    if (!file) {
      return;
    }
    if (!file.name.toLowerCase().endsWith('.mod.ops_ext')) {
      setPickError(t('请选择 .mod.ops_ext 扩展包'));
      return;
    }
    setPicking(true);
    setPickError('');
    try {
      const data = await moduleApi.uploadCheck(file);
      const nextToken = data.previewToken || createModulePreviewToken();
      if (!writeModulePreview(nextToken, data)) {
        setPickError(t('无法保存预览数据，请检查浏览器存储空间后重试'));
        return;
      }
      setToken(nextToken);
      setPreview(data);
      window.location.hash = encodeURIComponent(nextToken);
    } catch (error) {
      setPickError(
        backendMessage(error, t('扩展包校验失败，请确认文件完整后重试')),
      );
    } finally {
      setPicking(false);
    }
  };

  const navColumns: TableColumnsType<ModuleNavItem> = useMemo(
    () => [
      {
        title: t('标识'),
        dataIndex: 'key',
        key: 'key',
        width: 120,
      },
      {
        title: t('菜单标题'),
        dataIndex: 'title',
        key: 'title',
        render: (_value, item) => moduleText(item.title, item.key),
      },
      {
        title: t('路径'),
        dataIndex: 'path',
        key: 'path',
        render: (value: string) => <Text code>{value}</Text>,
      },
      {
        title: t('类型'),
        dataIndex: 'type',
        key: 'type',
        width: 90,
        render: (value: string) => (
          <Tag>{value === 'js' ? 'JS' : 'iframe'}</Tag>
        ),
      },
      {
        title: t('入口'),
        dataIndex: 'entry',
        key: 'entry',
        render: (value?: string) => value || '-',
      },
      {
        title: t('权限'),
        dataIndex: 'permission',
        key: 'permission',
        render: (value?: string) => (value ? <Text code>{value}</Text> : '-'),
      },
    ],
    [],
  );

  const settingColumns: TableColumnsType<ModuleSetting> = useMemo(
    () => [
      {
        title: t('配置键'),
        dataIndex: 'key',
        key: 'key',
        render: (value: string, item) => (
          <Space direction="vertical" size={0}>
            <Text code>{item.rawKey || value}</Text>
            {item.rawKey ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {value}
              </Text>
            ) : null}
          </Space>
        ),
      },
      {
        title: t('名称'),
        dataIndex: 'label',
        key: 'label',
        render: (_value, item) => moduleText(item.label, item.key),
      },
      {
        title: t('类型'),
        dataIndex: 'type',
        key: 'type',
        width: 80,
      },
      {
        title: t('默认值'),
        dataIndex: 'default',
        key: 'default',
        width: 100,
        render: (value?: string) => value || '-',
      },
      {
        title: t('取值范围'),
        key: 'range',
        width: 110,
        render: (_value, item) => formatRange(item),
      },
    ],
    [],
  );

  if (!preview) {
    if (!token || token === 'pick') {
      return (
        <div
          style={{ display: 'flex', justifyContent: 'center', paddingTop: 80 }}
        >
          <Card title={t('选择扩展包')} style={{ width: 560 }}>
            <Paragraph type="secondary">
              {t(
                '选择一个 .mod.ops_ext 扩展包，这里会显示它的功能说明与导航、参数、权限，并可以直接安装。',
              )}
            </Paragraph>
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Button
                type="primary"
                icon={<ExperimentOutlined />}
                loading={picking}
                onClick={() => pickerRef.current?.click()}
              >
                {t('选择 .mod.ops_ext 文件')}
              </Button>
              {pickError ? (
                <Alert type="error" showIcon message={pickError} />
              ) : null}
            </Space>
            <input
              ref={pickerRef}
              type="file"
              accept=".mod.ops_ext"
              hidden
              onChange={handlePick}
            />
          </Card>
        </div>
      );
    }
    return (
      <div style={{ paddingTop: 160, textAlign: 'center' }}>
        <Spin size="large" />
        <Title level={5} style={{ marginTop: 20 }}>
          {t('等待模块解析结果…')}
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 0 }}>
          {t('如果长时间没有结果，请回到扩展模块页重新选择扩展包。')}
        </Paragraph>
      </div>
    );
  }

  const issues = preview.issues ?? [];
  const fatal = preview.installable === false;
  const tags = [...preview.tags];
  if (
    preview.experimental &&
    !tags.some((tag) => tag.trim().toLowerCase() === 'experimental')
  ) {
    tags.unshift('experimental');
  }

  const effectiveToken = preview.previewToken || token;
  const isExperimental =
    preview.experimental ||
    tags.some((tag) => tag.trim().toLowerCase() === 'experimental');
  const canInstall =
    !installed &&
    !installing &&
    preview.installable !== false &&
    Boolean(effectiveToken) &&
    (!isExperimental || ack.trim() === EXPERIMENTAL_ACK_PHRASE);

  const previewAssetUrl = (path: string): string =>
    modulePreviewAssetUrl(effectiveToken, path);

  const handleInstall = async () => {
    if (!canInstall) {
      return;
    }
    setInstalling(true);
    setInstallError('');
    try {
      await moduleApi.installPreview(
        effectiveToken,
        isExperimental ? ack.trim() : '',
      );
      setInstalled(true);
      notifyModuleInstalled(preview.id);
    } catch (error) {
      setInstallError(backendMessage(error, t('安装失败，请稍后重试')));
    } finally {
      setInstalling(false);
    }
  };

  return (
    <div
      style={{ maxWidth: 1040, margin: '0 auto', padding: '24px 16px 48px' }}
    >
      <Flex align="flex-start" justify="space-between" gap={16} wrap>
        <Space align="start" size={12}>
          <span style={{ fontSize: 28, lineHeight: 1.2 }}>
            {resolveModuleIcon(preview.icon)}
          </span>
          <Space direction="vertical" size={4}>
            <Space size={8} wrap>
              <Title level={3} style={{ margin: 0 }}>
                {moduleText(preview.name, preview.id)}
              </Title>
              <Tag>{`v${preview.version || '?'}`}</Tag>
              {preview.official ? <Tag color="gold">{t('官方')}</Tag> : null}
              {preview.experimental ? (
                <Tag color="magenta" icon={<ExperimentOutlined />}>
                  {t('实验性')}
                </Tag>
              ) : null}
            </Space>
            <Text type="secondary" code>
              {preview.id}
            </Text>
          </Space>
        </Space>
      </Flex>

      <Space size={[4, 4]} wrap style={{ marginTop: 12 }}>
        {tags.length > 0
          ? tags.map((tag) => (
              <Tag
                key={tag}
                color={TAG_COLORS[tag.trim().toLowerCase()] ?? 'default'}
                icon={
                  tag.trim().toLowerCase() === 'experimental' ? (
                    <ExperimentOutlined />
                  ) : undefined
                }
              >
                {tagText(tag)}
              </Tag>
            ))
          : null}
      </Space>

      <Descriptions
        size="small"
        column={{ xs: 1, sm: 2, md: 3 }}
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
            key: 'homepage',
            label: t('主页'),
            children: preview.homepage ? (
              <Link href={preview.homepage} target="_blank">
                {preview.homepage}
              </Link>
            ) : (
              '-'
            ),
          },
          {
            key: 'minBastionVersion',
            label: t('最低宿主版本'),
            children: preview.minBastionVersion || '-',
          },
          {
            key: 'size',
            label: t('包体积'),
            children: formatBytes(preview.size),
          },
          {
            key: 'fileCount',
            label: t('文件数'),
            children: preview.fileCount ?? '-',
          },
          {
            key: 'folders',
            label: t('来源目录'),
            children:
              preview.folders && preview.folders.length > 0 ? (
                <Space size={[4, 4]} wrap>
                  {preview.folders.map((folder) => (
                    <Tag key={folder}>
                      {folder.replace(/\/+$/, '') || folder}
                    </Tag>
                  ))}
                </Space>
              ) : (
                '-'
              ),
          },
        ]}
      />

      {issues.length > 0 ? (
        <Alert
          type={fatal ? 'error' : 'warning'}
          showIcon
          style={{ marginTop: 16 }}
          message={fatal ? t('这个扩展包不能安装') : t('安装前检查发现问题')}
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {issues.map((issue) => (
                <li key={issue}>{issue}</li>
              ))}
            </ul>
          }
        />
      ) : null}

      <Card
        title={t('功能说明')}
        size="small"
        style={{ marginTop: 16 }}
        extra={
          preview.readmeName ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {preview.readmeName}
            </Text>
          ) : undefined
        }
      >
        {preview.readme ? (
          renderReadme(
            preview.readme,
            [
              preview.id,
              moduleText(preview.name, preview.id),
              ...Object.values(preview.name ?? {}),
            ],
            previewAssetUrl,
          )
        ) : (
          <Empty description={t('模块包里没有 README')} />
        )}
      </Card>

      <Card title={t('导航页面')} size="small" style={{ marginTop: 16 }}>
        <Table<ModuleNavItem>
          rowKey={(item) => `${item.key}-${item.path}`}
          size="small"
          columns={navColumns}
          dataSource={preview.nav}
          pagination={false}
        />
      </Card>

      <Card title={t('参数设置')} size="small" style={{ marginTop: 16 }}>
        <Table<ModuleSetting>
          rowKey="key"
          size="small"
          columns={settingColumns}
          dataSource={preview.settings}
          pagination={false}
        />
      </Card>

      <Card title={t('权限与依赖')} size="small" style={{ marginTop: 16 }}>
        <Space direction="vertical" size={8}>
          <Space size={[4, 4]} wrap>
            <Text type="secondary">{t('权限')}：</Text>
            {preview.permissions.length > 0 ? (
              preview.permissions.map((item) => (
                <Tag key={item} color="blue">
                  {item}
                </Tag>
              ))
            ) : (
              <Text>-</Text>
            )}
          </Space>
          <Space size={[4, 4]} wrap>
            <Text type="secondary">{t('依赖模块')}：</Text>
            {preview.requires.length > 0 ? (
              preview.requires.map((item) => <Tag key={item}>{item}</Tag>)
            ) : (
              <Text>-</Text>
            )}
          </Space>
        </Space>
      </Card>

      <Card title={t('包内目录')} size="small" style={{ marginTop: 16 }}>
        <Space direction="vertical" size={10}>
          <Space size={[4, 4]} wrap>
            {preview.folders && preview.folders.length > 0 ? (
              preview.folders.map((folder) => (
                <Tag key={folder}>{folder.replace(/\/+$/, '') || folder}</Tag>
              ))
            ) : (
              <Text type="secondary">-</Text>
            )}
          </Space>
          <Text type="secondary">
            {t('共 {0} 个文件', {
              0: preview.fileCount ?? '-',
            })}
          </Text>
          {preview.images.length > 0 ? (
            <Space direction="vertical" size={8}>
              <Text type="secondary">{t('包内图片')}：</Text>
              <Image.PreviewGroup>
                <Flex gap={12} wrap>
                  {preview.images.map((image) => (
                    <Flex
                      key={image.path}
                      vertical
                      gap={4}
                      style={{ maxWidth: 180 }}
                    >
                      <PreviewImage
                        src={modulePreviewImageUrl(effectiveToken, image)}
                        alt={image.path.split('/').pop() || image.path}
                      />
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {image.path.split('/').pop() || image.path}
                      </Text>
                    </Flex>
                  ))}
                </Flex>
              </Image.PreviewGroup>
            </Space>
          ) : (
            <Space direction="vertical" size={6}>
              <Text type="secondary">{t('包内图片')}：</Text>
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={t('模块包里没有截图')}
              />
            </Space>
          )}
        </Space>
      </Card>

      <Card title={t('安装')} size="small" style={{ marginTop: 16 }}>
        {installed ? (
          <Alert
            type="success"
            showIcon
            message={t('模块已安装')}
            description={t('主窗口会自动刷新列表和侧边栏，可以关闭本窗口了。')}
            action={
              <Button
                size="small"
                type="primary"
                onClick={() => window.close()}
              >
                {t('关闭窗口')}
              </Button>
            }
          />
        ) : (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Descriptions
              size="small"
              column={2}
              items={[
                {
                  key: 'source',
                  label: t('来源'),
                  children: t('上传的扩展包'),
                },
                {
                  key: 'version',
                  label: t('版本'),
                  children: `v${preview.version || '?'}`,
                },
                {
                  key: 'size',
                  label: t('包体积'),
                  children: formatBytes(preview.size),
                },
                {
                  key: 'fileCount',
                  label: t('文件数'),
                  children: preview.fileCount ?? '-',
                },
              ]}
            />

            {fatal ? (
              <Alert
                type="error"
                showIcon
                message={t('这个扩展包不能安装')}
                description={t('请先解决上面列出的问题，再重新选择扩展包。')}
              />
            ) : null}

            {isExperimental ? (
              <>
                <Paragraph style={{ marginBottom: 8 }}>
                  {t('请逐字输入下面的确认语（不支持粘贴）：')}
                </Paragraph>
                <Paragraph
                  style={{
                    padding: '8px 12px',
                    background: 'rgba(0, 0, 0, 0.04)',
                    borderRadius: 6,
                    marginBottom: 12,
                  }}
                >
                  <Text strong>{EXPERIMENTAL_ACK_PHRASE}</Text>
                </Paragraph>
                <Input.TextArea
                  value={ack}
                  autoSize={{ minRows: 2, maxRows: 4 }}
                  placeholder={t('在这里逐字输入确认语')}
                  status={
                    ack && ack.trim() !== EXPERIMENTAL_ACK_PHRASE
                      ? 'error'
                      : undefined
                  }
                  onPaste={(event) => event.preventDefault()}
                  onDrop={(event) => event.preventDefault()}
                  onChange={(event) => setAck(event.target.value)}
                />
              </>
            ) : null}

            {installError ? (
              <Alert type="error" showIcon message={installError} />
            ) : null}

            <Space wrap>
              <Button
                type="primary"
                danger={isExperimental}
                loading={installing}
                disabled={!canInstall}
                onClick={() => void handleInstall()}
              >
                {installing ? t('正在安装…') : t('安装')}
              </Button>
              {!effectiveToken ? (
                <Text type="secondary">
                  {t('缺少安装令牌，请回到扩展模块页重新选择扩展包。')}
                </Text>
              ) : null}
            </Space>
          </Space>
        )}
      </Card>
    </div>
  );
};

export default ModulePreviewPage;
