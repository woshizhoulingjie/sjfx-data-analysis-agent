"""Conservative evidence checks for generated mail claims and relations."""

from __future__ import annotations

import re
import hashlib
import json


BUSINESS_PREDICATES = {
    "proposed", "confirmed", "modified", "cancelled", "supports", "contradicts",
}

PREDICATE_MARKERS = {
    "proposed": ("建议", "提议", "要求", "拟", "计划", "proposal", "proposed", "request", "เสนอ", "แผน"),
    "confirmed": ("确认", "已确认", "批准", "同意", "确认过", "confirm", "approve", "agreed", "accepted", "อนุมัติ", "ยืนยัน"),
    "modified": ("修改", "变更", "调整", "更新", "修订", "modified", "updated", "แก้ไข"),
    "cancelled": ("取消", "撤回", "撤销", "终止", "作废", "cancel", "cancelled", "withdrawn", "ยกเลิก"),
    "supports": ("支持", "认可", "赞成", "支持了", "support", "supports", "เห็นด้วย"),
    "contradicts": ("冲突", "矛盾", "反对", "否认", "不一致", "contradict", "dispute", "ขัดแย้ง"),
}


def _anchor(value):
    """Keep letters and numbers from multilingual labels for a literal evidence check."""
    return "".join(re.findall(r"[^\W_]", str(value or ""), flags=re.UNICODE)).casefold()


def _appears_in_evidence(value, quotes):
    value = _anchor(value)
    if len(value) < 2:
        return False
    return any(value in _anchor(quote) for quote in quotes)


def _has_predicate_marker(predicate, claim_text):
    """Require the generated proposition to express the requested action/state."""
    text = str(claim_text or "").casefold()
    return any(str(marker).casefold() in text for marker in PREDICATE_MARKERS.get(predicate, ()))


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
    counter_evidence_ids = []
    for item in candidate.get("counter_evidence_ids") or []:
        item = str(item or "").strip()
        if item and item in evidence_by_id and item not in counter_evidence_ids:
            counter_evidence_ids.append(item)
        if len(counter_evidence_ids) == 8:
            break
    quotes = [str(evidence_by_id[item].get("quote_text") or "") for item in evidence_ids]
    if predicate not in BUSINESS_PREDICATES:
        return {"state": "insufficient", "support_status": "invalid_predicate", "reason": "关系类型不在允许列表", "evidence_ids": evidence_ids, "counter_evidence_ids": counter_evidence_ids}
    if not subject_ref or not object_ref or not claim_text:
        return {"state": "insufficient", "support_status": "invalid_relation", "reason": "关系缺少主体、事项或主张", "evidence_ids": evidence_ids, "counter_evidence_ids": counter_evidence_ids}
    if not quotes:
        return {"state": "insufficient", "support_status": "missing_evidence", "reason": "关系没有可回查证据", "evidence_ids": evidence_ids, "counter_evidence_ids": counter_evidence_ids}
    if not _appears_in_evidence(subject_ref, quotes) or not _appears_in_evidence(object_ref, quotes):
        return {"state": "insufficient", "support_status": "unanchored_entity", "reason": "主体或事项未在引用证据中出现", "evidence_ids": evidence_ids, "counter_evidence_ids": counter_evidence_ids}
    if not _has_predicate_marker(predicate, claim_text):
        return {"state": "insufficient", "support_status": "predicate_not_expressed", "reason": "主张文本没有表达所声明的动作或状态", "evidence_ids": evidence_ids, "counter_evidence_ids": counter_evidence_ids}
    if not _has_predicate_marker(predicate, "\n".join(quotes)):
        return {"state": "insufficient", "support_status": "predicate_not_in_evidence", "reason": "引用证据没有表达所声明的动作或状态", "evidence_ids": evidence_ids, "counter_evidence_ids": counter_evidence_ids}
    # A relation must be about at least one supplied anchor; this prevents an
    # unrelated Chinese sentence from being attached to a valid quote merely
    # because the model copied two entity labels into separate fields.
    if not (_appears_in_evidence(claim_text, quotes) or _appears_in_evidence(subject_ref, [claim_text]) or _appears_in_evidence(object_ref, [claim_text])):
        return {"state": "insufficient", "support_status": "claim_not_anchored", "reason": "主张文本未保留主体或事项锚点", "evidence_ids": evidence_ids, "counter_evidence_ids": counter_evidence_ids}
    from config import Config
    if Config.MAIL_ENABLE_AGENT_MINING:
        quote="\n".join(quotes)
        statement = "\n".join([claim_text] + [v for v in candidate.get('conditions') or [] if isinstance(v, str)])
        polarity_quote = "\n".join(qualification_scope(evidence_by_id[e], candidate.get('source_quote'), object_ref)[0]
                                   for e in evidence_ids) if verified_qualification_scope(candidate, evidence_by_id) else quote
        if not verify_numeric(claim_text,quote) or not verify_temporal(claim_text,quote) or not verify_polarity(statement,polarity_quote):
            return {"state":"insufficient","support_status":"numeric_temporal_or_polarity_mismatch","reason":"数值、时间、否定或条件未与原文一致","evidence_ids":evidence_ids, "counter_evidence_ids": counter_evidence_ids}
    if counter_evidence_ids:
        return {
            "state": "conflicting",
            "support_status": "contradicted",
            "reason": "候选同时声明存在反证，保留主张但不写入已支持关系",
            "evidence_ids": evidence_ids,
            "counter_evidence_ids": counter_evidence_ids,
        }
    requested_state = str(candidate.get("state") or "asserted").strip().casefold()
    if requested_state not in {"asserted", "inferred", "conflicting"}:
        requested_state = "asserted"
    return {
        "state": requested_state,
        "support_status": "supported",
        "reason": None,
        "evidence_ids": evidence_ids,
        "counter_evidence_ids": counter_evidence_ids,
    }


