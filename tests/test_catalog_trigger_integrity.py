# -*- coding: utf-8 -*-
"""Regression tests for the lp_catalog_changes trigger UNIQUE-conflict defect.

The defect (production, 2026-09-23, package lp-085e413e2…):
  `lp_catalog_changed_insert` / `_vector` / `_delete` wrote
  `INSERT OR IGNORE INTO lp_catalog_changes VALUES(...)`. Inside a trigger fired from
  an `INSERT ... ON CONFLICT(...) DO UPDATE` statement the inner `OR IGNORE` does NOT
  suppress its own UNIQUE conflict, so the whole statement aborts with
  `UNIQUE constraint failed: lp_catalog_changes.package_id, lp_catalog_changes.path`.
  That killed `embed_batch` (vector staging) and `kmeans_recluster` (group assignment),
  so `large_catalog_km_state` was never written and all 7484 files stayed in one
  `km-pending-*` placeholder group -- the catalog showed no themes at all.

Layers:
  * L0  raw-SQL reproduction of the SQLite semantics that caused it;
  * L1  the real triggers created by `quality.init()`, driven through the exact
        `_MEMBER_UPSERT` statement that crashed in production;
  * L2  end-to-end: a real k-means recluster over members whose `lp_catalog_changes`
        rows are all already present (the production precondition).
"""
import sqlite3
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
from services import large_package_catalog_quality as quality
from services import large_package_semantic as semantic
from services import large_package_catalog_naming as naming


class TriggerConflictSemanticsTests(unittest.TestCase):
    """L0: pin down the SQLite behaviour the fix depends on."""

    @staticmethod
    def _db():
        c = sqlite3.connect(':memory:', isolation_level=None)
        c.execute('CREATE TABLE mem(package_id TEXT,path TEXT,group_id TEXT,'
                  'input_hash TEXT,vector BLOB,PRIMARY KEY(package_id,path))')
        c.execute('CREATE TABLE chg(package_id TEXT,path TEXT,PRIMARY KEY(package_id,path))')
        return c

    def test_ignore_inside_trigger_fails_under_upsert_but_not_under_insert(self):
        """The exact asymmetry that produced the production outage."""
        upsert = ('INSERT INTO mem(package_id,path,group_id,input_hash,vector) '
                  'VALUES(?,?,?,?,?) ON CONFLICT(package_id,path) DO UPDATE SET '
                  'group_id=excluded.group_id WHERE mem.group_id<>excluded.group_id')
        for label, sql in (('upsert', upsert),
                           ('insert_or_ignore', 'INSERT OR IGNORE INTO mem VALUES(?,?,?,?,?)')):
            c = self._db()
            c.execute('CREATE TRIGGER t AFTER UPDATE ON mem BEGIN '
                      'INSERT OR IGNORE INTO chg VALUES(NEW.package_id,NEW.path); END')
            c.execute("INSERT INTO mem VALUES('p','f','g1','h',x'00')")
            c.execute("INSERT INTO chg VALUES('p','f')")
            if label == 'upsert':
                with self.assertRaises(Exception) as ctx:
                    c.execute(sql, ('p', 'f', 'g2', 'h', b'\x00'))
                self.assertIn('UNIQUE constraint failed', str(ctx.exception))
                self.assertIn('chg', str(ctx.exception))
            else:
                c.execute(sql, ('p', 'f', 'g2', 'h', b'\x00'))
            c.close()

    def test_where_not_exists_form_survives_upsert(self):
        c = self._db()
        c.execute('CREATE TRIGGER t AFTER UPDATE ON mem BEGIN '
                  'INSERT INTO chg(package_id,path) SELECT NEW.package_id,NEW.path '
                  'WHERE NOT EXISTS(SELECT 1 FROM chg '
                  '                 WHERE package_id=NEW.package_id AND path=NEW.path); END')
        c.execute("INSERT INTO mem VALUES('p','f','g1','h',x'00')")
        c.execute("INSERT INTO chg VALUES('p','f')")
        c.execute('INSERT INTO mem(package_id,path,group_id,input_hash,vector) '
                  'VALUES(?,?,?,?,?) ON CONFLICT(package_id,path) DO UPDATE SET '
                  'group_id=excluded.group_id WHERE mem.group_id<>excluded.group_id',
                  ('p', 'f', 'g2', 'h', b'\x00'))
        self.assertEqual(c.execute('SELECT COUNT(*) FROM chg').fetchone()[0], 1)
        c.close()


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for p in (patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()),
                  patch.object(Config, 'LP_CATALOG_KMODE', True)):
            p.start(); self.addCleanup(p.stop)
        self.store = LargePackageStore(self.root / 'state.db')
        self.pid = self.store.create(self.root)['id']

    def _vec(self, values=(1.0, 0.0)):
        v = np.array(values, dtype=np.float32)
        v = v / max(float(np.linalg.norm(v)), 1e-8)
        return v.astype(np.float32).tobytes()


