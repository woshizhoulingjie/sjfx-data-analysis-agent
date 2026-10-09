"""RFC 5322/MIME parser that keeps the original message as bytes."""

from __future__ import annotations

import hashlib
import base64
import binascii
import quopri
import re
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


def _raw_body(raw):
    pieces = re.split(br"\r\n\r\n|\n\n|\r\r", raw, maxsplit=1)
    return pieces[1] if len(pieces) == 2 else b""


def _raw_parts(message, raw, prefix=()):
    """Locate original MIME bytes, without reserialising attached messages.

    The stdlib parses message/rfc822 children before applying transfer
    encoding. QP soft breaks can consequently be mistaken for MIME headers.
    Retain the corresponding wire payload for decoding and for attachments.
    """
    yield prefix or (1,), raw
    attached = message.get_content_disposition() == "attachment" or bool(message.get_filename() and not message.get_content_disposition())
    if message.get_content_type().casefold() == "message/rfc822" or not message.is_multipart() or attached:
        return
    boundary = message.get_boundary()
    if not boundary:
        return
    try:
        pattern = re.compile(br"(?m)^--" + re.escape(boundary.encode("ascii")) + br"(--)?[ \t]*\r?$")
    except UnicodeError:
        return
    body = _raw_body(raw)
    markers = list(pattern.finditer(body))
    children = iter(message.iter_parts())
    position = 0
    for index, marker in enumerate(markers):
        if marker.group(1):
            break
        child = next(children, None)
        if child is None:
            break
        position += 1
        start = marker.end()
        if body[start:start+2] == b"\r\n": start += 2
        elif body[start:start+1] in {b"\n", b"\r"}: start += 1
        end = markers[index+1].start() if index+1 < len(markers) else len(body)
        child_raw = body[start:end]
        if index+1 < len(markers):
            if child_raw.endswith(b"\r\n"): child_raw = child_raw[:-2]
            elif child_raw.endswith((b"\n", b"\r")): child_raw = child_raw[:-1]
        yield from _raw_parts(child, child_raw, prefix + (position,))


def _part_payload(part, warnings, raw_part=None):
    if part.get_content_type().casefold() == "message/rfc822":
        encoding = str(part.get("Content-Transfer-Encoding") or "").strip().casefold()
        if raw_part is not None:
            original = _raw_body(raw_part)
            if encoding == "quoted-printable":
                return quopri.decodestring(original)
            if encoding == "base64":
                compact = b"".join(original.split())
                try:
                    return base64.b64decode(compact, validate=True)
                except binascii.Error:
                    warnings.append("嵌套邮件 Base64 编码异常；原件已保留")
                    try:
                        return base64.b64decode(compact + b"=" * (-len(compact) % 4))
                    except binascii.Error:
                        return b""
            return original
        nested = part.get_payload()
        if isinstance(nested, list):
            try:
                encoding = str(part.get("Content-Transfer-Encoding") or "").strip().casefold()
                # The stdlib treats encoded message/rfc822 as a child message
                # whose body is still encoded. Decode that body before parsing
                # the attached message; serialising it adds spurious headers.
                if encoding in {"base64", "quoted-printable"} and len(nested) == 1:
                    value = nested[0].get_payload()
                    if not list(nested[0].raw_items()) and isinstance(value, str):
                        encoded = value.encode("ascii", errors="surrogateescape")
                    else:
                        # QP leaves ASCII header names intact, so the parser
                        # may already have interpreted them as child headers.
                        encoded = nested[0].as_bytes(policy=policy.default.clone(refold_source="none"))
                    if isinstance(encoded, bytes):
                        if encoding == "quoted-printable":
                            return quopri.decodestring(encoded)
                        compact = b"".join(encoded.split())
                        try:
                            return base64.b64decode(compact, validate=True)
                        except binascii.Error:
                            warnings.append("嵌套邮件 Base64 编码异常；原件已保留")
                            return base64.b64decode(compact + b"=" * (-len(compact) % 4))
                return b"\n".join(item.as_bytes(policy=policy.default) for item in nested)
            except (TypeError, ValueError, UnicodeError, binascii.Error) as exc:
                warnings.append("嵌套邮件读取失败：{}".format(type(exc).__name__))
                return b""
    if part.is_multipart():
        # A multipart attachment is an attached MIME document. Its children
        # must neither become the enclosing body nor disappear on download.
        return raw_part if raw_part is not None else part.as_bytes(policy=policy.default)
    try:
        payload = part.get_payload(decode=True)
    except Exception as exc:
        warnings.append("MIME 部件解码失败：{}".format(type(exc).__name__))
        return b""
    return payload if isinstance(payload, bytes) else b""


def _decode_text(payload, charset, warnings):
    # A BOM supplies an encoding only when MIME did not declare one. Do not
    # guess a legacy encoding or silently conceal invalid input as success.
    if not charset:
        charset = "utf-8-sig" if payload.startswith(b"\xef\xbb\xbf") else "utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    charset = str(charset).strip() or "utf-8"
    # Windows-874 is a common MIME label for Thai Windows mail, but Python's
    # codec registry exposes it as cp874 (unlike windows-1252 aliases).
    if charset.casefold() in {"windows-874", "windows874"}:
        charset = "cp874"
    try:
        return payload.decode(charset)
    except LookupError:
        warnings.append("未知或错误字符集：{}".format(charset))
        return payload.decode("utf-8", errors="replace")
    except UnicodeError:
        warnings.append("文本含无效编码字节；保留原件，解码文本不完整")
        try:
            return payload.decode(charset, errors="replace")
        except UnicodeError:
            return payload.decode("utf-8", errors="replace")


