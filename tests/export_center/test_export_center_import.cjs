/* Offline real-DOM regression: synthetic sources only; never contacts SJFX. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../..');
const markup = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8')
  .match(/<section class="app-view utility-view" data-view="exports">[\s\S]*?<\/section>/)[0];
let browser;
test.before(async () => { browser = await chromium.launch({ headless: true }); });
test.after(async () => { await browser?.close(); });

async function fixture() {
  const page = await browser.newPage();
  await page.route('**/*', route => route.fulfill({ contentType: 'text/html', body: markup }));
  await page.goto('http://synthetic.invalid/');
  await page.evaluate(() => {
    const f = window.fixture = { posts: [], exports: [], calls: [], pending: [], hold: false, fail: false, jumps: [], notices: [] };
    window.SJFXShell = { route: '', activate(route) { f.jumps.push(route); } };
    window.SJFXDataSources = { open(mode) { f.jumps.push(mode); } };
    window.toast = (message, error) => f.notices.push({ message, error });
    window.authenticatedDownload = async () => {};
    window.sjfxLargeResultId = 'lp-synthetic-a';
    window.sjfxApi = async (url, options = {}) => {
      f.calls.push(url);
      if (url.startsWith('/api/large-packages/history')) return { items: [
        { large_package_id: 'lp-synthetic-a', name: 'Synthetic A', status: 'completed' },
        { large_package_id: 'lp-synthetic-b', name: 'Synthetic B', status: 'completed' },
        { large_package_id: 'lp-synthetic-paused', name: 'Synthetic paused', status: 'paused' },
      ] };
      if (url.startsWith('/api/exports')) return { items: [] };
      if (url.endsWith('/catalog/tree')) return { tree: { themes: [
        { node_id: 'theme-a', name: 'Synthetic group A', files: 502, leaves: [
          { node_id: 'leaf-a1', name: 'Synthetic leaf 1', files: 501 },
          { node_id: 'leaf-a2', name: 'Synthetic leaf 2', files: 1 },
        ] },
        { node_id: 'theme-b', name: 'Synthetic group B', files: 1, leaves: [
          { node_id: 'leaf-b1', name: 'Synthetic leaf 3', files: 1 },
        ] },
      ], leaf_total: 3, file_count: 503 } };
      if (url.includes('/files?')) {
        const leaf = new URL(url, location.href).searchParams.get('node_id');
        const files = leaf === 'leaf-a1'
          ? [{ path: `${leaf}/synthetic-1.txt`, size: 10 }, { path: `${leaf}/synthetic-2.txt`, size: 20 }]
          : [{ path: `${leaf}/synthetic-2.txt`, size: 20 }];
        return { files, has_more: leaf === 'leaf-a1' };
      }
      if (url.endsWith('/catalog/import')) {
        f.posts.push({ url, body: JSON.parse(options.body) });
        if (f.hold) await new Promise(resolve => f.pending.push(resolve));
        if (f.fail) throw Object.assign(new Error('Synthetic retry failure'), { status: 409 });
        return { scan_id: 'scan-synthetic', source_large_package_id: url.split('/')[3], selected_file_count: 501, selected_bytes: 30 };
      }
      if (url.endsWith('/export')) {
        f.exports.push({ url, body: JSON.parse(options.body) });
        return { job_id: 'job-synthetic' };
      }
      if (url === '/api/jobs/job-synthetic') return { job: { status: 'completed', result: {
        file_name: 'synthetic.zip', source_file_count: 501, selection_count: 2, download_url: '/synthetic.zip',
      } } };
      if (url.startsWith('/api/data-sources')) return { items: [{ scan_id: 'scan-synthetic', name: 'Synthetic ordinary' }] };
      if (url.startsWith('/api/tree/')) return { node: { children: [] } };
      throw new Error(`Unexpected synthetic endpoint: ${url}`);
    };
  });
  await page.addScriptTag({ path: path.join(root, 'static/export-center.js') });
  await page.evaluate(() => document.dispatchEvent(new Event('DOMContentLoaded')));
  await page.evaluate(() => window.SJFXExportCenter.activate('exports'));
  await page.locator('#exportTree li[data-key="t:theme-a"]').waitFor();
  return page;
}

