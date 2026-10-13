/**
 * ComputerUse 部署向导纯逻辑的测试：阶段↔百分比、阶段列表状态、部署参数组装、
 * 随机密钥、日志分级着色、结果摘要。
 *
 * 为什么值得单独测：这些值直接决定「阶段进度跟后端 `deploy.py::STAGES` 对不上」
 * 「参数拼错导致后端 400」「日志配色错」这三类线上问题，而它们全是不碰 DOM、
 * 不碰接口的纯函数，用 vitest 跑最便宜。接口层用造出来的 `Deployment` 数据喂。
 */
import { describe, expect, it } from 'vitest';
import {
  type Deployment,
  type PythonVersionItem,
  STAGE_SEQUENCE,
} from '@/services/bastion/computeruse';
import {
  backendMessage,
  buildDeployPayload,
  buildDeployResult,
  buildStages,
  canDeploy,
  defaultVersionItem,
  deployEntryFromDeployment,
  deployStreamReducer,
  detectLogLevel,
  displayProgress,
  formatLogEntry,
  formatLogLine,
  freshOptions,
  generateAuthKey,
  humanSize,
  initialStreamState,
  isHostReady,
  localSizeOf,
  logLineColor,
  optionsSummary,
  parseTailLines,
  stageIndex,
  stageLabel,
  stageProgress,
  stepStatus,
  stripLogDecorations,
  versionAtLeast,
} from './helpers';
import { DEFAULT_DEPLOY_OPTIONS, type DeployOptionDraft } from './types';

/** 造一条完整的部署记录（后端 `to_dict()` 的字段一个不少，避免测试靠 as 硬转） */
function makeDeployment(overrides: Partial<Deployment> = {}): Deployment {
  return {
    id: 7,
    hostId: 3,
    hostName: 'win-01',
    hostAddress: '10.0.0.31',
    accountId: 11,
    accountUsername: 'Administrator',
    status: 'running',
    stage: 'prepare',
    stageLabel: '',
    progress: 0,
    pythonVersion: '3.13.1',
    installerName: 'python-3.13.1-amd64.exe',
    installerSize: 28 * 1024 * 1024,
    installerSha256: 'a'.repeat(64),
    pythonPath: '',
    mcpVersion: '',
    mcpExe: '',
    mcpHost: '',
    mcpPort: 8000,
    mcpUrl: '',
    mcpToolCount: 0,
    mcpTools: [],
    hasAuthKey: true,
    vbsPath: '',
    startupPath: '',
    portproxy: false,
    options: {},
    bastionUrl: '',
    deployedBy: 'admin',
    logTail: '',
    error: '',
    startedAt: null,
    finishedAt: null,
    createdAt: null,
    updatedAt: null,
    ...overrides,
  };
}

function draft(overrides: Partial<DeployOptionDraft> = {}): DeployOptionDraft {
  return { ...DEFAULT_DEPLOY_OPTIONS, ...overrides };
}

/** 造一条 `/versions` 清单项（`mcpCompatible` 显式给，别靠 as 硬转掩盖漏字段） */
function makeVersionItem(
  overrides: Partial<PythonVersionItem> = {},
): PythonVersionItem {
  return {
    version: '3.13.1',
    filename: 'python-3.13.1-amd64.exe',
    url: 'https://mirrors.example/python-3.13.1-amd64.exe',
    size: 28 * 1024 * 1024,
    released: '2024-12-03',
    mcpCompatible: true,
    cached: false,
    cachedAt: null,
    sha256: '',
    localSize: 0,
    ...overrides,
  };
}

