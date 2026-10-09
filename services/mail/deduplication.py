"""Conservative identity for copies whose transport headers differ.

Message-ID alone never proves that two source records are the same message.
All non-transport headers, recipient roles and decoded MIME payloads must agree.
Every occurrence still retains its own original bytes and hash.
"""
from __future__ import annotations

import hashlib
import json
import re

from .normalization import normalize_message_id

_TRANSPORT_HEADERS = {
    'received', 'return-path', 'delivered-to', 'x-original-to',
    'x-uid', 'x-uidl', 'x-mozilla-status', 'x-mozilla-status2', 'status', 'x-status',
}


def copy_fingerprint(parsed):
    identifiers = parsed.headers_raw.get('message-id') or []
    if len(identifiers) != 1 or len(parsed.message_id_raw) != 1:
        return None
    identifier = normalize_message_id(parsed.message_id_raw[0])
    if not identifier or '@' not in identifier or parsed.parse_status != 'parsed' or not getattr(parsed, 'content_complete', True):
        return None
    headers = {
        name: [re.sub(r'\r?\n[ \t]+', ' ', value).strip() for value in values]
        for name, values in sorted(parsed.headers_raw.items())
        if name not in _TRANSPORT_HEADERS
    }
    parts = []
    for part in parsed.parts:
        if not getattr(part, 'content_complete', True):
            return None
        payload = part.raw_payload or b''
        if part.content_type.startswith('text/'):
            payload = payload.replace(b'\r\n', b'\n')
        parts.append((part.mime_path, part.parent_mime_path, part.content_type,
                      part.content_disposition, part.content_id, part.filename_raw,
                      part.part_role, hashlib.sha256(payload).hexdigest()))
    value = [identifier, headers, sorted(parsed.participants, key=str), parts]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
