"""Logical-message role unions, identity boundaries and clickable pair scopes."""
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from config import Config
from services.storage import Storage
from services.mail.repository import MailRepository
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.people import contacts, correspondence, bilateral, relationships
from services.mail.graph_metrics import confirm_entity, revoke_entity, entity_history, network


@pytest.fixture
def foundation_people(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'MAIL_ENABLE_GRAPH_METRICS', True)
    monkeypatch.setattr(Config, 'MAIL_FEATURE_ENABLED', True)
    repo = MailRepository(Storage(tmp_path / 'people.sqlite3'))
    repo.migrate()
    package, _ = repo.create_package(owner_id='owner', display_name='People', source_kind='directory', source_path=str(tmp_path), source_revision='people-v1', config={})
    pid = package['package_id']
    counter = [0]

    def add(sender='a@example.com', to='b@example.com', cc=None, bcc=None, date='Tue, 07 May 2024 15:38:50 +0700', subject='alpha', attachment=False, raw=None, package_id=None):
        ordinal = counter[0]; counter[0] += 1
        if raw is None:
            msg = EmailMessage()
            msg['From'] = sender
            msg['To'] = to
            if cc: msg['Cc'] = cc
            if bcc: msg['Bcc'] = bcc
            if date: msg['Date'] = date
            msg['Subject'] = subject
            msg['Message-ID'] = '<people-%s@example.com>' % ordinal
            msg.set_content('Body ' + subject)
            if attachment: msg.add_attachment(b'contract', maintype='application', subtype='octet-stream', filename='contract.txt')
            raw = msg.as_bytes()
        result = repo.upsert_ingested_message(package_id=package_id or pid,
            source={'locator_type':'directory','root_path':str(tmp_path),'container_sha256':'people-v1','source_revision':'people-v1'},
            member=MailMember('people-%s.eml' % ordinal, ordinal, len(raw), {'kind':'file','path':'unused'}), raw=raw, parsed=parse_eml(raw))
        return result['message_key'], raw
    return repo, pid, add


def page_keys(loader, expected, **kwargs):
    result = loader(offset=0, limit=1, **kwargs)
    assert result['total'] == expected
    keys = [result['items'][0]['message_key']] if expected else []
    for offset in range(1, expected):
        next_page = loader(offset=offset, limit=1, **kwargs)
        assert next_page['total'] == expected
        keys.append(next_page['items'][0]['message_key'])
    assert len(keys) == len(set(keys)) == expected
    return keys


def test_role_unions_match_every_paginated_card(foundation_people):
    repo, pid, add = foundation_people
    _, raw = add(to=', '.join('r%d@example.com' % n for n in range(10)), cc='r0@example.com')
    add(raw=raw)
    add(sender='b@example.com', to='a@example.com', cc='a@example.com', bcc='a@example.com')
    add(to='a@example.com')
    loader = lambda **kw: correspondence(repo, pid, 'owner', 'a@example.com', **kw)
    result = loader()
    assert result['role_counts'] == {'total':3,'sent':2,'direct':2,'cc':1,'bcc':1,'received':2}
    for direction, expected in result['role_counts'].items():
        page_keys(loader, expected, direction='all' if direction=='total' else direction)
    assert correspondence(repo, pid, 'owner', 'r0@example.com', direction='direct')['total'] == 1
    assert correspondence(repo, pid, 'owner', 'r0@example.com', direction='cc')['total'] == 1
    assert correspondence(repo, pid, 'owner', 'r0@example.com')['role_counts']['received'] == 1
    assert next(n for n in network(repo, pid, 'owner')['nodes'] if n['address']=='a@example.com')['sent'] == 2


