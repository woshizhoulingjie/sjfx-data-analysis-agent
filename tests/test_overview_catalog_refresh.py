"""Published overviews must follow asynchronous catalog changes, without polling writes."""
from contextlib import closing
import json
import time

import pytest

from config import Config
from large_package_worker import LargePackageWorker
from services.large_package_isolated import LargePackageStore
from services import large_package_reporting as reports


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', ())
    monkeypatch.setattr(Config, 'LP_DIRECTIONS_ENABLED', False)
    monkeypatch.setattr(Config, 'LP_OVERVIEW_EAGER', True)
    store = LargePackageStore(tmp_path / 'state.db')
    pid = store.create(tmp_path)['id']
    store.update_package(pid, status='waiting_for_selection', phase='catalog_ready')
    with closing(store._connect()) as conn:
        conn.execute(
            "INSERT INTO large_catalog_groups(package_id,id,model,anchor,representative,"
            "result,status,updated_at,content_revision,member_count) "
            "VALUES(?,'group-one','test',zeroblob(0),'','{}','named',?,1,0)",
            (pid, time.time()))
        conn.execute(
            "INSERT INTO lp_catalog_labels(package_id,group_id,revision,version,name,description,"
            "evidence,model,digest,needs_review,locked,updated) "
            "VALUES(?,'group-one',1,1,'旧设备维护','有原文支持的设备维护记录','[]','test','digest',0,0,?)",
            (pid, time.time()))
        conn.commit()
    worker = LargePackageWorker(store=store, poll_seconds=0.2)
    return store, pid, worker


def publish(scenario, milestone='catalog'):
    store, pid, _ = scenario
    reports.build_overview(store, store.get(pid), milestone=milestone)
    result = reports.published_report(store, store.get(pid))
    assert result
    return result


def rename(scenario):
    store, pid, _ = scenario
    with closing(store._connect()) as conn:
        # Deliberately retain count/timestamp: aggregate-only signatures miss this.
        conn.execute("UPDATE lp_catalog_labels SET name='既有设备密封圈维护',version=version+1 WHERE package_id=?", (pid,))
        conn.commit()


def settle_overview(worker, pid):
    return worker._build_overview(pid, 'catalog') or worker._build_overview(pid, 'catalog')


def test_catalog_change_refreshes_published_report_and_keeps_v1(scenario):
    store, pid, worker = scenario
    first = publish(scenario)
    original = (reports.report_directory(store, pid) / 'overview-v1.json').read_bytes()
    rename(scenario)
    assert settle_overview(worker, pid)
    current = reports.published_report(store, store.get(pid))
    assert current['report_id'] != first['report_id']
    assert current['provenance']['catalog_id'] != first['provenance']['catalog_id']
    assert (reports.report_directory(store, pid) / 'overview-v1.json').read_bytes() == original
    assert not worker._build_overview(pid, 'catalog')


def test_idle_catchup_refreshes_existing_overview(scenario):
    store, pid, worker = scenario
    first = publish(scenario)
    rename(scenario)
    assert worker._overview_catchup() or worker._overview_catchup()
    assert reports.published_report(store, store.get(pid))['report_id'] != first['report_id']
    assert not worker._overview_catchup()


def test_catalog_refresh_never_downgrades_deep_milestone(scenario):
    store, pid, worker = scenario
    publish(scenario)
    first = publish(scenario, 'deep')
    rename(scenario)
    assert settle_overview(worker, pid)
    current = reports.published_report(store, store.get(pid))
    assert current['milestone'] == 'deep'
    assert current['version'] == 2
    assert current['report_id'] != first['report_id']


def test_report_cache_detects_rename_without_timestamp_change(scenario):
    store, pid, _ = scenario
    first = reports.build_report(store, store.get(pid))
    rename(scenario)
    assert reports.build_report(store, store.get(pid))['revision'] != first['revision']


def test_stale_report_cannot_bind_to_newer_catalog(scenario):
    store, pid, _ = scenario
    published = publish(scenario)
    old = reports.build_report(store, store.get(pid))
    rename(scenario)
    assert reports._publish_overview(store, store.get(pid), old, 'catalog') is None
    assert reports.published_report(store, store.get(pid))['report_id'] == published['report_id']


def test_legacy_report_without_input_revision_refreshes_once(scenario):
    store, pid, worker = scenario
    publish(scenario)
    with closing(store._connect()) as conn:
        filename = conn.execute("SELECT payload FROM lp_overview_versions WHERE package_id=? AND published=1", (pid,)).fetchone()[0]
    target = reports.report_directory(store, pid) / filename
    payload = json.loads(target.read_text())
    payload.pop('catalog_input_revision', None)
    target.write_text(json.dumps(payload))
    assert settle_overview(worker, pid)
    assert reports.published_report(store, store.get(pid)).get('catalog_input_revision')
    assert not worker._build_overview(pid, 'catalog')


def test_running_names_are_partial_and_refresh_after_drain(scenario):
    store, pid, worker = scenario
    worker._overview_stable = 0
    with closing(store._connect()) as conn:
        conn.execute("INSERT INTO lp_catalog_jobs(package_id,group_id,revision,status) VALUES(?,'group-one',1,'running')", (pid,))
        conn.commit()
    assert settle_overview(worker, pid)
    assert reports.published_report(store, store.get(pid))['partial'] is True
    with closing(store._connect()) as conn:
        conn.execute("UPDATE lp_catalog_jobs SET status='completed' WHERE package_id=?", (pid,))
        conn.commit()
    assert settle_overview(worker, pid)
    assert not reports.published_report(store, store.get(pid)).get('partial')


def test_invalid_new_catalog_does_not_borrow_previous_version(scenario):
    store, pid, _ = scenario
    first = publish(scenario)
    assert first['provenance']['catalog_id']
    with closing(store._connect()) as conn:
        conn.execute("DELETE FROM lp_catalog_labels WHERE package_id=?", (pid,))
        conn.commit()
    from services.large_package_catalog_naming import ensure_catalog_version
    # Existing callers retain their historical fallback contract.
    assert ensure_catalog_version(store, pid) == first['provenance']['catalog_id']
    current = publish(scenario)
    assert current['provenance']['catalog_id'] == ''


def test_unchanged_deep_overview_without_v1_does_not_loop(scenario):
    store, pid, worker = scenario
    store.update_package(pid, status='completed', phase='completed')
    first = publish(scenario, 'deep')
    assert not (reports.report_directory(store, pid) / 'overview-v1.json').exists()
    assert not worker._overview_catchup()
    assert not worker._overview_catchup()
    assert reports.published_report(store, store.get(pid))['report_id'] == first['report_id']
