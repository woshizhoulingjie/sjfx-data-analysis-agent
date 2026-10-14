"""Text-only PDF fallback, page provenance and bounded extraction contracts."""
from types import SimpleNamespace
import json
import subprocess
import sys

from pypdf.errors import PdfReadError
import pytest

from services import large_qa_extract as extraction
from services.large_qa_index import LargeQAIndex


class FontErrorPage:
    def extract_text(self):
        raise PdfReadError('More than one /FontFile found')


class TextPage:
    def __init__(self, text):
        self.text = text
        self.closed = False

    def get_text_range(self):
        return self.text

    def close(self):
        self.closed = True


class PDFiumPage:
    def __init__(self, text):
        self.text_page = TextPage(text)
        self.closed = False

    def get_textpage(self):
        return self.text_page

    def close(self):
        self.closed = True


class PDFiumDocument:
    def __init__(self, texts):
        self.pages = [PDFiumPage(text) for text in texts]
        self.visited = []
        self.closed = False

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, position):
        self.visited.append(position)
        return self.pages[position]

    def close(self):
        self.closed = True


@pytest.fixture
def font_failure(monkeypatch):
    monkeypatch.setattr('pypdf.PdfReader', lambda *args, **kwargs:
                        SimpleNamespace(pages=[FontErrorPage(), FontErrorPage()]))
    def no_ocr(*args, **kwargs):
        raise AssertionError('font fallback must not initialize OCR/parser/model')
    monkeypatch.setattr('services.unified_parser.UnifiedDocumentParser', no_ocr)


def pdfium(monkeypatch, texts):
    document = PDFiumDocument(texts)
    monkeypatch.setitem(sys.modules, 'pypdfium2', SimpleNamespace(PdfDocument=lambda path: document))
    return document


def test_font_error_uses_existing_pdfium_and_preserves_every_page(monkeypatch, font_failure):
    document = pdfium(monkeypatch, ['First page background.', 'However the last page has an exception.'])
    records, complete, note = extraction._extract('fonts.pdf', 'fonts.pdf', 1000)
    assert complete
    assert [item['location']['page'] for item in records] == [1, 2]
    assert records[1]['text'] == 'However the last page has an exception.'
    assert 'PDFium' in note
    assert document.closed
    assert all(page.closed and page.text_page.closed for page in document.pages)


def test_pdfium_budget_clips_text_and_releases_native_objects(monkeypatch, font_failure):
    document = pdfium(monkeypatch, ['a' * 200, 'Late second page.'])
    records, complete, note = extraction._extract('fonts.pdf', 'fonts.pdf', 45)
    assert not complete
    assert records == [{'text': 'a' * 45, 'location': {'page': 1}}]
    assert document.visited == [0]
    assert document.closed and document.pages[0].closed and document.pages[0].text_page.closed
    assert '预算' in note


def test_empty_pdfium_page_marks_partial_without_starting_ocr(monkeypatch, font_failure):
    document = pdfium(monkeypatch, ['Readable first page.', ''])
    records, complete, note = extraction._extract('fonts.pdf', 'fonts.pdf', 1000)
    assert not complete and len(records) == 1
    assert records[0]['location']['page'] == 1
    assert 'OCR' in note
    assert document.closed


def test_poppler_is_text_only_and_keeps_pdf_page_numbers(monkeypatch, font_failure):
    def unavailable(*args, **kwargs):
        raise ImportError('PDFium is unavailable')
    monkeypatch.setattr(extraction, '_pdfium_records', unavailable)
    monkeypatch.setattr(extraction.shutil, 'which', lambda name: '/usr/bin/pdftotext' if name == 'pdftotext' else None)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        number = int(args[args.index('-f') + 1])
        return subprocess.CompletedProcess(args, 0, stdout=('Text on page %s.' % number).encode(), stderr=b'')
    monkeypatch.setattr(extraction.subprocess, 'run', run)
    records, complete, note = extraction._extract('fonts.pdf', 'fonts.pdf', 1000)
    assert complete and 'Poppler' in note
    assert [item['location']['page'] for item in records] == [1, 2]
    assert records[-1]['text'] == 'Text on page 2.'
    assert len(calls) == 2
    assert all(options['timeout'] == 15 and not options.get('shell') for _, options in calls)


def test_pdfium_resource_pressure_does_not_retry_another_reader(monkeypatch, font_failure):
    def exhausted(*args, **kwargs):
        raise MemoryError('memory pressure')
    monkeypatch.setattr(extraction, '_pdfium_records', exhausted)
    monkeypatch.setattr(extraction, '_poppler_records', lambda *args, **kwargs:
                        pytest.fail('resource pressure must propagate to the worker'))
    with pytest.raises(MemoryError):
        extraction._extract('fonts.pdf', 'fonts.pdf', 1000)


def test_failed_text_readers_leave_explicit_failure_and_never_start_ocr(monkeypatch, font_failure):
    def unavailable(*args, **kwargs):
        raise ImportError('reader unavailable')
    monkeypatch.setattr(extraction, '_pdfium_records', unavailable)
    monkeypatch.setattr(extraction, '_poppler_records', unavailable)
    with pytest.raises(ValueError, match='未启动 OCR 或模型'):
        extraction._extract('fonts.pdf', 'fonts.pdf', 1000)


def test_published_fallback_keeps_existing_summary_chunks_and_summary_generation(
        tmp_path, monkeypatch, font_failure):
    source = tmp_path / 'fonts.pdf'
    source.write_bytes(b'%PDF-placeholder')
    stat = source.stat()
    index = LargeQAIndex('lp-fonts', tmp_path / 'index', create=True)
    index.sync_rows([{'path': source.name, 'name': source.name, 'size': stat.st_size,
                     'modified_at_ns': stat.st_mtime_ns,
                     'local_summary': json.dumps({'summary': 'An existing persisted file summary.'}),
                     'deep_summary': '{}'}], {'id': 'lp-fonts', 'root_path': str(tmp_path)})
    with index.connect() as db:
        document = dict(db.execute('SELECT * FROM documents').fetchone())
        before = index.meta(db)
        summary = [dict(row) for row in db.execute("SELECT * FROM chunks WHERE kind='summary'")]
    pdfium(monkeypatch, ['Readable original.', 'Late original exception.'])
    records, complete, note = extraction._extract(source, source.name, 1000)
    assert index.put_original(source.name, document['source_version'], records, complete, note)
    with index.connect() as db:
        assert summary == [dict(row) for row in db.execute("SELECT * FROM chunks WHERE kind='summary'")]
        after = index.meta(db)
        assert after['summary_generation'] == before['summary_generation']
        assert after['generation'] > before['generation']
        originals = list(db.execute("SELECT * FROM chunks WHERE kind='original' ORDER BY ordinal"))
        assert json.loads(originals[-1]['location'])['page'] == 2
        assert 'Late original exception.' in originals[-1]['text']
