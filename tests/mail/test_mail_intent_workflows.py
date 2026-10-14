"""Synthetic mail only; tests isolate storage and generation."""
import json
from contextlib import nullcontext
from email.message import EmailMessage
from unittest.mock import patch
import pytest
from services.storage import Storage
from services.mail.repository import MailRepository
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.analysis import run_slice, analysis_page, validate_dossier, manifest
from services.mail.analysis_api import make_spec
from services.mail.research.planner import validate_plan

@pytest.fixture
def corpus(tmp_path):
    repo=MailRepository(Storage(tmp_path/'intent.db'),text_segment_chars=350);repo.migrate()
    pkg,_=repo.create_package(owner_id='owner',display_name='Synthetic intent',source_kind='directory',source_path=str(tmp_path),source_revision='v1',config={})
    repo.update_package(pkg['package_id'],'owner',status='ready')
    n=[0]
    def add(body='Please review the contract before agreeing.',sender='alice@example.com',to='bob@example.com',reply=None,attachment=False):
        i=n[0];n[0]+=1;m=EmailMessage();m['From']=sender;m['To']=to;m['Subject']='Contract';m['Message-ID']='<intent-%s@example.com>'%i;m['Date']='Tue, %02d Sep 2026 08:00:00 +0000'%(i+1)
        if reply:m['In-Reply-To']=reply
        m.set_content(body)
        if attachment:m.add_attachment(b'attachment not extracted',maintype='application',subtype='pdf',filename='terms.pdf')
        raw=m.as_bytes();out=repo.upsert_ingested_message(package_id=pkg['package_id'],source={'locator_type':'directory','root_path':str(tmp_path),'source_revision':'v1'},member=MailMember('%s.eml'%i,i,len(raw),{}),raw=raw,parsed=parse_eml(raw));return out['message_key']
    return repo,pkg['package_id'],add

def create(repo,pid,kind,**kwargs):
    spec,_=make_spec(repo,pid,'owner',{'kind':kind,'question':'找出提前付款相关的邮件，分别说明双方条件',**kwargs})
    session=repo.create_session(pid,'owner','Intent test');turn=repo.create_turn(session['session_id'],'owner',spec['objective'],spec)
    return turn['turn_id'],spec

class ScreenModel:
    def __init__(self,matched=False):self.matched=matched;self.calls=[]
    def chat_json(self,system,prompt,**kwargs):
        p=json.loads(prompt);self.calls.append(p);c=p['chunks'][0]
        return {'json':{'decision':'matched' if self.matched or 'advance' in c['text'] else 'not_matched',
            'rationale':'按具体付款含义判定','citations':[{'chunk_id':c['chunk_id'],'quote':c['text'],'char_start':c['char_start']}] if self.matched or 'advance' in c['text'] else []}}

class DossierModel:
    def __init__(self):self.calls=[]
    def chat_json(self,system,prompt,**kwargs):
        p=json.loads(prompt);self.calls.append(p)
        if 'chunks' in p:
            findings=[]
            for c in p['chunks']:
                author=next(x['address'] for x in c['participants'] if x['role']=='from')
                findings.append({'actor':author,'intent':'request','issue':'合同审阅','kind':'action','statement':'请求对方审阅合同','uncertainty':'需核实后续回复','conditions':[],'contradictions':[],'unresolved_questions':[],
                    'citations':[{'chunk_id':c['chunk_id'],'quote':c['text'],'char_start':c['char_start']}]})
            return {'json':{'findings':findings}}
        if 'context_chunks' in p:
            c=p['context_chunks'][0]
            return {'json':{'reviews':[{'finding_id':f['finding_id'],'status':'supported','rationale':'当前上下文支持','conditions':[],'contradictions':[],'unresolved_questions':[],
                'citations':[{'chunk_id':c['chunk_id'],'quote':c['text'],'char_start':c['char_start']}]} for f in p['findings']]}}
        ids=[f['finding_id'] for f in p['findings']]
        return {'json':{'issues':[{'title':'合同审阅','statement':'双方就合同审阅进行沟通','uncertainty':'仅覆盖已收录表达','finding_ids':ids[:8]}]}}

