import errno
import os
import tempfile
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import Config
from large_package_worker import LargePackageWorker
from services.large_package_isolated import LargePackageStore, iter_inventory
from services.large_package_preview import preview_file
from services.large_package_preview_runner import PreviewRunner, _serve
from services.large_package_resources import (AdmissionGuard, ResourcePressure,
    GIB, defer_lease, pressure, show_wait, sqlite_temp_directory)


class AdmissionTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'SQLite Windows VFS uses the native temp directory')
    def test_explicit_sqlite_temp_directory_precedes_python_default(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'SQLITE_TMPDIR':folder}):
            self.assertEqual(sqlite_temp_directory(),Path(folder).resolve())

    @unittest.skipIf(os.name == 'nt', 'Unix VFS fallback order')
    def test_sqlite_unix_fallback_order_matches_official_vfs(self):
        with patch.dict('os.environ', {'SQLITE_TMPDIR':'','TMPDIR':''}), patch(
                'services.large_package_resources.Path.is_dir',return_value=True), patch(
                'services.large_package_resources.os.access',return_value=True):
            self.assertEqual(sqlite_temp_directory(),Path('/var/tmp').resolve())

    def test_checks_all_volumes_and_throttles_healthy_probes(self):
        guard = AdmissionGuard([('state', '/state'), ('parser', '/scratch'), ('sqlite', '/tmp')])
        with patch.object(guard.disk, 'check') as check, patch(
                'services.large_package_resources.time.monotonic', return_value=10):
            guard.check()
            for _ in range(10000):guard.check()
        check.assert_called_once_with(guard.paths)
        self.assertEqual(len(guard.paths), 3)

    def test_retry_deadline_is_not_extended_by_polling(self):
        guard = AdmissionGuard([], retry_seconds=30)
        problem = ResourcePressure('space', 'disk_capacity')
        with patch('services.large_package_resources.time.monotonic', return_value=100):
            guard.defer(problem)
        for now in (101, 109, 120, 129):
            with patch('services.large_package_resources.time.monotonic', return_value=now):
                with self.assertRaises(ResourcePressure) as caught:guard.check()
                guard.defer(caught.exception)
        with patch('services.large_package_resources.time.monotonic', return_value=130):
            guard.check()
        self.assertIsNone(guard._failure)

    def test_unavailable_path_resolver_is_retried_after_backoff(self):
        resolver=MagicMock(side_effect=[ResourcePressure('missing temp','sqlite_temp_unavailable'),[]])
        guard=AdmissionGuard(resolver,retry_seconds=1)
        with patch('services.large_package_resources.time.monotonic',return_value=100):
            with self.assertRaises(ResourcePressure):guard.check()
        with patch('services.large_package_resources.time.monotonic',return_value=101):
            guard.check()
        self.assertEqual(resolver.call_count,2)

    def test_recovery_requires_headroom_not_just_threshold(self):
        with tempfile.TemporaryDirectory() as folder:
            guard = AdmissionGuard([('state', folder)], retry_seconds=1)
            for now, free, fail in ((10,GIB-1,True),(12,GIB+1,True),(14,2*GIB,False)):
                with patch('services.large_package_resources.time.monotonic', return_value=now), patch(
                        'services.large_qa_resources.shutil.disk_usage', return_value=SimpleNamespace(free=free)):
                    if fail:
                        with self.assertRaises(ResourcePressure):guard.check()
                    else:guard.check()

    def test_content_errors_are_not_reclassified_as_resource_pressure(self):
        for error in (ValueError('bad document'), PermissionError('protected'), FileNotFoundError('gone')):
            with self.assertRaises(ValueError):pressure(error)
        self.assertEqual(pressure(MemoryError()).code, 'memory_exhausted')
        self.assertEqual(pressure(OSError(errno.ENOSPC, 'full')).code, 'ENOSPC')


class PackageResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.allowed = patch.object(Config, 'SCAN_ALLOWED_ROOTS', ())
        self.allowed.start(); self.addCleanup(self.allowed.stop)
        self.store = LargePackageStore(self.root/'state.db')
        self.pid = self.store.create(self.root)['id']
        (self.root/'source.txt').write_text('Evidence: invoice remains unpaid.', encoding='utf8')
        self.store.upsert_file(self.pid, dict(path='source.txt',name='source.txt',size=33))
        self.store.update_package(self.pid, status='running',phase='quick_parse')
        self.worker = LargePackageWorker(self.store)
        self.worker._admission = AdmissionGuard([])
        for runner in self.worker._preview_runners:self.addCleanup(runner.close)

    def row(self, stage='quick'):
        with closing(self.store._connect()) as conn:
            return dict(conn.execute('SELECT * FROM queue WHERE package_id=? AND path=? AND stage=?',
                (self.pid,'source.txt',stage)).fetchone())

    def claim(self, stage='quick'):
        if stage != 'quick':
            with closing(self.store._connect()) as conn:
                conn.execute('UPDATE queue SET stage=? WHERE package_id=? AND stage=?', (stage,self.pid,'quick'))
        return self.store.claim(stage,1,package_id=self.pid)[0][2]

    def test_current_lease_refunded_once_without_touching_evidence(self):
        token = self.claim()
        before = self.store.get_file(self.pid,'source.txt')
        self.assertTrue(defer_lease(self.store,self.pid,'source.txt','quick',token,MemoryError()))
        self.assertFalse(defer_lease(self.store,self.pid,'source.txt','quick',token,MemoryError()))
        row=self.row()
        self.assertEqual((row['status'],row['attempts'],row['lease_token']),('retry',0,None))
        self.assertEqual(self.store.get_file(self.pid,'source.txt'),before)
        token2=self.claim()
        self.assertFalse(defer_lease(self.store,self.pid,'source.txt','quick',token,MemoryError()))
        self.assertEqual(self.row()['lease_token'],token2)
        self.assertEqual(self.row()['attempts'],1)

    def test_missing_token_cannot_requeue_someone_elses_work(self):
        self.claim()
        before=self.row()
        with self.assertRaises(ValueError):
            defer_lease(self.store,self.pid,'source.txt','quick',None,MemoryError())
        self.assertEqual(self.row(),before)

    def test_infrastructure_retry_does_not_erase_past_content_attempts(self):
        with closing(self.store._connect()) as conn:
            conn.execute('UPDATE queue SET attempts=4 WHERE package_id=?',(self.pid,))
        token=self.claim()
        self.assertTrue(defer_lease(self.store,self.pid,'source.txt','quick',token,MemoryError()))
        self.assertEqual(self.row()['attempts'],4)

    def test_resource_wait_does_not_override_user_pause(self):
        self.store.update_package(self.pid,status='paused',message='User paused')
        show_wait(self.store,self.pid,MemoryError())
        self.assertEqual(self.store.get(self.pid)['message'],'User paused')

    def test_low_disk_stops_before_claiming_or_recovering_leases(self):
        problem=ResourcePressure('low disk','disk_capacity')
        self.worker._admission.defer(problem)
        with patch.object(self.store,'stale_running_to_retry') as recover, patch.object(self.store,'claim') as claim:
            self.assertFalse(self.worker.run_once())
            recover.assert_not_called();claim.assert_not_called()
        self.assertEqual(self.row()['attempts'],0)
        self.assertEqual(self.store.get(self.pid)['status'],'running')
        self.assertIn('自动续跑',self.store.get(self.pid)['message'])

    def test_quick_resource_error_is_retry_not_failed_or_completed(self):
        runner=MagicMock()
        runner.run.side_effect=MemoryError('allocation')
        self.worker._preview_runners=[runner]
        before=self.store.get_file(self.pid,'source.txt')
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.row()['status'],'retry')
        self.assertEqual(self.row()['attempts'],0)
        self.assertEqual(self.store.get_file(self.pid,'source.txt'),before)
        self.assertEqual(self.store.get(self.pid)['phase'],'quick_parse')

    def test_nonresource_parse_failure_retains_original_failure_behavior(self):
        runner=MagicMock();runner.run.side_effect=ValueError('invalid document')
        self.worker._preview_runners=[runner]
        with patch.object(self.worker,'_catalog_one',return_value=True):
            self.worker.run_once()
        self.assertEqual(self.row()['status'],'failed')
        self.assertEqual(self.row()['attempts'],1)

    def test_same_file_succeeds_after_resource_recovery(self):
        runner=MagicMock();runner.run.side_effect=MemoryError()
        self.worker._preview_runners=[runner]
        self.assertFalse(self.worker.run_once())
        runner.run.side_effect=None
        runner.run.return_value=(dict(summary_type='local',sample='invoice remains unpaid',portrait={},category='fixture'),None)
        self.worker._admission._retry_at=0
        with patch.object(self.worker,'_catalog_one',return_value=True), patch.object(self.worker,'_catalog_incremental',return_value=0):
            self.assertTrue(self.worker.run_once())
        self.assertEqual(self.row()['status'],'completed')
        self.assertEqual(self.row()['attempts'],1)
        self.assertIn('invoice remains unpaid',str(self.store.get_file(self.pid,'source.txt')['local_summary']))

    def test_database_full_rolls_back_batch_without_failing_content(self):
        error=sqlite3.OperationalError('database or disk is full');error.sqlite_errorcode=13
        runner=MagicMock();runner.run.return_value=(dict(summary_type='local',sample='kept',portrait={},category='fixture'),None)
        self.worker._preview_runners=[runner]
        before=self.store.get_file(self.pid,'source.txt')
        with patch.object(self.store,'commit_stage_batch',side_effect=error):
            self.assertFalse(self.worker.run_once())
        self.assertEqual((self.row()['status'],self.row()['attempts']),('retry',0))
        self.assertEqual(self.store.get_file(self.pid,'source.txt'),before)

    def test_resource_failure_drains_and_keeps_other_completed_futures(self):
        self.store.upsert_file(self.pid,dict(path='healthy.txt',name='healthy.txt',size=10))
        def run(root,item,**kwargs):
            if item['path']=='source.txt':raise MemoryError()
            time.sleep(.02)
            return dict(summary_type='local',sample='healthy evidence',portrait={},category='fixture'),None
        runners=[MagicMock(),MagicMock()]
        for runner in runners:runner.run.side_effect=run
        self.worker._preview_runners=runners
        with patch.object(self.worker,'_catalog_incremental',return_value=0):
            self.assertFalse(self.worker.run_once())
        self.assertEqual(self.row()['status'],'retry')
        self.assertEqual(self.store.stage_counts(self.pid,'quick'),{'completed':1,'retry':1})
        self.assertIn('healthy evidence',str(self.store.get_file(self.pid,'healthy.txt')['local_summary']))

    def test_deep_memory_error_preserves_existing_results(self):
        token=self.claim('deep')
        before=self.store.get_file(self.pid,'source.txt')
        with patch.object(self.worker,'_deep_parse',side_effect=MemoryError()):
            with self.assertRaises(ResourcePressure):
                self.worker._deep_worker(self.pid,str(self.root),'source.txt',token)
        self.assertEqual(self.row('deep')['status'],'retry')
        self.assertEqual(self.row('deep')['attempts'],0)
        self.assertEqual(self.store.get_file(self.pid,'source.txt'),before)

    def test_inventory_pressure_preserves_phase_and_all_existing_rows(self):
        self.store.update_package(self.pid,phase='inventory')
        before=self.store.get_file(self.pid,'source.txt')
        with patch('large_package_worker.iter_inventory',side_effect=MemoryError()):
            self.assertFalse(self.worker.run_once())
        self.assertEqual(self.store.get(self.pid)['phase'],'inventory')
        self.assertNotEqual(self.store.get(self.pid)['status'],'failed')
        self.assertEqual(self.store.get_file(self.pid,'source.txt'),before)

    def test_parser_resource_oserror_cannot_become_an_empty_success(self):
        for code in (errno.ENOSPC,errno.EDQUOT,errno.EMFILE,errno.ENOMEM):
            with self.subTest(code=code), patch('services.large_package_preview._text',
                    side_effect=OSError(code,'infrastructure')):
                with self.assertRaises(OSError):preview_file(self.root,dict(path='source.txt'))

    def test_inventory_handle_exhaustion_is_not_a_skipped_directory(self):
        with patch('services.large_package_isolated.os.scandir',side_effect=OSError(errno.EMFILE,'handles')):
            with self.assertRaises(OSError):list(iter_inventory(self.root))

    def test_catalog_refinement_pressure_preserves_reserved_budget_and_pending_job(self):
        from services.large_package_catalog_refine import process_one,source_key
        row=self.store.get_file(self.pid,'source.txt')
        with closing(self.store._connect()) as conn:
            conn.execute("UPDATE queue SET stage='catalog_refine' WHERE package_id=?",(self.pid,))
            conn.execute('INSERT INTO lp_catalog_refine VALUES(?,?,?,?,?,?,?)',
                (self.pid,'source.txt',source_key(row),'queued',20,1024,1))
        self.worker.preview_runner=MagicMock()
        self.worker.preview_runner.run.side_effect=MemoryError()
        with self.assertRaises(ResourcePressure):process_one(self.worker)
        self.assertEqual((self.row('catalog_refine')['status'],self.row('catalog_refine')['attempts']),('retry',0))
        with closing(self.store._connect()) as conn:
            state=conn.execute('SELECT status,seconds,bytes,pages FROM lp_catalog_refine WHERE package_id=?',(self.pid,)).fetchone()
        self.assertEqual(tuple(state),('queued',20,1024,1))
        self.assertEqual(self.store.get_file(self.pid,'source.txt'),row)

    def test_refinement_read_failure_after_claim_returns_the_lease(self):
        from services.large_package_catalog_refine import process_one
        with closing(self.store._connect()) as conn:
            conn.execute("UPDATE queue SET stage='catalog_refine' WHERE package_id=?",(self.pid,))
        with patch.object(self.store,'get_file',side_effect=MemoryError()):
            with self.assertRaises(ResourcePressure):process_one(self.worker)
        self.assertEqual((self.row('catalog_refine')['status'],self.row('catalog_refine')['attempts']),('retry',0))
        self.assertEqual(self.worker._resource_package_id,self.pid)

    def test_inventory_resource_stat_error_is_not_a_skipped_file(self):
        entry=MagicMock();entry.path=str(self.root/'source.txt')
        entries=MagicMock();entries.__iter__.return_value=iter([entry])
        # The shared no-follow boundary uses lstat on Windows and DirEntry.stat
        # on POSIX. Inject resource pressure at that boundary on either OS.
        with patch('services.large_package_isolated.os.scandir',return_value=entries), patch(
                'services.large_package_isolated.nofollow_entry_stat',side_effect=OSError(errno.ENOMEM,'memory')):
            with self.assertRaises(OSError) as caught:list(iter_inventory(self.root))
        self.assertEqual(caught.exception.errno,errno.ENOMEM)
        entries.close.assert_called_once()

    def test_inventory_permission_error_keeps_existing_visible_warning(self):
        warnings=[]
        with patch('services.large_package_isolated.os.scandir',side_effect=PermissionError('protected')):
            self.assertEqual(list(iter_inventory(self.root,on_error=lambda path,error:warnings.append(error))),[])
        self.assertEqual(warnings,['protected'])

    def test_parser_format_problem_still_returns_explicit_warning(self):
        with patch('services.large_package_preview._text',side_effect=ValueError('malformed')):
            result,_=preview_file(self.root,dict(path='source.txt'))
        self.assertIn('malformed',result['portrait']['warnings'])