class CatalogTriggerIntegrityTests(_StoreCase):
    """L1: the real triggers, driven through the real crashing statement."""

    def _add_member(self, path, gid, vec=(1.0, 0.0)):
        vector = self._vec(vec)
        with closing(self.store._connect()) as c:
            c.execute('INSERT OR IGNORE INTO large_catalog_groups(package_id,id,model,anchor,'
                      'representative,updated_at) VALUES(?,?,?,?,?,?)',
                      (self.pid, gid, 'test', vector, path, time.time()))
            c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                      (self.pid, path, gid, 'hash-' + path, vector))
            c.commit()
        return vector

    def _changes(self):
        with closing(self.store._connect()) as c:
            return [r[0] for r in c.execute(
                'SELECT path FROM lp_catalog_changes WHERE package_id=?', (self.pid,))]

    def test_moving_a_member_to_another_group_does_not_raise(self):
        """The exact production failure: a member whose path is already recorded in
        lp_catalog_changes is re-upserted under a different group."""
        vector = self._add_member('a.txt', 'km-pending-00000000')
        self.assertEqual(self._changes(), ['a.txt'],
                         '成员插入后触发器应当记录一条变更')
        # 生产里抛出的就是下面这一句：
        # UNIQUE constraint failed: lp_catalog_changes.package_id, lp_catalog_changes.path
        with closing(self.store._connect()) as c:
            c.execute(semantic._MEMBER_UPSERT,
                      (self.pid, 'a.txt', 'km-clustered-00000', 'hash-a.txt', vector))
            c.commit()
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT group_id FROM large_catalog_members '
                                       'WHERE package_id=? AND path=?',
                                       (self.pid, 'a.txt')).fetchone()[0],
                             'km-clustered-00000')
        self.assertEqual(self._changes(), ['a.txt'], '变更表不得出现重复行')

    def test_repeated_replays_keep_the_change_table_unique(self):
        self._add_member('a.txt', 'km-pending-00000000')
        vector = self._vec()
        for gid in ('g1', 'g2', 'g3'):
            with closing(self.store._connect()) as c:
                c.execute(semantic._MEMBER_UPSERT,
                          (self.pid, 'a.txt', gid, 'hash-a.txt', vector))
                c.commit()
        self.assertEqual(self._changes(), ['a.txt'])

    def test_delete_and_reinsert_still_records_exactly_one_change(self):
        self._add_member('a.txt', 'g1')
        with closing(self.store._connect()) as c:
            c.execute('DELETE FROM large_catalog_members WHERE package_id=? AND path=?',
                      (self.pid, 'a.txt'))
            c.commit()
        self._add_member('a.txt', 'g2')
        self.assertEqual(self._changes(), ['a.txt'])
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM lp_catalog_vector_state '
                                       'WHERE package_id=?', (self.pid,)).fetchone()[0], 1)

    def test_init_replaces_a_stale_broken_trigger_definition(self):
        """`CREATE TRIGGER IF NOT EXISTS` used to keep the broken definition forever,
        so shipping the corrected source alone would NOT have fixed production."""
        broken = ('CREATE TRIGGER lp_catalog_changed_insert AFTER INSERT ON large_catalog_members '
                  'BEGIN INSERT OR IGNORE INTO lp_catalog_changes VALUES(NEW.package_id,NEW.path); '
                  'INSERT INTO lp_catalog_vector_state VALUES(NEW.package_id,1) '
                  'ON CONFLICT(package_id) DO UPDATE SET epoch=epoch+1; END')
        with closing(self.store._connect()) as c:
            c.execute('DROP TRIGGER IF EXISTS lp_catalog_changed_insert')
            c.execute(broken)
            c.commit()
        # 重新打开一个连接跑 init()，模拟进程重启
        with closing(self.store._connect()) as c:
            quality.init(c)
            sql = c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                            "AND name='lp_catalog_changed_insert'").fetchone()[0]
        self.assertNotIn('INSERT OR IGNORE', sql)
        self.assertIn('NOT EXISTS', sql)
        # 旧定义真的被换掉了吗——用会崩的那条语句验证
        self._add_member('b.txt', 'km-pending-00000000')
        vector = self._vec()
        with closing(self.store._connect()) as c:
            c.execute(semantic._MEMBER_UPSERT,
                      (self.pid, 'b.txt', 'km-clustered-00000', 'hash-b.txt', vector))
            c.commit()
        self.assertIn('b.txt', self._changes())


