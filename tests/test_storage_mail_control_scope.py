"""Unified SQL journal with real proof Store and synthetic frozen authority."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_package_cache_provenance import FakePackage, context
from sjfx_protection import business, cache
from sjfx_protection.storage_cache import connection
from sjfx_protection.store import Store, ProtectionError


PAUSE = '已安全暂停；已完成结果永久保留，未完成文件仍在待处理队列。'


class UnifiedMailControlScopes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'approved'
        self.root.mkdir()
        self.child = self.root / 'registered-child'
        self.child.mkdir()
        self.store = Store(Path(self.tmp.name) / 'proofs')
        self.package = FakePackage()
        self.ctx = context()
        self.package.current['A'] = copy.deepcopy(self.ctx)
        self.package._registration = lambda owner, rid: {'id': rid, 'root': str(self.root)}
        self.package.resolve = lambda owner, root=None, **kw: {
            'id': 'B' if Path(root).is_relative_to(self.child) else 'A'}
        self.old = business._configured
        business.configure(SimpleNamespace(enabled=True, store=self.store, package=self.package))
        self.context_token = business.execution.set(self.ctx)
        self.metadata_token = business.metadata_projection.set(False)
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE scans(id TEXT PRIMARY KEY, owner_id TEXT, root_path TEXT);
            CREATE TABLE mail_packages(package_id TEXT PRIMARY KEY, owner_id TEXT, source_path TEXT);
            CREATE TABLE analysis_jobs(id TEXT PRIMARY KEY, scan_id TEXT, owner_id TEXT, task_type TEXT, result TEXT);
            CREATE TABLE analysis_progress(scan_id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE unified_documents(scan_id TEXT PRIMARY KEY, payload TEXT);
        ''')
        self.db.executemany('INSERT INTO scans VALUES(?,?,?)', [
            ('scan-A', 'alice', str(self.root)), ('scan-B', 'bob', str(self.root)),
            ('same-id', 'alice', str(self.root))])
        self.db.executemany('INSERT INTO mail_packages VALUES(?,?,?)', [
            ('mail-A', 'alice', str(self.root)), ('mail-B', 'bob', str(self.root)),
            ('mail-child', 'alice', str(self.child)), ('same-id', 'alice', str(self.root))])
        self.db.commit()
        self.namespace = str(Path(self.tmp.name) / 'business.db')

    def tearDown(self):
        self.db.close()
        business.metadata_projection.reset(self.metadata_token)
        business.execution.reset(self.context_token)
        business.configure(self.old)
        self.tmp.cleanup()

    def checked(self):
        return connection(self.db, self.namespace)

    def insert_job(self, identifier='job', package='mail-A', owner='alice', task='mail_extract', result=None):
        with self.checked() as db:
            db.execute('INSERT INTO analysis_jobs VALUES(?,?,?,?,?)',
                       (identifier, package, owner, task, result))

    def read_result(self, identifier='job'):
        with self.checked() as db:
            return db.execute('SELECT result FROM analysis_jobs WHERE id=?', (identifier,)).fetchone()

    def write_progress(self, scan='scan-A', payload=None):
        with self.checked() as db:
            db.execute('INSERT OR REPLACE INTO analysis_progress VALUES(?,?)',
                       (scan, json.dumps(payload or {'status': 'paused', 'stage': 'paused', 'message': PAUSE, 'progress': 0})))

    def proof_count(self):
        with self.store.db() as db:
            exists = db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (cache.TABLE,)).fetchone()
            return db.execute('SELECT COUNT(*) FROM ' + cache.TABLE).fetchone()[0] if exists else 0

    def owner_only(self, flag=True):
        business.execution.set({'owner': 'alice'})
        business.metadata_projection.set(flag)

    def test_mail_derivative_queue_and_result_publication(self):
        self.insert_job()
        self.assertEqual(self.proof_count(), 0)
        with self.checked() as db:
            db.execute('UPDATE analysis_jobs SET result=? WHERE id=?', ('{"processed":2}', 'job'))
        self.assertEqual(json.loads(self.read_result()[0]), {'processed': 2})
        self.assertEqual(self.proof_count(), 1)
        self.assertTrue(self.package.calls)

    def test_raw_intake_queue_null_allows_first_protected_result(self):
        self.db.execute('INSERT INTO analysis_jobs VALUES(?,?,?,?,NULL)',
                        ('job', 'mail-A', 'alice', 'mail_import'))
        self.db.commit()
        self.assertIsNone(self.read_result()[0])
        self.assertEqual(self.proof_count(), 0)
        with self.checked() as db:
            db.execute('UPDATE analysis_jobs SET result=? WHERE id=?', ('{"processed":2}', 'job'))
        self.assertEqual(json.loads(self.read_result()[0]), {'processed': 2})
        self.assertEqual(self.proof_count(), 1)

    def test_text_values_cannot_impersonate_raw_intake_sql_null(self):
        for value in ('null', '{}', 'None', '{"result":null}'):
            with self.subTest(value=value):
                self.db.execute('INSERT OR REPLACE INTO analysis_jobs VALUES(?,?,?,?,?)',
                                ('job', 'mail-A', 'alice', 'mail_import', value))
                self.db.commit()
                with self.assertRaises(ProtectionError) as caught:
                    self.read_result()
                self.assertEqual(caught.exception.code, 'cache_provenance_missing')
                with self.assertRaises(ProtectionError):
                    with self.checked() as db:
                        db.execute('UPDATE analysis_jobs SET result=? WHERE id=?', ('{"processed":2}', 'job'))
                self.assertEqual(self.db.execute('SELECT result FROM analysis_jobs').fetchone()[0], value)
                self.assertEqual(self.proof_count(), 0)

    def test_mail_all_actual_task_types_share_owner_and_source_checks(self):
        for task in ('mail_import', 'mail_extract', 'mail_ocr', 'mail_research',
                     'mail_export', 'mail_foundation', 'mail_embed', 'mail_factextract'):
            with self.subTest(task=task):
                self.insert_job(identifier=task, task=task)
                self.assertIsNotNone(self.read_result(task))

    def test_foreign_missing_unknown_and_independent_child_reject_atomically(self):
        for package, owner, task in [('mail-B', 'bob', 'mail_extract'), ('mail-A', 'bob', 'mail_extract'),
                                    ('mail-A', '', 'mail_extract'), ('unknown', 'alice', 'mail_extract'),
                                    ('mail-child', 'alice', 'mail_extract'), ('mail-A', 'alice', 'analyze_package'),
                                    ('mail-A', 'alice', 'mail_unknown')]:
            with self.subTest(package=package, owner=owner, task=task):
                with self.assertRaises(ProtectionError) as caught:
                    self.insert_job(package=package, owner=owner, task=task)
                self.assertEqual(caught.exception.code, 'cache_scope_mismatch')
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_jobs').fetchone()[0], 0)
                self.assertEqual(self.proof_count(), 0)

    def test_mail_result_tamper_and_changed_source_version_reject(self):
        self.insert_job(result='{"processed":2}')
        newer = context(version='v2')
        self.package.current['A'] = copy.deepcopy(newer)
        business.execution.set(newer)
        with self.assertRaises(ProtectionError) as caught:
            self.read_result()
        self.assertEqual(caught.exception.code, 'context_stale')
        self.package.current['A'] = copy.deepcopy(self.ctx)
        business.execution.set(self.ctx)
        self.db.execute('UPDATE analysis_jobs SET result=? WHERE id=?', ('{"processed":99}', 'job'))
        self.db.commit()
        with self.assertRaises(ProtectionError) as caught:
            self.read_result()
        self.assertEqual(caught.exception.code, 'cache_output_mismatch')

    def test_revocation_still_blocks_mail_publication(self):
        self.package.blocked.add('A')
        with self.assertRaises(ProtectionError) as caught:
            self.insert_job(result='{"processed":2}')
        self.assertEqual(caught.exception.code, 'package_revoked')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_jobs').fetchone()[0], 0)

    def test_same_id_in_ordinary_and_mail_domains_cannot_reuse_result_proof(self):
        self.insert_job(package='same-id', result='{"processed":2}')
        self.db.execute("UPDATE analysis_jobs SET task_type='analyze_package' WHERE id='job'")
        self.db.commit()
        with self.assertRaises(ProtectionError) as caught:
            self.read_result()
        self.assertEqual(caught.exception.code, 'cache_provenance_missing')

    def test_owner_only_pause_metadata_and_typed_queue_result_never_stamp_content(self):
        self.owner_only()
        self.write_progress()
        self.write_progress()
        self.insert_job(package='scan-A', task='scan_and_analyze', result='{"scan_id":"scan-A","_await_selection":true}')
        self.assertEqual(self.proof_count(), 0)
        self.assertEqual(self.package.calls, [])
        self.assertEqual(json.loads(self.db.execute('SELECT payload FROM analysis_progress').fetchone()[0])['message'], PAUSE)

    def test_owner_only_metadata_cannot_authorize_mail_results_or_other_content(self):
        self.owner_only()
        for action in (lambda: self.insert_job(result='{"processed":2}'),
                       lambda: self.insert_job(package='same-id', result='{"scan_id":"same-id"}')):
            with self.assertRaises(ProtectionError):
                action()
        with self.assertRaises(ProtectionError):
            with self.checked() as db:
                db.execute('INSERT INTO unified_documents VALUES(?,?)', ('scan-A', '{"text":"source text"}'))
        self.assertEqual(self.proof_count(), 0)

    def test_metadata_flag_owner_ids_and_payload_shape_are_strict(self):
        self.owner_only()
        for scan, payload in [('scan-B', {'status': 'paused', 'message': PAUSE}),
                              ('missing', {'status': 'paused', 'message': PAUSE}),
                              ('scan-A', {'status': 'paused', 'message': 'source-derived notice'}),
                              ('scan-A', {'status': 'arbitrary', 'message': PAUSE}),
                              ('scan-A', {'status': 'paused', 'message': PAUSE, 'text': 'source text'}),
                              ('scan-A', {'status': 'paused', 'message': PAUSE, 'progress': True})]:
            with self.subTest(scan=scan, payload=payload):
                with self.assertRaises(ProtectionError):
                    self.write_progress(scan, payload)
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_progress').fetchone()[0], 0)
        for flag in (False, 1, 'true'):
            with self.subTest(flag=flag):
                self.owner_only(flag)
                with self.assertRaises(ProtectionError):
                    self.write_progress()
        self.assertEqual(self.proof_count(), 0)

    def test_old_content_progress_and_proof_survive_failed_owner_only_update(self):
        self.write_progress(payload={'status': 'running', 'message': 'source text', 'text': 'source body'})
        before = self.db.execute('SELECT payload FROM analysis_progress').fetchone()[0]
        proofs = self.proof_count()
        self.owner_only()
        current = json.loads(before)
        current.update(status='paused', message=PAUSE, stage='paused')
        with self.assertRaises(ProtectionError):
            self.write_progress(payload=current)
        self.assertEqual(self.db.execute('SELECT payload FROM analysis_progress').fetchone()[0], before)
        self.assertEqual(self.proof_count(), proofs)


if __name__ == '__main__':
    unittest.main()
