import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from services.evidence import (
    claim_semantic_scores,
    evidence_quality,
    select_evidence,
    verify_claim_evidence,
    verify_inference_evidence,
)


DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".md", ".txt", ".png", ".jpg", ".jpeg"}
DATA_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".json", ".jsonl"}


def _valid_summaries(summaries):
    return [item for item in summaries if item.get("payload", {}).get("schema_version") in (3, 4)]


def _analysis_evidence(analysis, limit=8, categories=None):
    """Collect evidence across query and topic pools without retrieval-order bias."""
    pools = []
    for retrieval_key in ("research_retrieval", "retrieval"):
        retrieval = analysis.get(retrieval_key) or {}
        for search in retrieval.get("queries") or []:
            topic = str(search.get("query") or search.get("topic") or "").strip()
            items = [item for item in (search.get("results") or []) if isinstance(item, dict)]
            if items:
                pools.append((topic or "retrieval", items))
    for cluster in analysis.get("topic_clusters") or []:
        topic = str(cluster.get("name") or cluster.get("topic") or "").strip()
        items = [
            item for item in (cluster.get("evidence_chain") or [])
            if isinstance(item, dict)
        ]
        if items:
            pools.append((topic or "topic_cluster", items))
    root_summary = (analysis.get("node_summaries") or {}).get(".", {}) or {}
    root_items = [
        item for item in (root_summary.get("evidence_chain") or [])
        if isinstance(item, dict)
    ]
    if root_items:
        pools.append(("root_summary", root_items))

    representative_paths = {
        str(path)
        for category in (categories or [])
        for path in (category.get("representative_documents") or [])
        if str(path)
    }
    if not pools:
        return []
    pools = [
        (topic, sorted(items, key=lambda item: (
            str(item.get("source_path") or "") not in representative_paths,
            str(item.get("source_path") or ""),
        )))
        for topic, items in pools
    ]
    per_pool = [0] * len(pools)
    output, seen = [], set()
    per_source = Counter()
    target = max(0, int(limit or 0))
    while len(output) < target:
        progressed = False
        for index, (_topic, items) in enumerate(pools):
            while per_pool[index] < len(items):
                item = items[per_pool[index]]
                per_pool[index] += 1
                key = item.get("evidence_id") or (
                    item.get("source_path"), item.get("content_sha256"), item.get("text")
                )
                if key in seen:
                    continue
                source = str(item.get("source_path") or item.get("archive_source_path") or "")
                if source and per_source[source] >= 2:
                    continue
                seen.add(key)
                if source:
                    per_source[source] += 1
                output.append(item)
                progressed = True
                break
            if len(output) >= target:
                break
        if not progressed:
            break
    return output


def _balanced_report_summaries(summaries, categories, limit=6):
    """Select summaries across category representatives and distinct themes."""
    valid = _valid_summaries(summaries)
    selected = []
    selected_paths = set()
    by_path = {str(item.get("path") or ""): item for item in valid}

    def add(item):
        path = str(item.get("path") or "")
        if not path or path in selected_paths or len(selected) >= max(0, int(limit)):
            return False
        selected.append(item)
        selected_paths.add(path)
        return True

    category_rows = list(categories or [])
    category_limit = max(0, int(limit or 0))
    take = min(len(category_rows), category_limit)
    category_indices = sorted(set(
        round(index * (len(category_rows) - 1) / float(max(1, take - 1)))
        for index in range(take)
    )) if take else []
    # Categories are ordered by the existing report ranking. Sampling across
    # that order keeps smaller tail categories visible when there are many.
    for category_index in category_indices:
        category = category_rows[category_index]
        if len(selected) >= category_limit:
            break
        representatives = [
            str(path) for path in (category.get("representative_documents") or [])
            if str(path)
        ]
        chosen = next((by_path[path] for path in representatives if path in by_path and path not in selected_paths), None)
        if chosen is None:
            terms = [
                str(value).strip().casefold()
                for value in [category.get("name")] + list(category.get("topics") or [])
                if str(value or "").strip()
            ]
            matches = []
            for item in valid:
                if str(item.get("path") or "") in selected_paths:
                    continue
                payload = item.get("payload") or {}
                topic_text = " ".join(str(value) for value in (payload.get("topics") or []))
                text = " ".join([
                    str(payload.get("title") or ""),
                    str(payload.get("summary") or payload.get("core_summary") or ""),
                    topic_text,
                ]).casefold()
                score = sum(1 for term in terms if term in text)
                if score:
                    matches.append((score, item))
            if matches:
                chosen = max(matches, key=lambda pair: pair[0])[1]
        if chosen is not None:
            add(chosen)

    remaining = [item for item in valid if str(item.get("path") or "") not in selected_paths]
    while remaining and len(selected) < max(0, int(limit)):
        selected_topics = {
            str(topic).casefold()
            for item in selected
            for topic in ((item.get("payload") or {}).get("topics") or [])
            if str(topic).strip()
        }
        ranked = []
        for index, item in enumerate(remaining):
            topics = {
                str(topic).casefold()
                for topic in ((item.get("payload") or {}).get("topics") or [])
                if str(topic).strip()
            }
            novelty = len(topics - selected_topics)
            ranked.append((novelty, -index, item))
        chosen = max(ranked, key=lambda row: (row[0], row[1]))[2]
        add(chosen)
        remaining.remove(chosen)
    return selected



def _cluster_evidence(cluster, analysis, topic, limit=8):
    """Collect only claim-eligible正文 evidence belonging to one topic."""
    candidates = list(cluster.get("evidence_chain") or [])
    member_paths = set(cluster.get("members") or [])

    def in_member_scope(item):
        source_path = str(item.get("source_path") or "")
        archive_source = str(item.get("archive_source_path") or "")
        return any(
            source_path == member
            or source_path.startswith(str(member) + "::")
            or archive_source == member
            for member in member_paths
        )

    for item in analysis.get("document_index", []) or []:
        path = (item.get("source") or {}).get("path") or item.get("path")
        if path not in member_paths:
            continue
        candidates.extend(item.get("evidence") or [])
    # Persistent retrieval results are a bounded projection of the complete
    # evidence index. Compact document rows intentionally omit evidence, so the
    # recommender must also consume these scoped results or it undercounts the
    # independent sources already available elsewhere in the report.
    for retrieval_key in ("research_retrieval", "retrieval"):
        for search in (analysis.get(retrieval_key) or {}).get("queries") or []:
            candidates.extend(
                item for item in search.get("results") or []
                if isinstance(item, dict) and in_member_scope(item)
            )
    evidence_topics = [topic] + list(cluster.get("related_topics") or cluster.get("keywords") or [])[:8]
    selected = select_evidence(
        candidates,
        topics=[value for value in evidence_topics if value],
        max_items=limit,
        per_source=2,
        max_chars=620,
    )
    return [
        item for item in selected
        if evidence_quality(item).get("eligible")
        and item.get("support_status") == "supported"
    ]


def _score01(value, default=0.0):
    try:
        number = float(value)
        if number > 1:
            number /= 100.0
        return max(0.0, min(1.0, number))
    except (TypeError, ValueError):
        return default


