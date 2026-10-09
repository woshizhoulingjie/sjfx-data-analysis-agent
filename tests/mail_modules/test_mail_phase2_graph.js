/* Synthetic transport fixtures exercise the real progressive graph UI. */
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
let chromium;try{chromium=require('playwright').chromium}catch(_){console.log('SKIP: Playwright unavailable');process.exit(0)}
if(!fs.existsSync(chromium.executablePath())){console.log('SKIP: Chromium unavailable');process.exit(0)}
const root=path.resolve(__dirname,'../..');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setContent('<style>body{font-family:Arial,"Microsoft YaHei",sans-serif;padding:24px;background:#f4f7fa;color:#173041}</style><h1>渐进通信图 · 虚构测试</h1><div id="graph"></div>');
  await page.addStyleTag({path:path.join(root,'static/mail-analysis.css')});await page.addStyleTag({path:path.join(root,'static/mail-graph.css')});
  await page.evaluate(()=>{
   window.calls=[];window.pairs=[];window.sources=[];window.invalid=false;
   const n=i=>({address:i===0?'alice@example.com':i===1?'bob@example.com':`contact${i}@example.com`,display_name:i===0?'Alice':i===1?'Bob':`Contact ${i}`,addresses:[i===0?'alice@example.com':i===1?'bob@example.com':`contact${i}@example.com`],total:30-i%20,sent:20,received:10,degree:i===0?500:1});
   const e=i=>({from_address:'alice@example.com',to_address:n(i).address,roles:['to','cc'],recipient_role:'to',message_count:i===1?22:1,role_counts:{to:22,cc:1},first_sent:'2024-05-01',last_sent:'2024-05-07'});
   const data=(u,nodes,edges)=>({progressive:true,api_base:'/api/mail/packages/p/relations/network',package_id:'p',nodes,edges,node_total:501,edge_total:500,manifest:'scope-'+(u.searchParams.get('date_from')||''),view:u.searchParams.get('view')||'mailbox',filters:{date_from:u.searchParams.get('date_from')||null,date_to:u.searchParams.get('date_to')||null,as_of:u.searchParams.get('as_of')||null,issue_id:u.searchParams.get('issue_id')||null}});
   window.initial=data(new URL('http://test/'),Array.from({length:30},(_,i)=>n(i)),Array.from({length:29},(_,i)=>e(i+1)));
   window.fetch=async url=>{
    window.calls.push(url);const u=new URL(url,'http://test'),p=u.pathname;let body;
    if(window.invalid&&!p.endsWith('/overview'))return{ok:false,json:async()=>({ok:false,error:'资料或人物归并已变化，请重新加载图谱'})};
    if(p.endsWith('/overview'))body={network:data(u,Array.from({length:30},(_,i)=>n(i)),Array.from({length:29},(_,i)=>e(i+1)))};
    else if(p.endsWith('/search')){
     if(u.searchParams.get('node_query')==='slow')await new Promise(r=>window.releaseSlow=r);
     body={network:{...data(u,[n(499)],[]),total:1,offset:0,next_cursor:null}};
    }else if(p.endsWith('/neighbors')){const start=Number(u.searchParams.get('cursor')||0),anchor=u.searchParams.get('node');if(anchor==='contact499@example.com')body={network:{...data(u,[n(499),n(0)],[e(499)]),neighbor_total:1,offset:0,next_cursor:null,unexpanded_neighbors:0}};else body={network:{...data(u,[n(0),...Array.from({length:30},(_,i)=>n(start+i+1))],Array.from({length:30},(_,i)=>e(start+i+1))),neighbor_total:500,offset:start,next_cursor:String(start+30),unexpanded_neighbors:500-start-30}};
    }else if(p.endsWith('/edge/messages'))body={items:[{message_key:u.searchParams.get('cursor')?'m2':'m1',subject_raw:u.searchParams.get('cursor')?'Second source':'First source'}],total:22,next_cursor:u.searchParams.get('cursor')?null:'m-next'};
    else if(p.endsWith('/relations/network/issues'))body={items:[{issue_id:'i1',title:'Terms review',link_status:'provisional',review_status:'unresolved',act_count:2,actor_states:[{actor:'alice@example.com',status:'pending_verification',expressed_status:'conditional_accept',object_version:'v2',conditions:[{expression:'after review',object:'terms',object_version:'v2',satisfaction:'unverified'}],gaps:['condition_satisfaction_not_independently_verified'],real_world_completion:'unverified'}]}],total:1,next_cursor:null};
    else if(p.endsWith('/relations/network/issues/i1/timeline'))body={items:[{act_id:'a1',actor:'alice@example.com',act_type:'request',object_version:'v2',semantic_status:'supported',statement:'Please approve v2 after review.',asserted_at:'2024-05-07',conditions:[{expression:'after review',object_version:'v2'}],citations:[{message_key:'m1',quote:'Please approve v2 after review.'}]},{act_id:'a2',actor:'bob@example.com',act_type:'question',object_version:'v2',semantic_status:'insufficient',statement:'Which review is required?',asserted_at:'2024-05-07',citations:[]}],total:2,next_cursor:null};
    else throw new Error('unexpected request '+url);
    return{ok:true,json:async()=>({ok:true,...body})};
   };
  });
  await page.addScriptTag({path:path.join(root,'static/mail-graph.js')});await page.evaluate(()=>window.SJFXMailGraph.render(document.getElementById('graph'),window.initial,{onPair:(a,b,s)=>window.pairs.push({a,b,s}),onMessage:(k,s)=>window.sources.push({k,s})}));
  assert.equal(await page.locator('g[data-address]').count(),30);assert((await page.locator('.mg-note').first().textContent()).includes('全部 501'));
  const coords=()=>page.locator('g[data-address="alice@example.com"] circle').first().evaluate(n=>[n.getAttribute('cx'),n.getAttribute('cy')]);const before=await coords();
  await page.locator('path[role=button]').first().press('Enter');assert((await page.locator('.mg-detail').textContent()).includes('22 封唯一邮件'));
  assert.equal(await page.evaluate(()=>window.calls.length),0);await page.getByRole('button',{name:'分析双方意图',exact:true}).click();assert.equal((await page.evaluate(()=>window.pairs))[0].s.view,'mailbox');
  await page.getByText('回查 22 封关联邮件',{exact:true}).click();await page.getByRole('button',{name:'First source',exact:true}).waitFor();await page.getByRole('button',{name:'继续加载来源邮件',exact:true}).click();await page.getByRole('button',{name:'Second source',exact:true}).click();assert.equal((await page.evaluate(()=>window.sources))[0].k,'m2');
  await page.locator('g[data-address="alice@example.com"]').press('Enter');await page.getByRole('button',{name:'继续展开联系对象',exact:true}).waitFor();assert.equal(await page.locator('g[data-address]').count(),31);
  for(let i=0;i<6;i++){const b=page.getByRole('button',{name:/继续展开联系对象|下一页联系对象/});await b.click();await page.waitForFunction(n=>document.querySelector('.mg-detail').textContent.includes(`已载入 ${n}`),(i+2)*30);}
  assert((await page.locator('g[data-address]').count())<=180);const ns=await page.evaluate(()=>window.calls.filter(u=>u.includes('/neighbors')).map(u=>new URL(u,'http://test').searchParams.get('cursor')));assert(ns.includes('180'));
  await page.getByLabel('搜索全部通信节点').fill('contact499');await page.getByRole('button',{name:'Contact 499',exact:true}).waitFor();assert.equal(await page.locator('g[data-address]').count(),1);await page.getByRole('button',{name:'Contact 499',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('g[data-address]').length===2);
  await page.getByLabel('图谱开始日期').fill('2024-05-03');await page.getByLabel('事项状态截止日期').fill('2024-05-07');await page.getByRole('button',{name:'应用图谱时间',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('g[data-address]').length===30);assert.deepEqual(await coords(),before);
  await page.locator('path[role=button]').first().press('Enter');await page.getByRole('button',{name:'分析双方意图',exact:true}).click();const scope=(await page.evaluate(()=>window.pairs)).at(-1).s;assert.equal(scope.date_from,'2024-05-03');assert.equal(scope.as_of,'2024-05-07');
  await page.getByLabel('图谱关系层').selectOption('issues');await page.locator('.mg-evidence-map').waitFor();assert((await page.locator('.mg-semantic').textContent()).includes('条件满足情况未经独立核实'));assert(!(await page.locator('.mg-semantic').textContent()).includes('[object Object]'));await page.getByRole('button',{name:'查看事项表达时间线',exact:true}).click();await page.getByText('Please approve v2 after review.',{exact:true}).first().waitFor();assert((await page.locator('.mg-detail').textContent()).includes('after review'));assert.equal(await page.locator('.mg-lane').count(),2);await page.getByRole('button',{name:'打开来源邮件',exact:true}).click();const timelineURL=new URL((await page.evaluate(()=>window.calls)).find(u=>u.includes('/issues/i1/timeline')),'http://test');assert.equal(timelineURL.searchParams.get('manifest'),'scope-2024-05-03');assert.equal(timelineURL.searchParams.get('date_from'),'2024-05-03');assert.equal(timelineURL.searchParams.get('as_of'),'2024-05-07');assert(JSON.parse(timelineURL.searchParams.get('nodes')).includes('bob@example.com'));
  if(process.env.SJFX_UI_ARTIFACT_DIR){await page.locator('.mg-detail').evaluate(n=>{const p=document.createElement('p');p.textContent='虚构验证样本：双方表达、v2 版本、条件及原文联动；不含生产邮件。';n.prepend(p);});await page.locator('.mg-detail').screenshot({path:path.join(process.env.SJFX_UI_ARTIFACT_DIR,'phase2-bilateral-issue-timeline-synthetic.png')});}
  await page.getByRole('button',{name:'按此事项查看通信',exact:true}).click();await page.waitForFunction(()=>window.calls.some(u=>u.includes('/overview')&&u.includes('issue_id=i1')));
  await page.getByLabel('搜索全部通信节点').fill('slow');await page.waitForFunction(()=>window.releaseSlow);await page.getByRole('button',{name:'重置视图',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('g[data-address]').length===30);await page.evaluate(()=>window.releaseSlow());await page.waitForTimeout(100);assert.equal(await page.locator('g[data-address]').count(),30);
  await page.evaluate(()=>window.invalid=true);await page.locator('g[data-address="alice@example.com"]').press('Enter');await page.getByRole('button',{name:'重新加载图谱',exact:true}).waitFor();assert((await page.locator('.mg-detail').textContent()).includes('已变化'));
  await page.evaluate(()=>window.invalid=false);await page.getByRole('button',{name:'重新加载图谱',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('g[data-address]').length===30);
  if(process.env.SJFX_UI_ARTIFACT_DIR)await page.screenshot({path:path.join(process.env.SJFX_UI_ARTIFACT_DIR,'phase2-communication-network.png'),fullPage:true});
  assert.deepEqual(errors,[]);console.log('PASS: bounded remote overview/search/neighbor/edge cursor, scope hooks, stable positions, shared issue timeline, stale responses, reload recovery');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
