"""生产模式（k-means）的目录契约测试。

**为什么单独一个文件**：仓库里原有的目录契约测试
（`test_large_semantic_catalog.py` / `test_catalog_batch.py` /
`test_catalog_naming_budget.py`）断言的产物属于**旧算法**
（`catalog_model='extractive-semantic-label/1'`、`catalog_group`、
`catalog_states['classified']`），而生产跑的是 `SJFX_LP_CATALOG_KMODE=1`。
结果是：本地默认口径（`config.py` 里该开关默认 `"0"`）全绿，生产口径下一片红 ——
**生产真正在用的目录算法长期没有契约测试保护**。

这里补上"模式无关"的契约：不依赖任何一种算法的内部字段，只断言
**目录作为产品必须满足的性质**。于是两种模式下都成立、都会有意义。

配套的 `_MODE_ISOLATED_LEGACY` 说明：旧算法断言在 k-means 模式下**不适用**，
它们应当被参数化（两种模式各跑一遍），这件事记在遗留清单里，不在这里用跳过掩盖。
"""
import os
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.large_package_isolated import LargePackageStore
from services import large_package_semantic as semantic
from services import large_package_catalog_naming as naming
from large_package_worker import LargePackageWorker

_MODE_ISOLATED_LEGACY = (
    "旧算法断言：在 SJFX_LP_CATALOG_KMODE=1（生产口径）下不适用。"
    "见 tests/test_catalog_mode_contract.py 的模块说明。")


class Model:
    model = 'fixture-model'
    supports_json_response_format = True

    def __init__(self):
        self.calls = 0

    def chat(self, system, user, **kwargs):
        self.calls += 1
        import json as _json
        payload = _json.loads(user)
        return {'content': _json.dumps({
            'name': '远程证明中的设备证据验证',
            'description': '讨论设备如何提供可信证据以及验证方如何完成校验。',
            'evidence_ids': [payload['evidence'][0]['id']],
            'needs_review': False}, ensure_ascii=False),
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50},
            'finish_reason': 'stop'}


