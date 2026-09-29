# -*- coding: utf-8 -*-
"""批次 3a / 3b / 4 的回归测试（方案 §6.1 §6.2 §6.3 §7 §8）。

全部确定性、不依赖模型服务与网络：
  · §6.1 成员指纹：minhash 的估计误差与 Jaccard 真值的偏差必须有界；
  · §6.2 叶组名字继承：开关默认关闭时行为逐字不变；打开后"内容几乎没变"的组
    直接沿用原名且**不排命名任务**；阈值不达标 / 漂移过大 / 连续沿用超限 /
    兜底名与人工名 一律不沿用；
  · §6.3 统一缓存键：默认关闭时与旧键逐字相同；打开后新键格式固定，
    且批量路径与单组路径算出来的是同一个键；
  · §7 压缩输出：默认关闭时提示词逐字不变；打开后提示词变体进缓存键
    （否则 A/B 会被旧缓存污染）、purity 与 evidence_ids 的硬约束仍在；
  · §8 样本去重：默认关闭时选样结果与改动前完全一致；打开后近重复样本被跳过，
    宁可少给一份。
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

import numpy as np  # noqa: E402

from services.large_package_isolated import LargePackageStore   # noqa: E402
from services import large_package_catalog_batch as batch       # noqa: E402
from services import large_package_catalog_naming as naming     # noqa: E402
from services import large_package_catalog_schema as schema     # noqa: E402


ENV_KEYS = ('SJFX_LP_LEAF_NAME_INHERIT_JACCARD', 'SJFX_LP_LEAF_NAME_INHERIT_MAX_DRIFT',
            'SJFX_LP_LEAF_NAME_INHERIT_MAX_STREAK', 'SJFX_LP_CACHE_KEY_UNIFIED',
            'SJFX_LP_NAMING_COMPACT_OUTPUT', 'SJFX_LP_SAMPLE_DEDUP_COSINE')


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='b3-')
        self.store = LargePackageStore(db_path=os.path.join(self.dir, 'lp.db'))
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.make_package('lp-x')

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def make_package(self, package_id):
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO packages(id,root_path,owner_id,status,phase,"
                      "progress,counts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (package_id, '/tmp/b3', 'o1', 'waiting_for_selection',
                       'catalog_build', 50, '{}', time.time(), time.time()))
            c.commit()

    def make_group(self, package_id, group_id, paths, model='qwen3.6:27b'):
        with closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO large_catalog_groups"
                      "(package_id,id,model,anchor,representative,result,status,updated_at,"
                      "member_count,content_revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (package_id, group_id, model, b'', paths[0] if paths else '',
                       '{}', 'named', time.time(), len(paths), 1))
            for path in paths:
                c.execute("INSERT OR REPLACE INTO large_catalog_members"
                          "(package_id,group_id,path,input_hash,vector) VALUES(?,?,?,?,?)",
                          (package_id, group_id, path, 'h-' + path, b''))
            c.commit()

    def set_group_members(self, package_id, group_id, paths):
        """直接改成员表（触发器会把 content_revision +1）。"""
        with self.store._lock, closing(self.store._connect()) as c:
            c.execute('DELETE FROM large_catalog_members WHERE package_id=? AND group_id=?',
                      (package_id, group_id))
            for path in paths:
                c.execute("INSERT INTO large_catalog_members"
                          "(package_id,group_id,path,input_hash,vector) VALUES(?,?,?,?,?)",
                          (package_id, group_id, path, 'h-' + path, b''))
            c.execute('UPDATE large_catalog_groups SET member_count=? WHERE package_id=? AND id=?',
                      (len(paths), package_id, group_id))
            c.commit()

    def publish_label(self, package_id, group_id, name='旧名字', model='qwen3.6:27b',
                      digest='digest-1', revision=1, locked=0, evidence='[]'):
        with self.store._lock, closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO lp_catalog_labels"
                      "(package_id,group_id,revision,version,name,description,evidence,model,"
                      "digest,needs_review,locked,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (package_id, group_id, revision, 1, name, '描述', evidence, model,
                       digest, 0, locked, time.time()))
            c.commit()

    def record_fingerprint(self, package_id, group_id):
        paths = naming._member_paths(self.store, package_id, group_id)
        with self.store._lock, closing(self.store._connect()) as c:
            c.execute("INSERT OR REPLACE INTO lp_catalog_label_members"
                      "(package_id,group_id,revision,member_count,minhash,named_at) "
                      "VALUES(?,?,?,?,?,?)",
                      (package_id, group_id, naming.group_revision(self.store, package_id,
                                                                   group_id),
                       len(paths), naming.minhash_bytes(paths), time.time()))
            c.commit()
        return paths

    def group_revision(self, package_id, group_id):
        return naming.group_revision(self.store, package_id, group_id)

    def label_revision(self, package_id, group_id):
        with closing(self.store._connect()) as c:
            row = c.execute('SELECT revision FROM lp_catalog_labels '
                            'WHERE package_id=? AND group_id=?',
                            (package_id, group_id)).fetchone()
        return int(row[0]) if row else None

    def job_status(self, package_id, group_id):
        with closing(self.store._connect()) as c:
            row = c.execute('SELECT status FROM lp_catalog_jobs '
                            'WHERE package_id=? AND group_id=?',
                            (package_id, group_id)).fetchone()
        return row[0] if row else None

    def queued_jobs(self, package_id):
        with closing(self.store._connect()) as c:
            return [r[0] for r in c.execute(
                "SELECT group_id FROM lp_catalog_jobs WHERE package_id=? AND status='queued'",
                (package_id,))]


# ============================================================ §6.1 指纹
class MinhashTests(_Base):
    def test_signature_is_fixed_length(self):
        blob = naming.minhash_bytes(['a', 'b', 'c'])
        self.assertEqual(len(blob), naming.MINHASH_SIZE * 8)

    def test_empty_membership_is_all_zero(self):
        blob = naming.minhash_bytes([])
        self.assertEqual(blob, b'\0' * (naming.MINHASH_SIZE * 8))
        self.assertIsNone(naming.estimate_jaccard(blob, b''))

    def test_identical_sets_are_one(self):
        paths = ['p%02d' % i for i in range(40)]
        self.assertEqual(naming.estimate_jaccard(naming.minhash_bytes(paths),
                                                 naming.minhash_bytes(list(paths))), 1.0)

    def test_disjoint_sets_are_zero(self):
        left = ['a%02d' % i for i in range(40)]
        right = ['b%02d' % i for i in range(40)]
        self.assertEqual(naming.estimate_jaccard(naming.minhash_bytes(left),
                                                 naming.minhash_bytes(right)), 0.0)

    def test_estimate_tracks_true_jaccard_within_tolerance(self):
        """估算与真值的偏差要在 minhash 的理论误差范围内（标准误约 1/sqrt(128)）。"""
        base = ['p%03d' % i for i in range(200)]
        for overlap in (100, 150, 180, 190):
            other = base[:overlap] + ['q%03d' % i for i in range(200 - overlap)]
            true = len(set(base) & set(other)) / float(len(set(base) | set(other)))
            est = naming.estimate_jaccard(naming.minhash_bytes(base),
                                          naming.minhash_bytes(other))
            self.assertLess(abs(est - true), 0.12,
                            'overlap=%d true=%.3f est=%.3f' % (overlap, true, est))


# ============================================================ §6.2 名字继承
class LeafInheritanceTests(_Base):
    def _fixture(self, new_paths, old_paths=None):
        old_paths = old_paths or ['f%03d.txt' % i for i in range(30)]
        self.make_group('lp-x', 'g1', old_paths)
        self.publish_label('lp-x', 'g1')
        self.record_fingerprint('lp-x', 'g1')
        self.set_group_members('lp-x', 'g1', new_paths)
        return old_paths

    def test_switch_off_is_exactly_old_behaviour(self):
        """默认关闭：一行都不动，照旧排命名任务。"""
        old = self._fixture(['f%03d.txt' % i for i in range(30)])
        self.assertEqual(naming._leaf_inherit_threshold(), 0.0)
        naming.queue_groups(self.store, 'lp-x')
        self.assertEqual(self.queued_jobs('lp-x'), ['g1'],
                         '默认值下必须照旧重新命名')
        self.assertEqual(self.label_revision('lp-x', 'g1'), 1)

    def test_switch_on_inherits_when_content_barely_changed(self):
        old = ['f%03d.txt' % i for i in range(30)]
        # 只挪进来 1 个新成员：Jaccard ≈ 30/31 ≈ 0.968
        self._fixture(old + ['extra.txt'], old)
        os.environ['SJFX_LP_LEAF_NAME_INHERIT_JACCARD'] = '0.9'
        naming.queue_groups(self.store, 'lp-x')
        self.assertEqual(self.queued_jobs('lp-x'), [],
                         '内容几乎没变时不该重新命名')
        self.assertEqual(self.label_revision('lp-x', 'g1'),
                         self.group_revision('lp-x', 'g1'),
                         '继承后标签修订号必须追平当前修订号')

    def test_inheritance_leaves_an_audit_trail(self):
        old = ['f%03d.txt' % i for i in range(30)]
        self._fixture(old + ['extra.txt'], old)
        os.environ['SJFX_LP_LEAF_NAME_INHERIT_JACCARD'] = '0.9'
        naming.queue_groups(self.store, 'lp-x')
        with closing(self.store._connect()) as c:
            evidence = json.loads(c.execute(
                'SELECT evidence FROM lp_catalog_labels WHERE package_id=? AND group_id=?',
                ('lp-x', 'g1')).fetchone()[0])
            streak = c.execute('SELECT streak FROM lp_catalog_label_inherit '
                               'WHERE package_id=? AND group_id=?', ('lp-x', 'g1')).fetchone()
        self.assertTrue(any(isinstance(e, dict) and 'inherited_from_rev' in e for e in evidence),
                        '继承痕迹必须写进 evidence：%r' % evidence)
        self.assertEqual(streak[0], 1)

    def test_threshold_blocks_distant_content(self):
        """成员换了 40%：Jaccard 低于 0.9，必须重新命名。"""
        old = ['f%03d.txt' % i for i in range(30)]
        new = old[:18] + ['new%02d.txt' % i for i in range(12)]
        self._fixture(new, old)
        os.environ['SJFX_LP_LEAF_NAME_INHERIT_JACCARD'] = '0.9'
        naming.queue_groups(self.store, 'lp-x')
        self.assertEqual(self.queued_jobs('lp-x'), ['g1'], '内容变化大时必须重新命名')

    def test_member_count_drift_blocks_inheritance(self):
        """成员数变化超过默认 10% 就不沿用，即使 Jaccard 还很高。"""
        old = ['f%03d.txt' % i for i in range(30)]
        # 30 -> 34：漂移 13.3% > 10%，但旧成员全在，Jaccard = 30/34 = 0.88
        self._fixture(old + ['x%02d.txt' % i for i in range(4)], old)
        os.environ['SJFX_LP_LEAF_NAME_INHERIT_JACCARD'] = '0.5'
        naming.queue_groups(self.store, 'lp-x')
        self.assertEqual(self.queued_jobs('lp-x'), ['g1'],
                         '成员数漂移超限时必须重新命名')

    def test_streak_limit_forces_renaming(self):
        """连续沿用超过上限就强制重新命名（累计漂移不被放过）。"""
        old = ['f%03d.txt' % i for i in range(30)]
        self._fixture(old + ['extra.txt'], old)
        os.environ['SJFX_LP_LEAF_NAME_INHERIT_JACCARD'] = '0.9'
        os.environ['SJFX_LP_LEAF_NAME_INHERIT_MAX_STREAK'] = '2'
        with self.store._lock, closing(self.store._connect()) as c:
            c.execute('INSERT OR REPLACE INTO lp_catalog_label_inherit'
                      '(package_id,group_id,streak,updated) VALUES(?,?,?,?)',
                      ('lp-x', 'g1', 2, time.time()))
            c.commit()
        naming.queue_groups(self.store, 'lp-x')
        self.assertEqual(self.queued_jobs('lp-x'), ['g1'],
                         '连续沿用次数到顶必须重新命名')

    def test_fallback_names_are_never_inherited(self):
        """兜底名（deterministic / fallback-naming）不沿用。"""
        old = ['f%03d.txt' % i for i in range(30)]
        self._fixture(old + ['extra.txt'], old)
        os.environ['SJFX_LP_LEAF_NAME_INHERIT_JACCARD'] = '0.9'
        for model in ('deterministic', 'fallback-naming'):
            self.publish_label('lp-x', 'g1', model=model,
                               revision=self.group_revision('lp-x', 'g1') - 1)
            naming.queue_groups(self.store, 'lp-x')
            self.assertEqual(self.queued_jobs('lp-x'), ['g1'],
                             '%s 名不该被沿用' % model)
            with self.store._lock, closing(self.store._connect()) as c:
                c.execute("DELETE FROM lp_catalog_jobs WHERE package_id='lp-x'")
                c.commit()

    def test_manual_names_are_never_inherited(self):
        """人工名不沿用：**两道**防线分别钉住。

        真实的人工名一定是 `locked=1`（见 `naming.rename_node`），而 `locked=1`
        的标签在 `queue_groups` 的候选查询里就被排除了 —— 这是原代码本来就有的
        语义：人工确认过的名字既不重新命名、也不被继承改写。
        对 `locked=0` 但 `model='manual'` 的历史/异常形态，则由
        `inherit_leaf_names` 里的 model 判定兜住。第一版 `_resolve_revisions`
        把 model 字段丢掉了，这道防线完全失效（兜底名与人工名都会被静默沿用），
        正是这条用例抓到的。
        """
        old = ['f%03d.txt' % i for i in range(30)]
        self._fixture(old + ['extra.txt'], old)
        os.environ['SJFX_LP_LEAF_NAME_INHERIT_JACCARD'] = '0.9'

        # ① locked=1（真实形态）：既不排任务，也不继承 —— 标签修订号保持原样
        self.publish_label('lp-x', 'g1', model='manual', digest='manual-3',
                           revision=self.group_revision('lp-x', 'g1') - 1, locked=1)
        before = self.label_revision('lp-x', 'g1')
        naming.queue_groups(self.store, 'lp-x')
        self.assertEqual(self.queued_jobs('lp-x'), [], '锁定的人工名不该被重新命名')
        self.assertEqual(self.label_revision('lp-x', 'g1'), before,
                         '锁定的人工名的修订号不该被继承改写')
        with self.store._lock, closing(self.store._connect()) as c:
            c.execute("DELETE FROM lp_catalog_jobs WHERE package_id='lp-x'")
            c.commit()

        # ② locked=0 但 model='manual'：必须重新命名（model 那道防线）
        self.publish_label('lp-x', 'g1', model='manual', digest='manual-4',
                           revision=self.group_revision('lp-x', 'g1') - 1, locked=0)
        naming.queue_groups(self.store, 'lp-x')
        self.assertEqual(self.queued_jobs('lp-x'), ['g1'],
                         'model=manual 的标签即使没锁也不该被沿用，必须重新命名')

    def test_no_fingerprint_means_rename(self):
        """第 1 部分上线之前命名的组没有指纹：无从判定，照旧重新命名。"""
        old = ['f%03d.txt' % i for i in range(30)]
        self.make_group('lp-x', 'g1', old)
        self.publish_label('lp-x', 'g1')
        self.set_group_members('lp-x', 'g1', old + ['extra.txt'])
        os.environ['SJFX_LP_LEAF_NAME_INHERIT_JACCARD'] = '0.9'
        naming.queue_groups(self.store, 'lp-x')
        self.assertEqual(self.queued_jobs('lp-x'), ['g1'])


# ============================================================ §6.3 统一缓存键
class UnifiedCacheKeyTests(_Base):
    MATERIAL = {'digest': 'abc123'}

    def test_switch_off_keeps_legacy_key(self):
        self.assertFalse(naming.cache_key_unified())
        self.assertEqual(naming.cache_key_legacy(self.MATERIAL), 'abc123')

    def test_key_encodes_output_budget(self):
        self.assertEqual(naming.cache_key(self.MATERIAL, 300), 'abc123\0out:300')
        self.assertEqual(naming.cache_key(self.MATERIAL, 0), 'abc123\0out:0')

    def test_batch_and_single_agree_on_the_key(self):
        """批量路径按每个组自己的 output_budget 算，必须与单组路径算出同一个键。"""
        material = {'digest': 'd1', 'route': 'clear'}
        cfg = naming.settings()
        single = naming.cache_key(material, schema.output_budget(cfg, material))
        via_batch = naming.cache_key(material, naming.output_budget_for(material))
        self.assertEqual(single, via_batch)

    def test_boundary_route_gets_the_bigger_budget(self):
        cfg = naming.settings()
        clear = {'digest': 'd', 'route': 'clear'}
        boundary = {'digest': 'd', 'route': 'boundary'}
        self.assertGreater(schema.output_budget(cfg, boundary),
                           schema.output_budget(cfg, clear))

    def test_switch_is_read_at_call_time(self):
        os.environ['SJFX_LP_CACHE_KEY_UNIFIED'] = '1'
        self.assertTrue(naming.cache_key_unified())
        os.environ['SJFX_LP_CACHE_KEY_UNIFIED'] = '0'
        self.assertFalse(naming.cache_key_unified())


# ============================================================ §7 压缩输出
class CompactOutputTests(_Base):
    def test_switch_off_keeps_the_exact_prompt(self):
        self.assertFalse(naming.compact_output_enabled())
        self.assertEqual(naming.prompt_variant(), 'full')
        self.assertEqual(naming.system_prompt(), naming.SYSTEM)
        self.assertEqual(naming.naming_output_rules(), naming.NAMING_OUTPUT_RULES)
        self.assertEqual(naming.naming_result_schema(), naming.NAMING_RESULT_SCHEMA)
        self.assertEqual(batch.batch_system(), batch.BATCH_SYSTEM)

    def test_switch_on_swaps_the_output_rules_only(self):
        os.environ['SJFX_LP_NAMING_COMPACT_OUTPUT'] = '1'
        self.assertTrue(naming.compact_output_enabled())
        self.assertEqual(naming.prompt_variant(), 'compact')
        prompt = naming.system_prompt()
        self.assertIn(naming.NAMING_OUTPUT_RULES_COMPACT, prompt)
        self.assertNotIn(naming.NAMING_OUTPUT_RULES, prompt)
        # 输入侧规则（证据怎么读）不该被批次 3b 碰到
        self.assertIn(naming.NAMING_RULES, prompt)

    def test_compact_prompt_still_pins_purity_and_two_evidence_ids(self):
        """方案 §7 第 2、3 条：purity 不得省略；多成员组 evidence_ids 至少 2 个。"""
        rules = naming.NAMING_OUTPUT_RULES_COMPACT
        self.assertIn('purity', rules)
        self.assertIn('不得省略', rules)
        self.assertIn('至少给2个', rules)

    def test_compact_batch_prompt_uses_compact_rules(self):
        os.environ['SJFX_LP_NAMING_COMPACT_OUTPUT'] = '1'
        prompt = batch.batch_system()
        self.assertIn(naming.NAMING_OUTPUT_RULES_COMPACT, prompt)
        self.assertIn('groups', prompt)

    def test_prompt_variant_enters_the_cache_key(self):
        """方案 §7 第 4 条：只改提示词时旧缓存**不得**被命中。"""
        args = ('lp-x', 'g1', 3, 'owner')
        full = naming.bundle_key(*args)
        os.environ['SJFX_LP_NAMING_COMPACT_OUTPUT'] = '1'
        compact = naming.bundle_key(*args)
        self.assertNotEqual(full, compact,
                            '提示词变体必须进缓存键，否则 A/B 测试会被旧缓存污染')

    def test_bundle_cache_key_also_tracks_the_variant(self):
        source = None
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'services', 'large_package_catalog_naming.py'),
                  encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn('prompt_variant()', source)


# ============================================================ §8 样本去重
class SampleDedupTests(_Base):
    def _matrix(self):
        # 6 个样本：0 与 1 几乎同向（余弦 0.9998），其余互相独立。
        # 刻意让 total(6) > want(5)：否则 select_evidence 会在开头的
        # `total<=want` 分支直接全取 —— 那条短路是既有行为，不是本批要改的地方。
        rows = [
            [1.0, 0.0, 0.0],
            [0.9998, 0.02, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [0.0, 0.30, 0.95],
            [0.0, 0.95, 0.30],
        ]
        matrix = np.array(rows, dtype=np.float32)
        matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix

    def test_switch_off_returns_the_original_selection(self):
        """默认关闭：`_sample_dedup_gap()` 返回 None，选样结果与改动前完全相同。"""
        self.assertIsNone(naming._sample_dedup_gap())
        matrix = self._matrix()
        relevance = np.array([1.0, 0.99, 0.5, 0.4, 0.3, 0.3], dtype=np.float32)
        picked = naming.select_evidence(matrix, relevance, 5, 0.9)
        self.assertEqual(len(picked), 5, '关闭时必须照旧把份数补满')
        self.assertEqual(len(set(picked)), 5)

    def test_switch_on_skips_near_duplicates(self):
        os.environ['SJFX_LP_SAMPLE_DEDUP_COSINE'] = '0.97'
        self.assertAlmostEqual(naming._sample_dedup_gap(), 0.03, places=6)
        matrix = self._matrix()
        relevance = np.array([1.0, 0.99, 0.5, 0.4, 0.3, 0.3], dtype=np.float32)
        picked = naming.select_evidence(matrix, relevance, 5, 0.9)
        self.assertNotIn(1, picked, '与已选样本余弦 0.9998 的候选必须被跳过')
        self.assertLessEqual(len(picked), 5, '宁可少给一份样本')

    def test_switch_threshold_mapping(self):
        os.environ['SJFX_LP_SAMPLE_DEDUP_COSINE'] = '0.9'
        self.assertAlmostEqual(naming._sample_dedup_gap(), 0.1, places=6)
        os.environ['SJFX_LP_SAMPLE_DEDUP_COSINE'] = '0'
        self.assertIsNone(naming._sample_dedup_gap())


if __name__ == '__main__':
    unittest.main()
