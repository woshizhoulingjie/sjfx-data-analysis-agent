import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.deepseek import DeepSeekError
from services.evidence import select_evidence
from services.scanner import extract_text


def _split_text(text, max_chunks=12, preferred_chars=32000):
    if not text:
        return []
    chunk_size = max(preferred_chars, int(math.ceil(len(text) / float(max(1, max_chunks)))))
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        if end < len(text):
            boundary = text.rfind("\n", start + int(chunk_size * 0.75), end)
            if boundary > start:
                end = boundary
        chunks.append({"index": len(chunks) + 1, "start": start, "end": end, "text": text[start:end]})
        start = end
    return chunks


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


def analyze_document(llm, path, node_path, max_chars=2000000, max_chunks=12, unified_document=None):
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
    # Use map-reduce only when the document is genuinely long. Shared Ollama
    # requests are especially sensitive to unnecessary chunk/merge calls.
    if len(text) <= 50000:
        effective_chunks = 1
    elif len(text) <= 180000:
        effective_chunks = min(max_chunks, 4)
    elif len(text) <= 600000:
        effective_chunks = min(max_chunks, 6)
    else:
        effective_chunks = min(max_chunks, 10)
    chunks = _split_text(text, max_chunks=effective_chunks)
    coverage = {
        "parser": extracted["parser"],
        "extracted_chars": extracted["char_count"],
        "document_chunks": len(chunks),
        "local_limit_truncated": extracted["truncated"],
        "metadata": extracted["metadata"],
        "warnings": extracted["warnings"],
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
            max_tokens=2600,
            strict=True,
            retries=1,
            timeout=150,
        )
        summary = result["json"]
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
            max_tokens=1100,
            strict=True,
            retries=1,
            timeout=150,
        )
        data = result["json"]
        data["chunk_index"] = chunk["index"]
        data["range"] = "{}-{}".format(chunk["start"], chunk["end"])
        return data

    results = {}
    with ThreadPoolExecutor(max_workers=min(getattr(llm, "max_concurrency", 1), len(chunks))) as executor:
        futures = {executor.submit(analyze_chunk, chunk): chunk for chunk in chunks}
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                results[chunk["index"]] = future.result()
            except Exception as exc:
                results[chunk["index"]] = _chunk_fallback(chunk, exc)
    ordered = [results[index] for index in sorted(results)]

    compact_chunks = [_compact_chunk_result(item) for item in ordered]
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
            max_tokens=2200,
            strict=True,
            retries=1,
            timeout=180,
        )
        summary = final_result["json"]
    except DeepSeekError as exc:
        summary = _local_merge(node_path, chunks, ordered, extracted["warnings"] + ["最终云端汇总失败：{}".format(exc)])
        final_result = {"model": None, "usage": {}, "content": ""}

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
