/* Shared, dependency-free intelligence reading surface. Never infers unread coverage. */
(function (root) {
  'use strict';
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const text = value => typeof value === 'string' || typeof value === 'number' ? String(value) : value && (value.text || value.summary || value.title || value.name || value.path) || '';
  const list = value => (Array.isArray(value) ? value : value == null ? [] : [value]).map(text).filter(Boolean);
  const items = value => Array.isArray(value) ? value : value && Array.isArray(value.items) ? value.items : [];
  const number = value => value == null || value === '' || typeof value === 'boolean' || !Number.isFinite(Number(value)) || Number(value) < 0 ? null : Number(value);
  const count = value => number(value) == null ? '—' : Number(value).toLocaleString('zh-CN');
  function model(kind, source, brief) {
    source = source || {}; brief = brief || {};
    const large = kind === 'large';
    const coverage = large ? source.coverage || {} : brief.coverage || (brief.value_judgment || {}).coverage || (brief.value_judgment || {}).analysis_coverage || {};
    const total = number(large ? coverage.inventory_files : (source.package || {}).file_count);
    const presentation = !large && brief.overview_presentation && typeof brief.overview_presentation === 'object' && !Array.isArray(brief.overview_presentation) && brief.overview_presentation.direction && typeof brief.overview_presentation.direction === 'object' && !Array.isArray(brief.overview_presentation.direction) ? brief.overview_presentation : null;
    const direction = presentation ? presentation.direction : brief.recommended_research_direction || {};
    const categoryCount = value => (typeof value === 'number' || typeof value === 'string' && /^\d+$/.test(value)) && Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : null;
    const fileCategoryRows = presentation ? evidenceRows(presentation.file_categories) : evidenceRows(brief.global_categories);
    const fileCategories = fileCategoryRows.map(row => ({label:text(row.name), value:categoryCount(row.file_count), parsed:categoryCount(row.parsed_file_count), status:row.status || (row.classification_status === 'needs_review' ? '待复核' : '已分类')}));
    const rawRatio = coverage.content_parse_ratio == null ? (coverage.coverage_ratio == null ? coverage.parsed_file_ratio : coverage.coverage_ratio) : coverage.content_parse_ratio;
    const ratio = number(rawRatio);
    const metrics = large ? [
      ['已盘点文件', total, '建立清单，不代表已读取全文'],
      ['快速解析', number(coverage.quick_completed), '本地提取与快速摘要'],
      ['深度解析', number(coverage.deep_completed), '完成深度处理的文件'],
      ['模型摘要', number(coverage.model_summaries), '已产出模型摘要的文件']
    ] : [
      ['资料文件', total, '当前数据包清单'],
      ['目录层级', number((source.package || {}).max_depth), '原始目录的最大深度'],
      ['内容联系', number((source.file_relationships || {}).relationship_count), '内容或结构上的联系'],
      ['解析覆盖', ratio == null ? null : Math.min(100, ratio <= 1 ? ratio * 100 : ratio), '按当前简报的统计口径', '%']
    ];
    return {kind, title: presentation && /[A-Za-z]/.test(text(brief.title)) ? '资料情报概览' : brief.title || (large ? '大数据包情报概览' : '资料情报概览'), presentation,
      ready: Boolean(brief.available || brief.ready), preliminary: Boolean(brief.preliminary || (large && brief.available && !brief.ready) || (!large && brief.available && brief.summary_stage === 'preliminary')),
      stage: large ? '' : String(brief.summary_stage || ''),
      basics: presentation ? [] : list(brief.basic_information), findings: list(presentation ? presentation.key_findings : brief.key_findings),
      alternatives: presentation ? evidenceRows(presentation.alternatives) : large ? [] : (Array.isArray(brief.direction_candidates) ? brief.direction_candidates : []).filter(item => item && typeof item === 'object' && (direction.candidate_id == null || item.candidate_id !== direction.candidate_id)),
      limitations: list(presentation ? presentation.limitations : brief.limitations), direction, total, metrics, fileCategories,
      fileCategoryCount: presentation ? categoryCount(presentation.file_category_count) : fileCategories.length, classificationScope: presentation && text(presentation.classification_scope),
      researchAudit: !large && brief.research_opportunity_audit && typeof brief.research_opportunity_audit === 'object' && !Array.isArray(brief.research_opportunity_audit) ? brief.research_opportunity_audit : null,
      categories: large ? Object.entries(source.categories || {}).sort((a,b) => Number(b[1]) - Number(a[1])).slice(0,6).map(([label, value]) => ({label,value:number(value)})) : items(source.topics).slice(0,6).map(item => ({label:text(item.topic),value:number(item.file_count)})),
      failures: Array.isArray(source.failures) ? source.failures.length : null,
      root: large ? '' : (source.package || {}).root || '',
      parsed: large ? number(coverage.deep_completed) : null,
      catalogRejection: large && source.catalog_rejection && typeof source.catalog_rejection === 'object' ? source.catalog_rejection : null};
  }
  // 目录校验拒绝了新目录、页面仍显示上一版正式概览时的提示横幅（后端只在拒绝比当前正式版更新时给出）。
  function rejectionTime(created) {
    const seconds = number(created);
    return seconds ? new Date(seconds * 1000).toLocaleString('zh-CN', {hour12: false}) : '';
  }
  function rejectionNotice(rejection) {
    if (!rejection) return '';
    const reason = text(rejection.reason);
    const when = rejectionTime(rejection.created);
    const detail = [reason ? `原因：${reason}` : '', when ? `时间：${when}` : ''].filter(Boolean).join('；');
    return `<section class="io-card io-catalog-rejection" role="status" aria-live="polite"><p class="io-warning"><strong>目录校验未通过，当前显示上一版</strong>${detail ? `（${esc(detail)}）` : ''}</p></section>`;
  }
  function researchFocus(vm) {
    if (vm.kind === 'ordinary') return ordinaryResearchFocus(vm);
    const direction = vm.direction || {};
    const questions = list(direction.research_questions || direction.questions);
    const files = list(direction.representative_documents);
    const methods = list(direction.methods);
    const evidence = Array.isArray(direction.evidence_chain) ? direction.evidence_chain.filter(item => item && typeof item === 'object') : [];
    const detailId = vm.kind === 'large' ? 'largeResultResearchDetails' : 'packageOverviewResearchDetails';
    const pills = [direction.priority ? `优先级 · ${text(direction.priority)}` : '', direction.confidence ? `简报置信度 · ${text(direction.confidence)}` : '', direction.coverage_note ? text(direction.coverage_note) : ''].filter(Boolean);
    return `<section class="io-focus${direction.title ? '' : ' is-waiting'}" aria-label="推荐研究方向"><div class="io-focus-main"><div class="io-focus-kicker"><span class="io-focus-mark" aria-hidden="true">↗</span><span><b>推荐研究方向</b></span><span class="io-focus-stage">${vm.preliminary ? '初步建议' : direction.title ? '当前首选方向' : '等待资料解析'}</span></div><h3>${esc(direction.title || '让下一步研究，从有据可查的问题开始')}</h3><div class="io-focus-rule"></div><h4>为什么值得研究</h4><p class="io-focus-rationale">${esc(direction.rationale || '完成资料解析后，这里将给出有证据支撑的研究主题与推荐理由，不以文件名猜测结论。')}</p>${vm.kind === 'ordinary' && direction.selection_reason ? `<div class="io-selection"><h4>为什么优先这个</h4><p>${esc(direction.selection_reason)}</p></div>` : ''}${vm.kind === 'ordinary' && (direction.known_work || direction.research_gap) ? `<div class="io-work-gap">${direction.known_work ? `<article><h4>资料已经做到什么</h4><p>${esc(direction.known_work)}</p></article>` : ''}${direction.research_gap ? `<article><h4>仍待核验的问题</h4><p>${esc(direction.research_gap)}</p></article>` : ''}</div>` : ''}${direction.package_profile ? `<h4>全包画像</h4><p class="io-focus-rationale">${esc(direction.package_profile)}</p>` : ''}${pills.length ? `<div class="io-focus-pills">${pills.map(pill=>`<span>${esc(pill)}</span>`).join('')}</div>` : ''}<div class="io-focus-footer"><span>${files.length ? `${files.length} 份代表资料` : '代表资料待形成'}${evidence.length ? ` · ${evidence.length} 条支撑原文` : ''}</span><a href="#${detailId}">查看研究详情 <span aria-hidden="true">↗</span></a></div></div><aside class="io-focus-plan"><span class="io-eyebrow">研究计划</span><h4>把方向变成研究路径</h4>${questions.length ? `<ol>${questions.map((question,index)=>`<li><span>${String(index+1).padStart(2,'0')}</span><p>${esc(question)}</p></li>`).join('')}</ol>` : '<p class="io-focus-empty">研究问题将在简报形成后出现。你仍可使用下方资料图表，自行选择问答范围。</p>'}${methods.length ? `<details><summary>建议研究方法 · ${methods.length}</summary><ul>${methods.map(method=>`<li>${esc(method)}</li>`).join('')}</ul></details>` : ''}${files.length ? `<div class="io-focus-sources"><h5>代表资料</h5>${files.slice(0,3).map(file=>vm.kind==='ordinary' ? `<button type="button" data-brief-file="${esc(file)}" title="${esc(file)}"><span aria-hidden="true">▤</span>${esc(file.split(/[\/]/).pop())}</button>` : `<span class="io-source-label" title="${esc(file)}"><span aria-hidden="true">▤</span>${esc(file.split(/[\/]/).pop())}</span>`).join('')}${files.length>3 ? `<a href="#${detailId}">查看全部 ${files.length} 份资料 →</a>` : ''}</div>` : ''}<p class="io-focus-disclaimer">推荐基于当前已解析资料，是研究起点，不是已证实的最终结论。</p></aside></section>`;
  }
  const EVIDENCE_ROLES = {support: '背景支撑', prior_work: '已有工作', conditions: '适用条件', counterevidence: '反证线索', resources: '资源条件'};
  const OPPORTUNITY_KINDS = {boundary: '适用边界验证', evaluation: '补全评价对照', disagreement: '条件与结果分歧复核', transfer: '方法迁移验证', replication: '关键结论复核', synthesis: '证据比较综合'};
  const evidenceRows = value => Array.isArray(value) ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item)) : [];
  const evidencePath = item => text(item.archive_source_path || item.source_path || item.path);
  function evidenceReferences(value) {
    const numbers = Array.from(new Set((Array.isArray(value) ? value : []).filter(value => Number.isSafeInteger(value) && value > 0)));
    return numbers.length ? `<span class="io-evidence-references">依据 ${numbers.map(value => `<a href="#packageOverviewEvidence-${value}" data-io-evidence-number="${value}">〔${value}〕</a>`).join('、')}</span>` : '';
  }
  function presentationEvidence(item) {
    const path = evidencePath(item);
    const page = number(item.page);
    const roles = list(item.roles).filter(role => role !== '背景支撑' || !list(item.roles).includes('反证线索'));
    const location = [page > 0 ? `第 ${page} 页` : '', text(item.section || item.heading), text(item.archive_member)].filter(Boolean);
    const metadata = [['证据编号', item.evidence_id], ['文件编号', item.file_id], ['原始路径', item.source_path && item.source_path !== path ? item.source_path : ''], ['内容单元', item.unit_id || item.chunk_id], ['段落序号', item.paragraph_index], ['内容块序号', item.block_index], ['行号', item.line ?? item.line_number], ['起始行', item.start_line ?? item.line_start], ['结束行', item.end_line ?? item.line_end], ['工作表', item.sheet || item.sheet_name], ['表格', item.table || item.table_name], ['行范围', item.row_range], ['字符起点', item.char_start ?? item.start_offset], ['字符终点', item.char_end ?? item.end_offset], ['原始定位', item.location]].filter(([, value]) => value != null && value !== '');
    const metadataText = value => value && typeof value === 'object' ? JSON.stringify(value) : text(value);
    const ordinal = Number.isSafeInteger(item.number) && item.number > 0 ? item.number : null;
    return `<article${ordinal ? ` id="packageOverviewEvidence-${ordinal}"` : ''}><div class="io-evidence-source"><strong>${ordinal ? `〔${ordinal}〕 ` : ''}${esc(path || '来源待核验')}${location.length ? ` · ${location.map(esc).join(' · ')}` : ''}</strong>${path ? `<button type="button" data-brief-file="${esc(path)}">查看来源</button>` : ''}</div>${roles.length ? `<p class="io-evidence-roles">${roles.map(esc).join(' · ')}</p>` : ''}${metadata.length ? `<dl class="io-evidence-metadata">${metadata.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(metadataText(value))}</dd></div>`).join('')}</dl>` : ''}<p class="io-original-quote">${esc(text(item.quote || item.original_text || item.text || item.snippet) || '该条未提供可展示原文，请打开来源核验。')}</p></article>`;
  }
  function presentationAppendix(presentation) {
    const evidence = evidenceRows(presentation.evidence_appendix);
    const notes = evidenceRows(presentation.source_notes);
    const documents = list(presentation.source_documents);
    if (!evidence.length && !notes.length && !documents.length) return '';
    return `<details id="packageOverviewEvidenceAppendix" class="io-evidence io-evidence-ordinary" data-io-disclosure="evidence"><summary>证据附件 · ${evidence.length} 条原文${notes.length ? ` · ${notes.length} 项规划原始表述` : ''}</summary><div>${evidence.map(presentationEvidence).join('')}${documents.length ? `<section class="io-appendix-documents"><h4>来源文件</h4>${documents.map(path => `<button type="button" data-brief-file="${esc(path)}">${esc(path)}</button>`).join('')}</section>` : ''}${notes.length ? `<section class="io-appendix-notes"><h4>研究规划原始表述</h4>${notes.map(item => `<p>${esc(text(item.text))}</p>`).join('')}</section>` : ''}</div></details>`;
  }
  function presentationResearchFocus(vm) {
    const direction = vm.direction || {};
    const presentation = vm.presentation;
    const questions = list(direction.research_questions || direction.questions).slice(0, 3);
    const methods = list(direction.methods).slice(0, 3);
    const reasons = evidenceRows(presentation.reasons).slice(0, 5);
    const evidence = evidenceRows(presentation.evidence_appendix);
    const files = list(presentation.source_documents);
    const pills = [Object.prototype.hasOwnProperty.call(OPPORTUNITY_KINDS, direction.opportunity_kind) ? OPPORTUNITY_KINDS[direction.opportunity_kind] : '', direction.priority ? `优先级 · ${text(direction.priority)}` : '', direction.confidence ? `简报置信度 · ${text(direction.confidence)}` : ''].filter(Boolean);
    return `<section class="io-focus io-focus-ordinary io-focus-presentation${direction.title ? '' : ' is-waiting'}" aria-label="推荐研究方向"><div class="io-focus-main"><div class="io-focus-kicker"><span class="io-focus-mark" aria-hidden="true">↗</span><b>推荐研究方向</b><span class="io-focus-stage">${vm.preliminary ? '基于当前已处理资料的首选建议' : direction.title ? '当前首选方向' : '等待资料解析'}</span></div><h3>${esc(text(direction.title) || '补充正文证据后确定研究方向')}</h3>${questions.length ? `<div class="io-core-question"><span>核心研究问题 · 待验证</span><p>${esc(questions[0])}</p></div>` : ''}<h4>为什么推荐</h4><div class="io-recommendation-reasons">${reasons.map(reason => `<section><h5>${esc(text(reason.label))}</h5><p>${esc(text(reason.text))}</p>${evidenceReferences(reason.evidence_numbers)}</section>`).join('') || '<p>当前依据尚不足，需先核对正文证据与研究条件。</p>'}</div>${pills.length ? `<div class="io-focus-pills">${pills.map(pill => `<span>${esc(pill)}</span>`).join('')}</div>` : ''}<div class="io-focus-footer"><span>${files.length} 份来源资料 · ${evidence.length} 条原文依据</span>${evidence.length || files.length || evidenceRows(presentation.source_notes).length ? '<a href="#packageOverviewEvidenceAppendix" data-io-open-evidence>查看证据附件 ↗</a>' : ''}</div></div><aside class="io-focus-plan"><span class="io-eyebrow">研究计划</span><h4>从核验问题与条件开始</h4>${questions.length > 1 ? `<ol>${questions.slice(1).map((question, index) => `<li><span>${String(index + 2).padStart(2, '0')}</span><p>${esc(question)}</p></li>`).join('')}</ol>` : ''}${methods.length ? `<details data-io-disclosure="plan"><summary>怎么研究 · 预期产出</summary><ul>${methods.map(method => `<li>${esc(method)}</li>`).join('')}</ul></details>` : '<p class="io-focus-empty">先确认正文依据与所需资源，再确定研究方法。</p>'}${text(direction.hypothesis) && direction.hypothesis !== questions[0] ? `<div class="io-hypothesis"><h5>待验证假设</h5><p>${esc(text(direction.hypothesis))}</p></div>` : ''}<p class="io-focus-disclaimer">推荐基于当前已解析资料，问题与假设仍需验证。</p></aside></section>`;
  }
  function evidenceRoles(direction, item) {
    if (item.evidence_id == null) return [];
    const roles = direction.evidence_roles || {};
    const hasRole = role => Array.isArray(roles[role]) && roles[role].some(id => String(id) === String(item.evidence_id));
    // The backend support bucket includes background material. A counterexample
    // may belong to it too, but must not be presented as supporting the proposal.
    const counter = hasRole('counterevidence') || evidenceRows(direction.counterevidence).some(row => row.evidence_id != null && String(row.evidence_id) === String(item.evidence_id));
    return Object.keys(EVIDENCE_ROLES).filter(role => hasRole(role) && (role !== 'support' || !counter)).map(role => EVIDENCE_ROLES[role]);
  }
  function ordinaryEvidence(direction, item) {
    const path = evidencePath(item);
    const page = number(item.page);
    const roles = evidenceRoles(direction, item);
    return `<article><div class="io-evidence-source"><strong>${esc(path || '来源待核验')}${page > 0 ? ` · 第 ${esc(page)} 页` : ''}</strong>${path ? `<button type="button" data-brief-file="${esc(path)}">查看来源</button>` : ''}</div>${roles.length ? `<p class="io-evidence-roles">${roles.map(esc).join(' · ')}</p>` : ''}<p>${esc(text(item.text || item.original_text || item.snippet) || '该条未提供可展示原文，请打开代表资料核验。')}</p></article>`;
  }
  function ordinaryResearchFocus(vm) {
    if (vm.presentation) return presentationResearchFocus(vm);
    const direction = vm.direction || {};
    const questions = list(direction.research_questions || direction.questions);
    const files = list(direction.representative_documents);
    const methods = list(direction.methods);
    const evidence = evidenceRows(direction.evidence_chain);
    const hypothesis = text(direction.hypothesis);
    const quality = direction.quality_gate || {};
    const pills = [Object.prototype.hasOwnProperty.call(OPPORTUNITY_KINDS, direction.opportunity_kind) ? OPPORTUNITY_KINDS[direction.opportunity_kind] : '', direction.priority ? `优先级 · ${text(direction.priority)}` : '', direction.confidence ? `简报置信度 · ${text(direction.confidence)}` : '', text(direction.coverage_note)].filter(Boolean);
    const methodItem = method => {
      const split = method.indexOf('→');
      return split > 0 && method.slice(split + 1).trim() ? `<li class="io-method-step"><span>研究动作</span><p>${esc(method.slice(0, split).trim())}</p><div class="io-method-output"><strong>预期产出</strong><span>${esc(method.slice(split + 1).trim())}</span></div></li>` : `<li>${esc(method)}</li>`;
    };
    return `<section class="io-focus io-focus-ordinary${direction.title ? '' : ' is-waiting'}" aria-label="推荐研究方向"><div class="io-focus-main"><div class="io-focus-kicker"><span class="io-focus-mark" aria-hidden="true">↗</span><span><b>推荐研究方向</b></span><span class="io-focus-stage">${vm.preliminary ? '基于当前已处理资料的首选建议' : direction.title ? '当前首选方向' : '等待资料解析'}</span></div><h3>${esc(text(direction.title) || '让下一步研究，从有据可查的问题开始')}</h3><div class="io-focus-rule"></div>${questions.length ? `<div class="io-core-question"><span>核心研究问题 · 待验证</span><p>${esc(questions[0])}</p></div>` : ''}<h4>为什么值得研究</h4><p class="io-focus-rationale">${esc(text(direction.rationale) || '完成资料解析后，这里将给出有证据支撑的研究主题与推荐理由，不以文件名猜测结论。')}</p>${text(direction.selection_reason) ? `<div class="io-selection"><h4>为什么优先这个</h4><p>${esc(text(direction.selection_reason))}</p></div>` : ''}${text(direction.known_work) || text(direction.research_gap) ? `<div class="io-work-gap">${text(direction.known_work) ? `<article><h4>资料已经做到什么</h4><p>${esc(text(direction.known_work))}</p></article>` : ''}${text(direction.research_gap) ? `<article><h4>仍待核验的问题</h4><p>${esc(text(direction.research_gap))}</p></article>` : ''}</div>` : ''}${hypothesis ? `<div class="io-hypothesis"><h4>待验证假设</h4>${hypothesis === questions[0] ? '' : `<p>${esc(hypothesis)}</p>`}<small>现有资料尚不能确认${hypothesis === questions[0] ? '核心研究问题的答案' : '该假设成立'}，需按右侧路径核验。</small></div>` : ''}${text(direction.package_profile) ? `<h4>全包画像</h4><p class="io-focus-rationale">${esc(text(direction.package_profile))}</p>` : ''}${pills.length ? `<div class="io-focus-pills">${pills.map(pill => `<span>${esc(pill)}</span>`).join('')}</div>` : ''}${text(direction.confidence_note) ? `<p class="io-confidence-note">${esc(text(direction.confidence_note))}</p>` : ''}<div class="io-focus-footer"><span>${files.length ? `${files.length} 份代表资料` : '代表资料待形成'}${evidence.length ? ` · ${evidence.length} 条依据原文` : ''}</span><a href="#packageOverviewResearchDetails">查看研究详情 <span aria-hidden="true">↗</span></a></div></div><aside class="io-focus-plan"><span class="io-eyebrow">研究计划</span><h4>把方向变成研究路径</h4>${questions.length ? `<ol>${questions.map((question, index) => `<li><span>${String(index + 1).padStart(2, '0')}</span><p>${esc(question)}</p></li>`).join('')}</ol>` : '<p class="io-focus-empty">研究问题将在简报形成后出现。你仍可使用下方资料图表，自行选择问答范围。</p>'}${methods.length ? `<details data-io-disclosure="plan" open><summary>怎么研究 · 预期产出</summary><ul>${methods.map(methodItem).join('')}</ul></details>` : ''}${quality.resources === 'confirmation_first' ? '<p class="io-resource-note">第一步先确认数据、代码与资源条件，再决定验证办法。</p>' : ''}${files.length ? `<div class="io-focus-sources"><h5>从这些代表资料开始</h5>${files.slice(0, 3).map(file => {
      const roles = Array.from(new Set(evidence.filter(item => evidencePath(item) === file).flatMap(item => evidenceRoles(direction, item))));
      return `<button type="button" data-brief-file="${esc(file)}" title="${esc(file)}"><span aria-hidden="true">▤</span><span class="io-source-name">${esc(file.split(/[\\/]/).pop())}${roles.length ? `<small>${roles.map(esc).join(' · ')}</small>` : ''}</span></button>`;
    }).join('')}${files.length > 3 ? `<a href="#packageOverviewResearchDetails">查看全部 ${files.length} 份资料 →</a>` : ''}</div>` : ''}<p class="io-focus-disclaimer">推荐基于当前已解析资料，是研究起点，不是已证实的最终结论。</p></aside></section>`;
  }
  function ordinaryResearchEvidence(vm) {
    if (vm.presentation) return presentationAppendix(vm.presentation);
    const direction = vm.direction || {};
    const evidence = evidenceRows(direction.evidence_chain);
    return evidence.length ? `<details class="io-evidence io-evidence-ordinary" data-io-disclosure="evidence"><summary>推荐依据 · 查看 ${evidence.length} 条依据原文</summary><div>${evidence.map(item => ordinaryEvidence(direction, item)).join('')}</div></details>` : '';
  }
  function fileClassification(vm) {
    const rows = vm.fileCategories || [];
    return `<section class="io-card io-file-classification" aria-label="文件分类"><div class="io-section-title"><span>资料构成</span><h3>文件分类</h3></div>${rows.length ? `<div class="io-category-list">${rows.map(row => `<article><div><h4>${esc(row.label || '未命名类别')}</h4><small>${esc(row.status)}</small></div><strong>${count(row.value)}<span> 文件</span></strong></article>`).join('')}</div><p class="io-category-note">${esc(vm.classificationScope || '当前分类结果')}；分类数量沿用已有结果，不将不同维度的数量相加作为文件总数。</p>${vm.fileCategoryCount > rows.length ? `<p class="io-category-note">展示前 ${count(rows.length)} 类，共 ${count(vm.fileCategoryCount)} 类；完整分类见智能目录。</p>` : ''}` : '<p>当前尚未形成可用的文件分类，分类结果生成后会自动更新。</p>'}</section>`;
  }
  function ordinaryResearchReview(vm) {
    if (vm.kind !== 'ordinary') return '';
    if (vm.presentation) {
      const limits = list(vm.direction.limitations);
      const alternatives = vm.alternatives;
      return `<section class="io-research-review" aria-label="推荐取舍与限制">${limits.length ? `<details data-io-disclosure="research-limits"><summary>适用边界与待确认条件</summary><ul>${limits.map(limit => `<li>${esc(limit)}</li>`).join('')}</ul></details>` : ''}${alternatives.length ? `<details data-io-disclosure="research-alternatives"><summary>候选研究方向与取舍 · ${alternatives.length}</summary>${alternatives.map(candidate => `<article><h4>${esc(text(candidate.title))}</h4><p>${esc(text(candidate.research_gap))}</p><p class="io-alternative-note">${esc(text(candidate.selection_reason))}</p>${evidenceReferences(candidate.evidence_numbers)}</article>`).join('')}</details>` : ''}</section>`;
    }
    const direction = vm.direction || {};
    const limits = list(direction.limitations);
    const counter = evidenceRows(direction.counterevidence);
    const alternatives = Array.isArray(vm.alternatives) ? vm.alternatives : [];
    return `<section class="io-research-review" aria-label="推荐取舍与限制"><details data-io-disclosure="research-limits"><summary>反证、适用边界与待确认条件${counter.length ? ` · ${counter.length} 条反例线索` : ''}</summary>${counter.length ? `<p class="io-review-label">以下材料可能限制或挑战推荐，需核对对象、条件与比较口径。</p>${counter.map(e => ordinaryEvidence(direction, e)).join('')}` : '<p class="io-review-label">当前未提供可展示的反证线索，不表示不存在反例。</p>'}${limits.length ? `<ul>${limits.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : '<p class="io-alternative-note">当前未列出额外适用边界，需结合原文继续核验。</p>'}</details>${alternatives.length ? `<details data-io-disclosure="research-alternatives"><summary>候选研究方向与取舍 · ${alternatives.length}</summary>${alternatives.map(c => `<article><h4>${esc(text(c.title) || '候选方向待明确')}</h4><p>${esc(text(c.research_gap || c.rationale))}</p><p class="io-alternative-note"><strong>取舍依据</strong> ${esc(text(c.selection_reason) || '需进一步核对证据及资源条件。')}</p></article>`).join('')}</details>` : ''}</section>`;
  }
  function ordinaryResearchAudit(vm) {
    const audit = vm.researchAudit;
    if (vm.kind !== 'ordinary' || !audit || !Object.keys(audit).length) return '';
    // Pipeline counters need stricter scalar validation than general coverage:
    // arrays, booleans and whitespace must never appear as zero opportunities.
    const auditCount = value => (typeof value === 'number' || typeof value === 'string' && /^\d+$/.test(value)) && Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : null;
    const auditText = value => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
    const discovered = auditCount(audit.discovered_count);
    const refined = auditCount(audit.refined_count);
    const budget = auditCount(audit.refinement_budget);
    const deferred = Array.isArray(audit.deferred) ? audit.deferred : null;
    const conflict = audit.refinement_complete === true && (deferred && deferred.length > 0 || discovered != null && refined != null && refined < discovered);
    const state = conflict ? '待核对（记录不一致）' : audit.refinement_complete === true ? '已精查全部已发现机会' : audit.refinement_complete === false ? '精查未完成' : '完整状态未知';
    const rows = [['已发现机会', discovered], ['已精查候选', refined], ['本轮精查上限', budget], ['延期精查', deferred ? deferred.length : null]];
    return `<section class="io-opportunity-audit" aria-label="研究机会处理记录"><details data-io-disclosure="research-audit"><summary>研究机会处理记录${discovered != null && refined != null ? ` · 精查 ${count(refined)} / ${count(discovered)}` : ''}</summary><p class="io-audit-scope">${audit.coverage === 'all_available_topics_scanned' ? '已对当前可用主题进行轻量盘点；研究方向仅在已精查并通过证据审查的候选中比较。' : '主题轻量盘点范围尚未记录；当前研究比较范围限于已精查候选。'}</p><dl class="io-audit-counts">${rows.map(([label, value]) => `<div><dt>${label}</dt><dd>${count(value)}</dd></div>`).join('')}</dl><p class="io-audit-state"><strong>精查状态</strong> ${esc(state)}</p>${conflict ? '<p class="io-audit-note">完成标记与候选数量或延期清单不一致，需核对处理记录后确认范围。</p>' : ''}<p class="io-audit-note">精查完成只描述已发现机会的处理范围，不表示全部资料或所有研究问题均已核验。</p><p class="io-audit-note">背景支撑、已有工作、适用条件、反证线索、资源条件这五种证据角色来自一次有界召回后的分桶。各类材料可能重叠，缺项仍需补充核验。</p>${deferred && deferred.length ? `<h4>延期精查的机会</h4><ul class="io-audit-deferred">${deferred.slice(0, 12).map(item => {
      const row = item && typeof item === 'object' && !Array.isArray(item) ? item : {};
      const kind = Object.prototype.hasOwnProperty.call(OPPORTUNITY_KINDS, row.opportunity_kind) ? OPPORTUNITY_KINDS[row.opportunity_kind] : '';
      const reason = row.reason === 'bounded_refinement_budget' ? '本轮精查预算有限，留待后续精查。' : auditText(row.reason) || '延期原因未记录。';
      return `<li><span>${esc(auditText(row.topic || row.title) || '候选范围待明确')}${kind ? ` · ${esc(kind)}` : ''}</span><small>${esc(reason)}</small></li>`;
    }).join('')}</ul>${deferred.length > 12 ? `<p class="io-audit-note">列出前 12 项，共 ${count(deferred.length)} 项延期机会。</p>` : ''}` : `<p class="io-audit-note">${deferred ? '当前记录没有延期项。' : '延期清单尚未提供，不能据此确认没有延期机会。'}</p>`}</details></section>`;
  }
  // Ordinary packages expose summary_stage in the brief (parsed / preliminary /
  // deep / legacy); large packages keep their existing preliminary flag.
  const STAGE_LABELS = {parsed: '解析版简报 · 待摘要', preliminary: '初步简报 · 待深化', deep: '深度证据版简报'};
  function statusLabel(vm) {
    if (vm.ready && STAGE_LABELS[vm.stage]) return STAGE_LABELS[vm.stage];
    return vm.preliminary ? '初步简报 · 待深化' : vm.ready ? '研究简报已生成' : '待生成简报';
  }
  function render(vm) {
    const findings = vm.findings.length ? vm.findings : [vm.ready ? '当前简报尚未形成稳定发现，请结合下方原文继续核验。' : '等待解析和研究简报，结果生成后将展示关键发现。'];
    const meter = vm.total > 0 && vm.parsed != null ? Math.min(100, vm.parsed / vm.total * 100) : null;
    const points = values => `<ul>${values.map(item => `<li>${esc(item)}</li>`).join('')}</ul>`;
    const header = `<header class="io-header"><div><span class="io-eyebrow">${vm.kind === 'large' ? '大数据包情报' : '资料情报'}</span><h2>${esc(vm.title)}</h2><p>${esc(vm.presentation ? '基于当前已解析资料整理' : vm.root || '从资料构成到关键发现，让每个研究方向都有据可查。')}</p></div><span class="io-status${vm.preliminary ? ' is-preliminary' : ''}">${esc(statusLabel(vm))}</span></header>
      ${rejectionNotice(vm.catalogRejection)}
      <div class="io-metrics">${vm.metrics.map(([label,value,note,unit]) => `<div class="io-metric"><span>${esc(label)}</span><strong>${unit && value != null ? Math.round(value) + esc(unit) : count(value)}</strong><small>${esc(note)}</small></div>`).join('')}</div>`;
    const findingCard = `<section class="io-card io-findings"><div class="io-section-title"><span>关键发现</span><h3>关键发现与资料简报</h3></div>${vm.basics.length ? `<div class="io-lead">${points(vm.basics)}</div>` : ''}<ol class="io-finding-list">${findings.map((item,i) => `<li><span class="io-index">${String(i+1).padStart(2,'0')}</span><p>${esc(item)}</p></li>`).join('')}</ol></section>`;
    const coverageCard = `<section class="io-card io-coverage"><div class="io-section-title"><span>覆盖边界</span><h3>理解分析覆盖边界</h3></div>${meter == null ? '' : `<div class="io-meter-label"><span>深度解析 / 已盘点</span><strong>${count(vm.parsed)} / ${count(vm.total)}</strong></div><div class="io-meter" role="meter" aria-label="深度解析覆盖率" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${meter.toFixed(1)}"><i style="width:${meter.toFixed(1)}%"></i></div>`}<p>${vm.kind === 'large' ? '盘点、快速解析、深度解析是不同阶段。快速摘要不等于全文核验；模型摘要数量也不等于证据覆盖率。' : '覆盖率沿用当前研究简报的统计口径，不代表每条结论都已经逐字核验。请结合原文与代表文件判断。'}</p>${vm.failures > 0 ? `<p class="io-warning">${count(vm.failures)} 项解析异常需复核；下方保留失败原因与重试入口。</p>` : ''}<details${vm.limitations.length ? ' open' : ''}><summary>限制与待补析${vm.limitations.length ? ` · ${vm.limitations.length}` : ''}</summary>${vm.limitations.length ? points(vm.limitations) : '<p>当前简报未列出额外限制，这不表示不存在遗漏。</p>'}</details></section>`;
    const topicCard = `<section class="io-card io-topics"><div class="io-section-title"><span>资料构成</span><h3>${vm.kind === 'large' ? '主要资料构成' : '主要内容主题'}</h3></div>${vm.categories.length ? `<ul>${vm.categories.map(item => `<li><span>${esc(item.label)}</span><b>${count(item.value)}<small> 文件</small></b></li>`).join('')}</ul><p>展示主要条目，完整分布与筛选入口见下方图表。</p>` : '<p>当前还没有可展示的分类统计，解析完成后自动更新。</p>'}</section>`;
    if (vm.kind === 'ordinary') {
      return `${header}<div class="io-grid io-overview-intro">${findingCard}${fileClassification(vm)}</div><div class="io-overview-context">${coverageCard}</div>${researchFocus(vm)}${ordinaryResearchReview(vm)}${ordinaryResearchAudit(vm)}${ordinaryResearchEvidence(vm)}`;
    }
    return `${header}${researchFocus(vm)}${ordinaryResearchReview(vm)}${ordinaryResearchAudit(vm)}${findingCard}<div class="io-bottom">${coverageCard}${topicCard}</div>`;
  }
  function clear(id) {
    if (typeof document === 'undefined') return;
    const host = document.getElementById(id);
    if (host) host.remove();
  }
  function mount(id, anchor, vm) {
    if (!anchor) return false;
    let host = document.getElementById(id);
    if (!host) {
      host = document.createElement('section'); host.id = id; host.className = 'io-board';
      host.setAttribute('aria-label', '情报概览阅读面板');
      if (vm.kind === 'large') host.dataset.largeSection = 'overview';
      anchor.before(host);
    }
    // Polling must not collapse disclosures, move focus, or reset scroll.
    const html = render(vm);
    const detailSignature = JSON.stringify([(vm.direction || {}).evidence_chain, (vm.direction || {}).confidence_note]);
    const disclosureKey = el => el.dataset.ioDisclosure || (el.matches('.io-evidence') ? 'evidence' : el.closest('.io-focus-plan') ? 'plan' : 'coverage');
    if (host._ioHtml !== html || host._ioDetailSignature !== detailSignature) {
      const opened = new Map(Array.from(host.querySelectorAll('details')).map(el => [disclosureKey(el), el.open]));
      const active = document.activeElement;
      const focused = host.contains(active) && active.matches('summary') ? disclosureKey(active.parentElement) : null;
      const filePath = host.contains(active) ? active.getAttribute('data-brief-file') : null;
      const focusedFile = filePath == null ? null : {path: filePath, index: Array.from(host.querySelectorAll('[data-brief-file]')).filter(el => el.getAttribute('data-brief-file') === filePath).indexOf(active)};
      const focusedLink = host.contains(active) && active.matches('a[href]') ? {href: active.getAttribute('href'), index: Array.from(host.querySelectorAll('a[href]')).filter(el => el.getAttribute('href') === active.getAttribute('href')).indexOf(active)} : null;
      host.innerHTML = html; host._ioHtml = html; host._ioDetailSignature = detailSignature;
      const plan = host.querySelector('.io-focus-plan details');
      if (plan && !opened.has('plan') && !vm.presentation) plan.open = true;
      if (plan) plan.querySelector('summary').textContent = '怎么研究 · 预期产出';
      if (plan && vm.kind !== 'ordinary') plan.querySelectorAll('li').forEach(item => {
        const method = item.textContent, split = method.indexOf('→');
        if (split < 1 || !method.slice(split + 1).trim()) return;
        item.classList.add('io-method-step');
        const actionLabel = document.createElement('span'); actionLabel.textContent = '研究动作';
        const action = document.createElement('p'); action.textContent = method.slice(0, split).trim();
        const outcome = document.createElement('div'); outcome.className = 'io-method-output';
        const outputLabel = document.createElement('strong'); outputLabel.textContent = '预期产出';
        const outputText = document.createElement('span'); outputText.textContent = method.slice(split + 1).trim();
        outcome.append(outputLabel, outputText); item.replaceChildren(actionLabel, action, outcome);
      });
      const sources = host.querySelector('.io-focus-sources h5');
      if (sources) sources.textContent = '从这些代表资料开始';
      const recommendation = host.querySelector('.io-focus-main');
      const question = list((vm.direction || {}).research_questions || (vm.direction || {}).questions)[0];
      if (vm.kind !== 'ordinary' && recommendation && question) {
        const block = document.createElement('div'); block.className = 'io-core-question';
        const label = document.createElement('span'); label.textContent = '核心研究问题';
        const content = document.createElement('p'); content.textContent = question;
        block.append(label,content);
        recommendation.querySelector('.io-focus-rule').after(block);
      }
      const rawEvidence = (vm.direction || {}).evidence_chain;
      const evidence = Array.isArray(rawEvidence) ? rawEvidence.filter(item => item && typeof item === 'object') : [];
      if (vm.kind !== 'ordinary' && Array.isArray(evidence) && evidence.length) {
        const block = document.createElement('details'); block.className = 'io-evidence';
        block.innerHTML = `<summary>推荐依据 · 查看 ${evidence.length} 条支撑原文</summary><div>${evidence.map(item => `<article><strong>${esc(item.source_path || item.path || '来源待核验')}${item.page ? ` · 第 ${esc(item.page)} 页` : ''}</strong><p>${esc(item.text || item.original_text || item.snippet || '该条未提供可展示原文，请打开代表资料核验。')}</p></article>`).join('')}</div>`;
        host.querySelector('.io-focus').after(block);
      }
      const note=(vm.direction || {}).confidence_note;
      if(vm.kind !== 'ordinary' && note){const el=document.createElement('p');el.className='io-confidence-note';el.textContent=note;recommendation.querySelector('.io-focus-footer').before(el);}
      host.querySelectorAll('details').forEach(el => {
        const key = disclosureKey(el);
        if (opened.has(key)) el.open = opened.get(key);
        if (key === focused) el.querySelector('summary')?.focus({preventScroll:true});
      });
      if (focusedFile !== null) {
        const matches = Array.from(host.querySelectorAll('[data-brief-file]')).filter(el => el.getAttribute('data-brief-file') === focusedFile.path);
        (matches[focusedFile.index] || matches[0])?.focus({preventScroll:true});
      }
      if (focusedLink) {
        const matches = Array.from(host.querySelectorAll('a[href]')).filter(el => el.getAttribute('href') === focusedLink.href);
        (matches[focusedLink.index] || matches[0])?.focus({preventScroll:true});
      }
    }
    if (!host._ioEvidenceNavigation && typeof host.addEventListener === 'function') {
      host.addEventListener('click', event => {
        if (!event.target.closest('[data-io-evidence-number], [data-io-open-evidence]')) return;
        const appendix = host.querySelector('[data-io-disclosure="evidence"]');
        if (appendix) appendix.open = true;
      });
      host._ioEvidenceNavigation = true;
    }
    return true;
  }
  const api = {model,render,mount,clear,rejectionNotice};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.SJFXIntelligenceOverview = api;
})(typeof window === 'undefined' ? globalThis : window);
