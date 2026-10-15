"""Deep model contention must never be persisted as a completed fallback."""
from contextlib import closing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from config import Config
from large_package_worker import LargePackageWorker
from services.large_package_isolated import LargePackageStore
from services.model_contract import LocalModelError
from services import model_dispatch as dispatch


REPLY = {'content': 'Grounded fixture summary.', 'model': 'fixture', 'finish_reason': 'stop'}


class DeepModelBusyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, value in [('SCAN_ALLOWED_ROOTS', ()), ('LP_DEEP_CONCURRENCY', 1),
                              ('DATA_DIR', self.root), ('DB_PATH', self.root / 'agent.db')]:
            setting = patch.object(Config, target, value)
            setting.start()
            self.addCleanup(setting.stop)
        settings = patch.dict(os.environ, {'LARGE_PACKAGE_MODEL_SUMMARY': '1',
                                          'SJFX_SHARED_GENERATION_GATE': '1'})
        settings.start()
        self.addCleanup(settings.stop)
        backoff = patch('large_package_worker._DEEP_MODEL_BUSY_BACKOFF', (0.01, 0.02), create=True)
        backoff.start()
        self.addCleanup(backoff.stop)
        self.store = LargePackageStore(self.root / 'state.db')
        self.pid = self.store.create(self.root)['id']
        self.path = 'source.txt'
        source = self.root / self.path
        source.write_text('Grounded fixture evidence. ' * 4, encoding='utf-8')
        stat = source.stat()
        self.store.upsert_file(self.pid, {'path': self.path, 'name': self.path,
                                         'size': stat.st_size, 'modified_at_ns': stat.st_mtime_ns})
        with closing(self.store._connect()) as conn:
            conn.execute("UPDATE queue SET status='completed' WHERE package_id=? AND stage='quick'", (self.pid,))
            conn.execute("INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,'deep','queued',?)",
                         (self.pid, self.path, time.time()))
        self.token = self.store.claim('deep', 1, self.pid)[0][2]
        self.worker = LargePackageWorker(store=self.store)
        self.addCleanup(self.worker.preview_runner.close)
        self.addCleanup(dispatch._drop_conn)
        self.worker._parser = Mock()
        self.worker._parser.parse.return_value = {'text': 'Grounded fixture evidence. ' * 4,
                                                 'source': {}, 'evidence': []}
        self.client = Mock()
        factory = patch('services.model_backend.create_model_client', return_value=self.client)
        factory.start()
        self.addCleanup(factory.stop)
        for name in ('_check_resources', '_catalog_incremental'):
            item = patch.object(self.worker, name)
            item.start()
            self.addCleanup(item.stop)

    def run_deep(self):
        result = self.worker._deep_worker(self.pid, str(self.root), self.path, self.token)
        with closing(self.store._connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM lp_catalog_mutations').fetchone()[0], 0)
        self.assertFalse(self.store.holds_lease(self.pid, self.path, 'deep', self.token))
        return result

    def test_real_busy_admission_retries_then_commits_model_summary(self):
        endpoint = 'fixture://deep-busy'
        with patch.object(dispatch, '_concurrency_limit', return_value=1):
            occupied = dispatch.generation_slot(endpoint)
            occupied.__enter__()
            released = False
            def chat(*args, **kwargs):
                nonlocal released
                self.assertTrue(self.store.holds_lease(self.pid, self.path, 'deep', self.token))
                try:
                    with dispatch.generation_slot(endpoint, timeout=0.01):
                        return dict(REPLY)
                except LocalModelError as exc:
                    self.assertEqual((exc.code, exc.retryable), ('busy', True))
                    with closing(dispatch._connect()) as conn:
                        self.assertEqual(conn.execute('SELECT COUNT(*) FROM admission').fetchone()[0], 1)
                    occupied.__exit__(None, None, None)
                    released = True
                    raise
            self.client.chat.side_effect = chat
            try:
                result = self.run_deep()
            finally:
                if not released:
                    occupied.__exit__(None, None, None)
        self.assertTrue(result[1], result)
        saved = self.store.get_file(self.pid, self.path)
        self.assertEqual((saved['deep_status'], saved['deep_summary']['summary_type']), ('completed', 'model'))
        self.assertEqual(saved['summary_quality'], 'complete')
        self.assertEqual(self.client.chat.call_count, 2)
        with closing(dispatch._connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM admission').fetchone()[0], 0)

    def test_busy_exhaustion_is_failed_and_can_be_retried_through_existing_action(self):
        self.client.chat.side_effect = LocalModelError('fixture busy', code='busy', retryable=True)
        result = self.run_deep()
        self.assertFalse(result[1], result)
        self.assertEqual(self.client.chat.call_count, 3)
        saved = self.store.get_file(self.pid, self.path)
        self.assertEqual(saved['deep_status'], 'failed')
        self.assertEqual(saved['deep_summary'], {})
        self.assertEqual(self.store.stage_counts(self.pid, 'deep'), {'failed': 1})
        self.store.update_package(self.pid, status='completed', phase='report_ready')
        self.assertTrue(self.store.workflow_snapshot(self.pid)['allowed_actions']['retry_deep'])
        self.assertEqual(self.store.retry_failed_stage(self.pid, 'deep'), [self.path])
        self.assertEqual(self.store.get(self.pid)['phase'], 'deep_parse')
        self.token = self.store.claim('deep', 1, self.pid)[0][2]
        self.client.chat.side_effect = None
        self.client.chat.return_value = dict(REPLY)
        recovered = self.run_deep()
        self.assertTrue(recovered[1], recovered)
        self.assertEqual(self.store.get_file(self.pid, self.path)['deep_summary']['summary_type'], 'model')

    def test_nonretryable_model_error_keeps_existing_degraded_contract(self):
        self.client.chat.side_effect = LocalModelError('fixture unavailable', code='busy', retryable=False)
        result = self.run_deep()
        self.assertTrue(result[1], result)
        self.assertEqual(self.client.chat.call_count, 1)
        saved = self.store.get_file(self.pid, self.path)
        self.assertEqual((saved['deep_summary']['summary_type'], saved['summary_quality']), ('local_deep', 'degraded'))

    def test_cancel_during_busy_wait_never_commits_a_fallback_or_calls_again(self):
        def cancel(*args, **kwargs):
            self.store.cancel_package(self.pid)
            raise LocalModelError('fixture busy', code='busy', retryable=True)
        self.client.chat.side_effect = cancel
        self.assertFalse(self.run_deep()[1])
        self.assertEqual(self.client.chat.call_count, 1)
        self.assertEqual(self.store.stage_counts(self.pid, 'deep'), {'cancelled': 1})
        self.assertEqual(self.store.get_file(self.pid, self.path)['deep_summary'], {})

    def test_dispatch_cancellation_requeues_without_a_failed_or_completed_summary(self):
        self.client.chat.side_effect = LocalModelError('fixture cancelled', code='cancelled')
        self.assertFalse(self.run_deep()[1])
        self.assertEqual(self.client.chat.call_count, 1)
        self.assertEqual(self.store.stage_counts(self.pid, 'deep'), {'queued': 1})
        self.assertEqual(self.store.get_file(self.pid, self.path)['deep_summary'], {})

    def test_lost_lease_during_busy_wait_does_not_retry_or_edit_new_owner(self):
        new_token = []
        def replace_lease(*args, **kwargs):
            self.store.requeue(self.pid, self.path, 'deep', token=self.token)
            new_token.append(self.store.claim('deep', 1, self.pid)[0][2])
            raise LocalModelError('fixture busy', code='busy', retryable=True)
        self.client.chat.side_effect = replace_lease
        self.assertFalse(self.run_deep()[1])
        self.assertEqual(self.client.chat.call_count, 1)
        self.assertTrue(self.store.holds_lease(self.pid, self.path, 'deep', new_token[0]))
        self.assertEqual(self.store.get_file(self.pid, self.path)['deep_summary'], {})


if __name__ == '__main__':
    unittest.main()
