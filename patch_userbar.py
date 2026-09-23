"""给 sjfx 主界面注入"当前用户"入口脚本（幂等、可回滚）。

只改一处：在 `</body>` 前追加一行 `<script src="/auth-static/userbar.js?v=1">`。

为什么用独立前缀 `/auth-static/`：那是多用户模块自己的资产路由，
不去动 sjfx 的 static 目录（那里文件名都带版本号和历史 .bak，容易撞名）。

用法：
    python patch_userbar.py --check
    python patch_userbar.py --apply
    python patch_userbar.py --rollback
"""
import io
import os
import shutil
import sys
import time

TPL = "/home/nas511/zhoulingjie/sjfx/templates/index.html"
MARKER = "auth-static/userbar.js"
SCRIPT_TAG = ('  <script src="/auth-static/userbar.js?v=1"></script>\n')
ANCHOR = "</body>"


def read():
    with io.open(TPL, "r", encoding="utf-8") as fh:
        return fh.read()


def do_check():
    text = read()
    n = text.count(ANCHOR)
    print("  锚点 </body> 出现 %d 次（期望 1）" % n)
    print("  是否已应用：%s" % ("是" if MARKER in text else "否"))
    ok = n == 1
    print("  结论：%s" % ("可以应用" if ok else "锚点不唯一，拒绝应用"))
    return ok


def do_apply():
    text = read()
    if MARKER in text:
        print("已应用过，不重复注入")
        return 0
    if text.count(ANCHOR) != 1:
        print("锚点 </body> 不唯一（%d 次），拒绝应用" % text.count(ANCHOR))
        return 2

    backup = TPL + ".pre-userbar-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TPL, backup)
    print("已备份 -> %s" % backup)

    new = text.replace(ANCHOR, SCRIPT_TAG + ANCHOR, 1)
    tmp = TPL + ".tmp-userbar"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new)
    os.replace(tmp, TPL)
    print("已写入（%d -> %d 字节）" % (len(text), len(new)))

    # 自检
    after = read()
    for needle in (MARKER, ANCHOR):
        if needle not in after:
            print("自检失败：缺少 %r，自动回滚" % needle)
            shutil.copy2(backup, TPL)
            return 4
    # 结构自检：script 必须在 </body> 之前
    if after.index(MARKER) > after.index(ANCHOR):
        print("自检失败：script 出现在 </body> 之后，自动回滚")
        shutil.copy2(backup, TPL)
        return 5
    print("自检通过（script 位于 </body> 之前）")
    return 0


def do_rollback():
    d = os.path.dirname(TPL)
    cands = sorted(f for f in os.listdir(d) if f.startswith("index.html.pre-userbar-"))
    if not cands:
        print("没有可回滚的备份")
        return 1
    src = os.path.join(d, cands[-1])
    shutil.copy2(src, TPL)
    print("已回滚 %s -> %s" % (src, TPL))
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
