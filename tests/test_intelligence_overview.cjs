const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const view = require('../static/intelligence-overview.js');
const vm = require('node:vm');

function chinesePresentation() {
  return {direction:{candidate_id:'chosen',title:'低质量输入的适用边界验证',priority:'高',confidence:'中',
    rationale:'不应重复的理由',selection_reason:'不应重复的取舍',known_work:'不应重复的已有工作',research_gap:'不应重复的缺口',
    research_questions:['噪声会如何改变结果？','评价口径是否一致？'],methods:['核对条件 → 条件对照表','确认资源 → 资源清单'],limitations:['同口径对照尚未补齐']},
    reasons:['已有依据','待解决问题与研究价值','为什么优先推荐','怎样开始验证','推荐的前提与限制'].map((label,index)=>({label,text:`中文推荐依据第${index+1}段。`,evidence_numbers:[1]})),
    key_findings:['材料描述了清晰输入下的结果。'],limitations:['当前只解析部分资料。'],
    alternatives:[{title:'补全评价对照',research_gap:'评价条件尚待核对。',selection_reason:'先确认比较口径。',evidence_numbers:[1]}],
    evidence_appendix:[{number:1,evidence_id:'evidence-RAW-1',source_path:'parsed/temp.txt',archive_source_path:'papers/English Source.pdf',page:7,section:'Failure conditions',start_line:0,end_line:4,roles:['背景支撑','反证线索'],text:'The result failed.\n' + 'Complete original quote. '.repeat(80)}],
    source_documents:['papers/English Source.pdf'],source_notes:[{number:1,text:'Original authored research plan.'}]};
}

test('Chinese presentation owns ordinary prose and keeps full originals only in the closed appendix', () => {
  const presentation = chinesePresentation();
  const brief = {title:'English report title',basic_information:['RAW basics'],key_findings:['RAW finding'],limitations:['RAW limits'],
    recommended_research_direction:{title:'RAW direction',rationale:'RAW recommendation',methods:['RAW method']},overview_presentation:presentation};
  const reading = view.model('ordinary',{package:{root:'C:/English Files'}},brief);
  assert.equal(reading.direction,presentation.direction);
  assert.deepEqual(reading.basics,[]);
  assert.deepEqual(reading.findings,presentation.key_findings);
  const html = view.render(reading);
  const appendixAt = html.indexOf('<details id="packageOverviewEvidenceAppendix"');
  const body = html.slice(0,appendixAt);
  assert.ok(appendixAt > 0);
  assert.doesNotMatch(body,/RAW direction|RAW recommendation|RAW method|English report title|English Files|English Source|evidence-RAW/);
  assert.doesNotMatch(html,/RAW basics|RAW finding|RAW limits|不应重复的/);
  assert.equal((html.match(/<section><h5>/g)||[]).length,5);
  assert.equal((html.match(/噪声会如何改变结果？/g)||[]).length,1,'core question must not repeat in plan');
  assert.equal((html.match(/核对条件 → 条件对照表/g)||[]).length,1);
  assert.match(html,/<details id="packageOverviewEvidenceAppendix"[^>]*data-io-disclosure="evidence">/);
  assert.match(html,/<details data-io-disclosure="plan">/);
  for (const original of ['English Source.pdf','evidence-RAW-1','Failure conditions','parsed/temp.txt','Original authored research plan.',presentation.evidence_appendix[0].text]) assert.ok(html.slice(appendixAt).includes(original),original);
  assert.ok(html.includes('第 7 页'));
  assert.ok(html.includes('起始行</dt><dd>0'));
  assert.ok(html.includes('href="#packageOverviewEvidence-1"'));
  assert.doesNotMatch(html,/背景支撑/,'an explicit counterexample must not be labeled as supporting the recommendation');
  assert.match(html,/<details data-io-disclosure="research-alternatives"><summary>候选研究方向与取舍 · 1/);
  assert.match(html,/资料情报概览/);
});

