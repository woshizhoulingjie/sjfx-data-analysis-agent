const state = {
  scan: null,
  selected: null,
  summary: null,
  analysis: null,
  processing: null,
  fileWorkflowPage: null,
  fileWorkflowFilter: 'all',
  fileWorkflowFilters: {},
  fileWorkflowOffset: 0,
  fileWorkflowRequestSeq: 0,
  fileWorkflowAbortController: null,
  progressiveAnalysis: null,
  progressiveRefreshKey: null,
  analysisTreeOriginal: null,
  summaries: new Map(),
  activeTree: 'physical',
  jobId: null,
  modelGenerationEnabled: null,
  lastRetrievalId: null,
  retrievalScope: 'package',
  pendingEvidenceLocation: null,
  selectedNodes: new Map(),
  treeEdits: [],
  jobs: new Map(),
  jobsEndpointAvailable: null,
  taskCenterRefreshInFlight: false,
  selectionRequestId: 0,
  dataSources: [],
  dataSourceOffset: 0,
  dataSourceQuery: '',
  dataSourceTotal: 0,
  dataSourceRequestInFlight: false,
  fileSearchResult: null,
  fileSearchPage: 1,
  selection: null,
  selectionFiles: [],
  selectionOffset: 0,
  selectionTotal: 0,
  selectionNextOffset: null
};

const ACTIVE_JOB_STATUSES = new Set(['queued', 'running', 'cancelling']);
const TASK_REGISTRY_KEY = 'sjfx_task_registry_v1';
const CURRENT_SCAN_KEY = 'sjfx_current_scan_id_v1';
const SELECTIONS_KEY_PREFIX = 'sjfx_export_selections_v1:';

const $ = (id) => document.getElementById(id);


function toast(message, error = false) {
  const el = $('toast');

  el.textContent = message;

  el.className =
    'toast show' +
    (error ? ' error' : '');

  clearTimeout(
    window.__toastTimer
  );

  window.__toastTimer = setTimeout(
    () => el.className = 'toast',
    5200
  );
}


const SJFX_API_TOKEN_KEY = 'sjfx_api_token';

function normalizeApiToken(value) {
  const token = String(value || '').trim();
  return /^[\x21-\x7e]+$/.test(token) ? token : '';
}

const SJFXAuth = (() => {
  let promptAttempted = false;

  function storedToken() {
    const raw = window.sessionStorage.getItem(SJFX_API_TOKEN_KEY) || '';
    const token = normalizeApiToken(raw);
    if (raw && !token) window.sessionStorage.removeItem(SJFX_API_TOKEN_KEY);
    return token;
  }

  function authError() {
    const error = new Error('\u8bf7\u5148\u8bbe\u7f6e\u6709\u6548\u7684 SJFX API Token');
    error.status = 401;
    error.code = 'SJFX_AUTH_REQUIRED';
    return error;
  }

  function clearToken() {
    window.sessionStorage.removeItem(SJFX_API_TOKEN_KEY);
  }

  function ensureToken({ force = false } = {}) {
    if (force) {
      clearToken();
      promptAttempted = false;
    }
    const existing = storedToken();
    if (existing) return existing;
    if (promptAttempted) throw authError();

    promptAttempted = true;
    const token = normalizeApiToken(
      window.prompt('\u8bf7\u8f93\u5165 SJFX API Token\uff08\u4ec5\u4fdd\u5b58\u4e8e\u672c\u6b21\u6d4f\u89c8\u4f1a\u8bdd\uff09', '') || ''
    );
    if (!token) throw authError();
    window.sessionStorage.setItem(SJFX_API_TOKEN_KEY, token);
    return token;
  }

  async function request(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    let retried = false;
    while (true) {
      headers['X-SJFX-Token'] = ensureToken();
      const response = await window.fetch(url, { ...options, headers });
      if (response.status !== 401 || retried) return response;
      retried = true;
      clearToken();
      ensureToken({ force: true });
    }
  }

  return { ensureToken, clearToken, hasToken: () => Boolean(storedToken()), authError, request };
})();
window.SJFXAuth = SJFXAuth;

document.addEventListener('DOMContentLoaded', () => {
  // Every API endpoint requires authentication. Ask once before polling starts.
  try { SJFXAuth.ensureToken(); } catch (_) { /* user can set it from the header later */ }
});

async function api(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const response = await SJFXAuth.request(url, { ...options, headers });
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
    // Reverse proxies can return an HTML error page while the Worker/Web
    // process restarts. Keep the HTTP status so polling can retry safely.
  }
  if (!response.ok || !data.ok) {
    const error = new Error(data.error || `\u8bf7\u6c42\u5931\u8d25\uff08HTTP ${response.status}\uff09`);
    error.status = response.status;
    error.transient = response.status >= 500 || [408, 425, 429].includes(response.status);
    throw error;
  }
  return data;
}

// Downloads cannot attach X-SJFX-Token to a plain navigation. Ask the API for
// a short-lived one-use URL, then let the browser stream the response directly
// to disk. Never buffer a multi-gigabyte export as an in-memory Blob.
async function authenticatedDownload(url) {
  const path = String(url || '').split('?', 1)[0];
  const encodedName = path.split('/').pop() || '';
  const filename = decodeURIComponent(encodedName);
  if (!filename) throw new Error('下载文件名无效');
  const ticket = await api('/api/download-ticket', {
    method: 'POST', body: JSON.stringify({ filename })
  });
  const anchor = document.createElement('a');
  anchor.href = ticket.download_url;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function downloadLink(url, label) {
  return `<a class="download-link authenticated-download" href="#" data-download-url="${escapeHtml(url)}">${escapeHtml(label)}</a>`;
}

document.addEventListener('click', async (event) => {
  const link = event.target.closest('.authenticated-download');
  if (!link) return;
  event.preventDefault();
  try {
    link.classList.add('disabled');
    await authenticatedDownload(link.dataset.downloadUrl || '');
    toast('文件已开始下载');
  } catch (error) {
    toast(error.message || '下载失败', true);
  } finally {
    link.classList.remove('disabled');
  }
});




function setBusy(button, busy, label) {
  if (busy) {
    button.dataset.label =
      button.textContent;

    button.textContent =
      label || '处理中…';

    button.disabled = true;
  } else {
    button.textContent =
      button.dataset.label
      || button.textContent;

    button.disabled = false;
  }
}


function escapeHtml(value) {
  const d =
    document.createElement('div');

  d.textContent =
    String(value ?? '');

  return d.innerHTML;
}


const SUMMARY_TOPIC_WORDS = [
  'shadow', 'shadows', 'cipher', 'spaces', 'exploiting', 'tweak',
  'hardware', 'memory', 'encryption', 'attack', 'attacks', 'security',
  'privacy', 'cryptography', 'method', 'methods', 'experiment', 'results',
  'system', 'data', 'analysis', 'network', 'language', 'model', 'research',
  'evaluation'
];

function splitSummaryTopic(value) {
  const text = String(value || '').trim();
  if (!text || /\s|[,，、;；/|]/.test(text) || text.length < 18) return text ? [text] : [];
  const source = text.toLowerCase();
  const words = [...SUMMARY_TOPIC_WORDS].sort((a, b) => b.length - a.length);
  const parts = [];
  let index = 0;
  while (index < source.length) {
    const match = words.find((word) => source.startsWith(word, index));
    if (match) {
      parts.push(text.slice(index, index + match.length));
      index += match.length;
      continue;
    }
    let end = index + 1;
    while (end < source.length && !words.some((word) => source.startsWith(word, end))) end += 1;
    parts.push(text.slice(index, end));
    index = end;
  }
  const covered = parts.join('').length;
  return parts.length > 1 && covered >= text.length * 0.8 ? parts : [text];
}

function summaryTopicValues(value) {
  const values = Array.isArray(value) ? value : [value];
  const output = [];
  values.forEach((item) => {
    if (Array.isArray(item)) {
      output.push(...summaryTopicValues(item));
      return;
    }
    String(item || '')
      .split(/[,，、;；/|\n]+/)
      .map((part) => part.replace(/\s+/g, ' ').trim())
      .filter(Boolean)
      .forEach((part) => splitSummaryTopic(part).forEach((piece) => {
        if (piece && !output.some((existing) => existing.toLowerCase() === piece.toLowerCase())) output.push(piece);
      }));
  });
  return output.slice(0, 16);
}

function summaryParagraphsHtml(value) {
  const paragraphs = String(value || '')
    .replace(/\r\n?/g, '\n')
    .split(/\n{2,}|\n/)
    .map((part) => part.trim())
    .filter(Boolean);
  return paragraphs.map((part) => `<p class="summary-paragraph">${escapeHtml(part)}</p>`).join('');
}



function compactClaimSupportsHtml(supports) {
  const items = Array.isArray(supports) ? supports.slice(0, 3) : [];
  if (!items.length) return '<p class="claim-support-empty">暂无可定位的原文依据。</p>';
  return '<div class="claim-supports">' + items.map((item) => {
    const sourcePath = item.source_path || item.archive_source_path || '';
    const location = [item.page != null ? `第 ${item.page} 页` : '', item.section || ''].filter(Boolean).join(' · ');
    const quote = String(item.supporting_quote || item.text || item.content || '').trim();
    const sourceLocation = {
      page: item.page ?? null,
      section: item.section || '',
      paragraph_index: item.paragraph_index ?? null,
      block_index: item.block_index ?? null,
      char_start: item.char_start ?? null,
      char_end: item.char_end ?? null
    };
    const action = sourcePath
      ? `<button type="button" class="evidence-source-link" data-evidence-source="${escapeHtml(sourcePath)}" data-evidence-location="${escapeHtml(JSON.stringify(sourceLocation))}">打开原文位置</button>`
      : '';
    return `<div class="claim-support"><p>“${escapeHtml(quote.slice(0, 700))}${quote.length > 700 ? '…' : ''}”</p><small>${escapeHtml([sourcePath, location].filter(Boolean).join(' · ') || '原文位置未记录')}</small>${action}</div>`;
  }).join('') + '</div>';
}

function displayText(value, fallback = '—') {
  if (value == null || value === '') return fallback;
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map((item) => displayText(item, '')).filter(Boolean).join('；') || fallback;
  if (typeof value === 'object') {
    return String(value.text || value.statement || value.answer || value.summary || value.title || value.name || fallback);
  }
  return fallback;
}

function displaySupport(value) {
  const score = Number(value);
  return Number.isFinite(score) && score >= 0 && score <= 1
    ? ` · 支撑度 ${Math.round(score * 100)}%`
    : '';
}

function fileClaimsHtml(data) {
  const isFile = data.summary_type === 'file' || Array.isArray(data.file_conclusions);
  const conclusions = isFile
    ? data.file_conclusions
    : (Array.isArray(data.conclusions) ? data.conclusions : []);
  const argumentsList = Array.isArray(data.file_arguments) ? data.file_arguments : [];
  const review = Array.isArray(data.file_review_items) ? data.file_review_items : [];
  const limitations = Array.isArray(data.file_limitations) ? data.file_limitations : [];
  const quality = data.evidence_quality && typeof data.evidence_quality === 'object'
    ? data.evidence_quality : {};
  if (!conclusions.length && !argumentsList.length && !review.length && !limitations.length) return '';
  const renderClaim = (item) => {
    const supports = Array.isArray(item.supports)
      ? item.supports
      : (Array.isArray(item.evidence) ? item.evidence : []);
    const badge = item.support_label || (item.status === 'verified' ? '原文直接支持' : '需复核');
    const supportScores = supports.map((entry) => Number(entry.support_score)).filter((score) => Number.isFinite(score));
    const confidenceValue = item.confidence != null ? Number(item.confidence) : (supportScores.length ? Math.max(...supportScores) : null);
    const confidence = displaySupport(confidenceValue);
    return `<article class="conclusion-evidence file-claim-card">` +
      `<div><span class="step-pill">${escapeHtml(item.label || item.type || '文件结论')}</span>` +
      `<span class="inference-badge">${escapeHtml(badge + confidence)}</span></div>` +
      `<p><strong>${escapeHtml(displayText(item))}</strong></p>` +
      `<p class="file-claim-proof-title"><strong>原文依据</strong></p>` +
      compactClaimSupportsHtml(supports) +
      `</article>`;
  };
  const heading = isFile ? '这篇文件可以支持的结论' : '这个节点可以支持的结论';
  let html = `<section class="file-claims"><h3>${heading}</h3>`;
  if (quality.claims_considered != null) {
    const status = quality.status === 'verified' ? '已完成原文核验' : quality.status === 'partial' ? '部分核验，保留复核项' : '暂无足够原文支撑';
    html += `<p class="coverage-card"><strong>结论核验：</strong>${escapeHtml(status)}；正式结论 ${escapeHtml(quality.formal_claim_count ?? 0)} 条，待复核 ${escapeHtml(quality.unsupported_count ?? 0)} 条，原文证据 ${escapeHtml(quality.eligible_evidence_count ?? 0)} 条。</p>`;
  }
  if (conclusions.length) {
    html += `<h4>可由文件支撑的结论</h4>${conclusions.map(renderClaim).join('')}`;
  }
  const argumentOnly = argumentsList.filter(item => !conclusions.some(c => c.conclusion_id === item.conclusion_id));
  if (argumentOnly.length) {
    html += `<h4>主要论点与方法依据</h4>${argumentOnly.map(renderClaim).join('')}`;
  }
  if (review.length) {
    html += `<h4>需要人工复核的候选判断</h4><ul class="file-review-list">${review.map(item => `<li><strong>${escapeHtml(item.text || '')}</strong><span>${escapeHtml(item.reason || '原文支撑不足')}</span></li>`).join('')}</ul>`;
  }
  if (limitations.length) {
    html += `<h4>文件限制与待核对项</h4><ul class="file-review-list">${limitations.map(item => `<li>${escapeHtml(item.text || item)}</li>`).join('')}</ul>`;
  }
  return html + `</section>`;
}

function summaryStructureHtml(value) {
  const structure = value && typeof value === 'object' ? value : {};
  const rows = [];
  const addRow = (label, item) => {
    if (item == null || item === '' || (Array.isArray(item) && !item.length)) return;
    const rendered = Array.isArray(item)
      ? item.map((entry) => typeof entry === 'object' ? JSON.stringify(entry) : String(entry)).join('、')
      : typeof item === 'object' ? '' : String(item);
    if (rendered) rows.push(`<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(rendered)}</strong></div>`);
  };
  addRow('文档标题', structure.title || structure.document_title);
  addRow('文档类型', structure.document_type || structure.type);
  addRow('页数 / 张数', structure.page_count ?? structure.pages);
  addRow('表格数量', structure.table_count ?? structure.tables);
  addRow('图片数量', structure.picture_count ?? structure.image_count ?? structure.images);
  addRow('字符数', structure.character_count ?? structure.char_count ?? structure.characters);
  const coverage = structure.coverage;
  if (coverage && typeof coverage === 'object') {
    const ratio = coverage.coverage_ratio != null ? `${Math.round(Number(coverage.coverage_ratio) * 10000) / 100}%` : '';
    addRow('正文覆盖', ratio || (coverage.complete === false ? '部分覆盖' : coverage.complete === true ? '完整' : '—'));
  }
  const sections = structure.sections || structure.headings || structure.chapter_titles || [];
  let html = rows.length ? `<div class="summary-structure-grid">${rows.join('')}</div>` : '';
  if (Array.isArray(sections) && sections.length) {
    html += `<div class="summary-sections"><span>章节 / 结构</span><ul>${sections.slice(0, 40).map((section) => `<li>${escapeHtml(typeof section === 'object' ? (section.title || section.name || JSON.stringify(section)) : section)}</li>`).join('')}</ul></div>`;
  }
  return html || '<p class="muted">未提取到可展示的结构字段。</p>';
}


function icon(node) {
  if (node.kind === 'evidence') {
    return '⌁';
  }

  if (node.kind === 'directory') {
    return '▾';
  }

  if (
    node.kind === 'group'
    || node.kind === 'analysis_root'
  ) {
    return '◈';
  }

  return '·';
}


function exportSelectionKey(node) {
  if (node.kind === 'evidence') {
    return `evidence:${node.source_path || ''}:${node.evidence?.evidence_id || node.name || ''}`;
  }
  return node.node_id
    ? `node:${node.node_id}`
    : `${node.kind || 'node'}:${node.path || node.name || ''}`;
}


function canExportNode(node) {
  return Boolean(node) && node.kind !== 'analysis_root';
}


function exportPayloadNode(node) {
  return {
    kind: node.kind,
    path: node.path || null,
    node_id: node.node_id || null,
    name: node.name || null,
    source_path: node.source_path || null,
    evidence_id: node.evidence?.evidence_id || null
  };
}


function updateSelectionCart() {
  const values = [...state.selectedNodes.values()];
  const cart = $('selectionCart');
  if (!values.length) {
    cart.className = 'selection-cart empty';
    cart.textContent = '可勾选多个主题、目录、文档或原文证据，组合导出时会自动去重。';
  } else {
    cart.className = 'selection-cart';
    cart.innerHTML = `<strong>已勾选 ${values.length} 个节点</strong> · 组合导出将按源文件去重<br><small>${escapeHtml(values.slice(0, 5).map(x => x.name || x.path).join('、'))}${values.length > 5 ? '…' : ''}</small>`;
  }
  $('exportBtn').disabled = !state.scan || (!values.length && !canExportNode(state.selected));
  persistNodeSelections();
}


function persistNodeSelections() {
  const scanId = state.scan?.scan_id;
  if (!scanId) return;
  try {
    window.localStorage.setItem(
      `${SELECTIONS_KEY_PREFIX}${scanId}`,
      JSON.stringify([...state.selectedNodes.values()])
    );
  } catch (_) {
    // Selection remains usable for this browser session.
  }
}


function restoreNodeSelections(scanId) {
  try {
    const values = JSON.parse(window.localStorage.getItem(`${SELECTIONS_KEY_PREFIX}${scanId}`) || '[]');
    state.selectedNodes = new Map(
      (Array.isArray(values) ? values : [])
        .filter((item) => item && typeof item === 'object')
        .map((item) => [exportSelectionKey(item), item])
    );
  } catch (_) {
    state.selectedNodes = new Map();
  }
}


function rememberCurrentScan(scanId) {
  const value = String(scanId || '').trim();
  if (!value) return;
  try {
    window.localStorage.setItem(CURRENT_SCAN_KEY, value);
    window.dispatchEvent(new CustomEvent('sjfx-scan-changed', { detail: { scanId: value } }));
  } catch (_) {
    // The current page remains usable when browser storage is unavailable.
  }
}


function forgetCurrentScan(scanId = '') {
  try {
    const stored = window.localStorage.getItem(CURRENT_SCAN_KEY) || '';
    if (!scanId || stored === String(scanId)) {
      window.localStorage.removeItem(CURRENT_SCAN_KEY);
      window.dispatchEvent(new CustomEvent('sjfx-scan-changed', { detail: { scanId: '' } }));
    }
  } catch (_) {
    // There is nothing else to clean up when browser storage is unavailable.
  }
}


let treeDragSource = null;
let treeDropTarget = null;

function closeTreeContextMenu() {
  const menu = document.querySelector('.tree-context-menu');
  if (menu) menu.remove();
}

function treeHistoryInfo() {
  const active = [];
  const undone = [];
  (state.treeEdits || []).forEach((edit) => {
    const operation = String(edit.operation || '').toLowerCase();
    const id = String(edit.edit_id || '');
    if (operation === 'undo' || operation === 'redo') {
      const target = String(edit.payload?.edit_id || '');
      if (operation === 'undo') {
        const index = active.findIndex((item) => String(item.edit_id || '') === target);
        if (index >= 0) undone.push(active.splice(index, 1)[0]);
      } else {
        const index = undone.findIndex((item) => String(item.edit_id || '') === target);
        if (index >= 0) active.push(undone.splice(index, 1)[0]);
      }
      return;
    }
    if (id) active.push(edit);
  });
  return { undoTarget: active[active.length - 1] || null, redoTarget: undone[undone.length - 1] || null };
}

function ensureTreeHistoryControls() {
  const tools = document.querySelector('.tree-tools');
  if (!tools) return;
  if (!$('treeUndoBtn')) {
    const undo = document.createElement('button');
    undo.id = 'treeUndoBtn'; undo.className = 'icon-button'; undo.type = 'button'; undo.textContent = '↶';
    undo.title = '撤销上一次目录操作'; undo.setAttribute('aria-label', '撤销');
    undo.onclick = async () => {
      const target = treeHistoryInfo().undoTarget;
      if (!target) return;
      try { await submitTreeEdit('undo', { edit_id: target.edit_id }); } catch (error) { toast(error.message || '撤销失败', true); }
    };
    tools.appendChild(undo);
  }
  if (!$('treeRedoBtn')) {
    const redo = document.createElement('button');
    redo.id = 'treeRedoBtn'; redo.className = 'icon-button'; redo.type = 'button'; redo.textContent = '↷';
    redo.title = '恢复已撤销的目录操作'; redo.setAttribute('aria-label', '恢复');
    redo.onclick = async () => {
      const target = treeHistoryInfo().redoTarget;
      if (!target) return;
      try { await submitTreeEdit('redo', { edit_id: target.edit_id }); } catch (error) { toast(error.message || '恢复失败', true); }
    };
    tools.appendChild(redo);
  }
}

function updateTreeHistoryControls() {
  ensureTreeHistoryControls();
  const history = treeHistoryInfo();
  const enabled = state.activeTree === 'analysis' && Boolean(state.analysis?.analysis_tree);
  if ($('treeUndoBtn')) $('treeUndoBtn').disabled = !enabled || !history.undoTarget;
  if ($('treeRedoBtn')) $('treeRedoBtn').disabled = !enabled || !history.redoTarget;
}

function closeSplitDialog() {
  const dialog = document.querySelector('.split-dialog-backdrop');
  if (dialog) dialog.remove();
}

async function openSplitDialog(node) {
  closeTreeContextMenu();
  let files = (node.children || []).filter((item) => item.kind === 'file' && item.path).map((item) => ({ path: item.path, name: item.name || item.path }));
  const expectedMembers = Number(node.member_count ?? node.file_count ?? (node.member_paths || []).length);
  if (state.scan && expectedMembers > files.length) {
    try {
      files = [];
      let offset = 0;
      do {
        const data = await api(
          `/api/analysis-node-members/${state.scan.scan_id}?node_id=${encodeURIComponent(node.node_id)}&offset=${offset}&limit=500`
        );
        files.push(...(data.members || []));
        offset = data.page?.next_offset;
      } while (offset != null);
    } catch (error) {
      toast(error.message || '无法加载主题完整成员，已阻止不完整拆分', true);
      return;
    }
  }
  if (files.length < 2) { toast('当前主题至少需要两个已解析文件才能拆分', true); return; }
  const draft = { source: files.slice(), groups: [{ name: (node.name || '主题') + ' A', paths: [] }, { name: (node.name || '主题') + ' B', paths: [] }] };
  const backdrop = document.createElement('div');
  backdrop.className = 'split-dialog-backdrop';
  backdrop.innerHTML = '<section class="split-dialog" role="dialog" aria-modal="true" aria-label="可视化拆分主题"><header><div><span class="section-kicker">SPLIT TOPIC</span><h2>拖动文件拆分主题</h2><p>把左侧文件拖入不同子主题；每个子主题至少放一个文件。</p></div><button type="button" class="icon-button split-close" aria-label="关闭">×</button></header><div class="split-board"><div class="split-pool"><strong>待分配文件</strong><div class="split-drop-zone" data-zone="source"></div></div><div class="split-groups"></div></div><footer><button type="button" class="secondary split-add-group">＋ 添加子主题</button><span class="split-dialog-spacer"></span><button type="button" class="ghost split-cancel">取消</button><button type="button" class="primary split-save">保存拆分</button></footer></section>';
  document.body.appendChild(backdrop);
  const splitFileCard = (file) => {
    const card = document.createElement('div');
    card.className = 'split-file-card'; card.draggable = true; card.textContent = file.name; card.title = file.path; card.dataset.path = file.path;
    card.ondragstart = (event) => event.dataTransfer.setData('text/plain', file.path);
    return card;
  };
  const render = () => {
    const source = backdrop.querySelector('[data-zone="source"]'); source.innerHTML = '';
    draft.source.forEach((file) => source.appendChild(splitFileCard(file)));
    const groups = backdrop.querySelector('.split-groups'); groups.innerHTML = '';
    draft.groups.forEach((group, index) => {
      const column = document.createElement('div'); column.className = 'split-group-column';
      column.innerHTML = '<div class="split-group-title"><input aria-label="子主题名称"><button type="button" class="icon-button split-remove-group" title="删除子主题">×</button></div><div class="split-drop-zone" data-zone="group" data-index="' + index + '"></div>';
      const input = column.querySelector('input'); input.value = group.name; input.oninput = (event) => { group.name = event.target.value; };
      column.querySelector('.split-remove-group').onclick = () => {
        if (draft.groups.length <= 2) { toast('至少保留两个子主题', true); return; }
        group.paths.forEach((path) => { const file = files.find((item) => item.path === path); if (file) draft.source.push(file); });
        draft.groups.splice(index, 1); render();
      };
      const zone = column.querySelector('[data-zone="group"]');
      group.paths.forEach((path) => zone.appendChild(splitFileCard(files.find((file) => file.path === path) || { path, name: path })));
      groups.appendChild(column);
    });
    backdrop.querySelectorAll('.split-drop-zone').forEach((zone) => {
      zone.ondragover = (event) => { event.preventDefault(); zone.classList.add('is-over'); };
      zone.ondragleave = () => zone.classList.remove('is-over');
      zone.ondrop = (event) => {
        event.preventDefault(); zone.classList.remove('is-over');
        const path = event.dataTransfer.getData('text/plain'); if (!path) return;
        draft.source = draft.source.filter((file) => file.path !== path);
        draft.groups.forEach((item) => { item.paths = item.paths.filter((value) => value !== path); });
        if (zone.dataset.zone === 'source') draft.source.push(files.find((file) => file.path === path));
        else draft.groups[Number(zone.dataset.index)].paths.push(path);
        render();
      };
    });
  };
  backdrop.querySelector('.split-close').onclick = closeSplitDialog;
  backdrop.querySelector('.split-cancel').onclick = closeSplitDialog;
  backdrop.querySelector('.split-add-group').onclick = () => { draft.groups.push({ name: '子主题 ' + (draft.groups.length + 1), paths: [] }); render(); };
  backdrop.querySelector('.split-save').onclick = async () => {
    const groups = draft.groups.map((group) => ({ name: group.name.trim(), paths: [...new Set(group.paths)] })).filter((group) => group.name && group.paths.length);
    if (groups.length < 2) { toast('至少需要两个有文件的子主题', true); return; }
    try { await submitTreeEdit('split', { node_id: node.node_id, groups }); closeSplitDialog(); } catch (error) { toast(error.message || '拆分主题失败', true); }
  };
  render();
}

function showTreeContextMenu(event, node) {
  closeTreeContextMenu();
  if (state.activeTree !== 'analysis' || !node) return;
  const sourceRow = event.currentTarget;
  const actions = [];
  if (node.kind === 'group' && node.node_id) {
    actions.push({ label: '重命名主题', run: async () => {
      const name = window.prompt('新的主题名称：', node.name || '');
      if (!name || !name.trim()) return;
      await submitTreeEdit('rename', { node_id: node.node_id, name: name.trim() });
    }});
    actions.push({ label: '确认分类', run: async () => {
      await submitTreeEdit('confirm', { node_id: node.node_id, confirmed: true });
    }});
    actions.push({ label: '拆分主题…', run: async () => {
      openSplitDialog(node);
    }});
  } else if (node.kind === 'file') {
    actions.push({ label: '选中文件', run: async () => selectNode(node, sourceRow) });
    actions.push({ label: '拖到主题即可挂载', run: async () => toast('请将文件拖到右侧主题节点上') });
  }
  if (!actions.length) return;
  const menu = document.createElement('div');
  menu.className = 'tree-context-menu';
  menu.addEventListener('click', (e) => e.stopPropagation());
  actions.forEach((action) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = action.label;
    button.onclick = async () => {
      closeTreeContextMenu();
      try { await action.run(); } catch (error) { toast(error.message || '目录操作失败', true); }
    };
    menu.appendChild(button);
  });
  document.body.appendChild(menu);
  const left = Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 12);
  const top = Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 12);
  menu.style.left = Math.max(8, left) + 'px';
  menu.style.top = Math.max(8, top) + 'px';
}

