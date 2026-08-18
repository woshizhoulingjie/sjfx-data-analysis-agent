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
    """Choose deterministic, type-aware representatives across the package."""
    files = sorted(files, key=lambda item: item.get("path", ""))
    if len(files) <= limit:
        return [item.get("path") for item in files]
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
        for index in range(slots):
            add(remaining[round(index * (len(remaining) - 1) / float(slots - 1))])
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
        selected = set(paths or files)
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
        status = "完整" if not pending and not failed and partial == 0 else "部分覆盖"
        if not parsed and selected:
            status = "待分析"
        return {
            "mode": (policy or {}).get("mode", "standard"),
            "status": status,
            "inventory_files": len(selected),
            "parsed_files": len(parsed),
            "complete_text_files": complete_text,
            "sampled_overview_files": sampled,
            "partial_text_files": partial,
            "failed_files": len(failed),
            "pending_files": len(pending),
            "parsed_file_ratio": round(len(parsed) / float(len(selected) or 1), 6),
            "inventory_bytes": total_bytes,
            "inventory_size_human": human_size(total_bytes),
            "parsed_bytes": parsed_bytes,
            "parsed_byte_ratio": round(parsed_bytes / float(total_bytes or 1), 6),
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
