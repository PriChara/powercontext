import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { config } from './config.js';
import { slugifyProject } from './handover.js';

/**
 * 本机只存两类可重建的元数据：项目名→scope_id 的解析结果，以及每次写入 PowerContext 的回执。
 * 交接内容本身只存在 PowerContext，召回结果一律不落盘。
 */

function projectsFile() {
  return path.join(config.dataDir, 'projects.json');
}

function receiptsFile(slug) {
  return path.join(config.dataDir, 'records', `${slug}.jsonl`);
}

async function ensureDir(dir) {
  await mkdir(dir, { recursive: true });
}

async function readJson(file, fallback) {
  try {
    const parsed = JSON.parse(await readFile(file, 'utf8'));
    return Array.isArray(parsed) ? parsed : fallback;
  } catch {
    return fallback;
  }
}

async function writeJson(file, data) {
  await ensureDir(path.dirname(file));
  const tmp = `${file}.${process.pid}.tmp`;
  await writeFile(tmp, `${JSON.stringify(data, null, 2)}\n`, 'utf8');
  await rename(tmp, file);
}

export async function listProjects() {
  return readJson(projectsFile(), []);
}

export async function findProject(name) {
  const slug = slugifyProject(name);
  const projects = await listProjects();
  return projects.find((project) => project.slug === slug) ?? null;
}

export async function upsertProject({ name, scope_id: scopeId, idempotency_key: idempotencyKey, summary }) {
  const projects = await listProjects();
  const slug = slugifyProject(name);
  const now = new Date().toISOString();
  const existing = projects.find((project) => project.slug === slug);
  if (existing) {
    const changed = existing.scope_id !== scopeId;
    existing.name = name;
    existing.scope_id = scopeId;
    existing.idempotency_key = idempotencyKey ?? existing.idempotency_key;
    existing.summary = summary ?? existing.summary ?? '';
    existing.last_opened_at = now;
    if (changed) existing.scope_updated_at = now;
    existing.open_count = (existing.open_count ?? 0) + 1;
  } else {
    projects.unshift({
      slug,
      name,
      scope_id: scopeId,
      idempotency_key: idempotencyKey ?? '',
      summary: summary ?? '',
      created_at: now,
      last_opened_at: now,
      open_count: 1,
    });
  }
  projects.sort((a, b) => String(b.last_opened_at).localeCompare(String(a.last_opened_at)));
  await writeJson(projectsFile(), projects.slice(0, 50));
  return projects.find((project) => project.slug === slug);
}

export async function listReceipts(name) {
  const slug = slugifyProject(name);
  try {
    const text = await readFile(receiptsFile(slug), 'utf8');
    return text
      .split(/\r?\n/)
      .filter((line) => line.trim() !== '')
      .map((line) => {
        try {
          return JSON.parse(line);
        } catch {
          return null;
        }
      })
      .filter(Boolean);
  } catch (err) {
    if (err?.code === 'ENOENT') return [];
    throw err;
  }
}

export async function appendReceipt(name, receipt) {
  const slug = slugifyProject(name);
  const file = receiptsFile(slug);
  await ensureDir(path.dirname(file));
  await writeFile(file, `${JSON.stringify(receipt)}\n`, { flag: 'a', encoding: 'utf8' });
  return receipt;
}

export const storePaths = () => ({ projects: projectsFile(), records: path.join(config.dataDir, 'records') });
