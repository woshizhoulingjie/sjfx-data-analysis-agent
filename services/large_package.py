"""Bounded, recoverable processing for genuinely large data packages.

Large-package mode still uses bounded batches and checkpoint reuse, but it no
longer stops after a representative sample.  Every inventoried file enters the
same parse pipeline; expensive deduplication and embedding clustering remain
optional statistics and never gate the content classification tree.
"""
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from services.scanner import human_size


PARSER_CHECKPOINT_SCHEMA = "large-package-checkpoint/2"


def file_fingerprint(node, parse_mode="accurate", parser_contract=None, source_sha256=None):
    """Hash every input that can change a parse result.

    The source digest is mandatory for reuse decisions.  Metadata remains in
    the fingerprint as a cheap diagnostic and to invalidate old checkpoints.
    """
    payload = {
        "schema": PARSER_CHECKPOINT_SCHEMA,
        "path": str(node.get("path") or ""),
        "size": int(node.get("size") or 0),
        "modified_at_ns": int(node.get("modified_at_ns") or 0),
        "device": node.get("device"),
        "inode": node.get("inode"),
        "source_sha256": str(source_sha256 or ""),
        "parse_mode": "fast" if str(parse_mode).lower() == "fast" else "accurate",
        "parser_contract": parser_contract or {},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_policy(scan, options=None):
    options = options or {}
    file_count = int(scan.get("file_count") or 0)
    total_size = int(scan.get("total_size") or 0)
    threshold_bytes = int(options.get("threshold_bytes") or 1024 * 1024 * 1024)
    threshold_files = int(options.get("threshold_files") or 3000)
    # Kept for backwards-compatible configuration display.  They are no longer
    # used as a hard cap in full large-package mode.
    initial_limit = max(1, int(options.get("initial_parse_files") or 700))
    deepen_limit = max(1, int(options.get("deepen_batch_files") or 500))
    batch_files = max(1, min(1000, int(options.get("batch_files") or 100)))
    overview_chars = max(1000, min(12000, int(options.get("overview_chars_per_file") or 4000)))
    overview_evidence = max(1, min(20, int(options.get("overview_evidence_per_file") or 6)))
    enabled = total_size >= threshold_bytes or file_count >= threshold_files
    return {
        "mode": "large_package" if enabled else "standard",
        "enabled": enabled,
        "threshold_bytes": threshold_bytes,
        "threshold_files": threshold_files,
        "initial_parse_files": initial_limit,
        "deepen_batch_files": deepen_limit,
        "batch_files": batch_files,
        "full_inventory_processing": True,
        "classification_scope": "all_parsed_files",
        "overview_chars_per_file": overview_chars,
        "overview_evidence_per_file": overview_evidence,
        "inventory_files": file_count,
        "inventory_bytes": total_size,
        "inventory_size_human": human_size(total_size),
        "checkpoint_resume": True,
        "pause_behavior": "安全停止后保留逐文件检查点；再次启动同一扫描可续跑",
        "deep_analysis_strategy": "首轮全量快速解析与索引，代表性概览；用户选择范围后准确解析和全文语义分析",
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
    inventory_errors = list(scan.get("errors") or [])
    inventory_error_count = int(scan.get("scan_error_count", len(inventory_errors)) or 0)
    inventory_truncated = bool(scan.get("truncated"))
    depth_limited = int(scan.get("depth_limited_directory_count") or 0)
    ignored_files = int(scan.get("ignored_file_count") or 0)
    ignored_directories = int(scan.get("ignored_directory_count") or 0)
    inventory_complete_globally = (
        not inventory_truncated
        and inventory_error_count == 0
        and depth_limited == 0
        and ignored_files == 0
        and ignored_directories == 0
    )

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
        def parse_is_complete(path):
            coverage = documents.get(path, {}).get("coverage") or {}
            return bool(coverage.get("parse_complete", coverage.get("complete", False)))

        def semantic_is_complete(path):
            document = documents.get(path, {})
            coverage = document.get("coverage") or {}
            if document.get("sidecar_projection") or coverage.get("semantic_projection"):
                return False
            return bool(coverage.get("semantic_complete", coverage.get("complete", False)))

        parse_complete_paths = {path for path in parsed if parse_is_complete(path)}
        semantic_complete_paths = {path for path in parsed if semantic_is_complete(path)}
        complete_text = len(parse_complete_paths)
        sampled = sum(
            1 for path in parsed
            if documents.get(path, {}).get("sidecar_projection")
            or (documents.get(path, {}).get("coverage") or {}).get("semantic_projection")
            or (documents.get(path, {}).get("coverage") or {}).get("overview_sampled")
        )
        parse_partial = len(parsed - parse_complete_paths)
        semantic_partial = len(parsed - semantic_complete_paths)
        deep_analyzed = len(semantic_complete_paths)
        inventory_complete = inventory_complete_globally
        parse_complete = bool(selected) and not pending and not failed and parse_partial == 0
        semantic_complete = (
            inventory_complete and parse_complete and bool(selected)
            and semantic_partial == 0
        )
        complete_analysis = semantic_complete
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
        if semantic_complete:
            coverage_level = "full_text_analysis"
        elif parsed:
            coverage_level = "representative_overview"
        else:
            coverage_level = "inventory_complete" if inventory_complete else "inventory_partial"
        status = "完整" if complete_analysis else "部分覆盖"
        if not parsed and selected:
            status = "待分析"
        limitations = []
        if inventory_truncated:
            limitations.append("扫描达到文件数量上限；清单之外仍可能存在文件，inventory_coverage 不完整。")
        if depth_limited:
            limitations.append("{} 个目录达到深度上限，未继续扫描其后代。".format(depth_limited))
        if inventory_error_count:
            limitations.append("扫描期间发生 {} 个读取错误，清单覆盖率不能视为完整。".format(inventory_error_count))
        if ignored_files or ignored_directories:
            limitations.append(
                "显式扫描排除规则跳过 {} 个文件、{} 个目录；这些对象不在清单基数内，inventory_coverage 不完整。".format(
                    ignored_files, ignored_directories
                )
            )
        if (policy or {}).get("enabled") and pending:
            limitations.append("大数据包采用有限批次持续处理；仍有文件待进入后续批次。")
        if sampled:
            limitations.append("{} 个文件当前为抽样/首轮概览，不能视为全文深度分析。".format(sampled))
        if parse_partial:
            limitations.append("{} 个已解析文件本身存在截断、快速模式或其他解析不完整。".format(parse_partial))
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
        inventory_coverage = {
            "complete": inventory_complete,
            "enumerated_files": len(selected),
            "enumerated_bytes": total_bytes,
            "scan_truncated": inventory_truncated,
            "scan_error_count": inventory_error_count,
            "depth_limited_directory_count": depth_limited,
            "ignored_file_count": ignored_files,
            "ignored_directory_count": ignored_directories,
            "symlink_count": int(scan.get("symlink_count", scan.get("skipped_symlink_count", 0)) or 0),
            "symlink_policy": "inventory_entry_only_target_not_followed",
            "max_files": scan.get("max_files"),
            "max_depth": scan.get("max_depth"),
        }
        parse_coverage = {
            "complete": parse_complete,
            "inventory_files": len(selected),
            "parsed_files": len(parsed),
            "complete_files": len(parse_complete_paths),
            "partial_files": parse_partial,
            "pending_files": len(pending),
            "failed_files": len(failed),
            "parsed_bytes": parsed_bytes,
            "parsed_file_ratio": round(len(parsed) / float(len(selected) or 1), 6),
            "parsed_byte_ratio": round(parsed_bytes / float(total_bytes or 1), 6),
        }
        semantic_analysis_coverage = {
            "complete": semantic_complete,
            "full_text_files": len(semantic_complete_paths),
            "projected_or_partial_files": semantic_partial,
            "projected_files": sampled,
            "analyzed_files": len(parsed),
            "full_text_file_ratio": round(len(semantic_complete_paths) / float(len(selected) or 1), 6),
            "projection_character_limit": (policy or {}).get("overview_chars_per_file"),
            "projection_evidence_limit": (policy or {}).get("overview_evidence_per_file"),
        }
        content_parse_ratio = round(len(parsed) / float(len(selected) or 1), 6)
        deep_analysis_ratio = round(len(semantic_complete_paths) / float(len(selected) or 1), 6)
        batch_size = max(1, int((policy or {}).get("batch_files") or 100))
        remaining_batches = int(math.ceil((len(pending) + len(failed)) / float(batch_size)))
        extension_inventory = Counter(
            str(files[path].get("extension") or Path(path).suffix.lower() or "[无扩展名]")
            for path in selected
        )
        extension_parsed = Counter(
            str(files[path].get("extension") or Path(path).suffix.lower() or "[无扩展名]")
            for path in parsed
        )
        format_coverage = []
        for extension, count in sorted(extension_inventory.items(), key=lambda item: (-item[1], item[0])):
            parsed_count = extension_parsed.get(extension, 0)
            format_coverage.append({
                "extension": extension,
                "inventory_files": count,
                "parsed_files": parsed_count,
                "coverage_ratio": round(parsed_count / float(count or 1), 6),
            })
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
            "partial_text_files": parse_partial,
            "failed_files": len(failed),
            "pending_files": len(pending),
            "parsed_file_ratio": content_parse_ratio,
            "coverage_ratio": content_parse_ratio,
            "inventory_coverage_ratio": 1.0 if inventory_complete else None,
            "content_parse_ratio": content_parse_ratio,
            "deep_analysis_ratio": deep_analysis_ratio,
            "inventory_bytes": total_bytes,
            "scanned_bytes": total_bytes,
            "inventory_size_human": human_size(total_bytes),
            "parsed_bytes": parsed_bytes,
            "parsed_byte_ratio": round(parsed_bytes / float(total_bytes or 1), 6),
            "complete_analysis": complete_analysis,
            "full_text_analysis": semantic_complete,
            "inventory_coverage": inventory_coverage,
            "parse_coverage": parse_coverage,
            "semantic_analysis_coverage": semantic_analysis_coverage,
            "coverage_contract": {
                "inventory": "文件和目录清点覆盖；只有未截断、无扫描错误、无显式排除且未触及深度上限时才是100%。符号链接登记自身但不跟随目标。",
                "content_parse": "成功生成统一解析结果的文件占清点文件比例，允许快速模式或截断结果。",
                "deep_analysis": "完成全文语义分析且不是有界投影的文件占清点文件比例。",
            },
            "coverage_level": coverage_level,
            "coverage_level_label": {
                "inventory_complete": "全量盘点",
                "inventory_partial": "不完整盘点",
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
            "format_coverage": format_coverage,
            "batch_progress": {
                "batch_size": batch_size,
                "estimated_remaining_batches": remaining_batches,
                "checkpoint_resume": bool((policy or {}).get("checkpoint_resume", True)),
                "eta_seconds": None,
                "eta_note": "尚无稳定的同类文件吞吐基线，完成首个批次后再按格式估算 ETA。" if remaining_batches else "已无待处理批次。",
            },
            "large_package_notice": (
                "大数据包按有限批次处理并逐文件保存检查点；可安全停止并续跑。首轮概览不等同于全文深度分析。"
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
        "summary": "该分支包含 {} 个尚未进入内容分析的文件，将在后续批次继续处理。".format(len(paths)),
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
