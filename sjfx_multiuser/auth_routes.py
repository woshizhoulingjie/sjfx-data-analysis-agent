"""认证与管理路由：注册 / 登录 / 登出 / 改密 / 管理员控制台。

对应设计方案 §6 与 §7。**只新增 HTTP 面，不改任何既有路由。**

## 框架无关（重要）

**曾经踩的坑**：最初这文件是 `from flask import request, jsonify, redirect` 写的。
后来读 `web_compat.py` 才发现 sjfx 的真实 HTTP 层是 **FastAPI + 窄兼容层**：
没有 `request.cookies`、没有 `redirect`、没有表单解析、没有 `after_request`、没有 `g`。

所以现在全部通过 `http_adapter.HttpAdapter` 与 `runtime`：
  - 读 cookie     → `adapter.cookie()`（从 `Cookie` 头手工解析）
  - 读请求体      → `adapter.form_body()`（含 urlencoded 手工解析）
  - 造响应        → `adapter.json/html/redirect()`
  - 传递当前用户  → `runtime.set_current_user()`（contextvars，框架无关）

同一份代码在 FastAPI（生产）与 Flask（本地测试）下都能跑。

## 安全要点（每条都有对应测试）

1. 登录失败**不区分**"账号不存在"与"密码错误" → 防账号枚举
2. **注册不开放管理员角色** → 自助注册只能得到 user
3. 管理员**不能停用自己** → 防把自己锁在门外
4. **不能停用最后一个管理员** → 防系统失去管理入口
5. 改密后**作废该用户全部会话** → 否则改密赶不走已进来的人
6. 视角只存**服务端会话**，且只接受管理员设置 → 防越权
7. 注册限流 → 防脚本批量注册
8. 首次登录**强制改密** → 否则管理员知道所有人的密码
"""

import os
import sqlite3

from . import runtime
from .http_adapter import (
    SESSION_COOKIE,
    build_clear_cookie,
    build_set_cookie,
    make_adapter,
)
from .ownership import ROLE_ADMIN, ROLE_USER, validate_view_target
from .users import STATUS_ACTIVE, STATUS_DISABLED, AuthError, UserStore

#: 每个 IP 每小时允许的注册次数
REGISTER_LIMIT_PER_HOUR = 3

#: 默认适配器（按环境自动选）。测试可注入自己的。
_DEFAULT_ADAPTER = None


def _adapter():
    global _DEFAULT_ADAPTER
    if _DEFAULT_ADAPTER is None:
        _DEFAULT_ADAPTER, _kind = make_adapter()
    return _DEFAULT_ADAPTER


# --------------------------------------------------------------------------
# 页面（实现在 auth_pages.py）
# --------------------------------------------------------------------------
#
# 页面之所以单独成文件：它是"给人看的东西"，跟"鉴权逻辑"是两件事。
# 混在一起会让这个文件又长又难审 —— 而鉴权逻辑是必须能一眼看完的东西。
#
# 这里保留同名函数（下面几行 import + 兼容别名），是为了**不动**本文件里
# 已有的调用点。改调用点是纯风险，没有收益。

from .auth_pages import (  # noqa: E402
    PASSWORD_MIN,
    change_password_page_html,
    login_page_html,
    page as _page,
    register_page_html,
    simple_page,
)


# --------------------------------------------------------------------------
# 管理员控制台（单页，前端调已有 /api/admin/* 接口）
# --------------------------------------------------------------------------

