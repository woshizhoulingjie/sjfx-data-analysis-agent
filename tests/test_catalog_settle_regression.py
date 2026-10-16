# -*- coding: utf-8 -*-
"""目录"一直显示正在完善"的回归测试（生产 lp-ba9afc71 / lp-7132acbf，2026-09-29）。

五个彼此独立的缺陷叠加，任何一个都会让目录永远收不了尾：
  1. representatives() 用原始下标索引按 usable 位置编号的 relevance → IndexError；
  2. 调度器把"重试已耗尽"的 deferred 当待办，每 2 秒把车道派给它（空转饿死别的包）；
  3. process_batch/process_one 在"没有待办"时仍 ensure_round → 刚结算的轮次被复活；
  4. validate_catalog 把单文件条目、无向量成员、空壳组算成阻断级违规；
  5. 同一主题内同名但内容不相容的叶组既不合并也不改名 → INV-3 永久阻断。
"""
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import numpy as np

import test_catalog_next_stage as hierarchy_fixture
from config import Config
from services import large_package_catalog_naming as naming


def _case():
    case = hierarchy_fixture.NextStageTests(); case.setUp()
    return case


def _branch(case, name, children):
    with closing(case.store._connect()) as c:
        c.execute("INSERT INTO lp_catalog_branches(package_id,id,name,children,revisions,round_id,updated) "
                  "VALUES(?,?,?,?,?,?,?)", (case.pid, 'topic:' + name, name,
                                           __import__('json').dumps(children), '{}', '', time.time()))
        c.commit()


def test_representatives_survives_members_without_vectors():
    """组里有成员没有向量时，usable 不是 0..n-1；补位排序不得越界、不得取错相关性。"""
    case = _case()
    try:
        v = np.zeros(8, dtype=np.float32); v[0] = 1
        case.add('g1', v, n=6)
        # 让前两个成员失去向量：usable = [2,3,4,5]，relevance 长度 4
        with closing(case.store._connect()) as c:
            c.execute("UPDATE large_catalog_members SET vector=? WHERE package_id=? AND path IN (?,?)",
                      (b'', case.pid, 'g1-0.txt', 'g1-1.txt'))
            c.commit()
        rows, group = naming.representatives(case.store, case.pid, 'g1')
        assert group and group['id'] == 'g1'
        paths = [r[0]['path'] for r in rows]
        assert paths, rows
        assert not ({'g1-0.txt', 'g1-1.txt'} & set(paths)), '没有向量的成员不应被当作代表'
    finally:
        case.doCleanups()


def test_validate_ignores_single_file_entries_vectorless_members_and_empty_shells():
    case = _case()
    try:
        v = np.zeros(8, dtype=np.float32); v[0] = 1
        case.add('g1', v, n=2); case.add('g2', v * -1 + 0.1, n=2)
        _branch(case, '主题甲', ['g1', 'g2'])
        with closing(case.store._connect()) as c:
            # 单文件条目：空质心、不在任何主题下（设计如此）
            c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at,member_count) "
                      "VALUES(?,?,?,?,?,?,1)", (case.pid, 'km-solo-x-00000', 'test', b'', 's.txt', time.time()))
            case.store.upsert_file(case.pid, dict(path='s.txt', name='s.txt', size=1, modified_at_ns=1))
            c.execute("INSERT INTO large_catalog_members VALUES(?,?,?,?,?)",
                      (case.pid, 's.txt', 'km-solo-x-00000', 'h', b''))
            rev = c.execute("SELECT content_revision FROM large_catalog_groups WHERE package_id=? AND id=?",
                            (case.pid, 'km-solo-x-00000')).fetchone()[0]
            c.execute('INSERT INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (case.pid, 'km-solo-x-00000', rev, 1, '单文件条目', '', '[]', 'test', 'd', 0, 0, time.time()))
            # k-means 之后被清空的占位壳：0 成员、无名字、无任务
            c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at,member_count) "
                      "VALUES(?,?,?,?,?,?,0)", (case.pid, 'km-pending-x', 'test', b'', '', time.time()))
            c.commit()
        verdict = naming.validate_catalog(case.store, case.pid)
        joined = ' '.join(verdict['violations'])
        assert 'INV-5' not in joined, verdict
        assert 'INV-7' not in joined, verdict
        assert verdict['checks']['INV5_placeholders']['files_in_single_file_entries'] == 1
        # 反向：一个**有向量**、却不在任何主题下的普通叶组仍然必须阻断
        case.add('g3', v, n=1)
        verdict = naming.validate_catalog(case.store, case.pid)
        assert any('INV-5' in x for x in verdict['violations']), verdict
    finally:
        case.doCleanups()


