(() => {
  "use strict";
  const state = { packageId: "", packageEpoch: 0, sessionId: "", resultSetId: "", evidenceId: "", jobId: "", turnId: "", pollEpoch: 0, researchTurns: {}, questions: {}, titles: {}, chinese: {}, previewEpoch: 0, resultEpoch: 0, listEpoch: 0, offset: 0, final: null, conversationItems: [], conversationOffset: 0, versionItems: [], versionOffset: 0 };
  const byId = (id) => document.getElementById(id);
  // 分页大小与「邮件与证据」列表保持一致。这两个列表不再截断，全部条目均可翻页抵达。
  const PANEL_PAGE_SIZE = 25;
  // 绑定助手：控件缺失时静默跳过，避免旧缓存页面配新脚本时抛错中断整个脚本。
  const on = (id, handler) => { const node = byId(id); if (node) node.addEventListener("click", handler); };
  const status = (text, error = false) => { const node = byId("mail-status"); node.textContent = text; node.classList.toggle("mail-error", error); };
  const TOKEN_KEYS = ["sjfx_api_token", "sjfxApiToken"];
  const normalizeToken = (value) => {
    const normalized = String(value || "").trim();
    return /^[\x21-\x7e]+$/.test(normalized) ? normalized : "";
  };
  const token = () => {
    for (const key of TOKEN_KEYS) {
      for (const name of ["sessionStorage", "localStorage"]) {
        try {
          const value = normalizeToken(window[name].getItem(key));
          if (value) return value;
        } catch (_) { /* A blocked browser store must not prevent rendering. */ }
      }
    }
    return "";
  };
  function configureToken() {
    const dialog = byId("mail-token-dialog");
    byId("mail-token-input").value = "";
    byId("mail-token-error").textContent = "";
    if (!dialog.open) dialog.showModal();
    byId("mail-token-input").focus();
  }
  function saveToken(event) {
    event.preventDefault();
    const value = normalizeToken(byId("mail-token-input").value);
    if (!value) {
      byId("mail-token-error").textContent = "请输入有效的访问凭证";
      return;
    }
    try { sessionStorage.setItem("sjfx_api_token", value); }
    catch (_) {
      byId("mail-token-error").textContent = "浏览器阻止了会话存储，请允许后重试";
      return;
    }
    byId("mail-token-input").value = "";
    byId("mail-token-dialog").close();
    status("正在连接邮件服务");
    loadWorkspaceGuide();
    loadPackages(state.packageId).then(() => status("邮件服务已连接，选择资料包后开始研究")).catch(report);
  }
  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const value = token();
    if (value) headers["X-SJFX-Token"] = value;
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { ...options, headers });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) throw new Error("未通过鉴权，请点击右上角的凭证设置按钮");
    if (!response.ok || !payload.ok) {
      const error = new Error(payload.error ? display(payload.error) : "邮件研究请求失败");
      error.status = response.status;
      throw error;
    }
    return payload;
  }
  const text = (value) => String(value == null ? "" : value);
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function option(label, value) { const node = document.createElement("option"); node.value = value; node.textContent = label; return node; }
  function cell(row, value) { const node = document.createElement("td"); node.textContent = text(value); row.appendChild(node); }
  const LABELS = {inventory:"待导入",importing:"导入中",attachment_index_ready:"附件就绪",body_index_ready:"正文就绪",queued:"排队中",waiting_for_index:"等待资料处理",running:"处理中",completed:"已完成",failed:"失败",cancelled:"已取消",paused:"已暂停",partial:"覆盖不完整",sufficient:"证据较充分",insufficient:"证据不足",conflicting:"存在冲突",not_started:"未开始",ocr_pending:"等待文字识别",unsupported:"暂不支持",ready:"已就绪",asserted:"原文支持",inferred:"待核实推断",confirmed:"已确认",candidate:"候选关联",exact:"精确关联",reply_to:"回复",references:"引用",from:"发件人",to:"收件人",cc:"抄送",bcc:"密送",mail_research:"邮件研究",mail_import:"邮件导入",mail_extract:"附件解析",mail_ocr:"文字识别",mail_embed:"语义索引",mail_factextract:"字段提取",mail_export:"结果导出"};
  const zhLabel = (value, fallback = "未记录") => {
    return LABELS[value] || window.SJFXMailUI?.label(value, fallback) || (/[\u3400-\u9fff]/.test(text(value)) ? text(value) : fallback);
  };
  const mime = value => window.SJFXMailUI?.mime(value) || "附件文件";
  const display = value => window.SJFXMailUI?.format(value) || (value == null ? "未记录" : typeof value === "object" ? Object.entries(value).map(([key, item]) => `${zhLabel(key, "其他信息")}：${display(item)}`).join("；") : text(value));
  const date = value => window.SJFXMailUI?.date(value) || text(value) || "日期未知";
  const updateText = (id, value) => { const node = byId(id); if (node) node.textContent = value; };
  const updateProgress = value => { updateText("mail-progress", value); updateText("mail-answer-progress", value); };
  const openModule = key => document.querySelector(`[data-mail-module-link="${key}"]`)?.click();
  function infoDetails(title, value) {
    if (window.SJFXMailUI) return window.SJFXMailUI.details(title, value);
    const node=document.createElement("details"),summary=document.createElement("summary"),body=document.createElement("p");summary.textContent=title;body.textContent=display(value);node.append(summary,body);return node;
  }
  const isChinese = value => /[\u3400-\u9fff]/.test(text(value)) && !/[\u0e00-\u0e7f\u3040-\u30ff\uac00-\ud7af]/.test(text(value)) && !/[A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){2,}/.test(text(value));
  const zhTitle = (key, original) => state.chinese[`title:${key}`] || original || "无主题";
  const zhValue = (key, original) => state.chinese[key] || (isChinese(original) || !/[A-Za-z\u0e00-\u0e7f]/.test(text(original)) ? text(original) : "中文翻译中…");
  function sourceDetails(value) { const d=document.createElement("details"),summary=document.createElement("summary"),body=document.createElement("div");summary.textContent="查看原文";body.textContent=text(value);d.append(summary,body);return d; }
  async function presentation(payload, valid, update) {
    const body={package_id:state.packageId,...payload};
    for(let attempt=0;attempt<300 && valid();attempt++) {
      const p=(await api("/api/mail/presentation",{method:"POST",body:JSON.stringify(body)})).presentation;
      body.retry=false;
      if(!valid())return null;
      update(p);
      if(!["pending","paused"].includes(p.status))return p;
      await new Promise(resolve=>window.setTimeout(resolve,1500));
    }
    if(valid())throw new Error("翻译仍在后台处理，可稍后重试");
    return null;
  }
  function absorbChinese(p) {Object.entries(p.values || {}).forEach(([key,value])=>{if(value.text)state.chinese[key]=value.text;else if(value.status==="failed")state.chinese[key]="译文暂不可用（原文可核查）";});}
  function renderTranslationSegments() {
    const output=byId("mail-preview");clear(output);
    const compare=byId("mail-bilingual").checked;
    output.classList.toggle("mail-bilingual",compare);
    const notice=state.previewPages.find(page=>page.sourceNotice)?.sourceNotice;
    if(notice){const warning=document.createElement("p");warning.className="mail-hint";warning.textContent=notice;output.append(warning);}
    state.previewPages.slice().sort((a,b)=>a.offset-b.offset).forEach(page=>{
      const segments=(page.value || {}).segments || [];
      if(!segments.length){const p=document.createElement("p");p.textContent=(page.value || {}).text || "正在准备中文译文…";output.append(p);return;}
      segments.forEach(segment=>{
        const row=document.createElement("section");row.className="mail-translation-segment";
        const label=document.createElement("div");label.className="mail-segment-label";label.textContent=`第 ${segment.ordinal+1} 段 · ${segment.status==="pending"?"翻译中":segment.status==="failed"?"需要重试":zhLabel(segment.status,"译文状态待确认")}`;
        const translated=document.createElement("div");translated.className="mail-segment-zh";translated.textContent=segment.text!=null?segment.text:segment.status==="failed"?"该段暂未完成，点击重试。其他已完成译文已保留。":"正在翻译这一段…";
        if(segment.status==="failed")row.classList.add("mail-segment-failed");
        row.append(label,translated);
        if(compare){const source=document.createElement("div");source.className="mail-segment-source";source.textContent=segment.source;row.append(source);}
        output.append(row);
      });
    });
  }
  async function presentSource(payload, original, append=false) {
    const epoch=++state.previewEpoch;
    const offset=payload.offset || 0;
    state.previewPayload=payload;
    if(!append){state.previewPages=[];state.previewOriginal=original;byId("mail-original").textContent=original || "暂无原文";byId("mail-preview").textContent="正在准备中文译文…";}
    byId("mail-translate").disabled=true;byId("mail-more-translation").hidden=true;
    try {
      const p=await presentation(payload,()=>epoch===state.previewEpoch,p=>{
        const value=(p.values || {}).body;
        if(value){const page={offset,value,status:p.status,sourceNotice:p.metadata?.notice};const existing=state.previewPages.findIndex(x=>x.offset===offset);if(existing<0)state.previewPages.push(page);else state.previewPages[existing]=page;renderTranslationSegments();byId("mail-original").textContent=state.previewPages.slice().sort((a,b)=>a.offset-b.offset).map(p=>(p.value.segments || []).map(s=>s.source).join("")).join("");state.previewOriginal=byId("mail-original").textContent;}
        const done=p.completed_segments || 0,total=p.total_segments || 0;
        byId("mail-translation-status").textContent=p.status==="pending" ? `${p.phase==="queued"?"等待翻译":"正在翻译"} · 已完成 ${done}/${total} 段` : p.status==="completed"?`中文翻译完成 · ${done} 段，可与原文对照`:"部分段落暂未完成，已保留可用译文";
        const title=(p.values || {}).title;
        if(title && title.text && byId("mail-detail").querySelector("h2"))byId("mail-detail").querySelector("h2").textContent=title.text;
        if((p.metadata || {}).notice)byId("mail-translation-status").textContent=display(p.metadata.notice);
      });
      if(!p || epoch!==state.previewEpoch)return;
      state.nextTranslationOffset=(p.metadata || {}).next_offset;
      byId("mail-more-translation").hidden=state.nextTranslationOffset==null;
      if(state.nextTranslationOffset!=null)byId("mail-translation-status").textContent+="；还有后续内容";
      byId("mail-translate").textContent=p.status==="completed"?"译文已完成":"重试未完成段落";
    } finally {if(epoch===state.previewEpoch)byId("mail-translate").disabled=state.previewPages.length>0 && state.previewPages.every(page=>page.status==="completed");}
  }
  async function translateResult(result,retry=false) {
    const epoch=++state.resultEpoch;let offset=0;
    do {
      const p=await presentation({kind:"result",result_set_id:result.result_set_id,offset,retry},()=>epoch===state.resultEpoch,p=>{absorbChinese(p);renderFields((result.definition || {}).fields || []);renderEvidence((result.final || {}).evidence || []);renderResultItems(result);byId("mail-result-language").textContent=p.status==="pending"?"正在补齐中文字段与证据译文…":p.status==="completed"?"中文显示已更新；原文可展开核对":`中文译文${zhLabel(p.status,"状态待确认")}；已保留可用译文，原文可核查`;});
      if(!p || epoch!==state.resultEpoch)return;
      offset=(p.metadata || {}).next_offset;
      if(p.status!=="completed")byId("mail-result-language").textContent="部分译文暂不可用；原始证据仍可核查";
    } while(offset!=null);
  }
  function renderEvidence(evidence) {
    const list=byId("mail-evidence-list");clear(list);
    (evidence || []).forEach(e=>{
      const li=document.createElement("li"),source=document.createElement("p"),quote=document.createElement("p");
      source.className="mail-evidence-source";
      const sender=e.actor || (e.participants || []).find(p=>p.role==="from")?.address;
      source.textContent=[e.sent_at_utc?date(e.sent_at_utc):"",sender,e.subject_raw?zhTitle(e.message_key,e.subject_raw):"",e.source_kind==="attachment"?"附件":"邮件正文"].filter(Boolean).join(" · ");
      quote.className="mail-evidence-quote";quote.textContent=zhValue(`evidence:${e.evidence_id}`,e.quote_text).slice(0,360);
      li.append(source,quote,evidenceButton("打开对应原文",e.evidence_id),sourceDetails(e.quote_text));
      if(e.source_text_notice){const warning=document.createElement("p");warning.className="mail-hint";warning.textContent=e.source_text_notice;li.append(warning);}list.append(li);
    });
    updateAnswerAppendix();
  }
  function renderResultItems(result) {
    const list=byId("mail-result-items");clear(list);
    (result.items || []).slice(0,200).forEach(item=>{const li=document.createElement("li"),button=document.createElement("button");button.type="button";button.className="mail-relation-link";button.textContent=`${({matched:"匹配",unknown:"待核查",failed:"失败"})[item.status] || "保留"} · ${zhTitle(item.item_id,state.titles[item.item_id])}`;button.addEventListener("click",()=>inspectMessage(item.item_id).catch(report));li.append(button);list.append(li);});
    if((result.items || []).length>200){const li=document.createElement("li");li.textContent="当前显示前 200 条，完整记录可导出查看";list.append(li);}
  }
  function renderOverview(overview) {
    const pkg = overview?.package;
    const packageOption = pkg && Array.from(byId("mail-package").options).find((item) => item.value === pkg.package_id);
    if (packageOption) {
      if (pkg.display_name) packageOption.dataset.displayName = pkg.display_name;
      packageOption.textContent = `${packageOption.dataset.displayName || pkg.package_id} (${zhLabel(pkg.status)})`;
    }
    const node = byId("mail-overview"); clear(node);
    const counts = (overview || {}).counts || {};
    [["唯一邮件数", counts.logical_messages, "messages"], ["来源副本", counts.occurrences, "package"], ["正文可读", counts.messages_with_body, "messages"], ["附件", counts.explicit_attachments, "structure"], ["参与邮箱", counts.participant_addresses, "network"]].forEach(([label, value, module]) => {
      const card = document.createElement("div"),button=document.createElement("button");button.type="button";button.className="mail-relation-link";const number = document.createElement("b"); number.textContent = value == null ? "未记录" : text(value);button.append(number, document.createTextNode(label));button.addEventListener("click",()=>{openModule(module);if(label==="来源副本"){const inventory=byId("mail-foundation-inventory"),details=inventory?.closest?.("details");if(details)details.open=true;inventory?.scrollIntoView?.({block:"nearest"});}});card.append(button); node.appendChild(card);
    });
    const coverage = overview.coverage || {};
    state.packageCoverage=coverage;
    node.append(infoDetails("副本及更多统计",{counts,coverage}));
    const missing=coverage.attachments_without_text != null ? coverage.attachments_without_text : Math.max(0,(coverage.explicit_attachments || 0)-(coverage.attachments_with_text || 0));
    byId("mail-coverage").textContent = `覆盖：附件可读 ${coverage.attachments_with_text || 0}/${coverage.explicit_attachments || 0}；尚无文本 ${missing}；文字识别待处理 ${coverage.ocr_pending || 0}；处理失败 ${coverage.failed_parts || 0}`;
    updateText("mail-package-summary",`已收录 ${counts.logical_messages ?? "未记录"} 封唯一邮件、${counts.occurrences ?? "未记录"} 份来源副本。可按当前范围浏览邮件并提交研究。`);
    updateText("mail-processing-summary", missing || coverage.failed_parts || coverage.ocr_pending ? `资料仍有缺口：${missing} 个附件尚无可读文本，${coverage.ocr_pending || 0} 个等待文字识别，${coverage.failed_parts || 0} 个处理失败。研究结论会保留这些限制。` : "当前附件没有已报告的文本处理缺口；研究和中文译文会在提交后按需处理。");
  }
  async function loadPackages(selectId) {
    const payload = await api("/api/mail/packages");
    const select = byId("mail-package"); clear(select); select.appendChild(option("选择邮件资料包...", ""));
    payload.packages.forEach((item) => {
      const entry = option(`${item.display_name} (${zhLabel(item.status)})`, item.package_id);
      entry.dataset.displayName = item.display_name;
      select.appendChild(entry);
    });
    if (selectId) { select.value = selectId; state.packageId = selectId; await loadPackage(); }
  }
  async function loadPackage() {
    state.packageId = byId("mail-package").value;
    const packageId=state.packageId,packageEpoch=++state.packageEpoch;
    state.packageCoverage={};state.final=null;state.jobId="";resetAnswerExtras();
    for(const id of ["mail-overview","mail-coverage","mail-foundation-status","mail-foundation-inventory","mail-foundation-job","mail-conversations","mail-versions","mail-network","mail-communication","mail-threads","mail-business-relations","mail-coverage-gaps","mail-timeline-list","mail-list"])if(byId(id))clear(byId(id));
    for(const id of ["mail-count","mail-timeline-count","mail-page","mail-inventory-page","mail-conversations-page","mail-versions-page","mail-result-summary","mail-result-language"])updateText(id,"");
    for(const id of ["mail-prev","mail-next","mail-inventory-prev","mail-inventory-next","mail-conversations-prev","mail-conversations-next","mail-versions-prev","mail-versions-next","mail-filter-result"])if(byId(id))byId(id).disabled=true;
    for(const [id,title] of [["mail-session","新建会话…"],["mail-result-history","选择结果集…"]])if(byId(id)){clear(byId(id));byId(id).append(option(title,""));}
    updateProgress("尚无任务");
    state.pollEpoch++; state.previewEpoch++; state.resultEpoch++; state.listEpoch++;state.previewPages=[];state.previewPayload=null;state.currentResult=null;clear(byId("mail-preview"));clear(byId("mail-original"));clear(byId("mail-detail"));if(byId("mail-foundation-reader"))clear(byId("mail-foundation-reader"));if(byId("mail-conversation-detail"))clear(byId("mail-conversation-detail"));clear(byId("mail-findings"));clear(byId("mail-answer-meta"));clear(byId("mail-limitations"));byId("mail-translation-status").textContent="选择邮件后显示中文译文"; state.offset=0; state.conversationOffset=0; state.versionOffset=0; state.conversationItems=[]; state.versionItems=[]; state.chinese={}; state.titles={}; clear(byId("mail-fields")); clear(byId("mail-evidence-list")); clear(byId("mail-result-items")); byId("mail-answer").textContent="输入你要查找、比较或提取的内容。"; byId("mail-conflicts").textContent=""; state.sessionId = ""; state.resultSetId = ""; state.turnId = ""; byId("mail-export").disabled = true; byId("mail-start").disabled = false; byId("mail-cancel").disabled = true;
    if(window.SJFXMailFoundation) {window.SJFXMailFoundation.emitScope();window.SJFXMailFoundation.refreshFoundation().catch(report);}
    if (!state.packageId) {status("请先选择资料包，再选择首页任务；也可导入新的邮件资料。");updateText("mail-package-summary","选择资料包后查看已收录邮件和来源。");updateText("mail-current-scope","尚未选择资料包");updateText("mail-processing-summary","选择资料包后显示处理状态与资料缺口");return;}
    status("正在读取所选资料包…");
    updateText("mail-package-summary","正在读取资料包数量与覆盖情况…");
    const payload = await api(`/api/mail/packages/${encodeURIComponent(packageId)}`);
    if(packageEpoch!==state.packageEpoch || packageId!==state.packageId)return;
    renderOverview({ ...payload.overview, coverage: payload.coverage });
    await Promise.all([loadMessages(), loadSessions(), loadRelations(), loadMiningViews()]);
    if(packageEpoch===state.packageEpoch && packageId===state.packageId)status(`已选择资料包：${byId('mail-package').selectedOptions?.[0]?.textContent||'当前资料包'}。可选择首页任务或浏览邮件。`);
  }
  async function loadMessages() {
    if (!state.packageId) return;
    const epoch=++state.listEpoch;
    const sharedScope = window.SJFXMailFoundation?.currentScope() || {
      q: byId("mail-query").value.trim(), participant: byId("mail-participant").value.trim(),
      has_attachment: byId("mail-with-attachment").checked ? true : null,
      date_from: byId("mail-date-from")?.value, date_to: byId("mail-date-to")?.value
    };
    const query = new URLSearchParams({ offset:String(state.offset), limit: "25", query: sharedScope.q || "", participant: sharedScope.participant || "" });
    if (sharedScope.has_attachment != null && sharedScope.has_attachment !== "") query.set("has_attachment", String(sharedScope.has_attachment));
    for(const key of ["date_from","date_to"]) if(sharedScope[key]) query.set(key,sharedScope[key]);
    const payload = await api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/messages?${query}`);
    if(epoch!==state.listEpoch)return;
    const result = payload.messages; (result.items || []).forEach(item=>{state.titles[item.message_key]=item.subject_raw || "无主题";}); const body = byId("mail-list"); clear(body);
    updateText("mail-current-scope",`当前范围：${[sharedScope.q ? `关键词“${sharedScope.q}”` : "",sharedScope.participant ? `参与邮箱 ${sharedScope.participant}` : "",sharedScope.date_from ? `从 ${sharedScope.date_from}` : "",sharedScope.date_to ? `至 ${sharedScope.date_to}` : "",sharedScope.has_attachment === true || sharedScope.has_attachment === "true" ? "含附件" : sharedScope.has_attachment === false || sharedScope.has_attachment === "false" ? "无附件" : ""].filter(Boolean).join("；") || "整个资料包"} · ${result.total} 封唯一邮件`);
    byId("mail-count").textContent = `${result.total} 封逻辑邮件`;
    renderTimeline(result.items, result.total);
    if (!result.items.length) { byId("mail-page").textContent="0 条";byId("mail-next").disabled=true;byId("mail-prev").disabled=state.offset===0;const row = document.createElement("tr"); const node = document.createElement("td"); node.colSpan = 5; node.textContent = "当前范围没有邮件"; row.appendChild(node); body.appendChild(row); return; }
    result.items.forEach((item) => { const row = document.createElement("tr"); row.tabIndex = 0; cell(row, date(item.sent_at_utc || item.date_raw)); cell(row, item.sender || ""); cell(row, zhTitle(item.message_key,item.subject_raw)); row.dataset.messageKey=item.message_key; row.title=item.subject_raw || ""; cell(row, item.attachment_count || 0);
      const status=[];if(item.original_preserved===1)status.push("原件已保存");else if(item.original_preserved===0)status.push("原件未保留");if(item.parse_status&&item.parse_status!=="parsed")status.push(item.parse_status==="parsed_with_warnings"?"解析有提示":"解析："+zhLabel(item.parse_status));if(item.body_empty)status.push("正文为空");if(item.date_unknown)status.push("发送时间未知");if(item.attachment_incomplete)status.push(`${item.attachment_incomplete} 个附件文字未完整`);cell(row,status.join(" · ")||"邮件已解析");
      row.addEventListener("click", () => inspectMessage(item.message_key).catch(report)); row.addEventListener("keydown", (event) => { if (event.key === "Enter") inspectMessage(item.message_key).catch(report); }); body.appendChild(row); });
    byId("mail-page").textContent=`${state.offset+1}–${state.offset+result.items.length} / ${result.total}`;byId("mail-prev").disabled=state.offset===0;byId("mail-next").disabled=state.offset+25>=result.total;
    presentation({kind:"titles",message_keys:result.items.map(i=>i.message_key)},()=>epoch===state.listEpoch,p=>{Object.entries(p.values || {}).forEach(([key,value])=>{state.chinese[`title:${key}`]=value.text || (value.status==="failed"?"译文暂不可用（点击查看原文）":"主题翻译中…");});body.querySelectorAll("tr[data-message-key]").forEach(row=>{row.cells[2].textContent=zhTitle(row.dataset.messageKey,state.titles[row.dataset.messageKey]);});renderTimeline(result.items,result.total);}).catch(report);

  }
  function renderTimeline(items, total) {
    const list = byId("mail-timeline-list"); clear(list); byId("mail-timeline-count").textContent = `${total} 封`;
    if (!items.length) { const row = document.createElement("li"); row.textContent = "当前范围没有邮件"; list.appendChild(row); return; }
    items.slice().reverse().forEach((item) => { const row = document.createElement("li"); const time = document.createElement("time"); time.textContent = date(item.sent_at_utc || item.date_raw); const detail = document.createElement("button"); detail.type = "button"; detail.className = "mail-relation-link"; detail.textContent = `${item.sender || "未知发件人"}：${zhTitle(item.message_key,item.subject_raw)}`; detail.addEventListener("click", () => inspectMessage(item.message_key).catch(report)); row.append(time, detail); list.appendChild(row); });
  }
  async function inspectMessage(messageKey, translate=true, filters={}) {
    window.SJFXMailLayout?.openReader();
    const epoch=++state.previewEpoch;
    state.previewPayload=null;state.previewPages=[];byId("mail-translate").disabled=true;byId("mail-more-translation").hidden=true;
    if(byId("mail-foundation-reader"))byId("mail-foundation-reader").textContent="正在读取完整原始内容…";
    const packageQuery = `package_id=${encodeURIComponent(filters.package_id || state.packageId)}`;
    const [detail, preview] = await Promise.all([api(`/api/mail/messages/${encodeURIComponent(messageKey)}?${packageQuery}`), api(`/api/mail/messages/${encodeURIComponent(messageKey)}/preview?${packageQuery}`).catch(error=>({content_type:"text/plain",content:`正文暂不可预览：${error.message}。原始邮件头、附件和来源仍可回查。`,unavailable:true}))]);
    if(epoch!==state.previewEpoch)return;
    const message = detail.message; const container = byId("mail-detail"); clear(container);
    const title = document.createElement("h2"); title.textContent = message.subject_raw || "无主题"; container.appendChild(title);
    const list = document.createElement("dl"); [["日期", date(message.sent_at_utc || message.date_raw)], ["参与方", (message.participants || []).map((item) => `${zhLabel(item.role)}: ${item.normalized_address}`).join("\n")]].forEach(([label, value]) => { const term = document.createElement("dt"); term.textContent = label; const definition = document.createElement("dd"); definition.textContent = text(value); list.append(term, definition); }); container.appendChild(list);
    const attachments = (message.parts || []).filter((item) => item.is_explicit_attachment);
    if (attachments.length) { const section = document.createElement("section"); section.className = "mail-attachment-list"; const heading = document.createElement("h3"); heading.textContent = "附件"; const items = document.createElement("ul"); attachments.forEach((item,i) => { const row = document.createElement("li"); const button = document.createElement("button"); button.type = "button"; button.className = "mail-relation-link"; button.textContent = `${item.filename_raw || `未命名附件 ${i+1}`}（${mime(item.content_type)}，${zhLabel(item.processing_status || "not_started")}）`; button.addEventListener("click", () => inspectAttachment(messageKey, item.mime_path, item.filename_raw || `未命名附件 ${i+1}`).catch(report)); row.appendChild(button); items.appendChild(row); }); section.append(heading, items); container.appendChild(section); }
    const output = byId("mail-preview"); clear(output); if (preview.content_type === "text/html") output.innerHTML = preview.content; else output.textContent = preview.content;
    byId("mail-original").textContent=output.textContent;
    byId("mail-translation-status").textContent=preview.unavailable?"正文暂不可翻译，完整原始资料可继续查看":"原文已就绪，中文翻译正在准备";
    state.evidenceId = "";
    if(window.SJFXMailFoundation) {
      try {await window.SJFXMailFoundation.renderMessage(messageKey,message,{package_id:state.packageId,...filters});}
      catch(error) {if(epoch===state.previewEpoch)byId("mail-foundation-reader").textContent=`完整原始内容暂不可读取：${error.message}。邮件元数据和现有阅读内容已保留。`;}
    }
    if(epoch!==state.previewEpoch)return;
    if(translate && !preview.unavailable) presentSource({kind:"message",message_key:messageKey,package_id:filters.package_id || state.packageId},output.textContent).catch(report);
  }
  async function inspectAttachment(messageKey, mimePath, label) {
    window.SJFXMailLayout?.openReader();
    const epoch=++state.previewEpoch;
    const packageQuery = `package_id=${encodeURIComponent(state.packageId)}`;
    const encodedMessage = encodeURIComponent(messageKey); const encodedPath = mimePath.split("/").map(encodeURIComponent).join("/");
    const [preview, segments] = await Promise.all([api(`/api/mail/messages/${encodedMessage}/attachments/${encodedPath}/preview?${packageQuery}`), api(`/api/mail/messages/${encodedMessage}/attachments/${encodedPath}/segments?${packageQuery}`)]);
    if(epoch!==state.previewEpoch)return;
    const values = (segments.segments || []).map((item) => `${item.text_origin==="ocr"?"OCR 识别文本 · ":""}${item.page_number ? `第 ${item.page_number} 页 ` : ""}${item.sheet_name ? `${item.sheet_name} ` : ""}${item.source_text_notice?item.source_text_notice+"\n":""}${item.text || ""}`).filter(Boolean);
    const output = byId("mail-preview"); output.textContent = values.length ? `附件：${label}\n\n${values.join("\n\n")}` : (preview.available ? preview.content : `${label}\n\n${preview.reason || "附件文本尚未就绪"}`);
    state.evidenceId = "";
    await presentSource({kind:"attachment",message_key:messageKey,mime_path:mimePath},output.textContent);
  }
  async function loadRelations() {
    if (!state.packageId) return;
    const packageEpoch=state.packageEpoch;
    const scopeQuery=window.SJFXMailFoundation?window.SJFXMailFoundation.query(window.SJFXMailFoundation.currentScope()):"";
    const unreviewed = byId("mail-show-unreviewed-relations").checked ? "?include_unreviewed=true" : "";
    const [communication, threads, business] = await Promise.all([api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/relations/communication?${scopeQuery}`), api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/relations/threads?status=exact&relation_type=reply_to&${scopeQuery}`), api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/relations/business${unreviewed}`)]);
    if(packageEpoch!==state.packageEpoch)return;
    const render = (id, items, line) => { const node = byId(id); clear(node); items.slice(0, 100).forEach((item) => { const row = document.createElement("li"); const value = line(item); if (typeof value === "string") row.textContent = value; else row.appendChild(value); node.appendChild(row); }); if (!items.length) { const row = document.createElement("li"); row.textContent = id === "mail-business-relations" ? "暂无经证据核验的业务事项关系" : "暂无可核验关系"; node.appendChild(row); } };
    render("mail-communication", communication.communication.items || [], (item) => { const node = document.createElement("button"); node.type = "button"; node.className = "mail-relation-link"; node.textContent = `${item.from_address} → ${item.to_address}（${item.message_count} 封，${date(item.first_sent)} 至 ${date(item.last_sent)}）`; node.addEventListener("click", () => { byId("mail-participant").value = item.to_address; if(window.SJFXMailFoundation)window.SJFXMailFoundation.emitScope(window.SJFXMailFoundation.currentScope({participant:item.to_address}));state.offset=0;loadMessages().catch(report);loadMiningViews().catch(report);loadRelations().catch(report); }); return node; });
    render("mail-threads", threads.threads || [], (item) => { const node = document.createElement("button"); node.type = "button"; node.className = "mail-relation-link"; node.textContent = `${item.source_subject || "无主题邮件"} → ${item.target_subject || "关联邮件未收录或未命名"}（${zhLabel(item.relation_type)}；${zhLabel(item.status)}）`; node.addEventListener("click", () => inspectMessage(item.source_message_key).catch(report)); return node; });
    const labels = { proposed: "提出", confirmed: "确认", modified: "修改", cancelled: "取消", supports: "支持", contradicts: "反驳" };
    render("mail-business-relations", business.relations || [], (item) => { const node = document.createElement("button"); node.type = "button"; node.className = "mail-relation-link"; const relation = `${display(item.subject_ref)} — ${labels[item.predicate] || zhLabel(item.predicate, "关联")} — ${display(item.object_ref)}`; node.textContent = `${relation}（${zhLabel(item.state)}）`; const evidenceId = (item.evidence_ids || [])[0]; if (evidenceId) node.addEventListener("click", () => inspectEvidence(evidenceId).catch(report)); else node.disabled = true; return node; });
  }
  async function inspectEvidence(evidenceId) {
    const epoch=++state.previewEpoch;
    const payload = await api(`/api/mail/evidence/${encodeURIComponent(evidenceId)}`); const evidence = payload.evidence;
    if(epoch!==state.previewEpoch)return;
    const detailEpoch=state.previewEpoch+1;
    await inspectMessage(evidence.message_key,false);
    if(state.previewEpoch!==detailEpoch)return;
    state.evidenceId = evidence.evidence_id; byId("mail-translate").disabled = false;
    await presentSource({kind:"evidence",evidence_id:evidenceId},`证据位置：${evidence.mime_path}${evidence.page_number ? `，第 ${evidence.page_number} 页` : ""}\n\n${evidence.quote_text || ""}`);
  }
  async function loadSessions() {
    const packageEpoch=state.packageEpoch, sessionEpoch=sessionRestoreEpoch;
    const payload = await api(`/api/mail/research-sessions?package_id=${encodeURIComponent(state.packageId)}`);
    if(packageEpoch!==state.packageEpoch || sessionEpoch!==sessionRestoreEpoch)return;
    const select = byId("mail-session"); clear(select); select.appendChild(option("新建会话...", "")); payload.sessions.forEach((item) => select.appendChild(option(item.title, item.session_id)));
  }
  async function ensureSession() {
    const packageEpoch=state.packageEpoch, sessionEpoch=sessionRestoreEpoch;
    state.sessionId = byId("mail-session").value || state.sessionId;
    if (state.sessionId) return state.sessionId;
    const payload = await api("/api/mail/research-sessions", { method: "POST", body: JSON.stringify({ package_id: state.packageId, title: "邮件研究" }) });
    if(packageEpoch!==state.packageEpoch || sessionEpoch!==sessionRestoreEpoch)return "";
    state.sessionId = payload.session.session_id; await loadSessions();if(packageEpoch!==state.packageEpoch || sessionEpoch!==sessionRestoreEpoch)return "";byId("mail-session").value = state.sessionId; return state.sessionId;
  }
  async function runResearch(event) {
    event.preventDefault(); if (!state.packageId) throw new Error("请先选择邮件资料包");
    const packageEpoch=state.packageEpoch, sessionEpoch=sessionRestoreEpoch;
    const question = byId("mail-question").value.trim(); if (!question) throw new Error("请输入研究目标");
    state.resultEpoch++;Object.keys(state.chinese).filter(k=>k.startsWith("field:") || k.startsWith("evidence:")).forEach(k=>delete state.chinese[k]);
    const sessionId = await ensureSession();
    if(packageEpoch!==state.packageEpoch || sessionEpoch!==sessionRestoreEpoch)return;
    const selectedScope = window.SJFXMailFoundation?.currentScope() || {};
    const {package_id: _packageId, ...filters} = selectedScope;
    const answerMode = byId("mail-answer-mode")?.value === "deep" ? "deep" : "fast";
    const payload = await api(`/api/mail/research-sessions/${encodeURIComponent(sessionId)}/turns`, { method: "POST", body: JSON.stringify({ question, answer_mode: answerMode, scope: { ...filters, include_explicit_attachments: true, include_inline_resources: false }, base_result_set_id: byId("mail-followup").checked ? state.resultSetId || null : null }) });
    if(packageEpoch!==state.packageEpoch || sessionEpoch!==sessionRestoreEpoch)return;
    resetAnswerExtras();renderAnswerQuestion(question);state.questions[payload.turn.turn_id]=question;state.jobId = payload.job_id; state.turnId = payload.turn.turn_id; byId("mail-start").disabled = true; byId("mail-cancel").disabled = false; byId("mail-progress").textContent = payload.waiting_for_index ? "研究已提交，等待资料导入或文字识别" : "研究已提交，正在排队";byId("mail-answer").textContent="正在研究，本次结论尚未生成。";clear(byId("mail-answer-meta"));clear(byId("mail-limitations"));clear(byId("mail-conflicts"));clear(byId("mail-findings"));clear(byId("mail-fields"));clear(byId("mail-evidence-list"));byId("mail-export").disabled=true;openModule("answer"); status(payload.waiting_for_index ? "研究已提交，附件和文字识别覆盖仍在准备" : "研究任务已提交"); pollTurn(payload.turn.turn_id);
    updateProgress(byId("mail-progress").textContent);
    state.currentResult=null;state.final=null;clear(byId("mail-result-items"));updateText("mail-result-summary","本次研究结果尚未生成");updateText("mail-result-language","研究结果生成后显示中文字段与证据");byId("mail-filter-result").disabled=true;
  }
  function renderResearchProgress(turn) {
    const events=turn.events || [],last=events[events.length-1] || {},payload=last.payload || {};
    const phases={get_package_overview:"核对资料范围",search_messages:"检索邮件",search_attachments:"检索附件",read_message:"阅读邮件原文",read_attachment:"阅读附件原文",read_segment:"核查证据原文",query_terms:"准备检索词",action_plan:"规划后续核查",field_extraction:"提取研究字段",evidence_analysis:"分析证据与矛盾",rerank:"核对检索相关性",answer:"整理研究结论",context_reviewed:"核查前后文",business_relations:"核查事项关系",budget_exhausted:"本次阅读预算已用完",reading:"逐批阅读往来邮件",review:"核查前后文",context_review:"核查前后文",synthesis:"整理研究结论"};
    const progress=turn.progress || turn.final || {},stage=progress.phase || progress.stage || turn.phase || payload.tool || payload.purpose || last.event_type;
    let phase=turn.status==="queued"?"等待开始":turn.status==="waiting_for_index"?"等待资料处理":phases[stage] || zhLabel(stage, "正在研究邮件与证据");
    if(["completed","failed","cancelled","paused"].includes(turn.status))phase=zhLabel(turn.status);
    if(turn.status==='completed'&&turn.final?.kind==='intent_screen'&&turn.final.complete===false)phase='逐封筛查已暂停（覆盖不完整）';
    const parts=[phase];
    if(turn.sufficiency)parts.push(zhLabel(turn.sufficiency));
    const modelEvents=events.filter(e=>e.event_type==="model_completed"),toolEvents=events.filter(e=>e.event_type==="tool_completed");
    const modelCalls=progress.model_calls ?? modelEvents[modelEvents.length-1]?.payload?.call,toolCalls=progress.tool_calls ?? toolEvents[toolEvents.length-1]?.payload?.call;
    if(modelCalls!=null)parts.push(`已完成 ${modelCalls} 次分析`);
    if(toolCalls!=null)parts.push(`已完成 ${toolCalls} 次检索或阅读`);
    for(const [key,label,unit] of [["messages_read","已读邮件","封"],["characters_read","已读文本","字"],["findings_count","记录发现","条"],["matched","匹配邮件","封"]])if(progress[key]!=null)parts.push(`${label} ${progress[key]} ${unit}`);
    const context=events.filter(e=>e.event_type==="context_reviewed").slice(-1)[0]?.payload || {};
    if(context.anchors!=null)parts.push(`前后文已核查 ${context.anchors} 封，尚余 ${context.unreviewed_candidates || 0} 封`);
    const failures=events.filter(e=>["tool_failed","model_failed"].includes(e.event_type)).length;
    if(failures)parts.push(`${failures} 次检索或分析遇到问题，已保留可用证据`);
    if(events.some(e=>e.event_type==="budget_exhausted"))parts.push("阅读预算已用完，未核查部分会作为限制保留");
    const coverage=(turn.final || {}).coverage || state.packageCoverage || {};
    if(coverage.attachments_without_text != null)parts.push(`附件文本缺口 ${coverage.attachments_without_text} 个`);
    if(coverage.ocr_pending)parts.push(`文字识别待处理 ${coverage.ocr_pending} 个`);
    if(coverage.failed_parts)parts.push(`资料处理失败 ${coverage.failed_parts} 个`);
    const gaps=progress.context_review?.remaining ?? progress.remaining_messages ?? progress.pending_messages;
    if(gaps!=null)parts.push(`剩余待核查 ${gaps} 封`);
    if(!["completed","failed","cancelled","paused"].includes(turn.status))parts.push("本次结论尚未生成");
    updateProgress(parts.join(" · "));
  }
  async function pollTurn(turnId, restoredTurn = null) {
    const epoch = ++state.pollEpoch;
    const tick = async () => {
      if (epoch !== state.pollEpoch) return;
      const payload = restoredTurn ? {turn:restoredTurn} : await api(`/api/mail/research-turns/${encodeURIComponent(turnId)}`);
      restoredTurn = null;
      if (epoch !== state.pollEpoch) return;
      const turn = payload.turn;
      renderResearchProgress(turn);
      state.turnId = turnId;
      if (["completed", "failed", "cancelled", "paused"].includes(turn.status)) {
        byId("mail-start").disabled = false; byId("mail-cancel").disabled = true;
        if (turn.status !== "completed") { byId("mail-answer").textContent=`研究状态：${zhLabel(turn.status)}。本次未生成完整结论。`;status(`研究状态：${zhLabel(turn.status)} ${turn.error ? display(turn.error) : ""}`, true); return; }
        const final = turn.final; state.resultSetId = (final || {}).result_set_id || turn.result_set_id || "";
        if(final){if(final.result_set_id)state.researchTurns[final.result_set_id]=turn;renderFinal(final);} await Promise.all([loadRelations(), loadMiningViews()]);
        if(epoch!==state.pollEpoch)return;
        if (state.resultSetId) await showResult(state.resultSetId);
        if(epoch!==state.pollEpoch)return;
        if(final || state.resultSetId){openModule("answer");status(final?.kind==='intent_screen'&&final.complete===false?'逐封筛查已暂停，现有结果覆盖不完整；可增加预算从检查点继续。':'研究完成，可核查结论、证据和限制');}else {byId("mail-answer").textContent="后台任务已结束，但研究结果尚不可用，请刷新历史结果或重试。";updateProgress("任务已结束，尚未取得研究结果");status("任务已结束，尚未取得研究结果",true);}return;
      }
      byId("mail-start").disabled = true; byId("mail-cancel").disabled = false;
      window.setTimeout(() => tick().catch(error=>{if(epoch===state.pollEpoch)report(error);}), 1500);
    }; return tick().catch(error=>{if(epoch===state.pollEpoch)report(error);});
  }
  let sessionRestoreEpoch = 0;
  async function restoreSession(sessionId) {
    const packageEpoch = state.packageEpoch, ticket = ++sessionRestoreEpoch;
    ++state.pollEpoch;
    ++state.resultEpoch;
    state.sessionId = sessionId; state.resultSetId = ""; state.turnId = "";
    state.currentResult = null; state.final = null; state.jobId = "";
    resetAnswerExtras();
    clear(byId("mail-answer-meta"));clear(byId("mail-findings"));clear(byId("mail-evidence-list"));clear(byId("mail-fields"));clear(byId("mail-limitations"));clear(byId("mail-conflicts"));clear(byId("mail-result-items"));
    updateText("mail-result-summary","本次研究结果尚未生成");updateText("mail-result-language","研究结果生成后显示中文字段与证据");
    byId("mail-answer").textContent = "选择已有研究后显示其结论与进度。";
    byId("mail-export").disabled = true;
    byId("mail-filter-result").disabled = true;
    byId("mail-start").disabled = false; byId("mail-cancel").disabled = true;
    if (!sessionId) {updateProgress("尚无任务");return;}
    const valid = () => packageEpoch === state.packageEpoch && ticket === sessionRestoreEpoch && sessionId === byId("mail-session").value;
    updateProgress("正在读取已有研究状态…");
    try {
      const payload = await api(`/api/mail/research-sessions/${encodeURIComponent(sessionId)}`);
      if (!valid()) return;
      const turns = payload.turns || [];
      turns.filter(turn=>turn.status === "completed" && turn.result_set_id).forEach(turn=>{state.researchTurns[turn.result_set_id]=turn;});
      const latest = turns[turns.length-1];
      if (!latest) {updateProgress("此研究会话尚未提交任务");return;}
      const turn = (await api(`/api/mail/research-turns/${encodeURIComponent(latest.turn_id)}`)).turn;
      if (!valid()) return;
      state.turnId = turn.turn_id || latest.turn_id;
      renderResearchProgress(turn);
      if (turn.status === "completed") {
        const resultId = turn.final?.result_set_id || turn.result_set_id;
        if (resultId) {state.researchTurns[resultId]=turn;await showResult(resultId);}
        else if (turn.final) renderFinal(turn.final);
        if(valid())status("已读取已有研究的完成状态与结果");
        return;
      }
      if (!["failed","cancelled","paused"].includes(turn.status)) {
        byId("mail-answer").textContent = "已有研究正在后台处理，本次结论尚未生成。";
        status("已恢复已有研究进度；继续读取后台状态，不会重新提交任务。");
      }
      await pollTurn(state.turnId, turn);
    } catch(error) {if(valid())report(error);}
  }
  async function exportCurrent() {
    if (!state.resultSetId) return;
    const packageEpoch=state.packageEpoch;
    const payload = await api("/api/mail/exports", { method: "POST", body: JSON.stringify({ result_set_id: state.resultSetId, format: "xlsx" }) });
    if(packageEpoch!==state.packageEpoch)return;
    watchJob(payload.job_id, true);
  }
  async function refreshPackageReadOnly(packageId, packageEpoch) {
    const payload=await api(`/api/mail/packages/${encodeURIComponent(packageId)}`);
    if(packageEpoch!==state.packageEpoch || packageId!==state.packageId)return;
    renderOverview({...payload.overview,coverage:payload.coverage});
    if(window.SJFXMailFoundation)await window.SJFXMailFoundation.refreshFoundation();
  }
  async function watchJob(jobId, download = false) {
    const packageId=state.packageId,packageEpoch=state.packageEpoch;
    const valid=()=>packageEpoch===state.packageEpoch && packageId===state.packageId;
    const tick = async () => {
      if(!valid())return;
      const value = await api(`/api/jobs/${encodeURIComponent(jobId)}`); const job = value.job || value;
      if(!valid())return;
      const progress=job.progress;
      const parts=[`${zhLabel(job.task_type,"后台任务")}：${zhLabel(job.status)}`];
      if(job.current_stage || job.stage)parts.push(zhLabel(job.current_stage || job.stage,"正在处理资料"));
      if(typeof progress==="number" && Number.isFinite(progress))parts.push(`${Math.max(0,Math.min(100,progress))}%`);
      else if(progress && typeof progress==="object")parts.push(display(progress));
      if(job.message)parts.push(display(job.message));
      updateProgress(parts.join(" · "));
      if(!download && packageId){await refreshPackageReadOnly(packageId,packageEpoch);if(!valid())return;}
      if (["completed", "succeeded"].includes(job.status)) {
        if (download && (job.result || {}).filename) {
          const ticket = await api("/api/download-ticket", { method: "POST", body: JSON.stringify({ filename: job.result.filename }) });
          if(!valid())return;
          const link = document.createElement("a"); link.href = ticket.download_url; link.textContent = "下载研究结果"; link.download = job.result.filename; byId("mail-progress").append(" ",link);
          if(byId("mail-answer-progress"))byId("mail-answer-progress").append(" ",link.cloneNode(true));
        }
        await loadMiningViews();if(!valid())return;
        if(job.task_type==="mail_import")await Promise.all([loadMessages(),loadSessions(),loadRelations()]);
        if(!valid())return;
        const finished=download ? "研究结果导出已完成，可下载查看" : `${zhLabel(job.task_type,"当前处理")}阶段已完成；其他处理阶段和资料缺口请查看覆盖情况`;
        if(!download)updateProgress(finished);status(finished);return;
      }
      if (["failed", "cancelled"].includes(job.status)) { status(`${zhLabel(job.status)}：${display(job.error || job.message)}`, true); return; }
      window.setTimeout(() => tick().catch(error=>{if(valid())report(error);}),2000);
    }; tick().catch(error=>{if(valid())report(error);});
  }
  function evidenceButton(label,evidenceId) {
    const button=document.createElement("button"); button.type="button"; button.className="mail-relation-link"; button.textContent=label;
    button.addEventListener("click",()=>inspectEvidence(evidenceId).catch(report)); return button;
  }
  function displayFieldValue(value) {
    if (value == null) return "具体值未确定";
    if (typeof value !== "object") return text(value);
    if (value.amount != null) return `${Number(value.amount).toLocaleString("zh-CN",{maximumFractionDigits:6})} ${({THB:"泰铢",USD:"美元",CNY:"人民币",EUR:"欧元"})[value.currency] || zhLabel(value.currency,"（币种未明确）")}`;
    if (value.quantity != null) {const prefix=value.comparison === "at_most" ? "最多 " : value.comparison === "at_least" ? "至少 " : "";const units={"ทุน":"份资助","ท่าน":"人"};return `${prefix}${value.quantity} ${units[value.unit] || zhLabel(value.unit, "（单位未明确）")}`;}
    return display(value);
  }
  function renderFields(fields) {
    const body=byId("mail-fields"); clear(body);
    const labels={amount:"金额",date:"日期",quantity:"数量",owner:"责任方",term:"条件",status:"状态",version:"版本"};
    (fields || []).forEach((f,i)=>{
      const row=document.createElement("tr");
      cell(row,zhValue(`field:${i}:subject`,f.subject_ref || f.subject_raw || "原文未明确事项"));
      cell(row,labels[f.fact_type] || zhLabel(f.fact_type,"提取字段"));
      cell(row,typeof f.value_raw==="object"?display(f.value_raw):zhValue(`field:${i}:value`,f.value_raw));
      const normalized=f.value_normalized;
      cell(row,normalized && typeof normalized==="object" || f.fact_type==="date" ? displayFieldValue(normalized) : zhValue(`field:${i}:value`,normalized ?? f.value_raw));
      const recordStatus=({mentioned:"原文提及",proposed:"提出",confirmed:"原文明确确认",modified:"修改",cancelled:"取消",conditional:"有条件"})[f.status] || zhLabel(f.status,"原文状态未记录");
      const contextStatus=f.context_status?({supported:"上下文已核查",context_qualified:"后续存在条件或变化",context_incomplete:"上下文未核查完整",chronology_unproven:"最终状态待核实",attribution_uncertain:"发言归属待核实"})[f.context_status] || zhLabel(f.context_status,"结论仍需核实"):"历史原文记录";
      cell(row,`${recordStatus} · ${contextStatus}`);
      const td=document.createElement("td");td.append(evidenceButton("核查证据",f.evidence_id),sourceDetails(`${f.subject_ref || f.subject_raw || ""}\n${typeof f.value_raw==="object"?display(f.value_raw):f.value_raw ?? ""}\n${f.quote || ""}`));row.append(td);body.append(row);
    });
    updateAnswerAppendix();
  }
  function updateAnswerAppendix() {
    const count=id=>byId(id)?.children.length || 0;
    const fields=count("mail-fields"),findings=count("mail-findings"),evidence=count("mail-evidence-list");
    const fieldsSection=byId("mail-fields-section"),findingsSection=byId("mail-findings-section"),appendix=byId("mail-evidence-appendix");
    if(fieldsSection)fieldsSection.hidden=!fields;
    if(findingsSection)findingsSection.hidden=!findings;
    if(appendix)appendix.hidden=!(fields || findings || evidence || byId("mail-recorded-answer")?.textContent || byId("mail-conflicts")?.textContent);
    updateText("mail-evidence-summary",evidence?`证据附录 · ${evidence} 段原文依据`:"证据附录");
  }
  function resetAnswerExtras() {
    for(const id of ["mail-answer-question","mail-research-details-body","mail-recorded-answer"])if(byId(id))clear(byId(id));
    for(const id of ["mail-answer-question","mail-research-details","mail-evidence-appendix","mail-recorded-answer-section","mail-limitations","mail-conflicts","mail-findings-section","mail-fields-section"]){const node=byId(id);if(node){node.hidden=true;if(node.tagName==="DETAILS")node.open=false;}}
  }
  function renderAnswerQuestion(question) {
    const node=byId("mail-answer-question");if(!node)return;
    node.textContent=question?`你的问题：${question}`:"";node.hidden=!question;
  }
  document.addEventListener("sjfx:mail-result",event=>{
    const data=event.detail||{};if(data.package_id!==state.packageId)return;
    state.resultSetId=data.result_set_id;showResult(data.result_set_id).then(()=>openModule("results")).catch(report);
  });
  function renderFinal(final) {
    state.final=final;
    byId("mail-answer").textContent=final.answer || "当前没有可确认的研究结论";
    const turn=state.researchTurns[final.result_set_id];
    renderAnswerQuestion(final.question || turn?.question || turn?.task_spec?.question || state.questions[turn?.turn_id] || state.currentResult?.definition?.question || "");
    // Source-level diagnostics stay in the stored result. Only an explicit reader-facing boundary belongs next to the answer.
    const boundary=typeof final.answer_boundary==="string"?final.answer_boundary:"";
    byId("mail-limitations").textContent=boundary;byId("mail-limitations").hidden=!boundary;
    clear(byId("mail-conflicts"));byId("mail-conflicts").hidden=true;
    const coverage=final.coverage || {},research=coverage.research || {};
    const sourceCount=new Set((final.evidence || []).map(e=>e.message_key).filter(Boolean)).size || research.matched || 0;
    byId("mail-answer-meta").textContent=`依据 ${sourceCount} 封相关邮件${final.elapsed_seconds ? ` · 用时 ${Math.round(final.elapsed_seconds)} 秒` : ""}`;
    const usage=final.token_usage||research.token_usage,budget=turn?.task_spec?.budget||{};
    if(final.kind==='intent_screen'&&final.complete===false){
      byId('mail-answer-meta').append(document.createTextNode(' · 逐封筛查已暂停，未读内容保留待判断'));
      if(turn?.turn_id){
        const resume=document.createElement('button');resume.type='button';resume.textContent='增加预算继续逐封筛查';resume.dataset.mailResumeScreen='';
        resume.addEventListener('click',async()=>{
          const packageEpoch=state.packageEpoch,turnId=turn.turn_id;resume.disabled=true;
          try{
            const saved=(await api(`/api/mail/research-turns/${encodeURIComponent(turnId)}`)).turn;
            if(packageEpoch!==state.packageEpoch)return;
            if(state.resultSetId!==final.result_set_id)throw new Error('当前查看的结果已变化，请从当前结果继续');
            if(saved?.task_spec?.mode!=='intent_screen'||saved?.final?.result_set_id!==final.result_set_id||saved.final.complete===true)throw new Error('筛查结果已变化，请刷新后继续');
            const previous=saved.task_spec,checkpoint=saved.final,usage=checkpoint.token_usage||{};
            const used=Number(usage.total_tokens||0)+Number(usage.reserved_tokens||0);
            const calls=Number(checkpoint.model_calls||0);
            const model_call_budget=Math.min(32768,Math.max(Number(previous.model_call_budget||1)*2,calls+1));
            const max_tokens=Math.min(100000000,Math.max(Number(previous.max_tokens||5000000)*2,used+36000));
            if(model_call_budget<=calls||max_tokens<=used)throw new Error('筛查预算已达到界面上限，请核查现有结果与资料缺口');
            const value=await api(`/api/mail/intent-analyses/${encodeURIComponent(turnId)}/resume`,{method:'POST',body:JSON.stringify({model_call_budget,max_tokens})});
            if(packageEpoch!==state.packageEpoch)return;
            state.turnId=turnId;state.jobId=value.job_id;byId('mail-start').disabled=true;byId('mail-cancel').disabled=false;
            status('逐封筛查已恢复，继续原范围内的邮件检查点。');updateProgress('继续逐封筛查；已有判定与累计消耗保留。');pollTurn(turnId);
          }catch(error){if(packageEpoch===state.packageEpoch){report(error);resume.disabled=false;}}
        });byId('mail-answer-meta').append(' ',resume);
      }
    }
    const detailsBody=byId("mail-research-details-body"),details=byId("mail-research-details");
    if(detailsBody){clear(detailsBody);if(usage){const n=value=>Number(value||0).toLocaleString('zh-CN');detailsBody.append(document.createTextNode(`token 已用 ${n(usage.total_tokens)} / ${usage.max_tokens==null?'未设上限':n(usage.max_tokens)}（实际 ${n(usage.actual_tokens)}，估算 ${n(usage.estimated_tokens)}，预留 ${n(usage.reserved_tokens)}）`));}if(final.research_version){const gap=final.gap_search||research.gap_search||{};detailsBody.append(document.createTextNode(`${usage?'\n':''}分析 ${research.model_calls||0} 次 · 检索或阅读 ${research.tool_calls||0} 次${gap.rounds!=null?' · 补检 '+(Array.isArray(gap.rounds)?gap.rounds.length:gap.rounds)+' 轮':''}`));}if(details)details.hidden=!detailsBody.textContent;}
    const exhausted=Boolean(usage?.exhausted||research.deadline_reached||research.tool_limit_reached||(budget.max_model_calls&&research.model_calls>=budget.max_model_calls));
    if(turn&&final.research_version&&exhausted&&turn.sufficiency!=='sufficient'){
      const resume=document.createElement('button');resume.type='button';resume.textContent='增加预算继续研究';resume.dataset.mailResumeResearch='';resume.addEventListener('click',async()=>{const packageEpoch=state.packageEpoch,turnId=turn.turn_id;resume.disabled=true;try{const saved=(await api(`/api/mail/research-turns/${encodeURIComponent(turnId)}`)).turn;if(packageEpoch!==state.packageEpoch)return;if(saved?.final?.result_set_id!==final.result_set_id)throw new Error('研究结果已变化，请刷新后继续');const previous=saved.task_spec?.budget||{},ceilings={max_tokens:100000000,max_model_calls:32768,max_tool_calls:100000,max_seconds:604800},increased={};Object.entries(ceilings).forEach(([key,maximum])=>{const old=Number(previous[key]||0);if(old<maximum)increased[key]=Math.min(maximum,Math.max(old+1,old*2));});if(!Object.keys(increased).length)throw new Error('研究预算已达到服务上限，请核查现有结果与资料缺口');const value=await api(`/api/mail/research-turns/${encodeURIComponent(turnId)}/resume`,{method:'POST',body:JSON.stringify({budget:increased})});if(packageEpoch!==state.packageEpoch)return;state.turnId=turnId;state.jobId=value.job_id;byId('mail-start').disabled=true;byId('mail-cancel').disabled=false;updateProgress('已增加预算，将在原资料范围内重新检索和核验；此前消耗继续累计。');pollTurn(turnId);}catch(error){if(packageEpoch===state.packageEpoch){report(error);resume.disabled=false;}}});resume.title='提高原研究预算上限，累计已有消耗，并在原资料范围内继续核查';byId('mail-answer-meta').append(' ',resume);
    }
    const findings=byId("mail-findings");clear(findings);
    const recorded=final.evidence_appendix?.recorded_answer || "";
    updateText("mail-recorded-answer",recorded);
    const recordsSection=byId("mail-recorded-answer-section");if(recordsSection)recordsSection.hidden=!recorded;
    const seen=new Set();
    (final.findings || []).forEach(f=>{
      const value=f.text || f.claim_text || f.statement;if(!value || seen.has(value))return;seen.add(value);
      const li=document.createElement("li");li.textContent=display(value);
      const ids=[...new Set([f.evidence_id,...(f.evidence_ids || [])].filter(Boolean))];
      ids.forEach(id=>li.append(" ",evidenceButton("查看原文",id)));findings.append(li);
    });
    (final.evidence || []).forEach(e=>{if(e.subject_raw)state.titles[e.message_key]=e.subject_raw;});renderFields(final.fields);renderEvidence(final.evidence);
  }
  async function showResult(id) {
    const packageEpoch=state.packageEpoch, sessionEpoch=sessionRestoreEpoch, resultEpoch=++state.resultEpoch;
    const payload=await api(`/api/mail/result-sets/${encodeURIComponent(id)}`);const result=payload.result_set;
    if(packageEpoch!==state.packageEpoch || sessionEpoch!==sessionRestoreEpoch || resultEpoch!==state.resultEpoch)return;
    Object.keys(state.chinese).filter(k=>k.startsWith("field:") || k.startsWith("evidence:")).forEach(k=>delete state.chinese[k]);
    state.currentResult=result;
    if(result.source_turn_id&&!state.researchTurns[id])state.researchTurns[id]={turn_id:result.source_turn_id};
    state.resultSetId=id;byId("mail-export").disabled=false;byId("mail-filter-result").disabled=false;byId("mail-result-history").value=id;
    if(result.final)renderFinal(result.final);else {resetAnswerExtras();byId("mail-answer").textContent=(result.definition || {}).question ? `研究目标：${result.definition.question}` : "筛选后的结果";clear(byId("mail-evidence-list"));clear(byId("mail-findings"));}
    byId("mail-result-summary").textContent=`${result.item_count} 条记录${result.parent_result_set_id ? " · 来源于上一结果集" : " · 全资料包检索"}`;
    if(result.final){
      const conclusion=document.createElement('button');conclusion.type='button';conclusion.dataset.mailResultConclusion='';
      conclusion.textContent=result.final.kind==='intent_screen'&&result.final.complete===false?'查看结论与继续分析':'查看研究结论';
      conclusion.addEventListener('click',()=>{openModule('answer');byId('mail-answer')?.scrollIntoView({block:'start'});});
      byId('mail-result-summary').append(' ',conclusion);
    }
    renderFields((result.definition || {}).fields || []);renderResultItems(result);
    translateResult(result).catch(report);
  }

  async function loadMiningViews() {
    if (!state.packageId) return;
    const packageEpoch=state.packageEpoch;
    const base=`/api/mail/packages/${encodeURIComponent(state.packageId)}`;
    const scopeQuery=window.SJFXMailFoundation?window.SJFXMailFoundation.query(window.SJFXMailFoundation.currentScope()):"";
    const [coverage,results,conversations,versions,network]=await Promise.all([api(`${base}/coverage`),api(`${base}/result-sets`),api(`${base}/conversations?${window.SJFXMailFoundation?window.SJFXMailFoundation.query(window.SJFXMailFoundation.currentScope()):""}`),api(`${base}/attachment-versions?${scopeQuery}`),api(`${base}/relations/network?${scopeQuery}`).catch(error=>{
      if(error.status!==409)throw error;
      if(packageEpoch===state.packageEpoch){
        const node=byId("mail-network");clear(node);
        const message=document.createElement("p");message.textContent=error.message || "资料正在变化，请重新加载图谱";
        const retry=document.createElement("button");retry.type="button";retry.textContent="重新加载图谱";retry.dataset.mailNetworkRetry="";
        retry.addEventListener("click",()=>{if(packageEpoch===state.packageEpoch)loadMiningViews().catch(report);});
        node.append(message,retry);
      }
      return null;
    })]);
    if(packageEpoch!==state.packageEpoch)return;
    const gaps=byId("mail-coverage-gaps");clear(gaps);
    (coverage.items || []).forEach((item,i)=>{const li=document.createElement("li");const button=document.createElement("button");button.type="button";button.className="mail-relation-link";button.textContent=`${zhLabel(item.status)} · ${item.filename_raw || `未命名附件 ${i+1}`}`;button.addEventListener("click",()=>inspectAttachment(item.message_key,item.mime_path,item.filename_raw || `未命名附件 ${i+1}`).catch(report));li.append(button);gaps.append(li);});
    if (!coverage.total) {const li=document.createElement("li");li.textContent="当前附件没有待处理缺口";gaps.append(li);}
    const history=byId("mail-result-history");clear(history);history.append(option("选择结果集…",""));(results.items || []).forEach(item=>history.append(option(`${date(item.created_at)} · ${item.item_count} 条${item.parent_result_set_id ? " · 追问" : ""}`,item.result_set_id)));history.value=state.resultSetId;
    state.conversationItems=conversations.items || []; renderConversationList();
    state.versionItems=versions.items || []; renderVersionList();
    if(network)renderNetwork(network.network || {});
  }
  // 分页切片：全量条目始终保留在 state 中，每页只渲染 PANEL_PAGE_SIZE 条。
  function pageSlice(items,offset){
    const total=items.length;
    const maxOffset=total?Math.floor((total-1)/PANEL_PAGE_SIZE)*PANEL_PAGE_SIZE:0;
    const start=Math.max(0,Math.min(offset,maxOffset));
    return {items:items.slice(start,start+PANEL_PAGE_SIZE),offset:start};
  }
  // 更新页码文字与翻页按钮状态；控件缺失时静默跳过。
  function updatePager(prefix,total,offset,count){
    const label=byId(prefix+"-page");
    if(label)label.textContent=total?`${offset+1}–${offset+count} / ${total}`:"0 条";
    const prev=byId(prefix+"-prev"); if(prev)prev.disabled=offset===0;
    const next=byId(prefix+"-next"); if(next)next.disabled=offset+PANEL_PAGE_SIZE>=total;
  }
  // 会话列表：不再截断到 150 条，改为分页显示全部。
  function renderConversationList(){
    const list=byId("mail-conversations"); if(!list)return;
    clear(list);
    const items=state.conversationItems || [];
    const page=pageSlice(items,state.conversationOffset);
    state.conversationOffset=page.offset;
    if(!items.length){const li=document.createElement("li");li.textContent="当前资料包没有会话记录";list.append(li);}
    page.items.forEach(item=>{const li=document.createElement("li");const button=document.createElement("button");button.type="button";button.textContent=`${item.subject_normalized || "无主题"} · ${item.message_count} 封 · ${zhLabel(item.merge_basis,"归并依据未记录")}`;button.addEventListener("click",()=>showConversation(item.conversation_id).catch(report));li.append(button);list.append(li);});
    updatePager("mail-conversations",items.length,page.offset,page.items.length);
  }
  // 附件传递与版本：不再截断到 200 条，改为分页显示全部。
  function renderVersionList(){
    const list=byId("mail-versions"); if(!list)return;
    clear(list);
    const items=state.versionItems || [];
    const page=pageSlice(items,state.versionOffset);
    state.versionOffset=page.offset;
    if(!items.length){const li=document.createElement("li");li.textContent="当前资料包没有附件版本记录";list.append(li);}
    page.items.forEach(item=>{const li=document.createElement("li");const button=document.createElement("button");button.type="button";const title=item.filename_raw || (/^[a-f0-9]{32,}$/i.test(item.chain_key || "") ? "未命名附件" : item.chain_key) || "未命名附件";button.textContent=`${title} · 版本顺序 ${item.version_ordinal} · ${date(item.sent_at_utc)}`;button.addEventListener("click",()=>inspectAttachment(item.message_key,item.mime_path,title).catch(report));li.append(button);list.append(li);});
    updatePager("mail-versions",items.length,page.offset,page.items.length);
  }
  async function showConversation(id) {
    if(window.SJFXMailFoundation)return window.SJFXMailFoundation.openConversation(id);
    const value=(await api(`/api/mail/conversations/${encodeURIComponent(id)}`)).conversation;const node=byId("mail-conversation-detail");clear(node);
    (value.edges || []).forEach(e=>{const row=document.createElement("div");row.className=e.status === "candidate" ? "mail-candidate-edge" : "mail-exact-edge";const button=document.createElement("button");button.textContent=`${e.source_subject || "无主题邮件"} → ${e.target_subject || "关联邮件未收录或未命名"}（${zhLabel(e.status)}）`;button.addEventListener("click",()=>inspectMessage(e.source_message_key).catch(report));row.append(button);node.append(row);});
    if (!(value.edges || []).length) node.textContent="此邮件没有可核验的回复引用关系。";
  }
  const screenButton=document.createElement("button");screenButton.type="button";screenButton.textContent="按目标逐封筛选";
  screenButton.addEventListener("click",()=>window.SJFXMailAnalysis?.open({kind:"screen",question:byId("mail-question")?.value||"",package_id:state.packageId,scope:window.SJFXMailFoundation?.currentScope()||{},base_result_set_id:byId("mail-followup")?.checked?state.resultSetId||undefined:undefined}).catch(report));
  byId("mail-start")?.after(screenButton);
  function renderNetwork(network) {
    if(window.SJFXMailGraph){
      const scope=window.SJFXMailFoundation?.currentScope()||{};
      window.SJFXMailGraph.render(byId("mail-network"),network,{
        request:api,
        onPerson:(n,graphScope)=>window.SJFXMailPeople?.select(n.entity_id&&(graphScope?.view||network.view)==="person"?{entity_id:n.entity_id,address:n.addresses?.[0]}:n.addresses?.[0]||n.address,graphScope||scope),
        onPair:(a,b,graphScope)=>window.SJFXMailAnalysis?.open({kind:"pair",package_id:state.packageId,scope:graphScope||scope,
          address:a.addresses?.[0]||a.address,counterpart:b.addresses?.[0]||b.address,
          ...((graphScope?.view||network.view)==="person"&&a.entity_id?{entity_id:a.entity_id}:{}),
          ...((graphScope?.view||network.view)==="person"&&b.entity_id?{counterpart_entity_id:b.entity_id}:{})}),
        onMessage:(k,graphScope)=>graphScope&&window.SJFXMailFoundation?window.SJFXMailFoundation.openMessage(k,graphScope):inspectMessage(k)});return;
    }
    const node=byId("mail-network");clear(node);const all=network.nodes || [];const nodes=all.slice(0,30);const ns="http://www.w3.org/2000/svg";const svg=document.createElementNS(ns,"svg");svg.setAttribute("viewBox","0 0 800 600");svg.setAttribute("aria-label","邮箱通信关系图");const positions={};
    nodes.forEach((n,i)=>{const a=i*2*Math.PI/Math.max(1,nodes.length);positions[n.address]=[400+260*Math.cos(a),300+230*Math.sin(a)];});
    (network.edges || []).forEach(e=>{const a=positions[e.from_address],b=positions[e.to_address];if(!a||!b)return;const line=document.createElementNS(ns,"line");line.setAttribute("x1",a[0]);line.setAttribute("y1",a[1]);line.setAttribute("x2",b[0]);line.setAttribute("y2",b[1]);line.setAttribute("stroke","#90a6ba");line.setAttribute("stroke-width",Math.min(8,1+Math.log2(1+e.message_count)));const title=document.createElementNS(ns,"title");title.textContent=`${e.from_address} → ${e.to_address}：${e.message_count} 封唯一邮件`;line.append(title);line.addEventListener("click",()=>{const list=document.createElement("ul");(e.message_keys || []).forEach((k,i)=>{const li=document.createElement("li"),b=document.createElement("button");b.type="button";b.textContent=state.titles[k] ? zhTitle(k,state.titles[k]) : `查看关联邮件 ${i+1}`;b.addEventListener("click",()=>inspectMessage(k).catch(report));li.append(b);list.append(li);});node.append(list);});svg.append(line);});
    nodes.forEach(n=>{const [x,y]=positions[n.address];const circle=document.createElementNS(ns,"circle");circle.setAttribute("cx",x);circle.setAttribute("cy",y);circle.setAttribute("r",n.bridge ? 8 : 5);circle.setAttribute("fill",n.bridge ? "#d99335" : "#3189b8");const label=document.createElementNS(ns,"text");label.setAttribute("x",x+8);label.setAttribute("y",y);label.setAttribute("font-size","10");label.textContent=n.display_name;svg.append(circle,label);});node.append(svg);const note=document.createElement("p");note.textContent=`显示 ${nodes.length}/${all.length} 个邮箱；按逻辑邮件统计。精确回复样本 ${(network.reply_latency || {}).exact_reply_count || 0} 条。`;node.append(note);
  }
  async function queueIndex(stage) {
    if (!state.packageId) throw new Error("请先选择资料包");
    const packageEpoch=state.packageEpoch;
    const payload=await api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/index`,{method:"POST",body:JSON.stringify({stage})});
    if(packageEpoch!==state.packageEpoch)return;
    for(const job of payload.jobs || []) watchJob(job.job_id);
    status("索引任务已提交");
  }
  async function translateEvidence() {if(state.previewPayload){const pages=state.previewPages.slice();for(const page of pages){if(page.status!=="completed")await presentSource({...state.previewPayload,offset:page.offset,retry:true},"",true);}if(pages.every(p=>p.status==="completed"))status("现有译文已完成，可开启中外文对照核查");}}

  function report(error) { status(error?.message ? display(error.message) : "邮件研究操作失败", true); }
  byId("mail-token").addEventListener("click", configureToken);
  byId("mail-token-form").addEventListener("submit", saveToken);
  byId("mail-token-cancel").addEventListener("click", () => byId("mail-token-dialog").close());
  byId("mail-token-dialog").addEventListener("close", () => { byId("mail-token-input").value = ""; });
  byId("mail-refresh").addEventListener("click", async () => {
    const packageId=state.packageId, sessionId=byId("mail-session").value, sessionTicket=sessionRestoreEpoch;
    try {
      await loadPackages(packageId);
      if(sessionId && sessionTicket===sessionRestoreEpoch && packageId===state.packageId && Array.from(byId("mail-session").options).some(option=>option.value===sessionId)) {
        byId("mail-session").value=sessionId;await restoreSession(sessionId);
      }
    } catch(error) {report(error);}
  });
  byId("mail-package").addEventListener("change", () => loadPackage().catch(report));
  byId("mail-apply-filter").addEventListener("click", () => {state.offset=0;if(window.SJFXMailFoundation)window.SJFXMailFoundation.emitScope();loadMessages().catch(report);loadMiningViews().catch(report);loadRelations().catch(report);});
  byId("mail-show-unreviewed-relations").addEventListener("change", () => loadRelations().catch(report));
  byId("mail-question-form").addEventListener("submit", (event) => runResearch(event).catch(report));
  byId("mail-new-session").addEventListener("click", () => {
    // A new session must invalidate the previous turn's polling closure.  The
    // old request may still resolve after the session selector changes; the
    // epoch check prevents stale progress/results from being painted into the
    // new session.
    byId("mail-session").value="";
    restoreSession("");
    byId("mail-answer").textContent = "输入你要查找、比较或提取的内容。";
    ensureSession().then(id => {if(id && id===state.sessionId)status("已创建研究会话");}).catch(report);
  });
  byId("mail-index").addEventListener("click",()=>queueIndex("attachments").catch(report));
  byId("mail-embed").addEventListener("click",()=>queueIndex("embedding").catch(report));
  byId("mail-factextract").addEventListener("click",()=>queueIndex("facts").catch(report));
  byId("mail-ocr").addEventListener("click",()=>queueIndex("ocr").catch(report));
  byId("mail-cancel").addEventListener("click",()=>{if(state.turnId) api(`/api/mail/research-turns/${encodeURIComponent(state.turnId)}/cancel`,{method:"POST",body:"{}"}).then(()=>status("正在取消研究")).catch(report);});
  byId("mail-result-history").addEventListener("change",e=>{if(e.target.value) showResult(e.target.value).catch(report);});
  byId("mail-filter-result").addEventListener("click",()=>{if(state.resultSetId) api(`/api/mail/result-sets/${encodeURIComponent(state.resultSetId)}/filter`,{method:"POST",body:JSON.stringify({has_attachment:true})}).then(async p=>{await loadMiningViews();await showResult(p.result_set.result_set_id);}).catch(report);});
  byId("mail-session").addEventListener("change",e=>{restoreSession(e.target.value).catch(report);});

  byId("mail-export").addEventListener("click", () => exportCurrent().catch(report));
  byId("mail-translate").addEventListener("click", () => translateEvidence().catch(report));
  byId("mail-import-form").addEventListener("submit", (event) => { event.preventDefault(); const path = byId("mail-source-path").value.trim(); const display_name = byId("mail-package-name").value.trim(); if (!path) return report(new Error("请输入服务器邮件来源路径")); api("/api/mail/packages", { method: "POST", body: JSON.stringify({ source: { path }, display_name, options: { build_basic_attachment_index: true } }) }).then(async payload => { status("邮件资料包已提交导入");await loadPackages(payload.package.package_id);if(payload.job_id && state.packageId===payload.package.package_id)watchJob(payload.job_id); }).catch(report); });
  byId("mail-entity-form").addEventListener("submit",event=>{event.preventDefault();if(!state.packageId)return;const addresses=byId("mail-entity-addresses").value.split(/[,，;；\s]+/).filter(Boolean);api("/api/mail/entities/confirm",{method:"POST",body:JSON.stringify({package_id:state.packageId,display_name:byId("mail-entity-name").value.trim(),addresses})}).then(()=>loadMiningViews()).then(()=>{window.SJFXMailFoundation?.emitScope();status("邮箱归并已记录人工确认依据");}).catch(report);});
  byId("mail-prev").addEventListener("click",()=>{state.offset=Math.max(0,state.offset-25);loadMessages().catch(report);});
  byId("mail-next").addEventListener("click",()=>{state.offset+=25;loadMessages().catch(report);});
  on("mail-conversations-prev",()=>{state.conversationOffset=Math.max(0,state.conversationOffset-PANEL_PAGE_SIZE);renderConversationList();});
  on("mail-conversations-next",()=>{state.conversationOffset+=PANEL_PAGE_SIZE;renderConversationList();});
  on("mail-versions-prev",()=>{state.versionOffset=Math.max(0,state.versionOffset-PANEL_PAGE_SIZE);renderVersionList();});
  on("mail-versions-next",()=>{state.versionOffset+=PANEL_PAGE_SIZE;renderVersionList();});
  byId("mail-more-translation").addEventListener("click",()=>{if(state.previewPayload && state.nextTranslationOffset!=null)presentSource({...state.previewPayload,offset:state.nextTranslationOffset},"",true).catch(report);});
  document.querySelectorAll("[data-mail-example]").forEach(button=>button.addEventListener("click",()=>{byId("mail-question").value=button.dataset.mailExample;byId("mail-question").focus();}));
  byId("mail-bilingual").addEventListener("change",()=>{if(state.previewPages)renderTranslationSegments();});
  byId("mail-retry-result").addEventListener("click",()=>{if(state.currentResult)translateResult(state.currentResult,true).catch(report);});
  window.SJFXMailReader={inspectMessage,inspectAttachment,presentSource};
  // Home tasks only prepare a workflow. Model work starts at its existing submit button.
  async function startTask(options={}) {
    const kind=options.kind;
    if(!['event','screen','intent','people'].includes(kind))throw new Error('请选择一种邮件任务');
    const packageId=byId('mail-package')?.value;
    if(!packageId){byId('mail-package')?.focus();throw new Error('请先选择资料包，再开始这项任务');}
    if(kind==='intent'){
      openModule('messages');byId('mail-query')?.focus();
      status('先打开一封邮件，再点击“通信意图”分析；阅读原文也可以查看人物与会话。');
      return {module:'messages',focusId:'mail-query'};
    }
    if(kind==='people'){
      openModule('network');document.querySelector('.mp-contacts input')?.focus();
      status('先选择联系人查看往来邮件，再分析这个人；点击通信对象可查看并分析双方往来。');
      return {module:'network',focusId:null};
    }
    openModule('research');
    state.sessionId='';state.resultSetId='';
    if(byId('mail-session'))byId('mail-session').value='';
    const followup=byId('mail-followup');if(followup)followup.checked=false;
    const input=byId('mail-question');
    const question=options.question || (kind==='screen'?'逐封查找与报价调整相关的邮件，记录提出的条件、回应与后续变化。':'谁提出了报价调整？双方如何回应，最后有哪些仍需确认的条件？');
    if(input){input.value=question;input.focus();}
    if(kind==='screen'){
      if(!window.SJFXMailAnalysis?.open)throw new Error('分析界面尚未就绪，请刷新后重试');
      const dialog=await window.SJFXMailAnalysis.open({kind:'screen',package_id:packageId,question,scope:window.SJFXMailFoundation?.currentScope()||{},autoStart:false});
      dialog.querySelector('textarea')?.focus();
      status('逐封筛查已准备：修改研究目标、核对当前邮件范围，然后点击“开始分析”。');
    } else status('已准备一个事件问题；修改问题并核对筛选范围，点击“开始研究”提交。');
    return {module:'research',focusId:kind==='screen'?null:'mail-question'};
  }
  window.SJFXMailResearch={startTask};
  async function loadWorkspaceGuide(){
    const host=byId('mail-workspace-capabilities');if(!host)return;
    try{
      const guide=await api('/api/mail/workspace-guide');clear(host);
      const paragraph=value=>{if(!value)return;const p=document.createElement('p');p.textContent=value;host.append(p);};
      paragraph(guide.imports?.input);
      const formats=guide.imports?.formats||[];
      if(formats.length){const list=document.createElement('ul');formats.forEach(format=>{const item=document.createElement('li');item.textContent=`${format.label||format.format} · ${format.available?'当前服务器可导入':'当前服务器暂不可导入'}${format.description?'：'+format.description:''}`;list.append(item);});host.append(list);}
      (guide.imports?.scope_notes||[]).forEach(paragraph);
      (guide.scope?.notes||[]).forEach(paragraph);
      (guide.analysis_modes||[]).forEach(mode=>{
        const details=document.createElement('details'),summary=document.createElement('summary');
        summary.textContent=`${mode.label||'邮件分析'} · ${mode.available?'可开始分析':'分析服务尚未就绪'}`;details.append(summary);
        [['purpose','用途'],['input','需要提供'],['output','得到的结果']].forEach(([key,label])=>{if(mode[key]){const p=document.createElement('p');p.textContent=`${label}：${mode[key]}`;details.append(p);}});
        (mode.scope_notes||[]).forEach(value=>{const p=document.createElement('p');p.textContent=value;details.append(p);});host.append(details);
      });
      if(guide.runtime){
        const ready=guide.runtime.model_available&&guide.runtime.queue_available;
        const readiness=byId('mail-workspace-readiness');
        const message=`当前服务器分析模型：${guide.runtime.model_available?'已配置':'尚未配置'}；后台队列：${guide.runtime.queue_available?'可提交任务':'尚未就绪'}。${ready?'选择任务后，点击开始按钮提交分析。':'分析暂不可开始；仍可查看已导入邮件与原文。'}`;
        if(guide.runtime.budgets){const b=guide.runtime.budgets;paragraph(`默认阅读预算：研究 ${Number(b.research_model_calls||0).toLocaleString('zh-CN')} 次分析、${Number(b.tokens||0).toLocaleString('zh-CN')} token，最长 ${Math.round(Number(b.seconds||0)/3600)} 小时；充分完成后会提前结束，可随时停止并保存进度。`);}
        paragraph(message);if(readiness){readiness.textContent=message;readiness.classList.toggle('mail-error',!ready);}
      }
    }catch(_){host.textContent='暂时无法读取当前服务器能力。可先浏览已导入资料；导入格式与分析服务是否可用，以提交后的服务提示为准。';const readiness=byId('mail-workspace-readiness');if(readiness){readiness.textContent='当前服务器分析服务状态尚未确认；可先浏览资料，连接服务后会更新。';readiness.classList.remove('mail-error');}}
  }
  document.addEventListener("sjfx:mail-scope",event=>{
    const detail=event.detail || {};if(detail.source!=="people" || (detail.package_id && detail.package_id!==state.packageId))return;
    const filters=detail.filters || detail;
    for(const [key,id] of Object.entries({q:"mail-query",participant:"mail-participant",date_from:"mail-date-from",date_to:"mail-date-to"})){const input=byId(id);if(input)input.value=filters[key] || "";}
    byId("mail-with-attachment").checked=filters.has_attachment===true || filters.has_attachment==="true";
    state.offset=0;Promise.all([loadMessages(),loadMiningViews(),loadRelations()]).catch(report);
  });
  loadPackages().catch(report);
  loadWorkspaceGuide();
})();