def _direction_topic_terms(value):
    """Compact topic-name features for cross-topic distinctiveness scoring."""
    text = str(value or "").lower()
    terms = set(re.findall(r"[a-z][a-z0-9-]{2,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        for size in (2, 3, 4):
            terms.update(run[index:index + size]
                         for index in range(max(0, len(run) - size + 1)))
    return terms


def _analysis_tree_direction_clusters(analysis):
    """Project official top-level analysis nodes into recommendation candidates.

    The analysis tree is the authoritative content partition and already owns
    the evidence selected for each topic. Semantic clustering metadata alone
    does not carry that evidence, so using it first can incorrectly erase every
    recommendation even when the UI tree has a valid evidence chain.
    """
    clusters = []
    for node in (analysis.get("analysis_tree") or {}).get("children") or []:
        if node.get("kind") != "group":
            continue
        if node.get("classification_status") != "classified":
            continue
        members = list(dict.fromkeys(node.get("member_paths") or []))
        name = str(node.get("name") or "").strip()
        if not name or not members:
            continue
        clusters.append({
            "topic": name,
            "name": name,
            "members": members,
            "representative_documents": list(node.get("representative_documents") or members[:5]),
            "evidence_chain": list(node.get("evidence_chain") or []),
            "related_topics": list(node.get("related_topics") or []),
            "mean_similarity": node.get("mean_similarity"),
            "classification_confidence": node.get("classification_confidence"),
            "node_id": node.get("node_id"),
            "source": "official_analysis_tree",
        })
    return clusters


def _legacy_direction_candidates(scan, analysis, limit=5):
    """Rank explainable local directions before optional model wording.

    The model may improve prose later, but it cannot invent the candidate,
    scope, score or evidence ids produced here.
    """
    statistics = analysis.get("statistics", {})
    parsed = max(1, int(statistics.get("parsed_files") or 0))
    coverage = analysis.get("coverage") or {}
    document_index = _document_index(analysis)
    clusters = (
        _analysis_tree_direction_clusters(analysis)
        or analysis.get("semantic_topic_clusters")
        or analysis.get("research_topic_clusters")
        or analysis.get("topic_clusters")
        or []
    )
    task_relevance = _score01(
        ((analysis.get("value_judgment") or {}).get("task_relevance") or {}).get("score"),
        0.5,
    )
    topic_terms_by_index = [
        _direction_topic_terms(cluster.get("topic") or cluster.get("name"))
        for cluster in clusters
    ]
    topic_term_frequency = Counter(
        term for terms in topic_terms_by_index for term in terms
    )
    technical_terms = {
        "漏洞", "攻击", "恶意", "威胁", "利用", "检测", "响应", "溯源", "认证", "加密",
        "vulnerability", "exploit", "attack", "malware", "threat", "detection", "cve", "cvss",
    }
    anomaly_terms = {"异常", "突增", "冲突", "失效", "失败", "高危", "critical", "anomaly", "outlier"}
    directions = []
    for cluster in clusters:
        topic = str(cluster.get("topic") or cluster.get("name") or "").strip()
        members = [
            path for path in dict.fromkeys(cluster.get("members") or [])
            if path not in document_index or (
                (document_index.get(path, {}).get("classification") or {}).get("document_role")
                not in {"要求与说明材料", "派生概览材料"}
            )
        ]
        if not topic or not members:
            continue
        scoped_cluster = dict(cluster, members=members)
        evidence = _cluster_evidence(scoped_cluster, analysis, topic)
        evidence_count = len(evidence)
        if not evidence:
            # An evidence-free classification may remain visible in the
            # directory, but it is not a formal research recommendation.
            continue
        independent_sources = len({
            item.get("source_sha256") or item.get("archive_source_path") or item.get("source_path")
            for item in evidence
            if item.get("source_sha256") or item.get("archive_source_path") or item.get("source_path")
        })
        member_documents = [document_index[path] for path in members if path in document_index]
        member_text = " ".join(
            " ".join(str(value) for value in (
                list((item.get("classification") or {}).get("topic_memberships") or [])
                + list((item.get("structure") or {}).get("headings") or [])[:5]
            ))
            for item in member_documents
        ).lower()
        formats = {
            (item.get("source") or {}).get("extension")
            for item in member_documents if (item.get("source") or {}).get("extension")
        }
        modified_values = []
        for item in member_documents:
            raw = str((item.get("source") or {}).get("modified_at") or "")
            try:
                modified_values.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                continue
        newest = max(modified_values) if modified_values else None
        if newest and newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - newest).days) if newest else None

        topic_terms = _direction_topic_terms(topic)
        unique_topic_terms = sum(
            1 for term in topic_terms if topic_term_frequency.get(term, 0) == 1
        )
        topical_distinctiveness = (
            unique_topic_terms / float(len(topic_terms)) if topic_terms else 0.5
        )
        dimensions = {
            "scale": min(1.0, math.log1p(len(members)) / math.log1p(max(2, parsed))),
            "topical_distinctiveness": topical_distinctiveness,
            "concentration": _score01(
                cluster.get("mean_similarity"),
                _score01(cluster.get("classification_confidence"), min(1.0, len(members) / float(parsed))),
            ),
            "information_richness": min(1.0, evidence_count / 6.0),
            "independent_sources": min(1.0, independent_sources / 4.0),
            "recency": (max(0.0, 1.0 - age_days / 1095.0) if age_days is not None else 0.35),
            "anomaly_signal": min(1.0, sum(member_text.count(term) for term in anomaly_terms) / 6.0),
            "technical_impact": min(1.0, sum(member_text.count(term) for term in technical_terms) / 10.0),
            "reportability": min(1.0, 0.45 * min(1.0, evidence_count / 4.0) + 0.35 * min(1.0, independent_sources / 3.0) + 0.20 * min(1.0, len(formats) / 3.0)),
            "novelty": min(1.0, 0.55 * (1.0 - len(members) / float(parsed)) + 0.45 * (max(0.0, 1.0 - age_days / 1095.0) if age_days is not None else 0.35)),
            "user_relevance": task_relevance,
        }
        weights = {
            # File volume has a deliberately small, logarithmic contribution.
            # Evidence diversity, cross-topic specificity and actionable signals
            # determine the recommendation more strongly than category size.
            "scale": 0.08, "topical_distinctiveness": 0.04,
            "concentration": 0.10, "information_richness": 0.14,
            "independent_sources": 0.14, "recency": 0.07,
            "anomaly_signal": 0.08, "technical_impact": 0.11,
            "reportability": 0.11, "novelty": 0.10, "user_relevance": 0.03,
        }
        score = round(100 * sum(dimensions[key] * weight for key, weight in weights.items()), 1)
        if score >= 70 and evidence_count >= 3 and independent_sources >= 2:
            priority = "高"
        elif score >= 45:
            priority = "中"
        else:
            priority = "低"
        confidence_score = min(1.0,
            0.40 * dimensions["information_richness"]
            + 0.35 * dimensions["independent_sources"]
            + 0.15 * dimensions["topical_distinctiveness"]
            + 0.10 * _score01(coverage.get("parsed_file_ratio")))
        confidence = "高" if confidence_score >= 0.78 else ("中" if confidence_score >= 0.42 else "低")
        limitations = list(coverage.get("limitations") or [])
        if not evidence:
            limitations.append("当前主题没有达到正文证据门槛，不能据此形成可靠结论。")
        related_topics = [
            str(value).strip() for value in cluster.get("related_topics") or []
            if str(value).strip() and str(value).strip() != topic
        ][:4]
        focus = "、".join(related_topics[:2]) or "关键结论与指标"
        directions.append({
            "title": topic,
            "direction": topic,
            "type": "推论",
            "priority": priority,
            "score": score,
            "score_breakdown": {
                "document_count": len(members),
                "topic_concentration": round(dimensions["concentration"], 6),
                "evidence_count": evidence_count,
                "independent_source_count": independent_sources,
                "dimensions": {key: round(value, 6) for key, value in dimensions.items()},
                "weights": weights,
                "coverage_ratio": coverage.get("parsed_file_ratio"),
                "newest_source_modified_at": newest.isoformat() if newest else None,
                "recency_basis": "源文件修改时间，仅作时效代理，不等同于资料形成时间",
            },
            "rationale": "该方向包含 {} 个已解析成员、{} 条合格证据和 {} 个独立来源；评分降低文件数量的影响，并综合证据丰富度、跨主题独特性、时效、异常、技术影响、可成稿性与新颖性，得分 {:.1f}。".format(len(members), evidence_count, independent_sources, score),
            "basis": "优先级由十维可解释评分计算；所有正文判断均回链到证据，时效仅以源文件修改时间为代理。",
            "research_questions": [
                "围绕“{}”，不同资料对{}给出了哪些一致结论、差异和适用条件？".format(topic, focus),
                "“{}”方向的关键判断能否由多个独立来源和可复核指标共同支撑？".format(topic),
            ],
            "methods": ["先阅读代表性文档并核对原文证据", "按时间、来源和版本关系进行交叉比较"],
            "representative_documents": list(cluster.get("representative_documents") or members[:5])[:5],
            "evidence_chain": evidence,
            "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
            "evidence_status": "supported",
            "unique_evidence_count": len({item.get("evidence_id") for item in evidence if item.get("evidence_id")}),
            "independent_source_count": independent_sources,
            "candidate_source": cluster.get("source") or "legacy_cluster_projection",
            "limitations": list(dict.fromkeys(limitations)),
            "confidence": confidence,
            "confidence_score": round(confidence_score, 3),
        })
    directions.sort(key=lambda item: (-float(item.get("score") or 0), item.get("title") or ""))
    deduplicated = []
    seen_titles = set()
    for item in directions:
        key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", item.get("title", "").lower())
        if key in seen_titles:
            continue
        seen_titles.add(key)
        item["rank"] = len(deduplicated) + 1
        deduplicated.append(item)
        if len(deduplicated) >= max(1, min(5, limit)):
            break
    return deduplicated


def _direction_candidates(scan, analysis, limit=8):
    from services.research_opportunities import discover_opportunities
    clusters = (_analysis_tree_direction_clusters(analysis)
                or analysis.get("semantic_topic_clusters")
                or analysis.get("research_topic_clusters")
                or analysis.get("topic_clusters") or [])
    if not clusters:
        # A single parsed paper can support replication without a directory.
        clusters = [{"topic": (d.get("structure") or {}).get("title") or "当前资料关键判断",
                     "members": [(d.get("source") or {}).get("path") or d.get("path")],
                     "evidence_chain": d.get("evidence") or []}
                    for d in analysis.get("document_index") or []
                    if (d.get("source") or {}).get("path") or d.get("path")]
    candidates, audit = discover_opportunities(analysis, clusters, limit=limit)
    analysis["research_opportunity_audit"] = audit
    return candidates


def _research_direction(_scan, _summaries, analysis, candidates=None):
    candidates = _direction_candidates(_scan, analysis) if candidates is None else candidates
    if candidates:
        return candidates[0]
    evidence = _analysis_evidence(analysis, 8)
    coverage = analysis.get("coverage") or {}
    return {
        "title": "补充解析并核验代表性正文证据",
        "direction": "补充解析并核验代表性正文证据",
        "type": "推论",
        "priority": "低",
        "score": 0,
        "evidence_status": "insufficient",
        "quality_gate": {"status": "insufficient", "reason": "no_evidence_backed_opportunity"},
        "rationale": "当前尚未形成有足够正文证据支撑的稳定主题，不能强行推荐领域方向。",
        "basis": "先补充待分析或失败文件，再重新生成概览。",
        "research_questions": ["哪些文件可以形成稳定主题？", "当前证据是否足以回答客户指定问题？"],
        "methods": ["重试失败文件", "选择待分析节点进行分批深化"],
        "evidence_chain": evidence,
        "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
        "limitations": list(coverage.get("limitations") or []) + ["证据不足，当前方向仅作为处理建议。"],
        "confidence": "低",
    }