describe('阶段与百分比', () => {
  it('七个阶段的完成百分比与后端 STAGES 一致', () => {
    expect(STAGE_SEQUENCE.map((item) => item.key)).toEqual([
      'prepare',
      'upload',
      'install',
      'pip',
      'vbs',
      'expose',
      'verify',
    ]);
    expect(STAGE_SEQUENCE.map((item) => item.progress)).toEqual([
      6, 28, 48, 68, 82, 92, 100,
    ]);
  });

  it('stageProgress 认全部七个阶段，未知阶段（如 queued）给 0', () => {
    for (const item of STAGE_SEQUENCE) {
      expect(stageProgress(item.key)).toBe(item.progress);
    }
    expect(stageProgress('queued')).toBe(0);
    expect(stageProgress(undefined)).toBe(0);
    expect(stageProgress(null)).toBe(0);
  });

  it('stageIndex / stageLabel 对未知阶段不抛异常', () => {
    expect(stageIndex('verify')).toBe(6);
    expect(stageIndex('queued')).toBe(-1);
    expect(stageIndex(null)).toBe(-1);
    expect(stageLabel('pip')).toBe('安装 windows-mcp');
    // 后端给了没收录的阶段名时，至少把它原样显示出来
    expect(stageLabel('unknown-stage')).toBe('unknown-stage');
    expect(stageLabel(null)).toBe('');
  });

  it('displayProgress 优先用后端数值，0 与越界按阶段锚点兜底', () => {
    expect(displayProgress(42, 'install')).toBe(42);
    expect(displayProgress(120, 'install')).toBe(100);
    expect(displayProgress(-5, 'install')).toBe(48);
    expect(displayProgress(47.6, 'install')).toBe(48);
    expect(displayProgress(undefined, 'pip')).toBe(68);
    expect(displayProgress(null, 'pip')).toBe(68);
    expect(displayProgress(Number.NaN, 'vbs')).toBe(82);
    // 刚进阶段时后端给 0，此时要按锚点显示，避免进度条看起来在退回
    expect(displayProgress(0, 'verify')).toBe(100);
    expect(displayProgress(0, 'queued')).toBe(0);
  });

  it('stepStatus：已过的阶段 finish、当前阶段 process、失败时当前阶段 error', () => {
    expect(stepStatus('install', 'running', 'prepare')).toBe('finish');
    expect(stepStatus('install', 'running', 'install')).toBe('process');
    expect(stepStatus('install', 'running', 'pip')).toBe('wait');
    expect(stepStatus('install', 'failed', 'install')).toBe('error');
    expect(stepStatus('install', 'cancelled', 'install')).toBe('wait');
    // 阶段名对不上时全部保持 wait，不瞎标
    expect(stepStatus('queued', 'running', 'install')).toBe('wait');
  });

  it('buildStages 优先用后端下发的阶段表，缺失时退回前端固定顺序', () => {
    const fromBackend = buildStages(
      [
        { key: 'prepare', label: '连接目标机', progress: 6 },
        { key: 'upload', label: '上传安装包', progress: 28 },
      ],
      'upload',
      'running',
    );
    expect(fromBackend.map((item) => item.label)).toEqual([
      '连接目标机',
      '上传安装包',
    ]);
    expect(fromBackend.map((item) => item.state)).toEqual([
      'finish',
      'process',
    ]);

    const fallback = buildStages(undefined, 'pip', 'running');
    expect(fallback).toHaveLength(7);
    expect(fallback[3]).toMatchObject({
      key: 'pip',
      state: 'process',
      progress: 68,
    });

    // 当前阶段不在列表里（部署取消后 stage 可能是 queued）时全部 wait
    const cancelled = buildStages(undefined, 'queued', 'cancelled');
    expect(cancelled.every((item) => item.state === 'wait')).toBe(true);
    // 失败时正好停在当前阶段上
    const failed = buildStages(undefined, 'expose', 'failed');
    expect(failed[5].state).toBe('error');
    expect(failed[6].state).toBe('wait');
  });
});

