// API token is read from the environment; never hardcode real credentials.
// Usage: SJFX_API_TOKEN=xxx node test_export_center_large.js
/* 导出中心前端集成测试：用 jsdom 加载真实 markup + 真实 export-center.js，
 * 通过真实 HTTP 打到运行中的服务，验证"目录树 → 勾选/排除 → 提交 → 下载记录"。
 *
 * 注意：renderTree() 会整树重建 DOM，所以每次交互后用 key 重新查询元素，
 * 不能持有旧引用。 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = "/home/nas511/zhoulingjie/sjfx";
const BASE = "http://127.0.0.1:18000";
const TOKEN = process.env.SJFX_API_TOKEN || "";
const PID = "lp-605781becd3f404580f9e56f24f4ee5b";

let failures = 0;
function check(label, ok, detail) {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${label}${detail ? "  [" + detail + "]" : ""}`);
  if (!ok) failures += 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const html = fs.readFileSync(path.join(ROOT, "templates/index.html"), "utf8");
  const match = html.match(/<section class="app-view utility-view" data-view="exports">[\s\S]*?<\/section>/);
  if (!match) throw new Error("exports view not found in index.html");

  const dom = new JSDOM(
    `<!doctype html><html><body><div id="toast" class="toast"></div>${match[0]}</body></html>`,
    { runScripts: "outside-only", pretendToBeVisual: true, url: "http://127.0.0.1:18000/" }
  );
  const { window } = dom;
  const document = window.document;

  const li = (key) => document.querySelector(`#exportTree li[data-key="${key}"]`);
  const rows = (selector) => Array.from(document.querySelectorAll(selector));
  const click = (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const toggle = (el, checked) => {
    el.checked = checked;
    el.dispatchEvent(new window.Event("change", { bubbles: true }));
  };
  async function until(fn, label, timeout = 30000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      let value;
      try { value = fn(); } catch (_) { value = false; }
      if (value) return value;
      await sleep(120);
    }
    throw new Error("timeout waiting for " + label);
  }

  async function realApi(url, options = {}) {
    const headers = { "X-SJFX-Token": TOKEN, ...(options.headers || {}) };
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(BASE + url, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      const error = new Error(data.error || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  const toasts = [];
  window.sjfxApi = realApi;
  window.toast = (message, error) => toasts.push({ message, error: Boolean(error) });
  window.authenticatedDownload = async () => { window.__downloaded = (window.__downloaded || 0) + 1; };
  window.SJFXShell = { route: "exports", activate() {} };

  window.eval(fs.readFileSync(path.join(ROOT, "static/export-center.js"), "utf8"));
  document.dispatchEvent(new window.Event("DOMContentLoaded"));

  console.log("\n=== 1. 数据源与目录树加载 ===");
  await until(() => rows("#exportTree li.ex-theme").length > 0, "主题渲染");
  const select = document.getElementById("exportSourceSelect");
  check("数据包下拉已填充", select.options.length > 1, `${select.options.length} 项`);
  // "当前大数据包"会随时间变化（新导入/新活动会把它顶到最前），
  // 只断言选中了一个真实的大数据包，不硬编码具体 id。
  check("默认选中了一个大数据包", /^lp-/.test(select.value), select.value);
  const activePid = select.value;
  // **不写死主题数阈值。** 原来断言 `>= 10`，但"当前大数据包"是历史列表里最新的那个，
  // 它可能是任意规模的包（实测有 5 个主题的、也有 20 个主题的），阈值一改夹具就红。
  // 改成拿接口的 theme_total 对账：既验证"树真的渲染了"，也验证"渲染的数量是对的"。
  // 正在被后台重建的包可能在两次读取之间变化，所以对不上时重取一次再判。
  async function themeTotalOf(pid) {
    const resp = await realApi(`/api/large-packages/${pid}/catalog/tree`);
    return Number(((resp && resp.tree) || resp || {}).theme_total || 0);
  }
  let expectedThemes = await themeTotalOf(activePid);
  let renderedThemes = rows("#exportTree li.ex-theme").length;
  if (renderedThemes !== expectedThemes) {
    await sleep(1500);
    expectedThemes = await themeTotalOf(activePid);
    renderedThemes = rows("#exportTree li.ex-theme").length;
  }
  check("主题已渲染", renderedThemes > 0 && renderedThemes === expectedThemes,
    `${renderedThemes} 个主题（接口 ${expectedThemes}）`);
  check("初始未展开任何子目录", rows("#exportTree li.ex-leaf").length === 0);
  check("状态栏显示主题/子目录/文件数",
    document.getElementById("exportTreeState").textContent.includes("子目录"),
    document.getElementById("exportTreeState").textContent);

  console.log("\n=== 2. 展开主题 → 子目录 ===");
  const themeKey = rows("#exportTree li.ex-theme")[0].dataset.key;
  click(li(themeKey).querySelector("[data-act='twisty']"));
  await until(() => li(themeKey).querySelectorAll("li.ex-leaf").length > 0, "子目录渲染");
  const leafKeys = Array.from(li(themeKey).querySelectorAll("li.ex-leaf")).map((el) => el.dataset.key);
  check("子目录已渲染", leafKeys.length > 1, `${leafKeys.length} 个子目录`);
  check("子目录显示文件数", /\d/.test(li(leafKeys[0]).querySelector(".ex-meta").textContent),
    li(leafKeys[0]).querySelector(".ex-meta").textContent.trim());
  check("主题行标记为已展开",
    li(themeKey).querySelector("[data-act='twisty']").getAttribute("aria-expanded") === "true");

  console.log("\n=== 3. 勾选整个主题 ===");
  toggle(li(themeKey).querySelector(".ex-check"), true);
  await sleep(150);
  const themeName = li(themeKey).querySelector(".ex-label").textContent.trim();
  check("已选清单出现该主题", document.getElementById("exportCartList").textContent.includes(themeName),
    themeName);
  check("子目录继承为已勾选", li(leafKeys[0]).querySelector(".ex-check").checked === true);
  check("缺任务主题时提交按钮禁用", document.getElementById("exportSubmitBtn").disabled === true);
  check("已选计数正确", document.getElementById("exportCartCount").textContent.includes("1 个目录"),
    document.getElementById("exportCartCount").textContent);

  console.log("\n=== 4. 在已勾选主题内取消一个子目录（按节点排除） ===");
  const dropLeafKey = leafKeys[0];
  toggle(li(dropLeafKey).querySelector(".ex-check"), false);
  await sleep(150);
  const themeBox = li(themeKey).querySelector(".ex-check");
  check("主题复选框变为半选", themeBox.indeterminate === true,
    `checked=${themeBox.checked} indeterminate=${themeBox.indeterminate}`);
  check("汇总显示已排除", document.getElementById("exportCartSummary").textContent.includes("已排除"),
    document.getElementById("exportCartSummary").textContent);

  console.log("\n=== 5. 展开另一个子目录 → 文件级选择 ===");
  const keepLeafKey = leafKeys.find((k) => k !== dropLeafKey);
  click(li(keepLeafKey).querySelector("[data-act='twisty']"));
  await until(() => li(keepLeafKey).querySelectorAll("li.ex-file").length > 0, "文件渲染");
  const fileEls = li(keepLeafKey).querySelectorAll("li.ex-file");
  check("文件行已渲染（文件级选择）", fileEls.length > 0, `${fileEls.length} 个文件`);
  check("文件行显示相对路径", (fileEls[0].dataset.path || "").length > 0, fileEls[0].dataset.path.slice(0, 60));
  check("文件继承勾选状态", fileEls[0].querySelector(".ex-check").checked === true);

  const dropFilePath = fileEls[0].dataset.path;
  toggle(li(keepLeafKey).querySelector("li.ex-file .ex-check"), false);
  await sleep(150);
  check("取消单个文件后该行未勾选",
    li(keepLeafKey).querySelector("li.ex-file .ex-check").checked === false);
  check("汇总排除计数增加", document.getElementById("exportCartSummary").textContent.includes("已排除"));

  console.log("\n=== 6. 取消后再勾回：应回到『由上级包含』状态 ===");
  toggle(li(keepLeafKey).querySelector("li.ex-file .ex-check"), true);
  await sleep(150);
  check("重新勾回后该行恢复勾选",
    li(keepLeafKey).querySelector("li.ex-file .ex-check").checked === true);
  check("重新勾回后不再计入排除",
    !document.getElementById("exportCartSummary").textContent.includes("已排除 2 项"),
    document.getElementById("exportCartSummary").textContent);
  // 复原成"排除该文件"，供第 7 节断言载荷
  toggle(li(keepLeafKey).querySelector("li.ex-file .ex-check"), false);
  await sleep(150);

  console.log("\n=== 7. 在未勾选的主题下单独勾选一个文件 ===");
  const otherThemeKey = rows("#exportTree li.ex-theme")
    .map((el) => el.dataset.key).find((k) => k !== themeKey);
  click(li(otherThemeKey).querySelector("[data-act='twisty']"));
  await until(() => li(otherThemeKey).querySelectorAll("li.ex-leaf").length > 0, "第二个主题的子目录");
  const otherLeafKey = li(otherThemeKey).querySelector("li.ex-leaf").dataset.key;
  click(li(otherLeafKey).querySelector("[data-act='twisty']"));
  await until(() => li(otherLeafKey).querySelectorAll("li.ex-file").length > 0, "第二个主题的文件列表");
  const looseFile = li(otherLeafKey).querySelector("li.ex-file");
  const loosePath = looseFile.dataset.path;
  check("未勾选主题下的文件初始未勾选", looseFile.querySelector(".ex-check").checked === false);
  toggle(looseFile.querySelector(".ex-check"), true);
  await sleep(150);
  check("单独勾选的文件计入已选",
    document.getElementById("exportCartCount").textContent.includes("1 个文件"),
    document.getElementById("exportCartCount").textContent);
  check("单独勾选的文件未显示为被覆盖",
    li(otherLeafKey).querySelector("li.ex-file").classList.contains("ex-covered") === false);

  console.log("\n=== 7. 提交任务（真实 HTTP） ===");
  const topicInput = document.getElementById("exportTaskTopic");
  topicInput.value = "jsdom 集成测试导出";
  topicInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(80);
  check("填写任务主题后提交按钮可用", document.getElementById("exportSubmitBtn").disabled === false);

  let posted = null;
  const originalApi = window.sjfxApi;
  window.sjfxApi = async (url, options) => {
    if (url.includes("/export") && options && options.method === "POST") posted = JSON.parse(options.body);
    return originalApi(url, options);
  };
  click(document.getElementById("exportSubmitBtn"));
  await until(() => posted, "导出请求");
  console.log("    提交载荷: " + JSON.stringify({
    node_ids: posted.node_ids, paths: posted.paths,
    exclude_node_ids: posted.exclude_node_ids, exclude_paths: posted.exclude_paths,
    task_topic: posted.task_topic,
  }));

  check("node_ids 含被勾选的主题", posted.node_ids.includes(themeKey.replace(/^t:/, "")),
    JSON.stringify(posted.node_ids));
  check("exclude_node_ids 含被取消的子目录", posted.exclude_node_ids.includes(dropLeafKey.replace(/^l:/, "")),
    JSON.stringify(posted.exclude_node_ids));
  check("exclude_paths 含被取消的文件", posted.exclude_paths.includes(dropFilePath),
    JSON.stringify(posted.exclude_paths));
  if (loosePath) check("paths 含单独勾选的文件", posted.paths.includes(loosePath), JSON.stringify(posted.paths));
  check("任务主题随载荷提交", posted.task_topic === "jsdom 集成测试导出");

  console.log("\n=== 8. 等待导出完成并检查结果卡 ===");
  await until(() => {
    const box = document.getElementById("exportResultBox");
    return box && box.hidden === false && box.querySelector("[data-download]");
  }, "结果卡出现", 900000);
  const box = document.getElementById("exportResultBox");
  check("结果卡显示压缩包文件名", /\.zip/.test(box.querySelector(".ex-file-name").textContent),
    box.querySelector(".ex-file-name").textContent);
  check("结果卡显示服务器落盘路径", box.querySelector(".ex-file-path").textContent.includes("outputs/"),
    box.querySelector(".ex-file-path").textContent);
  check("结果卡有下载按钮", Boolean(box.querySelector(".primary[data-download]")));
  check("完成后自动触发下载", window.__downloaded === 1, String(window.__downloaded));
  check("任务状态显示完成", document.getElementById("exportJobState").textContent.includes("完成"),
    document.getElementById("exportJobState").textContent);
  check("任务状态区无错误样式", document.getElementById("exportJobState").className.includes("is-error") === false);

  console.log("\n=== 9. 导出记录 ===");
  const exportedName = box.querySelector(".ex-file-name").textContent.trim();
  await until(() => Array.from(rows("#exportRecords .export-record-name"))
    .some((el) => el.textContent.trim() === exportedName), "本次导出出现在记录中", 30000);
  check("导出记录已渲染", rows("#exportRecords .export-record").length > 0,
    `${rows("#exportRecords .export-record").length} 条`);
  const first = Array.from(rows("#exportRecords .export-record"))
    .find((el) => el.querySelector(".export-record-name").textContent.trim() === exportedName);
  const meta = first.querySelector(".export-record-meta").textContent.replace(/\s+/g, " ");
  check("最新记录就是本次导出", Boolean(first), exportedName);
  check("记录显示文件名", first.querySelector(".export-record-name").textContent.includes(".zip"),
    first.querySelector(".export-record-name").textContent);
  // 来源名从接口取，不硬编码（包名随导入变化：cssj1 / cssj3 / ...）。
  const pkgList = await realApi("/api/large-packages/history?limit=50");
  const expectItem = (pkgList.items || []).find(
    (i) => String(i.large_package_id || i.scan_id || "") === activePid);
  const expectLabel = expectItem ? String(expectItem.name || "") : "";
  check("记录显示来源数据包名",
    expectLabel ? meta.includes(expectLabel) : /来源：\S/.test(meta),
    `期望含 ${expectLabel} / 实际 ${meta.slice(0, 80)}`);
  check("记录显示落盘位置", meta.includes("outputs/"));
  check("记录有下载按钮", Boolean(first.querySelector("[data-download]")));

  console.log("\n=== 10. 切到普通文件导出 tab ===");
  const ordinaryTab = document.querySelector("[data-export-mode='ordinary']");
  click(ordinaryTab);
  await sleep(4000);
  check("tab 高亮已切换", ordinaryTab.classList.contains("active"));
  check("数据源已切到普通数据包", document.getElementById("exportSourceSelect").options.length > 1,
    `${document.getElementById("exportSourceSelect").options.length} 项`);
  check("已选内容已清空", document.getElementById("exportCartCount").textContent.includes("尚未选择"),
    document.getElementById("exportCartCount").textContent);
  console.log("    普通目录状态:", document.getElementById("exportTreeState").textContent);

  console.log("\n=== 页面提示 ===");
  if (toasts.length) toasts.forEach((t) => console.log(`  ${t.error ? "[错误] " : "[提示] "}${t.message}`));
  else console.log("  (无)");

  console.log(`\n===== 结果：${failures === 0 ? "全部通过" : failures + " 项失败"} =====`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((error) => {
  console.error("\n测试异常终止:", error && error.stack ? error.stack : error);
  process.exit(2);
});