def test_incompatible_same_name_leaves_are_disambiguated_not_left_blocking():
    case = _case()
    try:
        a = np.zeros(8, dtype=np.float32); a[0] = 1
        b = np.zeros(8, dtype=np.float32); b[5] = 1
        case.add('big', a, n=3, name='生物医学基因表达研究')
        case.add('small', b, n=2, name='生物医学基因表达研究')
        _branch(case, '主题甲', ['big', 'small'])
        with patch('services.large_package_catalog_quality.compatible_groups', return_value=False):
            stats = naming.normalize_leaves(case.store, case.pid)
        assert stats.get('renamed_incompatible') == 1, stats
        with closing(case.store._connect()) as c:
            names = dict(c.execute("SELECT group_id,name FROM lp_catalog_labels WHERE package_id=?",
                                   (case.pid,)).fetchall())
            review = c.execute("SELECT needs_review FROM lp_catalog_labels WHERE package_id=? AND group_id='small'",
                               (case.pid,)).fetchone()[0]
            members = dict(c.execute("SELECT group_id,COUNT(*) FROM large_catalog_members WHERE package_id=? "
                                     "GROUP BY group_id", (case.pid,)).fetchall())
        assert names['big'] == '生物医学基因表达研究'           # 大组保留原名
        assert names['small'] == '生物医学基因表达研究·其二'     # 小组改名消歧
        assert review == 1
        assert members == {'big': 3, 'small': 2}                # 不合并、不搬成员
        assert not naming.duplicate_name_groups(case.store, case.pid)
        assert 'INV-3' not in ' '.join(naming.validate_catalog(case.store, case.pid)['violations'])
    finally:
        case.doCleanups()


def test_drained_round_is_not_revived_by_the_next_slice():
    """轮次排空结算为 completed 之后，再次进入命名切片不得把它续期回 active。"""
    case = _case()
    try:
        with patch.object(Config, 'LP_CATALOG_MODEL_NAMING', True):
            v = np.zeros(8, dtype=np.float32); v[0] = 1
            case.add('g1', v, n=2)
            naming.ensure_round(case.store, case.pid)
            with closing(case.store._connect()) as c:
                c.execute("DELETE FROM lp_catalog_jobs WHERE package_id=?", (case.pid,))
                c.commit()
            # A named live leaf is not drained until the worker has persisted
            # its independent scope. The old >=3 threshold hid this missing
            # work for the one-leaf fixture.
            from large_package_worker import LargePackageWorker
            with patch.object(Config, 'LP_TOPIC_MERGE', False), \
                    patch.object(naming, 'client', return_value=None):
                assert LargePackageWorker(case.store)._merge_topics_once(case.pid)
            naming.settle_round(case.store, case.pid)
            assert not naming.naming_work_pending(case.store, case.pid)
            from services import large_package_catalog_batch as batch
            for _ in range(3):
                batch.process_batch(case.store, case.pid)
                naming.process_one(case.store, case.pid)
            with closing(case.store._connect()) as c:
                statuses = [r[0] for r in c.execute("SELECT status FROM lp_catalog_rounds WHERE package_id=?",
                                                    (case.pid,))]
            assert 'active' not in statuses, statuses
            assert naming.state(case.store, case.pid)['active'] is False
            # 有新待办时仍然会建/续轮次（原行为不变）
            with closing(case.store._connect()) as c:
                c.execute("INSERT INTO lp_catalog_jobs(package_id,group_id,revision,status,attempts,next_at) "
                          "VALUES(?,?,?,?,0,0) ON CONFLICT(package_id,group_id) DO UPDATE SET status='queued',attempts=0",
                          (case.pid, 'g1', 99, 'queued'))
                c.commit()
            assert naming.naming_work_pending(case.store, case.pid)
    finally:
        case.doCleanups()