function setupTreeDrag(row, node) {
  const canDrag = state.activeTree === 'analysis' && (node.kind === 'file' || node.kind === 'group') && Boolean(node.path || node.node_id);
  if (!canDrag) return;
  row.draggable = true;
  row.addEventListener('dragstart', (event) => {
    treeDragSource = node;
    row.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('application/x-sjfx-tree-node', JSON.stringify({ node_id: node.node_id || null, path: node.path || null, kind: node.kind }));
  });
  row.addEventListener('dragend', () => {
    row.classList.remove('dragging');
    if (treeDropTarget) treeDropTarget.classList.remove('tree-drop-target');
    treeDragSource = null;
    treeDropTarget = null;
  });
  row.addEventListener('dragover', (event) => {
    if (!treeDragSource || node.kind !== 'group' || treeDragSource === node) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    if (treeDropTarget && treeDropTarget !== row) treeDropTarget.classList.remove('tree-drop-target');
    treeDropTarget = row;
    row.classList.add('tree-drop-target');
  });
  row.addEventListener('dragleave', () => {
    row.classList.remove('tree-drop-target');
    if (treeDropTarget === row) treeDropTarget = null;
  });
  row.addEventListener('drop', async (event) => {
    event.preventDefault();
    row.classList.remove('tree-drop-target');
    if (!treeDragSource || node.kind !== 'group' || treeDragSource === node) return;
    const source = treeDragSource;
    treeDragSource = null;
    treeDropTarget = null;
    try {
      if (source.kind === 'file') {
        await submitTreeEdit('mount', { node_id: node.node_id, path: source.path });
      } else if (source.kind === 'group' && source.node_id && node.node_id) {
        const name = window.prompt('合并后的主题名称：', node.name || source.name || '合并主题');
        if (name && name.trim()) await submitTreeEdit('merge', { node_ids: [source.node_id, node.node_id], name: name.trim() });
      }
    } catch (error) { toast(error.message || '拖拽目录操作失败', true); }
  });
}

function appendTreePageControl(node, childList, twisty) {
  childList.querySelectorAll(':scope > .tree-load-more').forEach((item) => item.remove());
  if (node._children_next_offset == null) return;
  const item = document.createElement('li');
  item.className = 'tree-load-more';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'text-button';
  const loaded = Number(node._children_next_offset || 0);
  const total = Number(node._children_total || node.child_count || 0);
  button.textContent = `继续加载（${loaded}/${total}）`;
  button.onclick = async (event) => {
    event.stopPropagation();
    button.disabled = true;
    try {
      await loadTreeChildren(node, childList, twisty, true);
    } catch (error) {
      toast(error.message || '目录下一页加载失败', true);
      button.disabled = false;
    }
  };
  item.appendChild(button);
  childList.appendChild(item);
}


async function loadTreeChildren(node, childList, twisty, append = false) {
  if (!state.scan?.scan_id || !node?._tree_key) return;
  const treeKind = state.activeTree === 'analysis' ? 'analysis' : 'physical';
  const treeFilter = treeKind === 'analysis' ? ($('treeFilter')?.value || 'all') : 'all';
  const offset = append ? Number(node._children_next_offset || 0) : 0;
  const data = await api(
    `/api/tree/${state.scan.scan_id}?kind=${treeKind}`
    + `&filter=${encodeURIComponent(treeFilter)}`
    + `&node_key=${encodeURIComponent(node._tree_key)}&offset=${offset}&limit=200`
  );
  const pageNode = data.node || {};
  const children = pageNode.children || [];
  if (!append) {
    childList.innerHTML = '';
    node.children = [];
  } else {
    childList.querySelectorAll(':scope > .tree-load-more').forEach((item) => item.remove());
  }
  children.forEach((child) => {
    node.children.push(child);
    childList.appendChild(renderTreeNode(child));
  });
  node._children_total = pageNode._children_total;
  node._children_next_offset = pageNode._children_next_offset;
  node._children_loaded = true;
  childList.style.display = '';
  twisty.textContent = '▾';
  appendTreePageControl(node, childList, twisty);
}


function renderTreeNode(node) {
  const li =
    document.createElement('li');

  const row =
    document.createElement('div');

  row.className =
    'tree-row';

  row.dataset.path =
    node.path || '';

  if (node.node_id) {
    row.dataset.nodeId =
      node.node_id;
  }

  setupTreeDrag(row, node);

  const twisty =
    document.createElement('span');

  twisty.className =
    'twisty';

  if (canExportNode(node)) {
    const picker = document.createElement('input');
    picker.type = 'checkbox';
    picker.className = 'tree-picker';
    picker.checked = state.selectedNodes.has(exportSelectionKey(node));
    picker.title = '加入组合导出';
    picker.onclick = (event) => {
      event.stopPropagation();
      const key = exportSelectionKey(node);
      if (picker.checked) {
        state.selectedNodes.set(key, exportPayloadNode(node));
      } else {
        state.selectedNodes.delete(key);
      }
      updateSelectionCart();
    };
    row.appendChild(picker);
  }

  const loadedChildren = Array.isArray(node.children) ? node.children : [];
  const hasChildren = Boolean(node.has_children || loadedChildren.length);

  twisty.textContent =
    hasChildren
      ? (loadedChildren.length ? '▾' : '▸')
      : '';

  const label =
    document.createElement('span');

  label.textContent =
    `${icon(node)}  ${node.name || '未命名节点'}`;

  const meta =
    document.createElement('span');

  meta.className =
    'file-meta';

  if (node.kind === 'directory') {
    meta.textContent =
      `${node.file_count || 0} 文件 / ${node.directory_count || 0} 目录`;
  } else if (node.kind === 'file') {
    const duplicateNote = node.duplicate_role === 'duplicate_alias'
      ? ` · 重复副本 → ${node.duplicate_of || node.canonical_path}`
      : (node.duplicate_aliases?.length ? ` · ${node.duplicate_aliases.length} 个副本` : '');
    const status = node.classification_status === 'unclassified' ? ' · 未分类'
      : node.classification_status === 'failed' ? ' · 解析失败'
      : node.classification_status === 'pending' ? ' · 待分析'
      : node.manual_confirmed ? ' · 人工已确认' : '';
    const confidence = node.classification_confidence != null
      ? ` · 置信度 ${Math.round(Number(node.classification_confidence) * 100)}%` : '';
    const memberships = node.topic_memberships?.length > 1
      ? ` · ${node.topic_memberships.length} 个主题` : '';
    meta.textContent = `${node.size_human || ''}${duplicateNote}${status}${confidence}${memberships}`;
  } else if (node.kind === 'evidence') {
    meta.textContent =
      node.evidence?.page
        ? `第 ${node.evidence.page} 页`
        : (node.evidence?.section || '原文片段');
  } else if (node.kind === 'group') {
    meta.textContent =
      `${node.dimension || '内容主题'} · ${node.file_count || (node.member_paths || []).length || 0} 文件${node.coverage ? ` · 已分析 ${node.coverage.parsed_files || 0}/${node.coverage.inventory_files || 0}` : ''}`;
  } else {
    meta.textContent =
      node.dimension || '';
  }

  row.append(
    twisty,
    label,
    meta
  );

  li.appendChild(row);

  let childList = null;

  if (hasChildren) {
    childList =
      document.createElement('ul');

    loadedChildren.forEach(
      child =>
        childList.appendChild(
          renderTreeNode(child)
        )
    );

    node.children = loadedChildren;
    node._children_loaded = loadedChildren.length > 0;
    if (!loadedChildren.length) childList.style.display = 'none';
    appendTreePageControl(node, childList, twisty);

    li.appendChild(
      childList
    );

    twisty.onclick = async (event) => {
      event.stopPropagation();
      const hidden = childList.style.display === 'none';
      if (!hidden) {
        childList.style.display = 'none';
        twisty.textContent = '▸';
        return;
      }
      if (!node._children_loaded && node._tree_key) {
        twisty.textContent = '…';
        try {
          await loadTreeChildren(node, childList, twisty, false);
        } catch (error) {
          twisty.textContent = '▸';
          toast(error.message || '目录加载失败', true);
        }
        return;
      }
      childList.style.display = '';
      twisty.textContent = '▾';
    };
  }

  row.onclick =
    () => selectNode(
      node,
      row
    );

  row.ondblclick = (event) => {
    event.stopPropagation();
    if (state.activeTree !== 'analysis' || node.kind !== 'group' || !node.node_id) return;
    const name = window.prompt('新的主题名称：', node.name || '');
    if (!name || !name.trim()) return;
    submitTreeEdit('rename', { node_id: node.node_id, name: name.trim() }).catch((error) => toast(error.message || '重命名失败', true));
  };
  row.oncontextmenu = (event) => {
    event.preventDefault();
    event.stopPropagation();
    showTreeContextMenu(event, node);
  };

  return li;
}


function renderTree(tree) {
  const host =
    $('tree');

  host.innerHTML = '';

  host.classList.remove(
    'empty'
  );

  const ul =
    document.createElement('ul');

  ul.appendChild(
    renderTreeNode(tree)
  );

  host.appendChild(ul);
}

function updateTreeEditPanel() {
  ensureTreeHistoryControls();
  updateTreeHistoryControls();
  const panel = $('treeEditPanel');
  if (!panel) return;
  const enabled = state.activeTree === 'analysis' && Boolean(state.analysis?.analysis_tree);
  panel.hidden = !enabled;
  if ($('treeFilter')) {
    $('treeFilter').disabled = !enabled;
  }
  const group = state.selected?.kind === 'group';
  const selectedGroups = [...state.selectedNodes.values()].filter((item) => item.kind === 'group' && item.node_id);
  ['treeRenameBtn', 'treeConfirmBtn', 'treeSplitBtn'].forEach((id) => {
    if ($(id)) $(id).disabled = !enabled || !group;
  });
  if ($('treeMountBtn')) $('treeMountBtn').disabled = !enabled || (!group && !(state.selected?.kind === 'file' && selectedGroups.length === 1));
  if ($('treeMergeBtn')) {
    const groups = [...state.selectedNodes.values()].filter((item) => item.kind === 'group');
    $('treeMergeBtn').disabled = !enabled || groups.length < 2;
  }
}

async function submitTreeEdit(operation, payload) {
  if (!state.scan) return;
  const data = await api(`/api/tree-edits/${state.scan.scan_id}?compact=1`, {
    method: 'POST', body: JSON.stringify({ operation, payload, compact: true })
  });
  state.analysis = data.analysis;
  state.treeEdits = data.edits || state.analysis.manual_tree_edits || state.treeEdits;
  state.analysisTreeOriginal = state.analysis.analysis_tree;
  renderTree(state.analysis.analysis_tree);
  updateTreeEditPanel();
  updateTreeHistoryControls();
  toast('目录树人工修改已保存');
}

async function applyTreeFilter() {
  if (!state.scan || state.activeTree !== 'analysis') return;
  const value = $('treeFilter')?.value || 'all';
  try {
    const data = await api(
      `/api/tree/${state.scan.scan_id}?kind=analysis&filter=${encodeURIComponent(value)}&limit=100`
    );
    state.analysis.analysis_tree = data.node || {};
    state.analysisTreeOriginal = value === 'all' ? state.analysis.analysis_tree : state.analysisTreeOriginal;
    renderTree(state.analysis.analysis_tree || {});
    updateTreeEditPanel();
  } catch (error) {
    toast(error.message || '目录筛选失败', true);
  }
}

// Show a physical-tree placeholder immediately after a scan is submitted.
// The worker replaces this skeleton with the complete inventory as soon as
// directory enumeration finishes, while parsing and semantic analysis continue.
function renderInitialPhysicalTree(rootPath) {
  const raw = String(rootPath || '').trim();
  const name = raw.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || raw || '待扫描目录';
  renderTree({
    kind: 'directory',
    name,
    path: raw || '.',
    file_count: 0,
    directory_count: 0,
    children: [],
    scan_pending: true
  });
  const row = $('tree').querySelector('.tree-row');
  if (row) {
    const meta = row.querySelector('.file-meta');
    if (meta) meta.textContent = '正在盘点…';
  }
}


function summaryKey(
  path,
  type = 'folder'
) {
  return `${type}:${path}`;
}


function localSummaryFor(node) {
  if (node.kind === 'file') {
    return state.summaries.get(summaryKey(node.path, 'deep_file_summary')) || state.summaries.get(summaryKey(node.path, 'preliminary_file_summary')) || state.summaries.get(summaryKey(node.path, 'file')) || null;
  }

  if (node.kind === 'directory') {
    return (
        state.summaries.get(summaryKey(node.path, 'deep_node_summary'))
        || state.summaries.get(summaryKey(node.path, 'preliminary_node_summary'))
        ||
      state.summaries.get(
        summaryKey(
          node.path,
          'folder'
        )
      )
      ||
      state.analysis
        ?.node_summaries
        ?.[node.path]
    );
  }

  /*
   * 新增：
   * 虚拟主题节点现在拥有 node_id，
   * 因此可以拥有自己独立的摘要。
   */
  if (node.kind === 'group' && node.node_id) {
    const cached =
        state.summaries.get(summaryKey(`node:${node.node_id}`, 'deep_node_summary'))
        || state.summaries.get(summaryKey(`node:${node.node_id}`, 'preliminary_node_summary'))
        ||
      state.summaries.get(
        summaryKey(
          `node:${node.node_id}`,
          'folder'
        )
      );

    if (cached) {
      return cached;
    }

      return {
      title:
        `${node.name} 分析节点`,

      summary:
        node.summary,

      research_value:
        node.research_value || node.question_value || '',

      research_questions:
        node.research_questions || node.questions || [],

      conclusions:
        node.conclusions || node.conclusion_evidence || [],

      conflicts:
        node.conflicts || [],

      limitations:
        node.limitations || [],

      topics:
        node.related_topics || [],

        representative_documents:
          (
            node.member_paths || []
        ).slice(
          0,
          5
        ),

        evidence_chain:
          node.evidence_chain || [],

        conclusion_evidence:
          node.conclusion_evidence || [],

        file_count:
          node.file_count || 0,
    };
  }

  if (
    node.kind ===
    'analysis_root'
  ) {
    return {
      title:
        `${node.name} 数据包总体`,

      summary:
        node.summary,

      conclusions:
        node.conclusions || node.conclusion_evidence || [],

      topics: [],

      evidence_chain:
        node.evidence_chain || []
    };
  }

  return null;
}

async function hydrateFileConclusions(node, summary, requestStillCurrent, summaryType = 'file') {
  if (!state.scan?.scan_id || node?.kind !== 'file' || !node.path) {
    return summary;
  }
  try {
    const data = await api(
      `/api/file-conclusions/${encodeURIComponent(state.scan.scan_id)}?path=${encodeURIComponent(node.path)}&type=${encodeURIComponent(summaryType)}`
    );
    if (!requestStillCurrent()) return summary;
    const merged = { ...(summary || {}), ...data };
    // The conclusion endpoint intentionally returns only the claim contract;
    // retain the already loaded title/body/structure from the summary page.
    state.summaries.set(summaryKey(node.path, summaryType), merged);
    return merged;
  } catch (_) {
    // A file may still be in parsing or summarization.  The caller will keep
    // the regular document/summary view and show the pending state instead of
    // turning a transient 404 into a hard page error.
    return summary;
  }
}


