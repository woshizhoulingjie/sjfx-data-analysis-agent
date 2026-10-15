"""Ordinary archive raw recovery, with isolated storage and no server startup."""
import gc
import hashlib
import io
import os
import tarfile
import tempfile
import unittest
import zipfile
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as web
from services import package_analysis
from services.logical_units import archive_member_units
from services.scanner import scan_directory
from services.storage import Storage


BODY = b'Exact selected archive member, never the whole container.'


def write_archive(path, members=None):
    members = members or [('docs/member.txt', BODY), ('sibling.txt', b'private sibling'),
                          ('.env', b'private sensitive')]
    if path.suffix == '.zip':
        with zipfile.ZipFile(path, 'w') as archive:
            for name, body in members:
                archive.writestr(name, body)
    else:
        with tarfile.open(path, 'w:gz' if path.name.endswith('.gz') else 'w') as archive:
            for name, body in members:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))


class ArchiveRawRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix='sjfx-archive-raw-recovery-')
        self.addCleanup(self.sandbox.cleanup)
        self.root = Path(self.sandbox.name) / 'sources'
        self.root.mkdir()
        self.scratch = Path(self.sandbox.name) / 'snapshots'
        self.scratch.mkdir()
        for name in ('bundle.zip', 'bundle.tar', 'bundle.tar.gz'):
            write_archive(self.root / name)
        (self.root / 'plain.txt').write_bytes(BODY)
        self.storage = Storage(Path(self.sandbox.name) / 'ordinary.db')
        self.owner = [{'owner_id': 'archive-test-owner', 'role': 'user'}]
        replacements = [
            (web, 'storage', self.storage),
            (web.Config, 'PARSE_TEMP_DIR', self.scratch),
            (web.Config, 'PARSE_TEMP_DISK_RESERVE_BYTES', 0),
            (web.Config, 'SOURCE_STABILITY_SECONDS', 0),
            (web.Config, 'SCAN_ALLOWED_ROOTS', (self.root,)),
            (web.Config, 'OWNER_ID', 'archive-test-owner'),
            (web.Config, 'AUTH_REQUIRED', True),
            (web.Config, 'API_ACCESS_TOKEN', 'archive-test-token'),
            (web.Config, 'API_TOKEN_EXPIRES_AT', None),
            (web._file_protection, 'guard_root', lambda _root: None),
            (web._multiuser_runtime, 'get_current_user', lambda: None),
            (web, '_multiuser_session_from_request', lambda: self.owner[0]),
        ]
        for target, name, value in replacements:
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        protection = patch.dict(os.environ, {'SJFX_DESKTOP_FILE_PROTECTION': '0'})
        protection.start()
        self.addCleanup(protection.stop)
        scan = scan_directory(self.root)
        self.scan_id = self.storage.save_scan(scan, scan_id='archive-raw-owner',
                                             owner_id='archive-test-owner')
        self.files = {node['path']: node for node in web._walk_analysis_nodes(scan['tree'])
                      if node.get('kind') == 'file'}
        self.client = TestClient(web.app)  # No lifespan, Worker, socket or model.
        self.addCleanup(self.client.close)
        self.addCleanup(gc.collect)

    def get(self, path):
        return self.client.get('/api/document/' + self.scan_id + '/raw', params={'path': path})

    def database_state(self):
        with self.storage._connect() as conn:
            return tuple(conn.iterdump())

    def put_physical(self, name, **extra):
        import json
        path = self.root / name
        stat = path.stat()
        node = {**self.files[name], 'size': stat.st_size, 'modified_at_ns': stat.st_mtime_ns,
                'device': stat.st_dev, 'inode': stat.st_ino, **extra}
        with self.storage._connect() as conn:
            conn.execute('INSERT OR REPLACE INTO inventory_entries '
                         '(scan_id,node_path,parent_path,position,kind,payload) VALUES (?,?,?,0,?,?)',
                         (self.scan_id, name, '.', 'file', json.dumps(node)))
        return node

    def assert_recovery(self, path, status, body=None):
        before = self.database_state()
        response = self.get(path)
        self.assertEqual(response.status_code, status, response.text)
        if body is not None:
            self.assertEqual(response.content, body)
        elif status != 200:
            self.assertNotEqual(response.content, BODY)
        self.assertEqual(self.database_state(), before, 'raw recovery wrote database state')
        self.assertFalse(list(self.scratch.iterdir()), 'private snapshots leaked')
        return response

    def test_saved_physical_inventory_and_legacy_tree_return_exact_member(self):
        for name in ('bundle.zip', 'bundle.tar', 'bundle.tar.gz'):
            for durable in (False, True):
                with self.subTest(archive=name, durable=durable):
                    if durable:
                        self.put_physical(name)
                    response = self.assert_recovery(name + '::docs/member.txt', 200, BODY)
                    self.assertIn('member.txt', response.headers['content-disposition'])
                    self.assertNotEqual(response.content, (self.root / name).read_bytes())

    def test_missing_single_logical_record_is_recovered_without_writing(self):
        node = self.put_physical('bundle.zip')
        units = list(archive_member_units(self.root, node))
        self.storage.replace_logical_inventory_entries(self.scan_id, units)
        with self.storage._connect() as conn:
            conn.execute('DELETE FROM inventory_entries WHERE scan_id=? AND node_path=?',
                         (self.scan_id, 'bundle.zip::docs/member.txt'))
        self.assert_recovery('bundle.zip::docs/member.txt', 200, BODY)

    def test_unknown_unsafe_rewritten_and_sensitive_members_fail_closed(self):
        write_archive(self.root / 'unrecorded.zip')
        paths = [
            'bundle.zip::missing.txt', 'bundle.zip::docs\\member.txt',
            'bundle.zip::docs/../docs/member.txt', 'bundle.zip::docs//member.txt',
            'bundle.zip::docs/member.txt ', 'bundle.zip::../docs/member.txt',
            'bundle.zip::/docs/member.txt', 'bundle.zip::C:/docs/member.txt',
            'bundle.zip::docs/member.txt::extra', 'plain.txt::docs/member.txt',
            'unrecorded.zip::docs/member.txt', 'bundle.zip::.env',
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assert_recovery(path, 404)

    def test_wrong_owner_cannot_reach_container_discovery(self):
        self.owner[0] = {'owner_id': 'foreign-owner', 'role': 'user'}
        with patch.object(web, '_discover_raw_archive_member',
                          side_effect=AssertionError('foreign owner reached recovery')):
            self.assert_recovery('bundle.zip::docs/member.txt', 404)

    def test_saved_root_guard_precedes_member_discovery(self):
        with patch.object(web._file_protection, 'guard_root', side_effect=ValueError('root denied')):
            with patch.object(web, '_discover_raw_archive_member',
                              side_effect=AssertionError('denied root reached recovery')):
                self.assert_recovery('bundle.zip::docs/member.txt', 404)

    def test_duplicate_exact_and_canonical_member_names_fail_closed(self):
        for second in ('docs/member.txt', './docs/member.txt'):
            with self.subTest(second=second):
                write_archive(self.root / 'bundle.zip',
                              [('docs/member.txt', BODY), (second, b'other')])
                self.put_physical('bundle.zip')
                self.assert_recovery('bundle.zip::docs/member.txt', 404)

    def test_complete_entry_budget_is_checked_before_serving_small_match(self):
        with patch.object(web.Config, 'MAX_LOGICAL_UNITS_PER_CONTAINER', 1):
            self.assert_recovery('bundle.zip::docs/member.txt', 404)

    def test_marked_changed_or_restricted_physical_container_remains_unreadable(self):
        for restriction, value, status in [('source_changed', True, 409),
                                           ('sensitive', True, 404),
                                           ('content_analysis_allowed', False, 404),
                                           ('content_policy', 'metadata_only_sensitive', 404)]:
            with self.subTest(restriction=restriction):
                self.put_physical('bundle.zip', **{restriction: value})
                self.assert_recovery('bundle.zip::docs/member.txt', status)

    def test_replacement_identity_is_rejected_before_and_after_discovery(self):
        path = self.root / 'bundle.zip'
        for after_discovery in (False, True):
            with self.subTest(after_discovery=after_discovery):
                write_archive(path)
                self.put_physical('bundle.zip')
                stat = path.stat()
                def replace_source():
                    replacement = self.root / 'replacement'
                    replacement.write_bytes(path.read_bytes().replace(b'Exact', b'Other'))
                    self.assertEqual(replacement.stat().st_size, stat.st_size)
                    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                    os.replace(replacement, path)
                if after_discovery:
                    original = web.iter_logical_units
                    def swapped(*args, **kwargs):
                        self.assertNotEqual(kwargs['physical_source_overrides']['bundle.zip'], path)
                        yield from original(*args, **kwargs)
                        replace_source()
                    with patch.object(web, 'iter_logical_units', swapped):
                        self.assert_recovery('bundle.zip::docs/member.txt', 409)
                else:
                    replace_source()
                    self.assert_recovery('bundle.zip::docs/member.txt', 409)

    def test_inventory_override_reads_private_bytes_and_preserves_stable_identity(self):
        physical = self.put_physical('bundle.zip')
        snapshot = Path(self.sandbox.name) / 'private-source.bin'
        snapshot.write_bytes((self.root / 'bundle.zip').read_bytes())
        (self.root / 'bundle.zip').write_bytes(b'live path is no longer an archive')
        units = list(archive_member_units(self.root, physical, physical_source_override=snapshot))
        unit = next(unit for unit in units if unit['member_name'] == 'docs/member.txt')
        original_path = 'bundle.zip::docs/member.txt'
        self.assertEqual(unit['path'], original_path)
        self.assertEqual(unit['id'], hashlib.sha1(original_path.encode()).hexdigest()[:16])
        for logical, original in [('container_path', 'path'), ('container_size', 'size'),
                                  ('container_modified_at_ns', 'modified_at_ns'),
                                  ('container_device', 'device'), ('container_inode', 'inode')]:
            self.assertEqual(unit[logical], physical[original])
        self.assertEqual(unit['size'], len(BODY))

    def test_bounded_stage_call_inherits_context_and_original_failure(self):
        marker = ContextVar('archive-bounded-stage-test', default='missing')
        token = marker.set('owned-context')
        try:
            self.assertEqual(package_analysis._call_with_timeout(lambda: marker.get(), 1), 'owned-context')
            expected = ValueError('original failure')
            def fail():
                self.assertEqual(marker.get(), 'owned-context')
                raise expected
            with self.assertRaises(ValueError) as caught:
                package_analysis._call_with_timeout(fail, 1)
            self.assertIs(caught.exception, expected)
        finally:
            marker.reset(token)


if __name__ == '__main__':
    unittest.main()
