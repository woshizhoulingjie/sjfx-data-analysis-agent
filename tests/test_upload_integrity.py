"""Upload bytes, interrupted publication, idempotency and session locking.

Pure unittest tests use real local files and injected I/O failures; no app,
server, model or production data. Also collected by pytest when available.
"""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sjfx_multiuser.uploads import UploadError, UploadManager, safe_rel_path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def concurrent_upload(root, start, results):
    """Spawn-safe worker: independent manager/process, same chunk/session."""
    try:
        manager = UploadManager(root, max_chunk_bytes=3)
        start.wait(15)
        result = manager.put_chunk('owner', 'session', 0, 0, b'abc', digest(b'abc'))
        results.put(('ok', result['already']))
    except Exception as exc:
        results.put(('error', repr(exc)))


class UploadIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sjfx-upload-integrity-')
        self.base = Path(self.temporary.name)
        self.root = self.base / 'uploads'
        self.manager = UploadManager(str(self.root), max_chunk_bytes=3)
        self.owner, self.session = 'owner', 'session'

    def tearDown(self):
        self.temporary.cleanup()

    def begin(self, data=b'abcdef', declared=True, name='a.txt'):
        self.data = data
        return self.manager.begin(self.owner, self.session, [{'rel_path': name, 'size': len(data),
                                  **({'sha256': digest(data)} if declared else {})}])

    def upload(self, data=b'abcdef', declared=True, name='a.txt'):
        self.begin(data, declared, name)
        for index in range((len(data) + 2) // 3):
            chunk = data[index * 3:(index + 1) * 3]
            self.manager.put_chunk(self.owner, self.session, 0, index, chunk, digest(chunk))

    def path(self, suffix):
        return self.root / self.owner / self.session / 'files' / ('0.' + suffix)

    def manifest(self):
        return self.manager._load(self.owner, self.session)

    def restart(self):
        self.manager = UploadManager(str(self.root), max_chunk_bytes=3)

    def assembled(self):
        return self.manager.assemble(self.owner, self.session, str(self.base / 'output'))

    def test_regular_resume_full_digest_and_finalize_retry(self):
        self.begin()
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.restart()
        self.assertEqual(self.manager.status(self.owner, self.session)['received_bytes'], 3)
        result = self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertEqual(result['actual_sha256'], digest(b'abcdef'))
        first = self.assembled()
        self.assertEqual(Path(first['files'][0]['path']).read_bytes(), b'abcdef')
        self.assertEqual(first['files'][0]['sha256'], digest(b'abcdef'))
        self.restart()
        self.assertEqual(self.assembled()['files'], first['files'])
        self.assertTrue(self.manager.status(self.owner, self.session)['complete'])
        retry = self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertTrue(retry['already'])

    def test_empty_and_nested_unicode_files(self):
        self.upload(b'', name='资料/空文件.txt')
        result = self.assembled()
        self.assertEqual(Path(result['files'][0]['path']).read_bytes(), b'')
        self.assertEqual(result['files'][0]['sha256'], digest(b''))

    def test_declaration_and_chunk_hash_formats(self):
        for value in ('bad', 'g' * 64, 42):
            with self.subTest(value=value), self.assertRaises(UploadError):
                self.manager.begin(self.owner, self.session, [{'rel_path': 'a', 'size': 3, 'sha256': value}])
        self.begin(b'abc')
        with self.assertRaises(UploadError):
            self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc', 'bad')
        with self.assertRaises(UploadError):
            self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc', digest(b'xyz'))
        self.assertEqual(self.manager.status(self.owner, self.session)['received_bytes'], 0)

    def test_wrong_declared_full_hash_is_rejected_and_can_reupload(self):
        self.begin(b'abc')
        with self.assertRaisesRegex(UploadError, 'SHA-256'):
            self.manager.put_chunk(self.owner, self.session, 0, 0, b'xyz', digest(b'xyz'))
        self.assertFalse(self.path('done').exists())
        self.assertEqual(self.path('part').read_bytes(), b'')
        self.assertFalse(self.manager.status(self.owner, self.session)['complete'])
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.assertEqual(Path(self.assembled()['files'][0]['path']).read_bytes(), b'abc')

    def test_actual_hash_recorded_without_client_file_hash(self):
        self.upload(declared=False)
        self.assertEqual(self.manifest()['files'][0]['actual_sha256'], digest(b'abcdef'))
        self.assertEqual(self.assembled()['files'][0]['sha256'], digest(b'abcdef'))

    def test_final_rename_then_manifest_failure_keeps_original_bytes(self):
        self.begin()
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        save = self.manager._save
        def fail_after_rename(owner, session, m):
            if self.path('done').exists() and m['files'][0]['state'] == 'complete':
                raise OSError('injected after final rename')
            return save(owner, session, m)
        with patch.object(self.manager, '_save', side_effect=fail_after_rename):
            with self.assertRaises(OSError):
                self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')
        self.restart()
        for _ in range(2):
            self.assertTrue(self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')['already'])
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')
        self.assertEqual(Path(self.assembled()['files'][0]['path']).read_bytes(), b'abcdef')

    def test_nonfinal_data_fsync_then_manifest_failure_recovers_receipt(self):
        self.begin(b'abcdefghi')
        save = self.manager._save
        def fail_after_data(owner, session, m):
            if m['files'][0]['chunks_done'] == 1:
                raise OSError('injected data written, receipt unsaved')
            return save(owner, session, m)
        with patch.object(self.manager, '_save', side_effect=fail_after_data):
            with self.assertRaises(OSError):
                self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.restart()
        self.assertTrue(self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')['already'])
        self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.manager.put_chunk(self.owner, self.session, 0, 2, b'ghi')
        self.assertEqual(self.path('done').read_bytes(), b'abcdefghi')

    def test_intent_before_write_failure_retries_cleanly(self):
        self.begin()
        with patch.object(self.manager, '_recover_entry', wraps=self.manager._recover_entry):
            real_fsync = os.fsync
            def failure(fd):
                if self.path('part').exists():
                    raise OSError('injected interrupted write')
                return real_fsync(fd)
            with patch('sjfx_multiuser.uploads.os.fsync', side_effect=failure):
                with self.assertRaises(OSError):
                    self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.restart()
        # fsync can fail after all bytes reach disk: recovery may acknowledge
        # verified bytes, but must never append them twice.
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_partial_intent_write_rolls_back_only_unconfirmed_suffix(self):
        self.begin()
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        m = self.manifest()
        m['files'][0]['pending_chunk'] = {'index': 1, 'offset': 3, 'size': 3, 'sha256': digest(b'def')}
        self.manager._save(self.owner, self.session, m)
        with self.path('part').open('ab') as output:
            output.write(b'd')
        self.restart()
        self.assertEqual(self.manager.status(self.owner, self.session)['received_bytes'], 3)
        self.assertEqual(self.path('part').read_bytes(), b'abc')
        self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_persisted_intent_without_any_bytes_recovers(self):
        self.begin()
        from sjfx_multiuser import uploads
        real_open = uploads._safe_open
        def fail_part(path, *args, **kwargs):
            if str(path).endswith('0.part'):
                raise OSError('injected after intent, before opening chunk')
            return real_open(path, *args, **kwargs)
        with patch('sjfx_multiuser.uploads._safe_open', side_effect=fail_part):
            with self.assertRaises(OSError):
                self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.restart()
        self.assertEqual(self.manager.status(self.owner, self.session)['received_bytes'], 0)
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_final_rename_failure_recovers_complete_part(self):
        self.begin()
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        with patch('sjfx_multiuser.uploads.os.rename', side_effect=OSError('injected rename failure')):
            with self.assertRaises(OSError):
                self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertEqual(self.path('part').read_bytes(), b'abcdef')
        self.restart()
        self.assertTrue(self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')['already'])
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_acknowledged_prefix_corruption_detected_without_file_declaration(self):
        self.begin(declared=False)
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.path('part').write_bytes(b'xyz')
        with self.assertRaisesRegex(UploadError, '分片.*SHA-256'):
            self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertFalse(self.path('done').exists())
        self.assertFalse(self.manager.status(self.owner, self.session)['complete'])

    def test_variable_sized_chunks_have_exact_retry_boundaries(self):
        self.begin(b'abcde')
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'ab')
        self.assertTrue(self.manager.put_chunk(self.owner, self.session, 0, 0, b'ab')['already'])
        with self.assertRaises(UploadError):
            self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.manager.put_chunk(self.owner, self.session, 0, 1, b'cde')
        self.assertEqual(Path(self.assembled()['files'][0]['path']).read_bytes(), b'abcde')

    def test_short_io_write_never_acknowledges_chunk(self):
        self.begin()
        real_fdopen = os.fdopen
        class ShortWriter:
            def __init__(self, file): self.file = file
            def __enter__(self): return self
            def __exit__(self, *args): return self.file.__exit__(*args)
            def __getattr__(self, name): return getattr(self.file, name)
            def write(self, data): return self.file.write(data[:1])
        def short(fd, mode='r', *args, **kwargs):
            file = real_fdopen(fd, mode, *args, **kwargs)
            return ShortWriter(file) if mode == 'r+b' else file
        with patch('sjfx_multiuser.uploads.os.fdopen', side_effect=short):
            with self.assertRaises(OSError):
                self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.restart()
        self.assertEqual(self.manager.status(self.owner, self.session)['received_bytes'], 0)
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_legacy_completed_and_assembled_manifest_upgrades_hash(self):
        self.upload(declared=False)
        m = self.manifest()
        entry = m['files'][0]
        for key in ('actual_sha256', 'verified_source', 'chunk_receipts'):
            entry.pop(key, None)
        m['schema'] = 1
        self.manager._save(self.owner, self.session, m)
        first = self.assembled()
        self.assertEqual(first['files'][0]['sha256'], digest(b'abcdef'))
        m = self.manifest()
        for key in ('actual_sha256', 'verified_source', 'chunk_receipts'):
            m['files'][0].pop(key, None)
        m['assembled_files']['0'].pop('sha256')
        m['assembled_files']['0'].pop('verified_source')
        self.manager._save(self.owner, self.session, m)
        self.restart()
        self.assertEqual(self.assembled()['files'], first['files'])

    def test_corrupt_manifest_counts_fail_without_touching_bytes(self):
        self.upload()
        m = self.manifest()
        m['files'][0]['received_bytes'] = 0
        self.manager._save(self.owner, self.session, m)
        with self.assertRaisesRegex(UploadError, '结构损坏'):
            self.manager.status(self.owner, self.session)
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_retries_require_identical_content_before_and_after_completion(self):
        self.begin()
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        with self.assertRaisesRegex(UploadError, '不一致'):
            self.manager.put_chunk(self.owner, self.session, 0, 0, b'xyz')
        self.assertTrue(self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')['already'])
        self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        for data, sha in ((b'xyz', None), (b'def', digest(b'xyz')), (b'ef', None)):
            with self.subTest(data=data, sha=sha), self.assertRaises(UploadError):
                self.manager.put_chunk(self.owner, self.session, 0, 1, data, sha)
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_out_of_order_oversize_and_short_final_do_not_corrupt_prefix(self):
        self.begin()
        with self.assertRaises(UploadError):
            self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        with self.assertRaises(UploadError):
            self.manager.put_chunk(self.owner, self.session, 0, 0, b'abcd')
        self.manager.put_chunk(self.owner, self.session, 0, None, b'abc')
        with self.assertRaises(UploadError):
            self.manager.put_chunk(self.owner, self.session, 0, 1, b'de')
        self.assertEqual(self.path('part').read_bytes(), b'abc')
        self.manager.put_chunk(self.owner, self.session, 0, None, b'def')
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_same_session_begin_cannot_reset_confirmed_bytes(self):
        files = [{'rel_path': 'a.txt', 'size': 6}]
        self.manager.begin(self.owner, self.session, files)
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.assertEqual(self.manager.begin(self.owner, self.session, files)['files'][0]['received_bytes'], 3)
        with self.assertRaises(UploadError):
            self.manager.begin(self.owner, self.session, [{'rel_path': 'other', 'size': 6}])
        self.assertEqual(self.path('part').read_bytes(), b'abc')

    def test_legacy_pending_resume_and_done_rename_recovery(self):
        self.begin()
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        m = self.manifest()
        m['schema'] = 1
        m['files'][0].pop('chunk_receipts')
        self.manager._save(self.owner, self.session, m)
        self.path('part').write_bytes(b'abcdef')
        self.path('part').rename(self.path('done'))
        self.restart()
        self.assertTrue(self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')['already'])
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_legacy_extra_part_bytes_only_drop_unconfirmed_suffix(self):
        self.begin()
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        m = self.manifest()
        m['schema'] = 1
        m['files'][0].pop('chunk_receipts')
        self.manager._save(self.owner, self.session, m)
        with self.path('part').open('ab') as output:
            output.write(b'xx')
        self.restart()
        self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_missing_confirmed_prefix_and_both_files_fail_closed(self):
        self.begin()
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.path('part').unlink()
        with self.assertRaisesRegex(UploadError, '丢失'):
            self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.path('part').write_bytes(b'abc')
        self.path('done').write_bytes(b'abcdef')
        with self.assertRaisesRegex(UploadError, 'part/done'):
            self.manager.put_chunk(self.owner, self.session, 0, 1, b'def')
        self.assertEqual(self.path('done').read_bytes(), b'abcdef')

    def test_same_size_done_tampering_rejected_by_assembly(self):
        self.upload(declared=False)
        original_stat = self.path('done').stat()
        self.path('done').write_bytes(b'xyzxyz')
        os.utime(self.path('done'), ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with self.assertRaisesRegex(UploadError, 'SHA-256'):
            self.assembled()
        self.assertTrue(self.path('done').exists())
        self.assertFalse((self.base / 'output' / 'a.txt').exists())

    def test_finalize_record_tampering_and_no_overwrite(self):
        self.upload()
        result = self.assembled()
        target = Path(result['files'][0]['path'])
        original_stat = target.stat()
        target.write_bytes(b'xyzxyz')
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with self.assertRaisesRegex(UploadError, 'SHA-256'):
            self.assembled()
        self.assertEqual(target.read_bytes(), b'xyzxyz')
        with self.assertRaises(UploadError):
            self.manager.assemble(self.owner, self.session, str(self.base / 'another'))

    def test_missing_assembled_file_and_unrecorded_target_fail_status(self):
        self.upload()
        result = self.assembled()
        target = Path(result['files'][0]['path'])
        target.unlink()
        with self.assertRaisesRegex(UploadError, '丢失'):
            self.manager.status(self.owner, self.session)
        target.write_bytes(b'xyzxyz')
        m = self.manifest()
        m['assembled_files'] = {}
        m['assembling_file'] = 0
        self.manager._save(self.owner, self.session, m)
        with self.assertRaisesRegex(UploadError, 'SHA-256'):
            self.manager.status(self.owner, self.session)

    def test_existing_unrelated_destination_is_never_overwritten(self):
        self.upload()
        target = self.base / 'output' / 'a.txt'
        target.parent.mkdir()
        target.write_bytes(b'abcdef')
        with self.assertRaisesRegex(UploadError, '拒绝覆盖'):
            self.assembled()
        self.assertEqual(target.read_bytes(), b'abcdef')
        self.assertTrue(self.path('done').exists())

    def test_placement_then_manifest_failure_recovers_by_hash(self):
        self.upload()
        save = self.manager._save
        def fail_after_placement(owner, session, m):
            if '0' in m.get('assembled_files', {}):
                raise OSError('injected after placing target')
            return save(owner, session, m)
        with patch.object(self.manager, '_save', side_effect=fail_after_placement):
            with self.assertRaises(OSError):
                self.assembled()
        self.assertTrue(self.path('done').exists())
        self.restart()
        self.assertEqual(Path(self.assembled()['files'][0]['path']).read_bytes(), b'abcdef')
        self.assertFalse(self.path('done').exists())

    def test_interrupted_placement_same_size_wrong_target_not_adopted(self):
        self.upload()
        destination = self.base / 'output'
        destination.mkdir()
        target = destination / 'a.txt'
        target.write_bytes(b'xyzxyz')
        m = self.manifest()
        m['assembly_destination'] = str(destination)
        m['assembling_file'] = 0
        self.manager._save(self.owner, self.session, m)
        with self.assertRaisesRegex(UploadError, 'SHA-256'):
            self.assembled()
        self.assertEqual(target.read_bytes(), b'xyzxyz')
        self.assertTrue(self.path('done').exists())

    def test_unchanged_completed_status_and_finalize_avoid_extra_hash_scans(self):
        self.upload()
        with patch.object(self.manager, '_hash_file', wraps=self.manager._hash_file) as verify:
            for _ in range(3):
                self.assertTrue(self.manager.status(self.owner, self.session)['complete'])
            self.assertEqual(verify.call_count, 3)  # stamp check; no reading
        first = self.assembled()
        # Hash cache uses source identity/change time. Assert no large-file
        # reads on unchanged finalize retry, rather than timing a tiny file.
        real_fdopen = os.fdopen
        class NoRead:
            def __init__(self, file): self.file = file
            def __enter__(self): return self
            def __exit__(self, *args): return self.file.__exit__(*args)
            def fileno(self): return self.file.fileno()
            def read(self, *args): raise AssertionError('unnecessary full-file rescan')
        def guarded(fd, mode='r', *args, **kwargs):
            file = real_fdopen(fd, mode, *args, **kwargs)
            return NoRead(file) if mode == 'rb' else file
        with patch('sjfx_multiuser.uploads.os.fdopen', side_effect=guarded):
            self.assertEqual(self.assembled()['files'], first['files'])

    def test_threads_independent_instances_serialize_same_session(self):
        self.begin(b'abc')
        barrier = threading.Barrier(8)
        def send(_):
            mgr = UploadManager(str(self.root), max_chunk_bytes=3)
            barrier.wait(timeout=15)
            return mgr.put_chunk(self.owner, self.session, 0, 0, b'abc')['already']
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(send, range(8)))
        self.assertEqual(results.count(False), 1)
        self.assertEqual(results.count(True), 7)
        self.assertEqual(self.path('done').read_bytes(), b'abc')

    def test_processes_serialize_same_session(self):
        self.begin(b'abc')
        context = multiprocessing.get_context('spawn')
        start, results = context.Event(), context.Queue()
        children = [context.Process(target=concurrent_upload, args=(str(self.root), start, results)) for _ in range(3)]
        try:
            for child in children:
                child.start()
            start.set()
            outcomes = [results.get(timeout=25) for _ in children]
            for child in children:
                child.join(timeout=25)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(outcomes.count(('ok', False)), 1, outcomes)
            self.assertEqual(outcomes.count(('ok', True)), 2, outcomes)
            self.assertEqual(self.path('done').read_bytes(), b'abc')
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            results.close()

    def test_conflicting_concurrent_same_index_does_not_change_winner(self):
        self.begin(b'abc', declared=False)
        barrier = threading.Barrier(2)
        def send(data):
            barrier.wait(timeout=15)
            try:
                self.manager.put_chunk(self.owner, self.session, 0, 0, data)
                return 'ok'
            except UploadError:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, [b'abc', b'xyz']))
        self.assertCountEqual(results, ['ok', 'rejected'])
        self.assertIn(self.path('done').read_bytes(), (b'abc', b'xyz'))

    def test_chunk_vs_abort_has_no_recreated_partial_session(self):
        self.begin()
        entered, release = threading.Event(), threading.Event()
        save = self.manager._save
        def hold_intent(owner, session, m):
            result = save(owner, session, m)
            if m['files'][0].get('pending_chunk'):
                entered.set()
                self.assertTrue(release.wait(timeout=15))
            return result
        with patch.object(self.manager, '_save', side_effect=hold_intent), ThreadPoolExecutor(max_workers=2) as pool:
            upload = pool.submit(self.manager.put_chunk, self.owner, self.session, 0, 0, b'abc')
            self.assertTrue(entered.wait(timeout=15))
            abort = pool.submit(self.manager.abort, self.owner, self.session)
            self.assertFalse(abort.done())
            release.set()
            upload.result(timeout=15)
            self.assertTrue(abort.result(timeout=15))
        self.assertFalse((self.root / self.owner / self.session).exists())

    def test_paths_reject_windows_aliases_and_collisions(self):
        for name in ('../bad', '/absolute', 'C:/bad', 'a:stream', 'NUL.txt', 'COM1', 'a.', 'a ', 'a/../b'):
            with self.subTest(name=name), self.assertRaises(UploadError):
                safe_rel_path(name)
        for names in (('A', 'a'), ('a', 'a/b'), ('a/b', 'a')):
            with self.subTest(names=names), self.assertRaises(UploadError):
                self.manager.begin(self.owner, self.session, [{'rel_path': n, 'size': 0} for n in names])

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            if os.name == 'nt' and directory:
                result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                        capture_output=True, check=False)
                if result.returncode == 0:
                    return
            self.skipTest('platform does not permit symlink creation')

    def test_symlink_ancestors_and_files_cannot_escape(self):
        outside = self.base / 'outside'
        outside.mkdir()
        link = self.root / self.owner
        self.symlink(link, outside, True)
        with self.assertRaises(UploadError):
            self.begin()
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_destination_and_done_are_rejected(self):
        self.upload()
        outside = self.base / 'outside'
        outside.mkdir()
        self.symlink(self.base / 'output', outside, True)
        with self.assertRaises(UploadError):
            self.assembled()
        self.assertEqual(list(outside.iterdir()), [])
        files = self.path('done').parent
        files.rename(files.with_name('original_files'))
        other = outside / '0.done'
        other.write_bytes(b'abcdef')
        self.symlink(files, outside, True)
        with self.assertRaises(UploadError):
            self.manager.status(self.owner, self.session)
        self.assertEqual(other.read_bytes(), b'abcdef')

    def test_allowed_root_and_existing_nested_link_rejected(self):
        self.upload(name='nested/a.txt')
        with self.assertRaises(UploadError):
            self.manager.assemble(self.owner, self.session, str(self.base / 'other'), allowed_root=str(self.base / 'allowed'))
        destination = self.base / 'output'
        destination.mkdir()
        outside = self.base / 'outside'
        outside.mkdir()
        self.symlink(destination / 'nested', outside, True)
        with self.assertRaises(UploadError):
            self.assembled()
        self.assertEqual(list(outside.iterdir()), [])

    def test_purge_only_expired_owned_session_and_abort_lock_survives(self):
        self.begin()
        m = self.manifest()
        m['updated_at'] = 1
        manifest = self.root / self.owner / self.session / 'session.json'
        manifest.write_text(json.dumps(m), encoding='utf-8')
        self.assertEqual(self.manager.purge_expired(now=8 * 86400), ['owner/session'])
        self.begin()
        self.assertTrue(self.manager.abort(self.owner, self.session))
        self.begin(b'abc')
        self.manager.put_chunk(self.owner, self.session, 0, 0, b'abc')
        self.assertEqual(self.path('done').read_bytes(), b'abc')


if __name__ == '__main__':
    unittest.main()
