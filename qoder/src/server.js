import http from 'node:http';
import path from 'node:path';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { config, describeConfig } from './config.js';
import { PowerContextError, createScope, prepareContext, remember } from './powercontext.js';
import {
  ValidationError,
  attachReceipts,
  idempotencyKeyFor,
  normalizeHandover,
  parseRecallContent,
  renderReason,
  renderRecordText,
  scoreBlock,
  slugifyProject,
  tokenize,
} from './handover.js';
import { buildChecklist } from './checklist.js';
import { appendReceipt, findProject, listProjects, listReceipts, upsertProject } from './store.js';

class HttpError extends Error {
  constructor(status, message, kind = 'bad_request') {
    super(message);
    this.status = status;
    this.kind = kind;
  }
}

const SECURITY_HEADERS = {
  'content-type': 'application/json; charset=utf-8',
  'x-content-type-options': 'nosniff',
  'cache-control': 'no-store',
  'content-security-policy':
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'",
  'cross-origin-opener-policy': 'same-origin',
  'x-frame-options': 'DENY',
};

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.txt': 'text/plain; charset=utf-8',
  '.ico': 'image/x-icon',
};

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, { ...SECURITY_HEADERS, 'content-length': Buffer.byteLength(body) });
  res.end(body);
}

function sendError(res, error) {
  if (error instanceof ValidationError || error instanceof HttpError) {
    const status = error instanceof HttpError ? error.status : 400;
    sendJson(res, status, {
      error: { kind: error.kind ?? 'validation', message: error.message, fields: error.fields ?? null },
    });
    return;
  }
  if (error instanceof PowerContextError) {
    sendJson(res, 502, {
      error: {
        kind: 'powercontext',
        message: error.message,
        http_status: error.httpStatus ?? null,
        endpoint: error.apiPath ?? null,
        detail: error.bodySnippet ?? null,
        powercontext_url: config.powercontextUrl,
      },
    });
    return;
  }
  console.error('[handover-book] 未预期的错误：', error);
  sendJson(res, 500, { error: { kind: 'internal', message: `应用内部错误：${error?.message ?? error}` } });
}

function clientLabel(req) {
  const socket = req.socket?.remoteAddress ?? '未知';
  return `${req.method} ${req.url} from ${socket}`;
}

function assertLocalRequest(req) {
  const host = String(req.headers.host ?? '');
  const hostname = host.replace(/:\d+$/, '').replace(/^\[|\]$/g, '');
  if (!['127.0.0.1', 'localhost', '::1'].includes(hostname)) {
    throw new HttpError(403, `只接受本机地址访问，收到 Host: ${host || '(空)'}`, 'forbidden_host');
  }
  const origin = req.headers.origin;
  if (typeof origin === 'string' && origin !== '') {
    let originHost;
    try {
      originHost = new URL(origin).host.replace(/:\d+$/, '');
    } catch {
      throw new HttpError(400, `Origin 无法解析：${origin}`, 'bad_origin');
    }
    if (!['127.0.0.1', 'localhost', '[::1]', '::1'].includes(originHost)) {
      throw new HttpError(403, `只接受本机页面发起的请求，收到 Origin: ${origin}`, 'forbidden_origin');
    }
  }
}

async function readJsonBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > config.maxBodyBytes) throw new HttpError(413, '请求体过大（上限 512 KB）', 'too_large');
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString('utf8');
  if (text.trim() === '') return {};
  try {
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new HttpError(400, '请求体必须是 JSON 对象', 'bad_json');
    }
    return parsed;
  } catch (err) {
    if (err instanceof HttpError) throw err;
    throw new HttpError(400, `请求体不是合法 JSON：${err.message}`, 'bad_json');
  }
}

function requireName(input) {
  const name = String(input?.project ?? '').replace(/[\u0000-\u001F]/g, '').trim();
  if (name === '') throw new ValidationError('请填写课题名称（同一个课题名会回到同一个 PowerContext scope）。');
  if (name.length > 120) throw new ValidationError('课题名称过长（上限 120 字）。');
  return name;
}

