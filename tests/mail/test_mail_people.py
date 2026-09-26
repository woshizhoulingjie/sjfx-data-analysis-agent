import json
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from services.storage import Storage
from services.mail.repository import MailRepository
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.people import contacts, correspondence, relationships, corpus_state, next_chunks, authorize
from services.mail.person_research import validate_findings, run_person_slice, analysis_page
from services.mail.research.multilingual import balanced_queries, intent_mode

@pytest.fixture
def corpus(tmp_path):
    repo=MailRepository(Storage(tmp_path/'mail.sqlite3'),text_segment_chars=300)
    repo.migrate()
    package,_=repo.create_package(owner_id='owner',display_name='Synthetic trilingual test',source_kind='directory',source_path=str(tmp_path),source_revision='test1',config={})
    counter=[0]
    def add(body, sender='alice@example.com', recipient='bob@example.com', cc=None, bcc=None, raw=None):
        n=counter[0];counter[0]+=1
        if raw is None:
            msg=EmailMessage();msg['From']=sender;msg['To']=recipient;msg['Subject']='计划 / Plan / แผนงาน';msg['Message-ID']='<m%d@example.com>'%n
            msg['Date']='Tue, 07 May 2024 15:38:50 +0700'
            if cc:msg['Cc']=cc
            if bcc:msg['Bcc']=bcc
            msg.set_content(body);raw=msg.as_bytes()
        result=repo.upsert_ingested_message(package_id=package['package_id'],source={'locator_type':'directory','root_path':str(tmp_path),'container_path':None,'container_sha256':'test1','source_revision':'test1'},member=MailMember('m%d.eml'%n,n,len(raw),{'kind':'file','path':'unused'}),raw=raw,parsed=parse_eml(raw))
        return result,raw
    return repo,package['package_id'],add

def test_contacts_dedup_roles_and_isolation(corpus):
    repo,pid,add=corpus
    _,raw=add('我们计划下周签约。',cc='carol@example.com',bcc='dave@example.com')
    add('',raw=raw)
    add('We need to review the conditions.',sender='bob@example.com',recipient='alice@example.com')
    add('เราต้องการตรวจสอบเงื่อนไขก่อนลงนาม',sender='alice@example.com',recipient='alice@example.com')
    result=correspondence(repo,pid,'owner','alice@EXAMPLE.COM')
    assert result['total']==3 and result['counts']=={'sent':2,'received':2,'total':3}
    assert correspondence(repo,pid,'owner','dave@example.com',direction='received')['total']==1
    assert correspondence(repo,pid,'owner','carol@example.com',direction='cc')['total']==1
    assert contacts(repo,pid,'owner',query='%')['total']==0
    assert contacts(repo,pid,'owner')['total']==4
    pages=[correspondence(repo,pid,'owner','alice@example.com',offset=n,limit=1)['items'][0]['message_key'] for n in range(3)]
    assert len(set(pages))==3
    repo.finish_import(pid,'owner')
    assert relationships(repo,pid,'owner','alice@example.com')['total']>=2
    with pytest.raises(LookupError):contacts(repo,pid,'intruder')
    for invalid in (None,'',[],{}):
        with pytest.raises(ValueError):authorize(repo,pid,'owner',invalid)
    with pytest.raises(LookupError):authorize(repo,pid,'owner','missing@example.com')

def test_cursor_visits_long_three_language_body(corpus):
    repo,pid,add=corpus
    add(('中文 English ภาษาไทย ประชุม讨论。'*900))
    before=corpus_state(repo,pid,'owner','alice@example.com')
    seen=[];cursor=[0,0];characters=0
    for _ in range(300):
        chunks,new=next_chunks(repo,pid,'owner','alice@example.com',cursor,char_budget=733)
        if not chunks:break
        assert new!=cursor
        seen.extend(c['chunk_id'] for c in chunks);characters+=sum(len(c['text']) for c in chunks);cursor=new
    assert len(seen)==len(set(seen))
    assert characters==before['characters'] and characters>20000
    add('新的邮件将改变指纹。')
    assert corpus_state(repo,pid,'owner','alice@example.com')['fingerprint']!=before['fingerprint']

