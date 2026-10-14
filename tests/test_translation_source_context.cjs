/* Actual engineering-v2 functions; no network, models, or production state. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  const elements = new Map();
  const node = (id) => elements.get(id) || elements.set(id, {
    id, innerHTML: '', textContent: '', value: '', disabled: false,
    dataset: {}, classList: { toggle() {} },
    querySelectorAll: () => [], querySelector: () => null,
    addEventListener() {}, setAttribute() {}, appendChild() {}, after() {}
  }).get(id);
  const storage = () => {
    const values = new Map();
    return { getItem: (k) => values.get(k) || null, setItem: (k, v) => values.set(k, String(v)), removeItem: (k) => values.delete(k) };
  };
  const requests = [];
  const sessionStorage = storage();
  const window = {
    localStorage: storage(), sessionStorage,
    setTimeout: () => 1, clearTimeout() {}, requestAnimationFrame() {},
    SJFXAuth: { request(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve: (body) => resolve({ ok: true, json: async () => ({ ok: true, ...body }) }), reject })); } }
  };
  const document = { getElementById: node, body: { dataset: { route: 'translation' } }, querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, createElement: () => node('created') };
  const context = vm.createContext({ window, document, sessionStorage, URLSearchParams, console });
  const filename = process.env.TRANSLATION_ENGINEERING_SOURCE || path.join(__dirname, '../static/engineering-v2.js');
  const source = fs.readFileSync(filename, 'utf8').replace("document.addEventListener('DOMContentLoaded', bind);", `window.testAPI = { state, translationScanId, resetTranslationView, renderTranslationSourceBar, loadTranslationList, loadTranslation, watchTranslationJob, translateSelected, translateDocument, translatePackage, rememberTranslationPath, resetForScan, openBriefFile };`);
  vm.runInContext(source, context);
  const api = window.testAPI;
  api.state.scanId = 'ordinary-current';
  api.state.chatSourceId = 'ordinary-current'; // Keep unrelated chat state out of these translation cases.
  return { ...api, window, node, requests };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('translation source remains selectable even without a QA index', () => {
  const h = harness();
  h.state.translationSourceList = [{ scan_id: 'historical', name: '历史未索引资料', usable: false }];
  h.renderTranslationSourceBar();
  assert.match(h.node('translationSourceSelect').innerHTML, /historical/);
  assert.doesNotMatch(h.node('translationSourceSelect').innerHTML, /disabled/);
});

test('switching packages immediately clears old files, counts, document, selection and job', () => {
  const h = harness();
  h.state.translationItems = [{ path: 'old.txt' }];
  h.state.translationCounts = { completed: 9 };
  h.state.translationSelectedPaths.add('old.txt');
  h.state.translationActiveJob = 'old-job';
  h.state.watchers.set('translation-files', 'old-token');
  h.resetTranslationView('lp-new');
  assert.equal(h.state.translationItems.length, 0);
  assert.equal(Object.keys(h.state.translationCounts).length, 0);
  assert.equal(h.state.translationSelectedPaths.size, 0);
  assert.equal(h.state.translationActiveJob, '');
  assert.equal(h.state.watchers.has('translation-files'), false);
  assert.doesNotMatch(h.node('translationList').innerHTML, /old.txt/);
});

test('A to B to A ignores late list response from first A selection', async () => {
  const h = harness();
  h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-A';
  const first = h.loadTranslationList();
  h.state.translationSourceId = 'lp-B'; h.resetTranslationView('lp-B');
  h.state.translationSourceId = 'lp-A'; h.resetTranslationView('lp-A');
  h.requests[0].resolve({ items: [{ path: 'stale.txt' }], total: 1, offset: 0 });
  await first;
  assert.equal(h.state.translationItems.length, 0);
  assert.doesNotMatch(h.node('translationList').innerHTML, /stale.txt/);
});

test('A to B to A ignores late document response', async () => {
  const h = harness();
  h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-A';
  const first = h.loadTranslation('shared.txt', 0);
  h.state.translationSourceId = 'lp-B'; h.resetTranslationView('lp-B');
  h.state.translationSourceId = 'lp-A'; h.resetTranslationView('lp-A');
  h.requests[0].resolve({ path: 'shared.txt', original: { text: 'stale text' } });
  await first;
  assert.equal(h.state.translationPage, null);
  assert.doesNotMatch(h.node('translationDocumentContent').innerHTML, /stale text/);
});

test('A to B to A ignores old submit response and never starts its watcher', async () => {
  const h = harness();
  h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-A';
  h.state.translationSelectedPaths.add('a.txt');
  const first = h.translateSelected();
  assert.match(h.requests[0].url, /translate-package\/lp-A$/);
  assert.deepEqual(JSON.parse(h.requests[0].options.body).target_paths, ['a.txt']);
  h.state.translationSourceId = 'lp-B'; h.resetTranslationView('lp-B');
  h.state.translationSourceId = 'lp-A'; h.resetTranslationView('lp-A');
  h.requests[0].resolve({ job_id: 'stale-job' }); await first;
  assert.equal(h.requests.length, 1);
  assert.equal(h.state.translationActiveJob, '');
});

test('late completed job cannot repaint another package or clear its active job', async () => {
  const h = harness();
  h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-A';
  const first = h.watchTranslationJob('old-job', '旧任务');
  h.state.translationSourceId = 'lp-B'; h.resetTranslationView('lp-B');
  h.state.translationActiveJob = 'new-job'; h.node('translationQueueState').textContent = '新包任务';
  h.requests[0].resolve({ job: { status: 'completed', result: { completed_files: 1 } } });
  await first;
  assert.equal(h.node('translationQueueState').textContent, '新包任务');
  assert.equal(h.state.translationActiveJob, 'new-job');
  assert.equal(h.requests.length, 1);
});

test('ordinary workspace changes preserve selected large translation and its watcher', async () => {
  const h = harness();
  h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-A';
  h.state.translationItems = [{ path: 'large.txt' }]; h.state.translationPath = 'large.txt';
  h.state.translationSelectedPaths.add('large.txt');
  const first = h.watchTranslationJob('large-job', '当前任务');
  h.resetForScan('ordinary-next');
  assert.equal(h.translationScanId(), 'lp-A');
  assert.equal(h.state.translationPath, 'large.txt');
  assert.equal(h.state.translationSelectedPaths.size, 1);
  h.requests[0].resolve({ job: { status: 'failed', error: 'synthetic failure' } });
  await tick();
  assert.match(h.node('translationQueueState').textContent, /synthetic failure/);
  // Final refresh is a bounded GET, not a new translation submission.
  h.requests.slice(1).forEach((r) => r.resolve({ items: [], total: 0, path: 'large.txt' }));
  await first;
});

test('remembered large document is bound to its actual source and survives workspace changes', () => {
  const h = harness(); h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-A';
  h.rememberTranslationPath('large.txt');
  assert.equal(h.window.sessionStorage.getItem('sjfx_last_document_scan_id_v1'), 'lp-A');
  h.resetForScan('ordinary-next');
  assert.equal(h.window.sessionStorage.getItem('sjfx_last_document_scan_id_v1'), 'lp-A');
});

test('large-only translation summary displays file counts without a global ordinary scan', async () => {
  const h = harness(); h.state.scanId = ''; h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-A';
  const result = h.loadTranslationList();
  h.requests[0].resolve({ items: [{ path: 'a.txt', status: 'completed' }], total: 1, unfiltered_total: 1, offset: 0, status_counts: { completed: 1 } });
  await result;
  assert.match(h.node('translationPackageState').innerHTML, /资料清单/);
  assert.doesNotMatch(h.node('translationPackageState').innerHTML, /导入数据包后/);
});

test('overview file jump switches translation to the source package before reading', async () => {
  const h = harness(); h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-old';
  h.openBriefFile('ordinary.txt', 'ordinary-next');
  assert.equal(h.translationScanId(), 'ordinary-next');
  assert.equal(h.state.translationSourceKind, 'standard');
  assert.ok(h.requests.some((r) => r.url.includes('/api/translation/ordinary-next?')));
  h.requests.forEach((r) => r.resolve({ items: [], total: 0, path: 'ordinary.txt' })); await tick();
});

test('loaded file list ends package switching hint when no file is selected', async () => {
  const h = harness(); h.resetTranslationView(h.translationScanId());
  assert.match(h.node('translationDocumentContent').innerHTML, /正在切换数据包/);
  const loading = h.loadTranslationList();
  h.requests[0].resolve({ items: [{ path: 'ready.txt' }], total: 1, offset: 0 }); await loading;
  assert.match(h.node('translationDocumentState').textContent, /资料清单已载入/);
  assert.match(h.node('translationDocumentContent').innerHTML, /请选择文件/);
  assert.doesNotMatch(h.node('translationDocumentContent').innerHTML, /正在切换|正在读取/);
  assert.equal(h.state.translationPath, '');
});

test('empty list shows no matching files after finishing source change', async () => {
  const h = harness(); h.resetTranslationView(h.translationScanId());
  const loading = h.loadTranslationList();
  h.requests[0].resolve({ items: [], total: 0, offset: 0 }); await loading;
  assert.match(h.node('translationDocumentState').textContent, /没有匹配文件/);
  assert.match(h.node('translationDocumentContent').innerHTML, /没有匹配文件/);
  assert.doesNotMatch(h.node('translationDocumentContent').innerHTML, /正在切换|正在读取/);
});

test('failed list ends switching hint with an explicit retry message', async () => {
  const h = harness(); h.resetTranslationView(h.translationScanId());
  const loading = h.loadTranslationList();
  h.requests[0].reject(new Error('synthetic list failure')); await loading;
  assert.match(h.node('translationDocumentState').textContent, /synthetic list failure/);
  assert.match(h.node('translationDocumentContent').innerHTML, /无法读取翻译资料/);
  assert.doesNotMatch(h.node('translationDocumentContent').innerHTML, /正在切换|正在读取/);
  assert.match(h.node('translationDocumentState').className, /is-error/);
});

test('list refresh success and failure preserve an already opened document', async () => {
  for (const failed of [false, true]) {
    const h = harness(); h.state.translationPath = 'open.txt';
    const page = h.state.translationPage = { path: 'open.txt' };
    h.node('translationDocumentState').textContent = '现有文档状态';
    h.node('translationDocumentContent').innerHTML = '现有文档正文';
    const loading = h.loadTranslationList();
    if (failed) h.requests[0].reject(new Error('synthetic failure'));
    else h.requests[0].resolve({ items: [], total: 0, offset: 0 });
    await loading;
    assert.equal(h.node('translationDocumentContent').innerHTML, '现有文档正文');
    assert.equal(h.node('translationDocumentState').textContent, '现有文档状态');
    assert.equal(h.state.translationPage, page);
  }
});

test('list arriving while document loads preserves its reader progress', async () => {
  const h = harness(); const listLoading = h.loadTranslationList();
  const reading = h.loadTranslation('opening.txt', 0);
  const progress = h.node('translationDocumentState').textContent;
  const content = h.node('translationDocumentContent').innerHTML;
  h.requests[0].resolve({ items: [{ path: 'opening.txt' }], total: 1, offset: 0 }); await listLoading;
  assert.equal(h.node('translationDocumentState').textContent, progress);
  assert.equal(h.node('translationDocumentContent').innerHTML, content);
  h.requests[1].resolve({ path: 'opening.txt', original: { text: 'opened source' } }); await reading;
});

test('late old list cannot end the new package loading hint', async () => {
  const h = harness(); h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-A';
  const oldList = h.loadTranslationList();
  h.state.translationSourceId = 'lp-B'; h.resetTranslationView('lp-B');
  const newList = h.loadTranslationList();
  h.requests[0].resolve({ items: [{ path: 'old.txt' }], total: 1, offset: 0 }); await oldList;
  assert.match(h.node('translationDocumentContent').innerHTML, /正在切换数据包/);
  h.requests[1].resolve({ items: [{ path: 'new.txt' }], total: 1, offset: 0 }); await newList;
  assert.match(h.node('translationDocumentContent').innerHTML, /请选择文件/);
});

test('late old reader cannot replace the newly loaded package idle prompt', async () => {
  const h = harness(); h.state.translationSourceKind = 'large'; h.state.translationSourceId = 'lp-A';
  const oldReader = h.loadTranslation('old.txt', 0);
  h.state.translationSourceId = 'lp-B'; h.resetTranslationView('lp-B');
  const newList = h.loadTranslationList();
  h.requests[1].resolve({ items: [{ path: 'new.txt' }], total: 1, offset: 0 }); await newList;
  h.requests[0].resolve({ path: 'old.txt', original: { text: 'stale source' } }); await oldReader;
  assert.match(h.node('translationDocumentContent').innerHTML, /请选择文件/);
  assert.doesNotMatch(h.node('translationDocumentContent').innerHTML, /stale source/);
  assert.equal(h.state.translationPage, null);
});
