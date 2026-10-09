const assert=require('node:assert/strict'),path=require('node:path');
let chromium;try{chromium=require('playwright').chromium}catch(_){console.log('SKIP: Playwright unavailable');process.exit(0)}
if(!require('node:fs').existsSync(chromium.executablePath())){console.log('SKIP: Chromium unavailable');process.exit(0)}
const root=path.resolve(__dirname,'../..');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.setContent('<style>body{background:#f4f7fa;font-family:Arial,"Microsoft YaHei",sans-serif;color:#173041;padding:30px}h1{font-size:26px}</style><h1>邮件通信网络 · 虚构验证样本</h1><select id="mail-package"><option value="p">Synthetic</option></select><div id="graph"></div>');
  await page.addStyleTag({path:path.join(root,'static/mail-analysis.css')});
  await page.evaluate(()=>{
    window.opened=[];window.requests=[];window.paused=true;
    window.SJFXMailFoundation={currentScope:()=>({package_id:'p',date_from:'2026-09-01'}),openMessage:(key,scope)=>window.opened.push({key,scope})};
    window.SJFXMailPeople={select:a=>window.selected=a};
    window.fetch=async(url,opts={})=>{
      window.requests.push({url,opts});let value;
      if(opts.method==='POST'){
        if(url.endsWith('/resume'))window.paused=false;
        value={turn_id:'t',job_id:'j',cached:false};
      } else value={status:'completed',progress:{kind:'pair_dossier',phase:window.paused?'reading':'completed',characters_read:200,corpus:{characters:200},model_calls:window.paused?1:3,complete:!window.paused,budget_exhausted:window.paused,issues:window.paused?[]:[{title:'付款提议已撤回',statement:'Alice 撤回提前付款；Bob 未接受提议。',uncertainty:'限于当前邮件记录',review_status:'contradicted',actors:['alice@example.com','bob@example.com']}]},batches:[{findings:[{actor:'alice@example.com',intent:'withdraw',issue:'提前付款',statement:'撤回提议',review_status:'contradicted',uncertainty:'仅限原文',conditions:['先取得法律批准'],citations:[{message_key:'m3',quote:'We withdraw the advance payment proposal.',authored:true,sent_at:'2026-09-03'}]}]}],total:1};
      return {ok:true,json:async()=>({ok:true,...value})};
    };
  });
  await page.addScriptTag({path:path.join(root,'static/mail-analysis.js')});
  await page.addScriptTag({path:path.join(root,'static/mail-graph.js')});
  await page.evaluate(()=>{
   const nodes=Array.from({length:45},(_,i)=>({address:i===0?'alice@example.com':i===1?'bob@example.com':`contact${i}@example.com`,display_name:i===0?'Alice':i===1?'Bob':`联系方 ${i}`,total:Math.max(1,45-i),degree:i===0?44:1,sent:5,received:4}));
   const edges=nodes.slice(1).map((n,i)=>({from_address:nodes[0].address,to_address:n.address,message_keys:['m'+(i+1)],recipient_role:i%4?'to':'cc',first_sent:'2026-09-01',last_sent:'2026-09-03'}));
   window.SJFXMailGraph.render(document.getElementById('graph'),{nodes,edges},{onPerson:n=>window.SJFXMailPeople.select(n.address),onPair:(a,b)=>window.SJFXMailAnalysis.open({kind:'pair',address:a.address,counterpart:b.address,question:'双方付款条件是什么？'}),onMessage:k=>window.SJFXMailFoundation.openMessage(k)});
  });
  assert.equal(await page.locator('g[data-address]').count(),30);
  await page.getByLabel('搜索全部通信节点').fill('contact44');assert.equal(await page.locator('g[data-address]').count(),2);
  await page.getByRole('button',{name:'重置视图',exact:true}).click();
  await page.locator('g[data-address="alice@example.com"]').press('Enter');assert.equal(await page.locator('g[data-address]').count(),45);
  await page.getByRole('button',{name:'查看人物与全部往来',exact:true}).click();assert.equal(await page.evaluate(()=>window.selected),'alice@example.com');
  await page.getByRole('button',{name:'放大',exact:true}).click();assert((await page.locator('.mg-map svg>g').getAttribute('transform')).includes('scale(1.2)'));
  await page.getByRole('button',{name:'重置视图',exact:true}).click();
  if(process.env.SJFX_UI_ARTIFACT_DIR)await page.screenshot({path:path.join(process.env.SJFX_UI_ARTIFACT_DIR,'communication-network.png'),fullPage:true});
  await page.locator('path[role="button"]').first().press('Enter');
  await page.getByRole('button',{name:'分析双方意图',exact:true}).click();
  await page.getByRole('button',{name:'开始分析',exact:true}).click();
  await page.getByRole('button',{name:'从检查点继续',exact:true}).waitFor();
  assert.equal(await page.getByLabel('模型调用上限').inputValue(),'4096');
  await page.getByRole('button',{name:'从检查点继续',exact:true}).click();
  await page.getByText('付款提议已撤回',{exact:true}).waitFor();
  await page.locator('.mi-finding summary').click();
  await page.getByRole('button',{name:'打开来源邮件',exact:true}).click();
  assert.equal((await page.evaluate(()=>window.opened))[0].key,'m3');
  await page.setViewportSize({width:1440,height:1400});await page.evaluate(()=>document.querySelector('dialog').scrollTop=0);
  if(process.env.SJFX_UI_ARTIFACT_DIR)await page.screenshot({path:path.join(process.env.SJFX_UI_ARTIFACT_DIR,'bilateral-intent.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>JSON.parse(window.requests.find(r=>r.opts.method==='POST').opts.body).scope.date_from),'2026-09-01');
  await page.getByRole('button',{name:'关闭',exact:true}).click();await page.locator('dialog').waitFor({state:'detached'});assert.equal(await page.locator('dialog').count(),0);
  assert.deepEqual(errors,[]);console.log('PASS: graph full search, expansion, zoom, node/edge/source; dossier scope, resume, quotes, close; no page errors');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});

