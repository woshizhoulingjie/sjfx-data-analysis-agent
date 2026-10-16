"""A late initial leaf must not leave the first legacy directory frozen forever."""
from contextlib import closing
import json
import time

import numpy as np
import pytest

import test_catalog_legacy_worker as fixtures
from services import large_package_catalog as catalog
from services import large_package_catalog_batch as batch
from services import large_package_catalog_naming as naming
from services import large_package_reporting as reports
from services import large_package_semantic as semantic


@pytest.fixture
def initial_case(tmp_path, monkeypatch):
    store, pid, worker, model = fixtures.make_case(tmp_path, monkeypatch, 1)
    try:
        fixtures.run_slice(worker, pid)
        first = reports.published_report(store, store.get(pid))
        assert first and not naming.state(store, pid)['active']
        yield store, pid, worker, model, first
    finally:
        if worker._naming_executor:
            worker._naming_executor.shutdown(wait=True)


def add_late_named_leaf(case):
    store, pid, _, model, _ = case
    for index in range(2):
        path = 'late-finance-source%d.txt' % index
        sample = (model.names[1] + '的记录包含来源、执行步骤、核验结果。') * 8
        store.upsert_file(pid, {'path': path, 'name': path, 'size': len(sample)})
        store.update_file(pid, path, quick_status='completed',
                          local_summary={'sample': sample, 'portrait': {'headings': [model.names[1]]}})
        digest = catalog.material(store.get_file(pid, path))[1]
        semantic._group(store, pid, path, digest, np.eye(3, dtype=np.float32)[1], 'test-embed')
    semantic.name_batch(store, pid)
    assert batch.process_batch(store, pid, model_client=model)
    with closing(store._connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM lp_catalog_labels WHERE package_id=?', (pid,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM lp_catalog_jobs WHERE package_id=? AND status='completed'",
                            (pid,)).fetchone()[0] == 2


def round_budget(store, pid):
    with closing(store._connect()) as conn:
        return tuple(conn.execute(
            'SELECT id,started,deadline,requests,max_requests,tokens,max_tokens FROM lp_catalog_rounds '
            'WHERE package_id=? ORDER BY started DESC LIMIT 1', (pid,)).fetchone())


def rejected_events(store, pid):
    with closing(store._connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM lp_workflow_events WHERE package_id=? "
                            "AND object='catalog' AND new_state='rejected'", (pid,)).fetchone()[0]


@pytest.mark.parametrize('persisted_status', ['active', 'completed'])
def test_later_initial_leaf_rebuilds_with_the_remaining_round_budget(initial_case, persisted_status):
    store, pid, worker, model, first = initial_case
    add_late_named_leaf(initial_case)
    with closing(store._connect()) as conn:
        conn.execute('UPDATE lp_catalog_rounds SET status=? WHERE package_id=?', (persisted_status, pid))
        conn.commit()
    before = fixtures.snapshot(store, pid)
    budget, calls = round_budget(store, pid), model.calls
    # Reads may expose the unfinished work but must not reopen a row themselves.
    assert naming.state(store, pid)['active']
    assert naming.naming_work_pending(store, pid)
    assert fixtures.snapshot(store, pid) == before
    assert any(value.startswith('INV-8') for value in naming.validate_catalog(store, pid)['violations'])
    fixtures.run_slice(worker, pid)
    assert round_budget(store, pid) == budget
    assert model.calls == calls  # Two unrelated leaves need no invented parent.
    assert naming.validate_catalog(store, pid)['ok']
    assert not naming.state(store, pid)['active']
    current = reports.published_report(store, store.get(pid))
    assert current['report_id'] != first['report_id']
    with closing(store._connect()) as conn:
        assert conn.execute('SELECT leaf_count FROM lp_catalog_versions WHERE catalog_id=?',
                            (current['provenance']['catalog_id'],)).fetchone()[0] == 2
    assert rejected_events(store, pid) == 0
    stable = fixtures.snapshot(store, pid)
    fixtures.run_slice(worker, pid)
    assert fixtures.snapshot(store, pid) == stable
    assert model.calls == calls


