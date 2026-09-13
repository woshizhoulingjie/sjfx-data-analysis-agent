"""Conservative evidence checks for generated mail claims and relations."""

from __future__ import annotations

import re


BUSINESS_PREDICATES = {
    "proposed", "confirmed", "modified", "cancelled", "supports", "contradicts",
}


def _anchor(value):
    """Keep letters and numbers from multilingual labels for a literal evidence check."""
    return "".join(re.findall(r"[^\W_]", str(value or ""), flags=re.UNICODE)).casefold()


def _appears_in_evidence(value, quotes):
    value = _anchor(value)
    if len(value) < 2:
        return False
    return any(value in _anchor(quote) for quote in quotes)


def verify_claim(claim, evidence):
    claim = str(claim or "").strip()
    quotes = [str(item.get("quote_text") or item.get("text") or "") for item in evidence or []]
    if not claim or not quotes:
        return {"state": "insufficient", "support_status": "missing_evidence", "reason": "缺少可回查的邮件或附件证据"}
    normalized = claim.casefold()
    supported = any(normalized in quote.casefold() for quote in quotes)
    return {"state": "asserted" if supported else "insufficient", "support_status": "supported" if supported else "not_entailing", "reason": None if supported else "证据摘录不足以逐字支持该主张"}


def verify_business_relation(candidate, evidence_by_id):
    """Accept only a bounded relation whose entities can be traced to supplied evidence.

    An Agent may classify multilingual wording, but it cannot attach a relation
    to evidence outside the retrieved set or invent unanchored entities.
    """
    candidate = candidate if isinstance(candidate, dict) else {}
    predicate = str(candidate.get("predicate") or "").strip().casefold()
    subject_ref = str(candidate.get("subject_ref") or "").strip()[:240]
    object_ref = str(candidate.get("object_ref") or "").strip()[:500]
    claim_text = str(candidate.get("claim_text") or "").strip()[:1000]
    evidence_ids = []
    for item in candidate.get("evidence_ids") or []:
        item = str(item or "").strip()
        if item and item in evidence_by_id and item not in evidence_ids:
            evidence_ids.append(item)
        if len(evidence_ids) == 8:
            break
    quotes = [str(evidence_by_id[item].get("quote_text") or "") for item in evidence_ids]
    if predicate not in BUSINESS_PREDICATES:
        return {"state": "insufficient", "support_status": "invalid_predicate", "reason": "关系类型不在允许列表", "evidence_ids": evidence_ids}
    if not subject_ref or not object_ref or not claim_text:
        return {"state": "insufficient", "support_status": "invalid_relation", "reason": "关系缺少主体、事项或主张", "evidence_ids": evidence_ids}
    if not quotes:
        return {"state": "insufficient", "support_status": "missing_evidence", "reason": "关系没有可回查证据", "evidence_ids": evidence_ids}
    if not _appears_in_evidence(subject_ref, quotes) or not _appears_in_evidence(object_ref, quotes):
        return {"state": "insufficient", "support_status": "unanchored_entity", "reason": "主体或事项未在引用证据中出现", "evidence_ids": evidence_ids}
    from config import Config
    if Config.MAIL_ENABLE_AGENT_MINING:
        quote="\n".join(quotes)
        if not verify_numeric(claim_text,quote) or not verify_temporal(claim_text,quote) or not verify_polarity(claim_text,quote):
            return {"state":"insufficient","support_status":"numeric_temporal_or_polarity_mismatch","reason":"数值、时间、否定或条件未与原文一致","evidence_ids":evidence_ids}
    requested_state = str(candidate.get("state") or "asserted").strip().casefold()
    if requested_state not in {"asserted", "inferred", "conflicting"}:
        requested_state = "asserted"
    return {
        "state": requested_state,
        "support_status": "supported",
        "reason": None,
        "evidence_ids": evidence_ids,
    }


def verify_numeric(claim_value,evidence_quote):
    from ..facts import ascii_digits
    def numbers(text):
        return {v.replace(",", "") for v in re.findall(r"[+-]?\d+(?:,\d{3})*(?:\.\d+)?",ascii_digits(text))}
    claimed=numbers(claim_value);present=numbers(evidence_quote)
    if not claimed.issubset(present):return False
    # Explicit magnitude/currency words cannot disappear from a normalized numeric claim.
    from ..facts import normalize_amount
    a,b=normalize_amount(claim_value),normalize_amount(evidence_quote)
    if a and b and a["currency"] and b["currency"]:
        return a==b
    return True


def verify_temporal(claim_date,evidence_quote):
    from ..facts import normalize_date
    claimed=normalize_date(claim_date)
    return claimed is None or claimed==normalize_date(evidence_quote) or str(claim_date) in str(evidence_quote)


def verify_polarity(claim_text,evidence_quote):
    negative=r"取消|撤回|未确认|不予|不得|暂缓|尚未|cancel(?:led)?|not confirmed|withdrawn|ยกเลิก|ไม่อนุมัติ"
    conditional=r"如果|若|除非|前提|待批准|subject to|provided that|conditional|หาก|เงื่อนไข"
    for pattern in (negative,conditional):
        if re.search(pattern,evidence_quote,re.I) and not re.search(pattern,claim_text,re.I):return False
    return True


def verify_conditions(claim,evidence):
    valid=verify_polarity(str(claim),str(evidence))
    return {"supported":valid,"reason":None if valid else "原文否定或条件未完整保留"}


def verify_translated_finding(text, quote):
    """Allow only deterministic calendar normalization alongside source numbers."""
    from ..facts import normalize_date
    normalized = normalize_date(quote)
    numeric_source = quote
    if normalized:
        year, month, day = map(int, normalized.split("-"))
        numeric_source += "\n{}年{}月{}日".format(year, month, day)
    return verify_numeric(text, numeric_source) and verify_polarity(text, quote)
