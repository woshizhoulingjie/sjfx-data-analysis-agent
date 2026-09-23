"""第 3 批回归测试：成员计数不变量与"假完成"。

3.2 成员计数
    成员写入原先统一使用 `INSERT OR REPLACE`。SQLite 在 REPLACE 冲突时会**隐式删除**
    旧行，而隐式删除只在 recursive_triggers 开启时才触发 DELETE 触发器——本项目的连接
    没有开启它（`large_package_isolated._connect` 只设了 busy_timeout/WAL/synchronous）。
    于是"从 A 组搬到 B 组"的文件会让 B 组 +1 而 A 组不减，计数只能单调上涨。
    生产实测：4/23 个包漂移（1040→1502、1040→1526、82→138），67 个组计数不符。
    这里用真实的 `kmeans_recluster` 路径跑两次不同划分，断言记数不变量。

3.4 "假完成"
    `_deep_finish` 原先只数 queued + running。`stale_running_to_retry()` 会把失联的
    running 打成 `retry`，于是**任何一次失联回收都会让包提前宣告完成**——
    生产实测 lp-1e0887：49 个成功 + 1 个失败却显示 progress=100 / report_ready。
    在 8 小时量级的深析里失联回收几乎必然发生，这条走的是主路径。
"""
import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

from config import Config
from services.large_package_isolated import LargePackageStore
from services import large_package_semantic as semantic
from large_package_worker import LargePackageWorker


class _Base(unittest.TestCase):
    FILES = 6

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(Config, "SCAN_ALLOWED_ROOTS", ()),
            patch.object(Config, "LP_CATALOG_KMODE", True),
            patch.object(Config, "LP_CATALOG_MODEL_NAMING", False),
            patch.object(Config, "LP_TOPIC_MERGE", False),
        ]
        for item in self.patches:
            item.start()
        self.store = LargePackageStore(self.root / "state.db")
        self.pid = self.store.create(self.root)["id"]
        with closing(self.store._connect()) as conn:
            semantic.init(conn)
        self.worker = LargePackageWorker(store=self.store, poll_seconds=0.2)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _seed_files(self, count=None):
        count = count or self.FILES
        rng = np.random.default_rng(7)
        now = time.time()
        vectors = {}
        with closing(self.store._connect()) as conn:
            for index in range(count):
                path = "f%d.txt" % index
                conn.execute(
                    "INSERT OR REPLACE INTO files(package_id,path,name,extension,size,"
                    "modified_at_ns,sha256,sensitive,file_type,quick_status,category,"
                    "confidence,local_summary,deep_summary,updated_at) "
                    "VALUES(?,?,?,'.txt',10,0,NULL,0,'.txt','completed','x',0,'{}','{}',?)",
                    (self.pid, path, path, now))
                vector = rng.standard_normal(8).astype(np.float32)
                vector /= np.linalg.norm(vector)
                vectors[path] = vector
                conn.execute(
                    "INSERT OR REPLACE INTO large_catalog_members"
                    "(package_id,path,group_id,input_hash,vector) VALUES(?,?,?,?,?)",
                    (self.pid, path, "seed-group", "h%d" % index, vector.tobytes()))
            conn.commit()
        return vectors

    def _counts(self):
        with closing(self.store._connect()) as conn:
            cached = dict(conn.execute(
                "SELECT id, member_count FROM large_catalog_groups WHERE package_id=?",
                (self.pid,)).fetchall())
            real = dict(conn.execute(
                "SELECT g.id, (SELECT COUNT(*) FROM large_catalog_members m "
                "  WHERE m.package_id=g.package_id AND m.group_id=g.id) "
                "FROM large_catalog_groups g WHERE g.package_id=?", (self.pid,)).fetchall())
        return cached, real


