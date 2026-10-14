/* Execute the shipped console controller and deliberately resolve requests out of order. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor() { this.children = []; this.listeners = {}; this.style = {}; this.hidden = false; this.value = ''; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) { this.children = children.flatMap(child => child.fragment ? child.children : [child]); }
  prepend(child) { this.children.unshift(...(child.fragment ? child.children : [child])); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  scrollIntoView() {}
  click() { return this.listeners.click?.(); }
}
const ids = new Map();
const doc = { getElementById(id) { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); },
  createElement() { return new Element(); }, createDocumentFragment() { const el = new Element(); el.fragment = true; return el; } };
const requests = [];
const context = { document: doc, window: {}, location: {}, AbortController, setTimeout, clearTimeout, console,
  fetch(url, options) { return new Promise(resolve => requests.push({ url, options, resolve })); } };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../sjfx_multiuser/web_assets/admin-materials.js'), 'utf8'), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
function respond(request, data) { request.resolve({ status: 200, ok: true, json: async () => ({ ok: true, ...data }) }); }
function page(name, kind = 'ordinary', total = 1) {
  return { kind, items: [{ id: name, source_id: name, name, root: '/safe/' + name, status: 'completed', updated_at: 1 }],
    total, limit: 25, offset: 0, has_more: total > 25 };
}
(async () => {
  doc.getElementById('materialKind').value = 'ordinary';
  const control = context.window.SJFXAdminMaterials;
  control.setUsers([{ user_id: 'a', owner_id: 'owner-a', username: 'alice' },
    { user_id: 'b', owner_id: 'owner-b', username: 'bob' }], { role: 'admin', owner_id: 'primary' });
  control.open('a'); const oldAccountRequest = requests.at(-1);
  control.open('b'); const newAccountRequest = requests.at(-1);
  assert.equal(oldAccountRequest.options.signal.aborted, true);
  respond(newAccountRequest, page('bob-package')); await tick();
  respond(oldAccountRequest, page('alice-stale-package')); await tick();
  assert.equal(doc.getElementById('materialRows').children[0].children[0].textContent, 'bob-package');
  assert.match(doc.getElementById('materialTitle').textContent, /bob/);

  const selector = doc.getElementById('materialKind');
  selector.value = 'conversations'; selector.listeners.change();
  const historyListRequest = requests.at(-1);
  selector.value = 'large'; selector.listeners.change();
  const largeRequest = requests.at(-1);
  respond(largeRequest, page('bob-large', 'large')); await tick();
  respond(historyListRequest, page('bob-stale-session', 'conversations')); await tick();
  assert.equal(doc.getElementById('materialRows').children[0].children[0].textContent, 'bob-large');

  selector.value = 'conversations'; selector.listeners.change();
  respond(requests.at(-1), page('bob-session', 'conversations')); await tick();
  const historyButton = doc.getElementById('materialRows').children[0].children[4].children[0];
  historyButton.click(); const oldMessageRequest = requests.at(-1);
  control.open('a'); const aliceListRequest = requests.at(-1);
  respond(aliceListRequest, { ...page('alice-session', 'conversations'), items: [] }); await tick();
  respond(oldMessageRequest, { title: 'bob private history', messages: [{ role: 'user', content: 'old text' }], total: 1, offset: 0, has_more: false });
  await tick();
  assert.equal(doc.getElementById('materialConversation').hidden, true);
  assert.equal(doc.getElementById('materialMessages').children.length, 0);
  assert.match(doc.getElementById('materialTitle').textContent, /alice/);
  assert.equal(doc.getElementById('materialNext').disabled, true);
  console.log('admin-materials controller: late account/list/message responses are ignored');
})().catch(error => { console.error(error); process.exitCode = 1; });
