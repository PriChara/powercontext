import './style.css';
import { loadModel } from './model.js';
import { ingest, request, search, splitDocument } from './client.js';
import { queries, sample } from './corpus.js';

const get = (id) => document.getElementById(id);
const status = (message) => { get('status').textContent = message; };
const controls = [...document.querySelectorAll('button, input, select')];
const number = (value) => value.toFixed(1);

async function run(operation) {
  const library = get('library').value.trim();
  if (!library) return status('请先填写知识库名称。');
  controls.forEach(control => { control.disabled = true; });
  try { await operation(library, get('mode').value); }
  catch (error) { status(error.message); }
  finally { controls.forEach(control => { control.disabled = false; }); }
}

async function selectModel(mode) {
  const model = await loadModel(mode, status);
  get('backend').textContent = `${model.device.toUpperCase()} · ${model.dtype === 'q8' ? 'INT8' : model.dtype.toUpperCase()}`;
  get('backend').title = model.fallback ?? '';
  return model;
}

async function add(library, mode, chunks) {
  if (!chunks.length) return status('没有可导入的文本。');
  get('results').replaceChildren();
  const model = await selectModel(mode);
  const times = await ingest(model, library, chunks, 8, status);
  const { count } = await request('count', { library, precision: model.dtype });
  status(`处理 ${chunks.length} 段 · 库内 ${count} 段 · 向量化 ${number(times.embedMs)} ms · 写入 ${number(times.writeMs)} ms · 索引刷新 ${number(times.refreshMs)} ms`);
}

get('import').onclick = () => run(async (library, mode) => {
  const files = [...get('files').files];
  if (files.length > 32 || files.some(file => file.size > 512 * 1024)) throw new Error('一次最多 32 个文件，每个不超过 512 KiB。');
  const chunks = [];
  for (const file of files) chunks.push(...splitDocument(file.name.slice(0, 200), await file.text()));
  await add(library, mode, chunks);
});
get('sample').onclick = () => run((library, mode) => add(library, mode, sample));
for (const field of ['library', 'mode']) {
  get(field).addEventListener('input', () => {
    get('results').replaceChildren();
    get('backend').textContent = '推理后端待确认';
    status('已切换。搜索或导入时会打开对应的知识库。');
  });
}

function renderHits(hits) {
  const fragment = document.createDocumentFragment();
  for (const [index, hit] of hits.entries()) {
    const item = document.createElement('article');
    const title = document.createElement('h3');
    title.textContent = `${index + 1}. ${hit.title ?? '文档'} · 第 ${hit.part ?? 1} 段`;
    const text = document.createElement('p');
    text.textContent = hit.text;
    item.append(title, text);
    fragment.append(item);
  }
  get('results').replaceChildren(fragment);
}

get('search').onsubmit = (event) => {
  event.preventDefault();
  return run(async (library, mode) => {
    const query = get('query').value.trim();
    if (!query) return status('请输入查询。');
    get('results').replaceChildren();
    const model = await selectModel(mode);
    const start = performance.now();
    const result = await search(model, library, query);
    renderHits(result.hits);
    status(`${result.hits.length} 条 · 向量化 ${number(result.embedMs)} ms · 数据库 ${number(result.dbMs)} ms · 总计 ${number(performance.now() - start)} ms`);
  });
};

get('benchmark').onclick = () => run(async () => {
  const output = { browser: navigator.userAgent, documents: sample.length, queries: queries.length, trials: 3, runs: [] };
  const library = `benchmark-${crypto.randomUUID()}`;
  for (const mode of ['wasm-fp32', 'wasm-q8', 'webgpu-fp16']) {
    status(`测试 ${mode}`);
    try {
      if (mode === 'webgpu-fp16' && !(await navigator.gpu?.requestAdapter())?.features.has('shader-f16')) throw new Error('WebGPU FP16 不可用');
      const model = await selectModel(mode);
      const entry = { mode, loadMs: model.loadMs, weightFetchMs: model.weightFetchMs, indexing: [], searches: [] };
      const vectors = sample.map(item => item.text);
      for (let trial = 0; trial < output.trials; trial++) {
        for (const batchSize of trial % 2 ? [8, 1] : [1, 8]) {
          const start = performance.now();
          for (let i = 0; i < vectors.length; i += batchSize) await model.embed(vectors.slice(i, i + batchSize));
          entry.indexing.push({ batchSize, embedMs: performance.now() - start });
        }
      }
      entry.import = await ingest(model, library, sample, 8);
      for (let trial = 0; trial < output.trials; trial++) {
        for (const [query, relevant] of queries) {
          const start = performance.now();
          const result = await search(model, library, query);
          renderHits(result.hits);
          const totalMs = performance.now() - start;
          const rank = result.hits.findIndex(hit => hit.title === relevant) + 1;
          entry.searches.push({ trial, query, relevant, rank, embedMs: result.embedMs, dbMs: result.dbMs, totalMs });
        }
      }
      const ranks = entry.searches.filter(result => result.trial === 0).map(result => result.rank);
      entry.recallAt5 = ranks.filter(Boolean).length / ranks.length;
      entry.ndcgAt5 = ranks.reduce((sum, rank) => sum + (rank ? 1 / Math.log2(rank + 1) : 0), 0) / ranks.length;
      output.runs.push(entry);
    } catch (error) { output.runs.push({ mode, error: error.message }); }
  }
  const download = get('download');
  if (download.href) URL.revokeObjectURL(download.href);
  download.href = URL.createObjectURL(new Blob([JSON.stringify(output, null, 2)], { type: 'application/json' }));
  download.hidden = false;
  status(`完成 ${output.runs.filter(result => !result.error).length} / ${output.runs.length} 种配置。下载记录查看指标或失败原因。`);
});