def finish(repo,tid,model,max_steps=100,char_budget=9000):
    with patch('services.model_dispatch.background_generation',lambda *_:nullcontext()):
        for _ in range(max_steps):
            r=run_slice(repo,tid,'owner',model,char_budget=char_budget)
            if not r.get('_requeue_slice'):return analysis_page(repo,tid,'owner')
    raise AssertionError('workflow did not finish')

def test_semantic_screen_saves_matches_and_real_negatives(corpus):
    repo,pid,add=corpus;yes=add('Could you remit an advance before shipment?');no=add('The meeting is tomorrow.');tid,_=create(repo,pid,'screen')
    out=finish(repo,tid,ScreenModel());items=out['batches'];assert {r['item_id']:r['status'] for r in items}=={yes:'matched',no:'not_matched'}
    assert out['progress']['coverage']['research']['messages_checked']==2

def test_screen_verbose_quote_field_still_requires_verbatim_source(corpus):
    from services.mail.analysis import _citations
    from services.mail.person_corpus import next_chunks
    repo,pid,add=corpus;key=add('Final delivery requires the signed acceptance report.')
    chunks,_=next_chunks(repo,pid,'owner',{'addresses':['alice@example.com']},[0,0],related_keys=[key])
    c=next(c for c in chunks if c['source_kind']=='body')
    raw={'chunk_id':c['chunk_id'],'exact_contiguous_original_quote':c['text'],'absolute_char_start':999}
    checked=_citations([raw],chunks)
    assert checked[0]['quote']==c['text'] and checked[0]['char_start']==c['char_start']
    assert checked[0]['offset_relocated']
    with pytest.raises(ValueError):
        _citations([{**raw,'exact_contiguous_original_quote':'Delivery is independently verified complete.'}],chunks)
    with pytest.raises(ValueError):
        _citations([{**raw,'chunk_id':'outside-scope'}],chunks)

def test_unprocessed_attachment_prevents_negative_decision(corpus):
    repo,pid,add=corpus;add('Please see attached.',attachment=True);tid,_=create(repo,pid,'screen')
    assert finish(repo,tid,ScreenModel())['batches'][0]['status']=='unknown'

@pytest.mark.parametrize('budget',[1,4])
def test_screen_repairs_short_quote_without_skipping_or_hiding_budget(corpus,budget):
    repo,pid,add=corpus;key=add('蓝湾项目必须完成双方签署验收报告。');tid,_=create(repo,pid,'screen',model_call_budget=budget)
    class ShortThenExact:
        calls=0
        def chat_json(self,system,prompt,**kwargs):
            self.calls+=1;p=json.loads(prompt);c=next(c for c in p['chunks'] if c['source_kind']=='body')
            if self.calls==1:quote='蓝湾项目'
            else:
                assert 'validation_error' in p and p['previous_output']['citations'][0]['quote']=='蓝湾项目'
                quote=c['text']
            return {'json':{'decision':'matched','rationale':'存在原文相关表达','citations':[{'chunk_id':c['chunk_id'],'quote':quote,'char_start':c['char_start']}]}}
    out=finish(repo,tid,ShortThenExact())
    assert out['progress']['model_calls']==min(budget,2)
    if budget==1:
        assert out['progress']['budget_exhausted'] and not out['progress']['complete']
        assert out['progress'].get('message_index',0)==0 and out['progress']['characters_read']==0
        assert out['batches'][0]['status']=='unknown'
    else:
        assert out['progress']['complete'] and out['batches'][0]['status']=='matched'
        assert out['progress']['message_index']==1

def test_long_mail_is_fully_read_and_match_near_end_survives(corpus):
    repo,pid,add=corpus;add('Meeting information. '*120+'Could you remit an advance?');tid,_=create(repo,pid,'screen');model=ScreenModel()
    out=finish(repo,tid,model,char_budget=350);assert out['batches'][0]['status']=='matched';assert sum(sum(len(c['text']) for c in p['chunks']) for p in model.calls)==out['progress']['corpus']['characters']

