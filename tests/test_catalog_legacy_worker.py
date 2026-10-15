"""The worker must publish a small legacy catalog without bypassing its gates."""
from contextlib import closing
import json
import time

import numpy as np
import pytest

from config import Config
from large_package_worker import LargePackageWorker
from services import large_package_catalog as catalog
from services import large_package_catalog_batch as batch
from services import large_package_catalog_naming as naming
from services import large_package_reporting as reports
from services import large_package_semantic as semantic
from services.large_package_isolated import LargePackageStore


class Model:
    model = 'legacy-worker-test'
    supports_json_response_format = True
    names = ['设备运行监测', '财务预算执行', '合同履约核验']

    def __init__(self):
        self.calls = 0

    def chat(self, system, user, **kwargs):
        self.calls += 1
        payload = json.loads(user)
        groups = payload.get('groups', [payload])
        results = []
        for group in groups:
            evidence = group['evidence'][0]
            index = next(i for i, name in enumerate(self.names) if name in evidence['text'])
            value = {
                'name': self.names[index],
                'description': self.names[index] + '的原始记录、核验步骤与结果。',
                'evidence_ids': [evidence['id']], 'needs_review': False,
            }
            if 'groups' in payload:
                value['id'] = group['id']
            results.append(value)
        return {
            'content': json.dumps({'groups': results} if 'groups' in payload else results[0],
                                  ensure_ascii=False),
            'usage': {'total_tokens': 321}, 'finish_reason': 'stop',
        }


def make_case(tmp_path, monkeypatch, leaves, topic_merge=False):
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', ())
    monkeypatch.setattr(Config, 'LP_CATALOG_MODEL_NAMING', True)
    monkeypatch.setattr(Config, 'LP_CATALOG_KMODE', False)
    monkeypatch.setattr(Config, 'LP_CATALOG_VALIDATE_FIX', False)
    monkeypatch.setattr(Config, 'LP_TOPIC_MERGE', topic_merge)
    monkeypatch.setattr(Config, 'LP_DIRECTIONS_ENABLED', False)
    monkeypatch.setattr(Config, 'LP_OVERVIEW_EAGER', True)
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 32768)
    monkeypatch.setattr(Config, 'LP_CATALOG_INPUT_TOKENS', 16000)
    monkeypatch.setattr(Config, 'LP_CATALOG_BATCH_INPUT_TOKENS', 16000)
    monkeypatch.setattr(Config, 'LP_CATALOG_BATCH_SIZE', 4)
    monkeypatch.setattr(Config, 'EMBED_TOKENIZER_PATH', '')
    monkeypatch.setenv('SJFX_LP_TOPIC_DEPENDENCY_PUBLICATION', '0')
    # A bad call from inside leaf_batch must fail immediately, rather than
    # turning this scheduling regression into a 45-second self-drain timeout.
    monkeypatch.setenv('SJFX_LP_TOPIC_DRAIN_SECONDS', '0')
    store = LargePackageStore(tmp_path / 'state.db')
    pid = store.create(tmp_path)['id']
    model = Model()
    monkeypatch.setattr(naming, 'client', lambda: model)
    for index in range(leaves):
        for source in range(2):
            path = 'group%d-source%d.txt' % (index, source)
            sample = (model.names[index] + '的记录包含来源、执行步骤、核验结果。') * 8
            store.upsert_file(pid, {'path': path, 'name': path, 'size': len(sample)})
            store.update_file(pid, path, quick_status='completed',
                              local_summary={'sample': sample, 'portrait': {'headings': [model.names[index]]}})
            digest = catalog.material(store.get_file(pid, path))[1]
            semantic._group(store, pid, path, digest, np.eye(3, dtype=np.float32)[index], 'test-embed')
    semantic.name_batch(store, pid)
    store.update_package(pid, status='waiting_for_selection', phase='catalog_ready', progress=70)
    worker = LargePackageWorker(store)
    # Only shorten the overview's initial observation interval. Its real
    # validation, construction and transactional publication all still run.
    worker._overview_max_wait = 0
    return store, pid, worker, model


def run_slice(worker, pid):
    assert worker._submit_naming(pid)
    worker._naming_future.result(timeout=60)


def snapshot(store, pid):
    with closing(store._connect()) as conn:
        members = [tuple(row) for row in conn.execute(
            'SELECT path,group_id,input_hash,vector FROM large_catalog_members '
            'WHERE package_id=? ORDER BY path', (pid,))]
        round_rows = [tuple(row) for row in conn.execute(
            'SELECT id,status,hierarchy_status,requests,tokens,deadline,max_requests,max_tokens '
            'FROM lp_catalog_rounds WHERE package_id=? ORDER BY started', (pid,))]
        branches = [dict(row) for row in conn.execute(
            'SELECT id,children,revisions FROM lp_catalog_branches WHERE package_id=? ORDER BY id', (pid,))]
        versions = [tuple(row) for row in conn.execute(
            'SELECT report_id,catalog_id,status,published FROM lp_overview_versions '
            'WHERE package_id=? ORDER BY report_id', (pid,))]
    return members, round_rows, branches, versions


