// Targeted VM fixtures for the actual task-center functions and logout handler.
// Prepared for a reviewed isolated runner; not executed during preparation.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const userbar = fs.readFileSync(path.join(root, 'sjfx_multiuser', 'web_assets', 'userbar.js'), 'utf8');
const taskStart = source.indexOf('function jobIdOf(');
const taskEnd = source.indexOf('function scanJobCandidate(', taskStart);
assert(taskStart >= 0 && taskEnd > taskStart);
const taskFunctions = source.slice(taskStart, taskEnd);
const LEGACY = 'sjfx_task_registry_v1';
const AUTHENTICATED = 'sjfx-authenticated';
const EMPTY = '导入数据包后，这里会显示最近一次分析和导出状态。';

function storage(values = {}, inaccessible = false) {
  const data = new Map(Object.entries(values));
  const reads = [], writes = [], removals = [];
  return {data, reads, writes, removals,
    getItem(key) { reads.push(key); if (inaccessible) throw Error('synthetic_storage_disabled');
      return data.get(key) ?? null; },
    setItem(key, value) { writes.push(key); if (inaccessible) throw Error('synthetic_storage_disabled');
      data.set(key, value); },
    removeItem(key) { removals.push(key); if (inaccessible) throw Error('synthetic_storage_disabled');
      data.delete(key); }};
}

function job(id, status = 'completed') {
  return {id, scan_id: 'scan-' + id, task_type: 'scan_and_analyze', status,
    progress: status === 'completed' ? 100 : 25, message: 'record-' + id,
    updated_local: Date.now()};
}

