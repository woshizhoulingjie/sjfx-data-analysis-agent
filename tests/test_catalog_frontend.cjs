const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function environment() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { id, textContent: '', innerHTML: '', dataset: {},
      setAttribute() {}, querySelector() { return null; }, classList: { toggle() {}, add() {} } });
    return elements.get(id);
  };
  const window = { addEventListener() {} };
  const document = { getElementById: element, addEventListener() {}, querySelectorAll: () => [], activeElement: null };
  const context = vm.createContext({ window, document, Map, Set, URLSearchParams });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/large-package-portrait.js'), 'utf8'), context);
  const source = fs.readFileSync(path.join(__dirname, '../static/large-package-result.js'), 'utf8').replace(
    'window.SJFXLargeResult = { activate };',
    `window.__catalogTest = { smartNavigation, renderCatalog, renderScope,
      setStatus: value => { latestStatus = value; },
      setNode: node => { browseMode = 'smart'; browseStates.smart.node = node; },
      getNode: () => browseStates.smart.node,
      addExtra: nodes => { extraCatalogNodes.push(...nodes); },
      nodeInfo: () => nodeInfo };
    window.SJFXLargeResult = { activate };`);
  vm.runInContext(source, context);
  return { view: window.SJFXLargePortrait, result: window.__catalogTest, element };
}

function catalogFixture() {
  return { files_total: 1040, smart: { content_files: 1021, attempted_files: 1040,
    theme_total: 4, leaf_total: 72, topic_total: 73,
    naming: { named_nodes: 71, named_files: 998, total_nodes: 72, pending_nodes: 1, active: true, status: 'active', jobs: { queued: 1 } },
    navigation: [ { node_id: 'leaf-a', topic: '公共卫生研究', files: 282 },
      { node_id: 'leaf-b', topic: '气候与健康', files: 742 } ],
    branches: [ { node_id: 'topic:a', name: '公共卫生', status: 'current', files: 282, children: ['leaf-a'] },
      { node_id: 'branch:unassigned', name: '待归并主题', status: 'pending', files: 742, children: ['leaf-b'] } ],
    topics: [ { node_id: 'preview:missing', topic: '等待建立内容画像', files: 16, naming_status: 'unread' } ],
    catalog_states: { classified: 998, grouped: 26, legacy: 16 } } };
}

test('1040-file baseline distinguishes theme, leaf, preview, naming coverage and ungrouped scopes', () => {
  const { view, result } = environment(), data = catalogFixture();
  const summary = view.catalogSummary(data);
  assert.equal(summary.text, '4 个主题 · 72 个叶子分组');
  assert.equal(summary.content, 1021);
  assert.equal(summary.named, 71);
  assert.equal(summary.namedFiles, 998);
  assert.equal(summary.pending, 742);
  assert.equal(summary.ungrouped, 16);
  const html = result.smartNavigation(data);
  assert.match(html, /叶子名称<\/dt><dd>71 \/ 72<small>分组/);
  assert.match(html, /待上层主题整理 742/);
  assert.match(html, /未归组文件/);
  assert.doesNotMatch(html, /71 个主题|内容主题.*73|目录已完善/);
  assert.match(html, /分组命名与主题整理待处理/);
});

test('authoritative stats preserve zeros and exclude system buckets from leaf naming counts', () => {
  const { view } = environment(), data = catalogFixture();
  data.smart.stats = { total_files: 1040, theme_nodes: 0, leaf_nodes: 71,
    grouped_files: 1024, ungrouped_files: 16, named_leaf_nodes: 71, named_files: 998,
    pending_theme_files: 0, independent_files: 742, quick_failed_files: 16,
    system_bucket_files: 26, content_files: 0 };
  const summary = view.catalogSummary(data);
  assert.equal(summary.themes, 0);
  assert.equal(summary.leaves, 71);
  assert.equal(summary.content, 0);
  assert.equal(summary.pending, 0);
  assert.equal(summary.unresolved, 0, 'an unnamed system bucket is not an unnamed semantic leaf');
  assert.equal(summary.quickFailed, 16);
  assert.equal(summary.independent, 742);
});

test('legacy fallback excludes previews from membership and keeps system buckets separate from semantic groups', () => {
  const { view } = environment(), data = catalogFixture();
  data.smart.navigation[1].files = 716;
  data.smart.navigation.push({ node_id: 'km-other-test', files: 20 }, { node_id: 'km-pending-test', files: 6 });
  const summary = view.catalogSummary(data);
  assert.equal(summary.grouped, 998);
  assert.equal(summary.ungrouped, 16, 'preview scopes are not catalog membership');
  data.smart.stats = { grouped_files: 0, member_files: 0 };
  assert.equal(view.catalogSummary(data).grouped, 0);
  assert.equal(view.catalogSummary(data).ungrouped, 1040, 'explicit membership zero wins over loaded nodes');
});