test('projection escapes reason references, full source metadata and original notes', () => {
  const presentation = chinesePresentation();
  const attack = '\"><svg onload=alert(1)>';
  presentation.reasons[0] = {label:attack,text:attack,evidence_numbers:['1',attack,1]};
  Object.assign(presentation.evidence_appendix[0],{evidence_id:attack,archive_source_path:attack,section:attack,text:attack});
  presentation.source_documents=[attack];
  presentation.source_notes=[{text:attack}];
  const html = view.render(view.model('ordinary',{}, {overview_presentation:presentation}));
  assert.doesNotMatch(html,/<svg|href="[^"]*<|data-brief-file=""><|\[object Object\]/);
  assert.ok(html.includes('&lt;svg'));
  assert.equal((html.match(/data-io-evidence-number="1"/g)||[]).length,6,'valid numeric references are deduplicated');
});

test('malformed presentation falls back to legacy and large packages ignore projections', () => {
  const legacy = {recommended_research_direction:{title:'既有方向',rationale:'既有理由'}};
  for (const projection of [[],{direction:[]},{direction:'invalid'},null]) assert.equal(view.model('ordinary',{}, {...legacy,overview_presentation:projection}).presentation,null);
  const large = view.model('large',{}, {...legacy,overview_presentation:chinesePresentation()});
  assert.equal(large.presentation,null);
  assert.equal(large.direction.title,'既有方向');
});

test('secondary renderer points to the shared appendix and does not duplicate raw or Chinese planning', () => {
  const source = fs.readFileSync(path.join(__dirname,'../static/engineering-v2.js'),'utf8');
  const renderer = source.slice(source.indexOf('  function renderResearchBrief('),source.indexOf('  function renderSelectedScope('));
  const grid = {hidden:false};
  const nodes = Object.fromEntries(['packageOverviewBriefTitle','packageOverviewBriefSummary','packageOverviewFindings','packageOverviewDirection','packageOverviewResearchDetails','packageOverviewReportBtn'].map(id=>[id,{innerHTML:'stale',dataset:{},closest:()=>grid}]));
  let writes=0,details='stale';
  Object.defineProperty(nodes.packageOverviewResearchDetails,'innerHTML',{get:()=>details,set:value=>{details=value;writes++;}});
  const context = vm.createContext({window:{SJFXIntelligenceOverview:view},$:id=>nodes[id],listOf:value=>Array.isArray(value)?value:[]});
  vm.runInContext(renderer,context);
  const brief = {title:'English report',overview_presentation:chinesePresentation(),recommended_research_direction:{title:'RAW title',rationale:'RAW evidence'}};
  context.renderResearchBrief(brief,{filename:'overview.docx'});
  assert.equal(grid.hidden,true);
  assert.equal(nodes.packageOverviewBriefTitle.textContent,'研究简报与导出');
  for (const id of ['packageOverviewBriefSummary','packageOverviewFindings','packageOverviewDirection']) assert.equal(nodes[id].innerHTML,'');
  assert.ok(details.includes('href="#packageOverviewEvidenceAppendix"'));
  assert.doesNotMatch(details,/RAW|English|中文推荐依据|低质量输入/);
  assert.equal(nodes.packageOverviewReportBtn.disabled,false);
  assert.equal(nodes.packageOverviewReportBtn.dataset.filename,'overview.docx');
  context.renderResearchBrief(brief,{filename:'overview.docx'});
  assert.equal(writes,1,'unchanged polling retains the same details-link node');
});
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
  assert.match(html,/1 份代表资料 · 1 条依据原文/);
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

