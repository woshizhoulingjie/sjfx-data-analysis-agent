// Offline browser regression using the real people script and mocked HTTP.
// No mail server, external model, or source mailbox is contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { chromium = require('playwright').chromium; }
catch (_) { console.log('SKIP: playwright is unavailable'); process.exit(0); }
if (!fs.existsSync(chromium.executablePath())) {
  console.log('SKIP: chromium is unavailable'); process.exit(0);
}
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setContent('<select id="mail-package"><option value="p" selected>Package</option></select><button data-mail-module-link="network">People</button><div data-mail-module="network"></div>');
    await page.evaluate(() => {
      window.calls = []; window.opened = []; window.scopeEvents = 0;window.budgetPaused=false;window.budgetContinued=false;window.budgetCeiling=false;
      document.addEventListener('sjfx:mail-scope', () => window.scopeEvents++);
      window.SJFXMailFoundation = {openMessage: (key, filters) => window.opened.push({key, filters})};
      const counts = {total: 2, sent: 1, direct: 1, cc: 0, bcc: 0, received: 1};
      const citation = n => ({message_key: 'm'+n, quote: 'Verified source '+n, sent_at: '2026-09-10T08:00:00+00:00', authored: false});
      window.fetch = async (raw, options={}) => {
        const url = new URL(raw, 'https://offline.test');
        window.calls.push({path: url.pathname, query: url.search, method: options.method || 'GET', body: options.body});
        let data;
        if (url.pathname.endsWith('/people')) data = {items: [{address: 'a@example.com', addresses: ['a@example.com', 'alias@example.com'], entity_id: 'entity-a', display_name: 'Alice', ...counts}], total: 1, offset: 0, limit: 30};
        else if (url.pathname.endsWith('/person/messages')) data = {items: [], role_counts: counts, counts, total: 0, offset: 0, limit: 20};
        else if (url.pathname.endsWith('/person/relationships')) data = {items: [], total: 0, offset: 0, limit: 8};
        else if (url.pathname.endsWith('/person/analyses')) data = options.method === 'POST' ? {turn_id: 'turn-v2', job_id: 'j'} : {items: []};
        else if (url.pathname === '/api/mail/person-analyses/turn-v2/resume') {window.budgetPaused=false;window.budgetContinued=true;data={turn_id:'turn-v2',job_id:'continued-job'};}
        else if (url.pathname === '/api/mail/person-analyses/turn-v2') {
          const offset = Number(url.searchParams.get('offset') || 0), limit = 5;
          const items = offset < 2 ? [{finding_id: 'finding-'+offset, label: '模型解读', statement: 'Source finding '+offset, uncertainty: '仍需核实', review_status: 'conditional', conditions: ['Source condition '+offset], contradictions: [], unresolved_questions: [], citations: [citation(offset)]}] : [];
          const audits = Array.from({length: Math.min(limit, 12-offset)}, (_, i) => ({reviews: [{finding_id: 'finding-0', status: 'conditional', rationale: 'Audit '+(offset+i), conditions: ['Audit condition '+(offset+i)], contradictions: ['Audit contradiction '+(offset+i)], unresolved_questions: ['Unresolved '+(offset+i)], citations: [citation(offset+i)], counterevidence: [citation(offset+i)], later_replies: [citation(offset+i)],guard_context:[citation(offset+i)]}]}));
          data = {status: window.budgetContinued?'running':'completed', progress: {complete: !window.budgetPaused&&!window.budgetContinued, budget_exhausted:window.budgetPaused,model_calls:window.budgetCeiling?32768:window.budgetPaused||window.budgetContinued?1024:8, model_call_budget: window.budgetCeiling?32768:window.budgetContinued?2048:1024, ledger_counts: {facts: 2, conditions: 1, contradictions: 1, unresolved_questions: 1}, corpus: {characters: 80, partial_attachment_text: 1, attribution_limited_body_messages: 1}, synthesis: {themes: [{title: 'Conditional theme', statement: 'Conclusion remains conditional', uncertainty: '需核实条件', review_status: 'conditional', evidence_refs: [{finding_id: 'finding-1', source_batch_offset: 1, review_status: 'conditional'}]}]}}, items, context_reviews: audits,
            ledger: [{kind: 'conditions', finding_id: 'finding-0', statement: 'Ledger condition '+offset, citations: [citation(offset)]}],
            offset, limit, batch_total: 2, context_review_total: 12, page_total: 12};
        } else throw new Error('Unexpected endpoint: '+url.pathname);
        return {ok: true, status: 200, json: async () => ({ok: true, ...data})};
      };
    });
    await page.addScriptTag({path: path.resolve(__dirname, '../../static/mail-people.js')});
    await page.evaluate(() => window.SJFXMailPeople.select({address: 'a@example.com', addresses: ['a@example.com', 'alias@example.com'], entity_id: 'entity-a', display_name: 'Alice'}, {q: 'plan', has_attachment: 'false'}));
    await page.getByText('处理上限与继续设置',{exact:true}).click();
    assert.equal(await page.getByLabel('本次分析调用上限（1–32768）').inputValue(),'4096','new analyses use the larger default');
    await page.getByLabel('本次分析调用上限（1–32768）').fill('1024');
    await page.getByRole('button', {name: '通读整个资料包并生成完整报告', exact: true}).click();
    await page.getByText('Conditional theme', {exact: true}).waitFor();
    const initial = await page.locator('.mp-content').innerText();
    assert(initial.includes('存在条件') && initial.includes('台账：'), 'theme status and ledger summary must be visible');
    assert(initial.includes('部分可用') && initial.includes('表达归属仍不确定'), 'coverage limits remain visible');
    await page.getByText('逐批发现、原文证据与上下文核查',{exact:true}).click();
    await page.getByText('本页事实、条件、矛盾与待确认问题台账', {exact: true}).click();
    await page.getByText('条件：Ledger condition 0', {exact: true}).waitFor();
    await page.getByRole('button', {name: '打开台账来源邮件', exact: true}).click();
    await page.locator('summary').filter({hasText: '存在条件 · Audit 0'}).click();
    assert((await page.locator('.mp-content').innerText()).includes('Audit contradiction 0'), 'audit changes are readable');
    assert((await page.locator('.mp-content').innerText()).includes('尚需解释的条件或变化原文'), 'automatically guarded changes are visible');
    await page.getByText('查看支撑这一主题的原文', {exact: true}).click();
    await page.getByRole('button', {name: '查看支撑发现 1', exact: true}).click();
    await page.getByText('Source finding 1', {exact: true}).waitFor();
    const analysis = page.locator('.mp-analysis');
    await analysis.getByRole('button', {name: '下一页', exact: true}).click();
    await analysis.getByText('7–11 / 12', {exact: true}).waitFor();
    await analysis.getByRole('button', {name: '下一页', exact: true}).click();
    await analysis.getByText('12–12 / 12', {exact: true}).waitFor();
    assert(await analysis.getByRole('button', {name: '下一页', exact: true}).isDisabled(), 'audit pagination ends at total');
    assert(!(await analysis.getByRole('button', {name: '上一页', exact: true}).isDisabled()), 'audit-only pages can return');
    await page.evaluate(async()=>{window.budgetPaused=true;await window.SJFXMailPeople.applyScope({q:'plan',has_attachment:'false'});});
    const resume=page.getByRole('button',{name:'提高上限并从已保存进度继续',exact:true});await resume.waitFor();
    await page.getByLabel('本次分析调用上限（1–32768）').fill('1024');await resume.click();
    await page.getByText('继续分析的调用上限需高于 1024 且不超过32768',{exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/resume')).length),0,'equal budget must be rejected before submission');
    await page.getByLabel('本次分析调用上限（1–32768）').fill('2048');await resume.click();
    await page.waitForFunction(()=>window.budgetContinued);
    await page.evaluate(async()=>{window.budgetPaused=true;window.budgetContinued=false;window.budgetCeiling=true;await window.SJFXMailPeople.applyScope({q:'plan',has_attachment:'false'});});
    assert(await page.getByRole('button',{name:'提高上限并从已保存进度继续',exact:true}).isDisabled(),'32768 is the maximum continuation ceiling');
    assert((await page.locator('.mp-content').innerText()).includes('已达到最大调用上限'),'ceiling explains why further continuation is disabled');
    const result = await page.evaluate(() => ({calls: window.calls, opened: window.opened, scopeEvents: window.scopeEvents}));
    const submitted = result.calls.find(c => c.path.endsWith('/person/analyses') && c.method === 'POST');
    assert.deepEqual(JSON.parse(submitted.body), {address: 'a@example.com', entity_id: 'entity-a', analysis_mode: 'full_report', model_call_budget: 1024});
    assert.deepEqual(JSON.parse(result.calls.find(c=>c.path.endsWith('/resume')).body),{model_call_budget:2048},'budget continuation modifies only the limit');
    assert.equal(result.calls.filter(c=>c.path.endsWith('/person/analyses')&&c.method==='POST').length,1,'continuation never creates a replacement analysis');
    assert(result.calls.some(c => c.path.endsWith('/turn-v2') && c.query.includes('offset=1')), 'theme reference reaches its source batch');
    assert(result.calls.some(c => c.path.endsWith('/turn-v2') && c.query.includes('offset=11')), 'audit pages beyond evidence batches remain reachable');
    assert.deepEqual(result.opened[0], {key: 'm0', filters: {package_id: 'p', q: 'plan', has_attachment: 'false'}});
    assert.equal(result.scopeEvents, 0, 'selecting or reading never emits a feedback scope event');
    assert.deepEqual(errors, []);
    console.log('mail people: v2 entity submission, theme references, review ledger, coverage, original links and audit pagination passed');
    const live = await browser.newPage();
    const liveErrors=[];live.on('pageerror',error=>liveErrors.push(error.message));
    const shell='<meta charset="utf-8"><select id="mail-package"><option value="p" selected>Package</option><option value="other">Other Package</option></select><button data-mail-module-link="network">People</button><div data-mail-module="network"></div>';
    const script=fs.readFileSync(path.resolve(__dirname,'../../static/mail-people.js'),'utf8');
    await live.route('https://offline.test/people-shell',route=>route.fulfill({contentType:'text/html',body:shell+'<script>'+script+'</script>'}));
    await live.route('**/static/mail-people.css*',route=>route.fulfill({contentType:'text/css',body:''}));
    await live.addInitScript(()=>{
      const counts={total:3,sent:2,direct:1,cc:0,bcc:0,received:1};window.liveCalls=[];window.failPoll=false;window.modelWaiting=!sessionStorage.getItem('mock-model-admitted');
      window.fetch=async(raw,options={})=>{
        const url=new URL(raw,location.href);window.liveCalls.push({path:url.pathname,query:url.search,method:options.method||'GET'});let data;
        if(url.pathname.endsWith('/people'))data={items:[{address:'a@example.com',display_name:'Alice',...counts}],total:1,offset:0,limit:30};
        else if(url.pathname.endsWith('/person/messages'))data={items:[],role_counts:counts,counts,total:0,offset:0,limit:20};
        else if(url.pathname.endsWith('/person/relationships'))data={items:[],total:0,offset:0,limit:8};
        else if(url.pathname.endsWith('/person/analyses')){
          if(options.method==='POST'){await new Promise(resolve=>window.releaseSubmit=resolve);sessionStorage.setItem('mock-live-status','running');data={turn_id:'turn-live',status:'queued'};}
          else data={items:sessionStorage.getItem('mock-live-status')?[{turn_id:'turn-live',created_at:'2026-10-08T03:00:00Z',status:sessionStorage.getItem('mock-live-status')}]:[]};
        }else if(url.pathname==='/api/mail/research-turns/turn-live/cancel'){sessionStorage.setItem('mock-live-status','cancelled');data={};}
        else if(url.pathname==='/api/mail/person-analyses/turn-live/resume'){sessionStorage.setItem('mock-live-status','running');data={turn_id:'turn-live'};}
        else if(url.pathname==='/api/mail/person-analyses/turn-live'){
          if(window.failPoll)throw new Error('Temporary connection failure');
          const status=sessionStorage.getItem('mock-live-status')||'running',now=new Date().toISOString();
          data={turn_id:'turn-live',status,created_at:new Date(Date.now()-65000).toISOString(),updated_at:now,
            progress:{started_at:new Date(Date.now()-65000).toISOString(),phase:'context_review',batches:2,characters_read:60,findings_count:1,model_calls:3,model_call_budget:4096,corpus:{messages:3,attachments:1,characters:100},context_review:{batches_read:1},complete:false,
              activity:{state:window.modelWaiting?'waiting_for_model':'running',stage:'context_review',summary:window.activitySummary||'核查第2批后续回复 <img src=x onerror=alert(1)>',started_at:new Date(Date.now()-30000).toISOString(),generation_started_at:window.modelWaiting?undefined:new Date(Date.now()-2000).toISOString(),updated_at:now,model:'test-model',batch_index:1,message_count:2,characters:40,range:{first_sent:'2026-09-01',last_sent:'2026-09-03',subjects:['Payment plan','Latest response']}},
              events:Array.from({length:12},(_,i)=>({at:now,stage:'reading',summary:'已保存实际批次 '+i}))},
            items:[{finding_id:'live-finding',label:'模型解读',statement:'已保存的阶段发现',uncertainty:'仍需核查',citations:[]}],context_reviews:[],ledger:[],offset:0,limit:5,batch_total:2,page_total:2};
        }else throw new Error('Unexpected live endpoint '+url.pathname);
        return {ok:true,status:200,json:async()=>({ok:true,...data})};
      };
    });
    await live.goto('https://offline.test/people-shell');
    await live.evaluate(()=>window.SJFXMailPeople.select('a@example.com'));
    assert.equal(await live.getByRole('button',{name:'通读整个资料包并生成完整报告',exact:true}).count(),1,'live shell renders the analysis controls: '+liveErrors.join(';')+' '+await live.locator('body').innerText());
    await live.getByRole('button',{name:'通读整个资料包并生成完整报告',exact:true}).click();
    await live.getByText('正在提交分析任务',{exact:true}).waitFor();
    assert.equal(await live.locator('.mp-live-progress progress').count(),0,'no invented coverage exists before the corpus is fixed');
    const submittingElapsed=await live.locator('.mp-elapsed').innerText();
    await live.waitForFunction(previous=>document.querySelector('.mp-elapsed').textContent!==previous,submittingElapsed);
    await live.evaluate(()=>window.releaseSubmit());
    await live.getByText('等待调用模型：test-model',{exact:true}).waitFor();
    const queuedText=await live.locator('.mp-live-progress').innerText();
    assert(queuedText.includes('等待模型资源')&&queuedText.includes('模型资源排队已等待'),'shared model resource waiting is explicitly visible');
    assert(!queuedText.includes('模型调用中')&&!queuedText.includes('本次模型处理已用时'),'resource queueing is never presented as model processing');
    await live.reload();
    await live.locator('.mp-live-wait').filter({hasText:'模型资源排队已等待'}).waitFor();
    assert.equal(await live.evaluate(()=>window.liveCalls.filter(call=>call.method==='POST'&&call.path.endsWith('/person/analyses')).length),0,'a queued turn restores across refresh without duplicate submission');
    await live.evaluate(async()=>{window.modelWaiting=false;sessionStorage.setItem('mock-model-admitted','true');await window.SJFXMailPeople.applyScope({});});
    const liveText=await live.locator('.mp-live-progress').innerText();
    assert(liveText.includes('当前第 2 批 · 2 封邮件 · 40 字')&&liveText.includes('Payment plan'),'the real active batch range is visible');
    assert(liveText.includes('模型调用中')&&liveText.includes('本次模型处理已用时'),'admitted model processing has a visible clock');
    assert(/本次模型处理已用时 [23]秒/.test(liveText),'processing elapsed uses actual admission time instead of the 30-second queue reservation');
    assert.equal(await live.locator('.mp-live-progress img').count(),0,'activity is rendered as inert text');
    assert.equal(await live.locator('.mp-stage-events li').count(),8,'recent stage events stay bounded');
    assert.equal(await live.locator('.mp-live-progress progress').getAttribute('value'),'60','coverage is the actual saved reading count');
    const runningElapsed=await live.locator('.mp-elapsed').innerText();
    await live.waitForFunction(previous=>document.querySelector('.mp-elapsed').textContent!==previous,runningElapsed);
    await live.evaluate(async()=>{window.activitySummary='新活动已发布，逐批发现未变化';await window.SJFXMailPeople.applyScope({});});
    await live.getByText('新活动已发布，逐批发现未变化',{exact:true}).waitFor();
    await live.evaluate(async()=>{window.failPoll=true;await window.SJFXMailPeople.applyScope({});});
    await live.getByText(/5秒后重新连接/).waitFor();
    assert((await live.locator('.mp-findings').textContent()).includes('已保存的阶段发现'),'connection failure preserves saved findings inside the evidence disclosure');
    await live.evaluate(()=>window.failPoll=false);
    await live.waitForFunction(()=>!document.querySelector('.mp-live-connection').dataset.failed);
    await live.reload();
    await live.getByText('实际调用模型：test-model',{exact:true}).waitFor();
    assert.equal(await live.evaluate(()=>window.liveCalls.filter(call=>call.method==='POST'&&call.path.endsWith('/person/analyses')).length),0,'refresh restores the existing turn without submitting another');
    await live.getByRole('button',{name:'停止分析',exact:true}).click();
    await live.getByText('分析已停止，阶段结果已保留',{exact:true}).waitFor();
    assert((await live.locator('.mp-live-activity').innerText()).includes('停止前最后活动'),'terminal status overrides a stale running checkpoint activity');
    assert(!(await live.locator('.mp-live-activity').innerText()).includes('模型调用中'),'a cancelled turn is not shown as still running');
    const stoppedElapsed=await live.locator('.mp-elapsed').innerText();
    await live.waitForTimeout(1100);
    assert.equal(await live.locator('.mp-elapsed').innerText(),stoppedElapsed,'the elapsed clock freezes after stopping');
    await live.reload();
    await live.getByRole('button',{name:'从已保存进度继续',exact:true}).waitFor();
    assert((await live.locator('.mp-findings').textContent()).includes('已保存的阶段发现'),'stopped results survive refresh');
    await live.getByRole('button',{name:'从已保存进度继续',exact:true}).click();
    await live.locator('.mp-live-activity').filter({hasText:'模型调用中'}).waitFor();
    assert.equal(await live.evaluate(()=>window.liveCalls.filter(call=>call.method==='POST'&&call.path.endsWith('/resume')).length),1,'continuation resumes the restored turn');
    assert.deepEqual(liveErrors,[]);
    await live.close();
    console.log('mail people: immediate submission, model queue/admission clocks, live activity, real coverage, changed events, reconnect, refresh and stop/resume passed');
    for(const mode of ['manual-person','manual-package','scope-package','scope-person']){
      const restored=await browser.newPage(),restoreErrors=[];restored.on('pageerror',error=>restoreErrors.push(error.message));
      await restored.route('https://offline.test/restore-shell',route=>route.fulfill({contentType:'text/html',body:shell+'<script>'+script+'</script>'}));
      await restored.route('**/static/mail-people.css*',route=>route.fulfill({contentType:'text/css',body:''}));
      await restored.addInitScript(({mode})=>{
        sessionStorage.setItem('sjfx:mail-people:active-v1',JSON.stringify({packageId:'p',address:'a@example.com',entityId:'',view:'mailbox',turnId:'saved-turn',batchOffset:0,filters:{}}));
        window.restoreCalls=[];window.activeRestoreFetches=0;let contactRequests=0;
        const counts={total:1,sent:1,direct:0,cc:0,bcc:0,received:0};
        window.fetch=async(raw,options={})=>{
          const url=new URL(raw,location.href);window.restoreCalls.push({path:url.pathname,query:url.search});window.activeRestoreFetches++;let data;
          try{
            if(url.pathname.endsWith('/people')){
              if(++contactRequests===1&&mode.startsWith('manual-'))await new Promise(resolve=>window.releaseRestoreContacts=()=>{window.restoreContactsReleased=true;resolve();});
              data={items:[{address:'a@example.com',display_name:'Alice',...counts},{address:'b@example.com',display_name:'Bob',...counts}],total:2,offset:0,limit:30};
            }else if(url.pathname.endsWith('/person/messages'))data={items:[],role_counts:counts,counts,total:0,offset:0,limit:20};
            else if(url.pathname.endsWith('/person/relationships'))data={items:[],total:0,offset:0,limit:8};
            else if(url.pathname.endsWith('/person/analyses'))data={items:[]};
            else if(url.pathname==='/api/mail/person-analyses/saved-turn'){
              const address=mode==='scope-person'?'b@example.com':'a@example.com';
              data={turn_id:'saved-turn',package_id:mode==='scope-package'?'other':'p',address,addresses:[address],entity_id:null,status:'completed',progress:{complete:true,corpus:{characters:1}},items:[{finding_id:'foreign-finding',statement:'跨范围发现不得展示',citations:[]}],context_reviews:[],ledger:[],offset:0,limit:5,page_total:1};
            }else throw new Error('Unexpected restoration endpoint '+url.pathname);
            return {ok:true,status:200,json:async()=>({ok:true,...data})};
          }finally{window.activeRestoreFetches--;}
        };
      },{mode});
      await restored.goto('https://offline.test/restore-shell');
      if(mode.startsWith('manual-')){
        await restored.waitForFunction(()=>typeof window.releaseRestoreContacts==='function');
        if(mode==='manual-person')await restored.evaluate(()=>window.SJFXMailPeople.select({address:'b@example.com',display_name:'Bob'}));
        else await restored.locator('#mail-package').selectOption('other');
        await restored.evaluate(()=>window.releaseRestoreContacts());
        await restored.waitForFunction(()=>window.restoreContactsReleased&&window.activeRestoreFetches===0);
        assert.equal(await restored.evaluate(()=>window.restoreCalls.filter(call=>call.path==='/api/mail/person-analyses/saved-turn').length),0,mode+' invalidates delayed turn restoration');
        if(mode==='manual-person')assert.equal(await restored.locator('.mp-person-title').innerText(),'Bob','delayed saved Alice never overwrites the user-selected Bob');
        else {
          assert.equal(await restored.locator('.mp-person-title').count(),0,'delayed saved Alice never attaches to the newly selected package');
          assert.equal(await restored.evaluate(()=>window.SJFXMailPeople.scope().package_id),'other','the new package stays selected');
        }
      }else{
        await restored.getByText('已保存分析属于其他资料包或人物，已停止恢复。请重新选择当前人物的历史分析。',{exact:true}).waitFor();
        assert(!(await restored.locator('.mp-content').innerText()).includes('跨范围发现不得展示'),mode+' rejects mismatched saved turn evidence');
        assert(!(await restored.getByRole('button',{name:'通读整个资料包并生成完整报告',exact:true}).isDisabled()),'scope mismatch allows a new correctly scoped analysis');
      }
      assert.deepEqual(restoreErrors,[]);await restored.close();
    }
    console.log('mail people: delayed refresh restoration never overrides manual person/package selection; mismatched turn scopes are rejected');
    for(const mode of ['async-running','async-completed','async-missing','async-manual-package','async-manual-person']){
      const refreshed=await browser.newPage(),refreshErrors=[];refreshed.on('pageerror',error=>refreshErrors.push(error.message));
      const emptyShell='<meta charset="utf-8"><select id="mail-package"><option value="">Choose package</option></select><button data-mail-module-link="network">People</button><div data-mail-module="package">Home</div><div data-mail-module="network" hidden></div>';
      await refreshed.route('https://offline.test/async-shell',route=>route.fulfill({contentType:'text/html',body:emptyShell+'<script>'+script+'</script>'}));
      await refreshed.route('**/static/mail-people.css*',route=>route.fulfill({contentType:'text/css',body:''}));
      await refreshed.addInitScript(({mode})=>{
        if(!sessionStorage.getItem('sjfx:mail-people:active-v1'))sessionStorage.setItem('sjfx:mail-people:active-v1',JSON.stringify({packageId:'p',address:'a@example.com',entityId:'entity-a',view:'person',turnId:'saved-async-turn',batchOffset:1,filters:{q:'saved-plan',has_attachment:'false'}}));
        window.asyncCalls=[];window.mainPackageChanges=[];window.activeAsyncFetches=0;
        const counts={total:1,sent:1,direct:0,cc:0,bcc:0,received:0};
        window.fetch=async(raw,options={})=>{
          const url=new URL(raw,location.href);window.asyncCalls.push({path:url.pathname,query:url.search,method:options.method||'GET'});window.activeAsyncFetches++;let data;
          try{
            if(url.pathname==='/api/mail/packages'){
              await new Promise(resolve=>window.releasePackageList=resolve);
              data={packages:[...(mode==='async-missing'?[]:[{package_id:'p',display_name:'Saved package'}]),{package_id:'other',display_name:'Other package'}]};
            }else if(url.pathname.endsWith('/people')){
              if(mode.startsWith('async-manual-'))await new Promise(resolve=>(window.releaseAsyncContacts||(window.releaseAsyncContacts=[])).push(resolve));
              data={items:[{address:'a@example.com',entity_id:'entity-a',display_name:'Alice',...counts},{address:'b@example.com',display_name:'Bob',...counts}],total:2,offset:0,limit:30};
            }else if(url.pathname.endsWith('/person/messages'))data={items:[],role_counts:counts,counts,total:0,offset:0,limit:20};
            else if(url.pathname.endsWith('/person/relationships'))data={items:[],total:0,offset:0,limit:8};
            else if(url.pathname.endsWith('/person/analyses'))data={items:[]};
            else if(url.pathname==='/api/mail/person-analyses/saved-async-turn')data={turn_id:'saved-async-turn',package_id:'p',address:'a@example.com',addresses:['a@example.com'],entity_id:'entity-a',status:mode==='async-completed'?'completed':'running',progress:{phase:'context_review',complete:mode==='async-completed',started_at:new Date(Date.now()-72000).toISOString(),batches:2,characters_read:1,corpus:{characters:1}},items:[{finding_id:'saved-async-finding',statement:'异步刷新恢复的已有结果',citations:[]}],context_reviews:[],ledger:[],offset:1,limit:5,page_total:2};
            else throw new Error('Unexpected async restoration endpoint '+url.pathname);
            return {ok:true,status:200,json:async()=>({ok:true,...data})};
          }finally{window.activeAsyncFetches--;}
        };
        window.SJFXMailLayout={show(key){document.querySelectorAll('[data-mail-module]').forEach(section=>section.hidden=section.dataset.mailModule!==key);}};
        // Match the main module's asynchronous option population and synchronous
        // change -> scope event; it does not publish a package-ready event.
        document.addEventListener('DOMContentLoaded',()=>{
          const select=document.getElementById('mail-package');
          let mainPackage='';window.SJFXMailFoundation={currentScope:()=>({package_id:mainPackage,q:''})};
          select.addEventListener('change',()=>{mainPackage=select.value;window.mainPackageChanges.push(mainPackage);document.dispatchEvent(new CustomEvent('sjfx:mail-scope',{detail:{package_id:mainPackage,q:''}}));});
          (async()=>{const payload=await (await fetch('/api/mail/packages')).json();select.replaceChildren(new Option('Choose package',''));payload.packages.forEach(item=>select.append(new Option(item.display_name,item.package_id)));window.packageListLoaded=true;})();
        });
      },{mode});
      await refreshed.goto('https://offline.test/async-shell');
      assert.equal(await refreshed.locator('#mail-package').inputValue(),'','refresh starts with the actual unpopulated package placeholder');
      assert.equal(await refreshed.locator('.mp-person-title').count(),0,'no person is restored before the source package is available');
      await refreshed.waitForFunction(()=>typeof window.releasePackageList==='function');await refreshed.evaluate(()=>window.releasePackageList());
      await refreshed.waitForFunction(()=>window.packageListLoaded);
      if(mode==='async-missing'){
        assert.equal(await refreshed.locator('#mail-package').inputValue(),'','an unavailable saved package cannot be invented or replaced by another');
        assert.equal(await refreshed.evaluate(()=>window.asyncCalls.filter(call=>call.path!=='/api/mail/packages').length),0,'missing package restoration makes no person or turn requests');
      }else if(mode.startsWith('async-manual-')){
        await refreshed.waitForFunction(()=>window.releaseAsyncContacts?.length>0);
        if(mode==='async-manual-package')await refreshed.locator('#mail-package').selectOption('other');
        else await refreshed.evaluate(()=>window.SJFXMailPeople.select({address:'b@example.com',display_name:'Bob'}));
        await refreshed.evaluate(()=>window.releaseAsyncContacts.splice(0).forEach(resolve=>resolve()));
        await refreshed.waitForFunction(()=>window.activeAsyncFetches===0);
        assert.equal(await refreshed.evaluate(()=>window.asyncCalls.filter(call=>call.path==='/api/mail/person-analyses/saved-async-turn').length),0,'a user selection cancels the delayed async saved turn restoration');
        if(mode==='async-manual-package'){
          assert.equal(await refreshed.locator('#mail-package').inputValue(),'other');assert.equal(await refreshed.locator('.mp-person-title').count(),0);
        }else assert.equal(await refreshed.locator('.mp-person-title').innerText(),'Bob');
      }else{
        await refreshed.locator('.mp-person-title').waitFor();await refreshed.getByText('逐批发现、原文证据与上下文核查',{exact:true}).click();
        await refreshed.getByText('异步刷新恢复的已有结果',{exact:true}).waitFor();
        assert.equal(await refreshed.locator('#mail-package').inputValue(),'p');
        assert.equal(await refreshed.locator('.mp-person-title').innerText(),'a@example.com');
        assert.equal(await refreshed.getByLabel('邮箱或人物视角').inputValue(),'person');
        assert(await refreshed.locator('[data-mail-module="network"]').isVisible(),'refresh returns to the person analysis page');
        assert.deepEqual(await refreshed.evaluate(()=>window.mainPackageChanges),['p'],'restored package uses the main module normal change path exactly once');
        const personRequest=await refreshed.evaluate(()=>window.asyncCalls.find(call=>call.path.endsWith('/person/messages')));
        assert(personRequest.query.includes('entity_id=entity-a')&&personRequest.query.includes('q=saved-plan')&&personRequest.query.includes('has_attachment=false'),'saved person and range survive the main module empty initial scope');
        assert((await refreshed.evaluate(()=>window.asyncCalls.find(call=>call.path==='/api/mail/person-analyses/saved-async-turn'))).query.includes('offset=1'),'the same saved turn and result page are restored');
        await refreshed.reload();await refreshed.waitForFunction(()=>typeof window.releasePackageList==='function');await refreshed.evaluate(()=>window.releasePackageList());
        await refreshed.locator('.mp-person-title').waitFor();await refreshed.getByText('逐批发现、原文证据与上下文核查',{exact:true}).click();
        await refreshed.getByText('异步刷新恢复的已有结果',{exact:true}).waitFor();
        assert(await refreshed.locator('[data-mail-module="network"]').isVisible(),'a second real reload still restores the saved analysis page');
      }
      assert.equal(await refreshed.evaluate(()=>window.asyncCalls.filter(call=>call.method==='POST').length),0,'restoration never submits or resumes an analysis automatically');
      assert.deepEqual(refreshErrors,[]);await refreshed.close();
    }
    console.log('mail people: async package loading and real reload restore the saved person/turn/page; unavailable packages and user selection races stay safe');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
