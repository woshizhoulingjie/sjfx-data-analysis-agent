/* 导出中心：普通文件导出与大数据包导出共用同一套"智能目录"文件选择器。
 *
 * 设计要点
 *  1. 两个 tab 共用同一个树渲染器、同一套选择模型、同一个提交/下载/记录流程，
 *     差别只在数据来源与提交接口。
 *  2. 树上每一个勾选框（主题 / 子目录 / 单个文件）都可以独立开关。勾选不等于
 *     "只能选整块"：勾选目录后再取消其中的子目录或文件，会作为**排除项**回传，
 *     由服务端在展开成员之后扣减。这样即使目录下的文件清单是分页读取的，
 *     没有加载到的文件也不会被静默打包进去。
 *  3. 导出完成后给出显眼的结果卡（文件名 + 服务器落盘路径 + 大号下载按钮），
 *     并在下方常驻"导出记录"，刷新页面或换设备后仍可重新下载。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const api = (url, options) => window.sjfxApi(url, options);
  const notify = (message, error) => {
    if (typeof window.toast === "function") window.toast(message, Boolean(error));
  };

  const FILE_PAGE = 500;
  const ORDINARY_SELECTION_LIMIT = 200; // 服务端 _combined_export_context 的上限

  const state = {
    loadedSources: false,
    mode: "large",
    sources: [],
    nameById: new Map(),
    sourceId: "",
    roots: [],
    nodeByKey: new Map(),
    nodeById: new Map(),
    loaded: new Set(),   // 已向服务端取过子节点的目录 key
    expanded: new Set(),
    nodes: new Set(),        // 明确勾选的目录 node_id
    excludedNodes: new Set(), // 在"已被上级覆盖"的前提下取消的目录 node_id
    picked: new Set(),       // 在没有任何已勾选上级的情况下单独勾选的文件 path
    excluded: new Set(),     // 在"已被上级覆盖"的前提下取消的文件 path
    poll: null,
    busy: false,
    treeRequest: 0,
  };

  const domIndex = new Map();

  /* ------------------------------------------------------------ 小工具 */
  function humanSize(bytes) {
    const n = Number(bytes || 0);
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    let value = n;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return `${value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
  }

  function baseName(path) {
    const parts = String(path || "").split("/");
    return parts[parts.length - 1] || String(path || "");
  }

  function formatTime(value) {
    if (value == null || value === "") return "";
    const numeric = Number(value);
    const date = Number.isFinite(numeric) && numeric > 0
      ? new Date(numeric > 1e12 ? numeric : numeric * 1000)
      : new Date(String(value));
    if (Number.isNaN(date.getTime())) return String(value);
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
      + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function setText(id, value) {
    const el = $(id);
    if (el) el.textContent = value == null ? "" : String(value);
  }

  function download(url) {
    if (!url) return;
    if (typeof window.authenticatedDownload === "function") {
      window.authenticatedDownload(url).catch((error) => notify(error.message || "下载失败", true));
      return;
    }
    // 兜底：与 app.js 相同的票据流程。
    const filename = decodeURIComponent(String(url).split("?")[0].split("/").pop() || "");
    api("/api/download-ticket", { method: "POST", body: JSON.stringify({ filename }) })
      .then((data) => { window.location.href = data.download_url; })
      .catch((error) => notify(error.message || "下载失败", true));
  }

  /* -------------------------------------------------- 数据来源（两个 tab） */
  async function loadSources() {
    const select = $("exportSourceSelect");
    setText("exportTreeState", "正在读取数据包列表…");
    try {
      const url = state.mode === "large"
        ? "/api/large-packages/history?limit=50"
        : "/api/data-sources?limit=50&offset=0";
      const data = await api(url);
      const items = data.items || [];
      state.sources = items;
      items.forEach((item) => {
        const id = String(item.scan_id || item.large_package_id || item.id || "");
        if (!id) return;
        const label = item.name || baseName(item.root || item.root_path || "") || id;
        state.nameById.set(id, label);
      });
      if (!items.length) {
        state.sourceId = "";
        if (select) select.innerHTML = '<option value="">暂无数据包</option>';
        resetTree(state.mode === "large" ? "还没有大数据包，请先导入。" : "还没有普通数据包，请先导入。");
        return;
      }
      const options = items.map((item) => {
        const id = String(item.scan_id || item.large_package_id || item.id || "");
        const label = item.name || baseName(item.root || item.root_path || "") || id;
        const status = item.phase || item.status || "";
        const size = item.total_size_human ? ` · ${item.total_size_human}` : "";
        const files = item.file_count ? ` · ${Number(item.file_count).toLocaleString()} 文件` : "";
        return `<option value="${esc(id)}">${esc(label)} · ${esc(status)}${esc(size)}${esc(files)}</option>`;
      }).join("");
      if (select) select.innerHTML = options;

      const preferred = String(
        window.sjfxLargeResultId
        || (state.mode === "large" ? window.localStorage.getItem("sjfx_large_package_id") : "")
        || window.localStorage.getItem("sjfx_current_scan_id_v1")
        || ""
      );
      const chosen = items.find((item) => String(item.scan_id || item.large_package_id || item.id) === preferred) || items[0];
      state.sourceId = String(chosen.scan_id || chosen.large_package_id || chosen.id || "");
      if (select) select.value = state.sourceId;
      // 记录列表要用数据包展示名；它与数据源列表是并发拉取的，先到的那次
      // 只能显示原始 id。名称就绪后重绘一次，避免"来源"一栏闪出裸 id。
      loadRecords();
      await loadTree();
    } catch (error) {
      state.sourceId = "";
      resetTree(error.message || "数据包列表读取失败");
    }
  }

  function resetTree(message) {
    state.roots = [];
    state.nodeByKey.clear();
    state.nodeById.clear();
    state.loaded.clear();
    state.expanded.clear();
    clearSelection();
    state.loadError = message || "";
    renderTree();
  }

  function clearSelection() {
    state.nodes.clear();
    state.excludedNodes.clear();
    state.picked.clear();
    state.excluded.clear();
  }

  /* ---------------------------------------------------------------- 建模 */
  function largeNode(raw, kind) {
    return {
      key: String(raw.node_id || raw.path || raw.name || Math.random()),
      kind,
      name: raw.name || raw.topic || raw.path || "未命名",
      nodeId: raw.node_id ? String(raw.node_id) : null,
      path: raw.path ? String(raw.path) : null,
      count: Number(raw.files || raw.file_count || 0),
      sizeHuman: raw.size_human || "",
      hasChildren: kind !== "file",
      children: [],
      hasMore: false,
      ancestors: [],
    };
  }

  function mapOrdinaryNode(raw, ancestors) {
    const isFile = raw.kind === "file";
    const kind = isFile ? "file" : (raw.kind === "analysis_root" ? "root" : "leaf");
    const node = {
      key: raw.node_id ? `n:${raw.node_id}` : (raw._tree_key ? `k:${raw._tree_key}` : `p:${raw.path || raw.name}`),
      kind,
      name: raw.name || raw.path || "未命名",
      nodeId: raw.node_id ? String(raw.node_id) : null,
      path: raw.path ? String(raw.path) : null,
      count: Number(raw.file_count || raw.child_count || 0),
      sizeHuman: raw.size_human || (raw.size ? humanSize(raw.size) : ""),
      loadKey: raw._tree_key || null,
      hasChildren: Boolean(raw.has_children || (Array.isArray(raw.children) && raw.children.length)),
      children: [],
      hasMore: false,
      ancestors: ancestors.slice(),
    };
    const kids = Array.isArray(raw.children) ? raw.children : [];
    node.children = kids.map((child) => mapOrdinaryNode(child, node.ancestors.concat([node])));
    if (kids.length) state.loaded.add(node.key);
    return node;
  }

  function registerNodes(nodes) {
    nodes.forEach((node) => {
      state.nodeByKey.set(node.key, node);
      if (node.nodeId) state.nodeById.set(node.nodeId, node);
      if (node.children.length) registerNodes(node.children);
    });
  }

  async function loadTree() {
    if (!state.sourceId) return;
    const seq = ++state.treeRequest;
    state.loadError = "";
    state.roots = [];
    state.nodeByKey.clear();
    state.nodeById.clear();
    state.loaded.clear();
    state.expanded.clear();
    clearSelection();
    setText("exportTreeState", "正在读取智能目录…");
    renderTree();
    try {
      if (state.mode === "large") {
        const data = await api(`/api/large-packages/${encodeURIComponent(state.sourceId)}/catalog/tree`);
        if (seq !== state.treeRequest) return;
        const tree = data.tree || {};
        state.roots = (tree.themes || []).map((theme) => {
          const node = {
            key: `t:${theme.node_id}`,
            kind: "theme",
            name: theme.name || "未命名主题",
            nodeId: theme.node_id ? String(theme.node_id) : null,
            path: null,
            count: Number(theme.files || 0),
            sizeHuman: "",
            hasChildren: true,
            children: [],
            hasMore: false,
            ancestors: [],
            status: theme.status || "current",
          };
          node.children = (theme.leaves || []).map((leaf) => {
            const child = {
              key: `l:${leaf.node_id}`,
              kind: "leaf",
              name: leaf.name || "未命名目录",
              nodeId: leaf.node_id ? String(leaf.node_id) : null,
              path: null,
              count: Number(leaf.files || 0),
              sizeHuman: "",
              hasChildren: true,
              children: [],
              hasMore: false,
              ancestors: [node],
            };
            return child;
          });
          state.loaded.add(node.key);
          return node;
        });
        registerNodes(state.roots);
        const notice = $("exportNotice");
        if (notice) {
          const ungrouped = Number(tree.ungrouped_files || 0);
          notice.hidden = !ungrouped;
          notice.textContent = ungrouped
            ? `另有 ${ungrouped.toLocaleString()} 个文件不在任何主题下（无可读内容或未能归组），未在此树中列出。`
            : "";
        }
        setText("exportTreeState", `已载入 ${state.roots.length} 个主题 / ${Number(tree.leaf_total || 0)} 个子目录`
          + (tree.file_count ? ` · 共 ${Number(tree.file_count).toLocaleString()} 个文件` : ""));
      } else {
        const data = await api(`/api/tree/${encodeURIComponent(state.sourceId)}?kind=analysis&limit=100`);
        if (seq !== state.treeRequest) return;
        const rootRaw = data.node || {};
        const root = mapOrdinaryNode(rootRaw, []);
        // 根节点本身不可导出，直接把它的子节点铺在顶层。
        state.roots = root.children.length ? root.children : [root];
        registerNodes(state.roots);
        const notice = $("exportNotice");
        if (notice) {
          notice.hidden = true;
          notice.textContent = "";
        }
        if (!state.roots.length) {
          state.loadError = "该数据包还没有生成智能目录。请先在「智能分析」完成分析后再导出。";
        } else {
          setText("exportTreeState", `已载入 ${state.roots.length} 个内容主题`);
        }
      }
    } catch (error) {
      if (seq !== state.treeRequest) return;
      state.loadError = error.message || "智能目录读取失败";
      setText("exportTreeState", state.loadError);
    }
    renderTree();
  }

  /* ------------------------------------------------------ 展开 / 懒加载 */
  async function loadChildren(node) {
    if (state.loaded.has(node.key)) return;
    if (state.mode === "large") {
      const data = await api(`/api/large-packages/${encodeURIComponent(state.sourceId)}/files`
        + `?node_id=${encodeURIComponent(node.nodeId)}&limit=${FILE_PAGE}&offset=${node.children.length}`);
      const rows = data.files || [];
      rows.forEach((row) => {
        const child = {
          key: `f:${row.path}`,
          kind: "file",
          name: row.name || baseName(row.path),
          nodeId: null,
          path: String(row.path),
          count: 1,
          sizeHuman: row.size_human || humanSize(row.size),
          hasChildren: false,
          children: [],
          hasMore: false,
          ancestors: node.ancestors.concat([node]),
        };
        node.children.push(child);
        state.nodeByKey.set(child.key, child);
      });
      node.hasMore = Boolean(data.has_more);
      if (!node.hasMore) state.loaded.add(node.key);
    } else {
      const data = await api(`/api/tree/${encodeURIComponent(state.sourceId)}?kind=analysis`
        + `&node_key=${encodeURIComponent(node.loadKey)}&offset=${node.children.length}&limit=200`);
      const page = data.node || {};
      const existing = new Set(node.children.map((child) => child.key));
      (page.children || []).forEach((raw) => {
        const child = mapOrdinaryNode(raw, node.ancestors.concat([node]));
        if (existing.has(child.key)) return;
        node.children.push(child);
        registerNodes([child]);
      });
      node.hasMore = page._children_next_offset != null;
      node.count = Number(page._children_total || node.count || 0);
      if (!node.hasMore) state.loaded.add(node.key);
    }
  }

  /* ---------------------------------------------------------------- 渲染 */
  function renderNode(node) {
    const isFile = node.kind === "file";
    const kindLabel = node.kind === "theme" ? "主题" : node.kind === "leaf" ? "目录" : "";
    const meta = isFile
      ? esc(node.sizeHuman || "")
      : `${Number(node.count || 0).toLocaleString()} 个文件`;
    const expanded = state.expanded.has(node.key);
    const twisty = node.hasChildren
      ? `<button type="button" class="ex-twisty" data-act="twisty" aria-label="展开或折叠" aria-expanded="${expanded ? "true" : "false"}">${expanded ? "▾" : "▸"}</button>`
      : '<button type="button" class="ex-twisty" data-leaf="1" tabindex="-1" aria-hidden="true">·</button>';
    let children = "";
    if (!isFile && expanded && node.children.length) {
      const more = node.hasMore
        ? `<button type="button" class="ex-load-more" data-act="more" data-key="${esc(node.key)}">继续加载（${node.children.length}/${Number(node.count || 0).toLocaleString()}）</button>`
        : "";
      children = `<ul class="ex-children">${node.children.map(renderNode).join("")}${more}</ul>`;
    }
    const classes = ["ex-node", `ex-${node.kind}`];
    if (isFile) classes.push("ex-file");
    return `<li class="${classes.join(" ")}" data-key="${esc(node.key)}"${isFile ? ` data-path="${esc(node.path)}"` : ""}>`
      + `<div class="ex-row">${twisty}`
      + `<input type="checkbox" class="ex-check" data-act="check" aria-label="选择">`
      + `<span class="ex-label" title="${esc(isFile ? node.path : node.name)}">${esc(node.name)}</span>`
      + (kindLabel ? `<span class="ex-kind">${kindLabel}</span>` : "")
      + `<span class="ex-meta">${meta}</span>`
      + `</div>${children}</li>`;
  }

  function indexDom(host) {
    domIndex.clear();
    host.querySelectorAll("li.ex-node[data-key]").forEach((li) => domIndex.set(li.dataset.key, li));
  }

  function renderTree() {
    const host = $("exportTree");
    if (!host) return;
    // 整树重建会重置滚动位置；展开靠下的目录时会把用户弹回顶部。
    const scrollTop = host.scrollTop;
    if (state.loadError) {
      host.innerHTML = `<div class="ex-empty">${esc(state.loadError)}</div>`;
      domIndex.clear();
      renderCart();
      return;
    }
    if (!state.roots.length) {
      host.innerHTML = '<div class="ex-empty">当前没有可选择的智能目录。</div>';
      domIndex.clear();
      renderCart();
      return;
    }
    host.innerHTML = `<ul>${state.roots.map(renderNode).join("")}</ul>`;
    host.scrollTop = scrollTop;
    indexDom(host);
    syncChecks();
  }

  /* ------------------------------------------------------------ 选择模型 */
  function chainEffective(ancestors) {
    let effective = false;
    ancestors.forEach((item) => {
      if (!item.nodeId) return;
      if (state.nodes.has(item.nodeId)) effective = true;
      if (state.excludedNodes.has(item.nodeId)) effective = false;
    });
    return effective;
  }

  function folderEffective(node, ancestors) {
    let effective = chainEffective(ancestors || node.ancestors || []);
    if (node.nodeId) {
      if (state.nodes.has(node.nodeId)) effective = true;
      if (state.excludedNodes.has(node.nodeId)) effective = false;
    }
    return effective;
  }

  function fileEffective(path, ancestors) {
    let effective = chainEffective(ancestors);
    if (state.picked.has(path)) effective = true;
    if (state.excluded.has(path)) effective = false;
    return effective;
  }

  function syncSubtree(node, ancestors) {
    const li = domIndex.get(node.key);
    if (node.kind === "file") {
      const selected = fileEffective(node.path, ancestors);
      if (li) {
        li.classList.toggle("ex-covered", chainEffective(ancestors));
        const box = li.querySelector(".ex-check");
        if (box) box.checked = selected;
      }
      return { selected: selected ? 1 : 0, total: 1 };
    }
    const chain = node.nodeId ? ancestors.concat([node]) : ancestors;
    let selected = 0;
    let total = 0;
    if (state.loaded.has(node.key) && node.children.length) {
      node.children.forEach((child) => {
        const result = syncSubtree(child, chain);
        selected += result.selected;
        total += result.total;
      });
    }
    if (!total) {
      total = 1;
      selected = folderEffective(node, ancestors) ? 1 : 0;
    }
    if (li) {
      const box = li.querySelector(".ex-check");
      if (box) {
        box.checked = selected === total && total > 0;
        box.indeterminate = selected > 0 && selected < total;
      }
    }
    return { selected, total };
  }

  function syncChecks() {
    state.roots.forEach((root) => syncSubtree(root, []));
    renderCart();
  }

  function clearExclusionsUnder(node) {
    if (node.nodeId) state.excludedNodes.delete(node.nodeId);
    if (node.path) state.excluded.delete(node.path);
    node.children.forEach(clearExclusionsUnder);
  }

  function toggleFolder(node, next) {
    if (!node.nodeId) return;
    const covered = folderEffective({ nodeId: null }, node.ancestors);
    if (next) {
      state.nodes.add(node.nodeId);
      state.excludedNodes.delete(node.nodeId);
      clearExclusionsUnder(node);
    } else {
      state.nodes.delete(node.nodeId);
      if (covered) state.excludedNodes.add(node.nodeId);
      else state.excludedNodes.delete(node.nodeId);
    }
    syncChecks();
  }

  function toggleFile(node, next) {
    const path = node.path;
    const covered = chainEffective(node.ancestors);
    if (next) {
      state.excluded.delete(path);
      if (!covered) state.picked.add(path);
    } else {
      state.picked.delete(path);
      if (covered) state.excluded.add(path);
      else state.excluded.delete(path);
    }
    syncChecks();
  }

  async function toggleExpand(node) {
    if (!node.hasChildren) return;
    if (state.expanded.has(node.key)) {
      state.expanded.delete(node.key);
      renderTree();
      return;
    }
    state.expanded.add(node.key);
    if (!state.loaded.has(node.key)) {
      setText("exportTreeState", "正在读取目录内容…");
      renderTree();
      try {
        await loadChildren(node);
      } catch (error) {
        state.expanded.delete(node.key);
        renderTree();
        setText("exportTreeState", error.message || "目录内容读取失败");
        notify(error.message || "目录内容读取失败", true);
        return;
      }
    }
    renderTree();
    setText("exportTreeState", "已展开目录，勾选需要的子目录或单个文件。");
  }

  /* ------------------------------------------------------------ 已选清单 */
  function estimateFiles(node, ancestors) {
    if (node.kind === "file") return fileEffective(node.path, ancestors) ? 1 : 0;
    const chain = node.nodeId ? ancestors.concat([node]) : ancestors;
    if (!state.loaded.has(node.key) || !node.children.length) {
      return folderEffective(node, ancestors) ? Number(node.count || 0) : 0;
    }
    let total = 0;
    node.children.forEach((child) => { total += estimateFiles(child, chain); });
    return total;
  }

  function renderCart() {
    const folders = state.nodes.size;
    const picked = state.picked.size;
    const excluded = state.excluded.size + state.excludedNodes.size;
    let estimate = 0;
    state.roots.forEach((root) => { estimate += estimateFiles(root, []); });

    const count = $("exportCartCount");
    if (count) count.textContent = folders || picked ? `已选 ${folders} 个目录 · ${picked} 个文件` : "尚未选择";

    const list = $("exportCartList");
    if (list) {
      const items = [];
      state.nodes.forEach((id) => {
        const node = state.nodeById.get(id);
        if (node) items.push({ name: node.name, note: `${Number(node.count || 0).toLocaleString()} 个文件` });
      });
      state.picked.forEach((path) => items.push({ name: baseName(path), note: "单个文件" }));
      if (!items.length) {
        list.className = "export-cart-list empty";
        list.textContent = "在上方智能目录中勾选主题、子目录或单个文件。";
      } else {
        list.className = "export-cart-list";
        const shown = items.slice(0, 40);
        list.innerHTML = `<ul>${shown.map((item) => `<li><span title="${esc(item.name)}">${esc(item.name)}</span><small>${esc(item.note)}</small></li>`).join("")}</ul>`
          + (items.length > shown.length ? `<div style="margin-top:6px;color:var(--p-subtle,#76839d)">另有 ${items.length - shown.length} 项未在此列出</div>` : "");
      }
    }

    const summary = $("exportCartSummary");
    if (summary) {
      summary.textContent = (folders || picked)
        ? `预计约 ${estimate.toLocaleString()} 个文件${excluded ? ` · 已排除 ${excluded} 项` : ""}`
        : "请选择要导出的内容";
    }

    const topic = String($("exportTaskTopic")?.value || "").trim();
    const submit = $("exportSubmitBtn");
    if (submit) submit.disabled = state.busy || !state.sourceId || (!folders && !picked) || !topic;
  }

  /* ---------------------------------------------------------------- 提交 */
  function buildPayload() {
    const topic = String($("exportTaskTopic")?.value || "").trim();
    if (state.mode === "large") {
      return {
        url: `/api/large-packages/${encodeURIComponent(state.sourceId)}/export`,
        body: {
          node_ids: [...state.nodes],
          paths: [...state.picked],
          exclude_node_ids: [...state.excludedNodes],
          exclude_paths: [...state.excluded],
          task_topic: topic,
          name: "大数据包智能目录导出",
        },
      };
    }
    const selections = [];
    state.nodes.forEach((id) => {
      const node = state.nodeById.get(id);
      selections.push({ kind: "group", node_id: id, path: node?.path || null, name: node?.name || null });
    });
    state.picked.forEach((path) => selections.push({ kind: "file", path, name: baseName(path) }));
    return {
      url: "/api/export",
      body: {
        scan_id: state.sourceId,
        selections,
        exclude_node_ids: [...state.excludedNodes],
        exclude_paths: [...state.excluded],
        task_topic: topic,
      },
    };
  }

  async function submit() {
    const topic = String($("exportTaskTopic")?.value || "").trim();
    if (!state.sourceId) { notify("请先选择数据包", true); return; }
    if (!state.nodes.size && !state.picked.size) { notify("请至少选择一个主题、子目录或文件", true); return; }
    if (!topic) { notify("请输入整编任务主题", true); $("exportTaskTopic")?.focus(); return; }

    const payload = buildPayload();
    if (state.mode === "ordinary" && payload.body.selections.length > ORDINARY_SELECTION_LIMIT) {
      notify(`一次最多选择 ${ORDINARY_SELECTION_LIMIT} 项；当前 ${payload.body.selections.length} 项。请改选上级目录。`, true);
      return;
    }

    state.busy = true;
    const button = $("exportSubmitBtn");
    if (button) { button.disabled = true; button.textContent = "正在创建任务…"; }
    setJobState("正在创建导出任务…");
    const resultBox = $("exportResultBox");
    if (resultBox) resultBox.hidden = true;
    try {
      const data = await api(payload.url, { method: "POST", body: JSON.stringify(payload.body) });
      setJobState("任务已提交，正在等待 Worker…", false, 1);
      pollJob(data.job_id);
    } catch (error) {
      setJobState(error.message || "导出任务创建失败", true);
      notify(error.message || "导出任务创建失败", true);
    } finally {
      state.busy = false;
      if (button) button.textContent = "导出为压缩包";
      renderCart();
    }
  }

  /* ------------------------------------------------------ 任务与结果展示 */
  function setJobState(message, error, progress) {
    const el = $("exportJobState");
    if (el) {
      el.className = "export-job-state" + (error ? " is-error" : "");
      el.textContent = message || "";
    }
    const bar = $("exportJobProgress");
    if (bar) {
      const value = Number(progress || 0);
      bar.hidden = !(value > 0 && value < 100);
      const fill = bar.querySelector("i");
      if (fill) fill.style.width = `${Math.max(0, Math.min(100, value))}%`;
    }
  }

  async function pollJob(jobId) {
    clearTimeout(state.poll);
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      const job = data.job || {};
      const progress = Number(job.progress || 0);
      if (["queued", "running", "cancelling"].includes(job.status)) {
        setJobState(`${job.message || "导出处理中"}（${progress}%）`, false, progress);
        state.poll = setTimeout(() => pollJob(jobId), 1500);
        return;
      }
      if (job.status === "completed" && job.result && job.result.file_name) {
        setJobState("导出完成。", false, 100);
        showResult(job.result);
        return;
      }
      if (job.status === "cancelled") {
        setJobState("导出已取消。", true);
      } else {
        setJobState(job.error || job.message || "导出任务未完成。", true);
      }
    } catch (error) {
      setJobState(`${error.message || "任务状态读取失败"}，稍后重试…`, false, 0);
      state.poll = setTimeout(() => pollJob(jobId), 2500);
    }
  }

  function showResult(result) {
    const box = $("exportResultBox");
    const name = String(result.file_name || "");
    const files = Number(result.source_file_count || 0);
    const selections = Number(result.selection_count || 0);
    const volumes = Array.isArray(result.volumes) ? result.volumes : [];
    if (box) {
      box.hidden = false;
      box.innerHTML = `<h4>✓ 压缩包已生成</h4>`
        + `<span class="ex-file-name">${esc(name)}</span>`
        + `<span class="ex-file-path">服务器位置：outputs/${esc(name)}</span>`
        + `<p>共 ${files.toLocaleString()} 个源文件，来自 ${selections.toLocaleString()} 个选择项。</p>`
        + `<div class="export-result-actions">`
        + `<button class="primary" type="button" data-download="${esc(result.download_url || `/outputs/${name}`)}">下载压缩包${volumes.length ? "（分卷索引）" : ""}</button>`
        + volumes.map((item) => `<button class="secondary" type="button" data-download="${esc(item.download_url || "")}">下载分卷 ${esc(item.index)}</button>`).join("")
        + `</div>`;
    }
    notify("导出完成，可在下方导出记录中随时重新下载。");
    if ($("exportAutoDownload")?.checked) download(result.download_url || `/outputs/${name}`);
    loadRecords();
  }

  /* ------------------------------------------------------------ 导出记录 */
  async function loadRecords() {
    const host = $("exportRecords");
    if (!host) return;
    if (!host.dataset.loaded) host.innerHTML = '<div class="ex-empty">正在读取导出记录…</div>';
    try {
      // 专用读取口：服务端已按 task_type=export_package 过滤。用通用的
      // /api/jobs 再在前端过滤的话，导出记录会被大量摘要任务挤出结果集。
      const data = await api("/api/exports?limit=50");
      const jobs = (data.items || []).filter((job) => job.result && job.result.file_name);
      host.dataset.loaded = "1";
      if (!jobs.length) {
        host.innerHTML = '<div class="ex-empty">还没有导出记录。完成一次导出后，压缩包会一直列在这里，可随时重新下载。</div>';
        return;
      }
      host.innerHTML = jobs.map((job) => {
        const result = job.result || {};
        const sourceId = String(job.scan_id || "");
        const sourceName = state.nameById.get(sourceId) || sourceId || "—";
        const isLarge = sourceId.startsWith("lp-");
        const volumes = Array.isArray(result.volumes) ? result.volumes : [];
        return `<article class="export-record">`
          + `<div class="export-record-main">`
          + `<span class="export-record-name" title="${esc(result.file_name)}">${esc(result.file_name)}</span>`
          + `<div class="export-record-meta">`
          + `<span>来源：<b>${esc(sourceName)}</b>${isLarge ? "（大数据包）" : ""}</span>`
          + `<span>源文件：<b>${Number(result.source_file_count || 0).toLocaleString()}</b> 个</span>`
          + (job.task_topic ? `<span>主题：<b>${esc(job.task_topic)}</b></span>` : "")
          + `<span>完成于：<b>${esc(formatTime(job.finished_at || job.updated_at))}</b></span>`
          + (volumes.length ? `<span>分卷：<b>${volumes.length}</b> 个</span>` : "")
          + `<span>位置：<b>outputs/${esc(result.file_name)}</b></span>`
          + `</div></div>`
          + `<div class="export-record-actions">`
          + `<button class="primary" type="button" data-download="${esc(result.download_url || "")}">下载</button>`
          + volumes.map((item) => `<button class="secondary" type="button" data-download="${esc(item.download_url || "")}">分卷 ${esc(item.index)}</button>`).join("")
          + `</div></article>`;
      }).join("");
    } catch (error) {
      host.innerHTML = `<div class="ex-empty">导出记录读取失败：${esc(error.message || "")}</div>`;
    }
  }

  /* ---------------------------------------------------------------- 绑定 */
  function setMode(mode) {
    state.mode = mode === "ordinary" ? "ordinary" : "large";
    document.querySelectorAll("[data-export-mode]").forEach((tab) => {
      const active = tab.dataset.exportMode === state.mode;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    const topic = $("exportTaskTopic");
    if (topic) {
      topic.placeholder = state.mode === "large" ? "例如：按业务主题交付原始资料" : "例如：按主题交付待整编资料";
    }
    const button = $("exportSubmitBtn");
    if (button) button.textContent = "导出为压缩包";
    resetTree("");
    loadSources();
  }

  function expandAll() {
    state.roots.forEach((root) => state.expanded.add(root.key));
    renderTree();
  }

  function collapseAll() {
    state.expanded.clear();
    renderTree();
  }

  let bound = false;
  function bind() {
    // 幂等：脚本被重复引入时（缓存版本切换期的重复 script 标签）不会把同一批
    // 监听器注册两遍——那样一次点击会被处理两次，展开后立刻又折叠。
    if (bound) return;
    bound = true;

    document.querySelectorAll("[data-export-mode]").forEach((tab) => {
      tab.addEventListener("click", () => setMode(tab.dataset.exportMode));
    });

    const tree = $("exportTree");
    tree?.addEventListener("click", async (event) => {
      const twisty = event.target.closest("[data-act='twisty']");
      if (twisty) {
        const li = twisty.closest("li[data-key]");
        const node = li && state.nodeByKey.get(li.dataset.key);
        if (node) await toggleExpand(node);
        return;
      }
      const more = event.target.closest("[data-act='more']");
      if (more) {
        const node = state.nodeByKey.get(more.dataset.key);
        if (!node) return;
        more.disabled = true;
        more.textContent = "正在加载…";
        try {
          await loadChildren(node);
        } catch (error) {
          notify(error.message || "目录内容读取失败", true);
        }
        renderTree();
      }
    });
    tree?.addEventListener("change", (event) => {
      const box = event.target.closest("input.ex-check");
      if (!box) return;
      const li = box.closest("li[data-key]");
      const node = li && state.nodeByKey.get(li.dataset.key);
      if (!node) return;
      if (node.kind === "file") toggleFile(node, box.checked);
      else toggleFolder(node, box.checked);
    });

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-download]");
      if (!button) return;
      event.preventDefault();
      download(button.dataset.download || "");
    });

    $("exportRefreshBtn")?.addEventListener("click", () => loadTree());
    $("exportExpandAllBtn")?.addEventListener("click", expandAll);
    $("exportCollapseAllBtn")?.addEventListener("click", collapseAll);
    $("exportClearSelectionBtn")?.addEventListener("click", () => { clearSelection(); syncChecks(); });
    $("exportSubmitBtn")?.addEventListener("click", submit);
    $("exportTaskTopic")?.addEventListener("input", renderCart);
    $("exportRecordsRefreshBtn")?.addEventListener("click", () => {
      const host = $("exportRecords");
      if (host) host.dataset.loaded = "";
      loadRecords();
    });
    $("exportSourceSelect")?.addEventListener("change", (event) => {
      state.sourceId = event.target.value;
      loadTree();
    });

    window.addEventListener("sjfx-shell-state", (event) => {
      if (event.detail?.route === "exports") activate("exports");
    });
  }

  function activate(route) {
    if (route !== "exports") return;
    if (!state.loadedSources) {
      state.loadedSources = true;
      loadSources();
    }
    loadRecords();
  }

  window.SJFXExportCenter = { activate, reload: () => loadSources() };
  document.addEventListener("DOMContentLoaded", () => {
    bind();
    if (window.SJFXShell?.route === "exports") activate("exports");
  });
})();
