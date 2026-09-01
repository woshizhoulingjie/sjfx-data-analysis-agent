/* Product shell: navigation, focused workspaces and mirrors for existing
 * analysis widgets.  The analysis client remains in app.js; this layer only
 * changes where the user sees those capabilities. */
(function () {
  const routeNames = {
    dashboard: ['工作台', '数据概览'], packages: ['数据包', '导入与任务'],
    physical: ['原始目录', '物理资料树'], analysis: ['智能分析', '主题与证据'],
    homogeneous: ['同构文件关联', '台账与事项脉络'],
    chat: ['资料问答', '持续对话'], translation: ['全文翻译', '原文与中文'],
    evidence: ['证据问答', '可追溯检索'], overview: ['数据包概览', '内容地图'],
    exports: ['导出中心', '交接成果'], tasks: ['任务中心', '运行状态'],
    settings: ['系统设置', '本地运行环境']
  };
  const viewFor = { dashboard: 'dashboard', packages: 'packages', physical: 'explore', analysis: 'explore', homogeneous: 'homogeneous', chat: 'chat', translation: 'translation', evidence: 'evidence', overview: 'overview', exports: 'exports', tasks: 'tasks', settings: 'settings' };
  let activeRoute = 'dashboard';
  let navigationRestoreTarget = null;

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function storedScanId() {
    try { return (window.localStorage.getItem('sjfx_current_scan_id_v1') || '').trim(); }
    catch (_) { return ''; }
  }

  function hasRealScan() {
    // The durable scan id changes before every feature panel has finished
    // repainting. Prefer it so the shell never briefly reports the previous
    // package as the current workspace during a package switch.
    if (storedScanId()) return true;
    const stats = $('scanStats');
    if (!stats || stats.classList.contains('empty')) return false;
    const text = stats.textContent.trim();
    return Boolean(text && !/尚未导入数据包/.test(text));
  }

  function isCompactNavigation() {
    return window.matchMedia('(max-width: 760px)').matches;
  }

  function publishWorkspaceState() {
    window.dispatchEvent(new CustomEvent('sjfx-shell-state', {
      detail: { hasScan: hasRealScan(), scanId: storedScanId(), route: activeRoute }
    }));
  }

  function mirror(sourceId, targetId) {
    const source = $(sourceId); const target = $(targetId);
    if (!source || !target) return;
    const copy = () => {
      target.innerHTML = source.innerHTML;
      target.className = source.className.replace(/\bhidden-result\b/g, '').trim();
      target.style.display = source.classList.contains('hidden-result') ? 'none' : '';
    };
    new MutationObserver(copy).observe(source, { childList: true, subtree: true, characterData: true, attributes: true });
    copy();
  }

  function syncDashboard() {
    const stats = $('scanStats');
    const text = stats?.textContent || '';
    const metricValue = (label) => {
      const row = [...(stats?.querySelectorAll('.metric-grid > div') || [])]
        .find((item) => item.querySelector('span')?.textContent.trim() === label);
      return row?.querySelector('b')?.textContent.trim() || '';
    };
    const file = metricValue('递归文件');
    const size = metricValue('总大小');
    const coverage = (text.match(/内容解析\s*(\d+(?:\.\d+)?%|—)/) || [])[1] || '';
    const value = ((text.match(/研究潜力\s*([^·；\n]+)/) || [])[1] || '').trim();
    if ($('dashboardFileCount') && file) $('dashboardFileCount').textContent = file;
    if ($('dashboardSize') && size) $('dashboardSize').textContent = size;
    if ($('dashboardCoverage') && coverage) $('dashboardCoverage').textContent = coverage;
    if ($('dashboardValue') && value) $('dashboardValue').textContent = value;
    const root = $('rootPath');
    const realScan = hasRealScan();
    if ($('workspaceName')) {
      if (realScan && root?.value) {
        const parts = root.value.replace(/[\\/]+$/, '').split(/[\\/]/);
        $('workspaceName').textContent = parts[parts.length - 1] || root.value;
      } else {
        $('workspaceName').textContent = '尚未导入数据包';
      }
    }
    publishWorkspaceState();
  }

  function setNavigationOpen(open, restoreFocus = true) {
    const sidebar = document.querySelector('.sidebar');
    const toggle = $('mobileMenuBtn');
    const backdrop = $('mobileNavBackdrop');
    if (!sidebar) return;
    open = Boolean(open) && isCompactNavigation();
    if (open && !sidebar.classList.contains('open')) {
      const candidate = document.activeElement;
      navigationRestoreTarget = candidate instanceof HTMLElement ? candidate : toggle;
    }
    sidebar.classList.toggle('open', Boolean(open));
    sidebar.setAttribute('aria-modal', open ? 'true' : 'false');
    document.body.classList.toggle('nav-open', open);
    if (backdrop) {
      backdrop.classList.toggle('open', open);
      backdrop.setAttribute('aria-hidden', String(!open));
      backdrop.tabIndex = open ? 0 : -1;
    }
    if (toggle) {
      toggle.setAttribute('aria-expanded', String(Boolean(open)));
      toggle.setAttribute('aria-label', open ? '关闭导航' : '打开导航');
    }
    if (open) {
      window.requestAnimationFrame(() => {
        const preferred = sidebar.querySelector('.nav-item.active') || sidebar.querySelector('button:not(:disabled)');
        preferred?.focus();
      });
    } else if (restoreFocus && navigationRestoreTarget?.isConnected) {
      const target = navigationRestoreTarget;
      navigationRestoreTarget = null;
      window.requestAnimationFrame(() => target.focus());
    }
  }

  function trapNavigationFocus(event) {
    const sidebar = document.querySelector('.sidebar');
    if (!isCompactNavigation() || !sidebar?.classList.contains('open') || event.key !== 'Tab') return;
    const focusable = [...sidebar.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
      .filter((element) => !element.hasAttribute('hidden') && element.offsetParent !== null);
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function ensureTaskControls() {
    const packageActions = document.querySelector('.task-card .task-actions');
    if (packageActions && !$('cancelJobBtn')) {
      const button = document.createElement('button');
      button.id = 'cancelJobBtn';
      button.className = 'danger';
      button.disabled = true;
      button.textContent = '取消当前任务';
      packageActions.prepend(button);
    }
    const chip = document.querySelector('.task-card .status-chip');
    if (chip && !chip.id) {
      chip.id = 'jobStatusChip';
      chip.textContent = 'IDLE';
    }
    const mount = $('taskPipelineMount');
    if (mount && !$('taskCenterProgressBar')) {
      mount.innerHTML = '<div class="progress-track"><div id="taskCenterProgressBar" class="progress-bar"></div></div>'
        + '<div id="taskCenterProgressText">当前没有运行中的任务。</div>';
    }
    if (mount) {
      const main = mount.closest('.task-center-main');
      const routeButton = main?.querySelector('[data-go-route="packages"]');
      if (main && routeButton && !$('taskCenterCancelBtn')) {
        const actions = document.createElement('div');
        actions.className = 'task-actions';
        const cancel = document.createElement('button');
        cancel.id = 'taskCenterCancelBtn';
        cancel.className = 'danger';
        cancel.disabled = true;
        cancel.textContent = '取消当前任务';
        routeButton.before(actions);
        actions.append(cancel, routeButton);
      }
    }
    const recovery = document.querySelector('.task-center-side');
    if (recovery && !recovery.querySelector('.task-safety-note, .task-recovery-points')) {
      const note = document.createElement('p');
      note.className = 'task-safety-note';
      note.textContent = '取消后会停止提交新文件并终止等待中的隔离解析进程；已经完成的文件检查点会保留。';
      recovery.appendChild(note);
    }
    if (!document.querySelector('link[data-task-controls]')) {
      const style = document.createElement('link');
      style.rel = 'stylesheet';
      style.href = '/static/task-controls.css?v=2';
      style.dataset.taskControls = '1';
      document.head.appendChild(style);
    }
  }

  function activate(route) {
    route = routeNames[route] ? route : 'dashboard';
    const routeChanged = activeRoute !== route;
    activeRoute = route;
    const view = viewFor[route];
    document.body.dataset.route = route;
    document.querySelectorAll('.app-view').forEach((el) => el.classList.toggle('active', el.dataset.view === view));
    document.querySelectorAll('.nav-item').forEach((el) => el.classList.toggle('active', el.dataset.route === route));
    const labels = routeNames[route];
    if ($('breadcrumbRoot')) $('breadcrumbRoot').textContent = labels[0];
    if ($('breadcrumbCurrent')) $('breadcrumbCurrent').textContent = labels[1];
    if (route === 'physical' && $('physicalTreeBtn') && !$('physicalTreeBtn').disabled) $('physicalTreeBtn').click();
    if (route === 'analysis' && $('analysisTreeBtn') && !$('analysisTreeBtn').disabled) $('analysisTreeBtn').click();
    if ($('exploreTitle')) $('exploreTitle').textContent = route === 'analysis' ? '智能分析目录' : '原始目录';
    if ($('exploreSubtitle')) $('exploreSubtitle').textContent = route === 'analysis' ? '从主题到子方向、文档和证据逐层下钻。' : '确认真实资料结构，原始目录不会被语义分类覆盖。';
    if (route === 'tasks') window.SJFXTasks?.refresh();
    if (route === 'homogeneous') window.SJFXHomogeneous?.activate();
    window.SJFXEngineering?.activate(route);
    syncDashboard();
    // Route changes should reveal the new module from its beginning. A smooth
    // page-level scroll leaves the next view half-way down during navigation,
    // especially on mobile; module-local panes retain their own scroll state.
    if (routeChanged) window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    setNavigationOpen(false, false);
    publishWorkspaceState();
  }

  function bind() {
    ensureTaskControls();
    document.querySelectorAll('[data-route]').forEach((el) => el.addEventListener('click', () => activate(el.dataset.route)));
    document.querySelectorAll('[data-go-route]').forEach((el) => el.addEventListener('click', () => activate(el.dataset.goRoute)));
    document.querySelectorAll('[data-forward]').forEach((el) => el.addEventListener('click', () => $(el.dataset.forward)?.click()));
    $('rootPath')?.addEventListener('input', syncDashboard);
    $('scanBtn')?.addEventListener('click', syncDashboard, true);
    $('mobileMenuBtn')?.addEventListener('click', () => {
      const sidebar = document.querySelector('.sidebar');
      setNavigationOpen(!sidebar?.classList.contains('open'));
    });
    $('mobileNavBackdrop')?.addEventListener('click', () => setNavigationOpen(false));
    document.querySelector('.main-area')?.addEventListener('click', (event) => {
      const sidebar = document.querySelector('.sidebar');
      if (sidebar?.classList.contains('open') && !event.target.closest('#mobileMenuBtn')) setNavigationOpen(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') setNavigationOpen(false);
      trapNavigationFocus(event);
    });
    window.addEventListener('resize', () => {
      if (!isCompactNavigation()) setNavigationOpen(false, false);
    }, { passive: true });
    const resetToken = () => { window.sessionStorage.removeItem('sjfx_api_token'); window.location.reload(); };
    $('headerTokenBtn')?.addEventListener('click', resetToken);
    $('headerTokenBtnSecondary')?.addEventListener('click', resetToken);
    const stats = $('scanStats');
    if (stats) new MutationObserver(syncDashboard).observe(stats, { childList: true, subtree: true, characterData: true });
    window.addEventListener('sjfx-scan-changed', syncDashboard);
    mirror('reportResult', 'overviewResultMount');
    mirror('summary', 'evidenceResultMount');
    syncDashboard();
  }

  window.SJFXShell = {
    activate,
    get route() { return activeRoute; },
    get workspace() { return { hasScan: hasRealScan(), scanId: storedScanId() }; }
  };
  document.addEventListener('DOMContentLoaded', bind);
})();