const node = (page, key) => page.locator(`#exportTree li[data-key="${key}"]`).first();
const check = (page, key, value = true) => node(page, key).locator(':scope > .ex-row > input').setChecked(value);
const expand = (page, key) => node(page, key).locator(':scope > .ex-row > button').click();
const release = page => page.evaluate(() => { window.fixture.pending.splice(0).forEach(resolve => resolve()); });
const posts = page => page.evaluate(() => window.fixture.posts);

test('existing export-center import preserves nodes, paged scope, file exclusions, metadata, retry key and ordinary jump', async () => {
  const page = await fixture();
  try {
    assert.equal(await page.locator('#exportCatalogImportBtn').isDisabled(), true, 'empty selection blocked');
    await expand(page, 't:theme-a');
    await check(page, 't:theme-a');
    await check(page, 'l:leaf-a2', false);
    await expand(page, 'l:leaf-a1');
    assert.equal(await node(page, 'f:leaf-a1/synthetic-1.txt').locator('input').isChecked(), true, 'paged file inherits selected theme');
    await check(page, 'f:leaf-a1/synthetic-1.txt', false);
    assert.equal(await node(page, 't:theme-a').locator(':scope > .ex-row > input').evaluate(el => el.indeterminate), true, 'partial page exclusion makes theme indeterminate');
    await expand(page, 't:theme-b');
    await expand(page, 'l:leaf-b1');
    await check(page, 'f:leaf-b1/synthetic-2.txt');
    assert.match(await page.locator('#exportCartSummary').textContent(), /501 个文件/, 'estimate retains unseen page members and deducts exclusions');
    await page.locator('#exportTaskTopic').fill('Synthetic ZIP regression');
    await page.locator('#exportSubmitBtn').click();
    await page.locator('#exportResultBox').waitFor({ state: 'visible' });
    const zipped = await page.evaluate(() => window.fixture.exports);
    assert.equal(zipped.length, 1);
    assert.equal(zipped[0].url, '/api/large-packages/lp-synthetic-a/export');
    assert.equal(zipped[0].body.task_topic, 'Synthetic ZIP regression');
    assert.equal(zipped[0].body.name, '大数据包智能目录导出');
    await page.locator('#exportCatalogImportBtn').click();
    await page.locator('#exportImportName').fill('Synthetic selected import');
    await page.locator('#exportImportParseMode').selectOption('fast');
    await page.evaluate(() => { window.fixture.hold = true; window.fixture.fail = true; });
    await page.locator('#exportImportConfirm').click();
    await page.evaluate(() => document.getElementById('exportImportConfirm').dispatchEvent(new MouseEvent('click', { bubbles: true })));
    assert.equal((await posts(page)).length, 1, 'in-flight double click blocked');
    assert.equal(await page.locator('#exportImportConfirm').isDisabled(), true);
    const first = (await posts(page))[0];
    assert.equal(first.url, '/api/large-packages/lp-synthetic-a/catalog/import');
    assert.deepEqual(first.body.node_ids, ['theme-a']);
    assert.deepEqual(first.body.paths, ['leaf-b1/synthetic-2.txt']);
    assert.deepEqual(first.body.exclude_node_ids, ['leaf-a2']);
    assert.deepEqual(first.body.exclude_paths, ['leaf-a1/synthetic-1.txt']);
    for (const field of ['node_ids', 'paths', 'exclude_node_ids', 'exclude_paths']) assert.deepEqual(zipped[0].body[field], first.body[field], `ZIP preserves ${field}`);
    assert.equal(first.body.name, 'Synthetic selected import');
    assert.equal(first.body.parse_mode, 'fast');
    assert.match(first.body.request_key, /^lp-import-lp-synthetic-a-[0-9a-f]{16}$/);
    assert.equal((await page.evaluate(() => window.fixture.calls)).filter(url => url.includes('/files?')).length, 2, 'node import does not fetch or truncate to every file page');
    await release(page);
    await page.locator('#exportImportState.is-error').waitFor();
    assert.equal(await page.locator('#exportImportName').inputValue(), 'Synthetic selected import');
    await page.locator('#exportImportName').fill('Synthetic renamed retry');
    await page.locator('#exportImportParseMode').selectOption('accurate');
    await page.evaluate(() => { window.fixture.hold = false; window.fixture.fail = false; });
    await page.locator('#exportImportConfirm').click();
    await page.locator('[data-import-open-package]').waitFor();
    const retry = (await posts(page))[1];
    assert.equal(retry.body.request_key, first.body.request_key);
    for (const field of ['node_ids', 'paths', 'exclude_node_ids', 'exclude_paths']) assert.deepEqual(retry.body[field], first.body[field], `failure preserves ${field}`);
    if (process.env.SJFX_TEST_EVIDENCE_DIR) await page.screenshot({ path: path.join(process.env.SJFX_TEST_EVIDENCE_DIR, 'synthetic-export-import-result.png'), fullPage: true });
    await page.locator('[data-import-open-package]').click();
    assert.equal(await page.evaluate(() => localStorage.getItem('sjfx_current_scan_id_v1')), 'scan-synthetic');
    assert.deepEqual(await page.evaluate(() => window.fixture.jumps), ['standard']);
    assert.equal(await page.locator('#exportCatalogImportDialog').isHidden(), true, 'ordinary jump closes global modal');
    await page.locator('#exportSourceSelect').selectOption('lp-synthetic-paused');
    await check(page, 't:theme-a');
    assert.equal(await page.locator('#exportCatalogImportBtn').isDisabled(), true, 'paused source blocked');
    await page.locator('[data-export-mode="ordinary"]').click();
    assert.equal(await page.locator('#exportCatalogImportBtn').isHidden(), true);
  } finally { await page.close(); }
});

