import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

from services.ollama import LocalModelError
from services.evidence import build_file_claims, select_evidence
from services.scanner import extract_text


CHINESE_CONCLUSION_RULE = (
    " 所有摘要、事实、论点、方法、结论、风险和不确定性必须使用简体中文表达；"
    "原文专有名词、缩写、CVE、数字和文件路径可以保留原文。"
)


TOPIC_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "are", "was", "were",
    "have", "has", "into", "using", "paper", "article", "document", "study",
    "通过", "以及", "其中", "相关", "内容", "文件", "本文", "研究", "分析",
}
COMMON_TOPIC_WORDS = {
    "shadow", "shadows", "cipher", "spaces", "exploiting", "tweak", "repetition",
    "hardware", "memory", "encryption", "attack", "attacks", "security", "paper",
    "privacy", "cryptography", "method", "methods", "experiment", "results", "system",
    "data", "analysis", "network", "language", "model", "research", "evaluation",
}


def _topic_candidates(text):
    values = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,8}", str(text or ""))
    counter = Counter(value.casefold() for value in values if value.casefold() not in TOPIC_STOPWORDS)
    return [value for value, _count in counter.most_common(80)]


def _split_concatenated_topic(value, candidates):
    """Recover boundaries in a model topic such as ``memoryencryption``."""
    value = str(value or "").strip()
    if not value or re.search(r"\s|[,，、;；/|]", value) or len(value) < 18:
        return [value] if value else []
    source = value.casefold()
    vocabulary = sorted(
        {item.casefold() for item in (list(candidates or []) + list(COMMON_TOPIC_WORDS)) if len(item) >= 3},
        key=lambda item: (-len(item), item),
    )
    parts = []
    index = 0
    while index < len(source):
        match = next((word for word in vocabulary if source.startswith(word, index)), None)
        if not match:
            end = index + 1
            while end < len(source) and not any(source.startswith(word, end) for word in vocabulary):
                end += 1
            parts.append(source[index:end])
            index = end
        else:
            parts.append(match)
            index += len(match)
    if len(parts) <= 1 or sum(len(item) for item in parts) < len(source) * 0.8:
        return [value]
    return parts


def _normalise_topics(values, text, title="", limit=12):
    candidates = _topic_candidates("{} {}".format(title, text))
    output = []
    for raw in values or []:
        nested = raw if isinstance(raw, (list, tuple, set)) else re.split(r"[,，、;；/|\n]+", str(raw or ""))
        for value in nested:
            value = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n-—")
            if not value:
                continue
            for piece in _split_concatenated_topic(value, candidates):
                piece = piece.strip()
                if piece and piece.casefold() not in TOPIC_STOPWORDS and piece not in output:
                    output.append(piece)
                if len(output) >= limit:
                    return output
    if len(output) < 2:
        for candidate in candidates:
            if candidate not in output:
                output.append(candidate)
            if len(output) >= limit:
                break
    return output[:limit]


def _normalise_summary_topics(summary, text, title=""):
    summary = dict(summary or {})
    summary["topics"] = _normalise_topics(summary.get("topics") or [], text, title)
    return summary

def _estimated_tokens(text):
    """Conservative tokenizer-free estimate used only for prompt budgeting."""
    value = str(text or "")
    cjk = len(re.findall(r"[\u3400-\u9fff]", value))
    remainder = max(0, len(value) - cjk)
    return cjk + int(math.ceil(remainder / 3.5))


def _split_text(text, max_chunks=64, preferred_chars=42000, max_input_tokens=14000, overlap_chars=320):
    """Split the complete text on structural boundaries under a token budget.

    ``max_chunks`` is a warning threshold, not a truncation switch. If a long
    document needs more chunks to preserve the full text, every chunk is kept.
    """
    if not text:
        return []
    preferred_chars = max(512, int(preferred_chars))
    max_input_tokens = max(1000, int(max_input_tokens))
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + preferred_chars)
        while end > start + 256 and _estimated_tokens(text[start:end]) > max_input_tokens:
            end = start + max(256, int((end - start) * 0.85))
        if end < len(text):
            search_from = start + int((end - start) * 0.65)
            boundaries = [
                text.rfind("\n#", search_from, end),
                text.rfind("\n\n", search_from, end),
                text.rfind("\n", search_from, end),
                text.rfind("。", search_from, end),
            ]
            boundary = max(boundaries)
            if boundary > start:
                end = boundary + (1 if text[boundary:boundary + 1] == "。" else 0)
        chunk_text = text[start:end]
        chunks.append({
            "index": len(chunks) + 1, "start": start, "end": end,
            "text": chunk_text, "estimated_input_tokens": _estimated_tokens(chunk_text),
            "overlap_chars": 0 if not chunks else min(overlap_chars, start),
        })
        if end >= len(text):
            break
        next_start = max(start + 1, end - max(0, int(overlap_chars)))
        start = next_start
    return chunks


