const state = {
  scan: null,
  selected: null,
  summary: null,
  analysis: null,
  progressiveAnalysis: null,
  progressiveRefreshKey: null,
  analysisTreeOriginal: null,
  summaries: new Map(),
  activeTree: 'physical',
  jobId: null,
  modelGenerationEnabled: null,
  lastRetrievalId: null,
  selectedNodes: new Map(),
  treeEdits: [],
  jobs: new Map(),
  jobsEndpointAvailable: null,
  taskCenterRefreshInFlight: false,
  selectionRequestId: 0
};

const ACTIVE_JOB_STATUSES = new Set(['queued', 'running', 'cancelling']);
const TASK_REGISTRY_KEY = 'sjfx_task_registry_v1';

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


async function api(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  let token = window.sessionStorage.getItem('sjfx_api_token') || '';
  if (token) headers['X-SJFX-Token'] = token;
  let response = await fetch(url, { ...options, headers });
  if (response.status === 401) {
    window.sessionStorage.removeItem('sjfx_api_token');
    delete headers['X-SJFX-Token'];
    token = window.prompt('访问凭据已失效，请重新输入 SJFX API Token', '')
      || '';
    if (token) {
      window.sessionStorage.setItem('sjfx_api_token', token);
      headers['X-SJFX-Token'] = token;
      response = await fetch(url, { ...options, headers });
    }
  }
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
    // Reverse proxies can return an HTML error page while the Worker/Web
    // process restarts. Keep the HTTP status so polling can retry safely.
  }
  if (!response.ok || !data.ok) {
    const error = new Error(data.error || `请求失败（HTTP ${response.status}）`);
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
      const data = await api(
        `/api/analysis-node-members/${state.scan.scan_id}?node_id=${encodeURIComponent(node.node_id)}&limit=500`
      );
      files = data.members || [];
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
  if (node.kind === 'directory') {
    return (
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
  if (
    node.kind === 'group'
    && node.node_id
  ) {
    const cached =
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

      topics: [],

      evidence_chain:
        node.evidence_chain || []
    };
  }

  return null;
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
  if ($('numericQuestionBtn')) {
    $('numericQuestionBtn').disabled = !state.scan;
  }

  let local = localSummaryFor(node);
  if (!local && state.scan && (node.kind === 'directory' || isVirtualGroup)) {
    const summaryPath = isVirtualGroup ? `node:${node.node_id}` : (node.path || '.');
    try {
      const page = await api(
        `/api/summaries/${state.scan.scan_id}?path=${encodeURIComponent(summaryPath)}&type=folder&limit=1`
      );
      if (!selectionStillCurrent()) return;
      const item = (page.items || [])[0];
      if (item) {
        state.summaries.set(summaryKey(item.path, item.type), item.payload);
        local = item.payload;
      }
    } catch (_) {
      // A missing local summary is valid while analysis is still running.
      if (!selectionStillCurrent()) return;
    }
  }

  if (!selectionStillCurrent()) return;

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
            `<strong>${
              escapeHtml(
                loc
                || '未知位置'
              )
            }</strong>`
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

  const summary =
    data.summary
    || data.core_summary;

  if (summary) {
    html +=
      `<p>${
        escapeHtml(summary)
      }</p>`;
  }

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

  if (
    data.topics?.length
  ) {
    html +=
      `<h3>内容主题</h3><div>${
        data.topics
          .map(
            x =>
              `<span class="tag">${escapeHtml(x)}</span>`
          )
          .join('')
      }</div>`;
  }

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
                  typeof x === 'string'
                    ? x
                    : JSON.stringify(x)
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
      +
      `<pre>${
        escapeHtml(
          JSON.stringify(
            data.structure_overview,
            null,
            2
          )
        )
      }</pre>`;
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

    html += `<p class="coverage-card"><strong>优先级：</strong>${escapeHtml(d.priority || '—')}；<strong>评分：</strong>${escapeHtml(d.score ?? '—')}；<strong>证据状态：</strong>${escapeHtml(d.evidence_status || (d.evidence_chain?.length ? 'supported' : 'insufficient'))}</p>`;

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
            `<p><strong>回答：</strong>${escapeHtml(item.answer || item.statement || '暂无可回查回答')} <span class="inference-badge">${escapeHtml(item.confidence || '待核验')}</span></p>`
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

  // A retry/deepening run can coexist with the previous final analysis.
  // Prefer the live compact card while it exists so coverage and pending/error
  // counts never appear frozen at the previous run.
  const displayedAnalysis = state.progressiveAnalysis || state.analysis || {};
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
}


async function refreshScan(scanId = state.scan?.scan_id) {
  if (!scanId) {
    return;
  }
  const data =
    await api(
      `/api/scan/${scanId}?compact=1&summary_limit=100`
    );

  state.scan =
    data.scan;

  state.analysis =
    data.analysis;
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

  $('reportBtn').disabled =
    false;

  $('reanalyzeBtn').disabled =
    false;
  $('retryBtn').disabled = !(state.analysis?.statistics?.failed_files > 0);

  $('retrievalBtn').disabled =
    !state.analysis;
  if ($('numericQuestionBtn')) {
    $('numericQuestionBtn').disabled = !state.analysis;
  }

  updateSelectionCart();
  updateTreeEditPanel();
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
      + (active ? `<button type="button" class="danger compact" data-job-cancel="${escapeHtml(job.id)}" ${cancelling ? 'disabled' : ''}>${cancelling ? '正在取消…' : (status === 'queued' ? '取消排队' : '取消任务')}</button>` : '')
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
        const data = await api('/api/jobs?status=active&limit=50');
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


function startTaskCenterRefresh() {
  loadTaskRegistry();
  renderTaskCenter();
  refreshTaskCenter();
  window.setInterval(refreshTaskCenter, 3000);
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
    button.textContent = cancelling ? '正在取消…' : '取消当前任务';
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
          toast('原始目录已加载，后台继续进行深度分析。');
        }
        updateStats();
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
            ? `<p>${downloadLink(result.download_url, '下载待整编数据包')}</p>`
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

      if (completedScanId) {
        state.scan = { scan_id: completedScanId };
        await refreshScan(completedScanId);
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
          : '完整分析、证据链和概览 Word 已生成'
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
        || '完整分析失败'
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
    state.progressiveAnalysis = null;
    state.progressiveRefreshKey = null;
    state.summary = null;
    state.summaries = new Map();
    state.selected = null;
    state.selectedNodes = new Map();
    state.activeTree = 'physical';
    $('tree').className = 'tree';
    renderInitialPhysicalTree($('rootPath').value);
    $('analysisTreeBtn').classList.remove('active');
    $('physicalTreeBtn').classList.add('active');
    $('analysisTreeBtn').disabled = true;
    $('reportBtn').disabled = true;
    $('reanalyzeBtn').disabled = true;
    $('retrievalBtn').disabled = true;
    if ($('numericQuestionBtn')) $('numericQuestionBtn').disabled = true;
    updateSelectionCart();

    try {
      const data =
        await api(
          '/api/scan',
          {
            method: 'POST',

            body: JSON.stringify({
              path:
                $('rootPath').value,

              parse_mode:
                $('parseMode').value
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

              parse_mode:
                $('parseMode').value
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
    if (
      !state.scan
      || !state.selected
    ) {
      return;
    }

    const btn =
      $('summaryBtn');

    setBusy(
      btn,
      true,
      '模型深度分析中…'
    );

    $('summary')
      .textContent =
        '本地模型正在对当前节点进行深度分析，可能需要数分钟。请保持页面打开。';

    try {
      const payload = {
        scan_id:
          state.scan.scan_id,

        path:
          state.selected.path
          || '.',

        kind:
          state.selected.kind
      };

      /*
       * 主题节点：
       * 把 node_id 发给 app.py
       */
      if (
        state.selected.kind
          === 'group'
        &&
        state.selected.node_id
      ) {
        payload.node_id =
          state.selected.node_id;
      }

      const data =
        await api(
          '/api/summary',
          {
            method: 'POST',

            body:
              JSON.stringify(
                payload
              )
          }
        );

      if (data.accepted && data.job_id) {
        toast('已提交深度摘要任务，Worker 正在处理当前节点。');
        await pollJob(data.job_id);
        return;
      }

      state.summary =
        data.summary;

      /*
       * 虚拟主题摘要加入前端缓存。
       */
      if (
        state.selected.kind
          === 'group'
        &&
        state.selected.node_id
      ) {
        state.summaries.set(
          summaryKey(
            `node:${state.selected.node_id}`,
            'folder'
          ),
          data.summary
        );
      }

      renderSummary(
        data.summary,

        state.selected.kind
          === 'group'

          ? '主题节点深度摘要'

          : '模型深度摘要'
      );

      toast(
        data.degraded

          ? '部分步骤降级，已返回可用摘要'

          : (
              data.cached

                ? '已读取缓存摘要'

                : '深度摘要生成完成'
            ),

        data.degraded
      );

    } catch (e) {
      $('summary')
        .textContent =
          e.message;

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

        path:
          state.selected
            ?.path
          || '.',

        top_k:
          12,

        previous_result_id:
          state.lastRetrievalId
      };

      /*
       * 当前选中的是主题节点。
       */
      if (
        state.selected
          ?.kind
          === 'group'
        &&
        state.selected
          ?.node_id
      ) {
        payload.node_id =
          state.selected.node_id;
      }

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

      const scopeLabel =
        result.node_name
        ||
        (
          state.selected
            ?.kind
            === 'group'

            ? state.selected.name

            : result.scope
        );

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
        path: state.selected?.path || '.'
      };
      if (state.selected?.node_id) payload.node_id = state.selected.node_id;
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
      $('testBtn').textContent =
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


refreshModelStatus();
startTaskCenterRefresh();

window.addEventListener('online', refreshTaskCenter);
window.SJFXTasks = {
  refresh: refreshTaskCenter,
  cancel: cancelJob
};
