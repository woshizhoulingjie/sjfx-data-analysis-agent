"""Ordinary preliminary-only generation and versioned, per-job ready results."""
import json
import logging
import time
import uuid
from datetime import datetime

from config import Config
from services.document_analysis import _estimated_tokens, _normalise_summary_topics, _preview_claim_contract, _model_call_profile
from services.evidence import select_evidence
from services.preliminary_summary_batch import collect_jobs, eligible_job
from services.preliminary_summary_input import INPUT_VERSION, build_preliminary_input

LOG = logging.getLogger("sjfx.preliminary")
CONTRACT = "preliminary-ready-v1"
PROMPT_VERSION = "compact-6500-v1"


def enabled(job):
    return bool(getattr(Config, "PRELIMINARY_SUMMARY_OPTIMIZATION_ENABLED", False) and eligible_job(job))


def result_identity(job):
    options = job.get("options") or {}
    return {
        "id": str(job.get("id") or ""),
        "scan_id": str(job.get("scan_id") or ""),
        "owner_id": str(job.get("owner_id") or "legacy"),
        "path": str(options.get("path") or ""),
        "options": json.dumps(options, ensure_ascii=False, sort_keys=True),
    }


def configuration_key():
    return {"contract": CONTRACT, "input": INPUT_VERSION, "prompt": PROMPT_VERSION,
            "model": Config.LLM_MODEL, "input_chars": Config.PRELIMINARY_SUMMARY_INPUT_MAX_CHARS}


def ready_matches(job, ready):
    return bool(isinstance(ready, dict)
                and ready.get("identity") == result_identity(job)
                and ready.get("configuration") == configuration_key()
                and 0 <= time.time() - float(ready.get("created_at") or 0) <= 1800)


def _prompt(rows):
    files = [{"file_id": r["job"]["id"], "path": r["job"]["options"]["path"], "source": r["input"]} for r in rows]
    contract = {
        "file_id": "原ID", "path": "原路径", "title": "简短中文标题",
        "core_summary": "简洁摘要", "topics": [], "key_facts": [], "arguments": [],
        "methodology": [], "conclusions": [], "uncertainties": [], "warnings": [],
    }
    # Preserve the pre-existing single-file contract; the existing batch contract
    # does not ask the model to generate a second outline/research report per file.
    if len(rows) == 1:
        contract["structure_overview"] = {"sections": [], "document_type": "类型"}
        contract["recommended_research_direction"] = {"title": "", "rationale": "", "questions": []}
    return (
        "根据文件摘录生成逐文件初步摘要，摘录不代表全文。只使用各文件自身的证据，不得交叉借用。\n"
        "所有分析用简体中文，原样返回每个file_id和path。标题用简短中文，避免附加冗长英文原名。\n"
        "core_summary约150字，最多180字；短材料按实际信息量更短。直接概括对象、关键发现和限制。"
        "topics最多3个短词；key_facts最多3项，每项尽量不超过30字；arguments和methodology各最多1项；"
        "conclusions最多2项，每项尽量不超过30字。方法和论点只写材料明确支持的内容，缺乏依据时留空。"
        "不要在摘要、事实、论点、结论中反复表达同一句话。"
        "保留重要数字、单位、日期、否定条件和限制，不得为压缩字数改变事实。"
        "uncertainties/warnings保留影响判断的事项，无依据的字段留空，不凑字段。\n"
        "仅输出所列字段；如包含结构概览，只列最多3个短章节名；研究方向最多一个简短问题，理由一句话。"
        "输出JSON：" + json.dumps({"documents": [contract]}, ensure_ascii=False, separators=(",", ":"))
        + "\n分析材料：\n" + json.dumps(files, ensure_ascii=False, separators=(",", ":"))
    )


def _call(llm, rows, ledger):
    call_id = uuid.uuid4().hex
    started = time.monotonic()
    record = {"model_call_id": call_id, "files": len(rows), "success": False}
    ledger.append(record)
    try:
        response = llm.chat_json(
            "你是严谨的中文资料预读助手。独立分析每个文件，输出简短、完整且有来源支持的JSON。",
            _prompt(rows), max_tokens=100 + 600 * len(rows), long_output=True,
            strict=True, retries=0, structured_retry_limit=0,
            timeout=180 if len(rows) > 1 else 90, allow_short_timeout=True,
            required_fields=("documents",), output_context="普通文件初步摘要",
        )
        record.update({"usage": response.get("usage") or {}, "finish_reason": response.get("finish_reason"), "success": True})
        return response
    finally:
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        LOG.info("model_call=%s files=%s success=%s elapsed_ms=%s", call_id, len(rows), record["success"], record["elapsed_ms"])


def _validated(response, rows):
    values = (response.get("json") or {}).get("documents")
    if not isinstance(values, list):
        return {}
    expected = {str(r["job"]["id"]): r["job"]["options"]["path"] for r in rows}
    grouped = {}
    for value in values:
        if isinstance(value, dict):
            grouped.setdefault(str(value.get("file_id") or ""), []).append(value)
    output = {}
    for job_id, path in expected.items():
        matches = grouped.get(job_id) or []
        if len(matches) != 1:
            continue
        item = matches[0]
        if item.get("path") != path or not isinstance(item.get("core_summary"), str) or not item["core_summary"].strip():
            continue
        fields = ("topics", "key_facts", "arguments", "methodology", "conclusions", "uncertainties", "warnings")
        if any(k in item and (not isinstance(item[k], list) or any(not isinstance(v, str) for v in item[k])) for k in fields):
            continue
        if "title" in item and not isinstance(item["title"], str):
            continue
        if any(k in item and not isinstance(item[k], dict) for k in ("structure_overview", "recommended_research_direction")):
            continue
        output[job_id] = item
    return output