function harness(local, jobs = [], denied = false, reply = null) {
  const elements = new Map(['activeTaskList', 'dashboardActivity',
    'taskCenterActiveCount', 'taskCenterSyncState'].map(id => [id,
      {className: '', innerHTML: '', textContent: ''}]));
  const state = {jobs: new Map(), jobId: null, jobsEndpointAvailable: null,
    taskCenterRefreshInFlight: false};
  const calls = [], intervals = [];
  const context = {state, ACTIVE_JOB_STATUSES: new Set(['queued', 'running', 'cancelling']),
    TASK_REGISTRY_KEY: LEGACY, $: id => elements.get(id),
    escapeHtml: value => String(value).replace(/[&<>"']/g, ch => '&#' + ch.charCodeAt(0) + ';'),
    window: {localStorage: local, setInterval: (callback, ms) => intervals.push(ms)},
    api: async url => { calls.push(url);
      if (denied) { const error = Error('synthetic_unauthenticated'); error.status = 401; throw error; }
      if (reply) return reply(url);
      assert.equal(url, '/api/jobs?status=all&limit=50&compact=1');
      return {jobs: jobs.map(value => ({...value}))}; }};
  vm.createContext(context);
  vm.runInContext(taskFunctions + '\nthis.fixture = {startTaskCenterRefresh, refreshTaskCenter, rememberJob, removeRememberedJob, renderTaskCenter, applyServerTaskSnapshot};', context);
  return {state, calls, intervals, elements, functions: context.fixture};
}

function userbarHarness(local, session, failLogout = false) {
  function slice(startText, endText) {
    const start = userbar.indexOf(startText);
    const end = userbar.indexOf(endText, start);
    assert(start >= 0 && end > start);
    return userbar.slice(start, end);
  }
  const clear = slice('  function clearTaskCache()', '  function el(');
  const login = slice('  function toLogin()', '  function run(');
  const logout = slice("    out.addEventListener('click', function () {", '    bar.appendChild(out);');
  const location = {pathname: '/', href: ''};
  const calls = [];
  let click;
  const context = {location,
    window: {localStorage: local, sessionStorage: session, location},
    out: {addEventListener: (event, callback) => { assert.equal(event, 'click'); click = callback; }},
    fetch: async (url, options) => { calls.push({url, options});
      if (failLogout) throw Error('synthetic_logout_network_failure');
      return {ok: true}; }};
  vm.createContext(context);
  vm.runInContext(clear + login + logout + '\nthis.toLoginFixture = toLogin;', context);
  return {context, location, calls, click: () => click(), toLogin: context.toLoginFixture};
}

(async () => {
  const checks = [];
  // A second account must receive only its API records, even with an old
  // same-origin browser registry containing a previous account's task.
  const shared = storage({[LEGACY]: JSON.stringify([job('prior-account')])});
  const accountA = harness(shared, [job('account-a')]);
  await accountA.functions.startTaskCenterRefresh();
  assert.deepEqual([...accountA.state.jobs.keys()], ['account-a']);
  accountA.functions.rememberJob(job('account-a-new', 'running'));
  const accountB = harness(shared, [job('account-b')]);
  await accountB.functions.startTaskCenterRefresh();
  assert.deepEqual([...accountB.state.jobs.keys()], ['account-b']);
  assert.equal(shared.reads.length, 0);
  assert.equal(shared.writes.length, 0);
  assert.equal(shared.data.has(LEGACY), false);
  assert.match(accountB.elements.get('dashboardActivity').innerHTML, /record-account-b/);
  assert.doesNotMatch(accountB.elements.get('dashboardActivity').innerHTML, /prior-account|account-a/);
  checks.push('different_accounts_use_only_current_owner_api_records');

  const anonymous = harness(storage({[LEGACY]: JSON.stringify([job('old-private')])}), [], true);
  await anonymous.functions.startTaskCenterRefresh();
  assert.equal(anonymous.state.jobs.size, 0);
  assert.equal(anonymous.elements.get('dashboardActivity').className, 'activity-empty');
  assert.equal(anonymous.elements.get('dashboardActivity').textContent, EMPTY);
  assert.equal(anonymous.calls.length, 1);
  checks.push('anonymous_api_failure_never_restores_legacy_cards');

  const liveStorage = storage();
  const live = harness(liveStorage);
  live.functions.rememberJob(job('same-page', 'running'));
  assert.equal(live.state.jobs.get('same-page').progress, 25);
  live.functions.rememberJob({...job('same-page'), progress: 100});
  assert.equal(live.state.jobs.get('same-page').status, 'completed');
  assert.match(live.elements.get('dashboardActivity').innerHTML, /record-same-page/);
  assert.equal(liveStorage.writes.length, 0);
  checks.push('same_page_task_updates_remain_in_memory_without_persistence');

  live.functions.removeRememberedJob('same-page');
  assert.equal(live.state.jobs.size, 0);
  assert.equal(live.elements.get('dashboardActivity').className, 'activity-empty');
  assert.equal(live.elements.get('dashboardActivity').textContent, EMPTY);
  assert.equal(live.elements.get('activeTaskList').className, 'task-list empty');
  checks.push('removing_last_task_resets_dashboard_and_task_list');

  const reloaded = harness(storage(), [job('owned-active', 'running'), job('owned-completed')]);
  await reloaded.functions.startTaskCenterRefresh();
  assert.deepEqual([...reloaded.state.jobs.keys()], ['owned-active', 'owned-completed']);
  assert.equal(reloaded.state.jobs.get('owned-completed').status, 'completed');
  assert.deepEqual(reloaded.calls, ['/api/jobs?status=all&limit=50&compact=1']);
  assert.deepEqual(reloaded.intervals, [3000]);
  checks.push('reload_recovers_active_and_terminal_jobs_from_existing_owner_api');

  const emptySnapshot = harness(storage());
  emptySnapshot.functions.rememberJob(job('previous-session-terminal'));
  await emptySnapshot.functions.refreshTaskCenter();
  assert.equal(emptySnapshot.state.jobs.size, 0);
  assert.equal(emptySnapshot.elements.get('dashboardActivity').className, 'activity-empty');
  assert.equal(emptySnapshot.elements.get('dashboardActivity').textContent, EMPTY);
  checks.push('successful_empty_owner_snapshot_clears_previous_memory_and_dashboard');

  const malformed = harness(storage());
  malformed.functions.rememberJob(job('retained-on-malformed-response'));
  for (const invalid of [null, undefined, {}]) {
    assert.throws(() => malformed.functions.applyServerTaskSnapshot(
      invalid, new Map(malformed.state.jobs), Date.now()));
  }
  assert.equal(malformed.state.jobs.has('retained-on-malformed-response'), true);
  assert.match(malformed.elements.get('dashboardActivity').innerHTML, /record-retained-on-malformed-response/);
  checks.push('malformed_snapshot_preserves_memory_and_dashboard');

  const ordered = harness(storage(), [job('newest-terminal'), job('oldest-terminal')]);
  await ordered.functions.startTaskCenterRefresh();
  assert(ordered.state.jobs.get('newest-terminal').updated_local
         > ordered.state.jobs.get('oldest-terminal').updated_local);
  assert.match(ordered.elements.get('dashboardActivity').innerHTML, /record-newest-terminal/);
  checks.push('server_terminal_order_survives_recovery');

  const bounded = harness(storage(), Array.from({length: 50}, (_, index) => job('current-' + index)));
  bounded.functions.rememberJob(job('omitted-old-terminal'));
  await bounded.functions.refreshTaskCenter();
  assert.equal(bounded.state.jobs.size, 50);
  assert.equal(bounded.state.jobs.has('omitted-old-terminal'), false);
  checks.push('bounded_snapshot_replaces_unchanged_older_memory_without_server_mutation');

  for (const absent of [false, true]) {
    const rows = Array.from({length: 50}, (_, index) => job('listed-' + index, 'running'));
    const overflow = harness(storage(), [], false, async url => {
      if (url === '/api/jobs?status=all&limit=50&compact=1') return {jobs: rows};
      assert.equal(url, '/api/jobs/overflow-active');
      if (absent) { const error = Error('synthetic_foreign_or_missing_job'); error.status = 404; throw error; }
      return {job: {...job('overflow-active', 'running'), progress: 80}};
    });
    overflow.functions.rememberJob(job('overflow-active', 'running'));
    await overflow.functions.refreshTaskCenter();
    assert.deepEqual(overflow.calls, ['/api/jobs?status=all&limit=50&compact=1', '/api/jobs/overflow-active']);
    assert.equal(overflow.state.jobs.has('overflow-active'), !absent);
    if (!absent) assert.equal(overflow.state.jobs.get('overflow-active').progress, 80);
    assert.equal(overflow.state.jobs.size, absent ? 50 : 51);
  }
  checks.push('active_jobs_beyond_list_limit_are_individually_authorized_and_removed_on_404');

  let finishList;
  const pendingList = new Promise(resolve => { finishList = resolve; });
  const racing = harness(storage(), [], false, () => pendingList);
  racing.functions.rememberJob(job('polled', 'queued'));
  racing.functions.rememberJob(job('removed-on-403', 'running'));
  const refreshing = racing.functions.refreshTaskCenter();
  racing.functions.rememberJob({...job('polled', 'running'), progress: 90});
  racing.functions.rememberJob(job('new-submit', 'running'));
  racing.functions.removeRememberedJob('removed-on-403');
  racing.state.jobId = 'new-submit';
  finishList({jobs: [{...job('polled', 'queued'), progress: 5}, job('removed-on-403', 'running')]});
  await refreshing;
  assert.equal(racing.state.jobs.get('polled').progress, 90);
  assert.equal(racing.state.jobs.get('polled').status, 'running');
  assert.equal(racing.state.jobs.has('new-submit'), true);
  assert.equal(racing.state.jobs.has('removed-on-403'), false);
  checks.push('inflight_list_preserves_new_submission_and_newer_poll_state');

  const disabled = harness(storage({}, true), [job('server-only')]);
  await disabled.functions.startTaskCenterRefresh();
  assert.deepEqual([...disabled.state.jobs.keys()], ['server-only']);
  checks.push('unavailable_browser_storage_does_not_block_server_recovery');

  const logoutLocal = storage({[LEGACY]: 'old-private-record'});
  const logoutSession = storage({[AUTHENTICATED]: '1'});
  const logout = userbarHarness(logoutLocal, logoutSession, true);
  logout.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(logoutLocal.data.has(LEGACY), false);
  assert.equal(logoutSession.data.has(AUTHENTICATED), false);
  assert.equal(logout.calls[0].url, '/api/auth/logout');
  assert.equal(logout.calls[0].options.method, 'POST');
  assert.equal(logout.calls[0].options.credentials, 'same-origin');
  assert.equal(logout.location.href, '/login');
  checks.push('normal_logout_clears_legacy_and_auth_marker_even_on_network_failure');

  const rejectedLocal = storage({[LEGACY]: 'old-private-record'});
  const rejectedSession = storage({[AUTHENTICATED]: '1'});
  const rejected = userbarHarness(rejectedLocal, rejectedSession);
  rejected.toLogin();
  assert.equal(rejectedLocal.data.has(LEGACY), false);
  assert.equal(rejectedSession.data.has(AUTHENTICATED), false);
  assert.equal(rejected.location.href, '/login');
  checks.push('unauthenticated_redirect_is_not_suppressed_by_stale_auth_marker');

  process.stdout.write(JSON.stringify({passed: true, check_count: checks.length, checks}) + '\n');
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
