"""Directory finalization gate: identical quality rules, bounded row loading."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from services import large_package_catalog as catalog
from services import large_package_semantic as semantic
from services.large_package_isolated import LargePackageStore


def original_count(store, package_id):
    """Frozen pre-optimization oracle, including its conservative exceptions."""
    with closing(store._connect()) as conn:
        where = (' FROM files WHERE package_id=? AND sensitive=0 '
                 'AND path NOT IN (SELECT path FROM large_catalog_members '
                 'WHERE package_id=? AND LENGTH(vector)>0)')
        args = (package_id, package_id)
        pending = conn.execute('SELECT COUNT(*)' + where, args).fetchone()[0]
        if not int(pending or 0):
            return 0
        rows = conn.execute('SELECT *' + where, args).fetchall()
    total = 0
    for raw in rows:
        row = store._row(raw)
        if ((row.get('local_summary') or {}).get('portrait') or {}).get('catalog_quality'):
            continue
        try:
            if catalog.material(row):
                total += 1
        except Exception:
            total += 1
    return total


class ReadStore:
    _row = staticmethod(LargePackageStore._row)

    def __init__(self, db):
        self.db = db
        self.sql = []
        self.connections = []
        self.forbid_fetchall = False

    def _connect(self):
        owner = self

        class Cursor(sqlite3.Cursor):
            def fetchall(self):
                if owner.forbid_fetchall:
                    raise AssertionError('The gate must stream, not fetchall')
                return super().fetchall()

        class Connection(sqlite3.Connection):
            was_closed = False

            def execute(self, sql, args=()):
                owner.sql.append(sql)
                return self.cursor(factory=Cursor).execute(sql, args)

            def close(self):
                self.was_closed = True
                return super().close()

        conn = sqlite3.connect(self.db, factory=Connection)
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn


class MaterialPendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ReadStore(str(Path(self.tmp.name) / 'isolated.db'))
        with closing(self.store._connect()) as conn:
            conn.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE files(
                    package_id TEXT NOT NULL,path TEXT NOT NULL,name TEXT,
                    sensitive INTEGER NOT NULL DEFAULT 0,local_summary TEXT,
                    deep_summary TEXT,deep_status TEXT,error TEXT,
                    PRIMARY KEY(package_id,path));
                CREATE TABLE large_catalog_members(
                    package_id TEXT NOT NULL,path TEXT NOT NULL,vector BLOB NOT NULL,
                    PRIMARY KEY(package_id,path));
            ''')

    def add(self, path, local=None, deep=None, status='idle', sensitive=0, package='p'):
        local = {'sample': 'Readable primary evidence about the project and its funding.'} if local is None else local
        local = json.dumps(local, ensure_ascii=False) if not isinstance(local, str) else local
        deep = json.dumps(deep or {}, ensure_ascii=False) if not isinstance(deep, str) else deep
        with closing(self.store._connect()) as conn:
            conn.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',
                         (package, path, 'irrelevant name', sensitive, local, deep, status, 'x' * 1024))
            conn.commit()

    def vector(self, path, vector=b'valid', package='p'):
        with closing(self.store._connect()) as conn:
            conn.execute('INSERT OR REPLACE INTO large_catalog_members VALUES(?,?,?)',
                         (package, path, vector))
            conn.commit()

    def check(self, expected, package='p'):
        self.assertEqual(original_count(self.store, package), expected)
        self.assertEqual(semantic.material_pending_count(self.store, package), expected)

    def test_empty_and_unknown_package(self):
        self.check(0)
        self.add('a')
        self.check(0, 'missing')

    def test_all_languages_and_projection_preserve_material_and_digest(self):
        texts = ['项目预算和采购审批需要逐项核查，所有判断必须引用原始证据。',
                 'A research project needs documented funding and procurement approval.',
                 'โครงการวิจัยต้องมีหลักฐานการอนุมัติงบประมาณและการจัดซื้อที่ตรวจสอบได้']
        for i, text in enumerate(texts):
            self.add(str(i), {'sample': text, 'keywords': ['预算', 'funding', 'งบประมาณ']})
        self.add('evidence', {'portrait': {'evidence': [{'kind': 'text', 'text': t, 'location': str(i)} for i,t in enumerate(texts)], 'fields': ['a', 'b'], 'headings': ['Title']}})
        self.add('deep', {}, {'summary': texts[0], 'text_preview': texts[2]}, 'completed')
        with closing(self.store._connect()) as conn:
            full = [self.store._row(r) for r in conn.execute('SELECT * FROM files ORDER BY path')]
            projected = [self.store._row(r) for r in conn.execute('SELECT local_summary,deep_summary,deep_status,sensitive FROM files ORDER BY path')]
        for left, right in zip(full, projected):
            self.assertEqual(catalog.material(left), catalog.material(right))
        self.check(5)

    def test_vectors_empty_vectors_and_package_isolation(self):
        for path in ['none', 'empty', 'present', 'zero-byte-value', 'same-path']:
            self.add(path)
        self.vector('empty', b'')
        self.vector('present')
        self.vector('zero-byte-value', b'\x00')
        self.vector('same-path', package='other')
        self.add('unrelated', package='other')
        self.check(3)
        self.check(1, 'other')

    def test_sensitive_restricted_quality_and_whitespace(self):
        self.add('sensitive', sensitive=1)
        self.add('restricted', {'sample': 'primary evidence ' * 4, 'portrait': {'recognition_status': 'restricted'}})
        self.add('quality', {'sample': 'primary evidence ' * 4, 'portrait': {'catalog_quality': {'reason': 'unreadable'}}})
        self.add('empty-quality', {'sample': 'primary evidence ' * 4, 'portrait': {'catalog_quality': ''}})
        self.add('space', {'sample': ' ' * 200})
        self.check(1)

    def test_deep_status_and_failed_summary(self):
        for status in ['idle', 'queued', 'running', 'failed']:
            self.add(status, {}, {'summary': 'deep content ' * 8}, status)
        self.add('completed', {}, {'summary': 'deep content ' * 8}, 'completed')
        self.add('failed-local', {}, {'summary': 'deep content ' * 8, 'summary_type': 'local_deep_failed'}, 'completed')
        self.check(1)

    def test_metadata_only_evidence_and_minimum_boundaries(self):
        self.add('short', {'sample': 'x' * 19})
        self.add('boundary', {'sample': 'x' * 20})
        self.add('one-field', {'portrait': {'fields': ['a']}})
        self.add('two-fields', {'portrait': {'fields': ['a', 'b']}})
        self.add('names-only', {'sample': 'Must not fall back to this sample', 'portrait': {'evidence': [{'kind': 'member_names', 'text': 'x' * 100}, {'kind': 'attachment', 'text': 'y' * 100}]}})
        self.check(2)

    def test_bad_json_is_decoded_by_existing_store(self):
        self.add('bad-local', '{broken', '{broken')
        self.add('bad-deep', deep='{broken', status='completed')
        self.check(1)

    def test_material_exception_is_still_pending(self):
        self.add('a')
        with patch.object(catalog, 'material', side_effect=RuntimeError('unavailable')):
            self.check(1)

    def test_malformed_local_structure_still_fails_closed(self):
        self.add('a', '[1]')
        for fn in [original_count, semantic.material_pending_count]:
            with self.assertRaises(AttributeError):
                fn(self.store, 'p')
        self.assertTrue(all(c.was_closed for c in self.store.connections))

    def test_single_select_streams_only_required_columns_without_limit(self):
        for i in range(137):
            self.add(str(i))
        self.store.sql.clear()
        self.store.forbid_fetchall = True
        self.assertEqual(semantic.material_pending_count(self.store, 'p'), 137)
        self.assertEqual(len(self.store.sql), 1)
        sql = self.store.sql[0].upper()
        self.assertNotIn('SELECT *', sql)
        self.assertNotIn('LIMIT', sql)
        self.assertNotIn('COUNT(', sql)
        self.assertTrue(all(c.was_closed for c in self.store.connections))

    def test_changes_are_visible_on_next_call_without_cache(self):
        self.add('a')
        self.check(1)
        self.vector('a')
        self.check(0)
        self.vector('a', b'')
        self.check(1)
        with closing(self.store._connect()) as conn:
            conn.execute('UPDATE files SET sensitive=1 WHERE path=?', ('a',))
            conn.commit()
        self.check(0)

    def test_reader_keeps_one_snapshot_during_concurrent_write(self):
        self.add('a')
        self.add('b')
        original = catalog.material
        calls = []
        def changing(row):
            if not calls:
                self.vector('b')
            calls.append(1)
            return original(row)
        with patch.object(catalog, 'material', side_effect=changing):
            self.assertEqual(semantic.material_pending_count(self.store, 'p'), 2)
        self.check(1)

    def test_sql_failure_is_not_mistaken_for_complete(self):
        with closing(self.store._connect()) as conn:
            conn.execute('DROP TABLE large_catalog_members')
        with self.assertRaises(sqlite3.OperationalError):
            semantic.material_pending_count(self.store, 'p')
        self.assertTrue(all(c.was_closed for c in self.store.connections))


if __name__ == '__main__':
    unittest.main()
