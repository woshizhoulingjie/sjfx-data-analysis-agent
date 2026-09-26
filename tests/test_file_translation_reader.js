/* Run with: node --test tests/test_file_translation_reader.js */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const reader = require('../static/translation-reader.js');
function sourceApi(texts, change) {
  const calls = [];
  return { calls, api: async (url) => {
    const query = new URL(url, 'http://localhost').searchParams;
    const offset = Number(query.get('offset')); const limit = Number(query.get('limit'));
    calls.push(offset);
    const result = { path: query.get('path'), status: 'completed', full_translation: true, provider_id: 'vllm:test', progress: { required_units: 3, completed_units: 3 } };
    for (const key of ['original', 'translated', 'working']) {
      const points = texts[key] == null ? null : Array.from(texts[key]);
      result[key] = points == null ? null : { text: points.slice(offset, offset + limit).join(''), offset, limit, total_characters: points.length, has_more: offset + limit < points.length };
    }
    return change ? change(result, offset) : result;
  }};
}
const read = (api, extra = {}) => reader.readComplete({ scanId: 'unit-package', path: 'report.pdf', api, ...extra });
test('loads every page without truncating unequal bilingual documents', async () => {
  reader.reset();
  const texts = { original: 'English source. '.repeat(5200) + 'SOURCE END', translated: '完整译文。'.repeat(15000) + '译文末尾', working: null };
  const source = sourceApi(texts); const page = await read(source.api);
  assert.equal(page.original.text, texts.original); assert.equal(page.translated.text, texts.translated);
  assert.deepEqual(source.calls, [0, 20000, 40000, 60000, 80000]); assert.equal(page.reader_complete, true);
});
test('uses server code-point offsets even for emoji and supplementary characters', async () => {
  reader.reset(); const text = '😀𠮷中'.repeat(20001);
  const page = await read(sourceApi({ original: text, translated: text }).api);
  assert.equal(page.original.text, text); assert.equal(page.translated.text, text);
});
test('loads all working text when final translation is unavailable', async () => {
  reader.reset(); const working = '译文与未译原文'.repeat(9000);
  const page = await read(sourceApi({ original: 'source', working }, (p) => ({ ...p, status: 'partial', full_translation: false })).api);
  assert.equal(page.working.text, working); assert.equal(reader.completeTranslation(page), false);
});
test('rejects a missing page instead of presenting it as full text', async () => {
  reader.reset(); const source = sourceApi({ translated: '中'.repeat(40001) }, (p, offset) => { if (offset) p.translated.text = ''; return p; });
  await assert.rejects(read(source.api), /分页不完整/);
});
test('rejects changing progress while retrieving a document', async () => {
  reader.reset(); const source = sourceApi({ original: 'a'.repeat(40001) }, (p, offset) => { if (offset) p.progress.completed_units = 4; return p; });
  await assert.rejects(read(source.api), /译文正在更新/);
});
test('rejects changed totals and wrong returned paths', async () => {
  reader.reset(); const source = sourceApi({ original: 'a'.repeat(40001) }, (p, offset) => { if (offset) p.original.total_characters += 1; return p; });
  await assert.rejects(read(source.api), /正文在读取期间/);
  await assert.rejects(read(sourceApi({}, (p) => ({ ...p, path: 'another.pdf' })).api), /所选文件不一致/);
});
test('stale requests stop and cannot replace the newly selected file', async () => {
  reader.reset(); let active = true; const source = sourceApi({ original: 'a'.repeat(60001) });
  await assert.rejects(read(source.api, { isCurrent: () => active, onProgress: () => { active = false; } }), { name: 'AbortError' });
  assert.equal(source.calls.length, 1);
});
test('finished documents reuse cached full text but recheck the first page', async () => {
  reader.reset(); const source = sourceApi({ translated: '中'.repeat(40001) });
  const first = await read(source.api); const second = await read(source.api);
  assert.equal(second, first); assert.deepEqual(source.calls, [0, 20000, 40000, 0]);
});
test('empty and untranslated documents are honest about readiness', async () => {
  reader.reset(); const page = await read(sourceApi({ original: '', translated: null }).api);
  assert.equal(page.original.text, '');
  assert.equal(reader.completeTranslation({ status: 'partial', full_translation: false }), false);
  assert.equal(reader.completeTranslation({ status: 'completed', full_translation: true, warnings: ['source_preserved_after_quality_review'] }), false);
  assert.equal(reader.completeTranslation({ status: 'not_required', full_translation: true, original: { text: '中文原文' } }), true);
  assert.equal(reader.completeTranslation({ status: 'completed', full_translation: true }), false);
});
test('safe formatting preserves end text and does not execute HTML', () => {
  const html = reader.prose('# 标题\n\n正文 <script>alert(1)</script>\n\n|项目|数值|\n|---|---|\n|收入|100|\n\n- 第一项\n- 第二项\n\n最后一段');
  assert.match(html, /<h2>标题<\/h2>/); assert.match(html, /<table>/);
  assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>/);
  assert.match(html, /最后一段<\/p>$/); assert.match(html, /- 第一项\n- 第二项/);
});
