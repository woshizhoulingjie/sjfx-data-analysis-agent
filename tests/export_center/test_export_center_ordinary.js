// API token is read from the environment; never hardcode real credentials.
// Usage: SJFX_API_TOKEN=xxx node test_export_center_ordinary.js
/* 普通文件导出 tab 的端到端验证：确认两个 tab 走的是同一套选择器，
 * 只是数据来源与提交接口不同。 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = "/home/nas511/zhoulingjie/sjfx";
const BASE = "http://127.0.0.1:18000";
const TOKEN = process.env.SJFX_API_TOKEN || "";

let failures = 0;
function check(label, ok, detail) {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${label}${detail ? "  [" + detail + "]" : ""}`);
  if (!ok) failures += 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const html = fs.readFileSync(path.join(ROOT, "templates/index.html"), "utf8");
  const match = html.match(/<section class="app-view utility-view" data-view="exports">[\s\S]*?<\/section>/);
  const dom = new JSDOM(`<!doctype html><html><body><div id="toast"></div>${match[0]}</body></html>`,
    { runScripts: "outside-only", pretendToBeVisual: true, url: "http://127.0.0.1:18000/" });
  const { window } = dom;
  const document = window.document;

  const li = (key) => document.querySelector(`#exportTree li[data-key="${key}"]`);
  const rows = (sel) => Array.from(document.querySelectorAll(sel));
  const click = (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const toggle = (el, checked) => { el.checked = checked; el.dispatchEvent(new window.Event("change", { bubbles: true })); };
  async function until(fn, label, timeout = 60000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      let v; try { v = fn(); } catch (_) { v = false; }
      if (v) return v;
      await sleep(150);
    }
    throw new Error("timeout waiting for " + label);
  }

  async function realApi(url, options = {}) {
    const headers = { "X-SJFX-Token": TOKEN, ...(options.headers || {}) };
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(BASE + url, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  const toasts = [];
  window.sjfxApi = realApi;
  window.toast = (m, e) => toasts.push({ m, e: Boolean(e) });
  window.authenticatedDownload = async () => { window.__downloaded = (window.__downloaded || 0) + 1; };
  window.SJFXShell = { route: "exports", activate() {} };
  window.eval(fs.readFileSync(path.join(ROOT, "static/export-center.js"), "utf8"));
  document.dispatchEvent(new window.Event("DOMContentLoaded"));

  console.log("\n=== 切到普通文件导出 tab ===");
  await until(() => rows("#exportTree li.ex-theme").length > 0, "大数据包目录");
  click(document.querySelector("[data-export-mode='ordinary']"));

  // 切 tab 后数据源是异步换的：不能只看"选项数 > 1"，那还是大数据包列表。
  // 等到下拉里出现真正属于普通数据包的 scan_id 再继续。
  const ds = await realApi("/api/data-sources?limit=50&offset=0");
  const byId = new Map((ds.items || []).map((i) => [String(i.scan_id), i]));
  const select = document.getElementById("exportSourceSelect");
  await until(() => Array.from(select.options).some((opt) => byId.has(String(opt.value))),
    "下拉切换到普通数据包", 60000);

  const usable = Array.from(select.options)
    .map((opt) => byId.get(String(opt.value)))
    .filter((item) => item && item.analysis_ready && item.file_count > 0)
    .sort((a, b) => a.file_count - b.file_count);
  const target = usable[0];
  if (!target) throw new Error("下拉里没有可用的普通数据包");
  console.log(`  选用数据源: ${target.name || target.scan_id} (${target.file_count} 个文件)`);
  select.value = target.scan_id;
  select.dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(200);
  const expectedScanId = select.value;
  check("界面选中了预期的数据源", expectedScanId === String(target.scan_id), expectedScanId);

  await until(() => document.getElementById("exportTreeState").textContent.includes("主题"), "普通智能目录", 60000);
  const themeEls = rows("#exportTree li.ex-theme, #exportTree li.ex-leaf");
  check("普通数据包渲染出智能目录节点", themeEls.length > 0, `${themeEls.length} 个顶层节点`);
  check("普通 tab 也提示了目录规模", document.getElementById("exportTreeState").textContent.length > 0,
    document.getElementById("exportTreeState").textContent);

  console.log("\n=== 展开并做文件级选择 ===");
  const rootKey = themeEls[0].dataset.key;
  click(li(rootKey).querySelector("[data-act='twisty']"));
  await until(() => li(rootKey).querySelectorAll("li.ex-leaf, li.ex-file").length > 0, "子节点渲染", 60000);
  const childSel = li(rootKey).querySelectorAll("li.ex-leaf");
  check("展开后出现子目录", childSel.length > 0, `${childSel.length} 个子目录`);

  const leafKey = childSel[0].dataset.key;
  click(li(leafKey).querySelector("[data-act='twisty']"));
  await until(() => li(leafKey).querySelectorAll("li.ex-file").length > 0, "普通模式文件渲染", 60000);
  const fileEls = li(leafKey).querySelectorAll("li.ex-file");
  check("普通模式渲染出可勾选的文件行", fileEls.length > 0, `${fileEls.length} 个文件`);

  const firstPath = fileEls[0].dataset.path;
  toggle(li(leafKey).querySelector("li.ex-file .ex-check"), true);
  await sleep(200);
  check("勾选文件后计入已选", document.getElementById("exportCartCount").textContent.includes("1 个文件"),
    document.getElementById("exportCartCount").textContent);

  // 再勾选一个父目录，验证"目录 + 文件"混合选择
  toggle(li(rootKey).querySelector(".ex-check"), true);
  await sleep(200);
  check("再勾选目录后仍可提交", document.getElementById("exportCartCount").textContent.includes("1 个目录"),
    document.getElementById("exportCartCount").textContent);

  console.log("\n=== 提交普通文件导出 ===");
  const topic = document.getElementById("exportTaskTopic");
  topic.value = "普通导出集成测试";
  topic.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(100);

  let posted = null;
  const originalApi = window.sjfxApi;
  window.sjfxApi = async (url, options) => {
    if (url === "/api/export" && options && options.method === "POST") posted = JSON.parse(options.body);
    return originalApi(url, options);
  };
  click(document.getElementById("exportSubmitBtn"));
  await until(() => posted, "普通导出请求");
  console.log("    提交载荷: " + JSON.stringify({
    scan_id: posted.scan_id,
    selection_count: posted.selections.length,
    sample: posted.selections.slice(0, 2),
    exclude_node_ids: posted.exclude_node_ids,
    exclude_paths: posted.exclude_paths,
    task_topic: posted.task_topic,
  }));
  check("提交到普通导出接口 /api/export", posted.scan_id === expectedScanId, posted.scan_id);
  check("选择项含目录节点", posted.selections.some((s) => s.node_id && s.kind === "group"),
    JSON.stringify(posted.selections.filter((s) => s.node_id).slice(0, 1)));
  check("选择项含单个文件路径", posted.selections.some((s) => s.path === firstPath && s.kind === "file"),
    firstPath);
  check("任务主题随载荷提交", posted.task_topic === "普通导出集成测试");

  console.log("\n=== 等待完成 ===");
  await until(() => {
    const box = document.getElementById("exportResultBox");
    return box && box.hidden === false && box.querySelector("[data-download]");
  }, "普通导出结果卡", 900000);
  const box = document.getElementById("exportResultBox");
  check("普通导出也产出压缩包", /\.zip/.test(box.querySelector(".ex-file-name").textContent),
    box.querySelector(".ex-file-name").textContent);
  check("结果卡显示落盘路径", box.querySelector(".ex-file-path").textContent.includes("outputs/"));

  await until(() => Array.from(rows("#exportRecords .export-record-name"))
    .some((el) => el.textContent.trim() === box.querySelector(".ex-file-name").textContent.trim()),
    "普通导出进入记录", 30000);
  const rec = Array.from(rows("#exportRecords .export-record"))
    .find((el) => el.querySelector(".export-record-name").textContent.trim()
      === box.querySelector(".ex-file-name").textContent.trim());
  const expectedName = byId.get(expectedScanId)?.name || expectedScanId;
  check("导出记录来源显示普通数据包名", rec.querySelector(".export-record-meta").textContent.includes(expectedName),
    `期望包含 ${expectedName} / 实际 ${rec.querySelector(".export-record-meta").textContent.replace(/\s+/g, " ").slice(0, 100)}`);
  check("普通导出记录无『大数据包』标记",
    !rec.querySelector(".export-record-meta").textContent.includes("（大数据包）"));

  console.log("\n=== 页面提示 ===");
  toasts.forEach((t) => console.log(`  ${t.e ? "[错误] " : "[提示] "}${t.m}`));
  console.log(`\n===== 结果：${failures === 0 ? "全部通过" : failures + " 项失败"} =====`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((error) => {
  console.error("\n测试异常终止:", error && error.stack ? error.stack : error);
  process.exit(2);
});
