import hashlib
import re
import json

from services.ollama import LocalModelError
from services.evidence import (
    attach_claim_evidence,
    compact_evidence,
    evidence_quality,
    evidence_support,
    verify_claim_evidence,
)


def _catalog_evidence_item(evidence, source_path=None):
    """Normalize an evidence card while retaining exact source locators."""
    item = compact_evidence(evidence, max_chars=520)
    item["source_path"] = evidence.get("source_path") or source_path
    for key in (
        "matched_terms", "supporting_quote", "support_type", "support_reason",
        "support_status", "support_score", "support_relation", "verification_contract",
        "semantic_score", "relevance_mode",
    ):
        if evidence.get(key) is not None:
            item[key] = evidence.get(key)
    return item


def _fallback(context, node_path, errors=None, catalog=None):
    top_types = list(context.get("type_counts", {}).items())[:6]
    notable = [
        "{}：{} 个".format(ext, count)
        for ext, count in top_types
    ]
    warnings = list(errors or [])
    if context.get("sample_truncated"):
        warnings.append(
            "深度摘要采用均匀抽样：实际 {} 个文件，抽取 {} 个。".format(
                context["total_files"], context["sampled_files"],
            )
        )

    claims = []
    evidence_ids = []
    topics = []
    seen = set()
    for file_summary in context.get("file_summaries") or []:
        for topic in file_summary.get("topics") or []:
            topic = str(topic).strip()
            if topic and topic not in topics:
                topics.append(topic)
        for item in file_summary.get("file_conclusions") or []:
            if isinstance(item, dict):
                statement = str(item.get("statement") or item.get("text") or "").strip()
                support_items = item.get("supports") or item.get("evidence") or []
            else:
                statement = str(item or "").strip()
                support_items = []
            if not statement or statement in seen:
                continue
            seen.add(statement)
            ids = [
                str(support.get("evidence_id"))
                for support in support_items
                if isinstance(support, dict) and support.get("evidence_id")
            ][:3]
            evidence_ids.extend(ids)
            claims.append({
                "statement": statement[:320],
                "type": (item.get("type") if isinstance(item, dict) else None) or "observation",
                "evidence_ids": ids,
            })
            if len(claims) >= 12:
                break
        if len(claims) >= 12:
            break

    answer = "；".join(item["statement"] for item in claims[:5])
    if not answer:
        answer = "当前节点暂无可直接继承的文件级结论。"
    warnings.append("节点模型调用不可用，已基于已完成的文件深度摘要进行确定性汇总。")
    return _normalize_question_answer_evidence({
        "title": "{} 文件夹概览".format(node_path),
        "summary": (
            "该节点递归包含 {} 个文件、{} 个子目录，总大小 {}。节点结论由已完成的文件深度摘要汇总，"
            "并保留每条结论对应的原文证据。"
        ).format(
            context["total_files"], context["total_dirs"], context["total_size_human"],
        ),
        "topics": topics[:12] or [ext for ext, _ in top_types],
        "notable_items": notable,
        "question": "该节点下各文件共同支持哪些结论？",
        "value": "汇总文件级深度摘要并保留原文回溯。",
        "answer": answer,
        "claims": claims,
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "limitations": warnings,
        "coverage": context.get("coverage") or {},
        "recommended_research_direction": {
            "title": "围绕文件级结论继续核验",
            "rationale": "优先复核跨文件重复出现且具备直接原文证据的结论。",
            "questions": ["哪些结论由多个独立文件共同支持？"],
        },
    }, catalog or [], node_path, context)


