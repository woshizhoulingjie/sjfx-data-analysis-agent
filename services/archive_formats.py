"""Bounded archive inventory and reads; member names never become output paths.

RAR requires rarfile and (for compressed members) an installed extraction tool.
7z uses py7zr's WriterFactory, never filesystem extraction or extractall.
"""
import bz2
from contextlib import contextmanager
from dataclasses import dataclass, field
import gzip
import io
import lzma
import os
from pathlib import Path
import posixpath
import stat
import struct
import tarfile
import tempfile
import zipfile

from config import Config
from services.file_capabilities import ARCHIVE_EXTENSIONS, TAR_SUFFIXES

SUFFIXES = tuple(sorted(ARCHIVE_EXTENSIONS, key=len, reverse=True))
_CHUNK = 1024 * 1024
_METADATA_LIMIT = 16 * 1024 * 1024


class ArchiveError(ValueError):
    def __init__(self, message, code='damaged'):
        super().__init__(message)
        self.code = code
        self.status = code


def supported(path):
    return str(path).lower().endswith(SUFFIXES)


def archive_kind(path):
    name = str(path).lower()
    if name.endswith(TAR_SUFFIXES):
        return 'tar'
    for suffix in ('.zip', '.rar', '.7z', '.gz', '.bz2', '.xz'):
        if name.endswith(suffix):
            return suffix[1:]
    return None


def configure_rar_backend(module=None, config=None):
    """Use a private 7zz only when no configured/PATH 7z tool is available."""
    import shutil
    if module is None:
        import rarfile as module
    if shutil.which(module.SEVENZIP_TOOL):
        return module.SEVENZIP_TOOL
    tools = Path((config or Config).TOOLS_DIR) / '7zip'
    names = ('7zz.exe', '7z.exe', '7za.exe') if os.name == 'nt' else ('7zz', '7z', '7za')
    for candidate in (directory / name for directory in (tools, tools / 'bin', tools / 'usr/bin') for name in names):
        if candidate.is_file() and (os.name == 'nt' or os.access(candidate, os.X_OK)):
            module.SEVENZIP_TOOL = str(candidate)
            return str(candidate)
    return None


@dataclass(frozen=True)
class ArchiveLimits:
    entries: int
    member_bytes: int
    total_bytes: int
    compression_ratio: float
    file_bytes: int

    @classmethod
    def from_config(cls, entry_limit=None):
        return cls(int(Config.MAX_ARCHIVE_ENTRIES if entry_limit is None else entry_limit),
                   min(int(Config.MAX_CONTENT_BYTES), int(Config.MAX_ARCHIVE_MEMBER_BYTES)),
                   int(Config.MAX_ARCHIVE_UNCOMPRESSED_BYTES),
                   float(Config.MAX_ARCHIVE_COMPRESSION_RATIO), int(Config.MAX_ARCHIVE_FILE_BYTES))


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    size: int
    regular: bool = True
    encrypted: bool = False
    compressed_size: int | None = None
    directory: bool = False
    _info: object = field(default=None, repr=False, compare=False)


def _safe_name(name):
    # Lazy import avoids a cycle when the unified parser calls this module.
    from services.unified_parser import strict_archive_member_name
    return strict_archive_member_name(name)


class _BoundedRead:
    def __init__(self, stream, limit, check, *, exact=False):
        self.stream, self.limit, self.check, self.exact = stream, limit, check, exact
        self.position = 0

    def read(self, size=-1):
        self.check()
        remaining = self.limit - self.position
        if size is None or size < 0:
            # Never delegate an unbounded read to a decompressor.
            if remaining > _METADATA_LIMIT:
                raise ArchiveError('大归档成员必须按块读取，不能一次分配全文', 'resource_limit')
            chunks = []
            while True:
                chunk = self.read(_CHUNK)
                if not chunk:
                    return b''.join(chunks)
                chunks.append(chunk)
        chunk = self.stream.read(min(size, remaining + 1, _CHUNK))
        self.position += len(chunk)
        if self.position > self.limit:
            raise ArchiveError('归档成员实际大小超过声明或解压安全上限', 'resource_limit')
        if not chunk and self.exact and self.position != self.limit:
            raise ArchiveError('归档成员提取长度与目录声明不一致')
        return chunk

    def tell(self):
        return self.position


