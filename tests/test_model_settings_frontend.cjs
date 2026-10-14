'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const screenshotDir = process.env.SJFX_TEST_SCREENSHOT_DIR;
const initial = {
  ok: true, model_interface_version: 2, restart_required: false, apply_mode: 'restart_all_roles', notice: '保存后重启 Web 与全部 Worker。',
  active: {
    generation: {backend: 'openai_compatible', base_url: 'http://127.0.0.1:8001/v1', model: 'chat-old', context_tokens: 4096,
      timeout_seconds: 180, concurrency: 1, enabled: true, supports_json_response_format: false, api_key_configured: true,
      tokenizer_path: '/old/tokenizer', tokenizer_version: 'old-v1', model_version: 'old-v1', tokenizer_status: 'unavailable',
      service_location: 'SJFX 所在机器'},
    embedding: {backend: 'openai_compatible', base_url: 'http://127.0.0.1:8002/v1', model: 'e5-old', timeout_seconds: 12,
      concurrency: 1, dimension: 384, encoding: 'e5', tokenizer_path: '/old/embed-tokenizer', version: 'old-v1', weights_id: 'old-weight',
      config_id: 'old-config', api_key_configured: true, service_location: 'SJFX 所在机器', lane_overrides: true,
      large_fallback_base_url: 'http://127.0.0.1:8004/v1', lanes: {
        ordinary: {base_url: 'http://127.0.0.1:8002/v1', model: 'e5-old'},
        large: {base_url: 'http://127.0.0.1:8003/v1', model: 'e5-old'},
        mail: {base_url: 'http://127.0.0.1:8002/v1', model: 'mail-old'}}}
  }
};
initial.saved = structuredClone(initial.active);
const html = '<!doctype html><meta charset="utf-8"><title>模型设置测试</title><style>body{font-family:Arial,sans-serif;margin:24px;color:#17211d;max-width:1100px}button{padding:10px;border:1px solid #ccc;border-radius:5px}</style><link rel="stylesheet" href="/static/model-settings.css"><main id="modelSettingsPanel"></main><script src="/static/model-settings.js" defer></script>';
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 960}});
    const errors = [], requests = [];
    let state = structuredClone(initial), unauthorized = false, testFailure = false;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://settings.test/**', async route => {
      const req = route.request(), url = new URL(req.url());
      const send = (data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: html});
      if (url.pathname.startsWith('/static/')) return route.fulfill({contentType: url.pathname.endsWith('.css') ? 'text/css' : 'application/javascript', body: fs.readFileSync(path.join(root, url.pathname.slice(1)))});
      if (url.pathname === '/api/admin/model-settings') {
        if (unauthorized) return send({ok: false, error: '需要管理员权限'}, 403);
        if (req.method() === 'GET') return send(state);
        const draft = req.postDataJSON(); requests.push({kind: 'save', body: draft});
        assert.equal(req.headers()['x-sjfx-model-settings'], '1');
        for (const role of Object.keys(draft)) {
          Object.assign(state.saved[role], draft[role]);
          delete state.saved[role].api_key; delete state.saved[role].clear_api_key;
          state.saved[role].api_key_configured = Boolean(draft[role].api_key) || !draft[role].clear_api_key && state.saved[role].api_key_configured;
          if (draft[role].base_url && !draft[role].base_url.includes('127.0.0.1')) state.saved[role].service_location = '指定内网或服务器';
          if (role === 'generation') state.saved[role].tokenizer_status = draft[role].tokenizer_path ? 'unavailable' : 'not_configured';
        }
        state.restart_required = true; return send(state);
      }
      if (url.pathname === '/api/admin/model-test') {
        const body = req.postDataJSON(); requests.push({kind: 'test', body});
        if (testFailure) return send({ok: false, error: '认证凭据格式无效'}, 400);
        return send({ok: true, tested_configuration: 'draft', roles: {[body.role]: {reachable: true, configured_model_present: true, inference: true, checks: {chat: {ok: true}}, capability: {chat: true, structured_output: true}}}});
      }
      return send({}, 404);
    });
    await page.goto('http://settings.test/');
    await page.getByRole('button', {name: '保存配置', exact: true}).waitFor();
    assert.match(await page.locator('.model-settings-status').textContent(), /当前运行一致/);
    assert.match(await page.locator('.model-lane-notice').textContent(), /8003.*mail-old.*8004.*清除/);
    assert.equal(await page.locator('#model-generation-context_tokens').getAttribute('min'), '2048');
    assert.equal(await page.locator('#model-generation-api_key').inputValue(), '');
    if (screenshotDir) { fs.mkdirSync(screenshotDir, {recursive: true}); await page.screenshot({path: path.join(screenshotDir, 'model-settings-active.png'), fullPage: true}); }

    await page.locator('#model-generation-model').fill('chat-new');
    assert.match(await page.locator('.model-settings-status').textContent(), /未保存修改/);
    assert.equal(await page.locator('#model-generation-tokenizer_path').inputValue(), '');
    assert.equal(await page.locator('#model-generation-model_version').inputValue(), '');
    await page.locator('#model-generation-context_tokens').fill('2048');
    await page.locator('#model-generation-base_url').fill('https://models.customer.example/v1');
    assert.equal(await page.locator('#model-generation-clear_api_key').isChecked(), true);
    await page.locator('#model-generation-api_key').fill('draft-test-secret');
    assert.equal(await page.locator('#model-generation-clear_api_key').isChecked(), false);
    await page.getByRole('button', {name: '测试生成模型', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('.model-settings-status').textContent.includes('测试的是表单草稿'));
    assert.equal(requests.length, 1);
    assert.equal(requests[0].kind, 'test');
    assert.deepEqual(Object.keys(requests[0].body.settings), ['generation']);
    assert.equal(requests[0].body.settings.generation.api_key, 'draft-test-secret');
    assert.equal(await page.locator('#model-generation-api_key').inputValue(), '');
    assert.match(await page.locator('.model-settings-summary').textContent(), /chat-old/);

    await page.locator('#model-embedding-model').fill('embed-new');
    assert.equal(await page.locator('#model-embedding-dimension').inputValue(), '0');
    assert.equal(await page.locator('#model-embedding-encoding').inputValue(), 'plain');
    for (const name of ['version', 'weights_id', 'config_id', 'tokenizer_path']) assert.equal(await page.locator('#model-embedding-' + name).inputValue(), '');
    await page.getByRole('button', {name: '保存配置', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('.model-settings-summary tbody tr:last-child').textContent.includes('chat-new'));
    const saved = requests.find(item => item.kind === 'save').body;
    assert.equal(saved.generation.context_tokens, 2048);
    assert.equal(saved.generation.tokenizer_path, '');
    assert.equal(saved.embedding.dimension, 0);
    assert.equal(saved.embedding.encoding, 'plain');
    assert.equal(saved.generation.api_key, undefined, 'cleared draft secrets never leak into a later save');
    assert.match(await page.locator('.model-settings-summary').textContent(), /当前运行.*chat-old.*已保存.*chat-new/s);
    if (screenshotDir) await page.screenshot({path: path.join(screenshotDir, 'model-settings-saved-pending.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'mobile model controls must fit the viewport');
    if (screenshotDir) await page.screenshot({path: path.join(screenshotDir, 'model-settings-mobile.png'), fullPage: true});
    await page.setViewportSize({width: 1280, height: 960});

    await page.locator('#model-generation-backend').selectOption('ollama');
    assert.equal(await page.locator('#model-generation-base_url').inputValue(), '');
    assert.equal(await page.locator('#model-generation-model').inputValue(), '');
    assert.equal(await page.locator('#model-generation-clear_api_key').isChecked(), true);
    await page.locator('#model-generation-base_url').fill('http://192.168.10.20:11434');
    await page.locator('#model-generation-model').fill('chat-native');
    await page.locator('#model-generation-api_key').fill('new-native-test-secret');
    testFailure = true;
    await page.getByRole('button', {name: '测试生成模型', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('.model-settings-status').textContent.includes('认证凭据格式无效'));
    assert.equal(await page.locator('#model-generation-api_key').inputValue(), '');
    assert(!((await page.locator('body').textContent()).includes('new-native-test-secret')));
    await page.locator('#model-embedding-backend').selectOption('disabled');
    assert.equal(await page.locator('#model-embedding-base_url').isDisabled(), true);
    await page.getByRole('button', {name: '保存配置', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('.model-settings-summary tbody tr:last-child').textContent.includes('关闭向量模型 /'));
    assert.deepEqual(requests.filter(item => item.kind === 'save').at(-1).body.embedding, {backend: 'disabled'});
    assert.deepEqual(errors, []);

    // A deployed static script can meet a still-running previous Web process.
    // Preserve its read-only values and block every write until it restarts.
    state = structuredClone(initial);
    delete state.model_interface_version;
    for (const profiles of [state.active, state.saved]) {
      for (const role of ['generation', 'embedding']) {
        for (const field of ['tokenizer_path', 'tokenizer_version', 'tokenizer_status', 'model_version', 'service_location', 'encoding', 'lanes', 'lane_overrides', 'large_fallback_base_url']) delete profiles[role][field];
      }
    }
    await page.reload();
    await page.waitForFunction(() => document.querySelector('.model-settings-status').textContent.includes('新模型配置代码已部署'));
    assert.match(await page.locator('.model-settings-status').textContent(), /请先重启 Web 与全部 Worker 以启用/);
    assert.match(await page.locator('.model-settings-summary').textContent(), /当前运行.*chat-old.*已保存.*e5-old/s);
    assert.equal(await page.locator('input,select').count(), 0);
    for (const title of ['保存配置', '测试生成模型', '测试向量模型']) assert.equal(await page.getByRole('button', {name: title, exact: true}).isDisabled(), true);
    assert.equal(await page.getByRole('button', {name: '重新读取', exact: true}).isEnabled(), true);
    const writeCount = requests.length;
    await page.locator('form').evaluate(el => el.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
    await page.getByRole('button', {name: '测试生成模型', exact: true}).evaluate(el => el.dispatchEvent(new Event('click', {bubbles: true})));
    assert.equal(requests.length, writeCount, 'old interface must never receive a new PUT or diagnostic payload');
    if (screenshotDir) await page.screenshot({path: path.join(screenshotDir, 'model-settings-upgrade-required.png'), fullPage: true});
    state = structuredClone(initial);
    await page.getByRole('button', {name: '重新读取', exact: true}).click();
    await page.locator('#model-generation-base_url').waitFor();
    assert.equal(await page.getByRole('button', {name: '保存配置', exact: true}).isEnabled(), true);
    assert.equal(await page.getByRole('button', {name: '测试生成模型', exact: true}).isEnabled(), true);

    unauthorized = true;
    await page.reload();
    await page.waitForFunction(() => document.querySelector('.model-settings-status').textContent.includes('需要管理员权限'));
    assert.equal(await page.locator('input,select').count(), 0);
    console.log('PASS: Chromium verifies draft versus active/saved state, independent role tests, secret handling, identity resets, 2048 window, legacy lane notice, admin-only UI and read-only upgrade gating for an old Web API.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