async function selectNode(
  node,
  row
) {
  const requestId = ++state.selectionRequestId;
  const selectionStillCurrent = () =>
    requestId === state.selectionRequestId
    && state.selected === node;

  document
    .querySelectorAll(
      '.tree-row.selected'
    )
    .forEach(
      el =>
        el.classList.remove(
          'selected'
        )
    );

  row.classList.add(
    'selected'
  );

  state.selected = node;
  state.summary = null;
  renderEvidenceScopeControl();

  /*
   * 换节点以后，
   * 上一次二次检索不能继续使用。
   */
  state.lastRetrievalId = null;

  const isVirtualGroup =
    node.kind === 'group'
    && Boolean(
      node.node_id
    );

  const unsupportedVirtual =
    node.kind ===
      'analysis_root'
    || node.kind === 'evidence'
    ||
    (
      !node.path
      && !isVirtualGroup
    );

  $('selection')
    .classList
    .remove(
      'empty'
    );

  let detailText = '';

  if (
    node.kind ===
    'directory'
  ) {
    detailText =
      `当前层 ${node.direct_file_count || 0} 个文件、`
      +
      `${node.direct_directory_count || 0} 个子目录；`
      +
      `递归共 ${node.file_count || 0} 个文件、`
      +
      `${node.directory_count || 0} 个目录 · `
      +
      `${node.size_human || ''}`;
  } else if (
    node.kind ===
    'file'
  ) {
    detailText =
      `${node.extension || '未知类型'} · `
      +
      `${node.size_human || ''}`;
  } else if (
    isVirtualGroup
  ) {
    detailText =
      `分类维度：${node.dimension || '内容主题'} · `
      +
      `${node.file_count || (node.member_paths || []).length} 个文件`;
  } else {
    detailText =
      `分类维度：${node.dimension || '数据包总体'}`;
  }

  $('selection').innerHTML =
    `<strong>${escapeHtml(node.name || '分析节点')}</strong><br>`
    +
    `${escapeHtml(
      node.path
      ||
      (
        isVirtualGroup
          ? '自适应主题节点'
          : '数据包总体'
      )
    )}<br>`
    +
    `${escapeHtml(detailText)}`;

  /*
   * 核心修改：
   *
   * 原来 group 一律不能点摘要。
   * 现在只要 group 有 node_id，
   * 就允许进行模型摘要。
   */
  $('summaryBtn').disabled =
    unsupportedVirtual
    ||
    state.modelGenerationEnabled
      === false;

  $('exportBtn').disabled =
    unsupportedVirtual
    && !state.selectedNodes.size;

  $('deepenBtn').disabled =
    unsupportedVirtual
    || !state.analysis
    || !(
      state.analysis?.coverage?.pending_files
      || node.coverage?.pending_files
      || node.kind === 'directory'
    );

  $('retrievalBtn').disabled =
    !state.scan;
  if ($('fileSearchBtn')) $('fileSearchBtn').disabled = !state.scan;
  if ($('numericQuestionBtn')) {
    $('numericQuestionBtn').disabled = !state.scan;
  }

  let local = localSummaryFor(node);
  if (state.scan && (node.kind === 'directory' || node.kind === 'file' || isVirtualGroup)) {
    const summaryPath = isVirtualGroup ? `node:${node.node_id}` : (node.path || '.');
    const summaryTypes = node.kind === 'file'
      ? ['deep_file_summary', 'preliminary_file_summary', 'file']
      : ['deep_node_summary', 'preliminary_node_summary', 'folder'];
    try {
      // Always re-read the durable summary; the tree can still contain a preview.
      for (const summaryType of summaryTypes) {
        const page = await api(
          `/api/summaries/${state.scan.scan_id}?path=${encodeURIComponent(summaryPath)}&type=${summaryType}&limit=1`
        );
        if (!selectionStillCurrent()) return;
        const item = (page.items || [])[0];
        if (item) {
          const stagedPayload = { ...item.payload, summary_type: item.type };
          state.summaries.set(summaryKey(item.path, item.type), stagedPayload);
          local = stagedPayload;
          break;
        }
      }
    } catch (_) {
      // A missing local summary is valid while analysis is still running.
      if (!selectionStillCurrent()) return;
    }
  }

  if (!selectionStillCurrent()) return;

  if (node.kind === 'file') {
    const summaryType = local?.summary_type || (
      state.summaries.has(summaryKey(node.path, 'deep_file_summary'))
        ? 'deep_file_summary'
        : state.summaries.has(summaryKey(node.path, 'preliminary_file_summary'))
          ? 'preliminary_file_summary'
          : 'file'
    );
    local = await hydrateFileConclusions(node, local, selectionStillCurrent, summaryType);
    if (!selectionStillCurrent()) return;
  }

  if (local) {
    renderSummary(
      local,
      isVirtualGroup
        ? '主题节点摘要'
        : '本地节点简易摘要'
    );
  } else if (node.kind === 'evidence') {
    renderSummary(
      {
        title: '可回查原文证据',
        summary: node.summary,
        evidence_chain: [node.evidence].filter(Boolean),
        representative_documents: [node.source_path].filter(Boolean)
      },
      '证据节点'
    );
  } else if (
    node.kind === 'file'
    && state.scan
  ) {
    $('summary').className =
      'summary';

    $('summary').textContent =
      '正在读取 Docling 统一解析结果…';

    try {
      const data =
        await api(
          `/api/document/${state.scan.scan_id}?path=${encodeURIComponent(node.path)}`
        );

      if (!selectionStillCurrent()) return;

      renderDocument(
        data.document
      );
      focusPendingEvidenceLocation(node.path);

    } catch (e) {
      if (!selectionStillCurrent()) return;
      $('summary').className =
        'summary empty';

      $('summary').textContent =
        '该文件尚未完成统一解析，可等待完整分析结束后重试。';
    }

  } else {
    $('summary').className =
      'summary empty';

    $('summary').textContent =
      '该节点尚无本地摘要。';
  }
  updateTreeEditPanel();
}


function evidenceHtml(items) {
  if (
    !Array.isArray(items)
    || !items.length
  ) {
    return (
      '<p class="muted">'
      +
      '暂无可引用正文证据；相关结论应人工复核。'
      +
      '</p>'
    );
  }

  return (
    `<div class="evidence-list">${
      items.map(
        item => {
          const loc = [
            item.source_path,

            item.page
              ? `第 ${item.page} 页`
              : '',

            item.section || ''

            ,item.paragraph_index != null
              ? `第 ${Number(item.paragraph_index) + 1} 段`
              : ''

            ,item.block_index != null
              ? `块 ${Number(item.block_index) + 1}`
              : ''

            ,item.char_start != null
              ? `字符 ${item.char_start}-${item.char_end != null ? item.char_end : '?'}`
              : ''
          ]
            .filter(Boolean)
            .join(' · ');

          const relevance =
            item.retrieval_score
              != null

              ? (
                  `<small>检索相关度：${
                    Math.round(
                      item.retrieval_score
                      * 1000
                    ) / 10
                  }%</small><br>`
                )

              : '';

          const quote =
            item.supporting_quote
            && item.supporting_quote !== item.text
              ? (`<p><strong>支撑原句：</strong>${escapeHtml(item.supporting_quote)}</p>`)
              : '';

          const supportReason =
            item.support_reason
            || item.evidence_quality?.reason;

          const supportType =
            item.support_type
            || '';

          const supportStatus = ({
            supported: '已核验支撑',
            partially_supported: '部分支撑，需复核',
            insufficient: '证据不足'
          })[item.support_status] || '';

          const matchType = ({
            fulltext: '全文命中',
            full_text: '全文命中',
            summary: '摘要命中',
            metadata: '元数据命中',
            relationship: '关系命中',
            relation: '关系命中',
          })[String(item.match_type || '').toLowerCase()] || item.match_type || '';

          const sourcePath = item.source_path || item.archive_source_path || '';
          const sourceLocation = {
            page: item.page ?? null,
            section: item.section || '',
            paragraph_index: item.paragraph_index ?? null,
            block_index: item.block_index ?? null,
            char_start: item.char_start ?? null,
            char_end: item.char_end ?? null
          };

          return (
            `<article class="evidence-card">`
            +
            `<div class="evidence-id">${
              escapeHtml(
                item.evidence_id
                || '元数据证据'
              )
            }</div>`
            +
            (matchType ? `<span class="evidence-match-type">${escapeHtml(matchType)}</span>` : '')
            +
            `<strong>${
              escapeHtml(
                loc
                || '未知位置'
              )
            }</strong>`
            +
            (sourcePath
              ? `<span class="evidence-card-actions"><button type="button" class="evidence-source-link" data-evidence-source="${escapeHtml(sourcePath)}" data-evidence-location="${escapeHtml(JSON.stringify(sourceLocation))}">回查原文</button><button type="button" class="evidence-prioritize-link" data-evidence-prioritize="${escapeHtml(sourcePath)}">优先深析</button></span>`
              : '')
            +
            `<p>${
              escapeHtml(
                item.text
                || item.fact
                || ''
              )
            }</p>`
            +
            quote
            +
            (
              supportReason
                ? `<small>入选原因：${escapeHtml(supportReason)}</small><br>`
                : ''
            )
            +
            (
              supportType
                ? `<small>证据类型：${escapeHtml(supportType)}</small><br>`
                : ''
            )
            +
            (
              supportStatus
                ? `<small>核验状态：${escapeHtml(supportStatus)}</small><br>`
                : ''
            )
            +
            relevance
            +
            (
              item.source_sha256

                ? (
                    `<small>源 SHA-256：${
                      escapeHtml(
                        item.source_sha256.slice(
                          0,
                          20
                        )
                      )
                    }…</small>`
                  )

                : ''
            )
            +
            `</article>`
          );
        }
      ).join('')
    }</div>`
  );
}

function retrievalStatusHtml(result) {
  const status = result?.search_status || {};
  const code = String(status.code || (result?.result_count ? 'matched' : 'no_match'));
  const labels = {
    matched: '已找到可回查证据',
    no_match: '当前范围未发现匹配证据',
    partial_index: '检索范围尚未完成索引',
    index_unavailable: '检索索引暂不可用',
  };
  const coverage = status.coverage || result?.coverage || {};
  const types = result?.match_type_counts || {};
  const typeLabels = { fulltext: '全文', full_text: '全文', summary: '摘要', metadata: '元数据', relationship: '关系', relation: '关系' };
  const typeSummary = Object.entries(types)
    .filter(([, count]) => Number(count) > 0)
    .map(([type, count]) => `${typeLabels[String(type).toLowerCase()] || type} ${count}`)
    .join(' · ');
  const coverageSummary = [
    coverage.searchable_files != null ? `可搜索 ${coverage.searchable_files}` : '',
    coverage.scope_files != null ? `范围文件 ${coverage.scope_files}` : '',
    coverage.deep_analyzed_files != null ? `深析 ${coverage.deep_analyzed_files}` : '',
  ].filter(Boolean).join(' · ');
  return `<div class="retrieval-status retrieval-status-${escapeHtml(code)}" role="status">
    <strong>${escapeHtml(labels[code] || '检索状态')}</strong>
    <span>${escapeHtml(status.message || (code === 'no_match' ? '这不代表未处理文件中不存在相关内容；可继续深析或扩大范围。' : ''))}</span>
    ${typeSummary ? `<small>命中来源：${escapeHtml(typeSummary)}</small>` : ''}
    ${coverageSummary ? `<small>覆盖：${escapeHtml(coverageSummary)}</small>` : ''}
  </div>`;
}

function renderFileSearchResult(result) {
  const mount = $('fileSearchResultMount');
  if (!mount) return;
  const files = Array.isArray(result?.matched_files) ? result.matched_files : [];
  const warnings = Array.isArray(result?.warnings) ? result.warnings : [];
  const complete = result?.coverage?.complete !== false;
  const answer = result?.answer || {};
  const matchedCount = Number(answer.matched_file_count ?? result?.matched_file_count ?? 0);
  const evidenceCount = Number(answer.evidence_count ?? result?.evidence_count ?? 0);
  const scopeCount = answer.scope_file_count ?? result?.coverage?.scope_file_count;
  const scopeLabel = scopeCount == null ? '' : ` · 当前范围 ${Number(scopeCount)} 个文件`;
  let html = `<section class="file-search-result"><div class="file-search-conclusion"><strong>${escapeHtml(answer.conclusion || result?.conclusion || '搜索完成')}</strong><span>命中文件 ${matchedCount} 个${scopeLabel} · 相关证据 ${evidenceCount} 条</span>${complete ? '' : '<small>当前结果基于已建立的索引范围，索引完成后可复查。</small>'}</div>`;
  if (files.length) {
    html += `<div class="file-search-list">${files.map((file) => {
      const snippets = Array.isArray(file.snippets) ? file.snippets : [];
      const pages = Array.isArray(file.pages) && file.pages.length ? ` · 页码 ${escapeHtml(file.pages.join('、'))}` : '';
      const level = file.analysis_level === 'deep' ? '深析完成' : '已有索引';
      return `<article class="file-search-card"><div class="file-search-card-head"><div><strong>${escapeHtml(file.name || file.path || '文件')}</strong><small>${escapeHtml(file.path || '')}</small></div><span>${escapeHtml(level)} · 命中 ${Number(file.match_count || 0)} 次${pages}</span></div><div class="file-search-actions"><button type="button" class="evidence-source-link" data-evidence-source="${escapeHtml(file.path || '')}" data-evidence-location="{}">回查原文</button><button type="button" class="evidence-prioritize-link" data-evidence-prioritize="${escapeHtml(file.path || '')}">优先深析</button></div>${snippets.map((text) => `<p>${escapeHtml(text)}</p>`).join('')}</article>`;
    }).join('')}</div>`;
  } else {
    html += '<p class="muted">当前范围没有命中文件。</p>';
  }
  if (result?.has_more || Number(result?.page || 1) > 1) {
    html += `<div class="file-search-pager"><button type="button" data-file-search-page="prev" ${Number(result.page || 1) <= 1 ? 'disabled' : ''}>上一页</button><span>第 ${Number(result.page || 1)} 页 · 当前显示 ${Number(result.displayed_file_count || 0)} 个</span><button type="button" data-file-search-page="next" ${result?.has_more ? '' : 'disabled'}>下一页</button></div>`;
  }
  if (warnings.length) html += `<ul class="file-search-warnings">${warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`;
  mount.innerHTML = html + '</section>';
}


function renderDocument(doc) {
  const structure =
    doc.structure || {};

  const parser =
    doc.parser || {};

  const resultLabel =
    parser.mode === 'fast'
      ? '快速解析结果'
      : 'Docling 统一文档结果';

  let html =
    `<div class="summary-kicker">${resultLabel}</div>`
    +
    `<h2>${
      escapeHtml(
        structure.title
        || doc.source?.name
        || '文档'
      )
    }</h2>`;

  html +=
    `<div class="metric-grid">`
    +
    `<div><b>${escapeHtml(parser.name || '未知')}</b><span>解析器</span></div>`
    +
    `<div><b>${structure.page_count ?? '—'}</b><span>页/张</span></div>`
    +
    `<div><b>${structure.table_count || 0}</b><span>表格</span></div>`
    +
    `<div><b>${doc.evidence_count || 0}</b><span>证据项</span></div>`
    +
    `</div>`;

  if (doc.coverage) {
    const ratio =
      doc.coverage.coverage_ratio
        == null

        ? '未知'

        : `${
            Math.round(
              doc.coverage.coverage_ratio
              * 10000
            ) / 100
          }%`;

    const reason =
      doc.coverage
        .coverage_ratio_reason

        ? `；${doc.coverage.coverage_ratio_reason}`

        : '';

    html +=
      `<p><strong>正文覆盖：</strong>`
      +
      `${
        doc.coverage.complete
          ? '完整'
          : '存在截断'
      }；`
      +
      `已保存 ${
        doc.coverage.stored_characters
        || 0
      } 字符；`
      +
      `覆盖率 ${ratio}${reason}`
      +
      `${
        doc.coverage
          .embedded_ocr_characters

          ? (
              `；内嵌图片 OCR ${
                doc.coverage
                  .embedded_ocr_characters
              } 字符`
            )

          : ''
      }`
      +
      `</p>`;
  }

  const profile = doc.data_profile || (doc.data_profiles && doc.data_profiles[0]?.profile);
  if (profile && profile.status !== "skipped" && profile.status !== "failed") {
    const judgment = profile.value_judgment || {};
    const profileCoverage = profile.coverage || profile.limits || {};
    const partialNote = profile.status === "partial" || profileCoverage.complete === false
      ? `；有界采样${profileCoverage.truncation_reasons?.length ? `（${escapeHtml(profileCoverage.truncation_reasons.join('、'))}）` : ''}，统计结果需回原表复核`
      : '';
    html += `<div class="coverage-card"><strong>结构化数据画像：</strong>${profile.row_count ?? 0} 行 / ${profile.column_count ?? 0} 列；质量评分 ${profile.quality_score ?? "—"} / 100；价值判断 ${escapeHtml(judgment.value_level || "—")}。` +
      `${profile.duplicate_row_count ? `重复行 ${profile.duplicate_row_count}；` : ""}${profile.missing_columns?.length ? `缺失字段 ${profile.missing_columns.length} 个；` : ""}${profile.sensitive_columns?.length ? `敏感字段 ${profile.sensitive_columns.length} 个，建议脱敏。` : ""}</div>`;
    if (partialNote) html += `<p class="coverage-card"><strong>画像覆盖提示：</strong>${partialNote}</p>`;
  }

  if (
    structure.headings?.length
  ) {
    html +=
      `<h3>结构目录</h3><ul>${
        structure.headings
          .slice(
            0,
            30
          )
          .map(
            x =>
              `<li>${escapeHtml(x)}</li>`
          )
          .join('')
      }</ul>`;
  }

  if (doc.text_preview) {
    html +=
      `<h3>正文预览</h3>`
      +
      `<pre>${
        escapeHtml(
          doc.text_preview
        )
      }</pre>`;
  }

  if (
    doc.warnings?.length
  ) {
    html +=
      `<h3>解析告警</h3><ul>${
        doc.warnings
          .map(
            x =>
              `<li>${escapeHtml(x)}</li>`
          )
          .join('')
      }</ul>`;
  }

  html +=
    `<h3>证据链（主题相关代表片段，最多 12 条）</h3>`
    +
    evidenceHtml(
      doc.evidence
    );

  $('summary').className =
    'summary';

  $('summary').innerHTML =
    html;
}