def _is_output_truncated(result, max_tokens):
    usage = (result or {}).get("usage") or {}
    count = int(usage.get("completion_tokens") or 0)
    reason = str((result or {}).get("finish_reason") or "").lower()
    return reason in {"length", "max_tokens"} or (count and count >= int(max_tokens) * 0.98)


def _model_call_profile(stage, result, max_tokens, context_window_tokens, chunk_index=None):
    usage = (result or {}).get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    context_tokens = prompt_tokens + completion_tokens
    timing = (result or {}).get("timing") or {}
    prefill_seconds = float(timing.get("prefill_seconds") or 0)
    decode_seconds = float(timing.get("decode_seconds") or 0)
    return {
        "stage": stage,
        "chunk_index": chunk_index,
        "usage": usage,
        "context_tokens": context_tokens,
        "context_window_tokens": int(context_window_tokens),
        "context_occupancy": round(context_tokens / float(max(1, context_window_tokens)), 6),
        "finish_reason": (result or {}).get("finish_reason"),
        "output_truncated": _is_output_truncated(result, max_tokens),
        "timing": timing,
        "prefill_tokens_per_second": round(prompt_tokens / prefill_seconds, 3) if prefill_seconds else None,
        "decode_tokens_per_second": round(completion_tokens / decode_seconds, 3) if decode_seconds else None,
    }


def _p95(values):
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    position = 0.95 * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _attach_call_statistics(coverage):
    calls = coverage.get("model_calls") or []
    coverage["context_p95_tokens"] = round(_p95(item.get("context_tokens") for item in calls) or 0, 3)
    coverage["context_occupancy_p95"] = round(_p95(item.get("context_occupancy") for item in calls) or 0, 6)
    coverage["model_call_count"] = len(calls)
    return coverage


def _dedupe(values, limit=40):
    output = []
    seen = set()
    for value in values:
        key = str(value).strip()
        if key and key not in seen:
            seen.add(key)
            output.append(key)
        if len(output) >= limit:
            break
    return output


def _chunk_fallback(chunk, error):
    preview = " ".join(chunk["text"].split())[:500]
    return {
        "chunk_index": chunk["index"],
        "range": "{}-{}".format(chunk["start"], chunk["end"]),
        "section_summary": preview,
        "key_facts": [],
        "sections": [],
        "arguments": [],
        "error": str(error),
    }


def _compact_chunk_result(item):
    """Bound map results before placing them in the final reduce prompt."""
    item = item or {}
    compact = {
        "chunk_index": item.get("chunk_index"),
        "range": item.get("range"),
        "section_summary": " ".join(str(item.get("section_summary") or "").split())[:700],
        "key_facts": [" ".join(str(value).split())[:180] for value in item.get("key_facts", [])[:4]],
        "sections": [" ".join(str(value).split())[:120] for value in item.get("sections", [])[:4]],
        "arguments": [" ".join(str(value).split())[:180] for value in item.get("arguments", [])[:3]],
        "methodology": [" ".join(str(value).split())[:160] for value in item.get("methodology", [])[:3]],
        "conclusions": [" ".join(str(value).split())[:180] for value in item.get("conclusions", [])[:3]],
        "limitations": [" ".join(str(value).split())[:160] for value in item.get("limitations", [])[:3]],
    }
    if item.get("error"):
        compact["error"] = str(item["error"])[:240]
    return compact


def _fit_reduce_items(items, budget_chars=36000):
    """Keep a trace from every map chunk while bounding the final prompt."""
    compact = [_compact_chunk_result(item) for item in items]
    if len(json.dumps(compact, ensure_ascii=False)) <= budget_chars:
        return compact
    per_item = max(280, int(budget_chars / max(1, len(compact))))
    fitted = []
    for item in compact:
        summary_budget = max(120, int(per_item * 0.48))
        fact_budget = max(60, int(per_item * 0.18))
        fitted.append({
            "chunk_index": item.get("chunk_index"),
            "range": item.get("range"),
            "section_summary": str(item.get("section_summary") or "")[:summary_budget],
            "key_facts": [str(value)[:fact_budget] for value in item.get("key_facts", [])[:2]],
            "conclusions": [str(value)[:fact_budget] for value in item.get("conclusions", [])[:1]],
            "limitations": [str(value)[:fact_budget] for value in item.get("limitations", [])[:1]],
            "error": str(item.get("error") or "")[:fact_budget] or None,
        })
    return fitted


