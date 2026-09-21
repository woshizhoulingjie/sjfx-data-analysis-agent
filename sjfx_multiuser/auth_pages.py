"""认证相关页面（注册 / 登录 / 改密 / 提示页）。

## 为什么单独成文件

页面是**要给人看的东西**，跟"鉴权逻辑"是两件事。混在 auth_routes.py 里
会让那个文件又长又难审 —— 而鉴权逻辑是必须能一眼看完的东西。

## 设计约束（都来自这套系统的实际情况）

1. **不能引外部资源**。sjfx 跑在内网（Tailscale），没有 CDN 可达；
   引一个 <link href="https://..."> 就是白屏 + 卡顿。所以 CSS/JS 全内联。
2. **不引前端框架**。主系统是原生 JS，这里也保持一致 ——
   少一个构建步骤，也少一类"框架没加载出来"的故障。
3. **深色工程风**，与主系统一致（主系统是深色）。
4. **移动端可用**。用户实际会在手机上看结果，登录页必须能单手填完。
5. **可访问性**：label 绑定、错误用 aria-live 播报、焦点可见、
   支持 prefers-reduced-motion、支持密码管理器（autocomplete 正确）。

## 成熟度体现在这些细节上

- 密码可见性切换（手机上输错密码是常态）
- 大写锁定提示（"密码明明对却登不上"最常见的原因）
- 注册时实时强度/一致性校验 + 不满足就不让提交
- 提交时禁用按钮 + 转圈（防重复提交产生重复注册）
- 失败后**保留已填的用户名**（只清密码），不让用户重打一遍
- 错误信息用 aria-live 播报，屏幕阅读器也能听到
- 回车提交、自动聚焦、Tab 顺序正确
"""

#: 账号规则（与 users.validate_username 保持一致，改一处要改两处 ——
#: 所以这里只用来做**前端提示**，真正的判定永远在服务端）
USERNAME_HINT = "3–32 位，字母开头，可含字母、数字、下划线、短横线"
PASSWORD_MIN = 8