def test_confirmed_person_unions_and_revoke_preserve_raw_roles(foundation_people):
    repo, pid, add = foundation_people
    add(sender='a@example.com, alias@example.com', to='b@example.com')
    add(sender='b@example.com', to='a@example.com, alias@example.com', cc='alias@example.com')
    eid = confirm_entity(repo, pid, 'owner', 'Alice', [' a@EXAMPLE.COM ', 'alias@example.com'])
    result = correspondence(repo, pid, 'owner', entity_id=eid)
    assert result['addresses'] == ['a@example.com','alias@example.com']
    assert result['role_counts'] == {'total':2,'sent':1,'direct':1,'cc':1,'bcc':0,'received':1}
    people = contacts(repo, pid, 'owner', view='person')['items']
    alice = next(p for p in people if p['entity_id']==eid)
    assert alice['total'] == 2 and alice['sent'] == 1
    assert contacts(repo, pid, 'owner', query='alias', view='person')['total'] == 1
    node = next(n for n in network(repo, pid, 'owner', view='person')['nodes'] if n['address']==eid)
    assert node['sent'] == 1 and node['received'] == 1
    assert node['sent_relations'] == 2
    before = correspondence(repo, pid, 'owner', 'a@example.com')['role_counts']
    revoke_entity(repo, pid, 'owner', eid, 'separate mailboxes')
    assert correspondence(repo, pid, 'owner', 'a@example.com')['role_counts'] == before
    assert {p['address'] for p in contacts(repo, pid, 'owner', view='person')['items']} == {'a@example.com','alias@example.com','b@example.com'}
    history = entity_history(repo, pid, 'owner', eid)
    assert [r['action'] for r in history['items']] == ['revoked','confirmed']
    assert history['items'][0]['payload']['addresses'] == ['a@example.com','alias@example.com']
    with pytest.raises(LookupError): correspondence(repo, pid, 'owner', entity_id=eid)


def test_sender_addresses_are_structured_without_comma_splitting(foundation_people):
    repo,pid,add=foundation_people
    add(sender='"left,right"@example.com')
    result=correspondence(repo,pid,'owner','b@example.com')
    assert len(result['items'][0]['senders'])==1
    assert 'left,right' in result['items'][0]['senders'][0]


def test_all_filters_are_shared_with_cards_contacts_and_pairs(foundation_people):
    repo, pid, add = foundation_people
    kept, _ = add(subject='alpha chosen', attachment=True)
    add(subject='beta', attachment=True)
    add(subject='alpha no file')
    add(subject='alpha old', attachment=True, date='Mon, 06 May 2024 12:00:00 +0000')
    add(subject='alpha undated', attachment=True, date=None)
    filters = {'date_from':'2024-05-07','date_to':'2024-05-07','q':'alpha','has_attachment':True}
    result = correspondence(repo, pid, 'owner', 'a@example.com', **filters)
    assert result['role_counts']['sent'] == result['total'] == 1
    assert result['items'][0]['message_key'] == kept
    assert contacts(repo, pid, 'owner', **filters)['items'][0]['total'] == 1
    pair = bilateral(repo, pid, 'owner', 'a@example.com', 'b@example.com', **filters)
    assert pair['counts']['sent'] == pair['total'] == 1
    assert relationships(repo, pid, 'owner', 'a@example.com', **filters)['items'][0]['total'] == 1
    assert next(n for n in network(repo, pid, 'owner', **filters)['nodes'] if n['address']=='a@example.com')['sent'] == 1
    with pytest.raises(ValueError): correspondence(repo, pid, 'owner', 'a@example.com', date_from='bad')
    with pytest.raises(ValueError): correspondence(repo, pid, 'owner', 'a@example.com', has_attachment='sometimes')
    with pytest.raises(ValueError): correspondence(repo, pid, 'owner', 'a@example.com', participant='invalid')
    assert correspondence(repo,pid,'owner','a@example.com',participant='missing@example.com')['total']==0
    assert correspondence(repo, pid, 'owner', 'a@example.com', q='%')['total'] == 0


def test_bilateral_direct_copy_and_shared_are_distinct(foundation_people):
    repo, pid, add = foundation_people
    sent, _ = add()
    received, _ = add(sender='b@example.com', to='a@example.com')
    copy, _ = add(to='outside@example.com', cc='b@example.com')
    blind, _ = add(sender='b@example.com', to='outside@example.com', bcc='a@example.com')
    shared, _ = add(sender='third@example.com', to='a@example.com, b@example.com')
    add(sender='third@example.com', to='a@example.com')
    loader = lambda **kw: bilateral(repo, pid, 'owner', 'a@example.com','b@example.com', **kw)
    result = loader()
    assert result['counts'] == {'total':5,'sent':1,'received':1,'sent_cc':1,'received_cc':0,'sent_bcc':0,'received_bcc':1,'cc':1,'bcc':1,'shared':1}
    for direction, expected in result['counts'].items():
        page_keys(loader, expected, direction='all' if direction=='total' else direction)
    assert set(page_keys(loader,5)) == {sent,received,copy,blind,shared}
    relation = next(r for r in relationships(repo, pid, 'owner','a@example.com')['items'] if r['counterpart']=='b@example.com')
    for key in result['counts']: assert relation[key] == result['counts'][key]
    assert loader(direction='shared')['items'][0]['pair_roles'] == ['shared']


