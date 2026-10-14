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
from services import large_package_catalog_naming as naming
from services import large_package_catalog_topics as topics
from services import large_package_catalog_batch as batch


class ThemeModel:
    model = 'theme-test'
    supports_json_response_format = True

    def __init__(self, modify=None):
        self.inputs = []
        self.modify = modify

    def chat(self, system, user, **kwargs):
        data = json.loads(user)
        self.inputs.append(data)
        result = {'groups': [{'id': g['id'], 'name': g['names'][0].split('：', 1)[0]}
                             for g in data['groups']]}
        if self.modify:
            self.modify(result, len(self.inputs))
        return {'content': json.dumps(result, ensure_ascii=False),
                'usage': {'total_tokens': 100}, 'finish_reason': 'stop'}


class ThemeQualityTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(Config, 'SCAN_ALLOWED_ROOTS', ()),
                        patch.object(Config, 'LP_CATALOG_REQUESTS', 100),
                        patch.object(Config, 'LP_CATALOG_TOTAL_TOKENS', 100000),
                        patch.object(Config, 'LP_CATALOG_SECONDS', 120),
                        patch.object(Config, 'LP_CATALOG_BATCH_INPUT_TOKENS', 6500),
                        patch.dict('os.environ', {'SJFX_LP_TOPIC_BATCH_SIZE': '4'})]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)
        self.store = LargePackageStore(self.root/'state.db')
        self.pid = self.store.create(self.root)['id']
        self.round = naming.ensure_round(self.store, self.pid)
        self.buckets = {i: [{'group_id': 'g%d-%d' % (i, j),
                             'name': name, 'description': name+'的具体方法和适用范围。',
                             'content_revision': 1, 'files': 2} for j in range(3)]
                        for i, name in enumerate(('设备证据验证', '内存隔离机制', '数据传输协议', '访问权限控制'))}

    def run_names(self, model=None, cancelled=lambda: False):
        return topics.name_topics(self.store, self.pid, self.buckets, {}, self.round,
                                  cancelled, model or ThemeModel())

    def test_four_themes_one_request_and_replay_zero_requests(self):
        model = ThemeModel()
        first = self.run_names(model)
        self.assertEqual(len(first), 4)
        self.assertEqual(len(model.inputs), 1)
        self.assertEqual(first, self.run_names(model))
        self.assertEqual(len(model.inputs), 1)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM lp_catalog_calls').fetchone()[0], 1)

    def test_only_changed_theme_invalidates_cache(self):
        model = ThemeModel()
        self.run_names(model)
        self.buckets[2][-1]['description'] += '补充新的传输范围。'
        self.run_names(model)
        self.assertEqual([len(x['groups']) for x in model.inputs], [4, 1])

    def test_invalid_item_only_retries_that_theme(self):
        def modify(result, count):
            if count == 1:
                # 这里要的是一个**仍然非法**的名字，用来验证"只重试这一条"。
                # 2026-09-20 起「编造的缩写」（如 虚构XYZ技术系统）不再算非法：
                # 查不到的编号/缩写会被从名字里删掉并标复核，而不是让整条作废
                # （整条作废 + 重试同一提示 = 永远没有名字，生产实测 7 组踩过）。
                # 所以改用泛化名——它依旧非法，重试语义的覆盖不变。
                result['groups'][1]['name'] = '相关资料'
                result['groups'].extend([None, {'id': 'unknown', 'name': '无效引用'}])
        model = ThemeModel(modify)
        self.assertEqual(len(self.run_names(model)), 4)
        self.assertEqual([len(x['groups']) for x in model.inputs], [4, 1])

    def test_duplicate_item_does_not_poison_valid_siblings(self):
        def modify(result, count):
            if count == 1:
                result['groups'].append(dict(result['groups'][0]))
        model = ThemeModel(modify)
        self.assertEqual(len(self.run_names(model)), 4)
        self.assertEqual([len(x['groups']) for x in model.inputs], [4, 1])

    def test_failed_batch_has_bounded_single_retries(self):
        def modify(result, count):
            result['groups'] = []
        model = ThemeModel(modify)
        self.assertEqual(self.run_names(model), {})
        self.assertEqual([len(x['groups']) for x in model.inputs], [4, 1, 1, 1, 1])

    def test_pause_during_call_discards_results_and_cache(self):
        model = ThemeModel(lambda *_: self.store.update_package(self.pid, status='paused'))
        self.assertEqual(self.run_names(model), {})
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM lp_catalog_cache').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT status FROM lp_catalog_calls').fetchone()[0], 'stale')

    def test_cancelled_makes_no_call(self):
        model = ThemeModel()
        self.assertEqual(self.run_names(model, lambda: True), {})
        self.assertFalse(model.inputs)

    def test_theme_scope_embeddings_are_cached_and_failure_falls_back(self):
        from services import large_package_semantic as semantic
        from unittest.mock import Mock
        leaves=[members[0] for members in self.buckets.values()]
        client=Mock()
        client.model='scope-embedding'
        client.identity='scope-embedding:test'
        client._legacy_namespace=None
        client.dimension=4
        client.embed.side_effect=lambda texts: np.eye(4,dtype=np.float32)[:len(texts)].tolist()
        fallback={leaf['group_id']:None for leaf in leaves}
        with patch.object(semantic,'embedding_client',return_value=client):
            first=topics.semantic_centroids(self.store,self.pid,leaves,fallback)
            second=topics.semantic_centroids(self.store,self.pid,leaves,fallback)
        self.assertEqual(client.embed.call_count,1)
        for gid in first:
            np.testing.assert_array_equal(first[gid],second[gid])
        leaves[0]['description']='Changed scope'
        client.embed.side_effect=RuntimeError('offline')
        with patch.object(semantic,'embedding_client',return_value=client):
            self.assertIs(topics.semantic_centroids(self.store,self.pid,leaves,fallback),fallback)

    def test_expired_round_makes_no_call(self):
        self.round = dict(self.round)
        self.round['deadline'] = time.time()-1
        model = ThemeModel()
        self.assertEqual(self.run_names(model), {})
        self.assertFalse(model.inputs)

    def test_small_token_budget_falls_back_to_one_theme(self):
        payload = topics._material(self.buckets[0], {}, Config.LP_CATALOG_INPUT_TOKENS)
        user = json.dumps({'groups': [{'id': 'T0', 'total': payload['total'],
                          'names': [n['name'] + ('：'+n['scope'] if n.get('scope') else '')
                                    for n in payload['nodes']]}]}, ensure_ascii=False, separators=(',', ':'))
        budget = naming.token_count(topics.SYSTEM + '\n' + user) + 96 + 80
        with closing(self.store._connect()) as c:
            c.execute('UPDATE lp_catalog_rounds SET max_tokens=? WHERE package_id=?', (budget+5, self.pid))
        model = ThemeModel()
        self.run_names(model)
        self.assertTrue(model.inputs)
        self.assertEqual(len(model.inputs[0]['groups']), 1)

    def test_diverse_sample_keeps_boundary_beyond_first_twelve(self):
        members = [dict(self.buckets[0][0], group_id='g%03d' % i, files=100-i) for i in range(20)]
        vectors = {m['group_id']: np.array([1., 0.]) for m in members}
        vectors['g019'] = np.array([0., 1.])
        selected = topics._sample(members, vectors, 12)
        self.assertIn('g019', [m['group_id'] for m in selected])
        self.assertEqual(selected, topics._sample(list(reversed(members)), vectors, 12))

    def test_theme_count_uses_separation_not_target_quota(self):
        rng=np.random.default_rng(17)
        X=np.repeat(np.eye(4,dtype=np.float32),10,axis=0)+rng.normal(0,.015,(40,4))
        X/=np.linalg.norm(X,axis=1,keepdims=True)
        labels=topics.adaptive_labels(X,8)
        self.assertEqual(len(set(labels)),4)
        self.assertEqual(labels,topics.adaptive_labels(X,8))

    def test_theme_rejects_generic_or_unsupported_identifier(self):
        payload = {'nodes': [{'name': '设备证据验证', 'scope': '远程证明的设备可信状态'}]}
        # 泛化名、编号占位名、超长名依旧非法。
        # 「虚构XYZ系统研究」不再列在这里：查不到的缩写现在会被删掉并标复核，
        # 而不是让整条作废（见 naming.validate 的说明）。要验"编造缩写不许进目录"，
        # 看 test_catalog_evidence_quality.py::NamingUnverifiableTokenTests。
        for name in ('相关资料', '主题1', '未分类', 'A'*40):
            with self.assertRaises(ValueError):
                topics._validate({'name': name}, payload)