test('ordinary proposal distinguishes prior work, unresolved gap and hypothesis', () => {
  const html = view.render(view.model('ordinary', {}, {available:true,summary_stage:'preliminary',recommended_research_direction:{
    title:'低质量图像下的边界验证', rationale:'材料仅评价清晰输入', selection_reason:'先复核边界，比迁移新方法更可执行',
    known_work:'已测清晰图像', research_gap:'噪声条件未核验', hypothesis:'低质量条件可能影响可靠性',
    research_questions:['噪声会如何影响结果？'], methods:['核对原方法与噪声分组 → 条件对照表'], quality_gate:{resources:'confirmation_first'}
  }}));
  for (const text of ['基于当前已处理资料的首选建议','为什么优先这个','资料已经做到什么','仍待核验的问题','待验证假设','现有资料尚不能确认该假设成立','研究动作','预期产出','条件对照表','第一步先确认数据、代码与资源条件']) assert.ok(html.includes(text), text);
  assert.ok(html.indexOf('已测清晰图像') < html.indexOf('低质量条件可能影响可靠性'));
  assert.equal((html.match(/class="io-core-question"/g) || []).length, 1);
  assert.ok(html.includes('data-io-disclosure="plan" open'));
});

test('source roles and counterevidence retain archive path, page and body', () => {
  const path = '资料/论文A.pdf';
  const direction = {title:'边界复核', representative_documents:[path], evidence_roles:{prior_work:['e1'],counterevidence:['e1']},
    evidence_chain:[{evidence_id:'e1', archive_source_path:path, source_path:'临时解析名.txt', page:7, text:'条件改变时没有改善。'}],
    counterevidence:[{evidence_id:'e1', archive_source_path:path, source_path:'临时解析名.txt', page:7, text:'条件改变时没有改善。'}], limitations:['缺少同口径对照']};
  const html = view.render(view.model('ordinary', {}, {recommended_research_direction:direction}));
  assert.ok(html.includes('已有工作 · 反证线索'));
  assert.ok(html.includes('资料/论文A.pdf · 第 7 页'));
  assert.ok(html.includes('data-brief-file="资料/论文A.pdf"'));
  assert.ok(html.includes('条件改变时没有改善。'));
  assert.ok(html.includes('缺少同口径对照'));
  assert.doesNotMatch(html, /临时解析名\.txt/);
});

test('counterevidence IDs suppress support labels in quotes and representative files', () => {
  const direction = {title:'边界复核',representative_documents:['反证.md'],
    evidence_roles:{support:['same'],prior_work:['same'],conditions:['same'],counterevidence:['same']},
    evidence_chain:[{evidence_id:'same',source_path:'反证.md',text:'该方法没有改善结果。'}],
    counterevidence:[{evidence_id:'same',source_path:'反证.md',text:'该方法没有改善结果。'}]};
  const html = view.render(view.model('ordinary',{}, {recommended_research_direction:direction}));
  assert.doesNotMatch(html,/背景支撑/);
  assert.ok(html.includes('已有工作 · 适用条件 · 反证线索'));
  const button = html.match(/<button[^>]*title="反证\.md"[\s\S]*?<\/button>/)[0];
  assert.ok(button.includes('已有工作 · 适用条件 · 反证线索'));
});

test('file role aggregation applies counter suppression per evidence ID', () => {
  const direction = {title:'条件复核',representative_documents:['混合证据.md','独立反证.md'],
    evidence_roles:{support:[1,2,3],conditions:[2,3],counterevidence:[2]},
    evidence_chain:[{evidence_id:1,source_path:'混合证据.md',text:'背景条件已描述。'},
      {evidence_id:2,source_path:'混合证据.md',text:'条件改变后没有改善。'},
      {evidence_id:3,source_path:'独立反证.md',text:'另一条件下方法失败。'}],
    counterevidence:[{evidence_id:'3',source_path:'独立反证.md',text:'另一条件下方法失败。'}]};
  const html = view.render(view.model('ordinary',{}, {recommended_research_direction:direction}));
  const mixed = html.match(/<button[^>]*title="混合证据\.md"[\s\S]*?<\/button>/)[0];
  assert.ok(mixed.includes('背景支撑 · 适用条件 · 反证线索'),'a different, non-counter background unit keeps its own role');
  const independent = html.match(/<button[^>]*title="独立反证\.md"[\s\S]*?<\/button>/)[0];
  assert.doesNotMatch(independent,/背景支撑/);
  assert.ok(independent.includes('适用条件'));
  const counterArticle = html.match(/<article>[\s\S]*?<p>条件改变后没有改善。<\/p><\/article>/)[0];
  const quote = counterArticle.slice(counterArticle.lastIndexOf('<article>'));
  assert.ok(quote.includes('适用条件 · 反证线索'));
  assert.doesNotMatch(quote,/背景支撑/);
});

