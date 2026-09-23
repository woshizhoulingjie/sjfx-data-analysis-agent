"""第 0 批回归测试：后台命名车道的空转止损与目录签名的进展判定。

背景（生产实测，只读查询 2026-09-17）：

    large package lp-60c199... name fallback applied to 1 groups
    large package lp-60c199... topic merge done=False
    （约每 2 秒重复一次，持续数小时）

根因是两处"永远不可能成功却每轮都重试"的路径：

  1. `_merge_topics_once` 在叶组数达不到 `LP_TOPIC_MERGE_MIN_GROUPS` 时，
     `merge_topics` 内部直接返回 False，而 worker 侧只有成功（ok=True）才缓存
     签名，于是每一轮命名切片都重新查库、重新尝试归并。
  2. `_name_fallback` 与 `_merge_topics_once` 挂在"排空"分支上，而命名切片
     约每 2 秒被提交一次，因此即使没有任何新工作也会被反复执行。

后果不只是日志噪音：`lp_catalog_label_history` 累积到 140,040 行，而不同的
(package, group) 只有 3,970 个 —— 35.3 倍冗余，其中两个包分别达到 816.7 倍与
729.1 倍；所有包共用的单后台命名槽被反复占用。

这里锁定两个行为：
  * 叶组数不足时 `_merge_topics_once` 不调用 `merge_topics`，并把该签名记为定稿；
  * 一轮"排空"走完而目录签名没有任何变化时，后续切片不再重复执行兜底与归并；
  * 反向保护：叶组数足够时主题归并**仍然必须被执行**（防止过度收缩）。
"""
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.large_package_isolated import LargePackageStore
from services import large_package_semantic as semantic
from services import large_package_catalog_naming as naming
from large_package_worker import LargePackageWorker


