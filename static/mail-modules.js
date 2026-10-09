/* Navigation and reader layout; mail data and task contracts stay in their modules. */
(() => {
  "use strict";

  // 顺序即导航顺序。
  const MODULES = ["package", "research", "answer", "results", "messages", "structure", "network", "relations"];
  // 模板中默认可见的模块（其余模块在 HTML 里已带 hidden，避免样式生效前闪一下）。
  const FALLBACK = "package";

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
    const filter = document.querySelector('.mail-sidebar-filter');
    const navigation = document.querySelector('.mail-navigation');
    const mobile = window.matchMedia?.('(max-width:760px)');
    if(filter&&mobile){filter.open=!mobile.matches;mobile.addEventListener?.('change',event=>{filter.open=!event.matches;});}
    if(navigation&&mobile){navigation.open=!mobile.matches;mobile.addEventListener?.('change',event=>{navigation.open=!event.matches;});}
    links().forEach((button) => {
      button.addEventListener("click", () => {
        show(button.getAttribute("data-mail-module-link"));
        const heading = document.querySelector('[data-mail-module]:not([hidden]) .mail-module-head h2');
        if (heading) { heading.tabIndex = -1; heading.focus({preventScroll:true}); }
        if (mobile?.matches) {
          if (navigation) navigation.open = false;
          document.querySelector('.mail-main')?.scrollIntoView({block:'start'});
        }
      });
    });
    const visible = sections().filter((section) => !section.hidden)[0];
    show(visible ? visible.getAttribute("data-mail-module") : FALLBACK);
    document.getElementById('mail-import-shortcut')?.addEventListener('click', () => {
      const input = document.getElementById('mail-source-path');
      document.getElementById('mail-import-form')?.scrollIntoView({block:'start'});
      input?.focus({preventScroll:true});
    });
    document.querySelectorAll('[data-mail-go]').forEach(button => {
      button.addEventListener('click', () => {
        const key = button.getAttribute('data-mail-go');
        document.querySelector(`[data-mail-module-link="${key}"]`)?.click();
        if (!document.getElementById('mail-package')?.value) {
          const notice = document.getElementById('mail-status');
          if (notice) notice.textContent = '先在左侧选择邮件资料包，再查看此功能。';
          document.getElementById('mail-package')?.focus();
        }
      });
    });
    document.querySelectorAll('[data-mail-task]').forEach(button => {
      button.addEventListener('click', async () => {
        const notice = document.getElementById('mail-status');
        if (!document.getElementById('mail-package')?.value) {
          if (notice) notice.textContent = '先选择邮件资料包，再准备分析任务。';
          document.getElementById('mail-package')?.focus();
          return;
        }
        try {
          if (!window.SJFXMailResearch?.startTask) throw new Error('任务入口尚未加载完成，请刷新页面后重试。');
          await window.SJFXMailResearch.startTask({kind:button.dataset.mailTask});
        } catch (error) {
          if (notice) notice.textContent = error.message || '暂时无法打开任务，请稍后重试。';
        }
      });
    });
    document.getElementById('mail-reader-open')?.addEventListener('click', openReader);
    document.getElementById('mail-reader-close')?.addEventListener('click', closeReader);
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !document.querySelector('dialog[open]')) closeReader();
    });
    const refresh = window.setInterval(() => {
      if(document.hidden||document.querySelector('[data-mail-module="package"]')?.hidden)return;
      if(document.querySelector('.mail-stage-list')?.dataset.active!=="true")return;
      if(document.querySelector('.mail-inventory-wrap')?.open)return;
      window.SJFXMailFoundation?.refreshFoundation().catch(()=>{});
    },15000);
    window.addEventListener('pagehide',()=>window.clearInterval(refresh),{once:true});
  }

  let readerTrigger = null;
  function openReader() {
    const reader = document.querySelector('.mail-inspector');
    if (!reader) return;
    if (reader.hidden) readerTrigger = document.activeElement;
    reader.hidden = false;
    document.querySelector('.mail-shell')?.classList.add('reader-open');
    document.getElementById('mail-reader-open')?.setAttribute('aria-expanded', 'true');
    reader.setAttribute('tabindex', '-1');
    reader.focus({preventScroll:true});
  }
  function closeReader() {
    const reader = document.querySelector('.mail-inspector');
    if (!reader || reader.hidden) return;
    reader.hidden = true;
    document.querySelector('.mail-shell')?.classList.remove('reader-open');
    document.getElementById('mail-reader-open')?.setAttribute('aria-expanded', 'false');
    if (readerTrigger?.isConnected) readerTrigger.focus({preventScroll:true});
  }
  window.SJFXMailLayout = {show, openReader, closeReader};

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