test('system holding buckets stay accessible and are distinguished from named leaves', () => {
  const { view, result, element } = environment(), data = catalogFixture();
  data.smart.navigation.push({ node_id: 'km-other-test', topic: '待归并主题', files: 26 });
  const html = result.smartNavigation(data);
  assert.match(html, /待进一步分组/);
  assert.match(html, /待内容分组/);
  assert.equal(view.nodeKind(data.smart.navigation.at(-1)), 'staging');
  result.renderCatalog(data);
  result.setNode({ type: 'group', value: 'km-other-test', label: '待进一步分组' });
  result.renderScope(26);
  assert.match(element('largeResultNodeSummary').innerHTML, /暂存文件/);
});

test('authoritative independent zeros win over loaded branch metadata', () => {
  const { view } = environment(), data = catalogFixture();
  Object.assign(data.smart.branches[1], { independent_leaf_ids: ['leaf-b'], independent_files: 742 });
  data.smart.stats = { display_independent_files: 0, independent_files: 742 };
  assert.equal(view.catalogSummary(data).independent, 0);
  delete data.smart.stats.display_independent_files;
  data.smart.stats.independent_files = 0;
  assert.equal(view.catalogSummary(data).independent, 0);
  data.smart.stats.independent_files = 900;
  assert.equal(view.catalogSummary(data).independent, 900, 'the loaded 742-file branch is only a partial page');
});

test('missing root totals are not displayed as a zero and explicit zeros are retained', () => {
  const { view, result } = environment(), data = catalogFixture();
  assert.equal(view.catalogSummary(data).rootKnown, false);
  assert.doesNotMatch(result.smartNavigation(data), /0 顶层/);
  data.smart.stats = { root_theme_nodes: 0 };
  assert.equal(view.catalogSummary(data).rootKnown, true);
  assert.match(view.catalogSummary(data).text, /0 个顶层/);
  assert.match(result.smartNavigation(data), /0 顶层/);
});

test('paused package overrides an active naming round and prevents queueing from the directory', () => {
  const { view, result } = environment(), data = catalogFixture();
  result.setStatus({ status: 'paused' });
  const summary = view.catalogSummary(data, { status: 'paused' });
  assert.equal(summary.active, false);
  assert.equal(summary.canImprove, false);
  const html = result.smartNavigation(data);
  assert.match(html, /数据包已暂停/);
  assert.match(html, /data-lp-improve disabled/);
  assert.doesNotMatch(html, /正在完善|目录已完善/);
});

test('cancelled and failed packages show preserved intermediate results, not completion', () => {
  const { view } = environment();
  for (const status of ['cancelled', 'failed']) {
    const summary = view.catalogSummary(catalogFixture(), { status });
    assert.equal(summary.canImprove, false);
    assert.match(summary.message, /已停止|处理失败/);
    assert.doesNotMatch(summary.message, /目录已完善|正在/);
  }
});

test('budget exhaustion and terminal deferred jobs remain visibly incomplete', () => {
  const { view } = environment(), data = catalogFixture();
  data.smart.naming.active = false;
  data.smart.naming.status = 'exhausted';
  data.smart.naming.jobs = { deferred: 3, failed: 2 };
  const summary = view.catalogSummary(data);
  assert.match(summary.message, /预算已结束/);
  assert.match(summary.message, /1 个分组待命名/);
  assert.match(summary.message, /2 个命名任务失败/);
  assert.match(summary.message, /3 个命名任务待重试/);
  assert.equal(summary.canImprove, true);
  assert.doesNotMatch(summary.message, /目录已完善/);
});

test('independent branches are not described as waiting, while mixed branches show separate causes', () => {
  const { view, result } = environment(), data = catalogFixture();
  const branch = data.smart.branches[1];
  Object.assign(branch, { kind: 'independent', name: '独立内容主题', independent_files: 742, pending_files: 0 });
  assert.equal(view.nodeState(branch), '独立分组');
  const html = result.smartNavigation(data);
  assert.match(html, /独立分组/);
  assert.doesNotMatch(html, /待归并主题 742|lp-branch-pending/);
  Object.assign(branch, { kind: 'mixed', name: '待归并主题', independent_files: 700, pending_files: 42 });
  const mixed = result.smartNavigation(data);
  assert.match(mixed, /独立分组与待处理范围/);
  assert.match(mixed, /待上层主题整理 42/);
});

