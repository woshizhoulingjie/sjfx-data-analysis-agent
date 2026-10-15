"""Small packages must leave staging through the real recluster transaction."""
import hashlib
import json
import time
from contextlib import closing

import numpy as np
import pytest

from config import Config
from services import large_package_catalog as catalog
from services import large_package_catalog_naming as naming
from services import large_package_semantic as semantic
from services.large_package_isolated import LargePackageStore


@pytest.fixture
def package(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', ())
    monkeypatch.setattr(Config, 'LP_CATALOG_KMODE', True)
    monkeypatch.setattr(Config, 'LP_CATALOG_VALIDATE_FIX', False)
    monkeypatch.setattr(Config, 'LP_KM_MIN_GROUP', 1)
    monkeypatch.setattr(Config, 'LP_KM_MIN_GROUP_AUTO', True)
    store = LargePackageStore(tmp_path / 'state.db')
    pid = store.create(tmp_path)['id']
    return store, pid


def stage(package, vectors):
    """Persist the same placeholder/member shape produced by embed_batch."""
    store, pid = package
    placeholder = 'km-pending-' + hashlib.sha256(pid.encode()).hexdigest()[:8]
    for i in range(len(vectors)):
        path = 'file%d.txt' % i
        store.upsert_file(pid, dict(path=path, name=path, size=100, modified_at_ns=1))
        store.update_file(pid, path, quick_status='completed', local_summary={
            'sample': '设备运行监测记录状态参数和异常处置结果。' * 5,
            'portrait': {'headings': ['设备运行监测']}})
    with closing(store._connect()) as c:
        c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,'
                  'representative,status,updated_at) VALUES(?,?,?,?,?,?,?)',
                  (pid, placeholder, 'small-test', b'', 'file0.txt', 'pending', time.time()))
        for i, vector in enumerate(vectors):
            path = 'file%d.txt' % i
            digest = catalog.material(store.get_file(pid, path))[1]
            c.execute(semantic._MEMBER_UPSERT,
                      (pid, path, placeholder, digest,
                       np.asarray(vector, dtype=np.float32).tobytes()))
        c.commit()
    return placeholder


def members(package):
    store, pid = package
    with closing(store._connect()) as c:
        return [tuple(r) for r in c.execute(
            'SELECT path,group_id,input_hash,vector FROM large_catalog_members '
            'WHERE package_id=? ORDER BY path', (pid,))]


def state(package):
    store, pid = package
    with closing(store._connect()) as c:
        return [tuple(r) for r in c.execute(
            'SELECT * FROM large_catalog_km_state WHERE package_id=?', (pid,))]


@pytest.mark.parametrize('fast', ['0', '1'])
@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_small_packages_get_real_groups_preserving_content_and_hashes(package, monkeypatch, count, fast):
    monkeypatch.setenv('SJFX_LP_CATALOG_FAST_KMEANS', fast)
    store, pid = package
    placeholder = stage(package, [[1., 0.]] * count)
    before = members(package)
    files = store.get_files(pid, [row[0] for row in before], decorate=False)
    assert naming.validate_catalog(store, pid)['checks']['INV5_placeholders']['files_in_placeholder_groups'] == count

    assert semantic.kmeans_recluster(store, pid) == count
    after = members(package)
    assert [(r[0], r[2], r[3]) for r in after] == [(r[0], r[2], r[3]) for r in before]
    assert len({r[1] for r in after}) == 1
    assert all(not r[1].startswith(('km-pending-', 'km-other-', 'km-solo-')) for r in after)
    assert store.get_files(pid, [row[0] for row in after], decorate=False) == files
    with closing(store._connect()) as c:
        groups = list(c.execute('SELECT * FROM large_catalog_groups WHERE package_id=?', (pid,)))
        assert len(groups) == 1
        group = groups[0]
        assert group['status'] == 'queued'  # Normal naming pipeline, no synthetic label.
        assert group['member_count'] == count
        assert group['representative'] in {r[0] for r in after}
        np.testing.assert_array_equal(np.frombuffer(group['anchor'], dtype=np.float32), [1., 0.])
        assert not c.execute('SELECT 1 FROM large_catalog_groups WHERE package_id=? AND id=?', (pid, placeholder)).fetchone()
        assert not c.execute('SELECT 1 FROM lp_catalog_changes WHERE package_id=?', (pid,)).fetchone()
        saved = c.execute('SELECT signature,vectors FROM large_catalog_km_state WHERE package_id=?', (pid,)).fetchone()
        assert tuple(saved) == (semantic._km_signature(store, pid), count)
    # Grouping alone cannot bypass strict INV-5: naming and topic publication remain required.
    verdict = naming.validate_catalog(store, pid)
    assert any('INV-5' in v for v in verdict['violations'])
    saved_state = state(package)
    assert semantic.kmeans_recluster(store, pid) == 0
    assert members(package) == after and state(package) == saved_state
    assert semantic.kmeans_recluster(store, pid, force=True) == count
    assert members(package) == after


