/* Evidence-backed mail and pair dossiers. All content stays text/DOM nodes. */
(() => {
  'use strict';
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const statuses={supported:'原文与上下文支持',conditional:'受条件限制',contradicted:'存在反证或变化',insufficient:'资料不足',unresolved:'待核实'};
  const intents={request:'请求',propose:'提议',accept:'接受',reject:'拒绝',commit:'承诺',deliver:'声称交付',report_completed:'声称完成',amend:'修改',acknowledge_receipt:'确认收到',confirm:'确认',refuse:'拒绝',negotiate:'协商',inform:'告知',coordinate:'协调',question:'询问',withdraw:'撤回',other:'其他表达'};
  const stateLabels={requested:'提出请求',proposed:'提出方案',accepted:'表达接受',rejected:'表达拒绝',committed:'表达承诺',reported_delivery:'声称交付',claimed_completed:'声称完成',amended:'修改表达',withdrawn:'撤回表达',informed:'告知',received_information:'收到信息',questioned:'提出疑问',confirmed_unspecified:'确认对象待明',unresolved:'资料不足',conditional_accept:'附条件接受',conditional_commit:'附条件承诺',pending_verification:'待核实'};
  const textValue=v=>typeof v==='string'?v: v?.expression||v?.statement||v?.reason||'待核实资料';
  const conditionText=c=>typeof c==='string'?c:[c.expression||'条件待明',c.object?`对象 ${c.object}`:'',c.object_version?`版本 ${c.object_version}`:'','满足情况待核实'].filter(Boolean).join(' · ');
  async function api(path,options={}){
    const headers={};for(const key of ['sjfx_api_token','sjfxApiToken'])for(const store of ['sessionStorage','localStorage']){try{const t=window[store].getItem(key);if(t&&Array.from(t.trim()).every(c=>c.charCodeAt(0)>32&&c.charCodeAt(0)<127))headers['X-SJFX-Token']=t.trim();}catch(_){}}
    if(options.body)headers['Content-Type']='application/json';
    const response=await fetch(path,{...options,headers,credentials:'same-origin'}),value=await response.json();
    if(!response.ok||value.ok===false)throw new Error(typeof value.error==='string'?value.error:value.error?.message||'邮件分析请求失败');return value;
  }
  function action(text,fn,cls){const b=el('button',text,cls);b.type='button';b.addEventListener('click',()=>Promise.resolve().then(fn).catch(e=>{const error=b.closest('dialog')?.querySelector('[data-analysis-error]');if(error)error.textContent=e.message;}));return b;}
  function scope(){return window.SJFXMailFoundation?.currentScope()||{package_id:document.getElementById('mail-package')?.value};}
  async function open(options={}){
    const frozen={...scope(),...(options.scope||{})},pid=options.package_id||frozen.package_id;
    if(!pid)throw new Error('请先选择资料包');
    const dialog=el('dialog',undefined,'mi-dialog');dialog.setAttribute('aria-label',options.kind==='pair'?'双方通信意图':options.kind==='screen'?'按研究目标逐封筛查':'单封通信意图');
    const head=el('header',undefined,'mi-head'),title=el('h2',dialog.getAttribute('aria-label'));
    head.append(title,action('关闭',()=>dialog.close(),'mi-close'));
    const question=el('textarea');question.rows=3;question.maxLength=1200;question.placeholder='希望理解什么目标、条件、分歧或后续变化？';question.value=options.question||'';
    const label=el('label',options.kind==='screen'?'逐封筛查目标（必填）':'希望核查的问题（可选）');label.append(question);
    const budget=el('input');budget.setAttribute('aria-label','模型调用上限');budget.type='number';budget.min='1';budget.max='32768';budget.value='4096';
    const tokenBudget=el('input');tokenBudget.setAttribute('aria-label','token 总上限');tokenBudget.type='number';tokenBudget.min='1';tokenBudget.max='100000000';tokenBudget.value='5000000';
    const advanced=el('details'),budgetLabel=el('label','模型调用上限'),tokenLabel=el('label','token 总上限');budgetLabel.append(budget);tokenLabel.append(tokenBudget);advanced.append(el('summary','阅读预算与续接'),budgetLabel,tokenLabel,el('p','到达预算会保存阶段结果，可增加上限后继续。实际与估算 token 分别记录，续接保留已有消耗。'));
    const controls=el('div',undefined,'mi-controls'),progress=el('p','提交后将在后台分批阅读和核查。','mi-progress'),error=el('p',undefined,'mi-error');error.dataset.analysisError='';error.setAttribute('role','alert');progress.setAttribute('role','status');
    const content=el('div',undefined,'mi-content'),ledger=el('div',undefined,'mi-ledger'),pager=el('nav',undefined,'mi-pager');
    let tid='',timer=null,offset=0,epoch=0,pollEpoch=0;
    const valid=()=>dialog.open&&scope().package_id===pid;
    function checkedBudget(){const b=Number(budget.value);if(!Number.isInteger(b)||b<1||b>32768)throw new Error('调用上限需要1至32768的整数');return b;}
    function checkedTokens(){const b=Number(tokenBudget.value);if(!Number.isInteger(b)||b<1||b>100000000)throw new Error('token 上限需要1至100000000的整数');return b;}
    const start=action('开始分析',async()=>{error.textContent='';checkedBudget();checkedTokens();start.disabled=true;const force=Boolean(tid);tid='';epoch++;clearTimeout(timer);try{const data=await api(`/api/mail/packages/${encodeURIComponent(pid)}/intent-analyses`,{method:'POST',body:JSON.stringify({...options,kind:options.kind||'message',scope:frozen,question:question.value,model_call_budget:checkedBudget(),max_tokens:checkedTokens(),force})});if(!valid())return;tid=data.turn_id;offset=0;progress.textContent=data.cached?'读取当前资料的已完成分析…':'任务已提交，正在固定证据范围…';await poll();}finally{if(valid()&&!tid)start.disabled=false;}});
    const cancel=action('停止',async()=>{if(!tid)return;await api(`/api/mail/research-turns/${encodeURIComponent(tid)}/cancel`,{method:'POST'});await poll();});cancel.disabled=true;
    const resume=action('从检查点继续',async()=>{if(!tid)return;await api(`/api/mail/intent-analyses/${encodeURIComponent(tid)}/resume`,{method:'POST',body:JSON.stringify({model_call_budget:checkedBudget(),max_tokens:checkedTokens()})});await poll();});resume.hidden=true;
    const rangeParts=[];
    if(frozen.date_from||frozen.date_to)rangeParts.push(`日期 ${frozen.date_from||'不限'} 至 ${frozen.date_to||'不限'}`);
    if(frozen.q)rangeParts.push('邮件关键词 '+frozen.q);
    if(frozen.participant)rangeParts.push('参与人 '+frozen.participant);
    if(frozen.conversation_id)rangeParts.push('所选会话');
    if(Array.isArray(frozen.message_keys))rangeParts.push(`明确选择 ${frozen.message_keys.length} 封`);
    if(frozen.has_attachment===true||frozen.has_attachment==='true')rangeParts.push('仅含附件邮件');
    if(frozen.has_attachment===false||frozen.has_attachment==='false')rangeParts.push('仅无附件邮件');
    const rangeText=`固定范围：${document.getElementById('mail-package')?.selectedOptions?.[0]?.textContent||'所选资料包'} · ${rangeParts.join('；')||'当前资料包全部邮件'}。`;
    const readingText=options.kind==='screen'?'逐封核查范围内可用正文与附件文本，相关、不相关、待判断和失败分别记录。':options.kind==='pair'?`${options.address||'人物'} ↔ ${options.counterpart||'对方'}：核查双方相关邮件和当前范围内上下文。`:'以所选这封邮件为中心，核查当前范围内相关上下文，分别识别作者正文与引用。';
    const help=el('details');help.append(el('summary','怎样使用与核对结果'),el('p','先确认范围和问题，再点击“开始分析”。进入此界面不会启动模型。分析结果是邮件表达与原文依据，现实履行情况需要独立确认。'),el('p','点击“打开来源邮件”核对原文；人物链接可查看往来；预算用完时保留阶段结果，可从检查点继续。'));
    controls.append(start,cancel,resume);dialog.append(head,el('p',rangeText,'mi-scope'),el('p',readingText,'mi-scope'),label,help,advanced,controls,progress,error,content,ledger,pager);document.body.append(dialog);
    dialog.addEventListener('close',()=>{epoch++;clearTimeout(timer);dialog.remove();},{once:true});dialog.showModal();
    const renderQuote=(parent,q)=>{if(!q)return;const block=el('div',undefined,'mi-quote');block.append(el('small',`${q.sent_at||'日期未知'} · ${(q.attribution==='authored'||q.authored)&&!q.quotation_warning?'可归属正文':'背景、引文或收到的信息'}`),el('blockquote',q.quote||q.exact_quote||q.quote_text||''));if(q.message_key)block.append(action('打开来源邮件',()=>window.SJFXMailFoundation?.openMessage(q.message_key,{...frozen,package_id:pid})));parent.append(block);};
    function renderState(parent,s){const card=el('section',undefined,'mi-state');card.append(el('strong',`${s.actor||'参与方待明'} · ${s.object_version||'版本未标明'}`),el('p',`当前表达：${stateLabels[s.status]||'待核实'}${s.expressed_status&&s.expressed_status!==s.status?'；原文表达 '+(stateLabels[s.expressed_status]||'待核实'):''}`));(s.conditions||[]).forEach(c=>card.append(el('p','条件：'+conditionText(c))));if(s.gaps?.length)card.append(el('p',`${s.gaps.length} 项资料或核查缺口，当前状态仍有适用限制。`,'mi-limit'));if(s.real_world_completion==='unverified')card.append(el('small','现实履行情况未经独立核实'));parent.append(card);}
    function render(data){
      const p=data.progress||{};content.replaceChildren();ledger.replaceChildren();pager.replaceChildren();
      (p.issues||[]).forEach(issue=>{const card=el('article',undefined,'mi-issue');card.append(el('span',statuses[issue.review_status]||'待核查',`mi-badge mi-${issue.review_status}`),el('h3',issue.title),el('p',issue.statement),el('small',issue.uncertainty));const people=el('div',undefined,'mi-actors');(issue.actors||[]).forEach(a=>people.append(action(a,()=>window.SJFXMailPeople?.select(a,frozen))));card.append(people);content.append(card);});
      if(p.answer){const answer=el('details',undefined,'mi-answer');answer.append(el('summary','阅读分析结论'),el('p',p.answer));content.append(answer);}
      const counts=p.ledger_counts||data.ledger_counts;if(counts)content.append(el('p',`结果台账 · 发现 ${counts.facts||counts.findings||0} · 条件 ${counts.conditions||0} · 矛盾或变化 ${counts.contradictions||0} · 待确认 ${counts.unresolved_questions||0}`,'mi-limit'));
      if(data.sufficiency==='insufficient'||p.sufficiency==='insufficient')content.append(el('p','当前资料不足以支持完整结论，请结合具体缺口与原文核查。','mi-limit'));
      const shared=data.shared_issues||data.issue_ledger,sharedItems=Array.isArray(shared)?shared:shared?.items||[];
      sharedItems.forEach(issue=>{const card=el('article',undefined,'mi-issue');card.append(el('h3',issue.title||'共享事项'),el('p',`归并：${issue.link_status==='confirmed'?'已确认':'暂定或待核实'} · 语义审核：${statuses[issue.review_status]||'待核实'}`));(issue.actor_states||issue.states||[]).forEach(s=>renderState(card,s));content.append(card);});if(shared?.next_offset!=null)content.append(el('p','共享事项概览还有更多记录，请在通信图的事项层搜索与分页查看。','mi-limit'));
      (p.limitations||[]).forEach(t=>content.append(el('p',t,'mi-limit')));
      if(p.kind==='intent_screen'&&p.result_set_id)content.append(action('查看筛选结果',()=>{document.dispatchEvent(new CustomEvent('sjfx:mail-result',{detail:{package_id:pid,result_set_id:p.result_set_id}}));dialog.close();}));
      (data.batches||[]).forEach(batch=>{
        if(batch.status){const row=el('article',undefined,'mi-issue');row.append(el('strong',({matched:'相关',not_matched:'不相关',unknown:'待判断',failed:'失败'})[batch.status]||batch.status),action('打开此邮件',()=>window.SJFXMailFoundation?.openMessage(batch.item_id,{...frozen,package_id:pid})));(batch.rationale?.decision_reasons||[]).forEach(t=>row.append(el('p',t)));(batch.evidence||[]).filter(Boolean).forEach(q=>renderQuote(row,{...q,quote:q.quote_text}));ledger.append(row);}
        (batch.findings||[]).forEach(f=>{const row=el('details',undefined,'mi-finding'),semantic=f.semantic_status||f.semantic_verification?.status;row.append(el('summary',`${f.actor} · ${intents[f.act_type||f.intent]||'表达'} · ${f.issue||f.issue_title||'事项待明'}`),el('p',f.statement),el('span','上下文核查：'+(statuses[f.review_status]||'待核查'),'mi-badge'),el('span','语义支持：'+(statuses[semantic]||'尚未核验'),`mi-badge mi-${semantic||'unresolved'}`));
          if(f.object||f.object_identity||f.object_version)row.append(el('p',`对象：${f.object_identity||f.object||'未标明'} · 版本：${f.object_version||'未标明'}`));
          [['conditions','条件'],['contradictions','矛盾或变化'],['unresolved_questions','待确认']].forEach(([key,name])=>{const values=Array.from(new Set([...(f[key]||[]),...(f.context_review?.[key]||[])].map(v=>key==='conditions'?conditionText(v):textValue(v))));if(values.length)row.append(el('p',name+'：'+values.join('；')));});row.append(el('small',f.uncertainty));(f.semantic_verification?.missing_requirements||[]).forEach(t=>row.append(el('p','资料缺口：'+textValue(t),'mi-limit')));const seen=new Set();[...(f.citations||[]),...(f.context_review?.citations||[])].forEach(q=>{const key=JSON.stringify([q.message_key,q.mime_path,q.char_start,q.quote]);if(!seen.has(key)){seen.add(key);renderQuote(row,q);}});ledger.append(row);});
      });
      if(data.batches?.length)ledger.prepend(el('h3','逐批表达与证据台账'));
      (data.ledger||[]).forEach(entry=>{const row=el('details',undefined,'mi-finding');row.append(el('summary',({fact:'有依据的发现',conditions:'条件',contradictions:'矛盾或变化',unresolved_questions:'待确认问题'})[entry.kind]||'核查记录'),el('p',textValue(entry.statement)));(entry.citations||[]).forEach(q=>renderQuote(row,q));ledger.append(row);});
      if(!data.batches?.length&&!p.issues?.length)ledger.append(el('p','当前页尚无已核查表达；任务完成前仅表示阶段资料。','mi-limit'));
      const total=Math.max(data.total||0,data.page_total||0,shared?.total||0);if(total){const prev=action('上一页',()=>{offset=Math.max(0,offset-5);return poll();}),next=action('下一页',()=>{offset+=5;return poll();});prev.disabled=offset===0;next.disabled=offset+5>=total;pager.append(prev,el('span',`${offset+1}–${Math.min(offset+5,total)} / ${total} ${p.kind==='intent_screen'?'封':'条台账与批次记录'}`),next);}
    }
    async function poll(){
      clearTimeout(timer);if(!valid()||!tid)return;const ticket=epoch,pollTicket=++pollEpoch,data=await api(`/api/mail/intent-analyses/${encodeURIComponent(tid)}?offset=${offset}&limit=5`);if(!valid()||ticket!==epoch||pollTicket!==pollEpoch)return;
      const p=data.progress||{},phase={reading:'逐批阅读',review:'核查条件与反证',summary:'整理双方与事项结论',completed:'当前分析结束'}[p.phase]||'等待处理';
      progress.textContent=`${phase} · 已读 ${p.characters_read||0} / ${p.corpus?.characters||'待统计'} 字 · ${p.findings_count||0} 条发现 · 模型调用 ${p.model_calls||0}`+(p.kind==='intent_screen'?` · 已判定 ${p.message_index||0} / ${p.corpus?.messages||0} 封`:'')+(p.complete?' · 已遍历当前可用资料':' · 当前为阶段结果');
      resume.disabled=false;const usage=p.token_usage;if(usage){progress.textContent+=` · token ${usage.total_tokens||0}/${usage.max_tokens||'未设上限'}（实际 ${usage.actual_tokens||0}，估算 ${usage.estimated_tokens||0}，预留 ${usage.reserved_tokens||0}）`;if(p.token_budget_exhausted||usage.exhausted){const used=(usage.total_tokens||0)+(usage.reserved_tokens||0);if(Number(tokenBudget.value)<=used||Number(tokenBudget.value)<=usage.max_tokens)tokenBudget.value=String(Math.min(100000000,Math.max((usage.max_tokens||0)*2,used+36000)));if(used>=100000000){resume.disabled=true;progress.textContent+=' · token 已达到界面上限，可保留当前资料核查。';}}}
      if(data.error)error.textContent=data.error.message||'当前分析未完成';
      const running=!['completed','failed','cancelled'].includes(data.status);cancel.disabled=!running;start.disabled=running;start.textContent=tid?'重新分析':'开始分析';resume.hidden=running||p.complete===true;
      if(p.budget_exhausted&&Number(budget.value)<=(p.model_calls||0))budget.value=String(Math.min(32768,Math.max(4096,(p.model_calls||0)*2)));
      render(data);if(running)timer=setTimeout(()=>poll().catch(e=>{if(valid())error.textContent=e.message;}),2000);
    }
    if(options.autoStart)start.click();return dialog;
  }
  window.SJFXMailAnalysis={open};
})();
