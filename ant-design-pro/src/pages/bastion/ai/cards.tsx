/**
 * ```ai-card 卡片的**唯一**渲染实现。
 *
 * 模型在正文里插 ```ai-card 代码块（JSON），对话页要把它渲染成 antd 卡片；
 * 审计页的「对话详情」也要把落库的 `ai_messages.cards` 用同一种样子还原出来 ——
 * 两处必须是同一份代码，否则审计看到的和当时用户看到的会长得不一样。
 */
import { Alert, Descriptions, Steps, Table, Tag } from 'antd';
import React from 'react';
import type { AiCard } from '@/services/bastion/ai';
import './cards.css';

/** 单张 ai-card → antd 组件 */
export const AiCardView = ({ card }: { card: AiCard }) => {
  if (card.type === 'table') {
    const columns = (card.columns ?? []).map((col) => ({
      title: col.title ?? col.key,
      dataIndex: col.key,
      key: col.key,
      ellipsis: true,
      render: (value: unknown) =>
        value === null || value === undefined || value === ''
          ? '-'
          : String(value),
    }));
    return (
      <div className="bastion-ai-card">
        {card.title ? (
          <div className="bastion-ai-card-title">{card.title}</div>
        ) : null}
        <Table
          size="small"
          rowKey={(_, index) => String(index)}
          columns={columns}
          dataSource={card.rows ?? []}
          pagination={(card.rows ?? []).length > 10 ? { pageSize: 10 } : false}
          scroll={{ x: 'max-content' }}
        />
      </div>
    );
  }
  if (card.type === 'keyvalue') {
    return (
      <div className="bastion-ai-card">
        {card.title ? (
          <div className="bastion-ai-card-title">{card.title}</div>
        ) : null}
        <Descriptions size="small" column={1} bordered>
          {(card.items ?? []).map((item) => (
            <Descriptions.Item key={item.label} label={item.label}>
              {item.status ? (
                <Tag
                  color={
                    item.status === 'success'
                      ? 'success'
                      : item.status === 'warning'
                        ? 'warning'
                        : item.status === 'error'
                          ? 'error'
                          : 'default'
                  }
                >
                  {String(item.value ?? '-')}
                </Tag>
              ) : (
                String(item.value ?? '-')
              )}
            </Descriptions.Item>
          ))}
        </Descriptions>
      </div>
    );
  }
  if (card.type === 'alert') {
    return (
      <Alert
        className="bastion-ai-card"
        type={card.level ?? 'info'}
        showIcon
        message={card.title ?? '提示'}
        description={card.text}
      />
    );
  }
  if (card.type === 'steps') {
    return (
      <div className="bastion-ai-card">
        {card.title ? (
          <div className="bastion-ai-card-title">{card.title}</div>
        ) : null}
        <Steps
          direction="vertical"
          size="small"
          current={-1}
          items={(card.items ?? []).map((item) => ({
            title: item.title,
            description: item.description,
            status:
              item.status === 'finish'
                ? 'finish'
                : item.status === 'process'
                  ? 'process'
                  : 'wait',
          }))}
        />
      </div>
    );
  }
  if (card.type === 'image') {
    const images = (card.images ?? []).filter(Boolean);
    if (images.length === 0) return null;
    return (
      <div className="bastion-ai-card">
        {card.title ? (
          <div className="bastion-ai-card-title">{card.title}</div>
        ) : null}
        <div className="bastion-ai-card-images">
          {images.map((src, index) => (
            <img
              // biome-ignore lint/suspicious/noArrayIndexKey: 后端给的截图列表没有 id，顺序即身份
              key={`image#${index}`}
              className="bastion-ai-card-image"
              src={src}
              alt={`${card.title ?? '截图'} ${index + 1}`}
            />
          ))}
        </div>
        {card.caption ? (
          <div className="bastion-ai-card-caption">{card.caption}</div>
        ) : null}
      </div>
    );
  }
  return null;
};

/** 一串 ai-card（落库的 `ai_messages.cards`） */
export const AiCards = ({ cards }: { cards?: AiCard[] }) => {
  if (!cards || cards.length === 0) return null;
  return (
    <div className="bastion-ai-cards">
      {Object.entries(cards).map(([position, card]) => (
        <AiCardView key={`${card.type}#${position}`} card={card} />
      ))}
    </div>
  );
};

/** 把 Markdown 代码块的 children 摊平成字符串（块级代码就是一整段文本） */
const flattenText = (value: React.ReactNode): string => {
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(flattenText).join('');
  if (
    value &&
    typeof value === 'object' &&
    'props' in (value as { props?: unknown })
  ) {
    return flattenText(
      (value as { props?: { children?: React.ReactNode } }).props?.children,
    );
  }
  return '';
};

const parseCard = (children: React.ReactNode): AiCard | null => {
  const raw = flattenText(children).trim();
  if (!raw.startsWith('{')) return null;
  try {
    const data = JSON.parse(raw) as AiCard;
    return data && typeof data === 'object' && 'type' in data ? data : null;
  } catch {
    return null;
  }
};

/**
 * Markdown 代码块渲染器：普通代码原样交给 `<pre>`，```ai-card 则换成 antd 卡片。
 *
 * 卡片在**原位**渲染（就落在这个代码块的位置），所以正文里的表格/告警/步骤不会跑到别处去。
 * 流式过程中 JSON 还没拼完时给一个「正在生成卡片…」的占位，避免把半截 JSON 甩给用户看。
 *
 * 对话页（AI 运维）与 RDP 侧边栏共用它 —— 两处的正文卡片必须长得一模一样。
 */
export const AiCode = (
  props: React.HTMLAttributes<HTMLElement> & {
    block?: boolean;
    lang?: string;
    streamStatus?: 'loading' | 'done';
  },
) => {
  const { block, lang, children, streamStatus } = props;
  if (block && typeof lang === 'string' && lang.startsWith('ai-card')) {
    const card = parseCard(children);
    if (card) return <AiCardView card={card} />;
    if (streamStatus === 'loading') {
      return <span className="bastion-ai-card-pending">正在生成卡片…</span>;
    }
    return <code className="bastion-ai-code">{flattenText(children)}</code>;
  }
  return (
    <code className={block ? 'bastion-ai-code' : undefined}>{children}</code>
  );
};
