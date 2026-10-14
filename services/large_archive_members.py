"""Verified archive-member identities shared by large-package consumers.

Metadata is spooled to private SQLite before any member is published. A changed
container, duplicate name or failed scan cannot publish a half-valid member set.
No source is extracted during discovery and no member path is a filesystem path.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from config import Config
from services.scanner import is_sensitive_file
from services.unified_parser import strict_archive_member_name
from services.archive_formats import ArchiveReader, ArchiveLimits, SUFFIXES

IDENTITY_FIELDS = ('container_path', 'container_size', 'container_modified_at_ns',
                   'container_device', 'container_inode', 'member_name', 'member_size')


def supported(path):
    return str(path).lower().endswith(SUFFIXES)


def member_node(row):
    """Validate persisted metadata; never trust caller-supplied logical flags."""
    if row.get('source_kind') != 'archive_member':
        return None
    meta = row.get('source_metadata')
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            meta = None
    if not isinstance(meta, dict) or meta.get('version') != 1:
        raise ValueError('归档成员缺少可信来源身份，请重新盘点')
    container, member = meta.get('container_path'), meta.get('member_name')
    if (not strict_archive_member_name(container) or not supported(container)
            or not strict_archive_member_name(member) or '::' in container
            or row.get('path') != container + '::' + member):
        raise ValueError('归档成员来源路径不一致')
    for field in IDENTITY_FIELDS[1:5] + ('member_size',):
        if type(meta.get(field)) is not int or meta[field] < 0:
            raise ValueError('归档成员来源身份字段无效')
    if (int(row.get('size') or 0) != meta['member_size']
            or int(row.get('modified_at_ns', row.get('mtime', 0)) or 0) != meta['container_modified_at_ns']):
        raise ValueError('归档成员清单与来源身份不一致')
    return {**dict(row), 'logical_unit': True, 'logical_kind': 'archive_member',
            'kind': 'file', 'name': Path(member).name, 'extension': Path(member).suffix.lower(),
            **{k: meta[k] for k in IDENTITY_FIELDS}, 'source_metadata': dict(meta),
            'size': meta['member_size'], 'modified_at_ns': meta['container_modified_at_ns'],
            'content_analysis_allowed': not bool(row.get('sensitive')), 'promotion_allowed': True}


def source_signature(row):
    node = member_node(row)
    if node is None:
        return ''
    return hashlib.sha256(json.dumps(node['source_metadata'], sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def source_identity(root, row):
    """Revalidate the inventoried container through no-follow file descriptors."""
    from services.logical_units import container_node_for
    from services.package_analysis import _open_inventory_source
    node = member_node(row)
    if node is None:
        raise ValueError('不是归档成员')
    fd, opened = _open_inventory_source(root, container_node_for(node))
    os.close(fd)
    return (Path(root) / node['container_path']).resolve(), (
        opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)


@contextmanager
def member_snapshot(root, row, *, prefix_bytes=None):
    from services.package_analysis import _logical_source_snapshot
    node = member_node(row)
    if node is None or node.get('sensitive'):
        raise ValueError('归档成员不存在或只允许读取元数据')
    with _logical_source_snapshot(root, node, max_snapshot_bytes=prefix_bytes) as path:
        yield path
    source_identity(root, row)


def discover_members(root, container, on_error=None):
    """Yield a verified member inventory with disk-bounded metadata staging."""
    if not supported(container['path']) or container.get('sensitive'):
        return
    from services.package_analysis import _open_inventory_source
    Config.PARSE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    source_fd, before = _open_inventory_source(root, container)
    with os.fdopen(source_fd, 'rb') as handle, tempfile.TemporaryDirectory(
            prefix='sjfx-member-index-', dir=str(Config.PARSE_TEMP_DIR)) as scratch:
        db = sqlite3.connect(str(Path(scratch) / 'members.db'))
        try:
            db.execute('CREATE TABLE members(name TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE names(name TEXT PRIMARY KEY)')
            count = 0
            def store(name, size, regular, encrypted=False, compressed_size=None):
                nonlocal count
                count += 1
                if count > Config.MAX_LOGICAL_UNITS_PER_CONTAINER:
                    raise ValueError('归档成员数量超过逻辑清点上限')
                try:
                    db.execute('INSERT INTO names VALUES(?)', (name,))
                except sqlite3.IntegrityError as exc:
                    raise ValueError('归档成员名称重复，未发布该归档的成员清单') from exc
                if name.endswith('/') and not regular:
                    return
                if not regular or encrypted or not strict_archive_member_name(name) or int(size) < 0:
                    if on_error:
                        on_error(container['path'] + '::' + str(name), '成员名称、类型或加密状态不允许独立读取')
                    return
                meta = {'version': 1, 'container_path': container['path'],
                        'container_size': before.st_size, 'container_modified_at_ns': before.st_mtime_ns,
                        'container_device': before.st_dev, 'container_inode': before.st_ino,
                        'member_name': name, 'member_size': int(size)}
                if compressed_size is not None:
                    meta['compressed_size'] = int(compressed_size)
                row = {'path': container['path'] + '::' + name, 'name': Path(name).name,
                       'extension': Path(name).suffix.lower(), 'source_kind': 'archive_member',
                       'size': int(size), 'modified_at_ns': before.st_mtime_ns,
                       'sensitive': bool(container.get('sensitive')) or is_sensitive_file(Path(name).name),
                       'source_metadata': meta}
                try:
                    db.execute('INSERT INTO members VALUES(?,?)', (name, json.dumps(row, ensure_ascii=False)))
                except sqlite3.IntegrityError as exc:
                    raise ValueError('归档成员名称重复，未发布该归档的成员清单') from exc
            limits = ArchiveLimits.from_config(entry_limit=Config.MAX_LOGICAL_UNITS_PER_CONTAINER)
            with ArchiveReader(handle, filename=container['path'], limits=limits) as archive:
                for entry in archive.entries():
                    store(entry.name, entry.size, entry.regular, entry.encrypted, entry.compressed_size)
            after = os.fstat(handle.fileno())
            keys = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
            if any(getattr(before, k) != getattr(after, k) for k in keys):
                raise ValueError('归档在清点期间变化，未发布成员清单')
            db.commit()
            for payload, in db.execute('SELECT payload FROM members ORDER BY name'):
                yield json.loads(payload)
        finally:
            db.close()
