import hashlib
import json
from email.message import EmailMessage

import pytest

from config import Config
from services import unified_parser
from services.unified_parser import UnifiedDocumentParser, compact_document


def message(body='Payment review evidence', charset='utf-8'):
    mail = EmailMessage()
    mail['Subject'] = '研究 / Research / การวิจัย'
    mail['From'] = 'Analyst <a@example.test>'
    mail['To'] = 'review@example.test'
    mail['Date'] = 'Sat, 26 Sep 2026 10:00:00 +0800'
    mail['Message-ID'] = '<source@example.test>'
    mail.set_content(body, charset=charset)
    return mail


def parse(tmp_path, mail, mode='fast', max_chars=2_000_000):
    path = tmp_path / 'mail.eml'
    path.write_bytes(mail if isinstance(mail, bytes) else mail.as_bytes())
    return UnifiedDocumentParser(max_chars=max_chars, fast_office_ocr=False).parse(
        path, relative_path='selected/mail.eml', mode=mode)


def verify_evidence(doc):
    assert doc['evidence']
    for item in doc['evidence']:
        start, end = item['character_range']
        assert doc['text'][start:end] == item['text']
        assert item['source_sha256'] == doc['source']['sha256']
        assert item['source_path'] == 'selected/mail.eml'
        assert item['content_sha256'] == hashlib.sha256(item['text'].encode()).hexdigest()
    assert len({e['evidence_id'] for e in doc['evidence']}) == len(doc['evidence'])


@pytest.mark.parametrize('mode', ['fast', 'accurate', 'auto'])
@pytest.mark.parametrize('body,charset', [('Payment review evidence', 'utf-8'),
    ('采购审核与支付记录', 'utf-8'), ('采购审核与支付记录', 'gb18030'),
    ('การตรวจสอบการจัดซื้อ', 'utf-8'), ('การตรวจสอบการจัดซื้อ', 'tis-620')])
def test_three_languages_without_docling(tmp_path, monkeypatch, mode, body, charset):
    def forbidden(*args, **kwargs):
        pytest.fail('EML must not invoke Docling')
    monkeypatch.setattr(UnifiedDocumentParser, 'docling_available', property(forbidden))
    mail = message(body, charset)
    doc = parse(tmp_path, mail, mode)
    assert body in doc['text'] and '研究' in doc['text'] and 'การวิจัย' in doc['text']
    assert doc['parser']['name'] == 'rfc5322-mime'
    assert doc['parser']['requested_mode'] == mode and not doc['parser']['ocr']
    assert doc['coverage']['complete'] and doc['coverage']['embedded_objects_covered']
    assert doc['source']['sha256'] == hashlib.sha256(mail.as_bytes()).hexdigest()
    assert compact_document(doc)['structure']['mail']['body_parts'] == 1
    verify_evidence(doc)


def test_all_outer_bodies_preserved_and_attachments_not_promoted(tmp_path):
    mail = message('Plain outer body')
    mail.add_alternative('<p>HTML outer evidence</p><script>DO_NOT_KEEP_SCRIPT</script>', subtype='html')
    mail.make_mixed()
    extra = EmailMessage(); extra.set_content('<p>Independent mixed evidence</p>', subtype='html')
    mail.attach(extra)
    mail.add_attachment(b'PRIVATE_ATTACHMENT_ONLY', maintype='text', subtype='plain', filename='attachment.txt')
    mail.add_attachment(message('NESTED_BODY_ONLY'))
    doc = parse(tmp_path, mail)
    for value in ('Plain outer body', 'HTML outer evidence', 'Independent mixed evidence'):
        assert value in doc['text']
    for value in ('PRIVATE_ATTACHMENT_ONLY', 'NESTED_BODY_ONLY', 'DO_NOT_KEEP_SCRIPT'):
        assert value not in doc['text']
    assert doc['coverage']['complete'] and not doc['coverage']['embedded_objects_covered']
    assert doc['coverage']['coverage_ratio'] is None
    parts = doc['structure']['mail']['parts']
    assert len([p for p in parts if p['role'].startswith('body_')]) == 3
    assert {p['role'] for p in parts} >= {'nested_message', 'attachment'}
    assert 'raw_payload' not in json.dumps(compact_document(doc))
    assert len({e['mime_path'] for e in doc['evidence'] if e['label'] == 'mail_body'}) == 3
    verify_evidence(doc)