def test_budget_stop_preserves_unread_mail_and_cursor(corpus):
    repo,pid,add=corpus;add('Background. '*150+'advance');tid,_=create(repo,pid,'screen',model_call_budget=1)
    out=finish(repo,tid,ScreenModel(),char_budget=350);assert out['progress']['budget_exhausted'];assert not out['progress']['complete'];assert out['batches'][0]['status']=='unknown';assert out['progress']['cursor']!=[0,0]

def test_pair_scope_does_not_include_other_counterparty(corpus):
    repo,pid,add=corpus;ab=add();ac=add(to='carol@example.com');tid,spec=create(repo,pid,'pair',address='alice@example.com',counterpart='bob@example.com')
    assert ab in spec['message_keys'] and ac not in spec['message_keys'];out=finish(repo,tid,DossierModel());assert out['progress']['complete'];assert out['batches'][0]['findings'][0]['actor']=='alice@example.com'

def test_wrong_actor_cannot_inherit_sender_intent(corpus):
    repo,pid,add=corpus;key=add();_,spec=create(repo,pid,'message',message_key=key)
    from services.mail.person_corpus import next_chunks
    chunks,_=next_chunks(repo,pid,'owner',{'addresses':spec['parties']},[0,0],related_keys=[key]);c=chunks[0]
    value={'findings':[{'actor':'bob@example.com','kind':'goal','intent':'request','issue':'合同','statement':'要求审核','uncertainty':'需核实','citations':[{'chunk_id':c['chunk_id'],'quote':c['text'],'char_start':c['char_start']}]}]}
    with pytest.raises(ValueError):validate_dossier(value,chunks,spec['parties'],{key})

def test_question_deadline_needs_its_own_source_paragraph(corpus):
    from services.mail.person_corpus import next_chunks
    repo,pid,add=corpus
    first='报价90,000.00元/台，需经财务审批后生效。'
    key=add(first+'\n财务预计2026年9月17日完成审批。')
    _,spec=create(repo,pid,'message',message_key=key)
    chunks,_=next_chunks(repo,pid,'owner',{'addresses':spec['parties']},[0,0],related_keys=[key])
    c=next(c for c in chunks if c['source_kind']=='body')
    row={'actor':'alice@example.com','kind':'action','intent':'accept','issue':'采购价格',
         'statement':first,'uncertainty':'审批仍待完成','conditions':[],
         'contradictions':[],'unresolved_questions':['财务是否于2026年9月17日完成审批？'],
         'citations':[{'chunk_id':c['chunk_id'],'quote':first,'char_start':c['char_start']}]}
    with pytest.raises(ValueError,match=r'unresolved_questions\[0\].*17'):
        validate_dossier({'findings':[row]},chunks,spec['parties'],{key})
    row['citations'][0]['quote']=c['text']
    assert len(validate_dossier({'findings':[row]},chunks,spec['parties'],{key}))==1


def test_prompt_changes_invalidate_cache_without_invalidating_checkpoint(corpus,monkeypatch):
    from services.mail import analysis_api
    repo,pid,add=corpus;add()
    first,_=analysis_api.make_spec(repo,pid,'owner',{'kind':'screen','question':'查找采购条件'})
    monkeypatch.setattr(analysis_api,'PROMPT_FINGERPRINT','changed-prompt')
    second,_=analysis_api.make_spec(repo,pid,'owner',{'kind':'screen','question':'查找采购条件'})
    assert first['cache_key']!=second['cache_key']
    assert first['engine_version']==second['engine_version'] and first['manifest']==second['manifest']


