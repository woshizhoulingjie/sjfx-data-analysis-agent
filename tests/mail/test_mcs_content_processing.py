"""MCS corpus regressions; supply SJFX_MAIL_MCS_FIXTURE for the real package."""
import base64
import hashlib
import io
import os
import quopri
import unittest
import zipfile
from collections import Counter
from email import policy
from email.parser import BytesParser
from pathlib import Path
import xml.etree.ElementTree as ET

from services.mail.extraction import _extract_attachment, _result
from services.mail.foundation import fragments
from services.mail.normalization import normalized_subject
from services.mail.parsers.eml import parse_eml


class MCSDiscoveredRegressionTests(unittest.TestCase):
    def test_chinese_reply_and_forward_prefixes_normalize_like_english(self):
        base = '【华芯智造】AGV数量变更及价格调整（变更阶段）'
        for value in [base, '回复: ' + base, '转发: ' + base, '回覆： ' + base, 'Re: 回复： Fwd: ' + base]:
            self.assertEqual(normalized_subject(value), normalized_subject(base))
        self.assertEqual(normalized_subject('回复客户的问题'), '回复客户的问题')

    def test_scanned_page_units_remain_without_counting_separators_as_text(self):
        result = _result('\n\n', 'pypdf', {'pages': 2, 'missing_pages': [1, 2],
            'units': [{'text': '', 'page_number': 1}, {'text': '', 'page_number': 2}]})
        self.assertEqual(result['text'], '')
        self.assertEqual(result['status'], 'ocr_pending')
        self.assertEqual(result['coverage_ratio'], 0)
        self.assertEqual([u['page_number'] for u in result['units']], [1, 2])


class MCSFullCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get('SJFX_MAIL_MCS_FIXTURE')
        if not path:
            path = Path(__file__).resolve().parents[3] / 'mcs-fixture' / 'full.zip'
        path = Path(path)
        if not path.is_file():
            raise unittest.SkipTest('Set SJFX_MAIL_MCS_FIXTURE to the original MCS full ZIP')
        with zipfile.ZipFile(path) as archive:
            cls.messages = [(name, archive.read(name)) for name in sorted(archive.namelist()) if name.casefold().endswith('.eml')]
        if len(cls.messages) != 56:
            raise AssertionError('Expected the full 56-message MCS fixture')

    def test_all_decoded_bodies_and_attachment_original_bytes_match_wire(self):
        attachment_count = 0
        for name, raw in self.messages:
            with self.subTest(message=name):
                independent = BytesParser(policy=policy.default).parsebytes(raw)
                parsed = parse_eml(raw)
                self.assertEqual(parsed.raw_sha256, hashlib.sha256(raw).hexdigest())
                wire_parts = [p for p in independent.walk() if not p.is_multipart()]
                self.assertEqual(len(wire_parts), len(parsed.parts))
                self.assertTrue(parsed.content_complete)
                for wire, part in zip(wire_parts, parsed.parts):
                    encoded = wire.get_payload(decode=False).encode('ascii', 'surrogateescape')
                    encoding = str(wire.get('Content-Transfer-Encoding') or '').casefold()
                    expected = base64.b64decode(encoded) if encoding == 'base64' else quopri.decodestring(encoded) if encoding == 'quoted-printable' else encoded
                    self.assertEqual(part.raw_payload, expected)
                    self.assertEqual(part.payload_sha256, hashlib.sha256(expected).hexdigest())
                    if part.part_role == 'body_plain':
                        self.assertEqual(part.text, expected.decode(wire.get_content_charset() or 'utf-8'))
                    if part.part_role == 'body_html':
                        self.assertEqual(part.html_raw, expected.decode(wire.get_content_charset() or 'utf-8'))
                    if part.is_explicit_attachment:
                        attachment_count += 1
                        self.assertEqual(part.filename_raw, wire.get_filename())
                        self.assertNotIn('\ufffd', part.filename_raw)
        self.assertEqual(attachment_count, 30)

    def test_all_body_fragment_offsets_reconstruct_full_plain_and_html_text(self):
        message_kinds = Counter()
        for name, raw in self.messages:
            with self.subTest(message=name):
                for part in parse_eml(raw).parts:
                    if not part.part_role.startswith('body_'):
                        continue
                    rows = fragments(part.text)
                    if part.part_role == 'body_plain':
                        message_kinds.update({row['fragment_kind'] for row in rows})
                    self.assertEqual(''.join(r['text'] for r in rows), part.text)
                    for row in rows:
                        self.assertEqual(row['text'], part.text[row['char_start']:row['char_end']])
        self.assertEqual(message_kinds, {'authored': 56, 'signature': 56, 'quoted': 55, 'forwarded': 1})

    def test_all_native_attachment_tokens_and_expected_ocr_gaps(self):
        counts = Counter()
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
              's': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        for name, raw in self.messages:
            for part in parse_eml(raw).parts:
                if not part.is_explicit_attachment:
                    continue
                with self.subTest(message=name, attachment=part.filename_raw):
                    result = _extract_attachment(part)
                    counts[result['status']] += 1
                    suffix = Path(part.filename_raw).suffix
                    if suffix in {'.docx', '.xlsx'}:
                        self.assertEqual(result['status'], 'ready')
                        with zipfile.ZipFile(io.BytesIO(part.raw_payload)) as doc:
                            self.assertIsNone(doc.testzip())
                            for member in doc.namelist():
                                if suffix == '.docx' and member.startswith('word/') and member.endswith('.xml'):
                                    tokens = ET.fromstring(doc.read(member)).findall('.//w:t', ns)
                                elif suffix == '.xlsx' and member.startswith('xl/worksheets/') and member.endswith('.xml'):
                                    root = ET.fromstring(doc.read(member))
                                    tokens = root.findall('.//s:v', ns) + root.findall('.//s:t', ns)
                                else:
                                    continue
                                for token in tokens:
                                    if token.text and token.text.strip():
                                        self.assertIn(token.text, result['text'])
                    elif result['status'] == 'ocr_pending':
                        self.assertEqual(result['text'], '')
                        if suffix == '.pdf':
                            self.assertTrue(result['meta']['missing_pages'])
                            self.assertEqual(result['coverage_ratio'], 0)
        self.assertEqual(counts, {'ready': 22, 'ocr_pending': 8})

    def test_same_named_attachment_versions_keep_distinct_hashes(self):
        versions = {}
        for _, raw in self.messages:
            for part in parse_eml(raw).parts:
                if part.is_explicit_attachment:
                    versions.setdefault(part.filename_raw, []).append(part.payload_sha256)
        self.assertEqual(len(set(versions['报价单.xlsx'])), 3)
        self.assertEqual(len(set(versions['技术方案说明书.docx'])), 2)
        self.assertEqual(len(set(versions['营业执照及资质证书-扫描件.pdf'])), 1)
        self.assertEqual(sum(len(v) for v in versions.values()), 30)


if __name__ == '__main__':
    unittest.main()
