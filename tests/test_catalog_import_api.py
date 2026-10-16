"""HTTP contract for catalog handoff; isolated stores, no model or lifespan."""
import gc
import json
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
import test_large_catalog_import as fixtures


@pytest.fixture
def api(monkeypatch):
    import app as web
    case = fixtures.LargeCatalogImportTests()
    case.setUp()
    try:
        monkeypatch.setattr(web, 'storage', case.ordinary)
        monkeypatch.setattr(web, 'large_package_store', case.large)
        monkeypatch.setattr(web.Config, 'OWNER_ID', 'o1')
        monkeypatch.setattr(web.Config, 'AUTH_REQUIRED', True)
        monkeypatch.setattr(web.Config, 'API_ACCESS_TOKEN', 'catalog-test-only')
        monkeypatch.setattr(web.Config, 'API_TOKEN_EXPIRES_AT', None)
        # Deliberately no TestClient context manager: do not run app lifespan.
        client = TestClient(web.app)
        yield web, case, client
        client.close()
    finally:
        # Large-store connections held by sqlite's internal cycles must be
        # collected before Windows can remove the temporary database files.
        gc.collect()
        case.tearDown()


def submit(client, key='first', mode='accurate', **extra):
    return client.post('/api/large-packages/%s/catalog/import' % fixtures.PACKAGE,
                       headers={'X-SJFX-Token': 'catalog-test-only'},
                       json={'name': 'HTTP验收', 'node_ids': [fixtures.GROUP_A],
                             'request_key': key, 'parse_mode': mode, **extra})


def test_http_create_retry_and_mode_conflict(api):
    _, case, client = api
    first = submit(client, mode='fast')
    assert first.status_code == 202
    assert first.json()['accepted'] and not first.json()['reused']
    assert first.json()['selected_file_count'] == 2
    retry = submit(client, mode='fast')
    assert retry.status_code == 200 and retry.json()['reused']
    assert retry.json()['job_id'] == first.json()['job_id']
    assert submit(client, mode='accurate').status_code == 409
    accurate = submit(client, key='accurate', mode='accurate')
    assert accurate.status_code == 202
    assert accurate.json()['job_id'] != first.json()['job_id']
    assert case._count('SELECT COUNT(*) FROM analysis_jobs') == 2


def test_http_auth_and_package_ownership(api, monkeypatch):
    web, case, client = api
    url = '/api/large-packages/%s/catalog/import' % fixtures.PACKAGE
    assert client.post(url, json={}).status_code == 401
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'other-owner')
    denied = submit(client)
    # This route's existing ValueError contract is 400, not 404. Preserve it
    # while ensuring missing and foreign packages reveal the same response.
    assert denied.status_code == 400
    missing = client.post('/api/large-packages/does-not-exist/catalog/import',
                          headers={'X-SJFX-Token': 'catalog-test-only'}, json={})
    assert missing.status_code == denied.status_code
    assert missing.json() == denied.json()
    assert case._count('SELECT COUNT(*) FROM analysis_jobs') == 0


def test_http_source_unavailable_returns_503_without_partial_job(api, monkeypatch):
    _, case, client = api
    original = case.large.export_selection
    def fail_connect():
        raise sqlite3.OperationalError('injected source store failure')
    def export_then_disconnect(*args, **kwargs):
        selected = original(*args, **kwargs)
        monkeypatch.setattr(case.large, '_connect', fail_connect)
        return selected
    monkeypatch.setattr(case.large, 'export_selection', export_then_disconnect)
    response = submit(client)
    assert response.status_code == 503
    assert response.json()['ok'] is False
    assert case._count('SELECT COUNT(*) FROM analysis_jobs') == 0
    assert case._count('SELECT COUNT(*) FROM scans') == 0


def test_http_changed_source_does_not_reuse_original_job(api):
    _, case, client = api
    first = submit(client)
    assert first.status_code == 202
    with closing(case.large._connect()) as conn:
        conn.execute('UPDATE files SET sha256=? WHERE package_id=?',
                     ('a' * 64, fixtures.PACKAGE))
        conn.commit()
    assert submit(client).status_code == 409
    replacement = submit(client, key='new-source')
    assert replacement.status_code == 202
    assert replacement.json()['job_id'] != first.json()['job_id']


def test_http_excluded_path_is_not_a_worker_target(api):
    _, case, client = api
    excluded = fixtures.FILES[0][0]
    response = submit(client, exclude_paths=[excluded])
    assert response.status_code == 202
    assert response.json()['selected_file_count'] == 1
    with case.ordinary._connect() as conn:
        options = conn.execute('SELECT options FROM analysis_jobs WHERE id=?',
                               (response.json()['job_id'],)).fetchone()[0]
    assert json.loads(options)['target_paths'] == [fixtures.FILES[1][0]]


@pytest.mark.parametrize('payload', [
    ['not-an-object'],
    {'exclude_paths': 'unselected.txt'},
    {'exclude_node_ids': fixtures.GROUP_B},
    {'exclude_paths': {'path': fixtures.FILES[0][0]}},
    {'exclude_node_ids': 1},
])
def test_http_malformed_selection_is_rejected_without_creating_job(api, payload):
    _, case, client = api
    if isinstance(payload, dict):
        response = submit(client, **payload)
    else:
        response = client.post('/api/large-packages/%s/catalog/import' % fixtures.PACKAGE,
                               headers={'X-SJFX-Token': 'catalog-test-only'}, json=payload)
    assert response.status_code == 400
    assert case._count('SELECT COUNT(*) FROM scans') == 0
    assert case._count('SELECT COUNT(*) FROM analysis_jobs') == 0