test('missing evidence and resources are not presented as confirmed', () => {
  const html = view.render(view.model('ordinary', {}, {recommended_research_direction:{title:'范围待核验',opportunity_kind:'__proto__',
    evidence_chain:[{page:-4}],counterevidence:[{page:true}]}}));
  assert.ok(html.includes('来源待核验'));
  assert.ok(html.includes('该条未提供可展示原文'));
  assert.doesNotMatch(html, /第 -4 页|第 true 页|第一步先确认|\[object Object\]|质量闸门已通过/);
  const empty = view.render(view.model('ordinary', {}, {}));
  assert.ok(empty.includes('当前未提供可展示的反证线索，不表示不存在反例'));
  assert.ok(empty.includes('当前未列出额外适用边界'));
});

test('ordinary additions escape every new content and source attribute', () => {
  const attack = '"><svg onload=alert(1)>';
  const candidate = {candidate_id:'c1',title:attack,selection_reason:attack,known_work:attack,research_gap:attack,hypothesis:attack,
    evidence_roles:{counterevidence:['x']}, evidence_chain:[{evidence_id:'x',archive_source_path:attack,text:attack}],
    counterevidence:[{evidence_id:'x',archive_source_path:attack,text:attack}],limitations:[attack],methods:[`${attack} → ${attack}`]};
  const html = view.render(view.model('ordinary', {}, {recommended_research_direction:candidate,direction_candidates:[candidate,{candidate_id:'c2',title:attack,selection_reason:attack}]}));
  assert.doesNotMatch(html, /<svg|<script|data-brief-file=""><|title=""></);
  assert.ok(html.includes('&lt;svg'));
  assert.ok(html.includes('候选研究方向与取舍 · 1'));
});

test('candidate IDs isolate the selected opportunity without dropping legacy candidates', () => {
  const candidates = [{candidate_id:'first',title:'首选'},{candidate_id:'second',title:'备选'},{title:'旧格式候选'}];
  assert.deepEqual(view.model('ordinary', {}, {recommended_research_direction:{candidate_id:'first'},direction_candidates:candidates}).alternatives, candidates.slice(1));
  assert.deepEqual(view.model('ordinary', {}, {direction_candidates:candidates}).alternatives, candidates);
});

test('large package output is unaffected by ordinary opportunity fields', () => {
  const source = {coverage:{inventory_files:12,deep_completed:4}};
  const base = {title:'既有大包方向',rationale:'原有建议',methods:['原有方法'],representative_documents:['A.pdf']};
  const additions = {selection_reason:'普通取舍',known_work:'普通已有工作',research_gap:'普通缺口',hypothesis:'普通假设',
    counterevidence:[{text:'普通反证'}],limitations:['普通限制'],quality_gate:{resources:'confirmation_first'},opportunity_kind:'boundary'};
  assert.equal(view.render(view.model('large',source,{recommended_research_direction:{...base,...additions},direction_candidates:[{title:'普通备选'}]})), view.render(view.model('large',source,{recommended_research_direction:base})));
});

