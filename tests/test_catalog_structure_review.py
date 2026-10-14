"""Real directory invariants: independent scopes, live counts and visible failures."""
import json
from contextlib import closing
from unittest.mock import Mock, patch

import numpy as np
import pytest

import test_catalog_next_stage as fixtures
from config import Config
from services import large_package_catalog_naming as naming
from services import large_package_catalog_tree as tree
from services.large_package_catalog_view import enhance_catalog, catalog_tree


@pytest.fixture
def case():
    value = fixtures.NextStageTests()
    value.setUp()
    try:
        yield value
    finally:
        value.doCleanups()


def _build(case):
    rnd = naming.ensure_round(case.store, case.pid)
    with patch('services.large_package_catalog_topics.name_topics',
               side_effect=lambda _s, _p, buckets, *args: {k: '设备运行监测技术' for k in buckets}):
        assert tree.build(case.store, case.pid, rnd, model_client=Mock())


def test_distinct_leaves_are_persisted_without_inventing_a_theme(case):
    for i, name in enumerate(('设备温度记录', '仓储盘点清单', '医学临床试验')):
        case.add('g%d' % i, np.eye(3)[i], name=name)
    _build(case)
    with closing(case.store._connect()) as c:
        payload = json.loads(c.execute('SELECT payload FROM lp_catalog_tree').fetchone()[0])
        branches = [dict(row) for row in c.execute('SELECT * FROM lp_catalog_branches')]
        visible = tree.visible(c, case.pid)
    assert payload['nodes'] == []
    assert payload['independent_leaf_ids'] == ['g0', 'g1', 'g2']
    assert len(branches) == 3 and all(row['id'].startswith('independent:') for row in branches)
    assert {child for row in branches for child in json.loads(row['children'])} == {'g0', 'g1', 'g2'}
    assert visible[0]['kind'] == 'independent'
    assert visible[0]['name'] == '独立分组'
    assert visible[0]['independent_files'] == 3 and visible[0]['pending_files'] == 0
    with patch.object(Config, 'LP_CATALOG_VALIDATE_FIX', True, create=True):
        verdict = naming.validate_catalog(case.store, case.pid)
    assert verdict['ok'], verdict
    assert verdict['checks']['INV5_placeholders']['files_in_independent_leaves'] == 3


def test_outdated_independent_scope_returns_to_pending(case):
    case.add('g0', [1, 0], name='设备温度记录')
    _build(case)
    naming.rename_node(case.store, case.pid, 'g0', '人工确认设备运行参数')
    with closing(case.store._connect()) as c:
        visible = tree.visible(c, case.pid)
    assert visible[0]['kind'] == 'pending'
    assert visible[0]['independent_files'] == 0 and visible[0]['pending_files'] == 1
    assert _smart(case)['stats']['pending_theme_files'] == 1


def test_union_coverage_cannot_hide_a_missing_file(case):
    paths = case.add('g0', [1, 0], name='设备温度记录')
    case.store.update_file(case.pid, paths[0], local_summary={'portrait': {'catalog_status': 'no_content'}})
    case.store.upsert_file(case.pid, {'path': 'lost.txt', 'name': 'lost.txt', 'size': 1})
    with patch.object(Config, 'LP_CATALOG_VALIDATE_FIX', True, create=True):
        verdict = naming.validate_catalog(case.store, case.pid)
    assert verdict['checks']['INV2_covered']['covered'] == 1
    assert verdict['checks']['INV2_covered']['overlap'] == 1
    assert verdict['checks']['INV2_covered']['uncovered'] == 1
    assert any('INV-2' in value for value in verdict['violations'])


def _smart(case):
    return enhance_catalog(case.store, case.pid, {'smart': {}})['smart']


def test_tree_and_stats_count_live_files_and_exclude_empty_groups(case):
    case.add('g0', [1, 0], n=2)
    case.add('g1', [1, .01], n=1)
    _build(case)
    with closing(case.store._connect()) as c:
        c.execute('UPDATE large_catalog_groups SET member_count=999 WHERE package_id=?', (case.pid,))
        c.execute("INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at) "
                  "VALUES(?,'empty','test',x'','',0)", (case.pid,))
        c.commit()
    smart = _smart(case)
    assert smart['stats']['leaf_nodes'] == smart['leaf_total'] == 2
    assert smart['stats']['grouped_files'] == 3
    assert smart['stats']['named_files'] == 3
    assert smart['naming']['named_files'] == 3
    assert smart['naming']['named_nodes'] == smart['naming']['total_nodes'] == 2
    assert sum(item['files'] for item in smart['navigation']) == 3
    assert sum(item['files'] for item in smart['branches'] if not item.get('parent_id')) == 3
    assert smart['topic_total'] == 2


