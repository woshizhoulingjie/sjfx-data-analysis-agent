"""Append a directory selection to one completed standard import.

Uses the existing parser/summary jobs, and never owns a model or a worker.
All scope and queue writes are committed together. The web reconciler only
publishes batches explicitly created through this opt-in feature.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

from services.processing_queue import deep_processing_eligible

log = logging.getLogger("sjfx")
STABLE_STATES = {"completed", "partial", "deep_update_available"}
ACTIVE_TYPES = ("analyze_package", "scan_and_analyze", "generate_summary", "generate_report")


class SupplementError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _json(raw, default):
    return json.loads(raw) if raw else default


def _path(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise SupplementError("目录或文件路径无效。", 400)
    if value.startswith("/") or ".." in value.split("/"):
        raise SupplementError("只能选择当前数据包内的目录或文件。", 400)
    return str(PurePosixPath(value))


def _scopes(payload):
    values = payload.get("paths")
    if not isinstance(values, list) or not values or len(values) > 2000:
        raise SupplementError("请至少选择一个目录或文件（最多 2000 个选择项）。", 400)
    paths = sorted({_path(value) for value in values})
    # Directory overlap is intentional; the final file set is resolved once.
    return paths


def _context(conn, scan_id, owner_id):
    scan = conn.execute("SELECT owner_id FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not scan or str(scan["owner_id"] or "legacy") != owner_id:
        raise SupplementError("数据包不存在或不属于当前用户。", 404)
    task = conn.execute("SELECT * FROM import_tasks WHERE scan_id=?", (scan_id,)).fetchone()
    selection = conn.execute("SELECT * FROM scan_selections WHERE scan_id=?", (scan_id,)).fetchone()
    if not task or not selection:
        raise SupplementError("请先完成首次文件选择和解析。")
    task, selection = dict(task), dict(selection)
    checkpoint = _json(task.get("checkpoint"), {})
    included = set(_json(selection.get("included_paths"), []))
    # Historical endpoints may have recorded a scope in either location.
    included.update(checkpoint.get("selected_paths") or [])
    if checkpoint.get("large_selection") or (checkpoint.get("selection_plan") or {}).get("schema_version"):
        raise SupplementError("此入口用于普通数据包；大数据包请使用原有补充深度解析入口。")
    active = int(conn.execute(
        "SELECT COUNT(*) FROM analysis_jobs WHERE scan_id=? AND task_type IN (?,?,?,?) "
        "AND status IN ('queued','running','cancelling')", (scan_id, *ACTIVE_TYPES),
    ).fetchone()[0])
    control = conn.execute("SELECT state,pause_requested FROM package_processing_controls WHERE scan_id=?", (scan_id,)).fetchone()
    paused = bool(control and (control["pause_requested"] or control["state"] in {"paused", "cancelled"}))
    ready = task["status"] in STABLE_STATES and not active and not paused and selection["status"] in {"confirmed", "running", "completed"}
    return {"task": task, "selection": selection, "checkpoint": checkpoint,
            "included": included, "ready": ready, "active_jobs": active}


def _entries(conn, scan_id, parent="."):
    values = [scan_id]
    condition = ""
    if parent != ".":
        # Escape LIKE syntax in real file names, including '%' and '_'.
        escaped = parent.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        condition = " AND (i.node_path=? OR i.node_path LIKE ? ESCAPE '\\')"
        values.extend([parent, escaped.rstrip("/") + "/%"])
    return conn.execute(
        "SELECT i.node_path,i.kind,json_extract(i.payload,'$.size') AS size,"
        "f.status AS analysis_status,f.retryable,f.attempt_count,f.next_retry_at,"
        "COALESCE(w.promotion_allowed,1) AS promotion_allowed,w.safety_status,w.reasons,"
        "w.selection_state,w.priority_source,w.light_index_status "
        "FROM inventory_entries i LEFT JOIN file_analysis_states f "
        "ON f.scan_id=i.scan_id AND f.node_path=i.node_path "
        "LEFT JOIN file_workflow_states w ON w.scan_id=i.scan_id AND w.node_path=i.node_path "
        "WHERE i.scan_id=? AND i.kind IN ('file','logical_file','directory')" + condition +
        " ORDER BY i.node_path", values,
    )


def _eligible(row):
    item = dict(row)
    workflow = {key: item.get(key) for key in (
        "promotion_allowed", "safety_status", "selection_state", "priority_source", "light_index_status")}
    workflow["reasons"] = _json(item.get("reasons"), [])
    analysis = {"status": item.get("analysis_status"), "retryable": item.get("retryable"),
                "attempt_count": item.get("attempt_count"), "next_retry_at": item.get("next_retry_at")}
    return deep_processing_eligible(workflow, analysis)


def _batch_status(conn, scan_id, context):
    batch = dict(context["checkpoint"].get("directory_supplement") or {})
    if not batch:
        return None
    job_id = batch.get("job_id")
    job = conn.execute("SELECT status,progress,message FROM analysis_jobs WHERE id=? AND scan_id=?", (job_id, scan_id)).fetchone()
    report_id = batch.get("report_job_id")
    report_job = conn.execute("SELECT status FROM analysis_jobs WHERE id=? AND scan_id=?", (report_id, scan_id)).fetchone() if report_id else None
    phase = context["task"]["status"]
    expected = int(batch.get("selection_version") or 0)
    published = bool(batch.get("published")) and int(context["selection"]["version"]) == expected
    if report_job and report_job["status"] == "completed":
        row = conn.execute("SELECT payload FROM summaries WHERE scan_id=? AND node_path='.' AND summary_type='deep_report'", (scan_id,)).fetchone()
        report = _json(row["payload"], {}) if row else {}
        scope = report.get("report_scope") or {}
        published = int(scope.get("selection_version") or 0) == expected and set(scope.get("selected_paths") or []) == context["included"]
    result = {"job_id": job_id, "new_count": int(batch.get("new_count") or 0),
              "selection_version": expected, "phase": phase, "published": published,
              "job_status": job["status"] if job else None, "report_status": report_job["status"] if report_job else None}
    if job:
        result.update(progress=job["progress"], message=job["message"])
    return result


def status(storage, scan_id, owner_id):
    with storage._connect() as conn:
        context = _context(conn, scan_id, owner_id)
        return {"scan_id": scan_id, "version": int(context["selection"]["version"]),
                "ready": context["ready"], "task_status": context["task"]["status"],
                "active_jobs": context["active_jobs"], "batch": _batch_status(conn, scan_id, context),
                "reason": "可补选未处理的目录和文件。" if context["ready"] else "请等待当前批次处理完成后再补选。"}


def browse(storage, scan_id, owner_id, parent=".", offset=0, limit=100):
    parent = _path(parent)
    offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
    with storage._connect() as conn:
        context = _context(conn, scan_id, owner_id)
        groups = {}
        total_files = remaining = selected_count = 0
        prefix = "" if parent == "." else parent.rstrip("/") + "/"
        for row in _entries(conn, scan_id, parent):
            path = str(row["node_path"])
            if path in {".", parent}:
                continue
            relative = path[len(prefix):]
            child_name, _, tail = relative.partition("/")
            child_path = prefix + child_name
            kind = "directory" if tail or row["kind"] == "directory" else "file"
            child = groups.setdefault(child_path, {"path": child_path, "name": child_name,
                "kind": kind, "file_count": 0, "selected_count": 0, "remaining_count": 0, "completed_count": 0})
            if kind == "directory":
                child["kind"] = kind
            if row["kind"] == "directory":
                continue
            selected = path in context["included"]
            eligible = not selected and _eligible(row)
            child["file_count"] += 1
            child["selected_count"] += int(selected)
            child["remaining_count"] += int(eligible)
            child["completed_count"] += int(row["analysis_status"] == "completed")
            total_files += 1
            selected_count += int(selected)
            remaining += int(eligible)
        items = sorted(groups.values(), key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["path"]))
        batch = _batch_status(conn, scan_id, context)
    return {"scan_id": scan_id, "parent": parent, "items": items[offset:offset + limit],
            "total": len(items), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(items) else None,
            "file_count": total_files, "remaining_count": remaining, "selected_count": selected_count,
            "version": int(context["selection"]["version"]), "ready": context["ready"],
            "reason": "可补选未处理的目录和文件。" if context["ready"] else "请等待当前批次处理完成后再补选。",
            "task_status": context["task"]["status"], "active_jobs": context["active_jobs"], "batch": batch}


def _plan(conn, scan_id, context, paths):
    roots = set(paths)
    matched = set()
    new_paths = []
    selected_count = skipped_count = new_bytes = 0
    for row in _entries(conn, scan_id):
        path = str(row["node_path"])
        ancestry = set(PurePosixPath(path).parents)
        hits = roots.intersection({path, ".", *(str(parent) for parent in ancestry)})
        if not hits:
            continue
        matched.update(hits)
        if row["kind"] == "directory":
            continue
        if path in context["included"]:
            selected_count += 1
        elif _eligible(row):
            new_paths.append(path)
            new_bytes += int(row["size"] or 0)
        else:
            skipped_count += 1
    if roots - matched:
        raise SupplementError("选择的目录或文件不在当前导入清单中，请刷新后重试。", 400)
    new_paths = sorted(set(new_paths))
    version = int(context["selection"]["version"])
    digest = hashlib.sha256(json.dumps([version, new_paths, new_bytes], ensure_ascii=False).encode()).hexdigest()
    return {"new_paths": new_paths, "new_count": len(new_paths), "new_bytes": new_bytes,
            "already_selected_count": selected_count, "skipped_count": skipped_count,
            "version": version, "preview_token": digest}


def preview(storage, scan_id, owner_id, payload):
    paths = _scopes(payload)
    with storage._connect() as conn:
        context = _context(conn, scan_id, owner_id)
        if not context["ready"]:
            raise SupplementError("请等待当前批次处理完成后再补选。")
        plan = _plan(conn, scan_id, context, paths)
    # Counts are complete; the bounded sample is for display only.
    return {key: value for key, value in plan.items() if key != "new_paths"} | {"sample_paths": plan["new_paths"][:20]}


def submit(storage, scan_id, owner_id, payload):
    paths = _scopes(payload)
    request_id = str(payload.get("request_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{12,128}", request_id):
        raise SupplementError("提交标识无效，请刷新后重试。", 400)
    request_digest = hashlib.sha256(json.dumps(paths, ensure_ascii=False).encode()).hexdigest()
    with storage.lock, storage._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        context = _context(conn, scan_id, owner_id)
        old = conn.execute("SELECT id,options FROM analysis_jobs WHERE scan_id=? AND owner_id=? "
                           "AND json_extract(options,'$.directory_supplement_request_id')=? ORDER BY rowid DESC LIMIT 1",
                           (scan_id, owner_id, request_id)).fetchone()
        if old:
            old_options = _json(old["options"], {})
            if old_options.get("directory_supplement_digest") != request_digest:
                raise SupplementError("提交标识已用于不同的选择，请重新确认。")
            return {"accepted": True, "reused": True, "job_id": old["id"],
                    "new_count": len(old_options.get("target_paths") or []), "version": old_options.get("selection_version")}
        if not context["ready"]:
            raise SupplementError("请等待当前批次处理完成后再补选。")
        if payload.get("version") != int(context["selection"]["version"]):
            raise SupplementError("分析范围已更新，请刷新目录并重新确认。")
        plan = _plan(conn, scan_id, context, paths)
        if payload.get("preview_token") != plan["preview_token"]:
            raise SupplementError("新增文件范围已变化，请重新预览后提交。")
        if not plan["new_paths"]:
            return {"accepted": False, "new_count": 0, "message": "所选范围没有新增的可解析文件。"}
        selected = sorted(context["included"].union(plan["new_paths"]))
        excluded = sorted(set(_json(context["selection"].get("excluded_paths"), [])).difference(selected))
        version = plan["version"] + 1
        job_id = uuid.uuid4().hex[:12]
        options = {"target_paths": plan["new_paths"], "workflow_source": "selected_file_parse",
                   "scope_label": "原始目录补选（{} 个新增文件）".format(plan["new_count"]),
                   "parse_mode": "accurate", "continue_full": False, "supplement": True,
                   "selection_version": version, "directory_supplement_request_id": request_id,
                   "directory_supplement_digest": request_digest}
        checkpoint = dict(context["checkpoint"])
        checkpoint.update(selected_paths=selected, selection_version=version, supplement_paths=plan["new_paths"],
                          preliminary_node_job_ids=[], deep_summary_node_job_ids=[], preliminary_overview_job_id=None)
        if "deep_selected_paths" in checkpoint:
            checkpoint["deep_selected_paths"] = selected
        checkpoint["directory_supplement"] = {"job_id": job_id, "new_count": plan["new_count"],
            "selection_version": version, "auto_publish": True, "created_at": time.time()}
        # Keep completed file jobs for resume accounting; only node/report products
        # whose membership changed are rebuilt by the established pipeline.
        conn.execute("UPDATE scan_selections SET status='confirmed',version=?,included_paths=?,excluded_paths=?,"
                     "confirmed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE scan_id=?",
                     (version, json.dumps(selected, ensure_ascii=False), json.dumps(excluded, ensure_ascii=False), scan_id))
        conn.execute("UPDATE import_tasks SET status='parsing_selected',current_phase='parsing_selected',checkpoint=?,updated_at=? WHERE scan_id=?",
                     (json.dumps(checkpoint, ensure_ascii=False), time.time(), scan_id))
        conn.execute("INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options,owner_id,priority,created_at) "
                     "VALUES (?,?,'analyze_package','queued','queued',0,?,?,?, ?,CURRENT_TIMESTAMP)",
                     (job_id, scan_id, "补选范围已保存，等待解析", json.dumps(options, ensure_ascii=False), owner_id,
                      storage._job_priority("analyze_package", options)))
        conn.execute("INSERT INTO package_processing_controls(scan_id,state,pause_requested,reason) VALUES (?,'parsing_selected',0,?) "
                     "ON CONFLICT(scan_id) DO UPDATE SET state=excluded.state,pause_requested=0,reason=excluded.reason",
                     (scan_id, "正在解析本次补选文件。"))
    return {"accepted": True, "reused": False, "job_id": job_id, "new_count": plan["new_count"], "version": version}


def publish_ready(storage):
    """Queue the existing report worker once for each opted-in, finished batch."""
    with storage._connect(timeout_ms=1000) as conn:
        candidates = conn.execute("SELECT scan_id,owner_id FROM import_tasks WHERE status='deep_update_available' "
            "AND json_extract(checkpoint,'$.directory_supplement.auto_publish')=1 LIMIT 20").fetchall()
    for candidate in candidates:
        scan_id, owner_id = candidate["scan_id"], candidate["owner_id"]
        with storage.lock, storage._connect(timeout_ms=1000) as conn:
            conn.execute("BEGIN IMMEDIATE")
            context = _context(conn, scan_id, owner_id)
            batch = dict(context["checkpoint"].get("directory_supplement") or {})
            version = int(context["selection"]["version"])
            if not context["ready"] or not batch.get("auto_publish") or int(batch.get("selection_version") or 0) != version:
                continue
            if batch.get("report_job_id"):
                report_status = conn.execute("SELECT status FROM analysis_jobs WHERE id=? AND scan_id=?", (batch["report_job_id"], scan_id)).fetchone()
                if report_status and report_status["status"] == "completed":
                    row = conn.execute("SELECT payload FROM summaries WHERE scan_id=? AND node_path='.' AND summary_type='deep_report'", (scan_id,)).fetchone()
                    scope = (_json(row["payload"], {}) if row else {}).get("report_scope") or {}
                    if int(scope.get("selection_version") or 0) != version or set(scope.get("selected_paths") or []) != context["included"]:
                        continue
                    # The existing worker has published the current complete scope.
                    conn.execute("DELETE FROM package_overviews WHERE scan_id=?", (scan_id,))
                    batch.update(auto_publish=False, published=True)
                    checkpoint = dict(context["checkpoint"], directory_supplement=batch)
                    conn.execute("UPDATE import_tasks SET checkpoint=?,updated_at=? WHERE scan_id=?", (json.dumps(checkpoint, ensure_ascii=False), time.time(), scan_id))
                # Failed reports remain visible/retryable in the existing task UI.
                continue
            deep_paths = set()
            for row in conn.execute("SELECT node_path,payload FROM summaries WHERE scan_id=? AND summary_type='deep_file_summary'", (scan_id,)):
                summary = _json(row["payload"], {})
                if summary.get("deep_analysis") and (summary.get("analysis_level") == "deep" or summary.get("analysis_depth") in {"deep", "deep_document"} or summary.get("generated_by") == "model-deep-analysis"):
                    deep_paths.add(row["node_path"])
            if not context["included"] or not context["included"].issubset(deep_paths):
                continue
            node_ids = context["checkpoint"].get("deep_summary_node_job_ids") or []
            if any(not (row := conn.execute("SELECT status FROM analysis_jobs WHERE id=? AND scan_id=?", (job_id, scan_id)).fetchone()) or row["status"] != "completed" for job_id in node_ids):
                continue
            report_id = uuid.uuid4().hex[:12]
            options = {"workflow_source": "deep_results_report", "selection_version": version,
                       "resume_state": "deep_update_available", "directory_supplement_auto_publish": True}
            conn.execute("INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options,owner_id,priority,created_at) "
                         "VALUES (?,?,'generate_report','queued','queued',0,?,?,?, ?,CURRENT_TIMESTAMP)",
                         (report_id, scan_id, "补选摘要已完成，自动更新目录和概览", json.dumps(options), owner_id,
                          storage._job_priority("generate_report", options)))
            batch["report_job_id"] = report_id
            checkpoint = dict(context["checkpoint"], directory_supplement=batch, deep_overview_job_id=report_id)
            conn.execute("UPDATE import_tasks SET status='deep_overview_updating',current_phase='deep_overview_updating',checkpoint=?,updated_at=? WHERE scan_id=?",
                         (json.dumps(checkpoint, ensure_ascii=False), time.time(), scan_id))
            conn.execute("UPDATE package_processing_controls SET state='deep_overview_updating',reason=? WHERE scan_id=?", ("正在自动更新补选后的目录和概览。", scan_id))




@asynccontextmanager
async def publication_watch(storage):
    async def watch():
        while True:
            try:
                await asyncio.to_thread(publish_ready, storage)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("补选结果更新检查失败，将在下一轮重试", exc_info=True)
            await asyncio.sleep(5)
    task = asyncio.create_task(watch(), name="sjfx-directory-supplement-publication")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