def candidates_have_evidence(local_report):
    """True when at least one local direction candidate carries body evidence."""
    return any(
        (item.get("evidence_chain") or item.get("evidence_ids"))
        for item in (local_report.get("direction_candidates") or [])
    )


def _aligned_evidence_catalog(analysis, local_report, categories, limit=40, per_candidate=5, candidates=8):
    """Evidence catalog aligned with the candidates the model must choose from.

    The first slots hold each top candidate's own verified evidence, so every
    candidate the model may pick has citable ids in the catalog.  Remaining
    slots are filled from the global pool for key findings.
    """
    output, seen = [], set()

    def add(item):
        evidence_id = str((item or {}).get("evidence_id") or "").strip()
        if not evidence_id or evidence_id in seen or len(output) >= limit:
            return False
        seen.add(evidence_id)
        output.append(item)
        return True

    # One slot per evidence role before extra excerpts, across all registered
    # candidates. Counterevidence must not be displaced by supporting snippets.
    rows = list(local_report.get("direction_candidates") or [])[:candidates]
    for role in ("counterevidence", "conditions", "prior_work", "resources", "support"):
        for candidate in rows:
            by_id = {str(e.get("evidence_id")): e for e in candidate.get("evidence_chain") or []}
            for identity in (candidate.get("evidence_roles") or {}).get(role) or []:
                if str(identity) in by_id:
                    add(by_id[str(identity)])
                    break
    for candidate in rows:
        taken = 0
        for item in candidate.get("evidence_chain") or []:
            if taken >= per_candidate:
                break
            if add(item):
                taken += 1
    for item in _analysis_evidence(analysis, limit, categories=categories):
        if len(output) >= limit:
            break
        add(item)
    return output


def build_report_analysis_prompt(scan, summaries, analysis, local_report, evidence_limit=8, excerpt_limit=650,
                                 aligned=False):
    """Build a bounded, traceable input for the report-planning model.

    ``aligned`` (SJFX_OVERVIEW_EVIDENCE_ALIGN) builds the catalog from the
    candidates' own evidence and asks for ``candidate_rank`` instead of a free
    title; the default keeps the previous prompt byte-for-byte.
    """
    categories = [{
        "name": item.get("name"),
        "dimension": item.get("dimension"),
        "file_count": item.get("file_count"),
        "topics": item.get("topics", [])[:8],
        "representative_documents": item.get("representative_documents", [])[:4],
    } for item in local_report.get("global_categories", [])]
    if aligned:
        return _build_aligned_report_prompt(scan, summaries, analysis, local_report, categories)

    catalog = []
    for item in _analysis_evidence(analysis, evidence_limit, categories=categories):
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not evidence_id:
            continue
        catalog.append({
            "evidence_id": evidence_id,
            "source_path": item.get("source_path"),
            "page": item.get("page"),
            "section": item.get("section"),
            "text": " ".join(str(item.get("text") or "").split())[:excerpt_limit],
        })

    summaries_context = []
    for item in _balanced_report_summaries(summaries, categories, limit=6):
        payload = item.get("payload", {})
        summaries_context.append({
            "path": item.get("path"),
            "title": payload.get("title"),
            "summary": (payload.get("summary") or payload.get("core_summary") or "")[:800],
            "topics": payload.get("topics", [])[:8],
            "key_facts": payload.get("key_facts", [])[:8],
        })

    prompt = """你正在为一个未知数据包撰写《数据包情况概览报告》的研究规划部分。请基于证据目录和本地分类做实质性分析，而不是按关键词套用固定题目。

硬约束：
1. 只能使用给定证据目录中的 evidence_id；不得编造文件、时间、结论、数字或领域事实。
2. “推荐研究方向”和“深入方向”都是推论，必须列出支撑它的 evidence_ids。证据不足时明确写出不足，不得强行给高置信度。
3. 优先发现资料之间的共同主题、分歧、版本关系、证据缺口、可验证问题及合适的研究方法。
4. 本地分类是确定性结构，不能重写或新增分类。
5. 推荐方向只能从本地评分候选中选择，候选分数和次序是参考；你可以根据证据的独立来源、跨主题独特性、影响、时效、矛盾与证据缺口重新排序，不得另造主题。
6. 文件数量只说明材料规模，不等于研究价值。综合比较主要主题与有依据的小众主题，核对跨来源和跨时间的相互印证、分歧与未解析范围；不因某类文件最多就自动优先。
7. 方向必须具体到材料中出现的研究对象与待核验问题。每条问题写明“研究哪个对象、比较什么维度、如何用原文验证”；只在证据明确提供时写机构、指标、时间区间或版本，不补造缺失信息。不要仅写“趋势分析”“进一步研究”“有何异同”。
8. 每条研究方法使用“具体动作 → 预期产物”的表达，例如按证据实际提供的字段制作来源对照表、版本差异清单或待核验问题清单。产物是研究计划，不得把尚未执行的分析写成既成发现；推荐理由说明先解决哪个具体问题及其证据基础。

输出一个合法 JSON 对象：
{{
  "key_findings":["不超过 5 条、仅陈述可由材料核验的发现"],
  "recommended_research_direction":{{
    "title":"具体且面向本数据包的首选方向",
    "rationale":"结合证据说明为何优先研究",
    "research_questions":["2-4 个可验证问题"],
    "methods":["2-4 个可执行方法"],
    "priority":"高/中/低",
    "confidence":"高/中/低",
    "evidence_ids":["E-..."]
  }},
  "directions":[{{
    "direction":"可选深入方向",
    "type":"推论",
    "confidence":"高/中/低",
    "basis":"判断依据",
    "confidence_note":"置信度说明",
    "evidence_ids":["E-..."]
  }}]
}}

扫描统计：{scan}
本地内容分类：{categories}
本地评分候选（分数是透明参考，可按证据质量重新排序）：{candidates}
已有节点摘要：{summaries}
证据目录：{evidence}""".format(
        scan=json.dumps({
            "file_count": scan.get("file_count", 0),
            "directory_count": scan.get("directory_count", 0),
            "total_size": scan.get("total_size_human"),
            "type_counts": scan.get("type_counts", {}),
            "truncated": scan.get("truncated", False),
            "read_errors": len(scan.get("errors", [])),
        }, ensure_ascii=False),
        categories=json.dumps(categories, ensure_ascii=False),
        candidates=json.dumps([{
            "rank": item.get("rank"), "title": item.get("title"),
            "score": item.get("score"), "priority": item.get("priority"),
            "confidence": item.get("confidence"), "score_breakdown": item.get("score_breakdown"),
            "evidence_ids": item.get("evidence_ids", []),
        } for item in local_report.get("direction_candidates", [])[:5]], ensure_ascii=False),
        summaries=json.dumps(summaries_context, ensure_ascii=False),
        evidence=json.dumps(catalog, ensure_ascii=False),
    )
    return prompt, catalog


