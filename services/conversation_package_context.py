"""Read-only package context for grounded conversations.

This module deliberately has no write path.  It adapts the existing standard
scan storage and the isolated large-package store into one bounded context for
the conversation model.  Existing parsing, summaries and structured profiles
remain the source of truth.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from services import answer_quality as aq
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


READING_SCHEMA_VERSION = "package-reading/2.0"
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,8}")
_STOP_WORDS = {
    "这个", "那个", "资料", "文件", "文档", "数据包", "内容", "分析",
    "相关", "进行", "已经", "可以", "需要", "问题", "情况", "结果",
}


def _text(value: Any, limit: int = 900) -> str:
    if isinstance(value, Mapping):
        if not value:
            return ""
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


def _tokens(value: Any) -> Iterable[str]:
    """Yield compact semantic signals from one persisted file summary."""
    for token in _TOKEN_RE.findall(str(value or "").casefold()):
        if token in _STOP_WORDS:
            continue
        yield token
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 3:
            yield from (token[index:index + 2] for index in range(len(token) - 1))


def _directory_key(path: str) -> str:
    physical = str(path or "").split("::", 1)[0].replace("\\", "/")
    parent = str(Path(physical).parent).replace("\\", "/")
    return "." if parent in {"", "."} else parent


def _reading_manifest(files: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Build an auditable all-file reading ledger from persisted summaries."""
    rows: List[str] = []
    directories: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"files": 0, "summary_backed_files": 0, "sample_paths": []}
    )
    signals: Counter[str] = Counter()
    missing: List[str] = []
    summary_backed = 0
    for item in files or []:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        summary = _text(item.get("summary"), 12000)
        has_summary = bool(summary)
        directory = _directory_key(path)
        directory_row = directories[directory]
        directory_row["files"] += 1
        if len(directory_row["sample_paths"]) < 3:
            directory_row["sample_paths"].append(path)
        if has_summary:
            summary_backed += 1
            directory_row["summary_backed_files"] += 1
            signals.update(_tokens(summary))
        else:
            missing.append(path)
        rows.append("{}\0{}\0{}".format(path, item.get("summary_type") or "", summary))

    total = len(rows)
    directory_atlas = [
        {
            "directory": directory,
            "files": value["files"],
            "summary_backed_files": value["summary_backed_files"],
            "sample_paths": value["sample_paths"],
        }
        for directory, value in sorted(
            directories.items(), key=lambda pair: (-pair[1]["files"], pair[0])
        )[:80]
    ]
    return {
        "schema_version": READING_SCHEMA_VERSION,
        "files_read": total,
        "summary_backed_files": summary_backed,
        "files_without_summary": max(0, total - summary_backed),
        "summary_coverage": round(summary_backed / float(total or 1), 6),
        "complete": bool(total and summary_backed == total),
        "source_digest": sha256("\n".join(rows).encode("utf-8")).hexdigest(),
        "directory_atlas": directory_atlas,
        "semantic_signals": [
            {"term": term, "file_summary_mentions": count}
            for term, count in signals.most_common(48)
        ],
        "missing_summary_sample": missing[:24],
    }


def _representative_capsules(
    files: Sequence[Mapping[str, Any]], max_chars: int, query: str = ""
) -> List[Dict[str, Any]]:
    """Provide a bounded, directory-balanced reading sample for the model."""
    if max_chars <= 0:
        return []
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for item in files or []:
        if item.get("path"):
            grouped[_directory_key(str(item.get("path")))].append(item)
    if query and aq.enabled():
        capsules = []
        used = 0
        ranked = sorted(files, key=lambda row: -aq.relevance(query, row.get("summary", "")))
        for item in ranked:
            if not aq.relevance(query, item.get("summary", "")):
                continue
            capsule = {"path": item.get("path"), "summary_type": item.get("summary_type"),
                       "summary": aq.extract_passage(item.get("summary"), query, 900)}
            cost = len(json.dumps(capsule, ensure_ascii=False))
            if used + cost > max_chars:
                continue
            capsules.append(capsule)
            used += cost
        return capsules
    capsules: List[Dict[str, Any]] = []
    used = 0
    # One early and one late record per directory avoids a lexical-tail bias.
    for directory in sorted(grouped):
        entries = sorted(grouped[directory], key=lambda item: str(item.get("path")))
        picks = entries[:1]
        if len(entries) > 1:
            picks.append(entries[-1])
        for item in picks:
            summary = _text(item.get("summary"), 340)
            capsule = {
                "path": item.get("path"),
                "summary_type": item.get("summary_type"),
                "summary": summary or "[尚无已持久化摘要]",
            }
            projected = len(json.dumps(capsule, ensure_ascii=False, separators=(",", ":")))
            if capsules and used + projected > max_chars:
                return capsules
            capsules.append(capsule)
            used += projected
    return capsules