def test_failed_preview_shows_failure_reason_instead_of_waiting(case):
    case.store.upsert_file(case.pid, {'path': 'failed.pdf', 'name': 'failed.pdf', 'size': 1})
    case.store.update_file(case.pid, 'failed.pdf', quick_status='failed', error='[Errno 32] Broken pipe')
    smart = _smart(case)
    unknown = smart['topics'][0]
    assert unknown['name'] == '内容预览失败'
    assert unknown['state'] == unknown['naming_status'] == 'failed'
    assert 'Broken pipe' in unknown['basis']
    assert case.store.file_page(case.pid, node_id=unknown['node_id'])['total'] == 1
    assert smart['stats']['quick_failed_files'] == smart['stats']['ungrouped_files'] == 1


def test_raw_common_direction_does_not_force_an_unrelated_leaf(case):
    for i in range(3):
        case.add('tight%d' % i, [10, 1, 0], name='设备运行温度监测')
    case.add('unrelated', [10, 0, 1], name='医学临床试验')
    with patch.object(Config, 'LP_TOPIC_MIN', 2), patch.object(Config, 'LP_TOPIC_LEAVES_PER_TOPIC', 3), \
            patch('services.large_package_semantic._kmeans_fit', return_value=np.array([[0, 1, 0], [0, 0, 1]])):
        _build(case)
    with closing(case.store._connect()) as c:
        payload = json.loads(c.execute('SELECT payload FROM lp_catalog_tree').fetchone()[0])
    themed = {gid for node in payload['nodes'] for gid in node['leaf_ids']}
    assert themed == {'tight0', 'tight1', 'tight2'}
    assert payload['independent_leaf_ids'] == ['unrelated']


def test_two_unrelated_singletons_do_not_gain_a_synthetic_parent(case):
    case.add('cooperation', [1, 0], name='人类社会互惠与合作演化研究')
    case.add('copper', [0, 1], name='微生物铜获取与植物毒力机制研究')
    _build(case)
    with closing(case.store._connect()) as c:
        payload = json.loads(c.execute('SELECT payload FROM lp_catalog_tree').fetchone()[0])
    assert payload['nodes'] == []
    assert payload['independent_leaf_ids'] == ['cooperation', 'copper']
    verdict = naming.validate_catalog(case.store, case.pid)
    assert verdict['checks']['INV8_tree_partition']['ok'], verdict
    assert _smart(case)['stats']['independent_files'] == 2


def test_first_partition_rejects_unrelated_pair_despite_shared_corpus_direction(case):
    case.add('cooperation', [10, 1, 0, 0], name='人类社会互惠与合作演化研究')
    case.add('copper', [10, 0, 1, 0], name='微生物铜获取与植物毒力机制研究')
    for i in range(4):
        case.add('context%d' % i, [10, 0, 0, 1], name='设备运行温度监测')
    # Both unrelated leaves enter the same first-level bucket, while a genuine
    # coherent scope remains available. Raw centroid support is above .99.
    with patch.object(Config, 'LP_TOPIC_MIN', 2), patch.object(Config, 'LP_TOPIC_LEAVES_PER_TOPIC', 3), \
            patch('services.large_package_semantic._kmeans_fit',
                  return_value=np.array([[0, 1, 1, -2], [0, -1, -1, 2]])):
        _build(case)
    with closing(case.store._connect()) as c:
        payload = json.loads(c.execute('SELECT payload FROM lp_catalog_tree').fetchone()[0])
    assert {g for node in payload['nodes'] for g in node['leaf_ids']} == {
        'context0', 'context1', 'context2', 'context3'}
    assert payload['independent_leaf_ids'] == ['cooperation', 'copper']
    assert naming.validate_catalog(case.store, case.pid)['checks']['INV8_tree_partition']['ok']


def test_first_level_fallback_cannot_collapse_shared_template_vectors(case):
    for i, vec in enumerate(([10, 1, 0], [10, 0, 1], [10, -1, 0], [10, 0, -1])):
        case.add('g%d' % i, vec, name=('社会合作', '铜获取', '市场经营', '医学教育')[i])
    _build(case)
    with closing(case.store._connect()) as c:
        payload = json.loads(c.execute('SELECT payload FROM lp_catalog_tree').fetchone()[0])
    assert payload['nodes'] == []
    assert payload['independent_leaf_ids'] == ['g0', 'g1', 'g2', 'g3']
    assert not payload['pending_names']


def test_pending_theme_retry_keeps_version_until_directory_actually_changes(case):
    for i, name in enumerate(('设备温度监测', '设备压力监测', '设备故障预警', '设备状态记录')):
        case.add('g%d' % i, [1, 0, 0, .01 * i], name=name)
    rnd = naming.ensure_round(case.store, case.pid)
    with patch('services.large_package_catalog_topics.name_topics', return_value={}):
        assert tree.build(case.store, case.pid, rnd, model_client=Mock())
        before = naming.ensure_catalog_version(case.store, case.pid, allow_previous=False)
        assert before
        assert tree.build(case.store, case.pid, rnd, model_client=Mock())
        assert naming.ensure_catalog_version(case.store, case.pid, allow_previous=False) == before
    with closing(case.store._connect()) as c:
        payload = json.loads(c.execute('SELECT payload FROM lp_catalog_tree').fetchone()[0])
    assert payload['pending_names'] and payload['topic_regeneration_attempts'] == 2
    assert len(payload['nodes']) == 1
    with patch('services.large_package_catalog_topics.name_topics', return_value={0: '工业设备健康管理'}):
        assert tree.build(case.store, case.pid, rnd, model_client=Mock())
    after = naming.ensure_catalog_version(case.store, case.pid, allow_previous=False)
    assert after and after != before
    assert not tree.build(case.store, case.pid, rnd, model_client=Mock())


