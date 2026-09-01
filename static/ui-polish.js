/* Small presentation helpers for the workbench shell.
 * No data or API state is owned here; the feature clients remain the source
 * of truth. This file only mirrors already-rendered status into shared chrome. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const routeTitles = {
    dashboard: 'SJFX · 工作台', packages: 'SJFX · 数据包管理',
    physical: 'SJFX · 原始目录', analysis: 'SJFX · 智能分析',
    homogeneous: 'SJFX · 同构文件关联', overview: 'SJFX · 数据包概览',
    evidence: 'SJFX · 证据问答', chat: 'SJFX · 资料问答',
    translation: 'SJFX · 全文翻译', tasks: 'SJFX · 任务中心',
    exports: 'SJFX · 导出中心', settings: 'SJFX · 系统设置'
  };
  const statusLabels = {
    idle: '等待导入', queued: '排队中', processing: '正在处理', running: '正在处理',
    cancelling: '正在安全暂停', paused: '已安全暂停', completed: '全部完成',
    failed: '需要处理', retry_waiting: '等待重试', partial: '部分完成',
    out_of_scope: '范围外', cancelled: '已取消'
  };
  const workflow = [
    ['packages', '资料'], ['overview', '概览'], ['analysis', '分析'],
    ['homogeneous', '关联'], ['evidence', '核验'], ['exports', '交付']
  ];
  const stageForRoute = {
    packages: 'packages', physical: 'packages', overview: 'overview', analysis: 'analysis',
    homogeneous: 'homogeneous', evidence: 'evidence', chat: 'evidence',
    translation: 'analysis', tasks: 'packages', exports: 'exports'
  };
  const quickRoutes = [
    ['dashboard', '工作台', '查看当前数据包与下一步'],
    ['packages', '数据包管理', '导入、继续、暂停或重试分析'],
    ['physical', '原始目录', '按原始路径浏览资料'],
    ['analysis', '智能分析', '按主题整理和深度分析资料'],
    ['overview', '数据包概览', '查看全局结构、质量与研究方向'],
    ['homogeneous', '同构文件关联', '核对信件、通知等结构相似文件'],
    ['evidence', '证据问答', '按范围检索原文证据'],
    ['chat', '资料问答', '针对资料提出多轮分析问题'],
    ['translation', '全文翻译', '保留原文并查看中文译文'],
    ['tasks', '任务中心', '跟踪后台队列与恢复任务'],
    ['exports', '导出中心', '整理并交付资料与分析成果'],
    ['settings', '系统设置', '查看本地模型、权限和解析边界']
  ];
  let syncFrame = 0;
  let quickNavigatorOpen = false;
  let quickNavigatorIndex = 0;
  let quickNavigatorReturnFocus = null;

  function ensureQuickNavigator() {
    if ($('quickNavigatorDialog')) return;
    const headerActions = document.querySelector('.header-actions');
    if (!headerActions) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.id = 'quickNavigatorBtn';
    button.className = 'header-command';
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', 'quickNavigatorDialog');
    button.title = '快速跳转模块（Ctrl + K）';
    button.innerHTML = '<span aria-hidden="true">⌕</span><span>快速跳转</span>';
    headerActions.insertBefore(button, headerActions.firstChild);

    const overlay = document.createElement('div');
    overlay.id = 'quickNavigatorOverlay';
    overlay.className = 'quick-navigator-overlay';
    overlay.hidden = true;
    overlay.innerHTML = `
      <section id="quickNavigatorDialog" class="quick-navigator" role="dialog" aria-modal="true" aria-labelledby="quickNavigatorTitle">
        <div class="quick-navigator-head">
          <div><span class="section-kicker">WORKSPACE NAVIGATION</span><h2 id="quickNavigatorTitle">前往模块</h2></div>
          <button type="button" class="quick-navigator-close" aria-label="关闭快速跳转" title="关闭">×</button>
        </div>
        <label class="sr-only" for="quickNavigatorInput">搜索模块</label>
        <div class="quick-navigator-input"><span aria-hidden="true">⌕</span><input id="quickNavigatorInput" type="search" autocomplete="off" placeholder="搜索模块，例如：证据、翻译、任务"></div>
        <div id="quickNavigatorResults" class="quick-navigator-results" role="listbox" aria-label="可前往的模块"></div>
      </section>`;
    document.body.appendChild(overlay);
    button.addEventListener('click', () => openQuickNavigator(button));
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay || event.target.closest('.quick-navigator-close')) closeQuickNavigator();
      const target = event.target.closest('[data-quick-route]');
      if (target) selectQuickRoute(target.dataset.quickRoute);
    });
    $('quickNavigatorInput')?.addEventListener('input', () => {
      quickNavigatorIndex = 0;
      renderQuickNavigatorResults();
    });
  }

  function matchingQuickRoutes() {
    const query = String($('quickNavigatorInput')?.value || '').trim().toLocaleLowerCase();
    if (!query) return quickRoutes;
    return quickRoutes.filter(([, label, description]) => `${label} ${description}`.toLocaleLowerCase().includes(query));
  }

  function renderQuickNavigatorResults() {
    const host = $('quickNavigatorResults');
    if (!host) return;
    const matches = matchingQuickRoutes();
    if (!matches.length) {
      host.innerHTML = '<div class="quick-navigator-empty">没有匹配的模块。请换一个关键词。</div>';
      return;
    }
    quickNavigatorIndex = Math.max(0, Math.min(quickNavigatorIndex, matches.length - 1));
    host.innerHTML = matches.map(([route, label, description], index) => `
      <button type="button" class="quick-navigator-result${index === quickNavigatorIndex ? ' is-active' : ''}" data-quick-route="${route}" role="option" aria-selected="${index === quickNavigatorIndex}">
        <span class="quick-navigator-index">${String(index + 1).padStart(2, '0')}</span>
        <span><b>${label}</b><small>${description}</small></span>
        <span class="quick-navigator-arrow" aria-hidden="true">→</span>
      </button>`).join('');
  }

  function openQuickNavigator(trigger) {
    ensureQuickNavigator();
    const overlay = $('quickNavigatorOverlay');
    if (!overlay) return;
    quickNavigatorReturnFocus = trigger || document.activeElement;
    quickNavigatorOpen = true;
    quickNavigatorIndex = 0;
    overlay.hidden = false;
    document.body.classList.add('quick-navigator-open');
    $('quickNavigatorBtn')?.setAttribute('aria-expanded', 'true');
    const input = $('quickNavigatorInput');
    if (input) input.value = '';
    renderQuickNavigatorResults();
    window.setTimeout(() => input?.focus(), 0);
  }

  function closeQuickNavigator() {
    if (!quickNavigatorOpen) return;
    quickNavigatorOpen = false;
    $('quickNavigatorOverlay').hidden = true;
    document.body.classList.remove('quick-navigator-open');
    $('quickNavigatorBtn')?.setAttribute('aria-expanded', 'false');
    const focusTarget = quickNavigatorReturnFocus;
    quickNavigatorReturnFocus = null;
    if (focusTarget && typeof focusTarget.focus === 'function') focusTarget.focus();
  }

  function selectQuickRoute(route) {
    closeQuickNavigator();
    window.SJFXShell?.activate(route);
  }

  function handleQuickNavigatorKeys(event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      if (quickNavigatorOpen) closeQuickNavigator();
      else openQuickNavigator(document.activeElement);
      return;
    }
    if (!quickNavigatorOpen) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeQuickNavigator();
      return;
    }
    const matches = matchingQuickRoutes();
    if (!matches.length) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      quickNavigatorIndex = (quickNavigatorIndex + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
      renderQuickNavigatorResults();
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      selectQuickRoute(matches[quickNavigatorIndex][0]);
      return;
    }
    if (event.key === 'Tab') {
      event.preventDefault();
      $('quickNavigatorInput')?.focus();
    }
  }

  function hasRealScan() {
    const shellState = window.SJFXShell?.workspace;
    if (shellState && typeof shellState.hasScan === 'boolean') return shellState.hasScan;
    // The persisted scan id is authoritative during transitions: scanStats
    // can still contain the previous package until the new API response
    // arrives.  Falling back to rendered stats keeps the shell usable when
    // storage is unavailable.
    try {
      if ((window.localStorage.getItem('sjfx_current_scan_id_v1') || '').trim()) return true;
    } catch (_) { /* fall through to the rendered-state check */ }
    const stats = $('scanStats');
    if (!stats || stats.classList.contains('empty')) return false;
    const text = stats.textContent.trim();
    return Boolean(text && !/尚未导入数据包/.test(text));
  }

  function metricText(label) {
    const host = $('scanStats');
    if (!host) return '';
    const row = [...host.querySelectorAll('.metric-grid > div')]
      .find((item) => item.querySelector('span')?.textContent.trim() === label);
    return row?.querySelector('b')?.textContent.trim() || '';
  }

  function processingMetric(label) {
    const host = document.querySelector('.package-processing-panel');
    if (!host) return '';
    const row = [...host.querySelectorAll('.package-processing-metrics > div')]
      .find((item) => item.querySelector('span')?.textContent.trim() === label);
    return row?.querySelector('b')?.textContent.trim() || '';
  }

  function processingState() {
    const chip = $('jobStatusChip');
    const packageChip = document.querySelector('.package-processing-panel .status-chip');
    const value = String(packageChip?.dataset.status || chip?.dataset.status || 'idle').toLowerCase();
    return value || 'idle';
  }

  function updateRail() {
    const rail = $('workspaceRail');
    if (!rail) return;
    const realScan = hasRealScan();
    const name = $('workspaceName')?.textContent.trim() || '';
    const root = $('rootPath')?.value.trim() || '';
    const workspaceName = realScan
      ? (name && name !== '尚未导入数据包'
        ? name
        : (root ? root.split(/[\\/]/).filter(Boolean).pop() : '数据包已连接'))
      : '尚未导入数据包';
    $('workspaceRailName').textContent = workspaceName || '尚未导入数据包';

    const inventory = processingMetric('全量盘点文件') || metricText('递归文件') || '—';
    const searchable = processingMetric('基础可搜索') || '';
    const deep = processingMetric('深析完成率') || '';
    const pendingMatch = document.querySelector('.package-processing-panel')?.textContent.match(/可立即处理\s*([\d,]+)/);
    const pending = pendingMatch ? pendingMatch[1] : '—';
    $('railInventory').textContent = inventory;
    $('railSearchable').textContent = searchable || (document.querySelector('.package-processing-panel') ? '—' : '—');
    const deepValue = deep || (metricText('内容解析') || '—');
    $('railDeep').textContent = deepValue;
    if ($('railDeepCompact')) $('railDeepCompact').textContent = deepValue;
    $('railPending').textContent = pending;

    const state = realScan ? processingState() : 'idle';
    rail.dataset.state = state;
    $('workspaceRailState').textContent = statusLabels[state] || '准备中';
    const meta = $('workspaceRailMeta');
    const progress = $('progressText')?.textContent.trim();
    if (meta) {
      meta.textContent = progress && !/导入后将按所选模式/.test(progress)
        ? progress.replace(/\s+/g, ' ').slice(0, 120)
        : (workspaceName === '尚未导入数据包' ? '导入后，这里会持续显示盘点、索引与深度分析进度' : '数据包已连接，选择左侧模块继续工作');
    }
  }

  function updateDashboardActivity() {
    const host = $('dashboardActivity');
    if (!host) return;
    const realScan = hasRealScan();
    const state = realScan ? processingState() : 'idle';
    const packagePanel = document.querySelector('.package-processing-panel');
    const task = document.querySelector('#activeTaskList .task-list-item, #activeTaskList .activity-task');
    if (packagePanel) {
      const pending = packagePanel.textContent.match(/可立即处理\s*([\d,]+)/)?.[1] || '0';
      const deep = processingMetric('深析完成率') || '—';
      host.className = 'activity-task';
      host.innerHTML = `<div><b>${statusLabels[state] || '处理中'}</b><small>深度分析 ${deep} · 待处理 ${pending}</small></div><button type="button" class="text-button" data-go-route="packages">打开数据包管理查看队列 →</button>`;
      return;
    }
    if (task) {
      host.className = 'activity-task';
      host.textContent = task.textContent.trim().replace(/\s+/g, ' ');
    }
  }

  function nextAction(route, state, hasScan) {
    if (!hasScan) return { route: 'packages', label: '导入数据包' };
    if (['running', 'queued', 'processing', 'cancelling'].includes(state)) return { route: 'packages', label: '查看进度' };
    if (['failed', 'retry_waiting', 'partial', 'paused'].includes(state)) return { route: 'packages', label: '处理异常' };
    const routeMap = {
      packages: ['overview', '查看全景'], physical: ['analysis', '进入智能分析'],
      overview: ['analysis', '查看智能目录'], analysis: ['homogeneous', '查看文件关联'],
      homogeneous: ['evidence', '核验证据'], evidence: ['chat', '进入资料问答'],
      chat: ['exports', '准备交付'], translation: ['chat', '进入资料问答'],
      tasks: ['packages', '返回数据包'], exports: ['analysis', '选择交付内容']
    };
    const next = routeMap[route] || ['overview', '查看全景'];
    return { route: next[0], label: next[1] };
  }

  function renderJourney() {
    const host = $('workspaceJourney');
    if (!host) return;
    const route = document.body.dataset.route || 'dashboard';
    const hasScan = hasRealScan();
    host.hidden = route === 'dashboard' || route === 'settings' || !hasScan;
    if (host.hidden) return;
    const state = hasScan ? processingState() : 'idle';
    const activeStage = stageForRoute[route] || 'packages';
    const activeIndex = workflow.findIndex(([key]) => key === activeStage);
    $('workspaceJourneyTrack').innerHTML = workflow.map(([key, label], index) => {
      const current = key === activeStage;
      const completed = hasScan && index < activeIndex;
      return `<button type="button" class="workspace-journey-step${current ? ' is-current' : ''}${completed ? ' is-complete' : ''}" data-workflow-route="${key}"${!hasScan && key !== 'packages' ? ' disabled' : ''}><i>${index + 1}</i><span>${label}</span></button>`;
    }).join('');
    const label = statusLabels[state] || '准备中';
    $('workspaceJourneyStatus').innerHTML = `<span class="workspace-journey-dot state-${state}"></span><strong>${label}</strong>`;
    const action = nextAction(route, state, hasScan);
    $('workspaceJourneyActions').innerHTML = `<button type="button" class="secondary workspace-journey-action" data-workflow-route="${action.route}">${action.label}</button>`;
  }

  function syncDashboardAction() {
    const button = document.querySelector('.large-action[data-go-route]');
    if (!button) return;
    if (!hasRealScan()) {
      button.dataset.goRoute = 'packages';
      button.textContent = '＋ 新建分析';
      return;
    }
    const action = nextAction('dashboard', processingState(), true);
    button.dataset.goRoute = action.route;
    button.textContent = `${action.label} →`;
  }

  function syncRouteChrome() {
    const route = document.body.dataset.route || 'dashboard';
    document.title = routeTitles[route] || routeTitles.dashboard;
    const sidebar = document.querySelector('.sidebar');
    const routeChanged = Boolean(sidebar && sidebar.dataset.lastRoute !== route);
    if (quickNavigatorOpen && routeChanged) closeQuickNavigator();
    if (routeChanged) {
      sidebar.scrollTop = 0;
      sidebar.dataset.lastRoute = route;
    }
    document.querySelectorAll('.nav-item[data-route]').forEach((item) => {
      if (item.dataset.route === route) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });
  }

  function syncOverviewEmpty() {
    const view = document.querySelector('[data-view="overview"]');
    if (!view) return;
    if (!hasRealScan()) {
      view.dataset.empty = 'true';
      return;
    }
    const mounts = ['packageOverviewMetrics', 'packageOverviewTreemap', 'packageOverviewTopics', 'packageOverviewTimeline', 'packageOverviewBriefSummary'];
    const hasContent = mounts.some((id) => {
      const node = $(id);
      return node && (node.children.length > 0 || node.textContent.trim());
    });
    view.dataset.empty = hasContent ? 'false' : 'true';
  }

  function syncNavigationDrawer() {
    const sidebar = document.querySelector('.sidebar');
    const open = sidebar?.classList.contains('open');
    if (open && window.matchMedia('(max-width: 760px)').matches && sidebar) sidebar.scrollTop = 0;
    document.body.classList.toggle('nav-open', Boolean(open));
    const toggle = $('mobileMenuBtn');
    if (toggle) {
      toggle.setAttribute('aria-expanded', String(Boolean(open)));
      toggle.setAttribute('aria-label', open ? '关闭导航' : '打开导航');
    }
  }

  function syncAll() {
    syncFrame = 0;
    syncRouteChrome();
    updateRail();
    updateDashboardActivity();
    syncDashboardAction();
    syncOverviewEmpty();
    syncNavigationDrawer();
    renderJourney();
  }

  function scheduleSync() {
    if (syncFrame) return;
    syncFrame = window.requestAnimationFrame(syncAll);
  }

  function handleDelegatedRoute(event) {
    const workflowTarget = event.target.closest('[data-workflow-route]');
    if (workflowTarget?.isConnected && !workflowTarget.disabled) {
      event.preventDefault();
      window.SJFXShell?.activate(workflowTarget.dataset.workflowRoute);
      return;
    }
    const target = event.target.closest('[data-go-route]');
    if (!target || !target.isConnected) return;
    if (target.closest('#dashboardActivity')) {
      event.preventDefault();
      window.SJFXShell?.activate(target.dataset.goRoute);
    }
  }

  function syncScrollChrome() {
    const scrolled = window.scrollY > 8;
    $('workspaceRail')?.classList.toggle('is-scrolled', scrolled);
    document.body.classList.toggle('page-scrolled', scrolled);
  }

  function bind() {
    ensureQuickNavigator();
    syncAll();
    syncScrollChrome();
    const routeObserver = new MutationObserver(scheduleSync);
    routeObserver.observe(document.body, { attributes: true, attributeFilter: ['data-route'] });
    const navObserver = new MutationObserver(syncNavigationDrawer);
    const sidebar = document.querySelector('.sidebar');
    if (sidebar) navObserver.observe(sidebar, { attributes: true, attributeFilter: ['class'] });
    const statusObserver = new MutationObserver(scheduleSync);
    ['scanStats', 'pipeline', 'jobStatusChip', 'workspaceName', 'activeTaskList'].forEach((id) => {
      const node = $(id);
      if (node) statusObserver.observe(node, { childList: true, subtree: true, characterData: true, attributes: true });
    });
    const overview = document.querySelector('[data-view="overview"]');
    if (overview) statusObserver.observe(overview, { childList: true, subtree: true, characterData: true });
    document.addEventListener('click', handleDelegatedRoute);
    $('workspaceRailToggle')?.addEventListener('click', () => {
      const rail = $('workspaceRail');
      if (!rail) return;
      const expanded = rail.dataset.expanded === 'true';
      rail.dataset.expanded = String(!expanded);
      $('workspaceRailToggle').setAttribute('aria-expanded', String(!expanded));
      $('workspaceRailToggle').setAttribute('aria-label', expanded ? '展开数据包处理详情' : '收起数据包处理详情');
    });
    window.addEventListener('sjfx-scan-changed', scheduleSync);
    window.addEventListener('sjfx-shell-state', scheduleSync);
    window.addEventListener('scroll', syncScrollChrome, { passive: true });
    document.addEventListener('keydown', handleQuickNavigatorKeys);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