test('ordinary opportunity processing distinguishes discovery from bounded refinement', () => {
  const audit = {coverage:'all_available_topics_scanned',discovered_count:20,refined_count:8,refinement_budget:8,
    deferred:[{topic:'低质量输入',opportunity_kind:'boundary',reason:'bounded_refinement_budget'}],refinement_complete:false};
  const html = view.render(view.model('ordinary', {}, {research_opportunity_audit:audit}));
  assert.ok(html.includes('研究机会处理记录 · 精查 8 / 20'));
  for (const text of ['已对当前可用主题进行轻量盘点','仅在已精查并通过证据审查的候选中比较','精查未完成','五种证据角色来自一次有界召回后的分桶','低质量输入 · 适用边界验证','本轮精查预算有限']) assert.ok(html.includes(text),text);
  assert.ok(html.includes('已发现机会</dt><dd>20'));
  assert.ok(html.includes('已精查候选</dt><dd>8'));
  assert.ok(html.includes('本轮精查上限</dt><dd>8'));
  assert.ok(html.includes('延期精查</dt><dd>1'));
  assert.ok(html.includes('<details data-io-disclosure="research-audit"><summary>'));
  assert.ok(html.indexOf('研究机会处理记录') > html.indexOf('推荐研究方向'));
  assert.equal(view.model('large', {}, {research_opportunity_audit:audit}).researchAudit,null);
  assert.doesNotMatch(view.render(view.model('large', {}, {research_opportunity_audit:audit})), /研究机会处理记录|精查状态/);
  assert.doesNotMatch(view.render(view.model('ordinary', {}, {})), /研究机会处理记录/);
});

test('audit completion stays local to discovered opportunities and missing counters stay unknown', () => {
  const complete = view.render(view.model('ordinary', {}, {research_opportunity_audit:{discovered_count:0,refined_count:0,refinement_budget:8,deferred:[],refinement_complete:true}}));
  assert.ok(complete.includes('已精查全部已发现机会'));
  assert.ok(complete.includes('不表示全部资料或所有研究问题均已核验'));
  assert.ok(complete.includes('延期精查</dt><dd>0'));
  const unknown = view.render(view.model('ordinary', {}, {research_opportunity_audit:{discovered_count:[],refined_count:true,refinement_budget:' ',deferred:'not-a-list',refinement_complete:'true'}}));
  for (const label of ['已发现机会','已精查候选','本轮精查上限','延期精查']) assert.ok(unknown.includes(`${label}</dt><dd>—`),label);
  assert.ok(unknown.includes('完整状态未知'));
  assert.ok(unknown.includes('主题轻量盘点范围尚未记录'));
  assert.ok(unknown.includes('延期清单尚未提供'));
  assert.doesNotMatch(unknown, /已精查全部已发现机会/);
  const conflict = view.render(view.model('ordinary', {}, {research_opportunity_audit:{discovered_count:9,refined_count:4,deferred:[{topic:'待精查'}],refinement_complete:true}}));
  assert.ok(conflict.includes('待核对（记录不一致）'));
  assert.doesNotMatch(conflict, /已精查全部已发现机会/);
});

test('audit deferred items escape long text and bound the rendered list explicitly', () => {
  const attack = '<img src=x onerror=alert(1)>"' + '长主题'.repeat(800);
  const audit = {discovered_count:30,refined_count:8,refinement_budget:8,refinement_complete:false,
    deferred:Array.from({length:22}, (_,i) => ({topic:attack + i,opportunity_kind:'__proto__',reason:attack}))};
  const html = view.render(view.model('ordinary', {}, {research_opportunity_audit:audit}));
  assert.doesNotMatch(html, /<img|<script|\[object Object\]/);
  assert.ok(html.includes('&lt;img'));
  assert.ok(html.includes('列出前 12 项，共 22 项延期机会'));
  assert.equal((html.match(/<li><span>&lt;img/g) || []).length,12);
});

