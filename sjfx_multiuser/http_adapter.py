"""HTTP 适配层：让认证路由在 **两种运行环境** 下都能工作。

## 为什么需要这一层（踩坑记录）

我最初把认证路由按 Flask 写（`from flask import request, jsonify, redirect`）。
后来读了 `web_compat.py` 才发现：**真实 sjfx 的 HTTP 层是 FastAPI + 一个窄兼容层**：

    app = SJFXFastAPI(FastAPI)          # 不是 Flask
    request = _RequestProxy()            # 基于 contextvars 的代理
    class RequestData: ...               # 只有 path/method/headers/args/get_json

具体差异（都是我逐一读源码确认的）：

| 能力 | Flask | sjfx 的 web_compat |
|---|---|---|
| `request.cookies` | ✅ | **❌ 完全不支持** |
| `redirect()` | ✅ | ❌ 没 import |
| `render_template_string` | ✅ | ❌ 没有（只有 `render_template` 读文件） |
| `after_request` 钩子 | ✅ | ❌ `SJFXFastAPI` 只有 `before_request` |
| 响应类型 | Flask Response | `CompatJSONResponse` / Starlette `Response` |

**所以直接把 Flask 路由贴进去会在服务器上跑不起来。**
但 `users.py` / `ownership.py` 是纯 Python、框架无关，不受影响 —— 只有这一层要适配。

## 设计

`HttpAdapter` 把"读 cookie/取请求数据/造响应"抽象出来，按环境自动选择实现：
  - 优先 sjfx 的 `web_compat`（生产环境）
  - 回落到 Flask（本地测试 / 万一将来改回 Flask）

**cookie 的处理是本层的核心**：因为 `web_compat` 不支持 cookie，
我们从原始 `Cookie` 请求头手工解析，并用 `Set-Cookie` 响应头手工设置。
这不是权宜之计 —— HTTP cookie 本来就是这个格式，手工处理只是少一层封装。
"""

import json as _json
import re

#: 会话 cookie 名
SESSION_COOKIE = "sjfx_session"

_COOKIE_RE = re.compile(r"\s*([^=;]+)=([^;]*)")


def parse_cookies(cookie_header):
    """从原始 Cookie 头解析出字典。

    `Cookie: a=1; b=2` → `{"a": "1", "b": "2"}`
    """
    out = {}
    if not cookie_header:
        return out
    for part in str(cookie_header).split(";"):
        m = _COOKIE_RE.match(part)
        if not m:
            continue
        out[m.group(1).strip()] = m.group(2).strip()
    return out


def build_set_cookie(name, value, max_age=None, httponly=True,
                     samesite="Lax", path="/", secure=False):
    """构造 Set-Cookie 头值。

    **不设 secure 的原因**：sjfx 走内网 http（Tailscale），
    设了 secure 浏览器会直接丢弃 cookie ——
    表现为"登录成功但立刻退回登录页"，很难排查。
    """
    parts = ["%s=%s" % (name, value or "")]
    parts.append("Path=%s" % path)
    if httponly:
        parts.append("HttpOnly")
    if samesite:
        parts.append("SameSite=%s" % samesite)
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append("Max-Age=%d" % int(max_age))
    return "; ".join(parts)


def build_clear_cookie(name, path="/"):
    return "%s=; Path=%s; Max-Age=0; HttpOnly; SameSite=Lax" % (name, path)


def parse_urlencoded(raw, content_type=""):
    """解析 `application/x-www-form-urlencoded` 请求体。

    `web_compat` 不解析表单，所以登录/注册页的 POST 要在这里手工处理。
    `application/json` 的内容交给 json_body()，这里也做个兜底（有些客户端会搞错）。
    """
    from urllib.parse import parse_qsl

    if not raw:
        return {}
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw.decode("latin-1", "replace")
    raw = str(raw).strip()
    if not raw:
        return {}
    ct = (content_type or "").lower()
    if "json" in ct:
        try:
            data = _json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    # 默认按 urlencoded 解析（含 `a=1&b=2` 形式）
    try:
        return dict(parse_qsl(raw, keep_blank_values=True))
    except Exception:  # noqa: BLE001
        return {}


