# -*- coding: utf-8 -*-
"""C（文档 §3C.2/§3C.4）：命名车道"有界两路在途"的回归测试。

不依赖 GPU、不发模型请求：把 `process_batch` 换成一个阻塞在 Event 上的桩，
只检验调度层的行为 ——
  · 开关关闭（默认）：第二个包拿不到车道（行为与改动前逐字一致）；
  · 开关打开且准入闸门放得下 2 路：两个包同时在途；
  · 同一个包不会被重复派两路；
  · 闸门只放 1 路时（SJFX_GENERATION_CONCURRENCY<=2）即使开关打开也夹紧成 1 路
    —— "在途 2"本身不是成功（文档 §3C 验收原话）。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.large_package_isolated import LargePackageStore   # noqa: E402
from services import large_package_catalog_batch as batch       # noqa: E402
from large_package_worker import LargePackageWorker             # noqa: E402


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='lanes-')
        self.store = LargePackageStore(db_path=os.path.join(self.dir, 'lp.db'))
        self.pids = []
        self._saved_batch = batch.process_batch
        self.entered = threading.Event()
        self.release = threading.Event()

        def blocking_batch(store, package_id, cancelled=lambda: False, model_client=None):
            self.entered.set()
            self.release.wait(timeout=10)
            return False
        batch.process_batch = blocking_batch

        self.worker = LargePackageWorker(self.store)
        for name in ('lp-lane-a', 'lp-lane-b'):
            self._make_package(name)
            self.pids.append(name)
        self._saved_env = os.environ.get('SJFX_LP_NAMING_PARALLEL')
        self._saved_gate = os.environ.get('SJFX_GENERATION_CONCURRENCY')

    def tearDown(self):
        self.release.set()
        batch.process_batch = self._saved_batch
        executor = getattr(self.worker, '_naming_executor', None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        for key, value in (('SJFX_LP_NAMING_PARALLEL', self._saved_env),
                           ('SJFX_GENERATION_CONCURRENCY', self._saved_gate)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _make_package(self, package_id):
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO packages(id,root_path,owner_id,status,phase,"
                      "progress,counts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (package_id, '/tmp/lane', 'o1', 'waiting_for_selection',
                       'catalog_build', 50, '{}', time.time(), time.time()))
            c.commit()

    def _submit_and_wait(self, package_id):
        ok = self.worker._submit_naming(package_id)
        if ok:
            self.entered.wait(timeout=5)
            self.entered.clear()
        return ok

    # ---------- 用例 ----------
    def test_switch_off_keeps_single_lane(self):
        os.environ.pop('SJFX_LP_NAMING_PARALLEL', None)
        os.environ['SJFX_GENERATION_CONCURRENCY'] = '3'
        self.assertTrue(self._submit_and_wait(self.pids[0]))
        self.assertFalse(self.worker._submit_naming(self.pids[1]),
                         '开关关闭时第二个包不得拿到车道')
        self.assertEqual(self.worker._naming_lanes(), 1)

    def test_switch_on_starts_two_packages(self):
        os.environ['SJFX_LP_NAMING_PARALLEL'] = '1'
        os.environ['SJFX_GENERATION_CONCURRENCY'] = '3'      # 后台名额 = 2
        self.assertEqual(self.worker._naming_lanes(), 2)
        self.assertTrue(self._submit_and_wait(self.pids[0]))
        self.assertTrue(self._submit_and_wait(self.pids[1]),
                        '开关打开且闸门有 2 个后台名额时，第二个包应能拿到第二路')
        self.assertEqual(sorted(self.worker._naming_inflight()), sorted(self.pids))

    def test_same_package_not_submitted_twice(self):
        os.environ['SJFX_LP_NAMING_PARALLEL'] = '1'
        os.environ['SJFX_GENERATION_CONCURRENCY'] = '3'
        self.assertTrue(self._submit_and_wait(self.pids[0]))
        self.assertFalse(self.worker._submit_naming(self.pids[0]),
                         '同一个包不得占两路（否则两组会互相抢同一批任务）')

    def test_gate_clamps_lanes_to_one(self):
        """准入闸门只放 1 路后台请求时，开关打开也必须夹紧 —— 不做无用功。"""
        os.environ['SJFX_LP_NAMING_PARALLEL'] = '1'
        os.environ['SJFX_GENERATION_CONCURRENCY'] = '2'      # 后台名额 = max(1, 2-1) = 1
        self.assertEqual(self.worker._naming_lanes(), 1)
        self.assertTrue(self._submit_and_wait(self.pids[0]))
        self.assertFalse(self.worker._submit_naming(self.pids[1]))

    def test_legacy_future_attribute_still_works(self):
        """老接口：`_naming_future` 仍可设置（当作一路在途）与读取。"""
        from concurrent.futures import Future
        os.environ.pop('SJFX_LP_NAMING_PARALLEL', None)
        self.worker._naming_future = Future()
        self.assertFalse(self.worker._submit_naming(self.pids[0]),
                         '旧接口塞进来的一路在途必须参与占位')
        self.worker._naming_future = None
        self.assertTrue(self._submit_and_wait(self.pids[0]))
        self.assertIsNotNone(self.worker._naming_future)


if __name__ == '__main__':
    unittest.main()
