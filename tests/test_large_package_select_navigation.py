"""Run real large-package navigation functions in Node, without a browser or API.

The server acceptance controller may set SJFX_NODE to its existing Node binary.
Missing Node is a failure, so this regression cannot silently pass by skipping.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]

NODE_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(path.join(input.root, 'static/large-package.js'), 'utf8');
const shell = fs.readFileSync(path.join(input.root, 'static/product-shell.js'), 'utf8');

function between(text, start, end) {
  const from = text.indexOf(start);
  assert.ok(from >= 0, `Missing source boundary: ${start}`);
  const to = text.indexOf(end, from + start.length);
  assert.ok(to > from, `Missing source boundary: ${end}`);
  return text.slice(from, to);
}

async function main() {
  const packageId = 'navigation fixture/one';
  const requests = [], historyCalls = [], pending = [], resultCalls = [];
  let generationRequests = 0, refreshCount = 0, timerCount = 0;
  const document = {
    body: { dataset: {} },
    getElementById() { return null; },
    querySelectorAll() { return []; }
  };
  const context = vm.createContext({
    document,
    $: id => document.getElementById(id),
    syncDashboard() {}, setNavigationOpen() {}, publishWorkspaceState() {},
    scrollTo() {},
    setInterval() { return ++timerCount; }, clearInterval() {},
    async refresh() { refreshCount += 1; },
    SJFXLargeResult: { activate(id, stage) { resultCalls.push({ id, stage }); } },
    async sjfxApi(url, options = {}) {
      const method = String(options.method || 'GET').toUpperCase();
      if (method !== 'GET') {
        generationRequests += 1;
        throw new Error(`Read-only navigation fixture rejected ${method} ${url}`);
      }
      requests.push({ method, url });
      assert.equal(url, `/api/large-packages/${encodeURIComponent(packageId)}`);
      return {
        large_package: { id: packageId, status: input.status },
        ...(input.workflowStatus ? { workflow: { status: input.workflowStatus } } : {})
      };
    }
  });
  context.window = context;
  context.sjfxLargeResultId = packageId;

  // Execute the production route table, shell activation, selection stage,
  // history entry and polling helpers. Only rendering/refresh are stubbed.
  const program = [
    between(shell, 'const routeNames =', 'let activeRoute ='),
    "let activeRoute = 'dashboard';",
    "let packageId = '', pollingNeeded = true, contextSeq = 0, selected = new Set();",
    "let fileOffset = 0, fileTopic = '', fileCategory = '', lastCatalogAt = 0;",
    "let showAllFiles = false, directoryPending = 0, currentStage = 'import', pollTimer = null;",
    'const api = () => window.sjfxApi;',
    "const activeView = () => document.body.dataset.route === 'large-select';",
    between(source, 'function stopPolling()', 'function activate(route)'),
    between(source, 'function showPanel(show)', 'function render(status, workflow)'),
    between(shell, 'function activate(route)', 'function bind()'),
    between(source, 'window.openLargePackageHistory = async function', 'async function control(action)'),
    'window.SJFXShell = { activate };',
    'window.SJFXLargePackage = { setStage, activate() {} };'
  ].join('\n');
  vm.runInContext(program, context, { timeout: 2000 });

  // Use the actual shell fallback: the internal stage "select" is not a route.
  context.SJFXShell.activate('select');
  assert.equal(document.body.dataset.route, 'dashboard');

  const openHistory = context.openLargePackageHistory;
  context.openLargePackageHistory = (...args) => {
    historyCalls.push(args);
    const promise = openHistory(...args);
    pending.push(promise);
    return promise;
  };
  if (input.requestedStage === 'select') {
    // Cold sidebar entry restores the remembered package through real setStage.
    context.SJFXShell.activate('large-select');
    assert.equal(pending.length, 1);
    await pending[0];
    assert.deepEqual(historyCalls, [[packageId, 'select']]);
    assert.equal(document.body.dataset.route, input.expectedRoute);
    // The already loaded package must also keep a second sidebar click usable.
    context.SJFXShell.activate('large-select');
    assert.equal(historyCalls.length, 1);
  } else {
    await context.openLargePackageHistory(packageId, input.requestedStage);
  }

  assert.equal(document.body.dataset.route, input.expectedRoute);
  assert.equal(context.sjfxLargeResultId, packageId);
  assert.equal(refreshCount, 1);
  assert.equal(requests.length, 1);
  assert.equal(generationRequests, 0);
  if (input.expectedRoute === 'large-catalog' || input.expectedRoute === 'large-overview') {
    assert.deepEqual(resultCalls.at(-1), {
      id: packageId,
      stage: input.expectedRoute === 'large-catalog' ? 'catalog' : 'overview'
    });
  }
  process.stdout.write(JSON.stringify({
    route: document.body.dataset.route, getRequests: requests.length,
    generationRequests, historyCalls: historyCalls.length
  }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize(
    "requested_stage,status,workflow_status,expected_route",
    [
        ("select", "completed", None, "large-select"),
        ("select", "waiting_for_selection", None, "large-select"),
        ("", "completed", None, "large-overview"),
        ("", "waiting_for_selection", None, "large-select"),
        ("large-catalog", "completed", None, "large-catalog"),
        ("large-overview", "waiting_for_selection", None, "large-overview"),
        ("", "running", "completed", "large-overview"),
    ],
    ids=[
        "cold-select-completed", "cold-select-pending", "default-completed",
        "default-pending", "explicit-catalog", "explicit-overview", "workflow-status-wins",
    ],
)
def test_large_package_history_uses_shell_routes(
    requested_stage, status, workflow_status, expected_route
):
    node = os.environ.get("SJFX_NODE") or shutil.which("node")
    if not node:
        pytest.fail("Node is required; add it to PATH or set SJFX_NODE to its existing executable")
    result = subprocess.run(
        [node, "-e", NODE_HARNESS],
        input=json.dumps({
            "root": str(ROOT), "requestedStage": requested_stage, "status": status,
            "workflowStatus": workflow_status, "expectedRoute": expected_route,
        }),
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=ROOT,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["route"] == expected_route
    assert evidence["getRequests"] == 1
    assert evidence["generationRequests"] == 0
