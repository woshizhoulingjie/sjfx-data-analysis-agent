# -*- coding: utf-8 -*-
"""大包 QA「按需 / 懒加载」的回归测试（纯逻辑，不读生产数据）。

覆盖：
  · 需求判定：最近对话过 / 提问点名 enqueue / 人工钉住 / 都不满足 → 空；
  · `updated_at` 兼容 epoch 与 ISO 文本；
  · 节流：包指纹没变 → 只有超过兜底时间才同步；指纹变了 → 按 interval 限频；
  · 限速开关的边界（budget 至少 1、pause 不为负）；
  · `LARGE_QA_DEMAND_ONLY=0` 能回退旧行为。
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                            # noqa: E402
from services import large_qa_demand as demand                           # noqa: E402


def _ordinary_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY, scan_id TEXT, owner_id TEXT, "
                 "title TEXT, status TEXT, payload TEXT, created_at REAL, updated_at TEXT)")
    conn.executemany("INSERT INTO conversations(id,scan_id,status,updated_at) VALUES(?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _qa_index(base, package_id, rows):
    directory = os.path.join(base, package_id)
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(os.path.join(directory, "index.db"))
    conn.execute("CREATE TABLE documents(path TEXT, parse_status TEXT, priority INTEGER DEFAULT 0)")
    conn.executemany("INSERT INTO documents(path,parse_status,priority) VALUES(?,?,?)", rows)
    conn.commit()
    conn.close()


class DemandTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='lqademand-')
        self.base = os.path.join(self.dir, 'large_qa')
        os.makedirs(self.base, exist_ok=True)
        self.agent_db = os.path.join(self.dir, 'agent.db')
        now = time.time()
        _ordinary_db(self.agent_db, [
            ('c1', 'lp-recent', 'active', str(now - 60)),
            ('c2', 'lp-old', 'active', str(now - 90 * 86400)),
            ('c3', 'scan-xxxx', 'active', str(now - 10)),
        ])
        _qa_index(self.base, 'lp-enqueued', [('a.pdf', 'pending', 2)])
        _qa_index(self.base, 'lp-quiet', [('b.pdf', 'pending', 0), ('c.pdf', 'complete', 3)])
        self.known = ['lp-recent', 'lp-old', 'lp-enqueued', 'lp-quiet', 'lp-nothing']

    def tearDown(self):
        for root, _dirs, files in os.walk(self.dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)

    def test_recent_conversation_is_demanded(self):
        self.assertIn('lp-recent', demand.recent_packages(self.agent_db))

    def test_old_conversation_expires(self):
        self.assertNotIn('lp-old', demand.recent_packages(self.agent_db))

    def test_ordinary_scans_are_ignored(self):
        self.assertNotIn('scan-xxxx', demand.recent_packages(self.agent_db))

    def test_enqueued_package_is_demanded(self):
        self.assertEqual(demand.enqueued_packages(self.base, self.known), {'lp-enqueued'})

    def test_quiet_package_is_not_enqueued(self):
        self.assertNotIn('lp-quiet', demand.enqueued_packages(self.base, self.known))

    def test_pinned_package_is_demanded(self):
        with open(os.path.join(self.base, 'demand.txt'), 'w', encoding='utf-8') as fh:
            fh.write('# 手工补索引\nlp-quiet\n')
        self.assertEqual(demand.pinned_packages(self.base), {'lp-quiet'})

    def test_demanded_combines_signals_with_reasons(self):
        with open(os.path.join(self.base, 'demand.txt'), 'w', encoding='utf-8') as fh:
            fh.write('lp-quiet\n')
        got = demand.demanded(self.agent_db, self.base, self.known)
        self.assertEqual(set(got), {'lp-quiet', 'lp-recent', 'lp-enqueued'})
        self.assertEqual(got['lp-quiet'], 'pinned')
        self.assertEqual(got['lp-recent'], 'recent_conversation')
        self.assertEqual(got['lp-enqueued'], 'question_enqueued')

    def test_unknown_packages_are_filtered_out(self):
        with open(os.path.join(self.base, 'demand.txt'), 'w', encoding='utf-8') as fh:
            fh.write('lp-does-not-exist\n')
        got = demand.demanded(self.agent_db, self.base, self.known)
        self.assertNotIn('lp-does-not-exist', got, '钉住的包必须真实存在才生效')
        self.assertIn('lp-recent', got)

    def test_nothing_demanded_is_empty(self):
        empty_db = os.path.join(self.dir, 'empty.db')
        _ordinary_db(empty_db, [])
        self.assertEqual(demand.demanded(empty_db, self.base, ['lp-quiet']), {})

    def test_age_accepts_epoch_and_iso(self):
        now = 1_800_000_000.0
        self.assertAlmostEqual(demand.age_seconds(now - 30, now), 30, places=3)
        self.assertIsNotNone(demand.age_seconds('2027-01-15 08:00:00', now))
        self.assertIsNone(demand.age_seconds('不是时间', now), )
        self.assertIsNone(demand.age_seconds(None, now))


class ThrottleTests(unittest.TestCase):
    def test_first_sync_always_runs(self):
        self.assertTrue(demand.should_sync(1000.0, None, None, 'stamp'))

    def test_unchanged_stamp_waits_for_force_after(self):
        self.assertFalse(demand.should_sync(200.0, 100.0, 's', 's', interval=120, force_after=1800))
        self.assertTrue(demand.should_sync(2000.0, 100.0, 's', 's', interval=120, force_after=1800))

    def test_changed_stamp_respects_interval(self):
        self.assertFalse(demand.should_sync(150.0, 100.0, 's1', 's2', interval=120, force_after=1800))
        self.assertTrue(demand.should_sync(300.0, 100.0, 's1', 's2', interval=120, force_after=1800))

    def test_package_stamp_changes_with_package(self):
        a = {'updated_at': 1, 'progress': 70, 'counts': '{}', 'phase': 'catalog_ready'}
        b = dict(a, updated_at=2)
        self.assertNotEqual(demand.package_stamp(a), demand.package_stamp(b))
        self.assertEqual(demand.package_stamp(a), demand.package_stamp(dict(a)))
        self.assertEqual(demand.package_stamp(None), '')

    def test_limits_have_sane_bounds(self):
        old = {k: getattr(config.Config, k, None) for k in
               ('LARGE_QA_BUDGET_FILES_PER_ROUND', 'LARGE_QA_EXTRACT_PAUSE')}
        try:
            config.Config.LARGE_QA_BUDGET_FILES_PER_ROUND = 0
            config.Config.LARGE_QA_EXTRACT_PAUSE = -5
            self.assertEqual(demand.extract_budget(), 1)
            self.assertEqual(demand.extract_pause(), 0.0)
        finally:
            for k, v in old.items():
                if v is None:
                    continue
                setattr(config.Config, k, v)

    def test_demand_only_defaults_on_and_can_be_disabled(self):
        old = getattr(config.Config, 'LARGE_QA_DEMAND_ONLY', None)
        try:
            config.Config.LARGE_QA_DEMAND_ONLY = True
            self.assertTrue(demand.demand_only())
            config.Config.LARGE_QA_DEMAND_ONLY = '0'
            self.assertFalse(demand.demand_only())
            config.Config.LARGE_QA_DEMAND_ONLY = 'false'
            self.assertFalse(demand.demand_only())
            config.Config.LARGE_QA_DEMAND_ONLY = '1'
            self.assertTrue(demand.demand_only())
        finally:
            if old is not None:
                config.Config.LARGE_QA_DEMAND_ONLY = old


if __name__ == '__main__':
    unittest.main()
