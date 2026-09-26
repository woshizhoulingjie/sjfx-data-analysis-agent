/* Additive correspondence workbench: no translation or protection hooks. */
(() => {
  'use strict';
  const host=document.querySelector('[data-mail-module=network]');
  const packageSelect=document.getElementById('mail-package');
  if(!host || !packageSelect)return;
  const state={packageId:'',address:'',contactsOffset:0,messagesOffset:0,relationsOffset:0,batchOffset:0,turnId:'',epoch:0,poll:null};
  const root=document.createElement('section');root.className='mp-workbench';host.prepend(root);
  const css=document.createElement('link');css.rel='stylesheet';css.href='/static/mail-people.css?v=1';document.head.append(css);
  const node=(tag,text,cls)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;};
  const button=(text,action,cls)=>{const el=node('button',text,cls);el.type='button';el.addEventListener('click',()=>{const epoch=state.epoch;Promise.resolve().then(action).catch(error=>{if(alive(epoch))report(error);});});return el;};
  function token(){for(const key of ['sjfx_api_token','sjfxApiToken'])for(const store of ['sessionStorage','localStorage']){try{const t=window[store].getItem(key);if(t && Array.from(t.trim()).every(c=>c.charCodeAt(0)>32&&c.charCodeAt(0)<127))return t.trim();}catch(_){}}return ''; }
  async function api(path,options={}){
    const headers={};const value=token();if(value)headers['X-SJFX-Token']=value;
    if(options.body)headers['Content-Type']='application/json';
    const response=await fetch(path,{...options,headers,credentials:'same-origin'});const data=await response.json();
    if(!response.ok || data.ok===false)throw new Error(response.status===401?'请先使用页面右上角 Token 连接服务':data.error || '邮件请求失败');return data;
  }
  const base=()=>'/api/mail/packages/'+encodeURIComponent(state.packageId);
  const params=(extra={})=>new URLSearchParams({address:state.address,...extra}).toString();
  const alive=(epoch)=>epoch===state.epoch && state.packageId===packageSelect.value;
  const heading=node('header',undefined,'mp-header');heading.append(node('span','CORRESPONDENCE / 往来分析','mp-eyebrow'),node('h2','联系人与往来邮件'),node('p','先看谁与谁通信，再查看一个邮箱的全部收发邮件，最后根据原文分析表达的目标与顾虑。'));
  const notice=node('p','请选择资料包','mp-notice');notice.setAttribute('role','status');
  const layout=node('div',undefined,'mp-layout'),sidebar=node('aside',undefined,'mp-contacts'),main=node('div',undefined,'mp-content');
  const searchLabel=node('label','查找联系人（邮箱或已确认姓名）');const search=node('input');search.type='search';search.placeholder='中文、English、ภาษาไทย';searchLabel.append(search);
  const list=node('div',undefined,'mp-contact-list'),contactPager=node('div',undefined,'mp-pager');
  sidebar.append(searchLabel,button('查找联系人',()=>{state.contactsOffset=0;return loadContacts();}),list,contactPager);layout.append(sidebar,main);root.append(heading,notice,layout);
  function report(error){notice.textContent=error.message||String(error);notice.classList.add('mp-error');}
  function status(text){notice.textContent=text;notice.classList.remove('mp-error');}
  search.addEventListener('keydown',e=>{if(e.key==='Enter'){state.contactsOffset=0;loadContacts().catch(report);}});
  async function loadContacts(){
    syncPackage();if(!state.packageId)return;const epoch=state.epoch,offset=state.contactsOffset,q=search.value;
    const data=await api(base()+'/people?'+new URLSearchParams({q,offset,limit:30}));
    if(!alive(epoch)||offset!==state.contactsOffset||q!==search.value)return;
    list.replaceChildren();data.items.forEach(item=>{const b=button('',()=>selectPerson(item.address),'mp-contact');b.append(node('strong',item.display_name||item.address),node('span',item.display_name?item.address:`发出 ${item.sent} · 收到 ${item.received}`),node('small',`${item.total} 封往来邮件`));b.setAttribute('aria-pressed',String(item.address===state.address));b.dataset.address=item.address;list.append(b);});
    if(!data.items.length)list.append(node('p','没有匹配的联系人。'));
    pager(contactPager,data.offset,data.limit,data.total,n=>{state.contactsOffset=n;return loadContacts();});status(data.identity_note);
  }
  function syncPackage(){if(state.packageId===packageSelect.value)return;document.querySelectorAll('dialog.mp-original').forEach(d=>d.close());clearTimeout(state.poll);state.epoch++;state.packageId=packageSelect.value;state.address='';state.turnId='';state.contactsOffset=0;list.replaceChildren();main.replaceChildren(node('p','请选择左侧联系人。'));contactPager.replaceChildren();}
  let mailList,mailPager,relationBox,relationPager,analysisBox,historyBox,direction,question,progress,startButton,cancelButton,resumeButton,summaryBox,analysisPager;
  function buildAnalysis(){
    const section=node('section',undefined,'mp-card mp-analysis');section.append(node('span','EVIDENCE FIRST / 基于原文','mp-eyebrow'),node('h3','这个人表达了什么意图？'),node('p','逐批阅读该邮箱的全部可用正文，不只挑选搜索命中的几封。收到的信息不等于本人认同；模型推测与原文分开显示。','mp-muted'));
    const label=node('label','你希望重点了解什么？（可选）');question=node('textarea');question.rows=3;question.maxLength=1200;question.placeholder='例如：他希望推动什么事项？提出了哪些条件？后来的邮件是否改变了计划？也支持 English / ภาษาไทย。';label.append(question);
    const actions=node('div',undefined,'mp-actions');startButton=button('分析全部往来邮件',startAnalysis);cancelButton=button('停止分析',async()=>{if(state.turnId){await api('/api/mail/research-turns/'+encodeURIComponent(state.turnId)+'/cancel',{method:'POST'});await pollAnalysis();}});cancelButton.disabled=true;resumeButton=button('从已保存进度继续',async()=>{const epoch=state.epoch,id=state.turnId;resumeButton.disabled=true;try{await api('/api/mail/person-analyses/'+encodeURIComponent(id)+'/resume',{method:'POST'});if(alive(epoch)&&state.turnId===id)await pollAnalysis();}catch(error){if(alive(epoch)){resumeButton.disabled=false;report(error);}}});resumeButton.hidden=true;actions.append(startButton,cancelButton,resumeButton);
    progress=node('p','尚未开始。完整邮件清单可直接浏览；分析需要模型逐批阅读。','mp-notice');progress.setAttribute('role','status');historyBox=node('div',undefined,'mp-history');analysisBox=node('div',undefined,'mp-findings');analysisPager=node('div',undefined,'mp-pager');summaryBox=node('div',undefined,'mp-summary');section.append(label,actions,progress,summaryBox,historyBox,node('h4','逐批发现与原文证据'),analysisBox,analysisPager);main.append(section);
  }
  async function loadHistory(){const epoch=state.epoch;const data=await api(base()+'/person/analyses?'+params());if(!alive(epoch))return;historyBox.replaceChildren();if(!data.items.length)return;const label=node('label','查看此前分析（最近20次）'),select=node('select');select.append(node('option','选择历史分析'));data.items.forEach(item=>{const o=node('option',item.created_at+' · '+item.question);o.value=item.turn_id;select.append(o);});select.addEventListener('change',()=>{if(!select.value)return;clearTimeout(state.poll);state.turnId=select.value;state.batchOffset=0;pollAnalysis().catch(report);});label.append(select);historyBox.append(label);}
  async function startAnalysis(){
    const epoch=state.epoch;startButton.disabled=true;progress.textContent='正在固定邮件范围并提交后台分析…';
    try{const data=await api(base()+'/person/analyses',{method:'POST',body:JSON.stringify({address:state.address,question:question.value})});if(!alive(epoch))return;state.turnId=data.turn_id;state.batchOffset=0;analysisBox.replaceChildren();await pollAnalysis();if(alive(epoch))await loadHistory();}
    catch(error){if(alive(epoch)){startButton.disabled=false;report(error);}}
  }
  async function pollAnalysis(){
    clearTimeout(state.poll);if(!state.turnId)return;const epoch=state.epoch,id=state.turnId,offset=state.batchOffset;
    const data=await api('/api/mail/person-analyses/'+encodeURIComponent(id)+'?'+new URLSearchParams({offset,limit:5}));if(!alive(epoch)||id!==state.turnId||offset!==state.batchOffset)return;
    const p=data.progress||{},c=p.corpus||{},labels={queued:'等待分析',running:'正在阅读',completed:'正文阅读完成',failed:'分析未完成',cancelled:'已停止',cancelling:'正在停止',waiting_for_index:'等待导入'};
    progress.textContent=(labels[data.status]||'等待后台处理')+` · 已读 ${p.characters_read||0}/${c.characters||'待统计'} 字符 · 已保存 ${p.findings_count||0} 条发现。`+(c.missing_body?` ${c.missing_body} 封缺少可用正文。`:'')+(p.rejected_findings?` ${p.rejected_findings} 条模型发现未通过核验。`:'')+(p.phase==='synthesis'?` 正在综合 ${p.synthesis?.batches_read||0}/${p.batches||0} 个批次。`:'')+(data.error?' '+data.error.message:'')+(p.complete?' '+p.coverage_note:' 当前仅为阶段结果，不代表全部分析完成。');
    renderSummary(p);resumeButton.hidden=!['failed','cancelled'].includes(data.status);resumeButton.disabled=false;const running=!['completed','failed','cancelled'].includes(data.status);
    startButton.disabled=running;cancelButton.disabled=!['queued','running','waiting_for_index'].includes(data.status);
    if(running&&!document.hidden)state.poll=setTimeout(()=>pollAnalysis().catch(error=>{if(alive(epoch))report(error);}),3500);
    const signature=JSON.stringify([id,offset,data.items,p.complete]);
    if(analysisBox.dataset.signature===signature)return;
    analysisBox.dataset.signature=signature;
    const expanded=new Set(Array.from(analysisBox.querySelectorAll('details[open]')).map(el=>el.parentElement.dataset.finding));
    analysisBox.replaceChildren();
    data.items.forEach(item=>{const card=node('article',undefined,'mp-finding');card.append(node('span',item.label,'mp-chip'),node('h4',item.statement),node('p',item.uncertainty,'mp-muted'));const details=node('details'),summary=node('summary',`核对 ${item.citations.length} 处原文证据`);details.append(summary);item.citations.forEach(citation=>{const quote=node('blockquote',citation.quote);quote.setAttribute('dir','auto');const meta=node('p',(citation.sent_at||'日期未知')+' · '+(citation.authored?'该邮箱发出':'该邮箱收到')+' · '+(citation.subject||'无主题'),'mp-muted');details.append(meta,quote,button('打开这封邮件原文',()=>openOriginal(citation.message_key),'mp-text-button'));});card.append(details,node('small',item.verification));analysisBox.append(card);});
    if(!data.items.length)analysisBox.append(node('p',p.complete?'没有通过证据核验的意图发现；这不表示该人没有意图。':'本页暂没有已核验的发现。'));
    pager(analysisPager,data.offset,data.limit,data.batch_total,n=>{state.batchOffset=n;return pollAnalysis();});
    Array.from(analysisBox.querySelectorAll('.mp-finding')).forEach((card,index)=>{card.dataset.finding=data.items[index].finding_id;if(expanded.has(card.dataset.finding))card.querySelector('details').open=true;});
  }
  function renderSummary(progressState){
    const synthesis=progressState.synthesis||{},signature=JSON.stringify([synthesis,progressState.complete]);
    if(summaryBox.dataset.signature===signature)return;summaryBox.dataset.signature=signature;summaryBox.replaceChildren();
    if(!synthesis.themes?.length)return;
    summaryBox.append(node('h3',progressState.complete?'整体意图概览':'整体意图概览 · 正在综合'),node('p',synthesis.note,'mp-muted'));
    synthesis.themes.forEach(theme=>{const card=node('article',undefined,'mp-finding');card.append(node('span','综合主题 · 模型解读','mp-chip'),node('h4',theme.title),node('p',theme.statement),node('p',theme.uncertainty,'mp-muted'));
      const details=node('details');details.append(node('summary','查看支撑这一主题的原文'));
      theme.evidence.forEach(f=>{details.append(node('p',f.label+'：'+f.statement));f.citations.forEach(c=>{const quote=node('blockquote',c.quote);quote.dir='auto';details.append(node('small',(c.sent_at||'日期未知')+' · '+(c.authored?'该邮箱发出':'该邮箱收到')),quote,button('打开邮件核对',()=>openOriginal(c.message_key),'mp-text-button'));});});card.append(details);summaryBox.append(card);});
  }
  async function loadRelations(){
    const epoch=state.epoch,offset=state.relationsOffset;const data=await api(base()+'/person/relationships?'+params({offset,limit:8}));if(!alive(epoch)||offset!==state.relationsOffset)return;
    relationBox.replaceChildren();const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 620 350');svg.setAttribute('class','mp-graph');svg.setAttribute('role','img');svg.setAttribute('aria-label','选中邮箱与本页联系人的收发方向图，详细数量见下方列表');
    const shape=(tag,attrs,text)=>{const el=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,String(v));if(text!==undefined)el.textContent=text;return el;};
    const defs=shape('defs',{}),marker=shape('marker',{id:'mp-arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(shape('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'currentColor'}));defs.append(marker);svg.append(defs);
    data.items.forEach((item,i)=>{const angle=2*Math.PI*i/Math.max(1,data.items.length)-Math.PI/2,x=310+210*Math.cos(angle),y=175+118*Math.sin(angle);const edge=shape('line',{x1:310+(x-310)*.19,y1:175+(y-175)*.19,x2:x-(x-310)*.1,y2:y-(y-175)*.1,stroke:'var(--accent)','stroke-width':1+Math.min(4,Math.log2(1+item.total)),opacity:.65});if(item.sent)edge.setAttribute('marker-end','url(#mp-arrow)');if(item.received)edge.setAttribute('marker-start','url(#mp-arrow)');svg.append(edge);const g=shape('g',{tabindex:0,role:'button','aria-label':'查看 '+item.counterpart+' 的全部往来邮件'});g.append(shape('circle',{cx:x,cy:y,r:12,fill:'var(--accent)'}),shape('text',{x,y:y+29,'text-anchor':'middle'},item.counterpart.length>25?item.counterpart.slice(0,22)+'…':item.counterpart),shape('title',{},item.counterpart+' · 发出 '+item.sent+' / 收到 '+item.received));const choose=()=>selectPerson(item.counterpart).catch(report);g.addEventListener('click',choose);g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();choose();}});svg.append(g);});
    svg.append(shape('circle',{cx:310,cy:175,r:35,fill:'var(--panel)',stroke:'var(--accent)','stroke-width':2}),shape('text',{x:310,y:180,'text-anchor':'middle'},'当前邮箱'));relationBox.append(svg);
    data.items.forEach(item=>{const row=node('div',undefined,'mp-relation');row.append(button(item.counterpart,()=>selectPerson(item.counterpart),'mp-text-button'),node('span',`发出 ${item.sent} →　← 收到 ${item.received}`),node('small',`${item.total} 封 · ${item.first_sent||'日期未知'} — ${item.last_sent||'日期未知'}`));relationBox.append(row);});
    if(!data.items.length)relationBox.append(node('p','没有可确认的通信对象，可能缺少收件人信息。'));
    pager(relationPager,data.offset,data.limit,data.total,n=>{state.relationsOffset=n;return loadRelations();});
  }
  async function selectPerson(address){
    syncPackage();if(!state.packageId)return;document.querySelectorAll('dialog.mp-original').forEach(d=>d.close());state.epoch++;clearTimeout(state.poll);state.address=address;state.turnId='';state.messagesOffset=0;state.relationsOffset=0;state.batchOffset=0;
    list.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.address===address)));
    main.replaceChildren(node('h3',address,'mp-person-title'));
    const stats=node('div',undefined,'mp-stats');stats.id='mp-counts';main.append(stats);
    const relations=node('section',undefined,'mp-card');relationBox=node('div');relationPager=node('div',undefined,'mp-pager');relations.append(node('h3','谁与这个邮箱通信'),node('p','箭头和数字表示实际邮件收发，不代表现实身份或认同关系。','mp-muted'),relationBox,relationPager);main.append(relations);
    const emails=node('section',undefined,'mp-card');direction=node('select');direction.setAttribute('aria-label','邮件收发范围');[['all','全部往来'],['sent','本人发出'],['received','收到（含抄送和密送）'],['cc','抄送给他'],['bcc','密送给他（包内可见）']].forEach(([v,t])=>{const o=node('option',t);o.value=v;direction.append(o);});
    direction.addEventListener('change',()=>{state.messagesOffset=0;loadMessages().catch(report);});
    mailList=node('div',undefined,'mp-mail-list');mailPager=node('div',undefined,'mp-pager');emails.append(node('h3','全部收发邮件'),direction,mailList,mailPager);main.append(emails);buildAnalysis();
    await Promise.all([loadMessages(),loadRelations(),loadHistory()]);
  }
  async function loadMessages(){
    const epoch=state.epoch,offset=state.messagesOffset,selected=direction.value;const data=await api(base()+'/person/messages?'+params({direction:selected,offset,limit:20}));
    if(!alive(epoch)||offset!==state.messagesOffset||selected!==direction.value)return;
    const stats=main.querySelector('#mp-counts');stats.replaceChildren();[['往来总数',data.counts.total],['本人发出',data.counts.sent],['收到邮件',data.counts.received]].forEach(([t,n])=>{const card=node('div');card.append(node('strong',n),node('span',t));stats.append(card);});
    mailList.replaceChildren();data.items.forEach(item=>{const row=node('article',undefined,'mp-mail-row'),open=button(item.subject_raw||'（无主题）',()=>openOriginal(item.message_key),'mp-text-button');row.append(open,node('small',(item.sent_at_utc||'日期未知')+' · '+(item.roles||'').split(',').map(r=>({from:'发出',to:'收件',cc:'抄送',bcc:'密送'}[r]||r)).join('、')+' · '+({zh:'中文',en:'英文',th:'泰文'}[item.body_language]||item.body_language||'语言未知')));if(item.sender)row.append(button('发件人：'+item.sender,()=>selectPerson(item.sender.split(',')[0]),'mp-text-button mp-muted'));mailList.append(row);});
    if(!data.items.length)mailList.append(node('p','此范围没有邮件。'));pager(mailPager,data.offset,data.limit,data.total,n=>{state.messagesOffset=n;return loadMessages();});
  }
  function pager(target,offset,limit,total,load){target.replaceChildren();const prev=button('上一页',()=>load(Math.max(0,offset-limit))),next=button('下一页',()=>load(offset+limit));prev.disabled=offset===0;next.disabled=offset+limit>=total;target.append(prev,node('span',total?`${offset+1}–${Math.min(offset+limit,total)} / ${total}`:'暂无记录'),next);}
  async function openOriginal(messageKey){
    const epoch=state.epoch;const data=await api('/api/mail/messages/'+encodeURIComponent(messageKey)+'/preview?'+new URLSearchParams({package_id:state.packageId}));if(!alive(epoch))return;
    const dialog=node('dialog',undefined,'mp-original');dialog.setAttribute('aria-label','邮件原文核对');const close=button('关闭原文',()=>dialog.close());
    const text=node('pre',data.content||'无正文');text.setAttribute('dir','auto');
    if(data.content_type==='text/html'){
      // Render HTML source as text, never execute mail markup or fetch its resources.
      text.textContent=data.content||'';dialog.append(node('p','此邮件是HTML正文。为避免远程图片、脚本和隐藏内容影响核对，这里展示原始标记文本。'));
    }
    dialog.append(node('h3','邮件原文 · 保留原语言'),close,text);document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove(),{once:true});dialog.showModal();close.focus();
  }
  packageSelect.addEventListener('change',()=>{syncPackage();loadContacts().catch(report);});
  const networkLink=document.querySelector('[data-mail-module-link=network]');
  if(networkLink){networkLink.textContent='联系人与往来邮件';networkLink.addEventListener('click',()=>loadContacts().catch(report));}
  window.SJFXMailPeople={select:async address=>{networkLink?.click();await selectPerson(address);}};
  window.addEventListener('pagehide',()=>clearTimeout(state.poll));
  document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(state.poll);else if(state.turnId)pollAnalysis().catch(report);});
  syncPackage();if(state.packageId)loadContacts().catch(report);
})();