class HttpAdapter(object):
    """把请求/响应操作抽象出来，屏蔽 Flask 与 web_compat 的差异。

    子类实现 `_request_data` / `_json` / `_html` / `_redirect`。
    调用方只用本类的方法，不直接碰框架。
    """

    #: 由子类提供：判断当前是否有请求上下文
    def has_request_context(self):
        raise NotImplementedError

    def header(self, name, default=None):
        """读请求头。**注意 web_compat 的 headers 键统一小写。**"""
        raise NotImplementedError

    def cookie(self, name, default=None):
        """从原始 Cookie 头解析出指定 cookie。

        为什么不用框架的 cookie API：sjfx 的 web_compat **完全不支持 cookies**
        （`RequestData` 只有 path/method/headers/args），
        所以统一从 `Cookie` 头手工解析 —— 两种环境下行为一致。
        """
        return parse_cookies(self.raw_cookie_header()).get(name, default)

    def raw_cookie_header(self):
        raise NotImplementedError

    def query_arg(self, name, default=None):
        raise NotImplementedError

    def remote_addr(self):
        return "unknown"

    # ---- 响应 ----
    def json(self, payload, status=200, set_cookie=None, clear_cookie=None):
        raise NotImplementedError

    def html(self, markup, status=200, set_cookie=None, clear_cookie=None,
             mime="text/html"):
        """返回文本响应。

        `mime` 默认 `text/html`（既有调用点不用改）；
        伺服 JS/CSS 这类静态资产时要显式传 `application/javascript` 等 ——
        content-type 错了浏览器会拒绝执行，表现为"脚本 200 但没生效"。
        """
        raise NotImplementedError

    def redirect(self, location, status=302, set_cookie=None, clear_cookie=None):
        raise NotImplementedError

    def form_body(self):
        """读请求体为 dict。

        优先级：JSON → 表单（urlencoded）→ 空 dict。
        **为什么需要它**：登录/注册页用 POST 表单提交，
        而 sjfx 的 web_compat 完全不解析表单（`RequestData` 只有 `get_json`），
        所以 urlencoded 要手工解。
        """
        data = self.json_body()
        if isinstance(data, dict) and data:
            return data
        raw = self.raw_body()
        if raw:
            return parse_urlencoded(raw, self.header("Content-Type", ""))
        return {}

    def json_body(self):
        raise NotImplementedError

    def raw_body(self):
        """返回原始请求体（bytes 或 str）。默认空。"""
        return b""


# --------------------------------------------------------------------------
# sjfx 生产环境适配（web_compat / FastAPI）
# --------------------------------------------------------------------------

