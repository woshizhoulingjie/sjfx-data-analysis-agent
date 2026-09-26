import asyncio
import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from sjfx_protection.access import ADMIN
from sjfx_protection.integration import install
from sjfx_protection.store import digest, uid

HEADERS = {'X-SJFX-Protection': '1'}


@pytest.fixture
def env(tmp_path):
    users = {
        'user': {'owner_id': 'alice', 'user_id': 'u1', 'role': 'user', 'viewing_owner': 'admin-space'},
        'admin': {'owner_id': 'admin-space', 'user_id': 'a1', 'role': 'admin'},
        'view': {'owner_id': 'admin-space', 'user_id': 'a1', 'role': 'admin', 'viewing_owner': 'alice'},
        'missing-role': {'owner_id': 'alice', 'user_id': 'u1'},
    }
    app = FastAPI()
    p = install(app, state_dir=tmp_path/'state', allowed_roots=[tmp_path],
                session_resolver=users.get, mode='audit')
    p.store.event('alice', 'secret.monitor.record', payload={'path': '/private/server/secret.txt'})
    p.store.event('admin-space', 'admin.only.record')
    cid = p.store.create_collection('alice', '/private/server/folder')
    with p.store.db(True) as db:
        db.execute("UPDATE collections SET state='ready' WHERE id=?", (cid,))
    return app, p, cid


def client(app, session='user'):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                             cookies={'sjfx_session': session}, headers=HEADERS)


@pytest.mark.parametrize('path', list(ADMIN) + ['/protection/api/future-telemetry'])
@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('session', ['user', 'missing-role'])
def test_regular_users_cannot_access_management(env, path, method, session):
    async def run():
        async with client(env[0], session) as c:
            r = await c.request(method, path+'?role=admin&owner=admin-space',
                                json={'role': 'admin', 'owner': 'admin-space'},
                                headers={'X-SJFX-Role': 'admin'})
            assert r.status_code == 403
            assert r.json()['code'] == 'admin_required'
            assert '/private/' not in r.text
    asyncio.run(run())


def test_pages_and_safe_payloads(env):
    async def run():
        async with client(env[0]) as c:
            page = await c.get('/protection')
            assert page.status_code == 200
            assert '你需要授权什么' in page.text
            for technical in ['traceDetail', '本地文件与处置', '处置回执', '服务器上的资料目录', "api('events", "api('actions"]:
                assert technical not in page.text
            assert 'no-store' in page.headers['cache-control']
            setup = (await c.get('/protection/api/setup?owner=admin-space')).json()
            assert setup['tasks'][0]['id'] == env[2]
            assert set(setup['tasks'][0]) == {'id', 'label', 'ready'}
            assert '/private/' not in json.dumps(setup)
            status = (await c.get('/protection/api/status')).json()
            assert set(status) == {'ok', 'mode', 'access_version'}
            session = (await c.get('/protection/api/session')).json()
            assert session['admin'] is False and 'owner' not in session
        async with client(env[0], 'admin') as c:
            for path in ['/protection', '/protection/admin']:
                assert '管理员专用' in (await c.get(path)).text
            assert '你需要授权什么' in (await c.get('/protection/setup')).text
    asyncio.run(run())


def test_admin_scope_uses_trusted_session_not_request(env):
    async def run():
        async with client(env[0], 'admin') as c:
            r = await c.get('/protection/api/events?owner=alice')
            assert r.status_code == 200
            assert {e['owner'] for e in r.json()['events']} == {'admin-space'}
        async with client(env[0], 'view') as c:
            r = await c.get('/protection/api/events?owner=admin-space')
            assert {e['owner'] for e in r.json()['events']} == {'alice'}
            assert (await c.get('/protection/api/collections')).json()['collections'][0]['id'] == env[2]
            assert (await c.post('/protection/api/policy', json={'enabled': False})).status_code == 403
            token = (await c.post('/protection/api/enrollments', json={})).json()['token']
            with env[1].store.db() as db:
                assert db.execute('SELECT owner FROM enrollments WHERE token_hash=?', (digest(token),)).fetchone()[0] == 'admin-space'
    asyncio.run(run())