def _normalize_question_answer_evidence(summary, catalog, node_path, context=None):
    """Expose one stable question -> answer -> claim -> evidence contract."""
    summary = dict(summary or {})
    context = context or {}
    question = str(
        summary.get("question")
        or summary.get("analysis_question")
        or "该分析节点主要包含哪些内容，哪些方向值得继续下钻？"
    ).strip()
    value = str(
        summary.get("value")
        or summary.get("question_value")
        or "该问题用于判断当前节点是否形成值得继续分析的独立方向。"
    ).strip()
    answer = str(summary.get("answer") or summary.get("summary") or "").strip()
    preview_mode = str((context.get("analysis_level") if isinstance(context, dict) else "") or "").lower() in {"preview", "candidate"}
    by_id = {
        str(item.get("evidence_id")): item
        for item in list(catalog or []) + list(summary.get("evidence") or [])
        if isinstance(item, dict) and item.get("evidence_id") and evidence_quality(item).get("eligible")
    }
    raw_claims = summary.get("claims") or summary.get("evidence_claims") or []
    claims = []
    all_evidence = []
    seen = set()
    for raw in raw_claims:
        if isinstance(raw, dict):
            statement = str(raw.get("statement") or raw.get("claim") or raw.get("text") or "").strip()
            raw_items = raw.get("evidence") or raw.get("evidence_chain") or []
            ids = list(raw.get("evidence_ids") or [])
        else:
            statement = str(raw or "").strip()
            raw_items, ids = [], []
        evidence = []
        evidence.extend(item for item in raw_items if isinstance(item, dict))
        evidence.extend(by_id[str(item)] for item in ids if str(item) in by_id)
        valid = []
        for item in evidence:
            evidence_id = str(item.get("evidence_id") or "")
            if not evidence_id or evidence_id in {str(value.get("evidence_id")) for value in valid}:
                continue
            if evidence_quality(item).get("eligible"):
                if preview_mode:
                    item = dict(item)
                    item.update({"support_status": "candidate", "support_score": None,
                                 "support_reason": "候选阶段代表性原文，尚未完成全文核验"})
                    valid.append(item)
                elif item.get("evidence_origin") == "file_summary":
                    # The file-level verifier already checked this quote
                    # against its file conclusion.  A node claim may be a
                    # synthesis of several files, so requiring one quote to
                    # lexically contain the synthesized sentence is invalid.
                    inherited = str(item.get("support_status") or item.get("file_conclusion_status") or "supported")
                    if inherited in {"supported", "partially_supported", "candidate"}:
                        item = dict(item)
                        item["support_status"] = inherited
                        item.setdefault("support_reason", "继承文件摘要结论的已验证支撑原文")
                        valid.append(item)
                else:
                    verification = verify_claim_evidence(statement, item)
                    if verification.get("support_status") in {"supported", "partially_supported"}:
                        item = dict(item)
                        item.update(verification)
                        valid.append(item)
        for item in valid:
            key = str(item.get("evidence_id"))
            if key not in seen:
                seen.add(key)
                all_evidence.append(item)
        if statement and not valid:
            # A model may omit the evidence id for a synthesized claim. Recover
            # the closest already-verified file-summary support instead of
            # silently turning a supported file conclusion into "insufficient".
            claim_terms = set(re.findall(r"[a-z0-9][a-z0-9._-]{1,}|[\u4e00-\u9fff]{2,}", statement.lower()))
            ranked = []
            for source in by_id.values():
                if source.get("evidence_origin") != "file_summary":
                    continue
                evidence_id = str(source.get("evidence_id") or "")
                if not evidence_id or evidence_id in seen:
                    continue
                haystack = " ".join(str(source.get(key) or "") for key in ("file_conclusion", "supporting_quote", "text")).lower()
                source_terms = set(re.findall(r"[a-z0-9][a-z0-9._-]{1,}|[\u4e00-\u9fff]{2,}", haystack))
                score = len(claim_terms & source_terms)
                score += sum(2 for term in claim_terms if len(term) >= 3 and term in haystack)
                if score:
                    ranked.append((score, source))
            ranked.sort(key=lambda value: (-value[0], str(value[1].get("source_path") or "")))
            for _, source in ranked[:2]:
                inherited = str(source.get("support_status") or source.get("file_conclusion_status") or "supported")
                if inherited not in {"supported", "partially_supported", "candidate"}:
                    continue
                source = dict(source)
                source["support_status"] = inherited
                source.setdefault("support_reason", "按文件摘要结论关键词回溯原文")
                valid.append(source)
                key = str(source.get("evidence_id"))
                if key not in seen:
                    seen.add(key)
                    all_evidence.append(source)
        if statement:
            claim_status = (
                "candidate" if preview_mode and valid
                else "supported"
                if any(item.get("support_status") == "supported" for item in valid)
                else "partially_supported"
                if valid
                else "insufficient"
            )
            claims.append({
                "statement": statement,
                "type": raw.get("type") if isinstance(raw, dict) else "observation",
                "evidence_ids": [item.get("evidence_id") for item in valid if item.get("evidence_id")],
                "support_status": claim_status,
            })
    if not claims and answer:
        answer_evidence = []
        for item in by_id.values():
            if preview_mode:
                verified_item = dict(item)
                verified_item.update({"support_status": "candidate", "support_score": None,
                                      "support_reason": "候选阶段代表性原文，尚未完成全文核验"})
                answer_evidence.append(verified_item)
                continue
            verification = ({
                "support_status": str(item.get("support_status") or item.get("file_conclusion_status") or "supported"),
                "support_reason": "继承文件摘要结论的已验证支撑原文",
            } if item.get("evidence_origin") == "file_summary" else verify_claim_evidence(answer, item))
            if verification.get("support_status") in {"supported", "partially_supported", "candidate"}:
                verified_item = dict(item)
                verified_item.update(verification)
                answer_evidence.append(verified_item)
        answer_evidence = answer_evidence[:6]
        all_evidence.extend(answer_evidence)
        claims.append({
            "statement": answer,
            "type": "summary",
            "evidence_ids": [item.get("evidence_id") for item in answer_evidence if item.get("evidence_id")],
            "support_status": (
                "candidate" if preview_mode and answer_evidence
                else "supported"
                if any(item.get("support_status") == "supported" for item in answer_evidence)
                else "partially_supported" if answer_evidence else "insufficient"
            ),
        })
    # Preserve one node-level coverage card per source file even when the model
    # cites only the most obvious papers in its claims.
    for source in sorted({item.get("source_path") for item in by_id.values() if item.get("source_path")}):
        if any(item.get("source_path") == source for item in all_evidence):
            continue
        for item in by_id.values():
            if item.get("source_path") == source and item.get("evidence_origin") == "file_summary":
                item = dict(item)
                item["evidence_role"] = "node_coverage"
                all_evidence.append(item)
                break

    status_counts = {
        status: sum(1 for item in claims if item.get("support_status") == status)
        for status in ("supported", "partially_supported", "candidate", "insufficient")
    }
    if preview_mode and claims:
        overall_status = "candidate"
    elif status_counts["candidate"] and not (status_counts["supported"] or status_counts["partially_supported"]):
        overall_status = "candidate"
    elif claims and status_counts["supported"] == len(claims):
        overall_status = "supported"
    elif status_counts["supported"] or status_counts["partially_supported"]:
        overall_status = "partially_supported"
    else:
        overall_status = "insufficient"
    if overall_status == "insufficient" and not preview_mode:
        answer = "证据不足，当前不能形成可靠回答。"
        all_evidence = []
    summary["question"] = question
    summary["value"] = value
    summary["answer"] = answer
    summary["claims"] = claims
    summary["evidence"] = all_evidence
    summary["evidence_ids"] = [item.get("evidence_id") for item in summary["evidence"] if item.get("evidence_id")]
    summary["evidence_status"] = overall_status
    summary["evidence_contract"] = "question-answer-evidence/3.0"
    summary["claim_status_counts"] = status_counts
    summary["unique_evidence_count"] = len({item.get("evidence_id") for item in all_evidence if item.get("evidence_id")})
    summary["independent_source_count"] = len({
        item.get("source_sha256") or item.get("archive_source_path") or item.get("source_path")
        for item in all_evidence
        if item.get("source_sha256") or item.get("archive_source_path") or item.get("source_path")
    })
    summary["question_answer_evidence"] = {
        "contract": "question-answer-evidence/3.0",
        "evidence_status": overall_status,
        "analysis_level": "preview" if preview_mode else str(summary.get("analysis_level") or "deep"),
        "verification_status": "candidate" if preview_mode else str(summary.get("verification_status") or "verified"),
        "question": question,
        "value": value,
        "answer": answer,
        "claims": claims,
        "evidence": summary["evidence"],
        "coverage": context.get("coverage") or {},
          "limitations": list(summary.get("limitations") or []) + (
              []
              if overall_status == "supported"
              else ["部分结论只有间接证据支撑，建议人工复核。"]
              if overall_status == "partially_supported"
              else ["当前没有足够的有效正文证据支撑该回答。"]
          ),
    }
    summary["limitations"] = list(dict.fromkeys(summary["question_answer_evidence"]["limitations"]))
    return summary