def test_exhausted_deferred_job_does_not_capture_the_background_lane():
    """重试已耗尽的 deferred 任务不得每轮都把车道抢走，目录队列必须轮得到。"""
    case = _case()
    try:
        from large_package_worker import LargePackageWorker
        with patch.object(Config, 'LP_CATALOG_MODEL_NAMING', True):
            v = np.zeros(8, dtype=np.float32); v[0] = 1
            case.add('g1', v, n=1)
            case.store.update_package(case.pid, status='waiting_for_selection', phase='catalog_ready')
            with closing(case.store._connect()) as c:
                c.execute("INSERT INTO lp_catalog_jobs(package_id,group_id,revision,status,attempts,next_at,error) "
                          "VALUES(?,?,?,?,?,?,?)", (case.pid, 'g1', 1, 'deferred',
                                                    int(getattr(Config, 'LP_NAME_MAX_ATTEMPTS', 5) or 5), 0, 'boom'))
                c.commit()
            worker = LargePackageWorker(store=case.store, poll_seconds=0.2)
            worker._topic_catchup = lambda: False
            worker._solo_catchup = lambda: 0
            submitted = []
            worker._submit_naming = lambda pid: submitted.append(pid) or True
            with patch('services.large_package_catalog_refine.process_one', return_value=False):
                worker._catalog_background()
            assert submitted == [], '耗尽的任务不应再被当成命名待办'
    finally:
        case.doCleanups()


class BackgroundNamingReadinessTests(unittest.TestCase):
    def _selection(self, round_status='completed', deadline_offset=-60, next_offset=0, only_expired=False):
        case = _case()
        self.addCleanup(case.doCleanups)
        old_pid, ready_pid = sorted([case.pid, case.store.create(case.root)['id']])
        now = time.time()
        with closing(case.store._connect()) as c:
            for pid in ((old_pid,) if only_expired else (old_pid, ready_pid)):
                c.execute("UPDATE packages SET status='completed',phase='report_ready' WHERE id=?", (pid,))
                c.execute("INSERT INTO lp_catalog_jobs(package_id,group_id,revision,next_at) VALUES(?,?,1,?)",
                          (pid, 'g1', now + next_offset if pid == old_pid and next_offset else 0))
            rounds = [] if only_expired else [(ready_pid, 'initial', now, now + 3600, 'active', 0)]
            if round_status is not None:
                # An older usable round must not hide the latest expired round.
                rounds.extend([(old_pid, 'earlier', now - 120, now + 3600, 'active', 0),
                               (old_pid, 'initial', now, now + deadline_offset, round_status,
                                1 if round_status == 'exhausted' else 0)])
            c.executemany("INSERT INTO lp_catalog_rounds(package_id,id,request_key,started,deadline,"
                          "status,requests,max_requests,max_tokens) VALUES(?,?,?,?,?,?,?,1,100)",
                          [(pid, key, key, started, deadline, status, requests)
                           for pid, key, started, deadline, status, requests in rounds])
            before = [tuple(row) for row in c.execute('SELECT * FROM lp_catalog_rounds ORDER BY package_id,id')]
            c.commit()
        from large_package_worker import LargePackageWorker
        worker = LargePackageWorker(store=case.store, poll_seconds=0.2)
        worker._topic_catchup = lambda: False
        worker._solo_catchup = lambda: 0
        worker._degraded_report_candidates = lambda: []
        worker._overview_catchup = lambda: False
        submitted = []
        worker._submit_naming = lambda pid: submitted.append(pid) or True
        with patch.object(Config, 'LP_CATALOG_MODEL_NAMING', True), \
                patch('services.large_package_catalog_refine.process_one', return_value=False):
            self.assertEqual(bool(worker._catalog_background()), not only_expired)
        with closing(case.store._connect()) as c:
            self.assertEqual(before, [tuple(row) for row in c.execute(
                'SELECT * FROM lp_catalog_rounds ORDER BY package_id,id')])
            self.assertEqual([(row['status'], row['attempts']) for row in c.execute(
                'SELECT status,attempts FROM lp_catalog_jobs')], [('queued', 0)] * (1 if only_expired else 2))
        return submitted, old_pid, ready_pid

    def test_latest_expired_round_does_not_starve_ready_package(self):
        submitted, _, ready_pid = self._selection()
        self.assertEqual(submitted, [ready_pid])

    def test_idle_fallback_does_not_submit_an_earlier_active_round(self):
        submitted, _, _ = self._selection(only_expired=True)
        self.assertEqual(submitted, [])

    def test_unexpired_or_missing_round_preserves_existing_initialization(self):
        for status in (None, 'active', 'completed', 'exhausted'):
            with self.subTest(status=status):
                submitted, old_pid, _ = self._selection(status, deadline_offset=3600)
                self.assertEqual(submitted, [old_pid])

    def test_future_job_does_not_starve_due_package(self):
        submitted, _, ready_pid = self._selection('active', deadline_offset=3600, next_offset=3600)
        self.assertEqual(submitted, [ready_pid])


