/* Product shell: navigation, focused workspaces and mirrors for existing
 * analysis widgets.  The analysis client remains in app.js; this layer only
 * changes where the user sees those capabilities. */
(function () {
  const routeNames = {
    dashboard: ['工作台', '数据概览'], packages: ['数据包', '导入与任务'],
    physical: ['原始目录', '物理资料树'], analysis: ['智能分析', '主题与证据'],
    evidence: ['证据问答', '可追溯检索'], overview: ['情况概览', '价值与发现'],
    exports: ['导出中心', '交接成果'], tasks: ['任务中心', '运行状态'],
    settings: ['系统设置', '本地运行环境']
  };
  const viewFor = { dashboard: 'dashboard', packages: 'packages', physical: 'explore', analysis: 'explore', evidence: 'evidence', overview: 'overview', exports: 'exports', tasks: 'tasks', settings: 'settings' };
  let activeRoute = 'dashboard';

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function mirror(sourceId, targetId) {
    const source = $(sourceId); const target = $(targetId);
    if (!source || !target) return;
    const copy = () => { target.innerHTML = source.innerHTML; target.className = source.className.replace(/\bhidden-result\b/g, '').trim(); };
    new MutationObserver(copy).observe(source, { childList: true, subtree: true, characterData: true, attributes: true });
    copy();
  }

  function syncDashboard() {
    const stats = $('scanStats');
    const text = stats?.textContent || '';
    const file = text.match(/(\d+)\s*递归文件/);
    const size = text.match(/([^；\n]+)\s*总大小/);
    const coverage = text.match(/已分析\s*(\d+\/\d+)（(\d+(?:\.\d+)?)%）/);
    const value = text.match(/价值判断：([^（;；]+)/);
    if ($('dashboardFileCount') && file) $('dashboardFileCount').textContent = file[1];
    if ($('dashboardSize') && size) $('dashboardSize').textContent = size[1].trim();
    if ($('dashboardCoverage') && coverage) $('dashboardCoverage').textContent = coverage[2] + '%';
    if ($('dashboardValue') && value) $('dashboardValue').textContent = value[1].trim();
    const root = $('rootPath');
    if ($('workspaceName') && root?.value) {
      const parts = root.value.replace(/[\\/]+$/, '').split(/[\\/]/);
      $('workspaceName').textContent = parts[parts.length - 1] || root.value;
    }
  }

  function activate(route) {
    route = routeNames[route] ? route : 'dashboard';
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
    syncDashboard();
    window.scrollTo({ top: 0, behavior: 'smooth' });
    document.querySelector('.sidebar')?.classList.remove('open');
  }

  function bind() {
    document.querySelectorAll('[data-route]').forEach((el) => el.addEventListener('click', () => activate(el.dataset.route)));
    document.querySelectorAll('[data-go-route]').forEach((el) => el.addEventListener('click', () => activate(el.dataset.goRoute)));
    document.querySelectorAll('[data-forward]').forEach((el) => el.addEventListener('click', () => $(el.dataset.forward)?.click()));
    $('rootPath')?.addEventListener('input', syncDashboard);
    $('scanBtn')?.addEventListener('click', syncDashboard, true);
    $('mobileMenuBtn')?.addEventListener('click', () => document.querySelector('.sidebar')?.classList.toggle('open'));
    const resetToken = () => { window.localStorage.removeItem('sjfx_api_token'); window.location.reload(); };
    $('headerTokenBtn')?.addEventListener('click', resetToken);
    $('headerTokenBtnSecondary')?.addEventListener('click', resetToken);
    const stats = $('scanStats');
    if (stats) new MutationObserver(syncDashboard).observe(stats, { childList: true, subtree: true, characterData: true });
    mirror('reportResult', 'overviewResultMount');
    mirror('summary', 'evidenceResultMount');
    mirror('pipeline', 'taskPipelineMount');
    setInterval(syncDashboard, 1500);
  }

  window.SJFXShell = { activate, get route() { return activeRoute; } };
  document.addEventListener('DOMContentLoaded', bind);
})();