function focusPendingEvidenceLocation(path) {
  const pending = state.pendingEvidenceLocation;
  if (!pending || pending.path !== path) return;
  state.pendingEvidenceLocation = null;
  const location = pending.location || {};
  const preview = $('summary')?.querySelector('pre');
  if (!preview) return;
  const text = preview.textContent || '';
  let start = Number(location.char_start);
  let end = Number(location.char_end);
  if (!Number.isFinite(start) && Number.isFinite(Number(location.paragraph_index))) {
    const index = Math.max(0, Number(location.paragraph_index));
    const lines = text.split(/\r?\n/);
    start = lines.slice(0, index).reduce((total, line) => total + line.length + 1, 0);
    end = start + (lines[index] || '').length;
  }
  if (Number.isFinite(start) && start >= 0 && start < text.length) {
    end = Number.isFinite(end) && end > start ? Math.min(end, text.length) : Math.min(text.length, start + 320);
    preview.innerHTML = `${escapeHtml(text.slice(0, start))}<mark class="evidence-location-highlight">${escapeHtml(text.slice(start, end))}</mark>${escapeHtml(text.slice(end))}`;
    const mark = preview.querySelector('.evidence-location-highlight');
    window.requestAnimationFrame(() => mark?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
    return;
  }
  window.requestAnimationFrame(() => preview.scrollIntoView({ behavior: 'smooth', block: 'center' }));
}


function renderSummary(
  data,
  kicker = '分析结果'
) {
  const host =
    $('summary');

  host.className =
    'summary';

  let html =
    `<div class="summary-kicker">${
      escapeHtml(kicker)
    }</div>`
    +
    `<h2>${
      escapeHtml(
        data.title
        || '分析结果'
      )
    }</h2>`;

  const generatedBy = String(data.generated_by || '').toLowerCase();
  const analysisDepth = String(data.analysis_depth || '').toLowerCase();
  const analysisStage = String(data.analysis_stage || '').toLowerCase();
  const isPreliminary = analysisStage === 'preliminary'
    || analysisDepth === 'preliminary_document'
    || analysisDepth === 'preliminary_node';
  const isPreliminaryNode = analysisDepth === 'preliminary_node';
  const isModelDeep = !isPreliminary
    && (generatedBy === 'model-deep-analysis'
      || generatedBy === 'model'
      || data.deep_analysis === true);
  const provenanceLabel = isPreliminary
    ? (isPreliminaryNode ? '节点初步摘要' : '模型初步摘要')
    : isModelDeep
      ? (analysisDepth === 'deep_folder' ? '模型深度分析 · 节点范围' : '模型深度分析 · 全文')
    : generatedBy === 'local-unified-parser'
      ? '本地解析预览'
      : generatedBy === 'local-inventory'
        ? '文件盘点信息'
        : '本地降级结果';
  html += `<div class="summary-provenance ${isModelDeep ? 'is-deep' : 'is-preview'}"><span>${escapeHtml(provenanceLabel)}</span>${data.parser_info?.degraded ? '<small>部分步骤未达到完整性门槛，请查看覆盖与告警</small>' : ''}</div>`;

  const summary =
    data.summary
    || data.core_summary;

  if (summary) {
    html += summaryParagraphsHtml(summary);
  }
  html += fileClaimsHtml(data);

  const scopeCoverage =
    data.coverage
    || data.parser_info?.coverage;
  if (scopeCoverage?.inventory_files != null) {
    const ratio = scopeCoverage.parsed_file_ratio == null
      ? '—'
      : `${Math.round(scopeCoverage.parsed_file_ratio * 10000) / 100}%`;
    html += `<p class="coverage-card"><strong>该节点分析覆盖：</strong>${escapeHtml(scopeCoverage.status || scopeCoverage.mode || '—')}；已解析 ${scopeCoverage.parsed_files ?? 0}/${scopeCoverage.inventory_files ?? scopeCoverage.total_files ?? 0}（${ratio}），抽样 ${scopeCoverage.sampled_files ?? scopeCoverage.sampled_overview_files ?? 0}，深度分析 ${scopeCoverage.deep_analyzed_files ?? 0}，待处理 ${scopeCoverage.pending_files || 0}，失败 ${scopeCoverage.failed_files || 0}；${scopeCoverage.complete_analysis ? '可视为完整分析' : '当前为部分覆盖，不能视为全文分析'}</p>`;
    if (scopeCoverage.limitations?.length) {
      html += `<p class="coverage-card"><strong>覆盖限制：</strong>${escapeHtml(scopeCoverage.limitations.join('；'))}</p>`;
    }
  }

  if (data.statistics) {
    html +=
      `<div class="metric-grid">`
      +
      `<div><b>${
        data.statistics
          .file_count
        ?? '—'
      }</b><span>文件</span></div>`
      +
      `<div><b>${
        data.statistics
          .page_count
        ?? '—'
      }</b><span>页/张</span></div>`
      +
      `<div><b>${
        data.statistics
          .table_count
        ?? 0
      }</b><span>表格</span></div>`
      +
      `<div><b>${
        data.statistics
          .degraded_document_count
        ?? 0
      }</b><span>降级项</span></div>`
      +
      `</div>`;
  }

  if (
    data.file_count != null
    && !data.statistics
  ) {
    html +=
      `<div class="metric-grid">`
      +
      `<div><b>${data.file_count}</b><span>节点文件</span></div>`
      +
      `</div>`;
  }

  const topicValues = summaryTopicValues(data.topics);
  if (topicValues.length) {
    html +=
      `<h3>内容主题</h3><div class="summary-topic-list">${
        topicValues
          .map((x) => `<span class="tag">${escapeHtml(x)}</span>`)
          .join('')
      }</div>`;
  }

  const verifiedFileClaimFields = new Set(['key_facts', 'arguments', 'methodology', 'conclusions']);
  for (
    const [key, label]
    of [
      ['key_facts', '关键事实'],
      ['arguments', '主要论点'],
      ['methodology', '研究方法'],
      ['conclusions', '结论'],
      ['notable_items', '值得注意'],
      ['uncertainties', '不确定信息'],
      ['limitations', '局限'],
      ['warnings', '告警']
    ]
  ) {
    if (data.claim_contract === 'file-claims/1.0' && verifiedFileClaimFields.has(key)) continue;
    if (
      Array.isArray(data[key])
      && data[key].length
    ) {
      html +=
        `<h3>${label}</h3><ul>${
          data[key]
            .map(
              x =>
                `<li>${escapeHtml(
                  displayText(x)
                )}</li>`
            )
            .join('')
        }</ul>`;
    }
  }

  if (
    data.structure_overview
  ) {
    html +=
      `<h3>结构概览</h3>`
      + summaryStructureHtml(data.structure_overview);
  }

  if (
    data.representative_documents
      ?.length
  ) {
    html +=
      `<h3>代表文档</h3><ul>${
        data.representative_documents
          .map(
            x =>
              `<li>${escapeHtml(x)}</li>`
          )
          .join('')
      }</ul>`;
  }

  if (
    data.member_paths?.length
  ) {
    html +=
      `<details>`
      +
      `<summary>查看该主题包含的 ${
        data.member_paths.length
      } 个文件</summary>`
      +
      `<ul>${
        data.member_paths
          .map(
            x =>
              `<li>${escapeHtml(x)}</li>`
          )
          .join('')
      }</ul>`
      +
      `</details>`;
  }

  if (
    data.recommended_research_direction
  ) {
    const d =
      data.recommended_research_direction;

    const questions =
      d.research_questions
      || d.questions;

    html +=
      `<h3>推荐研究方向 <span class="inference-badge">推论</span></h3>`
      +
      `<p><strong>${
        escapeHtml(
          d.title
          || '待确定'
        )
      }</strong></p>`
      +
      `<p>${
        escapeHtml(
          d.rationale
          || ''
        )
      }</p>`;

    const directionScore = Number(d.score);
    const directionScoreText = Number.isFinite(directionScore) ? String(directionScore) : '—';
    html += `<p class="coverage-card"><strong>优先级：</strong>${escapeHtml(displayText(d.priority, '—'))}；<strong>评分：</strong>${escapeHtml(directionScoreText)}；<strong>证据状态：</strong>${escapeHtml(displayText(d.evidence_status || (d.evidence_chain?.length ? 'supported' : 'insufficient'), 'insufficient'))}</p>`;

    if (
      questions?.length
    ) {
      html +=
        `<ol>${
          questions
            .map(
              x =>
                `<li>${escapeHtml(x)}</li>`
            )
            .join('')
        }</ol>`;
    }
    if (d.methods?.length) {
      html += `<p><strong>建议方法：</strong>${escapeHtml(d.methods.join('；'))}</p>`;
    }
    html += d.evidence_chain?.length
      ? evidenceHtml(d.evidence_chain)
      : `<p class="coverage-card"><strong>证据状态：</strong>当前没有达到质量门槛的正文证据，建议先补充解析后再下结论。</p>`;
  }

  const qa = data.question_answer_evidence;
  if (qa) {
    html += `<h3>问题—回答—证据</h3><section class="conclusion-evidence">` +
      `<p><strong>问题：</strong>${escapeHtml(qa.question || data.question || '—')}</p>` +
      `<p><strong>价值：</strong>${escapeHtml(qa.value || data.value || '—')}</p>` +
      `<p><strong>回答：</strong>${escapeHtml(qa.answer || data.answer || '—')}</p>` +
      `<p><strong>证据状态：</strong>${escapeHtml(data.evidence_status || (qa.evidence?.length ? 'supported' : 'insufficient'))}</p>` +
      (qa.claims?.length ? `<ul>${qa.claims.map(claim => `<li>${escapeHtml(claim.statement || '')}（${escapeHtml(claim.support_status || 'insufficient')}）${claim.evidence_ids?.length ? ` · ${escapeHtml(claim.evidence_ids.join(', '))}` : ''}</li>`).join('')}</ul>` : '') +
      (qa.evidence?.length ? evidenceHtml(qa.evidence) : `<p class="coverage-card">没有有效正文证据支撑当前回答。</p>`) +
      `</section>`;
  }

  if (
    data.conclusion_evidence?.length
  ) {
    html +=
      `<h3>问题—回答—证据链</h3>`
      +
      data.conclusion_evidence
        .map(
          item =>
            `<section class="conclusion-evidence">`
            +
            `<p><strong>问题：</strong>${escapeHtml(item.analysis_question || '该范围内有哪些可回查的关键判断？')}</p>`
            +
            `<p><strong>价值：</strong>${escapeHtml(item.question_value || '帮助判断该资料范围是否值得继续分析，并明确后续核查重点。')}</p>`
            +
            `<p><strong>回答：</strong>${escapeHtml(displayText(item))} <span class="inference-badge">${escapeHtml(item.confidence || (item.verification_status === 'candidate' ? '初步分析' : '待核验'))}</span></p>`
            +
            `<p>${escapeHtml(item.basis || '该结论由下列证据支撑。')}</p>`
            +
            evidenceHtml(item.evidence || [])
            +
            `</section>`
        )
        .join('');
  }

  const evidence =
    data.evidence_chain
    || data.evidence;

  if (
    evidence?.length
  ) {
    html +=
      `<h3>证据链</h3>`
      +
      evidenceHtml(
        evidence
      );
  }

  if (
    data.parser_info
  ) {
    html +=
      `<details>`
      +
      `<summary>处理信息</summary>`
      +
      `<pre>${
        escapeHtml(
          JSON.stringify(
            data.parser_info,
            null,
            2
          )
        )
      }</pre>`
      +
      `</details>`;
  }

  host.innerHTML =
    html;
}


function updateStats() {
  if (!state.scan) {
    return;
  }

  // A retry/deepening run can coexist with the previous final analysis. Only
  // prefer its live card while that exact task is still active; persisted
  // progress rows may otherwise remain at 95% after the final result is saved.
  const currentJob = state.jobId ? state.jobs.get(state.jobId) : null;
  const useProgressiveAnalysis = Boolean(
    state.progressiveAnalysis
    && currentJob
    && ACTIVE_JOB_STATUSES.has(currentJob.status)
  );
  const displayedAnalysis = useProgressiveAnalysis
    ? state.progressiveAnalysis
    : (state.analysis || state.progressiveAnalysis || {});
  const a =
    displayedAnalysis
      ?.statistics
    ||
    state.scan.analysis
    ||
    {};
  const coverage =
    displayedAnalysis?.coverage
    || {};
  const ratio =
    (coverage.parsed_file_ratio ?? coverage.content_parse_ratio) == null
      ? '—'
      : `${Math.round((coverage.parsed_file_ratio ?? coverage.content_parse_ratio) * 10000) / 100}%`;
  const inventoryRatio = coverage.inventory_coverage_ratio == null
    ? (coverage.inventory_coverage?.complete ? '100%' : '待确认')
    : `${Math.round(coverage.inventory_coverage_ratio * 10000) / 100}%`;
  const deepRatio = coverage.deep_analysis_ratio == null
    ? (coverage.inventory_files ? `${Math.round((coverage.deep_analyzed_files || 0) / coverage.inventory_files * 10000) / 100}%` : '—')
    : `${Math.round(coverage.deep_analysis_ratio * 10000) / 100}%`;

  $('scanStats')
    .classList
    .remove(
      'empty'
    );

  $('scanStats').innerHTML =
    `<div class="metric-grid">`
    +
    `<div><b>${state.scan.file_count}</b><span>递归文件</span></div>`
    +
    `<div><b>${state.scan.directory_count || 0}</b><span>子目录</span></div>`
    +
    `<div><b>${escapeHtml(state.scan.total_size_human)}</b><span>总大小</span></div>`
    +
    `<div><b>${a.evidence_items ?? '—'}</b><span>证据项</span></div>`
    +
    `<div><b>${a.structured_profiled_files ?? '—'}</b><span>结构化画像</span></div>`
    +
    `</div>`
    +
    `<p>`
    +
    `精确重复组：${a.exact_duplicate_groups ?? '分析中'}；`
    +
    `可合并重复文件：${a.exact_duplicate_files ?? '—'}；`
    +
    `相似文档簇：${a.similar_document_clusters ?? '—'}；`
    +
    `语义主题：${a.semantic_topic_clusters ?? a.topic_clusters ?? '—'}`
    +
    `</p>`;
  const overview = displayedAnalysis?.overview || {};
  const judgment = displayedAnalysis?.value_judgment || {};
  if (overview.file_count != null || judgment.level) {
    const usability = judgment.data_usability || {};
    const richness = judgment.information_richness || {};
    const potential = judgment.research_potential || {};
    const relevance = judgment.task_relevance || {};
    $('scanStats').innerHTML +=
      `<div class="coverage-card"><strong>数据概览：</strong>已解析 ${overview.parsed_files ?? a.parsed_files ?? 0} 个文件，证据 ${overview.evidence_count ?? a.evidence_items ?? 0} 条；` +
      `规范文档 ${judgment.canonical_document_count ?? a.canonical_documents ?? '—'} 份，重复副本 ${judgment.duplicate_alias_count ?? a.exact_duplicate_files ?? 0} 份。<br>` +
      `<strong>四维判断：</strong>数据可用性 ${escapeHtml(usability.level || '—')} · 信息丰富度 ${escapeHtml(richness.level || '—')} · 研究潜力 ${escapeHtml(potential.level || judgment.research_value || '待分析')} · 任务相关性 ${escapeHtml(relevance.level || '未评估')}` +
      `${judgment.limitations?.length ? `<br><small>${escapeHtml(judgment.limitations.join('；'))}</small>` : ''}</div>`;
  }
  if (coverage.inventory_files != null) {
    $('scanStats').innerHTML +=
      `<div class="coverage-card"><strong>分析覆盖：${escapeHtml(coverage.status || '—')}</strong> · ${escapeHtml(coverage.coverage_level_label || '覆盖等级未标注')}<br>` +
      `清点覆盖 ${inventoryRatio} · 内容解析 ${ratio} · 全文深度分析 ${deepRatio}<br>` +
      `已解析 ${coverage.parsed_files || 0}/${coverage.inventory_files || 0}` +
      `；抽样 ${coverage.sampled_files ?? coverage.sampled_overview_files ?? 0}；深度分析 ${coverage.deep_analyzed_files ?? 0}` +
      `；待处理 ${coverage.pending_files || 0}；失败 ${coverage.failed_files || 0}` +
      `；${coverage.complete_analysis ? '完整分析' : '部分覆盖'}` +
      `${coverage.large_package_notice ? `<br><small>${escapeHtml(coverage.large_package_notice)}</small>` : ''}</div>`;
    if (coverage.limitations?.length) {
      $('scanStats').innerHTML += `<div class="coverage-card"><strong>覆盖限制：</strong>${escapeHtml(coverage.limitations.join('；'))}</div>`;
    }
    const archiveTotals = coverage.archive_member_totals || {};
    if (archiveTotals.total_members) {
      $('scanStats').innerHTML += `<div class="coverage-card"><strong>压缩包成员覆盖：</strong>已解析 ${archiveTotals.parsed_members || 0}/${archiveTotals.total_members || 0}；跳过 ${archiveTotals.skipped_members || 0}；失败 ${archiveTotals.failed_members || 0}。</div>`;
    }
  }
  if (judgment.dimensions) {
    const labels = { readability: '可读性', completeness: '完整性', uniqueness: '独特性', topic_concentration: '主题集中度', evidence_density: '证据密度', structured_quality: '结构化质量' };
    $('scanStats').innerHTML += `<div class="coverage-card"><strong>价值维度：</strong>${Object.entries(judgment.dimensions).map(([key, value]) => `${escapeHtml(labels[key] || key)} ${escapeHtml(value?.score ?? '—')}`).join(' · ')}</div>`;
  }
  renderPackageProcessing();
}


function formatRatio(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(number * 10000) / 100}%` : '—';
}


function packageSelectedPaths() {
  const values = [...state.selectedNodes.values(), state.selected].filter(Boolean);
  const paths = [];
  values.forEach((item) => {
    if (item.kind === 'file' && item.path) paths.push(String(item.path));
    (item.member_paths || []).forEach((path) => paths.push(String(path)));
  });
  return [...new Set(paths)];
}


function renderPackageProcessing() {
  if (!state.scan || !$('scanStats')) return;
  const currentQuery = $('packagePriorityQuery')?.value || '';
  const keepRunning = $('packageContinueFull')?.checked ?? false;
  const processing = state.processing || {};
  const status = String(processing.state || 'running');
  const active = Boolean(processing.active_job_id);
  const pending = Number(processing.deep_pending_files || 0);
  const retryWaiting = Number(processing.retry_waiting_files || 0);
  const needsAttention = Number(processing.needs_attention_files || 0);
  const selectionCount = packageSelectedPaths().length;
  const importStatus = String(processing.import_task?.status || state.importTask?.status || '').toLowerCase();
  const deepSummaryFiles = Number(processing.deep_summary_files || state.importTask?.checkpoint?.deep_summary_completed || 0);
  const refreshableImport = ['deep_summarizing_files', 'deep_summarizing_nodes', 'deep_update_available'].includes(importStatus);
  const deepReady = Boolean(deepSummaryFiles > 0 && refreshableImport);
  const labels = { running: 'RUNNING', paused: 'PAUSED', completed: 'COMPLETED' };
  const stateText = {
    running: active ? '正在深度解析' : '等待用户选择',
    paused: '已安全暂停',
    completed: '已完成当前批次',
  }[status] || '准备中';
  $('scanStats').insertAdjacentHTML('beforeend',
    `<section class="package-processing-panel" aria-label="导入与深度解析进度">` +
      `<div class="package-processing-heading"><div><span class="section-kicker">IMPORT PIPELINE</span><h3>导入与深度解析进度</h3></div><span class="status-chip" data-status="${escapeHtml(status)}">${escapeHtml(labels[status] || 'WAITING')}</span></div>` +
      `<div class="package-processing-metrics">` +
        `<div><b>${processing.discovery_progress}</b><span>发现进度</span><small>${processing.inventory_files ?? state.scan.file_count ?? 0} 个文件</small></div>` +
        `<div><b>${formatRatio(processing.preview_completion_ratio)}</b><span>轻量预览进度</span><small>${processing.previewed_files ?? 0}/${processing.inventory_files ?? state.scan.file_count ?? 0}</small></div>` +
        `<div><b>${formatRatio(processing.deep_completion_ratio)}</b><span>深度解析进度</span><small>${processing.deep_completed_files ?? 0}/${processing.logical_total_files ?? processing.inventory_files ?? 0}</small></div>` +
        `<div><b>${formatRatio(processing.evidence_index_ratio)}</b><span>证据索引进度</span><small>${processing.evidence_indexed_files ?? 0}/${processing.logical_total_files ?? processing.inventory_files ?? 0}</small></div>` +
      `</div>` +
      `<p><strong>${escapeHtml(stateText)}</strong> · 可立即处理 ${pending} · 等待重试 ${retryWaiting} · 需要处理 ${needsAttention} · 策略排除 ${processing.terminal_excluded_files || 0} · 单批最多 ${processing.batch_file_limit || 500} 个，并受工作量上限保护。</p>` +
      `${processing.reason ? `<small class="package-processing-reason">${escapeHtml(processing.reason)}</small>` : ''}` +
      `<div class="package-processing-actions">` +
        `<button class="primary" data-package-action="continue" ${active || !pending ? 'disabled' : ''}>从检查点继续</button>` +
        `<button class="secondary" data-package-action="recall" ${active || !pending ? 'disabled' : ''}>召回候选文件</button>` +
        `<button data-package-action="selection" ${active || !pending || !selectionCount ? 'disabled' : ''}>已勾选文件优先${selectionCount ? `（${selectionCount}）` : ''}</button>` +
        `<button class="danger" data-package-action="pause" ${!active ? 'disabled' : ''}>结束本次运行</button>` +

        `<button class="primary" data-package-action="refresh-deep-results" ${deepReady ? '' : 'disabled'} title="按当前已完成的深度摘要更新正式结果，未完成任务继续后台运行">更新正式结果</button>` +      `</div>` +
      `<div class="package-priority-query"><input id="packagePriorityQuery" maxlength="1000" value="${escapeHtml(currentQuery)}" placeholder="关键词、人物、机构、编号、时间范围或自然语言研究要求"><button class="secondary" data-package-action="query" ${active || !pending ? 'disabled' : ''}>搜索并优先处理</button></div>` +
      `<label class="package-continue-option"><input id="packageContinueFull" type="checkbox" ${keepRunning ? 'checked' : ''}><span>当前批次完成后继续处理剩余文件</span></label>` +
      `<small class="package-processing-note">召回、检索和手选只改变队列顺序，不删除普通待处理文件；暂停后已完成结果永久保留。</small>` +
    `</section>`
  );
  renderFileWorkflowPanel();
}

const FILE_WORKFLOW_LABELS = {
  all: '全部文件', pending: '待处理', processing: '处理中', completed: '完成',
  partial: '部分完成', failed: '失败', retry_waiting: '等待重试', out_of_scope: '范围外'
};

function normalizedFileStatus(item) {
  const raw = String(item?.display_status || item?.status || item?.workflow_state || '').toLowerCase();
  if (raw.includes('retry') || raw.includes('wait')) return 'retry_waiting';
  if (raw.includes('fail') || raw.includes('error')) return 'failed';
  if (raw.includes('scope') || raw.includes('unsupported') || raw.includes('excluded')) return 'out_of_scope';
  if (raw.includes('partial') || raw.includes('incomplete')) return 'partial';
  if (raw.includes('process') || raw.includes('running')) return 'processing';
  if (raw.includes('complete') || raw.includes('ready') || raw.includes('evidence')) return 'completed';
  return 'pending';
}

function fileWorkflowStatusFilter() {
  return $('fileWorkflowFilter')?.value || state.fileWorkflowFilter || 'all';
}

function fileWorkflowFilterKey() {
  return state.scan?.scan_id ? 'sjfx.fileWorkflowFilters.' + state.scan.scan_id : '';
}

function loadFileWorkflowFilters() {
  const key = fileWorkflowFilterKey();
  if (!key) return;
  try {
    state.fileWorkflowFilters = JSON.parse(localStorage.getItem(key) || '{}') || {};
  } catch (_) {
    state.fileWorkflowFilters = {};
  }
}

function saveFileWorkflowFilters() {
  const key = fileWorkflowFilterKey();
  if (!key) return;
  try { localStorage.setItem(key, JSON.stringify(state.fileWorkflowFilters || {})); } catch (_) {}
}

function collectFileWorkflowFilters(host) {
  const value = (name) => host.querySelector('[data-file-filter="' + name + '"]')?.value?.trim() || '';
  const fromDate = value('modified_from');
  const toDate = value('modified_to');
  return {
    min_size: value('min_size'),
    max_size: value('max_size'),
    min_modified_ns: fromDate ? String(new Date(fromDate + 'T00:00:00').getTime() * 1000000) : '',
    max_modified_ns: toDate ? String(new Date(toDate + 'T23:59:59.999').getTime() * 1000000) : '',
    modified_from: fromDate,
    modified_to: toDate,
    language: value('language'),
    archive_member: value('archive_member'),
    keyword: value('keyword'),
    entity: value('entity'),
    deep: value('deep'),
  };
}

function renderFileWorkflowPanel() {
  const host = $('fileWorkflowPanel');
  if (!host || !state.scan) return;
  const packageView = document.querySelector('[data-view="packages"]');
  if (packageView && host.parentElement !== packageView) packageView.appendChild(host);
  const page = state.fileWorkflowPage || {};
  const allItems = Array.isArray(page.items) ? page.items : [];
  const filter = fileWorkflowStatusFilter();
  const items = filter === 'all'
    ? allItems
    : allItems.filter((item) => normalizedFileStatus(item) === filter);
  const rows = items.map((item) => {
    const status = normalizedFileStatus(item);
    const path = item.node_path || item.path || '';
    const reason = item.reason || item.error || item.failure_reason || item.workflow_reason || '';
    const level = item.analysis_level || (item.formal_evidence_ready ? 'evidence' : 'preview');
    const planName = item.parse_plan?.parser || item.parser_plan?.parser || '';
    const levelLabel = level === 'evidence' ? '正式证据' : (level === 'deep' ? '深度解析' : '轻量预览');
    const levelBadge = '<span class="file-workflow-status status-' + (level === 'evidence' ? 'completed' : 'pending') + '">' + levelLabel + '</span>' + (planName ? ' <small>' + escapeHtml(planName) + '</small>' : '');
    const retry = item.next_retry_at ? ` · 下次重试 ${item.next_retry_at}` : '';
    const retryAction = ['failed', 'retry_waiting', 'partial'].includes(status) && item.accounting_role !== 'container_only'
      ? `<button type="button" class="file-workflow-retry" data-file-retry="${escapeHtml(path)}" title="重新分析此文件">重试</button>`
      : '';
    return `<div class="file-workflow-row" data-file-status="${status}">
      <button type="button" class="file-workflow-path" data-file-open="${escapeHtml(path)}" title="打开文件详情">${escapeHtml(path || '未命名文件')}</button>
      <span class="file-workflow-actions"><span class="file-workflow-status status-${status}">${FILE_WORKFLOW_LABELS[status]}</span>${levelBadge}${retryAction}</span>
      <small>${escapeHtml(reason || `尝试 ${item.attempt_count ?? item.attempts ?? 0} 次${retry}`)}</small>
    </div>`;
  }).join('');
  const total = Number(page.total ?? items.length);
  const offset = Number(state.fileWorkflowOffset || 0);
  host.innerHTML = `<div class="file-workflow-heading"><div><span class="section-kicker">AUDITABLE FILE QUEUE</span><h3>逐文件状态与异常</h3></div>
    <span class="file-workflow-total">${total} 个逻辑文件</span></div>
    <div class="file-workflow-tools"><select id="fileWorkflowFilter" aria-label="文件处理状态筛选">
      ${Object.entries(FILE_WORKFLOW_LABELS).map(([value, label]) => `<option value="${value}" ${value === filter ? 'selected' : ''}>${label}</option>`).join('')}
    </select><button type="button" class="secondary" data-file-workflow-refresh>刷新</button></div>
    <div class="file-workflow-list">${rows || '<div class="file-workflow-empty">当前页没有符合筛选状态的文件；可翻页继续查看。</div>'}</div>
    <div class="file-workflow-pagination"><button type="button" data-file-workflow-page="prev" ${offset <= 0 ? 'disabled' : ''}>上一页</button>
      <span>${total ? `${offset + 1}-${Math.min(offset + allItems.length, total)} / ${total}` : '0 / 0'}</span>
      <button type="button" data-file-workflow-page="next" ${offset + allItems.length >= total ? 'disabled' : ''}>下一页</button></div>`;
  host.insertAdjacentHTML('afterbegin',
    '<div class="file-workflow-advanced-tools">' +
    '<input data-file-filter="min_size" type="number" min="0" placeholder="最小大小(B)" aria-label="最小文件大小" value="' + escapeHtml(state.fileWorkflowFilters.min_size || '') + '">' +
    '<input data-file-filter="max_size" type="number" min="0" placeholder="最大大小(B)" aria-label="最大文件大小" value="' + escapeHtml(state.fileWorkflowFilters.max_size || '') + '">' +
    '<input data-file-filter="modified_from" type="date" aria-label="修改时间起始" value="' + escapeHtml(state.fileWorkflowFilters.modified_from || '') + '">' +
    '<input data-file-filter="modified_to" type="date" aria-label="修改时间结束" value="' + escapeHtml(state.fileWorkflowFilters.modified_to || '') + '">' +
    '<input data-file-filter="language" placeholder="语言" aria-label="语言筛选" value="' + escapeHtml(state.fileWorkflowFilters.language || '') + '">' +
    '<input data-file-filter="archive_member" placeholder="压缩包成员" aria-label="压缩包及成员筛选" value="' + escapeHtml(state.fileWorkflowFilters.archive_member || '') + '">' +
    '<input data-file-filter="keyword" placeholder="轻量关键词" aria-label="轻量关键词筛选" value="' + escapeHtml(state.fileWorkflowFilters.keyword || '') + '">' +
    '<input data-file-filter="entity" placeholder="实体" aria-label="实体筛选" value="' + escapeHtml(state.fileWorkflowFilters.entity || '') + '">' +
    '<select data-file-filter="deep" aria-label="深度解析筛选"><option value="">深度状态不限</option><option value="completed">已完成深度解析</option><option value="pending">未完成深度解析</option></select>' +
    '<button type="button" class="secondary" data-file-workflow-apply>应用筛选</button>' +
    '<button type="button" class="ghost" data-file-workflow-save>保存筛选</button>' +
    '<button type="button" class="ghost" data-file-workflow-restore>恢复筛选</button></div>'
  );
  const deepSelect = host.querySelector('[data-file-filter="deep"]');
  if (deepSelect) deepSelect.value = state.fileWorkflowFilters.deep || '';
  $('fileWorkflowFilter').onchange = () => {
    state.fileWorkflowFilter = $('fileWorkflowFilter').value;
    state.fileWorkflowOffset = 0;
    loadFileWorkflowPage();
  };
  host.querySelector('[data-file-workflow-apply]')?.addEventListener('click', () => {
    state.fileWorkflowFilters = collectFileWorkflowFilters(host);
    state.fileWorkflowOffset = 0;
    loadFileWorkflowPage();
  });
  host.querySelector('[data-file-workflow-save]')?.addEventListener('click', () => {
    state.fileWorkflowFilters = collectFileWorkflowFilters(host);
    saveFileWorkflowFilters();
    toast('文件筛选条件已保存');
  });
  host.querySelector('[data-file-workflow-restore]')?.addEventListener('click', () => {
    loadFileWorkflowFilters();
    state.fileWorkflowOffset = 0;
    renderFileWorkflowPanel();
    loadFileWorkflowPage();
  });
}

async function loadFileWorkflowPage() {
  if (!state.scan?.scan_id) return;
  const requestedScanId = state.scan.scan_id;
  const requestSeq = ++state.fileWorkflowRequestSeq;
  state.fileWorkflowAbortController?.abort();
  const controller = new AbortController();
  state.fileWorkflowAbortController = controller;
  const filter = fileWorkflowStatusFilter();
  const params = new URLSearchParams({
    offset: String(state.fileWorkflowOffset || 0), limit: '50'
  });
  Object.entries(state.fileWorkflowFilters || {}).forEach(([key, value]) => {
    if (value) params.set(key, String(value));
  });
  // Filter by the canonical presentation state. The server still accepts the
  // old selection_state parameter for older clients, but it cannot distinguish
  // previewed, failed, and delayed files reliably.
  if (filter && filter !== 'all') params.set('status', filter);
  try {
    const page = await api(`/api/file-workflow/${encodeURIComponent(requestedScanId)}?${params}`, { signal: controller.signal });
    if (state.scan?.scan_id !== requestedScanId || state.fileWorkflowRequestSeq !== requestSeq) return;
    state.fileWorkflowPage = page;
    renderFileWorkflowPanel();
  } catch (error) {
    if (error?.name === 'AbortError' || state.scan?.scan_id !== requestedScanId || state.fileWorkflowRequestSeq !== requestSeq) return;
    const host = $('fileWorkflowPanel');
    if (host) host.innerHTML = `<div class="file-workflow-empty">状态清单暂时不可用：${escapeHtml(error.message || '读取失败')}</div>`;
  } finally {
    if (state.fileWorkflowRequestSeq === requestSeq) state.fileWorkflowAbortController = null;
  }
}

async function retryFileWorkflow(path, button) {
  if (!state.scan?.scan_id || !path) return;
  setBusy(button, true, '提交中…');
  try {
    const data = await api(`/api/file-workflow/${encodeURIComponent(state.scan.scan_id)}/retry`, {
      method: 'POST', body: JSON.stringify({ path })
    });
    if (data.job_id) {
      toast(`已重新加入处理队列（${data.batch_files || 0} 个文件）`);
      await pollJob(data.job_id);
    } else {
      toast(data.message || '当前文件未进入队列');
    }
    await loadFileWorkflowPage();
  } catch (error) {
    toast(error.message || '文件重试失败', true);
  } finally {
    if (button?.isConnected) setBusy(button, false);
  }
}

function openEvidenceSource(path, location) {
  if (!state.scan || !path) return;
  const node = { kind: 'file', path, name: path.split(/[\\/]/).pop(), extension: path.includes('.') ? `.${path.split('.').pop()}` : '' };
  state.pendingEvidenceLocation = { path, location: location || {} };
  state.selected = node;
  if (window.SJFXShell) window.SJFXShell.activate('physical');
  selectNode(node, document.createElement('div'));
  const loc = location || {};
  const note = [loc.page ? `第 ${loc.page} 页` : '', loc.section, loc.paragraph_index != null ? `第 ${Number(loc.paragraph_index) + 1} 段` : '', loc.char_start != null ? `字符 ${loc.char_start}-${loc.char_end ?? '?'}` : ''].filter(Boolean).join(' · ');
  if (note) toast(`已打开原文位置：${note}`);
}


async function loadPreliminaryDirectory(scanId = state.scan?.scan_id) {
  if (!scanId) return null;
  try {
    const data = await api('/api/scan/' + encodeURIComponent(scanId) + '/preliminary-directory');
    state.preliminaryDirectory = data.directory || null;
    if (state.preliminaryDirectory && $('selectionHint')) {
      const topics = Array.isArray(state.preliminaryDirectory.topics) ? state.preliminaryDirectory.topics : [];
      const names = topics.slice(0, 5).map(item => item.name || item.title).filter(Boolean).join('、');
      $('selectionHint').textContent = (data.notice || '初步智能目录仅用于筛选。') + (names ? ' 当前候选主题：' + names : '');
    }
    return state.preliminaryDirectory;
  } catch (_) { return null; }
}


async function loadFormalDirectory(scanId = state.scan?.scan_id) {
  if (!scanId) return null;
  try {
    const data = await api('/api/scan/' + encodeURIComponent(scanId) + '/formal-directory');
    state.formalDirectory = data.directory || null;
    if (state.formalDirectory && $('selectionHint')) {
      const c = state.formalDirectory.coverage || {};
      $('selectionHint').dataset.formalDirectory = JSON.stringify({status: state.formalDirectory.status, coverage: c});
    }
    return state.formalDirectory;
  } catch (_) { state.formalDirectory = null; return null; }
}

async function refreshScan(scanId = state.scan?.scan_id) {
  if (!scanId) {
    return;
  }
  const previousScanId = state.scan?.scan_id;
  if (previousScanId && previousScanId !== scanId) {
    state.fileWorkflowRequestSeq += 1;
    state.fileWorkflowAbortController?.abort();
    state.fileWorkflowAbortController = null;
  }
  const data =
    await api(
      `/api/scan/${scanId}?compact=1&summary_limit=100`
    );

  state.scan =
    data.scan;
  if ($('rootPath') && state.scan) {
    const resolvedRoot = state.scan.root || state.scan.root_path || state.scan.path;
    if (resolvedRoot) $('rootPath').value = resolvedRoot;
  }
  rememberCurrentScan(state.scan?.scan_id || scanId);
  restoreNodeSelections(state.scan?.scan_id || scanId);

  state.analysis =
    data.analysis;
  state.preliminaryDirectory = state.analysis?.preliminary_directory || null;
  await loadFormalDirectory(scanId);
  if (!state.preliminaryDirectory) await loadPreliminaryDirectory(scanId);
  state.processing = data.processing || null;
  loadFileWorkflowFilters();
  try {
    const importState = await api(`/api/import-tasks/${scanId}`);
    state.importTask = importState.import_task || null;
    if (state.importTask) state.processing = { ...(state.processing || {}), import_task: state.importTask, preview_queue: state.importTask.preview_queue || {} };
    if (['candidate_importing', 'candidate_analyzing', 'waiting_for_deep_selection'].includes(String(state.importTask?.status || '').toLowerCase())) {
      await loadPreliminaryDirectory(scanId);
    }
    renderCandidatePreviewCard();
  } catch (error) {
    state.importTask = null;
  }
  state.fileWorkflowPage = null;
  state.fileWorkflowOffset = 0;
  loadFileWorkflowPage();
  state.progressiveAnalysis = data.progressive_analysis || null;
  state.treeEdits = data.tree_edits || data.analysis?.manual_tree_edits || [];
  state.analysisTreeOriginal = data.analysis?.analysis_tree || null;

  if (
    state.scan.parse_mode
  ) {
    $('parseMode').value =
      state.scan.parse_mode;
  }

  state.summaries =
    new Map(
      (data.summaries || [])
        .map(
          item => [
            summaryKey(
              item.path,
              item.type
            ),
            item.payload
          ]
        )
    );

  renderTree(
    state.activeTree === 'analysis'
    && state.analysis?.analysis_tree

      ? state.analysis.analysis_tree

      : state.scan.tree
  );

  updateStats();

  $('analysisTreeBtn').disabled = !state.analysis?.analysis_tree;
  // The physical inventory remains available after semantic analysis.
  $('physicalTreeBtn').disabled = false;

  const awaitingSelection = state.processing?.state === 'awaiting_selection';
  const formalReady = Boolean(state.analysis?.coverage?.formal_evidence_ready) || Boolean(state.analysis?.formal_evidence_ready);
  $('reportBtn').disabled = awaitingSelection || !formalReady;

  $('reanalyzeBtn').disabled = awaitingSelection;
  $('retryBtn').disabled = !(state.analysis?.statistics?.failed_files > 0);

  $('retrievalBtn').disabled =
    !state.analysis || !formalReady;
  if ($('fileSearchBtn')) $('fileSearchBtn').disabled = !state.analysis || !formalReady;
  if ($('numericQuestionBtn')) {
    $('numericQuestionBtn').disabled = !state.analysis || !formalReady;
  }

  const requestedRoute = window.SJFXShell?.route;
  if (requestedRoute === 'analysis' && state.analysis?.analysis_tree) {
    await $('analysisTreeBtn').onclick();
  } else if (requestedRoute === 'physical' && state.scan?.tree) {
    $('physicalTreeBtn').onclick();
  }

  updateSelectionCart();
  updateTreeEditPanel();
}


function dataSourceStatusLabel(status) {
  return ({
    ready: '可直接使用', searchable: '可搜索', processing: '处理中',
    awaiting_selection: '等待确认分析范围',
    candidate_importing: '正在导入候选集',
    candidate_analyzing: '正在生成初步摘要',
    waiting_for_deep_selection: '等待选择深度对象',
    scanning: '正在扫描', preprocessing: '全量轻度解析中', previewing: '全量轻度解析中',
    parsing_selected: '正在解析选中文件', parsed_overview: '正在生成解析版智能目录和情报概览', preliminary_summarizing: '正在生成文件初步摘要', preliminary_nodes: '正在生成节点初步摘要', preliminary_overview: '正在生成初步情报概览',
    deep_summarizing_files: '后台生成文件深度摘要', deep_summarizing_nodes: '后台生成节点深度摘要', deep_overview_updating: '正在更新深度证据概览',
    deep_parsing: '深度解析中', summarizing_files: '正在生成文件结论', building_directory: '正在生成正式目录',
    completed: '已完成', partial: '部分完成，需复核',
    paused: '已暂停', failed: '有失败项', scanned: '已盘点', pending: '等待处理'
  })[status] || '状态未知';
}


function dataSourceSize(source) {
  if (source.total_size_human) return source.total_size_human;
  const bytes = Number(source.total_size || 0);
  if (!bytes) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}


function renderDataSources() {
  const list = $('dataSourceList');
  const stateEl = $('dataSourceState');
  if (!list || !stateEl) return;
  if (!state.dataSources.length) {
    list.innerHTML = '';
    stateEl.textContent = state.dataSourceQuery
      ? '没有找到匹配的数据包。'
      : '还没有历史数据包，请从下方导入新的资料。';
  } else {
    stateEl.textContent = `共 ${state.dataSourceTotal} 个历史数据包；选择后直接复用已有处理结果。`;
    list.innerHTML = state.dataSources.map((source) => {
      const status = String(source.status || 'pending');
      const progress = source.analysis_progress || {};
      const active = source.processing?.active_job_id;
      const detail = status === 'processing' && progress.progress != null
        ? `${Math.max(0, Math.min(100, Number(progress.progress)))}% · ${progress.message || '后台处理中'}`
        : `${source.file_count || 0} 个文件 · ${dataSourceSize(source)}`;
      const action = active ? '查看进度' : (source.usable || source.analysis_ready ? '使用此数据包' : '打开数据包');
      return `<article class="data-source-item status-${escapeHtml(status)}">`
        + `<div class="data-source-main"><div class="data-source-title"><strong title="${escapeHtml(source.name || source.root || source.scan_id)}">${escapeHtml(source.name || source.scan_id)}</strong><span class="data-source-status">${escapeHtml(dataSourceStatusLabel(status))}</span></div>`
        + `<small title="${escapeHtml(source.root || '')}">${escapeHtml(source.root || '服务器目录未记录')}</small><span class="data-source-meta">${escapeHtml(detail)} · ${escapeHtml(source.created_at || '创建时间未知')}</span></div>`
        + `<button type="button" class="secondary data-source-use" data-source-select="${escapeHtml(source.scan_id)}">${escapeHtml(action)}</button></article>`;
    }).join('');
  }
  const page = Math.floor(state.dataSourceOffset / 8) + 1;
  const pages = Math.max(1, Math.ceil(state.dataSourceTotal / 8));
  if ($('dataSourcePageInfo')) $('dataSourcePageInfo').textContent = `${page} / ${pages}`;
  if ($('dataSourcePrevBtn')) $('dataSourcePrevBtn').disabled = state.dataSourceOffset <= 0;
  if ($('dataSourceNextBtn')) $('dataSourceNextBtn').disabled = !state.dataSourceTotal || state.dataSourceOffset + 8 >= state.dataSourceTotal;
}


async function refreshDataSources({ reset = false } = {}) {
  if (state.dataSourceRequestInFlight) return;
  if (reset) state.dataSourceOffset = 0;
  state.dataSourceRequestInFlight = true;
  const stateEl = $('dataSourceState');
  if (stateEl) stateEl.textContent = '正在读取历史数据包…';
  try {
    const params = new URLSearchParams({
      query: state.dataSourceQuery,
      limit: '8',
      offset: String(state.dataSourceOffset)
    });
    const data = await api(`/api/data-sources?${params.toString()}`);
    state.dataSources = data.items || [];
    state.dataSourceTotal = Number(data.total || 0);
    renderDataSources();
  } catch (error) {
    if (stateEl) stateEl.textContent = error.message || '历史数据包读取失败';
    if ($('dataSourceList')) $('dataSourceList').innerHTML = '';
  } finally {
    state.dataSourceRequestInFlight = false;
  }
}


async function selectDataSource(scanId, button) {
  if (!scanId) return;
  setBusy(button, true, '正在打开…');
  try {
    const data = await api('/api/data-sources/select', {
      method: 'POST',
      body: JSON.stringify({ scan_id: scanId })
    });
    const source = data.source || {};
    const activeJobId = source.processing?.active_job_id;
    if (activeJobId) {
      await pollJob(activeJobId);
    } else {
      await refreshScan(scanId);
      if (window.SJFXShell) window.SJFXShell.activate(state.analysis?.analysis_tree ? 'analysis' : 'physical');
    }
    toast(data.next_action === 'use_existing_data' ? '已切换到历史数据包，复用已有处理结果' : '已打开数据包');
    await refreshDataSources();
  } catch (error) {
    toast(error.message || '打开数据包失败', true);
  } finally {
    if (button?.isConnected) setBusy(button, false);
  }
}

function selectionSize(bytes) {
  const value = Number(bytes || 0);
  if (!value) return '0 B';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function renderSelectionPanel() {
  const list = $('selectionFileList');
  const stats = $('selectionStats');
  if (!list || !stats) return;
  const selection = state.selection || {};
  const included = new Set(selection.included_paths || []);
  const excluded = new Set(selection.excluded_paths || []);
  const selectedSize = Number(selection.selected_total_size || state.selectionFiles.filter((item) => included.has(item.node_path || item.path)).reduce((sum, item) => sum + Number(item.size || item.source_size || 0), 0));
  stats.textContent = `已选 ${included.size} 个文件 · ${selectionSize(selectedSize)} · 清单共 ${state.selectionTotal || 0} 个文件 · 版本 ${selection.version || 1}`;
  if (!state.selectionFiles.length) {
    list.innerHTML = '<div class="empty-state">当前筛选没有匹配文件。</div>';
    return;
  }
  list.innerHTML = state.selectionFiles.map((item) => {
    const path = String(item.node_path || item.path || '');
    const checked = included.has(path) && !excluded.has(path);
    const isExcluded = excluded.has(path) || item.status === 'out_of_scope';
    const preview = item.preview || {};
    const badge = preview.status === 'previewed' ? '<span class="selection-file-badge previewed">轻量预览</span>' : (isExcluded ? '<span class="selection-file-badge excluded">不进入深析</span>' : '<span class="selection-file-badge">待确认</span>');
    const plan = item.parse_plan || {};
    const planBadge = plan.parser ? '<span class="selection-file-badge">' + escapeHtml(plan.parser) + (plan.estimated_cost ? ' · ' + escapeHtml(plan.estimated_cost) : '') + '</span>' : '';
    return `<label class="selection-file-row"><input type="checkbox" data-selection-path="${escapeHtml(path)}" ${checked ? 'checked' : ''} ${isExcluded && !checked ? '' : ''}><span class="selection-file-main"><strong title="${escapeHtml(path)}">${escapeHtml(path.split('/').pop() || path)}</strong><small title="${escapeHtml(path)}">${escapeHtml(path)} · ${escapeHtml(item.extension || item.type || '未知类型')} · ${selectionSize(item.size || 0)}</small></span><span class="selection-file-badges">${badge}${planBadge}${item.duplicate ? '<span class="selection-file-badge excluded">重复项</span>' : ''}</span></label>`;
  }).join('');
}

async function loadSelectionFiles({ reset = false } = {}) {
  const scanId = state.scan?.scan_id;
  if (!scanId) return;
  if (reset) state.selectionOffset = 0;
  const query = String($('selectionSearch')?.value || '').trim();
  const fileType = String($('selectionFileType')?.value || '').trim();
  const searchScope = String($('selectionSearchScope')?.value || 'name').trim();
  const params = new URLSearchParams({ offset: String(state.selectionOffset), limit: '50' });
  if (query) params.set('query', query);
  if (fileType) params.set('file_type', fileType);
  if (query) params.set('search_scope', searchScope);
  const data = await api('/api/scan/' + encodeURIComponent(scanId) + '/files?' + params.toString());
  state.selectionFiles = data.items || [];
  state.selectionTotal = Number(data.total || 0);
  state.selectionNextOffset = data.next_offset == null ? null : Number(data.next_offset);
  if ($('selectionPrevBtn')) $('selectionPrevBtn').disabled = state.selectionOffset <= 0;
  if ($('selectionNextBtn')) $('selectionNextBtn').disabled = state.selectionNextOffset == null;
  renderSelectionPanel();
  if ($('selectionHint')) $('selectionHint').textContent = (data.coverage_notice || '当前仅登记目录、文件名和类型，正文尚未读取。') + ' 确认后才开始深度解析和模型摘要。';
}



function renderCandidatePreviewCard() {
  const card = $('candidatePreviewCard');
  const stats = $('candidatePreviewStats');
  if (!card || !stats) return;
  const task = state.importTask || {};
  const checkpoint = task.checkpoint || {};
  const expected = Number(checkpoint.candidate_preview_expected || 0);
  const completed = Number(checkpoint.candidate_preview_completed || 0);
  const failed = Number(checkpoint.candidate_preview_failed || 0);
  const paths = checkpoint.candidate_paths || [];
  const status = String(task.status || '').toLowerCase();
  stats.innerHTML = [
    `<div class="candidate-preview-stat"><strong>${escapeHtml(paths.length || expected || 0)}</strong><span>候选文件</span></div>`,
    `<div class="candidate-preview-stat"><strong>${escapeHtml(completed)}/${escapeHtml(expected || paths.length || 0)}</strong><span>已生成初步摘要</span></div>`,
    `<div class="candidate-preview-stat"><strong>${escapeHtml(failed)}</strong><span>降级/失败</span></div>`,
    `<div class="candidate-preview-stat"><strong>${escapeHtml(status === 'waiting_for_deep_selection' ? '可选择' : '分析中')}</strong><span>当前阶段</span></div>`,
  ].join('');
  const open = $('candidateOpenDirectoryBtn');
  if (open) open.disabled = status !== 'waiting_for_deep_selection' && status !== 'partial' && status !== 'completed';
  const notice = $('candidatePreviewNotice');
  if (notice) notice.textContent = checkpoint.candidate_preview_partial
    ? '部分文件预览超时或降级；初步目录仍可使用，深度摘要时可重试。'
    : '初步分析使用代表性内容，正式结论需要进一步深度摘要和原文校验。';

  const topicBox = $('candidatePreviewTopics');
  if (topicBox) {
    const topics = Array.isArray(state.preliminaryDirectory?.topics)
      ? state.preliminaryDirectory.topics
      : [];
    const categories = topics.filter(item => item.selection_filter?.kind === 'content_category');
    const categoryHtml = categories.slice(0, 12).map(item =>
      '<button type="button" class="candidate-topic-chip candidate-category-choice" data-large-category-node="' +
      escapeHtml(item.node_id || '') + '">' +
      '<strong>' + escapeHtml(item.name || item.title || '') + '</strong>' +
      '<small>' + escapeHtml(item.file_count || 0) + ' 个文件 · ' +
      escapeHtml(item.total_size_human || '') + '</small>' +
      '<span>按此类别生成有界解析计划</span></button>'
    ).join('');
    const topicHtml = topics.filter(item => item.selection_filter?.kind !== 'content_category')
      .slice(0, 8)
      .map(item => '<span class="candidate-topic-chip">' + escapeHtml(item.name || item.title || '') +
        '<small>' + escapeHtml(item.file_count || (item.member_paths || []).length || 0) +
        ' 个文件</small></span>').join('');
    topicBox.innerHTML = topics.length
      ? '<strong>按内容类别选择下一步</strong>' +
        (categoryHtml ? '<div class="candidate-topic-list">' + categoryHtml + '</div>' : '') +
        (topicHtml ? '<small class="candidate-topic-caption">代表性主题线索</small><div class="candidate-topic-list">' + topicHtml + '</div>' : '')
      : '候选摘要完成后，这里会显示由文献内容归纳出的主题。';
    const refreshButton = $('refreshDeepResultsBtn');
    if (!refreshButton) {
      const actions = $('candidateOpenDirectoryBtn')?.parentElement;
      if (actions) {
        const button = document.createElement('button');
        button.id = 'refreshDeepResultsBtn';
        button.type = 'button';
        button.className = 'secondary';
        button.textContent = '更新正式结果';
        actions.insertBefore(button, $('candidateBackSelectionBtn'));
        button.addEventListener('click', (event) => {
          runPackageProcessingAction('refresh-deep-results', event.currentTarget);
        });
      }
    }
    const readyRefreshButton = $('refreshDeepResultsBtn');
    if (readyRefreshButton) {
      const plan = checkpoint.selection_plan || {};
      const active = ['queued', 'running', 'cancelling'].includes(status);
      readyRefreshButton.disabled = !plan.schema_version || active;
      readyRefreshButton.title = active ? '等待当前模型任务完成后更新' : '使用已完成的模型摘要重建正式目录和情报概览';
    }
  }
}

function setPackageStage(stage) {
  const show = (id, visible) => $(id)?.classList.toggle('hidden', !visible);
  const isSource = stage === 'source';
  const isImport = stage === 'import';
  const isSelection = stage === 'selection';
  const isAnalysis = stage === 'analysis';
  show('dataSourcePanel', isSource);
  show('newImportCard', isImport);
  show('selectionCard', isSelection);
  show('candidatePreviewCard', isAnalysis);
  show('packageTaskCard', isAnalysis);
  show('packageStatsCard', isAnalysis);
  show('fileWorkflowPanel', isAnalysis);
}

async function openSelectionPanel(scanId = state.scan?.scan_id) {
  if (!scanId) return;
  state.scan = state.scan || { scan_id: scanId };
  const data = await api(`/api/scan/${encodeURIComponent(scanId)}/selection`);
  state.selection = data.selection || {};
  setPackageStage('selection');
  await loadSelectionFiles({ reset: true });
  $('selectionCard')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

let selectionSaveTimer = null;
function scheduleSelectionDraft() {
  clearTimeout(selectionSaveTimer);
  selectionSaveTimer = setTimeout(async () => {
    if (!state.scan?.scan_id || !state.selection) return;
    try {
    const importStatus = String(state.importTask?.status || '').toLowerCase();
    if (importStatus && importStatus !== 'waiting_for_selection') return;
      const data = await api(`/api/scan/${encodeURIComponent(state.scan.scan_id)}/selection`, { method: 'POST', body: JSON.stringify({ included_paths: state.selection.included_paths || [], excluded_paths: state.selection.excluded_paths || [], rules: state.selection.rules || {}, version: state.selection.version }) });
      state.selection = data.selection || state.selection;
      renderSelectionPanel();
    } catch (error) { toast(error.message || '保存分析范围失败', true); }
  }, 300);
}

function bindSelectionControls() {
  $('selectionRefreshBtn')?.addEventListener('click', () => loadSelectionFiles({ reset: true }));
  $('selectionPrevBtn')?.addEventListener('click', () => { state.selectionOffset = Math.max(0, state.selectionOffset - 50); loadSelectionFiles(); });
  $('selectionNextBtn')?.addEventListener('click', () => { if (state.selectionNextOffset == null) return; state.selectionOffset = state.selectionNextOffset; loadSelectionFiles(); });
  $('selectionSearch')?.addEventListener('keydown', (event) => { if (event.key === 'Enter') loadSelectionFiles({ reset: true }); });
  $('selectionSearchBtn')?.addEventListener('click', () => loadSelectionFiles({ reset: true }));
  $('selectionClearSearchBtn')?.addEventListener('click', () => {
    if ($('selectionSearch')) $('selectionSearch').value = '';
    if ($('selectionSearchScope')) $('selectionSearchScope').value = 'name';
    if ($('selectionFileType')) $('selectionFileType').value = '';
    loadSelectionFiles({ reset: true });
  });
  $('selectionSelectAllBtn')?.addEventListener('click', () => {
    if (!state.selection) return;
    const excluded = new Set(state.selection.excluded_paths || []);
    const included = new Set(state.selection.included_paths || []);
    state.selectionFiles.forEach((item) => { const path = String(item.node_path || item.path || ''); if (path && !excluded.has(path) && item.status !== 'out_of_scope') included.add(path); });
    state.selection.included_paths = [...included];
    renderSelectionPanel(); scheduleSelectionDraft();
  });
  $('selectionClearBtn')?.addEventListener('click', () => { if (!state.selection) return; state.selection.included_paths = []; renderSelectionPanel(); scheduleSelectionDraft(); });
  $('selectionFileList')?.addEventListener('change', (event) => {
    const checkbox = event.target.closest('[data-selection-path]'); if (!checkbox || !state.selection) return;
    const path = checkbox.dataset.selectionPath; const included = new Set(state.selection.included_paths || []); const excluded = new Set(state.selection.excluded_paths || []);
    if (checkbox.checked) { included.add(path); excluded.delete(path); } else { included.delete(path); excluded.add(path); }
    state.selection.included_paths = [...included]; state.selection.excluded_paths = [...excluded]; renderSelectionPanel(); scheduleSelectionDraft();
  });
  $('selectionConfirmBtn')?.addEventListener('click', async () => {
    if (!state.scan?.scan_id || !state.selection) return;
    const button = $('selectionConfirmBtn'); setBusy(button, true, '正在启动…');
    try {
      const importStatus = String(state.importTask?.status || '').toLowerCase();
      const endpoint = importStatus && importStatus !== 'waiting_for_selection' ? 'supplement' : 'confirm'; const data = await api(`/api/scan/${encodeURIComponent(state.scan.scan_id)}/selection/${endpoint}`, { method: 'POST', body: JSON.stringify({ included_paths: state.selection.included_paths || [], excluded_paths: state.selection.excluded_paths || [], rules: state.selection.rules || {}, version: state.selection.version }) });
      state.selection = data.selection || state.selection; setPackageStage('analysis'); state.jobId = data.job_id; toast(endpoint === 'supplement' ? `已补充 ${data.new_paths?.length || 0} 个文件，开始解析与初步摘要。` : `已确认 ${data.selected_count || 0} 个文件，开始解析与初步摘要。`); await pollJob(data.job_id);
    } catch (error) { toast(error.message || '确认分析范围失败', true); } finally { if (button.isConnected) setBusy(button, false); }
  });
}

function bindDataSourceControls() {
  const refreshButton = $('dataSourceRefreshBtn');
  const searchButton = $('dataSourceSearchBtn');
  const importButton = $('dataSourceImportBtn');
  const previousButton = $('dataSourcePrevBtn');
  const nextButton = $('dataSourceNextBtn');
  const searchInput = $('dataSourceSearch');
  const list = $('dataSourceList');

  refreshButton?.addEventListener('click', () => refreshDataSources());
  searchButton?.addEventListener('click', () => {
    state.dataSourceQuery = String(searchInput?.value || '').trim();
    refreshDataSources({ reset: true });
  });
  searchInput?.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    searchButton?.click();
  });
  importButton?.addEventListener('click', () => {
    setPackageStage('import');
    const card = $('newImportCard');
    card?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    $('rootPath')?.focus();
  });
  document.querySelector('[data-back-source]')?.addEventListener('click', () => {
    setPackageStage('source');
    $('dataSourcePanel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  previousButton?.addEventListener('click', () => {
    if (state.dataSourceOffset <= 0) return;
    state.dataSourceOffset = Math.max(0, state.dataSourceOffset - 8);
    refreshDataSources();
  });
  nextButton?.addEventListener('click', () => {
    if (state.dataSourceOffset + 8 >= state.dataSourceTotal) return;
    state.dataSourceOffset += 8;
    refreshDataSources();
  });
  list?.addEventListener('click', (event) => {
    const button = event.target.closest('.data-source-use');
    if (!button || button.disabled) return;
    selectDataSource(button.dataset.sourceSelect, button);
  });
}


function jobIdOf(job, fallbackId = '') {
  return String(job?.id || job?.job_id || fallbackId || '');
}


function persistTaskRegistry() {
  try {
    const jobs = [...state.jobs.values()]
      .sort((left, right) => Number(right.updated_local || 0) - Number(left.updated_local || 0))
      .slice(0, 30)
      .map((job) => ({
        id: job.id,
        scan_id: job.scan_id || '',
        task_type: job.task_type || '',
        status: job.status || 'queued',
        stage: job.stage || '',
        current_stage: job.current_stage || '',
        current_file: job.current_file || '',
        progress: Number(job.progress || 0),
        message: job.message || '',
        error: job.error || '',
        queue_position: job.queue_position ?? null,
        blocking_job: job.blocking_job || null,
        heartbeat_at: job.heartbeat_at || null,
        heartbeat_age_seconds: job.heartbeat_age_seconds ?? null,
        worker_online: job.worker_online ?? null,
        updated_at: job.updated_at || null,
        updated_local: Number(job.updated_local || Date.now())
      }));
    window.localStorage.setItem(TASK_REGISTRY_KEY, JSON.stringify(jobs));
  } catch (_) {
    // Task polling must continue even when localStorage is disabled or full.
  }
}


function loadTaskRegistry() {
  try {
    const stored = JSON.parse(window.localStorage.getItem(TASK_REGISTRY_KEY) || '[]');
    const oldest = Date.now() - (7 * 24 * 60 * 60 * 1000);
    if (!Array.isArray(stored)) return;
    stored.forEach((job) => {
      const id = jobIdOf(job);
      if (id && Number(job.updated_local || 0) >= oldest) {
        state.jobs.set(id, { ...job, id });
      }
    });
  } catch (_) {
    window.localStorage.removeItem(TASK_REGISTRY_KEY);
  }
}


function rememberJob(job = {}, fallbackId = '') {
  const id = jobIdOf(job, fallbackId);
  if (!id) return job;
  const previous = state.jobs.get(id) || {};
  const normalized = {
    ...previous,
    ...job,
    id,
    status: String(job.status || previous.status || 'queued').toLowerCase(),
    progress: Math.max(0, Math.min(100, Number(job.progress ?? previous.progress ?? 0))),
    updated_local: Date.now()
  };
  state.jobs.set(id, normalized);
  persistTaskRegistry();
  renderTaskCenter();
  return normalized;
}


function removeRememberedJob(jobId) {
  state.jobs.delete(String(jobId || ''));
  persistTaskRegistry();
  renderTaskCenter();
}


function timestampMilliseconds(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value < 100000000000 ? value * 1000 : value;
  }
  const parsed = Date.parse(value || '');
  return Number.isFinite(parsed) ? parsed : null;
}


function relativeHeartbeat(value, reportedAge = null) {
  const numericAge = Number(reportedAge);
  const hasReportedAge = reportedAge !== null && reportedAge !== '' && Number.isFinite(numericAge);
  const milliseconds = timestampMilliseconds(value);
  if (!milliseconds && !hasReportedAge) {
    return { label: '尚未上报心跳', stale: false };
  }
  const age = hasReportedAge
    ? Math.max(0, Math.round(numericAge))
    : Math.max(0, Math.round((Date.now() - milliseconds) / 1000));
  if (age < 8) return { label: '心跳刚刚更新', stale: false };
  if (age < 60) return { label: `心跳 ${age} 秒前`, stale: false };
  const minutes = Math.floor(age / 60);
  return { label: `心跳 ${minutes} 分钟前`, stale: age >= 120 };
}


function jobTaskLabel(taskType) {
  return ({
    scan_and_analyze: '导入与完整分析',
    analyze_package: '数据包分析',
    generate_report: '生成情况概览',
    generate_summary: '生成深度摘要',
    export_package: '导出交接包'
  })[taskType] || '分析任务';
}


function jobStatusLabel(status) {
  return ({
    queued: '排队中', running: '运行中', cancelling: '正在取消',
    completed: '已完成', failed: '已失败', cancelled: '已取消'
  })[status] || '状态未知';
}


function jobStageLabel(stage) {
  return ({
    queued: '等待 Worker', claimed: 'Worker 已接收', scanning: '盘点目录',
    parsing: '解析文件', analyzing: '内容分析', generating_report: '生成概览',
    generating_summary: '生成深度摘要', preparing_export: '准备导出', exporting: '生成交接包',
    completed: '已完成', failed: '已失败', cancelled: '已取消'
  })[stage] || stage || '';
}


function jobActivityText(job = {}) {
  const stage = job.current_stage || job.stage || '';
  const currentFile = job.current_file || '';
  const stageLabel = jobStageLabel(stage);
  if (currentFile && currentFile !== stage) return `${stageLabel || '处理文件'} · ${currentFile}`;
  return job.message || stageLabel || jobStatusLabel(job.status) || '等待状态更新';
}


function queueSummary(job = {}) {
  if (job.status !== 'queued') return '';
  const position = Number(job.queue_position || 0);
  const positionText = position > 0 ? `当前队列第 ${position} 位` : '正在获取队列位置';
  const blocker = job.blocking_job || null;
  if (!blocker) return `${positionText}；Worker 空闲后会自动启动。`;
  const heartbeat = relativeHeartbeat(blocker.heartbeat_at, blocker.heartbeat_age_seconds);
  return `${positionText}；前序任务 ${String(blocker.id || '').slice(0, 8) || '共享 Worker'} `
    + `${Math.max(0, Math.min(100, Number(blocker.progress || 0)))}% · ${blocker.message || '处理中'} · ${heartbeat.label}。`;
}


function renderTaskCenter() {
  const list = $('activeTaskList');
  if (!list) return;
  const jobs = [...state.jobs.values()].sort((left, right) => {
    const leftActive = ACTIVE_JOB_STATUSES.has(left.status) ? 1 : 0;
    const rightActive = ACTIVE_JOB_STATUSES.has(right.status) ? 1 : 0;
    if (leftActive !== rightActive) return rightActive - leftActive;
    if (left.status === 'running' && right.status !== 'running') return -1;
    if (right.status === 'running' && left.status !== 'running') return 1;
    return Number(right.updated_local || 0) - Number(left.updated_local || 0);
  });
  const activeCount = jobs.filter((job) => ACTIVE_JOB_STATUSES.has(job.status)).length;
  if ($('taskCenterActiveCount')) {
    $('taskCenterActiveCount').textContent = activeCount ? `${activeCount} 个活动任务` : '队列空闲';
  }
  if (!jobs.length) {
    list.className = 'task-list empty';
    list.innerHTML = '<div class="task-list-empty"><strong>当前没有任务</strong><span>导入数据包后，排队、运行、取消和失败状态会显示在这里。</span></div>';
    return;
  }
  list.className = 'task-list';
  list.innerHTML = jobs.slice(0, 12).map((job) => {
    const status = String(job.status || '').toLowerCase();
    const active = ACTIVE_JOB_STATUSES.has(status);
    const cancelling = status === 'cancelling';
    const heartbeat = relativeHeartbeat(job.heartbeat_at, job.heartbeat_age_seconds);
    const queue = queueSummary(job);
    const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
    const heartbeatHtml = status === 'running' || status === 'cancelling'
      ? `<span class="task-heartbeat${heartbeat.stale ? ' stale' : ''}">${escapeHtml(heartbeat.label)}</span>`
      : '';
    const canOpenResult = status === 'completed'
      && ['scan_and_analyze', 'analyze_package'].includes(job.task_type)
      && Boolean(job.result?.scan_id || job.scan_id || job.id);
    return `<article class="task-list-item status-${escapeHtml(status || 'unknown')}" data-task-id="${escapeHtml(job.id)}">`
      + '<div class="task-list-heading">'
      + `<div><strong>${escapeHtml(jobTaskLabel(job.task_type))}</strong><span class="task-id">#${escapeHtml(String(job.id).slice(0, 12))}</span></div>`
      + `<span class="task-state">${escapeHtml(jobStatusLabel(status))}</span></div>`
      + `<div class="task-list-progress"><i style="width:${progress}%"></i></div>`
      + `<div class="task-list-meta"><b>${progress}%</b><span>${escapeHtml(jobActivityText(job))}</span>${heartbeatHtml}</div>`
      + (queue ? `<p class="task-queue-detail">${escapeHtml(queue)}</p>` : '')
      + (job.connection_issue ? `<p class="task-network-warning">${escapeHtml(job.connection_issue)}</p>` : '')
      + (job.error && status === 'failed' ? `<p class="task-error-detail">${escapeHtml(job.error)}</p>` : '')
      + '<div class="task-list-actions">'
      + (active ? `<button type="button" class="text-button" data-job-watch="${escapeHtml(job.id)}">查看实时进度</button>` : '')
      + (canOpenResult ? `<button type="button" class="text-button" data-job-open="${escapeHtml(job.id)}">打开分析结果</button>` : '')
      + (active ? `<button type="button" class="danger compact" data-job-cancel="${escapeHtml(job.id)}" ${cancelling ? 'disabled' : ''}>${cancelling ? '正在暂停…' : (['scan_and_analyze', 'analyze_package'].includes(job.task_type) ? '结束本次运行' : (status === 'queued' ? '取消排队' : '取消任务'))}</button>` : '')
      + '</div></article>';
  }).join('');

  const latest = jobs[0];
  if ($('dashboardActivity') && latest) {
    $('dashboardActivity').className = 'activity-task';
    $('dashboardActivity').innerHTML = `<strong>${escapeHtml(jobTaskLabel(latest.task_type))}</strong>`
      + `<span>${escapeHtml(jobStatusLabel(latest.status))} · ${escapeHtml(jobActivityText(latest))}</span>`;
  }
}


