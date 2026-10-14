"""Real Storage control writes with an owner-only HTTP metadata context."""
import json
import sqlite3
import unittest
from contextlib import closing, contextmanager

import test_storage_cache_guard as storage_fixture
from sjfx_protection import business, cache
from sjfx_protection.store import ProtectionError


PAUSE_NOTICE = '已安全暂停；已完成结果永久保留，未完成文件仍在待处理队列。'
INVENTORY_NOTICE = '目录导入完成，尚未读取正文，等待选择文件。'


class PauseControlMetadataTests(unittest.TestCase):
    setUp = storage_fixture.StorageCacheTests.setUp
    tearDown = storage_fixture.StorageCacheTests.tearDown
    raw = storage_fixture.StorageCacheTests.raw

    @contextmanager
    def metadata(self, owner='alice', projection=True):
        token = business.metadata_projection.set(projection)
        try:
            # The real control middleware supplies no registration or versions.
            with business.activate({'owner': owner, 'device': '', 'enforced': True}):
                yield
        finally:
            business.metadata_projection.reset(token)

    def inventory(self):
        return {'schema_version': 'analysis-progress/1.0', 'progress': 0,
                'status': 'awaiting_selection', 'stage': 'awaiting_selection',
                'message': INVENTORY_NOTICE}

    def progress_row(self):
        return self.raw('SELECT payload FROM analysis_progress WHERE scan_id=?', ('scan-A',))

    def proof_rows(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (cache.TABLE,)).fetchone():
                return []
            return db.execute('SELECT * FROM ' + cache.TABLE + ' ORDER BY resource_key').fetchall()

    def pause(self, scan_id='scan-A', message=PAUSE_NOTICE, stage='paused'):
        return self.storage.update_analysis_progress_status(scan_id, 'paused', message, stage)

    def assert_denied_unchanged(self, operation):
        before, proofs = self.progress_row(), self.proof_rows()
        with self.assertRaises(ProtectionError):
            operation()
        self.assertEqual(self.progress_row(), before)
        self.assertEqual(self.proof_rows(), proofs)

    def test_owned_pause_updates_five_key_card_without_versions_or_stamps(self):
        self.storage.save_analysis_progress('scan-A', self.inventory())
        before = self.proof_rows()
        with self.metadata():
            self.assertNotIn('registration_id', business.context())
            self.assertNotIn('versions', business.context())
            self.storage.pause_package_analysis_jobs('scan-A')
            result = self.pause()
            self.assertEqual(self.pause(), result)
        self.assertEqual(set(result), {'schema_version', 'progress', 'status', 'stage', 'message'})
        self.assertEqual(result['message'], PAUSE_NOTICE)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(json.loads(self.progress_row()[0][0]), result)
        self.assertEqual(self.proof_rows(), before)

    def test_owned_pause_can_create_control_card(self):
        with self.metadata():
            self.pause()
        self.assertEqual(json.loads(self.progress_row()[0][0])['message'], PAUSE_NOTICE)
        self.assertEqual(self.proof_rows(), [])

    def test_arbitrary_messages_and_stages_remain_rejected(self):
        self.storage.save_analysis_progress('scan-A', self.inventory())
        for message, stage in [('document excerpt', 'paused'),
                               (PAUSE_NOTICE + ' secret.txt', 'paused'),
                               (PAUSE_NOTICE, 'source-derived-stage')]:
            with self.subTest(message=message, stage=stage), self.metadata():
                self.assert_denied_unchanged(lambda: self.pause(message=message, stage=stage))

    def test_extra_source_content_fields_are_never_control_metadata(self):
        self.storage.save_analysis_progress('scan-A', self.inventory())
        for field in ('text', 'analysis_tree', 'summary', 'evidence'):
            payload = dict(self.inventory(), status='paused', stage='paused', message=PAUSE_NOTICE)
            payload[field] = 'source content'
            with self.subTest(field=field), self.metadata():
                self.assert_denied_unchanged(lambda: self.storage.save_analysis_progress('scan-A', payload))

    def test_foreign_owner_and_nonexistent_scan_cannot_gain_scope(self):
        self.storage.save_analysis_progress('scan-A', self.inventory())
        with self.metadata(owner='bob'):
            self.assert_denied_unchanged(self.pause)
        with self.metadata():
            self.assert_denied_unchanged(lambda: self.pause(scan_id='forged-scan'))
        self.assertEqual(self.raw('SELECT COUNT(*) FROM analysis_progress WHERE scan_id=?', ('forged-scan',)), [(0,)])

    def test_metadata_flag_must_be_literal_true(self):
        self.storage.save_analysis_progress('scan-A', self.inventory())
        for projection in (False, 1, 'true'):
            with self.subTest(projection=projection), self.metadata(projection=projection):
                self.assert_denied_unchanged(self.pause)

    def test_invalid_progress_types_and_schema_still_rejected(self):
        self.storage.save_analysis_progress('scan-A', self.inventory())
        for key, value in [('schema_version', 'unknown'), ('progress', True),
                           ('progress', 101), ('progress', -1), ('status', 'excerpt')]:
            payload = dict(self.inventory(), message=PAUSE_NOTICE, status='paused', stage='paused')
            payload[key] = value
            with self.subTest(key=key, value=value), self.metadata():
                self.assert_denied_unchanged(lambda: self.storage.save_analysis_progress('scan-A', payload))

    def test_approved_content_card_is_preserved_and_cannot_be_relabelled(self):
        payload = dict(self.inventory(), analysis_tree={'summary': 'approved source-derived content'})
        self.storage.save_analysis_progress('scan-A', payload)
        before = self.proof_rows()
        self.assertTrue(before)
        with self.metadata():
            self.assert_denied_unchanged(self.pause)
        self.assertEqual(self.storage.get_analysis_progress('scan-A')['analysis_tree'], payload['analysis_tree'])
        self.package.blocked.add('A')
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_analysis_progress('scan-A')
        self.assertEqual(caught.exception.code, 'package_revoked')
        self.assertEqual(self.proof_rows(), before)

    def test_historical_content_card_is_preserved_and_never_stamped(self):
        payload = dict(self.inventory(), summary='historical source-derived content')
        self.p.enabled = False
        self.storage.save_analysis_progress('scan-A', payload)
        self.p.enabled = True
        with self.metadata():
            self.assert_denied_unchanged(self.pause)
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_analysis_progress('scan-A')
        self.assertEqual(caught.exception.code, 'cache_provenance_missing')
        self.assertEqual(self.proof_rows(), [])

    def test_pause_notice_does_not_authorize_other_cached_content(self):
        self.p.enabled = False
        self.storage.save_summary('scan-A', 'old.txt', 'file', {'summary': 'historical text'})
        self.p.enabled = True
        with self.metadata():
            self.pause()
            with self.assertRaises(ProtectionError):
                self.storage.save_summary('scan-A', 'new.txt', 'file', {'summary': PAUSE_NOTICE})
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_summary('scan-A', 'old.txt', 'file')
        self.assertEqual(caught.exception.code, 'cache_provenance_missing')
        self.assertEqual(self.proof_rows(), [])

    def test_owned_typed_job_result_uses_metadata_scope_without_versions(self):
        self.p.enabled = False
        job_id = self.storage.create_job('scan-A', owner_id='alice')
        self.p.enabled = True
        typed = {'scan_id': 'scan-A', 'scan_available': True, '_await_selection': True}
        with self.metadata():
            self.storage.update_job(job_id, result=typed)
            before = self.raw('SELECT result FROM analysis_jobs WHERE id=?', (job_id,))
            for result in (dict(typed, summary='source content'), dict(typed, scan_id='forged-scan'),
                           dict(typed, scan_available='true')):
                with self.subTest(result=result), self.assertRaises(ProtectionError):
                    self.storage.update_job(job_id, result=result)
                self.assertEqual(self.raw('SELECT result FROM analysis_jobs WHERE id=?', (job_id,)), before)
        with self.metadata(owner='bob'), self.assertRaises(ProtectionError):
            self.storage.update_job(job_id, result=dict(typed, scan_available=False))
        self.assertEqual(self.proof_rows(), [])


if __name__ == '__main__':
    unittest.main()