def test_package_and_owner_identity_isolation(foundation_people, tmp_path):
    repo, pid, add = foundation_people
    key, raw = add()
    other, _ = repo.create_package(owner_id='other', display_name='Other', source_kind='directory', source_path=str(tmp_path), source_revision='other', config={})
    add(raw=raw, package_id=other['package_id'])
    eid = confirm_entity(repo,pid,'owner','Alice',['a@example.com'])
    assert correspondence(repo,other['package_id'],'other','a@example.com')['total'] == 1
    for fn in (lambda: correspondence(repo,pid,'other','a@example.com'),lambda: bilateral(repo,pid,'other','a@example.com','b@example.com'),lambda: entity_history(repo,pid,'other'),lambda: revoke_entity(repo,pid,'other',eid)):
        with pytest.raises(LookupError): fn()
    with pytest.raises(LookupError): correspondence(repo,other['package_id'],'other',entity_id=eid)
    with pytest.raises(LookupError): revoke_entity(repo,other['package_id'],'other',eid)
    assert entity_history(repo,other['package_id'],'other')['total'] == 0
    assert correspondence(repo,pid,'owner','a@example.com')['items'][0]['message_key'] == key


def test_foundation_people_routes_filters_pagination_and_revoke(foundation_people):
    from web_compat import SJFXFastAPI
    from fastapi.testclient import TestClient
    from services.mail.people_api import register_people_routes
    repo, pid, add = foundation_people
    add(attachment=True); add(subject='beta')
    owner = ['owner']
    services = SimpleNamespace(repository=repo,model=None,owner_resolver=lambda:owner[0],storage=SimpleNamespace(get_job=lambda *args:None))
    app=SJFXFastAPI(); register_people_routes(app,services); client=TestClient(app)
    base='/api/mail/packages/'+pid
    eid=confirm_entity(repo,pid,'owner','Alice',['a@example.com'])
    params={'entity_id':eid,'direction':'sent','q':'alpha','has_attachment':'true','date_from':'2024-05-07','date_to':'2024-05-07','limit':'1'}
    response=client.get(base+'/person/messages',params=params)
    assert response.status_code==200,response.text
    assert response.json()['total']==response.json()['role_counts']['sent']==1
    response=client.get(base+'/person/pair/messages',params={**params,'counterpart':'b@example.com'})
    assert response.status_code==200 and response.json()['total']==1
    assert client.get(base+'/people',params={'view':'person','contact_query':'Alice','q':'alpha','has_attachment':'true'}).json()['total']==1
    assert client.get(base+'/person/messages',params={'entity_id':eid,'direction':'invalid'}).status_code==400
    assert client.get(base+'/person/pair/messages',params={'entity_id':eid,'counterpart':'a@example.com'}).status_code==400
    owner[0]='intruder'
    assert client.post(base+'/entities/'+eid+'/revoke',json={}).status_code==404
    assert client.get(base+'/entities/history').status_code==404
    owner[0]='owner'
    assert client.post(base+'/entities/'+eid+'/revoke',json={'reason':'correction'}).json()['revoked'] is True
    assert client.get(base+'/entities/'+eid+'/history').json()['total']==2
    assert client.get(base+'/person/messages',params={'entity_id':eid}).status_code==404