def test_model_payload_retains_evidence_and_attribution_without_storage_bloat(corpus):
    from services.mail.person_corpus import next_chunks
    from services.mail.analysis import _model_payload
    repo,pid,add=corpus;key=add('We accept only after approval.\n> The supplier promises immediate delivery.')
    _,spec=create(repo,pid,'message',message_key=key)
    chunks,cursor=next_chunks(repo,pid,'owner',{'addresses':spec['parties']},[0,0],related_keys=[key])
    before=json.dumps(chunks,ensure_ascii=False)
    projected=_model_payload({'chunks':chunks})['chunks']
    assert json.dumps(chunks,ensure_ascii=False)==before
    for source,shown in zip(chunks,projected):
        for field in ('chunk_id','message_key','text','char_start','char_end','source_kind','mime_path','body_fragments','quoted_ranges','quotation_boundary'):
            assert shown[field]==source[field]
        assert [(p['role'],p['address']) for p in shown['participants']]==[(p['role'],p['address']) for p in source['participants']]
        assert 'source_revision' not in shown and 'text_sha256' not in shown and 'package_id' not in shown
    assert len(json.dumps(projected,ensure_ascii=False))<len(before)

def test_actual_text_change_invalidates_final_manifest(corpus):
    repo,pid,add=corpus;key=add();_,spec=create(repo,pid,'message',message_key=key)
    with repo._connection() as c:c.execute("UPDATE mail_text_segments SET text=replace(text,'Please','Never!') WHERE message_key=?",(key,))
    assert manifest(repo,pid,'owner',spec['context_keys'])['fingerprint']!=spec['manifest']['fingerprint']

def test_explicit_scope_is_never_replaced_by_model_guess():
    p=validate_plan({'goal':'付款','criteria':['确认提前付款'],'queries':['advance'],'participant':'evil@example.com','date_from':'2020-01-01'},'研究 alice@example.com 的目标',{'participant':'alice@example.com','date_from':'2026-09-01'})
    assert p['scope']=={'participant':'alice@example.com','date_from':'2026-09-01'}

def test_other_owner_cannot_start_or_read_analysis(corpus):
    repo,pid,add=corpus;add()
    with pytest.raises(ValueError):make_spec(repo,pid,'intruder',{'kind':'screen','question':'付款'})
    tid,_=create(repo,pid,'screen')
    with pytest.raises(LookupError):analysis_page(repo,tid,'intruder')


def test_api_queue_cache_resume_and_owner_isolation(corpus,monkeypatch):
    from fastapi.testclient import TestClient
    from config import Config
    from web_compat import SJFXFastAPI
    from services.mail.bootstrap import build_mail_services
    from services.mail.api import register_mail_routes
    from services.mail.jobs import _research
    repo,pid,add=corpus;key=add('advance payment '+ 'background '*1500)
    identity={'owner':'owner'};monkeypatch.setattr(Config,'MAIL_FEATURE_ENABLED',True)
    services=build_mail_services(repo.storage,model=ScreenModel(),job_factory=repo.storage.create_or_get_typed_job,owner_resolver=lambda:identity['owner'])
    app=SJFXFastAPI();register_mail_routes(app,services)
    with TestClient(app) as client,patch('services.model_dispatch.background_generation',lambda *_:nullcontext()):
        path='/api/mail/packages/'+pid+'/intent-analyses'
        r=client.post(path,json={'kind':'screen','question':'提前付款','model_call_budget':1})
        assert r.status_code==202,r.text
        tid=r.json()['turn_id'];jid=r.json()['job_id'];job=repo.storage.get_job(jid,'owner')
        assert job['task_type']=='mail_research' and job['options']['turn_id']==tid
        for _ in range(20):
            result=_research(job,services)
            if not result.get('_requeue_slice'):break
        page=client.get('/api/mail/intent-analyses/'+tid).json()
        assert page['progress']['budget_exhausted'] and not page['progress']['complete']
        with repo._connection() as c:c.execute("UPDATE analysis_jobs SET status='completed' WHERE id=?",(jid,))
        same=client.post('/api/mail/intent-analyses/'+tid+'/resume',json={'model_call_budget':1})
        assert same.status_code==400
        r=client.post('/api/mail/intent-analyses/'+tid+'/resume',json={'model_call_budget':20})
        assert r.status_code==202,r.text
        job=repo.storage.get_job(r.json()['job_id'],'owner')
        for _ in range(30):
            result=_research(job,services)
            if not result.get('_requeue_slice'):break
        page=client.get('/api/mail/intent-analyses/'+tid).json()
        assert page['progress']['complete'] and page['batches'][0]['status']=='matched'
        assert repo.get_result_set(page['progress']['result_set_id'],'owner')['final']['complete']
        calls=len(services.model.calls)
        assert not _research(job,services).get('_requeue_slice') and len(services.model.calls)==calls
        cached=client.post(path,json={'kind':'screen','question':'提前付款','model_call_budget':20}).json()
        assert cached['cached'] and cached['turn_id']==tid
        identity['owner']='intruder'
        assert client.get('/api/mail/intent-analyses/'+tid).status_code==404
        assert client.post('/api/mail/intent-analyses/'+tid+'/resume',json={}).status_code==404