test('quick failures retain an actionable node and are not called waiting for a portrait', () => {
  const { result, element } = environment(), data = catalogFixture();
  Object.assign(data.smart.topics[0], { topic: '内容预览失败', state: 'failed', naming_status: 'failed',
    error: '未能打开 PDF：文件损坏', basis: '保留原文件，可核查后重试' });
  data.smart.stats = { total_files: 1040, ungrouped_files: 16, quick_failed_files: 16 };
  result.renderCatalog(data);
  const html = element('largeResultSmartCatalog').innerHTML;
  assert.match(html, /内容预览失败|解析失败/);
  assert.doesNotMatch(html, /等待建立内容画像/);
  result.setNode({ type: 'group', value: 'preview:missing', label: '内容预览失败' });
  result.renderScope(16);
  assert.match(element('largeResultNodeSummary').innerHTML, /未能打开 PDF：文件损坏/);
  assert.match(element('largeResultNodeSummary').innerHTML, /当前范围匹配 16 个文件/);
  assert.doesNotMatch(element('largeResultNodeSummary').innerHTML, /data-lp-rename/);
});

test('recursive themes display each leaf once and retain orphan-parent branches', () => {
  const { view, result } = environment(), data = catalogFixture();
  data.smart.branches = [
    { node_id: 'topic:a', name: '健康', status: 'current', files: 1024, child_nodes: ['topic:b', 'leaf-a'], children: ['leaf-a', 'leaf-b'] },
    { node_id: 'topic:b', parent_id: 'topic:a', name: '气候与健康', status: 'current', files: 742, children: ['leaf-b'] },
    { node_id: 'topic:orphan', parent_id: 'topic:missing', name: '待复核目录', status: 'stale', files: 282, children: ['leaf-a'] } ];
  for (const html of [result.smartNavigation(data), view.catalog(data)]) {
    assert.equal((html.match(/value="leaf-a"/g) || []).length, 1);
    assert.equal((html.match(/value="leaf-b"/g) || []).length, 1);
    assert.match(html, /待复核目录/);
  }
});

test('cyclic branches cannot recurse forever', () => {
  const { view, result } = environment(), data = catalogFixture();
  data.smart.branches = [ { node_id: 'topic:a', name: '主题A', child_nodes: ['topic:b'] },
    { node_id: 'topic:b', parent_id: 'topic:a', name: '主题B', child_nodes: ['topic:a', 'leaf-a'] } ];
  assert.match(result.smartNavigation(data), /主题A/);
  assert.match(view.catalog(data), /主题B/);
});

test('updated directory resets a deleted selected group and does not keep an empty obsolete scope', () => {
  const { result } = environment(), data = catalogFixture();
  result.setNode({ type: 'group', value: 'removed-leaf', label: '旧分组' });
  result.renderCatalog(data);
  assert.equal(result.getNode().type, 'all');
});

test('count overview and node labels remain escaped and all count units are explicit', () => {
  const { result, view, element } = environment(), data = catalogFixture();
  data.smart.branches[0].name = '<img src=x onerror=alert(1)>';
  const html = result.smartNavigation(data);
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!html.includes('<img src=x'));
  assert.match(html, /aria-label="282 个文件"/);
  result.renderCatalog(data);
  assert.equal(element('largeResultSmartStats').textContent, '4 个主题 · 72 个叶子分组');
  assert.match(view.catalog(data), /上下级数量不相加/);
});

test('partial loading keeps full file counts and exposes missing preview details without false completion', () => {
  const { result } = environment(), data = catalogFixture();
  data.smart.topics = [];
  data.smart.topics_truncated = true;
  data.smart.naming.active = false;
  const html = result.smartNavigation(data);
  assert.match(html, /16 文件/);
  assert.match(html, /更多条目详情尚未加载/);
  assert.match(html, /data-lp-more/);
  assert.doesNotMatch(html, /目录已完善/);
});

test('live directory revision discards cached detail pages that would hide a rename or resurrect removed groups', () => {
  const { result, element } = environment(), data = catalogFixture();
  data.smart.topics = [];
  data.smart.topics_truncated = true;
  result.renderCatalog(data);
  result.addExtra([{ node_id: 'leaf-b', topic: '过时分组名称', files: 742 },
    { node_id: 'removed-extra', topic: '已删除的分页分组', files: 1 }]);
  result.renderCatalog(data);
  assert.match(element('largeResultSmartCatalog').innerHTML, /过时分组名称/);
  data.smart.navigation[1].topic = '更新后的气候健康';
  result.renderCatalog(data);
  const html = element('largeResultSmartCatalog').innerHTML;
  assert.match(html, /更新后的气候健康/);
  assert.doesNotMatch(html, /过时分组名称|已删除的分页分组/);
});
