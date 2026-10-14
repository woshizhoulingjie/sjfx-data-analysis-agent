"""Upload -> HTTP import -> durable worker inventory, using temporary stores."""
import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app as web
import worker
from services.storage import Storage
from sjfx_multiuser.upload_routes import register
from web_compat import SJFXFastAPI, has_request_context


@pytest.fixture
def flow(tmp_path, monkeypatch):
    data = tmp_path / 'state'
    output = data / 'outputs'
    nas = tmp_path / 'nas'
    nas.mkdir()
    storage = Storage(str(data / 'agent.db'))
    sessions = {
        'alice': {'owner_id': 'owner_a', 'role': 'user'},
        'bob': {'owner_id': 'owner_b', 'role': 'user'},
        'admin': {'owner_id': 'owner_admin', 'role': 'admin'},
        'admin_self': {'owner_id': 'owner_admin', 'role': 'admin',
                       'viewing_owner': 'owner_admin'},
        'view': {'owner_id': 'owner_admin', 'role': 'admin', 'viewing_owner': 'owner_b'},
    }
    users = Mock()
    users.resolve_session.side_effect = sessions.get
    monkeypatch.setattr(web, 'storage', storage)
    monkeypatch.setattr(web, '_multi_user_store', users)
    monkeypatch.setattr(web, '_MULTIUSER_AVAILABLE', True)
    monkeypatch.setattr(web._multiuser_runtime, 'get_current_user', lambda: None)
    monkeypatch.setattr(web.Config, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(web.Config, 'API_ACCESS_TOKEN', 'synthetic-flow-token')
    monkeypatch.setattr(web.Config, 'API_TOKEN_EXPIRES_AT', None)
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'primary')
    monkeypatch.setattr(web, '_configured_owner_id', 'primary')
    monkeypatch.setattr(web.Config, 'OUTPUT_DIR', output)
    monkeypatch.setattr(web.Config, 'DATA_DIR', data)
    monkeypatch.setattr(web.Config, 'SCAN_ALLOWED_ROOTS', (nas,))
    protection = Mock()
    monkeypatch.setattr(web, '_file_protection', protection)
    model = Mock(side_effect=AssertionError('import must not call a model'))
    monkeypatch.setattr(web, 'llm', model)
    probe = SJFXFastAPI(max_content_length=4 * 1024 * 1024)
    probe.before_request(web._access_guard)
    probe.route('/api/scan', methods=['POST'])(web.scan)
    register(probe, str(output / '_uploads'), allowed_roots=(nas,),
             session_resolver=web._multiuser_session_from_request,
             request_owner_id=web._request_owner_id)
    with TestClient(probe) as client:
        yield client, storage, protection, model, output, nas


def headers(identity):
    return {'Cookie': 'sjfx_session=' + identity}


def upload(client, identity='alice'):
    content = b'amount\n1\n3\n'
    created = client.post('/api/uploads', headers=headers(identity), json={
        'files': [{'rel_path': 'records.csv', 'size': len(content),
                   'sha256': hashlib.sha256(content).hexdigest()}]})
    assert created.status_code == 200, created.text
    session = created.json()['session_id']
    chunk = client.put('/api/uploads/' + session + '/chunks/0',
                       params={'chunk_index': 0, 'sha256': hashlib.sha256(content).hexdigest()},
                       headers={**headers(identity), 'Content-Type': 'application/octet-stream'},
                       content=content)
    assert chunk.status_code == 200, chunk.text
    finished = client.post('/api/uploads/' + session + '/finalize', headers=headers(identity), json={})
    assert finished.status_code == 200, finished.text
    body = finished.json()
    # The API emits the actual server path so the existing scan UI can use it.
    path = body['dest_dir']
    assert path, body
    assert (Path(path) / 'records.csv').read_bytes() == content
    return path


@pytest.mark.parametrize('identity,owner', [('alice', 'owner_a'), ('admin', 'owner_admin'),
                                          ('admin_self', 'owner_admin')])
