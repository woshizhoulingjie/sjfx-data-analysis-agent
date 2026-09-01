(function () {
  'use strict';
  const CURRENT_SCAN_KEY = 'sjfx_current_scan_id_v1';
  const PAGE_SIZE = 100;
  const state = { scanId: '', data: null, offset: 0, query: '', relationType: '', selectedCase: '', jobTimer: null, requestToken: 0, detailRestoreTarget: null };
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function token(value) {
    value = String(value || '').trim();
    return /^[\x21-\x7e]+$/.test(value) ? value : '';
  }

  async function api(url, options) {
    options = options || {};
    const headers = Object.assign({'Content-Type': 'application/json'}, options.headers || {});
    let credential = token(window.sessionStorage.getItem('sjfx_api_token'));
    if (credential) headers['X-SJFX-Token'] = credential;
    let response = await window.fetch(url, Object.assign({}, options, {headers}));
    if (response.status === 401) {
      credential = token(window.prompt('请输入 SJFX API Token', '') || '');
      if (credential) {
        window.sessionStorage.setItem('sjfx_api_token', credential);
        headers['X-SJFX-Token'] = credential;
        response = await window.fetch(url, Object.assign({}, options, {headers}));
      }
    }
    let body = {};
    try { body = await response.json(); } catch (_) {}
    if (!response.ok || !body.ok) throw new Error(body.error || `请求失败（HTTP ${response.status}）`);
    return body;
  }

  function setMessage(message, kind) {
    const host = $('homogeneousState');
    if (!host) return;
    host.textContent = message;
    host.className = `homogeneous-state${kind ? ` ${kind}` : ''}`;
  }

  function setLoading(loading) {
    const view = document.querySelector('[data-view="homogeneous"]');
    if (!view) return;
    view.classList.toggle('is-loading', Boolean(loading));
    view.setAttribute('aria-busy', String(Boolean(loading)));
  }

  function resetView(scanId) {
    // Clear every result mount immediately when the active package changes.
    // Otherwise the previous package remains visible while the new request is
    // in flight, which is especially misleading for the ledger and relations.
    ['homogeneousMetrics', 'homogeneousSchema', 'homogeneousLedger', 'homogeneousCases', 'homogeneousRelations', 'homogeneousAnomalies'].forEach((id) => {
      const node = $(id);
      if (node) node.innerHTML = '';
    });
    const detail = $('homogeneousDetail');
    if (detail) {
      detail.classList.remove('open');
      detail.innerHTML = '<div class="homogeneous-detail-empty">点击台账中的文件查看字段证据和上下游关系。</div>';
    }
    if ($('homogeneousPageInfo')) $('homogeneousPageInfo').textContent = '—';
    if ($('homogeneousPrevBtn')) $('homogeneousPrevBtn').disabled = true;
    if ($('homogeneousNextBtn')) $('homogeneousNextBtn').disabled = true;
    if ($('homogeneousEligibility')) {
      $('homogeneousEligibility').textContent = scanId ? '读取中' : 'WAITING';
      $('homogeneousEligibility').dataset.status = scanId ? 'running' : 'idle';
    }
    if ($('homogeneousLedger')) $('homogeneousLedger').innerHTML = scanId
      ? '<tr class="homogeneous-ledger-loading"><td colspan="6"><span>正在读取当前数据包…</span></td></tr><tr class="homogeneous-ledger-loading"><td colspan="6"><span></span></td></tr><tr class="homogeneous-ledger-loading"><td colspan="6"><span></span></td></tr>'
      : '<tr><td colspan="6">尚无分析结果</td></tr>';
    if ($('homogeneousSchema')) $('homogeneousSchema').innerHTML = scanId ? '<div class="homogeneous-empty">正在读取公共字段…</div>' : '<div class="homogeneous-empty">导入后显示公共字段覆盖率。</div>';
    if ($('homogeneousCases')) $('homogeneousCases').innerHTML = '<div class="homogeneous-empty">尚未生成事项线程。</div>';
    if ($('homogeneousRelations')) $('homogeneousRelations').innerHTML = '<div class="homogeneous-empty">尚未识别文件关系。</div>';
    if ($('homogeneousAnomalies')) $('homogeneousAnomalies').innerHTML = '<div class="homogeneous-empty">尚未发现需要核对的项目。</div>';
  }

  function syncScan() {
    const scanId = (window.localStorage.getItem(CURRENT_SCAN_KEY) || '').trim();
    if (scanId === state.scanId) return false;
    if (state.jobTimer) { window.clearTimeout(state.jobTimer); state.jobTimer = null; }
    state.requestToken += 1;
    state.scanId = scanId;
    state.data = null;
    state.offset = 0;
    state.query = '';
    state.relationType = '';
    state.selectedCase = '';
    if ($('homogeneousSearch')) $('homogeneousSearch').value = '';
    if ($('homogeneousRelationFilter')) $('homogeneousRelationFilter').value = '';
    ['homogeneousAnalyzeBtn', 'homogeneousRefreshBtn'].forEach((id) => { if ($(id)) $(id).disabled = !scanId; });
    resetView(scanId);
    if (!scanId) setMessage('请先导入并完成一个数据包的基础解析。');
    else setMessage('正在读取当前数据包的同构关联结果…', 'running');
    return true;
  }

  function metricsMarkup(metrics) {
    const integrity = (state.data && state.data.summary && state.data.summary.integrity) || {};
    const items = [
      ['结构化文件', metrics.document_count || 0],
      ['文件关系', metrics.relationship_count || 0],
      ['事项线程', metrics.case_count || 0],
      ['待核对项', metrics.anomaly_count || 0],
      ['关系证据覆盖', `${Math.round(Number(metrics.relation_evidence_coverage || 0) * 100)}%`],
      ['待确认关系', integrity.relation_candidate_count || 0],
      ['截断文件', integrity.text_truncated_files || 0],
    ];
    return items.map(([label, value]) => `<div class="homogeneous-metric"><span>${esc(label)}</span><b>${esc(value)}</b></div>`).join('');
  }

  function renderSchema(summary) {
    const fields = summary.schema_fields || [];
    $('homogeneousSchema').innerHTML = fields.length ? `<div class="homogeneous-schema-list">${fields.map((field) => {
      const percent = Math.round(Number(field.coverage || 0) * 100);
      return `<div class="homogeneous-schema-row"><span>${esc(field.label)}</span><span class="homogeneous-schema-track"><i style="width:${percent}%"></i></span><b>${percent}%</b></div>`;
    }).join('')}</div>` : '<div class="homogeneous-empty">没有识别到公共字段。</div>';
    const badge = $('homogeneousEligibility');
    badge.textContent = summary.eligible ? '可用于关联' : '结构不足';
    badge.dataset.status = summary.eligible ? 'completed' : 'failed';
  }

  function renderLedger(page) {
    const items = page.items || [];
    const relationCounts = new Map();
    const anomalyByPath = new Map();
    ((state.data && state.data.relations) || []).forEach((item) => {
      [item.source_path, item.target_path].filter(Boolean).forEach((path) => {
        relationCounts.set(path, (relationCounts.get(path) || 0) + 1);
      });
    });
    ((state.data && state.data.anomalies) || []).forEach((item) => {
      if (!item.path) return;
      const previous = anomalyByPath.get(item.path) || { count: 0, severity: 'low' };
      const severity = { low: 1, medium: 2, high: 3 };
      anomalyByPath.set(item.path, {
        count: previous.count + 1,
        severity: severity[item.severity] > severity[previous.severity] ? item.severity : previous.severity
      });
    });
    $('homogeneousLedger').innerHTML = items.length ? items.map((record) => {
      const fields = record.fields || {};
      const relationCount = relationCounts.get(record.path) || 0;
      const anomaly = anomalyByPath.get(record.path);
      const understanding = record.content_understanding || {};
      const role = understanding.document_role_label || '';
      const meaning = [
        role ? `<span class="homogeneous-role-badge">${esc(role)}</span>` : '',
        understanding.response_requested ? '<span class="homogeneous-response-badge">需回复</span>' : '',
        `<span class="homogeneous-ledger-summary">${esc(record.summary || '—')}</span>`,
        understanding.requested_action ? `<small>请求：${esc(understanding.requested_action)}</small>` : ''
      ].join('');
      const context = [
        relationCount ? `<span>${relationCount} 条关系</span>` : '<span>独立文件</span>',
        anomaly ? `<span class="${anomaly.severity === 'high' ? 'is-danger' : 'is-warning'}">${anomaly.count} 项待核对</span>` : ''
      ].join('');
      return `<tr class="homogeneous-ledger-row" data-record-path="${esc(record.path)}" tabindex="0" role="button" aria-label="打开 ${esc(record.path)} 的字段与关系详情"><td data-label="日期">${esc(fields.date || '—')}</td><td data-label="编号">${esc(fields.document_number || '—')}</td><td data-label="往来方">${esc(fields.sender || '未知')} → ${esc(fields.recipient || '未知')}</td><td data-label="事项">${esc(fields.subject || '—')}</td><td data-label="内容理解" class="homogeneous-ledger-meaning">${meaning}</td><td data-label="关联" class="homogeneous-ledger-context">${context}</td></tr>`;
    }).join('') : '<tr><td colspan="6">没有符合当前条件的文件</td></tr>';
    const start = page.total ? page.offset + 1 : 0;
    const end = page.offset + items.length;
    $('homogeneousPageInfo').textContent = `${start}–${end} / ${page.total || 0}`;
    $('homogeneousPrevBtn').disabled = page.offset <= 0;
    $('homogeneousNextBtn').disabled = page.next_offset == null;
  }

  function renderCases(cases) {
    const host = $('homogeneousCases');
    if (!cases.length) { host.innerHTML = '<div class="homogeneous-empty">尚未形成两份以上文件的事项线程。</div>'; return; }
    const selected = cases.find((item) => item.case_id === state.selectedCase);
    host.innerHTML = `<div class="homogeneous-case-list">${cases.map((item) => `<button class="homogeneous-case" data-case-id="${esc(item.case_id)}"><b>${esc(item.title)}</b><small>${esc(item.document_count)} 份文件 · ${esc(item.relation_count)} 条关系 · ${esc(item.start_date || '日期不明')} 至 ${esc(item.end_date || '日期不明')}</small></button>`).join('')}</div>${selected ? `<div class="homogeneous-case-dialog"><h3>${esc(selected.title)} · 时间线</h3><div class="homogeneous-timeline">${(selected.timeline || []).map((item) => `<div><b>${esc(item.date || '日期不明')}</b><br>${esc(item.summary)}<br><small>${esc(item.path)}</small></div>`).join('')}</div></div>` : ''}`;
  }

  function renderRelations(relations) {
    const host = $('homogeneousRelations');
    host.innerHTML = relations.length ? `<div class="homogeneous-relation-list">${relations.map((item) => `<div class="homogeneous-relation"><span class="homogeneous-relation-score">${Math.round(Number(item.confidence || 0) * 100)}%</span><b>${esc(item.relation_label)} <span class="homogeneous-relation-status ${esc(item.relation_status || 'derived')}">${esc(item.relation_status === 'validated' ? '已确认' : item.relation_status === 'candidate' ? '待确认' : '推断')}</span></b><small><button type="button" class="homogeneous-relation-path" data-record-path="${esc(item.source_path)}">${esc(item.source_path)}</button><br>→ <button type="button" class="homogeneous-relation-path" data-record-path="${esc(item.target_path)}">${esc(item.target_path)}</button><br>${esc((item.reasons || []).join('；'))}${item.evidence ? `<br>证据：${esc(item.evidence)}` : ''}</small></div>`).join('')}</div>` : '<div class="homogeneous-empty">当前条件下没有文件关系。</div>';
  }

  function renderAnomalies(anomalies) {
    const host = $('homogeneousAnomalies');
    host.innerHTML = anomalies.length ? `<div class="homogeneous-anomaly-list">${anomalies.map((item) => `<div class="homogeneous-anomaly" data-severity="${esc(item.severity)}"><b>${esc(item.label)}</b><small><button type="button" class="homogeneous-anomaly-path" data-record-path="${esc(item.path)}">${esc(item.path)}</button><br>${esc(item.message)}</small></div>`).join('')}</div>` : '<div class="homogeneous-empty">没有发现需要核对的异常。</div>';
  }

  function render() {
    const analysis = state.data;
    if (!analysis) {
      $('homogeneousMetrics').innerHTML = metricsMarkup({});
      return;
    }
    const summary = analysis.summary || {};
    $('homogeneousMetrics').innerHTML = metricsMarkup(summary.metrics || {});
    renderSchema(summary);
    renderLedger(analysis.records || {items: [], total: 0, offset: 0});
    renderCases(analysis.cases || []);
    renderRelations(analysis.relations || []);
    renderAnomalies(analysis.anomalies || []);
    if (summary.eligible) {
      setMessage(`分析完成：识别 ${summary.stable_field_count || 0} 个稳定公共字段，结构一致性 ${Math.round(Number(summary.structural_score || 0) * 100)}%。`);
    } else {
      setMessage(`当前资料未达到同构关联条件：${(summary.eligibility_reasons || []).join('；') || '公共字段不足'}`, 'error');
    }
  }

  async function load(offset) {
    syncScan();
    if (!state.scanId) return;
    const requestedScanId = state.scanId;
    const requestToken = state.requestToken;
    state.offset = Math.max(0, Number(offset == null ? state.offset : offset));
    const params = new URLSearchParams({offset: String(state.offset), limit: String(PAGE_SIZE)});
    if (state.query) params.set('query', state.query);
    if (state.relationType) params.set('relation_type', state.relationType);
    setMessage('正在读取同构文件关联结果…', 'running');
    setLoading(true);
    try {
      const response = await api(`/api/homogeneous-analysis/${encodeURIComponent(requestedScanId)}?${params.toString()}`);
      if (state.scanId !== requestedScanId || state.requestToken !== requestToken) return;
      if (!response.available) {
        state.data = null;
        render();
        setMessage('尚未运行同构文件关联分析。基础解析完成后点击“开始关联分析”。');
        return;
      }
      state.data = response.analysis;
      render();
    } catch (error) {
      if (state.scanId === requestedScanId && state.requestToken === requestToken) setMessage(error.message || '读取失败', 'error');
    } finally {
      if (state.scanId === requestedScanId && state.requestToken === requestToken) setLoading(false);
    }
  }

  function watchJob(jobId) {
    if (state.jobTimer) window.clearTimeout(state.jobTimer);
    const requestedScanId = state.scanId;
    const requestToken = state.requestToken;
    const poll = async () => {
      if (state.scanId !== requestedScanId || state.requestToken !== requestToken) return;
      try {
        const response = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        const job = response.job || {};
        if (state.scanId !== requestedScanId || state.requestToken !== requestToken) return;
        setMessage(`${job.message || '正在分析'} · ${job.progress || 0}%`, job.status === 'failed' ? 'error' : 'running');
        if (job.status === 'completed') {
          $('homogeneousAnalyzeBtn').disabled = false;
          if (state.scanId === requestedScanId && state.requestToken === requestToken) await load(0);
          return;
        }
        if (job.status === 'failed' || job.status === 'cancelled') {
          $('homogeneousAnalyzeBtn').disabled = false;
          setMessage(job.error || job.message || '关联分析未完成', 'error');
          setLoading(false);
          return;
        }
        state.jobTimer = window.setTimeout(poll, 1200);
      } catch (error) {
        if (state.scanId !== requestedScanId || state.requestToken !== requestToken) return;
        setMessage(error.message || '任务状态读取失败', 'error');
        $('homogeneousAnalyzeBtn').disabled = false;
        setLoading(false);
      }
    };
    poll();
  }

  async function start() {
    syncScan();
    if (!state.scanId) return;
    const button = $('homogeneousAnalyzeBtn');
    button.disabled = true;
    setLoading(true);
    setMessage('正在提交同构文件关联任务…', 'running');
    try {
      const response = await api(`/api/homogeneous-analysis/${encodeURIComponent(state.scanId)}`, {method: 'POST', body: '{}'});
      watchJob(response.job_id);
    } catch (error) {
      button.disabled = false;
      setMessage(error.message || '任务提交失败', 'error');
      setLoading(false);
    }
  }

  async function openRecord(path) {
    if (!path || !state.scanId) return;
    const requestedScanId = state.scanId;
    const requestToken = state.requestToken;
    const host = $('homogeneousDetail');
    if (!host.classList.contains('open')) {
      const target = document.activeElement;
      state.detailRestoreTarget = target instanceof HTMLElement ? target : null;
    }
    host.classList.add('open');
    host.setAttribute('role', 'dialog');
    host.setAttribute('aria-modal', 'true');
    host.setAttribute('aria-label', '文件关系详情');
    host.innerHTML = '<button class="homogeneous-detail-close">关闭</button><p>正在读取文件详情…</p>';
    host.querySelector('.homogeneous-detail-close')?.focus();
    try {
      const response = await api(`/api/homogeneous-record/${encodeURIComponent(requestedScanId)}?path=${encodeURIComponent(path)}`);
      if (state.scanId !== requestedScanId || state.requestToken !== requestToken) return;
      const record = response.record || {}, fields = record.fields || {}, evidence = record.field_evidence || {};
      const labels = {document_number:'文件编号',date:'日期',sender:'发件方',recipient:'收件方',subject:'主题事项',matter_id:'事项编号',deadline:'回复期限',signer:'签发人',message_id:'邮件标识',in_reply_to:'回复邮件标识'};
      const custom = record.custom_fields || {}, customEvidence = record.custom_field_evidence || {};
      const understanding = record.content_understanding || {};
      const listMarkup = (items) => (Array.isArray(items) && items.length)
        ? `<ul class="homogeneous-understanding-list">${items.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>`
        : '<span class="muted">未从正文识别</span>';
      const truncation = record.text_truncated
        ? `<div class="homogeneous-integrity-warning">正文已截断：已扫描 ${esc(record.scanned_char_count || 0)} / ${esc(record.source_char_count || 0)} 字符，引用关系可能不完整。</div>`
        : '';
      host.innerHTML = `<button class="homogeneous-detail-close">关闭</button><span class="section-kicker">DOCUMENT DETAIL</span><h2>${esc(record.name || path)}</h2><small>${esc(path)}</small>${truncation}<p>${esc(record.summary)}</p><section class="homogeneous-understanding"><h3>内容理解</h3><dl class="homogeneous-detail-grid"><dt>文件角色</dt><dd>${esc(understanding.document_role_label || '未识别')}</dd><dt>文件意图</dt><dd>${esc(understanding.intent || '未识别')}</dd><dt>请求动作</dt><dd>${esc(understanding.requested_action || '无明确请求')}</dd><dt>是否需要回复</dt><dd>${understanding.response_requested ? '是' : '未识别为需要回复'}</dd><dt>关键事实</dt><dd>${listMarkup(understanding.key_facts)}</dd><dt>主要结论</dt><dd>${listMarkup(understanding.key_conclusions)}</dd></dl><h4>正文证据</h4>${listMarkup(understanding.evidence_units)}</section><dl class="homogeneous-detail-grid">${Object.keys(labels).map((key) => `<dt>${labels[key]}</dt><dd>${esc(fields[key] || '—')}</dd>`).join('')}${Object.keys(custom).map((key) => `<dt>${esc(key)}</dt><dd>${esc(custom[key])}</dd>`).join('')}</dl><h3>字段原文依据</h3>${Object.keys(evidence).length || Object.keys(customEvidence).length ? Object.keys(evidence).map((key) => `<div class="homogeneous-evidence"><b>${esc(labels[key] || key)}</b><br>${esc(evidence[key])}</div>`).concat(Object.keys(customEvidence).map((key) => `<div class="homogeneous-evidence"><b>${esc(key)}</b><br>${esc(customEvidence[key])}</div>`)).join('') : '<p class="muted">暂无字段位置证据。</p>'}<h3>上下游关系</h3>${(response.relations || []).length ? (response.relations || []).map((item) => `<div class="homogeneous-relation"><b>${esc(item.relation_label)} · ${Math.round(Number(item.confidence || 0) * 100)}% <span class="homogeneous-relation-status">${esc(item.relation_status === 'validated' ? '已确认' : item.relation_status === 'candidate' ? '待确认' : '推断')}</span></b><small>${esc(item.source_path)} → ${esc(item.target_path)}<br>${esc((item.reasons || []).join('；'))}${item.evidence ? `<br>证据：${esc(item.evidence)}` : ''}</small></div>`).join('') : '<p class="muted">尚未识别关联文件。</p>'}`;
      host.querySelector('.homogeneous-detail-close')?.focus();
    } catch (error) {
      if (state.scanId === requestedScanId && state.requestToken === requestToken) host.innerHTML = `<button class="homogeneous-detail-close">关闭</button><p>${esc(error.message || '读取失败')}</p>`;
    }
  }

  function closeDetail() {
    const host = $('homogeneousDetail');
    if (!host?.classList.contains('open')) return;
    host.classList.remove('open');
    host.removeAttribute('aria-modal');
    const target = state.detailRestoreTarget;
    state.detailRestoreTarget = null;
    if (target?.isConnected) target.focus();
  }

  function activate() {
    const changed = syncScan();
    if (state.scanId && (changed || !state.data)) load(0);
  }

  function bind() {
    syncScan();
    $('homogeneousAnalyzeBtn').addEventListener('click', start);
    $('homogeneousRefreshBtn').addEventListener('click', () => load(state.offset));
    $('homogeneousSearchBtn').addEventListener('click', () => { state.query = $('homogeneousSearch').value.trim(); load(0); });
    $('homogeneousSearch').addEventListener('keydown', (event) => { if (event.key === 'Enter') { state.query = event.currentTarget.value.trim(); load(0); } });
    $('homogeneousRelationFilter').addEventListener('change', (event) => { state.relationType = event.currentTarget.value; load(state.offset); });
    $('homogeneousPrevBtn').addEventListener('click', () => load(Math.max(0, state.offset - PAGE_SIZE)));
    $('homogeneousNextBtn').addEventListener('click', () => load(state.offset + PAGE_SIZE));
    $('homogeneousLedger').addEventListener('click', (event) => { const row = event.target.closest('[data-record-path]'); if (row) openRecord(row.dataset.recordPath); });
    $('homogeneousLedger').addEventListener('keydown', (event) => {
      const row = event.target.closest('[data-record-path]');
      if (row && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); openRecord(row.dataset.recordPath); }
    });
    $('homogeneousCases').addEventListener('click', (event) => { const button = event.target.closest('[data-case-id]'); if (button) { state.selectedCase = button.dataset.caseId; renderCases((state.data && state.data.cases) || []); } });
    $('homogeneousRelations').addEventListener('click', (event) => { const item = event.target.closest('[data-record-path]'); if (item) openRecord(item.dataset.recordPath); });
    $('homogeneousAnomalies').addEventListener('click', (event) => { const item = event.target.closest('[data-record-path]'); if (item) openRecord(item.dataset.recordPath); });
    $('homogeneousDetail').addEventListener('click', (event) => { if (event.target.closest('.homogeneous-detail-close')) closeDetail(); });
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDetail(); });
    window.setInterval(() => { if (syncScan() && document.body.dataset.route === 'homogeneous') load(0); }, 1200);
  }

  window.SJFXHomogeneous = {activate, refresh: () => load(state.offset)};
  document.addEventListener('DOMContentLoaded', bind);
}());
