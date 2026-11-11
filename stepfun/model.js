const MODEL = 'Xenova/all-MiniLM-L6-v2';
const REVISION = '751bff37182d3f1213fa05d7196b954e230abad9';
const wasm = { device: 'wasm', dtype: 'q8' };
const gpu = { device: 'webgpu', dtype: 'fp16' };
const modes = { 'wasm-fp32': { device: 'wasm', dtype: 'fp32' }, 'wasm-q8': wasm, 'webgpu-fp16': gpu };
const models = new Map();
let runtime;

async function open(config, report) {
  const key = `${config.device}-${config.dtype}`;
  if (!models.has(key)) {
    models.set(key, (async () => {
      const started = performance.now();
      runtime ??= import('@huggingface/transformers');
      const { pipeline, env } = await runtime;
      const localOnly = import.meta.env.VITE_LOCAL_MODELS === '1';
      env.allowLocalModels = localOnly;
      env.allowRemoteModels = !localOnly;
      env.localModelPath = '/models/';
      env.useBrowserCache = true;
      env.backends.onnx.wasm.numThreads = 1;
      const modelFetchStart = performance.now();
      const extractor = await pipeline('feature-extraction', MODEL, {
        ...config, revision: REVISION,
        progress_callback: ({ status, file, progress }) => {
          if (status === 'progress') report(`加载 ${file} · ${Math.floor(progress)}%`);
        },
      });
      async function embed(texts) {
        const tensor = await extractor(texts, { pooling: 'mean', normalize: true });
        try { return tensor.tolist(); } finally { tensor.dispose(); }
      }
      try { await embed(['warmup']); } catch (error) { await extractor.dispose(); throw error; }
      const weights = performance.getEntriesByType('resource').filter(entry =>
        entry.startTime >= modelFetchStart && new URL(entry.name).pathname.endsWith('.onnx') && entry.responseStart > 0);
      const weightFetchMs = weights.length ? weights.reduce((sum, entry) => sum + entry.duration, 0) : null;
      return { ...config, embed, loadMs: performance.now() - started, weightFetchMs };
    })().catch((error) => { models.delete(key); throw error; }));
  }
  return models.get(key);
}

export async function loadModel(mode, report = () => {}) {
  if (modes[mode]) return open(modes[mode], report);
  let reason = '浏览器没有可用的 WebGPU 适配器';
  try {
    const adapter = await navigator.gpu?.requestAdapter();
    if (adapter?.features.has('shader-f16')) return await open(gpu, report);
    if (adapter) reason = 'WebGPU 适配器缺少 shader-f16';
  } catch (error) {
    reason = `WebGPU 初始化失败：${error.message}`;
  }
  report(`${reason}，切换至 WASM`);
  return { ...await open(wasm, report), fallback: reason };
}