class KmeansUnderPopulatedChangesTests(_StoreCase):
    """L2: end-to-end reproduction of the production crash."""

    def _seed(self, n=40):
        """n files, all embedded into one placeholder group -- the production shape,
        so lp_catalog_changes is fully populated before clustering runs."""
        rng = np.random.default_rng(11)
        with closing(self.store._connect()) as c:
            c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                      'representative,updated_at) VALUES(?,?,?,?,?,?)',
                      (self.pid, 'km-pending-00000000', 'test', b'', 'f0.txt', time.time()))
            for i in range(n):
                path = 'f%03d.txt' % i
                self.store.upsert_file(self.pid, dict(path=path, name=path, size=100,
                                                      modified_at_ns=1))
                self.store.update_file(
                    self.pid, path, quick_status='completed',
                    local_summary={'sample': '设备运行监测技术记录第%d号。' % i * 4,
                                   'portrait': {'headings': ['设备运行监测']}})
                vec = rng.standard_normal(16).astype(np.float32)
                vec /= max(float(np.linalg.norm(vec)), 1e-8)
                c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                          (self.pid, path, 'km-pending-00000000', 'h-%s' % path,
                           vec.tobytes()))
            c.commit()
        return n

    def test_populated_changes_table_does_not_break_recluster(self):
        n = self._seed()
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM lp_catalog_changes '
                                       'WHERE package_id=?', (self.pid,)).fetchone()[0], n,
                             '生产前置条件：变更表在聚类前已被全部填充')
        changed = semantic.kmeans_recluster(self.store, self.pid, force=True)
        self.assertTrue(changed, '聚类必须真正写进去，而不是返回 0')
        with closing(self.store._connect()) as c:
            groups = sorted({r[0] for r in c.execute(
                'SELECT DISTINCT group_id FROM large_catalog_members WHERE package_id=?',
                (self.pid,))})
            km_state = c.execute('SELECT COUNT(*) FROM large_catalog_km_state WHERE package_id=?',
                                 (self.pid,)).fetchone()[0]
            left = c.execute('SELECT COUNT(*) FROM lp_catalog_changes WHERE package_id=?',
                             (self.pid,)).fetchone()[0]
        self.assertNotEqual(groups, ['km-pending-00000000'], '必须换掉占位分组')
        self.assertGreater(len(groups), 1, '必须拆成多个叶组，否则目录没有主题')
        self.assertEqual(km_state, 1, 'km_state 必须落库，否则冻结逻辑会永久卡死该包')
        self.assertEqual(left, 0, '聚类收尾应清空变更表（本轮变更已被吸收）')