describe('部署参数组装', () => {
  const base = {
    hostId: 3,
    accountId: 11,
    pythonVersion: '3.13.1',
    installerName: 'python-3.13.1-amd64.exe',
  };

  it('canDeploy 要求主机与账号都选了、且有安装包', () => {
    expect(canDeploy({ ...base, options: draft() })).toBe(true);
    expect(canDeploy({ ...base, hostId: null, options: draft() })).toBe(false);
    expect(canDeploy({ ...base, hostId: 0, options: draft() })).toBe(false);
    expect(canDeploy({ ...base, accountId: null, options: draft() })).toBe(
      false,
    );
    expect(canDeploy({ ...base, accountId: 0, options: draft() })).toBe(false);
    expect(canDeploy({ ...base, installerName: '', options: draft() })).toBe(
      false,
    );
    expect(canDeploy({ ...base, pythonVersion: '', options: draft() })).toBe(
      false,
    );
  });

  it('默认选项：8000 端口 + 清华 pip 源 + 带访问密钥 + 不开 bindAll', () => {
    const payload = buildDeployPayload({
      ...base,
      options: draft(),
      authKey: 'K'.repeat(32),
    });
    expect(payload).toEqual({
      hostId: 3,
      accountId: 11,
      pythonVersion: '3.13.1',
      installerName: 'python-3.13.1-amd64.exe',
      options: {
        mcpPort: 8000,
        pipIndex: 'https://pypi.tuna.tsinghua.edu.cn/simple',
        bindAll: false,
        authKey: 'K'.repeat(32),
      },
    });
  });

  it('端口非法时退回 8000（含小数取整、字符串数字）', () => {
    expect(
      buildDeployPayload({ ...base, options: draft({ mcpPort: 0 }) }).options
        .mcpPort,
    ).toBe(8000);
    expect(
      buildDeployPayload({ ...base, options: draft({ mcpPort: 70000 }) })
        .options.mcpPort,
    ).toBe(8000);
    expect(
      buildDeployPayload({ ...base, options: draft({ mcpPort: 9000.6 }) })
        .options.mcpPort,
    ).toBe(9000);
    expect(
      buildDeployPayload({ ...base, options: draft({ mcpPort: 65535 }) })
        .options.mcpPort,
    ).toBe(65535);
  });

  it('pip 源清空/只有空白时不带该字段；有值时 trim', () => {
    const empty = buildDeployPayload({
      ...base,
      options: draft({ pipIndex: '   ' }),
    });
    expect('pipIndex' in empty.options).toBe(false);
    const spaced = buildDeployPayload({
      ...base,
      options: draft({ pipIndex: '  https://mirrors.example/simple  ' }),
    });
    expect(spaced.options.pipIndex).toBe('https://mirrors.example/simple');
  });

  it('关闭访问密钥时不带 authKey；开启但密钥为空时也不带', () => {
    expect(
      buildDeployPayload({
        ...base,
        options: draft({ useAuthKey: false }),
        authKey: 'K'.repeat(32),
      }).options.authKey,
    ).toBeUndefined();
    expect(
      buildDeployPayload({ ...base, options: draft(), authKey: '   ' }).options
        .authKey,
    ).toBeUndefined();
    // 用户粘进来的密钥带空白也要 trim
    expect(
      buildDeployPayload({ ...base, options: draft(), authKey: '  abc  ' })
        .options.authKey,
    ).toBe('abc');
  });

  it('bindAll 恒为布尔值，bastionUrl 空了就不带', () => {
    const bindAll = buildDeployPayload({
      ...base,
      options: draft({ bindAll: true }),
      bastionUrl: 'https://bastion.example.com',
    });
    expect(bindAll.options.bindAll).toBe(true);
    expect(bindAll.bastionUrl).toBe('https://bastion.example.com');
    const noUrl = buildDeployPayload({
      ...base,
      options: draft({ bindAll: true }),
      bastionUrl: '  ',
    });
    expect('bastionUrl' in noUrl).toBe(false);
  });

  it('hostId/accountId 统一转成数字，避免后端按字符串校验失败', () => {
    const payload = buildDeployPayload({
      hostId: Number('3'),
      accountId: Number('11'),
      pythonVersion: '3.13.1',
      installerName: 'python-3.13.1-amd64.exe',
      options: draft(),
    });
    expect(payload.hostId).toBe(3);
    expect(payload.accountId).toBe(11);
  });
});

