"""Private uploaded scans: lexical preflight and real link/reparse checks."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sjfx_multiuser.scan_paths import authorize_uploaded_scan_path


class UploadedScanPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sjfx-uploaded-scan-')
        self.base = Path(self.temporary.name)
        self.root = self.base / 'outputs' / '_uploads'
        self.private = self.root / 'assembled' / 'owner_a' / 'session_1'
        self.private.mkdir(parents=True)
        self.outside = self.base / 'external'
        self.outside.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def check(self, path, owner='owner_a', filesystem=False):
        return authorize_uploaded_scan_path(path, self.root, owner, check_filesystem=filesystem)

    def directory_link(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name == 'nt':
                result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                        capture_output=True, check=False)
                if result.returncode == 0:
                    return
            self.skipTest('platform cannot create symlink or junction')

    def test_own_session_and_nested_path_authorized(self):
        nested = self.private / '资料' / '子目录'
        nested.mkdir(parents=True)
        for path in (self.private, nested):
            with self.subTest(path=path):
                self.assertEqual(self.check(path), path)
                self.assertEqual(self.check(path, filesystem=True), path.resolve())

    def test_outside_and_lookalike_roots_are_external(self):
        paths = (self.outside, self.base / 'outputs' / '_uploads_other' / 'assembled' / 'owner_a' / 'session_1',
                 self.root.parent / '_uploads2')
        for path in paths:
            with self.subTest(path=path):
                self.assertIsNone(self.check(path))
                self.assertIsNone(self.check(path, owner=None))

    def test_all_upload_ancestors_are_rejected_before_broad_allowed_roots(self):
        for path in self.root.parents:
            for filesystem in (False, True):
                for owner in ('owner_a', None):
                    with self.subTest(path=path, filesystem=filesystem, owner=owner):
                        with self.assertRaisesRegex(ValueError, '请选择具体资料目录'):
                            self.check(path, owner=owner, filesystem=filesystem)

    def test_upload_tree_is_reserved_before_broad_allowed_roots(self):
        paths = (self.root, self.root / 'assembled', self.root / 'assembled' / 'owner_a',
                 self.root / 'owner_a' / 'session_1', self.root / '.locks' / 'owner_a',
                 self.root / 'assembled' / 'owner_b' / 'session_1')
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.check(path)

    def test_no_owner_or_other_owner_cannot_read_private_outputs(self):
        for owner in (None, '', 'owner_b', 'admin', 'configured_fallback'):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.check(self.private, owner=owner)

    def test_invalid_owner_session_and_windows_aliases_rejected(self):
        for owner in ('..', 'a/b', 'owner_a.', 'CON', 'a:stream', 'invalid owner'):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.check(self.private, owner=owner)
        for session in ('bad session', 'session_1.', 'session_1 ', 'COM1', 'a:stream', 'a~1'):
            path = str(self.root / 'assembled' / 'owner_a') + os.sep + session
            with self.subTest(session=session), self.assertRaises(ValueError):
                self.check(path)
        for child in ('a:stream', 'NUL.txt', 'trailing.', 'trailing ', 'bad|name'):
            with self.subTest(child=child), self.assertRaises(ValueError):
                self.check(str(self.private) + os.sep + child)

    def test_lexical_normalization_cannot_hide_other_owner(self):
        self.assertEqual(self.check(self.private / 'child' / '..'), self.private)
        cross = self.private / '..' / '..' / 'owner_b' / 'other_session'
        with self.assertRaises(ValueError):
            self.check(cross)
        staging = self.private / '..' / '..' / '..' / 'owner_a' / 'session_1'
        with self.assertRaises(ValueError):
            self.check(staging)

    def test_fast_check_performs_no_filesystem_access(self):
        with (
            patch('sjfx_multiuser.scan_paths.os.lstat', side_effect=AssertionError('filesystem access')),
            patch('sjfx_multiuser.scan_paths.os.path.realpath', side_effect=AssertionError('resolve access')),
        ):
            self.assertEqual(self.check(self.private), self.private)
            self.assertIsNone(self.check(self.outside))
            with self.assertRaises(ValueError):
                self.check(self.root / 'assembled' / 'owner_b' / 'session_1')
            with self.assertRaisesRegex(ValueError, '请选择具体资料目录'):
                self.check(self.root.parent)

    def test_manifest_cleanup_does_not_revoke_assembled_output(self):
        # There is no staging session or session.json in this fixture.
        (self.private / 'document.txt').write_text('preserved output', encoding='utf-8')
        self.assertFalse((self.root / 'owner_a' / 'session_1').exists())
        self.assertEqual(self.check(self.private, filesystem=True), self.private.resolve())

    def test_external_link_into_private_tree_requires_resolved_check(self):
        public_link = self.outside / 'private_alias'
        self.directory_link(public_link, self.private)
        self.assertIsNone(self.check(public_link))
        resolved = public_link.resolve()
        self.assertEqual(self.check(resolved, filesystem=True), self.private.resolve())
        for owner in (None, 'owner_b'):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.check(resolved, owner=owner, filesystem=True)

    def test_external_link_to_staging_is_rejected_after_resolution(self):
        staging = self.root / 'owner_a' / 'session_1'
        staging.mkdir(parents=True)
        link = self.outside / 'staging_alias'
        self.directory_link(link, staging)
        self.assertIsNone(self.check(link))
        with self.assertRaises(ValueError):
            self.check(link.resolve(), filesystem=True)

    def test_external_link_to_upload_ancestor_is_rejected_after_resolution(self):
        link = self.outside / 'outputs_alias'
        self.directory_link(link, self.root.parent)
        self.assertIsNone(self.check(link))
        self.assertIsNone(self.check(link, filesystem=True))
        for filesystem in (False, True):
            with self.subTest(filesystem=filesystem):
                with self.assertRaisesRegex(ValueError, '请选择具体资料目录'):
                    self.check(link.resolve(), filesystem=filesystem)

    def test_app_resolver_rejects_ancestors_and_external_links_despite_allowlist(self):
        import app as web

        link = self.outside / 'outputs_alias'
        self.directory_link(link, self.root.parent)
        with (
            patch.object(web.Config, 'OUTPUT_DIR', self.root.parent),
            patch.object(web.Config, 'SCAN_ALLOWED_ROOTS', (self.base,)),
        ):
            for path in (self.root.parent, self.base, link):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, '请选择具体资料目录'):
                        web._resolve_allowed_scan_root(str(path), owner_id='owner_a')
            with self.assertRaises(ValueError):
                web._resolve_allowed_scan_root(str(self.root), owner_id='owner_a')
            self.assertEqual(web._resolve_allowed_scan_root(str(self.private), owner_id='owner_a'),
                             self.private.resolve())
            self.assertEqual(web._resolve_allowed_scan_root(str(self.outside), owner_id='owner_a'),
                             self.outside.resolve())

    def test_private_link_outside_is_rejected_with_filesystem_check(self):
        link = self.private / 'escape'
        self.directory_link(link, self.outside)
        self.assertEqual(self.check(link), link)
        with self.assertRaises(ValueError):
            self.check(link, filesystem=True)

    def test_private_link_even_to_own_session_is_rejected(self):
        sibling = self.root / 'assembled' / 'owner_a' / 'session_2'
        sibling.mkdir()
        link = self.private / 'alias'
        self.directory_link(link, sibling)
        with self.assertRaises(ValueError):
            self.check(link, filesystem=True)

    def test_reserved_ancestors_cannot_be_reparse_points(self):
        for level in ('assembled', 'owner', 'session', 'root'):
            with self.subTest(level=level):
                base = self.base / ('reparse_' + level)
                root = base / '_uploads'
                assembled = root / 'assembled'
                owner = assembled / 'owner_a'
                session = owner / 'session_1'
                source = {'root': root, 'assembled': assembled, 'owner': owner, 'session': session}[level]
                source.parent.mkdir(parents=True, exist_ok=True)
                target = self.outside / level
                target.mkdir()
                self.directory_link(source, target)
                path = session / 'child'
                self.assertEqual(authorize_uploaded_scan_path(path, root, 'owner_a'), path)
                with self.assertRaises(ValueError):
                    authorize_uploaded_scan_path(path, root, 'owner_a', check_filesystem=True)

    def test_ancestor_above_upload_root_is_also_checked(self):
        root = self.base / 'linked_outputs' / '_uploads'
        self.directory_link(root.parent, self.outside)
        path = root / 'assembled' / 'owner_a' / 'session_1'
        self.assertEqual(authorize_uploaded_scan_path(path, root, 'owner_a'), path)
        with self.assertRaises(ValueError):
            authorize_uploaded_scan_path(path, root, 'owner_a', check_filesystem=True)

    @unittest.skipUnless(os.name == 'nt', 'Windows spelling contract')
    def test_windows_case_and_extended_path_spellings_are_reserved(self):
        upper = str(self.private).upper()
        self.assertEqual(self.check(upper), Path(os.path.abspath(upper)))
        self.assertEqual(self.check('\\\\?\\' + str(self.private)), self.private)
        cross = '\\\\?\\' + str(self.root / 'assembled' / 'owner_b' / 'session_1')
        with self.assertRaises(ValueError):
            self.check(cross)

    def test_malformed_raw_path_raises_clear_error(self):
        for path in (None, '', 'bad\x00path'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.check(path)


if __name__ == '__main__':
    unittest.main()
