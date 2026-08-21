const state = {
  scan: null,
  selected: null,
  summary: null,
  analysis: null,
  summaries: new Map(),
  activeTree: 'physical',
  jobId: null,
  modelGenerationEnabled: null,
  lastRetrievalId: null,
  selectedNodes: new Map()
};

const $ = (id) => document.getElementById(id);


function toast(message, error = false) {
  const el = $('toast');

  el.textContent = message;

  el.className =
    'toast show' +
    (error ? ' error' : '');

  clearTimeout(
    window.__toastTimer
  );

  window.__toastTimer = setTimeout(
    () => el.className = 'toast',
    5200
  );
}


async function api(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  let token = window.localStorage.getItem('sjfx_api_token') || '';
  if (token) headers['X-SJFX-Token'] = token;
  let response = await fetch(url, { ...options, headers });
  if (response.status === 401) {
    token = window.prompt('请输入 SJFX API Token（首次访问输入一次即可）', '') || '';
    if (token) {
      window.localStorage.setItem('sjfx_api_token', token);
      headers['X-SJFX-Token'] = token;
      response = await fetch(url, { ...options, headers });
    }
  }
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || '请求失败');
  return data;
}

// Downloads cannot use a plain <a href> when API-token authentication is on:
// browsers do not attach the X-SJFX-Token header to a normal navigation.
// Fetch the artifact with the same authenticated header as other API calls,
// then save the returned Blob locally.
async function authenticatedDownload(url) {
  const headers = {};
  let token = window.localStorage.getItem('sjfx_api_token') || '';
  if (token) headers['X-SJFX-Token'] = token;
  let response = await fetch(url, { headers });
  if (response.status === 401) {
    token = window.prompt('请输入 SJFX API Token（首次访问输入一次即可）', '') || '';
    if (token) {
      window.localStorage.setItem('sjfx_api_token', token);
      headers['X-SJFX-Token'] = token;
      response = await fetch(url, { headers });
    }
  }
  if (!response.ok) {
    let message = '下载失败';
    try { message = (await response.json()).error || message; } catch (_) {}
    throw new Error(message);
  }
  const disposition = response.headers.get('Content-Disposition') || '';
  const encoded = (disposition.match(/filename\*=UTF-8''([^;]+)/i) || [])[1];
  const quoted = (disposition.match(/filename="?([^";]+)"?/i) || [])[1];
  const filename = encoded ? decodeURIComponent(encoded) : (quoted || 'sjfx-download');
  const blobUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = blobUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(blobUrl);
}

function downloadLink(url, label) {
  return `<a class="download-link authenticated-download" href="#" data-download-url="${escapeHtml(url)}">${escapeHtml(label)}</a>`;
}

document.addEventListener('click', async (event) => {
  const link = event.target.closest('.authenticated-download');
  if (!link) return;
  event.preventDefault();
  try {
    link.classList.add('disabled');
    await authenticatedDownload(link.dataset.downloadUrl || '');
    toast('文件已开始下载');
  } catch (error) {
    toast(error.message || '下载失败', true);
  } finally {
    link.classList.remove('disabled');
  }
});




function setBusy(button, busy, label) {
  if (busy) {
    button.dataset.label =
      button.textContent;

    button.textContent =
      label || '处理中…';

    button.disabled = true;
  } else {
    button.textContent =
      button.dataset.label
      || button.textContent;

    button.disabled = false;
  }
}


function escapeHtml(value) {
  const d =
    document.createElement('div');

  d.textContent =
    String(value ?? '');

  return d.innerHTML;
}


function icon(node) {
  if (node.kind === 'evidence') {
    return '⌁';
  }

  if (node.kind === 'directory') {
    return '▾';
  }

  if (
    node.kind === 'group'
    || node.kind === 'analysis_root'
  ) {
    return '◈';
  }

  return '·';
}


function exportSelectionKey(node) {
  if (node.kind === 'evidence') {
    return `evidence:${node.source_path || ''}:${node.evidence?.evidence_id || node.name || ''}`;
  }
  return node.node_id
    ? `node:${node.node_id}`
    : `${node.kind || 'node'}:${node.path || node.name || ''}`;
}


function canExportNode(node) {
  return Boolean(node) && node.kind !== 'analysis_root';
}


function exportPayloadNode(node) {
  return {
    kind: node.kind,
    path: node.path || null,
    node_id: node.node_id || null,
    name: node.name || null,
    source_path: node.source_path || null,
    evidence_id: node.evidence?.evidence_id || null
  };
}


function updateSelectionCart() {
  const values = [...state.selectedNodes.values()];
  const cart = $('selectionCart');
  if (!values.length) {
    cart.className = 'selection-cart empty';
    cart.textContent = '可勾选多个主题、目录、文档或原文证据，组合导出时会自动去重。';
  } else {
    cart.className = 'selection-cart';
    cart.innerHTML = `<strong>已勾选 ${values.length} 个节点</strong> · 组合导出将按源文件去重<br><small>${escapeHtml(values.slice(0, 5).map(x => x.name || x.path).join('、'))}${values.length > 5 ? '…' : ''}</small>`;
  }
  $('exportBtn').disabled = !state.scan || (!values.length && !canExportNode(state.selected));
}


function renderTreeNode(node) {
  const li =
    document.createElement('li');

  const row =
    document.createElement('div');

  row.className =
    'tree-row';

  row.dataset.path =
    node.path || '';

  if (node.node_id) {
    row.dataset.nodeId =
      node.node_id;
  }

  const twisty =
    document.createElement('span');

  twisty.className =
    'twisty';

  if (canExportNode(node)) {
    const picker = document.createElement('input');
    picker.type = 'checkbox';
    picker.className = 'tree-picker';
    picker.checked = state.selectedNodes.has(exportSelectionKey(node));
    picker.title = '加入组合导出';
    picker.onclick = (event) => {
      event.stopPropagation();
      const key = exportSelectionKey(node);
      if (picker.checked) {
        state.selectedNodes.set(key, exportPayloadNode(node));
      } else {
        state.selectedNodes.delete(key);
      }
      updateSelectionCart();
    };
    row.appendChild(picker);
  }

  const hasChildren =
    Boolean(
      node.children?.length
    );

  twisty.textContent =
    hasChildren
      ? '▾'
      : '';

  const label =
    document.createElement('span');

  label.textContent =
    `${icon(node)}  ${node.name || '未命名节点'}`;

  const meta =
    document.createElement('span');

  meta.className =
    'file-meta';

  if (node.kind === 'directory') {
    meta.textContent =
      `${node.file_count || 0} 文件 / ${node.directory_count || 0} 目录`;
  } else if (node.kind === 'file') {
    meta.textContent =
      node.size_human || '';
  } else if (node.kind === 'evidence') {
    meta.textContent =
      node.evidence?.page
        ? `第 ${node.evidence.page} 页`
        : (node.evidence?.section || '原文片段');
  } else if (node.kind === 'group') {
    meta.textContent =
      `${node.dimension || '内容主题'} · ${node.file_count || (node.member_paths || []).length || 0} 文件${node.coverage ? ` · 已分析 ${node.coverage.parsed_files || 0}/${node.coverage.inventory_files || 0}` : ''}`;
  } else {
    meta.textContent =
      node.dimension || '';
  }

  row.append(
    twisty,
    label,
    meta
  );

  li.appendChild(row);

  let childList = null;

  if (hasChildren) {
    childList =
      document.createElement('ul');

    node.children.forEach(
      child =>
        childList.appendChild(
          renderTreeNode(child)
        )
    );

    li.appendChild(
      childList
    );

    twisty.onclick = (event) => {
      event.stopPropagation();

      const hidden =
        childList.style.display
        === 'none';

      childList.style.display =
        hidden
          ? ''
          : 'none';

      twisty.textContent =
        hidden
          ? '▾'
          : '▸';
    };
  }

  row.onclick =
    () => selectNode(
      node,
      row
    );

  return li;
}


