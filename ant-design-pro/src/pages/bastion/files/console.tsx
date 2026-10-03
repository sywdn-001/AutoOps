/**
 * 文件管理器控制台（**`window.open()` 弹出的独立窗口**，路由 `/files/console`，`layout: false`）。
 *
 * 形态与终端窗口一致：终端工具条上点「文件管理」→ 弹出本窗口 → 本窗口自己去后端开一条
 * SFTP 会话（一个窗口一条会话，会话结束即关窗）。窗口里能做的事分两类：
 *
 * 1. **可视化操作**：进入目录、上传、下载、打包下载、新建目录/文件、编辑保存、重命名、
 *    移动、复制、改权限、删除；
 * 2. **访问控制可见化**：每次选中路径都会向后端 `/check` 试算一次「这个操作能不能作用在这个
 *    路径上」，按钮据此置灰并把后端给的拒绝原因显示出来——前端**不复刻**策略逻辑，
 *    真正拦不拦由后端说了算（每个操作无论放行还是拦截都会落一条文件审计）。
 */
import {
  ArrowUpOutlined,
  CloseOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  FileAddOutlined,
  FileOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  HomeOutlined,
  KeyOutlined,
  ReloadOutlined,
  SwapOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { history, useAccess, useSearchParams } from '@umijs/max';
import {
  Button,
  Drawer,
  Form,
  Input,
  message,
  Modal,
  Result,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Upload,
  type UploadProps,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { fileApi } from '@/services/bastion/endpoints';
import { formatDateTime } from '@/services/bastion/constants';
import type {
  FileCapabilities,
  FileDecision,
  FileEntry,
  FileListResult,
} from '@/services/bastion/types';
import './files.css';

/** 关闭文件管理器后本窗口自动关闭的倒计时秒数（与终端窗口同一口径）。 */
const CLOSE_COUNTDOWN_SECONDS = 10;

/** 选中项变化时要向后端试算的操作。前端只做展示，真正的拦截在后端。 */
const SINGLE_TARGET_OPS = [
  'download',
  'read',
  'write',
  'rename',
  'move',
  'copy',
  'chmod',
  'delete',
  'archive',
];
const MULTI_TARGET_OPS = ['delete', 'archive'];

const formatSize = (size: number): string => {
  if (!size) {
    return '0 B';
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = size;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${index === 0 ? value : value.toFixed(1)} ${units[index]}`;
};

const parentOf = (path: string): string => {
  const normalized = path.replace(/\/+$/, '');
  if (!normalized || normalized === '/') {
    return '/';
  }
  const cut = normalized.slice(0, normalized.lastIndexOf('/'));
  return cut || '/';
};

/** 面包屑：`/data/backup` → [{name:'/',path:'/'},{name:'data',path:'/data'},…] */
const crumbs = (path: string): { name: string; path: string }[] => {
  const items: { name: string; path: string }[] = [{ name: '/', path: '/' }];
  let acc = '';
  for (const part of path.split('/').filter(Boolean)) {
    acc += `/${part}`;
    items.push({ name: part, path: acc });
  }
  return items;
};

/** 回到资产列表：能关本窗口就关，关不掉就把本窗口变成列表页（与终端窗口同逻辑）。 */
const backToLauncher = () => {
  const opener = window.opener as Window | null;
  if (opener && !opener.closed) {
    try {
      opener.focus();
      window.close();
      window.setTimeout(() => window.location.assign('/terminal'), 200);
      return;
    } catch {
      // 跨窗口访问被拒，走本窗口跳转
    }
  }
  window.location.assign('/terminal');
};

type PendingModal = {
  kind: 'mkdir' | 'newfile' | 'rename' | 'move' | 'copy' | 'chmod';
  entry?: FileEntry;
};

const FileConsolePage = () => {
  const access = useAccess();
  const [params] = useSearchParams();
  const hostId = Number(params.get('hostId') ?? '');
  const accountId = Number(params.get('accountId') ?? '');
  const urlTitle = params.get('title') ?? '';

  const [caps, setCaps] = useState<FileCapabilities>();
  const [preparing, setPreparing] = useState(true);
  const [error, setError] = useState<string>();
  const [fullscreen, setFullscreen] = useState(false);

  const [path, setPath] = useState('/');
  const [listing, setListing] = useState<FileListResult>();
  const [loadingList, setLoadingList] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [decisions, setDecisions] = useState<Record<string, FileDecision>>({});
  const pending = useRef(new Set<string>());

  const [pendingModal, setPendingModal] = useState<PendingModal>();
  const [form] = Form.useForm<{ value?: string }>();
  const [submitting, setSubmitting] = useState(false);

  const [editor, setEditor] = useState<{ path: string; content: string; mtime: number }>();
  const [editorLoading, setEditorLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const [closing, setClosing] = useState<number>();
  const openedOnce = useRef(false);

  const sid = caps?.sid;

  /** 会话建立：本窗口自己开一条 SFTP 会话（URL 参数不作数，后端会重新做准入校验）。 */
  useEffect(() => {
    if (openedOnce.current) {
      return;
    }
    openedOnce.current = true;
    if (!hostId) {
      setError('缺少主机参数，请从资产列表重新打开文件管理器。');
      setPreparing(false);
      return;
    }
    fileApi
      .open({ hostId, accountId: accountId || undefined })
      .then((data) => {
        setCaps(data);
        setPath(data.homeDir || '/');
      })
      .catch((err: Error) => setError(err?.message || '打开文件管理器失败'))
      .finally(() => setPreparing(false));
  }, [hostId, accountId]);

  // 窗口卸载（用户直接关标签页）时收口会话：后端会在会话记录里落一条关闭事件。
  useEffect(() => {
    return () => {
      if (sid) {
        void fileApi.close(sid).catch(() => undefined);
      }
    };
  }, [sid]);

  const loadList = useCallback(
    async (target: string) => {
      if (!sid) {
        return;
      }
      setLoadingList(true);
      try {
        const data = await fileApi.list(sid, target);
        setListing(data);
        setSelected([]);
      } catch (err) {
        message.error((err as Error)?.message || '读取目录失败');
        // 目录读不了（被策略拦或不存在）时退回上一级，避免卡在空页面
        const parent = parentOf(target);
        if (parent !== target) {
          setPath(parent);
        }
      } finally {
        setLoadingList(false);
      }
    },
    [sid],
  );

  useEffect(() => {
    if (sid) {
      void loadList(path);
    }
  }, [sid, path, loadList]);

  const entries = useMemo(() => listing?.entries ?? [], [listing]);
  const selectedEntries = useMemo(
    () => entries.filter((entry) => selected.includes(entry.path)),
    [entries, selected],
  );
  const decisionKey = (operation: string, target: string) => `${operation}|${target}`;

  const decisionFor = useCallback(
    (operation: string, target?: string): FileDecision | undefined => {
      if (!target) {
        return undefined;
      }
      return decisions[decisionKey(operation, target)];
    },
    [decisions],
  );

  /** 向后端试算「某操作能否作用在这些路径上」，结果只用于置灰与提示原因。 */
  const runChecks = useCallback(
    (operations: string[], target: string) => {
      if (!sid || !target) {
        return;
      }
      const missing = operations.filter((operation) => {
        const key = decisionKey(operation, target);
        return !pending.current.has(key) && !decisions[key];
      });
      if (!missing.length) {
        return;
      }
      for (const operation of missing) {
        pending.current.add(decisionKey(operation, target));
      }
      void Promise.all(
        missing.map((operation) =>
          fileApi
            .check(sid, { operation, path: target })
            .then((decision) => [operation, decision] as const)
            .catch(() => undefined),
        ),
      ).then((results) => {
        setDecisions((prev) => {
          const next = { ...prev };
          for (const item of results) {
            if (item) {
              next[decisionKey(item[0], target)] = item[1];
            }
          }
          return next;
        });
      });
    },
    [sid, decisions],
  );

  // 目录级操作：上传 / 新建目录 / 新建文件
  useEffect(() => {
    if (sid) {
      runChecks(['upload', 'mkdir', 'write'], path);
    }
  }, [sid, path, runChecks]);

  // 选中项操作：单选用全套，多选只用批量能做的（删除 / 打包）
  useEffect(() => {
    if (!sid || !selectedEntries.length) {
      return;
    }
    const single = selectedEntries.length === 1 ? selectedEntries[0] : undefined;
    if (single) {
      runChecks(SINGLE_TARGET_OPS, single.path);
    } else {
      for (const entry of selectedEntries) {
        runChecks(MULTI_TARGET_OPS, entry.path);
      }
    }
  }, [sid, selectedEntries, runChecks]);

  const denied = (operation: string, target?: string): string | undefined => {
    const decision = decisionFor(operation, target);
    if (decision && decision.allowed === false) {
      return decision.reason || '该操作被文件策略拦截';
    }
    return undefined;
  };

  const toggleFullscreen = () => {
    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(() => undefined);
      return;
    }
    const root = document.documentElement;
    if (typeof root.requestFullscreen === 'function') {
      void root.requestFullscreen().catch(() => setFullscreen((prev) => !prev));
      return;
    }
    setFullscreen((prev) => !prev);
  };

  const refresh = useCallback(() => {
    if (sid) {
      setDecisions({});
      pending.current.clear();
      void loadList(path);
    }
  }, [sid, path, loadList]);

  const openEditor = useCallback(
    async (entry: FileEntry) => {
      if (!sid) {
        return;
      }
      setEditorLoading(true);
      try {
        const data = await fileApi.read(sid, entry.path);
        if (data.truncated) {
          message.warning(`文件较大，仅载入前 ${formatSize(data.readBytes)}`);
        }
        setEditor({ path: data.path, content: data.content, mtime: data.mtime });
      } catch (err) {
        message.error((err as Error)?.message || '读取文件失败');
      } finally {
        setEditorLoading(false);
      }
    },
    [sid],
  );

  const saveEditor = useCallback(async () => {
    if (!sid || !editor) {
      return;
    }
    setSaving(true);
    try {
      await fileApi.write(sid, {
        path: editor.path,
        content: editor.content,
        expectedMtime: editor.mtime || undefined,
      });
      message.success('已保存');
      setEditor(undefined);
      void loadList(path);
    } catch (err) {
      message.error((err as Error)?.message || '保存失败');
    } finally {
      setSaving(false);
    }
  }, [sid, editor, path, loadList]);

  const doDownload = useCallback(
    async (target: string) => {
      if (!sid) {
        return;
      }
      try {
        await fileApi.download(sid, target);
      } catch (err) {
        message.error((err as Error)?.message || '下载失败');
      }
    },
    [sid],
  );

  const doArchive = useCallback(
    async (paths: string[]) => {
      if (!sid || !paths.length) {
        return;
      }
      try {
        await fileApi.archive(sid, paths);
      } catch (err) {
        message.error((err as Error)?.message || '打包下载失败');
      }
    },
    [sid],
  );

  const doDelete = useCallback(
    (targets: FileEntry[]) => {
      if (!sid || !targets.length) {
        return;
      }
      Modal.confirm({
        title: `删除 ${targets.length} 项？`,
        content: (
          <div>
            <div>删除后无法恢复，操作会记入文件审计：</div>
            <ul style={{ paddingInlineStart: 18, marginTop: 8 }}>
              {targets.slice(0, 8).map((entry) => (
                <li key={entry.path}>{entry.path}</li>
              ))}
            </ul>
            {targets.length > 8 ? <div>…等 {targets.length} 项</div> : null}
          </div>
        ),
        okText: '删除',
        okType: 'danger',
        cancelText: '取消',
        onOk: async () => {
          try {
            const result = await fileApi.remove(sid, {
              paths: targets.map((entry) => entry.path),
              recursive: true,
            });
            if (result.failed?.length) {
              message.warning(
                `已删除 ${result.deleted.length} 项，${result.failed.length} 项失败：${result.failed[0]?.message ?? ''}`,
              );
            } else {
              message.success(`已删除 ${result.deleted.length} 项`);
            }
            refresh();
          } catch (err) {
            message.error((err as Error)?.message || '删除失败');
          }
        },
      });
    },
    [sid, refresh],
  );

  const openModal = (next: PendingModal) => {
    setPendingModal(next);
    if (next.kind === 'rename' && next.entry) {
      form.setFieldsValue({ value: next.entry.name });
    } else if (next.kind === 'chmod' && next.entry) {
      form.setFieldsValue({ value: next.entry.modeOctal.replace(/^0+/, '') || '644' });
    } else if (next.kind === 'copy' || next.kind === 'move') {
      form.setFieldsValue({ value: `${path === '/' ? '' : path}/${next.entry?.name ?? ''}` });
    } else {
      form.setFieldsValue({ value: '' });
    }
  };

  const submitModal = async () => {
    if (!sid || !pendingModal) {
      return;
    }
    const values = await form.validateFields();
    const raw = (values.value ?? '').trim();
    const absolute = (value: string) => (value.startsWith('/') ? value : `${path === '/' ? '' : path}/${value}`);
    setSubmitting(true);
    try {
      if (pendingModal.kind === 'mkdir') {
        await fileApi.mkdir(sid, { path: absolute(raw), parents: true });
        message.success('目录已创建');
      } else if (pendingModal.kind === 'newfile') {
        await fileApi.write(sid, { path: absolute(raw), content: '' });
        message.success('文件已创建');
      } else if (pendingModal.kind === 'rename' && pendingModal.entry) {
        await fileApi.rename(sid, { path: pendingModal.entry.path, newName: raw });
        message.success('已重命名');
      } else if (pendingModal.kind === 'move' && pendingModal.entry) {
        await fileApi.rename(sid, { path: pendingModal.entry.path, targetPath: raw });
        message.success('已移动');
      } else if (pendingModal.kind === 'copy' && pendingModal.entry) {
        await fileApi.copy(sid, { path: pendingModal.entry.path, targetPath: raw });
        message.success('已复制');
      } else if (pendingModal.kind === 'chmod' && pendingModal.entry) {
        await fileApi.chmod(sid, { path: pendingModal.entry.path, mode: raw });
        message.success('权限已更新');
      }
      setPendingModal(undefined);
      form.resetFields();
      refresh();
    } catch (err) {
      message.error((err as Error)?.message || '操作失败');
    } finally {
      setSubmitting(false);
    }
  };

  const uploadProps = useMemo<UploadProps>(
    () => ({
      multiple: true,
      showUploadList: false,
      customRequest: (options) => {
        if (!sid) {
          options.onError?.(new Error('会话未就绪'));
          return;
        }
        const file = options.file as File;
        fileApi
          .upload(sid, path, file)
          .then((result) => {
            const failed = result.failed ?? [];
            if (failed.length) {
              message.warning(`${failed[0]?.name}：${failed[0]?.message}`);
            } else {
              message.success(`已上传 ${result.uploaded.length} 个文件`);
            }
            options.onSuccess?.(result);
            refresh();
          })
          .catch((err: Error) => {
            message.error(err?.message || '上传失败');
            options.onError?.(err);
          });
      },
    }),
    [sid, path, refresh],
  );

  const closeSession = useCallback(async () => {
    if (!sid) {
      window.close();
      return;
    }
    try {
      await fileApi.close(sid);
    } catch {
      // 会话可能已被管理员断开，继续走关窗流程
    }
    setClosing(CLOSE_COUNTDOWN_SECONDS);
  }, [sid]);

  useEffect(() => {
    if (closing === undefined) {
      return;
    }
    if (closing <= 0) {
      window.close();
      // 浏览器拒绝脚本关窗时（手工打开的本页）兜底回到资产列表，不留一个关不掉的窗口
      window.setTimeout(() => window.location.assign('/terminal'), 300);
      return;
    }
    const timer = window.setTimeout(() => setClosing(closing - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [closing]);

  if (!access.canFileUse) {
    return (
      <div className="bastion-files-page bastion-files-centered">
        <Result
          status="403"
          title="没有文件管理器权限"
          subTitle="当前账号缺少 file:use 权限，无法打开文件管理器。请联系管理员在角色里勾选「文件管理器」。"
          extra={
            <Space>
              <Button type="primary" onClick={() => history.push('/dashboard')}>
                返回主页
              </Button>
              <Button onClick={() => window.close()}>关闭本窗口</Button>
            </Space>
          }
        />
      </div>
    );
  }

  if (preparing) {
    return (
      <div className="bastion-files-page bastion-files-centered">
        <Space direction="vertical" align="center">
          <Spin size="large" />
          <span>正在建立 SFTP 会话…</span>
        </Space>
      </div>
    );
  }

  if (error || !caps) {
    return (
      <div className="bastion-files-page bastion-files-centered">
        <Result
          status="warning"
          title="无法打开文件管理器"
          subTitle={error || '会话信息缺失'}
          extra={
            <Space>
              <Button type="primary" onClick={backToLauncher}>
                返回资产列表
              </Button>
              <Button onClick={() => window.close()}>关闭本窗口</Button>
            </Space>
          }
        />
      </div>
    );
  }

  if (closing !== undefined && closing > 0) {
    return (
      <div className="bastion-files-closing">
        <Result
          status="success"
          title="文件管理器已关闭"
          subTitle={`本窗口将在 ${closing} 秒后自动关闭，文件操作记录已进入审计中心。`}
          extra={
            <Space>
              <Button onClick={() => setClosing(undefined)}>保留本窗口</Button>
              <Button type="primary" onClick={() => window.close()}>
                立即关闭
              </Button>
            </Space>
          }
        />
      </div>
    );
  }

  const columns = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (_: unknown, entry: FileEntry) => (
        <span className="bastion-files-name">
          {entry.isDir ? <FolderOutlined /> : <FileOutlined />}
          {entry.isDir ? (
            <span className="bastion-files-name-dir">
              <button type="button" onClick={() => setPath(entry.path)}>
                {entry.name}
              </button>
            </span>
          ) : (
            <span>{entry.name}</span>
          )}
        </span>
      ),
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 100,
      render: (_: unknown, entry: FileEntry) =>
        entry.isDir ? <span className="bastion-files-muted">—</span> : formatSize(entry.size),
    },
    {
      title: '修改时间',
      dataIndex: 'mtime',
      key: 'mtime',
      width: 150,
      render: (_: unknown, entry: FileEntry) => formatDateTime(entry.mtime),
    },
    {
      title: '权限',
      dataIndex: 'modeOctal',
      key: 'mode',
      width: 90,
      render: (_: unknown, entry: FileEntry) => <code>{entry.modeOctal}</code>,
    },
    {
      title: '属主',
      key: 'owner',
      width: 90,
      render: (_: unknown, entry: FileEntry) => (
        <span className="bastion-files-muted">
          {entry.uid}:{entry.gid}
        </span>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 330,
      render: (_: unknown, entry: FileEntry) => {
        const locked = (operation: string) => denied(operation, entry.path);
        return (
          <Space size={2} wrap>
            {!entry.isDir ? (
              <>
                <Tooltip title={locked('download') || '下载到本地'}>
                  <Button
                    size="small"
                    type="link"
                    icon={<DownloadOutlined />}
                    disabled={Boolean(locked('download'))}
                    onClick={() => void doDownload(entry.path)}
                  >
                    下载
                  </Button>
                </Tooltip>
                <Tooltip title={locked('read') || locked('write') || '在线编辑并保存'}>
                  <Button
                    size="small"
                    type="link"
                    icon={<EditOutlined />}
                    disabled={Boolean(locked('read') || locked('write'))}
                    onClick={() => void openEditor(entry)}
                  >
                    编辑
                  </Button>
                </Tooltip>
              </>
            ) : (
              <Button size="small" type="link" onClick={() => setPath(entry.path)}>
                打开
              </Button>
            )}
            <Tooltip title={locked('rename') || '重命名'}>
              <Button
                size="small"
                type="link"
                disabled={Boolean(locked('rename'))}
                onClick={() => openModal({ kind: 'rename', entry })}
              >
                重命名
              </Button>
            </Tooltip>
            <Tooltip title={locked('move') || '移动到其它目录'}>
              <Button
                size="small"
                type="link"
                icon={<SwapOutlined />}
                disabled={Boolean(locked('move'))}
                onClick={() => openModal({ kind: 'move', entry })}
              >
                移动
              </Button>
            </Tooltip>
            <Tooltip title={locked('copy') || '复制到其它目录'}>
              <Button
                size="small"
                type="link"
                icon={<CopyOutlined />}
                disabled={Boolean(locked('copy'))}
                onClick={() => openModal({ kind: 'copy', entry })}
              >
                复制
              </Button>
            </Tooltip>
            <Tooltip title={locked('chmod') || '修改权限'}>
              <Button
                size="small"
                type="link"
                icon={<KeyOutlined />}
                disabled={Boolean(locked('chmod'))}
                onClick={() => openModal({ kind: 'chmod', entry })}
              >
                权限
              </Button>
            </Tooltip>
            <Tooltip title={locked('delete') || '删除'}>
              <Button
                size="small"
                type="link"
                danger
                icon={<DeleteOutlined />}
                disabled={Boolean(locked('delete'))}
                onClick={() => doDelete([entry])}
              >
                删除
              </Button>
            </Tooltip>
            <Tooltip title={locked('archive') || '打包为 zip 下载'}>
              <Button
                size="small"
                type="link"
                disabled={Boolean(locked('archive'))}
                onClick={() => void doArchive([entry.path])}
              >
                打包
              </Button>
            </Tooltip>
          </Space>
        );
      },
    },
  ];

  const uploadDenied = denied('upload', path);
  const mkdirDenied = denied('mkdir', path);
  const writeDenied = denied('write', path);
  const cursor = selectedEntries.length === 0 ? path : selectedEntries[0]?.path;

  return (
    <div className={`bastion-files-page${fullscreen ? ' bastion-console-fullscreen' : ''}`}>
      <div className="bastion-files-toolbar">
        <Tooltip title="回到资产列表（关闭本窗口）">
          <Button
            size="small"
            type="text"
            icon={<HomeOutlined />}
            onClick={backToLauncher}
          >
            资产列表
          </Button>
        </Tooltip>
        <span className="bastion-files-title" title={caps.hostName}>
          {caps.hostName || urlTitle || 'SFTP'}
        </span>
        <Tag color="success">已连接</Tag>
        {caps.accountUsername ? (
          <span className="bastion-files-chip">账号 {caps.accountUsername}</span>
        ) : null}
        <span className="bastion-files-chip" title="文件策略">
          策略 {caps.policyName || '默认文件策略'}
        </span>
        <span className="bastion-files-chip" title="会话号">
          {caps.sid}
        </span>
        <span className="bastion-files-spacer" />
        <Tooltip title={uploadDenied || '上传文件到当前目录'}>
          <span>
            <Upload {...uploadProps} disabled={Boolean(uploadDenied)}>
              <Button
                size="small"
                type="text"
                icon={<UploadOutlined />}
                disabled={Boolean(uploadDenied)}
              >
                上传
              </Button>
            </Upload>
          </span>
        </Tooltip>
        <Tooltip title={mkdirDenied || '在当前目录新建文件夹'}>
          <Button
            size="small"
            type="text"
            icon={<FolderAddOutlined />}
            disabled={Boolean(mkdirDenied)}
            onClick={() => openModal({ kind: 'mkdir' })}
          >
            新建目录
          </Button>
        </Tooltip>
        <Tooltip title={writeDenied || '在当前目录新建空文件'}>
          <Button
            size="small"
            type="text"
            icon={<FileAddOutlined />}
            disabled={Boolean(writeDenied)}
            onClick={() => openModal({ kind: 'newfile' })}
          >
            新建文件
          </Button>
        </Tooltip>
        <Tooltip title="刷新当前目录">
          <Button
            size="small"
            type="text"
            icon={<ReloadOutlined />}
            onClick={refresh}
          />
        </Tooltip>
        <Tooltip title={fullscreen ? '退出全屏' : '全屏'}>
          <Button
            size="small"
            type="text"
            icon={fullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
            onClick={toggleFullscreen}
          />
        </Tooltip>
        <Tooltip title="关闭文件管理器（结束 SFTP 会话）">
          <Button
            size="small"
            type="text"
            danger
            icon={<CloseOutlined />}
            onClick={() => void closeSession()}
          >
            关闭
          </Button>
        </Tooltip>
      </div>

      <div className="bastion-files-pathbar">
        <Tooltip title="上级目录">
          <Button
            size="small"
            type="text"
            icon={<ArrowUpOutlined />}
            disabled={path === '/'}
            onClick={() => setPath(parentOf(path))}
          />
        </Tooltip>
        <span className="bastion-files-crumb">
          {crumbs(path).map((item, index, all) => (
            <span key={item.path}>
              <button type="button" onClick={() => setPath(item.path)}>
                {item.name}
              </button>
              {index < all.length - 1 ? <span className="bastion-files-muted">/</span> : null}
            </span>
          ))}
        </span>
        <Input
          size="small"
          style={{ width: 320 }}
          prefix={<FolderOpenOutlined />}
          value={path}
          onChange={(event) => setPath(event.target.value || '/')}
          onPressEnter={() => refresh()}
        />
      </div>

      {selectedEntries.length ? (
        <div className="bastion-files-selection">
          <span>已选 {selectedEntries.length} 项</span>
          {selectedEntries.length === 1 ? (
            <span className="bastion-files-muted">{selectedEntries[0]?.path}</span>
          ) : null}
          <span className="bastion-files-spacer" />
          <Tooltip title={denied('download', cursor) || '下载选中文件'}>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              disabled={selectedEntries.length !== 1 || Boolean(denied('download', cursor))}
              onClick={() => cursor && void doDownload(cursor)}
            >
              下载
            </Button>
          </Tooltip>
          <Tooltip title={denied('archive', cursor) || '打包为 zip 下载'}>
            <Button
              size="small"
              disabled={Boolean(denied('archive', cursor))}
              onClick={() => void doArchive(selectedEntries.map((entry) => entry.path))}
            >
              打包下载
            </Button>
          </Tooltip>
          <Tooltip title={denied('delete', cursor) || '删除选中项'}>
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={Boolean(denied('delete', cursor))}
              onClick={() => doDelete(selectedEntries)}
            >
              删除
            </Button>
          </Tooltip>
          <Button size="small" type="text" onClick={() => setSelected([])}>
            取消选择
          </Button>
        </div>
      ) : null}

      <div className="bastion-files-body">
        <Table<FileEntry>
          rowKey="path"
          size="small"
          columns={columns}
          dataSource={entries}
          loading={loadingList}
          pagination={false}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys) => setSelected(keys as string[]),
          }}
          scroll={{ y: 'calc(100vh - 190px)' }}
          locale={{ emptyText: listing?.truncated ? '目录内容过多，仅显示前部分' : '空目录' }}
        />
      </div>

      <div className="bastion-files-statusbar">
        <span>
          <span className="bastion-files-dot" />
          已连接 {caps.hostAddress}
        </span>
        <span>共 {listing?.count ?? 0} 项</span>
        {listing?.truncated ? <span className="bastion-files-denied">列表已截断</span> : null}
        <span className="bastion-files-statusbar-right">
          文件策略 {caps.policyName || '默认文件策略'} · 每个操作都会记入文件审计
        </span>
      </div>

      <Modal
        open={Boolean(pendingModal)}
        title={{
          mkdir: '新建目录',
          newfile: '新建文件',
          rename: '重命名',
          move: '移动到',
          copy: '复制到',
          chmod: '修改权限',
        }[pendingModal?.kind ?? 'mkdir']}
        okText="确定"
        cancelText="取消"
        confirmLoading={submitting}
        onOk={() => void submitModal()}
        onCancel={() => {
          setPendingModal(undefined);
          form.resetFields();
        }}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="value"
            label={
              pendingModal?.kind === 'mkdir'
                ? '目录名或完整路径'
                : pendingModal?.kind === 'newfile'
                  ? '文件名或完整路径'
                  : pendingModal?.kind === 'rename'
                    ? '新名称'
                    : pendingModal?.kind === 'chmod'
                      ? '八进制权限（例如 644 / 0755）'
                      : '目标完整路径'
            }
            rules={[{ required: true, message: '请填写内容' }]}
          >
            <Input placeholder={pendingModal?.kind === 'chmod' ? '644' : '/data/backup'} />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        open={Boolean(editor) || editorLoading}
        title={editor ? `编辑 ${editor.path}` : '读取文件…'}
        width={760}
        onClose={() => setEditor(undefined)}
        extra={
          <Space>
            <Button
              type="primary"
              loading={saving}
              disabled={Boolean(denied('write', editor?.path))}
              onClick={() => void saveEditor()}
            >
              保存
            </Button>
            <Button onClick={() => setEditor(undefined)}>取消</Button>
          </Space>
        }
      >
        {editorLoading ? (
          <Spin />
        ) : (
          <Input.TextArea
            className="bastion-files-editor"
            value={editor?.content ?? ''}
            autoSize={{ minRows: 24, maxRows: 40 }}
            onChange={(event) =>
              setEditor((prev) => (prev ? { ...prev, content: event.target.value } : prev))
            }
          />
        )}
      </Drawer>
    </div>
  );
};

export default FileConsolePage;
