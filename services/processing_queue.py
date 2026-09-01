"""Deterministic scheduling helpers for resumable full-package processing.

The inventory remains authoritative.  These helpers only decide which eligible
logical files should move to the front of the next bounded deep-analysis batch.
"""

import math
import time
from collections import defaultdict


TERMINAL_EXCLUSION_REASONS = {
    "restricted_or_sensitive",
    "empty_file",
    "exact_duplicate_non_primary",
    "cache_temporary_or_dependency_file",
    "out_of_scope_media",
}

SOURCE_PRIORITY = {
    "manual_selection": 600,
    "user_query": 550,
    "user_intent": 550,
    "relationship_recall": 500,
    "question_promotion": 480,
    "initial_overview": 300,
    "background_backfill": 100,
}

HEAVY_MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".mp4", ".mov", ".mkv", ".avi",
}


def estimated_work_units(path, inventory_item=None, workflow_state=None):
    """Estimate a conservative per-file share of one 500-unit logical batch."""
    inventory_item = inventory_item or {}
    workflow_state = workflow_state or {}
    size = max(0, int(inventory_item.get("size") or 0))
    fallback_extension = (
        "." + str(path).rsplit("/", 1)[-1].rsplit(".", 1)[-1]
        if "." in str(path).rsplit("/", 1)[-1] else ""
    )
    extension = str(inventory_item.get("extension") or fallback_extension).lower()
    if extension and not extension.startswith("."):
        extension = "." + extension
    units = max(1, int(math.ceil(size / float(20 * 1024 * 1024))))
    if extension in HEAVY_MEDIA_EXTENSIONS:
        units = max(units, 20)
    elif bool(workflow_state.get("ocr_candidate")):
        units = max(units, 8)
    return min(500, units)


def _reason_codes(state):
    return {
        str(value or "").strip()
        for value in (state or {}).get("reasons") or []
        if str(value or "").strip()
    }


def deep_processing_eligible(workflow_state, analysis_state=None, now=None):
    """Return whether one inventory entry still belongs to the logical queue."""
    workflow_state = workflow_state or {}
    analysis_state = analysis_state or {}
    now = time.time() if now is None else float(now)
    if str(analysis_state.get("status") or "") == "completed":
        return False
    if str(analysis_state.get("status") or "") == "needs_attention":
        return False
    if not bool(workflow_state.get("promotion_allowed", True)):
        return False
    if _reason_codes(workflow_state) & TERMINAL_EXCLUSION_REASONS:
        return False
    safety = str(workflow_state.get("safety_status") or "unknown")
    if safety in {"restricted", "rejected"}:
        return False
    if str(analysis_state.get("status") or "") == "failed":
        if not bool(analysis_state.get("retryable")):
            return False
        if int(analysis_state.get("attempt_count") or 0) >= 6:
            return False
        if float(analysis_state.get("next_retry_at") or 0) > now:
            return False
    return True


def ranked_pending_paths(
    inventory_paths,
    workflow_states,
    analysis_states,
    *,
    exclude_paths=None,
    preferred_paths=None,
    limit=500,
    workload_limit=500,
    now=None,
):
    """Choose the next logical batch without starving ordinary files forever."""
    limit = max(1, min(500, int(limit or 500)))
    workload_limit = max(1, min(5000, int(workload_limit or 500)))
    inventory = inventory_paths if isinstance(inventory_paths, dict) else {}
    paths = inventory.keys() if inventory else inventory_paths
    excluded = {str(path) for path in (exclude_paths or []) if path}
    preferred_order = {
        str(path): index for index, path in enumerate(preferred_paths or []) if path
    }
    rows = []
    for path in sorted({str(path) for path in paths if path}):
        if path in excluded:
            continue
        workflow = workflow_states.get(path) or {}
        state = analysis_states.get(path) or {}
        if not deep_processing_eligible(workflow, state, now=now):
            continue
        preferred = path in preferred_order
        source = str(workflow.get("priority_source") or "")
        selection = str(workflow.get("selection_state") or "")
        score = float(workflow.get("selection_score") or 0.0)
        rows.append((
            0 if preferred else 1,
            preferred_order.get(path, 10**9),
            -SOURCE_PRIORITY.get(source, 0),
            0 if selection == "priority" else 1,
            -score,
            str(workflow.get("updated_at") or ""),
            path,
        ))
    rows.sort()
    selected = []
    used_units = 0
    for row in rows:
        path = row[-1]
        units = estimated_work_units(path, inventory.get(path), workflow_states.get(path))
        if selected and used_units + units > workload_limit:
            continue
        selected.append(path)
        used_units += units
        if len(selected) >= limit or used_units >= workload_limit:
            break
    return selected


def relationship_recall_paths(content_map, completed_paths, eligible_paths, limit=500):
    """Rank unprocessed neighbours of completed files with auditable reasons."""
    completed = {str(path) for path in (completed_paths or []) if path}
    eligible = {str(path) for path in (eligible_paths or []) if path}
    scores = defaultdict(float)
    reasons = defaultdict(list)
    for relation in (content_map or {}).get("relationships") or []:
        left = str(relation.get("source") or "")
        right = str(relation.get("target") or "")
        weight = max(1.0, float(relation.get("weight") or 1.0))
        candidate = None
        if left in completed and right in eligible:
            candidate = right
        elif right in completed and left in eligible:
            candidate = left
        if not candidate:
            continue
        scores[candidate] += weight
        for reason in relation.get("reasons") or []:
            text = "{}:{}".format(reason.get("kind") or "relation", reason.get("value") or "")
            if text not in reasons[candidate] and len(reasons[candidate]) < 8:
                reasons[candidate].append(text)
    ordered = sorted(scores, key=lambda path: (-scores[path], path))[:max(1, min(5000, int(limit or 500)))]
    return [
        {"path": path, "score": round(scores[path], 6), "reasons": reasons[path]}
        for path in ordered
    ]


__all__ = [
    "deep_processing_eligible",
    "estimated_work_units",
    "ranked_pending_paths",
    "relationship_recall_paths",
]