def _build_aligned_report_prompt(scan, summaries, analysis, local_report, categories,
                                 evidence_limit=40, excerpt_limit=650):
    raw_catalog = _aligned_evidence_catalog(analysis, local_report, categories, limit=evidence_limit)
    catalog = []
    from services.research_elements import complete_excerpt, research_passages
    for item in raw_catalog:
        text = " ".join(str(item.get("text") or "").split())
        excerpt = complete_excerpt(text, excerpt_limit)
        if len(text) > excerpt_limit:
            units = research_passages({"evidence": [item]}, per_role=1, max_chars=excerpt_limit)
            for role in ("limitation", "condition", "result", "method"):
                if units.get(role):
                    excerpt = units[role][0]["text"]
                    break
        if not excerpt:
            continue
        # Verification uses exactly the passage supplied to the model. Full
        # local chains remain in the server recommendation for source opening.
        record = dict(item, text=excerpt)
        if record.get("translated_text"):
            record["translated_text"] = complete_excerpt(record["translated_text"], excerpt_limit)
        catalog.append(record)
    catalog_ids = {str(item.get("evidence_id")) for item in catalog}
    prompt_catalog = [{
        "evidence_id": str(item.get("evidence_id")),
        "source_path": item.get("source_path"),
        "page": item.get("page"),
        "section": item.get("section"),
        "text": str(item.get("text") or "")[:excerpt_limit],
    } for item in catalog]

    summaries_context = []
    for item in _balanced_report_summaries(summaries, categories, limit=6):
        payload = item.get("payload", {})
        summaries_context.append({
            "path": item.get("path"),
            "title": payload.get("title"),
            "summary": (payload.get("summary") or payload.get("core_summary") or "")[:600],
            "topics": payload.get("topics", [])[:8],
        })

    candidates = []
    for item in list(local_report.get("direction_candidates") or [])[:8]:
        breakdown = item.get("score_breakdown") or {}
        candidates.append({
            "candidate_rank": item.get("rank"),
            "candidate_id": item.get("candidate_id"),
            "quality_gate": item.get("quality_gate"),
            "research_questions": item.get("research_questions"),
            "known_work": item.get("known_work"),
            "research_gap": item.get("research_gap"),
            "hypothesis": item.get("hypothesis"),
            "limitations": item.get("limitations"),
            "selection_reason": item.get("selection_reason"),
            "evidence_roles": {role: [i for i in ids if str(i) in catalog_ids] for role, ids in (item.get("evidence_roles") or {}).items()},
            "title": item.get("title"),
            "score": item.get("score"),
            "priority": item.get("priority"),
            "confidence": item.get("confidence"),
            "document_count": breakdown.get("document_count"),
            "independent_source_count": item.get("independent_source_count"),
            "dimensions": breakdown.get("dimensions"),
            "evidence_ids": [value for value in (item.get("evidence_ids") or []) if str(value) in catalog_ids],
        })

    prompt = """你正在为一个未知数据包撰写《数据包情况概览报告》的研究规划部分。请基于证据目录和本地候选做实质性分析，而不是按关键词套用固定题目。

硬约束：
1. 只能使用给定证据目录中的 evidence_id；不得编造文件、时间、结论、数字或领域事实。
2. 首选方向只能从“本地评分候选”中选择，用 candidate_rank 指明是哪一个；不得改写候选标题、不得另造主题。你可以根据证据的独立来源、跨主题独特性、影响、时效、矛盾与证据缺口改变先后，不必选第 1 名。
3. 首选方向与深入方向都是推论，evidence_ids 必须取自所选候选列出的 evidence_ids；证据不足时明确写出不足，不得强行给高置信度。
4. 文件数量只说明材料规模，不等于研究价值；不因某类文件最多就自动优先。
5. 研究问题写明“研究哪个对象、比较什么维度、如何用原文验证”；只在证据明确提供时写机构、指标、时间区间或版本。不要仅写“趋势分析”“进一步研究”。
6. 研究方法使用“具体动作 → 预期产物”的表达；产物是研究计划，不得把尚未执行的分析写成既成发现。
7. key_findings 只写原文可直接核验的事实，每条必须带 1-2 个 evidence_id，数字必须与原文一致；没有把握就少写或不写。
8. 比较明确问题、已有工作、条件、反证及资源；rationale 说明与最强备选的取舍，不能仅写文件数量或分数高。
9. 方法迁移是待验证假设，反例未命中不代表不存在；当前资料未见不能写全球首次或填补领域空白。资源未确认先安排确认，不能虚构数据集、指标、算法名称或已完成实验。
10. 从全部登记候选作一次有界比较与可行性审查，所有模型意见逐项经服务端核验后采用；不要输出新的候选或提高本地置信度。
11. 五种证据角色来自每候选一次有界召回与全主题盘点的分桶，不代表五次独立检索或全量精查。比较范围仅限下列登记候选；待精查机会不能写成已经排除。
12. 全部分析措辞、问题、方法及推荐理由用中文表达；英文原始术语仅保留在证据目录。推荐理由分清已有依据、具体待解问题、研究价值、与最强备选的取舍及资源限制。写明为什么先做这个、先查什么，避免只重复分数、文件数或原文长引文。

输出一个合法 JSON 对象：
{{
  "key_findings":[{{"statement":"一条可由原文直接核验的发现","evidence_ids":["E-..."]}}],
  "recommended_research_direction":{{
    "candidate_rank":1,
    "rationale":"结合证据说明为何优先研究该候选",
    "research_questions":["2-4 个可验证问题"],
    "methods":["2-4 个可执行方法"],
    "priority":"高/中/低",
    "confidence":"高/中/低",
    "evidence_ids":["E-..."]
  }},
  "directions":[{{
    "candidate_rank":2,
    "direction":"可选深入方向（基于该候选的具体切入点）",
    "confidence":"高/中/低",
    "basis":"判断依据",
    "confidence_note":"置信度说明",
    "evidence_ids":["E-..."]
  }}]
}}
key_findings 不超过 3 条，directions 不超过 3 条。问题与方法引用候选的原文对象及条件。

扫描统计：{scan}
本地内容分类：{categories}
本地评分候选：{candidates}
机会盘点与精查范围：{research_audit}
已有节点摘要：{summaries}
证据目录：{evidence}""".format(
        scan=json.dumps({
            "file_count": scan.get("file_count", 0),
            "directory_count": scan.get("directory_count", 0),
            "total_size": scan.get("total_size_human"),
            "type_counts": scan.get("type_counts", {}),
            "truncated": scan.get("truncated", False),
            "read_errors": len(scan.get("errors", [])),
        }, ensure_ascii=False),
        categories=json.dumps(categories, ensure_ascii=False),
        candidates=json.dumps(candidates, ensure_ascii=False),
        research_audit=json.dumps({key: (local_report.get("research_opportunity_audit") or {}).get(key) for key in ("discovered_count", "refined_count", "refinement_budget", "refinement_complete")}, ensure_ascii=False),
        summaries=json.dumps(summaries_context, ensure_ascii=False),
        evidence=json.dumps(prompt_catalog, ensure_ascii=False),
    )
    return prompt, catalog


def _time_range(analysis):
    values = []
    for item in analysis.get("document_index", []):
        modified = item.get("source", {}).get("modified_at")
        if modified:
            values.append(modified[:10])
    if not values:
        return "未能从文件元数据推断"
    return "{} 至 {}（文件修改时间，不等同于资料形成时间）".format(min(values), max(values))


def _walk_analysis_files(node):
    if node.get("kind") == "file":
        path = node.get("path")
        if path:
            yield node
        return
    for child in node.get("children", []):
        yield from _walk_analysis_files(child)


def _distinct(values, limit=None):
    output = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
        if limit is not None and len(output) >= limit:
            break
    return output


def _ranked_leaf_topics(leaves, limit=12):
    counts = Counter()
    for leaf in leaves:
        counts.update(_distinct(leaf.get("content_topics", [])))
    return [topic for topic, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _document_index(analysis):
    return {
        item.get("source", {}).get("path"): item
        for item in analysis.get("document_index", [])
        if item.get("source", {}).get("path")
    }


def _scan_file_index(node):
    """Return raw inventory metadata for every scanned file.

    The analysis index intentionally contains parsed files only. Reports need
    the scan inventory as well, otherwise an unparseable PDF silently vanishes
    from category sizes and extension counts.
    """
    files = {}

    def walk(current):
        if current.get("kind") == "file":
            path = current.get("path")
            if path:
                files[path] = current
            return
        for child in current.get("children", []):
            walk(child)

    walk(node or {})
    return files


def _matching_evidence(analysis, paths, limit=4):
    """Return auditable evidence that belongs to the exact category members."""
    path_set = set(paths)
    output = []
    seen = set()

    def add(items):
        for item in items or []:
            if not isinstance(item, dict) or item.get("source_path") not in path_set:
                continue
            key = item.get("evidence_id") or (item.get("source_path"), item.get("content_sha256"), item.get("text"))
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
            if len(output) >= limit:
                return True
        return False

    for cluster in analysis.get("topic_clusters", []):
        if add(cluster.get("evidence_chain")):
            return output
    for retrieval_key in ("research_retrieval", "retrieval"):
        for query in analysis.get(retrieval_key, {}).get("queries", []):
            if add(query.get("results")):
                return output
    return output


def _representative_documents(analysis, paths, limit=5):
    path_set = set(paths)
    candidates = []
    for cluster in analysis.get("topic_clusters", []):
        members = set(cluster.get("members", []))
        if not members.intersection(path_set):
            continue
        candidates.extend(cluster.get("representative_documents", []))
    return _distinct([path for path in candidates if path in path_set] + sorted(path_set), limit)


def _category_payload(node, analysis, document_index, scan_files):
    leaves = list(_walk_analysis_files(node))
    paths = _distinct(leaf.get("path") for leaf in leaves)
    documents = [document_index[path] for path in paths if path in document_index]
    total_size = sum(int((scan_files.get(path) or {}).get("size") or item.get("source", {}).get("size") or 0)
                     for path, item in ((path, document_index.get(path, {})) for path in paths))
    type_counts = Counter(
        (scan_files.get(path) or {}).get("extension")
        or document_index.get(path, {}).get("source", {}).get("extension")
        or "[无扩展名]"
        for path in paths
    )
    topics = _ranked_leaf_topics(leaves)
    subcategories = []
    for child in node.get("children", []):
        if child.get("kind") != "group":
            continue
        child_leaves = list(_walk_analysis_files(child))
        child_paths = _distinct(leaf.get("path") for leaf in child_leaves)
        child_topics = _ranked_leaf_topics(child_leaves, limit=10)
        subcategories.append({
            "name": child.get("name", "未命名主题"),
            "dimension": child.get("dimension", "内容主题"),
            "file_count": len(child_paths),
            "description": child.get("summary") or "该主题下的文件已完成统一解析。",
            "topics": child_topics,
            "representative_documents": _representative_documents(analysis, child_paths, limit=3),
            "evidence_chain": _matching_evidence(analysis, child_paths, limit=2),
            "conclusion_evidence": child.get("conclusion_evidence", []),
        })

    parsed_count = len(documents)
    return {
        "name": node.get("name", "未命名分类"),
        "dimension": node.get("dimension", "内容类别"),
        "classification_status": node.get(
            "classification_status",
            "classified",
        ),
        "file_count": len(paths),
        "member_paths": paths,
        "parsed_file_count": parsed_count,
        "unparsed_file_count": len(paths) - parsed_count,
        "total_size": total_size,
        "type_counts": dict(sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))),
        "description": node.get("summary") or "该内容类别下的文件已完成统一解析。",
        "topics": topics,
        "subcategories": subcategories,
        "representative_documents": _representative_documents(analysis, paths),
        "evidence_chain": _matching_evidence(analysis, paths),
        "conclusion_evidence": node.get("conclusion_evidence", []),
    }


