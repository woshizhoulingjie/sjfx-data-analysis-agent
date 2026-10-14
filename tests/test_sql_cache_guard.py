"""Exercise real SQLite triggers, projected joins, cursors and FTS5."""
import sqlite3
import unittest
from unittest.mock import patch

import test_package_cache_provenance as provenance
from sjfx_protection import business
from sjfx_protection.sql_cache import checked_connection
from sjfx_protection.store import ProtectionError


class SqlCacheTests(unittest.TestCase):
    def setUp(self):
        provenance.CacheProvenanceTests.setUp(self)
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE docs(id TEXT PRIMARY KEY,package TEXT,body TEXT,status TEXT);
          CREATE TABLE packages(id TEXT PRIMARY KEY,owner TEXT);
          INSERT INTO packages VALUES('A','alice'),('B','bob');
          CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,package UNINDEXED,body);
        ''')
        self.specs = {'docs': {'content': ('body',), 'identity': ('package',)},
                      'search': {'content': ('body',), 'identity': ('id', 'package'),
                                 'fts': ('id UNINDEXED', 'package UNINDEXED', 'body')}}

    def tearDown(self):
        self.db.close()
        provenance.CacheProvenanceTests.tearDown(self)

    def scopes(self, db, table, row):
        if row['package'] == 'A':
            return [{'owner': 'alice', 'scope': 'A'}]
        return []

    def checked(self):
        return checked_connection(self.db, 'synthetic.db', self.specs, self.scopes)

    def insert(self, *values):
        with self.checked() as db:
            db.execute('INSERT INTO docs VALUES(?,?,?,?)', values or ('d1', 'A', 'approved text', 'ready'))

    def test_direct_insert_projected_read_join(self):
        self.insert()
        with self.checked() as db:
            row = db.execute("SELECT substr(d.body,1,8) AS output FROM docs d JOIN packages p ON p.id=d.package WHERE p.owner=?", ('alice',)).fetchone()
            self.assertEqual(row['output'], 'approved')

    def test_unknown_content_not_blessed_on_select(self):
        self.db.execute("INSERT INTO docs VALUES('old','A','historical text','ready')")
        self.db.commit()
        with self.assertRaises(ProtectionError) as caught:
            with self.checked() as db:
                db.execute('SELECT body FROM docs').fetchall()
        self.assertEqual(caught.exception.code, 'cache_provenance_missing')

    def test_metadata_exempt_but_status_never_blesses(self):
        self.db.execute("INSERT INTO docs VALUES('old','A','historical text','ready')")
        self.db.commit()
        with self.checked() as db:
            db.execute("UPDATE docs SET status='complete' WHERE id='old'")
            self.assertEqual(db.execute('SELECT status FROM docs').fetchone()[0], 'complete')
        self.test_unknown_content_not_blessed_on_select_after_metadata()

    def test_unknown_content_not_blessed_on_select_after_metadata(self):
        self.db.execute("INSERT OR IGNORE INTO docs VALUES('old','A','historical text','ready')")
        self.db.commit()
        with self.assertRaises(ProtectionError):
            with self.checked() as db:
                db.execute('SELECT body FROM docs').fetchall()

    def test_partial_update_cannot_bless_old_content(self):
        self.db.execute("INSERT INTO docs VALUES('old','A','historical text','ready')")
        self.db.commit()
        with self.assertRaises(ProtectionError):
            with self.checked() as db:
                db.execute("UPDATE docs SET body=body || 'extra' WHERE id='old'")
        self.assertEqual(self.db.execute("SELECT body FROM docs WHERE id='old'").fetchone()[0], 'historical text')

    def test_new_output_can_be_updated_and_read_before_commit(self):
        with self.checked() as db:
            db.execute("INSERT INTO docs VALUES('new','A','first','new')")
            db.execute("UPDATE docs SET body='second' WHERE id='new'")
            self.assertEqual(db.execute("SELECT body FROM docs WHERE id='new'").fetchone()[0], 'second')
        with self.checked() as db:
            self.assertEqual(db.execute("SELECT body FROM docs WHERE id='new'").fetchone()[0], 'second')

    def test_external_tamper_detected(self):
        self.insert()
        self.db.execute("UPDATE docs SET body='tampered' WHERE id='d1'")
        self.db.commit()
        with self.assertRaises(ProtectionError) as caught:
            with self.checked() as db:
                db.execute("SELECT body FROM docs WHERE id='d1'").fetchone()
        self.assertEqual(caught.exception.code, 'cache_output_mismatch')

    def test_scope_filtered_on_real_rows_projection_no_id(self):
        self.insert()
        # Simulates another owner's producer outside this connection.
        self.db.execute("INSERT INTO docs VALUES('foreign','B','foreign secret','ready')")
        self.db.commit()
        with self.checked() as db:
            self.assertEqual([row[0] for row in db.execute('SELECT body FROM docs')], ['approved text'])
            self.assertEqual(db.execute("SELECT body FROM main.docs WHERE package='B'").fetchall(), [])

    def test_foreign_write_rejected(self):
        with self.assertRaises(ProtectionError) as caught:
            with self.checked() as db:
                db.execute("INSERT INTO docs VALUES('foreign','B','untrusted','ready')")
        self.assertEqual(caught.exception.code, 'cache_scope_mismatch')

    def test_quoted_identifier_literals_comments_and_cte(self):
        self.insert()
        with self.checked() as db:
            row = db.execute('''WITH subset AS (SELECT body FROM "docs")
                SELECT body,'docs',length(body) FROM subset -- docs
                ''').fetchone()
            self.assertEqual(row[1], 'docs')

    def test_saved_sources_version_change(self):
        self.insert()
        newer = provenance.context(version='v2')
        self.package.current['A'] = newer
        business.execution.set(newer)
        with self.assertRaises(ProtectionError) as caught:
            with self.checked() as db:
                db.execute('SELECT body FROM docs').fetchall()
        self.assertEqual(caught.exception.code, 'context_stale')

    def test_fts_match_and_bm25_still_work(self):
        with self.checked() as db:
            db.execute("INSERT INTO search(id,package,body) VALUES('d1','A','approved text')")
        with self.checked() as db:
            rows = db.execute("SELECT id,body FROM search WHERE search MATCH ? ORDER BY bm25(search)", ('approved',)).fetchall()
            self.assertEqual(rows[0]['body'], 'approved text')

    def test_explicit_rollback_has_no_stamp(self):
        with self.checked() as db:
            db.execute("INSERT INTO docs VALUES('rolled','A','discarded','new')")
            db.rollback()
        self.assertIsNone(self.db.execute("SELECT id FROM docs WHERE id='rolled'").fetchone())

    def test_off_uses_original_connection(self):
        self.p.enabled = False
        with self.checked() as db:
            self.assertIs(db, self.db)
            db.execute("INSERT INTO docs VALUES('old','A','historical','ready')")
            self.assertEqual(db.execute('SELECT body FROM docs').fetchone()[0], 'historical')
        self.assertEqual(self.store.opens, 0)

    def test_raw_regeneration_identical_output_stamps_exact_row_only(self):
        self.db.executescript("INSERT INTO docs VALUES('rebuilt','A','same','ready'); INSERT INTO docs VALUES('other','A','unknown','ready');")
        with patch.object(business, 'validated_cached_bytes', return_value=b'approved'):
            with self.checked() as db, db.raw_regeneration(b'approved', 'raw.eml', ('docs',)):
                db.execute("UPDATE docs SET body='same' WHERE id='rebuilt'")
        with self.checked() as db:
            self.assertEqual(db.execute("SELECT body FROM docs WHERE id='rebuilt'").fetchone()[0], 'same')
            with self.assertRaises(ProtectionError):
                db.execute("SELECT body FROM docs WHERE id='other'").fetchone()

    def test_raw_regeneration_does_not_allow_partial_old_content_update(self):
        self.db.execute('ALTER TABLE docs ADD COLUMN title TEXT')
        self.specs['docs']['content'] += ('title',)
        self.db.execute("INSERT INTO docs VALUES('old','A','unknown','ready','old title')")
        self.db.commit()
        with patch.object(business, 'validated_cached_bytes', return_value=b'approved'):
            with self.assertRaises(ProtectionError):
                with self.checked() as db, db.raw_regeneration(b'approved', 'raw.eml', ('docs',)):
                    db.execute("UPDATE docs SET title='new title' WHERE id='old'")

    def test_fts_external_tamper_rejected(self):
        with self.checked() as db:
            db.execute("INSERT INTO search(id,package,body) VALUES('d1','A','approved text')")
        self.db.execute("UPDATE search SET body='tampered text' WHERE id='d1'")
        self.db.commit()
        with self.assertRaises(ProtectionError) as caught:
            with self.checked() as db:
                db.execute("SELECT body FROM search WHERE search MATCH 'tampered'").fetchall()
        self.assertEqual(caught.exception.code, 'cache_output_mismatch')

    def test_trusted_metadata_projection_read_only_never_stamps(self):
        self.db.execute("INSERT INTO docs VALUES('old','A','unknown','ready')")
        self.db.commit()
        token = business.metadata_projection.set(True)
        try:
            with self.checked() as db:
                self.assertEqual(db.execute('SELECT body FROM docs').fetchone()[0], 'unknown')
                with self.assertRaises(ProtectionError):
                    db.execute("UPDATE docs SET body='new' WHERE id='old'")
        finally:
            business.metadata_projection.reset(token)
        with self.assertRaises(ProtectionError):
            with self.checked() as db:
                db.execute('SELECT body FROM docs').fetchall()


if __name__ == '__main__':
    unittest.main()
