"""路由归属审计：把"每个带 id 的接口都要校验归属"从人工纪律变成自动检查。

## 为什么不是"启动时就报错"

设计方案 §4.2 的原话是"收敛成装饰器 + 启动闸门"。实现时发现
**纯文本模式的硬闸门会产生误报**：

实测（2026-09-21，`survey_guards.py`）有 **11 条**带 id 路由的文本里
找不到任何归属过滤写法，但逐条查证后**全部是安全的**：

| 类型 | 例 | 实际校验在哪 |
|---|---|---|
| 用了别的守卫函数 | 9 条大包路由 | `_large_package_owned()` → `large_package_store.get(pkg, owner_id=_effective_owner())` |
| 纯转发 | `/api/package/<id>/overview` | 被调的 `formal_directory()` 内部 `require_scan()` |
| 纯转发 | `/api/conversation/<sid>/messages` | 被调的 `create_conversation_turn()` 内部 `_effective_owner()` |

如果把文本模式做成**启动失败**条件，这 11 条合法代码会让服务起不来 ——
**误报比不检查更糟**（会逼人把闸门关掉，等于没有）。

## 所以用"证据 + 显式清单"两层

1. **模式层**：扫描每个处理函数，看有没有归属过滤的写法。
   覆盖 `_request_owner_id()` / `_effective_owner()` / `scan_owned()` /
   `require_scan()` / `_require_admin()` / `_large_package_owned()` /
   `owner_id=` 传参。
2. **清单层**：模式没命中的必须出现在 `KNOWN_SAFE_DELEGATORS` 里，
   **且必须写明原因**。新增路由若两边都不占 → 审计失败。

这样"纪律"就被固化成结构了：加一个新的带 id 接口时，
要么写校验，要么**有意识地**在清单里解释为什么不需要 ——
不能像以前那样默默地漏掉。

## 与真实探针的关系

`probe_all_routes.py` 用真实 HTTP 打 75 条路由，是**实测准绳**。
本模块是**静态防线**，用于在代码变更时（部署前）发现问题。
两者互补：静态层能覆盖"测试没跑到的分支"，实测层能发现"模式对了但逻辑错了"。
"""

import os
import re

#: id 类路径参数（会进路径、决定"操作哪个对象"）
ID_PARAMS = ("scan_id", "job_id", "session_id", "node_id", "turn_id",
             "package_id", "user_id", "filename", "node_path")

#: 归属过滤的写法。每条都要有真实代码支撑（已在 app.py 里核对过）。
GUARD_PATTERNS = [
    (r"_request_owner_id\(\)", "_request_owner_id()"),
    (r"_effective_owner\(\)", "_effective_owner()"),
    (r"\bowner_id\s*=", "owner_id= 传参"),
    (r"\bscan_owned\(", "scan_owned()"),
    (r"\brequire_scan\(", "require_scan()"),
    (r"\b_require_admin\(", "_require_admin()"),
    (r"\b_large_package_owned\(", "_large_package_owned()"),
    (r"\b_large_package_owner_id\(", "_large_package_owner_id()"),
    (r"\b_owned_scan\(", "_owned_scan()"),
]

#: 已知安全的"无显式守卫"路由：自己不做校验，但校验在别处做了。
#: **每条必须写原因** —— 这是这个清单存在的全部意义。
#:
#: 三类原因（都要写明）：
#:   1. 纯转发 —— 被调用方内部有守卫
#:   2. 守卫在上游全局层 —— 例如 `_access_guard()` 对某个路径前缀统一校验
#:   3. 用的守卫函数不在 GUARD_PATTERNS 里 —— 此时应当**加模式**而不是加清单项
KNOWN_SAFE_DELEGATORS = {
    "package_directory_contract": (
        u"纯转发：return formal_directory(scan_id)，"
        u"而 formal_directory() 第一行就是 require_scan(scan_id)"),
    "conversation_message": (
        u"纯转发：return create_conversation_turn(session_id)，"
        u"而它内部用 storage.get_conversation(session_id, _effective_owner()) 过滤"),
    "package_preliminary_directory": (
        u"纯转发：return preliminary_directory(scan_id)，"
        u"被调用方内部做归属判定（探针实测：普通用户对该接口 400 被拒）"),
    "package_overview_contract": (
        u"纯转发：return package_overview(scan_id)，"
        u"被调用方内部做归属判定（探针实测：普通用户对该接口 400 被拒）"),
    "list_translations": (
        u"守卫在上游全局层：`_access_guard()` 里对 /api/translations/ 前缀"
        u"统一做 storage.scan_owned() 校验并返回 404。"
        u"这是本次改造专门补的历史越权点（见设计方案 §4.5），"
        u"校验刻意放在全局层以便集中审查与回滚"),
}

#: 路由 → 处理函数的解析
_ROUTE_RE = re.compile(r'^\s*@app\.route\(\s*"([^"]+)"')
_DEF_RE = re.compile(r'^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(')


