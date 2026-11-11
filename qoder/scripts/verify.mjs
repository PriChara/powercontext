#!/usr/bin/env node
// 端到端验证脚本：真实调用 PowerContext 与课题交接簿应用，打印并保存实际输出。
// 用法：
//   node scripts/verify.mjs                       # 默认 POWERCONTEXT_URL=http://127.0.0.1:18379
//   node scripts/verify.mjs --url http://127.0.0.1:18379 --port 18791
//   node scripts/verify.mjs --offline             # PowerContext 不可用时只跑应用侧检查
//   node scripts/verify.mjs --keep                # 保留临时数据目录并打印路径
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { get as httpGet } from 'node:http';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SERVER = path.join(ROOT, 'src', 'server.js');
const PINNED_COMMIT = 'dab240063bfcfa7917898dc44da4baadb3747bd0';

const transcript = [];
function out(line = '') {
  console.log(line);
  transcript.push(line);
}
function outJson(label, value, max = 1200) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const flat = text.length > max ? `${text.slice(0, max)}…(已截断，共 ${text.length} 字)` : text;
  out(`${label} ${flat}`);
}

const args = parseArgs(process.argv.slice(2));
const PC_URL = (args.url ?? process.env.POWERCONTEXT_URL ?? 'http://127.0.0.1:18379').replace(/\/+$/, '');
const APP_PORT = Number(args.port ?? process.env.HB_VERIFY_PORT ?? 18791);
const OFFLINE = Boolean(args.offline);
const KEEP = Boolean(args.keep);
const RUN_ID = `${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)}-${Math.random().toString(36).slice(2, 6)}`;

function parseArgs(list) {
  const result = {};
  for (let i = 0; i < list.length; i += 1) {
    const item = list[i];
    if (item === '--url') result.url = list[++i];
    else if (item === '--port') result.port = list[++i];
    else if (item === '--offline') result.offline = true;
    else if (item === '--keep') result.keep = true;
    else if (item.startsWith('-')) usageAndExit();
  }
  return result;
}

function usageAndExit() {
  out('用法：node scripts/verify.mjs [--url http://127.0.0.1:18379] [--port 18791] [--offline] [--keep]');
  process.exit(2);
}

const checks = [];
function check(id, name, assertion, detail) {
  if (assertion === 'skip') {
    checks.push({ id, name, status: 'SKIP', detail });
    out(`  ⏭  [${id}] ${name} —— 跳过：${detail}`);
    return;
  }
  checks.push({ id, name, status: assertion ? 'PASS' : 'FAIL', detail });
  out(`  ${assertion ? '✅' : '❌'} [${id}] ${name}${detail ? ` —— ${detail}` : ''}`);
}

function section(title) {
  out('');
  out(`── ${title} ${''.padStart(Math.max(2, 62 - title.length), '─')}`);
}

async function httpJson(url, options = {}, timeoutMs = 15000) {
  const started = Date.now();
  try {
    const res = await fetch(url, { ...options, signal: AbortSignal.timeout(timeoutMs), redirect: 'error' });
    const text = await res.text();
    let json = null;
    try {
      json = text === '' ? null : JSON.parse(text);
    } catch {
      json = null;
    }
    return { ok: res.ok, status: res.status, json, text, ms: Date.now() - started, headers: Object.fromEntries(res.headers) };
  } catch (err) {
    return {
      ok: false,
      status: 0,
      json: null,
      text: '',
      error: err?.cause?.code ?? err?.message ?? String(err),
      ms: Date.now() - started,
    };
  }
}

