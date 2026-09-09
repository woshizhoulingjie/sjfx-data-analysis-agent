(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>\"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
  const api = () => window.sjfxApi;
  let packageId = '';
  let pollTimer = null;
  let selected = new Set();
  let files = [];
  let lastCatalogAt = 0;
  let currentStage = 'import';
  let showAllFiles = false;
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
    if ($('largePackageOpenCatalog')) $('largePackageOpenCatalog').hidden = !['waiting_for_selection', 'completed'].includes(status.status);
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
    if ($('largePackageSmartCatalog')) $('largePackageSmartCatalog').innerHTML = smartRows.join('') || '<div class="large-empty">快速解析完成后生成智能目录。</div>';
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
    try {
      const nodes = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/nodes`);
      renderUserNodes(nodes.nodes || []);
    } catch (_) { /* custom nodes are an additive directory view */ }
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
    if (!packageId) return;
    try {
      const data = await api()(`/api/large-packages/${encodeURIComponent(packageId)}`);
      render(data.large_package);
      const catalogReady = ['waiting_for_selection', 'completed'].includes(data.large_package.status) || data.large_package.phase === 'catalog_ready';
      if (catalogReady && Date.now() - lastCatalogAt > 3000) await loadCatalog();
      if (data.large_package.status === 'completed') await loadReport();
      if (['completed','failed','cancelled'].includes(data.large_package.status)) {
        clearInterval(pollTimer); pollTimer = null;
      }
      if (selectableStatuses.has(String(data.large_package.status || '').toLowerCase()) && currentStage === 'select') await loadFiles();
    } catch (error) {
      if ($('largePackageStatus')) $('largePackageStatus').textContent = error.message || '状态读取失败';
    }
  }
  async function loadFiles() {
    const q = $('largePackageSearch')?.value || '';
    const data = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/files?limit=200&q=${encodeURIComponent(q)}`);
    const allFiles = data.files || [];
    files = showAllFiles ? allFiles : allFiles.filter((item) => String(item.deep_status || '').toLowerCase() !== 'completed');
    const box = $('largePackageFiles');
    if (!box) return;
    box.innerHTML = files.map((item) => {
      const deepStatus = String(item.deep_status || 'idle').toLowerCase();
      const statusText = deepStatus === 'completed' ? '已完成深度解析' : (deepStatus === 'failed' ? '深度解析失败，可重试' : (deepStatus === 'queued' || deepStatus === 'running' ? '正在处理中' : '待深度解析'));
      const summary = String(item.local_summary?.sample || item.local_summary?.notice || item.error || '').replace(/\s+/g, ' ').slice(0, 240);
      const locked = deepStatus === 'queued' || deepStatus === 'running';
      return `<label class="large-file-row ${deepStatus === 'completed' ? 'is-completed' : ''}"><input type="checkbox" data-large-path="${esc(item.path)}" ${selected.has(item.path) ? 'checked' : ''} ${locked ? 'disabled' : ''}><span><b title="${esc(item.path)}">${esc(item.path)}</b><small>${esc(item.category || '未分类')} · ${esc(item.size_human || '')} · ${esc(statusText)}</small>${summary ? `<em title="${esc(summary)}">${esc(summary)}</em>` : ''}</span></label>`;
    }).join('') || '<div class="large-empty">当前没有待补充文件；点击“查看全部文件”可重新处理已完成文件。</div>';
    box.querySelectorAll('input[data-large-path]').forEach((input) => {
      input.addEventListener('change', () => input.checked ? selected.add(input.dataset.largePath) : selected.delete(input.dataset.largePath));
    });
    if ($('largePackageSelectionStats')) $('largePackageSelectionStats').textContent = `共 ${allFiles.length} 个文件 · 当前显示 ${files.length} 个${showAllFiles ? '全部文件' : '待补充文件'} · 已选择 ${selected.size} 个`;
  }
  async function start() {
    const result = await api()('/api/large-packages', {method:'POST', body: JSON.stringify({path: $('rootPath')?.value || ''})});
    packageId = result.package_id;
    selected = new Set();
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
    await refresh();
  }
  window.openLargePackageHistory = async function (id, requestedStage = '') {
    requestedStage = typeof requestedStage === 'string' ? requestedStage : '';
    packageId = String(id || '');
    selected = new Set();
    lastCatalogAt = 0;
    showAllFiles = false;
    showPanel(true);
    if (requestedStage) setStage(requestedStage);
    await refresh();
    clearInterval(pollTimer);
    const current = await api()(`/api/large-packages/${encodeURIComponent(packageId)}`);
    const route = requestedStage || (current.large_package?.status === 'completed' ? 'large-overview' : 'large-select');
    if (window.SJFXShell) window.SJFXShell.activate(route); else setStage(route === 'large-overview' ? 'overview' : 'select');
    if (!['completed', 'failed', 'cancelled'].includes(current.large_package?.status)) pollTimer = setInterval(refresh, 1500);
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
    $('largePackageSearchBtn')?.addEventListener('click', loadFiles);
    $('largePackageSelectAll')?.addEventListener('click', () => { files.forEach((item) => selected.add(item.path)); loadFiles(); });
    $('largePackageClearSelection')?.addEventListener('click', () => { selected.clear(); loadFiles(); });
    $('largePackageConfirm')?.addEventListener('click', async () => { try { await confirmSelection(); } catch (error) { if (window.toast) window.toast(error.message || '提交选择失败', true); } });
    $('largePackageShowRemaining')?.addEventListener('click', () => { showAllFiles = false; loadFiles(); });
    $('largePackageShowAll')?.addEventListener('click', () => { showAllFiles = true; loadFiles(); });
    $('largePackageSwitchBtn')?.addEventListener('click', () => { clearInterval(pollTimer); window.SJFXDataSources?.open('large') || window.SJFXShell?.activate('packages'); });
    $('largePackagePauseBtn')?.addEventListener('click', async () => { try { await control('pause'); } catch (error) { window.toast?.(error.message || '暂停失败', true); } });
    $('largePackageResumeBtn')?.addEventListener('click', async () => { try { await control('resume'); } catch (error) { window.toast?.(error.message || '继续失败', true); } });
    $('largePackageCancelBtn')?.addEventListener('click', async () => { try { await control('cancel'); } catch (error) { window.toast?.(error.message || '停止失败', true); } });
    $('largePackageOpenCatalog')?.addEventListener('click', () => window.SJFXShell?.activate('large-catalog'));
    $('largePackageOpenOverview')?.addEventListener('click', () => window.SJFXShell?.activate('large-overview'));
  });
  window.SJFXLargePackage = { setStage };
})();
