"""把分片上传路由接进 app.py（幂等、可回滚）。

三处改动：
  1. import 上传模块
  2. `_initialize_runtime_state()` 里注册上传路由
  3. `_access_guard()` 放行 `/api/uploads`（它有自己的会话鉴权，
     与 /api/auth/、/api/admin/ 同类理由：浏览器导航带不了 X-SJFX-Token）

用法：
    python patch_upload_routes.py --check     # 只验证锚点
    python patch_upload_routes.py --apply
    python patch_upload_routes.py --rollback
"""
import io
import os
import re
import shutil
import subprocess
import sys
import time

APP = "/home/nas511/zhoulingjie/sjfx/app.py"
STAMP = time.strftime("%Y%m%d-%H%M%S")
BACKUP = APP + ".pre-upload-" + STAMP
MARKER = "# --- sjfx 分片上传路由（新增） ---"

# ---------------------------------------------------------------- 锚点

ANCHOR_IMPORT = """    from sjfx_multiuser import auth_routes as _auth_routes
"""

NEW_IMPORT = """    from sjfx_multiuser import auth_routes as _auth_routes
    from sjfx_multiuser import upload_routes as _upload_routes
"""

ANCHOR_REGISTER = """            _auth_routes.register(app, _multi_user_store,
                                  config_owner_id=_configured_owner_id)
            logger.info("多用户隔离已启用：注册/登录/管理员路由已挂载")
"""

NEW_REGISTER = """            _auth_routes.register(app, _multi_user_store,
                                  config_owner_id=_configured_owner_id)
            _upload_info = _upload_routes.register(
                app,
                os.path.join(Config.OUTPUT_DIR, "_uploads"),
                allowed_roots=Config.SCAN_ALLOWED_ROOTS,
                session_resolver=_multiuser_session_from_request,
                request_owner_id=_request_owner_id,
            )
            logger.info(
                "分片上传已启用：暂存 %s，组装目标限于 %s",
                _upload_info["root"], list(_upload_info["allowed_roots"]))
            logger.info("多用户隔离已启用：注册/登录/管理员路由已挂载")
"""

ANCHOR_GUARD = """    if request.path.startswith("/api/auth/") or request.path.startswith("/api/admin/"):
        return None
"""

NEW_GUARD = """    if request.path.startswith("/api/auth/") or request.path.startswith("/api/admin/"):
        return None
    # 分片上传：/api/uploads/* 与 /upload 页面同样走会话 cookie 鉴权
    # （upload_routes._owner() 未登录一律 401，归属只认会话）。
    if request.path.startswith("/api/uploads") or request.path == "/upload":
        return None
"""

PATCHES = [
    ("import", ANCHOR_IMPORT, NEW_IMPORT),
    ("register", ANCHOR_REGISTER, NEW_REGISTER),
    ("guard", ANCHOR_GUARD, NEW_GUARD),
]


def read():
    with io.open(APP, "r", encoding="utf-8") as fh:
        return fh.read()


def counts(text):
    return [(name, text.count(anchor)) for name, anchor, _ in PATCHES]


def do_check():
    text = read()
    ok = True
    for name, n in counts(text):
        want = 1
        flag = "OK " if n == want else "BAD"
        if n != want:
            ok = False
        print("  [%s] 锚点 %-10s 出现 %d 次（期望 %d）" % (flag, name, n, want))
    print("  补丁标记（应仅在已应用时存在）：%d 次" % text.count(MARKER))
    print("  结论：%s" % ("可以应用" if ok else "锚点不匹配，拒绝应用"))
    return ok


def do_apply():
    text = read()
    if MARKER in text:
        print("已应用过（找到标记），不重复打补丁")
        return 0
    for name, n in counts(text):
        if n != 1:
            print("锚点 %s 出现 %d 次，拒绝应用（先人工检查）" % (name, n))
            return 2

    shutil.copy2(APP, BACKUP)
    print("已备份 -> %s" % BACKUP)

    new = text
    for _, anchor, repl in PATCHES:
        new = new.replace(anchor, repl, 1)
    # 在注册块前插入标记（便于幂等判断与后续定位）
    new = new.replace(
        '            _upload_info = _upload_routes.register(',
        '            ' + MARKER + '\n'
        '            _upload_info = _upload_routes.register(', 1)

    try:
        compile(new, APP, "exec")
    except SyntaxError as exc:
        print("语法检查失败，未写入：%r" % exc)
        return 3

    tmp = APP + ".tmp-upload"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new)
    os.replace(tmp, APP)
    print("已写入 %s（%d -> %d 字节）" % (APP, len(text), len(new)))

    for needle in ("_upload_routes.register", MARKER,
                   'request.path.startswith("/api/uploads")'):
        if needle not in read():
            print("自检失败：缺少 %r，自动回滚" % needle)
            shutil.copy2(BACKUP, APP)
            return 4
    print("自检通过")
    return 0


def do_rollback():
    cands = [f for f in os.listdir(os.path.dirname(APP))
             if f.startswith("app.py.pre-upload-")]
    if not cands:
        print("没有可回滚的备份")
        return 1
    cands.sort()
    src = os.path.join(os.path.dirname(APP), cands[-1])
    shutil.copy2(src, APP)
    print("已回滚 %s -> %s" % (src, APP))
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if mode == "--check":
        sys.exit(0 if do_check() else 1)
    if mode == "--apply":
        sys.exit(do_apply())
    if mode == "--rollback":
        sys.exit(do_rollback())
    print("用法: --check | --apply | --rollback")
    sys.exit(9)