def test_html_only_message(tmp_path):
    mail = message(); mail.set_content('<p>泰文 การวิจัย</p>', subtype='html')
    doc = parse(tmp_path, mail)
    assert 'การวิจัย' in doc['text'] and '<p>' not in doc['text']
    assert doc['coverage']['complete']
    verify_evidence(doc)


@pytest.mark.parametrize('max_chars', [0, 1, 100, 1500])
def test_character_limit_reports_incomplete(tmp_path, max_chars):
    doc = parse(tmp_path, message('长正文' * 1200), max_chars=max_chars)
    assert len(doc['text']) <= max_chars
    assert not doc['coverage']['complete'] and doc['coverage']['truncated_by_limit']
    assert not doc['coverage']['evidence_complete']
    assert doc['coverage']['extracted_characters'] > doc['coverage']['stored_characters']
    if doc['text']:
        verify_evidence(doc)


def test_no_body_is_not_success(tmp_path):
    doc = parse(tmp_path, message(''))
    assert not doc['coverage']['complete']
    assert not any(e['label'] == 'mail_body' for e in doc['evidence'])


@pytest.mark.parametrize('charset,body', [('unknown-test-charset', b'body'), ('utf-8', bytes([255, 254]))])
def test_encoding_loss_is_not_complete(tmp_path, charset, body):
    raw = (('Subject: review' + chr(10) + 'Content-Type: text/plain; charset=' + charset
            + chr(10) * 2).encode() + body)
    doc = parse(tmp_path, raw)
    assert not doc['coverage']['complete'] and doc['warnings']


def test_part_limit_is_not_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'MAIL_MAX_MIME_PARTS_PER_MESSAGE', 1)
    mail = message('first'); mail.add_alternative('<p>second</p>', subtype='html')
    doc = parse(tmp_path, mail)
    assert not doc['coverage']['complete'] and not doc['coverage']['embedded_objects_covered']
    assert doc['warnings']


def test_evidence_cap_retains_text_and_reports_gap(tmp_path, monkeypatch):
    from services import mail_document
    monkeypatch.setattr(mail_document, '_EVIDENCE_LIMIT', 2)
    doc = parse(tmp_path, message('evidence ' * 600))
    assert len(doc['evidence']) == 2 and len(doc['text']) > 3000
    assert not doc['coverage']['evidence_complete'] and not doc['coverage']['complete']
    assert not doc['coverage']['truncated_by_limit']
    verify_evidence(doc)


def test_message_size_rejected_before_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'MAIL_MAX_MESSAGE_BYTES', 50)
    def forbidden(*args):
        pytest.fail('Oversize mail must be rejected before hashing')
    monkeypatch.setattr(unified_parser, 'sha256_file', forbidden)
    doc = parse(tmp_path, message('body'))
    assert not doc['text'] and doc['coverage']['limited_by_size']
    assert not doc['source']['sha256']


def test_source_changed_after_hash_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(unified_parser, 'sha256_file', lambda path: '0' * 64)
    with pytest.raises(ValueError, match='哈希'):
        parse(tmp_path, message())


def test_sensitive_mail_remains_metadata_only(tmp_path, monkeypatch):
    monkeypatch.setattr(unified_parser, 'is_sensitive_file', lambda name: True)
    doc = parse(tmp_path, message('secret body'))
    assert not doc['text'] and not doc['evidence']
    assert doc['coverage']['content_restricted']
    assert doc['source']['content_policy'] == 'metadata_only_sensitive'
