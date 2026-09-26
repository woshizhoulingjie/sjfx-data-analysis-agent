"""A catalog handoff may reuse only the same owner, mode and source version."""
import json
import os
import sqlite3
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_large_catalog_import as fixtures
from services import large_package_catalog_import as bridge
from services.storage import Storage

PACKAGE, GROUP_A = fixtures.PACKAGE, fixtures.GROUP_A


class ImportIdentityTests(unittest.TestCase):
    setUp = fixtures.LargeCatalogImportTests.setUp
    tearDown = fixtures.LargeCatalogImportTests.tearDown
    _export = fixtures.LargeCatalogImportTests._export
    _count = fixtures.LargeCatalogImportTests._count

    def create(self, key='first', mode='accurate', owner='o1', selected=None, store=None):
        return (store or self.ordinary).create_scan_from_large_selection(
            {'id': PACKAGE, 'root_path': '/tmp/lpimport-src', 'status': 'waiting_for_selection'},
            selected if selected is not None else self._export(node_ids=[GROUP_A]),
            name='版本一致性验收', parse_mode=mode, owner_id=owner,
            request_key=key, large_store=self.large)

    def change(self, sql, args=()):
        with closing(self.large._connect()) as conn:
            conn.execute(sql, args)
            conn.commit()

    def assert_conflict(self, **kwargs):
        with self.assertRaises(bridge.SelectionError) as exc:
            self.create(**kwargs)
        self.assertEqual(exc.exception.status, 409)

    def test_same_key_different_mode_is_conflict(self):
        self.create(mode='fast')
        self.assert_conflict(mode='accurate')
        self.assertEqual(self._count('SELECT COUNT(*) FROM analysis_jobs'), 1)

    def test_new_key_different_mode_creates_requested_pipeline(self):
        fast = self.create(mode='fast')
        accurate = self.create(key='second', mode='accurate')
        self.assertNotEqual(fast['scan_id'], accurate['scan_id'])
        with self.ordinary._connect() as conn:
            row = conn.execute('SELECT options FROM analysis_jobs WHERE id=?', (accurate['job_id'],)).fetchone()
        self.assertEqual(json.loads(row[0])['parse_mode'], 'accurate')

    def test_auto_matches_effective_accurate_mode_in_scan_and_job(self):
        automatic = self.create(mode='auto')
        accurate = self.create(key='second', mode='accurate')
        self.assertEqual(automatic['scan_id'], accurate['scan_id'])
        with self.ordinary._connect() as conn:
            row = conn.execute('SELECT payload FROM scans WHERE id=?', (automatic['scan_id'],)).fetchone()
        self.assertEqual(json.loads(row[0])['parse_mode'], 'accurate')

    def test_distinct_owner_never_reuses_another_users_scan(self):
        first = self.create(owner='o1')
        second = self.create(key='second', owner='o2')
        self.assertNotEqual(first['scan_id'], second['scan_id'])

    def test_same_key_different_owner_is_conflict(self):
        self.create(owner='o1')
        self.assert_conflict(owner='o2')

    def test_new_content_digest_never_reuses_old_task(self):
        first = self.create()
        self.change('UPDATE files SET sha256=? WHERE package_id=?', ('new-content-digest', PACKAGE))
        second = self.create(key='second')
        self.assertNotEqual(first['scan_id'], second['scan_id'])

    def test_same_key_new_source_version_is_conflict(self):
        self.create()
        self.change('UPDATE files SET size=size+1 WHERE package_id=?', (PACKAGE,))
        self.assert_conflict()

    def test_changed_metadata_with_stale_digest_never_reuses_old_task(self):
        self.change('UPDATE files SET sha256=? WHERE package_id=?', ('old-digest', PACKAGE))
        first = self.create()
        self.change('UPDATE files SET modified_at_ns=modified_at_ns+1 WHERE package_id=?', (PACKAGE,))
        second = self.create(key='second')
        self.assertNotEqual(first['scan_id'], second['scan_id'])

    def test_source_failure_does_not_create_unverified_job(self):
        selected = self._export(node_ids=[GROUP_A])
        with patch.object(self.large, '_connect', side_effect=sqlite3.OperationalError('simulated unavailable source')):
            with self.assertRaises(bridge.SelectionError) as exc:
                self.create(selected=selected)
        self.assertEqual(exc.exception.status, 503)
        self.assertEqual(self._count('SELECT COUNT(*) FROM scans'), 0)
        self.assertEqual(self._count('SELECT COUNT(*) FROM analysis_jobs'), 0)

    def test_removed_source_between_export_and_handoff_requires_refresh(self):
        selected = self._export(node_ids=[GROUP_A])
        self.change('DELETE FROM files WHERE package_id=? AND path=?', (PACKAGE, selected['paths'][0]))
        self.assert_conflict(selected=selected)
        self.assertEqual(self._count('SELECT COUNT(*) FROM scans'), 0)

    def test_changed_source_between_export_and_handoff_requires_refresh(self):
        selected = self._export(node_ids=[GROUP_A])
        self.change('UPDATE files SET size=size+1 WHERE package_id=?', (PACKAGE,))
        self.assert_conflict(selected=selected)
        self.assertEqual(self._count('SELECT COUNT(*) FROM scans'), 0)

    def test_newly_sensitive_source_is_excluded_everywhere(self):
        selected = self._export(node_ids=[GROUP_A])
        blocked, allowed = sorted(selected['paths'])
        self.change('UPDATE files SET sensitive=1 WHERE package_id=? AND path=?', (PACKAGE, blocked))
        result = self.create(selected=selected)
        self.assertEqual(result['selected_file_count'], 1)
        with self.ordinary._connect() as conn:
            scan = json.loads(conn.execute('SELECT payload FROM scans WHERE id=?', (result['scan_id'],)).fetchone()[0])
            job = json.loads(conn.execute('SELECT options FROM analysis_jobs WHERE id=?', (result['job_id'],)).fetchone()[0])
            chosen = json.loads(conn.execute('SELECT included_paths FROM scan_selections WHERE scan_id=?', (result['scan_id'],)).fetchone()[0])
            snapshot = json.loads(conn.execute('SELECT selection_snapshot FROM large_catalog_imports WHERE scan_id=?', (result['scan_id'],)).fetchone()[0])
        self.assertEqual(scan['selected_paths'], [allowed])
        self.assertEqual(job['target_paths'], [allowed])
        self.assertEqual(job['deep_selection']['paths'], [allowed])
        self.assertEqual(chosen, [allowed])
        self.assertEqual(snapshot['selected_file_count'], 1)
        self.assertEqual(snapshot['selected_bytes'], 1024)
        self.assertEqual(snapshot['requested_file_count'], 2)
        self.assertEqual(snapshot['imported_paths'], [allowed])

    def test_already_sensitive_source_is_absent_from_worker_targets(self):
        selected = self._export(node_ids=[GROUP_A])
        blocked = selected['paths'][0]
        self.change('UPDATE files SET sensitive=1 WHERE package_id=? AND path=?', (PACKAGE, blocked))
        result = self.create()
        with self.ordinary._connect() as conn:
            job = json.loads(conn.execute('SELECT options FROM analysis_jobs WHERE id=?', (result['job_id'],)).fetchone()[0])
        self.assertNotIn(blocked, job['target_paths'])

    def legacy(self, result):
        with self.ordinary._connect() as conn:
            row = conn.execute('SELECT selection_snapshot FROM large_catalog_imports WHERE scan_id=?', (result['scan_id'],)).fetchone()
            payload = json.loads(row[0])
            payload.pop('import_identity', None)
            conn.execute('UPDATE large_catalog_imports SET selection_snapshot=? WHERE scan_id=?', (json.dumps(payload), result['scan_id']))

    def test_legacy_identical_retry_remains_idempotent(self):
        first = self.create()
        self.legacy(first)
        self.assertEqual(self.create()['scan_id'], first['scan_id'])

    def test_legacy_active_job_is_reused_only_for_same_mode(self):
        first = self.create(mode='fast')
        self.legacy(first)
        self.assertEqual(self.create(key='same-mode', mode='fast')['scan_id'], first['scan_id'])
        self.assertNotEqual(self.create(key='new-mode', mode='accurate')['scan_id'], first['scan_id'])

    def test_legacy_same_key_different_mode_is_conflict(self):
        first = self.create(mode='fast')
        self.legacy(first)
        self.assert_conflict(mode='accurate')

    def test_legacy_new_source_version_is_not_reused(self):
        first = self.create()
        self.legacy(first)
        self.change('UPDATE files SET size=size+1 WHERE package_id=?', (PACKAGE,))
        self.assertNotEqual(self.create(key='new-version')['scan_id'], first['scan_id'])

    def test_legacy_targets_with_excluded_files_are_not_reused(self):
        selected = self._export(node_ids=[GROUP_A])
        self.change('UPDATE files SET sensitive=1 WHERE package_id=? AND path=?', (PACKAGE, selected['paths'][0]))
        first = self.create()
        self.legacy(first)
        with self.ordinary._connect() as conn:
            row = conn.execute('SELECT options FROM analysis_jobs WHERE id=?', (first['job_id'],)).fetchone()
            options = json.loads(row[0])
            options['target_paths'] = selected['paths']
            conn.execute('UPDATE analysis_jobs SET options=? WHERE id=?', (json.dumps(options), first['job_id']))
        self.assert_conflict()
        self.assertNotEqual(self.create(key='new-key')['scan_id'], first['scan_id'])

    def test_changed_job_mode_cannot_be_hidden_by_cached_identity(self):
        first = self.create()
        with self.ordinary._connect() as conn:
            row = conn.execute('SELECT options FROM analysis_jobs WHERE id=?', (first['job_id'],)).fetchone()
            options = json.loads(row[0]); options['parse_mode'] = 'fast'
            conn.execute('UPDATE analysis_jobs SET options=? WHERE id=?', (json.dumps(options), first['job_id']))
        self.assert_conflict()
        self.assertNotEqual(self.create(key='new-key')['scan_id'], first['scan_id'])

    def test_missing_fingerprint_is_not_valid_idempotency_proof(self):
        first = self.create()
        with self.ordinary._connect() as conn:
            conn.execute('UPDATE large_catalog_imports SET selection_snapshot=? WHERE scan_id=?', ('{}', first['scan_id']))
        self.assert_conflict()

    def add_later_child(self, result, status):
        with self.ordinary._connect() as conn:
            conn.execute('INSERT INTO analysis_jobs(id,scan_id,task_type,status,options,created_at) VALUES(?,?,?,?,?,?)',
                         ('later-child', result['scan_id'], 'generate_summary', status, '{}', '2999-01-01'))

    def test_failed_child_does_not_hide_active_original_import(self):
        first = self.create()
        self.add_later_child(first, 'failed')
        self.assertEqual(self.create(key='second')['scan_id'], first['scan_id'])

    def test_running_child_does_not_make_completed_import_reusable(self):
        first = self.create()
        with self.ordinary._connect() as conn:
            conn.execute('UPDATE analysis_jobs SET status=? WHERE id=?', ('completed', first['job_id']))
        self.add_later_child(first, 'running')
        self.assertNotEqual(self.create(key='second')['scan_id'], first['scan_id'])

    def test_new_request_recovers_from_missing_job_instead_of_reusing_dead_id(self):
        first = self.create()
        with self.ordinary._connect() as conn:
            conn.execute('DELETE FROM analysis_jobs WHERE id=?', (first['job_id'],))
        self.assert_conflict()
        second = self.create(key='second')
        self.assertNotEqual(second['scan_id'], first['scan_id'])
        self.assertEqual(self._count('SELECT COUNT(*) FROM analysis_jobs'), 1)

    def test_new_request_does_not_reuse_missing_import_task(self):
        first = self.create()
        with self.ordinary._connect() as conn:
            conn.execute('DELETE FROM import_tasks WHERE scan_id=?', (first['scan_id'],))
        self.assertNotEqual(self.create(key='second')['scan_id'], first['scan_id'])

    def test_changed_scan_owner_cannot_be_reused_through_old_request(self):
        first = self.create()
        with self.ordinary._connect() as conn:
            conn.execute('UPDATE scans SET owner_id=? WHERE id=?', ('o2', first['scan_id']))
        self.assert_conflict()

    def test_source_batches_share_one_consistent_read_snapshot(self):
        paths = ['bulk/%04d.txt' % i for i in range(501)]
        original_connect = self.large._connect
        with closing(original_connect()) as conn:
            conn.executemany('INSERT INTO files(package_id,path,name,size,modified_at_ns,updated_at) VALUES(?,?,?,?,?,?)',
                             [(PACKAGE, path, path.rsplit('/', 1)[-1], 10, 1, 1.0) for path in paths])
            conn.commit()
        batch_sizes = []
        class Connection:
            def __init__(self):
                self.inner = original_connect()
            def execute(self, sql, parameters=()):
                result = self.inner.execute(sql, parameters)
                if sql.startswith('SELECT path,sha256'):
                    batch_sizes.append(len(parameters)-1)
                    if len(batch_sizes) == 1:
                        with closing(original_connect()) as writer:
                            writer.execute('UPDATE files SET size=99 WHERE package_id=? AND path=?', (PACKAGE, paths[-1]))
                            writer.commit()
                return result
            def close(self):
                self.inner.close()
        with patch.object(self.large, '_connect', side_effect=Connection):
            marks = bridge.load_file_fingerprints(self.large, PACKAGE, paths, strict=True)
        self.assertEqual(batch_sizes, [500, 1])
        self.assertEqual(len(marks), len(paths))
        self.assertEqual(marks[paths[-1]]['size'], 10)
        current = bridge.load_file_fingerprints(self.large, PACKAGE, [paths[-1]], strict=True)
        self.assertEqual(current[paths[-1]]['size'], 99)

    def test_path_order_does_not_change_identity(self):
        first = self.create()
        selected = self._export(node_ids=[GROUP_A])
        selected['paths'].reverse()
        selected['inventory'] = dict(reversed(list(selected['inventory'].items())))
        self.assertEqual(self.create(key='reordered', selected=selected)['scan_id'], first['scan_id'])

    def test_inventory_cannot_smuggle_unselected_file_into_import(self):
        selected = self._export(node_ids=[GROUP_A])
        selected['paths'] = selected['paths'][:1]
        self.assert_conflict(selected=selected)
        self.assertEqual(self._count('SELECT COUNT(*) FROM scans'), 0)

    def test_racing_different_modes_do_not_collapse(self):
        barrier = Barrier(2)
        original = bridge._inventory_records
        def synchronize(*args, **kwargs):
            value = original(*args, **kwargs)
            barrier.wait(timeout=20)
            return value
        stores = [self.ordinary, Storage(os.path.join(self.dir, 'ordinary.db'))]
        with patch.object(bridge, '_inventory_records', side_effect=synchronize):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.create, key='key-'+str(i), mode=mode, store=stores[i]) for i, mode in enumerate(['fast', 'accurate'])]
                results = [f.result(timeout=40) for f in futures]
        self.assertEqual(len({r['scan_id'] for r in results}), 2)
        self.assertEqual(self._count('SELECT COUNT(*) FROM analysis_jobs'), 2)

    def test_racing_same_key_different_modes_has_one_conflict(self):
        barrier = Barrier(2)
        original = bridge._inventory_records
        def synchronize(*args, **kwargs):
            value = original(*args, **kwargs)
            barrier.wait(timeout=20)
            return value
        stores = [self.ordinary, Storage(os.path.join(self.dir, 'ordinary.db'))]
        def create(i, mode):
            try:
                return self.create(mode=mode, store=stores[i])
            except bridge.SelectionError as exc:
                return exc
        with patch.object(bridge, '_inventory_records', side_effect=synchronize):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(create, i, mode) for i, mode in enumerate(['fast', 'accurate'])]
                results = [f.result(timeout=40) for f in futures]
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual([r.status for r in results if isinstance(r, bridge.SelectionError)], [409])
        self.assertEqual(self._count('SELECT COUNT(*) FROM analysis_jobs'), 1)
