// First-use acceptance with fictional data and the complete, real mail page.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const root=process.env.SJFX_TEST_ROOT||path.resolve(__dirname,'../..');
const outputs=process.env.SJFX_UI_ARTIFACT_DIR||path.resolve(root,'test-artifacts');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1480,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const html=fs.readFileSync(path.join(root,'templates/mail_research.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,'');
  await page.setContent(html);
  for(const name of ['mail-research.css','mail-analysis.css','mail-graph.css','mail-people.css'])await page.addStyleTag({path:path.join(root,'static',name)});
  await page.evaluate(()=>{
   window.calls=[];
   window.fetch=async(raw,options={})=>{
    const u=new URL(raw,'http://fictional.test'),method=options.method||'GET';
    window.calls.push({path:u.pathname,method,body:options.body});
    let data={};
    if(u.pathname==='/api/mail/packages')data=method==='POST'?{package:{package_id:'p'},job_id:null}:{packages:[{package_id:'p',display_name:'演示资料包（虚构）',status:'ready',logical_message_count:2}],support_matrix:[{format:'eml',available:true}]};
    else if(u.pathname==='/api/mail/workspace-guide')data={version:1,imports:{path_location:'server',formats:[{format:'eml',label:'单封邮件',extensions:['.eml'],available:true,description:'原始邮件'},{format:'zip',label:'压缩包',extensions:['.zip'],available:true,description:'收集 EML 邮件'},{format:'pst',label:'Outlook PST',extensions:['.pst'],available:false,description:'解析支持尚未安装'}]},runtime:{model_available:true,queue_available:true},analysis_modes:[],scope:{fields:[],notes:[]}};
    else if(u.pathname==='/api/mail/packages/p')data={overview:{counts:{logical_messages:2,occurrences:2,messages_with_body:2,explicit_attachments:1,participant_addresses:2}},coverage:{}};
    else if(u.pathname.endsWith('/messages'))data={messages:{items:[{message_key:'m1',subject_raw:'交付日期调整：请确认新方案',sent_at_utc:'2026-10-01T08:00:00+00:00',sender:'Alice <alice@example.test>',attachment_count:1},{message_key:'m2',subject_raw:'回复：同意方案，但需法务批准',sent_at_utc:'2026-10-02T08:00:00+00:00',sender:'Bob <bob@example.test>',attachment_count:0}],total:2},items:[],total:0};
    else if(u.pathname.endsWith('/foundation'))data={foundation:{counts:{logical_messages:2,occurrences:2},stages:{body_index:{status:'completed'},basic_text:{status:'completed'},ocr:{status:'not_started'}},versions:{}}};
    else if(u.pathname.endsWith('/inventory'))data={inventory:{items:[],total:0}};
    else if(u.pathname==='/api/mail/research-sessions')data={sessions:[{session_id:'old',title:'此前的问题'}]};
    else if(u.pathname==='/api/mail/research-sessions/old')data={session:{session_id:'old'},turns:[]};
    else if(u.pathname.endsWith('/relations/communication'))data={communication:{items:[]}};
    else if(u.pathname.endsWith('/relations/threads'))data={threads:[]};
    else if(u.pathname.endsWith('/relations/business'))data={relations:[]};
    else if(u.pathname.endsWith('/relations/network'))data={network:{nodes:[],edges:[]}};
    else if(u.pathname.endsWith('/coverage'))data={coverage:{}};
    else if(u.pathname==='/api/mail/presentation')data={presentation:{status:'completed',values:{},metadata:{}}};
    else if(['/result-sets','/conversations','/attachment-versions','/people'].some(s=>u.pathname.endsWith(s)))data={items:[],total:0,offset:0,limit:30};
    else throw new Error('Unexpected endpoint '+u.pathname);
    return{ok:true,status:200,json:async()=>({ok:true,...data})};
   };
  });
  for(const name of ['mail-analysis','mail-graph','mail-ui','mail-foundation','mail-research','mail-modules','mail-people'])await page.addScriptTag({path:path.join(root,'static',name+'.js')});
  await page.waitForFunction(()=>document.querySelector('#mail-package').options.length===2);
  await page.locator('[data-mail-task="event"]').click();
  assert.equal(await page.locator('#mail-package').inputValue(),'');
  assert((await page.locator('#mail-status').innerText()).includes('选择'));
  await page.locator('#mail-import-shortcut').click();
  assert.equal(await page.locator('#mail-source-path').evaluate(n=>document.activeElement===n),true);
  await page.locator('#mail-package').selectOption('p');
  await page.waitForFunction(()=>document.querySelector('#mail-count').textContent.includes('2'));
  await page.locator('[data-mail-module-link="package"]').click();
  await page.screenshot({path:path.join(outputs,'邮件工作台新版-首页.png'),fullPage:true});
  await page.locator('[data-mail-module-link="research"]').click();
  await page.locator('#mail-session').selectOption('old');
  await page.locator('[data-mail-module-link="package"]').click();
  await page.locator('[data-mail-task="event"]').click();
  assert(await page.locator('[data-mail-module="research"]').isVisible());
  assert(!(await page.locator('#mail-followup').isChecked()),'new event must not inherit old result restriction');
  assert.equal(await page.locator('#mail-session').inputValue(),'','new event prepares a fresh session');
  assert((await page.locator('#mail-question').inputValue()).length>0);
  assert.equal(await page.locator('#mail-question').evaluate(n=>document.activeElement===n),true);
  await page.screenshot({path:path.join(outputs,'邮件工作台新版-查一件事.png'),fullPage:true});
  await page.locator('[data-mail-module-link="package"]').click();
  await page.locator('[data-mail-task="screen"]').click();
  await page.locator('dialog.mi-dialog').waitFor({state:'visible'});
  assert((await page.locator('dialog.mi-dialog').innerText()).includes('逐封'));
  assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.method==='POST'&&!c.path.endsWith('/presentation')).length),0,'task cards must never queue model jobs');
  await page.evaluate(()=>document.querySelector('dialog.mi-dialog').close());
  await page.locator('[data-mail-module-link="package"]').click();
  await page.locator('[data-mail-task="intent"]').click();
  assert(await page.locator('[data-mail-module="messages"]').isVisible());
  await page.locator('[data-mail-module-link="package"]').click();
  await page.locator('[data-mail-task="people"]').click();
  assert(await page.locator('[data-mail-module="network"]').isVisible());
  for(const key of ['package','messages','network','structure','research','answer','results','relations']){
   await page.locator(`[data-mail-module-link="${key}"]`).click();
   assert.equal(await page.locator('[data-mail-module]:visible').count(),1);
   assert(await page.locator(`[data-mail-module="${key}"]`).isVisible());
  }
  await page.locator('#mail-reader-open').click();assert(await page.locator('.mail-inspector').isVisible());
  await page.keyboard.press('Escape');assert(!(await page.locator('.mail-inspector').isVisible()));
  await page.locator('[data-mail-module-link="package"]').click();
  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(()=>!document.querySelector('.mail-navigation').open);
  await page.screenshot({path:path.join(outputs,'邮件工作台新版-手机.png'),fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'mobile document must not overflow horizontally');
  const buttons=await page.locator('.mail-start-grid button').evaluateAll(ns=>ns.map(n=>({width:n.getBoundingClientRect().width,height:n.getBoundingClientRect().height})));
  assert(buttons.every(b=>b.width>=44&&b.height>=44),'task targets accessible on mobile');
  await page.locator('.mail-navigation summary').click();
  await page.locator('[data-mail-module-link="messages"]').click();
  assert(!(await page.locator('.mail-navigation').evaluate(n=>n.open)),'mobile menu closes after selecting a task');
  assert(await page.locator('[data-mail-module="messages"]').isVisible());
  await page.evaluate(()=>window.SJFXMailLayout.show('package'));
  await page.locator('#mail-import-shortcut').click();
  await page.locator('#mail-source-path').fill('/controlled/fictional-mails.zip');
  await page.locator('#mail-package-name').fill('虚构导入验收');
  await page.locator('#mail-import-form').evaluate(n=>n.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
  await page.waitForFunction(()=>window.calls.some(c=>c.path==='/api/mail/packages'&&c.method==='POST'));
  assert.deepEqual(await page.evaluate(()=>JSON.parse(window.calls.find(c=>c.path==='/api/mail/packages'&&c.method==='POST').body)),{source:{path:'/controlled/fictional-mails.zip'},display_name:'虚构导入验收',options:{build_basic_attachment_index:true}});
  assert.deepEqual(errors,[]);
  const receipt={passed:true,fictional_data:true,checks:['first-use requires selected package','six task entry points','server import shortcut and existing POST contract','new question clears previous-result restriction','task entries never enqueue model jobs','eight modules and reader remain usable','mobile collapsed navigation and no document overflow','desktop and mobile screenshots'],page_errors:errors};
  fs.writeFileSync(path.join(outputs,'邮件工作台新版-浏览器验收.json'),JSON.stringify(receipt,null,2));
  console.log(JSON.stringify(receipt));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});


