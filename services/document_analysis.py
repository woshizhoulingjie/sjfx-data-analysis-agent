import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.ollama import LocalModelError
from services.evidence import select_evidence
from services.scanner import extract_text


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
    return {
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
    }


def analyze_document(llm, path, node_path, max_chars=2000000, max_chunks=64,
                     unified_document=None, preferred_chunk_chars=42000,
                     context_window_tokens=65536):
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
            "metadata": dict(unified_document.get("structure", {}), coverage=unified_coverage),
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
            "你是严谨的全文文献分析助手，需要覆盖研究问题、方法、主要论点、结论和局限。",
            prompt,
            max_tokens=3200,
            strict=True,
            retries=1,
            timeout=150,
            required_fields=("core_summary",),
            output_context="全文文档分析",
        )
        summary = result["json"]
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
        return summary, coverage, result

    def analyze_chunk(chunk):
        prompt = """这是文档“{path}”的第 {index}/{total} 个连续正文块，字符范围 {start}-{end}。
请只分析本块，输出 JSON：
{{"section_summary":"本块摘要","key_facts":["事实"],"sections":["章节或主题"],"arguments":["论点"],"methodology":["方法"],"conclusions":["结论"],"limitations":["局限"]}}
正文：
{text}""".format(path=node_path, index=chunk["index"], total=len(chunks), start=chunk["start"], end=chunk["end"], text=chunk["text"])
        result = llm.chat_json(
            "你正在进行全文分块阅读。不要猜测其他块内容，只提取当前块的事实和论证。",
            prompt,
            max_tokens=1800,
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
            "你是全文文献综合分析助手。必须综合所有分块，区分作者结论、事实和局限。",
            merge_prompt,
            max_tokens=3200,
            strict=True,
            retries=1,
            timeout=180,
            required_fields=("core_summary",),
            output_context="全文分块汇总",
        )
        summary = final_result["json"]
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
    return summary, coverage, final_result
