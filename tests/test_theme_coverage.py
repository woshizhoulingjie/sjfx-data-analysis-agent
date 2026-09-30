# -*- coding: utf-8 -*-
"""三级架构完整性：**每个已命名叶组都必须属于某个主题**（不许有孤儿）。

生产实测（2026-09-25）修复前，顶层主题只覆盖：
  lp-68848e2c 140/370、lp-085e413e 143/340、lp-ff42d1f7 144/376 —— 58~62% 的叶组是孤儿，
  因为它们所在的簇"不够紧"（`len(items)<2` 或 `min(support)<0.65`）被丢进 `terminal`，
  而 terminal 从不进入 `nodes`。

现在 `build()` 会在层级循环之后做一遍归属兜底：terminal 节点按向量最近邻挂到顶层
已接受节点下（簇作为子节点、单叶并入 leaf_ids），**不配新名字、零模型调用**。
"""
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.large_package_isolated import LargePackageStore      # noqa: E402
from services import large_package_catalog_tree as tree            # noqa: E402
from services import large_package_catalog_topics as topics        # noqa: E402
from config import Config                                          # noqa: E402

# 11 个几乎同向（会聚成一簇并被接受）+ 1 个正交（必被单独成簇、因 len<2 被拒 → terminal）
#
# 为什么是 12 个：`build()` 里 `k = ceil(len(X)/10)`，叶组 ≤10 时 k=1、只形成一个簇，
# 那个簇要么整体接受要么整体被拒，测不出"部分接受 + 部分被拒"。12 个才有 k=2。
TIGHT = ['km-cov-%05d' % i for i in range(11)]
LOOSE = ['km-cov-90000']
ALL_LEAVES = TIGHT + LOOSE


def _v(x, y, z):
    n = (x * x + y * y + z * z) ** 0.5
    return struct.pack('<3f', x / n, y / n, z / n)


class _MockModel(object):
    model = 'mock-model'


def _name_every_bucket(store, package_id, buckets, *a, **k):
    """桩：像真实 name_topics 一样给**每个**待命名簇返回名字（label 0 为 '紧密簇主题'）。

    原桩只返回 {0: ...}；2026-09-29 起层级聚类给 k 加了下限（db04530），同一层会有
    多个被接受的簇，只给 label 0 起名会让其余簇落到占位名 —— 那是桩的缺口，不是被测行为。
    """
    return {lab: ('紧密簇主题' if lab == 0 else '紧密簇主题%d' % lab) for lab in buckets}


class ThemeCoverageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='coverage-')
        self.store = LargePackageStore(db_path=os.path.join(self.dir, 'lp.db'))
        self.pid = 'lp-coverage'
        now = time.time()
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO packages(id,root_path,owner_id,status,phase,progress,counts,"
                      "created_at,updated_at,control_state,control_generation) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, '/tmp/cov', 'o1', 'waiting_for_selection', 'catalog_ready',
                       70, '{}', now, now, 'active', 0))
            vecs = [_v(1, 0, 0.01 * i) for i in range(11)] + [_v(0, 0, 1)]
            for gid, vec in zip(ALL_LEAVES, vecs):
                c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                          "result,status,updated_at,member_count,content_revision) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (self.pid, gid, 'm', vec, 'rep.pdf', '{}', 'named', now, 4, 1))
                c.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,"
                          "description,evidence,model,digest,needs_review,locked,updated) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (self.pid, gid, 1, 1, '叶组-%s' % gid[-5:], '说明', '[]', 'm',
                           'd-' + gid, 0, 0, now))
            c.commit()
        self._flag = patch.object(Config, 'LP_TOPIC_MERGE', True)
        self._flag.start()
        self._saved = topics.name_topics

    def tearDown(self):
        topics.name_topics = self._saved
        self._flag.stop()
        for root, _dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            os.rmdir(root)

    def _payload(self):
        with closing(self.store._connect()) as c:
            row = c.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?',
                            (self.pid,)).fetchone()
        return json.loads(row[0]) if row else None

    def _covered(self, payload):
        """从顶层节点出发，把子树里的 leaf_ids 全收上来。"""
        nodes = {n['group_id']: n for n in payload['nodes']}
        covered = set()

        def walk(gid):
            node = nodes[gid]
            covered.update(node.get('leaf_ids') or [])
            for child in node.get('child_nodes') or []:
                if child in nodes:
                    walk(child)
        for node in payload['nodes']:
            if node.get('parent_id') is None:
                walk(node['group_id'])
        return covered

    def test_similar_leaf_is_attached_and_dissimilar_is_not_forced(self):
        """**符合设计的断言**：语义相近的叶组被吸附；确实不相关的**不强迫归属**。

        契约 `test_optional_hierarchy_preserves_unrelated_members_without_forced_parent`
        明确要求"不适合归并的保留原样"。所以这里不是断言 100% 覆盖，而是断言：
          · 11 个紧密同向的叶组 → 被主题覆盖；
          · 1 个正交叶组（余弦 < 0.65）→ **不得**被塞进那个主题；
          · 整个 terminal 里没有"被硬塞进不相关主题"的。
        """
        topics.name_topics = _name_every_bucket
        self.assertTrue(tree.build(self.store, self.pid,
                                  {'id': 'r1', 'deadline': time.time() + 3600},
                                  lambda: False, _MockModel()))
        payload = self._payload()
        self.assertIsNotNone(payload)
        covered = self._covered(payload)
        for gid in TIGHT:
            self.assertIn(gid, covered, '语义相近的叶组必须被主题覆盖')
        self.assertNotIn(LOOSE[0], covered,
                         '与主题余弦 < 0.65 的叶组不得被强行塞进该主题（契约要求保留原样）')
        tops = [n for n in payload['nodes'] if n.get('parent_id') is None]
        self.assertTrue(tops, '必须有顶层主题')

    def test_rejected_cluster_keeps_its_existing_name(self):
        """被拒的簇**不配新名字**，沿用 frontier 上已有的名字（命名门槛不放松）。"""
        topics.name_topics = _name_every_bucket
        tree.build(self.store, self.pid, {'id': 'r1', 'deadline': time.time() + 3600},
                   lambda: False, _MockModel())
        payload = self._payload()
        names = {n['name'] for n in payload['nodes']}
        self.assertIn('紧密簇主题', names)
        # 被拒的簇/叶组带的是叶组自己的名字，不可能是 name_topics 的返回值
        self.assertNotIn('内容分组（', ' '.join(names), '不得出现占位名')

    def test_leaf_without_vector_is_not_forced_under_a_theme(self):
        """**没有向量的叶组**不得被强行塞进内容主题。

        生产实测（lp-ff42d1f7）：266 个无向量成员**全部**被管道自己的质量闸门拒掉
        （194 个 too_little_text、72 个 no_content）—— 它们不是"漏算向量"，而是内容
        不足以产生有意义的向量。给残片硬算向量再按它吸附，就是伪造归属。
        所以这里的断言是：**不吸附**，如实留作未归类。
        """
        gid = 'km-cov-99000'
        now = time.time()
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                      "result,status,updated_at,member_count,content_revision) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, gid, 'm', b'', 'thin.pdf', '{}', 'named', now, 1, 1))
            c.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,"
                      "description,evidence,model,digest,needs_review,locked,updated) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, gid, 1, 1, '正文过少的通知', '说明', '[]', 'm', 'thin', 0, 0, now))
            c.commit()
        topics.name_topics = _name_every_bucket
        tree.build(self.store, self.pid, {'id': 'r1', 'deadline': time.time() + 3600},
                   lambda: False, _MockModel())
        payload = self._payload()
        self.assertIsNotNone(payload)
        self.assertNotIn(gid, self._covered(payload),
                         '没有向量的叶组不得被硬塞进内容主题（那是伪造归属）')
        self.assertNotIn('内容分组（', ' '.join(n['name'] for n in payload['nodes']),
                         '不得用占位名充当主题名')


if __name__ == '__main__':
    unittest.main()