_BASE_CSS = """
:root{
  color-scheme:dark;
  --bg0:#070a11; --bg1:#0b0f18; --bg2:#151c2b;
  --line:rgba(120,160,255,.18); --line-strong:rgba(120,160,255,.34);
  --fg:#e6ecf5; --fg-dim:#8296b3; --fg-mute:#64748b;
  --accent:#4d7cfe; --accent2:#6b5cff;
  --err:#ff8080; --ok:#5fd39a; --warn:#ffc46b;
  --r:12px;
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; padding:20px 16px 32px;
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  background:radial-gradient(circle at 30% 15%,#1b2740 0%,var(--bg1) 55%,var(--bg0) 100%);
  color:var(--fg);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",
       "PingFang SC","Hiragino Sans GB",sans-serif;
  -webkit-font-smoothing:antialiased;
}
@media (max-height:620px){ body{justify-content:flex-start} }

.card{
  width:100%; max-width:412px;
  padding:32px 28px 24px; border-radius:18px;
  background:rgba(21,28,43,.92);
  border:1px solid var(--line);
  box-shadow:0 24px 64px rgba(0,0,0,.55), 0 0 0 1px rgba(255,255,255,.02) inset;
  backdrop-filter:blur(8px);
}
.brand{display:flex; align-items:center; gap:11px; margin-bottom:6px}
.mark{
  width:34px;height:34px;border-radius:10px;flex:none;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:grid;place-items:center;font-size:16px;
  box-shadow:0 6px 18px rgba(77,124,254,.4);
}
h1{font-size:18px;margin:0;letter-spacing:.2px;font-weight:650}
.sub{color:var(--fg-dim);font-size:12.5px;margin:6px 0 22px;line-height:1.55}

label{display:block;font-size:12.5px;color:#9fb0c9;margin:16px 0 7px;font-weight:550}
.field{position:relative;display:flex;align-items:center}
input{
  width:100%;padding:11px 13px;border-radius:10px;font-size:15px;
  background:#0e1422;border:1px solid #263349;color:var(--fg);
  outline:none;transition:border-color .15s,box-shadow .15s;
  font-family:inherit;
}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(77,124,254,.16)}
input:disabled{opacity:.6}
.field.has-toggle input{padding-right:60px}
.toggle{
  position:absolute;right:6px;top:50%;transform:translateY(-50%);
  border:0;background:transparent;color:var(--fg-dim);
  font-size:12px;padding:7px 10px;border-radius:7px;cursor:pointer;
  font-family:inherit;
}
.toggle:hover{color:var(--fg);background:rgba(120,160,255,.1)}
.toggle:focus-visible{outline:2px solid var(--accent);outline-offset:1px}

.row-between{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.hint{color:var(--fg-mute);font-size:11.5px;margin-top:5px;line-height:1.5}
.hint.bad{color:var(--err)} .hint.good{color:var(--ok)}

/* 密码强度条 */
.meter{display:flex;gap:4px;margin-top:7px}
.meter i{flex:1;height:3px;border-radius:2px;background:#263349;transition:background .2s}
.meter.s1 i:nth-child(1){background:var(--err)}
.meter.s2 i:nth-child(-n+2){background:var(--warn)}
.meter.s3 i:nth-child(-n+3){background:#7ea6ff}
.meter.s4 i{background:var(--ok)}

button.submit{
  width:100%;margin-top:24px;padding:12px;border:0;border-radius:10px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:#fff;font-size:15px;font-weight:640;cursor:pointer;
  font-family:inherit;transition:filter .15s,transform .06s;
}
button.submit:hover:not(:disabled){filter:brightness(1.08)}
button.submit:active:not(:disabled){transform:translateY(1px)}
button.submit:disabled{opacity:.55;cursor:default}
button.submit:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.msg{
  margin-top:14px;font-size:13px;min-height:20px;line-height:1.5;
}
.err{color:var(--err)}
.ok{color:var(--ok)}

.notice{
  margin:0 0 18px;padding:11px 13px;border-radius:10px;font-size:12.5px;
  background:rgba(255,196,107,.1);border:1px solid rgba(255,196,107,.3);
  color:var(--warn);line-height:1.55;
}
.notice b{color:#ffd48a}

.foot{margin-top:20px;font-size:12.5px;color:var(--fg-mute);text-align:center}
.foot a{color:#9fb0c9;text-decoration:none;border-bottom:1px solid transparent}
.foot a:hover{color:var(--fg);border-bottom-color:var(--line-strong)}

kbd{
  font:11.5px ui-monospace,Consolas,monospace;
  background:#0e1422;border:1px solid #263349;border-bottom-width:2px;
  border-radius:5px;padding:1px 5px;color:#9fb0c9;
}
.link-btn{
  display:block;text-align:center;text-decoration:none;margin-top:14px;
  padding:11px;border-radius:10px;font-weight:600;
  background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;
}
.sr{
  position:absolute;width:1px;height:1px;padding:0;margin:-1px;
  overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0;
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important}
}
"""

#: 所有页面共用的少量 JS（密码可见性 + 大写锁定提示）
_COMMON_JS = """
(function () {
  // 密码可见性切换。手机上"输了三遍才发现打错"是最高频的挫败来源。
  document.querySelectorAll('[data-toggle-for]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var el = document.getElementById(btn.getAttribute('data-toggle-for'));
      if (!el) return;
      var show = el.type === 'password';
      el.type = show ? 'text' : 'password';
      btn.textContent = show ? '隐藏' : '显示';
      btn.setAttribute('aria-label', (show ? '隐藏' : '显示') + '密码');
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      el.focus();
    });
  });

  // 大写锁定提示。这是"密码明明对却登不上"最常见的真实原因。
  document.querySelectorAll('input[type=password]').forEach(function (el) {
    function update(e) {
      var on = e.getModifierState && e.getModifierState('CapsLock');
      var tip = document.getElementById(el.id + '-caps');
      if (tip) tip.style.display = on ? 'block' : 'none';
    }
    el.addEventListener('keydown', update);
    el.addEventListener('keyup', update);
    el.addEventListener('blur', function () {
      var tip = document.getElementById(el.id + '-caps');
      if (tip) tip.style.display = 'none';
    });
  });

  // 提交时禁用按钮 —— 防重复提交（重复注册/重复改密）。
  document.querySelectorAll('form[data-guard]').forEach(function (f) {
    f.addEventListener('submit', function () {
      var b = f.querySelector('button.submit');
      if (!b || b.disabled) return;
      if (f.dataset.invalid === '1') return;
      b.disabled = true;
      b.dataset.label = b.textContent;
      b.textContent = '处理中…';
      // 万一被浏览器拦截（比如必填未过），3 秒后恢复，避免按钮永久卡死
      setTimeout(function () {
        if (b.disabled) { b.disabled = false; b.textContent = b.dataset.label || '提交'; }
      }, 3000);
    });
  });
})();
"""


