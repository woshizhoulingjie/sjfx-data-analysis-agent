"""Read-only package context for grounded conversations.

This module deliberately has no write path.  It adapts the existing standard
scan storage and the isolated large-package store into one bounded context for
the conversation model.  Existing parsing, summaries and structured profiles
remain the source of truth.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional


def _text(value: Any, limit: int = 900) -> str:
    if isinstance(value, Mapping):
        parts = []
        for key in ("title", "summary", "text", "sample", "abstract", "conclusion", "key_findings", "keywords"):
            if key in value and value[key] not in (None, "", [], {}):
                item = value[key]
                if isinstance(item, (list, tuple)):
                    item = "、".join(str(x) for x in item[:20])
                elif isinstance(item, Mapping):
                    item = json.dumps(item, ensure_ascii=False)
                parts.append(f"{key}: {item}")
        if not parts:
            parts = [json.dumps(dict(value), ensure_ascii=False)]
        value = "；".join(parts)
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit]


def _coverage(analysis: Mapping[str, Any], summary_count: int, file_count: int) -> dict:
    raw = dict(analysis.get("coverage") or {}) if isinstance(analysis, Mapping) else {}
    return {
        "scope": "package",
        "total_files": int(raw.get("inventory_files") or raw.get("total_files") or file_count or 0),
        "parsed_files": int(raw.get("parsed_files") or 0),
        "deep_analyzed_files": int(raw.get("deep_analyzed_files") or 0),
        "summary_records": int(summary_count or 0),
        "failed_files": int(raw.get("failed_files") or raw.get("parse_failed_files") or 0),
        "source": "existing_storage",
    }


def build_standard_context(storage: Any, scan_id: str, max_summary_chars: int = 700) -> dict:
    """Build a read-only whole-scan context from existing persisted artifacts."""
    overview = storage.get_package_overview(scan_id) or {}
    analysis = storage.get_analysis_overview(scan_id) or {}
    summaries = []
    offset = 0
    # Read every summary page.  The model-facing digest is compacted below, but
    # the coverage count proves that the whole package was considered.
    while True:
        page = storage.list_summaries_page(scan_id, offset=offset, limit=500)
        items = page.get("items") or []
        summaries.extend(items)
        next_offset = page.get("next_offset")
        if next_offset is None or not items:
            break
        offset = next_offset
    files = []
    for item in summaries:
        payload = item.get("payload") or {}
        path = str(item.get("path") or payload.get("source_path") or "").strip()
        if not path:
            continue
        files.append({
            "path": path,
            "name": Path(path.split("::", 1)[-1]).name,
            "summary_type": item.get("type"),
            "summary": _text(payload, max_summary_chars),
        })
    # A package can contain files whose summary job is still pending or failed.
    # Include the persisted inventory as zero-summary records so package scope
    # remains complete without triggering parsing or summary work.
    known = {item["path"] for item in files}
    try:
        inventory_paths = storage.inventory_paths_under(scan_id, ".") or []
    except Exception:
        inventory_paths = []
    for raw_path in inventory_paths:
        path = str(raw_path or "").strip()
        if path and path not in known:
            files.append({
                "path": path,
                "name": Path(path.split("::", 1)[-1]).name,
                "summary_type": None,
                "summary": "",
            })
    files.sort(key=lambda item: item["path"])
    return {
        "package_type": "standard",
        "package_id": str(scan_id),
        "package_overview": overview,
        "analysis_overview": analysis,
        "coverage": _coverage(analysis, len(summaries), len(files)),
        "file_summaries": files,
    }


def build_large_context(store: Any, package_id: str, max_summary_chars: int = 700) -> Optional[dict]:
    """Build context from the isolated large-package manifest and summaries."""
    package = store.get(package_id)
    if not package:
        return None
    counts = store.counts(package_id) or {}
    files = []
    offset = 0
    while True:
        page = store.list_files(package_id, limit=500, offset=offset)
        if not page:
            break
        for row in page:
            local = row.get("local_summary") or {}
            deep = row.get("deep_summary") or {}
            summary = deep if deep and deep.get("summary_type") not in {"model_pending"} else local
            files.append({
                "path": row.get("path"),
                "name": row.get("name"),
                "summary_type": summary.get("summary_type") if isinstance(summary, Mapping) else None,
                "summary": _text(summary, max_summary_chars),
                "quick_status": row.get("quick_status"),
                "deep_status": row.get("deep_status"),
                "category": row.get("category"),
            })
        if len(page) < 500:
            break
        offset += len(page)
    return {
        "package_type": "large",
        "package_id": str(package_id),
        "package_info": {"name": Path(str(package.get("root_path") or "")).name, "root": package.get("root_path"), "status": package.get("status"), "phase": package.get("phase")},
        "coverage": {"scope": "package", "total_files": int(counts.get("total_files") or len(files)), "quick": counts.get("quick") or {}, "deep": counts.get("deep") or {}, "source": "large_package.db"},
        "file_summaries": files,
    }


def model_digest(context: Mapping[str, Any], max_chars: int = 30000) -> str:
    """Serialize package facts for the model without dropping coverage metadata."""
    payload = {
        "package_type": context.get("package_type"),
        "package_id": context.get("package_id"),
        "package_info": context.get("package_info") or {},
        "package_overview": context.get("package_overview") or {},
        "analysis_overview": context.get("analysis_overview") or {},
        "coverage": context.get("coverage") or {},
        "file_summaries": context.get("file_summaries") or [],
        "instruction": "这些是当前整个数据包的已持久化资料。不得把未列出的文件当作已检查，也不得把部分覆盖说成完整覆盖。",
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= max_chars else text[: max_chars - 80] + "…（模型上下文已压缩，覆盖数量保持真实）"