def test_outlier_bucket_uses_the_same_truthful_name_at_all_read_endpoints(case):
    paths = case.add('km-other-review', [1, 0], n=2, name='未能识别内容')
    with closing(case.store._connect()) as c:
        c.execute('UPDATE large_catalog_groups SET result=? WHERE package_id=?',
                  (json.dumps({'hold_reason': 'outliers'}), case.pid))
        c.commit()
    smart = _smart(case)
    assert smart['topics'][0]['name'] == smart['navigation'][0]['name'] == '离群内容待归并'
    assert catalog_tree(case.store, case.pid)['themes'][0]['leaves'][0]['name'] == '离群内容待归并'
    assert case.store.get_file(case.pid, paths[0])['catalog_name'] == '离群内容待归并'
    assert smart['stats']['grouped_files'] == 0
    assert smart['stats']['member_files'] == smart['stats']['system_bucket_files'] == 2
    assert smart['stats']['ungrouped_files'] == 0
    note = naming.validate_catalog(case.store, case.pid)['checks']['INV5b_system_bucket']['note']
    assert '按保留原因展示' in note and '离群内容待归并' in note
    assert '名称固定' not in note


def test_stale_leaf_without_work_cannot_claim_a_valid_name(case):
    case.add('g0', [1, 0], name='设备温度记录')
    _build(case)
    with closing(case.store._connect()) as c:
        c.execute('UPDATE lp_catalog_labels SET revision=revision+99 WHERE package_id=?', (case.pid,))
        c.commit()
    verdict = naming.validate_catalog(case.store, case.pid)
    assert verdict['checks']['INV7_unnamed']['groups_without_name'] == 1
    assert any('INV-7' in value for value in verdict['violations'])


def test_inconsistent_persistent_partition_blocks_publication(case):
    case.add('g0', [1, 0], name='设备温度记录')
    _build(case)
    with closing(case.store._connect()) as c:
        c.execute('DELETE FROM lp_catalog_branches WHERE package_id=?', (case.pid,))
        c.commit()
    verdict = naming.validate_catalog(case.store, case.pid)
    assert not verdict['checks']['INV8_tree_partition']['ok']
    assert any('INV-8' in value for value in verdict['violations'])
    assert naming.ensure_catalog_version(case.store, case.pid, allow_previous=False) == ''


def test_catalog_version_records_visible_theme_and_live_leaf_counts(case):
    for i, name in enumerate(('设备温度记录', '仓储盘点清单', '医学临床试验')):
        case.add('g%d' % i, np.eye(3)[i], name=name)
    _build(case)
    catalog_id = naming.ensure_catalog_version(case.store, case.pid, allow_previous=False)
    assert catalog_id
    assert naming.ensure_catalog_version(case.store, case.pid, allow_previous=False) == catalog_id
    with closing(case.store._connect()) as c:
        version = c.execute('SELECT * FROM lp_catalog_versions WHERE catalog_id=?', (catalog_id,)).fetchone()
        evidence = c.execute('SELECT stats FROM lp_evidence_snapshots WHERE catalog_id=?', (catalog_id,)).fetchone()
    stats = _smart(case)['stats']
    assert version['topic_count'] == stats['theme_nodes'] == 0
    assert version['leaf_count'] == stats['named_leaf_nodes'] == 3
    assert json.loads(version['validation'])['checks']['HIERARCHY_counts']['root_theme_nodes'] == 0
    assert json.loads(evidence['stats'])['named_groups'] == 3


def test_current_deterministic_name_is_marked_temporary_not_model_generated(case):
    paths = case.add('g0', [1, 0], name='RESEARCH Open Access')
    with closing(case.store._connect()) as c:
        c.execute("UPDATE lp_catalog_labels SET model='deterministic',needs_review=1 WHERE package_id=?", (case.pid,))
        c.commit()
    smart = _smart(case)
    assert smart['topics'][0]['naming_status'] == smart['navigation'][0]['naming_status'] == 'extractive'
    assert case.store.get_file(case.pid, paths[0])['catalog_naming_status'] == 'extractive'
    assert smart['stats']['temporary_leaf_nodes'] == smart['stats']['named_leaf_nodes'] == 1
    assert smart['stats']['model_named_leaf_nodes'] == 0