def build_standard_context(storage: Any, scan_id: str, max_summary_chars: int = 8000) -> dict:
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
    files_by_path: Dict[str, Dict[str, Any]] = {}

    def summary_priority(summary_type: Any) -> int:
        label = str(summary_type or "").lower()
        if "deep" in label:
            return 3
        if label == "file" or label.endswith("_file"):
            return 2
        if "preliminary" in label:
            return 1
        return 0

    for item in summaries:
        payload = item.get("payload") or {}
        path = str(item.get("path") or payload.get("source_path") or "").strip()
        if not path:
            continue
        candidate = {
            "path": path,
            "name": Path(path.split("::", 1)[-1]).name,
            "summary_type": item.get("type"),
            "summary": _text(payload, max_summary_chars),
        }
        current = files_by_path.get(path)
        if current is None or summary_priority(candidate["summary_type"]) >= summary_priority(current["summary_type"]):
            files_by_path[path] = candidate
    # A package can contain files whose summary job is still pending or failed.
    # Include the persisted inventory as zero-summary records so package scope
    # remains complete without triggering parsing or summary work.
    known = set(files_by_path)
    try:
        inventory_paths = storage.inventory_paths_under(scan_id, ".") or []
    except Exception:
        inventory_paths = []
    for raw_path in inventory_paths:
        path = str(raw_path or "").strip()
        if path and path not in known:
            files_by_path[path] = {
                "path": path,
                "name": Path(path.split("::", 1)[-1]).name,
                "summary_type": None,
                "summary": "",
            }
    files = list(files_by_path.values())
    files.sort(key=lambda item: item["path"])
    reading_manifest = _reading_manifest(files)
    return {
        "package_type": "standard",
        "package_id": str(scan_id),
        "package_overview": overview,
        "analysis_overview": analysis,
        "coverage": _coverage(analysis, len(summaries), len(files)),
        "file_summaries": files,
        "reading_manifest": reading_manifest,
    }


def build_large_context(store: Any, package_id: str, max_summary_chars: int = 8000) -> Optional[dict]:
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
            content_keys = ("summary", "text", "sample", "abstract", "conclusion", "key_findings")
            if not isinstance(summary, Mapping) or not any(summary.get(key) for key in content_keys):
                summary = local
            has_content = isinstance(summary, Mapping) and any(summary.get(key) for key in content_keys)
            files.append({
                "path": row.get("path"),
                "name": row.get("name"),
                "summary_type": summary.get("summary_type") if isinstance(summary, Mapping) else None,
                "summary": _text(summary, max_summary_chars) if has_content else "",
                "quick_status": row.get("quick_status"),
                "deep_status": row.get("deep_status"),
                "category": row.get("category"),
            })
        if len(page) < 500:
            break
        offset += len(page)
    reading_manifest = _reading_manifest(files)
    return {
        "package_type": "large",
        "package_id": str(package_id),
        "package_info": {"name": Path(str(package.get("root_path") or "")).name, "root": package.get("root_path"), "status": package.get("status"), "phase": package.get("phase")},
        "coverage": {"scope": "package", "total_files": int(counts.get("total_files") or len(files)), "quick": counts.get("quick") or {}, "deep": counts.get("deep") or {}, "source": "large_package.db"},
        "file_summaries": files,
        "reading_manifest": reading_manifest,
    }