def _path_in_folder(path, folder):
    if folder == ".":
        return True

    prefix = folder.rstrip("/") + "/"

    return (
        path == folder
        or path.startswith(prefix)
        or path.startswith(folder + "::")
    )


def _evidence_in_member_scope(evidence, member_paths):
    source_path = str(evidence.get("source_path") or "")
    archive_source = str(evidence.get("archive_source_path") or "")
    return any(
        source_path == member
        or source_path.startswith(member + "::")
        or archive_source == member
        for member in member_paths
    )


def _file_summary_evidence(context, max_items):
    """Promote persisted file-summary supports into node-level evidence.

    A node is an aggregation of file analyses.  Its evidence therefore starts
    with the supports already validated while producing each file summary;
    topic-cluster snippets are only a fallback or supplement.
    """
    per_source = {}
    seen = set()
    for file_summary in context.get("file_summaries") or []:
        path = str(file_summary.get("path") or "")
        for conclusion in file_summary.get("file_conclusions") or []:
            if not isinstance(conclusion, dict):
                continue
            statement = str(conclusion.get("statement") or "").strip()
            conclusion_status = str(conclusion.get("support_status") or conclusion.get("verification_status") or conclusion.get("status") or "unknown").lower()
            conclusion_status = {
                "verified": "supported",
                "supported": "supported",
                "partially_verified": "partially_supported",
                "inferred": "partially_supported",
                "candidate": "candidate",
            }.get(conclusion_status, conclusion_status)
            for support in conclusion.get("supports") or []:
                if not isinstance(support, dict):
                    continue
                text = " ".join(str(support.get("text") or support.get("supporting_quote") or "").split())
                if len(text) < 6:
                    continue
                evidence_id = str(support.get("evidence_id") or "").strip()
                if not evidence_id:
                    digest = hashlib.sha1((path + "\0" + statement + "\0" + text).encode("utf-8", "replace")).hexdigest()[:16]
                    evidence_id = "FS-{}".format(digest)
                if evidence_id in seen:
                    continue
                seen.add(evidence_id)
                item = dict(support)
                item.update({
                    "evidence_id": evidence_id,
                    "source_path": support.get("source_path") or path,
                    "text": text[:520],
                    "supporting_quote": str(support.get("supporting_quote") or text)[:520],
                    "evidence_origin": "file_summary",
                    "file_conclusion": statement,
                    "file_conclusion_status": conclusion_status,
                    "support_status": str(support.get("support_status") or conclusion_status or "supported"),
                    "support_reason": str(support.get("support_reason") or "文件摘要结论对应的支撑原文"),
                })
                if evidence_quality(item).get("eligible"):
                    per_source.setdefault(str(item.get("source_path") or path), []).append(item)
    primary = [per_source[source][0] for source in sorted(per_source)]
    extras = [item for source in sorted(per_source) for item in per_source[source][1:]]
    return (primary + extras)[:max_items]