def parse_routes(source):
    """从 app.py 源码解析出 [(path, fn, start_line, body)]。

    做法：找到 `@app.route(...)` 后，在**其后 12 行内**找第一个 `def`
    （装饰器可能堆叠，也可能 route 本身跨行）。

    两个踩过的坑：
      1. 回看窗口太小（6 行）会漏掉装饰器堆叠的路由
      2. 想"先跳过 methods=[...] 再跳装饰器"是错的 —— 多数 `@app.route("/x")`
         **不含** methods，按那个条件跳会直接跳进函数体，
         于是被误判成"找不到 def"（我的诊断脚本就是这么写错的，
         白折腾了一轮）
    """
    lines = source.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = _ROUTE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        path = m.group(1)
        fn, start = None, None
        for k in range(i + 1, min(i + 13, len(lines))):
            d = _DEF_RE.match(lines[k])
            if d:
                fn, start = d.group(1), k
                break
        if fn is None:
            i += 1
            continue
        body = []
        p = start + 1
        while p < len(lines):
            ln = lines[p]
            if ln.strip() and not ln.startswith((" ", "\t")):
                break
            body.append(ln)
            p += 1
        out.append({"path": path, "fn": fn, "start": start + 1,
                    "end": p, "body": "\n".join(body)})
        i = start + 1
    return out


def has_id_param(path):
    return any(("<%s>" % p) in path for p in ID_PARAMS)


def guards_in(body):
    return [label for pat, label in GUARD_PATTERNS if re.search(pat, body)]


def audit(source):
    """审计 app.py 源码。

    返回 dict：
      total_routes / id_routes / guarded / via_allowlist
      violations: [ {path, fn, start} ]  ← 两边都不占的，必须人工处理
      allowlist_stale: [fn]  ← 在清单里但已经自己带守卫了（清单该清理）
    """
    routes = parse_routes(source)
    id_routes = [r for r in routes if has_id_param(r["path"])]
    guarded, via_allow, violations = [], [], []
    for r in id_routes:
        hits = guards_in(r["body"])
        if hits:
            guarded.append((r, hits))
        elif r["fn"] in KNOWN_SAFE_DELEGATORS:
            via_allow.append(r)
        else:
            violations.append(r)
    # 清单里已经自带守卫的 → 清单过时，应清理
    allow_fns = set(KNOWN_SAFE_DELEGATORS)
    stale = sorted(fn for fn in allow_fns
                   if any(r["fn"] == fn and guards_in(r["body"])
                          for r in id_routes))
    # 清单里引用了不存在的函数 → 拼写错误
    all_fns = set(r["fn"] for r in routes)
    missing = sorted(fn for fn in allow_fns if fn not in all_fns)
    return {
        "total_routes": len(routes),
        "id_routes": len(id_routes),
        "guarded": guarded,
        "via_allowlist": via_allow,
        "violations": violations,
        "allowlist_stale": stale,
        "allowlist_missing": missing,
    }


def app_py_path(root=None):
    """找 app.py：优先显式传入，其次同仓库部署布局，最后服务器常见路径。

    **不要对候选路径做 `os.path.abspath`** —— 在 Windows 上它会把
    `/home/nas511/...` 变成 `C:\\home\\nas511\\...`，于是"文件存在"检查
    通过了一个不存在的路径，`audit()` 解析出 0 条路由，
    然后拿这个空结果去断言"清单里的函数不存在" —— 全是假失败。
    （第一版就是这么写的，被 test_route_audit.py 的 [6] 抓住了。）
    """
    cands = []
    if root:
        cands.append(root)
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.normpath(os.path.join(here, "..", "app.py")))
    if os.name != "nt":
        cands.append("/home/nas511/zhoulingjie/sjfx/app.py")
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def main():
    import sys
    path = app_py_path(sys.argv[1] if len(sys.argv) > 1 else None)
    if not path:
        print(u"找不到 app.py；用法：python -m sjfx_multiuser.route_audit <app.py 路径>")
        return 2
    with open(path, "r", encoding="utf-8") as fh:
        report = audit(fh.read())

    print(u"审计对象: %s" % path)
    print(u"  路由总数: %d" % report["total_routes"])
    print(u"  带 id 参数: %d" % report["id_routes"])
    print(u"  模式层判定有守卫: %d" % len(report["guarded"]))
    print(u"  清单层放行（纯转发）: %d" % len(report["via_allowlist"]))
    for r in report["via_allowlist"]:
        print(u"     %s（%s）" % (r["fn"], KNOWN_SAFE_DELEGATORS[r["fn"]][:60]))
    print(u"  未被覆盖: %d" % len(report["violations"]))
    for r in report["violations"]:
        print(u"     ★ %s  %s  (行 %d)" % (r["path"], r["fn"], r["start"]))
    if report["allowlist_stale"]:
        print(u"  清单已过时（这些函数现在自带守卫了，可清理）: %s"
              % report["allowlist_stale"])
    if report["allowlist_missing"]:
        print(u"  ★ 清单引用了不存在的函数: %s" % report["allowlist_missing"])

    bad = bool(report["violations"]) or bool(report["allowlist_missing"])
    print(u"")
    print(u"结论: %s" % ("FAIL —— 有带 id 的路由既没有归属守卫、也不在清单里"
                        if bad else "PASS"))
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
