# -*- coding: utf-8 -*-
"""大数据包智能目录 → 普通数据包导入（设计文档 Phase 1）的回归测试。

覆盖文档 §13 的验收项里可自动化验证的部分：
  · 选择叶节点 / 父主题节点都能导出到普通导入；
  · 多节点选择自动去重；
  · 排除单个文件；
  · 幂等：同 request_key 返回第一次的任务；同号不同参返回 409；
  · 安全：路径穿越、临时节点（preview:/unread:）、空选择、超长名称被拒；
  · 一致性：普通清单与节点展开的文件数一致；创建普通 scan 不改动原大包任务；
  · 引用：`referenced_by()` 能检测到大包被普通数据包引用（删除保护的基础）。
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.large_package_isolated import LargePackageStore           # noqa: E402
from services.storage import Storage                                    # noqa: E402
from services import large_package_catalog_import as bridge             # noqa: E402

PACKAGE = 'lp-import-test'
GROUP_A = 'km-import-a'
GROUP_B = 'km-import-b'
FILES = [
    ('政策/文件1.pdf', GROUP_A),
    ('政策/文件2.docx', GROUP_A),
    ('会议/记录3.xlsx', GROUP_B),
    ('会议/剔除4.pdf', GROUP_B),
]


class LargeCatalogImportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='lpimport-')
        self.large = LargePackageStore(db_path=os.path.join(self.dir, 'large.db'))
        self.ordinary = Storage(os.path.join(self.dir, 'ordinary.db'))
        # Storage.__init__ 内部已经调用 _init_db() 建表，这里不需要额外初始化。
        now = time.time()
        with closing(self.large._connect()) as c:
            c.execute("INSERT INTO packages(id,root_path,owner_id,status,phase,progress,counts,"
                      "created_at,updated_at,control_state,control_generation) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (PACKAGE, '/tmp/lpimport-src', 'o1', 'waiting_for_selection',
                       'catalog_ready', 70, '{}', now, now, 'active', 0))
            for gid, name in ((GROUP_A, '政策文件'), (GROUP_B, '会议记录')):
                c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                          "result,status,updated_at,member_count,content_revision) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (PACKAGE, gid, 'm', b'', 'rep.pdf', '{}', 'named', now, 2, 1))
                c.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,"
                          "description,evidence,model,digest,needs_review,locked,updated) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (PACKAGE, gid, 1, 1, name, '说明', '[]', 'm', 'd-' + gid, 0, 0, now))
            for path, gid in FILES:
                c.execute("INSERT INTO files(package_id,path,name,extension,size,modified_at_ns,"
                          "sensitive,quick_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                          (PACKAGE, path, os.path.basename(path),
                           os.path.splitext(path)[1], 1024, 0, 0, 'completed', now))
                c.execute("INSERT INTO large_catalog_members(package_id,path,vector,input_hash,group_id) "
                          "VALUES(?,?,?,?,?)", (PACKAGE, path, b'', 'h-' + path, gid))
            c.commit()

    def tearDown(self):
        for root, _dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            os.rmdir(root)

    # ---------- helpers ----------
    def _export(self, node_ids=None, paths=None, exclude_paths=None):
        return self.large.export_selection(PACKAGE, node_ids=node_ids or [],
                                           paths=paths or [], exclude_paths=exclude_paths or [])

    def _create(self, selection, request_key='rk-1', name='专项资料', parse_mode='accurate'):
        return self.ordinary.create_scan_from_large_selection(
            {'id': PACKAGE, 'root_path': '/tmp/lpimport-src', 'status': 'waiting_for_selection'},
            selection, name=name, parse_mode=parse_mode, owner_id='o1', request_key=request_key,
        )

    def _count(self, sql, args=()):
        with self.ordinary._connect() as c:
            return c.execute(sql, args).fetchone()[0]

    def _validate(self, payload):
        return bridge.validate_request(payload, {'status': 'waiting_for_selection'})

    # ---------- 用例：核心创建 ----------
    def test_creates_scan_inventory_and_queued_job(self):
        sel = self._export(node_ids=[GROUP_A])
        out = self._create(sel)
        self.assertTrue(out['scan_id'].startswith('scan-'))
        self.assertFalse(out['reused'])
        self.assertEqual(out['selected_file_count'], 2)
        self.assertEqual(out['selected_bytes'], 2048)

        self.assertEqual(self._count("SELECT COUNT(*) FROM scans WHERE id=?", (out['scan_id'],)), 1)
        # 清单 = 根目录 + 中间目录（政策）+ 2 个文件
        self.assertEqual(self._count("SELECT COUNT(*) FROM inventory_entries WHERE scan_id=?",
                                     (out['scan_id'],)), 4)
        self.assertEqual(self._count("SELECT COUNT(*) FROM inventory_entries WHERE scan_id=? "
                                     "AND kind='directory'", (out['scan_id'],)), 2)

        with self.ordinary._connect() as c:
            row = c.execute("SELECT status,included_paths FROM scan_selections WHERE scan_id=?",
                            (out['scan_id'],)).fetchone()
        self.assertEqual(row['status'], 'confirmed')
        self.assertEqual(sorted(json.loads(row['included_paths'])),
                         ['政策/文件1.pdf', '政策/文件2.docx'])

        with self.ordinary._connect() as c:
            task = c.execute("SELECT status,total_files,root_path FROM import_tasks WHERE scan_id=?",
                             (out['scan_id'],)).fetchone()
            job = c.execute("SELECT task_type,status,options FROM analysis_jobs WHERE id=?",
                            (out['job_id'],)).fetchone()
            ctrl = c.execute("SELECT state FROM package_processing_controls WHERE scan_id=?",
                             (out['scan_id'],)).fetchone()
        self.assertEqual(task['status'], 'parsing_selected')
        self.assertEqual(task['total_files'], 2)
        self.assertEqual(task['root_path'], '/tmp/lpimport-src')
        self.assertEqual(job['task_type'], 'analyze_package')
        self.assertEqual(job['status'], 'queued')
        options = json.loads(job['options'])
        self.assertEqual(options['workflow_source'], 'selected_file_parse')
        self.assertFalse(options['candidate_preview'])
        self.assertIsNone(options['deep_batch_id'])

        self.assertEqual(sorted(options['target_paths']), ['政策/文件1.pdf', '政策/文件2.docx'])
        self.assertEqual(options['source_large_package_id'], PACKAGE)
        self.assertIn(GROUP_A, options['source_node_ids'])
        self.assertEqual(ctrl['state'], 'running')

    def test_scan_payload_records_source_and_snapshot(self):
        sel = self._export(node_ids=[GROUP_A])
        out = self._create(sel)
        with self.ordinary._connect() as c:
            payload = json.loads(c.execute("SELECT payload FROM scans WHERE id=?",
                                           (out['scan_id'],)).fetchone()[0])
            bridge_row = c.execute("SELECT * FROM large_catalog_imports WHERE scan_id=?",
                                   (out['scan_id'],)).fetchone()
        self.assertEqual(payload['source_kind'], 'large_catalog_selection')
        self.assertEqual(payload['source_large_package_id'], PACKAGE)
        self.assertEqual(payload['source_node_names'], ['政策文件'])
        snapshot = json.loads(bridge_row['selection_snapshot'])
        self.assertEqual(snapshot['selected_file_count'], 2)
        self.assertTrue(snapshot['selection_fingerprint'])
        self.assertEqual(bridge_row['large_package_id'], PACKAGE)

    # ---------- 用例：节点展开与去重 ----------
    def test_parent_and_leaf_nodes_deduplicate(self):
        sel = self._export(node_ids=[GROUP_A, GROUP_A, GROUP_B])
        out = self._create(sel)
        self.assertEqual(out['selected_file_count'], len(FILES))

    def test_excluded_file_is_dropped(self):
        sel = self._export(node_ids=[GROUP_B], exclude_paths=['会议/剔除4.pdf'])
        out = self._create(sel)
        self.assertEqual(out['selected_file_count'], 1)
        with self.ordinary._connect() as c:
            paths = [r[0] for r in c.execute("SELECT node_path FROM inventory_entries "
                                             "WHERE scan_id=? AND kind='file'", (out['scan_id'],))]
        self.assertEqual(sorted(paths), ['会议/记录3.xlsx'])

    def test_manual_paths_are_supported(self):
        sel = self._export(paths=['会议/记录3.xlsx'])
        out = self._create(sel)
        self.assertEqual(out['selected_file_count'], 1)

    # ---------- 用例：幂等 ----------
    def test_same_request_key_returns_first_task(self):
        sel = self._export(node_ids=[GROUP_A])
        first = self._create(sel, request_key='rk-dup')
        again = self._create(sel, request_key='rk-dup')
        self.assertEqual(again['scan_id'], first['scan_id'])
        self.assertEqual(again['job_id'], first['job_id'])
        self.assertTrue(again['reused'])
        self.assertEqual(self._count("SELECT COUNT(*) FROM large_catalog_imports"), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM analysis_jobs"), 1)

    def test_same_request_key_with_different_selection_conflicts(self):
        self._create(self._export(node_ids=[GROUP_A]), request_key='rk-x')
        with self.assertRaises(bridge.SelectionError) as ctx:
            self._create(self._export(node_ids=[GROUP_B]), request_key='rk-x')
        self.assertEqual(ctx.exception.status, 409)

    # ---------- 用例：安全校验 ----------
    def test_rejects_path_traversal_and_absolute_paths(self):
        for bad in (['../etc/passwd'], ['/etc/passwd'], ['a/../../b']):
            with self.assertRaises(bridge.SelectionError):
                self._validate({'node_ids': [], 'paths': bad, 'name': 'x', 'request_key': 'k'})

    def test_rejects_preview_and_unread_nodes(self):
        for node in ('preview:abc', 'unread:abc'):
            with self.assertRaises(bridge.SelectionError) as ctx:
                self._validate({'node_ids': [node], 'name': 'x', 'request_key': 'k'})
            self.assertEqual(ctx.exception.status, 409)

    def test_rejects_empty_selection_and_bad_name_and_mode(self):
        with self.assertRaises(bridge.SelectionError):
            self._validate({'node_ids': [], 'paths': [], 'name': 'x', 'request_key': 'k'})
        with self.assertRaises(bridge.SelectionError):
            self._validate({'node_ids': [GROUP_A], 'name': '', 'request_key': 'k'})
        with self.assertRaises(bridge.SelectionError):
            self._validate({'node_ids': [GROUP_A], 'name': 'x' * 121, 'request_key': 'k'})
        with self.assertRaises(bridge.SelectionError):
            self._validate({'node_ids': [GROUP_A], 'name': 'x', 'request_key': 'k',
                            'parse_mode': 'ultra'})
        with self.assertRaises(bridge.SelectionError):
            self._validate({'node_ids': [GROUP_A], 'name': 'x'})

    def test_rejects_unavailable_large_package(self):
        with self.assertRaises(bridge.SelectionError) as ctx:
            bridge.validate_request({'node_ids': ['g'], 'name': 'x', 'request_key': 'k'},
                                    {'status': 'cancelled'})
        self.assertEqual(ctx.exception.status, 409)

    def test_unknown_node_is_rejected(self):
        with self.assertRaises(ValueError):
            self.large.export_selection(PACKAGE, node_ids=['km-does-not-exist'])

    # ---------- 用例：一致性 / 生命周期 ----------
    def test_creating_scan_does_not_touch_large_package(self):
        before = self._count_large("SELECT COUNT(*) FROM lp_catalog_jobs")
        self._create(self._export(node_ids=[GROUP_A]))
        self.assertEqual(self._count_large("SELECT COUNT(*) FROM lp_catalog_jobs"), before)
        self.assertEqual(self._count_large("SELECT status FROM packages WHERE id=?", (PACKAGE,)),
                         'waiting_for_selection')

    def _count_large(self, sql, args=()):
        with closing(self.large._connect()) as c:
            row = c.execute(sql, args).fetchone()
        return row[0] if row else None

    def test_reference_lookup_supports_delete_protection(self):
        self.assertFalse(bridge.referenced_by(self.ordinary, PACKAGE))
        out = self._create(self._export(node_ids=[GROUP_A]))
        self.assertTrue(bridge.referenced_by(self.ordinary, PACKAGE))
        refs = bridge.references_for(self.ordinary, PACKAGE)
        self.assertEqual([r['scan_id'] for r in refs], [out['scan_id']])
        self.assertFalse(bridge.referenced_by(self.ordinary, 'lp-other'))

    def test_snapshot_is_immutable_after_node_rename(self):
        """目录节点改名不改变已创建普通数据包的文件集合（文档 §9.3）。"""
        out = self._create(self._export(node_ids=[GROUP_A]))
        with closing(self.large._connect()) as c:
            c.execute("UPDATE lp_catalog_labels SET name='改名后的主题' WHERE package_id=? AND group_id=?",
                      (PACKAGE, GROUP_A))
            c.commit()
        with self.ordinary._connect() as c:
            paths = sorted(r[0] for r in c.execute(
                "SELECT node_path FROM inventory_entries WHERE scan_id=? AND kind='file'",
                (out['scan_id'],)))
        self.assertEqual(sorted(paths), ['政策/文件1.pdf', '政策/文件2.docx'])


if __name__ == '__main__':
    unittest.main()
