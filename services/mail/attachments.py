"""Attachment payload lookup without exposing a filesystem path to clients."""

from __future__ import annotations

from .parsers.eml import parse_eml
from .source import open_locator


def read_message_raw(repository, message_key, owner_id, package_id):
    source = repository.message_source(message_key, owner_id, package_id)
    if not source:
        raise ValueError("原始邮件定位记录不存在")
    with open_locator(source["locator"]) as stream:
        return stream.read()


def read_part(repository, message_key, owner_id, package_id, mime_path):
    raw = read_message_raw(repository, message_key, owner_id, package_id)
    parsed = parse_eml(raw)
    for part in parsed.parts:
        if part.mime_path == str(mime_path):
            return part, raw
    raise ValueError("邮件部件不存在")
