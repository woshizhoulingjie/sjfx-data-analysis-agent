"""Bounded, recoverable first-pass analysis for genuinely large data packages.

The module intentionally keeps the first pass honest: it inventories every
file, profiles a deterministic representative subset, and records everything
that remains pending.  A later scoped run can reuse completed files and extend
the same analysis without restarting from zero.
"""
import hashlib
from pathlib import Path

from services.scanner import human_size


def file_fingerprint(node):
    return "{}|{}|{}".format(
        node.get("path", ""), node.get("size", 0), node.get("modified_at", ""),
    )


def build_policy(scan, options=None):
    options = options or {}
    file_count = int(scan.get("file_count") or 0)
    total_size = int(scan.get("total_size") or 0)
    threshold_bytes = int(options.get("threshold_bytes") or 1024 * 1024 * 1024)
    threshold_files = int(options.get("threshold_files") or 3000)
    initial_limit = max(1, int(options.get("initial_parse_files") or 700))
    deepen_limit = max(1, int(options.get("deepen_batch_files") or 500))
    overview_chars = max(4000, int(options.get("overview_chars_per_file") or 30000))
    enabled = total_size >= threshold_bytes or file_count >= threshold_files
    return {
        "mode": "large_package" if enabled else "standard",
        "enabled": enabled,
        "threshold_bytes": threshold_bytes,
        "threshold_files": threshold_files,
        "initial_parse_files": initial_limit,
        "deepen_batch_files": deepen_limit,
        "overview_chars_per_file": overview_chars,
        "inventory_files": file_count,
        "inventory_bytes": total_size,
        "inventory_size_human": human_size(total_size),
    }


