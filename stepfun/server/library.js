import { createHash } from 'node:crypto';
import { resolve } from 'node:path';

const DIMENSION = 384;
const PRECISIONS = new Set(['fp32', 'fp16', 'q8']);
const digest = (value) => createHash('sha256').update(value).digest('hex');

function check(condition, message) {
  if (!condition) throw Object.assign(new Error(message), { status: 400 });
}

function vector(value) {
  check(Array.isArray(value) && value.length === DIMENSION && value.every(Number.isFinite), '需要 384 维有限数值向量');
  return value;
}

async function readBody(req) {
  check(/^application\/json(?:;|$)/i.test(req.headers['content-type'] ?? ''), '需要 JSON 请求');
  const chunks = [];
  let bytes = 0;
  for await (const chunk of req) {
    bytes += chunk.length;
    check(bytes <= 1024 * 1024, '请求超过 1 MiB');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks)); } catch { check(false, 'JSON 格式错误'); }
}

async function connect() {
  const sdk = await import('seekdb');
  const database = process.env.SEEKDB_DATABASE || 'test';
  const options = process.env.SEEKDB_HOST ? {
    host: process.env.SEEKDB_HOST, port: Number(process.env.SEEKDB_PORT || 2881),
    user: process.env.SEEKDB_USER || 'root', password: process.env.SEEKDB_PASSWORD || '', database,
  } : { path: resolve(process.env.SEEKDB_PATH || '.seekdb'), database };
  const client = new sdk.SeekdbClient(options);
  const schema = new sdk.Schema()
    .createIndex(new sdk.FulltextIndexConfig('beng', { min_token_size: 2, max_token_size: 84 }))
    .createIndex(new sdk.VectorIndexConfig({ hnsw: { dimension: DIMENSION, distance: 'cosine' } }));
  return { client, schema };
}

export function libraryApi() {
  let connection;
  let searchQueue = Promise.resolve();
  const collections = new Map();

  async function collection(library, precision) {
    check(typeof library === 'string' && library.trim() && library.trim().length <= 80, '知识库名须为 1–80 字符');
    check(PRECISIONS.has(precision), '未知模型精度');
    const name = `sf_minilm_v1_${precision}_${digest(library.trim()).slice(0, 24)}`;
    connection ??= connect().catch((error) => { connection = undefined; throw error; });
    if (!collections.has(name)) {
      const opening = connection.then(({ client, schema }) =>
        client.getOrCreateCollection({ name, schema, embeddingFunction: null }));
      collections.set(name, opening);
      opening.catch(() => collections.delete(name));
    }
    return collections.get(name);
  }

  return async (req, res, next) => {
    const path = req.url?.split('?')[0];
    if (!['/ingest', '/search', '/count'].includes(path)) return next();
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'no-store');
    try {
      check(req.method === 'POST', '需要 POST 请求');
      const data = await readBody(req);
      check(data && typeof data === 'object', '缺少请求参数');
      const store = await collection(data.library, data.precision);
      let result;
      if (path === '/count') {
        result = { count: await store.count() };
      } else if (path === '/ingest') {
        check(Array.isArray(data.chunks) && data.chunks.length >= 1 && data.chunks.length <= 16, '每批需要 1–16 个段落');
        const unique = new Map();
        for (const { title, text, part, embedding } of data.chunks) {
          check(typeof title === 'string' && title.length > 0 && title.length <= 200, '文档标题须为 1–200 字符');
          check(typeof text === 'string' && text.trim() && text.length <= 1200, '段落须为 1–1200 字符');
          check(Number.isInteger(part) && part >= 1, '无效段号');
          const id = digest(JSON.stringify([title, part, text]));
          unique.set(id, { title, text, part, embedding: vector(embedding) });
        }
        const ids = [...unique.keys()];
        const rows = [...unique.values()];
        const started = performance.now();
        await store.upsert({ ids, documents: rows.map(r => r.text), embeddings: rows.map(r => r.embedding),
          metadatas: rows.map(({ title, part }) => ({ title, part })) });
        result = { written: ids.length, writeMs: performance.now() - started };
        if (data.finalBatch === true) {
          const refreshing = performance.now();
          await store.refresh_index();
          result.refreshMs = performance.now() - refreshing;
        }
      } else {
        const query = data.query;
        check(typeof query === 'string' && query.trim() && query.length <= 240, '查询须为 1–240 字符');
        const embedding = vector(data.embedding);
        // seekdb-js builds the hybrid query using a connection-scoped SQL variable.
        const searching = searchQueue.then(async () => {
          const started = performance.now();
          const response = await store.hybridSearch({
            query: { whereDocument: { $contains: query }, nResults: 20 },
            knn: { queryEmbeddings: [embedding], nResults: 20 },
            rank: { rrf: {} }, nResults: 5, include: ['documents', 'metadatas'],
          });
          return { response, dbMs: performance.now() - started };
        });
        searchQueue = searching.then(() => undefined, () => undefined);
        const { response, dbMs } = await searching;
        result = { dbMs, hits: (response.ids[0] || []).map((id, index) => ({
          id, text: response.documents?.[0]?.[index], ...response.metadatas?.[0]?.[index],
        })) };
      }
      res.end(JSON.stringify(result));
    } catch (error) {
      res.statusCode = error.status || 500;
      res.end(JSON.stringify({ error: error.status ? error.message : 'seekdb 请求失败。请检查终端日志和数据库配置。' }));
      if (!error.status) console.error(error);
    }
  };
}