def _evidence_catalog(
    context,
    node_path,
    max_clusters=8,
    max_evidence=18,
):
    """
    构造文件夹或虚拟主题节点的可追溯证据包。

    普通真实文件夹：
        根据 node_path 判断文件是否属于该目录。

    虚拟主题节点：
        根据 context["member_paths"] 判断文件是否属于该主题。
    """

    clusters = []
    catalog = []
    seen = set()

    # First preserve the evidence chain produced by file-level analysis.
    # This prevents a node-level model answer from being judged against an
    # unrelated single snippet selected from a global topic cluster.
    for item in _file_summary_evidence(context, max_evidence):
        evidence_id = item.get("evidence_id")
        if not evidence_id or evidence_id in seen:
            continue
        seen.add(evidence_id)
        item = dict(item)
        item.update(evidence_support(
            item,
            topics=[item.get("file_conclusion") or "文件摘要结论"],
            semantic_score=item.get("support_score") or 0.0,
            relevance_mode="file-summary-support",
        ))
        item["evidence_origin"] = "file_summary"
        item["file_conclusion"] = item.get("file_conclusion") or ""
        catalog.append(item)
        if len(catalog) >= max_evidence:
            break

    documents = {
        item.get("path"): item
        for item in context.get("documents", [])
    }

    # =========================================================
    # 新增：
    # 如果 context 中存在 member_paths，
    # 说明当前分析对象不是磁盘上的真实文件夹，
    # 而是“内容主题”等虚拟分析节点。
    # =========================================================
    member_paths = set(
        context.get("member_paths") or []
    )

    virtual_scope = bool(member_paths)

    for cluster in context.get(
        "topic_clusters",
        []
    ):

        # =====================================================
        # 判断这个主题簇有哪些文件属于当前分析节点
        # =====================================================
        if virtual_scope:

            members = [
                path
                for path in cluster.get(
                    "members",
                    []
                )
                if path in member_paths
            ]

        else:

            members = [
                path
                for path in cluster.get(
                    "members",
                    []
                )
                if _path_in_folder(
                    path,
                    node_path
                )
            ]

        if not members:
            continue

        cluster_items = []

        raw_evidence = (
            list(
                cluster.get(
                    "evidence",
                    []
                )
            )
            +
            list(
                cluster.get(
                    "evidence_chain",
                    []
                )
            )
        )

        # =====================================================
        # 读取主题簇已经选择好的证据
        # =====================================================
        for evidence in raw_evidence:

            if not isinstance(
                evidence,
                dict
            ):
                continue

            source_path = evidence.get(
                "source_path"
            )

            # 虚拟主题节点必须严格限制在 member_paths 中
            if (
                virtual_scope
                and source_path
                and not _evidence_in_member_scope(evidence, member_paths)
            ):
                continue

            evidence_id = evidence.get(
                "evidence_id"
            )

            if (
                not evidence_id
                or evidence_id in seen
            ):
                continue

            seen.add(
                evidence_id
            )

            item = _catalog_evidence_item(evidence, source_path)

            # =================================================
            # 保留原有证据支持判断能力
            # =================================================
            item.update(
                evidence_support(
                    evidence,

                    topics=[
                        cluster.get(
                            "topic"
                        )
                    ],

                    semantic_score=evidence.get(
                        "semantic_score",
                        0.0
                    ),

                    relevance_mode=evidence.get(
                        "relevance_mode",
                        "lexical-fallback"
                    ),
                )
            )

            cluster_items.append(
                item
            )

            catalog.append(item)

            if len(catalog) >= max_evidence:
                break

        # =====================================================
        # 如果主题簇现有证据太少，
        # 从代表文档中继续补充证据
        # =====================================================
        if len(cluster_items) < 3:

            representative_documents = [
                path
                for path in cluster.get(
                    "representative_documents",
                    []
                )
                if (
                    not virtual_scope
                    or path in member_paths
                )
            ][:3]

            for path in representative_documents:

                document = documents.get(
                    path,
                    {}
                )

                for evidence in (
                    document.get(
                        "payload",
                        {}
                    ).get(
                        "evidence",
                        []
                    )[:3]
                ):

                    evidence_id = evidence.get(
                        "evidence_id"
                    )

                    if (
                        not evidence_id
                        or evidence_id in seen
                    ):
                        continue

                    source_path = evidence.get(
                        "source_path",
                        path
                    )

                    if (
                        virtual_scope
                        and not _evidence_in_member_scope(evidence, member_paths)
                    ):
                        continue

                    seen.add(
                        evidence_id
                    )

                    item = _catalog_evidence_item(evidence, source_path)

                    item.update(
                        evidence_support(
                            evidence,

                            topics=[
                                cluster.get(
                                    "topic"
                                )
                            ],

                            semantic_score=evidence.get(
                                "semantic_score",
                                0.0
                            ),

                            relevance_mode=evidence.get(
                                "relevance_mode",
                                "lexical-fallback"
                            ),
                        )
                    )

                    cluster_items.append(
                        item
                    )

                    catalog.append(
                        item
                    )

                    if (
                        len(catalog)
                        >= max_evidence
                    ):
                        break

                if (
                    len(catalog)
                    >= max_evidence
                ):
                    break

        # =====================================================
        # 当前主题簇在这个节点中的代表文档
        # =====================================================
        representatives = [
            path
            for path in cluster.get(
                "representative_documents",
                []
            )
            if path in members
        ][:3]

        # 如果原代表文档碰巧不属于当前节点，
        # 就从 members 中补充
        if not representatives:
            representatives = members[:3]

        clusters.append({
            "cluster_id": cluster.get(
                "cluster_id"
            ),

            "topic": cluster.get(
                "topic"
            ),

            "file_count": len(
                members
            ),

            "representative_documents": representatives,

            "evidence_ids": [
                item["evidence_id"]
                for item in cluster_items
            ],
        })

        if (
            len(clusters) >= max_clusters
            or len(catalog) >= max_evidence
        ):
            break

    return clusters, catalog