def _unparsed_category(scan_files, document_index):
    paths = sorted(set(scan_files) - set(document_index))
    if not paths:
        return None
    type_counts = Counter(
        (scan_files[path].get("extension") or "[无扩展名]") for path in paths
    )
    return {
        "name": "未解析文件（待复核）",
        "dimension": "解析状态",
        "classification_status": "unclassified",
        "file_count": len(paths),
        "parsed_file_count": 0,
        "unparsed_file_count": len(paths),
        "total_size": sum(int(scan_files[path].get("size") or 0) for path in paths),
        "type_counts": dict(sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))),
        "description": "这些文件已被扫描到，但正文解析未完成，未参与主题聚类或模型结论；请查看解析失败清单后重试或人工复核。",
        "topics": [],
        "subcategories": [],
        "representative_documents": paths[:5],
        "evidence_chain": [],
    }


def _adaptive_categories(scan, analysis):
    tree = analysis.get("analysis_tree") or {}
    children = tree.get("children", [])
    if not children:
        return []
    documents = _document_index(analysis)
    scan_files = _scan_file_index(scan.get("tree", {}))
    categories = [
        _category_payload(node, analysis, documents, scan_files)
        for node in children if node.get("kind") == "group"
    ]
    unparsed = _unparsed_category(scan_files, documents)
    if unparsed:
        categories.append(unparsed)
    return [category for category in categories if category["file_count"]]


def _physical_directory_categories(scan, analysis):
    """Return one bounded inventory category when adaptive analysis failed.

    Creating one category per physical child turns a partial failure into a
    10,000-item report. A single explicit root category remains useful and
    makes the need to rerun analysis unambiguous.
    """
    root = scan.get("tree", {})
    summary = analysis.get("node_summaries", {}).get(".", {})
    return [{
        "name": "数据包根节点（分类未完成）",
        "dimension": "扫描清单（降级）",
        "classification_status": "unclassified",
        "file_count": scan.get("file_count", root.get("file_count", 0)),
        "parsed_file_count": len(_document_index(analysis)),
        "unparsed_file_count": max(0, scan.get("file_count", 0) - len(_document_index(analysis))),
        "total_size": scan.get("total_size", root.get("total_size", 0)),
        "type_counts": scan.get("type_counts", root.get("type_counts", {})),
        "description": "自适应内容分类未完成。本项仅保留扫描清单，未按物理目录或单个文件展开；请修复解析告警后重新分析。",
        "topics": summary.get("topics", []),
        "subcategories": [],
        "representative_documents": summary.get("representative_documents", [])[:5],
        "evidence_chain": summary.get("evidence_chain", [])[:5],
    }]


def _global_categories(scan, analysis):
    categories = _adaptive_categories(scan, analysis)
    if categories:
        return categories, "adaptive_analysis_tree"
    return _physical_directory_categories(scan, analysis), "root_inventory_fallback"


KEY_FINDINGS_PLACEHOLDER = "尚未形成已核验的内容发现：文件与节点摘要中还没有通过原文核验的结论，完成初步或深度摘要后会在此列出。"


def is_fallback_summary(payload):
    """Local fallback summaries (raw text preview) count for coverage only, never as content evidence."""
    return "fallback" in str((payload or {}).get("generated_by") or "").lower()


def content_key_findings(summaries, limit=5):
    """Content findings from already-verified file claims and node summary claims.

    File claims are used only when ``build_file_claims`` marked them verified.
    Node claims marked supported are used as-is; a node claim still in
    ``candidate`` state is verified here against the node's own evidence.
    """
    file_rows, node_rows = [], []
    for row in summaries or []:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") or {}
        if not isinstance(payload, dict) or is_fallback_summary(payload):
            continue
        path = str(row.get("path") or payload.get("node_path") or "")
        for claim in payload.get("file_conclusions") or []:
            if not isinstance(claim, dict) or claim.get("status") != "verified":
                continue
            text = " ".join(str(claim.get("text") or "").split())
            ids = [
                str(item.get("evidence_id")) for item in (claim.get("supports") or [])
                if isinstance(item, dict) and item.get("evidence_id")
            ]
            if text and ids:
                file_rows.append((float(claim.get("confidence") or 0), path, text, list(dict.fromkeys(ids))))
        if not path.startswith("node:"):
            continue
        evidence_by_id = {
            str(item.get("evidence_id")): item
            for item in list(payload.get("evidence") or []) + list(payload.get("evidence_chain") or [])
            if isinstance(item, dict) and item.get("evidence_id")
        }
        for claim in payload.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            text = " ".join(str(claim.get("statement") or "").split())
            ids = [str(value) for value in (claim.get("evidence_ids") or []) if str(value)]
            if not text or not ids:
                continue
            if claim.get("support_status") != "supported":
                ids = [
                    value for value in ids
                    if value in evidence_by_id
                    and verify_claim_evidence(text, evidence_by_id[value]).get("support_status") == "supported"
                ]
                if not ids:
                    continue
            node_rows.append((path, str(payload.get("title") or ""), text, ids))
            break  # one finding per node keeps themes diverse
    file_rows.sort(key=lambda row: -row[0])
    files_seen = set()
    file_items = []
    for confidence, path, text, ids in file_rows:
        if path in files_seen:
            continue
        files_seen.add(path)
        file_items.append({
            "statement": text, "source_type": "file_claim", "source_path": path,
            "evidence_ids": ids, "support_status": "supported", "confidence": round(confidence, 3),
        })
    node_items = [{
        "statement": text, "source_type": "node_claim", "source_path": path,
        "node_title": title, "evidence_ids": ids, "support_status": "supported",
    } for path, title, text, ids in node_rows]
    output, seen = [], set()
    for index in range(max(len(file_items), len(node_items))):
        for pool in (node_items, file_items):
            if index >= len(pool) or len(output) >= limit:
                continue
            key = re.sub(r"\s+", "", pool[index]["statement"]).casefold()
            if key in seen:
                continue
            seen.add(key)
            output.append(pool[index])
    return output[:limit]


def _finding_text(item):
    statement = str(item.get("statement") or "").strip()
    if item.get("source_type") == "file_claim" and item.get("source_path"):
        return "{}（文件：{}）".format(statement, Path(str(item["source_path"])).name)
    if item.get("source_type") == "node_claim" and item.get("node_title"):
        return "{}（主题：{}）".format(statement, item["node_title"])
    if item.get("evidence_ids"):
        return "{}（引证 {}）".format(statement, "、".join(str(value) for value in item["evidence_ids"][:2]))
    return statement


def compose_key_findings(report, model_findings=None, limit=5):
    """Model findings that passed factual verification first, then content findings."""
    rows, seen = [], set()
    for item in list(model_findings or [])[:3] + list(report.get("content_key_findings") or []):
        key = re.sub(r"\s+", "", str(item.get("statement") or "")).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(item)
        if len(rows) >= limit:
            break
    report["key_findings_detail"] = rows
    report["key_findings"] = [_finding_text(item) for item in rows] or [KEY_FINDINGS_PLACEHOLDER]
    return report


def _direction_entry(candidate):
    return {
        "direction": candidate["title"],
        "type": "推论",
        "question": (candidate.get("research_questions") or [None])[0],
        "value": candidate.get("rationale"),
        "answer": candidate.get("rationale"),
        "confidence": candidate["confidence"],
        "priority": candidate.get("priority"),
        "score": candidate.get("score"),
        "representative_documents": candidate.get("representative_documents", []),
        "evidence_ids": candidate.get("evidence_ids", []),
        "evidence_chain": candidate["evidence_chain"],
        "basis": candidate.get("basis") or candidate["rationale"],
        "limitations": candidate.get("limitations", []),
        "confidence_note": "置信度由跨文档主题覆盖与可引用证据数量决定；建议在正式研究前复核代表文档原文。",
    }


