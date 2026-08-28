"""Deterministic analysis tools and bounded map/reduce helpers."""

from __future__ import annotations

import json
import re
from collections import Counter, OrderedDict, defaultdict
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from services.conversation import ConversationScope, RetrievalRequest


TOOL_LABELS = {
    "conversation_response": "基础交流",
    "document_discovery": "寻找相关文件",
    "evidence_search": "检索正文证据",
    "structured_calculation": "结构化计算",
    "cross_file_compare": "跨文件比较",
    "timeline_builder": "构建时间线",
    "relationship_analyzer": "分析关系",
    "contradiction_detector": "检查矛盾",
    "risk_analyzer": "分析风险",
    "summary_reducer": "归并分析结果",
    "translation_tool": "翻译证据",
    "counter_evidence_search": "寻找反向证据",
    "claim_verifier": "核验结论",
    "answer_composer": "组织回答",
}

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,8}")
DATE_RE = re.compile(
    r"(?:19|20)\d{2}(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?|年)?|"
    r"\d{1,2}月\d{1,2}日"
)
NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(-?\d+(?:\.\d+)?)\s*(%|％|万|亿|吨|元|天|次|例|倍|项|种|个|份|人)?"
)
NEGATION_RE = re.compile(r"(?:未|没有|不能|无法|不可|并非|不再|尚未|无)")
COUNTER_RE = re.compile(r"(?:但是|然而|例外|除外|反之|相反|并非|不一致|未能|无法|争议|否认)")
RELATION_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9_.-]{1,30}|[\u4e00-\u9fff]{2,20})"
    r"\s*(支持|反对|投资|收购|合作|签署|负责|隶属|任职|联系|支付|供应|委托|起诉|"
    r"攻击|控制|依赖|导致|影响|提供|关联)\s*"
    r"([A-Za-z][A-Za-z0-9_.-]{1,30}|[\u4e00-\u9fff]{2,20})"
)
RISK_RULES = OrderedDict(
    (
        ("责任与违约", re.compile(r"违约|赔偿|责任|罚款|违约金|追责")),
        ("付款与资金", re.compile(r"付款|欠款|逾期|资金|成本|损失|金额")),
        ("合规与法律", re.compile(r"违法|违规|合规|诉讼|仲裁|许可|监管")),
        ("安全与隐私", re.compile(r"安全|泄露|攻击|漏洞|隐私|事故")),
        ("进度与交付", re.compile(r"延期|延误|交付|进度|中断|失败")),
    )
)
STOPWORDS = {
    "这个", "那个", "数据包", "文件", "资料", "内容", "分析", "主要", "相关",
    "进行", "以及", "其中", "已经", "可以", "需要", "问题", "情况", "结果",
}


