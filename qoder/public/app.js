'use strict';

const state = { project: null, scope: null, examples: null, checklistText: '', recall: null };

const $ = (id) => document.getElementById(id);

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  const { class: className, text, dataset, ...attrs } = options;
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    node.setAttribute(key, value === true ? '' : String(value));
  }
  if (dataset) for (const [key, value] of Object.entries(dataset)) node.dataset[key] = String(value);
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function show(node, visible) {
  node.classList.toggle('hidden', !visible);
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = Boolean(busy);
  if (busy) button.dataset.busy = '1';
  else delete button.dataset.busy;
}

function message(node, kind, text) {
  clear(node);
  node.classList.add('msg');
  node.classList.remove('ok', 'warn', 'err');
  if (kind) node.classList.add(kind);
  node.appendChild(document.createTextNode(text));
}

async function api(method, path, body) {
  const init =
    method === 'GET'
      ? { headers: { accept: 'application/json' } }
      : { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body ?? {}) };
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    throw new Error(`无法连接本机应用（${method} ${path}）：${err.message}。请确认已运行 npm start。`);
  }
  const text = await res.text();
  let data = null;
  try {
    data = text === '' ? null : JSON.parse(text);
  } catch {
    throw new Error(`${method} ${path} 的响应不是 JSON（HTTP ${res.status}）：${text.slice(0, 160)}`);
  }
  if (!res.ok) {
    const info = data?.error ?? {};
    const parts = [info.message ?? `请求失败（HTTP ${res.status}）`];
    if (info.endpoint) parts.push(`接口 ${info.endpoint}`);
    if (info.detail) parts.push(`PowerContext 返回：${info.detail}`);
    if (info.powercontext_url) parts.push(`POWERCONTEXT_URL=${info.powercontext_url}`);
    const error = new Error(parts.join(' ｜ '));
    error.kind = info.kind ?? 'unknown';
    error.status = res.status;
    throw error;
  }
  return data;
}

async function refreshHealth() {
  const box = $('service-status');
  try {
    const health = await api('GET', '/api/health');
    clear(box);
    box.appendChild(el('span', { class: 'dot ok' }));
    box.appendChild(
      el('span', {
        text: `本机服务在线 · Node ${health.node} · PowerContext ${health.powercontext.powercontext_url}`,
      })
    );
    box.appendChild(el('span', { class: 'commit', text: `源码版本 ${health.powercontext.powercontext_commit.slice(0, 8)}…` }));
  } catch (err) {
    clear(box);
    box.appendChild(el('span', { class: 'dot err' }));
    box.appendChild(el('span', { text: err.message }));
  }
}

async function refreshProjects() {
  const box = clear($('recent-projects'));
  let data;
  try {
    data = await api('GET', '/api/projects');
  } catch (err) {
    message(box, 'err', err.message);
    return;
  }
  if (!data.projects.length) {
    box.appendChild(el('span', { class: 'muted', text: '本机还没有打开过课题。' }));
    return;
  }
  box.appendChild(el('span', { class: 'muted', text: '最近课题：' }));
  for (const project of data.projects.slice(0, 6)) {
    const chip = el('button', { class: 'chip', text: project.name, title: `scope ${project.scope_id}` });
    chip.addEventListener('click', () => {
      $('project-name').value = project.name;
      $('project-summary').value = project.summary ?? '';
      openProject();
    });
    box.appendChild(chip);
  }
}

function renderScopeInfo(scope) {
  const box = clear($('scope-info'));
  show(box, true);
  const rows = [
    ['课题名', scope.name],
    ['scope_id', scope.scope_id],
    ['idempotency_key', scope.idempotency_key],
    ['本机缓存', scope.cached_scope_id ? `上次记录的 scope_id：${scope.cached_scope_id}（${scope.reused_local_cache ? '一致' : '不一致，以 PowerContext 返回为准'}）` : '本机首次打开该课题'],
    ['PowerContext', `POST /v1/scopes → HTTP ${scope.http_status}，耗时 ${scope.elapsed_ms} ms`],
  ];
  for (const [key, value] of rows) {
    box.appendChild(el('div', { class: 'kv-row' }, [el('dt', { text: key }), el('dd', { text: value })]));
  }
}

