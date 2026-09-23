import hashlib
import json
import os
import struct
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from config import Config
from large_package_worker import LargePackageWorker
from services.large_package_isolated import LargePackageStore
from services import large_package_catalog_naming as naming
from services.large_package_catalog_view import enhance_catalog


class StateCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for key, value in [('SCAN_ALLOWED_ROOTS', ()), ('LP_DEEP_CONCURRENCY', 1),
                           ('LP_CATALOG_MODEL_NAMING', False), ('LP_OVERVIEW_EAGER', False)]:
            p = patch.object(Config, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.store = LargePackageStore(self.root / 'state.db')
        self.pid = self.store.create(self.root)['id']
        self.worker = LargePackageWorker(store=self.store)
        self.addCleanup(self.worker.preview_runner.close)

    def seed(self, path='a.txt', stage='deep', status='failed'):
        source = self.root / path
        source.write_text('Source material for reliable analysis. ' * 8)
        stat = source.stat()
        self.store.upsert_file(self.pid, {'path': path, 'name': path,
                                         'size': stat.st_size, 'modified_at_ns': stat.st_mtime_ns})
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(package_id,path,stage) DO UPDATE SET status=excluded.status",
                      (self.pid, path, stage, status, time.time()))
            c.execute('UPDATE files SET ' + stage + '_status=? WHERE package_id=? AND path=?',
                      (status, self.pid, path))
            if stage == 'deep':
                c.execute("UPDATE queue SET status='completed' WHERE package_id=? AND path=? AND stage='quick'",
                          (self.pid, path))
        return self.store.get_file(self.pid, path)

    def group(self, gid, paths):
        for path in paths:
            self.seed(path, stage='quick', status='completed')
        with closing(self.store._connect()) as c:
            c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at) '
                      'VALUES(?,?,?,?,?,?)', (self.pid, gid, 'fixture', struct.pack('ff', 1, 0), paths[0], time.time()))
            for path in paths:
                c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                          (self.pid, path, gid, 'hash', struct.pack('ff', 1, 0)))
            rev = c.execute('SELECT content_revision FROM large_catalog_groups WHERE package_id=? AND id=?',
                            (self.pid, gid)).fetchone()[0]
            c.execute('INSERT INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (self.pid, gid, rev, 1, '证据分析' + gid, '分析资料中的证据', '[]', 'fixture', 'digest', 0, 0, time.time()))
        return rev

    def branch(self, gid, rev):
        with closing(self.store._connect()) as c:
            c.execute('INSERT INTO lp_catalog_branches VALUES(?,?,?,?,?,?,?)',
                      (self.pid, 'topic:fixture', '证据研究', json.dumps([gid]), json.dumps({gid: rev}), 'fixture', time.time()))

    def test_retry_recovers_schedulable_phase(self):
        self.seed()
        self.store.update_package(self.pid, status='completed', phase='report_ready')
        self.assertEqual(self.store.retry_failed_stage(self.pid, 'deep'), ['a.txt'])
        self.assertEqual(self.store.get(self.pid)['phase'], 'deep_parse')
        self.assertIn(self.pid, [p['id'] for p in self.worker._packages()])

    def test_combined_retry_runs_fast_before_deep(self):
        self.seed('deep.txt')
        self.seed('quick.txt', stage='quick')
        self.store.update_package(self.pid, status='completed', phase='report_ready')
        result = self.store.retry_failed_stages(self.pid, ['quick', 'deep'])
        self.assertEqual(result, {'quick': ['quick.txt'], 'deep': ['deep.txt']})
        self.assertEqual(self.store.get(self.pid)['phase'], 'quick_parse')
        self.assertEqual(self.store.pending_total(self.pid, 'deep'), 1)
        self.store.finish_queue(self.pid, 'quick.txt', 'quick')
        self.store.update_package(self.pid, status='running', phase='recognize')
        with patch.object(self.worker, '_catalog_incremental'), patch.object(self.worker, '_ensure_km_finalized'):
            self.worker._recognize_one(self.store.get(self.pid))
        self.assertEqual(self.store.get(self.pid)['phase'], 'deep_parse')

    def test_retry_reopens_affected_selection_only(self):
        self.seed()
        self.store.update_package(self.pid, status='completed', phase='report_ready')
        with closing(self.store._connect()) as c:
            for ident, path in [('affected', 'a.txt'), ('unrelated', 'other.txt')]:
                c.execute("INSERT INTO selections(id,package_id,paths,rule,status,created_at,updated_at) "
                          "VALUES(?,?,?,'{}','partial',?,?)", (ident, self.pid, json.dumps([path]), time.time(), time.time()))
        self.store.retry_failed_stage(self.pid, 'deep')
        with closing(self.store._connect()) as c:
            states = dict(c.execute('SELECT id,status FROM selections'))
        self.assertEqual(states, {'affected': 'running', 'unrelated': 'partial'})

    def test_cancelled_and_paused_cannot_be_claimed_or_retried(self):
        self.seed(status='queued')
        for status in ('paused', 'cancelled'):
            self.store.update_package(self.pid, status=status)
            self.assertEqual(self.store.claim('deep', 1, self.pid), [])
            with self.assertRaises(ValueError):
                self.store.retry_failed_stage(self.pid, 'deep')

    def test_late_completion_cannot_overwrite_cancel(self):
        self.store.cancel_package(self.pid)
        self.store.update_package(self.pid, status='completed', phase='report_ready')
        self.assertEqual(self.store.get(self.pid)['status'], 'cancelled')

    def test_pause_available_for_inventory_and_deep(self):
        self.assertTrue(self.store.workflow_snapshot(self.pid)['allowed_actions']['pause'])
        self.seed(status='queued')
        self.store.update_package(self.pid, status='queued', phase='deep_parse')
        self.assertTrue(self.store.workflow_snapshot(self.pid)['allowed_actions']['pause'])

    def test_deep_wait_returns_to_recovery_loop(self):
        self.seed(status='queued')
        self.store.update_package(self.pid, status='queued', phase='deep_parse')
        self.store.claim('deep', 1, self.pid)
        started = time.monotonic()
        self.assertFalse(self.worker._deep_one(self.store.get(self.pid)))
        self.assertLess(time.monotonic() - started, 1)
        with closing(self.store._connect()) as c:
            c.execute("UPDATE queue SET updated_at=? WHERE package_id=? AND stage='deep'", (time.time() - 301, self.pid))
        with patch.object(self.worker, '_deep_parse', return_value={'summary_type': 'model', 'summary': 'Grounded evidence summary. ' * 4}), patch.object(self.worker, '_catalog_incremental'):
            self.worker.run_once()
        self.assertEqual(self.store.get(self.pid)['status'], 'completed')

    def test_heartbeat_keeps_only_current_lease_alive(self):
        self.seed(status='queued')
        token = self.store.claim('deep', 1, self.pid)[0][2]
        with closing(self.store._connect()) as c:
            c.execute("UPDATE queue SET updated_at=? WHERE package_id=? AND stage='deep'", (time.time() - 301, self.pid))
        self.assertFalse(self.store.renew_lease(self.pid, 'a.txt', 'deep', 'wrong'))
        self.assertTrue(self.store.renew_lease(self.pid, 'a.txt', 'deep', token))
        self.store.stale_running_to_retry()
        self.assertTrue(self.store.holds_lease(self.pid, 'a.txt', 'deep', token))

    def test_select_uses_one_write_transaction_and_records_version(self):
        self.seed()
        self.store.update_package(self.pid, status='completed', phase='report_ready')
        original = self.store._connect
        def short():
            c = original()
            c.execute('PRAGMA busy_timeout=100')
            return c
        with patch.object(self.store, '_connect', side_effect=short), patch.object(naming, 'validate_catalog', return_value={'blocking': False}):
            selection = self.store.save_selection(self.pid, ['a.txt'])
        with closing(self.store._connect()) as c:
            version = c.execute('SELECT catalog_id FROM selections WHERE id=?', (selection['selection_id'],)).fetchone()[0]
        self.assertTrue(version.startswith('cat-'))

    def test_catalog_version_changes_on_rename_without_count_change(self):
        self.group('g1', ['one.txt'])
        with patch.object(naming, 'validate_catalog', return_value={'blocking': False}):
            first = naming.ensure_catalog_version(self.store, self.pid)
            with closing(self.store._connect()) as c:
                c.execute("UPDATE lp_catalog_labels SET name='新证据主题' WHERE package_id=?", (self.pid,))
            second = naming.ensure_catalog_version(self.store, self.pid)
            self.assertNotEqual(first, second)
            self.assertEqual(second, naming.ensure_catalog_version(self.store, self.pid))

    def test_changed_source_is_not_silently_accepted(self):
        row = self.seed()
        (self.root / 'a.txt').write_text('Different evidence')
        result = self.worker._deep_parse(self.root, row)
        self.assertEqual(result['summary_type'], 'local_deep_failed')
        self.assertTrue(result['source_changed'])

    def test_change_during_model_call_is_rejected(self):
        row = self.seed()
        self.worker._parser = Mock()
        self.worker._parser.parse.return_value = {'text': 'Source evidence', 'source': {}, 'evidence': []}
        def model(*args):
            (self.root / 'a.txt').write_text('Changed while model was working')
            return {'summary_type': 'model', 'summary': 'old evidence'}
        with patch.dict(os.environ, {'LARGE_PACKAGE_MODEL_SUMMARY': '1'}), patch.object(self.worker, '_model_summary', side_effect=model):
            result = self.worker._deep_parse(self.root, row)
        self.assertTrue(result['source_changed'])

    def test_symlink_outside_package_is_rejected(self):
        row = self.seed()
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / 'outside.txt'
            outside.write_text('Outside package')
            (self.root / 'a.txt').unlink()
            try:
                (self.root / 'a.txt').symlink_to(outside)
            except OSError:
                self.skipTest('symlink unavailable')
            result = self.worker._deep_parse(self.root, row)
        self.assertTrue(result['source_changed'])

    def test_duplicate_prompt_does_not_disclose_other_owner(self):
        one = self.store.create(self.root, owner_id='owner-a')
        two = self.store.create(self.root, owner_id='owner-b')
        self.assertNotIn(one['id'], [p['id'] for p in two.get('existing_packages', [])])
        three = self.store.create(self.root, owner_id='owner-a')
        self.assertIn(one['id'], [p['id'] for p in three['existing_packages']])

    def test_topic_click_returns_all_children_including_single_leaf(self):
        rev = self.group('g1', ['one.txt', 'two.txt'])
        self.branch('g1', rev)
        self.assertEqual(self.store.file_page(self.pid, node_id='topic:fixture')['total'], 2)
        smart = self.store.catalog(self.pid)['smart']
        self.assertEqual(smart['theme_total'], 1)
        self.assertEqual(smart['hierarchy'], 'theme_leaf_files')
        self.assertEqual(smart['branches'][0]['files'], 2)

    def test_stale_revision_does_not_hide_real_members(self):
        rev = self.group('g1', ['one.txt'])
        self.branch('g1', rev - 1)
        smart = self.store.catalog(self.pid)['smart']
        self.assertEqual(smart['branches'][0]['status'], 'stale')
        self.assertEqual(self.store.file_page(self.pid, node_id='topic:fixture')['total'], 1)

    def test_orphan_leaf_is_visible_under_explicit_pending_theme(self):
        self.group('g1', ['one.txt'])
        branches = self.store.catalog(self.pid)['smart']['branches']
        self.assertEqual(branches[0]['node_id'], 'branch:unassigned')
        self.assertEqual(branches[0]['status'], 'pending')
        self.assertEqual(self.store.file_page(self.pid, node_id='branch:unassigned')['total'], 1)

    def test_counts_use_real_members_not_drifted_cache(self):
        self.group('g1', ['one.txt'])
        with closing(self.store._connect()) as c:
            c.execute('UPDATE large_catalog_groups SET member_count=999 WHERE package_id=?', (self.pid,))
        smart = enhance_catalog(self.store, self.pid, {'smart': {}})['smart']
        self.assertEqual(smart['topics'][0]['files'], 1)

    def test_small_package_generates_theme_above_leaf(self):
        self.group('g1', ['one.txt'])
        model = Mock()
        model.supports_json_response_format = True
        model.chat.return_value = {'content': json.dumps({'name': '资料证据研究'}), 'usage': {'total_tokens': 40}}
        with patch.object(Config, 'LP_TOPIC_MERGE', True), patch.object(naming, 'reserve', return_value='call'), patch.object(naming, 'finish_call'):
            result = naming.merge_topics(self.store, self.pid, {'id': 'round', 'deadline': time.time() + 60}, model_client=model)
        self.assertTrue(result)
        smart = self.store.catalog(self.pid)['smart']
        self.assertEqual(smart['theme_total'], 1)
        self.assertEqual(smart['branches'][0]['children'], ['g1'])

    def test_directory_and_file_states_are_derived_without_mutating_portrait(self):
        self.group('g1', ['one.txt'])
        portrait = json.dumps({'portrait': {'catalog_status': 'pending', 'catalog_input_hash': 'hash'}})
        with closing(self.store._connect()) as c:
            c.execute('UPDATE files SET local_summary=? WHERE package_id=?', (portrait, self.pid))
        file = self.store.get_file(self.pid, 'one.txt')
        self.assertEqual(file['catalog_state'], 'classified')
        self.assertEqual(file['local_summary']['portrait']['catalog_status'], 'pending')
        smart = self.store.catalog(self.pid)['smart']
        self.assertEqual(smart['catalog_states'], {'classified': 1})
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT local_summary FROM files WHERE package_id=?', (self.pid,)).fetchone()[0], portrait)

    def test_pause_returns_uncommitted_preview_leases(self):
        from services.large_package_preview_runner import PreviewCancelled
        self.seed('one.txt', stage='quick', status='queued')
        self.seed('two.txt', stage='quick', status='queued')
        self.store.update_package(self.pid, status='running', phase='quick_parse')
        def pause(*args, **kwargs):
            self.store.update_package(self.pid, status='paused')
            raise PreviewCancelled()
        runner = Mock()
        runner.run.side_effect = pause
        self.worker.preview_runner = runner
        with patch.object(Config, 'LP_PREVIEW_CONCURRENCY', 2), patch(
                'services.large_package_preview_runner.PreviewRunner', return_value=runner):
            self.worker._quick_one(self.store.get(self.pid))
        self.assertEqual(self.store.stage_counts(self.pid, 'quick'), {'queued': 2})

    def test_preview_complete_with_catalog_backlog_has_visible_separate_phase(self):
        self.seed('one.txt', stage='quick', status='completed')
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,'catalog','queued',?)",
                      (self.pid, 'one.txt', time.time()))
        self.store.update_package(self.pid, status='running', phase='quick_parse')
        with patch.object(self.worker, '_catalog_incremental'):
            self.worker._quick_one(self.store.get(self.pid))
        package = self.store.get(self.pid)
        self.assertEqual(package['phase'], 'catalog_build')
        self.assertIn('1', package['message'])
        snapshot = self.store.workflow_snapshot(self.pid)
        self.assertEqual(snapshot['pending_blocking'], 1)
        self.assertTrue(snapshot['allowed_actions']['pause'])
        self.store.finish_queue(self.pid, 'one.txt', 'catalog')
        with patch.object(self.worker, '_catalog_incremental'):
            self.worker.run_once()
        self.assertEqual(self.store.get(self.pid)['phase'], 'recognize')


if __name__ == '__main__':
    unittest.main()
