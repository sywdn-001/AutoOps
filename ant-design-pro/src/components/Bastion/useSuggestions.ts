/**
 * AI 输入框的示例建议（AI 运维页 + ComputerUse 侧边栏共用）。
 *
 * 建议由后端按**系统当前状态**生成（`GET /api/ai/suggestions`，中英双语各一份）：
 * - 每 24 小时换一轮：本地缓存 24 小时后失效，后端按「当天」桶返回同一批；
 * - 也可以手动换：`refresh()` 把 nonce +1 再取一批；
 * - 后端不可用时退回调用方给的兜底文案（中文），保证输入框永远有东西可点。
 */
import { request } from '@umijs/max';
import { useEffect, useState } from 'react';
import { isEnglish } from '@/i18n';

export type SuggestionPayload = {
  bucket: string;
  nonce: number;
  source?: 'ai' | 'mixed' | 'fallback';
  items: { zh: string; en: string }[];
};

const TTL_MS = 24 * 60 * 60 * 1000; // 一轮 24 小时
const cacheKey = (scope: string) => `bastion_suggest_${scope}`;

type Cache = SuggestionPayload & { savedAt: number };

function readCache(scope: string): Cache | null {
  try {
    const raw = localStorage.getItem(cacheKey(scope));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Cache;
    if (!parsed?.items?.length) return null;
    if (Date.now() - (parsed.savedAt || 0) > TTL_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeCache(scope: string, payload: SuggestionPayload): void {
  try {
    localStorage.setItem(
      cacheKey(scope),
      JSON.stringify({ ...payload, savedAt: Date.now() }),
    );
  } catch {
    /* 隐私模式等写不进去就忽略 */
  }
}

const pick = (item: { zh: string; en: string }): string =>
  isEnglish() ? item.en || item.zh : item.zh;

export function useSuggestions(
  scope: 'chat' | 'computeruse',
  fallback: string[],
): { items: string[]; refresh: () => void; loading: boolean } {
  const [items, setItems] = useState<string[]>(fallback);
  const [nonce, setNonce] = useState(0);
  const [loading, setLoading] = useState(false);

  const load = async (next: number, force: boolean) => {
    const cached = readCache(scope);
    if (!force && cached) {
      setItems(cached.items.map(pick));
      setNonce(cached.nonce ?? 0);
      return;
    }
    setLoading(true);
    try {
      const resp = await request<SuggestionPayload>('/api/ai/suggestions', {
        method: 'GET',
        params: { scope, nonce: next },
        // 首次生成要真调一次模型（实测 6~8 秒），别用默认的短超时
        timeout: 30000,
      });
      // 拦截器一般已经解包成 data；没解包时再兜一层
      const payload =
        (resp as unknown as { items?: unknown })?.items !== undefined
          ? (resp as unknown as SuggestionPayload)
          : ((resp as unknown as { data?: SuggestionPayload })?.data as
              | SuggestionPayload
              | undefined);
      if (payload?.items?.length) {
        // 只有真·模型产出的批次才缓存：兜底文案不缓存，下次进来继续尝试生成
        if (payload.source !== 'fallback') {
          writeCache(scope, payload);
        }
        setItems(payload.items.map(pick));
        setNonce(payload.nonce ?? next);
      }
    } catch {
      // 后端不可用（离线 / 测试环境）：保留兜底文案
    }
    setLoading(false);
  };

  useEffect(() => {
    void load(0, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);

  return { items, refresh: () => void load(nonce + 1, true), loading };
}