async function openProject(nameOverride) {
  const status = $('scope-info');
  const name = (nameOverride ?? $('project-name').value).trim();
  if (!name) {
    message(status, 'err', '请先填写课题名。');
    show(status, true);
    return;
  }
  const button = $('btn-open');
  setBusy(button, true);
  try {
    const scope = await api('POST', '/api/scope/open', { project: name, summary: $('project-summary').value.trim() });
    state.project = scope.name;
    state.scope = scope;
    localStorage.setItem('handover-book.last-project', scope.name);
    $('project-name').value = scope.name;
    $('btn-save').disabled = false;
    $('btn-recall').disabled = false;
    renderScopeInfo(scope);
    await Promise.all([refreshReceipts(), refreshProjects()]);
  } catch (err) {
    show(status, true);
    message(status, 'err', err.message);
  } finally {
    setBusy(button, false);
  }
}

function collectForm() {
  return {
    project: state.project ?? $('project-name').value.trim(),
    title: $('f-title').value,
    finding: $('f-finding').value,
    evidence: $('f-evidence').value,
    conclusion: $('f-conclusion').value,
    next_steps: $('f-next').value,
    risks: $('f-risks').value,
  };
}

async function saveHandover() {
  const status = $('save-status');
  if (!state.project) {
    message(status, 'err', '请先在第 1 步进入课题。');
    return;
  }
  const button = $('btn-save');
  setBusy(button, true);
  message(status, 'warn', '正在写入 PowerContext…');
  try {
    const result = await api('POST', '/api/handover/save', collectForm());
    message(
      status,
      'ok',
      `已保存：记录号 ${result.record.record_id} ｜ entry ${result.receipt.entry_id} ｜ text ${result.receipt.text_bytes} 字节`
    );
    const detail = $('save-detail-body');
    clear(detail);
    detail.appendChild(document.createTextNode(`发送的 text:\n${result.sent.text}\n\nreason:\n${result.sent.reason}\n\n返回的 entry:\n${JSON.stringify(result.entry, null, 2)}`));
    show($('save-detail'), true);
    $('save-detail').open = true;
    for (const id of ['f-title', 'f-finding', 'f-evidence', 'f-conclusion', 'f-next', 'f-risks']) $(id).value = '';
    await Promise.all([refreshReceipts(), refreshProjects()]);
  } catch (err) {
    message(status, 'err', err.message);
  } finally {
    setBusy(button, false);
  }
}

async function loadExamples() {
  const status = $('scope-info');
  if (!state.examples) {
    try {
      state.examples = await api('GET', '/api/examples');
      fillExampleSelect();
    } catch (err) {
      show(status, true);
      message(status, 'err', `读取示例失败：${err.message}`);
      return;
    }
  }
  const selected = $('example-select').value;
  const example = state.examples.projects.find((item) => item.name === selected) ?? state.examples.projects[0];
  if (!example) {
    message(status, 'err', 'examples/seed.json 里没有可用示例。');
    show(status, true);
    return;
  }
  const proceed = window.confirm(
    `将把示例课题「${example.name}」的 ${example.handovers.length} 条虚构交接写入 PowerContext（同一课题名会复用同一个 scope）。继续？`
  );
  if (!proceed) return;
  const button = $('btn-load-examples');
  setBusy(button, true);
  try {
    $('project-name').value = example.name;
    $('project-summary').value = example.summary ?? '';
    await openProject(example.name);
    for (const handover of example.handovers) {
      const result = await api('POST', '/api/handover/save', { project: example.name, ...handover });
      message(status, 'ok', `示例已写入：${result.record.record_id}（entry ${result.receipt.entry_id}）`);
    }
    show($('panel-recall'), true);
    $('recall-query').value = $('recall-query').value || '重启后偏差变大，下一步该做什么';
  } catch (err) {
    message(status, 'err', err.message);
  } finally {
    setBusy(button, false);
  }
}

function fillExampleSelect() {
  const select = clear($('example-select'));
  for (const project of state.examples?.projects ?? []) {
    select.appendChild(el('option', { text: project.name, value: project.name }));
  }
}