def _local_merge(node_path, chunks, chunk_results, warnings):
    facts = []
    sections = []
    summaries = []
    arguments = []
    methodology = []
    conclusions = []
    limitations = []
    errors = []
    for item in chunk_results:
        summaries.append(item.get("section_summary", ""))
        facts.extend(item.get("key_facts", []))
        sections.extend(item.get("sections", []))
        arguments.extend(item.get("arguments", []))
        methodology.extend(item.get("methodology", []))
        conclusions.extend(item.get("conclusions", []))
        limitations.extend(item.get("limitations", []))
        if item.get("error"):
            errors.append("第{}块：{}".format(item.get("chunk_index"), item["error"]))
    usable = [value for value in summaries if value]
    fact_count = len(_dedupe(facts))
    research = {
        "title": "核验正文块之间的核心结论与证据一致性",
        "type": "推论",
        "rationale": "本地解析已覆盖 {} 个正文块，整理出 {} 条事实线索；模型汇总不可用时，最稳妥的下一步是对照代表性正文块核验结论、方法和失败块。".format(len(usable), fact_count),
        "basis": "基于已解析正文块的事实、章节和结论字段；未引入领域关键词或外部事实。",
        "questions": ["核心结论分别由哪些正文块和原文证据支持？", "不同正文块之间是否存在定义、口径或结论差异？", "失败块是否包含会改变整体判断的关键信息？"],
    }
    return _normalise_summary_topics({
        "title": node_path,
        "structure_overview": {"sections": _dedupe(sections), "document_type": "长文档"},
        "core_summary": "\n".join(value for value in summaries if value)[:12000],
        "key_facts": _dedupe(facts),
        "arguments": _dedupe(arguments),
        "methodology": _dedupe(methodology),
        "conclusions": _dedupe(conclusions),
        "uncertainties": [],
        "warnings": _dedupe(list(warnings) + limitations + errors),
        "recommended_research_direction": research,
    }, "\n".join(summaries + facts + conclusions), node_path)


def _model_metadata(unified_document):
    """Build compact metadata so models understand tables without raw-table overload."""
    metadata = dict((unified_document or {}).get("structure", {}) or {})
    profile = (unified_document or {}).get("data_profile") or {}
    if profile:
        columns = {}
        for name, item in list((profile.get("columns") or {}).items())[:120]:
            item = item or {}
            column = {"inferred_type": item.get("inferred_type"), "missing_ratio": item.get("missing_ratio"), "unique_count": item.get("unique_count"), "sample_values": list(item.get("sample_values") or [])[:8]}
            for key in ("min", "max", "sum", "mean", "median", "count", "outlier_count"):
                if key in item:
                    column[key] = item[key]
            columns[str(name)] = column
        metadata["data_profile"] = {"row_count": profile.get("row_count"), "column_count": profile.get("column_count"), "columns": columns, "numeric_columns": list(profile.get("numeric_columns") or [])[:120], "temporal_columns": list(profile.get("temporal_columns") or [])[:120], "coverage": profile.get("coverage") or {}, "limits": profile.get("limits") or {}}
    return metadata




def _preview_input(unified_document, max_chars=24000):
    """Build a bounded, structure-aware model input for candidate analysis.

    Candidate analysis intentionally does not read the entire document.  It
    samples the title/structure, the beginning and end, and a few source
    evidence blocks so the first directory is useful without paying the cost
    of a deep, chunk-by-chunk analysis.
    """
    document = unified_document or {}
    source = document.get("source") or {}
    structure = document.get("structure") or {}
    title = structure.get("title") or source.get("name") or ""
    headings = [str(item).strip() for item in (structure.get("headings") or []) if str(item).strip()]
    raw = " ".join(str(document.get("text") or "").split())
    pieces = []
    if title:
        pieces.append("标题：{}".format(title))
    if headings:
        pieces.append("章节：{}".format("、".join(headings[:36])))
    if raw:
        head = raw[: max(1500, int(max_chars * 0.30))]
        tail = raw[-max(1500, int(max_chars * 0.24)) :]
        pieces.append("正文开头：{}".format(head))
        if tail and tail != head:
            pieces.append("正文结尾：{}".format(tail))
    seen = set()
    evidence_parts = []
    for item in sorted(
        [item for item in (document.get("evidence") or []) if str(item.get("text") or "").strip()],
        key=lambda item: (int(item.get("page") or 0), int(item.get("char_start") or 0)),
    ):
        text = " ".join(str(item.get("text") or "").split())
        key = text[:500].casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        locator = item.get("page") and "第 {} 页".format(item.get("page")) or item.get("section") or "正文片段"
        evidence_parts.append("[{}] {}".format(locator, text[:1200]))
        if len(evidence_parts) >= 8:
            break
    if evidence_parts:
        pieces.append("代表性原文片段：\n{}".format("\n".join(evidence_parts)))
    text = "\n\n".join(pieces)
    return text[:max(2000, int(max_chars))]