def _mime_path(indexes):
    return ".".join(str(value) for value in indexes)


def _walk(message, prefix=(), body_allowed=True):
    # Keep forwarded RFC 822 messages as an explicit MIME part. Their inner
    # bodies are not silently promoted into the enclosing message's body.
    attached = message.get_content_disposition() == "attachment" or bool(message.get_filename() and not message.get_content_disposition())
    if message.get_content_type().casefold() == "message/rfc822" or (message.is_multipart() and attached):
        yield prefix or (1,), message, body_allowed
        return
    if message.is_multipart():
        children = list(message.iter_parts())
        root_position = 1
        if message.get_content_type().casefold() == "multipart/related":
            start = message.get_param("start")
            if start:
                root_position = next((n for n, child in enumerate(children, start=1) if str(child.get("Content-ID") or "").strip() == str(start).strip()), 1)
        for position, child in enumerate(children, start=1):
            child_body = body_allowed and (message.get_content_type().casefold() != "multipart/related" or position == root_position)
            yield from _walk(child, prefix + (position,), child_body)
    else:
        yield prefix or (1,), message, body_allowed


def _max_nested_message_depth(message, depth=0):
    current = depth + int(message.get_content_type().casefold() == "message/rfc822")
    deepest = current
    if message.is_multipart():
        for child in message.iter_parts():
            deepest = max(deepest, _max_nested_message_depth(child, current))
    return deepest


def parse_eml(raw: bytes, *, max_parts=1000, max_nested_message_depth=3) -> ParsedMailMessage:
    warnings = []
    content_complete = True
    message = BytesParser(policy=policy.default).parsebytes(raw)
    initial_defects = tuple(message.defects)
    if initial_defects:
        content_complete = False
    for defect in initial_defects:
        warnings.append("邮件 MIME 结构解析异常：{}".format(type(defect).__name__))
    if _max_nested_message_depth(message) > max(0, int(max_nested_message_depth)):
        warnings.append("嵌套邮件深度超过策略限制；仅保留外层 MIME 部件")
    headers = {}
    for name, value in message.items():
        headers.setdefault(str(name).casefold(), []).append(str(value))
    date_raw = next((value for name,value in message.raw_items() if name.casefold()=='date'),None)
    sent_at_utc, timezone_offset = parse_mail_date(date_raw)
    if not sent_at_utc:
        warnings.append("发送日期缺失、格式错误或缺少时区；未使用导入时间替代")
    for name, value in message.items():
        for defect in getattr(value, "defects", ()):
            warnings.append("邮件头 {} 解析异常：{}".format(name, type(defect).__name__))
    ids = message_id_tokens(message.get_all("Message-ID", []))
    if len(ids) > 1:
        warnings.append("存在多个 Message-ID，回复关联需检查")
    participants = []
    roles = (("from", "From"), ("sender", "Sender"), ("reply_to", "Reply-To"), ("to", "To"), ("cc", "Cc"), ("bcc", "Bcc"))
    for role, header_name in roles:
        entries = getaddresses(message.get_all(header_name, []))
        for position, (display_name, address) in enumerate(entries):
            if address:
                participants.append((role, address, position, _decoded_header(display_name) or None))
    parts = []
    raw_parts = dict(_raw_parts(message, raw))
    for index, (path_indexes, part, body_allowed) in enumerate(_walk(message), start=1):
        if index > max_parts:
            warnings.append("MIME 部件数超过策略限制")
            content_complete = False
            break
        warning_start = len(warnings)
        content_type = part.get_content_type().casefold()
        disposition = (part.get_content_disposition() or "").casefold() or None
        filename = part.get_filename()
        filename = _decoded_header(filename) if filename else None
        content_id = part.get("Content-ID")
        payload = _part_payload(part, warnings, raw_parts.get(path_indexes))
        # Transfer decoding may add defects (for example invalid base64 padding)
        # without raising an exception. The original remains downloadable.
        for defect in part.defects:
            if part is message and defect in initial_defects:
                continue
            warnings.append("MIME 部件解析异常：{}".format(type(defect).__name__))
        # A filename alone can identify an attachment when the sender omitted
        # Content-Disposition.  It must not override an explicit ``inline``
        # disposition, which represents a CID/embedded resource in the mail
        # UI and is deliberately excluded from the default attachment list.
        explicit_attachment = disposition == "attachment" or bool(filename and disposition is None)
        inline_resource = not explicit_attachment and (bool(content_id) or disposition == "inline")
        if content_type == "text/plain" and not explicit_attachment and body_allowed:
            role = "body_plain"
        elif content_type == "text/html" and not explicit_attachment and body_allowed:
            role = "body_html"
        elif content_type == "message/rfc822":
            role = "nested_message"
        elif explicit_attachment:
            role = "attachment"
        else:
            role = "inline"
        text = None
        html_raw = None
        if role == "body_plain":
            text = _decode_text(payload, part.get_content_charset(), warnings)
        elif role == "body_html":
            rendered = _decode_text(payload, part.get_content_charset(), warnings)
            html_raw = rendered
            from ..html_safety import html_to_translation_text
            text = html_to_translation_text(rendered)
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
            html_raw=html_raw,
            charset=part.get_content_charset(),
            content_complete=len(warnings) == warning_start,
            warnings=warnings[warning_start:],
        ))
    if not any((part.text or "").strip() for part in parts if part.part_role.startswith("body_")):
        warnings.append("邮件正文为空或没有可读取的正文部件；原件与附件仍保留")
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
        content_complete=content_complete and all(p.content_complete for p in parts),
    )