class CatalogModeContractTests(unittest.TestCase):
    """与算法无关的目录性质：任何模式下都必须成立。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()),
                        patch.object(Config, 'LP_CATALOG_MODEL_NAMING', True)]
        for p in self.patches:
            p.start()
        self.store = LargePackageStore(self.root / 'large.db')
        self.pid = self.store.create(self.root)['id']

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_advance_is_the_single_verdict_and_is_read_only(self):
        """`advance()` 必须是纯读取的唯一判定口。"""
        with closing(self.store._connect()) as c:
            before = dict(c.execute(
                "SELECT phase,status,control_state FROM packages WHERE id=?",
                (self.pid,)).fetchone())
            rounds_before = c.execute("SELECT COUNT(*) FROM lp_catalog_rounds").fetchone()[0]
        for _ in range(3):
            v = self.store.advance(self.pid)
        self.assertTrue(v.get('ok'))
        self.assertIn('can_finish', v)
        self.assertIn('blocked_by', v)
        with closing(self.store._connect()) as c:
            after = dict(c.execute(
                "SELECT phase,status,control_state FROM packages WHERE id=?",
                (self.pid,)).fetchone())
            rounds_after = c.execute("SELECT COUNT(*) FROM lp_catalog_rounds").fetchone()[0]
        self.assertEqual(before, after, "advance() 不得修改包状态")
        self.assertEqual(rounds_before, rounds_after, "advance() 不得创建/改动轮次")

    def test_phase_definitions_are_the_only_source_of_schedulable_phases(self):
        """阶段白名单必须从 PHASES 派生（加阶段只改一处）。"""
        derived = tuple(k for k, v in LargePackageStore.PHASES.items() if v.get('schedulable'))
        self.assertEqual(derived, tuple(LargePackageStore.SCHEDULABLE_PHASES),
                         "SCHEDULABLE_PHASES 必须等于 PHASES 里 schedulable 的键")
        for key, spec in LargePackageStore.PHASES.items():
            self.assertIn('next', spec, "阶段 %s 缺少 next" % key)
            self.assertIn('gates', spec, "阶段 %s 缺少 gates" % key)
        # gates 里的每一个都必须是真实的 queue stage 名或保留字
        allowed = {'inventory', 'quick', 'recognize', 'catalog', 'deep', 'naming'}
        for key, spec in LargePackageStore.PHASES.items():
            for gate in spec.get('gates') or ():
                self.assertIn(gate, allowed, "阶段 %s 的 gate %s 不是已知事实" % (key, gate))

    def test_degraded_catalog_is_reported_not_silent(self):
        """降级目录必须能被读到（不允许静默的碎片目录）。"""
        info = semantic.catalog_degraded(self.store, self.pid)
        self.assertIn('degraded', info)
        self.assertTrue(info.get('reason'), "必须给出判据理由，便于页面显示")
        snap = self.store.workflow_snapshot(self.pid) or {}
        self.assertIn('catalog_degraded', snap)
        self.assertIn('catalog_degraded_detail', snap)

    def test_topic_read_follows_catalog_not_stale_file_field(self):
        """按 topic 过滤必须走目录成员关系，而不是可能过期的 files.topic。"""
        self.store.upsert_file(self.pid, {'path': 'a.txt', 'name': 'a.txt', 'size': 10})
        self.store.update_file(self.pid, 'a.txt', quick_status='completed',
                               local_summary={'sample': 'remote attestation evidence. ' * 5,
                                              'portrait': {'schema': 'x', 'fields': [], 'headings': []}})
        with closing(self.store._connect()) as c:
            c.execute("UPDATE files SET topic='过期的主题名' WHERE package_id=? AND path='a.txt'",
                      (self.pid,))
            c.execute("INSERT OR REPLACE INTO large_catalog_groups"
                      "(package_id,id,model,anchor,representative,result,status,updated_at,"
                      " attempts,content_revision,member_count) VALUES(?,?,'km','','','','named',?,0,1,1)",
                      (self.pid, 'g-1', 0.0))
            c.execute("INSERT OR REPLACE INTO large_catalog_members"
                      "(package_id,path,group_id,input_hash,vector) VALUES(?,?,?,?,zeroblob(0))",
                      (self.pid, 'a.txt', 'g-1', 'h'))
            c.execute("INSERT OR REPLACE INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self.pid, 'g-1', 1, 1, '目录里的真名字', 'd', '[]', 'fixture-model',
                       'dg', 0, 0, 0.0))
            c.commit()
        # 目录名能查到
        self.assertEqual(self.store.file_page(self.pid, topic='目录里的真名字')['total'], 1,
                         "按目录里的名字过滤必须命中")
        # 过期的 files.topic 不再命中（该文件已经进了目录）
        self.assertEqual(self.store.file_page(self.pid, topic='过期的主题名')['total'], 0,
                         "文件已进目录时，不得再用过期的 files.topic 命中它")


    # ------------------------------------------------------------------
    # P-11：框架 §12.1 的两条额外验收（此前只有生产实测，没有单测锁住）
    # ------------------------------------------------------------------
    def _seed_groups(self, specs, label_name="同名叶子"):
        """铺 groups + members + labels。specs = [(gid, [path, ...]), ...]"""
        with closing(self.store._connect()) as c:
            for gid, paths in specs:
                c.execute(
                    "INSERT OR REPLACE INTO large_catalog_groups"
                    "(package_id,id,model,anchor,representative,result,status,updated_at,"
                    " attempts,content_revision,member_count) "
                    "VALUES(?,?,'km','','','','named',?,0,1,?)",
                    (self.pid, gid, 0.0, len(paths)))
                for p in paths:
                    c.execute(
                        "INSERT OR REPLACE INTO large_catalog_members"
                        "(package_id,path,group_id,input_hash,vector) VALUES(?,?,?,?,zeroblob(0))",
                        (self.pid, p, gid, "h-" + p))
                # **标签的 revision 必须等于"触发器加过之后"的组修订号。**
                # 每插一个成员，`lp_member_added` 就把 content_revision +1；
                # 若标签仍写 1，则 `l.revision = g.content_revision` 不成立，
                # 该标签会被 duplicate_name_groups 的 JOIN 整条排除 ——
                # 夹具会"安静地考不到归一逻辑"（这正是本轮踩到的坑）。
                rev = int(c.execute(
                    "SELECT content_revision FROM large_catalog_groups "
                    "WHERE package_id=? AND id=?", (self.pid, gid)).fetchone()[0])
                c.execute(
                    "INSERT OR REPLACE INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.pid, gid, rev, 1, label_name, "同一主题下的同名叶子",
                     "[]", "fixture-model", "d-" + gid, 0, 0, 0.0))
            c.commit()

    def _group_count(self):
        with closing(self.store._connect()) as c:
            return int(c.execute("SELECT COUNT(*) FROM large_catalog_groups WHERE package_id=?",
                                 (self.pid,)).fetchone()[0])

    def test_twenty_same_named_leaves_collapse_to_one(self):
        """验收①：一个主题下 20 个同名叶组 → 归一为 1。

        这是用户最初投诉的那件事（实测 cssj4 最多 20 个叶子同名）。
        改动前全项目**没有任何"按名字合并"的代码**，名字只是聚类的输出。
        """
        specs = [("g-%02d" % i, ["f-%02d.txt" % i]) for i in range(20)]
        self._seed_groups(specs, label_name="法律法规与司法案例汇编")
        # Identical labels are insufficient: every group needs compatible content.
        with closing(self.store._connect()) as c:
            c.execute("UPDATE large_catalog_members SET vector=? WHERE package_id=?",
                      (struct.pack('ff', 1, 0), self.pid))
            c.execute("UPDATE lp_catalog_labels SET revision=(SELECT content_revision FROM large_catalog_groups g "
                      "WHERE g.package_id=lp_catalog_labels.package_id AND g.id=lp_catalog_labels.group_id) "
                      "WHERE package_id=?", (self.pid,))
        self.assertEqual(self._group_count(), 20)
        naming.normalize_leaves(self.store, self.pid)
        self.assertEqual(self._group_count(), 1,
                         "20 个同名叶组必须归一为 1 个（INV-3）")
        # 成员一个都不能丢
        with closing(self.store._connect()) as c:
            n = int(c.execute("SELECT COUNT(*) FROM large_catalog_members WHERE package_id=?",
                              (self.pid,)).fetchone()[0])
        self.assertEqual(n, 20, "归一不得丢成员")
        # 归一后不留空名：宿主保留原名
        with closing(self.store._connect()) as c:
            row = c.execute("SELECT name FROM lp_catalog_labels WHERE package_id=?",
                            (self.pid,)).fetchone()
        self.assertEqual(row[0], "法律法规与司法案例汇编")

    def test_same_named_leaves_without_content_evidence_must_not_merge(self):
        self._seed_groups([('a',['a.txt']),('b',['b.txt'])],label_name='法律法规与司法案例汇编')
        stats=naming.normalize_leaves(self.store,self.pid)
        self.assertEqual(stats['merged_groups'],0)
        self.assertEqual(self._group_count(),2)

    def test_leaf_count_converges_over_twenty_rounds(self):
        """验收②：连续 20 轮目录构建 → 叶组数**收敛**，不是 170→412 那种单调增长。

        改动前 `split_oversized` 只有"增"没有"减"、且没有累计上限与自适应 cap，
        实测某包叶组数从 170 单调涨到 412。现在：自适应 cap + `leaf_growth_cap`
        早退 + 同名归一，叶组数必须在少数几轮内稳定下来。
        """
        # 一个 200 个成员的大组：远超 cap(60)，必须被切，但只能切到刚好够
        self._seed_groups([("big", ["m-%03d.txt" % i for i in range(200)])],
                          label_name="混合语料")
        counts = []
        for _ in range(20):
            naming.queue_groups(self.store, self.pid)
            counts.append(self._group_count())
        self.assertLessEqual(counts[0], counts[-1] + 0 if False else 10 ** 6)  # 占位，见下断言
        # 核心断言：后 10 轮不再增长（收敛），且总量远小于"每文件一组"的退化
        self.assertLessEqual(max(counts[10:]), 64,
                             "20 轮之后叶组数必须收敛在增长上限附近，实测序列=%s" % counts)
        self.assertEqual(len(set(counts[10:])), 1,
                         "后 10 轮叶组数必须完全稳定（不单调增长），实测序列=%s" % counts)
        self.assertLessEqual(counts[-1], 200,
                             "叶组数不得退化成每文件一组")


    def test_stale_catalog_label_does_not_spin_the_worker(self):
        """反规范化标签陈旧 != 有待办：不得让 Worker 空转。

        背景（生产实测 2026-09-19）：KMODE=1 下命名**有意**派给后台队列，文件停在
        `catalog_status='pending'` 是正确稳态（见
        `test_large_semantic_catalog.test_background_names_do_not_wait_for_busy_generation`）。
        而"目录还有待推进项"这条兜底只认这个字段，于是对 5 个冻结包里 2225 个
        **早已进入真实叶组**的文件长期为真；命中后原实现无条件 `return True`，
        `run_forever` 因此永不休眠 —— 单核 100% 持续空转，每轮还附带一次全表
        扫描，并把紧随其后的概览补发彻底饿死。

        这条钉住的是：同一个陈旧标签在一轮调度里最多被当成待办一次。
        """
        self.store.upsert_file(self.pid, {'path': 'a.txt', 'name': 'a.txt', 'size': 10})
        self.store.update_file(self.pid, 'a.txt', quick_status='completed',
                               local_summary={'sample': 'remote attestation evidence. ' * 5,
                                              'portrait': {'schema': 'x', 'fields': [], 'headings': [],
                                                           'catalog_status': 'pending'}})
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO large_catalog_groups"
                      "(package_id,id,model,anchor,representative,result,status,updated_at,"
                      " attempts,content_revision,member_count) VALUES(?,?,'km','','','','named',?,0,1,1)",
                      (self.pid, 'g-1', 0.0))
            c.execute("INSERT OR REPLACE INTO large_catalog_members"
                      "(package_id,path,group_id,input_hash,vector) VALUES(?,?,?,?,zeroblob(0))",
                      (self.pid, 'a.txt', 'g-1', 'h'))
        self.store.update_package(self.pid, status='waiting_for_selection', phase='catalog_ready')

        worker = LargePackageWorker(self.store)
        # 本用例只测调度收敛，不测概览；隔离掉概览补发避免引入模型依赖。
        worker._overview_catchup = lambda: False
        scheduled = []
        original = worker._schedule_catalog_incremental
        worker._schedule_catalog_incremental = lambda pid: (scheduled.append(pid), original(pid))[1]
        try:
            # 阶段一：让正常车道把队列排空。这里走的是 embed_batch / 命名车道，
            # 属于真实工作，不计入"空转"。
            for _ in range(2):
                worker.run_once()
                worker._drain_catalog_lane()
            # 阶段二：队列已空，剩下的只有"陈旧标签"这条兜底。
            # 允许它首次尝试（契约要求首次必须尝试）。
            for _ in range(2):
                worker.run_once()
                worker._drain_catalog_lane()
            baseline = len(scheduled)
            # 阶段三：冷却期内反复调度，不得再产生新的尝试 —— 这正是原先
            # "每轮 return True" 让 run_forever 永不休眠的地方。
            for _ in range(5):
                worker.run_once()
                worker._drain_catalog_lane()
        finally:
            worker.preview_runner.close()
        self.assertEqual(
            len(scheduled), baseline,
            "陈旧标签在冷却期内被重复当成待办（%d -> %d 次）；"
            "这会让 run_forever 永不休眠、单核空转" % (baseline, len(scheduled)))


    def test_topic_layer_is_not_rebuilt_while_leaves_keep_changing(self):
        """叶组还在增长时不得反复重排已发布目录。

        背景（生产实测 2026-09-19）：`lp-702f7a` 在 27 分钟内重建主题层 **10 次**
        （叶组数 153→156→165→172→180→183→172→179→183→166 一路在变），主题数在
        10/16/10/20/19/15/19/18/18/9 之间跳变。代价是双重的：每次重建都要为全部主题
        重新调用模型，而用户正在看的目录会看到主题反复出现、消失、改名 ——
        框架 §11 明令"已发布目录不静默重排"。

        钉住的性质：**首次归并立即发生**（主题层要尽早出现），
        **之后的重建必须等叶组签名先稳定一段去抖窗口**。
        """
        import time as _time
        from unittest.mock import patch as _patch
        from services import large_package_catalog_naming as _naming

        # 造出 N 个"已命名且修订号对齐"的叶组，使主题归并的前置条件成立。
        with closing(self.store._connect()) as c:
            for i in range(20):
                gid = 'km-test-%05d' % i
                c.execute("INSERT OR REPLACE INTO large_catalog_groups"
                          "(package_id,id,model,anchor,representative,result,status,updated_at,"
                          " attempts,content_revision,member_count) "
                          "VALUES(?,?,'km','','','','named',?,0,1,3)", (self.pid, gid, 0.0))
                c.execute("INSERT OR REPLACE INTO lp_catalog_labels"
                          "(package_id,group_id,revision,version,name,description,evidence,"
                          " model,digest,needs_review,locked,updated) "
                          "VALUES(?,?,1,1,?,?,'','fixture-model','',0,0,?)",
                          (self.pid, gid, '叶组 %d' % i, '说明', 0.0))
        self.store.update_package(self.pid, status='waiting_for_selection', phase='catalog_ready')

        calls = []
        fake_round = {'id': 'round-test', 'deadline': _time.time() + 3600}
        with _patch.object(Config, 'LP_TOPIC_MERGE', True), _patch.object(_naming, 'merge_topics',
                           side_effect=lambda *a, **k: (calls.append(1), True)[1]), \
             _patch.object(_naming, 'ensure_round', return_value=fake_round):
            worker = LargePackageWorker(self.store)
            worker._overview_catchup = lambda: False
            try:
                # ① 首次：必须立即归并，主题层要尽早出现
                worker._merge_topics_once(self.pid)
                after_first = len(calls)
                # ② 叶组变化一点点（修订号变了 => 签名变了）
                with closing(self.store._connect()) as c:
                    c.execute("UPDATE large_catalog_groups SET content_revision=2 "
                              "WHERE package_id=? AND id='km-test-00000'", (self.pid,))
                worker._merge_topics_once(self.pid)
                after_change = len(calls)
                worker._merge_topics_once(self.pid)
                after_second = len(calls)
            finally:
                worker.preview_runner.close()

        self.assertEqual(after_first, 1, "首次主题归并必须立即发生，不能也被去抖挡住")
        self.assertEqual(after_change, 1,
                         "叶组刚变化就重建主题层 —— 这正是 27 分钟重建 10 次的成因")
        self.assertEqual(after_second, 1,
                         "叶组仍在变化时不得重复重建主题层")


    def test_deferred_topic_rebuild_is_owed_until_it_actually_runs(self):
        """去抖只能**延后**主题层重建，绝不能**取消**它。

        背景（生产实测 `lp-605781`，2026-09-19，重新聚类后）：09:06:46 做了首次
        主题归并，当时只有 28 个叶组已命名；之后叶组持续分裂，去抖把重建一次次
        推后；09:14:13 最后一次变化，而 09:16:11 就判定"定稿"—— 去抖窗口（180 秒）
        还差 62 秒没到，那次本该覆盖全部叶组的重建**永远不会发生**。

        后果：终态真实主题只覆盖 **28 / 159** 个叶组，132 个被永久留在
        "待归并主题"兜底分支里；而旧代码同一个包的终态是 14 个主题、
        168 个叶组全部归位。也就是说，"主题层不完整"被当成了"目录已定稿"。

        钉住的性质：
          * 叶组变化 → 重建被推迟，但系统**知道自己欠着一次重建**；
          * 去抖窗口一到 → 那次重建**必须真的执行**，欠账清零；
          * 于是定稿闸门不可能把欠着的重建冻掉。
        """
        import time as _time
        from unittest.mock import patch as _patch
        from services import large_package_catalog_naming as _naming

        with closing(self.store._connect()) as c:
            for i in range(20):
                gid = 'km-owed-%05d' % i
                c.execute("INSERT OR REPLACE INTO large_catalog_groups"
                          "(package_id,id,model,anchor,representative,result,status,updated_at,"
                          " attempts,content_revision,member_count) "
                          "VALUES(?,?,'km','','','','named',?,0,1,3)", (self.pid, gid, 0.0))
                c.execute("INSERT OR REPLACE INTO lp_catalog_labels"
                          "(package_id,group_id,revision,version,name,description,evidence,"
                          " model,digest,needs_review,locked,updated) "
                          "VALUES(?,?,1,1,?,?,'','fixture-model','',0,0,?)",
                          (self.pid, gid, '叶组 %d' % i, '说明', 0.0))
        self.store.update_package(self.pid, status='waiting_for_selection', phase='catalog_ready')

        calls = []
        fake_round = {'id': 'round-test', 'deadline': _time.time() + 3600}
        with _patch.object(Config, 'LP_TOPIC_MERGE', True), _patch.object(_naming, 'merge_topics',
                           side_effect=lambda *a, **k: (calls.append(1), True)[1]), \
             _patch.object(_naming, 'ensure_round', return_value=fake_round):
            worker = LargePackageWorker(self.store)
            worker._overview_catchup = lambda: False
            try:
                worker._merge_topics_once(self.pid)
                self.assertEqual(len(calls), 1, "首次归并必须立即发生")

                # 叶组变化 → 重建被推迟，并且**被记为欠账**
                with closing(self.store._connect()) as c:
                    c.execute("UPDATE large_catalog_groups SET content_revision=2 "
                              "WHERE package_id=? AND id='km-owed-00000'", (self.pid,))
                worker._merge_topics_once(self.pid)
                self.assertEqual(len(calls), 1, "叶组刚变化不得立刻重建")
                self.assertTrue(worker._topic_rebuild_owed(self.pid),
                                "重建只是被推迟，系统必须记得自己欠着一次重建 —— "
                                "否则定稿闸门会把这次重建永久冻掉")

                # 去抖窗口走完 → 欠着的那次重建必须真的执行
                sig, _ = worker._topics_pending[self.pid]
                worker._topics_pending[self.pid] = (sig, _time.monotonic() - 1000.0)
                worker._merge_topics_once(self.pid)
                self.assertEqual(len(calls), 2, "去抖窗口到了就必须补做那次重建")
                self.assertFalse(worker._topic_rebuild_owed(self.pid),
                                 "重建做完后不得再记着欠账，否则永远无法定稿")
            finally:
                worker.preview_runner.close()


    def _merge_fixture(self, sizes):
        """造一个包：若干叶组，组大小由 sizes 给定，成员带单位向量。

        返回 (package_id, rows) —— rows 可直接交给 apply_merge。
        """
        import json as _json
        import numpy as _np
        root = self.root / ('m%d' % len(sizes))
        root.mkdir(parents=True, exist_ok=True)
        pid = self.store.create(root)['id']
        vec = _np.array([1.0, 0.0, 0.0], dtype=_np.float32).tobytes()
        rows = []
        with closing(self.store._connect()) as c:
            for gi, size in enumerate(sizes):
                gid = 'km-mergetest-%05d' % gi
                c.execute("INSERT OR REPLACE INTO large_catalog_groups"
                          "(package_id,id,model,anchor,representative,result,status,updated_at,"
                          " attempts,content_revision,member_count) "
                          "VALUES(?,?,'km',?,'','{}','named',?,0,1,?)",
                          (pid, gid, vec, 0.0, size))
                for k in range(size):
                    c.execute("INSERT OR REPLACE INTO large_catalog_members"
                              "(package_id,path,group_id,input_hash,vector) VALUES(?,?,?,?,?)",
                              (pid, 'f%d-%d.txt' % (gi, k), gid, 'h', vec))
                # member_count 上有触发器会在成员插入时 +1，这里按实际数量回填，避免重复计。
                c.execute("UPDATE large_catalog_groups SET member_count="
                          "(SELECT COUNT(*) FROM large_catalog_members m "
                          " WHERE m.package_id=? AND m.group_id=?) WHERE package_id=? AND id=?",
                          (pid, gid, pid, gid))
                c.execute("INSERT OR REPLACE INTO lp_catalog_labels"
                          "(package_id,group_id,revision,version,name,description,evidence,"
                          " model,digest,needs_review,locked,updated) "
                          "VALUES(?,?,1,1,?,?,?,'fixture-model','',0,0,?)",
                          (pid, gid, '叶组 %d' % gi, '说明',
                           _json.dumps([{'text': '证据 %d' % gi}], ensure_ascii=False), 0.0))
            # 从库里回读真实的 content_revision / version：lp_catalog_labels 上有触发器会
            # 递增 large_catalog_groups.content_revision，写死的值会与库里不一致，
            # apply_merge 的一致性守卫会直接回滚。
            for gi in range(len(sizes)):
                gid = 'km-mergetest-%05d' % gi
                actual = c.execute(
                    'SELECT g.content_revision AS rev, l.version AS ver '
                    'FROM lp_catalog_labels l JOIN large_catalog_groups g '
                    'ON g.package_id=l.package_id AND g.id=l.group_id '
                    'WHERE l.package_id=? AND l.group_id=?', (pid, gid)).fetchone()
                rows.append({'group_id': gid, 'content_revision': actual['rev'],
                             'version': actual['ver'],
                             'name': '叶组 %d' % gi, 'description': '说明',
                             'evidence': _json.dumps([{'text': '证据 %d' % gi}], ensure_ascii=False)})
        return pid, rows

    def test_semantic_leaf_merge_respects_size_cap(self):
        """语义叶组合并不得产出超过叶组规模上限的组。

        背景（生产实测 2026-09-19）：`merge_topics -> consolidate` 的语义合并把被并组的
        全部成员搬进"最大的那个 host"，**全程没有大小检查**，于是产出 610 / 810 个文件的
        叶组，而这两个包的上限是 101 / 103（超 6.0 / 7.9 倍）。一个 810 文件的叶组只能有
        一个名字，名字必然覆盖不了内容 —— 这是"命名质量"退化的直接来源，而且合并后立刻
        标成 status='named'，一个名字就直接贴到了 810 个文件上。

        钉住的性质：
          * 装不下的合并必须**整体拒绝**，不得产生超限组；
          * 装得下的合并必须**照常进行**（不能因噎废食）。
        """
        from services import large_package_catalog_merge as merge_mod
        from services.large_package_semantic import effective_group_max

        # ① 装不下：40 + 30 = 70，而 cap = 60
        pid_a, rows_a = self._merge_fixture([40, 30])
        cap_a = int(effective_group_max(self.store, pid_a))
        self.assertEqual(cap_a, 60, "本用例依赖小包的 cap=60")
        moved = merge_mod.apply_merge(self.store, pid_a, rows_a,
                                      {'name': '合并名', 'description': '合并说明'},
                                      'fixture-model')
        self.assertEqual(moved, 0, "70 > cap 60 的合并必须被拒绝")
        with closing(self.store._connect()) as c:
            biggest = c.execute("SELECT MAX(member_count) FROM large_catalog_groups "
                                "WHERE package_id=?", (pid_a,)).fetchone()[0]
            left = c.execute("SELECT COUNT(*) FROM large_catalog_groups WHERE package_id=?",
                             (pid_a,)).fetchone()[0]
        self.assertLessEqual(biggest, cap_a, "合并后仍出现了超过 cap 的叶组")
        self.assertEqual(left, 2, "被拒绝的合并不应删除任何叶组")

        # ② 装得下：30 + 20 + 5 = 55 <= cap 60，必须正常合并
        pid_b, rows_b = self._merge_fixture([30, 20, 5])
        cap_b = int(effective_group_max(self.store, pid_b))
        moved_b = merge_mod.apply_merge(self.store, pid_b, rows_b,
                                        {'name': '合并名', 'description': '合并说明'},
                                        'fixture-model')
        self.assertEqual(moved_b, 2, "55 <= cap 60 的合并应当照常进行（合并掉 2 个组）")
        with closing(self.store._connect()) as c:
            sizes = [r[0] for r in c.execute(
                "SELECT member_count FROM large_catalog_groups WHERE package_id=?", (pid_b,))]
        self.assertEqual(sizes, [55], "合并后应只剩一个 55 成员的组")
        self.assertLessEqual(max(sizes), cap_b)


    def test_ungrouped_count_matches_what_the_tree_hides(self):
        """“不在任何主题下”的计数必须与主题树实际隐藏的文件数一致。

        背景（生产实测 2026-09-19）：`catalog_tree` 原来只统计"没有任何成员行"的文件，
        于是 `km-other-` / `km-pending-` 系统桶里的文件**既不出现在主题树里、也不计入
        这个提示** —— 目录看起来比实际更完整。实测 4 个包全部漏报：
        `lp-605781` 提示 0 而实际 58 个文件不在任何主题里（其余三包漏报 65 / 20 / 4）。

        钉住的性质：提示数 == 文件总数 − 真实叶组覆盖数。
        """
        import json as _json
        from services.large_package_catalog_view import catalog_tree

        with closing(self.store._connect()) as c:
            # 两个文件进真实叶组
            c.execute("INSERT OR REPLACE INTO large_catalog_groups"
                      "(package_id,id,model,anchor,representative,result,status,updated_at,"
                      " attempts,content_revision,member_count) "
                      "VALUES(?,?,'km','','','{}','named',?,0,1,2)", (self.pid, 'km-real-00001', 0.0))
            # 一个文件进系统桶
            c.execute("INSERT OR REPLACE INTO large_catalog_groups"
                      "(package_id,id,model,anchor,representative,result,status,updated_at,"
                      " attempts,content_revision,member_count) "
                      "VALUES(?,?,'km','','','{}','named',?,0,1,1)", (self.pid, 'km-other-abc12345', 0.0))
        for path in ('a.txt', 'b.txt', 'c.txt', 'd.txt'):
            self.store.upsert_file(self.pid, {'path': path, 'name': path, 'size': 1})
        with closing(self.store._connect()) as c:
            for path, gid in (('a.txt', 'km-real-00001'), ('b.txt', 'km-real-00001'),
                              ('c.txt', 'km-other-abc12345')):
                c.execute("INSERT OR REPLACE INTO large_catalog_members"
                          "(package_id,path,group_id,input_hash,vector) VALUES(?,?,?,'h',zeroblob(0))",
                          (self.pid, path, gid))
        # files 表用 store 自己的写入口，避免手写 NOT NULL 列（updated_at 等）。
        for path in ('a.txt', 'b.txt', 'c.txt', 'd.txt'):
            self.store.upsert_file(self.pid, {'path': path, 'name': path, 'size': 1})

        tree = catalog_tree(self.store, self.pid)
        with closing(self.store._connect()) as c:
            total = c.execute("SELECT COUNT(*) FROM files WHERE package_id=?",
                              (self.pid,)).fetchone()[0]
        self.assertEqual(total, 4)
        self.assertEqual(
            tree['ungrouped_files'], 2,
            "系统桶里的 c.txt 与完全无归属的 d.txt 都应计入未归组；"
            "只算无成员行会漏掉系统桶，目录看起来比实际完整")
        self.assertEqual(tree['ungrouped_files'], total - 2, "口径必须等于总数减真实叶组覆盖")


if __name__ == '__main__':
    unittest.main()