function renderTree(tree) {
  const host =
    $('tree');

  host.innerHTML = '';

  host.classList.remove(
    'empty'
  );

  const ul =
    document.createElement('ul');

  ul.appendChild(
    renderTreeNode(tree)
  );

  host.appendChild(ul);
}

// Show a physical-tree placeholder immediately after a scan is submitted.
// The worker replaces this skeleton with the complete inventory as soon as
// directory enumeration finishes, while parsing and semantic analysis continue.
function renderInitialPhysicalTree(rootPath) {
  const raw = String(rootPath || '').trim();
  const name = raw.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || raw || '待扫描目录';
  renderTree({
    kind: 'directory',
    name,
    path: raw || '.',
    file_count: 0,
    directory_count: 0,
    children: [],
    scan_pending: true
  });
  const row = $('tree').querySelector('.tree-row');
  if (row) {
    const meta = row.querySelector('.file-meta');
    if (meta) meta.textContent = '正在盘点…';
  }
}


function summaryKey(
  path,
  type = 'folder'
) {
  return `${type}:${path}`;
}


function localSummaryFor(node) {
  if (node.kind === 'directory') {
    return (
      state.summaries.get(
        summaryKey(
          node.path,
          'folder'
        )
      )
      ||
      state.analysis
        ?.node_summaries
        ?.[node.path]
    );
  }

  /*
   * 新增：
   * 虚拟主题节点现在拥有 node_id，
   * 因此可以拥有自己独立的摘要。
   */
  if (
    node.kind === 'group'
    && node.node_id
  ) {
    const cached =
      state.summaries.get(
        summaryKey(
          `node:${node.node_id}`,
          'folder'
        )
      );

    if (cached) {
      return cached;
    }

    return {
      title:
        `${node.name} 分析节点`,

      summary:
        node.summary,

      topics:
        node.related_topics || [],

        representative_documents:
          (
            node.member_paths || []
        ).slice(
          0,
          5
        ),

        evidence_chain:
          node.evidence_chain || [],

        conclusion_evidence:
          node.conclusion_evidence || [],

        file_count:
          node.file_count || 0,
    };
  }

  if (
    node.kind ===
    'analysis_root'
  ) {
    return {
      title:
        `${node.name} 数据包总体`,

      summary:
        node.summary,

      topics: [],

      evidence_chain:
        node.evidence_chain || []
    };
  }

  return null;
}


async function selectNode(
  node,
  row
) {
  document
    .querySelectorAll(
      '.tree-row.selected'
    )
    .forEach(
      el =>
        el.classList.remove(
          'selected'
        )
    );

  row.classList.add(
    'selected'
  );

  state.selected = node;
  state.summary = null;

  /*
   * 换节点以后，
   * 上一次二次检索不能继续使用。
   */
  state.lastRetrievalId = null;

  const isVirtualGroup =
    node.kind === 'group'
    && Boolean(
      node.node_id
    );

  const unsupportedVirtual =
    node.kind ===
      'analysis_root'
    || node.kind === 'evidence'
    ||
    (
      !node.path
      && !isVirtualGroup
    );

  $('selection')
    .classList
    .remove(
      'empty'
    );

  let detailText = '';

  if (
    node.kind ===
    'directory'
  ) {
    detailText =
      `当前层 ${node.direct_file_count || 0} 个文件、`
      +
      `${node.direct_directory_count || 0} 个子目录；`
      +
      `递归共 ${node.file_count || 0} 个文件、`
      +
      `${node.directory_count || 0} 个目录 · `
      +
      `${node.size_human || ''}`;
  } else if (
    node.kind ===
    'file'
  ) {
    detailText =
      `${node.extension || '未知类型'} · `
      +
      `${node.size_human || ''}`;
  } else if (
    isVirtualGroup
  ) {
    detailText =
      `分类维度：${node.dimension || '内容主题'} · `
      +
      `${node.file_count || (node.member_paths || []).length} 个文件`;
  } else {
    detailText =
      `分类维度：${node.dimension || '数据包总体'}`;
  }

  $('selection').innerHTML =
    `<strong>${escapeHtml(node.name || '分析节点')}</strong><br>`
    +
    `${escapeHtml(
      node.path
      ||
      (
        isVirtualGroup
          ? '自适应主题节点'
          : '数据包总体'
      )
    )}<br>`
    +
    `${escapeHtml(detailText)}`;

  /*
   * 核心修改：
   *
   * 原来 group 一律不能点摘要。
   * 现在只要 group 有 node_id，
   * 就允许进行模型摘要。
   */
  $('summaryBtn').disabled =
    unsupportedVirtual
    ||
    state.modelGenerationEnabled
      === false;

  $('exportBtn').disabled =
    unsupportedVirtual
    && !state.selectedNodes.size;

  $('deepenBtn').disabled =
    unsupportedVirtual
    || !state.analysis
    || !(
      state.analysis?.coverage?.pending_files
      || node.coverage?.pending_files
      || node.kind === 'directory'
    );

  $('retrievalBtn').disabled =
    !state.scan;
  if ($('numericQuestionBtn')) {
    $('numericQuestionBtn').disabled = !state.scan;
  }

  const local =
    localSummaryFor(node);

  if (local) {
    renderSummary(
      local,
      isVirtualGroup
        ? '主题节点摘要'
        : '本地节点简易摘要'
    );
  } else if (node.kind === 'evidence') {
    renderSummary(
      {
        title: '可回查原文证据',
        summary: node.summary,
        evidence_chain: [node.evidence].filter(Boolean),
        representative_documents: [node.source_path].filter(Boolean)
      },
      '证据节点'
    );
  } else if (
    node.kind === 'file'
    && state.scan
  ) {
    $('summary').className =
      'summary';

    $('summary').textContent =
      '正在读取 Docling 统一解析结果…';

    try {
      const data =
        await api(
          `/api/document/${state.scan.scan_id}?path=${encodeURIComponent(node.path)}`
        );

      renderDocument(
        data.document
      );

    } catch (e) {
      $('summary').className =
        'summary empty';

      $('summary').textContent =
        '该文件尚未完成统一解析，可等待完整分析结束后重试。';
    }

  } else {
    $('summary').className =
      'summary empty';

    $('summary').textContent =
      '该节点尚无本地摘要。';
  }
}


