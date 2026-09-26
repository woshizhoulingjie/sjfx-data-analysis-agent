# -*- coding: utf-8 -*-
"""主题层必须**自动拿到可用轮次** —— 智能目录生成完就该出主题，不靠碰巧。

生产根因（2026-09-25）：`ensure_round(..., "topics")` 拿到的绑定轮次一旦进了终态
（completed / exhausted），它就原样被交出来；而 `reserve()` 只认 `status=='active'`，
于是主题层每次重建都在 `reserve` 处静默失败，主题名永远停在「内容分组（N个子目录）」。
实测：
  · lp-68848e2c —— 2 个轮次全 exhausted、0 个 active，16 个主题节点里 2 个占位名
  · lp-085e413e —— completed×3 + exhausted×1、0 个 active，同样 2 个占位名

修法**不在 naming 模块里**（那是维护者精心设计的命名算法与预算语义）：worker 侧
换一个「由主题内容签名派生」的键再要一次。若已有可用 active 轮次就复用它，
**不新建轮次**；确实没有可用的才新建。
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.large_package_isolated import LargePackageStore      # noqa: E402
from services import large_package_catalog_naming as naming        # noqa: E402
from large_package_worker import LargePackageWorker                # noqa: E402
from config import Config                                          # noqa: E402

LEAF_COUNT = 12          # 必须 >= LP_TOPIC_MERGE_MIN_GROUPS(12)


class TopicRoundAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='topicround-')
        self.store = LargePackageStore(db_path=os.path.join(self.dir, 'lp.db'))
        self.pid = 'lp-topicround'
        # `_merge_topics_once` 第一行就检查这个开关；生产是开的（实测 SJFX_LP_TOPIC_MERGE=1），
        # 测试进程默认没有这个环境变量，必须显式打开，否则根本走不到轮次逻辑。
        self._merge_flag = patch.object(Config, 'LP_TOPIC_MERGE', True)
        self._merge_flag.start()
        now = time.time()
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO packages(id,root_path,owner_id,status,phase,progress,counts,"
                      "created_at,updated_at,control_state,control_generation) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, '/tmp/tr', 'o1', 'waiting_for_selection', 'catalog_ready',
                       70, '{}', now, now, 'active', 0))
            for i in range(LEAF_COUNT):
                gid = 'km-tr-%05d' % i
                c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                          "result,status,updated_at,member_count,content_revision) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (self.pid, gid, 'm', b'', 'rep.pdf', '{}', 'named', now, 5, 1))
                c.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,"
                          "description,evidence,model,digest,needs_review,locked,updated) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (self.pid, gid, 1, 1, '叶组%d' % i, '说明', '[]', 'm', 'd%d' % i, 0, 0, now))
            c.commit()
        self.worker = LargePackageWorker(self.store)

    def tearDown(self):
        self._merge_flag.stop()
        executor = getattr(self.worker, '_naming_executor', None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        for root, _dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            os.rmdir(root)

    # ---------- helpers ----------
    def _round(self, rid, status, seconds_left=7200, key=None):
        now = time.time()
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO lp_catalog_rounds(package_id,id,request_key,started,deadline,"
                      "max_requests,max_tokens,requests,tokens,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, rid, key or rid, now - 60, now + seconds_left,
                       1000, 2000000, 3, 100, status))
            if key:
                c.execute("INSERT OR REPLACE INTO lp_catalog_round_keys VALUES(?,?,?)",
                          (self.pid, key, rid))
            c.commit()

    def _count_rounds(self):
        with closing(self.store._connect()) as c:
            return c.execute("SELECT COUNT(*) FROM lp_catalog_rounds WHERE package_id=?",
                             (self.pid,)).fetchone()[0]

    def _keys(self):
        with closing(self.store._connect()) as c:
            return {r[0]: r[1] for r in c.execute(
                "SELECT request_key, round_id FROM lp_catalog_round_keys WHERE package_id=?",
                (self.pid,))}

    def _statuses(self):
        with closing(self.store._connect()) as c:
            return dict(c.execute("SELECT id, status FROM lp_catalog_rounds WHERE package_id=?",
                                  (self.pid,)))

    # ---------- 用例 ----------
    def test_rebuild_proceeds_when_bound_round_is_completed(self):
        """**核心**：topics 绑在 completed 轮次上时，主题重建必须照常进行。"""
        self._round('r-done', 'completed', key='topics')
        with patch.object(naming, 'merge_topics', return_value=False) as merge:
            self.worker._merge_topics_once(self.pid)
            merge.assert_called_once()
        st = self._statuses()
        self.assertIn('active', st.values(), '必须新建出一个可用轮次，否则主题层永远起不来')

    def test_rebuild_proceeds_when_bound_round_is_exhausted(self):
        self._round('r-dead', 'exhausted', key='topics')
        with patch.object(naming, 'merge_topics', return_value=False) as merge:
            self.worker._merge_topics_once(self.pid)
            merge.assert_called_once()

    def test_rebuild_reuses_active_round_without_creating_new_ones(self):
        """**不许制造轮次爆炸**：已有可用 active 轮次时必须复用。"""
        self._round('r-done', 'completed', key='topics')
        self._round('r-live', 'active', seconds_left=7200)
        before = self._count_rounds()
        with patch.object(naming, 'merge_topics', return_value=False) as merge:
            self.worker._merge_topics_once(self.pid)
            merge.assert_called_once()
        self.assertEqual(self._count_rounds(), before, '应复用现有 active 轮次，不得新建')

    def test_alt_key_is_stable_for_unchanged_content(self):
        """内容不变时派生键必须相同 —— 这是"不开一堆轮次"的关键。"""
        self._round('r-done', 'completed', key='topics')
        seen = []
        with patch.object(naming, 'merge_topics', return_value=False) as merge:
            self.worker._merge_topics_once(self.pid)
            seen.append(set(self._keys()))
            self.worker._topics_merged.pop(self.pid, None)   # 绕过去抖，模拟再重建
            self.worker._merge_topics_once(self.pid)
            seen.append(set(self._keys()))
        self.assertTrue(merge.called)
        alt_first = {k for k in seen[0] if k.startswith('topics-')}
        alt_second = {k for k in seen[1] if k.startswith('topics-')}
        self.assertTrue(alt_first, '必须派生出一个 topics-<签名> 键')
        self.assertEqual(alt_first, alt_second, '内容未变时派生键必须一致，不得每次换键')


if __name__ == '__main__':
    unittest.main()
