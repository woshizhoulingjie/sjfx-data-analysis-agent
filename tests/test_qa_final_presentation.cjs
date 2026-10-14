/* Render the actual QA client against saved final turns and live drafts. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const filename = path.join(__dirname, '../static/engineering-v2.js');
const storage = { getItem: () => null, setItem() {}, removeItem() {} };
const window = { localStorage: storage, sessionStorage: storage };
const context = vm.createContext({ window, document: { addEventListener() {} },
  sessionStorage: storage, Map, Set, Date, Math });
const source = fs.readFileSync(filename, 'utf8').replace(
  "document.addEventListener('DOMContentLoaded', bind);",
  'window.__qa = { state, messageMarkup, registerAnalysisTurn };');
vm.runInContext(source, context);
const { state, messageMarkup, registerAnalysisTurn } = window.__qa;
const checks = [];

function savedTurn(overrides = {}) {
  return {
    turn_id: 'turn-1', scan_id: 'lp-package', status: 'completed', stage: 'completed',
    task_status: 'partially_fulfilled', evidence_status: 'partial', intent: { name: 'summary' },
    quality_metrics: { verification_status: 'partial', scope_files: 1040, inspected_files: 1032,
      citation_count: 68, evidence_support_ratio: 0.8, unparsed_files: 8 },
    warnings: ['8 份资料暂无可用内容', '待核对 <source>'],
    input_manifest: { large_qa: { global_mode: true, synthesis_complete: false,
      scope_files: 1040, scanned_files: 1040, model_mapped_files: 1032,
      map_processed_batches: 6, map_batches: 6, files_without_content: 8 },
      final_synthesis_complete: false, selected_files: 20, selected_evidence: 40,
      selected_characters: 14000, final_synthesis_notes: 33, total_notes: 33 },
    citations: [{ citation_label: '[1]', source_path: 'report.txt', original_text: '原文内容' }],
    related_file_paths: ['report.txt'], file_results_total: 1,
    file_results: [{ path: 'report.txt', extension: '.txt' }],
    steps: [{ action: '读取资料', status: 'completed' }],
    ...overrides,
  };
}

function render(turn, content = '完整成稿，覆盖现有资料的主要主题。[1]') {
  state.turns.clear();
  state.scanId = 'ordinary-package';
  state.chatSourceId = '';
  state.chatSourceKind = 'standard';
  state.conversation = { scan_id: 'ordinary-package', messages: [] };
  state.turns.set('assistant-1', turn);
  return messageMarkup({ message_id: 'assistant-1', role: 'assistant', content });
}

function details(html, className) {
  const pattern = new RegExp(`<details\\b([^>]*class="[^"]*\\b${className}\\b[^"]*"[^>]*)>([\\s\\S]*?)<\\/details>`);
  const matched = html.match(pattern);
  assert.ok(matched, `${className} should be a native disclosure`);
  assert.doesNotMatch(matched[1], /\bopen(?:\s|=|$)/, `${className} must start collapsed`);
  return matched[2];
}

// Captured from the production renderer on 2026-10-12 for savedTurn().
const PRODUCTION_FINAL_APPENDIX = "<section class=\"v2-quality\" aria-label=\"分析质量\"><header><strong>分析质量</strong><span>任务部分完成 · 引用部分通过</span></header><div><dl><dt>范围文件</dt><dd>1,040</dd></dl><dl><dt>候选文件</dt><dd>0</dd></dl><dl><dt>实际检查</dt><dd>1,032</dd></dl><dl><dt>未检查</dt><dd>8</dd></dl><dl><dt>深析完成</dt><dd>0</dd></dl><dl><dt>分析批次</dt><dd>0</dd></dl><dl><dt>引用</dt><dd>68</dd></dl><dl><dt>结论支持率</dt><dd>80%</dd></dl><dl><dt>无证据结论</dt><dd>0</dd></dl><dl><dt>反证</dt><dd>0</dd></dl><dl><dt>矛盾</dt><dd>0</dd></dl><dl><dt>未解析文件</dt><dd>8</dd></dl></div></section><section class=\"v2-turn-warnings\"><strong>范围与核验限制</strong><ul><li>8 份资料暂无可用内容</li><li>待核对 &lt;source&gt;</li></ul></section><details class=\"v2-input-manifest\"><summary>查看本次实际分析范围</summary><div><strong>当前为阶段性综合，尚未形成完整全范围结论</strong><br>程序扫描 1,040/1,040 份文件；模型归并 1,032/1,040 份文件，已完成 6/6 批。<br>回答引用 20 份文件的 40 个内容片段，共约 14,000 字；引用数量不代表扫描范围。 本轮保留 33/33 条综合要点。<br>扫描依据已有摘要及正文索引，不等于逐份全文阅读。<br>资料缺口：暂无可用内容 8 份。</div></details><details class=\"v2-analysis-steps\"><summary>查看分析步骤</summary><div class=\"v2-analysis-step completed\"><span>读取资料</span><b>分析完成</b></div></details><details class=\"v2-citations\"><summary>证据轨迹 · 检索到 1 条证据</summary><article class=\"v2-citation\"><strong>[1] report.txt</strong><small>原文片段 · 直接证据</small><p><b>原文：</b>原文内容</p><div class=\"v2-citation-actions\"><button type=\"button\" class=\"text-button\" data-citation-read=\"report.txt\">阅读原文</button><button type=\"button\" class=\"text-button\" data-citation-collect=\"report.txt\">收入资料夹</button><button type=\"button\" class=\"text-button\" data-citation-node=\"report.txt\">归入节点</button><button type=\"button\" class=\"text-button\" data-citation-translation=\"report.txt\" data-translation-package=\"ordinary-package\">查看译文</button></div></article></details><details class=\"v2-file-results-fold\"><summary>相关文件 · 1 个范围文件（点击展开／收起）</summary><section class=\"v2-file-results\"><header><div><strong>相关文件</strong><span>1 个范围文件</span></div><div class=\"v2-file-bulk-actions\"><span class=\"v2-file-selection-status\" data-file-selection-count>未选择文件</span><button type=\"button\" data-select-all-files>全选</button><button type=\"button\" data-create-node-selected disabled>选中后创建节点</button><button type=\"button\" data-export-file-folder>导出资料夹</button></div></header><article class=\"v2-file-card\"><label class=\"v2-file-select\"><input type=\"checkbox\" data-file-select=\"report.txt\"><span class=\"sr-only\">选择 report.txt</span></label><div><strong title=\"report.txt\">report.txt</strong><small>report.txt · .txt</small></div><div class=\"v2-file-card-actions\"><button type=\"button\" data-file-open=\"report.txt\">阅读原文</button><button type=\"button\" data-file-deep-summary=\"report.txt\">深度摘要</button><button type=\"button\" data-file-collect=\"report.txt\">加入资料夹</button><button type=\"button\" data-file-node=\"report.txt\">归入新节点</button></div></article></section></details>";

const final = savedTurn();
const finalHtml = render(final);
const meta = finalHtml.match(/<div class="v2-message-meta">([\s\S]*?)<\/div>/)[1];
assert.equal(meta, '<span>资料问答 · 已回答</span>');
assert.doesNotMatch(meta, /任务部分完成|partially_fulfilled|证据：partial/);
assert.match(finalHtml, /完整成稿，覆盖现有资料的主要主题/);
const appendix = finalHtml.slice(finalHtml.indexOf('<section class="v2-quality"'),
  finalHtml.indexOf('<div class="v2-message-actions">'));
assert.equal(appendix, PRODUCTION_FINAL_APPENDIX,
  'all final quality, warnings, scope, steps, evidence and related-file DOM must match production');
assert.match(finalHtml, /<section class="v2-quality"/);
assert.match(finalHtml, /<section class="v2-turn-warnings"/);
assert.match(finalHtml, /待核对 &lt;source&gt;/);
assert.doesNotMatch(finalHtml, /v2-quality-fold/);
assert.match(finalHtml, /<summary>查看本次实际分析范围<\/summary>/);
assert.match(finalHtml, /当前为阶段性综合，尚未形成完整全范围结论/);
assert.match(details(finalHtml, 'v2-citations'), /原文内容/);
assert.match(finalHtml, /暂无可用内容 8 份/);
assert.match(finalHtml, /不等于逐份全文阅读/);
assert.match(finalHtml, /v2-citations|report.txt/);
assert.equal(final.task_status, 'partially_fulfilled', 'presentation must preserve internal truth');
checks.push('final large QA changes only its label and preserves every production appendix byte');

const nonGlobalHtml = render(savedTurn({ scan_id: '', input_manifest: {
  large_qa: { global_mode: false, generation: 2 }, selected_files: 2, selected_evidence: 3 } }));
assert.match(nonGlobalHtml, /资料问答 · 已回答/);
assert.match(nonGlobalHtml, /<section class="v2-quality"/);
assert.doesNotMatch(nonGlobalHtml, /v2-quality-fold/);
checks.push('non-global large QA metadata is recognized without a package ID');

const byIdHtml = render(savedTurn({ input_manifest: undefined }));
assert.match(byIdHtml, /资料问答 · 已回答/);
assert.match(byIdHtml, /<section class="v2-quality"/);
assert.doesNotMatch(byIdHtml, /v2-quality-fold/);
checks.push('saved large-package turns remain recognizable without a generation manifest');

for (const status of ['queued', 'running', 'waiting_for_deep_analysis']) {
  const html = render(savedTurn({ status, stage: 'repairing', progress: 88,
    progress_message: '正在核对资料，已完成 6/6 批' }), 'DRAFT_BODY_SHOULD_NOT_APPEAR');
  assert.match(html, /正在核对资料，已完成 6\/6 批/);
  assert.match(html, /88%/);
  assert.match(html, /data-cancel-turn="turn-1"/);
  assert.doesNotMatch(html, /DRAFT_BODY|v2-message-content|v2-message-meta|v2-quality|v2-turn-warnings|v2-input-manifest|v2-citations|v2-file-results|v2-analysis-steps/);
  assert.doesNotMatch(html, /report.txt|8 份资料|复制|再次回答/);
  checks.push(`${status} large QA shows progress without draft body or result attachments`);
}

state.turns.clear();
registerAnalysisTurn({ id: 'turn-early', scan_id: 'lp-early', assistant_message_id: 'assistant-early',
  status: 'running', stage: 'retrieving', progress: 24, message: '正在整个范围检索资料' });
assert.equal(state.turns.get('assistant-early').scan_id, 'lp-early');
const early = messageMarkup({ message_id: 'assistant-early', role: 'assistant', content: 'EARLY_PLACEHOLDER' });
assert.match(early, /正在整个范围检索资料/);
assert.doesNotMatch(early, /EARLY_PLACEHOLDER|v2-message-content|v2-message-meta/);
checks.push('early live turns use their durable scan ID before any draft manifest exists');

const ordinary = render(savedTurn({ scan_id: 'ordinary-package', input_manifest: { large_qa: {} } }));
assert.match(ordinary, /任务部分完成/);
assert.match(ordinary, /证据：partial/);
assert.match(ordinary, /<section class="v2-quality"/);
assert.match(ordinary, /<section class="v2-turn-warnings"/);
assert.doesNotMatch(ordinary, /v2-quality-fold|资料问答 · 已回答/);
checks.push('ordinary analysis keeps its existing metadata and expanded quality/caveat panels');

for (const status of ['running', 'completed']) {
  const html = render(savedTurn({ status, intent: { name: 'file_search' }, file_search: true,
    progress: 35, stage: 'retrieving' }), '搜索到 report.txt');
  assert.match(html, /搜索到 report.txt/);
  assert.match(html, /文件搜索/);
  assert.match(html, /v2-file-results/);
  assert.doesNotMatch(html, /v2-quality|v2-turn-warnings|v2-input-manifest|资料问答 · 已回答/);
  checks.push(`${status} large-package file search keeps its dedicated presentation`);
}

const searchPlan = render(savedTurn({ status: 'running', intent: undefined, file_search: undefined,
  plan: { modes: ['file_search'] }, input_manifest: undefined, quality_metrics: undefined,
  warnings: [] }), '搜索文件进度');
assert.match(searchPlan, /搜索文件进度/);
assert.match(searchPlan, /v2-message-content/);
assert.doesNotMatch(searchPlan, /v2-quality|v2-turn-warnings|资料问答 · 已回答/);
checks.push('file-search plans are preserved before the engine has returned an intent');

for (const intent of ['casual', 'creative', 'general_qa']) {
  const html = render(savedTurn({ intent: { name: intent }, input_manifest: undefined }), '正常对话内容');
  assert.match(html, /正常对话内容/);
  assert.doesNotMatch(html, /资料问答 · 已回答|v2-quality-fold/);
  checks.push(`${intent} keeps its own presentation even inside a large-package session`);
}

for (const status of ['failed', 'cancelled']) {
  const html = render(savedTurn({ status }), status === 'failed' ? '运行失败，请重试' : '本轮已取消');
  assert.match(html, /运行失败，请重试|本轮已取消/);
  assert.match(html, /data-retry-turn/);
  assert.doesNotMatch(html, /v2-turn-progress|资料问答 · 已回答/);
  checks.push(`${status} turns keep their terminal message and retry control`);
}

const complete = render(savedTurn({ task_status: 'fulfilled', evidence_status: 'supported',
  input_manifest: { large_qa: { global_mode: true, synthesis_complete: true },
    final_synthesis_complete: true }, warnings: [] }));
assert.match(complete, /资料问答 · 已回答/);
assert.match(complete, /全范围综合已完成/);
assert.doesNotMatch(complete, /v2-turn-warnings|阶段性综合/);
checks.push('complete final answers retain their truthful full-synthesis disclosure');

const originalOnly = render(savedTurn({ package_reading: {
  files_read: 2, summary_backed_files: 1, files_without_summary: 1,
  summary_coverage: 0.5, complete: true, summary_complete: false,
  available_content_complete: true,
} }));
assert.match(originalOnly, /摘要待补全/);
assert.match(originalOnly, /已纳入 1\/2 份可用摘要/);
assert.doesNotMatch(originalOnly, /摘要已齐备/);
assert.match(originalOnly, /资料摘要覆盖范围/);
checks.push('complete indexed-content scans do not falsely mark original-only sources as summarized');

const template = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
assert.match(template, /engineering-v2\.js\?v=41&large-chat=1&translation-reader=20260926-1&whole-corpus=20261012-3/);
assert.match(template, /intelligence-overview\.css\?v=20261012-autochinese-1/);
checks.push('template updates the QA cache version while preserving concurrent production cachebusters');

process.stdout.write(JSON.stringify({ passed: checks.length, checks }, null, 2) + '\n');