def page(title, body, script=""):
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,'
        'viewport-fit=cover">\n'
        '<meta name="color-scheme" content="dark">\n'
        '<meta name="robots" content="noindex,nofollow">\n'
        '<title>%s · 数据分析智能体</title>\n'
        '<link rel="icon" href="data:image/svg+xml,'
        '%%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 32 32\'%%3E'
        '%%3Crect width=\'32\' height=\'32\' rx=\'8\' fill=\'%%234d7cfe\'/%%3E'
        '%%3Ctext x=\'16\' y=\'23\' font-size=\'19\' text-anchor=\'middle\''
        ' fill=\'white\'%%3E%%E2%%97%%86%%3C/text%%3E%%3C/svg%%3E">\n'
        '<style>%s</style>\n</head>\n<body>\n%s\n<script>%s</script>\n</body>\n</html>\n'
        % (title, _BASE_CSS, body, _COMMON_JS + "\n" + script)
    )


def _err_block(error):
    """错误提示块。

    **必须转义** —— 错误路径是最容易被塞进用户输入的地方
    （"账号 xxx 不存在" 这类消息），不转义就是反射型 XSS。
    当前所有错误文案都是代码里的静态字符串，所以现在不可利用；
    但"现在不可利用"不是不转义的理由 —— 只要将来有一条错误消息
    带上了用户输入，这里就是漏洞。页面自己的输出必须自己负责转义。
    """
    return ('<div class="msg err" id="err" role="alert" aria-live="polite">%s</div>'
            % _escape(error))


def login_page_html(error="", notice="", username=""):
    body = """<form class="card" method="post" action="/login" data-guard>
  <div class="brand"><div class="mark" aria-hidden="true">◆</div><h1>数据分析智能体</h1></div>
  <div class="sub">本地运行 · 数据隔离 · 证据可回溯</div>
  %s
  <label for="u">账号</label>
  <input id="u" name="username" autocomplete="username" autofocus required
         value="%s" spellcheck="false" autocapitalize="none">
  <label for="p">密码</label>
  <div class="field has-toggle">
    <input id="p" name="password" type="password" autocomplete="current-password"
           required aria-describedby="p-caps"
           autocapitalize="none" autocorrect="off" spellcheck="false">
    <button type="button" class="toggle" data-toggle-for="p"
            aria-label="显示密码" aria-pressed="false">显示</button>
  </div>
  <div class="hint" id="p-caps" style="display:none">⚠ 大写锁定已打开</div>
  %s
  <button type="submit" class="submit">登录</button>
  <div class="foot">还没有账号？<a href="/register">注册一个</a></div>
</form>""" % (
        ('<div class="notice">%s</div>' % notice) if notice else "",
        _escape(username),
        _err_block(error),
    )
    return page("登录", body)


def register_page_html(error="", username=""):
    body = """<form class="card" method="post" action="/register" data-guard id="regform">
  <div class="brand"><div class="mark" aria-hidden="true">◆</div><h1>创建账号</h1></div>
  <div class="sub">注册后会得到一个独立的数据空间 —— 你只会看到自己导入的资料，
    别人也看不到你的。</div>
  <label for="u">账号</label>
  <input id="u" name="username" autocomplete="username" autofocus required
         value="%s" spellcheck="false" autocapitalize="none"
         minlength="3" maxlength="32" aria-describedby="u-hint">
  <div class="hint" id="u-hint">%s</div>

  <label for="p">密码</label>
  <div class="field has-toggle">
    <input id="p" name="password" type="password" autocomplete="new-password"
           required minlength="%d" aria-describedby="p-hint p-caps"
           autocapitalize="none" autocorrect="off" spellcheck="false">
    <button type="button" class="toggle" data-toggle-for="p"
            aria-label="显示密码" aria-pressed="false">显示</button>
  </div>
  <div class="meter" id="meter" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
  <div class="hint" id="p-hint">至少 %d 位</div>
  <div class="hint" id="p-caps" style="display:none">⚠ 大写锁定已打开</div>

  <label for="p2">确认密码</label>
  <div class="field has-toggle">
    <input id="p2" name="confirm" type="password" autocomplete="new-password"
           required aria-describedby="p2-hint"
           autocapitalize="none" autocorrect="off" spellcheck="false">
    <button type="button" class="toggle" data-toggle-for="p2"
            aria-label="显示确认密码" aria-pressed="false">显示</button>
  </div>
  <div class="hint" id="p2-hint"></div>

  %s
  <button type="submit" class="submit">创建账号</button>
  <div class="foot">已有账号？<a href="/login">去登录</a></div>
</form>""" % (
        _escape(username),
        USERNAME_HINT,
        PASSWORD_MIN,
        PASSWORD_MIN,
        _err_block(error),
    )
    return page("注册", body, script=_REGISTER_JS)