function evidenceHtml(items) {
  if (
    !Array.isArray(items)
    || !items.length
  ) {
    return (
      '<p class="muted">'
      +
      '暂无可引用正文证据；相关结论应人工复核。'
      +
      '</p>'
    );
  }

  return (
    `<div class="evidence-list">${
      items.map(
        item => {
          const loc = [
            item.source_path,

            item.page
              ? `第 ${item.page} 页`
              : '',

            item.section || ''
          ]
            .filter(Boolean)
            .join(' · ');

          const relevance =
            item.retrieval_score
              != null

              ? (
                  `<small>检索相关度：${
                    Math.round(
                      item.retrieval_score
                      * 1000
                    ) / 10
                  }%</small><br>`
                )

              : '';

          const quote =
            item.supporting_quote
            && item.supporting_quote !== item.text
              ? (`<p><strong>支撑原句：</strong>${escapeHtml(item.supporting_quote)}</p>`)
              : '';

          const supportReason =
            item.support_reason
            || item.evidence_quality?.reason;

          const supportType =
            item.support_type
            || '';

          return (
            `<article class="evidence-card">`
            +
            `<div class="evidence-id">${
              escapeHtml(
                item.evidence_id
                || '元数据证据'
              )
            }</div>`
            +
            `<strong>${
              escapeHtml(
                loc
                || '未知位置'
              )
            }</strong>`
            +
            `<p>${
              escapeHtml(
                item.text
                || item.fact
                || ''
              )
            }</p>`
            +
            quote
            +
            (
              supportReason
                ? `<small>入选原因：${escapeHtml(supportReason)}</small><br>`
                : ''
            )
            +
            (
              supportType
                ? `<small>证据类型：${escapeHtml(supportType)}</small><br>`
                : ''
            )
            +
            relevance
            +
            (
              item.source_sha256

                ? (
                    `<small>源 SHA-256：${
                      escapeHtml(
                        item.source_sha256.slice(
                          0,
                          20
                        )
                      )
                    }…</small>`
                  )

                : ''
            )
            +
            `</article>`
          );
        }
      ).join('')
    }</div>`
  );
}


function renderDocument(doc) {
  const structure =
    doc.structure || {};

  const parser =
    doc.parser || {};

  const resultLabel =
    parser.mode === 'fast'
      ? '快速解析结果'
      : 'Docling 统一文档结果';

  let html =
    `<div class="summary-kicker">${resultLabel}</div>`
    +
    `<h2>${
      escapeHtml(
        structure.title
        || doc.source?.name
        || '文档'
      )
    }</h2>`;

  html +=
    `<div class="metric-grid">`
    +
    `<div><b>${escapeHtml(parser.name || '未知')}</b><span>解析器</span></div>`
    +
    `<div><b>${structure.page_count ?? '—'}</b><span>页/张</span></div>`
    +
    `<div><b>${structure.table_count || 0}</b><span>表格</span></div>`
    +
    `<div><b>${doc.evidence_count || 0}</b><span>证据项</span></div>`
    +
    `</div>`;

  if (doc.coverage) {
    const ratio =
      doc.coverage.coverage_ratio
        == null

        ? '未知'

        : `${
            Math.round(
              doc.coverage.coverage_ratio
              * 10000
            ) / 100
          }%`;

    const reason =
      doc.coverage
        .coverage_ratio_reason

        ? `；${doc.coverage.coverage_ratio_reason}`

        : '';

    html +=
      `<p><strong>正文覆盖：</strong>`
      +
      `${
        doc.coverage.complete
          ? '完整'
          : '存在截断'
      }；`
      +
      `已保存 ${
        doc.coverage.stored_characters
        || 0
      } 字符；`
      +
      `覆盖率 ${ratio}${reason}`
      +
      `${
        doc.coverage
          .embedded_ocr_characters

          ? (
              `；内嵌图片 OCR ${
                doc.coverage
                  .embedded_ocr_characters
              } 字符`
            )

          : ''
      }`
      +
      `</p>`;
  }

  const profile = doc.data_profile || (doc.data_profiles && doc.data_profiles[0]?.profile);
  if (profile && profile.status !== "skipped" && profile.status !== "failed") {
    const judgment = profile.value_judgment || {};
    const profileCoverage = profile.coverage || profile.limits || {};
    const partialNote = profile.status === "partial" || profileCoverage.complete === false
      ? `；有界采样${profileCoverage.truncation_reasons?.length ? `（${escapeHtml(profileCoverage.truncation_reasons.join('、'))}）` : ''}，统计结果需回原表复核`
      : '';
    html += `<div class="coverage-card"><strong>结构化数据画像：</strong>${profile.row_count ?? 0} 行 / ${profile.column_count ?? 0} 列；质量评分 ${profile.quality_score ?? "—"} / 100；价值判断 ${escapeHtml(judgment.value_level || "—")}。` +
      `${profile.duplicate_row_count ? `重复行 ${profile.duplicate_row_count}；` : ""}${profile.missing_columns?.length ? `缺失字段 ${profile.missing_columns.length} 个；` : ""}${profile.sensitive_columns?.length ? `敏感字段 ${profile.sensitive_columns.length} 个，建议脱敏。` : ""}</div>`;
    if (partialNote) html += `<p class="coverage-card"><strong>画像覆盖提示：</strong>${partialNote}</p>`;
  }

  if (
    structure.headings?.length
  ) {
    html +=
      `<h3>结构目录</h3><ul>${
        structure.headings
          .slice(
            0,
            30
          )
          .map(
            x =>
              `<li>${escapeHtml(x)}</li>`
          )
          .join('')
      }</ul>`;
  }

  if (doc.text_preview) {
    html +=
      `<h3>正文预览</h3>`
      +
      `<pre>${
        escapeHtml(
          doc.text_preview
        )
      }</pre>`;
  }

  if (
    doc.warnings?.length
  ) {
    html +=
      `<h3>解析告警</h3><ul>${
        doc.warnings
          .map(
            x =>
              `<li>${escapeHtml(x)}</li>`
          )
          .join('')
      }</ul>`;
  }

  html +=
    `<h3>证据链（主题相关代表片段，最多 12 条）</h3>`
    +
    evidenceHtml(
      doc.evidence
    );

  $('summary').className =
    'summary';

  $('summary').innerHTML =
    html;
}