_ADMIN_EXTRA_CSS = """
  body { align-items: flex-start; }
  .wrap { width: min(1180px, 94vw); padding: 24px 0 60px; }
  .topbar { display: flex; align-items: center; gap: 14px; padding: 12px 18px;
    border-radius: 14px; background: rgba(21,28,43,.9);
    border: 1px solid rgba(120,160,255,.18); margin-bottom: 18px; }
  .topbar .brand { margin: 0; }
  .topbar .spacer { flex: 1; }
  .who { color: #8296b3; font-size: 12.5px; }
  .who b { color: #e6ecf5; }
  .role { font-size: 11px; padding: 2px 8px; border-radius: 20px; margin-left: 8px;
    background: rgba(255,196,107,.16); color: #ffc46b; }
  .panel { background: rgba(21,28,43,.92); border: 1px solid rgba(120,160,255,.18);
    border-radius: 14px; padding: 20px; margin-bottom: 16px; }
  .panel h2 { font-size: 14px; margin: 0 0 4px; }
  .panel .sub { color: #8296b3; font-size: 12px; margin: 0 0 16px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 9px 10px; text-align: left; border-bottom: 1px solid #1e2739; }
  th { color: #8296b3; font-size: 11.5px; font-weight: 600; }
  tr:hover td { background: rgba(120,160,255,.05); }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px;
    background: rgba(120,160,255,.13); color: #b9cdfd; }
  .tag.admin { background: rgba(255,196,107,.16); color: #ffc46b; }
  .tag.off { background: rgba(130,150,179,.18); color: #8296b3; }
  .tag.on { background: rgba(95,211,154,.14); color: #5fd39a; }
  .btn { padding: 6px 12px; border-radius: 8px; font-size: 12.5px; cursor: pointer;
    border: 1px solid #263349; background: #0e1422; color: #e6ecf5; }
  .btn:hover { border-color: #4d7cfe; }
  .btn.primary { border: 0; background: linear-gradient(135deg,#4d7cfe,#6b5cff); color: #fff; font-weight: 600; }
  .btn.danger { border-color: rgba(255,128,128,.4); color: #ff8080; }
  .btn:disabled { opacity: .45; cursor: default; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px,1fr)); gap: 12px; }
  .stat { background: #0e1422; border: 1px solid #263349; border-radius: 10px; padding: 12px 14px; }
  .stat span { display: block; font-size: 11.5px; color: #8296b3; margin-bottom: 4px; }
  .stat b { font-size: 18px; }
  .msg { margin-top: 12px; font-size: 12.5px; min-height: 18px; }
  .err { color: #ff8080; }
  .ok { color: #5fd39a; }
  .hint { color: #64748b; font-size: 11.5px; margin-top: 6px; }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; color: #8296b3; }
"""


