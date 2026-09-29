# -*- coding: utf-8 -*-
"""提速批次核查后的六项修复的回归测试（不依赖 GPU，不发模型请求）。

1. km_finalize / km_repair 触发源只在成员真的改动时记录；
2. 模型重新命名后，连续沿用次数清零；
3. 默认提示词变体下 bundle_key 与改动前逐字相同（重启不会让缓存全量失效）；
4. 语义合并 / 同名合并记 consolidate 触发源。
"""
import hashlib
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.large_package_isolated import LargePackageStore   # noqa: E402
from services import large_package_catalog_naming as naming     # noqa: E402
from services import large_package_semantic as semantic         # noqa: E402
from large_package_worker import LargePackageWorker             # noqa: E402

GROUP = 'km-1001-00000'
ENV_KEYS = ('SJFX_LP_NAMING_COMPACT_OUTPUT',)


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='fix6-')
        self.store = LargePackageStore(db_path=os.path.join(self.dir, 'lp.db'))
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.pid = 'lp-fix6'
        now = time.time()
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO packages(id,root_path,owner_id,status,phase,progress,counts,"
                      "created_at,updated_at,control_state,control_generation) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, '/tmp/fix6', 'o1', 'waiting_for_selection', 'catalog_ready',
                       70, '{}', now, now, 'active', 0))
            c.commit()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def events(self):
        with closing(self.store._connect()) as c:
            return [(r[0], r[1]) for r in c.execute(
                'SELECT trigger,groups_touched FROM lp_catalog_rebuild_events '
                'WHERE package_id=? ORDER BY at', (self.pid,))]


# ============================================================ 1. 触发源只记真改动
class KmEventTests(_Base):
    def setUp(self):
        super().setUp()
        self.worker = LargePackageWorker(self.store)
        from config import Config
        self._cfg = mock.patch.multiple(Config, create=True, LP_CATALOG_KMODE=True,
                                        LP_CATALOG_MODEL_NAMING=False,
                                        LP_CATALOG_FREEZE_AFTER_CATALOG=True)
        self._cfg.start()
        self.addCleanup(self._cfg.stop)
        # 全部闸门放行：quick / catalog 无待办，素材都有向量
        for name, value in (('material_pending_count', lambda *a, **k: 0),):
            p = mock.patch.object(semantic, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(self.store, 'stage_counts', lambda *a, **k: {})
        p.start()
        self.addCleanup(p.stop)

    def _run(self, state, signature, recluster=0):
        with mock.patch.object(semantic, '_km_state_get', lambda *a, **k: state), \
                mock.patch.object(semantic, '_km_signature', lambda *a, **k: signature), \
                mock.patch.object(semantic, 'kmeans_recluster', lambda *a, **k: recluster):
            return self.worker._ensure_km_finalized(self.pid)

    def test_unchanged_signature_records_nothing(self):
        """指纹未变 → 直接返回，这是最常见的情况，绝不能记事件。"""
        self._run(state='sig-a', signature='sig-a')
        self.assertEqual(self.events(), [])

    def test_recluster_without_change_records_nothing(self):
        with closing(self.store._connect()) as c:
            c.execute("UPDATE packages SET phase='catalog_build' WHERE id=?", (self.pid,))
            c.commit()
        self._run(state=None, signature='sig-b', recluster=0)
        self.assertEqual(self.events(), [])

    def test_real_recluster_records_km_finalize(self):
        with closing(self.store._connect()) as c:
            c.execute("UPDATE packages SET phase='catalog_build' WHERE id=?", (self.pid,))
            c.commit()
        self._run(state=None, signature='sig-b', recluster=7)
        self.assertEqual(self.events(), [('km_finalize', 7)])

    def test_frozen_repair_records_km_repair_only_when_changed(self):
        from services import large_package_catalog_quality as quality
        with mock.patch.object(quality, 'repair_changed',
                               lambda *a, **k: {'status': 'retry', 'dirty': 195}):
            self._run(state='sig-old', signature='sig-new')
        self.assertEqual(self.events(), [], 'retry（什么都没改）不得记事件')
        with mock.patch.object(quality, 'repair_changed',
                               lambda *a, **k: {'status': 'done', 'changed': 3}):
            self._run(state='sig-old', signature='sig-new')
        self.assertEqual(self.events(), [('km_repair', 3)])

    def test_recognize_slice_no_longer_records_unconditionally(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'large_package_worker.py'), encoding='utf-8') as fh:
            source = fh.read()
        self.assertNotIn("record_rebuild_event(self.store, package_id, 'km_finalize', 0)", source)


