"""Bounded, live history reads and partial-package chat availability."""
import gc
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.large_package_isolated import LargePackageStore
from services.large_qa_index import summary_text


class HistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='lp-history-')
        self.addCleanup(tmp.cleanup)
        # Existing store initialization uses connection context managers,
        # which release transactions but leave handles until collection.
        self.addCleanup(gc.collect)
        self.root = Path(tmp.name)
        self.enterContext(patch.object(Config, 'LP_CATALOG_KMODE', False))
        self.store = LargePackageStore(self.root / 'state.db')

    def package(self, pid, *, owner='alice', status='running', phase='quick_parse', updated=1):
        with closing(self.store._connect()) as conn:
            conn.execute('INSERT INTO packages(id,root_path,owner_id,status,phase,counts,created_at,updated_at) '
                         'VALUES(?,?,?,?,?,?,0,?)',
                         (pid, str(self.root / pid), owner, status, phase,
                          json.dumps(dict(total_files=999, total_bytes=42, total_size_human='42 B')), updated))
        return pid

    def file(self, pid, path='a.txt', *, local=None, deep=None, sensitive=0, changed=0):
        with closing(self.store._connect()) as conn:
            conn.execute('INSERT INTO files(package_id,path,name,local_summary,deep_summary,sensitive,source_changed,updated_at) '
                         'VALUES(?,?,?,?,?,?,?,0)',
                         (pid, path, path, json.dumps(local if local is not None else {}),
                          json.dumps(deep if deep is not None else {}), sensitive, changed))

    def instrument(self, before=None):
        connect = self.store._connect
        connections, statements = [], []

        class Connection:
            def __init__(self, real): self.real = real
            def execute(self, sql, args=()):
                statements.append((sql, tuple(args)))
                if before: before(sql)
                return self.real.execute(sql, args)
            def __getattr__(self, name): return getattr(self.real, name)
            def close(self): self.real.close()

        def factory():
            real = connect()
            connections.append(real)
            return Connection(real)
        return factory, connections, statements

    def assert_closed(self, connections):
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError): conn.execute('SELECT 1')

    def test_page_batch_counts_owner_query_and_live_empty_contract(self):
        for i in range(12):
            pid = self.package('lp-match-' + str(i), updated=i)
            for j in range(i): self.file(pid, str(j), local={'summary': 'usable'})
        self.package('lp-match-foreign', owner='bob', updated=100)
        self.file('lp-match-foreign', local={'summary': 'foreign'})
        self.package('lp-unmatched', updated=99)
        factory, connections, statements = self.instrument()
        with patch.object(self.store, '_connect', side_effect=factory), \
                patch.object(self.store, 'counts', side_effect=AssertionError('full statistics are forbidden')):
            result = self.store.list_history(owner_id='alice', query='match-', limit=3, offset=2)
        self.assertEqual(result['total'], 12)
        self.assertEqual((result['limit'], result['offset']), (3, 2))
        self.assertEqual([item['scan_id'] for item in result['items']], ['lp-match-9', 'lp-match-8', 'lp-match-7'])
        self.assertEqual([item['file_count'] for item in result['items']], [9, 8, 7])
        self.assertTrue(all(item['total_size'] == 42 and item['total_size_human'] == '42 B' for item in result['items']))
        self.assertEqual(len(connections), 1)
        self.assertEqual(sum(sql.startswith('SELECT ') for sql, _ in statements), 4)
        self.assertEqual([args for sql, args in statements if 'FROM files' in sql],
                         [('lp-match-9', 'lp-match-8', 'lp-match-7')] * 2)
        self.assertFalse(any('FROM queue' in sql for sql, _ in statements))
        self.assert_closed(connections)
        empty_package = self.store.list_history(owner_id='alice', query='lp-match-0')['items'][0]
        self.assertEqual(empty_package['file_count'], 0)
        self.assertFalse(empty_package['can_chat'])

    def test_chat_available_from_partial_summaries_in_all_package_states(self):
        for status in ('queued', 'running', 'paused', 'failed', 'cancelled', 'completed', 'waiting_for_selection'):
            pid = self.package('lp-' + status, status=status)
            self.file(pid, local={'sample': 'persisted partial content'})
        items = self.store.list_history()['items']
        self.assertEqual(len(items), 7)
        for item in items:
            self.assertTrue(item['can_chat'], item)
            self.assertTrue(item['usable'], item)
            self.assertEqual(item['analysis_ready'], item['large_status'] in {'completed', 'waiting_for_selection'})
        by_status = {item['large_status']: item for item in items}
        self.assertEqual(by_status['running']['status'], 'preprocessing')
        self.assertEqual(by_status['failed']['status'], 'failed')
        self.assertEqual(by_status['paused']['status'], 'paused')

    def test_summary_content_matches_qa_selection_and_pending_fallback(self):
        cases = [({}, {}, False), ({'title': 'name', 'keywords': ['word']}, {}, False),
                 ({}, {'summary_type': 'model_pending', 'summary': 'placeholder'}, False),
                 ({'summary': 'local'}, {'summary_type': 'model_pending', 'summary': 'placeholder'}, True),
                 ({'sample': 'local'}, {'summary_type': 'deep', 'title': 'metadata'}, True),
                 ({}, {'conclusion': 'deep'}, True)]
        for key in ('summary', 'text', 'sample', 'abstract', 'conclusion', 'key_findings'):
            cases.append(({key: ['fact']}, {}, True))
            for value in ('', [], {}, None, False, 0): cases.append(({key: value}, {}, False))
        for i, (local, deep, expected) in enumerate(cases):
            pid = self.package('lp-case-' + str(i))
            self.file(pid, local=local, deep=deep)
            self.assertEqual(bool(summary_text({'local_summary': local, 'deep_summary': deep})), expected)
        items = {item['scan_id']: item for item in self.store.list_history(limit=50)['items']}
        for i, (_, _, expected) in enumerate(cases):
            self.assertEqual(items['lp-case-' + str(i)]['can_chat'], expected, i)

    def test_sensitive_and_changed_summaries_do_not_enable_chat(self):
        self.package('lp-sensitive', status='completed')
        self.file('lp-sensitive', local={'summary': 'secret'}, sensitive=1)
        self.package('lp-changed', status='paused')
        self.file('lp-changed', deep={'summary': 'stale'}, changed=1)
        self.package('lp-healthy')
        self.file('lp-healthy', local={'summary': 'fresh'})
        items = {item['scan_id']: item for item in self.store.list_history()['items']}
        self.assertFalse(items['lp-sensitive']['can_chat'])
        self.assertFalse(items['lp-sensitive']['usable'])
        self.assertTrue(items['lp-sensitive']['analysis_ready'])
        self.assertFalse(items['lp-changed']['can_chat'])
        self.assertTrue(items['lp-healthy']['can_chat'])
        self.assertTrue(all(item['file_count'] == 1 for item in items.values()))

    def test_fresh_reads_reflect_new_removed_and_invalidated_summaries(self):
        self.package('lp-live')
        self.file('lp-live')
        first = self.store.list_history()['items'][0]
        self.assertEqual((first['file_count'], first['can_chat']), (1, False))
        self.file('lp-live', 'ready.txt', local={'summary': 'ready'})
        second = self.store.list_history()['items'][0]
        self.assertEqual((second['file_count'], second['can_chat']), (2, True))
        with closing(self.store._connect()) as conn:
            conn.execute('UPDATE files SET source_changed=1 WHERE path=?', ('ready.txt',))
        self.assertFalse(self.store.list_history()['items'][0]['can_chat'])
        with closing(self.store._connect()) as conn:
            conn.execute('DELETE FROM files WHERE package_id=?', ('lp-live',))
        last = self.store.list_history()['items'][0]
        self.assertEqual((last['file_count'], last['can_chat']), (0, False))

    def test_single_snapshot_survives_concurrent_writer(self):
        self.package('lp-live')
        self.file('lp-live', local={'summary': 'current'})
        fired = []

        def before(sql):
            if sql.startswith('SELECT package_id,COUNT') and not fired:
                with closing(sqlite3.connect(self.store.db_path, timeout=1)) as conn, conn:
                    conn.execute("UPDATE packages SET status='paused' WHERE id='lp-live'")
                    conn.execute("DELETE FROM files WHERE package_id='lp-live'")
                fired.append(True)

        factory, connections, statements = self.instrument(before)
        with patch.object(self.store, '_connect', side_effect=factory): old = self.store.list_history()['items'][0]
        self.assertEqual(fired, [True])
        self.assertEqual((old['large_status'], old['file_count'], old['can_chat']), ('running', 1, True))
        self.assertIn(('BEGIN', ()), statements)
        self.assert_closed(connections)
        latest = self.store.list_history()['items'][0]
        self.assertEqual((latest['large_status'], latest['file_count'], latest['can_chat']), ('paused', 0, False))

    def test_empty_page_and_error_close_the_read_connection(self):
        self.package('lp-live')
        factory, connections, statements = self.instrument()
        with patch.object(self.store, '_connect', side_effect=factory): empty = self.store.list_history(offset=10)
        self.assertEqual(empty['items'], [])
        self.assertEqual(empty['total'], 1)
        self.assertEqual(sum(sql.startswith('SELECT ') for sql, _ in statements), 2)
        self.assert_closed(connections)

        def before(sql):
            if sql.startswith('SELECT package_id,COUNT'): raise sqlite3.OperationalError('fixture')
        factory, connections, _ = self.instrument(before)
        with patch.object(self.store, '_connect', side_effect=factory):
            with self.assertRaises(sqlite3.OperationalError): self.store.list_history()
        self.assert_closed(connections)

    def test_exists_stops_after_first_eligible_summary_and_counts_use_covering_index(self):
        self.package('lp-page')
        for i in range(30): self.file('lp-page', str(i), local={'summary': 'eligible'})
        self.package('lp-off-page', updated=0)
        for i in range(30): self.file('lp-off-page', str(i), local={'summary': 'excluded'})
        with patch('services.large_qa_index.summary_text', wraps=summary_text) as render:
            item = self.store.list_history(limit=1)['items'][0]
        self.assertEqual((item['scan_id'], item['file_count'], item['can_chat']), ('lp-page', 30, True))
        self.assertEqual(render.call_count, 1)
        with closing(self.store._connect()) as conn:
            plan = [row[3] for row in conn.execute(
                'EXPLAIN QUERY PLAN SELECT package_id,COUNT(*) FROM files WHERE package_id IN (?) GROUP BY package_id',
                ('lp-page',))]
        self.assertTrue(any('COVERING INDEX' in item for item in plan), plan)
        self.assertFalse(any('SCAN files' in item for item in plan), plan)


if __name__ == '__main__':
    unittest.main()
