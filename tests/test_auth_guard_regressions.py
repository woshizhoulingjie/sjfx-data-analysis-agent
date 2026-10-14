"""BUG-08/09: synthetic owner and cookie/token combinations at real guards."""
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as web
from web_compat import SJFXFastAPI, _normalise_response, jsonify


def identity_context(session, *, auth_required=True, expired=False):
    stack = ExitStack()
    for target, name, value in (
        (web.Config, 'AUTH_REQUIRED', auth_required),
        (web.Config, 'API_ACCESS_TOKEN', 'synthetic-deployment-token'),
        (web.Config, 'API_TOKEN_EXPIRES_AT', time.time() - 10 if expired else None),
        (web.Config, 'OWNER_ID', 'primary'),
        (web, '_MULTIUSER_AVAILABLE', True),
        (web, '_multi_user_store', Mock()),
    ):
        stack.enter_context(patch.object(target, name, value))
    stack.enter_context(patch.object(web._multiuser_runtime, 'get_current_user', return_value=None))
    stack.enter_context(patch.object(web, '_multiuser_session_from_request', return_value=session))
    return stack


def direct_response(path, handler, token='synthetic-deployment-token'):
    """Also exercise request-scoped guards without a platform thread bridge."""
    with web.app.test_request_context(path, headers={'X-SJFX-Token': token}):
        try:
            blocked = web._access_guard()
            response = _normalise_response(handler() if blocked is None else blocked)
        except HTTPException as exc:
            response = jsonify({'detail': exc.detail})
            response.status_code = exc.status_code
    return SimpleNamespace(status_code=response.status_code, text=response.body.decode('utf-8'), json=response.get_json)


@pytest.mark.parametrize('transport', ['direct', 'http'])
@pytest.mark.parametrize('auth_required', [False, True])
@pytest.mark.parametrize('large', [False, True])
@pytest.mark.parametrize('session, target_owner, expected', [
    ({'owner_id': 'alice', 'role': 'user'}, 'alice', 200),
    ({'owner_id': 'alice', 'role': 'user'}, 'bob', 404),
    ({'owner_id': 'alice', 'role': 'user', 'viewing_owner': 'bob'}, 'bob', 404),
    ({'owner_id': 'admin', 'role': 'admin', 'viewing_owner': 'bob'}, 'bob', 200),
    ({'owner_id': 'admin', 'role': 'admin', 'viewing_owner': 'bob'}, 'alice', 404),
    ({'owner_id': 'admin', 'role': 'admin'}, 'alice', 200),
    (None, 'primary', 200),
    (None, 'bob', 404),
    ({}, 'primary', 503),
])
def test_translation_owner_checks_survive_auth_switch(transport, auth_required, large, session, target_owner, expected):
    scan_id = 'lp-synthetic' if large else 'synthetic-scan'
    source = Mock()
    source.scan_owned.side_effect = lambda _id, owner_id: owner_id is None or owner_id == target_owner
    source.list_translation_page.return_value = {'items': [{'source_path': 'synthetic-private.txt'}], 'total': 1}
    source.translation_counts.return_value = {'completed': 1}
    packages = Mock()
    packages.get.side_effect = lambda _id, owner_id: {'owner_id': target_owner} if owner_id is None or owner_id == target_owner else None
    with identity_context(session, auth_required=auth_required), \
         patch.object(web, 'storage', source), patch.object(web, 'large_package_store', packages), \
         patch.object(web, '_large_translation_page', return_value=source.list_translation_page.return_value) as large_page:
        path = '/api/translations/' + scan_id
        response = direct_response(path, lambda: web.list_translations(scan_id)) if transport == 'direct' else \
            TestClient(web.app).get(path, headers={'X-SJFX-Token': 'synthetic-deployment-token'})
    assert response.status_code == expected, response.text
    reader = large_page if large else source.list_translation_page
    if expected == 200:
        reader.assert_called_once()
        assert response.json()['items'][0]['source_path'] == 'synthetic-private.txt'
    else:
        reader.assert_not_called()
        source.translation_counts.assert_not_called()
        assert 'synthetic-private.txt' not in response.text


@pytest.mark.parametrize('transport', ['direct', 'http'])
@pytest.mark.parametrize('session, token, expired, expected', [
    (None, '', False, 401),
    (None, 'synthetic-deployment-token', False, 200),
    (None, 'synthetic-deployment-token', True, 401),
    (None, 'invalid-token', False, 401),
    ({'owner_id': 'alice', 'role': 'user'}, '', True, 200),
    ({'owner_id': 'alice', 'role': 'user'}, 'invalid-token', True, 200),
    ({'owner_id': 'alice', 'role': 'user'}, 'synthetic-deployment-token', True, 200),
    ({'owner_id': 'alice', 'role': 'user'}, '', False, 200),
    ({}, '', True, 503),
    ({'role': 'user'}, 'synthetic-deployment-token', False, 503),
])
def test_expired_legacy_token_does_not_reject_cookie_identity(transport, session, token, expired, expected):
    probe = SJFXFastAPI()
    probe.before_request(web._access_guard)

    @probe.route('/api/guard-probe')
    def guarded_owner():
        return jsonify({'owner': web._request_owner_id()})

    with identity_context(session, expired=expired):
        if transport == 'direct':
            response = direct_response('/api/guard-probe', guarded_owner, token)
        else:
            response = TestClient(probe).get('/api/guard-probe', headers={'X-SJFX-Token': token})
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()['owner'] == ('alice' if session else 'primary')