def representative_paths(files, limit):
    """Choose deterministic representatives with information-density priority.

    Small CSV/TXT/JSON files often contain the most actionable conclusions but
    are easy to hide beside large media or generated archives.  They receive a
    bounded priority quota; format coverage and deterministic spread still
    guarantee that other extensions remain visible in the first pass.
    """
    files = sorted(files, key=lambda item: item.get("path", ""))
    if len(files) <= limit:
        return [item.get("path") for item in files]
    limit = max(1, int(limit))
    text_extensions = {".csv", ".tsv", ".txt", ".json", ".jsonl", ".md", ".markdown"}
    small_text_limit = 5 * 1024 * 1024

    def is_priority(item):
        extension = str(item.get("extension") or Path(item.get("path", "")).suffix).lower()
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return extension in text_extensions and size < small_text_limit

    priority_files = [item for item in files if is_priority(item)]
    # Reserve at most 35% for dense small text so a directory containing many
    # tiny logs cannot crowd out PDFs, spreadsheets and presentations.
    priority_quota = min(len(priority_files), max(1, int(limit * 0.35)))
    priority_files = sorted(priority_files, key=lambda item: (int(item.get("size") or 0), item.get("path", "")))
    by_extension = {}
    for item in files:
        by_extension.setdefault(item.get("extension") or "[无扩展名]", []).append(item)
    selected = []
    selected_paths = set()

    def add(item):
        path = item.get("path")
        if path and path not in selected_paths and len(selected) < limit:
            selected_paths.add(path)
            selected.append(path)

    for item in priority_files[:priority_quota]:
        add(item)
        if len(selected) >= limit:
            return sorted(selected)[:limit]

    # First guarantee that every observed format and its rough start/end are
    # visible.  This prevents a large CSV/PDF minority from disappearing.
    for _extension, values in sorted(by_extension.items(), key=lambda item: (-len(item[1]), item[0])):
        add(values[0])
        if len(values) > 1:
            add(values[-1])
    if len(selected) >= limit:
        return sorted(selected)[:limit]

    remaining = [item for item in files if item.get("path") not in selected_paths]
    slots = limit - len(selected)
    if slots == 1:
        add(remaining[len(remaining) // 2])
    elif slots > 1:
        # Fill remaining slots by a stable blend of high-information files and
        # evenly distributed representatives from the original inventory.
        ranked = sorted(
            remaining,
            key=lambda item: (0 if is_priority(item) else 1, item.get("path", "")),
        )
        for item in ranked[:slots]:
            add(item)
        if len(selected) < limit:
            for index in range(slots):
                if not remaining:
                    break
                add(remaining[round(index * (len(remaining) - 1) / float(max(1, slots - 1)))])
                if len(selected) >= limit:
                    break
    return sorted(selected)


def compact_overview_document(document, max_chars):
    """Keep only enough content for package-level exploration.

    The source file remains authoritative.  A user can request a selected file
    or scope to be parsed at normal depth later; this projection must never be
    described as complete-document analysis.
    """
    result = dict(document)
    text = str(result.get("text") or "")
    max_chars = max(1000, int(max_chars))
    if len(text) > max_chars:
        head = int(max_chars * 0.70)
        tail = max_chars - head
        result["text"] = text[:head] + "\n\n[大数据包首轮概览已省略中间正文；可对该文件按需深挖]\n\n" + text[-tail:]
    evidence = list(result.get("evidence") or [])
    if len(evidence) > 40:
        positions = sorted(set(round(index * (len(evidence) - 1) / 39.0) for index in range(40)))
        result["evidence"] = [evidence[index] for index in positions]
    coverage = dict(result.get("coverage") or {})
    coverage.update({
        "overview_sampled": True,
        "large_package_mode": True,
        "overview_character_limit": max_chars,
        "stored_characters": len(result.get("text") or ""),
        "complete": False,
        "coverage_ratio_reason": "大数据包首轮仅保留代表正文和代表证据；选择该文件后可按需进行完整解析。",
    })
    result["coverage"] = coverage
    warnings = list(result.get("warnings") or [])
    warning = "大数据包首轮概览：该文件保存为有界代表内容，尚未进行完整深度解析。"
    if warning not in warnings:
        warnings.append(warning)
    result["warnings"] = warnings
    return result


def inventory_by_path(scan):
    output = {}
    stack = [scan.get("tree") or {}]
    while stack:
        node = stack.pop()
        if node.get("kind") == "file" and node.get("path"):
            output[node["path"]] = node
        stack.extend(reversed(node.get("children") or []))
    return output


def build_coverage(scan, documents, failures=None, pending_paths=None, policy=None):
    files = inventory_by_path(scan)
    failures = failures or []
    failed_paths = {item.get("path") for item in failures if item.get("path")}
    pending_paths = set(pending_paths or [])
    parsed_paths = set(documents)

    def for_paths(paths=None):
        # ``paths=[]`` is a legitimate empty scope (for example an empty
        # virtual node).  Treating it as the package root silently inflated
        # coverage and could make a node look fully analysed.
        selected = set(files) if paths is None else set(paths)
        selected &= set(files)
        parsed = selected & parsed_paths
        failed = selected & failed_paths
        pending = selected & pending_paths
        # Paths absent from all known processing states are pending too.
        pending |= selected - parsed - failed
        total_bytes = sum(int(files[path].get("size") or 0) for path in selected)
        parsed_bytes = sum(int(files[path].get("size") or 0) for path in parsed)
        complete_text = sum(
            1 for path in parsed
            if (documents.get(path, {}).get("coverage") or {}).get("complete")
        )
        sampled = sum(
            1 for path in parsed
            if (documents.get(path, {}).get("coverage") or {}).get("overview_sampled")
        )
        partial = sum(
            1 for path in parsed
            if not (documents.get(path, {}).get("coverage") or {}).get("complete")
        )
        deep_analyzed = len(parsed) - sampled
        complete_analysis = bool(selected) and not pending and not failed and partial == 0
        archive_manifests = []
        for path in sorted(parsed):
            manifest = documents.get(path, {}).get("archive_manifest")
            if isinstance(manifest, dict) and manifest.get("total_members") is not None:
                compact = dict(manifest)
                compact["container_path"] = compact.get("container_path") or path
                archive_manifests.append(compact)
        archive_total_members = sum(int(item.get("total_members") or 0) for item in archive_manifests)
        archive_parsed_members = sum(int(item.get("parsed_members") or 0) for item in archive_manifests)
        archive_skipped_members = sum(int(item.get("skipped_members") or 0) for item in archive_manifests)
        archive_failed_members = sum(int(item.get("failed_members") or 0) for item in archive_manifests)
        if complete_analysis:
            coverage_level = "full_text_analysis"
        elif parsed:
            coverage_level = "representative_overview"
        else:
            coverage_level = "inventory_complete"
        status = "完整" if complete_analysis else "部分覆盖"
        if not parsed and selected:
            status = "待分析"
        limitations = []
        if (policy or {}).get("enabled"):
            limitations.append("大数据包采用代表性文件首轮概览，未抽中的文件需要按范围继续深化。")
        if sampled:
            limitations.append("{} 个文件当前为抽样/首轮概览，不能视为全文深度分析。".format(sampled))
        if partial:
            limitations.append("{} 个已解析文件存在正文截断、快速模式或其他不完整覆盖。".format(partial))
        if pending:
            limitations.append("{} 个文件尚未进入内容分析。".format(len(pending)))
        if failed:
            limitations.append("{} 个文件解析失败，可通过失败文件重试继续处理。".format(len(failed)))
        if archive_skipped_members or archive_failed_members:
            limitations.append(
                "压缩包成员仅解析 {}/{} 个，跳过 {} 个、失败 {} 个；压缩包不得视为完整覆盖。".format(
                    archive_parsed_members, archive_total_members, archive_skipped_members, archive_failed_members
                )
            )
        return {
            "mode": (policy or {}).get("mode", "standard"),
            "status": status,
            "inventory_files": len(selected),
            "scanned_files": len(selected),
            "parsed_files": len(parsed),
            "sampled_files": sampled,
            "complete_text_files": complete_text,
            "sampled_overview_files": sampled,
            "deep_analyzed_files": max(0, deep_analyzed),
            "partial_text_files": partial,
            "failed_files": len(failed),
            "pending_files": len(pending),
            "parsed_file_ratio": round(len(parsed) / float(len(selected) or 1), 6),
            "coverage_ratio": round(len(parsed) / float(len(selected) or 1), 6),
            "inventory_bytes": total_bytes,
            "scanned_bytes": total_bytes,
            "inventory_size_human": human_size(total_bytes),
            "parsed_bytes": parsed_bytes,
            "parsed_byte_ratio": round(parsed_bytes / float(total_bytes or 1), 6),
            "complete_analysis": complete_analysis,
            "coverage_level": coverage_level,
            "coverage_level_label": {
                "inventory_complete": "全量盘点",
                "representative_overview": "代表性概览",
                "full_text_analysis": "全文深度分析",
            }[coverage_level],
            "archive_containers": archive_manifests,
            "archive_member_totals": {
                "total_members": archive_total_members,
                "parsed_members": archive_parsed_members,
                "skipped_members": archive_skipped_members,
                "failed_members": archive_failed_members,
            },
            "limitations": limitations,
            "pending_paths": sorted(pending)[:200],
            "failed_paths": sorted(failed)[:200],
            "large_package_notice": (
                "首轮结果基于代表文件；未覆盖文件可在选中范围后继续补充分析。"
                if (policy or {}).get("enabled") else None
            ),
        }

    return for_paths, for_paths()


def attach_tree_coverage(tree, coverage_for_paths, all_paths):
    """Attach the same honest coverage contract to root/topic/subtopic nodes."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.get("kind") == "analysis_root":
            node["coverage"] = coverage_for_paths(all_paths)
        elif node.get("kind") == "group":
            node["coverage"] = coverage_for_paths(node.get("member_paths") or [])
        stack.extend(reversed(node.get("children") or []))
    return tree


def pending_group(paths, inventory, policy):
    paths = sorted(set(paths))
    if not paths:
        return None
    total_size = sum(int(inventory[path].get("size") or 0) for path in paths if path in inventory)
    return {
        "kind": "group",
        "node_type": "pending_scope",
        "node_id": "pending-{}".format(hashlib.sha256("|".join(paths).encode("utf-8")).hexdigest()[:16]),
        "dimension": "分析进度",
        "name": "待按需深度分析",
        "summary": "该分支包含 {} 个尚未进入首轮内容分析的文件。可选中物理目录后使用“补充分析当前范围”分批处理。".format(len(paths)),
        "member_paths": paths,
        "file_count": len(paths),
        "total_size": total_size,
        "total_size_human": human_size(total_size),
        "related_topics": [],
        "evidence_chain": [],
        "conclusion_evidence": [],
        "children": [],
        "coverage": {
            "mode": policy.get("mode"), "status": "待分析", "inventory_files": len(paths),
            "parsed_files": 0, "pending_files": len(paths), "failed_files": 0,
        },
    }
