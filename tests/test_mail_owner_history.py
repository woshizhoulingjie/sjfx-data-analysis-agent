import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from config import Config
from web_compat import SJFXFastAPI
from services.storage import Storage
from services.mail.api import _owner, register_mail_routes
from services.mail.bootstrap import build_mail_services
from sjfx_multiuser.ownership import resolve_owner


@pytest.mark.parametrize('session,view,expected', [
    (None, None, 'primary'),
    ({'role':'admin','owner_id':'primary'}, None, 'primary'),
    ({'role':'admin','owner_id':'primary'}, 'primary', 'primary'),
    ({'role':'admin','owner_id':'primary'}, 'alice', 'alice'),
    ({'role':'user','owner_id':'alice'}, None, 'alice'),
    ({'role':'user','owner_id':'alice'}, 'primary', 'alice'),
    ({'role':'unexpected','owner_id':'alice'}, 'primary', 'alice'),
])
def test_trusted_session_owner_mapping(monkeypatch, session, view, expected):
    monkeypatch.setattr(Config, 'OWNER_ID', 'primary')
    services=SimpleNamespace(owner_resolver=lambda:resolve_owner(session,view,Config.OWNER_ID).owner_id)
    assert _owner(services)==expected


def test_admin_default_uses_config_not_hardcoded_primary(monkeypatch):
    monkeypatch.setattr(Config,'OWNER_ID','existing-workspace')
    assert _owner(SimpleNamespace(owner_resolver=lambda:None))=='existing-workspace'


def test_missing_and_empty_resolvers_do_not_gain_admin_scope():
    assert _owner(SimpleNamespace(owner_resolver=None))=='legacy'
    assert _owner(SimpleNamespace(owner_resolver=lambda:''))=='legacy'
    def broken():raise RuntimeError('identity unavailable')
    with pytest.raises(RuntimeError):_owner(SimpleNamespace(owner_resolver=broken))


@pytest.fixture
def mail_api(tmp_path, monkeypatch):
    monkeypatch.setattr(Config,'OWNER_ID','primary')
    monkeypatch.setattr(Config,'MAIL_FEATURE_ENABLED',True)
    identity={'session':{'role':'admin','owner_id':'primary'},'view':None}
    storage=Storage(tmp_path/'mail.db')
    services=build_mail_services(storage,owner_resolver=lambda:resolve_owner(identity['session'],identity['view'],Config.OWNER_ID).owner_id)
    repo=services.repository;repo.migrate();records={}
    for owner in ['primary','alice','bob','legacy']:
        package,_=repo.create_package(owner_id=owner,display_name='History '+owner,source_kind='directory',source_path=str(tmp_path),source_revision='revision-'+owner,config={})
        pid=package['package_id']
        session=repo.create_session(pid,owner,'Saved research')
        turn=repo.create_turn(session['session_id'],owner,'Saved question',{})
        result=repo.create_result_set(pid,owner,[],{'answer':'Saved result'}, {})
        repo.update_turn(turn['turn_id'],owner,status='completed',result_set_id=result)
        records[owner]={'package':pid,'session':session['session_id'],'turn':turn['turn_id'],'result':result}
    app=SJFXFastAPI();register_mail_routes(app,services)
    with TestClient(app) as client:
        yield client,identity,records,repo


@pytest.mark.parametrize('role,view,expected',[('admin',None,'primary'),('admin','primary','primary'),('admin','alice','alice'),('user',None,'alice'),('user','primary','alice')])
def test_package_list_has_only_resolved_workspace(mail_api,role,view,expected):
    client,identity,records,repo=mail_api
    identity.update(session={'role':role,'owner_id':'primary' if role=='admin' else 'alice'},view=view)
    response=client.get('/api/mail/packages?owner_id=bob&as_owner=bob',headers={'X-Owner-ID':'bob'})
    assert response.status_code==200
    assert [p['package_id'] for p in response.json()['packages']]==[records[expected]['package']]


def history_paths(record):
    return [
        '/api/mail/packages/'+record['package'],
        '/api/mail/packages/'+record['package']+'/messages',
        '/api/mail/research-sessions?package_id='+record['package'],
        '/api/mail/research-sessions/'+record['session'],
        '/api/mail/research-turns/'+record['turn'],
        '/api/mail/packages/'+record['package']+'/result-sets',
        '/api/mail/result-sets/'+record['result'],
    ]


@pytest.mark.parametrize('index',range(7))
def test_admin_can_open_each_historical_surface(mail_api,index):
    client,identity,records,repo=mail_api
    response=client.get(history_paths(records['primary'])[index])
    assert response.status_code==200,response.text
    assert response.json()['ok'] is True
    if index==2:assert response.json()['sessions'][0]['session_id']==records['primary']['session']
    if index==3:assert response.json()['turns'][0]['turn_id']==records['primary']['turn']
    if index==5:assert response.json()['items'][0]['result_set_id']==records['primary']['result']
    if index==6:assert response.json()['result_set']['result_set_id']==records['primary']['result']


@pytest.mark.parametrize('index',range(7))
def test_user_cannot_open_other_workspace_history(mail_api,index):
    client,identity,records,repo=mail_api
    identity.update(session={'role':'user','owner_id':'alice'},view='primary')
    response=client.get(history_paths(records['primary'])[index],headers={'X-Owner-ID':'primary'})
    if response.status_code==200:
        payload=response.json()
        assert payload.get('items',payload.get('messages',{}).get('items',None))==[]
        assert records['primary']['session'] not in response.text
        assert records['primary']['result'] not in response.text
    else:assert response.status_code in (400,404)


def test_admin_session_creation_stays_in_historical_workspace(mail_api):
    client,identity,records,repo=mail_api
    response=client.post('/api/mail/research-sessions',json={'package_id':records['primary']['package'],'owner_id':'bob','title':'Continue history'})
    assert response.status_code==201,response.text
    assert response.json()['session']['owner_id']=='primary'


def test_user_cannot_write_session_into_other_workspace(mail_api):
    client,identity,records,repo=mail_api
    identity.update(session={'role':'user','owner_id':'alice'},view='primary')
    before=repo.list_sessions(records['primary']['package'],'primary')
    response=client.post('/api/mail/research-sessions',json={'package_id':records['primary']['package'],'owner_id':'primary','title':'Attempt'})
    assert response.status_code==404
    assert repo.list_sessions(records['primary']['package'],'primary')==before
