import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);

function parseEnvFile(text) {
  const out = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"') && value.length > 1) ||
      (value.startsWith("'") && value.endsWith("'") && value.length > 1)
    ) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

function readDotEnv() {
  const file = path.join(ROOT, '.env');
  if (!existsSync(file)) return {};
  try {
    return parseEnvFile(readFileSync(file, 'utf8'));
  } catch {
    return {};
  }
}

const dotEnv = readDotEnv();

function env(name, fallback) {
  const raw = process.env[name] ?? dotEnv[name];
  if (raw === undefined || String(raw).trim() === '') return fallback;
  return String(raw).trim();
}

function intEnv(name, fallback) {
  const raw = env(name, String(fallback));
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`配置 ${name} 必须是正整数，当前值：${raw}`);
  }
  return parsed;
}

const host = env('HB_HOST', '127.0.0.1');
if (!LOOPBACK_HOSTS.has(host)) {
  throw new Error(
    `HB_HOST 只允许本机回环地址（127.0.0.1 / localhost / ::1），当前值：${host}。本工具按赛题要求不提供公网服务。`
  );
}

const powercontextUrl = env('POWERCONTEXT_URL', 'http://127.0.0.1:18379').replace(/\/+$/, '');
function assertLoopbackPowercontext(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error(`POWERCONTEXT_URL 无法解析（期望形如 http://127.0.0.1:18379），当前值：${rawUrl}`);
  }
  if (parsed.protocol !== 'http:') {
    throw new Error(`POWERCONTEXT_URL 必须是 http 地址，当前值：${rawUrl}`);
  }
  const hostname = parsed.hostname.replace(/^\[|\]$/g, '');
  if (!LOOPBACK_HOSTS.has(hostname)) {
    throw new Error(
      `POWERCONTEXT_URL 必须指向本机回环地址（127.0.0.1 / localhost / ::1），当前值：${rawUrl}`
    );
  }
}

assertLoopbackPowercontext(powercontextUrl);

export const config = {
  root: ROOT,
  publicDir: path.join(ROOT, 'public'),
  powercontextUrl,
  powercontextTimeoutMs: intEnv('POWERCONTEXT_TIMEOUT_MS', 12000),
  powercontextCommit: 'dab240063bfcfa7917898dc44da4baadb3747bd0',
  host,
  port: intEnv('HB_PORT', 8787),
  dataDir: path.resolve(ROOT, env('HB_DATA_DIR', '.data')),
  defaultMaxBytes: intEnv('HB_MAX_BYTES', 8000),
  maxBodyBytes: 512 * 1024,
};

export function describeConfig() {
  return {
    powercontext_url: config.powercontextUrl,
    powercontext_commit: config.powercontextCommit,
    powercontext_timeout_ms: config.powercontextTimeoutMs,
    listen: `http://${config.host}:${config.port}`,
    data_dir: config.dataDir,
    default_max_bytes: config.defaultMaxBytes,
  };
}
