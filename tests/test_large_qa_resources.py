"""Offline exhaustion/recovery contracts; never reads production state."""
from contextlib import ExitStack, contextmanager, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from services.large_qa_resources import DiskGuard, GIB, ResourcePressure, existing_directory, resource_code
from services.large_qa_index import LargeQAIndex
from services import large_qa_extract as extraction
from scripts import large_qa_worker as worker


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_only_typed_infrastructure_errors_are_retryable(self):
        for number in (errno.ENOSPC, errno.EDQUOT, errno.ENOMEM, errno.EMFILE, errno.ENFILE):
            with self.subTest(number=number):
                self.assertIsNotNone(resource_code(OSError(number, 'test')))
        self.assertIsNotNone(resource_code(MemoryError()))
        for error in (ValueError('disk full'), PermissionError(errno.EACCES, 'test'), RuntimeError('test')):
            with self.subTest(error=type(error).__name__):
                self.assertIsNone(resource_code(error))
        for code in (5, 6, 7, 13, 5 | (2 << 8)):
            error = sqlite3.OperationalError('injected')
            error.sqlite_errorcode = code
            self.assertIsNotNone(resource_code(error))
        error = sqlite3.OperationalError('corrupt')
        error.sqlite_errorcode = 11
        self.assertIsNone(resource_code(error))

    def test_missing_target_checks_existing_volume_without_creating_paths(self):
        target = self.root/'not-created'/'child'
        self.assertEqual(existing_directory(target), self.root)
        with patch('services.large_qa_resources.shutil.disk_usage', return_value=SimpleNamespace(free=2*GIB)) as usage:
            DiskGuard().check([('index', target)])
        usage.assert_called_once_with(self.root)
        self.assertFalse(target.exists())

    def test_reserve_floor_and_recovery_hysteresis(self):
        guard = DiskGuard(reserve_bytes=1)
        with patch('services.large_qa_resources.shutil.disk_usage') as usage:
            usage.return_value.free = GIB-1
            with self.assertRaises(ResourcePressure) as caught:
                guard.check([('index', self.root)])
            self.assertEqual(caught.exception.details[0]['free_bytes'], GIB-1)
            usage.return_value.free = GIB
            with self.assertRaises(ResourcePressure):
                guard.check([('index', self.root)])
            usage.return_value.free = GIB + GIB//4
            guard.check([('index', self.root)])
            usage.return_value.free = GIB
            guard.check([('index', self.root)])

    def test_low_temp_volume_pauses_even_when_index_volume_has_space(self):
        (self.root/'temp').mkdir()
        with patch('services.large_qa_resources.shutil.disk_usage', side_effect=[SimpleNamespace(free=5*GIB), SimpleNamespace(free=100)]):
            with self.assertRaises(ResourcePressure) as caught:
                DiskGuard().check([('index', self.root), ('temp', self.root/'temp')])
        self.assertEqual([x['label'] for x in caught.exception.details], ['temp'])

    def test_unreadable_capacity_fails_closed(self):
        with patch('services.large_qa_resources.shutil.disk_usage', side_effect=PermissionError('no access')):
            with self.assertRaises(ResourcePressure) as caught:
                DiskGuard().check([('temp', self.root)])
        self.assertIn('no access', caught.exception.details[0]['error'])

    def test_child_preserves_resource_error_and_generic_error_contract(self):
        for error, tag in ((OSError(errno.ENOSPC, 'full'), 'resource_pressure'), (MemoryError('full'), 'resource_pressure'), (ValueError('invalid file'), 'error')):
            with self.subTest(tag=tag, error=type(error).__name__):
                connection = MagicMock()
                with patch.dict(os.environ), patch.object(extraction.os, 'nice', create=True), patch.object(extraction, '_extract', side_effect=error):
                    extraction._child(connection, 'x', 'x', 100)
                self.assertEqual(connection.send.call_args.args[0][0], tag)
                connection.close.assert_called_once()

    def test_parent_preserves_resource_error_type(self):
        source = self.root/'file.txt'
        source.write_text('原文 English ภาษาไทย', encoding='utf8')
        info = source.stat()
        context = MagicMock()
        parent, child = MagicMock(), MagicMock()
        context.Pipe.return_value = (parent, child)
        parent.poll.return_value = True
        parent.recv.return_value = ('resource_pressure', {'code':'ENOSPC', 'message':'full'})
        # This fixture creates no OS process; never expose MagicMock's PID=1 coercion.
        context.Process.return_value.pid = None
        context.Process.return_value.is_alive.return_value = False
        with patch.object(extraction.mp, 'get_context', return_value=context):
            with self.assertRaises(ResourcePressure) as caught:
                extraction.extract_document(self.root, dict(path=source.name, size=info.st_size, mtime=info.st_mtime_ns))
        self.assertEqual(caught.exception.code, 'ENOSPC')
        parent.close.assert_called_once()
        context.Process.return_value.join.assert_called()
        context.Process.return_value.terminate.assert_not_called()
        context.Process.return_value.kill.assert_not_called()

    def test_heartbeat_failure_keeps_last_good_status_and_logs_error(self):
        target = self.root/'worker_status.json'
        target.write_text('{"at":1}', encoding='utf8')
        with patch.object(worker.os, 'replace', side_effect=OSError(errno.ENOSPC, 'full')), self.assertLogs(worker.LOGGER, level='ERROR'):
            self.assertFalse(worker.heartbeat(self.root, {}, 'running'))
        self.assertEqual(target.read_text(encoding='utf8'), '{"at":1}')
        self.assertFalse(target.with_suffix('.tmp').exists())
        self.assertTrue(worker.heartbeat(self.root, {}, 'running'))

    def test_process_start_failure_releases_both_pipe_handles(self):
        source = self.root/'a.txt'
        source.write_text('original')
        info = source.stat()
        context = MagicMock()
        parent, child = MagicMock(), MagicMock()
        context.Pipe.return_value = (parent, child)
        context.Process.return_value.start.side_effect = OSError(errno.EMFILE, 'full')
        with patch.object(extraction.mp, 'get_context', return_value=context):
            with self.assertRaises(OSError):
                extraction.extract_document(self.root, dict(path=source.name, size=info.st_size, mtime=info.st_mtime_ns))
        parent.close.assert_called_once()
        child.close.assert_called_once()
        context.Process.return_value.join.assert_not_called()


class WorkerResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root/'indexes'
        self.package = dict(id='lp-test', root_path=str(self.root), owner_id='owner')
        self.index = LargeQAIndex('lp-test', self.base, create=True)
        self.row = dict(path='原文.txt', name='原文.txt', local_summary='{"summary":"中文 English ภาษาไทย"}',
                        deep_summary='{}', size=100, modified_at_ns=1, sensitive=0)
        self.index.sync_rows([self.row], self.package, 'test')

    def contents(self):
        with self.index.connect() as db:
            return ([tuple(r) for r in db.execute('SELECT * FROM documents ORDER BY path')],
                    [tuple(r) for r in db.execute('SELECT * FROM chunks ORDER BY id')], self.index.meta(db))

    def run_worker(self, error=None, disk_error=None, packages=None, factory=None, once=True, clock=None):
        args = SimpleNamespace(base=str(self.base), source_db=str(self.root/'unused.db'), package=None, once=once, summaries_only=False)
        output = io.StringIO()
        code = 0
        with ExitStack() as stack:
            stack.enter_context(patch.object(worker, 'STOP', False))
            stack.enter_context(patch.object(worker, 'enabled', side_effect=[True, True, True, False] if not once else None, return_value=True))
            stack.enter_context(patch.object(worker, 'ReadOnlyStore', return_value=SimpleNamespace(packages=lambda: packages or [self.package])))
            stack.enter_context(patch.object(worker.demand, 'demand_only', return_value=False))
            stack.enter_context(patch.object(worker.demand, 'extract_pause', return_value=0))
            stack.enter_context(patch.object(worker.demand, 'extract_budget', return_value=10))
            stack.enter_context(patch.object(worker, 'foreground_busy', return_value=False))
            stack.enter_context(patch.object(worker, 'refresh_maps'))
            stack.enter_context(patch.object(worker.Config, 'LARGE_QA_REFINE_TOPICS', False))
            stack.enter_context(patch.object(worker.Config, 'DATA_DIR', self.root))
            stack.enter_context(patch.object(worker.Config, 'PARSE_TEMP_DIR', self.root/'parse-temp'))
            stack.enter_context(patch.dict(os.environ, {'SJFX_PARSE_TEMP_DIR':str(self.root/'parse-temp')}))
            stack.enter_context(patch.object(LargeQAIndex, 'sync_store', return_value=0))
            guard = stack.enter_context(patch.object(worker, 'DiskGuard')).return_value
            guard.check.side_effect = disk_error
            parsed = stack.enter_context(patch.object(worker, 'extract_document', side_effect=error,
                return_value=([{'text':'中文 English ภาษาไทย', 'location':{'page':9}}], True, 'complete')))
            if factory is not None:
                stack.enter_context(patch.object(worker, 'LargeQAIndex', side_effect=factory))
            if clock is not None:
                stack.enter_context(patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]))
                stack.enter_context(patch.object(worker.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0]+n)))
            with redirect_stdout(output):
                try:
                    worker.run(args)
                except SystemExit as exc:
                    code = exc.code
        return json.loads(output.getvalue()), code, parsed.call_count, guard.check.call_args_list

    def test_disk_check_precedes_index_constructor(self):
        factory = MagicMock(side_effect=AssertionError('must not open index'))
        states, code, calls, _ = self.run_worker(disk_error=ResourcePressure('low', 'disk_capacity'), factory=factory)
        factory.assert_not_called()
        self.assertEqual((code, calls), (1, 0))
        self.assertIn('resource_pause', states['lp-test'])
        status = json.loads((self.base/'worker_status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['phase'], 'waiting_for_resources')

    def test_resource_pause_preserves_pending_document_and_recovers_all_languages(self):
        before = self.contents()
        states, code, _, paths = self.run_worker(error=ResourcePressure('full', 'ENOSPC'))
        self.assertEqual(code, 1)
        self.assertEqual(self.contents(), before)
        self.assertEqual(len(paths[-1].args[0]), 3)
        self.assertEqual(states['lp-test']['resource_pause']['code'], 'ENOSPC')
        states, code, _, _ = self.run_worker()
        self.assertEqual(code, 0)
        self.assertNotIn('error', states['lp-test'])
        with self.index.connect() as db:
            self.assertEqual(db.execute('SELECT parse_status FROM documents').fetchone()[0], 'complete')
            original = db.execute("SELECT text,location FROM chunks WHERE kind='original'").fetchone()
            self.assertEqual(original['text'], '中文 English ภาษาไทย')
            self.assertEqual(json.loads(original['location'])['page'], 9)

    def test_true_document_error_remains_visible_as_failed(self):
        _, code, _, _ = self.run_worker(error=ValueError('invalid format'))
        self.assertEqual(code, 0)
        with self.index.connect() as db:
            row = db.execute('SELECT parse_status,parse_note FROM documents').fetchone()
        self.assertEqual(row['parse_status'], 'failed')
        self.assertIn('invalid format', row['parse_note'])

    def test_constructor_failure_does_not_stop_other_packages(self):
        bad = dict(self.package, id='lp-broken')
        def factory(package_id, *args, **kwargs):
            if package_id == 'lp-broken':
                raise sqlite3.DatabaseError('corrupt index')
            return LargeQAIndex(package_id, *args, **kwargs)
        states, code, parsed, _ = self.run_worker(packages=[bad, self.package], factory=factory)
        self.assertEqual((code, parsed), (1, 1))
        self.assertIn('corrupt index', states['lp-broken']['error'])
        self.assertNotIn('error', states['lp-test'])

    def test_backoff_prevents_hot_loop_and_recovers_in_same_process(self):
        clock = [100.0]
        result = ([{'text':'恢复中文 English ภาษาไทย', 'location':{'page':2}}], True, 'done')
        states, code, calls, _ = self.run_worker(error=[ResourcePressure('full', 'ENOSPC'), result], once=False, clock=clock)
        self.assertEqual((code, calls), (0, 2))
        self.assertGreaterEqual(clock[0], 130.0)
        self.assertNotIn('resource_pause', states['lp-test'])

    def test_restricted_documents_are_never_parsed(self):
        self.index.sync_rows([dict(self.row, sensitive=1)], self.package, 'test')
        _, code, calls, _ = self.run_worker()
        self.assertEqual((code, calls), (0, 0))
        with self.index.connect() as db:
            self.assertEqual(db.execute('SELECT parse_status FROM documents').fetchone()[0], 'restricted')

    def test_mid_write_exhaustion_restores_existing_evidence_and_fts(self):
        doc = self.index.next_parse()
        self.index.put_original(doc['path'], doc['source_version'], [{'text':'Existing evidence before retry', 'location':{'page':3}}], True)
        before = self.contents()
        original_add = self.index._add_chunk
        for error in (OSError(errno.ENOSPC, 'full'), MemoryError('full')):
            count = [0]
            def fail_second(*args, **kwargs):
                count[0] += 1
                if count[0] == 2:
                    raise error
                return original_add(*args, **kwargs)
            with self.subTest(error=type(error).__name__), patch.object(self.index, '_add_chunk', side_effect=fail_second):
                with self.assertRaises(type(error)):
                    self.index.put_original(doc['path'], doc['source_version'],
                        [{'text':'new paragraph', 'location':{}}, {'text':'another paragraph', 'location':{}}], True)
            self.assertEqual(self.contents(), before)
            with self.index.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM search WHERE search MATCH 'existing'").fetchone()[0], 1)

    def test_sqlite_full_keeps_pending_and_all_summary_evidence(self):
        doc = self.index.next_parse()
        before = self.contents()
        error = sqlite3.OperationalError('database or disk is full')
        error.sqlite_errorcode = 13
        with patch.object(self.index, '_add_chunk', side_effect=error):
            with self.assertRaises(sqlite3.OperationalError):
                self.index.put_original(doc['path'], doc['source_version'], [{'text':'payload'}], True)
        self.assertEqual(self.contents(), before)

    def test_real_sqlite_page_limit_rolls_back_all_evidence(self):
        doc = self.index.next_parse()
        self.index.put_original(doc['path'], doc['source_version'], [{'text':'Existing original evidence'}], True)
        before = self.contents()
        normal_connect = self.index.connect
        @contextmanager
        def constrained(write=False):
            with normal_connect(write=write) as db:
                pages = db.execute('PRAGMA page_count').fetchone()[0]
                db.execute('PRAGMA max_page_count=' + str(pages))
                yield db
        with patch.object(self.index, 'connect', constrained):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.index.put_original(doc['path'], doc['source_version'], [{'text':'large new evidence '*30000}], True)
        self.assertEqual(caught.exception.sqlite_errorcode, 13)
        self.assertEqual(self.contents(), before)
        with self.index.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM search WHERE search MATCH 'existing'").fetchone()[0], 1)

    def test_once_reports_heartbeat_write_failure(self):
        with patch.object(worker, 'heartbeat', return_value=False):
            _, code, _, _ = self.run_worker()
        self.assertEqual(code, 1)
