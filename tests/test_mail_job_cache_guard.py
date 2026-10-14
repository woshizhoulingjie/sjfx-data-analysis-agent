"""Mail queue scopes against real SQLite, durable protection and local snapshots.

Static-only inspection is an explicit test fixture, never production admission.
No application lifespan, model or service is started.
"""
import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import Config
from services.mail.jobs import _queue_derivatives
from services.mail.repository import MailRepository
from services.storage import Storage
from sjfx_protection import business, cache, storage_cache
from sjfx_protection.registration import PackageProtection
from sjfx_protection.store import ProtectionError, Store


class MailJobCacheTests(unittest.TestCase):
    def setUp(self):
        self.old_service = business._configured
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        sources = self.base / 'sources'
        self.root = sources / 'mail'
        self.child = self.root / 'child'
        self.other = sources / 'other'
        for directory in (self.child, self.other):
            directory.mkdir(parents=True)
        raw = ('From: Alice <alice@example.com>\r\nTo: Bob <bob@example.com>\r\n'
               'Subject: Approved queue fixture\r\nMessage-ID: <queue@example.com>\r\n'
               '\r\nApproved source evidence.\r\n')
        self.file = self.root / 'one.eml'
        for path in (self.file, self.child / 'two.eml', self.other / 'three.eml'):
            path.write_bytes(raw.encode())
        self.store = Store(self.base / 'file_protection')
        self.package = PackageProtection(
            self.store, [str(sources)],
            authorize_root=lambda owner, root: owner == 'alice',
            authorize_owner=lambda owner, user: {'user_id': 'u1', 'role': 'user'} if owner == 'alice' else False,
        )
        self.service = SimpleNamespace(enabled=False, package=self.package, store=self.store)
        business.configure(self.service)
        self.addCleanup(business.configure, self.old_service)
        env = patch.dict(os.environ, {'SJFX_PROTECTION_TEST_STATIC_ONLY': '1'})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (('DB_PATH', self.base / 'analysis.db'),
                            ('DATA_DIR', self.base / 'data'),
                            ('OUTPUT_DIR', self.base / 'outputs'),
                            ('SCAN_ALLOWED_ROOTS', ())):
            target = patch.object(Config, name, value)
            target.start()
            self.addCleanup(target.stop)
        self.storage = Storage(Config.DB_PATH)
        self.storage.save_scan({'root': str(self.root), 'tree': {}, 'files': []},
                               scan_id='scan-A', owner_id='alice')
        self.repo = MailRepository(self.storage)
        self.repo.migrate()
        self.package.update_policy('admin', True, {
            'enabled': True, 'allowed_registration_modes': ['local'],
            'inspection_profile': {'required_engines': ['static']},
        }, 'isolated static-only queue regression')
        self.rid = self.register(self.root)
        self.ctx = self.package.require_operation('alice', root=str(self.root), operation='analyze')['context']
        self.service.enabled = True
        token = business.execution.set(self.ctx)
        self.addCleanup(business.execution.reset, token)
        self.mail = self.new_package(self.root)

    def register(self, root):
        registration = self.package.register('alice', 'u1', str(root), 'queue fixture', 'local')
        job = self.package.process_job(registration['job_id'])
        self.assertEqual(job['state'], 'completed', job.get('error'))
        return registration['registration_id']

    def new_package(self, root, owner='alice'):
        return self.repo.create_package(owner_id=owner, display_name='queue fixture',
            source_kind='directory', source_path=str(root), source_revision='fixture-v1', config={})[0]

    def queue(self, task='mail_extract'):
        job_id, created = self.storage.create_or_get_typed_job(
            self.mail['package_id'], task, options={'package_id': self.mail['package_id']}, owner_id='alice')
        self.assertTrue(created)
        return job_id

    def insert(self, identifier, owner='alice', task='mail_extract', result=None):
        # Adversarial queue insertion still uses the real guarded connection.
        # Options deliberately claim the valid package; they are never proof.
        with self.storage._connect() as db:
            db.execute('INSERT INTO analysis_jobs(id,scan_id,task_type,status,stage,progress,message,options,owner_id,result,priority,created_at) '
                       "VALUES(?,?,?,'queued','queued',1,'Fixture queue',?,?,?,0,CURRENT_TIMESTAMP)",
                ('invalid-job', identifier, task, json.dumps({'package_id': self.mail['package_id']}), owner, result))

    def raw_read(self, sql, parameters=()):
        with closing(sqlite3.connect(self.storage.db_path)) as db:
            return db.execute(sql, parameters).fetchall()

    def assert_insert_denied(self, *args, **kwargs):
        with self.assertRaises(ProtectionError) as caught:
            self.insert(*args, **kwargs)
        self.assertEqual(caught.exception.code, 'cache_scope_mismatch')
        self.assertEqual(self.raw_read("SELECT count(*) FROM analysis_jobs WHERE id='invalid-job'")[0][0], 0)

    def test_actual_import_derivatives_extract_and_embed_are_proven(self):
        with patch.object(Config, 'MAIL_ENABLE_VECTOR_INDEX', True):
            queued = _queue_derivatives(SimpleNamespace(storage=self.storage, repository=self.repo),
                                        self.mail, attachments=True)
        self.assertEqual([item['task_type'] for item in queued], ['mail_extract', 'mail_embed'])
        for item in queued:
            self.assertTrue(item['created'])
            job = self.storage.get_job(item['job_id'], 'alice')
            self.assertEqual(job['scan_id'], self.mail['package_id'])
            self.assertEqual(job['options']['protection_context']['registration_id'], self.rid)
            with self.storage._connect() as db:
                # Explicit result projection authenticates even the NULL row.
                self.assertEqual(db.execute('SELECT result FROM analysis_jobs WHERE id=?',
                                           (item['job_id'],)).fetchone()[0], None)
            payload = {'summary': 'Approved derived content', 'count': 1}
            self.storage.update_job(item['job_id'], status='completed', result=payload)
            self.assertEqual(self.storage.get_job(item['job_id'], 'alice')['result'], payload)

    def test_queue_owner_mismatch_and_empty_owner_are_denied(self):
        for owner in ('bob', ''):
            with self.subTest(owner=owner):
                self.assert_insert_denied(self.mail['package_id'], owner=owner)
        foreign = self.new_package(self.root, owner='bob')
        self.assert_insert_denied(foreign['package_id'])

    def test_missing_package_and_unregistered_root_cannot_use_forged_options(self):
        self.assert_insert_denied('mp_does_not_exist')
        other = self.new_package(self.other)
        self.assert_insert_denied(other['package_id'])
        with business.activate({}):
            with self.assertRaises(ProtectionError):
                self.storage.create_or_get_typed_job(other['package_id'], 'mail_extract', owner_id='alice')

    def test_unrelated_registered_root_context_cannot_publish_or_read_mail_result(self):
        job_id = self.queue()
        payload = {'summary': 'Only this mail root is authorized'}
        self.storage.update_job(job_id, result=payload)
        self.register(self.other)
        other_context = self.package.require_operation('alice', root=str(self.other), operation='analyze')['context']
        with business.activate(other_context):
            self.assertIsNone(self.storage.get_job(job_id, 'alice'))
            with self.assertRaises(ProtectionError):
                self.storage.update_job(job_id, result={'summary': 'Wrong root output'})
        self.assertEqual(self.storage.get_job(job_id, 'alice')['result'], payload)

    def test_registered_child_is_not_covered_by_parent_context(self):
        self.register(self.child)
        nested = self.new_package(self.child)
        self.assert_insert_denied(nested['package_id'])
        child_context = self.package.require_operation('alice', root=str(self.child), operation='analyze')['context']
        with business.activate(child_context):
            job_id, created = self.storage.create_or_get_typed_job(nested['package_id'], 'mail_extract', owner_id='alice')
            self.assertTrue(created)
            self.storage.update_job(job_id, result={'summary': 'Child context content'})
            self.assertEqual(self.storage.get_job(job_id, 'alice')['result']['summary'], 'Child context content')
        self.assertIsNone(self.storage.get_job(job_id, 'alice'))

    def test_unknown_task_and_nonmail_identifier_are_not_mail_scopes(self):
        self.assert_insert_denied(self.mail['package_id'], task='mail_unrecognized')
        self.assert_insert_denied(self.mail['package_id'], task='analyze_package')
        self.assert_insert_denied('scan-A', task='mail_extract')

    def test_existing_mail_result_is_denied_after_unknown_or_nonmail_task_rename(self):
        job_id = self.queue()
        payload = {'summary': 'Existing proved mail result'}
        self.storage.update_job(job_id, result=payload)
        for renamed in ('mail_unrecognized', 'analyze_package'):
            with self.subTest(task=renamed):
                with self.storage._connect() as db:
                    db.execute('UPDATE analysis_jobs SET task_type=? WHERE id=?', (renamed, job_id))
                self.assertIsNone(self.storage.get_job(job_id, 'alice'))
                with self.storage._connect() as db:
                    self.assertEqual(db.execute('SELECT result FROM analysis_jobs WHERE id=?', (job_id,)).fetchall(), [])
                with self.assertRaises(ProtectionError) as caught:
                    self.storage.update_job(job_id, result={'summary': 'Retyped output'})
                self.assertEqual(caught.exception.code, 'cache_scope_mismatch')
                with self.storage._connect() as db:
                    db.execute("UPDATE analysis_jobs SET task_type='mail_extract' WHERE id=?", (job_id,))
                self.assertEqual(self.storage.get_job(job_id, 'alice')['result'], payload)

    def test_rich_result_rejects_changed_source_and_missing_context(self):
        job_id = self.queue()
        payload = {'summary': 'Rich result bound to actual source versions'}
        self.storage.update_job(job_id, result=payload)
        with business.activate({}):
            self.assertIsNone(self.storage.get_job(job_id, 'alice'))
            with self.assertRaises(ProtectionError):
                self.storage.update_job(job_id, result={'summary': 'Unattributed output'})
        self.file.write_bytes(b'Changed unapproved mail source bytes')
        with self.assertRaises(ProtectionError):
            self.storage.get_job(job_id, 'alice')
        with self.assertRaises(ProtectionError):
            self.storage.update_job(job_id, result={'summary': 'Stale source output'})
        self.assertEqual(json.loads(self.raw_read('SELECT result FROM analysis_jobs WHERE id=?', (job_id,))[0][0]), payload)

    def test_rich_result_output_tamper_is_rejected(self):
        job_id = self.queue()
        self.storage.update_job(job_id, result={'summary': 'Proven output'})
        # Controlled external corruption of this disposable fixture DB.
        with closing(sqlite3.connect(self.storage.db_path)) as db:
            db.execute('UPDATE analysis_jobs SET result=? WHERE id=?', ('{"summary":"forged"}', job_id))
            db.commit()
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_job(job_id, 'alice')
        self.assertEqual(caught.exception.code, 'cache_output_mismatch')

    def test_unknown_historical_mail_result_is_not_blessed(self):
        self.service.enabled = False
        self.insert(self.mail['package_id'], result='{"summary":"historical unproven output"}')
        self.service.enabled = True
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_job('invalid-job', 'alice')
        self.assertEqual(caught.exception.code, 'cache_provenance_missing')

    def test_original_scan_queue_proof_survives_adapter_upgrade(self):
        # Produce a proof using the byte-for-byte frozen adapter, then consume
        # it using the candidate. This catches identity/hash migration breaks.
        frozen = Path(__file__).resolve().parent / 'fixtures/storage_cache_before_mail_scope.py'
        assert hashlib.sha256(frozen.read_bytes()).hexdigest() == '13d7ad8db8effd6c9d0c88423ce21fad0e3f04bce65b05f928919b1aca9a9802'
        spec = importlib.util.spec_from_file_location('sjfx_protection._frozen_storage_cache', frozen)
        previous = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(previous)
        payload = {'summary': 'Existing proved scan result', 'items': ['source evidence']}
        with patch.object(storage_cache, 'connection', previous.connection):
            job_id, created = self.storage.create_or_get_typed_job('scan-A', 'analyze_package', owner_id='alice')
            self.assertTrue(created)
            self.storage.update_job(job_id, result=payload)
        self.assertEqual(self.storage.get_job(job_id, 'alice')['result'], payload)
        self.assertEqual(storage_cache.SPECS['analysis_jobs']['identity'], previous.SPECS['analysis_jobs']['identity'])
        with self.assertRaises(ProtectionError):
            self.storage.create_or_get_typed_job('scan-A', 'analyze_package', owner_id='bob')


if __name__ == '__main__':
    unittest.main()
