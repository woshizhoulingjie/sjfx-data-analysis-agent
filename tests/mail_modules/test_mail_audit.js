// Real DOM acceptance for source preservation and person ↔ mail ↔ conversation browsing.
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('https://offline.test/**',route=>route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><html><body></body></html>'}));
    await page.goto('https://offline.test/');
    await page.setContent('<select id="mail-package"><option value="p">资料包</option></select><button data-mail-module-link="network">人物</button><button data-mail-module-link="structure">会话</button><div data-mail-module="network"></div><div id="mail-foundation-reader"></div><div id="mail-conversation-detail"></div><div id="mail-status"></div>');
    await page.evaluate(()=>{
      window.calls=[];window.opened=[];window.entityConfirmed=false;
      localStorage.setItem('sjfx_api_token','audit-token');
      window.SJFXMailReader={inspectMessage:async(...args)=>window.opened.push(args)};
      const counts={total:3,sent:2,direct:1,cc:1,bcc:1,received:1};
      const item={message_key:'m1',subject_raw:'完整邮件',sent_at_utc:'2024-05-07T08:00:00Z',roles:'from',senders:['a@example.com'],attachment_count:1,participants:[{role:'from',address:'a@example.com'},{role:'to',address:'outside@example.com'},{role:'cc',address:'b@example.com'}],conversations:['conv']};
      window.fetch=async(raw,options={})=>{
        const url=new URL(raw,'https://offline.test');window.calls.push({path:url.pathname,query:url.search,headers:options.headers,body:options.body});let result={};
        if(url.pathname.endsWith('/people'))result={items:[{address:'a@example.com',addresses:window.entityConfirmed?['a@example.com','alias@example.com']:['a@example.com'],display_name:'Alice',entity_id:window.entityConfirmed?'entity':null,...counts}],total:1,offset:0,limit:30};
        else if(url.pathname.endsWith('/person/messages')){const direction=url.searchParams.get('direction'),total=counts[direction==='all'?'total':direction];result={items:Array.from({length:total},(_,i)=>({...item,message_key:'m'+i})),role_counts:counts,total,offset:0,limit:20,time_range:{first_sent:'2024-05-07T08:00:00Z',last_sent:'2024-05-07T08:00:00Z'}};}
        else if(url.pathname.endsWith('/person/relationships'))result={items:[{counterpart:'b@example.com',display_name:'Bob',total:1,sent:0,received:0,cc:1,bcc:0,shared:0}],total:1,offset:0,limit:8};
        else if(url.pathname.endsWith('/person/pair/messages'))result={items:[{...item,pair_roles:['sent_cc']}],counts:{total:1,sent:0,received:0,cc:1,bcc:0,shared:0},total:1,offset:0,limit:20};
        else if(url.pathname.endsWith('/person/analyses'))result={items:[]};
        else if(url.pathname==='/api/mail/entities/confirm'){window.entityConfirmed=true;result={entity_id:'entity'};}
        else if(url.pathname.endsWith('/preview-image'))return {ok:true,blob:async()=>new Blob(['image'],{type:'image/png'})};
        else if(url.pathname.endsWith('/raw'))return {ok:true,blob:async()=>new Blob(['raw original'])};
        else if(url.pathname.endsWith('/content'))result={content:{raw_available:true,complete:true,raw_headers:'From: a@example.com',occurrences:[{occurrence_id:'copy',source_member_name:'inbox.eml'}],participants:item.participants,conversation_ids:['conv'],reply_edges:[{source_message_key:'m1',target_message_key:null,relation_type:'reply_to',status:'missing_target'}],parts:[{mime_path:'1',part_role:'body_plain',text:'新增内容\n历史原话',fragments:[{fragment_kind:'authored',text:'新增内容'},{fragment_kind:'quoted',text:'历史原话'}]},{mime_path:'2',part_role:'body_html',text:'图片正文',html_raw:'<p>原始HTML</p><script>unsafe()</script>',html:'<p>图片正文</p><img src="/api/mail/messages/m1/parts/3/preview-image?package_id=p">',fragments:[]},{mime_path:'3',content_id:'logo',part_role:'inline',is_inline_resource:true,content_type:'image/png',byte_size:30,filename_raw:'logo.png'}]}};
        else if(url.pathname==='/api/mail/conversations/conv')result={conversation:{subject_normalized:'往来',merge_basis:'exact_chain',filtered_time_range:{from:'2024-05-07T08:00:00Z',to:'2024-05-08T08:00:00Z'},participants:[{address:'a@example.com',roles:['from','to']},{address:'b@example.com',roles:['from','cc']}],messages:[{...item,message_key:'m1',body_fragments:[{fragment_kind:'authored',text:'新增内容'},{fragment_kind:'quoted',text:'历史原话'}],attachments:[{message_key:'m1',mime_path:'4',filename_raw:'合同.pdf',content_type:'application/pdf',byte_size:50}]},{...item,message_key:'m2',subject_raw:'Bob的回复',participants:[{role:'from',address:'b@example.com'},{role:'to',address:'a@example.com'}],body_fragments:[{fragment_kind:'uncertain',text:'完整且不确定的正文'}]}],tree_nodes:[],tree_edges:[],corrections:[]}};
        else throw new Error('Unexpected endpoint '+url.pathname);
        return {ok:true,status:200,json:async()=>({ok:true,...result})};
      };
    });
    await page.addScriptTag({path:path.resolve(__dirname,'../../static/mail-ui.js')});
    await page.addScriptTag({path:path.resolve(__dirname,'../../static/mail-foundation.js')});
    await page.addScriptTag({path:path.resolve(__dirname,'../../static/mail-people.js')});
    await page.evaluate(()=>window.SJFXMailPeople.select('a@example.com',{q:'原话',has_attachment:'false',conversation_id:'conv'}));
    await page.getByRole('button',{name:'直接收到 1 封，查看明细',exact:true}).click();
    await page.waitForFunction(()=>window.calls.some(c=>c.path.endsWith('/person/messages')&&c.query.includes('direction=direct')));
    await page.getByRole('button',{name:'Bob',exact:true}).click();
    await page.locator('.mp-mail-row').last().getByText('抄送对方',{exact:true}).waitFor();
    await page.locator('.mp-mail-row').last().getByRole('button',{name:'完整邮件',exact:true}).click();
    const opened=await page.evaluate(()=>window.opened[0]);assert.equal(opened[1],false,'mail navigation never launches translation');assert.equal(opened[2].conversation_id,'conv');assert.equal(opened[2].has_attachment,'false');
    const requests=await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/person/messages')||c.path.endsWith('/person/pair/messages')));
    requests.forEach(c=>{const p=new URLSearchParams(c.query);assert.equal(p.get('q'),'原话');assert.equal(p.get('conversation_id'),'conv');assert.equal(p.get('has_attachment'),'false');});
    await page.getByRole('button',{name:'人工确认多邮箱归并',exact:true}).click();
    await page.getByLabel('人物名称',{exact:true}).fill('Alice');await page.getByLabel('已确认属于同一人的邮箱（每行一个）',{exact:true}).fill('a@example.com\nalias@example.com');
    await page.getByRole('button',{name:'确认这些邮箱属于同一人',exact:true}).click();await page.getByRole('button',{name:'撤销此人工邮箱归并',exact:true}).waitFor();
    const confirm=await page.evaluate(()=>JSON.parse(window.calls.find(c=>c.path==='/api/mail/entities/confirm').body));assert.deepEqual(confirm,{package_id:'p',display_name:'Alice',addresses:['a@example.com','alias@example.com']});
    await page.evaluate(()=>window.SJFXMailFoundation.renderMessage('m1',{parts:[]},{package_id:'p'}));
    const reader=page.locator('#mail-foundation-reader');assert((await reader.innerText()).includes('新增内容'));assert(!(await reader.innerText()).includes('历史原话'),'quotes start folded in reading view');
    await reader.locator('summary').filter({hasText:/^历史引用$/}).first().click();assert((await reader.innerText()).includes('历史原话'));
    assert((await reader.innerText()).includes('被回复邮件未收录'));
    await reader.getByText('原始 HTML 正文（未清理源码）',{exact:true}).click();assert((await reader.innerText()).includes('unsafe()'),'raw HTML survives separately without execution');
    assert.equal(await reader.locator('.mail-foundation-body > section').count(),1,'default reading selects one preferred alternative');
    await reader.getByRole('button',{name:'完整正文',exact:true}).click();
    assert.equal(await reader.locator('.mail-foundation-body > section').count(),2,'both stored alternatives remain available');
    const frame=reader.locator('iframe');assert((await frame.getAttribute('srcdoc')).includes('src="blob:'),'CID resources use authenticated blob URLs');assert.equal(await frame.getAttribute('sandbox'),'');
    await reader.getByRole('button',{name:'预览内嵌图片',exact:true}).click();await reader.locator('img').waitFor();
    await reader.getByText('导入来源与副本 (1)',{exact:true}).click();await reader.getByRole('button',{name:'下载此来源的原始副本',exact:true}).click();
    await page.waitForFunction(()=>window.calls.some(c=>c.path.endsWith('/raw')&&c.query.includes('occurrence_id=copy')));
    const imageCalls=await page.evaluate(()=>window.calls.filter(c=>c.path.endsWith('/preview-image')));assert(imageCalls.length>=2);assert(imageCalls.every(c=>c.headers['X-SJFX-Token']==='audit-token'));
    await page.evaluate(()=>window.SJFXMailFoundation.openConversation('conv',{package_id:'p'}));
    const conversation=page.locator('#mail-conversation-detail');assert((await conversation.innerText()).includes('合同.pdf'));assert((await conversation.innerText()).includes('完整且不确定的正文'));
    await conversation.getByLabel('会话发件人筛选',{exact:true}).selectOption('b@example.com');assert((await conversation.innerText()).includes('发件人筛选：1 / 2'));assert((await conversation.innerText()).includes('Bob的回复'));
    assert.deepEqual(errors,[]);console.log('mail audit: role drilldown, shared scope, manual identity, full source, missing parent, CID and conversation sender filtering passed');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