describe('随机访问密钥与格式化', () => {
  it('generateAuthKey 默认 32 位，且只含安全字符集', () => {
    const key = generateAuthKey();
    expect(key).toHaveLength(32);
    expect(key).toMatch(/^[A-Za-z0-9]+$/);
    expect(generateAuthKey(16)).toHaveLength(16);
    // getRandomValues 的行为可控：全 0 → 全表首字符 'A'
    const zeroed = generateAuthKey(8, {
      getRandomValues: (array) => {
        // 全填 0 → 每个字节都映射到字符表第一位 'A'
        (array as unknown as Uint8Array).fill(0);
        return array;
      },
    });
    expect(zeroed).toBe('AAAAAAAA');
  });

  it('没有 crypto（非安全上下文）时退回 Math.random，仍然给得出密钥', () => {
    const key = generateAuthKey(32, undefined);
    expect(key).toHaveLength(32);
    expect(key).toMatch(/^[A-Za-z0-9]+$/);
  });

  it('两次生成的密钥不同（不能是常量）', () => {
    const set = new Set([
      generateAuthKey(),
      generateAuthKey(),
      generateAuthKey(),
    ]);
    expect(set.size).toBeGreaterThan(1);
  });

  it('humanSize 按 B/KB/MB/GB 分档', () => {
    expect(humanSize(0)).toBe('0 B');
    expect(humanSize(1023)).toBe('1023 B');
    expect(humanSize(1024)).toBe('1.0 KB');
    expect(humanSize(28 * 1024 * 1024)).toBe('28.00 MB');
    expect(humanSize(3 * 1024 * 1024 * 1024)).toBe('3.00 GB');
    expect(humanSize(undefined)).toBe('0 B');
    expect(humanSize(null)).toBe('0 B');
    expect(humanSize(-100)).toBe('0 B');
  });

  it('versionAtLeast 按数字段比较，3.13 与 3.13.0 等价', () => {
    expect(versionAtLeast('3.13', '3.13')).toBe(true);
    expect(versionAtLeast('3.13.0', '3.13')).toBe(true);
    expect(versionAtLeast('3.13.1', '3.13')).toBe(true);
    expect(versionAtLeast('3.14', '3.13')).toBe(true);
    expect(versionAtLeast('3.12.9', '3.13')).toBe(false);
    expect(versionAtLeast('2.7', '3.13')).toBe(false);
    expect(versionAtLeast('', '3.13')).toBe(false);
  });

  it('localSizeOf 只在缓存里找得到时才给字节数', () => {
    const cached = [
      {
        filename: 'python-3.13.1-amd64.exe',
        version: '3.13.1',
        size: 1024,
        sha256: 'b'.repeat(64),
        cachedAt: null,
        path: 'C:\\ComputerUse\\installers\\python-3.13.1-amd64.exe',
      },
    ];
    expect(localSizeOf('python-3.13.1-amd64.exe', cached)).toBe(1024);
    expect(localSizeOf('python-3.12.9-amd64.exe', cached)).toBe(0);
    expect(localSizeOf('python-3.13.1-amd64.exe', [])).toBe(0);
  });

  it('backendMessage 优先后端信封里的中文 message', () => {
    expect(
      backendMessage(
        { response: { data: { message: '请选择要部署的目标主机' } } },
        '兜底文案',
      ),
    ).toBe('请选择要部署的目标主机');
    expect(
      backendMessage({ response: { data: { message: '   ' } } }, '兜底文案'),
    ).toBe('兜底文案');
    expect(backendMessage(new Error('网络断了'), '创建失败')).toBe(
      '创建失败（网络断了）',
    );
    expect(backendMessage(undefined, '创建失败')).toBe('创建失败');
  });
});

