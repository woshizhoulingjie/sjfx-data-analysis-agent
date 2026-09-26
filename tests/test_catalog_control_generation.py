# -*- coding: utf-8 -*-
"""§3B.1「包取消代次校验」的回归测试。

只校验包**状态**是不够的：用户"取消 → 恢复"之后包又是 active，一个取消之前领取的
命名请求这时回来，状态检查照样通过，于是把陈旧结果写进标签。代次
（`packages.control_generation`，`set_control_state()` 每次 +1、cancel 路径同样 +1）
才能区分这两次"同为 active"的时刻。

`apply()` 的校验在**同一个事务内**完成：领取时把当时的代次写进任务行，落库前与包上的
当前代次比对，不一致就整事务回滚。
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

from services.large_package_isolated import LargePackageStore     # noqa: E402
from services import large_package_catalog_naming as naming       # noqa: E402

GROUP = 'km-1001-00000'


class ControlGenerationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ctlgen-')
        self.store = LargePackageStore(db_path=os.path.join(self.dir, 'lp.db'))
        self.pid = 'lp-ctlgen'
        now = time.time()
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO packages(id,root_path,owner_id,status,phase,progress,counts,"
                      "created_at,updated_at,control_state,control_generation) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, '/tmp/ctl', 'o1', 'waiting_for_selection', 'catalog_ready',
                       70, '{}', now, now, 'active', 0))
            c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                      "result,status,updated_at,member_count,content_revision) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, GROUP, '', b'', 'rep.pdf', '{}', 'queued', now, 3, 1))
            c.commit()
        self.material = {'revision': 1, 'owner_id': 'o1', 'digest': 'd1', 'cache_digest': 'd1',
                         'evidence': [{'id': 'E1', 'text': '证据一'}], 'user': '{}'}
        self.result = {'name': '中央部门预算绩效管理资料', 'description': '依据组内文件归纳的说明',
                       'evidence_ids': ['E1'], 'needs_review': 0, 'purity': 'same'}

    def tearDown(self):
        for root, _dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            os.rmdir(root)

    # ---------- helpers ----------
    def _claim(self):
        """模拟命名领取：任务转 running 并**记下当时的包代次**（与生产同一段 SQL）。"""
        with closing(self.store._connect()) as c:
            c.execute("UPDATE lp_catalog_jobs SET status='running',leased=?,"
                      "control_generation=COALESCE((SELECT control_generation FROM packages "
                      "WHERE id=lp_catalog_jobs.package_id),0) "
                      "WHERE package_id=? AND group_id=?", (time.time(), self.pid, GROUP))
            c.commit()

    def _gen(self, table='packages'):
        with closing(self.store._connect()) as c:
            if table == 'packages':
                return c.execute("SELECT control_generation FROM packages WHERE id=?",
                                 (self.pid,)).fetchone()[0]
            return c.execute("SELECT control_generation FROM lp_catalog_jobs "
                             "WHERE package_id=? AND group_id=?", (self.pid, GROUP)).fetchone()[0]

    def _labels(self):
        with closing(self.store._connect()) as c:
            return c.execute("SELECT COUNT(*) FROM lp_catalog_labels WHERE package_id=? AND group_id=?",
                             (self.pid, GROUP)).fetchone()[0]

    def _enqueue(self):
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO lp_catalog_jobs(package_id,group_id,revision,status,"
                      "attempts,next_at,leased,error,control_generation) VALUES(?,?,1,'queued',0,0,NULL,'',0)",
                      (self.pid, GROUP))
            c.commit()

    def _resume_fully(self):
        """把包恢复成"看起来完全正常"：status 回到可调度、control_state=active。

        代次已经被 cancel/resume 推高，所以此刻**只有代次能暴露"这是一个跨过取消的
        陈旧请求"** —— 这正是本测试要隔离的变量。
        """
        self.store.set_control_state(self.pid, 'cancelled', note='测试取消')
        self.store.set_control_state(self.pid, 'active', note='测试恢复')
        with closing(self.store._connect()) as c:
            c.execute("UPDATE packages SET status='waiting_for_selection' WHERE id=?", (self.pid,))
            c.commit()

    # ---------- 用例 ----------
    def test_column_exists(self):
        """幂等迁移确实加上了列（老库升级路径）。"""
        with closing(self.store._connect()) as c:
            cols = {r[1] for r in c.execute('PRAGMA table_info(lp_catalog_jobs)')}
        self.assertIn('control_generation', cols)

    def test_claim_records_current_generation(self):
        self._enqueue()
        self._claim()
        self.assertEqual(self._gen('jobs'), self._gen('packages'))

    def test_apply_accepts_when_generation_matches(self):
        self._enqueue()
        self.store.set_control_state(self.pid, 'active')      # gen: 0 → 1，包仍是 active
        self._claim()
        self.assertTrue(naming.apply(self.store, self.pid, GROUP, self.material,
                                     self.result, 'model-x'))
        self.assertEqual(self._labels(), 1, '代次一致时必须正常落库')

    def test_apply_rejects_after_cancel_and_resume(self):
        """**核心用例**：取消→恢复后包仍是 active，只有代次能挡住陈旧结果。"""
        self._enqueue()
        self._claim()
        claimed_gen = self._gen('jobs')
        self._resume_fully()
        self.assertGreater(self._gen('packages'), claimed_gen, '取消+恢复必须推高代次')
        self.assertFalse(naming.apply(self.store, self.pid, GROUP, self.material,
                                      self.result, 'model-x'),
                         '跨过取消动作的陈旧结果不得落库')
        self.assertEqual(self._labels(), 0, '被拒绝时不得写任何标签')

    def test_apply_rejects_after_single_pause(self):
        self._enqueue()
        self._claim()
        self.store.set_control_state(self.pid, 'paused', note='测试暂停')
        self.store.set_control_state(self.pid, 'active', note='测试恢复')
        with closing(self.store._connect()) as c:
            c.execute("UPDATE packages SET status='waiting_for_selection' WHERE id=?", (self.pid,))
            c.commit()
        self.assertFalse(naming.apply(self.store, self.pid, GROUP, self.material,
                                      self.result, 'model-x'))
        self.assertEqual(self._labels(), 0)

    def test_normal_progress_updates_do_not_invalidate(self):
        """反例保护：普通进度/状态更新（不涉及控制态）不得推高代次、不得误杀在途结果。"""
        self._enqueue()
        self._claim()
        gen_before = self._gen('packages')
        self.store.update_package(self.pid, status='waiting_for_selection',
                                  phase='catalog_ready', progress=71, message='正常推进')
        self.assertEqual(self._gen('packages'), gen_before, '普通更新不得改代次')
        self.assertTrue(naming.apply(self.store, self.pid, GROUP, self.material,
                                     self.result, 'model-x'),
                        '普通推进之后在途结果仍应正常落库')
        self.assertEqual(self._labels(), 1)


if __name__ == '__main__':
    unittest.main()
