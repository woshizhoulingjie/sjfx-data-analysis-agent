"""Server-only VM regressions for cold results and the import return handler."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const read = name => fs.readFileSync(path.join(input.root, 'static', name), 'utf8');
const app = read('app.js'), large = read('large-package.js');
const result = read('large-package-result.js'), shell = read('product-shell.js');
function between(text, start, end) {
  const from = text.indexOf(start), to = text.indexOf(end, from + start.length);
  assert.ok(from >= 0 && to > from, `Source boundaries: ${start} / ${end}`);
  return text.slice(from, to);
}
function element(id) {
  const classes = new Set();
  return {
    id, hidden: false, disabled: false, dataset: {}, textContent: '',
    style: { display: '', removeProperty(key) { delete this[key]; } },
    classList: {
      toggle(name, on) { on ? classes.add(name) : classes.delete(name); },
      contains(name) { return classes.has(name); }
    },
    // HTML hidden reflects attribute presence as a boolean property.
    setAttribute(name, value) {
      if (name === 'hidden') this.hidden = true;
      else this[name] = value;
    },
    removeAttribute(name) {
      if (name === 'hidden') this.hidden = false;
      else delete this[name];
    },
    querySelector() { return null; }, focus() {}, scrollIntoView() {}
  };
}
async function main() {
  const elements = new Map(), calls = [], requests = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  let generationRequests = 0;
  const readOnly = async (url, options = {}) => {
    if (String(options.method || 'GET').toUpperCase() !== 'GET') {
      generationRequests += 1;
      throw new Error('Navigation fixture rejects all writes');
    }
    requests.push(url);
    assert.ok(url.startsWith('/api/large-packages/history?'), url);
    return { items: input.empty ? [] : [{ large_package_id: 'lp-fixture', status: 'completed' }], total: input.empty ? 0 : 1 };
  };
  if (input.scenario === 'result') {
    const document = {
      body: { dataset: { route: input.stage === 'catalog' ? 'large-catalog' : 'large-overview' } },
      querySelector: () => null, querySelectorAll: () => []
    };
    const context = vm.createContext({
      document, $: get,
      ensureExplorer() {}, resetBrowser() {}, renderSelection() {},
      ensureExportCenterEntry: () => get('exportEntry'),
      setText(id, text) { get(id).textContent = text; },
      async load() { calls.push('load'); },
      sjfxApi: readOnly,
      SJFXShell: { activate() { throw new Error('Already in the requested result route'); } }
    });
    context.window = context;
    // A stale global must not expose an action when history contains no package.
    if (input.empty) context.sjfxLargeResultId = 'stale-package';
    vm.runInContext([
      "let packageId = '', extraCatalogNodes = [], currentStage = 'overview', activationId = 0;",
      'const api = () => window.sjfxApi;',
      between(result, 'function setStage(stage)', 'function openExportCenter()'),
      between(result, 'async function activate(id, stage = currentStage)', 'window.SJFXLargeResult ='),
      'window.openResult = activate;'
    ].join('\n'), context, { timeout: 2000 });
    await context.openResult('', input.stage);
    assert.equal(get('largeResultToSelect').hidden, input.empty);
    assert.equal(get('exportEntry').disabled, input.empty);
    assert.equal(get('largeResultToCatalog').disabled, input.stage === 'catalog');
    assert.equal(get('largeResultToOverview').disabled, input.stage === 'overview');
    assert.equal(calls.filter(x => x === 'load').length, input.empty ? 0 : 1);
    if (!input.empty) assert.equal(context.sjfxLargeResultId, 'lp-fixture');
    assert.equal(requests.length, 1);
  } else {
    const modes = [{ value: 'standard', checked: true }, { value: 'large', checked: false }];
    const kinds = [get('standardKind'), get('largeKind')];
    kinds[0].dataset.sourceKind = 'standard'; kinds[1].dataset.sourceKind = 'large';
    let handler;
    const document = {
      body: { dataset: {} },
      querySelector(selector) {
        if (selector === '[data-back-source]') return { addEventListener(type, fn) { handler = fn; } };
        if (selector === 'input[name="workflowMode"]:checked') return modes.find(x => x.checked);
        return null;
      },
      querySelectorAll(selector) {
        if (selector === 'input[name="workflowMode"]') return modes;
        if (selector === '[data-source-kind]') return kinds;
        return [];
      }
    };
    const state = {
      dataSourceKind: 'standard', dataSourceOffset: 24, dataSourceQuery: '',
      dataSourceRequestInFlight: false, dataSources: [], dataSourceTotal: 0
    };
    const context = vm.createContext({
      document, state, $: get, URLSearchParams,
      invalidateSelectionSession() {}, setModeText() {},
      detachUiFromCurrentWork() { calls.push('detach'); },
      syncDashboard() {}, setNavigationOpen() {}, publishWorkspaceState() {}, scrollTo() {},
      sessionStorage: { getItem() { return ''; } },
      renderDataSources() { calls.push('render-history'); }, api: readOnly
    });
    context.window = context;
    vm.runInContext([
      between(shell, 'const routeNames =', 'let activeRoute ='),
      "let activeRoute = 'dashboard', packageId = '', currentStage = 'import';",
      between(app, 'function setPackageStage(stage)', 'async function openSelectionPanel('),
      between(large, 'function setStage(stage)', 'function render(status, workflow)'),
      between(shell, 'function activate(route)', 'function bind()'),
      between(app, 'async function refreshDataSources(', 'async function selectDataSource('),
      between(app, 'window.SJFXDataSources = {', 'function jobIdOf('),
      'window.SJFXShell = { activate };',
      'window.SJFXLargePackage = { setStage, activate() {} };',
      between(app, "document.querySelector('[data-back-source]')", "previousButton?.addEventListener")
    ].join('\n'), context, { timeout: 2000 });
    const pending = [], refresh = context.refreshDataSources;
    context.refreshDataSources = (...args) => {
      const promise = refresh(...args); pending.push(promise); return promise;
    };
    if (input.kind === 'large') {
      context.SJFXShell.activate('large-import');
      assert.equal(get('dataSourcePanel').style.display, 'none');
    } else {
      context.SJFXShell.activate('packages');
      context.setPackageStage('import');
    }
    assert.equal(typeof handler, 'function');
    handler();
    await Promise.all(pending);
    assert.notEqual(get('dataSourcePanel').style.display, 'none');
    assert.equal(get('dataSourcePanel').classList.contains('hidden'), false);
    if (input.kind === 'large') {
      assert.equal(document.body.dataset.route, 'packages');
      assert.equal(state.dataSourceKind, 'large');
      assert.equal(state.dataSourceOffset, 0);
      assert.equal(kinds[1].classList.contains('active'), true);
      assert.equal(kinds[0].classList.contains('active'), false);
      assert.equal(get('largePackagePanel').hidden, true);
      assert.equal(state.dataSources[0].large_package_id, 'lp-fixture');
      assert.deepEqual(calls, ['detach', 'render-history']);
      assert.equal(requests.length, 1);
      assert.ok(requests[0].startsWith('/api/large-packages/history?'));
    } else {
      assert.equal(state.dataSourceKind, 'standard');
      assert.equal(state.dataSourceOffset, 24);
      assert.deepEqual(calls, []);
      assert.equal(requests.length, 0);
    }
  }
  assert.equal(generationRequests, 0);
  process.stdout.write(JSON.stringify({ passed: true, generationRequests }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
"""


def run_navigation_case(case):
    node = os.environ.get("SJFX_NODE") or shutil.which("node")
    if not node:
        pytest.fail("Node is required; set SJFX_NODE or add it to PATH")
    result = subprocess.run(
        [node, "-e", HARNESS], input=json.dumps({"root": str(ROOT), **case}),
        text=True, encoding="utf-8", capture_output=True, cwd=ROOT, timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"passed": True, "generationRequests": 0}


@pytest.mark.parametrize("stage,empty", [("catalog", False), ("overview", False), ("catalog", True)])
def test_cold_result_actions_use_the_resolved_package(stage, empty):
    run_navigation_case({"scenario": "result", "stage": stage, "empty": empty})


@pytest.mark.parametrize("kind", ["large", "standard"])
def test_import_return_uses_the_correct_history_entry(kind):
    run_navigation_case({"scenario": "return", "kind": kind})
