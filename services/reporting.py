import json
from collections import Counter
from pathlib import Path


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


def _research_direction(_scan, _summaries, analysis):
    """Return an honest placeholder until the model evaluates the evidence.

    Rules may rank and preserve evidence, but they must never manufacture a
    domain-specific research recommendation from a few trigger words.
    """
    evidence = _analysis_evidence(analysis, 8)
    statistics = analysis.get("statistics", {})
    parsed = int(statistics.get("parsed_files") or 0)
    failed = int(statistics.get("failed_files") or 0)
    topic_count = int(statistics.get("topic_clusters") or 0)
    return {
        "title": "核验高频主题与代表文档之间的一致性",
        "type": "推论",
        "rationale": "本地已解析 {} 个文件并形成 {} 个主题簇，当前最可执行的深入方向是核验主题线索在代表文档中的一致性；{} 个文件仍需复核。".format(parsed, topic_count, failed),
        "basis": "依据为本地解析统计、主题簇代表文档和下列可定位证据；这是推论，不是外部事实。",
        "research_questions": [],
        "methods": [],
        "priority": "待定",
        "confidence": "待模型评估",
        "evidence_chain": evidence,
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
        "file_count": len(paths),
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
    classified_file_count = sum(item.get("file_count", 0) for item in categories)
    parsed_file_count = stats.get("parsed_files", 0)
    scanned_file_count = scan.get("file_count", 0)
    classification_coverage = {
        "source": classification_source,
        "dimension_count": len(analysis.get("classification_dimensions", [])),
        "top_level_category_count": len(categories),
        "classified_file_count": classified_file_count,
        "parsed_file_count": parsed_file_count,
        "coverage_ratio": round(classified_file_count / float(scanned_file_count or 1), 6),
        "complete": classified_file_count == scanned_file_count,
    }

    findings = [
        "包内精确去重发现 {} 个重复组，除每组保留 1 个规范副本外，可合并重复文件 {} 个。".format(stats.get("exact_duplicate_groups", 0), stats.get("exact_duplicate_files", 0)),
        "高相似度检测形成 {} 个相似文档簇；内容主题聚合形成 {} 个主题簇。".format(stats.get("similar_document_clusters", 0), stats.get("topic_clusters", 0)),
        "统一解析成功登记 {} 个文件，生成 {} 条可回查证据；{} 个文件解析失败。".format(stats.get("parsed_files", 0), stats.get("evidence_items", 0), stats.get("failed_files", 0)),
        "本地混合检索已建立 {} 个证据块；{} 个 Office 文件启用了内嵌图片 OCR。".format(stats.get("retrieval_evidence_chunks", 0), stats.get("office_embedded_image_ocr_files", 0)),
    ]
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

    direction = _research_direction(scan, summaries, analysis)
    dimensions = analysis.get("classification_dimensions", [])
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
        ],
        "global_categories": categories,
        "classification_coverage": classification_coverage,
        "key_findings": findings,
        "recommended_research_direction": direction,
        "directions": [{
            "direction": direction["title"],
            "type": "推论",
            "confidence": direction["confidence"],
            "evidence_chain": direction["evidence_chain"],
            "basis": direction.get("basis") or direction["rationale"],
            "confidence_note": "置信度由跨文档主题覆盖与可引用证据数量决定；建议在正式研究前复核代表文档原文。",
        }],
        "analysis_method": {
            "parse": "Docling 统一文档模型；图片和扫描 PDF 使用 RapidOCR；失败项显式降级",
            "deduplication": "SHA-256 精确去重",
            "similarity": "64-bit SimHash + 8-band LSH + 特征包含率",
            "retrieval": analysis.get("retrieval", {}).get("method", "BM25 + 本地 TF-IDF 字符向量"),
            "classification": "根据实际目录、格式、时间与正文高频主题动态选择维度",
            "traceability": "节点摘要和研究方向引用 evidence_id、文件路径、页/节、片段与源文件 SHA-256",
        },
        "generation_mode": "local_complete",
    }


def _model_evidence_chain(evidence_catalog, evidence_ids, fallback):
    by_id = {str(item.get("evidence_id")): item for item in evidence_catalog if item.get("evidence_id")}
    selected = []
    for evidence_id in evidence_ids or []:
        item = by_id.get(str(evidence_id))
        if item and item not in selected:
            selected.append(item)
    return selected or list(fallback or [])


def merge_cloud_report(local_report, cloud_report, evidence_catalog=None):
    merged = dict(local_report)
    # Content categories are a complete, deterministic projection of the local
    # analysis tree. Language enhancement must never truncate or replace them.
    for key in ("key_findings", "directions"):
        if isinstance(cloud_report.get(key), list) and cloud_report[key]:
            merged[key] = cloud_report[key]
    evidence_catalog = evidence_catalog or []
    fallback_evidence = local_report.get("recommended_research_direction", {}).get("evidence_chain", [])
    recommendation = cloud_report.get("recommended_research_direction")
    if isinstance(recommendation, dict) and recommendation.get("title"):
        # The cloud model may improve language and questions, but it must not erase
        # locally generated traceability or the inference label.
        recommendation = dict(recommendation)
        recommendation["type"] = "推论"
        selected_ids = [str(value) for value in recommendation.get("evidence_ids", [])]
        recommendation["evidence_chain"] = _model_evidence_chain(
            evidence_catalog, recommendation.pop("evidence_ids", []), fallback_evidence
        )
        recommendation["supports_evidence_ids"] = [
            item.get("evidence_id") for item in recommendation["evidence_chain"] if item.get("evidence_id") in selected_ids
        ]
        recommendation.setdefault("confidence", "中")
        merged["recommended_research_direction"] = recommendation
    for direction in merged.get("directions", []):
        if isinstance(direction, dict):
            direction["type"] = "推论"
            direction["evidence_chain"] = _model_evidence_chain(
                evidence_catalog, direction.pop("evidence_ids", []), fallback_evidence
            )
            direction["supports_evidence_ids"] = [
                item.get("evidence_id") for item in direction["evidence_chain"] if item.get("evidence_id")
            ]
            direction.setdefault("confidence", "中")
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