def _prepare(row, raw, response, ledger, batch_id, batch_size, fallback_reason=None):
    started = time.monotonic()
    document, job = row["document"], row["job"]
    path = job["options"]["path"]
    summary = _normalise_summary_topics(dict(raw), row["input"], path)
    summary["summary"] = summary.get("summary") or summary.get("core_summary") or ""
    for field in ("key_facts", "arguments", "methodology", "conclusions", "uncertainties", "warnings"):
        summary.setdefault(field, [])
    summary.setdefault("title", (document.get("structure") or {}).get("title") or path)
    summary["evidence_chain"] = select_evidence(document.get("evidence") or [], topics=summary.get("topics") or [], max_items=3, per_source=3, max_chars=360)
    if (document.get("parser") or {}).get("name") == "structured-table":
        from services.tabular_document import grounded_table_summary
        summary = grounded_table_summary(summary, document)
    summary.update(_preview_claim_contract(summary, document.get("evidence") or []))
    degraded = fallback_reason is not None
    summary.update({
        "schema_version": 4, "summary_type": "file", "node_path": path,
        "generated_by": "local-preliminary-fallback" if degraded else "model-preliminary-analysis",
        "analysis_stage": "preliminary", "analysis_depth": "preliminary_document",
        "analysis_level": "preview", "verification_status": "preliminary",
        "preview_only": True, "deep_analysis": False,
        "preview_input_chars": len(row["input"]), "preview_source_chars": len(str(document.get("text") or "")),
        "preview_coverage": {"mode": "selective", "selected_chars": len(row["input"]), "complete": False},
        "file_review_items": [{"type": "preliminary", "text": "初步摘要已结合已解析内容和支撑原文；后续深度摘要将继续校验全文。", "status": "review"}],
        "parser_info": {"parser": (document.get("parser") or {}).get("name"), "coverage": document.get("coverage") or {},
                        "model": response.get("model"), "local_model": response.get("model"),
                        "usage": response.get("usage") or {}, "degraded": degraded, "batch_size": batch_size,
                        "model_calls": [_model_call_profile("candidate_preview", response, int(response.get("max_tokens") or 100 + 600 * batch_size), Config.LLM_CONTEXT_TOKENS, 1)]},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    })
    if degraded:
        summary["warnings"] = list(summary.get("warnings") or []) + ["模型未完成，当前为本地解析保底结果。"]
    result = {"scan_id": job["scan_id"], "summary": summary, "cached": False, "degraded": degraded,
              "analysis_level": "preview", "path": path, "workflow_source": "preliminary_model_summary",
              "preliminary_timing": {"batch_id": batch_id, "model_calls": list(ledger),
                                     "input_build_ms": row["input_build_ms"],
                                     "prepare_ms": round((time.monotonic() - started) * 1000, 2)}}
    return {"identity": result_identity(job), "configuration": configuration_key(), "stamp": row["stamp"],
            "created_at": time.time(), "result": result}


def generate_ready_results(llm, storage, lead_job, fallback):
    jobs = collect_jobs(storage, lead_job, limit=Config.PRELIMINARY_SUMMARY_BATCH_SIZE)
    rows = []
    estimated = 1700
    for job in jobs:
        snapshot = storage.preliminary_document_snapshot(job)
        if not snapshot:
            if job["id"] == lead_job["id"]:
                raise ValueError("初步摘要的文档或任务身份已失效")
            continue
        started = time.monotonic()
        text = build_preliminary_input(snapshot["document"], Config.PRELIMINARY_SUMMARY_INPUT_MAX_CHARS)
        cost = _estimated_tokens(text) + 900
        if rows and estimated + cost > Config.LLM_CONTEXT_TOKENS - 1500:
            break
        estimated += cost
        rows.append({"job": job, "document": snapshot["document"], "stamp": snapshot["stamp"],
                     "input": text, "input_build_ms": round((time.monotonic() - started) * 1000, 2)})
    batch_id, ledger, output = uuid.uuid4().hex, [], {}
    response, error = {}, "模型输出未通过逐文件校验"
    try:
        response = _call(llm, rows, ledger)
        valid = _validated(response, rows)
    except Exception as exc:
        valid = {}
        error = str(exc)[:300]
    for row in rows:
        job_id = row["job"]["id"]
        if storage.is_job_cancel_requested(job_id):
            continue
        raw, item_response = valid.get(job_id), response
        reason = None
        if raw is None:
            # One recovery call per failed file; successful siblings are never repeated.
            try:
                item_response = _call(llm, [row], ledger)
                raw = _validated(item_response, [row]).get(job_id)
                if raw is None:
                    raise ValueError("初步摘要恢复请求未通过文件身份或内容校验")
            except Exception as exc:
                reason = str(exc)[:300] or error
                raw = fallback(row["document"], row["job"]["options"]["path"], reason)
                item_response = {}
        output[job_id] = _prepare(row, raw, item_response, ledger, batch_id, len(rows), reason)
    # All rows share a call ledger. Keep IDs so aggregation does not count it per file.
    for ready in output.values():
        ready["result"]["preliminary_timing"]["model_calls"] = list(ledger)
    if str(lead_job["id"]) in output:
        output[str(lead_job["id"])]["result"]["_preliminary_batch_size"] = len(rows)
    return {"_preliminary_ready_results": output, "_preliminary_batch_size": len(rows)}
