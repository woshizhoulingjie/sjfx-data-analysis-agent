(() => {
  "use strict";
  const state = { packageId: "", sessionId: "", resultSetId: "", evidenceId: "", jobId: "", turnId: "", pollEpoch: 0, titles: {}, chinese: {}, previewEpoch: 0, resultEpoch: 0, listEpoch: 0, offset: 0, final: null };
  const byId = (id) => document.getElementById(id);
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
      byId("mail-token-error").textContent = "请输入有效的 API Token";
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
    loadPackages(state.packageId).then(() => status("邮件服务已连接，选择资料包后开始研究")).catch(report);
  }
  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const value = token();
    if (value) headers["X-SJFX-Token"] = value;
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { ...options, headers });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) throw new Error("未通过鉴权，请点击右上角 Token 设置访问凭证");
    if (!response.ok || !payload.ok) throw new Error(payload.error || "邮件研究请求失败");
    return payload;
  }
  const text = (value) => String(value == null ? "" : value);
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function option(label, value) { const node = document.createElement("option"); node.value = value; node.textContent = label; return node; }
  function cell(row, value) { const node = document.createElement("td"); node.textContent = text(value); row.appendChild(node); }
  const LABELS = {inventory:"待导入",importing:"导入中",attachment_index_ready:"附件就绪",body_index_ready:"正文就绪",queued:"排队中",waiting_for_index:"等待资料处理",running:"处理中",completed:"已完成",failed:"失败",cancelled:"已取消",paused:"已暂停",partial:"覆盖不完整",sufficient:"证据较充分",insufficient:"证据不足",conflicting:"存在冲突",not_started:"未开始",ocr_pending:"等待文字识别",unsupported:"暂不支持",ready:"已就绪",asserted:"原文支持",inferred:"待核实推断",confirmed:"已确认",candidate:"候选关联",exact:"精确关联",reply_to:"回复",references:"引用",from:"发件人",to:"收件人",cc:"抄送",bcc:"密送",mail_research:"邮件研究",mail_import:"邮件导入",mail_extract:"附件解析",mail_ocr:"文字识别",mail_embed:"语义索引",mail_factextract:"字段提取",mail_export:"结果导出"};
  const zhLabel = value => LABELS[value] || value || "";
  const isChinese = value => /[\u3400-\u9fff]/.test(text(value)) && !/[\u0e00-\u0e7f\u3040-\u30ff\uac00-\ud7af]/.test(text(value)) && !/[A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){2,}/.test(text(value));
  const zhTitle = (key, original) => state.chinese[`title:${key}`] || (isChinese(original) ? original : "主题翻译中…");
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
    state.previewPages.slice().sort((a,b)=>a.offset-b.offset).forEach(page=>{
      const segments=(page.value || {}).segments || [];
      if(!segments.length){const p=document.createElement("p");p.textContent=(page.value || {}).text || "正在准备中文译文…";output.append(p);return;}
      segments.forEach(segment=>{
        const row=document.createElement("section");row.className="mail-translation-segment";
        const label=document.createElement("div");label.className="mail-segment-label";label.textContent=`第 ${segment.ordinal+1} 段 · ${segment.status==="pending"?"翻译中":segment.status==="failed"?"需要重试":"已完成"}`;
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
        if(value){const page={offset,value,status:p.status};const existing=state.previewPages.findIndex(x=>x.offset===offset);if(existing<0)state.previewPages.push(page);else state.previewPages[existing]=page;renderTranslationSegments();byId("mail-original").textContent=state.previewPages.slice().sort((a,b)=>a.offset-b.offset).map(p=>(p.value.segments || []).map(s=>s.source).join("")).join("");state.previewOriginal=byId("mail-original").textContent;}
        const done=p.completed_segments || 0,total=p.total_segments || 0;
        byId("mail-translation-status").textContent=p.status==="pending" ? `${p.phase==="queued"?"等待翻译":"正在翻译"} · 已完成 ${done}/${total} 段` : p.status==="completed"?`中文翻译完成 · ${done} 段，可与原文对照`:"部分段落暂未完成，已保留可用译文";
        const title=(p.values || {}).title;
        if(title && title.text && byId("mail-detail").querySelector("h2"))byId("mail-detail").querySelector("h2").textContent=title.text;
        if((p.metadata || {}).notice)byId("mail-translation-status").textContent=p.metadata.notice;
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
      const p=await presentation({kind:"result",result_set_id:result.result_set_id,offset,retry},()=>epoch===state.resultEpoch,p=>{absorbChinese(p);renderFields((result.definition || {}).fields || []);renderEvidence((result.final || {}).evidence || []);renderResultItems(result);byId("mail-result-language").textContent=p.status==="pending"?"正在补齐中文字段与证据译文…":"中文显示已更新；原文可展开核对";});
      if(!p || epoch!==state.resultEpoch)return;
      offset=(p.metadata || {}).next_offset;
      if(p.status!=="completed")byId("mail-result-language").textContent="部分译文暂不可用；原始证据仍可核查";
    } while(offset!=null);
  }
  function renderEvidence(evidence) {
    const list=byId("mail-evidence-list");clear(list);
    (evidence || []).forEach((e,i)=>{const li=document.createElement("li"),quote=zhValue(`evidence:${e.evidence_id}`,e.quote_text);li.append(evidenceButton(`${i+1}. ${quote.slice(0,240)}`,e.evidence_id),sourceDetails(e.quote_text));list.append(li);});
  }
  function renderResultItems(result) {
    const list=byId("mail-result-items");clear(list);
    (result.items || []).slice(0,200).forEach(item=>{const li=document.createElement("li"),button=document.createElement("button");button.type="button";button.className="mail-relation-link";button.textContent=`${({matched:"匹配",unknown:"待核查",failed:"失败"})[item.status] || "保留"} · ${zhTitle(item.item_id,state.titles[item.item_id])}`;button.addEventListener("click",()=>inspectMessage(item.item_id).catch(report));li.append(button);list.append(li);});
    if((result.items || []).length>200){const li=document.createElement("li");li.textContent="当前显示前 200 条，完整记录可导出查看";list.append(li);}
  }
  function renderOverview(overview) {
    const node = byId("mail-overview"); clear(node);
    const counts = (overview || {}).counts || {};
    [["逻辑邮件", counts.logical_messages], ["原始副本", counts.occurrences], ["正文就绪 (逻辑)", counts.messages_with_body], ["正文就绪 (副本)", counts.occurrences_with_body], ["显式附件 (逻辑)", counts.explicit_attachments], ["显式附件 (副本)", counts.explicit_attachment_occurrences], ["内嵌资源 (副本)", counts.inline_resource_occurrences], ["发件邮箱", counts.sender_addresses], ["参与地址", counts.participant_addresses]].forEach(([label, value]) => {
      const card = document.createElement("div"); const number = document.createElement("b"); number.textContent = text(value || 0); card.append(number, document.createTextNode(label)); node.appendChild(card);
    });
    const coverage = overview.coverage || {};
    byId("mail-coverage").textContent = `覆盖：附件已抽取 ${coverage.attachments_with_text || 0}/${coverage.explicit_attachments || 0}；OCR 待处理 ${coverage.ocr_pending || 0}；失败 ${coverage.failed_parts || 0}`;
  }
  async function loadPackages(selectId) {
    const payload = await api("/api/mail/packages");
    const select = byId("mail-package"); clear(select); select.appendChild(option("选择邮件资料包...", ""));
    payload.packages.forEach((item) => select.appendChild(option(`${item.display_name} (${zhLabel(item.status)})`, item.package_id)));
    if (selectId) { select.value = selectId; state.packageId = selectId; await loadPackage(); }
  }
  async function loadPackage() {
    state.packageId = byId("mail-package").value;
    state.pollEpoch++; state.previewEpoch++; state.resultEpoch++; state.listEpoch++;state.previewPages=[];state.previewPayload=null;state.currentResult=null;clear(byId("mail-preview"));clear(byId("mail-original"));clear(byId("mail-detail"));clear(byId("mail-findings"));clear(byId("mail-answer-meta"));clear(byId("mail-limitations"));byId("mail-translation-status").textContent="选择邮件后显示中文译文"; state.offset=0; state.chinese={}; state.titles={}; clear(byId("mail-fields")); clear(byId("mail-evidence-list")); clear(byId("mail-result-items")); byId("mail-answer").textContent="输入你要查找、比较或提取的内容。"; byId("mail-conflicts").textContent=""; state.sessionId = ""; state.resultSetId = ""; state.turnId = ""; byId("mail-export").disabled = true; byId("mail-start").disabled = false; byId("mail-cancel").disabled = true;
    if (!state.packageId) return;
    const payload = await api(`/api/mail/packages/${encodeURIComponent(state.packageId)}`);
    renderOverview({ ...payload.overview, coverage: payload.coverage });
    await Promise.all([loadMessages(), loadSessions(), loadRelations(), loadMiningViews()]);
  }
  async function loadMessages() {
    if (!state.packageId) return;
    const epoch=++state.listEpoch;
    const query = new URLSearchParams({ offset:String(state.offset), limit: "25", query: byId("mail-query").value.trim(), participant: byId("mail-participant").value.trim() });
    if (byId("mail-with-attachment").checked) query.set("has_attachment", "true");
    const payload = await api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/messages?${query}`);
    if(epoch!==state.listEpoch)return;
    const result = payload.messages; (result.items || []).forEach(item=>{state.titles[item.message_key]=item.subject_raw || "无主题";}); const body = byId("mail-list"); clear(body);
    byId("mail-count").textContent = `${result.total} 封逻辑邮件`;
    renderTimeline(result.items, result.total);
    if (!result.items.length) { byId("mail-page").textContent="0 条";byId("mail-next").disabled=true;byId("mail-prev").disabled=state.offset===0;const row = document.createElement("tr"); const node = document.createElement("td"); node.colSpan = 4; node.textContent = "当前范围没有邮件"; row.appendChild(node); body.appendChild(row); return; }
    result.items.forEach((item) => { const row = document.createElement("tr"); row.tabIndex = 0; cell(row, item.sent_at_utc || item.date_raw || "未解析日期"); cell(row, item.sender || ""); cell(row, zhTitle(item.message_key,item.subject_raw)); row.dataset.messageKey=item.message_key; row.title=item.subject_raw || ""; cell(row, item.attachment_count || 0); row.addEventListener("click", () => inspectMessage(item.message_key).catch(report)); row.addEventListener("keydown", (event) => { if (event.key === "Enter") inspectMessage(item.message_key).catch(report); }); body.appendChild(row); });
    byId("mail-page").textContent=`${state.offset+1}–${state.offset+result.items.length} / ${result.total}`;byId("mail-prev").disabled=state.offset===0;byId("mail-next").disabled=state.offset+25>=result.total;
    presentation({kind:"titles",message_keys:result.items.map(i=>i.message_key)},()=>epoch===state.listEpoch,p=>{Object.entries(p.values || {}).forEach(([key,value])=>{state.chinese[`title:${key}`]=value.text || (value.status==="failed"?"译文暂不可用（点击查看原文）":"主题翻译中…");});body.querySelectorAll("tr[data-message-key]").forEach(row=>{row.cells[2].textContent=zhTitle(row.dataset.messageKey,state.titles[row.dataset.messageKey]);});renderTimeline(result.items,result.total);}).catch(report);

  }
  function renderTimeline(items, total) {
    const list = byId("mail-timeline-list"); clear(list); byId("mail-timeline-count").textContent = `${total} 封`;
    if (!items.length) { const row = document.createElement("li"); row.textContent = "当前范围没有邮件"; list.appendChild(row); return; }
    items.slice().reverse().forEach((item) => { const row = document.createElement("li"); const time = document.createElement("time"); time.textContent = item.sent_at_utc || item.date_raw || "未解析日期"; const detail = document.createElement("button"); detail.type = "button"; detail.className = "mail-relation-link"; detail.textContent = `${item.sender || "未知发件人"}：${zhTitle(item.message_key,item.subject_raw)}`; detail.addEventListener("click", () => inspectMessage(item.message_key).catch(report)); row.append(time, detail); list.appendChild(row); });
  }
  async function inspectMessage(messageKey, translate=true) {
    const epoch=++state.previewEpoch;
    const packageQuery = `package_id=${encodeURIComponent(state.packageId)}`;
    const [detail, preview] = await Promise.all([api(`/api/mail/messages/${encodeURIComponent(messageKey)}?${packageQuery}`), api(`/api/mail/messages/${encodeURIComponent(messageKey)}/preview?${packageQuery}`)]);
    if(epoch!==state.previewEpoch)return;
    const message = detail.message; const container = byId("mail-detail"); clear(container);
    const title = document.createElement("h2"); title.textContent = zhTitle(messageKey,message.subject_raw); container.appendChild(title);
    const list = document.createElement("dl"); [["日期", message.sent_at_utc || message.date_raw], ["参与方", (message.participants || []).map((item) => `${zhLabel(item.role)}: ${item.normalized_address}`).join("\n")]].forEach(([label, value]) => { const term = document.createElement("dt"); term.textContent = label; const definition = document.createElement("dd"); definition.textContent = text(value); list.append(term, definition); }); container.appendChild(list);
    const attachments = (message.parts || []).filter((item) => item.is_explicit_attachment);
    if (attachments.length) { const section = document.createElement("section"); section.className = "mail-attachment-list"; const heading = document.createElement("h3"); heading.textContent = "显式附件"; const items = document.createElement("ul"); attachments.forEach((item) => { const row = document.createElement("li"); const button = document.createElement("button"); button.type = "button"; button.className = "mail-relation-link"; button.textContent = `${item.filename_raw || item.mime_path} (${item.content_type}，${zhLabel(item.processing_status || "not_started")})`; button.addEventListener("click", () => inspectAttachment(messageKey, item.mime_path, item.filename_raw || item.mime_path).catch(report)); row.appendChild(button); items.appendChild(row); }); section.append(heading, items); container.appendChild(section); }
    const output = byId("mail-preview"); clear(output); if (preview.content_type === "text/html") output.innerHTML = preview.content; else output.textContent = preview.content;
    state.evidenceId = "";
    if(translate)await presentSource({kind:"message",message_key:messageKey},output.textContent);
  }
  async function inspectAttachment(messageKey, mimePath, label) {
    const epoch=++state.previewEpoch;
    const packageQuery = `package_id=${encodeURIComponent(state.packageId)}`;
    const encodedMessage = encodeURIComponent(messageKey); const encodedPath = mimePath.split("/").map(encodeURIComponent).join("/");
    const [preview, segments] = await Promise.all([api(`/api/mail/messages/${encodedMessage}/attachments/${encodedPath}/preview?${packageQuery}`), api(`/api/mail/messages/${encodedMessage}/attachments/${encodedPath}/segments?${packageQuery}`)]);
    if(epoch!==state.previewEpoch)return;
    const values = (segments.segments || []).map((item) => `${item.page_number ? `第 ${item.page_number} 页 ` : ""}${item.sheet_name ? `${item.sheet_name} ` : ""}${item.text || ""}`).filter(Boolean);
    const output = byId("mail-preview"); output.textContent = values.length ? `附件：${label}\n\n${values.join("\n\n")}` : (preview.available ? preview.content : `${label}\n\n${preview.reason || "附件文本尚未就绪"}`);
    state.evidenceId = "";
    await presentSource({kind:"attachment",message_key:messageKey,mime_path:mimePath},output.textContent);
  }
  async function loadRelations() {
    if (!state.packageId) return;
    const unreviewed = byId("mail-show-unreviewed-relations").checked ? "?include_unreviewed=true" : "";
    const [communication, threads, business] = await Promise.all([api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/relations/communication`), api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/relations/threads?status=exact&relation_type=reply_to`), api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/relations/business${unreviewed}`)]);
    const render = (id, items, line) => { const node = byId(id); clear(node); items.slice(0, 100).forEach((item) => { const row = document.createElement("li"); const value = line(item); if (typeof value === "string") row.textContent = value; else row.appendChild(value); node.appendChild(row); }); if (!items.length) { const row = document.createElement("li"); row.textContent = id === "mail-business-relations" ? "暂无经证据核验的业务事项关系" : "暂无可核验关系"; node.appendChild(row); } };
    render("mail-communication", communication.communication.items || [], (item) => { const node = document.createElement("button"); node.type = "button"; node.className = "mail-relation-link"; node.textContent = `${item.from_address} -> ${item.to_address} (${item.message_count}，${item.first_sent || ""} 至 ${item.last_sent || ""})`; node.addEventListener("click", () => { byId("mail-participant").value = item.to_address; loadMessages().catch(report); }); return node; });
    render("mail-threads", threads.threads || [], (item) => { const node = document.createElement("button"); node.type = "button"; node.className = "mail-relation-link"; node.textContent = `${item.source_subject || item.source_message_key} -> ${item.target_subject || item.status} (${zhLabel(item.relation_type)})`; node.addEventListener("click", () => inspectMessage(item.source_message_key).catch(report)); return node; });
    const labels = { proposed: "提出", confirmed: "确认", modified: "修改", cancelled: "取消", supports: "支持", contradicts: "反驳" };
    render("mail-business-relations", business.relations || [], (item) => { const node = document.createElement("button"); node.type = "button"; node.className = "mail-relation-link"; const relation = `${item.subject_ref} - ${labels[item.predicate] || item.predicate} - ${item.object_ref}`; node.textContent = `${relation} (${zhLabel(item.state)})`; const evidenceId = (item.evidence_ids || [])[0]; if (evidenceId) node.addEventListener("click", () => inspectEvidence(evidenceId).catch(report)); else node.disabled = true; return node; });
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
    const payload = await api(`/api/mail/research-sessions?package_id=${encodeURIComponent(state.packageId)}`);
    const select = byId("mail-session"); clear(select); select.appendChild(option("新建会话...", "")); payload.sessions.forEach((item) => select.appendChild(option(item.title, item.session_id)));
  }
  async function ensureSession() {
    state.sessionId = byId("mail-session").value || state.sessionId;
    if (state.sessionId) return state.sessionId;
    const payload = await api("/api/mail/research-sessions", { method: "POST", body: JSON.stringify({ package_id: state.packageId, title: "邮件研究" }) });
    state.sessionId = payload.session.session_id; await loadSessions(); byId("mail-session").value = state.sessionId; return state.sessionId;
  }
  async function runResearch(event) {
    event.preventDefault(); if (!state.packageId) throw new Error("请先选择邮件资料包");
    const question = byId("mail-question").value.trim(); if (!question) throw new Error("请输入研究目标");
    state.resultEpoch++;Object.keys(state.chinese).filter(k=>k.startsWith("field:") || k.startsWith("evidence:")).forEach(k=>delete state.chinese[k]);
    const sessionId = await ensureSession(); const payload = await api(`/api/mail/research-sessions/${encodeURIComponent(sessionId)}/turns`, { method: "POST", body: JSON.stringify({ question, scope: { include_explicit_attachments: true, include_inline_resources: false }, base_result_set_id: byId("mail-followup").checked ? state.resultSetId || null : null }) });
    state.jobId = payload.job_id; state.turnId = payload.turn.turn_id; byId("mail-start").disabled = true; byId("mail-cancel").disabled = false; byId("mail-progress").textContent = `研究任务 ${state.jobId} 已进入队列`; status(payload.waiting_for_index ? "研究已提交，附件/OCR 覆盖仍在准备" : "研究任务已提交"); pollTurn(payload.turn.turn_id);
  }
  async function pollTurn(turnId) {
    const epoch = ++state.pollEpoch;
    const tick = async () => {
      if (epoch !== state.pollEpoch) return;
      const payload = await api(`/api/mail/research-turns/${encodeURIComponent(turnId)}`);
      if (epoch !== state.pollEpoch) return;
      const turn = payload.turn;
      byId("mail-progress").textContent = `${zhLabel(turn.status)}${turn.sufficiency ? ` · ${zhLabel(turn.sufficiency)}` : ""}`;
      if (["completed", "failed", "cancelled", "paused"].includes(turn.status)) {
        byId("mail-start").disabled = false; byId("mail-cancel").disabled = true;
        if (turn.status !== "completed") { status(`研究状态：${zhLabel(turn.status)} ${(turn.error || {}).message || ""}`, true); return; }
        const final = turn.final || {}; state.resultSetId = final.result_set_id || turn.result_set_id || "";
        renderFinal(final); await Promise.all([loadRelations(), loadMiningViews()]);
        if (state.resultSetId) await showResult(state.resultSetId);
        status("研究完成"); return;
      }
      window.setTimeout(() => tick().catch(report), 1500);
    }; tick().catch(report);
  }
  async function exportCurrent() {
    if (!state.resultSetId) return;
    const payload = await api("/api/mail/exports", { method: "POST", body: JSON.stringify({ result_set_id: state.resultSetId, format: "xlsx" }) });
    watchJob(payload.job_id, true);
  }
  async function watchJob(jobId, download = false) {
    const tick = async () => {
      const value = await api(`/api/jobs/${encodeURIComponent(jobId)}`); const job = value.job || value;
      byId("mail-progress").textContent = `${zhLabel(job.task_type) || "任务"}：${zhLabel(job.status)} ${job.progress || 0}% ${job.message || ""}`;
      if (["completed", "succeeded"].includes(job.status)) {
        if (download && (job.result || {}).filename) {
          const ticket = await api("/api/download-ticket", { method: "POST", body: JSON.stringify({ filename: job.result.filename }) });
          const link = document.createElement("a"); link.href = ticket.download_url; link.textContent = "下载研究结果"; link.download = job.result.filename; byId("mail-progress").append(" ",link);
        }
        await loadMiningViews(); status("任务完成"); return;
      }
      if (["failed", "cancelled"].includes(job.status)) { status(`${zhLabel(job.status)}：${job.error || job.message || ""}`, true); return; }
      window.setTimeout(() => tick().catch(report),2000);
    }; tick().catch(report);
  }
  function evidenceButton(label,evidenceId) {
    const button=document.createElement("button"); button.type="button"; button.className="mail-relation-link"; button.textContent=label;
    button.addEventListener("click",()=>inspectEvidence(evidenceId).catch(report)); return button;
  }
  function displayFieldValue(value) {
    if (value == null) return "具体值未确定";
    if (typeof value !== "object") return text(value);
    if (value.amount != null) return `${Number(value.amount).toLocaleString("zh-CN",{maximumFractionDigits:6})} ${({THB:"泰铢",USD:"美元",CNY:"人民币",EUR:"欧元"})[value.currency] || value.currency || "（币种未明确）"}`;
    if (value.quantity != null) {const prefix=value.comparison === "at_most" ? "最多 " : value.comparison === "at_least" ? "至少 " : "";const units={"ทุน":"份资助","ท่าน":"人"};return `${prefix}${value.quantity} ${units[value.unit] || value.unit || ""}`;}
    return JSON.stringify(value);
  }
  function renderFields(fields) {
    const body=byId("mail-fields"); clear(body);
    const labels={amount:"金额",date:"日期",quantity:"数量",owner:"责任方",term:"条件",status:"状态",version:"版本"};
    (fields || []).forEach((f,i)=>{ const row=document.createElement("tr");cell(row,zhValue(`field:${i}:subject`,f.subject_ref || f.subject_raw || "原文未明确事项"));cell(row,labels[f.fact_type] || "提取字段");cell(row,zhValue(`field:${i}:value`,f.value_raw));const normalized=f.value_normalized;cell(row,normalized && typeof normalized==="object" || f.fact_type==="date" ? displayFieldValue(normalized) : zhValue(`field:${i}:value`,normalized || f.value_raw));cell(row,({mentioned:"原文提及",proposed:"提出",confirmed:"明确确认",modified:"修改",cancelled:"取消",conditional:"有条件"})[f.status] || "原文提及");const td=document.createElement("td");td.append(evidenceButton("核查证据",f.evidence_id),sourceDetails(`${f.subject_ref || f.subject_raw || ""}\n${f.value_raw}\n${f.quote || ""}`));row.append(td);body.append(row); });
  }
  function renderFinal(final) {
    state.final=final;
    byId("mail-answer").textContent=final.answer || "当前没有可确认的研究结论";
    byId("mail-limitations").textContent=(final.limitations || []).join("\n");
    byId("mail-conflicts").textContent=(final.conflicts || []).map(c=>c.reason).join("\n");
    const coverage=final.coverage || {},research=coverage.research || {};
    byId("mail-answer-meta").textContent=`找到 ${research.matched || 0} 封邮件 · 提取 ${(final.fields || []).length} 个字段 · 引用 ${(final.evidence || []).length} 段证据${final.elapsed_seconds ? ` · 用时 ${Math.round(final.elapsed_seconds)} 秒` : ""}`;
    const findings=byId("mail-findings");clear(findings);
    (final.findings || []).forEach((f,i)=>{const li=document.createElement("li");li.append(evidenceButton(`核查第 ${i+1} 项发现的原文`,f.evidence_id));findings.append(li);});
    (final.evidence || []).forEach(e=>{if(e.subject_raw)state.titles[e.message_key]=e.subject_raw;});renderFields(final.fields);renderEvidence(final.evidence);
  }
  async function showResult(id) {
    const payload=await api(`/api/mail/result-sets/${encodeURIComponent(id)}`);const result=payload.result_set;
    Object.keys(state.chinese).filter(k=>k.startsWith("field:") || k.startsWith("evidence:")).forEach(k=>delete state.chinese[k]);
    state.currentResult=result;
    state.resultSetId=id;byId("mail-export").disabled=false;byId("mail-filter-result").disabled=false;byId("mail-result-history").value=id;
    if(result.final)renderFinal(result.final);else {byId("mail-answer").textContent=(result.definition || {}).question ? `研究目标：${result.definition.question}` : "筛选后的结果";clear(byId("mail-evidence-list"));clear(byId("mail-findings"));}
    byId("mail-result-summary").textContent=`${result.item_count} 条记录${result.parent_result_set_id ? " · 来源于上一结果集" : " · 全资料包检索"}`;
    renderFields((result.definition || {}).fields || []);renderResultItems(result);
    translateResult(result).catch(report);
  }

  async function loadMiningViews() {
    if (!state.packageId) return;
    const base=`/api/mail/packages/${encodeURIComponent(state.packageId)}`;
    const [coverage,results,conversations,versions,network]=await Promise.all([api(`${base}/coverage`),api(`${base}/result-sets`),api(`${base}/conversations`),api(`${base}/attachment-versions`),api(`${base}/relations/network`)]);
    const gaps=byId("mail-coverage-gaps");clear(gaps);
    (coverage.items || []).forEach(item=>{const li=document.createElement("li");const button=document.createElement("button");button.type="button";button.className="mail-relation-link";button.textContent=`${zhLabel(item.status)} · ${item.filename_raw || item.mime_path}`;button.addEventListener("click",()=>inspectAttachment(item.message_key,item.mime_path,item.filename_raw).catch(report));li.append(button);gaps.append(li);});
    if (!coverage.total) {const li=document.createElement("li");li.textContent="当前附件没有待处理缺口";gaps.append(li);}
    const history=byId("mail-result-history");clear(history);history.append(option("选择结果集…",""));(results.items || []).forEach(item=>history.append(option(`${item.created_at} · ${item.item_count} 条${item.parent_result_set_id ? " · 追问" : ""}`,item.result_set_id)));history.value=state.resultSetId;
    const conv=byId("mail-conversations");clear(conv);(conversations.items || []).slice(0,150).forEach(item=>{const li=document.createElement("li");const button=document.createElement("button");button.type="button";button.textContent=`${item.subject_normalized || "无主题"} · ${item.message_count} 封 · ${item.merge_basis}`;button.addEventListener("click",()=>showConversation(item.conversation_id).catch(report));li.append(button);conv.append(li);});
    const versionList=byId("mail-versions");clear(versionList);(versions.items || []).slice(0,200).forEach(item=>{const li=document.createElement("li");const button=document.createElement("button");button.type="button";button.textContent=`${item.chain_key} · 顺序 ${item.version_ordinal} · ${item.sent_at_utc || "日期未知"}`;button.addEventListener("click",()=>inspectAttachment(item.message_key,item.mime_path,item.chain_key).catch(report));li.append(button);versionList.append(li);});
    renderNetwork(network.network || {});
  }
  async function showConversation(id) {
    const value=(await api(`/api/mail/conversations/${encodeURIComponent(id)}`)).conversation;const node=byId("mail-conversation-detail");clear(node);
    (value.edges || []).forEach(e=>{const row=document.createElement("div");row.className=e.status === "candidate" ? "mail-candidate-edge" : "mail-exact-edge";const button=document.createElement("button");button.textContent=`${e.source_subject || e.source_message_key} → ${e.target_subject || e.status} (${zhLabel(e.status)})`;button.addEventListener("click",()=>inspectMessage(e.source_message_key).catch(report));row.append(button);node.append(row);});
    if (!(value.edges || []).length) node.textContent="此邮件没有可核验的回复引用关系。";
  }
  function renderNetwork(network) {
    const node=byId("mail-network");clear(node);const all=network.nodes || [];const nodes=all.slice(0,30);const ns="http://www.w3.org/2000/svg";const svg=document.createElementNS(ns,"svg");svg.setAttribute("viewBox","0 0 800 600");svg.setAttribute("aria-label","邮箱通信关系图");const positions={};
    nodes.forEach((n,i)=>{const a=i*2*Math.PI/Math.max(1,nodes.length);positions[n.address]=[400+260*Math.cos(a),300+230*Math.sin(a)];});
    (network.edges || []).forEach(e=>{const a=positions[e.from_address],b=positions[e.to_address];if(!a||!b)return;const line=document.createElementNS(ns,"line");line.setAttribute("x1",a[0]);line.setAttribute("y1",a[1]);line.setAttribute("x2",b[0]);line.setAttribute("y2",b[1]);line.setAttribute("stroke","#90a6ba");line.setAttribute("stroke-width",Math.min(8,1+Math.log2(1+e.message_count)));const title=document.createElementNS(ns,"title");title.textContent=`${e.from_address} → ${e.to_address}：${e.message_count} 封逻辑邮件`;line.append(title);line.addEventListener("click",()=>{const list=document.createElement("ul");(e.message_keys || []).forEach(k=>{const li=document.createElement("li"),b=document.createElement("button");b.textContent=k;b.addEventListener("click",()=>inspectMessage(k).catch(report));li.append(b);list.append(li);});node.append(list);});svg.append(line);});
    nodes.forEach(n=>{const [x,y]=positions[n.address];const circle=document.createElementNS(ns,"circle");circle.setAttribute("cx",x);circle.setAttribute("cy",y);circle.setAttribute("r",n.bridge ? 8 : 5);circle.setAttribute("fill",n.bridge ? "#d99335" : "#3189b8");const label=document.createElementNS(ns,"text");label.setAttribute("x",x+8);label.setAttribute("y",y);label.setAttribute("font-size","10");label.textContent=n.display_name;svg.append(circle,label);});node.append(svg);const note=document.createElement("p");note.textContent=`显示 ${nodes.length}/${all.length} 个邮箱；按逻辑邮件统计。精确回复样本 ${(network.reply_latency || {}).exact_reply_count || 0} 条。`;node.append(note);
  }
  async function queueIndex(stage) {
    if (!state.packageId) throw new Error("请先选择资料包");
    const payload=await api(`/api/mail/packages/${encodeURIComponent(state.packageId)}/index`,{method:"POST",body:JSON.stringify({stage})});
    for(const job of payload.jobs || []) watchJob(job.job_id);
    status("索引任务已提交");
  }
  async function translateEvidence() {if(state.previewPayload){const pages=state.previewPages.slice();for(const page of pages){if(page.status!=="completed")await presentSource({...state.previewPayload,offset:page.offset,retry:true},"",true);}if(pages.every(p=>p.status==="completed"))status("现有译文已完成，可开启中外文对照核查");}}

  function report(error) { status(error.message || "邮件研究操作失败", true); }
  byId("mail-token").addEventListener("click", configureToken);
  byId("mail-token-form").addEventListener("submit", saveToken);
  byId("mail-token-cancel").addEventListener("click", () => byId("mail-token-dialog").close());
  byId("mail-token-dialog").addEventListener("close", () => { byId("mail-token-input").value = ""; });
  byId("mail-refresh").addEventListener("click", () => loadPackages(state.packageId).catch(report));
  byId("mail-package").addEventListener("change", () => loadPackage().catch(report));
  byId("mail-apply-filter").addEventListener("click", () => {state.offset=0;loadMessages().catch(report);});
  byId("mail-show-unreviewed-relations").addEventListener("change", () => loadRelations().catch(report));
  byId("mail-question-form").addEventListener("submit", (event) => runResearch(event).catch(report));
  byId("mail-new-session").addEventListener("click", () => { state.sessionId = ""; state.resultSetId = ""; byId("mail-session").value=""; ensureSession().then(() => status("已创建研究会话")).catch(report); });
  byId("mail-index").addEventListener("click",()=>queueIndex("attachments").catch(report));
  byId("mail-embed").addEventListener("click",()=>queueIndex("embedding").catch(report));
  byId("mail-factextract").addEventListener("click",()=>queueIndex("facts").catch(report));
  byId("mail-ocr").addEventListener("click",()=>queueIndex("ocr").catch(report));
  byId("mail-cancel").addEventListener("click",()=>{if(state.turnId) api(`/api/mail/research-turns/${encodeURIComponent(state.turnId)}/cancel`,{method:"POST",body:"{}"}).then(()=>status("正在取消研究")).catch(report);});
  byId("mail-result-history").addEventListener("change",e=>{if(e.target.value) showResult(e.target.value).catch(report);});
  byId("mail-filter-result").addEventListener("click",()=>{if(state.resultSetId) api(`/api/mail/result-sets/${encodeURIComponent(state.resultSetId)}/filter`,{method:"POST",body:JSON.stringify({has_attachment:true})}).then(async p=>{await loadMiningViews();await showResult(p.result_set.result_set_id);}).catch(report);});
  byId("mail-session").addEventListener("change",async e=>{state.sessionId=e.target.value;state.resultSetId="";if(!state.sessionId)return;try{const p=await api(`/api/mail/research-sessions/${encodeURIComponent(state.sessionId)}`);const completed=(p.turns || []).filter(t=>t.status === "completed" && t.result_set_id);if(completed.length)await showResult(completed[completed.length-1].result_set_id);}catch(error){report(error);}});

  byId("mail-export").addEventListener("click", () => exportCurrent().catch(report));
  byId("mail-translate").addEventListener("click", () => translateEvidence().catch(report));
  byId("mail-import-form").addEventListener("submit", (event) => { event.preventDefault(); const path = byId("mail-source-path").value.trim(); const display_name = byId("mail-package-name").value.trim(); if (!path) return report(new Error("请输入服务器邮件来源路径")); api("/api/mail/packages", { method: "POST", body: JSON.stringify({ source: { path }, display_name, options: { build_basic_attachment_index: true } }) }).then((payload) => { status("邮件资料包已提交导入"); return loadPackages(payload.package.package_id); }).catch(report); });
  byId("mail-entity-form").addEventListener("submit",event=>{event.preventDefault();if(!state.packageId)return;const addresses=byId("mail-entity-addresses").value.split(/[,，;；\s]+/).filter(Boolean);api("/api/mail/entities/confirm",{method:"POST",body:JSON.stringify({package_id:state.packageId,display_name:byId("mail-entity-name").value.trim(),addresses})}).then(()=>loadMiningViews()).then(()=>status("邮箱归并已记录人工确认依据")).catch(report);});
  byId("mail-prev").addEventListener("click",()=>{state.offset=Math.max(0,state.offset-25);loadMessages().catch(report);});
  byId("mail-next").addEventListener("click",()=>{state.offset+=25;loadMessages().catch(report);});
  byId("mail-more-translation").addEventListener("click",()=>{if(state.previewPayload && state.nextTranslationOffset!=null)presentSource({...state.previewPayload,offset:state.nextTranslationOffset},"",true).catch(report);});
  document.querySelectorAll("[data-mail-example]").forEach(button=>button.addEventListener("click",()=>{byId("mail-question").value=button.dataset.mailExample;byId("mail-question").focus();}));
  byId("mail-bilingual").addEventListener("change",()=>{if(state.previewPages)renderTranslationSegments();});
  byId("mail-retry-result").addEventListener("click",()=>{if(state.currentResult)translateResult(state.currentResult,true).catch(report);});
  loadPackages().catch(report);
})();
