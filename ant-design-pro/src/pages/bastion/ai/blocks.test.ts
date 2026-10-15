/**
 * 按轮切块（`blocks.ts`）的纯函数测试。
 *
 * 这是「思考 | 工具调用 | 思考 | 工具调用 | … | 回答」排版的底座：
 * AI 运维页与 RDP 侧边栏的实时流、以及 AI 页的历史回放都靠这三个函数。
 */
import { describe, expect, it } from 'vitest';
import { newRoundBlock, patchRoundBlock, roundBlocksOf } from './blocks';

interface FakeCall {
  callId: string;
  status: string;
}

describe('newRoundBlock', () => {
  it('空块：round 默认 0，其余字段都是空壳', () => {
    expect(newRoundBlock<FakeCall>()).toEqual({
      round: 0,
      reasoning: '',
      content: '',
      toolCalls: [],
      cards: [],
    });
    expect(newRoundBlock<FakeCall>(3).round).toBe(3);
  });
});

describe('patchRoundBlock', () => {
  it('还没有块时先建一块，round 缺省就当第 0 块', () => {
    const blocks = patchRoundBlock<FakeCall>(undefined, 0, (block) => ({
      ...block,
      content: '第一轮',
    }));
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({ round: 0, content: '第一轮' });

    const noRound = patchRoundBlock<FakeCall>(
      undefined,
      undefined,
      (block) => ({
        ...block,
        content: '续流',
      }),
    );
    expect(noRound[0].round).toBe(0);
  });

  it('round 与最后一块相同：就地并进最后一块', () => {
    const first = [newRoundBlock<FakeCall>(0)];
    const blocks = patchRoundBlock<FakeCall>(first, 0, (block) => ({
      ...block,
      reasoning: `${block.reasoning}想`,
    }));
    expect(blocks).toHaveLength(1);
    expect(blocks[0].reasoning).toBe('想');
  });

  it('round 变了：另起一块，前一块原样不动', () => {
    const first = [{ ...newRoundBlock<FakeCall>(0), content: '第一轮' }];
    const blocks = patchRoundBlock<FakeCall>(first, 1, (block) => ({
      ...block,
      content: '第二轮',
    }));
    expect(blocks.map((block) => block.round)).toEqual([0, 1]);
    expect(blocks[0].content).toBe('第一轮');
    expect(blocks[1].content).toBe('第二轮');
    // 原数组不被就地改写（React 里要靠新引用来触发重渲染）
    expect(first).toHaveLength(1);
  });

  it('round 缺省（续流时补执行的挂起调用）：并进最后一块', () => {
    const blocks = patchRoundBlock<FakeCall>(
      [
        { ...newRoundBlock<FakeCall>(0), content: '第一轮' },
        { ...newRoundBlock<FakeCall>(1), content: '第二轮' },
      ],
      undefined,
      (block) => ({ ...block, content: `${block.content}+补执行` }),
    );
    expect(blocks.map((block) => block.content)).toEqual([
      '第一轮',
      '第二轮+补执行',
    ]);
  });
});

describe('roundBlocksOf', () => {
  it('有 blocks 就直接用（实时流只要收到过带 round 的事件就命中）', () => {
    const blocks = [{ ...newRoundBlock<FakeCall>(0), content: '实时' }];
    expect(
      roundBlocksOf<FakeCall>(blocks, {
        reasoning: '',
        content: '扁平',
        toolCalls: [],
        cards: [],
      }),
    ).toBe(blocks);
  });

  it('没有 blocks（历史数据）就用扁平字段合成一块，老数据也走同一套渲染', () => {
    const blocks = roundBlocksOf<FakeCall>(undefined, {
      reasoning: '想过',
      content: '答过',
      toolCalls: [{ callId: 'c1', status: 'success' }],
      cards: [{ type: 'alert', text: '卡片' }],
    });
    expect(blocks).toEqual([
      {
        round: 0,
        reasoning: '想过',
        content: '答过',
        toolCalls: [{ callId: 'c1', status: 'success' }],
        cards: [{ type: 'alert', text: '卡片' }],
      },
    ]);
  });

  it('全空时返回空数组（不渲染一个空壳块）', () => {
    expect(
      roundBlocksOf<FakeCall>(undefined, {
        reasoning: '',
        content: '',
        toolCalls: [],
        cards: [],
      }),
    ).toEqual([]);
    expect(
      roundBlocksOf<FakeCall>([], {
        reasoning: '',
        content: '',
        toolCalls: [],
        cards: [],
      }),
    ).toEqual([]);
  });
});