def _make_web_compat_adapter():
    from starlette.responses import HTMLResponse, RedirectResponse, Response

    import web_compat as wc

    class WebCompatAdapter(HttpAdapter):
        def has_request_context(self):
            try:
                return wc.has_request_context()
            except Exception:  # noqa: BLE001
                return False

        def _req(self):
            return wc.request

        def header(self, name, default=None):
            # web_compat 的 _Headers.get 会把 key 转小写再查 —— 传原名即可
            try:
                return self._req().headers.get(name, default)
            except Exception:  # noqa: BLE001
                return default

        def raw_cookie_header(self):
            return self.header("Cookie", "") or ""

        def query_arg(self, name, default=None):
            try:
                return self._req().args.get(name, default)
            except Exception:  # noqa: BLE001
                return default

        def json_body(self):
            try:
                return self._req().get_json(silent=True)
            except Exception:  # noqa: BLE001
                return None

        def raw_body(self):
            """web_compat 的 RequestData 把原始体放在 `_body`（可能是 bytes/dict）。"""
            try:
                body = getattr(self._req(), "_body", None)
                if body is None or isinstance(body, dict):
                    return b""
                return body
            except Exception:  # noqa: BLE001
                return b""

        def remote_addr(self):
            fwd = self.header("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
            return self.header("X-Real-IP", "unknown") or "unknown"

        def _headers_for(self, set_cookie=None, clear_cookie=None,
                         content_type=None):
            headers = {}
            if content_type:
                headers["content-type"] = content_type
            if set_cookie:
                headers["set-cookie"] = set_cookie
            if clear_cookie:
                headers["set-cookie"] = clear_cookie
            return headers

        def json(self, payload, status=200, set_cookie=None, clear_cookie=None):
            return Response(
                content=_json.dumps(payload, ensure_ascii=False),
                status_code=status,
                headers=self._headers_for(set_cookie, clear_cookie,
                                          "application/json; charset=utf-8"),
                media_type=None,
            )

        def html(self, markup, status=200, set_cookie=None, clear_cookie=None,
                 mime="text/html"):
            # 用 Response 而不是 HTMLResponse：后者会把 media_type 钉死成
            # text/html，伺服 JS 时浏览器会因为 content-type 不对而拒绝执行
            # （现象是"脚本 200 OK 但完全没生效"，很难查）。
            return Response(
                content=markup, status_code=status,
                media_type=mime,
                headers=self._headers_for(set_cookie, clear_cookie, mime),
            )

        def redirect(self, location, status=302, set_cookie=None,
                     clear_cookie=None):
            resp = RedirectResponse(url=location, status_code=status)
            if set_cookie:
                resp.headers["set-cookie"] = set_cookie
            if clear_cookie:
                resp.headers["set-cookie"] = clear_cookie
            return resp

    return WebCompatAdapter()


# --------------------------------------------------------------------------
# Flask 适配（本地测试）
# --------------------------------------------------------------------------

def _make_flask_adapter():
    from flask import Response, jsonify, redirect as flask_redirect, request

    class FlaskAdapter(HttpAdapter):
        def has_request_context(self):
            try:
                from flask import has_request_context as h
                return h()
            except Exception:  # noqa: BLE001
                return False

        def header(self, name, default=None):
            return request.headers.get(name, default)

        def raw_cookie_header(self):
            return request.headers.get("Cookie", "") or ""

        def query_arg(self, name, default=None):
            return request.args.get(name, default)

        def json_body(self):
            return request.get_json(silent=True)

        def raw_body(self):
            try:
                data = request.get_data(cache=True)
                return data or b""
            except Exception:  # noqa: BLE001
                return b""

        def form_body(self):
            # Flask 自己会解析表单，优先用它（更可靠）
            if request.form:
                return request.form.to_dict()
            return super(FlaskAdapter, self).form_body()

        def remote_addr(self):
            fwd = request.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
            return request.remote_addr or "unknown"

        def _apply(self, resp, set_cookie=None, clear_cookie=None):
            if set_cookie:
                resp.headers["Set-Cookie"] = set_cookie
            if clear_cookie:
                resp.headers["Set-Cookie"] = clear_cookie
            return resp

        def json(self, payload, status=200, set_cookie=None, clear_cookie=None):
            resp = Response(_json.dumps(payload, ensure_ascii=False),
                            status=status, mimetype="application/json")
            return self._apply(resp, set_cookie, clear_cookie)

        def html(self, markup, status=200, set_cookie=None, clear_cookie=None,
                 mime="text/html"):
            resp = Response(markup, status=status, mimetype=mime)
            return self._apply(resp, set_cookie, clear_cookie)

        def redirect(self, location, status=302, set_cookie=None,
                     clear_cookie=None):
            resp = flask_redirect(location, code=status)
            resp.headers.pop("Set-Cookie", None)
            return self._apply(resp, set_cookie, clear_cookie)

    return FlaskAdapter()


def make_adapter():
    """按运行环境选适配器。生产用 web_compat，本地测试用 Flask。"""
    try:
        import web_compat  # noqa: F401
        return _make_web_compat_adapter(), "web_compat"
    except ImportError:
        pass
    try:
        import flask  # noqa: F401
        return _make_flask_adapter(), "flask"
    except ImportError:
        raise RuntimeError("既没有 web_compat 也没有 Flask，无法选适配器")