def _numbers(text):
    from decimal import Decimal
    from ..facts import ascii_digits
    return {Decimal(v.replace(",", "")) for v in re.findall(r"[+-]?\d+(?:,\d{3})*(?:\.\d+)?",ascii_digits(text))}


def numeric_mismatches(claim_value,evidence_quote):
    return [str(v) for v in sorted(_numbers(claim_value)-_numbers(evidence_quote))]


def verify_numeric(claim_value,evidence_quote):
    # Decimal equality ignores display-only trailing zeros without float
    # rounding or accepting a different amount, date, or magnitude.
    claimed=_numbers(claim_value);present=_numbers(evidence_quote)
    if not claimed.issubset(present):return False
    # Explicit magnitude/currency words cannot disappear from a normalized numeric claim.
    from ..facts import normalize_amount
    a,b=normalize_amount(claim_value),normalize_amount(evidence_quote)
    if a and b and a["currency"] and b["currency"]:
        return a==b
    return True


def verify_temporal(claim_date,evidence_quote):
    from ..facts import date_mentions
    claimed=date_mentions(claim_date)
    present={value for value in date_mentions(evidence_quote) if value is not None}
    if not all(value is not None and value in present for value in claimed):return False
    # Month/day expressions without a year are dates too. A day number found
    # in an unrelated clock time must not establish a different deadline.
    def month_days(text):
        return {(int(m),int(d)) for m,d in re.findall(
            r'(?<!\d)(\d{1,2})[^\S\r\n]*月[^\S\r\n]*(\d{1,2})[^\S\r\n]*[日号]',str(text))}
    claim_pairs=month_days(claim_date)
    maximum_days=[31,29,31,30,31,30,31,31,30,31,30,31]
    if any(not 1<=m<=12 or not 1<=d<=maximum_days[m-1] for m,d in claim_pairs):return False
    source_pairs=month_days(evidence_quote)|{tuple(map(int,date.split('-')[1:])) for date in present}
    return claim_pairs.issubset(source_pairs)


def verify_polarity(claim_text,evidence_quote):
    negative=r"取消|撤回|未.{0,4}(?:批准|确认|同意)|没有.{0,4}(?:批准|确认|同意)|不(?:确认|批准|同意|支持|认可|接受)|不予|不得|暂缓|尚未|拒绝|cancel(?:led)?|not (?:yet )?(?:confirmed|approved|agreed|accepted)|do not (?:confirm|approve|agree|accept|support)|cannot approve|no longer|withdrawn|ยกเลิก|ไม่อนุมัติ|ไม่ยืนยัน|ยังไม่"
    conditional=r"如果|若|除非|前提|待批准|待确认|待审批|审批后|批准后|内部审批|等.{0,30}审批|审核通过|审批通过|仅当|只有|需经|须经|subject to|provided that|conditional|\b(?:if|unless)\b|pending approval|awaiting approval|หาก|เงื่อนไข|เว้นแต่"
    for pattern in (negative,conditional):
        if re.search(pattern,evidence_quote,re.I) and not re.search(pattern,claim_text,re.I):return False
    return True


