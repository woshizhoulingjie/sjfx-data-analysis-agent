/* The original-directory supplement UI owns its own selection and pagination. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const tools = document.querySelector('.explore-tools');
  const surface = document.querySelector('.tree-surface');
  if (!tools || !surface || !$('tree')) return;
  const button = document.createElement('button');
  button.id = 'directorySupplementBtn';
  button.type = 'button';
  button.className = 'secondary';
  button.textContent = '补选解析';
  button.disabled = true;
  button.hidden = true;
  tools.prepend(button);
  const notice = document.createElement('div');
  notice.id = 'directorySupplementStatus';
  notice.className = 'directory-supplement-status';
  notice.setAttribute('role', 'status');
  notice.hidden = true;
  surface.insertBefore(notice, $('tree'));
  const panel = document.createElement('section');
  panel.id = 'directorySupplementPanel';
  panel.className = 'directory-supplement';
  panel.hidden = true;
  panel.setAttribute('aria-label', '补选解析范围');
  panel.innerHTML = '<div class="supplement-heading"><div><h3>补选解析范围</h3><p>勾选目录会包含全部子目录和文件，已选内容会自动跳过。</p></div><button type="button" class="ghost" data-supplement-close>收起</button></div><div class="supplement-actions"><button type="button" class="secondary" data-supplement-all>选择全部待解析文件</button><button type="button" class="ghost" data-supplement-clear>清空本次选择</button></div><div id="directorySupplementTree" class="supplement-tree" aria-label="当前数据包目录"></div><div class="supplement-footer"><div id="directorySupplementPreview" aria-live="polite">请选择此前未选的目录或文件。</div><button type="button" class="primary" id="directorySupplementSubmit" disabled>确认补选并解析</button></div>';
  surface.insertBefore(panel, $('tree'));
  let scanId = '', generation = 0, editing = false, submitting = false;
  let latest = null, preview = null, previewSeq = 0, previewTimer = null, pollTimer = null;
  let requestId = '', lastResultStamp = '', pollRunning = false;
  const chosen = new Set();
  const controllers = new Set();
  const currentScan = () => String(window.localStorage.getItem('sjfx_current_scan_id_v1') || '');
  const physical = () => window.SJFXShell?.route === 'physical';
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const current = (id, epoch) => scanId === id && generation === epoch && currentScan() === id;
  const endpoint = (id) => `/api/scan/${encodeURIComponent(id)}/directory-supplement`;
  const covered = (path) => [...chosen].some((parent) => parent === '.' || path === parent || path.startsWith(parent + '/'));
  const phases = {parsing_selected: '正在解析新增文件', parsed_overview: '正在更新解析版目录',
    preliminary_summarizing: '正在生成文件初步摘要', preliminary_nodes: '正在更新节点摘要',
    preliminary_overview: '正在更新初步概览', deep_summarizing_files: '正在生成深度摘要',
    deep_summarizing_nodes: '正在更新节点深度摘要', deep_update_available: '摘要已就绪，等待自动更新结果',
    deep_overview_updating: '正在自动更新智能目录和概览', paused: '已暂停，可在任务中心继续',
    partial: '部分完成，请在任务中心查看待处理项', failed: '本次处理失败，请在任务中心重试', cancelled: '本次处理已停止'};

  async function call(url, options = {}) {
    const controller = new AbortController();
    controllers.add(controller);
    try { return await api(url, {...options, signal: controller.signal}); }
    finally { controllers.delete(controller); }
  }
  function close() {
    editing = false; panel.hidden = true;
    $('tree').hidden = false;
    chosen.clear(); preview = null; previewSeq++; clearTimeout(previewTimer);
  }
  function renderStatus(data) {
    latest = data;
    button.hidden = !physical();
    button.disabled = !data.ready || submitting;
    button.title = data.reason || '';
    const batch = data.batch;
    notice.hidden = !physical();
    if (batch?.published) notice.textContent = `本次补选 ${batch.new_count} 个文件已完成，智能目录、文件摘要和概览已更新。`;
    else if (batch) notice.textContent = `本次补选 ${batch.new_count} 个文件 · ${phases[batch.phase] || batch.message || '处理中'}${batch.job_status === 'failed' || batch.report_status === 'failed' ? '；有失败任务，请在任务中心重试。' : ''}`;
    else notice.textContent = data.ready ? '可继续补选尚未解析的目录和文件，已有成果会保留。' : (data.reason || '请等待当前批次完成。');
    if (editing && !data.ready && !submitting) {
      $('directorySupplementSubmit').disabled = true;
      $('directorySupplementPreview').textContent = '当前范围正在处理，请等待完成后重新打开补选。';
    }
  }
  function updateChecks() {
    panel.querySelectorAll('[data-supplement-path]').forEach((checkbox) => {
      const path = checkbox.dataset.supplementPath;
      checkbox.checked = (checkbox.dataset.eligible === 'true' && covered(path)) || checkbox.dataset.complete === 'true';
      const label = checkbox.closest('.supplement-row').querySelector('small');
      if (label?.dataset.pendingLabel) label.textContent = covered(path) ? '本次补选' : label.dataset.pendingLabel;
      checkbox.indeterminate = !covered(path) && [...chosen].some((item) => item.startsWith(path + '/'));
      const ancestor = covered(path) && !chosen.has(path);
      checkbox.disabled = submitting || checkbox.dataset.eligible !== 'true' || ancestor;
    });
  }
  async function loadChildren(host, parent = '.', offset = 0) {
    const id = scanId, epoch = generation;
    const data = await call(`${endpoint(id)}?parent=${encodeURIComponent(parent)}&offset=${offset}`);
    if (!current(id, epoch) || !editing || !host.isConnected) return;
    if (latest && latest.version !== data.version) {
      chosen.clear(); preview = null; previewSeq++;
      $('directorySupplementSubmit').disabled = true;
      $('directorySupplementPreview').textContent = '分析范围已变化，请重新选择。';
    }
    renderStatus(data);
    if (!offset) host.replaceChildren();
    for (const item of data.items || []) {
      const row = document.createElement('div');
      row.className = 'supplement-node';
      const complete = item.selected_count === item.file_count && item.file_count > 0;
      const label = item.kind === 'directory'
        ? `${item.file_count} 个文件 · 已选 ${item.selected_count} · 可补选 ${item.remaining_count}`
        : item.selected_count ? (item.completed_count ? '已选 · 已解析' : '已选') : item.remaining_count ? '尚未选择' : '当前不可解析';
      row.innerHTML = `<div class="supplement-row"><input type="checkbox" aria-label="选择 ${esc(item.path)}" data-supplement-path="${esc(item.path)}" data-eligible="${item.remaining_count > 0}" data-complete="${complete}">${item.kind === 'directory' ? `<button type="button" class="supplement-expand" aria-expanded="false" aria-label="展开 ${esc(item.path)}">▸</button>` : '<span class="supplement-file-icon" aria-hidden="true">·</span>'}<span class="supplement-name" title="${esc(item.path)}">${esc(item.name)}</span><small${item.kind === 'file' && item.remaining_count ? ` data-pending-label="${esc(label)}"` : ''}>${esc(label)}</small></div>`;
      if (item.kind === 'directory') {
        const children = document.createElement('div');
        children.className = 'supplement-children'; children.hidden = true;
        row.append(children);
        row.querySelector('.supplement-expand').addEventListener('click', async (event) => {
          const toggle = event.currentTarget;
          const opening = children.hidden;
          children.hidden = !opening; toggle.setAttribute('aria-expanded', String(opening));
          toggle.textContent = opening ? '▾' : '▸';
          if (opening && !children.dataset.loaded) {
            toggle.disabled = true; children.textContent = '正在读取目录…';
            try { await loadChildren(children, item.path); if (current(id, epoch)) children.dataset.loaded = 'true'; }
            catch (error) { if (current(id, epoch)) children.textContent = error.message || '读取失败，请重新展开。'; }
            finally { toggle.disabled = false; }
          }
        });
      }
      host.append(row);
    }
    if (data.next_offset != null) {
      const more = document.createElement('button');
      more.type = 'button'; more.className = 'ghost supplement-more'; more.textContent = '显示更多';
      more.addEventListener('click', async () => {
        more.disabled = true;
        try { await loadChildren(host, parent, data.next_offset); more.remove(); }
        catch (error) { more.disabled = false; if (current(id, epoch)) toast(error.message, true); }
      });
      host.append(more);
    }
    if (!host.children.length) host.textContent = '此目录没有文件。';
    updateChecks();
  }
  function selectionChanged() {
    preview = null; requestId = ''; previewSeq++;
    clearTimeout(previewTimer);
    $('directorySupplementSubmit').disabled = true;
    updateChecks();
    if (!chosen.size) { $('directorySupplementPreview').textContent = '请选择此前未选的目录或文件。'; return; }
    $('directorySupplementPreview').textContent = '正在统计整个所选范围…';
    const id = scanId, epoch = generation, sequence = previewSeq;
    previewTimer = setTimeout(async () => {
      try {
        const data = await call(endpoint(id) + '/preview', {method: 'POST', body: JSON.stringify({paths: [...chosen]})});
        if (!current(id, epoch) || sequence !== previewSeq || !editing) return;
        preview = data;
        $('directorySupplementPreview').textContent = `本次新增 ${data.new_count} 个文件 · 跳过已选 ${data.already_selected_count} 个${data.skipped_count ? ` · 暂不可解析 ${data.skipped_count} 个` : ''}`;
        $('directorySupplementSubmit').disabled = !data.new_count || submitting;
      } catch (error) { if (current(id, epoch) && sequence === previewSeq) $('directorySupplementPreview').textContent = error.message || '统计失败，请重新选择。'; }
    }, 250);
  }
  button.addEventListener('click', async () => {
    if (!latest?.ready || !scanId) return;
    editing = true; chosen.clear(); preview = null; requestId = ''; previewSeq++;
    panel.hidden = false; $('tree').hidden = true;
    $('directorySupplementTree').textContent = '正在读取完整导入目录…';
    selectionChanged();
    const id = scanId, epoch = generation;
    try { await loadChildren($('directorySupplementTree')); }
    catch (error) { if (current(id, epoch)) $('directorySupplementTree').textContent = error.message; }
  });
  panel.querySelector('[data-supplement-close]').addEventListener('click', close);
  panel.querySelector('[data-supplement-clear]').addEventListener('click', () => { chosen.clear(); selectionChanged(); });
  panel.querySelector('[data-supplement-all]').addEventListener('click', () => { chosen.clear(); chosen.add('.'); selectionChanged(); });
  panel.addEventListener('change', (event) => {
    const checkbox = event.target.closest('[data-supplement-path]');
    if (!checkbox || submitting) return;
    const path = checkbox.dataset.supplementPath;
    if (checkbox.checked) {
      for (const value of chosen) if (value.startsWith(path + '/')) chosen.delete(value);
      chosen.add(path);
    } else chosen.delete(path);
    selectionChanged();
  });
  $('directorySupplementSubmit').addEventListener('click', async () => {
    if (!preview?.new_count || submitting || !latest?.ready) return;
    const id = scanId, epoch = generation;
    requestId ||= crypto.randomUUID().replaceAll('-', '');
    submitting = true; button.disabled = true; updateChecks();
    panel.querySelectorAll('button').forEach((item) => item.disabled = true);
    $('directorySupplementSubmit').textContent = '正在提交…';
    try {
      const data = await call(endpoint(id), {method: 'POST', body: JSON.stringify({paths: [...chosen], version: preview.version, preview_token: preview.preview_token, request_id: requestId})});
      if (!current(id, epoch)) return;
      if (data.accepted) {
        close();
        notice.textContent = `已提交 ${data.new_count} 个新增文件，后台将依次解析并更新摘要和智能目录。`;
        toast(`已补选 ${data.new_count} 个文件`);
        window.SJFXTasks?.refresh();
      } else $('directorySupplementPreview').textContent = data.message || '没有新增文件。';
    } catch (error) {
      if (current(id, epoch)) $('directorySupplementPreview').textContent = `${error.message || '提交失败'}；可重试本次提交。`;
    } finally {
      if (!current(id, epoch)) return;
      submitting = false;
      panel.querySelectorAll('button').forEach((item) => item.disabled = false);
      $('directorySupplementSubmit').textContent = '确认补选并解析';
      $('directorySupplementSubmit').disabled = !preview?.new_count;
      updateChecks();
      if (current(id, epoch)) poll();
    }
  });
  async function poll() {
    clearTimeout(pollTimer);
    if (!scanId || !physical() || document.hidden || pollRunning) return;
    const id = scanId, epoch = generation;
    pollRunning = true;
    try {
      const data = await call(endpoint(id) + '?status_only=1');
      if (!current(id, epoch)) return;
      renderStatus(data);
      const batch = data.batch;
      const stamp = batch ? `${batch.selection_version}:${batch.phase}:${batch.published}` : '';
      if (batch && stamp !== lastResultStamp && !editing && !submitting) {
        lastResultStamp = stamp;
        if (['deep_summarizing_files', 'deep_summarizing_nodes', 'deep_update_available'].includes(batch.phase) || batch.published) {
          const isCurrent = () => current(id, epoch) && physical();
          if (isCurrent() && typeof refreshScan === 'function') await refreshScan(id, isCurrent);
        }
      }
    } catch (error) {
      if (current(id, epoch) && error.name !== 'AbortError') {
        button.disabled = true; button.title = error.message;
        notice.textContent = error.message || '补选状态暂时不可用。';
      }
    } finally {
      pollRunning = false;
      if (scanId && physical()) pollTimer = setTimeout(poll, 5000);
    }
  }
  function sync() {
    const id = currentScan();
    if (id !== scanId) {
      generation++; scanId = id; latest = null; lastResultStamp = ''; submitting = false;
      controllers.forEach((controller) => controller.abort()); close();
      panel.querySelectorAll('button').forEach((item) => item.disabled = false);
      $('directorySupplementSubmit').disabled = true;
      $('directorySupplementSubmit').textContent = '确认补选并解析';
    }
    button.hidden = !physical(); notice.hidden = !physical();
    if (!physical()) { close(); clearTimeout(pollTimer); return; }
    if (!id) { button.disabled = true; notice.textContent = '请先导入数据包并完成首次解析。'; return; }
    poll();
  }
  window.addEventListener('sjfx-shell-state', sync);
  window.addEventListener('sjfx-scan-changed', sync);
  document.addEventListener('visibilitychange', sync);
  sync();
})();
