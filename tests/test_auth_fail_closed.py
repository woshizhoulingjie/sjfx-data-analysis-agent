"""Identity failures must never select the legacy owner's material."""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as web
from sjfx_multiuser.ownership import resolve_owner
from web_compat import SJFXFastAPI, jsonify


def test_missing_user_owner_never_falls_back():
    for role in ('user', 'admin', 'unknown'):
        with pytest.raises(ValueError):
            resolve_owner({'role': role}, None, 'primary')
    with pytest.raises(ValueError):
        resolve_owner({}, None, 'primary')


def test_legacy_token_owner_and_admin_views_remain_supported():
    assert resolve_owner(None, None, 'primary').owner_id == 'primary'
    assert resolve_owner({'role': 'user', 'owner_id': 'a'}, 'b', 'primary').owner_id == 'a'
    assert resolve_owner({'role': 'admin', 'owner_id': 'admin'}, None, 'primary').owner_id is None
    assert resolve_owner({'role': 'admin', 'owner_id': 'admin'}, 'a', 'primary').owner_id == 'a'


def probe_client():
    probe = SJFXFastAPI()

    @probe.route('/api/owner')
    def owner():
        return jsonify({'owner': web._request_owner_id()})

    @probe.route('/api/effective-owner')
    def effective_owner():
        return jsonify({'owner': web._effective_owner()})

    return TestClient(probe)


def test_owner_resolution_exception_returns_503_without_reading_default():
    with patch.object(web, '_MULTIUSER_AVAILABLE', True), patch.object(web, '_multi_user_store', Mock()), \
         patch.object(web._multiuser_runtime, 'get_current_user', return_value=None), \
         patch.object(web, '_multiuser_session_from_request', return_value={'owner_id': 'a', 'role': 'user'}), \
         patch.object(web, '_resolve_owner', side_effect=OSError('identity failure')):
        response = probe_client().get('/api/owner')
    assert response.status_code == 503
    assert 'primary' not in response.text


def test_missing_owner_and_unexpected_all_users_decision_are_rejected():
    with patch.object(web, '_MULTIUSER_AVAILABLE', True), patch.object(web, '_multi_user_store', Mock()), \
         patch.object(web._multiuser_runtime, 'get_current_user', return_value=None):
        with patch.object(web, '_multiuser_session_from_request', return_value={'role': 'user'}):
            assert probe_client().get('/api/owner').status_code == 503
        with patch.object(web, '_multiuser_session_from_request', return_value={'owner_id': 'a', 'role': 'user'}), \
             patch.object(web, '_resolve_owner', return_value=Mock(owner_id=None)):
            assert probe_client().get('/api/owner').status_code == 503


def test_unavailable_identity_store_does_not_select_default_owner():
    with patch.object(web, '_MULTIUSER_AVAILABLE', False), patch.object(web, '_multi_user_store', None):
        assert probe_client().get('/api/owner').status_code == 503


def test_cookie_store_failure_is_503_not_anonymous_owner():
    store = Mock()
    store.resolve_session.side_effect = OSError('database temporarily unavailable')
    with patch.object(web, '_MULTIUSER_AVAILABLE', True), patch.object(web, '_multi_user_store', store), \
         patch.object(web._multiuser_runtime, 'get_current_user', return_value=None):
        response = probe_client().get('/api/owner', headers={'Cookie': 'sjfx_session=synthetic-test-session'})
    assert response.status_code == 503


def test_effective_owner_propagates_identity_failure():
    with patch.object(web, '_request_owner_id', side_effect=HTTPException(503, 'identity unavailable')):
        assert probe_client().get('/api/effective-owner').status_code == 503


def test_empty_runtime_session_is_not_replaced_by_cookie():
    with patch.object(web, '_MULTIUSER_AVAILABLE', True), patch.object(web, '_multi_user_store', Mock()), \
         patch.object(web._multiuser_runtime, 'get_current_user', return_value={}), \
         patch.object(web, '_multiuser_session_from_request', return_value=None):
        assert probe_client().get('/api/owner').status_code == 503


@pytest.mark.parametrize('owner', [None, 'foreign-owner'])
def test_legacy_token_rejects_unexpected_owner(owner):
    with patch.object(web, '_MULTIUSER_AVAILABLE', True), patch.object(web, '_multi_user_store', Mock()), \
         patch.object(web._multiuser_runtime, 'get_current_user', return_value=None), \
         patch.object(web, '_multiuser_session_from_request', return_value=None), \
         patch.object(web, '_resolve_owner', return_value=Mock(owner_id=owner)):
        assert probe_client().get('/api/owner').status_code == 503


def test_real_history_route_does_not_read_default_owner_on_store_failure():
    store = Mock()
    store.resolve_session.side_effect = OSError('identity database unavailable')
    with patch.object(web.Config, 'AUTH_REQUIRED', True), \
         patch.object(web.Config, 'API_ACCESS_TOKEN', 'synthetic-test-token'), \
         patch.object(web.Config, 'API_TOKEN_EXPIRES_AT', None), \
         patch.object(web, '_MULTIUSER_AVAILABLE', True), patch.object(web, '_multi_user_store', store), \
         patch.object(web._multiuser_runtime, 'get_current_user', return_value=None), \
         patch.object(web, 'large_package_store', Mock()) as packages:
        response = TestClient(web.app).get('/api/large-packages/history',
            headers={'X-SJFX-Token': 'synthetic-test-token', 'Cookie': 'sjfx_session=synthetic'})
        assert response.status_code == 503
        packages.list_history.assert_not_called()


def test_user_store_initialization_failure_stops_startup():
    store = Mock()
    store.initialize.side_effect = OSError('initialization failed')
    with patch.object(web, 'ensure_runtime_directories'), patch.object(web, 'storage', Mock()), \
         patch.object(web, 'large_package_store', Mock()), patch.object(web, '_MULTIUSER_AVAILABLE', True), \
         patch.object(web, '_multi_user_store', store):
        with pytest.raises(RuntimeError, match='拒绝'):
            web._initialize_runtime_state()


def test_admin_bootstrap_failure_stops_startup():
    store = Mock()
    store.bootstrap_first_admin.side_effect = OSError('bootstrap failed')
    with patch.object(web, '_MULTIUSER_AVAILABLE', True), patch.object(web, '_multi_user_store', store):
        with pytest.raises(RuntimeError, match='拒绝'):
            web._bootstrap_multiuser_admin()


def test_identity_module_import_failure_cannot_start_single_user(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root), SJFX_STATE_DIR=str(tmp_path / 'state'),
               SJFX_PARSE_TEMP_DIR=str(tmp_path / 'parse'), HOST='127.0.0.1',
               SJFX_EXTRA_BIND_HOSTS='', AUTH_REQUIRED='0', ENABLE_TRANSLATION='0', ENABLE_IMPORT_TRANSLATION='0')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c',
                             "import sys; sys.modules['sjfx_multiuser.users']=None; import app"],
                            cwd=root, env=env, text=True, encoding='utf-8', capture_output=True, timeout=90)
    assert result.returncode != 0
    assert '拒绝退回默认归属' in result.stderr
    assert not (tmp_path / 'state').exists()
