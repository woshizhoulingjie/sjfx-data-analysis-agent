"""Copies, original-source downloads and status indicators use the same scope."""
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.mail.attachments import read_message_raw
from services.mail.conversations import list_conversations
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.people import correspondence
from services.mail.repository import MailRepository


class _Storage:
    def __init__(self, path):
        self.db_path = str(path)
        self.lock = threading.Lock()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _raw(*, body='Complete body', to='bob@example.com', attachment=False):
    message = EmailMessage()
    message['From'] = 'alice@example.com'
    message['To'] = to
    message['Subject'] = 'Copy review'
    message['Message-ID'] = '<same@example.com>'
    message['Date'] = 'Thu, 10 Sep 2026 08:00:00 +0000'
    message.set_content(body)
    if attachment:
        message.add_attachment(b'original attachment', maintype='text', subtype='plain', filename='notes.txt')
    return message.as_bytes()


class StorageAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.r = MailRepository(_Storage(self.root / 'mail.db'))
        self.r.migrate()
        self.p = self.package('owner')
        self.pid = self.p['package_id']
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def package(self, owner):
        return self.r.create_package(owner_id=owner, display_name='test', source_kind='directory',
                                     source_path=str(self.root), source_revision='v1', config={})[0]

    def ingest(self, raw, package=None, name=None, ordinal=None):
        package = package or self.p
        ordinal = self.n if ordinal is None else ordinal
        name = name or 'source-{}.eml'.format(ordinal)
        path = self.root / name
        path.write_bytes(raw)
        self.n += 1
        return self.r.upsert_ingested_message(package_id=package['package_id'],
            source={'locator_type': 'directory', 'root_path': str(self.root), 'source_revision': 'v1'},
            member=MailMember(name, ordinal, len(raw), {'kind': 'file', 'path': str(path)}),
            raw=raw, parsed=parse_eml(raw))

    def test_transport_copies_keep_every_original_but_count_once(self):
        raw = _raw(attachment=True)
        one = self.ingest(raw)
        variant = b'Received: from export.example by local.example\nDelivered-To: bob@example.com\n' + raw
        two = self.ingest(variant)
        self.assertEqual(one['message_key'], two['message_key'])
        self.assertEqual(two['dedup_basis'], 'transport_copy')
        key = one['message_key']
        self.assertEqual(read_message_raw(self.r, key, 'owner', self.pid, two['occurrence_id']), variant)
        self.assertEqual(read_message_raw(self.r, key, 'owner', self.pid, one['occurrence_id']), raw)
        with patch.object(Config, 'MAIL_ENABLE_CONVERSATIONS', True):
            self.r.finish_import(self.pid, 'owner')
            self.assertEqual([c['message_count'] for c in list_conversations(self.r, self.pid, 'owner')], [1])
        self.assertEqual(correspondence(self.r, self.pid, 'owner', 'alice@example.com')['role_counts']['sent'], 1)
        with self.r._connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM mail_raw_store').fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mail_text_segments WHERE source_kind='body'").fetchone()[0], 1)
            self.assertEqual(dict(connection.execute('SELECT status,COUNT(*) FROM mail_import_entries GROUP BY status')), {'duplicate': 1, 'ready': 1})

    def test_same_id_changed_content_or_recipient_stays_distinct(self):
        keys = {self.ingest(_raw())['message_key'], self.ingest(_raw(body='Changed'))['message_key'],
                self.ingest(_raw(to='carol@example.com'))['message_key']}
        self.assertEqual(len(keys), 3)

    def test_different_owners_never_share_transport_only_headers(self):
        one = self.ingest(_raw())
        other = self.package('other')
        two = self.ingest(b'Delivered-To: private@example.com\n' + _raw(), other)
        self.assertNotEqual(one['message_key'], two['message_key'])
        with self.assertRaises(LookupError):
            read_message_raw(self.r, two['message_key'], 'owner', other['package_id'])

    def test_source_download_does_not_accept_occurrence_from_another_mail(self):
        one = self.ingest(_raw())
        two = self.ingest(_raw(body='different'))
        with self.assertRaises(LookupError):
            read_message_raw(self.r, one['message_key'], 'owner', self.pid, two['occurrence_id'])

    def test_changed_source_is_rejected_and_keeps_original(self):
        one = self.ingest(_raw(), name='fixed.eml', ordinal=0)
        with self.assertRaises(ValueError):
            self.ingest(_raw(body='changed'), name='fixed.eml', ordinal=0)
        self.assertEqual(read_message_raw(self.r, one['message_key'], 'owner', self.pid), _raw())
        self.assertEqual(self.r.list_messages(self.pid, 'owner')['total'], 1)

    def test_list_indicators_distinguish_original_and_attachment_text(self):
        key = self.ingest(_raw(attachment=True))['message_key']
        row = self.r.list_messages(self.pid, 'owner')['items'][0]
        self.assertEqual((row['original_preserved'], row['body_empty'], row['attachment_incomplete']), (1, 0, 1))
        part = next(p for p in self.r.get_message(key, 'owner', self.pid)['parts'] if p['is_explicit_attachment'])
        self.r.update_processing(key, part['mime_path'], 'basic_text', 'ready', text='original attachment', coverage_ratio=1)
        self.assertEqual(self.r.list_messages(self.pid, 'owner')['items'][0]['attachment_incomplete'], 0)
        self.r.update_processing(key, part['mime_path'], 'ocr', 'terminal_failed', error={'message': 'missing page'})
        self.assertEqual(self.r.list_messages(self.pid, 'owner')['items'][0]['attachment_incomplete'], 1)

    def test_parser_upgrade_retires_obsolete_part_without_deleting_original(self):
        raw = _raw(attachment=True)
        key = self.ingest(raw)['message_key']
        parsed = parse_eml(raw)
        attachment = next(p for p in parsed.parts if p.is_explicit_attachment)
        with self.r._connection() as connection:
            self.r._insert_parts(connection, key, [p for p in parsed.parts if p is not attachment])
            retired = connection.execute('SELECT part_role,is_explicit_attachment FROM mail_parts WHERE message_key=? AND mime_path=?', (key, attachment.mime_path)).fetchone()
        self.assertEqual(tuple(retired), ('retired', 0))
        self.assertFalse(any(p['mime_path'] == attachment.mime_path for p in self.r.get_message(key, 'owner', self.pid)['parts']))
        self.assertEqual(read_message_raw(self.r, key, 'owner', self.pid), raw)