async function resolveScope(name, summary) {
  const cached = await findProject(name);
  const idempotencyKey = idempotencyKeyFor(name);
  const title = cached?.name ?? name;
  const scopeSummary =
    cached?.summary || String(summary ?? '').trim() || `课题交接簿：${name} 的交接记录（发现、证据、下一步）。内容由组员在页面手工填写。`;
  const created = await createScope({ title, summary: scopeSummary, idempotencyKey });
  const project = await upsertProject({
    name: title,
    scope_id: created.scopeId,
    idempotency_key: idempotencyKey,
    summary: scopeSummary,
  });
  return {
    name,
    slug: project.slug,
    scope_id: created.scopeId,
    idempotency_key: idempotencyKey,
    cached_scope_id: cached?.scope_id ?? null,
    reused_local_cache: cached?.scope_id === created.scopeId,
    elapsed_ms: created.elapsedMs,
    http_status: created.httpStatus,
    request_summary: created.requestBody,
  };
}

function clampMaxBytes(value) {
  if (value === undefined || value === null || value === '') return config.defaultMaxBytes;
  const parsed = Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed)) throw new ValidationError('max_bytes 必须是整数');
  if (parsed < 512 || parsed > 20000) throw new ValidationError('max_bytes 需要在 512 到 20000 之间');
  return parsed;
}

const STARTED_AT = new Date().toISOString();

const handlers = {
  'GET /api/health': async (req, res) => {
    sendJson(res, 200, {
      ok: true,
      app: 'handover-book',
      node: process.version,
      started_at: STARTED_AT,
      powercontext: describeConfig(),
      endpoints: Object.keys(handlers),
    });
  },

  'GET /api/projects': async (req, res) => {
    sendJson(res, 200, { projects: await listProjects(), powercontext_url: config.powercontextUrl });
  },

  'GET /api/records': async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    const name = url.searchParams.get('project')?.trim() ?? '';
    if (name === '') throw new ValidationError('缺少 project 参数');
    const receipts = await listReceipts(name);
    sendJson(res, 200, {
      project: name,
      slug: slugifyProject(name),
      receipts,
      note: '这是本机写入回执（证明调用过 /v1/memory/remember），不是召回结果；召回内容一律来自 PowerContext。',
    });
  },

  'GET /api/examples': async (req, res) => {
    const file = path.join(config.root, 'examples', 'seed.json');
    const seed = JSON.parse(await readFile(file, 'utf8'));
    sendJson(res, 200, { ...seed, powercontext_url: config.powercontextUrl });
  },

  'POST /api/scope/open': async (req, res, body) => {
    const name = requireName(body);
    const scope = await resolveScope(name, body.summary);
    sendJson(res, 200, {
      ...scope,
      powercontext: { url: config.powercontextUrl, commit: config.powercontextCommit, endpoint: 'POST /v1/scopes' },
    });
  },

  'POST /api/handover/save': async (req, res, body) => {
    const name = requireName(body);
    const scope = await resolveScope(name, body.summary);
    const record = normalizeHandover({ ...body, project: name });
    const text = renderRecordText(record);
    const reason = renderReason(record);
    const saved = await remember({ scopeId: scope.scope_id, kind: 'decision', text, reason });
    const receipt = {
      record_id: record.record_id,
      project: name,
      scope_id: scope.scope_id,
      entry_id: saved.entryId,
      kind: 'decision',
      title: record.title,
      saved_at: record.created_at,
      text_bytes: Buffer.byteLength(text, 'utf8'),
      text_sha256: createHash('sha256').update(text).digest('hex').slice(0, 16),
      source: '本机写入回执',
    };
    await appendReceipt(name, receipt);
    sendJson(res, 200, {
      record,
      receipt,
      sent: { kind: 'decision', text, reason, scope_id: scope.scope_id },
      entry: saved.entry,
      powercontext: { endpoint: 'POST /v1/memory/remember', elapsed_ms: saved.elapsedMs, http_status: saved.httpStatus },
      scope,
    });
  },

  'POST /api/recall': async (req, res, body) => {
    const name = requireName(body);
    const query = String(body.query ?? '').replace(/[\u0000-\u001F]/g, ' ').trim();
    if (query === '') throw new ValidationError('请输入当前问题，召回需要它作为查询词。');
    if (query.length > 500) throw new ValidationError('当前问题过长（上限 500 字）。');
    const maxBytes = clampMaxBytes(body.max_bytes);
    const scope = await resolveScope(name, body.summary);
    const recalled = await prepareContext({ scopeId: scope.scope_id, query, maxBytes });
    const receipts = await listReceipts(name);
    const tokens = tokenize(query);
    const scored = attachReceipts(parseRecallContent(recalled.content), receipts).map((block) => ({
      ...block,
      score: scoreBlock(block, tokens),
    }));
    const checklist = buildChecklist({
      project: name,
      query,
      blocks: scored,
      status: recalled.status,
      maxBytes,
      recalledAt: new Date().toISOString(),
      contentLength: Buffer.byteLength(recalled.content, 'utf8'),
    });
    sendJson(res, 200, {
      project: name,
      slug: scope.slug,
      scope_id: scope.scope_id,
      query,
      max_bytes: maxBytes,
      tokens,
      recall: {
        status: recalled.status,
        content: recalled.content,
        content_bytes: Buffer.byteLength(recalled.content, 'utf8'),
        elapsed_ms: recalled.elapsedMs,
        http_status: recalled.httpStatus,
        endpoint: 'POST /v1/context/prepare',
        request_summary: recalled.requestBody,
      },
      blocks: scored,
      checklist,
      provenance: {
        receipt_total: receipts.length,
        recalled_record_ids: scored.map((block) => block.record_id),
        recalled_without_local_receipt: scored.filter((block) => !block.has_receipt).map((block) => block.record_id),
      },
      powercontext: { url: config.powercontextUrl, commit: config.powercontextCommit },
    });
  },
};

