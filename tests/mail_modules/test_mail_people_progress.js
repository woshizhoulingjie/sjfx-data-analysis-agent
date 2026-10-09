/* Actual batch work, committed coverage, adaptive retry, and terminal failure. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const script=fs.readFileSync(path.resolve(__dirname,'../../static/mail-people.js'),'utf8');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('https://offline.test/progress',route=>route.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><select id="mail-package"><option value="p" selected>Package</option></select><button data-mail-module-link="network">People</button><div data-mail-module="network"></div><script>'+script+'</script>'}));
    await page.route('**/static/mail-people.css*',route=>route.fulfill({contentType:'text/css',body:''}));
    await page.addInitScript(()=>{
      window.calls=[];window.failPoll=false;
      const started=new Date(Date.now()-240000).toISOString();
      window.task={turn_id:'full',package_id:'p',address:'alice@example.com',addresses:['alice@example.com'],analysis_version:3,analysis_mode:'full_report',status:'running',created_at:started,updated_at:new Date().toISOString(),error:null,
        progress:{phase:'reading',started_at:started,characters_read:0,batches:0,findings_count:0,complete:false,scan_complete:false,model_calls:1,model_call_budget:4096,corpus:{messages:26,attachments:5,characters:13166},activity:{stage:'reading',state:'running',summary:'模型读取原文',batch_index:0,characters:3749,message_count:6,model:'test-model',started_at:started,generation_started_at:started,updated_at:started}},
        report:{complete:false,title:'阶段事实报告',markdown:'# 阶段事实报告',coverage:{characters_read:0,total_characters:13166,messages:26,attachments:5}},items:[],offset:0,limit:5,page_total:0};
      window.fetch=async(raw,options={})=>{
        const url=new URL(raw,location.href),method=options.method||'GET';window.calls.push({path:url.pathname,method});let data;
        if(url.pathname.endsWith('/people'))data={items:[{address:'alice@example.com',display_name:'Alice',total:6,sent:1,direct:5,cc:0}],total:1,offset:0,limit:30};
        else if(url.pathname.endsWith('/person/messages'))data={items:[],role_counts:{},counts:{},total:0,offset:0,limit:20};
        else if(url.pathname.endsWith('/person/relationships'))data={items:[],total:0,offset:0,limit:8};
        else if(url.pathname.endsWith('/person/analyses'))data=method==='POST'?{turn_id:'full',status:'queued'}:{items:[]};
        else if(url.pathname==='/api/mail/person-analyses/full/resume'){window.task.status='running';data={turn_id:'full'};}
        else if(url.pathname==='/api/mail/person-analyses/full'){if(window.failPoll)throw new Error('Temporary connection failure');data=window.task;}
        else throw new Error('Unexpected endpoint '+url.pathname);
        return {ok:true,status:200,json:async()=>({ok:true,...data})};
      };
    });
    await page.goto('https://offline.test/progress');await page.evaluate(()=>window.SJFXMailPeople.select('alice@example.com'));
    await page.getByRole('button',{name:'通读整个资料包并生成完整报告',exact:true}).click();
    assert.equal(await page.locator('.mp-current-batch').innerText(),'当前正在处理第1批，3749字，尚未通过核查保存。');
    assert((await page.locator('.mp-stages').innerText()).includes('已核查保存 0 批 · 0 / 13166 字'));
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'0','an active 3749-character batch cannot become committed coverage');
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('aria-label'),'已核查保存的可用文本覆盖');
    assert.equal(await page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).count(),0);

    await page.evaluate(async()=>{window.task.status='failed';window.task.updated_at=new Date().toISOString();window.task.error={message:'人物事实报告未返回可解析的JSON',detail:"Expecting ',' delimiter: line 783 column 21"};window.task.progress.activity.state='validating';window.task.progress.activity.summary='等待模型资源；正在读取原文';await window.SJFXMailPeople.applyScope({});});
    assert((await page.locator('.mp-live-error').innerText()).includes("Expecting ',' delimiter: line 783 column 21"),'the actual provider failure detail is visible');
    assert((await page.locator('.mp-current-batch').innerText()).includes('失败前处理第1批，3749字，尚未通过核查保存'));
    const failedText=await page.locator('.mp-live-progress').innerText();
    assert(!failedText.includes('等待模型资源')&&!failedText.includes('正在读取原文')&&!failedText.includes('等待阅读与核查'),'terminal failure replaces stale running and waiting activity');
    assert.equal(await page.locator('.mp-stage[aria-current]').count(),0);
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'0');
    const frozen=await page.locator('.mp-elapsed').innerText();await page.waitForTimeout(1100);assert.equal(await page.locator('.mp-elapsed').innerText(),frozen,'failure stops the running elapsed timer');
    assert(!(await page.getByRole('button',{name:'从已保存进度继续',exact:true}).isDisabled()));

    await page.evaluate(()=>{window.task.error=null;window.task.progress.adaptive_batch={char_budget:1800,retry_count:1,retry_limit:3,minimum_char_budget:500,last_error:'结构化JSON不完整',failed_characters:3749};Object.assign(window.task.progress.activity,{state:'retrying_smaller_batch',summary:'同一游标缩小批次后重试',retry_reason:'结构化JSON不完整',retry_attempt:1,retry_limit:3,failed_characters:3749,next_char_budget:1800});});
    await page.getByRole('button',{name:'从已保存进度继续',exact:true}).click();
    const retryText=await page.locator('.mp-live-retry').innerText();
    assert(retryText.includes('缩批重试 1 / 3')&&retryText.includes('原失败批次 3749 字')&&retryText.includes('下一批上限 1800 字')&&retryText.includes('原因：结构化JSON不完整'));
    assert((await page.locator('.mp-live-wait').innerText()).includes('正在按较小批次重试'));
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'0','retry keeps the original committed coverage');
    assert.equal(await page.locator('.mp-live-error').count(),0,'an automatic retry is not presented as a terminal failure');
    assert.equal(await page.evaluate(()=>window.calls.filter(call=>call.path.endsWith('/person/analyses')&&call.method==='POST').length),1,'resume retries the existing turn instead of creating a new report');

    await page.evaluate(async()=>{Object.assign(window.task.progress.activity,{state:'waiting_for_model',characters:1690,summary:'等待模型资源；读取缩小后的批次',queue_started_at:new Date(Date.now()-30000).toISOString(),generation_started_at:undefined});await window.SJFXMailPeople.applyScope({});});
    assert.equal(await page.locator('.mp-current-batch').innerText(),'当前正在处理第1批，1690字，尚未通过核查保存。','the active batch shows actual smaller content, not the configured limit');
    assert((await page.locator('.mp-live-retry').innerText()).includes('原失败批次 3749 字'),'retry metadata survives the model wait');
    assert((await page.locator('.mp-live-wait').innerText()).includes('模型资源排队已等待'));
    await page.evaluate(async()=>{Object.assign(window.task.progress.activity,{state:'running',generation_started_at:new Date(Date.now()-2000).toISOString(),summary:'读取缩小后的批次'});await window.SJFXMailPeople.applyScope({});});
    assert(/本次模型处理已用时 [23]秒/.test(await page.locator('.mp-live-wait').innerText()),'admitted retry uses generation time, not its earlier queue wait');
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'0');

    await page.evaluate(async()=>{Object.assign(window.task.progress,{characters_read:1690,batches:1,findings_count:2});window.task.report.coverage.characters_read=1690;Object.assign(window.task.progress.activity,{state:'checkpointed',summary:'已保存本批事件概括'});await window.SJFXMailPeople.applyScope({});});
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'1690','only a real checkpoint advances coverage');
    assert.equal(await page.locator('.mp-current-batch,.mp-live-retry').count(),0,'saved checkpoints do not look like an uncommitted or ongoing retry');
    await page.evaluate(async()=>{window.failPoll=true;await window.SJFXMailPeople.applyScope({});});
    assert((await page.locator('.mp-live-connection').innerText()).includes('5秒后重新连接'));
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'1690','a network read failure never clears verified saved coverage');
    await page.evaluate(async()=>{window.failPoll=false;window.task.status='failed';window.task.updated_at=new Date().toISOString();window.task.error={code:'person_fact_output_incomplete',message:'缩批次数已达上限，原游标保留',detail:'Unterminated string starting at line 778'};Object.assign(window.task.progress.activity,{state:'failed',batch_index:1,characters:1750,summary:'等待模型资源'});await window.SJFXMailPeople.applyScope({});});
    assert((await page.locator('.mp-live-error').innerText()).includes('缩批次数已达上限，原游标保留'));
    assert((await page.locator('.mp-live-error').innerText()).includes('Unterminated string starting at line 778'));
    assert.equal(await page.locator('.mp-live-retry').count(),0,'terminal adaptive failure cannot appear to be retrying');
    assert.equal(await page.locator('.mp-live-progress progress').getAttribute('value'),'1690','later failure preserves earlier saved batches');
    assert.equal(await page.getByRole('button',{name:'下载完整报告文件（Markdown）',exact:true}).count(),0);
    assert.deepEqual(errors,[]);
    console.log('mail people progress: pending first batch vs saved zero coverage, truthful adaptive retries and model clocks, terminal reasons/timer stop, and checkpoint preservation across network/later failure passed');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
