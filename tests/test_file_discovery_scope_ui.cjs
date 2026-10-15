// Pure fixtures prepared for later execution; no DOM, browser, network or app.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'engineering-v2.js'), 'utf8');
const start = source.indexOf('  async function attachFileResults(question, turn) {');
const end = source.indexOf('  async function sendQuestion(', start);
assert(start >= 0 && end > start);
const fn = source.slice(start, end);

async function run(scanId, resultPayload, storedPayload = {}, topPayload = {}) {
  const calls = [];
  const stored = {intent: {name: 'file_search'}, ...storedPayload};
  const state = {chatSourceVersion: 1, conversationContextVersion: 1,
    turns: new Map([['assistant-fixture', stored]])};
  const context = {state, chatScanId: () => scanId,
    loadFileSummaries: async () => ({}), basename: value => value.split('/').pop(),
    renderConversation: () => {}, fileSearchQuery: () => '巡检',
    api: async (url, options) => { calls.push({url, body: JSON.parse(options.body)});
      return {matched_files: [{path: '包级回退不应出现.txt'}], matched_file_count: 1}; }};
  vm.createContext(context);
  vm.runInContext(fn + '\nthis.attach = attachFileResults;', context);
  await context.attach('查找 巡检', {assistant_message_id: 'assistant-fixture',
    intent: {name: 'file_search'}, result: resultPayload, ...topPayload});
  return {calls, stored};
}

(async () => {
  const checks = [];
  // A scoped backend empty list must stay empty and must not call package search.
  let value = await run('e', {related_file_paths: []},
    {file_results: [{path: '过期包级卡片.txt'}], file_results_total: 1});
  assert.equal(value.calls.length, 0);
  assert.equal(value.stored.file_results.length, 0);
  assert.equal(value.stored.file_results_total, 0);
  checks.push('ordinary_empty_clears_stale_cards');
  // Normal backend matches remain the complete file-card set.
  value = await run('e', {related_file_paths: ['甲/巡检.txt']});
  assert.equal(value.calls.length, 0);
  assert.equal(value.stored.file_results[0].path, '甲/巡检.txt');
  checks.push('ordinary_matches_are_authoritative');
  // Direct top-level result arrays are also authoritative for ordinary scans.
  value = await run('e', {}, {}, {related_file_paths: []});
  assert.equal(value.calls.length, 0);
  checks.push('top_level_empty_is_authoritative');
  // Legacy responses that omit the field retain their previous fallback.
  value = await run('e', {});
  assert.equal(value.calls.length, 1);
  checks.push('omitted_field_retains_fallback');
  // The existing large-package path is unchanged by this ordinary-only guard.
  value = await run('lp-fixture', {related_file_paths: []});
  assert.equal(value.calls.length, 1);
  checks.push('large_package_retains_fallback');
  process.stdout.write(JSON.stringify({passed: true, check_count: checks.length, checks}) + '\n');
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