def build_reading_manifest(files: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Public adapter used when a conversation narrows an existing package scope."""
    return _reading_manifest(files)


def synthesis_note_sources(context: Mapping[str, Any], citations: Sequence[Mapping[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Link synthesis notes to this turn's actual source evidence, never old labels."""
    state = context.get("large_qa") or {}
    provenance = defaultdict(list)
    for row in state.get("notes_provenance") or []:
        provenance[str(row.get("text") or "")].extend(str(path).replace("\\", "/") for path in row.get("sources") or [])
    output = []
    for note in state.get("map_notes") or []:
        text = str(note)
        paths = list(dict.fromkeys(provenance.get(text, [])))
        matched = [item for item in citations if str(item.get("source_path") or "").replace("\\", "/") in paths]
        output.append({"text": text, "sources": paths,
                       "citations": [item["citation_label"] for item in matched[:4]],
                       "source_kinds": [aq.source_kind(item) for item in matched[:4]]})
    return output


def model_digest(
    context: Mapping[str, Any], max_chars: int = 36000, query: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    citations: Optional[Sequence[Mapping[str, Any]]] = None,
) -> str:
    """Serialize an all-file reading layer without unsafe tail truncation.

    Every persisted summary is consumed by ``_reading_manifest``.  The prompt
    receives its digest, directory atlas and semantic signals, plus a bounded
    directory-balanced sample.  This is intentionally different from cutting
    a large JSON string at an arbitrary byte offset, which could silently drop
    the last portion of a package while claiming broad coverage.
    """
    files = list(context.get("file_summaries") or [])
    reading_manifest = dict(
        context.get("reading_manifest") or _reading_manifest(files)
    )
    payload = {
        "schema_version": "conversation-package-digest/2.0",
        "package_type": context.get("package_type"),
        "package_id": context.get("package_id"),
        "package_info": context.get("package_info") or {},
        "package_overview": _text(context.get("package_overview"), 4200),
        "analysis_overview": _text(context.get("analysis_overview"), 2600),
        "coverage": context.get("coverage") or {},
        "all_file_reading": reading_manifest,
        "instruction": (
            "全包阅读账本已逐份读取本包所有已持久化文件摘要，并把每份摘要纳入摘要指纹、目录统计和语义信号。"
            "它用于把握整个资料包的脉络；编号证据才可以支撑正文里的具体事实。"
            "若 all_file_reading.complete 为 false，必须明确有文件尚未完成可用摘要，不能称为全包完整结论。"
        ),
    }
    large_qa = context.get("large_qa") or {}
    all_notes = [str(note) for note in large_qa.get("map_notes") or [] if str(note).strip()]
    note_sources = synthesis_note_sources(context, citations or []) if citations is not None else []
    links_by_text = {item["text"]: item for item in note_sources}
    upstream_complete = bool(
        large_qa.get("synthesis_complete", large_qa.get("map_complete", False))
        and large_qa.get("source_snapshot_current") is not False
    )
    if large_qa:
        payload["topic_overview"] = {
            "instruction": "扫描计数表示程序检查已有摘要和正文块的范围；模型分批归并后的要点用于组织脉络，具体事实仍须对应编号证据。摘要扫描不等于全文阅读；扫描、归并或本轮要点保留不完整时不得宣称全包完整结论。",
            "topic_count": large_qa.get("map_topics_total", len(large_qa.get("topics") or [])),
            "map_topics_processed": large_qa.get("map_topics_processed"),
            "map_complete": large_qa.get("map_complete", False),
            **{key: large_qa[key] for key in (
                "source_files", "scope_files", "missing_index_files", "scanned_files",
                "scanned_summary_files", "scanned_original_files", "scanned_chunks",
                "model_mapped_files", "map_batches", "map_processed_batches", "map_failures",
                "files_without_content", "stale_files", "scan_complete", "reduce_complete",
                "reduce_levels", "source_snapshot_current",
            ) if key in large_qa},
            "total_notes": len(all_notes),
            "retained_notes": len(all_notes),
            "notes_complete": bool(all_notes),
            "synthesis_complete": upstream_complete and bool(all_notes),
            "notes": list(all_notes),
        }
        if note_sources:
            payload["topic_overview"]["note_citations"] = [links_by_text.get(note, {}).get("citations", []) for note in all_notes]
            payload["topic_overview"]["instruction"] += "note_citations与notes逐条对应，只能引用这些当前编号来源；编号是候选出处，事实必须核对对应证据原文。无编号的要点仅是组织线索，不能写成已核实结论；摘要出处只能表述为摘要记载。"
    base_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    payload["representative_file_capsules"] = _representative_capsules(
        files, max(0, int(max_chars) - base_size - 200), query=query
    )
    if aq.enabled():
        payload["instruction"] = "账本表示程序扫描已有资料的范围，不表示模型已阅读全部全文。归并要点用于组织整体脉络，摘要样本与统计不能视为全包穷尽结论；具体事实以编号证据为准。"
        dump = lambda: json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        while len(dump()) > max_chars and payload["representative_file_capsules"]:
            payload["representative_file_capsules"].pop()
        for key in ("directory_atlas", "semantic_signals", "missing_summary_sample"):
            while len(dump()) > max_chars and payload["all_file_reading"].get(key):
                payload["all_file_reading"][key] = payload["all_file_reading"][key][:-1]
        for key in ("analysis_overview", "package_overview"):
            if len(dump()) > max_chars:
                payload[key] = aq.extract_passage(payload[key], query, max(0, len(payload[key]) - (len(dump()) - max_chars) - 50))
        # Final all-scope synthesis has priority over descriptive metadata and
        # representative samples. Keep it whole whenever the budget permits.
        if len(dump()) > max_chars:
            if large_qa:
                payload = {
                    "instruction": "程序扫描和摘要归并不等于逐份全文阅读；具体事实仍以编号证据为准。",
                    "all_file_reading": {key: reading_manifest[key] for key in (
                        "files_read", "summary_backed_files", "files_without_summary", "complete",
                    ) if key in reading_manifest},
                    "topic_overview": payload["topic_overview"],
                }
            else:
                payload = {"instruction": "上下文预算不足，仅保留覆盖计数；不代表完整阅读。", "summary_records": len(files)}
        if len(dump()) > max_chars and large_qa:
            topic = payload["topic_overview"]
            topic.update(notes=[], retained_notes=0, notes_complete=False, synthesis_complete=False,
                         compression_incomplete=True)
            if note_sources:
                topic["note_citations"] = []
            topic["instruction"] = "本轮预算无法保留全部归并要点，以下只是部分线索；不能宣称全包完整结论。具体事实以编号证据为准。"
            # Alternate between early and late notes instead of silently
            # preserving only a lexical prefix of the package.
            order = []
            left, right = 0, len(all_notes) - 1
            while left <= right:
                order.append(left)
                if right != left:
                    order.append(right)
                left += 1
                right -= 1
            retained = []
            for index in order:
                candidate = sorted(retained + [index])
                topic["notes"] = [all_notes[i] for i in candidate]
                if note_sources:
                    topic["note_citations"] = [links_by_text.get(all_notes[i], {}).get("citations", []) for i in candidate]
                topic["retained_notes"] = len(candidate)
                if len(dump()) <= max_chars:
                    retained = candidate
            topic["notes"] = [all_notes[i] for i in retained]
            if note_sources:
                topic["note_citations"] = [links_by_text.get(all_notes[i], {}).get("citations", []) for i in retained]
            topic["retained_notes"] = len(retained)
            if len(dump()) > max_chars:
                payload = {"instruction": "本轮归并要点因预算不足未能保留，不代表全包完整结论。",
                           "topic_overview": {"total_notes": len(all_notes), "retained_notes": 0,
                                              "notes_complete": False, "synthesis_complete": False,
                                              "compression_incomplete": True, "notes": []}}
    topic = payload.get("topic_overview") or {}
    if metadata is not None and large_qa.get("global_mode"):
        metadata.update(final_synthesis_notes=len(topic.get("notes") or []),
                        total_notes=len(all_notes),
                        final_synthesis_complete=bool(topic.get("synthesis_complete")),
                        final_synthesis_note_characters=sum(len(note) for note in topic.get("notes") or []))
        if note_sources:
            retained_sources = [links_by_text.get(note, {}) for note in topic.get("notes") or []]
            linked = sum(bool(item.get("citations")) for item in retained_sources)
            metadata.update(final_synthesis_notes_with_sources=linked,
                            final_synthesis_notes_without_sources=len(retained_sources) - linked,
                            final_synthesis_sources_complete=bool(retained_sources) and linked == len(retained_sources))
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
