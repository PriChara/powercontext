import { FIELD_LABELS } from './handover.js';

const SECTIONS = [
  { field: 'next_steps', kind: 'do', heading: '先做（交接里写明的下一步）' },
  { field: 'finding', kind: 'confirm', heading: '先复现或确认的现象' },
  { field: 'evidence', kind: 'verify', heading: '证据核对（照着能验证）' },
  { field: 'conclusion', kind: 'context', heading: '已有结论（不必重复推导）' },
  { field: 'risks', kind: 'avoid', heading: '避免（交接里点名的坑）' },
];

const EXCERPT_CHARS = 200;

function normKey(text) {
  return String(text)
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, '');
}

function clip(text, max) {
  const flat = String(text).replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

function blockTitle(block) {
  const title = block.fields?.title?.trim();
  if (title) return title;
  if (block.receipt?.title) return block.receipt.title;
  return `记录 ${block.record_id}`;
}

function sourceLabel(block) {
  return `${block.record_id}「${clip(blockTitle(block), 40)}」`;
}

export function buildChecklist({ project, query, blocks, status, maxBytes, recalledAt, contentLength = 0, limit = 14 }) {
  const usable = (blocks ?? []).filter((block) => block.structured);
  const items = [];
  const notes = [];
  const seen = new Set();

  for (const section of SECTIONS) {
    for (const block of usable) {
      for (const line of block.fields[section.field] ?? []) {
        if (items.length >= limit) break;
        const key = `${section.kind}|${normKey(line)}`;
        if (seen.has(key)) continue;
        seen.add(key);
        items.push({
          id: items.length + 1,
          kind: section.kind,
          heading: section.heading,
          field: section.field,
          text: line,
          source_record_ids: [block.record_id],
          source_label: sourceLabel(block),
        });
      }
    }
  }

  const usedRecordIds = [...new Set(items.flatMap((item) => item.source_record_ids))];
  const usedBlocks = usable.filter((block) => usedRecordIds.includes(block.record_id));

  if (items.length >= limit) {
    notes.push(`清单条目已截断到 ${limit} 条（召回片段较多时可换更具体的当前问题，或调大 max_bytes 后重试）。`);
  }
  if (blocks?.length && usable.length === 0) {
    notes.push(
      `召回返回了 ${blocks.length} 段文本，但没有可解析的交接字段（可能是旧记录或 PowerContext 重排了文本）。这些片段只在「召回原文」区展示，未进入清单。`
    );
  }
  if (!blocks || blocks.length === 0) {
    notes.push(
      `没有召回到带记录标记的交接内容（status: ${status ?? '未知'}，content 长度 ${String(query ?? '').length > 0 ? '见原文' : '空'}）。请先「保存一次交接」，或把当前问题写成交接里出现过的词。`
    );
  }
  const truncatedBlocks = (blocks ?? []).filter((block) => block.truncated);
  if (truncatedBlocks.length > 0) {
    notes.push(`${truncatedBlocks.length} 段召回内容被长度上限截断（见原文区标记）。`);
  }

  const header = [
    `# 接手清单 · ${project}`,
    `当前问题：${clip(query, 200)}`,
    `生成时间：${recalledAt}`,
    `召回状态：${status ?? '未知'} · max_bytes=${maxBytes} · 召回记录 ${blocks?.length ?? 0} 条 · 清单条目 ${items.length} 条`,
  ];

  const body = [];
  SECTIONS.forEach((section, index) => {
    const sectionItems = items.filter((item) => item.kind === section.kind);
    if (sectionItems.length === 0) return;
    body.push('', `## ${index + 1}. ${section.heading}`);
    for (const item of sectionItems) body.push(`- [ ] ${item.text}（来源 ${item.source_label}）`);
  });

  const sources = [];
  if (usedBlocks.length > 0) {
    sources.push('', '## 这份清单来自哪些保存记录');
    usedBlocks.forEach((block, index) => {
      const matched = (block.score?.matched ?? []).map((m) => `${m.token}×${m.count}`).join('、') || '（按召回顺序）';
      const entry = block.receipt?.entry_id ? ` · entry ${block.receipt.entry_id}` : '';
      const savedAt = block.fields.created_at || block.receipt?.saved_at || '';
      sources.push(
        `${index + 1}. ${sourceLabel(block)}${entry}${savedAt ? ` · 保存于 ${savedAt}` : ''}`,
        `   命中搜索词：${matched}`,
        `   摘录：${clip(block.raw_text, EXCERPT_CHARS)}`
      );
    });
  }

  const tail = ['', '说明：条目逐条来自上面列出的 PowerContext 召回片段，按召回顺序去重，未使用模型密钥或模板话术。'];
  if (notes.length > 0) {
    tail.push('', '注意：', ...notes.map((note) => `- ${note}`));
  }

  return { text: [...header, ...body, ...sources, ...tail].join('\n').trim(), items, notes };
}

export function sectionHeadings() {
  return SECTIONS.map((section) => ({ kind: section.kind, heading: section.heading, label: FIELD_LABELS[section.field] }));
}