def test_person_analysis_routes_freeze_confirmed_mailboxes_and_keep_legacy_history(foundation_people):
    from web_compat import SJFXFastAPI
    from fastapi.testclient import TestClient
    from services.mail.people_api import register_people_routes
    from services.mail.people import corpus_state as legacy_corpus
    repo,pid,add=foundation_people
    add();add(sender='alias@example.com',subject='Alias plans')
    repo.finish_import(pid,'owner')
    eid=confirm_entity(repo,pid,'owner','Alice',['a@example.com','alias@example.com'])
    jobs={}
    def queue(package_id,task_type,options,owner_id):
        jid='test-person-'+str(len(jobs));jobs[jid]={'status':'queued'};return jid,True
    services=SimpleNamespace(repository=repo,model=object(),owner_resolver=lambda:'owner',job_factory=queue,
        storage=SimpleNamespace(get_job=lambda jid,owner:jobs.get(jid)))
    app=SJFXFastAPI();register_people_routes(app,services);client=TestClient(app)
    base='/api/mail/packages/'+pid
    legacy_session=repo.create_session(pid,'owner','Legacy',{})
    legacy=repo.create_turn(legacy_session['session_id'],'owner','Old question',{'mode':'person_correspondence',
        'package_id':pid,'person_address':'a@example.com','corpus':legacy_corpus(repo,pid,'owner','a@example.com')})
    for budget in (True,False,0,32769,'512',1.5,None):
        assert client.post(base+'/person/analyses',json={'entity_id':eid,'model_call_budget':budget}).status_code==400
    response=client.post(base+'/person/analyses',json={'entity_id':eid,'model_call_budget':1024})
    assert response.status_code==202,response.text
    tid=response.json()['turn_id'];job=response.json()['job_id']
    spec=repo.get_turn(tid,'owner')['task_spec']
    assert spec['analysis_version']==2 and spec['person_entity_id']==eid
    assert spec['model_call_budget']==1024
    assert spec['person_addresses']==['a@example.com','alias@example.com']
    assert spec['corpus']['messages']==2 and response.json()['corpus']['entity_id']==eid
    assert [r['turn_id'] for r in client.get(base+'/person/analyses',params={'entity_id':eid}).json()['items']]==[tid]
    assert [r['turn_id'] for r in client.get(base+'/person/analyses',params={'address':'a@example.com'}).json()['items']]==[legacy['turn_id']]
    jobs[job]['status']='failed';repo.update_turn(tid,'owner',status='failed')
    assert client.post('/api/mail/person-analyses/'+tid+'/resume').status_code==202
    resumed=repo.get_turn(tid,'owner')['job_id'];jobs[resumed]['status']='failed';repo.update_turn(tid,'owner',status='failed')
    revoke_entity(repo,pid,'owner',eid)
    assert client.post('/api/mail/person-analyses/'+tid+'/resume').status_code==404
    assert client.get(base+'/person/analyses',params={'entity_id':eid}).status_code==404


@pytest.mark.parametrize('queue_failure',['busy','error'])
def test_person_budget_resume_preserves_checkpoint_and_rolls_back_failed_enqueue(foundation_people,queue_failure):
    from web_compat import SJFXFastAPI
    from fastapi.testclient import TestClient
    from services.mail.people_api import register_people_routes
    from services.mail.research.token_budget import init_usage
    repo,pid,add=foundation_people
    add();repo.finish_import(pid,'owner')
    jobs={};mode=['ready']
    def queue(package_id,task_type,options,owner_id):
        if mode[0]=='error':raise RuntimeError('Queue unavailable')
        if mode[0]=='busy':return 'another-running-job',False
        jid='budget-job-'+str(len(jobs));jobs[jid]={'status':'queued'};return jid,True
    services=SimpleNamespace(repository=repo,model=object(),owner_resolver=lambda:'owner',job_factory=queue,
        storage=SimpleNamespace(get_job=lambda jid,owner:jobs.get(jid)))
    app=SJFXFastAPI();register_people_routes(app,services);client=TestClient(app)
    started=client.post('/api/mail/packages/'+pid+'/person/analyses',json={'address':'a@example.com','model_call_budget':2})
    assert started.status_code==202,started.text
    tid=started.json()['turn_id'];jobs[started.json()['job_id']]['status']='completed'
    checkpoint={'analysis_version':2,'complete':False,'budget_exhausted':True,'model_calls':2,'model_call_budget':2,
        'cursor':[17,123],'characters_read':9000,'phase':'context_review','context_review':{'cursor':[9,12],'batches_read':3},
        'synthesis':{'themes':[{'title':'Preserved theme'}]},
        'token_usage':init_usage({'max_tokens':120000},{'total_tokens':2300,'actual_tokens':2300,'actual_calls':2,'calls':2})}
    repo.update_turn(tid,'owner',status='completed',sufficiency='partial',final=checkpoint,error={'message':'Preserved old notice'})
    spec=repo.get_turn(tid,'owner')['task_spec'];url='/api/mail/person-analyses/'+tid+'/resume'
    def persisted():
        with repo._connection() as c:
            return dict(c.execute('SELECT status,task_spec_json,final_json,job_id,error_json FROM mail_research_turns WHERE turn_id=?',(tid,)).fetchone())
    before=persisted()
    for budget in (None,True,'4',4.0,2,1,32769):
        assert client.post(url,json={'model_call_budget':budget}).status_code==400
        assert persisted()==before
    mode[0]=queue_failure
    response=client.post(url,json={'model_call_budget':4})
    assert response.status_code==409,response.text
    assert persisted()==before,'failed queue submission must restore exact spec, checkpoint, status, job and error'
    mode[0]='ready'
    response=client.post(url,json={'model_call_budget':4,'person_addresses':['intruder@example.com']})
    assert response.status_code==202,response.text
    resumed=repo.get_turn(tid,'owner')
    assert resumed['task_spec']=={**spec,'model_call_budget':4}
    assert resumed['final']=={**checkpoint,'model_call_budget':4,'budget_exhausted':False,'token_budget_exhausted':False,
        'token_usage':init_usage({'max_tokens':spec['max_tokens']},checkpoint['token_usage'])}
    assert resumed['final']['token_usage']['total_tokens']==2300
    assert resumed['status']=='queued' and resumed['job_id']==response.json()['job_id'] and resumed['error'] is None
    assert client.post(url,json={'model_call_budget':8}).status_code==400
    assert len(jobs)==2


