(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>\"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
  const api = () => window.sjfxApi;
  let packageId = '';
  let pollTimer = null;
  let selected = new Set();
  let files = [];
  let lastCatalogAt = 0;
  let directoryPending = 0;
  let currentStage = 'import';
  let showAllFiles = false;
  let fileOffset = 0;
  let fileTopic = '';
  let fileNodeId = '';
  let fileCategory = '';
  let refreshing = false;
  const selectableStatuses = new Set(['waiting_for_selection', 'completed']);
  const activeStatuses = new Set(['queued', 'running']);

  function mode() {
    return document.querySelector('input[name="workflowMode"]:checked')?.value || 'standard';
  }
  function setModeText() {
    const large = mode() === 'large';
    const btn = $('scanBtn');
    if (btn) btn.textContent = large ? '启动大数据包流式处理' : '仅导入目录';
    const note = $('workflowModeNote');
    if (note) note.textContent = large
      ? '大包模式使用独立队列和 large_package.db；先盘点和本地摘要，再选择文件做深度解析。'
      : '普通模式保持现有导入分析流程。';
  }
  function showPanel(show) {
    const panel = $('largePackagePanel');
    if (panel) panel.hidden = !show;
  }
  function setStage(stage) {
    currentStage = stage || 'import';
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
  function render(status) {
    if (!status) return;
    const counts = status.counts || {};
    const quick = counts.quick || {};
    const deep = counts.deep || {};
    const text = `${status.message || ''} · 阶段：${status.phase || '—'} · 进度 ${status.progress || 0}%` +
      ` · 文件 ${counts.total_files || 0} · 快速摘要 ${quick.completed || 0} · 深度 ${deep.completed || 0}`;
    if ($('largePackageStatus')) $('largePackageStatus').textContent = text;
    if ($('largePackageProgress')) $('largePackageProgress').style.width = `${Math.max(0, Math.min(100, Number(status.progress || 0)))}%`;
    const ready = selectableStatuses.has(String(status.status || '').toLowerCase());
    if ($('largePackagePanel')) $('largePackagePanel').dataset.ready = String(ready);
    if ($('largePackageSelectionBox')) $('largePackageSelectionBox').hidden = !ready;
    if ($('largePackageSelectionBox')) $('largePackageSelectionBox').hidden = currentStage !== 'select' || !ready;
    if ($('largePackageOpenCatalog')) $('largePackageOpenCatalog').hidden = !Number(counts.total_files || 0);
    if ($('largePackageOpenOverview')) $('largePackageOpenOverview').hidden = status.status !== 'completed';
    if ($('largePackagePauseBtn')) $('largePackagePauseBtn').hidden = !activeStatuses.has(String(status.status || '').toLowerCase());
    if ($('largePackageResumeBtn')) $('largePackageResumeBtn').hidden = status.status !== 'paused';
    if ($('largePackageCancelBtn')) $('largePackageCancelBtn').hidden = !activeStatuses.has(String(status.status || '').toLowerCase());
    if ($('largePackageReport') && status.status !== 'completed') $('largePackageReport').textContent = '';
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
    const topicCount = smart.topic_total || (smart.topics || []).length;
    const hint = $('largePackageSelectCatalogHint');
    if (hint) {
      hint.textContent = topicCount
        ? `已生成 ${topicCount} 个内容主题；点击主题或分类，下方只显示该组文件`
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
    if (!packageId || refreshing) return;
    refreshing = true;
    try {
      const data = await api()(`/api/large-packages/${encodeURIComponent(packageId)}`);
      render(data.large_package);
      const catalogReady = Number(data.large_package.counts?.total_files || 0) > 0;
      if (catalogReady && Date.now() - lastCatalogAt > 5000) await loadCatalog();
      if (data.large_package.status === 'completed') await loadReport();
      if (['failed','cancelled'].includes(data.large_package.status) || (data.large_package.status === 'completed' && !directoryPending)) {
        clearInterval(pollTimer); pollTimer = null;
      }
      if (selectableStatuses.has(String(data.large_package.status || '').toLowerCase()) && currentStage === 'select') await loadFiles();
    } catch (error) {
      if ($('largePackageStatus')) $('largePackageStatus').textContent = error.message || '状态读取失败';
    } finally { refreshing = false; }
  }
  async function loadFiles() {
    const q = $('largePackageSearch')?.value || '';
    const data = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/files?limit=100&offset=${fileOffset}&remaining=${showAllFiles ? 0 : 1}&node_id=${encodeURIComponent(fileNodeId)}&topic=${encodeURIComponent(fileTopic)}&category=${encodeURIComponent(fileCategory)}&q=${encodeURIComponent(q)}`);
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
    window.sjfxLargeResultId = packageId;
    selected = new Set();
    fileOffset = 0; fileTopic = ''; fileNodeId = ''; fileCategory = '';
    lastCatalogAt = 0;
    showPanel(true);
    if (window.SJFXShell) window.SJFXShell.activate('large-select');
    else setStage('select');
    await refresh();
    clearInterval(pollTimer);
    pollTimer = setInterval(refresh, 1500);
  }
  async function confirmSelection() {
    if (!selected.size) return window.alert('请至少选择一个文件');
    await api()(`/api/large-packages/${encodeURIComponent(packageId)}/select`, {method:'POST', body: JSON.stringify({paths: Array.from(selected), rule:{kind:'manual_ui'}})});
    selected.clear();
    if ($('largePackageSelectionStats')) $('largePackageSelectionStats').textContent = '已提交深度解析，后台继续处理。';
    clearInterval(pollTimer); pollTimer = setInterval(refresh, 1500);
    await refresh();
  }
  window.openLargePackageHistory = async function (id, requestedStage = '') {
    requestedStage = typeof requestedStage === 'string' ? requestedStage : '';
    packageId = String(id || '');
    window.sjfxLargeResultId = packageId;
    selected = new Set();
    fileOffset = 0; fileTopic = ''; fileCategory = '';
    lastCatalogAt = 0;
    showAllFiles = false;
    showPanel(true);
    if (requestedStage) setStage(requestedStage);
    await refresh();
    clearInterval(pollTimer);
    const current = await api()(`/api/large-packages/${encodeURIComponent(packageId)}`);
    const route = requestedStage || (current.large_package?.status === 'completed' ? 'large-overview' : 'large-select');
    if (window.SJFXShell) window.SJFXShell.activate(route); else setStage(route === 'large-overview' ? 'overview' : 'select');
    if (!['completed', 'failed', 'cancelled'].includes(current.large_package?.status) || (current.large_package?.status === 'completed' && directoryPending > 0)) pollTimer = setInterval(refresh, 1500);
  };
  async function control(action) {
    if (!packageId) return;
    const endpoint = action === 'pause' ? 'pause' : action === 'resume' ? 'resume' : 'cancel';
    if (action === 'cancel' && !window.confirm('停止后会保留已完成结果，但未完成文件不会继续处理。确定停止这个数据包吗？')) return;
    const response = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/${endpoint}`, { method: 'POST' });
    render(response.large_package || {});
    if (endpoint === 'cancel') clearInterval(pollTimer);
  }
  document.addEventListener('DOMContentLoaded', () => {
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
  window.SJFXLargePackage = { setStage };
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
