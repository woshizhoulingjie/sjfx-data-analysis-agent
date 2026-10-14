/* Existing sessions recover server progress without creating another task. */
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');const root=path.resolve(__dirname,'../..');
const html=fs.readFileSync(path.join(root,'templates/mail_research.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'');
(async()=>{
const browser=await chromium.launch({headless:true});
try{const page=await browser.newPage();await page.setContent(html);
await page.evaluate(async source=>{
const assert=Object.assign(ok=>{if(!ok)throw new Error('Assertion failed');},{equal:(a,b)=>{if(a!==b)throw new Error(`${a} != ${b}`);}});
const wait=async(fn)=>{const until=Date.now()+6000;while(!fn()){if(Date.now()>until)throw new Error('Timed out waiting for UI');await new Promise(r=>setTimeout(r,20));}};
const w=window,calls=[];let runningStatus='queued',networkStatus=200,holdNetwork=null,releaseNetwork=null;
let sessionNames=['run','cancel','failed','complete'],newSessionCount=0,holdTurn=null,releaseTurn=null,holdResult=null,releaseResult=null,lateTurnError=false;
let holdSessionList=null,releaseSessionList=null;
w.matchMedia=()=>({matches:false,addEventListener(){}});w.HTMLElement.prototype.scrollIntoView=()=>{};
w.fetch=async(raw,options={})=>{const u=new URL(raw,'http://test'),method=options.method||'GET';calls.push({path:u.pathname,method});let data;
if(u.pathname==='/api/mail/workspace-guide')data={guide:{}};
else if(u.pathname==='/api/mail/packages')data={packages:['p','q'].map(package_id=>({package_id,display_name:package_id,status:'ready'}))};
else if(['/api/mail/packages/p','/api/mail/packages/q'].includes(u.pathname))data={overview:{counts:{}},coverage:{}};
else if(u.pathname.endsWith('/messages'))data={messages:{items:[],total:0}};
else if(u.pathname==='/api/mail/research-sessions'){
  if(method==='POST'){const id='new-'+(++newSessionCount);sessionNames.push(id);data={session:{session_id:id}};}
  else {if(holdSessionList){const pending=holdSessionList;holdSessionList=null;await pending;}data={sessions:sessionNames.map(s=>({session_id:s,title:s}))};}
}
else if(u.pathname.startsWith('/api/mail/research-sessions/')){const s=u.pathname.split('/').at(-1);data={turns:s==='run'?[{turn_id:'old',status:'completed',result_set_id:'r'},{turn_id:'run',status:runningStatus}]:[{turn_id:s,status:s==='complete'?'completed':s==='cancel'?'cancelled':'failed',result_set_id:s==='complete'?'r':null}]};}
else if(u.pathname.startsWith('/api/mail/research-turns/')){const s=u.pathname.split('/').at(-1);data={turn:{turn_id:s,status:s==='run'?runningStatus:s==='complete'?'completed':s==='cancel'?'cancelled':'failed',progress:{phase:'context_review',model_calls:4},events:[],result_set_id:s==='complete'?'r':null,final:s==='complete'?{answer:'saved completed answer',result_set_id:'r',fields:[],evidence:[]}:null}};
  if(s==='run'&&holdTurn){const pending=holdTurn;holdTurn=null;await pending;if(lateTurnError)throw new Error('old turn unavailable');data.turn={turn_id:'run',status:'completed',result_set_id:'r',final:{answer:'late old turn answer',result_set_id:'r',fields:[],evidence:[]}};}
}
else if(u.pathname==='/api/mail/result-sets/r'){data={result_set:{result_set_id:'r',source_turn_id:'complete',item_count:0,items:[],definition:{},final:{answer:'saved completed answer',result_set_id:'r',fields:[],evidence:[]}}};if(holdResult){const pending=holdResult;holdResult=null;await pending;}}
else if(u.pathname==='/api/mail/presentation')data={presentation:{status:'completed',values:{},metadata:{}}};
else if(u.pathname.endsWith('/relations/communication'))data={communication:{items:[]}};
else if(u.pathname.endsWith('/relations/threads'))data={threads:[]};
else if(u.pathname.endsWith('/relations/business'))data={relations:[]};
else if(u.pathname.endsWith('/relations/network')){
  const responseStatus=networkStatus;
  if(holdNetwork){const pending=holdNetwork;holdNetwork=null;await pending;}
  if(responseStatus!==200)return {ok:false,status:responseStatus,json:async()=>({ok:false,error:responseStatus===409?'资料正在变化，请重新加载图谱':'图谱访问被拒绝'})};
  data={network:{nodes:[],edges:[]}};
}
else if(['/coverage','/result-sets','/conversations','/attachment-versions'].some(x=>u.pathname.endsWith(x)))data={items:[],total:0};
else throw new Error('Unexpected endpoint '+u.pathname);
return {ok:true,status:200,json:async()=>({ok:true,...data})};};
w.eval(source);
const by=id=>w.document.getElementById(id),select=(id,value)=>{by(id).value=value;by(id).dispatchEvent(new w.Event('change',{bubbles:true}));};
await wait(()=>by('mail-package').options.length===3);select('mail-package','p');await wait(()=>by('mail-session').options.length===5);
select('mail-session','run');await wait(()=>by('mail-progress').textContent.includes('等待开始'));assert(by('mail-start').disabled);assert(!by('mail-cancel').disabled);assert(calls.some(c=>c.path==='/api/mail/research-turns/run'));assert(!calls.some(c=>c.path==='/api/mail/result-sets/r'));
runningStatus='running';await wait(()=>by('mail-progress').textContent.includes('核查前后文'));assert(by('mail-progress').textContent.includes('4 次分析'));
const priorGets=calls.filter(c=>c.path==='/api/mail/research-turns/run').length;by('mail-refresh').click();await wait(()=>calls.filter(c=>c.path==='/api/mail/research-turns/run').length>priorGets&&by('mail-session').value==='run'&&by('mail-progress').textContent.includes('核查前后文'));assert(by('mail-start').disabled);
networkStatus=409;by('mail-refresh').click();await wait(()=>by('mail-session').value==='run'&&by('mail-progress').textContent.includes('4 次分析')&&by('mail-network').querySelector('[data-mail-network-retry]'));
assert(by('mail-network').textContent.includes('资料正在变化'));assert(by('mail-start').disabled&&!by('mail-cancel').disabled);
networkStatus=200;by('mail-network').querySelector('[data-mail-network-retry]').click();await wait(()=>!by('mail-network').querySelector('[data-mail-network-retry]'));assert(by('mail-session').value==='run');assert(by('mail-progress').textContent.includes('4 次分析'));
// A session selection made during refresh wins over the captured old session.
holdNetwork=new Promise(resolve=>{releaseNetwork=resolve;});by('mail-refresh').click();await wait(()=>holdNetwork===null&&by('mail-session').options.length===5);select('mail-session','cancel');await wait(()=>by('mail-progress').textContent.includes('已取消'));releaseNetwork();await new Promise(r=>setTimeout(r,100));assert.equal(by('mail-session').value,'cancel');assert(by('mail-progress').textContent.includes('已取消'));
// An old package's delayed conflict cannot overwrite a newly selected package.
select('mail-session','run');await wait(()=>by('mail-progress').textContent.includes('4 次分析'));networkStatus=409;holdNetwork=new Promise(resolve=>{releaseNetwork=resolve;});by('mail-refresh').click();await wait(()=>holdNetwork===null);networkStatus=200;select('mail-package','q');await wait(()=>by('mail-session').options.length===5&&by('mail-package').value==='q');releaseNetwork();await new Promise(r=>setTimeout(r,100));assert.equal(by('mail-session').value,'');assert.equal(by('mail-progress').textContent,'尚无任务');assert(!by('mail-network').querySelector('[data-mail-network-retry]'));
// Access failures remain errors; they are never labelled as a retryable conflict.
networkStatus=403;by('mail-refresh').click();await wait(()=>by('mail-status').textContent.includes('访问被拒绝'));assert(!by('mail-network').querySelector('[data-mail-network-retry]'));networkStatus=401;by('mail-refresh').click();await wait(()=>by('mail-status').textContent.includes('未通过鉴权'));assert(!by('mail-network').querySelector('[data-mail-network-retry]'));networkStatus=200;select('mail-package','p');await wait(()=>by('mail-session').options.length===5);
select('mail-session','cancel');await wait(()=>by('mail-progress').textContent.includes('已取消'));assert(!by('mail-start').disabled&&by('mail-cancel').disabled);await new Promise(r=>setTimeout(r,1700));assert(by('mail-progress').textContent.includes('已取消'));assert(by('mail-answer').textContent.includes('已取消'));
select('mail-session','failed');await wait(()=>by('mail-progress').textContent.includes('失败'));assert(!by('mail-start').disabled&&by('mail-cancel').disabled);
select('mail-session','complete');await wait(()=>by('mail-answer').textContent==='saved completed answer');assert(by('mail-progress').textContent.includes('已完成'));
assert.equal(calls.filter(c=>c.method==='POST'&&!c.path.endsWith('/presentation')).length,0);
select('mail-session','');await wait(()=>by('mail-progress').textContent==='尚无任务');
// A completed old polling response cannot repopulate a newly created session.
select('mail-session','run');await wait(()=>by('mail-progress').textContent.includes('4 次分析'));
holdTurn=new Promise(resolve=>{releaseTurn=resolve;});await wait(()=>holdTurn===null);
const oldRequests=calls.filter(c=>c.path==='/api/mail/research-turns/run').length;
by('mail-new-session').click();await wait(()=>by('mail-session').value==='new-1');
assert.equal(by('mail-progress').textContent,'尚无任务');assert(!by('mail-start').disabled&&by('mail-cancel').disabled&&by('mail-export').disabled);
releaseTurn();await new Promise(r=>setTimeout(r,1700));
assert.equal(by('mail-session').value,'new-1');assert.equal(by('mail-progress').textContent,'尚无任务');assert.equal(by('mail-answer').textContent,'输入你要查找、比较或提取的内容。');
assert.equal(calls.filter(c=>c.path==='/api/mail/research-turns/run').length,oldRequests);
assert(!by('mail-start').disabled&&by('mail-cancel').disabled&&by('mail-export').disabled);
// A result fetch already begun during restoration is invalidated as well.
holdResult=new Promise(resolve=>{releaseResult=resolve;});select('mail-session','complete');await wait(()=>holdResult===null);
by('mail-new-session').click();await wait(()=>by('mail-session').value==='new-2');releaseResult();await new Promise(r=>setTimeout(r,100));
assert.equal(by('mail-answer').textContent,'输入你要查找、比较或提取的内容。');assert.equal(by('mail-progress').textContent,'尚无任务');
assert(!by('mail-start').disabled&&by('mail-cancel').disabled&&by('mail-export').disabled&&by('mail-filter-result').disabled);
// Delayed failures from the old poll must also leave the new session untouched.
select('mail-session','run');await wait(()=>by('mail-progress').textContent.includes('4 次分析'));
lateTurnError=true;holdTurn=new Promise(resolve=>{releaseTurn=resolve;});await wait(()=>holdTurn===null);
by('mail-new-session').click();await wait(()=>by('mail-session').value==='new-3'&&by('mail-status').textContent.includes('已创建研究会话'));
releaseTurn();await new Promise(r=>setTimeout(r,100));assert(by('mail-status').textContent.includes('已创建研究会话'));assert.equal(by('mail-progress').textContent,'尚无任务');
// Changing the selection during a new session's list refresh must win.
holdSessionList=new Promise(resolve=>{releaseSessionList=resolve;});by('mail-new-session').click();await wait(()=>holdSessionList===null);
select('mail-session','cancel');await wait(()=>by('mail-progress').textContent.includes('已取消'));releaseSessionList();await new Promise(r=>setTimeout(r,100));
assert.equal(by('mail-session').value,'cancel');assert(by('mail-progress').textContent.includes('已取消'));
assert.equal(calls.filter(c=>c.method==='POST'&&!c.path.endsWith('/presentation')).length,4,'only the four explicit new-session clicks may create tasks');
},fs.readFileSync(path.join(root,'static/mail-research.js'),'utf8'));
console.log('PASS: session restoration/refresh, terminal states, new session discards delayed old polling and results');
}finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
