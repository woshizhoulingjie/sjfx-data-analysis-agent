/* Exercise live, session history and standalone result history using the real template. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),root=path.resolve(__dirname,'../..');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{for(const entry of ['live','session','result','stale']){
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setContent(fs.readFileSync(path.join(root,'templates/mail_research.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,''));
  await page.evaluate(entry=>{
   window.calls=[];window.resumed=false;window.entry=entry;
   window.SJFXMailFoundation={currentScope:()=>({package_id:'p',date_from:'2024-01-01'}),query:()=>'',emitScope:()=>{},refreshFoundation:async()=>{}};
   const final=()=>({kind:'intent_screen',mode:'exhaustive',result_set_id:window.resumed?'r2':'r1',answer:'逐封判定：1封相关，1封待判断。',complete:window.resumed,budget_exhausted:!window.resumed,model_calls:2,token_usage:{total_tokens:30,reserved_tokens:5,max_tokens:35,exhausted:!window.resumed},coverage:{research:{matched:1,unknown:window.resumed?0:1}},evidence:[],limitations:[]});
   const turn=()=>({turn_id:'rt',status:'completed',sufficiency:window.resumed?'sufficient':'partial',result_set_id:window.resumed?'r2':'r1',task_spec:{mode:'intent_screen',package_id:'p',model_call_budget:2,max_tokens:35,scope:{date_from:'2024-01-01'}},final:{...final(),...(entry==='stale'?{result_set_id:'changed'}:{})}});
   window.fetch=async(raw,options={})=>{
    const u=new URL(raw,'http://test');window.calls.push({path:u.pathname,body:options.body,method:options.method||'GET'});let data={};
    if(u.pathname==='/api/mail/packages')data={packages:[{package_id:'p',display_name:'虚构范围',status:'ready'}]};
    else if(u.pathname==='/api/mail/packages/p')data={overview:{counts:{}},coverage:{}};
    else if(u.pathname.endsWith('/messages'))data={messages:{items:[],total:0}};
    else if(u.pathname==='/api/mail/research-sessions')data=options.method==='POST'?{session:{session_id:'s'}}:{sessions:[{session_id:'s',title:'逐封历史'}]};
    else if(u.pathname==='/api/mail/research-sessions/s')data={turns:[{turn_id:'rt',status:'completed',result_set_id:'r1'}]};
    else if(u.pathname==='/api/mail/research-sessions/s/turns')data={turn:{turn_id:'rt'},job_id:'j'};
    else if(u.pathname==='/api/mail/research-turns/rt')data={turn:turn()};
    else if(u.pathname==='/api/mail/intent-analyses/rt/resume'){window.resumed=true;data={turn_id:'rt',job_id:'new-job'};}
    else if(u.pathname==='/api/mail/result-sets/r1'||u.pathname==='/api/mail/result-sets/r2')data={result_set:{result_set_id:window.resumed?'r2':'r1',source_turn_id:'rt',item_count:2,items:[],definition:{},final:final()}};
    else if(u.pathname.endsWith('/result-sets'))data={items:[{result_set_id:'r1',item_count:2}]};
    else if(u.pathname.endsWith('/relations/communication'))data={communication:{items:[]}};
    else if(u.pathname.endsWith('/relations/threads'))data={threads:[]};
    else if(u.pathname.endsWith('/relations/business'))data={relations:[]};
    else if(u.pathname.endsWith('/relations/network'))data={network:{nodes:[],edges:[]}};
    else if(u.pathname==='/api/mail/presentation')data={presentation:{status:'completed',values:{},metadata:{}}};
    else if(['/coverage','/conversations','/attachment-versions'].some(p=>u.pathname.endsWith(p)))data={items:[]};
    else if(u.pathname!='/api/mail/workspace-guide')throw new Error('Unexpected endpoint '+u.pathname);
    return{ok:true,status:200,json:async()=>({ok:true,...data})};
   };
  },entry);
  for(const name of ['mail-research.js','mail-modules.js'])await page.addScriptTag({path:path.join(root,'static',name)});
  await page.waitForFunction(()=>document.querySelector('#mail-package').options.length===2);await page.locator('#mail-package').selectOption('p');
  await page.waitForFunction(()=>document.querySelector('#mail-result-history').options.length===2);
  await page.locator(`[data-mail-module-link="${entry==='live'||entry==='session'?'research':'results'}"]`).click();
  if(entry==='live'){
   await page.locator('#mail-question').fill('逐封查找所有条件');await page.locator('#mail-question-form').evaluate(n=>n.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
  }else if(entry==='session')await page.locator('#mail-session').selectOption('s');
  else await page.locator('#mail-result-history').selectOption('r1');
  const resume=page.locator('[data-mail-resume-screen]');await resume.waitFor({state:'attached'});
  if(entry==='result'||entry==='stale')await page.getByRole('button',{name:'查看结论与继续分析',exact:true}).click();
  else if(entry==='session')await page.locator('[data-mail-module-link="answer"]').click();
  await resume.waitFor({state:'visible'});
  assert((await page.locator('#mail-answer-meta').textContent()).includes('逐封筛查已暂停'));
  if(entry==='live'){await page.waitForFunction(()=>document.querySelector('#mail-status').textContent.includes('暂停'));assert((await page.locator('#mail-progress').textContent()).includes('暂停'));}
  await resume.click();
  if(entry==='stale'){
   await page.waitForFunction(()=>document.querySelector('#mail-status').textContent.includes('结果已变化'));
   assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/resume')).length),0);
  }else{
   await page.waitForFunction(()=>window.resumed&&!document.querySelector('[data-mail-resume-screen]'));
   const calls=await page.evaluate(()=>window.calls);const request=calls.find(c=>c.path.endsWith('/resume'));
   assert.equal(request.path,'/api/mail/intent-analyses/rt/resume');assert.deepEqual(JSON.parse(request.body),{model_call_budget:4,max_tokens:36035});
   assert.equal(calls.filter(c=>c.method==='POST'&&c.path.endsWith('/turns')).length,entry==='live'?1:0);
   assert(!calls.some(c=>c.path==='/api/mail/research-turns/rt/resume'));
  }
  assert.deepEqual(errors,[]);await page.close();
 }console.log('PASS: screen checkpoint resume from live/session/result history, cumulative budgets and stale-result rejection');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