def test_private_upload_is_imported_outside_nas_allowlist(flow, monkeypatch, identity, owner):
    client, storage, protection, model, _, _ = flow
    path = upload(client, identity)
    response = client.post('/api/scan', headers=headers(identity), json={'path': path})
    assert response.status_code == 202, response.text
    job_id = response.json()['job_id']
    queued = storage.get_job(job_id, owner_id=owner)
    assert queued and queued['owner_id'] == owner
    assert not has_request_context(), 'worker must use persistent job ownership'
    # A new connection reads the queued record; request-local identity is gone.
    durable = Storage(storage.db_path)
    monkeypatch.setattr(web, 'storage', durable)
    job = durable.claim_next_job('synthetic-worker')
    assert job['owner_id'] == owner
    result = worker.execute(job)
    assert result == {'scan_id': job_id, '_await_selection': True}
    scan = storage.get_scan(job_id, owner_id=owner)
    assert scan['inventory_complete'] and scan['file_count'] == 1
    assert storage.get_scan(job_id, owner_id='foreign') is None
    entries = list(storage.iter_inventory_entries(job_id, kind='file'))
    assert len(entries) == 1 and entries[0]['path'] == 'records.csv'
    protection.guard_root.assert_called_once_with(path)
    model.assert_not_called()
    assert model.mock_calls == []


def test_foreign_private_upload_is_denied_before_creating_job(flow):
    client, storage, _, _, _, _ = flow
    path = upload(client)
    for identity in ('bob', 'view'):
        response = client.post('/api/scan', headers=headers(identity), json={'path': path})
        assert response.status_code == 403, response.text
    assert storage.claim_next_job('synthetic-worker') is None


def test_worker_cannot_trust_owner_in_options_or_broad_allowlist(flow, monkeypatch):
    client, storage, _, _, output, nas = flow
    path = upload(client)
    monkeypatch.setattr(web.Config, 'SCAN_ALLOWED_ROOTS', (output, nas))
    job_id = storage.create_scan_job(path, 100, 'auto', 10, owner_id='owner_b')
    job = storage.claim_next_job('synthetic-worker')
    job['options']['owner_id'] = 'owner_a'
    with pytest.raises(ValueError):
        worker.execute(job)
    assert storage.get_scan(job_id, owner_id='owner_b') is None


def test_regular_allowed_directory_remains_importable(flow):
    client, storage, _, _, _, nas = flow
    (nas / 'document.txt').write_text('synthetic local material', encoding='utf-8')
    response = client.post('/api/scan', headers=headers('alice'), json={'path': str(nas)})
    assert response.status_code == 202, response.text
    job = storage.claim_next_job('synthetic-worker')
    worker.execute(job)
    assert storage.get_scan(job['id'], owner_id='owner_a')['file_count'] == 1


def test_protection_guard_still_blocks_private_upload_import(flow):
    client, storage, protection, _, _, _ = flow
    path = upload(client)
    protection.guard_root.side_effect = ValueError('synthetic protection denial')
    response = client.post('/api/scan', headers=headers('alice'), json={'path': path})
    assert response.status_code == 400
    assert storage.claim_next_job('synthetic-worker') is None


def test_allowed_parent_cannot_inventory_other_owners_private_uploads(flow, monkeypatch):
    client, storage, _, _, output, _ = flow
    foreign_path = upload(client, 'bob')
    monkeypatch.setattr(web.Config, 'SCAN_ALLOWED_ROOTS', (output,))
    response = client.post('/api/scan', headers=headers('alice'), json={'path': str(output)})
    assert response.status_code == 403, response.text
    assert storage.claim_next_job('synthetic-worker') is None
    # Persisted/pre-existing jobs must pass the same check outside HTTP.
    storage.create_scan_job(output, 100, 'auto', 10, owner_id='owner_a')
    job = storage.claim_next_job('synthetic-worker')
    with pytest.raises(ValueError):
        worker.execute(job)
    assert storage.get_scan(job['id'], owner_id='owner_a') is None
    assert list(storage.iter_inventory_entries(job['id'])) == []
    assert (Path(foreign_path) / 'records.csv').is_file()
