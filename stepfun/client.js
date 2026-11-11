export async function request(path, body) {
  const res = await fetch(`/api/${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

export function splitDocument(title, text) {
  const clean = text.replace(/\r\n/g, '\n').trim();
  const chunks = [];
  for (let start = 0; start < clean.length;) {
    const limit = Math.min(start + 550, clean.length);
    const boundary = clean.lastIndexOf(' ', limit);
    const end = limit < clean.length && boundary > start + 350 ? boundary : limit;
    const content = clean.slice(start, end).trim();
    if (content) chunks.push({ title, text: content, part: chunks.length + 1 });
    if (end === clean.length) break;
    start = end - 50;
  }
  return chunks;
}

export async function ingest(model, library, chunks, batchSize, progress = () => {}) {
  let embedMs = 0, writeMs = 0, refreshMs = 0;
  for (let i = 0; i < chunks.length; i += batchSize) {
    const batch = chunks.slice(i, i + batchSize);
    progress(`向量化 ${i + 1}–${i + batch.length} / ${chunks.length}`);
    const start = performance.now();
    const embeddings = await model.embed(batch.map(chunk => chunk.text));
    embedMs += performance.now() - start;
    const finalBatch = i + batch.length === chunks.length;
    progress(`写入 ${i + 1}–${i + batch.length} / ${chunks.length}${finalBatch ? ' 并刷新索引' : ''}`);
    const result = await request('ingest', { library, precision: model.dtype,
      finalBatch,
      chunks: batch.map((chunk, index) => ({ ...chunk, embedding: embeddings[index] })) });
    writeMs += result.writeMs;
    refreshMs += result.refreshMs ?? 0;
  }
  return { embedMs, writeMs, refreshMs };
}

export async function search(model, library, query) {
  const start = performance.now();
  const [embedding] = await model.embed([query]);
  const embedMs = performance.now() - start;
  const result = await request('search', { library, precision: model.dtype, query, embedding });
  return { ...result, embedMs };
}