function renderSummary(
  data,
  kicker = '分析结果'
) {
  const host =
    $('summary');

  host.className =
    'summary';

  let html =
    `<div class="summary-kicker">${
      escapeHtml(kicker)
    }</div>`
    +
    `<h2>${
      escapeHtml(
        data.title
        || '分析结果'
      )
    }</h2>`;

  const summary =
    data.summary
    || data.core_summary;

  if (summary) {
    html +=
      `<p>${
        escapeHtml(summary)
      }</p>`;
  }

  const scopeCoverage =
    data.coverage
    || data.parser_info?.coverage;
  if (scopeCoverage?.inventory_files != null) {
    const ratio = scopeCoverage.parsed_file_ratio == null
      ? '—'
      : `${Math.round(scopeCoverage.parsed_file_ratio * 10000) / 100}%`;
    html += `<p class="coverage-card"><strong>该节点分析覆盖：</strong>${escapeHtml(scopeCoverage.status || scopeCoverage.mode || '—')}；已解析 ${scopeCoverage.parsed_files ?? 0}/${scopeCoverage.inventory_files ?? scopeCoverage.total_files ?? 0}（${ratio}），抽样 ${scopeCoverage.sampled_files ?? scopeCoverage.sampled_overview_files ?? 0}，深度分析 ${scopeCoverage.deep_analyzed_files ?? 0}，待处理 ${scopeCoverage.pending_files || 0}，失败 ${scopeCoverage.failed_files || 0}；${scopeCoverage.complete_analysis ? '可视为完整分析' : '当前为部分覆盖，不能视为全文分析'}</p>`;
    if (scopeCoverage.limitations?.length) {
      html += `<p class="coverage-card"><strong>覆盖限制：</strong>${escapeHtml(scopeCoverage.limitations.join('；'))}</p>`;
    }
  }

  if (data.statistics) {
    html +=
      `<div class="metric-grid">`
      +
      `<div><b>${
        data.statistics
          .file_count
        ?? '—'
      }</b><span>文件</span></div>`
      +
      `<div><b>${
        data.statistics
          .page_count
        ?? '—'
      }</b><span>页/张</span></div>`
      +
      `<div><b>${
        data.statistics
          .table_count
        ?? 0
      }</b><span>表格</span></div>`
      +
      `<div><b>${
        data.statistics
          .degraded_document_count
        ?? 0
      }</b><span>降级项</span></div>`
      +
      `</div>`;
  }

  if (
    data.file_count != null
    && !data.statistics
  ) {
    html +=
      `<div class="metric-grid">`
      +
      `<div><b>${data.file_count}</b><span>节点文件</span></div>`
      +
      `</div>`;
  }

  if (
    data.topics?.length
  ) {
    html +=
      `<h3>内容主题</h3><div>${
        data.topics
          .map(
            x =>
              `<span class="tag">${escapeHtml(x)}</span>`
          )
          .join('')
      }</div>`;
  }

  for (
    const [key, label]
    of [
      ['key_facts', '关键事实'],
      ['arguments', '主要论点'],
      ['methodology', '研究方法'],
      ['conclusions', '结论'],
      ['notable_items', '值得注意'],
      ['uncertainties', '不确定信息'],
      ['limitations', '局限'],
      ['warnings', '告警']
    ]
  ) {
    if (
      Array.isArray(data[key])
      && data[key].length
    ) {
      html +=
        `<h3>${label}</h3><ul>${
          data[key]
            .map(
              x =>
                `<li>${escapeHtml(
                  typeof x === 'string'
                    ? x
                    : JSON.stringify(x)
                )}</li>`
            )
            .join('')
        }</ul>`;
    }
  }

  if (
    data.structure_overview
  ) {
    html +=
      `<h3>结构概览</h3>`
      +
      `<pre>${
        escapeHtml(
          JSON.stringify(
            data.structure_overview,
            null,
            2
          )
        )
      }</pre>`;
  }

  if (
    data.representative_documents
      ?.length
  ) {
    html +=
      `<h3>代表文档</h3><ul>${
        data.representative_documents
          .map(
            x =>
              `<li>${escapeHtml(x)}</li>`
          )
          .join('')
      }</ul>`;
  }

  if (
    data.member_paths?.length
  ) {
    html +=
      `<details>`
      +
      `<summary>查看该主题包含的 ${
        data.member_paths.length
      } 个文件</summary>`
      +
      `<ul>${
        data.member_paths
          .map(
            x =>
              `<li>${escapeHtml(x)}</li>`
          )
          .join('')
      }</ul>`
      +
      `</details>`;
  }

  if (
    data.recommended_research_direction
  ) {
    const d =
      data.recommended_research_direction;

    const questions =
      d.research_questions
      || d.questions;

    html +=
      `<h3>推荐研究方向 <span class="inference-badge">推论</span></h3>`
      +
      `<p><strong>${
        escapeHtml(
          d.title
          || '待确定'
        )
      }</strong></p>`
      +
      `<p>${
        escapeHtml(
          d.rationale
          || ''
        )
      }</p>`;

    html += `<p class="coverage-card"><strong>优先级：</strong>${escapeHtml(d.priority || '—')}；<strong>评分：</strong>${escapeHtml(d.score ?? '—')}；<strong>证据状态：</strong>${escapeHtml(d.evidence_status || (d.evidence_chain?.length ? 'supported' : 'insufficient'))}</p>`;

    if (
      questions?.length
    ) {
      html +=
        `<ol>${
          questions
            .map(
              x =>
                `<li>${escapeHtml(x)}</li>`
            )
            .join('')
        }</ol>`;
    }
    if (d.methods?.length) {
      html += `<p><strong>建议方法：</strong>${escapeHtml(d.methods.join('；'))}</p>`;
    }
    html += d.evidence_chain?.length
      ? evidenceHtml(d.evidence_chain)
      : `<p class="coverage-card"><strong>证据状态：</strong>当前没有达到质量门槛的正文证据，建议先补充解析后再下结论。</p>`;
  }

  const qa = data.question_answer_evidence;
  if (qa) {
    html += `<h3>问题—回答—证据</h3><section class="conclusion-evidence">` +
      `<p><strong>问题：</strong>${escapeHtml(qa.question || data.question || '—')}</p>` +
      `<p><strong>价值：</strong>${escapeHtml(qa.value || data.value || '—')}</p>` +
      `<p><strong>回答：</strong>${escapeHtml(qa.answer || data.answer || '—')}</p>` +
      `<p><strong>证据状态：</strong>${escapeHtml(data.evidence_status || (qa.evidence?.length ? 'supported' : 'insufficient'))}</p>` +
      (qa.claims?.length ? `<ul>${qa.claims.map(claim => `<li>${escapeHtml(claim.statement || '')}（${escapeHtml(claim.support_status || 'insufficient')}）${claim.evidence_ids?.length ? ` · ${escapeHtml(claim.evidence_ids.join(', '))}` : ''}</li>`).join('')}</ul>` : '') +
      (qa.evidence?.length ? evidenceHtml(qa.evidence) : `<p class="coverage-card">没有有效正文证据支撑当前回答。</p>`) +
      `</section>`;
  }

  if (
    data.conclusion_evidence?.length
  ) {
    html +=
      `<h3>问题—回答—证据链</h3>`
      +
      data.conclusion_evidence
        .map(
          item =>
            `<section class="conclusion-evidence">`
            +
            `<p><strong>问题：</strong>${escapeHtml(item.analysis_question || '该范围内有哪些可回查的关键判断？')}</p>`
            +
            `<p><strong>价值：</strong>${escapeHtml(item.question_value || '帮助判断该资料范围是否值得继续分析，并明确后续核查重点。')}</p>`
            +
            `<p><strong>回答：</strong>${escapeHtml(item.answer || item.statement || '暂无可回查回答')} <span class="inference-badge">${escapeHtml(item.confidence || '待核验')}</span></p>`
            +
            `<p>${escapeHtml(item.basis || '该结论由下列证据支撑。')}</p>`
            +
            evidenceHtml(item.evidence || [])
            +
            `</section>`
        )
        .join('');
  }

  const evidence =
    data.evidence_chain
    || data.evidence;

  if (
    evidence?.length
  ) {
    html +=
      `<h3>证据链</h3>`
      +
      evidenceHtml(
        evidence
      );
  }

  if (
    data.parser_info
  ) {
    html +=
      `<details>`
      +
      `<summary>处理信息</summary>`
      +
      `<pre>${
        escapeHtml(
          JSON.stringify(
            data.parser_info,
            null,
            2
          )
        )
      }</pre>`
      +
      `</details>`;
  }

  host.innerHTML =
    html;
}


