"""修掉写路径的 owner 语义 bug：`_request_owner_id() or "legacy"` → `_effective_owner()`。

## 问题

`_request_owner_id()` 返回 `None` 表示"不过滤"（管理员看全部的**查询**语义）。
但**写**数据需要的是**具体**归属。`or "legacy"` 把它变成了字面字符串 `"legacy"`：

  1. 管理员建的任务归属变成 `legacy`，而管理员"全部视角"下过滤的是 `primary`
     —— **他自己看不到刚建的任务**
  2. 数据变成孤儿：没有任何用户能在列表里看到它
  3. 后台 worker 会照常处理它（已验证：`ebd9f4b3959f` 状态 running）

实测证据（2026-09-21）：一次管理员 `/api/analyze-package` 调用建出了
`owner_id='legacy'` 的任务，而同一接口对普通用户是 400 被拒。

## 修法

`_effective_owner()`（§19.2 为这个语义问题加的函数）返回**具体** owner：
  - 普通用户 / 无会话 → 自己的 owner
  - 管理员未切视角 → `_configured_owner_id`（默认 primary）
  - 管理员切到某用户 → 那个用户的 owner

对普通用户与无会话场景，两者**返回值完全相同** → 零行为变化。
只有管理员场景从 `"legacy"` 变成 `primary` —— 那正是要修的。

用法：
    python patch_write_owner.py --check
    python patch_write_owner.py --apply
    python patch_write_owner.py --rollback
"""
import io
import os
import re
import shutil
import sys
import time

APP = "/home/nas511/zhoulingjie/sjfx/app.py"
MARKER = "# --- sjfx 写路径 owner 语义修正（新增） ---"

#: 精确的旧写法 → 新写法。用正则而不是简单 replace，
#: 是为了同时覆盖 `or "legacy"` 与 `or 'legacy'`（引号风格可能不同）。
PAT = re.compile(r'_request_owner_id\(\)\s*or\s*["\']legacy["\']')
NEW = '_effective_owner()'


def read():
    with io.open(APP, "r", encoding="utf-8") as fh:
        return fh.read()


def counts(text):
    return len(PAT.findall(text))


def do_check():
    text = read()
    n = counts(text)
    print("  待替换处数：%d" % n)
    print("  已应用标记：%s" % ("有" if MARKER in text else "无"))
    print("  `_effective_owner` 定义存在：%s"
          % ("是" if "def _effective_owner" in text else "否"))
    # 确认替换目标里没有别的同名函数
    other = re.findall(r'\bdef\s+_effective_owner\b', text)
    print("  `_effective_owner` 定义个数：%d（必须为 1）" % len(other))
    ok = n > 0 and len(other) == 1 and "def _effective_owner" in text
    print("  结论：%s" % ("可以应用" if ok else "拒绝应用"))
    return ok


def do_apply():
    text = read()
    n = counts(text)
    if n == 0:
        print("没有待替换处（可能已应用过）")
        return 0
    if len(re.findall(r'\bdef\s+_effective_owner\b', text)) != 1:
        print("`_effective_owner` 定义不是唯一，拒绝应用")
        return 2

    backup = APP + ".pre-writeowner-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(APP, backup)
    print("已备份 -> %s" % backup)

    new, k = PAT.subn(NEW, text)
    print("已替换 %d 处" % k)

    # 在 _effective_owner 定义前加标记（便于识别本次改动）
    new = new.replace("def _effective_owner():",
                      MARKER + "\ndef _effective_owner():", 1)

    try:
        compile(new, APP, "exec")
    except SyntaxError as exc:
        print("语法检查失败，未写入：%r" % exc)
        return 3

    tmp = APP + ".tmp-writeowner"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new)
    os.replace(tmp, APP)
    print("已写入（%d -> %d 字节）" % (len(text), len(new)))

    after = read()
    left = counts(after)
    if left != 0:
        print("自检失败：仍有 %d 处旧写法，自动回滚" % left)
        shutil.copy2(backup, APP)
        return 4
    if MARKER not in after:
        print("自检失败：缺少标记，自动回滚")
        shutil.copy2(backup, APP)
        return 5
    print("自检通过：旧写法 0 处，标记已写入")
    return 0


def do_rollback():
    d = os.path.dirname(APP)
    cands = sorted(f for f in os.listdir(d)
                   if f.startswith("app.py.pre-writeowner-"))
    if not cands:
        print("没有可回滚的备份")
        return 1
    src = os.path.join(d, cands[-1])
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