function renderRecallSummary(data) {
  const box = clear($('recall-summary'));
  show(box, true);
  const kind = data.blocks.length === 0 ? 'warn' : 'ok';
  box.classList.add('statusbox', kind);
  box.appendChild(
    el('p', {
      text: `召回 status：${data.recall.status} ｜ 接口 ${data.recall.endpoint} ｜ HTTP ${data.recall.http_status} ｜ 耗时 ${data.recall.elapsed_ms} ms ｜ max_bytes=${data.max_bytes} ｜ content ${data.recall.content_bytes} 字节 ｜ 解析出 ${data.blocks.length} 段记录`,
    })
  );
  box.appendChild(el('p', { class: 'muted', text: `搜索词切分：${data.tokens.slice(0, 12).join('、') || '（无）'}${data.tokens.length > 12 ? ' …' : ''}` }));
  if (data.provenance.recalled_without_local_receipt.length) {
    box.appendChild(
      el('p', {
        class: 'muted',
        text: `注意：${data.provenance.recalled_without_local_receipt.length} 段召回内容在本机没有写入回执（可能由其他机器或更早的会话保存）：${data.provenance.recalled_without_local_receipt.join(', ')}`,
      })
    );
  }
  if (data.checklist.notes.length) {
    for (const note of data.checklist.notes) box.appendChild(el('p', { class: 'note', text: note }));
  }
}

function renderSources(blocks) {
  const box = clear($('recall-sources'));
  if (!blocks.length) {
    box.appendChild(el('p', { class: 'muted', text: '没有识别到任何记录片段，因此清单不会有来源。' }));
    return;
  }
  box.appendChild(el('h3', { class: 'sub', text: '清单来源（按 PowerContext 返回顺序）' }));
  const list = el('ol', { class: 'src-list' });
  for (const block of blocks) {
    const item = el('li', { class: 'src' });
    const head = el('div', { class: 'src-head' }, [
      el('span', { class: 'badge', text: `#${block.index + 1}` }),
      el('code', { class: 'rid', text: block.record_id }),
      el('strong', { text: block.fields.title || '(召回片段里没有标题)' }),
    ]);
    item.appendChild(head);

    const tags = el('div', { class: 'tags' });
    if (block.score.matched.length) {
      for (const match of block.score.matched) tags.appendChild(el('span', { class: 'tag', text: `${match.token} ×${match.count}` }));
    } else {
      tags.appendChild(el('span', { class: 'tag none', text: '搜索词未直接命中，仅按召回顺序保留' }));
    }
    item.appendChild(tags);

    if (!block.structured) {
      item.appendChild(
        el('p', {
          class: 'warn-text',
          text: '这段文本没有可解析的交接字段，只在原文区保留，不会进入清单条目。',
        })
      );
    } else {
      const sections = [
        ['现象与发现', block.fields.finding],
        ['证据', block.fields.evidence],
        ['结论', block.fields.conclusion],
        ['下一步', block.fields.next_steps],
        ['风险', block.fields.risks],
      ];
      const dl = el('dl', { class: 'kv tight' });
      for (const [label, values] of sections) {
        if (!values.length) continue;
        dl.appendChild(
          el('div', { class: 'kv-row' }, [
            el('dt', { text: label }),
            el('dd', {}, values.map((value, index) => el('span', { class: 'line', text: `${index + 1}. ${value}` }))),
          ])
        );
      }
      item.appendChild(dl);
    }

    const meta = el('div', { class: 'src-meta' });
    meta.appendChild(
      el('span', {
        class: block.has_receipt ? 'badge ok' : 'badge warn',
        text: block.has_receipt ? `本机回执 entry ${block.receipt.entry_id}` : '无本机回执',
      })
    );
    if (block.fields.created_at) meta.appendChild(el('span', { class: 'muted', text: `记录时间 ${block.fields.created_at}` }));
    if (block.truncated) meta.appendChild(el('span', { class: 'badge warn', text: '被长度上限截断' }));
    item.appendChild(meta);

    const details = el('details', {}, [el('summary', { text: '这段召回原文' })]);
    details.appendChild(el('pre', { class: 'mono', text: block.raw_text }));
    item.appendChild(details);

    list.appendChild(item);
  }
  box.appendChild(list);
}

