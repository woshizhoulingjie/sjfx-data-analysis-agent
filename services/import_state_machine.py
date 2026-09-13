"""Canonical state and artifact contract for newly imported packages.

The project still understands legacy jobs, but a new import must progress only
through this state machine. Keeping the contract independent from Flask and
SQLite makes it usable by the API, worker and storage layers without circular
imports.
"""

from __future__ import annotations

from typing import Dict, FrozenSet


CREATED = "created"
SCANNING = "scanning"
AWAITING_SELECTION = "waiting_for_selection"
PARSING_SELECTED = "parsing_selected"
PRELIMINARY_FILES = "preliminary_summarizing"
PARSED_OVERVIEW = "parsed_overview"
PRELIMINARY_NODES = "preliminary_nodes"
PRELIMINARY_OVERVIEW = "preliminary_overview"
DEEP_FILES = "deep_summarizing_files"
DEEP_NODES = "deep_summarizing_nodes"
DEEP_UPDATE_AVAILABLE = "deep_update_available"
DEEP_OVERVIEW = "deep_overview_updating"
COMPLETED = "completed"
PARTIAL = "partial"
PAUSED = "paused"
FAILED = "failed"
CANCELLED = "cancelled"


STRICT_IMPORT_STATES: FrozenSet[str] = frozenset({
    CREATED,
    SCANNING,
    AWAITING_SELECTION,
    PARSING_SELECTED,
    PRELIMINARY_FILES,
    PARSED_OVERVIEW,
    PRELIMINARY_NODES,
    PRELIMINARY_OVERVIEW,
    DEEP_FILES,
    DEEP_NODES,
    DEEP_OVERVIEW,
    DEEP_UPDATE_AVAILABLE,
    COMPLETED,
    PARTIAL,
    PAUSED,
    FAILED,
    CANCELLED,
})


ALLOWED_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    CREATED: frozenset({SCANNING, FAILED, CANCELLED}),
    SCANNING: frozenset({AWAITING_SELECTION, PAUSED, FAILED, CANCELLED}),
    AWAITING_SELECTION: frozenset({PARSING_SELECTED, PAUSED, CANCELLED}),
    PARSING_SELECTED: frozenset({PARSED_OVERVIEW, PAUSED, FAILED, CANCELLED}),
    PARSED_OVERVIEW: frozenset({PRELIMINARY_FILES, PAUSED, FAILED, CANCELLED}),
    PRELIMINARY_FILES: frozenset({PRELIMINARY_NODES, PAUSED, FAILED, CANCELLED}),
    PRELIMINARY_NODES: frozenset({PRELIMINARY_OVERVIEW, PAUSED, FAILED, CANCELLED}),
    PRELIMINARY_OVERVIEW: frozenset({PARSING_SELECTED, DEEP_FILES, PAUSED, FAILED, CANCELLED}),
    DEEP_FILES: frozenset({PARSING_SELECTED, DEEP_NODES, DEEP_OVERVIEW, DEEP_UPDATE_AVAILABLE, PAUSED, PARTIAL, FAILED, CANCELLED}),
    DEEP_NODES: frozenset({PARSING_SELECTED, DEEP_OVERVIEW, DEEP_UPDATE_AVAILABLE, PAUSED, PARTIAL, FAILED, CANCELLED}),
    DEEP_UPDATE_AVAILABLE: frozenset({PARSING_SELECTED, DEEP_OVERVIEW, DEEP_FILES, PAUSED, PARTIAL, FAILED, CANCELLED}),
    DEEP_OVERVIEW: frozenset({PARSING_SELECTED, DEEP_FILES, DEEP_NODES, DEEP_UPDATE_AVAILABLE, COMPLETED, PARTIAL, PAUSED, FAILED, CANCELLED}),
    PAUSED: frozenset({PARSING_SELECTED, PRELIMINARY_FILES, PRELIMINARY_NODES,
                       PRELIMINARY_OVERVIEW, DEEP_FILES, DEEP_NODES, DEEP_UPDATE_AVAILABLE,
                       DEEP_OVERVIEW, CANCELLED}),
    COMPLETED: frozenset({PARSING_SELECTED, DEEP_OVERVIEW}),
    PARTIAL: frozenset({PARSING_SELECTED, DEEP_FILES, DEEP_NODES, DEEP_OVERVIEW, COMPLETED}),
    FAILED: frozenset({PARSING_SELECTED, PRELIMINARY_FILES, DEEP_FILES, CANCELLED}),
    CANCELLED: frozenset(),
}


SUMMARY_TYPES = {
    ("preliminary", "file"): "preliminary_file_summary",
    ("preliminary", "node"): "preliminary_node_summary",
    ("deep", "file"): "deep_file_summary",
    ("deep", "node"): "deep_node_summary",
}


def summary_type(stage: str, kind: str) -> str:
    """Return the durable summary key for one immutable workflow product."""
    key = (str(stage or "").lower(), str(kind or "").lower())
    try:
        return SUMMARY_TYPES[key]
    except KeyError as exc:
        raise ValueError("unknown import summary stage: {}:{}".format(*key)) from exc


def can_transition(current: str, target: str) -> bool:
    """Check a transition without allowing a later stage to skip earlier work."""
    current = str(current or CREATED)
    target = str(target or current)
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def processing_state(stage: str) -> str:
    """Map detailed task state to the durable package-processing indicator."""
    mapping = {
        CREATED: "running",
        PARSED_OVERVIEW: "parsed_overview",
        SCANNING: "running",
        AWAITING_SELECTION: "awaiting_selection",
        PARSING_SELECTED: "parsing_selected",
        PRELIMINARY_FILES: "preliminary_summarizing",
        PRELIMINARY_NODES: "preliminary_nodes",
        PRELIMINARY_OVERVIEW: "preliminary_overview",
        DEEP_FILES: "deep_summarizing_files",
        DEEP_NODES: "deep_summarizing_nodes",
        DEEP_OVERVIEW: "deep_overview_updating",
        COMPLETED: "completed",
        DEEP_UPDATE_AVAILABLE: "deep_update_available",
        PARTIAL: "partial",
        PAUSED: "paused",
        FAILED: "failed",
        CANCELLED: "cancelled",
    }
    return mapping.get(str(stage or ""), "running")


def deep_report_outcome(task, report):
    """A published current-scope report closes the selected import honestly."""
    checkpoint = task.get("checkpoint") or {}
    selected = set(checkpoint.get("selected_paths") or [])
    scope = report.get("report_scope") or {}
    if not selected or selected != set(scope.get("selected_paths") or []):
        return None
    if int(checkpoint.get("selection_version") or 0) != int(scope.get("selection_version") or 0):
        return None
    pipeline = (report.get("coverage") or {}).get("pipeline_coverage") or {}
    quality = report.get("summary_quality") or {}
    complete = bool((pipeline.get("content_parse") or {}).get("complete")
                    and (pipeline.get("deep_analysis") or {}).get("complete")
                    and quality.get("stage") == "deep"
                    and quality.get("file_summaries") == len(selected)
                    and not quality.get("degraded_file_summaries")
                    and not quality.get("degraded_node_summaries")
                    and (report.get("model_analysis") or {}).get("status") == "completed")
    return COMPLETED if complete else PARTIAL