@pytest.mark.parametrize('topic_merge', [False, True], ids=['default-legacy', 'topic-merge'])
@pytest.mark.parametrize('leaves', [1, 2, 3])
def test_worker_registers_live_leaves_and_publishes_v1_without_manual_tree_build(
        tmp_path, monkeypatch, leaves, topic_merge):
    store, pid, worker, model = make_case(tmp_path, monkeypatch, leaves, topic_merge)
    before = snapshot(store, pid)[0]
    try:
        run_slice(worker, pid)
        members, rounds, branches, versions = snapshot(store, pid)
        assert members == before
        assert len({row[1] for row in members}) == leaves
        assert len(branches) == leaves, {'branches': branches, 'rounds': rounds}
        assert all(row['id'].startswith('independent:') for row in branches)
        assert {gid for row in branches for gid in json.loads(row['children'])} == {row[1] for row in members}
        with closing(store._connect()) as conn:
            revisions = dict(conn.execute('SELECT id,content_revision FROM large_catalog_groups WHERE package_id=?', (pid,)))
            for branch in branches:
                assert json.loads(branch['revisions']) == {
                    gid: revisions[gid] for gid in json.loads(branch['children'])}
            labels = conn.execute('SELECT model FROM lp_catalog_labels WHERE package_id=?', (pid,)).fetchall()
            assert len(labels) == leaves and all(row[0] == model.model for row in labels)
            assert conn.execute('SELECT COUNT(*) FROM lp_catalog_tree WHERE package_id=?', (pid,)).fetchone()[0] == 1
        verdict = naming.validate_catalog(store, pid)
        assert verdict['checks']['INV5_placeholders']['files_in_placeholder_groups'] == 0, verdict
        assert not verdict['blocking'], verdict
        assert len(rounds) == 1 and rounds[0][1:3] == ('completed', 'completed'), rounds
        published = reports.published_report(store, store.get(pid))
        assert published and published['milestone'] == 'catalog' and published['version'] == 1
        assert published['provenance']['catalog_id']
        assert len(versions) == 1 and versions[0][2:] == ('published', 1)
        stable = snapshot(store, pid)
        calls = model.calls
        run_slice(worker, pid)
        assert snapshot(store, pid) == stable
        assert model.calls == calls  # No renewed round, repeated tree or duplicate report.
    finally:
        if worker._naming_executor:
            worker._naming_executor.shutdown(wait=True)


@pytest.mark.parametrize('gate', [
    'paused', 'cancelled', 'expired', 'request_limit', 'token_limit', 'completed', 'retry_pending',
])
def test_legacy_worker_does_not_reopen_or_spend_a_blocked_round(tmp_path, monkeypatch, gate):
    store, pid, worker, model = make_case(tmp_path, monkeypatch, 1)
    assert batch.process_batch(store, pid, model_client=model)
    if gate in {'paused', 'cancelled'}:
        store.update_package(pid, status=gate)
    else:
        with closing(store._connect()) as conn:
            if gate == 'expired':
                conn.execute('UPDATE lp_catalog_rounds SET deadline=? WHERE package_id=?', (time.time()-1, pid))
            elif gate == 'request_limit':
                conn.execute('UPDATE lp_catalog_rounds SET requests=max_requests WHERE package_id=?', (pid,))
            elif gate == 'token_limit':
                conn.execute('UPDATE lp_catalog_rounds SET tokens=max_tokens WHERE package_id=?', (pid,))
            elif gate == 'completed':
                conn.execute("UPDATE lp_catalog_rounds SET status='completed' WHERE package_id=?", (pid,))
            else:
                conn.execute("UPDATE lp_catalog_jobs SET status='deferred',next_at=? WHERE package_id=?",
                             (time.time()+120, pid))
            conn.commit()
    before = snapshot(store, pid)
    calls = model.calls
    assert not worker._merge_topics_once(pid)
    assert snapshot(store, pid) == before
    assert not snapshot(store, pid)[2]
    assert model.calls == calls


def test_hierarchy_pending_matches_the_tree_live_label_inputs(tmp_path, monkeypatch):
    from services.large_package_catalog_hierarchy import needed

    store, pid, _, model = make_case(tmp_path, monkeypatch, 1)
    assert batch.process_batch(store, pid, model_client=model)
    with closing(store._connect()) as conn:
        assert needed(conn, pid)
        conn.execute('UPDATE large_catalog_groups SET content_revision=content_revision+1 WHERE package_id=?', (pid,))
        assert not needed(conn, pid)  # A stale unlocked label cannot keep a round alive.
        conn.execute('UPDATE lp_catalog_labels SET locked=1 WHERE package_id=?', (pid,))
        assert needed(conn, pid)
        conn.execute("UPDATE lp_catalog_labels SET name='' WHERE package_id=?", (pid,))
        assert not needed(conn, pid)
        conn.execute("UPDATE lp_catalog_labels SET name='设备运行监测' WHERE package_id=?", (pid,))
        conn.execute('DELETE FROM large_catalog_members WHERE package_id=?', (pid,))
        assert not needed(conn, pid)  # The cached count/label cannot invent live files.