def test_scoped_vector_hit_survives_outside_scope_top_hit(corpus):
    import numpy as np
    from services.mail.semantic import vector_search,model_key
    repo,pid,add=corpus;inside=add('Could you remit an advance?');outside=add('Payment due now',sender='carol@example.com')
    class Embed:
        model='synthetic'
        def embed(self,texts):return [[1.,0.]]
    client=Embed()
    with repo._connection() as c:
        for row in c.execute('SELECT * FROM mail_text_segments WHERE package_id=?',(pid,)).fetchall():
            v=np.array([.8,.6] if row['message_key']==inside else [1.,0.],dtype=np.float32)
            c.execute('INSERT INTO mail_embeddings(package_id,segment_id,model,dim,vector,text_sha256,index_epoch) VALUES(?,?,?,?,?,?,?)',(pid,row['segment_id'],model_key(client),2,v.tobytes(),row['text_sha256'],1))
    hits,status=vector_search(repo,pid,'owner','预付货款',client=client,allowed_keys={inside},limit=1)
    assert len(hits)==1 and hits[0]['message_key']==inside and status['scope_applied_before_limit']
    assert not vector_search(repo,pid,'owner','付款',client=client,allowed_keys=set(),limit=1)[0]


def test_relevance_batches_candidates_and_retains_synthesis_budget(corpus):
    from services.mail.research.relevance import classify
    repo,pid,add=corpus;keys=[add('advance payment') for _ in range(8)]
    class Runtime:
        repository=repo;context_limit=24000
        def _model_call(self,tid,owner,state,purpose,system,prompt,**kwargs):
            state['model_calls']+=1;p=json.loads(prompt)
            return {'decisions':[{'message_key':m['message_key'],'status':'matched','rationale':'相关','evidence_id':m['evidence'][0]['evidence_id'],'source_quote':m['evidence'][0]['text']} for m in p['messages']]}
    tid,spec=create(repo,pid,'screen');state={'max_model_calls':4,'model_calls':0}
    result=classify(Runtime(),tid,'owner',state,spec,[{'message_key':key} for key in keys])
    assert len(result)==8 and state['model_calls']==2 and all(d['status']=='matched' for d in state['relevance_decisions'].values())


def test_attachment_exclusion_is_preserved_and_does_not_block_negative(corpus):
    repo,pid,add=corpus;add('Meeting tomorrow',attachment=True)
    tid,spec=create(repo,pid,'screen',scope={'include_explicit_attachments':False})
    assert spec['scope']['include_explicit_attachments'] is False
    assert finish(repo,tid,ScreenModel())['batches'][0]['status']=='not_matched'


def test_followup_filter_keeps_relevance_status_and_evidence(corpus):
    repo,pid,add=corpus;key=add(attachment=True)
    rid=repo.create_result_set(pid,'owner',[{'item_id':key,'item_type':'message','status':'unknown','rationale':{'reason':'未读完'}}],{}, {})
    filtered=repo.filter_result_set(rid,'owner',has_attachment=True)
    item=repo.get_result_set(filtered,'owner')['items'][0]
    assert item['status']=='unknown' and item['rationale']['reason']=='未读完'