def admin_page_html():
    """管理员控制台。纯前端调已有的 /api/admin/* 接口，不引入新后端逻辑。"""
    body = """<div class="wrap">
  <div class="topbar">
    <div class="brand"><div class="mark">◆</div><h1>管理员控制台</h1></div>
    <div class="spacer"></div>
    <span class="who">当前用户：<b id="who">…</b><span class="role" id="roleTag">管理员</span></span>
    <a class="btn" href="/">返回工作台</a>
    <button class="btn" id="logoutBtn" type="button">退出</button>
  </div>

  <div class="panel">
    <h2>数据视角</h2>
    <p class="sub">切换后，工作台里看到的数据包与历史记录会变成该用户的范围。存在服务端会话里，改不了。</p>
    <div class="row">
      <span class="who">当前视角：<b id="curView">…</b></span>
      <select id="viewSelect" style="padding:8px 10px;border-radius:8px;background:#0e1422;border:1px solid #263349;color:#e6ecf5;">
        <option value="">全部用户（管理员默认）</option>
      </select>
      <button class="btn primary" id="applyView" type="button">切换视角</button>
    </div>
    <div class="msg" id="viewMsg"></div>
  </div>

  <div class="panel">
    <h2>用户与数据量</h2>
    <p class="sub">每个用户有独立的数据空间。停用后该用户立即无法登录，已有数据保留。</p>
    <div class="grid" id="stats"></div>
  </div>

  <div class="panel">
    <h2>用户列表</h2>
    <p class="sub">账号名不区分大小写；归属空间（owner）注册后不可更改，改了会找不到自己的历史数据。</p>
    <div class="row" style="margin-bottom:12px;">
      <button class="btn primary" id="refreshBtn" type="button">刷新</button>
      <button class="btn" id="newUserBtn" type="button">创建账号</button>
    </div>
    <div style="overflow:auto;">
      <table>
        <thead><tr>
          <th>账号</th><th>显示名</th><th>角色</th><th>状态</th>
          <th>归属空间</th><th>最后登录</th><th>操作</th>
        </tr></thead>
        <tbody id="userRows"><tr><td colspan="7" class="hint">加载中…</td></tr></tbody>
      </table>
    </div>
    <div class="msg" id="userMsg"></div>
  </div>
</div>

<script>
function $(i){return document.getElementById(i);}
function esc(t){return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function say(el, text, cls){ var e=$(el); e.className='msg '+(cls||''); e.textContent=text||''; }

async function api(path, opts){
  var r = await fetch(path, Object.assign({headers:{'Accept':'application/json'}}, opts||{}));
  if (r.status === 401) { location.href='/login'; throw new Error('未登录'); }
  var d = null; try { d = await r.json(); } catch(e) { d = null; }
  return {status: r.status, data: d};
}

var ME = null, USERS = [];

async function loadMe(){
  var r = await api('/api/auth/me');
  if (!r.data || !r.data.ok) { location.href='/login'; return false; }
  ME = r.data.user;
  $('who').textContent = ME.display_name || ME.username;
  if (ME.role !== 'admin') {
    document.querySelector('.wrap').innerHTML =
      '<div class="panel"><h2>需要管理员权限</h2><p class="sub">当前账号不是管理员。</p>' +
      '<a class="btn" href="/">返回工作台</a></div>';
    return false;
  }
  if (ME.must_change_pw) { location.href='/change-password'; return false; }
  return true;
}

async function loadUsers(){
  var r = await api('/api/admin/users');
  if (!r.data || !r.data.ok) { say('userMsg', '加载失败：' + (r.data&&r.data.error||r.status), 'err'); return; }
  USERS = r.data.users || [];
  var rows = USERS.map(function(u){
    var isMe = u.user_id === ME.user_id;
    var tag = u.role === 'admin' ? '<span class="tag admin">管理员</span>' : '<span class="tag">普通用户</span>';
    var st  = u.status === 'active' ? '<span class="tag on">正常</span>' : '<span class="tag off">已停用</span>';
    var acts = [];
    acts.push('<button class="btn" data-act="reset" data-id="'+u.user_id+'" data-name="'+esc(u.username)+'">重置密码</button>');
    if (isMe) {
      acts.push('<span class="hint">当前账号</span>');
    } else if (u.status === 'active') {
      acts.push('<button class="btn danger" data-act="disable" data-id="'+u.user_id+'" data-name="'+esc(u.username)+'">停用</button>');
    } else {
      acts.push('<button class="btn" data-act="enable" data-id="'+u.user_id+'">启用</button>');
    }
    return '<tr><td><b>'+esc(u.username)+'</b></td><td>'+esc(u.display_name)+'</td><td>'+tag+'</td><td>'+st+'</td>' +
      '<td class="mono">'+esc(u.owner_id)+'</td><td class="hint">'+esc(u.last_login_utc||'从未登录')+'</td>' +
      '<td><div class="row">'+acts.join(' ')+'</div></td></tr>';
  }).join('');
  $('userRows').innerHTML = rows || '<tr><td colspan="7" class="hint">没有用户</td></tr>';

  var active = USERS.filter(function(u){return u.status==='active';}).length;
  var admins = USERS.filter(function(u){return u.role==='admin' && u.status==='active';}).length;
  $('stats').innerHTML =
    '<div class="stat"><span>账号总数</span><b>'+USERS.length+'</b></div>' +
    '<div class="stat"><span>活跃账号</span><b>'+active+'</b></div>' +
    '<div class="stat"><span>活跃管理员</span><b>'+admins+'</b></div>' +
    '<div class="stat"><span>数据空间</span><b>'+new Set(USERS.map(function(u){return u.owner_id;})).size+'</b></div>';

  var sel = $('viewSelect');
  var cur = sel.value;
  sel.innerHTML = '<option value="">全部用户（管理员默认）</option>' +
    USERS.map(function(u){
      return '<option value="'+esc(u.owner_id)+'">'+esc(u.display_name||u.username)+'（'+esc(u.username)+'）</option>';
    }).join('');
  sel.value = cur;
}

async function loadView(){
  var r = await api('/api/admin/view-as');
  if (!r.data || !r.data.ok) return;
  var v = r.data.viewing_owner;
  $('curView').textContent = v ? v : '全部用户';
  $('viewSelect').value = v || '';
}

async function applyView(){
  var v = $('viewSelect').value || null;
  var r = await api('/api/admin/view-as', {method:'POST', headers:{'Content-Type':'application/json'},
                                           body: JSON.stringify({owner_id: v})});
  if (r.status === 200 && r.data && r.data.ok) {
    say('viewMsg', '已切换到：' + (v || '全部用户'), 'ok');
    await loadView();
  } else {
    say('viewMsg', '切换失败：' + ((r.data&&r.data.error) || r.status), 'err');
  }
}

async function resetPassword(id, name){
  var pw = prompt('为「' + name + '」设置新密码（至少 8 位）。\\n该用户下次登录会被要求改密。');
  if (!pw) return;
  var r = await api('/api/admin/users/' + encodeURIComponent(id) + '/password',
                    {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify({password: pw})});
  if (r.status === 200 && r.data && r.data.ok) say('userMsg', '已重置「'+name+'」的密码，并作废其现有会话', 'ok');
  else say('userMsg', '重置失败：' + ((r.data&&r.data.error) || r.status), 'err');
}

async function setStatus(id, status, name){
  if (status === 'disabled' && !confirm('确定停用「' + name + '」？\\n该用户将立即无法登录，已有数据保留。')) return;
  var r = await api('/api/admin/users/' + encodeURIComponent(id),
                    {method:'DELETE', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify({status: status})});
  if (r.status === 200 && r.data && r.data.ok) {
    say('userMsg', (status==='disabled'?'已停用 ':'已启用 ') + name, 'ok');
    await loadUsers();
  } else {
    say('userMsg', '操作失败：' + ((r.data&&r.data.error) || r.status), 'err');
  }
}

async function createUser(){
  var name = prompt('新账号名（3-32 位，字母开头）：');
  if (!name) return;
  var pw = prompt('为「' + name + '」设置初始密码（至少 8 位）：');
  if (!pw) return;
  var role = confirm('设为管理员吗？\\n确定=管理员，取消=普通用户') ? 'admin' : 'user';
  var r = await api('/api/admin/users', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify({username:name, password:pw, role:role, must_change_pw:true})});
  if (r.status === 200 && r.data && r.data.ok) {
    say('userMsg', '已创建 ' + name + '（' + role + '），归属空间 ' + r.data.owner_id, 'ok');
    await loadUsers();
  } else {
    say('userMsg', '创建失败：' + ((r.data&&r.data.error) || r.status), 'err');
  }
}

document.addEventListener('click', function(e){
  var b = e.target.closest('[data-act]');
  if (!b) return;
  var id = b.dataset.id, name = b.dataset.name, act = b.dataset.act;
  if (act === 'reset') resetPassword(id, name);
  else if (act === 'disable') setStatus(id, 'disabled', name);
  else if (act === 'enable') setStatus(id, 'active', name);
});

$('applyView').addEventListener('click', applyView);
$('refreshBtn').addEventListener('click', function(){ loadUsers(); loadView(); });
$('newUserBtn').addEventListener('click', createUser);
$('logoutBtn').addEventListener('click', async function(){
  await fetch('/api/auth/logout', {method:'POST'}); location.href='/login';
});

(async function(){
  if (!await loadMe()) return;
  await loadUsers();
  await loadView();
})();
</script>"""
    return _page("管理员控制台", body).replace("</style>", _ADMIN_EXTRA_CSS + "</style>")