def qualification_binding(candidate, evidence_by_id):
    """Bind the internal server audit to the claim and complete source snapshots.

    This is integrity checking of server-produced records, not authentication of
    client input. Callers must never accept a client-provided semantic audit.
    """
    ids = list(dict.fromkeys([str(e) for e in candidate.get('evidence_ids') or []] +
                            ([str(candidate['evidence_id'])] if candidate.get('evidence_id') else [])))
    fields = {'claim_text': str(candidate.get('claim_text') or candidate.get('statement') or candidate.get('text_zh') or '').strip(),
              'actor': candidate.get('actor') or (candidate.get('subject_ref') if '@' in str(candidate.get('subject_ref') or '') else None),
              'source_quote': candidate.get('source_quote'), 'kind': candidate.get('kind'),
              'object_ref': candidate.get('object_ref'),
              'conditions': [v for v in candidate.get('conditions') or [] if isinstance(v, str)]}
    snapshots = [{**evidence_by_id[e], 'evidence_id': e,
                  'quote_text': str(evidence_by_id[e].get('quote_text') or evidence_by_id[e].get('text') or '')}
                 for e in sorted(ids) if e in evidence_by_id]
    return hashlib.sha256(json.dumps([fields, sorted(ids), snapshots], ensure_ascii=False,
                                    sort_keys=True, default=str).encode()).hexdigest()


def verified_qualification_scope(candidate, evidence_by_id):
    audit = (candidate.get('semantic_verification') or {}).get('qualification_scope') or {}
    if not audit.get('complete') or audit.get('binding') != qualification_binding(candidate, evidence_by_id):
        return False
    ids = candidate.get('evidence_ids') or ([candidate['evidence_id']] if candidate.get('evidence_id') else [])
    required = [{**q, 'claim_text': str(candidate.get('claim_text') or candidate.get('text_zh') or '').strip()}
                for e in ids if e in evidence_by_id
                for q in qualification_scope(evidence_by_id[e], candidate.get('source_quote'), candidate.get('object_ref'))[1]]
    return all(q in audit.get('reviewed', []) for q in required)


def qualification_scope(source, quote, anchor=None):
    """Locate a literal citation and retain its complete paragraph, never a token window.

    Every qualified paragraph is separately exposed to the semantic judge. The
    caller cannot use a short quote to hide a qualification outside this boundary.
    """
    text = str(source.get("quote_text") or "")
    located=isinstance(quote,str) and bool(quote.strip()) and text.count(quote)==1
    start=text.index(quote) if located else 0
    end=start+len(quote) if located else len(text)
    paragraphs = []
    for match in re.finditer(r"[^\r\n]+", text):
        value = match.group()
        local = match.start() < end and match.end() > start
        if local:
            paragraphs.append(value)
    qualifiers = []
    for match in re.finditer(r"[^\r\n]+", text):
        value = match.group()
        if not verify_polarity("", value):
            qualifiers.append({"evidence_id": str(source.get("evidence_id") or source.get("chunk_id") or ""),
                               "quote": value,
                               "char_start": int(source.get("char_start") or 0) + match.start()})
    # Missing/ambiguous excerpt never erases full-source conditions. Literal
    # quotation validity is checked independently by the caller.
    return "\n".join(paragraphs) if located else text, qualifiers


def verify_conditions(claim,evidence):
    valid=verify_polarity(str(claim),str(evidence))
    return {"supported":valid,"reason":None if valid else "原文否定或条件未完整保留"}


def translated_numeric_source(quote):
    from ..facts import date_mentions
    numeric_source = quote
    for normalized in date_mentions(quote):
        if normalized is None:continue
        year, month, day = map(int, normalized.split("-"))
        numeric_source += "\n{}年{}月{}日".format(year, month, day)
    return numeric_source


