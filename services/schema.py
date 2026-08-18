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
    return result

