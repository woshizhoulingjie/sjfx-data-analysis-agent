"""Controller readiness keeps API authentication while protection is enabled."""
import ast
import hmac
import json
import time
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from sjfx_protection.integration import Protection, ProtectionMiddleware
from web_compat import SJFXFastAPI, jsonify, request


TOKEN = 'synthetic-controller-readiness-token'


@pytest.fixture
def readiness(tmp_path):
    """Use the real guards without importing app's models or runtime storage."""
    sessions = {
        'browser-session': {'owner_id': 'alice', 'user_id': 'u1', 'role': 'user'},
    }
    config = SimpleNamespace(AUTH_REQUIRED=True, API_ACCESS_TOKEN=TOKEN,
                             API_TOKEN_EXPIRES_AT=None)
    owner = Mock(return_value='primary')

    def session_from_request():
        cookie = SimpleCookie()
        cookie.load(request.headers.get('Cookie', ''))
        return sessions.get(cookie['sjfx_session'].value) if 'sjfx_session' in cookie else None

    # Execute the application authentication functions themselves; importing the
    # complete application would initialize unrelated model and parser objects.
    source = Path(__file__).resolve().parents[1] / 'app.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    names = {'_api_token_expired', '_access_guard'}
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == names
    for node in functions:
        node.decorator_list = []
    namespace = {'Config': config, 'time': time, 'hmac': hmac, 'request': request,
                 'jsonify': jsonify, '_MULTIUSER_AVAILABLE': True,
                 '_multiuser_session_from_request': session_from_request,
                 '_request_owner_id': owner}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)

    app = SJFXFastAPI()
    app.before_request(namespace['_access_guard'])
    handler = Mock(return_value={'ok': True})
    for path in ('/api/status', '/api/status/', '/api/status/future',
                 '/api/uploads/probe', '/api/scan', '/api/health',
                 '/api/file-capabilities', '/api/file-capabilities/future'):
        app.route(path, methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'])(handler)

    state = tmp_path / 'protection'
    state.mkdir()
    (state / 'package-policy.json').write_text(json.dumps({'enabled': True}), encoding='utf-8')
    protection = Protection(state, [tmp_path], sessions.get, mode='enforce',
                            resource_resolver=lambda *_args: [])
    protection.ensure_executor = Mock()
    protection._store = Mock()
    app.add_middleware(ProtectionMiddleware, protection=protection)
    return SimpleNamespace(client=TestClient(app), config=config, handler=handler,
                           owner=owner, protection=protection)


def test_controller_token_reaches_application_status_guard(readiness):
    response = readiness.client.get('/api/status', headers={'X-SJFX-Token': TOKEN})
    assert response.status_code == 200, response.text
    assert response.json() == {'ok': True}
    readiness.owner.assert_called_once()
    readiness.handler.assert_called_once()


def test_authenticated_capability_inventory_needs_no_registered_material(readiness):
    response = readiness.client.get('/api/file-capabilities', headers={'Cookie': 'sjfx_session=browser-session'})
    assert response.status_code == 200
    assert response.json() == {'ok': True}


def test_capability_inventory_keeps_authentication_and_exact_get_scope(readiness):
    assert readiness.client.get('/api/file-capabilities').status_code == 401
    headers = {'Cookie': 'sjfx_session=browser-session'}
    for method, path in [('POST', '/api/file-capabilities'), ('GET', '/api/file-capabilities/future')]:
        response = readiness.client.request(method, path, headers=headers)
        assert response.status_code == 423
        assert response.json()['code'] == 'protection_target_required'
    readiness.handler.assert_not_called()


@pytest.mark.parametrize('token,expired', [('', False), ('wrong-token', False), (TOKEN, True)])
def test_status_still_rejects_missing_wrong_and_expired_token(readiness, token, expired):
    if expired:
        readiness.config.API_TOKEN_EXPIRES_AT = time.time() - 10
    headers = {'X-SJFX-Token': token} if token else {}
    response = readiness.client.get('/api/status', headers=headers)
    assert response.status_code == 401
    # The rejection comes from the existing API guard, not protection login.
    assert 'code' not in response.json()
    assert ('已过期' if expired else '未授权') in response.json()['error']
    readiness.handler.assert_not_called()


@pytest.mark.parametrize('expired', [False, True])
def test_browser_session_status_remains_authenticated(readiness, expired):
    if expired:
        readiness.config.API_TOKEN_EXPIRES_AT = time.time() - 10
    response = readiness.client.get('/api/status', headers={'Cookie': 'sjfx_session=browser-session'})
    assert response.status_code == 200, response.text
    readiness.owner.assert_called_once()
    readiness.handler.assert_called_once()


@pytest.mark.parametrize('method,path', [
    ('HEAD', '/api/status'), ('POST', '/api/status'), ('PUT', '/api/status'),
    ('PATCH', '/api/status'), ('DELETE', '/api/status'),
    ('GET', '/api/status/'), ('GET', '/api/status/future'),
    ('GET', '/api/health'), ('POST', '/api/scan'), ('GET', '/api/uploads/probe'),
])
def test_only_exact_get_status_skips_protection_session(readiness, method, path):
    response = readiness.client.request(method, path, headers={'X-SJFX-Token': TOKEN})
    assert response.status_code == 401
    if method != 'HEAD':
        assert response.json()['code'] == 'login_required'
    readiness.owner.assert_not_called()
    readiness.handler.assert_not_called()


def test_other_browser_control_path_keeps_protection_session(readiness):
    response = readiness.client.get('/api/uploads/probe',
                                    headers={'Cookie': 'sjfx_session=browser-session'})
    assert response.status_code == 200, response.text
    readiness.protection.ensure_executor.assert_called_once()
    readiness.handler.assert_called_once()


def test_other_browser_business_operation_still_requires_protection_admission(readiness):
    response = readiness.client.post('/api/scan', json={},
                                     headers={'Cookie': 'sjfx_session=browser-session'})
    assert response.status_code == 423
    assert response.json()['code'] == 'protection_target_required'
    readiness.handler.assert_not_called()
