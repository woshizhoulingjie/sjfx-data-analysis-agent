// Offline contract regression: no server or translation model is needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Node {
  constructor(tag='div') {this.tagName=tag;this.children=[];this.events={};this.dataset={};this.style={};this.value='';this.checked=false;this._text='';this.classList={add:()=>{},remove:()=>{}};}
  set textContent(value) {this._text=String(value);this.children=[];}
  get textContent() {return this._text+this.children.map(c=>c.textContent).join('');}
  get lastChild() {return this.children.at(-1);}
  append(...nodes) {this.children.push(...nodes);}
  prepend(...nodes) {this.children.unshift(...nodes);}
  replaceChildren(...nodes) {this._text='';this.children=nodes;}
  addEventListener(name,fn) {(this.events[name]??=[]).push(fn);}
  setAttribute(k,v) {this[k]=v;}
  querySelectorAll(tag) {return this.children.flatMap(c=>[...(c.tagName===tag?[c]:[]),...c.querySelectorAll(tag)]);}
  click() {return Promise.all((this.events.click||[]).map(fn=>fn({target:this})));}
  remove() {}
}
const ids=new Map(['mail-package','mail-query','mail-participant','mail-with-attachment','mail-date-from','mail-date-to','mail-foundation-reader','mail-conversation-detail','mail-foundation-status','mail-foundation-inventory','mail-inventory-page','mail-inventory-prev','mail-inventory-next','mail-foundation-refresh','mail-foundation-backfill','mail-foundation-job','mail-status'].map(id=>[id,new Node()]));
ids.get('mail-package').value='pkg';ids.get('mail-date-from').value='2026-01-01';ids.get('mail-query').value='报价';
const events=[];const calls=[];const requests=[];const listeners={};const document={getElementById:id=>ids.get(id),createElement:tag=>new Node(tag),querySelector:()=>new Node('button'),addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:e=>{events.push(e);(listeners[e.type]||[]).forEach(fn=>fn(e));},body:new Node('body')};
const fixtures={
  content:{ok:true,content:{raw_available:true,raw_headers:'Subject: original',occurrences:[{source_path:'inbox/a.eml'}],parts:[{mime_path:'1',part_role:'body',html:'<script>bad()</script><b>html</b>',text:'new text\nquoted text\nuncertain text',fragments:[{fragment_kind:'authored',text:'new text',char_start:0,char_end:8},{fragment_kind:'quoted',text:'quoted text',char_start:9,char_end:20},{fragment_kind:'uncertain',text:'uncertain text'}]}]}},
  conversation:{ok:true,conversation:{subject_normalized:'conversation',timeline:[{message_key:'parent',subject_raw:'original'},{message_key:'child',subject_raw:'reply changed subject'}],tree_nodes:[{node_id:'parent',message_key:'parent'},{node_id:'child',message_key:'child'},{node_id:'absent',label:'not imported',missing:true}],tree_edges:[{source_node_id:'child',target_node_id:'parent',source_message_key:'child',target_message_key:'parent',status:'exact'}],context_edges:[],candidate_edges:[{source_message_key:'child',target_message_key:'other',status:'candidate'}],anomalies:[{anomaly_type:'duplicate_message_id',detail:'ambiguous'}]}},
  foundation:{ok:true,foundation:{counts:{logical_messages:2,occurrences:3},stages:{body:{status:'completed'},ocr:{status:'failed',attempts:3,error:'bad PDF'}},versions:{parser:2}}},
  inventory:{ok:true,inventory:{items:[{source_path:'a.eml',status:'completed',message_key:'parent'},{source_path:'b.pdf',status:'failed',error:'password protected'}],total:30}},
  segments:{ok:true,segments:[{mime_path:'2',page_number:3,text:'page three'},{mime_path:'2',sheet_name:'Sheet B',cell_range:'A1:D4',text:'cells'}]},
  backfill:{ok:true,job_id:'backfill-job'},
  job:{ok:true,job:{status:'completed',progress:100,message:'backfill done'}}
};
const window={sessionStorage:{getItem:()=> 'test-token'},localStorage:{getItem:()=>null},setTimeout:()=>0,SJFXMailReader:{inspectMessage:async(...args)=>requests.push(args)}};
const context={document,window,URLSearchParams,CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},fetch:async(url,options={})=>{calls.push({url,options});let key=url.includes('/content?')?'content':url.includes('/conversations/')?'conversation':url.includes('/foundation/backfill')?'backfill':url.includes('/api/jobs/')?'job':url.includes('/foundation')?'foundation':url.includes('/inventory?')?'inventory':'segments';return{ok:true,json:async()=>fixtures[key],blob:async()=>({})};}};
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../../static/mail-ui.js'),'utf8'),context);
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../../static/mail-foundation.js'),'utf8'),context);
const api=window.SJFXMailFoundation;
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await api.renderMessage('parent',{conversation_ids:['conv'],reply_edges:[{source_message_key:'child',target_message_key:'parent'}],parts:[{mime_path:'2',filename_raw:'report.pdf',is_explicit_attachment:true,processing_status:'waiting_retry',retry_count:2,error:'bad page',payload_sha256:'abc'}]});
  let reader=ids.get('mail-foundation-reader');
  assert(reader.textContent.includes('new text\nquoted text\nuncertain text'),'full source survives uncertain boundaries');
  const frame=reader.querySelectorAll('iframe')[0];assert.equal(frame.sandbox,'');assert(frame.srcdoc.includes("default-src 'none'"),'HTML remains isolated with restrictive CSP');
  assert(reader.textContent.includes('Subject: original')&&reader.textContent.includes('inbox/a.eml'),'raw headers and sources are visible');
  assert(reader.textContent.includes('bad page')&&reader.textContent.includes('abc'),'attachment errors, hash and retry information remain visible');
  reader.querySelectorAll('button').find(b=>b.textContent==='下载原文件').click();await settle();
  assert(calls.some(c=>c.url==='/api/mail/messages/parent/parts/2/raw?package_id=pkg'),'attachment download uses actual raw-part route');
  reader.querySelectorAll('button').find(b=>b.textContent==='重试此附件').click();await settle();await settle();
  const retry=calls.find(c=>c.url==='/api/mail/messages/parent/attachments/2/retry');assert(retry&&retry.options.method==='POST'&&JSON.parse(retry.options.body).package_id==='pkg','attachment retry is scoped to owning package');
  reader.querySelectorAll('button').find(b=>b.textContent==='新增正文').click();await settle();
  assert(reader.textContent.includes('new text')&&!reader.textContent.includes('quoted text'),'authored view excludes historic quote');
  await api.openMessage('child',{package_id:'other',has_attachment:true});
  assert.equal(requests[0][2].package_id,'other');assert.equal(requests[0][2].q,'报价');
  api.emitScope();assert.equal(events[0].type,'sjfx:mail-scope');assert.equal(events[0].detail.date_from,'2026-01-01');
  assert.equal(events[0].detail.has_attachment,null,'unchecked global checkbox means all mail, never only no-attachment mail');
  assert(!api.query(api.currentScope()).includes('has_attachment'),'all-mail scope omits attachment filtering');
  ids.get('mail-with-attachment').checked=true;api.emitScope();assert.equal(events.at(-1).detail.has_attachment,true);assert(api.query(api.currentScope()).includes('has_attachment=true'),'checked scope filters attachment mail');
  ids.get('mail-with-attachment').checked=false;api.emitScope();assert.equal(events.at(-1).detail.has_attachment,null,'switching back restores all-mail scope');
  document.dispatchEvent(new context.CustomEvent('sjfx:mail-scope',{detail:{package_id:'pkg',q:'people-only',has_attachment:'false',source:'people'}}));
  assert.equal(api.currentScope().has_attachment,false,'people no-attachment filter survives an unchecked global checkbox');
  assert.equal(api.currentScope().q,'people-only');assert(api.query(api.currentScope()).includes('has_attachment=false'));
  api.emitScope(api.currentScope({participant:'partner@example.test'}));assert.equal(api.currentScope().has_attachment,false,'changing the relationship participant preserves the active no-attachment range');assert.equal(api.currentScope().q,'people-only');assert.equal(api.currentScope().participant,'partner@example.test');
  api.emitScope();assert.equal(api.currentScope().has_attachment,null,'explicit global filter submission restores all attachments');
  assert.equal(api.currentScope().q,'报价','explicit global submission reads edited DOM values');
  await api.openConversation('conv');let conv=ids.get('mail-conversation-detail');
  assert(calls.at(-1).url.includes('date_from=2026-01-01')&&calls.at(-1).url.includes('package_id=pkg'),'conversation inherits scope');
  conv.querySelectorAll('button').find(b=>b.textContent==='回复树').click();await settle();
  assert(conv.textContent.includes('reply changed subject')&&conv.textContent.includes('邮件未收录'),'reply tree preserves branches and missing parents');
  assert(conv.textContent.includes('候选关联（未确认）')&&conv.textContent.includes('父邮件不明确'),'candidates and anomalies remain distinct and readable');
  const ordinary=fixtures.conversation.conversation.tree_edges;
  fixtures.conversation.conversation.tree_edges=[...ordinary,{source_node_id:'parent',target_node_id:'child',source_message_key:'parent',target_message_key:'child',relation_type:'reply_to',status:'cycle'}];
  await api.openConversation('conv');conv=ids.get('mail-conversation-detail');await conv.querySelectorAll('button').find(b=>b.textContent==='回复树').click();await settle();
  assert(conv.textContent.includes('未采用为父子关系的异常邮件头')&&conv.textContent.includes('循环回复异常'),'cyclic header links are shown separately from confirmed parents');
  const cards=conv.querySelectorAll('section').filter(s=>s.style.marginLeft!==undefined);
  assert.equal(cards.find(s=>s.textContent.includes('original')).style.marginLeft,'0px','cycle cannot invert the root in the reply tree');
  assert.equal(cards.find(s=>s.textContent.includes('reply changed subject')).style.marginLeft,'14px','valid branch keeps its real depth');
  fixtures.conversation.conversation.tree_edges=ordinary;
  const ordinaryConversation=fixtures.conversation.conversation;
  fixtures.conversation.conversation={subject_normalized:'gap context',messages:[{message_key:'parent',subject_raw:'earlier ancestor'},{message_key:'child',subject_raw:'after missing parent'}],tree_nodes:[{node_id:'message:parent',message_key:'parent'},{node_id:'message:child',message_key:'child'},{node_id:'missing:one',missing:true}],tree_edges:[{source_node_id:'message:child',target_node_id:'missing:one',source_message_key:'child',target_message_key:null,status:'missing_target',relation_type:'reply_to'}],context_bridges:[{source_message_key:'child',ancestor_message_key:'parent',missing_node_id:'missing:one',relation_type:'historical_gap_context',status:'context',evidence:{direct_parent_replaced:false}}],gap_summary:{missing_message_count:1,direct_parent_gap_count:1,inferred_parent_gap_count:0,historical_reference_count:31},missing_context:Array.from({length:32},(_,i)=>({source_message_key:'child',target_node_id:'missing:one',status:'missing_target',relation_type:i?'reference':'reply_to'}))};
  await api.openConversation('gap');conv=ids.get('mail-conversation-detail');await conv.querySelectorAll('button').find(b=>b.textContent==='回复树').click();await settle();
  assert(conv.textContent.includes('未收录邮件：1 封')&&conv.textContent.includes('历史上下文引用：31 条'),'many missing-reference records count one absent message');
  assert(conv.textContent.includes('References 历史上下文')&&conv.textContent.includes('较早邮件不是后续邮件的直接回复对象'),'context bridge cannot be mistaken for a direct reply');
  const gapCards=conv.querySelectorAll('section').filter(s=>s.style.marginLeft!==undefined);
  const viewPane=conv.querySelectorAll('div').find(row=>row.dataset.mailConversationView==='tree');
  assert(viewPane,'rendered view exposes the completed asynchronous switch');
  assert.equal(conv.querySelectorAll('button').find(b=>b.textContent==='回复树')['aria-pressed'],'true');
  const gapRows=new Map(viewPane.children.map(row=>[row.dataset.replyNode,row]));
  assert.equal(gapRows.size,3,'each mail and the missing parent render once');
  assert.equal(gapRows.get('message:child').dataset.replyParent,'missing:one');
  assert.equal(gapRows.get('missing:one').dataset.replyParent,'message:parent');
  assert.equal(gapRows.get('message:child').dataset.replyDepth,'2');
  assert.equal(gapCards.find(s=>s.textContent.includes('earlier ancestor')).style.marginLeft,'0px');
  assert.equal(gapCards.find(s=>s.textContent.includes('after missing parent')).style.marginLeft,'28px','context bridge preserves the missing intermediate node');
  assert.equal(fixtures.conversation.conversation.tree_edges[0].target_message_key,null,'reader never replaces the direct missing parent with an ancestor');
  fixtures.conversation.conversation=ordinaryConversation;
  await api.refreshFoundation();assert(ids.get('mail-foundation-status').textContent.includes('bad PDF'));assert(ids.get('mail-foundation-inventory').textContent.includes('password protected'));assert.equal(ids.get('mail-inventory-next').disabled,false);
  await api.openAttachment('parent','2');reader=ids.get('mail-foundation-reader');assert(reader.textContent.includes('第 3 页')&&reader.textContent.includes('工作表 Sheet B')&&reader.textContent.includes('范围 A1:D4'),'attachment locator is rendered without guessed pages');
  const previousSegments=fixtures.segments.segments;
  fixtures.segments.segments=[{mime_path:'2',page_number:1,text:'OCR unmodified text 9,000.00',text_origin:'ocr',source_text_notice:'扫描件 OCR 可能漏字，请回看原图。',ocr_quality:{minimum_confidence:.99,recognition_accuracy:'unverified'}}];
  await api.openAttachment('parent','2');reader=ids.get('mail-foundation-reader');
  assert(reader.textContent.includes('OCR 识别文本')&&reader.textContent.includes('可能漏字')&&reader.textContent.includes('回看原图'),'ready OCR text explicitly retains uncertainty and source route');
  assert(reader.textContent.includes('0.990')&&reader.textContent.includes('不是逐字准确率'),'OCR confidence never promises character fidelity');
  assert(reader.querySelectorAll('pre').some(row=>row.textContent==='OCR unmodified text 9,000.00'),'quality notice never edits the literal OCR text');
  fixtures.segments.segments=previousSegments;
  ids.get('mail-foundation-backfill').click();await settle();await settle();
  assert(calls.some(c=>c.url==='/api/mail/packages/pkg/foundation/backfill'&&c.options.method==='POST'),'history backfill uses queued endpoint');
  assert(ids.get('mail-foundation-job').textContent.includes('backfill done'),'history backfill shows job completion');assert.equal(ids.get('mail-foundation-backfill').disabled,false,'backfill control recovers after terminal status');
  assert(calls.every(c=>c.options.headers['X-SJFX-Token']==='test-token'),'all foundation APIs are authenticated');
  const originalFetch=context.fetch;let resume;context.fetch=async()=>new Promise(resolve=>{resume=()=>resolve({ok:true,json:async()=>fixtures.content});});
  const pending=api.renderMessage('stale',{parts:[]});api.emitScope();reader.textContent='new scope';resume();await pending;assert.equal(reader.textContent,'new scope','old package request cannot overwrite the current scope');context.fetch=originalFetch;
  console.log('mail foundation: content, source, filters, tree, recovery, HTML isolation and stale-scope regressions passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