class LeafQualityTests(unittest.TestCase):
    def setUp(self):
        from tests.test_catalog_batch import CatalogBatchTests
        self.fixture = CatalogBatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store, self.pid = self.fixture.store, self.fixture.pid

    def test_leaf_unknown_item_keeps_valid_groups(self):
        original = self.fixture.model.chat
        def chat(*args, **kwargs):
            response = original(*args, **kwargs)
            data = json.loads(response['content'])
            data['groups'].extend([None, {'id': 'G999'}])
            response['content'] = json.dumps(data)
            return response
        self.fixture.model.chat = chat
        batch.process_batch(self.store, self.pid, model_client=self.fixture.model)
        self.assertEqual(naming.state(self.store, self.pid)['named_nodes'], 4)

    def test_leaf_duplicate_retries_only_ambiguous_group(self):
        original = self.fixture.model.chat
        def chat(*args, **kwargs):
            response = original(*args, **kwargs)
            data = json.loads(response['content'])
            data['groups'].append(dict(data['groups'][0]))
            response['content'] = json.dumps(data)
            return response
        self.fixture.model.chat = chat
        batch.process_batch(self.store, self.pid, model_client=self.fixture.model)
        self.assertEqual(naming.state(self.store, self.pid)['named_nodes'], 3)
        with closing(self.store._connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM lp_catalog_jobs WHERE attempts=1').fetchone()[0], 1)

    def test_representatives_cover_distinct_content_with_same_budget(self):
        gid = self.fixture.gids[0]
        with closing(self.store._connect()) as c:
            for i in range(12):
                path = 'diverse%02d.txt' % i
                self.store.upsert_file(self.pid, {'path': path, 'name': path})
                self.store.update_file(self.pid, path, local_summary={'sample': 'Evidence topic %d '%i*10})
                vector = ([0, 1, 0, 0] if i==10 else [.5, 0, .866, 0] if i==11 else [1, 0, 0, 0])
                c.execute('INSERT INTO large_catalog_members VALUES(?,?,?,?,?)',
                          (self.pid, path, gid, 'unique%d'%i, np.array(vector, dtype=np.float32).tobytes()))
        rows, _ = naming.representatives(self.store, self.pid, gid)
        paths = [r['path'] for r, _ in rows]
        # L3 起份数不再固定 5：按组内异质性自适应（同质 6、中等 10、异质最多 14），
        # 并被输入预算与成员数封顶。这条测试要守的是"**不同的内容必须被覆盖**"
        # （下面两个离群方向都要被选到），因此断言不变式而不是写死的数字。
        low, high, _per_sample, _lam = naming._sample_policy()
        self.assertGreaterEqual(len(rows), low)
        self.assertLessEqual(len(rows), high)
        self.assertIn('diverse10.txt', paths)
        self.assertIn('diverse11.txt', paths)

    def test_streaming_centroids_match_normalized_mean(self):
        leaves = [{'group_id': g} for g in self.fixture.gids]
        result = naming._leaf_centroids(self.store, self.pid, leaves)
        for i, gid in enumerate(self.fixture.gids):
            np.testing.assert_allclose(result[gid], np.eye(4)[i], atol=1e-6)


if __name__ == '__main__':
    unittest.main()
