/* Execute the actual turn watcher with controlled HTTP, time, and browser DOM. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function environment(stage, progress, message) {
  const elements = new Map();
  const timers = [];
  const requests = [];
  let now = 100000;
  let eventRequests = 0;
  let status = 'running';
  const storage = { getItem: () => null, setItem() {}, removeItem() {} };
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { textContent: '', className: '' });
    return elements.get(id);
  };
  class Clock extends Date { static now() { return now; } }
  const window = {
    localStorage: storage, sessionStorage: storage,
    setTimeout: (callback, milliseconds) => { timers.push({ callback, milliseconds }); return timers.length; },
    clearTimeout() {},
    SJFXAuth: {
      request: async (url) => {
        requests.push(url);
        let payload;
        if (url.includes('/events?')) {
          eventRequests += 1;
          payload = { ok: true, items: eventRequests === 1
            ? [{ event_id: 1, stage, progress, message }] : [], next_after: 1 };
        } else {
          assert.equal(url, '/api/turns/turn-1');
          const failed = status === 'failed';
          const turn = { id: 'turn-1', assistant_message_id: 'assistant-1', status,
            stage: failed ? 'failed' : stage, progress: failed ? 100 : progress,
            error: failed ? '分析达到运行时限' : null };
          // Running snapshots intentionally omit a message field and contain
          // an older placeholder. The just-received event must survive them.
          payload = { ok: true, turn, steps: [], session: { session_id: 'session-1', messages: [
            { message_id: 'assistant-1', role: 'assistant', content: failed ? turn.error : '正在分析',
              metadata: { stage: failed ? 'failed' : 'executing', progress: failed ? 100 : 62 } },
          ] } };
        }
        return { ok: true, status: 200, json: async () => payload };
      },
    },
  };
  const context = vm.createContext({ window, document: { getElementById: element, addEventListener() {} },
    sessionStorage: storage, Date: Clock, Math, Map, Set });
  const filename = path.join(__dirname, '../static/engineering-v2.js');
  const source = fs.readFileSync(filename, 'utf8').replace("document.addEventListener('DOMContentLoaded', bind);", `
    renderConversation = () => { window.__rendered = messageMarkup(state.conversation.messages[0]); };
    loadConversationList = async () => { window.__finishedRefresh = true; };
    window.__qa = { state, watchAnalysisTurn };
  `);
  vm.runInContext(source, context);
  const qa = window.__qa;
  qa.state.scanId = 'scan-1';
  qa.state.conversation = { session_id: 'session-1', messages: [] };
  return {
    qa, window, requests, element, timers,
    fail: () => { status = 'failed'; },
    tick: async () => {
      const timer = timers.shift();
      assert.ok(timer, 'the live watcher should schedule its next poll');
      now += timer.milliseconds;
      timer.callback();
      await flush();
    },
  };
}

async function flush() {
  for (let index = 0; index < 40; index += 1) await Promise.resolve();
}

async function run() {
  const batch = environment('synthesizing', 76, '模型归并已完成 6/20 批，覆盖 300/1040 份文件');
  batch.qa.watchAnalysisTurn('turn-1');
  await flush();
  assert.equal(batch.requests.filter((url) => !url.includes('/events?')).length, 1);
  assert.match(batch.element('conversationDeepeningState').textContent, /6\/20 批/);
  assert.match(batch.qa.state.turns.get('assistant-1').progress_message, /300\/1040/);
  assert.match(batch.window.__rendered, /6\/20 批/);
  batch.qa.state.watchers.delete('analysis-turn-turn-1');

  const timeout = environment('repairing', 88, '正在补充检索并修订回答');
  timeout.qa.watchAnalysisTurn('turn-1');
  await flush();
  assert.match(timeout.element('conversationDeepeningState').textContent, /88%/);
  timeout.fail();
  // No further events arrive. Idle polls stay lightweight until the periodic
  // snapshot is due; that snapshot must terminate the stale running card.
  for (let index = 0; index < 5; index += 1) await timeout.tick();
  assert.equal(timeout.requests.filter((url) => !url.includes('/events?')).length, 1);
  await timeout.tick();
  assert.equal(timeout.requests.filter((url) => !url.includes('/events?')).length, 2);
  assert.equal(timeout.qa.state.turns.get('assistant-1').status, 'failed');
  assert.equal(timeout.element('conversationDeepeningState').className, 'v2-job-state is-error');
  assert.match(timeout.element('conversationDeepeningState').textContent, /分析达到运行时限/);
  assert.doesNotMatch(timeout.element('conversationDeepeningState').textContent, /88%/);
  assert.doesNotMatch(timeout.window.__rendered, /v2-turn-progress/);
  assert.equal(timeout.qa.state.watchers.has('analysis-turn-turn-1'), false);
  assert.equal(timeout.timers.length, 0);
  assert.equal(timeout.window.__finishedRefresh, true);
  process.stdout.write(JSON.stringify({ passed: 2, checks: [
    'event batch counts survive a subsequent message-less turn snapshot',
    'idle event polling discovers a failed snapshot and stops stale 88% progress',
  ] }, null, 2) + '\n');
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