@pytest.mark.parametrize('fast', ['0', '1'])
@pytest.mark.parametrize('vectors,expected', [([[1., 0.], [0., 1.]], 2),
                                            ([[1., 0.], [1., 0.], [0., 1.]], 2)])
def test_small_sample_singletons_are_not_forced_into_an_unrelated_group(package, monkeypatch, vectors, expected, fast):
    monkeypatch.setenv('SJFX_LP_CATALOG_FAST_KMEANS', fast)
    store, pid = package
    stage(package, vectors)
    assert semantic.kmeans_recluster(store, pid) == len(vectors)
    after = members(package)
    assert len({r[1] for r in after}) == expected
    assert after[0][1] != after[-1][1]
    if len(vectors) == 3:
        assert after[0][1] == after[1][1]


@pytest.mark.parametrize('unlocked', [1, 2, 3])
def test_fewer_than_four_eligible_vectors_preserve_locked_members(package, unlocked):
    store, pid = package
    stage(package, [[1., 0.]] * unlocked + [[0., 1.]])
    locked_path = 'file%d.txt' % unlocked
    with closing(store._connect()) as c:
        c.execute('INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,updated_at) '
                  'VALUES(?,?,?,?,?,?)', (pid, 'manual', 'small-test', np.array([0., 1.], dtype=np.float32).tobytes(), locked_path, time.time()))
        c.execute('UPDATE large_catalog_members SET group_id=? WHERE package_id=? AND path=?', ('manual', pid, locked_path))
        revision = c.execute('SELECT content_revision FROM large_catalog_groups WHERE package_id=? AND id=?', (pid, 'manual')).fetchone()[0]
        c.execute('INSERT INTO lp_catalog_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  (pid, 'manual', revision, 1, '人工确认主题', '', '[]', 'manual', 'hash', 0, 1, time.time()))
        c.commit()
        locked_group = tuple(c.execute('SELECT * FROM large_catalog_groups WHERE package_id=? AND id=?', (pid, 'manual')).fetchone())
        locked_label = tuple(c.execute('SELECT * FROM lp_catalog_labels WHERE package_id=? AND group_id=?', (pid, 'manual')).fetchone())
    locked_member = members(package)[-1]
    assert semantic.kmeans_recluster(store, pid) == unlocked
    assert members(package)[-1] == locked_member
    assert all(not r[1].startswith('km-pending-') for r in members(package)[:-1])
    with closing(store._connect()) as c:
        assert tuple(c.execute('SELECT * FROM large_catalog_groups WHERE package_id=? AND id=?', (pid, 'manual')).fetchone()) == locked_group
        assert tuple(c.execute('SELECT * FROM lp_catalog_labels WHERE package_id=? AND group_id=?', (pid, 'manual')).fetchone()) == locked_label


@pytest.mark.parametrize('vectors', [[], [[]], [[0., 0.]], [[float('nan'), 1.]]])
def test_no_usable_vector_does_not_fabricate_groups_or_finalization(package, vectors):
    store, pid = package
    if vectors:
        stage(package, vectors)
    before = members(package)
    assert semantic.kmeans_recluster(store, pid) == 0
    assert members(package) == before
    assert not state(package)


def test_invalid_staged_member_is_not_deleted_when_valid_sibling_is_grouped(package):
    store, pid = package
    placeholder = stage(package, [[1., 0.], [0., 0.]])
    invalid = members(package)[1]
    assert semantic.kmeans_recluster(store, pid) == 1
    assert members(package)[1] == invalid
    with closing(store._connect()) as c:
        group = c.execute('SELECT member_count FROM large_catalog_groups WHERE package_id=? AND id=?', (pid, placeholder)).fetchone()
        assert group and group[0] == 1
    # The invalid sibling remains visibly unfinished; grouping cannot make INV-5 pass.
    assert any('INV-5' in v for v in naming.validate_catalog(store, pid)['violations'])


@pytest.mark.parametrize('status', ['paused', 'cancelled'])
@pytest.mark.parametrize('count', [1, 2, 3])
def test_small_package_control_gate_prevents_writes(package, count, status):
    store, pid = package
    stage(package, [[1., 0.]] * count)
    store.update_package(pid, status=status)
    before = members(package)
    assert semantic.kmeans_recluster(store, pid, force=True) == 0
    assert members(package) == before
    assert not state(package)


def test_disabled_kmeans_does_not_mutate_staging(package, monkeypatch):
    store, pid = package
    stage(package, [[1., 0.], [0., 1.]])
    monkeypatch.setattr(Config, 'LP_CATALOG_KMODE', False)
    before = members(package)
    assert semantic.kmeans_recluster(store, pid, force=True) == 0
    assert members(package) == before
    assert not state(package)


@pytest.mark.parametrize('count', [1, 2, 3])
def test_real_embedding_naming_and_tree_pipeline_can_publish_a_small_package(package, monkeypatch, count):
    from services import large_package_catalog_tree as tree

    class Embeddings:
        model = 'small-test'
        dimension = 2

        def embed(self, texts):
            return [[1., 0.] for _ in texts]

    class Naming:
        model = 'small-naming-test'
        supports_json_response_format = True

        def chat(self, system, user, **kwargs):
            payload = json.loads(user)
            return {'content': json.dumps({
                'name': '设备运行监测与异常处置',
                'description': '记录设备运行状态、监测参数和异常处置结果。',
                'evidence_ids': [payload['evidence'][0]['id']],
                'needs_review': False}, ensure_ascii=False),
                'usage': {'total_tokens': 100}, 'finish_reason': 'stop'}

    store, pid = package
    monkeypatch.setattr(Config, 'LP_CATALOG_MODEL_NAMING', True)
    # This local model double declares the same 32K budget as the batch tests;
    # no tokenizer is installed, so token_count uses conservative UTF-8 bytes.
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 32768)
    monkeypatch.setattr(Config, 'LP_CATALOG_INPUT_TOKENS', 16000)
    monkeypatch.setattr(Config, 'EMBED_TOKENIZER_PATH', '')
    client = Embeddings()
    monkeypatch.setattr(semantic, 'embedding_client', lambda: client)
    for i in range(count):
        path = 'source%d.txt' % i
        store.upsert_file(pid, dict(path=path, name=path, size=100, modified_at_ns=1))
        store.update_file(pid, path, quick_status='completed', local_summary={
            'sample': '设备运行监测记录状态参数和异常处置结果。' * 5,
            'portrait': {'headings': ['设备运行监测']}})
    assert semantic.embed_batch(store, pid, client) == count
    assert len(members(package)) == count
    assert all(r[1].startswith('km-pending-') for r in members(package))
    assert semantic.material_pending_count(store, pid) == 0
    assert semantic.kmeans_recluster(store, pid) == count
    store.update_package(pid, status='waiting_for_selection', phase='catalog_ready')
    model = Naming()
    assert naming.process_one(store, pid, model_client=model)
    with closing(store._connect()) as c:
        assert c.execute('SELECT 1 FROM lp_catalog_labels WHERE package_id=?', (pid,)).fetchone(), [tuple(r) for r in c.execute('SELECT status,error FROM lp_catalog_jobs WHERE package_id=?', (pid,))]
    assert tree.build(store, pid, naming.ensure_round(store, pid), model_client=model)
    verdict = naming.validate_catalog(store, pid)
    assert verdict['checks']['INV5_placeholders']['files_in_placeholder_groups'] == 0, verdict
    assert verdict['ok'], verdict
    assert len(members(package)) == count
    assert all(not r[1].startswith(('km-pending-', 'km-other-', 'km-solo-')) for r in members(package))