def _attach_model_evidence(
    summary,
    catalog,
):
    by_id = {
        str(
            item.get(
                "evidence_id"
            )
        ): item
        for item in catalog
        if item.get("evidence_id") and evidence_quality(item).get("eligible")
    }

    requested = (
        summary.pop(
            "evidence_ids",
            []
        )
        if isinstance(
            summary,
            dict
        )
        else []
    )

    evidence = [
        by_id[str(item)]
        for item in requested
        if str(item) in by_id
    ]

    summary["evidence"] = evidence

    direction = summary.get(
        "recommended_research_direction"
    )

    if isinstance(
        direction,
        dict
    ):

        ids = direction.pop(
            "evidence_ids",
            []
        )

        direction_claim = str(
            direction.get("rationale")
            or direction.get("title")
            or ""
        ).strip()
        direction_evidence = []
        for evidence_id in ids:
            source = by_id.get(str(evidence_id))
            if not source:
                continue
            verification = verify_claim_evidence(direction_claim, source)
            if verification.get("support_status") != "supported":
                continue
            source = dict(source)
            source.update(verification)
            direction_evidence.append(source)
        direction["evidence_chain"] = direction_evidence

        direction["evidence_status"] = (
            "supported" if direction["evidence_chain"] else "insufficient"
        )
        if not direction["evidence_chain"]:
            direction["priority"] = "低"
            direction["confidence"] = "低"
            direction.setdefault("limitations", []).append(
                "模型未引用当前节点中的合法正文证据，该方向不能作为正式推荐。"
            )

    return summary


