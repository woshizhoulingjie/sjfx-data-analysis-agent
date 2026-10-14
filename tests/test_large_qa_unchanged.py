"""Full verification fast path: no timestamp shortcuts or lost evidence."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from services.large_qa_index import LargeQAIndex, _fingerprint_rows
from scripts.large_qa_worker import ReadOnlyStore


class UnchangedSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base/'source.db'
        # Native SQLite contexts commit/rollback but do not own the handle.
        with closing(sqlite3.connect(str(self.db))) as c, c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE packages(id TEXT PRIMARY KEY,root_path TEXT,owner_id TEXT,updated_at TEXT);
                CREATE TABLE files(package_id TEXT,path TEXT,name TEXT,category TEXT,size INTEGER,
                  modified_at_ns INTEGER,sha256 TEXT,sensitive INTEGER,local_summary TEXT,deep_summary TEXT);
                CREATE TABLE large_catalog_centers(package_id TEXT,model TEXT);
                CREATE TABLE large_catalog_members(package_id TEXT,path TEXT,group_id TEXT,vector BLOB);
                CREATE TABLE large_catalog_groups(package_id TEXT,id TEXT,model TEXT);
                CREATE TABLE lp_catalog_labels(package_id TEXT,group_id TEXT,name TEXT);
                INSERT INTO packages VALUES('p','/original','owner','1');
                INSERT INTO files VALUES('p','a.txt','a.txt','energy',100,1,'sha',0,
                  '{"summary":"Evidence about energy and supply chains"}','{}');
                INSERT INTO large_catalog_centers VALUES('p','model1');
                INSERT INTO large_catalog_members VALUES('p','a.txt','g1',X'01020304');
                INSERT INTO large_catalog_groups VALUES('p','g1','model1');
                INSERT INTO lp_catalog_labels VALUES('p','g1','Energy research');
            ''')
        self.store = ReadOnlyStore(self.db)
        self.index = LargeQAIndex('p', self.base/'indexes', create=True)

    def execute(self, sql, args=()):
        with closing(sqlite3.connect(str(self.db))) as c, c:
            c.execute(sql, args)

    def meta(self):
        with self.index.connect() as c:
            return self.index.meta(c)

    def documents(self):
        with self.index.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM documents ORDER BY path')]

    def test_unchanged_skips_normalization_staging_and_publish(self):
        self.assertEqual(self.index.sync_store(self.store), 1)
        before = self.meta()
        with patch.object(self.index, 'sync_rows', side_effect=AssertionError('full sync')):
            with patch('services.large_qa_index.summary_text', side_effect=AssertionError('normalize')):
                with patch('services.large_qa_index.tempfile.TemporaryDirectory', side_effect=AssertionError('stage')):
                    self.assertEqual(self.index.sync_store(self.store), 0)
        self.assertEqual(self.meta()['generation'], before['generation'])
        self.assertEqual(self.meta()['source_signature'], before['source_signature'])
        self.assertGreaterEqual(self.meta()['synced_at'], before['synced_at'])

    def test_fingerprint_survives_new_instance(self):
        self.index.sync_store(self.store)
        index = LargeQAIndex('p', self.base/'indexes')
        with patch.object(index, 'sync_rows', side_effect=AssertionError('full sync')):
            self.assertEqual(index.sync_store(ReadOnlyStore(self.db)), 0)

    def test_all_selected_fields_trigger_reverification(self):
        self.index.sync_store(self.store)
        mutations = [
            ("UPDATE files SET name='renamed.txt'", ()),
            ("UPDATE files SET category='health'", ()),
            ("UPDATE files SET size=101", ()),
            ("UPDATE files SET modified_at_ns=2", ()),
            ("UPDATE files SET sha256='new'", ()),
            ("UPDATE files SET sensitive=1", ()),
            ("UPDATE files SET sensitive=0", ()),
            ("UPDATE files SET local_summary=?", (json.dumps({'summary':'中文证据：供应链研究'}),)),
            ("UPDATE files SET deep_summary=?", (json.dumps({'summary':'รายงานการประชุมเกี่ยวกับการพัฒนาโครงการ'}),)),
            ("UPDATE large_catalog_members SET vector=X'04030201'", ()),
            ("UPDATE lp_catalog_labels SET name='Specific energy evidence'", ()),
            ("UPDATE large_catalog_groups SET model='model2'", ()),
            ("UPDATE large_catalog_centers SET model='model2'", ()),
            ("UPDATE large_catalog_members SET group_id='g2'", ()),
            ("UPDATE files SET path='b.txt'", ()),
        ]
        for sql, args in mutations:
            with self.subTest(sql=sql):
                old = self.meta()['source_signature']
                self.execute(sql, args)
                with patch.object(self.index, 'sync_rows', wraps=self.index.sync_rows) as sync:
                    self.index.sync_store(self.store)
                    sync.assert_called_once()
                self.assertNotEqual(self.meta()['source_signature'], old)
                self.assertEqual(self.index.sync_store(self.store), 0)

    def test_same_size_same_mtime_edit_detected(self):
        self.index.sync_store(self.store)
        self.execute('UPDATE files SET local_summary=?', (json.dumps({'summary':'Changed factual evidence'}),))
        self.assertEqual(self.index.sync_store(self.store), 1)
        self.assertEqual(self.documents()[0]['summary'], 'summary: Changed factual evidence')

    def test_active_ingestion_keeps_one_pass_without_fingerprint_overhead(self):
        self.index.sync_store(self.store)
        self.execute('ALTER TABLE packages ADD COLUMN status TEXT')
        for status in ('running', 'queued', 'processing', 'unknown-new-state'):
            with self.subTest(status=status):
                self.execute('UPDATE packages SET status=?', (status,))
                self.execute('UPDATE files SET local_summary=?', (json.dumps({'summary':status}),))
                with patch('services.large_qa_index._fingerprint_rows', side_effect=AssertionError('extra scan')):
                    self.assertEqual(self.index.sync_store(self.store), 1)
                self.assertIsNone(self.meta()['source_signature'])
                self.assertEqual(self.documents()[0]['summary'], 'summary: '+status)
        self.execute("UPDATE packages SET status='completed'")
        self.assertEqual(self.index.sync_store(self.store), 0)
        self.assertIsInstance(self.meta()['source_signature'], dict)
        with patch.object(self.index, 'sync_rows', side_effect=AssertionError('full sync')):
            self.assertEqual(self.index.sync_store(self.store), 0)

    def test_package_permissions_and_root_refresh(self):
        self.index.sync_store(self.store)
        self.execute("UPDATE packages SET root_path='/new', owner_id='other'")
        with patch.object(self.index, 'sync_rows', side_effect=AssertionError('full sync')):
            self.assertEqual(self.index.sync_store(self.store), 0)
        self.assertEqual(self.meta()['package'], dict(id='p',root_path='/new',owner_id='other',updated_at='1'))

    def test_announced_package_change_uses_one_verification_pass(self):
        self.index.sync_store(self.store)
        self.execute("UPDATE packages SET updated_at='2'")
        self.execute('UPDATE files SET local_summary=?', (json.dumps({'summary':'Newly ingested evidence'}),))
        with patch('services.large_qa_index._fingerprint_rows', wraps=_fingerprint_rows) as fingerprint:
            self.assertEqual(self.index.sync_store(self.store), 1)
            fingerprint.assert_called_once()
        self.assertEqual(self.documents()[0]['summary'], 'summary: Newly ingested evidence')

    def test_unannounced_change_reuses_same_snapshot_fingerprint(self):
        self.index.sync_store(self.store)
        self.execute('UPDATE files SET local_summary=?', (json.dumps({'summary':'Unannounced evidence'}),))
        with patch('services.large_qa_index._fingerprint_rows', wraps=_fingerprint_rows) as fingerprint:
            self.assertEqual(self.index.sync_store(self.store), 1)
            fingerprint.assert_called_once()

    def test_removed_package_never_uses_fingerprint(self):
        self.index.sync_store(self.store)
        self.execute('DELETE FROM packages')
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            self.index.sync_store(self.store)

    def test_delete_empty_and_reinsert(self):
        self.index.sync_store(self.store)
        self.execute('DELETE FROM files')
        self.assertEqual(self.index.sync_store(self.store), 1)
        self.assertEqual(self.meta()['source_files'], 0)
        with patch.object(self.index, 'sync_rows', side_effect=AssertionError('full sync')):
            self.assertEqual(self.index.sync_store(self.store), 0)
        self.execute("INSERT INTO files VALUES('p','new.txt','new.txt','energy',0,0,'',0,'{}','{}')")
        self.assertEqual(self.index.sync_store(self.store), 1)
        self.assertEqual(self.documents()[0]['path'], 'new.txt')

    def test_original_evidence_and_generation_preserved(self):
        self.index.sync_store(self.store)
        self.index.put_original('a.txt', self.documents()[0]['source_version'],
                                [{'text':'Original evidence from source file'}], True)
        before = self.meta()
        self.index.sync_store(self.store)
        self.assertEqual(self.meta()['generation'], before['generation'])
        with self.index.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM chunks WHERE kind='original'").fetchone()[0], 1)

    def test_sensitive_change_removes_original_evidence(self):
        self.index.sync_store(self.store)
        self.index.put_original('a.txt', self.documents()[0]['source_version'],
                                [{'text':'Private original evidence'}], True)
        self.execute('UPDATE files SET sensitive=1')
        self.index.sync_store(self.store)
        self.assertEqual(self.documents()[0]['parse_status'], 'restricted')
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)

    def test_direct_sync_invalidates_fast_path(self):
        self.index.sync_store(self.store)
        self.index.sync_rows([], self.store.get('p'))
        self.assertIsNone(self.meta()['source_signature'])
        self.assertEqual(self.index.sync_store(self.store), 1)

    def test_legacy_writer_generation_invalidates_cached_signature(self):
        self.index.sync_store(self.store)
        old_stamp = self.meta()['source_signature']
        self.index.sync_rows([], self.store.get('p'))
        # Legacy binaries don't know the new metadata key and leave it behind.
        with self.index.connect(write=True) as c:
            self.index.set_meta(c, 'source_signature', old_stamp)
        self.assertEqual(self.index.sync_store(self.store), 1)
        self.assertEqual(self.documents()[0]['path'], 'a.txt')

    def test_legacy_string_fingerprint_is_not_trusted(self):
        self.index.sync_store(self.store)
        with self.index.connect(write=True) as c:
            self.index.set_meta(c, 'source_signature', 'old-format')
        with patch.object(self.index, 'sync_rows', wraps=self.index.sync_rows) as sync:
            self.index.sync_store(self.store)
            sync.assert_called_once()

    def test_publication_failure_keeps_old_fingerprint(self):
        self.index.sync_store(self.store)
        before, docs = self.meta(), self.documents()
        self.execute('UPDATE files SET local_summary=?', (json.dumps({'summary':'Replacement evidence'}),))
        with patch.object(self.index, '_add_chunk', side_effect=RuntimeError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                self.index.sync_store(self.store)
        self.assertEqual(self.meta(), before)
        self.assertEqual(self.documents(), docs)
        self.assertFalse(list(self.index.path.parent.glob('sync-*')))
        self.assertEqual(self.index.sync_store(self.store), 1)

    def test_concurrent_publisher_forces_fresh_source_snapshot(self):
        self.index.sync_store(self.store)
        original = self.index._touch_unchanged
        def competing(signature, package, model):
            self.index.sync_rows([], package)
            self.execute('UPDATE files SET local_summary=?', (json.dumps({'summary':'Newest committed source'}),))
            return original(signature, package, model)
        with patch.object(self.index, '_touch_unchanged', side_effect=competing):
            self.assertEqual(self.index.sync_store(self.store), 1)
        self.assertEqual(self.documents()[0]['summary'], 'summary: Newest committed source')

    def test_source_snapshot_released_before_fast_touch(self):
        self.index.sync_store(self.store)
        source = sqlite3.connect(str(self.db)); source.row_factory = sqlite3.Row
        original = self.index._touch_unchanged
        def checked(*args):
            self.assertFalse(source.in_transaction)
            return original(*args)
        with patch.object(self.store, '_connect', return_value=source):
            with patch.object(self.store, 'get', return_value={'id':'p'}):
                with patch.object(self.index, '_touch_unchanged', side_effect=checked):
                    self.index.sync_store(self.store)
        with self.assertRaises(sqlite3.ProgrammingError):
            source.execute('SELECT 1')

    def test_full_membership_not_limited_to_first_batch(self):
        with closing(sqlite3.connect(str(self.db))) as c, c:
            c.executemany("INSERT INTO files VALUES('p',?,?,'energy',0,0,'',0,'{}','{}')",
                          [('file-%04d.txt'%i,'file-%04d.txt'%i) for i in range(400)])
        self.assertEqual(self.index.sync_store(self.store), 401)
        self.execute("DELETE FROM files WHERE path='file-0399.txt'")
        self.assertEqual(self.index.sync_store(self.store), 1)
        self.assertEqual(self.meta()['source_files'], 400)


class FingerprintTests(unittest.TestCase):
    def signature(self, rows, model='model'):
        state = {}
        list(_fingerprint_rows(rows, model, state))
        return state['signature']

    def test_types_lengths_order_and_model_are_distinct(self):
        samples = [[('ab','c')], [('a','bc')], [('a',),('bc',)], [('bc',),('a',)],
                   [(None,)], [('',)], [(b'',)], [(0,)], [('0',)], [(0.0,)], [], [()]]
        self.assertEqual(len({self.signature(r) for r in samples}), len(samples))
        self.assertNotEqual(self.signature([], 'model1'), self.signature([], 'model2'))

    def test_incomplete_iteration_never_exposes_signature(self):
        state = {}
        iterator = _fingerprint_rows(iter([('first',), ('second',)]), '', state)
        next(iterator)
        iterator.close()
        self.assertEqual(state, {})

    def test_read_error_does_not_expose_signature(self):
        state = {}
        def broken():
            yield ('first',)
            raise RuntimeError('source read failed')
        with self.assertRaisesRegex(RuntimeError, 'source read failed'):
            list(_fingerprint_rows(broken(), '', state))
        self.assertEqual(state, {})
