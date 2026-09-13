"""Small, dependency-free DTOs shared by the mail domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO, Iterator


@dataclass(frozen=True)
class SourceRef:
    package_id: str
    kind: str
    path: str
    source_revision: str


@dataclass(frozen=True)
class MailMember:
    name: str
    ordinal: int
    byte_size: int
    locator: dict


@dataclass
class ParsedMimePart:
    mime_path: str
    parent_mime_path: str | None
    content_type: str
    content_disposition: str | None
    content_id: str | None
    filename_raw: str | None
    transfer_encoding: str | None
    byte_size: int
    payload_sha256: str
    part_role: str
    is_explicit_attachment: bool
    is_inline_resource: bool
    text: str | None = None
    raw_payload: bytes | None = None


@dataclass
class ParsedMailMessage:
    raw_sha256: str
    headers_raw: dict[str, list[str]]
    message_id_raw: list[str]
    in_reply_to_raw: list[str]
    references_raw: list[str]
    date_raw: str | None
    sent_at_utc: str | None
    timezone_offset_minutes: int | None
    parse_status: str
    warnings: list[str] = field(default_factory=list)
    participants: list[tuple[str, str, int, str | None]] = field(default_factory=list)
    parts: list[ParsedMimePart] = field(default_factory=list)


class MailContainerAdapter:
    """Protocol-shaped base class kept deliberately independent of plugins."""

    format_name = "unknown"

    def supports(self, source: SourceRef) -> bool:
        raise NotImplementedError

    def inventory(self, source: SourceRef, policy) -> Iterator[MailMember]:
        raise NotImplementedError

    def open_message(self, source: SourceRef, member: MailMember) -> BinaryIO:
        raise NotImplementedError