def _text(item: Mapping[str, Any], limit: int = 1200) -> str:
    value = str(item.get("translated_text") or item.get("text") or item.get("original_text") or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _source(item: Mapping[str, Any]) -> str:
    return str(
        item.get("archive_source_path")
        or str(item.get("source_path") or "").split("::", 1)[0]
    )


def _tokens(value: Any) -> List[str]:
    output = []
    for token in TOKEN_RE.findall(str(value or "").lower()):
        if token in STOPWORDS:
            continue
        output.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 3:
            output.extend(token[index : index + 2] for index in range(len(token) - 1))
    return output


def _sentences(value: Any) -> List[str]:
    return [
        item.strip()
        for item in re.split(r"(?<=[。！？；.!?;])\s*|\n+", str(value or ""))
        if len(item.strip()) >= 6
    ]


def _cancel(cancel_check: Optional[Callable[[], Any]]) -> None:
    if cancel_check:
        cancel_check()


def merge_retrieval_results(
    results: Iterable[Mapping[str, Any]], limit: int = 24
) -> Dict[str, Any]:
    """Merge several bounded searches without materialising the full index."""
    merged: List[Dict[str, Any]] = []
    seen = set()
    deferred: List[str] = []
    coverage_values: List[float] = []
    evidence_coverage_values: List[float] = []
    warnings: List[str] = []
    reported_candidate_files = 0
    total_files = 0
    scope_files = 0
    searchable_files = 0
    deep_analyzed_files = 0
    deep_candidate_files = 0
    candidate_depth_values: List[float] = []
    coverage_bases = set()
    for result in results or []:
        result = dict(result or {})
        result_items = list(result.get("results") or result.get("evidence") or [])
        for item in result_items:
            key = (
                str(item.get("evidence_id") or ""),
                str(item.get("source_path") or ""),
                str(item.get("text") or item.get("original_text") or "")[:500],
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
        coverage = dict(result.get("coverage") or {})
        reported_candidate_files = max(
            reported_candidate_files, int(coverage.get("candidate_files") or 0)
        )
        total_files = max(total_files, int(coverage.get("total_files") or 0))
        scope_files = max(scope_files, int(coverage.get("scope_files") or 0))
        searchable_files = max(
            searchable_files, int(coverage.get("searchable_files") or 0)
        )
        deep_analyzed_files = max(
            deep_analyzed_files, int(coverage.get("deep_analyzed_files") or 0)
        )
        deep_candidate_files = max(
            deep_candidate_files, int(coverage.get("deep_candidate_files") or 0)
        )
        if coverage.get("coverage_basis"):
            coverage_bases.add(str(coverage.get("coverage_basis")))
        if coverage.get("candidate_deep_coverage") is not None:
            try:
                candidate_depth_values.append(
                    float(coverage.get("candidate_deep_coverage"))
                )
            except (TypeError, ValueError):
                pass
        value = coverage.get("query_coverage")
        if value is not None:
            try:
                numeric_coverage = float(value)
                coverage_values.append(numeric_coverage)
                if result_items:
                    evidence_coverage_values.append(numeric_coverage)
            except (TypeError, ValueError):
                pass
        for path in coverage.get("deferred_candidates") or coverage.get("promotion_candidates") or []:
            path = str(path)
            if path and path not in deferred:
                deferred.append(path)
        warnings.extend(str(item) for item in result.get("warnings") or [] if item)
    merged.sort(
        key=lambda item: float(item.get("retrieval_score") or item.get("score") or 0),
        reverse=True,
    )
    merged = merged[: max(1, min(100, int(limit or 24)))]
    visible_candidate_files = len({_source(item) for item in merged if _source(item)})
    scope_inspection_coverage = (
        round(visible_candidate_files / float(scope_files), 6)
        if scope_files else None
    )
    candidate_deep_coverage = (
        min(candidate_depth_values) if candidate_depth_values else None
    )
    scope_based = "scope_inspection" in coverage_bases
    # Repair/counter queries are deliberately broader than the user's primary
    # query and may legitimately return no rows.  Such an empty auxiliary query
    # must not reduce a successful exact lookup to zero coverage.
    effective_query_coverage = (
        max(evidence_coverage_values)
        if evidence_coverage_values
        else (max(coverage_values) if coverage_values else candidate_deep_coverage)
    )
    if merged:
        stale_no_evidence = re.compile(
            r"没有可检索正文证据|未找到可检索(?:正文)?证据|没有找到正文证据|^no match$",
            re.I,
        )
        warnings = [item for item in warnings if not stale_no_evidence.search(item)]
    return {
        "results": merged,
        "coverage": {
            "total_files": total_files or None,
            "scope_files": scope_files or None,
            "searchable_files": searchable_files or None,
            "deep_analyzed_files": deep_analyzed_files or None,
            "deep_candidate_files": deep_candidate_files,
            "candidate_deep_coverage": candidate_deep_coverage,
            "scope_inspection_coverage": scope_inspection_coverage,
            "coverage_basis": "scope_inspection" if scope_based else "candidate_depth",
            "query_coverage": (
                scope_inspection_coverage if scope_based else effective_query_coverage
            ),
            "deferred_candidates": deferred[:100],
            "candidate_files": max(reported_candidate_files, visible_candidate_files),
            "inspected_files": visible_candidate_files,
            "retrieved_files": visible_candidate_files,
        },
        "needs_promotion": bool(deferred),
        "warnings": list(dict.fromkeys(warnings)),
    }


def execute_bounded_searches(
    engine: Any,
    scan_id: str,
    scope: ConversationScope,
    queries: Sequence[str],
    intent: str = "analysis",
    top_k: int = 12,
) -> Dict[str, Any]:
    results = []
    for query in list(queries or [])[:3]:
        results.append(
            engine.retriever.retrieve(
                RetrievalRequest(
                    scan_id=str(scan_id),
                    query=str(query),
                    scope=scope,
                    top_k=max(1, min(20, int(top_k or 12))),
                    intent=str(intent),
                )
            )
        )
    return merge_retrieval_results(results, limit=max(top_k, 8) * 2)


def load_candidate_evidence(
    storage: Any,
    scan_id: str,
    scope: ConversationScope,
    plan: Mapping[str, Any],
    limit: int = 5000,
    cancel_check: Optional[Callable[[], Any]] = None,
) -> List[Dict[str, Any]]:
    """Load a bounded FTS candidate corpus for deterministic tools."""
    limit = max(100, min(5000, int(limit or 5000)))
    queries = list(plan.get("query_variants") or [plan.get("objective") or ""])
    if set(plan.get("modes") or []).intersection({"summary"}) and scope.kind == "package":
        queries.append("")
    merged = []
    seen = set()
    per_query = max(100, limit // max(1, min(3, len(queries))))
    for query in queries[:3]:
        _cancel(cancel_check)
        rows = storage.search_evidence_index(
            scan_id,
            query,
            scope=scope.retrieval_path,
            source_paths=list(scope.source_paths) or None,
            limit=per_query,
        )
        for item in rows:
            key = str(item.get("evidence_id") or "") or (
                _source(item),
                str(item.get("page") or ""),
                _text(item, 300),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
            if len(merged) >= limit:
                return merged
    if not merged:
        merged = list(
            storage.search_evidence_index(
                scan_id,
                "",
                scope=scope.retrieval_path,
                source_paths=list(scope.source_paths) or None,
                limit=min(1000, limit),
            )
        )
    return merged[:limit]


def reduce_evidence_batches(
    evidence: Sequence[Mapping[str, Any]],
    objective: str,
    inventory_paths: Sequence[str],
    file_states: Optional[Mapping[str, Mapping[str, Any]]] = None,
    batch_size: int = 30,
    cancel_check: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Map candidate files into structured profiles and reduce 20-50 at a time."""
    batch_size = max(20, min(50, int(batch_size or 30)))
    query_terms = set(_tokens(objective))
    grouped: "OrderedDict[str, List[Mapping[str, Any]]]" = OrderedDict()
    for item in evidence or []:
        path = _source(item)
        if path:
            grouped.setdefault(path, []).append(item)
    profiles = []
    batches = []
    all_paths = list(grouped)
    for offset in range(0, len(all_paths), batch_size):
        _cancel(cancel_check)
        batch_paths = all_paths[offset : offset + batch_size]
        batch_profiles = []
        for path in batch_paths:
            items = grouped[path]
            ranked = sorted(
                items,
                key=lambda item: (
                    -len(query_terms.intersection(_tokens(_text(item)))),
                    -float(item.get("retrieval_score") or item.get("score") or 0),
                ),
            )
            excerpts = []
            for item in ranked[:3]:
                excerpts.append(
                    {
                        "evidence_id": item.get("evidence_id"),
                        "source_path": item.get("source_path") or path,
                        "text": _text(item, 520),
                        "page": item.get("page"),
                        "section": item.get("section"),
                    }
                )
            profile = {
                "source_path": path,
                "evidence_count": len(items),
                "matched_term_count": max(
                    [len(query_terms.intersection(_tokens(_text(item)))) for item in ranked[:5]]
                    or [0]
                ),
                "date_count": sum(len(DATE_RE.findall(_text(item))) for item in items),
                "number_count": sum(len(NUMBER_RE.findall(_text(item))) for item in items),
                "excerpts": excerpts,
            }
            profiles.append(profile)
            batch_profiles.append(profile)
        batches.append(
            {
                "batch_id": "batch-{:04d}".format(len(batches) + 1),
                "file_count": len(batch_profiles),
                "evidence_count": sum(item["evidence_count"] for item in batch_profiles),
                "source_paths": [item["source_path"] for item in batch_profiles],
                "representative_excerpts": [
                    item["excerpts"][0]
                    for item in batch_profiles
                    if item.get("excerpts")
                ][:12],
            }
        )
    profiles.sort(
        key=lambda item: (-item["matched_term_count"], -item["evidence_count"], item["source_path"])
    )
    states = dict(file_states or {})
    unparsed = sum(
        1
        for path in inventory_paths or []
        if (states.get(str(path)) or {}).get("status") != "completed"
    ) if states else 0
    return {
        "schema_version": "batch-reduction/1.0",
        "batch_size": batch_size,
        "batch_count": len(batches),
        "candidate_files": len(grouped),
        "inspected_files": len(profiles),
        "inventory_files": len(inventory_paths or []),
        "unparsed_files": unparsed,
        "scope_complete": len(profiles) == len(inventory_paths or []),
        "scope_limitation": None if len(profiles) == len(inventory_paths or []) else (
            "结果仅基于检索候选文件，不代表全部范围文件。"
        ),
        "evidence_records": len(evidence or []),
        "batches": batches,
        "file_profiles": profiles,
        "representative_evidence": [
            excerpt
            for profile in profiles[:60]
            for excerpt in profile.get("excerpts") or []
        ][:120],
    }


def build_batch_analysis(
    storage: Any,
    scan_id: str,
    scope: ConversationScope,
    plan: Mapping[str, Any],
    inventory_paths: Sequence[str],
    batch_size: int = 30,
    max_evidence: int = 5000,
    cancel_check: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    evidence = load_candidate_evidence(
        storage, scan_id, scope, plan, limit=max_evidence, cancel_check=cancel_check
    )
    candidate_paths = list(dict.fromkeys(_source(item) for item in evidence if _source(item)))
    states = storage.get_file_states(scan_id, list(inventory_paths or candidate_paths))
    reduced = reduce_evidence_batches(
        evidence,
        str(plan.get("objective") or ""),
        inventory_paths,
        file_states=states,
        batch_size=batch_size,
        cancel_check=cancel_check,
    )
    reduced["candidate_evidence"] = evidence
    return reduced


def _result(items: Sequence[Mapping[str, Any]], **extra: Any) -> Dict[str, Any]:
    output = {"status": "completed", "item_count": len(items), "items": list(items)[:80]}
    output.update(extra)
    return output


def _timeline(evidence: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    items = []
    seen = set()
    for entry in evidence:
        for sentence in _sentences(_text(entry)):
            dates = DATE_RE.findall(sentence)
            for date in dates:
                key = (date, sentence, _source(entry))
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    {
                        "date": date,
                        "event": sentence[:360],
                        "source_path": _source(entry),
                        "evidence_id": entry.get("evidence_id"),
                    }
                )
    items.sort(key=lambda item: (item["date"], item["source_path"]))
    return _result(items, method="deterministic_date_extraction")


def _relationships(evidence: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    edges = {}
    for entry in evidence:
        for sentence in _sentences(_text(entry)):
            for subject, relation, target in RELATION_RE.findall(sentence):
                key = (subject[-30:], relation, target[:30])
                edge = edges.setdefault(
                    key,
                    {
                        "subject": key[0],
                        "relation": relation,
                        "object": key[2],
                        "weight": 0,
                        "evidence_ids": [],
                        "source_paths": [],
                    },
                )
                edge["weight"] += 1
                if entry.get("evidence_id") and entry.get("evidence_id") not in edge["evidence_ids"]:
                    edge["evidence_ids"].append(entry.get("evidence_id"))
                if _source(entry) not in edge["source_paths"]:
                    edge["source_paths"].append(_source(entry))
    items = sorted(edges.values(), key=lambda item: (-item["weight"], item["subject"]))
    return _result(items, method="relation_frame_extraction")


def _number_observations(evidence: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    output = []
    for entry in evidence:
        for sentence in _sentences(_text(entry)):
            for raw, unit in NUMBER_RE.findall(sentence):
                try:
                    value = float(raw)
                except ValueError:
                    continue
                output.append(
                    {
                        "value": value,
                        "raw": raw,
                        "unit": unit or "",
                        "context": sentence[:360],
                        "anchor_terms": sorted(set(_tokens(NUMBER_RE.sub(" ", sentence))))[:12],
                        "negative": bool(NEGATION_RE.search(sentence)),
                        "source_path": _source(entry),
                        "evidence_id": entry.get("evidence_id"),
                    }
                )
    return output


def _structured_calculation(
    evidence: Sequence[Mapping[str, Any]], objective: str
) -> Dict[str, Any]:
    observations = _number_observations(evidence)
    groups = defaultdict(list)
    for item in observations:
        groups[item["unit"]].append(item["value"])
    calculations = []
    for unit, values in sorted(groups.items()):
        calculation = {
            "unit": unit,
            "count": len(values),
            "minimum": min(values),
            "maximum": max(values),
            "average": round(sum(values) / float(len(values)), 6),
        }
        if re.search(r"合计|总和|总额|sum|total", objective, re.I):
            calculation["sum"] = round(sum(values), 6)
        calculations.append(calculation)
    return _result(
        observations,
        calculations=calculations[:20],
        method="deterministic_numeric_extraction",
        warning="数值按原文单位分组，未自动换算不同量纲。",
    )


def _contradictions(evidence: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    observations = _number_observations(evidence)[:1200]
    items = []
    seen = set()
    for index, left in enumerate(observations):
        left_terms = set(left["anchor_terms"])
        if not left_terms:
            continue
        for right in observations[index + 1 :]:
            if left["source_path"] == right["source_path"] or left["unit"] != right["unit"]:
                continue
            overlap = left_terms.intersection(right["anchor_terms"])
            if len(overlap) < 2:
                continue
            if left["value"] == right["value"] and left["negative"] == right["negative"]:
                continue
            key = tuple(sorted((left["source_path"], right["source_path"]))) + tuple(sorted(overlap)[:4])
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "shared_terms": sorted(overlap)[:8],
                    "left": left,
                    "right": right,
                    "reason": "相同主题下的数值或否定方向不一致",
                }
            )
            if len(items) >= 80:
                return _result(items, method="cross_source_value_and_polarity_check")
    return _result(items, method="cross_source_value_and_polarity_check")


def _risks(evidence: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    items = []
    seen = set()
    for entry in evidence:
        for sentence in _sentences(_text(entry)):
            for category, pattern in RISK_RULES.items():
                if not pattern.search(sentence):
                    continue
                key = (category, sentence, _source(entry))
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    {
                        "category": category,
                        "statement": sentence[:360],
                        "source_path": _source(entry),
                        "evidence_id": entry.get("evidence_id"),
                        "classification": "rule_match",
                    }
                )
    return _result(items, method="auditable_risk_rules")


def _counter_evidence(evidence: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    items = []
    for entry in evidence:
        for sentence in _sentences(_text(entry)):
            if COUNTER_RE.search(sentence) or NEGATION_RE.search(sentence):
                items.append(
                    {
                        "statement": sentence[:360],
                        "source_path": _source(entry),
                        "evidence_id": entry.get("evidence_id"),
                        "reason": "包含例外、否定或反向信号",
                    }
                )
    return _result(items, method="counter_signal_search")


def execute_analysis_toolbox(
    plan: Mapping[str, Any],
    batch_summary: Mapping[str, Any],
    cancel_check: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Execute every requested professional tool and return bounded JSON."""
    tools = {item.get("tool") for item in plan.get("steps") or []}
    evidence = list(batch_summary.get("candidate_evidence") or [])
    objective = str(plan.get("objective") or "")
    output: Dict[str, Any] = {}
    handlers = OrderedDict(
        (
            ("structured_calculation", lambda: _structured_calculation(evidence, objective)),
            (
                "cross_file_compare",
                lambda: _result(
                    list(batch_summary.get("file_profiles") or []),
                    compared_files=int(batch_summary.get("inspected_files") or 0),
                    dimensions=["evidence_count", "matched_term_count", "date_count", "number_count"],
                    method="normalized_file_profile_comparison",
                ),
            ),
            ("timeline_builder", lambda: _timeline(evidence)),
            ("relationship_analyzer", lambda: _relationships(evidence)),
            ("contradiction_detector", lambda: _contradictions(evidence)),
            ("risk_analyzer", lambda: _risks(evidence)),
            (
                "summary_reducer",
                lambda: {
                    "status": "completed",
                    "method": "bounded_map_reduce",
                    "batch_size": batch_summary.get("batch_size"),
                    "batch_count": batch_summary.get("batch_count"),
                    "candidate_files": batch_summary.get("candidate_files"),
                    "inspected_files": batch_summary.get("inspected_files"),
                    "batches": list(batch_summary.get("batches") or [])[:100],
                },
            ),
            (
                "translation_tool",
                lambda: {
                    "status": "completed",
                    "translated_evidence_count": sum(
                        1 for item in evidence if item.get("translated_text")
                    ),
                    "original_evidence_count": len(evidence),
                },
            ),
            ("counter_evidence_search", lambda: _counter_evidence(evidence)),
        )
    )
    for name, handler in handlers.items():
        if name not in tools:
            continue
        _cancel(cancel_check)
        result = handler()
        result["analysis_scope"] = {
            "scope_files": int(batch_summary.get("inventory_files") or 0),
            "candidate_files": int(batch_summary.get("candidate_files") or 0),
            "inspected_files": int(batch_summary.get("inspected_files") or 0),
            "scope_complete": bool(batch_summary.get("scope_complete")),
            "limitation": batch_summary.get("scope_limitation"),
        }
        output[name] = result
    return output


def compact_tool_context(
    tool_results: Mapping[str, Any], batch_summary: Mapping[str, Any], limit: int = 16000
) -> str:
    """Serialize tool output for the answer model without unbounded prompt growth."""
    payload = {
        "batch_summary": {
            key: batch_summary.get(key)
            for key in (
                "batch_size",
                "batch_count",
                "candidate_files",
                "inspected_files",
                "inventory_files",
                "unparsed_files",
                "scope_complete",
                "scope_limitation",
                "evidence_records",
            )
        },
        "tool_results": tool_results,
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text[: max(2000, int(limit or 16000))]