def test_anonymous_denied_without_store_initialization(tmp_path):
    async def run():
        app = FastAPI()
        install(app, state_dir=tmp_path/'absent', allowed_roots=[tmp_path], mode='audit', session_resolver=lambda _: None)
        async with client(app, 'unknown') as c:
            for path in ['/protection/api/events', '/protection/api/setup', '/protection/api/status', '/protection/download']:
                assert (await c.get(path)).status_code == 401
            r = await c.get('/protection')
            assert r.status_code == 303 and r.headers['location'] == '/login'
            assert not (tmp_path/'absent').exists()
    asyncio.run(run())


@pytest.mark.parametrize('path', ['/protection/api/setup','/protection/api/session','/protection/api/events','/protection/api/preview','/protection/admin'])
def test_read_methods_do_not_accept_post(env, path):
    async def run():
        async with client(env[0], 'admin') as c:
            assert (await c.post(path, json={})).status_code == 405
    asyncio.run(run())


def test_consent_signed_agent_and_self_revoke(env):
    async def run():
        app, p, _ = env
        key = Ed25519PrivateKey.generate()
        pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        async with client(app) as c:
            for consent in [None, False, 'true', 1]:
                assert (await c.post('/protection/api/enrollments', json={'consent': consent})).status_code == 400
            token = (await c.post('/protection/api/enrollments', json={'consent': True, 'owner': 'admin-space'})).json()['token']
            enrolled = await c.post('/agent/v2/enroll', json={'token':token, 'public_key':pub, 'name':'我的电脑'})
            assert enrolled.status_code == 200
            did = enrolled.json()['device_id']
            with p.store.db() as db:
                row = db.execute('SELECT owner,user_id FROM devices WHERE id=?', (did,)).fetchone()
                assert tuple(row) == ('alice', 'u1')
            path = '/agent/v2/heartbeat'
            raw = json.dumps({'healthy': True, 'last_error': '/private/server/secret', 'watch_roots': 1}).encode()
            stamp, nonce = str(int(time.time())), uid()
            msg = chr(10).join(['sjfx-agent-v2', 'POST', path, digest(raw), did, stamp, nonce])
            proof = {'X-SJFX-Device':did, 'X-SJFX-Time':stamp, 'X-SJFX-Nonce':nonce,
                     'X-SJFX-Proof':base64.b64encode(key.sign(msg.encode())).decode()}
            assert (await c.post(path, content=raw, headers=proof)).status_code == 200
            safe = (await c.get('/protection/api/setup')).json()
            assert safe['devices'] == [{'id':did, 'name':'我的电脑', 'connected':True}]
            assert 'secret' not in json.dumps(safe)
            other_key = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            other = p.store.enroll(p.store.enrollment('alice','other-user'), base64.b64encode(other_key).decode(),'another')
            assert (await c.post('/protection/api/revoke-self',json={'device_id':other})).status_code == 404
            pending = (await c.post('/protection/api/enrollments', json={'consent':True})).json()['token']
            assert (await c.post('/protection/api/revoke-self',json={'device_id':did})).status_code == 200
            assert not (await c.get('/protection/api/setup')).json()['devices']
            with p.store.db() as db:
                assert db.execute('SELECT expires FROM enrollments WHERE token_hash=?',(digest(pending),)).fetchone()[0] == 0
                assert db.execute('SELECT revoked FROM devices WHERE id=?',(other,)).fetchone()[0] is None
            assert (await c.post(path,content=raw,headers=proof)).status_code == 403
    asyncio.run(run())


def test_csrf_and_regular_business_unchanged(env):
    async def run():
        app, p, _ = env
        @app.get('/api/ordinary-import-status')
        def ordinary():
            return {'unchanged':True}
        async with client(app) as c:
            assert (await c.get('/api/ordinary-import-status')).json() == {'unchanged':True}
            assert (await c.post('/protection/api/enrollments',json={'consent':True},headers={'Origin':'https://evil.invalid'})).status_code == 403
    asyncio.run(run())
