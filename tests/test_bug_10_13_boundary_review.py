"""Synthetic boundary regressions for CSV partition and source preservation."""
import csv
import io

import pytest

from services.logical_units import structured_text_units


@pytest.mark.parametrize('fts', [False, True])
def test_source_and_scope_filters_precede_limit(tmp_path, fts):
    from services.storage import Storage
    storage = Storage(tmp_path / 'filters.db')
    if fts and not storage.evidence_fts_available:
        pytest.skip('SQLite FTS unavailable')
    storage.replace_evidence_index('scan', [
        {'evidence_id': str(i), 'source_path': 'bundle.zip::other.txt',
         'archive_source_path': 'bundle.zip', 'text': 'payment'} for i in range(60)
    ] + [{'evidence_id': 'wanted', 'source_path': 'bundle.zip::inside.txt',
          'archive_source_path': 'bundle.zip', 'text': 'payment'}])
    storage.evidence_fts_available = fts
    result = storage.search_evidence_index('scan', 'payment',
        source_paths=['bundle.zip'], scope='bundle.zip::inside.txt', limit=50)
    assert [item['evidence_id'] for item in result] == ['wanted']


@pytest.mark.parametrize('fts', [False, True])
def test_source_directory_prefix_and_literal_scope(tmp_path, fts):
    from services.storage import Storage
    storage = Storage(tmp_path / 'prefix.db')
    storage.replace_evidence_index('scan', [
        {'evidence_id': str(i), 'source_path': 'dirX/a.txt', 'text': 'payment'} for i in range(60)
    ] + [{'evidence_id': 'wanted', 'source_path': 'dir_/a.txt', 'text': 'payment'}])
    storage.evidence_fts_available = fts and storage.evidence_fts_available
    result = storage.search_evidence_index('scan', 'payment',
        source_paths=['dir_'], scope='dir_', limit=50)
    assert [item['evidence_id'] for item in result] == ['wanted']


@pytest.mark.parametrize('word', ['payment', 'café', 'оплаты', '付款', 'สัญญา'])
def test_storage_retains_existing_and_unicode_lexical_search(tmp_path, word):
    from services.storage import Storage
    storage = Storage(tmp_path / 'words.db')
    storage.replace_evidence_index('scan', [
        {'evidence_id': 'wanted', 'source_path': 'file.txt', 'text': word}])
    assert storage._retrieval_terms(word)
    assert storage.search_evidence_index('scan', word)[0]['evidence_id'] == 'wanted'


def test_bom_multiline_quoted_header_and_escaped_quotes_round_trip(tmp_path, monkeypatch):
    from config import Config
    from services.package_analysis import _logical_source_snapshot
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(Config, "PARSE_TEMP_DIR", scratch)
    monkeypatch.setattr(Config, "SCAN_ALLOWED_ROOTS", ())
    path = tmp_path / "table.csv"
    path.write_bytes(b'\xef\xbb\xbf"multi\r\nline",note\r\n1,"a""quoted\r\nvalue"\r\n2,end\r\n3,tail')
    stat = path.stat()
    container = {"path": path.name, "name": path.name, "extension": ".csv",
                 "size": stat.st_size, "modified_at_ns": stat.st_mtime_ns,
                 "device": stat.st_dev, "inode": stat.st_ino}
    units = list(structured_text_units(tmp_path, container, partition_bytes=5))
    assert len(units) == 3
    assert units[0]["header_byte_end"] == len(b'\xef\xbb\xbf"multi\r\nline",note\r\n')
    actual = []
    for unit in units:
        with _logical_source_snapshot(tmp_path, unit) as snapshot:
            rows = list(csv.reader(io.StringIO(snapshot.read_bytes().decode("utf-8-sig"))))
        actual.extend(rows[1:] if unit["include_header"] else rows)
    assert actual == list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig"))))
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("extension", [".csv", ".tsv"])
@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-32", "utf-32-be"])
def test_wide_encoding_bom_preserves_whole_source_for_parser(tmp_path, encoding, extension):
    path = tmp_path / ("wide" + extension)
    bom = {"utf-16-be": b"\xfe\xff", "utf-32-be": b"\x00\x00\xfe\xff"}.get(encoding, b"")
    raw = bom + 'id,note\n1,"first\nsecond"\n2,end\n'.encode(encoding)
    path.write_bytes(raw)
    container = {"path": path.name, "name": path.name, "extension": extension, "size": len(raw)}
    assert list(structured_text_units(tmp_path, container, partition_bytes=5)) == []
    assert path.read_bytes() == raw