async function refreshKnownJobs(jobIds) {
  await Promise.all(jobIds.map(async (jobId) => {
    try {
      const data = await api(`/api/jobs/${jobId}`);
      rememberJob({ ...(data.job || {}), connection_issue: '' }, jobId);
    } catch (error) {
      if ([403, 404].includes(error.status)) {
        removeRememberedJob(jobId);
        return;
      }
      const previous = state.jobs.get(jobId) || { id: jobId, status: 'queued' };
      rememberJob({ ...previous, connection_issue: '暂时无法同步服务器状态，将自动重试。' }, jobId);
    }
  }));
}


async function refreshTaskCenter() {
  if (state.taskCenterRefreshInFlight) return;
  state.taskCenterRefreshInFlight = true;
  const indicator = $('taskCenterSyncState');
  try {
    let listedIds = null;
    if (state.jobsEndpointAvailable !== false) {
      try {
        const data = await api('/api/jobs?status=active&limit=50&compact=1');
        if (!Array.isArray(data.jobs)) throw new Error('任务列表响应格式错误');
        state.jobsEndpointAvailable = true;
        listedIds = new Set();
        data.jobs.forEach((job) => {
          const remembered = rememberJob({ ...job, connection_issue: '' });
          if (remembered.id) listedIds.add(remembered.id);
        });
        if (indicator) indicator.textContent = '已与 Worker 队列同步';
      } catch (error) {
        if ([404, 405].includes(error.status)) {
          state.jobsEndpointAvailable = false;
        } else if (indicator) {
          indicator.textContent = '连接波动，正在自动重试';
        }
      }
    }
    const knownActiveIds = [...state.jobs.values()]
      .filter((job) => ACTIVE_JOB_STATUSES.has(job.status) && job.id !== state.jobId)
      .map((job) => job.id)
      .filter((jobId) => !listedIds || !listedIds.has(jobId));
    if (knownActiveIds.length) await refreshKnownJobs(knownActiveIds);
    if (state.jobsEndpointAvailable === false && indicator) {
      indicator.textContent = '已同步本浏览器提交的任务';
    }
  } finally {
    state.taskCenterRefreshInFlight = false;
    renderTaskCenter();
  }
}


