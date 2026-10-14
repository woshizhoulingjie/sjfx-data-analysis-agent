"""Durable, ordered chunk uploads with owner/session isolation.

A persisted chunk intent bridges data fsync, completion rename and manifest
publication. Session locks cover recovery as well as writes, across manager
instances and processes. Completed bytes are verified, never inferred only
from a filename or length. Schema-1 manifests remain readable.
"""

import errno
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
import weakref
from contextlib import contextmanager

MAX_CHUNK_BYTES = 4 * 1024 * 1024 - 65536
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024 * 1024
_UNSAFE_SEG = re.compile(r'[/\\\x00-\x1f<>:"|?*]')
_SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_SHA256 = re.compile(r'^[0-9a-fA-F]{64}$')
_DEVICE = re.compile(r'^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)
_THREAD_LOCKS = weakref.WeakValueDictionary()
_THREAD_LOCKS_GUARD = threading.Lock()


class UploadError(Exception):
    """Expected upload failure suitable for the API response."""


def safe_rel_path(rel_path):
    if not isinstance(rel_path, str) or not rel_path:
        raise UploadError("文件路径为空")
    raw = rel_path.replace('\\', '/')
    if not raw.strip() or len(raw) > 1024 or raw.startswith('/'):
        raise UploadError("文件路径为空、过长或为绝对路径")
    parts = []
    for seg in raw.split('/'):
        if not seg or seg == '.':
            continue
        if (seg == '..' or _UNSAFE_SEG.search(seg) or seg.endswith(('.', ' '))
                or seg != seg.strip() or _DEVICE.match(seg)):
            raise UploadError("文件路径含非法或不安全的名称")
        if len(seg.encode('utf-8')) > 255:
            raise UploadError("文件名过长（超过 255 字节）")
        parts.append(seg)
    if not parts:
        raise UploadError("文件路径无效")
    return '/'.join(parts)


def _check_id(value, label):
    if (not isinstance(value, str) or not _SAFE_ID.fullmatch(value)
            or value.endswith('.') or _DEVICE.match(value)):
        raise UploadError("%s 含非法字符" % label)
    return value


def _digest(value, optional=True):
    if value is None or value == '':
        if optional:
            return None
        raise UploadError("缺少 SHA-256")
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise UploadError("SHA-256 必须为 64 位十六进制字符串")
    return value.lower()


def _is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _assert_no_links(path):
    """Reject existing symlink/junction/reparse components, including ancestors."""
    current = os.path.abspath(path)
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            pass
        else:
            if _is_link(info):
                raise UploadError("上传路径包含符号链接或重解析点")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


def _mkdir(path):
    _assert_no_links(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    _assert_no_links(path)
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise UploadError("上传路径不是目录")


def _within(path, root):
    try:
        path = os.path.normcase(os.path.realpath(path))
        root = os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([path, root]) == root
    except (ValueError, OSError):
        return False


def _sync_dir(path):
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_open(path, flags, mode=0o600):
    """Open a regular file; POSIX walks parents with no-follow descriptors."""
    path = os.path.abspath(path)
    _assert_no_links(path)
    fd = None
    directories = []
    try:
        if os.name != 'nt' and os.open in os.supports_dir_fd:
            pieces = path.split(os.sep)
            parent = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
            directories.append(parent)
            for piece in pieces[1:-1]:
                parent = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                directories.append(parent)
            fd = os.open(pieces[-1], flags | os.O_NOFOLLOW, mode, dir_fd=parent)
        else:
            fd = os.open(path, flags | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0), mode)
        opened = os.fstat(fd)
        _assert_no_links(path)
        current = os.lstat(path)
        if (not stat.S_ISREG(opened.st_mode) or _is_link(current)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise UploadError("上传文件路径或身份发生变化")
        return fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        for directory in reversed(directories):
            os.close(directory)


def _stamp(fd):
    st = os.fstat(fd)
    value = {'device': st.st_dev, 'inode': st.st_ino, 'size': st.st_size,
             'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns}
    if os.name == 'nt':
        # Windows st_ctime is creation time. Use NTFS ChangeTime as well so
        # restoring mtime cannot hide a changed same-size file from the cache.
        import ctypes
        import msvcrt
        class BasicInfo(ctypes.Structure):
            _fields_ = [('creation', ctypes.c_longlong), ('access', ctypes.c_longlong),
                        ('write', ctypes.c_longlong), ('change', ctypes.c_longlong),
                        ('attributes', ctypes.c_ulong)]
        info = BasicInfo()
        function = ctypes.windll.kernel32.GetFileInformationByHandleEx
        function.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        function.restype = ctypes.c_int
        if function(msvcrt.get_osfhandle(fd), 0, ctypes.byref(info), ctypes.sizeof(info)):
            value['change_time'] = info.change
        else:
            # No stable change identity available: callers must rehash.
            value['cacheable'] = False
    return value


def _regular_exists(path):
    _assert_no_links(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode):
        raise UploadError("上传文件不是普通文件")
    return True


def _session_locked(method):
    @functools.wraps(method)
    def wrapped(self, owner_id, session_id, *args, **kwargs):
        with self._locked(owner_id, session_id):
            return method(self, owner_id, session_id, *args, **kwargs)
    return wrapped


class UploadManager:
    def __init__(self, root, max_file_bytes=DEFAULT_MAX_FILE_BYTES,
                 max_chunk_bytes=MAX_CHUNK_BYTES, session_ttl_seconds=7 * 86400):
        self.root = os.path.abspath(root)
        self.max_file_bytes = int(max_file_bytes)
        self.max_chunk_bytes = int(max_chunk_bytes)
        self.session_ttl_seconds = int(session_ttl_seconds)
        if self.max_file_bytes < 0 or self.max_chunk_bytes <= 0:
            raise UploadError("上传大小上限非法")
        _mkdir(self.root)

    def _session_dir(self, owner_id, session_id):
        _check_id(owner_id, 'owner_id')
        _check_id(session_id, 'session_id')
        path = os.path.join(self.root, owner_id, session_id)
        _assert_no_links(path)
        return path

    def _manifest_path(self, owner_id, session_id):
        return os.path.join(self._session_dir(owner_id, session_id), 'session.json')

    @contextmanager
    def _locked(self, owner_id, session_id):
        self._session_dir(owner_id, session_id)
        directory = os.path.join(self.root, '.locks', owner_id)
        _mkdir(directory)
        path = os.path.join(directory, session_id + '.lock')
        with _THREAD_LOCKS_GUARD:
            lock = _THREAD_LOCKS.get(os.path.normcase(path))
            if lock is None:
                lock = threading.RLock()
                _THREAD_LOCKS[os.path.normcase(path)] = lock
        with lock:
            fd = _safe_open(path, os.O_RDWR | os.O_CREAT)
            held = False
            try:
                if os.name == 'nt':
                    import msvcrt
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b'\0')
                    os.lseek(fd, 0, os.SEEK_SET)
                    while True:
                        try:
                            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                            break
                        except OSError as exc:
                            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                                raise
                            time.sleep(0.05)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                held = True
                yield
            finally:
                if held:
                    if os.name == 'nt':
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _load(self, owner_id, session_id):
        try:
            with os.fdopen(_safe_open(self._manifest_path(owner_id, session_id), os.O_RDONLY), 'r', encoding='utf-8') as source:
                manifest = json.load(source)
        except FileNotFoundError:
            raise UploadError("上传会话不存在或已过期")
        except (OSError, ValueError) as exc:
            raise UploadError("上传会话清单损坏：%r" % exc)
        if (not isinstance(manifest, dict) or manifest.get('owner_id') != owner_id
                or manifest.get('session_id') != session_id or not isinstance(manifest.get('files'), list)):
            raise UploadError("上传会话清单归属或结构不符")
        try:
            limit = manifest['max_chunk_bytes']
            if not isinstance(limit, int) or limit <= 0 or manifest['file_count'] != len(manifest['files']):
                raise ValueError('invalid manifest counts')
            for entry in manifest['files']:
                if not isinstance(entry, dict) or safe_rel_path(entry['rel_path']) != entry['rel_path']:
                    raise ValueError('invalid file entry')
                if any(not isinstance(entry[k], int) or entry[k] < 0
                       for k in ('size', 'chunks_total', 'chunks_done', 'received_bytes')):
                    raise ValueError('invalid file counts')
                if (entry['chunks_total'] != (entry['size'] + limit - 1) // limit
                        or entry['chunks_done'] > entry['chunks_total'] or entry['received_bytes'] > entry['size']
                        or entry['state'] not in ('pending', 'complete')
                        or (entry['state'] == 'complete' and (entry['chunks_done'] != entry['chunks_total']
                                                            or entry['received_bytes'] != entry['size']))):
                    raise ValueError('inconsistent file counts')
                _digest(entry.get('sha256'))
                _digest(entry.get('actual_sha256'))
            if manifest['total_bytes'] != sum(f['size'] for f in manifest['files']):
                raise ValueError('invalid manifest size')
        except (KeyError, TypeError, ValueError) as exc:
            raise UploadError("上传会话清单结构损坏：%s" % exc)
        return manifest

    def _save(self, owner_id, session_id, manifest):
        manifest['updated_at'] = time.time()
        path = self._manifest_path(owner_id, session_id)
        temporary = path + '.tmp.' + uuid.uuid4().hex
        try:
            with os.fdopen(_safe_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'w', encoding='utf-8') as output:
                json.dump(manifest, output, ensure_ascii=False, indent=1)
                output.flush()
                os.fsync(output.fileno())
            _assert_no_links(path)
            os.replace(temporary, path)
            _sync_dir(os.path.dirname(path))
        finally:
            if os.path.lexists(temporary):
                _assert_no_links(temporary)
                os.unlink(temporary)

    def _paths(self, manifest, index):
        folder = os.path.join(self._session_dir(manifest['owner_id'], manifest['session_id']), 'files')
        return os.path.join(folder, '%d.part' % index), os.path.join(folder, '%d.done' % index)

    def _hash_file(self, path, entry, use_cache=True):
        with os.fdopen(_safe_open(path, os.O_RDONLY), 'rb') as source:
            before = _stamp(source.fileno())
            if before['size'] != entry['size']:
                raise UploadError("文件 %s 长度不符" % entry['rel_path'])
            actual = entry.get('actual_sha256')
            if not (use_cache and actual and entry.get('verified_source') == before
                    and before.get('cacheable', True)):
                digest = hashlib.sha256()
                receipts = list(entry.get('chunk_receipts', {}).values())
                if entry.get('pending_chunk'):
                    receipts.append(entry['pending_chunk'])
                position = 0
                for receipt in sorted(receipts, key=lambda row: row['offset']):
                    offset, length = int(receipt['offset']), int(receipt['size'])
                    if offset < position or length <= 0 or offset + length > entry['size']:
                        raise UploadError("上传分片摘要记录不一致")
                    for segment_size, chunk_digest in ((offset - position, None), (length, hashlib.sha256())):
                        remaining = segment_size
                        while remaining:
                            block = source.read(min(1024 * 1024, remaining))
                            if not block:
                                raise UploadError("上传分片字节丢失")
                            digest.update(block)
                            if chunk_digest is not None:
                                chunk_digest.update(block)
                            remaining -= len(block)
                        if chunk_digest is not None and chunk_digest.hexdigest() != receipt['sha256']:
                            raise UploadError("已确认分片完整 SHA-256 校验失败")
                    position = offset + length
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
                actual = digest.hexdigest()
            if before != _stamp(source.fileno()):
                raise UploadError("文件在校验过程中发生变化")
        expected = _digest(entry.get('sha256'))
        recorded = entry.get('actual_sha256')
        if (expected and actual != expected) or (recorded and actual != recorded):
            raise UploadError("文件 %s 完整 SHA-256 校验失败" % entry['rel_path'])
        entry['actual_sha256'] = actual
        entry['verified_source'] = before
        return actual

    def _slice_hash(self, path, offset, length):
        with os.fdopen(_safe_open(path, os.O_RDONLY), 'rb') as source:
            source.seek(offset)
            data = source.read(length)
        if len(data) != length:
            raise UploadError("已接收分片丢失或长度不符")
        return hashlib.sha256(data).hexdigest()

    def _truncate(self, path, size):
        with os.fdopen(_safe_open(path, os.O_RDWR), 'r+b') as output:
            output.truncate(size)
            output.flush()
            os.fsync(output.fileno())

    def _recover_entry(self, manifest, index, verify_complete=True):
        entry = manifest['files'][index]
        if entry['size'] == 0:
            actual = hashlib.sha256(b'').hexdigest()
            if _digest(entry.get('sha256')) not in (None, actual):
                raise UploadError("空文件完整 SHA-256 校验失败")
            entry['actual_sha256'] = actual
            return
        part, done = self._paths(manifest, index)
        has_part, has_done = _regular_exists(part), _regular_exists(done)
        if has_part and has_done:
            raise UploadError("上传同时存在 part/done，拒绝覆盖已完成文件")
        assembled = str(index) in manifest.get('assembled_files', {})
        if not has_part and not has_done:
            if verify_complete and (assembled or manifest.get('assembling_file') == index):
                record = manifest.get('assembled_files', {}).get(str(index), {})
                destination = manifest.get('assembly_destination') or manifest.get('assembled_to')
                if not destination:
                    raise UploadError("已组装文件缺少目标目录记录")
                target = os.path.join(destination, *safe_rel_path(entry['rel_path']).split('/'))
                if not _regular_exists(target):
                    raise UploadError("已组装文件丢失")
                if not assembled and not entry.get('actual_sha256') and not entry.get('sha256'):
                    raise UploadError("旧组装中断缺少可信摘要，保留现场供核验")
                probe = dict(entry, actual_sha256=entry.get('actual_sha256') or record.get('sha256'),
                             verified_source=record.get('verified_source') or entry.get('verified_source'))
                self._hash_file(target, probe)
                if probe['verified_source'] != record.get('verified_source'):
                    entry['actual_sha256'] = probe['actual_sha256']
                    if assembled:
                        record.update(sha256=probe['actual_sha256'], verified_source=probe['verified_source'])
                    else:
                        entry['verified_source'] = probe['verified_source']
                    self._save(manifest['owner_id'], manifest['session_id'], manifest)
            if entry['received_bytes'] and not assembled and manifest.get('assembling_file') != index:
                raise UploadError("已确认上传字节丢失，拒绝从错误偏移续传")
            return
        path = done if has_done else part
        intent = entry.get('pending_chunk')
        if intent:
            offset, length = int(intent['offset']), int(intent['size'])
            if (intent['index'] != entry['chunks_done'] or offset != entry['received_bytes']
                    or length <= 0 or length > manifest['max_chunk_bytes']):
                raise UploadError("上传分片恢复记录不一致")
            size = os.path.getsize(path)
            if size < offset:
                raise UploadError("已确认上传前缀丢失")
            valid = size == offset + length and self._slice_hash(path, offset, length) == intent['sha256']
            if not valid:
                if has_done:
                    raise UploadError("已完成文件与分片恢复记录不一致")
                self._truncate(part, offset)
                entry.pop('pending_chunk', None)
                self._save(manifest['owner_id'], manifest['session_id'], manifest)
                return
            last = entry['chunks_done'] + 1 == entry['chunks_total']
            if last:
                try:
                    self._hash_file(path, entry, use_cache=False)
                except UploadError:
                    if not has_done:
                        self._truncate(part, offset)
                        entry.pop('pending_chunk', None)
                        self._save(manifest['owner_id'], manifest['session_id'], manifest)
                    raise
                if not has_done:
                    os.rename(part, done)
                    _sync_dir(os.path.dirname(done))
                    with os.fdopen(_safe_open(done, os.O_RDONLY), 'rb') as source:
                        entry['verified_source'] = _stamp(source.fileno())
                entry['state'] = 'complete'
            elif has_done:
                raise UploadError("非末片出现已完成文件")
            receipt = {key: intent[key] for key in ('index', 'offset', 'size', 'sha256')}
            entry.setdefault('chunk_receipts', {})[str(intent['index'])] = receipt
            entry['chunks_done'] += 1
            entry['received_bytes'] += length
            entry.pop('pending_chunk', None)
            self._save(manifest['owner_id'], manifest['session_id'], manifest)
        elif has_done:
            if entry['state'] != 'complete':
                # Legacy crash after rename: the done file, not stale received
                # bytes, is authoritative, but only after full SHA validation.
                old_count, old_bytes = entry['chunks_done'], entry['received_bytes']
                self._hash_file(done, entry, use_cache=False)
                if old_count == entry['chunks_total'] - 1:
                    length = entry['size'] - old_bytes
                    entry.setdefault('chunk_receipts', {})[str(old_count)] = {
                        'index': old_count, 'offset': old_bytes, 'size': length,
                        'sha256': self._slice_hash(done, old_bytes, length)}
                entry.update(state='complete', chunks_done=entry['chunks_total'], received_bytes=entry['size'])
                self._save(manifest['owner_id'], manifest['session_id'], manifest)
            elif verify_complete:
                prior = entry.get('verified_source')
                self._hash_file(done, entry)
                if prior != entry.get('verified_source'):
                    self._save(manifest['owner_id'], manifest['session_id'], manifest)
        else:
            if os.path.getsize(part) < entry['received_bytes']:
                raise UploadError("已确认上传前缀丢失")
            if os.path.getsize(part) > entry['received_bytes']:
                # Schema-1 has no intent; only its confirmed prefix is durable.
                self._truncate(part, entry['received_bytes'])
            if entry['state'] == 'complete':
                raise UploadError("完成状态缺少已完成文件")

    @_session_locked
    def begin(self, owner_id, session_id, files, package_name=None):
        if not isinstance(files, list) or not files:
            raise UploadError("文件列表为空")
        prepared, seen, directories = [], set(), set()
        for item in files:
            if not isinstance(item, dict):
                raise UploadError("文件列表结构非法")
            rel = safe_rel_path(item.get('rel_path'))
            # Portable case-insensitive collision and file/directory aliases.
            key = rel.casefold()
            pieces = key.split('/')
            parents = ['/'.join(pieces[:index]) for index in range(1, len(pieces))]
            if key in seen or key in directories or any(parent in seen for parent in parents):
                raise UploadError("文件列表里有重复或冲突路径：%s" % rel)
            seen.add(key)
            directories.update(parents)
            try:
                size = int(item.get('size'))
            except (TypeError, ValueError):
                raise UploadError("文件 %s 缺少合法大小" % rel)
            if size < 0 or size > self.max_file_bytes:
                raise UploadError("文件 %s 大小非法或超过单文件上限" % rel)
            digest = _digest(item.get('sha256'))
            if size == 0 and digest not in (None, hashlib.sha256(b'').hexdigest()):
                raise UploadError("空文件完整 SHA-256 校验失败")
            prepared.append({'rel_path': rel, 'size': size, 'sha256': digest,
                             'chunks_total': (size + self.max_chunk_bytes - 1) // self.max_chunk_bytes,
                             'chunks_done': 0, 'received_bytes': 0,
                             'state': 'complete' if not size else 'pending', 'chunk_receipts': {},
                             **({'actual_sha256': hashlib.sha256(b'').hexdigest()} if not size else {})})
        path = self._manifest_path(owner_id, session_id)
        if os.path.lexists(path):
            existing = self._load(owner_id, session_id)
            fields = ('rel_path', 'size', 'sha256')
            if ([tuple(f.get(k) for k in fields) for f in existing['files']]
                    != [tuple(f.get(k) for k in fields) for f in prepared]
                    or existing.get('package_name') != package_name):
                raise UploadError("上传会话已存在，拒绝覆盖不同文件清单")
            return existing
        sdir = self._session_dir(owner_id, session_id)
        if os.path.isdir(sdir) and os.listdir(sdir):
            raise UploadError("上传会话缺少清单但仍有文件，拒绝覆盖")
        _mkdir(os.path.join(sdir, 'files'))
        manifest = {'schema': 2, 'owner_id': owner_id, 'session_id': session_id,
                    'package_name': package_name, 'created_at': time.time(),
                    'max_chunk_bytes': self.max_chunk_bytes, 'file_count': len(prepared),
                    'total_bytes': sum(f['size'] for f in prepared), 'files': prepared}
        self._save(owner_id, session_id, manifest)
        return manifest

    @_session_locked
    def status(self, owner_id, session_id):
        m = self._load(owner_id, session_id)
        for index in range(len(m['files'])):
            self._recover_entry(m, index)
        done = sum(f['state'] == 'complete' for f in m['files'])
        return {'session_id': m['session_id'], 'file_count': m['file_count'],
                'files_complete': done, 'total_bytes': m['total_bytes'],
                'received_bytes': sum(f['received_bytes'] for f in m['files']),
                'max_chunk_bytes': m['max_chunk_bytes'], 'complete': done == m['file_count'],
                'files': [{k: f.get(k) for k in ('rel_path', 'size', 'chunks_total', 'chunks_done',
                                                'received_bytes', 'state', 'actual_sha256')} for f in m['files']]}

    def _retry_path(self, m, index):
        part, done = self._paths(m, index)
        if _regular_exists(done):
            return done
        if _regular_exists(part):
            return part
        dest = m.get('assembly_destination') or m.get('assembled_to')
        if dest and (str(index) in m.get('assembled_files', {}) or m.get('assembling_file') == index):
            path = os.path.join(dest, *safe_rel_path(m['files'][index]['rel_path']).split('/'))
            if _regular_exists(path):
                return path
        raise UploadError("重试分片的原始字节不存在")

    @_session_locked
    def put_chunk(self, owner_id, session_id, file_index, chunk_index, data, chunk_sha256=None):
        m = self._load(owner_id, session_id)
        try:
            file_index = int(file_index)
        except (TypeError, ValueError):
            raise UploadError("file_index 非法")
        if file_index < 0 or file_index >= len(m['files']):
            raise UploadError("file_index 越界")
        self._recover_entry(m, file_index)
        entry = m['files'][file_index]
        if not isinstance(data, (bytes, bytearray, memoryview)) or not data:
            raise UploadError("分片内容为空或类型非法")
        data = bytes(data)
        limit = min(self.max_chunk_bytes, int(m['max_chunk_bytes']))
        if len(data) > limit:
            raise UploadError("分片超过上限（%d 字节）" % limit)
        actual = hashlib.sha256(data).hexdigest()
        expected_hash = _digest(chunk_sha256)
        if expected_hash and actual != expected_hash:
            raise UploadError("分片校验失败")
        try:
            chunk_index = entry['chunks_done'] if chunk_index is None else int(chunk_index)
        except (TypeError, ValueError):
            raise UploadError("chunk_index 非法")
        expected = entry['chunks_done']
        if chunk_index < 0:
            raise UploadError("chunk_index 非法")
        if chunk_index < expected:
            if entry['state'] == 'complete' and chunk_index != expected - 1:
                raise UploadError("该文件已收完")
            receipt = entry.get('chunk_receipts', {}).get(str(chunk_index))
            if receipt:
                offset, length = receipt['offset'], receipt['size']
                if length != len(data) or receipt['sha256'] != actual:
                    raise UploadError("重试分片与已接收内容不一致")
            else:
                # Legacy chunks have no receipts. Only compare saved bytes;
                # never treat an unknown index as proof that content matches.
                if entry['state'] == 'complete':
                    offset, length = entry['size'] - len(data), len(data)
                elif entry['received_bytes'] == expected * m['max_chunk_bytes']:
                    offset, length = chunk_index * m['max_chunk_bytes'], m['max_chunk_bytes']
                else:
                    raise UploadError("旧清单缺少该分片边界，不能安全接受重试")
                if length != len(data):
                    raise UploadError("重试分片长度不一致")
            if self._slice_hash(self._retry_path(m, file_index), offset, length) != actual:
                raise UploadError("重试分片与已保存字节不一致")
            return {'already': True, 'chunks_done': expected, 'chunks_total': entry['chunks_total'],
                    'file_complete': entry['state'] == 'complete', 'actual_sha256': entry.get('actual_sha256'),
                    'session_complete': all(f['state'] == 'complete' for f in m['files'])}
        if entry['state'] == 'complete':
            raise UploadError("该文件已收完")
        if chunk_index != expected or expected >= entry['chunks_total']:
            raise UploadError("分片乱序：期望 %d，收到 %d" % (expected, chunk_index))
        if entry['received_bytes'] + len(data) > entry['size']:
            raise UploadError("收到的字节超出声明大小")
        part, done = self._paths(m, file_index)
        if _regular_exists(done):
            raise UploadError("已存在完成件，拒绝覆盖")
        new_part = not _regular_exists(part)
        entry['pending_chunk'] = {'index': expected, 'offset': entry['received_bytes'],
                                  'size': len(data), 'sha256': actual}
        self._save(owner_id, session_id, m)
        with os.fdopen(_safe_open(part, os.O_RDWR | os.O_CREAT), 'r+b') as output:
            if os.fstat(output.fileno()).st_size != entry['received_bytes']:
                raise UploadError("分片文件长度与已确认偏移不一致")
            output.seek(entry['received_bytes'])
            if output.write(data) != len(data):
                raise OSError("分片写入不完整")
            output.flush()
            os.fsync(output.fileno())
        if new_part:
            _sync_dir(os.path.dirname(part))
        self._recover_entry(m, file_index)
        if entry['chunks_done'] != expected + 1:
            raise UploadError("写入分片未通过持久化校验，请重传")
        return {'already': False, 'chunks_done': entry['chunks_done'], 'chunks_total': entry['chunks_total'],
                'file_complete': entry['state'] == 'complete', 'actual_sha256': entry.get('actual_sha256'),
                'session_complete': all(f['state'] == 'complete' for f in m['files'])}

    def _copy_verified(self, source_path, temporary, entry):
        digest = hashlib.sha256()
        with os.fdopen(_safe_open(source_path, os.O_RDONLY), 'rb') as source:
            before = _stamp(source.fileno())
            with os.fdopen(_safe_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as output:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
                    if output.write(block) != len(block):
                        raise OSError("组装写入不完整")
                output.flush()
                os.fsync(output.fileno())
            if before != _stamp(source.fileno()) or before['size'] != entry['size']:
                raise UploadError("组装期间上传源文件发生变化")
        actual = digest.hexdigest()
        if any(want and want != actual for want in (_digest(entry.get('sha256')), entry.get('actual_sha256'))):
            raise UploadError("组装文件完整 SHA-256 校验失败")
        entry['actual_sha256'] = actual
        return actual

    @_session_locked
    def assemble(self, owner_id, session_id, dest_dir, allowed_root=None):
        m = self._load(owner_id, session_id)
        for index in range(len(m['files'])):
            self._recover_entry(m, index, verify_complete=False)
        pending = [f['rel_path'] for f in m['files'] if f['state'] != 'complete']
        if pending:
            raise UploadError("还有 %d 个文件未传完，例如 %s" % (len(pending), pending[0]))
        dest_dir = os.path.abspath(dest_dir)
        _assert_no_links(dest_dir)
        if allowed_root:
            _assert_no_links(allowed_root)
            if not _within(dest_dir, allowed_root):
                raise UploadError("目标目录必须位于允许根之下")
        prior_dest = m.get('assembly_destination') or m.get('assembled_to')
        if prior_dest and os.path.normcase(os.path.abspath(prior_dest)) != os.path.normcase(dest_dir):
            raise UploadError("该上传会话已开始组装到另一个目录，拒绝改写目标")
        _mkdir(dest_dir)
        m.setdefault('assembled_files', {})
        m['assembly_destination'] = dest_dir
        self._save(owner_id, session_id, m)
        written = []
        for index, entry in enumerate(m['files']):
            rel = safe_rel_path(entry['rel_path'])
            target = os.path.join(dest_dir, *rel.split('/'))
            _assert_no_links(target)
            if not _within(target, dest_dir):
                raise UploadError("组装目标路径越界")
            _mkdir(os.path.dirname(target))
            part, source = self._paths(m, index)
            record = m['assembled_files'].get(str(index))
            exists = _regular_exists(target)
            if record:
                if record.get('rel_path') != rel or record.get('size') != entry['size'] or not exists:
                    raise UploadError("已组装文件与会话记录不一致")
                if record.get('sha256') and entry.get('actual_sha256') not in (None, record['sha256']):
                    raise UploadError("已组装文件摘要记录不一致")
                probe = dict(entry, actual_sha256=entry.get('actual_sha256') or record.get('sha256'),
                             verified_source=record.get('verified_source'))
                actual = self._hash_file(target, probe)
            elif m.get('assembling_file') == index and exists:
                probe = dict(entry)
                if not entry.get('actual_sha256') and not entry.get('sha256'):
                    if not _regular_exists(source):
                        raise UploadError("旧组装中断缺少可信摘要，保留现场供核验")
                    self._hash_file(source, probe, use_cache=False)
                actual = self._hash_file(target, probe, use_cache=False)
            else:
                if exists:
                    raise UploadError("目标文件已存在，拒绝覆盖：%s" % target)
                m['assembling_file'] = index
                self._save(owner_id, session_id, m)
                temporary = os.path.join(os.path.dirname(target), '.sjfx-assemble-' + uuid.uuid4().hex + '.tmp')
                try:
                    if entry['size']:
                        if not _regular_exists(source):
                            raise UploadError("已完成分片丢失")
                        actual = self._copy_verified(source, temporary, entry)
                        _assert_no_links(source)
                        _assert_no_links(temporary)
                        shutil.copystat(source, temporary, follow_symlinks=False)
                    else:
                        with os.fdopen(_safe_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as output:
                            output.flush()
                            os.fsync(output.fileno())
                        actual = hashlib.sha256(b'').hexdigest()
                    _assert_no_links(target)
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        raise UploadError("目标文件已存在，拒绝覆盖")
                    except OSError:
                        # Preserve exclusive/no-overwrite behavior where links
                        # are unavailable. A partial target is never accepted.
                        try:
                            fd = _safe_open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                        except FileExistsError:
                            raise UploadError("目标文件已存在，拒绝覆盖")
                        created_stamp = _stamp(fd)
                        try:
                            with os.fdopen(fd, 'wb') as output, os.fdopen(_safe_open(temporary, os.O_RDONLY), 'rb') as staged:
                                shutil.copyfileobj(staged, output, 1024 * 1024)
                                output.flush()
                                os.fsync(output.fileno())
                        except BaseException:
                            if _regular_exists(target):
                                st = os.stat(target)
                                if (st.st_dev, st.st_ino) == (created_stamp['device'], created_stamp['inode']):
                                    os.unlink(target)
                            raise
                    _sync_dir(os.path.dirname(target))
                finally:
                    if os.path.lexists(temporary):
                        _assert_no_links(temporary)
                        os.unlink(temporary)
                with os.fdopen(_safe_open(target, os.O_RDONLY), 'rb') as placed:
                    probe = dict(entry, verified_source=_stamp(placed.fileno()))
            entry['actual_sha256'] = actual
            m['assembled_files'][str(index)] = {'rel_path': rel, 'size': entry['size'],
                                                 'sha256': actual, 'verified_source': probe['verified_source']}
            m['assembling_file'] = None
            # Persist receipt before removing the only uploaded source. A
            # failure here leaves both copies and a recoverable placement intent.
            self._save(owner_id, session_id, m)
            if _regular_exists(source):
                os.unlink(source)
                _sync_dir(os.path.dirname(source))
            written.append({'rel_path': rel, 'path': target, 'size': entry['size'], 'sha256': actual})
        m['assembled_at'] = time.time()
        m['assembled_to'] = dest_dir
        self._save(owner_id, session_id, m)
        return {'ok': True, 'file_count': len(written), 'dest_dir': dest_dir, 'files': written}

    @_session_locked
    def abort(self, owner_id, session_id):
        sdir = self._session_dir(owner_id, session_id)
        if not os.path.isdir(sdir):
            raise UploadError("上传会话不存在或已过期")
        if not _within(sdir, self.root) or sdir == self.root:
            raise UploadError("上传清理路径越界")
        _assert_no_links(sdir)
        shutil.rmtree(sdir)
        return True

    def purge_expired(self, now=None):
        now = time.time() if now is None else now
        removed = []
        _assert_no_links(self.root)
        for owner in os.listdir(self.root):
            try:
                _check_id(owner, 'owner_id')
                directory = os.path.join(self.root, owner)
                _assert_no_links(directory)
                if not os.path.isdir(directory):
                    continue
                for session in os.listdir(directory):
                    try:
                        with self._locked(owner, session):
                            m = self._load(owner, session)
                            if now - float(m.get('updated_at') or 0) > self.session_ttl_seconds:
                                sdir = self._session_dir(owner, session)
                                shutil.rmtree(sdir)
                                removed.append(owner + '/' + session)
                    except (UploadError, OSError, ValueError):
                        continue
            except (UploadError, OSError):
                continue
        return removed
