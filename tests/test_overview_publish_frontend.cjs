const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
function between(start, end) {
  const from = source.indexOf(start);
  assert.ok(from >= 0, start);
  const to = source.indexOf(end, from + start.length);
  assert.ok(to > from, end);
  return source.slice(from, to);
}

function environment() {
  const events = new Map();
  const calls = [];
  const button = { disabled: true, dataset: {}, textContent: '更新正式结果', isConnected: true,
    addEventListener(type, listener) { events.set('button:' + type, listener); } };
  const state = { scan: { scan_id: 'scan-a' }, importTask: { scan_id: 'scan-a', status: 'completed' },
    processing: { deep_summary_files: 1 }, summaries: new Map(), selected: null };
  const context = vm.createContext({ state, $: () => button, JSON, String, Number,
    window: { addEventListener(type, listener) { events.set(type, listener); },
      SJFXEngineering: { refreshOverview() { calls.push({ type: 'overview' }); } } },
    document: { querySelector() { return null; } },
    api: async (url, options) => { calls.push({ type: 'api', url, options }); return { job_id: 'publish-job' }; },
    pollJob: async id => { calls.push({ type: 'poll', id }); },
    refreshScan: async id => { calls.push({ type: 'scan', id }); },
    toast: (message, error) => { calls.push({ type: 'toast', error: Boolean(error) }); }
  });
  vm.runInContext(between('function setBusy(', '\n\nfunction escapeHtml('), context);
  vm.runInContext(between('function updateOverviewPublishButton(', '\n\nfunction renderPackageProcessing('), context);
  vm.runInContext(between('async function refreshDeepResults(', '\n\ndocument.addEventListener('), context);
  vm.runInContext(between("  $('packageOverviewPublishBtn')?.addEventListener('click'", '  // Each panel can become usable'), context);
  return { state, button, events, calls, update: () => vm.runInContext('updateOverviewPublishButton()', context) };
}

test('overview has a unique visible publication button beside refresh', () => {
  const overview = html.split('data-view="overview"')[1].split('data-view="chat"')[0];
  assert.match(overview, /id="packageOverviewPublishBtn"[^>]*disabled>更新正式结果<\/button>/);
  assert.equal((html.match(/id="packageOverviewPublishBtn"/g) || []).length, 1);
  assert.doesNotMatch(overview.match(/<button[^>]*id="packageOverviewPublishBtn"[^>]*>/)[0], /hidden/);
});

test('completed, partial and publish-ready same-scan summaries enable the normal action', () => {
  const env = environment();
  for (const status of ['completed', 'partial', 'deep_update_available']) {
    env.state.importTask.status = status;
    env.update();
    assert.equal(env.button.disabled, false, status);
    assert.equal(env.button.dataset.scanId, 'scan-a');
  }
});

test('unready and different-scan tasks never enable the publication button', async () => {
  const env = environment();
  env.state.processing.deep_summary_files = 0;
  env.update();
  assert.equal(env.button.disabled, true);
  env.state.processing.deep_summary_files = 1;
  env.state.importTask.status = 'deep_summarizing_files';
  env.update();
  assert.equal(env.button.disabled, true);
  env.state.importTask.status = 'completed';
  env.state.importTask.scan_id = 'old-scan';
  env.update();
  assert.equal(env.button.disabled, true);
  assert.equal(env.button.dataset.scanId, '');
  await env.events.get('button:click')({ currentTarget: env.button });
  assert.equal(env.calls.length, 0);
  env.state.importTask = null;
  env.update();
  assert.equal(env.button.disabled, true);
});

test('package switching invalidates readiness until the new task is loaded', async () => {
  const env = environment();
  env.update();
  env.state.scan = { scan_id: 'scan-b' };
  env.events.get('sjfx-scan-changed')();
  assert.equal(env.button.disabled, true);
  await env.events.get('button:click')({ currentTarget: env.button });
  assert.equal(env.calls.length, 0);
  env.update();
  assert.equal(env.button.disabled, true);
  env.state.importTask = { scan_id: 'scan-b', status: 'completed' };
  env.update();
  assert.equal(env.button.disabled, false);
  assert.equal(env.button.dataset.scanId, 'scan-b');
});

test('click reuses the existing publication endpoint, polls its job and refreshes overview', async () => {
  const env = environment();
  env.update();
  env.button.dataset.publishBusy = 'true';
  env.update();
  assert.equal(env.button.disabled, true);
  delete env.button.dataset.publishBusy;
  env.update();
  await env.events.get('button:click')({ currentTarget: env.button });
  const request = env.calls.find(call => call.type === 'api');
  assert.equal(request.url, '/api/scan/scan-a/refresh-deep-results');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.body, '{}');
  assert.equal(env.calls.filter(call => call.type === 'api').length, 1);
  assert.equal(env.calls.find(call => call.type === 'poll').id, 'publish-job');
  assert.equal(env.calls.find(call => call.type === 'scan').id, 'scan-a');
  assert.equal(env.calls.filter(call => call.type === 'overview').length, 1);
  assert.equal(env.button.dataset.publishBusy, undefined);
  assert.equal(env.button.disabled, false);
});