def test_empty_segments_do_not_finish_early(corpus):
    repo,pid,add=corpus;add('x'*9000+'后面仍有真实正文内容。')
    with repo._connection() as c:
        c.execute("UPDATE mail_text_segments SET text='' WHERE rowid IN (SELECT rowid FROM mail_text_segments ORDER BY rowid LIMIT 25)")
    chunks,_=next_chunks(repo,pid,'owner','alice@example.com',[0,0])
    assert chunks and sum(len(c['text']) for c in chunks)>0

def finding(chunk,kind='goal',quote=None):
    return {'kind':kind,'statement':'希望推进计划','uncertainty':'需要进一步核实','citations':[{'chunk_id':chunk['chunk_id'],'quote':quote or chunk['text'][:30]}]}

def test_cross_segment_forwarded_text_is_not_own_intent(corpus):
    repo,pid,add=corpus;add('Original message'+chr(10)+'They demand that we sign immediately. '*80)
    chunks,_=next_chunks(repo,pid,'owner','alice@example.com',[0,0])
    later=next(c for c in chunks if c['char_start']>=300)
    assert validate_findings({'findings':[finding(later)]},[later])==([],1)

def test_evidence_validation_is_strict(corpus):
    repo,pid,add=corpus;add('I intend to complete the agreement next week.')
    chunks,_=next_chunks(repo,pid,'owner','alice@example.com',[0,0]);chunk=chunks[0]
    assert len(validate_findings({'findings':[finding(chunk)]},chunks)[0])==1
    for bad in ({'kind':[]},{'citations':[{'chunk_id':[],'quote':'something'}]},{'citations':[]},{'citations':[{'chunk_id':chunk['chunk_id'],'quote':'invented quotation'}]},{'uncertainty':[]}):
        row=finding(chunk);row.update(bad);assert validate_findings({'findings':[row]},chunks)==([],1)
    incoming={**chunk,'authored':False}
    assert validate_findings({'findings':[finding(incoming)]},[incoming])==([],1)
    assert len(validate_findings({'findings':[finding(incoming,'received')]},[incoming])[0])==1
    with pytest.raises(ValueError):validate_findings({'findings':[finding(chunk)]*13},chunks)

def test_checkpoint_and_no_model(corpus):
    repo,pid,add=corpus;add('I intend to complete the agreement next week. '*35)
    spec={'mode':'person_correspondence','package_id':pid,'person_address':'alice@example.com','corpus':corpus_state(repo,pid,'owner','alice@example.com')}
    session=repo.create_session(pid,'owner','test',{})
    turn=repo.create_turn(session['session_id'],'owner','Goals?',spec);tid=turn['turn_id']
    with pytest.raises(RuntimeError):run_person_slice(repo,tid,'owner',None)
    assert repo.get_turn(tid,'owner')['status']=='failed'
    class Model:
        def chat_json(self,system,prompt,**kw):
            payload=json.loads(prompt)
            if 'evidence' in payload:
                f=payload['evidence'][0]
                return {'json':{'themes':[{'title':'计划目标','statement':'提出了推进计划的意向','uncertainty':'需人工核实原文','finding_ids':[f['finding_id']]}]}}
            chunk=payload['chunks'][0]
            return {'json':{'findings':[finding(chunk)]}}
    with patch('services.model_dispatch.background_generation',lambda *a:__import__('contextlib').nullcontext()):
        for _ in range(50):
            result=run_person_slice(repo,tid,'owner',Model(),char_budget=400)
            if not result.get('_requeue_slice'):break
    page=analysis_page(repo,tid,'owner')
    assert page['status']=='completed'
    assert page['progress']['characters_read']==spec['corpus']['characters']
    assert page['items'] and page['batch_total']>1
    with pytest.raises(LookupError):analysis_page(repo,tid,'intruder')

@pytest.mark.parametrize('text,mode',[('全部邮件','exhaustive'),('all correspondence','exhaustive'),('ทุกฉบับ','exhaustive'),('分析计划','research'),('analyze intentions','research'),('วิเคราะห์','research'),('Alice','lookup')])
def test_language_intents(text,mode):
    assert intent_mode(text)==mode

def test_balanced_queries_preserve_objective():
    queries=balanced_queries('研究计划',['ภาษาไทย','ข้อเสนอ','การประชุม','合同','agreement','plan'],['th','zh','en'])
    assert queries[0]=='研究计划' and '合同' in queries and 'agreement' in queries and 'ภาษาไทย' in queries

