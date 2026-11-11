import { config } from './config.js';

export class PowerContextError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'PowerContextError';
    this.kind = details.kind ?? 'unavailable';
    this.method = details.method;
    this.apiPath = details.apiPath;
    this.httpStatus = details.httpStatus;
    this.bodySnippet = details.bodySnippet;
  }
}

export function snippet(text, max = 600) {
  if (text === undefined || text === null) return '';
  const flat = String(text).replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

function describeFetchError(err) {
  if (err?.name === 'TimeoutError' || err?.name === 'AbortError') return '请求超时';
  const code = err?.cause?.code ?? err?.code;
  if (code === 'ECONNREFUSED') return '连接被拒绝（服务未启动或端口不对）';
  if (code) return code;
  return err?.message ?? String(err);
}

/** 浅层遍历对象，取第一个匹配的非空标量，容忍 {data:{...}} 之类包装。 */
export function dig(object, names, maxDepth = 3) {
  const queue = [[object, 0]];
  const visited = new Set();
  while (queue.length > 0) {
    const [current, depth] = queue.shift();
    if (!current || typeof current !== 'object') continue;
    if (visited.has(current)) continue;
    visited.add(current);
    for (const name of names) {
      const value = current[name];
      if (typeof value === 'string' && value.trim() !== '') return value.trim();
      if (typeof value === 'number' && Number.isFinite(value)) return String(value);
    }
    if (depth < maxDepth) {
      for (const value of Object.values(current)) {
        if (value && typeof value === 'object') queue.push([value, depth + 1]);
      }
    }
  }
  return undefined;
}

function toText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value
      .map((item) => (typeof item === 'string' ? item : dig(item, ['text', 'content', 'body', 'value']) ?? ''))
      .filter((item) => item.trim() !== '')
      .join('\n\n');
  }
  if (value && typeof value === 'object') return dig(value, ['text', 'content', 'body', 'value']) ?? '';
  return '';
}

async function callApi(method, apiPath, payload) {
  const url = `${config.powercontextUrl}${apiPath}`;
  const startedAt = Date.now();
  let response;
  let raw = '';

  try {
    response = await fetch(url, {
      method,
      headers: { accept: 'application/json', 'content-type': 'application/json' },
      body: payload === undefined ? undefined : JSON.stringify(payload),
      signal: AbortSignal.timeout(config.powercontextTimeoutMs),
      redirect: 'error',
    });
    raw = await response.text();
  } catch (err) {
    throw new PowerContextError(
      `无法访问 PowerContext：${method} ${url} 失败（${describeFetchError(err)}）。` +
        `请确认服务已在本机监听，且环境变量 POWERCONTEXT_URL=${config.powercontextUrl} 指向它。`,
      { kind: 'unavailable', method, apiPath, bodySnippet: snippet(raw) }
    );
  }

  const elapsedMs = Date.now() - startedAt;
  let json;
  try {
    json = raw.trim() === '' ? null : JSON.parse(raw);
  } catch {
    json = undefined;
  }

  if (!response.ok) {
    const detail = json !== undefined ? snippet(JSON.stringify(json)) : snippet(raw);
    throw new PowerContextError(
      `PowerContext 返回错误：${method} ${apiPath} → HTTP ${response.status}${detail ? ` · ${detail}` : ''}`,
      { kind: 'http_error', method, apiPath, httpStatus: response.status, bodySnippet: detail }
    );
  }
  if (json === undefined) {
    throw new PowerContextError(
      `PowerContext 响应不是 JSON：${method} ${apiPath} → HTTP ${response.status}（${snippet(raw, 200)}）`,
      { kind: 'bad_response', method, apiPath, httpStatus: response.status, bodySnippet: snippet(raw) }
    );
  }

  return { json, raw, httpStatus: response.status, elapsedMs };
}

/** 创建或复用课题 scope：idempotency_key 由项目名稳定派生，所以同名项目回到同一课题。 */
export async function createScope({ title, summary, idempotencyKey }) {
  const { json, raw, httpStatus, elapsedMs } = await callApi('POST', '/v1/scopes', {
    title,
    summary,
    idempotency_key: idempotencyKey,
  });
  const scopeId = dig(json, ['scope_id', 'scopeId', 'id']);
  if (!scopeId) {
    throw new PowerContextError(`创建课题失败：响应中没有 scope_id（${snippet(raw)}）`, {
      kind: 'bad_response',
      method: 'POST',
      apiPath: '/v1/scopes',
      httpStatus,
      bodySnippet: snippet(raw),
    });
  }
  return { scopeId, json, raw, httpStatus, elapsedMs, requestBody: { title, summary, idempotency_key: idempotencyKey } };
}

export async function remember({ scopeId, kind, text, reason }) {
  const requestBody = { scope_id: scopeId, kind, text, reason };
  const { json, raw, httpStatus, elapsedMs } = await callApi('POST', '/v1/memory/remember', requestBody);
  const entry =
    (json && typeof json === 'object' && typeof json.entry === 'object' && json.entry) ||
    (json && typeof json === 'object' && typeof json.memory === 'object' && json.memory) ||
    (json && typeof json === 'object' && typeof json.data === 'object' && json.data !== null && json.data.entry) ||
    json;
  const entryId = dig(entry, ['id', 'entry_id', 'entryId', 'memory_id', 'uuid']);
  if (!entryId) {
    throw new PowerContextError(`保存交接失败：响应中没有可识别的 entry（${snippet(raw)}）`, {
      kind: 'bad_response',
      method: 'POST',
      apiPath: '/v1/memory/remember',
      httpStatus,
      bodySnippet: snippet(raw),
    });
  }
  return { entry, entryId, json, raw, httpStatus, elapsedMs, requestBody };
}

export async function prepareContext({ scopeId, query, maxBytes }) {
  const requestBody = { scope_id: scopeId, query, max_bytes: maxBytes };
  const { json, raw, httpStatus, elapsedMs } = await callApi('POST', '/v1/context/prepare', requestBody);
  const status = dig(json, ['status', 'state']);
  const contentValue =
    json && typeof json === 'object'
      ? (json.content ?? json.data?.content ?? json.context ?? json.result?.content)
      : undefined;
  const content = toText(contentValue);

  if (status === undefined && content === '') {
    throw new PowerContextError(`召回失败：响应里既没有 status 也没有 content（${snippet(raw)}）`, {
      kind: 'bad_response',
      method: 'POST',
      apiPath: '/v1/context/prepare',
      httpStatus,
      bodySnippet: snippet(raw),
    });
  }

  return {
    status: status ?? '(响应未提供 status)',
    content,
    json,
    raw,
    httpStatus,
    elapsedMs,
    requestBody,
  };
}
