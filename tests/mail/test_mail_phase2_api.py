"""Public research entry enables phase two and preserves resume authorization."""
import json
from email.message import EmailMessage
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from config import Config
from web_compat import SJFXFastAPI
from services.storage import Storage
from services.mail.repository import MailRepository
from services.mail.bootstrap import build_mail_services
from services.mail.api import register_mail_routes
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.research.planner import VERSION


@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.setattr(Config,'MAIL_FEATURE_ENABLED',True)
    monkeypatch.setattr(Config,'MAIL_PHASE2_ENABLED',True)
    repo=MailRepository(Storage(tmp_path/'api.db'));repo.migrate()
    pkg,_=repo.create_package(owner_id='owner',display_name='API synthetic',source_kind='directory',source_path=str(tmp_path),source_revision='v1',config={})
    pid=pkg['package_id'];m=EmailMessage()
    m['From']='alice@example.com';m['To']='bob@example.com';m['Subject']='Quote v2';m['Message-ID']='<api@example.com>'
    m.set_content('I accept quote v2 subject to approval.');raw=m.as_bytes()
    row=repo.upsert_ingested_message(package_id=pid,source={'locator_type':'directory','root_path':str(tmp_path),'source_revision':'v1'},member=MailMember('api.eml',0,len(raw),{}),raw=raw,parsed=parse_eml(raw))
    repo.update_package(pid,'owner',status='ready')
    session=repo.create_session(pid,'owner','Phase2',{'message_keys':[row['message_key']]})
    identity={'owner':'owner'}
    services=build_mail_services(repo.storage,model=object(),job_factory=repo.storage.create_or_get_typed_job,owner_resolver=lambda:identity['owner'])
    app=SJFXFastAPI();register_mail_routes(app,services)
    with TestClient(app) as client:yield client,repo,pid,session,identity


def start(client,session):
    return client.post('/api/mail/research-sessions/'+session['session_id']+'/turns',json={'question':'分析报价接受条件和后续撤回'})


def test_public_create_enables_engine_and_does_not_strand_second_turn(api):
    client,repo,pid,session,_=api
    response=start(client,session)
    assert response.status_code==202,response.text
    turn=response.json()['turn'];spec=turn['task_spec']
    assert spec['research_version']==VERSION
    assert spec['budget']['max_tokens']==Config.MAIL_RESEARCH_MAX_TOKENS
    assert spec['budget']['max_context_tokens']==Config.MAIL_RESEARCH_CONTEXT_TOKENS
    assert spec['scope']['message_keys']==session['scope']['message_keys']
    with patch('services.mail.api._queue',return_value=(response.json()['job_id'],False)):
        second=start(client,session)
    assert second.status_code==400,second.text
    with repo._connection() as c:
        statuses=[r[0] for r in c.execute('SELECT status FROM mail_research_turns WHERE session_id=?',(session['session_id'],))]
    assert statuses.count('failed')==1 and len(statuses)==2


def test_resume_only_increases_budget_keeps_scope_owner_and_source(api,monkeypatch):
    client,repo,pid,session,identity=api
    # Exercise increase-only semantics using an intentionally smaller initial token budget.
    monkeypatch.setattr(Config,'MAIL_RESEARCH_MAX_TOKENS',120000)
    response=start(client,session).json();tid=response['turn']['turn_id'];spec=response['turn']['task_spec']
    path='/api/mail/research-turns/'+tid+'/resume'
    assert client.post(path,json={'budget':{'max_tokens':200000}}).status_code==400
    repo.update_turn(tid,'owner',status='completed',sufficiency='partial',final={'limitations':['预算不足']})
    with repo._connection() as c:c.execute("UPDATE analysis_jobs SET status='completed' WHERE id=?",(response['job_id'],))
    assert client.post(path,json={'scope':{},'budget':{'max_tokens':200000}}).status_code==400
    assert client.post(path,json={'budget':{'max_tokens':True}}).status_code==400
    identity['owner']='intruder'
    assert client.post(path,json={'budget':{'max_tokens':200000}}).status_code==404
    identity['owner']='owner'
    resumed=client.post(path,json={'budget':{'max_tokens':200000}})
    assert resumed.status_code==202,resumed.text
    after=repo.get_turn(tid,'owner')['task_spec']
    assert after['scope']==spec['scope'] and after['budget']['max_tokens']==200000
    repo.update_turn(tid,'owner',status='completed',sufficiency='partial')
    with repo._connection() as c:
        c.execute("UPDATE analysis_jobs SET status='completed' WHERE id=?",(resumed.json()['job_id'],))
        c.execute('UPDATE mail_packages SET index_epoch=index_epoch+1 WHERE package_id=?',(pid,))
    assert client.post(path,json={'budget':{'max_tokens':300000}}).status_code==400