#: 注册页的实时校验。
#: 目标是"用户提交前就知道哪里不对"，而不是提交后被打回来重填。
#: 注意：这里只是**体验**，真正的判定在服务端（users.validate_username /
#: validate_password）—— 前端校验永远不能当安全措施。
_REGISTER_JS = """
(function () {
  var U = document.getElementById('u'),
      P = document.getElementById('p'),
      P2 = document.getElementById('p2'),
      form = document.getElementById('regform'),
      meter = document.getElementById('meter'),
      uh = document.getElementById('u-hint'),
      ph = document.getElementById('p-hint'),
      p2h = document.getElementById('p2-hint');

  var U_RE = /^[A-Za-z][A-Za-z0-9_-]{2,31}$/;

  function score(pw) {
    if (!pw) return 0;
    var s = 0;
    if (pw.length >= 8) s++;
    if (pw.length >= 12) s++;
    if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
    if (/[0-9]/.test(pw)) s++;
    if (/[^A-Za-z0-9]/.test(pw)) s++;
    return Math.min(4, s);
  }

  function sync() {
    var okU = U_RE.test(U.value.trim());
    uh.className = 'hint' + (U.value && !okU ? ' bad' : (okU ? ' good' : ''));
    uh.textContent = (!U.value ? '%(uh)s'
        : (okU ? '✓ 可以用' : '%(uh)s'));

    var s = score(P.value);
    meter.className = 'meter' + (s ? ' s' + s : '');
    var labels = ['', '太弱', '一般', '不错', '很强'];
    if (!P.value) { ph.className = 'hint'; ph.textContent = '至少 %(min)d 位'; }
    else if (P.value.length < %(min)d) {
      ph.className = 'hint bad'; ph.textContent = '还要 %(min)d 位以上' ;
    } else {
      ph.className = 'hint ' + (s >= 3 ? 'good' : '');
      ph.textContent = '强度：' + labels[s];
    }

    var ok2 = P2.value && P2.value === P.value;
    if (!P2.value) { p2h.className = 'hint'; p2h.textContent = ''; }
    else if (ok2) { p2h.className = 'hint good'; p2h.textContent = '✓ 两次输入一致'; }
    else { p2h.className = 'hint bad'; p2h.textContent = '两次输入不一致'; }

    form.dataset.invalid = (okU && P.value.length >= %(min)d && ok2) ? '0' : '1';
  }

  [U, P, P2].forEach(function (el) { el.addEventListener('input', sync); });
  sync();
})();
""" % {"uh": USERNAME_HINT, "min": PASSWORD_MIN}


def change_password_page_html(error="", forced=False):
    notice = ('<div class="notice"><b>首次登录</b> —— 请把管理员发给你的初始密码'
              '改成只有你自己知道的密码。</div>') if forced else ""
    body = """<form class="card" id="f" data-guard>
  <div class="brand"><div class="mark" aria-hidden="true">◆</div><h1>修改密码</h1></div>
  <div class="sub">改密后其它设备上的登录会一并失效，需要用新密码重新登录。</div>
  %s
  <label for="c">当前密码</label>
  <div class="field has-toggle">
    <input id="c" type="password" autocomplete="current-password" required
           autocapitalize="none" autocorrect="off" spellcheck="false">
    <button type="button" class="toggle" data-toggle-for="c"
            aria-label="显示密码" aria-pressed="false">显示</button>
  </div>
  <label for="n">新密码</label>
  <div class="field has-toggle">
    <input id="n" type="password" autocomplete="new-password" required minlength="%d"
           autocapitalize="none" autocorrect="off" spellcheck="false">
    <button type="button" class="toggle" data-toggle-for="n"
            aria-label="显示新密码" aria-pressed="false">显示</button>
  </div>
  <div class="meter" id="meter" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
  <div class="hint" id="n-hint">至少 %d 位</div>
  <label for="r">确认新密码</label>
  <div class="field has-toggle">
    <input id="r" type="password" autocomplete="new-password" required
           autocapitalize="none" autocorrect="off" spellcheck="false">
    <button type="button" class="toggle" data-toggle-for="r"
            aria-label="显示确认密码" aria-pressed="false">显示</button>
  </div>
  <div class="hint" id="r-hint"></div>
  %s
  <button type="submit" class="submit">确认修改</button>
  <div class="foot"><a href="/logout">退出登录</a></div>
</form>""" % (notice, PASSWORD_MIN, PASSWORD_MIN, _err_block(error))
    return page("修改密码", body, script=_CHANGE_PW_JS)


