"""Deterministic final gate for grounded package-analysis answers."""

from __future__ import annotations

import re
from itertools import combinations
from typing import Any, Dict, List, Mapping, Optional

from services.conversation import ConversationScope
from services.evidence import evidence_quality, verify_claim_evidence
from services.grounded_clauses import compound_support
from services.evidence_ledger import build_evidence_ledger
from services import answer_quality as aq


NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])[+-]?\d+(?:\.\d+)?\s*(?:" + aq.NUMERIC_UNITS + r")?"
)
SECTION_LABEL_RE = re.compile(
    r"^(?:直接回答|资料依据|主要结论|分析结论|核验后的结论|核验说明|进一步分析或建议|风险提示|"
    r"时间线|关系分析|矛盾检查|统计结果|进一步分析|主要差异|比较结果|下一步建议)[:：]?$"
)
VERIFICATION_NOTE_RE = re.compile(
    r"(?:条陈述因证据不足|未作为确定结论输出|未找到直接原文支持|避免伪造事实|"
    r"^以上概括中，部分细节的依据还不充分，需结合原文进一步确认[。]?$|"
    r"^资料暂时无法支持的内容没有纳入这份回答[。]?$)"
)
PROVENANCE_NOTE_RE = re.compile(
    r"^(?:(?:本次|当前|以上)?(?:分析|回答|结论|核验)(?:是)?(?:基于|依据|引用自)|"
    r"(?:证据|引用)(?:来自|来源于)|本次(?:检查|检索)(?:了|范围为))",
    re.I,
)
NEGATION_RE = re.compile(r"(?:未|无|没有|不能|无法|不可|并非|不再|尚未)")


def _numbers(value: Any) -> set:
    value = aq.numeric_text(re.sub(r"\[\d+\]", "", str(value or "")))
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


def _attribute_claim(
    claim_text: str,
    claim_concepts: set,
    citations: List[Mapping[str, Any]],
    scope: "ConversationScope",
    min_concepts: int = 4,
    max_sources: int = 6,
    min_sources: int = 2,
    min_matched: int = 4,
    min_coverage: float = 0.22,
) -> Optional[Dict[str, Any]]:
    """Attribute an uncited factual claim to the evidence it actually matches.

    A broad package-wide answer is a synthesis over the package reading layer,
    and the model does not always attach ``[n]`` markers to every sentence.
    Treating those sentences as unverified facts discarded the whole answer
    even when the retrieved evidence covered them.  The claim is therefore
    matched against the retrieved evidence using the same concept-overlap bar
    the ordinary per-citation check already uses.  Only strong multi-source
    coverage is accepted; anything weaker stays unsupported and is removed.
    """
    if len(claim_concepts) < min_concepts:
        return None
    scored: List[Any] = []
    for position, citation in enumerate(citations, 1):
        if not _scope_contains(scope, citation) or (aq.enabled() and aq.source_kind(citation) == "preview"):
            continue
        evidence_concepts = _concepts(_citation_item(citation).get("text"))
        if not evidence_concepts:
            continue
        overlap = claim_concepts.intersection(evidence_concepts)
        if len(overlap) < 2:
            continue
        scored.append(
            (len(overlap), int(citation.get("citation_index") or position), overlap)
        )
    if len(scored) < max(2, min_sources):
        return None
    scored.sort(key=lambda value: -value[0])
    selected = scored[:max_sources]
    union: set = set()
    for _count, _index, overlap in selected:
        union.update(overlap)
    if len(union) < min_matched:
        return None
    coverage = len(union) / float(len(claim_concepts) or 1)
    if coverage < min_coverage:
        return None
    # Six overlapping matches need not become six inline references. Find the
    # smallest subset retaining exactly the same matched concepts and numbers;
    # never lower the attribution bar just to make a sentence look cleaner.
    source_numbers = {
        int(item.get("citation_index") or position): _numbers(_citation_item(item).get("text"))
        for position, item in enumerate(citations, 1)
    }
    needed_numbers = _numbers(claim_text) & set().union(
        *(source_numbers[index] for _count, index, _overlap in selected)
    )
    reduced = None
    for size in range(max(2, min_sources), len(selected)):
        for subset in combinations(selected, size):
            concepts = set().union(*(overlap for _count, _index, overlap in subset))
            numbers = set().union(*(source_numbers[index] for _count, index, _overlap in subset))
            if concepts == union and needed_numbers.issubset(numbers):
                reduced = list(subset)
                break
        if reduced is not None:
            selected = reduced
            break
    return {
        "indexes": [index for _count, index, _overlap in selected],
        "coverage": coverage,
        "matched": sorted(union)[:24],
    }


