"""第 3.1 项回归测试：队列租约令牌。

背景（生产实测）：`queue` 表原先没有租约令牌，`stale_running_to_retry()` 只是把
失联的 `running` 全局改成 `retry`，不校验持有者。于是一个"被判失联"的旧 worker
晚到之后**仍能提交成功、覆盖别人的结果**。

这里锁定四件事：
  1. 每次领取都生成新令牌，且写入数据库；
  2. 回收（`stale_running_to_retry`）会轮换掉令牌，旧持有者随后提交**被拒绝**；
  3. 令牌不匹配的提交/回退一律拒绝，且不改变队列状态；
  4. 不带令牌的旧调用方行为不变（向后兼容，避免影响既有测试与部署）。

注：这是**新增能力**而非修既有 bug，所以在基线代码上这些用例是因为缺少
`lease_token` 列 / `token` 参数而失败，而不是断言不成立。
"""
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.large_package_isolated import LargePackageStore


class QueueLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patcher = patch.object(Config, "SCAN_ALLOWED_ROOTS", ())
        self.patcher.start()
        self.store = LargePackageStore(self.root / "state.db")
        self.pid = self.store.create(self.root)["id"]
        self.store.upsert_file(self.pid, {"path": "a.txt", "name": "a.txt", "size": 10})

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _row(self, path="a.txt", stage="quick"):
        with closing(self.store._connect()) as conn:
            return conn.execute(
                "SELECT status, lease_token, attempts FROM queue "
                "WHERE package_id=? AND path=? AND stage=?", (self.pid, path, stage)).fetchone()

    # 1 + 2 + 3 ----------------------------------------------------------
    def test_reclaim_invalidates_the_previous_lease(self):
        claimed = self.store.claim("quick", 1, package_id=self.pid)
        self.assertEqual(len(claimed), 1)
        _package, path, token = claimed[0]
        self.assertTrue(token, "领取必须返回一个非空租约令牌")
        row = self._row()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["lease_token"], token, "令牌必须落库")

        # 模拟失联回收：把 updated_at 拨旧，再走真实的回收路径
        with closing(self.store._connect()) as conn:
            conn.execute("UPDATE queue SET updated_at=? WHERE package_id=? AND path=?",
                         (time.time() - 7200, self.pid, path))
        self.store.stale_running_to_retry(seconds=60)
        row = self._row()
        self.assertEqual(row["status"], "retry")
        self.assertIsNone(row["lease_token"], "回收必须轮换掉旧令牌")

        # 旧持有者晚到：必须被拒绝，且不得改变队列状态
        self.assertFalse(self.store.finish_queue(self.pid, path, "quick", True, token=token),
                         "被判失联的旧持有者不得提交成功")
        self.assertEqual(self._row()["status"], "retry", "被拒绝的提交不得改变状态")

        # 重新领取会拿到新令牌，且能正常收尾
        reclaimed = self.store.claim("quick", 1, package_id=self.pid)
        self.assertEqual(len(reclaimed), 1)
        _p, _path, new_token = reclaimed[0]
        self.assertNotEqual(new_token, token, "重新领取必须换新令牌")
        self.assertTrue(self.store.finish_queue(self.pid, path, "quick", True, token=new_token))
        self.assertEqual(self._row()["status"], "completed")
        self.assertIsNone(self._row()["lease_token"], "完成后应清掉令牌")

    def test_wrong_token_is_rejected(self):
        claimed = self.store.claim("quick", 1, package_id=self.pid)
        _package, path, token = claimed[0]
        self.assertFalse(self.store.finish_queue(self.pid, path, "quick", True, token="bogus"))
        self.assertFalse(self.store.requeue(self.pid, path, "quick", token="bogus"))
        row = self._row()
        self.assertEqual(row["status"], "running", "令牌错误时状态不得改变")
        self.assertEqual(row["lease_token"], token)
        # 正确令牌仍然可用
        self.assertTrue(self.store.requeue(self.pid, path, "quick", token=token))
        self.assertEqual(self._row()["status"], "queued")
        self.assertIsNone(self._row()["lease_token"])

    # 4 ------------------------------------------------------------------
    def test_legacy_call_without_token_keeps_working(self):
        """既有调用方（主要是测试）不传令牌时必须保持改动前的行为。"""
        self.store.claim("quick", 1, package_id=self.pid)
        self.assertTrue(self.store.finish_queue(self.pid, "a.txt", "quick", True))
        self.assertEqual(self._row()["status"], "completed")
        self.assertIsNone(self._row()["lease_token"], "旧路径也应清掉残留令牌")


if __name__ == "__main__":
    unittest.main()
