(function () {
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {identified:'已识别主题', needs_more:'待补充采样', needs_ocr:'待 OCR', needs_review:'待核查', metadata_only:'仅元数据', restricted:'受限文件', unsupported:'暂不支持', legacy:'旧版预览'};
  const catalogLabels = {pending:'待语义分组',grouped:'已语义分组，正在提取名称',classified:'已按语义归类',deferred:'主题待确认',no_content:'待提取内容',legacy:'旧版目录'};
  function portrait(info) {
    if (!info || !info.schema) return '';
    const sources = (info.catalog_evidence || info.evidence || []).slice(0, 5).map(e => `<li><b>${esc(e.location)}</b>：${esc(e.text)}</li>`).join('');
    return `<details class="large-portrait"><summary>${esc(info.topic)} · ${esc(catalogLabels[info.catalog_status] || labels[info.recognition_status] || info.recognition_status)}</summary><p>${esc(info.classification_basis)}</p><small>${esc(info.grouping_basis || '')}</small>${info.fields?.length ? `<p>字段：${info.fields.map(esc).join(' · ')}</p>` : ''}${info.projects?.length ? `<p>项目/编号线索：${info.projects.map(esc).join(' · ')}</p>` : ''}${info.dates?.length ? `<p>样本时间线索：${info.dates.map(esc).join(' · ')}</p>` : ''}<ul>${sources}</ul><small>${info.catalog_source === 'deep_summary' ? '此归类已结合深度摘要更新。' : info.catalog_source === 'deep_content' ? '此归类已结合深度正文更新。' : '此归类依据内容预览，深度摘要完成后会更新。'}${esc((info.warnings || []).join('；'))}</small></details>`;
  }
  function catalog(data) {
    const smart = data?.smart || {};
    const states = Object.entries(smart.catalog_states || smart.recognition || {}).map(([k,v]) => `${catalogLabels[k] || labels[k] || k} ${v}`).join(' · ');
    const leaf = t => `<details class="large-result-node large-smart-category" data-large-node-type="${t.node_id ? 'group' : 'topic'}" data-large-node-value="${esc(t.node_id || t.topic)}"><summary><b>${esc(t.topic)}</b><span>${t.files} 个文件</span></summary><small>${esc(t.basis)}</small>${(t.sample || []).map(f => `<p><b>${esc(f.path)}</b><br>${esc(f.basis)}${(f.evidence || []).map(e => `<br><small>${esc(e.location)}：${esc(e.text).slice(0, 220)}</small>`).join('')}</p>`).join('')}</details>`;
    const byId=new Map([...(smart.navigation || []),...(smart.topics || [])].map(t=>[t.node_id,t]));
    const nodes=new Map((smart.branches || []).map(t=>[t.node_id,t]));
    const nested=new Set();
    const branchHTML=(branch,seen=new Set())=>{
      if(seen.has(branch.node_id))return '';
      const visited=new Set(seen);visited.add(branch.node_id);
      const body=(branch.child_nodes || branch.children || []).map(id=>{
        if(nodes.has(id))return branchHTML(nodes.get(id),visited);
        const item=byId.get(id);if(!item)return '';nested.add(id);return leaf(item);
      }).join('');
      return `<details class="large-result-node" data-large-node-type="group" data-large-node-value="${esc(branch.node_id)}"><summary><b>${esc(branch.name)}</b><span>${Number(branch.files) || 0} 个文件</span></summary><div class="lp-content-children">${body}</div></details>`;
    };
    const themes=(smart.branches || []).filter(b=>!b.parent_id).map(b=>branchHTML(b)).join('');
    const topics=themes+[...byId.values()].filter(t=>!nested.has(t.node_id)).map(leaf).join('');
    const categories = (smart.categories || []).map(c => `<details class="large-result-node" data-large-node-type="category" data-large-node-value="${esc(c.category)}"><summary>${esc(c.category)} · ${c.files}</summary><small>${(c.sample || []).map(f => esc(f.path)).join(' · ')}</small></details>`).join('');
    const names = {role:'文档用途', project:'项目/编号线索', entity:'参与者线索', date:'样本时间'};
    const facets = Object.entries(smart.facets || {}).map(([kind,items]) => `<details><summary>${names[kind]}（样本线索）</summary>${items.map(x => `<details data-large-node-type="${kind}" data-large-node-value="${esc(x.value)}"><summary>${esc(x.value)} · ${x.files}</summary></details>`).join('')}</details>`).join('');
    return `<p>${esc(states)}</p><small>目录按内容语义分组，名称结合代表原文归纳；可勾选文件进入深度摘要，完成后更新目录和概览。</small>${facets}<h4>内容主题 · ${smart.topic_total || 0} 组</h4>${topics || '<p>正在建立内容画像，已盘点文件仍可查看。</p>'}${smart.topics_truncated ? '<p>当前显示前200组；可按主题或内容搜索全部文件。</p>' : ''}<details><summary>按资料类别查看</summary>${categories}</details>`;
  }
  function pager(id, offset, limit, total) {
    return `<div class="button-row"><button type="button" id="${id}Prev" ${offset <= 0 ? 'disabled' : ''}>上一页</button><span>${total ? offset + 1 : 0}–${Math.min(offset + limit, total)} / ${total}</span><button type="button" id="${id}Next" ${offset + limit >= total ? 'disabled' : ''}>下一页</button></div>`;
  }
  window.SJFXLargePortrait = {portrait, catalog, pager};
})();
