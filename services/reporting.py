import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from services.evidence import evidence_quality, select_evidence, verify_claim_evidence


DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".md", ".txt", ".png", ".jpg", ".jpeg"}
DATA_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".json", ".jsonl"}


def _valid_summaries(summaries):
    return [item for item in summaries if item.get("payload", {}).get("schema_version") in (3, 4)]


def _analysis_evidence(analysis, limit=8):
    output = []
    seen = set()
    for retrieval_key in ("research_retrieval", "retrieval"):
        for search in analysis.get(retrieval_key, {}).get("queries", []):
            for item in search.get("results", []):
                key = item.get("evidence_id") or (item.get("source_path"), item.get("content_sha256"), item.get("text"))
                if key in seen:
                    continue
                seen.add(key)
                output.append(item)
                if len(output) >= limit:
                    return output
        if output:
            return output[:limit]
    for cluster in analysis.get("topic_clusters", []):
        for item in cluster.get("evidence_chain", []):
            key = item.get("evidence_id") or (item.get("source_path"), item.get("text"))
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
            if len(output) >= limit:
                return output
    root_summary = analysis.get("node_summaries", {}).get(".", {})
    for item in root_summary.get("evidence_chain", []):
        key = item.get("evidence_id") or item.get("source_path")
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output[:limit]


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


def _direction_candidates(scan, analysis, limit=5):
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

        dimensions = {
            "scale": min(1.0, math.log1p(len(members)) / math.log1p(max(2, parsed))),
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
            "scale": 0.12, "concentration": 0.10, "information_richness": 0.15,
            "independent_sources": 0.12, "recency": 0.08, "anomaly_signal": 0.08,
            "technical_impact": 0.12, "reportability": 0.10, "novelty": 0.07,
            "user_relevance": 0.06,
        }
        score = round(100 * sum(dimensions[key] * weight for key, weight in weights.items()), 1)
        if score >= 70 and evidence_count >= 3 and independent_sources >= 2:
            priority = "高"
        elif score >= 45:
            priority = "中"
        else:
            priority = "低"
        confidence_score = min(1.0, 0.45 * dimensions["information_richness"] + 0.35 * dimensions["independent_sources"] + 0.20 * _score01(coverage.get("parsed_file_ratio")))
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
            "rationale": "该方向包含 {} 个已解析成员、{} 条合格证据和 {} 个独立来源；综合规模、集中度、丰富度、时效、异常、技术影响、可成稿性、新颖性与任务相关性后得分 {:.1f}。".format(len(members), evidence_count, independent_sources, score),
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


def _research_direction(_scan, _summaries, analysis):
    candidates = _direction_candidates(_scan, analysis)
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
        "rationale": "当前尚未形成有足够正文证据支撑的稳定主题，不能强行推荐领域方向。",
        "basis": "先补充待分析或失败文件，再重新生成概览。",
        "research_questions": ["哪些文件可以形成稳定主题？", "当前证据是否足以回答客户指定问题？"],
        "methods": ["重试失败文件", "选择待分析节点进行分批深化"],
        "evidence_chain": evidence,
        "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
        "limitations": list(coverage.get("limitations") or []) + ["证据不足，当前方向仅作为处理建议。"],
        "confidence": "低",
    }


def build_report_analysis_prompt(scan, summaries, analysis, local_report, evidence_limit=8, excerpt_limit=650):
    """Build a bounded, traceable input for the report-planning model."""
    catalog = []
    for item in _analysis_evidence(analysis, evidence_limit):
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
    for item in _valid_summaries(summaries)[:6]:
        payload = item.get("payload", {})
        summaries_context.append({
            "path": item.get("path"),
            "title": payload.get("title"),
            "summary": (payload.get("summary") or payload.get("core_summary") or "")[:800],
            "topics": payload.get("topics", [])[:8],
            "key_facts": payload.get("key_facts", [])[:8],
        })

    categories = [{
        "name": item.get("name"),
        "dimension": item.get("dimension"),
        "file_count": item.get("file_count"),
        "topics": item.get("topics", [])[:8],
        "representative_documents": item.get("representative_documents", [])[:4],
    } for item in local_report.get("global_categories", [])]

    prompt = """你正在为一个未知数据包撰写《数据包情况概览报告》的研究规划部分。请基于证据目录和本地分类做实质性分析，而不是按关键词套用固定题目。

硬约束：
1. 只能使用给定证据目录中的 evidence_id；不得编造文件、时间、结论、数字或领域事实。
2. “推荐研究方向”和“深入方向”都是推论，必须列出支撑它的 evidence_ids。证据不足时明确写出不足，不得强行给高置信度。
3. 优先发现资料之间的共同主题、分歧、版本关系、证据缺口、可验证问题及合适的研究方法。
4. 本地分类是确定性结构，不能重写或新增分类。
5. 推荐方向必须使用本地十维评分候选的第 1 名；你只能改善理由、研究问题和方法，不能改名、换序或另造方向。

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
本地十维评分候选（排序不可更改）：{candidates}
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


def build_local_report(scan, summaries, analysis=None):
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
        "统一解析成功登记 {} 个文件，生成 {} 条可回查证据；{} 个文件解析失败。".format(stats.get("parsed_files", 0), stats.get("evidence_items", 0), stats.get("failed_files", 0)),
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
    fast_preview_paths = stats.get("fast_preview_paths", [])
    if fast_preview_paths:
        shown = "、".join(fast_preview_paths[:8])
        suffix = " 等" if len(fast_preview_paths) > 8 else ""
        findings.append("快速模式仅预览 OCR 了 {} 个扫描型 PDF 的前几页：{}{}；这些文件不得视为全文分析。".format(
            len(fast_preview_paths), shown, suffix
        ))
    if classification_source != "adaptive_analysis_tree":
        findings.append("自适应内容分类未完成，报告已降级为单一扫描清单类别；未按目录或文件逐项展开，避免产生误导性海量类别。")

    direction_candidates = _direction_candidates(scan, analysis)
    direction = direction_candidates[0] if direction_candidates else _research_direction(scan, summaries, analysis)
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
    return {
        "schema_version": 5,
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
        "directions": [{
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
        } for candidate in direction_candidates[1:5]],
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
        # The local model may improve language and questions, but it must not erase
        # locally generated traceability or the inference label.
        recommendation = dict(recommendation)
        recommendation["model_proposed_title"] = recommendation.get("title")
        # Ranking and scope are deterministic. The model may improve wording
        # and questions, but cannot replace the top-ranked local candidate.
        recommendation["title"] = local_primary.get("title") or recommendation.get("title")
        for key in ("direction", "rank", "score", "score_breakdown", "priority", "confidence_score", "representative_documents"):
            if key in local_primary:
                recommendation[key] = local_primary[key]
        recommendation["type"] = "推论"
        selected_ids = [str(value) for value in recommendation.get("evidence_ids", [])]
        recommendation["evidence_chain"] = _model_evidence_chain(
            evidence_catalog,
            recommendation.pop("evidence_ids", []),
            fallback_evidence,
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
            "evidence_chain": payload.get("evidence_chain", [])[:5],
        })
        if len(output) >= limit:
            break
    return output
