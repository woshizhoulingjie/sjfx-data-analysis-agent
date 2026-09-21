/* 邮件分析：功能模块导航。
 *
 * 这是一个**纯导航层**：只根据 data-mail-module 切换各模块区块的 hidden 属性，
 * 不读取、不修改、不调用 mail-research.js 的任何状态或函数，也不增删任何元素。
 * 页面原有的全部 id、事件绑定与请求流程保持原样，因此拆分成模块不会影响功能。
 *
 * 单独成文件而不是写进 mail-research.js，就是为了让"没有改动业务脚本"这件事
 * 可以被直接验证。
 */
(() => {
  "use strict";

  // 顺序即导航顺序。
  const MODULES = ["package", "research", "answer", "results", "messages", "structure", "network", "relations"];
  // 模板中默认可见的模块（其余模块在 HTML 里已带 hidden，避免样式生效前闪一下）。
  const FALLBACK = "research";

  const sections = () => Array.prototype.slice.call(document.querySelectorAll("[data-mail-module]"));
  const links = () => Array.prototype.slice.call(document.querySelectorAll("[data-mail-module-link]"));

  function show(key) {
    const target = MODULES.indexOf(key) >= 0 ? key : FALLBACK;
    sections().forEach((section) => {
      section.hidden = section.getAttribute("data-mail-module") !== target;
    });
    links().forEach((button) => {
      const active = button.getAttribute("data-mail-module-link") === target;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    // 切换模块后把内容区滚回顶部，否则会停在上一模块的滚动位置。
    const main = document.querySelector(".mail-main");
    if (main) main.scrollTop = 0;
  }

  function bind() {
    links().forEach((button) => {
      button.addEventListener("click", () => show(button.getAttribute("data-mail-module-link")));
    });
    const visible = sections().filter((section) => !section.hidden)[0];
    show(visible ? visible.getAttribute("data-mail-module") : FALLBACK);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
