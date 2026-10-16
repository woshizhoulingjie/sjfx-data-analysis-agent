"""Durable and effective switches agree without starting product services."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from sjfx_protection import business, policy
from sjfx_protection.integration import Protection, install
from sjfx_protection.registration import PackageProtection


def _app(directory, monkeypatch, role='admin'):
    monkeypatch.setattr(business, '_configured', None)
    app = FastAPI()
    user = {'user_id': 'u1', 'owner_id': 'alice', 'role': role, 'must_change_pw': False}
    protection = install(app, state_dir=directory, allowed_roots=[], mode='enforce',
                         session_resolver=lambda sid: user if sid == 'valid-session' else None)
    # Only disposable fixture state; ASGITransport never starts app lifespan.
    protection.package._ensure()
    return app, protection


HEADERS = {'Cookie': 'sjfx_session=valid-session', 'Origin': 'http://test',
           'X-SJFX-Protection': '1'}


def test_default_runtime_switch_leaves_durable_off_and_allows_saved_on_after_restart(monkeypatch, tmp_path):
    monkeypatch.delenv('SJFX_DESKTOP_FILE_PROTECTION', raising=False)
    assert policy.file_protection_requested() is True
    assert policy.read_policy(tmp_path)['enabled'] is False
    app, protection = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(protection, 'ensure_executor', lambda: None)

    async def save_and_read():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            before = await client.get('/protection/api/package-policy', headers=HEADERS)
            assert before.status_code == 200
            assert before.json()['policy']['enabled'] is False
            assert before.json()['effective_enabled'] is False
            assert before.json()['runtime_disabled'] is False
            saved = await client.post('/protection/api/package-policy', headers=HEADERS,
                                      json={'enabled': True, 'reason': 'synthetic switch persistence'})
            assert saved.status_code == 200
            assert saved.json()['policy']['enabled'] is True
            assert saved.json()['effective_enabled'] is True
            assert saved.json()['runtime_disabled'] is False
            status = (await client.get('/protection/api/status', headers=HEADERS)).json()
            assert status['enabled'] is True and status['runtime_disabled'] is False
    asyncio.run(save_and_read())
    reopened = object.__new__(PackageProtection)
    reopened.store = SimpleNamespace(directory=tmp_path)
    assert reopened.enabled is True
    assert json.loads((tmp_path / 'package-policy.json').read_text())['enabled'] is True


def test_runtime_off_reports_saved_policy_without_overwriting_it(monkeypatch, tmp_path):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    path = tmp_path / 'package-policy.json'
    path.write_text('{"enabled":true,"revision":7}', encoding='utf-8')
    app, protection = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(protection, 'ensure_executor', lambda: None)

    async def save_and_read():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            original = path.read_bytes()
            response = await client.get('/protection/api/package-policy', headers=HEADERS)
            assert response.status_code == 200
            assert response.json()['policy']['enabled'] is True
            assert response.json()['effective_enabled'] is False
            assert response.json()['runtime_disabled'] is True
            assert path.read_bytes() == original
            saved = await client.post('/protection/api/package-policy', headers=HEADERS,
                                      json={'enabled': True, 'operation_policy': {'delete': 'warn'},
                                            'reason': 'synthetic unrelated policy edit'})
            assert saved.status_code == 200
            assert saved.json()['policy']['enabled'] is True
            assert saved.json()['effective_enabled'] is False
            legacy = await client.post('/protection/api/policy', headers=HEADERS,
                                       json={'enabled': True, 'reason': 'synthetic effective switch'})
            assert legacy.status_code == 200
            assert legacy.json()['policy']['enabled'] is True
            assert legacy.json()['enforced'] is False
            status = (await client.get('/protection/api/status', headers=HEADERS)).json()
            assert status['enabled'] is False and status['runtime_disabled'] is True
            assert status['mode'] == 'off' and status['configured_mode'] == 'enforce'
    asyncio.run(save_and_read())
    assert json.loads(path.read_text())['enabled'] is True
    monkeypatch.delenv('SJFX_DESKTOP_FILE_PROTECTION')
    assert policy.protection_enabled(tmp_path) is True


@pytest.mark.parametrize('role', ['user', 'admin'])
def test_status_requires_login_and_switch_requires_admin_and_write_marker(monkeypatch, tmp_path, role):
    monkeypatch.delenv('SJFX_DESKTOP_FILE_PROTECTION', raising=False)
    app, protection = _app(tmp_path, monkeypatch, role)
    monkeypatch.setattr(protection, 'ensure_executor', lambda: None)

    async def check_boundaries():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/protection/api/status')).status_code == 401
            assert (await client.get('/protection/api/status', headers=HEADERS)).status_code == 200
            missing_marker = {k: v for k, v in HEADERS.items() if k != 'X-SJFX-Protection'}
            assert (await client.post('/protection/api/policy', headers=missing_marker,
                                      json={'enabled': True})).status_code == 403
            saved = await client.post('/protection/api/policy', headers=HEADERS,
                                      json={'enabled': True, 'reason': 'synthetic permission boundary'})
            assert saved.status_code == (200 if role == 'admin' else 403)
            if role == 'admin':
                assert saved.json()['enforced'] is True
    asyncio.run(check_boundaries())


def test_runtime_off_never_starts_watcher_even_with_old_agent_token(monkeypatch, tmp_path):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', '0')
    monkeypatch.setenv('SJFX_LOCAL_AGENT_TOKEN_FILE', str(tmp_path / 'old-token'))
    protection = Protection(tmp_path, [], lambda _: None, mode='enforce')
    protection.ensure_executor()
    assert protection._watch_proc is None and protection._store is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('value', ['', 'true', 'false', '2', ' 0'])
def test_invalid_runtime_switch_rejects_ambiguous_values(monkeypatch, tmp_path, value):
    monkeypatch.setenv('SJFX_DESKTOP_FILE_PROTECTION', value)
    with pytest.raises(RuntimeError, match='exactly 0 or 1'):
        policy.protection_enabled(tmp_path)
