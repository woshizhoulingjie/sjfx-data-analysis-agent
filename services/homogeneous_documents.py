"""Deterministic analysis for batches of similarly structured documents.

The module deliberately uses labelled fields and explicit references before
semantic similarity.  This keeps a several-thousand-document workload bounded,
auditable and useful even when the local generation model is unavailable.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path


SCHEMA_VERSION = "homogeneous-documents/2.0"
MAX_DOCUMENT_CHARS = 160_000
# Relationship generation must remain bounded even when a package contains a
# very large number of similarly named records.  These are safety limits on
# derived edges/candidates, never limits on the files that are inventoried.
MAX_RELATIONS = 50_000
MAX_SUBJECT_CANDIDATE_PAIRS = 200_000
MAX_RELATIONS_PER_SOURCE = 500

FIELD_DEFINITIONS = (
    ("document_number", "文件编号", r"(?:文件编号|文号|函号|编号|Reference(?!s))[ \t]*[：:]?[ \t]*(.{2,100})"),
    ("date", "日期", r"(?:发文日期|成文日期|日期|Date)[ \t]*[：:]?[ \t]*(.{4,60})"),
    ("sender", "发件方", r"(?:发件单位|发文单位|来函单位|发件人|发函方|From)[ \t]*[：:]?[ \t]*(.{2,120})"),
    ("recipient", "收件方", r"(?:收件单位|收文单位|收件人|致|To)[ \t]*[：:]?[ \t]*(.{2,120})"),
    ("subject", "主题事项", r"(?:主题|标题|事由|事项|Subject)[ \t]*[：:]?[ \t]*(.{2,220})"),
    ("matter_id", "事项编号", r"(?:事项编号|项目编号|项目号|合同编号|合同号|案件编号|案件号|Case ID)[ \t]*[：:]?[ \t]*(.{2,100})"),
    ("deadline", "回复期限", r"(?:回复期限|办理期限|截止日期|完成期限|Deadline)[ \t]*[：:]?[ \t]*(.{2,100})"),
    ("signer", "签发人", r"(?:签发人|联系人|负责人|经办人|Signed by)[ \t]*[：:]?[ \t]*(.{2,100})"),
    ("message_id", "邮件标识", r"(?:Message-ID|邮件标识|消息ID)[ \t]*[：:]?[ \t]*(.{2,240})"),
    ("in_reply_to", "回复邮件标识", r"(?:In-Reply-To|回复邮件标识|回复消息ID)[ \t]*[：:]?[ \t]*(.{2,240})"),
    ("cc", "抄送人", r"(?:Cc|CC|抄送|抄送人)[ \t]*[：:]?[ \t]*(.{2,500})"),
    ("bcc", "密送人", r"(?:Bcc|BCC|密送|密送人)[ \t]*[：:]?[ \t]*(.{2,500})"),
    ("reply_to", "回复地址", r"(?:Reply-To|回复地址)[ \t]*[：:]?[ \t]*(.{2,300})"),
    ("references_header", "邮件引用链", r"(?:References|邮件引用链)[ \t]*[：:]?[ \t]*(.{2,1000})"),
)

FIELD_PATTERNS = {
    key: re.compile(r"(?im)^\s*" + pattern + r"\s*$")
    for key, _label, pattern in FIELD_DEFINITIONS
}
LABELS = {key: label for key, label, _pattern in FIELD_DEFINITIONS}
GENERIC_FIELD_PATTERN = re.compile(
    r"(?m)^\s*([A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _（）()/-]{0,23})"
    r"\s*[：:]\s*(\S.{0,240})\s*$"
)
KNOWN_LABEL_TERMS = tuple(
    term.casefold()
    for term in (
        "文件编号", "文号", "函号", "编号", "reference", "ref", "发文日期", "成文日期",
        "日期", "date", "发件单位", "发文单位", "来函单位", "发件人", "发函方", "from",
        "收件单位", "收文单位", "收件人", "致", "to", "主题", "标题", "事由", "事项",
        "subject", "事项编号", "项目编号", "项目号", "合同编号", "合同号", "案件编号",
        "案件号", "case id", "回复期限", "办理期限", "截止日期", "完成期限", "deadline",
        "签发人", "联系人", "负责人", "经办人", "signed by", "message-id", "邮件标识", "in-reply-to", "回复邮件标识",
        "cc", "抄送", "抄送人", "bcc", "密送", "密送人", "reply-to", "回复地址", "references", "邮件引用链",
    )
)

DATE_PATTERNS = (
    re.compile(r"(?P<y>(?:19|20)\d{2})[年./-]\s*(?P<m>\d{1,2})[月./-]\s*(?P<d>\d{1,2})日?"),
    re.compile(r"(?P<y>(?:19|20)\d{2})(?P<m>\d{2})(?P<d>\d{2})"),
    re.compile(r"(?P<y>(?:19|20)\d{2})年\s*(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日?"),
)
DOCUMENT_NUMBER_RE = re.compile(
    r"(?<![\w\u4e00-\u9fff])([A-Za-z\u4e00-\u9fff]{0,24}"
    r"(?:\[|〔|（|\()?(?:19|20)?\d{2,4}(?:\]|〕|）|\))?"
    r"[-_/A-Za-z0-9\u4e00-\u9fff]{0,32}(?:号|函|字|发)?)(?![\w\u4e00-\u9fff])"
)
REFERENCE_PATTERNS = (
    re.compile(r"(?:贵[方单位]|你[方单位]|我[方单位])?\s*(?:于)?\s*[^\n。；]{0,50}?(?:来函|文件|通知|函件)[^\n。；]{0,20}?[“\"《]?([^\n。；，,\"”》]{2,80}(?:号|函))[\"”》]?"),
    re.compile(r"(?:回复|复函|关于|依据|根据|参见|引用)[^\n。；]{0,30}?[“\"《]?([^\n。；，,\"”》]{2,80}(?:号|函))[\"”》]?"),
)
# Reply markers are deliberately not request markers.
REQUEST_WORDS = ("请", "要求", "申请", "请求", "望", "函请", "办理", "协助")
REPLY_WORDS = ("复函", "答复", "就贵", "针对贵", "现答复", "回复如下")
FOLLOW_UP_WORDS = ("催办", "再次", "跟进", "尚未", "未收到", "尽快", "逾期")
ROLE_RULES = (
    ("reply", "回复/复函", REPLY_WORDS),
    ("follow_up", "催办/跟进", FOLLOW_UP_WORDS),
    ("approval", "批复/审批", ("批复", "批准", "同意", "审批意见", "核准")),
    ("notification", "通知/告知", ("通知", "告知", "通报", "公告", "函告")),
    ("supplement", "补充材料", ("补充材料", "补充说明", "附件材料", "补充提交")),
    ("request", "来函/请求", REQUEST_WORDS),
)
STOP_WORDS = {
    "关于", "有关", "事项", "文件", "通知", "函", "回复", "复函", "申请", "报告",
    "the", "and", "for", "with", "from", "subject", "letter",
}

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{5,18}\d)(?!\d)")
IP_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
URL_RE = re.compile(r"https?://[^\s<>\"'，。；;）)\]]{4,240}", re.I)
CASE_ID_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,8}[-_/]?\d{2,}(?:[-_/][A-Z0-9]+)*(?![A-Za-z0-9])", re.I)


def _extract_entities(text, fields):
    """Extract conservative, evidence-bearing entities for relation recall.

    These are lookup features, not conclusions. Entity resolution remains
    separate so a shared name or identifier never becomes a confirmed edge by
    itself.
    """
    source = str(text or "")[:120_000]
    found = []
    seen = set()

    def add(kind, value, match=None):
        value = _clean_value(value, 180)
        key = re.sub(r"[^0-9a-z@._:+/-]+", "", value.casefold())
        if not key or key in seen:
            return
        if kind == "identifier" and re.fullmatch(r"\d{6,8}", key):
            return
        seen.add(key)
        if match is not None:
            start = max(0, match.start() - 50)
            end = min(len(source), match.end() + 80)
            evidence = _clean_value(source[start:end], 320)
        else:
            evidence = value[:320]
        found.append({"kind": kind, "value": value, "key": key, "evidence": evidence})

    for kind, pattern in (("email", EMAIL_RE), ("phone", PHONE_RE), ("ip", IP_RE), ("url", URL_RE), ("identifier", CASE_ID_RE)):
        for match in pattern.finditer(source):
            value = match.group(1) if kind == "email" else match.group(0)
            if kind == "ip" and any(int(part) > 255 for part in value.split(".")):
                continue
            if kind == "phone":
                if re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", value):
                    continue
                digits = re.sub(r"\D", "", value)
                if len(digits) < 7 or len(digits) > 18:
                    continue
                if len(digits) == 8 and digits[:2] in {"19", "20"}:
                    continue
                value = digits
            add(kind, value, match)

    # Header/label values may be supplied by parsers rather than present in the
    # body. Include only stable identifiers as relation features.
    for kind, key in (("email", "sender"), ("email", "recipient"), ("email", "cc"), ("email", "bcc"), ("email", "reply_to"), ("identifier", "matter_id"), ("identifier", "document_number")):
        value = str(fields.get(key) or "")
        if kind == "email":
            for match in EMAIL_RE.finditer(value):
                add(kind, match.group(1), None)
        elif value:
            cleaned = _normalise_identifier(value)
            if len(cleaned) >= 3:
                add(kind, value, None)
    return found


def _clean_value(value, limit=220):
    value = re.sub(r"\s+", " ", str(value or "")).strip(" ：:;；，,")
    return value[:limit]


def _normalise_identifier(value):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _normalise_label(value):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _normalise_party(value):
    value = _clean_value(value, 120)
    return re.sub(r"[（(].*?[)）]", "", value).strip()


def _party_key(value):
    """Stable comparison key that keeps legal entities distinct enough."""
    value = _normalise_party(value).casefold()
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)
    return value


def _normalise_date(value):
    value = str(value or "")
    for pattern in DATE_PATTERNS:
        match = pattern.search(value)
        if not match:
            continue
        try:
            parsed = datetime(
                int(match.group("y")), int(match.group("m")), int(match.group("d"))
            )
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        parsed = parsedate_to_datetime(value)
        if parsed:
            return parsed.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    return ""


def _subject_tokens(value):
    value = re.sub(r"^(?:re|fw|fwd|回复|转发)\s*[:：]\s*", "", str(value or ""), flags=re.I).casefold()
    chinese = re.findall(r"[\u4e00-\u9fff]{2,8}", value)
    latin = re.findall(r"[a-z0-9]{3,}", value)
    tokens = []
    for token in chinese + latin:
        if token not in STOP_WORDS and token not in tokens:
            tokens.append(token)
    return tokens[:24]


def _similarity(left, right):
    left, right = set(left or ()), set(right or ())
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _first_meaningful_line(text, path):
    filename = Path(str(path)).stem.strip()
    for raw in str(text or "").splitlines()[:80]:
        line = _clean_value(raw, 220)
        if len(line) < 4 or re.fullmatch(r"[-_=*\s]+", line):
            continue
        if any(pattern.match(raw) for pattern in FIELD_PATTERNS.values()):
            continue
        if line != filename:
            return line
    return filename


def _field_evidence(text, match):
    start = max(0, match.start() - 40)
    end = min(len(text), match.end() + 80)
    return _clean_value(text[start:end], 320)


def _clean_reference_candidate(value):
    """Return only the document-number part of a reference expression."""
    value = _clean_value(value, 100)
    value = re.sub(r"^(?:文件编号|文号|编号)\s*[：:]\s*", "", value)
    value = re.sub(r"^(?:(?:关于|依据|根据|参见|引用|贵方来函|来函|贵方|我方|你方)\s*)+", "", value)
    matches = list(DOCUMENT_NUMBER_RE.finditer(value))
    if matches:
        value = _clean_value(matches[-1].group(1), 100)
    return value


def _content_units_with_spans(text):
    """Split body text into unique units while retaining original positions."""
    source = str(text or "")
    units = []
    seen = set()
    for match in re.finditer(r"[^\r\n。！？!?；;]+", source):
        raw = match.group(0)
        line = _clean_value(raw, 360)
        if len(line) < 4:
            continue
        if any(pattern.match(raw) for pattern in FIELD_PATTERNS.values()):
            continue
        if GENERIC_FIELD_PATTERN.match(raw):
            continue
        if line in seen:
            continue
        seen.add(line)
        left_trimmed = raw.lstrip()
        start = match.start() + (len(raw) - len(left_trimmed))
        end = match.end() - (len(raw) - len(raw.rstrip()))
        units.append({
            "text": line,
            "source_text": source[start:end],
            "char_start": start,
            "char_end": end,
        })
        if len(units) >= 80:
            break
    return units


def _content_units(text):
    """Split body text into short evidence-bearing units and drop labelled metadata."""
    return [item["text"] for item in _content_units_with_spans(text)]


def _conclusion_support(unit, source="body"):
    return {
        "text": _clean_value(unit.get("source_text") or unit.get("text") or "", 360),
        "source": source,
        "locator": "正文",
        "char_start": int(unit.get("char_start") or 0),
        "char_end": int(unit.get("char_end") or 0),
    }


def _make_conclusion(kind, text, units, support_level="direct"):
    level_config = {
        "direct": ("直接支撑", 0.96),
        "contextual": ("上下文支撑", 0.84),
        "inferred": ("谨慎推断", 0.68),
    }
    level_label, confidence = level_config.get(support_level, level_config["direct"])
    supports = [_conclusion_support(item) for item in units if item.get("text")]
    if not supports:
        return None
    raw_id = "{}\0{}\0{}".format(kind, text, "\0".join(item["text"] for item in units))
    return {
        "conclusion_id": "CON-" + hashlib.sha256(raw_id.encode("utf-8", errors="replace")).hexdigest()[:12],
        "type": kind,
        "text": _clean_value(text, 360),
        "support_level": support_level,
        "support_label": level_label,
        "confidence": confidence,
        "supports": supports[:4],
        "status": "system_generated",
    }


def _build_content_understanding(text, fields, action):
    """Produce deterministic, evidence-bound understanding for structured documents."""
    sample = str(text or "")[:12_000]
    unit_records = _content_units_with_spans(sample)
    units = [item["text"] for item in unit_records]
    # Field labels such as “回复期限” are metadata, not evidence that the
    # document itself is a reply. Role signals come from body units and subject.
    signal_text = "\n".join(units) + "\n" + str(fields.get("subject") or "")
    role_key, role_label = "statement", "说明/告知"
    for candidate_key, candidate_label, markers in ROLE_RULES:
        if any(marker in signal_text for marker in markers):
            role_key, role_label = candidate_key, candidate_label
            break
    if role_key == "reply":
        intent = "针对前件或来函作出回复，并说明处理结果"
    elif role_key == "follow_up":
        intent = "催办、跟进或提示逾期事项"
    elif role_key == "approval":
        intent = "表达审批、批准或处理意见"
    elif role_key == "notification":
        intent = "向收件方通知、告知或传达事项"
    elif role_key == "supplement":
        intent = "补充事实、材料或办理说明"
    elif role_key == "request":
        intent = "向收件方提出办理、协助或回复请求"
    else:
        intent = "说明事实、背景或办理情况"
    request_records = [item for item in unit_records if any(word in item["text"] for word in REQUEST_WORDS)]
    request_units = [item["text"] for item in request_records]
    conclusion_markers = REPLY_WORDS + ("同意", "批准", "已完成", "已处理", "结论", "决定", "无法", "不予")
    conclusion_records = [item for item in unit_records if any(marker in item["text"] for marker in conclusion_markers)]
    conclusion_units = [item["text"] for item in conclusion_records]
    ranked = []
    for index, unit in enumerate(units):
        score = 0
        if unit in request_units:
            score += 3
        if unit in conclusion_units:
            score += 3
        if any(marker in unit for marker in ("原因", "由于", "因此", "目前", "截至", "情况")):
            score += 1
        ranked.append((score, index, unit))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    key_facts = [item[2] for item in ranked if item[2] not in request_units and item[2] not in conclusion_units][:4]
    if not key_facts:
        key_facts = units[:3]
    facts_records = [item for item in unit_records if item["text"] in key_facts]
    conclusions = []
    for item in request_records[:3]:
        conclusion = _make_conclusion(
            "动作", "{}要求{}：{}".format(
                fields.get("sender") or "发件方",
                fields.get("recipient") or "收件方",
                item["text"],
            ), [item], "direct",
        )
        if conclusion:
            conclusions.append(conclusion)
    for item in conclusion_records[:3]:
        conclusion = _make_conclusion("事实", "文件说明：{}".format(item["text"]), [item], "direct")
        if conclusion:
            conclusions.append(conclusion)
    for item in facts_records[:3]:
        conclusion = _make_conclusion("事实", "文件提及：{}".format(item["text"]), [item], "direct")
        if conclusion:
            conclusions.append(conclusion)
    unique_conclusions = []
    seen_conclusions = set()
    for conclusion in conclusions:
        if conclusion["text"] in seen_conclusions:
            continue
        seen_conclusions.add(conclusion["text"])
        unique_conclusions.append(conclusion)
    return {
        "document_role": role_key,
        "document_role_label": role_label,
        "intent": intent,
        "requested_action": request_units[0] if request_units else "",
        "response_requested": bool(request_units) and role_key not in {"reply", "notification", "approval"},
        "response_due": fields.get("deadline") or "",
        "key_facts": key_facts[:4],
        "key_conclusions": conclusion_units[:4],
        "evidence_units": (request_units[:2] + conclusion_units[:2] + key_facts[:2])[:6],
        "conclusions": unique_conclusions[:8],
        "understanding_complete": bool(units),
    }


def extract_record(path, payload):
    payload = payload or {}
    raw_text = str(payload.get("text") or "")
    text = raw_text[:MAX_DOCUMENT_CHARS]
    text_truncated = len(raw_text) > len(text)
    fields, evidence = {}, {}
    for key, _label, _pattern in FIELD_DEFINITIONS:
        match = FIELD_PATTERNS[key].search(text[:40_000])
        value = _clean_value(match.group(1)) if match else ""
        if key in {"sender", "recipient"}:
            value = _normalise_party(value)
        if key == "date":
            value = _normalise_date(value)
        fields[key] = value
        if match:
            evidence[key] = _field_evidence(text, match)

    # Parsers may already have recovered labelled metadata (for example from
    # an EML header or a spreadsheet row). Use it only when body labels are
    # absent, keeping the body evidence authoritative and auditable.
    metadata = payload.get("metadata") or payload.get("fields") or {}
    if isinstance(metadata, dict):
        aliases = {
            "document_number": ("document_number", "文号", "文件编号"),
            "date": ("date", "日期", "发文日期"),
            "sender": ("sender", "from", "发件人", "发文单位"),
            "recipient": ("recipient", "to", "收件人", "收文单位"),
            "subject": ("subject", "主题", "标题"),
            "matter_id": ("matter_id", "项目编号", "合同编号", "案件编号"),
            "deadline": ("deadline", "截止日期", "办理期限"),
            "signer": ("signer", "签发人", "负责人"),
            "message_id": ("message_id", "message-id", "邮件标识"),
            "in_reply_to": ("in_reply_to", "in-reply-to", "回复邮件标识"),
            "cc": ("cc", "cc", "抄送", "抄送人"),
            "bcc": ("bcc", "bcc", "密送", "密送人"),
            "reply_to": ("reply_to", "reply-to", "回复地址"),
            "references_header": ("references_header", "references", "邮件引用链"),
        }
        for key, keys in aliases.items():
            if fields.get(key):
                continue
            value = next((metadata.get(alias) for alias in keys if metadata.get(alias)), "")
            if value:
                fields[key] = _normalise_date(value) if key == "date" else _clean_value(value)
                if key in {"sender", "recipient"}:
                    fields[key] = _normalise_party(fields[key])

    custom_fields, custom_evidence, custom_field_conflicts = {}, {}, {}
    custom_seen = {}
    for match in GENERIC_FIELD_PATTERN.finditer(text[:40_000]):
        label = _clean_value(match.group(1), 40)
        label_key = _normalise_label(label)
        if not label_key or any(label_key == _normalise_label(term) for term in KNOWN_LABEL_TERMS):
            continue
        value = _clean_value(match.group(2), 240)
        if not value:
            continue
        if label_key in custom_seen:
            previous_label = custom_seen[label_key]
            if custom_fields.get(previous_label) != value:
                custom_field_conflicts.setdefault(label_key, []).append({
                    "label": label, "value": value,
                    "evidence": _field_evidence(text, match),
                })
            continue
        custom_seen[label_key] = label
        custom_fields[label] = value
        custom_evidence[label] = _field_evidence(text, match)
        if len(custom_fields) >= 40:
            break

    source = payload.get("source") or {}
    language_value = payload.get("language") or payload.get("source_language") or "unknown"
    if isinstance(language_value, dict):
        language_value = language_value.get("code") or language_value.get("language") or "unknown"
    attachment_source = payload.get("attachments") or payload.get("attachment") or []
    if isinstance(attachment_source, dict):
        attachment_source = [attachment_source]
    attachments = []
    for item in attachment_source if isinstance(attachment_source, (list, tuple)) else ():
        if isinstance(item, dict):
            name = item.get("name") or item.get("filename") or item.get("path") or "未命名附件"
            kind = item.get("mime_type") or item.get("content_type") or item.get("type") or ""
            text_available = bool(item.get("text") or item.get("content") or item.get("parsed_text"))
        else:
            name, kind, text_available = str(item), "", False
        attachments.append({
            "name": _clean_value(name, 180),
            "type": _clean_value(kind, 80),
            "text_available": text_available,
        })
    analysis_translation = payload.get("analysis_translation") or payload.get("translation") or {}
    if not fields["subject"]:
        headings = ((payload.get("structure") or {}).get("headings") or [])
        if headings:
            heading = headings[0]
            fields["subject"] = _clean_value(
                heading.get("text") if isinstance(heading, dict) else heading
            )
        if not fields["subject"]:
            fields["subject"] = _first_meaningful_line(text, path)

    # Exclude the document-number header itself from external references.
    reference_text = "\n".join(
        line for line in text.splitlines()
        if not FIELD_PATTERNS["document_number"].match(line)
    )
    own_number_key = _normalise_identifier(fields.get("document_number"))
    references = []
    for pattern in REFERENCE_PATTERNS:
        for match in pattern.finditer(reference_text[:80_000]):
            value = _clean_reference_candidate(match.group(1))
            key = _normalise_identifier(value)
            if key and key != own_number_key and all(_normalise_identifier(item) != key for item in references):
                references.append(value)
            if len(references) >= 12:
                break
    # A reference often appears as a bare number in a table or subject line;
    # capture those forms as well, while excluding ordinary dates.
    for match in DOCUMENT_NUMBER_RE.finditer(reference_text[:120_000]):
        value = _clean_reference_candidate(match.group(1))
        if not re.search(r"(?:号|函|字|发)", value) or re.fullmatch(r"\d{6,8}", value):
            continue
        key = _normalise_identifier(value)
        if key and key != own_number_key and all(_normalise_identifier(item) != key for item in references):
            references.append(value)
        if len(references) >= 24:
            break

    body_excerpt = _clean_value(text, 420)
    action = "说明"
    body_signal = "\n".join(_content_units(text[:12_000]))
    if any(word in body_signal for word in FOLLOW_UP_WORDS):
        action = "催办或跟进"
    elif any(word in body_signal for word in REPLY_WORDS):
        action = "回复"
    elif any(word in body_signal for word in REQUEST_WORDS):
        action = "提出请求"
    content_understanding = _build_content_understanding(text, fields, action)
    entities = _extract_entities(text, fields)
    entity_keys = sorted({item["key"] for item in entities if item.get("key")})
    record_type = "email" if (
        fields.get("message_id") or fields.get("in_reply_to")
        or fields.get("references_header")
        or Path(str(path)).suffix.casefold() in {".eml", ".msg"}
        or "@" in str(fields.get("sender") or "")
        or "@" in str(fields.get("recipient") or "")
    ) else "document"
    parties = "{} → {}".format(fields["sender"] or "未知发件方", fields["recipient"] or "未知收件方")
    summary = "{}；{}；围绕“{}”{}。".format(
        fields["date"] or "日期不明", parties, fields["subject"] or "未识别事项", action
    )

    source_fingerprint = str(source.get("sha256") or "").strip()
    if not source_fingerprint:
        source_fingerprint = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()
    return {
        "path": str(path),
        "name": str(source.get("name") or Path(str(path)).name),
        "fields": fields,
        "references": references,
        "summary": summary,
        "excerpt": body_excerpt,
        "content_understanding": content_understanding,
        "field_evidence": evidence,
        "custom_fields": custom_fields,
        "custom_field_evidence": custom_evidence,
        "custom_field_conflicts": custom_field_conflicts,
        "subject_tokens": _subject_tokens(fields["subject"]),
        "party_keys": {
            "sender": _party_key(fields.get("sender")),
            "recipient": _party_key(fields.get("recipient")),
        },
        "action": action,
        "content_available": bool(text.strip()),
        "text_truncated": text_truncated,
        "source_char_count": len(raw_text),
        "scanned_char_count": len(text),
        "reference_scan_complete": not text_truncated,
        "source_fingerprint": source_fingerprint,
        "record_type": record_type,
        "source_language": _clean_value(language_value, 40) or "unknown",
        "analysis_translation": analysis_translation if isinstance(analysis_translation, dict) else {},
        "attachments": attachments[:100],
        "attachment_count": len(attachments),
        "entities": entities[:200],
        "entity_keys": entity_keys[:200],
        "entity_evidence": {item["key"]: item["evidence"] for item in entities[:200] if item.get("key")},
        "relation_stage": "normalized",
    }


def _date_distance(left, right):
    try:
        first = datetime.strptime(left, "%Y-%m-%d")
        second = datetime.strptime(right, "%Y-%m-%d")
        return abs((first - second).days)
    except (TypeError, ValueError):
        return None


def _ordered_pair(left, right):
    left_date = left["fields"].get("date") or "9999-99-99"
    right_date = right["fields"].get("date") or "9999-99-99"
    return (left, right) if (left_date, left["path"]) <= (right_date, right["path"]) else (right, left)


def _relation_id(source, target, relation_type):
    raw = "{}\0{}\0{}".format(source, target, relation_type).encode("utf-8")
    return "REL-" + hashlib.sha256(raw).hexdigest()[:16]


def _relation(source, target, relation_type, confidence, reasons, evidence="", status=None):
    if status is None:
        status = "candidate" if relation_type in {"same_matter", "correspondence_flow", "shared_entity"} else "derived"
    evidence_refs = []
    snippet = _clean_value(evidence, 360)
    if snippet:
        evidence_refs.append({
            "path": source.get("path") or "",
            "kind": "body_or_metadata",
            "snippet": snippet,
            "locator": "normalized_record",
        })
    if relation_type == "reply_to":
        for path, record, field in (
            (source.get("path"), source, "in_reply_to"),
            (target.get("path"), target, "message_id"),
        ):
            value = str((record.get("fields") or {}).get(field) or "")
            if value:
                evidence_refs.append({
                    "path": path or "",
                    "kind": "header",
                    "field": field,
                    "snippet": str((record.get("field_evidence") or {}).get(field) or value)[:360],
                    "locator": "header",
                })
    elif relation_type == "same_matter":
        for path, record in ((source.get("path"), source), (target.get("path"), target)):
            value = str((record.get("fields") or {}).get("matter_id") or "")
            if value:
                evidence_refs.append({
                    "path": path or "",
                    "kind": "field",
                    "field": "matter_id",
                    "snippet": str((record.get("field_evidence") or {}).get("matter_id") or value)[:360],
                    "locator": "normalized_record",
                })
    elif relation_type == "shared_entity":
        for path, record in ((source.get("path"), source), (target.get("path"), target)):
            entity_evidence = record.get("entity_evidence") or {}
            if snippet:
                matches = [value for key, value in entity_evidence.items() if value and any(str(reason).endswith(str(key)) for reason in reasons)]
                if matches:
                    evidence_refs.append({"path": path or "", "kind": "entity", "snippet": str(matches[0])[:360], "locator": "body"})
    return {
        "relation_id": _relation_id(source["path"], target["path"], relation_type),
        "source_path": source["path"],
        "target_path": target["path"],
        "relation_type": relation_type,
        "relation_label": {
            "reply_to": "回复",
            "follow_up": "跟进/催办",
            "references": "引用",
            "same_matter": "同一事项",
            "correspondence_flow": "往来链",
            "shared_entity": "共同实体",
        }.get(relation_type, relation_type),
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "relation_status": status,
        "evidence_level": "explicit" if any(
            "明确" in str(reason) or "精确" in str(reason) or "完全相同" in str(reason)
            for reason in reasons
        ) else "inferred",
        "reasons": reasons,
        "confidence_components": {
            "explicit_reference": any(
                marker in reasons
                for marker in ("正文明确文号引用", "邮件 In-Reply-To 标识精确匹配", "邮件 References 引用链精确匹配")
            ),
            "matter_id_match": "事项/项目/合同编号完全相同" in reasons,
            "party_direction": "发件方与收件方互换" in reasons,
            "temporal_order": "日期顺序一致" in reasons,
        },
        "evidence": _clean_value(evidence, 360),
        "evidence_refs": evidence_refs[:8],
        "source_record_type": source.get("record_type") or "document",
        "target_record_type": target.get("record_type") or "document",
    }


class _UnionFind:
    def __init__(self, values):
        self.parent = {value: value for value in values}

    def find(self, value):
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            value, self.parent[value] = self.parent[value], root
        return root

    def union(self, left, right):
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _build_relations(records):
    by_number = defaultdict(list)
    by_number_suffix = defaultdict(list)
    by_message_id = {}
    duplicate_numbers = defaultdict(list)
    by_matter = defaultdict(list)
    by_subject = defaultdict(list)
    by_entity = defaultdict(list)
    entity_kind = {}
    for record in records:
        fields = record["fields"]
        number_key = _normalise_identifier(fields.get("document_number"))
        if number_key:
            by_number[number_key].append(record)
            duplicate_numbers[number_key].append(record)
            # Keep a suffix index for OCR/punctuation variants.  The previous
            # implementation scanned every known number for every reference,
            # which became quadratic on large homogeneous packages.
            for width in (6, 8, 10, 12):
                if len(number_key) >= width:
                    by_number_suffix[number_key[-width:]].append(record)
        message_key = _normalise_identifier(fields.get("message_id"))
        if message_key:
            by_message_id.setdefault(message_key, record)
        matter_key = _normalise_identifier(fields.get("matter_id"))
        if matter_key:
            by_matter[matter_key].append(record)
        subject_key = "|".join(sorted(record.get("subject_tokens") or ()))
        if subject_key:
            by_subject[subject_key].append(record)
        for entity in record.get("entities") or ():
            entity_key = str(entity.get("key") or "").strip()
            if entity_key:
                by_entity[entity_key].append(record)
                entity_kind.setdefault(entity_key, str(entity.get("kind") or "unknown"))

    relations, seen = [], set()

    per_source_counts = defaultdict(int)

    def add(item):
        key = (item["source_path"], item["target_path"], item["relation_type"])
        source_path = item["source_path"]
        if (
            key in seen
            or source_path == item["target_path"]
            or len(relations) >= MAX_RELATIONS
            or per_source_counts[source_path] >= MAX_RELATIONS_PER_SOURCE
        ):
            return
        seen.add(key)
        relations.append(item)
        per_source_counts[source_path] += 1

    for record in records:
        fields = record["fields"]
        reply_key = _normalise_identifier(fields.get("in_reply_to"))
        if reply_key and by_message_id.get(reply_key):
            target = by_message_id[reply_key]
            if target["path"] != record["path"]:
                add(_relation(
                    record, target, "reply_to", 0.995,
                    ["邮件 In-Reply-To 标识精确匹配", "日期顺序一致"] if fields.get("date") and target["fields"].get("date") else ["邮件 In-Reply-To 标识精确匹配"],
                    fields.get("in_reply_to"),
                    status="validated",
                ))
        message_references = re.findall(
            r"<[^>]{2,240}>|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+",
            str(fields.get("references_header") or ""),
        )
        for reference_id in message_references:
            reference_key = _normalise_identifier(reference_id)
            target = by_message_id.get(reference_key)
            if not target or target["path"] == record["path"]:
                continue
            add(_relation(
                record, target, "references", 0.975,
                ["邮件 References 引用链精确匹配"],
                reference_id,
                status="validated",
            ))
        for reference in record.get("references") or ():
            reference_key = _normalise_identifier(reference)
            exact_targets = by_number.get(reference_key) or []
            target = exact_targets[0] if len(exact_targets) == 1 else None
            if not target and reference_key:
                # OCR and punctuation differences are common in scanned
                # letters. Resolve only an unambiguous normalized suffix.
                candidates = []
                for width in (12, 10, 8, 6):
                    suffix = reference_key[-width:] if len(reference_key) >= width else reference_key
                    for item in by_number_suffix.get(suffix, ()):
                        if item not in candidates:
                            candidates.append(item)
                    if candidates:
                        break
                if len(candidates) == 1:
                    target = candidates[0]
            if not target or target["path"] == record["path"]:
                continue
            text = record.get("excerpt") or ""
            swapped = bool(
                fields.get("sender") and fields.get("recipient")
                and fields.get("sender") == target["fields"].get("recipient")
                and fields.get("recipient") == target["fields"].get("sender")
            )
            relation_type = "reply_to" if swapped or any(word in text for word in REPLY_WORDS) else "references"
            confidence = 0.98 if relation_type == "reply_to" else 0.94
            reasons = ["正文明确引用目标文件编号"]
            if swapped:
                reasons.append("发件方与收件方互换")
            if record["fields"].get("date") and target["fields"].get("date") and record["fields"]["date"] >= target["fields"]["date"]:
                reasons.append("日期顺序一致")
            add(_relation(record, target, relation_type, confidence, reasons, reference, status="validated"))

    for matter_key, items in by_matter.items():
        ordered = sorted(items, key=lambda item: (item["fields"].get("date") or "9999", item["path"]))
        for previous, current in zip(ordered, ordered[1:]):
            add(_relation(current, previous, "same_matter", 0.96, ["事项/项目/合同编号完全相同"], current["fields"].get("matter_id") or matter_key, status="validated"))

    # Build partial-subject candidate groups through an inverted index. Exact
    # subject keys remain strongest, while shared meaningful tokens recover
    # common variants such as “付款安排” vs “关于付款安排的回复”.
    token_index = defaultdict(list)
    for record in records:
        for token in record.get("subject_tokens") or ():
            token_index[token].append(record)
    subject_pairs = defaultdict(int)
    candidate_pair_count = 0
    for items in token_index.values():
        if len(items) > 250:
            continue
        for left_index, left in enumerate(items):
            for right in items[left_index + 1:]:
                if candidate_pair_count >= MAX_SUBJECT_CANDIDATE_PAIRS:
                    break
                key = tuple(sorted((left["path"], right["path"])))
                subject_pairs[key] += 1
                candidate_pair_count += 1
            if candidate_pair_count >= MAX_SUBJECT_CANDIDATE_PAIRS:
                break
        if candidate_pair_count >= MAX_SUBJECT_CANDIDATE_PAIRS:
            break
    grouped = defaultdict(list)
    by_path = {item["path"]: item for item in records}
    for (left_path, right_path), shared in subject_pairs.items():
        left, right = by_path[left_path], by_path[right_path]
        similarity = _similarity(left.get("subject_tokens"), right.get("subject_tokens"))
        if shared >= 2 or similarity >= 0.5:
            grouped[(left_path, right_path)] = [left, right]

    for _subject_key, items in by_subject.items():
        # Exact groups are already represented by the partial candidate map;
        # retaining them here guarantees one pass for single-token subjects.
        if len(items) >= 2:
            for pair in zip(items, items[1:]):
                grouped.setdefault(tuple(sorted((pair[0]["path"], pair[1]["path"]))), list(pair))

    for _pair_key, items in grouped.items():
        if len(items) < 2:
            continue
        previous, current = _ordered_pair(items[0], items[1])
        days = _date_distance(previous["fields"].get("date"), current["fields"].get("date"))
        if days is not None and days > 730:
            continue
        pfields, cfields = previous["fields"], current["fields"]
        pkeys, ckeys = previous.get("party_keys") or {}, current.get("party_keys") or {}
        swapped = bool(pkeys.get("sender") and pkeys.get("recipient") and pkeys.get("sender") == ckeys.get("recipient") and pkeys.get("recipient") == ckeys.get("sender"))
        current_text = current.get("excerpt") or ""
        shared = _similarity(previous.get("subject_tokens"), current.get("subject_tokens"))
        if swapped and any(word in current_text for word in REPLY_WORDS):
            relation_type, confidence, reasons = "reply_to", 0.86 + 0.05 * shared, ["主题具有共同关键词", "发件方与收件方互换", "后续文件包含回复表达"]
            relation_status = "derived"
        elif any(word in current_text for word in FOLLOW_UP_WORDS):
            relation_type, confidence, reasons = "follow_up", 0.78 + 0.08 * shared, ["主题具有共同关键词", "后续文件包含催办或跟进表达"]
            relation_status = "derived"
        else:
            relation_type, confidence, reasons = "same_matter", 0.55 + 0.10 * shared, ["主题具有共同关键词"]
            relation_status = "candidate"
        if previous["fields"].get("date") and current["fields"].get("date"):
            reasons.append("日期顺序一致")
        add(_relation(current, previous, relation_type, confidence, reasons, cfields.get("subject"), status=relation_status))

    # Shared stable entities provide a bounded recall path when subjects or
    # document numbers differ. They remain candidates until a reviewer confirms
    # the identity or the relation has an independent hard signal.
    entity_pair_count = 0
    for entity_key, items in by_entity.items():
        if len(items) > 100:
            continue
        kind = entity_kind.get(entity_key) or "unknown"
        if kind == "email" and len(items) > 20:
            continue
        for left_index, left in enumerate(items):
            for right in items[left_index + 1:]:
                if entity_pair_count >= MAX_SUBJECT_CANDIDATE_PAIRS:
                    break
                previous, current = _ordered_pair(left, right)
                confidence = 0.72 if kind in {"phone", "ip"} else (0.68 if kind == "identifier" else 0.58)
                add(_relation(
                    current, previous, "shared_entity", confidence,
                    ["共同实体:{}".format(entity_key)],
                    (current.get("entity_evidence") or {}).get(entity_key)
                    or (previous.get("entity_evidence") or {}).get(entity_key)
                    or entity_key,
                    status="candidate",
                ))
                entity_pair_count += 1
            if entity_pair_count >= MAX_SUBJECT_CANDIDATE_PAIRS:
                break
        if entity_pair_count >= MAX_SUBJECT_CANDIDATE_PAIRS:
            break

    # A correspondence chain can be evident even when subjects differ (for
    # example a request followed by a formal approval). Only link adjacent
    # files when there is a close date and an independent topic/entity signal;
    # otherwise unrelated mailboxes would be chained together.
    dated = sorted(
        [item for item in records if item.get("fields", {}).get("date")],
        key=lambda item: (item["fields"].get("date"), item["path"]),
    )
    for previous, current in zip(dated, dated[1:]):
        pkeys, ckeys = previous.get("party_keys") or {}, current.get("party_keys") or {}
        if not (pkeys.get("recipient") and ckeys.get("sender") and pkeys.get("recipient") == ckeys.get("sender")):
            continue
        days = _date_distance(previous["fields"].get("date"), current["fields"].get("date"))
        shared_subject = bool(set(previous.get("subject_tokens") or ()) & set(current.get("subject_tokens") or ()))
        shared_entities = bool(set(previous.get("entity_keys") or ()) & set(current.get("entity_keys") or ()))
        current_text = current.get("excerpt") or ""
        if days is None or days > 90 or not (shared_subject or shared_entities or any(word in current_text for word in REPLY_WORDS + FOLLOW_UP_WORDS)):
            continue
        if pkeys.get("sender") == ckeys.get("recipient"):
            reasons = ["收发双方形成连续往来", "日期顺序一致"]
            if shared_subject:
                reasons.append("主题存在交集")
            if shared_entities:
                reasons.append("实体存在交集")
            add(_relation(
                current, previous, "correspondence_flow", 0.76,
                reasons,
                "{} → {}".format(previous["fields"].get("recipient"), current["fields"].get("sender")),
                status="derived",
            ))

    relations.sort(key=lambda item: (-item["confidence"], item["source_path"], item["target_path"]))
    return relations


def _build_cases(records, relations):
    by_path = {record["path"]: record for record in records}
    groups = _UnionFind(by_path)
    for relation in relations:
        if relation.get("relation_status") == "validated":
            groups.union(relation["source_path"], relation["target_path"])
    members = defaultdict(list)
    for path in by_path:
        members[groups.find(path)].append(by_path[path])
    cases = []
    relation_ids_by_path = defaultdict(list)
    for relation in relations:
        relation_ids_by_path[relation["source_path"]].append(relation["relation_id"])
        relation_ids_by_path[relation["target_path"]].append(relation["relation_id"])
    for root, items in members.items():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda item: (item["fields"].get("date") or "9999", item["path"]))
        matters = [item["fields"].get("matter_id") for item in ordered if item["fields"].get("matter_id")]
        subjects = [item["fields"].get("subject") for item in ordered if item["fields"].get("subject")]
        title = Counter(subjects).most_common(1)[0][0] if subjects else (matters[0] if matters else Path(root).stem)
        case_id = "CASE-" + hashlib.sha256("\0".join(sorted(item["path"] for item in ordered)).encode("utf-8")).hexdigest()[:12]
        paths = {item["path"] for item in ordered}
        edge_ids = []
        seen_edges = set()
        for member in paths:
            for relation_id in relation_ids_by_path.get(member, ()):
                if relation_id not in seen_edges:
                    seen_edges.add(relation_id)
                    edge_ids.append(relation_id)
        cases.append({
            "case_id": case_id,
            "title": title,
            "matter_ids": sorted(set(matters)),
            "document_count": len(ordered),
            "relation_count": len(edge_ids),
            "start_date": next((item["fields"].get("date") for item in ordered if item["fields"].get("date")), ""),
            "end_date": next((item["fields"].get("date") for item in reversed(ordered) if item["fields"].get("date")), ""),
            "document_paths": [item["path"] for item in ordered],
            "relation_ids": edge_ids,
            "timeline": [{"path": item["path"], "date": item["fields"].get("date"), "summary": item["summary"]} for item in ordered],
        })
    cases.sort(key=lambda item: (-item["document_count"], item["title"]))
    return cases


def _build_anomalies(records, relations, schema_fields):
    anomalies = []
    required = [item["key"] for item in schema_fields if item["coverage"] >= 0.6]
    known_numbers = {
        _normalise_identifier(item["fields"].get("document_number"))
        for item in records if item["fields"].get("document_number")
    }
    number_groups = defaultdict(list)
    for item in records:
        number = _normalise_identifier(item["fields"].get("document_number"))
        if number:
            number_groups[number].append(item)
    replied_sources = {
        item["source_path"] for item in relations if item["relation_type"] == "reply_to"
    }
    for record in records:
        number = _normalise_identifier(record["fields"].get("document_number"))
        if number and len(number_groups[number]) > 1:
            anomalies.append({
                "type": "duplicate_document_number", "label": "文号重复",
                "path": record["path"], "severity": "high",
                "message": "文号“{}”在 {} 份文件中重复，引用关系需要人工确认".format(
                    record["fields"].get("document_number"), len(number_groups[number])
                ),
            })
        custom_values = {
            _normalise_label(label): value
            for label, value in (record.get("custom_fields") or {}).items()
        }
        missing = []
        for key in required:
            if str(key).startswith("custom:"):
                present = custom_values.get(str(key).split(":", 1)[1])
                label = next(
                    (item["label"] for item in schema_fields if item["key"] == key), key
                )
            else:
                present = record["fields"].get(key)
                label = LABELS[key]
            if not present:
                missing.append(label)
        if missing:
            anomalies.append({"type": "missing_fields", "label": "字段缺失", "path": record["path"], "severity": "medium", "message": "缺少：{}".format("、".join(missing))})
        for logical_key, conflicts in (record.get("custom_field_conflicts") or {}).items():
            anomalies.append({
                "type": "custom_field_conflict", "label": "自定义字段冲突",
                "path": record["path"], "severity": "medium",
                "message": "字段“{}”在同一文件中出现不同值：{}".format(
                    logical_key, "、".join(str(item.get("value") or "") for item in conflicts)
                ),
            })
        for reference in record.get("references") or ():
            if _normalise_identifier(reference) not in known_numbers:
                anomalies.append({"type": "missing_reference", "label": "引用文件缺失", "path": record["path"], "severity": "high", "message": "引用“{}”，但当前数据包中未找到对应编号".format(reference)})
        excerpt = record.get("excerpt") or ""
        if record["path"] not in replied_sources and any(word in excerpt for word in REQUEST_WORDS):
            anomalies.append({"type": "possible_unanswered", "label": "可能未回复", "path": record["path"], "severity": "low", "message": "文件包含请求或答复要求，但尚未识别到明确回复关系"})
    return anomalies[:20_000]


def _email_values(value):
    return [match.group(1).casefold() for match in EMAIL_RE.finditer(str(value or ""))]


def _entity_statistics(records, limit=80):
    """Return ranked entities with document counts and bounded evidence paths."""
    buckets = {}
    for record in records:
        seen = set()
        for entity in record.get("entities") or ():
            if not isinstance(entity, dict):
                continue
            kind = str(entity.get("kind") or "unknown")
            key = str(entity.get("key") or "").strip()
            value = _clean_value(entity.get("value") or key, 180)
            if not key or (kind, key) in seen:
                continue
            seen.add((kind, key))
            bucket = buckets.setdefault((kind, key), {
                "kind": kind,
                "key": key,
                "value": value,
                "mention_count": 0,
                "document_count": 0,
                "document_paths": [],
                "evidence": [],
            })
            bucket["mention_count"] += 1
            bucket["document_count"] += 1
            path = str(record.get("path") or "")
            if path and len(bucket["document_paths"]) < 12:
                bucket["document_paths"].append(path)
            evidence = _clean_value(entity.get("evidence") or "", 320)
            if evidence and evidence not in bucket["evidence"] and len(bucket["evidence"]) < 3:
                bucket["evidence"].append(evidence)
    items = list(buckets.values())
    for item in items:
        item["sample_paths"] = list(item.pop("document_paths"))
    items.sort(key=lambda item: (-item["mention_count"], -item["document_count"], item["kind"], item["value"]))
    return items[:max(1, int(limit or 80))]


def _communication_statistics(records, limit=60):
    """Aggregate directed sender/recipient pairs without inventing identity links."""
    buckets = {}
    for record in records:
        fields = record.get("fields") or {}
        sender_values = _email_values(fields.get("sender")) or [_party_key(fields.get("sender"))]
        recipient_values = _email_values(fields.get("recipient")) or [_party_key(fields.get("recipient"))]
        sender_values = [value for value in sender_values if value]
        recipient_values = [value for value in recipient_values if value]
        for sender in sender_values[:5]:
            for recipient in recipient_values[:10]:
                if sender == recipient:
                    continue
                key = (sender, recipient)
                bucket = buckets.setdefault(key, {
                    "sender": sender,
                    "recipient": recipient,
                    "message_count": 0,
                    "document_count": 0,
                    "document_paths": [],
                    "first_date": "",
                    "last_date": "",
                })
                bucket["message_count"] += 1
                bucket["document_count"] += 1
                path = str(record.get("path") or "")
                if path and len(bucket["document_paths"]) < 8:
                    bucket["document_paths"].append(path)
                date = str(fields.get("date") or "")
                if date and (not bucket["first_date"] or date < bucket["first_date"]):
                    bucket["first_date"] = date
                if date and date > bucket["last_date"]:
                    bucket["last_date"] = date
    items = list(buckets.values())
    for item in items:
        item["sample_paths"] = list(item.pop("document_paths"))
    items.sort(key=lambda item: (-item["message_count"], item["sender"], item["recipient"]))
    return items[:max(1, int(limit or 60))]


def _content_profile(records):
    roles = Counter()
    record_types = Counter()
    languages = Counter()
    attachments = 0
    characters = 0
    for record in records:
        understanding = record.get("content_understanding") or {}
        roles[str(understanding.get("document_role_label") or "未识别")] += 1
        record_types[str(record.get("record_type") or "document")] += 1
        languages[str(record.get("source_language") or "unknown")] += 1
        attachments += int(record.get("attachment_count") or 0)
        characters += int(record.get("source_char_count") or 0)
    return {
        "role_counts": dict(roles),
        "record_type_counts": dict(record_types),
        "language_counts": dict(languages),
        "attachment_count": attachments,
        "source_characters": characters,
    }


def analyze_homogeneous_documents(
    documents, minimum_documents=2, cancel_check=None, progress_callback=None,
    record_callback=None,
):
    records = []
    scanned = 0
    for item in documents or ():
        scanned += 1
        if cancel_check and scanned % 32 == 0:
            cancel_check()
        path = item.get("path") if isinstance(item, dict) else None
        payload = item.get("payload") if isinstance(item, dict) else None
        if isinstance(item, dict) and isinstance(item.get("record"), dict):
            # Resume entries are already bounded extracted records from the
            # previous time slice; do not rehydrate their original sidecars.
            record = dict(item["record"])
            path = record.get("path")
        else:
            if not path or not isinstance(payload, dict):
                continue
            record = extract_record(path, payload)
        if record["content_available"]:
            records.append(record)
            if record_callback:
                record_callback(record)
        if progress_callback and (scanned % 100 == 0):
            progress_callback(scanned, len(records))

    if cancel_check:
        cancel_check()

    total = len(records)
    schema_fields = []
    for key, label, _pattern in FIELD_DEFINITIONS:
        present = sum(bool(item["fields"].get(key)) for item in records)
        schema_fields.append({
            "key": key,
            "label": label,
            "present": present,
            "coverage": round(present / max(1, total), 4),
        })
    custom_counts, custom_labels = Counter(), {}
    for record in records:
        seen_custom_keys = set()
        for label, value in (record.get("custom_fields") or {}).items():
            key = _normalise_label(label)
            if key and value and key not in seen_custom_keys:
                custom_counts[key] += 1
                custom_labels.setdefault(key, label)
                seen_custom_keys.add(key)
    for key, present in custom_counts.most_common(30):
        schema_fields.append({
            "key": "custom:" + key,
            "label": custom_labels[key],
            "present": present,
            "coverage": round(present / max(1, total), 4),
            "custom": True,
        })
    stable_fields = [item for item in schema_fields if item["coverage"] >= 0.6]
    observed_fields = [item for item in schema_fields if item["coverage"] > 0]
    structural_score = round(
        sum(item["coverage"] for item in observed_fields) / max(1, len(observed_fields)), 4
    )
    structured_eligible = total >= max(1, int(minimum_documents)) and len(stable_fields) >= 2
    relation_signal_count = sum(
        1 for record in records
        if (
            record.get("entities")
            or record.get("references")
            or any((record.get("fields") or {}).get(key) for key in (
                "sender", "recipient", "message_id", "in_reply_to", "references_header",
            ))
        )
    )
    relation_eligible = total >= max(1, int(minimum_documents)) and (
        relation_signal_count > 0 or structured_eligible
    )
    eligible = relation_eligible
    reasons = []
    if total < max(1, int(minimum_documents)):
        reasons.append("可用文档数量不足")
    if not structured_eligible:
        reasons.append("未发现至少两个稳定公共字段")
        reasons.append("公共字段不足，已切换到邮件头、正文和实体关系分析")
    if not relation_eligible and total >= max(1, int(minimum_documents)):
        reasons.append("未发现可用于关联的邮件头、正文或实体信号")

    relations = _build_relations(records) if eligible else []
    for record in records:
        record["relation_stage"] = "relation_indexed" if eligible else "normalized"
    cases = _build_cases(records, relations) if eligible else []
    anomalies = _build_anomalies(records, relations, schema_fields) if eligible else []
    entity_statistics = _entity_statistics(records)
    communication_statistics = _communication_statistics(records)
    content_profile = _content_profile(records)
    relation_counts = Counter(item["relation_type"] for item in relations)
    explicit_relations = sum(1 for item in relations if item.get("confidence_components", {}).get("explicit_reference"))
    high_confidence_relations = sum(1 for item in relations if float(item.get("confidence") or 0) >= 0.85)
    fingerprint_source = "\n".join(
        "{}\0{}\0{}".format(item.get("path") or "", item.get("source_fingerprint") or "", item.get("source_char_count") or 0)
        for item in sorted(records, key=lambda value: str(value.get("path") or ""))
    )
    input_fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8", errors="replace")).hexdigest()
    truncated_count = sum(1 for item in records if item.get("text_truncated"))
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_version": SCHEMA_VERSION,
        "input_fingerprint": input_fingerprint,
        "integrity": {
            "text_truncated_files": truncated_count,
            "relation_candidate_count": sum(1 for item in relations if item.get("relation_status") == "candidate"),
            "validated_relation_count": sum(1 for item in relations if item.get("relation_status") == "validated"),
            "relations_are_complete": len(relations) < MAX_RELATIONS,
            "relation_scope": "content_records" if eligible else "not_eligible",
            "relation_coverage": round(total / max(1, scanned), 4),
            "entity_records": sum(1 for item in records if item.get("entity_keys")),
            "email_record_count": sum(1 for item in records if item.get("record_type") == "email"),
            "structured_eligible": structured_eligible,
            "relation_eligible": relation_eligible,
            "relation_signal_count": relation_signal_count,
            "entity_index_complete": True,
        },
        "status": "completed",
        "eligible": eligible,
        "structured_eligible": structured_eligible,
        "relation_eligible": relation_eligible,
        "eligibility_reasons": reasons,
        "document_count": total,
        "structural_score": structural_score,
        "stable_field_count": len(stable_fields),
        "schema_fields": schema_fields,
        "records": records,
        "relations": relations,
        "cases": cases,
        "anomalies": anomalies,
        "entity_statistics": entity_statistics,
        "communication_statistics": communication_statistics,
        "content_profile": content_profile,
        "metrics": {
            "document_count": total,
            "relationship_count": len(relations),
            "case_count": len(cases),
            "anomaly_count": len(anomalies),
            "relation_types": dict(relation_counts),
            "explicit_reference_count": explicit_relations,
            "high_confidence_relation_count": high_confidence_relations,
            "relation_evidence_coverage": round(
                sum(bool(item.get("evidence") or item.get("evidence_refs")) for item in relations) / max(1, len(relations)), 4
            ),
            "entity_count": sum(len(item.get("entities") or ()) for item in records),
            "unique_entity_count": len(entity_statistics),
            "communication_pair_count": len(communication_statistics),
            "attachment_count": content_profile["attachment_count"],
            "source_characters": content_profile["source_characters"],
            "structured_document_count": sum(1 for item in records if item.get("record_type") == "document"),
            "email_record_count": sum(1 for item in records if item.get("record_type") == "email"),
            "candidate_relation_count": sum(1 for item in relations if item.get("relation_status") == "candidate"),
            "validated_relation_count": sum(1 for item in relations if item.get("relation_status") == "validated"),
        },
    }