class _BoundedTarInfo(tarfile.TarInfo):
    def _proc_pax(self, archive):
        if self.size < 0 or self.size > min(_CHUNK, Config.MAX_ARCHIVE_MEMBER_BYTES):
            raise ArchiveError('TAR扩展元数据超过安全上限', 'resource_limit')
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        if self.size < 0 or self.size > min(_CHUNK, Config.MAX_ARCHIVE_MEMBER_BYTES):
            raise ArchiveError('TAR长名称元数据超过安全上限', 'resource_limit')
        return super()._proc_gnulong(archive)

    def _proc_sparse(self, archive):
        raise ArchiveError('TAR稀疏成员不支持直接读取', 'unsupported')


class _MetadataSource(io.IOBase):
    """Reject hostile metadata allocations before library read() allocates."""
    def __init__(self, source, check):
        self.source, self.check = source, check

    def read(self, size=-1):
        self.check()
        if size is None or size < 0 or size > _METADATA_LIMIT:
            raise ArchiveError('归档元数据读取超过安全上限', 'resource_limit')
        return self.source.read(size)

    def seek(self, offset, whence=0):
        self.check()
        return self.source.seek(offset, whence)

    def tell(self):
        return self.source.tell()

    def seekable(self):
        return True

    def readable(self):
        return True