class CatalogHealAndRebuildGateTests(_StoreCase):
    """The two follow-on defects that made the outage unrecoverable."""

    def _set_phase(self, phase):
        self.store.update_package(self.pid, status='waiting_for_selection', phase=phase)

    def _mark_clustered(self):
        with closing(self.store._connect()) as c:
            c.execute('INSERT INTO large_catalog_km_state(package_id,signature,vectors,updated_at) '
                      'VALUES(?,?,?,?)',
                      (self.pid, semantic._km_signature(self.store, self.pid), 0, 0.0))
            c.commit()

    def test_phase_freeze_does_not_apply_to_a_package_that_never_clustered(self):
        """生产死锁：聚类失败 → 相位推进到 catalog_ready → 冻结 → 永不重试。"""
        self._set_phase('catalog_ready')
        self.assertFalse(semantic._km_state_get(self.store, self.pid),
                         '前置条件：这个包从未聚类成功过')
        from large_package_worker import LargePackageWorker
        worker = LargePackageWorker(self.store)
        seen = {}
        real = semantic.kmeans_recluster

        def spy(store, package_id, force=False):
            seen['called'] = True
            return real(store, package_id, force=force)

        with patch.object(semantic, 'kmeans_recluster', side_effect=spy), \
                patch.object(semantic, 'material_pending_count', return_value=0):
            worker._ensure_km_finalized(self.pid)
        self.assertTrue(seen.get('called'),
                        '从未聚类成功的包即使相位已到 catalog_ready 也必须允许补聚类')

    def test_frozen_package_with_unchanged_signature_is_left_alone(self):
        """冻结规则本身不能被削弱：已聚类的包在指纹未变时必须原地不动。"""
        self._set_phase('catalog_ready')
        self._mark_clustered()
        from large_package_worker import LargePackageWorker
        worker = LargePackageWorker(self.store)
        with patch.object(semantic, 'kmeans_recluster',
                          side_effect=AssertionError('冻结的包不得被静默重聚类')):
            worker._ensure_km_finalized(self.pid)

    def _dead_file(self, path):
        """复刻 _mark_unusable 留下的现场：正文不可用 + catalog 队列项仍是 queued。

        这样的行永远不会拿到向量，也不进 material_pending_count；
        但队列收尾一旦漏掉，它们会永远挂在 queued 上（生产实测 47 条）。
        """
        self.store.upsert_file(self.pid, dict(path=path, name=path, size=10,
                                              modified_at_ns=1))
        self.store.update_file(self.pid, path, quick_status='completed')
        with closing(self.store._connect()) as c:
            c.execute("UPDATE files SET local_summary=? WHERE package_id=? AND path=?",
                      (json.dumps({'portrait': {'catalog_quality': 'too_little_text'}}),
                       self.pid, path))
            # 快速解析早已跑完（生产状态就是这样），只有 catalog 那条残留
            c.execute("UPDATE queue SET status='completed' WHERE package_id=? AND path=? "
                      "AND stage='quick'", (self.pid, path))
            c.execute("INSERT OR REPLACE INTO queue(package_id,path,stage,status,"
                      "updated_at,priority) VALUES(?,?,'catalog','queued',?,0)",
                      (self.pid, path, time.time()))
            c.commit()

    def test_rebuild_gate_ignores_files_that_can_never_get_a_vector(self):
        """47 条 _mark_unusable 的残留队列项曾经把 POST /catalog/rebuild 整个挡死。"""
        self._set_phase('catalog_ready')
        self._mark_clustered()
        for path in ('dead-a.pdf', 'dead-b.pdf'):
            self._dead_file(path)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM queue WHERE package_id=? AND stage='catalog' "
                "AND status='queued'", (self.pid,)).fetchone()[0], 2)
        with patch.object(semantic, 'material_pending_count', return_value=0), \
                patch.object(semantic, 'kmeans_recluster', return_value=1), \
                patch.object(naming, 'queue_groups'), \
                patch.object(naming, 'ensure_round'):
            result = naming.rebuild_catalog(self.store, self.pid)
        self.assertIn('groups_before', result)

    def test_rebuild_gate_still_blocks_files_that_really_wait_for_a_vector(self):
        """真正的待嵌入文件必须继续拦住重建，闸门不能整体失效。"""
        self._set_phase('catalog_ready')
        self._mark_clustered()
        self.store.upsert_file(self.pid, dict(path='live.pdf', name='live.pdf',
                                              size=10, modified_at_ns=1))
        # 有可用正文 -> update_file 会把它排进 catalog 队列（真实路径）
        self.store.update_file(self.pid, 'live.pdf', quick_status='completed',
                               local_summary={'sample': '设备运行监测技术记录。' * 8})
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM queue WHERE package_id=? AND stage='catalog' "
                "AND status='queued'", (self.pid,)).fetchone()[0], 1,
                '前置条件：真的有文件在等向量')
        with patch.object(semantic, 'material_pending_count', return_value=0):
            with self.assertRaises(ValueError) as ctx:
                naming.rebuild_catalog(self.store, self.pid)
        self.assertIn('待办', str(ctx.exception))