class MemberCountInvariantTests(_Base):
    """3.2：跨组迁移与重复写入都不得让 member_count 漂移。"""

    def test_cross_group_move_keeps_cached_counts_exact(self):
        self._seed_files()
        partitions = [[0, 0, 1, 1, 2, 2], [0, 1, 2, 0, 1, 2]]
        for partition in partitions:
            with patch.object(semantic, "_balanced_labels", return_value=list(partition)):
                semantic.kmeans_recluster(self.store, self.pid, force=True)
            cached, real = self._counts()
            self.assertEqual(
                cached, real,
                "跨组迁移后 member_count 缓存必须与真实成员数一致"
                "（改动前 INSERT OR REPLACE 的隐式删除不触发减计数触发器，"
                "计数只增不减）")
        self.assertEqual(sum(self._counts()[0].values()), self.FILES,
                         "所有组的缓存计数之和必须等于文件数")

    def test_replayed_identical_write_does_not_inflate_counts(self):
        self._seed_files()
        partition = [0, 0, 1, 1, 2, 2]
        with patch.object(semantic, "_balanced_labels", return_value=list(partition)):
            semantic.kmeans_recluster(self.store, self.pid, force=True)
            before = self._counts()[0]
            # 完全相同的内容与分组再写一次：不得产生任何计数变化
            semantic.kmeans_recluster(self.store, self.pid, force=True)
            after = self._counts()[0]
        self.assertEqual(before, after,
                         "分组与内容都没变的重放不得改变 member_count")

    def test_repair_member_counts_fixes_historical_drift(self):
        self._seed_files()
        with patch.object(semantic, "_balanced_labels", return_value=[0, 0, 1, 1, 2, 2]):
            semantic.kmeans_recluster(self.store, self.pid, force=True)
        with closing(self.store._connect()) as conn:      # 人为制造历史漂移
            conn.execute("UPDATE large_catalog_groups SET member_count=member_count+400 "
                         "WHERE package_id=?", (self.pid,))
            conn.commit()
        self.assertTrue(semantic.repair_member_counts(self.store, self.pid))
        cached, real = self._counts()
        self.assertEqual(cached, real, "repair_member_counts 必须把缓存修回真实值")
        self.assertEqual(sum(cached.values()), self.FILES)


class FalseCompletionTests(_Base):
    """3.4：retry 必须计入待办；selection 必须写终态。"""

    def _seed_deep(self, statuses):
        """铺 deep 行 + 一条**覆盖这些文件**的 selection。

        夹具修正：原先把 `paths` 写成 `[]`，selection 覆盖 0 个文件，
        `_finish_selection` 按"本批"口径只能给出 completed，用例却断言 partial
        —— 等于在考一个空批次。第 3 批的语义是"批次成败只看本批 paths"，
        所以夹具必须让 paths 与铺下的文件一致。
        """
        now = time.time()
        paths = ["d%d.txt" % index for index in range(len(statuses))]
        with closing(self.store._connect()) as conn:
            for index, status in enumerate(statuses):
                path = "d%d.txt" % index
                conn.execute(
                    "INSERT OR REPLACE INTO files(package_id,path,name,extension,size,"
                    "modified_at_ns,sha256,sensitive,file_type,quick_status,deep_status,"
                    "category,confidence,local_summary,deep_summary,updated_at) "
                    "VALUES(?,?,?,'.txt',10,0,NULL,0,'.txt','completed',?,"
                    "'x',0,'{}','{}',?)", (self.pid, path, path, status, now))
                conn.execute(
                    "INSERT OR REPLACE INTO queue(package_id,path,stage,status,attempts,"
                    "leased_at,error,updated_at,priority) "
                    "VALUES(?,?,'deep',?,0,NULL,NULL,?,0)", (self.pid, path, status, now))
            # `started_at` 是第 3 批新增的列。这里按实际 schema 决定是否写入，
            # 以便在基线代码上也能建出 selection——否则用例会因为"列不存在"而失败，
            # 掩盖真正的行为差异（retry 未计入待办导致的假完成）。
            columns = {row[1] for row in conn.execute("PRAGMA table_info(selections)")}
            if "started_at" in columns:
                conn.execute(
                    "INSERT OR REPLACE INTO selections(id,package_id,paths,rule,status,"
                    "created_at,updated_at,started_at) VALUES(?,?,?,'{}','running',?,?,?)",
                    ("sel-fixture", self.pid, json.dumps(paths), now, now, now))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO selections(id,package_id,paths,rule,status,"
                    "created_at,updated_at) VALUES(?,?,?,'{}','queued',?,?)",
                    ("sel-fixture", self.pid, json.dumps(paths), now, now))
            conn.commit()

    def test_retry_rows_block_completion(self):
        self._seed_deep(["completed", "completed", "retry"])
        with patch.object(self.worker, "_build_overview", return_value=False):
            finished = self.worker._deep_finish(self.pid)
        self.assertFalse(finished, "存在 retry 文件时不得判定深度阶段完成")
        self.assertNotEqual(self.store.get(self.pid).get("status"), "completed",
                            "存在 retry 文件时包不得进入 completed")

    def test_failed_rows_yield_completed_with_errors(self):
        self._seed_deep(["completed", "completed", "failed"])
        with patch.object(self.worker, "_build_overview", return_value=False):
            finished = self.worker._deep_finish(self.pid)
        self.assertTrue(finished)
        package = self.store.get(self.pid)
        self.assertEqual(package.get("status"), "completed")
        self.assertEqual((package.get("counts") or {}).get("terminal_state"),
                         "completed_with_errors",
                         "有失败项时必须与完全成功区分开")
        self.assertEqual((package.get("counts") or {}).get("deep_failed_files"), 1)
        with closing(self.store._connect()) as conn:
            row = conn.execute("SELECT status, finished_at FROM selections WHERE id=?",
                               ("sel-fixture",)).fetchone()
        self.assertEqual(row["status"], "partial")
        self.assertIsNotNone(row["finished_at"],
                             "selection 必须写入完成时间（改动前永远停在 queued）")

    def test_clean_run_marks_selection_completed(self):
        self._seed_deep(["completed", "completed", "completed"])
        with patch.object(self.worker, "_build_overview", return_value=False):
            self.worker._deep_finish(self.pid)
        with closing(self.store._connect()) as conn:
            row = conn.execute("SELECT status, finished_at FROM selections WHERE id=?",
                               ("sel-fixture",)).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual((self.store.get(self.pid).get("counts") or {}).get("terminal_state"),
                         "completed")


