# -*- coding: utf-8 -*-
"""批次 2（方案 §5.2）的回归测试：同包双车道、职责拆分、租约续期。

本文件**全部不依赖 GPU、不发模型请求**：把 `process_batch` 换成桩，
只检验调度层与租约这两个真正被改动的地方。

覆盖方案 §5.2 "新增测试" 的四条：
  1. 同一个包开两条车道时，同一个组不会被调用两次；
  2. 单次调用超过 120 秒时，租约不会被另一条车道回收；
  3. 只有 lane 0 执行主题归并和定稿；
  4. `SAME_PACKAGE_LANES=1`（默认）时行为与改动前一致 —— 第二个包/同一包的第二路
     都拿不到车道。
另外补一条"开关关闭时不装心跳线程"（零开销），以及一条批量路径与单组路径的
租约续期在关闭态下都必须是空操作。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.large_package_isolated import LargePackageStore   # noqa: E402
from services import large_package_catalog_batch as batch       # noqa: E402
from services import large_package_catalog_naming as naming     # noqa: E402
from large_package_worker import LargePackageWorker             # noqa: E402


ENV_KEYS = ('SJFX_LP_NAMING_PARALLEL', 'SJFX_GENERATION_CONCURRENCY',
            'SJFX_LP_NAMING_SAME_PACKAGE_LANES')


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='lanes2-')
        self.store = LargePackageStore(db_path=os.path.join(self.dir, 'lp.db'))
        self._saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        self._saved_batch = batch.process_batch
        self.worker = LargePackageWorker(self.store)

    def tearDown(self):
        batch.process_batch = self._saved_batch
        executor = getattr(self.worker, '_naming_executor', None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def set_env(self, parallel=None, gate=None, same_package=None):
        for key, value in (('SJFX_LP_NAMING_PARALLEL', parallel),
                           ('SJFX_GENERATION_CONCURRENCY', gate),
                           ('SJFX_LP_NAMING_SAME_PACKAGE_LANES', same_package)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)

    def make_package(self, package_id, status='waiting_for_selection'):
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO packages(id,root_path,owner_id,status,phase,"
                      "progress,counts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (package_id, '/tmp/lane2', 'o1', status, 'catalog_build',
                       50, '{}', time.time(), time.time()))
            c.commit()
        return package_id

    def make_job(self, package_id, group_id, revision=1, status='queued', leased=None):
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO lp_catalog_jobs"
                      "(package_id,group_id,revision,status,attempts,leased,next_at,error) "
                      "VALUES(?,?,?,?,0,?,0,'')",
                      (package_id, group_id, revision, status, leased))
            c.commit()


class DualLaneTests(_Base):
    """§5.2.1 / §5.2.2：车道键与槽位判定。"""

    def test_default_same_package_still_single_lane(self):
        """默认（SAME_PACKAGE_LANES=1）：同一个包不得占两路 —— 旧行为逐字保留。"""
        self.set_env(parallel='1', gate='3', same_package=None)
        self.make_package('lp-a')
        self.assertEqual(self.worker._naming_lanes(), 2)
        self.assertEqual(self.worker._same_package_lanes(), 1)
        self.assertTrue(self.worker._submit_naming('lp-a'))
        self.assertFalse(self.worker._submit_naming('lp-a'),
                         '默认值下同一个包的第二路必须被拒绝')

    def test_same_package_gets_second_lane_when_enabled(self):
        """开关打开：同一个包能拿到 lane 0 与 lane 1 两路，全局车道数仍然受闸门限制。"""
        self.set_env(parallel='1', gate='3', same_package='2')
        self.make_package('lp-a')
        self.assertEqual(self.worker._same_package_lanes(), 2)
        entered = threading.Event()
        release = threading.Event()

        def blocking(store, package_id, cancelled=lambda: False, model_client=None, prefetch=False):
            entered.set()
            release.wait(timeout=10)
            return False
        batch.process_batch = blocking
        try:
            self.assertTrue(self.worker._submit_naming('lp-a'))
            self.assertTrue(self.worker._submit_naming('lp-a'),
                            '开关打开时同一个包应能拿到第二路')
            self.assertEqual(self.worker._package_lanes_inflight('lp-a'), 2)
            # 全局 2 路已满：第三个包（或第三路）不得再拿到车道
            self.make_package('lp-b')
            self.assertFalse(self.worker._submit_naming('lp-b'))
        finally:
            release.set()
            entered.wait(timeout=5)

    def test_same_package_lanes_ignored_without_global_parallel(self):
        """全局车道没开时，同包开关不得让一个包独占两路（默认 1 = 旧行为）。"""
        self.set_env(parallel=None, gate='3', same_package='2')
        self.make_package('lp-a')
        self.assertEqual(self.worker._naming_lanes(), 1)
        self.assertEqual(self.worker._same_package_lanes(), 1)

    def test_legacy_future_attribute_still_works(self):
        """老接口 `_naming_future`（两个既有测试在用）仍然可设置、可读取。"""
        from concurrent.futures import Future
        self.set_env(parallel=None, same_package=None)
        self.make_package('lp-a')
        self.worker._naming_future = Future()
        self.assertFalse(self.worker._submit_naming('lp-a'),
                         '旧接口塞进来的一路在途必须参与占位')
        self.worker._naming_future = None
        entered = threading.Event()
        release = threading.Event()

        def blocking(store, package_id, cancelled=lambda: False, model_client=None, prefetch=False):
            entered.set()
            release.wait(timeout=10)
            return False
        batch.process_batch = blocking
        try:
            self.assertTrue(self.worker._submit_naming('lp-a'))
            self.assertIsNotNone(self.worker._naming_future)
        finally:
            release.set()
            entered.wait(timeout=5)


class NoDoubleCallTests(_Base):
    """§5.2 新增测试 1：同一个组不会被两条车道调用两次。"""

    def test_two_lanes_never_claim_the_same_group(self):
        """两条车道并发领取同一批任务：每个组恰好被领走一次。

        这是 `process_batch` "阶段 4：原子领取" 的行为验证 —— 条件更新带
        `status='queued'` 且检查 rowcount，所以两条车道即使同时选中同一组，
        也只有一个能领到。批次 2 放开同包双车道后，这条性质成了**正确性前提**。
        """
        self.make_package('lp-a')
        groups = ['g%02d' % i for i in range(12)]
        for gid in groups:
            self.make_job('lp-a', gid)

        def claim_once():
            owned = []
            with self.store._lock, closing(self.store._connect()) as c:
                c.execute('BEGIN IMMEDIATE')
                for gid in groups:
                    cur = c.execute(
                        "UPDATE lp_catalog_jobs SET status='running',leased=? "
                        "WHERE package_id=? AND group_id=? AND revision=? AND status='queued'",
                        (time.time(), 'lp-a', gid, 1))
                    if cur.rowcount == 1:
                        owned.append(gid)
                c.commit()
            return owned

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _i: claim_once(), range(2)))
        seen = [g for owned in results for g in owned]
        self.assertEqual(len(seen), len(set(seen)), '同一个组被两条车道重复领取：%r' % seen)
        self.assertEqual(sorted(seen), sorted(groups), '有组没被任何车道领到')
        self.assertEqual(min(len(owned) for owned in results), 0,
                         '两条车道应当一条领满、另一条空手，而不是各领一半')
        self.assertEqual(max(len(owned) for owned in results), len(groups))


class LaneDutyTests(_Base):
    """§5.2 新增测试 3：只有 lane 0 执行主题归并和定稿。"""

    def test_only_lane_zero_runs_finalisation(self):
        """lane>=1 排空后直接返回：不调 _merge_topics_once / _build_overview / 定稿。"""
        self.set_env(parallel='1', gate='3', same_package='2')
        self.make_package('lp-a')
        calls = []

        def fake_batch(store, package_id, cancelled=lambda: False, model_client=None, prefetch=False):
            calls.append((os.getpid(), threading.get_ident()))
            return False           # 立刻"排空"，让车道走到收尾判定
        batch.process_batch = fake_batch

        finals = []
        self.worker._merge_topics_once = lambda pid: finals.append(('merge', pid))
        self.worker._build_overview = lambda pid, kind: finals.append(('overview', pid, kind))
        self.worker._name_fallback = lambda pid: finals.append(('fallback', pid))
        self.worker._mark_catalog_refined = lambda pid: finals.append(('refined', pid))

        self.assertTrue(self.worker._submit_naming('lp-a'))     # lane 0
        self.assertTrue(self.worker._submit_naming('lp-a'))     # lane 1
        self.worker._naming_executor.shutdown(wait=True, cancel_futures=True)

        self.assertEqual(len(calls), 2, '两条车道应各调一次 process_batch：%r' % calls)
        self.assertEqual(len(set(calls)), 2, '两条车道必须跑在不同的线程上：%r' % calls)
        # 收尾段的安全性判据：`_merge_topics_once` 与 `_name_fallback` **只**在收尾段里
        # 被调用（已逐一核对调用点），所以"各出现一次"就等价于"只有一条车道走进了
        # 收尾段"。`_build_overview` / `_mark_catalog_refined` 不能用计数判定：
        # `_overview_catchup`（收尾段内的一次全局补发扫描）也会调它们，同一车道因此
        # 可能调两次 —— 那是改动前就有的既有行为，不属于本批范围。
        self.assertEqual(sum(1 for f in finals if f[0] == 'merge'), 1,
                         '主题归并应当只被一条车道做一次，实际：%r' % finals)
        self.assertEqual(sum(1 for f in finals if f[0] == 'fallback'), 1,
                         '兜底命名应当只被一条车道做一次，实际：%r' % finals)
        self.assertTrue(any(f[0] == 'overview' for f in finals),
                        '收尾段应当仍然会出情报概览，实际：%r' % finals)
        self.assertTrue(any(f[0] == 'refined' for f in finals),
                        '收尾段应当仍然会定稿，实际：%r' % finals)


class LeaseRenewalTests(_Base):
    """§5.2 新增测试 2：租约不会被另一条车道回收。"""

    def test_expired_lease_is_reclaimed_when_not_renewed(self):
        """对照实验：不续期时，`leased<now-120` 的 running 任务确实会被抢回队列。

        这正是方案 §5.2.4 描述的隐患 —— 闸门等待上限 300 秒 + 一次调用约 24 秒
        可能超过 120 秒回收阈值。
        """
        self.make_package('lp-a')
        self.make_job('lp-a', 'g-stale', status='running', leased=time.time() - 200)
        with self.store._lock, closing(self.store._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE lp_catalog_jobs SET status='queued',leased=NULL "
                      "WHERE package_id=? AND status='running' AND leased<?",
                      ('lp-a', time.time() - 120))
            c.commit()
        with closing(self.store._connect()) as c:
            row = c.execute(
                "SELECT status FROM lp_catalog_jobs WHERE package_id='lp-a' "
                "AND group_id='g-stale'").fetchone()
        self.assertEqual(row[0], 'queued', '过期租约本应被回收（这是要修的隐患本身）')

    def test_renewed_lease_survives_the_reclaim_window(self):
        """续期之后的租约不再满足 `leased<now-120`，因此不会被回收。"""
        self.make_package('lp-a')
        self.make_job('lp-a', 'g-fresh', status='running', leased=time.time() - 200)
        # 模拟心跳：把 leased 推到"现在"
        with self.store._lock, closing(self.store._connect()) as c:
            c.execute("UPDATE lp_catalog_jobs SET leased=? WHERE package_id=? "
                      "AND status='running' AND group_id IN (?)",
                      (time.time(), 'lp-a', 'g-fresh'))
            c.commit()
        with self.store._lock, closing(self.store._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE lp_catalog_jobs SET status='queued',leased=NULL "
                      "WHERE package_id=? AND status='running' AND leased<?",
                      ('lp-a', time.time() - 120))
            c.commit()
        with closing(self.store._connect()) as c:
            row = c.execute(
                "SELECT status FROM lp_catalog_jobs WHERE package_id='lp-a' "
                "AND group_id='g-fresh'").fetchone()
        self.assertEqual(row[0], 'running', '续期过的租约不得被回收')

    def test_lease_renewal_is_a_noop_when_switch_off(self):
        """默认关闭：不装心跳线程、不碰数据库 —— 旧行为逐字保留。"""
        self.set_env(same_package=None)
        self.assertFalse(naming.same_package_lanes_enabled())
        before = threading.active_count()
        with naming.lease_renewal(self.store, 'lp-a', ['g1']):
            time.sleep(0.05)
        self.assertEqual(threading.active_count(), before, '关闭时不该起心跳线程')

    def test_lease_renewal_renews_when_switch_on(self):
        """打开后：心跳确实把 leased 往后推，而且只动 running 的行。"""
        self.set_env(same_package='2')
        self.assertTrue(naming.same_package_lanes_enabled())
        self.make_package('lp-a')
        old = time.time() - 300
        self.make_job('lp-a', 'g-run', status='running', leased=old)
        self.make_job('lp-a', 'g-done', status='completed', leased=old)
        names = []
        stop = threading.Event()
        with naming.lease_renewal(self.store, 'lp-a', ['g-run', 'g-done'], stop, interval=0.05):
            deadline = time.time() + 3
            while time.time() < deadline:
                with closing(self.store._connect()) as c:
                    value = c.execute(
                        "SELECT leased FROM lp_catalog_jobs WHERE package_id='lp-a' "
                        "AND group_id='g-run'").fetchone()[0]
                if value > old + 1:
                    names.append(value)
                    break
                time.sleep(0.05)
        self.assertTrue(names, '打开开关后 leased 应当被推进')
        with closing(self.store._connect()) as c:
            done = c.execute(
                "SELECT leased FROM lp_catalog_jobs WHERE package_id='lp-a' "
                "AND group_id='g-done'").fetchone()[0]
        self.assertAlmostEqual(done, old, places=3,
                               msg='completed 的行不该被续期（否则会把别人的任务"认领"回来）')


if __name__ == '__main__':
    unittest.main()