class PreviewProtocolTests(unittest.TestCase):
    def test_process_constructor_failure_also_closes_both_pipes(self):
        context=MagicMock();parent=MagicMock();child=MagicMock()
        context.Pipe.return_value=(parent,child);context.Process.side_effect=MemoryError()
        runner=PreviewRunner()
        with patch('services.large_package_preview_runner.multiprocessing.get_context',return_value=context):
            with self.assertRaises(MemoryError):runner.run('/unused',dict(path='a.txt'))
        parent.close.assert_called_once();child.close.assert_called_once()
        self.assertIsNone(runner.process);self.assertIsNone(runner.connection)

    def test_process_start_failure_closes_both_pipe_handles(self):
        context=MagicMock();parent=MagicMock();child=MagicMock()
        context.Pipe.return_value=(parent,child)
        context.Process.return_value.start.side_effect=OSError(errno.EMFILE,'handles')
        runner=PreviewRunner()
        with patch('services.large_package_preview_runner.multiprocessing.get_context',return_value=context):
            with self.assertRaises(OSError):runner.run('/unused',dict(path='a.txt'))
        parent.close.assert_called_once();child.close.assert_called_once()
        self.assertIsNone(runner.process);self.assertIsNone(runner.connection)

    def test_child_transports_resource_type_instead_of_only_string(self):
        connection=MagicMock()
        connection.recv.side_effect=[dict(root='/unused',item={}),EOFError()]
        # This protocol test runs the child loop in the pytest process. Its
        # real session/signal setup belongs to the spawned-process tests.
        with patch('services.large_package_preview.preview_file',side_effect=MemoryError()), patch(
                'services.large_package_preview_runner.os.nice', create=True), patch(
                'services.large_package_preview_runner.initialize_process_family'):
            _serve(connection)
        self.assertEqual(connection.send.call_args.args[0]['resource_code'],'memory_exhausted')
        connection.close.assert_called_once()

    def test_parent_raises_transported_resource_error_and_releases_child(self):
        runner=PreviewRunner();runner.process=MagicMock();runner.connection=MagicMock()
        runner.connection.recv.return_value={'error':'allocation','resource_code':'memory_exhausted'}
        with patch.object(runner,'close') as close:
            with self.assertRaises(ResourcePressure) as caught:runner.run('/unused',dict(path='a.txt'))
        self.assertEqual(caught.exception.code,'memory_exhausted');close.assert_called_once()
