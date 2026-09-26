const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const view = require('../static/intelligence-overview.js');
test('unknown counts are not silently zero', () => {
  const vm = view.model('large', {}, {});
  assert.ok(vm.metrics.every(item => item[1] === null));
  assert.match(view.render(vm), /—/);
  assert.doesNotMatch(view.render(vm), /aria-valuenow/);
});
test('deep coverage is not quick coverage', () => {
  const vm = view.model('large',{coverage:{inventory_files:100,quick_completed:95,deep_completed:20,model_summaries:15}}, {available:true,preliminary:true});
  assert.ok(view.render(vm).includes('aria-valuenow="20.0"'));
  assert.match(view.render(vm), /初步简报/);
  assert.match(view.render(vm), /快速摘要不等于全文核验/);
});
test('source content is escaped in text and attributes', () => {
  const attack = '<img src=x onerror=alert(1)>"';
  const html = view.render(view.model('ordinary',{package:{root:attack},topics:{items:[{topic:attack,file_count:3}]}}, {title:attack,basic_information:[attack],key_findings:[attack],limitations:[attack],recommended_research_direction:{title:attack,rationale:attack,priority:attack,research_questions:[attack],representative_documents:[attack]}}));
  assert.doesNotMatch(html, /<img|<script/);
  assert.match(html, /&lt;img/);
});
test('ordinary wrapped collections and ratios are supported', () => {
  const vm=view.model('ordinary',{package:{file_count:0},topics:{items:[{topic:'测试',file_count:0}]}}, {coverage:{content_parse_ratio:0.5}});
  assert.equal(vm.categories[0].label,'测试');
  assert.equal(vm.metrics[0][1],0);
  assert.match(view.render(vm), /50%/);
});
test('invalid counts never become success indicators', () => {
  const vm=view.model('large',{coverage:{inventory_files:-1,quick_completed:'bad',deep_completed:true}},{});
  assert.ok(vm.metrics.every(row=>row[1]===null));
});
test('recommended direction shows only actual support', () => {
  const brief={available:true,recommended_research_direction:{title:'供应链韧性',rationale:'跨期比较',research_questions:['断点在哪里？'],representative_documents:['证据/报告.pdf'],evidence_chain:[{text:'证据'}]}};
  const html=view.render(view.model('ordinary',{},brief));
  assert.match(html,/推荐研究方向/); assert.match(html,/为什么值得研究/);
  assert.match(html,/1 份代表资料 · 1 条支撑原文/);
  assert.ok(html.includes('data-brief-file="证据/报告.pdf"'));
  assert.doesNotMatch(html,/简报置信度/);
});
test('loads before both consumers and clears stale package content', () => {
  const read=p=>fs.readFileSync(path.join(__dirname,'..',p),'utf8');
  const html=read('templates/index.html');
  assert.ok(html.indexOf('/static/intelligence-overview.js') < html.indexOf('/static/large-package-result.js'));
  assert.ok(html.indexOf('/static/intelligence-overview.js') < html.indexOf('/static/engineering-v2.js'));
  assert.ok(read('static/engineering-v2.js').includes("clear('packageIntelligenceBoard')"));
  assert.ok(read('static/large-package-result.js').includes("clear('largeIntelligenceBoard')"));
});
