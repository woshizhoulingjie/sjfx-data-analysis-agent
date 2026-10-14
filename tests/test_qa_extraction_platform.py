"""Real spawn/IPC and process-tree regressions for background QA extraction."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from services import large_qa_extract as extraction
from services.large_qa_resources import ResourcePressure
from services.package_analysis import _parse_with_limits
from services.process_liveness import (
    available_memory_bytes, initialize_process_family, pid_is_alive, process_rss_bytes,
)


def _qa_native_child(connection, path, relative, max_chars, root=None, doc=None, gate=None, parent_pid=None, protection_context=None):
    initialize_process_family()
    gate.wait(10)
    native = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    folder = Path(path).parent
    (folder / 'pids.json').write_text(json.dumps([os.getpid(), native.pid]))
    if relative.startswith('result'):
        connection.send(('ok', ([{'text': 'verified IPC result', 'location': {}}], True, '')))
        return
    if relative.startswith('resource'):
        connection.send(('resource_pressure', {'code': 'ENOSPC', 'message': 'injected disk full'}))
        return
    if relative.startswith('memory'):
        retained = bytearray(160 * 1024 * 1024)
        retained[0] = 1
    time.sleep(60)


class QAExtractionPlatformTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='sjfx-qa-platform-')
        self.root = Path(self.folder.name)
        self.addCleanup(self.folder.cleanup)

    def doc(self, name='source.txt'):
        path = self.root / name
        path.write_text('QA 正文和 source evidence。', encoding='utf-8')
        info = path.stat()
        return {'path': name, 'size': info.st_size, 'mtime': info.st_mtime_ns}

    def assert_tree_dead(self):
        pids = json.loads((self.root / 'pids.json').read_text())
        def alive(pid):
            if sys.platform.startswith('linux'):
                try:
                    if Path('/proc/{}/stat'.format(pid)).read_text().split(')', 1)[1].split()[0] == 'Z':
                        return False
                except (OSError, IndexError):
                    pass
            return pid_is_alive(pid)
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if not any(alive(pid) for pid in pids):
                return
            time.sleep(.02)
        self.fail('QA native descendant survived its owning extraction')

    def test_real_spawn_extraction_preserves_text_and_coverage_ipc(self):
        doc = self.doc('文字 文件.txt')
        records, complete, note = extraction.extract_document(self.root, doc, timeout=20)
        self.assertTrue(complete)
        self.assertIn('QA 正文', ' '.join(row['text'] for row in records))

    def test_actual_timeout_reaps_parser_and_native_descendant(self):
        doc = self.doc('timeout.txt')
        with patch.object(extraction, '_child', _qa_native_child):
            with self.assertRaisesRegex(TimeoutError, '时间预算'):
                extraction.extract_document(self.root, doc, timeout=3)
        self.assert_tree_dead()

    def test_actual_rss_limit_is_resource_pressure_and_reaps_native_descendant(self):
        if (available_memory_bytes() or 0) < 320 * 1024 * 1024:
            self.skipTest('insufficient memory for the real working-set fixture')
        doc = self.doc('memory.txt')
        with patch.object(extraction, '_child', _qa_native_child), patch.object(
            extraction, 'own_process_tree', wraps=extraction.own_process_tree
        ) as owner, patch.object(extraction, 'process_rss_bytes', wraps=process_rss_bytes) as rss:
            with self.assertRaises(ResourcePressure) as caught:
                extraction.extract_document(self.root, doc, timeout=10, memory_mb=128)
        self.assertEqual(caught.exception.code, 'memory_exhausted')
        actual_pid = json.loads((self.root / 'pids.json').read_text())[0]
        self.assertEqual(owner.call_args.args[0], actual_pid,
                         'multiprocessing PID must identify the real parsing interpreter')
        rss.assert_any_call(actual_pid)
        self.assert_tree_dead()

    def test_normal_result_reaps_native_descendant_after_parser_exit(self):
        doc = self.doc('result.txt')
        with patch.object(extraction, '_child', _qa_native_child):
            records, complete, note = extraction.extract_document(self.root, doc, timeout=10)
        self.assertTrue(complete)
        self.assertEqual(records[0]['text'], 'verified IPC result')
        self.assert_tree_dead()

    def test_resource_pressure_packet_retains_type_and_code(self):
        doc = self.doc('resource.txt')
        with patch.object(extraction, '_child', _qa_native_child):
            with self.assertRaises(ResourcePressure) as caught:
                extraction.extract_document(self.root, doc, timeout=10)
        self.assertEqual(caught.exception.code, 'ENOSPC')
        self.assert_tree_dead()

    def test_posix_helper_refuses_work_if_owner_died_before_initialization(self):
        connection, gate = Mock(), Mock()
        with patch.dict(os.environ), patch.object(extraction, 'initialize_process_family'), patch.object(
            extraction.os, 'name', 'posix'
        ), patch.object(extraction.os, 'getppid', return_value=456), patch.object(
            extraction.os, 'nice', create=True
        ), patch.object(extraction, '_extract') as parse:
            extraction._child(connection, 'unused', 'unused', 100, start_gate=gate, parent_pid=123)
        parse.assert_not_called()
        gate.wait.assert_not_called()
        self.assertEqual(connection.send.call_args.args[0][0], 'error')
        connection.close.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows Job ownership gate')
    def test_windows_ownership_failure_stops_spawn_before_document_work(self):
        doc = self.doc('blocked.txt')
        with patch.object(extraction, '_child', _qa_native_child), patch.object(
            extraction, 'own_process_tree', return_value=None
        ) as owner:
            with self.assertRaises(ResourcePressure) as caught:
                extraction.extract_document(self.root, doc, timeout=10)
        self.assertEqual(caught.exception.code, 'process_ownership')
        self.assertFalse((self.root / 'pids.json').exists())
        self.assertFalse(pid_is_alive(owner.call_args.args[0]))

    def test_package_guard_rejects_current_rss_before_calling_parser(self):
        parser = Mock()
        with patch.dict(os.environ, {'MAX_WORKER_MEMORY_MB': '256'}), patch(
            'services.package_analysis.process_rss_bytes', return_value=257 * 1024 * 1024
        ) as memory:
            with self.assertRaisesRegex(MemoryError, '当前内存'):
                _parse_with_limits(parser, 'unused', 'unused', 'fast')
        memory.assert_called_once_with(os.getpid())
        parser.parse.assert_not_called()

    def test_package_guard_recovers_after_transient_rss_allocation(self):
        parser = Mock()
        parser.parse.return_value = {'text': 'current memory recovered'}
        with patch.dict(os.environ, {'MAX_WORKER_MEMORY_MB': '256'}), patch(
            'services.package_analysis.process_rss_bytes',
            side_effect=[300 * 1024 * 1024, 80 * 1024 * 1024]
        ):
            with self.assertRaises(MemoryError):
                _parse_with_limits(parser, 'unused', 'unused', 'fast')
            result = _parse_with_limits(parser, 'unused', 'unused', 'fast')
        self.assertEqual(result['text'], 'current memory recovered')
        parser.parse.assert_called_once()

    def test_package_guard_reads_real_rss_on_host_platform(self):
        parser = Mock()
        parser.parse.return_value = {'text': 'verified'}
        with patch('services.package_analysis.process_rss_bytes', wraps=process_rss_bytes) as memory:
            result = _parse_with_limits(parser, 'unused', 'unused', 'fast')
        memory.assert_called_once_with(os.getpid())
        self.assertGreater(process_rss_bytes(os.getpid()), 0)
        self.assertEqual(result['text'], 'verified')


if __name__ == '__main__':
    unittest.main()