class SystemBucketLabelTests(unittest.TestCase):
    """空转回归：系统桶的确定性标签不得被 label_singletons 回收。

    真因（生产实测 2026-09-17，包 lp-60c199e1）：
      `label_singletons` 删除 member_count<=1 且 model='deterministic' 的未锁定
      标签，而 `_name_fallback` 给系统桶 `km-other-*` 写的就是这样一个标签。
      两者每个命名切片（约 2 秒）对写一次，日志里
      "name fallback applied to 1 groups" 刷了 714 次并持续不停，
      历史上把 lp_catalog_label_history 灌到 140,473 行 / 489.6 MB。

    这里锁两件事：
      * `label_singletons` 不动 `km-other-*` 的标签；
      * 于是"兜底命名 → 回收 → 再兜底"不再成立：第二次兜底应当无事可做。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(Config, "SCAN_ALLOWED_ROOTS", ()),
            patch.object(Config, "LP_TOPIC_MERGE", False),
        ]
        for item in self.patches:
            item.start()
        self.store = LargePackageStore(self.root / "state.db")
        self.pid = self.store.create(self.root)["id"]
        with closing(self.store._connect()) as conn:
            semantic.init(conn)
            naming.init(conn)
        self.worker = LargePackageWorker(store=self.store, poll_seconds=0.2)
        with closing(self.store._connect()) as conn:
            # member_count 先写 0：紧接着插入成员会触发 lp_member_count_add
            # 把它加成 1。**这一步是夹具的关键** —— 若直接写 1，触发器会加成 2，
            # 于是 label_singletons 的 `member_count<=1` 条件天然不成立，
            # 用例就会"因为错误的原因通过"，根本抓不住要防的空转。
            conn.execute(
                "INSERT OR REPLACE INTO large_catalog_groups"
                "(package_id,id,model,anchor,representative,result,status,updated_at,"
                " attempts,content_revision,member_count) "
                "VALUES(?,?,'km','','','','named',?,0,1,0)",
                (self.pid, "km-other-abc123", time.time()))
            conn.execute(
                # vector 是 NOT NULL；系统桶本来就没有向量，
                # 用空 BLOB 表示"无向量"，与语义聚类侧对空向量的判断一致。
                "INSERT OR REPLACE INTO large_catalog_members"
                "(package_id,path,group_id,input_hash,vector) VALUES(?,?,?,?,zeroblob(0))",
                (self.pid, "tiny.txt", "km-other-abc123", "h"))
            conn.commit()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _label(self):
        with closing(self.store._connect()) as conn:
            row = conn.execute("SELECT name,model FROM lp_catalog_labels "
                               "WHERE package_id=? AND group_id='km-other-abc123'",
                               (self.pid,)).fetchone()
        return (row["name"], row["model"]) if row else None

    def test_system_bucket_label_survives_singleton_reclaim(self):
        self.assertEqual(self.worker._name_fallback(self.pid), 1,
                         "首次兜底应为系统桶写一个确定性标签")
        self.assertEqual(self._label(), ("未能识别内容", "deterministic"))
        self.assertEqual(naming.label_singletons(self.store, self.pid), 0,
                         "系统桶的标签被 label_singletons 回收了 —— 这会与 "
                         "_name_fallback 形成每 2 秒一次的对写空转")
        self.assertEqual(self._label(), ("未能识别内容", "deterministic"),
                         "回收之后标签必须还在")
        self.assertEqual(self.worker._name_fallback(self.pid), 0,
                         "第二轮兜底必须无事可做（这正是空转是否收敛的判据）")

    def test_fallback_does_not_preempt_pending_naming_job(self):
        """兜底命名不得抢在排队中的模型命名之前。

        真因（生产实测 2026-09-17）：`_name_fallback` 无条件给单文件组写确定性
        标签，`label_singletons` 又按 "member_count<=1 且 deterministic 且未锁定"
        把它删掉并重新排队 —— 两个组件每 2 秒对写一次，实测 lp-fb811610 的 3 个
        单文件组反复重建、lp-c91bc4bb 12 秒内新增 2 条。
        """
        with closing(self.store._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO large_catalog_groups"
                "(package_id,id,model,anchor,representative,result,status,updated_at,"
                " attempts,content_revision,member_count) "
                "VALUES(?,?,'km','','','','named',?,0,1,1)",
                (self.pid, "km-single-0002", time.time()))
            conn.execute(
                "INSERT OR REPLACE INTO lp_catalog_jobs(package_id,group_id,revision,status)"
                " VALUES(?,?,1,'queued')", (self.pid, "km-single-0002"))
            conn.commit()
        # 断言**具体这一组**没有拿到兜底标签。不能断言返回值：本夹具里还躺着
        # 一个尚未命名的系统桶 km-other-abc123，兜底会（正确地）把它算进返回条数。
        self.worker._name_fallback(self.pid)
        with closing(self.store._connect()) as conn:
            got = conn.execute("SELECT COUNT(*) FROM lp_catalog_labels "
                               "WHERE package_id=? AND group_id='km-single-0002'",
                               (self.pid,)).fetchone()[0]
        self.assertEqual(got, 0, "有排队中的命名任务时不得抢写兜底标签")
        # 模型放弃（deferred）之后，兜底应当接手，而且此后不得再被回收
        with closing(self.store._connect()) as conn:
            conn.execute("UPDATE lp_catalog_jobs SET status='deferred' "
                         "WHERE package_id=? AND group_id='km-single-0002'", (self.pid,))
            conn.commit()
        self.worker._name_fallback(self.pid)
        with closing(self.store._connect()) as conn:
            got = conn.execute("SELECT COUNT(*) FROM lp_catalog_labels "
                               "WHERE package_id=? AND group_id='km-single-0002'",
                               (self.pid,)).fetchone()[0]
        self.assertEqual(got, 1, "模型已放弃时兜底必须接手，否则目录会留空名")
        self.assertEqual(naming.label_singletons(self.store, self.pid), 0,
                         "模型已放弃的组，其兜底标签不得再被回收（否则又是对写循环）")

    def test_legacy_singleton_label_is_still_reclaimed(self):
        """反向保护：普通单文件组的旧式确定性标签**仍然必须**被回收。"""
        with closing(self.store._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO large_catalog_groups"
                "(package_id,id,model,anchor,representative,result,status,updated_at,"
                " attempts,content_revision,member_count) "
                "VALUES(?,?,'km','','','','named',?,0,1,0)",
                (self.pid, "km-single-0001", time.time()))
            conn.execute(
                "INSERT OR REPLACE INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.pid, "km-single-0001", 1, 1, "Some English Filename",
                 "旧式单文件标签", "[]", "deterministic", "d1", 1, 0, time.time()))
            conn.commit()
        self.assertEqual(naming.label_singletons(self.store, self.pid), 1)
        with closing(self.store._connect()) as conn:
            left = conn.execute("SELECT COUNT(*) FROM lp_catalog_labels "
                                "WHERE package_id=? AND group_id='km-single-0001'",
                                (self.pid,)).fetchone()[0]
        self.assertEqual(left, 0, "旧式确定性单文件标签必须仍被回收")


class LargePackageSettleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(Config, "SCAN_ALLOWED_ROOTS", ()),
            patch.object(Config, "LP_TOPIC_MERGE", True),
            patch.object(Config, "LP_TOPIC_MERGE_MIN_GROUPS", 12),
        ]
        for item in self.patches:
            item.start()
        self.store = LargePackageStore(self.root / "state.db")
        self.pid = self.store.create(self.root)["id"]
        with closing(self.store._connect()) as conn:
            semantic.init(conn)
            naming.init(conn)
        self.store.update_package(self.pid, status="waiting_for_selection",
                                  phase="catalog_ready", progress=70)
        self.worker = LargePackageWorker(store=self.store, poll_seconds=0.2)

    def tearDown(self):
        if self.worker._naming_executor is not None:
            self.worker._naming_executor.shutdown(wait=False, cancel_futures=True)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _seed_groups(self, count, labelled=True):
        """直接写入叶组（必要时带标签），不经过嵌入与聚类流水线。"""
        now = time.time()
        with closing(self.store._connect()) as conn:
            for index in range(count):
                group_id = "g%03d" % index
                conn.execute(
                    "INSERT OR REPLACE INTO large_catalog_groups"
                    "(package_id,id,model,anchor,representative,result,status,"
                    "updated_at,member_count,content_revision) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (self.pid, group_id, "fixture", b"", "file%d.txt" % index,
                     "{}", "queued", now, 1, 1))
                if labelled:
                    conn.execute(
                        "INSERT OR REPLACE INTO lp_catalog_labels VALUES"
                        "(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self.pid, group_id, 1, 1, "name%d" % index, "desc", "[]",
                         "deterministic", "digest%d" % index, 0, 0, now))
            conn.commit()

    # ------------------------------------------------------------------
    # 1. 叶组数不足 → 主题层不可能生成 → 必须记为定稿，不再重试
    # ------------------------------------------------------------------
    def test_topic_merge_is_terminal_when_no_groups(self):
        self._seed_groups(0)
        with patch.object(naming, "merge_topics") as merge:
            self.assertFalse(self.worker._merge_topics_once(self.pid))
            self.assertFalse(self.worker._merge_topics_once(self.pid))
            self.assertFalse(self.worker._merge_topics_once(self.pid))
            merge.assert_not_called()
        self.assertIn(self.pid, self.worker._topics_merged,
                      "叶组数不足时必须记为定稿，否则每轮命名切片都会空转")

    # ------------------------------------------------------------------
    # 2. 反向保护：叶组数足够时主题归并仍须执行（不能过度收缩）
    # ------------------------------------------------------------------
    def test_topic_merge_still_runs_when_enough_leaves(self):
        self._seed_groups(12)
        round_row = {"id": "round-fixture", "deadline": time.time() + 600}
        with patch.object(naming, "ensure_round", return_value=round_row), \
                patch.object(naming, "merge_topics", return_value=False) as merge:
            self.worker._merge_topics_once(self.pid)
            merge.assert_called_once()
        self.assertNotIn(self.pid, self.worker._topics_merged,
                         "归并未成功时不得记为定稿，否则主题层会永久缺失")

    # ------------------------------------------------------------------
    # 3. 目录签名必须能反映进展（否则会把"还有活"误判成定稿）
    # ------------------------------------------------------------------
    def test_settle_signature_tracks_progress(self):
        self._seed_groups(2)
        first = self.worker._catalog_settle_signature(self.pid)
        self.assertIsNotNone(first)
        self.assertEqual(first[0], 2, "签名第一位是叶组数")
        self.assertEqual(first[3], 2, "签名第四位是已有标签数")

        self._seed_groups(3, labelled=False)    # 新增叶组，但这一组还没有标签
        second = self.worker._catalog_settle_signature(self.pid)
        self.assertNotEqual(first, second, "新增叶组必须改变签名")
        self.assertEqual(second[3], 2, "新增组未带标签，标签数应保持 2")

        with closing(self.store._connect()) as conn:   # 只补标签（结构不变）
            conn.execute(
                "INSERT OR REPLACE INTO lp_catalog_labels VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.pid, "g002", 1, 1, "new", "d", "[]", "deterministic",
                 "dg", 0, 0, time.time()))
            conn.commit()
        third = self.worker._catalog_settle_signature(self.pid)
        self.assertNotEqual(second, third, "补齐标签也必须改变签名")
        self.assertEqual(third[3], 3, "补齐一个标签后标签数应为 3")

    # ------------------------------------------------------------------
    # 4. 核心：排空且无进展时，后续切片不再重复执行兜底与归并
    #    这条在改动前会失败（每轮都会调用一次）
    # ------------------------------------------------------------------
    def test_drained_slice_does_not_repeat_fallback_and_merge(self):
        self._seed_groups(2)
        calls = {"fallback": 0, "merge": 0}

        def fallback(_package_id):
            calls["fallback"] += 1

        def merge_once(_package_id):
            calls["merge"] += 1
            return False

        with patch("services.large_package_catalog_batch.process_batch",
                   return_value=False), \
                patch.object(self.worker, "_name_fallback", side_effect=fallback), \
                patch.object(self.worker, "_merge_topics_once", side_effect=merge_once), \
                patch.object(self.worker, "_build_overview", return_value=False), \
                patch.object(self.worker, "_overview_catchup", return_value=False):
            for _ in range(3):
                self.worker._submit_naming(self.pid)
                self.worker._naming_future.result()

        self.assertEqual(calls["fallback"], 1,
                         "排空且无进展后不应重复执行兜底命名")
        self.assertEqual(calls["merge"], 1,
                         "排空且无进展后不应重复执行主题归并")

    # ------------------------------------------------------------------
    # 5. 有进展时必须继续工作（防止把"还有活"误判成定稿）
    # ------------------------------------------------------------------
    def test_progress_keeps_the_slice_working(self):
        self._seed_groups(2)
        calls = {"fallback": 0}

        def fallback(_package_id):
            calls["fallback"] += 1
            # 每轮都新增一个叶组，模拟"还在不断产生新分组"的包
            with closing(self.store._connect()) as conn:
                gid = "extra%03d" % calls["fallback"]
                conn.execute(
                    "INSERT OR REPLACE INTO large_catalog_groups"
                    "(package_id,id,model,anchor,representative,result,status,"
                    "updated_at,member_count,content_revision) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (self.pid, gid, "fixture", b"", "extra.txt", "{}",
                     "queued", time.time(), 1, 1))
                conn.commit()

        with patch("services.large_package_catalog_batch.process_batch",
                   return_value=False), \
                patch.object(self.worker, "_name_fallback", side_effect=fallback), \
                patch.object(self.worker, "_merge_topics_once", return_value=False), \
                patch.object(self.worker, "_build_overview", return_value=False), \
                patch.object(self.worker, "_overview_catchup", return_value=False):
            for _ in range(3):
                self.worker._submit_naming(self.pid)
                self.worker._naming_future.result()

        self.assertEqual(calls["fallback"], 3,
                         "目录仍在变化时必须继续执行兜底命名")


if __name__ == "__main__":
    unittest.main()
