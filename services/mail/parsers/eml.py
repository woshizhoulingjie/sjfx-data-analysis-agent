"""RFC 5322/MIME parser that keeps the original message as bytes."""

from __future__ import annotations

import hashlib
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses

from ..html_safety import html_to_text, sanitize_html
from ..models import ParsedMailMessage, ParsedMimePart
from ..normalization import message_id_tokens, parse_mail_date


def _decoded_header(value):
    try:
        return str(make_header(decode_header(str(value or ""))))
    except Exception:
        return str(value or "")


def _part_payload(part, warnings):
    if part.get_content_type().casefold() == "message/rfc822":
        nested = part.get_payload()
        if isinstance(nested, list):
            try:
                return b"\n".join(item.as_bytes(policy=policy.default) for item in nested)
            except (TypeError, ValueError, UnicodeError) as exc:
                warnings.append("嵌套邮件读取失败：{}".format(type(exc).__name__))
                return b""
    try:
        payload = part.get_payload(decode=True)
    except Exception as exc:
        warnings.append("MIME 部件解码失败：{}".format(type(exc).__name__))
        return b""
    return payload if isinstance(payload, bytes) else b""


def _decode_text(payload, charset, warnings):
    charset = str(charset or "utf-8").strip() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        warnings.append("未知或错误字符集：{}".format(charset))
        return payload.decode("utf-8", errors="replace")


def _mime_path(indexes):
    return ".".join(str(value) for value in indexes)


def _walk(message, prefix=()):
    # Keep forwarded RFC 822 messages as an explicit MIME part. Their inner
    # bodies are not silently promoted into the enclosing message's body.
    if message.get_content_type().casefold() == "message/rfc822":
        yield prefix or (1,), message
        return
    if message.is_multipart():
        for position, child in enumerate(message.iter_parts(), start=1):
            yield from _walk(child, prefix + (position,))
    else:
        yield prefix or (1,), message


def _max_nested_message_depth(message, depth=0):
    current = depth + int(message.get_content_type().casefold() == "message/rfc822")
    deepest = current
    if message.is_multipart():
        for child in message.iter_parts():
            deepest = max(deepest, _max_nested_message_depth(child, current))
    return deepest


def parse_eml(raw: bytes, *, max_parts=1000, max_nested_message_depth=3) -> ParsedMailMessage:
    warnings = []
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if _max_nested_message_depth(message) > max(0, int(max_nested_message_depth)):
        warnings.append("嵌套邮件深度超过策略限制；仅保留外层 MIME 部件")
    headers = {}
    for name, value in message.items():
        headers.setdefault(str(name).casefold(), []).append(str(value))
    date_raw = message.get("Date")
    sent_at_utc, timezone_offset = parse_mail_date(date_raw)
    participants = []
    roles = (("from", "From"), ("sender", "Sender"), ("reply_to", "Reply-To"), ("to", "To"), ("cc", "Cc"), ("bcc", "Bcc"))
    for role, header_name in roles:
        entries = getaddresses(message.get_all(header_name, []))
        for position, (display_name, address) in enumerate(entries):
            if address:
                participants.append((role, address, position, _decoded_header(display_name) or None))
    parts = []
    for index, (path_indexes, part) in enumerate(_walk(message), start=1):
        if index > max_parts:
            warnings.append("MIME 部件数超过策略限制")
            break
        content_type = part.get_content_type().casefold()
        disposition = (part.get_content_disposition() or "").casefold() or None
        filename = part.get_filename()
        filename = _decoded_header(filename) if filename else None
        content_id = part.get("Content-ID")
        payload = _part_payload(part, warnings)
        # A filename alone can identify an attachment when the sender omitted
        # Content-Disposition.  It must not override an explicit ``inline``
        # disposition, which represents a CID/embedded resource in the mail
        # UI and is deliberately excluded from the default attachment list.
        explicit_attachment = disposition == "attachment" or bool(
            filename and disposition is None and content_type not in {"text/plain", "text/html"}
        )
        inline_resource = not explicit_attachment and (bool(content_id) or disposition == "inline")
        if content_type == "text/plain" and not explicit_attachment:
            role = "body_plain"
        elif content_type == "text/html" and not explicit_attachment:
            role = "body_html"
        elif content_type == "message/rfc822":
            role = "nested_message"
        elif explicit_attachment:
            role = "attachment"
        else:
            role = "inline"
        text = None
        if role == "body_plain":
            text = _decode_text(payload, part.get_content_charset(), warnings)
        elif role == "body_html":
            rendered = _decode_text(payload, part.get_content_charset(), warnings)
            text = html_to_text(rendered)
        parts.append(ParsedMimePart(
            mime_path=_mime_path(path_indexes),
            parent_mime_path=_mime_path(path_indexes[:-1]) if len(path_indexes) > 1 else None,
            content_type=content_type,
            content_disposition=disposition,
            content_id=str(content_id) if content_id else None,
            filename_raw=filename,
            transfer_encoding=part.get("Content-Transfer-Encoding"),
            byte_size=len(payload),
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            part_role=role,
            is_explicit_attachment=explicit_attachment,
            is_inline_resource=inline_resource,
            text=text,
            raw_payload=payload,
        ))
    return ParsedMailMessage(
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        headers_raw=headers,
        message_id_raw=message_id_tokens(message.get_all("Message-ID", [])),
        in_reply_to_raw=message_id_tokens(message.get_all("In-Reply-To", [])),
        references_raw=message_id_tokens(message.get_all("References", [])),
        date_raw=str(date_raw) if date_raw else None,
        sent_at_utc=sent_at_utc,
        timezone_offset_minutes=timezone_offset,
        parse_status="parsed" if not warnings else "parsed_with_warnings",
        warnings=warnings,
        participants=participants,
        parts=parts,
    )
