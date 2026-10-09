// API token is read from the environment; never hardcode real credentials.
// Usage: SJFX_API_TOKEN=xxx node test_mail_modules.js
/* 邮件分析拆分成功能模块后的功能回归验证。
 *
 * 做法：用 jsdom 加载**真实的** templates/mail_research.html，再依次执行
 * 页面按模板顺序加载**真实的** mail UI、foundation、research、modules 与 people 脚本，通过真实 HTTP
 * 打运行中的服务。
 *
 * 目的只有一个：证明拆分是纯结构的，没有影响任何既有功能。
 * 为不打扰生产数据，翻译接口 /api/mail/presentation 被替换为立即完成的桩，
 * 其余接口都是只读 GET。
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = process.env.SJFX_TEST_ROOT || path.resolve(__dirname, '../..');
const BASE = process.env.SJFX_TEST_BASE || "http://127.0.0.1:18000";
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
    url: BASE + "/mail-research",
  });
  const { window } = dom;
  const document = window.document;
  // jsdom does not expose browser Blob URLs; source images are still fetched
  // through the real authenticated endpoint, and never fetched from the email.
  window.URL.createObjectURL = blob => URL.createObjectURL(blob);
  window.URL.revokeObjectURL = value => URL.revokeObjectURL(value);
  window.HTMLElement.prototype.scrollIntoView ||= function () {};

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
  const requestLog = [];
  const messagePayloads = new Map();
  const contentPayloads = new Map();
  window.fetch = async (url, options = {}) => {
    const target = String(url);
    calls.push(target);
    requestLog.push({target,method:(options.method || 'GET').toUpperCase()});
    if (target.indexOf("/api/mail/presentation") === 0) {
      // 桩：不触发真实翻译排队，直接返回已完成。
      return { status: 200, ok: true, json: async () => ({ ok: true, presentation: { status: "completed", values: {}, metadata: {} } }) };
    }
    const headers = { ...(options.headers || {}) };
    const response = await fetch(BASE + target, { ...options, headers });
    const bytes = await response.arrayBuffer();
    const body = Buffer.from(bytes).toString('utf8');
    const json = () => JSON.parse(body);
    if (/\/api\/mail\/packages\/[^/]+\/messages\?/.test(target) && response.ok) {
      const payload=json();(payload.messages?.items || []).forEach(item=>messagePayloads.set(item.message_key,item));
    }
    if (/\/api\/mail\/messages\/[^/]+\/content\?/.test(target) && response.ok) {
      const payload=json();if(payload.content)contentPayloads.set(payload.content.message_key,payload.content);
    }
    return { status: response.status, ok: response.ok, json: async () => json(), blob: async()=>new Blob([bytes],{type:response.headers.get('Content-Type') || 'application/octet-stream'}) };
  };

  // outside-only 不会自动执行 defer；复制模板中五个邮件脚本的依赖顺序。
  // protection/bridge.js 是动态服务路由，不是磁盘静态文件，也不参与邮件资料回归。
  const scripts=Array.from(document.querySelectorAll('script[src]')).map(script=>script.getAttribute('src').split('?')[0].replace(/^\//,'')).filter(source=>/^static\/mail(?:-[\w-]+)?\.js$/.test(source));
  const evalFile = (rel) => window.eval(fs.readFileSync(path.join(ROOT, rel), "utf8"));
  let bootError = null;
  try {
    scripts.forEach(evalFile);
  } catch (error) {
    bootError = error;
  }

  console.log("\n=== 1. 脚本能否正常启动（无 id 缺失导致的中断）===");
  const expectedScripts=['mail-analysis','mail-graph','mail-ui','mail-foundation','mail-research','mail-modules','mail-people'].map(name=>`static/${name}.js`);
  check("模板中的真实邮件脚本依次执行无异常", !bootError && scripts.length===expectedScripts.length && expectedScripts.every(source=>scripts.includes(source)), bootError ? bootError.message : `${scripts.length} 个`);
  check("mail-ui 与 foundation 在业务脚本前加载", Boolean(window.SJFXMailUI && window.SJFXMailFoundation) && scripts.indexOf('static/mail-ui.js') < scripts.indexOf('static/mail-foundation.js') && scripts.indexOf('static/mail-foundation.js') < scripts.indexOf('static/mail-research.js'));
  check("页面无 JS 错误", pageErrors.length === 0, pageErrors.join(" | "));

  console.log("\n=== 2. 模板中的 id 全部就位 ===");
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
  check("默认模块为 package（资料包与处理进度）", visible()[0] === "package", visible()[0]);
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

  const target = process.env.SJFX_TEST_PACKAGE ? options.find(option=>option.value===process.env.SJFX_TEST_PACKAGE) : options[0];
  if(!target)throw new Error('指定测试资料包不存在');
  const select = document.getElementById("mail-package");
  select.value = target.value;
  select.dispatchEvent(new window.Event("change", { bubbles: true }));

  await until(() => document.querySelectorAll("#mail-overview > div").length > 0, "概览渲染");
  check("概览卡片渲染", document.querySelectorAll("#mail-overview > div").length === 5,
    String(document.querySelectorAll("#mail-overview > div").length) + " 张");
  check("概览保留唯一邮件、来源、正文、附件与邮箱入口",Array.from(document.querySelectorAll('#mail-overview > div > button')).map(button=>button.textContent).join(' ').includes('唯一邮件数')&&['来源副本','正文可读','附件','参与邮箱'].every(label=>document.getElementById('mail-overview').textContent.includes(label)));
  check("覆盖情况文字已渲染", (document.getElementById("mail-coverage").textContent || "").includes("覆盖"),
    document.getElementById("mail-coverage").textContent);

  // 注意：#mail-list 初始就带一行"尚未选择资料包"的占位行，
  // 必须等真正带 data-message-key 的数据行，否则会误判为已加载。
  await until(() => document.querySelector("#mail-list tr[data-message-key]"), "邮件列表数据");
  const rows = Array.from(document.querySelectorAll("#mail-list tr[data-message-key]"));
  check("邮件列表渲染出真实数据行", rows.length > 0, `${rows.length} 行`);
  check("每封邮件包含五列及独立处理状态", rows.every(row=>row.cells.length===5 && row.cells[4].textContent.trim()),`${rows.length} 行`);
  check("处理状态与真实 API 保存和缺口指标一致",rows.every(row=>{const item=messagePayloads.get(row.dataset.messageKey),text=row.cells[4].textContent;if(!item)return false;return (item.original_preserved!==1||text.includes('原件已保存'))&&(item.original_preserved!==0||text.includes('原件未保留'))&&(!item.body_empty||text.includes('正文为空'))&&(!item.date_unknown||text.includes('发送时间未知'))&&(!item.attachment_incomplete||text.includes(`${item.attachment_incomplete} 个附件文字未完整`));}));
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
  await until(()=>document.querySelector('#mail-foundation-reader h3'),'完整内容阅读栏');
  check("完整原文与原文件下载入口已渲染",Boolean(document.querySelector('#mail-foundation-reader h3'))&&Array.from(document.querySelectorAll('#mail-foundation-reader button')).some(button=>button.textContent==='下载原始邮件'));
  const originalContent=contentPayloads.get(firstRow.dataset.messageKey),readerText=document.getElementById('mail-foundation-reader').textContent;
  check("阅读投影与附件缺口提示对应真实处理状态",Boolean(originalContent)&&(originalContent.reading_projection_complete!==false||originalContent.raw_available===false||readerText.includes('部分阅读文字解码不完整'))&&(originalContent.body_status!=='empty'||readerText.includes('本邮件正文为空'))&&(originalContent.attachment_text_complete!==false||readerText.includes('部分附件文字尚未完整抽取')));

  console.log("\n=== 8. 通信关系点击 → 写入筛选范围（拆分后反馈链路必须还在）===");
  click(document.querySelector("[data-mail-module-link='relations']"));
  await until(() => document.querySelector("#mail-communication button"), "通信关系按钮");
  const relationButton = document.querySelector("#mail-communication button");
  const before = document.getElementById("mail-participant").value;
  if (relationButton) {
    const requestStart=requestLog.length;
    click(relationButton);
    await until(()=>requestLog.slice(requestStart).some(request=>request.target.includes('/messages?')),'关系对象筛选请求');
    check("参与方筛选框被写入地址",
      document.getElementById("mail-participant").value !== before
      && document.getElementById("mail-participant").value.length > 0,
      document.getElementById("mail-participant").value);
    const filtered=requestLog.slice(requestStart).find(request=>request.target.includes('/messages?'));
    check("关系对象点击后的邮件请求使用该参与邮箱",new URL(filtered.target,BASE).searchParams.get('participant')===document.getElementById('mail-participant').value);
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

  console.log("\n=== 11. 业务脚本和依赖完整加载 ===");
  check("执行的是模板引用的真实业务脚本",scripts.includes('static/mail-research.js')&&Boolean(window.SJFXMailReader));
  check("浏览完成后没有脚本错误",pageErrors.length===0,pageErrors.join(' | '));

  console.log("\n=== 请求统计（确认没有触发写操作）===");
  const writes = requestLog.filter(request=>request.method!=='GET'&&!request.target.startsWith('/api/mail/presentation'));
  check("只有只读请求访问真实服务",writes.length===0);
  console.log("  总请求:", calls.length, "| 写类请求:", writes.length ? writes.map(request=>request.method+' '+request.target).join(', ') : "无");

  console.log(`\n===== 结果：${failures === 0 ? "全部通过" : failures + " 项失败"} =====`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((error) => {
  console.error("\n测试异常终止:", error && error.stack ? error.stack : error);
  process.exit(2);
});
