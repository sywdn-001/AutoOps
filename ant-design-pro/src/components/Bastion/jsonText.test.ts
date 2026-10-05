/**
 * 审计页 JSON 文本层单测：工具消息拆行 + 截断 JSON 修复。
 *
 * 这些用例盯的是「用户截图里的那种场景」：`role="tool"` 的消息正文里装着几 KB 的 JSON，
 * 而且这段 JSON 被后端按字符截断过（`…（结果过长，已截断，共 63909 字符）`）——它已经不是
 * 合法 JSON 了，但页面必须能把它摊成卡片，同时不许把原文改掉或凭空补数据。
 */
import {
  parseMaybeJson,
  repairTruncatedJson,
  splitToolMessage,
} from './jsonText';

const COMPLETE = `[工具 list_users] 成功：OK（共 6 条）
[
  {
    "id": 1,
    "username": "admin"
  },
  {
    "id": 2,
    "username": "laowang"
  }
]`;

const TRUNCATED = `[工具 list_ai_tools] 成功：ok
{
  "available": 55,
  "groups": [
    {
      "category": "总览",
      "count": 4
    },
    {
      "category": "身份",
      "count": 7…（结果过长，已截断，共 63909 字符）`;

describe('repairTruncatedJson', () => {
  it('把被后端截断的 JSON 修回可解析，并标出这是截断后的条目', () => {
    const repaired = repairTruncatedJson(
      TRUNCATED.split('\n').slice(1).join('\n'),
    );
    expect(repaired).toBeDefined();
    expect(repaired?.truncated).toBe(true);
    expect(repaired?.data).toEqual({
      available: 55,
      groups: [{ category: '总览', count: 4 }],
    });
  });

  it('完整 JSON 原样解析，不算截断', () => {
    const repaired = repairTruncatedJson('{"a": 1}');
    expect(repaired).toEqual({ data: { a: 1 }, truncated: false });
  });

  it('彻底解析不了就返回 undefined，不回退成半个对象', () => {
    expect(repairTruncatedJson('{ 这不是 JSON')).toBeUndefined();
    expect(repairTruncatedJson('   ')).toBeUndefined();
  });
});

describe('parseMaybeJson', () => {
  it('普通文本原样返回，不当 JSON', () => {
    expect(parseMaybeJson('你好')).toEqual({
      data: '你好',
      json: false,
      truncated: false,
    });
  });

  it('被截断的 JSON 也能给出卡片可用的数据', () => {
    const parsed = parseMaybeJson(
      '{"rows": [1, 2], "note": "很长的说明…（结果过长，已截断，共 900 字符）',
    );
    expect(parsed.json).toBe(true);
    expect(parsed.truncated).toBe(true);
  });
});

describe('splitToolMessage', () => {
  it('拆出给人看的一行 + 完整 JSON 载荷', () => {
    const parts = splitToolMessage(COMPLETE);
    expect(parts.head).toBe('[工具 list_users] 成功（共 6 条）');
    expect(parts.json).toBe(true);
    expect(parts.payload).toHaveLength(2);
  });

  it('老记录里的机器词 ok 翻成中文，且不与状态词重复', () => {
    const parts = splitToolMessage(TRUNCATED);
    expect(parts.head).toBe('[工具 list_ai_tools] 成功');
    expect(parts.truncated).toBe(true);
    expect(parts.payload).toEqual({
      available: 55,
      groups: [{ category: '总览', count: 4 }],
    });
  });

  it('失败消息里多出的 HTTP 行不会挡住载荷起点', () => {
    const parts = splitToolMessage(
      '[工具 get_command_policy] 失败：失败：权限不足，需要：policy:view\nHTTP 403\n{"ok": false}',
    );
    expect(parts.head).toBe(
      '[工具 get_command_policy] 失败：权限不足，需要：policy:view\nHTTP 403',
    );
    expect(parts.payload).toEqual({ ok: false });
  });

  it('没有载荷时按普通文本处理，一个字都不吞', () => {
    const parts = splitToolMessage(
      '[工具 不存在的工具] 不存在，请从可用工具里选择',
    );
    expect(parts.payloadText).toBe('');
    expect(parts.json).toBe(false);
    expect(parts.head).toBe('[工具 不存在的工具] 不存在，请从可用工具里选择');
  });
});
