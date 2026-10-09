"""Regression scenarios for original content, MIME attribution and safe reading."""
import base64
import quopri
import sqlite3
import unittest
from email.message import EmailMessage

from services.mail.foundation import _reader_parts, fragments, retain_content
from services.mail.html_safety import cid_image_urls, sanitize_html
from services.mail.normalization import parse_mail_date
from services.mail.parsers.eml import parse_eml


class MailContentAuditTests(unittest.TestCase):
    def test_attached_multipart_is_not_promoted_into_outer_body(self):
        outer = EmailMessage()
        outer['Date'] = 'Tue, 07 May 2024 15:38:50 +0700'
        outer.set_content('Outer authored content')
        attached = EmailMessage()
        attached.set_content('Attached plain content')
        attached.add_alternative('<p>Attached HTML content</p>', subtype='html')
        attached['Content-Disposition'] = 'attachment; filename="attached.mime"'
        outer.make_mixed(); outer.attach(attached)
        parsed = parse_eml(outer.as_bytes())
        bodies = [p.text for p in parsed.parts if p.part_role.startswith('body_')]
        self.assertEqual(bodies, ['Outer authored content\n'])
        attachment = next(p for p in parsed.parts if p.is_explicit_attachment)
        self.assertIn(b'Attached plain content', attachment.raw_payload)
        self.assertIn(b'Attached HTML content', attachment.raw_payload)
        self.assertEqual(attachment.part_role, 'attachment')

    def test_related_nonroot_text_is_a_resource_and_start_selects_body(self):
        raw = (b'Date: Tue, 07 May 2024 15:38:50 +0700\r\n'
               b'Content-Type: multipart/related; boundary=x; start="<main>"\r\n\r\n'
               b'--x\r\nContent-Type: text/plain\r\nContent-ID: <resource>\r\n\r\nresource text\r\n'
               b'--x\r\nContent-Type: text/html\r\nContent-ID: <main>\r\n\r\n<p>Main body</p>\r\n--x--\r\n')
        parts = parse_eml(raw).parts
        self.assertEqual([p.part_role for p in parts], ['inline', 'body_html'])
        self.assertEqual(parts[1].text, 'Main body')
        self.assertEqual(parts[0].raw_payload, b'resource text')

    def test_nested_wire_bytes_keep_crlf_and_unfolded_original_headers(self):
        inner = (b'From: nested@example.com\r\nSubject: Original\r\n\tcontinued header\r\n'
                 b'Date: Tue, 07 May 2024 15:38:50 +0700\r\nContent-Type: text/plain\r\n\r\nNested body\r\n')
        raw = (b'Content-Type: multipart/mixed; boundary=x\r\n\r\n'
               b'--x\r\nContent-Type: text/plain\r\n\r\nOuter body\r\n'
               b'--x\r\nContent-Type: message/rfc822\r\n\r\n' + inner + b'\r\n--x--\r\n')
        nested = next(p for p in parse_eml(raw).parts if p.part_role == 'nested_message')
        self.assertEqual(nested.raw_payload, inner)
        self.assertEqual(parse_eml(nested.raw_payload).parts[0].text, 'Nested body\r\n')

    def test_encoded_nested_message_keeps_decoded_content_and_own_participants(self):
        inner = EmailMessage()
        inner['From'] = 'inner@example.com'
        inner['To'] = 'other@example.com'
        inner['Subject'] = 'Nested 中文'
        inner['Date'] = 'Tue, 07 May 2024 15:38:50 +0700'
        inner.set_content('Nested content 中文 / ภาษาไทย')
        inner.add_attachment(b'attachment bytes', maintype='application', subtype='octet-stream', filename='nested.bin')
        original = inner.as_bytes()
        for encoding, encoded in [('base64', base64.encodebytes(original)), ('quoted-printable', quopri.encodestring(original))]:
            with self.subTest(encoding=encoding):
                raw = (b'From: outer@example.com\r\nDate: Tue, 07 May 2024 15:38:50 +0700\r\n'
                       b'Content-Type: message/rfc822\r\nContent-Transfer-Encoding: ' + encoding.encode() + b'\r\n\r\n' + encoded)
                parsed = parse_eml(raw)
                self.assertEqual(parsed.parts[0].raw_payload, original)
                self.assertEqual([p[1] for p in parsed.participants], ['outer@example.com'])
                nested = _reader_parts(parsed)[0]['nested']
                self.assertIn('中文 / ภาษาไทย', nested['parts'][0]['text'])
                self.assertEqual(nested['parts'][1]['filename_raw'], 'nested.bin')
                self.assertEqual(nested['participants'][0]['address'], 'inner@example.com')

    def test_thai_and_chinese_declared_encodings_are_preserved(self):
        for charset, value, codec in [('windows-874', 'ภาษาไทย', 'cp874'), ('gb18030', '中文𠀀', 'gb18030')]:
            with self.subTest(charset=charset):
                raw = (b'Date: Tue, 07 May 2024 15:38:50 +0700\r\nContent-Type: text/plain; charset=' + charset.encode() + b'\r\n\r\n' + value.encode(codec))
                parsed = parse_eml(raw)
                self.assertEqual(parsed.parts[0].text, value)
                self.assertTrue(parsed.content_complete)
                self.assertEqual(parsed.sent_at_utc, '2024-05-07T08:38:50+00:00')
                self.assertEqual(parsed.timezone_offset_minutes, 420)

    def test_invalid_text_encoding_marks_projection_incomplete_without_dropping_bytes(self):
        parsed = parse_eml(b'Date: Tue, 07 May 2024 15:38:50 +0700\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nvalid\xffbytes')
        self.assertFalse(parsed.content_complete)
        self.assertFalse(parsed.parts[0].content_complete)
        self.assertEqual(parsed.parts[0].raw_payload, b'valid\xffbytes')
        self.assertTrue(parsed.parts[0].warnings)
        self.assertIn('valid', parsed.parts[0].text)

    def test_part_limit_reports_incomplete_projection(self):
        mail = EmailMessage(); mail.set_content('first')
        mail.add_attachment(b'second', maintype='application', subtype='octet-stream', filename='second.bin')
        parsed = parse_eml(mail.as_bytes(), max_parts=1)
        self.assertFalse(parsed.content_complete)
        self.assertEqual(len(parsed.parts), 1)
        self.assertTrue(any('部件数超过' in warning for warning in parsed.warnings))

    def test_empty_body_is_an_explicit_parse_warning(self):
        parsed = parse_eml(b'Date: Tue, 07 May 2024 15:38:50 +0700\r\n\r\n')
        self.assertEqual(parsed.parse_status, 'parsed_with_warnings')
        self.assertTrue(any('正文为空' in warning for warning in parsed.warnings))

    def test_local_cid_image_preview_keeps_html_source_and_discards_active_content(self):
        mail = EmailMessage(); mail.set_content('plain')
        html = '<p>Hello</p><img src="cid:poster%40example.com" alt="中文"><img src="https://tracker.invalid/track"><script>alert(1)</script>'
        mail.add_alternative(html, subtype='html')
        mail.get_payload()[1].add_related(b'\x89PNG\r\n\x1a\nfixture', maintype='image', subtype='png', cid='<poster@example.com>', filename='poster.png', disposition='inline')
        parsed = parse_eml(mail.as_bytes())
        body = next(p for p in _reader_parts(parsed) if p['part_role'] == 'body_html')
        self.assertEqual(body['html_raw'], html + '\n')
        self.assertIn('<img src="data:image/png;base64,', body['html'])
        self.assertIn('alt="中文"', body['html'])
        self.assertNotIn('tracker.invalid', body['html'])
        self.assertNotIn('script', body['html'])
        image = next(p for p in _reader_parts(parsed) if p['content_type'] == 'image/png')
        self.assertFalse(image['is_explicit_attachment'])
        self.assertTrue(image['preview_data_url'].startswith('data:image/png;base64,'))

    def test_missing_cid_and_duplicate_ids_do_not_silently_choose_a_resource(self):
        for count in [0, 2]:
            with self.subTest(count=count):
                mail = EmailMessage(); mail.set_content('<img src="cid:same">', subtype='html'); mail.make_related()
                for n in range(count):
                    mail.add_related(b'image-' + str(n).encode(), maintype='image', subtype='png', cid='<same>', disposition='inline')
                body = next(p for p in _reader_parts(parse_eml(mail.as_bytes())) if p['part_role'] == 'body_html')
                self.assertIn('内嵌图片未能预览', body['html'])
                self.assertNotIn('<img', body['html'])

    def test_primary_reader_uses_local_authenticated_image_endpoint(self):
        mail = EmailMessage(); mail.set_content('<p>Hello</p><img src="cid:poster">', subtype='html')
        mail.make_related()
        mail.add_related(b'\x89PNG\r\n\x1a\nfixture', maintype='image', subtype='png', cid='<poster>', disposition='inline')
        parsed = parse_eml(mail.as_bytes())
        parts = _reader_parts(parsed, cid_urls=cid_image_urls('message', 'package', parsed.parts))
        html = next(p['html'] for p in parts if p['part_role'] == 'body_html')
        self.assertIn('/api/mail/messages/message/parts/2/preview-image?package_id=package', html)
        self.assertNotIn('data:', html)

    def test_discarded_void_html_tags_do_not_hide_the_rest_of_the_body(self):
        html = ('<html><head><meta charset="utf-8"><link href="https://tracker.invalid/css">'
                '<base href="https://tracker.invalid"></head><body><p>Complete body 中文</p>'
                '<embed src="https://tracker.invalid/plugin"><p>After embedded tag</p></body></html>')
        preview = sanitize_html(html)
        self.assertIn('Complete body 中文', preview)
        self.assertIn('After embedded tag', preview)
        self.assertNotIn('tracker.invalid', preview)

    def test_reply_marker_is_conservative_for_unprefixed_inline_answers(self):
        body = 'New text\r\nOn Tue, A wrote:\r\n> Question\r\nMy inline answer\r\n'
        rows = fragments(body)
        self.assertEqual(''.join(p['text'] for p in rows), body)
        self.assertEqual(rows[-1]['fragment_kind'], 'uncertain')
        for row in rows:
            self.assertEqual(row['text'], body[row['char_start']:row['char_end']])

    def test_reparse_removes_stale_body_fragment_paths(self):
        connection = sqlite3.connect(':memory:')
        try:
            connection.execute('CREATE TABLE mail_raw_store(raw_sha256 TEXT PRIMARY KEY, raw_bytes BLOB)')
            connection.execute('CREATE TABLE mail_body_fragments(message_key TEXT,mime_path TEXT,ordinal INTEGER,fragment_kind TEXT,char_start INTEGER,char_end INTEGER,text TEXT,method TEXT,confidence TEXT)')
            connection.execute("INSERT INTO mail_body_fragments VALUES('message','old.attached.body',0,'authored',0,5,'stale','old','uncertain')")
            raw = b'Content-Type: text/plain\r\n\r\nCurrent body'
            retain_content(connection, 'message', raw, parse_eml(raw))
            rows = connection.execute('SELECT mime_path,text FROM mail_body_fragments').fetchall()
            self.assertEqual(rows, [('1', 'Current body')])
            retain_content(connection, 'message', raw, parse_eml(raw))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM mail_body_fragments').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT raw_bytes FROM mail_raw_store').fetchone()[0], raw)
        finally:
            connection.close()

    def test_utc_boundary_overflow_and_unknown_timezone_keep_date_unresolved(self):
        self.assertEqual(parse_mail_date('Fri, 31 Dec 9999 23:59:59 -1400'), (None, None))
        self.assertEqual(parse_mail_date('Tue, 07 May 2024 15:38:50 -0000'), (None, None))


if __name__ == '__main__':
    unittest.main()