const pc = (apiPath, body) =>
  httpJson(`${PC_URL}${apiPath}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(body),
  });

const appBase = (port) => `http://127.0.0.1:${port}`;
const appGet = (port, apiPath) => httpJson(`${appBase(port)}${apiPath}`, { headers: { accept: 'application/json' } });
const appPost = (port, apiPath, body) =>
  httpJson(`${appBase(port)}${apiPath}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(body),
  });

function startApp({ port, dataDir, powercontextUrl }) {
  const logs = [];
  const child = spawn(process.execPath, [SERVER], {
    cwd: ROOT,
    env: {
      ...process.env,
      HB_HOST: '127.0.0.1',
      HB_PORT: String(port),
      HB_DATA_DIR: dataDir,
      POWERCONTEXT_URL: powercontextUrl,
      POWERCONTEXT_TIMEOUT_MS: '8000',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (chunk) => logs.push(String(chunk)));
  child.stderr.on('data', (chunk) => logs.push(String(chunk)));
  return {
    child,
    logs,
    async ready(timeoutMs = 10000) {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        const health = await appGet(port, '/api/health');
        if (health.status === 200) return health;
        if (child.exitCode !== null) throw new Error(`应用进程提前退出（code ${child.exitCode}）：${logs.join('')}`);
        await new Promise((resolve) => setTimeout(resolve, 150));
      }
      throw new Error(`应用未在 ${timeoutMs}ms 内监听 ${port}：${logs.join('')}`);
    },
    async stop() {
      if (child.exitCode !== null) return;
      child.kill('SIGTERM');
      await new Promise((resolve) => {
        const timer = setTimeout(() => {
          try {
            child.kill('SIGKILL');
          } catch {
            /* 已退出 */
          }
          resolve();
        }, 3000);
        child.on('exit', () => {
          clearTimeout(timer);
          resolve();
        });
      });
    },
  };
}

function digString(value, names, depth = 0) {
  if (!value || typeof value !== 'object' || depth > 3) return undefined;
  for (const name of names) {
    const candidate = value[name];
    if (typeof candidate === 'string' && candidate.trim() !== '') return candidate;
    if (typeof candidate === 'number' && Number.isFinite(candidate)) return String(candidate);
  }
  for (const child of Object.values(value)) {
    if (child && typeof child === 'object') {
      const found = digString(child, names, depth + 1);
      if (found !== undefined) return found;
    }
  }
  return undefined;
}

const MARKER_SEEN = /hb-[0-9a-z]{10}/g;

async function main() {
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), 'handover-book-verify-'));
  const appDataDir = path.join(tempRoot, 'app-data');
  mkdirSync(appDataDir, { recursive: true });
  const outputDir = path.join(ROOT, 'docs', 'test-output');
  mkdirSync(outputDir, { recursive: true });
  const outputFile = path.join(outputDir, `verify-${RUN_ID}.txt`);

  const projectA = `验证课题A-${RUN_ID}`;
  const projectB = `验证课题B-${RUN_ID}`;
  const projectEmpty = `验证课题空-${RUN_ID}`;
  const kwA = `kwa${RUN_ID.slice(-6)}`;
  const kwB = `kwb${RUN_ID.slice(-6)}`;

  let pcAvailable = !OFFLINE;
  let app = null;

  const finish = async (exitCode) => {
    if (app) await app.stop();
    const passed = checks.filter((item) => item.status === 'PASS').length;
    const failed = checks.filter((item) => item.status === 'FAIL').length;
    const skipped = checks.filter((item) => item.status === 'SKIP').length;
    section('结果汇总');
    out(`PASS ${passed} · FAIL ${failed} · SKIP ${skipped} · 合计 ${checks.length}`);
    for (const item of checks) out(`  ${item.status.padEnd(4)} ${item.id} ${item.name}`);
    out('');
    out(`说明：以上全部是本机实际请求返回内容，未做任何人工修饰；FAIL 项请连同本文件一起反馈。`);
    if (KEEP) out(`临时数据目录已保留：${tempRoot}`);
    else {
      try {
        rmSync(tempRoot, { recursive: true, force: true });
      } catch {
        /* Windows 下偶发占用，忽略 */
      }
    }
    writeFileSync(outputFile, `${transcript.join('\n')}\n`, 'utf8');
    console.log(`\n本次验证输出已保存：${outputFile}`);
    process.exit(exitCode);
  };

  section('0. 运行环境');
  out(`时间(UTC)：${new Date().toISOString()}`);
  out(`Node 版本：${process.version}（要求 >= 22）`);
  out(`平台：${process.platform} ${os.arch()}`);
  out(`PowerContext 地址：${PC_URL}（约定的本机测试端口）`);
  out(`PowerContext 固定源码版本：${PINNED_COMMIT}`);
  out(`应用端口：${APP_PORT}　临时目录：${tempRoot}`);
  out(`运行编号：${RUN_ID}`);
  const nodeMajor = Number(process.version.slice(1).split('.')[0]);
  check('0.1', 'Node 版本满足 22+', nodeMajor >= 22, `当前 ${process.version}`);
  if (OFFLINE) {
    out('  已指定 --offline：跳过所有依赖 PowerContext 的检查，只验证应用自身行为与错误处理。');
  }

  let scopeA;
  let directEntryIds = [];
  let directMarkers = [];

  section('1. PowerContext 契约：创建课题 / 保存 / 召回（直接调用服务，不经过应用）');
  if (!pcAvailable) {
    for (const id of ['1.1', '1.2', '1.3', '1.4']) check(id, 'PowerContext 直接调用', 'skip', '--offline 模式');
  } else {
    const key = `verify:${RUN_ID}:direct`;
    const first = await pc('/v1/scopes', { title: `直接调用验证课题 ${RUN_ID}`, summary: '验证 /v1/scopes 返回 scope_id', idempotency_key: key });
    out(`POST /v1/scopes（第 1 次） → HTTP ${first.status} (${first.ms}ms)`);
    outJson('响应：', first.json ?? first.text ?? first.error);
    const scopeId1 = first.json ? digString(first.json, ['scope_id', 'scopeId', 'id']) : undefined;
    check('1.1', 'POST /v1/scopes 返回 scope_id', Boolean(scopeId1), scopeId1 ? `scope_id=${scopeId1}` : `未取到 scope_id（HTTP ${first.status}）`);

    const second = await pc('/v1/scopes', { title: `直接调用验证课题 ${RUN_ID}`, summary: '验证 /v1/scopes 返回 scope_id', idempotency_key: key });
    const scopeId2 = second.json ? digString(second.json, ['scope_id', 'scopeId', 'id']) : undefined;
    out(`POST /v1/scopes（第 2 次，同 idempotency_key） → HTTP ${second.status}，scope_id=${scopeId2 ?? '(未取到)'}`);
    check('1.2', '相同 idempotency_key 复用同一个 scope', Boolean(scopeId1 && scopeId1 === scopeId2), `${scopeId1} vs ${scopeId2}`);

    const probeScopeId = scopeId1 ?? scopeId2;
    const handovers = [
      { text: `[HB:hb-verify${RUN_ID.slice(-4)}aaaa]\n课题: 直接调用验证\n标题: 第一步：${kwA} 的偏差来自配置未落盘\n发现:\n- 使用 kwA 场景复现，偏差 12mm\n证据:\n- 日志行 41 显示 loaded from /tmp\n下一步:\n- 把配置目录改到持久卷\n- 用 ${kwA} 关键词回归一次`, reason: '验证 remember 返回 entry' },
      { text: `[HB:hb-verify${RUN_ID.slice(-4)}bbbb]\n课题: 直接调用验证\n标题: 第二步：${kwB} 采集缺帧\n发现:\n- 每 200 帧出现一次 ${kwB} 超时\n证据:\n- timeout 计数 18/3600\n下一步:\n- 提高串口波特率并记录计数`, reason: '验证第二条交接可被区分召回' },
    ];
    directMarkers = handovers.map((item) => /hb-[0-9a-z]{10}/.exec(item.text)[0]);

    for (const [index, item] of handovers.entries()) {
      const saved = await pc('/v1/memory/remember', { scope_id: probeScopeId, kind: 'decision', text: item.text, reason: item.reason });
      const entryId = saved.json ? digString(saved.json, ['id', 'entry_id', 'entryId', 'memory_id', 'uuid']) : undefined;
      out(`POST /v1/memory/remember（第 ${index + 1} 条） → HTTP ${saved.status} (${saved.ms}ms)，entry id=${entryId ?? '(未取到)'}`);
      if (index === 0) outJson('响应（截断）：', saved.json ?? saved.text ?? saved.error, 700);
      if (entryId) directEntryIds.push(entryId);
      check(`1.3.${index + 1}`, `保存第 ${index + 1} 条交接并返回 entry`, Boolean(entryId), entryId ? `entry ${entryId}` : `HTTP ${saved.status}：${String(saved.text ?? saved.error).slice(0, 160)}`);
    }
    check('1.3', '两条交接的 entry id 可区分', directEntryIds.length === 2 && directEntryIds[0] !== directEntryIds[1], directEntryIds.join(' vs '));

    const prepared = await pc('/v1/context/prepare', { scope_id: probeScopeId, query: `${kwA} 偏差 下一步`, max_bytes: 8000 });
    const status = prepared.json ? digString(prepared.json, ['status', 'state']) : undefined;
    const content = prepared.json ? digString(prepared.json, ['content', 'context', 'text']) ?? '' : '';
    out(`POST /v1/context/prepare → HTTP ${prepared.status} (${prepared.ms}ms)`);
    out(`  status=${status ?? '(无)'}　content 长度=${content.length} 字节`);
    outJson('  content（截断）：', content, 900);
    check('1.4', 'prepare 返回 status 且 content 含保存的标记', status !== undefined && content.includes(directMarkers[0]), `status=${status}；命中 ${content.includes(directMarkers[0]) ? 'marker ' + directMarkers[0] : '未命中（见上方原文）'}`);
  }

  section('2. 应用启动与首页');
  try {
    app = await startApp({ port: APP_PORT, dataDir: appDataDir, powercontextUrl: PC_URL });
    const health = await app.ready();
    out(`GET /api/health → HTTP ${health.status} (${health.ms}ms)`);
    outJson('响应：', health.json, 900);
    check('2.1', '应用在 127.0.0.1 启动并报告配置', health.status === 200 && health.json?.ok === true, `监听 ${health.json?.powercontext?.listen}，POWERCONTEXT_URL=${health.json?.powercontext?.powercontext_url}`);

    const page = await httpJson(`${appBase(APP_PORT)}/`);
    const asset = await httpJson(`${appBase(APP_PORT)}/app.js`);
    check('2.2', '首页与脚本同源提供且带 CSP', page.status === 200 && asset.status === 200 && /content-security-policy/i.test(JSON.stringify(page.headers)), `GET / → ${page.status}，GET /app.js → ${asset.status}`);

    const blocked = await new Promise((resolve, reject) => {
      httpGet(`http://127.0.0.1:${APP_PORT}/`, { headers: { host: 'evil.example' } }, (response) => {
        response.resume();
        resolve({ status: response.statusCode });
      }).on('error', reject);
    });
    check('2.3', '拒绝非本机 Host 的请求', blocked.status === 403, `Host: evil.example → HTTP ${blocked.status}`);
  } catch (err) {
    check('2.1', '应用启动', false, err.message);
    return finish(1);
  }

  if (pcAvailable) {
    section('3. 应用：进入课题 → 保存两条有区别的交接 → 召回生成清单');
    const open = await appPost(APP_PORT, '/api/scope/open', { project: projectA, summary: '验证脚本创建的课题（虚构数据）' });
    out(`POST /api/scope/open → HTTP ${open.status} (${open.ms}ms)`);
    outJson('响应：', open.json, 800);
    scopeA = open.json?.scope_id;
    check('3.1', '应用用项目名拿到 scope_id', open.status === 200 && Boolean(scopeA), `scope_id=${scopeA}`);

    const forms = [
      {
        title: `现象：重启后 ${kwA} 抓取偏差变大`,
        finding: `重启采集容器后，${kwA} 场景的抓取偏差从 2mm 变成 12mm，方向不稳定。`,
        evidence: `日志 loaded from /tmp/calib.yaml\n两次重启的 sha256 前 8 位不同：3f9c2a11 / 77ab0e55`,
        conclusion: '标定结果写在容器临时目录，重启即丢失。',
        next_steps: `把标定输出目录改到持久卷 /data/calib\n加载时校验 sha256，不匹配就报错退出\n用 ${kwA} 关键词跑 3 次重启回归`,
        risks: '不要把 yaml 提交进仓库',
      },
      {
        title: `现象：${kwB} 串口超时导致缺帧`,
        finding: `每 200 帧出现一次 ${kwB} 时间戳晚 60ms，轨迹出现台阶。`,
        evidence: 'timeout 计数 18/3600\n波特率提到 921600 后计数降到 1/3600',
        conclusion: '低波特率下高频上报排队超时。',
        next_steps: `统一改成 921600 并写明需要重新烧录固件\n时间戳差值超过 20ms 丢弃该帧并计数（含 ${kwB}）`,
        risks: '不要在旧固件上开高波特率',
      },
    ];

    const savedRecords = [];
    for (const [index, form] of forms.entries()) {
      const saved = await appPost(APP_PORT, '/api/handover/save', { project: projectA, ...form });
      const recordId = saved.json?.record?.record_id;
      const entryId = saved.json?.receipt?.entry_id;
      if (recordId) savedRecords.push({ recordId, entryId });
      out(`POST /api/handover/save（第 ${index + 1} 条） → HTTP ${saved.status}，record=${recordId ?? '(无)'}，entry=${entryId ?? '(无)'}`);
      if (index === 0) outJson('  发送的 text：', saved.json?.sent?.text ?? saved.json, 700);
      check(`3.2.${index + 1}`, `应用保存第 ${index + 1} 条交接`, saved.status === 200 && Boolean(recordId) && Boolean(entryId), `record ${recordId} / entry ${entryId}`);
    }
    check(
      '3.3',
      '两条保存记录互相可区分（record_id 与 entry_id 都不同）',
      savedRecords.length === 2 && savedRecords[0].recordId !== savedRecords[1].recordId && savedRecords[0].entryId !== savedRecords[1].entryId,
      savedRecords.map((item) => `${item.recordId}→${item.entryId}`).join('　')
    );

    const query = `${kwA} 偏差变大，下一步该做什么`;
    const recall1 = await appPost(APP_PORT, '/api/recall', { project: projectA, query, max_bytes: 8000 });
    out(`POST /api/recall（当前问题：“${query}”） → HTTP ${recall1.status} (${recall1.ms}ms)`);
    if (recall1.status === 200) {
      const body = recall1.json;
      out(`  recall.status=${body.recall.status}　content ${body.recall.content_bytes} 字节　解析片段 ${body.blocks.length} 段　清单条目 ${body.checklist.items.length} 条`);
      outJson('  清单（前 900 字）：', body.checklist.text, 900);
      for (const block of body.blocks) {
        out(`  片段 #${block.index + 1} ${block.record_id}「${block.fields.title}」命中词 ${block.score.matched.map((m) => `${m.token}×${m.count}`).join(',') || '(无)'}　本机回执=${block.has_receipt}`);
      }
      const hit = new Set(body.blocks.map((block) => block.record_id));
      check('3.4', '召回包含与当前问题相关的第一条记录', hit.has(savedRecords[0]?.recordId), `命中 ${[...hit].join(', ') || '(空)'}`);
      check(
        '3.5',
        '清单条目都能溯源到保存记录的 record_id',
        body.checklist.items.length > 0 && body.checklist.items.every((item) => item.source_record_ids.every((id) => hit.has(id))),
        `${body.checklist.items.length} 条，来源 ${[...new Set(body.checklist.items.flatMap((item) => item.source_record_ids))].join(', ')}`
      );
      check(
        '3.6',
        '清单文本包含当前问题、召回 status 与来源标记',
        body.checklist.text.includes(kwA) && body.checklist.text.includes(String(body.recall.status)) && [...hit].every((id) => body.checklist.text.includes(id)),
        `包含 query/status/record_id`
      );
      check(
        '3.7',
        '搜索词与原文对应（至少一条片段报告了命中词）',
        body.blocks.some((block) => block.score.matched.length > 0),
        body.blocks.map((block) => `${block.record_id}:${block.score.matched.length}`).join('　')
      );

      section('4. 刷新页面（同一应用进程内第二次召回）');
      const recall2 = await appPost(APP_PORT, '/api/recall', { project: projectA, query: `${kwB} 缺帧 怎么处理`, max_bytes: 8000 });
      out(`POST /api/recall（换问题） → HTTP ${recall2.status}`);
      if (recall2.status === 200) {
        const hit2 = new Set(recall2.json.blocks.map((block) => block.record_id));
        out(`  命中记录：${[...hit2].join(', ') || '(空)'}　清单条目 ${recall2.json.checklist.items.length} 条`);
        check('4.1', '换问题后召回相关的第二条记录', hit2.has(savedRecords[1]?.recordId), `命中 ${[...hit2].join(', ')}`);
      } else {
        check('4.1', '刷新页面后重新召回', false, `HTTP ${recall2.status}：${String(recall2.text ?? recall2.error).slice(0, 200)}`);
      }

      section('5. 重启应用后重新召回（模拟隔几天再接手）');
      const projectSnapshot = savedRecords.map((item) => item.recordId).join(', ');
      await app.stop();
      out('  已停止应用进程（临时数据目录保留，PowerContext 不受影响）');
      app = await startApp({ port: APP_PORT, dataDir: appDataDir, powercontextUrl: PC_URL });
      await app.ready();
      const reopened = await appPost(APP_PORT, '/api/scope/open', { project: projectA });
      out(`  重启后重新进入课题 → HTTP ${reopened.status}，scope_id=${reopened.json?.scope_id}`);
      check('5.1', '重启后用同一项目名回到同一 scope', reopened.status === 200 && reopened.json?.scope_id === scopeA, `${scopeA} → ${reopened.json?.scope_id}`);
      const recall3 = await Promise.all([kwA, kwB].map((keyword) => appPost(APP_PORT, '/api/recall', { project: projectA, query: keyword, max_bytes: 8000 })));
      if (recall3.every((result) => result.status === 200)) {
        const hit3 = new Set(recall3.flatMap((result) => result.json.blocks.map((block) => block.record_id)));
        out(`  重启后召回 → HTTP 200，命中 ${[...hit3].join(', ') || '(空)'}`);
        check('5.2', '重启后仍能召回到保存过的两条交接', savedRecords.every((item) => hit3.has(item.recordId)), `期望 ${projectSnapshot}`);
      } else {
        check('5.2', '重启后重新召回', false, `HTTP ${recall3.map((result) => result.status).join(', ')}`);
      }

      section('6. 课题隔离：打开不同课题不应看到前一个课题的记录');
      const openB = await appPost(APP_PORT, '/api/scope/open', { project: projectB });
      const saveB = await appPost(APP_PORT, '/api/handover/save', {
        project: projectB,
        title: `课题B 专属：${kwB}-only 索引顺序写反`,
        finding: `课题B 的执行计划显示全表扫描（标记 ${kwB}-only）。`,
        evidence: 'EXPLAIN key=NULL rows=412300',
        next_steps: '按 (user_id, created_at) 重建索引',
      });
      out(`POST /api/scope/open(课题B) → scope_id=${openB.json?.scope_id}`);
      out(`保存课题B 交接 → record=${saveB.json?.record?.record_id}`);
      check('6.1', '第二个课题拿到不同的 scope_id', Boolean(openB.json?.scope_id) && openB.json.scope_id !== scopeA, `${scopeA} ≠ ${openB.json?.scope_id}`);

      const recallB = await appPost(APP_PORT, '/api/recall', { project: projectB, query: `${kwA} 偏差`, max_bytes: 8000 });
      const leakToB = recallB.status === 200 ? recallB.json.blocks.filter((block) => savedRecords.some((item) => item.recordId === block.record_id)) : [];
      out(`在课题B 里用课题A 的关键词召回 → HTTP ${recallB.status}，片段 ${recallB.json?.blocks.length ?? 0} 段`);
      outJson('  课题B 召回 content（截断）：', recallB.json?.recall?.content ?? recallB.text, 500);
      check('6.2', '课题B 看不到课题A 的保存记录', recallB.status === 200 && leakToB.length === 0, leakToB.length ? `泄漏 ${leakToB.map((b) => b.record_id).join(', ')}` : '未发现跨课题片段');

      const recallA = await appPost(APP_PORT, '/api/recall', { project: projectA, query: `${kwB}-only 索引`, max_bytes: 8000 });
      const leakToA = recallA.status === 200 ? recallA.json.blocks.filter((block) => block.record_id === saveB.json?.record?.record_id) : [];
      check('6.3', '课题A 也看不到课题B 的保存记录', recallA.status === 200 && leakToA.length === 0, leakToA.length ? `泄漏 ${leakToA.map((b) => b.record_id).join(', ')}` : '未发现跨课题片段');

      section('7. 无结果：全新课题（没有任何保存）');
      const emptyRecall = await appPost(APP_PORT, '/api/recall', { project: projectEmpty, query: `与任何保存都无关的词 ${kwA}-nothing`, max_bytes: 8000 });
      out(`POST /api/recall（空课题） → HTTP ${emptyRecall.status}`);
      if (emptyRecall.status === 200) {
        out(`  status=${emptyRecall.json.recall.status}　content ${emptyRecall.json.recall.content_bytes} 字节　片段 ${emptyRecall.json.blocks.length} 段　清单条目 ${emptyRecall.json.checklist.items.length} 条`);
        outJson('  content（截断）：', emptyRecall.json.recall.content, 400);
        for (const note of emptyRecall.json.checklist.notes) out(`  提示：${note}`);
        const noLeak = emptyRecall.json.blocks.every((block) => !savedRecords.some((item) => item.recordId === block.record_id));
        check('7.1', '无结果时不编造清单条目', emptyRecall.json.checklist.items.length === 0 && emptyRecall.json.blocks.length === 0, `条目 ${emptyRecall.json.checklist.items.length}，片段 ${emptyRecall.json.blocks.length}`);
        check('7.2', '空课题不会串到已保存课题', noLeak, noLeak ? '未发现其他课题的记录' : `出现 ${emptyRecall.json.blocks.map((b) => b.record_id).join(', ')}`);
        check('7.3', '空课题召回给出可读提示（notes 非空或明确状态）', emptyRecall.json.checklist.notes.length > 0 || emptyRecall.json.recall.content_bytes === 0, `notes ${emptyRecall.json.checklist.notes.length} 条`);
      } else {
        outJson('  响应：', emptyRecall.json ?? emptyRecall.text ?? emptyRecall.error, 500);
        check('7.1', '无结果召回返回结构化响应', false, `HTTP ${emptyRecall.status}`);
      }

      section('8. 文本安全与输入校验');
      const evilTitle = '<img src=x onerror=alert(1)> 伪造标记 hb-aaaaaaaaaa 与 下一步: 假的';
      const evilSave = await appPost(APP_PORT, '/api/handover/save', {
        project: projectA,
        title: evilTitle,
        finding: `引用伪造标记 hb-aaaaaaaaaa 和字段名伪装：\n下一步: 这条其实只是普通文本`,
        next_steps: '确认页面只用 textContent 渲染',
      });
      const evilText = evilSave.json?.sent?.text ?? '';
      const evilMarkers = [...new Set(evilText.match(MARKER_SEEN) ?? [])];
      out(`保存含 HTML 片段/伪造标记的交接 → HTTP ${evilSave.status}，record=${evilSave.json?.record?.record_id}`);
      outJson('  发送的 text（截断）：', evilText, 600);
      check('8.1', '用户文本里的伪造 hb- 标记被消歧', evilSave.status === 200 && evilMarkers.length === 1, `text 中 hb- 标记数=${evilMarkers.length}（应为 1，即我们自己生成的记录号）`);
      const longField = await appPost(APP_PORT, '/api/handover/save', {
        project: projectA,
        title: '超长字段测试',
        finding: 'x'.repeat(1300),
        next_steps: '略',
      });
      check('8.2', '超长字段返回 400 而不是写入', longField.status === 400, `HTTP ${longField.status}：${longField.json?.error?.message ?? ''}`);
      const missingNext = await appPost(APP_PORT, '/api/handover/save', { project: projectA, title: '缺少下一步', finding: '只有现象' });
      check('8.3', '缺少「下一步」返回 400 并说明原因', missingNext.status === 400 && /下一步/.test(missingNext.json?.error?.message ?? ''), `HTTP ${missingNext.status}：${missingNext.json?.error?.message ?? ''}`);
      const badMaxBytes = await appPost(APP_PORT, '/api/recall', { project: projectA, query: kwA, max_bytes: 999999 });
      check('8.4', '非法 max_bytes 被拒绝', badMaxBytes.status === 400, `HTTP ${badMaxBytes.status}：${badMaxBytes.json?.error?.message ?? ''}`);
      const evilRecall = await appPost(APP_PORT, '/api/recall', { project: projectA, query: evilTitle, max_bytes: 8000 });
      check('8.5', '召回接口不因特殊字符崩溃', evilRecall.status === 200, `HTTP ${evilRecall.status}`);
    } else {
      check('3.4', '召回生成清单', false, `HTTP ${recall1.status}：${String(recall1.text ?? recall1.error).slice(0, 200)}`);
    }
  } else {
    for (const id of ['3.1', '3.4', '4.1', '5.2', '6.2', '7.1', '8.1']) {
      check(id, '依赖 PowerContext 的场景', 'skip', OFFLINE ? '--offline 模式' : 'PowerContext 不可用');
    }
  }

  section('9. 错误反馈：PowerContext 不可用');
  const deadApp = await startApp({ port: APP_PORT + 1, dataDir: path.join(tempRoot, 'app-data-dead'), powercontextUrl: 'http://127.0.0.1:1' });
  await deadApp.ready();
  const deadOpen = await appPost(APP_PORT + 1, '/api/scope/open', { project: `验证课题死掉-${RUN_ID}` });
  out(`指向 http://127.0.0.1:1 的应用调用 /api/scope/open → HTTP ${deadOpen.status}`);
  outJson('  响应：', deadOpen.json ?? deadOpen.text ?? deadOpen.error, 700);
  check(
    '9.1',
    '服务不可用时返回 502 与可读原因（不崩溃、不伪造结果）',
    deadOpen.status === 502 && deadOpen.json?.error?.kind === 'powercontext' && /PowerContext/.test(deadOpen.json?.error?.message ?? ''),
    `HTTP ${deadOpen.status}：${deadOpen.json?.error?.message ?? ''}`
  );
  const deadRecall = await appPost(APP_PORT + 1, '/api/recall', { project: '任意课题', query: kwA, max_bytes: 8000 });
  check('9.2', '不可用时召回接口同样明确报错', deadRecall.status === 502, `HTTP ${deadRecall.status}：${deadRecall.json?.error?.message ?? ''}`);
  const deadHealth = await appGet(APP_PORT + 1, '/api/health');
  check('9.3', '应用进程仍存活，健康检查可用', deadHealth.status === 200, `HTTP ${deadHealth.status}`);
  await deadApp.stop();

  return finish(checks.some((item) => item.status === 'FAIL') ? 1 : 0);
}

main().then(
  (code) => {
    if (typeof code === 'number') process.exit(code);
  },
  (err) => {
    out('');
    out(`验证脚本自身出错：${err?.stack ?? err}`);
    try {
      writeFileSync(path.join(ROOT, 'docs', 'test-output', `verify-${RUN_ID}-crash.txt`), `${transcript.join('\n')}\n`, 'utf8');
    } catch {
      /* 目录可能不存在 */
    }
    process.exit(1);
  }
);
