import json

from services.ollama import LocalModelError
from services.evidence import attach_claim_evidence, evidence_quality, evidence_support


def _fallback(context, node_path, errors=None):
    top_types = list(context.get("type_counts", {}).items())[:6]

    notable = [
        "{}：{} 个".format(ext, count)
        for ext, count in top_types
    ]

    warnings = list(errors or [])

    if context.get("sample_truncated"):
        warnings.append(
            "深度摘要采用均匀抽样：实际 {} 个文件，抽取 {} 个。".format(
                context["total_files"],
                context["sampled_files"],
            )
        )

    summary = {
        "title": "{} 文件夹概览".format(node_path),

        "summary": (
            "该文件夹递归包含 {} 个文件、{} 个子目录，总大小 {}。"
            "主要文件类型为 {}。"
        ).format(
            context["total_files"],
            context["total_dirs"],
            context["total_size_human"],
            "、".join(notable) or "暂无",
        ),

        "topics": [
            ext
            for ext, _ in top_types
        ],

        "notable_items": notable,

        "limitations": warnings,
        "coverage": context.get("coverage") or {},

        "recommended_research_direction": {
            "title": "围绕主要文件类型开展分层抽样研究",

            "rationale": (
                "先按文件类型和目录层级确定代表性资料，"
                "再逐篇进行全文摘要，可降低大文件夹分析遗漏。"
            ),

            "questions": [
                "各类型资料的核心主题是否一致？",
                "哪些子目录或文档应优先全文分析？",
            ],
        },
    }
    return _normalize_question_answer_evidence(summary, [], node_path, context)


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
                valid.append(item)
        for item in valid:
            key = str(item.get("evidence_id"))
            if key not in seen:
                seen.add(key)
                all_evidence.append(item)
        if statement:
            claims.append({
                "statement": statement,
                "type": raw.get("type") if isinstance(raw, dict) else "observation",
                "evidence_ids": [item.get("evidence_id") for item in valid if item.get("evidence_id")],
                "support_status": "supported" if valid else "insufficient",
            })
    if not claims and answer:
        claims.append({
            "statement": answer,
            "type": "summary",
            "evidence_ids": [item.get("evidence_id") for item in all_evidence if item.get("evidence_id")],
            "support_status": "supported" if all_evidence else "insufficient",
        })
    supported = any(item.get("support_status") == "supported" for item in claims)
    summary["question"] = question
    summary["value"] = value
    summary["answer"] = answer
    summary["claims"] = claims
    summary["evidence"] = all_evidence or [item for item in summary.get("evidence") or [] if evidence_quality(item).get("eligible")]
    summary["evidence_ids"] = [item.get("evidence_id") for item in summary["evidence"] if item.get("evidence_id")]
    summary["evidence_status"] = "supported" if supported else "insufficient"
    summary["evidence_contract"] = "question-answer-evidence/2.0"
    summary["question_answer_evidence"] = {
        "question": question,
        "value": value,
        "answer": answer,
        "claims": claims,
        "evidence": summary["evidence"],
        "coverage": context.get("coverage") or {},
        "limitations": list(summary.get("limitations") or []) + ([] if supported else ["当前没有足够的有效正文证据支撑该回答。"]),
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
    )


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
                and source_path not in member_paths
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

            item = {
                "evidence_id": evidence_id,

                "source_path": source_path,

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

            catalog.append(
                item
            )

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
                        and source_path not in member_paths
                    ):
                        continue

                    seen.add(
                        evidence_id
                    )

                    item = {
                        "evidence_id": evidence_id,

                        "source_path": source_path,

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
        if item.get("evidence_id")
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

    summary["evidence"] = (
        evidence
        or catalog[:8]
    )

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

        direction[
            "evidence_chain"
        ] = [
            by_id[str(item)]
            for item in ids
            if str(item) in by_id
        ]

        if not direction[
            "evidence_chain"
        ]:
            direction[
                "evidence_chain"
            ] = summary[
                "evidence"
            ][:5]

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
    prompt = """请对分析节点“{path}”做概览级深度分析。这里不是逐篇精读，而是根据主题簇和高信息密度证据判断这个节点大致包含什么、有哪些关键发现、哪些方向值得继续下钻。

真实统计：文件 {files} 个，子目录 {dirs} 个，总大小 {size}，类型 {types}。
主题簇：{clusters}
证据目录：{evidence}

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

            prompt,

            max_tokens=1800,

            strict=True,

            retries=0,

            timeout=180,

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
            "节点级摘要基于主题簇代表证据，不等同于逐文件全文精读。"
        )

        summary[
            "summary_mode"
        ] = "topic_cluster_evidence"

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
                _fallback(context, node_path, errors),
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
