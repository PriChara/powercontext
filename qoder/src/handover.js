import { createHash, randomBytes } from 'node:crypto';

export class ValidationError extends Error {
  constructor(message, fields) {
    super(message);
    this.name = 'ValidationError';
    this.fields = fields;
  }
}

export const FIELD_LABELS = {
  title: '标题',
  finding: '现象与发现',
  evidence: '证据',
  conclusion: '结论或判断',
  next_steps: '下一步',
  risks: '风险或不要做',
};

const LIMITS = {
  project: 120,
  title: 120,
  finding: 1200,
  evidence: 1600,
  conclusion: 600,
  next_steps: 1200,
  risks: 600,
};

const KEY_ORDER = [
  ['课题', 'project'],
  ['标题', 'title'],
  ['时间', 'created_at'],
  ['发现', 'finding'],
  ['证据', 'evidence'],
  ['结论', 'conclusion'],
  ['下一步', 'next_steps'],
  ['风险', 'risks'],
];

const KEY_TO_FIELD = new Map(KEY_ORDER.map(([key, field]) => [key, field]));
const LIST_FIELDS = new Set(['finding', 'evidence', 'conclusion', 'next_steps', 'risks']);

const KEY_NAME = '课题|标题|时间|发现|证据|结论|下一步|风险';
const KEY_LINE_RE = new RegExp(`^\\s*(${KEY_NAME})\\s*:\\s*(.*)$`);
const BULLET_RE = /^\s*(?:[-*•]|\d+[.)、])\s+(.*)$/;
const MARKER_RE = /hb-[0-9a-z]{10}(?![0-9a-z-])/g;
const FORGED_MARKER_RE = /hb-[0-9a-z]{10}/g;
const RESERVED_KEY_RE = new RegExp(`(${KEY_NAME})\\s*:`, 'g');
const SEPARATOR_RE = /^\s*(?:[-=*#]{3,}|#{1,6}\s+\S.*)$/;

const CONTROL_CHARS_RE = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g;

export function newRecordId() {
  const stamp = Date.now().toString(36).slice(-6);
  const rand = randomBytes(4).toString('hex').slice(0, 4);
  return `hb-${stamp}${rand}`;
}

export function slugifyProject(name) {
  const base = String(name ?? '')
    .toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/[^0-9a-z\u4e00-\u9fff-]/g, '')
    .replace(/-{2,}/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 24);
  const hash = createHash('sha256').update(String(name ?? '').trim().toLowerCase()).digest('hex').slice(0, 8);
  return `${base || 'project'}-${hash}`;
}

export function idempotencyKeyFor(name) {
  const trimmed = String(name ?? '').trim().toLowerCase();
  const hash = createHash('sha256').update(trimmed).digest('hex').slice(0, 16);
  return `handover-book:project:${hash}`;
}

function flatten(value) {
  return String(value ?? '')
    .replace(/\r\n?/g, '\n')
    .replace(CONTROL_CHARS_RE, '')
    .replace(/\t/g, ' ');
}

function cleanField(value, field) {
  const label = FIELD_LABELS[field] ?? field;
  const limit = LIMITS[field];
  const text = flatten(value).trim();
  if (text.length > limit) {
    throw new ValidationError(`「${label}」过长（${text.length} 字，上限 ${limit} 字），请精简后重试。`, [field]);
  }
  return text;
}

/** 用户文本里可能伪造记录标记或复用字段名，写进 PowerContext 前先消歧，保证解析只认我们生成的结构。 */
function escapeValueLine(line) {
  return line
    .replace(FORGED_MARKER_RE, (match) => match.toUpperCase())
    .replace(RESERVED_KEY_RE, (match, key) => `${key}：`)
    .trim();
}

function toLines(value, field) {
  const text = cleanField(value, field);
  if (text === '') return [];
  return text
    .split('\n')
    .map((line) => escapeValueLine(line))
    .filter((line) => line !== '');
}

export function normalizeHandover(input) {
  const project = String(input?.project ?? '').replace(CONTROL_CHARS_RE, '').trim().slice(0, LIMITS.project);
  if (project === '') throw new ValidationError('请先填写课题名称。', ['project']);

  const titleLines = toLines(input?.title, 'title');
  if (titleLines.length === 0) throw new ValidationError('请填写交接标题（一句话说明这次交接在讲什么）。', ['title']);

  const record = {
    record_id: typeof input?.record_id === 'string' && /^hb-[0-9a-z]{10}$/.test(input.record_id) ? input.record_id : newRecordId(),
    project,
    title: titleLines[0].slice(0, LIMITS.title),
    created_at: typeof input?.created_at === 'string' && input.created_at.trim() !== '' ? input.created_at.trim() : new Date().toISOString(),
    finding: toLines(input?.finding, 'finding'),
    evidence: toLines(input?.evidence, 'evidence'),
    conclusion: toLines(input?.conclusion, 'conclusion'),
    next_steps: toLines(input?.next_steps, 'next_steps'),
    risks: toLines(input?.risks, 'risks'),
  };

  const hasContent =
    record.finding.length > 0 || record.evidence.length > 0 || record.next_steps.length > 0 || record.conclusion.length > 0;
  if (!hasContent) {
    throw new ValidationError('至少要填写「现象与发现」「证据」「下一步」中的一项，否则接手的人无据可依。', [
      'finding',
      'evidence',
      'next_steps',
    ]);
  }
  if (record.next_steps.length === 0) {
    throw new ValidationError('「下一步」不能为空：接手清单依赖这一步，否则清单只能复述现象。', ['next_steps']);
  }
  return record;
}

export function renderRecordText(record) {
  const lines = [`[HB:${record.record_id}]`];
  for (const [key, field] of KEY_ORDER) {
    const value = record[field];
    if (field === 'title' || field === 'project' || field === 'created_at') {
      if (String(value ?? '').trim() !== '') lines.push(`${key}: ${value}`);
      continue;
    }
    if (!Array.isArray(value) || value.length === 0) continue;
    lines.push(`${key}:`);
    for (const item of value) lines.push(`- ${item}`);
  }
  return lines.join('\n');
}

export function renderReason(record) {
  const parts = [`课题「${record.project}」的第 ${record.created_at.slice(0, 10)} 次交接`, `标题：${record.title}`];
  if (record.finding.length) parts.push(`含发现 ${record.finding.length} 条`);
  if (record.evidence.length) parts.push(`含证据 ${record.evidence.length} 条`);
  if (record.next_steps.length) parts.push(`下一步 ${record.next_steps.length} 条`);
  parts.push(`记录号 ${record.record_id}（用于接手清单溯源）`);
  return parts.join('；');
}

function emptyFields() {
  return { title: '', project: '', created_at: '', finding: [], evidence: [], conclusion: [], next_steps: [], risks: [] };
}

function pushValue(target, field, inline) {
  if (inline.trim() === '') {
    if (LIST_FIELDS.has(field)) target[field].push('');
    return;
  }
  const pieces = inline.split(/[;；]/).map((piece) => piece.trim()).filter((piece) => piece !== '');
  for (const piece of pieces) {
    if (LIST_FIELDS.has(field)) target[field].push(piece);
    else target[field] = target[field] ? `${target[field]} ${piece}` : piece;
  }
}

/** PowerContext 可能把多行文本压成一行；先按已知字段名还原成行，再解析。 */
function restoreReflowedLines(rawText) {
  const lines = String(rawText).replace(/\r\n?/g, '\n').split('\n');
  if (lines.length > 2) return lines;
  return lines
    .join('\n')
    .split('\n')
    .flatMap((line) => line.split(new RegExp(`(?=(?:${KEY_NAME})\\s*:)`)))
    .map((line) => line.trim())
    .filter((line) => line !== '');
}

export function parseRecordBlock(rawText) {
  const fields = emptyFields();
  let activeList = null;
  let sawKeyLine = false;

  for (const line of restoreReflowedLines(rawText)) {
    const keyMatch = KEY_LINE_RE.exec(line);
    if (keyMatch) {
      sawKeyLine = true;
      const field = KEY_TO_FIELD.get(keyMatch[1]);
      const inline = keyMatch[2] ?? '';
      pushValue(fields, field, inline);
      activeList = LIST_FIELDS.has(field) && inline.trim() === '' ? field : null;
      continue;
    }
    const bulletMatch = BULLET_RE.exec(line);
    if (bulletMatch && activeList) {
      fields[activeList].push(bulletMatch[1].trim());
      continue;
    }
    if (activeList && !SEPARATOR_RE.test(line)) {
      fields[activeList].push(line);
      continue;
    }
  }

  for (const field of LIST_FIELDS) {
    fields[field] = fields[field].map((item) => item.trim()).filter((item) => item !== '');
  }
  return { fields, sawKeyLine };
}

/**
 * PowerContext 的 content 是文本块。按记录标记切块并保留服务返回顺序；
 * 认不出字段时只保留摘录，不编造清单条目。
 */
export function parseRecallContent(content) {
  const text = String(content ?? '').replace(/\r\n?/g, '\n');
  const envelope = /^BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n([\s\S]*?)\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1$/m.exec(text);
  if (envelope) {
    let parsed;
    try { parsed = JSON.parse(envelope[1]); } catch { return []; }
    if (!Array.isArray(parsed?.items)) return [];
    return parsed.items.flatMap((item) => {
      const match = /^\[HB:(hb-[0-9a-z]{10})\]\r?\n/.exec(item?.content ?? '');
      if (!match) return [];
      const rawText = item.content.slice(match[0].length);
      const { fields, sawKeyLine } = parseRecordBlock(rawText);
      return [{
        record_id: match[1], raw_text: rawText, excerpt: rawText.replace(/\s+/g, ' ').slice(0, 500),
        truncated: Boolean(item.truncated), structured: sawKeyLine, fields: { ...fields, record_id: match[1] },
      }];
    }).map((block, index) => ({ ...block, index }));
  }
  const blocks = [];
  let current = null;
  const MAX_BLOCK_LINES = 80;

  const close = () => {
    if (current) {
      const rawText = current.rawLines.join('\n').trim();
      const { fields, sawKeyLine } = parseRecordBlock(rawText);
      blocks.push({
        record_id: current.record_id,
        index: blocks.length,
        raw_text: rawText,
        excerpt: rawText.replace(/\s+/g, ' ').slice(0, 500),
        truncated: current.overflow,
        structured: sawKeyLine,
        fields: { ...fields, record_id: current.record_id },
      });
    }
    current = null;
  };

  for (const rawLine of text.split('\n')) {
    const markers = [...rawLine.matchAll(MARKER_RE)];
    if (markers.length > 0) {
      close();
      const first = markers[0];
      current = {
        record_id: first[1] ?? first[0],
        rawLines: [rawLine.replace(MARKER_RE, '').trim()].filter((line) => line !== ''),
        overflow: false,
      };
      continue;
    }
    if (!current) continue;
    if (SEPARATOR_RE.test(rawLine.trim())) {
      close();
      continue;
    }
    if (current.rawLines.length >= MAX_BLOCK_LINES) {
      current.overflow = true;
      continue;
    }
    current.rawLines.push(rawLine);
  }
  close();

  return blocks;
}

export function tokenize(value) {
  const text = String(value ?? '').toLowerCase();
  const tokens = new Set();
  for (const match of text.matchAll(/[a-z0-9][a-z0-9._+-]{1,}/g)) tokens.add(match[0]);
  for (const match of text.match(/[\u4e00-\u9fff]+/g) ?? []) {
    if (match.length === 1) tokens.add(match);
    for (let i = 0; i + 2 <= match.length; i += 1) tokens.add(match.slice(i, i + 2));
  }
  return [...tokens];
}

export function scoreBlock(block, queryTokens) {
  const haystack = `${block.fields.title} ${block.fields.finding.join(' ')} ${block.fields.evidence.join(' ')} ${block.fields.conclusion.join(' ')} ${block.fields.next_steps.join(' ')} ${block.fields.risks.join(' ')} ${block.raw_text}`
    .toLowerCase();
  const matched = [];
  for (const token of queryTokens) {
    let count = 0;
    let from = haystack.indexOf(token);
    while (from !== -1 && count < 10) {
      count += 1;
      from = haystack.indexOf(token, from + token.length);
    }
    if (count > 0) matched.push({ token, count });
  }
  matched.sort((a, b) => b.count - a.count || a.token.localeCompare(b.token));
  return {
    matched: matched.slice(0, 8),
    overlap: matched.length,
    rank: matched.length * 100 - block.index,
  };
}

export function attachReceipts(blocks, receipts) {
  const byId = new Map((receipts ?? []).map((receipt) => [receipt.record_id, receipt]));
  return blocks.map((block) => {
    const receipt = byId.get(block.record_id);
    return {
      ...block,
      receipt: receipt
        ? {
            entry_id: receipt.entry_id,
            saved_at: receipt.saved_at,
            title: receipt.title ?? '',
            scope_id: receipt.scope_id ?? '',
            source: receipt.source ?? '本机写入回执',
          }
        : null,
      has_receipt: Boolean(receipt),
    };
  });
}