def _preview_claim_contract(summary, evidence_items):
    """Expose preview claims in the same shape as deep claims, without
    pretending that the sampled input has passed full source verification."""
    source = summary if isinstance(summary, dict) else {}
    evidence_items = [item for item in (evidence_items or []) if isinstance(item, dict)]
    groups = (
        ("conclusions", "conclusion", "文件结论"),
        ("key_facts", "fact", "关键事实"),
        ("arguments", "argument", "主要论点"),
        ("methodology", "method", "方法依据"),
    )
    formal = []
    arguments = []
    seen = set()
    for field, claim_type, label in groups:
        for raw in source.get(field) or []:
            text = raw.get("text") if isinstance(raw, dict) else str(raw or "")
            text = " ".join(str(text).split()).strip()
            if not text or text.casefold() in seen:
                continue
            seen.add(text.casefold())
            supports = select_evidence(evidence_items, topics=[text], max_items=2, per_source=2, max_chars=360)
            record = {
                "conclusion_id": "PREVIEW-%03d" % (len(formal) + len(arguments) + 1),
                "type": claim_type,
                "label": label,
                "statement": text,
                "text": text,
                "status": "candidate",
                "support_status": "candidate",
                "verification_status": "candidate",
                "evidence_ids": [
                    str(item.get("evidence_id") or "")
                    for item in supports if item.get("evidence_id")
                ],
                "support_level": "preview",
                "support_label": "初步支持（未完成全文校验）",
                "support_score": None,
                "support_score_type": "preview_estimate",
                "supports": supports,
                "preview_only": True,
            }
            (arguments if claim_type in {"argument", "method"} else formal).append(record)
            if len(formal) + len(arguments) >= 18:
                break
        if len(formal) + len(arguments) >= 18:
            break
    return {
        "file_conclusions": formal,
        "file_arguments": arguments,
        "file_review_items": [],
        "file_limitations": [{"type": "preview", "text": "候选分析只使用代表性内容，深度摘要后需重新校验结论。", "status": "review"}],
        "evidence_quality": {
            "status": "preview",
            "claims_considered": len(formal) + len(arguments),
            "formal_claim_count": 0,
            "preview_claim_count": len(formal) + len(arguments),
            "complete": False,
        },
        "claim_contract": "file-claims/preview-1.0",
    }


def analyze_document_preview(llm, path, node_path, unified_document=None,
                             max_chars=24000, context_window_tokens=32768,
                             timeout_seconds=60):
    """Run one bounded model call for candidate/import preview analysis."""
    if unified_document is None:
        raise ValueError("候选分析缺少统一文档内容")
    selected_text = _preview_input(unified_document, max_chars=max_chars)
    if not selected_text.strip():
        raise ValueError("未能从该文件提取候选分析内容")
    metadata = _model_metadata(unified_document)
    prompt = """你正在进行文献候选集预读。输入只包含文件的结构和代表性片段，不是全文。
请只根据给定内容生成与正式文件摘要相同类型的结果；不要把未提供的全文内容当成事实。
候选结论必须标记为待深度校验，输出要简洁，不能写成长篇报告。

文件：{path}
解析元数据：{metadata}
代表性内容：
{text}

输出 JSON：
{{"title":"标题","structure_overview":{{"sections":["章节"],"document_type":"类型"}},"core_summary":"摘要","key_facts":["事实"],"arguments":["论点"],"methodology":["方法"],"conclusions":["候选结论"],"uncertainties":["不确定信息"],"warnings":["预览限制"],"recommended_research_direction":{{"title":"方向","rationale":"理由","questions":["问题"]}}}}
""".format(path=node_path, metadata=json.dumps(metadata, ensure_ascii=False), text=selected_text)
    result = llm.chat_json(
        "你是严谨的文献预读助手。只根据代表性内容提炼候选摘要和论点，不得冒充全文校验结论。" + CHINESE_CONCLUSION_RULE,
        prompt,
        max_tokens=600,
        long_output=False,
        strict=True,
        retries=0,
        timeout=max(15, int(timeout_seconds or 60)),
        allow_short_timeout=True,
        required_fields=("core_summary",),
        output_context="候选文件预读分析",
    )
    summary = _normalise_summary_topics(result["json"], selected_text, node_path)
    summary["summary"] = summary.get("summary") or summary.get("core_summary") or ""
    summary["evidence_chain"] = select_evidence(
        unified_document.get("evidence", []),
        topics=summary.get("topics") or summary.get("structure_overview", {}).get("sections", []),
        max_items=3,
        per_source=3,
        max_chars=360,
    )
    summary.update(_preview_claim_contract(summary, unified_document.get("evidence", [])))
    summary.update({
        "schema_version": 4,
        "summary_type": "file",
        "node_path": node_path,
        "generated_by": "model-preview-analysis",
        "analysis_depth": "preview_document",
        "analysis_level": "preview",
        "verification_status": "candidate",
        "deep_analysis": False,
        "preview_only": True,
        "preview_input_chars": len(selected_text),
        "preview_source_chars": len(str(unified_document.get("text") or "")),
        "preview_coverage": {"mode": "selective", "selected_chars": len(selected_text), "complete": False},
        "parser_info": {
            "parser": (unified_document.get("parser") or {}).get("name", "本地解析"),
            "coverage": unified_document.get("coverage") or {},
            "local_model": result.get("model"),
            "usage": result.get("usage") or {},
            "model_calls": [_model_call_profile("candidate_preview", result, 640, context_window_tokens, 1)],
            "degraded": False,
        },
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    })
    return summary, result



