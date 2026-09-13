"""Content-derived navigation for the isolated large-package pipeline.

Only directory fields and the catalog queue are updated here. Semantic embeddings
provide grouping; extractive labels require no generative model calls.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from contextlib import closing

VERSION = "large-semantic-catalog/2"

def _text(value, limit=6000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def material(row):
    local = row.get("local_summary") or {}
    info = local.get("portrait") or {}
    deep = row.get("deep_summary") or {}
    if row.get("sensitive") or info.get("recognition_status") == "restricted":
        return None
    parts = [p for p in info.get("evidence", []) if isinstance(p, dict) and p.get("kind") not in {"member_names", "attachment"}]
    if len(parts) > 3:
        positions = sorted({0, len(parts)//2, len(parts)-1})
        sampled = [parts[i] for i in positions]
    else:
        sampled = parts
    text = "\n".join(_text(p.get("text"), 1150) for p in sampled)
    if not parts and not info.get("evidence"):
        text = local.get("sample") or ""
    deep_ready = row.get("deep_status") == "completed" and deep.get("summary_type") != "local_deep_failed"
    summary = _text(deep.get("summary"), 1800) if deep_ready else ""
    full_preview = _text(deep.get("text_preview"), 6000) if deep_ready else ""
    if full_preview:
        text = full_preview
    text = _text(text, 3600)
    fields = [_text(x, 100) for x in info.get("fields", [])[:20]]
    headings = [_text(x, 140) for x in info.get("headings", [])[:8]]
    if len(text) < 20 and len(summary) < 20 and len(fields) < 2:
        return None
    value = {"text": text, "deep_summary": summary, "fields": fields, "headings": headings,
             "source": "deep_summary" if summary else "deep_content" if full_preview else "content_preview"}
    # File names and old rule labels are intentionally absent from model input.
    digest = hashlib.sha256((VERSION + json.dumps(value, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
    return value, digest


def prepare_update(previous, fields):
    """Return directory changes for new content without changing parse results."""
    row = {**previous, **fields}
    local = copy.deepcopy(row.get("local_summary") or {})
    info = local.setdefault("portrait", {})
    prior_info = (previous.get("local_summary") or {}).get("portrait") or {}
    source = material(row)
    if not source:
        # No content is never permission to assign an industry from a filename.
        info.update(schema=info.get("schema") or "large-content-portrait/1", catalog_version=VERSION,
                    catalog_status="no_content", category="待识别内容", topic="待识别内容",
                    classification_basis="尚无足够可读内容，等待内容识别", confidence=0)
        if row.get("sensitive") or info.get("recognition_status") == "restricted":
            info.update(category="受限文件", topic="受限文件", recognition_status="restricted")
        else:
            info.setdefault("recognition_status", "metadata_only")
        local["category"] = info["category"]
        return {**fields, "local_summary": local, "category": info["category"], "topic": info["topic"],
                "confidence": 0, "recognition_status": info["recognition_status"]}, False
    value, digest = source
    if prior_info.get("catalog_input_hash") == digest:
        # Refreshing a preview must not replace a classification based on deeper content.
        for key in ("schema", "topic", "category", "quality", "confidence", "recognition_status", "classification_basis", "grouping_basis"):
            if key in prior_info:
                info[key] = prior_info[key]
        info.update({k: v for k, v in prior_info.items() if k.startswith("catalog_")})
        local["category"] = info["category"]
        return {**fields, "local_summary": local, "category": info["category"], "topic": info["topic"],
                "confidence": info.get("confidence", 0), "recognition_status": info.get("recognition_status", "needs_review")}, False
    clues = value["fields"][:3] or value["headings"][:1]
    clue = " / ".join(clues) or value["deep_summary"][:100] or value["text"][:100]
    # Keep a good previous topic visible until a deeper result replaces it.
    retained = prior_info.get("catalog_status") == "classified"
    info.update(schema=info.get("schema") or "large-content-portrait/1", catalog_version=VERSION,
                catalog_input_hash=digest, catalog_source=value["source"], catalog_status="pending",
                category=prior_info.get("category") if retained else "内容待归纳",
                topic=prior_info.get("topic") if retained else "内容线索：" + _text(clue, 100),
                quality="content", recognition_status="needs_review", confidence=0,
                classification_basis="已有内容线索，等待按主要内容归纳主题", grouping_basis="",
                catalog_evidence=[], catalog_error="")
    local["category"] = info["category"]
    return {**fields, "local_summary": local, "category": info["category"], "topic": info["topic"],
            "confidence": 0, "recognition_status": "needs_review"}, True


def get_file(store, package_id, path):
    with closing(store._connect()) as conn:
        row = conn.execute("SELECT * FROM files WHERE package_id=? AND path=?", (package_id,path)).fetchone()
    return store._row(row) if row else None


def _save(store, package_id, path, digest, result=None, error=""):
    """Apply only navigation fields, with a content fingerprint concurrency check."""
    with store._lock, closing(store._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        raw = conn.execute("SELECT * FROM files WHERE package_id=? AND path=?", (package_id, path)).fetchone()
        if not raw:
            conn.execute("DELETE FROM queue WHERE package_id=? AND path=? AND stage='catalog'", (package_id, path))
            conn.commit()
            return False
        row = store._row(raw)
        local = row["local_summary"]
        info = local.get("portrait") or {}
        if info.get("catalog_input_hash") != digest:
            conn.execute("UPDATE queue SET status='queued',leased_at=NULL WHERE package_id=? AND path=? AND stage='catalog'", (package_id, path))
            conn.commit()
            return False
        if result:
            label = result["topic"]
            info.update(category=result["category"], topic=label, quality="identified", recognition_status="identified",
                        catalog_status="classified", catalog_model=result["model"], catalog_label_method="source_phrases", catalog_error="",
                        catalog_evidence=[{"source_path": path, "location": "深度摘要" if p["source"] == "deep_summary" else "内容片段",
                                           "text": p["text"], "preview_only": info["catalog_source"] == "content_preview"} for p in result["quotes"]],
                        classification_basis=result["reason"], grouping_basis="按内容语义相似性分组；目录名直接提取自组内代表内容")
        else:
            info.update(catalog_status="deferred", catalog_error=_text(error, 240), recognition_status="needs_review",
                        classification_basis="暂保留内容线索，主题归纳待补充识别" if info.get("catalog_status") != "classified" else info.get("classification_basis"))
        info["catalog_updated_at"] = time.time()
        local.update(portrait=info, category=info["category"])
        conn.execute("""UPDATE files SET local_summary=?,category=?,topic=?,recognition_status=?,confidence=0,updated_at=?
          WHERE package_id=? AND path=?""", (json.dumps(local, ensure_ascii=False), info["category"], info["topic"], info["recognition_status"], time.time(), package_id, path))
        conn.execute("UPDATE queue SET status='completed',error=?,updated_at=? WHERE package_id=? AND path=? AND stage='catalog'",
                     (_text(error, 240) or None, time.time(), package_id, path))
        conn.commit()
    store._catalog_cache.pop(package_id, None)
    return True


def refresh_existing(store, package_id):
    """Queue a directory-only upgrade from saved previews/summaries, without reparsing."""
    after = ""
    count = 0
    while True:
        with closing(store._connect()) as conn:
            rows = conn.execute("SELECT * FROM files WHERE package_id=? AND path>? ORDER BY path LIMIT 100", (package_id, after)).fetchall()
        if not rows:
            return count
        for raw in rows:
            row = store._row(raw)
            store.update_file(package_id, row["path"], local_summary=row["local_summary"])
            count += 1
        after = rows[-1]["path"]
