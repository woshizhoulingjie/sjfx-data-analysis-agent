"""Bounded indexing, atomic publication and language-quality contracts."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.large_qa_index import LargeQAIndex
from services.large_package_semantic import evidence_quality, garble_ratio, _is_allowed
from services.large_package_catalog_import import _clean_paths, SelectionError


def row(path, text='Research evidence about energy and supply chain', **kwargs):
    value = dict(path=path, size=100, modified_at_ns=1, category='energy',
                 local_summary=json.dumps({'summary': text}), deep_summary='{}', vector=b'1234')
    value.update(kwargs)
    return value


class BoundedIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = LargeQAIndex('test-package', Path(self.tmp.name), create=True)
        self.package = {'id': 'test-package'}

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self):
        with self.index.connect() as c:
            return {table: [tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY 1')]
                    for table in ('documents', 'chunks', 'meta', 'topic_nodes')}

    def test_generator_and_duplicate_semantics(self):
        rows = [row('a.txt'), row('b.txt'), row('a.txt', 'Last duplicate wins')]
        self.assertEqual(self.index.sync_rows(iter(rows), self.package), 2)
        with self.index.connect() as c:
            self.assertIn('Last duplicate', c.execute(
                'SELECT summary FROM documents WHERE path=?', ('a.txt',)).fetchone()[0])
            self.assertEqual([r[0] for r in c.execute('SELECT path FROM chunks ORDER BY id')], ['a.txt', 'b.txt'])
            self.assertEqual(self.index.meta(c)['source_files'], 2)
        self.assertEqual(self.index.sync_rows(iter(rows), self.package), 0)
        self.assertFalse(list(self.index.path.parent.glob('sync-*')))

    def test_delete_more_than_one_batch_and_empty_input(self):
        self.index.sync_rows((row('%04d.txt' % i) for i in range(390)), self.package)
        self.assertEqual(self.index.sync_rows([row('0389.txt')], self.package), 389)
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM documents').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM search WHERE search MATCH ?', ('energy',)).fetchone()[0], 1)
        self.assertEqual(self.index.sync_rows([], self.package), 1)
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)
            self.assertEqual(self.index.meta(c)['source_files'], 0)

    def test_staging_failure_does_not_publish(self):
        self.index.sync_rows([row('before.txt')], self.package)
        before = self.snapshot()
        def broken():
            yield row('after.txt')
            raise RuntimeError('source interrupted')
        with self.assertRaisesRegex(RuntimeError, 'source interrupted'):
            self.index.sync_rows(broken(), self.package)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.index.path.parent.glob('sync-*')))

    def test_publication_failure_rolls_back_deletions(self):
        self.index.sync_rows([row('before.txt')], self.package)
        before = self.snapshot()
        with patch.object(self.index, '_add_chunk', side_effect=RuntimeError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                self.index.sync_rows([row('after.txt')], self.package)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.index.path.parent.glob('sync-*')))

    def test_readers_keep_previous_generation_until_commit(self):
        self.index.sync_rows([row('before.txt')], self.package)
        with self.index.connect() as reader:
            old = list(reader.execute('SELECT path FROM documents'))
            self.index.sync_rows([row('after.txt')], self.package)
            self.assertEqual(list(reader.execute('SELECT path FROM documents')), old)
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT path FROM documents').fetchone()[0], 'after.txt')

    def test_originals_priority_and_sensitive_boundary(self):
        original = row('a.txt')
        self.index.sync_rows([original], self.package)
        with self.index.connect(write=True) as c:
            version = c.execute('SELECT source_version FROM documents').fetchone()[0]
            c.execute('UPDATE documents SET priority=8')
        self.index.put_original('a.txt', version, [{'text': 'Detailed original research evidence'}], True)
        self.index.sync_rows([row('a.txt', 'A revised summary')], self.package)
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT priority,parse_complete FROM documents').fetchone()[:], (8, 1))
            self.assertEqual(c.execute(
                "SELECT count(*) FROM chunks WHERE kind='original'").fetchone()[0], 1)
        self.index.sync_rows([row('a.txt', sensitive=1)], self.package)
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT summary,parse_status FROM documents').fetchone()[:], ('', 'restricted'))
            self.assertEqual(c.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)
    def test_many_original_chunks_are_fully_removed_from_search(self):
        self.index.sync_rows([row('a.txt')], self.package)
        with self.index.connect() as c:
            version = c.execute('SELECT source_version FROM documents').fetchone()[0]
        self.index.put_original('a.txt', version,
            [{'text': 'distinctiveoldtoken ' * 140} for _ in range(400)], True)
        self.index.sync_rows([], self.package)
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)
            self.assertEqual(c.execute(
                "SELECT count(*) FROM search WHERE search MATCH 'distinctiveoldtoken'").fetchone()[0], 0)

    def test_sync_store_releases_snapshot_before_publish_and_closes_source(self):
        db = Path(self.tmp.name) / 'source.db'
        source = sqlite3.connect(str(db)); source.row_factory = sqlite3.Row
        source.executescript('''
          CREATE TABLE packages(id TEXT PRIMARY KEY,owner_id TEXT,root_path TEXT);
          INSERT INTO packages VALUES('test-package','owner-snapshot','/snapshot-root');
          CREATE TABLE files(package_id TEXT,path TEXT,local_summary TEXT,deep_summary TEXT,
            name TEXT,category TEXT,size INTEGER DEFAULT 0,modified_at_ns INTEGER DEFAULT 0,sha256 TEXT,sensitive INTEGER DEFAULT 0);
          CREATE TABLE large_catalog_centers(package_id TEXT,model TEXT);
          CREATE TABLE large_catalog_members(package_id TEXT,path TEXT,group_id TEXT,vector BLOB);
          CREATE TABLE large_catalog_groups(package_id TEXT,id TEXT,model TEXT);
          CREATE TABLE lp_catalog_labels(package_id TEXT,group_id TEXT,name TEXT);
          INSERT INTO files(package_id,path,local_summary,deep_summary) VALUES('test-package','a.txt','{}','{}');
        '''); source.commit()
        class Store:
            def get(_, package):
                return self.package
            def _connect(_):
                return source
        original = self.index._publish_snapshot
        def checked(*args):
            self.assertFalse(source.in_transaction)
            return original(*args)
        with patch.object(self.index, '_publish_snapshot', side_effect=checked):
            self.assertEqual(self.index.sync_store(Store()), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            source.execute('SELECT 1')
        with self.index.connect() as c:
            package = self.index.meta(c)['package']
            self.assertEqual(package['owner_id'], 'owner-snapshot')
            self.assertEqual(package['root_path'], '/snapshot-root')
        # Exercise the exact owning context manager used by the production
        # sidecar, not just the raw connection used by the ordinary store.
        from scripts.large_qa_worker import ReadOnlyStore
        real_store = ReadOnlyStore(db)
        with patch.object(real_store, 'get', return_value=self.package):
            self.assertEqual(self.index.sync_store(real_store), 0)


class LanguageAndSelectionTests(unittest.TestCase):
    def test_chinese_english_thai_are_readable(self):
        for text in ('รายงานการประชุมเกี่ยวกับการพัฒนาโครงการและการจัดการข้อมูล',
                     '项目会议讨论供应链风险与后续研究方向。',
                     'The meeting reviewed project evidence and future research directions.'):
            with self.subTest(language=text[:8]):
                self.assertEqual(garble_ratio(text * 8), 0)
                self.assertEqual(evidence_quality({'text': text * 8}), (True, ''))

    def test_unassigned_thai_private_use_and_replacement_remain_rejected(self):
        for code in (0x0E00, 0x0E3B, 0x0E3E, 0x0E5C, 0x0E7F, 0xE000, 0xFFFD):
            self.assertFalse(_is_allowed(code))
            self.assertEqual(evidence_quality({'text': chr(code) * 100}), (False, 'garbled'))
        self.assertEqual(evidence_quality({'text': 'รายงาน'}), (False, 'too_little_text'))

    def test_large_selection_preserves_order_and_validation(self):
        paths = ['folder/%05d.txt' % i for i in range(50000)]
        self.assertEqual(_clean_paths(paths + paths[:100]), paths)
        self.assertEqual(_clean_paths([' a/b/ ', 'a\\b', '中文/泰文.txt']), ['a/b', '中文/泰文.txt'])
        for path in ('/etc/passwd', '../secret', 'a/../../secret', 'C:/secret'):
            with self.assertRaises(SelectionError):
                _clean_paths([path])
