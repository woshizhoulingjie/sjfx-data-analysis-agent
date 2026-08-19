"""Small compatibility normalizers for JSON payloads persisted by older demos."""


def normalize_summary(payload):
    if not isinstance(payload, dict):
        return payload
    result = dict(payload)
    result.setdefault("schema_version", 1)
    if not result.get("summary") and result.get("core_summary"):
        result["summary"] = result["core_summary"]
    if not result.get("core_summary") and result.get("summary"):
        result["core_summary"] = result["summary"]
    if "evidence_chain" not in result and isinstance(result.get("evidence"), list):
        result["evidence_chain"] = result["evidence"]
    if "evidence" not in result and isinstance(result.get("evidence_chain"), list):
        result["evidence"] = result["evidence_chain"]
    if not isinstance(result.get("topics"), list):
        result["topics"] = []
    return normalize_conclusion_evidence(result)



def _normalize_evidence_item(item, default_type="direct"):
    if not isinstance(item, dict):
        return item
    value = dict(item)
    value.setdefault("evidence_type", default_type)
    value.setdefault("verification_status", "待核验" if default_type != "direct" else "可回查")
    value.setdefault("location", {
        key: value.get(key) for key in ("source_path", "page", "paragraph", "section", "table", "row", "bbox", "archive_member") if value.get(key) is not None
    })
    return value


def normalize_conclusion_evidence(payload):
    if not isinstance(payload, dict):
        return payload
    result = dict(payload)
    conclusions = []
    for item in result.get("conclusion_evidence") or []:
        if not isinstance(item, dict):
            continue
        conclusion = dict(item)
        conclusion.setdefault("statement", conclusion.get("conclusion") or "分析结论")
        conclusion.setdefault("confidence", "待核验")
        default_type = "inference" if str(conclusion.get("type") or "").lower() in {"推论", "inference"} else "direct"
        conclusion["evidence"] = [_normalize_evidence_item(e, default_type) for e in conclusion.get("evidence", []) if isinstance(e, dict)]
        if not conclusion["evidence"]:
            conclusion["verification_status"] = "待补证据"
        conclusions.append(conclusion)
    result["conclusion_evidence"] = conclusions
    return result