test('switching source invalidates an open confirmation and ignores a stale import completion', async () => {
  const page = await fixture();
  try {
    await check(page, 't:theme-a');
    await page.locator('#exportCatalogImportBtn').click();
    await page.evaluate(() => { window.fixture.hold = true; });
    await page.locator('#exportImportConfirm').click();
    await page.locator('#exportSourceSelect').selectOption('lp-synthetic-b');
    await node(page, 't:theme-a').waitFor();
    assert.equal(await page.locator('#exportCatalogImportDialog').isHidden(), true, 'source switch must close old confirmation');
    await release(page);
    await page.waitForFunction(() => !document.getElementById('exportImportConfirm').disabled);
    assert.equal(await page.locator('[data-import-open-package]').count(), 0, 'old response must not render in new source context');
    assert.equal(await page.locator('#exportCatalogImportBtn').isDisabled(), true, 'new source starts with no selection');
    await check(page, 't:theme-b');
    await page.locator('#exportCatalogImportBtn').click();
    assert.match(await page.locator('#exportImportName').inputValue(), /^Synthetic B/);
    assert.match(await page.locator('#exportImportSummary').textContent(), /1 个目录/);
    await page.evaluate(() => { window.fixture.hold = false; });
    await page.locator('#exportImportConfirm').click();
    await page.locator('[data-import-open-package]').waitFor();
    const submitted = await posts(page);
    assert.equal(submitted[1].url, '/api/large-packages/lp-synthetic-b/catalog/import');
    assert.deepEqual(submitted[1].body.node_ids, ['theme-b']);
    assert.deepEqual(submitted[1].body.paths, []);
    assert.deepEqual(submitted[1].body.exclude_paths, []);
    assert.deepEqual(submitted[1].body.exclude_node_ids, []);
    assert.notEqual(submitted[1].body.request_key, submitted[0].body.request_key, 'source and selection changes produce another key');
  } finally { await release(page).catch(() => {}); await page.close(); }
});