describe('日志分级与着色', () => {
  it('detectLogLevel 认后端四个级别，大小写与空白都容错', () => {
    expect(detectLogLevel('info')).toBe('info');
    expect(detectLogLevel('SUCCESS')).toBe('success');
    expect(detectLogLevel(' Warning ')).toBe('warning');
    expect(detectLogLevel('error')).toBe('error');
    expect(detectLogLevel('debug')).toBe('info');
  });

  it('level 缺失时从消息里的 [level] 标记兜底', () => {
    expect(detectLogLevel(null, '[error] 探活失败')).toBe('error');
    expect(detectLogLevel(undefined, '12:00:00 [warning] 慢')).toBe('warning');
    expect(detectLogLevel(undefined, '普通消息')).toBe('info');
  });

  it('formatLogLine 拼成 <time> [阶段] 消息，缺字段用占位符', () => {
    expect(
      formatLogLine({
        time: '12:00:01',
        stageLabel: '安装 windows-mcp',
        message: '正在 pip install',
      }),
    ).toBe('12:00:01 [安装 windows-mcp] 正在 pip install');
    expect(formatLogLine({ message: '无时间无阶段' })).toBe(
      '--:--:-- 无时间无阶段',
    );
  });

  it('formatLogEntry 把 seq 与 level 前缀塞进行首（展示时再剥掉）', () => {
    expect(
      formatLogEntry({
        seq: 12,
        time: '12:00:01',
        level: 'success',
        stageLabel: '探活 MCP（tools/list）',
        message: '探活通过',
      }),
    ).toBe('12|[success] 12:00:01 [探活 MCP（tools/list）] 探活通过');
    // 首屏 logTail 回放没有 seq，只带 level
    expect(formatLogEntry({ time: '12:00:02', message: '继续' })).toBe(
      '[info] 12:00:02 继续',
    );
  });

  it('stripLogDecorations 剥掉 seq| 与 [level] ，只留给人看的正文', () => {
    expect(stripLogDecorations('12|[success] 12:00:01 [上传] 完成')).toBe(
      '12:00:01 [上传] 完成',
    );
    expect(stripLogDecorations('[error] 12:00:01 失败')).toBe('12:00:01 失败');
    // 后端 logTail 的老格式：没有前缀，原样返回
    expect(stripLogDecorations('12:00:01 [上传] 完成')).toBe(
      '12:00:01 [上传] 完成',
    );
    // 消息正文里带竖线但行首不是纯数字时不能被当成 seq
    expect(stripLogDecorations('a|b')).toBe('a|b');
  });

  it('logLineColor 给出终端配色（浅灰/绿/橙/红）', () => {
    expect(logLineColor('1|[info] x')).toBe('#c9d1d9');
    expect(logLineColor('2|[success] x')).toBe('#52c41a');
    expect(logLineColor('3|[warning] x')).toBe('#faad14');
    expect(logLineColor('4|[error] x')).toBe('#ff4d4f');
    // 没有 [level] 前缀的旧格式按 info 处理
    expect(logLineColor('12:00:01 [上传] 完成')).toBe('#c9d1d9');
  });

  it('parseTailLines 切行去空白，空摘要给空数组', () => {
    expect(parseTailLines('a\n\n  b  \n')).toEqual(['a', 'b']);
    expect(parseTailLines('')).toEqual([]);
    expect(parseTailLines(null)).toEqual([]);
  });
});