def _strip_list_marker(value: Any) -> str:
    """Remove the model's own leading list marker(s), e.g. "2. " or "(3) ".

    Markers are presentation, not content: they must not be re-numbered by the
    guard, and they must never be read as factual numbers by the numeric gate.
    """
    text = str(value or "").strip()
    for _ in range(3):
        stripped = re.sub(r"^[（(]?\d{1,3}\s*[.、)）]\s*", "", text).strip()
        if stripped == text:
            break
        text = stripped
    return text


def _claim_body(value: Any) -> str:
    text = re.sub(r"\[\d+\]", "", str(value or ""))
    text = re.sub(r"^(?:部分证据支持|分析判断)[:：]\s*", "", text)
    # A leading list marker is presentation, not a factual number.
    text = _strip_list_marker(text)
    return text.strip(" -*#\t")


def _is_section_lead_in(text: Any) -> bool:
    """A short heading ending in a colon is a label, not a factual claim."""
    body = re.sub(r"\[\d+\]", "", str(text or "")).strip(" -*#\t")
    if not body or len(body) > 24:
        return False
    return body.endswith(("：", ":"))


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


def _draft_cites_known_evidence(result: Mapping[str, Any]) -> bool:
    """True when the drafted answer cites at least one real evidence item."""
    citations = [item for item in (result.get("citations") or []) if isinstance(item, Mapping)]
    if not citations:
        return False
    draft = str(result.get("answer") or "")
    indexes = {int(value) for value in re.findall(r"\[(\d{1,4})\]", draft)}
    if not indexes:
        return False
    available = set()
    for position, item in enumerate(citations, 1):
        try:
            available.add(int(item.get("citation_index") or position))
        except (TypeError, ValueError):
            available.add(position)
    return bool(indexes & available)


