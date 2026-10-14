"""Resource and path policy for untrusted mail containers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class MailPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class MailPolicy:
    allowed_roots: tuple[Path, ...]
    max_archive_entries: int
    max_message_bytes: int
    max_attachment_bytes: int
    max_total_uncompressed_bytes: int
    max_compression_ratio: float
    max_member_depth: int
    max_mime_parts: int
    max_nested_message_depth: int
    # Reserved multi-user upload namespace.  Kept optional for small unit
    # tests that construct MailPolicy directly.
    upload_root: Path | None = None
    native_cache_root: Path | None = None

    @classmethod
    def from_config(cls, config):
        return cls(
            allowed_roots=tuple(Path(item).resolve() for item in config.SCAN_ALLOWED_ROOTS),
            max_archive_entries=int(getattr(config, "MAX_ARCHIVE_ENTRIES", 5000)),
            max_message_bytes=int(getattr(config, "MAIL_MAX_MESSAGE_BYTES", 50 * 1024 * 1024)),
            max_attachment_bytes=int(getattr(config, "MAIL_MAX_ATTACHMENT_BYTES", 512 * 1024 * 1024)),
            max_total_uncompressed_bytes=int(getattr(config, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 4 * 1024 * 1024 * 1024)),
            max_compression_ratio=float(getattr(config, "MAX_ARCHIVE_COMPRESSION_RATIO", 200)),
            max_member_depth=int(getattr(config, "MAX_ARCHIVE_MEMBER_PATH_DEPTH", 32)),
            max_mime_parts=int(getattr(config, "MAIL_MAX_MIME_PARTS_PER_MESSAGE", 1000)),
            max_nested_message_depth=int(getattr(config, "MAIL_MAX_NESTED_MESSAGE_DEPTH", 3)),
            upload_root=Path(config.OUTPUT_DIR) / "_uploads",
            native_cache_root=Path(config.DATA_DIR) / "mail-native",
        )

    def resolve_source(self, raw_path: str, *, owner_id: str | None = None,
                       require_exists: bool = True) -> Path:
        value = str(raw_path or "").strip()
        if not value or "\x00" in value:
            raise MailPolicyError("邮件来源路径无效")
        # Uploads are a reserved tree.  Check lexical and resolved ownership
        # before the general allow-list, otherwise an ordinary allowed root
        # could be used to alias another user's private upload session.
        uploaded = resolved_upload = None
        if self.upload_root is not None:
            try:
                from sjfx_multiuser.scan_paths import authorize_uploaded_scan_path
                uploaded = authorize_uploaded_scan_path(
                    value, self.upload_root, owner_id, check_filesystem=True
                )
            except ValueError as exc:
                raise MailPolicyError(str(exc)) from exc
        path = Path(value).expanduser().resolve(strict=require_exists)
        if self.upload_root is not None:
            try:
                resolved_upload = authorize_uploaded_scan_path(
                    path, self.upload_root, owner_id, check_filesystem=True
                )
            except ValueError as exc:
                raise MailPolicyError(str(exc)) from exc
        if require_exists and not (path.is_file() or path.is_dir()):
            raise MailPolicyError("邮件来源不是文件或目录")
        if uploaded is not None or resolved_upload is not None:
            return path
        for root in self.allowed_roots:
            try:
                path.relative_to(root)
                return path
            except ValueError:
                continue
        raise MailPolicyError("邮件来源不在服务器允许的资料目录内")

    def resolve_locator(self, locator, *, owner_id=None, source_kind=None,
                        source_revision=None):
        """Re-authorize an adapter locator immediately before a source read."""
        value = dict(locator or {})
        kind = value.get("kind")
        if kind == "nested":
            value["parent"] = self.resolve_locator(
                value.get("parent"), owner_id=owner_id,
                source_kind=source_kind, source_revision=source_revision,
            )
            return value
        if kind not in {"file", "zip", "mbox"} or not value.get("path"):
            raise MailPolicyError("邮件来源定位器不可用")
        if source_kind in {"msg", "pst"} and kind == "file":
            # Native adapters emit EML files under this exact revision cache,
            # which intentionally lives outside SCAN_ALLOWED_ROOTS.
            import hashlib
            from sjfx_multiuser.uploads import UploadError, _assert_no_links
            if self.native_cache_root is None or not source_revision:
                raise MailPolicyError("原生邮件转换缓存不可用")
            cache = self.native_cache_root / hashlib.sha256(
                str(source_revision).encode()
            ).hexdigest()
            candidate = Path(value["path"]).expanduser()
            try:
                candidate.absolute().relative_to(cache.absolute())
                _assert_no_links(candidate)
                candidate = candidate.resolve(strict=True)
                candidate.relative_to(cache.resolve(strict=True))
            except (ValueError, OSError, UploadError) as exc:
                raise MailPolicyError("原生邮件来源越出转换缓存") from exc
            if not candidate.is_file():
                raise MailPolicyError("原生邮件转换缓存不是文件")
        else:
            candidate = self.resolve_source(value["path"], owner_id=owner_id)
        value["path"] = str(candidate)
        return value

    def validate_member_name(self, member_name: str) -> str:
        candidate = str(member_name or "").replace("\\", "/")
        if not candidate or candidate.startswith("/") or "\x00" in candidate:
            raise MailPolicyError("压缩包成员路径无效")
        parts = [part for part in candidate.split("/") if part]
        if any(part in {".", ".."} for part in parts) or len(parts) > self.max_member_depth:
            raise MailPolicyError("压缩包成员路径不安全")
        return "/".join(parts)

    def validate_size(self, size: int, *, limit: int, label: str) -> None:
        if int(size) < 0 or int(size) > int(limit):
            raise MailPolicyError("{}超过资源限制".format(label))

    def validate_archive_member(self, compressed_size: int, uncompressed_size: int) -> None:
        self.validate_size(uncompressed_size, limit=self.max_message_bytes, label="邮件成员")
        if compressed_size <= 0:
            if uncompressed_size > 0:
                raise MailPolicyError("压缩包成员压缩信息无效")
            return
        if float(uncompressed_size) / float(compressed_size) > self.max_compression_ratio:
            raise MailPolicyError("压缩包成员压缩比超过限制")

    def is_supported_source_suffix(self, path: Path) -> bool:
        return path.suffix.casefold() in {".eml", ".mbox", ".mbx", ".msg", ".pst", ".zip"}
