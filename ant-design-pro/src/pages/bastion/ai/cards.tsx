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
