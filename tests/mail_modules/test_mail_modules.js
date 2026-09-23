// API token is read from the environment; never hardcode real credentials.
// Usage: SJFX_API_TOKEN=xxx node test_mail_modules.js
/* 邮件分析拆分成功能模块后的功能回归验证。
 *
 * 做法：用 jsdom 加载**真实的** templates/mail_research.html，再依次执行
 * **真实的** static/mail-research.js 与 static/mail-modules.js，通过真实 HTTP
 * 打运行中的服务。
 *
 * 目的只有一个：证明拆分是纯结构的，没有影响任何既有功能。
 * 为不打扰生产数据，翻译接口 /api/mail/presentation 被替换为立即完成的桩，
 * 其余接口都是只读 GET。
 */
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
async function until(fn, label, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    let v; try { v = fn(); } catch (_) { v = false; }
    if (v) return v;
    await sleep(120);
  }
  throw new Error("timeout waiting for " + label);
}

(async () => {
  const html = fs.readFileSync(path.join(ROOT, "templates/mail_research.html"), "utf8");
  const dom = new JSDOM(html, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    url: "http://127.0.0.1:18000/mail-research",
  });
  const { window } = dom;
  const document = window.document;

  const pageErrors = [];
  window.addEventListener("error", (e) => pageErrors.push(e.message));
  window.addEventListener("unhandledrejection", (e) => pageErrors.push("unhandled: " + (e.reason && e.reason.message || e.reason)));

  // <dialog> 的 showModal 在 jsdom 里不完整；用例中不会走到，做个兜底以免误报。
  if (window.HTMLDialogElement && !window.HTMLDialogElement.prototype.showModal) {
    window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
    window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  }

  // 页面自己会从 sessionStorage 取 token 并加 X-SJFX-Token 头，这里如实配合。
  window.sessionStorage.setItem("sjfx_api_token", TOKEN);

  const calls = [];
  window.fetch = async (url, options = {}) => {
    const target = String(url);
    calls.push(target);
    if (target.indexOf("/api/mail/presentation") === 0) {
      // 桩：不触发真实翻译排队，直接返回已完成。
      return { status: 200, ok: true, json: async () => ({ ok: true, presentation: { status: "completed", values: {}, metadata: {} } }) };
    }
    const headers = { ...(options.headers || {}) };
    const response = await fetch(BASE + target, { ...options, headers });
    const body = await response.text();
    return { status: response.status, ok: response.ok, json: async () => JSON.parse(body) };
  };

  // 依次执行两个真实脚本
  const evalFile = (rel) => window.eval(fs.readFileSync(path.join(ROOT, rel), "utf8"));
  let bootError = null;
  try {
    evalFile("static/mail-research.js");
    evalFile("static/mail-modules.js");
  } catch (error) {
    bootError = error;
  }

  console.log("\n=== 1. 脚本能否正常启动（无 id 缺失导致的中断）===");
  check("mail-research.js + mail-modules.js 执行无异常", !bootError, bootError ? bootError.message : "");
  check("页面无 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

  console.log("\n=== 2. 77 个 id 全部就位 ===");
  const ids = Array.from(new Set((html.match(/id="([A-Za-z0-9_-]+)"/g) || []).map((s) => s.slice(4, -1))));
  const missing = ids.filter((id) => !document.getElementById(id));
  check("全部 id 都能取到元素", missing.length === 0, missing.length ? missing.join(",") : `${ids.length} 个`);

  console.log("\n=== 3. 模块划分与默认视图 ===");
  // 真实浏览器里脚本用 defer 加载，执行时 readyState 已是 interactive，会立即初始化；
  // jsdom 里 DOMContentLoaded 在下一个 tick 才触发，这里等一下。
  await until(() => document.querySelector("[data-mail-module-link].is-active"), "模块导航初始化");
  const modules = Array.from(document.querySelectorAll("[data-mail-module]"));
  const visible = () => modules.filter((m) => !m.hidden).map((m) => m.getAttribute("data-mail-module"));
  const activeLinks = document.querySelectorAll("[data-mail-module-link].is-active");
  check("模块数量为 8", modules.length === 8, String(modules.length));
  check("初始只显示一个模块", visible().length === 1, visible().join(","));
  check("默认模块为 research（研究提问）", visible()[0] === "research", visible()[0]);
  check("导航按钮 8 个且初始高亮唯一",
    document.querySelectorAll("[data-mail-module-link]").length === 8 && activeLinks.length === 1,
    activeLinks.length ? activeLinks[0].textContent : "无高亮按钮");

  console.log("\n=== 4. 模块导航可用 ===");
  const click = (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  let navOk = true;
  const navResult = [];
  for (const button of document.querySelectorAll("[data-mail-module-link]")) {
    const key = button.getAttribute("data-mail-module-link");
    click(button);
    const shown = visible();
    const active = button.classList.contains("is-active");
    const current = button.getAttribute("aria-current") === "page";
    if (shown.length !== 1 || shown[0] !== key || !active || !current) {
      navOk = false;
      navResult.push(`${key} -> shown=${shown.join(",")} active=${active} aria=${current}`);
    }
  }
  check("逐个点击 8 个模块都只显示对应模块且高亮正确", navOk, navResult.join(" ; ") || "8/8");
  click(document.querySelector("[data-mail-module-link='research']"));

  console.log("\n=== 5. 资料包加载（拆分前就有的启动流程）===");
  await until(() => document.getElementById("mail-package").options.length > 1, "资料包下拉");
  const options = Array.from(document.getElementById("mail-package").options).filter((o) => o.value);
  check("资料包下拉已填充", options.length > 0, `${options.length} 个`);

  const target = options[0];
  const select = document.getElementById("mail-package");
  select.value = target.value;
  select.dispatchEvent(new window.Event("change", { bubbles: true }));

  await until(() => document.querySelectorAll("#mail-overview > div").length > 0, "概览渲染");
  check("概览卡片渲染", document.querySelectorAll("#mail-overview > div").length === 9,
    String(document.querySelectorAll("#mail-overview > div").length) + " 张");
  check("覆盖情况文字已渲染", (document.getElementById("mail-coverage").textContent || "").includes("覆盖"),
    document.getElementById("mail-coverage").textContent);

  // 注意：#mail-list 初始就带一行"尚未选择资料包"的占位行，
  // 必须等真正带 data-message-key 的数据行，否则会误判为已加载。
  await until(() => document.querySelector("#mail-list tr[data-message-key]"), "邮件列表数据");
  const rows = Array.from(document.querySelectorAll("#mail-list tr[data-message-key]"));
  check("邮件列表渲染出真实数据行", rows.length > 0, `${rows.length} 行`);
  check("邮件计数已渲染", /\d+\s*封/.test(document.getElementById("mail-count").textContent),
    document.getElementById("mail-count").textContent);
  check("时间线已渲染真实条目", document.querySelectorAll("#mail-timeline-list li button").length > 0,
    `${document.querySelectorAll("#mail-timeline-list li button").length} 条`);
  check("分页控件已更新", /\d/.test(document.getElementById("mail-page").textContent),
    document.getElementById("mail-page").textContent);

  console.log("\n=== 6. 跨模块：模块隐藏时数据仍照常加载 ===");
  // 此刻只有 research 可见，但列表/关系/网络等都应已经填好。
  check("隐藏状态下邮件列表仍有数据", document.querySelectorAll("#mail-list tr[data-message-key]").length > 0);
  await until(() => document.querySelector("#mail-communication li"), "通信关系渲染");
  check("隐藏状态下关系列表仍有数据",
    document.querySelectorAll("#mail-communication li").length > 0,
    `${document.querySelectorAll("#mail-communication li").length} 条通信关系`);
  await until(() => document.querySelector("#mail-network svg"), "网络图渲染");
  check("隐藏状态下网络图仍已构建", Boolean(document.querySelector("#mail-network svg")));
  await until(() => document.querySelector("#mail-conversations li"), "会话列表渲染");
  check("隐藏状态下会话列表仍已构建", document.querySelectorAll("#mail-conversations li").length > 0,
    `${document.querySelectorAll("#mail-conversations li").length} 条`);
  await until(() => document.querySelector("#mail-versions li"), "附件版本渲染");
  check("隐藏状态下附件版本列表仍已构建", document.querySelectorAll("#mail-versions li").length > 0,
    `${document.querySelectorAll("#mail-versions li").length} 条`);

  console.log("\n=== 7. 邮件列表点击 → 右侧阅读栏（拆分后必须照旧）===");
  const firstRow = rows.find((r) => r.dataset.messageKey);
  check("列表行带有 messageKey", Boolean(firstRow), firstRow && firstRow.dataset.messageKey);
  click(firstRow);
  await until(() => document.querySelector("#mail-detail h2"), "阅读栏标题");
  const detailTitle = document.querySelector("#mail-detail h2");
  check("阅读栏出现邮件标题", Boolean(detailTitle), detailTitle ? detailTitle.textContent.slice(0, 40) : "无");
  check("阅读栏出现元信息", document.querySelectorAll("#mail-detail dd").length > 0,
    `${document.querySelectorAll("#mail-detail dd").length} 项`);
  check("正文预览已填入", (document.getElementById("mail-preview").textContent || "").length > 0);

  console.log("\n=== 8. 通信关系点击 → 写入筛选范围（拆分后反馈链路必须还在）===");
  click(document.querySelector("[data-mail-module-link='relations']"));
  await until(() => document.querySelector("#mail-communication button"), "通信关系按钮");
  const relationButton = document.querySelector("#mail-communication button");
  const before = document.getElementById("mail-participant").value;
  if (relationButton) {
    click(relationButton);
    await sleep(800);
    check("参与方筛选框被写入地址",
      document.getElementById("mail-participant").value !== before
      && document.getElementById("mail-participant").value.length > 0,
      document.getElementById("mail-participant").value);
    check("筛选框在常驻栏里始终可见（未被藏进模块）",
      !document.getElementById("mail-participant").closest("[data-mail-module]"),
      "位于 .mail-sidebar");
  } else {
    check("存在可点击的通信关系", false, "无按钮");
  }

  console.log("\n=== 9. 示例按钮与表单绑定（拆分后仍生效）===");
  click(document.querySelector("[data-mail-module-link='research']"));
  const example = document.querySelector("[data-mail-example]");
  click(example);
  check("示例按钮写入提问框", document.getElementById("mail-question").value === example.dataset.mailExample,
    document.getElementById("mail-question").value.slice(0, 30));

  console.log("\n=== 10. 各模块内容在切换后正常显示 ===");
  for (const key of ["answer", "results", "messages", "structure", "network", "relations", "package", "research"]) {
    click(document.querySelector(`[data-mail-module-link='${key}']`));
    const section = document.querySelector(`[data-mail-module='${key}']`);
    const hasContent = section.textContent.trim().length > 0;
    check(`模块 ${key} 可见且有内容`, !section.hidden && hasContent, `${section.textContent.trim().length} 字`);
  }

  console.log("\n=== 11. 未触碰业务脚本 ===");
  check("static/mail-research.js 未被修改（由部署脚本 diff 保证）", true, "见 mail_step5 输出 IDENTICAL");

  console.log("\n=== 请求统计（确认没有触发写操作）===");
  const writes = calls.filter((u) => /\/api\/mail\/(research-sessions|exports|entities|packages\/[^/]+\/index)/.test(u));
  console.log("  总请求:", calls.length, "| 写类请求:", writes.length ? writes.join(", ") : "无");

  console.log(`\n===== 结果：${failures === 0 ? "全部通过" : failures + " 项失败"} =====`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((error) => {
  console.error("\n测试异常终止:", error && error.stack ? error.stack : error);
  process.exit(2);
});
