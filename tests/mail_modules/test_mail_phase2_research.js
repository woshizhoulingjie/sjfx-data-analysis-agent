/* Real-template acceptance: direct answer, clean evidence appendix and cumulative continuation. */
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
let chromium;try{chromium=require('playwright').chromium}catch(_){console.log('SKIP: Playwright unavailable');process.exit(0)}
if(!fs.existsSync(chromium.executablePath())){console.log('SKIP: Chromium unavailable');process.exit(0)}
const root=path.resolve(__dirname,'../..');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  const html=fs.readFileSync(path.join(root,'templates/mail_research.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,'');
  await page.setContent(html);await page.addStyleTag({path:path.join(root,'static/mail-research.css')});
  await page.evaluate(()=>{
   window.calls=[];window.resumed=false;
   window.SJFXMailFoundation={currentScope:()=>({package_id:'p',date_from:'2024-05-03'}),query:()=>'',emitScope:()=>{},refreshFoundation:async()=>{},renderMessage:async()=>{}};
   const final=()=>({result_set_id:window.resumed?'r2':'r1',research_version:'mail-research-phase2-v1',question:'双方对 v2 的条件是什么？',answer:'双方接受 v2 的前提是完成联调。现有邮件没有证明这一条件已满足，因此目前不能确认已经履行。',answer_presentation:{version:'answer-first-v1',status:'complete'},evidence_appendix:{recorded_answer:'2026年10月9日，Alice 的邮件记载：完成联调后接受 v2。'},limitations:['mm_internal: uncertain_thread_links','补检停止：awaiting_semantic_verification'],context_review:Array.from({length:8},(_,i)=>({complete:false,message_key:'mm_'+i,gaps:['uncertain_thread_links']})),research_memory:{conditions:['条件与后续变化'],open_questions:['待核实问题']},coverage:{research:{matched:2,model_calls:2,tool_calls:3}},fields:[],evidence:[{evidence_id:'e1',message_key:'m1',quote_text:'完成联调后接受 v2。',source_kind:'body',subject_raw:'v2 确认',actor:'alice@example.com',sent_at_utc:'2026-10-09T01:00:00Z'}],findings:[{text:'完成联调后接受 v2。',evidence_id:'e1'}],claims:[{claim_text:'Alice 接受 v2',semantic_status:'supported',evidence_ids:[],semantic_verification:{atoms:[{text:'Alice 表达接受 v2',status:'supported'}],missing_requirements:[]}},{claim_text:'当前条件已满足',semantic_status:'insufficient',evidence_ids:[],semantic_verification:{atoms:[{text:'当前条件已满足',status:'insufficient'}],missing_requirements:['temporal_scope_or_full_history_unproven'],temporal_status:'unknown'}}],token_usage:{total_tokens:30,actual_tokens:20,estimated_tokens:10,reserved_tokens:0,max_tokens:window.resumed?60:30,exhausted:!window.resumed},gap_search:{rounds:[{queries:['accept v2']}],stop_reason:'token_budget'}});
   const turn=()=>({turn_id:'rt',status:'completed',sufficiency:window.resumed?'sufficient':'partial',result_set_id:window.resumed?'r2':'r1',task_spec:{package_id:'p',research_version:'mail-research-phase2-v1',scope:{date_from:'2024-05-03'},budget:{max_tokens:30,max_model_calls:2,max_tool_calls:3,max_seconds:10}},final:final()});
   window.fetch=async(raw,options={})=>{
    const u=new URL(raw,'http://test');window.calls.push({path:u.pathname,method:options.method||'GET',body:options.body});let data;
    if(u.pathname==='/api/mail/packages')data={packages:[{package_id:'p',display_name:'虚构验证样本',status:'ready'}]};
    else if(u.pathname==='/api/mail/packages/p')data={overview:{counts:{logical_messages:2}},coverage:{}};
    else if(u.pathname.endsWith('/messages'))data={messages:{items:[],total:0}};
    else if(u.pathname==='/api/mail/research-sessions')data=options.method==='POST'?{session:{session_id:'s'}}:{sessions:[]};
    else if(u.pathname==='/api/mail/research-sessions/s/turns')data={turn:{turn_id:'rt'},job_id:'j'};
    else if(u.pathname==='/api/mail/research-turns/rt/resume'){window.resumed=true;data={turn_id:'rt',job_id:'jr'};}
    else if(u.pathname==='/api/mail/research-turns/rt')data={turn:turn()};
    else if(u.pathname.endsWith('/relations/communication'))data={communication:{items:[]}};
    else if(u.pathname.endsWith('/relations/threads'))data={threads:[]};
    else if(u.pathname.endsWith('/relations/business'))data={relations:[]};
    else if(u.pathname.endsWith('/relations/network'))data={network:{nodes:[],edges:[]}};
    else if(/^\/api\/mail\/result-sets\//.test(u.pathname))data={result_set:{result_set_id:window.resumed?'r2':'r1',item_count:0,items:[],definition:{},final:final()}};
    else if(u.pathname==='/api/mail/evidence/e1')data={evidence:{evidence_id:'e1',message_key:'m1',mime_path:'1',quote_text:'完成联调后接受 v2。'}};
    else if(u.pathname==='/api/mail/messages/m1')data={message:{subject_raw:'v2 确认',sent_at_utc:'2026-10-09T01:00:00Z',participants:[],parts:[]}};
    else if(u.pathname==='/api/mail/messages/m1/preview')data={content_type:'text/plain',content:'完成联调后接受 v2。'};
    else if(u.pathname==='/api/mail/exports')data={job_id:'export-job'};
    else if(u.pathname==='/api/jobs/export-job')data={job:{status:'completed',task_type:'mail_export',result:{filename:'research.xlsx'}}};
    else if(u.pathname==='/api/download-ticket')data={download_url:'/test/research.xlsx'};
    else if(u.pathname==='/api/mail/presentation')data={presentation:{status:'completed',values:{},metadata:{}}};
    else if(['/coverage','/result-sets','/conversations','/attachment-versions'].some(s=>u.pathname.endsWith(s)))data={items:[],total:0};
    else throw new Error('Unexpected endpoint '+raw);
    return{ok:true,status:200,json:async()=>({ok:true,...data})};
   };
  });
  await page.addScriptTag({path:path.join(root,'static/mail-research.js')});await page.addScriptTag({path:path.join(root,'static/mail-modules.js')});
  await page.waitForFunction(()=>document.querySelector('#mail-package').options.length===2);await page.locator('#mail-package').selectOption('p');await page.getByText('当前范围没有邮件',{exact:true}).first().waitFor({state:'attached'});await page.locator('[data-mail-module-link="research"]').click();
  await page.locator('#mail-question').fill('双方对 v2 的条件是什么？');await page.locator('#mail-question-form').evaluate(n=>n.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
  await page.getByRole('button',{name:'增加预算继续研究',exact:true}).waitFor();
  assert.equal(await page.locator('#mail-answer').innerText(),'双方接受 v2 的前提是完成联调。现有邮件没有证明这一条件已满足，因此目前不能确认已经履行。');
  assert((await page.locator('#mail-answer-question').innerText()).includes('双方对 v2 的条件是什么？'));
  const meta=await page.locator('#mail-answer-meta').innerText();assert(meta.includes('依据 1 封相关邮件'));assert(!meta.includes('token')&&!meta.includes('补检')&&!meta.includes('上下文'));
  assert.equal(await page.locator('#mail-evidence-appendix').evaluate(n=>n.open),false);
  assert.equal(await page.locator('#mail-research-details').evaluate(n=>n.open),false);
  const clean=await page.locator('.mail-answer-result').innerText();for(const noisy of ['逐项语义','条件与后续变化','待核实问题','uncertain_thread_links','mm_internal','上下文尚未核查完整','awaiting_semantic_verification'])assert(!clean.includes(noisy),noisy);
  if(process.env.MAIL_UI_SCREENSHOT){fs.mkdirSync(path.dirname(process.env.MAIL_UI_SCREENSHOT),{recursive:true});await page.screenshot({path:process.env.MAIL_UI_SCREENSHOT,fullPage:true});}
  await page.locator('#mail-evidence-summary').click();
  assert.equal(await page.locator('#mail-evidence-list>li').count(),1);
  assert((await page.locator('#mail-evidence-list').innerText()).includes('完成联调后接受 v2。'));
  await page.locator('#mail-recorded-answer-section>summary').click();assert((await page.locator('#mail-recorded-answer').innerText()).includes('Alice 的邮件记载'));
  await page.locator('#mail-evidence-list summary').click();assert((await page.locator('#mail-evidence-list details').innerText()).includes('完成联调后接受 v2。'));
  await page.getByRole('button',{name:'打开对应原文',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#mail-original').textContent.includes('完成联调后接受 v2。'));
  await page.locator('#mail-research-details>summary').click();const details=await page.locator('#mail-research-details-body').innerText();assert(details.includes('实际 20，估算 10'));assert(details.includes('补检 1 轮'));
  await page.locator('#mail-evidence-summary').click();await page.locator('#mail-research-details>summary').click();
  await page.getByRole('button',{name:'增加预算继续研究',exact:true}).click();await page.waitForFunction(()=>window.resumed);await page.waitForFunction(()=>!document.querySelector('[data-mail-resume-research]'));
  const request=await page.evaluate(()=>window.calls.find(c=>c.path.endsWith('/resume')));assert.deepEqual(JSON.parse(request.body),{budget:{max_tokens:60,max_model_calls:4,max_tool_calls:6,max_seconds:20}});assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/turns')).length),1);
  assert(!(await page.locator('.mail-answer-result').innerText()).includes('uncertain_thread_links'));
  await page.locator('[data-mail-module-link="research"]').click();await page.locator('#mail-export').click();await page.waitForFunction(()=>window.calls.some(c=>c.path==='/api/download-ticket'));
  const exported=await page.evaluate(()=>window.calls.find(c=>c.path==='/api/mail/exports'));assert.deepEqual(JSON.parse(exported.body),{result_set_id:'r2',format:'xlsx'});
  assert.deepEqual(errors,[]);console.log('PASS: direct Chinese answer, hidden diagnostics, expandable source appendix, original source, export, cumulative budget continuation and original-turn polling');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