def build_local_report(scan, summaries, analysis=None, content_findings=False):
    """``content_findings`` (SJFX_OVERVIEW_EVIDENCE_ALIGN) moves pipeline counters to
    基本情况, quality notices to limitations, and fills 关键发现 with verified content."""
    analysis = analysis or {}
    stats = analysis.get("statistics", {})
    coverage = analysis.get("coverage") or {}
    value_judgment = analysis.get("value_judgment") or {}
    type_counts = scan.get("type_counts", {})
    ordered_types = list(type_counts.items())
    root_summary = analysis.get("node_summaries", {}).get(".", {})
    categories, classification_source = _global_categories(scan, analysis)
    if not categories:
        categories = [{
            "name": "数据包根节点",
            "dimension": "未形成分类",
            "file_count": scan.get("file_count", 0),
            "parsed_file_count": 0,
            "total_size": scan.get("total_size", 0),
            "type_counts": scan.get("type_counts", {}),
            "description": root_summary.get("summary", "扫描范围内暂无可分类文件。"),
            "topics": [],
            "subcategories": [],
            "representative_documents": [],
            "evidence_chain": root_summary.get("evidence_chain", []),
        }]
        classification_source = "root_fallback"
    parsed_file_count = stats.get("parsed_files", 0)
    scanned_file_count = scan.get("file_count", 0)
    classified_paths = set()
    for item in categories:
        if item.get("classification_status") == "classified":
            classified_paths.update(item.get("member_paths") or [])
    # Older stored analyses may not expose member_paths. Their sum is still
    # bounded by the physical inventory so reports can never claim >100%.
    classified_file_count = (
        len(classified_paths)
        if classified_paths
        else min(scanned_file_count, sum(
            int(item.get("file_count") or 0)
            for item in categories
            if item.get("classification_status") == "classified"
        ))
    )
    unclassified_file_count = max(0, scanned_file_count - classified_file_count)
    classification_coverage = {
        "source": classification_source,
        "dimension_count": len(analysis.get("classification_dimensions", [])),
        "top_level_category_count": len(categories),
        "classified_file_count": classified_file_count,
        "unclassified_file_count": unclassified_file_count,
        "parsed_file_count": parsed_file_count,
        "scanned_file_count": scanned_file_count,
        "coverage_ratio": round(classified_file_count / float(scanned_file_count or 1), 6),
        "complete": classified_file_count == scanned_file_count,
    }

    findings = [
        "包内精确去重发现 {} 个重复组，除每组保留 1 个规范副本外，可合并重复文件 {} 个。".format(stats.get("exact_duplicate_groups", 0), stats.get("exact_duplicate_files", 0)),
        "高相似度检测形成 {} 个相似文档簇；内容主题聚合形成 {} 个主题簇。".format(stats.get("similar_document_clusters", 0), stats.get("topic_clusters", 0)),
        # ``failed_files`` is a subset of ``parsed_files`` (a file can be
        # registered with an incomplete body), so the wording must not read as
        # two disjoint totals.
        "统一解析登记 {} 个文件，生成 {} 条可回查证据；其中 {} 个文件正文未完整覆盖（详见《解析覆盖率清单》）。".format(stats.get("parsed_files", 0), stats.get("evidence_items", 0), stats.get("failed_files", 0)),
        "本地混合检索已建立 {} 个证据块；{} 个 Office 文件启用了内嵌图片 OCR。".format(stats.get("retrieval_evidence_chunks", 0), stats.get("office_embedded_image_ocr_files", 0)),
    ]
    if coverage.get("limitations"):
        findings.append("分析覆盖限制：{}".format("；".join(coverage.get("limitations", [])[:3])))
    degraded = root_summary.get("statistics", {}).get("degraded_document_count", 0)
    if degraded:
        findings.append("有 {} 个文件使用兼容解析或仅保留元数据，涉及结论应优先人工复核。".format(degraded))
    if scan.get("truncated"):
        findings.append("扫描达到数量上限，本报告仅覆盖已扫描文件，不能代表完整磁盘范围。")
    if scan.get("errors"):
        findings.append("扫描阶段有 {} 个路径读取失败，需要补充权限后重新分析。".format(len(scan["errors"])))
    if stats.get("truncated_text_files", 0):
        findings.append("有 {} 个文件达到正文保存上限；相关摘要必须结合《解析覆盖率清单》复核，不得表述为全文完整覆盖。".format(stats["truncated_text_files"]))
    # ``fast_preview_paths`` mixes two unrelated limitations.  A scanned PDF
    # really was only OCR'd on a few pages; an Office file simply skipped the
    # optional embedded-image OCR while its body text was read in full.  Report
    # them separately so a .docx is never described as a 扫描型 PDF.
    fast_preview_paths = stats.get("fast_preview_paths", [])
    scanned_pdf_previews = [
        path for path in fast_preview_paths
        if str(path).lower().endswith(".pdf")
    ]
    office_image_skipped = [
        path for path in fast_preview_paths
        if not str(path).lower().endswith(".pdf")
    ]
    if scanned_pdf_previews:
        shown = "、".join(scanned_pdf_previews[:8])
        suffix = " 等" if len(scanned_pdf_previews) > 8 else ""
        findings.append("快速模式仅预览 OCR 了 {} 个扫描型 PDF 的前几页：{}{}；这些文件不得视为全文分析。".format(
            len(scanned_pdf_previews), shown, suffix
        ))
    if office_image_skipped:
        findings.append(
            "有 {} 个 Office 文件（如 .docx/.pptx/.xlsx）正文已完整提取，但快速模式跳过了内嵌图片 OCR；"
            "需要图片文字时请对这些文件切换高精度解析。".format(len(office_image_skipped))
        )
    if classification_source != "adaptive_analysis_tree":
        findings.append("自适应内容分类未完成，报告已降级为单一扫描清单类别；未按目录或文件逐项展开，避免产生误导性海量类别。")
    # Degradation must be visible: without this the semantic path could fail
    # silently and the directory would be read as if it were model-named.
    # Cluster degradation and naming degradation are separate facts and must be
    # described separately, otherwise a successful clustering run with a
    # degraded label would be reported as a failed clustering run.
    semantic_error = stats.get("semantic_cluster_error")
    semantic_clusters = int(stats.get("semantic_topic_clusters") or 0)
    if semantic_error and semantic_clusters == 0:
        findings.append(
            "文档级语义聚类未完成（{}），本次目录由词法主题兜底生成，主题命名与分组粒度会明显下降；"
            "建议修复后重新分析或人工复核目录。".format(str(semantic_error)[:200])
        )
    elif semantic_error:
        findings.append(
            "文档级语义聚类已完成（{} 个主题簇），但主题命名降级（{}）；"
            "分组结构可信，名称建议人工复核或触发“完善目录名称”。".format(
                semantic_clusters, str(semantic_error)[:200])
        )
    elif semantic_clusters == 0 and int(stats.get("parsed_files") or 0) > 1:
        findings.append("本次未产生语义主题簇，目录按词法主题生成，分组精度低于语义模式，请人工复核。")

    # The first four lines are pipeline counters, the rest are quality notices.
    pipeline_statistics, quality_notices = findings[:4], findings[4:]
    direction_candidates = _direction_candidates(scan, analysis)
    direction = direction_candidates[0] if direction_candidates else _research_direction(scan, summaries, analysis, candidates=[])
    research_audit = analysis.get("research_opportunity_audit") or {}
    if research_audit:
        direction["coverage_note"] = "已盘点 {} 个可用主题，发现 {} 项机会，精查 {} 项；研究比较限于当前登记并通过审查的候选。".format(
            len(research_audit.get("topics") or []), research_audit.get("discovered_count", 0), research_audit.get("refined_count", 0))
        if research_audit.get("deferred"):
            direction["limitations"] = list(direction.get("limitations") or []) + ["部分机会尚未精查，当前首选限于已完成证据审查的候选，详见处理记录。"]
    dimensions = analysis.get("classification_dimensions", [])
    structured = analysis.get("structured_data_overview") or stats.get("structured_data") or {}
    intelligence_overview = {
        "schema_version": "intelligence-overview/1.0",
        "temporal": {
            "source_modified_time_range": _time_range(analysis),
            "document_content_time_range": None,
            "limitation": "源文件修改时间与正文事件时间分开记录；当前没有可靠正文时间字段时不做替代推断。",
        },
        "entities": structured.get("entity_statistics") or {},
        "structured_anomaly_questions": list(structured.get("recommendation_questions") or [])[:12],
        "version_and_duplicates": {
            "exact_duplicate_groups": stats.get("exact_duplicate_groups", 0),
            "exact_duplicate_files": stats.get("exact_duplicate_files", 0),
            "similar_document_clusters": stats.get("similar_document_clusters", 0),
            "note": "精确重复可视为同内容副本；高相似簇仅是版本/关联候选，需人工核验后才能认定版本关系。",
        },
        "ocr_and_parse_quality": {
            "office_embedded_image_ocr_files": stats.get("office_embedded_image_ocr_files", 0),
            "truncated_text_files": stats.get("truncated_text_files", 0),
            "failed_files": stats.get("failed_files", 0),
            "structured_average_quality_score": stats.get("structured_average_quality_score"),
        },
        "incremental_reuse": {
            "reused_parse_checkpoints": stats.get("reused_parse_checkpoints", 0),
            "newly_processed_files": stats.get("newly_processed_files", stats.get("parsed_files", 0)),
        },
    }
    report = {
        "schema_version": 5,
        "research_version": research_audit.get("version"),
        "research_opportunity_audit": research_audit,
        "title": "数据包情况概览报告",
        "basic_information": [
            "扫描根目录：{}".format(scan["root"]),
            "递归文件总数：{}；子目录总数：{}".format(scan.get("file_count", 0), scan.get("directory_count", 0)),
            "数据总大小：{}".format(scan.get("total_size_human", "未知")),
            "可推断时间范围：{}".format(_time_range(analysis)),
            "来源/格式构成：{}".format("，".join("{} {}个".format(ext, count) for ext, count in ordered_types) or "无"),
            "本地分类维度：{}".format("；".join("{}（{}）".format(item.get("name"), item.get("reason")) for item in dimensions) or "数据量不足，保持原目录结构"),
            "内容分类覆盖：{} 个顶层类别，已归入 {} / {} 个扫描文件；其中已解析 {} 个（{}）。".format(
                classification_coverage["top_level_category_count"],
                classification_coverage["classified_file_count"],
                scanned_file_count,
                classification_coverage["parsed_file_count"],
                "完整" if classification_coverage["complete"] else "存在未归类或重复归类，请复核",
            ),
            "分析覆盖：扫描 {} 个文件，已解析 {} 个，抽样 {} 个，深度分析 {} 个，待处理 {} 个，失败 {} 个；文件覆盖率 {}，字节覆盖率 {}。".format(
                coverage.get("scanned_files", coverage.get("inventory_files", scan.get("file_count", 0))),
                coverage.get("parsed_files", stats.get("parsed_files", 0)),
                coverage.get("sampled_files", coverage.get("sampled_overview_files", 0)),
                coverage.get("deep_analyzed_files", 0),
                coverage.get("pending_files", stats.get("pending_files", 0)),
                coverage.get("failed_files", stats.get("failed_files", 0)),
                "{:.1%}".format(float(coverage.get("coverage_ratio", coverage.get("parsed_file_ratio") or 0))),
                "{:.1%}".format(float(coverage.get("parsed_byte_ratio") or 0)),
            ),
        ],
        "coverage": coverage,
        "value_judgment": value_judgment,
        "intelligence_overview": intelligence_overview,
        "global_categories": categories,
        "classification_coverage": classification_coverage,
        "key_findings": findings,
        "recommended_research_direction": direction,
        "directions": [_direction_entry(candidate) for candidate in direction_candidates[1:5]],
        "direction_candidates": direction_candidates,
        "analysis_method": {
            "parse": "Docling 统一文档模型；图片和扫描 PDF 使用 RapidOCR；失败项显式降级",
            "deduplication": "SHA-256 规范文档投影；主题、检索、证据独立来源和价值评分排除精确重复副本，原始路径仍完整保留",
            "similarity": "64-bit SimHash + 8-band LSH + 特征包含率；语义聚类在 500 份规范文档以上切换 MiniBatchKMeans",
            "retrieval": analysis.get("retrieval", {}).get("method", "BM25 + 本地 TF-IDF 字符向量"),
            "classification": "根据实际目录、格式、时间与正文高频主题动态选择维度",
            "traceability": "节点摘要和研究方向引用 evidence_id、文件路径、页/节、片段与源文件 SHA-256",
        },
        "generation_mode": "local_complete",
    }
    if not content_findings:
        return report
    report["basic_information"].append("处理统计：{}".format(
        "；".join(str(item).rstrip("。") for item in pipeline_statistics)
    ))
    report["quality_notices"] = list(quality_notices)
    report["limitations"] = list(quality_notices)
    value_judgment = dict(report.get("value_judgment") or {})
    value_judgment["limitations"] = list(dict.fromkeys(
        list(value_judgment.get("limitations") or []) + list(quality_notices)
    ))
    report["value_judgment"] = value_judgment
    report["content_key_findings"] = content_key_findings(summaries)
    report["key_findings_source"] = "verified_content"
    return compose_key_findings(report)


