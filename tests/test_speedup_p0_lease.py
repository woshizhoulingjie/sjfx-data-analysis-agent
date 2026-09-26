# -*- coding: utf-8 -*-
"""P0 租约竞态测试（提速方案 §3B-4）。

任何 stale 调用都不得改变文件、组或队列。
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

from services.large_package_isolated import LargePackageStore
from services import large_package_catalog as catalog
from services import large_package_semantic as semantic


class LeaseRaceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="p0lease-")
        self.store = LargePackageStore(db_path=os.path.join(self.dir, "lp.db"))
        self.pid = "lp-p0lease"
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO packages(id,root_path,owner_id,status,phase,progress,counts,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (self.pid, "/tmp/p0", "owner-1", "waiting_for_selection", "catalog_build", 50, "{}", time.time(), time.time()))
            c.commit()
        self.path = "a.txt"
        self.digest = "digest-1"
        self._add_file(self.path, self.digest)
        self._queue(self.path, "running", token="tok-A")

    def tearDown(self):
        for root, _dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            os.rmdir(root)

    # ---------- helpers ----------
    def _add_file(self, path, digest):
        portrait = {"catalog_input_hash": digest, "catalog_status": "pending"}
        local = {"portrait": portrait, "sample": "正文样本足够长用于测试占位内容填充" * 2}
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO files(package_id,path,name,size,local_summary,deep_summary,"
                      "quick_status,deep_status,sensitive,recognition_status,confidence,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, path, path, 10, json.dumps(local, ensure_ascii=False), "{}",
                       "completed", "idle", 0, "needs_review", 0, time.time()))
            c.commit()

    def _queue(self, path, status, token):
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO queue(package_id,path,stage,status,attempts,leased_at,lease_token,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (self.pid, path, "catalog", status, 1, time.time(), token, time.time()))
            c.commit()

    def _queue_row(self):
        with closing(self.store._connect()) as c:
            c.row_factory = sqlite3.Row
            return c.execute("SELECT * FROM queue WHERE package_id=? AND path=? AND stage='catalog'",
                             (self.pid, self.path)).fetchone()

    def _file_portrait(self):
        with closing(self.store._connect()) as c:
            row = c.execute("SELECT local_summary FROM files WHERE package_id=? AND path=?",
                            (self.pid, self.path)).fetchone()
        return (json.loads(row[0]) or {}).get("portrait") or {}

    def _result(self, topic="测试主题"):
        return {"topic": topic, "category": topic, "model": "m",
                "quotes": [{"source": "content", "text": "x"}], "reason": "r"}

    # ---------- 1) 旧 token 晚到 ----------
    def test_stale_token_late_save_cannot_touch_files_or_queue(self):
        """别人已重领（token=B），持旧 token=A 的晚到结果必须整事务放弃。"""
        self._queue(self.path, "running", token="tok-B")     # 已重领
        before_q = dict(self._queue_row())
        before_p = self._file_portrait()
        ok = catalog._save(self.store, self.pid, self.path, self.digest,
                           self._result("旧名称"), token="tok-A")
        self.assertFalse(ok, "过期 token 的 _save 必须返回 False")
        self.assertEqual(dict(self._queue_row()), before_q, "队列被改动")
        self.assertEqual(self._file_portrait(), before_p, "文件画像被改动")

    # ---------- 2) 输入 digest 变化 ----------
    def test_changed_input_digest_is_not_overwritten(self):
        """文件内容已变（digest-2），拿旧 digest-1 的结果不得写进去。"""
        self._add_file(self.path, "digest-2")
        before_p = self._file_portrait()
        ok = catalog._save(self.store, self.pid, self.path, "digest-1",
                           self._result("旧内容名称"), token="tok-A")
        self.assertFalse(ok, "digest 不匹配必须返回 False")
        self.assertEqual(self._file_portrait(), before_p, "文件画像被旧 digest 覆盖")

    # ---------- 3) 队列项已收尾 ----------
    def test_completed_queue_row_cannot_be_rewritten(self):
        """队列项已是 completed，晚到的 _save 不得再改它。"""
        self._queue(self.path, "completed", token="tok-A")
        before_q = dict(self._queue_row())
        ok = catalog._save(self.store, self.pid, self.path, self.digest,
                           self._result("晚到名称"), token="tok-A")
        self.assertFalse(ok, "已完成队列项不得被改写")
        self.assertEqual(dict(self._queue_row()), before_q)

    # ---------- 4) _mark_unusable 半写 ----------
    def test_mark_unusable_does_not_half_write_on_token_mismatch(self):
        """token 不匹配：文件画像与队列都必须保持原样（原实现会先改文件）。"""
        before_p = self._file_portrait()
        before_q = dict(self._queue_row())
        semantic._mark_unusable(self.store, self.pid, self.path, "too_little_text", "tok-WRONG")
        self.assertEqual(self._file_portrait(), before_p,
                         "token 不匹配时文件画像被改（半写缺陷）")
        self.assertEqual(dict(self._queue_row()), before_q,
                         "token 不匹配时队列被改")

    def test_mark_unusable_still_works_with_correct_token(self):
        """正向路径不能被上面的加固破坏。"""
        semantic._mark_unusable(self.store, self.pid, self.path, "too_little_text", "tok-A")
        self.assertEqual(self._file_portrait().get("catalog_quality"), "too_little_text")
        self.assertEqual(self._queue_row()["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
