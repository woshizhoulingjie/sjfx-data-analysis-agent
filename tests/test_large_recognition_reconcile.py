import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.large_package_isolated import LargePackageStore
from services.large_package_repair import r1_recognize_reconcile


class RecognitionReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        guard = patch.object(Config, 'SCAN_ALLOWED_ROOTS', ())
        guard.start()
        self.addCleanup(guard.stop)
        self.store = LargePackageStore(self.root / 'state.db')
        self.pid = self.store.create(self.root)['id']
        self.store.update_package(self.pid, status='completed', phase='report_ready')

    def seed(self, path, *, catalog='classified', recognition='needs_review',
             member=True, group=True, digest='current', catalog_queue='completed',
             queue='queued', group_id=None):
        self.store.upsert_file(self.pid, {'path': path, 'name': path})
        gid = group_id or 'group-' + path
        portrait = {'catalog_input_hash': 'current'}
        if catalog is not None:
            portrait['catalog_status'] = catalog
        local = {'sample': 'Real readable content for classification. ' * 4, 'portrait': portrait}
        with closing(self.store._connect()) as conn:
            conn.execute(
                "UPDATE files SET quick_status='completed',recognition_status=?,local_summary=? "
                "WHERE package_id=? AND path=?",
                (recognition, json.dumps(local), self.pid, path))
            if group:
                conn.execute(
                    'INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at) '
                    'VALUES(?,?,?,?,?,?)', (self.pid, gid, 'fixture', b'', path, time.time()))
            if member:
                conn.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                             (self.pid, path, gid, digest, b''))
            if catalog_queue is not None:
                conn.execute('INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,?,?,?)',
                             (self.pid, path, 'catalog', catalog_queue, time.time()))
            conn.execute(
                "INSERT INTO queue(package_id,path,stage,status,updated_at,leased_at,lease_token,error) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (self.pid, path, 'recognize', queue, time.time(), 1.0, 'prior-lease', 'prior-error'))

    def queue(self, path):
        with closing(self.store._connect()) as conn:
            return dict(conn.execute(
                "SELECT * FROM queue WHERE package_id=? AND path=? AND stage='recognize'",
                (self.pid, path)).fetchone())

    def test_dry_run_is_read_only_and_apply_is_idempotent(self):
        self.seed('classified.txt')
        before = self.queue('classified.txt')
        file_before = self.store.get_file(self.pid, 'classified.txt')
        self.assertEqual(r1_recognize_reconcile(self.store), {'rows': 1})
        self.assertEqual(before, self.queue('classified.txt'))
        self.assertEqual(r1_recognize_reconcile(self.store, apply=True), {'rows': 1})
        after = self.queue('classified.txt')
        self.assertEqual(after['status'], 'completed')
        for key in ('lease_token', 'leased_at', 'error'):
            self.assertIsNone(after[key])
        self.assertEqual(file_before, self.store.get_file(self.pid, 'classified.txt'))
        self.assertEqual(r1_recognize_reconcile(self.store, apply=True), {'rows': 0})
        self.assertEqual(self.store.workflow_snapshot(self.pid)['queue_pending']['recognize'], 0)

    def test_unresolved_and_stale_content_is_preserved(self):
        cases = [
            {'catalog': 'no_content'}, {'catalog': 'deferred'}, {'catalog': 'grouped'},
            {'catalog': 'pending'}, {'catalog': None}, {'digest': 'old'}, {'digest': ''},
            {'member': False}, {'group': False}, {'catalog_queue': 'queued'},
            {'catalog_queue': None}, {'group_id': 'km-other-fixture'},
            {'group_id': 'km-pending-fixture'}, {'recognition': 'needs_ocr', 'catalog': 'no_content'},
        ]
        for index, kwargs in enumerate(cases):
            self.seed('unresolved-%d.txt' % index, **kwargs)
        self.assertEqual(self.store.reconcile_recognition(), 0)
        for index in range(len(cases)):
            self.assertEqual(self.queue('unresolved-%d.txt' % index)['status'], 'queued')

    def test_terminal_recognition_remains_supported(self):
        for status in ('identified', 'restricted', 'unsupported'):
            self.seed(status, recognition=status, catalog='no_content', member=False, group=False)
        self.assertEqual(self.store.reconcile_recognition(), 3)

    def test_live_failed_and_cancelled_jobs_are_untouched(self):
        for status in ('running', 'failed', 'cancelled'):
            self.seed(status, queue=status)
        before = {s: self.queue(s) for s in ('running', 'failed', 'cancelled')}
        self.assertEqual(self.store.reconcile_recognition(), 0)
        self.assertEqual(before, {s: self.queue(s) for s in before})
        self.seed('retry', queue='retry')
        self.assertEqual(self.store.reconcile_recognition(), 1)

    def test_cancelled_package_is_left_to_cancel_repair(self):
        self.seed('cancelled-package.txt')
        self.store.update_package(self.pid, status='cancelled')
        self.assertEqual(self.store.reconcile_recognition(), 0)
        self.assertEqual(self.queue('cancelled-package.txt')['status'], 'queued')

    def test_request_does_not_reenqueue_resolved_files_when_queue_is_empty(self):
        self.seed('done.txt')
        self.assertEqual(self.store.request_recognition(self.pid), 0)
        self.assertEqual(self.queue('done.txt')['status'], 'completed')
        self.assertEqual(self.store.request_recognition(self.pid), 0)
        self.assertEqual(self.store.get(self.pid)['status'], 'completed')

    def test_request_preserves_real_work_and_requeues_only_unresolved_files(self):
        self.seed('done.txt')
        self.seed('needs-ocr.txt', recognition='needs_ocr', catalog='no_content', queue='failed')
        self.assertEqual(self.store.request_recognition(self.pid), 1)
        self.assertEqual(self.queue('done.txt')['status'], 'completed')
        self.assertEqual(self.queue('needs-ocr.txt')['status'], 'queued')
        self.assertEqual(self.store.get(self.pid)['phase'], 'recognize_completed')

    def test_enqueue_skips_current_classification_but_accepts_missing_status(self):
        self.seed('done.txt')
        self.seed('unknown.txt', catalog=None)
        with closing(self.store._connect()) as conn:
            conn.execute("DELETE FROM queue WHERE package_id=? AND stage='recognize'", (self.pid,))
        self.store.enqueue_recognition(self.pid, 'done.txt')
        self.store.enqueue_recognition(self.pid, 'unknown.txt')
        self.assertEqual(self.store.stage_counts(self.pid, 'recognize'), {'queued': 1})
        self.assertEqual(self.queue('unknown.txt')['status'], 'queued')

    def test_package_scoping(self):
        self.seed('first.txt')
        first = self.pid
        self.pid = self.store.create(self.root)['id']
        self.seed('second.txt')
        self.assertEqual(self.store.reconcile_recognition(first), 1)
        self.assertEqual(self.queue('second.txt')['status'], 'queued')

    def test_named_current_legacy_pending_id_is_not_a_placeholder(self):
        self.seed('named.txt', catalog='pending', group_id='km-pending-old')
        with closing(self.store._connect()) as c:
            revision = c.execute('SELECT content_revision FROM large_catalog_groups WHERE package_id=?', (self.pid,)).fetchone()[0]
            c.execute('INSERT INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (self.pid, 'km-pending-old', revision, 1, 'Current grounded name', '', '[]', 'fixture', 'd', 0, 0, time.time()))
        self.assertEqual(self.store.reconcile_recognition(), 1)


if __name__ == '__main__':
    unittest.main()