# ============================================================ 2. streak 清零
class StreakResetTests(_Base):
    def setUp(self):
        super().setUp()
        now = time.time()
        with closing(self.store._connect()) as c:
            c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                      "result,status,updated_at,member_count,content_revision) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, GROUP, '', b'', 'a.pdf', '{}', 'queued', now, 0, 0))
            for path in ('a.pdf', 'b.pdf', 'c.pdf'):
                c.execute("INSERT INTO large_catalog_members(package_id,group_id,path,input_hash,vector) "
                          "VALUES(?,?,?,?,?)", (self.pid, GROUP, path, 'h-' + path, b''))
            revision = c.execute('SELECT content_revision FROM large_catalog_groups '
                                 'WHERE package_id=? AND id=?', (self.pid, GROUP)).fetchone()[0]
            c.execute("INSERT OR REPLACE INTO lp_catalog_jobs(package_id,group_id,revision,status,"
                      "attempts,next_at,leased,error,control_generation) "
                      "VALUES(?,?,?,'running',0,0,?,'',0)", (self.pid, GROUP, revision, now))
            c.execute('INSERT INTO lp_catalog_label_inherit(package_id,group_id,streak,updated) '
                      'VALUES(?,?,?,?)', (self.pid, GROUP, 3, now))
            c.commit()
        self.material = {'revision': revision, 'owner_id': 'o1', 'digest': 'd1',
                         'cache_digest': 'd1', 'evidence': [{'id': 'E1', 'text': '证据一'}],
                         'user': '{}'}
        self.result = {'name': '中央部门预算绩效管理资料', 'description': '依据组内文件归纳的说明',
                       'evidence_ids': ['E1'], 'needs_review': 0, 'purity': 'same'}

    def test_model_rename_resets_streak_and_refreshes_fingerprint(self):
        self.assertTrue(naming.apply(self.store, self.pid, GROUP, self.material,
                                     self.result, 'model-x'))
        with closing(self.store._connect()) as c:
            streak = c.execute('SELECT streak FROM lp_catalog_label_inherit '
                               'WHERE package_id=? AND group_id=?', (self.pid, GROUP)).fetchone()
            fp = c.execute('SELECT member_count FROM lp_catalog_label_members '
                           'WHERE package_id=? AND group_id=?', (self.pid, GROUP)).fetchone()
        self.assertIsNone(streak, '重新命名之后连续沿用次数必须清零')
        self.assertEqual(fp[0], 3, '指纹必须换成当前成员')
        self.assertEqual(naming._inherit_streak(self.store, self.pid, GROUP), 0)


# ============================================================ 3. 缓存键兼容
class BundleKeyCompatTests(_Base):
    ARGS = ('lp-x', 'g1', 3, 'owner')

    def legacy(self):
        parts = [naming.EVIDENCE_POLICY_VERSION, naming.VERSION, naming._tokenizer_version(),
                 'owner', 'lp-x', 'g1', '3']
        return hashlib.sha256('\0'.join(parts).encode('utf-8')).hexdigest()

    def test_default_variant_keeps_the_pre_change_key(self):
        """默认（full）下键与改动前逐字相同：重启后既有缓存仍可命中。"""
        self.assertEqual(naming.bundle_key(*self.ARGS), self.legacy())

    def test_compact_variant_still_gets_its_own_key(self):
        os.environ['SJFX_LP_NAMING_COMPACT_OUTPUT'] = '1'
        self.assertNotEqual(naming.bundle_key(*self.ARGS), self.legacy())


# ============================================================ 4. consolidate 触发源
class ConsolidateEventTests(_Base):
    def test_identical_merge_records_consolidate(self):
        from services import large_package_catalog_merge as merge
        now = time.time()
        with closing(self.store._connect()) as c:
            for gid, paths in (('km-1001-00001', ['a.pdf', 'b.pdf']),
                               ('km-1001-00002', ['c.pdf'])):
                c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
                          "result,status,updated_at,member_count,content_revision) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (self.pid, gid, '', b'', paths[0], '{}', 'named', now, len(paths), 0))
                for path in paths:
                    c.execute("INSERT INTO large_catalog_members(package_id,group_id,path,input_hash,vector) "
                              "VALUES(?,?,?,?,?)", (self.pid, gid, path, 'h-' + path, b''))
                rev = c.execute('SELECT content_revision FROM large_catalog_groups '
                                'WHERE package_id=? AND id=?', (self.pid, gid)).fetchone()[0]
                c.execute("INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,"
                          "description,evidence,model,digest,needs_review,locked,updated) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (self.pid, gid, rev, 1, '中央部门预算绩效管理资料', '描述', '[]',
                           'model-x', 'd-' + gid, 0, 0, now))
            c.commit()
        with mock.patch('services.large_package_catalog_quality.compatible_groups',
                        lambda *a, **k: True):
            merged = merge.merge_identical_names(self.store, self.pid)
        self.assertEqual(merged, 1)
        self.assertEqual(self.events(), [('consolidate', 1)])


