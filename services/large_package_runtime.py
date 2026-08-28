"""Bounded, checkpointed large-package preview execution.

This stateful stage is kept separate from semantic package analysis so its
I/O, resume and yield behaviour can evolve without growing analyze_package().
"""

import time

from config import Config
from services.package_exploration import (
    CONTENT_MAP_SCHEMA,
    PREVIEW_SCHEMA,
    PreviewBudget,
    PreviewSliceYield,
    build_content_map,
    preview_as_document,
    preview_file,
)
from services.parse_isolation import ParseIsolationCancelled
from services.retrieval import evidence_corpus


def _preview_matches_inventory(preview, file_node):
    if not preview or preview.get("status") not in {"previewed", "restricted"}:
        return False
    return (
        int(preview.get("size") or -1) == int(file_node.get("size") or 0)
        and int(preview.get("modified_at_ns") or -1)
        == int(file_node.get("modified_at_ns") or 0)
    )


def explore_large_package(
    scan_id,
    scan,
    files,
    storage,
    policy,
    deep_paths=None,
    progress=None,
    cancel_check=None,
    yield_check=None,
    preview_file_func=preview_file,
):
    """Persist one bounded preview per inventory entry and build a content map."""
    progress = progress or (lambda percent, message: None)
    deep_paths = set(deep_paths or [])
    existing = {
        item["path"]: item for item in storage.iter_file_preview_states(scan_id)
    }
    previous_map = storage.get_content_map(scan_id)
    previous_map_stale = bool(
        previous_map and previous_map.get("schema_version") != CONTENT_MAP_SCHEMA
    )
    if not previous_map_stale and existing:
        first_path = min(existing)
        first_preview = storage.get_file_preview(scan_id, first_path) or {}
        previous_map_stale = bool(
            first_preview and first_preview.get("schema_version") != PREVIEW_SCHEMA
        )
    if previous_map_stale:
        existing = {}
        previous_map = None
    previous_run = (previous_map or {}).get("run") or {}
    previous_budget_consumed = max(
        0, int(previous_run.get("budget_consumed_bytes") or 0)
    )
    budget = PreviewBudget(
        policy.get("preview_total_bytes"),
        consumed_bytes=previous_budget_consumed,
    )
    pending_previews = []
    pending_documents = []
    pending_states = []
    reused = 0
    previewed = 0
    total = max(1, len(files))
    batch_size = max(1, min(500, int(policy.get("batch_files") or 200)))
    slice_files = max(
        1, int(getattr(Config, "LARGE_PACKAGE_PREVIEW_SLICE_FILES", 100))
    )
    slice_seconds = max(
        5, int(getattr(Config, "LARGE_PACKAGE_PREVIEW_SLICE_SECONDS", 30))
    )
    slice_started = time.monotonic()
    start_index = max(
        0, int(((previous_map or {}).get("run") or {}).get("next_index") or 0)
    )

    def flush():
        nonlocal pending_previews, pending_documents, pending_states
        storage.save_exploration_batch(
            scan_id,
            pending_previews,
            [],
            pending_states,
            evidence_by_path=[
                (path, evidence_corpus({path: document}))
                for path, document in pending_documents
            ],
            remove_document_paths=[path for path, _document in pending_documents],
        )
        pending_previews = []
        pending_documents = []
        pending_states = []

    def save_slice_checkpoint(next_index, reason):
        checkpoint = {
            "schema_version": CONTENT_MAP_SCHEMA,
            "status": "previewing",
            "representative_paths": [],
            "run": {
                "next_index": int(next_index),
                "new_previews": previewed,
                "reused_previews": reused,
                "budget_consumed_bytes": budget.consumed_bytes,
                "budget_consumed_in_slice": max(
                    0, budget.consumed_bytes - previous_budget_consumed
                ),
                "slice_incomplete": True,
                "yield_reason": reason,
            },
        }
        storage.save_content_map(scan_id, checkpoint)
        return checkpoint

    for index, file_node in enumerate(files[start_index:], start_index + 1):
        if cancel_check is not None and cancel_check():
            flush()
            raise ParseIsolationCancelled("任务已取消，已保存完成的轻量预览检查点")
        path = file_node.get("path")
        prior = existing.get(path)
        preserve_deep = path in deep_paths and _preview_matches_inventory(
            prior, file_node
        )
        if _preview_matches_inventory(prior, file_node):
            reused += 1
        else:
            try:
                preview = preview_file_func(
                    scan.get("root"),
                    file_node,
                    per_file_bytes=policy.get("preview_bytes_per_file"),
                    budget=budget,
                    zip_member_limit=policy.get("preview_zip_members"),
                    zip_member_bytes=policy.get("preview_zip_member_bytes"),
                    cancel_check=cancel_check,
                    yield_check=yield_check,
                )
            except PreviewSliceYield:
                flush()
                return save_slice_checkpoint(index - 1, "higher_priority_job")
            pending_previews.append((path, preview))
            existing[path] = preview
            previewed += 1
            if (
                not preserve_deep
                and preview.get("status") in {"previewed", "restricted"}
            ):
                document = preview_as_document(preview)
                pending_documents.append((path, document))
                pending_states.append(
                    (
                        path,
                        "preview:{}".format(
                            preview.get("preview_fingerprint")
                            or preview.get("sample_sha256")
                            or ""
                        ),
                        "previewed",
                        document,
                        None,
                    )
                )
            elif (
                preview.get("status") in {"failed", "deferred"}
                and not preserve_deep
            ):
                pending_states.append(
                    (
                        path,
                        "preview:{}".format(
                            preview.get("preview_fingerprint")
                            or preview.get("sample_sha256")
                            or ""
                        ),
                        "preview_{}".format(preview.get("status")),
                        None,
                        "; ".join(preview.get("warnings") or []) or None,
                    )
                )
        if index % batch_size == 0:
            flush()
            progress(
                2 + int(18 * index / total),
                "全量有界轻量预览：{}/{}（复用 {}）".format(
                    index, len(files), reused
                ),
            )
        if (
            previewed >= slice_files
            or time.monotonic() - slice_started >= slice_seconds
            or (yield_check is not None and yield_check())
        ) and index < len(files):
            flush()
            return save_slice_checkpoint(index, "slice_budget")
    flush()
    progress(20, "轻量预览完成，正在发现主题、重复候选与文件关系")
    content_map = build_content_map(
        (item["payload"] for item in storage.iter_file_previews(scan_id)),
        representative_limit=policy.get("initial_parse_files"),
    )
    selection_decisions = content_map.pop("selection_decisions", [])
    storage.save_file_workflow_states(scan_id, selection_decisions)
    for duplicate in content_map.get("duplicates") or []:
        if duplicate.get("kind") != "exact_sha256":
            continue
        canonical = duplicate.get("canonical_path")
        for alias in duplicate.get("paths") or []:
            if alias != canonical:
                storage.replace_document_evidence_index(
                    scan_id, alias, [], preserve_translations=True
                )
    content_map["policy"] = {
        "preview_bytes_per_file": policy.get("preview_bytes_per_file"),
        "preview_total_bytes": policy.get("preview_total_bytes"),
        "representative_limit": policy.get("initial_parse_files"),
        "selection_basis": "主题覆盖、格式/目录/语言覆盖、信息量、独特性与关系价值",
    }
    content_map["run"] = {
        "new_previews": previewed,
        "reused_previews": reused,
        "budget_consumed_bytes": budget.consumed_bytes,
        "budget_consumed_in_slice": max(
            0, budget.consumed_bytes - previous_budget_consumed
        ),
        "budget_exhausted": budget.exhausted,
    }
    preview_counts = storage.file_preview_counts(scan_id)
    content_map["run"]["preview_status_counts"] = preview_counts
    content_map["run"]["deferred_files"] = int(
        preview_counts.get("deferred") or 0
    )
    storage.save_content_map(scan_id, content_map)
    return content_map


__all__ = ["explore_large_package"]
