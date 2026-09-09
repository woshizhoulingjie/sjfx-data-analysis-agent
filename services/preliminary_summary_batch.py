"""Runtime-only batching for preliminary file summaries.

The import state machine still creates and completes one ``generate_summary``
job per file.  This module only coalesces compatible queued jobs in memory for
one model request; callers keep the per-file save/finalize path unchanged.
"""

from __future__ import annotations

import json
import time

from services.document_analysis import (
    _estimated_tokens,
    _normalise_summary_topics,
    _preview_input,
)
from services.evidence import select_evidence


class PreliminaryBatchProtocolError(ValueError):
    """The model did not return an unambiguous per-file batch result."""


def eligible_job(job):
    """Return whether a job is safe to prefetch with other file summaries."""
    job = job or {}
    options = job.get("options") or {}
    return bool(
        job.get("task_type") == "generate_summary"
        and str(options.get("workflow_source") or "") == "preliminary_model_summary"
        and str(options.get("kind") or "file") == "file"
        and str(options.get("path") or "").strip()
        and not options.get("paths")
        and not options.get("node_id")
        and not options.get("deep_batch_id")
        and not options.get("_preliminary_prefetched_summary")
    )


def _decode_rows(storage, rows):
    return [storage._decode_job(dict(row)) for row in rows]


def collect_jobs(storage, lead_job, limit=4):
    """Read compatible queued siblings without changing their queue state."""
    if not eligible_job(lead_job):
        return [lead_job]
    limit = max(1, min(4, int(limit or 4)))
    owner_id = str(lead_job.get("owner_id") or "legacy")
    scan_id = str(lead_job.get("scan_id") or "")
    priority = int(lead_job.get("priority") or 0)
    with storage._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM analysis_jobs WHERE scan_id=? AND owner_id=? "
            "AND task_type='generate_summary' AND status='queued' "
            "AND cancel_requested=0 AND available_at<=? AND priority=? "
            "ORDER BY rowid LIMIT ?",
            (scan_id, owner_id, time.time(), priority, max(16, limit * 8)),
        ).fetchall()
    siblings = []
    seen = {str(lead_job.get("id") or "")}
    seen_paths = {str((lead_job.get("options") or {}).get("path") or "").strip()}
    for candidate in _decode_rows(storage, rows):
        candidate_id = str(candidate.get("id") or "")
        if candidate_id in seen or not eligible_job(candidate):
            continue
        candidate_path = str((candidate.get("options") or {}).get("path") or "").strip()
        if not candidate_path or candidate_path in seen_paths:
            continue
        seen.add(candidate_id)
        seen_paths.add(candidate_path)
        siblings.append(candidate)
    return [lead_job, *siblings][:limit]


def _make_rows(storage, jobs, max_chars):
    rows = []
    for job in jobs:
        path = str((job.get("options") or {}).get("path") or "").strip()
        document = storage.get_document(job.get("scan_id"), path)
        if not document:
            continue
        selected = _preview_input(document, max_chars=max_chars)
        if selected.strip():
            rows.append({
                "job_id": str(job.get("id") or ""),
                "path": path,
                "document": document,
                "input": selected,
            })
    return rows


def _fit_rows(rows, context_window_tokens, output_tokens_per_file=240):
    """Keep four-file batching within the existing model context budget."""
    # Reserve room for the contract, file IDs and the per-file JSON result.
    budget = max(4000, int(context_window_tokens or 32768) - 3200)
    selected = []
    used = 0
    for row in rows:
        cost = _estimated_tokens(row["input"]) + 260
        if selected and used + cost + output_tokens_per_file * (len(selected) + 1) > budget:
            break
        selected.append(row)
        used += cost
    return selected