function renderChecklist(data) {
  state.checklistText = data.checklist.text;
  const pre = $('checklist-text');
  clear(pre);
  pre.appendChild(document.createTextNode(data.checklist.text));
  $('btn-copy').disabled = data.checklist.items.length === 0;
  const raw = $('recall-raw');
  clear(raw);
  raw.appendChild(document.createTextNode(data.recall.content === '' ? '(PowerContext 返回的 content 为空)' : data.recall.content));
}

async function recall() {
  const summary = $('recall-summary');
  if (!state.project) {
    show(summary, true);
    message(summary, 'err', '请先在第 1 步进入课题。');
    return;
  }
  const query = $('recall-query').value.trim();
  if (!query) {
    show(summary, true);
    message(summary, 'err', '请填写当前问题，它作为 /v1/context/prepare 的 query。');
    return;
  }
  const button = $('btn-recall');
  setBusy(button, true);
  try {
    const data = await api('POST', '/api/recall', {
      project: state.project,
      query,
      max_bytes: Number.parseInt($('recall-max-bytes').value, 10) || undefined,
    });
    state.recall = data;
    renderRecallSummary(data);
    renderSources(data.blocks);
    renderChecklist(data);
  } catch (err) {
    show(summary, true);
    message(summary, 'err', err.message);
  } finally {
    setBusy(button, false);
  }
}

async function copyChecklist() {
  const button = $('btn-copy');
  const summary = $('recall-summary');
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(state.checklistText);
    } else {
      const area = document.createElement('textarea');
      area.value = state.checklistText;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.left = '-1000px';
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    message(summary, 'ok', '清单已复制到剪贴板。');
    show(summary, true);
  } catch (err) {
    message(summary, 'warn', `复制失败（${err.message}），请在文本框里手工全选复制。`);
  } finally {
    button.classList.add('done');
    setTimeout(() => button.classList.remove('done'), 1200);
  }
}

async function refreshReceipts() {
  const box = clear($('receipt-list'));
  if (!state.project) {
    box.appendChild(el('p', { class: 'muted', text: '进入课题后显示该课题的本机写入回执。' }));
    return;
  }
  try {
    const data = await api('GET', `/api/records?project=${encodeURIComponent(state.project)}`);
    box.appendChild(el('p', { class: 'muted', text: data.note }));
    if (!data.receipts.length) {
      box.appendChild(el('p', { class: 'muted', text: '该课题在本机还没有写入回执。' }));
      return;
    }
    const list = el('ul', { class: 'receipt-list' });
    for (const receipt of data.receipts) {
      list.appendChild(
        el('li', {}, [
          el('code', { text: receipt.record_id }),
          el('span', { text: ` · ${receipt.title}` }),
          el('span', { class: 'muted', text: ` · entry ${receipt.entry_id} · ${receipt.saved_at} · ${receipt.text_bytes} 字节 · sha256 ${receipt.text_sha256}` }),
        ])
      );
    }
    box.appendChild(list);
  } catch (err) {
    message(box, 'err', err.message);
  }
}

function bind() {
  $('btn-open').addEventListener('click', () => openProject());
  $('btn-save').addEventListener('click', saveHandover);
  $('btn-recall').addEventListener('click', recall);
  $('btn-copy').addEventListener('click', copyChecklist);
  $('btn-load-examples').addEventListener('click', loadExamples);
  $('recall-query').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') recall();
  });
  $('project-name').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') openProject();
  });
}

async function init() {
  bind();
  const last = localStorage.getItem('handover-book.last-project');
  if (last) $('project-name').value = last;
  $('btn-save').disabled = true;
  $('btn-recall').disabled = true;
  await Promise.all([refreshHealth(), refreshProjects()]);
  try {
    state.examples = await api('GET', '/api/examples');
    fillExampleSelect();
  } catch {
    clear($('example-select')).appendChild(el('option', { text: '示例读取失败', value: '' }));
  }
  clear($('receipt-list')).appendChild(el('p', { class: 'muted', text: '进入课题后显示该课题的本机写入回执。' }));
}

init();
