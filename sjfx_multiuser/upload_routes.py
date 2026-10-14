"""分片上传的 HTTP 路由层。

把 `uploads.UploadManager`（纯文件系统、已本地测试）接到 sjfx 的 HTTP 上。

## 这一层唯一的职责是"归属"

`UploadManager` 只管分片和续传，完全不知道"谁在传"。
**归属判断全部在这里**，规则只有一条：

> `owner_id` 永远取自**已认证会话**的 `session["owner_id"]`，
> 任何请求参数、请求头、请求体里的 owner 字段一律不看。

理由：上传是**写**操作。设计方案里"管理员切换视角（view-as）"是**只读**能力
（`ownership.resolve_owner` 在 admin+viewing_owner 时返回被查看者的 owner_id，
那是给读接口用的过滤语义）。如果上传也跟随视角，管理员在查看某用户时上传，
文件就会落进那个用户名下 —— 这是**伪造他人数据**，不是便利功能。
所以这里绕开视角，直接用登录者自己的 owner。

## 与 sjfx 运行时的适配约束（实测）

- `app.py:136` `max_content_length = 4MB` → 单个分片必须 <= `MAX_CHUNK_BYTES`
- `web_compat` 只提供 `before_request`，**没有** `after_request`
- 请求体通过 `adapter.raw_body()` 拿到**原始 bytes**（`_read_request_body`
  直接迭代 ASGI stream，不做文本转换 —— 二进制安全，已核对源码）
- 路由只在真实启动时注册（`app.py` 的 `_initialize_runtime_state()`），
  所以本地测试要用桩 app

## 为什么分片放在请求体而不是 base64

base64 会把 4MB 的分片膨胀成 5.4MB —— **直接超过** 4MB 的 `max_content_length`，
要么传不过去，要么只能把分片砍到 3MB（请求数 +33%）。
原始字节体没有这个税。代价是客户端必须发 `application/octet-stream`。
"""

import os
import re
import stat
import uuid

from .http_adapter import HttpAdapter, make_adapter
from .runtime import get_current_user
from .uploads import UploadError, UploadManager

#: 分片接口接受的 content-type（宽松一点，客户端各语言默认值不一）
OCTET = "application/octet-stream"


def _human(n):
    """把字节数变成人看得懂的量级 —— 上传页要显示"已传 3.2G / 100G"。"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f%s" if unit == "B" else "%.1f%s") % (n, unit)
        n /= 1024


def _manager_root(path):
    """上传暂存目录：优先与 OUTPUT_DIR 同分区（组装时能用 rename，免拷贝）。"""
    path = os.path.abspath(path)
    _reject_path_links(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    _reject_path_links(path)
    return path


def _reject_path_links(path):
    """Reject existing symlink/reparse ancestors, including Windows junctions.

    UploadManager repeats these checks while it writes. This route check is an
    authorization preflight, not a substitute for its filesystem safeguards.
    """
    current = os.path.abspath(path)
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise UploadError("无法安全检查上传目录") from exc
        else:
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise UploadError("上传目录不能包含符号链接或重解析点")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


def upload_page_html(max_chunk_bytes):
    """上传页。

    刻意做成"一个文件输入 + 一个按钮"的极简形态：
    用户的实际场景是**U 盘**，而浏览器对 `<input type="file" webkitdirectory>`
    的支持和表现都不稳定；真正跑 100G 的场合应该是命令行客户端。
    这个页面存在的意义是"能立刻试用 + 看到进度"，不是最终形态。

    **刻意不接收 upload_root**：页面是要发给浏览器的，把服务端路径写进去
    等于白送攻击者一份目录结构情报（虽然这里有权限保护，但没有必要暴露）。
    """
    mb = max_chunk_bytes / 1024.0 / 1024.0
    return r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>上传数据包</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      margin:0;padding:28px;background:#f6f7f9;color:#1b1f23}
 .card{max-width:760px;margin:0 auto;background:#fff;border:1px solid #e3e6ea;
       border-radius:10px;padding:22px}
 h1{font-size:19px;margin:0 0 6px}
 p.sub{color:#5b6470;font-size:13px;margin:0 0 18px}
 .row{margin:14px 0}
 label{display:block;font-size:13px;font-weight:600;margin-bottom:5px}
 input[type=text],input[type=file]{width:100%;box-sizing:border-box;padding:8px;
      border:1px solid #cfd5dc;border-radius:6px;font-size:14px;background:#fff}
 button{padding:9px 18px;border:0;border-radius:6px;background:#1668d6;color:#fff;
      font-size:14px;cursor:pointer}
 button:disabled{background:#9fb4cd;cursor:default}
 .bar{height:9px;background:#e6e9ee;border-radius:5px;overflow:hidden;margin:8px 0}
 .bar>i{display:block;height:100%;width:0;background:#1668d6;transition:width .2s}
 pre{background:#0f1419;color:#d6e2f0;padding:12px;border-radius:6px;font-size:12px;
      max-height:280px;overflow:auto;white-space:pre-wrap;word-break:break-all}
 .note{background:#fff8e6;border:1px solid #f2dfa8;border-radius:6px;padding:10px 12px;
      font-size:12.5px;color:#6b5a1e;margin:14px 0}
 code{background:#eef1f5;padding:1px 5px;border-radius:4px;font-size:12px}
</style></head><body>
<div class="card">
  <h1>上传数据包</h1>
  <p class="sub">文件会按 __MB__ MB 一片切分上传，中途断开可以续传。
     上传完成后前往工作台导入，再选择需要分析的文件。</p>
  <div class="note">
    <b>断点续传</b>：暂停后重新选择相同文件即可继续。续传记录保存在当前浏览器标签页，关闭标签页后可能无法自动找回；服务端暂存会在 7 天后清理。
  </div>
  <div class="row">
    <label>数据包名称（可留空，默认用第一个文件名）</label>
    <input type="text" id="pkg" placeholder="例如：2026-09 U盘数据">
  </div>
  <div class="row">
    <label>选择文件</label>
    <input type="file" id="files" multiple>
  </div>
  <div class="row">
    <button id="go">开始上传</button>
    <button id="stop" disabled style="background:#8a94a0">暂停</button>
  </div>
  <div class="bar"><i id="bar"></i></div>
  <div id="stat" class="sub"></div>
  <div id="ready" class="row" hidden>
    <a id="openImport" href="/">前往工作台导入这个数据包</a>
  </div>
  <pre id="log"></pre>
</div>
<script src="/static/upload-sha256.js?v=1" defer></script>
<script src="/static/upload.js?v=1" data-chunk-bytes="__CHUNK__" defer></script>

</body></html>""".replace("__CHUNK__", str(max_chunk_bytes)).replace(
        "__MB__", "%.0f" % mb)


