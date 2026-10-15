(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = () => window.sjfxApi;
  let packageId = '';
  let extraCatalogNodes = [];
  let files = [];
  const selectedForSummary = new Set();
  let currentStage = 'overview';
  let latestStatus = {};
  let workspaceStatus = null;
  let liveTimer = null;
  let retryTimer = null;

  function setText(id, value) { const el = $(id); if (el) el.textContent = value == null ? '' : value; }
  function catalogStatusMessage(item, fallback) {
    if (currentStage === 'catalog' && ['waiting_for_selection', 'completed'].includes(item.status) && String(item.message || '').includes('待确认')) return '内容目录可用，可查看和选择文件。';
    return item.message || fallback;
  }
  function setStage(stage) {
    ensureExplorer();
    currentStage = stage || 'overview';
    document.querySelector('.large-result-view')?.setAttribute('data-large-mode', currentStage);
    const titles = {
      catalog: ['大数据包目录', '选择目录或主题，查看关联文件与摘要。'],
      overview: ['大数据包情报概览', '基于智能目录与已有摘要形成初步简报；完成深度摘要后自动更新。'],
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
    const exportEntry = ensureExportCenterEntry();
    if (exportEntry) exportEntry.disabled = !packageId;
    renderSelection();
  }

  // 大数据包成果 → 导出中心。复用页面已有的两个约定，不新造路由：
  //  1) 切屏用 window.SJFXShell.activate('exports')（product-shell.js 的
  //     viewFor 已经把 exports 映射到导出中心这一屏）；
  //  2) "要看哪个大包"用 window.sjfxLargeResultId —— export-center.js 的
  //     loadSources() 本来就优先读它来选来源，其次读 localStorage 的
  //     sjfx_large_package_id，所以这里把两个都登记上。
  function openExportCenter() {
    if (packageId) {
      window.sjfxLargeResultId = packageId;
      try {
        window.localStorage.setItem('sjfx_large_package_id', packageId);
      } catch (_) {
        // 浏览器禁用存储时只影响"下次进入导出中心仍记得这个大包"。
      }
    }
    // 顺序不能反：导出中心若已经加载过数据源，activate('exports') 不会再读一次，
    // 下拉框会停在上一个大包上。先让它按这个大包重读（并切回大数据包这一栏）。
    const center = window.SJFXExportCenter;
    if (center && typeof center.openForLargePackage === 'function') center.openForLargePackage(packageId);
    else if (center && typeof center.reload === 'function') center.reload(); // 旧缓存版本的兜底
    window.SJFXShell?.activate('exports');
  }

  function ensureExportCenterEntry() {
    const existing = $('largeResultToExportCenter');
    if (existing) return existing;
    const anchor = $('largeResultToOverview') || $('largeResultToCatalog');
    if (!anchor || !anchor.parentElement) return null;
    const button = document.createElement('button');
    button.id = 'largeResultToExportCenter';
    button.type = 'button';
    button.className = 'secondary';
    button.textContent = '导出中心';
    button.title = '在导出中心勾选这个大数据包的智能目录，交付压缩包或转入普通数据包';
    button.addEventListener('click', openExportCenter);
    anchor.insertAdjacentElement('afterend', button);
    return button;
  }
  // Browsing state belongs only to this isolated large-package module.
  const newBrowseState = () => ({ node: { type: 'all', value: '', label: '整个数据包' }, query: '', draft: '', category: '', offset: 0, expanded: new Set(['topics', 'unclassified']), scroll: 0, listScroll: 0, file: null });
  let browseMode = 'physical';
  let browseStates = { physical: newBrowseState(), smart: newBrowseState() };
  let latestCatalog = null;
  let catalogFingerprint = '';
  let catalogContentFingerprint = '';
  let fileFingerprint = '';
  let detailFingerprint = '';
  let fileRequestId = 0;
  let loadRequestId = 0;
  let activationId = 0;
  let nodeInfo = new Map();
  const browse = () => browseStates[browseMode];
  const nodeKey = (type, value) => JSON.stringify([type, value || '']);
  const scopeLabels = { all: '全部资料', directory: '原始目录', topic: '内容分组', group: '内容分组', category: '资料类别', role: '文档用途', project: '项目 / 编号', entity: '参与者', date: '样本时间' };
  const folderIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9H3Z"/></svg>';
  const topicIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m12 3 2.8 6.2L21 12l-6.2 2.8L12 21l-2.8-6.2L3 12l6.2-2.8Z"/></svg>';

  function ensureExplorer() {
    if ($('largeResultExplorer')) return;
    const view = document.querySelector('.large-result-view');
    const nav = view?.querySelector('.large-result-catalog-grid');
    const detail = view?.querySelector('.large-result-files-card');
    if (!nav || !detail) return;
    const original = $('largeResultOriginalCatalog')?.closest('section');
    const smart = $('largeResultSmartCatalog')?.closest('section');
    if (!original || !smart) return;
    const workspace = document.createElement('div');
    workspace.id = 'largeResultExplorer';
    workspace.className = 'large-explorer large-result-section';
    workspace.dataset.largeSection = 'catalog';
    nav.before(workspace);
    workspace.append(nav, detail);
    nav.classList.add('large-explorer-nav');
    detail.classList.add('large-explorer-detail');
    original.id = 'largeResultPhysicalPane';
    smart.id = 'largeResultSmartPane';
    original.setAttribute('role', 'tabpanel');
    smart.setAttribute('role', 'tabpanel');
    original.setAttribute('aria-labelledby', 'largeResultPhysicalTab');
    smart.setAttribute('aria-labelledby', 'largeResultSmartTab');
    const heading = document.createElement('div');
    heading.className = 'lp-nav-heading';
    heading.innerHTML = '<span class="section-kicker">NAVIGATE</span><h2>资料目录</h2><div class="lp-mode-tabs" role="tablist" aria-label="大数据包目录模式"><button id="largeResultPhysicalTab" type="button" role="tab" aria-controls="largeResultPhysicalPane" data-lp-mode="physical">原始目录</button><button id="largeResultSmartTab" type="button" role="tab" aria-controls="largeResultSmartPane" data-lp-mode="smart">智能目录</button></div>';
    nav.prepend(heading);
    const title = detail.querySelector('.surface-heading h2');
    if (title) title.textContent = '节点详情';
    const hint = detail.querySelector('.surface-heading small');
    if (hint) hint.textContent = '查看当前范围内的文件与已有摘要';
    const scope = $('largeResultNodeSummary');
    if (scope) { scope.classList.add('lp-scope'); scope.setAttribute('aria-live', 'polite'); }
    const filters = detail.querySelector('.large-result-filters');
    if (filters) {
      filters.id = 'largeResultFilterBar';
      $('largeResultSearch')?.setAttribute('aria-label', '搜索当前范围内的文件');
      $('largeResultSearch')?.setAttribute('placeholder', '在当前范围搜索文件或摘要');
      $('largeResultCategory')?.setAttribute('aria-label', '按资料类别筛选');
      const clear = document.createElement('button');
      clear.id = 'largeResultClearSearch'; clear.type = 'button'; clear.className = 'ghost'; clear.textContent = '清空筛选';
      filters.append(clear);
    }
    const list = document.createElement('div');
    list.id = 'largeResultListView'; list.className = 'lp-list-view';
    if (filters) detail.insertBefore(list, filters); else detail.append(list);
    if (filters) list.append(filters);
    if ($('largeResultFiles')) list.append($('largeResultFiles'));
    const pager = $('largeResultPager') || document.createElement('div');
    pager.id = 'largeResultPager'; pager.className = 'lp-pager';
    list.append(pager);
    const fileDetail = document.createElement('div');
    fileDetail.id = 'largeResultFileDetail'; fileDetail.className = 'lp-file-detail'; fileDetail.hidden = true;
    detail.append(fileDetail);
    for (const id of ['largeResultOriginalCatalog', 'largeResultSmartCatalog']) {
      $(id)?.addEventListener('scroll', () => { browseStates[id === 'largeResultOriginalCatalog' ? 'physical' : 'smart'].scroll = $(id).scrollTop; }, { passive: true });
    }
    $('largeResultFiles')?.addEventListener('scroll', () => { browse().listScroll = $('largeResultFiles').scrollTop; }, { passive: true });
    syncBrowseUI();
  }

  function syncBrowseUI() {
    const state = browse();
    for (const mode of ['physical', 'smart']) {
      const tab = $(mode === 'physical' ? 'largeResultPhysicalTab' : 'largeResultSmartTab');
      const pane = $(mode === 'physical' ? 'largeResultPhysicalPane' : 'largeResultSmartPane');
      if (tab) { tab.setAttribute('aria-selected', String(mode === browseMode)); tab.tabIndex = mode === browseMode ? 0 : -1; }
      if (pane) pane.hidden = mode !== browseMode;
    }
    if ($('largeResultSearch')) $('largeResultSearch').value = state.draft;
    if ($('largeResultCategory')) $('largeResultCategory').value = state.category;
    $('largeResultExplorer')?.setAttribute('data-directory-mode', browseMode);
    markSelectedNode();
  }

  function markSelectedNode() {
    const state = browse();
    document.querySelectorAll('#largeResultExplorer [data-lp-node]').forEach(el => {
      const selected = el.dataset.lpType === state.node.type && el.dataset.lpValue === state.node.value;
      el.classList.toggle('is-selected', selected);
      if (selected) el.setAttribute('aria-current', 'true'); else el.removeAttribute('aria-current');
    });
  }

  function nodeButton(type, value, label, count, info = {}) {
    nodeInfo.set(nodeKey(type, value), { ...info, type, value, label, count });
    const status = window.SJFXLargePortrait?.nodeState(info) || '';
    return `<button type="button" class="lp-node${status ? ' lp-node-has-state' : ''}" data-lp-node="1" data-lp-type="${esc(type)}" data-lp-value="${esc(value)}" title="${esc(type === 'group' ? label : value || label)}">${type === 'directory' || type === 'all' ? folderIcon : topicIcon}<span class="lp-node-copy"><span class="lp-node-label">${esc(label)}</span>${status ? `<em class="lp-node-state">${esc(status)}</em>` : ''}</span>${count == null ? '' : `<small aria-label="${Number(count) || 0} 个文件">${Number(count) || 0}</small>`}</button>`;
  }

  function originalNavigation(catalog) {
    const root = { children: new Map() };
    for (const item of catalog.original?.directories || []) {
      let parent = root;
      const parts = String(item.path || '').split('/').filter(Boolean);
      parts.forEach((part, index) => {
        const path = parts.slice(0, index + 1).join('/');
        if (!parent.children.has(part)) parent.children.set(part, { path, label: part, children: new Map() });
        parent = parent.children.get(part);
      });
      if (parts.length) parent.info = item;
    }
    const rows = (branch) => [...branch.children.values()].map(item => {
      const key = 'dir:' + item.path;
      const open = browseStates.physical.expanded.has(key);
      const expand = item.children.size ? `<button type="button" class="lp-expand" data-lp-expand="${esc(key)}" aria-expanded="${open}" aria-label="${open ? '收起' : '展开'} ${esc(item.label)}"><span aria-hidden="true">›</span></button>` : '<span class="lp-expand-space"></span>';
      return `<li><div class="lp-nav-row">${expand}${nodeButton('directory', item.path, item.label, item.info?.files, item.info)}</div>${item.children.size ? `<ul class="lp-children" ${open ? '' : 'hidden'}>${rows(item)}</ul>` : ''}</li>`;
    }).join('');
    return `<div class="lp-all-node">${nodeButton('all', '', '整个数据包', catalog.files_total)}</div>${catalog.original?.root_files?.length ? `<div class="lp-root-files">${nodeButton('directory', '', '根目录文件', null, { sample: catalog.original.root_files })}</div>` : ''}<ul class="lp-tree">${rows(root)}</ul>${catalog.original?.truncated ? `<p class="lp-limit-note">${esc(catalog.original.notice || '目录显示数量已达上限，完整文件可搜索并翻页查看。')}</p>` : ''}`;
  }

  function smartNavigation(catalog) {
    const smart = catalog.smart || {};
    const summary = window.SJFXLargePortrait.catalogSummary(catalog, latestStatus);
    const labels = { pending: '待语义分组', grouped: '分组名称待完善', classified: '已按语义归类', no_content: '尚无可读内容', legacy: '尚无内容画像', identified: '已有内容线索', needs_more: '待补充采样', needs_ocr: '待文字识别', metadata_only: '尚无可读内容', restricted: '受限文件', unsupported: '暂不支持', failed: '快速解析失败' };
    const group = (key, title, content, count) => `<section class="lp-nav-group"><button type="button" class="lp-group-toggle" data-lp-group="${esc(key)}" aria-expanded="${browseStates.smart.expanded.has(key)}"><span class="lp-group-arrow" aria-hidden="true">›</span><span>${esc(title)}</span>${count == null ? '' : `<small>${esc(count)}</small>`}</button><div class="lp-group-content" ${browseStates.smart.expanded.has(key) ? '' : 'hidden'}>${content}</div></section>`;
    const byId=new Map([...(smart.navigation || []),...(smart.topics || [])].map(item=>[item.node_id,item]));
    const branchesById=new Map((smart.branches || []).map(b=>[b.node_id,b]));
    const nested=new Set(), renderedBranches=new Set();
    const branchHTML=(branch,visited=new Set())=>{
      if(visited.has(branch.node_id) || renderedBranches.has(branch.node_id))return '';
      renderedBranches.add(branch.node_id);
      const seen=new Set(visited);seen.add(branch.node_id);
      const key='theme:'+branch.node_id;
      const open=browseStates.smart.expanded.has(key);
      const children=branch.child_nodes || branch.children || [];
      const renderChild=id=>{
        const next=branchesById.get(id);
        if(next)return branchHTML(next,seen);
        const child=byId.get(id);if(!child || nested.has(id))return '';
        nested.add(id);
        return nodeButton('group',id,window.SJFXLargePortrait.nodeLabel(child),child.files,{...child,node_kind:window.SJFXLargePortrait.nodeKind(child)});
      };
      const sections=window.SJFXLargePortrait.branchSections(branch,byId);
      if (branch.node_id==='branch:unassigned' && sections.length) {
        // These are display containers, never semantic nodes or file scopes.
        // Keep an already-open aggregate scope valid across a catalog repaint.
        nodeInfo.set(nodeKey('group',branch.node_id),{...branch,type:'group',value:branch.node_id,label:'独立与未归入主题的分组',count:branch.files,node_kind:'independent',description:'保留现有内容分组与文件范围。'});
        return sections.map(s=>group('partition:'+s.key,s.label,s.ids.map(renderChild).join(''),`${s.files} 文件`)).join('');
      }
      const body=sections.length ? sections.map(s=>`<section><h5>${esc(s.label)} · ${s.files} 个文件</h5>${s.ids.map(renderChild).join('')}</section>`).join('') : children.map(renderChild).join('');
      const description=branch.node_id==='branch:unassigned'?'保留现有内容分组与文件范围。':'根据子目录共同内容组织，点击查看全部成员。';
      const parent=nodeButton('group',branch.node_id,window.SJFXLargePortrait.branchLabel(branch),branch.files,{...branch,description,node_kind:branch.kind==='independent'?'independent':'theme'});
      return `<div class="lp-content-branch${branch.kind==='pending' || (branch.status==='pending' && branch.kind!=='independent') ? ' lp-branch-pending' : ''}"><div class="lp-nav-row"><button type="button" class="lp-expand" data-lp-expand="${esc(key)}" aria-expanded="${open}" aria-label="${open?'收起':'展开'} ${esc(branch.name)}"><span aria-hidden="true">›</span></button>${parent}</div><div class="lp-content-children" ${open?'':'hidden'}>${body}</div></div>`;
    };
    const branches=(smart.branches || []).filter(b=>!b.parent_id || !branchesById.has(b.parent_id)).map(b=>branchHTML(b)).join('');
    const loose=[...byId.values()].filter(item=>!nested.has(item.node_id));
    const previews=loose.filter(item=>String(item.node_id || '').startsWith('preview:'));
    const topics=branches+loose.filter(item=>!previews.includes(item)).map(item=>nodeButton(item.node_id?'group':'topic',item.node_id || item.topic,window.SJFXLargePortrait.nodeLabel(item),item.files,{...item,node_kind:window.SJFXLargePortrait.nodeKind(item)})).join('');
    const categories = (smart.categories || []).map(item => nodeButton('category', item.category, item.category || '未分类', item.files, item)).join('');
    const facets = Object.entries(smart.facets || {}).filter(([kind]) => ['role', 'project', 'entity', 'date'].includes(kind)).map(([kind, items]) => group('facet:' + kind, scopeLabels[kind] + '线索', (items || []).map(item => nodeButton(kind, item.value, item.value, item.files, item)).join(''), items.length)).join('');
    const inventoryNotice = catalog.inventory?.inventory_error_count ? '<p class="lp-scope-notice">盘点有 ' + catalog.inventory.inventory_error_count + ' 处无法读取，当前目录仅覆盖已发现文件。</p>' : '';
    const publicationNotice = summary.publicationText ? `<p class="lp-catalog-publication">${esc(summary.publicationText)}</p>` : '';
    const namingInfo = inventoryNotice + publicationNotice + `<div class="lp-catalog-progress"><dl><div><dt>可读内容预览</dt><dd>${summary.content}<small>文件</small></dd></div><div><dt>叶子名称</dt><dd>${summary.named} / ${summary.leaves}<small>分组</small></dd></div><div><dt>主题构建</dt><dd>${summary.themes}<small>主题${summary.rootKnown ? ` · ${summary.roots} 顶层` : ''}</small></dd></div></dl><p class="lp-catalog-message" role="status">${esc(summary.message)}</p><p class="lp-catalog-coverage">文件归组 ${summary.grouped} / ${summary.total}${summary.ungrouped ? ` · 未归组 ${summary.ungrouped}` : ''}${summary.pending ? ` · 待上层主题整理 ${summary.pending}` : ''}${summary.stagingFiles ? ` · 暂存 ${summary.stagingFiles}` : ''}${summary.quickFailed ? ` · 快速解析失败 ${summary.quickFailed}` : ''}</p><button type="button" data-lp-improve ${summary.canImprove ? '' : 'disabled'}${summary.stopped ? ' title="继续处理数据包后可完善目录名称"' : ''}>完善目录名称</button></div><p class="lp-count-guide">主题 → 内容分组 → 文件。节点右侧为文件数，上下级数量不相加。</p>`;
    const states = Object.entries(smart.catalog_states || smart.recognition || {}).filter(([key])=>!['deferred','needs_review'].includes(key)).map(([key, value]) => `<span>${esc(labels[key] || key)} <b>${Number(value) || 0}</b></span>`).join('');
    const unknown = summary.ungrouped ? group('unclassified', '未归组文件', previews.map(item=>nodeButton('group',item.node_id,item.topic || item.name,item.files,{...item,node_kind:'unclassified'})).join('') || '<p class="lp-empty-note">未归组文件可从原始目录查看；更多条目详情尚未加载。</p>', `${summary.ungrouped} 文件`) : '';
    return `<div class="lp-all-node">${nodeButton('all', '', '整个数据包', catalog.files_total)}</div>${namingInfo}${group('topics', '主题与分组', topics || '<p class="lp-empty-note">尚未生成内容分组，已盘点文件可从原始目录查看。</p>')}${unknown}${smart.topics_truncated ? '<button type="button" data-lp-more>加载更多分组详情</button>' : ''}${group('categories', '资料类别', categories, `${(smart.categories || []).length} 类`)}${facets}${group('states', '目录整理状态（文件数）', `<div class="lp-catalog-states">${states || '尚无内容识别结果。'}</div>`)} `;
  }

  function renderCatalog(catalog) {
    if (!catalog) return;
    ensureExplorer();
    const contentFingerprint = JSON.stringify([catalog.smart?.navigation || [], catalog.smart?.branches || [], catalog.smart?.stats || {}, catalog.smart?.topic_total]);
    // Loaded detail pages belong to this directory revision. Old pages must not
    // override a renamed/merged live node or resurrect a removed preview scope.
    if (catalogContentFingerprint && catalogContentFingerprint !== contentFingerprint) extraCatalogNodes = [];
    catalogContentFingerprint = contentFingerprint;
    if (extraCatalogNodes.length) {
      const found = new Map((catalog.smart?.topics || []).map(n => [n.node_id || n.topic, n]));
      extraCatalogNodes.forEach(n => { if (!found.has(n.node_id || n.topic)) found.set(n.node_id || n.topic,n); });
      catalog = {...catalog,smart:{...catalog.smart,topics:[...found.values()],topics_truncated:found.size < catalog.smart.topic_total}};
    }
    latestCatalog = catalog;
    setText('largeResultRootPath', catalog.root_path || '');
    setText('largeResultDirectoryStats', `${catalog.files_total || 0} 个文件 · ${(catalog.original?.directories || []).length} 个目录${catalog.original?.truncated ? '（部分显示）' : ''}`);
    setText('largeResultSmartStats', window.SJFXLargePortrait.catalogSummary(catalog, latestStatus).text);
    const fingerprint = JSON.stringify([catalog, latestStatus.status]);
    if (fingerprint !== catalogFingerprint) {
      catalogFingerprint = fingerprint;
      nodeInfo = new Map();
      const active = document.activeElement;
      const focused = active?.dataset.lpNode ? { type: active.dataset.lpType, value: active.dataset.lpValue } : null;
      const focusedToggle = active?.dataset.lpExpand || active?.dataset.lpGroup;
      $('largeResultOriginalCatalog').innerHTML = originalNavigation(catalog);
      $('largeResultSmartCatalog').innerHTML = smartNavigation(catalog);
      const smartState = browseStates.smart;
      if (['topic','group'].includes(smartState.node.type) && !nodeInfo.has(nodeKey(smartState.node.type, smartState.node.value)) && !catalog.smart?.topics_truncated) {
        smartState.node = { type: 'all', value: '', label: '整个数据包' };
        smartState.offset = 0;
        smartState.notice = '原主题已重新整理，当前显示整个数据包。';
      }
      markSelectedNode();
      $('largeResultOriginalCatalog').scrollTop = browseStates.physical.scroll;
      $('largeResultSmartCatalog').scrollTop = browseStates.smart.scroll;
      if (focused) [...document.querySelectorAll('#largeResultExplorer [data-lp-node]')].find(el => el.dataset.lpType === focused.type && el.dataset.lpValue === focused.value)?.focus({ preventScroll: true });
      else if (focusedToggle) [...document.querySelectorAll('#largeResultExplorer [data-lp-expand], #largeResultExplorer [data-lp-group]')].find(el => (el.dataset.lpExpand || el.dataset.lpGroup) === focusedToggle)?.focus({ preventScroll: true });
    }
    renderScope();
  }

  function renderScope(total) {
    const state = browse();
    if (total != null) state.total = total;
    const info = nodeInfo.get(nodeKey(state.node.type, state.node.value)) || state.node;
    const label = info.label || state.node.label;
    const namingText = {model:'依据代表内容由模型命名',extractive:'原文内容名称，尚可完善',stale:'内容已更新，名称待复核',manual:'已人工确认',unread:'尚未完成内容分组',failed:'快速解析失败；文件仍保留，可查看失败原因'}[info.naming_status] || '';
    const basis = [info.description || info.basis, namingText].filter(Boolean).join(' · ') || (state.node.type === 'directory' ? '文件按原始路径归属，包含该目录下的子目录文件。' : '');
    const samples = (info.sample || []).map(item => typeof item === 'string' ? `<li>${esc(item)}</li>` : `<li><b>${esc(item.path)}</b>${item.basis ? `<p>${esc(item.basis)}</p>` : ''}${(item.evidence || []).map(e => `<p><span>${esc(e.location)}</span>：${esc(e.text)}</p>`).join('')}</li>`).join('');
    const details = basis || samples ? `<details class="lp-scope-evidence"><summary>节点信息与代表文件</summary>${basis ? `<p>${esc(basis)}</p>` : ''}${samples ? `<ul>${samples}</ul>` : ''}</details>` : '';
    const kindLabel = {theme:'内容主题',leaf:'叶子分组',independent:'独立分组',unclassified:'未归组文件',staging:'暂存文件'}[info.node_kind] || scopeLabels[state.node.type] || '当前范围';
    const content = `<div class="lp-scope-top"><div><span class="lp-scope-kind">${esc(kindLabel)}</span><h3 title="${esc(state.node.value || label)}">${esc(label)}</h3></div><button id="largeResultClearScope" type="button" class="ghost">查看全包</button></div>${state.node.value && state.node.type === 'directory' ? `<p class="lp-scope-path">${esc(state.node.value)}</p>` : ''}${info.description ? `<p class="lp-scope-description">${esc(info.description)}</p>` : ''}${info.error ? `<p class="lp-file-error">${esc(info.error)}</p>` : ''}<p class="lp-scope-count">${state.total == null ? '正在读取文件…' : `当前范围匹配 ${state.total} 个文件`}${state.query || state.category ? ' · 已应用筛选' : ''}</p>${state.notice ? `<p class="lp-scope-notice">${esc(state.notice)}</p>` : ''}${details}${state.node.type === 'group' && !state.node.value.startsWith('preview:') && !state.node.value.startsWith('branch:') && !state.node.value.startsWith('topic:') ? '<button type="button" data-lp-rename>确认或修改名称</button>' : ''}`;
    const host = $('largeResultNodeSummary');
    if (host && host._lpMarkup !== content) {
      const wasOpen = host.querySelector('details')?.open;
      host.innerHTML = content; host._lpMarkup = content;
      if (wasOpen && host.querySelector('details')) host.querySelector('details').open = true;
    }
  }

  function summaryData(item) {
    const deep = item.deep_summary || {}, local = item.local_summary || {};
    return { deep, local, type: deep.summary_type === 'model' ? '模型摘要' : (deep.summary_type ? '深度本地摘要' : '本地摘要'), text: deep.summary || local.sample || local.notice || item.error || '暂无摘要' };
  }
  function fileStatus(item) {
    if (item.deep_status === 'completed') return '已深度解析';
    if (item.deep_status === 'running' || item.deep_status === 'queued') return '深度解析中';
    if (item.deep_status === 'failed') return '深度解析失败';
    if (item.quick_status === 'failed') return '快速解析失败';
    return item.quick_status === 'completed' ? '已有本地摘要' : '已盘点';
  }
  function renderFiles(rows) {
    files = rows || [];
    const box = $('largeResultFiles');
    if (!box) return;
    const fingerprint = JSON.stringify([files, Array.from(selectedForSummary), latestStatus.status, currentStage]);
    if (fingerprint !== fileFingerprint) {
      fileFingerprint = fingerprint;
      const focusedPath = document.activeElement?.dataset.lpFile;
      const scroll = browse().listScroll;
      box.innerHTML = files.map(item => {
        const summary = summaryData(item);
        const parts = String(item.path || '').split('/');
        return `<article class="lp-file-row" style="display:flex;align-items:flex-start">${currentStage === 'catalog' ? `<input style="flex:0 0 auto;width:18px;margin:18px 0 0 14px" type="checkbox" data-catalog-select="${esc(item.path)}" aria-label="选择 ${esc(item.path)} 进入深度摘要" ${selectedForSummary.has(item.path) ? 'checked' : ''} ${!['completed','waiting_for_selection'].includes(latestStatus.status) || ['queued','running'].includes(item.deep_status) ? 'disabled' : ''}>` : ''}<button class="lp-file-open" type="button" data-lp-file="${esc(item.path)}"><span class="lp-file-name">${esc(parts.pop())}</span><span class="lp-file-path">${esc(item.path)}</span><span class="lp-file-tags">${esc(item.catalog_name || item.category || '未分类')} · ${esc(summary.type)} · ${esc(fileStatus(item))}</span><span class="lp-file-preview">${esc(summary.text)}</span><span class="lp-file-action">查看详情 <span aria-hidden="true">→</span></span></button></article>`;
      }).join('') || '<div class="large-empty">当前范围没有匹配文件，可清空筛选或查看全包。</div>';
      box.scrollTop = scroll;
      if (focusedPath) [...box.querySelectorAll('[data-lp-file]')].find(el => el.dataset.lpFile === focusedPath)?.focus({ preventScroll: true });
    }
    const state = browse();
    if (state.file) {
      const updated = files.find(item => item.path === state.file.path);
      if (updated) state.file = updated;
    }
    renderFileDetail();
    renderSelection();
  }

  function renderSelection() {
    let bar = $('largeCatalogSelection');
    if (!bar && $('largeResultFiles')) {
      bar = document.createElement('div'); bar.id = 'largeCatalogSelection'; bar.className = 'button-row';
      $('largeResultFiles').before(bar);
      bar.innerHTML = '<button type="button" id="largeCatalogSelectPage">选择本页</button><button type="button" id="largeCatalogClear">清空选择</button><span id="largeCatalogSelectedCount"></span><button type="button" id="largeCatalogStartDeep">开始深度摘要</button>';
      $('largeCatalogSelectPage').onclick = () => { files.filter(x => !['queued','running'].includes(x.deep_status)).forEach(x => selectedForSummary.add(x.path)); renderFiles(files); };
      $('largeCatalogClear').onclick = () => { selectedForSummary.clear(); renderFiles(files); };
      $('largeCatalogStartDeep').onclick = async () => {
        if (!selectedForSummary.size) return;
        const button = $('largeCatalogStartDeep'); button.disabled = true;
        try {
          const targetId = packageId;
          await api()(`/api/large-packages/${encodeURIComponent(targetId)}/select`, {method:'POST', body:JSON.stringify({paths:Array.from(selectedForSummary),rule:{kind:'semantic_catalog_selection'}})});
          if (targetId !== packageId) return;
          selectedForSummary.clear();
          setText('largeResultStatus', '已提交选中文件；深度摘要完成后将更新目录和概览。');
          await load();
        } catch (error) { setText('largeResultStatus', error.message || '提交深度摘要失败'); }
        finally { renderSelection(); }
      };
    }
    if (!bar) return;
    bar.hidden = currentStage !== 'catalog';
    const ready = ['completed','waiting_for_selection'].includes(latestStatus.status);
    $('largeCatalogSelectPage').disabled = !ready;
    $('largeCatalogStartDeep').disabled = !ready || selectedForSummary.size === 0;
    setText('largeCatalogSelectedCount', `已选择 ${selectedForSummary.size} 个文件`);
  }
  function renderFileDetail() {
    const item = browse().file;
    const host = $('largeResultFileDetail');
    if (!host) return;
    host.hidden = !item;
    if ($('largeResultListView')) $('largeResultListView').hidden = Boolean(item);
    if (!item) { detailFingerprint = ''; return; }
    const fingerprint = JSON.stringify(item);
    if (fingerprint === detailFingerprint) return;
    detailFingerprint = fingerprint;
    const sameFile = host.dataset.filePath === item.path;
    const detailKey = el => el.classList.contains('large-portrait') ? 'portrait' : el.querySelector('summary')?.textContent;
    const openDetails = new Set(sameFile ? [...host.querySelectorAll('details')].filter(el => el.open).map(detailKey) : []);
    const scroll = sameFile ? host.scrollTop : 0;
    host.dataset.filePath = item.path;
    const { deep, local, type, text } = summaryData(item);
    const currentPortrait = local.portrait ? {...local.portrait,
      catalog_status: item.catalog_state || local.portrait.catalog_status,
      topic: item.catalog_name || local.portrait.topic} : null;
    const basis = local.portrait?.catalog_version ? (local.portrait.catalog_source === 'deep_summary' ? '目录依据：深度摘要' : '目录依据：文件内容') : `置信度 ${Math.round(Number(item.confidence || 0) * 100)}%`;
    host.innerHTML = `<button id="largeResultBackToFiles" type="button" class="ghost lp-back">← 返回文件列表</button><div class="lp-file-detail-heading"><span class="lp-scope-kind">文件详情</span><h3>${esc(String(item.path || '').split('/').pop())}</h3><p>${esc(item.path)}</p><div class="lp-detail-tags"><span>${esc(item.category || '未分类')}</span><span>${esc(fileStatus(item))}</span><span>${esc(basis)}</span></div></div><section class="lp-summary-block"><h4>${esc(type)}</h4><p>${esc(text)}</p></section>${deep.summary && (local.sample || local.notice) ? `<details class="lp-summary-extra"><summary>本地摘要</summary><p>${esc(local.sample || local.notice)}</p></details>` : ''}${window.SJFXLargePortrait?.portrait(currentPortrait) || ''}${deep.text_preview ? `<details class="lp-summary-extra"><summary>已有正文预览</summary><p>${esc(deep.text_preview)}</p></details>` : ''}${item.error ? `<p class="lp-file-error">${esc(item.error)}</p>` : ''}`;
    host.querySelectorAll('details').forEach(el => { if (openDetails.has(detailKey(el))) el.open = true; });
    host.scrollTop = scroll;
  }

  async function loadFiles() {
    if (!packageId) return;
    const requestId = ++fileRequestId;
    const id = packageId, mode = browseMode, stage = currentStage, state = browse();
    const node = state.node;
    const params = new URLSearchParams({ limit: '100', offset: String(state.offset), q: state.query, category: node.type === 'category' ? node.value : state.category, topic: node.type === 'topic' ? node.value : '' });
    if (node.type === 'directory') params.set('directory', node.value);
    if (node.type === 'group') params.set('node_id', node.value);
    if (['role', 'project', 'entity', 'date'].includes(node.type)) { params.set('facet_kind', node.type); params.set('facet_value', node.value); }
    const isCurrent = () => requestId === fileRequestId && id === packageId && mode === browseMode && stage === currentStage && state === browse();
    $('largeResultListView')?.setAttribute('aria-busy', 'true');
    try {
      const data = await api()(`/api/large-packages/${encodeURIComponent(id)}/files?${params}`);
      if (!isCurrent()) return;
      const total = Number(data.total || 0);
      if (state.offset >= total && state.offset > 0) { state.offset = total ? Math.floor((total - 1) / 100) * 100 : 0; return await loadFiles(); }
      renderFiles(data.files || []);
      setText('largeResultFileStats', `匹配 ${total} 个 · 本页 ${files.length} 个`);
      renderScope(total);
      const pager = $('largeResultPager');
      if (pager) {
        const markup = window.SJFXLargePortrait.pager('largeResultPage', state.offset, 100, total);
        if (pager.dataset.markup !== markup) { pager.innerHTML = markup; pager.dataset.markup = markup; }
      }
    } catch (error) {
      if (isCurrent()) { setText('largeResultFileStats', '读取失败，请重试'); state.notice = error.message || '文件摘要读取失败'; renderScope(); }
    } finally {
      if (isCurrent()) $('largeResultListView')?.setAttribute('aria-busy', 'false');
    }
  }

  function prepareFileNavigation() {
    fileRequestId++; files = []; fileFingerprint = '';
    if ($('largeResultFiles')) $('largeResultFiles').innerHTML = '<div class="large-empty">正在读取文件…</div>';
    setText('largeResultFileStats', '正在读取文件…');
    $('largeResultPager')?.querySelectorAll('button').forEach(button => { button.disabled = true; });
    if ($('largeResultPager')) delete $('largeResultPager').dataset.markup;
  }

  function switchBrowseMode(mode) {
    if (!['physical', 'smart'].includes(mode) || mode === browseMode) return;
    browse().draft = $('largeResultSearch')?.value || '';
    browseMode = mode;
    fileRequestId++; fileFingerprint = ''; detailFingerprint = '';
    syncBrowseUI(); renderScope(); renderFileDetail();
    prepareFileNavigation(); loadFiles();
  }

  function selectBrowseNode(type, value) {
    const state = browse();
    const info = nodeInfo.get(nodeKey(type, value));
    state.node = { type, value, label: info?.label || value || '整个数据包' };
    state.query = ''; state.draft = ''; state.category = type === 'category' ? value : ''; state.offset = 0; state.total = null; state.file = null; state.notice = ''; state.listScroll = 0;
    fileRequestId++; fileFingerprint = ''; detailFingerprint = '';
    syncBrowseUI(); renderScope(); renderFileDetail();
    prepareFileNavigation(); loadFiles();
  }

  function resetBrowser() {
    window.SJFXIntelligenceOverview?.clear('largeIntelligenceBoard');
    selectedForSummary.clear();
    browseMode = 'physical'; browseStates = { physical: newBrowseState(), smart: newBrowseState() };
    latestCatalog = null; files = []; nodeInfo = new Map();
    catalogFingerprint = ''; catalogContentFingerprint = ''; fileFingerprint = ''; detailFingerprint = '';
    fileRequestId++; loadRequestId++; clearTimeout(liveTimer); clearInterval(retryTimer);
    for (const id of ['largeResultOriginalCatalog', 'largeResultSmartCatalog', 'largeResultFiles']) if ($(id)) $(id).innerHTML = '<div class="large-empty">正在读取数据包…</div>';
    if ($('largeResultPager')) $('largeResultPager').innerHTML = '';
    if ($('largeResultCategory')) $('largeResultCategory').innerHTML = '<option value="">全部分类</option>';
    setText('largeResultFileStats', '正在读取文件…');
    syncBrowseUI(); renderScope(); renderFileDetail();
  }

  function bindExplorer() {
    ensureExplorer();
    $('largeResultSearch')?.addEventListener('input', () => { browse().draft = $('largeResultSearch').value; });
    const search = () => {
      const state = browse();
      state.query = $('largeResultSearch')?.value.trim() || ''; state.draft = $('largeResultSearch')?.value || ''; state.category = $('largeResultCategory')?.value || ''; state.offset = 0; state.listScroll = 0; state.notice = ''; state.total = null; state.file = null;
      if (state.node.type === 'category' && state.node.value !== state.category) state.node = { type: 'all', value: '', label: '整个数据包' };
      markSelectedNode(); renderScope(); renderFileDetail(); prepareFileNavigation(); loadFiles();
    };
    $('largeResultSearchBtn')?.addEventListener('click', search);
    $('largeResultSearch')?.addEventListener('keydown', event => { if (event.key === 'Enter') search(); });
    $('largeResultCategory')?.addEventListener('change', search);
    $('largeResultClearSearch')?.addEventListener('click', () => { if ($('largeResultSearch')) $('largeResultSearch').value = ''; if ($('largeResultCategory')) $('largeResultCategory').value = browse().node.type === 'category' ? browse().node.value : ''; search(); });
    $('largeResultExplorer')?.addEventListener('keydown', event => {
      if (!event.target.closest('[role="tab"]')) return;
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const mode = event.key === 'Home' ? 'physical' : event.key === 'End' ? 'smart' : browseMode === 'physical' ? 'smart' : 'physical';
        switchBrowseMode(mode); $(mode === 'physical' ? 'largeResultPhysicalTab' : 'largeResultSmartTab')?.focus();
      }
    });
    $('largeResultExplorer')?.addEventListener('click', event => {
      const mode = event.target.closest('[data-lp-mode]');
      if (mode) { switchBrowseMode(mode.dataset.lpMode); return; }
      const expand = event.target.closest('[data-lp-expand]');
      if (expand) {
        const state = browse(), key = expand.dataset.lpExpand, open = !state.expanded.has(key);
        if (open) state.expanded.add(key); else state.expanded.delete(key);
        expand.setAttribute('aria-expanded', String(open));
        expand.setAttribute('aria-label', `${open ? '收起' : '展开'} ${expand.parentElement.querySelector('.lp-node')?.textContent || '目录'}`);
        const children = expand.parentElement.nextElementSibling;
        if (children) children.hidden = !open;
        return;
      }
      const group = event.target.closest('[data-lp-group]');
      if (group) {
        const key = group.dataset.lpGroup, open = !browse().expanded.has(key);
        if (open) browse().expanded.add(key); else browse().expanded.delete(key);
        group.setAttribute('aria-expanded', String(open)); group.nextElementSibling.hidden = !open; return;
      }
      const node = event.target.closest('[data-lp-node]');
      if (node) { selectBrowseNode(node.dataset.lpType, node.dataset.lpValue); return; }
      if (event.target.closest('#largeResultClearScope')) { selectBrowseNode('all', ''); return; }
      const file = event.target.closest('[data-lp-file]');
      if (file) { browse().file = files.find(item => item.path === file.dataset.lpFile) || null; detailFingerprint = ''; renderFileDetail(); $('largeResultBackToFiles')?.focus({ preventScroll: true }); return; }
      if (event.target.closest('#largeResultBackToFiles')) {
        const path = browse().file?.path; browse().file = null; renderFileDetail();
        if ($('largeResultFiles')) $('largeResultFiles').scrollTop = browse().listScroll;
        [...document.querySelectorAll('#largeResultFiles [data-lp-file]')].find(el => el.dataset.lpFile === path)?.focus({ preventScroll: true }); return;
      }
      const page = event.target.closest('#largeResultPagePrev, #largeResultPageNext');
      if (page && !page.disabled) { browse().offset = Math.max(0, browse().offset + (page.id.endsWith('Next') ? 100 : -100)); browse().listScroll = 0; prepareFileNavigation(); loadFiles(); }
    });
  }

  // 「目录校验未通过，当前显示上一版」横幅。概览阶段由情报阅读面板渲染同一条提示，这里只在
  // 面板不可用（或目录阶段）时显示，避免同一页出现两条。
  function renderCatalogRejection(rejection) {
    let banner = $('largeResultCatalogRejection');
    if (!rejection) { if (banner) banner.hidden = true; return; }
    const anchor = $('largeResultStatus');
    if (!banner && anchor) {
      banner = document.createElement('p'); banner.id = 'largeResultCatalogRejection';
      banner.className = 'large-result-catalog-rejection'; banner.setAttribute('role', 'status');
      banner.setAttribute('aria-live', 'polite'); anchor.after(banner);
    }
    if (!banner) return;
    const seconds = Number(rejection.created || 0);
    const when = seconds > 0 ? new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
    const detail = [rejection.reason ? `原因：${rejection.reason}` : '', when ? `时间：${when}` : ''].filter(Boolean).join('；');
    banner.textContent = `⚠ 目录校验未通过，当前显示上一版${detail ? `（${detail}）` : ''}`;
    banner.hidden = false;
  }

  function renderResearchBrief(report) {
    const brief = report.research_brief || {};
    const intelligence = window.SJFXIntelligenceOverview;
    if (intelligence) intelligence.mount('largeIntelligenceBoard', $('largeResultOverviewStatus')?.closest('.large-result-overview'), intelligence.model('large', report, brief));
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
    const preliminary = Boolean(brief.preliminary || (brief.available && !brief.ready));
    if (directionHost) directionHost.innerHTML = direction.title
      ? `<h4>${esc(direction.title)}</h4>${preliminary ? '<p class="lp-brief-stage">初步研究方向：完成深度摘要后将自动更新。</p>' : ''}${direction.coverage_note ? `<p class="lp-brief-stage">${esc(direction.coverage_note)}</p>` : ''}<p>${esc(direction.rationale || '该方向仍需结合代表性资料继续验证。')}</p>${direction.package_profile ? `<p>${esc(direction.package_profile)}</p>` : ''}${list(direction.research_questions, '暂无建议问题')}`
      : `<p>${esc(brief.empty_direction_reason || '当前尚未形成首选研究方向。')}</p>`;
    const details = $('largeResultResearchDetails');
    if (details) {
      const methods = list(direction.methods, '暂无建议方法');
      const files = list(direction.representative_documents, '暂无代表文件');
      details.innerHTML = `<section><h3>建议研究方法</h3>${methods}</section><section><h3>代表文件</h3>${files}</section>`;
    }
    const limitations = $('largeResultLimitations');
    if (limitations) limitations.innerHTML = list(brief.limitations, '当前未记录额外限制');
    const ready = Boolean(brief.available || brief.ready);
    ['largeResultDownloadDocx', 'largeResultDownloadJson'].forEach((id) => {
      const button = $(id);
      if (button) button.disabled = !ready;
    });
    setText('largeResultDownloadStatus', ready ? (preliminary ? '初步研究简报已生成，可下载；完成深度摘要后将自动更新。' : '研究简报已生成，可下载概览文件。') : (brief.empty_direction_reason || '快速解析形成研究方向后即可下载简报。'));
  }

  function renderControls(item, snapshot = null) {
    latestStatus = item || {};
    workspaceStatus = { packageId, item: latestStatus, workflow: snapshot };
    window.dispatchEvent(new CustomEvent('sjfx-large-state'));
    const status = String(item.status || '').toLowerCase();
    const active = status === 'queued' || status === 'running';
    const paused = status === 'paused';
    const actions = snapshot?.allowed_actions;
    ['largeResultPauseBtn', 'largePackagePauseBtn'].forEach((id) => { const b = $(id); if (b) b.hidden = !(actions ? actions.pause : active); });
    ['largeResultResumeBtn', 'largePackageResumeBtn'].forEach((id) => { const b = $(id); if (b) b.hidden = !(actions ? actions.resume : paused); });
    ['largeResultCancelBtn', 'largePackageCancelBtn'].forEach((id) => { const b = $(id); if (b) b.hidden = !(actions ? actions.cancel : active); });
    // 暂停后双入口：继续深度摘要（上方"继续处理"）+ 生成情报概览（基于已完成部分）。
    const deepCompleted = Number(latestStatus?.counts?.deep?.completed || 0);
    const briefButton = $('largeResultBuildBrief');
    if (briefButton) briefButton.hidden = !(paused && deepCompleted > 0);
  }

  async function control(action) {
    if (!packageId) return;
    if (action === 'cancel' && !window.confirm('停止后会保留已完成结果，但未完成文件不会继续处理。确定停止这个数据包吗？')) return;
    const endpoint = action === 'pause' ? 'pause' : action === 'resume' ? 'resume' : 'cancel';
    const targetId = packageId;
    const response = await api()(`/api/large-packages/${encodeURIComponent(targetId)}/${endpoint}`, { method: 'POST' });
    if (targetId !== packageId) return;
    const snap = response.snapshot || null;
    const item = Object.assign({}, response.large_package || {}, snap ? {
      status: snap.status, phase: snap.phase, progress: snap.progress, message: snap.message,
    } : {});
    renderControls(item, snap);
    setText('largeResultStatus', `${catalogStatusMessage(item, '状态已更新')} · ${item.phase || '—'} · ${item.progress || 0}%`);
    if (endpoint === 'cancel') setText('largeResultDownloadStatus', '任务已停止，已完成结果仍然保留。');
    await load();
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
    const requestId = ++loadRequestId, id = packageId, stage = currentStage;
    const isCurrent = () => requestId === loadRequestId && id === packageId && stage === currentStage;
    clearTimeout(liveTimer);
    const status = await api()(`/api/large-packages/${encodeURIComponent(id)}`);
    if (!isCurrent()) return;
    // 统一口径：/workflow 快照是权威；counts 仍取 large_package（快照不带明细）。
    const snap = status.workflow || null;
    const item = Object.assign({}, status.large_package || {}, snap ? {
      status: snap.status, phase: snap.phase, progress: snap.progress, message: snap.message,
    } : {});
    renderControls(item, snap);
    let recognize = $('largeResultRecognize');
    if (!recognize && $('largeResultRefreshBtn')) {
      recognize = document.createElement('button'); recognize.id = 'largeResultRecognize'; recognize.type = 'button'; recognize.textContent = '补充内容识别';
      $('largeResultRefreshBtn').after(recognize);
      recognize.onclick = async () => {
        recognize.disabled = true;
        try {
          const targetId = packageId;
          const result = await api()(`/api/large-packages/${encodeURIComponent(targetId)}/recognize`, {method:'POST'});
          if (targetId !== packageId) return;
          setText('largeResultStatus', `已排队 ${result.queued_files || 0} 个文件；每轮追加识别有时间预算，目录将继续更新。`);
          clearTimeout(liveTimer); liveTimer = setTimeout(() => load().catch(error => setText('largeResultStatus',error.message)), 1500);
        } catch (error) { setText('largeResultStatus', error.message); }
        finally { recognize.disabled = false; }
      };
    }
    // 「补充内容识别」是否可用也以 allowed_actions 为准（后端最清楚能不能排队）。
    if (recognize) recognize.disabled = snap && snap.allowed_actions
      ? !snap.allowed_actions.recognize
      : !['completed', 'waiting_for_selection'].includes(item.status);
    const pendingNote = snap && Number(snap.pending_total || 0)
      ? ` · 待办 阻塞 ${Number(snap.pending_blocking || 0)} / 可选 ${Number(snap.pending_optional || 0)}` : '';
    const catalogNote = snap
      ? ` · 目录 ${snap.catalog_state === 'published' ? '已发布' : (snap.catalog_state || '无')}` +
        (snap.catalog_id ? `(${String(snap.catalog_id).slice(4, 12)})` : '') +
        (snap.catalog_degraded ? ` · ⚠ 降级目录（未经语义聚类）` : '')
      : '';
    const importState = snap || item;
    const importNote = importState.import_completed
      ? '大数据包导入已完成（100%）；深度摘要可按需选择' : `大数据包导入进度 ${importState.import_progress ?? item.progress ?? 0}%`;
    const deepNote = item.phase === 'deep_parse' ? ` · 深度摘要进度 ${item.progress || 0}%` : '';
    setText('largeResultStatus', `${importNote}${deepNote} · ${catalogStatusMessage(item, '大数据包成果')}${pendingNote}${catalogNote}`);
    setText('largeResultPackageId', packageId);
    const c = item.counts || {};
    const reportPayload = currentStage === 'catalog' ? {report:{coverage:{inventory_files:c.total_files,quick_completed:c.quick?.completed,deep_completed:c.deep?.completed},categories:c.categories || {}}} : await api()(`/api/large-packages/${encodeURIComponent(packageId)}/report`);
    if (!isCurrent()) return;
    const report = reportPayload.report || {};
    // 目录校验拒绝了新目录、仍显示上一版时的提示：报告接口顶层字段优先，其次取 workflow 快照。
    const catalogRejection = reportPayload.catalog_rejection || (snap && snap.catalog_rejection) || null;
    if (stage === 'overview') report.catalog_rejection = catalogRejection;
    renderCatalogRejection(stage === 'overview' && window.SJFXIntelligenceOverview ? null : catalogRejection);
    if (stage === 'overview') renderResearchBrief(report);
    const coverage = report.coverage || {};
    setText('largeResultInventory', coverage.inventory_files || 0);
    setText('largeResultQuick', coverage.quick_completed || 0);
    setText('largeResultDeep', coverage.deep_completed || 0);
    setText('largeResultModel', coverage.model_summaries || 0);
    const failuresTotal = Number((report.research_brief || {}).failures_total ?? coverage.failed_files_total ?? (report.failures || []).length) || 0;
    setText('largeResultFailure', failuresTotal);
    const overviewReady = snap ? Boolean(snap.published_view || snap.overview_state === 'published' || item.status === 'completed') : item.status === 'completed';
    setText('largeResultOverviewStatus', overviewReady ? 'REPORT READY' : (item.phase || 'PROCESSING'));
    const remaining = Math.max(0, Number(coverage.quick_completed || 0) - Number(coverage.deep_completed || 0));
    setText('largeResultOverviewSummary', `本数据包共盘点 ${coverage.inventory_files || 0} 个文件，已完成 ${coverage.quick_completed || 0} 个本地快速摘要；已深度解析 ${coverage.deep_completed || 0} 个，其中 ${coverage.model_summaries || 0} 个生成了模型摘要。${remaining ? `还有 ${remaining} 个文件可随时补充深度解析。` : '当前没有待补充文件。'}`);
    const categories = report.categories || {};
    const categoryEntries = Object.entries(categories).sort((a, b) => Number(b[1]) - Number(a[1]));
    const categoryTotal = Math.max(1, Number(coverage.inventory_files || 0));
    if ($('largeResultCategorySummary')) $('largeResultCategorySummary').innerHTML = categoryEntries.map(([category, count]) => `<div class="large-result-category-row"><span>${esc(category)}</span><i><b style="width:${Math.min(100, Number(count) / categoryTotal * 100)}%"></b></i><em>${count}</em></div>`).join('') || '<div class="large-empty">暂无分类统计。</div>';
    const failures = report.failures || [];
    if ($('largeResultWarnings')) $('largeResultWarnings').innerHTML = failures.length ? `<p>有 ${Math.max(failuresTotal, failures.length)} 个文件需要复核或重新处理${failuresTotal > failures.length ? `（下方列出前 ${failures.length} 条）` : ''}。</p><ul>${failures.slice(0, 8).map((failure) => `<li>${esc(failure.path || '')}${failure.error ? `：${esc(failure.error)}` : ''}</li>`).join('')}</ul>` : '<p>当前没有快速解析失败项。</p><p>未进入深度解析的文件仍然只保留本地摘要。</p>';
    const retry = $('largeResultRetryFailed');
    if (retry) retry.hidden = !failures.length || !['completed', 'waiting_for_selection'].includes(item.status);
    const select = $('largeResultCategory');
    if (select) {
      const selectedCategory = browse().category;
      const categoryNames = Object.keys(categories);
      if (selectedCategory && !categoryNames.includes(selectedCategory)) categoryNames.push(selectedCategory);
      const options = '<option value="">全部分类</option>' + categoryNames.map(category => `<option value="${esc(category)}">${esc(category)}${categories[category] == null ? '' : ` (${categories[category]})`}</option>`).join('');
      if (select.dataset.options !== options) { select.innerHTML = options; select.dataset.options = options; }
      select.value = selectedCategory;
    }
    const catalog = (await api()(`/api/large-packages/${encodeURIComponent(id)}/catalog`)).catalog;
    if (!isCurrent()) return;
    renderCatalog(catalog);
    const directoryPending = (catalog?.smart?.naming?.active ? 1 : 0) + Number(catalog?.smart?.catalog_states?.pending || 0) + Number(catalog?.smart?.catalog_states?.grouped || 0) + Number(catalog?.smart?.pending_groups || 0);
    if (stage === 'catalog') await loadFiles();
    if (!isCurrent()) return;
    clearTimeout(liveTimer);
    if (['queued','running'].includes(item.status) || (directoryPending > 0 && ['completed','waiting_for_selection'].includes(item.status))) {
      liveTimer = setTimeout(() => { load().catch(error => { if (id === packageId && stage === currentStage) setText('largeResultStatus', error.message); }); }, 5000);
    }
  }

  async function retryFailed() {
    if (!packageId) return;
    const targetId = packageId;
    const response = await api()(`/api/large-packages/${encodeURIComponent(targetId)}/retry-failed`, { method: 'POST' });
    if (targetId !== packageId) return;
    setText('largeResultDownloadStatus', response.retry_files ? `已重新排队 ${response.retry_files} 个失败文件，正在处理。` : '当前没有可重试的失败文件。');
    clearInterval(retryTimer);
    if (response.retry_files) {
      retryTimer = window.setInterval(async () => {
        if (targetId !== packageId) { window.clearInterval(retryTimer); return; }
        try {
          const status = await api()(`/api/large-packages/${encodeURIComponent(targetId)}`);
          if (targetId !== packageId) return;
          await load();
          if (['waiting_for_selection', 'completed', 'failed', 'cancelled'].includes(status.large_package?.status)) window.clearInterval(retryTimer);
        } catch (_) { window.clearInterval(retryTimer); }
      }, 2000);
    }
  }
  async function activate(id, stage = currentStage) {
    const requestId = ++activationId;
    let nextId = String(id || packageId || '');
    if (!nextId) {
      try {
        const history = await api()('/api/large-packages/history?limit=1');
        if (requestId !== activationId) return;
        const latest = (history.items || [])[0];
        nextId = String(latest?.large_package_id || latest?.scan_id || '');
      } catch (error) {
        if (requestId === activationId) setText('largeResultStatus', error.message || '大数据包历史读取失败');
        return;
      }
    }
    ensureExplorer();
    if (nextId !== packageId) { packageId = nextId; extraCatalogNodes = []; resetBrowser(); }
    setStage(stage);
    if (!packageId) {
      setText('largeResultStatus', '暂无大数据包成果，请先创建一个大数据包任务');
      return;
    }
    window.sjfxLargeResultId = packageId;
    if (window.SJFXShell && !['large-catalog', 'large-overview', 'large-result'].includes(document.body.dataset.route)) {
      window.SJFXShell.activate(currentStage === 'catalog' ? 'large-catalog' : 'large-overview'); return;
    }
    document.querySelectorAll('.app-view').forEach(el => el.classList.toggle('active', el.dataset.view === 'large-result'));
    try { await load(); } catch (error) { if (requestId === activationId) setText('largeResultStatus', error.message || '大数据包成果读取失败'); }
  }
  window.SJFXLargeResult = { activate,
    workspace: () => workspaceStatus?.packageId === packageId ? workspaceStatus : null };
  window.addEventListener('sjfx-shell-state', event => {
    if (!['large-catalog', 'large-overview', 'large-result'].includes(event.detail?.route)) {
      clearTimeout(liveTimer); clearInterval(retryTimer); loadRequestId++; fileRequestId++; activationId++;
    }
  });
  document.addEventListener('click', async event => {
    const more = event.target.closest?.('[data-lp-more]');
    if (more && packageId) {
      more.disabled=true;const id=packageId, revision=catalogContentFingerprint;
      try {
        const offset=(latestCatalog?.smart?.topics || []).length;
        const result=await api()('/api/large-packages/'+encodeURIComponent(id)+'/catalog/nodes?offset='+offset+'&limit=200');
        if (id===packageId && revision===catalogContentFingerprint) { extraCatalogNodes.push(...result.topics);renderCatalog(latestCatalog); }
      } catch(error) { setText('largeResultStatus',error.message);more.disabled=false; }
      return;
    }
    const rename=event.target.closest?.('[data-lp-rename]');
    if (rename && packageId) {
      const node=browse().node;const info=nodeInfo.get(nodeKey(node.type,node.value)) || node;
      const name=window.prompt('输入根据内容确认的目录名称。保存后自动更新将保留此名称。',info.label || node.label);
      if (name==null)return;
      try { await api()('/api/large-packages/'+encodeURIComponent(packageId)+'/catalog/nodes/'+encodeURIComponent(node.value)+'/name',{method:'POST',body:JSON.stringify({name})});extraCatalogNodes=[];await load(); }
      catch(error) {setText('largeResultStatus',error.message);}
      return;
    }
    const improve = event.target.closest?.('[data-lp-improve]');
    if (!improve || !packageId) return;
    improve.disabled = true;
    const id = packageId;
    const request_key = improve.dataset.requestKey || ('name-' + Date.now() + '-' + Math.random().toString(36).slice(2));
    improve.dataset.requestKey = request_key;
    try {
      await api()('/api/large-packages/' + encodeURIComponent(id) + '/catalog/improve', {method:'POST',body:JSON.stringify({request_key})});
      if (id === packageId) await load();
    } catch (error) { setText('largeResultStatus',error.message); improve.disabled=false; }
  });
  document.addEventListener('change', event => {
    const input = event.target.closest?.('[data-catalog-select]');
    if (!input) return;
    if (input.checked) selectedForSummary.add(input.dataset.catalogSelect); else selectedForSummary.delete(input.dataset.catalogSelect);
    renderSelection();
  });
  document.addEventListener('DOMContentLoaded', () => {
    bindExplorer();
    $('largeResultRefreshBtn')?.addEventListener('click', () => activate(packageId));
    $('largeResultToImport')?.addEventListener('click', () => window.SJFXShell?.activate('large-import'));
    $('largeResultToSelect')?.addEventListener('click', () => {
      if (window.openLargePackageHistory && packageId) window.openLargePackageHistory(packageId, 'select');
      else window.SJFXShell?.activate('large-select');
    });
    $('largeResultSwitchPackage')?.addEventListener('click', () => window.SJFXDataSources?.open('large') || window.SJFXShell?.activate('packages'));
    $('largeResultPauseBtn')?.addEventListener('click', async () => { try { await control('pause'); } catch (error) { setText('largeResultStatus', error.message || '暂停失败'); } });
    $('largeResultResumeBtn')?.addEventListener('click', async () => { try { await control('resume'); } catch (error) { setText('largeResultStatus', error.message || '继续失败'); } });
    $('largeResultBuildBrief')?.addEventListener('click', async () => {
      if (!packageId) return;
      const button = $('largeResultBuildBrief');
      if (button) { button.disabled = true; button.textContent = '正在生成…'; }
      const targetId = packageId;
      try {
        let queued = false;
        try {
          await api()(`/api/large-packages/${encodeURIComponent(targetId)}/report/rebuild`, { method: 'POST' });
          queued = true;
        } catch (error) {
          // 409：包尚未完成深度摘要，概览由目录里程碑自动生成；只刷新当前版本。
          if (!/深度摘要完成后/.test(String(error.message || ''))) throw error;
        }
        if (queued) {
          if (button) button.textContent = '正在重新生成…';
          for (let attempt = 0; attempt < 40 && targetId === packageId; attempt += 1) {
            await new Promise(resolve => setTimeout(resolve, 3000));
            const state = await api()(`/api/large-packages/${encodeURIComponent(targetId)}/report/rebuild`);
            if (!state.pending) break;
          }
        } else {
          await api()(`/api/large-packages/${encodeURIComponent(targetId)}/report`);
        }
        if (targetId !== packageId) return;
        await activate(targetId, 'overview');
        if (window.toast) window.toast(queued ? '情报概览已重新生成（基于已完成的深度摘要）' : '情报概览已刷新');
      } catch (error) { setText('largeResultStatus', error.message || '情报概览生成失败'); }
      finally { if (button) { button.disabled = false; button.textContent = '生成情报概览'; } }
    });
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
