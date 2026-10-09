"""Conservative normalisation of mail identifiers and timestamps."""

from __future__ import annotations

import re
from datetime import timezone
from email.utils import parsedate_to_datetime


_MESSAGE_ID_TOKEN = re.compile(r"<[^<>\s]+>")


def message_id_tokens(values) -> list[str]:
    tokens = []
    for value in values or []:
        for token in _MESSAGE_ID_TOKEN.findall(str(value or "")):
            if token not in tokens:
                tokens.append(token)
    return tokens


def normalize_message_id(value: str | None) -> str | None:
    """Normalize domain-bearing IDs; retain safe nonstandard opaque IDs exactly.

    Missing ``@`` is not RFC 5322 syntax, but an intact ASCII bracketed token
    can still be header evidence. Consumers must label that matching basis;
    it never permits case folding or subject-based identity inference.
    """
    if not value:
        return None
    token = str(value).strip()
    if not (token.startswith("<") and token.endswith(">")):
        return None
    inner = token[1:-1]
    if not inner or any(ch.isspace() or ch in '<>' for ch in inner):
        return None
    if "@" not in inner:
        if any(ord(ch) < 33 or ord(ch) > 126 or ch == '\\' for ch in inner):
            return None
        return token
    local, domain = inner.rsplit("@", 1)
    if not local or not domain:
        return None
    return "<{}@{}>".format(local, domain.casefold())


def message_id_format(value: str | None) -> str | None:
    """Distinguish compatible opaque identifiers from domain-bearing IDs."""
    normalized = normalize_message_id(value)
    if normalized is None:
        return None
    return 'domain_bearing' if '@' in normalized[1:-1] else 'nonstandard_opaque'


def normalize_address(value: str | None) -> str | None:
    if not value:
        return None
    address = str(value).strip()
    if "@" not in address or any(ch.isspace() for ch in address):
        return None
    local, domain = address.rsplit("@", 1)
    return "{}@{}".format(local, domain.casefold())


def parse_mail_date(raw: str | None):
    if not raw:
        return None, None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None, None
    if parsed is None or parsed.tzinfo is None:
        return None, None
    try:
        offset = parsed.utcoffset()
        offset_minutes = int(offset.total_seconds() // 60) if offset is not None else None
        return parsed.astimezone(timezone.utc).isoformat(), offset_minutes
    except (ValueError, OverflowError):
        # Valid local calendar values can overflow when converted across a
        # timezone at the datetime boundaries. Preserve the raw Date instead.
        return None, None


def normalized_subject(value: str | None) -> str:
    text = " ".join(str(value or "").split())
    while True:
        stripped = re.sub(r"^(?:(?:re|fw|fwd|回复|回覆|答复|轉發|转发|轉寄|转寄)\s*[:：]\s*)", "", text, flags=re.I)
        if stripped == text:
            return text.casefold()
        text = stripped
