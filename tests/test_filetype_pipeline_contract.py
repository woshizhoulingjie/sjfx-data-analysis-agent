"""New archive formats retain provenance through selection, preview and export."""
import bz2
import gzip
import hashlib
import io
import json
import lzma
import os
import tarfile
import zipfile

import pytest

from config import Config
from services.large_archive_members import member_snapshot
from services.large_package_isolated import LargePackageStore, iter_inventory
from services.large_package_preview import preview_file
from services.unified_parser import UnifiedDocumentParser
from test_archive_member_export import export_selection
from test_archive_formats import _stored_rar


@pytest.mark.skipif(os.name == 'nt', reason='Inventory identities require POSIX no-follow descriptors')
@pytest.mark.parametrize('suffix', ['.tbz', '.tbz2', '.txz', '.rar', '.7z', '.gz', '.bz2', '.xz'])
def test_new_formats_complete_selection_parse_and_export(tmp_path, monkeypatch, suffix):
    root = tmp_path / 'source'
    root.mkdir()
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (root,))
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', tmp_path / 'scratch')
    monkeypatch.setattr(Config, 'PARSE_TEMP_DISK_RESERVE_BYTES', 0)
    monkeypatch.setenv('SJFX_PARSE_TEMP_DIR', str(tmp_path / 'scratch'))
    payload = '订单记录 PO-4821\n交付数量=8\n'.encode('utf-8')
    stream = suffix in {'.gz', '.bz2', '.xz'}
    container = root / (('receipt.ini' if stream else 'records') + suffix)
    if stream:
        container.write_bytes({'.gz': gzip.compress, '.bz2': bz2.compress, '.xz': lzma.compress}[suffix](payload))
    elif suffix in {'.tbz', '.tbz2', '.txz'}:
        with tarfile.open(container, 'w:xz' if suffix == '.txz' else 'w:bz2') as archive:
            item = tarfile.TarInfo('receipt.ini')
            item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))
    elif suffix == '.rar':
        pytest.importorskip('rarfile')
        container.write_bytes(_stored_rar([('receipt.ini', payload)]))
    else:
        py7zr = pytest.importorskip('py7zr')
        with py7zr.SevenZipFile(container, 'w') as archive:
            archive.writestr(payload, 'receipt.ini')
    store = LargePackageStore(tmp_path / 'large.db')
    package = store.create(str(root), owner_id='format-test-owner')
    store.upsert_files(package['id'], list(iter_inventory(root)))
    logical = container.name + '::receipt.ini'
    selection = store.export_selection(package['id'], paths=[logical])
    row = store.get_file(package['id'], logical)
    assert row['source_kind'] == 'archive_member'
    with member_snapshot(root, row) as snapshot:
        assert snapshot.read_bytes() == payload
        deep = UnifiedDocumentParser(max_chars=20000).parse(snapshot, logical, mode='fast')
        assert deep['coverage']['complete']
        assert deep['source']['path'] == logical
        assert 'PO-4821' in deep['text']
    sample, _ = preview_file(root, row)
    assert sample['source_path'] == logical
    assert 'PO-4821' in sample['sample']
    exported = export_selection(root, selection, content_deduplication=False)
    with zipfile.ZipFile(exported) as output:
        assert output.read(container.name + '.members/receipt.ini') == payload
        manifest = json.loads(output.read('源文件SHA-256清单.json'))
        assert manifest['files'][0]['path'] == logical
        assert manifest['files'][0]['sha256'] == hashlib.sha256(payload).hexdigest()
