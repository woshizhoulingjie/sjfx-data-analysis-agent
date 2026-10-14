(function () {
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {identified:'已有内容线索', needs_more:'待补充采样', needs_ocr:'待文字识别', needs_review:'待核查', metadata_only:'尚无可读内容', restricted:'受限文件', unsupported:'暂不支持', legacy:'尚无内容画像', failed:'快速解析失败'};
  const catalogLabels = {pending:'待语义分组',grouped:'已分组，名称待完善',classified:'已按语义归类',deferred:'已有内容分组',no_content:'尚无可读内容',legacy:'尚无内容画像',failed:'快速解析失败'};
  const count = (value) => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  function catalogSummary(data, packageStatus = {}) {
    const smart = data?.smart || {}, stats = smart.stats || {}, naming = smart.naming || {};
    const navigation = new Map([...(smart.navigation || []), ...(smart.topics || [])].filter(n => n.node_id).map(n => [n.node_id, n]));
    const total = count(stats.total_files ?? data?.files_total);
    const grouped = count(stats.grouped_files ?? [...navigation.values()].filter(n => nodeKind(n) === 'leaf').reduce((sum, n) => sum + count(n.files), 0));
    const members = count(stats.member_files ?? [...navigation.values()].filter(n => nodeKind(n) !== 'unclassified').reduce((sum, n) => sum + count(n.files), 0));
    const unassigned = (smart.branches || []).filter(b => b.node_id === 'branch:unassigned' || b.status === 'pending');
    const pending = count(stats.display_pending_theme_files ?? stats.pending_theme_files ?? unassigned.filter(b => b.kind !== 'independent').reduce((sum, b) => sum + count(b.semantic_pending_files ?? Math.max(0, count(b.pending_files ?? b.files) - count(b.system_files) - count(b.staging_files))), 0));
    const independent = count(stats.display_independent_files ?? stats.independent_files ?? unassigned.reduce((sum, b) =>
      sum + (branchSections(b, navigation).find(s => s.key === 'independent')?.files
        ?? count(b.display_independent_files ?? b.independent_files ?? (b.kind === 'independent' ? b.files : 0))), 0));
    const themes = count(stats.theme_nodes ?? smart.theme_total);
    const roots = count(stats.root_theme_nodes ?? (smart.branches || []).filter(b => b.kind === 'theme' && !b.parent_id).length);
    const rootKnown = stats.root_theme_nodes != null || (smart.branches || []).some(b => b.kind === 'theme');
    const leaves = count(stats.leaf_nodes ?? smart.leaf_total ?? naming.total_nodes ?? navigation.size);
    const named = count(stats.named_leaf_nodes ?? naming.named_nodes);
    const unresolved = count(stats.leaf_nodes != null ? Math.max(0, leaves - named) : naming.pending_nodes ?? Math.max(0, leaves - named));
    const status = String(packageStatus.status || smart.catalog_pending?.status || '');
    const stopped = ['paused', 'cancelled', 'failed'].includes(status);
    const active = Boolean(naming.active && !stopped);
    const failedJobs = count(naming.jobs?.failed);
    const deferredJobs = count(naming.jobs?.deferred);
    const reviewLeaves = count(stats.review_leaf_nodes);
    const reviewThemes = count(stats.review_theme_nodes);
    let message = smart.catalog_pending?.message || '';
    if (status === 'paused') message = '数据包已暂停，当前目录保留已完成结果。';
    else if (status === 'cancelled') message = '数据包已停止，当前目录保留已完成结果。';
    else if (status === 'failed') message = '数据包处理失败，当前目录保留已完成结果。';
    else if (active) message = count(naming.jobs?.running) || naming.hierarchy_status === 'running'
      ? '正在完善分组名称与主题层级。' : '分组命名与主题整理待处理。';
    else if (naming.status === 'exhausted') message = '本轮命名预算已结束，可继续完善名称。';
    else if (failedJobs || deferredJobs) message = '部分命名任务需要重试。';
    else if (unresolved) message = '部分分组名称尚待完善。';
    else if (pending) message = '分组名称已保存，部分分组尚待归入上层主题。';
    else if (leaves) message = '当前分组名称已同步。';
    else message = '尚未生成内容分组，已盘点文件可从原始目录查看。';
    if (unresolved && !active) message += ` ${unresolved} 个分组待命名。`;
    if (failedJobs) message += ` ${failedJobs} 个命名任务失败。`;
    if (deferredJobs) message += ` ${deferredJobs} 个命名任务待重试。`;
    const publication = smart.catalog_publication || {};
    const publicationText = publication.version ? `目录第 ${count(publication.version)} 版 · 已覆盖 ${count(publication.covered_files)} / ${count(publication.scope_files ?? grouped)} 个语义分组文件 · ${publication.state === 'partial' ? '部分快照' : '当前快照'} ${String(publication.snapshot_id || '').slice(0,12)}` : '';
    if (publication.state === 'partial') message += ' 当前为部分快照，新增或变更范围仍待整理。';
    return {
      total, themes, roots, rootKnown, leaves, grouped, pending, independent, publicationText,
      ungrouped: count(stats.ungrouped_files ?? Math.max(0, total - members)),
      content: count(stats.content_files ?? smart.content_files),
      attempted: count(stats.quick_attempted_files ?? smart.attempted_files),
      quickFailed: count(stats.quick_failed_files), systemFiles: count(stats.system_bucket_files), stagingFiles: count(stats.staging_files),
      named, namedFiles: count(stats.named_files ?? naming.named_files),
      unresolved, reviewLeaves, reviewThemes, active, stopped, message, canImprove: leaves > 0 && !active && !stopped,
      text: `${themes} 个主题${rootKnown ? `（${roots} 个顶层）` : ''} · ${leaves} 个叶子分组`,
    };
  }
  function reviewText() {
    // Review facts remain in the catalog payload; the main directory does not display them.
    return '';
  }
  function nodeState(item) {
    if (item.naming_status === 'failed' || item.state === 'failed') return '解析失败';
    if (String(item.node_id || '').startsWith('km-other-')) return '待内容分组';
    if (String(item.node_id || '').startsWith('km-pending-')) return '聚类暂存';
    if (item.naming_status === 'stale' || item.status === 'stale') return '名称待复核';
    if (item.naming_status === 'pending') return '待命名';
    if (item.naming_status === 'extractive') return '名称待完善';
    if (item.naming_status === 'unread') return labels[item.state] || '尚未归组';
    if (item.kind === 'independent') return '独立分组';
    if (item.kind === 'mixed') return '独立分组与待处理范围';
    if (item.kind === 'pending' || item.status === 'pending') return '待归并';
    return '';
  }
  function nodeKind(item) {
    const id = String(item.node_id || '');
    return id.startsWith('preview:') ? 'unclassified' : /^km-(other|pending)-/.test(id) ? 'staging' : 'leaf';
  }
  function nodeLabel(item) {
    const label = item.topic || item.name;
    return String(item.node_id || '').startsWith('km-other-') && ['待归并主题', '内容正在归纳', '独立主题与待归并目录'].includes(label)
      ? '待进一步分组' : label;
  }
  function branchSections(branch, byId) {
    if (branch.node_id !== 'branch:unassigned') return [];
    const baseIndependentIds = branch.display_independent_leaf_ids ?? branch.independent_leaf_ids ?? [];
    const independentIds = [...new Set([
      ...baseIndependentIds,
      ...(branch.review_independent_leaf_ids || []),
      ...(branch.pending_name_independent_leaf_ids || []),
    ])];
    const independentFiles = count(branch.display_independent_files ?? branch.independent_files)
      + ((branch.review_independent_leaf_ids || []).every(id => baseIndependentIds.includes(id)) ? 0 : count(branch.review_independent_files))
      + ((branch.pending_name_independent_leaf_ids || []).every(id => baseIndependentIds.includes(id)) ? 0 : count(branch.pending_name_independent_files));
    const sections = [
      ['independent', '独立分组', independentIds, independentFiles],
      ['outliers', '离群内容', branch.system_leaf_ids, branch.system_files],
      ['semantic-pending', '待上层主题整理', branch.display_semantic_pending_leaf_ids ?? branch.semantic_pending_leaf_ids,
        branch.display_semantic_pending_files ?? branch.semantic_pending_files],
      ['staging', '聚类暂存', branch.staging_leaf_ids, branch.staging_files],
    ];
    const seen = new Set();
    return sections.filter(([, , ids]) => ids?.length).map(([key, label, sourceIds, files]) => {
      const ids = [...new Set(sourceIds)].filter(id => !seen.has(id));
      ids.forEach(id => seen.add(id));
      const complete = byId && ids.every(id => byId.has(id) && byId.get(id).files != null);
      return {key, label, ids, files: complete ? ids.reduce((sum, id) => sum + count(byId.get(id).files), 0) : count(files)};
    }).filter(section => section.ids.length);
  }
  function branchLabel(branch) {
    const sections = branchSections(branch);
    return sections.length ? sections.map(s => `${s.label} ${s.files}`).join(' · ') : branch.name;
  }
  function portrait(info) {
    if (!info || !info.schema) return '';
    const sources = (info.catalog_evidence || info.evidence || []).slice(0, 5).map(e => `<li><b>${esc(e.location)}</b>：${esc(e.text)}</li>`).join('');
    return `<details class="large-portrait"><summary>${esc(info.topic)} · ${esc(catalogLabels[info.catalog_status] || labels[info.recognition_status] || info.recognition_status)}</summary><p>${esc(info.classification_basis)}</p><small>${esc(info.grouping_basis || '')}</small>${info.fields?.length ? `<p>字段：${info.fields.map(esc).join(' · ')}</p>` : ''}${info.projects?.length ? `<p>项目/编号线索：${info.projects.map(esc).join(' · ')}</p>` : ''}${info.dates?.length ? `<p>样本时间线索：${info.dates.map(esc).join(' · ')}</p>` : ''}<ul>${sources}</ul><small>${info.catalog_source === 'deep_summary' ? '此归类已结合深度摘要更新。' : info.catalog_source === 'deep_content' ? '此归类已结合深度正文更新。' : '此归类依据内容预览，深度摘要完成后会更新。'}${esc((info.warnings || []).join('；'))}</small></details>`;
  }
  function catalog(data) {
    const smart = data?.smart || {};
    const summary = catalogSummary(data);
    const states = Object.entries(smart.catalog_states || smart.recognition || {}).filter(([k]) => !['deferred', 'needs_review'].includes(k)).map(([k,v]) => `${catalogLabels[k] || labels[k] || k} ${v}`).join(' · ');
    const leaf = t => `<details class="large-result-node large-smart-category" data-large-node-type="${t.node_id ? 'group' : 'topic'}" data-large-node-value="${esc(t.node_id || t.topic)}"><summary><b>${esc(nodeLabel(t))}</b><span>${count(t.files)} 个文件</span></summary>${nodeState(t) ? `<small class="lp-node-state">${esc(nodeState(t))}</small>` : ''}<small>${esc(t.basis || t.description || t.error)}</small>${(t.sample || []).map(f => `<p><b>${esc(f.path)}</b><br>${esc(f.basis)}${(f.evidence || []).map(e => `<br><small>${esc(e.location)}：${esc(e.text).slice(0, 220)}</small>`).join('')}</p>`).join('')}</details>`;
    const byId=new Map([...(smart.navigation || []),...(smart.topics || [])].map(t=>[t.node_id,t]));
    const nodes=new Map((smart.branches || []).map(t=>[t.node_id,t]));
    const nested=new Set(), renderedBranches=new Set();
    const branchHTML=(branch,seen=new Set())=>{
      if(seen.has(branch.node_id) || renderedBranches.has(branch.node_id))return '';
      renderedBranches.add(branch.node_id);
      const visited=new Set(seen);visited.add(branch.node_id);
      const renderChild=id=>{
        if(nodes.has(id))return branchHTML(nodes.get(id),visited);
        const item=byId.get(id);if(!item || nested.has(id))return '';nested.add(id);return leaf(item);
      };
      const sections=branchSections(branch,byId);
      if(sections.length) return sections.map(s=>`<details class="large-result-node lp-display-section"><summary><b>${esc(s.label)}</b><span>${s.files} 个文件</span></summary><div class="lp-content-children">${s.ids.map(renderChild).join('')}</div></details>`).join('');
      const body=(branch.child_nodes || branch.children || []).map(renderChild).join('');
      return `<details class="large-result-node" data-large-node-type="group" data-large-node-value="${esc(branch.node_id)}"><summary><b>${esc(branchLabel(branch))}</b><span>${count(branch.files)} 个文件</span></summary>${nodeState(branch) ? `<small class="lp-node-state">${esc(nodeState(branch))}</small>` : ''}<div class="lp-content-children">${body}</div></details>`;
    };
    const themes=(smart.branches || []).filter(b=>!b.parent_id || !nodes.has(b.parent_id)).map(b=>branchHTML(b)).join('');
    const loose=[...byId.values()].filter(t=>!nested.has(t.node_id));
    const previews=loose.filter(t=>String(t.node_id || '').startsWith('preview:'));
    const topics=themes+loose.filter(t=>!previews.includes(t)).map(leaf).join('');
    const categories = (smart.categories || []).map(c => `<details class="large-result-node" data-large-node-type="category" data-large-node-value="${esc(c.category)}"><summary>${esc(c.category)} · ${c.files}</summary><small>${(c.sample || []).map(f => esc(f.path)).join(' · ')}</small></details>`).join('');
    const names = {role:'文档用途', project:'项目/编号线索', entity:'参与者线索', date:'样本时间'};
    const facets = Object.entries(smart.facets || {}).map(([kind,items]) => `<details><summary>${names[kind]}（样本线索）</summary>${items.map(x => `<details data-large-node-type="${kind}" data-large-node-value="${esc(x.value)}"><summary>${esc(x.value)} · ${x.files}</summary></details>`).join('')}</details>`).join('');
    return `<p class="lp-catalog-summary">${esc(summary.text)}</p><p class="lp-catalog-message">${esc(summary.message)}</p>${summary.publicationText ? `<p>${esc(summary.publicationText)}</p>` : ''}<small>主题 → 内容分组 → 文件。各节点数量为该范围内的文件数，上下级数量不相加。</small><h4>主题与分组</h4>${topics || '<p>尚未生成内容分组，已盘点文件仍可查看。</p>'}${summary.ungrouped ? `<h4>未归组文件 · ${summary.ungrouped} 个</h4>${previews.map(leaf).join('') || '<p>未归组文件可从原始目录查看。</p>'}` : ''}${smart.topics_truncated ? '<p>部分分组详情尚未加载，可按节点查看文件或搜索全部文件。</p>' : ''}<details><summary>按资料类别查看</summary>${categories}</details>${facets}<details><summary>目录整理状态（文件数）</summary><p>${esc(states)}</p></details>`;
  }
  function pager(id, offset, limit, total) {
    return `<div class="button-row"><button type="button" id="${id}Prev" ${offset <= 0 ? 'disabled' : ''}>上一页</button><span>${total ? offset + 1 : 0}–${Math.min(offset + limit, total)} / ${total}</span><button type="button" id="${id}Next" ${offset + limit >= total ? 'disabled' : ''}>下一页</button></div>`;
  }
  window.SJFXLargePortrait = {portrait, catalog, pager, catalogSummary, reviewText, nodeState, nodeKind, nodeLabel, branchSections, branchLabel};
})();
