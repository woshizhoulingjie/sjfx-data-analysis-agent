# -*- coding: utf-8 -*-
"""主题层"占位名覆盖真名字"翻转缺陷的回归测试（生产 lp-ff42d1f7，2026-09-25）。

现象：15 个主题节点全部 `name_pending` → 页面显示「内容分组（N个子目录）」，
约 50 分钟后"自己好了"，而轮次一转终态就会再坏一次。

这里用可控的 `name_topics` 桩复现两条关键行为：
  1. **沿用旧名字**：这次重建一个名字都没拿到时，同一簇（node_id 不变）必须保留
     上一版已经验证过的名字，不能退回占位名；
  2. **拒绝覆盖**：如果连"上一版的名字"都没有（首次建树就没拿到预算），
     不许把一棵全是占位名的树写进库 —— 宁可先不显示主题层。
  3. worker 侧：轮次不是 `active` 时直接跳过重建，而不是产出一棵占位名树。
"""
import json
import os
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.large_package_isolated import LargePackageStore        # noqa: E402
from services import large_package_catalog_tree as tree              # noqa: E402
from services import large_package_catalog_topics as topics          # noqa: E402
from services import large_package_catalog_naming as naming          # noqa: E402

LEAVES = ['km-test-0000%d' % i for i in range(4)]
REAL_NAME = '测试主题：邻域检索与向量索引'


class _MockModel(object):
    model = 'mock-model'


def _anchor(seed):
    """四个几乎同向的单位向量 → k-means 必然把它们聚成一簇。"""
    vec = [1.0, 0.0, 0.0, seed * 0.01]
    norm = sum(v * v for v in vec) ** 0.5
    return struct.pack('<4f', *[v / norm for v in vec])


class ThemeOverwriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='theme-')
        self.store = LargePackageStore(db_path=os.path.join(self.dir, 'lp.db'))
        self.pid = 'lp-theme'
        now = time.time()
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO packages(id,root_path,owner_id,status,phase,progress,counts,"
                      "created_at,updated_at,control_state,control_generation) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, '/tmp/theme', 'o1', 'waiting_for_selection', 'catalog_ready',
                       70, '{}', now, now, 'active', 0))
            for i, gid in enumerate(LEAVES):
                c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                          "result,status,updated_at,member_count,content_revision) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (self.pid, gid, 'm', _anchor(i), 'rep%d.pdf' % i, '{}', 'named', now, 5, 1))
                c.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,"
                          "description,evidence,model,digest,needs_review,locked,updated) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (self.pid, gid, 1, 1, '叶组%d' % i, '说明', '[]', 'm', 'd%d' % i, 0, 0, now))
            c.commit()
        self._saved = topics.name_topics
        self.round = {'id': 'round-a', 'deadline': time.time() + 3600}

    def tearDown(self):
        topics.name_topics = self._saved
        for root, _dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            os.rmdir(root)

    # ---------- helpers ----------
    def _fake(self, names_for, refused=0, stopped=False):
        """模拟 name_topics：结果写进模块级 LAST_REPORT（生产用的是同一处）。"""
        def fake(store, package_id, buckets, centroids, round_state, cancelled, model_client):
            topics.LAST_REPORT[package_id] = {
                'named': len(names_for), 'cached': 0, 'refused': refused,
                'stopped': stopped, 'round': round_state.get('id')}
            return dict(names_for)
        return fake

    def _payload(self):
        with closing(self.store._connect()) as c:
            row = c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',
                            (self.pid,)).fetchone()
        return json.loads(row[0]) if row else None

    def _node_names(self):
        payload = self._payload()
        if not payload:
            return {}
        return {n['group_id']: (n.get('name'), bool(n.get('name_pending')))
                for n in payload.get('nodes') or []}

    def _build(self, round_id):
        return tree.build(self.store, self.pid,
                          {'id': round_id, 'deadline': time.time() + 3600},
                          lambda: False, _MockModel())

    # ---------- 用例 ----------
    def test_first_build_with_names_writes_real_names(self):
        topics.name_topics = self._fake({0: REAL_NAME})
        self.assertTrue(self._build('round-a'))
        names = self._node_names()
        self.assertEqual(len(names), 1)
        self.assertEqual(list(names.values())[0], (REAL_NAME, False))

    def test_no_budget_rebuild_keeps_previous_names(self):
        """**核心用例**：第二次重建一个名字都没拿到，必须沿用上一版真名字。"""
        topics.name_topics = self._fake({0: REAL_NAME})
        self.assertTrue(self._build('round-a'))
        before = self._node_names()

        topics.name_topics = self._fake({}, refused=1)          # 预算被拒
        self._build('round-b')                                   # 换轮次，绕开早退保护
        after = self._node_names()
        self.assertEqual(after, before, '拿不到预算的重建不得把真名字抹成占位名')
        self.assertEqual(list(after.values())[0], (REAL_NAME, False))

    def test_first_build_without_budget_is_refused(self):
        """首次建树就没拿到预算：宁可先不写主题层，也不写一棵占位名树。"""
        topics.name_topics = self._fake({}, refused=1)
        self.assertFalse(self._build('round-a'))
        self.assertIsNone(self._payload(), '不得留下全是占位名的树')

    def test_stopped_early_is_also_refused(self):
        topics.name_topics = self._fake({}, stopped=True)
        self.assertFalse(self._build('round-a'))
        self.assertIsNone(self._payload())

    def test_worker_skips_non_active_round(self):
        """worker 侧：topics 键绑在 completed 轮次上、且主题归并关闭时必须跳过重建。

        注意：本用例依赖 `LP_TOPIC_MERGE`。生产的 `state/.env` 里有
        `SJFX_LP_TOPIC_MERGE=1`，此时 `_merge_topics_once` 会先经 `ensure_round`
        换绑到新的 active 轮次再继续（那是我为"备选键空转"修的路径），返回 True；
        工作树没这个变量，走的是"归并关闭 → 直接跳过"分支。为了让这条用例在两种
        环境下都确定，这里把开关显式钉住（只影响本进程的 Config 对象）。
        """
        from large_package_worker import LargePackageWorker
        from config import Config
        pinned = {name: getattr(Config, name, None) for name in
                  ('LP_TOPIC_MERGE', 'LP_TOPIC_MERGE_MIN_GROUPS')}
        Config.LP_TOPIC_MERGE = False
        try:
            now = time.time()
            with closing(self.store._connect()) as c:
                c.execute("INSERT INTO lp_catalog_rounds(package_id,id,request_key,started,deadline,"
                          "max_requests,max_tokens,requests,tokens,status) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (self.pid, 'round-done', 'topics', now, now + 7200,
                           1000, 2000000, 1, 10, 'completed'))
                c.execute("INSERT OR REPLACE INTO lp_catalog_round_keys VALUES(?,?,?)",
                          (self.pid, 'topics', 'round-done'))
                c.commit()
            worker = LargePackageWorker(self.store)
            self.assertFalse(worker._merge_topics_once(self.pid),
                             '轮次不是 active 时必须跳过，而不是产出一棵占位名树')
            self.assertIsNone(self._payload())
        finally:
            for name, value in pinned.items():
                if value is not None:
                    setattr(Config, name, value)


if __name__ == '__main__':
    unittest.main()