def _model_evidence_chain(evidence_catalog, evidence_ids, fallback, claim=None):
    by_id = {
        str(item.get("evidence_id")): item
        for item in evidence_catalog
        if item.get("evidence_id") and evidence_quality(item).get("eligible")
    }
    selected = []
    for evidence_id in evidence_ids or []:
        item = by_id.get(str(evidence_id))
        if item and item not in selected:
            if claim:
                verification = verify_claim_evidence(claim, item)
                if verification.get("support_status") != "supported":
                    continue
                item = dict(item)
                item.update(verification)
            selected.append(item)
    return selected


_CONFIDENCE_ORDER = {"低": 0, "中": 1, "高": 2}


def _inference_chain(evidence_catalog, allowed_ids, claim, semantic_threshold):
    """Evidence (restricted to ``allowed_ids``) relevant to one inference claim."""
    pool = [
        item for item in evidence_catalog
        if str(item.get("evidence_id") or "") in allowed_ids and evidence_quality(item).get("eligible")
    ]
    if not pool:
        return []
    scores, mode = claim_semantic_scores(claim, pool)
    output = []
    for index, item in enumerate(pool):
        verification = verify_inference_evidence(
            claim, item,
            semantic_score=scores[index] if index < len(scores) else 0.0,
            relevance_mode=mode, semantic_threshold=semantic_threshold,
        )
        if verification.get("support_status") == "supported":
            output.append(dict(item, **verification))
    return output


def _match_candidate(value, by_rank, by_title):
    if not isinstance(value, dict):
        return None, None
    try:
        rank = int(value.get("candidate_rank"))
    except (TypeError, ValueError):
        rank = None
    if rank is not None and rank in by_rank:
        return by_rank[rank], "candidate_rank"
    title = re.sub(r"\s+", "", str(value.get("title") or value.get("direction") or "")).casefold()
    if title and title in by_title:
        return by_title[title], "exact_title"
    return None, None


def _resolve_inference(evidence_catalog, candidate, cited_ids, claim, semantic_threshold):
    """Verify against the model's citations first, then the candidate's own evidence."""
    catalog_ids = {str(item.get("evidence_id")) for item in evidence_catalog if item.get("evidence_id")}
    candidate_ids = {str(value) for value in (candidate.get("evidence_ids") or []) if value} & catalog_ids
    cited = {str(value) for value in (cited_ids or []) if value} & candidate_ids
    if cited:
        chain = _inference_chain(evidence_catalog, cited, claim, semantic_threshold)
        if chain:
            return chain, "model_cited"
    chain = _inference_chain(evidence_catalog, candidate_ids, claim, semantic_threshold)
    return chain, ("candidate_evidence" if chain else "none")