class EmptyPlaceholderGroupTests(_StoreCase):
    """零成员的 km-pending 占位组不得被反复排进命名队列。

    生产实测（lp-3a095dc8…，2026-09-23 02:08 起）：这样的组每轮都以
    error='没有内容证据' 失败、每轮又被重置回 queued，worker 在它上面
    每 2 秒空转一次、约 300 行日志/分钟，持续了一个多小时。
    """

    def test_zero_member_pending_group_is_not_queued_for_naming(self):
        with closing(self.store._connect()) as c:
            c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                      'representative,updated_at,member_count) VALUES(?,?,?,?,?,?,0)',
                      (self.pid, 'km-pending-00000000', 'test', b'', '', time.time()))
            c.commit()
        naming.queue_groups(self.store, self.pid)
        with closing(self.store._connect()) as c:
            n = c.execute('SELECT COUNT(*) FROM lp_catalog_jobs WHERE package_id=?',
                          (self.pid,)).fetchone()[0]
        self.assertEqual(n, 0, '零成员的 km-pending 占位组不应产生命名任务')

    def test_stale_job_for_zero_member_pending_group_is_reclaimed(self):
        """历史库里已经存在的这种任务行也要被回收，否则仍会被 claim。"""
        with closing(self.store._connect()) as c:
            c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                      'representative,updated_at,member_count) VALUES(?,?,?,?,?,?,0)',
                      (self.pid, 'km-pending-00000000', 'test', b'', '', time.time()))
            c.execute("INSERT INTO lp_catalog_jobs(package_id,group_id,revision,status,attempts) "
                      "VALUES(?,?,1,'deferred',5)", (self.pid, 'km-pending-00000000'))
            c.commit()
        naming.queue_groups(self.store, self.pid)
        with closing(self.store._connect()) as c:
            n = c.execute('SELECT COUNT(*) FROM lp_catalog_jobs WHERE package_id=?',
                          (self.pid,)).fetchone()[0]
        self.assertEqual(n, 0, '残留的空占位任务应被回收')

    def test_pending_group_with_members_is_still_queued(self):
        """有成员的占位组行为不变（不能被这条修复顺手改掉）。"""
        vector = self._vec()
        with closing(self.store._connect()) as c:
            c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                      'representative,updated_at,member_count) VALUES(?,?,?,?,?,?,1)',
                      (self.pid, 'km-pending-11111111', 'test', vector, 'x.txt', time.time()))
            c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                      (self.pid, 'x.txt', 'km-pending-11111111', 'h', vector))
            c.commit()
        naming.queue_groups(self.store, self.pid)
        with closing(self.store._connect()) as c:
            n = c.execute('SELECT COUNT(*) FROM lp_catalog_jobs WHERE package_id=?',
                          (self.pid,)).fetchone()[0]
        self.assertEqual(n, 1, '有成员的占位组仍应正常排队命名')


if __name__ == '__main__':
    unittest.main()
