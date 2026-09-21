# -*- coding: utf-8 -*-
"""一次性历史数据修复（幂等；默认 dry-run）。

为什么需要它
------------
本轮改修把**新**流程的口径修正了，但生产库里已经积累了按旧口径写下的行。
这些行不会自己变好，而且会污染每一个读取它们的页面与统计：

  R1  已识别的文件仍挂在"待补充识别"队列里（目录阶段给出结论时只结束 catalog 行）
  R2  已取消的数据包仍留着可被领取的队列行与 queued 的 deep_status
  R3  20/20 条 selection 的 batch_total=0（批次结算此前因缺 json 导入而恒为空批次）
  R4  212 个组的 member_count 与实际成员数不符（REPLACE 的隐式删除不触发减计数）
  R5  28,008 行的 summary_quality 为空，报告只能把它们记成 "unknown"
  R6  73 个 catalog_status=deferred 的文件停在已完成的行上，永远不会被重试
  R7  lp_catalog_label_history 140,473 行 / 489.6 MB（41% 的库体积、35.3 倍冗余）
  R8  版本链表（lp_catalog_versions / lp_evidence_snapshots / lp_runs）全空

设计约束
--------
* **幂等**：每一项的条件都是"当前仍是错的"，重复执行不会二次改动任何数据；
* **不覆盖业务结论**：只补计数、质量分级与终态，不改 category/topic/标签名/摘要正文；
* **可 dry-run**：`--dry-run` 只统计将要改动的行数，不写库；
* **复用生产代码**：selection 结算直接调用 Worker 的 `_finish_selection`，
  成员计数直接调用 `semantic.repair_member_counts`，避免"修复脚本自己一套口径"；
* **一项一个事务**，只拿短事务，服务不用停。

用法
----
    python -m services.large_package_repair --dry-run
    python -m services.large_package_repair --apply
    python -m services.large_package_repair --apply --only R1,R4
    python -m services.large_package_repair --vacuum      # 需先停服务
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.large_package_isolated import LargePackageStore   # noqa: E402

LOGGER = logging.getLogger("sjfx.large_package_repair")

_LEGACY_QUALITY = "legacy_unverified"   # 老格式摘要：没有 finish_reason，无法证明完整
_LEGACY_COVERAGE = "sampled_legacy"


def _rows(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def _one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def _scalar(conn, sql, args=(), default=0):
    row = _one(conn, sql, args)
    return row[0] if row and row[0] is not None else default


def _packages(conn):
    return [str(r[0]) for r in _rows(conn, "SELECT id FROM packages ORDER BY created_at")]


# ---------------------------------------------------------------------------
# R1：识别队列与 files 对齐
# ---------------------------------------------------------------------------
def r1_recognize_reconcile(store, apply=False):
    """Use the runtime predicate; preserve running leases and unresolved content.

    Classification must match the current member fingerprint and a completed
    catalog job. A catalog completion or membership alone is not sufficient.
    """
    return {"rows": store.reconcile_recognition(apply=apply)}


# ---------------------------------------------------------------------------
# R2：已取消的数据包不得留下可被领取的活
# ---------------------------------------------------------------------------
def r2_cancelled_packages(store, apply=False):
    with closing(store._connect()) as conn:
        pids = [str(r[0]) for r in _rows(
            conn, "SELECT id FROM packages WHERE status='cancelled' OR control_state='cancelled'")]
        alive = files_queued = sels = 0
        for pid in pids:
            alive += int(_scalar(conn, "SELECT COUNT(*) FROM queue WHERE package_id=? "
                                       "AND status IN ('queued','running','retry')", (pid,)))
            files_queued += int(_scalar(conn, "SELECT COUNT(*) FROM files WHERE package_id=? "
                                             "AND deep_status='queued'", (pid,)))
            sels += int(_scalar(conn, "SELECT COUNT(*) FROM selections WHERE package_id=? "
                                      "AND status IN ('queued','running')", (pid,)))
        if not apply:
            return {"packages": len(pids), "queue_rows": alive,
                    "files_reset": files_queued, "selections": sels}
        # **复用 store.cancel_package**，修复脚本不另写一套取消口径。
        done = {"queue_rows": 0, "selections": 0, "files_reset": 0}
        for pid in pids:
            detail = store.cancel_package(pid) or {}
            done["queue_rows"] += int(detail.get("queue_rows") or 0)
            done["selections"] += int(detail.get("selections") or 0)
            done["files_reset"] += int((detail.get("deep_files") or 0) + (detail.get("quick_files") or 0))
        # 历史遗留：早期把取消包的队列项标成了 failed + 那条固定文案，
        # 归一到 cancelled 词表（幂等；条件写死，不会碰到真正的失败项）。
        with closing(store._connect()) as conn:
            cur = conn.execute(
                "UPDATE queue SET status='cancelled' WHERE status='failed' "
                "AND error='所属数据包已取消，队列项作废' "
                "AND package_id IN (%s)" % ",".join("?" for _ in pids or ['']),
                tuple(pids or ['']))
            done["legacy_failed_to_cancelled"] = int(cur.rowcount or 0)
            conn.commit()
        done["packages"] = len(pids)
        return done


# ---------------------------------------------------------------------------
# R3：selection 批次结算（复用 Worker 的口径）
# ---------------------------------------------------------------------------
def r3_selections(store, apply=False):
    """对所有 batch_total=0 的 selection 重算批次统计与终态。

    直接构造 Worker 并调用它自己的 `_finish_selection`，保证与线上口径完全一致
    （含 `summary_quality` 参与 complete/degrade 的判定）。已取消包的 selection
    由 R2 处理，这里跳过。
    """
    with closing(store._connect()) as conn:
        targets = [dict(r) for r in _rows(
            conn, "SELECT s.id,s.package_id FROM selections s "
                  "JOIN packages p ON p.id=s.package_id "
                  "WHERE (s.batch_total=0 OR COALESCE(s.quality,'')='') "
                  "AND s.status NOT IN ('cancelled') "
                  "AND p.status!='cancelled' AND p.control_state!='cancelled'")]
    if not apply:
        return {"selections": len(targets),
                "packages": len({t["package_id"] for t in targets})}
    from large_package_worker import LargePackageWorker
    worker = LargePackageWorker(store=store, poll_seconds=0.5)
    changed = 0
    for pid in sorted({t["package_id"] for t in targets}):
        try:
            # force=True：连"已经是终态但 batch_total 还是 0"的历史行一起补。
            # 口径只有这一处实现（Worker 的 _finish_selection），修复脚本不另写一套。
            if worker._finish_selection(pid, force=True):
                changed += 1
        except Exception:
            LOGGER.exception("R3 selection settle failed: %s", pid)
    with closing(store._connect()) as conn:
        left = int(_scalar(conn, "SELECT COUNT(*) FROM selections WHERE batch_total=0 "
                                 "AND status!='cancelled'"))
    return {"selections": len(targets), "packages_settled": changed, "still_zero": left}


# ---------------------------------------------------------------------------
# R4：member_count 缓存回真
# ---------------------------------------------------------------------------
def r4_member_counts(store, apply=False):
    from services import large_package_semantic as semantic
    with closing(store._connect()) as conn:
        pid_list = _packages(conn)
        bad = int(_scalar(conn, (
            "SELECT COUNT(*) FROM large_catalog_groups g WHERE g.member_count <> "
            "(SELECT COUNT(*) FROM large_catalog_members m "
            "  WHERE m.package_id=g.package_id AND m.group_id=g.id)")))
        excess = int(_scalar(conn, (
            "SELECT COALESCE(SUM(g.member_count - (SELECT COUNT(*) FROM large_catalog_members m "
            "  WHERE m.package_id=g.package_id AND m.group_id=g.id)),0) FROM large_catalog_groups g "
            "WHERE g.member_count > (SELECT COUNT(*) FROM large_catalog_members m "
            "  WHERE m.package_id=g.package_id AND m.group_id=g.id)")))
    if not apply:
        return {"groups": bad, "virtual_excess": excess}
    fixed = 0
    for pid in pid_list:
        try:
            if semantic.repair_member_counts(store, pid):
                fixed += 1
        except Exception:
            LOGGER.exception("R4 member_count repair failed: %s", pid)
    with closing(store._connect()) as conn:
        left = int(_scalar(conn, (
            "SELECT COUNT(*) FROM large_catalog_groups g WHERE g.member_count <> "
            "(SELECT COUNT(*) FROM large_catalog_members m "
            "  WHERE m.package_id=g.package_id AND m.group_id=g.id)")))
    return {"groups": bad, "virtual_excess": excess, "packages_fixed": fixed, "remaining": left}


# ---------------------------------------------------------------------------
# R5：摘要质量分级
# ---------------------------------------------------------------------------
def _grade(payload_text, deep_status):
    """返回 (summary_quality, summary_coverage)。

    有 `finish_reason` 的就是新格式，可以**精确**分级（截断就是截断）；
    没有的（173 行里 118 行）是老格式，无法证明完整 —— 如实标成
    `legacy_unverified`，绝不冒充 complete。报告里它会单独成一档
    "历史摘要（未分级）"，而不是被混进 unknown。
    """
    if deep_status == "failed":
        return "failed", "none"
    if deep_status != "completed":
        return "", ""
    try:
        data = json.loads(payload_text or "{}")
    except Exception:
        return _LEGACY_QUALITY, _LEGACY_COVERAGE
    if not isinstance(data, dict) or not data:
        return _LEGACY_QUALITY, _LEGACY_COVERAGE
    coverage = str(data.get("coverage") or _LEGACY_COVERAGE)
    if data.get("truncated") or str(data.get("finish_reason") or "") == "length":
        return "truncated", coverage
    if not str(data.get("summary") or "").strip():
        return "empty", coverage
    if "finish_reason" in data and str(data.get("finish_reason")) == "stop":
        return "complete", coverage
    return _LEGACY_QUALITY, _LEGACY_COVERAGE


def r5_summary_quality(store, apply=False):
    with closing(store._connect()) as conn:
        rows = _rows(conn, "SELECT package_id,path,deep_status,deep_summary,"
                           "COALESCE(summary_quality,'') q,COALESCE(summary_coverage,'') c "
                           "FROM files WHERE COALESCE(summary_quality,'')='' "
                           "AND deep_status IN ('completed','failed')")
    plan = {}
    updates = []
    for r in rows:
        qual, cov = _grade(r["deep_summary"], r["deep_status"])
        if not qual:
            continue
        plan[qual] = plan.get(qual, 0) + 1
        updates.append((qual, cov, r["package_id"], r["path"]))
    if not apply:
        return {"rows": len(updates), "by_quality": plan}
    with closing(store._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("UPDATE files SET summary_quality=?,summary_coverage=? "
                         "WHERE package_id=? AND path=?", updates)
        conn.commit()
    return {"rows": len(updates), "by_quality": plan}


# ---------------------------------------------------------------------------
# R6：deferred 的目录行放回重试
# ---------------------------------------------------------------------------
def r6_deferred_catalog(store, apply=False):
    sql = ("SELECT COUNT(*) FROM queue q JOIN files f "
           "ON f.package_id=q.package_id AND f.path=q.path "
           "WHERE q.stage='catalog' AND q.status='completed' AND q.error IS NOT NULL "
           "AND f.deep_status!='failed'")
    with closing(store._connect()) as conn:
        n = int(_scalar(conn, sql))
        if not apply or not n:
            return {"rows": n}
        # attempts 归零只做一次：新代码的 CATALOG_MAX_ATTEMPTS=3 会接管重试，
        # 用尽后自动落 failed，不会再出现"永远停在已完成上、永不重试"。
        cur = conn.execute(
            "UPDATE queue SET status='queued',attempts=0,leased_at=NULL,lease_token=NULL,"
            "updated_at=? WHERE rowid IN ("
            "  SELECT q.rowid FROM queue q JOIN files f "
            "    ON f.package_id=q.package_id AND f.path=q.path "
            "  WHERE q.stage='catalog' AND q.status='completed' AND q.error IS NOT NULL "
            "    AND f.deep_status!='failed')", (time.time(),))
        conn.commit()
        return {"rows": int(cur.rowcount or 0)}


# ---------------------------------------------------------------------------
# R7：标签历史裁剪
# ---------------------------------------------------------------------------
def r7_label_history(store, apply=False):
    from services import large_package_catalog_naming as naming
    with closing(store._connect()) as conn:
        before_rows = int(_scalar(conn, "SELECT COUNT(*) FROM lp_catalog_label_history"))
        before_bytes = int(_scalar(conn, "SELECT COALESCE(SUM(LENGTH(payload)),0) "
                                         "FROM lp_catalog_label_history"))
        pid_list = _packages(conn)
    if not apply:
        keep = 5
        with closing(store._connect()) as conn:
            # 单遍 GROUP BY 的**上界**（不扣掉"恒留第 1 版"那一条/组）。
            # 原来用逐行相关子查询精确统计，140,473 行花了 255 秒 —— 为一个
            # 展示用的数字不值得。
            removable = int(_scalar(conn, (
                "SELECT COALESCE(SUM(CASE WHEN c>? THEN c-? ELSE 0 END),0) FROM ("
                "  SELECT COUNT(*) c FROM lp_catalog_label_history GROUP BY package_id,group_id)"),
                (keep, keep)))
        return {"before_rows": before_rows, "before_mb": round(before_bytes / 1048576.0, 1),
                "removable_rows_upper": removable, "keep_per_group": keep}
    removed = 0
    for pid in pid_list:
        removed += int(naming.prune_history(store, pid) or 0)
    with closing(store._connect()) as conn:
        after_rows = int(_scalar(conn, "SELECT COUNT(*) FROM lp_catalog_label_history"))
    return {"before_rows": before_rows, "before_mb": round(before_bytes / 1048576.0, 1),
            "removed_rows": removed, "after_rows": after_rows}


# ---------------------------------------------------------------------------
# R8：把版本链表从"空表"补成真实链条（历史物化）
# ---------------------------------------------------------------------------
def r8_version_chain(store, apply=False):
    from services.large_package_catalog_naming import validate_catalog
    plan = []
    with closing(store._connect()) as conn:
        pid_list = _packages(conn)
    for pid in pid_list:
        with closing(store._connect()) as conn:
            leaves = int(_scalar(conn, (
                "SELECT COUNT(*) FROM large_catalog_groups g WHERE g.package_id=? AND EXISTS ("
                "  SELECT 1 FROM lp_catalog_labels l WHERE l.package_id=g.package_id "
                "    AND l.group_id=g.id AND (l.revision=g.content_revision OR l.locked=1))"), (pid,)))
            if not leaves:
                continue
            topics = int(_scalar(conn, "SELECT COUNT(*) FROM lp_catalog_branches WHERE package_id=?",
                                 (pid,)))
            deep_done = int(_scalar(conn, "SELECT COUNT(*) FROM files WHERE package_id=? "
                                          "AND deep_status='completed'", (pid,)))
            sel = _one(conn, "SELECT id FROM selections WHERE package_id=? "
                             "ORDER BY created_at DESC LIMIT 1", (pid,))
        try:
            validation = validate_catalog(store, pid)
        except Exception:
            validation = {}
        plan.append({"package_id": pid, "leaf_count": leaves, "topic_count": topics,
                     "deep_completed": deep_done,
                     "selection_id": (str(sel["id"]) if sel else "")})
    if not apply:
        return {"packages": len(plan), "detail": plan}
    written = 0
    for item in plan:
        pid = item["package_id"]
        sig = "%s|%s|%s|%s" % (pid, item["leaf_count"], item["topic_count"], item["deep_completed"])
        catalog_id = "cat-" + hashlib.sha1(sig.encode()).hexdigest()[:16]
        evidence_id = "ev-" + hashlib.sha1(("ev" + sig).encode()).hexdigest()[:16]
        run_id = "run-" + hashlib.sha1(("run" + sig).encode()).hexdigest()[:16]
        now = time.time()
        with closing(store._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO lp_catalog_versions"
                         "(catalog_id,package_id,run_id,input_version,status,validation,"
                         " leaf_count,topic_count,created,published) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (catalog_id, pid, run_id, "legacy-materialized/1", "available",
                          json.dumps(validation, ensure_ascii=False),
                          item["leaf_count"], item["topic_count"], now,
                          1 if _scalar(conn, "SELECT COUNT(*) FROM lp_published_views WHERE package_id=?",
                                       (pid,)) else 0))
            conn.execute("INSERT OR REPLACE INTO lp_evidence_snapshots"
                         "(evidence_id,package_id,selection_id,catalog_id,status,stats,created) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (evidence_id, pid, item["selection_id"], catalog_id, "available",
                          json.dumps({"deep_completed_files": item["deep_completed"],
                                      "leaf_groups": item["leaf_count"],
                                      "topic_branches": item["topic_count"],
                                      "source": "legacy-materialized"}, ensure_ascii=False), now))
            conn.execute("INSERT OR REPLACE INTO lp_runs"
                         "(run_id,package_id,kind,input_version,scope_closed,status,result,"
                         " started,finished,catalog_id,evidence_id) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, pid, "catalog", "legacy-materialized/1", 1, "completed",
                          json.dumps({"materialized": True}, ensure_ascii=False),
                          now, now, catalog_id, evidence_id))
            conn.commit()
        written += 1
    return {"packages": written}


# ---------------------------------------------------------------------------
# R9：把已命名的标签投影到成员文件（DEF-14 的历史回填）
# ---------------------------------------------------------------------------
# 与增量投影（`large_package_catalog_naming.apply()`）**同一口径**：同一批字段、
# 同一个取值 `'classified'`、同样不动 `category`。两处必须保持一致，改动其一
# 时另一处要同步（口径测试见 tests/test_catalog_mode_contract.py）。
def r9_project_labels(store, apply=False):
    """为什么历史行要单独回填：

    增量投影写在"组刚拿到名字"的那一刻（`naming.apply()`）。在那次改动**之前**
    命名的组不会再走一遍 `apply()`，于是它们的成员文件永远停在
    `catalog_status='pending'` —— 生产实测 **13,426 行、6 个包**，
    其中最大的一个包 12,851 行、而它的命名任务**一条都没在排队**
    （早就命名完了）。这就是"目录里有名字、文件却说自己没分类"的最后一层。

    安全性：只补投影字段（`topic` / `portrait.catalog_status` /
    `catalog_group` / `catalog_model` / `catalog_label_method`），
    **不动目录结构、不动 category、不改标签名**；对"目录冻结"
    （`LP_CATALOG_FREEZE_AFTER_CATALOG`）同样安全 —— 冻结的是结构，不是投影。
    幂等：条件恒为"当前仍停在 pending/grouped 且所在组已有标签"，跑完即为 0 行。
    """
    match = ("EXISTS (SELECT 1 FROM large_catalog_members m JOIN lp_catalog_labels l "
             "          ON l.package_id=m.package_id AND l.group_id=m.group_id "
             "         WHERE m.package_id=files.package_id AND m.path=files.path)")
    todo = ("COALESCE(json_extract(files.local_summary,'$.portrait.catalog_status'),'pending') "
            "IN ('pending','grouped')")
    with closing(store._connect()) as conn:
        before = dict(conn.execute(
            "SELECT COALESCE(json_extract(local_summary,'$.portrait.catalog_status'),'<无>'),COUNT(*) "
            "FROM files GROUP BY 1").fetchall())
        todo_rows = int(_scalar(conn, "SELECT COUNT(*) FROM files WHERE %s AND %s" % (match, todo)))
        by_pkg = {str(r[0]): int(r[1]) for r in
                  _rows(conn, "SELECT files.package_id,COUNT(*) FROM files "
                              "WHERE %s AND %s GROUP BY 1 ORDER BY 2 DESC" % (match, todo))}
    if not apply:
        return {"before": before, "todo_rows": todo_rows,
                "packages": len(by_pkg), "top": list(by_pkg.items())[:4]}
    with closing(store._connect()) as conn:
        changed = conn.execute("""
            UPDATE files SET
              topic = (SELECT l.name FROM large_catalog_members m JOIN lp_catalog_labels l
                         ON l.package_id=m.package_id AND l.group_id=m.group_id
                       WHERE m.package_id=files.package_id AND m.path=files.path),
              local_summary = json_set(COALESCE(local_summary,'{}'),
                  '$.portrait.catalog_status','classified',
                  '$.portrait.catalog_group',(SELECT m.group_id FROM large_catalog_members m
                        JOIN lp_catalog_labels l ON l.package_id=m.package_id AND l.group_id=m.group_id
                       WHERE m.package_id=files.package_id AND m.path=files.path),
                  '$.portrait.catalog_model',(SELECT l.model FROM large_catalog_members m
                        JOIN lp_catalog_labels l ON l.package_id=m.package_id AND l.group_id=m.group_id
                       WHERE m.package_id=files.package_id AND m.path=files.path),
                  '$.portrait.catalog_label_method','backfill_from_labels'),
              updated_at = ?
            WHERE %s AND %s
        """ % (match, todo), (time.time(),)).rowcount
        after = dict(conn.execute(
            "SELECT COALESCE(json_extract(local_summary,'$.portrait.catalog_status'),'<无>'),COUNT(*) "
            "FROM files GROUP BY 1").fetchall())
        left = int(_scalar(conn, "SELECT COUNT(*) FROM files WHERE %s AND %s" % (match, todo)))
    return {"before": before, "todo_rows": todo_rows, "changed": int(changed),
            "after": after, "left": left}


# ---------------------------------------------------------------------------
# R10：叶子组几何回填（anchor / representative）
# ---------------------------------------------------------------------------
def r10_group_geometry(store, apply=False):
    """按**当前成员**重算 anchor 与 representative，修掉历史遗留的空质心组。

    为什么需要：split_off_outliers() 原先创建新组时写 anchor=b''，也没有重算
    父组。这类组在 representatives() 里每个成员与质心的相似度全部并列，
    best/boundary 退化成"路径序第一个"，多样性选择同时失效——整组只能靠
    一两份任意文件定名，生产实测因此产出“金鱼草属植物与演员托尼·托德”
    “良性对称性脂肪增多症与命令行界面”这类目录项。代码侧已在成员搬移的
    同一事务里重算；本步骤把历史遗留的组补齐。成员向量已在库，纯算术。
    """
    from services.large_package_catalog_naming import _group_geometry
    todo_sql = (
        "SELECT package_id,id FROM large_catalog_groups "
        "WHERE (anchor IS NULL OR LENGTH(anchor)=0) AND id NOT LIKE 'km-pending-%' "
        "AND id NOT LIKE 'km-other-%' "
        "AND EXISTS(SELECT 1 FROM large_catalog_members m "
        "           WHERE m.package_id=large_catalog_groups.package_id "
        "             AND m.group_id=large_catalog_groups.id AND LENGTH(m.vector)>0) "
        "ORDER BY package_id,id")
    with closing(store._connect()) as conn:
        todo = [tuple(r) for r in _rows(conn, todo_sql)]
    if not apply:
        return {"groups": len(todo)}
    fixed = 0
    with store._lock, closing(store._connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        for package_id, group_id in todo:
            anchor, representative = _group_geometry(conn, package_id, group_id)
            if anchor is None:
                continue
            conn.execute('UPDATE large_catalog_groups SET anchor=?,representative=? '
                         'WHERE package_id=? AND id=?',
                         (anchor, representative, package_id, group_id))
            fixed += 1
        conn.commit()
    with closing(store._connect()) as conn:
        left = len(_rows(conn, todo_sql))
    return {"groups": len(todo), "fixed": fixed, "remaining": left}


STEPS = [
    ("R9", "标签投影到成员文件（DEF-14 回填）", r9_project_labels),("R1", "识别队列与 files 对齐", r1_recognize_reconcile),
         ("R2", "取消包清理", r2_cancelled_packages),
         ("R3", "selection 批次结算", r3_selections),
         ("R4", "member_count 回真", r4_member_counts),
         ("R5", "摘要质量分级", r5_summary_quality),
         ("R6", "deferred 目录行重试", r6_deferred_catalog),
         ("R7", "标签历史裁剪", r7_label_history),
         ("R8", "版本链物化", r8_version_chain),
         ("R10", "叶子组几何回填（anchor/representative）", r10_group_geometry)]


def run(store, apply=False, only=None):
    result = {}
    for code, title, fn in STEPS:
        if only and code not in only:
            continue
        t = time.time()
        try:
            out = fn(store, apply=apply)
        except Exception as exc:
            LOGGER.exception("repair step failed: %s", code)
            out = {"error": repr(exc)[:300]}
        out["seconds"] = round(time.time() - t, 2)
        result[code] = {"title": title, **out}
        print("  %s %-22s %s" % (code, title, json.dumps(out, ensure_ascii=False)))
    return result


def vacuum(store):
    path = str(store.db_path)
    with closing(store._connect()) as conn:
        before = _scalar(conn, "PRAGMA page_count") * _scalar(conn, "PRAGMA page_size")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn = __import__("sqlite3").connect(path, isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    with closing(store._connect()) as conn:
        after = _scalar(conn, "PRAGMA page_count") * _scalar(conn, "PRAGMA page_size")
    return {"before_mb": round(before / 1048576.0, 1), "after_mb": round(after / 1048576.0, 1),
            "saved_mb": round((before - after) / 1048576.0, 1)}


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="历史数据一次性修复")
    parser.add_argument("--db", default=None, help="数据库路径（默认取运行时状态目录）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    parser.add_argument("--only", default="", help="只跑指定步骤，逗号分隔，如 R1,R4")
    parser.add_argument("--vacuum", action="store_true", help="仅执行 VACUUM（请先停服务）")
    args = parser.parse_args(argv)

    # LargePackageStore 自身就把默认库定位在 Config.DATA_DIR / large_package.db，
    # 这里不再自己拼路径，避免两处口径不一致。
    store = LargePackageStore(args.db)
    db = str(store.db_path)
    if not os.path.exists(db):
        print("数据库不存在：%s" % db)
        return 2
    only = tuple(x.strip().upper() for x in args.only.split(",") if x.strip())
    print("数据库 %s（%.1f MB）  模式=%s" % (db, os.path.getsize(db) / 1048576.0,
                                            "APPLY" if args.apply else "DRY-RUN"))
    if args.vacuum:
        print("  VACUUM %s" % json.dumps(vacuum(store), ensure_ascii=False))
        return 0
    run(store, apply=args.apply, only=only or None)
    if args.apply:
        with closing(store._connect()) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print("已写库。建议接着执行：python -m services.large_package_repair --vacuum（需停服务）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