def test_invalid_change_is_repaired_without_advancing_cursor(corpus):
    repo,pid,add=corpus;key=add('Please review the contract.');tid,_=create(repo,pid,'message',message_key=key)
    class Repair(DossierModel):
        def __init__(self):super().__init__();self.bad=False
        def chat_json(self,system,prompt,**kwargs):
            value=super().chat_json(system,prompt,**kwargs)
            if 'chunks' in json.loads(prompt) and not self.bad:
                self.bad=True;value['json']['findings'][0]['kind']='change'
            return value
    model=Repair();out=finish(repo,tid,model)
    assert out['progress']['complete'] and any('validation_error' in p for p in model.calls)
    assert out['progress']['characters_read']==out['progress']['corpus']['characters']


def test_fast_manifest_matches_strong_metadata_and_alias_citations_are_checked(corpus):
    from services.mail.analysis import _citations
    from services.mail.person_corpus import next_chunks
    repo,pid,add=corpus;key=add('Please review this contract.')
    assert manifest(repo,pid,'owner',[key])['metadata_fingerprint']==manifest(repo,pid,'owner',[key],strong=False)['metadata_fingerprint']
    chunks,_=next_chunks(repo,pid,'owner',{'addresses':['alice@example.com']},[0,0],related_keys=[key]);c=chunks[0]
    raw={'chunk_id':c['chunk_id'],'exact_original_quote':c['text'],'absolute_char_start':c['char_start']}
    assert _citations([raw],chunks)[0]['quote']==c['text']
    relocated=_citations([{**raw,'absolute_char_start':999}],chunks)[0]
    assert relocated['char_start']==c['char_start'] and relocated['offset_relocated']
    duplicate={**c,'text':c['text']+c['text']}
    with pytest.raises(ValueError):_citations([{**raw,'absolute_char_start':999}],[duplicate])


def test_header_only_candidate_cannot_be_declared_irrelevant(corpus):
    from services.mail.research.relevance import classify
    repo,pid,add=corpus;key=add('Header-only extracted text')
    with repo._connection() as c:c.execute("UPDATE mail_text_segments SET source_kind='header' WHERE message_key=?",(key,))
    class Runtime:
        repository=repo;context_limit=24000
        def _model_call(self,tid,owner,state,purpose,system,prompt,**kwargs):
            state['model_calls']+=1
            return {'decisions':[{'message_key':key,'status':'not_matched','rationale':'片段不相关'}]}
    tid,spec=create(repo,pid,'screen');state={'max_model_calls':4,'model_calls':0}
    assert classify(Runtime(),tid,'owner',state,spec,[{'message_key':key}])
    assert state['relevance_decisions'][key]['status']=='unknown'


@pytest.mark.parametrize('kind',['message','pair','screen'])
def test_high_analysis_default_and_service_ceiling_keep_fixed_scope(corpus,kind):
    from config import Config
    repo,pid,add=corpus;key=add()
    payload={'kind':kind,'question':'分析付款与审批条件'}
    if kind=='message':payload['message_key']=key
    if kind=='pair':payload.update(address='alice@example.com',counterpart='bob@example.com')
    initial,_=make_spec(repo,pid,'owner',payload)
    assert initial['model_call_budget']==Config.MAIL_ANALYSIS_MAX_MODEL_CALLS==4096
    assert initial['max_tokens']==Config.MAIL_RESEARCH_MAX_TOKENS
    raised,_=make_spec(repo,pid,'owner',{**payload,'model_call_budget':32768,'max_tokens':100000000})
    assert raised['message_keys']==initial['message_keys']==[key]
    assert raised['cache_key']==initial['cache_key']
    for option,value in [('model_call_budget',32769),('max_tokens',100000001),('model_call_budget',True)]:
        with pytest.raises(ValueError):make_spec(repo,pid,'owner',{**payload,option:value})
