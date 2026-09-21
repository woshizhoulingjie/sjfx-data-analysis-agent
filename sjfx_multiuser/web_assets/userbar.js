/* 主界面的"当前用户"入口。
 *
 * 为什么需要它：登录/登出/改密/管理员控制台这些页面**已经能访问**，
 * 但主界面里没有任何可见入口 —— 用户根本不知道它们存在。
 * 功能齐全但没有入口，等于没有。
 *
 * 设计原则：
 *   1. **不碰 app.js**。那是 5280 行的核心文件，改它的风险远大于收益。
 *      这里只**追加**一个浮层，全部逻辑自带。
 *   2. **不抢事件的默认行为**。用固定的右上角小条，不覆盖主界面任何控件。
 *   3. **未登录时直接引导到登录页**。否则用户看到的是一堆 401 报错，
 *      而不是"你需要先登录"。
 *   4. 所有内容用 textContent 写入，不拼 HTML —— 用户名来自服务端，
 *      拼 HTML 就是自己给自己开一个 XSS 口子。
 */
(function () {
  'use strict';

  var API = '/api/auth/me';

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function css() {
    if (document.getElementById('sjfx-userbar-css')) return;
    var s = document.createElement('style');
    s.id = 'sjfx-userbar-css';
    s.textContent = [
      '#sjfx-userbar{position:fixed;top:10px;right:12px;z-index:2147483000;',
      'display:flex;align-items:center;gap:8px;padding:6px 8px 6px 12px;',
      'border-radius:10px;background:rgba(12,18,30,.92);',
      'border:1px solid rgba(120,160,255,.24);color:#e6ecf5;',
      'font:12.5px/1.4 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;',
      'box-shadow:0 8px 24px rgba(0,0,0,.4);backdrop-filter:blur(6px)}',
      '#sjfx-userbar .who{color:#8296b3}',
      '#sjfx-userbar .who b{color:#e6ecf5;font-weight:600}',
      '#sjfx-userbar .tag{font-size:11px;padding:1px 7px;border-radius:20px;',
      'background:rgba(255,196,107,.16);color:#ffc46b}',
      '#sjfx-userbar a,#sjfx-userbar button{color:#9fb0c9;text-decoration:none;',
      'background:transparent;border:0;font:inherit;cursor:pointer;',
      'padding:3px 7px;border-radius:6px}',
      '#sjfx-userbar a:hover,#sjfx-userbar button:hover{color:#e6ecf5;',
      'background:rgba(120,160,255,.12)}',
      '#sjfx-userbar .sep{width:1px;height:14px;background:rgba(120,160,255,.22)}',
      '@media (max-width:560px){#sjfx-userbar .opt{display:none}}'
    ].join('');
    document.head.appendChild(s);
  }

  function build(me) {
    css();
    var bar = el('div');
    bar.id = 'sjfx-userbar';

    var who = el('span', 'who');
    who.appendChild(document.createTextNode('当前用户 '));
    who.appendChild(el('b', null, me.username || '未知'));
    bar.appendChild(who);

    if (me.role === 'admin') {
      bar.appendChild(el('span', 'tag', '管理员'));
      var admin = el('a', 'opt', '控制台');
      admin.href = '/admin';
      bar.appendChild(admin);
    }

    bar.appendChild(el('span', 'sep'));

    var pw = el('a', 'opt', '修改密码');
    pw.href = '/change-password';
    bar.appendChild(pw);

    var out = el('button', null, '退出');
    out.type = 'button';
    out.addEventListener('click', function () {
      out.disabled = true;
      out.textContent = '退出中…';
      fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
        .catch(function () { /* 网络失败也要走：会话可能已经没了 */ })
        .then(function () { window.location.href = '/login'; });
    });
    bar.appendChild(out);

    document.body.appendChild(bar);
  }

  function toLogin() {
    // 只在"确实没登录"时跳，并且用 sessionStorage 防跳转循环
    try {
      if (sessionStorage.getItem('sjfx-authenticated') === '1') return;
    } catch (_) { /* 隐私模式下 sessionStorage 可能不可用，忽略 */ }
    if (location.pathname === '/login' || location.pathname === '/register') return;
    window.location.href = '/login';
  }

  function run() {
    fetch(API, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (r.status === 401) { toLogin(); return null; }
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        var me = data.user || data;
        if (!me || !me.username) { toLogin(); return; }
        try { sessionStorage.setItem('sjfx-authenticated', '1'); } catch (_) {}
        build(me);
      })
      .catch(function () {
        // 服务重启/网络抖动时不打扰用户 —— 主界面自己有重试与提示
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
