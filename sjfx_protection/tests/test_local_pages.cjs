'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

// Execute the real page scripts. Refuse HTML sinks and remote/legacy fetches.
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.disabled = false; this.hidden = false; this._text = ''; }
  set textContent(value) { this._text = String(value ?? ''); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw Error('Untrusted values must use textContent'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  async click() { if (!this.disabled && this.onclick) await this.onclick(); }
}
function buttons(node) { return node.children.flatMap(child => child.tagName === 'button' ? [child] : buttons(child)); }
function named(node, label) { const button = buttons(node).find(item => item.textContent === label); assert.ok(button, label); return button; }
function fixtures(enabled = false) {
  return {
    session: {admin: true, owner: 'alice'}, status: {enabled},
    registrations: {registrations: [{id: 'r1', root: 'D:\\资料\\<img src=x onerror=alert(1)>', name: '<b>本地资料</b>', state: 'ready', manifest_revision: 7}]},
    bindings: {bindings: [{id: 'b1', file_id: 'f1', local_path: 'D:\\资料\\报告.txt', sha256: 'sha-current', state: 'watching'}]},
    actions: {actions: [
      {id: 'a1', binding: 'b1', kind: 'quarantine', state: 'completed', result: JSON.stringify({detail: 'quarantined current instance', actual: {identity: 'vol:1'}})},
      {id: 'a2', binding: 'b1', kind: 'restore', state: 'completed', result: '{}'},
      {id: 'a3', binding: 'b1', kind: 'deny_read', state: 'failed', result: JSON.stringify({status: 'unsupported', code: 'action_receipt_incomplete', detail: 'fixture failure'})},
    ]},
    events: {events: []},
    devices: {devices: [{id: 'd1', name: '<script>device</script>', heartbeat: 1700000000,
      health: JSON.stringify({healthy: true, access_audit_requested: true, access_audit_healthy: false, access_audit_pending_count: 4})}]},
    'verify-ledger': {ok: true, count: 9},
    registration: {registration: {id: 'r1', root: 'D:\\资料'}, manifest_revision: 7, can_use: false,
      blocking_reason: 'inspection_incomplete', local_coverage: {complete: false, gap_epoch: 3},
      alerts: [{reason: 'audit_not_configured'}], jobs: [{state: 'completed'}], files: [
        {file_id: 'f1', relative_path: '<img>review.txt', risk: 'review', version_id: 'sha-current', sha256: 'sha-current', coverage: {complete: true}},
        {file_id: 'f2', relative_path: 'incomplete.txt', risk: 'review', sha256: 'other', coverage: {complete: false}},
        {file_id: 'f3', relative_path: 'dangerous.txt', risk: 'dangerous', sha256: 'danger', coverage: {complete: true}},
      ]},
    trace: {file: {path: 'D:\\资料\\报告.txt', sha256: 'sha-current', collection_state: 'ready', risk: 'review'}, instances: [
      {id: 'direct', path: 'D:\\直接.txt', identity: 'native:1', version_id: 'sha-direct', state: 'available', content_matches: true},
      {id: 'assistant', local_path: 'D:\\助手.txt', local_identity: 'native:2', sha256: 'sha-assistant', state: 'watching', content_matches: true, online: true, monitor_healthy: true},
    ]}, review: {ok: true},
  };
}
async function load(page = 'admin', data = fixtures(), extra = {}) {
  const html = fs.readFileSync(path.join(__dirname, '../web', page + '.html'), 'utf8');
  const nodes = new Map();
  for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const element = new Element(); element.hidden = /\bhidden\b/.test(match[0]); nodes.set(match[1], element);
  }
  const calls = [], redirects = [], prompts = [];
  const context = vm.createContext({
    document: {getElementById(id) { assert.ok(nodes.has(id), 'Unknown DOM id ' + id); return nodes.get(id); }, createElement(tag) { return new Element(tag); }},
    location: {replace(url) { redirects.push(url); }},
    prompt(message) { prompts.push(message); return extra.reason ?? 'current fixture version reviewed'; },
    async fetch(url, options) {
      assert.ok(url.startsWith('/protection/api/'), 'Request must stay on SJFX origin: ' + url);
      assert.equal(options.credentials, 'same-origin');
      const parsed = new URL(url, 'http://sjfx.test'), route = parsed.pathname.split('/').pop();
      assert.ok(!['enrollments', 'collections', 'staging-version'].includes(route), 'Legacy browser route');
      const call = {url, route, options, query: parsed.searchParams, body: options.body ? JSON.parse(options.body) : undefined}; calls.push(call);
      if (options.method === 'POST') {
        assert.equal(options.headers['X-SJFX-Protection'], '1');
        assert.equal(options.headers['Content-Type'], 'application/json');
        return {ok: true, json: async () => ({ok: true})};
      }
      const result = typeof data[route] === 'function' ? data[route](call) : data[route];
      assert.notEqual(result, undefined, 'Missing fixture for ' + route);
      return {ok: !result.error, json: async () => result};
    }, Date, JSON, encodeURIComponent,
  });
  const inline = html.match(/<script>([\s\S]*?)<\/script>/);
  const script = inline ? inline[1] : fs.readFileSync(path.join(__dirname, '../web', page + '.js'), 'utf8');
  await vm.runInContext(script + '\nready;', context, {filename: page + '.html'});
  return {html, nodes, calls, redirects, prompts, context, get: id => nodes.get(id)};
}

