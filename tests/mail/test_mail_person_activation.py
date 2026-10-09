import json
from contextlib import nullcontext
from email.message import EmailMessage
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from config import Config
from web_compat import SJFXFastAPI
from services.storage import Storage
from services.mail.bootstrap import build_mail_services
from services.mail.api import register_mail_routes
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.jobs import _research
from services.mail.people import corpus_state
from sjfx_multiuser.ownership import resolve_owner

from test_person_context_upgrade import decode_person_source_payload,PersonModel

class EvidenceModel:
    def __init__(self):self.calls=[]
    def chat_json(self,system,prompt,**kwargs):
        payload=decode_person_source_payload(json.loads(prompt));self.calls.append(payload)
        if 'claims' in payload:return PersonModel().chat_json(system,prompt,**kwargs)
        if 'context_chunks' in payload:
            return {'json':{'reviews':[{'finding_id':f['finding_id'],'status':'supported',
                'rationale':'已核查当前上下文，仍需按完整会话审阅',
                'conditions':[],'contradictions':[],'unresolved_questions':[],
                'citations':[],'counterevidence':[],'later_replies':[]}
                for f in payload['provisional_findings']]}}
        if 'chunks' in payload:
            return {'json':{'findings':[{'kind':'goal' if c['authored'] else 'received','statement':'核实条件后推进合作','uncertainty':'需结合原文核实','citations':[{'chunk_id':c['chunk_id'],'quote':c['text'][:40]}]} for c in payload['chunks']]}}
        return {'json':{'themes':[{'title':'核实条件再推进','statement':'以明确条件作为后续行动前提','uncertainty':'收到的信息不等于本人认同','finding_ids':[f['finding_id'] for f in payload['evidence'][:6]]}]}}

@pytest.mark.parametrize('role,view,owner',[('admin',None,'primary'),('admin','alice','alice'),('user',None,'alice')])
def test_selected_correspondent_runs_through_real_mail_queue(tmp_path,monkeypatch,role,view,owner):
    monkeypatch.setattr(Config,'OWNER_ID','primary');monkeypatch.setattr(Config,'MAIL_FEATURE_ENABLED',True)
    storage=Storage(tmp_path/'mail.db');model=EvidenceModel()
    identity={'role':role,'owner_id':'primary' if role=='admin' else 'alice'}
    services=build_mail_services(storage,model=model,job_factory=storage.create_or_get_typed_job,owner_resolver=lambda:resolve_owner(identity,view,Config.OWNER_ID).owner_id)
    repo=services.repository;repo.migrate()
    package,_=repo.create_package(owner_id=owner,display_name='Trilingual activation test',source_kind='directory',source_path=str(tmp_path),source_revision='activation',config={});pid=package['package_id']
    bodies=['我们希望下周推进合作，但必须先核对合同条件和付款安排。','We intend to proceed after reviewing the contract and confirming the payment schedule.','เราต้องการตรวจสอบเงื่อนไขสัญญาและการชำระเงินก่อนเริ่มโครงการ']
    for n,body in enumerate(bodies):
        message=EmailMessage();message['From']='alice@example.com' if n!=1 else 'bob@example.com';message['To']='bob@example.com' if n!=1 else 'alice@example.com';message['Subject']='计划 / Plan / แผน';message['Message-ID']='<activation-%s@example.com>'%n;message['Date']='Tue, 07 May 2024 15:38:50 +0700';message.set_content(body);raw=message.as_bytes()
        repo.upsert_ingested_message(package_id=pid,source={'locator_type':'directory','root_path':str(tmp_path),'container_path':None,'container_sha256':'activation','source_revision':'activation'},member=MailMember('%s.eml'%n,n,len(raw),{'kind':'file','path':'unused'}),raw=raw,parsed=parse_eml(raw))
    repo.finish_import(pid,owner)
    app=SJFXFastAPI();register_mail_routes(app,services)
    with TestClient(app) as client,patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        assert client.get('/api/mail/packages').json()['packages'][0]['package_id']==pid
        people=client.get('/api/mail/packages/'+pid+'/people').json();assert {p['address'] for p in people['items']}=={'alice@example.com','bob@example.com'}
        response=client.post('/api/mail/packages/'+pid+'/person/analyses',json={'address':'alice@example.com','question':'他表达了哪些目标和条件？','owner_id':'intruder'})
        assert response.status_code==202,response.text
        tid=response.json()['turn_id'];job=storage.get_job(response.json()['job_id'],owner)
        assert job and job['task_type']=='mail_research' and job['owner_id']==owner
        for _ in range(30):
            result=_research(job,services)
            if not result.get('_requeue_slice'):break
        else:raise AssertionError('Person analysis did not finish')
        result=client.get('/api/mail/person-analyses/'+tid).json()
        assert result['status']=='completed' and result['progress']['complete']
        assert result['progress']['characters_read']==corpus_state(repo,pid,owner,'alice@example.com')['characters']
        assert result['progress']['synthesis']['themes']
        findings=result['items'];assert {f['kind'] for f in findings}=={'goal','received'}
        assert any('chunks' in c for c in model.calls) and any('claims' in c for c in model.calls)
        assert result['progress']['synthesis']['method']=='source_bound_extractive'
        if role=='user':
            from services.mail.person_research import PERSON_EXECUTION_ENGINE
            original=repo.get_turn(tid,owner)['task_spec']
            assert original['execution_engine']==PERSON_EXECUTION_ENGINE
            # Even a copied matching cache key must not revive a completed
            # analysis from an older execution workflow.
            legacy={k:v for k,v in original.items() if k!='execution_engine'}
            with repo._connection() as c:
                c.execute('UPDATE mail_research_turns SET task_spec_json=? WHERE turn_id=?',(json.dumps(legacy),tid))
            response=client.post('/api/mail/packages/'+pid+'/person/analyses',json={'address':'alice@example.com','question':'他表达了哪些目标和条件？'})
            assert response.status_code==202 and response.json()['turn_id']!=tid and not response.json().get('cached')
        identity.update(role='user',owner_id='intruder')
        assert client.get('/api/mail/person-analyses/'+tid).status_code==404
        assert client.get('/api/mail/packages/'+pid+'/person/messages?address=alice@example.com').status_code==404
