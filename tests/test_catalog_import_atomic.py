"""The directory-to-research handoff must be atomic across API workers."""
import json
import hashlib
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_large_catalog_import as fixtures
PACKAGE, GROUP_A, GROUP_B = fixtures.PACKAGE, fixtures.GROUP_A, fixtures.GROUP_B
from services import large_package_catalog_import as bridge
from services.storage import Storage


class AtomicImportTests(unittest.TestCase):
    setUp = fixtures.LargeCatalogImportTests.setUp
    tearDown = fixtures.LargeCatalogImportTests.tearDown
    _export = fixtures.LargeCatalogImportTests._export
    _create = fixtures.LargeCatalogImportTests._create
    _count = fixtures.LargeCatalogImportTests._count

    def race(self, selections, keys):
        # Separate locks/connections represent distinct application processes.
        stores = [self.ordinary, Storage(os.path.join(self.dir, 'ordinary.db'))]
        barrier = threading.Barrier(2)
        original = bridge._inventory_records
        def synchronized(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier.wait(timeout=20)
            return result
        def create(i):
            try:
                return stores[i].create_scan_from_large_selection(
                    {'id': PACKAGE, 'root_path': '/tmp/lpimport-src', 'status': 'waiting_for_selection'},
                    selections[i], name='专项资料', parse_mode='accurate',
                    owner_id='o1', request_key=keys[i], large_store=self.large)
            except bridge.SelectionError as exc:
                return exc
        with patch.object(bridge, '_inventory_records', side_effect=synchronized):
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = [pool.submit(create,i) for i in range(2)]
                return [f.result(timeout=40) for f in tasks]

    def test_same_key_different_selection_is_conflict_even_when_racing(self):
        results = self.race([self._export(node_ids=[GROUP_A]),self._export(node_ids=[GROUP_B])],['same','same'])
        errors = [r for r in results if isinstance(r,bridge.SelectionError)]
        successes = [r for r in results if isinstance(r,dict)]
        self.assertEqual(len(errors),1, 'A request number cannot silently change the research scope')
        self.assertEqual(errors[0].status,409)
        self.assertEqual(len(successes),1)
        self.assertEqual(self._count('SELECT COUNT(*) FROM scans'),1)
        self.assertEqual(self._count('SELECT COUNT(*) FROM analysis_jobs'),1)
        self.assertEqual(self._count('SELECT COUNT(*) FROM large_catalog_imports'),1)

    def test_same_selection_different_keys_queues_only_one_job(self):
        selected=self._export(node_ids=[GROUP_A])
        results=self.race([selected,selected],['first','second'])
        self.assertTrue(all(isinstance(r,dict) for r in results))
        self.assertEqual(len({r['scan_id'] for r in results}),1)
        self.assertEqual(len({r['job_id'] for r in results}),1)
        self.assertEqual(sorted(r['reused'] for r in results),[False,True])
        self.assertEqual(self._count('SELECT COUNT(*) FROM analysis_jobs'),1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM inventory_entries WHERE kind='file'"),2)

    def test_same_key_same_selection_reuses_without_partial_rows(self):
        selected=self._export(node_ids=[GROUP_A])
        results=self.race([selected,selected],['same','same'])
        self.assertTrue(all(isinstance(r,dict) for r in results))
        self.assertEqual(len({r['scan_id'] for r in results}),1)
        for table in ('scans','analysis_jobs','import_tasks','large_catalog_imports','scan_selections','package_processing_controls'):
            self.assertEqual(self._count('SELECT COUNT(*) FROM '+table),1)

    def test_distinct_concurrent_selections_remain_distinct(self):
        results=self.race([self._export(node_ids=[GROUP_A]),self._export(node_ids=[GROUP_B])],['a','b'])
        self.assertEqual(len({r['scan_id'] for r in results}),2)
        self.assertFalse(any(r['reused'] for r in results))
        self.assertEqual(self._count('SELECT COUNT(*) FROM analysis_jobs'),2)

    def test_active_selection_older_than_fifty_imports_is_still_reused(self):
        selected=self._export(node_ids=[GROUP_A])
        first=self._create(selected,'original')
        with self.ordinary._connect() as conn:
            for i in range(55):
                scan='filler-'+str(i)
                conn.execute('INSERT INTO scans(id,root_path,payload,owner_id) VALUES(?,?,?,?)',(scan,'/tmp/lpimport-src','{}','o1'))
                conn.execute('INSERT INTO large_catalog_imports(id,large_package_id,scan_id,job_id,owner_id,request_key,request_payload,selection_snapshot,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (scan,PACKAGE,scan,None,'o1',scan,'{}',json.dumps({'selection_fingerprint':'unrelated-'+str(i)}),'queued',time.time()+i,time.time()))
        second=self._create(selected,'retry-key')
        self.assertEqual(second['scan_id'],first['scan_id'])
        self.assertTrue(second['reused'])

    def test_transaction_failure_leaves_no_partial_handoff(self):
        with self.ordinary._connect() as conn:
            conn.execute("CREATE TRIGGER fail_import BEFORE INSERT ON import_tasks BEGIN SELECT RAISE(ABORT,'simulated write failure'); END")
        with self.assertRaisesRegex(Exception,'simulated write failure'):
            self._create(self._export(node_ids=[GROUP_A]))
        for table in ('scans','analysis_jobs','import_tasks','large_catalog_imports','scan_selections','inventory_entries','tree_nodes'):
            self.assertEqual(self._count('SELECT COUNT(*) FROM '+table),0)

    def test_terminal_jobs_allow_new_request_but_same_key_remains_idempotent(self):
        selected = self._export(node_ids=[GROUP_A])
        for status in ('completed', 'partial', 'failed', 'cancelled', 'deleted'):
            with self.subTest(status=status):
                first = self._create(selected, 'before-' + status)
                with self.ordinary._connect() as conn:
                    conn.execute('UPDATE analysis_jobs SET status=? WHERE id=?', (status, first['job_id']))
                same = self._create(selected, 'before-' + status)
                self.assertEqual(same['scan_id'], first['scan_id'])
                after = self._create(selected, 'after-' + status)
                self.assertNotEqual(after['scan_id'], first['scan_id'])
                with self.ordinary._connect() as conn:
                    conn.execute('UPDATE analysis_jobs SET status=? WHERE id=?', (status, after['job_id']))

    def test_deleted_scan_can_be_recreated_with_same_key(self):
        selected = self._export(node_ids=[GROUP_A])
        first = self._create(selected, 'deleted')
        with self.ordinary._connect() as conn:
            conn.execute('DELETE FROM scans WHERE id=?', (first['scan_id'],))
        after = self._create(selected, 'deleted')
        self.assertFalse(after['reused'])
        self.assertNotEqual(after['scan_id'], first['scan_id'])
        self.assertEqual(self._count('SELECT COUNT(*) FROM large_catalog_imports'), 1)

    def test_same_key_and_paths_in_different_packages_do_not_alias(self):
        selected = self._export(node_ids=[GROUP_A])
        first = self._create(selected, 'shared')
        second = self.ordinary.create_scan_from_large_selection(
            {'id': 'different-package', 'root_path': '/tmp/other-package', 'status': 'waiting_for_selection'},
            selected, name='另一包资料', parse_mode='accurate', owner_id='o1', request_key='shared')
        self.assertNotEqual(first['scan_id'], second['scan_id'])
        self.assertFalse(second['reused'])

    def test_malformed_nonobject_snapshot_is_not_silently_reused(self):
        selected = self._export(node_ids=[GROUP_A])
        first = self._create(selected, 'broken')
        with self.ordinary._connect() as conn:
            conn.execute('UPDATE large_catalog_imports SET selection_snapshot=? WHERE scan_id=?', ('[]', first['scan_id']))
        with self.assertRaises(bridge.SelectionError) as ctx:
            self._create(selected, 'broken')
        self.assertEqual(ctx.exception.status, 409)
        after = self._create(selected, 'new-key')
        self.assertNotEqual(after['scan_id'], first['scan_id'])

    def test_three_language_real_files_handoff_parse_persist_and_retrieve(self):
        # Synthetic, non-sensitive source files; real exporter/parser/storage/
        # retrieval. No LLM or automatic worker execution is claimed here.
        from services.unified_parser import UnifiedDocumentParser
        from services.retrieval import retrieve_evidence
        texts = {
            '中文/交付计划.txt': ('轨道巡检项目计划在十月进行验收。负责团队需要检查传感器校准记录、轨道巡检日志和安全培训记录。发现的问题必须填写整改日期和负责人，完成复核后才可提交最终验收报告。', '轨道巡检'),
            'English/maintenance.txt': ('The hydraulic pressure inspection is scheduled for October. The maintenance team must compare hydraulic pressure readings with the approved operating range. Record each deviation, assign an owner, and verify corrective actions before signing the acceptance report.', 'hydraulic pressure'),
            'ไทย/แผนงาน.txt': ('แผนการตรวจสอบคลังสินค้าประจำเดือนตุลาคม ทีมงานต้องตรวจสอบรายการสินค้าในคลังสินค้าและบันทึกผลการตรวจสอบ หากพบข้อผิดพลาดให้กำหนดผู้รับผิดชอบและวันที่แก้ไข จากนั้นตรวจสอบความถูกต้องอีกครั้งก่อนส่งรายงานสรุปให้ผู้จัดการ', 'คลังสินค้า'),
        }
        root = Path(self.dir) / 'real-source'
        hashes = {}
        with closing(self.large._connect()) as conn:
            conn.execute('DELETE FROM large_catalog_members WHERE package_id=?', (PACKAGE,))
            conn.execute('DELETE FROM files WHERE package_id=?', (PACKAGE,))
            conn.execute('UPDATE packages SET root_path=? WHERE id=?', (str(root), PACKAGE))
            for rel, (text, _) in texts.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf8')
                hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
                conn.execute('INSERT INTO files(package_id,path,name,extension,size,modified_at_ns,sensitive,quick_status,updated_at,sha256) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (PACKAGE, rel, path.name, '.txt', path.stat().st_size, path.stat().st_mtime_ns, 0, 'completed', time.time(), hashes[rel]))
                conn.execute('INSERT INTO large_catalog_members(package_id,path,vector,input_hash,group_id) VALUES(?,?,?,?,?)',
                    (PACKAGE, rel, b'', hashes[rel], GROUP_A))
            conn.commit()
        selected = self._export(node_ids=[GROUP_A])
        imported = self.ordinary.create_scan_from_large_selection(
            {'id': PACKAGE, 'root_path': str(root), 'status': 'waiting_for_selection'}, selected,
            name='三语证据验收', parse_mode='accurate', owner_id='o1', request_key='three-languages', large_store=self.large)
        scan = imported['scan_id']
        self.assertEqual(imported['selected_file_count'], len(texts))
        with self.ordinary._connect() as conn:
            inventory = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM inventory_entries WHERE scan_id=? AND kind='file'", (scan,))]
        self.assertEqual({item['path'] for item in inventory}, set(texts))
        parser = UnifiedDocumentParser()
        for item in inventory:
            rel = item['path']
            self.assertEqual(item['source_large_package_id'], PACKAGE)
            self.assertEqual(item['source_path'], rel)
            self.assertEqual(item['sha256'], hashes[rel])
            document = parser.parse(root / rel, relative_path=rel, mode='accurate')
            self.assertIn(texts[rel][0], document['text'])
            self.assertTrue(document['coverage']['complete'])
            self.assertFalse(document['coverage']['truncated_by_limit'])
            self.assertEqual(document['source']['sha256'], hashes[rel])
            self.ordinary.save_document(scan, rel, document)
        documents = self.ordinary.list_documents(scan)
        self.assertEqual(len(documents), len(texts))
        for rel, (_, query) in texts.items():
            with self.subTest(language_path=rel):
                found = retrieve_evidence(documents, query, top_k=3)['results']
                self.assertTrue(found, rel)
                evidence = found[0]
                self.assertEqual(evidence['source_path'], rel)
                self.assertTrue(evidence['evidence_id'])
                self.assertIn(query, evidence['text'])
                self.assertEqual(evidence['source_sha256'], hashes[rel])
                self.assertFalse(evidence['preview_only'])
                self.assertEqual(hashlib.sha256((root / rel).read_bytes()).hexdigest(), hashes[rel])


if __name__=='__main__':
    unittest.main()