class CancelPackageTests(_Base):
    """取消必须是"单一入口、一个事务、活全清"，否则垃圾必然复现。"""

    def test_cancel_closes_queue_files_and_selection(self):
        self._seed_files()
        now = time.time()
        with closing(self.store._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO queue(package_id,path,stage,status,attempts,leased_at,"
                "error,updated_at,priority) VALUES(?,?,'deep','queued',0,NULL,NULL,?,0)",
                (self.pid, "f0.txt", now))
            conn.execute("UPDATE files SET deep_status='queued' WHERE package_id=? AND path='f0.txt'",
                         (self.pid,))
            conn.execute(
                "INSERT OR REPLACE INTO selections(id,package_id,paths,rule,status,created_at,"
                "updated_at,started_at) VALUES('sel-cancel',?,?,'{}','running',?,?,?)",
                (self.pid, json.dumps(["f0.txt"]), now, now, now))
            conn.commit()
        detail = self.store.cancel_package(self.pid)
        self.assertEqual(self.store.get(self.pid).get("status"), "cancelled")
        with closing(self.store._connect()) as conn:
            alive = conn.execute(
                "SELECT COUNT(*) FROM queue WHERE package_id=? AND status IN "
                "('queued','running','retry')", (self.pid,)).fetchone()[0]
            cancelled_rows = conn.execute(
                "SELECT COUNT(*) FROM queue WHERE package_id=? AND status='cancelled'",
                (self.pid,)).fetchone()[0]
            deepq = conn.execute("SELECT COUNT(*) FROM files WHERE package_id=? "
                                 "AND deep_status='queued'", (self.pid,)).fetchone()[0]
            sel = conn.execute("SELECT status,finished_at FROM selections WHERE id='sel-cancel'").fetchone()
        self.assertEqual(alive, 0, "取消后不得留下可被领取的活")
        self.assertGreaterEqual(cancelled_rows, 1, "队列项应落成独立的 cancelled 终态，而不是 failed")
        self.assertEqual(deepq, 0, "取消后不得留下 deep_status=queued 的文件")
        self.assertEqual(sel["status"], "cancelled")
        self.assertIsNotNone(sel["finished_at"], "取消也要写 finished_at，页面才能显示结束时间")
        # 幂等：再取消一次不得报错、也不得改出负数
        again = self.store.cancel_package(self.pid)
        self.assertEqual(again["queue_rows"], 0)

    def test_workflow_snapshot_reports_cancelled_control(self):
        self._seed_files()
        self.store.cancel_package(self.pid)
        snap = self.store.workflow_snapshot(self.pid)
        self.assertEqual(snap.get("control_state"), "cancelled")
        self.assertEqual(snap.get("pending_blocking"), 0)
        self.assertFalse((snap.get("allowed_actions") or {}).get("select"),
                         "已取消的包不得再允许选择深度范围")


if __name__ == "__main__":
    unittest.main()
