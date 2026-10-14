"""Attachment payload lookup without exposing a filesystem path to clients."""

from __future__ import annotations

from .parsers.eml import parse_eml
from .source import open_locator


def read_message_raw(repository, message_key, owner_id, package_id, occurrence_id=None):
    import hashlib
    message = repository.get_message(message_key, owner_id, package_id)
    if not message:
        raise LookupError("邮件不存在或不属于当前资料包")
    digest = message['raw_sha256']
    if occurrence_id:
        occurrence = next((item for item in message['occurrences'] if item['occurrence_id'] == occurrence_id), None)
        if not occurrence:
            raise LookupError('原始副本不存在或不属于当前邮件与资料包')
        digest = occurrence['raw_sha256']
    with repository._connection() as c:
        cached = c.execute("SELECT raw_bytes FROM mail_raw_store WHERE raw_sha256=?", (digest,)).fetchone()
    if cached:
        raw=bytes(cached[0])
        if hashlib.sha256(raw).hexdigest()!=digest:
            raise ValueError('已保存原始邮件哈希校验失败')
        return raw
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
            return part, raw
    raise ValueError("邮件部件不存在")
