"""Real input fixtures for the ordinary / bounded-preview format contract."""
import bz2
import gzip
import io
import lzma
import tarfile
from unittest.mock import patch

import pytest

from services.large_package_preview import preview_file
from services.unified_parser import UnifiedDocumentParser


BODY = '采购订单 2026-10-12\n供应商: Acme\n采购金额: 12345\n采购订单编号: PO-4821\n'


def ordinary(path, tmp_path):
    with patch.dict('os.environ', {'SJFX_PARSE_TEMP_DIR': str(tmp_path / 'scratch')}):
        return UnifiedDocumentParser(max_chars=40000).parse(path, mode='fast')


def preview(path, refine=True):
    stat = path.stat()
    return preview_file(path.parent, {'path': path.name, 'size': stat.st_size,
                        'modified_at_ns': stat.st_mtime_ns}, refine=refine)[0]


@pytest.mark.parametrize('extension', ['.ini', '.cfg'])
def test_configuration_text_is_read_in_both_routes(tmp_path, extension):
    path = tmp_path / ('settings' + extension)
    path.write_text(BODY, encoding='utf-8')
    document, sample = ordinary(path, tmp_path), preview(path)
    assert 'PO-4821' in document['text']
    assert document['coverage']['complete']
    assert document['parse_status'] == 'complete'
    assert 'PO-4821' in sample['sample']
    assert not sample['complete']
    assert sample['coverage']['status'] == 'partial'


@pytest.mark.parametrize('suffix,mode', [('.tar', 'w'), ('.tar.gz', 'w:gz'),
    ('.tgz', 'w:gz'), ('.tar.bz2', 'w:bz2'), ('.tbz', 'w:bz2'),
    ('.tbz2', 'w:bz2'), ('.tar.xz', 'w:xz'), ('.txz', 'w:xz')])
def test_tar_aliases_extract_actual_member_in_both_routes(tmp_path, suffix, mode):
    path = tmp_path / ('documents' + suffix)
    payload = BODY.encode('utf-8')
    with tarfile.open(str(path), mode) as archive:
        info = tarfile.TarInfo('采购记录.ini')
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    document, sample = ordinary(path, tmp_path), preview(path)
    assert document['source']['extension'] == suffix
    assert document['archive_manifest']['parsed_members'] == 1
    assert document['parse_status'] == 'complete'
    assert 'PO-4821' in document['text']
    assert 'PO-4821' in sample['sample']
    assert sample['parse_status'] == 'partial'


@pytest.mark.parametrize('suffix,compress', [('.gz', gzip.compress),
    ('.bz2', bz2.compress), ('.xz', lzma.compress)])
def test_single_stream_keeps_json_suffix_and_structure(tmp_path, suffix, compress):
    path = tmp_path / ('records.json' + suffix)
    path.write_bytes(compress(b'{"order":"PO-4821","amount":12345}'))
    document, sample = ordinary(path, tmp_path), preview(path)
    assert document['structure']['archive_members'] == ['records.json']
    assert document['archive_manifest']['parsed_members'] == 1
    assert document['parse_status'] == 'complete'
    assert 'PO-4821' in document['text']
    assert 'PO-4821' in sample['sample']


@pytest.mark.parametrize('extension', ['.mbox', '.mbx', '.msg', '.pst'])
def test_mail_sources_do_not_become_ordinary_body_success(tmp_path, extension):
    path = tmp_path / ('mail' + extension)
    path.write_bytes(b'From sender@example.test Mon Oct 12 00:00:00 2026\nSubject: purchase\n\nprivate body')
    document, sample = ordinary(path, tmp_path), preview(path)
    for result in (document, sample):
        assert result['parse_status'] == 'unsupported'
        assert not result['coverage']['complete']
        assert result['coverage']['mail_source_required']
        assert result['coverage']['route_hint'] == 'mail_import'
    assert document['text'] == sample['sample'] == ''


def test_empty_text_and_header_only_mail_never_claim_body_complete(tmp_path):
    for name, content in [('empty.txt', b''), ('headers.eml', b'Subject: purchase\nFrom: a@example.test\n\n')]:
        path = tmp_path / name
        path.write_bytes(content)
        document = ordinary(path, tmp_path)
        assert not document['coverage']['complete']
        assert document['parse_status'] == 'partial'


def test_blank_pdf_page_labels_are_not_body_evidence(tmp_path):
    from pypdf import PdfWriter
    path = tmp_path / 'blank.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(str(path))
    with patch.object(UnifiedDocumentParser, '_rapidocr_pdf', return_value=1):
        document = ordinary(path, tmp_path)
    assert document['text'] == ''
    assert document['evidence'] == []
    assert not document['coverage']['complete']