async function serveStatic(pathname, res) {
  const rel = pathname === '/' ? 'index.html' : decodeURIComponent(pathname.replace(/^\/+/, ''));
  const abs = path.resolve(config.publicDir, rel);
  const inside = abs === config.publicDir || abs.startsWith(config.publicDir + path.sep);
  if (!inside) throw new HttpError(403, '禁止访问应用目录之外的文件', 'forbidden_path');
  const ext = path.extname(abs).toLowerCase();
  if (!MIME[ext]) throw new HttpError(404, '未找到该文件', 'not_found');
  let body;
  try {
    body = await readFile(abs);
  } catch (err) {
    if (err?.code === 'ENOENT') throw new HttpError(404, `未找到 ${rel}`, 'not_found');
    throw err;
  }
  res.writeHead(200, {
    'content-type': MIME[ext],
    'content-length': body.length,
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
    'content-security-policy': SECURITY_HEADERS['content-security-policy'],
  });
  res.end(body);
}

async function route(req, res) {
  const url = new URL(req.url, `http://${req.headers.host ?? '127.0.0.1'}`);
  const key = `${req.method} ${url.pathname}`;
  const handler = handlers[key];
  if (handler) {
    // 先把请求体读干净再判断，避免半途报错时连接被直接断开、前端拿不到错误信息。
    const body = req.method === 'POST' ? await readJsonBody(req) : {};
    if (req.method === 'POST') {
      const type = String(req.headers['content-type'] ?? '');
      if (!type.includes('application/json')) throw new HttpError(415, '接口只接受 application/json', 'bad_content_type');
    }
    await handler(req, res, body);
    return;
  }
  if (req.method === 'GET' && !url.pathname.startsWith('/api/')) {
    await serveStatic(url.pathname, res);
    return;
  }
  throw new HttpError(404, `未知接口 ${key}`, 'not_found');
}

export function createAppServer() {
  return http.createServer((req, res) => {
    const started = Date.now();
    res.on('finish', () => {
      console.log(`[handover-book] ${clientLabel(req)} → ${res.statusCode} (${Date.now() - started}ms)`);
    });
    Promise.resolve()
      .then(async () => {
        assertLocalRequest(req);
        await route(req, res);
      })
      .catch((err) => sendError(res, err));
  });
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isMain) {
  const server = createAppServer();
  server.listen(config.port, config.host, () => {
    console.log(`[handover-book] 课题交接簿已启动：http://${config.host}:${config.port}`);
    console.log(`[handover-book] PowerContext：${config.powercontextUrl}（源码版本 ${config.powercontextCommit}）`);
    console.log(`[handover-book] 本机元数据目录：${config.dataDir}`);
  });
  server.on('error', (err) => {
    if (err.code === 'EADDRINUSE') {
      console.error(`[handover-book] 端口 ${config.port} 已被占用，请用 HB_PORT 换一个端口。`);
      process.exit(1);
    }
    console.error('[handover-book] 服务错误：', err);
    process.exit(1);
  });
  const stop = (signal) => {
    console.log(`[handover-book] 收到 ${signal}，正在关闭。`);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1500).unref();
  };
  process.on('SIGINT', () => stop('SIGINT'));
  process.on('SIGTERM', () => stop('SIGTERM'));
}
