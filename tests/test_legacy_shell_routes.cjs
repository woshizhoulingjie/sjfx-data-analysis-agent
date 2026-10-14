const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('a legacy homogeneous link opens the real analysis workspace', () => {
  const source = fs.readFileSync(path.join(__dirname, '../static/product-shell.js'), 'utf8');
  const activate = source.slice(source.indexOf('  function activate(route)'), source.indexOf('  function bind()'));
  const nodes = new Map(), views = ['explore', 'chat', 'dashboard'].map(view => ({
    dataset: { view }, active: false, classList: { toggle(_name, active) { this.owner.active = active; } }
  }));
  views.forEach(view => { view.classList.owner = view; });
  let analysisClicks = 0, activated = '';
  nodes.set('analysisTreeBtn', { disabled: false, click() { analysisClicks++; } });
  const document = { body: { dataset: {} }, querySelectorAll(selector) { return selector === '.app-view' ? views : []; } };
  const context = vm.createContext({
    document, $: id => nodes.get(id),
    routeNames: { analysis: ['智能分析', '主题与证据'], dashboard: ['工作台', '数据概览'] },
    viewFor: { analysis: 'explore', dashboard: 'dashboard' }, activeRoute: 'dashboard',
    window: { SJFXEngineering: { activate(route) { activated = route; } }, scrollTo() {} },
    syncDashboard() {}, setNavigationOpen() {}, publishWorkspaceState() {},
  });
  vm.runInContext(activate, context);
  context.activate('homogeneous');
  assert.equal(document.body.dataset.route, 'analysis');
  assert.equal(activated, 'analysis');
  assert.equal(analysisClicks, 1);
  assert.equal(views.find(view => view.dataset.view === 'explore').active, true);
  assert.equal(views.filter(view => view.active).length, 1);
});
