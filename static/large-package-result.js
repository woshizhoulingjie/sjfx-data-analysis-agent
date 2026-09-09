(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = () => window.sjfxApi;
  let packageId = '';
  let files = [];
  let currentStage = 'overview';
  let latestStatus = {};

  function setText(id, value) { const el = $(id); if (el) el.textContent = value == null ? '' : value; }
  function setStage(stage) {
    currentStage = stage || 'overview';
    const titles = {
      catalog: ['大数据包目录', '原始目录、智能目录和节点摘要按范围查看。'],
      overview: ['大数据包情报概览', '深度摘要完成后生成研究简报、推荐方向和下载报告。'],
    };
    const heading = titles[currentStage] || titles.overview;
    setText('largeResultPageTitle', heading[0]);
    setText('largeResultPageSubtitle', heading[1]);
    document.querySelectorAll('[data-large-section]').forEach((el) => {
      const section = el.dataset.largeSection;
      const visible = currentStage === 'catalog' ? ['metrics', 'catalog', 'files'].includes(section) : ['metrics', 'overview', 'research'].includes(section);
      el.hidden = !visible;
    });
    const select = $('largeResultToSelect');
    if (select) select.hidden = !window.sjfxLargeResultId;
    const catalog = $('largeResultToCatalog');
    if (catalog) catalog.disabled = currentStage === 'catalog';
    const overview = $('largeResultToOverview');
    if (overview) overview.disabled = currentStage === 'overview';
  }
  function renderCatalog(catalog) {
    if (!catalog) return;
    setText('largeResultRootPath', catalog.root_path || '');
    setText('largeResultDirectoryStats', `${catalog.files_total || 0} 个文件 · ${(catalog.original?.directories || []).length} 个目录`);
    const dirs = (catalog.original?.directories || []).map((item) => `<details class="large-result-node" data-large-node-type="directory" data-large-node-value="${esc(item.path || '')}"><summary><b>${esc(item.path || '根目录')}</b><span>${item.files || 0} 个文件</span></summary><small>${(item.sample || []).map(esc).join(' · ')}</small></details>`);
    if (catalog.original?.root_files?.length) dirs.unshift(`<div class="large-result-root"><b>根目录文件</b><small>${catalog.original.root_files.map(esc).join(' · ')}</small></div>`);
    $('largeResultOriginalCatalog').innerHTML = dirs.join('') || '<div class="large-empty">暂无原始目录记录。</div>';
    const categories = catalog.smart?.categories || [];
    setText('largeResultSmartStats', `${categories.length} 个分类`);
    $('largeResultSmartCatalog').innerHTML = categories.map((item) => `<details class="large-result-node" data-large-node-type="category" data-large-node-value="${esc(item.category || '')}"><summary><b>${esc(item.category || '未分类')}</b><span>${item.files || 0} 个文件 · ${Math.round(Number(item.confidence || 0) * 100)}%</span></summary><small>${(item.sample || []).map((file) => esc(file.path)).join(' · ')}</small></details>`).join('') || '<div class="large-empty">暂无智能分类。</div>';
  }
  function renderFiles(rows) {
    files = rows || [];
    const box = $('largeResultFiles');
    if (!box) return;
    box.innerHTML = files.map((item) => {
      const deep = item.deep_summary || {};
      const local = item.local_summary || {};
      const type = deep.summary_type === 'model' ? '模型摘要' : (deep.summary_type ? '深度本地摘要' : '本地摘要');
      const summary = deep.summary || local.sample || local.notice || item.error || '暂无摘要';
      return `<article class="large-result-file"><div><b>${esc(item.path)}</b><small>${esc(item.category || '未分类')} · 置信度 ${Math.round(Number(item.confidence || 0) * 100)}% · ${esc(type)}</small><p>${esc(summary)}</p></div><span class="large-result-file-status ${item.deep_status === 'completed' ? 'is-deep' : ''}">${item.deep_status === 'completed' ? '已深度解析' : '已盘点'}</span></article>`;
    }).join('') || '<div class="large-empty">暂无匹配文件。</div>';
    setText('largeResultFileStats', `当前显示 ${files.length} 个文件`);
  }
  async function loadFiles() {
    const query = $('largeResultSearch')?.value || '';
    const category = $('largeResultCategory')?.value || '';
    const data = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/files?limit=200&q=${encodeURIComponent(query)}&category=${encodeURIComponent(category)}`);
    renderFiles(data.files || []);
  }

  function renderResearchBrief(report) {
    const brief = report.research_brief || {};
    const direction = brief.recommended_research_direction || {};
    const list = (values, empty) => {
      const rows = Array.isArray(values) ? values.filter(Boolean) : [];
      return rows.length ? `<ul>${rows.map((value) => `<li>${esc(value)}</li>`).join('')}</ul>` : `<p>${esc(empty || '暂无')}</p>`;
    };
    const basic = $('largeResultBasicInfo');
    if (basic) basic.innerHTML = list(brief.basic_information, '暂无总体信息');
    const findings = $('largeResultFindings');
    if (findings) findings.innerHTML = `<h4>关键发现</h4>${list(brief.key_findings, '当前没有形成稳定发现')}`;
    const directionHost = $('largeResultDirection');
    if (directionHost) directionHost.innerHTML = direction.title
      ? `<h4>${esc(direction.title)}</h4><p>${esc(direction.rationale || '该方向仍需结合代表性资料继续验证。')}</p>${list(direction.research_questions, '暂无建议问题')}`
      : `<p>${esc(brief.empty_direction_reason || '当前尚未形成首选研究方向。')}</p>`;
    const details = $('largeResultResearchDetails');
    if (details) {
      const methods = list(direction.methods, '暂无建议方法');
      const files = list(direction.representative_documents, '暂无代表文件');
      details.innerHTML = `<section><h3>建议研究方法</h3>${methods}</section><section><h3>代表文件</h3>${files}</section>`;
    }
    const limitations = $('largeResultLimitations');
    if (limitations) limitations.innerHTML = list(brief.limitations, '当前未记录额外限制');
    const ready = Boolean(brief.ready || brief.available);
    ['largeResultDownloadDocx', 'largeResultDownloadJson'].forEach((id) => {
      const button = $(id);
      if (button) button.disabled = !ready;
    });
    setText('largeResultDownloadStatus', ready ? '研究简报已生成，可下载概览文件。' : (brief.empty_direction_reason || '深度摘要完成后生成研究简报。'));
  }

  function renderControls(item) {
    latestStatus = item || {};
    const status = String(item.status || '').toLowerCase();
    const active = status === 'queued' || status === 'running';
    const paused = status === 'paused';
    ['largeResultPauseBtn', 'largePackagePauseBtn'].forEach((id) => { const b = $(id); if (b) b.hidden = !active; });
    ['largeResultResumeBtn', 'largePackageResumeBtn'].forEach((id) => { const b = $(id); if (b) b.hidden = !paused; });
    ['largeResultCancelBtn', 'largePackageCancelBtn'].forEach((id) => { const b = $(id); if (b) b.hidden = !active; });
  }

  async function control(action) {
    if (!packageId) return;
    if (action === 'cancel' && !window.confirm('停止后会保留已完成结果，但未完成文件不会继续处理。确定停止这个数据包吗？')) return;
    const endpoint = action === 'pause' ? 'pause' : action === 'resume' ? 'resume' : 'cancel';
    const response = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/${endpoint}`, { method: 'POST' });
    const item = response.large_package || {};
    renderControls(item);
    setText('largeResultStatus', `${item.message || '状态已更新'} · ${item.phase || '—'} · ${item.progress || 0}%`);
    if (endpoint === 'cancel') setText('largeResultDownloadStatus', '任务已停止，已完成结果仍然保留。');
  }

  async function downloadReport(format) {
    if (!packageId) return;
    try {
      const response = await window.SJFXAuth.request(
        `/api/large-packages/${encodeURIComponent(packageId)}/report/download?format=${encodeURIComponent(format)}`,
        { headers: { Accept: '*/*' } },
      );
      if (!response.ok) throw new Error('概览下载失败');
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `large-package-overview.${format}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      setText('largeResultDownloadStatus', `概览 ${format.toUpperCase()} 已开始下载。`);
    } catch (error) {
      setText('largeResultDownloadStatus', error.message || '概览下载失败');
    }
  }

  async function load() {
    if (!packageId) return;
    const status = await api()(`/api/large-packages/${encodeURIComponent(packageId)}`);
    const item = status.large_package || {};
    renderControls(item);
    setText('largeResultStatus', `${item.message || '大数据包成果'} · ${item.phase || '—'} · ${item.progress || 0}%`);
    setText('largeResultPackageId', packageId);
    const reportPayload = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/report`);
    const report = reportPayload.report || {};
    renderResearchBrief(report);
    const coverage = report.coverage || {};
    setText('largeResultInventory', coverage.inventory_files || 0);
    setText('largeResultQuick', coverage.quick_completed || 0);
    setText('largeResultDeep', coverage.deep_completed || 0);
    setText('largeResultModel', coverage.model_summaries || 0);
    setText('largeResultFailure', (report.failures || []).length);
    setText('largeResultOverviewStatus', item.status === 'completed' ? 'REPORT READY' : (item.phase || 'PROCESSING'));
    const remaining = Math.max(0, Number(coverage.quick_completed || 0) - Number(coverage.deep_completed || 0));
    setText('largeResultOverviewSummary', `本数据包共盘点 ${coverage.inventory_files || 0} 个文件，已完成 ${coverage.quick_completed || 0} 个本地快速摘要；已深度解析 ${coverage.deep_completed || 0} 个，其中 ${coverage.model_summaries || 0} 个生成了模型摘要。${remaining ? `还有 ${remaining} 个文件可随时补充深度解析。` : '当前没有待补充文件。'}`);
    const categories = report.categories || {};
    const categoryEntries = Object.entries(categories).sort((a, b) => Number(b[1]) - Number(a[1]));
    const categoryTotal = Math.max(1, Number(coverage.inventory_files || 0));
    if ($('largeResultCategorySummary')) $('largeResultCategorySummary').innerHTML = categoryEntries.map(([category, count]) => `<div class="large-result-category-row"><span>${esc(category)}</span><i><b style="width:${Math.min(100, Number(count) / categoryTotal * 100)}%"></b></i><em>${count}</em></div>`).join('') || '<div class="large-empty">暂无分类统计。</div>';
    const failures = report.failures || [];
    if ($('largeResultWarnings')) $('largeResultWarnings').innerHTML = failures.length ? `<p>有 ${failures.length} 个文件需要复核或重新处理。</p><ul>${failures.slice(0, 8).map((failure) => `<li>${esc(failure.path || '')}${failure.error ? `：${esc(failure.error)}` : ''}</li>`).join('')}</ul>` : '<p>当前没有快速解析失败项。</p><p>未进入深度解析的文件仍然只保留本地摘要。</p>';
    const retry = $('largeResultRetryFailed');
    if (retry) retry.hidden = !failures.length || !['completed', 'waiting_for_selection'].includes(item.status);
    const select = $('largeResultCategory');
    if (select) {
      select.innerHTML = '<option value="">全部分类</option>';
      Object.keys(categories).forEach((category) => select.add(new Option(`${category} (${categories[category]})`, category)));
    }
    renderCatalog((await api()(`/api/large-packages/${encodeURIComponent(packageId)}/catalog`)).catalog);
    await loadFiles();
  }

  async function retryFailed() {
    if (!packageId) return;
    const response = await api()(`/api/large-packages/${encodeURIComponent(packageId)}/retry-failed`, { method: 'POST' });
    setText('largeResultDownloadStatus', response.retry_files ? `已重新排队 ${response.retry_files} 个失败文件，正在处理。` : '当前没有可重试的失败文件。');
    if (response.retry_files) {
      const timer = window.setInterval(async () => {
        try {
          const status = await api()(`/api/large-packages/${encodeURIComponent(packageId)}`);
          await load();
          if (['waiting_for_selection', 'completed', 'failed', 'cancelled'].includes(status.large_package?.status)) window.clearInterval(timer);
        } catch (_) { window.clearInterval(timer); }
      }, 2000);
    }
  }
  async function activate(id, stage = currentStage) {
    packageId = String(id || packageId || '');
    if (!packageId) {
      try {
        const history = await api()('/api/large-packages/history?limit=1');
        const latest = (history.items || [])[0];
        packageId = String(latest?.large_package_id || latest?.scan_id || '');
      } catch (error) {
        setText('largeResultStatus', error.message || '大数据包历史读取失败');
      }
    }
    if (!packageId) {
      setText('largeResultStatus', '暂无大数据包成果，请先创建一个大数据包任务');
      if (window.SJFXShell && !['large-catalog', 'large-overview', 'large-result'].includes(document.body.dataset.route)) window.SJFXShell.activate('large-overview');
      return;
    }
    window.sjfxLargeResultId = packageId;
    setStage(stage);
    if (window.SJFXShell && !['large-catalog', 'large-overview', 'large-result'].includes(document.body.dataset.route)) window.SJFXShell.activate('large-overview');
    else document.querySelectorAll('.app-view').forEach((el) => el.classList.toggle('active', el.dataset.view === 'large-result'));
    try { await load(); } catch (error) { setText('largeResultStatus', error.message || '大数据包成果读取失败'); }
  }
  window.SJFXLargeResult = { activate };
  document.addEventListener('DOMContentLoaded', () => {
    $('largeResultRefreshBtn')?.addEventListener('click', () => activate(packageId));
    $('largeResultSearchBtn')?.addEventListener('click', loadFiles);
    $('largeResultSearch')?.addEventListener('keydown', (event) => { if (event.key === 'Enter') loadFiles(); });
    $('largeResultCategory')?.addEventListener('change', loadFiles);
    document.querySelector('.large-result-view')?.addEventListener('click', (event) => {
      const node = event.target.closest('[data-large-node-type]');
      if (!node || !event.target.closest('summary')) return;
      const type = node.dataset.largeNodeType || '';
      const value = node.dataset.largeNodeValue || '';
      if ($('largeResultSearch')) $('largeResultSearch').value = type === 'directory' ? value : '';
      if ($('largeResultCategory')) $('largeResultCategory').value = type === 'category' ? value : '';
      setText('largeResultNodeSummary', `${type === 'category' ? '智能目录分类' : '原始目录'}：${value || '根目录'} · 正在读取该节点的文件摘要`);
      loadFiles().then(() => setText('largeResultNodeSummary', `${type === 'category' ? '智能目录分类' : '原始目录'}：${value || '根目录'} · 已显示该节点的文件摘要`)).catch((error) => setText('largeResultNodeSummary', error.message || '节点摘要读取失败'));
    });
    $('largeResultToImport')?.addEventListener('click', () => window.SJFXShell?.activate('large-import'));
    $('largeResultToSelect')?.addEventListener('click', () => {
      if (window.openLargePackageHistory && packageId) window.openLargePackageHistory(packageId, 'select');
      else window.SJFXShell?.activate('large-select');
    });
    $('largeResultSwitchPackage')?.addEventListener('click', () => window.SJFXDataSources?.open('large') || window.SJFXShell?.activate('packages'));
    $('largeResultPauseBtn')?.addEventListener('click', async () => { try { await control('pause'); } catch (error) { setText('largeResultStatus', error.message || '暂停失败'); } });
    $('largeResultResumeBtn')?.addEventListener('click', async () => { try { await control('resume'); } catch (error) { setText('largeResultStatus', error.message || '继续失败'); } });
    $('largeResultCancelBtn')?.addEventListener('click', async () => { try { await control('cancel'); } catch (error) { setText('largeResultStatus', error.message || '停止失败'); } });
    $('largeResultToCatalog')?.addEventListener('click', () => window.SJFXShell?.activate('large-catalog'));
    $('largeResultToOverview')?.addEventListener('click', () => window.SJFXShell?.activate('large-overview'));
    $('largeResultBackBtn')?.addEventListener('click', () => window.SJFXShell?.activate('large-select'));
    $('largeResultDownloadDocx')?.addEventListener('click', () => downloadReport('docx'));
    $('largeResultDownloadJson')?.addEventListener('click', () => downloadReport('json'));
    $('largeResultRetryFailed')?.addEventListener('click', async () => {
      const button = $('largeResultRetryFailed');
      try { if (button) button.disabled = true; await retryFailed(); }
      catch (error) { setText('largeResultDownloadStatus', error.message || '失败文件重试失败'); }
      finally { if (button) button.disabled = false; }
    });
  });
})();