def analyze_folder(
    llm,
    context,
    node_path,
):
    clusters, catalog = _evidence_catalog(
        context,
        node_path
    )

    # =========================================================
    # 如果没有形成稳定主题簇，
    # 至少从当前范围中的文件抽取部分证据。
    # =========================================================
    if not clusters:

        for document in context.get(
            "documents",
            []
        )[:8]:

            payload = document.get(
                "payload",
                {}
            )

            for evidence in payload.get(
                "evidence",
                []
            )[:2]:

                if not evidence.get(
                    "evidence_id"
                ):
                    continue

                item = {
                    "evidence_id": evidence[
                        "evidence_id"
                    ],

                    "source_path": evidence.get(
                        "source_path",
                        document.get(
                            "path"
                        )
                    ),

                    "page": evidence.get(
                        "page"
                    ),

                    "section": evidence.get(
                        "section"
                    ),

                    "text": " ".join(
                        str(
                            evidence.get(
                                "text"
                            )
                            or ""
                        ).split()
                    )[:900],

                    "matched_terms": evidence.get(
                        "matched_terms",
                        []
                    ),

                    "supporting_quote": evidence.get(
                        "supporting_quote"
                    ),

                    "support_type": evidence.get(
                        "support_type"
                    ),

                    "support_reason": evidence.get(
                        "support_reason"
                    ),
                }

                item.update(
                    evidence_support(
                        evidence,
                        topics=[
                            "文件夹抽样证据"
                        ]
                    )
                )

                catalog.append(
                    item
                )

        clusters = [{
            "cluster_id": "SAMPLE",

            "topic": "文件夹抽样证据",

            "file_count": context.get(
                "total_files",
                0
            ),

            "evidence_ids": [
                item["evidence_id"]
                for item in catalog
            ],
        }]

    # =========================================================
    # 这里继续复用原来的模型摘要逻辑。
    #
    # node_path 对真实文件夹来说是路径；
    # 对虚拟节点来说，可以传主题名称。
    # =========================================================
    file_summaries = context.get("file_summaries") or []
    prompt = """请对分析节点“{path}”做正式节点摘要。节点摘要必须先综合该节点下每个文件已经生成的文件摘要，再用证据目录校验关键结论；不能只根据文件名、文件类型或少量片段猜测。

真实统计：文件 {files} 个，子目录 {dirs} 个，总大小 {size}，类型 {types}。
文件摘要汇总（每个文件一条，必须逐条阅读并综合）：{file_summaries}
文件摘要覆盖：{summary_coverage}
主题簇补充信息：{clusters}
证据目录（用于原文支撑校验）：{evidence}

只输出 JSON：
{{"title":"标题","question":"本节点要回答的问题","value":"为什么值得分析","answer":"基于证据的谨慎回答","summary":"300字以内的概览摘要","topics":["主题"],"notable_items":["发现"],"claims":[{{"statement":"可核验结论","type":"direct_fact或inference","evidence_ids":["E-..."]}}],"evidence_ids":["E-..."],"limitations":["局限"],"recommended_research_direction":{{"title":"首选深入方向","rationale":"理由","questions":["可验证问题"],"methods":["建议方法"],"evidence_ids":["E-..."]}}}}

要求：
1. 只能使用证据目录中的 evidence_id。
2. 不能把抽样说成全文。
3. 不能编造外部事实。
4. 发现与推论必须分开。
5. 摘要重点说明这个节点实际包含的内容，而不是只罗列文件类型。
6. claims 中的每个结论必须引用 evidence_ids；没有有效证据时标记证据不足，不得编造。
""".format(
        path=node_path,

        files=context[
            "total_files"
        ],

        dirs=context[
            "total_dirs"
        ],

        size=context[
            "total_size_human"
        ],

        types=json.dumps(
            context[
                "type_counts"
            ],
            ensure_ascii=False
        ),

        clusters=json.dumps(
            clusters,
            ensure_ascii=False
        ),

        file_summaries=json.dumps(file_summaries, ensure_ascii=False),
        summary_coverage=json.dumps({
            "generated_file_summaries": context.get("file_summary_count", len(file_summaries)),
            "expected_files": context.get("total_files", 0),
            "complete": bool(context.get("file_summaries_complete", False)),
            "generated_file_summaries": context.get("file_summary_count", len(file_summaries)),
            "missing_paths": context.get("missing_file_summaries") or [],
            "non_deep_paths": context.get("non_deep_file_summaries") or [],
        }, ensure_ascii=False),
        evidence=json.dumps(
            catalog,
            ensure_ascii=False
        ),
    )

    try:

        result = llm.chat_json(
            (
                "你是严谨的数据包节点概览分析助手。"
                "必须基于给定证据提炼主题、关键发现、"
                "异常和可验证的深入方向。"
            ),

            prompt + (
                "\nOUTPUT_CONTRACT: return the smallest valid JSON; each array may have at most one item; "
                "each free-text value must stay below 80 Chinese characters; do not repeat evidence."
            ),

            # The evidence-backed object can exceed the generic structured cap.
            # Permit enough output room for a complete vLLM JSON object.
            max_tokens=650,
            long_output=True,

            strict=True,

            retries=0,

            timeout=120,

            required_fields=("summary",),

            output_context="目录节点摘要",
        )

        summary = _attach_model_evidence(
            result["json"],
            catalog
        )

        # =====================================================
        # 保留原来的 claim -> evidence 绑定能力
        # =====================================================
        attach_claim_evidence(
            summary,
            catalog,
            fields=(
                "notable_items",
            ),
            max_items=3,
        )
        summary = _normalize_question_answer_evidence(summary, catalog, node_path, context)

        summary.setdefault(
            "limitations",
            []
        )

        summary[
            "limitations"
        ].append(
            "节点级摘要已综合节点下的文件摘要，并使用证据目录进行原文支撑校验。"
        )

        summary[
            "summary_mode"
        ] = "file_summary_evidence" if file_summaries else "topic_cluster_evidence"
        summary["file_summary_count"] = context.get("file_summary_count", len(file_summaries))
        summary["file_summaries_complete"] = bool(context.get("file_summaries_complete", False))
        summary["deep_file_summaries_complete"] = bool(context.get("deep_file_summaries_complete", False))

        return (
            summary,
            result,
            []
        )

    except (
        LocalModelError,
        ValueError,
        KeyError,
    ) as exc:

        errors = [
            "主题簇概览模型调用失败：{}".format(
                exc
            )
        ]

        return (
            _normalize_question_answer_evidence(
                _fallback(context, node_path, errors, catalog),
                catalog,
                node_path,
                context,
            ),

            {
                "model": None,
                "usage": {}
            },

            errors,
        )
