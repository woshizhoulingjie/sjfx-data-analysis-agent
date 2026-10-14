"""第 1 批回归测试：预览车道的滚动补位（慢文件不得阻塞空闲车道）。

背景：`_quick_one` 改动前是"先领满 `LP_PREVIEW_CONCURRENCY` 个文件，再按
**提交顺序**逐个 `future.result()`"。预览预算是 4 秒/文件（含超时），
因此一个慢文件会把同批里已经跑完的车道一起挂住——并发度是 4 时，
最坏情况有 3 条空闲车道在等一个慢文件。

这条在改动前是**确定性失败**的：第一个被领取的慢文件会先被 `future.result()`
阻塞，于是 0.9 秒内不会产生任何完成记录；改成"完成即收 + 空闲车道立刻补位"后，
快文件在 0.02 秒即可完成。

注意：`claim()` 对 quick 阶段按 `ORDER BY updated_at` 取件，所以这里让
slow.txt 先入库以获得严格更小的 `updated_at`，从而保证它被第一个领取——
否则用例会依赖 SQLite 的并列排序行为而不稳定。

接口搬迁说明：第 3 批把"写预览 + 结束队列项"合并成原子的 `_commit_preview`
（改动前是 `_save_preview()` 之后 `finish_queue(token=lease)`；租约被拒时预览
已经落库，旧持有者能覆盖新结果）。本用例考的是**车道调度**（完成即收 +
空闲车道补位），不是落库细节，因此跟随接口改桩。
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.large_package_isolated import LargePackageStore
from services.large_package_preview_runner import PreviewCancelled
from large_package_worker import LargePackageWorker


class _FakePreviewRunner:
    """按文件名睡眠的假预览子进程：slow.txt 慢，其余很快。"""

    SLOW_SECONDS = 1.5
    FAST_SECONDS = 0.02

    def close(self):
        return None

    def run(self, root, item, refine=False, cancel_check=None):
        delay = self.SLOW_SECONDS if item.get("name") == "slow.txt" else self.FAST_SECONDS
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if cancel_check and cancel_check():
                raise PreviewCancelled()
            time.sleep(0.01)
        return {"summary_type": "local", "portrait": {}, "category": "fixture"}, None


class PreviewLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(Config, "SCAN_ALLOWED_ROOTS", ()),
            patch.object(Config, "LP_PREVIEW_CONCURRENCY", 2),
        ]
        for item in self.patches:
            item.start()
        self.store = LargePackageStore(self.root / "state.db")
        self.pid = self.store.create(self.root)["id"]
        self._add_file("slow.txt")          # 先入库 → 被第一个领取
        time.sleep(0.02)
        for index in range(6):
            self._add_file("fast%d.txt" % index)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _add_file(self, name):
        self.store.upsert_file(self.pid, {"path": name, "name": name, "size": 100})

    def test_slow_file_does_not_block_free_lanes(self):
        completed = []
        errors = []

        worker = LargePackageWorker(store=self.store, poll_seconds=0.2)
        worker.preview_runner = _FakePreviewRunner()
        worker._preview_runners = [worker.preview_runner,_FakePreviewRunner()]
        commit=self.store.commit_stage_batch
        def capture(items):
            result=commit(items)
            completed.extend(item["path"] for item,(ok,_) in zip(items,result) if ok)
            return result
        flag = {"stop": False}
        package = {"id": self.pid, "root_path": str(self.root)}

        def drive():
            try:
                worker._quick_one(package)
            except Exception as exc:                     # pragma: no cover - 诊断用
                errors.append(exc)

        with patch("services.large_package_preview_runner.PreviewRunner",
                   _FakePreviewRunner), \
                patch.object(worker, "_interrupted",
                             side_effect=lambda _pid: flag["stop"]), \
                patch.object(self.store, "commit_stage_batch", side_effect=capture), \
                patch.object(worker, "_catalog_incremental", return_value=0):
            thread = threading.Thread(target=drive, daemon=True)
            thread.start()
            time.sleep(0.9)
            snapshot = list(completed)          # 0.9 秒时刻的快照，用于断言与报错
            finished_fast = [path for path in snapshot if path != "slow.txt"]
            flag["stop"] = True
            thread.join(timeout=15)

        self.assertEqual(errors, [], "预览车道不应抛异常")
        self.assertTrue(
            finished_fast,
            "慢文件不得阻塞空闲车道：0.9 秒内应至少完成一个快文件，"
            "实际 0.9 秒快照=%r（若为空，说明仍在按提交顺序等整批收结果；"
            "收尾后累计完成=%r）" % (snapshot, completed))
        self.assertNotIn("slow.txt", snapshot,
                         "0.9 秒内慢文件不应完成（它需要 1.5 秒），"
                         "否则这个用例无法区分两种实现")


if __name__ == "__main__":
    unittest.main()