class ArchiveReader:
    def __init__(self, source, filename=None, limits=None, cancel_check=None):
        self.source = source
        self.filename = str(filename or getattr(source, 'name', source))
        self.kind = archive_kind(self.filename)
        self.limits = limits or ArchiveLimits.from_config()
        self.cancel_check = cancel_check
        self._owned = False
        self._archive = None
        self._entries = None

    def _check(self):
        if self.cancel_check is not None and self.cancel_check():
            from services.parse_isolation import ParseIsolationCancelled
            raise ParseIsolationCancelled('任务已取消，停止读取归档')

    def __enter__(self):
        if not self.kind:
            raise ArchiveError('不支持的归档格式', 'unsupported')
        if isinstance(self.source, (str, os.PathLike)):
            self.source = open(self.source, 'rb')
            self._owned = True
        try:
            self.source.seek(0, os.SEEK_END)
            self.source_size = self.source.tell()
            self.source.seek(0)
            if self.source_size > self.limits.file_bytes:
                raise ArchiveError('归档文件大小超过安全上限', 'resource_limit')
            self.total_limit = min(self.limits.total_bytes,
                                   max(1, int(self.source_size * self.limits.compression_ratio)))
            self._check()
            if self.kind == 'zip':
                from services.unified_parser import _zip_central_directory_preflight
                result = _zip_central_directory_preflight(self.source)
                if not result.get('safe'):
                    raise ArchiveError('归档中央目录预检失败：' + str(result.get('reason')))
                if result['observed_entries'] > self.limits.entries:
                    raise ArchiveError('归档成员数量超过安全上限', 'resource_limit')
                self._archive = zipfile.ZipFile(self.source)
                if len(self._archive.filelist) != result['observed_entries']:
                    raise ArchiveError('归档目录在预检后变化')
            elif self.kind == 'rar':
                self._open_rar()
            elif self.kind == '7z':
                self._open_7z()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self._archive is not None:
            self._archive.close()
        if self._owned:
            self.source.close()

    def _open_rar(self):
        try:
            import rarfile
        except ImportError as exc:
            raise ArchiveError('RAR读取需要安装 rarfile 依赖', 'dependency_missing') from exc
        configure_rar_backend(rarfile)
        count = 0
        def guard(info):
            nonlocal count
            self._check()
            count += 1
            if count > self.limits.entries + 16:
                raise ArchiveError('RAR目录记录数量超过安全上限', 'resource_limit')
            if ((info.type == rarfile.RAR_BLOCK_MAIN and info.flags & rarfile.RAR_MAIN_VOLUME)
                    or (info.type == rarfile.RAR_BLOCK_FILE and
                        info.flags & (rarfile.RAR_FILE_SPLIT_BEFORE | rarfile.RAR_FILE_SPLIT_AFTER))):
                raise ArchiveError('分卷RAR不支持独立安全读取', 'unsupported')
        try:
            self._archive = rarfile.RarFile(_MetadataSource(self.source, self._check),
                                          info_callback=guard, errors='strict')
            if self._archive.needs_password() and not self._archive.infolist():
                raise ArchiveError('RAR目录已加密，需要密码', 'encrypted')
        except rarfile.Error as exc:
            raise ArchiveError('RAR目录损坏或无法读取：' + str(exc)) from exc

    def _open_7z(self):
        try:
            import py7zr
            from py7zr import archiveinfo
            from py7zr.exceptions import ArchiveError as SevenZipError
        except ImportError as exc:
            raise ArchiveError('7z读取需要安装 py7zr>=1.1.4 依赖', 'dependency_missing') from exc
        if not hasattr(archiveinfo, 'check_count'):
            raise ArchiveError('7z安全读取需要 py7zr>=1.1.4，请升级依赖', 'dependency_missing')
        try:
            self._preflight_7z(archiveinfo)
            self.source.seek(0)
            self._archive = py7zr.SevenZipFile(_MetadataSource(self.source, self._check),
                                             mode='r', max_extract_size=self.total_limit)
        except ArchiveError:
            raise
        except py7zr.PasswordRequired as exc:
            raise ArchiveError('7z目录已加密，需要密码', 'encrypted') from exc
        except (SevenZipError, EOFError, ValueError, TypeError, struct.error, lzma.LZMAError) as exc:
            raise ArchiveError('7z目录损坏或无法读取：' + str(exc)) from exc

    def _preflight_7z(self, api):
        """Bound decoded headers and file counts before py7zr allocates a list."""
        self.source.seek(0)
        fixed = self.source.read(32)
        if len(fixed) != 32 or fixed[:6] != b'7z\xbc\xaf\x27\x1c':
            raise ArchiveError('不是有效7z文件')
        offset, size = struct.unpack_from('<QQ', fixed, 12)
        if size > _METADATA_LIMIT or 32 + offset + size > self.source_size:
            raise ArchiveError('7z头部大小超过安全上限或文件长度', 'resource_limit')
        self.source.seek(32 + offset)
        header = io.BytesIO(self.source.read(size))
        pid = header.read(1)
        if not pid:
            return
        if pid == api.PROPERTY.ENCODED_HEADER:
            if size > _CHUNK:
                raise ArchiveError('7z编码头部超过安全上限', 'resource_limit')
            self._preflight_7z_stream_counts(header, api)
            streams = api.HeaderStreamsInfo.retrieve(header)
            output = bytearray()
            position = 32
            for folder in streams.unpackinfo.folders:
                self._validate_7z_coders(folder.coders)
                declared = folder.unpacksizes[-1]
                if declared < 0 or len(output) + declared > _METADATA_LIMIT:
                    raise ArchiveError('7z解码头部超过安全上限', 'resource_limit')
                position += streams.packinfo.packpos
                self.source.seek(position)
                packed = streams.packinfo.packsizes[0]
                decoder = folder.get_decompressor(packed)
                produced = 0
                while produced < declared:
                    self._check()
                    chunk = decoder.decompress(self.source, max_length=min(_CHUNK, declared - produced))
                    if not chunk:
                        raise ArchiveError('7z编码头部损坏或已截断')
                    output.extend(chunk)
                    produced += len(chunk)
                position += packed
            header = io.BytesIO(output)
            pid = header.read(1)
        if pid != api.PROPERTY.HEADER:
            raise ArchiveError('7z头部结构无效')
        pid = header.read(1)
        if pid == api.PROPERTY.MAIN_STREAMS_INFO:
            self._preflight_7z_stream_counts(header, api)
            streams = api.StreamsInfo.retrieve(header)
            if streams.unpackinfo is not None:
                if len(streams.unpackinfo.folders) > self.limits.entries:
                    raise ArchiveError('7z数据流数量超过安全上限', 'resource_limit')
                for folder in streams.unpackinfo.folders:
                    self._validate_7z_coders(folder.coders)
            pid = header.read(1)
        if pid == api.PROPERTY.FILES_INFO:
            count = api.read_uint64(header)
            if count > self.limits.entries:
                raise ArchiveError('归档成员数量超过安全上限', 'resource_limit')
        elif pid != api.PROPERTY.END:
            raise ArchiveError('不支持的7z头部结构', 'unsupported')

    def _preflight_7z_stream_counts(self, header, api):
        """Check packed-stream/folder counts before library list allocation."""
        position = header.tell()
        try:
            pid = header.read(1)
            if pid == api.PROPERTY.PACK_INFO:
                api.read_uint64(header)  # pack position
                count = api.read_uint64(header)
                if count > self.limits.entries * 4 + 16:
                    raise ArchiveError('7z压缩数据流数量超过安全上限', 'resource_limit')
                header.seek(position + 1)
                api.PackInfo.retrieve(header)
                pid = header.read(1)
            if pid == api.PROPERTY.UNPACK_INFO:
                unpack_start = header.tell()
                if header.read(1) != api.PROPERTY.FOLDER:
                    raise ArchiveError('7z文件夹头部结构无效')
                folders = api.read_uint64(header)
                if folders > self.limits.entries + 1:
                    raise ArchiveError('7z数据流数量超过安全上限', 'resource_limit')
                header.seek(unpack_start)
                api.UnpackInfo.retrieve(header)
                if header.read(1) == api.PROPERTY.SUBSTREAMS_INFO:
                    if header.read(1) == api.PROPERTY.NUM_UNPACK_STREAM:
                        streams = 0
                        for _ in range(folders):
                            self._check()
                            streams += api.read_uint64(header)
                            if streams > self.limits.entries:
                                raise ArchiveError('7z子数据流数量超过安全上限', 'resource_limit')
        finally:
            header.seek(position)

    @staticmethod
    def _validate_7z_coders(coders):
        # py7zr's Deflate/Deflate64/Zstd adapters ignore max_length; a bounded
        # output sink cannot prevent their earlier unbounded allocation.
        bounded_methods = {b'\x00', b'\x03', b'\x21', b'\x03\x01\x01', b'\x03\x04\x01',
                           b'\x04\x02\x02', b'\x03\x03\x01\x03', b'\x03\x03\x02\x05',
                           b'\x03\x03\x04\x01', b'\x03\x03\x05\x01', b'\x03\x03\x07\x01',
                           b'\x03\x03\x08\x05'}
        if len(coders) > 4:
            raise ArchiveError('7z压缩方法链超过安全资源上限', 'resource_limit')
        dictionary_bytes = 0
        for coder in coders:
            method, properties = coder['method'], coder.get('properties') or b''
            if method == b'\x06\xf1\x07\x01':
                raise ArchiveError('7z包含加密数据，需要密码', 'encrypted')
            if method not in bounded_methods:
                raise ArchiveError('该7z压缩方法缺少有界解码支持', 'unsupported')
            if method == b'\x21' and properties:
                prop = properties[0]
                if prop > 28:  # LZMA2 dictionary <= 64 MiB.
                    raise ArchiveError('7z压缩字典超过安全内存上限', 'resource_limit')
                dictionary_bytes += (2 | (prop & 1)) << (prop // 2 + 11)
            elif method == b'\x03\x01\x01' and len(properties) >= 5:
                dictionary_bytes += struct.unpack_from('<I', properties, 1)[0]
            elif method == b'\x03\x04\x01' and len(properties) >= 5:
                dictionary_bytes += struct.unpack_from('<I', properties, 1)[0]
            if dictionary_bytes > 64 * _CHUNK:
                raise ArchiveError('7z压缩字典超过安全内存上限', 'resource_limit')

    @contextmanager
    def _stream(self, kind=None):
        kind = kind or self.kind
        self.source.seek(0)
        stream = self.source
        if kind == 'gz':
            stream = gzip.GzipFile(fileobj=self.source, mode='rb')
        elif kind == 'bz2':
            stream = bz2.BZ2File(self.source, mode='rb')
        elif kind == 'xz':
            # LZMAFile does not expose memlimit. Use an incremental decoder.
            stream = _XZStream(self.source, self._check)
        try:
            yield stream
        finally:
            if stream is not self.source:
                stream.close()

    @contextmanager
    def _tar(self):
        name = self.filename.lower()
        compression = ('gz' if name.endswith(('.gz', '.tgz')) else
                       'bz2' if name.endswith(('.bz2', '.tbz', '.tbz2')) else
                       'xz' if name.endswith(('.xz', '.txz')) else 'tar')
        with self._stream(compression) as stream:
            # Account for headers, bounded extension metadata and padding too.
            budget = self.total_limit + _METADATA_LIMIT + self.limits.entries * 1024 + 10240
            bounded = _BoundedRead(stream, budget, self._check)
            with tarfile.open(fileobj=bounded, mode='r|', tarinfo=_BoundedTarInfo) as archive:
                yield archive

    def entries(self):
        if self._entries is None:
            result, seen, total = [], set(), 0
            for entry in self._iter_entries():
                self._check()
                if len(result) >= self.limits.entries:
                    raise ArchiveError('归档成员数量超过安全上限', 'resource_limit')
                if entry.name in seen:
                    raise ArchiveError('归档成员名称重复，未发布该归档的成员清单')
                seen.add(entry.name)
                if entry.size < 0:
                    raise ArchiveError('归档成员大小无效')
                total += entry.size
                if total > self.total_limit:
                    raise ArchiveError('归档解压总量或压缩比超过安全上限', 'resource_limit')
                result.append(entry)
            self._entries = tuple(result)
        return iter(self._entries)

    def _iter_entries(self):
        if self.kind == 'zip':
            for info in self._archive.filelist:
                mode = stat.S_IFMT((info.external_attr >> 16) & 0xffff)
                # ZipInfo.filename rewrites backslashes on Windows. Validate
                # the original archive name consistently on both platforms.
                yield ArchiveEntry(info.orig_filename, info.file_size,
                                   not info.is_dir() and mode in {0, stat.S_IFREG},
                                   bool(info.flag_bits & 1), info.compress_size, info.is_dir(), info)
        elif self.kind == 'tar':
            with self._tar() as archive:
                for info in archive:
                    name = info.name + ('/' if info.isdir() and not info.name.endswith('/') else '')
                    yield ArchiveEntry(name, info.size, info.isfile() and not info.issparse(),
                                       directory=info.isdir())
                    archive.members.clear()
        elif self.kind == 'rar':
            import rarfile
            for info in self._archive.infolist():
                mode = stat.S_IFMT(info.mode or 0) if info.host_os == rarfile.RAR_OS_UNIX else 0
                yield ArchiveEntry(info.filename, info.file_size,
                                   info.is_file() and not info.is_symlink() and not info.file_redir
                                   and mode in {0, stat.S_IFREG},
                                   info.needs_password(), info.compress_size, info.is_dir(), info)
        elif self.kind == '7z':
            for info in self._archive.files:
                yield ArchiveEntry(info.filename, int(info.uncompressed or 0), bool(info.is_file),
                                   self._archive.needs_password(), info.compressed,
                                   info.is_directory, info)
        else:
            with self._stream() as stream:
                bounded = _BoundedRead(stream, min(self.limits.member_bytes, self.total_limit), self._check)
                while bounded.read(_CHUNK):
                    pass
                yield ArchiveEntry(Path(self.filename).name[:-len(self.kind)-1], bounded.position,
                                   compressed_size=self.source_size)

    @contextmanager
    def open_member(self, entry):
        # Complete inventory first: a late duplicate invalidates earlier entries.
        entries = tuple(self.entries())
        if isinstance(entry, str):
            entry = next((item for item in entries if item.name == entry), None)
        if entry is None or not any(item is entry for item in entries):
            raise ArchiveError('归档成员不存在或不是本目录中的成员')
        if not _safe_name(entry.name) or not entry.regular:
            raise ArchiveError('归档成员路径、链接或类型不允许读取', 'unsafe_member')
        if entry.encrypted:
            raise ArchiveError('加密压缩包成员需要密码，不能读取', 'encrypted')
        if entry.size > min(self.limits.member_bytes, self.total_limit):
            raise ArchiveError('归档成员超过内容或解压安全上限', 'resource_limit')
        if (entry.compressed_size is not None and
                entry.size > max(1, entry.compressed_size) * self.limits.compression_ratio):
            raise ArchiveError('归档成员压缩比超过安全上限', 'resource_limit')
        if self.kind == '7z':
            with self._7z_member(entry) as stream:
                yield _BoundedRead(stream, entry.size, self._check, exact=True)
        elif self.kind == 'tar':
            with self._tar() as archive:
                for info in archive:
                    if info.name == entry.name:
                        with archive.extractfile(info) as stream:
                            yield _BoundedRead(stream, entry.size, self._check, exact=True)
                        return
                    archive.members.clear()
                raise ArchiveError('TAR成员已不存在')
        elif self.kind in {'zip', 'rar'}:
            try:
                if self.kind == 'rar':
                    import rarfile
                    if entry._info.compress_type != rarfile.RAR_M0 or rarfile.FORCE_TOOL:
                        # Check the backend before rarfile creates its temporary
                        # compressed-member/solid-container copy.
                        rarfile.tool_setup()
                        Config.PARSE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
                        import shutil
                        if (shutil.disk_usage(Config.PARSE_TEMP_DIR).free -
                                int(Config.PARSE_TEMP_DISK_RESERVE_BYTES or 0) < self.source_size):
                            raise ArchiveError('RAR临时磁盘空间不足', 'resource_limit')
                        rarfile.HACK_TMP_DIR = str(Config.PARSE_TEMP_DIR)
                with self._archive.open(entry._info, 'r') as stream:
                    yield _BoundedRead(stream, entry.size, self._check, exact=True)
            except Exception as exc:
                if self.kind == 'rar':
                    import rarfile
                    if isinstance(exc, rarfile.RarCannotExec):
                        raise ArchiveError('RAR压缩成员读取需要安装 unrar、unar 或7z解压后端',
                                           'dependency_missing') from exc
                    if isinstance(exc, rarfile.Error):
                        raise ArchiveError('RAR成员损坏或无法解码：' + str(exc)) from exc
                raise
        else:
            with self._stream() as stream:
                yield _BoundedRead(stream, entry.size, self._check, exact=True)

    @contextmanager
    def _7z_member(self, entry):
        from py7zr.io import Py7zIO, WriterFactory
        from py7zr.exceptions import ArchiveError as SevenZipError
        Config.PARSE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        import shutil
        free = shutil.disk_usage(Config.PARSE_TEMP_DIR).free
        if free - int(Config.PARSE_TEMP_DISK_RESERVE_BYTES or 0) < entry.size:
            raise ArchiveError('归档成员临时磁盘空间不足', 'resource_limit')
        with tempfile.TemporaryFile(dir=Config.PARSE_TEMP_DIR) as spool:
            owner = self
            class Sink(Py7zIO):
                def write(self, data):
                    owner._check()
                    if spool.tell() + len(data) > entry.size:
                        raise ArchiveError('7z成员实际大小超过声明或安全上限', 'resource_limit')
                    return spool.write(data)
                def read(self, size=None):
                    return spool.read(size if size is not None else entry.size)
                def seek(self, offset, whence=0):
                    position = spool.seek(offset, whence)
                    if position < 0 or position > entry.size:
                        raise ArchiveError('7z成员写入位置超过安全上限', 'resource_limit')
                    return position
                def flush(self):
                    spool.flush()
                def size(self):
                    return os.fstat(spool.fileno()).st_size
            class Factory(WriterFactory):
                created = False
                def create(self, filename):
                    if filename not in {entry.name, posixpath.normpath(entry.name)} or self.created:
                        raise ArchiveError('7z输出成员名称重复或不一致')
                    self.created = True
                    return Sink()
            self._archive.reset()
            try:
                self._archive.extract(targets=[entry.name], factory=Factory())
            except SevenZipError as exc:
                raise ArchiveError('7z成员损坏或无法解码：' + str(exc)) from exc
            if os.fstat(spool.fileno()).st_size != entry.size:
                raise ArchiveError('7z成员提取长度与目录声明不一致')
            spool.seek(0)
            yield spool


class _XZStream:
    """Incremental XZ decoding with an explicit 64 MiB decoder memory budget."""
    def __init__(self, source, check):
        self.source, self.check = source, check
        self.decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=64 * _CHUNK)
        self.done = False

    def read(self, size):
        self.check()
        if self.done:
            return b''
        output = bytearray()
        while len(output) < size:
            self.check()
            if self.decoder.eof:
                data = self.decoder.unused_data or self.source.read(_CHUNK)
                # XZ stream padding consists of a multiple of four zero bytes.
                if data and data[0] == 0:
                    padding = 0
                    while data and data[0] == 0:
                        zeros = len(data) - len(data.lstrip(b'\0'))
                        padding += zeros
                        data = data[zeros:]
                        if not data:
                            data = self.source.read(_CHUNK)
                    if padding % 4:
                        raise ArchiveError('XZ尾部填充损坏')
                if not data:
                    self.done = True
                    break
                self.decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=64 * _CHUNK)
            elif self.decoder.needs_input:
                data = self.source.read(_CHUNK)
                if not data:
                    raise ArchiveError('XZ数据已截断')
            else:
                data = b''
            try:
                output.extend(self.decoder.decompress(data, max_length=size - len(output)))
            except lzma.LZMAError as exc:
                raise ArchiveError('XZ解压失败或超过内存上限：' + str(exc)) from exc
        return bytes(output)

    def close(self):
        pass