describe('部署状态机（SSE 事件 → 界面状态）', () => {
  it('initialStreamState 用部署记录打底，并把 logTail 回放成日志行', () => {
    const state = initialStreamState(
      makeDeployment({
        stage: 'pip',
        progress: 0,
        logTail: '12:00:01 [上传] 完成\n[success] 12:00:02 [安装] 完成',
        lastSeq: 5,
        live: true,
      }),
    );
    expect(state.stage).toBe('pip');
    expect(state.stageLabel).toBe('安装 windows-mcp');
    // 后端没给 progress 时按阶段锚点推
    expect(state.progress).toBe(68);
    expect(state.logs).toEqual([
      '[info] 12:00:01 [上传] 完成',
      '[success] 12:00:02 [安装] 完成',
    ]);
    expect(state.lastSeq).toBe(5);
    expect(state.live).toBe(true);
    expect(state.ended).toBe(false);
  });

  it('hello 帧补齐阶段表与已有日志的序号', () => {
    const before = initialStreamState(makeDeployment());
    const after = deployStreamReducer(before, {
      type: 'hello',
      deployment: makeDeployment({
        stage: 'vbs',
        stageLabel: '写开机自启 VBS',
        stages: [{ key: 'vbs', label: '写开机自启 VBS', progress: 82 }],
        lastSeq: 9,
        live: true,
        status: 'running',
      }),
    });
    expect(after.stage).toBe('vbs');
    // 后端这一帧给了中文阶段名，就用它的
    expect(after.stageLabel).toBe('写开机自启 VBS');
    expect(after.stages).toEqual([
      { key: 'vbs', label: '写开机自启 VBS', progress: 82 },
    ]);
    expect(after.lastSeq).toBe(9);
    expect(after.live).toBe(true);
    expect(after.status).toBe('running');
  });

  it('hello 帧没给中文阶段名时，用固定阶段表把 key 翻成中文', () => {
    const before = {
      ...initialStreamState(makeDeployment()),
      // 上一条状态里也没有中文名（比如刚从排队态进流）
      stageLabel: '',
    };
    const after = deployStreamReducer(before, {
      type: 'hello',
      deployment: makeDeployment({ stage: 'expose', stageLabel: '' }),
    });
    expect(after.stage).toBe('expose');
    expect(after.stageLabel).toBe('开 portproxy + 防火墙');
  });

  it('log 帧追加一行并推进阶段，同 seq 的重复帧被丢掉', () => {
    const before = initialStreamState(makeDeployment());
    const event = {
      seq: 3,
      time: '12:00:03',
      level: 'success' as const,
      stage: 'upload',
      stageLabel: '上传 Python 安装包到 C:\\Installer',
      message: '上传完成',
      progress: 28,
    };
    const once = deployStreamReducer(before, { type: 'log', event });
    expect(once.logs).toHaveLength(1);
    expect(once.logs[0]).toBe(
      '3|[success] 12:00:03 [上传 Python 安装包到 C:\\Installer] 上传完成',
    );
    expect(once.stage).toBe('upload');
    expect(once.progress).toBe(28);
    expect(once.status).toBe('running');
    expect(once.live).toBe(true);

    // 重连补发同一条 → 幂等（日志行不增、lastSeq 不动）
    const twice = deployStreamReducer(once, { type: 'log', event });
    expect(twice.logs).toHaveLength(1);
    expect(twice.logs).toEqual(once.logs);
    expect(twice.lastSeq).toBe(once.lastSeq);
  });

  it('log 的 progress 只增不减（重放旧日志不会把进度条拽回去）', () => {
    const state = {
      ...initialStreamState(makeDeployment({ stage: 'expose' })),
      progress: 92,
    };
    const after = deployStreamReducer(state, {
      type: 'log',
      event: {
        seq: 1,
        time: '12:00:00',
        level: 'info',
        stage: 'prepare',
        stageLabel: '连接目标机 / 环境体检',
        message: '开始',
        progress: 6,
      },
    });
    expect(after.progress).toBe(92);
  });

  it('status 帧是权威状态，但空 error 不会清掉已经记下的失败原因', () => {
    const state = {
      ...initialStreamState(makeDeployment()),
      error: 'pip 安装 windows-mcp 失败：No matching distribution',
    };
    const after = deployStreamReducer(state, {
      type: 'status',
      id: 7,
      status: 'failed',
      stage: 'pip',
      stageLabel: '安装 windows-mcp',
      progress: 68,
      error: '',
      lastSeq: 20,
      live: false,
    });
    expect(after.status).toBe('failed');
    expect(after.error).toBe(
      'pip 安装 windows-mcp 失败：No matching distribution',
    );
    expect(after.progress).toBe(68);
    expect(after.lastSeq).toBe(20);
    expect(after.live).toBe(false);
  });

  it('status 帧带新 error 时覆盖旧值', () => {
    const state = {
      ...initialStreamState(makeDeployment()),
      error: '旧错误',
    };
    const after = deployStreamReducer(state, {
      type: 'status',
      id: 7,
      status: 'failed',
      stage: 'expose',
      stageLabel: '开 portproxy + 防火墙',
      progress: 92,
      error: 'portproxy 添加失败：端口已被占用',
      lastSeq: 21,
      live: false,
    });
    expect(after.error).toBe('portproxy 添加失败：端口已被占用');
  });

  it('end 帧落终态并标记 ended', () => {
    const before = initialStreamState(makeDeployment());
    const success = deployStreamReducer(before, {
      type: 'end',
      status: 'success',
    });
    expect(success.status).toBe('success');
    expect(success.live).toBe(false);
    expect(success.ended).toBe(true);

    const cancelled = deployStreamReducer(before, {
      type: 'end',
      status: 'cancelled',
    });
    expect(cancelled.status).toBe('cancelled');
    expect(cancelled.ended).toBe(true);
  });

  it('ended 之后再来日志不会把状态改回 running', () => {
    const ended = deployStreamReducer(initialStreamState(makeDeployment()), {
      type: 'end',
      status: 'success',
    });
    const after = deployStreamReducer(ended, {
      type: 'log',
      event: {
        seq: 99,
        time: '12:10:00',
        level: 'info',
        stage: 'verify',
        stageLabel: '探活 MCP（tools/list）',
        message: '迟到的日志',
        progress: 100,
      },
    });
    expect(after.status).toBe('success');
    expect(after.logs).toHaveLength(1);
  });
});