async function startTaskCenterRefresh() {
  loadTaskRegistry();
  renderTaskCenter();
  await refreshTaskCenter();
  window.setInterval(refreshTaskCenter, 3000);
}


function scanJobCandidate(jobs) {
  const scanJobs = jobs.filter((job) =>
    ['scan_and_analyze', 'analyze_package'].includes(job.task_type)
    && job.status !== 'failed'
    && job.status !== 'cancelled'
  );
  return scanJobs.find((job) => ACTIVE_JOB_STATUSES.has(job.status))
    || scanJobs.find((job) => job.status === 'completed')
    || null;
}


async function restoreWorkspace() {
  let storedScanId = '';
  try {
    storedScanId = window.localStorage.getItem(CURRENT_SCAN_KEY) || '';
  } catch (_) {
    storedScanId = '';
  }

  if (storedScanId) {
    try {
      await refreshScan(storedScanId);
      return;
    } catch (error) {
      if ([403, 404].includes(error.status)) forgetCurrentScan(storedScanId);
    }
  }

  let candidate = scanJobCandidate([...state.jobs.values()]);
  if (!candidate) {
    try {
      const data = await api('/api/jobs?status=all&limit=50&compact=1');
      (data.jobs || []).forEach((job) => rememberJob(job));
      candidate = scanJobCandidate(data.jobs || []);
    } catch (_) {
      return;
    }
  }
  if (!candidate) return;

  const jobId = jobIdOf(candidate);
  if (ACTIVE_JOB_STATUSES.has(candidate.status) && jobId) {
    pollJob(jobId).catch((error) => toast(error.message || '任务恢复失败', true));
    return;
  }

  const scanId = candidate.result?.scan_id || candidate.scan_id || jobId;
  if (!scanId) return;
  try {
    await refreshScan(scanId);
    if (state.processing?.state === 'awaiting_selection') {
      await openSelectionPanel(scanId);
    }
  } catch (_) {
    forgetCurrentScan(scanId);
  }
}


function updateJobControls(job = {}) {
  const status = String(job.status || '').toLowerCase();
  if (state.jobId) job = rememberJob(job, state.jobId);
  const active = ACTIVE_JOB_STATUSES.has(status) && Boolean(state.jobId);
  const cancelling = status === 'cancelling';
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const baseDetail = jobActivityText(job);
  const queueDetail = queueSummary(job);
  const heartbeat = relativeHeartbeat(job.heartbeat_at, job.heartbeat_age_seconds);
  const detail = status === 'queued' && queueDetail
    ? `${baseDetail} · ${queueDetail}`
    : ((status === 'running' || status === 'cancelling')
      ? `${baseDetail} · ${heartbeat.label}`
      : baseDetail);
  ['cancelJobBtn', 'taskCenterCancelBtn'].forEach((id) => {
    const button = $(id);
    if (!button) return;
    button.disabled = !active || cancelling;
    const packageJob = ['scan_and_analyze', 'analyze_package'].includes(job.task_type);
    button.textContent = cancelling
      ? (packageJob ? '正在安全暂停…' : '正在取消…')
      : (packageJob ? '结束本次运行' : '取消当前任务');
  });
  if ($('jobStatusChip')) {
    $('jobStatusChip').textContent = (status || 'idle').toUpperCase();
    $('jobStatusChip').dataset.status = status || 'idle';
  }
  if ($('taskCenterProgressBar')) $('taskCenterProgressBar').style.width = `${progress}%`;
  if ($('taskCenterProgressText')) $('taskCenterProgressText').textContent = `${progress}% · ${detail}`;
}


