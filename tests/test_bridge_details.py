# -*- coding: utf-8 -*-
"""设计文档 Phase 1 余下条款 + Phase 3 稳定性条款的回归测试。

§8.1 敏感文件排除
§8.2 压缩包内部成员首版拒绝
§9.2 文件指纹 + `detect_source_changes()` 标记 source_changed
§10  桥接状态机轨迹
§11  节点/文件缺失检查（路由据此返回 404 / 409）
§5.3 活动任务按选择指纹去重（重复点击不产生重复任务）
Phase 3 大节点批量写入（不逐文件提交）
"""
import json
import os
import sys
import time
import unittest
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import large_package_catalog_import as bridge             # noqa: E402
from test_large_catalog_import import (                                 # noqa: E402
    LargeCatalogImportTests, PACKAGE, GROUP_A, GROUP_B)

GROUP_C = 'km-import-c'      # 只放压缩包内部成员


class BridgeDetailTests(LargeCatalogImportTests):
    """复用父类夹具（大包 + 普通库 + 4 个文件），只加新条款的用例。"""

    # ---------- 夹具辅助 ----------
    def _create(self, selection, request_key='rk-1', name='专项资料', parse_mode='accurate'):
        """与路由完全同一条调用路径：Storage 薄委托 + large_store 透传。"""
        return self.ordinary.create_scan_from_large_selection(
            {'id': PACKAGE, 'root_path': '/tmp/lpimport-src', 'status': 'waiting_for_selection'},
            selection, name=name, parse_mode=parse_mode, owner_id='o1',
            request_key=request_key, large_store=self.large,
        )

    def _add_group(self, gid, name):
        now = time.time()
        with closing(self.large._connect()) as c:
            c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                      "result,status,updated_at,member_count,content_revision) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (PACKAGE, gid, 'm', b'', 'rep.pdf', '{}', 'named', now, 2, 1))
            c.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,"
                      "description,evidence,model,digest,needs_review,locked,updated) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (PACKAGE, gid, 1, 1, name, '说明', '[]', 'm', 'd-' + gid, 0, 0, now))
            c.commit()

    def _add_files(self, rows):
        """rows = [(path, group_id, size, sha256), ...]，批量插入。"""
        now = time.time()
        with closing(self.large._connect()) as c:
            c.executemany(
                "INSERT INTO files(package_id,path,name,extension,size,modified_at_ns,"
                "sensitive,quick_status,updated_at,sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(PACKAGE, path, os.path.basename(path), os.path.splitext(path)[1], size, 0, 0,
                  'completed', now, sha) for path, _gid, size, sha in rows])
            c.executemany(
                "INSERT INTO large_catalog_members(package_id,path,vector,input_hash,group_id) "
                "VALUES(?,?,?,?,?)",
                [(PACKAGE, path, b'', 'h-' + path, gid) for path, gid, _s, _h in rows])
            c.commit()

    def _set_sensitive(self, path, value=1):
        with closing(self.large._connect()) as c:
            c.execute("UPDATE files SET sensitive=? WHERE package_id=? AND path=?",
                      (value, PACKAGE, path))
            c.commit()

    def _file_payloads(self, scan_id):
        with self.ordinary._connect() as c:
            return [json.loads(r[0]) for r in c.execute(
                "SELECT payload FROM inventory_entries WHERE scan_id=? AND kind='file'",
                (scan_id,))]

    def _snapshot(self, scan_id):
        with self.ordinary._connect() as c:
            return json.loads(c.execute(
                "SELECT selection_snapshot FROM large_catalog_imports WHERE scan_id=?",
                (scan_id,)).fetchone()[0])

    # ---------- §8.1 敏感文件 ----------
    def test_sensitive_file_is_excluded(self):
        self._set_sensitive('政策/文件2.docx')
        out = self._create(self._export(node_ids=[GROUP_A]))
        self.assertEqual(out['selected_file_count'], 1)
        self.assertEqual([i['path'] for i in self._file_payloads(out['scan_id'])],
                         ['政策/文件1.pdf'])
        self.assertEqual(self._snapshot(out['scan_id'])['excluded']['sensitive'], 1)

    def test_all_sensitive_selection_is_rejected(self):
        for path in ('政策/文件1.pdf', '政策/文件2.docx'):
            self._set_sensitive(path)
        with self.assertRaises(bridge.SelectionError) as ctx:
            self._create(self._export(node_ids=[GROUP_A]))
        self.assertIn('敏感', str(ctx.exception))
        self.assertEqual(self._count("SELECT COUNT(*) FROM scans"), 0)

    # ---------- §8.2 压缩包内部成员 ----------
    def test_archive_member_detection(self):
        self.assertTrue(bridge.looks_like_archive_member('包.zip/doc/a.pdf'))
        self.assertTrue(bridge.looks_like_archive_member('a/b.rar/x/y.docx'))
        self.assertFalse(bridge.looks_like_archive_member('政策/文件1.pdf'))

    def test_archive_member_is_excluded_from_mixed_selection(self):
        self._add_files([('包.zip/doc/a.pdf', GROUP_B, 512, None)])
        out = self._create(self._export(node_ids=[GROUP_B]))
        self.assertEqual(out['selected_file_count'], 2)
        self.assertEqual(self._snapshot(out['scan_id'])['excluded']['archive_member'], 1)

    def test_all_archive_member_selection_is_rejected(self):
        self._add_group(GROUP_C, '压缩包内容')
        self._add_files([('包.zip/doc/a.pdf', GROUP_C, 512, None)])
        with self.assertRaises(bridge.SelectionError) as ctx:
            self._create(self._export(node_ids=[GROUP_C]))
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn('压缩包', str(ctx.exception))
        self.assertEqual(self._count("SELECT COUNT(*) FROM scans"), 0)

    # ---------- §9.2 指纹与源变化 ----------
    def test_inventory_records_fingerprint(self):
        out = self._create(self._export(node_ids=[GROUP_A]))
        for item in self._file_payloads(out['scan_id']):
            self.assertTrue(item.get('fingerprint'), '每条文件记录都必须带指纹')
            self.assertTrue(item['fingerprint'].startswith(('sha256:', 'meta:')))
            self.assertEqual(item['source_large_package_id'], PACKAGE)
            self.assertEqual(item['source_path'], item['path'])

    def test_sha256_is_preferred_when_available(self):
        self._add_files([('政策/带哈希.pdf', GROUP_A, 2048, 'deadbeef')])
        out = self._create(self._export(node_ids=[GROUP_A]))
        marked = [i for i in self._file_payloads(out['scan_id'])
                  if i['path'] == '政策/带哈希.pdf']
        self.assertEqual(marked[0]['fingerprint'], 'sha256:deadbeef')

    def test_detect_source_changes_is_clean_when_nothing_changed(self):
        out = self._create(self._export(node_ids=[GROUP_A]))
        self.assertEqual(bridge.detect_source_changes(self.ordinary, self.large, out['scan_id']), [])
        with self.ordinary._connect() as c:
            payload = json.loads(c.execute("SELECT payload FROM scans WHERE id=?",
                                           (out['scan_id'],)).fetchone()[0])
        self.assertFalse(payload['source_changed'])
        self.assertEqual(payload['source_changed_files'], [])

    def test_detect_source_changes_marks_modified_file(self):
        out = self._create(self._export(node_ids=[GROUP_A]))
        with closing(self.large._connect()) as c:
            c.execute("UPDATE files SET size=size+1 WHERE package_id=? AND path=?",
                      (PACKAGE, '政策/文件1.pdf'))
            c.commit()
        changed = bridge.detect_source_changes(self.ordinary, self.large, out['scan_id'])
        self.assertEqual([c0['path'] for c0 in changed], ['政策/文件1.pdf'])
        self.assertEqual(changed[0]['reason'], 'changed')
        with self.ordinary._connect() as c:
            payload = json.loads(c.execute("SELECT payload FROM scans WHERE id=?",
                                           (out['scan_id'],)).fetchone()[0])
        self.assertTrue(payload['source_changed'], '必须显式标记 source_changed')
        self.assertEqual(len(payload['source_changed_files']), 1)

    def test_detect_source_changes_marks_missing_file(self):
        out = self._create(self._export(node_ids=[GROUP_A]))
        with closing(self.large._connect()) as c:
            c.execute("DELETE FROM files WHERE package_id=? AND path=?",
                      (PACKAGE, '政策/文件2.docx'))
            c.commit()
        changed = bridge.detect_source_changes(self.ordinary, self.large, out['scan_id'])
        self.assertEqual([c0['reason'] for c0 in changed], ['missing'])

    # ---------- §10 状态机 ----------
    def test_state_trace_is_recorded(self):
        out = self._create(self._export(node_ids=[GROUP_A]))
        snapshot = self._snapshot(out['scan_id'])
        self.assertEqual([step['state'] for step in snapshot['state_trace']],
                         ['created', 'validating', 'resolving_selection',
                          'materializing_scan', 'queued'])
        self.assertEqual(snapshot['excluded'], {'sensitive': 0, 'archive_member': 0})
        self.assertEqual(snapshot['selected_file_count'], 2)

    # ---------- §11 缺失检查 ----------
    def test_missing_nodes_helper(self):
        self.assertEqual(bridge.missing_nodes(self.large, PACKAGE, [GROUP_A, GROUP_B]), [])
        self.assertEqual(bridge.missing_nodes(self.large, PACKAGE, ['km-nope']), ['km-nope'])

    def test_missing_paths_helper(self):
        self.assertEqual(bridge.missing_paths(self.large, PACKAGE, ['政策/文件1.pdf']), [])
        self.assertEqual(bridge.missing_paths(self.large, PACKAGE, ['政策/没了.pdf']),
                         ['政策/没了.pdf'])

    # ---------- §5.3 幂等与并发 ----------
    def test_same_selection_with_new_request_key_reuses_active_job(self):
        first = self._create(self._export(node_ids=[GROUP_A]), request_key='rk-a')
        again = self._create(self._export(node_ids=[GROUP_A]), request_key='rk-b')
        self.assertEqual(again['scan_id'], first['scan_id'],
                         '同一文件集合且任务仍活动 → 复用，不重复建包')
        self.assertTrue(again['reused'])
        self.assertEqual(self._count("SELECT COUNT(*) FROM scans"), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM analysis_jobs"), 1)

    def test_finished_job_is_not_reused(self):
        first = self._create(self._export(node_ids=[GROUP_A]), request_key='rk-a')
        with self.ordinary._connect() as c:
            c.execute("UPDATE analysis_jobs SET status='completed' WHERE id=?", (first['job_id'],))
            c.execute("UPDATE import_tasks SET status='completed' WHERE scan_id=?",
                      (first['scan_id'],))
        again = self._create(self._export(node_ids=[GROUP_A]), request_key='rk-b')
        self.assertNotEqual(again['scan_id'], first['scan_id'], '已完成的任务不应被复用')
        self.assertFalse(again['reused'])

    def test_different_selection_creates_new_scan(self):
        self._create(self._export(node_ids=[GROUP_A]), request_key='rk-a')
        other = self._create(self._export(node_ids=[GROUP_B]), request_key='rk-b')
        self.assertFalse(other['reused'])
        self.assertEqual(self._count("SELECT COUNT(*) FROM scans"), 2)

    # ---------- Phase 3 大节点批量写入 ----------
    def test_large_node_is_written_in_one_batch(self):
        rows = [('大批量/文件%05d.pdf' % i, GROUP_A, 100 + i, None) for i in range(2000)]
        self._add_files(rows)
        started = time.time()
        out = self._create(self._export(node_ids=[GROUP_A]), request_key='rk-big')
        elapsed = time.time() - started
        self.assertEqual(out['selected_file_count'], 2002)
        self.assertEqual(self._count("SELECT COUNT(*) FROM inventory_entries WHERE scan_id=?",
                                     (out['scan_id'],)), 2005)   # 2002 文件 + 根 + 政策 + 大批量
        self.assertLess(elapsed, 30.0, '大节点导入不应退化成逐文件提交')
        # 一个事务：不存在半成品（清单条目数与声明一致）
        self.assertEqual(self._count("SELECT COUNT(*) FROM large_catalog_imports WHERE scan_id=?",
                                     (out['scan_id'],)), 1)

    # ---------- §9.2 解析入口接线 ----------
    def test_source_change_check_runs_once_per_job(self):
        out = self._create(self._export(node_ids=[GROUP_A]))

        def payload_now():
            with self.ordinary._connect() as c:
                return json.loads(c.execute("SELECT payload FROM scans WHERE id=?",
                                            (out['scan_id'],)).fetchone()[0])

        self.assertEqual(bridge.apply_source_change_check(self.ordinary, self.large,
                                                          out['scan_id'], 'job-1'), [])
        self.assertFalse(payload_now()['source_changed'])
        self.assertEqual(payload_now()['source_checked_job_id'], 'job-1')

        # 同一任务不重复检查：源文件变了也不改结论
        with closing(self.large._connect()) as c:
            c.execute("UPDATE files SET size=size+1 WHERE package_id=? AND path=?",
                      (PACKAGE, '政策/文件1.pdf'))
            c.commit()
        bridge.apply_source_change_check(self.ordinary, self.large, out['scan_id'], 'job-1')
        self.assertFalse(payload_now()['source_changed'], '同一任务只检查一次')

        # 新任务重新检查，并如实标记变化
        changed = bridge.apply_source_change_check(self.ordinary, self.large,
                                                   out['scan_id'], 'job-2')
        self.assertEqual([c0['path'] for c0 in changed], ['政策/文件1.pdf'])
        self.assertTrue(payload_now()['source_changed'])
        self.assertEqual(payload_now()['source_checked_job_id'], 'job-2')

    def test_source_change_check_ignores_normal_scans(self):
        with self.ordinary._connect() as c:
            c.execute("INSERT INTO scans(id,root_path,payload,owner_id) VALUES(?,?,?,?)",
                      ('scan-normal', '/tmp/x', json.dumps({'root': '/tmp/x'}), 'o1'))
        self.assertEqual(bridge.apply_source_change_check(self.ordinary, self.large,
                                                          'scan-normal', 'job-1'), [])
        with self.ordinary._connect() as c:
            payload = json.loads(c.execute("SELECT payload FROM scans WHERE id='scan-normal'"
                                           ).fetchone()[0])
        self.assertNotIn('source_checked_job_id', payload, '普通数据包不应被大包逻辑碰到')

    def test_source_change_check_never_raises(self):
        """大包库不可用时也必须安静跳过，绝不能拖垮普通解析。"""
        out = self._create(self._export(node_ids=[GROUP_A]))
        self.assertEqual(bridge.apply_source_change_check(self.ordinary, None,
                                                          out['scan_id'], 'job-x'), [])
        self.assertEqual(bridge.apply_source_change_check(None, self.large,
                                                          out['scan_id'], 'job-x'), [])

    def test_worker_wires_the_check(self):
        """worker.py 必须真的调用桥接层的检查（源码级契约，避免接线被改掉）。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, '..', 'worker.py'), encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn('apply_source_change_check', source)
        self.assertIn('_mark_source_changes_once(job)', source)
        self.assertIn('from services.large_package_catalog_import import apply_source_change_check',
                      source)

    # ---------- §4.3 父主题节点展开 ----------
    def _add_branch(self, branch_id, name, children):
        now = time.time()
        with closing(self.large._connect()) as c:
            c.execute("INSERT INTO lp_catalog_branches(package_id,id,name,children,revisions,"
                      "round_id,updated) VALUES(?,?,?,?,?,?,?)",
                      (PACKAGE, branch_id, name, json.dumps(children),
                       json.dumps({gid: 1 for gid in children}), 'r1', now))
            c.commit()

    def test_parent_theme_node_expands_to_all_children(self):
        """验收：可以选择**父主题节点**（不只是叶节点），展开结果与普通清单一致。"""
        self._add_branch('br-all', '政策与会议', [GROUP_A, GROUP_B])
        sel = self._export(node_ids=['br-all'])
        self.assertEqual([n['kind'] for n in sel['nodes']], ['branch'])
        self.assertEqual(sorted(sel['inventory']),
                         sorted(['会议/记录3.xlsx', '会议/剔除4.pdf',
                                 '政策/文件1.pdf', '政策/文件2.docx']))
        out = self._create(sel, request_key='rk-branch')
        self.assertEqual(out['selected_file_count'], 4)
        self.assertEqual(len(self._file_payloads(out['scan_id'])), 4)
        snapshot = self._snapshot(out['scan_id'])
        self.assertEqual([n['name'] for n in snapshot['nodes']], ['政策与会议'])

    def test_branch_and_child_node_together_do_not_duplicate(self):
        self._add_branch('br-all', '政策与会议', [GROUP_A, GROUP_B])
        out = self._create(self._export(node_ids=['br-all', GROUP_A]), request_key='rk-mix')
        self.assertEqual(out['selected_file_count'], 4, '父节点 + 子节点同时勾选不能重复导入')

    def test_node_rename_does_not_change_created_scan(self):
        """一致性：父主题节点改名不改变已创建普通数据包的文件集合（§9.3）。"""
        self._add_branch('br-all', '政策与会议', [GROUP_A, GROUP_B])
        out = self._create(self._export(node_ids=['br-all']), request_key='rk-rename')
        with closing(self.large._connect()) as c:
            c.execute("UPDATE lp_catalog_branches SET name='改过名的主题' WHERE package_id=? AND id=?",
                      (PACKAGE, 'br-all'))
            c.commit()
        self.assertEqual(len(self._file_payloads(out['scan_id'])), 4)
        self.assertEqual(self._snapshot(out['scan_id'])['nodes'][0]['name'], '政策与会议')

    # ---------- 回归：普通解析必须能读到 scan["tree"] ----------
    def test_payload_carries_a_physical_tree(self):
        """曾经的真缺陷：payload 没有 `tree` → 普通解析 `KeyError: 'tree'`。"""
        out = self._create(self._export(node_ids=[GROUP_A]))
        with self.ordinary._connect() as c:
            payload = json.loads(c.execute("SELECT payload FROM scans WHERE id=?",
                                           (out['scan_id'],)).fetchone()[0])
        self.assertIn('tree', payload, 'scans.payload 必须带 tree（普通分析流水线会读它）')
        tree = payload['tree']
        self.assertEqual(tree['kind'], 'directory')
        self.assertEqual(tree['path'], '.')
        names = sorted(child['name'] for child in tree['children'])
        self.assertEqual(names, ['政策'], '中间目录要成为树的节点')
        inner = tree['children'][0]
        self.assertEqual(inner['kind'], 'directory')
        self.assertEqual(sorted(child['name'] for child in inner['children']),
                         ['文件1.pdf', '文件2.docx'], '文件必须挂在所属目录下，不能成孤儿')

    def test_tree_matches_the_ordinary_builder(self):
        """桥接写进 payload 的树，必须与普通侧 `build_inventory_tree()` 算出来的一致。"""
        out = self._create(self._export(node_ids=[GROUP_A, GROUP_B]))
        with self.ordinary._connect() as c:
            payload = json.loads(c.execute("SELECT payload FROM scans WHERE id=?",
                                           (out['scan_id'],)).fetchone()[0])
        self.assertEqual(payload['tree'], self.ordinary.build_inventory_tree(out['scan_id']))
        self.assertEqual(payload['directory_count'], 3)      # 根 + 政策 + 会议
        self.assertEqual(payload['scanned_node_count'], 7)   # 4 文件 + 2 目录 + 根

    def test_analysis_entry_can_read_tree_without_keyerror(self):
        """照 `package_analysis` 的读法取一遍，必须不抛 KeyError。"""
        out = self._create(self._export(node_ids=[GROUP_A]))
        from services.package_analysis import _walk_files            # noqa: E402
        with self.ordinary._connect() as c:
            scan = json.loads(c.execute("SELECT payload FROM scans WHERE id=?",
                                        (out['scan_id'],)).fetchone()[0])
        walked = sorted(item['path'] for item in _walk_files(scan['tree']))
        self.assertEqual(walked, ['政策/文件1.pdf', '政策/文件2.docx'])

    # ---------- 路由调用形状 ----------
    def test_storage_entrypoint_accepts_large_store(self):
        """路由按文档 §4.4 传 `large_store=`，入口必须收得下（否则线上 TypeError）。"""
        import inspect
        from services.storage import Storage
        params = inspect.signature(Storage.create_scan_from_large_selection).parameters
        self.assertIn('large_store', params, 'Storage 入口必须接受 large_store 并透传')
        self.assertIsNone(params['large_store'].default, 'large_store 必须是可选参数')

    # ---------- Phase 3 任务恢复 ----------
    def test_dead_reference_is_rebuilt(self):
        first = self._create(self._export(node_ids=[GROUP_A]), request_key='rk-dead')
        # 普通数据包被删掉，但幂等记录还在
        with self.ordinary._connect() as c:
            c.execute("DELETE FROM scans WHERE id=?", (first['scan_id'],))
        again = self._create(self._export(node_ids=[GROUP_A]), request_key='rk-dead')
        self.assertFalse(again['reused'], '指向已删除数据包时必须重建，而不是返回死 id')
        self.assertNotEqual(again['scan_id'], first['scan_id'])
        self.assertEqual(self._count("SELECT COUNT(*) FROM large_catalog_imports"), 1,
                         '重建后旧桥接记录必须被清掉，不能留死引用')

    # ---------- Phase 3 审计日志 ----------
    def test_list_imports_reports_audit_trail(self):
        out = self._create(self._export(node_ids=[GROUP_A]), request_key='rk-audit')
        rows = bridge.list_imports(self.ordinary, PACKAGE)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['scan_id'], out['scan_id'])
        self.assertEqual(row['selected_file_count'], 2)
        self.assertTrue(row['scan_alive'])
        self.assertEqual([s['state'] for s in row['state_trace']],
                         ['created', 'validating', 'resolving_selection',
                          'materializing_scan', 'queued'])
        self.assertEqual(row['source_node_names'], ['政策文件'])
        self.assertTrue(row['selection_fingerprint'])
        self.assertEqual(bridge.list_imports(self.ordinary, 'lp-nobody'), [])

    def test_list_imports_marks_dead_scan(self):
        out = self._create(self._export(node_ids=[GROUP_A]))
        with self.ordinary._connect() as c:
            c.execute("DELETE FROM scans WHERE id=?", (out['scan_id'],))
        self.assertFalse(bridge.list_imports(self.ordinary, PACKAGE)[0]['scan_alive'])

    # ---------- Phase 3 大文件 ----------
    def test_huge_file_is_referenced_not_copied(self):
        """5GB 级文件也只记录大小/指纹，桥接层绝不读取或复制文件内容。"""
        self._add_files([('大文件/整库备份.bin', GROUP_A, 5 * 1024 ** 3, None)])
        out = self._create(self._export(node_ids=[GROUP_A]))
        self.assertEqual(out['selected_file_count'], 3)
        self.assertGreaterEqual(out['selected_bytes'], 5 * 1024 ** 3)
        big = [i for i in self._file_payloads(out['scan_id'])
               if i['path'] == '大文件/整库备份.bin'][0]
        self.assertEqual(big['size'], 5 * 1024 ** 3)
        self.assertTrue(big['fingerprint'])
        # 普通数据包根目录仍是大包根目录，清单里是相对路径（不复制）
        with self.ordinary._connect() as c:
            scan = json.loads(c.execute("SELECT payload FROM scans WHERE id=?",
                                        (out['scan_id'],)).fetchone()[0])
        self.assertEqual(scan['root'], '/tmp/lpimport-src')


if __name__ == '__main__':
    unittest.main()