class ImagePreviewAuditTests(unittest.TestCase):
    def test_html_blockquote_is_labeled_as_history_and_inline_reply_remains_authored(self):
        from services.mail.foundation import fragments
        message = EmailMessage()
        html = '<p>New response</p><blockquote><p>Earlier statement</p></blockquote><p>Inline answer</p>'
        message.set_content(html, subtype='html')
        part = parse_eml(message.as_bytes()).parts[0]
        values = fragments(part.text)
        self.assertTrue(any(v['fragment_kind']=='quoted' and 'Earlier statement' in v['text'] for v in values))
        self.assertTrue(any(v['fragment_kind']=='authored' and 'Inline answer' in v['text'] for v in values))
        self.assertEqual(part.html_raw, html+'\n')
        self.assertEqual(''.join(v['text'] for v in values), part.text)

    def test_cid_images_are_local_unique_and_external_images_are_described(self):
        from services.mail.html_safety import cid_image_urls, sanitize_html
        part = {'content_id': '<image@local>', 'mime_path': '1.2', 'content_type': 'image/png', 'is_inline_resource': True}
        urls = cid_image_urls('mail', 'package', [part])
        rendered = sanitize_html('<meta charset="utf-8"><p>Complete</p><img src="cid:image%40local"><img src="https://remote.invalid/tracker" alt="Remote chart">', urls)
        self.assertIn('Complete', rendered)
        self.assertIn('preview-image?package_id=package', rendered)
        self.assertNotIn('remote.invalid', rendered)
        self.assertIn('Remote chart', rendered)
        self.assertEqual(cid_image_urls('mail', 'package', [part, part]), {})

    def test_image_preview_and_copy_download_enforce_owner_package_and_type(self):
        import base64
        from fastapi.testclient import TestClient
        from services.mail.api import register_mail_routes
        from services.mail.bootstrap import build_mail_services
        from web_compat import SJFXFastAPI
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = ['owner']
            services = build_mail_services(_Storage(root / 'api.db'), owner_resolver=lambda: owner[0])
            r = services.repository
            r.migrate()
            package = r.create_package(owner_id='owner', display_name='test', source_kind='directory',
                source_path=str(root), source_revision='v1', config={})[0]
            pid = package['package_id']
            message = EmailMessage()
            message['From'], message['To'] = 'alice@example.com', 'bob@example.com'
            message['Message-ID'], message['Date'] = '<image@example.com>', 'Thu, 10 Sep 2026 08:00:00 +0000'
            message.set_content('<p>Original body</p><img src="cid:photo@local">', subtype='html')
            png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5ZkAAAAASUVORK5CYII=')
            message.add_related(png, maintype='image', subtype='png', cid='<photo@local>', disposition='inline')
            message.add_related(b'<html>invalid raster</html>', maintype='image', subtype='png', cid='<bad@local>', disposition='inline')
            original = message.as_bytes()
            row = r.upsert_ingested_message(package_id=pid, source={'locator_type':'directory','root_path':str(root),'source_revision':'v1'},
                member=MailMember('image.eml', 0, len(original), {'kind':'file','path':'unused'}), raw=original, parsed=parse_eml(original))
            key = row['message_key']
            paths = {p['content_id']: p['mime_path'] for p in r.get_message(key, 'owner', pid)['parts'] if p['content_id']}
            app = SJFXFastAPI()
            register_mail_routes(app, services)
            with patch.object(Config, 'MAIL_FEATURE_ENABLED', True), TestClient(app) as client:
                page = client.get('/mail-research')
                self.assertEqual(page.status_code, 200)
                self.assertIn("img-src 'self' data: blob:", page.headers['content-security-policy'])
                self.assertIn('20261003-foundation-v3', page.text)
                url = '/api/mail/messages/' + key + '/parts/' + paths['<photo@local>'] + '/preview-image'
                response = client.get(url, params={'package_id':pid})
                self.assertEqual(response.content, png)
                self.assertEqual(response.headers['content-type'], 'image/png')
                self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
                bad = '/api/mail/messages/' + key + '/parts/' + paths['<bad@local>'] + '/preview-image'
                self.assertEqual(client.get(bad, params={'package_id':pid}).status_code, 400)
                download = client.get('/api/mail/messages/' + key + '/raw', params={'package_id':pid,'occurrence_id':row['occurrence_id']})
                self.assertEqual(download.content, original)
                self.assertEqual(client.get(url, params={'package_id':'other'}).status_code, 404)
                owner[0] = 'intruder'
                self.assertEqual(client.get(url, params={'package_id':pid}).status_code, 404)