_CHANGE_PW_JS = """
(function () {
  var f = document.getElementById('f'),
      c = document.getElementById('c'),
      n = document.getElementById('n'),
      r = document.getElementById('r'),
      m = document.getElementById('err'),
      meter = document.getElementById('meter'),
      nh = document.getElementById('n-hint'),
      rh = document.getElementById('r-hint');

  function score(pw) {
    if (!pw) return 0;
    var s = 0;
    if (pw.length >= 8) s++;
    if (pw.length >= 12) s++;
    if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
    if (/[0-9]/.test(pw)) s++;
    if (/[^A-Za-z0-9]/.test(pw)) s++;
    return Math.min(4, s);
  }

  function sync() {
    var s = score(n.value);
    meter.className = 'meter' + (s ? ' s' + s : '');
    if (!n.value) { nh.className = 'hint'; nh.textContent = '至少 %(min)d 位'; }
    else if (n.value.length < %(min)d) {
      nh.className = 'hint bad'; nh.textContent = '还要 %(min)d 位以上';
    } else {
      nh.className = 'hint ' + (s >= 3 ? 'good' : '');
      nh.textContent = '强度：' + ['', '太弱', '一般', '不错', '很强'][s];
    }
    var ok = r.value && r.value === n.value;
    if (!r.value) { rh.className = 'hint'; rh.textContent = ''; }
    else if (ok) { rh.className = 'hint good'; rh.textContent = '✓ 两次输入一致'; }
    else { rh.className = 'hint bad'; rh.textContent = '两次输入不一致'; }
    f.dataset.invalid = (n.value.length >= %(min)d && ok) ? '0' : '1';
  }
  [n, r].forEach(function (el) { el.addEventListener('input', sync); });
  sync();

  f.addEventListener('submit', async function (e) {
    e.preventDefault();
    if (f.dataset.invalid === '1') {
      m.className = 'msg err';
      m.textContent = n.value.length < %(min)d ? '新密码太短' : '两次输入不一致';
      return;
    }
    m.className = 'msg'; m.textContent = '正在修改…';
    try {
      var resp = await fetch('/api/auth/change-password', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        body: JSON.stringify({
          current_password: c.value,
          new_password: n.value,
          confirm_password: r.value
        })
      });
      var data = {};
      try { data = await resp.json(); } catch (_) {}
      if (resp.ok && data.ok) {
        m.className = 'msg ok';
        m.textContent = '已修改，正在跳到登录页…';
        setTimeout(function () { location.href = '/login'; }, 900);
      } else {
        m.className = 'msg err';
        m.textContent = data.error || ('修改失败（HTTP ' + resp.status + '）');
      }
    } catch (err) {
      m.className = 'msg err';
      m.textContent = '网络请求失败，请重试';
    }
  });
})();
""" % {"min": PASSWORD_MIN}


def simple_page(title, heading, message, link_href="/", link_text="返回工作台",
                status_word=""):
    """通用提示页（需要管理员权限 / 请先改密 / 出错等）。

    为什么不直接返回空白 403：给人看的页面要能看懂，并且要给一个**下一步**。
    """
    body = """<div class="card">
  <div class="brand"><div class="mark" aria-hidden="true">◆</div><h1>%s</h1></div>
  <div class="sub">%s</div>
  <a class="link-btn" href="%s">%s</a>
</div>""" % (_escape(heading), _escape(message), link_href, _escape(link_text))
    return page(title, body)


def _escape(text):
    """给 HTML 属性/文本用的最小转义。

    用户名会回填到表单里 —— 不转义就是反射型 XSS。
    （users.validate_username 已经限制了字符集，但**不能依赖**别的模块
    替这里做转义：页面自己的输出必须自己负责。）
    """
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))
