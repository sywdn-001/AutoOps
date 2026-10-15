import type { AiCard } from '@/services/bastion/ai';

/**
 * 一条回答里的「一块」：模型每轮先思考（reasoning）、再决定调用哪些工具
 * （toolCalls）、最后给出这一轮的文字（content）。
 *
 * 为什么要切块：后端本来就是**一轮一条 assistant 消息**（每轮各自带
 * reasoning / tool_calls / content），但前端把整条回答的思考和工具调用糊成
 * 了扁平的两个折叠块，长任务读起来就成了「思考 | 工具调用 | 回答」。
 * 用户要的是按轮交错：「思考 | 工具调用 | 思考 | 工具调用 | … | 回答」。
 */
export interface RoundBlock<TCall> {
  /** 后端事件上的 round（从 0 开始）；续流时补执行的挂起调用没有 round，归进最后一块 */
  round: number;
  reasoning: string;
  content: string;
  toolCalls: TCall[];
  cards: AiCard[];
}

export const newRoundBlock = <TCall>(round = 0): RoundBlock<TCall> => ({
  round,
  reasoning: '',
  content: '',
  toolCalls: [],
  cards: [],
});

/**
 * 把一次事件并进正确的块，返回新的块数组。
 *
 * - 没有 round（续流时补执行的挂起调用）：并进**最后一块** —— 那张「待确认」的
 *   卡片就在最后一块里，正好被这次 tool_result 翻成成功；
 * - round 与最后一块相同：还是最后一块；
 * - round 变了（下一轮，或续流后 round 从 0 重新数）：另起一块。
 */
export const patchRoundBlock = <TCall>(
  blocks: RoundBlock<TCall>[] | undefined,
  round: number | undefined,
  update: (block: RoundBlock<TCall>) => RoundBlock<TCall>,
): RoundBlock<TCall>[] => {
  const list = blocks ?? [];
  if (list.length === 0) return [update(newRoundBlock<TCall>(round ?? 0))];
  const lastIndex = list.length - 1;
  const last = list[lastIndex];
  if (round === undefined || round === last.round) {
    return list.map((block, index) =>
      index === lastIndex ? update(block) : block,
    );
  }
  return [...list, update(newRoundBlock<TCall>(round))];
};

/**
 * 渲染兜底：没有 blocks（历史数据、或还没收到任何事件）时用扁平字段合成一块，
 * 这样老数据和实时流走的是同一套渲染。
 */
export const roundBlocksOf = <TCall>(
  blocks: RoundBlock<TCall>[] | undefined,
  flat: Omit<RoundBlock<TCall>, 'round'>,
): RoundBlock<TCall>[] => {
  if (blocks && blocks.length > 0) return blocks;
  const { reasoning, content, toolCalls, cards } = flat;
  if (!reasoning && !content && toolCalls.length === 0 && cards.length === 0) {
    return [];
  }
  return [{ round: 0, reasoning, content, toolCalls, cards }];
};
