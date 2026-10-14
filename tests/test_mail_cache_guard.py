"""MailRepository production/SQL/provenance probes on real temporary SQLite."""
import copy
import json
import sqlite3
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import test_package_cache_provenance as provenance
from sjfx_protection import business, cache
from sjfx_protection.store import ProtectionError
from services.mail.repository import MailRepository
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.translation_quality import SegmentMemory


class SyntheticStorage:
    def __init__(self, path):
        self.db_path = str(path)
        self.lock = threading.RLock()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


class MailCacheTests(unittest.TestCase):
    def setUp(self):
        provenance.CacheProvenanceTests.setUp(self)
        self.root = Path(self.tmp.name) / 'approved-source'
        self.root.mkdir()
        self.package._registration = lambda owner, rid: {'root': str(self.root), 'id': rid}
        self.package.resolve = lambda owner, root=None, **kw: {'id': 'A', 'root': str(self.root)}
        self.storage = SyntheticStorage(Path(self.tmp.name) / 'agent.db')
        self.repo = MailRepository(self.storage)
        self.repo.migrate()
        self.pid = self.repo.create_package(owner_id='alice', display_name='test', source_kind='directory', source_path=str(self.root), source_revision='source-v1', config={})[0]['package_id']
        self.raw = b'From: Alice <alice@example.com>\r\nTo: Bob <bob@example.com>\r\nSubject: Approved contract\r\nMessage-ID: <synthetic@example.com>\r\n\r\nThis is verified source text.\r\n'
        self.parsed = parse_eml(self.raw)
        self.member = MailMember('one.eml', 0, len(self.raw), {'kind': 'file', 'path': str(self.root / 'one.eml')})
        self.source = {'locator_type': 'directory', 'root_path': str(self.root), 'source_revision': 'source-v1'}
        self.raw_validator = patch.object(business, 'validated_cached_bytes', side_effect=self.validate_raw)
        self.raw_validator.start()

    def validate_raw(self, raw, name, owner=None):
        if raw != self.raw:
            raise ProtectionError('unapproved raw', 423, 'cached_version_unverified')
        return raw

    def tearDown(self):
        self.raw_validator.stop()
        provenance.CacheProvenanceTests.tearDown(self)

    def ingest(self):
        return self.repo.upsert_ingested_message(package_id=self.pid, source=self.source, member=self.member, raw=self.raw, parsed=self.parsed)

    def test_ingest_approved_raw_and_replay_exact_bytes(self):
        item = self.ingest()
        message = self.repo.get_message(item['message_key'], 'alice', self.pid)
        self.assertEqual(message['subject_raw'], 'Approved contract')
        replay = self.ingest()
        self.assertEqual(item['message_key'], replay['message_key'])
        with self.repo._connection() as db:
            texts = [row[0] for row in db.execute('SELECT text FROM mail_text_segments WHERE package_id=?', (self.pid,))]
        self.assertIn('verified source text', ''.join(texts))

    def test_unrecorded_history_rebuilt_only_from_actual_raw(self):
        self.p.enabled = False
        item = self.ingest()
        self.p.enabled = True
        with self.assertRaises(ProtectionError):
            self.repo.get_message(item['message_key'], 'alice', self.pid)
        self.ingest()
        message = self.repo.get_message(item['message_key'], 'alice', self.pid)
        self.assertEqual(message['subject_raw'], 'Approved contract')

    def test_research_direct_sql_steps_and_final_are_covered(self):
        self.ingest()
        session = self.repo.create_session(self.pid, 'alice', 'Research')
        turn = self.repo.create_turn(session['session_id'], 'alice', 'Question', {'type': 'synthetic'})
        tid = turn['turn_id']
        with self.repo._connection() as db:
            db.execute('UPDATE mail_research_turns SET final_json=? WHERE turn_id=?', (json.dumps({'answer': 'new result'}), tid))
            db.execute("INSERT INTO mail_research_steps(step_id,turn_id,sequence,tool_name,input_json,output_ref,status) VALUES('step',?,1,'direct','{}',?,'completed')", (tid, json.dumps({'stored': 'checked'})))
        with self.repo._connection() as db:
            self.assertEqual(json.loads(db.execute('SELECT final_json FROM mail_research_turns WHERE turn_id=?', (tid,)).fetchone()[0])['answer'], 'new result')
        with self.storage._connect() as db:
            db.execute('UPDATE mail_research_turns SET final_json=? WHERE turn_id=?', ('{"answer":"tampered"}', tid))
        with self.assertRaises(ProtectionError) as caught:
            with self.repo._connection() as db:
                db.execute('SELECT json_extract(final_json,\'$.answer\') FROM mail_research_turns WHERE turn_id=?', (tid,)).fetchall()
        self.assertEqual(caught.exception.code, 'cache_output_mismatch')

    def test_message_and_fragment_external_tamper(self):
        item = self.ingest()
        with self.storage._connect() as db:
            db.execute("UPDATE mail_body_fragments SET text='tampered' WHERE message_key=?", (item['message_key'],))
        with self.assertRaises(ProtectionError):
            with self.repo._connection() as db:
                db.execute('SELECT text FROM mail_body_fragments WHERE message_key=?', (item['message_key'],)).fetchall()

    def test_version_v2_cannot_reuse_v1_steps(self):
        self.ingest()
        session = self.repo.create_session(self.pid, 'alice', 'Research')
        turn = self.repo.create_turn(session['session_id'], 'alice', 'Question', {'type': 'synthetic'})
        newer = provenance.context(version='v2')
        self.package.current['A'] = copy.deepcopy(newer)
        business.execution.set(newer)
        with self.assertRaises(ProtectionError) as caught:
            self.repo.get_turn(turn['turn_id'], 'alice')
        self.assertEqual(caught.exception.code, 'context_stale')

    def test_fts_real_repository_search(self):
        self.ingest()
        results = self.repo.search_segments_lexical(self.pid, 'alice', 'verified')
        self.assertTrue(results)
        self.assertIn('verified', results['items'][0]['text'])

    def test_segment_memory_history_miss_tamper_and_version(self):
        memory = SegmentMemory(Path(self.tmp.name) / 'segments.db', ['alice', self.pid], 'model')
        self.p.enabled = False
        memory.put('old', {'text': 'historical translation'})
        self.p.enabled = True
        self.assertIsNone(memory.get('old'))
        memory.put('new', {'text': 'approved translation'})
        self.assertEqual(memory.get('new')['text'], 'approved translation')
        db = memory.connection()
        try:
            db.execute("UPDATE translations SET value='{}' WHERE key='new'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(ProtectionError):
            memory.get('new')

    def test_off_historical_rows_and_schema_migrate_compatible(self):
        self.p.enabled = False
        item = self.ingest()
        self.assertIsNotNone(self.repo.get_message(item['message_key'], 'alice', self.pid))
        self.assertFalse(self.repo.migrate())
        self.assertEqual(self.store.opens, 0)

    def test_cancel_metadata_remains_writable_without_content_sources(self):
        self.p.enabled = False
        session = self.repo.create_session(self.pid, 'alice', 'Old session')
        turn = self.repo.create_turn(session['session_id'], 'alice', 'Old question', {})
        self.repo.update_turn(turn['turn_id'], 'alice', job_id='job-cancel')
        self.p.enabled = True
        business.execution.set({})
        token = business.metadata_projection.set(True)
        try:
            self.repo.update_turn(turn['turn_id'], 'alice', status='cancelling')
            self.repo.event(turn['turn_id'], 'cancel_requested', {'job_id': 'job-cancel'})
            value = self.repo.get_turn(turn['turn_id'], 'alice')
            self.assertEqual(value['status'], 'cancelling')
            with self.assertRaises(ProtectionError) as caught:
                self.repo.event(turn['turn_id'], 'cancel_requested', {'job_id': 'job-cancel', 'answer': 'secret body'})
            self.assertEqual(caught.exception.code, 'cache_metadata_event_denied')
        finally:
            business.metadata_projection.reset(token)
        self.assertEqual(self.store.opens, 0)
        business.execution.set(self.ctx)
        with self.assertRaises(ProtectionError):
            self.repo.get_turn(turn['turn_id'], 'alice')


if __name__ == '__main__':
    unittest.main()