# --------------------------------------------------------------------------
# 路由注册
# --------------------------------------------------------------------------

def register(app, user_store, config_owner_id=None, adapter=None,
             register_limit_per_hour=REGISTER_LIMIT_PER_HOUR):
    """把认证路由挂到 app 上。

    参数
    ----
    app : 支持 `@app.route(path, methods=[...])` 的框架应用
        （sjfx 的 `SJFXFastAPI` 或 Flask 都行）
    user_store : UserStore
    config_owner_id : str
        首个管理员的 owner_id（= 现有数据的归属，保证免迁移）
    adapter : HttpAdapter
        不传则按环境自动选
    register_limit_per_hour : int
        每个 IP 每小时可注册次数；0 表示不限流

    返回 dict，供归属解析复用。
    """
    ad = adapter or _adapter()

    # ---------------- 辅助 ----------------

    def _session():
        sid = ad.cookie(SESSION_COOKIE)
        return user_store.resolve_session(sid) if sid else None

    def _session_id():
        return ad.cookie(SESSION_COOKIE)

    def _publish_actor(sess):
        """把"当前已认证用户"交给归属解析层。

        **为什么必须有这一步**：会话 cookie 是本响应才设上的（登录），
        或已在本响应被清掉（改密）—— 两种情况下归属解析都读不到用户，
        结果就是"成功的登录/改密"没有主体，或（更糟）回落到配置 owner。
        """
        runtime.set_current_user({
            "user_id": sess["user_id"],
            "username": sess["username"],
            "role": sess["role"],
            "owner_id": sess["owner_id"],
        })

    def _require_user():
        s = _session()
        if not s:
            return None, ad.json({"ok": False, "error": "未登录"}, 401)
        return s, None

    def _require_admin():
        s, err = _require_user()
        if err:
            return None, err
        if s.get("role") != ROLE_ADMIN:
            return None, ad.json({"ok": False, "error": "需要管理员权限"}, 403)
        return s, None

    def _set_cookie_header(session_id):
        return build_set_cookie(SESSION_COOKIE, session_id)

    # ---------------- 强制改密守卫（服务端）----------------

    #: 强制改密状态下**仍然允许**访问的路径。
    #: 少一条就会出现"进得去出不来"，多一条就是一个绕过口子。
    _PW_CHANGE_ALLOWED = frozenset([
        "/change-password",            # 改密页面本身
        "/api/auth/change-password",   # 改密接口
        "/api/auth/me",                # 前端要知道自己是谁
        "/logout",                     # 必须能退出（换账号）
        "/api/auth/logout",
    ])

    #: 静态资产前缀（守卫对这些一律放行）。
    #: `/auth-static/` 是本模块自己的资产（主界面的用户入口脚本）——
    #: 它只是 JS 文件，不含任何数据，放行不构成信息泄露。
    _ASSET_PREFIXES = ("/static/", "/auth-static/")

    def _req_path():
        try:
            import web_compat as _wc
            return _wc.request.path
        except Exception:  # noqa: BLE001
            try:
                from flask import request as _fr
                return _fr.path
            except Exception:  # noqa: BLE001
                return ""

    @app.before_request
    def _enforce_password_change():
        """强制改密：**在服务端拦住**，不只靠前端跳转。

        为什么必须有这一层：`must_change_pw` 是"管理员发的临时密码"这个
        流程的全部安全依据 —— 管理员知道初始密码，用户首次登录必须改掉它，
        否则管理员就能一直以该用户身份登录。

        早先的实现只在**登录那一刻**决定跳到哪个页面，之后没有任何校验：
        用户在地址栏敲 `/` 就能直接进入系统、调所有 API，强制改密形同虚设。
        所以这里对"未改密"的会话一刀切 —— 除了上表那几个必要路径，一律拦。

        另一条也更重要的保证：那些**只有管理员能做的写操作**
        （`/api/admin/*` 全是写）也在这里被挡住，不依赖各处自己记得检查。
        """
        s = _session()
        if not s or not s.get("must_change_pw"):
            return None
        path = _req_path()
        if path in _PW_CHANGE_ALLOWED:
            return None
        if any(path.startswith(p) for p in _ASSET_PREFIXES):
            return None
        if path.startswith("/api/"):
            return ad.json({"ok": False,
                            "error": "首次登录必须先修改初始密码",
                            "must_change_pw": True}, 403)
        # 页面请求：跳到改密页。不同框架重定向能力不一，所以除了 Location
        # 还给一个可点的链接（拿不到 Location 的客户端也能自己走）。
        return ad.html(
            _page("请先修改密码", """
<div class="card"><h1>请先修改初始密码</h1>
<div class="sub">为了账号安全，首次登录必须设置自己的密码后才能使用系统。</div>
<a href="/change-password" style="display:block;text-align:center;text-decoration:none;
   padding:11px;border-radius:9px;background:linear-gradient(135deg,#4d7cfe,#6b5cff);
   color:#fff;font-weight:600;margin-top:6px;">去修改密码</a>
<div class="foot"><a href="/logout">退出登录</a></div></div>"""), 403)

    # ---------------- 页面 ----------------

    @app.route("/login", methods=["GET"])
    def sjfx_login_page():
        if _session():
            return ad.redirect("/")
        return ad.html(login_page_html())

    @app.route("/register", methods=["GET"])
    def sjfx_register_page():
        if _session():
            return ad.redirect("/")
        return ad.html(register_page_html())

    @app.route("/change-password", methods=["GET"])
    def sjfx_change_pw_page():
        if not _session():
            return ad.redirect("/login")
        return ad.html(change_password_page_html(
            forced=bool((_session() or {}).get("must_change_pw"))))

    @app.route("/auth-static/userbar.js", methods=["GET"])
    def sjfx_userbar_js():
        """主界面的"当前用户"入口脚本。

        为什么自带一个路由而不是放进 /static/：
        1. 模块要能独立部署 —— 不用去动 sjfx 的 static 目录（那里的文件名
           都带版本号和 .bak 历史，往里塞东西容易撞名）
        2. `/static/` 由主应用伺服，而这份脚本属于多用户模块
        3. 用独立的 `/auth-static/` 前缀，守卫里放行它就不会牵连别的东西
        """
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "web_assets", "userbar.js")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            return ad.json({"ok": False, "error": "脚本缺失"}, 404)
        # 显式给 JS 的 content-type：适配层默认是 html
        return ad.html(body, mime="application/javascript")

    @app.route("/admin", methods=["GET"])
    def sjfx_admin_page():
        """管理员控制台页面。

        页面本身不渲染任何数据 —— 它只是前端壳，数据全部通过
        `/api/admin/*` 接口取（那些接口有独立的鉴权）。
        所以即使有人绕过页面直接访问接口，也拿不到别人的数据。
        """
        s = _session()
        if not s:
            return ad.redirect("/login")
        if s.get("role") != ROLE_ADMIN:
            # 不用 403 空白页：给人看的页面要能看懂
            return ad.html(_page("需要管理员权限", """
<div class="card"><h1>需要管理员权限</h1>
<div class="sub">当前账号不是管理员，无法进入控制台。</div>
<a class="btn" href="/" style="display:block;text-align:center;text-decoration:none;
   padding:11px;border-radius:9px;background:linear-gradient(135deg,#4d7cfe,#6b5cff);
   color:#fff;font-weight:600;">返回工作台</a></div>"""), 403)
        return ad.html(admin_page_html())

    # ---------------- 注册 ----------------

    @app.route("/register", methods=["POST"])
    def sjfx_register_submit():
        ip = ad.remote_addr()
        if register_limit_per_hour and \
                user_store.register_attempts_recent(ip, minutes=60) >= register_limit_per_hour:
            return ad.html(register_page_html(
                "注册过于频繁，请稍后再试（每小时最多 %d 次）" % register_limit_per_hour),
                429)

        form = ad.form_body()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        confirm = form.get("confirm") or form.get("confirm_password") or password

        user_store.record_register_attempt(ip)

        if password != confirm:
            return ad.html(register_page_html("两次输入的密码不一致",
                                             username=username), 400)
        try:
            # 关键：自助注册**只能**得到 user 角色 —— 绝不接受客户端指定的 role
            created = user_store.create_user(username, password,
                                             display_name=username,
                                             role=ROLE_USER, must_change_pw=False)
        except AuthError as exc:
            # 失败时把已填的账号带回去 —— 不让用户重打一遍
            return ad.html(register_page_html(str(exc), username=username), 400)
        except sqlite3.IntegrityError:
            return ad.html(register_page_html("账号已存在", username=username), 400)

        result = user_store.login(created["username"], password,
                                  machine_fp=_fingerprint())
        _publish_actor(result["user"])
        return ad.redirect("/", set_cookie=_set_cookie_header(result["session_id"]))

    # ---------------- 登录 / 登出 ----------------

    @app.route("/login", methods=["POST"])
    def sjfx_login_submit():
        form = ad.form_body()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        try:
            result = user_store.login(username, password, machine_fp=_fingerprint())
        except AuthError:
            # 统一话术 —— 不区分"账号不存在"与"密码错误"（防账号枚举）。
            # 回填用户名：失败后清空账号输入框是移动端最烦人的体验之一。
            return ad.html(login_page_html("账号或密码错误",
                                           username=username), 401)

        _publish_actor(result["user"])
        target = "/change-password" if result["must_change_pw"] else "/"
        return ad.redirect(target, set_cookie=_set_cookie_header(result["session_id"]))

    @app.route("/logout", methods=["GET", "POST"])
    def sjfx_logout_page():
        sid = _session_id()
        if sid:
            user_store.logout(sid)
        return ad.redirect("/login", clear_cookie=build_clear_cookie(SESSION_COOKIE))

    @app.route("/api/auth/logout", methods=["POST"])
    def sjfx_logout_api():
        sid = _session_id()
        if sid:
            user_store.logout(sid)
        return ad.json({"ok": True}, clear_cookie=build_clear_cookie(SESSION_COOKIE))

    @app.route("/api/auth/me", methods=["GET"])
    def sjfx_me():
        s = _session()
        if not s:
            return ad.json({"ok": False, "error": "未登录"}, 401)
        return ad.json({"ok": True, "user": {
            "user_id": s["user_id"], "username": s["username"],
            "display_name": s["display_name"], "role": s["role"],
            "owner_id": s["owner_id"],
            "must_change_pw": bool(s.get("must_change_pw")),
        }})

    # ---------------- 改密 ----------------

    @app.route("/api/auth/change-password", methods=["POST"])
    def sjfx_change_password():
        s = _session()
        if not s:
            return ad.json({"ok": False, "error": "未登录"}, 401)
        body = ad.form_body()
        current = body.get("current_password") or ""
        new = body.get("new_password") or ""
        confirm = body.get("confirm_password") or ""

        if not user_store.authenticate(s["username"], current):
            return ad.json({"ok": False, "error": "当前密码不正确"}, 400)
        if new != confirm:
            return ad.json({"ok": False, "error": "两次输入的新密码不一致"}, 400)
        try:
            user_store.set_password(s["user_id"], new, must_change_pw=False)
        except AuthError as exc:
            return ad.json({"ok": False, "error": str(exc)}, 400)

        # 必须在作废**之前**发布身份 —— 会话一旦作废、cookie 一旦清掉，
        # 归属解析就只能拿到匿名（或更糟：回落到配置 owner）
        _publish_actor(s)
        user_store.revoke_user_sessions(s["user_id"])
        return ad.json({"ok": True, "relogin": True},
                       clear_cookie=build_clear_cookie(SESSION_COOKIE))

    # ---------------- 管理员：视角 ----------------

    @app.route("/api/admin/view-as", methods=["GET"])
    def sjfx_view_as_get():
        s, err = _require_admin()
        if err:
            return err
        return ad.json({"ok": True, "viewing_owner": s.get("viewing_owner"),
                        "own_owner": s["owner_id"]})

    @app.route("/api/admin/view-as", methods=["POST"])
    def sjfx_view_as_set():
        s, err = _require_admin()
        if err:
            return err
        body = ad.form_body()
        target = body.get("owner_id") or None
        ok, verr = validate_view_target(
            s, target, lambda oid: user_store.get_by_owner(oid) is not None)
        if not ok:
            return ad.json({"ok": False, "error": verr},
                           403 if "管理员" in verr else 400)
        user_store.set_viewing_owner(s["session_id"], target)
        return ad.json({"ok": True, "viewing_owner": target})

    # ---------------- 管理员：用户管理 ----------------

    @app.route("/api/admin/users", methods=["GET"])
    def sjfx_admin_users():
        s, err = _require_admin()
        if err:
            return err
        users = user_store.list_users()
        return ad.json({"ok": True, "count": len(users), "users": users,
                        "me": s["user_id"]})

    @app.route("/api/admin/users", methods=["POST"])
    def sjfx_admin_user_create():
        s, err = _require_admin()
        if err:
            return err
        body = ad.form_body()
        try:
            created = user_store.create_user(
                body.get("username"), body.get("password") or "",
                display_name=body.get("display_name"),
                role=body.get("role") or ROLE_USER,
                must_change_pw=bool(body.get("must_change_pw", True)))
        except AuthError as exc:
            return ad.json({"ok": False, "error": str(exc)}, 400)
        return ad.json({"ok": True, "user_id": created["user_id"],
                        "username": created["username"],
                        "owner_id": created["owner_id"]})

    @app.route("/api/admin/users/<user_id>/password", methods=["POST"])
    def sjfx_admin_reset_pw(user_id):
        s, err = _require_admin()
        if err:
            return err
        if not user_store.get_user(user_id):
            return ad.json({"ok": False, "error": "用户不存在"}, 404)
        body = ad.form_body()
        try:
            # 管理员重置后强制首次改密 —— 否则管理员会长期知道别人的密码
            user_store.set_password(user_id, body.get("password") or "",
                                    must_change_pw=True)
        except AuthError as exc:
            return ad.json({"ok": False, "error": str(exc)}, 400)
        user_store.revoke_user_sessions(user_id)
        return ad.json({"ok": True, "user_id": user_id, "must_change_pw": True})

    @app.route("/api/admin/users/<user_id>", methods=["DELETE"])
    def sjfx_admin_user_status(user_id):
        s, err = _require_admin()
        if err:
            return err
        if user_id == s["user_id"]:
            return ad.json({"ok": False, "error": "不能停用当前登录的管理员账号"}, 400)
        target = user_store.get_user(user_id)
        if not target:
            return ad.json({"ok": False, "error": "用户不存在"}, 404)
        body = ad.form_body()
        status = body.get("status") or STATUS_DISABLED
        if status not in (STATUS_ACTIVE, STATUS_DISABLED):
            return ad.json({"ok": False, "error": "非法状态"}, 400)
        # 防系统失去管理入口
        if target["role"] == ROLE_ADMIN and status == STATUS_DISABLED \
                and user_store.admin_count() <= 1:
            return ad.json({"ok": False,
                            "error": "这是最后一个管理员账号，停用后系统将无人可管理"}, 400)
        try:
            user_store.set_status(user_id, status)
        except AuthError as exc:
            return ad.json({"ok": False, "error": str(exc)}, 400)
        if status == STATUS_DISABLED:
            user_store.revoke_user_sessions(user_id)
        return ad.json({"ok": True, "user_id": user_id, "status": status})

    def _fingerprint():
        """机器指纹（用于后续"归属存疑"判定）。

        刻意用粗糙但稳定的组合：**这不是安全边界**，只是相关性信号。
        """
        import hashlib
        ua = ad.header("User-Agent", "") or ""
        lang = ad.header("Accept-Language", "") or ""
        raw = "|".join([ua, lang, ad.remote_addr()])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    return {
        "session_cookie": SESSION_COOKIE,
        "resolve_session_from_request": _session,
        "adapter": ad,
    }
