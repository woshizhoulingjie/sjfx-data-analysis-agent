"""Source token correctness and unchanged-source scan elimination."""
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_large_qa_unchanged as fixtures
from services import large_qa_changes as changes
from services.large_qa_index import LargeQAIndex


class ChangeTrackingTests(unittest.TestCase):
    setUp = fixtures.UnchangedSyncTests.setUp
    execute = fixtures.UnchangedSyncTests.execute
    meta = fixtures.UnchangedSyncTests.meta
    documents = fixtures.UnchangedSyncTests.documents

    def install(self):
        with sqlite3.connect(str(self.db)) as c:
            changes.install(c)

    def stamp(self, package='p'):
        with self.store._connect() as c:
            return changes.read_stamp(c, package)

    def prime(self):
        self.install()
        self.assertEqual(self.index.sync_store(self.store), 1)
        self.assertIsNotNone(self.meta()['source_change_stamp'])

    def test_unchanged_never_reads_file_rows(self):
        self.prime()
        queries = []
        source = sqlite3.connect(str(self.db))
        source.row_factory = sqlite3.Row
        source.set_trace_callback(queries.append)
        with patch.object(self.store, '_connect', return_value=source):
            with patch.object(self.index, 'sync_rows', side_effect=AssertionError('full sync')):
                with patch('services.large_qa_index._fingerprint_rows', side_effect=AssertionError('fingerprint scan')):
                    self.assertEqual(self.index.sync_store(self.store), 0)
        self.assertFalse(any('FROM FILES ' in q.upper() for q in queries), queries)

    def test_new_instance_reuses_persistent_stamp(self):
        self.prime()
        index = LargeQAIndex('p', self.base/'indexes')
        with patch.object(index, 'sync_rows', side_effect=AssertionError('full sync')):
            self.assertEqual(index.sync_store(self.store), 0)

    def test_first_enablement_verifies_then_skips_republishing(self):
        self.assertEqual(self.index.sync_store(self.store),1)
        self.assertIsNone(self.meta()['source_change_stamp'])
        self.install()
        with patch.object(self.index,'sync_rows',side_effect=AssertionError('unnecessary staging')):
            with patch('services.large_qa_index._fingerprint_rows',wraps=fixtures._fingerprint_rows) as verify:
                self.assertEqual(self.index.sync_store(self.store),0)
                verify.assert_called_once()
        self.assertIsNotNone(self.meta()['source_change_stamp'])
        with patch('services.large_qa_index._fingerprint_rows',side_effect=AssertionError('full scan')):
            self.assertEqual(self.index.sync_store(self.store),0)

    def test_all_relevant_fields_invalidate(self):
        self.prime()
        # Fixtures deliberately have no timestamp-based announcement.
        for table, fields in changes.FIELDS.items():
            for field in fields:
                if field in ('id', 'package_id', 'path', 'group_id'):
                    continue
                with self.subTest(table=table, field=field):
                    before = self.stamp()
                    value = b'new-vector' if field == 'vector' else (2 if field in ('size', 'modified_at_ns', 'sensitive') else 'new-'+field)
                    if field in ('local_summary', 'deep_summary'):
                        value = json.dumps({'summary':'中文 evidence ภาษาไทย '+field})
                    self.execute(f'UPDATE {table} SET {field}=?', (value,))
                    self.assertNotEqual(self.stamp(), before)
                    with patch.object(self.index, 'sync_rows', wraps=self.index.sync_rows) as sync:
                        self.index.sync_store(self.store)
                        sync.assert_called_once()

    def test_insert_delete_and_package_move_each_table(self):
        self.install()
        for table in changes.FIELDS:
            with self.subTest(table=table):
                key = 'id' if table == 'packages' else 'package_id'
                old_p, old_q = self.stamp(), self.stamp('q')
                self.execute(f'UPDATE {table} SET {key}=?', ('q',))
                self.assertNotEqual(old_p, self.stamp())
                self.assertNotEqual(old_q, self.stamp('q'))
                with sqlite3.connect(str(self.db)) as c:
                    row = c.execute(f'SELECT * FROM {table}').fetchone()
                    c.execute('BEGIN')
                    before = changes.read_stamp(c, 'q')
                    c.execute(f'DELETE FROM {table}')
                    after_delete = changes.read_stamp(c, 'q')
                    self.assertNotEqual(before, after_delete)
                    c.execute(f'INSERT INTO {table} VALUES('+','.join('?' for _ in row)+')', row)
                    self.assertNotEqual(after_delete, changes.read_stamp(c, 'q'))

    def test_irrelevant_progress_and_other_package_do_not_invalidate(self):
        self.prime()
        before = self.stamp()
        self.execute("UPDATE packages SET updated_at='progress'")
        self.execute("UPDATE files SET name=name")
        self.execute("INSERT INTO files VALUES('q','b','b','x',1,1,'q',0,'{}','{}')")
        self.assertEqual(before, self.stamp())
        with patch.object(self.index, 'sync_rows', side_effect=AssertionError('full sync')):
            self.assertEqual(self.index.sync_store(self.store), 0)
        self.assertEqual(self.meta()['package']['updated_at'], 'progress')

    def test_rollback_and_source_snapshot_are_consistent(self):
        self.prime()
        before = self.stamp()
        with sqlite3.connect(str(self.db)) as writer:
            writer.execute("UPDATE files SET name='rolled back'")
            self.assertNotEqual(changes.read_stamp(writer, 'p'), before)
            self.assertEqual(self.stamp(), before)
            writer.rollback()
        self.assertEqual(self.stamp(), before)
        with self.store._connect() as reader:
            old = changes.read_stamp(reader, 'p')
            self.execute("UPDATE files SET name='committed'")
            self.assertEqual(changes.read_stamp(reader, 'p'), old)
        self.assertNotEqual(self.stamp(), before)

    def test_replace_and_outer_ignore_still_invalidate(self):
        self.install()
        self.execute('CREATE UNIQUE INDEX file_identity ON files(package_id,path)')
        for verb in ('INSERT OR REPLACE', 'INSERT OR IGNORE'):
            with self.subTest(verb=verb):
                before = self.stamp()
                path = 'a.txt' if verb.endswith('REPLACE') else 'new.txt'
                self.execute(verb+" INTO files VALUES('p',?,?,'energy',1,1,'x',0,'{}','{}')", (path,path))
                self.assertNotEqual(self.stamp(), before)
        before = self.stamp()
        self.execute("INSERT OR IGNORE INTO files VALUES('p','new.txt','ignored','x',0,0,'',0,'{}','{}')")
        self.assertEqual(self.stamp(), before)

    def test_missing_tracking_falls_back(self):
        self.prime()
        name = next(iter(changes.definitions()))
        self.execute('DROP TRIGGER '+name)
        self.assertIsNone(self.stamp())
        with patch('services.large_qa_index._fingerprint_rows', wraps=fixtures._fingerprint_rows) as scan:
            self.assertEqual(self.index.sync_store(self.store), 0)
            scan.assert_called_once()
        self.assertIsNone(self.meta()['source_change_stamp'])

    def test_trigger_gap_and_recreated_definition_cannot_hide_change(self):
        self.prime()
        before = self.stamp()
        name = changes.PREFIX+'files_update'
        self.execute('DROP TRIGGER '+name)
        self.execute("UPDATE files SET local_summary='{}'")
        self.execute(changes.definitions()[name])
        self.assertNotEqual(self.stamp(), before)
        self.assertEqual(self.index.sync_store(self.store), 1)
        self.assertEqual(self.documents()[0]['summary'], '')

    def test_install_idempotent_and_repairs_epoch(self):
        self.install()
        before = self.stamp()
        self.install()
        self.assertEqual(self.stamp(), before)
        name = changes.PREFIX+'files_update'
        self.execute('DROP TRIGGER '+name)
        self.execute('CREATE TRIGGER '+name+' AFTER UPDATE ON files BEGIN SELECT 1; END')
        self.assertIsNone(self.stamp())
        self.install()
        self.assertNotEqual(before['epoch'], self.stamp()['epoch'])

    def test_global_unique_disables_replace_shortcut(self):
        self.install()
        self.execute('CREATE UNIQUE INDEX global_name ON files(name)')
        self.assertIsNone(self.stamp())
        with sqlite3.connect(str(self.db)) as c:
            self.assertFalse(changes.install(c))

    def test_case_insensitive_package_unique_disables_shortcut(self):
        self.install()
        self.execute('CREATE UNIQUE INDEX nocase_path ON files(package_id COLLATE NOCASE,path)')
        self.assertIsNone(self.stamp())

    def test_direct_target_sync_invalidates_token_even_without_changes(self):
        self.prime()
        with self.store._connect() as c:
            row = dict(c.execute('SELECT * FROM files').fetchone())
            row.update(group_id='g1',topic_name='Energy research',vector=bytes([1,2,3,4]))
        self.assertEqual(self.index.sync_rows([row],{'id':'p'},'model1'),0)
        self.assertIsNone(self.meta()['source_change_stamp'])
        with patch.object(self.index, 'sync_rows', wraps=self.index.sync_rows) as sync:
            self.index.sync_store(self.store)
            sync.assert_called_once()

    def test_old_binary_generation_invalidates_token(self):
        self.prime()
        with self.index.connect(write=True) as c:
            self.index.set_meta(c, 'summary_generation', 99)
        with patch.object(self.index, 'sync_rows', wraps=self.index.sync_rows) as sync:
            self.index.sync_store(self.store)
            sync.assert_called_once()

    def test_competing_publisher_refreshes_package_acl_and_content(self):
        self.prime()
        original = self.index._touch_tracked
        def competing(*args):
            self.index.sync_rows([], {'id':'p'})
            self.execute("UPDATE packages SET owner_id='new-owner'")
            self.execute('UPDATE files SET local_summary=?', (json.dumps({'summary':'new evidence'}),))
            return original(*args)
        with patch.object(self.index, '_touch_tracked', side_effect=competing):
            self.assertEqual(self.index.sync_store(self.store), 1)
        self.assertEqual(self.meta()['package']['owner_id'], 'new-owner')
        self.assertEqual(self.documents()[0]['summary'], 'summary: new evidence')

    def test_source_released_before_target_writer(self):
        self.prime()
        source = sqlite3.connect(str(self.db)); source.row_factory = sqlite3.Row
        original = self.index._touch_tracked
        def checked(*args):
            self.assertFalse(source.in_transaction)
            return original(*args)
        with patch.object(self.store, '_connect', return_value=source):
            with patch.object(self.index, '_touch_tracked', side_effect=checked):
                self.assertEqual(self.index.sync_store(self.store), 0)

    def test_publication_failure_does_not_publish_token(self):
        self.prime()
        before = self.meta()
        self.execute('UPDATE files SET local_summary=?', (json.dumps({'summary':'new evidence'}),))
        with patch.object(self.index, '_add_chunk', side_effect=RuntimeError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                self.index.sync_store(self.store)
        self.assertEqual(before, self.meta())
        self.assertEqual(self.index.sync_store(self.store), 1)

    def test_missing_source_never_reuses_token(self):
        self.prime()
        self.execute('DELETE FROM packages')
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            self.index.sync_store(self.store)

    def test_original_evidence_preserved_then_restricted(self):
        self.prime()
        doc = self.documents()[0]
        self.index.put_original(doc['path'],doc['source_version'],
            [{'text':'Original evidence 中文 ภาษาไทย','location':{'page':19}}],True)
        with self.index.connect() as c:
            original = [tuple(r) for r in c.execute("SELECT * FROM chunks WHERE kind='original'")]
        self.execute('UPDATE files SET local_summary=?',(json.dumps({'summary':'new summary'}),))
        self.assertEqual(self.index.sync_store(self.store),1)
        with self.index.connect() as c:
            self.assertEqual(original,[tuple(r) for r in c.execute("SELECT * FROM chunks WHERE kind='original'")])
        self.execute('UPDATE files SET sensitive=1')
        self.assertEqual(self.index.sync_store(self.store),1)
        self.assertEqual(self.documents()[0]['summary'],'')
        self.assertEqual(self.documents()[0]['parse_status'],'restricted')
        with self.index.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM chunks').fetchone()[0],0)

    def test_snapshot_restore_never_reuses_newer_content(self):
        self.prime()
        before = self.documents()
        saved = self.base/'saved.db'
        with sqlite3.connect(str(self.db)) as source, sqlite3.connect(str(saved)) as dest:
            source.backup(dest)
        self.execute('UPDATE files SET local_summary=?',(json.dumps({'summary':'newer evidence'}),))
        self.index.sync_store(self.store)
        self.assertNotEqual(self.documents(),before)
        with sqlite3.connect(str(saved)) as source, sqlite3.connect(str(self.db)) as dest:
            source.backup(dest)
        self.assertEqual(self.index.sync_store(self.store),1)
        self.assertEqual(self.documents(),before)
        # A repeated write after recovery obtains a new random token, not ABA.
        stamp = self.stamp()
        self.execute('UPDATE files SET local_summary=?',(json.dumps({'summary':'newer evidence'}),))
        self.assertNotEqual(stamp,self.stamp())

    def test_source_read_errors_are_not_unchanged_success(self):
        self.prime()
        with patch('services.large_qa_index.read_stamp',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaisesRegex(sqlite3.OperationalError,'disk full'):
                self.index.sync_store(self.store)

    def test_active_ingestion_reuses_verified_token(self):
        self.execute('ALTER TABLE packages ADD COLUMN status TEXT')
        self.execute("UPDATE packages SET status='processing'")
        self.prime()
        with patch.object(self.index,'sync_rows',side_effect=AssertionError('full scan')):
            self.assertEqual(self.index.sync_store(self.store),0)
        self.execute('UPDATE files SET local_summary=?',(json.dumps({'summary':'next ingest'}),))
        self.assertEqual(self.index.sync_store(self.store),1)

    def test_install_failure_is_atomic(self):
        with sqlite3.connect(str(self.db)) as c:
            with patch.object(changes, 'definitions', return_value={'bad':'invalid SQL'}):
                with self.assertRaises(sqlite3.OperationalError):
                    changes.install(c)
            names = {r[0] for r in c.execute('SELECT name FROM sqlite_master')}
            self.assertNotIn(changes.EPOCH, names)
            self.assertNotIn(changes.REVISIONS, names)
