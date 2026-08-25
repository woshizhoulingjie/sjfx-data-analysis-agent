/* SJFX engineering v2 front-end integration.
 *
 * This client deliberately uses only browser primitives: authenticated fetch,
 * SVG and CSS.  It keeps package content visualisation separate from worker
 * telemetry, preserves original text beside translations, and renders every
 * generated answer with its available evidence handles. */
(function () {
  'use strict';

  const CURRENT_SCAN_KEY = 'sjfx_current_scan_id_v1';
  const LAST_DOCUMENT_KEY = 'sjfx_last_document_path_v2';
  const PAGE_SIZE = 6000;
  const COLORS = ['#267657', '#d39247', '#397ab5', '#75a988', '#9b6eac', '#c05f55', '#637f72', '#c2a13e'];
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
    scanId: '', overview: null, overviewLoading: false,
    conversation: null, conversationList: [], turns: new Map(), conversationSending: false,
    translationItems: [], translationCounts: {}, translationPath: '', translationView: 'translated',
    translationOffset: 0, translationPage: null, translationLoading: false,
    watchers: new Map(), scopeConstraints: {}
  };

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
    let token = window.sessionStorage.getItem('sjfx_api_token') || '';
    if (token) headers['X-SJFX-Token'] = token;
    let response = await window.fetch(url, Object.assign({}, options, { headers }));
    if (response.status === 401) {
      window.sessionStorage.removeItem('sjfx_api_token');
      delete headers['X-SJFX-Token'];
      token = window.prompt('访问凭据已失效，请重新输入 SJFX API Token', '') || '';
      if (token) {
        window.sessionStorage.setItem('sjfx_api_token', token);
        headers['X-SJFX-Token'] = token;
        response = await window.fetch(url, Object.assign({}, options, { headers }));
      }
    }
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* reverse-proxy errors may not be JSON */ }
    if (!response.ok || !payload.ok) {
      const error = new Error(payload.error || `请求失败（HTTP ${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function formatBytes(value) {
    let bytes = numeric(value);
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let unit = 0;
    while (bytes >= 1024 && unit < units.length - 1) { bytes /= 1024; unit += 1; }
    return `${bytes >= 100 || unit === 0 ? bytes.toFixed(0) : bytes.toFixed(1)} ${units[unit]}`;
  }

  function setOverviewState(message, type) {
    const host = $('packageOverviewState');
    if (!host) return;
    host.textContent = message;
    host.className = 'v2-inline-state' + (type ? ` is-${type}` : '');
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

  function scopeAttributes(scope) {
    if (!scope) return '';
    return ` data-overview-scope-kind="${escapeHtml(scope.kind)}" data-overview-scope-value="${escapeHtml(scope.value)}" data-overview-scope-label="${escapeHtml(scope.label || scope.value)}" data-overview-scope-dimension="${escapeHtml(scope.dimension || '')}"`;
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
      const attrs = scopeAttributes({ kind: 'time', value: period, label: `${period} 年` });
      return `<g class="v2-overview-scope" role="button" tabindex="0"${attrs}><rect class="v2-bar" x="${x}" y="${base - firstHeight}" width="${Math.max(7, slot * .22)}" height="${firstHeight}"><title>文件修改时间 ${period}：${integer(first)}</title></rect><rect class="v2-bar-alt" x="${x + Math.max(9, slot * .25)}" y="${base - secondHeight}" width="${Math.max(7, slot * .22)}" height="${secondHeight}"><title>正文日期 ${period}：${integer(second)}</title></rect><text x="${left + index * slot + slot * .5}" y="${base + 18}" text-anchor="middle">${escapeHtml(period)}</text></g>`;
    }).join('');
    host.innerHTML = `<div class="v2-timeline-scroll"><svg class="v2-timeline-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="资料年份分布">${grid}${bars}<rect x="${left}" y="${height - 22}" width="9" height="9" class="v2-bar"></rect><text x="${left + 14}" y="${height - 14}">文件修改时间</text><rect x="${left + 112}" y="${height - 22}" width="9" height="9" class="v2-bar-alt"></rect><text x="${left + 126}" y="${height - 14}">正文识别日期</text></svg></div>`;
  }

  function renderEntities(overview) {
    const host = $('packageOverviewEntities');
    if (!host) return;
    const people = itemsOf(overview.entities && overview.entities.people);
    const organisations = itemsOf(overview.entities && overview.entities.organizations);
    const group = (title, items) => `<div class="v2-entity-group"><h3>${escapeHtml(title)}</h3>${items.length ? `<div class="v2-tag-cloud">${items.slice(0, 18).map((item) => `<button type="button" class="v2-entity-tag is-actionable" title="${escapeHtml(item.name)}"${scopeAttributes({ kind: 'entity', value: item.name, label: `${title} · ${item.name}` })}><span>${escapeHtml(item.name || '未知')}</span><b>${integer(item.file_count)}</b></button>`).join('')}</div>` : '<span class="muted">暂无</span>'}</div>`;
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
    const nodeMarkup = nodes.map((path) => {
      const point = positions[path];
      return `<g><circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="8"><title>${escapeHtml(path)}</title></circle><text x="${point.x.toFixed(1)}" y="${(point.y + (point.y < centreY ? -13 : 20)).toFixed(1)}" text-anchor="middle">${escapeHtml(truncate(basename(path), 18))}</text></g>`;
    }).join('');
    const rows = relationships.slice(0, 10).map((item) => `<tr><td>${escapeHtml(item.source_file)}</td><td>${escapeHtml(item.relation || '关联')}</td><td>${escapeHtml(item.target_file)}</td><td>${escapeHtml(item.weight == null ? '—' : item.weight)}</td></tr>`).join('');
    host.innerHTML = `<div class="v2-relationship-wrap"><svg class="v2-relationship-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="文件关系网络">${lines}${nodeMarkup}</svg></div><table class="v2-relationship-table"><thead><tr><th>来源文件</th><th>关系</th><th>目标文件</th><th>权重</th></tr></thead><tbody>${rows}</tbody></table>${disclosure(section, '关系')}`;
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
    host.innerHTML = `<div class="v2-split-facts"><div><b class="v2-fact-count">${integer(exact.duplicate_file_count)}</b><span>精确重复副本</span><div class="v2-fact-list">${exactItems.slice(0, 4).map(duplicateGroup).join('') || '<small class="muted">无精确重复组</small>'}</div></div><div><b class="v2-fact-count">${integer(near.duplicate_file_count)}</b><span>近似重复副本</span><div class="v2-fact-list">${nearItems.slice(0, 4).map(duplicateGroup).join('') || '<small class="muted">无近似重复组</small>'}</div></div></div>${disclosure(exact, '精确重复组')}${disclosure(near, '近似重复组')}`;
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

  function renderOverview(overview) {
    const pkg = overview.package || {};
    const metrics = [
      ['文件总数', integer(pkg.file_count), '数据包中的物理文件'],
      ['数据规模', formatBytes(pkg.total_bytes), '全部文件总体积'],
      ['目录数量', integer(pkg.directory_count), `最大深度 ${integer(pkg.max_depth)}`],
      ['文件关系', integer(overview.file_relationships && overview.file_relationships.relationship_count), '内容或结构联系']
    ];
    $('packageOverviewMetrics').innerHTML = metrics.map((metric) => `<div class="v2-overview-metric"><span>${escapeHtml(metric[0])}</span><strong>${escapeHtml(metric[1])}</strong><small>${escapeHtml(metric[2])}</small></div>`).join('');
    renderBars('packageOverviewDirectories', overview.directories, (item) => item.path === '.' ? '数据包根目录' : item.path, 'recursive_file_count', (value, item) => `${integer(value)} 文件 · ${formatBytes(item.total_bytes)}`, (item, label) => item.path === '.' ? null : ({ kind: 'directory', value: item.path, label }));
    renderDonut('packageOverviewFormats', overview.formats, 'format', 'file_count', (item, label) => ({ kind: 'file_type', value: item.format, label: `格式 · ${label}`, dimension: 'format' }));
    renderDonut('packageOverviewTypes', overview.document_types, 'document_type', 'file_count', (item, label) => ({ kind: 'file_type', value: item.document_type, label: `文档类型 · ${label}`, dimension: 'document_type' }));
    renderBars('packageOverviewLanguages', overview.languages, (item) => item.label || item.language, 'file_count', null, (item, label) => ({ kind: 'file_type', value: item.label || item.language, label: `语言 · ${label}`, dimension: 'language' }));
    renderTimeline(overview);
    renderBars('packageOverviewTopics', overview.topics, 'topic', 'file_count', null, (item, label) => ({ kind: 'topic', value: item.topic, label: `主题 · ${label}` }));
    renderEntities(overview);
    renderRelationships(overview);
    renderDuplicates(overview);
    renderOutliers(overview);
    setOverviewState(`正在查看 ${pkg.root || '当前数据包'} 的内容地图。`, 'ready');
  }

  async function loadOverview(force) {
    if (!state.scanId || state.overviewLoading || (state.overview && !force)) return;
    state.overviewLoading = true;
    $('packageOverviewRefreshBtn').disabled = true;
    setOverviewState('正在汇总数据包自身的内容构成…');
    try {
      const response = await api(`/api/package-overview/${encodeURIComponent(state.scanId)}`);
      state.overview = response.overview || {};
      renderOverview(state.overview);
    } catch (error) {
      setOverviewState(error.message || '无法加载数据包概览', 'error');
    } finally {
      state.overviewLoading = false;
      $('packageOverviewRefreshBtn').disabled = !state.scanId;
    }
  }

  /* Conversation */
  function scopeKindLabel(kind) {
    return { package: '整个数据包', topic: '主题', directory: '目录', entity: '人物或机构', time: '时间范围', file_type: '文件类型', files: '指定文件' }[kind] || kind;
  }

  function renderScopeFields() {
    const host = $('conversationScopeFields');
    if (!host) return;
    const kind = $('conversationScopeKind').value;
    if (kind === 'package') {
      host.innerHTML = '<p class="help-text">问题将在当前数据包的全部可检索资料中查找证据。</p>';
    } else if (kind === 'time') {
      host.innerHTML = '<label for="conversationScopeStart">开始时间</label><input id="conversationScopeStart" placeholder="例如 2020-01-01"><label for="conversationScopeEnd">结束时间</label><input id="conversationScopeEnd" placeholder="例如 2024-12-31">';
    } else if (kind === 'files') {
      host.innerHTML = '<label for="conversationScopeValue">文件路径（每行一个）</label><textarea id="conversationScopeValue" rows="5" placeholder="letters/a.eml\nreports/annual.pdf"></textarea>';
    } else {
      const labels = { topic: '主题名称', directory: '相对目录路径', entity: '人物或机构名称', file_type: '格式、文档类型或语言' };
      const examples = { topic: '例如：项目交付', directory: '例如：letters/2024', entity: '例如：某某机构', file_type: '例如：.pdf 或 报告' };
      host.innerHTML = `<label for="conversationScopeValue">${escapeHtml(labels[kind])}</label><input id="conversationScopeValue" placeholder="${escapeHtml(examples[kind])}">`;
    }
  }

  function conversationScope() {
    const kind = $('conversationScopeKind').value;
    if (kind === 'package') return { kind: 'package' };
    if (kind === 'time') {
      const start = ($('conversationScopeStart').value || '').trim();
      const end = ($('conversationScopeEnd').value || '').trim();
      if (!start && !end) throw new Error('请至少填写开始或结束时间');
      return { kind: 'time', value: Object.assign({}, start ? { start } : {}, end ? { end } : {}) };
    }
    const value = ($('conversationScopeValue').value || '').trim();
    if (!value) throw new Error(`请填写${scopeKindLabel(kind)}`);
    if (kind === 'files') {
      const paths = Array.from(new Set(value.split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean)));
      if (!paths.length) throw new Error('请至少填写一个文件路径');
      return { kind: 'files', value: paths, source_paths: paths };
    }
    return { kind, value, constraints: Object.assign({}, state.scopeConstraints || {}) };
  }

  function applyOverviewScope(element) {
    const kind = element.dataset.overviewScopeKind;
    const rawValue = element.dataset.overviewScopeValue || '';
    if (!kind || !rawValue) return;
    state.scopeConstraints = element.dataset.overviewScopeDimension ? { dimension: element.dataset.overviewScopeDimension } : {};
    $('conversationScopeKind').value = kind;
    renderScopeFields();
    if (kind === 'time') {
      $('conversationScopeStart').value = `${rawValue}-01-01`;
      $('conversationScopeEnd').value = `${rawValue}-12-31`;
    } else if ($('conversationScopeValue')) {
      $('conversationScopeValue').value = rawValue;
    }
    window.SJFXShell && window.SJFXShell.activate('chat');
    $('conversationQuestion').focus();
    notify(`已进入${element.dataset.overviewScopeLabel || rawValue}资料范围`);
  }

  function scopeSummary(scope) {
    scope = scope || { kind: 'package' };
    if (scope.kind === 'package') return '范围：整个数据包';
    if (scope.kind === 'time') return `范围：${scope.value && scope.value.start || '不限'} 至 ${scope.value && scope.value.end || '不限'}`;
    if (scope.kind === 'files') return `范围：${(scope.source_paths || scope.value || []).length} 个指定文件`;
    return `范围：${scopeKindLabel(scope.kind)} · ${scope.label || scope.value || '—'}`;
  }

  function renderConversationList() {
    const host = $('conversationList');
    if (!host) return;
    if (!state.conversationList.length) {
      host.innerHTML = '<span class="help-text">当前数据包还没有历史会话。</span>';
      return;
    }
    host.innerHTML = state.conversationList.map((item) => `<button type="button" class="v2-session-item${state.conversation && state.conversation.session_id === item.session_id ? ' active' : ''}" data-session-id="${escapeHtml(item.session_id)}"><strong>${escapeHtml(item.title || '资料问答')}</strong><span>${escapeHtml(scopeSummary(item.scope))} · ${integer(item.message_count)} 条消息</span></button>`).join('');
  }

  async function loadConversationList() {
    if (!state.scanId) return;
    try {
      const response = await api(`/api/conversations/${encodeURIComponent(state.scanId)}`);
      state.conversationList = response.items || [];
      renderConversationList();
    } catch (error) {
      $('conversationList').innerHTML = `<span class="help-text">${escapeHtml(error.message)}</span>`;
    }
  }

  function citationMarkup(citation) {
    const location = citation.location && Object.keys(citation.location).length
      ? Object.entries(citation.location).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join('-') : value}`).join(' · ') : '原文片段';
    const original = citation.original_text ? `<p><b>原文：</b>${escapeHtml(citation.original_text)}</p>` : '';
    const translated = citation.translated_text ? `<p><b>中文：</b>${escapeHtml(citation.translated_text)}</p>` : '';
    return `<div class="v2-citation"><strong>${escapeHtml(citation.citation_label || '')} ${escapeHtml(citation.source_path || '未知来源')}</strong><small>${escapeHtml(location)} · ${escapeHtml(citation.evidence_role || '直接证据')}</small>${translated}${original}<button type="button" class="text-button" data-citation-translation="${escapeHtml(citation.source_path || '')}">在翻译页打开</button></div>`;
  }

  function messageMarkup(message) {
    const role = message.role === 'user' ? 'user' : 'assistant';
    const turn = state.turns.get(message.message_id);
    const intent = message.intent || turn && turn.intent && turn.intent.name;
    let evidence = '';
    if (turn && Array.isArray(turn.citations) && turn.citations.length) {
      evidence = `<details class="v2-citations"><summary>查看 ${integer(turn.citations.length)} 条引用证据</summary>${turn.citations.map(citationMarkup).join('')}</details>`;
    } else if (message.evidence_ids && message.evidence_ids.length) {
      evidence = `<details class="v2-citations"><summary>查看证据标识</summary><div class="v2-citation"><small>${escapeHtml(message.evidence_ids.join('、'))}</small></div></details>`;
    }
    const meta = role === 'assistant' ? `<div class="v2-message-meta"><span>${escapeHtml(intent || '资料回答')}</span>${turn && turn.context && turn.context.follow_up ? '<span>已理解为追问</span>' : ''}${turn && turn.evidence_status ? `<span>证据：${escapeHtml(turn.evidence_status)}</span>` : ''}</div>` : '';
    return `<div class="v2-message ${role}" data-message-id="${escapeHtml(message.message_id || '')}"><div class="v2-message-card">${escapeHtml(message.content || '')}${meta}${evidence}</div></div>`;
  }

  function renderConversation() {
    const session = state.conversation;
    $('conversationTitle').textContent = session ? (session.title || '资料问答') : '尚未开始会话';
    $('conversationScopeSummary').textContent = session ? scopeSummary(session.scope) : '请选择范围并新建会话';
    const messages = session && Array.isArray(session.messages) ? session.messages : [];
    $('conversationMessages').innerHTML = messages.length ? messages.map(messageMarkup).join('') : emptyMarkup('从资料中提出第一个问题', '例如：这批资料主要讲了什么？哪些文件相互关联？');
    $('conversationQuestion').disabled = !session || state.conversationSending;
    $('conversationSendBtn').disabled = !session || state.conversationSending;
    $('conversationComposerHint').textContent = session ? '回答将尽量引用可回查的原文证据' : '新建会话后即可提问';
    renderConversationList();
    window.requestAnimationFrame(() => { $('conversationMessages').scrollTop = $('conversationMessages').scrollHeight; });
  }

  async function createConversation() {
    if (!state.scanId) return;
    let scope;
    try { scope = conversationScope(); } catch (error) { notify(error.message, true); return; }
    const button = $('conversationNewBtn');
    button.disabled = true;
    try {
      const response = await api('/api/conversations', {
        method: 'POST', body: JSON.stringify({ scan_id: state.scanId, scope, title: `资料问答 · ${new Date().toLocaleString('zh-CN', { hour12: false })}` })
      });
      state.conversation = response.session;
      state.turns.clear();
      renderConversation();
      await loadConversationList();
      $('conversationQuestion').focus();
    } catch (error) {
      notify(error.message || '新建会话失败', true);
    } finally { button.disabled = !state.scanId; }
  }

  async function openConversation(sessionId) {
    if (!state.scanId || !sessionId) return;
    try {
      const response = await api(`/api/conversation/${encodeURIComponent(sessionId)}?scan_id=${encodeURIComponent(state.scanId)}`);
      state.conversation = response.session;
      state.turns.clear();
      renderConversation();
    } catch (error) { notify(error.message || '无法打开会话', true); }
  }

  function jobStatusLabel(job) {
    return { queued: '已排队', running: '正在运行', cancelling: '正在取消', completed: '已完成', failed: '失败', cancelled: '已取消' }[job.status] || job.status || '未知';
  }

  async function watchJob(jobId, key, callback) {
    if (!jobId) return;
    const token = `${Date.now()}-${Math.random()}`;
    state.watchers.set(key, token);
    for (let attempt = 0; attempt < 240 && state.watchers.get(key) === token; attempt += 1) {
      try {
        const response = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        const job = response.job || response;
        callback(job, false);
        if (['completed', 'failed', 'cancelled'].includes(job.status)) {
          state.watchers.delete(key);
          return job;
        }
      } catch (error) {
        callback({ status: 'connection_error', error: error.message }, false);
      }
      await wait(3000);
    }
    if (state.watchers.get(key) === token) {
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

  async function sendQuestion() {
    if (!state.conversation || state.conversationSending) return;
    const question = ($('conversationQuestion').value || '').trim();
    if (!question) { notify('请输入问题', true); return; }
    let turnScope;
    try { turnScope = conversationScope(); } catch (error) { notify(error.message, true); return; }
    state.conversationSending = true;
    $('conversationQuestion').value = '';
    const optimistic = { message_id: `pending-${Date.now()}`, role: 'user', content: question };
    state.conversation.messages = (state.conversation.messages || []).concat([optimistic]);
    renderConversation();
    $('conversationComposerHint').textContent = '正在检索证据并组织回答…';
    try {
      const response = await api(`/api/conversation/${encodeURIComponent(state.conversation.session_id)}/messages`, {
        method: 'POST', body: JSON.stringify({
          scan_id: state.scanId, question, scope: turnScope,
          persist_scope: $('conversationPersistScope').checked
        })
      });
      state.conversation = response.session;
      if (response.turn && response.turn.message_id) state.turns.set(response.turn.message_id, response.turn);
      renderConversation();
      await loadConversationList();
      if (response.promotion_job_id || response.turn && response.turn.promotion_job_id) {
        watchPromotion(response.promotion_job_id || response.turn.promotion_job_id);
      } else {
        $('conversationDeepeningState').textContent = '';
        $('conversationDeepeningState').className = 'v2-job-state';
      }
    } catch (error) {
      state.conversation.messages = state.conversation.messages.filter((message) => message.message_id !== optimistic.message_id);
      renderConversation();
      $('conversationQuestion').value = question;
      notify(error.message || '发送失败', true);
    } finally {
      state.conversationSending = false;
      renderConversation();
      $('conversationQuestion').focus();
    }
  }

  /* Translation */
  function translationStatusLabel(status) {
    return { completed: '已完成', partial: '部分完成', failed: '失败', not_required: '无需翻译', not_started: '未开始', pending: '等待翻译', running: '翻译中' }[status] || status || '未开始';
  }

  function renderTranslationSummary() {
    const host = $('translationPackageState');
    if (!host) return;
    if (!state.scanId) {
      host.innerHTML = '<span class="help-text">导入数据包后可识别并翻译外文资料。</span>';
      return;
    }
    const counts = state.translationCounts || {};
    const values = [
      ['翻译记录', Object.values(counts).reduce((sum, value) => sum + numeric(value), 0)],
      ['已完成', counts.completed || counts.not_required || 0],
      ['部分完成', counts.partial || 0],
      ['失败', counts.failed || 0]
    ];
    host.innerHTML = values.map(([label, value]) => `<div class="v2-translation-stat"><span>${escapeHtml(label)}</span><b>${integer(value)}</b></div>`).join('');
  }

  function renderTranslationList() {
    const host = $('translationList');
    if (!host) return;
    const filter = $('translationFilter').value;
    const filtered = state.translationItems.filter((item) => filter === 'all' || (item.status || 'not_started') === filter);
    if (!filtered.length) {
      host.innerHTML = '<span class="help-text">当前筛选条件下没有翻译记录。仍可在上方输入任意已解析文件路径。</span>';
      return;
    }
    host.innerHTML = filtered.map((item) => {
      const progress = item.progress || {};
      const total = numeric(progress.required_units || progress.total_units);
      const completed = numeric(progress.completed_units);
      const progressText = total ? ` · ${integer(completed)}/${integer(total)} 段` : '';
      return `<button type="button" class="v2-translation-item${state.translationPath === item.path ? ' active' : ''}" data-translation-path="${escapeHtml(item.path)}"><strong>${escapeHtml(item.titles && (item.titles.translated || item.titles.original) || basename(item.path))}</strong><span>${escapeHtml(item.path)} · ${escapeHtml(translationStatusLabel(item.status))}${escapeHtml(progressText)}</span></button>`;
    }).join('');
  }

  async function loadTranslationList() {
    if (!state.scanId) return;
    try {
      const response = await api(`/api/translations/${encodeURIComponent(state.scanId)}?limit=500`);
      state.translationItems = response.items || [];
      state.translationCounts = response.counts || {};
      renderTranslationSummary();
      renderTranslationList();
    } catch (error) {
      $('translationList').innerHTML = `<span class="help-text">${escapeHtml(error.message)}</span>`;
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
    $('translationDocumentMeta').textContent = `${page.path} · ${translationStatusLabel(page.status)} · ${page.source_language || '语言待识别'} → ${page.target_language || 'zh-CN'}`;
    const progress = page.progress || {};
    const details = [];
    if (progress.required_units) details.push(`已完成 ${integer(progress.completed_units)}/${integer(progress.required_units)} 个翻译段`);
    if (page.source_level) details.push(page.source_level === 'full' ? '全文来源' : '轻量预览来源');
    if (page.full_translation) details.push('全文译文已就绪');
    $('translationDocumentState').textContent = details.join(' · ') || (page.plan && page.plan.translation_required ? `需要翻译，共 ${integer(page.plan.required_unit_count)} 个外文段` : '文档已打开');
    $('translationDocumentState').className = 'v2-inline-state' + (page.status === 'failed' ? ' is-error' : ' is-ready');
    const original = page.original && page.original.text;
    const translated = page.translated && page.translated.text;
    let content;
    if (state.translationView === 'bilingual') {
      content = `<div class="v2-bilingual"><section class="v2-document-column"><h3>原文</h3>${original != null ? `<pre>${escapeHtml(original)}</pre>` : '<div class="v2-document-notice">本页未返回原文。</div>'}</section><section class="v2-document-column"><h3>中文译文</h3>${translated != null ? `<pre>${escapeHtml(translated)}</pre>` : '<div class="v2-document-notice">译文尚未生成，请启动当前文件翻译。</div>'}</section></div>`;
    } else {
      const isOriginal = state.translationView === 'original';
      const text = isOriginal ? original : translated;
      content = `<section class="v2-document-column"><h3>${isOriginal ? '原文' : '中文译文'}</h3>${text != null ? `<pre>${escapeHtml(text)}</pre>` : `<div class="v2-document-notice">${isOriginal ? '本页未返回原文。' : '译文尚未生成，请启动当前文件翻译。'}</div>`}</section>`;
    }
    if (page.errors && page.errors.length) {
      content = `<div class="v2-document-errors">${page.errors.map((error) => escapeHtml(typeof error === 'string' ? error : error.message || error.code || JSON.stringify(error))).join('<br>')}</div>${content}`;
    }
    $('translationDocumentContent').innerHTML = content;
    const bounds = translationPageBounds(page);
    $('translationPageInfo').textContent = bounds.total ? `${integer(bounds.offset + 1)}–${integer(bounds.end)} / ${integer(bounds.total)} 字符` : '暂无正文';
    $('translationPrevBtn').disabled = bounds.offset <= 0;
    $('translationNextBtn').disabled = !bounds.hasMore;
    $('translateDocumentBtn').disabled = !state.scanId || !state.translationPath || page.status === 'completed' && page.full_translation;
    renderTranslationList();
  }

  async function loadTranslation(path, offset) {
    path = String(path || '').trim();
    if (!state.scanId || !path || state.translationLoading) return;
    state.translationLoading = true;
    state.translationPath = path;
    state.translationOffset = Math.max(0, numeric(offset));
    $('translationPath').value = path;
    window.sessionStorage.setItem(LAST_DOCUMENT_KEY, path);
    $('translationDocumentState').textContent = '正在读取文档页面…';
    $('translationDocumentState').className = 'v2-inline-state';
    $('translationOpenBtn').disabled = true;
    try {
      const query = new URLSearchParams({ path, view: state.translationView, offset: String(state.translationOffset), limit: String(PAGE_SIZE) });
      const response = await api(`/api/translation/${encodeURIComponent(state.scanId)}?${query.toString()}`);
      renderTranslationDocument(response);
    } catch (error) {
      $('translationDocumentState').textContent = error.message || '无法打开文档';
      $('translationDocumentState').className = 'v2-inline-state is-error';
      $('translationDocumentContent').innerHTML = emptyMarkup('文档未能打开', error.message || '请检查文件路径和解析状态。');
      $('translateDocumentBtn').disabled = !state.scanId || !state.translationPath;
    } finally {
      state.translationLoading = false;
      $('translationOpenBtn').disabled = !state.scanId;
    }
  }

  function translationJobState(job, label) {
    const host = $('translationDocumentState');
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

  async function translateDocument() {
    const path = state.translationPath || ($('translationPath').value || '').trim();
    if (!state.scanId || !path) { notify('请先打开一个文件', true); return; }
    const button = $('translateDocumentBtn');
    button.disabled = true;
    try {
      const response = await api(`/api/translation/${encodeURIComponent(state.scanId)}`, {
        method: 'POST', body: JSON.stringify({ path, require_full: true })
      });
      $('translationDocumentState').textContent = '当前文件已加入全文翻译队列。';
      watchJob(response.job_id, 'translation-document', (job) => translationJobState(job, '当前文件翻译'));
    } catch (error) {
      notify(error.message || '无法提交翻译', true);
      button.disabled = false;
    }
  }

  async function translatePackage(phase) {
    if (!state.scanId) return;
    const button = phase === 'deep_backfill' ? $('translateBackfillBtn') : $('translatePriorityBtn');
    button.disabled = true;
    try {
      const response = await api(`/api/translate-package/${encodeURIComponent(state.scanId)}`, {
        method: 'POST', body: JSON.stringify({ phase })
      });
      const label = phase === 'deep_backfill' ? '全部外文补齐' : '预览与重点文件翻译';
      $('translationDocumentState').textContent = `${label}已加入后台队列。`;
      watchJob(response.job_id, `translation-package-${phase}`, (job) => translationJobState(job, label));
      notify(`${label}已提交`);
    } catch (error) {
      notify(error.message || '无法提交数据包翻译', true);
    } finally { button.disabled = !state.scanId; }
  }

  function resetForScan(scanId) {
    state.scanId = scanId;
    state.overview = null;
    state.conversation = null;
    state.conversationList = [];
    state.turns.clear();
    state.translationItems = [];
    state.translationCounts = {};
    state.translationPath = '';
    state.translationPage = null;
    state.scopeConstraints = {};
    state.watchers.clear();
    if ($('conversationScopeKind')) { $('conversationScopeKind').value = 'package'; renderScopeFields(); }
    const enabled = Boolean(scanId);
    ['packageOverviewRefreshBtn', 'conversationNewBtn', 'translatePriorityBtn', 'translateBackfillBtn', 'translationOpenBtn'].forEach((id) => { if ($(id)) $(id).disabled = !enabled; });
    renderTranslationSummary();
    renderConversation();
    renderConversationList();
    if (!enabled) {
      setOverviewState('导入数据包后生成内容概览。');
      $('packageOverviewMetrics').innerHTML = '';
    }
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
    if (route === 'chat') loadConversationList();
    if (route === 'translation') {
      loadTranslationList();
      const remembered = state.translationPath || window.sessionStorage.getItem(LAST_DOCUMENT_KEY) || '';
      if (remembered) {
        $('translationPath').value = remembered;
        if (!state.translationPath) loadTranslation(remembered, 0);
      }
    }
  }

  function bind() {
    renderScopeFields();
    resetForScan((window.localStorage.getItem(CURRENT_SCAN_KEY) || '').trim());
    $('packageOverviewRefreshBtn').addEventListener('click', () => loadOverview(true));
    $('conversationScopeKind').addEventListener('change', () => { state.scopeConstraints = {}; renderScopeFields(); });
    document.querySelector('[data-view="overview"]').addEventListener('click', (event) => {
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
    $('conversationMessages').addEventListener('click', (event) => {
      const button = event.target.closest('[data-citation-translation]');
      if (!button) return;
      const path = button.dataset.citationTranslation;
      if (!path) return;
      state.translationPath = path;
      window.sessionStorage.setItem(LAST_DOCUMENT_KEY, path);
      window.SJFXShell && window.SJFXShell.activate('translation');
      loadTranslation(path, 0);
    });
    $('translationListRefreshBtn').addEventListener('click', loadTranslationList);
    $('translationFilter').addEventListener('change', renderTranslationList);
    $('translationList').addEventListener('click', (event) => {
      const button = event.target.closest('[data-translation-path]');
      if (button) loadTranslation(button.dataset.translationPath, 0);
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
        window.sessionStorage.setItem(LAST_DOCUMENT_KEY, row.dataset.path);
        if (!state.translationPath) $('translationPath').value = row.dataset.path;
      }
    }, true);
    window.setInterval(syncScan, 1200);
    activate(currentRoute());
  }

  window.SJFXEngineering = { activate, refreshOverview: () => loadOverview(true), openTranslation: (path) => loadTranslation(path, 0) };
  document.addEventListener('DOMContentLoaded', bind);
}());
