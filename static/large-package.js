(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>\"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
  const api = () => window.sjfxApi;
  let packageId = '';
  let pollTimer = null;
  let pollingNeeded = true;
  let selected = new Set();
  let files = [];
  let lastCatalogAt = 0;
  let directoryPending = 0;
  let currentStage = 'import';
  let workspaceStatus = null;
  let showAllFiles = false;
  let fileOffset = 0;
  let fileTopic = '';
  let fileNodeId = '';
  let fileCategory = '';
  let refreshing = false;
  // 晚到响应守卫：每次请求记下"这一发是给哪个包的"，回来时若包的上下文已经变了
  // 就整包丢弃。框架 §7.2 明确要求"前端丢弃较旧请求的晚到响应"——否则用户在
  // 历史里快速切换数据包时，旧包的响应会盖到新包的界面上。
  let contextSeq = 0;
  const contextToken = () => `${packageId}#${contextSeq}`;
  const stillCurrent = (token) => token === contextToken();
  const activeView = () => !document.body.dataset.route || document.body.dataset.route === 'large-select';
  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  function startPolling() {
    if (packageId && activeView() && pollingNeeded && !pollTimer) pollTimer = setInterval(refresh, 1500);
  }
  function activate(route) {
    if (route !== 'large-select') { stopPolling(); return; }
    if (packageId) { startPolling(); refresh(); }
  }
  const selectableStatuses = new Set(['waiting_for_selection', 'completed']);
  const activeStatuses = new Set(['queued', 'running']);

  function mode() {
    return document.querySelector('input[name="workflowMode"]:checked')?.value || 'standard';
  }
  function setModeText() {
    const large = mode() === 'large';
    const btn = $('scanBtn');
    const label = large ? '启动大数据包流式处理' : '仅导入目录';
    if (btn && !btn.disabled) btn.textContent = label;
    else if (btn?.dataset.label) btn.dataset.label = label;
    const note = $('workflowModeNote');
    if (note) note.textContent = large
      ? '大数据包生成智能目录后即完成导入；完成后可按需选择文件生成深度摘要。'
      : '普通模式保持现有导入分析流程。';
  }
  function showPanel(show) {
    const panel = $('largePackagePanel');
    if (panel) panel.hidden = !show;
  }
  function setStage(stage) {
    currentStage = stage || 'import';
    if (currentStage === 'import' || currentStage === 'normal') {
      const nextMode = currentStage === 'import' ? 'large' : 'standard';
      document.querySelectorAll('input[name="workflowMode"]').forEach((input) => { input.checked = input.value === nextMode; });
      setModeText();
    }
    if (currentStage === 'normal') {
      if (typeof window.setPackageStage === 'function') window.setPackageStage('source');
      $('dataSourcePanel')?.style.removeProperty('display');
      $('largePackagePanel')?.setAttribute('hidden', 'hidden');
      return;
    }
    if (typeof window.setPackageStage === 'function') window.setPackageStage(currentStage === 'import' ? 'import' : 'source');
    const source = $('dataSourcePanel');
    const importCard = $('newImportCard');
    const panel = $('largePackagePanel');
    if (source) source.style.display = 'none';
    if (importCard) importCard.classList.toggle('hidden', currentStage !== 'import');
    if (panel) panel.hidden = currentStage === 'import';
    if (panel) {
      panel.dataset.largeStage = currentStage;
      const catalog = panel.querySelector('.large-package-catalog-grid');
      if (catalog) catalog.hidden = currentStage !== 'catalog';
      const selection = $('largePackageSelectionBox');
      if (selection) selection.hidden = currentStage !== 'select' || !(panel.dataset.ready === 'true');
      const report = $('largePackageReport');
      if (report) report.hidden = currentStage !== 'overview';
    }
    if (currentStage === 'import') {
      $('largePackagePanel')?.setAttribute('aria-hidden', 'true');
      $('rootPath')?.focus();
    } else {
      $('largePackagePanel')?.removeAttribute('aria-hidden');
    }
    if (currentStage === 'select' && !packageId) {
      // 直接从导航进入"选择深度摘要"时恢复最近打开的大包，或给出引导。
      const remembered = String(window.sjfxLargeResultId || '');
      if (remembered && window.openLargePackageHistory) {
        openLargePackageHistory(remembered, 'select');
        return;
      }
      const status = $('largePackageStatus');
      if (status) status.textContent = '请先在"大数据包导入"页导入，或从数据包历史里打开一个大数据包，再来选择深度摘要文件。';
    }
  }
  function render(status, workflow) {
    if (!status) return;
    // **状态口径统一**：后端 `/workflow` 快照是唯一权威。有它就优先用它的
    // status/phase/progress/message、allowed_actions（按钮可见性）与
    // 阻塞/可选待办拆分；没有快照就完全按原来的 packages 字段推断，旧后端不变。
    const wf = workflow && typeof workflow === 'object' ? workflow : null;
    const view = wf ? Object.assign({}, status, {
      status: wf.status || status.status,
      phase: wf.phase || status.phase,
      progress: Number(wf.progress == null ? status.progress : wf.progress) || 0,
      message: wf.message || status.message,
    }) : status;
    workspaceStatus = { packageId, item: view, workflow: wf };
    window.dispatchEvent(new CustomEvent('sjfx-large-state'));
    const actions = wf && wf.allowed_actions ? wf.allowed_actions : null;
    const counts = view.counts || {};
    const quick = counts.quick || {};
    const deep = counts.deep || {};
    // 待办必须分两种口径显示，否则"已完成"和"还有一万多项"会同时出现。
    const pendingNote = wf && Number(wf.pending_total || 0)
      ? ` · 待办 阻塞 ${Number(wf.pending_blocking || 0)} / 可选 ${Number(wf.pending_optional || 0)}` : '';
    // 目录版本与状态：明确"看的目录是哪一版、是草稿还是定稿、有没有降级"。
    const catalogNote = wf
      ? ` · 目录 ${wf.catalog_state === 'published' ? '已发布' : (wf.catalog_state || '无')}` +
        (wf.catalog_id ? `(${String(wf.catalog_id).slice(4, 12)})` : '') +
        (wf.catalog_degraded ? ` · ⚠ 降级目录（${(wf.catalog_degraded_detail || {}).fragment_groups || '?'}/${(wf.catalog_degraded_detail || {}).groups || '?'} 个叶组是碎片，未经语义聚类）` : '')
      : '';
    const importState = wf || status;
    const importNote = importState.import_completed
      ? '大数据包导入已完成（100%）；深度摘要可按需选择' : `大数据包导入进度 ${importState.import_progress ?? view.progress ?? 0}%`;
    const deepNote = view.phase === 'deep_parse' ? ` · 深度摘要进度 ${view.progress || 0}%` : '';
    const text = `${importNote}${deepNote} · ${view.message || ''}` +
      ` · 文件 ${counts.total_files || 0} · 快速摘要 ${quick.completed || 0} · 深度 ${deep.completed || 0}` + pendingNote + catalogNote;
    if ($('largePackageStatus')) $('largePackageStatus').textContent = text;
    if ($('largePackageProgress')) $('largePackageProgress').style.width = `${Math.max(0, Math.min(100, Number(view.progress || 0)))}%`;
    const ready = actions ? !!actions.select : selectableStatuses.has(String(view.status || '').toLowerCase());
    if ($('largePackagePanel')) $('largePackagePanel').dataset.ready = String(ready);
    if ($('largePackageSelectionBox')) $('largePackageSelectionBox').hidden = !ready;
    if ($('largePackageSelectionBox')) $('largePackageSelectionBox').hidden = currentStage !== 'select' || !ready;
    if ($('largePackageOpenCatalog')) $('largePackageOpenCatalog').hidden = !Number(counts.total_files || 0);
    // 有**已发布版本**就一定能看成果，哪怕包因为补充解析被重新排队。
    const hasOverview = wf
      ? Boolean(wf.published_view || wf.overview_state === 'published' || view.status === 'completed')
      : view.status === 'completed';
    if ($('largePackageOpenOverview')) $('largePackageOpenOverview').hidden = !hasOverview;
    if ($('largePackagePauseBtn')) $('largePackagePauseBtn').hidden = actions ? !actions.pause : !activeStatuses.has(String(view.status || '').toLowerCase());
    if ($('largePackageResumeBtn')) $('largePackageResumeBtn').hidden = actions ? !actions.resume : view.status !== 'paused';
    if ($('largePackageCancelBtn')) $('largePackageCancelBtn').hidden = actions ? !actions.cancel : !activeStatuses.has(String(view.status || '').toLowerCase());
    if ($('largePackageReport') && !hasOverview) $('largePackageReport').textContent = '';
    // U12：明确"本批依据哪一版目录"，并在目录降级时给出**可执行的修复入口**。
    // 后端已经有 catalog_degraded / current_selection.catalog_id / POST /catalog/rebuild，
    // 这里把最后一段接上，否则用户看得见"降级"却无从下手。
    if ($('largePackageBatchCatalog')) {
      const sel = (wf && wf.current_selection) || {};
      $('largePackageBatchCatalog').textContent = sel.catalog_id
        ? `本批依据目录 ${String(sel.catalog_id).slice(4, 12)}`
        : '本批未记录目录版本（旧批次）';
    }
    let rebuild = $('largePackageRebuildCatalog');
    if (wf && wf.catalog_degraded && $('largePackageSmartStats') && !rebuild) {
      rebuild = document.createElement('button');
      rebuild.id = 'largePackageRebuildCatalog';
      rebuild.type = 'button';
      rebuild.className = 'secondary';
      rebuild.textContent = '重建语义目录（当前为降级目录）';
      rebuild.onclick = async () => {
        if (!window.confirm('重建会重排叶子分组与名称，已生成的目录名和报告骨架会随之变化。确定继续？')) return;
        rebuild.disabled = true;
        try {
          const r = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/catalog/rebuild`, {method: 'POST'});
          const rb = r.rebuild || {};
          window.alert(`重建完成：叶组 ${rb.groups_before} → ${rb.groups_after}；降级 ${(rb.degraded_before||{}).degraded} → ${(rb.degraded_after||{}).degraded}`);
          lastCatalogAt = 0;
          await refresh();
        } catch (e) {
          window.alert(e.message || '目录重建失败');
        } finally { rebuild.disabled = false; }
      };
      $('largePackageSmartStats').after(rebuild);
    }
  }
  function renderCatalog(catalog) {
    if (!catalog) return;
    const original = catalog.original || {};
    const smart = catalog.smart || {};
    directoryPending = (smart.naming?.active ? 1 : 0) + Number(smart.catalog_states?.pending || 0) + Number(smart.catalog_states?.grouped || 0) + Number(smart.pending_groups || 0);
    if ($('largePackageRootPath')) $('largePackageRootPath').textContent = catalog.root_path || '目录未记录';
    if ($('largePackageDirectoryStats')) $('largePackageDirectoryStats').textContent = `${catalog.files_total || 0} 个文件 · ${(original.directories || []).length} 个目录`;
    const originalRows = (original.directories || []).map((item) =>
      `<details class="large-catalog-node"><summary><b>${esc(item.path || '根目录')}</b><span>${item.files || 0} 个文件 · ${esc(item.bytes || 0)} bytes</span></summary><small>${(item.sample || []).map(esc).join(' · ')}</small></details>`
    );
    if (original.root_files?.length) originalRows.unshift(`<div class="large-catalog-root-files"><b>根目录文件</b><small>${original.root_files.map(esc).join(' · ')}</small></div>`);
    if ($('largePackageOriginalCatalog')) $('largePackageOriginalCatalog').innerHTML = originalRows.join('') || '<div class="large-empty">暂未发现文件。</div>';
    if ($('largePackageSmartStats')) $('largePackageSmartStats').textContent = `${(smart.categories || []).length} 个分类`;
    const smartRows = (smart.categories || []).map((item) =>
      `<details class="large-smart-category"><summary><b>${esc(item.category || '未分类')}</b><span>${item.files || 0} 个文件 · 置信度 ${Math.round(Number(item.confidence || 0) * 100)}%</span></summary><small>${(item.sample || []).map((file) => esc(file.path)).join(' · ')}</small></details>`
    );
    if ($('largePackageSmartCatalog')) $('largePackageSmartCatalog').innerHTML = window.SJFXLargePortrait?.catalog(catalog) || smartRows.join('');
  }
  function renderUserNodes(nodes) {
    const host = $('largePackageSmartCatalog');
    if (!host || !Array.isArray(nodes) || !nodes.length) return;
    const section = document.createElement('section');
    section.className = 'large-user-nodes';
    section.innerHTML = `<h4>自定义节点</h4>${nodes.map((node) =>
      `<details class="large-smart-category"><summary><b>${esc(node.name || '未命名节点')}</b><span>${(node.paths || []).length} 个文件</span></summary><small>${(node.paths || []).map(esc).join(' · ')}</small></details>`
    ).join('')}`;
    host.appendChild(section);
  }
  async function loadCatalog() {
    if (!packageId) return;
    lastCatalogAt = Date.now();
    const data = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/catalog`);
    renderCatalog(data.catalog);
    renderSelectCatalog(data.catalog);
    try {
      const nodes = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/nodes`);
      renderUserNodes(nodes.nodes || []);
    } catch (_) { /* custom nodes are an additive directory view */ }
  }
  function renderSelectCatalog(catalog) {
    // 选择深度摘要时按智能目录组织：点主题/分类即筛选下方文件列表。
    const tree = $('largePackageSelectCatalogTree');
    if (!tree) return;
    const smart = (catalog || {}).smart || {};
    const topicCount = smart.topic_total ?? (smart.topics || []).length;
    const hint = $('largePackageSelectCatalogHint');
    if (hint) {
      hint.textContent = topicCount
        ? window.SJFXLargePortrait.catalogSummary(catalog).text
        : '智能目录在快速解析完成后生成；当前可按原始列表选择。';
    }
    tree.innerHTML = window.SJFXLargePortrait?.catalog(catalog) || '<p class="large-empty">智能目录尚未生成。</p>';
    const reset = $('largePackageSelectCatalogAll');
    if (reset) reset.hidden = !(fileNodeId || fileTopic || fileCategory);
  }
  async function selectAllFiltered() {
    if (!packageId) return;
    const q = $('largePackageSearch')?.value || '';
    let offset = 0;
    let added = 0;
    for (let page = 0; page < 200; page += 1) {
      const data = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/files?limit=100&offset=${offset}&remaining=${showAllFiles ? 0 : 1}&node_id=${encodeURIComponent(fileNodeId)}&topic=${encodeURIComponent(fileTopic)}&category=${encodeURIComponent(fileCategory)}&q=${encodeURIComponent(q)}`);
      const batch = data.files || [];
      if (!batch.length) break;
      batch.forEach((item) => {
        const deepStatus = String(item.deep_status || '').toLowerCase();
        if (deepStatus !== 'queued' && deepStatus !== 'running' && !selected.has(item.path)) {
          selected.add(item.path);
          added += 1;
        }
      });
      offset += 100;
      if (offset >= Number(data.total || 0)) break;
    }
    await loadFiles();
    if ($('largePackageSelectionStats')) $('largePackageSelectionStats').textContent = `已把当前筛选的 ${added} 个文件加入选择 · 共选中 ${selected.size} 个`;
    if (window.toast) window.toast(`已选择当前筛选的 ${added} 个文件`);
  }
  async function loadReport() {
    const payload = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/report`);
    const report = payload.report || payload;
    const coverage = report.coverage || {};
    const failures = report.failures || [];
    if ($('largePackageReport')) $('largePackageReport').textContent =
      `专业概览：已盘点 ${coverage.inventory_files || 0}，快速解析 ${coverage.quick_completed || 0}，本地摘要 ${coverage.quick_completed || 0}，深度解析 ${coverage.deep_completed || 0}，模型摘要 ${coverage.model_summaries || 0}，失败/不支持 ${failures.length}。`;
  }
  async function refresh() {
    if (!activeView()) { stopPolling(); return; }
    if (!packageId || refreshing) return;
    refreshing = true;
    try {
      const token = contextToken();
      const data = await api()(`/api/large-packages/${encodeURIComponent(packageId)}`);
      if (!stillCurrent(token) || !activeView()) return;
      const pkg = data.large_package || {};
      const wf = data.workflow || null;
      render(pkg, wf);
      const statusValue = String((wf && wf.status) || pkg.status || '').toLowerCase();
      const catalogReady = Number(pkg.counts?.total_files || 0) > 0;
      if (catalogReady && Date.now() - lastCatalogAt > 5000) await loadCatalog();
      if (!stillCurrent(token) || !activeView()) return;
      if (statusValue === 'completed') await loadReport();
      if (!stillCurrent(token) || !activeView()) return;
      pollingNeeded = !(['failed','cancelled'].includes(statusValue) || (statusValue === 'completed' && !directoryPending));
      if (!pollingNeeded) stopPolling(); else startPolling();
      const selectable = wf && wf.allowed_actions ? !!wf.allowed_actions.select : selectableStatuses.has(statusValue);
      if (selectable && currentStage === 'select') await loadFiles();
    } catch (error) {
      if (!activeView()) return;
      if ($('largePackageStatus')) $('largePackageStatus').textContent = error.message || '状态读取失败';
      if ([401, 403, 404, 410].includes(error.status)) { pollingNeeded = false; stopPolling(); }
    } finally { refreshing = false; }
  }
  async function loadFiles() {
    const token = contextToken();
    const q = $('largePackageSearch')?.value || '';
    const data = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/files?limit=100&offset=${fileOffset}&remaining=${showAllFiles ? 0 : 1}&node_id=${encodeURIComponent(fileNodeId)}&topic=${encodeURIComponent(fileTopic)}&category=${encodeURIComponent(fileCategory)}&q=${encodeURIComponent(q)}`);
    if (!stillCurrent(token)) return;
    const allFiles = data.files || [];
    files = allFiles;
    const box = $('largePackageFiles');
    if (!box) return;
    box.innerHTML = files.map((item) => {
      const deepStatus = String(item.deep_status || 'idle').toLowerCase();
      const statusText = deepStatus === 'completed' ? '已完成深度解析' : (deepStatus === 'failed' ? '深度解析失败，可重试' : (deepStatus === 'queued' || deepStatus === 'running' ? '正在处理中' : '待深度解析'));
      const summary = String(item.local_summary?.sample || item.local_summary?.notice || item.error || '').replace(/\s+/g, ' ').slice(0, 240);
      const locked = deepStatus === 'queued' || deepStatus === 'running';
      return `<label class="large-file-row ${deepStatus === 'completed' ? 'is-completed' : ''}"><input type="checkbox" data-large-path="${esc(item.path)}" ${selected.has(item.path) ? 'checked' : ''} ${locked ? 'disabled' : ''}><span><b title="${esc(item.path)}">${esc(item.path)}</b><small>${esc(item.category || '未分类')} · ${esc(item.size_human || '')} · ${esc(statusText)} · ${esc(item.topic || '')}</small>${summary ? `<em title="${esc(summary)}">${esc(summary)}</em>` : ''}${item.local_summary?.portrait ? `<small>${esc(item.local_summary.portrait.classification_basis || '')}</small>` : ''}</span></label>`;
    }).join('') || '<div class="large-empty">当前没有待补充文件；点击“查看全部文件”可重新处理已完成文件。</div>';
    box.querySelectorAll('input[data-large-path]').forEach((input) => {
      input.addEventListener('change', () => input.checked ? selected.add(input.dataset.largePath) : selected.delete(input.dataset.largePath));
    });
    const total = Number(data.total || 0);
    let pager = $('largePackagePager');
    if (!pager) { pager = document.createElement('div'); pager.id = 'largePackagePager'; box.after(pager); }
    pager.innerHTML = window.SJFXLargePortrait.pager('largePackagePage', fileOffset, 100, total);
    $('largePackagePagePrev').onclick = () => { fileOffset = Math.max(0, fileOffset - 100); loadFiles(); };
    $('largePackagePageNext').onclick = () => { fileOffset += 100; loadFiles(); };
    if ($('largePackageSelectionStats')) $('largePackageSelectionStats').textContent = `匹配 ${total} 个文件 · 当前显示 ${files.length} 个 · 已选择 ${selected.size} 个`;
  }
  async function start() {
    const result = await api()('/api/large-packages', {method:'POST', body: JSON.stringify({path: $('rootPath')?.value || ''})});
    packageId = result.package_id;
    pollingNeeded = true;
    window.sjfxLargeResultId = packageId;
    selected = new Set();
    fileOffset = 0; fileTopic = ''; fileNodeId = ''; fileCategory = '';
    lastCatalogAt = 0;
    showPanel(true);
    if (window.SJFXShell) window.SJFXShell.activate('large-select');
    else setStage('select');
    await refresh();
    stopPolling();
    startPolling();
  }
  async function confirmSelection() {
    if (!selected.size) return window.alert('请至少选择一个文件');
    await api()(`/api/large-packages/${encodeURIComponent(packageId)}/select`, {method:'POST', body: JSON.stringify({paths: Array.from(selected), rule:{kind:'manual_ui'}})});
    selected.clear();
    if ($('largePackageSelectionStats')) $('largePackageSelectionStats').textContent = '已提交深度解析，后台继续处理。';
    pollingNeeded = true;
    stopPolling(); startPolling();
    await refresh();
  }
  window.openLargePackageHistory = async function (id, requestedStage = '') {
    requestedStage = typeof requestedStage === 'string' ? requestedStage : '';
    packageId = String(id || '');
    pollingNeeded = true;
    contextSeq += 1;                       // 换包：让所有在途响应立即失效
    window.sjfxLargeResultId = packageId;
    selected = new Set();
    fileOffset = 0; fileTopic = ''; fileCategory = '';
    lastCatalogAt = 0;
    showAllFiles = false;
    showPanel(true);
    if (requestedStage) setStage(requestedStage);
    await refresh();
    stopPolling();
    const current = await api()(`/api/large-packages/${encodeURIComponent(packageId)}`);
    const curStatus = String((current.workflow && current.workflow.status) || current.large_package?.status || '');
    const route = requestedStage || (curStatus === 'completed' ? 'large-overview' : 'large-select');
    if (window.SJFXShell) window.SJFXShell.activate(route); else setStage(route === 'large-overview' ? 'overview' : 'select');
    pollingNeeded = !['completed', 'failed', 'cancelled'].includes(curStatus) || (curStatus === 'completed' && directoryPending > 0);
    if (!pollingNeeded) stopPolling(); else startPolling();
  };
  async function control(action) {
    if (!packageId) return;
    const endpoint = action === 'pause' ? 'pause' : action === 'resume' ? 'resume' : 'cancel';
    if (action === 'cancel' && !window.confirm('停止后会保留已完成结果，但未完成文件不会继续处理。确定停止这个数据包吗？')) return;
    const token = contextToken(), targetId = packageId;
    const response = await api()(`/api/large-packages/${encodeURIComponent(targetId)}/${endpoint}`, { method: 'POST' });
    if (!stillCurrent(token)) return;
    // pause/resume/cancel 三个接口统一返回 snapshot（改动前 cancel 返回的是
    // 裸 large_package，页面因此渲染不出来）。两者都接受，然后立刻整体刷新一次。
    render(response.large_package || {}, response.snapshot || null);
    if (endpoint === 'cancel') { pollingNeeded = false; stopPolling(); }
    if (endpoint === 'resume') { pollingNeeded = true; startPolling(); }
    await refresh();
  }
  document.addEventListener('DOMContentLoaded', () => {
    // 放在最前面：不与下面 `if (!btn) return`（#scanBtn 缺失时的提前返回）耦合。
    ensureExportCenterEntry();
    document.querySelectorAll('input[name="workflowMode"]').forEach((input) => input.addEventListener('change', setModeText));
    setModeText();
    const btn = $('scanBtn');
    if (!btn) return;
    const original = btn.onclick;
    btn.onclick = async (event) => {
      if (mode() !== 'large') return original ? original.call(btn, event) : undefined;
      try { btn.disabled = true; btn.textContent = '正在启动…'; await start(); }
      catch (error) { if (window.toast) window.toast(error.message || '大数据包任务启动失败', true); }
      finally { btn.disabled = false; setModeText(); }
    };
    $('largePackageSearchBtn')?.addEventListener('click', () => { fileOffset = 0; fileTopic = ''; fileCategory = ''; loadFiles(); });
    $('largePackageSelectAll')?.addEventListener('click', () => { files.forEach((item) => selected.add(item.path)); loadFiles(); });
    $('largePackageClearSelection')?.addEventListener('click', () => { selected.clear(); loadFiles(); });
    $('largePackageConfirm')?.addEventListener('click', async () => { try { await confirmSelection(); } catch (error) { if (window.toast) window.toast(error.message || '提交选择失败', true); } });
    $('largePackageShowRemaining')?.addEventListener('click', () => { showAllFiles = false; fileOffset = 0; loadFiles(); });
    $('largePackageShowAll')?.addEventListener('click', () => { showAllFiles = true; fileOffset = 0; loadFiles(); });
    $('largePackageSelectCatalogAll')?.addEventListener('click', () => { fileNodeId = ''; fileTopic = ''; fileCategory = ''; fileOffset = 0; const reset = $('largePackageSelectCatalogAll'); if (reset) reset.hidden = true; loadFiles(); });
    $('largePackageSelectGroupAll')?.addEventListener('click', async () => { try { await selectAllFiltered(); } catch (error) { window.toast?.(error.message || '批量选择失败', true); } });
    $('largePackageSwitchBtn')?.addEventListener('click', () => { clearInterval(pollTimer); window.SJFXDataSources?.open('large') || window.SJFXShell?.activate('packages'); });
    $('largePackagePauseBtn')?.addEventListener('click', async () => { try { await control('pause'); } catch (error) { window.toast?.(error.message || '暂停失败', true); } });
    $('largePackageResumeBtn')?.addEventListener('click', async () => { try { await control('resume'); } catch (error) { window.toast?.(error.message || '继续失败', true); } });
    $('largePackageCancelBtn')?.addEventListener('click', async () => { try { await control('cancel'); } catch (error) { window.toast?.(error.message || '停止失败', true); } });
    $('largePackageOpenCatalog')?.addEventListener('click', () => window.SJFXLargeResult?.activate(packageId, 'catalog'));
    $('largePackageOpenOverview')?.addEventListener('click', () => window.SJFXLargeResult?.activate(packageId, 'overview'));
  });
  // 大数据包 → 导出中心。复用既有约定：
  //  1) 切屏用 window.SJFXShell.activate('exports')（product-shell.js 的 viewFor
  //     已经把 exports 映射到导出中心这一屏）；
  //  2) "要看哪个大包"用 window.sjfxLargeResultId，export-center.js 的
  //     loadSources() 优先读它来选来源，其次读 localStorage 的
  //     sjfx_large_package_id —— 这里把两个都登记上。
  // 不新造路由，也不重复实现导出中心的数据源加载。
  function openExportCenter() {
    if (!packageId) {
      window.toast?.('请先选择或创建大数据包，再进入导出中心。', true);
      return;
    }
    window.sjfxLargeResultId = packageId;
    try {
      window.localStorage.setItem('sjfx_large_package_id', packageId);
    } catch (_) {
      // 浏览器禁用存储时只影响"下次进入导出中心仍记得这个大包"。
    }
    // 顺序不能反：导出中心若已经加载过数据源，activate('exports') 不会再读一次，
    // 下拉框会停在上一个大包上。先让它按这个大包重读（并切回大数据包这一栏）。
    const center = window.SJFXExportCenter;
    if (center && typeof center.openForLargePackage === 'function') center.openForLargePackage(packageId);
    else if (center && typeof center.reload === 'function') center.reload(); // 旧缓存版本的兜底
    window.SJFXShell?.activate('exports');
  }

  function ensureExportCenterEntry() {
    const existing = $('largePackageToExportCenter');
    if (existing) return existing;
    const anchor = $('largePackageSwitchBtn');
    if (!anchor || !anchor.parentElement) return null;
    const button = document.createElement('button');
    button.id = 'largePackageToExportCenter';
    button.type = 'button';
    button.className = 'ghost';
    button.textContent = '导出中心';
    button.title = '在导出中心勾选这个大数据包的智能目录，交付压缩包或转入普通数据包';
    button.addEventListener('click', openExportCenter);
    anchor.insertAdjacentElement('afterend', button);
    return button;
  }

  window.SJFXLargePackage = { setStage, activate,
    workspace: () => workspaceStatus?.packageId === packageId ? workspaceStatus : null };
  document.addEventListener('click', event => {
    const node = event.target.closest('#largePackageSmartCatalog [data-large-node-type], #largePackageSelectCatalogTree [data-large-node-type]');
    if (node && event.target.closest('summary')) {
      fileNodeId = node.dataset.largeNodeType === 'group' ? node.dataset.largeNodeValue : '';
      fileTopic = node.dataset.largeNodeType === 'topic' ? node.dataset.largeNodeValue : '';
      fileCategory = node.dataset.largeNodeType === 'category' ? node.dataset.largeNodeValue : '';
      fileOffset = 0;
      const reset = $('largePackageSelectCatalogAll');
      if (reset) reset.hidden = !(fileNodeId || fileTopic || fileCategory);
      loadFiles();
    }
  });
})();
