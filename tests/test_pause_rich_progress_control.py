"""Enabled-protection rich-card pause controls; synthetic SQLite, no workers."""
import json
import unittest
from unittest.mock import patch

import app as web
import test_pause_control_metadata as control
from sjfx_protection.store import ProtectionError


class RichProgressControlTests(unittest.TestCase):
    setUp = control.PauseControlMetadataTests.setUp
    tearDown = control.PauseControlMetadataTests.tearDown
    raw = control.PauseControlMetadataTests.raw
    metadata = control.PauseControlMetadataTests.metadata
    inventory = control.PauseControlMetadataTests.inventory
    progress_row = control.PauseControlMetadataTests.progress_row
    proof_rows = control.PauseControlMetadataTests.proof_rows
    pause = control.PauseControlMetadataTests.pause
    assert_denied_unchanged = control.PauseControlMetadataTests.assert_denied_unchanged

    def rich(self, historical=False):
        payload = dict(self.inventory(), progress=37,
                       analysis_tree={'summary': 'synthetic source-derived result'},
                       evidence=[{'quote': 'synthetic protected excerpt'}])
        self.p.enabled = not historical
        self.storage.save_analysis_progress('scan-A', payload)
        self.p.enabled = True
        return payload

    def durable_pause(self):
        self.storage.pause_package_analysis_jobs('scan-A')
        return self.pause()

    def test_approved_rich_pause_preserves_exact_payload_and_provenance(self):
        payload = self.rich()
        before, proofs = self.progress_row(), self.proof_rows()
        self.assertTrue(proofs)
        with self.metadata():
            result = self.durable_pause()
            self.assertEqual(self.durable_pause(), result)
        self.assertEqual(set(result), {'schema_version', 'progress', 'status', 'stage', 'message'})
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['progress'], 37)
        self.assertEqual(self.progress_row(), before)
        self.assertEqual(self.proof_rows(), proofs)
        self.assertEqual(self.storage.get_package_processing_control('scan-A')['state'], 'paused')
        self.assertEqual(self.storage.get_analysis_progress('scan-A')['analysis_tree'], payload['analysis_tree'])
        self.package.blocked.add('A')
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_analysis_progress('scan-A')
        self.assertEqual(caught.exception.code, 'package_revoked')

    def test_historical_rich_pause_never_creates_provenance(self):
        self.rich(historical=True)
        before = self.progress_row()
        with self.metadata():
            self.assertEqual(self.durable_pause()['status'], 'paused')
        self.assertEqual(self.progress_row(), before)
        self.assertEqual(self.proof_rows(), [])
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_analysis_progress('scan-A')
        self.assertEqual(caught.exception.code, 'cache_provenance_missing')

    def test_tampered_rich_pause_preserves_mismatch(self):
        payload = self.rich()
        payload['analysis_tree']['summary'] = 'synthetic tampering'
        self.raw('UPDATE analysis_progress SET payload=? WHERE scan_id=?',
                 (json.dumps(payload, ensure_ascii=False), 'scan-A'))
        before, proofs = self.progress_row(), self.proof_rows()
        with self.metadata():
            self.durable_pause()
        self.assertEqual(self.progress_row(), before)
        self.assertEqual(self.proof_rows(), proofs)
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_analysis_progress('scan-A')
        self.assertEqual(caught.exception.code, 'cache_output_mismatch')

    def test_rich_preservation_requires_owner_and_exact_notice(self):
        self.rich()
        with self.metadata():
            self.storage.pause_package_analysis_jobs('scan-A')
            for message, stage in [('source excerpt', 'paused'),
                                   (control.PAUSE_NOTICE, 'failed')]:
                with self.subTest(message=message, stage=stage):
                    self.assert_denied_unchanged(lambda: self.pause(message=message, stage=stage))
        with self.metadata(owner='bob'):
            self.assert_denied_unchanged(self.pause)

    def test_producer_can_still_update_rich_progress_with_source_receipts(self):
        payload = self.rich()
        result = self.pause()
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['analysis_tree'], payload['analysis_tree'])
        self.assertEqual(self.storage.get_analysis_progress('scan-A')['status'], 'paused')

    def call(self, handler, identifier, owner='alice'):
        identity = {'owner_id': owner, 'role': 'user'}
        with patch.object(web, 'storage', self.storage), \
                patch.object(web._multiuser_runtime, 'get_current_user', return_value=identity), \
                patch.object(web._file_protection, 'guard_root', return_value=None), \
                self.metadata(owner=owner), \
                web.app.test_request_context('/api/test', method='POST', json={}):
            response = handler(identifier)
        if isinstance(response, tuple):
            response, status = response
        else:
            status = response.status_code
        return status, response.get_json()

    def test_actual_pause_and_cancel_handlers_preserve_rich_progress(self):
        self.rich()
        before, proofs = self.progress_row(), self.proof_rows()
        self.p.enabled = False
        queued = self.storage.create_job('scan-A', task_type='analyze_package', owner_id='alice')
        running = self.storage.create_job('scan-A', task_type='analyze_package', owner_id='alice')
        self.storage.update_job(running, status='running')
        self.p.enabled = True
        status, result = self.call(web.pause_package_processing, 'scan-A')
        self.assertEqual(status, 200)
        self.assertEqual(result['processing']['state'], 'paused')
        self.assertEqual(result['queued_cancelled'], 1)
        self.assertEqual(result['running_stopping'], 1)
        self.assertEqual(self.storage.get_job(queued)['status'], 'cancelled')
        self.assertEqual(self.storage.get_job(running)['status'], 'cancelling')
        self.assertTrue(self.storage.is_job_cancel_requested(running))
        status, result = self.call(web.cancel_job, running)
        self.assertEqual(status, 200)
        self.assertTrue(result['cancel_requested'])
        self.assertEqual(self.progress_row(), before)
        self.assertEqual(self.proof_rows(), proofs)

    def test_actual_foreign_pause_and_cancel_handlers_cannot_change_controls(self):
        self.rich()
        self.p.enabled = False
        queued = self.storage.create_job('scan-A', task_type='analyze_package', owner_id='alice')
        self.p.enabled = True
        before, proofs = self.progress_row(), self.proof_rows()
        for handler, identifier in [(web.pause_package_processing, 'scan-A'), (web.cancel_job, queued)]:
            with self.subTest(handler=handler.__name__):
                status, _ = self.call(handler, identifier, owner='bob')
                self.assertEqual(status, 404)
        self.assertEqual(self.progress_row(), before)
        self.assertEqual(self.proof_rows(), proofs)
        self.assertFalse(self.storage.get_package_processing_control('scan-A')['pause_requested'])
        self.assertEqual(self.storage.get_job(queued)['status'], 'queued')