function updateStats() {
  if (!state.scan) {
    return;
  }

  const a =
    state.analysis
      ?.statistics
    ||
    state.scan.analysis
    ||
    {};
  const coverage =
    state.analysis?.coverage
    || {};
  const ratio =
    coverage.parsed_file_ratio == null
      ? '—'
      : `${Math.round(coverage.parsed_file_ratio * 10000) / 100}%`;

  $('scanStats')
    .classList
    .remove(
      'empty'
    );

  $('scanStats').innerHTML =
    `<div class="metric-grid">`
    +
    `<div><b>${state.scan.file_count}</b><span>递归文件</span></div>`
    +
    `<div><b>${state.scan.directory_count || 0}</b><span>子目录</span></div>`
    +
    `<div><b>${escapeHtml(state.scan.total_size_human)}</b><span>总大小</span></div>`
    +
    `<div><b>${a.evidence_items ?? '—'}</b><span>证据项</span></div>`
    +
    `<div><b>${a.structured_profiled_files ?? '—'}</b><span>结构化画像</span></div>`
    +
    `</div>`
    +
    `<p>`
    +
    `精确重复组：${a.exact_duplicate_groups ?? '分析中'}；`
    +
    `可合并重复文件：${a.exact_duplicate_files ?? '—'}；`
    +
    `相似文档簇：${a.similar_document_clusters ?? '—'}；`
    +
    `语义主题：${a.semantic_topic_clusters ?? a.topic_clusters ?? '—'}`
    +
    `</p>`;
  const overview = state.analysis?.overview || {};
  const judgment = state.analysis?.value_judgment || {};
  if (overview.file_count != null || judgment.level) {
    $('scanStats').innerHTML +=
      `<div class="coverage-card"><strong>数据概览：</strong>已解析 ${overview.parsed_files ?? a.parsed_files ?? 0} 个文件，证据 ${overview.evidence_count ?? a.evidence_items ?? 0} 条；` +
      `价值判断：${escapeHtml(judgment.level || '待分析')}（${escapeHtml(judgment.confidence || '—')}）` +
      `${judgment.limitations?.length ? `<br><small>${escapeHtml(judgment.limitations.join('；'))}</small>` : ''}</div>`;
  }
  if (coverage.inventory_files != null) {
    $('scanStats').innerHTML +=
      `<div class="coverage-card"><strong>分析覆盖：${escapeHtml(coverage.status || '—')}</strong> · 已分析 ${coverage.parsed_files || 0}/${coverage.inventory_files || 0}（${ratio}）` +
      `；抽样 ${coverage.sampled_files ?? coverage.sampled_overview_files ?? 0}；深度分析 ${coverage.deep_analyzed_files ?? 0}` +
      `；待处理 ${coverage.pending_files || 0}；失败 ${coverage.failed_files || 0}` +
      `；${coverage.complete_analysis ? '完整分析' : '部分覆盖'}` +
      `${coverage.large_package_notice ? `<br><small>${escapeHtml(coverage.large_package_notice)}</small>` : ''}</div>`;
    if (coverage.limitations?.length) {
      $('scanStats').innerHTML += `<div class="coverage-card"><strong>覆盖限制：</strong>${escapeHtml(coverage.limitations.join('；'))}</div>`;
    }
  }
  if (judgment.dimensions) {
    const labels = { readability: '可读性', completeness: '完整性', uniqueness: '独特性', topic_concentration: '主题集中度', evidence_density: '证据密度', structured_quality: '结构化质量' };
    $('scanStats').innerHTML += `<div class="coverage-card"><strong>价值维度：</strong>${Object.entries(judgment.dimensions).map(([key, value]) => `${escapeHtml(labels[key] || key)} ${escapeHtml(value?.score ?? '—')}`).join(' · ')}</div>`;
  }
}


async function refreshScan(scanId = state.scan?.scan_id) {
  if (!scanId) {
    return;
  }
  const data =
    await api(
      `/api/scan/${scanId}`
    );

  state.scan =
    data.scan;

  state.analysis =
    data.analysis;

  if (
    state.scan.parse_mode
  ) {
    $('parseMode').value =
      state.scan.parse_mode;
  }

  state.summaries =
    new Map(
      (data.summaries || [])
        .map(
          item => [
            summaryKey(
              item.path,
              item.type
            ),
            item.payload
          ]
        )
    );

  renderTree(
    state.activeTree === 'analysis'
    && state.analysis?.analysis_tree

      ? state.analysis.analysis_tree

      : state.scan.tree
  );

  updateStats();

  $('analysisTreeBtn').disabled =
    !state.analysis?.analysis_tree;
  // The physical inventory remains available after semantic analysis.
  $('physicalTreeBtn').disabled = false;

  $('reportBtn').disabled =
    false;

  $('reanalyzeBtn').disabled =
    false;
  $('retryBtn').disabled = !(state.analysis?.statistics?.failed_files > 0);

  $('retrievalBtn').disabled =
    !state.analysis;
  if ($('numericQuestionBtn')) {
    $('numericQuestionBtn').disabled = !state.analysis;
  }

  updateSelectionCart();
}