async function cancelJob(jobId) {
  if (!jobId) {
    toast('当前没有可以取消的任务。');
    return;
  }
  const previous = state.jobs.get(jobId) || { id: jobId, status: 'running', progress: 0 };
  const cancelling = rememberJob({ ...previous, status: 'cancelling', message: '正在请求 Worker 安全停止' }, jobId);
  if (state.jobId === jobId) updateJobControls(cancelling);
  try {
    const data = await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
    const updated = rememberJob(data.job || { ...cancelling, status: 'cancelling', message: '已发送取消请求' }, jobId);
    if (state.jobId === jobId) updateJobControls(updated);
    toast(updated.status === 'cancelled'
      ? '排队任务已取消。'
      : '已发送取消请求，Worker 正在安全停止当前步骤。');
    refreshTaskCenter();
  } catch (error) {
    const restored = rememberJob({ ...previous, connection_issue: '取消请求未确认，请重试。' }, jobId);
    if (state.jobId === jobId) updateJobControls(restored);
    toast(error.message || '取消任务失败', true);
  }
}


async function cancelCurrentJob() {
  return cancelJob(state.jobId);
}


async function prioritizeEvidenceSource(path, button) {
  const scanId = state.scan?.scan_id;
  const targetPath = String(path || '').trim();
  if (!scanId || !targetPath) return;
  const previous = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = '正在加入…';
  }
  try {
    const response = await api(
      `/api/package-processing/${encodeURIComponent(scanId)}/resume`,
      {
        method: 'POST',
        body: JSON.stringify({
          mode: 'selection',
          target_paths: [targetPath],
          continue_full: true,
        })
      }
    );
    state.processing = response.processing || state.processing;
    if (!response.accepted || !response.job_id) {
      toast(response.message || '该文件当前不需要继续深析。');
      return;
    }
    toast(`已将 ${targetPath} 加入下一批优先深析。`);
    await pollJob(response.job_id);
  } catch (error) {
    toast(error.message || '无法将检索结果加入深析队列', true);
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = previous || '优先深析';
    }
  }
}


async function openJobResult(jobId) {
  let job = state.jobs.get(String(jobId || '')) || null;
  if (!job) {
    const data = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    job = rememberJob(data.job || {}, jobId);
  }
  const scanId = job?.result?.scan_id || job?.scan_id || jobId;
  if (!scanId) throw new Error('该任务没有可打开的分析结果');
  await refreshScan(scanId);
  const route = state.analysis?.analysis_tree ? 'analysis' : 'physical';
  if (window.SJFXShell) window.SJFXShell.activate(route);
  toast(route === 'analysis' ? '已恢复智能分析目录' : '已恢复原始目录');
}


async function refreshDeepResults(button) {
  if (!state.scan?.scan_id) return;
  setBusy(button, true, '正在更新正式结果…');
  try {
    const data = await api(
      `/api/scan/${encodeURIComponent(state.scan.scan_id)}/refresh-deep-results`,
      { method: 'POST', body: JSON.stringify({}) }
    );
    if (data.job_id) {
      state.jobId = data.job_id;
      await pollJob(data.job_id);
    }
    await refreshScan(state.scan.scan_id);
    state.summaries.clear();
    toast(data.message || '深度证据更新已完成。');
    if (state.selected) {
      const row = document.querySelector('.tree-row.selected');
      if (row) await selectNode(state.selected, row);
    }
  } catch (error) {
    toast(error.message || '当前还没有可用于更新正式结果的深度摘要。', true);
  } finally {
    if (button?.isConnected) setBusy(button, false);
  }
}

async function runPackageProcessingAction(action, button) {
  if (action === 'refresh-deep-results') {
    await refreshDeepResults(button);
    return;
  }
  if (!state.scan?.scan_id) return;
  const scanId = encodeURIComponent(state.scan.scan_id);
  setBusy(button, true, action === 'pause' ? '正在安全暂停…' : '正在生成下一批…');
  try {
    if (action === 'pause') {
      const data = await api(`/api/package-processing/${scanId}/pause`, {
        method: 'POST', body: JSON.stringify({ reason: '用户结束本次运行' })
      });
      state.processing = data.processing || data.control || state.processing;
      await refreshScan(state.scan.scan_id);
      toast('已安全暂停：完成结果已保留，未完成文件仍在待处理池。');
      return;
    }
    const payload = {
      mode: action,
      continue_full: $('packageContinueFull')?.checked ?? false,
    };
    if (action === 'query') {
      payload.query = String($('packagePriorityQuery')?.value || '').trim();
      if (!payload.query) throw new Error('请输入关键词或自然语言研究要求');
    }
    if (action === 'selection') {
      payload.target_paths = packageSelectedPaths();
      if (!payload.target_paths.length) throw new Error('请先在目录树中勾选文件');
    }
    const data = await api(`/api/package-processing/${scanId}/resume`, {
      method: 'POST', body: JSON.stringify(payload)
    });
    state.processing = data.processing || state.processing;
    if (!data.accepted || !data.job_id) {
      updateStats();
      toast(data.message || '当前没有符合条件的未处理文件。');
      return;
    }
    toast(action === 'continue'
      ? `已从断点生成下一批（${data.batch_files}个逻辑文件）`
      : `已找到${data.preferred_matches || 0}个优先文件，开始处理下一批`);
    await pollJob(data.job_id);
  } catch (error) {
    toast(error.message || '无法更新处理队列', true);
  } finally {
    if (button?.isConnected) setBusy(button, false);
  }
}


document.addEventListener('click', (event) => {
  const sourceButton = event.target.closest('[data-evidence-source]');
  if (sourceButton) {
    event.preventDefault();
    let location = {};
    try { location = JSON.parse(sourceButton.dataset.evidenceLocation || '{}'); } catch (_) { /* malformed optional location */ }
    openEvidenceSource(sourceButton.dataset.evidenceSource, location);
    return;
  }
  const searchPageButton = event.target.closest('[data-file-search-page]');
  if (searchPageButton && !searchPageButton.disabled) {
    event.preventDefault();
    const current = Number(state.fileSearchResult?.page || state.fileSearchPage || 1);
    runFileSearch(Math.max(1, current + (searchPageButton.dataset.fileSearchPage === 'next' ? 1 : -1)));
    return;
  }
  const prioritizeButton = event.target.closest('[data-evidence-prioritize]');
  if (prioritizeButton) {
    event.preventDefault();
    if (!prioritizeButton.disabled) {
      prioritizeEvidenceSource(
        prioritizeButton.dataset.evidencePrioritize,
        prioritizeButton
      );
    }
    return;
  }
  const fileButton = event.target.closest('[data-file-open]');
  if (fileButton) {
    event.preventDefault();
    openEvidenceSource(fileButton.dataset.fileOpen, {});
    return;
  }
  const retryFileButton = event.target.closest('[data-file-retry]');
  if (retryFileButton) {
    event.preventDefault();
    if (!retryFileButton.disabled) retryFileWorkflow(retryFileButton.dataset.fileRetry, retryFileButton);
    return;
  }
  const workflowRefresh = event.target.closest('[data-file-workflow-refresh]');
  if (workflowRefresh) {
    event.preventDefault();
    loadFileWorkflowPage();
    return;
  }
  const workflowPage = event.target.closest('[data-file-workflow-page]');
  if (workflowPage && !workflowPage.disabled) {
    event.preventDefault();
    const page = state.fileWorkflowPage || {};
    const size = Array.isArray(page.items) && page.items.length ? page.items.length : 50;
    state.fileWorkflowOffset = Math.max(0, Number(state.fileWorkflowOffset || 0) + (workflowPage.dataset.fileWorkflowPage === 'next' ? size : -size));
    loadFileWorkflowPage();
    return;
  }
  const button = event.target.closest('[data-package-action]');
  if (!button) return;
  event.preventDefault();
  if (button.disabled) return;
  runPackageProcessingAction(button.dataset.packageAction, button);
});


document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.target?.id !== 'packagePriorityQuery') return;
  event.preventDefault();
  const button = document.querySelector('[data-package-action="query"]');
  if (button && !button.disabled) runPackageProcessingAction('query', button);
});


document.addEventListener('click', (event) => {
  const cancelButton = event.target.closest('#cancelJobBtn, #taskCenterCancelBtn, [data-job-cancel]');
  if (cancelButton) {
    event.preventDefault();
    if (cancelButton.disabled) return;
    const jobId = cancelButton.dataset.jobCancel || state.jobId;
    cancelJob(jobId);
    return;
  }
  const watchButton = event.target.closest('[data-job-watch]');
  if (watchButton) {
    event.preventDefault();
    const jobId = watchButton.dataset.jobWatch;
    const job = state.jobs.get(jobId);
    if (!job || !ACTIVE_JOB_STATUSES.has(job.status)) return;
    if (window.SJFXShell) window.SJFXShell.activate('tasks');
    pollJob(jobId).catch((error) => toast(error.message || '任务轮询失败', true));
    return;
  }
  const openButton = event.target.closest('[data-job-open]');
  if (openButton) {
    event.preventDefault();
    openJobResult(openButton.dataset.jobOpen)
      .catch((error) => toast(error.message || '无法打开分析结果', true));
    return;
  }
  const refreshButton = event.target.closest('#taskCenterRefreshBtn');
  if (!refreshButton) return;
  event.preventDefault();
  setBusy(refreshButton, true, '同步中…');
  refreshTaskCenter().finally(() => setBusy(refreshButton, false));
});


function isTransientPollError(error) {
  return Boolean(error?.transient || !error?.status || window.navigator.onLine === false);
}