@pytest.mark.parametrize('selection,status', [
    ({'node_ids': [], 'paths': []}, 400),
    ({'node_ids': [], 'paths': ['missing.txt']}, 409),
    ({'node_ids': ['missing-node']}, 404),
    ({'paths': ['../outside.txt'], 'node_ids': []}, 400),
    ({'node_ids': ['preview:unfinished']}, 409),
    ({'exclude_paths': [path for path, group in fixtures.FILES if group == fixtures.GROUP_A]}, 400),
])
def test_http_empty_missing_or_unavailable_selection_never_creates_job(api, selection, status):
    _, case, client = api
    response = submit(client, **selection)
    assert response.status_code == status
    assert case._count('SELECT COUNT(*) FROM scans') == 0
    assert case._count('SELECT COUNT(*) FROM analysis_jobs') == 0


def test_http_mixed_exclusions_share_the_server_limit(api, monkeypatch):
    _, case, client = api
    from services import large_package_catalog_import as bridge
    monkeypatch.setattr(bridge, 'MAX_EXCLUSIONS', 1)
    response = submit(client, exclude_paths=[fixtures.FILES[0][0]],
                      exclude_node_ids=[fixtures.GROUP_B])
    assert response.status_code == 413
    assert case._count('SELECT COUNT(*) FROM scans') == 0
    assert case._count('SELECT COUNT(*) FROM analysis_jobs') == 0


def test_http_stale_excluded_node_requires_refresh_without_expanding_selection(api):
    _, case, client = api
    response = submit(client, node_ids=[fixtures.GROUP_A, fixtures.GROUP_B],
                      exclude_node_ids=['removed-child-node'])
    assert response.status_code == 404
    for table in ('scans', 'analysis_jobs', 'import_tasks'):
        assert case._count('SELECT COUNT(*) FROM ' + table) == 0
    # The route rejects before the bridge's lazy audit-table initialization.
    assert case._count("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                       "AND name='large_catalog_imports'") == 0


@pytest.mark.parametrize('selection,expected', [
    ({'node_ids': [], 'paths': [fixtures.FILES[2][0], fixtures.FILES[0][0], fixtures.FILES[2][0]]},
     [fixtures.FILES[2][0], fixtures.FILES[0][0]]),
    ({'node_ids': [fixtures.GROUP_A, fixtures.GROUP_B], 'exclude_node_ids': [fixtures.GROUP_B],
      'exclude_paths': [fixtures.FILES[0][0]]}, [fixtures.FILES[1][0]]),
    ({'node_ids': [fixtures.GROUP_A, fixtures.GROUP_A], 'paths': [fixtures.FILES[0][0]]},
     [fixtures.FILES[0][0], fixtures.FILES[1][0]]),
])
def test_http_selection_remains_exact_through_ordinary_handoff(api, selection, expected):
    _, case, client = api
    first = submit(client, **selection)
    assert first.status_code == 202
    retry = submit(client, **selection)
    assert retry.status_code == 200 and retry.json()['reused']
    assert retry.json()['job_id'] == first.json()['job_id']
    assert first.json()['selected_file_count'] == len(expected)
    scan_id, job_id = first.json()['scan_id'], first.json()['job_id']
    with case.ordinary._connect() as conn:
        scan = conn.execute('SELECT root_path,owner_id,payload FROM scans WHERE id=?', (scan_id,)).fetchone()
        inventory = [row[0] for row in conn.execute(
            "SELECT node_path FROM inventory_entries WHERE scan_id=? AND kind='file'", (scan_id,))]
        selection_row = conn.execute('SELECT included_paths FROM scan_selections WHERE scan_id=?', (scan_id,)).fetchone()
        task = conn.execute('SELECT owner_id,checkpoint FROM import_tasks WHERE scan_id=?', (scan_id,)).fetchone()
        job = conn.execute('SELECT task_type,owner_id,options FROM analysis_jobs WHERE id=?', (job_id,)).fetchone()
    options = json.loads(job['options'])
    assert set(inventory) == set(json.loads(selection_row[0])) == set(expected)
    assert set(json.loads(scan['payload'])['selected_paths']) == set(expected)
    assert set(json.loads(task['checkpoint'])['selected_paths']) == set(expected)
    assert set(options['target_paths']) == set(expected)
    assert scan['owner_id'] == task['owner_id'] == job['owner_id'] == 'o1'
    assert scan['root_path'] == '/tmp/lpimport-src'
    assert job['task_type'] == 'analyze_package'
    assert options['workflow_source'] == 'selected_file_parse'
    assert options['continue_full'] is False and options['candidate_preview'] is False
    assert options['deep_batch_id'] is None
    assert case._count('SELECT COUNT(*) FROM analysis_jobs') == 1
    with closing(case.large._connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM queue WHERE stage=\'deep\'').fetchone()[0] == 0
