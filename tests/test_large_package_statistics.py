"""Read consistency and query plans, without models or production state."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from config import Config
from services.large_package_isolated import LargePackageStore


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()))
        self.enterContext(patch.object(Config, 'LP_CATALOG_KMODE', False))
        self.store = LargePackageStore(self.root / 'state.db')
        self.pid = self.store.create(self.root)['id']
        self.store.upsert_files(self.pid, [dict(path='a.txt', name='a.txt', size=1)])
        self.store.update_package(self.pid, status='running', phase='quick_parse')

    def instrument(self, before=None):
        connect = self.store._connect
        connections, statements = [], []
        class Connection:
            def __init__(self, real): self.real = real
            def execute(self, sql, args=()):
                statements.append(sql)
                if before: before(sql)
                return self.real.execute(sql, args)
            def close(self): self.real.close()
            def __enter__(self):
                self.real.__enter__()
                return self
            def __exit__(self, *args): return self.real.__exit__(*args)
        def factory():
            real = connect()
            connections.append(real)
            return Connection(real)
        return factory, connections, statements

    def assert_closed(self, connections):
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError): conn.execute('SELECT 1')

    def writer(self):
        with closing(sqlite3.connect(self.store.db_path, timeout=1)) as conn:
            with conn:
                conn.execute("UPDATE files SET quick_status='completed',deep_status='completed',summary_quality='complete' WHERE package_id=?", (self.pid,))
                conn.execute("UPDATE queue SET status='completed' WHERE package_id=?", (self.pid,))
                conn.execute("UPDATE packages SET status='paused',control_state='paused' WHERE id=?", (self.pid,))

    def test_counts_snapshot_survives_writer_commit(self):
        fired = []
        def before(sql):
            if sql.startswith('SELECT quick_status') and not fired:
                self.writer(); fired.append(True)
        factory, connections, _ = self.instrument(before)
        with patch.object(self.store, '_connect', side_effect=factory): result = self.store.counts(self.pid)
        self.assertEqual(fired, [True])
        self.assertEqual(result['quick'], {'queued': 1})
        self.assertEqual(result['deep'], {'idle': 1})
        self.assertEqual(result['queue_pending'], {'quick': 1})
        self.assertEqual(result['total_files'], 1)
        self.assertEqual(len(connections), 1)
        self.assert_closed(connections)
        latest = self.store.counts(self.pid)
        self.assertEqual(latest['quick'], {'completed': 1})
        self.assertEqual(latest['pending_total'], 0)

    def test_workflow_core_snapshot_survives_writer_commit(self):
        fired = []
        def before(sql):
            if sql.startswith('SELECT stage,status') and not fired:
                self.writer(); fired.append(True)
        factory, connections, _ = self.instrument(before)
        with patch.object(self.store, '_connect', side_effect=factory): result = self.store.workflow_snapshot(self.pid)
        self.assertEqual(fired, [True])
        self.assertEqual(result['control_state'], 'active')
        self.assertEqual(result['pending_blocking'], 1)
        self.assertEqual(result['quality'], {'no_summary': 1})
        self.assertEqual(result['deep_status'], {'idle': 1})
        self.assert_closed(connections)
        latest = self.store.workflow_snapshot(self.pid)
        self.assertEqual(latest['control_state'], 'paused')
        self.assertEqual(latest['pending_total'], 0)
        self.assertEqual(latest['quality'], {'complete': 1})

    def test_counts_closes_single_deferred_read_connection(self):
        factory, connections, statements = self.instrument()
        with patch.object(self.store, '_connect', side_effect=factory): self.store.counts(self.pid)
        self.assertEqual(len(connections), 1)
        self.assertIn('BEGIN', statements)
        self.assertNotIn('BEGIN IMMEDIATE', statements)
        self.assert_closed(connections)

    def test_error_paths_close_connections(self):
        for method, trigger in [('counts', 'SELECT quick_status'), ('workflow_snapshot', 'SELECT stage,status')]:
            with self.subTest(method=method):
                def before(sql):
                    if sql.startswith(trigger): raise sqlite3.OperationalError('read error fixture')
                factory, connections, _ = self.instrument(before)
                with patch.object(self.store, '_connect', side_effect=factory):
                    with self.assertRaises(sqlite3.OperationalError): getattr(self.store, method)(self.pid)
                self.assert_closed(connections)

    def test_unknown_package_preserves_empty_contract(self):
        self.assertEqual(self.store.counts('missing'), dict(total_files=0, quick={}, deep={}, categories={}, recognition={}, queue_pending={}, pending_total=0))
        factory, connections, _ = self.instrument()
        with patch.object(self.store, '_connect', side_effect=factory): self.assertIsNone(self.store.workflow_snapshot('missing'))
        self.assert_closed(connections)

    def test_pending_preserves_stages_statuses_and_package_scope(self):
        statuses = ['queued', 'running', 'retry', 'completed', 'failed', 'cancelled']
        stages = ['quick', 'recognize', 'catalog', 'catalog_refine', 'deep', 'future_stage']
        with closing(self.store._connect()) as conn:
            conn.execute('DELETE FROM queue WHERE package_id=?', (self.pid,))
            conn.executemany('INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,?,?,0)', [(self.pid, status, stage, status) for stage in stages for status in statuses])
            conn.execute("INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES('other','a','quick','queued',0)")
        result = self.store.counts(self.pid)
        self.assertEqual(result['queue_pending'], {stage: 3 for stage in stages})
        self.assertEqual(result['pending_total'], 18)
        with closing(self.store._connect()) as conn: conn.execute("UPDATE queue SET status='completed' WHERE package_id=?", (self.pid,))
        self.assertEqual(self.store.counts(self.pid)['pending_total'], 0)

    def test_quality_index_covering_and_empty_semantics(self):
        values = ['', 'complete', 'truncated', 'degraded', 'failed', 'unsupported', 'no_summary']
        self.store.upsert_files(self.pid, [dict(path=str(i), name=str(i)) for i in range(len(values))])
        sql = "SELECT COALESCE(NULLIF(summary_quality,''),'no_summary') k,COUNT(*) FROM files WHERE package_id=? GROUP BY 1"
        with closing(self.store._connect()) as conn:
            conn.executemany('UPDATE files SET summary_quality=? WHERE package_id=? AND path=?', [(v, self.pid, str(i)) for i, v in enumerate(values)])
            plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+sql, (self.pid,))]
            self.assertTrue(any('COVERING INDEX idx_lp_files_quality' in p for p in plan), plan)
        quality = self.store.workflow_snapshot(self.pid)['quality']
        self.assertEqual(quality['no_summary'], 3)
        self.assertEqual(sum(quality.values()), len(values)+1)

    def test_pending_index_excludes_terminal_history(self):
        sql = "SELECT stage,COUNT(*) FROM queue WHERE package_id=? AND status IN ('queued','running','retry') GROUP BY stage"
        with closing(self.store._connect()) as conn:
            conn.execute('BEGIN')
            conn.executemany("INSERT INTO queue(package_id,path,stage,status,updated_at) VALUES(?,?,'quick','completed',0)", [(self.pid, 'old'+str(i)) for i in range(2000)])
            conn.commit()
            conn.execute('ANALYZE')
            plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+sql, (self.pid,))]
            self.assertTrue(any('idx_lp_queue_pending_counts' in p for p in plan), plan)
            self.assertFalse(any('TEMP B-TREE' in p for p in plan), plan)
            self.assertEqual([tuple(r) for r in conn.execute(sql, (self.pid,))], [('quick', 1)])

    def test_additive_indexes_idempotent_and_preserve_rows(self):
        tables = ('files', 'queue', 'packages')
        with closing(self.store._connect()) as conn:
            before = {t: [tuple(r) for r in conn.execute('SELECT * FROM '+t)] for t in tables}
            conn.execute('DROP INDEX IF EXISTS idx_lp_files_quality')
            conn.execute('DROP INDEX IF EXISTS idx_lp_queue_pending_counts')
        LargePackageStore(self.store.db_path)
        LargePackageStore(self.store.db_path)
        with closing(self.store._connect()) as conn:
            self.assertEqual(before, {t: [tuple(r) for r in conn.execute('SELECT * FROM '+t)] for t in tables})
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn('idx_lp_files_quality', indexes)
            self.assertIn('idx_lp_queue_pending_counts', indexes)
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_migration_analyzes_only_new_indexes_and_preserves_covering_plans(self):
        self.store.upsert_files(self.pid, [dict(path=str(i), name=str(i)) for i in range(2000)])
        with closing(self.store._connect()) as conn:
            conn.execute('DROP INDEX IF EXISTS idx_lp_files_quality')
            conn.execute('DROP INDEX IF EXISTS idx_lp_queue_pending_counts')
            conn.execute('ANALYZE')
        statements = []
        connect = self.store._connect
        def traced():
            conn = connect(); conn.set_trace_callback(statements.append); return conn
        with patch.object(self.store, '_connect', side_effect=traced): self.store._init_db()
        self.assertEqual([s for s in statements if s.startswith('ANALYZE ')], ['ANALYZE idx_lp_files_quality', 'ANALYZE idx_lp_queue_pending_counts'])
        with closing(self.store._connect()) as conn:
            stats = {r[0] for r in conn.execute("SELECT idx FROM sqlite_stat1")}
            self.assertIn('idx_lp_files_quality', stats)
            self.assertIn('idx_lp_queue_pending_counts', stats)
            for column in ('quick_status', 'deep_status', 'category', 'recognition_status'):
                sql = 'SELECT '+column+',COUNT(*) FROM files WHERE package_id=? GROUP BY '+column
                plan = [r[3] for r in conn.execute('EXPLAIN QUERY PLAN '+sql, (self.pid,))]
                self.assertTrue(any('COVERING INDEX' in p for p in plan), plan)
                # A one-package fixture may legitimately scan its covering
                # index and use temporary grouping. The regression is reading
                # large file rows via the unrelated new quality index.
                self.assertFalse(any('idx_lp_files_quality' in p for p in plan), plan)
        statements.clear()
        with patch.object(self.store, '_connect', side_effect=traced): self.store._init_db()
        self.assertFalse(any(s.startswith('ANALYZE ') for s in statements))