def test_catalog_message_switches_to_refined_when_everything_is_done():
    case = _case()
    try:
        from large_package_worker import LargePackageWorker
        with patch.object(Config, 'LP_CATALOG_MODEL_NAMING', True):
            v = np.zeros(8, dtype=np.float32); v[0] = 1
            paths = case.add('g1', v, n=2)
            _branch(case, '主题甲', ['g1'])
            with closing(case.store._connect()) as c:
                c.execute("UPDATE queue SET status='completed' WHERE package_id=? AND stage IN ('catalog','quick','inventory')", (case.pid,))
                c.commit()
            case.store.update_package(case.pid, status='waiting_for_selection', phase='catalog_ready',
                                      message='内容目录已就绪，可选择深度摘要范围；主题名称正在后台按证据完善')
            worker = LargePackageWorker(store=case.store, poll_seconds=0.2)
            # 主题层还欠一次重建（叶组刚改过名）时不改
            with patch.object(naming, 'validate_catalog', return_value={'blocking': False}), \
                    patch('services.large_package_catalog_tree.stale', return_value=True):
                assert not worker._mark_catalog_refined(case.pid)
            with patch.object(naming, 'validate_catalog', return_value={'blocking': False}), \
                    patch.object(worker, '_topic_rebuild_owed', return_value=True):
                assert not worker._mark_catalog_refined(case.pid)
            with patch.object(naming, 'validate_catalog', return_value={'blocking': False}):
                assert worker._mark_catalog_refined(case.pid)
            assert case.store.get(case.pid)['message'] == LargePackageWorker.CATALOG_REFINED_MESSAGE
            # 幂等：已改过就不再改
            assert not worker._mark_catalog_refined(case.pid)
            # 还有阻断项时不改
            case.store.update_package(case.pid, message='内容目录已就绪，可选择深度摘要范围；主题名称正在后台按证据完善')
            with patch.object(naming, 'validate_catalog', return_value={'blocking': True}):
                assert not worker._mark_catalog_refined(case.pid)
            # 还有文件没收录进目录时不改
            with closing(case.store._connect()) as c:
                c.execute("UPDATE queue SET status='queued' WHERE package_id=? AND stage='catalog'", (case.pid,))
                c.commit()
            with patch.object(naming, 'validate_catalog', return_value={'blocking': False}):
                assert not worker._mark_catalog_refined(case.pid)
    finally:
        case.doCleanups()
