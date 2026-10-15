/* SJFX engineering v2 front-end integration.
 *
 * This client deliberately uses only browser primitives: authenticated fetch,
 * SVG and CSS.  It keeps package content visualisation separate from worker
 * telemetry, preserves original text beside translations, and renders every
 * generated answer with its available evidence handles. */
(function () {
  'use strict';

  const CURRENT_SCAN_KEY = 'sjfx_current_scan_id_v1';
  const CHAT_SOURCE_KEY = 'sjfx_chat_source_v1';
  const TRANSLATION_SOURCE_KEY = 'sjfx_translation_source_v1';
  const LAST_DOCUMENT_KEY = 'sjfx_last_document_path_v2';
  const LAST_DOCUMENT_SCAN_KEY = 'sjfx_last_document_scan_id_v1';
  const PAGE_SIZE = 6000;
  const TRANSLATION_LIST_PAGE_SIZE = 100;
  const COLORS = ['#4169a1', '#d68a3a', '#508c78', '#a45f67', '#7667a8', '#b4a13d', '#5b7e94', '#8a7162'];
  const $ = (id) => document.getElementById(id);
  const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  const escapeHtml = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
  const itemsOf = (section) => Array.isArray(section && section.items) ? section.items : [];
  const numeric = (value) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const integer = (value) => Math.round(numeric(value)).toLocaleString('zh-CN');
  const basename = (path) => String(path || '').replace(/\\/g, '/').split('/').pop() || String(path || '');
  const truncate = (value, length) => {
    const text = String(value || '');
    return text.length > length ? text.slice(0, Math.max(1, length - 1)) + '…' : text;
  };

  const state = {
    scanId: '', overview: null, researchBrief: null, reportArtifact: null,
    selectedScope: null, overviewLoading: false, overviewRequestSeq: 0,
    conversation: null, conversationList: [], turns: new Map(), conversationSending: false, conversationLoading: false,
    generalChatMessages: [], generalChatSending: false,
    conversationPending: null, searchIndex: null,
    translationItems: [], translationCounts: {}, translationPath: '', translationView: 'translated',
    translationOffset: 0, translationPage: null, translationLoading: false,
    translationListOffset: 0, translationListPage: null, translationListLoading: false,
    translationRequestSeq: 0, translationListRequestSeq: 0, translationSearchTimer: 0, translationSourceVersion: 0,
    translationSelectedPaths: new Set(), translationSubmitting: false, translationActiveJob: '',
    conversationListRequestSeq: 0, conversationRequestSeq: 0, conversationContextVersion: 0, chatSourceVersion: 0,
    chatSourceKind: 'standard', chatSourceId: '', chatSourceList: [], chatSourceRequestSeq: 0,
    chatSourceOffset: 0, chatSourceHasMore: false, chatSourceLoading: false, chatSourceError: '', chatSourceSelectionSeq: 0,
    translationSourceKind: 'standard', translationSourceId: '', translationSourceList: [], translationSourceRequestSeq: 0,
    translationSourceOffset: 0, translationSourceHasMore: false, translationSourceLoading: false, translationSourceError: '',
    watchers: new Map(), scopeConstraints: {}, scopeEditVersion: 0, fileFolder: new Set(),
    fileReaderRequestSeq: 0
  };
  let fileReaderReturnFocus = null;

  try { state.fileFolder = new Set(JSON.parse(sessionStorage.getItem('sjfx_file_folder_v1') || '[]')); } catch (_) {}

  const OVERVIEW_MOUNT_IDS = [
    'packageOverviewMetrics', 'packageOverviewSummary', 'packageOverviewTreemap',
    'packageOverviewDirectories', 'packageOverviewFormats', 'packageOverviewTypes',
    'packageOverviewLanguages', 'packageOverviewTimeline', 'packageOverviewTopics',
    'packageOverviewEntities', 'packageOverviewRelationships', 'packageOverviewDuplicates',
    'packageOverviewOutliers', 'packageOverviewBriefSummary', 'packageOverviewFindings',
    'packageOverviewDirection', 'packageOverviewResearchDetails', 'packageOverviewScopeFiles'
  ];

  function currentRoute() {
    return document.body.dataset.route || 'dashboard';
  }

  function notify(message, error) {
    const host = $('toast');
    if (!host) return;
    host.textContent = message;
    host.className = 'toast show' + (error ? ' error' : '');
    window.clearTimeout(window.__engineeringToastTimer);
    window.__engineeringToastTimer = window.setTimeout(() => { host.className = 'toast'; }, 5200);
  }

  async function api(url, options) {
    options = options || {};
    const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
    const response = await window.SJFXAuth.request(url, Object.assign({}, options, { headers }));
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* reverse-proxy errors may not be JSON */ }
    if (!response.ok || !payload.ok) {
      const error = new Error(payload.error || `\u8bf7\u6c42\u5931\u8d25\uff08HTTP ${response.status}\uff09`);
      error.status = response.status;
      error.code = payload.code;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function normalizeApiToken(value) {
    const token = String(value || '').trim();
    return /^[\x21-\x7e]+$/.test(token) ? token : '';
  }

  function formatBytes(value) {
    let bytes = numeric(value);
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let unit = 0;
    while (bytes >= 1024 && unit < units.length - 1) { bytes /= 1024; unit += 1; }
    return `${bytes >= 100 || unit === 0 ? bytes.toFixed(0) : bytes.toFixed(1)} ${units[unit]}`;
  }

  function rememberTranslationPath(path, scanId = translationScanId()) {
    const value = String(path || '').trim();
    if (!value) return;
    window.sessionStorage.setItem(LAST_DOCUMENT_KEY, value);
    if (scanId) window.sessionStorage.setItem(LAST_DOCUMENT_SCAN_KEY, scanId);
  }

  function setOverviewState(message, type) {
    const host = $('packageOverviewState');
    if (!host) return;
    host.textContent = message;
    host.className = 'v2-inline-state' + (type ? ` is-${type}` : '');
  }

  function resetOverviewView(scanId) {
    window.SJFXIntelligenceOverview?.clear('packageIntelligenceBoard');
    OVERVIEW_MOUNT_IDS.forEach((id) => { if ($(id)) $(id).innerHTML = ''; });
    const reportButton = $('packageOverviewReportBtn');
    if (reportButton) {
      reportButton.disabled = true;
      reportButton.dataset.filename = '';
    }
    if ($('packageOverviewAskAllBtn')) $('packageOverviewAskAllBtn').disabled = !scanId;
    setOverviewState(scanId ? '正在切换到新的数据包，旧结果已清除…' : '导入数据包后生成内容概览。');
    renderSelectedScope();
  }

  function disclosure(section, noun) {
    if (!section) return '';
    const notes = [];
    if (section.counts_are_approximate) notes.push('高基数数据采用有界估算');
    if (section.truncated) {
      const omitted = section.omitted_count;
      notes.push(omitted == null ? `仅显示主要${noun}` : `另有 ${integer(omitted)} 项未展开`);
    }
    return notes.length ? `<small class="v2-truncation">${escapeHtml(notes.join('；'))}</small>` : '';
  }

  function emptyMarkup(title, detail) {
    return `<div class="v2-empty"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail || '')}</span></div>`;
  }

  function listOf(value) {
    if (Array.isArray(value)) return value.filter((item) => item != null);
    return value == null || value === '' ? [] : [value];
  }

  function textOf(value) {
    if (value == null) return '';
    if (typeof value === 'string' || typeof value === 'number') return String(value);
    return String(value.statement || value.summary || value.description || value.name || value.title || value.text || '');
  }

  function inlineMarkdown(value) {
    return escapeHtml(value)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([0-9]+)\]/g, '<span class="v2-cite-ref">[$1]</span>');
  }

  function safeMarkdown(value) {
    const lines = String(value || '').replace(/\r\n?/g, '\n').split('\n');
    const output = [];
    let list = '', code = false, codeLines = [];
    const closeList = () => { if (list) { output.push(`</${list}>`); list = ''; } };
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      if (/^```/.test(line)) {
        closeList();
        if (code) { output.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`); codeLines = []; }
        code = !code;
        continue;
      }
      if (code) { codeLines.push(line); continue; }
      const cells = line.split('|').map((cell) => cell.trim());
      const next = lines[index + 1] || '';
      if (line.includes('|') && /^\s*\|?\s*:?-{3,}/.test(next)) {
        closeList();
        const headers = cells.filter((cell, cellIndex) => cell || (cellIndex > 0 && cellIndex < cells.length - 1));
        const rows = [];
        index += 2;
        while (index < lines.length && lines[index].includes('|')) {
          rows.push(lines[index].split('|').map((cell) => cell.trim()).filter((cell, cellIndex, all) => cell || (cellIndex > 0 && cellIndex < all.length - 1)));
          index += 1;
        }
        index -= 1;
        output.push(`<div class="v2-md-table-wrap"><table><thead><tr>${headers.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${headers.map((_, cellIndex) => `<td>${inlineMarkdown(row[cellIndex] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
        continue;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) { closeList(); output.push(`<h${heading[1].length + 2}>${inlineMarkdown(heading[2])}</h${heading[1].length + 2}>`); continue; }
      const bullet = line.match(/^\s*[-*]\s+(.+)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (bullet || numbered) {
        const wanted = bullet ? 'ul' : 'ol';
        if (list !== wanted) { closeList(); list = wanted; output.push(`<${list}>`); }
        output.push(`<li>${inlineMarkdown((bullet || numbered)[1])}</li>`);
        continue;
      }
      closeList();
      if (!line.trim()) { output.push(''); continue; }
      if (/^(直接回答|资料依据|进一步分析或建议|初步分析|建议)\s*[：:]?$/.test(line.trim())) {
        output.push(`<h3 class="v2-answer-section">${escapeHtml(line.trim().replace(/[：:]$/, ''))}</h3>`);
      } else {
        output.push(`<p>${inlineMarkdown(line)}</p>`);
      }
    }
    closeList();
    if (codeLines.length) output.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
    return output.join('');
  }

  function scopeAttributes(scope) {
    if (!scope) return '';
    return ` data-overview-scope-kind="${escapeHtml(scope.kind)}" data-overview-scope-value="${escapeHtml(scope.value)}" data-overview-scope-label="${escapeHtml(scope.label || scope.value)}" data-overview-scope-dimension="${escapeHtml(scope.dimension || '')}" data-overview-scope-paths="${escapeHtml(JSON.stringify(scope.source_paths || []))}"`;
  }

  function renderBars(hostId, section, labelKey, valueKey, valueLabel, scopeFactory) {
    const host = $(hostId);
    if (!host) return;
    const items = itemsOf(section);
    if (!items.length) {
      host.innerHTML = emptyMarkup('暂无可展示数据', '该维度尚未在数据包中发现。');
      return;
    }
    const values = items.map((item) => numeric(item[valueKey]));
    const maximum = Math.max(1, ...values);
    host.innerHTML = `<div class="v2-bar-list">${items.slice(0, 16).map((item, index) => {
      const label = typeof labelKey === 'function' ? labelKey(item) : item[labelKey];
      const value = values[index];
      const formatted = valueLabel ? valueLabel(value, item) : integer(value);
      const scope = scopeFactory ? scopeFactory(item, label) : null;
      return `<button type="button" class="v2-bar-row${scope ? ' is-actionable' : ''}" title="${escapeHtml(label)}：${escapeHtml(formatted)}"${scopeAttributes(scope)}><span class="v2-bar-label">${escapeHtml(label || '未知')}</span><span class="v2-bar-track"><i style="width:${Math.max(1.5, value / maximum * 100).toFixed(2)}%"></i></span><b class="v2-bar-value">${escapeHtml(formatted)}</b></button>`;
    }).join('')}</div>${disclosure(section, '分类')}`;
  }

  function renderDonut(hostId, section, labelKey, valueKey, scopeFactory) {
    const host = $(hostId);
    if (!host) return;
    const items = itemsOf(section).slice(0, 8);
    const total = items.reduce((sum, item) => sum + numeric(item[valueKey]), 0);
    if (!items.length || !total) {
      host.innerHTML = emptyMarkup('暂无可展示数据', '该维度尚未在数据包中发现。');
      return;
    }
    let offset = 0;
    const circles = items.map((item, index) => {
      const portion = numeric(item[valueKey]) / total * 100;
      const circle = `<circle cx="18" cy="18" r="15.915" fill="none" stroke="${COLORS[index % COLORS.length]}" stroke-width="5.5" stroke-dasharray="${portion.toFixed(3)} ${(100 - portion).toFixed(3)}" stroke-dashoffset="${(-offset).toFixed(3)}"></circle>`;
      offset += portion;
      return circle;
    }).join('');
    const legend = items.map((item, index) => {
      const rawLabel = typeof labelKey === 'function' ? labelKey(item) : item[labelKey];
      const scope = scopeFactory ? scopeFactory(item, rawLabel) : null;
      return `<button type="button" class="v2-legend-item${scope ? ' is-actionable' : ''}" title="${escapeHtml(rawLabel)}"${scopeAttributes(scope)}><i class="v2-legend-dot" style="background:${COLORS[index % COLORS.length]}"></i><span>${escapeHtml(rawLabel || '未知')}</span><b>${integer(item[valueKey])}</b></button>`;
    }).join('');
    host.innerHTML = `<div class="v2-donut-wrap"><svg class="v2-donut" viewBox="0 0 36 36" role="img" aria-label="${escapeHtml(items.map((item) => `${typeof labelKey === 'function' ? labelKey(item) : item[labelKey]} ${integer(item[valueKey])}`).join('，'))}"><circle cx="18" cy="18" r="15.915" fill="none" stroke="#edf1ee" stroke-width="5.5"></circle><g transform="rotate(-90 18 18)">${circles}</g><text x="18" y="17" text-anchor="middle" fill="#738079" font-size="2.8">合计</text><text x="18" y="21.2" text-anchor="middle" fill="#173b2e" font-size="4.2" font-weight="700">${escapeHtml(integer(total))}</text></svg><div class="v2-donut-legend">${legend}</div></div>${disclosure(section, '分类')}`;
  }

  function renderTimeline(overview) {
    const host = $('packageOverviewTimeline');
    if (!host) return;
    const modified = itemsOf(overview.timeline && overview.timeline.file_modified);
    const documented = itemsOf(overview.timeline && overview.timeline.document_dates);
    const periods = Array.from(new Set(modified.concat(documented).map((item) => String(item.period || '')))).filter(Boolean).sort();
    if (!periods.length) {
      host.innerHTML = emptyMarkup('暂无明确时间', '文件修改时间和正文日期均未形成可展示分布。');
      return;
    }
    const modifiedMap = Object.fromEntries(modified.map((item) => [String(item.period), numeric(item.file_count)]));
    const documentedMap = Object.fromEntries(documented.map((item) => [String(item.period), numeric(item.file_count)]));
    const max = Math.max(1, ...periods.flatMap((period) => [modifiedMap[period] || 0, documentedMap[period] || 0]));
    const width = Math.max(620, periods.length * 70 + 90);
    const height = 230, left = 48, top = 18, plotHeight = 154, base = top + plotHeight;
    const slot = (width - left - 24) / periods.length;
    const grid = [0, .5, 1].map((fraction) => {
      const y = base - fraction * plotHeight;
      return `<line class="v2-grid" x1="${left}" y1="${y}" x2="${width - 18}" y2="${y}"></line><text x="${left - 8}" y="${y + 3}" text-anchor="end">${integer(max * fraction)}</text>`;
    }).join('');
    const bars = periods.map((period, index) => {
      const x = left + index * slot + slot * .22;
      const first = modifiedMap[period] || 0, second = documentedMap[period] || 0;
      const firstHeight = first / max * plotHeight, secondHeight = second / max * plotHeight;
      const matching = modified.concat(documented).filter((item) => String(item.period) === String(period));
      const sourcePaths = Array.from(new Set(matching.flatMap((item) => item.representative_files || [])));
      const attrs = scopeAttributes({ kind: 'time', value: period, label: `${period} 年`, source_paths: sourcePaths });
      return `<g class="v2-overview-scope" role="button" tabindex="0"${attrs}><rect class="v2-bar" x="${x}" y="${base - firstHeight}" width="${Math.max(7, slot * .22)}" height="${firstHeight}"><title>文件修改时间 ${period}：${integer(first)}</title></rect><rect class="v2-bar-alt" x="${x + Math.max(9, slot * .25)}" y="${base - secondHeight}" width="${Math.max(7, slot * .22)}" height="${secondHeight}"><title>正文日期 ${period}：${integer(second)}</title></rect><text x="${left + index * slot + slot * .5}" y="${base + 18}" text-anchor="middle">${escapeHtml(period)}</text></g>`;
    }).join('');
    host.innerHTML = `<div class="v2-timeline-scroll"><svg class="v2-timeline-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="资料年份分布">${grid}${bars}<rect x="${left}" y="${height - 22}" width="9" height="9" class="v2-bar"></rect><text x="${left + 14}" y="${height - 14}">文件修改时间</text><rect x="${left + 112}" y="${height - 22}" width="9" height="9" class="v2-bar-alt"></rect><text x="${left + 126}" y="${height - 14}">正文识别日期</text></svg></div>`;
  }

  function renderEntities(overview) {
    const host = $('packageOverviewEntities');
    if (!host) return;
    const people = itemsOf(overview.entities && overview.entities.people);
    const organisations = itemsOf(overview.entities && overview.entities.organizations);
    const group = (title, items) => `<div class="v2-entity-group"><h3>${escapeHtml(title)}</h3>${items.length ? `<div class="v2-tag-cloud">${items.slice(0, 18).map((item) => `<button type="button" class="v2-entity-tag is-actionable" title="${escapeHtml(item.name)}"${scopeAttributes({ kind: 'entity', value: item.name, label: `${title} · ${item.name}`, source_paths: item.representative_files || [] })}><span>${escapeHtml(item.name || '未知')}</span><b>${integer(item.file_count)}</b></button>`).join('')}</div>` : '<span class="muted">暂无</span>'}</div>`;
    host.innerHTML = `<div class="v2-entity-groups">${group('人物', people)}${group('机构', organisations)}</div>${disclosure(overview.entities && overview.entities.people, '人物')}${disclosure(overview.entities && overview.entities.organizations, '机构')}`;
  }

  function renderRelationships(overview) {
    const host = $('packageOverviewRelationships');
    if (!host) return;
    const section = overview.file_relationships || {};
    const relationships = itemsOf(section).slice(0, 20);
    if (!relationships.length) {
      host.innerHTML = emptyMarkup('暂无稳定文件联系', '当前资料中尚未形成可展示的引用、回复或共同事件联系。');
      return;
    }
    const nodes = [];
    relationships.forEach((item) => {
      [item.source_file, item.target_file].forEach((path) => {
        if (path && !nodes.includes(path) && nodes.length < 14) nodes.push(path);
      });
    });
    const width = 900, height = 300, centreX = 450, centreY = 145, radiusX = 350, radiusY = 105;
    const positions = {};
    nodes.forEach((path, index) => {
      const angle = Math.PI * 2 * index / Math.max(1, nodes.length) - Math.PI / 2;
      positions[path] = { x: centreX + Math.cos(angle) * radiusX, y: centreY + Math.sin(angle) * radiusY };
    });
    const lines = relationships.filter((item) => positions[item.source_file] && positions[item.target_file]).map((item) => {
      const a = positions[item.source_file], b = positions[item.target_file];
      return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"><title>${escapeHtml(item.relation || '关联')} · 权重 ${escapeHtml(item.weight == null ? '—' : item.weight)}</title></line>`;
    }).join('');
    const fileScope = (path) => ({
      kind: 'files', value: path, label: `关联文件 · ${basename(path)}`,
      source_paths: [path], dimension: 'relationship_file'
    });
    const nodeMarkup = nodes.map((path) => {
      const point = positions[path];
      return `<g class="v2-relationship-node v2-overview-scope" role="button" tabindex="0" aria-label="查看 ${escapeHtml(path)} 的资料范围"${scopeAttributes(fileScope(path))}><circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="8"><title>${escapeHtml(path)}</title></circle><text x="${point.x.toFixed(1)}" y="${(point.y + (point.y < centreY ? -13 : 20)).toFixed(1)}" text-anchor="middle">${escapeHtml(truncate(basename(path), 18))}</text></g>`;
    }).join('');
    const rows = relationships.slice(0, 10).map((item) => `<tr><td><button type="button" class="v2-relationship-path" title="限定到 ${escapeHtml(item.source_file)}"${scopeAttributes(fileScope(item.source_file))}>${escapeHtml(item.source_file)}</button></td><td>${escapeHtml(item.relation || '关联')}</td><td><button type="button" class="v2-relationship-path" title="限定到 ${escapeHtml(item.target_file)}"${scopeAttributes(fileScope(item.target_file))}>${escapeHtml(item.target_file)}</button></td><td>${escapeHtml(item.weight == null ? '—' : item.weight)}</td></tr>`).join('');
    host.innerHTML = `<div class="v2-relationship-hint">点击节点或文件路径，可将该文件带入范围、对话或文档查看。</div><div class="v2-relationship-wrap"><svg class="v2-relationship-svg" viewBox="0 0 ${width} ${height}" role="group" aria-label="文件关系网络；点击节点查看对应文件">${lines}${nodeMarkup}</svg></div><table class="v2-relationship-table"><thead><tr><th>来源文件</th><th>关系</th><th>目标文件</th><th>权重</th></tr></thead><tbody>${rows}</tbody></table>${disclosure(section, '关系')}`;
  }

  function duplicateGroup(group) {
    const members = Array.isArray(group.members) ? group.members : [];
    return `<div class="v2-fact"><strong>${escapeHtml(group.canonical_file || group.group_id || '重复文件组')}</strong><small>${integer(group.member_count)} 个成员${members.length ? ` · ${escapeHtml(members.map(basename).join('、'))}` : ''}</small></div>`;
  }

  function renderDuplicates(overview) {
    const host = $('packageOverviewDuplicates');
    if (!host) return;
    const exact = overview.duplicates && overview.duplicates.exact_groups || {};
    const near = overview.duplicates && overview.duplicates.near_duplicate_groups || {};
    const exactItems = itemsOf(exact), nearItems = itemsOf(near);
    host.innerHTML = `<div class="v2-split-facts"><div><b class="v2-fact-count">${integer(exact.duplicate_file_count)}</b><span>精确重复副本</span><div class="v2-fact-list">${exactItems.slice(0, 4).map(duplicateGroup).join('') || '<small class="muted">无精确重复组</small>'}</div></div><div><b class="v2-fact-count">${integer(near.duplicate_file_count)}</b><span>高相似候选文件</span><div class="v2-fact-list">${nearItems.slice(0, 4).map(duplicateGroup).join('') || '<small class="muted">无高相似候选组</small>'}</div></div></div>${disclosure(exact, '精确重复组')}${disclosure(near, '近似重复组')}`;
  }

  function renderOutliers(overview) {
    const host = $('packageOverviewOutliers');
    if (!host) return;
    const anomalous = overview.outliers && overview.outliers.anomalous_files || {};
    const isolated = overview.outliers && overview.outliers.isolated_files || {};
    const facts = [];
    itemsOf(anomalous).slice(0, 6).forEach((item) => facts.push(`<div class="v2-fact"><strong>异常 · ${escapeHtml(item.path)}</strong><small>${escapeHtml((item.reasons || [item.reason]).filter(Boolean).join('；') || '具有显著差异')}</small></div>`));
    itemsOf(isolated).slice(0, 6).forEach((item) => facts.push(`<div class="v2-fact"><strong>孤立 · ${escapeHtml(item.path)}</strong><small>${escapeHtml(item.reason || '尚未发现稳定文件联系')}</small></div>`));
    host.innerHTML = facts.length ? `<div class="v2-fact-list">${facts.join('')}</div>${disclosure(anomalous, '异常文件')}${disclosure(isolated, '孤立文件')}` : emptyMarkup('未发现异常或孤立文件', '当前数据包没有可展示的异常信号。');
  }

  function directoryWeight(item) {
    return Math.max(1, numeric(item.total_bytes) || numeric(item.recursive_file_count));
  }

  function renderTreemap(overview) {
    const host = $('packageOverviewTreemap');
    if (!host) return;
    const directories = itemsOf(overview.directories)
      .filter((item) => item.path && item.path !== '.')
      .slice(0, 12);
    if (!directories.length) {
      host.innerHTML = emptyMarkup('当前数据包没有子目录', '文件直接位于数据包根目录。');
      return;
    }
    const total = directories.reduce((sum, item) => sum + directoryWeight(item), 0);
    const palette = ['blue', 'amber', 'teal', 'rose', 'violet', 'steel'];
    host.innerHTML = directories.map((item, index) => {
      const share = directoryWeight(item) / Math.max(1, total);
      const basis = Math.max(16, share * 100);
      const scope = { kind: 'directory', value: item.path, label: `目录 · ${item.path}`, source_paths: item.representative_files || [] };
      return `<button type="button" class="v2-treemap-node is-${palette[index % palette.length]}" style="flex-grow:${directoryWeight(item)};flex-basis:${basis.toFixed(2)}%"${scopeAttributes(scope)} title="${escapeHtml(item.path)} · ${formatBytes(item.total_bytes)} · ${integer(item.recursive_file_count)} 个文件"><strong>${escapeHtml(item.path)}</strong><span>${formatBytes(item.total_bytes)}</span><small>${integer(item.recursive_file_count)} 个文件</small></button>`;
    }).join('');
  }

  function overviewTimeSpan(overview) {
    const periods = itemsOf(overview.timeline && overview.timeline.document_dates)
      .concat(itemsOf(overview.timeline && overview.timeline.file_modified))
      .map((item) => String(item.period || '')).filter(Boolean).sort();
    return periods.length ? (periods[0] === periods[periods.length - 1] ? periods[0] : `${periods[0]}–${periods[periods.length - 1]}`) : '待识别';
  }

  function parseCoverage(brief) {
    const value = brief && brief.value_judgment || {};
    const coverage = brief && brief.coverage || value.coverage || value.analysis_coverage || {};
    const raw = coverage.content_parse_ratio == null ? (coverage.coverage_ratio == null ? coverage.parsed_file_ratio : coverage.coverage_ratio) : coverage.content_parse_ratio;
    if (raw == null) return '待评估';
    const ratio = Number(raw);
    return Number.isFinite(ratio) ? `${Math.round((ratio <= 1 ? ratio * 100 : ratio))}%` : String(raw);
  }

  function briefList(items, emptyText) {
    const values = listOf(items).map(textOf).filter(Boolean);
    return values.length ? `<ul>${values.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : `<p class="muted">${escapeHtml(emptyText || '暂无')}</p>`;
  }

  function evidenceBrief(item) {
    const source = item.source_path || item.path || '未知来源';
    const location = [item.page ? `第 ${item.page} 页` : '', item.section || item.heading || '', item.archive_member || ''].filter(Boolean).join(' · ');
    return `<details class="v2-brief-evidence"><summary>${escapeHtml(source)}${location ? ` · ${escapeHtml(location)}` : ''}</summary><p>${escapeHtml(item.text || item.original_text || item.snippet || '该条证据暂无可展示原文。')}</p></details>`;
  }

  function renderResearchBrief(brief, artifact) {
    brief = brief || {};
    const intelligence = window.SJFXIntelligenceOverview;
    const reading = intelligence && intelligence.model('ordinary', {}, brief);
    const projected = reading && reading.presentation;
    const briefGrid = $('packageOverviewBriefSummary').closest('.v2-brief-grid');
    if (briefGrid) briefGrid.hidden = Boolean(projected);
    if (projected) {
      $('packageOverviewBriefTitle').textContent = '研究简报与导出';
      for (const id of ['packageOverviewBriefSummary', 'packageOverviewFindings', 'packageOverviewDirection']) {
        if ($(id).innerHTML) $(id).innerHTML = '';
      }
      const hasAppendix = listOf(projected.evidence_appendix).length || listOf(projected.source_notes).length || listOf(projected.source_documents).length;
      const detailHtml = hasAppendix ? '<section><h3>原文与来源定位</h3><a href="#packageOverviewEvidenceAppendix" data-io-open-evidence>查看证据附件</a></section>' : '';
      if ($('packageOverviewResearchDetails').innerHTML !== detailHtml) $('packageOverviewResearchDetails').innerHTML = detailHtml;
      const reportButton = $('packageOverviewReportBtn');
      reportButton.disabled = !(artifact && artifact.filename);
      reportButton.dataset.filename = artifact && artifact.filename || '';
      return;
    }
    const direction = brief.recommended_research_direction || {};
    const stageLabel = { parsed: '解析版', preliminary: '初步摘要版', deep: '深度证据版' }[brief.summary_stage] || '';
    $('packageOverviewBriefTitle').textContent = (brief.title || '数据包研究简报') + (brief.available && stageLabel ? ` · ${stageLabel}` : '');
    $('packageOverviewBriefSummary').innerHTML = brief.available
      ? briefList(brief.basic_information, '暂无总体信息')
      : emptyMarkup('研究简报尚未生成', '完成数据包分析后，这里会形成可阅读和下载的情况概览。');
    $('packageOverviewFindings').innerHTML = `<h4>关键发现</h4>${briefList(brief.key_findings, '当前没有形成稳定发现')}`;
    $('packageOverviewDirection').innerHTML = direction.title
      ? `<h3>${escapeHtml(direction.title)}</h3><div class="v2-direction-meta"><span>优先级 ${escapeHtml(direction.priority || '待评估')}</span><span>置信度 ${escapeHtml(direction.confidence || '待评估')}</span></div><p>${escapeHtml(direction.rationale || '该方向仍需结合代表性资料继续验证。')}</p>`
      : `<p class="muted">当前尚未形成首选研究方向。</p>`;
    const representative = listOf(direction.representative_documents).map(textOf).filter(Boolean);
    const evidence = listOf(direction.evidence_chain).filter((item) => item && typeof item === 'object');
    const candidates = listOf(brief.direction_candidates).filter((item) => item && typeof item === 'object');
    const categories = listOf(brief.global_categories).filter((item) => item != null);
    $('packageOverviewResearchDetails').innerHTML = `
      <section><h3>可继续追问</h3>${briefList(direction.research_questions || direction.questions, '暂无建议问题')}</section>
      <section><h3>建议研究方法</h3>${briefList(direction.methods, '暂无建议方法')}</section>
      <section><h3>代表文件</h3>${representative.length ? `<div class="v2-brief-files">${representative.map((path) => `<button type="button" data-brief-file="${escapeHtml(path)}"><strong>${escapeHtml(basename(path))}</strong><small>${escapeHtml(path)}</small></button>`).join('')}</div>` : '<p class="muted">暂无代表文件</p>'}</section>
      <section><h3>支撑原文</h3>${evidence.length ? evidence.map(evidenceBrief).join('') : '<p class="muted">当前方向尚无可展开的原文片段。</p>'}</section>
      <section><h3>主要内容分类</h3>${categories.length ? `<div class="v2-category-list">${categories.map((item) => `<span>${escapeHtml(textOf(item))}</span>`).join('')}</div>` : '<p class="muted">暂无分类</p>'}</section>
      <section><h3>其他候选方向</h3>${candidates.length ? `<ol class="v2-candidate-list">${candidates.map((item) => `<li><strong>${escapeHtml(item.title || textOf(item))}</strong><span>${escapeHtml(item.rationale || item.description || '')}</span></li>`).join('')}</ol>` : '<p class="muted">暂无其他候选方向</p>'}</section>
      <section class="v2-limitations"><h3>当前限制与待补析</h3>${briefList(brief.limitations, '当前未记录额外限制')}</section>`;
    const reportButton = $('packageOverviewReportBtn');
    reportButton.disabled = !(artifact && artifact.filename);
    reportButton.dataset.filename = artifact && artifact.filename || '';
  }

  function renderSelectedScope() {
    const selected = state.selectedScope;
    const button = $('packageOverviewAskBtn');
    if (!selected) {
      $('packageOverviewScopeTitle').textContent = '点击图表查看对应资料';
      $('packageOverviewScopeNote').textContent = '主题、人物、机构、时间、格式和目录都可以成为独立问答范围。';
      $('packageOverviewScopeFiles').innerHTML = '';
      button.disabled = true;
      return;
    }
    const paths = selected.source_paths || [];
    $('packageOverviewScopeTitle').textContent = selected.label || selected.value;
    $('packageOverviewScopeNote').textContent = paths.length ? `已定位 ${paths.length} 个代表文件，可直接带入对话继续检索。` : '已选定资料范围；对话时将按该维度继续检索。';
    $('packageOverviewScopeFiles').innerHTML = paths.length ? paths.map((path) => `<button type="button" data-brief-file="${escapeHtml(path)}"><strong>${escapeHtml(basename(path))}</strong><small>${escapeHtml(path)}</small></button>`).join('') : '<span class="muted">该聚合项暂未返回代表文件，仍可按范围检索。</span>';
    button.disabled = false;
  }

  function renderOverview(overview) {
    const pkg = overview.package || {};
    const intelligence = window.SJFXIntelligenceOverview;
    if (intelligence) intelligence.mount('packageIntelligenceBoard', $('packageOverviewMetrics')?.closest('.v2-overview-hero'), intelligence.model('ordinary', overview, state.researchBrief));
    const languages = itemsOf(overview.languages);
    const mainLanguage = languages.length ? (languages[0].label || languages[0].language || '待识别') : '待识别';
    const metrics = [
      ['文件总数', integer(pkg.file_count), '数据包中的物理文件'],
      ['数据规模', formatBytes(pkg.total_bytes), '全部文件总体积'],
      ['时间跨度', overviewTimeSpan(overview), '正文日期与文件时间'],
      ['主要语言', mainLanguage, `${integer(languages[0] && languages[0].file_count)} 个已识别文件`],
      ['解析覆盖', parseCoverage(state.researchBrief), '可检索正文覆盖情况']
    ];
    $('packageOverviewMetrics').innerHTML = metrics.map((metric) => `<div class="v2-overview-metric"><span>${escapeHtml(metric[0])}</span><strong>${escapeHtml(metric[1])}</strong><small>${escapeHtml(metric[2])}</small></div>`).join('');
    $('packageOverviewSummary').innerHTML = `<span>数据包画像</span><strong>${escapeHtml(pkg.root || '当前数据包')}</strong><p>${integer(pkg.directory_count)} 个目录，最深 ${integer(pkg.max_depth)} 层；已识别 ${integer(overview.file_relationships && overview.file_relationships.relationship_count)} 条内容或结构联系。</p>`;
    renderTreemap(overview);
    renderBars('packageOverviewDirectories', overview.directories, (item) => item.path === '.' ? '数据包根目录' : item.path, 'recursive_file_count', (value, item) => `${integer(value)} 文件 · ${formatBytes(item.total_bytes)}`, (item, label) => item.path === '.' ? null : ({ kind: 'directory', value: item.path, label, source_paths: item.representative_files || [] }));
    renderDonut('packageOverviewFormats', overview.formats, 'format', 'file_count', (item, label) => ({ kind: 'file_type', value: item.format, label: `格式 · ${label}`, dimension: 'format', source_paths: item.representative_files || [] }));
    renderDonut('packageOverviewTypes', overview.document_types, 'document_type', 'file_count', (item, label) => ({ kind: 'file_type', value: item.document_type, label: `文档类型 · ${label}`, dimension: 'document_type', source_paths: item.representative_files || [] }));
    renderBars('packageOverviewLanguages', overview.languages, (item) => item.label || item.language, 'file_count', null, (item, label) => ({ kind: 'file_type', value: item.label || item.language, label: `语言 · ${label}`, dimension: 'language', source_paths: item.representative_files || [] }));
    renderTimeline(overview);
    renderBars('packageOverviewTopics', overview.topics, 'topic', 'file_count', null, (item, label) => ({ kind: 'topic', value: item.topic, label: `主题 · ${label}`, source_paths: item.representative_files || [] }));
    renderEntities(overview);
    renderRelationships(overview);
    renderDuplicates(overview);
    renderOutliers(overview);
    renderResearchBrief(state.researchBrief, state.reportArtifact);
    renderSelectedScope();
    setOverviewState(`正在查看 ${pkg.root || '当前数据包'} 的内容地图。`, 'ready');
  }

  async function loadOverview(force) {
    if (!state.scanId || state.overviewLoading || (state.overview && !force)) return;
    const requestedScanId = state.scanId;
    const requestSeq = ++state.overviewRequestSeq;
    state.overviewLoading = true;
    $('packageOverviewRefreshBtn').disabled = true;
    setOverviewState('正在汇总数据包自身的内容构成…');
    updateDirectoryScopeOptions('正在读取目录，可先输入相对目录路径。');
    try {
      const response = await api(`/api/package-overview/${encodeURIComponent(requestedScanId)}`);
      if (state.scanId !== requestedScanId || state.overviewRequestSeq !== requestSeq) return;
      state.overview = response.overview || {};
      state.researchBrief = response.research_brief || {};
      state.reportArtifact = response.report_artifact || null;
      renderOverview(state.overview);
      updateDirectoryScopeOptions();
    } catch (error) {
      if (state.scanId === requestedScanId && state.overviewRequestSeq === requestSeq) {
        setOverviewState(error.message || '无法加载数据包概览', 'error');
        updateDirectoryScopeOptions(error.message || '目录读取失败，可输入相对目录路径。');
      }
    } finally {
      if (state.overviewRequestSeq === requestSeq) {
        state.overviewLoading = false;
        if (state.scanId === requestedScanId) $('packageOverviewRefreshBtn').disabled = !state.scanId;
      }
    }
  }

  /* Conversation */
  function scopeKindLabel(kind) {
    return { package: '整个数据包', topic: '主题', directory: '目录', entity: '人物或机构', time: '时间范围', file_type: '文件类型', files: '指定文件' }[kind] || kind;
  }

  function updateDirectoryScopeOptions(message) {
    if (isLargeChatSource() || !$('conversationScopeKind') || $('conversationScopeKind').value !== 'directory') return;
    const node = $('conversationScopeNode');
    if (!node) return;
    const selected = node.value;
    const directories = itemsOf(state.overview && state.overview.directories)
      .filter((item) => item && item.path && item.path !== '.')
      .slice(0, 80);
    const options = ['<option value="">从内容地图选择目录节点</option>'];
    if (selected && !directories.some((item) => item.path === selected)) {
      options.push(`<option value="${escapeHtml(selected)}">${escapeHtml(selected)}</option>`);
    }
    node.innerHTML = options.concat(directories.map((item) =>
      `<option value="${escapeHtml(item.path)}">${escapeHtml(item.path)} · ${integer(item.recursive_file_count)} 个文件</option>`)).join('');
    node.value = selected;
    const hint = $('conversationScopeDirectoryState');
    if (hint) hint.textContent = message || (!state.overview && state.overviewLoading
      ? '正在读取目录，可先输入相对目录路径。'
      : (directories.length ? '可选择目录节点，或输入相对目录路径。' : '可输入相对目录路径。'));
    updateContextChip();
  }

  function renderScopeFields() {
    const host = $('conversationScopeFields');
    if (!host) return;
    const select = $('conversationScopeKind');
    if (select) syncScopeOptions(select);
    const kind = select ? select.value : 'package';
    if (kind === 'package') {
      host.innerHTML = '<p class="help-text">问题将在当前数据包的全部可检索资料中查找证据。</p>';
    } else if (kind === 'time') {
      host.innerHTML = '<label for="conversationScopeStart">开始时间</label><input id="conversationScopeStart" placeholder="例如 2020-01-01"><label for="conversationScopeEnd">结束时间</label><input id="conversationScopeEnd" placeholder="例如 2024-12-31">';
    } else if (kind === 'files') {
      host.innerHTML = '<label for="conversationScopeValue">文件路径（每行一个）</label><textarea id="conversationScopeValue" rows="5" placeholder="letters/a.eml\nreports/annual.pdf"></textarea>';
    } else if (kind === 'directory') {
      host.innerHTML = '<label for="conversationScopeNode">目录节点</label><select id="conversationScopeNode"></select><p id="conversationScopeDirectoryState" class="help-text" aria-live="polite"></p><label for="conversationScopeValue">或输入相对目录路径</label><input id="conversationScopeValue" placeholder="例如：letters/2024">';
      updateDirectoryScopeOptions();
      if (!isLargeChatSource() && state.scanId && !state.overview && !state.overviewLoading) loadOverview(false);
    } else {
      const labels = { topic: '主题名称', entity: '人物或机构名称', file_type: '格式、文档类型或语言' };
      const examples = { topic: '例如：项目交付', entity: '例如：某某机构', file_type: '例如：.pdf 或 报告' };
      host.innerHTML = `<label for="conversationScopeValue">${escapeHtml(labels[kind])}</label><input id="conversationScopeValue" placeholder="${escapeHtml(examples[kind])}">`;
    }
    updateContextChip();
  }

  function updateContextChip(scope) {
    const chip = $('conversationContextChip');
    if (!chip) return;
    if (!scope) {
      try { scope = conversationScope(); } catch (_error) { scope = { kind: $('conversationScopeKind').value || 'package' }; }
    }
    chip.textContent = scopeSummary(scope).replace(/^范围：/, '');
    chip.title = scopeSummary(scope);
  }

  function conversationScope() {
    const kind = $('conversationScopeKind').value;
    if (kind === 'package') return { kind: 'package' };
    if (kind === 'time') {
      const start = ($('conversationScopeStart').value || '').trim();
      const end = ($('conversationScopeEnd').value || '').trim();
      if (!start && !end) throw new Error('请至少填写开始或结束时间');
      const constraints = Object.assign({}, state.scopeConstraints || {});
      const sourcePaths = Array.isArray(constraints.source_paths) ? constraints.source_paths : [];
      delete constraints.source_paths;
      return { kind: 'time', value: Object.assign({}, start ? { start } : {}, end ? { end } : {}), source_paths: sourcePaths, constraints };
    }
    const selectedNode = kind === 'directory' && $('conversationScopeNode')
      ? ($('conversationScopeNode').value || '').trim() : '';
    const value = selectedNode || (($('conversationScopeValue') && $('conversationScopeValue').value) || '').trim();
    if (!value) throw new Error(`请选择或填写${scopeKindLabel(kind)}`);
    if (kind === 'files') {
      const paths = Array.from(new Set(value.split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean)));
      if (!paths.length) throw new Error('请至少填写一个文件路径');
      return { kind: 'files', value: paths, source_paths: paths };
    }
    const constraints = Object.assign({}, state.scopeConstraints || {});
    const sourcePaths = Array.isArray(constraints.source_paths) ? constraints.source_paths : [];
    delete constraints.source_paths;
    return { kind, value, source_paths: sourcePaths, constraints };
  }

  function applyOverviewScope(element) {
    const kind = element.dataset.overviewScopeKind;
    const rawValue = element.dataset.overviewScopeValue || '';
    if (!kind || !rawValue) return;
    const constraints = element.dataset.overviewScopeDimension ? { dimension: element.dataset.overviewScopeDimension } : {};
    constraints.overview_drilldown = true;
    let sourcePaths = [];
    try {
      sourcePaths = JSON.parse(element.dataset.overviewScopePaths || '[]');
    } catch (_error) {
      sourcePaths = [];
    }
    state.selectedScope = {
      kind, value: rawValue, label: element.dataset.overviewScopeLabel || rawValue,
      dimension: element.dataset.overviewScopeDimension || '', source_paths: sourcePaths,
      constraints
    };
    renderSelectedScope();
    $('packageOverviewScopeDock').scrollIntoView({ behavior: 'smooth', block: 'center' });
    notify(`已选中${state.selectedScope.label}`);
  }

  function carryScopeToConversation(scope) {
    state.scopeEditVersion += 1;
    scope = scope || state.selectedScope || { kind: 'package', value: '' };
    state.scopeConstraints = Object.assign({}, scope.constraints || {});
    state.scopeConstraints.source_paths = scope.source_paths || [];
    $('conversationScopeKind').value = scope.kind || 'package';
    renderScopeFields();
    if (scope.kind === 'time') {
      $('conversationScopeStart').value = `${scope.value}-01-01`;
      $('conversationScopeEnd').value = `${scope.value}-12-31`;
    } else if (scope.kind === 'directory') {
      if ($('conversationScopeNode')) $('conversationScopeNode').value = scope.value || '';
      if ($('conversationScopeValue')) $('conversationScopeValue').value = scope.value || '';
    } else if ($('conversationScopeValue')) {
      $('conversationScopeValue').value = scope.value || '';
    }
    updateContextChip();
    window.SJFXShell && window.SJFXShell.activate('chat');
    if (state.conversation) $('conversationQuestion').focus();
    notify(`已带入${scope.label || scopeKindLabel(scope.kind)}资料范围`);
  }

  async function downloadReport() {
    const filename = $('packageOverviewReportBtn').dataset.filename || '';
    if (!filename) return;
    try {
      const ticket = await api('/api/download-ticket', {
        method: 'POST', body: JSON.stringify({ filename })
      });
      const anchor = document.createElement('a');
      anchor.href = ticket.download_url;
      anchor.style.display = 'none';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      notify('研究简报已开始下载');
    } catch (error) { notify(error.message || '简报下载失败', true); }
  }

  function openBriefFile(path, scanId = state.scanId) {
    if (!path) return;
    if (scanId) setTranslationSource(String(scanId).startsWith('lp-') ? 'large' : 'standard', scanId);
    rememberTranslationPath(path);
    window.SJFXShell && window.SJFXShell.activate('translation');
    loadTranslation(path, 0);
  }

  function previousUserQuestion(messageId) {
    const messages = state.conversation && state.conversation.messages || [];
    const index = messages.findIndex((message) => message.message_id === messageId);
    for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
      if (messages[cursor].role === 'user') return messages[cursor].content || '';
    }
    return '';
  }

  function scopeSummary(scope) {
    scope = scope || { kind: 'package' };
    if (scope.kind === 'general') return '通用聊天';
    if (scope.kind === 'package') return '范围：整个数据包';
    if (scope.kind === 'time') return `范围：${scope.value && scope.value.start || '不限'} 至 ${scope.value && scope.value.end || '不限'}`;
    if (scope.kind === 'files') return `范围：${(scope.source_paths || scope.value || []).length} 个指定文件`;
    return `范围：${scopeKindLabel(scope.kind)} · ${scope.label || scope.value || '—'}`;
  }

  function ensureGeneralSession() {
    if (state.chatSourceKind === 'large' || chatScanId() || state.conversation) return;
    state.conversation = {
      session_id: `general-${Date.now()}`,
      scan_id: '',
      title: '通用聊天',
      scope: { kind: 'general' },
      messages: state.generalChatMessages.slice()
    };
  }

  /* Chat data source -------------------------------------------------------
   * The chat page owns its own package selection.  An isolated large package
   * lives in a separate store, so choosing one must never rewrite the global
   * current-scan key that the translation, directory, overview and export
   * views depend on.  ``chatScanId`` therefore resolves the chat package
   * independently and falls back to the ordinary workspace in standard mode.
   */
  const SCOPE_OPTIONS = [
    ['package', '整个数据包'], ['topic', '主题'], ['directory', '目录'],
    ['entity', '人物或机构'], ['time', '时间范围'], ['file_type', '文件类型'],
    ['files', '指定文件']
  ];
  const LARGE_SCOPE_OPTIONS = [['package', '整个数据包'], ['files', '指定文件']];

  function chatScanId() {
    return state.chatSourceKind === 'large' ? state.chatSourceId : state.scanId;
  }

  function isLargeChatSource() {
    return state.chatSourceKind === 'large';
  }

  function chatSourcePayload() {
    return isLargeChatSource() ? { package_type: 'large' } : {};
  }

  function beginConversationTransition() {
    state.conversationContextVersion += 1;
    state.conversationRequestSeq += 1;
    state.conversationSending = false;
    state.generalChatSending = false;
    state.conversationPending = null;
    for (const key of state.watchers.keys()) {
      if (key.startsWith('analysis-turn-') || key.startsWith('conversation-')) state.watchers.delete(key);
    }
    return state.conversationRequestSeq;
  }

  function chatSourceStatusLabel(item) {
    return String((item && item.status) || '').trim() || '未知状态';
  }

  function persistChatSource() {
    try {
      window.localStorage.setItem(CHAT_SOURCE_KEY, JSON.stringify({
        kind: state.chatSourceKind, id: state.chatSourceId
      }));
    } catch (_error) { /* browser storage is optional */ }
  }

  function restoreChatSource() {
    try {
      const stored = JSON.parse(window.localStorage.getItem(CHAT_SOURCE_KEY) || '{}');
      if (stored && stored.kind === 'large' && String(stored.id || '').startsWith('lp-')) {
        state.chatSourceKind = 'large';
        state.chatSourceId = String(stored.id);
        return;
      }
    } catch (_error) { /* fall back to the ordinary package */ }
    state.chatSourceKind = 'standard';
    state.chatSourceId = '';
  }

  function syncScopeOptions(select) {
    if (!select) return;
    const signature = isLargeChatSource() ? 'large' : 'standard';
    if (select.dataset.scopeSignature === signature) return;
    if (signature === 'standard' && !select.dataset.scopeSignature) {
      // The standard option list is rendered by the template; leave it alone.
      select.dataset.scopeSignature = signature;
      return;
    }
    const options = signature === 'large' ? LARGE_SCOPE_OPTIONS : SCOPE_OPTIONS;
    const previous = select.value;
    select.innerHTML = options
      .map(([value, label]) => `<option value="${value}">${label}</option>`)
      .join('');
    select.dataset.scopeSignature = signature;
    select.value = options.some(([value]) => value === previous) ? previous : 'package';
  }

  function renderSourceMoreButton(select, id, loading, hasMore, error, onMore) {
    if (!select) return;
    let button = $(id);
    if (!button) {
      button = document.createElement('button');
      button.id = id;
      button.type = 'button';
      button.className = 'text-button';
      button.addEventListener('click', onMore);
      select.after(button);
    }
    button.hidden = !loading && !hasMore && !error;
    button.disabled = loading;
    button.textContent = loading ? '正在读取…' : (error ? '重试读取数据包' : '加载更多数据包');
  }

  function renderChatSourceBar() {
    const bar = $('chatSourceBar');
    if (!bar) return;
    bar.querySelectorAll('[data-chat-source-kind]').forEach((button) => {
      const active = button.dataset.chatSourceKind === state.chatSourceKind;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    const select = $('chatSourceSelect');
    if (select) {
      const desired = state.chatSourceKind === 'large' ? state.chatSourceId : state.scanId;
      const matched = state.chatSourceList.some((item) => String(item.scan_id) === String(desired));
      select.innerHTML = ['<option value="">选择数据包…</option>']
        .concat(desired && !matched ? [`<option value="${escapeHtml(desired)}">当前数据包 · ${escapeHtml(desired)}</option>`] : [])
        .concat(state.chatSourceList.map((item) => {
          const label = `${item.name || item.scan_id} · ${chatSourceStatusLabel(item)}`;
          // Readiness controls asking, not access to saved package history.
          const disabled = item.selectable === false ? ' disabled' : '';
          return `<option value="${escapeHtml(item.scan_id)}"${disabled}>${escapeHtml(label)}</option>`;
        }))
        .join('');
      select.value = desired ? String(desired) : '';
      select.disabled = !state.chatSourceList.length && !desired;
    }
    renderSourceMoreButton(select, 'chatSourceMoreBtn', state.chatSourceLoading,
      state.chatSourceHasMore, state.chatSourceError, () => loadChatSources(state.chatSourceOffset > 0));
    const hint = $('chatSourceState');
    if (hint) {
      hint.textContent = state.chatSourceError || (state.chatSourceLoading ? '正在读取数据包列表…' : '') || (state.chatSourceKind === 'large'
        ? '大包问答依据文件摘要，证据定位到文件'
        : '普通数据包沿用数据包页当前选择');
    }
    syncScopeOptions($('conversationScopeKind'));
  }

  async function loadChatSources(append = false) {
    if (append && state.chatSourceLoading) return;
    const requestSeq = ++state.chatSourceRequestSeq;
    const kind = state.chatSourceKind;
    const endpoint = kind === 'large' ? '/api/large-packages/history' : '/api/data-sources';
    const offset = append ? state.chatSourceOffset : 0;
    state.chatSourceLoading = true;
    state.chatSourceError = '';
    if (!append) { state.chatSourceOffset = 0; state.chatSourceHasMore = false; state.chatSourceList = []; }
    renderChatSourceBar();
    try {
      const response = await api(`${endpoint}?limit=50&offset=${offset}`);
      if (state.chatSourceRequestSeq !== requestSeq || state.chatSourceKind !== kind) return;
      const items = (response.items || []).map((item) => ({
        scan_id: String(item.scan_id || item.id || ''),
        name: String(item.name || item.root || item.scan_id || ''),
        status: item.status || '',
        usable: item.usable === undefined ? true : Boolean(item.usable),
        selectable: item.selectable !== false,
      })).filter((item) => item.scan_id);
      const merged = append ? state.chatSourceList.concat(items) : items;
      state.chatSourceList = Array.from(new Map(merged.map((item) => [item.scan_id, item])).values());
      state.chatSourceOffset = offset + (response.items || []).length;
      const total = Number(response.total);
      state.chatSourceHasMore = items.length > 0 && (Number.isFinite(total)
        ? state.chatSourceOffset < total : items.length === 50);
    } catch (error) {
      if (state.chatSourceRequestSeq !== requestSeq || state.chatSourceKind !== kind) return;
      state.chatSourceError = error.message || '数据包列表读取失败';
    } finally {
      if (state.chatSourceRequestSeq === requestSeq && state.chatSourceKind === kind) {
        state.chatSourceLoading = false;
        renderChatSourceBar();
      }
    }
  }

  async function setChatSource(kind, id) {
    closeOriginalFile();
    const selectionSeq = ++state.chatSourceSelectionSeq;
    const nextKind = kind === 'large' ? 'large' : 'standard';
    if (nextKind === 'standard' && id && id !== state.scanId) {
      // Picking an ordinary package keeps the single global workspace in step,
      // exactly like the data-package page does.
      try {
        await api('/api/data-sources/select', { method: 'POST', body: JSON.stringify({ scan_id: id }) });
        if (selectionSeq !== state.chatSourceSelectionSeq) return;
      } catch (error) {
        if (selectionSeq !== state.chatSourceSelectionSeq) return;
        renderChatSourceBar();
        notify(error.message || '切换数据包失败', true);
        return;
      }
      state.chatSourceKind = 'standard';
      state.chatSourceId = '';
      persistChatSource();
      try {
        window.localStorage.setItem(CURRENT_SCAN_KEY, id);
        window.dispatchEvent(new CustomEvent('sjfx-scan-changed', { detail: { scanId: id } }));
      } catch (_error) { /* storage is optional */ }
      resetForScan(id);
      loadChatSources();
      loadConversationList();
      // Keep ordinary directory and analysis views on this workspace without
      // routing away from chat or waiting for their bounded read requests.
      const isWorkspaceCurrent = () => {
        try { return window.localStorage.getItem(CURRENT_SCAN_KEY) === id; }
        catch (_error) { return state.scanId === id; }
      };
      window.SJFXDataSources?.syncWorkspace?.(id, isWorkspaceCurrent).catch((error) => {
        if (isWorkspaceCurrent()) notify(error.message || '数据包目录读取失败', true);
      });
      return;
    }
    const nextId = nextKind === 'large' ? String(id || '') : '';
    if (nextKind === state.chatSourceKind && nextId === state.chatSourceId) return;
    const kindChanged = nextKind !== state.chatSourceKind;
    state.chatSourceKind = nextKind;
    state.chatSourceId = nextId;
    if (kindChanged) {
      state.chatSourceRequestSeq += 1;
      state.chatSourceLoading = false;
      state.chatSourceList = [];
      state.chatSourceOffset = 0;
      state.chatSourceHasMore = false;
      state.chatSourceError = '';
    }
    state.chatSourceVersion += 1;
    beginConversationTransition();
    state.conversationListRequestSeq += 1;
    persistChatSource();
    // Every chat artifact belonged to the previous package and must go.
    state.conversation = null;
    state.conversationPending = null;
    state.conversationLoading = false;
    state.searchIndex = null;
    state.conversationList = [];
    state.turns.clear();
    state.fileSummaries = null;
    state.fileSummariesScanId = '';
    state.scopeConstraints = {};
    if ($('conversationScopeKind')) { $('conversationScopeKind').value = 'package'; }
    renderChatSourceBar();
    renderScopeFields();
    renderConversation();
    renderConversationList();
    notify(nextKind === 'large' ? '已切换到大数据库，证据定位到文件' : '已切换到普通数据包');
    if (chatScanId()) loadConversationList();
  }

  function bindChatSource() {
    const bar = $('chatSourceBar');
    if (!bar) return;
    bar.addEventListener('click', (event) => {
      const button = event.target.closest('[data-chat-source-kind]');
      if (!button) return;
      const kind = button.dataset.chatSourceKind;
      if (kind === state.chatSourceKind) return;
      setChatSource(kind, '');
      loadChatSources();
    });
    const select = $('chatSourceSelect');
    if (select) {
      select.addEventListener('change', (event) => {
        const id = String(event.target.value || '');
        if (!id) return;
        setChatSource(state.chatSourceKind, id);
      });
    }
  }

  function renderConversationList() {
    const host = $('conversationList');
    if (!host) return;
    if (!state.conversationList.length) {
      host.innerHTML = '<span class="help-text">当前数据包还没有历史会话。</span>';
      return;
    }
    host.innerHTML = state.conversationList.map((item) => {
      const active = state.conversation && state.conversation.session_id === item.session_id ? ' active' : '';
      return `<div class="v2-session-row${active}"><button type="button" class="v2-session-item${active}" data-session-id="${escapeHtml(item.session_id)}"><strong>${escapeHtml(item.title || '资料问答')}</strong><span>${escapeHtml(scopeSummary(item.scope))} · ${integer(item.message_count)} 条消息</span></button><button type="button" class="v2-session-delete" data-delete-session="${escapeHtml(item.session_id)}" title="删除这个会话" aria-label="删除会话">×</button></div>`;
    }).join('');
  }

  async function loadConversationList() {
    if (!chatScanId()) return;
    const requestedScanId = chatScanId();
    const requestSeq = ++state.conversationListRequestSeq;
    try {
      const response = await api(`/api/conversations/${encodeURIComponent(requestedScanId)}`);
      if (chatScanId() !== requestedScanId || state.conversationListRequestSeq !== requestSeq) return;
      state.conversationList = response.items || [];
      renderConversationList();
    } catch (error) {
      if (chatScanId() === requestedScanId && state.conversationListRequestSeq === requestSeq) $('conversationList').innerHTML = `<span class="help-text">${escapeHtml(error.message)}</span>`;
    }
  }

  function citationMarkup(citation) {
    const legacyPackageId = (String(citation.evidence_id || '').match(/^large:(lp-[^:]+):/) || [])[1];
    const sourcePackage = citation.source_package || (legacyPackageId ? {
      id: legacyPackageId,
      name: (state.chatSourceList.find((item) => item.scan_id === legacyPackageId) || {}).name,
      type: 'large'
    } : null);
    const fileEvidence = citation.evidence_level === 'file' || Boolean(sourcePackage && sourcePackage.type === 'large' && citation.evidence_level !== 'passage');
    const packageLabel = sourcePackage ? `<small>大数据包：${escapeHtml(sourcePackage.name || sourcePackage.id)} · ${escapeHtml(sourcePackage.id)}</small>` : '';
    const location = fileEvidence ? '文件级证据 · 已保存摘要，非全文原文' : citation.location && Object.keys(citation.location).length
      ? Object.entries(citation.location).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join('-') : value}`).join(' · ') : '原文片段';
    const original = citation.original_text ? `<p><b>${fileEvidence ? '文件摘要' : '原文'}：</b>${escapeHtml(citation.original_text)}</p>` : '';
    const translated = citation.translated_text ? `<p><b>中文：</b>${escapeHtml(citation.translated_text)}</p>` : '';
    const rerankRank = Number(citation.rerank_rank);
    const scoreValue = Number(citation.retrieval_score);
    // After a rerank the list order is the honest relevance signal; showing the
    // raw retrieval score there would look out of order.
    const scoreLabel = Number.isFinite(rerankRank) && rerankRank > 0
      ? `综合排序 ${rerankRank}`
      : (Number.isFinite(scoreValue) ? `相关度 ${scoreValue.toFixed(2)}` : '');
    const matchedKeywords = Array.isArray(citation.matched_keywords) ? citation.matched_keywords.filter(Boolean) : [];
    const matchLabel = citation.match_reason
      ? String(citation.match_reason)
      : (matchedKeywords.length ? `命中关键词：${matchedKeywords.slice(0, 5).join('、')}` : '');
    const provenance = [scoreLabel, matchLabel].filter(Boolean).join(' · ');
    const provenanceLine = provenance ? `<small class="v2-citation-provenance">${escapeHtml(provenance)}</small>` : '';
    const path = String(citation.source_path || '');
    const actions = path ? `<div class="v2-citation-actions"><button type="button" class="text-button" data-citation-read="${escapeHtml(path)}">阅读原文</button><button type="button" class="text-button" data-citation-collect="${escapeHtml(path)}">收入资料夹</button><button type="button" class="text-button" data-citation-node="${escapeHtml(path)}">归入节点</button><button type="button" class="text-button" data-citation-translation="${escapeHtml(path)}" data-translation-package="${escapeHtml(sourcePackage && sourcePackage.id || chatScanId())}">查看译文</button></div>` : '';
    return `<article class="v2-citation">${packageLabel}<strong>${escapeHtml(citation.citation_label || '')} ${escapeHtml(path || '未知来源')}</strong><small>${escapeHtml(location)} · ${escapeHtml(citation.evidence_role || '直接证据')}</small>${provenanceLine}${translated}${original}${actions}</article>`;
  }

  function packageReadingMarkup(turn) {
    const reading = turn && (turn.package_reading || turn.input_manifest && turn.input_manifest.package_reading);
    if (!reading || reading.files_read == null) return '';
    const filesRead = integer(reading.files_read);
    const summaryFiles = integer(reading.summary_backed_files);
    const missing = numeric(reading.files_without_summary);
    const coverage = reading.summary_coverage == null ? '' : ` · 摘要覆盖 ${Math.round(numeric(reading.summary_coverage) * 100)}%`;
    const summaryComplete = reading.summary_complete == null ? reading.complete : reading.summary_complete;
    const status = summaryComplete ? '摘要已齐备' : '摘要待补全';
    const detail = summaryComplete
      ? `已逐份纳入 ${filesRead} 个文件的已持久化摘要${coverage}`
      : `已纳入 ${summaryFiles}/${filesRead} 份可用摘要${coverage}，仍有 ${integer(missing)} 份等待补全`;
    return `<section class="v2-package-reading ${summaryComplete ? 'is-complete' : 'is-partial'}" aria-label="资料摘要覆盖范围"><div><strong>摘要覆盖</strong><span>${escapeHtml(status)}</span></div><p>${escapeHtml(detail)}；这不表示模型已读完全部原文。</p></section>`;
  }

  function analysisTurnStatusLabel(status) {
    return {
      queued: '等待分析', running: '正在分析', waiting_for_deep_analysis: '正在补充深析',
      pending: '等待执行', completed: '分析完成', failed: '分析失败', cancelled: '已取消',
      skipped: '已跳过'
    }[status] || status || '等待分析';
  }

  function analysisTurnStageLabel(stage) {
    return {
      queued: '等待执行', understanding: '理解指令', planning: '制定计划',
      retrieving: '检索资料', batching: '分批归并', tool_execution: '专业工具分析',
      scanning: '扫描全范围资料', synthesizing: '分批综合资料', reducing: '归并综合要点',
      composing: '组织回答',
      waiting_for_deep_analysis: '补充深析', executing: '执行分析',
      verifying: '核验结论', repairing: '补检索并修正', completed: '分析完成',
      failed: '分析失败', cancelled: '已取消'
    }[stage] || stage || '等待执行';
  }

  function taskStatusLabel(status) {
    return {
      fulfilled: '\u4efb\u52a1\u5df2\u5b8c\u6210',
      partially_fulfilled: '\u4efb\u52a1\u90e8\u5206\u5b8c\u6210',
      not_fulfilled: '\u4efb\u52a1\u672a\u5b8c\u6210'
    }[status] || '';
  }

  function intentLabel(intent) {
    return {
      creative: '\u521b\u4f5c', general_qa: '\u901a\u7528\u95ee\u7b54',
      casual: '\u5bf9\u8bdd', retrieval: '\u8d44\u6599\u95ee\u7b54',
      summary: '\u8d44\u6599\u6458\u8981', analysis: '\u5206\u6790',
      structured: '\u7ed3\u6784\u5316\u67e5\u8be2', translation: '\u7ffb\u8bd1',
      file_search: '文件搜索',
      relationship: '\u5173\u7cfb\u5206\u6790', multi_task: '\u7efc\u5408\u4efb\u52a1'
    }[intent] || intent || '\u52a9\u624b\u56de\u7b54';
  }

  function conclusionVerificationLabel(status, turn) {
    if (status !== 'unverified') return status;
    const disabled = turn && (turn.answer_verification_enabled === false
      || turn.verification && turn.verification.disabled === true
      || turn.quality_metrics && turn.quality_metrics.answer_verification_enabled === false);
    return disabled ? '未进行结论核验' : '结论尚未核实';
  }

  function analysisQualityMarkup(turn) {
    const metrics = turn && turn.quality_metrics;
    if (!metrics || typeof metrics !== 'object' || metrics.verification_status === 'not_required') return '';
    const ratioValue = metrics.evidence_support_ratio == null ? metrics.claim_support_ratio : metrics.evidence_support_ratio;
    const ratio = ratioValue == null ? null : numeric(ratioValue);
    const coverageValue = metrics.retrieval_coverage == null ? metrics.query_coverage : metrics.retrieval_coverage;
    const coverage = coverageValue == null ? null : numeric(coverageValue);
    const scopeFiles = numeric(metrics.scope_files == null ? metrics.inventory_files : metrics.scope_files);
    const inspectedFiles = numeric(metrics.inspected_files);
    const uncheckedFiles = Math.max(0, scopeFiles - inspectedFiles);
    const candidateDepthValue = metrics.deep_analysis_coverage == null ? metrics.candidate_deep_coverage : metrics.deep_analysis_coverage;
    const candidateDepth = candidateDepthValue == null ? null : numeric(candidateDepthValue);
    const scopeCoverageValue = metrics.inspection_coverage == null ? metrics.scope_inspection_coverage : metrics.inspection_coverage;
    const scopeCoverage = scopeCoverageValue == null ? null : numeric(scopeCoverageValue);
    const packageReadingFiles = metrics.package_reading_files == null ? null : numeric(metrics.package_reading_files);
    const summaryBackedFiles = metrics.summary_backed_files == null ? null : numeric(metrics.summary_backed_files);
    const packageReadingCoverage = metrics.package_reading_coverage == null ? null : numeric(metrics.package_reading_coverage);
    const values = [
      ['范围文件', integer(scopeFiles)],
      ['候选文件', integer(metrics.candidate_files)],
      ['实际检查', integer(inspectedFiles)],
      ['未检查', integer(uncheckedFiles)],
      ['深析完成', integer(metrics.deep_candidate_files == null ? metrics.deep_analyzed_files : metrics.deep_candidate_files)],
      ['分析批次', integer(metrics.batch_count)],
      ['引用', integer(metrics.citation_count)],
      ['结论支持率', ratio == null ? '待核验' : `${Math.round(ratio * 100)}%`],
      ['无证据结论', integer(metrics.unsupported_claim_count)],
      ['反证', integer(metrics.counter_evidence_count)],
      ['矛盾', integer(metrics.contradiction_count)],
      ['未解析文件', integer(metrics.unparsed_files)]
    ];
    if (scopeCoverage != null) values.splice(5, 0, ['范围检查率', `${Math.round(scopeCoverage * 100)}%`]);
    if (candidateDepth != null) values.splice(6, 0, ['候选深析率', `${Math.round(candidateDepth * 100)}%`]);
    if (coverage != null && scopeCoverage == null && candidateDepth == null) values.splice(5, 0, ['覆盖率', `${Math.round(coverage * 100)}%`]);
    if (packageReadingFiles != null) values.splice(1, 0, ['全包摘要', `${integer(summaryBackedFiles)}/${integer(packageReadingFiles)}`]);
    if (packageReadingCoverage != null) values.splice(2, 0, ['摘要覆盖', `${Math.round(packageReadingCoverage * 100)}%`]);
    const status = { verified: '引用核验通过', partial: '引用部分通过', insufficient_evidence: '证据不足', not_required: '无需引用核验' }[metrics.verification_status] || conclusionVerificationLabel(metrics.verification_status, turn) || '待核验';
    const task = taskStatusLabel(turn && turn.task_status);
    return `<section class="v2-quality" aria-label="分析质量"><header><strong>分析质量</strong><span>${escapeHtml(task ? `${task} · ${status}` : status)}</span></header><div>${values.map(([label, value]) => `<dl><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></dl>`).join('')}</div></section>`;
  }

  function registerAnalysisTurn(turn, steps) {
    if (!turn || !turn.assistant_message_id) return;
    const result = turn.result && typeof turn.result === 'object' ? turn.result : {};
    const previous = state.turns.get(turn.assistant_message_id) || {};
    const storedMessage = state.conversation && Array.isArray(state.conversation.messages)
      ? state.conversation.messages.find((item) => item.message_id === turn.assistant_message_id)
      : null;
    const active = ['queued', 'running', 'waiting_for_deep_analysis'].includes(turn.status);
    const currentMessage = turn.message || turn.current_message || result.message
      || (active && storedMessage && storedMessage.metadata && storedMessage.metadata.stage === turn.stage
          && numeric(storedMessage.metadata.progress) === numeric(turn.progress) ? storedMessage.content : '')
      || (previous.stage === turn.stage && numeric(previous.progress) === numeric(turn.progress) ? previous.progress_message : '');
    const relatedPaths = Array.isArray(result.related_file_paths) ? result.related_file_paths.filter(Boolean) : [];
    // Saved paths already identify the files; opening history needs no search.
    const savedFiles = relatedPaths.length && !result.file_results && !previous.file_results ? {
      file_results: relatedPaths.map((path) => ({
        path, node_path: path, name: basename(path),
        extension: path.includes('.') ? path.slice(path.lastIndexOf('.')).toLowerCase() : '',
        preview: { text: '' }, analysis_level: '已归集',
      })),
      file_results_total: relatedPaths.length,
    } : {};
    state.turns.set(turn.assistant_message_id, {
      ...previous,
      ...result,
      ...savedFiles,
      turn_id: turn.id,
      scan_id: turn.scan_id || previous.scan_id,
      status: turn.status,
      stage: turn.stage,
      progress: numeric(turn.progress),
      progress_message: active ? currentMessage || '' : '',
      error: turn.error,
      job_id: turn.job_id,
      promotion_job_id: turn.promotion_job_id,
      plan: turn.plan,
      verification: turn.verification,
      steps: steps || turn.steps || []
    });
  }

  function registerAnalysisTurns(turns) {
    (turns || []).forEach((turn) => registerAnalysisTurn(turn, turn.steps));
  }

  function assistantDisplayText(value) {
    let text = String(value || '');
    text = text.split(/\n(?:\u8d44\u6599\u4f9d\u636e|\u6838\u9a8c\u8bf4\u660e)\s*\n/)[0];
    text = text.replace(/\s*\[\d+\]/g, '');
    text = text.replace(/^\s*(\u6838\u9a8c\u540e\u7684\u7ed3\u8bba|\u8d44\u6599\u4f9d\u636e|\u6838\u9a8c\u8bf4\u660e)\s*$/gm, '');
    text = text.replace(/^\s*-\s*\d+\s*\u6761\u9648\u8ff0.*$/gm, '');
    return text.trim();
  }

  function messageMarkup(message) {
    const role = message.role === 'user' ? 'user' : 'assistant';
    const turn = state.turns.get(message.message_id);
    const intent = message.intent || turn && turn.intent && turn.intent.name;
    const simpleFileSearch = intent === 'file_search' || Boolean(turn && turn.file_search);
    const qaIntent = intent || turn && turn.plan && Array.isArray(turn.plan.modes) && turn.plan.modes[0];
    const inputManifest = turn && turn.input_manifest;
    const qaScanId = turn && turn.scan_id || state.conversation && state.conversation.scan_id || chatScanId();
    const largeQA = role === 'assistant' && turn && !simpleFileSearch && qaIntent !== 'file_search'
      && !['casual', 'creative', 'general_qa'].includes(qaIntent) && Boolean(
      String(qaScanId || '').startsWith('lp-')
      || inputManifest && inputManifest.large_qa && Object.keys(inputManifest.large_qa).length
    );
    const finalLargeQA = largeQA && turn.status === 'completed';
    const running = turn && ['queued', 'running', 'waiting_for_deep_analysis'].includes(turn.status);
    const progress = running ? `<div class="v2-turn-progress" role="status"><div><span>${escapeHtml(analysisTurnStageLabel(turn.stage))}${turn.progress_message ? ` · ${escapeHtml(turn.progress_message)}` : ''}</span><b>${Math.round(numeric(turn.progress))}%</b></div><progress max="100" value="${Math.round(numeric(turn.progress))}"></progress></div>` : '';
    if (largeQA && running) {
      // Draft checkpoints are recovery data. A live QA card shows progress
      // until the final answer, evidence and caveats are ready together.
      return `<div class="v2-message ${role}" data-message-id="${escapeHtml(message.message_id || '')}"><div class="v2-message-card">${progress}<div class="v2-message-actions"><button type="button" data-cancel-turn="${escapeHtml(turn.turn_id || '')}">停止分析</button></div></div></div>`;
    }
    let evidence = '';
    if (turn && Array.isArray(turn.citations) && turn.citations.length) {
      // The page lists every retrieved result; the model only receives the
      // dynamically selected core evidence, so both counts are surfaced.
      const retrievedCount = Number.isFinite(Number(turn.retrieval_count)) ? Number(turn.retrieval_count) : turn.citations.length;
      const modelCount = Number.isFinite(Number(turn.model_evidence_count)) ? Number(turn.model_evidence_count) : null;
      const evidenceCounts = modelCount == null
        ? `检索到 ${integer(retrievedCount)} 条证据`
        : `检索到 ${integer(retrievedCount)} 条证据 · 本轮模型采用 ${integer(modelCount)} 条核心证据`;
      evidence = `${packageReadingMarkup(turn)}<details class="v2-citations"><summary>证据轨迹 · ${evidenceCounts}</summary>${turn.citations.map(citationMarkup).join('')}</details>`;
    } else if (message.evidence_ids && message.evidence_ids.length) {
      evidence = `<details class="v2-citations"><summary>查看证据标识</summary><div class="v2-citation"><small>${escapeHtml(message.evidence_ids.join('、'))}</small></div></details>`;
    }
    const task = turn && taskStatusLabel(turn.task_status);
    const evidenceStatus = turn && turn.evidence_status;
    const fileResults = turn && Array.isArray(turn.file_results) ? turn.file_results : [];
    const completePackageFiles = Boolean(turn && Array.isArray(turn.related_file_paths) && turn.related_file_paths.length);
    const renderedFileResults = completePackageFiles ? fileResults : fileResults.slice(0, 20);
    const fileCountLabel = simpleFileSearch ? '个命中文件' : '个范围文件';
    const fileCards = fileResults.length ? `<details class="v2-file-results-fold"><summary>相关文件 · ${integer(turn.file_results_total || fileResults.length)} ${fileCountLabel}${turn.file_results_coverage ? ` · ${escapeHtml(turn.file_results_coverage)}` : ''}（点击展开／收起）</summary><section class="v2-file-results"><header><div><strong>相关文件</strong><span>${integer(turn.file_results_total || fileResults.length)} ${fileCountLabel}${turn.file_results_coverage ? ` · ${escapeHtml(turn.file_results_coverage)}` : ''}</span></div><div class="v2-file-bulk-actions"><span class="v2-file-selection-status" data-file-selection-count>未选择文件</span><button type="button" data-select-all-files>全选</button><button type="button" data-create-node-selected disabled>选中后创建节点</button><button type="button" data-export-file-folder>导出资料夹</button></div></header>${renderedFileResults.map((item) => {
      const path = item.node_path || item.path || item.source_path || '';
      const snippets = Array.isArray(item.snippets) ? item.snippets.filter(Boolean) : [];
      const preview = item.preview || {};
      const previewText = preview.text || snippets.join(' … ');
      const pages = Array.isArray(item.pages) && item.pages.length ? ` · 页 ${item.pages.slice(0, 3).join(', ')}` : '';
      const status = item.analysis_level || item.status || '';
      return `<article class="v2-file-card"><label class="v2-file-select"><input type="checkbox" data-file-select="${escapeHtml(path)}"><span class="sr-only">选择 ${escapeHtml(basename(path))}</span></label><div><strong title="${escapeHtml(path)}">${escapeHtml(basename(path))}</strong><small>${escapeHtml(path)} · ${escapeHtml(item.extension || '未知类型')}${item.size != null ? ` · ${escapeHtml(formatBytes(item.size))}` : ''}${pages}${status ? ` · ${escapeHtml(status)}` : ''}</small>${previewText ? `<p>${escapeHtml(truncate(previewText, 260))}</p>` : ''}</div><div class="v2-file-card-actions"><button type="button" data-file-open="${escapeHtml(path)}">阅读原文</button><button type="button" data-file-deep-summary="${escapeHtml(path)}">深度摘要</button><button type="button" data-file-collect="${escapeHtml(path)}">加入资料夹</button><button type="button" data-file-node="${escapeHtml(path)}">归入新节点</button></div></article>`;
    }).join('')}</section></details>` : '';
    const meta = finalLargeQA ? '<div class="v2-message-meta"><span>资料问答 · 已回答</span></div>'
      : role === 'assistant' ? `<div class="v2-message-meta"><span>${escapeHtml(intentLabel(intent))}</span>${task ? `<span>${escapeHtml(task)}</span>` : ''}${turn && turn.context && turn.context.follow_up ? '<span>已理解为追问</span>' : ''}${evidenceStatus && evidenceStatus !== 'not_required' ? `<span>证据：${escapeHtml(conclusionVerificationLabel(evidenceStatus, turn))}</span>` : ''}</div>` : '';
    const content = role === 'assistant' ? safeMarkdown(assistantDisplayText(message.content)) : `<p>${escapeHtml(message.content || '')}</p>`;
    const retryable = turn && ['failed', 'cancelled'].includes(turn.status);
    const quality = role === 'assistant' && turn && !simpleFileSearch ? analysisQualityMarkup(turn) : '';
    const warnings = role === 'assistant' && turn && !simpleFileSearch && Array.isArray(turn.warnings) && turn.warnings.length
      ? `<section class="v2-turn-warnings"><strong>范围与核验限制</strong><ul>${turn.warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul></section>`
      : '';
    const globalLargeQA = inputManifest && inputManifest.large_qa && inputManifest.large_qa.global_mode ? inputManifest.large_qa : null;
    let manifestText = '';
    if (inputManifest && globalLargeQA) {
      const scopeFiles = globalLargeQA.scope_files != null ? globalLargeQA.scope_files : globalLargeQA.source_files || 0;
      const complete = globalLargeQA.synthesis_complete === true && inputManifest.final_synthesis_complete !== false;
      const gaps = [];
      if (globalLargeQA.missing_index_files) gaps.push(`尚未建立索引 ${integer(globalLargeQA.missing_index_files)} 份`);
      if (globalLargeQA.files_without_content) gaps.push(`暂无可用内容 ${integer(globalLargeQA.files_without_content)} 份`);
      if (globalLargeQA.stale_files) gaps.push(`来源已变化待更新 ${integer(globalLargeQA.stale_files)} 份`);
      if (globalLargeQA.source_snapshot_current === false) gaps.push('本轮期间资料范围或内容发生变化');
      if (globalLargeQA.map_failures) gaps.push(`归并未成功 ${integer(globalLargeQA.map_failures)} 次`);
      manifestText = `<strong>${complete ? '全范围综合已完成' : '当前为阶段性综合，尚未形成完整全范围结论'}</strong><br>程序扫描 ${integer(globalLargeQA.scanned_files || 0)}/${integer(scopeFiles)} 份文件；模型归并 ${integer(globalLargeQA.model_mapped_files || 0)}/${integer(scopeFiles)} 份文件，已完成 ${integer(globalLargeQA.map_processed_batches || 0)}/${integer(globalLargeQA.map_batches || 0)} 批。<br>回答引用 ${integer(inputManifest.selected_files || 0)} 份文件的 ${integer(inputManifest.selected_evidence || 0)} 个内容片段，共约 ${integer(inputManifest.selected_characters || 0)} 字；引用数量不代表扫描范围。${inputManifest.total_notes != null ? ` 本轮保留 ${integer(inputManifest.final_synthesis_notes || 0)}/${integer(inputManifest.total_notes)} 条综合要点。` : ''}<br>扫描依据已有摘要及正文索引，不等于逐份全文阅读。${gaps.length ? `<br>资料缺口：${gaps.join('；')}。` : ''}`;
    } else if (inputManifest) {
      manifestText = `实际纳入 ${integer(inputManifest.selected_files || 0)} 个文件、${integer(inputManifest.selected_evidence || 0)} 个内容片段，共约 ${integer(inputManifest.selected_characters || 0)} 字。${inputManifest.package_reading && inputManifest.package_reading.files_read != null ? ` 全包阅读层已处理 ${integer(inputManifest.package_reading.summary_backed_files || 0)}/${integer(inputManifest.package_reading.files_read)} 份摘要。` : ''}${inputManifest.deferred_files && inputManifest.deferred_files.length ? `仍有 ${integer(inputManifest.deferred_files.length)} 个候选文件未纳入本轮。` : ''}`;
    }
    const manifest = role === 'assistant' && inputManifest && !simpleFileSearch
      ? `<details class="v2-input-manifest"><summary>查看本次实际分析范围</summary><div>${manifestText}</div></details>`
      : '';
    const steps = !simpleFileSearch && turn && Array.isArray(turn.steps) && turn.steps.length ? `<details class="v2-analysis-steps"><summary>查看分析步骤</summary>${turn.steps.map((step) => `<div class="v2-analysis-step ${escapeHtml(step.status || 'pending')}"><span>${escapeHtml(step.action || step.tool || '')}</span><b>${escapeHtml(analysisTurnStatusLabel(step.status))}</b></div>`).join('')}</details>` : '';
    const continueDeep = turn && turn.status === 'completed' && turn.promotion_limit_reached
      ? `<button type="button" data-continue-deep-turn="${escapeHtml(turn.turn_id || '')}">继续深析</button>`
      : '';
    const actions = role === 'assistant' ? `<div class="v2-message-actions"><button type="button" data-copy-message="${escapeHtml(message.message_id || '')}">复制</button>${running ? `<button type="button" data-cancel-turn="${escapeHtml(turn.turn_id || '')}">停止分析</button>` : ''}${continueDeep}${retryable ? `<button type="button" data-retry-turn="${escapeHtml(turn.turn_id || '')}">重新分析</button>` : `<button type="button" data-regenerate-message="${escapeHtml(message.message_id || '')}">再次回答</button>`}</div>` : '';
    return `<div class="v2-message ${role}" data-message-id="${escapeHtml(message.message_id || '')}"><div class="v2-message-card"><div class="v2-message-content">${content}</div>${progress}${meta}${quality}${warnings}${manifest}${steps}${evidence}${fileCards}${actions}</div></div>`;
  }

  function renderConversation() {
    const session = state.conversation;
    const generalMode = Boolean(session && !chatScanId());
    const indexBlocked = Boolean(
      session && state.searchIndex && state.searchIndex.usable === false
    );
    const indexPartial = Boolean(
      session && state.searchIndex && state.searchIndex.usable !== false
      && !state.searchIndex.ready
    );
    const suggestions = $('conversationSuggestions');
    if (suggestions) {
      const hidden = !session;
      const examples = generalMode
        ? ['帮我把这段话写得更自然', '给我想几个可执行的方案', '解释一个我不熟悉的概念']
        : ['概括这批资料并列出关键证据', '找出资料中的矛盾、例外和风险', '建立主要人物、机构与事件的时间线'];
      const expected = examples.map(escapeHtml).join('|');
      if (suggestions.dataset.examples !== expected) {
        suggestions.innerHTML = examples.map((item) => `<button type="button">${escapeHtml(item)}</button>`).join('');
        suggestions.dataset.examples = expected;
      }
      suggestions.hidden = hidden;
      suggestions.setAttribute('aria-hidden', String(hidden));
      suggestions.querySelectorAll('button').forEach((button) => { button.disabled = hidden || state.conversationSending || state.generalChatSending; });
    }
    $('conversationTitle').textContent = session ? (generalMode ? '通用聊天' : (session.title || '资料问答')) : '尚未开始对话';
    $('conversationScopeSummary').textContent = session ? (generalMode ? '通用聊天 · 导入数据包后可切换资料证据模式' : scopeSummary(session.scope)) : '请选择范围并新建会话';
    const contextPicker = document.querySelector('.v2-context-picker');
    if (contextPicker) contextPicker.hidden = generalMode;
    updateContextChip(generalMode ? { kind: 'general' } : undefined);
    const messages = session && Array.isArray(session.messages) ? session.messages : [];
    const older = session && session.message_page && session.message_page.has_more
      ? '<button type="button" class="text-button v2-load-older" data-load-older-messages>加载更早消息</button>'
      : '';
    $('conversationMessages').setAttribute('aria-busy', String(state.conversationLoading));
    $('conversationMessages').innerHTML = state.conversationLoading
      ? emptyMarkup('正在打开会话', '正在读取历史消息、分析进度和可引用证据。')
      : (messages.length ? older + messages.map(messageMarkup).join('') : emptyMarkup(
        generalMode ? '问我任何问题' : '从资料中提出第一个问题',
        generalMode ? '我会先理解你的目标，再给出自然回答；导入资料包后还能补充原文证据。' : '例如：这批资料主要讲了什么？哪些文件相互关联？'
      ));
    $('conversationQuestion').disabled = !session || state.conversationSending || state.generalChatSending || state.conversationLoading || (!generalMode && indexBlocked);
    $('conversationSendBtn').disabled = !session || state.conversationSending || state.generalChatSending || state.conversationLoading || (!generalMode && indexBlocked);
    $('conversationNewBtn').disabled = isLargeChatSource() && !chatScanId();
    $('conversationComposerHint').textContent = indexBlocked
      ? '当前没有可检索文本；请等待基础解析或重建索引'
      : indexPartial
        ? '当前可基于已索引资料作答；回答会标注覆盖范围，完整索引仍可继续重建'
      : (generalMode ? '通用聊天模式 · 回答不引用当前资料；导入数据包后可在同一界面追溯证据'
        : (session ? '资料事实优先引用；分析判断会单独标明' : '新建会话后即可提问'));
    if (!session && $('conversationDeepeningState')) {
      $('conversationDeepeningState').className = 'v2-job-state';
      $('conversationDeepeningState').textContent = '';
    }
    if (isLargeChatSource() && state.searchIndex) {
      const host = $('conversationDeepeningState');
      host.className = `v2-job-state ${indexBlocked ? 'is-error' : 'is-complete'}`;
      host.textContent = `大数据包文件摘要：${integer(state.searchIndex.indexed_files)}/${integer(state.searchIndex.total_files)} 份可用${indexBlocked ? ' · 请等待大包基础摘要完成' : ' · 证据定位到文件'}`;
    } else if (indexBlocked) {
      const host = $('conversationDeepeningState');
      host.className = 'v2-job-state is-error';
      host.innerHTML = `当前没有可检索文本 · ${integer(state.searchIndex.documents)} 份已保存文档 <button type="button" data-rebuild-search-index>重建索引</button>`;
    } else if (indexPartial) {
      const host = $('conversationDeepeningState');
      host.className = 'v2-job-state is-running';
      host.innerHTML = `阶段性索引可用 · ${integer(state.searchIndex.evidence_records)} 条证据 · ${integer(state.searchIndex.processed_documents)}/${integer(state.searchIndex.expected_documents)} 份文档已入索引 <button type="button" data-rebuild-search-index>继续重建</button>`;
    }
    renderConversationList();
    window.requestAnimationFrame(() => { $('conversationMessages').scrollTop = $('conversationMessages').scrollHeight; });
  }

  async function loadOlderConversationMessages() {
    const session = state.conversation;
    const page = session && session.message_page;
    if (!session || !page || !page.before_sequence) return;
    const sourceVersion = state.chatSourceVersion;
    const contextVersion = state.conversationContextVersion;
    try {
      const response = await api(`/api/conversation/${encodeURIComponent(session.session_id)}?scan_id=${encodeURIComponent(chatScanId())}&message_limit=100&before_sequence=${encodeURIComponent(page.before_sequence)}`);
      if (sourceVersion !== state.chatSourceVersion || contextVersion !== state.conversationContextVersion || state.conversation !== session) return;
      const older = response.session || {};
      const currentMessages = Array.isArray(session.messages) ? session.messages : [];
      const olderMessages = Array.isArray(older.messages) ? older.messages : [];
      state.conversation = { ...session, ...older, messages: olderMessages.concat(currentMessages) };
      renderConversation();
    } catch (error) { notify(error.message || '加载历史消息失败', true); }
  }

  async function createConversation() {
    if (isLargeChatSource() && !chatScanId()) { notify('请先选择大数据包', true); return; }
    if (!chatScanId()) {
      beginConversationTransition();
      state.generalChatMessages = [];
      state.conversation = null;
      ensureGeneralSession();
      state.turns.clear();
      renderConversation();
      $('conversationQuestion').focus();
      return;
    }
    let scope;
    try { scope = conversationScope(); } catch (error) { notify(error.message, true); return; }
    const button = $('conversationNewBtn');
    const requestSeq = beginConversationTransition();
    button.disabled = true;
    try {
      const response = await api('/api/conversations', {
        method: 'POST', body: JSON.stringify({ scan_id: chatScanId(), ...chatSourcePayload(), scope, title: `对话 · ${new Date().toLocaleString('zh-CN', { hour12: false })}` })
      });
      if (requestSeq !== state.conversationRequestSeq) return;
      state.conversation = response.session;
      state.searchIndex = response.search_index || null;
      state.turns.clear();
      renderConversation();
      await loadConversationList();
      $('conversationQuestion').focus();
    } catch (error) {
      notify(error.message || '新建会话失败', true);
    } finally { if (requestSeq === state.conversationRequestSeq) button.disabled = isLargeChatSource() && !chatScanId(); }
  }

  async function deleteConversation(sessionId, button) {
    if (!sessionId) return;
    if (button) button.disabled = true;
    try {
      await api(`/api/conversation/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
      if (state.conversation && state.conversation.session_id === sessionId) {
        state.conversation = null;
        state.turns = new Map();
        renderConversation();
      }
      await loadConversationList();
      notify('会话已删除');
    } catch (error) {
      if (button) button.disabled = false;
      notify(error && error.message ? error.message : '删除会话失败', true);
    }
  }

  function restoreConversationScope(scope) {
    scope = scope || { kind: 'package' };
    state.scopeConstraints = { ...(scope.constraints || {}), source_paths: scope.source_paths || [] };
    syncScopeOptions($('conversationScopeKind'));
    $('conversationScopeKind').value = scope.kind || 'package';
    if (!$('conversationScopeKind').value) $('conversationScopeKind').value = 'package';
    renderScopeFields();
    if (scope.kind === 'time') {
      $('conversationScopeStart').value = (scope.value || {}).start || '';
      $('conversationScopeEnd').value = (scope.value || {}).end || '';
    } else if ($('conversationScopeValue')) {
      $('conversationScopeValue').value = scope.kind === 'files'
        ? (scope.source_paths || scope.value || []).join('\n') : (scope.value || '');
    }
    updateContextChip(scope);
  }

  async function openConversation(sessionId) {
    if (!chatScanId() || !sessionId) return;
    const requestSeq = beginConversationTransition();
    state.conversationLoading = true;
    renderConversation();
    try {
      const response = await api(`/api/conversation/${encodeURIComponent(sessionId)}?scan_id=${encodeURIComponent(chatScanId())}`);
      if (requestSeq !== state.conversationRequestSeq) return;
      state.conversation = response.session;
      restoreConversationScope((response.session || {}).scope);
      state.searchIndex = response.search_index || null;
      state.turns.clear();
      registerAnalysisTurns(response.turns || []);
      state.conversationLoading = false;
      renderConversation();
      if (requestSeq !== state.conversationRequestSeq) return;
      (response.turns || []).filter((turn) => ['queued', 'running', 'waiting_for_deep_analysis'].includes(turn.status)).forEach((turn) => watchAnalysisTurn(turn.id));
    } catch (error) { notify(error.message || '无法打开会话', true); }
    finally { if (requestSeq === state.conversationRequestSeq) { state.conversationLoading = false; renderConversation(); } }
  }

  function jobStatusLabel(job) {
    return { queued: '已排队', running: '正在运行', cancelling: '正在取消', completed: '已完成', failed: '失败', cancelled: '已取消' }[job.status] || job.status || '未知';
  }

  async function watchJob(jobId, key, callback, isCurrent) {
    if (!jobId) return;
    const token = `${Date.now()}-${Math.random()}`;
    const requestedScanId = state.scanId;
    const requestIsCurrent = isCurrent || (() => state.scanId === requestedScanId);
    state.watchers.set(key, token);
    for (let attempt = 0; attempt < 240 && state.watchers.get(key) === token; attempt += 1) {
      try {
        const response = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        const job = response.job || response;
        if (!requestIsCurrent() || state.watchers.get(key) !== token) return null;
        callback(job, false);
        if (['completed', 'failed', 'cancelled'].includes(job.status)) {
          state.watchers.delete(key);
          return job;
        }
      } catch (error) {
        if (!requestIsCurrent() || state.watchers.get(key) !== token) return null;
        callback({ status: 'connection_error', error: error.message }, false);
      }
      await wait(3000);
    }
    if (requestIsCurrent() && state.watchers.get(key) === token) {
      state.watchers.delete(key);
      callback({ status: 'background', message: '后台任务仍在继续，可在任务中心查看。' }, true);
    }
    return null;
  }

  function watchPromotion(jobId) {
    const host = $('conversationDeepeningState');
    host.className = 'v2-job-state is-running';
    host.textContent = '证据不足，已提交补充深析。';
    watchJob(jobId, 'conversation-promotion', (job) => {
      const progress = job.progress == null ? '' : ` · ${Math.round(numeric(job.progress))}%`;
      if (job.status === 'completed') {
        host.className = 'v2-job-state is-complete';
        host.textContent = '补充深析已完成，系统已自动重新检索并续写回答。';
        if (state.conversation && state.conversation.session_id) {
          openConversation(state.conversation.session_id);
        }
      } else if (['failed', 'cancelled'].includes(job.status)) {
        host.className = 'v2-job-state is-error';
        host.textContent = `补充深析${jobStatusLabel(job)}${job.error ? `：${job.error}` : ''}`;
      } else if (job.status === 'background') {
        host.className = 'v2-job-state is-running';
        host.textContent = job.message;
      } else {
        host.className = 'v2-job-state is-running';
        host.textContent = `补充深析${jobStatusLabel(job)}${progress}${job.message ? ` · ${job.message}` : ''}`;
      }
    });
  }

  function watchAnalysisTurn(turnId) {
    if (!turnId) return;
    const key = `analysis-turn-${turnId}`;
    const token = `${Date.now()}-${Math.random()}`;
    const requestedScanId = chatScanId();
    const sourceVersion = state.chatSourceVersion;
    const contextVersion = state.conversationContextVersion;
    const requestedSessionId = state.conversation && state.conversation.session_id;
    const isCurrent = () => sourceVersion === state.chatSourceVersion
      && contextVersion === state.conversationContextVersion
      && chatScanId() === requestedScanId
      && state.conversation && state.conversation.session_id === requestedSessionId
      && state.watchers.get(key) === token;
    let afterEventId = 0;
    let needsSnapshot = true;
    let lastSnapshotAt = 0;
    let latestEvent = null;
    state.watchers.set(key, token);
    const host = $('conversationDeepeningState');
    (async () => {
      for (let attempt = 0; attempt < 2400 && state.watchers.get(key) === token; attempt += 1) {
        try {
          // Events are the lightweight progress channel. The full turn/session
          // snapshot also refreshes periodically, so a worker timeout or other
          // terminal change without a new event cannot leave a running card.
          const eventResponse = await api(`/api/turns/${encodeURIComponent(turnId)}/events?after=${afterEventId}`);
          if (!isCurrent()) return;
          const events = Array.isArray(eventResponse.items) ? eventResponse.items : [];
          afterEventId = numeric(eventResponse.next_after == null ? afterEventId : eventResponse.next_after);
          if (events.length) {
            const latest = events[events.length - 1] || {};
            latestEvent = latest;
            const progress = latest.progress == null ? '' : ` · ${Math.round(numeric(latest.progress))}%`;
            host.className = 'v2-job-state is-running';
            host.textContent = `${analysisTurnStageLabel(latest.stage)}${progress}${latest.message ? ` · ${latest.message}` : ''}`;
          }
          if (needsSnapshot || events.length || Date.now() - lastSnapshotAt >= 8000) {
            const response = await api(`/api/turns/${encodeURIComponent(turnId)}`);
            if (!isCurrent()) return;
            const turn = response.turn || {};
            lastSnapshotAt = Date.now();
            if (response.session && response.session.session_id === requestedSessionId) state.conversation = response.session;
            if (!turn.message && latestEvent && latestEvent.stage === turn.stage
                && numeric(latestEvent.progress) === numeric(turn.progress)) {
              turn.message = latestEvent.message || '';
            }
            registerAnalysisTurn(turn, response.steps || []);
            const progress = Math.round(numeric(turn.progress));
            if (['queued', 'running', 'waiting_for_deep_analysis'].includes(turn.status)) {
              const registered = state.turns.get(turn.assistant_message_id) || {};
              const currentMessage = registered.progress_message || '';
              host.className = 'v2-job-state is-running';
              host.textContent = `${analysisTurnStageLabel(turn.stage)} · ${progress}%${currentMessage ? ` · ${currentMessage}` : ''}`;
            } else if (turn.status === 'completed') {
              host.className = 'v2-job-state is-complete';
              host.textContent = '分析完成，结论与引用已保存。';
            } else {
              host.className = 'v2-job-state is-error';
              host.textContent = `${analysisTurnStatusLabel(turn.status)}${turn.error ? `：${turn.error}` : ''}`;
            }
            renderConversation();
            needsSnapshot = false;
            if (['completed', 'failed', 'cancelled'].includes(turn.status)) {
              state.watchers.delete(key);
              await loadConversationList();
              return;
            }
          }
        } catch (error) {
          if (!isCurrent()) return;
          host.className = 'v2-job-state is-error';
          host.textContent = `分析状态暂时无法读取：${error.message}`;
          if ([401, 403, 404, 410].includes(error.status)) {
            state.watchers.delete(key);
            return;
          }
          needsSnapshot = true;
        }
        await wait(needsSnapshot ? 2000 : 1500);
      }
    })();
  }

  async function cancelAnalysisTurn(turnId) {
    if (!turnId) return;
    try {
      const response = await api(`/api/turns/${encodeURIComponent(turnId)}/cancel`, {
        method: 'POST', body: '{}'
      });
      registerAnalysisTurn(response.turn, []);
      if (state.conversation) await openConversation(state.conversation.session_id);
    } catch (error) { notify(error.message || '停止分析失败', true); }
  }

  async function retryAnalysisTurn(turnId) {
    if (!turnId) return;
    try {
      const response = await api(`/api/turns/${encodeURIComponent(turnId)}/retry`, {
        method: 'POST', body: '{}'
      });
      registerAnalysisTurn(response.turn, []);
      if (state.conversation) await openConversation(state.conversation.session_id);
      watchAnalysisTurn(turnId);
    } catch (error) { notify(error.message || '重新分析失败', true); }
  }

  async function continueDeepAnalysis(turnId) {
    if (!turnId) return;
    try {
      const response = await api(`/api/turns/${encodeURIComponent(turnId)}/continue-deep-analysis`, {
        method: 'POST', body: JSON.stringify({ desired_file_count: 12 })
      });
      registerAnalysisTurn(response.turn, []);
      renderConversation();
      $('conversationDeepeningState').className = 'v2-job-state is-running';
      $('conversationDeepeningState').textContent = `已继续深析 ${integer((response.candidate_paths || []).length)} 份候选文件`;
      watchAnalysisTurn(turnId);
    } catch (error) { notify(error.message || '继续深析失败', true); }
  }

  async function rebuildConversationSearchIndex() {
    if (!chatScanId() || isLargeChatSource()) return;
    try {
      const response = await api(`/api/scans/${encodeURIComponent(chatScanId())}/rebuild-search-index`, {
        method: 'POST', body: '{}'
      });
      const host = $('conversationDeepeningState');
      host.className = 'v2-job-state is-running';
      host.textContent = response.created ? '索引重建任务已进入队列' : '索引重建任务正在运行';
      watchJob(response.job_id, `search-index-${chatScanId()}`, (job) => {
        const progress = job.progress == null ? '' : ` · ${Math.round(numeric(job.progress))}%`;
        if (job.status === 'completed') {
          host.className = 'v2-job-state is-complete';
          host.textContent = '对话索引重建完成。';
          if (state.conversation) openConversation(state.conversation.session_id);
        } else if (['failed', 'cancelled'].includes(job.status)) {
          host.className = 'v2-job-state is-error';
          host.textContent = `索引重建${jobStatusLabel(job)}${job.error ? `：${job.error}` : ''}`;
        } else {
          host.className = 'v2-job-state is-running';
          host.textContent = `索引重建${jobStatusLabel(job)}${progress}`;
        }
      });
    } catch (error) { notify(error.message || '提交索引重建失败', true); }
  }

  function fileSearchQuery(text) {
    const original = String(text || '').trim();
    let query = original
      .replace(/^(?:请|帮我|麻烦|给我|可以)?\s*/u, '')
      .replace(/^(?:搜索|查找|找一下|帮我找|找到|找出|列出|显示|搜索一下)\s*/u, '')
      .replace(/^(?:有关|关于|包含|命中)\s*/u, '')
      .replace(/(?:的)?(?:文件|资料|文档)(?:列表|清单)?[。！？!?]*$/u, '')
      .trim();
    return query || original;
  }

  async function downloadOriginalFile(path) {
    if (!chatScanId() || !path) return;
    try {
      const endpoint = String(chatScanId()).startsWith('lp-')
        ? `/api/large-packages/${encodeURIComponent(chatScanId())}/file?path=${encodeURIComponent(path)}&download=1`
        : `/api/document/${encodeURIComponent(chatScanId())}/raw?path=${encodeURIComponent(path)}&download=1`;
      const response = await window.SJFXAuth.request(
        endpoint,
        { headers: { Accept: '*/*' } },
      );
      if (!response.ok) throw new Error('原文件下载失败');
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = basename(path);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (error) { notify(error.message || '原文件下载失败', true); }
  }

  async function openOriginalFile(path) {
    const scanId = chatScanId();
    const modal = $('fileReaderModal');
    const content = $('fileReaderContent');
    if (!scanId || !path || !modal || !content) return;
    const returnFocus = modal.hidden ? document.activeElement : fileReaderReturnFocus;
    closeOriginalFile(false);
    fileReaderReturnFocus = returnFocus;
    const requestSeq = state.fileReaderRequestSeq;
    const sourceVersion = state.chatSourceVersion;
    const isCurrent = () => requestSeq === state.fileReaderRequestSeq && !modal.hidden
      && scanId === chatScanId() && sourceVersion === state.chatSourceVersion;
    modal.dataset.path = path;
    modal.hidden = false;
    $('fileReaderTitle').textContent = basename(path);
    $('fileReaderMeta').textContent = path;
    $('fileReaderState').textContent = '正在加载原文…';
    $('fileReaderDownload').disabled = true;
    $('fileReaderDeepSummary').disabled = true;
    $('fileReaderDeepSummary').textContent = '生成深度摘要';
    $('fileReaderClose')?.focus();
    try {
      const endpoint = String(scanId).startsWith('lp-')
        ? `/api/large-packages/${encodeURIComponent(scanId)}/file?path=${encodeURIComponent(path)}`
        : `/api/document/${encodeURIComponent(scanId)}/raw?path=${encodeURIComponent(path)}`;
      const response = await window.SJFXAuth.request(
        endpoint,
        { headers: { Accept: '*/*' } },
      );
      if (!isCurrent()) return;
      if (!response.ok) throw new Error('原文件读取失败');
      const type = String(response.headers.get('content-type') || '').toLowerCase();
      const blob = await response.blob();
      if (!isCurrent()) return;
      const textType = type.startsWith('text/') || type.includes('json') || type.includes('xml') || type.includes('csv');
      const text = textType ? await blob.text() : '';
      if (!isCurrent()) return;
      const fileUrl = URL.createObjectURL(blob);
      modal.dataset.objectUrl = fileUrl;
      $('fileReaderDownload').disabled = false;
      $('fileReaderDeepSummary').disabled = false;
      if (type.includes('pdf')) {
        const frame = document.createElement('iframe');
        frame.src = fileUrl;
        frame.title = basename(path) + ' · PDF 原文';
        frame.style.cssText = 'width:100%;height:100%;min-height:480px;border:0;display:block';
        content.appendChild(frame);
        $('fileReaderState').textContent = 'PDF 原文已载入；若浏览器无法显示，请下载原文件查看。';
      } else if (type.startsWith('image/')) {
        const image = document.createElement('img');
        image.src = fileUrl;
        image.alt = basename(path);
        image.style.cssText = 'max-width:100%;height:auto;display:block;margin:0 auto';
        image.onerror = () => { if (isCurrent()) $('fileReaderState').textContent = '浏览器无法显示此图片，请下载原文件查看。'; };
        content.appendChild(image);
        $('fileReaderState').textContent = '图片原文已载入。';
      } else if (textType) {
        // HTML/XML source is shown as literal text, never interpreted as markup.
        content.textContent = text;
        $('fileReaderState').textContent = '原文已载入。';
      } else {
        content.textContent = `此格式（${type || '未知'}）无法在浏览器内直接展示，请下载原文件查看。`;
        $('fileReaderState').textContent = '原文件已载入，可下载查看。';
      }
    } catch (error) {
      if (!isCurrent()) return;
      content.textContent = '';
      if (modal.dataset.objectUrl) URL.revokeObjectURL(modal.dataset.objectUrl);
      modal.dataset.objectUrl = '';
      $('fileReaderDownload').disabled = true;
      $('fileReaderDeepSummary').disabled = true;
      $('fileReaderState').textContent = error.message || '原文读取失败';
    }
  }

  function closeOriginalFile(restoreFocus = true) {
    state.fileReaderRequestSeq += 1;
    const modal = $('fileReaderModal');
    if (!modal) return;
    const wasOpen = !modal.hidden;
    if ($('fileReaderContent')) $('fileReaderContent').textContent = '';
    if (modal.dataset.objectUrl) URL.revokeObjectURL(modal.dataset.objectUrl);
    modal.dataset.objectUrl = '';
    modal.dataset.path = '';
    modal.hidden = true;
    if (restoreFocus && wasOpen && fileReaderReturnFocus?.isConnected) fileReaderReturnFocus.focus();
    fileReaderReturnFocus = null;
  }

  function downloadReaderOriginal() {
    const modal = $('fileReaderModal');
    if (!modal || modal.hidden || !modal.dataset.objectUrl) return;
    const anchor = document.createElement('a');
    anchor.href = modal.dataset.objectUrl;
    anchor.download = basename(modal.dataset.path);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  async function queueFileDeepSummary(path, button) {
    if (!chatScanId() || !path) return;
    const original = button && button.textContent;
    if (button) { button.disabled = true; button.textContent = '已提交…'; }
    try {
      const largeSource = isLargeChatSource();
      const result = await api(
        largeSource ? `/api/large-packages/${encodeURIComponent(chatScanId())}/select` : '/api/summary',
        {
          method: 'POST',
          body: JSON.stringify(largeSource
            ? { paths: [path] }
            : { scan_id: chatScanId(), path, kind: 'file', force: true }),
        }
      );
      notify(result.accepted ? '文件已提交模型深度摘要' : '文件深度摘要已完成');
      if (result.job_id) watchJob(result.job_id, `file-summary-${path}`);
      if (button) button.textContent = result.job_id ? '分析中' : '已完成';
    } catch (error) {
      if (button) { button.disabled = false; button.textContent = original || '深度摘要'; }
      notify(error.message || '深度摘要提交失败', true);
    }
  }

  function ensureFolderDock() {
    let dock = document.getElementById('v2FolderDock');
    if (dock) return dock;
    dock = document.createElement('section');
    dock.id = 'v2FolderDock';
    dock.className = 'v2-folder-dock';
    dock.hidden = true;
    dock.innerHTML = `<header><button type="button" class="v2-folder-toggle" data-folder-toggle aria-expanded="false"><strong>资料夹</strong><span data-folder-count>0 个文件</span></button><div class="v2-folder-dock-actions"><button type="button" data-folder-export>导出资料夹</button><button type="button" data-folder-clear>清空</button></div></header><ul data-folder-list hidden></ul>`;
    dock.addEventListener('click', (event) => {
      if (event.target.closest('[data-folder-toggle]')) {
        const list = dock.querySelector('[data-folder-list]');
        list.hidden = !list.hidden;
        dock.querySelector('[data-folder-toggle]').setAttribute('aria-expanded', String(!list.hidden));
        return;
      }
      const remove = event.target.closest('[data-folder-remove]');
      if (remove) {
        state.fileFolder.delete(remove.dataset.folderRemove || '');
        try { sessionStorage.setItem('sjfx_file_folder_v1', JSON.stringify([...state.fileFolder])); } catch (_) {}
        renderFolderDock();
        return;
      }
      if (event.target.closest('[data-folder-clear]')) {
        state.fileFolder.clear();
        try { sessionStorage.setItem('sjfx_file_folder_v1', '[]'); } catch (_) {}
        renderFolderDock();
        notify('资料夹已清空');
        return;
      }
      if (event.target.closest('[data-folder-export]')) { exportCollectedFolder(); return; }
    });
    document.body.appendChild(dock);
    return dock;
  }

  function renderFolderDock() {
    state.fileFolder = state.fileFolder || new Set();
    const dock = ensureFolderDock();
    const count = state.fileFolder.size;
    dock.hidden = count === 0;
    dock.querySelector('[data-folder-count]').textContent = `${count} 个文件`;
    dock.querySelector('[data-folder-list]').innerHTML = [...state.fileFolder]
      .map((path) => `<li><span title="${escapeHtml(path)}">${escapeHtml(basename(path))}</span><button type="button" data-folder-remove="${escapeHtml(path)}" aria-label="从资料夹移除 ${escapeHtml(basename(path))}">×</button></li>`)
      .join('');
    // 卡片上的"加入资料夹"按钮与资料夹内容保持一致（例如清空后恢复可点击）。
    document.querySelectorAll('[data-file-collect],[data-citation-collect]').forEach((button) => {
      const path = button.dataset.fileCollect || button.dataset.citationCollect || '';
      if (path && !state.fileFolder.has(path) && button.disabled) {
        button.disabled = false;
        button.textContent = button.hasAttribute('data-citation-collect') ? '收入资料夹' : '加入资料夹';
      }
    });
  }

  function collectFileForFolder(path, button) {
    if (!path) return;
    state.fileFolder = state.fileFolder || new Set();
    state.fileFolder.add(path);
    try { sessionStorage.setItem('sjfx_file_folder_v1', JSON.stringify([...state.fileFolder])); } catch (_) {}
    if (button) { button.textContent = '已加入资料夹'; button.disabled = true; }
    renderFolderDock();
    notify(`已加入资料夹，共 ${state.fileFolder.size} 个文件；面板在页面右下角`);
  }

  async function downloadOutputFile(filename, message) {
    if (!filename) return;
    try {
      const ticket = await api('/api/download-ticket', {
        method: 'POST', body: JSON.stringify({ filename }),
      });
      const anchor = document.createElement('a');
      anchor.href = ticket.download_url;
      anchor.style.display = 'none';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      notify(message || '文件已开始下载');
    } catch (error) { notify(error.message || '下载失败', true); }
  }

  async function exportCollectedFolder() {
    state.fileFolder = state.fileFolder || new Set();
    if (!state.fileFolder.size || !chatScanId()) { notify('资料夹还是空的，请先加入文件', true); return; }
    // The ordinary export pipeline reads ordinary scan tables only.
    if (isLargeChatSource()) { notify('大数据包暂不支持资料夹打包导出，请使用大包报告的下载入口。', true); return; }
    try {
      const response = await api('/api/export', {
        method: 'POST',
        body: JSON.stringify({
          scan_id: chatScanId(),
          task_topic: '搜索结果资料夹',
          selections: [...state.fileFolder].map((path) => ({ path, kind: 'file', name: basename(path) })),
        }),
      });
      notify('资料夹打包中，完成后自动下载');
      if (response.job_id) {
        watchJob(response.job_id, 'file-folder-export', (job) => {
          if (job.status === 'completed') {
            const result = job.result && typeof job.result === 'object' ? job.result : {};
            if (result.file_name) downloadOutputFile(result.file_name, '资料夹打包完成，已开始下载');
            else notify('资料夹打包完成');
          } else if (job.status === 'failed') {
            notify(job.error || '资料夹打包失败', true);
          }
        });
      }
    } catch (error) { notify(error.message || '资料夹生成失败', true); }
  }

  function askNodeName(paths) {
    if (window.__sjfxNodeDialogPromise) return window.__sjfxNodeDialogPromise;
    window.__sjfxNodeDialogPromise = new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'v2-node-dialog';
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');
      overlay.innerHTML = `<div class="v2-node-dialog-panel"><span class="section-kicker">ORGANIZE MATERIALS</span><h3>创建资料节点</h3><p>已选择 ${paths.length} 个文件。为它们命名一个新节点，之后可在智能目录中继续编辑。</p><label for="v2NodeName">节点名称</label><input id="v2NodeName" maxlength="120" value="待整理资料" autocomplete="off"><div class="v2-node-dialog-actions"><button type="button" data-node-cancel>取消</button><button type="button" class="primary" data-node-submit>创建节点</button></div></div>`;
      document.body.appendChild(overlay);
      const input = overlay.querySelector('#v2NodeName');
      const finish = (value) => { overlay.remove(); window.__sjfxNodeDialogPromise = null; resolve(value); };
      overlay.addEventListener('click', (event) => {
        if (event.target === overlay || event.target.closest('[data-node-cancel]')) finish('');
        if (event.target.closest('[data-node-submit]')) finish((input.value || '').trim());
      });
      input.addEventListener('keydown', (event) => { if (event.key === 'Enter') finish((input.value || '').trim()); if (event.key === 'Escape') finish(''); });
      window.setTimeout(() => { input.focus(); input.select(); }, 0);
    });
    return window.__sjfxNodeDialogPromise;
  }

  async function createNodeFromFiles(paths, button) {
    const selected = [...new Set((paths || []).filter(Boolean))];
    if (!chatScanId() || !selected.length) { notify('请先选择要整理的文件', true); return; }
    const name = await askNodeName(selected);
    if (!name) return;
    if (button) { button.disabled = true; button.textContent = '创建中…'; }
    try {
      const endpoint = String(chatScanId()).startsWith('lp-')
        ? `/api/large-packages/${encodeURIComponent(chatScanId())}/nodes`
        : `/api/tree-edits/${encodeURIComponent(chatScanId())}`;
      const body = String(chatScanId()).startsWith('lp-')
        ? { name, paths: selected }
        : { operation: 'create', payload: { name, paths: selected } };
      const response = await api(endpoint, { method: 'POST', body: JSON.stringify(body) });
      if (response.analysis) window.dispatchEvent(new CustomEvent('sjfx:analysis-updated', { detail: response.analysis }));
      notify(`已创建节点“${name}”，并归入 ${selected.length} 个文件`);
      document.querySelectorAll('[data-file-select]').forEach((item) => { item.checked = false; });
      updateFileSelectionActions();
      if (button) button.textContent = '已创建节点';
    } catch (error) {
      if (button) { button.disabled = false; button.textContent = '选中后创建节点'; }
      notify(error.message || '创建节点失败', true);
    }
  }

  function updateFileSelectionActions() {
    const selected = [...document.querySelectorAll('[data-file-select]:checked')];
    const label = selected.length ? `已选择 ${selected.length} 个文件` : '未选择文件';
    document.querySelectorAll('[data-create-node-selected]').forEach((button) => {
      button.disabled = selected.length === 0;
      button.textContent = selected.length ? `把 ${selected.length} 个文件并成新节点` : '选中后创建节点';
      button.setAttribute('aria-label', selected.length ? `用已选择的 ${selected.length} 个文件创建节点` : '请先选择文件');
    });
    document.querySelectorAll('[data-file-selection-count]').forEach((item) => { item.textContent = label; });
  }

  async function createNodeFromFile(path, button) {
    return createNodeFromFiles([path], button);
  }

  async function loadFileSummaries() {
    if (!chatScanId()) return {};
    const requestedScanId = chatScanId();
    const sourceVersion = state.chatSourceVersion;
    if (state.fileSummaries && state.fileSummariesScanId === chatScanId()) return state.fileSummaries;
    try {
      const result = await api(`/api/file-summaries/${encodeURIComponent(requestedScanId)}`);
      if (sourceVersion !== state.chatSourceVersion || chatScanId() !== requestedScanId) return {};
      state.fileSummaries = result.summaries || {};
      state.fileSummariesScanId = requestedScanId;
    } catch (_) {
      if (sourceVersion === state.chatSourceVersion && chatScanId() === requestedScanId) {
        state.fileSummaries = state.fileSummaries || {};
      }
    }
    return state.fileSummaries;
  }

  async function attachFileResults(question, turn) {
    if (!chatScanId() || !turn || !turn.assistant_message_id) return;
    const sourceVersion = state.chatSourceVersion;
    const contextVersion = state.conversationContextVersion;
    const stored = state.turns.get(turn.assistant_message_id);
    const resultPayload = turn.result && typeof turn.result === 'object' ? turn.result : {};
    const authoritativeOrdinaryPaths = !String(chatScanId()).startsWith('lp-') && (
      Array.isArray(resultPayload.related_file_paths) || Array.isArray(turn.related_file_paths)
      || Boolean(stored && Array.isArray(stored.related_file_paths))
    );
    const relatedPaths = Array.isArray(resultPayload.related_file_paths)
      ? resultPayload.related_file_paths.filter(Boolean)
      : (authoritativeOrdinaryPaths && Array.isArray(turn.related_file_paths) ? turn.related_file_paths.filter(Boolean)
        : (stored && Array.isArray(stored.related_file_paths) ? stored.related_file_paths.filter(Boolean) : []));
    if (relatedPaths.length && stored) {
      // The backend resolves the complete related-file set.  Keep this list
      // independent from the bounded evidence window shown to the model.
      const summaries = await loadFileSummaries();
      if (sourceVersion !== state.chatSourceVersion || contextVersion !== state.conversationContextVersion) return;
      stored.related_file_paths = relatedPaths;
      stored.file_results = relatedPaths.map((path) => ({
        path, node_path: path, name: basename(path),
        extension: path.includes('.') ? path.slice(path.lastIndexOf('.')).toLowerCase() : '',
        preview: { text: summaries[path] || '' },
        analysis_level: '已归集',
      }));
      stored.file_results_total = relatedPaths.length;
      stored.file_results_coverage = null;
      const intent = stored.intent && typeof stored.intent === 'object' ? stored.intent.name : stored.intent;
      stored.file_search = intent === 'file_search';
      renderConversation();
      return;
    }
    if (authoritativeOrdinaryPaths) {
      // An empty backend result is scoped and authoritative. A package-wide
      // legacy search cannot safely replace it with unrelated file cards.
      if (stored) {
        stored.related_file_paths = [];
        stored.file_results = [];
        stored.file_results_total = 0;
        stored.file_results_coverage = null;
        renderConversation();
      }
      return;
    }
    const text = String(question || '').trim();
    const intent = turn.intent && typeof turn.intent === 'object' ? turn.intent.name : turn.intent;
    if (intent !== 'file_search') return;
    const query = fileSearchQuery(text);
    if (!query) return;
    try {
      const result = await api('/api/search/files', {
        method: 'POST',
        body: JSON.stringify({ scan_id: chatScanId(), query, page: 1, page_size: 50 })
      });
      const target = state.turns.get(turn.assistant_message_id);
      if (sourceVersion !== state.chatSourceVersion || contextVersion !== state.conversationContextVersion) return;
      if (target) {
        target.file_results = result.matched_files || [];
        target.file_results_total = result.matched_file_count || target.file_results.length;
        target.file_results_coverage = result.coverage && result.coverage.complete === false
          ? ((result.warnings || []).join('；') || '当前显示已建立索引范围内的结果')
          : null;
        target.file_search_query = query;
        target.file_search = true;
      }
    } catch (_) { /* File cards are an enhancement; the answer remains usable. */ }
  }

  async function sendQuestion(questionOverride) {
    if (!state.conversation || state.conversationSending || state.generalChatSending || (isLargeChatSource() && !chatScanId())) return;
    const sourceVersion = state.chatSourceVersion;
    const contextVersion = state.conversationContextVersion;
    const sessionId = state.conversation.session_id;
    const isCurrent = () => sourceVersion === state.chatSourceVersion
      && contextVersion === state.conversationContextVersion
      && state.conversation && state.conversation.session_id === sessionId;
    const question = String(questionOverride || $('conversationQuestion').value || '').trim();
    if (!question) { notify('请输入问题', true); return; }
    const generalMode = !chatScanId();
    let turnScope;
    if (!generalMode) {
      try { turnScope = conversationScope(); } catch (error) { notify(error.message, true); return; }
    }
    const scopeEditVersion = state.scopeEditVersion;
    if (generalMode) {
      state.generalChatSending = true;
      const history = (state.conversation.messages || []).filter((message) => message.role === 'user' || message.role === 'assistant').slice(-12);
      const optimistic = { message_id: `pending-${Date.now()}`, role: 'user', content: question };
      state.conversation.messages = (state.conversation.messages || []).concat([optimistic]);
      state.generalChatMessages = state.conversation.messages.slice();
      $('conversationQuestion').value = '';
      renderConversation();
      try {
        const response = await api('/api/general-chat', { method: 'POST', body: JSON.stringify({ question, messages: history }) });
        if (!isCurrent()) return;
        const stamp = Date.now();
        state.conversation.messages = state.conversation.messages.filter((message) => message.message_id !== optimistic.message_id).concat([
          { message_id: `general-${stamp}-user`, role: 'user', content: question },
          { message_id: `general-${stamp}-assistant`, role: 'assistant', content: response.answer || '暂时无法回答。', intent: 'general_qa', evidence_status: 'not_required' }
        ]);
        state.generalChatMessages = state.conversation.messages.slice();
        renderConversation();
      } catch (error) {
        if (!isCurrent()) return;
        state.conversation.messages = state.conversation.messages.filter((message) => message.message_id !== optimistic.message_id);
        state.generalChatMessages = state.conversation.messages.slice();
        renderConversation();
        $('conversationQuestion').value = question;
        notify(error.message || '发送失败', true);
      } finally {
        if (isCurrent()) {
          state.generalChatSending = false;
          renderConversation();
          $('conversationQuestion').focus();
        }
      }
      return;
    }
    state.conversationSending = true;
    // Keep one idempotency key for this logical send operation.  A transient
    // network failure can therefore be retried without creating a duplicate
    // conversation turn on the server.
    const pending = state.conversationPending;
    const idempotencyKey = pending && pending.question === question
      ? pending.key
      : `${state.conversation.session_id}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    state.conversationPending = { key: idempotencyKey, question };
    $('conversationQuestion').value = '';
    const optimistic = { message_id: `pending-${Date.now()}`, role: 'user', content: question };
    state.conversation.messages = (state.conversation.messages || []).concat([optimistic]);
    renderConversation();
    $('conversationComposerHint').textContent = '正在检索证据并组织回答…';
    try {
      const response = await api(`/api/conversation/${encodeURIComponent(state.conversation.session_id)}/turns`, {
        method: 'POST', body: JSON.stringify({
          scan_id: chatScanId(), ...chatSourcePayload(), question, scope: turnScope,
          persist_scope: $('conversationPersistScope').checked,
          idempotency_key: idempotencyKey
        })
      });
      if (!isCurrent()) return;
      state.conversation = response.session;
      // Adopt the next-turn scope once the request is accepted. Polling only
      // redraws the draft chip, so later scope edits stay under user control.
      if (scopeEditVersion === state.scopeEditVersion) restoreConversationScope(response.session?.scope);
      state.conversationPending = null;
      registerAnalysisTurn(response.turn, []);
      await attachFileResults(question, response.turn);
      if (!isCurrent()) return;
      renderConversation();
      await loadConversationList();
      if (!isCurrent()) return;
      $('conversationDeepeningState').className = 'v2-job-state is-running';
      $('conversationDeepeningState').textContent = '分析任务已进入队列';
      if (response.turn && response.turn.id) watchAnalysisTurn(response.turn.id);
    } catch (error) {
      if (!isCurrent()) return;
      state.conversation.messages = state.conversation.messages.filter((message) => message.message_id !== optimistic.message_id);
      if (error.code === 'search_index_rebuild_required' && error.payload) {
        state.searchIndex = error.payload.search_index || state.searchIndex;
      }
      renderConversation();
      $('conversationQuestion').value = question;
      // Keep the key for transport/server errors so the next submit reuses it;
      // malformed/unauthorized requests are explicit failures and can start a
      // fresh operation.
      if (error && error.status >= 400 && error.status < 500) state.conversationPending = null;
      notify(error.message || '发送失败', true);
    } finally {
      if (isCurrent()) {
        state.conversationSending = false;
        renderConversation();
        $('conversationQuestion').focus();
      }
    }
  }

  /* Translation */
  function translationStatusLabel(status) {
    return {
      completed: '已完成', partial: '部分完成', failed: '失败',
      not_required: '无需翻译', not_started: '未开始', pending: '等待翻译',
      running: '翻译中', cancelled: '已取消', cache_unavailable: '缓存不可用'
    }[status] || status || '未开始';
  }

  function translationLanguageLabel(language) {
    return {
      zh: '中文', en: '英语', ja: '日语', ko: '韩语', fr: '法语', de: '德语',
      es: '西班牙语', ru: '俄语', ar: '阿拉伯语', pt: '葡萄牙语', it: '意大利语',
      mixed: '混合语言', unknown: '待识别'
    }[language] || String(language || 'unknown').toUpperCase();
  }

  function translationListControls() {
    return {
      status: ($('translationFilter') && $('translationFilter').value) || 'all',
      language: ($('translationLanguageFilter') && $('translationLanguageFilter').value) || 'all',
      query: ($('translationSearch') && $('translationSearch').value || '').trim()
    };
  }

  function renderTranslationFilterControls() {
    const page = state.translationListPage || {};
    const statusCounts = page.status_counts || {};
    const languageCounts = page.language_counts || {};
    const statusSelect = $('translationFilter');
    const languageSelect = $('translationLanguageFilter');
    if (statusSelect) {
      const selected = statusSelect.value || 'all';
      const order = ['all', 'failed', 'partial', 'running', 'pending', 'not_started', 'completed', 'not_required', 'cancelled', 'cache_unavailable'];
      const available = new Set(order);
      Object.keys(statusCounts).forEach((value) => available.add(value));
      const values = order.concat(Array.from(available).filter((value) => !order.includes(value)).sort());
      statusSelect.innerHTML = values.map((value) => {
        const label = value === 'all' ? '全部状态' : translationStatusLabel(value);
        const count = value === 'all' ? numeric(page.unfiltered_total) : numeric(statusCounts[value]);
        return `<option value="${escapeHtml(value)}">${escapeHtml(label)}${page.unfiltered_total != null ? `（${integer(count)}）` : ''}</option>`;
      }).join('');
      statusSelect.value = values.includes(selected) ? selected : 'all';
    }
    if (languageSelect) {
      const selected = languageSelect.value || 'all';
      const order = ['all', 'foreign', 'zh', 'unknown'];
      const available = new Set(order);
      Object.keys(languageCounts).forEach((value) => available.add(value));
      const values = order.concat(Array.from(available).filter((value) => !order.includes(value)).sort());
      languageSelect.innerHTML = values.map((value) => {
        let label = '全部语言';
        let count = numeric(page.unfiltered_total);
        if (value === 'foreign') {
          label = '全部外语';
          count = Object.entries(languageCounts).reduce((total, item) => (
            item[0] !== 'zh' && item[0] !== 'unknown' ? total + numeric(item[1]) : total
          ), 0);
        } else if (value !== 'all') {
          label = translationLanguageLabel(value);
          count = numeric(languageCounts[value]);
        }
        return `<option value="${escapeHtml(value)}">${escapeHtml(label)}${page.unfiltered_total != null ? `（${integer(count)}）` : ''}</option>`;
      }).join('');
      languageSelect.value = values.includes(selected) ? selected : 'all';
    }
  }

  function renderTranslationSummary() {
    const host = $('translationPackageState');
    if (!host) return;
    if (!translationScanId()) {
      host.innerHTML = '<span class="help-text">导入数据包后可识别并翻译外文资料。</span>';
      return;
    }
    const page = state.translationListPage || {};
    const counts = page.status_counts || state.translationCounts || {};
    const total = page.unfiltered_total == null
      ? Object.values(counts).reduce((sum, value) => sum + numeric(value), 0)
      : numeric(page.unfiltered_total);
    const values = [
      ['资料清单', total],
      ['已完成', numeric(counts.completed) + numeric(counts.not_required)],
      ['待翻译', numeric(counts.not_started) + numeric(counts.pending) + numeric(counts.running)],
      ['需处理', numeric(counts.partial) + numeric(counts.failed)]
    ];
    host.innerHTML = values.map(([label, value]) => `<div class="v2-translation-stat"><span>${escapeHtml(label)}</span><b>${integer(value)}</b></div>`).join('');
  }


  /* Translation data source --------------------------------------------------
   * This selector is deliberately independent from the global scan id.  A
   * large package is stored in large_package.db and must never replace the
   * ordinary package used by analysis, chat, import or email translation.
   */
  function translationScanId() {
    return state.translationSourceKind === 'large' ? state.translationSourceId : (state.translationSourceId || state.scanId);
  }

  function isLargeTranslationSource() {
    return String(translationScanId() || '').startsWith('lp-');
  }

  function translationSourceIsCurrent(scanId, version) {
    return translationScanId() === scanId && state.translationSourceVersion === version;
  }

  function persistTranslationSource() {
    try {
      window.localStorage.setItem(TRANSLATION_SOURCE_KEY, JSON.stringify({
        kind: state.translationSourceKind, id: state.translationSourceId
      }));
    } catch (_) { /* optional browser storage */ }
  }

  function restoreTranslationSource() {
    let stored = {};
    try {
      stored = JSON.parse(window.localStorage.getItem(TRANSLATION_SOURCE_KEY) || '{}');
      if (stored && stored.kind === 'large' && String(stored.id || '').startsWith('lp-')) {
        state.translationSourceKind = 'large';
        state.translationSourceId = String(stored.id);
        return;
      }
    } catch (_) { /* use standard source */ }
    if (stored && stored.kind === 'standard' && stored.id && !String(stored.id).startsWith('lp-')) {
      state.translationSourceKind = 'standard';
      state.translationSourceId = String(stored.id);
      return;
    }
    state.translationSourceKind = 'standard';
    state.translationSourceId = '';
  }

  function translationSourceLabel(item) {
    return String((item && (item.name || item.root || item.scan_id || item.id)) || '未命名数据包');
  }

  function renderTranslationSourceBar() {
    const bar = $('translationSourceBar');
    if (!bar) return;
    bar.querySelectorAll('[data-translation-source-kind]').forEach((button) => {
      const active = button.dataset.translationSourceKind === state.translationSourceKind;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    const select = $('translationSourceSelect');
    if (select) {
      const desired = state.translationSourceKind === 'large' ? state.translationSourceId : (state.translationSourceId || state.scanId);
      select.innerHTML = ['<option value="">选择数据包…</option>']
        .concat(desired && !state.translationSourceList.some((item) => String(item.scan_id || item.id || '') === String(desired))
          ? [`<option value="${escapeHtml(desired)}">当前数据包 · ${escapeHtml(desired)}</option>`] : [])
        .concat(state.translationSourceList.map((item) => {
          const id = String(item.scan_id || item.id || '');
          const status = item.status ? ` · ${item.status}` : '';
          return `<option value="${escapeHtml(id)}">${escapeHtml(translationSourceLabel(item) + status)}</option>`;
        })).join('');
      select.value = desired ? String(desired) : '';
      select.disabled = !state.translationSourceList.length && !desired;
    }
    renderSourceMoreButton(select, 'translationSourceMoreBtn', state.translationSourceLoading,
      state.translationSourceHasMore, state.translationSourceError, () => loadTranslationSources(state.translationSourceOffset > 0));
    const hint = $('translationSourceState');
    if (hint) hint.textContent = state.translationSourceError || (state.translationSourceLoading ? '正在读取数据包列表…' : '') || (state.translationSourceKind === 'large'
      ? '大数据包只翻译明确勾选的文件；不会影响普通数据包和邮件翻译。'
      : '普通数据包沿用当前数据包；可跨页勾选多个文件。');
    const large = isLargeTranslationSource();
    ['translatePriorityBtn', 'translateBackfillBtn'].forEach((id) => {
      if ($(id)) $(id).hidden = large;
    });
  }

  async function loadTranslationSources(append = false) {
    if (append && state.translationSourceLoading) return;
    const requestSeq = ++state.translationSourceRequestSeq;
    const kind = state.translationSourceKind;
    const endpoint = kind === 'large' ? '/api/large-packages/history' : '/api/data-sources';
    const offset = append ? state.translationSourceOffset : 0;
    state.translationSourceLoading = true;
    state.translationSourceError = '';
    if (!append) { state.translationSourceOffset = 0; state.translationSourceHasMore = false; state.translationSourceList = []; }
    renderTranslationSourceBar();
    try {
      const response = await api(`${endpoint}?limit=50&offset=${offset}`);
      if (state.translationSourceRequestSeq !== requestSeq || state.translationSourceKind !== kind) return;
      const items = (response.items || []).map((item) => ({
        scan_id: String(item.scan_id || item.id || ''), name: translationSourceLabel(item),
        status: item.status || '', usable: item.usable === undefined ? true : Boolean(item.usable)
      })).filter((item) => item.scan_id);
      const merged = append ? state.translationSourceList.concat(items) : items;
      state.translationSourceList = Array.from(new Map(merged.map((item) => [item.scan_id, item])).values());
      state.translationSourceOffset = offset + (response.items || []).length;
      const total = Number(response.total);
      state.translationSourceHasMore = items.length > 0 && (Number.isFinite(total)
        ? state.translationSourceOffset < total : items.length === 50);
    } catch (error) {
      if (state.translationSourceRequestSeq !== requestSeq || state.translationSourceKind !== kind) return;
      state.translationSourceError = error.message || '数据包列表读取失败';
    } finally {
      if (state.translationSourceRequestSeq === requestSeq && state.translationSourceKind === kind) {
        state.translationSourceLoading = false;
        renderTranslationSourceBar();
      }
    }
  }

  async function setTranslationSource(kind, id) {
    const nextKind = kind === 'large' ? 'large' : 'standard';
    const nextId = String(id || (nextKind === 'standard' ? state.scanId : '') || '');
    if (nextKind === 'large' && !nextId) return;
    if (nextKind === 'standard' && !nextId) return;
    if (nextKind === state.translationSourceKind && nextId === state.translationSourceId) return;
    state.translationSourceKind = nextKind;
    state.translationSourceId = nextId;
    persistTranslationSource();
    state.translationSourceRequestSeq += 1;
    state.translationSourceLoading = false;
    resetTranslationView(translationScanId());
    renderTranslationSourceBar();
    notify(nextKind === 'large' ? '已切换到大数据包翻译' : '已切换到普通数据包翻译');
    if (translationScanId()) loadTranslationList();
  }

  function bindTranslationSource() {
    const bar = $('translationSourceBar');
    if (!bar) return;
    bar.addEventListener('click', (event) => {
      const button = event.target.closest('[data-translation-source-kind]');
      if (button) {
        state.translationSourceKind = button.dataset.translationSourceKind === 'large' ? 'large' : 'standard';
        state.translationSourceId = state.translationSourceKind === 'large' ? '' : '';
        persistTranslationSource();
        state.translationSourceRequestSeq += 1;
        resetTranslationView(translationScanId());
        renderTranslationSourceBar();
        loadTranslationSources();
        if (translationScanId()) loadTranslationList();
      }
    });
    $('translationSourceSelect')?.addEventListener('change', (event) => {
      const id = event.currentTarget.value;
      if (id) setTranslationSource(state.translationSourceKind, id);
    });
  }

  function resetTranslationView(scanId) {
    state.translationSourceVersion += 1;
    state.translationRequestSeq += 1;
    state.translationListRequestSeq += 1;
    state.watchers.delete('translation-files');
    window.SJFXTranslationReader?.reset();
    state.translationItems = [];
    state.translationCounts = {};
    state.translationSelectedPaths.clear();
    state.translationSubmitting = false;
    state.translationActiveJob = '';
    if ($('translationQueueState')) $('translationQueueState').textContent = '';
    state.translationOffset = 0;
    state.translationPage = null;
    state.translationLoading = false;
    state.translationListOffset = 0;
    state.translationListPage = null;
    state.translationListLoading = false;
    window.clearTimeout(state.translationSearchTimer);
    state.translationPath = '';
    if ($('translationPath')) $('translationPath').value = '';
    if ($('translationFilter')) $('translationFilter').value = 'all';
    if ($('translationLanguageFilter')) $('translationLanguageFilter').value = 'all';
    if ($('translationSearch')) $('translationSearch').value = '';
    if ($('translationDocumentTitle')) $('translationDocumentTitle').textContent = '选择文件查看原文与译文';
    if ($('translationDocumentMeta')) $('translationDocumentMeta').textContent = '';
    if ($('translationDocumentState')) {
      $('translationDocumentState').textContent = scanId ? '正在读取新数据包的翻译记录…' : '从左侧选择文件，或输入文件相对路径。';
      $('translationDocumentState').className = 'v2-inline-state';
    }
    if ($('translationDocumentContent')) {
      $('translationDocumentContent').innerHTML = scanId
        ? emptyMarkup('正在切换数据包', '旧文档结果已清除，新的翻译记录正在读取。') : '';
    }
    if ($('translationPageInfo')) $('translationPageInfo').textContent = '—';
    ['translationPrevBtn', 'translationNextBtn', 'translateDocumentBtn'].forEach((id) => { if ($(id)) $(id).disabled = true; });
    if ($('translationOpenBtn')) $('translationOpenBtn').disabled = !scanId;
    if ($('translationList')) {
      $('translationList').innerHTML = scanId
        ? '<span class="help-text">正在读取新数据包的翻译记录…</span>'
        : '<span class="help-text">导入数据包后显示翻译记录。</span>';
    }
    if ($('translationListSummary')) $('translationListSummary').textContent = scanId ? '正在读取文件清单…' : '';
    if ($('translationListPageInfo')) $('translationListPageInfo').textContent = '—';
    ['translationListPrevBtn', 'translationListNextBtn'].forEach((id) => { if ($(id)) $(id).disabled = true; });
    renderTranslationSummary();
    renderTranslationSelection();
  }

  function renderTranslationSelection() {
    const selected = state.translationSelectedPaths;
    const visible = (state.translationItems || []).map((item) => item.path);
    const selectedVisible = visible.filter((path) => selected.has(path)).length;
    const busy = state.translationSubmitting || Boolean(state.translationActiveJob);
    const checkbox = $('translationSelectPage');
    if (checkbox) {
      checkbox.checked = visible.length > 0 && selectedVisible === visible.length;
      checkbox.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
      checkbox.disabled = !translationScanId() || !visible.length || state.translationListLoading;
    }
    if ($('translationSelectedCount')) $('translationSelectedCount').textContent = `已选 ${integer(selected.size)} 份`;
    if ($('translationClearSelection')) $('translationClearSelection').disabled = !selected.size;
    if ($('translateSelectedBtn')) $('translateSelectedBtn').disabled = !translationScanId() || !selected.size || busy;
    if ($('translationCancelBtn')) $('translationCancelBtn').disabled = !state.translationActiveJob;
    ['translatePriorityBtn', 'translateBackfillBtn'].forEach((id) => { if ($(id)) $(id).disabled = !translationScanId() || busy; });
    const page = state.translationPage || {};
    const ready = String(page.provider_id || '').startsWith('vllm:') && (window.SJFXTranslationReader
      ? window.SJFXTranslationReader.completeTranslation(page)
      : page.full_translation && ['completed', 'not_required'].includes(page.status));
    if ($('translateDocumentBtn')) $('translateDocumentBtn').disabled = !translationScanId() || !state.translationPath || busy || ready;
  }

  function renderTranslationListPager() {
    const page = state.translationListPage || {};
    const total = numeric(page.total);
    const offset = numeric(page.offset == null ? state.translationListOffset : page.offset);
    const count = (state.translationItems || []).length;
    if ($('translationListPageInfo')) {
      $('translationListPageInfo').textContent = total
        ? `${integer(offset + 1)}–${integer(Math.min(total, offset + count))} / ${integer(total)} 份`
        : '0 份';
    }
    if ($('translationListPrevBtn')) $('translationListPrevBtn').disabled = state.translationListLoading || offset <= 0;
    if ($('translationListNextBtn')) $('translationListNextBtn').disabled = state.translationListLoading || !page.has_more;
  }

  function renderTranslationList() {
    const host = $('translationList');
    if (!host) return;
    const page = state.translationListPage || {};
    const items = state.translationItems || [];
    renderTranslationFilterControls();
    renderTranslationListPager();
    renderTranslationSelection();
    if ($('translationListSummary')) {
      const controls = translationListControls();
      const total = numeric(page.total);
      const suffix = [
        controls.status !== 'all' ? translationStatusLabel(controls.status) : '',
        controls.language !== 'all' ? (controls.language === 'foreign' ? '外语' : translationLanguageLabel(controls.language)) : '',
        controls.query ? `“${controls.query}”` : ''
      ].filter(Boolean).join(' · ');
      $('translationListSummary').textContent = page.unfiltered_total == null
        ? (state.translationListLoading ? '正在读取文件清单…' : '')
        : `匹配 ${integer(total)} 份${suffix ? ` · ${suffix}` : ''}`;
    }
    if (!items.length) {
      host.innerHTML = state.translationListLoading
        ? '<span class="help-text">正在读取翻译资料…</span>'
        : '<span class="help-text">当前条件下没有匹配文件。可以调整状态、语言或文件名搜索。</span>';
      return;
    }
    host.innerHTML = items.map((item) => {
      const progress = item.progress || {};
      const total = numeric(progress.required_units || progress.total_units);
      const completed = numeric(progress.completed_units);
      const progressText = total ? ` · ${integer(completed)}/${integer(total)} 段` : '';
      const availability = item.source_availability === 'metadata' ? ' · 待解析' : item.source_availability === 'preview' ? ' · 预览可读' : '';
      return `<div class="v2-translation-row"><label class="v2-translation-check"><input type="checkbox" data-translation-select="${escapeHtml(item.path)}" aria-label="选择 ${escapeHtml(item.path)}"${state.translationSelectedPaths.has(item.path) ? ' checked' : ''}></label><button type="button" class="v2-translation-item${state.translationPath === item.path ? ' active' : ''}" data-translation-path="${escapeHtml(item.path)}"><strong>${escapeHtml(item.titles && (item.titles.translated || item.titles.original) || basename(item.path))}</strong><span>${escapeHtml(item.path)} · ${escapeHtml(translationLanguageLabel(item.source_language))} · ${escapeHtml(translationStatusLabel(item.status))}${escapeHtml(progressText)}${escapeHtml(availability)}</span></button></div>`;
    }).join('');
  }

  function renderTranslationIdleDocument(error = '') {
    if (state.translationPath || state.translationLoading) return;
    const hasFiles = state.translationItems.length > 0;
    const title = error ? '无法读取翻译资料' : hasFiles ? '请选择文件' : '没有匹配文件';
    const detail = error ? `${error}。可刷新左侧清单重试，或输入文件相对路径。`
      : hasFiles ? '资料清单已载入，请从左侧选择文件查看原文与译文。'
        : '当前条件下没有匹配文件，可调整左侧筛选或搜索。';
    if ($('translationDocumentState')) {
      $('translationDocumentState').textContent = detail;
      $('translationDocumentState').className = 'v2-inline-state' + (error ? ' is-error' : '');
    }
    if ($('translationDocumentContent')) $('translationDocumentContent').innerHTML = emptyMarkup(title, detail);
  }

  async function loadTranslationList(options) {
    if (!translationScanId()) return;
    options = options || {};
    const requestedScanId = translationScanId();
    const requestSeq = ++state.translationListRequestSeq;
    state.translationListOffset = Math.max(0, numeric(options.offset == null ? state.translationListOffset : options.offset));
    state.translationListLoading = true;
    renderTranslationList();
    try {
      const controls = translationListControls();
      const query = new URLSearchParams({
        offset: String(state.translationListOffset), limit: String(TRANSLATION_LIST_PAGE_SIZE),
        status: controls.status, language: controls.language, q: controls.query
      });
      const response = await api(`/api/translations/${encodeURIComponent(requestedScanId)}?${query.toString()}`);
      if (translationScanId() !== requestedScanId || state.translationListRequestSeq !== requestSeq) return;
      if (!response.items?.length && numeric(response.total) && numeric(response.offset) >= numeric(response.total)) {
        const lastOffset = Math.floor((numeric(response.total) - 1) / TRANSLATION_LIST_PAGE_SIZE) * TRANSLATION_LIST_PAGE_SIZE;
        if (lastOffset !== state.translationListOffset) return loadTranslationList({ offset: lastOffset });
      }
      state.translationItems = response.items || [];
      state.translationCounts = response.counts || {};
      state.translationListPage = response;
      state.translationListOffset = numeric(response.offset);
      renderTranslationSummary();
      renderTranslationList();
      renderTranslationIdleDocument();
      if ($('translationOpenBtn')) $('translationOpenBtn').disabled = false;
    } catch (error) {
      if (translationScanId() === requestedScanId && state.translationListRequestSeq === requestSeq) {
        $('translationList').innerHTML = `<span class="help-text">${escapeHtml(error.message)}</span>`;
        if ($('translationListSummary')) $('translationListSummary').textContent = '无法读取翻译资料';
        renderTranslationIdleDocument(error.message || '资料清单读取失败');
        if ($('translationOpenBtn')) $('translationOpenBtn').disabled = false;
      }
    } finally {
      if (state.translationListRequestSeq === requestSeq) {
        state.translationListLoading = false;
        renderTranslationListPager();
        renderTranslationSelection();
      }
    }
  }

  function translationPageBounds(page) {
    const pages = [page && page.original, page && page.translated].filter(Boolean);
    const total = Math.max(0, ...pages.map((item) => numeric(item.total_characters)));
    const offset = numeric(page && page.original && page.original.offset != null ? page.original.offset : page && page.translated && page.translated.offset);
    return { total, offset, end: Math.min(total, offset + PAGE_SIZE), hasMore: pages.some((item) => item.has_more) };
  }

  function renderTranslationDocument(page) {
    state.translationPage = page;
    const title = page.titles && (page.titles.translated || page.titles.original) || basename(page.path);
    $('translationDocumentTitle').textContent = title || page.path;
    const modeLabel = String(page.provider_id || '').startsWith('vllm:') ? `vLLM · ${String(page.provider_id).slice(5)}` : page.translation_mode === 'quality' ? '精细复核模式' : '已有工作译文';
    $('translationDocumentMeta').textContent = `${page.path} · ${translationStatusLabel(page.status)} · ${page.source_language || '语言待识别'} → ${page.target_language || 'zh-CN'} · ${modeLabel}`;
    const progress = page.progress || {};
    const details = [];
    if (progress.required_units) details.push(`已完成 ${integer(progress.completed_units)}/${integer(progress.required_units)} 个翻译段`);
    if (page.source_level) details.push(page.source_level === 'full' ? '全文来源' : '轻量预览来源');
    else if (page.source_availability === 'preview') details.push('当前为全库预览，翻译时会按需读取全文');
    else if (page.content_available === false || page.source_availability === 'metadata') details.push('当前只有文件清单；翻译时会先按需解析');
    if (page.full_translation) details.push('全文译文已就绪');
    if (page.performance && page.performance.paragraph_batching) details.push('短段落已合并加速');
    $('translationDocumentState').textContent = details.join(' · ') || (page.plan && page.plan.translation_required ? `需要翻译，共 ${integer(page.plan.required_unit_count)} 个外文段` : '文档已打开');
    $('translationDocumentState').className = 'v2-inline-state' + (page.status === 'failed' ? ' is-error' : ' is-ready');
    if (window.SJFXTranslationReader && page.reader_complete) {
      const ready = window.SJFXTranslationReader.completeTranslation(page);
      if (page.full_translation && !ready) {
        $('translationDocumentState').textContent = '仍有未通过校验的段落，请重新翻译当前文件；已保留可读内容。';
      }
      $('translationDocumentState').className = 'v2-inline-state' + (ready ? ' is-ready' : page.status === 'failed' ? ' is-error' : '');
      window.SJFXTranslationReader.render({ page, view: state.translationView, title });
      const total = Math.max(0, ...[page.original, page.translated, page.working].filter(Boolean).map((item) => numeric(item.total_characters)));
      $('translationPageInfo').textContent = total ? '正文已全部载入 · 连续阅读' : '暂无正文';
      $('translationPrevBtn').hidden = true;
      $('translationNextBtn').hidden = true;
      renderTranslationList();
      return;
    }
    const original = page.original && page.original.text;
    const translated = page.translated && page.translated.text;
    // A partially translated document still has usable content: show the
    // completed portion (unfinished units fall back to their source text)
    // instead of an empty panel that hides all progress.
    const working = page.working && page.working.text;
    const shownTranslation = translated != null ? translated : working;
    const partialNote = translated == null && working
      ? '<div class="v2-document-notice">本文档尚未全部完成，以下为已完成部分的译文，未完成处暂保留原文。</div>'
      : '';
    let content;
    if (state.translationView === 'bilingual') {
      content = `<div class="v2-bilingual"><section class="v2-document-column"><h3>原文</h3>${original != null ? `<pre>${escapeHtml(original)}</pre>` : '<div class="v2-document-notice">本页未返回原文。</div>'}</section><section class="v2-document-column"><h3>中文译文</h3>${shownTranslation != null ? `${partialNote}<pre>${escapeHtml(shownTranslation)}</pre>` : '<div class="v2-document-notice">译文尚未生成，请启动当前文件翻译。</div>'}</section></div>`;
    } else {
      const isOriginal = state.translationView === 'original';
      const text = isOriginal ? original : shownTranslation;
      const note = isOriginal ? '' : partialNote;
      content = `<section class="v2-document-column"><h3>${isOriginal ? '原文' : '中文译文'}</h3>${text != null ? `${note}<pre>${escapeHtml(text)}</pre>` : `<div class="v2-document-notice">${isOriginal ? '本页未返回原文。' : '译文尚未生成，请启动当前文件翻译。'}</div>`}</section>`;
    }
    if (page.errors && page.errors.length) {
      content = `<div class="v2-document-errors">${page.errors.map((error) => escapeHtml(typeof error === 'string' ? error : error.message || error.code || JSON.stringify(error))).join('<br>')}</div>${content}`;
    }
    if (page.warnings && page.warnings.length) {
      const warningLabels = page.warnings.map((warning) => {
        if (warning === 'line_structure_changed') return '正文换行已按语义标准化，未影响全文内容';
        if (warning === 'reference_metadata_preserved') return '参考文献、链接与编号按原文保留，确保可回查';
        if (warning === 'source_preserved_after_quality_review') return '部分图表或标签段落未能译出，已按原文保留，建议人工复核';
        if (warning === 'figure_label_preserved') return '图表坐标与标签按原文保留，避免误译数值';
        return warning;
      });
      content = `<div class="v2-document-warnings">${warningLabels.map(escapeHtml).join('<br>')}</div>${content}`;
    }
    $('translationDocumentContent').innerHTML = content;
    const bounds = translationPageBounds(page);
    $('translationPageInfo').textContent = bounds.total ? `${integer(bounds.offset + 1)}–${integer(bounds.end)} / ${integer(bounds.total)} 字符` : '暂无正文';
    $('translationPrevBtn').disabled = bounds.offset <= 0;
    $('translationNextBtn').disabled = !bounds.hasMore;
    renderTranslationList();
  }

  async function loadTranslation(path, offset) {
    path = String(path || '').trim();
    if (!translationScanId() || !path) return;
    if (state.translationLoading && state.translationPath === path && state.translationLoadingView === state.translationView) return;
    const requestedScanId = translationScanId();
    const requestedView = state.translationView;
    const newDocument = state.translationPath !== path;
    const requestSeq = ++state.translationRequestSeq;
    const isCurrent = () => translationScanId() === requestedScanId && state.translationRequestSeq === requestSeq && state.translationView === requestedView;
    state.translationLoading = true;
    state.translationLoadingView = requestedView;
    state.translationPath = path;
    state.translationOffset = window.SJFXTranslationReader ? 0 : Math.max(0, numeric(offset));
    if (newDocument) {
      state.translationPage = null;
      $('translationDocumentTitle').textContent = basename(path);
      $('translationDocumentMeta').textContent = path;
      $('translationDocumentContent').innerHTML = emptyMarkup('正在载入全文', '会按文档顺序连续呈现，不截断长文。');
      renderTranslationSelection();
    }
    $('translationPath').value = path;
    rememberTranslationPath(path);
    $('translationDocumentState').textContent = '正在读取完整文档…';
    $('translationDocumentState').className = 'v2-inline-state';
    $('translationOpenBtn').disabled = true;
    try {
      const query = new URLSearchParams({ path, view: state.translationView, offset: String(state.translationOffset), limit: String(PAGE_SIZE) });
      const response = window.SJFXTranslationReader
        ? await window.SJFXTranslationReader.readComplete({
          scanId: requestedScanId, path, api, isCurrent,
          onProgress: (loaded, total) => {
            if (isCurrent()) $('translationDocumentState').textContent = `正在载入完整正文：${integer(loaded)} / ${integer(total)} 字符`;
          }
        })
        : await api(`/api/translation/${encodeURIComponent(requestedScanId)}?${query.toString()}`);
      if (isCurrent()) renderTranslationDocument(response);
    } catch (error) {
      if (isCurrent() && error.name !== 'AbortError') {
        $('translationDocumentState').textContent = error.message || '无法打开文档';
        $('translationDocumentState').className = 'v2-inline-state is-error';
        if (!$('translationDocumentContent').querySelector('.tr-reader')) {
          $('translationDocumentContent').innerHTML = emptyMarkup('全文暂未载入', error.message || '请检查文件路径和解析状态。');
        }
        const retry = document.createElement('button');
        retry.type = 'button'; retry.className = 'text-button'; retry.textContent = '重新载入全文';
        retry.addEventListener('click', () => loadTranslation(path, 0));
        $('translationDocumentState').appendChild(retry);
        $('translateDocumentBtn').disabled = !translationScanId() || !state.translationPath;
      }
    } finally {
      if (state.translationRequestSeq === requestSeq) {
        state.translationLoading = false;
        $('translationOpenBtn').disabled = !translationScanId();
      }
    }
  }

  function translationJobState(job, label) {
    const host = $('translationQueueState');
    if (job.status === 'completed') {
      host.textContent = `${label}已完成，正在刷新译文…`;
      host.className = 'v2-inline-state is-ready';
      loadTranslationList();
      if (state.translationPath) loadTranslation(state.translationPath, state.translationOffset);
    } else if (['failed', 'cancelled'].includes(job.status)) {
      host.textContent = `${label}${jobStatusLabel(job)}${job.error ? `：${job.error}` : ''}`;
      host.className = 'v2-inline-state is-error';
    } else if (job.status === 'background') {
      host.textContent = job.message;
      host.className = 'v2-inline-state';
    } else {
      const progress = job.progress == null ? '' : ` · ${Math.round(numeric(job.progress))}%`;
      host.textContent = `${label}${jobStatusLabel(job)}${progress}${job.message ? ` · ${job.message}` : ''}`;
      host.className = 'v2-inline-state';
    }
  }

  async function watchTranslationJob(jobId, label) {
    const scanId = translationScanId();
    const sourceVersion = state.translationSourceVersion;
    const isCurrent = () => translationSourceIsCurrent(scanId, sourceVersion);
    let current = jobId;
    let failures = 0;
    let completed = 0;
    let lastRefresh = 0;
    try {
      while (current && isCurrent()) {
        state.translationActiveJob = current;
        renderTranslationSelection();
        const job = await watchJob(current, 'translation-files', (item) => {
          if (!isCurrent()) return;
          translationJobState(item, label);
          if (Date.now() - lastRefresh > 6000 && !['completed', 'failed', 'cancelled'].includes(item.status)) {
            lastRefresh = Date.now();
            loadTranslationList();
            if (state.translationPath) loadTranslation(state.translationPath, state.translationOffset);
          }
        }, isCurrent);
        if (!isCurrent() || !job || job.status !== 'completed') break;
        const result = job.result || {};
        completed += numeric(result.completed_files);
        failures += numeric(result.failed_files) || (['failed', 'partial'].includes(result.translation_status) ? 1 : 0);
        current = result.continuation_job_id || '';
        if (!current) {
          const host = $('translationQueueState');
          host.textContent = failures ? `${label}已结束：${integer(completed)} 份成功，${integer(failures)} 份未完成，可查看文件错误后重试。` : `${label}已完成。`;
          host.className = 'v2-inline-state' + (failures ? ' is-error' : ' is-ready');
        }
      }
    } finally {
      if (isCurrent()) {
        state.translationActiveJob = '';
        state.translationSubmitting = false;
        renderTranslationSelection();
        loadTranslationList();
        if (state.translationPath) loadTranslation(state.translationPath, state.translationOffset);
      }
    }
  }

  async function translateSelected() {
    const scanId = translationScanId();
    const sourceVersion = state.translationSourceVersion;
    const paths = Array.from(state.translationSelectedPaths);
    if (!scanId || !paths.length || state.translationSubmitting || state.translationActiveJob) return;
    state.translationSubmitting = true;
    renderTranslationSelection();
    try {
      const response = await api(`/api/translate-package/${encodeURIComponent(scanId)}`, {
        method: 'POST', body: JSON.stringify({ phase: 'deep_backfill', target_paths: paths, require_full: true })
      });
      if (!translationSourceIsCurrent(scanId, sourceVersion)) return;
      if (!state.translationPath) loadTranslation(paths[0], 0);
      watchTranslationJob(response.job_id, `所选 ${integer(paths.length)} 份文件翻译`);
    } catch (error) {
      if (translationSourceIsCurrent(scanId, sourceVersion)) {
        state.translationSubmitting = false;
        renderTranslationSelection();
        notify(error.message || '无法提交所选文件翻译', true);
      }
    }
  }

  async function translateDocument() {
    const path = state.translationPath || ($('translationPath').value || '').trim();
    if (!translationScanId() || !path) { notify('请先打开一个文件', true); return; }
    if (state.translationSubmitting || state.translationActiveJob) return;
    const scanId = translationScanId();
    const sourceVersion = state.translationSourceVersion;
    state.translationSubmitting = true;
    renderTranslationSelection();
    try {
      const response = await api(`/api/translation/${encodeURIComponent(scanId)}`, {
        method: 'POST', body: JSON.stringify({ path, require_full: true })
      });
      if (!translationSourceIsCurrent(scanId, sourceVersion)) return;
      $('translationQueueState').textContent = '当前文件已加入 vLLM 翻译队列。';
      watchTranslationJob(response.job_id, '当前文件全文翻译');
    } catch (error) {
      if (translationSourceIsCurrent(scanId, sourceVersion)) {
        notify(error.message || '无法提交翻译', true);
        state.translationSubmitting = false;
        renderTranslationSelection();
      }
    }
  }

  async function translatePackage(phase) {
    if (!translationScanId()) return;
    if (state.translationSubmitting || state.translationActiveJob) return;
    const scanId = translationScanId();
    const sourceVersion = state.translationSourceVersion;
    state.translationSubmitting = true;
    renderTranslationSelection();
    try {
      const response = await api(`/api/translate-package/${encodeURIComponent(scanId)}`, {
        method: 'POST', body: JSON.stringify({ phase })
      });
      const label = phase === 'deep_backfill' ? '全部外文全文补齐' : '预览与重点文件翻译';
      if (!translationSourceIsCurrent(scanId, sourceVersion)) return;
      $('translationQueueState').textContent = `${label}已加入后台队列。`;
      watchTranslationJob(response.job_id, label);
      notify(`${label}已提交`);
    } catch (error) {
      if (translationSourceIsCurrent(scanId, sourceVersion)) {
        notify(error.message || '无法提交数据包翻译', true);
        state.translationSubmitting = false;
        renderTranslationSelection();
      }
    }
  }

  function resetForScan(scanId) {
    if (!isLargeChatSource()) closeOriginalFile();
    const previousScanId = state.scanId;
    const preserveIndependentTranslation = Boolean(state.translationSourceId);
    if (!preserveIndependentTranslation && previousScanId && previousScanId !== scanId) {
      // A remembered document belongs to the previous package and must never
      // be opened against the newly selected scan.
      window.sessionStorage.removeItem(LAST_DOCUMENT_KEY);
      window.sessionStorage.removeItem(LAST_DOCUMENT_SCAN_KEY);
    }
    if (!preserveIndependentTranslation && !scanId) {
      window.sessionStorage.removeItem(LAST_DOCUMENT_KEY);
      window.sessionStorage.removeItem(LAST_DOCUMENT_SCAN_KEY);
    }
    state.scanId = scanId;
    // Invalidate any in-flight package request before replacing the active
    // workspace.  The response guards below then become a no-op for the old
    // package instead of repainting the new view with stale data.
    state.overviewLoading = false;
    state.overviewRequestSeq += 1;
    if (!preserveIndependentTranslation) {
      state.translationLoading = false;
      state.translationListLoading = false;
      state.translationRequestSeq += 1;
      state.translationListRequestSeq += 1;
      window.clearTimeout(state.translationSearchTimer);
      state.translationSearchTimer = 0;
    }
    state.conversationListRequestSeq += 1;
    state.overview = null;
    state.researchBrief = null;
    state.reportArtifact = null;
    state.selectedScope = null;
    // A chat session bound to an isolated large package is independent of the
    // global workspace, so switching ordinary packages must not discard it.
    const keepChatSource = Boolean(state.chatSourceId);
    if (!keepChatSource) {
      state.chatSourceVersion += 1;
      beginConversationTransition();
      state.conversation = null;
      state.conversationLoading = false;
      if (!scanId) ensureGeneralSession();
      state.searchIndex = null;
      state.conversationList = [];
      state.turns.clear();
      state.fileSummaries = null;
      state.fileSummariesScanId = '';
    }
    if (!preserveIndependentTranslation) {
      state.translationItems = [];
      state.translationCounts = {};
      state.translationPath = '';
      state.translationPage = null;
    }
    state.scopeConstraints = {};
    if (!preserveIndependentTranslation) state.watchers.clear();
    if (!keepChatSource && $('conversationScopeKind')) { $('conversationScopeKind').value = 'package'; renderScopeFields(); }
    renderChatSourceBar();
    const enabled = Boolean(scanId);
    ['packageOverviewRefreshBtn', 'packageOverviewAskAllBtn'].forEach((id) => { if ($(id)) $(id).disabled = !enabled; });
    ['translatePriorityBtn', 'translateBackfillBtn', 'translationOpenBtn'].forEach((id) => { if ($(id)) $(id).disabled = !translationScanId(); });
    if ($('conversationNewBtn')) $('conversationNewBtn').disabled = false;
    resetOverviewView(scanId);
    if (!preserveIndependentTranslation) resetTranslationView(translationScanId());
    renderTranslationSummary();
    renderConversation();
    renderConversationList();
  }

  function syncScan() {
    const scanId = (window.localStorage.getItem(CURRENT_SCAN_KEY) || '').trim();
    if (scanId !== state.scanId) {
      resetForScan(scanId);
      if (currentRoute() === 'overview') loadOverview(true);
      if (currentRoute() === 'chat') loadConversationList();
      if (currentRoute() === 'translation') loadTranslationList();
    }
  }

  function activate(route) {
    syncScan();
    if (route === 'overview') loadOverview(false);
    if (route === 'chat') {
      loadChatSources();
      loadConversationList();
    }
    if (route === 'translation') {
      loadTranslationSources();
      loadTranslationList();
      const rememberedScan = window.sessionStorage.getItem(LAST_DOCUMENT_SCAN_KEY) || '';
      const sourceId = translationScanId();
      const remembered = state.translationPath || (rememberedScan === sourceId ? window.sessionStorage.getItem(LAST_DOCUMENT_KEY) || '' : '');
      if (remembered) {
        $('translationPath').value = remembered;
        if (!state.translationPath) loadTranslation(remembered, 0);
      }
    }
  }

  function bind() {
    renderScopeFields();
    renderFolderDock();
    $('conversationMessages').addEventListener('change', (event) => {
      if (event.target.matches('[data-file-select]')) updateFileSelectionActions();
    });
    restoreTranslationSource();
    restoreChatSource();
    resetForScan((window.localStorage.getItem(CURRENT_SCAN_KEY) || '').trim());
    bindChatSource();
    bindTranslationSource();
    renderTranslationSourceBar();
    loadChatSources();
    $('packageOverviewRefreshBtn').addEventListener('click', () => loadOverview(true));
    $('packageOverviewAskAllBtn').addEventListener('click', () => carryScopeToConversation({ kind: 'package', label: '整个数据包', source_paths: [] }));
    $('packageOverviewAskBtn').addEventListener('click', () => carryScopeToConversation(state.selectedScope));
    $('packageOverviewReportBtn').addEventListener('click', downloadReport);
    $('conversationScopeKind').addEventListener('change', () => { state.scopeEditVersion += 1; state.scopeConstraints = {}; renderScopeFields(); });
    const scopeEdited = () => { state.scopeEditVersion += 1; updateContextChip(); };
    $('conversationScopeFields').addEventListener('input', scopeEdited);
    $('conversationScopeFields').addEventListener('change', scopeEdited);
    document.querySelector('[data-view="overview"]').addEventListener('click', (event) => {
      const appendixLink = event.target.closest('[data-io-open-evidence], [data-io-evidence-number]');
      if (appendixLink) {
        const appendix = $('packageOverviewEvidenceAppendix');
        if (appendix) appendix.open = true;
      }
      const file = event.target.closest('[data-brief-file]');
      if (file) { openBriefFile(file.dataset.briefFile); return; }
      const target = event.target.closest('[data-overview-scope-kind]');
      if (target) applyOverviewScope(target);
    });
    document.querySelector('[data-view="overview"]').addEventListener('keydown', (event) => {
      const target = event.target.closest('[data-overview-scope-kind]');
      if (target && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); applyOverviewScope(target); }
    });
    $('conversationNewBtn').addEventListener('click', createConversation);
    $('conversationListRefreshBtn').addEventListener('click', loadConversationList);
    $('conversationList').addEventListener('click', (event) => {
      const remove = event.target.closest('[data-delete-session]');
      if (remove) {
        // Deleting a conversation is not recoverable, so it is always a
        // deliberate two-step action.
        const sessionId = remove.dataset.deleteSession || '';
        const row = remove.closest('.v2-session-row');
        const label = row && row.querySelector('.v2-session-item strong');
        const name = label ? label.textContent.trim() : '这个会话';
        if (sessionId && window.confirm(`确定删除「${name}」及其全部消息和证据吗？删除后无法恢复。`)) {
          deleteConversation(sessionId, remove);
        }
        return;
      }
      const button = event.target.closest('[data-session-id]');
      if (button) openConversation(button.dataset.sessionId);
    });
    $('conversationForm').addEventListener('submit', (event) => { event.preventDefault(); sendQuestion(); });
    $('conversationQuestion').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendQuestion(); }
    });
    $('conversationSuggestions').addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      $('conversationQuestion').value = button.textContent;
      if (state.conversation) $('conversationQuestion').focus();
    });
    $('conversationMessages').addEventListener('click', async (event) => {
      if (event.target.closest('[data-load-older-messages]')) {
        loadOlderConversationMessages();
        return;
      }
      const fileOpen = event.target.closest('[data-file-open]');
      if (fileOpen) { openOriginalFile(fileOpen.dataset.fileOpen || ''); return; }
      const citationRead = event.target.closest('[data-citation-read]');
      if (citationRead) { openOriginalFile(citationRead.dataset.citationRead || ''); return; }
      const fileDownload = event.target.closest('[data-file-download]');
      if (fileDownload) { downloadOriginalFile(fileDownload.dataset.fileDownload || ''); return; }
      const collectFile = event.target.closest('[data-file-collect]');
      if (collectFile) { collectFileForFolder(collectFile.dataset.fileCollect || '', collectFile); return; }
      const citationCollect = event.target.closest('[data-citation-collect]');
      if (citationCollect) { collectFileForFolder(citationCollect.dataset.citationCollect || '', citationCollect); return; }
      const nodeFile = event.target.closest('[data-file-node]');
      if (nodeFile) { createNodeFromFile(nodeFile.dataset.fileNode || '', nodeFile); return; }
      const citationNode = event.target.closest('[data-citation-node]');
      if (citationNode) { createNodeFromFile(citationNode.dataset.citationNode || '', citationNode); return; }
      if (event.target.closest('[data-file-select]')) { updateFileSelectionActions(); return; }
      if (event.target.closest('[data-select-all-files]')) {
        const boxes = [...event.currentTarget.querySelectorAll('[data-file-select]')];
        const shouldCheck = boxes.some((box) => !box.checked);
        boxes.forEach((box) => { box.checked = shouldCheck; });
        updateFileSelectionActions();
        return;
      }
      if (event.target.closest('[data-create-node-selected]')) {
        const button = event.target.closest('[data-create-node-selected]');
        const selectedPaths = [...event.currentTarget.querySelectorAll('[data-file-select]:checked')]
          .map((box) => box.dataset.fileSelect)
          .filter(Boolean);
        createNodeFromFiles(selectedPaths, button);
        return;
      }
      if (event.target.closest('[data-export-file-folder]')) { exportCollectedFolder(); return; }
      const deepSummary = event.target.closest('[data-file-deep-summary]');
      if (deepSummary) { queueFileDeepSummary(deepSummary.dataset.fileDeepSummary || '', deepSummary); return; }
      const prioritize = event.target.closest('[data-evidence-prioritize]');
      if (prioritize) {
        const path = prioritize.dataset.evidencePrioritize || '';
        if (!path || !chatScanId()) return;
        prioritize.disabled = true;
        const originalLabel = prioritize.textContent;
        prioritize.textContent = '已提交…';
        try {
          const largeSource = isLargeChatSource();
          const result = await api(
            largeSource
              ? `/api/large-packages/${encodeURIComponent(chatScanId())}/select`
              : `/api/package-processing/${encodeURIComponent(chatScanId())}/resume`,
            {
              method: 'POST',
              body: JSON.stringify(largeSource
                ? { paths: [path] }
                : { mode: 'selection', target_paths: [path], continue_full: false })
            }
          );
          notify(result.message || (result.accepted ? '已加入深度分析队列' : '文件已在分析队列中'));
          prioritize.textContent = result.accepted ? '已加入队列' : '已处理';
        } catch (error) {
          prioritize.disabled = false;
          prioritize.textContent = originalLabel;
          notify(error.message || '加入深度分析失败', true);
        }
        return;
      }
      const copy = event.target.closest('[data-copy-message]');
      if (copy) {
        const message = (state.conversation && state.conversation.messages || []).find((item) => item.message_id === copy.dataset.copyMessage);
        if (message && navigator.clipboard) navigator.clipboard.writeText(message.content || '').then(() => notify('回答已复制')).catch(() => notify('复制失败', true));
        return;
      }
      const cancelTurn = event.target.closest('[data-cancel-turn]');
      if (cancelTurn) { cancelAnalysisTurn(cancelTurn.dataset.cancelTurn); return; }
      const retryTurn = event.target.closest('[data-retry-turn]');
      if (retryTurn) { retryAnalysisTurn(retryTurn.dataset.retryTurn); return; }
      const continueTurn = event.target.closest('[data-continue-deep-turn]');
      if (continueTurn) { continueDeepAnalysis(continueTurn.dataset.continueDeepTurn); return; }
      const regenerate = event.target.closest('[data-regenerate-message]');
      if (regenerate) {
        const question = previousUserQuestion(regenerate.dataset.regenerateMessage);
        if (question) sendQuestion(question);
        return;
      }
      const button = event.target.closest('[data-citation-translation]');
      if (!button) return;
      const path = button.dataset.citationTranslation;
      if (!path) return;
      openBriefFile(path, button.dataset.translationPackage || chatScanId());
    });
    $('conversationDeepeningState').addEventListener('click', (event) => {
      if (event.target.closest('[data-rebuild-search-index]')) rebuildConversationSearchIndex();
    });
    $('fileReaderClose')?.addEventListener('click', closeOriginalFile);
    $('fileReaderModal')?.addEventListener('click', (event) => { if (event.target.id === 'fileReaderModal') closeOriginalFile(); });
    $('fileReaderDownload')?.addEventListener('click', downloadReaderOriginal);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !$('fileReaderModal')?.hidden) closeOriginalFile();
    });
    $('fileReaderDeepSummary')?.addEventListener('click', () => queueFileDeepSummary($('fileReaderModal')?.dataset.path || '', $('fileReaderDeepSummary')));
    $('translationListRefreshBtn').addEventListener('click', () => loadTranslationList());
    $('translationFilter').addEventListener('change', () => loadTranslationList({ offset: 0 }));
    $('translationLanguageFilter').addEventListener('change', () => loadTranslationList({ offset: 0 }));
    $('translationSearch').addEventListener('input', (event) => {
      if (event.isComposing) return;
      window.clearTimeout(state.translationSearchTimer);
      state.translationSearchTimer = window.setTimeout(() => loadTranslationList({ offset: 0 }), 260);
    });
    $('translationSearch').addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      window.clearTimeout(state.translationSearchTimer);
      loadTranslationList({ offset: 0 });
    });
    $('translationList').addEventListener('click', (event) => {
      const button = event.target.closest('[data-translation-path]');
      if (button) loadTranslation(button.dataset.translationPath, 0);
    });
    $('translationList').addEventListener('change', (event) => {
      const checkbox = event.target.closest('[data-translation-select]');
      if (!checkbox) return;
      const path = checkbox.dataset.translationSelect;
      if (checkbox.checked && state.translationSelectedPaths.size >= 500 && !state.translationSelectedPaths.has(path)) {
        checkbox.checked = false;
        notify('单次最多选择 500 个文件', true);
        return;
      }
      if (checkbox.checked) state.translationSelectedPaths.add(path);
      else state.translationSelectedPaths.delete(path);
      renderTranslationSelection();
    });
    $('translationSelectPage')?.addEventListener('change', (event) => {
      const paths = state.translationItems.map((item) => item.path);
      if (event.target.checked && new Set([...state.translationSelectedPaths, ...paths]).size > 500) {
        notify('单次最多选择 500 个文件', true);
        renderTranslationSelection();
        return;
      }
      paths.forEach((path) => event.target.checked ? state.translationSelectedPaths.add(path) : state.translationSelectedPaths.delete(path));
      renderTranslationList();
    });
    $('translationClearSelection')?.addEventListener('click', () => {
      state.translationSelectedPaths.clear();
      renderTranslationList();
    });
    $('translateSelectedBtn')?.addEventListener('click', translateSelected);
    $('translationCancelBtn')?.addEventListener('click', async () => {
      const jobId = state.translationActiveJob;
      const scanId = translationScanId();
      const sourceVersion = state.translationSourceVersion;
      if (!jobId) return;
      try {
        await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', body: '{}' });
        if (!translationSourceIsCurrent(scanId, sourceVersion) || state.translationActiveJob !== jobId) return;
        $('translationQueueState').textContent = '正在取消，已完成的译文会保留。';
      } catch (error) { if (translationSourceIsCurrent(scanId, sourceVersion) && state.translationActiveJob === jobId) notify(error.message || '无法取消翻译', true); }
    });
    $('translationListPrevBtn').addEventListener('click', () => {
      const pageOffset = numeric(state.translationListPage && state.translationListPage.offset);
      loadTranslationList({ offset: Math.max(0, pageOffset - TRANSLATION_LIST_PAGE_SIZE) });
    });
    $('translationListNextBtn').addEventListener('click', () => {
      const nextOffset = state.translationListPage && state.translationListPage.next_offset;
      if (nextOffset != null) loadTranslationList({ offset: numeric(nextOffset) });
    });
    $('translationOpenBtn').addEventListener('click', () => loadTranslation($('translationPath').value, 0));
    $('translationPath').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); loadTranslation(event.currentTarget.value, 0); }
    });
    document.querySelectorAll('[data-translation-view]').forEach((button) => button.addEventListener('click', () => {
      state.translationView = button.dataset.translationView;
      document.querySelectorAll('[data-translation-view]').forEach((item) => item.classList.toggle('active', item === button));
      if (state.translationPath) loadTranslation(state.translationPath, 0);
    }));
    $('translationPrevBtn').addEventListener('click', () => loadTranslation(state.translationPath, Math.max(0, state.translationOffset - PAGE_SIZE)));
    $('translationNextBtn').addEventListener('click', () => loadTranslation(state.translationPath, state.translationOffset + PAGE_SIZE));
    $('translateDocumentBtn').addEventListener('click', translateDocument);
    $('translatePriorityBtn').addEventListener('click', () => translatePackage('preview_and_priority'));
    $('translateBackfillBtn').addEventListener('click', () => translatePackage('deep_backfill'));
    document.addEventListener('click', (event) => {
      const row = event.target.closest('.tree-row[data-path]');
      if (row && row.dataset.path && row.dataset.path !== '.') {
        rememberTranslationPath(row.dataset.path, state.scanId);
        if (translationScanId() === state.scanId && !state.translationPath) $('translationPath').value = row.dataset.path;
      }
    }, true);
    window.setInterval(syncScan, 1200);
    activate(currentRoute());
  }

  window.SJFXEngineering = { activate, refreshOverview: () => loadOverview(true), openTranslation: (path) => loadTranslation(path, 0) };
  document.addEventListener('DOMContentLoaded', bind);
}());