@pytest.mark.parametrize('options,expected_budget',[({},4096),({'model_call_budget':32768},32768)])
def test_person_budget_default_maximum_and_resume_ceiling(foundation_people,options,expected_budget):
    from web_compat import SJFXFastAPI
    from fastapi.testclient import TestClient
    from services.mail.people_api import register_people_routes
    repo,pid,add=foundation_people
    add();repo.finish_import(pid,'owner');jobs={}
    def queue(package_id,task_type,options,owner_id):
        jid='budget-limit-'+str(len(jobs));jobs[jid]={'status':'completed'};return jid,True
    services=SimpleNamespace(repository=repo,model=object(),owner_resolver=lambda:'owner',job_factory=queue,
        storage=SimpleNamespace(get_job=lambda jid,owner:jobs.get(jid)))
    app=SJFXFastAPI();register_people_routes(app,services);client=TestClient(app)
    response=client.post('/api/mail/packages/'+pid+'/person/analyses',json={'address':'a@example.com',**options})
    assert response.status_code==202,response.text
    tid=response.json()['turn_id'];assert repo.get_turn(tid,'owner')['task_spec']['model_call_budget']==expected_budget
    checkpoint={'complete':False,'budget_exhausted':True,'model_call_budget':expected_budget,'model_calls':expected_budget,'cursor':[3,42]}
    repo.update_turn(tid,'owner',status='completed',sufficiency='partial',final=checkpoint)
    url='/api/mail/person-analyses/'+tid+'/resume'
    assert client.post(url,json={'model_call_budget':32769}).status_code==400
    if expected_budget<32768:
        assert client.post(url,json={'model_call_budget':32768}).status_code==202
        continued=repo.get_turn(tid,'owner');assert continued['task_spec']['model_call_budget']==continued['final']['model_call_budget']==32768
        assert continued['final']['cursor']==[3,42]
    else:
        assert client.post(url,json={'model_call_budget':32768}).status_code==400
        assert repo.get_turn(tid,'owner')['final']==checkpoint


def test_budget_resume_claim_prevents_duplicate_enqueue(foundation_people):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from web_compat import SJFXFastAPI
    from fastapi.testclient import TestClient
    from services.mail.people_api import register_people_routes
    from services.mail.person_corpus import corpus_state,resolve_selection
    from services.mail.person_research import LEDGER_ENGINE
    repo,pid,add=foundation_people
    add();repo.finish_import(pid,'owner')
    corpus=corpus_state(repo,pid,'owner',resolve_selection(repo,pid,'owner','a@example.com'))
    session=repo.create_session(pid,'owner','Budget continuation',{})
    turn=repo.create_turn(session['session_id'],'owner','Continue',{'mode':'person_correspondence','analysis_version':2,
        'package_id':pid,'person_address':'a@example.com','person_addresses':['a@example.com'],'person_entity_id':None,'corpus':corpus,'model_call_budget':2,
        'ledger_engine':LEDGER_ENGINE})
    tid=turn['turn_id'];repo.update_turn(tid,'owner',status='completed',sufficiency='partial',final={'complete':False,'budget_exhausted':True,'model_call_budget':2,'model_calls':2,'cursor':[7,10]})
    claimed=Event();release=Event();calls=[]
    def queue(package_id,task_type,options,owner_id):
        calls.append(options);claimed.set()
        if not release.wait(10):raise RuntimeError('Test queue timeout')
        return 'continuation-job',True
    services=SimpleNamespace(repository=repo,model=object(),owner_resolver=lambda:'owner',job_factory=queue,
        storage=SimpleNamespace(get_job=lambda *args:None))
    app=SJFXFastAPI();register_people_routes(app,services)
    url='/api/mail/person-analyses/'+tid+'/resume'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(lambda:TestClient(app).post(url,json={'model_call_budget':4}))
        try:
            assert claimed.wait(10)
            assert TestClient(app).post(url,json={'model_call_budget':8}).status_code==400
            assert len(calls)==1
        finally:release.set()
        assert future.result(timeout=10).status_code==202
    resumed=repo.get_turn(tid,'owner')
    assert resumed['task_spec']['model_call_budget']==4 and resumed['final']['cursor']==[7,10]