def _readable_guard_blocks(claims):
    """Keep the author's paragraph structure while filtering failed claims.

    No new factual prose is generated here. Citation attribution is copied only
    from the verified ledger. Headings survive only with a surviving body.
    """
    blocks = []
    pending_headings = []
    last_paragraph = None
    last_was_heading = False
    partial = 0
    omitted = 0
    for position, claim in enumerate(claims):
        status = claim.get("status")
        raw = str(claim.get("raw_text") or claim.get("text") or "").strip()
        if not raw:
            continue
        if status == "section_label":
            if not last_was_heading:
                pending_headings = []
            if not re.fullmatch(r"[| :\-]+", raw):
                pending_headings.append(raw)
            last_was_heading = True
            last_paragraph = None
            continue
        last_was_heading = False
        if status not in {"supported", "partially_supported", "analysis_or_advice"}:
            omitted += 1
            continue
        if status == "partially_supported":
            partial += 1
        # Preserve the actual references when the verifier attributed an
        # uncited synthesis; never guess a reference from paragraph position.
        indexes = claim.get("citation_indexes") or []
        if indexes and not re.search(r"\[\d{1,4}\]", raw):
            labels = " ".join("[{}]".format(int(index)) for index in indexes)
            ending = raw[-1:] if raw.endswith(("。", "！", "？", ".", "!", "?")) else ""
            raw = (raw[:-1] if ending else raw) + " " + labels + ending
        if pending_headings:
            blocks.extend(pending_headings)
            pending_headings = []
        paragraph = claim.get("paragraph_index", position)
        if blocks and paragraph == last_paragraph:
            separator = "" if blocks[-1].endswith(("。", "！", "？")) else " "
            blocks[-1] += separator + raw
        else:
            blocks.append(raw)
        last_paragraph = paragraph
    heading_texts={str(c.get('raw_text') or c.get('text') or '').strip() for c in claims if c.get('status')=='section_label'}
    blocks=[block for index,block in enumerate(blocks)
            if not (re.sub(r'\[\d{1,4}\]', '', block).strip().endswith(('：', ':'))
                    and (index+1==len(blocks) or blocks[index+1] in heading_texts))]
    blocks=[block for index,block in enumerate(blocks)
            if block not in heading_texts or (index+1<len(blocks) and blocks[index+1] not in heading_texts)]
    # Removing an unsupported list item must not leave numbering such as 2,3,4.
    number = 0
    for index, block in enumerate(blocks):
        if re.match(r'^\d{1,3}[.)、]\s+', block):
            number += 1
            blocks[index] = re.sub(r'^\d{1,3}([.)、])', str(number)+r'\1', block, count=1)
        else:
            number = 0
    return blocks, omitted, partial


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
        if aq.enabled() and "model_citations" in turn_result:
            citations = list(turn_result.get("model_citations") or [])
            manifest = turn_result.get("input_manifest") or {}
            if (manifest.get("answer_mode") == "evidence_extract"
                    and manifest.get("model_request_sent") is False
                    and manifest.get("actual_citation_indexes") == []
                    and turn_result.get("model_citations") == []):
                # An extract produced before any model request has its own
                # explicit source set. Empty model input on any other path
                # must never grant access to unsent display evidence.
                fallback_indexes = {index for index in
                    (manifest.get("fallback_citation_indexes") or [])
                    if type(index) is int and index > 0}
                citations = [item for item in (turn_result.get("citations") or [])
                    if isinstance(item, Mapping)
                    and type(item.get("citation_index")) is int
                    and item["citation_index"] in fallback_indexes
                    and aq.source_kind(item) != "preview"
                    and evidence_quality(_citation_item(item)).get("eligible")]
        by_index = {
            int(item.get("citation_index") or index): item
            for index, item in enumerate(citations, 1)
        }
        ledger = build_evidence_ledger(str(turn_result.get("answer") or ""), citations)
        casual = "casual" in (plan.get("modes") or [])
        scope = ConversationScope.from_dict(plan.get("scope") or {"kind": "package"})
        warnings: List[str] = []
        invalid_labels = len((turn_result.get("input_manifest") or {}).get("invalid_generated_citation_indexes") or [])
        numeric_failures = 0
        out_of_scope = [item for item in citations if not _scope_contains(scope, item)]

        verified_claims = []
        for claim in ledger.get("claims") or []:
            claim = dict(claim)
            text = str(claim.get("text") or "").strip()
            indexes = list(claim.get("citation_indexes") or [])
            if claim.get("status") == "section_label" or SECTION_LABEL_RE.match(_claim_body(text)) or _is_section_lead_in(text):
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
                verify_claim_evidence(check_text, aq.claim_source(_citation_item(item), check_text) if aq.enabled() else _citation_item(item)) for item in named
            ]
            claim_concepts = _concepts(check_text)
            for evaluation, item in zip(evaluations, named):
                full_check = verify_claim_evidence(check_text, _citation_item(item))
                if full_check.get("hard_reject"):
                    evaluation.update(full_check)
                if evaluation.get("hard_reject"):
                    continue
                if aq.enabled() and aq.source_kind(item) == "preview":
                    continue
                if evaluation.get("support_status") != "insufficient":
                    continue
                evidence_text = (aq.claim_source(_citation_item(item), check_text) if aq.enabled() else _citation_item(item)).get("text") or ""
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
            has_support = any(
                item.get("support_status") in {"supported", "partially_supported"}
                for item in evaluations
            )
            if not has_support:
                # A broad package-wide claim is normally a synthesis over
                # several sources, which no single citation can support on its
                # own; the model also sometimes omits markers entirely on a
                # whole-package summary.  Judge the claim against the combined
                # coverage of the evidence it names (or, when nothing is
                # named, of the retrieved evidence it actually matches) so a
                # covered synthesis is kept instead of being discarded.
                pool = named if named else citations
                pool = [item for item in pool if not verify_claim_evidence(
                    check_text, _citation_item(item)
                ).get("hard_reject")]
                attribution = _attribute_claim(
                    check_text, claim_concepts, pool, scope
                )
                if attribution:
                    if not named:
                        named = [
                            by_index[index] for index in attribution["indexes"]
                            if index in by_index
                        ]
                        indexes = list(attribution["indexes"])
                        missing = []
                        evaluations = [
                            verify_claim_evidence(check_text, _citation_item(item))
                            for item in named
                        ]
                    evaluations.append({
                        "support_status": (
                            "supported"
                            if attribution["coverage"] >= 0.7
                            else "partially_supported"
                        ),
                        "support_score": round(
                            min(0.9, 0.6 + attribution["coverage"] / 4.0), 3
                        ),
                        "support_reason": "陈述为多来源综合，已由所引证据共同覆盖（自动归因）。",
                        "support_relation": "aggregate_attribution",
                        "matched_concepts": attribution["matched"][:8],
                        "concept_coverage": round(attribution["coverage"], 6),
                    })
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
                citation_numbers.update(_numbers(aq.numeric_evidence_text(_citation_item(item).get("text"))))
            numeric_supported = not claim_numbers or claim_numbers.issubset(citation_numbers)
            if aq.enabled() and claim_numbers and numeric_supported:
                eligible = [item for evaluation, item in zip(evaluations, named)
                            if aq.source_kind(item) != "preview" and not evaluation.get("hard_reject")]
                numeric_supported = bool(eligible) and all(
                    any(aq.numeric_relation_supported(check_text, _citation_item(item).get("text", ""), {number}) for item in eligible)
                    for number in claim_numbers
                )
                # Precise key/value facts may have very few lexical concepts.
                # Upgrade only when one complete eligible clause binds the value.
                for evaluation, item in zip(evaluations, named):
                    if not evaluation.get("hard_reject") and aq.source_kind(item) != "preview" and aq.numeric_relation_supported(check_text, _citation_item(item).get("text", ""), claim_numbers):
                        evaluation.update({"support_status": "supported", "support_score": 0.9,
                                           "support_reason": "对象、字段与数值在同一原文句段中对应。",
                                           "support_relation": "local_field_value"})
                supported = [item for item in evaluations if item.get("support_status") == "supported"]
                partial = [item for item in evaluations if item.get("support_status") == "partially_supported"]
            # Conjunctions are not a bag of numbers. Each recognized atom must
            # be proven within one eligible, in-scope source record. Never
            # splice two unrelated citations or let numeric fallback override it.
            compound_checks = []
            for item in named:
                source = _citation_item(item)
                proof = compound_support(check_text, source.get("text", ""))
                if proof is not None:
                    proof = dict(proof)
                    proof["supported"] = bool(proof["supported"]
                        and _scope_contains(scope, item)
                        and aq.source_kind(item) != "preview"
                        and not source.get("preview_only")
                        and str((source.get("coverage") or {}).get("level") or "").lower() != "preview"
                        and evidence_quality(source).get("eligible"))
                    compound_checks.append(proof)
            if compound_checks:
                numeric_supported = any(proof["supported"] for proof in compound_checks)
                supported = [proof for proof in compound_checks if proof["supported"]]
                partial = []
            if aq.enabled() and named and all(aq.source_kind(item) == "preview" for item in named):
                supported, partial = [], []
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
                failure_reason = ("引用中缺少数值：{}".format("、".join(missing_numbers))
                                  if missing_numbers else "数值与对应对象、字段、时间或否定条件不一致")
            elif aq.enabled() and named and all(aq.source_kind(item) == "preview" for item in named):
                failure_reason = "预览内容不能作为正式事实依据"
            elif status == "unsupported":
                failure_reason = "引用正文与陈述的核心含义不匹配"
            elif status == "partially_supported":
                failure_reason = "引用只支持陈述的一部分"
            else:
                failure_reason = None
            claim.update(
                {
                    "status": status,
                    # Keep the attributed indexes on the claim so the ledger and
                    # the answer card agree about what supports this statement.
                    "citation_indexes": list(indexes),
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
                        "compound_checks": compound_checks,
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
        inspected_scope_files = batch_summary.get("inspected_files")
        if inspected_scope_files is None:
            inspected_scope_files = coverage.get("inspected_files")
        if inspected_scope_files is None:
            inspected_scope_files = coverage.get("retrieved_files")
        inspected_scope_files = int(inspected_scope_files or 0)
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
        inspected_files = inspected_scope_files
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
            "scope_inspection_coverage": (
                inspected_scope_files / float(scope_files)
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
        # Repair the answer that can survive the final guard, not a draft whose
        # rejected statements only appear to cover the requested question.
        retained_blocks = _readable_guard_blocks(verified_claims)[0] if aq.enabled() else []
        missing_aspects = aq.missing_answer_aspects(
            "\n\n".join(retained_blocks),
            (turn_result.get("input_manifest") or {}).get("answer_contract") or {},
        ) if aq.enabled() else []
        quality_metrics["possibly_missing_answer_aspects"] = missing_aspects
        if missing_aspects:
            warnings.append("请检查是否漏答以下明确问题要点：{}。".format("、".join(missing_aspects)))
            if status == "verified":
                status = "partial"
                quality_metrics["verification_status"] = status
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
            # Only evidence that a claim actually failed justifies another
            # generation round.  Partial support and an incomplete scope are
            # caveats the UI already reports; treating them as "needs
            # revision" rewrote good package-wide summaries into a single
            # "现有证据不足" sentence.
            "needs_revision": bool(
                status not in {"verified", "not_required"}
                and (
                    unsupported_count
                    or invalid_labels
                    or numeric_failures
                    or out_of_scope
                    or missing_aspects
                )
            ),
            "scope_checked": True,
            "citation_labels_checked": True,
            "citation_entailment_checked": True,
            "numeric_values_checked": True,
            "counter_evidence_checked": counter_search_performed,
            "warnings": warnings,
            "missing_answer_aspects": missing_aspects,
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
        partial = 0
        if aq.enabled():
            kept, omitted, partial = _readable_guard_blocks(claims)
        for claim in ([] if aq.enabled() else claims):
            status = claim.get("status")
            text = _strip_list_marker(str(claim.get("text") or "").strip())
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
        kept_draft = False
        if kept:
            # Rebuild the list after filtering so removed claims never leave
            # broken numbering or orphaned sections in the final answer.
            answer = "\n\n".join(kept) if aq.enabled() else "\n".join(
                "{}. {}".format(index, item) for index, item in enumerate(kept, 1)
            )
        elif not aq.enabled() and _draft_cites_known_evidence(result):
            # The verifier could not confirm a single claim, but the draft does
            # cite evidence that exists.  Replacing the whole answer with a bare
            # "insufficient evidence" line discards everything the reader asked
            # for -- and a package-wide synthesis is exactly the kind of
            # sentence that is hardest to confirm claim by claim.  Keep the
            # draft and say plainly that it did not pass line-by-line review.
            answer = str(result.get("answer") or "").strip()
            kept_draft = True
            result["warnings"] = list(result.get("warnings") or []) + [
                "本回答未能逐条通过原文核验，请对照引用自行复核。"
            ]
        else:
            # No surviving claim and nothing cited: the answer is unrelated to
            # the retrieved evidence, so it is withheld entirely.
            answer = "现有证据不足，暂时无法形成可核验的确定结论。"
        if aq.enabled() and kept:
            notes = []
            if partial:
                notes.append("以上概括中，部分细节的依据还不充分，需结合原文进一步确认。")
            if omitted:
                notes.append("资料暂时无法支持的内容没有纳入这份回答。")
            if notes:
                answer += "\n\n" + "".join(notes)
        elif omitted and not aq.enabled():
            answer += (
                "\n\n核验说明\n- {} 条陈述因证据不足或仅得到部分支持，"
                "未作为确定结论输出。"
            ).format(omitted)
        result["answer"] = answer
        result["evidence_status"] = (
            "unverified" if kept_draft
            else ("supported" if kept and not omitted and not partial else "partial")
        )
        result["status"] = "answered" if (kept or kept_draft) else "insufficient_evidence"
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
