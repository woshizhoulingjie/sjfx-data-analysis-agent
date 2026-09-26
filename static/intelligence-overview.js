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
    const direction = brief.recommended_research_direction || {};
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
    return {kind, title: brief.title || (large ? '大数据包情报概览' : '资料情报概览'),
      ready: Boolean(brief.available || brief.ready), preliminary: Boolean(brief.preliminary || (large && brief.available && !brief.ready)),
      basics: list(brief.basic_information), findings: list(brief.key_findings),
      limitations: list(brief.limitations), direction, total, metrics,
      categories: large ? Object.entries(source.categories || {}).sort((a,b) => Number(b[1]) - Number(a[1])).slice(0,6).map(([label, value]) => ({label,value:number(value)})) : items(source.topics).slice(0,6).map(item => ({label:text(item.topic),value:number(item.file_count)})),
      failures: Array.isArray(source.failures) ? source.failures.length : null,
      root: large ? '' : (source.package || {}).root || '',
      parsed: large ? number(coverage.deep_completed) : null};
  }
  function researchFocus(vm) {
    const direction = vm.direction || {};
    const questions = list(direction.research_questions || direction.questions);
    const files = list(direction.representative_documents);
    const methods = list(direction.methods);
    const evidence = Array.isArray(direction.evidence_chain) ? direction.evidence_chain.filter(item => item && typeof item === 'object') : [];
    const detailId = vm.kind === 'large' ? 'largeResultResearchDetails' : 'packageOverviewResearchDetails';
    const pills = [direction.priority ? `优先级 · ${text(direction.priority)}` : '', direction.confidence ? `简报置信度 · ${text(direction.confidence)}` : ''].filter(Boolean);
    return `<section class="io-focus${direction.title ? '' : ' is-waiting'}" aria-label="推荐研究方向"><div class="io-focus-main"><div class="io-focus-kicker"><span class="io-focus-mark" aria-hidden="true">↗</span><span>RECOMMENDED RESEARCH<br><b>推荐研究方向</b></span><span class="io-focus-stage">${vm.preliminary ? '初步建议' : direction.title ? '当前首选方向' : '等待资料解析'}</span></div><h3>${esc(direction.title || '让下一步研究，从有据可查的问题开始')}</h3><div class="io-focus-rule"></div><h4>为什么值得研究</h4><p class="io-focus-rationale">${esc(direction.rationale || '完成资料解析后，这里将给出有证据支撑的研究主题与推荐理由，不以文件名猜测结论。')}</p>${pills.length ? `<div class="io-focus-pills">${pills.map(pill=>`<span>${esc(pill)}</span>`).join('')}</div>` : ''}<div class="io-focus-footer"><span>${files.length ? `${files.length} 份代表资料` : '代表资料待形成'}${evidence.length ? ` · ${evidence.length} 条支撑原文` : ''}</span><a href="#${detailId}">查看研究详情 <span aria-hidden="true">↗</span></a></div></div><aside class="io-focus-plan"><span class="io-eyebrow">YOUR NEXT MOVE</span><h4>把方向变成研究路径</h4>${questions.length ? `<ol>${questions.map((question,index)=>`<li><span>${String(index+1).padStart(2,'0')}</span><p>${esc(question)}</p></li>`).join('')}</ol>` : '<p class="io-focus-empty">研究问题将在简报形成后出现。你仍可使用下方资料图表，自行选择问答范围。</p>'}${methods.length ? `<details><summary>建议研究方法 · ${methods.length}</summary><ul>${methods.map(method=>`<li>${esc(method)}</li>`).join('')}</ul></details>` : ''}${files.length ? `<div class="io-focus-sources"><h5>代表资料</h5>${files.slice(0,3).map(file=>vm.kind==='ordinary' ? `<button type="button" data-brief-file="${esc(file)}" title="${esc(file)}"><span aria-hidden="true">▤</span>${esc(file.split(/[\/]/).pop())}</button>` : `<span class="io-source-label" title="${esc(file)}"><span aria-hidden="true">▤</span>${esc(file.split(/[\/]/).pop())}</span>`).join('')}${files.length>3 ? `<a href="#${detailId}">查看全部 ${files.length} 份资料 →</a>` : ''}</div>` : ''}<p class="io-focus-disclaimer">推荐基于当前已解析资料，是研究起点，不是已证实的最终结论。</p></aside></section>`;
  }
  function render(vm) {
    const direction = vm.direction || {};
    const findings = vm.findings.length ? vm.findings : [vm.ready ? '当前简报尚未形成稳定发现，请结合下方原文继续核验。' : '等待解析和研究简报，结果生成后将展示关键发现。'];
    const meter = vm.total > 0 && vm.parsed != null ? Math.min(100, vm.parsed / vm.total * 100) : null;
    const points = values => `<ul>${values.map(item => `<li>${esc(item)}</li>`).join('')}</ul>`;
    return `<header class="io-header"><div><span class="io-eyebrow">INTELLIGENCE / ${vm.kind === 'large' ? 'LARGE PACKAGE' : 'PACKAGE'}</span><h2>${esc(vm.title)}</h2><p>${esc(vm.root || '从资料构成到关键发现，让每个研究方向都有据可查。')}</p></div><span class="io-status${vm.preliminary ? ' is-preliminary' : ''}">${vm.preliminary ? '初步简报 · 待深化' : vm.ready ? '研究简报已生成' : '待生成简报'}</span></header>
      <div class="io-metrics">${vm.metrics.map(([label,value,note,unit]) => `<div class="io-metric"><span>${esc(label)}</span><strong>${unit && value != null ? Math.round(value) + esc(unit) : count(value)}</strong><small>${esc(note)}</small></div>`).join('')}</div>
      ${researchFocus(vm)}
      <section class="io-card io-findings"><div class="io-section-title"><span>02 / BRIEFING</span><h3>关键发现与资料简报</h3></div>${vm.basics.length ? `<div class="io-lead">${points(vm.basics)}</div>` : ''}<ol class="io-finding-list">${findings.map((item,i) => `<li><span class="io-index">${String(i+1).padStart(2,'0')}</span><p>${esc(item)}</p></li>`).join('')}</ol></section>
      <div class="io-bottom"><section class="io-card io-coverage"><div class="io-section-title"><span>03 / EVIDENCE BOUNDARY</span><h3>理解分析覆盖边界</h3></div>${meter == null ? '' : `<div class="io-meter-label"><span>深度解析 / 已盘点</span><strong>${count(vm.parsed)} / ${count(vm.total)}</strong></div><div class="io-meter" role="meter" aria-label="深度解析覆盖率" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${meter.toFixed(1)}"><i style="width:${meter.toFixed(1)}%"></i></div>`}<p>${vm.kind === 'large' ? '盘点、快速解析、深度解析是不同阶段。快速摘要不等于全文核验；模型摘要数量也不等于证据覆盖率。' : '覆盖率沿用当前研究简报的统计口径，不代表每条结论都已经逐字核验。请结合原文与代表文件判断。'}</p>${vm.failures > 0 ? `<p class="io-warning">${count(vm.failures)} 项解析异常需复核；下方保留失败原因与重试入口。</p>` : ''}<details${vm.limitations.length ? ' open' : ''}><summary>限制与待补析${vm.limitations.length ? ` · ${vm.limitations.length}` : ''}</summary>${vm.limitations.length ? points(vm.limitations) : '<p>当前简报未列出额外限制，这不表示不存在遗漏。</p>'}</details></section>
      <section class="io-card io-topics"><div class="io-section-title"><span>04 / CONTENT SIGNALS</span><h3>${vm.kind === 'large' ? '主要资料构成' : '主要内容主题'}</h3></div>${vm.categories.length ? `<ul>${vm.categories.map(item => `<li><span>${esc(item.label)}</span><b>${count(item.value)}<small> 文件</small></b></li>`).join('')}</ul><p>展示主要条目，完整分布与筛选入口见下方图表。</p>` : '<p>当前还没有可展示的分类统计，解析完成后自动更新。</p>'}</section></div>`;
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
    const disclosureKey = el => el.matches('.io-evidence') ? 'evidence' : el.closest('.io-focus-plan') ? 'plan' : 'coverage';
    if (host._ioHtml !== html || host._ioDetailSignature !== detailSignature) {
      const opened = new Map(Array.from(host.querySelectorAll('details')).map(el => [disclosureKey(el), el.open]));
      const active = document.activeElement;
      const focused = host.contains(active) && active.matches('summary') ? disclosureKey(active.parentElement) : null;
      const focusedFile = host.contains(active) ? active.getAttribute('data-brief-file') : null;
      host.innerHTML = html; host._ioHtml = html; host._ioDetailSignature = detailSignature;
      const plan = host.querySelector('.io-focus-plan details');
      if (plan && !opened.has('plan')) plan.open = true;
      if (plan) plan.querySelector('summary').textContent = '怎么研究 · 预期产出';
      if (plan) plan.querySelectorAll('li').forEach(item => {
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
      if (recommendation && question) {
        const block = document.createElement('div'); block.className = 'io-core-question';
        const label = document.createElement('span'); label.textContent = '核心研究问题';
        const content = document.createElement('p'); content.textContent = question;
        block.append(label,content);
        recommendation.querySelector('.io-focus-rule').after(block);
      }
      const rawEvidence = (vm.direction || {}).evidence_chain;
      const evidence = Array.isArray(rawEvidence) ? rawEvidence.filter(item => item && typeof item === 'object') : [];
      if (Array.isArray(evidence) && evidence.length) {
        const block = document.createElement('details'); block.className = 'io-evidence';
        block.innerHTML = `<summary>推荐依据 · 查看 ${evidence.length} 条支撑原文</summary><div>${evidence.map(item => `<article><strong>${esc(item.source_path || item.path || '来源待核验')}${item.page ? ` · 第 ${esc(item.page)} 页` : ''}</strong><p>${esc(item.text || item.original_text || item.snippet || '该条未提供可展示原文，请打开代表资料核验。')}</p></article>`).join('')}</div>`;
        host.querySelector('.io-focus').after(block);
      }
      const note=(vm.direction || {}).confidence_note;
      if(note){const el=document.createElement('p');el.className='io-confidence-note';el.textContent=note;recommendation.querySelector('.io-focus-footer').before(el);}
      host.querySelectorAll('details').forEach(el => {
        const key = disclosureKey(el);
        if (opened.has(key)) el.open = opened.get(key);
        if (key === focused) el.querySelector('summary')?.focus({preventScroll:true});
      });
      if (focusedFile !== null) {
        Array.from(host.querySelectorAll('[data-brief-file]')).find(el => el.getAttribute('data-brief-file') === focusedFile)?.focus({preventScroll:true});
      }
    }
    return true;
  }
  const api = {model,render,mount,clear};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.SJFXIntelligenceOverview = api;
})(typeof window === 'undefined' ? globalThis : window);