def test_rollback_switch_preserves_previous_research_entry(api,monkeypatch):
    client,repo,pid,session,_=api
    monkeypatch.setattr(Config,'MAIL_PHASE2_ENABLED',False)
    response=start(client,session)
    assert response.status_code==202,response.text
    assert not response.json()['turn']['task_spec'].get('research_version')


def test_public_create_carries_full_source_batch_defaults_without_changing_scope(api,monkeypatch):
    client,repo,pid,session,_=api
    expected={'MAIL_RESEARCH_MAX_MODEL_CALLS':64,'MAIL_RESEARCH_MAX_TOOL_CALLS':200,
              'MAIL_RESEARCH_MAX_TOKENS':600000,'MAIL_RESEARCH_TIMEOUT_SECONDS':7200}
    for name,value in expected.items():monkeypatch.setattr(Config,name,value)
    response=start(client,session)
    assert response.status_code==202,response.text
    spec=response.json()['turn']['task_spec']
    assert {k:spec['budget'][k] for k in ('max_model_calls','max_tool_calls','max_tokens','max_seconds')}=={
        'max_model_calls':64,'max_tool_calls':200,'max_tokens':600000,'max_seconds':7200}
    assert spec['budget']['max_model_calls']>=14*2
    assert spec['scope']['message_keys']==session['scope']['message_keys']


@pytest.mark.parametrize('budget',[{'max_tokens':100000001},{'max_model_calls':32769},{'max_tool_calls':100001},{'max_seconds':604801},{'max_gap_rounds':13},{'max_tokens':True},[]])
def test_create_rejects_invalid_or_above_service_budget_before_queue(api,budget):
    client,repo,pid,session,_=api
    response=client.post('/api/mail/research-sessions/'+session['session_id']+'/turns',json={'question':'分析报价条件','budget':budget})
    assert response.status_code==400,response.text
    with repo._connection() as c:
        assert c.execute('SELECT COUNT(*) FROM mail_research_turns').fetchone()[0]==0


def test_create_and_resume_high_allowances_match_worker_options_and_keep_checkpoint(api):
    client,repo,pid,session,_=api
    selected={'max_model_calls':1024,'max_tool_calls':4000,'max_tokens':10000000,'max_seconds':172800,'max_gap_rounds':8,'max_gap_candidates':1000}
    response=client.post('/api/mail/research-sessions/'+session['session_id']+'/turns',json={'question':'分析报价条件','budget':selected})
    assert response.status_code==202,response.text
    body=response.json();tid=body['turn']['turn_id'];original=body['turn']['task_spec']
    assert all(original['budget'][key]==value for key,value in selected.items())
    assert repo.storage.get_job(body['job_id'],'owner')['options']['research_timeout_seconds']==172800
    checkpoint={'token_usage':{'total_tokens':50000},'runtime_checkpoint':{'tool_calls':20,'model_calls':5},'answer':'partial preserved'}
    repo.update_turn(tid,'owner',status='completed',sufficiency='partial',final=checkpoint)
    with repo._connection() as c:c.execute("UPDATE analysis_jobs SET status='completed' WHERE id=?",(body['job_id'],))
    resumed=client.post('/api/mail/research-turns/'+tid+'/resume',json={'budget':{'max_seconds':604800,'max_model_calls':32768}})
    assert resumed.status_code==202,resumed.text
    after=repo.get_turn(tid,'owner')
    assert after['task_spec']['scope']==original['scope'] and after['final']==checkpoint
    assert after['task_spec']['budget']['max_tokens']==10000000
    assert repo.storage.get_job(resumed.json()['job_id'],'owner')['options']['research_timeout_seconds']==604800


def test_restart_upgrades_old_call_and_time_defaults_and_keeps_higher_token_allowance(api):
    client,repo,pid,session,_=api
    response=start(client,session).json();tid=response['turn']['turn_id']
    spec=response['turn']['task_spec'];spec['budget'].update(max_model_calls=12,max_tool_calls=48,max_seconds=1800,max_tokens=12000000)
    with repo._connection() as c:
        c.execute('UPDATE mail_research_turns SET task_spec_json=? WHERE turn_id=?',(json.dumps(spec),tid))
        c.execute("UPDATE analysis_jobs SET status='completed' WHERE id=?",(response['job_id'],))
    repo.update_turn(tid,'owner',status='completed',sufficiency='partial')
    retry=client.post('/api/mail/research-turns/'+tid+'/retry')
    assert retry.status_code==202,retry.text
    fresh=retry.json()['turn']['task_spec']
    assert fresh['budget']['max_model_calls']==Config.MAIL_RESEARCH_MAX_MODEL_CALLS
    assert fresh['budget']['max_tool_calls']==Config.MAIL_RESEARCH_MAX_TOOL_CALLS
    assert fresh['budget']['max_seconds']==Config.MAIL_RESEARCH_TIMEOUT_SECONDS
    assert fresh['budget']['max_tokens']==12000000
    assert fresh['scope']==spec['scope']