function waitFor(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function pollImportTaskCompletion(scanId) {
  if (!scanId) return null;
  let lastTask = null;
  while (true) {
    const data = await api(`/api/import-tasks/${encodeURIComponent(scanId)}`);
    lastTask = data.import_task || null;
    const task = lastTask || {};
    const checkpoint = task.checkpoint || {};
    const expected = Number(checkpoint.deep_summary_expected || 0);
    const completed = Number(checkpoint.deep_summary_completed || 0);
    const failed = Number(checkpoint.deep_summary_failed || 0);
    const candidateExpected = Number(checkpoint.candidate_preview_expected || 0);
    const candidateCompleted = Number(checkpoint.candidate_preview_completed || 0);
    const candidateFailed = Number(checkpoint.candidate_preview_failed || 0);
    const ratio = expected ? Math.min(1, (completed + failed) / expected) : 0;
    const candidateRatio = candidateExpected ? Math.min(1, (candidateCompleted + candidateFailed) / candidateExpected) : 0;
    const progress = Math.max(80, Math.min(99, 80 + Math.round(ratio * 18)));
    const candidateProgress = Math.max(35, Math.min(88, 35 + Math.round(candidateRatio * 50)));
    const status = String(task.status || '').toLowerCase();
    if (status === 'candidate_importing' || status === 'candidate_analyzing') {
      $('progressBar').style.width = `${candidateProgress}%`;
      $('progressText').textContent = `${candidateProgress}% · ${status === 'candidate_importing' ? '正在完成候选文件解析…' : `正在生成候选文件初步摘要：${candidateCompleted}/${candidateExpected || '多'}${candidateFailed ? `，降级 ${candidateFailed}` : ''}`}`;
      if ($('jobStatusChip')) $('jobStatusChip').textContent = status === 'candidate_importing' ? 'CANDIDATE_IMPORTING' : 'CANDIDATE_ANALYZING';
      renderCandidatePreviewCard();
      await waitFor(1200);
      continue;
    }
    if (status === 'waiting_for_deep_selection') {
      await refreshScan(scanId);
      setPackageStage('analysis');
      renderCandidatePreviewCard();
      return lastTask;
    }
      if (status === 'parsed_overview' || status === 'preliminary_overview') {
        const overviewProgress = status === 'parsed_overview' ? 86 : 92;
        $('progressBar').style.width = `${overviewProgress}%`;
        $('progressText').textContent = `${overviewProgress}% · ${status === 'parsed_overview' ? '正在生成解析版智能目录和情报概览…' : '正在自动更新初步智能目录和情报概览…'}`;
        if ($('jobStatusChip')) $('jobStatusChip').textContent = status === 'parsed_overview' ? 'PARSED_OVERVIEW' : 'PRELIMINARY_OVERVIEW';
        await waitFor(1200);
        continue;
      }
    if (
      status === 'preliminary_summarizing'
      || status === 'preliminary_nodes'
      || status === 'summarizing_files'
      || status === 'building_directory'
    ) {
      $('progressBar').style.width = `${progress}%`;
      $('progressText').textContent = `${progress}% · ${
        status === 'preliminary_summarizing'
          ? `正在生成选中文件初步摘要：${completed}/${expected || '多'}`
          : status === 'preliminary_nodes'
            ? '正在生成选中文件对应的节点初步摘要…'
            : status === 'building_directory'
              ? '正在生成正式智能目录…'
              : `正在生成文件深度摘要：${completed}/${expected || '多'}${failed ? `，失败 ${failed}` : ''}`
      }`;
      if ($('jobStatusChip')) $('jobStatusChip').textContent =
        status === 'preliminary_summarizing'
          ? 'PRELIMINARY_SUMMARIZING'
          : status === 'preliminary_nodes'
            ? 'PRELIMINARY_NODES'
            : status === 'building_directory'
              ? 'BUILDING_DIRECTORY'
              : 'SUMMARIZING_FILES';
    }
      if (status === 'deep_summarizing_files' || status === 'deep_summarizing_nodes' || status === 'deep_update_available') {
        await refreshScan(scanId);
        setPackageStage('analysis');
        return lastTask;
      }
    if (status === 'completed' || status === 'partial' || status === 'failed' || status === 'paused') {
      await refreshScan(scanId);
      return lastTask;
    }
    await waitFor(1200);
  }
}


async function pollJob(jobId) {
  state.jobId =
    jobId;

  const registered = rememberJob({ id: jobId, status: 'queued', progress: 0, message: '任务已提交，等待本地 Worker' }, jobId);
  updateJobControls(registered);

  $('pipeline')
    .classList
    .remove(
      'empty'
    );

  let consecutivePollErrors = 0;
  while (
    state.jobId === jobId
  ) {
    let data;
    try {
      data = await api(`/api/jobs/${jobId}`);
      consecutivePollErrors = 0;
    } catch (error) {
      if (state.jobId !== jobId) return;
      if (!isTransientPollError(error)) {
        if ([403, 404].includes(error.status)) removeRememberedJob(jobId);
        state.jobId = null;
        updateJobControls({ status: 'failed', progress: 0, message: error.message || '无法访问任务状态' });
        throw error;
      }
      consecutivePollErrors += 1;
      const delay = Math.min(10000, 1000 * (2 ** Math.min(consecutivePollErrors - 1, 4)));
      const previous = state.jobs.get(jobId) || registered;
      const retryMessage = window.navigator.onLine === false
        ? '网络已断开，恢复后将自动继续同步任务。'
        : `状态同步暂时失败，${Math.ceil(delay / 1000)} 秒后自动重试。`;
      const retryJob = rememberJob({ ...previous, connection_issue: retryMessage }, jobId);
      updateJobControls(retryJob);
      if ($('progressText')) $('progressText').textContent = `${retryJob.progress || 0}% · ${retryMessage}`;
      await waitFor(delay);
      continue;
    }

    const job = rememberJob({ ...(data.job || {}), connection_issue: '' }, jobId);

    updateJobControls(job);

    $('progressBar').style.width =
      `${job.progress || 0}%`;

    const queueDetail = queueSummary(job);
    const heartbeat = relativeHeartbeat(job.heartbeat_at, job.heartbeat_age_seconds);
    const activity = job.status === 'queued' && queueDetail
      ? `${jobActivityText(job)} · ${queueDetail}`
      : ((job.status === 'running' || job.status === 'cancelling')
        ? `${jobActivityText(job)} · ${heartbeat.label}`
        : jobActivityText(job));
    $('progressText').textContent =
      `${job.progress || 0}% · ${activity}`;

    // A scan-and-analyze job publishes its inventory before parsing begins.
    // Load and show the physical tree immediately instead of making the user
    // wait for semantic clustering and report generation.
    const partialScanId = job.result?.scan_available
      ? (job.result.scan_id || job.scan_id)
      : null;
    const progressiveRefreshKey = partialScanId
      ? `${partialScanId}:${Math.floor(Number(job.progress || 0) / 5)}`
      : null;
    if (partialScanId && progressiveRefreshKey !== state.progressiveRefreshKey) {
      try {
        const partial = await api(`/api/scan/${partialScanId}?compact=1&summary_limit=100`);
        const existingTree = state.scan?.tree || null;
        const firstInventoryLoad = !existingTree;
        state.scan = partial.scan;
        rememberCurrentScan(partialScanId);
        if (existingTree) state.scan.tree = existingTree;
        state.analysis = partial.analysis;
        state.progressiveAnalysis = partial.progressive_analysis || null;
        state.progressiveRefreshKey = progressiveRefreshKey;
        state.summaries = new Map((partial.summaries || []).map(item => [summaryKey(item.path, item.type), item.payload]));
        $('physicalTreeBtn').disabled = false;
        if (firstInventoryLoad) {
          state.activeTree = 'physical';
          $('physicalTreeBtn').classList.add('active');
          $('analysisTreeBtn').classList.remove('active');
          renderTree(state.scan.tree);
          $('tree').classList.remove('empty');
          toast('原始目录已加载，可按目录名和文件名选择；当前未解析正文。');
        updateStats();
        }
      } catch (partialError) {
        // The main job status remains authoritative; a transient fetch error
        // should not abort polling.
      }
    }

    if (
      job.status ===
      'completed'
    ) {
      if (job.task_type === 'export_package') {
        const result = job.result || {};
        $('reportResult').className = 'report-result';
        $('reportResult').innerHTML =
          `<strong>待整编数据包已生成</strong>`
          + `<p>已合并 ${escapeHtml(result.selection_count || 0)} 个选择，去重后包含 ${escapeHtml(result.source_file_count || 0)} 个源文件。</p>`
          + (result.download_url
            ? `<p>${downloadLink(result.download_url, result.segmented ? '下载分卷索引与校验清单' : '下载待整编数据包')}</p>`
            : '')
          + (result.volumes?.length
            ? `<p><strong>共 ${escapeHtml(result.volumes.length)} 个独立 ZIP 分卷：</strong><br>${result.volumes.map((item) => downloadLink(item.download_url, `下载分卷 ${item.index}`)).join(' · ')}</p>`
            : '');
        toast('待整编数据包已生成，可下载。');
        state.jobId = null;
        updateJobControls({ ...job, status: 'completed', progress: 100 });
        return;
      }

      if (job.task_type === 'generate_summary') {
        const result = job.result || {};
        if (result.summary) {
          state.summary = result.summary;
          const resultPath = result.summary.node_path || state.selected?.path;
          const resultType = result.summary.summary_type || (state.selected?.kind === 'directory' ? 'folder' : 'file');
          if (resultPath) state.summaries.set(summaryKey(resultPath, resultType), result.summary);
          renderSummary(
            result.summary,
            result.node_id ? '主题节点深度摘要' : '模型深度摘要'
          );
          }
        toast(
          result.degraded
            ? '深度摘要已完成，但部分内容使用了本地保底结果'
            : '深度摘要生成完成'
        );
        state.jobId = null;
        updateJobControls({ ...job, status: 'completed', progress: 100 });
        return;
      }

      const completedScanId =
        job.result?.scan_id
        || state.scan?.scan_id
        || job.scan_id;

      if (job.result?._await_selection && completedScanId) {
        state.scan = { scan_id: completedScanId };
        await refreshScan(completedScanId);
        state.jobId = null;
        updateJobControls({ ...job, status: 'completed', progress: 100, message: '目录导入完成，等待选择文件' });
        await openSelectionPanel(completedScanId);
        toast('目录导入完成，请按目录名和文件名选择要深度解析的文件。');
        return;
      }

      if (Array.isArray(job.result?.candidate_preview_job_ids) && job.result.candidate_preview_job_ids.length && completedScanId) {
        state.scan = { scan_id: completedScanId };
        await refreshScan(completedScanId);
        await pollImportTaskCompletion(completedScanId);
        state.jobId = null;
        updateJobControls({ ...job, status: 'completed', progress: 100, message: '导入阶段初步分析完成，等待选择深度对象' });
        setPackageStage('analysis');
        renderCandidatePreviewCard();
        if (state.analysis?.analysis_tree) {
          state.activeTree = 'analysis';
          $('analysisTreeBtn')?.click();
        }
        toast('导入阶段的文件摘要和节点概览已生成；可继续补充深度分析。');
        return;
      }

      if (completedScanId) {
        state.scan = { scan_id: completedScanId };
        await refreshScan(completedScanId);
        if (Array.isArray(job.result?.deep_summary_job_ids) && job.result.deep_summary_job_ids.length) {
          await pollImportTaskCompletion(completedScanId);
        }
      }

      // Keep the original physical directory as the default view. Users can
      // switch to the semantic topic tree explicitly after analysis completes.
      // This preserves the source structure and avoids making it appear lost.
      $('physicalTreeBtn').disabled = false;
      if (state.activeTree !== 'analysis' || !state.analysis?.analysis_tree) {
        state.activeTree = 'physical';
        $('physicalTreeBtn').classList.add('active');
        $('analysisTreeBtn').classList.remove('active');
        renderTree(state.scan?.tree || state.scan);
      }

      const overview =
        job.result?.overview;

      const d =
        overview
          ?.report
          ?.recommended_research_direction
        || {};

      $('reportResult').className =
        'report-result';

      $('reportResult').innerHTML =
        `<strong>${
          job.task_type === 'generate_report'
            ? '概览 Word 已重新生成'
            : '自动概览已生成'
        }</strong>`
        +
        `<p><span class="inference-badge">推论</span> ${
          escapeHtml(
            d.title
            || '待进一步确定研究方向'
          )
        }</p>`
        +
        `<p>${
          escapeHtml(
            d.rationale
            || ''
          )
        }</p>`
        +
        `${
          overview?.download_url

            ? (
                downloadLink(overview.download_url, '下载自动生成的情况概览 Word')
              )

            : ''
        }`;

      toast(
        job.task_type === 'generate_report'
          ? '概览 Word 已生成'
          : (job.result?._await_selection ? '轻量预览完成，请确认深度解析范围' : '深度解析、证据校验和正式结果已生成')
      );

      state.jobId =
        null;

      updateJobControls({ ...job, status: 'completed', progress: 100 });

      return;
    }

    if (job.status === 'cancelled') {
      state.jobId =
        null;

      updateJobControls(job);
      toast(job.message || '任务已取消，已完成的文件检查点已保留。');
      return;
    }

    if (job.status === 'failed') {
      state.jobId =
        null;

      updateJobControls(job);

      throw new Error(
        job.message
        || job.error
        || '深度解析失败'
      );
    }

    await waitFor(1000);
  }
}


$('scanBtn').onclick =
  async () => {
    if (state.jobId) {
      toast(
        '当前分析任务尚未完成，请勿重复提交。',
        true
      );

      return;
    }

    const btn =
      $('scanBtn');

    // Detach the previous package before a new import starts.  This keeps the
    // shell, overview, translation and relationship modules from presenting
    // stale results while the new inventory is being built.
    forgetCurrentScan();
    state.fileWorkflowRequestSeq += 1;
    state.fileWorkflowAbortController?.abort();
    state.fileWorkflowAbortController = null;
    if ($('scanStats')) {
      $('scanStats').className = 'stats empty';
      $('scanStats').textContent = '正在导入新的数据包…';
    }

    setPackageStage('import');
    setBusy(
      btn,
      true,
      '正在导入…'
    );

    $('pipeline')
      .classList
      .remove(
        'empty'
      );

    $('progressBar')
      .style
      .width =
        '3%';

    $('progressText')
      .textContent =
        '正在遍历服务器目录；大数据包扫描阶段可能需要一段时间…';

    // A new scan starts a new UI session; clear selections from the prior package.
    state.scan = null;
    state.analysis = null;
    state.processing = null;
    state.progressiveAnalysis = null;
    state.progressiveRefreshKey = null;
    state.summary = null;
    state.summaries = new Map();
    state.selected = null;
    state.retrievalScope = 'package';
    state.pendingEvidenceLocation = null;
    state.selectedNodes = new Map();
    state.activeTree = 'physical';
    if ($('workspaceName')) $('workspaceName').textContent = '尚未导入数据包';
    $('tree').className = 'tree';
    renderInitialPhysicalTree($('rootPath').value);
    $('analysisTreeBtn').classList.remove('active');
    $('physicalTreeBtn').classList.add('active');
    $('analysisTreeBtn').disabled = true;
    $('reportBtn').disabled = true;
    $('reanalyzeBtn').disabled = true;
    $('retrievalBtn').disabled = true;
    if ($('fileSearchBtn')) $('fileSearchBtn').disabled = true;
    if ($('numericQuestionBtn')) $('numericQuestionBtn').disabled = true;
    updateSelectionCart();
    renderEvidenceScopeControl();

    try {
      const data =
        await api(
          '/api/scan',
          {
            method: 'POST',

            body: JSON.stringify({
              path:
                $('rootPath').value,

              // The public workflow is a single Smart Parse action.  The
              // backend performs per-file routing; keep the hidden control for
              // backwards compatibility with older saved sessions.
              parse_mode: $('parseMode')?.value || 'auto'
            })
          }
        );

      await pollJob(
        data.job_id
        || data.analysis_job_id
      );

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


$('reanalyzeBtn').onclick =
  async () => {
    if (state.jobId) {
      toast(
        '当前分析任务尚未完成，请勿重复提交。',
        true
      );

      return;
    }

    if (!state.scan) {
      return;
    }

    const btn =
      $('reanalyzeBtn');

    setBusy(
      btn,
      true,
      '正在启动…'
    );

    try {
      const data =
        await api(
          '/api/analyze-package',
          {
            method: 'POST',

            body: JSON.stringify({
              scan_id:
                state.scan.scan_id,

              parse_mode: $('parseMode')?.value || 'auto'
            })
          }
        );

      await pollJob(
        data.job_id
      );

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


$('physicalTreeBtn').onclick =
  () => {
    state.activeTree =
      'physical';

    $('physicalTreeBtn')
      .classList
      .add(
        'active'
      );

    $('analysisTreeBtn')
      .classList
      .remove(
        'active'
      );

    renderTree(
      state.scan.tree
    );
    if ($('treeFilter')) $('treeFilter').disabled = true;
    updateTreeEditPanel();
  };


$('analysisTreeBtn').onclick =
  async () => {
    if (
      !state.analysis
        ?.analysis_tree
    ) {
      return;
    }

    state.activeTree =
      'analysis';

    $('analysisTreeBtn')
      .classList
      .add(
        'active'
      );

    $('physicalTreeBtn')
      .classList
      .remove(
        'active'
      );

    if ($('treeFilter')) {
      $('treeFilter').disabled = false;
      $('treeFilter').value = 'all';
    }
    try {
      const data = await api(`/api/tree/${state.scan.scan_id}?kind=analysis&filter=all&limit=100`);
      state.analysis.analysis_tree = data.node || state.analysis.analysis_tree;
    } catch (_) {
      // Keep the already loaded root if a transient page request fails.
    }
    renderTree(state.analysis.analysis_tree);
    updateTreeEditPanel();
  };

if ($('treeFilter')) $('treeFilter').onchange = applyTreeFilter;
document.addEventListener('click', closeTreeContextMenu);
window.addEventListener('resize', closeTreeContextMenu);
if ($('treeRenameBtn')) $('treeRenameBtn').onclick = async () => {
  const node = state.selected;
  if (!node?.node_id) return;
  const name = window.prompt('输入新的主题名称：', node.name || '');
  if (!name || !name.trim()) return;
  try { await submitTreeEdit('rename', { node_id: node.node_id, name: name.trim() }); }
  catch (error) { toast(error.message || '重命名失败', true); }
};
if ($('treeConfirmBtn')) $('treeConfirmBtn').onclick = async () => {
  const node = state.selected;
  if (!node?.node_id) return;
  try { await submitTreeEdit('confirm', { node_id: node.node_id, confirmed: true }); }
  catch (error) { toast(error.message || '确认分类失败', true); }
};
if ($('treeMountBtn')) $('treeMountBtn').onclick = async () => {
  const node = state.selected;
  const selectedGroups = [...state.selectedNodes.values()].filter((item) => item.kind === 'group' && item.node_id);
  const target = node?.kind === 'group' ? node : selectedGroups[0];
  if (!target?.node_id) return;
  const defaultPath = node?.kind === 'file' ? node.path : '';
  const path = window.prompt('输入要挂载到此主题的已解析文件相对路径：', defaultPath);
  if (!path || !path.trim()) return;
  try { await submitTreeEdit('mount', { node_id: target.node_id, path: path.trim() }); }
  catch (error) { toast(error.message || '挂载主题失败', true); }
};
if ($('treeMergeBtn')) $('treeMergeBtn').onclick = async () => {
  const groups = [...state.selectedNodes.values()].filter((item) => item.kind === 'group' && item.node_id);
  if (groups.length < 2) return;
  const name = window.prompt('输入合并后的主题名称：', '合并主题');
  if (!name || !name.trim()) return;
  try { await submitTreeEdit('merge', { node_ids: groups.map((item) => item.node_id), name: name.trim() }); }
  catch (error) { toast(error.message || '合并主题失败', true); }
};
if ($('treeSplitBtn')) $('treeSplitBtn').onclick = async () => {
  const node = state.selected;
  if (!node?.node_id) return;
  openSplitDialog(node);
};


$('testBtn').onclick =
  async () => {
    const btn =
      $('testBtn');

    setBusy(
      btn,
      true,
      '连接中…'
    );

    try {
      const data =
        await api(
          '/api/test-model',
          {
            method: 'POST',

            body: JSON.stringify({})
          }
        );

      toast(
        `${data.reply}（${data.model}）`
      );

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


$('evidenceScopeAllBtn').onclick = () => {
  state.retrievalScope = 'package';
  state.lastRetrievalId = null;
  renderEvidenceScopeControl();
};
$('evidenceScopeSelectionBtn').onclick = () => {
  if (!state.selected || (!state.selected.path && !state.selected.node_id)) {
    toast('请先从资料目录选择一个文件、目录或主题', true);
    return;
  }
  state.retrievalScope = 'selection';
  state.lastRetrievalId = null;
  renderEvidenceScopeControl();
};
renderEvidenceScopeControl();

/*
 * ============================================================
 * 深度摘要
 *
 * 核心修改：
 * group 节点如果有 node_id，
 * 就把 node_id 一起发给后端。
 * ============================================================
 */
$('summaryBtn').onclick =
  async () => {
    if (!state.scan || !state.selected) return;
    const btn = $('summaryBtn');
    setBusy(btn, true, '正在读取文件摘要…');
    $('summary').textContent = '正在读取导入阶段生成的文件或节点摘要。';
    try {
      const summaryPayload = {
        scan_id: state.scan.scan_id,
        path: state.selected.path || '.',
        kind: state.selected.kind,
        force: false,
      };
      if (state.selected.kind === 'group' && state.selected.node_id) {
        summaryPayload.node_id = state.selected.node_id;
      }
      const data = await api('/api/summary', { method: 'POST', body: JSON.stringify(summaryPayload) });
      if (data.accepted && data.job_id) {
        toast('摘要正在生成，完成后会自动刷新。');
        await pollJob(data.job_id);
        return;
      }
      state.summary = data.summary;
      const returnedPath = data.summary?.node_path || state.selected.path;
      const returnedType = data.summary?.summary_type || (state.selected.kind === 'directory' ? 'folder' : 'file');
      if (returnedPath) state.summaries.set(summaryKey(returnedPath, returnedType), data.summary);
      if (state.selected.kind === 'group' && state.selected.node_id) {
        state.summaries.set(summaryKey(`node:${state.selected.node_id}`, 'folder'), data.summary);
      }
      renderSummary(data.summary, state.selected.kind === 'group' ? '节点摘要' : '文件摘要');
      toast(data.cached ? '已读取导入阶段摘要' : '摘要生成完成', Boolean(data.degraded));
    } catch (e) {
      $('summary').textContent = e.message;
      toast(e.message, true);
    } finally {
      setBusy(btn, false);
    }
  };


$('retryBtn').onclick = async () => {
  if (!state.scan) return;
  const btn = $('retryBtn');
  setBusy(btn, true, '正在重试…');
  try {
    const data = await api(`/api/retry-failed/${state.scan.scan_id}`, { method: 'POST', body: JSON.stringify({}) });
    if (data.job_id) await pollJob(data.job_id);
    else toast(data.message || '当前没有失败文件');
  } catch (e) { toast(e.message, true); }
  finally { setBusy(btn, false); }
};


$('reportBtn').onclick =
  async () => {
    if (!state.scan) {
      return;
    }

    const btn =
      $('reportBtn');

    setBusy(
      btn,
      true,
      '概览生成中…'
    );

    try {
      const data =
        await api(
          '/api/report',
          {
            method: 'POST',

            body: JSON.stringify({
              scan_id:
                state.scan.scan_id
            })
          }
        );

      await pollJob(data.job_id);

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


$('deepenBtn').onclick =
  async () => {
    if (!state.scan || !state.selected) {
      return;
    }
    const btn = $('deepenBtn');
    setBusy(btn, true, '正在创建补充任务…');
    try {
      const payload = {
        scan_id: state.scan.scan_id,
        path: state.selected.path || '.',
        node_id: state.selected.node_id || null
      };
      const data = await api('/api/analyze-scope', {
        method: 'POST', body: JSON.stringify(payload)
      });
      toast(`已开始补充分析“${data.scope_label}”，本批最多 ${data.batch_limit} 个文件。`);
      await pollJob(data.job_id);
    } catch (e) {
      toast(e.message, true);
    } finally {
      setBusy(btn, false);
    }
  };


$('exportBtn').onclick =
  async () => {
    if (!state.scan || (!state.selected && !state.selectedNodes.size)) {
      return;
    }

    const btn =
      $('exportBtn');

    setBusy(
      btn,
      true,
      '正在打包…'
    );

    const taskTopic =
      window.prompt(
        '请输入整编任务主题（必填）',
        ''
      );

    if (
      !taskTopic
      || !taskTopic.trim()
    ) {
      setBusy(
        btn,
        false
      );

      toast(
        '未指定整编任务主题，已取消导出',
        true
      );

      return;
    }

    try {
      const data =
        await api(
          '/api/export',
          {
            method: 'POST',

            body: JSON.stringify({
              scan_id:
                state.scan.scan_id,
              selections: state.selectedNodes.size
                ? [...state.selectedNodes.values()]
                : [exportPayloadNode(state.selected)],
              task_topic:
                taskTopic.trim()
            })
          }
        );

      toast('已提交待整编任务，Worker 将生成去重资料包和统一交接说明。');
      await pollJob(data.job_id);

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


function selectedRetrievalScope() {
  const selected = state.selected;
  const canUseSelection = Boolean(selected && (selected.path || selected.node_id));
  if (state.retrievalScope !== 'selection' || !canUseSelection) {
    return { mode: 'package', path: '.', label: '整个数据包', selected: null };
  }
  return {
    mode: 'selection',
    path: selected.path || '.',
    nodeId: selected.kind === 'group' && selected.node_id ? selected.node_id : null,
    label: selected.name || selected.path || '当前选择',
    selected
  };
}

function renderEvidenceScopeControl() {
  const chip = $('evidenceScopeChip');
  const all = $('evidenceScopeAllBtn');
  const selection = $('evidenceScopeSelectionBtn');
  if (!chip || !all || !selection) return;
  const scope = selectedRetrievalScope();
  const canUseSelection = Boolean(state.selected && (state.selected.path || state.selected.node_id));
  chip.textContent = scope.mode === 'selection' ? `当前选择 · ${scope.label}` : '整个数据包';
  all.classList.toggle('is-active', scope.mode === 'package');
  selection.classList.toggle('is-active', scope.mode === 'selection');
  selection.disabled = !canUseSelection;
  selection.title = canUseSelection ? `限定到：${state.selected.name || state.selected.path}` : '请先从资料目录选择一个文件、目录或主题';
}

function applyRetrievalScope(payload) {
  const scope = selectedRetrievalScope();
  payload.path = scope.path;
  if (scope.nodeId) payload.node_id = scope.nodeId;
  return scope;
}

/*
 * ============================================================
 * 本地 RAG
 *
 * 核心修改：
 * 如果当前是主题节点，
 * 把 node_id 一起传给后端。
 * 后端会根据 member_paths 只检索该主题文件。
 * ============================================================
 */
$('retrievalBtn').onclick =
  async () => {
    if (!state.scan) {
      return;
    }

    const query =
      $('retrievalQuery')
        .value
        .trim();

    if (!query) {
      toast(
        '请输入要检索的问题',
        true
      );

      return;
    }

    const btn =
      $('retrievalBtn');

    setBusy(
      btn,
      true,
      '本地检索中…'
    );

    try {
      const payload = {
        scan_id:
          state.scan.scan_id,

        query:
          query,

        top_k:
          12,

        previous_result_id:
          state.lastRetrievalId
      };

      const requestedScope = applyRetrievalScope(payload);

      const data =
        await api(
          '/api/retrieve',
          {
            method: 'POST',

            body:
              JSON.stringify(
                payload
              )
          }
        );

      const result =
        data.retrieval;

      state.lastRetrievalId =
        result.result_id
        || null;

      $('summary').className =
        'summary';

      const scopeLabel = result.node_name || requestedScope.label || result.scope;

      $('summary').innerHTML =
        `<div class="summary-kicker">本地混合检索 RAG</div>`
        +
        `<h2>${
          escapeHtml(
            result.query
          )
        }</h2>`
        +
        `<p>`
        +
        `范围：${
          escapeHtml(
            scopeLabel
            || '整个数据包'
          )
        }；`
        +
        `方法：${
          escapeHtml(
            result.method
          )
        }；`
        +
        `检索语料 ${
          result.corpus_chunks
        } 个证据块。`
        +
        `</p>`
        +
        retrievalStatusHtml(result)
        +
        evidenceHtml(
          result.results
        )
        +
        (
          result.warnings
            ?.length

            ? (
                `<h3>检索说明</h3>`
                +
                `<ul>${
                  result.warnings
                    .map(
                      x =>
                        `<li>${escapeHtml(x)}</li>`
                    )
                    .join('')
                }</ul>`
              )

            : ''
        );

      toast(
        `已返回 ${
          result.result_count
          || 0
        } 条可追溯证据`
      );

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


async function runFileSearch(page = 1) {
  if (!state.scan) return;
  const query = $('retrievalQuery')?.value.trim();
  if (!query) {
    toast('请输入要搜索的关键词或问题', true);
    return;
  }
  const btn = $('fileSearchBtn');
  setBusy(btn, true, '搜索文件中…');
  try {
    const payload = {
      scan_id: state.scan.scan_id,
      query,
      page,
      page_size: 50,
    };
    applyRetrievalScope(payload);
    const data = await api('/api/search/files', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    state.fileSearchResult = data;
    state.fileSearchPage = Number(data.page || page);
    renderFileSearchResult(data);
    $('evidenceSummary').textContent = `${data.conclusion || '搜索完成'}${data.coverage?.complete === false ? ' 当前索引尚未完全覆盖，结果请在索引完成后复查。' : ''}`;
    toast(`已找到 ${Number(data.matched_file_count || 0)} 个相关文件`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

$('fileSearchBtn').onclick = () => runFileSearch(1);


$('numericQuestionBtn').onclick =
  async () => {
    if (!state.scan) return;
    const question = $('numericQuestion').value.trim();
    if (!question) {
      toast('请输入数字统计问题', true);
      return;
    }
    const btn = $('numericQuestionBtn');
    setBusy(btn, true, '计算中…');
    try {
      const payload = {
        scan_id: state.scan.scan_id,
        question,
        path: '.'
      };
      applyRetrievalScope(payload);
      const data = await api('/api/ask', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
      const answer = data.answer || {};
      const answerCoverage = answer.coverage || {};
      const exactResult = answerCoverage.complete !== false;
      const aggregationScope = answer.aggregation_scope || {};
      $('summary').className = 'summary';
      $('summary').innerHTML =
        `<div class="summary-kicker">${exactResult ? '可验证精确统计' : '可验证部分覆盖统计'}</div>` +
        `<h2>${escapeHtml(answer.question || question)}</h2>` +
        `<div class="metric-grid"><div><b>${escapeHtml(answer.value ?? '—')}</b><span>${escapeHtml(answer.operation || '结果')}</span></div>` +
        `<div><b>${escapeHtml(answer.column || '记录数')}</b><span>字段</span></div>` +
        `<div><b>${escapeHtml(answer.confidence || '—')}</b><span>置信度</span></div></div>` +
        `<p>来源：${escapeHtml(answer.source_path || '当前范围')}；表/成员：${escapeHtml(answer.table || '—')}</p>` +
        `<p>参与 ${escapeHtml(aggregationScope.participating_source_count ?? answer.source_paths?.length ?? 1)} 个文件、${escapeHtml(aggregationScope.participating_profile_count ?? 1)} 张表` +
        `${answer.calculation ? `；计算口径：${escapeHtml(answer.calculation)}` : ''}</p>` +
        `${answerCoverage.complete === false ? `<p class="coverage-card"><strong>覆盖提示：</strong>${escapeHtml(answerCoverage.warning || '结果基于有界采样，请回原表复核。')}</p>` : ''}` +
        evidenceHtml(answer.evidence || []);
      toast(exactResult ? '已返回带来源定位的精确统计结果' : '已返回部分覆盖统计，结论中已标明采样限制');
    } catch (e) {
      toast(e.message, true);
    } finally {
      setBusy(btn, false);
    }
  };


// Present one user-facing Smart Parse action while retaining the legacy
// hidden select so old state hydration remains compatible.
const smartParseBox = document.querySelector('.parse-mode-box');
if (smartParseBox && !smartParseBox.querySelector('.smart-parse-choice')) {
  const label = smartParseBox.querySelector('label');
  if (label) label.textContent = '解析方式';
  const choice = document.createElement('div');
  choice.className = 'smart-parse-choice';
  choice.innerHTML = '<span class="status-dot"></span><b>智能解析（自动分流）</b><small>系统会按文件类型、文本层、版面复杂度和 OCR 置信度自动选择快速或高精度解析。</small>';
  smartParseBox.appendChild(choice);
  const help = $('parseModeHelp');
  if (help) help.textContent = '普通文件优先快速解析；扫描件、复杂版面和低置信度文件自动进入高精度队列。';
}

$('parseMode').onchange =
  () => {
    $('parseModeHelp')
      .textContent =
        $('parseMode').value
          === 'fast'

          ? (
              '快速提取正文；扫描型 PDF 仅预览前几页 OCR。'
              +
              '需要完整版面、表格和图片识别时请选择高精度解析。'
            )

          : (
              '使用 Docling 完成版面分析、OCR、TableFormer 表格识别和 '
              +
              'Office 内嵌图片 OCR，耗时会明显增加。'
            );
  };


async function refreshModelStatus() {
  try {
    const data =
      await api(
        '/api/status'
      );

    state.modelGenerationEnabled =
      data.model_generation_enabled
      !== false;

    if (
      !state.modelGenerationEnabled
    ) {
      if ($('testBtn')) $('testBtn').textContent =
        '检查本机模型状态';

      $('summaryBtn').title =
        '共享模型未启用，避免影响实验室其他用户';

      if (state.selected) {
        $('summaryBtn').disabled =
          true;
      }
    }

  } catch (_) {
    state.modelGenerationEnabled =
      null;
  }
}

// The package entry page is intentionally limited to history selection and
// new-data import.  Model diagnostics remain available from Settings and do
// not compete with the import workflow.
if ($('testBtn')) $('testBtn').hidden = true;
const exploreTools = document.querySelector('.explore-tools');
if (exploreTools && !exploreTools.querySelector('[data-go-route="packages"]')) {
  const backButton = document.createElement('button');
  backButton.type = 'button';
  backButton.className = 'text-button';
  backButton.dataset.goRoute = 'packages';
  backButton.textContent = '返回数据包';
  exploreTools.prepend(backButton);
}


window.addEventListener('online', refreshTaskCenter);
window.SJFXTasks = {
  refresh: refreshTaskCenter,
  cancel: cancelJob
};

async function initializeApp() {
  bindDataSourceControls();
  bindSelectionControls();
  $('candidateOpenDirectoryBtn')?.addEventListener('click', async () => {
    if (!state.scan?.scan_id) return;
    setPackageStage('analysis');
    await refreshScan(state.scan.scan_id);
    state.activeTree = 'analysis';
    if (state.analysis?.analysis_tree) {
      $('analysisTreeBtn')?.click();
    }
    $('tree')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  $('candidateBackSelectionBtn')?.addEventListener('click', () => {
    openSelectionPanel(state.scan?.scan_id);
  });
  $('candidatePreviewTopics')?.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-large-category-node]');
    if (!button || !state.scan?.scan_id) return;
    setBusy(button, true, '正在生成计划…');
    try {
      const data = await api(
        `/api/scan/${encodeURIComponent(state.scan.scan_id)}/deep-selection`,
        {
          method: 'POST',
          body: JSON.stringify({ node_id: button.dataset.largeCategoryNode }),
        }
      );
      state.jobId = data.job_id || state.jobId;
      toast(`已选择 ${data.normal_parse_count || 0} 个文件解析，${data.model_candidate_count || 0} 个高价值文件进入模型；${data.deferred_count || 0} 个文件留待后续。`);
      await pollJob(data.job_id);
    } catch (error) {
      toast(error.message || '无法生成大数据包解析计划', true);
    } finally {
      if (button.isConnected) setBusy(button, false);
    }
  });
  $('refreshDeepResultsBtn')?.addEventListener('click', (event) => {
    runPackageProcessingAction('refresh-deep-results', event.currentTarget);
  });
  await refreshModelStatus();
  await startTaskCenterRefresh();
  await refreshDataSources();
  await restoreWorkspace();
}

initializeApp().catch((error) => toast(error.message || '工作区恢复失败', true));