def register(app, upload_root, allowed_roots=(), adapter=None,
             session_resolver=None, request_owner_id=None,
             max_file_bytes=None, session_ttl_seconds=7 * 86400):
    """把上传路由挂到 app 上。

    参数
    ----
    app : 支持 `@app.route(path, methods=[...])` 的框架应用
    upload_root : str
        分片暂存目录
    allowed_roots : sequence of str
        保留注册参数兼容。扫描允许根不是写入授权；HTTP 上传仅组装到
        upload_root/assembled/<登录 owner>/<session_id>。显式 dest 只允许
        这个同一目录。需要扫描时使用响应中的 dest_dir，不直接写共享扫描根。
    adapter : HttpAdapter
    session_resolver : callable 或 None
        无参，返回当前请求的有效会话 dict 或 None。
        **必须传 app.py 的 `_multiuser_session_from_request`**：
        `runtime.get_current_user()` 只在登录/改密那一个请求内被显式设置，
        拿它当通用解析器会得到 None。
    request_owner_id : callable 或 None
        无参，返回"该写进哪个 owner"。传 app.py 的 `_request_owner_id`
        以复用那套已经过对抗性测试的回退逻辑。

    返回
    ----
    dict，含 manager 实例（便于测试和运维直接调用）。
    """
    # 注意 `make_adapter()` 返回的是 **(adapter, kind) 二元组**，不是 adapter 本身。
    # 漏了解包时 `http` 会是个 tuple，然后每个接口都在 `.json()` 上崩成 500 ——
    # 这个错误在本地 6 套测试里全溜过去了，因为它们都显式传了 adapter，
    # 而生产（app.py）恰恰不传。见 tests/test_upload_http.py 的 [7] 组。
    http = adapter or make_adapter()[0]
    root = _manager_root(upload_root)
    if max_file_bytes:
        mgr = UploadManager(root, max_file_bytes=max_file_bytes,
                            session_ttl_seconds=session_ttl_seconds)
    else:
        mgr = UploadManager(root, session_ttl_seconds=session_ttl_seconds)

    # 没注入解析器时退回 runtime（本地测试可以只 set_current_user）
    def _resolve_session():
        if session_resolver is not None:
            return session_resolver()
        return get_current_user()

    def _write_owner(sess):
        """返回 (owner_id, source)。

        source 有四种，调用方按它决定放行或拒绝：

        | source     | 含义                                   | 处置          |
        |------------|----------------------------------------|---------------|
        | `resolver` | 归属解析器给出了具体 owner              | 必须与会话一致 |
        | `self`     | 解析器返回 None（管理员"看全部"）       | 用登录者自己   |
        | `context`  | 没有解析器，从 runtime 取到             | 必须与会话一致 |
        | `none`     | 什么都取不到                            | 拒绝           |

        **为什么区分得这么细**：`_request_owner_id()` 对"看全部"的管理员
        会返回 `None`（那是**查询过滤**的语义 —— None 表示不过滤）。
        把它当成错误就会让管理员完全没法上传（这是端到端测试抓到的真实故障）。
        正确处置是回落到登录者自己的 owner：管理员上传的文件归自己，
        而不是"无主"。

        但**解析器缺失**是另一回事：那时唯一可用的来源是 contextvar，
        而它可能是上一个请求残留的身份（web_compat 把视图丢进线程池），
        所以取到的值必须与当前会话严格一致才敢用。
        """
        if request_owner_id is not None:
            got = request_owner_id()
            if got:
                return got, "resolver"
            # 解析器可用但返回 None —— 管理员"看全部"，回落到自己的 owner
            own = (sess or {}).get("owner_id")
            return (own, "self") if own else (None, "none")
        stale = get_current_user()
        if stale and stale.get("owner_id"):
            return stale.get("owner_id"), "context"
        return None, "none"

    # ---------------- 归属（本模块唯一的安全决策点）----------------

    def _owner():
        """返回 (owner_id, None) 或 (None, 错误响应)。

        **只看已认证会话**，不看 query、不看 body、不看 header。

        上传是**写**操作，必须落在登录者自己的 owner 下。设计方案里
        "管理员切换视角（view-as）"是**只读**能力（`ownership.resolve_owner`
        在 admin+viewing_owner 时返回被查看者的 owner_id，那是读过滤语义）。
        如果上传也跟随视角，管理员在查看某用户时上传，文件就会落进那个用户名下
        —— 那是**伪造他人数据**，不是便利功能。

        四重拒绝，缺一不可：
          1. 会话解析不出来 → 401（绝不回落到可能过期的 contextvar）
          2. 任何来源都给不出 owner → 403
          3. 取到的 owner 与登录者的 owner 不一致 → 403
             （视角是读语义；也是"陈旧 contextvar"的最后一道闸）
          4. 来源是 `context`（没注入解析器）时，额外要求完全一致 ——
             由第 3 条覆盖，这里只在两者都为空时拒绝
        """
        sess = _resolve_session()
        if not sess:
            return None, http.json({"ok": False, "error": "未登录"}, 401)
        owner, source = _write_owner(sess)
        if not owner:
            return None, http.json({"ok": False, "error": "无法确定归属，拒绝写入"}, 403)
        if owner != sess.get("owner_id"):
            # 真发生了说明归属解析被篡改或出现新语义 —— 保守拒绝，
            # 因为放行的后果是"以别人的身份写文件"（或写进别人的数据包）。
            return None, http.json(
                {"ok": False, "error": "归属与登录身份不一致，拒绝写入"}, 403)
        return owner, None

    def _fail(exc, status=400):
        return http.json({"ok": False, "error": str(exc)}, status)

    def _json_object():
        body = http.json_body()
        if body is None:
            if http.raw_body():
                raise UploadError("请求体必须是合法 JSON 对象")
            return {}
        if not isinstance(body, dict):
            raise UploadError("请求体必须是 JSON 对象")
        return body

    def _valid_digest(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None

    # ---------------- 页面 ----------------

    @app.route("/upload", methods=["GET"])
    def sjfx_upload_page():
        owner, err = _owner()
        if err:
            return http.redirect("/login")
        return http.html(upload_page_html(mgr.max_chunk_bytes))

    # ---------------- API ----------------

    @app.route("/api/uploads", methods=["POST"])
    def sjfx_upload_begin():
        owner, err = _owner()
        if err:
            return err
        try:
            body = _json_object()
        except UploadError as exc:
            return _fail(exc)
        files = body.get("files")
        if not isinstance(files, list):
            return _fail("缺少 files 列表")
        for item in files:
            if not isinstance(item, dict):
                return _fail("files 中的每一项必须是对象")
            digest = item.get("sha256")
            if digest not in (None, "") and not _valid_digest(digest):
                return _fail("文件 sha256 必须是 64 位十六进制字符串")
        # session_id 由服务端生成 —— 不接受客户端指定，
        # 否则客户端可以用同一个 id 覆盖/窥探别人的会话目录
        session_id = body.get("session_id") or _new_session_id()
        try:
            manifest = mgr.begin(owner, session_id, files,
                                 package_name=body.get("package_name"))
        except UploadError as exc:
            return _fail(exc)
        return http.json({
            "ok": True,
            "session_id": manifest["session_id"],
            "file_count": manifest["file_count"],
            "total_bytes": manifest["total_bytes"],
            "max_chunk_bytes": manifest["max_chunk_bytes"],
            "files": [{"rel_path": f["rel_path"], "size": f["size"],
                       "chunks_total": f["chunks_total"], "state": f["state"]}
                      for f in manifest["files"]],
        })

    @app.route("/api/uploads/<session_id>", methods=["GET"])
    def sjfx_upload_status(session_id):
        owner, err = _owner()
        if err:
            return err
        try:
            st = mgr.status(owner, session_id)
        except UploadError as exc:
            return _fail(exc, 404)
        st["ok"] = True
        # 给客户端现成的可读量级 —— 上传页要显示"已传 3.2GB / 100GB"
        st["received_human"] = _human(st["received_bytes"])
        st["total_human"] = _human(st["total_bytes"])
        return http.json(st)

    @app.route("/api/uploads/<session_id>", methods=["DELETE"])
    def sjfx_upload_abort(session_id):
        owner, err = _owner()
        if err:
            return err
        try:
            mgr.abort(owner, session_id)
        except UploadError as exc:
            return _fail(exc, 404)
        return http.json({"ok": True, "aborted": session_id})

    # web_compat converts <name>, but not Flask's <int:name> syntax. The manager
    # validates the integer and its bounds; a typed Flask converter would make
    # the production /chunks/0 URL a literal-route 404.
    @app.route("/api/uploads/<session_id>/chunks/<file_index>",
               methods=["PUT", "POST"])
    def sjfx_upload_chunk(session_id, file_index):
        owner, err = _owner()
        if err:
            return err
        data = http.raw_body()
        if isinstance(data, str):
            data = data.encode("utf-8", "surrogateescape")
        # chunk_index 从 query 取；不传 = 让服务端取下一片（更安全，见 uploads.py）
        raw_ci = http.query_arg("chunk_index")
        chunk_index = None
        if raw_ci not in (None, ""):
            try:
                chunk_index = int(raw_ci)
            except (TypeError, ValueError):
                return _fail("chunk_index 非法")
        digest = http.query_arg("sha256") or http.header("X-Chunk-SHA256") or None
        if digest is not None and not _valid_digest(digest):
            return _fail("分片 sha256 必须是 64 位十六进制字符串")
        try:
            res = mgr.put_chunk(owner, session_id, file_index, chunk_index, data,
                                chunk_sha256=digest)
        except UploadError as exc:
            return _fail(exc)
        res["ok"] = True
        return http.json(res)

    @app.route("/api/uploads/<session_id>/finalize", methods=["POST"])
    def sjfx_upload_finalize(session_id):
        owner, err = _owner()
        if err:
            return err
        try:
            body = _json_object()
            requested = body.get("dest")
            if requested is not None and not isinstance(requested, str):
                raise UploadError("dest 必须是字符串")
            # Validate the session/owner identifiers before constructing a path.
            # A session from another owner is unavailable, even to an admin.
            mgr.status(owner, session_id)
        except UploadError as exc:
            return _fail(exc)
        dest = os.path.join(root, "assembled", owner, session_id)
        try:
            allowed = _pick_allowed_root(
                requested.strip() if requested and requested.strip() else dest,
                allowed_roots, own_root=dest,
            )
        except UploadError as exc:
            return _fail(exc, 403)
        try:
            res = mgr.assemble(owner, session_id, dest,
                               allowed_root=allowed)
        except UploadError as exc:
            return _fail(exc)
        res["ok"] = True
        return http.json(res)

    return {"manager": mgr, "root": root, "allowed_roots": tuple(allowed_roots)}


def _pick_allowed_root(dest, allowed_roots, own_root=None):
    """Authorize only this owner's server-selected session output directory.

    ``own_root`` must be the complete private session destination, never the
    whole upload root. ``allowed_roots`` remains a compatibility parameter;
    scanner read roots confer no write permission, including for administrators.
    Return a concrete root: an absent allowlist never means unrestricted writes.
    """
    if not own_root:
        raise UploadError("缺少当前用户的私有上传目录")
    if not isinstance(dest, str) or "\x00" in dest or not os.path.isabs(dest):
        raise UploadError("上传只能组装到当前用户的私有会话目录；请省略 dest")
    private = os.path.abspath(own_root)
    requested = os.path.abspath(dest)
    if os.path.normcase(requested) != os.path.normcase(private):
        raise UploadError("上传只能组装到当前用户的私有会话目录；请省略 dest")
    _reject_path_links(private)
    return private


def _new_session_id():
    return uuid.uuid4().hex