def test_people_v2_browser_themes_ledger_and_audit_pagination():
    import shutil
    import subprocess
    from pathlib import Path
    node=shutil.which('node')
    if not node:pytest.skip('Node is unavailable')
    source=Path(__file__).resolve().parents[1]/'mail_modules'/'test_mail_people_analysis.js'
    completed=subprocess.run([node,str(source)],capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert completed.returncode==0,completed.stderr
    if completed.stdout.startswith('SKIP:'):pytest.skip(completed.stdout.strip())
    assert 'audit pagination passed' in completed.stdout


def test_people_browser_cards_edges_and_shared_scope():
    """Exercise the actual script's click handlers with mocked API responses."""
    import json
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node: pytest.skip('Node is unavailable')
    script = r'''
const fs=require('fs');let chromium;try{chromium=require('playwright').chromium;}catch(error){process.stdout.write(JSON.stringify({unavailable:'playwright'}));process.exit(0);}
if(!fs.existsSync(chromium.executablePath())){process.stdout.write(JSON.stringify({unavailable:'chromium'}));process.exit(0);}
(async()=>{
  const browser=await chromium.launch({headless:true});
  try{
    const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.setContent('<select id="mail-package"><option value="p" selected>Package</option></select><button data-mail-module-link="network">People</button><div data-mail-module="network"></div>');
    await page.evaluate(()=>{
      window.calls=[];window.opened=[];window.SJFXMailFoundation={openMessage:(key,filters)=>window.opened.push({key,filters})};
      const roleCounts={total:3,sent:2,direct:2,cc:1,bcc:1,received:2};
      const pairCounts={total:2,sent:1,received:0,cc:0,bcc:0,shared:1};
      window.fetch=async path=>{
        window.calls.push(String(path));const url=new URL(path,'https://example.test');let data={};
        const item={message_key:'m1',subject_raw:'Source mail',sent_at_utc:'2024-05-07T08:38:50+00:00',roles:'from',sender:'a@example.com',attachment_count:1};
        if(url.pathname.endsWith('/people'))data={items:[{address:'a@example.com',entity_id:'entity-a',display_name:'Alice',addresses:['a@example.com','alias@example.com'],...roleCounts}],total:1,offset:0,limit:30,identity_note:'Explicit identities'};
        else if(url.pathname.endsWith('/person/pair/messages')){const direction=url.searchParams.get('direction');const count=pairCounts[direction==='all'?'total':direction];data={items:Array.from({length:count},(_,i)=>({...item,message_key:'pair'+i,pair_roles:['shared'],conversations:[]})),counts:pairCounts,total:count,offset:0,limit:20};}
        else if(url.pathname.endsWith('/person/messages')){const direction=url.searchParams.get('direction');const count=roleCounts[direction==='all'?'total':direction];data={items:Array.from({length:count},(_,i)=>({...item,message_key:'m'+i})),role_counts:roleCounts,counts:roleCounts,total:count,offset:0,limit:20};}
        else if(url.pathname.endsWith('/person/relationships'))data={items:[{counterpart:'b@example.com',entity_id:'entity-b',display_name:'Bob',sent:1,received:0,cc:0,bcc:0,shared:1,total:2}],total:1,offset:0,limit:8};
        else if(url.pathname.endsWith('/person/analyses'))data={items:[]};
        else throw new Error('Unexpected endpoint '+url.pathname);
        return {ok:true,status:200,json:async()=>({ok:true,...data})};
      };
    });
    await page.addScriptTag({path:process.argv[1]});
    await page.evaluate(()=>window.SJFXMailPeople.select('a@example.com',{date_from:'2024-05-07',date_to:'2024-05-07',q:'alpha',has_attachment:'true'}));
    await page.getByRole('button',{name:'直接收到 2 封，查看明细',exact:true}).click();
    await page.waitForFunction(()=>window.calls.some(path=>path.includes('/person/messages?')&&new URL(path,'https://example.test').searchParams.get('direction')==='direct'));
    await page.locator('line[role=button]').dispatchEvent('click');
    await page.waitForFunction(()=>window.calls.some(path=>path.includes('/person/pair/messages?')));
    await page.getByRole('button',{name:'共同参与 1',exact:true}).last().click();
    await page.waitForFunction(()=>window.calls.some(path=>path.includes('/person/pair/messages?')&&new URL(path,'https://example.test').searchParams.get('direction')==='shared'));
    await page.getByRole('button',{name:'Source mail',exact:true}).first().click();
    const result=await page.evaluate(()=>({calls:window.calls,opened:window.opened}));
    result.errors=errors;process.stdout.write(JSON.stringify(result));
  }finally{await browser.close();}
})().catch(error=>{process.stderr.write(error.stack);process.exit(1);});
'''
    source = Path(__file__).resolve().parents[2] / 'static' / 'mail-people.js'
    completed = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    if result.get('unavailable'): pytest.skip('Browser dependency unavailable: ' + result['unavailable'])
    assert result['errors'] == []
    from urllib.parse import urlparse, parse_qs
    detail_calls = [parse_qs(urlparse(path).query) for path in result['calls'] if '/person/messages?' in path or '/person/pair/messages?' in path]
    assert detail_calls
    for params in detail_calls:
        assert params['address'] == ['a@example.com']
        assert params['date_from'] == ['2024-05-07'] and params['date_to'] == ['2024-05-07']
        assert params['q'] == ['alpha'] and params['has_attachment'] == ['true']
    assert any(params.get('counterpart') == ['b@example.com'] and params['direction'] == ['shared'] for params in detail_calls)
    assert result['opened'][0]['filters']['package_id'] == 'p'
    assert result['opened'][0]['filters']['q'] == 'alpha'


def test_coverage_retains_partial_retry_and_independent_ocr_gaps(foundation_people):
    from services.mail.coverage import coverage_details
    repo,pid,add=foundation_people
    one,raw=add(attachment=True);add(raw=raw)
    two,_=add(attachment=True,subject='partial')
    three,_=add(attachment=True,subject='retry')
    parts={key:next(p['mime_path'] for p in repo.get_message(key,'owner',pid)['parts'] if p['is_explicit_attachment']) for key in (one,two,three)}
    repo.update_processing(one,parts[one],'basic_text','ready',text='available')
    repo.update_processing(one,parts[one],'ocr','terminal_failed',error={'message':'missing page'})
    repo.update_processing(two,parts[two],'basic_text','partial',text='some text',coverage_ratio=.5,warnings=['truncated'])
    repo.update_processing(three,parts[three],'basic_text','waiting_retry',error={'message':'temporary parser failure'})
    with repo._connection() as c:c.execute("UPDATE mail_part_processing SET next_retry_at='2026-10-02T04:00:00+00:00' WHERE message_key=? AND stage='basic_text'",(three,))
    result=coverage_details(repo,pid,'owner',limit=1)
    assert result['total']==result['attachments']['total']==3
    assert result['attachments_without_text']==1
    assert len(result['items'])==1 and result['truncated'] is True
    assert result['by_status']=={'terminal_failed':1,'partial':1,'waiting_retry':1}
    assert result['stages']['basic_text']['by_status']=={'ready':1,'partial':1,'waiting_retry':1}
    assert result['stages']['ocr']=={'total':1,'by_status':{'terminal_failed':1}}
    items={r['message_key']:r for r in coverage_details(repo,pid,'owner')['items']}
    assert items[one]['basic_status']=='ready' and items[one]['gap_stages']==['ocr']
    assert items[two]['warnings']==['truncated']
    assert items[three]['next_retry_at']=='2026-10-02T04:00:00+00:00'