def analyze_document_previews_batch(llm, documents, max_chars=8000,
                                    context_window_tokens=32768,
                                    timeout_seconds=90,
                                    output_tokens_per_file=240):
    """Analyze a small candidate batch with one bounded model request.

    The batch contract keeps each file independently addressable while removing
    one 27B generation queue wait per file. Missing/invalid entries are left to
    the caller's local fallback path.
    """
    rows = []
    for item in documents or []:
        path = str((item or {}).get("path") or "").strip()
        document = (item or {}).get("document") or {}
        if not path:
            continue
        selected = _preview_input(document, max_chars=max_chars)
        if selected.strip():
            rows.append({"path": path, "input": selected})
    if not rows:
        raise ValueError("未能从候选文件提取预览内容")
    prompt_rows = "\n\n".join(
        "文件 %d：%s\n代表性内容：\n%s" % (index, row["path"], row["input"])
        for index, row in enumerate(rows, 1)
    )
    prompt = """你正在进行文献候选集预读。下面包含多个文件的结构和代表性片段，
不是全文。请严格依据每个文件自己的内容分别输出简洁候选摘要，不要把文件之间的内容混合，
也不要把候选结论写成全文校验结论。所有 title、core_summary、topics、key_facts、arguments、
methodology、conclusions、uncertainties、warnings 必须使用简体中文；专业术语可在中文后保留英文缩写。

%s

只输出 JSON：{"documents":[{"path":"原文件路径","title":"标题","core_summary":"摘要",
"topics":["主题"],"key_facts":["事实"],"arguments":["论点"],"methodology":["方法"],
"conclusions":["候选结论"],"uncertainties":["不确定信息"],"warnings":["预览限制"]}]}""" % prompt_rows
    per_file_tokens = max(160, min(360, int(output_tokens_per_file or 240)))
    result = llm.chat_json(
        "你是严谨的文献预读助手。一次处理多个文件，每个文件独立返回结果。" + CHINESE_CONCLUSION_RULE,
        prompt,
        max_tokens=max(per_file_tokens, min(1600, per_file_tokens * len(rows))),
        # A multi-file object needs its per-file output budget; the ordinary
        # structured cap is sized for one compact object and caused truncation.
        long_output=True,
        strict=True,
        retries=0,
        timeout=max(20, int(timeout_seconds or 90)),
        allow_short_timeout=True,
        required_fields=("documents",),
        output_context="候选文件批量预读分析",
    )
    payload = result.get("json") or {}
    values = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise ValueError("候选批量预读返回格式无效")
    by_path = {str(item.get("path") or ""): item for item in values if isinstance(item, dict)}
    output = {}
    for row in rows:
        path = row["path"]
        raw = dict(by_path.get(path) or {})
        # Models sometimes omit the path or normalize it; use positional fallback
        # only when there is no collision with another returned path.
        if not raw:
            index = rows.index(row)
            if index < len(values) and isinstance(values[index], dict):
                raw = dict(values[index])
        summary = _normalise_summary_topics(raw, row["input"], path)
        summary["summary"] = summary.get("summary") or summary.get("core_summary") or ""
        summary["path"] = path
        summary["evidence_chain"] = select_evidence(
            (documents[rows.index(row)].get("document") or {}).get("evidence") or [],
            topics=summary.get("topics") or [], max_items=3, per_source=3,
            max_chars=360,
        )
        summary.update(_preview_claim_contract(
            summary, (documents[rows.index(row)].get("document") or {}).get("evidence") or []
        ))
        summary.update({
            "schema_version": 4,
            "summary_type": "file",
            "node_path": path,
            "generated_by": "model-preview-batch-analysis",
            "analysis_depth": "preview_document",
            "analysis_level": "preview",
            "verification_status": "candidate",
            "deep_analysis": False,
            "preview_only": True,
            "preview_input_chars": len(row["input"]),
            "preview_coverage": {"mode": "selective", "selected_chars": len(row["input"]), "complete": False},
            "parser_info": {"model": result.get("model"), "usage": result.get("usage") or {}, "batch_size": len(rows)},
            "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        })
        output[path] = {"summary": summary, "usage": result.get("usage") or {},
                        "model": result.get("model")}
    return output, result


def _structured_record_input(unified_document, max_chars=52000):
    """Build a bounded, source-addressable prompt for structured data."""
    document = unified_document or {}
    parser = document.get("parser") or {}
    structure = document.get("structure") or {}
    profile = document.get("data_profile") or {}
    evidence = [
        item for item in (document.get("evidence") or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    preferred = [
        item for item in evidence
        if "CVSS：" in str(item.get("text") or "")
        or "漏洞类型：" in str(item.get("text") or "")
    ]
    selected = []
    seen = set()

    def add(item):
        key = str(item.get("evidence_id") or item.get("text") or "")
        if key and key not in seen:
            seen.add(key)
            selected.append(item)

    for item in preferred[:36]:
        add(item)
    remaining = max(12, min(96, len(evidence)))
    if evidence:
        positions = sorted(set(
            round(index * (len(evidence) - 1) / float(max(1, remaining - 1)))
            for index in range(remaining)
        ))
        for index in positions:
            add(evidence[index])

    rows = []
    for item in selected:
        rows.append({
            "evidence_id": item.get("evidence_id"),
            "source_path": item.get("source_path"),
            "section": item.get("section") or "JSON记录",
            "record_id": item.get("record_id"),
            "text": " ".join(str(item.get("text") or "").split())[:620],
        })
    metadata = {
        "parser": parser,
        "structure": structure,
        "data_profile": profile,
        "record_count": parser.get("record_count") or len(evidence),
        "evidence_record_count": len(evidence),
        "sampled_record_count": len(rows),
        "source_coverage": (document.get("coverage") or {}).get(
            "structured_records_complete", True
        ),
    }
    payload = "解析元数据：{}\n记录级原文证据：{}".format(
        json.dumps(metadata, ensure_ascii=False),
        json.dumps(rows, ensure_ascii=False),
    )
    # Structured files can contain millions of records. Keep model input bounded
    # while retaining complete record counts and coverage metadata for claims.

    input_limit = min(24000, max(12000, int(max_chars or 12000)))
    return payload[:input_limit], rows, metadata

def _analyze_structured_document(llm, node_path, unified_document,
                                 context_window_tokens):
    """Analyze structured JSON/JSONL without prose chunk explosion."""
    selected_text, evidence_rows, metadata = _structured_record_input(
        unified_document
    )
    if not evidence_rows:
        raise ValueError("结构化文件未生成可回查的记录级证据")
    prompt = """请分析结构化数据文件“{path}”。解析器已对源文件进行完整记录遍历，
下面的 evidence_id 对应源文件中的记录级原文。请只根据给定记录和解析元数据作答，
不要把抽样记录写成文件的全部记录，不要引入外部事实。

{content}

输出 JSON：
{{"title":"数据集标题","structure_overview":{{"sections":["字段或内容类型"],"document_type":"结构化漏洞数据"}},
"core_summary":"说明数据集记录的实际主题、覆盖范围和主要风险类型",
"key_facts":["可由记录直接核验的事实"],"arguments":["基于多条记录的归纳"],
"methodology":["数据中体现的分类或统计口径"],"conclusions":["谨慎的文件级结论"],
"uncertainties":["记录或覆盖范围的限制"],"warnings":["需要复核的限制"],
"recommended_research_direction":{{"title":"方向","rationale":"理由","questions":["问题"]}}}}

要求：最多输出 8 条事实、6 条结论；每条结论尽量包含可核验的产品、漏洞类型、CVE 或统计信息；
不要声称没有提供的数据；不要把 evidence_id 写入正文列表。""".format(
        path=node_path,
        content=selected_text,
    )
    result = llm.chat_json(
        "你是严谨的结构化漏洞数据分析助手，必须区分记录事实与跨记录归纳。" + CHINESE_CONCLUSION_RULE,
        prompt,
        max_tokens=3200,
        long_output=True,
        strict=True,
        retries=1,
        timeout=180,
        required_fields=("core_summary",),
        output_context="结构化文件深度分析",
    )
    summary = _normalise_summary_topics(
        result["json"], selected_text, node_path
    )
    evidence_items = (unified_document or {}).get("evidence", [])
    summary["evidence_chain"] = select_evidence(
        evidence_items,
        topics=(summary.get("topics") or [])
        + list(summary.get("conclusions") or [])[:4],
        max_items=12,
        per_source=12,
        max_chars=520,
    )
    coverage = {
        "parser": "structured-json",
        "extracted_chars": len(str((unified_document or {}).get("text") or "")),
        "document_chunks": 1,
        "successfully_analyzed_chunks": 1,
        "failed_chunks": [],
        "local_limit_truncated": False,
        "structured_record_count": metadata.get("record_count") or 0,
        "structured_evidence_record_count": metadata.get("evidence_record_count") or 0,
        "structured_sampled_record_count": metadata.get("sampled_record_count") or 0,
        "structured_records_complete": bool(metadata.get("source_coverage")),
        "metadata": metadata,
        "warnings": list((unified_document or {}).get("warnings", [])),
        "model_calls": [_model_call_profile(
            "structured_record_analysis", result, 3200,
            context_window_tokens, 1,
        )],
    }
    _attach_call_statistics(coverage)
    return summary, coverage, result

def analyze_document(llm, path, node_path, max_chars=2000000, max_chunks=64,
                     unified_document=None, preferred_chunk_chars=42000,
                     context_window_tokens=65536):
    parser_name = str((unified_document or {}).get("parser", {}).get("name") or "").lower()
    structured_evidence = any(
        isinstance(item, dict) and item.get("label") == "json_record"
        for item in (unified_document or {}).get("evidence") or []
    )
    structured_path = str(path or node_path or "").lower().endswith((".json", ".jsonl"))
    if unified_document and (parser_name == "structured-json" or (structured_path and structured_evidence)) :
        return _analyze_structured_document(
            llm, node_path, unified_document, context_window_tokens
        )
    if unified_document:
        raw_text = unified_document.get("text", "")
        unified_coverage = dict(unified_document.get("coverage", {}))
        truncated = (not unified_coverage.get("complete", True)) or len(raw_text) > max_chars
        warnings = list(unified_document.get("warnings", []))
        if truncated:
            warnings.append("统一正文覆盖率不足，深度摘要只能覆盖已存储正文；请依据覆盖率清单复核。")
        extracted = {
            "text": raw_text[:max_chars],
            "parser": unified_document.get("parser", {}).get("name", "Docling"),
            "warnings": warnings,
            "metadata": dict(_model_metadata(unified_document), coverage=unified_coverage),
            "char_count": min(len(raw_text), max_chars),
            "truncated": truncated,
        }
    else:
        extracted = extract_text(path, max_chars=max_chars)
    if not extracted["text"]:
        raise ValueError("未能从该文件提取正文。{}".format("；".join(extracted["warnings"])))

    text = extracted["text"]
    chunks = _split_text(
        text,
        max_chunks=max_chunks,
        preferred_chars=preferred_chunk_chars,
        max_input_tokens=max(4000, int(preferred_chunk_chars / 3.5)),
    )
    coverage = {
        "parser": extracted["parser"],
        "extracted_chars": extracted["char_count"],
        "document_chunks": len(chunks),
        "local_limit_truncated": extracted["truncated"],
        "metadata": extracted["metadata"],
        "warnings": extracted["warnings"],
        "complete_text_chars": len(text),
        "estimated_input_tokens": sum(item["estimated_input_tokens"] for item in chunks),
        "chunk_soft_limit": max_chunks,
        "chunk_soft_limit_exceeded": len(chunks) > max_chunks,
        "chunking_strategy": "token-budgeted-structure-aware-with-overlap",
    }
    if len(chunks) <= 1:
        prompt = """请完整分析以下文档正文。正文已全部放在本请求中。
文件：{path}
解析元数据：{metadata}
正文：
{text}

输出 JSON：
{{"title":"标题","structure_overview":{{"sections":["章节"],"document_type":"类型"}},"core_summary":"摘要","key_facts":["事实"],"arguments":["论点"],"methodology":["方法"],"conclusions":["结论"],"uncertainties":["不确定信息"],"warnings":["局限"],"recommended_research_direction":{{"title":"方向","rationale":"理由","questions":["问题"]}}}}
只依据正文；不要把程序分块或输入长度描述成原文不完整。""".format(
            path=node_path,
            metadata=json.dumps(extracted["metadata"], ensure_ascii=False),
            text=text,
        )
        result = llm.chat_json(
            "你是严谨的全文文献分析助手，需要覆盖研究问题、方法、主要论点、结论和局限。" + CHINESE_CONCLUSION_RULE,
            prompt,
            max_tokens=3200,
            long_output=True,
            strict=True,
            retries=1,
            timeout=150,
            required_fields=("core_summary",),
            output_context="全文文档分析",
        )
        summary = _normalise_summary_topics(
            result["json"], text, extracted.get("metadata", {}).get("title") or node_path
        )
        coverage["model_calls"] = [_model_call_profile(
            "full_document_analysis", result, 3200, context_window_tokens, 1,
        )]
        if coverage["model_calls"][0]["output_truncated"]:
            coverage["warnings"].append("全文分析输出达到模型预算上限，结论可能不完整，建议继续生成或缩小分析范围。")
        _attach_call_statistics(coverage)
        if unified_document:
            summary["evidence_chain"] = select_evidence(
                unified_document.get("evidence", []),
                topics=summary.get("structure_overview", {}).get("sections", []),
                max_items=12,
                per_source=12,
                max_chars=520,
            )
        summary.update(build_file_claims(summary, (unified_document or {}).get("evidence", [])))
        return summary, coverage, result

    def analyze_chunk(chunk):
        prompt = """这是文档“{path}”的第 {index}/{total} 个连续正文块，字符范围 {start}-{end}。
请只分析本块，输出 JSON：
{{"section_summary":"本块摘要","key_facts":["事实"],"sections":["章节或主题"],"arguments":["论点"],"methodology":["方法"],"conclusions":["结论"],"limitations":["局限"]}}
正文：
{text}""".format(path=node_path, index=chunk["index"], total=len(chunks), start=chunk["start"], end=chunk["end"], text=chunk["text"])
        result = llm.chat_json(
            "你正在进行全文分块阅读。不要猜测其他块内容，只提取当前块的事实和论证。" + CHINESE_CONCLUSION_RULE,
            prompt,
            max_tokens=1800,
            long_output=True,
            strict=True,
            retries=1,
            timeout=150,
            required_fields=("section_summary",),
            output_context="文档分块分析",
        )
        data = result["json"]
        data["chunk_index"] = chunk["index"]
        data["range"] = "{}-{}".format(chunk["start"], chunk["end"])
        return data, result

    results = {}
    with ThreadPoolExecutor(max_workers=min(getattr(llm, "max_concurrency", 1), len(chunks))) as executor:
        futures = {executor.submit(analyze_chunk, chunk): chunk for chunk in chunks}
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                data, call_result = future.result()
                data["model_call"] = _model_call_profile(
                    "document_chunk_analysis", call_result, 1800,
                    context_window_tokens, chunk["index"],
                )
                if data["model_call"]["output_truncated"]:
                    data.setdefault("limitations", []).append("本块输出达到模型预算上限，已标记为可能不完整。")
                results[chunk["index"]] = data
            except Exception as exc:
                results[chunk["index"]] = _chunk_fallback(chunk, exc)
    ordered = [results[index] for index in sorted(results)]
    coverage["model_calls"] = [item["model_call"] for item in ordered if item.get("model_call")]

    compact_chunks = _fit_reduce_items(ordered)
    coverage["reduce_input_chars"] = len(json.dumps(compact_chunks, ensure_ascii=False))
    coverage["reduce_preserved_chunk_count"] = len(compact_chunks)
    merge_prompt = """你已获得文档“{path}”全部 {count} 个连续正文块的压缩分析结果。请合并成全文级结论，不能遗漏后半部分，也不要把分块处理说成原文不完整。
解析元数据：{metadata}
各块分析（每块的事实、论点、方法和结论均已限量保留）：{chunks}

输出 JSON：
{{"title":"标题","structure_overview":{{"sections":["章节"],"document_type":"类型"}},"core_summary":"全文摘要","key_facts":["事实"],"arguments":["主要论点"],"methodology":["方法"],"conclusions":["结论"],"uncertainties":["不确定信息"],"warnings":["原文明确局限或解析局限"],"recommended_research_direction":{{"title":"首选方向","rationale":"理由","questions":["研究问题"]}}}}
硬约束：必须覆盖第 1 到第 {count} 块；各事实列表最多 12 条并按全文覆盖度去重；core_summary 不超过 1800 字。
只有在解析元数据明确显示截断或有页面无文本层时，才能报告解析覆盖问题。""".format(
        path=node_path,
        count=len(chunks),
        metadata=json.dumps(extracted["metadata"], ensure_ascii=False),
        chunks=json.dumps(compact_chunks, ensure_ascii=False),
    )
    try:
        final_result = llm.chat_json(
            "你是全文文献综合分析助手。必须综合所有分块，区分作者结论、事实和局限。" + CHINESE_CONCLUSION_RULE,
            merge_prompt,
            max_tokens=3200,
            long_output=True,
            strict=True,
            retries=1,
            timeout=180,
            required_fields=("core_summary",),
            output_context="全文分块汇总",
        )
        summary = _normalise_summary_topics(final_result["json"], text, node_path)
    except LocalModelError as exc:
        summary = _local_merge(node_path, chunks, ordered, extracted["warnings"] + ["最终本地模型汇总失败：{}".format(exc)])
        final_result = {"model": None, "usage": {}, "content": ""}

    reduce_call = _model_call_profile(
        "document_chunk_reduce", final_result, 3200, context_window_tokens,
    )
    coverage["model_calls"].append(reduce_call)
    if reduce_call["output_truncated"]:
        coverage["warnings"].append("全文汇总输出达到模型预算上限，已标记为可能不完整。")
    _attach_call_statistics(coverage)

    failed_chunks = [item["chunk_index"] for item in ordered if item.get("error")]
    coverage["failed_chunks"] = failed_chunks
    coverage["successfully_analyzed_chunks"] = len(chunks) - len(failed_chunks)
    if unified_document:
        summary["evidence_chain"] = select_evidence(
            unified_document.get("evidence", []),
            topics=summary.get("structure_overview", {}).get("sections", []),
            max_items=12,
            per_source=12,
            max_chars=520,
        )
    summary.update(build_file_claims(summary, (unified_document or {}).get("evidence", [])))
    return summary, coverage, final_result