test('local introduction renders supplied paths as text and only uses same-origin registration APIs', async () => {
  const data = fixtures(), page = await load('index', data);
  assert.match(page.get('packages').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(page.get('adminLink').hidden, false);
  assert.deepEqual(page.calls.map(call => call.route).sort(), ['registrations', 'session', 'status']);
  data.session = {admin: false}; data.registrations = {registrations: []};
  const ordinary = await load('index', data);
  assert.equal(ordinary.get('adminLink').hidden, true);
  assert.match(ordinary.get('packages').textContent, /尚未登记资料/);
});

test('administrator page redirects ordinary users before requesting diagnostics', async () => {
  const data = fixtures(); data.session = {admin: false};
  const page = await load('admin', data);
  assert.deepEqual(page.redirects, ['/protection/packages']);
  assert.deepEqual(page.calls.map(call => call.route), ['session']);
});

test('OFF disables new dispositions but restores the exact completed original action', async () => {
  const page = await load();
  for (const label of ['隔离文件', '限制读取', '防止删除']) {
    const button = named(page.get('bindings'), label);
    assert.equal(button.disabled, true); await button.click();
  }
  assert.equal(page.calls.filter(call => call.body).length, 0);
  assert.equal(buttons(page.get('actions')).length, 1);
  await named(page.get('actions'), '恢复此具体处置').click();
  const post = page.calls.find(call => call.body);
  assert.deepEqual(post.body, {binding_id: 'b1', kind: 'restore', restore_action_id: 'a1', reason: 'current fixture version reviewed'});
  assert.match(page.get('actions').textContent, /quarantined current instance/);
  assert.match(page.get('actions').textContent, /action_receipt_incomplete/);
  assert.match(page.get('notice').textContent, /保护关闭/);
});

test('ON sends a concrete binding disposition and a locked instance prevents repeat dispositions', async () => {
  const data = fixtures(true), page = await load('admin', data, {reason: ' quarantine this inspected version '});
  await named(page.get('bindings'), '隔离文件').click();
  assert.deepEqual(page.calls.find(call => call.body).body, {binding_id: 'b1', kind: 'quarantine', reason: 'quarantine this inspected version'});
  data.bindings.bindings[0].state = 'quarantined';
  await page.get('refresh').click();
  for (const label of ['隔离文件', '限制读取', '防止删除']) assert.equal(named(page.get('bindings'), label).disabled, true);
});

test('trace supports direct-reader and assistant instances and does not assert copy causality', async () => {
  const data = fixtures(); data.events = {events: [{action: 'file.read', occurred: 1700000000, device: 'd1', instance_id: 'assistant',
    payload: JSON.stringify({payload: {path: 'D:\\助手.txt', identity: 'native:2', version_id: 'sha-assistant',
      version_evidence: 'binding_at_collection', registration_id: 'r1', gap_epoch: 3, user: '<b>user</b>', process: 'reader.exe', process_id: 42}})}]};
  const page = await load('admin', data);
  await named(page.get('bindings'), '实例与履历').click();
  assert.equal(page.get('traceSection').hidden, false);
  const trace = page.get('traceDetail').textContent;
  for (const value of ['D:\\直接.txt', 'D:\\助手.txt', 'native:1', 'native:2', 'sha-direct', 'sha-assistant', '不证明复制因果']) assert.ok(trace.includes(value), value);
  const evidence = page.get('events').textContent;
  for (const value of ['版本引用来自采集时绑定', '缺口代次：3', '<b>user</b>', 'reader.exe（42）', '资料包：r1']) assert.ok(evidence.includes(value), value);
  assert.equal(page.calls.filter(call => call.route === 'events').at(-1).query.get('file_id'), 'f1');
  await page.get('allEvents').click();
  assert.equal(page.calls.filter(call => call.route === 'events').at(-1).query.get('file_id'), '');
});

test('coverage shows local gaps and review is limited to a complete current review version', async () => {
  const page = await load();
  await named(page.get('registrations'), '检查与覆盖').click();
  assert.equal(page.get('detailSection').hidden, false);
  const coverage = JSON.parse(page.get('coverage').textContent);
  assert.equal(coverage.can_use, false);
  assert.equal(coverage.local_coverage.gap_epoch, 3);
  assert.equal(coverage.blocking_reason, 'inspection_incomplete');
  assert.match(page.get('files').textContent, /<img>review.txt/);
  assert.equal(buttons(page.get('files')).filter(button => button.textContent === '复核当前版本').length, 1);
  await named(page.get('files'), '复核当前版本').click();
  assert.deepEqual(page.calls.find(call => call.route === 'review').body, {file_id: 'f1', sha256: 'sha-current', reason: 'current fixture version reviewed'});
});

test('device diagnostics distinguish requested audit from healthy audit and ledger works while OFF', async () => {
  const data = fixtures(), page = await load('admin', data);
  assert.match(page.get('devices').textContent, /访问审计请求：已请求；访问审计健康：未证明/);
  assert.match(page.get('devices').textContent, /待配置访问审计文件：4/);
  await page.get('verifyLedger').click();
  assert.match(page.get('ledger').textContent, /核对通过，共 9/);
  data.devices = {error: 'diagnostic unavailable'};
  await page.get('refresh').click();
  assert.match(page.get('deviceNotice').textContent, /diagnostic unavailable/);
  assert.match(page.get('registrations').textContent, /<b>本地资料<\/b>/);
  assert.equal(named(page.get('actions'), '恢复此具体处置').disabled, false);
  data['verify-ledger'] = {ok: false, first_invalid_seq: 17};
  await page.get('verifyLedger').click();
  assert.match(page.get('ledger').textContent, /异常序号：17/);
});

test('binding and event pagination use server offsets and stop at a short page', async () => {
  const data = fixtures();
  const binding = data.bindings.bindings[0];
  data.bindings = call => ({bindings: call.query.get('offset') === '0' ? Array.from({length: 500}, (_, n) => ({...binding, id: 'b' + n})) : [binding]});
  data.events = call => ({events: call.query.get('offset') === '0' ? Array.from({length: 200}, () => ({action: 'system.request', payload: '{}'})) : []});
  const page = await load('admin', data);
  assert.equal(page.get('bindingPrev').disabled, true); assert.equal(page.get('bindingNext').disabled, false);
  await page.get('bindingNext').click();
  assert.equal(page.calls.filter(call => call.route === 'bindings').at(-1).query.get('offset'), '500');
  assert.equal(page.get('bindingNext').disabled, true);
  await page.get('bindingPrev').click();
  assert.equal(page.calls.filter(call => call.route === 'bindings').at(-1).query.get('offset'), '0');
  await page.get('eventNext').click();
  assert.equal(page.calls.filter(call => call.route === 'events').at(-1).query.get('offset'), '200');
  assert.equal(page.get('eventNext').disabled, true);
  await page.get('eventPrev').click();
  assert.equal(page.calls.filter(call => call.route === 'events').at(-1).query.get('offset'), '0');
});

test('both adapted pages contain no browser pairing, server registration, or assistant bearer flow', async () => {
  for (const name of ['index', 'admin']) {
    const page = await load(name);
    assert.ok(!/18766|localhost|127\.0\.0\.1|Authorization|Bearer|enrollments|server_root|local_url|双路径|配对码/.test(page.html), name);
  }
});
