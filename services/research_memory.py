"""Maintain compact cumulative research state instead of raw chat history."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence


def _merge_scalars(previous: Iterable[Any], current: Iterable[Any], limit: int) -> List[str]:
    output = []
    for item in list(previous or []) + list(current or []):
        value = str(item or "").strip()
        if value and value not in output:
            output.append(value)
    return output[-max(1, limit) :]


def _merge_records(
    previous: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
    limit: int,
) -> List[Dict[str, Any]]:
    output = []
    positions = {}
    for raw in list(previous or []) + list(current or []):
        item = dict(raw or {})
        identity = tuple(str(item.get(key) or "").strip() for key in keys)
        if not any(identity):
            continue
        if identity in positions:
            position = positions[identity]
            merged = dict(output[position])
            merged.update(item)
            merged["evidence_ids"] = _merge_scalars(
                output[position].get("evidence_ids") or [],
                item.get("evidence_ids") or [],
                50,
            )
            output[position] = merged
        else:
            positions[identity] = len(output)
            output.append(item)
    return output[-max(1, limit) :]


def update_research_memory(
    previous: Mapping[str, Any],
    plan: Mapping[str, Any],
    turn_result: Mapping[str, Any],
    verification: Mapping[str, Any],
    turn_id: str,
    tool_results: Mapping[str, Any] = None,
) -> Dict[str, Any]:
    memory = dict(previous or {})
    ledger = dict((verification or {}).get("ledger") or {})
    supported = [
        {
            "claim_id": item.get("claim_id"),
            "text": item.get("text"),
            "evidence_ids": list(item.get("evidence_ids") or []),
            "turn_id": str(turn_id),
        }
        for item in ledger.get("claims") or []
        if item.get("status") == "supported"
    ]
    citations = list((turn_result or {}).get("citations") or [])
    tool_results = dict(tool_results or {})
    contradictions = list((tool_results.get("contradiction_detector") or {}).get("items") or [])
    counter_evidence = list((tool_results.get("counter_evidence_search") or {}).get("items") or [])
    objective_record = {
        "turn_id": str(turn_id),
        "objective": str((plan or {}).get("objective") or ""),
        "modes": list((plan or {}).get("modes") or []),
        "scope": dict((plan or {}).get("scope") or {}),
    }
    source_paths = [
        str(item.get("source_path")) for item in citations if item.get("source_path")
    ]
    evidence_ids = [
        str(item.get("evidence_id")) for item in citations if item.get("evidence_id")
    ]
    open_questions = list((turn_result or {}).get("warnings") or [])
    memory.update(
        {
            "schema_version": "research-memory/2.0",
            "current_scope": dict((plan or {}).get("scope") or {}),
            "current_objective": str((plan or {}).get("objective") or ""),
            "analysis_modes": _merge_scalars(
                memory.get("analysis_modes") or [], (plan or {}).get("modes") or [], 20
            ),
            "user_constraints": _merge_scalars(
                memory.get("user_constraints") or [],
                (plan or {}).get("constraints") or [],
                40,
            ),
            "objectives": _merge_records(
                memory.get("objectives") or [],
                [objective_record],
                ("turn_id",),
                50,
            ),
            "confirmed_claims": _merge_records(
                memory.get("confirmed_claims") or [],
                supported,
                ("claim_id", "text"),
                200,
            ),
            "evidence_ids": _merge_scalars(
                memory.get("evidence_ids") or [], evidence_ids, 500
            ),
            "source_paths": _merge_scalars(
                memory.get("source_paths") or [], source_paths, 500
            ),
            "open_questions": _merge_scalars(
                memory.get("open_questions") or [], open_questions, 100
            ),
            "contradictions": _merge_records(
                memory.get("contradictions") or [],
                contradictions,
                ("reason",),
                100,
            ),
            "counter_evidence": _merge_records(
                memory.get("counter_evidence") or [],
                counter_evidence,
                ("evidence_id", "statement"),
                100,
            ),
            "turn_ids": _merge_scalars(
                memory.get("turn_ids") or [], [str(turn_id)], 100
            ),
            "last_turn_id": str(turn_id),
        }
    )
    return memory
