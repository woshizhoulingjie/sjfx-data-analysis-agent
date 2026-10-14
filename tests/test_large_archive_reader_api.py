"""Real HTTP reader contracts using temporary archive/index/store fixtures."""
import gc
import os
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_large_qa_archive_sources import archive_project, indexed_archive, write_archive, BODY


@pytest.fixture
def reader_api(archive_project, monkeypatch):
    import app as web
    case = indexed_archive(archive_project)
    monkeypatch.setattr(web, 'large_package_store', case.store)
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'archive-owner')
    monkeypatch.setattr(web.Config, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(web.Config, 'API_ACCESS_TOKEN', 'reader-test-only')
    monkeypatch.setattr(web.Config, 'API_TOKEN_EXPIRES_AT', None)
    monkeypatch.setattr(web.Config, 'LARGE_QA_ENABLED', True)
    monkeypatch.setattr(web.Config, 'LARGE_QA_INDEX_DIR', case.base)
    client = TestClient(web.app)  # no lifespan: never start production workers
    try:
        yield web, case, client
    finally:
        client.close()
        gc.collect()


def open_document(case, client, path=None, authenticated=True):
    return client.get('/api/document/' + case.package['id'],
                      params={'path': path or case.path, 'full': '1'},
                      headers={'X-SJFX-Token': 'reader-test-only'} if authenticated else {})


def test_http_member_and_ordinary_original_reader(reader_api):
    _, case, client = reader_api
    response = open_document(case, client)
    assert response.status_code == 200, response.text
    doc = response.json()['document']
    assert doc['text_content'] == BODY
    assert doc['source']['physical_path'] == case.container.name
    assert doc['source']['archive_member'] == 'orders/receipt.txt'
    ordinary = open_document(case, client, 'ordinary.txt')
    assert ordinary.status_code == 200
    assert 'Ordinary aurora delivery' in ordinary.json()['document']['text_content']


def test_http_auth_owner_and_sensitive_member(reader_api, monkeypatch):
    web, case, client = reader_api
    assert open_document(case, client, authenticated=False).status_code == 401
    assert open_document(case, client, case.container.name + '::.env').status_code == 400
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'foreign-owner')
    foreign = open_document(case, client)
    assert foreign.status_code == 400
    assert 'eight pumps' not in foreign.text


@pytest.mark.parametrize('change', ['delete', 'replace', 'marked_changed'])
def test_http_changed_container_cannot_fall_back_to_old_preview(reader_api, change):
    _, case, client = reader_api
    case.store.update_file(case.package['id'], case.path,
                           local_summary={'sample': 'stale synthetic preview'})
    if change == 'marked_changed':
        with closing(case.store._connect()) as connection:
            connection.execute('UPDATE files SET source_changed=1 WHERE package_id=? AND path=?',
                               (case.package['id'], case.path))
            connection.commit()
    elif change == 'delete':
        case.container.unlink()
    else:
        before = case.container.stat()
        replacement = case.root / 'replacement.zip'
        write_archive(replacement, {'orders/receipt.txt': BODY.replace('eight', 'three'),
                                   'notes/other.txt': 'Unrelated sibling metadata.',
                                   '.env': 'synthetic-private-secret'})
        assert replacement.stat().st_size == before.st_size
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, case.container)
    response = open_document(case, client)
    assert response.status_code == 400, response.text
    assert response.json()['ok'] is False
    assert 'stale synthetic preview' not in response.text
    assert 'eight pumps' not in response.text