def verify_translated_finding(text, quote):
    """Allow only deterministic calendar normalization alongside source numbers."""
    return verify_numeric(text, translated_numeric_source(quote)) and verify_temporal(text, quote) and verify_polarity(text, quote)


def verify_context(candidate, evidence_by_id, reviews):
    """Mandatory second gate: a literal hit is insufficient without context.

    Possible later qualifications are surfaced even when the model omits its
    counter_evidence_ids.  This is a conservative safety check, not a semantic
    assertion that every revision cancels the earlier proposal.
    """
    from .context import CHANGE_MARKERS, excerpt_attribution
    ids = [str(e) for e in candidate.get("evidence_ids") or [] if str(e) in evidence_by_id]
    sources = [evidence_by_id[e] for e in ids]
    counters, reasons = [], []
    for source in sources:
        key = source.get("message_key")
        review = reviews.get(key) or next((r for r in reviews.values() if key in r.get("message_keys", [])), None)
        if not review or not review.get("complete"):
            reasons.append("回复、会话或附件上下文尚未完整核验")
        cited_excerpt = candidate.get("source_quote")
        attribution = excerpt_attribution(source, cited_excerpt)
        if attribution not in {"authored", "attachment"}:
            reasons.append("引用、转发、签名或未知正文片段不能认定为本邮件发件人亲自陈述")
        local, qualifiers = qualification_scope(source, cited_excerpt, candidate.get("object_ref"))
        statement = "\n".join([str(candidate.get("claim_text") or "")] +
                              [v for v in candidate.get("conditions") or [] if isinstance(v, str)])
        if not verify_polarity(statement, local):
            reasons.append("同一来源的引用段落中的否定或条件没有保留在结论中")
        required = [{**q, "claim_text": str(candidate.get("claim_text") or "")} for q in qualifiers]
        if required and not verified_qualification_scope(candidate, evidence_by_id):
            reasons.append("完整来源的条件适用范围尚未逐项核验")
        for predicate in BUSINESS_PREDICATES:
            if _has_predicate_marker(predicate, candidate.get("claim_text")) and not _has_predicate_marker(predicate, source.get("quote_text")):
                reasons.append("来源没有表达结论中的动作或确认状态")
        if not review:
            continue
        anchor = str(candidate.get("object_ref") or "").strip()
        if not anchor:
            reasons.append("缺少可检查后续条件的事项锚点")
            continue
        if not _appears_in_evidence(anchor, [str(cited_excerpt or source.get("quote_text") or "")]):
            reasons.append("事项锚点未在引用原文中出现")
            continue
        for evidence_id in review.get("change_evidence_ids") or []:
            if evidence_id in ids:
                continue
            other = evidence_by_id.get(evidence_id)
            if not other:
                reasons.append("修改或条件证据未纳入本轮分析上下文")
                continue
            # A related quoted historic email is not the newer sender's
            # withdrawal; retain it as a gap rather than a proved contradiction.
            if excerpt_attribution(other) not in {"authored", "attachment"}:
                continue
            quote = str(other.get("quote_text") or "")
            if not _appears_in_evidence(anchor, [quote]) or not CHANGE_MARKERS.search(quote):
                continue
            source_time, other_time = source.get("sent_at_utc"), other.get("sent_at_utc")
            if source_time and other_time and str(other_time) < str(source_time):
                continue
            if source.get("message_key") == other.get("message_key") and int(other.get("char_start") or 0) <= int(source.get("char_start") or 0):
                continue
            if evidence_id not in counters:
                counters.append(evidence_id)
    if counters:
        return {"state": "conflicting", "support_status": "context_qualified",
                "reason": "相关后续原文包含修改、撤回或条件，不能沿用未经限定的早期结论",
                "counter_evidence_ids": counters[:8]}
    if reasons:
        return {"state": "inferred", "support_status": "context_incomplete",
                "reason": "；".join(dict.fromkeys(reasons)), "counter_evidence_ids": []}
    if re.search(r"最早|最终|最后|\bearliest\b|\bfinal\b", str(candidate.get("claim_text") or ""), re.I):
        return {"state": "inferred", "support_status": "chronology_unproven",
                "reason": "有限会话样本不能证明整包最早或最终状态", "counter_evidence_ids": []}
    return {"state": "asserted", "support_status": "supported", "reason": None, "counter_evidence_ids": []}
