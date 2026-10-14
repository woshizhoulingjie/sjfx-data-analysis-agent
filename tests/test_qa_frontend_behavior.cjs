/* Deterministic browser-state regressions: actual JS, controlled async HTTP. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function environment(filename) {
  const elements = new Map();
  const values = new Map([['sjfx_current_scan_id_v1', 'scan-a']]);
  const requests = [];
  const workspace = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      id, value: id === 'conversationScopeKind' ? 'package' : '', innerHTML: '', textContent: '',
      dataset: {}, style: {}, classList: { toggle() {}, add() {}, remove() {} },
      querySelectorAll: () => [], setAttribute() {}, after() {}, focus() {}, addEventListener() {}
    });
    return elements.get(id);
  }
  const storage = { getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)), removeItem: (key) => values.delete(key) };
  const window = { localStorage: storage, sessionStorage: storage, clearTimeout() {}, setTimeout: () => 0,
    dispatchEvent() {}, requestAnimationFrame() {},
    SJFXDataSources: { syncWorkspace: async (id, guard) => { if (guard()) workspace.push(id); } } };
  let respond = async (url) => url.startsWith('/api/conversations/')
    ? { ok: true, items: [{ session_id: url.split('/').pop() + '-session' }] }
    : { ok: true, items: [], total: 0 };
  window.SJFXAuth = { request: async (url, options) => {
    requests.push({ url, body: options.body && JSON.parse(options.body) });
    const payload = await respond(url, options);
    return { ok: true, status: 200, json: async () => payload };
  } };
  const context = vm.createContext({ window, document: { getElementById: element, addEventListener() {} },
    sessionStorage: storage, CustomEvent: function () {}, Map, Set, Date, Math });
  const source = fs.readFileSync(filename, 'utf8').replace("document.addEventListener('DOMContentLoaded', bind);", `
    resetOverviewView = () => {};
    resetTranslationView = () => {};
    renderTranslationSummary = () => {};
    renderConversation = () => {};
    renderScopeFields = () => {};
    registerAnalysisTurns = () => {};
    window.__qa = { state, setChatSource, renderChatSourceBar, loadConversationList,
      openConversation, loadOlderConversationMessages, syncScan, restoreChatSource, resetForScan };
  `);
  vm.runInContext(source, context);
  window.__qa.state.scanId = 'scan-a';
  return { qa: window.__qa, element, values, requests, workspace,
    respond: (callback) => { respond = callback; } };
}

async function run(filename) {
  const checks = [];
  const env = environment(filename), { qa, element, values } = env;
  qa.state.chatSourceList = [{ scan_id: 'legacy', usable: false, name: '保存的历史包' }];
  qa.renderChatSourceBar();
  assert.doesNotMatch(element('chatSourceSelect').innerHTML, /value="legacy" disabled/);
  checks.push('saved package remains selectable while readiness is false');

  qa.state.conversation = { session_id: 'a-session', messages: [{ content: '旧包内容' }] };
  qa.state.scopeConstraints = { source_paths: ['old.txt'] };
  element('conversationScopeKind').value = 'files';
  await qa.setChatSource('standard', 'scan-b');
  await new Promise(setImmediate);
  assert.equal(values.get('sjfx_current_scan_id_v1'), 'scan-b');
  qa.syncScan();
  assert.equal(qa.state.scanId, 'scan-b');
  assert.equal(qa.state.conversation, null);
  assert.equal(qa.state.conversationList[0].session_id, 'scan-b-session');
  assert.equal(element('conversationScopeKind').value, 'package');
  assert.deepEqual([...qa.state.scopeConstraints.source_paths || []], []);
  assert.equal(env.workspace.at(-1), 'scan-b');
  checks.push('ordinary selection persists through sync and immediately replaces scope/session list');

  const first = deferred(), second = deferred();
  env.respond(async (url, options) => {
    if (url === '/api/data-sources/select') {
      return JSON.parse(options.body).scan_id === 'scan-first' ? first.promise : second.promise;
    }
    return { ok: true, items: [], total: 0 };
  });
  const selectingFirst = qa.setChatSource('standard', 'scan-first');
  const selectingSecond = qa.setChatSource('standard', 'scan-second');
  second.resolve({ ok: true }); await selectingSecond;
  first.resolve({ ok: true }); await selectingFirst;
  assert.equal(qa.state.scanId, 'scan-second');
  assert.equal(values.get('sjfx_current_scan_id_v1'), 'scan-second');
  checks.push('late package selection cannot replace newer choice');

  const oldSession = deferred();
  env.respond(async (url) => url.includes('/api/conversation/old-session') ? oldSession.promise
    : { ok: true, items: [], total: 0 });
  const opening = qa.openConversation('old-session');
  await qa.setChatSource('large', 'lp-new');
  oldSession.resolve({ ok: true, session: { session_id: 'old-session', scope: { kind: 'files', source_paths: ['old.txt'] } } });
  await opening;
  assert.equal(qa.state.conversation, null);
  assert.equal(qa.state.chatSourceId, 'lp-new');
  checks.push('late history response cannot paint a newly selected package');

  env.respond(async (url) => url.includes('/api/conversation/current-session') ? {
    ok: true, session: { session_id: 'current-session', scope: { kind: 'files', source_paths: ['a.txt', 'b.txt'] }, messages: [] },
    search_index: { usable: false }, turns: []
  } : { ok: true, items: [], total: 0 });
  await qa.openConversation('current-session');
  assert.equal(element('conversationScopeKind').value, 'files');
  assert.equal(element('conversationScopeValue').value, 'a.txt\nb.txt');
  assert.equal(qa.state.searchIndex.usable, false);
  checks.push('opening saved history restores its file scope without pretending the index is ready');

  const paging = deferred();
  qa.state.conversation.message_page = { before_sequence: 10 };
  env.respond(async (url) => url.includes('before_sequence=') ? paging.promise
    : { ok: true, items: [], total: 0 });
  const older = qa.loadOlderConversationMessages();
  await qa.setChatSource('large', 'lp-next');
  paging.resolve({ ok: true, session: { session_id: 'current-session', messages: [{ content: 'older wrong package' }] } });
  await older;
  assert.equal(qa.state.conversation, null);
  checks.push('late older-message response cannot append into another package');

  const restored = environment(filename);
  restored.values.set('sjfx_chat_source_v1', JSON.stringify({ kind: 'large', id: 'lp-restored' }));
  restored.qa.restoreChatSource();
  restored.qa.resetForScan('');
  assert.equal(restored.qa.state.conversation, null);
  assert.equal(restored.qa.state.chatSourceId, 'lp-restored');
  checks.push('restored large source remains separate from general chat and ordinary workspace');
  return { passed: checks.length, checks };
}

const filename = process.argv[2] || path.join(__dirname, '../static/engineering-v2.js');
run(filename).then((result) => {
  fs.writeFileSync(path.join(__dirname, 'qa_frontend_behavior_result.json'), JSON.stringify(result, null, 2));
  process.stdout.write(JSON.stringify(result, null, 2) + '\n');
}).catch((error) => { console.error(error); process.exitCode = 1; });