def merge_model_report_aligned(local_report, model_report, evidence_catalog=None, semantic_threshold=0.86):
    """SJFX_OVERVIEW_EVIDENCE_ALIGN merge: rank-based choice, inference relevance check,
    fact-checked key findings.  The local candidate's scope, score, priority and
    evidence boundary stay fixed; the model contributes choice and wording."""
    merged = dict(local_report)
    evidence_catalog = list(evidence_catalog or [])
    model_report = model_report if isinstance(model_report, dict) else {}
    candidates = list(local_report.get("direction_candidates") or [])
    by_rank = {int(item["rank"]): item for item in candidates if isinstance(item.get("rank"), int)}
    by_title = {
        re.sub(r"\s+", "", str(item.get("title") or "")).casefold(): item
        for item in candidates if item.get("title")
    }
    adoption = {
        "recommendation": "absent", "matched_by": None, "evidence_source": None,
        "directions_offered": 0, "directions_adopted": 0,
        "key_findings_offered": 0, "key_findings_adopted": 0,
    }

    chosen_rank = None
    recommendation = model_report.get("recommended_research_direction")
    if isinstance(recommendation, dict):
        candidate, matched_by = _match_candidate(recommendation, by_rank, by_title)
        adoption["matched_by"] = matched_by
        if candidate is None:
            adoption["recommendation"] = "invalid_candidate"
            merged["rejected_model_recommendation"] = dict(
                recommendation, evidence_status="insufficient",
                rejection_reason="模型未指明有效的本地候选编号。",
            )
        else:
            proposal_review = None
            if candidate.get("candidate_id"):
                from services.research_opportunities import review_model_proposal
                patch, proposal_review, valid = review_model_proposal(candidate, recommendation, evidence_catalog)
                if not valid:
                    adoption["recommendation"] = "rejected_proposal_review"
                    merged["rejected_model_recommendation"] = dict(recommendation, proposal_review=proposal_review)
                    recommendation = {}
                else:
                    recommendation = dict(recommendation, **patch)
                    for field in ("research_questions", "methods"):
                        if field not in patch:
                            recommendation.pop(field, None)
            rationale = " ".join(str(recommendation.get("rationale") or "").split())
            claim = "{} {}".format(candidate.get("title") or "", rationale).strip()
            chain, source = _resolve_inference(
                evidence_catalog, candidate, recommendation.get("evidence_ids"), claim, semantic_threshold,
            )
            adoption["evidence_source"] = source
            record = dict(candidate)
            record.update({
                "type": "推论",
                "model_candidate_rank": candidate.get("rank"),
                "model_matched_by": matched_by,
                "local_rationale": candidate.get("rationale"),
                "evidence_selection": source,
            })
            if rationale:
                record["rationale"] = rationale
            for key in ("research_questions", "methods"):
                values = [str(value).strip() for value in (recommendation.get(key) or []) if str(value).strip()]
                if values:
                    record[key] = values[:4]
            model_confidence = str(recommendation.get("confidence") or "").strip()
            local_confidence = str(candidate.get("confidence") or "中")
            if model_confidence in _CONFIDENCE_ORDER and local_confidence in _CONFIDENCE_ORDER:
                # The model may lower, never raise, the locally computed confidence.
                record["confidence"] = min(model_confidence, local_confidence, key=_CONFIDENCE_ORDER.get)
            record["evidence_chain"] = chain
            record["evidence_ids"] = [item.get("evidence_id") for item in chain if item.get("evidence_id")]
            record["supports_evidence_ids"] = list(record["evidence_ids"])
            record["evidence_status"] = "supported" if chain else "insufficient"
            record["claims"] = [{
                "statement": record.get("rationale") or record.get("title"),
                "type": "inference",
                "evidence_ids": list(record["evidence_ids"]),
                "support_status": record["evidence_status"],
                "verification_contract": "inference-relevance/1.0",
            }]
            if proposal_review is not None:
                record["proposal_review"] = proposal_review
                record["evidence_chain"] = list(candidate.get("evidence_chain") or [])
                record["evidence_ids"] = list(candidate.get("evidence_ids") or [])
                if adoption["recommendation"] == "rejected_proposal_review":
                    chain = []
            if chain:
                adoption["recommendation"] = "adopted"
                chosen_rank = candidate.get("rank")
                merged["recommended_research_direction"] = record
            else:
                if adoption["recommendation"] != "rejected_proposal_review":
                    adoption["recommendation"] = "rejected_no_relevant_evidence"
                record["rationale"] = "证据不足，当前不能形成可靠推荐。"
                record.setdefault("limitations", [])
                record["limitations"] = list(record["limitations"]) + ["模型推荐理由与该候选的正文证据不相关，未采纳。"]
                merged["rejected_model_recommendation"] = record
    if chosen_rank is not None:
        merged["directions"] = [
            _direction_entry(item) for item in candidates[:5] if item.get("rank") != chosen_rank
        ][:4]

    validated = []
    for direction in (model_report.get("directions") or [])[:5]:
        if not isinstance(direction, dict):
            continue
        adoption["directions_offered"] += 1
        candidate, matched_by = _match_candidate(direction, by_rank, by_title)
        if candidate is None:
            continue
        if candidate.get("candidate_id"):
            from services.research_opportunities import review_model_proposal
            patch, review, valid = review_model_proposal(candidate, {
                "rationale": direction.get("basis") or direction.get("direction"),
                "evidence_ids": direction.get("evidence_ids") or [],
            }, evidence_catalog)
            if not valid:
                merged.setdefault("rejected_model_directions", []).append(dict(direction, proposal_review=review))
                continue
            direction = dict(direction, direction=candidate.get("title"), basis=patch["rationale"])
        basis = " ".join(str(direction.get("basis") or "").split())
        text = " ".join(str(direction.get("direction") or "").split()) or candidate.get("title")
        claim = "{} {} {}".format(candidate.get("title") or "", text, basis).strip()
        chain, source = _resolve_inference(
            evidence_catalog, candidate, direction.get("evidence_ids"), claim, semantic_threshold,
        )
        if not chain:
            continue
        confidence = str(direction.get("confidence") or "中")
        local_confidence = str(candidate.get("confidence") or "中")
        if confidence in _CONFIDENCE_ORDER and local_confidence in _CONFIDENCE_ORDER:
            confidence = min(confidence, local_confidence, key=_CONFIDENCE_ORDER.get)
        ids = [item.get("evidence_id") for item in chain if item.get("evidence_id")]
        validated.append({
            "direction": text,
            "type": "推论",
            "candidate_rank": candidate.get("rank"),
            "candidate_title": candidate.get("title"),
            "matched_by": matched_by,
            "basis": basis or candidate.get("basis"),
            "confidence": confidence,
            "confidence_note": direction.get("confidence_note"),
            "evidence_chain": chain,
            "supports_evidence_ids": ids,
            "evidence_selection": source,
            "evidence_status": "supported",
            "claims": [{
                "statement": basis or text, "type": "inference", "evidence_ids": ids,
                "support_status": "supported", "verification_contract": "inference-relevance/1.0",
            }],
        })
    adoption["directions_adopted"] = len(validated)
    if validated:
        merged["model_suggested_directions"] = validated

    by_id = {
        str(item.get("evidence_id")): item
        for item in evidence_catalog
        if item.get("evidence_id") and evidence_quality(item).get("eligible")
    }
    model_findings = []
    for finding in (model_report.get("key_findings") or [])[:5]:
        if not isinstance(finding, dict):
            continue  # free text without evidence ids is never adopted
        adoption["key_findings_offered"] += 1
        statement = " ".join(str(finding.get("statement") or "").split())[:300]
        ids = []
        for value in (finding.get("evidence_ids") or [])[:3]:
            item = by_id.get(str(value))
            if item and verify_claim_evidence(statement, item).get("support_status") == "supported":
                ids.append(str(value))
        if statement and ids:
            model_findings.append({
                "statement": statement, "source_type": "model_verified",
                "evidence_ids": ids, "support_status": "supported",
            })
    adoption["key_findings_adopted"] = len(model_findings)
    merged["model_key_findings"] = model_findings
    if "content_key_findings" in merged:
        compose_key_findings(merged, model_findings)

    adopted = adoption["recommendation"] == "adopted" or validated or model_findings
    merged["model_adoption"] = adoption
    merged["generation_mode"] = "model_analyzed" if adopted else local_report.get("generation_mode", "local_complete")
    return merged


def merge_model_report(local_report, model_report, evidence_catalog=None):
    merged = dict(local_report)
    # Content categories are a complete, deterministic projection of the local
    # analysis tree. Language enhancement must never truncate or replace them.
    # Free-form model findings without evidence ids must never replace the
    # deterministic local overview.  Directions are validated below.
    evidence_catalog = evidence_catalog or []
    local_primary = dict(local_report.get("recommended_research_direction") or {})
    fallback_evidence = local_primary.get("evidence_chain", [])
    recommendation = model_report.get("recommended_research_direction")
    if isinstance(recommendation, dict) and recommendation.get("title"):
        # Let synthesis choose among locally grounded candidates, while keeping
        # each candidate's scope and evidence boundary fixed.
        recommendation = dict(recommendation)
        recommendation["model_proposed_title"] = recommendation.get("title")
        local_candidates = list(local_report.get("direction_candidates") or [])
        chosen = next((item for item in local_candidates
                       if str(item.get("title") or "").strip()
                       == str(recommendation.get("title") or "").strip()), None)
        if chosen is None:
            chosen = local_primary
        chosen = dict(chosen or {})
        recommendation["title"] = chosen.get("title") or local_primary.get("title") or recommendation.get("title")
        for key in ("direction", "rank", "score", "score_breakdown", "priority",
                    "confidence_score", "representative_documents"):
            if key in chosen:
                recommendation[key] = chosen[key]
        recommendation["type"] = "推论"
        selected_ids = [str(value) for value in recommendation.get("evidence_ids", [])]
        allowed_ids = {str(value) for value in chosen.get("evidence_ids", []) if value}
        selected_ids = [value for value in selected_ids if value in allowed_ids]
        recommendation.pop("evidence_ids", None)
        recommendation["evidence_chain"] = _model_evidence_chain(
            evidence_catalog,
            selected_ids,
            chosen.get("evidence_chain") or fallback_evidence,
            claim=recommendation.get("rationale") or recommendation.get("title"),
        )
        recommendation["supports_evidence_ids"] = [
            item.get("evidence_id") for item in recommendation["evidence_chain"] if item.get("evidence_id") in selected_ids
        ]
        recommendation["evidence_status"] = "supported" if recommendation["evidence_chain"] else "insufficient"
        recommendation["claims"] = [{
            "statement": recommendation.get("rationale") or recommendation.get("title"),
            "type": "inference",
            "evidence_ids": [item.get("evidence_id") for item in recommendation["evidence_chain"] if item.get("evidence_id")],
            "support_status": recommendation["evidence_status"],
        }]
        recommendation.setdefault("confidence", "中")
        if recommendation["evidence_status"] == "insufficient":
            recommendation["priority"] = "低"
            recommendation["confidence"] = "低"
            recommendation["rationale"] = "证据不足，当前不能形成可靠推荐。"
            recommendation.setdefault("limitations", []).append("模型未引用合法正文证据，该方向不进入正式首选方向。")
        if recommendation["evidence_status"] == "supported":
            merged["recommended_research_direction"] = recommendation
        else:
            merged["rejected_model_recommendation"] = recommendation
    validated_directions = []
    for direction in model_report.get("directions", []) or []:
        if isinstance(direction, dict):
            direction["type"] = "推论"
            direction["evidence_chain"] = _model_evidence_chain(
                evidence_catalog,
                direction.pop("evidence_ids", []),
                fallback_evidence,
                claim=direction.get("basis") or direction.get("direction") or direction.get("title"),
            )
            direction["supports_evidence_ids"] = [
                item.get("evidence_id") for item in direction["evidence_chain"] if item.get("evidence_id")
            ]
            direction["evidence_status"] = "supported" if direction["evidence_chain"] else "insufficient"
            direction["claims"] = [{
                "statement": direction.get("basis") or direction.get("direction") or direction.get("title"),
                "type": "inference",
                "evidence_ids": [item.get("evidence_id") for item in direction["evidence_chain"] if item.get("evidence_id")],
                "support_status": direction["evidence_status"],
            }]
            direction.setdefault("confidence", "中")
            if direction["evidence_status"] == "supported":
                validated_directions.append(direction)
    if validated_directions:
        merged["model_suggested_directions"] = validated_directions
    merged["generation_mode"] = "model_analyzed"
    return merged


def compact_summary_context(summaries, limit=12):
    output = []
    for item in _valid_summaries(summaries):
        payload = item["payload"]
        output.append({
            "path": item.get("path"),
            "type": item.get("type"),
            "title": payload.get("title"),
            "summary": (payload.get("summary") or payload.get("core_summary") or "")[:1800],
            "topics": payload.get("topics", [])[:10],
            "key_facts": payload.get("key_facts", [])[:12],
            "file_conclusions": payload.get("file_conclusions", [])[:8],
            "file_arguments": payload.get("file_arguments", [])[:6],
            "evidence_quality": payload.get("evidence_quality", {}),
            "evidence_chain": payload.get("evidence_chain", [])[:5],
        })
        if len(output) >= limit:
            break
    return output
