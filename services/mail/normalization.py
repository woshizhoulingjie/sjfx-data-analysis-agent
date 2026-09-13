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
    """Keep the local part byte-for-byte; case-fold only the domain."""
    if not value:
        return None
    token = str(value).strip()
    if not (token.startswith("<") and token.endswith(">")):
        return None
    inner = token[1:-1]
    if "@" not in inner or not inner or any(ch.isspace() for ch in inner):
        return None
    local, domain = inner.rsplit("@", 1)
    return "<{}@{}>".format(local, domain.casefold())


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
    offset = parsed.utcoffset()
    offset_minutes = int(offset.total_seconds() // 60) if offset is not None else None
    return parsed.astimezone(timezone.utc).isoformat(), offset_minutes


def normalized_subject(value: str | None) -> str:
    text = " ".join(str(value or "").split())
    while True:
        stripped = re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)", "", text, flags=re.I)
        if stripped == text:
            return text.casefold()
        text = stripped