function updateJobControls(job = {}) {
  const status = String(job.status || '').toLowerCase();
  const active = ['queued', 'running', 'cancelling'].includes(status) && Boolean(state.jobId);
  const cancelling = status === 'cancelling';
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const stage = job.current_stage || job.stage || '';
  const currentFile = job.current_file || '';
  const detail = currentFile && currentFile !== stage
    ? `${stage} · ${currentFile}`
    : (stage || job.message || (active ? '任务运行中' : '当前没有运行中的任务'));
  ['cancelJobBtn', 'taskCenterCancelBtn'].forEach((id) => {
    const button = $(id);
    if (!button) return;
    button.disabled = !active || cancelling;
    button.textContent = cancelling ? '正在取消…' : '取消当前任务';
  });
  if ($('jobStatusChip')) {
    $('jobStatusChip').textContent = cancelling ? 'CANCELLING' : (active ? 'RUNNING' : (status || 'IDLE').toUpperCase());
  }
  if ($('taskCenterProgressBar')) $('taskCenterProgressBar').style.width = `${progress}%`;
  if ($('taskCenterProgressText')) $('taskCenterProgressText').textContent = `${progress}% · ${detail}`;
}


async function cancelCurrentJob() {
  const jobId = state.jobId;
  if (!jobId) {
    toast('当前没有可以取消的任务。');
    return;
  }
  updateJobControls({ status: 'cancelling', progress: Number(($('progressBar')?.style.width || '0').replace('%', '')), message: '正在请求取消任务' });
  try {
    const data = await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
    updateJobControls(data.job || { status: 'cancelling', message: '已发送取消请求' });
    toast('已发送取消请求，Worker 正在安全停止当前步骤。');
  } catch (error) {
    updateJobControls({ status: 'running', message: '取消请求失败，任务仍在运行' });
    toast(error.message || '取消任务失败', true);
  }
}


document.addEventListener('click', (event) => {
  const button = event.target.closest('#cancelJobBtn, #taskCenterCancelBtn');
  if (!button) return;
  event.preventDefault();
  cancelCurrentJob();
});


async function pollJob(jobId) {
  state.jobId =
    jobId;

  updateJobControls({ status: 'queued', progress: 0, message: '任务已提交，等待本地 Worker' });

  $('pipeline')
    .classList
    .remove(
      'empty'
    );

  while (
    state.jobId === jobId
  ) {
    const data =
      await api(
        `/api/jobs/${jobId}`
      );

    const job =
      data.job;

    updateJobControls(job);

    $('progressBar').style.width =
      `${job.progress || 0}%`;

    const currentStage = job.current_stage || job.stage || '';
    const currentFile = job.current_file || '';
    const activity = currentFile && currentFile !== currentStage
      ? `${currentStage} · ${currentFile}`
      : (currentStage || job.message || job.status);
    $('progressText').textContent =
      `${job.progress || 0}% · ${activity}`;

    // A scan-and-analyze job publishes its inventory before parsing begins.
    // Load and show the physical tree immediately instead of making the user
    // wait for semantic clustering and report generation.
    const partialScanId = job.result?.scan_available
      ? (job.result.scan_id || job.scan_id)
      : null;
    if (partialScanId && !state.scan?.tree) {
      try {
        const partial = await api(`/api/scan/${partialScanId}`);
        state.scan = partial.scan;
        state.analysis = partial.analysis;
        state.summaries = new Map((partial.summaries || []).map(item => [summaryKey(item.path, item.type), item.payload]));
        state.activeTree = 'physical';
        $('physicalTreeBtn').disabled = false;
        $('physicalTreeBtn').classList.add('active');
        $('analysisTreeBtn').classList.remove('active');
        renderTree(state.scan.tree);
        updateStats();
        $('tree').classList.remove('empty');
        toast('原始目录已加载，后台继续进行深度分析。');
      } catch (partialError) {
        // The main job status remains authoritative; a transient fetch error
        // should not abort polling.
      }
    }

    if (
      job.status ===
      'completed'
    ) {
      if (job.task_type === 'export_package') {
        const result = job.result || {};
        $('reportResult').className = 'report-result';
        $('reportResult').innerHTML =
          `<strong>待整编数据包已生成</strong>`
          + `<p>已合并 ${escapeHtml(result.selection_count || 0)} 个选择，去重后包含 ${escapeHtml(result.source_file_count || 0)} 个源文件。</p>`
          + (result.download_url
            ? `<p>${downloadLink(result.download_url, '下载待整编数据包')}</p>`
            : '');
        toast('待整编数据包已生成，可下载。');
        state.jobId = null;
        updateJobControls({ ...job, status: 'completed', progress: 100 });
        return;
      }

      if (job.task_type === 'generate_summary') {
        const result = job.result || {};
        if (result.summary) {
          state.summary = result.summary;
          renderSummary(
            result.summary,
            result.node_id ? '主题节点深度摘要' : '模型深度摘要'
          );
        }
        toast(
          result.degraded
            ? '深度摘要已完成，但部分内容使用了本地保底结果'
            : '深度摘要生成完成'
        );
        state.jobId = null;
        updateJobControls({ ...job, status: 'completed', progress: 100 });
        return;
      }

      const completedScanId =
        job.result?.scan_id
        || state.scan?.scan_id
        || job.scan_id;

      if (completedScanId) {
        state.scan = { scan_id: completedScanId };
        await refreshScan(completedScanId);
      }

      // Keep the original physical directory as the default view. Users can
      // switch to the semantic topic tree explicitly after analysis completes.
      // This preserves the source structure and avoids making it appear lost.
      $('physicalTreeBtn').disabled = false;
      if (state.activeTree !== 'analysis' || !state.analysis?.analysis_tree) {
        state.activeTree = 'physical';
        $('physicalTreeBtn').classList.add('active');
        $('analysisTreeBtn').classList.remove('active');
        renderTree(state.scan?.tree || state.scan);
      }

      const overview =
        job.result?.overview;

      const d =
        overview
          ?.report
          ?.recommended_research_direction
        || {};

      $('reportResult').className =
        'report-result';

      $('reportResult').innerHTML =
        `<strong>${
          job.task_type === 'generate_report'
            ? '概览 Word 已重新生成'
            : '自动概览已生成'
        }</strong>`
        +
        `<p><span class="inference-badge">推论</span> ${
          escapeHtml(
            d.title
            || '待进一步确定研究方向'
          )
        }</p>`
        +
        `<p>${
          escapeHtml(
            d.rationale
            || ''
          )
        }</p>`
        +
        `${
          overview?.download_url

            ? (
                downloadLink(overview.download_url, '下载自动生成的情况概览 Word')
              )

            : ''
        }`;

      toast(
        job.task_type === 'generate_report'
          ? '概览 Word 已生成'
          : '完整分析、证据链和概览 Word 已生成'
      );

      state.jobId =
        null;

      updateJobControls({ ...job, status: 'completed', progress: 100 });

      return;
    }

    if (job.status === 'cancelled') {
      state.jobId =
        null;

      updateJobControls(job);
      toast(job.message || '任务已取消，已完成的文件检查点已保留。');
      return;
    }

    if (job.status === 'failed') {
      state.jobId =
        null;

      updateJobControls(job);

      throw new Error(
        job.message
        || job.error
        || '完整分析失败'
      );
    }

    await new Promise(
      resolve =>
        setTimeout(
          resolve,
          1000
        )
    );
  }
}


