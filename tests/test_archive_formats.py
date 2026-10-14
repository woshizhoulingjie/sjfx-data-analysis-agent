import bz2
from dataclasses import replace
import gzip
import io
import lzma
import os
import stat
import struct
import tarfile
import zipfile
import zlib

import pytest

from config import Config
from services.archive_formats import ArchiveError, ArchiveLimits, ArchiveReader


@pytest.fixture
def limits(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', tmp_path / 'scratch')
    monkeypatch.setattr(Config, 'PARSE_TEMP_DISK_RESERVE_BYTES', 0)
    return ArchiveLimits(entries=10, member_bytes=4096, total_bytes=8192,
                         compression_ratio=200, file_bytes=65536)


@pytest.mark.parametrize('suffix,compress', [('.gz', gzip.compress), ('.bz2', bz2.compress),
                                            ('.xz', lzma.compress)])
def test_single_stream_preserves_original_suffix_and_exact_size(limits, suffix, compress):
    data = b'key,value\nreceipt,8\n'
    with ArchiveReader(io.BytesIO(compress(data)), 'orders.csv' + suffix, limits) as reader:
        entry, = reader.entries()
        assert entry.name == 'orders.csv' and entry.size == len(data)
        with reader.open_member(entry) as stream:
            assert stream.read() == data


@pytest.mark.parametrize('suffix,mode', [('.tar', 'w'), ('.tar.gz', 'w:gz'), ('.tgz', 'w:gz'),
    ('.tar.bz2', 'w:bz2'), ('.tbz', 'w:bz2'), ('.tbz2', 'w:bz2'),
    ('.tar.xz', 'w:xz'), ('.txz', 'w:xz')])
def test_tar_alias_inventory_and_read(limits, suffix, mode):
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode=mode) as archive:
        info = tarfile.TarInfo('folder/receipt.txt'); info.size = 7
        archive.addfile(info, io.BytesIO(b'receipt'))
    with ArchiveReader(source, 'bundle' + suffix, limits) as reader:
        entry, = reader.entries()
        with reader.open_member(entry) as stream:
            assert stream.read() == b'receipt'


def test_inventory_checks_late_duplicate_before_any_read(limits):
    source = io.BytesIO()
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('safe.txt', b'first')
        archive.writestr('safe.txt', b'last')
    with ArchiveReader(source, 'bundle.zip', limits) as reader:
        with pytest.raises(ArchiveError, match='重复'):
            reader.open_member('safe.txt').__enter__()


def test_unsafe_and_link_members_cannot_be_opened(limits):
    source = io.BytesIO()
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('../outside.txt', b'bad')
        link = zipfile.ZipInfo('link.txt'); link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b'outside')
        archive.writestr('safe.txt', b'ok')
    with ArchiveReader(source, 'bundle.zip', limits) as reader:
        for name in ('../outside.txt', 'link.txt'):
            with pytest.raises(ArchiveError) as error:
                with reader.open_member(name):
                    pass
            assert error.value.code == 'unsafe_member'
        with reader.open_member('safe.txt') as stream:
            assert stream.read() == b'ok'


@pytest.mark.parametrize('suffix,compress', [('.gz', gzip.compress), ('.bz2', bz2.compress),
                                            ('.xz', lzma.compress)])
def test_stream_bomb_and_truncation_fail_before_publication(limits, suffix, compress):
    data = compress(b'x' * 4097)
    with ArchiveReader(io.BytesIO(data), 'text.txt' + suffix, limits) as reader:
        with pytest.raises(ArchiveError, match='安全上限'):
            list(reader.entries())
    with ArchiveReader(io.BytesIO(compress(b'valid')[:-5]), 'text.txt' + suffix, limits) as reader:
        with pytest.raises((ArchiveError, EOFError, OSError)):
            list(reader.entries())


def test_count_and_total_limits(limits):
    source = io.BytesIO()
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('a.txt', b'1234'); archive.writestr('b.txt', b'1234')
    with pytest.raises(ArchiveError, match='数量'):
        with ArchiveReader(source, 'bundle.zip', replace(limits, entries=1)):
            pass
    with ArchiveReader(source, 'bundle.zip', replace(limits, total_bytes=7)) as reader:
        with pytest.raises(ArchiveError, match='总量'):
            list(reader.entries())


def test_real_7z_factory_reads_without_filesystem_extraction(limits, tmp_path, monkeypatch):
    py7zr = pytest.importorskip('py7zr')
    path = tmp_path / 'bundle.7z'
    with py7zr.SevenZipFile(path, 'w') as archive:
        archive.writestr(b'earlier solid member', 'first.txt')
        archive.writestr(b'key,value\nreceipt,8\n', 'folder/orders.csv')
    def forbidden(*args, **kwargs):
        raise AssertionError('filesystem extractall must not be used')
    monkeypatch.setattr(py7zr.SevenZipFile, 'extractall', forbidden)
    with ArchiveReader(path, limits=limits) as reader:
        entries = list(reader.entries())
        assert len(entries) == 2
        for _ in range(2):
            with reader.open_member('folder/orders.csv') as stream:
                assert stream.read() == b'key,value\nreceipt,8\n'
    assert not (tmp_path / 'folder').exists()
    assert not list(Config.PARSE_TEMP_DIR.iterdir())


def test_7z_header_count_rejected_before_library_allocates(limits, tmp_path):
    py7zr = pytest.importorskip('py7zr')
    source = io.BytesIO()
    with py7zr.SevenZipFile(source, 'w') as archive:
        archive.writestr(b'a', 'a.txt'); archive.writestr(b'b', 'b.txt')
    with pytest.raises(ArchiveError, match='数量'):
        with ArchiveReader(source, 'bundle.7z', replace(limits, entries=1)):
            pass