def test_long_segment_fills_batch_without_losing_characters(corpus):
    repo,pid,add=corpus;repo.text_segment_chars=20000;add('x'*19000)
    cursor=[0,0];sizes=[]
    while True:
        chunks,cursor=next_chunks(repo,pid,'owner','alice@example.com',cursor)
        if not chunks:break
        sizes.append(sum(len(c['text']) for c in chunks))
    assert sizes==[9000,9000,1000]

def test_bad_model_does_not_advance_checkpoint(corpus):
    repo,pid,add=corpus;add('We intend to complete this agreement next week.')
    spec={'mode':'person_correspondence','package_id':pid,'person_address':'alice@example.com','corpus':corpus_state(repo,pid,'owner','alice@example.com')}
    session=repo.create_session(pid,'owner','test',{})
    turn=repo.create_turn(session['session_id'],'owner','Goals?',spec);tid=turn['turn_id']
    class BadModel:
        def chat_json(self,*a,**kw):return {'json':{'findings':[{'kind':'goal','statement':'幻觉','citations':[{'chunk_id':'unknown','quote':'fabricated words'}]}]}}
    with patch('services.model_dispatch.background_generation',lambda *a:__import__('contextlib').nullcontext()):
        with pytest.raises(ValueError):run_person_slice(repo,tid,'owner',BadModel())
    assert repo.get_turn(tid,'owner')['status']=='failed'
    assert not repo.get_turn(tid,'owner').get('final')
    assert analysis_page(repo,tid,'owner')['batch_total']==0

def test_cancel_and_source_change_do_not_advance(corpus):
    from services.mail.person_research import JobCancelled
    repo,pid,add=corpus;add('We plan to complete this agreement next week.')
    spec={'mode':'person_correspondence','package_id':pid,'person_address':'alice@example.com','corpus':corpus_state(repo,pid,'owner','alice@example.com')}
    session=repo.create_session(pid,'owner','test',{})
    turn=repo.create_turn(session['session_id'],'owner','Goals?',spec);tid=turn['turn_id']
    with pytest.raises(JobCancelled):run_person_slice(repo,tid,'owner',None,cancelled=lambda:True)
    assert not repo.get_turn(tid,'owner').get('final')
    repo.update_turn(tid,'owner',status='queued');add('Additional correspondence changes the corpus.')
    with pytest.raises(RuntimeError):run_person_slice(repo,tid,'owner',None)
    assert not repo.get_turn(tid,'owner').get('final')

def test_person_api_scope_inputs_and_resume(corpus,monkeypatch):
    from config import Config
    from web_compat import SJFXFastAPI
    from fastapi.testclient import TestClient
    from services.mail.people_api import register_people_routes
    repo,pid,add=corpus;add('We intend to complete the project next week.');repo.finish_import(pid,'owner')
    owner=['owner'];jobs={};count=[0]
    def queue(package_id,task_type,options,owner_id):
        count[0]+=1;jid='fake%d'%count[0];jobs[jid]={'status':'queued'};return jid,True
    services=SimpleNamespace(repository=repo,model=object(),owner_resolver=lambda:owner[0],job_factory=queue,storage=SimpleNamespace(get_job=lambda jid,owner:jobs.get(jid)))
    monkeypatch.setattr(Config,'MAIL_FEATURE_ENABLED',True)
    app=SJFXFastAPI();register_people_routes(app,services);client=TestClient(app)
    base='/api/mail/packages/'+pid
    assert client.get(base+'/people').status_code==200
    assert client.get(base+'/person/messages').status_code==400
    assert client.post(base+'/person/analyses',json=['invalid']).status_code==400
    response=client.post(base+'/person/analyses',json={'address':'alice@example.com'})
    assert response.status_code==202,response.text
    tid=response.json()['turn_id'];job=response.json()['job_id']
    assert client.get('/api/mail/person-analyses/'+tid).status_code==200
    assert client.post('/api/mail/person-analyses/'+tid+'/resume').status_code==400
    owner[0]='other'
    assert client.get(base+'/people').status_code==404
    assert client.get('/api/mail/person-analyses/'+tid).status_code==404
    owner[0]='owner';jobs[job]['status']='failed'
    assert client.get('/api/mail/person-analyses/'+tid).json()['status']=='failed'
    assert client.post('/api/mail/person-analyses/'+tid+'/resume').status_code==202
    assert client.post('/api/mail/person-analyses/'+tid+'/resume').status_code==400
    monkeypatch.setattr(Config,'MAIL_FEATURE_ENABLED',False)
    assert client.get(base+'/people').status_code==404