# ============================================================ 5. §5.1 方案 B 空闲借用
class BorrowIdleTests(unittest.TestCase):
    KEYS = ('SJFX_GENERATION_BG_BORROW_IDLE_SECONDS', 'SJFX_GENERATION_CONCURRENCY',
            'SJFX_LP_NAMING_PARALLEL')

    def setUp(self):
        import sqlite3
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        self.dir = tempfile.mkdtemp(prefix='borrow-')
        self.c = sqlite3.connect(os.path.join(self.dir, 'd.db'), isolation_level=None)
        self.c.execute('CREATE TABLE admission(id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, '
                       'priority INTEGER NOT NULL, created REAL NOT NULL, heartbeat REAL NOT NULL, '
                       'pid INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 0)')

    def tearDown(self):
        self.c.close()
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def ticket(self, tid, priority, active=1):
        self.c.execute('INSERT INTO admission VALUES(?,?,?,?,?,?,?)',
                       (tid, 'ep', priority, time.time(), time.time(), os.getpid(), active))

    def test_default_is_n_minus_one_and_touches_nothing(self):
        from services import model_dispatch as md
        self.assertEqual(md._background_limit(self.c, 'ep', 2), 1)
        tables = {r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('admission_frontend_seen', tables, '关闭时不得建表或多执行 SQL')

    def test_borrows_full_slots_when_frontend_idle(self):
        from services import model_dispatch as md
        os.environ['SJFX_GENERATION_BG_BORROW_IDLE_SECONDS'] = '60'
        self.ticket('bg1', 10)
        self.assertEqual(md._background_limit(self.c, 'ep', 2), 2)

    def test_no_borrow_while_frontend_waiting_or_running(self):
        from services import model_dispatch as md
        os.environ['SJFX_GENERATION_BG_BORROW_IDLE_SECONDS'] = '60'
        self.ticket('fg1', 100, active=0)
        self.assertEqual(md._background_limit(self.c, 'ep', 2), 1)

    def test_no_borrow_within_idle_window_after_frontend_left(self):
        from services import model_dispatch as md
        os.environ['SJFX_GENERATION_BG_BORROW_IDLE_SECONDS'] = '60'
        self.ticket('fg1', 100)
        md._background_limit(self.c, 'ep', 2)          # 后台轮询时看见前台，记下时刻
        self.c.execute("DELETE FROM admission WHERE id='fg1'")
        self.assertEqual(md._background_limit(self.c, 'ep', 2), 1, '空闲不足阈值时不借')
        self.c.execute("UPDATE admission_frontend_seen SET seen_at=?", (time.time() - 61,))
        self.assertEqual(md._background_limit(self.c, 'ep', 2), 2, '超过阈值后可以借')

    def test_single_slot_never_borrows(self):
        from services import model_dispatch as md
        os.environ['SJFX_GENERATION_BG_BORROW_IDLE_SECONDS'] = '60'
        self.assertEqual(md._background_limit(self.c, 'ep', 1), 1)

    def test_second_background_admitted_only_with_borrow(self):
        """端到端：N=2，一个后台请求在跑时，第二个后台请求只有打开借用才进得去。"""
        from pathlib import Path
        from config import Config
        from services import model_dispatch as md
        from services.ollama import LocalModelError
        os.environ['SJFX_GENERATION_CONCURRENCY'] = '2'
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(Config, 'DATA_DIR', Path(folder)):
            for borrow, expect in (('0', False), ('60', True)):
                os.environ['SJFX_GENERATION_BG_BORROW_IDLE_SECONDS'] = borrow
                admitted = False
                with md.background_generation():
                    with md.generation_slot('fixture://borrow', timeout=2):
                        try:
                            with md.generation_slot('fixture://borrow', timeout=1):
                                admitted = True
                        except LocalModelError:
                            admitted = False
                self.assertEqual(admitted, expect, 'borrow=%s' % borrow)

    def test_worker_lane_cap_follows_borrow(self):
        os.environ['SJFX_GENERATION_CONCURRENCY'] = '2'
        os.environ['SJFX_LP_NAMING_PARALLEL'] = '1'
        d = tempfile.mkdtemp(prefix='borrow-w-')
        worker = LargePackageWorker(LargePackageStore(db_path=os.path.join(d, 'lp.db')))
        self.assertEqual(worker._naming_lanes(), 1, '默认只有 N-1=1 路')
        os.environ['SJFX_GENERATION_BG_BORROW_IDLE_SECONDS'] = '60'
        self.assertEqual(worker._naming_lanes(), 2, '借用打开后车道上限按 N 算')


# ============================================================ 6. 台账带 route
class RouteDetailTests(unittest.TestCase):
    def test_both_paths_record_route(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ('services/large_package_catalog_naming.py',
                    'services/large_package_catalog_batch.py'):
            with open(os.path.join(root, rel), encoding='utf-8') as fh:
                self.assertRegex(fh.read(), r"'route':\s*(_route|batch_route)", rel)


if __name__ == '__main__':
    unittest.main()