$('scanBtn').onclick =
  async () => {
    if (state.jobId) {
      toast(
        '当前分析任务尚未完成，请勿重复提交。',
        true
      );

      return;
    }

    const btn =
      $('scanBtn');

    setBusy(
      btn,
      true,
      '正在导入…'
    );

    $('pipeline')
      .classList
      .remove(
        'empty'
      );

    $('progressBar')
      .style
      .width =
        '3%';

    $('progressText')
      .textContent =
        '正在遍历服务器目录；大数据包扫描阶段可能需要一段时间…';

    // A new scan starts a new UI session; clear selections from the prior package.
    state.scan = null;
    state.analysis = null;
    state.summary = null;
    state.summaries = new Map();
    state.selected = null;
    state.selectedNodes = new Map();
    state.activeTree = 'physical';
    $('tree').className = 'tree';
    renderInitialPhysicalTree($('rootPath').value);
    $('analysisTreeBtn').classList.remove('active');
    $('physicalTreeBtn').classList.add('active');
    $('analysisTreeBtn').disabled = true;
    $('reportBtn').disabled = true;
    $('reanalyzeBtn').disabled = true;
    $('retrievalBtn').disabled = true;
    if ($('numericQuestionBtn')) $('numericQuestionBtn').disabled = true;
    updateSelectionCart();

    try {
      const data =
        await api(
          '/api/scan',
          {
            method: 'POST',

            body: JSON.stringify({
              path:
                $('rootPath').value,

              max_files:
                50000,

              parse_mode:
                $('parseMode').value
            })
          }
        );

      await pollJob(
        data.job_id
        || data.analysis_job_id
      );

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


$('reanalyzeBtn').onclick =
  async () => {
    if (state.jobId) {
      toast(
        '当前分析任务尚未完成，请勿重复提交。',
        true
      );

      return;
    }

    if (!state.scan) {
      return;
    }

    const btn =
      $('reanalyzeBtn');

    setBusy(
      btn,
      true,
      '正在启动…'
    );

    try {
      const data =
        await api(
          '/api/analyze-package',
          {
            method: 'POST',

            body: JSON.stringify({
              scan_id:
                state.scan.scan_id,

              parse_mode:
                $('parseMode').value
            })
          }
        );

      await pollJob(
        data.job_id
      );

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


$('physicalTreeBtn').onclick =
  () => {
    state.activeTree =
      'physical';

    $('physicalTreeBtn')
      .classList
      .add(
        'active'
      );

    $('analysisTreeBtn')
      .classList
      .remove(
        'active'
      );

    renderTree(
      state.scan.tree
    );
  };


$('analysisTreeBtn').onclick =
  () => {
    if (
      !state.analysis
        ?.analysis_tree
    ) {
      return;
    }

    state.activeTree =
      'analysis';

    $('analysisTreeBtn')
      .classList
      .add(
        'active'
      );

    $('physicalTreeBtn')
      .classList
      .remove(
        'active'
      );

    renderTree(
      state.analysis
        .analysis_tree
    );
  };


$('testBtn').onclick =
  async () => {
    const btn =
      $('testBtn');

    setBusy(
      btn,
      true,
      '连接中…'
    );

    try {
      const data =
        await api(
          '/api/test-model',
          {
            method: 'POST',

            body: JSON.stringify({})
          }
        );

      toast(
        `${data.reply}（${data.model}）`
      );

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


/*
 * ============================================================
 * 深度摘要
 *
 * 核心修改：
 * group 节点如果有 node_id，
 * 就把 node_id 一起发给后端。
 * ============================================================
 */
$('summaryBtn').onclick =
  async () => {
    if (
      !state.scan
      || !state.selected
    ) {
      return;
    }

    const btn =
      $('summaryBtn');

    setBusy(
      btn,
      true,
      '模型深度分析中…'
    );

    $('summary')
      .textContent =
        '本地模型正在对当前节点进行深度分析，可能需要数分钟。请保持页面打开。';

    try {
      const payload = {
        scan_id:
          state.scan.scan_id,

        path:
          state.selected.path
          || '.',

        kind:
          state.selected.kind
      };

      /*
       * 主题节点：
       * 把 node_id 发给 app.py
       */
      if (
        state.selected.kind
          === 'group'
        &&
        state.selected.node_id
      ) {
        payload.node_id =
          state.selected.node_id;
      }

      const data =
        await api(
          '/api/summary',
          {
            method: 'POST',

            body:
              JSON.stringify(
                payload
              )
          }
        );

      if (data.accepted && data.job_id) {
        toast('已提交深度摘要任务，Worker 正在处理当前节点。');
        await pollJob(data.job_id);
        return;
      }

      state.summary =
        data.summary;

      /*
       * 虚拟主题摘要加入前端缓存。
       */
      if (
        state.selected.kind
          === 'group'
        &&
        state.selected.node_id
      ) {
        state.summaries.set(
          summaryKey(
            `node:${state.selected.node_id}`,
            'folder'
          ),
          data.summary
        );
      }

      renderSummary(
        data.summary,

        state.selected.kind
          === 'group'

          ? '主题节点深度摘要'

          : '模型深度摘要'
      );

      toast(
        data.degraded

          ? '部分步骤降级，已返回可用摘要'

          : (
              data.cached

                ? '已读取缓存摘要'

                : '深度摘要生成完成'
            ),

        data.degraded
      );

    } catch (e) {
      $('summary')
        .textContent =
          e.message;

      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


$('retryBtn').onclick = async () => {
  if (!state.scan) return;
  const btn = $('retryBtn');
  setBusy(btn, true, '正在重试…');
  try {
    const data = await api(`/api/retry-failed/${state.scan.scan_id}`, { method: 'POST', body: JSON.stringify({}) });
    if (data.job_id) await pollJob(data.job_id);
    else toast(data.message || '当前没有失败文件');
  } catch (e) { toast(e.message, true); }
  finally { setBusy(btn, false); }
};


$('reportBtn').onclick =
  async () => {
    if (!state.scan) {
      return;
    }

    const btn =
      $('reportBtn');

    setBusy(
      btn,
      true,
      '概览生成中…'
    );

    try {
      const data =
        await api(
          '/api/report',
          {
            method: 'POST',

            body: JSON.stringify({
              scan_id:
                state.scan.scan_id
            })
          }
        );

      await pollJob(data.job_id);

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


$('deepenBtn').onclick =
  async () => {
    if (!state.scan || !state.selected) {
      return;
    }
    const btn = $('deepenBtn');
    setBusy(btn, true, '正在创建补充任务…');
    try {
      const payload = {
        scan_id: state.scan.scan_id,
        path: state.selected.path || '.',
        node_id: state.selected.node_id || null
      };
      const data = await api('/api/analyze-scope', {
        method: 'POST', body: JSON.stringify(payload)
      });
      toast(`已开始补充分析“${data.scope_label}”，本批最多 ${data.batch_limit} 个文件。`);
      await pollJob(data.job_id);
    } catch (e) {
      toast(e.message, true);
    } finally {
      setBusy(btn, false);
    }
  };


$('exportBtn').onclick =
  async () => {
    if (!state.scan || (!state.selected && !state.selectedNodes.size)) {
      return;
    }

    const btn =
      $('exportBtn');

    setBusy(
      btn,
      true,
      '正在打包…'
    );

    const taskTopic =
      window.prompt(
        '请输入整编任务主题（必填）',
        ''
      );

    if (
      !taskTopic
      || !taskTopic.trim()
    ) {
      setBusy(
        btn,
        false
      );

      toast(
        '未指定整编任务主题，已取消导出',
        true
      );

      return;
    }

    try {
      const data =
        await api(
          '/api/export',
          {
            method: 'POST',

            body: JSON.stringify({
              scan_id:
                state.scan.scan_id,
              selections: state.selectedNodes.size
                ? [...state.selectedNodes.values()]
                : [exportPayloadNode(state.selected)],
              task_topic:
                taskTopic.trim()
            })
          }
        );

      toast('已提交待整编任务，Worker 将生成去重资料包和统一交接说明。');
      await pollJob(data.job_id);

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


/*
 * ============================================================
 * 本地 RAG
 *
 * 核心修改：
 * 如果当前是主题节点，
 * 把 node_id 一起传给后端。
 * 后端会根据 member_paths 只检索该主题文件。
 * ============================================================
 */
$('retrievalBtn').onclick =
  async () => {
    if (!state.scan) {
      return;
    }

    const query =
      $('retrievalQuery')
        .value
        .trim();

    if (!query) {
      toast(
        '请输入要检索的问题',
        true
      );

      return;
    }

    const btn =
      $('retrievalBtn');

    setBusy(
      btn,
      true,
      '本地检索中…'
    );

    try {
      const payload = {
        scan_id:
          state.scan.scan_id,

        query:
          query,

        path:
          state.selected
            ?.path
          || '.',

        top_k:
          12,

        previous_result_id:
          state.lastRetrievalId
      };

      /*
       * 当前选中的是主题节点。
       */
      if (
        state.selected
          ?.kind
          === 'group'
        &&
        state.selected
          ?.node_id
      ) {
        payload.node_id =
          state.selected.node_id;
      }

      const data =
        await api(
          '/api/retrieve',
          {
            method: 'POST',

            body:
              JSON.stringify(
                payload
              )
          }
        );

      const result =
        data.retrieval;

      state.lastRetrievalId =
        result.result_id
        || null;

      $('summary').className =
        'summary';

      const scopeLabel =
        result.node_name
        ||
        (
          state.selected
            ?.kind
            === 'group'

            ? state.selected.name

            : result.scope
        );

      $('summary').innerHTML =
        `<div class="summary-kicker">本地混合检索 RAG</div>`
        +
        `<h2>${
          escapeHtml(
            result.query
          )
        }</h2>`
        +
        `<p>`
        +
        `范围：${
          escapeHtml(
            scopeLabel
            || '整个数据包'
          )
        }；`
        +
        `方法：${
          escapeHtml(
            result.method
          )
        }；`
        +
        `检索语料 ${
          result.corpus_chunks
        } 个证据块。`
        +
        `</p>`
        +
        evidenceHtml(
          result.results
        )
        +
        (
          result.warnings
            ?.length

            ? (
                `<h3>检索说明</h3>`
                +
                `<ul>${
                  result.warnings
                    .map(
                      x =>
                        `<li>${escapeHtml(x)}</li>`
                    )
                    .join('')
                }</ul>`
              )

            : ''
        );

      toast(
        `已返回 ${
          result.result_count
          || 0
        } 条可追溯证据`
      );

    } catch (e) {
      toast(
        e.message,
        true
      );

    } finally {
      setBusy(
        btn,
        false
      );
    }
  };


$('numericQuestionBtn').onclick =
  async () => {
    if (!state.scan) return;
    const question = $('numericQuestion').value.trim();
    if (!question) {
      toast('请输入精确数字问题', true);
      return;
    }
    const btn = $('numericQuestionBtn');
    setBusy(btn, true, '计算中…');
    try {
      const payload = {
        scan_id: state.scan.scan_id,
        question,
        path: state.selected?.path || '.'
      };
      if (state.selected?.node_id) payload.node_id = state.selected.node_id;
      const data = await api('/api/ask', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
      const answer = data.answer || {};
      const answerCoverage = answer.coverage || {};
      $('summary').className = 'summary';
      $('summary').innerHTML =
        `<div class="summary-kicker">可验证精确数字问答</div>` +
        `<h2>${escapeHtml(answer.question || question)}</h2>` +
        `<div class="metric-grid"><div><b>${escapeHtml(answer.value ?? '—')}</b><span>${escapeHtml(answer.operation || '结果')}</span></div>` +
        `<div><b>${escapeHtml(answer.column || '记录数')}</b><span>字段</span></div>` +
        `<div><b>${escapeHtml(answer.confidence || '—')}</b><span>置信度</span></div></div>` +
        `<p>来源：${escapeHtml(answer.source_path || '当前范围')}；表/成员：${escapeHtml(answer.table || '—')}</p>` +
        `${answerCoverage.complete === false ? `<p class="coverage-card"><strong>覆盖提示：</strong>${escapeHtml(answerCoverage.warning || '结果基于有界采样，请回原表复核。')}</p>` : ''}` +
        evidenceHtml(answer.evidence || []);
      toast('已返回带来源定位的精确统计结果');
    } catch (e) {
      toast(e.message, true);
    } finally {
      setBusy(btn, false);
    }
  };


$('parseMode').onchange =
  () => {
    $('parseModeHelp')
      .textContent =
        $('parseMode').value
          === 'fast'

          ? (
              '快速提取正文；扫描型 PDF 仅预览前几页 OCR。'
              +
              '需要完整版面、表格和图片识别时请选择高精度解析。'
            )

          : (
              '使用 Docling 完成版面分析、OCR、TableFormer 表格识别和 '
              +
              'Office 内嵌图片 OCR，耗时会明显增加。'
            );
  };


async function refreshModelStatus() {
  try {
    const data =
      await api(
        '/api/status'
      );

    state.modelGenerationEnabled =
      data.model_generation_enabled
      !== false;

    if (
      !state.modelGenerationEnabled
    ) {
      $('testBtn').textContent =
        '检查本机模型状态';

      $('summaryBtn').title =
        '共享模型未启用，避免影响实验室其他用户';

      if (state.selected) {
        $('summaryBtn').disabled =
          true;
      }
    }

  } catch (_) {
    state.modelGenerationEnabled =
      null;
  }
}


refreshModelStatus();