// A narrow DOM fixture exercises mount itself; it implements the controls this
// reader owns and does not launch a browser or load application scripts.
function mountDocument() {
  const doc = {activeElement:null,host:null,writes:0};
  const decode = s => s.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
  const node = (tag,attrs={},parent=null) => ({tagName:tag,dataset:{},parentElement:parent,className:attrs.class || '',open:false,
    getAttribute(key) { return attrs[key] == null ? null : attrs[key]; },
    matches(selector) { return selector === 'summary' ? tag === 'summary' : selector === 'a[href]' ? tag === 'a' && attrs.href != null : selector === '.io-evidence' && this.className.split(' ').includes('io-evidence'); },
    closest(selector) { return selector === '.io-focus-plan' && this.plan ? this : this.parentElement?.closest?.(selector) || null; },
    focus(options) { doc.activeElement=this; this.focusOptions=options; }
  });
  const host = node('section');
  host.controls=[];
  host.setAttribute=() => {};
  host.contains=el => Boolean(el && (el === host || el.parentElement === host || el.parentElement?.parentElement === host));
  host.querySelectorAll=selector => host.controls.filter(el => selector === 'details' ? el.tagName === 'details' : selector === 'a[href]' ? el.tagName === 'a' : selector === '[data-brief-file]' && el.getAttribute('data-brief-file') != null);
  host.querySelector=selector => selector === '.io-focus-plan details' ? host.controls.find(el => el.plan) || null : null;
  Object.defineProperty(host,'innerHTML',{set(html) {
    doc.writes++; host.controls=[];
    const attrsOf = str => Object.fromEntries(Array.from(str.matchAll(/([\w-]+)="([^"]*)"/g),m => [m[1],decode(m[2])]));
    for (const match of html.matchAll(/<details([^>]*)><summary>([\s\S]*?)<\/summary>/g)) {
      const attrs=attrsOf(match[1]); const detail=node('details',attrs,host);
      detail.dataset.ioDisclosure=attrs['data-io-disclosure']; detail.plan=detail.dataset.ioDisclosure === 'plan'; detail.open=/\sopen(?:\s|$)/.test(match[1]);
      const summary=node('summary',{},detail); summary.textContent=decode(match[2]);
      detail.querySelector=selector => selector === 'summary' ? summary : null;
      detail.querySelectorAll=() => [];
      host.controls.push(detail);
    }
    for (const match of html.matchAll(/<(button|a)\b([^>]*)>/g)) host.controls.push(node(match[1],attrsOf(match[2]),host));
  }});
  doc.getElementById=() => doc.host;
  doc.createElement=() => host;
  doc.anchor={before(el) {doc.host=el;}};
  return doc;
}

test('projection polling preserves the closed plan, opened appendix, and evidence reference focus', () => {
  const previous=global.document;
  const doc=mountDocument(); global.document=doc;
  try {
    const presentation=chinesePresentation();
    const reading=() => view.model('ordinary',{}, {overview_presentation:presentation});
    view.mount('board',doc.anchor,reading());
    const details=() => new Map(doc.host.querySelectorAll('details').map(el => [el.dataset.ioDisclosure || 'coverage',el]));
    assert.equal(details().get('plan').open,false);
    assert.equal(details().get('evidence').open,false);
    details().get('evidence').open=true;
    details().get('evidence').querySelector('summary').focus();
    presentation.evidence_appendix[0].text='The whole updated source quote.';
    view.mount('board',doc.anchor,reading());
    assert.equal(details().get('evidence').open,true);
    assert.equal(details().get('plan').open,false);
    assert.equal(doc.activeElement,details().get('evidence').querySelector('summary'));
    const refs=() => doc.host.querySelectorAll('a[href]').filter(el=>el.getAttribute('href')==='#packageOverviewEvidence-1');
    refs()[2].focus();
    presentation.reasons[0].text='更新后的中文依据。';
    view.mount('board',doc.anchor,reading());
    assert.equal(doc.activeElement,refs()[2]);
    assert.deepEqual(doc.activeElement.focusOptions,{preventScroll:true});
    const writes=doc.writes;
    view.mount('board',doc.anchor,reading());
    assert.equal(doc.writes,writes);
  } finally {if (previous === undefined) delete global.document; else global.document=previous;}
});

test('polling preserves each disclosure, summary focus, file focus and details link focus', () => {
  const previous=global.document;
  const doc=mountDocument(); global.document=doc;
  try {
    const direction={title:'研究方向',methods:['复核 → 清单'],representative_documents:['A.pdf','B.pdf','C.pdf','D.pdf'],
      evidence_chain:[{evidence_id:'1',source_path:'A.pdf',text:'原文'}],counterevidence:[{source_path:'B.pdf',text:'反例'}]};
    const vm=title => view.model('ordinary',{}, {title,recommended_research_direction:direction,direction_candidates:[{candidate_id:'other',title:'另一建议'}]});
    assert.equal(view.mount('board',doc.anchor,vm('版本1')),true);
    const host=doc.host;
    const details=() => new Map(host.querySelectorAll('details').map(el => [el.dataset.ioDisclosure || 'coverage',el]));
    assert.equal(details().get('plan').open,true);
    details().get('plan').open=false;
    for (const key of ['evidence','research-limits','research-alternatives','coverage']) details().get(key).open=true;
    details().get('research-limits').querySelector('summary').focus();
    view.mount('board',doc.anchor,vm('版本2'));
    assert.equal(doc.activeElement,details().get('research-limits').querySelector('summary'));
    assert.deepEqual(doc.activeElement.focusOptions,{preventScroll:true});
    assert.equal(details().get('plan').open,false);
    for (const key of ['evidence','research-limits','research-alternatives','coverage']) assert.equal(details().get(key).open,true,key);
    const sourceButtons=() => host.querySelectorAll('[data-brief-file]');
    sourceButtons().filter(el => el.getAttribute('data-brief-file') === 'B.pdf').at(-1).focus();
    view.mount('board',doc.anchor,vm('版本3'));
    assert.equal(doc.activeElement.getAttribute('data-brief-file'),'B.pdf');
    assert.equal(doc.activeElement,sourceButtons().filter(el => el.getAttribute('data-brief-file') === 'B.pdf').at(-1),'focus stays in counterevidence rather than moving to the same file in the main card');
    const links=() => host.querySelectorAll('a[href]');
    links()[1].focus();
    view.mount('board',doc.anchor,vm('版本4'));
    assert.equal(doc.activeElement,links()[1]);
    const writes=doc.writes,active=doc.activeElement;
    view.mount('board',doc.anchor,vm('版本4'));
    assert.equal(doc.writes,writes,'unchanged polling must not rebuild controls');
    assert.equal(doc.activeElement,active);
    sourceButtons().filter(el => el.getAttribute('data-brief-file') === 'B.pdf').at(-1).focus();
    direction.counterevidence=[];
    view.mount('board',doc.anchor,vm('版本5'));
    assert.equal(doc.activeElement,sourceButtons().find(el => el.getAttribute('data-brief-file') === 'B.pdf'),'when an evidence control disappears, retain the remaining control for its source');
  } finally {if (previous === undefined) delete global.document; else global.document=previous;}
});

test('audit-only polling updates preserve audit disclosure, keyboard focus and other open sections', () => {
  const previous=global.document;
  const doc=mountDocument(); global.document=doc;
  try {
    const audit={discovered_count:10,refined_count:4,refinement_budget:8,deferred:[{topic:'待精查'}],refinement_complete:false};
    const vm=() => view.model('ordinary',{}, {recommended_research_direction:{title:'当前建议'},research_opportunity_audit:audit});
    view.mount('board',doc.anchor,vm());
    const details=() => new Map(doc.host.querySelectorAll('details').map(el => [el.dataset.ioDisclosure || 'coverage',el]));
    assert.equal(details().get('research-audit').open,false);
    details().get('research-audit').open=true;
    details().get('research-limits').open=true;
    details().get('research-audit').querySelector('summary').focus();
    audit.refined_count=5;
    const writes=doc.writes;
    view.mount('board',doc.anchor,vm());
    assert.equal(doc.writes,writes+1,'changed audit must update even when recommendation is unchanged');
    assert.equal(details().get('research-audit').open,true);
    assert.equal(details().get('research-limits').open,true);
    assert.equal(doc.activeElement,details().get('research-audit').querySelector('summary'));
    assert.deepEqual(doc.activeElement.focusOptions,{preventScroll:true});
    assert.ok(doc.activeElement.textContent.includes('精查 5 / 10'));
    details().get('research-audit').open=false;
    audit.refinement_budget=9;
    view.mount('board',doc.anchor,vm());
    assert.equal(details().get('research-audit').open,false);
  } finally {if (previous === undefined) delete global.document; else global.document=previous;}
});
