"""Attachment payload lookup without exposing a filesystem path to clients."""

from __future__ import annotations

from .parsers.eml import parse_eml
from .source import open_locator


def read_message_raw(repository, message_key, owner_id, package_id, occurrence_id=None):
    import hashlib
    # Resolve only stored identity/ownership here. Raw recovery must not first
    # consume an unverified historical subject/body projection via get_message.
    with repository._connection() as c:
        args = [message_key, package_id, owner_id]
        condition = ''
        if occurrence_id:
            condition = ' AND o.occurrence_id=?'
            args.append(occurrence_id)
        identity = c.execute('SELECT o.raw_sha256 FROM mail_occurrences o JOIN mail_packages p ON p.package_id=o.package_id WHERE o.message_key=? AND o.package_id=? AND p.owner_id=?' + condition + ' ORDER BY o.source_member_ordinal LIMIT 1', args).fetchone()
    if not identity:
        raise LookupError("邮件不存在或不属于当前资料包")
    digest = identity['raw_sha256']
    with repository._connection() as c:
        # A raw cache can prove itself from its exact bytes and the approved
        # manifest/derived artifact. This exception never accepts stored text,
        # summaries or model output and never writes historical provenance.
        raw_connection = getattr(c, 'raw', c)
        cached = raw_connection.execute("SELECT raw_bytes FROM mail_raw_store WHERE raw_sha256=?", (digest,)).fetchone()
    if cached:
        raw=bytes(cached[0])
        if hashlib.sha256(raw).hexdigest()!=digest:
            raise ValueError('已保存原始邮件哈希校验失败')
        from sjfx_protection.business import validated_cached_bytes
        return validated_cached_bytes(raw,'cached-message.eml',owner_id)
    import json
    with repository._connection() as c:
        sources=c.execute('SELECT s.locator_json FROM mail_sources s JOIN mail_occurrences o ON o.source_id=s.source_id WHERE o.package_id=? AND o.message_key=? AND o.raw_sha256=? ORDER BY o.source_member_ordinal',(package_id,message_key,digest)).fetchall()
    for source in sources:
        try:
            from config import Config
            from .policy import MailPolicy
            package = repository.get_package(package_id, owner_id)
            if not package:
                raise LookupError("邮件资料包不存在或不属于当前所有者")
            policy = MailPolicy.from_config(Config)
            # Historical locators may still point at a readable conversion
            # cache after the original container has been removed.
            policy.resolve_source(package['source_path'], owner_id=owner_id, require_exists=False)
            locator = policy.resolve_locator(
                json.loads(source[0]), owner_id=owner_id,
                source_kind=package['source_kind'],
                source_revision=package['source_revision'],
            )
            with open_locator(locator, policy=policy, owner_id=owner_id,
                              source_kind=package['source_kind'],
                              source_revision=package['source_revision']) as stream:
                raw=stream.read(policy.max_message_bytes + 1)
            policy.validate_size(len(raw), limit=policy.max_message_bytes, label='邮件')
            if hashlib.sha256(raw).hexdigest()==digest:return raw
        except (OSError,ValueError,KeyError):
            continue
    raise RuntimeError('原始邮件来源已不可读或内容已变化；已保存的阅读投影仍可查看，不能提供原件下载')


def read_part(repository, message_key, owner_id, package_id, mime_path):
    raw = read_message_raw(repository, message_key, owner_id, package_id)
    parsed = parse_eml(raw)
    for part in parsed.parts:
        if part.mime_path == str(mime_path):
            from sjfx_protection.business import enabled,inspect_bytes
            if enabled() and part.raw_payload:
                import hashlib
                inspect_bytes(part.raw_payload,part.filename_raw or str(mime_path),hashlib.sha256(raw).hexdigest())
            return part, raw
    raise ValueError("邮件部件不存在")