def analyze_preliminary_batch(llm, storage, jobs, max_chars=8000,
                              context_window_tokens=32768,
                              output_tokens_per_file=240):
    """Generate independent preliminary summaries in one model call."""
    rows = _fit_rows(
        _make_rows(storage, jobs, max_chars=max_chars),
        context_window_tokens=context_window_tokens,
        output_tokens_per_file=output_tokens_per_file,
    )
    lead_id = str((jobs[0] if jobs else {}).get("id") or "")
    if len(rows) < 2 or not any(row["job_id"] == lead_id for row in rows):
        raise PreliminaryBatchProtocolError("当前文件无法安全组成多文件摘要批次")
    prompt_rows = "\n\n".join(
        "<DOCUMENT file_id={file_id} path={path}>\n解析文本块：\n{input}\n"
        "</DOCUMENT>".format(
            file_id=json.dumps(row["job_id"], ensure_ascii=False),
            path=json.dumps(row["path"], ensure_ascii=False),
            input=row["input"],
        )
        for row in rows
    )
    prompt = """你正在生成资料包的文件初步摘要。下面是多个相互独立的文件。
必须逐个文件分析，绝不能合并、借用或推断另一个文件的内容。
每条结论、事实、论点、方法、风险和不确定性都必须使用简体中文表达；文件原文中的专有名词、缩写和文件路径可以保留原文。
必须返回 JSON，并且每个对象必须原样带回对应的 file_id 和 path。

{rows}

返回格式：
{{"documents":[{{"file_id":"任务ID","path":"原文件路径","title":"中文标题",
"core_summary":"中文摘要","topics":["中文主题"],"key_facts":["中文事实"],
"arguments":["中文论点"],"methodology":["中文方法"],"conclusions":["中文结论"],
"uncertainties":["中文不确定性"],"warnings":["中文提示"]}}]}}
不要遗漏文件，不要重复 file_id，不要在文件之间交叉引用。""".format(rows=prompt_rows)
    result = llm.chat_json(
        "你是严谨的中文资料摘要助手。只输出有证据支持的、逐文件隔离的中文结果。",
        prompt,
        max_tokens=max(160, min(1600, int(output_tokens_per_file) * len(rows))),
        long_output=True,
        strict=True,
        retries=0,
        timeout=180,
        allow_short_timeout=True,
        required_fields=("documents",),
        output_context="多文件初步摘要",
    )
    values = (result.get("json") or {}).get("documents")
    if not isinstance(values, list):
        raise PreliminaryBatchProtocolError("多文件初步摘要返回格式无效")
    by_id = {}
    for item in values:
        if not isinstance(item, dict):
            raise PreliminaryBatchProtocolError("多文件初步摘要包含无效文件对象")
        file_id = str(item.get("file_id") or "").strip()
        if not file_id or file_id in by_id:
            raise PreliminaryBatchProtocolError("多文件初步摘要缺少或重复 file_id")
        by_id[file_id] = item
    expected = {row["job_id"] for row in rows}
    if set(by_id) != expected:
        missing = sorted(expected - set(by_id))
        extra = sorted(set(by_id) - expected)
        raise PreliminaryBatchProtocolError(
            "多文件初步摘要文件隔离校验失败 missing={} extra={}".format(missing, extra)
        )
    output = {}
    for row in rows:
        raw = dict(by_id[row["job_id"]])
        summary = _normalise_summary_topics(raw, row["input"], row["path"])
        summary["summary"] = summary.get("summary") or summary.get("core_summary") or ""
        summary["path"] = row["path"]
        summary["evidence_chain"] = select_evidence(
            (row["document"].get("evidence") or []),
            topics=summary.get("topics") or [], max_items=3, per_source=3,
            max_chars=360,
        )
        summary.update({
            "schema_version": 4,
            "summary_type": "file",
            "node_path": row["path"],
            "generated_by": "model-preliminary-batch-analysis",
            "analysis_depth": "preliminary_document",
            "analysis_level": "preview",
            "verification_status": "preliminary",
            "deep_analysis": False,
            "preview_only": True,
            "preview_input_chars": len(row["input"]),
            "preview_source_chars": len(str(row["document"].get("text") or "")),
            "preview_coverage": {"mode": "selective", "selected_chars": len(row["input"]), "complete": False},
            "parser_info": {"model": result.get("model"), "usage": result.get("usage") or {}, "batch_size": len(rows)},
        })
        output[row["job_id"]] = {"summary": summary, "result": result, "path": row["path"]}
    return output, [row["job_id"] for row in rows]
