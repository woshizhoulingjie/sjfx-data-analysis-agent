/* Complete package report first, then an explicitly requested deep analysis. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../..'),script=fs.readFileSync(path.join(root,'static/mail-people.js'),'utf8'),css=fs.readFileSync(path.join(root,'static/mail-people.css'),'utf8');
const shell='<meta charset="utf-8"><style>body{margin:20px;background:#1d2529;color:white}button,input,textarea,select{padding:10px;box-sizing:border-box}button:disabled{opacity:.5}</style><select id="mail-package"><option value="p" selected>Fictional package</option><option value="other">Other package</option></select><button data-mail-module-link="network">People</button><div data-mail-module="network"></div>';
(async()=>{
  const browser=await chromium.launch({headless:true});
  try{
    async function setup(mode='normal'){
      const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',error=>errors.push(error.message));
      await page.route('https://offline.test/reports',route=>route.fulfill({contentType:'text/html',body:shell+'<script>'+script+'</script>'}));
      await page.route('**/static/mail-people.css*',route=>route.fulfill({contentType:'text/css',body:css}));
      await page.addInitScript(({mode})=>{
        const key='sjfx:mail-people:active-v1';
        if(mode!=='normal')sessionStorage.setItem(key,JSON.stringify({packageId:'p',address:'alice@example.com',view:'mailbox',turnId:'deep',analysisMode:'deep_analysis',reportTurnId:'full',filters:{}}));
        sessionStorage.setItem('sjfx_api_token','synthetic-test-token');
        window.reportCalls=[];window.fullState=sessionStorage.getItem('full-completed')?'complete':'running';window.deepState=sessionStorage.getItem('deep-completed')?'complete':'running';window.deepWaiting=true;window.deepStarted=new Date(Date.now()-65000).toISOString();
        const counts={total:1,sent:1,direct:0,cc:0,bcc:0,received:0};
        window.fullMarkdown='# Alice 的完整时间报告\n\n## 按时间整理\n\n1. **2026-09-01**：Alice 根据 Bob 的要求协调交付。[E0001](#e0001)\n\n后续：等待回复。\n\n2. **2026-09-02**：Alice 要求 Carol 确认时间；之后收到回复。[E0002]\n\n未标明依据的 [E999]、[E0003] 保留为文字。\n\n## 附录：原文支撑\n\n### E0001\n邮件主题：交付要求\n原文：\n````text\nPlease coordinate the delivery.\n```text\n### E0002 原文里的伪证据标题\n```\n### E0002 短围栏之后仍是原文\n````\n\n### E0002\n附件原文：Please confirm the date. <img src=x onerror=alert(1)>\n~~~text\n### E0003 原文里的另一伪标题\n~~~\n\n完整文件结束标记';
        const deepMarkdown='# 人物目标深度分析 · 合同附录\n\n## 合同附录的态度与变化\n\n- 表达了推动交付的意图；后续要求确认时间。[E1]\n\n## 附录：原文支撑\n\n### E1\nPlease coordinate the delivery.';
        function result(id){
          const deep=id==='deep',status=deep?window.deepState:window.fullState,invalidSource=['source-coverage-missing','source-binding-missing','source-binding-mismatch','source-turn-missing','source-total-mismatch','report-source-missing','report-binding-mismatch','source-marker-missing'],complete=['complete','scan-inconsistent','progress-inconsistent','report-incomplete'].includes(status)||(deep&&invalidSource.includes(status)),read=complete||status==='flags-inconsistent'?100:20;
          const sourceCoverage={characters_read:status==='progress-inconsistent'?20:status==='source-total-mismatch'?200:100,total_characters:status==='source-total-mismatch'?200:100,messages:10,attachments:3,missing_attachment_text:1,scan_complete:true};
          return {turn_id:id,package_id:'p',address:'alice@example.com',addresses:['alice@example.com'],entity_id:null,analysis_version:3,analysis_mode:deep?'deep_analysis':'full_report',source_report_turn_id:deep&&status!=='source-turn-missing'?'full':null,status:['partial','flags-inconsistent','coverage-inconsistent'].includes(status)?'completed':complete?'completed':'running',
            progress:{phase:deep?'deep_synthesis':complete?'assembly':'reading',...(deep?{coverage_source:status==='source-marker-missing'?undefined:'completed_activity_report',coverage_inherited_from:status==='source-binding-missing'?undefined:status==='source-binding-mismatch'?'other-full':'full',source_scan_complete:status!=='scan-inconsistent',source_coverage:status==='source-coverage-missing'?undefined:sourceCoverage,started_at:window.deepStarted}:{scan_complete:(complete||status==='coverage-inconsistent')&&status!=='scan-inconsistent'}),complete:complete||status==='coverage-inconsistent',characters_read:deep?0:status==='progress-inconsistent'?20:read,batches:deep?0:complete?4:1,model_calls:3,model_call_budget:4096,budget_exhausted:status==='partial',corpus:{messages:10,attachments:3,characters:100},activity:{state:deep&&window.deepWaiting?'waiting_for_model':'running',stage:deep?'deep_synthesis':complete?'assembly':'reading',summary:deep?'正在综合态度与后续':'正在扫描资料包第1批',started_at:new Date(Date.now()-(deep?30000:2000)).toISOString(),generation_started_at:deep&&!window.deepWaiting?new Date(Date.now()-2000).toISOString():undefined,model:deep?'test-deep-model':undefined}},
            report:{title:deep?'关于交付安排的深度分析':'Alice 的完整时间报告',markdown:deep?deepMarkdown:window.fullMarkdown,complete:status!=='report-incomplete'&&(complete||['flags-inconsistent','coverage-inconsistent'].includes(status)),source_complete:false,coverage:{...(deep?{coverage_source:'completed_activity_report',coverage_inherited_from:status==='report-binding-mismatch'?'other-full':'full',source_coverage:status==='report-source-missing'?undefined:{...sourceCoverage,characters_read:status==='coverage-inconsistent'?20:100,total_characters:100}}:{}),characters_read:deep?0:status==='coverage-inconsistent'?20:read,total_characters:100,messages:10,attachments:3,missing_attachment_text:1},limitations:['1 个附件没有可用文本；不能据此确认该附件内容。']},items:[],context_reviews:[],ledger:[],offset:0,limit:5,page_total:0};
        }
        window.fetch=async(raw,options={})=>{
          const url=new URL(raw,location.href),method=options.method||'GET';window.reportCalls.push({path:url.pathname,query:url.search,method,body:options.body,headers:options.headers,credentials:options.credentials});let data;
          if(url.pathname.endsWith('/people'))data={items:[{address:'alice@example.com',display_name:'Alice',...counts},{address:'bob@example.com',display_name:'Bob',...counts}],total:2,offset:0,limit:30};
          else if(url.pathname.endsWith('/person/messages'))data={items:[],role_counts:counts,counts,total:0,offset:0,limit:20};
          else if(url.pathname.endsWith('/person/relationships'))data={items:[],total:0,offset:0,limit:8};
          else if(url.pathname.endsWith('/person/analyses')){
            if(method==='POST'){const body=JSON.parse(options.body);data={turn_id:body.analysis_mode==='deep_analysis'?'deep':'full',status:'queued'};}
            else data={items:sessionStorage.getItem('full-completed')&&url.searchParams.get('address')==='alice@example.com'?[{turn_id:'deep',analysis_mode:'deep_analysis',target:'交付安排',status:window.deepState==='complete'?'completed':'running'},{turn_id:'full',analysis_mode:'full_report',report_complete:true,status:'completed'},{turn_id:'other-full',analysis_mode:'full_report',report_complete:false,status:'failed'}]:[]};
          }else if(url.pathname==='/api/mail/person-analyses/full/report/download'||url.pathname==='/api/mail/person-analyses/deep/report/download')return new Response(url.pathname.includes('/full/')?window.fullMarkdown:deepMarkdown,{status:200,headers:{'Content-Type':'text/markdown; charset=utf-8'}});
          else if(url.pathname==='/api/mail/person-analyses/full/report'){
            if(mode==='race-package'||mode==='race-person')await new Promise(resolve=>window.releaseSourceReport=resolve);
            data={...result('full'),status:'completed',progress:{...result('full').progress,complete:true,scan_complete:true,characters_read:100},report:{...result('full').report,complete:true,coverage:{...result('full').report.coverage,characters_read:100}}};
            if(mode==='foreign-package')data.package_id='other';if(mode==='foreign-person'){data.address='bob@example.com';data.addresses=['bob@example.com'];}if(mode==='partial-source')data.progress.complete=false;
          }else if(url.pathname==='/api/mail/person-analyses/full')data=result('full');
          else if(url.pathname==='/api/mail/person-analyses/deep'){if(window.holdDeepPoll)await new Promise(resolve=>window.releaseDeepPoll=()=>{window.holdDeepPoll=false;resolve();});data=result('deep');}
          else if(url.pathname==='/api/mail/person-analyses/other-full')data={turn_id:'other-full',package_id:'p',address:'alice@example.com',analysis_mode:'full_report',status:'failed',progress:{complete:false},items:[],offset:0,limit:5,page_total:0};
          else throw new Error('Unexpected report endpoint '+url.pathname);
          return {ok:true,status:200,json:async()=>({ok:true,...data})};
        };
      },{mode});
      await page.goto('https://offline.test/reports');return {page,errors};
    }
    const {page,errors}=await setup();await page.evaluate(()=>window.SJFXMailPeople.select('alice@example.com',{q:'delivery',date_from:'2026-09-02'}));
    assert.equal(await page.locator('.mp-report-step textarea,.mp-report-step input[type=checkbox]').count(),0,'the first step has a fixed objective and requires no issue selection or question');
    assert(await page.getByRole('button',{name:'深度分析',exact:true}).isDisabled());
    await page.getByRole('button',{name:'通读整个资料包并生成完整报告',exact:true}).click();
    await page.getByText('正在扫描资料包第1批',{exact:true}).waitFor();
    const firstPost=await page.evaluate(()=>JSON.parse(window.reportCalls.find(call=>call.method==='POST').body));
    assert.deepEqual(firstPost,{address:'alice@example.com',analysis_mode:'full_report',model_call_budget:4096},'the first task neither supplies a user question nor narrows the full package to browsing filters');
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'20');
    assert((await page.locator('.mp-live-progress').innerText()).includes('10 封邮件'),'coverage uses the whole package counts supplied by the backend');
    assert.equal(await page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).count(),0);
    for(const status of ['partial','flags-inconsistent','coverage-inconsistent','scan-inconsistent','progress-inconsistent','report-incomplete']){
      await page.evaluate(async status=>{window.fullState=status;await window.SJFXMailPeople.applyScope({q:'delivery'});},status);
      assert(await page.getByRole('button',{name:'深度分析',exact:true}).isDisabled(),status+' cannot unlock deep analysis');
      assert.equal(await page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).count(),0,status+' cannot be called a complete file');
    }
    await page.evaluate(async()=>{window.fullState='complete';sessionStorage.setItem('full-completed','true');await window.SJFXMailPeople.applyScope({q:'delivery'});});
    assert(!(await page.getByRole('button',{name:'深度分析',exact:true}).isDisabled()));
    const body=page.locator('.mp-complete-report> .mp-report-content');
    assert.equal(await body.locator('h1').innerText(),'Alice 的完整时间报告');assert.equal(await body.locator('ol li').count(),2,'the body shows ordered chronological events');
    assert.deepEqual(await body.locator('ol li').evaluateAll(items=>items.map(item=>item.value)),[1,2],'event numbers survive intervening paragraphs and list breaks');
    assert(!(await body.innerText()).includes('(#e0001)'),'an exact known internal Markdown anchor is consumed completely');
    assert(!(await body.innerText()).includes('Please coordinate the delivery.'),'source quotes are kept out of the timeline body');
    assert.equal(await body.getByRole('button',{name:'[E999]',exact:true}).count(),0,'an unknown evidence identifier cannot become a link');
    assert.equal(await body.getByRole('button',{name:'[E0003]',exact:true}).count(),0,'a heading inside a tilde-fenced original quote cannot create an evidence identifier');
    assert.equal(await page.locator('.mp-complete-report img,.mp-complete-report script').count(),0,'source HTML remains inert text');
    const appendix=page.locator('.mp-complete-report .mp-report-appendix');assert.equal(await appendix.getAttribute('open'),null,'source appendix is folded initially');
    await body.getByRole('button',{name:'[E0002]',exact:true}).click();assert.equal(await appendix.getAttribute('open'),'','a known reference opens its matching source appendix');
    assert.equal(await page.evaluate(()=>document.activeElement.textContent),'### E0002\n','the link focuses the real evidence entry, not a forged heading inside dynamically fenced original text');
    assert.deepEqual(await appendix.locator('pre span').allTextContents(),['### E0001\n','### E0002\n'],'shorter nested fences cannot end the original-text fence or steal the next evidence anchor');
    assert((await appendix.innerText()).includes('<img src=x onerror=alert(1)>'));
    await appendix.locator('summary').click();
    const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).click()]);
    assert.equal(download.suggestedFilename(),'人物完整报告.md');assert.equal(fs.readFileSync(await download.path(),'utf8'),await page.evaluate(()=>window.fullMarkdown),'download includes the complete body and final appendix, without preview truncation');
    const fileCall=await page.evaluate(()=>window.reportCalls.find(call=>call.path.endsWith('/report/download')));assert.equal(fileCall.method,'GET');assert.equal(fileCall.headers['X-SJFX-Token'],'synthetic-test-token');assert.equal(fileCall.credentials,'same-origin');
    await page.getByRole('button',{name:'深度分析',exact:true}).click();await page.getByText('请填写关注的事情、对象或东西，以及深度分析问题',{exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.reportCalls.filter(call=>call.method==='POST').length),1,'a missing target/question cannot create a deep task');
    await page.getByLabel('关注的事情、对象或东西').fill(' 合同附录 ');await page.getByLabel('深度分析问题').fill(' 他对合同附录的态度和意图是什么，后来如何变化？ ');
    await page.evaluate(()=>window.holdDeepPoll=true);
    await page.getByRole('button',{name:'深度分析',exact:true}).click();await page.waitForFunction(()=>typeof window.releaseDeepPoll==='function');
    const queuedDeep=await page.locator('.mp-live-progress').innerText();
    assert(queuedDeep.includes('已有完整事实报告作为依据')&&queuedDeep.includes('继承完整事实报告覆盖'),'queued deep analysis names its verified basis before inherited coverage fields arrive');
    assert(!queuedDeep.includes('逐批阅读')&&!queuedDeep.includes('核查相关原文')&&!queuedDeep.includes('等待阅读与核查'),'queued deep analysis never presents a new reading phase');
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'100','the verified parent supplies inherited coverage before the deep checkpoint arrives');
    assert.equal(await page.getByRole('button',{name:'下载深度分析文件（Markdown）',exact:true}).count(),0,'inherited full coverage cannot mark a queued deep file complete');
    await page.evaluate(()=>window.releaseDeepPoll());await page.getByText('正在综合态度与后续',{exact:true}).waitFor();
    const deepPost=await page.evaluate(()=>JSON.parse(window.reportCalls.filter(call=>call.method==='POST')[1].body));
    assert.equal(deepPost.analysis_mode,'deep_analysis');assert.equal(deepPost.source_report_turn_id,'full');assert.equal(deepPost.target,'合同附录');assert.equal(deepPost.question,'他对合同附录的态度和意图是什么，后来如何变化？');assert.equal(deepPost.address,'alice@example.com');
    const waitingDeep=await page.locator('.mp-live-progress').innerText();
    assert(waitingDeep.includes('基于已完成事实报告分析')&&waitingDeep.includes('模型资源排队已等待')&&waitingDeep.includes('资源可用后开始本轮对象综合'),'deep resource waiting describes synthesis of the completed facts');
    assert(!waitingDeep.includes('开始处理本批邮件')&&!waitingDeep.includes('本次模型处理已用时'),'queued model resources are not presented as another reading pass or admitted processing');
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('aria-label'),'继承的完整事实报告覆盖');
    assert((await page.locator('.mp-deep-report .mp-report-coverage').innerText()).includes('继承完整事实报告覆盖：100 / 100 字'));
    assert(!(await page.locator('.mp-deep-report').innerText()).includes('全量可用文本扫描'),'deep preview distinguishes inherited coverage from a new scan');
    await page.evaluate(async()=>{window.deepWaiting=false;await window.SJFXMailPeople.applyScope({});});
    const processingDeep=await page.locator('.mp-live-progress').innerText();
    assert(processingDeep.includes('模型调用中')&&processingDeep.includes('更新综合结果'),'deep synthesis reports actual admitted model work');
    assert(/本次模型处理已用时 [23]秒/.test(processingDeep),'deep processing clock uses generation admission time, not the 30-second queue wait');
    const deepElapsed=await page.locator('.mp-elapsed').innerText();await page.waitForFunction(previous=>document.querySelector('.mp-elapsed').textContent!==previous,deepElapsed);
    assert((await page.locator('.mp-deep-step > .mp-notice').innerText()).includes('本轮对象综合及文件尚未完成'),'a running deep task stays incomplete despite inherited 100-percent coverage');
    const deepBody=page.locator('.mp-deep-report> .mp-report-content');assert.equal(await deepBody.locator('h1').innerText(),'人物目标深度分析 · 合同附录');assert.equal(await deepBody.locator('h2').innerText(),'合同附录的态度与变化');assert(await deepBody.locator('li').isVisible(),'a target or normal analysis heading containing 附录 never folds the actual report body');
    assert.equal(await page.locator('.mp-deep-report .mp-report-appendix').getAttribute('open'),null,'only the actual second-level source appendix remains folded');
    assert.equal(await page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).count(),1,'the completed basis file remains available during deep analysis');
    assert(await page.getByRole('button',{name:'深度分析',exact:true}).isDisabled(),'the running deep task blocks a duplicate submission');
    for(const status of ['flags-inconsistent','coverage-inconsistent','scan-inconsistent','progress-inconsistent','report-incomplete','source-coverage-missing','source-binding-missing','source-binding-mismatch','source-turn-missing','source-total-mismatch','report-source-missing','report-binding-mismatch','source-marker-missing']){
      await page.evaluate(async status=>{window.deepState=status;await window.SJFXMailPeople.applyScope({});},status);
      assert.equal(await page.getByRole('button',{name:'下载深度分析文件（Markdown）',exact:true}).count(),0,status+' cannot release a deep file');
      assert(!(await page.locator('.mp-live-progress').innerText()).includes('文件已生成'),status+' cannot claim the deep file is complete');
    }
    await page.evaluate(async()=>{window.deepState='complete';sessionStorage.setItem('deep-completed','true');await window.SJFXMailPeople.applyScope({});});
    await page.getByRole('button',{name:'下载深度分析文件（Markdown）',exact:true}).waitFor();
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'100','completed deep coverage comes from source_coverage despite zero raw scan counts');
    const deepCheckpoint=await page.evaluate(async()=>await (await window.fetch('/api/mail/person-analyses/deep')).json());
    assert.equal(deepCheckpoint.progress.characters_read,0);assert.equal(deepCheckpoint.progress.batches,0);assert.equal(deepCheckpoint.progress.scan_complete,undefined,'the deep checkpoint does not invent a raw scan completion flag');assert.equal(deepCheckpoint.report.coverage.characters_read,0,'the report retains honest raw counts');
    await page.reload();await page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).waitFor();await page.getByRole('button',{name:'下载深度分析文件（Markdown）',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.reportCalls.filter(call=>call.method==='POST').length),0,'refresh restores both files and the existing deep turn without starting work');
    assert.equal(await page.evaluate(()=>window.reportCalls.filter(call=>call.path==='/api/mail/person-analyses/full/report').length),1,'deep refresh restores the exact completed basis report');
    await page.getByLabel('查看此前报告与分析（最近20次）').selectOption('other-full');
    await page.waitForFunction(()=>document.querySelector('.mp-live-progress').dataset.status==='failed');
    assert.equal(await page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).count(),0,'a different failed report never displays the previously completed file');
    assert(await page.getByRole('button',{name:'深度分析',exact:true}).isDisabled());
    await page.getByLabel('查看此前报告与分析（最近20次）').selectOption('deep');await page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).waitFor();
    assert((await page.locator('.mp-live-progress').innerText()).includes('文件已生成'),'changing history restores the parent binding before rendering the completed deep stages');
    const output=path.resolve(root,'../../outputs');fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'person-report-ui-v3-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'complete report and controls fit the mobile viewport');await page.screenshot({path:path.join(output,'person-report-ui-v3-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);await page.close();
    for(const mode of ['foreign-package','foreign-person','partial-source','race-package','race-person']){
      const {page,errors}=await setup(mode);
      if(mode.startsWith('race-')){
        await page.waitForFunction(()=>typeof window.releaseSourceReport==='function');
        if(mode==='race-package')await page.locator('#mail-package').selectOption('other');else await page.evaluate(()=>window.SJFXMailPeople.select('bob@example.com'));
        await page.evaluate(()=>window.releaseSourceReport());await page.waitForTimeout(100);
        assert.equal(await page.locator('.mp-complete-report h4').filter({hasText:'Alice 的完整时间报告'}).count(),0,'a delayed basis report cannot overwrite the newly selected package or person');
      }else{
        await page.getByText(/未能恢复完整报告/).waitFor();assert(await page.getByRole('button',{name:'深度分析',exact:true}).isDisabled(),mode+' is not a usable basis report');
        assert.equal(await page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).count(),0);
      }
      assert.equal(await page.evaluate(()=>window.reportCalls.filter(call=>call.method==='POST').length),0,'restoration and rejection never enqueue work');assert.deepEqual(errors,[]);await page.close();
    }
    console.log('mail people reports: fixed full-package scan, inherited deep coverage with truthful completion gates and model clocks, chronological safe preview and source references, exact authenticated file download, explicit deep question, refresh/history and source restoration races passed');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