def test_later_label_version_also_rebuilds_the_saved_snapshot(initial_case):
    store, pid, worker, model, first = initial_case
    with closing(store._connect()) as conn:
        conn.execute("UPDATE lp_catalog_labels SET name=name||'记录',version=version+1 WHERE package_id=?", (pid,))
        conn.commit()
    budget, calls = round_budget(store, pid), model.calls
    # The independent catch-up path must handle an already completed round too.
    worker._topic_catchup_at = 0
    assert worker._topic_catchup()
    assert round_budget(store, pid) == budget
    assert model.calls == calls
    assert naming.validate_catalog(store, pid)['ok']
    assert worker._build_overview(pid, 'catalog')
    assert reports.published_report(store, store.get(pid))['report_id'] != first['report_id']
    assert rejected_events(store, pid) == 0


@pytest.mark.parametrize('gate', ['expired', 'request_limit', 'token_limit', 'paused', 'cancelled'])
@pytest.mark.parametrize('hierarchy_status', ['completed', 'queued'])
def test_late_snapshot_cannot_renew_or_bypass_a_blocked_round(initial_case, gate, hierarchy_status):
    store, pid, worker, model, _ = initial_case
    add_late_named_leaf(initial_case)
    with closing(store._connect()) as conn:
        conn.execute("UPDATE lp_catalog_rounds SET status='completed',hierarchy_status=? WHERE package_id=?",
                     (hierarchy_status, pid))
        if gate == 'expired':
            conn.execute('UPDATE lp_catalog_rounds SET deadline=? WHERE package_id=?', (time.time()-1, pid))
        elif gate == 'request_limit':
            conn.execute('UPDATE lp_catalog_rounds SET requests=max_requests WHERE package_id=?', (pid,))
        elif gate == 'token_limit':
            conn.execute('UPDATE lp_catalog_rounds SET tokens=max_tokens WHERE package_id=?', (pid,))
        conn.commit()
    if gate in {'paused', 'cancelled'}:
        store.update_package(pid, status=gate)
    before, budget, calls = fixtures.snapshot(store, pid), round_budget(store, pid), model.calls
    assert not naming.state(store, pid)['active']
    assert not naming.naming_work_pending(store, pid)
    assert not worker._merge_topics_once(pid)
    assert not batch.process_batch(store, pid, model_client=model)
    assert fixtures.snapshot(store, pid) == before
    assert round_budget(store, pid) == budget
    assert model.calls == calls


def test_sound_old_snapshot_defers_both_overview_publication_gates(initial_case, monkeypatch):
    store, pid, _, _, first = initial_case
    add_late_named_leaf(initial_case)
    monkeypatch.setattr(reports, 'build_report', lambda *args, **kwargs: pytest.fail('report prepared before tree catch-up'))
    blocked = reports.build_overview(store, store.get(pid))
    assert not blocked['published'] and not blocked['catalog_rejected']
    assert blocked['publication_blocked']['reason'] == 'catalog_snapshot_changed'
    with closing(store._connect()) as conn:
        report = {'catalog_input_revision': reports.catalog_input_revision(conn, pid)}
    rejection = {}
    assert reports._publish_overview(store, store.get(pid), report, 'catalog', rejection) is None
    assert rejection['violations'] == ['catalog_snapshot_changed']
    assert reports.published_report(store, store.get(pid))['report_id'] == first['report_id']
    assert rejected_events(store, pid) == 0
    # Deferral never certifies an outdated tree as the current directory.
    assert any(value.startswith('INV-8') for value in naming.validate_catalog(store, pid)['violations'])


@pytest.mark.parametrize('damage', ['branches', 'saved_dependencies'])
def test_damaged_old_partition_remains_a_real_inv8_rejection(initial_case, monkeypatch, damage):
    store, pid, _, _, _ = initial_case
    add_late_named_leaf(initial_case)
    with closing(store._connect()) as conn:
        if damage == 'branches':
            conn.execute('DELETE FROM lp_catalog_branches WHERE package_id=?', (pid,))
        else:
            payload = json.loads(conn.execute('SELECT payload FROM lp_catalog_tree WHERE package_id=?', (pid,)).fetchone()[0])
            payload['names'][next(iter(payload['names']))] += '损坏'
            conn.execute('UPDATE lp_catalog_tree SET payload=? WHERE package_id=?', (json.dumps(payload), pid))
        conn.commit()
        assert not naming._legacy_topic_snapshot_pending(conn, pid)
    monkeypatch.setattr(reports, 'build_report', lambda *args, **kwargs: pytest.fail('damaged directory prepared a report'))
    blocked = reports.build_overview(store, store.get(pid))
    assert blocked['catalog_rejected']
    assert blocked['publication_blocked']['reason'] == 'catalog_validation'
    assert any(value.startswith('INV-8') for value in blocked['rejection']['violations'])
    assert rejected_events(store, pid) == 1
