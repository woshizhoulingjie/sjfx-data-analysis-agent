"""Deterministic final gate for grounded package-analysis answers."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

from services.conversation import ConversationScope
from services.evidence import verify_claim_evidence
from services.evidence_ledger import build_evidence_ledger


NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:%|％|万|亿|吨|元|天|次|例|倍|项|种|个|份|人|年|月|日)?"
)
SECTION_LABEL_RE = re.compile(
    r"^(?:直接回答|资料依据|主要结论|分析结论|核验后的结论|核验说明|进一步分析或建议|风险提示|"
    r"时间线|关系分析|矛盾检查|统计结果)[:：]?$"
)
VERIFICATION_NOTE_RE = re.compile(
    r"(?:条陈述因证据不足|未作为确定结论输出|未找到直接原文支持|避免伪造事实)"
)
PROVENANCE_NOTE_RE = re.compile(
    r"^(?:(?:本次|当前|以上)?(?:分析|回答|结论|核验)(?:是)?(?:基于|依据|引用自)|"
    r"(?:证据|引用)(?:来自|来源于)|本次(?:检查|检索)(?:了|范围为))",
    re.I,
)
NEGATION_RE = re.compile(r"(?:未|无|没有|不能|无法|不可|并非|不再|尚未)")


def _numbers(value: Any) -> set:
    value = re.sub(r"\[\d+\]", "", str(value or ""))
    return {re.sub(r"\s+", "", item) for item in NUMBER_RE.findall(value)}


def _citation_item(citation: Mapping[str, Any]) -> Dict[str, Any]:
    item = dict(citation or {})
    item["text"] = str(
        item.get("original_text") or item.get("text") or item.get("translated_text") or ""
    )
    item.setdefault(
        "label",
        "structured_column"
        if item.get("evidence_role") == "structured_statistic"
        else "text_chunk",
    )
    return item


def _concepts(value: Any) -> set:
    text = re.sub(r"\[\d+\]", "", str(value or "")).lower()
    output = set(re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", text))
    for block in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        output.update(block[index : index + 2] for index in range(len(block) - 1))
    return output


def _claim_body(value: Any) -> str:
    text = re.sub(r"\[\d+\]", "", str(value or ""))
    text = re.sub(r"^(?:部分证据支持|分析判断)[:：]\s*", "", text)
    return text.strip(" -*#\t")


def _identifiers(value: Any) -> set:
    """Return field-like tokens useful for exact key/value verification."""
    return {
        item.lower()
        for item in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", str(value or ""))
        if item.lower() not in {"the", "and", "from", "file", "value", "json"}
    }


def _scope_contains(scope: ConversationScope, citation: Mapping[str, Any]) -> bool:
    path = str(citation.get("archive_source_path") or citation.get("source_path") or "")
    return not path or scope.contains_source(path)


class ClaimVerifier:
    """Verify every factual sentence against the exact citations it names."""

    def verify(
        self,
        turn_result: Mapping[str, Any],
        plan: Mapping[str, Any],
        tool_results: Optional[Mapping[str, Any]] = None,
        batch_summary: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        turn_result = dict(turn_result or {})
        plan = dict(plan or {})
        tool_results = dict(tool_results or plan.get("tool_results") or {})
        batch_summary = dict(batch_summary or plan.get("batch_summary") or {})
        citations = list(turn_result.get("citations") or [])
        by_index = {
            int(item.get("citation_index") or index): item
            for index, item in enumerate(citations, 1)
        }
        ledger = build_evidence_ledger(str(turn_result.get("answer") or ""), citations)
        casual = "casual" in (plan.get("modes") or [])
        scope = ConversationScope.from_dict(plan.get("scope") or {"kind": "package"})
        warnings: List[str] = []
        invalid_labels = 0
        numeric_failures = 0
        out_of_scope = [item for item in citations if not _scope_contains(scope, item)]

        verified_claims = []
        for claim in ledger.get("claims") or []:
            claim = dict(claim)
            text = str(claim.get("text") or "").strip()
            indexes = list(claim.get("citation_indexes") or [])
            if SECTION_LABEL_RE.match(text):
                claim.update({"status": "section_label", "evidence_ids": []})
                verified_claims.append(claim)
                continue
            if VERIFICATION_NOTE_RE.search(text) or PROVENANCE_NOTE_RE.search(
                _claim_body(text)
            ):
                claim.update({"status": "analysis_or_advice", "evidence_ids": []})
                verified_claims.append(claim)
                continue
            if claim.get("status") == "analysis_or_advice":
                verified_claims.append(claim)
                continue

            check_text = _claim_body(text)
            missing = [index for index in indexes if index not in by_index]
            if missing:
                invalid_labels += len(missing)
            named = [by_index[index] for index in indexes if index in by_index]
            evaluations = [
                verify_claim_evidence(check_text, _citation_item(item)) for item in named
            ]
            claim_concepts = _concepts(check_text)
            for evaluation, item in zip(evaluations, named):
                if evaluation.get("support_status") != "insufficient":
                    continue
                evidence_text = _citation_item(item).get("text") or ""
                overlap = claim_concepts.intersection(_concepts(evidence_text))
                same_polarity = bool(NEGATION_RE.search(check_text)) == bool(
                    NEGATION_RE.search(evidence_text)
                )
                concept_ratio = len(overlap) / float(
                    min(len(claim_concepts), len(_concepts(evidence_text))) or 1
                )
                if len(overlap) >= 4 and concept_ratio >= 0.30 and same_polarity:
                    evaluation.update(
                        {
                            "support_status": "supported",
                            "support_score": round(min(0.85, 0.65 + concept_ratio / 3), 3),
                            "support_reason": "概括性结论与原文存在充分的实质概念重合，且否定方向一致。",
                            "support_relation": "lexical_summary",
                            "matched_concepts": sorted(overlap)[:8],
                            "concept_coverage": round(concept_ratio, 6),
                        }
                    )
                elif len(overlap) >= 2 and same_polarity:
                    evaluation.update(
                        {
                            "support_status": "partially_supported",
                            "support_score": 0.55,
                            "support_reason": "结论与原文存在多个共同概念，但概括程度较高。",
                            "support_relation": "lexical_summary",
                            "matched_concepts": sorted(overlap)[:8],
                            "concept_coverage": round(concept_ratio, 6),
                        }
                    )
            supported = [
                item for item in evaluations if item.get("support_status") == "supported"
            ]
            partial = [
                item
                for item in evaluations
                if item.get("support_status") == "partially_supported"
            ]
            claim_numbers = _numbers(check_text)
            citation_numbers = set()
            for item in named:
                citation_numbers.update(_numbers(_citation_item(item).get("text")))
            numeric_supported = not claim_numbers or claim_numbers.issubset(citation_numbers)
            claim_identifiers = _identifiers(check_text)
            if claim_numbers and numeric_supported and claim_identifiers:
                for evaluation, item in zip(evaluations, named):
                    evidence_identifiers = _identifiers(
                        _citation_item(item).get("text")
                    )
                    if claim_identifiers.intersection(evidence_identifiers):
                        evaluation.update({
                            "support_status": "supported",
                            "support_score": max(
                                0.92, float(evaluation.get("support_score") or 0)
                            ),
                            "support_reason": "字段名和精确数值均可在对应原文中直接核对。",
                            "support_relation": "exact_field_value",
                            "matched_identifiers": sorted(
                                claim_identifiers.intersection(evidence_identifiers)
                            )[:8],
                        })
                supported = [
                    item for item in evaluations
                    if item.get("support_status") == "supported"
                ]
                partial = [
                    item for item in evaluations
                    if item.get("support_status") == "partially_supported"
                ]
            if not numeric_supported:
                numeric_failures += 1

            scoped_named = [item for item in named if _scope_contains(scope, item)]
            if supported and numeric_supported and scoped_named and not missing:
                status = "supported"
            elif (supported or partial) and numeric_supported and scoped_named and not missing:
                status = "partially_supported"
            else:
                status = "unsupported"
            if missing:
                failure_reason = "引用标号不存在：{}".format(
                    "、".join(str(item) for item in missing)
                )
            elif not named:
                failure_reason = "陈述没有关联正文引用"
            elif not numeric_supported:
                missing_numbers = sorted(claim_numbers - citation_numbers)
                failure_reason = "引用中缺少数值：{}".format(
                    "、".join(missing_numbers)
                )
            elif status == "unsupported":
                failure_reason = "引用正文与陈述的核心含义不匹配"
            elif status == "partially_supported":
                failure_reason = "引用只支持陈述的一部分"
            else:
                failure_reason = None
            claim.update(
                {
                    "status": status,
                    "evidence_ids": list(
                        dict.fromkeys(
                            str(item.get("evidence_id"))
                            for item in scoped_named
                            if item.get("evidence_id")
                        )
                    ),
                    "verification": {
                        "citation_indexes": indexes,
                        "missing_citation_indexes": missing,
                        "numeric_values": sorted(claim_numbers),
                        "numeric_supported": numeric_supported,
                        "failure_reason": failure_reason,
                        "evidence_checks": evaluations,
                    },
                }
            )
            verified_claims.append(claim)

        supported_count = sum(item.get("status") == "supported" for item in verified_claims)
        partial_count = sum(
            item.get("status") == "partially_supported" for item in verified_claims
        )
        unsupported_count = sum(
            item.get("status") == "unsupported" for item in verified_claims
        )
        factual_count = supported_count + partial_count + unsupported_count
        support_ratio = round(supported_count / float(factual_count or 1), 6)

        counter_result = dict(tool_results.get("counter_evidence_search") or {})
        contradiction_result = dict(tool_results.get("contradiction_detector") or {})
        counter_items = list(counter_result.get("items") or [])
        contradictions = list(contradiction_result.get("items") or [])
        expected_counter_search = any(
            item.get("tool") == "counter_evidence_search"
            for item in plan.get("steps") or []
        )
        counter_search_performed = bool(counter_result) or not expected_counter_search

        coverage = dict(turn_result.get("coverage") or {})
        query_coverage = coverage.get("query_coverage")
        broad_scope_modes = {
            "summary", "comparison", "timeline", "relationship",
            "contradiction", "risk", "multi_task",
        }
        scope_files = int(
            batch_summary.get("inventory_files")
            or coverage.get("scope_files")
            or coverage.get("total_files")
            or 0
        )
        inspected_scope_files = int(
            batch_summary.get("inspected_files")
            or coverage.get("inspected_files")
            or coverage.get("retrieved_files")
            or 0
        )
        scope_incomplete = bool(
            broad_scope_modes.intersection(plan.get("modes") or [])
            and scope_files
            and inspected_scope_files < scope_files
        )
        if not casual and not citations:
            warnings.append("当前回答没有可持久化的正文引用。")
        if unsupported_count:
            warnings.append(
                "有 {} 条事实性陈述没有通过原文支持核验。".format(
                    unsupported_count
                )
            )
            for item in verified_claims:
                if item.get("status") != "unsupported":
                    continue
                reason = (item.get("verification") or {}).get("failure_reason")
                warnings.append(
                    "未通过核验：{}（{}）。".format(
                        _claim_body(item.get("text"))[:100],
                        reason or "缺少直接原文支持",
                    )
                )
                if sum(value.startswith("未通过核验：") for value in warnings) >= 5:
                    break
        if partial_count:
            warnings.append("有 {} 条陈述只得到部分支持。".format(partial_count))
        if numeric_failures:
            warnings.append("有 {} 条数字结论无法在对应证据中核对。".format(numeric_failures))
        if invalid_labels:
            warnings.append("有 {} 个引用标号无对应证据。".format(invalid_labels))
        if out_of_scope:
            warnings.append("有 {} 条引用超出用户指定范围。".format(len(out_of_scope)))
        if expected_counter_search and not counter_search_performed:
            warnings.append("反向证据检索未完成。")
        if query_coverage is not None and float(query_coverage) < 0.45:
            warnings.append("当前问题的候选证据覆盖率较低。")
        if scope_incomplete:
            warnings.append(
                "当前结论仅基于候选文件：实际检查 {}/{} 个范围文件，"
                "不能视为全范围完整结论。".format(
                    inspected_scope_files, scope_files
                )
            )
        if contradictions:
            warnings.append("检出 {} 组需要保留的矛盾或不一致证据。".format(len(contradictions)))

        if casual:
            status = "not_required"
        elif not citations or not factual_count:
            status = "insufficient_evidence"
        elif (
            not unsupported_count
            and not partial_count
            and not invalid_labels
            and not out_of_scope
            and not numeric_failures
            and not scope_incomplete
        ):
            status = "verified"
        elif citations and factual_count:
            status = "partial"
        else:
            status = "insufficient_evidence"

        inventory_files = batch_summary.get("inventory_files") or coverage.get("total_files")
        candidate_files = (
            batch_summary.get("candidate_files") or coverage.get("candidate_files") or 0
        )
        inspected_files = batch_summary.get("inspected_files") or candidate_files
        unparsed_files = batch_summary.get("unparsed_files")
        if (
            unparsed_files is None
            and coverage.get("total_files") is not None
            and coverage.get("searchable_files") is not None
        ):
            unparsed_files = max(
                0,
                int(coverage.get("total_files") or 0)
                - int(coverage.get("searchable_files") or 0),
            )
        quality_metrics = {
            "verification_status": status,
            "inventory_files": inventory_files,
            "candidate_files": int(candidate_files or 0),
            "inspected_files": int(inspected_files or 0),
            "batch_count": int(batch_summary.get("batch_count") or 0),
            "evidence_records": int(
                batch_summary.get("evidence_records") or len(citations)
            ),
            "citation_count": len(citations),
            "factual_claim_count": factual_count,
            "supported_claim_count": supported_count,
            "partially_supported_claim_count": partial_count,
            "unsupported_claim_count": unsupported_count,
            "claim_support_ratio": support_ratio,
            "query_coverage": query_coverage,
            "coverage_basis": coverage.get("coverage_basis"),
            "scope_files": scope_files or None,
            "scope_inspection_coverage": coverage.get(
                "scope_inspection_coverage"
            ) if coverage.get("scope_inspection_coverage") is not None else (
                round(inspected_scope_files / float(scope_files), 6)
                if scope_files else None
            ),
            "candidate_deep_coverage": coverage.get("candidate_deep_coverage"),
            "deep_candidate_files": int(coverage.get("deep_candidate_files") or 0),
            "deep_analyzed_files": coverage.get("deep_analyzed_files"),
            "scope_incomplete": scope_incomplete,
            "counter_evidence_count": len(counter_items),
            "contradiction_count": len(contradictions),
            "unparsed_files": int(unparsed_files or 0),
            "numeric_failure_count": numeric_failures,
            "out_of_scope_citation_count": len(out_of_scope),
        }
        # Canonical names for new API/UI consumers; retain legacy fields above.
        quality_metrics.update({
            "retrieval_coverage": query_coverage,
            "inspection_coverage": quality_metrics["scope_inspection_coverage"],
            "deep_analysis_coverage": quality_metrics["candidate_deep_coverage"],
            "evidence_support_ratio": support_ratio,
        })
        ledger.update(
            {
                "claims": verified_claims,
                "supported_claim_count": supported_count,
                "partially_supported_claim_count": partial_count,
                "unsupported_claim_count": unsupported_count,
                "claim_support_ratio": support_ratio,
            }
        )
        return {
            "schema_version": "claim-verification/2.0",
            "status": status,
            "needs_revision": status not in {"verified", "not_required"},
            "scope_checked": True,
            "citation_labels_checked": True,
            "citation_entailment_checked": True,
            "numeric_values_checked": True,
            "counter_evidence_checked": counter_search_performed,
            "warnings": warnings,
            "failed_claims": [
                {
                    "claim_id": item.get("claim_id"),
                    "text": item.get("text"),
                    "reason": (item.get("verification") or {}).get(
                        "failure_reason"
                    ),
                }
                for item in verified_claims
                if item.get("status") in {"unsupported", "partially_supported"}
            ],
            "counter_evidence": counter_items[:20],
            "contradictions": contradictions[:20],
            "quality_metrics": quality_metrics,
            "ledger": ledger,
        }

    def guard_result(
        self, turn_result: Mapping[str, Any], verification: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Remove unsupported factual claims after the repair budget is spent."""
        result = dict(turn_result or {})
        verification = dict(verification or {})
        claims = list((verification.get("ledger") or {}).get("claims") or [])
        kept: List[str] = []
        omitted = 0
        for claim in claims:
            status = claim.get("status")
            text = str(claim.get("text") or "").strip()
            if not text or status == "section_label":
                continue
            if status == "supported":
                kept.append(text)
            elif status == "partially_supported":
                kept.append("部分证据支持：{}".format(text))
            elif status == "analysis_or_advice":
                kept.append("分析判断：{}".format(text))
            else:
                omitted += 1
        if kept:
            # Rebuild the list after filtering so removed claims never leave
            # broken numbering or orphaned sections in the final answer.
            answer = "\n".join(
                "{}. {}".format(index, item)
                for index, item in enumerate(kept, 1)
            )
        else:
            answer = "现有证据不足，暂时无法形成可核验的确定结论。"
        if omitted:
            answer += (
                "\n\n核验说明\n- {} 条陈述因证据不足或仅得到部分支持，"
                "未作为确定结论输出。"
            ).format(omitted)
        result["answer"] = answer
        result["evidence_status"] = "supported" if kept and not omitted else "partial"
        result["status"] = "answered" if kept else "insufficient_evidence"
        stale_verification_warning = re.compile(
            r"(?:事实性陈述没有通过|陈述只得到部分支持|数字结论无法|"
            r"引用标号无对应证据|候选证据覆盖率较低|当前回答没有可持久化的正文引用|"
            r"^未通过核验：)"
        )
        prior_warnings = [
            item for item in (result.get("warnings") or [])
            if not stale_verification_warning.search(str(item))
        ]
        if result.get("citations"):
            prior_warnings = [
                item for item in prior_warnings
                if "没有可检索正文证据" not in str(item)
            ]
        result["warnings"] = list(
            dict.fromkeys(
                prior_warnings + list(verification.get("warnings") or [])
            )
        )
        result["citation_verification_status"] = verification.get("status")
        result["scope_completeness"] = (
            "candidate_only"
            if (verification.get("quality_metrics") or {}).get("scope_incomplete")
            else "checked_scope"
        )
        return result
