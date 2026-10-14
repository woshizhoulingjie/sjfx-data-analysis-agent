"""Real route-handler/Storage regressions for the restored import selection gate.

Uses the existing test_request_context adapter, without ASGI middleware.
No lifespan or workers start. The protection engine is explicitly disabled in
this fixture: these cases verify durable routing, queue and owner semantics,
not enabled-protection admission or metadata provenance.
"""
from types import SimpleNamespace

import pytest

import app as web
from services.scanner import scan_directory
from services.storage import Storage
from sjfx_protection import business


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / 'synthetic-source'
    root.mkdir()
    (root / 'pending.txt').write_text('Synthetic pending document.', encoding='utf-8')
    storage = Storage(tmp_path / 'state.db')
    monkeypatch.setattr(web, 'storage', storage)
    monkeypatch.setattr(business, '_configured', SimpleNamespace(enabled=False))
    monkeypatch.setattr(web.Config, 'SCAN_ALLOWED_ROOTS', (root,))
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'resume-owner')
    monkeypatch.setattr(web.Config, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(web.Config, 'API_ACCESS_TOKEN', 'synthetic-resume-test')
    monkeypatch.setattr(web.Config, 'API_TOKEN_EXPIRES_AT', None)
    monkeypatch.setattr(web._file_protection, 'guard_root', lambda _root: None)
    monkeypatch.setattr(web._multiuser_runtime, 'get_current_user', lambda: None)
    identity = [{'owner_id': 'resume-owner', 'role': 'user'}]
    monkeypatch.setattr(web, '_multiuser_session_from_request', lambda: identity[0])
    scan_id = storage.save_scan(scan_directory(root), scan_id='resume-scan', owner_id='resume-owner')
    storage.create_import_task(scan_id, str(root), owner_id='resume-owner')
    storage.transition_import_task(scan_id, 'scanning')
    storage.transition_import_task(scan_id, 'waiting_for_selection')
    storage.save_scan_selection(scan_id, [], [], status='draft')
    yield SimpleNamespace(storage=storage, scan_id=scan_id, identity=identity)


def jobs(case):
    with case.storage._connect() as conn:
        return [dict(row) for row in conn.execute(
            'SELECT id,status,scan_id,owner_id FROM analysis_jobs ORDER BY id')]


def call_route(handler, identifier, payload=None):
    # Existing route tests use this exact compatibility request context. It
    # executes the registered product handler, not an HTTP/ASGI test client.
    with web.app.test_request_context('/api/test', method='POST', json={} if payload is None else payload):
        response = handler(identifier)
    if isinstance(response, tuple):
        response, status = response
    else:
        status = response.status_code
    return SimpleNamespace(status_code=status, json=response.get_json,
                           text=response.body.decode('utf-8'))


def post_control(case, action, payload=None):
    handler = web.pause_package_processing if action == 'pause' else web.resume_package_processing
    return call_route(handler, case.scan_id, payload)


@pytest.mark.parametrize('payload', [{}, {'mode': 'continue'}])
def test_paused_draft_restores_waiting_selection_without_enqueuing(package, payload):
    case = package
    assert jobs(case) == []
    pause = post_control(case, 'pause')
    assert pause.status_code == 200, pause.text
    paused = case.storage.get_import_task(case.scan_id)
    assert paused['status'] == 'paused'
    assert paused['checkpoint']['paused_from_state'] == 'waiting_for_selection'
    response = post_control(case, 'resume', payload)
    assert response.status_code == 409, response.text
    assert response.json()['accepted'] is False
    assert 'job_id' not in response.json()
    assert response.json()['processing']['state'] == 'awaiting_selection'
    task = case.storage.get_import_task(case.scan_id)
    assert task['status'] == task['current_phase'] == 'waiting_for_selection'
    assert 'paused_from_state' not in task['checkpoint']
    assert case.storage.get_package_processing_control(case.scan_id)['pause_requested'] is False
    selection = case.storage.get_scan_selection(case.scan_id)
    assert selection['status'] == 'draft' and selection['included_paths'] == []
    assert jobs(case) == []


def test_unpaused_draft_still_requires_selection(package):
    response = post_control(package, 'resume')
    assert response.status_code == 409, response.text
    assert response.json()['accepted'] is False
    assert jobs(package) == []
    assert package.storage.get_import_task(package.scan_id)['status'] == 'waiting_for_selection'


def test_legacy_draft_gate_precedes_import_task_migration(package):
    case = package
    with case.storage._connect() as conn:
        conn.execute('DELETE FROM import_tasks WHERE scan_id=?', (case.scan_id,))
    response = post_control(case, 'resume')
    assert response.status_code == 409, response.text
    assert response.json()['accepted'] is False
    assert jobs(case) == []


def test_confirmed_pending_job_can_pause_resume_and_cancel(package):
    case = package
    confirmation = call_route(web.confirm_scan_selection, case.scan_id,
                              {'included_paths': ['pending.txt'], 'version': 1})
    assert confirmation.status_code == 202, confirmation.text
    confirmed_job = confirmation.json()['job_id']
    assert case.storage.get_job(confirmed_job)['status'] == 'queued'
    assert case.storage.get_scan_selection(case.scan_id)['status'] == 'confirmed'
    pause = post_control(case, 'pause')
    assert pause.status_code == 200, pause.text
    assert case.storage.get_job(confirmed_job)['status'] == 'cancelled'
    assert case.storage.get_import_task(case.scan_id)['checkpoint']['paused_from_state'] == 'parsing_selected'
    response = post_control(case, 'resume')
    assert response.status_code == 202, response.text
    assert response.json()['accepted'] is True
    resumed_job = response.json()['job_id']
    assert resumed_job != confirmed_job
    job = case.storage.get_job(resumed_job)
    assert job['status'] == 'queued' and job['owner_id'] == 'resume-owner'
    assert job['options']['target_paths'] == ['pending.txt']
    assert case.storage.get_import_task(case.scan_id)['status'] == 'parsing_selected'
    cancelled = call_route(web.cancel_job, resumed_job)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()['cancelled'] is True
    assert case.storage.get_job(resumed_job)['status'] == 'cancelled'
    assert case.storage.get_package_processing_control(case.scan_id)['state'] == 'paused'


def test_explicit_selection_resume_retains_existing_semantics(package):
    response = post_control(package, 'resume', {'mode': 'selection', 'target_paths': ['pending.txt']})
    assert response.status_code == 202, response.text
    assert response.json()['accepted'] is True
    job = package.storage.get_job(response.json()['job_id'])
    assert job['status'] == 'queued'
    assert job['options']['target_paths'] == ['pending.txt']
    assert job['options']['workflow_source'] == 'manual_selection'


@pytest.mark.parametrize('action,denial_status', [('pause', 404), ('resume', 400)])
def test_foreign_owner_cannot_change_package_controls(package, action, denial_status):
    case = package
    assert post_control(case, 'pause').status_code == 200
    before_task = case.storage.get_import_task(case.scan_id)
    before_control = case.storage.get_package_processing_control(case.scan_id)
    case.identity[0] = {'owner_id': 'foreign-owner', 'role': 'user'}
    response = post_control(case, action)
    assert response.status_code == denial_status, response.text
    assert case.storage.get_import_task(case.scan_id) == before_task
    assert case.storage.get_package_processing_control(case.scan_id) == before_control
    assert jobs(case) == []


def test_foreign_owner_cannot_cancel_confirmed_job(package):
    case = package
    confirmation = call_route(web.confirm_scan_selection, case.scan_id,
                              {'included_paths': ['pending.txt'], 'version': 1})
    assert confirmation.status_code == 202, confirmation.text
    job_id = confirmation.json()['job_id']
    case.identity[0] = {'owner_id': 'foreign-owner', 'role': 'user'}
    response = call_route(web.cancel_job, job_id)
    assert response.status_code == 404, response.text
    assert case.storage.get_job(job_id)['status'] == 'queued'
