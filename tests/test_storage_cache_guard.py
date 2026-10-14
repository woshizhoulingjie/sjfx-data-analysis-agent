"""Actual ordinary Storage: historical caches, projections, FTS and sidecars."""
import copy
import gzip
import json
import sqlite3
import unittest
from pathlib import Path

import test_package_cache_provenance as provenance
from services.storage import Storage
from sjfx_protection import business
from sjfx_protection.store import ProtectionError


class StorageCacheTests(unittest.TestCase):
    def setUp(self):
        provenance.CacheProvenanceTests.setUp(self)
        self.root = Path(self.tmp.name) / 'input'
        self.root.mkdir()
        self.package._registration = lambda owner, identifier: {
            'owner': owner, 'id': identifier, 'root': str(self.root)}
        self.package.resolve = lambda owner, root: {'id': 'A'}
        self.p.enabled = False
        self.storage = Storage(Path(self.tmp.name) / 'analysis.db', sidecar_threshold=32768)
        self.storage.save_scan({'root': str(self.root), 'tree': {}, 'files': []},
                               scan_id='scan-A', owner_id='alice')
        self.p.enabled = True

    def tearDown(self):
        provenance.CacheProvenanceTests.tearDown(self)

    def raw(self, sql, args=()):
        db = sqlite3.connect(self.storage.db_path)
        try:
            result = db.execute(sql, args).fetchall()
            db.commit()
            return result
        finally:
            db.close()

    def test_new_summary_and_document_roundtrip(self):
        self.storage.save_summary('scan-A', 'a.txt', 'file', {'summary': 'approved summary'})
        self.assertIn('approved summary', str(self.storage.get_summary('scan-A', 'a.txt', 'file')))
        doc = {'source': {'path': 'a.txt'}, 'text': 'approved document', 'evidence': []}
        self.storage.save_document('scan-A', 'a.txt', doc)
        self.assertEqual(self.storage.get_document('scan-A', 'a.txt')['text'], doc['text'])

    def test_historical_cache_cannot_be_blessed_by_registration(self):
        self.p.enabled = False
        self.storage.save_summary('scan-A', 'old.txt', 'file', {'summary': 'historical text'})
        self.p.enabled = True
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_summary('scan-A', 'old.txt', 'file')
        self.assertEqual(caught.exception.code, 'cache_provenance_missing')

    def test_summary_json_projection_tamper_rejected(self):
        self.storage.save_summary('scan-A', 'a.txt', 'file', {'summary': 'approved'})
        self.raw("UPDATE summaries SET payload=? WHERE scan_id=?", (json.dumps({'summary': 'forged'}), 'scan-A'))
        with self.assertRaises(ProtectionError) as caught:
            with self.storage._connect() as db:
                db.execute("SELECT json_extract(payload,'$.summary') FROM summaries WHERE scan_id=?", ('scan-A',)).fetchall()
        self.assertEqual(caught.exception.code, 'cache_output_mismatch')

    def test_v1_cache_not_current_v2(self):
        self.storage.save_document('scan-A', 'a.txt', {'text': 'v1'})
        v2 = provenance.context(version='v2')
        self.package.current['A'] = copy.deepcopy(v2)
        business.execution.set(v2)
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_document('scan-A', 'a.txt')
        self.assertEqual(caught.exception.code, 'context_stale')

    def test_actual_scan_ownership_filters_other_rows(self):
        self.p.enabled = False
        self.storage.save_scan({'root': str(self.root), 'tree': {}}, scan_id='scan-B', owner_id='bob')
        self.storage.save_document('scan-B', 'b.txt', {'text': 'bob secret'})
        self.p.enabled = True
        with self.storage._connect() as db:
            self.assertEqual(db.execute("SELECT json_extract(payload,'$.text') FROM unified_documents WHERE scan_id='scan-B'").fetchall(), [])

    def test_parent_context_cannot_cover_registered_child(self):
        self.p.enabled = False
        child = self.root / 'child'
        child.mkdir()
        self.storage.save_scan({'root': str(child), 'tree': {}}, scan_id='scan-child', owner_id='alice')
        self.storage.save_document('scan-child', 'b.txt', {'text': 'child secret'})
        self.p.enabled = True
        self.package.resolve = lambda owner, root: {'id': 'B' if Path(root).is_relative_to(child) else 'A'}
        with self.storage._connect() as db:
            self.assertEqual(db.execute("SELECT json_extract(payload,'$.text') FROM unified_documents WHERE scan_id='scan-child'").fetchall(), [])
        with self.assertRaises(ProtectionError):
            self.storage.save_document('scan-child', 'b.txt', {'text': 'wrong parent producer'})

    def test_sidecar_exact_bytes_and_projection(self):
        doc = {'source': {'path': 'a.txt'}, 'text': 'approved ' * 10000, 'evidence': []}
        self.storage.save_document('scan-A', 'a.txt', doc)
        self.assertEqual(self.storage.get_document('scan-A', 'a.txt')['text'], doc['text'])
        pointer = json.loads(self.raw('SELECT payload FROM unified_documents')[0][0])
        with gzip.open(str(self.storage.sidecar_dir / pointer['file']), 'wb') as stream:
            stream.write(json.dumps({'text': 'forged sidecar'}).encode())
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_document('scan-A', 'a.txt')
        self.assertEqual(caught.exception.code, 'cache_output_changed')

    def test_missing_sidecar_refuses_instead_of_empty_text(self):
        self.storage.save_document('scan-A', 'a.txt', {'text': 'long ' * 10000})
        pointer = json.loads(self.raw('SELECT payload FROM unified_documents')[0][0])
        (self.storage.sidecar_dir / pointer['file']).unlink()
        with self.assertRaises(ProtectionError) as caught:
            self.storage.get_document('scan-A', 'a.txt')
        self.assertEqual(caught.exception.code, 'cache_unavailable')

    def test_fts_real_content_and_old_index_block(self):
        self.storage.replace_evidence_index('scan-A', [{'source_path': 'a.txt', 'evidence_id': 'e1', 'text': 'approved keyword evidence'}])
        rows = self.storage.search_evidence_index('scan-A', 'keyword')
        self.assertTrue(rows)
        self.raw("UPDATE evidence_fts SET text='forged keyword'")
        with self.assertRaises(ProtectionError) as caught:
            self.storage.search_evidence_index('scan-A', 'keyword')
        self.assertEqual(caught.exception.code, 'cache_output_mismatch')

    def test_off_existing_use_without_context_or_registration(self):
        self.p.enabled = False
        business.execution.set(None)
        self.storage.save_document('scan-A', 'a.txt', {'text': 'normal old use'})
        self.assertEqual(self.storage.get_document('scan-A', 'a.txt')['text'], 'normal old use')


if __name__ == '__main__':
    unittest.main()