describe('结果摘要与其它小工具', () => {
  it('buildDeployResult 汇总成功页要展示的字段', () => {
    const result = buildDeployResult(
      makeDeployment({
        status: 'success',
        mcpUrl: 'http://10.0.0.31:8000/mcp',
        mcpVersion: '0.2.1',
        mcpToolCount: 2,
        pythonPath: 'C:\\Program Files\\Python313\\python.exe',
        vbsPath: 'C:\\ProgramData\\ComputerUse\\start-mcp.vbs',
        startupPath:
          'C:\\Users\\Administrator\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\computer-use-mcp.lnk',
        portproxy: true,
      }),
      ['screenshot', 'click'],
    );
    expect(result.mcpUrl).toBe('http://10.0.0.31:8000/mcp');
    expect(result.mcpToolCount).toBe(2);
    expect(result.toolNames).toEqual(['screenshot', 'click']);
    expect(result.portproxy).toBe(true);
    expect(result.mcpPort).toBe(8000);
    expect(result.vbsPath).toContain('start-mcp.vbs');
  });

  it('后端没给工具数时按工具名个数算，工具名为空则过滤掉', () => {
    const result = buildDeployResult(
      makeDeployment({ mcpToolCount: 0, mcpPort: 0 }),
      ['screenshot', '', 'click'],
    );
    expect(result.mcpToolCount).toBe(2);
    expect(result.toolNames).toEqual(['screenshot', 'click']);
    // 端口缺失时用默认值，避免成功页显示 0
    expect(result.mcpPort).toBe(8000);
  });

  it('defaultVersionItem 挑第一个支持 windows-mcp 的版本', () => {
    // 清单是「新版本在前」，默认选中就是第一个 mcpCompatible 的
    const items = [
      makeVersionItem({ version: '3.14.0', mcpCompatible: true }),
      makeVersionItem({ version: '3.13.1', mcpCompatible: true }),
      makeVersionItem({ version: '3.12.9', mcpCompatible: false }),
    ];
    expect(defaultVersionItem(items)?.version).toBe('3.14.0');
    expect(
      defaultVersionItem([makeVersionItem({ mcpCompatible: false })]),
    ).toBeNull();
    expect(defaultVersionItem([])).toBeNull();
  });

  it('freshOptions 不污染默认值对象', () => {
    const options = freshOptions({ mcpPort: 9001 });
    expect(options.mcpPort).toBe(9001);
    expect(DEFAULT_DEPLOY_OPTIONS.mcpPort).toBe(8000);
    expect(freshOptions().pipIndex).toBe(
      'https://pypi.tuna.tsinghua.edu.cn/simple',
    );
  });

  it('isHostReady 优先信 /status 的 ready', () => {
    expect(isHostReady({ ready: true })).toBe(true);
    expect(isHostReady({ ready: false }, { ready: true })).toBe(true);
    expect(isHostReady({ ready: true }, { ready: false })).toBe(false);
    expect(isHostReady({})).toBe(false);
  });

  it('deployEntryFromDeployment 取「再部署一台」需要的主机信息', () => {
    expect(deployEntryFromDeployment(makeDeployment())).toEqual({
      hostId: 3,
      accountId: 11,
      hostName: 'win-01',
    });
  });

  it('optionsSummary 汇总本次部署用的参数', () => {
    const summary = optionsSummary(
      makeDeployment({
        options: {
          mcpPort: 9001,
          pipIndex: 'https://mirrors.example/simple',
          bindAll: true,
          authKey: 'x',
        },
      }),
    );
    expect(summary).toContain('MCP 端口 9001');
    expect(summary).toContain('pip 源 https://mirrors.example/simple');
    expect(summary).toContain('监听所有网卡');
    expect(summary).toContain('已启用访问密钥');

    // 老记录 options 为空时不能抛异常，且要给出「未启用」的结论
    const legacy = optionsSummary(makeDeployment({ options: {} }));
    expect(legacy).toContain('未启用访问密钥');
    expect(legacy).not.toContain('pip 源');
  });
});
