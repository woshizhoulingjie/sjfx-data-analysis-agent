import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.large_package_isolated import LargePackageStore
from services.large_package_catalog import material
from services import large_package_semantic as semantic


class BulkIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()))
        self.store = LargePackageStore(self.root / 'db.sqlite')
        self.pid = self.store.create(self.root)['id']

    def fill(self, count=6):
        texts = ['中文设备证据验证，保留完整原文与附件说明。',
                 'Evidence verification and trusted communication records.',
                 'การตรวจสอบหลักฐานและการสื่อสารของระบบข้อมูล']
        paths = ['目录/%04d-เอกสาร.txt' % i for i in range(count)]
        self.store.upsert_files(self.pid, [dict(path=p, name=p, size=128) for p in paths])
        for i, path in enumerate(paths):
            self.store.update_file(self.pid, path, quick_status='completed',
                local_summary={'sample': texts[i % 3] * 4,
                               'portrait': {'fields': [], 'headings': []}})
        return paths

    def test_exact_rows_and_material_across_chunks(self):
        paths = self.fill(259)
        fetched = self.store.get_files(self.pid, list(reversed(paths)) + paths[:3] + ['missing'])
        self.assertEqual(set(fetched), set(paths))
        for path in paths:
            expected = self.store.get_file(self.pid, path)
            self.assertEqual(fetched[path], expected)
            self.assertEqual(material(fetched[path]), material(expected))

    def test_empty_read_opens_no_connection(self):
        with patch.object(self.store, '_connect', side_effect=AssertionError('unexpected connection')):
            self.assertEqual(self.store.get_files(self.pid, []), {})

    def test_package_scope_and_missing_rows(self):
        paths = self.fill()
        self.assertEqual(self.store.get_files('other-package', paths), {})
        self.assertEqual(self.store.get_files(self.pid, ['missing']), {})

    def test_updates_are_never_served_from_stale_cache(self):
        path = self.fill(1)[0]
        before = self.store.get_files(self.pid, [path], decorate=False)[path]
        self.store.update_file(self.pid, path, sensitive=1, local_summary={'sample':'Changed source'})
        after = self.store.get_files(self.pid, [path], decorate=False)[path]
        self.assertNotEqual(before, after)
        self.assertIsNone(material(after))

    def test_raw_batch_uses_one_connection_and_bounded_queries(self):
        paths = self.fill(259)
        statements, connections = [], []
        connect = self.store._connect
        def traced():
            c = connect(); c.set_trace_callback(statements.append); connections.append(c); return c
        with patch.object(self.store, '_connect', side_effect=traced):
            result = self.store.get_files(self.pid, paths, decorate=False)
        self.assertEqual(len(result), 259)
        self.assertEqual(len(connections), 1)
        self.assertEqual(sum(s.startswith('SELECT * FROM files') for s in statements), 3)
        import sqlite3
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute('SELECT 1')

    def test_ready_indexes_keep_priority_and_ignore_terminal_history(self):
        with closing(self.store._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            c.executemany('INSERT INTO queue(package_id,path,stage,status,attempts,priority,updated_at) VALUES(?,?,?,?,?,?,?)',
                [(self.pid,'old%d'%i,'recognize','completed',0,0,float(i)) for i in range(2000)] +
                [(self.pid,'retry','recognize','retry',1,0,1.),
                 (self.pid,'low','recognize','queued',0,8,2.),
                 (self.pid,'first','recognize','queued',0,2,3.),
                 (self.pid,'second','recognize','queued',0,2,4.)])
            c.commit()
            plan = [r[3] for r in c.execute(
                "EXPLAIN QUERY PLAN SELECT path FROM queue INDEXED BY idx_lp_claim_ready_priority WHERE package_id=? AND stage=? AND status IN ('queued','retry') ORDER BY attempts,priority,updated_at LIMIT 4",
                (self.pid,'recognize'))]
        self.assertFalse(any('TEMP B-TREE' in p for p in plan), plan)
        rows = self.store.claim('recognize',4,package_id=self.pid)
        self.assertEqual([r[1] for r in rows],['first','second','low','retry'])
        self.assertEqual(len({r[2] for r in rows}),4)

    def test_pause_prevents_claiming_and_old_lease_cannot_commit(self):
        self.fill(1)
        claimed = self.store.claim('quick',1,package_id=self.pid)
        self.assertEqual(len(claimed),1)
        pid,path,lease = claimed[0]
        self.assertFalse(self.store.holds_lease(pid,path,'quick','outdated-token'))
        self.store.update_package(pid,status='paused')
        self.assertEqual(self.store.claim('catalog',10,package_id=pid),[])

    def model(self, callback=None):
        class Model:
            model = 'bulk-io-fixture'
            max_batch_size = 64
            def embed(self, texts):
                if callback: callback()
                import hashlib
                return [[b + 1 for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts]
        return Model()

    def eligible(self, paths):
        # Parity test, not a claim that the existing language-quality gate is
        # adequate: that gate currently rejects the pure-Thai fixture. This
        # performance change must not silently widen or bypass its policy.
        return {p for p in paths if semantic.evidence_quality(material(self.store.get_file(self.pid,p))[0])[0]}

    def test_embeddings_preserve_every_file_and_exact_vectors(self):
        paths = self.fill()
        eligible = self.eligible(paths)
        model = self.model()
        with patch.object(Config, 'LP_CATALOG_KMODE', True):
            semantic.embed_batch(self.store, self.pid, client=model)
        with closing(self.store._connect()) as c:
            rows = c.execute('SELECT path,input_hash,vector FROM large_catalog_members WHERE package_id=?',(self.pid,)).fetchall()
        self.assertEqual(set(r['path'] for r in rows),eligible)
        self.assertEqual(set(self.store.get_files(self.pid,paths)),set(paths))
        from services.large_package_catalog_quality import representation
        policy=representation(self.store,self.pid)
        from services.large_package_evidence import row_parts
        for row in rows:
            source=self.store.get_file(self.pid,row['path']); value,digest=material(source)
            if policy>=2:value=dict(value,evidence_blocks=row_parts(source))
            expected=semantic._vector(model.embed([semantic.embedding_passage(value,policy)])[0]).tobytes()
            self.assertEqual(row['input_hash'],digest)
            self.assertEqual(row['vector'],expected)
        self.assertEqual(self.store.stage_counts(self.pid,'catalog'),{'completed':len(paths)})

    def test_cache_batch_failure_rolls_back_every_insert(self):
        paths=self.fill()
        eligible=self.eligible(paths)
        with closing(self.store._connect()) as c:
            c.execute("CREATE TRIGGER fixture_cache_failure BEFORE INSERT ON large_catalog_embeddings WHEN (SELECT COUNT(*) FROM large_catalog_embeddings)>0 BEGIN SELECT RAISE(ABORT,'fixture cache failure'); END")
        with patch.object(Config,'LP_CATALOG_KMODE',True):
            semantic.embed_batch(self.store,self.pid,client=self.model())
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM large_catalog_embeddings').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM large_catalog_members').fetchone()[0],0)
        self.assertEqual(sum(self.store.stage_counts(self.pid,'catalog').values()),len(paths))
        self.assertEqual(self.store.stage_counts(self.pid,'catalog').get('completed',0),len(paths)-len(eligible))

    def test_cancel_after_embedding_returns_all_leases(self):
        paths=self.fill();cancelled=[False]
        eligible=self.eligible(paths)
        # Chinese, English AND Thai are valid evidence. Cancellation must
        # return every lease; GROUP BY does not emit a zero-count status.
        self.assertEqual(len(eligible), len(paths))
        def cancel():cancelled[0]=True
        with patch.object(Config,'LP_CATALOG_KMODE',True):
            semantic.embed_batch(self.store,self.pid,client=self.model(cancel),cancelled=lambda:cancelled[0])
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM large_catalog_members').fetchone()[0],0)
        self.assertEqual(self.store.stage_counts(self.pid,'catalog'),{'queued':len(paths)})

    def test_source_change_during_embedding_rejects_old_material(self):
        path=self.fill(1)[0]
        def change():
            self.store.update_file(self.pid,path,local_summary={'sample':'Completely different procurement and budget source information.'})
        with patch.object(Config,'LP_CATALOG_KMODE',True):
            semantic.embed_batch(self.store,self.pid,client=self.model(change))
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM large_catalog_members').fetchone()[0],0)
        self.assertEqual(self.store.stage_counts(self.pid,'catalog').get('completed',0),0)