def test_7z_encrypted_archive_reports_password(limits, tmp_path):
    py7zr = pytest.importorskip('py7zr')
    path = tmp_path / 'encrypted.7z'
    with py7zr.SevenZipFile(path, 'w', password='test', header_encryption=True) as archive:
        archive.writestr(b'secret', 'private.txt')
    with pytest.raises(ArchiveError) as error:
        with ArchiveReader(path, limits=limits):
            pass
    assert error.value.code == 'encrypted'


def test_7z_rejects_large_declared_header_before_allocating(limits):
    header = bytearray(32); header[:6] = b'7z\xbc\xaf\x27\x1c'
    struct.pack_into('<QQ', header, 12, 0, 1024 * 1024 * 1024)
    pytest.importorskip('py7zr')
    with pytest.raises(ArchiveError, match='头部大小'):
        with ArchiveReader(io.BytesIO(header), 'bundle.7z', limits):
            pass


def _stored_rar(members, flags=0, method=0x30):
    """Construct ordinary stored RAR4 files without a proprietary writer."""
    def block(kind, block_flags, body):
        header = struct.pack('<BHH', kind, block_flags, 7 + len(body)) + body
        return struct.pack('<H', zlib.crc32(header) & 0xffff) + header
    result = bytearray(b'Rar!\x1a\x07\x00')
    result.extend(block(0x73, flags, b'\0' * 6))
    for name, data in members:
        encoded = name.encode()
        body = struct.pack('<LLBLLBBHL', len(data), len(data), 3, zlib.crc32(data),
                           0x5a210000, 20, method, len(encoded), 0o100644) + encoded
        result.extend(block(0x74, 0x8000, body)); result.extend(data)
    result.extend(block(0x7b, 0, b''))
    return result


def test_real_stored_rar_list_read_and_duplicate_rejection(limits):
    pytest.importorskip('rarfile')
    with ArchiveReader(io.BytesIO(_stored_rar([('receipt.txt', b'receipt')])),
                       'bundle.rar', limits) as reader:
        entry, = reader.entries()
        assert entry.name == 'receipt.txt'
        with reader.open_member(entry) as stream:
            assert stream.read() == b'receipt'
    with ArchiveReader(io.BytesIO(_stored_rar([('same.txt', b'a'), ('same.txt', b'b')])),
                       'bundle.rar', limits) as reader:
        with pytest.raises(ArchiveError, match='重复'):
            list(reader.entries())


def test_rar_multivolume_rejected_without_opening_neighbor_paths(limits):
    pytest.importorskip('rarfile')
    with pytest.raises(ArchiveError, match='分卷'):
        with ArchiveReader(io.BytesIO(_stored_rar([('a.txt', b'a')], flags=1)),
                           'bundle.rar', limits):
            pass


def test_missing_rar_backend_is_reported_before_temporary_copy(limits, monkeypatch):
    rarfile = pytest.importorskip('rarfile')
    def missing():
        raise rarfile.RarCannotExec('No extraction tool')
    monkeypatch.setattr(rarfile, 'tool_setup', missing)
    def forbidden(*args, **kwargs):
        raise AssertionError('Backend must be checked before allocating scratch files')
    monkeypatch.setattr(rarfile, 'mkstemp', forbidden)
    with ArchiveReader(io.BytesIO(_stored_rar([('receipt.txt', b'receipt')], method=0x33)),
                       'compressed.rar', limits) as reader:
        with pytest.raises(ArchiveError) as error:
            with reader.open_member('receipt.txt'):
                pass
        assert error.value.code == 'dependency_missing'


def test_7z_codec_with_unbounded_library_decoder_is_explicitly_unsupported(limits):
    py7zr = pytest.importorskip('py7zr')
    source = io.BytesIO()
    with py7zr.SevenZipFile(source, 'w', filters=[{'id': py7zr.FILTER_DEFLATE}]) as archive:
        archive.writestr(b'content', 'text.txt')
    with pytest.raises(ArchiveError) as error:
        with ArchiveReader(source, 'bundle.7z', limits):
            pass
    assert error.value.code == 'unsupported'


def test_cancelled_directory_read_propagates_cancellation(limits):
    from services.parse_isolation import ParseIsolationCancelled
    with pytest.raises(ParseIsolationCancelled):
        with ArchiveReader(io.BytesIO(gzip.compress(b'text')), 'text.txt.gz', limits,
                           cancel_check=lambda: True):
            pass


@pytest.mark.parametrize('suffix,compress', [('.gz', gzip.compress), ('.bz2', bz2.compress),
                                            ('.xz', lzma.compress)])
@pytest.mark.skipif(os.name == 'nt', reason='Trusted inventory source requires POSIX no-follow descriptors')
def test_single_stream_large_inventory_snapshot_keeps_identity(tmp_path, monkeypatch, suffix, compress):
    from services.large_archive_members import discover_members, member_snapshot
    root = tmp_path / 'root'; root.mkdir()
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (root,))
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', tmp_path / 'scratch')
    path = root / ('orders.csv' + suffix)
    data = b'key,value\nreceipt,8\n'; path.write_bytes(compress(data))
    st = path.stat()
    container = {'path': path.name, 'size': st.st_size, 'modified_at_ns': st.st_mtime_ns,
                 'device': st.st_dev, 'inode': st.st_ino}
    row, = discover_members(root, container)
    assert row['path'] == path.name + '::orders.csv'
    assert row['size'] == len(data) and row['extension'] == '.csv'
    with member_snapshot(root, row) as snapshot:
        assert snapshot.name == 'orders.csv' and snapshot.read_bytes() == data
    with member_snapshot(root, row, prefix_bytes=4) as snapshot:
        assert snapshot.read_bytes() == data[:4]