@pytest.mark.parametrize('error,status', [(ImportError('OCR unavailable'), 'dependency_missing'),
                                         (TimeoutError('OCR timeout'), 'timeout')])
def test_ocr_failure_retains_terminal_reason_for_empty_pdf(tmp_path, error, status):
    from pypdf import PdfWriter
    path = tmp_path / 'blank.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(str(path))
    with patch.object(UnifiedDocumentParser, '_rapidocr_pdf', side_effect=error):
        document = ordinary(path, tmp_path)
    assert document['parse_status'] == status
    assert not document['coverage']['complete']


def test_plain_text_that_looks_like_a_page_label_is_retained(tmp_path):
    path = tmp_path / 'label.txt'
    path.write_text('[第 1 页]', encoding='utf-8')
    document = ordinary(path, tmp_path)
    assert document['text'] == '[第 1 页]'
    assert document['coverage']['complete']


@pytest.mark.parametrize('suffix', ['.rar', '.7z'])
def test_missing_archive_component_is_reported_from_actual_route(tmp_path, suffix):
    path = tmp_path / ('input' + suffix)
    path.write_bytes(b'placeholder archive: loader cannot inspect without its component')
    module = 'rarfile' if suffix == '.rar' else 'py7zr'
    with patch.dict('sys.modules', {module: None}):
        document, sample = ordinary(path, tmp_path), preview(path)
    assert document['parse_status'] == sample['parse_status'] == 'dependency_missing'
    assert not document['coverage']['complete']
    assert not sample['coverage']['complete']


def test_truncated_xz_is_damaged_in_both_routes(tmp_path):
    path = tmp_path / 'records.json.xz'
    path.write_bytes(lzma.compress(b'{"order":"PO-4821"}')[:-8])
    document, sample = ordinary(path, tmp_path), preview(path)
    assert document['parse_status'] == sample['parse_status'] == 'damaged'
    assert not document['coverage']['complete']


def test_archive_metadata_without_readable_members_is_partial(tmp_path):
    path = tmp_path / 'empty-body.tar'
    with tarfile.open(str(path), 'w') as archive:
        info = tarfile.TarInfo('empty.txt')
        archive.addfile(info, io.BytesIO(b''))
    document = ordinary(path, tmp_path)
    assert document['text'] == ''
    assert document['archive_manifest']['parsed_members'] == 0
    assert document['parse_status'] == 'partial'
    assert not document['coverage']['complete']


def test_tar_directory_records_do_not_reduce_body_coverage(tmp_path):
    path = tmp_path / 'folder.tar'
    payload = BODY.encode('utf-8')
    with tarfile.open(str(path), 'w') as archive:
        directory = tarfile.TarInfo('folder')
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        info = tarfile.TarInfo('folder/orders.ini')
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    document = ordinary(path, tmp_path)
    assert document['archive_manifest']['total_members'] == 1
    assert document['parse_status'] == 'complete'


def test_actual_7z_content_and_password_status_in_both_routes(tmp_path):
    py7zr = pytest.importorskip('py7zr')
    path = tmp_path / 'documents.7z'
    with py7zr.SevenZipFile(path, 'w') as archive:
        archive.writestr(BODY.encode('utf-8'), 'orders.cfg')
    document, sample = ordinary(path, tmp_path), preview(path)
    assert 'PO-4821' in document['text'] and 'PO-4821' in sample['sample']
    assert document['parse_status'] == 'complete'
    encrypted = tmp_path / 'encrypted.7z'
    with py7zr.SevenZipFile(encrypted, 'w', password='test', header_encryption=True) as archive:
        archive.writestr(b'private body', 'private.txt')
    for result in (ordinary(encrypted, tmp_path), preview(encrypted)):
        assert result['parse_status'] == 'encrypted'
        assert not result['coverage']['complete']


def test_actual_stored_rar_content_in_both_routes(tmp_path):
    pytest.importorskip('rarfile')
    from test_archive_formats import _stored_rar
    path = tmp_path / 'orders.rar'
    path.write_bytes(_stored_rar([('orders.ini', BODY.encode('utf-8'))]))
    document, sample = ordinary(path, tmp_path), preview(path)
    assert 'PO-4821' in document['text'] and 'PO-4821' in sample['sample']
    assert document['parse_status'] == 'complete'


def test_jsonl_invalid_record_retains_partial_content_without_complete_claim(tmp_path):
    path = tmp_path / 'records.jsonl'
    path.write_text('{"id":"valid-record","description":"known body"}\n{"broken":\n', encoding='utf-8')
    document = ordinary(path, tmp_path)
    assert 'valid-record' in document['text']
    assert document['parse_status'] == 'partial'
    assert document['coverage']['failure_status'] == 'damaged'
    assert not document['coverage']['complete']
