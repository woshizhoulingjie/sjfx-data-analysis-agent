"""The two-step workflow must read everything and retain exact role-bound sources."""
import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from web_compat import SJFXFastAPI
from config import Config
from services.mail.api import register_mail_routes
from services.mail.bootstrap import build_mail_services
from services.mail.person_corpus import resolve_selection,corpus_state,next_chunks
from services.mail.person_research import run_person_slice,analysis_page
from services.mail.person_activity import (ENGINE,VERSION,FACT_OBJECTIVE,full_corpus,validate_batch,
    validate_source_report,activity_page,_commit,DEEP_WORKFLOW,source_report_basis)
from services.mail.person_activity_api import create_analysis,resume_analysis
from test_person_context_upgrade import person_data,attachment_text,manual_person


def make_turn(repo,pid,mode='full_report',source=None,budget=100,target='agreement'):
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    spec={'mode':'person_correspondence','analysis_version':VERSION,'execution_engine':ENGINE,
        'analysis_mode':mode,'purpose':'activity_report' if mode=='full_report' else 'deep_analysis',
        'package_id':pid,'person_address':selected['address'],'person_addresses':selected['addresses'],
        'person_entity_id':None,'objective':FACT_OBJECTIVE if mode=='full_report' else '对这件事的态度与可能意图是什么？',
        'target':None if mode=='full_report' else target,'source_report_turn_id':source,
        'corpus':full_corpus(repo,pid,'owner',selected),'model_call_budget':budget,'max_tokens':1000000}
    if mode=='deep_analysis':
        parent=validate_source_report(repo,source,'owner',spec)
        spec.update(workflow_revision=DEEP_WORKFLOW,source_report_evidence_hash=source_report_basis(repo,parent)[1])
    session=repo.create_session(pid,'owner','v3 test',{})
    return repo.create_turn(session['session_id'],'owner',spec['objective'],spec)['turn_id']


class Model:
    def __init__(self,deep=False):self.payloads=[];self.deep=deep
    def chat_json(self,system,prompt,**kwargs):
        p=json.loads(prompt);self.payloads.append(p)
        if 'nodes' in p:
            result=[]
            for kind in ['expressed_attitude','intent_hypothesis','context']:
                rows=[n for n in p['nodes'] if n['kind']==kind]
                if rows:
                    result.append({'kind':kind,'title':'有条件地推进约定',
                        'statement':'原文表达了推进约定的态度，同时保留审批条件；现实履行仍不明确。',
                        'source_ids':[n['finding_id'] for n in rows],
                        'uncertainty':'意图仍需核实','alternative_explanations':['也可能只是暂时的安排'],
                        'conditions':['需先完成审批'],'contradictions':[],'unresolved_questions':['未核实现实履行']})
            return {'json':{'sections':result}}
        findings=[]
        for c in p['chunks']:
            if c.get('context_overlap'):continue
            spans=[s for s in p['citation_spans'] if s['chunk_id']==c['chunk_id']]
            text=''.join(s['quote'] for s in spans)
            if c['source_kind']=='attachment' and not self.deep:
                kind='source_information'
            elif c['authored'] and c['source_kind']=='body':
                kind='expressed_attitude' if self.deep else 'action_statement'
            elif 'alice@example.com' in text:
                kind='context' if self.deep else 'third_party_report'
            elif c.get('selected_roles'):
                kind='context' if self.deep else ('received_information' if
                    set(c['selected_roles'])&{'to','cc','bcc'} else 'follow_up')
            else:
                continue
            findings.append({'kind':kind,'statement':'人物提出推进约定，或其他邮件提供了相关背景。',
                'matter':'约定的安排','counterpart':'bob@example.com',
                'conditions':['须先确认审批'],'citations':[{'evidence_id':s['evidence_id']} for s in spans if s['quote'].strip()]})
        return {'json':{'scanned_chunk_ids':[c['chunk_id'] for c in p['chunks'] if not c.get('context_overlap')],'findings':findings},
                'usage':{'input_tokens':50,'output_tokens':50,'total_tokens':100}}


def finish(repo,tid,model,budget=400):
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(200):
            result=run_person_slice(repo,tid,'owner',model,char_budget=budget)
            if not result.get('_requeue_slice'):return activity_page(repo,tid,'owner')
    raise AssertionError('v3 report did not converge')


def raw_findings(chunks,kind):
    c=chunks[0]
    return {'scanned_chunk_ids':[q['chunk_id'] for q in chunks], 'findings':[{
        'kind':kind,'statement':'记录明确的事情和对象','citations':[{
            'chunk_id':c['chunk_id'],'quote':c['text'].strip(),'char_start':c['char_start']+len(c['text'])-len(c['text'].lstrip())}]}]}


def test_full_package_includes_private_mentions_and_attachment_with_no_thread(person_data):
    repo,pid,add=person_data
    add('I will complete the agreement after approval.',attachment=True)
    private=add('alice@example.com has still not delivered the agreement.',sender='bob@example.com',recipient='carol@example.com')
    unrelated=add('A wholly unrelated Carol-only subject.',sender='carol@example.com',recipient='dave@example.com')
    with repo._connection() as c:
        own=c.execute("SELECT message_key FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id WHERE role='from' AND normalized_address='alice@example.com'").fetchone()[0]
    attachment_text(repo,pid,own,'The agreement still requires approval.')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    old=corpus_state(repo,pid,'owner',selected)
    full=full_corpus(repo,pid,'owner',selected)
    assert full['messages']==3 and old['messages']==1 and full['characters']>old['characters']
    tid=make_turn(repo,pid);model=Model();page=finish(repo,tid,model)
    read={c['message_key'] for p in model.payloads for c in p.get('chunks',[])}
    assert private in read and unrelated in read
    assert page['report']['complete'] and page['progress']['scan_complete']
    assert page['progress']['characters_read']==full['characters']
    md=page['report']['markdown']
    assert md.index('## 按时间整理的事件')<md.index('## 附录：原文支撑')
    assert 'alice@example.com has still not delivered' in md
    assert 'The agreement still requires approval.' in md and 'terms.pdf' in md
    assert 'E0001' in md and '[查看邮件原文]' in md
    assert page['report']['source_complete']
    with pytest.raises(LookupError):activity_page(repo,tid,'intruder')


def test_roles_cc_request_and_reality_completion_are_not_promoted(person_data):
    repo,pid,add=person_data
    add('Please send the agreement tomorrow.',sender='bob@example.com',recipient='carol@example.com',cc='alice@example.com')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    for kind in ['action_statement','request_by_person','response_by_person','request_to_person']:
        with pytest.raises(ValueError):validate_batch(raw_findings(chunks,kind),chunks,selected,'activity_report')
    good=raw_findings(chunks,'received_information')
    assert validate_batch(good,chunks,selected,'activity_report')[0]['completion']=='not_established'
    for status in ['completed','self_reported']:
        bad=json.loads(json.dumps(good));bad['findings'][0]['completion']=status
        with pytest.raises(ValueError):validate_batch(bad,chunks,selected,'activity_report')
    add('alice@example.com, please send the agreement tomorrow.',sender='bob@example.com',recipient='carol@example.com',cc='alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    explicit=chunks[-1]
    assert validate_batch(raw_findings([explicit],'request_to_person'),[explicit],selected,'activity_report')


def test_forwarded_and_attachment_text_cannot_support_own_attitude(person_data):
    repo,pid,add=person_data
    key=add('Original message\nI accept this agreement and intend to proceed.',attachment=True)
    attachment_text(repo,pid,key,'I accept this agreement and intend to proceed.')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    for c in chunks:
        with pytest.raises(ValueError):validate_batch(raw_findings([c],'expressed_attitude'),[c],selected,'deep_analysis')


def test_unacknowledged_or_invalid_batch_does_not_skip_evidence_and_keeps_tokens(person_data):
    repo,pid,add=person_data;add('I will complete this agreement after approval.')
    tid=make_turn(repo,pid)
    class Missing(Model):
        def chat_json(self,*args,**kw):
            result=super().chat_json(*args,**kw);result['json']['scanned_chunk_ids']=[];return result
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        with pytest.raises(ValueError):run_person_slice(repo,tid,'owner',Missing())
    state=repo.get_turn(tid,'owner')
    assert state['status']=='failed' and state['final']['cursor']==[0,0] and state['final']['characters_read']==0
    assert state['final']['model_calls']==1 and state['final']['token_usage']['total_tokens']==100
    repo.update_turn(tid,'owner',status='queued')
    page=finish(repo,tid,Model())
    assert page['progress']['batches']==1 and page['report']['complete']


def test_budget_exhaustion_is_partial_and_resume_has_no_duplicate_batches(person_data):
    repo,pid,add=person_data;add('I will complete the agreement. '*60)
    tid=make_turn(repo,pid,budget=1)
    page=finish(repo,tid,Model(),budget=150)
    assert page['status']=='completed' and not page['progress']['complete'] and not page['report']['complete']
    assert page['progress']['budget_exhausted'] and page['progress']['token_usage']['reserved_tokens']==0
    before=repo.get_turn(tid,'owner');spec=before['task_spec'];checkpoint=before['final']
    # Model API continuation uses the same fingerprint and accounting checkpoint.
    services=SimpleNamespace(repository=repo,storage=SimpleNamespace(get_job=lambda *a:None),model=Model())
    result=resume_analysis(services,before,'owner',{'model_call_budget':30},lambda *a:('new-job',True))
    assert result[1]==202
    page=finish(repo,tid,Model(),budget=150)
    assert page['progress']['complete'] and page['progress']['characters_read']==spec['corpus']['characters']
    with repo._connection() as c:
        rows=c.execute("SELECT input_json FROM mail_research_steps WHERE turn_id=? AND tool_name='person_activity_batch'",(tid,)).fetchall()
    cursors=[tuple(json.loads(row[0])['cursor']) for row in rows]
    assert len(cursors)==len(set(cursors))


def test_complete_parent_required_and_bound_to_owner_package_and_person(person_data):
    repo,pid,add=person_data;add('I propose completing the agreement after approval.')
    report_id=make_turn(repo,pid);source=repo.get_turn(report_id,'owner')['task_spec']
    with pytest.raises(ValueError):validate_source_report(repo,report_id,'owner',source)
    finish(repo,report_id,Model())
    assert validate_source_report(repo,report_id,'owner',source)
    for owner,patch_spec in [('intruder',{}),('owner',{'package_id':'other-package'}),
                            ('owner',{'person_addresses':['bob@example.com']}),('owner',{'person_entity_id':'manual-person'})]:
        with pytest.raises(ValueError):validate_source_report(repo,report_id,owner,{**source,**patch_spec})
    add('A newly received item changes the whole package.',sender='bob@example.com',recipient='carol@example.com')
    with pytest.raises(ValueError,match='变化'):validate_source_report(repo,report_id,'owner',source)


def test_deep_rechecks_originals_and_synthesizes_all_themes_with_final_quote_appendix(person_data):
    repo,pid,add=person_data
    add('I plan to complete the agreement, only after approval.')
    add('alice@example.com has not completed the agreement.',sender='bob@example.com',recipient='carol@example.com',date='Wed, 08 May 2024 15:38:50 +0700')
    parent=make_turn(repo,pid);finish(repo,parent,Model())
    deep=make_turn(repo,pid,'deep_analysis',parent,target='agreement')
    model=Model(deep=True);page=finish(repo,deep,model,budget=55)
    assert page['report']['complete'] and page['source_report_turn_id']==parent
    assert all(p['target']=='agreement' for p in model.payloads)
    assert any('nodes' in p for p in model.payloads)
    md=page['report']['markdown']
    assert md.index('## 综合分析')<md.index('## 按时间整理的完整事实底稿')<md.index('## 附录：原文支撑')
    assert 'only after approval' in md and 'has not completed' in md
    assert page['progress']['characters_read']==0 and page['progress']['batches']==0
    assert page['progress']['source_coverage']['characters_read']==page['progress']['corpus']['characters']


def test_missing_attachment_is_explicit_not_claimed_as_read(person_data):
    repo,pid,add=person_data;add('I propose advancing the agreement.',attachment=True)
    page=finish(repo,make_turn(repo,pid),Model())
    assert page['report']['complete'] and not page['report']['source_complete']
    assert page['report']['coverage']['missing_attachment_text']==1
    assert '1个附件没有可读取文本' in page['report']['markdown']


def test_no_event_count_cap_and_exact_event_time(person_data):
    repo,pid,add=person_data;add('I plan delivery on 10/18 subject to approval.')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    value=raw_findings(chunks,'action_statement');value['findings'][0]['event_time']='10/18'
    value['findings']*=37
    assert len(validate_batch(value,chunks,selected,'activity_report'))==37
    value['findings'][0]['event_time']='10/20'
    checked=validate_batch(value,chunks,selected,'activity_report')
    assert 'event_time' not in checked[0] and checked[0]['time_warning']


def test_original_content_mutation_prevents_completing_file(person_data):
    repo,pid,add=person_data;add('I propose completing the agreement.')
    tid=make_turn(repo,pid)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,tid,'owner',Model())
    with repo._connection() as c:
        c.execute("UPDATE mail_text_segments SET text=REPLACE(text,'propose','oppose!') WHERE package_id=?",(pid,))
    with pytest.raises(RuntimeError):finish(repo,tid,Model())
    assert not activity_page(repo,tid,'owner')['report']['complete']


def test_download_owner_bound_utf8_file_and_partial_download_denied(person_data,monkeypatch):
    repo,pid,add=person_data;add('I propose completing the agreement after approval.')
    monkeypatch.setattr(Config,'MAIL_FEATURE_ENABLED',True)
    owner={'value':'owner'}
    services=build_mail_services(repo.storage,model=Model(),owner_resolver=lambda:owner['value'])
    app=SJFXFastAPI();register_mail_routes(app,services)
    tid=make_turn(repo,pid)
    with TestClient(app) as client:
        path='/api/mail/person-analyses/'+tid+'/report'
        assert client.get(path+'/download').status_code==400
        finish(repo,tid,Model())
        result=client.get(path)
        assert result.status_code==200 and result.json()['package_id']==pid and result.json()['progress']['scan_complete']
        file=client.get(path+'/download')
        assert file.status_code==200 and file.content.startswith(b'\xef\xbb\xbf')
        assert '.md' in file.headers['content-disposition'] and '## 附录：原文支撑' in file.content.decode('utf-8-sig')
        completed=repo.get_turn(tid,'owner')['final']
        for patch_fields in ({'scan_complete':False},{'characters_read':completed['characters_read']-1}):
            repo.update_turn(tid,'owner',final={**completed,**patch_fields})
            assert client.get(path+'/download').status_code==400
        repo.update_turn(tid,'owner',final=completed)
        owner['value']='intruder'
        assert client.get(path).status_code==404 and client.get(path+'/download').status_code==404


def test_creation_history_cache_and_deep_binding_through_http(person_data,monkeypatch):
    repo,pid,add=person_data
    add('I intend to complete the agreement after approval.')
    add('alice@example.com must still provide the original agreement.',sender='bob@example.com',recipient='carol@example.com')
    repo.finish_import(pid,'owner')
    monkeypatch.setattr(Config,'MAIL_FEATURE_ENABLED',True)
    identity={'owner':'owner'}
    services=build_mail_services(repo.storage,model=Model(),job_factory=repo.storage.create_or_get_typed_job,
                                 owner_resolver=lambda:identity['owner'])
    app=SJFXFastAPI();register_mail_routes(app,services)
    path='/api/mail/packages/'+pid+'/person/analyses'
    with TestClient(app) as client:
        for invalid_mode in ([],{},True):
            assert client.post(path,json={'address':'alice@example.com','analysis_mode':invalid_mode}).status_code==400
        posted=client.post(path,json={'address':'alice@example.com','analysis_mode':'full_report',
            'question':'ignored scope filter','target':'ignored target','date_from':'2099-01-01','max_tokens':1000000})
        assert posted.status_code==202,posted.text
        body=posted.json();tid=body['turn_id'];spec=repo.get_turn(tid,'owner')['task_spec']
        assert spec['objective']==FACT_OBJECTIVE and spec['target'] is None and spec['corpus']['messages']==2
        premature=client.post(path,json={'address':'alice@example.com','analysis_mode':'deep_analysis',
            'source_report_turn_id':tid,'target':'agreement','question':'他对此态度如何？'})
        assert premature.status_code==400
        finish(repo,tid,Model())
        repo.storage.update_job(body['job_id'],status='completed')
        cached=client.post(path,json={'address':'alice@example.com','analysis_mode':'full_report'})
        assert cached.status_code==202 and cached.json()['cached'] and cached.json()['turn_id']==tid
        history=client.get(path+'?address=alice@example.com').json()['items']
        assert history[0]['analysis_version']==3 and history[0]['report_complete'] and history[0]['analysis_mode']=='full_report'
        rejected=client.post(path,json={'address':'bob@example.com','analysis_mode':'deep_analysis',
            'source_report_turn_id':tid,'target':'agreement','question':'他对此态度如何？'})
        assert rejected.status_code==400
        deep=client.post(path,json={'address':'alice@example.com','analysis_mode':'deep_analysis',
            'source_report_turn_id':tid,'target':'agreement','question':'他对此态度如何？','max_tokens':1000000})
        assert deep.status_code==202,deep.text
        deep_spec=repo.get_turn(deep.json()['turn_id'],'owner')['task_spec']
        assert deep_spec['purpose']=='deep_analysis' and deep_spec['source_report_turn_id']==tid
        identity['owner']='intruder'
        assert client.post(path,json={'address':'alice@example.com','analysis_mode':'full_report'}).status_code==404


def test_cancelled_call_retains_usage_without_advancing_and_resume_is_safe(person_data):
    repo,pid,add=person_data;add('I intend to complete the agreement after approval.')
    tid=make_turn(repo,pid)
    class Cancel(Model):
        def chat_json(self,*a,**kw):
            response=super().chat_json(*a,**kw)
            repo.update_turn(tid,'owner',status='cancelling')
            return response
    from services.mail.research.runtime import JobCancelled
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        with pytest.raises(JobCancelled):run_person_slice(repo,tid,'owner',Cancel())
    turn=repo.get_turn(tid,'owner')
    assert turn['status']=='cancelled' and turn['final']['cursor']==[0,0]
    assert turn['final']['token_usage']['total_tokens']==100 and turn['final']['token_usage']['reserved_tokens']==0
    assert turn['final']['pending_token_reservation'] is None
    services=SimpleNamespace(repository=repo,storage=SimpleNamespace(get_job=lambda *a:None),model=Model())
    assert resume_analysis(services,turn,'owner',{},lambda *a:('resumed',True))[1]==202
    assert finish(repo,tid,Model())['report']['complete']


def test_truncated_output_never_advances_or_hides_a_model_retry(person_data):
    repo,pid,add=person_data;add('I plan to complete the agreement after approval.')
    tid=make_turn(repo,pid)
    class Truncated(Model):
        def chat_json(self,*a,**kw):
            assert kw['structured_retry_limit']==0 and kw['retries']==0
            response=super().chat_json(*a,**kw);response['finish_reason']='MAX_LENGTH';return response
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        with pytest.raises(ValueError,match='截断'):run_person_slice(repo,tid,'owner',Truncated())
    turn=repo.get_turn(tid,'owner')
    assert turn['final']['characters_read']==0 and turn['final']['model_calls']==1
    assert turn['final']['token_usage']['reserved_tokens']==0
    assert not activity_page(repo,tid,'owner')['report']['complete']


def test_operational_checkpoint_reports_actual_current_batch_and_keeps_v3_kinds(person_data):
    repo,pid,add=person_data;add('I intend to complete the agreement after approval.')
    tid=make_turn(repo,pid)
    class Observe(Model):
        def chat_json(self,system,prompt,**kw):
            p=json.loads(prompt);state=repo.get_turn(tid,'owner')['final']
            activity=state['activity']
            assert activity['characters']==sum(len(s['quote']) for s in p['citation_spans'])
            assert activity['message_count']==1 and activity['range']['first_sent'] and activity['started_at']
            assert all(set(c['allowed_kinds'])<=set(['action_statement','request_by_person','response_by_person',
                'received_information','follow_up','third_party_report','request_to_person','source_information']) for c in p['chunks'])
            assert p['chunks'][0]['date_raw']
            schema=kw['output_schema'];acks=schema['properties']['scanned_scan_ids']
            assert kw['required_fields']==('scanned_scan_ids','findings')
            assert acks['minItems']==acks['maxItems']==len(p['scan_manifest']) and 'uniqueItems' not in acks
            assert acks['items']['enum']==p['scan_manifest']
            findings=schema['properties']['findings']
            assert 'maxItems' not in findings
            for variant in findings['items']['oneOf']:
                fields=variant['properties'];kind=fields['kind']['enum'][0]
                assert 'event_time' not in fields
                assert fields['citations']['items']['properties']['evidence_id']['enum']==[
                    s['evidence_id'] for s in p['citation_spans'] if kind in s['allowed_kinds']]
            return super().chat_json(system,prompt,**kw)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,tid,'owner',Observe())
    assert repo.get_turn(tid,'owner')['final']['events']


def test_guided_kind_citations_exclude_unrelated_mail_and_quoted_own_tail(person_data):
    from services.mail.person_activity import bind_span_kinds,reading_output_schema
    from services.mail.person_research import citation_span_payload
    repo,pid,add=person_data
    own=add('I plan to provide the agreement.\n\nOriginal message\nAnother person claims agreement completion.')
    unrelated=add('Please reduce our contract price.',sender='bob@example.com',recipient='carol@example.com')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],5000,full_package=True)
    spans=bind_span_kinds(citation_span_payload(chunks),chunks,selected,'activity_report')
    by_chunk={c['chunk_id']:c for c in chunks}
    for span in spans:
        c=by_chunk[span['chunk_id']]
        if c['message_key']==unrelated:assert span['allowed_kinds']==[]
        if c['message_key']==own and 'Another person' in span['quote']:
            assert not set(span['allowed_kinds'])&{'action_statement','request_by_person','response_by_person'}
    schema=reading_output_schema({'scan_manifest':['c'+str(i+1) for i in range(len(chunks))],
                                 'citation_spans':spans},'activity_report')
    irrelevant={s['evidence_id'] for s in spans if by_chunk[s['chunk_id']]['message_key']==unrelated}
    for variant in schema['properties']['findings']['items']['oneOf']:
        ids=variant['properties']['citations']['items']['properties']['evidence_id']['enum']
        assert not irrelevant.intersection(ids)
    assert schema['properties']['scanned_scan_ids']['minItems']==len(chunks)


def test_guided_unrelated_only_batch_still_requires_full_scan_but_no_person_events():
    from services.mail.person_activity import reading_output_schema
    schema=reading_output_schema({'scan_manifest':['c1','c2'],
        'citation_spans':[{'evidence_id':'q1','allowed_kinds':[]}]},'activity_report')
    assert schema['properties']['findings']['maxItems']==0
    assert schema['properties']['scanned_scan_ids']['items']['enum']==['c1','c2']


def test_short_mail_is_not_cut_at_end_of_another_mail_batch(person_data):
    repo,pid,add=person_data
    add('I will propose the agreement after approval.'+'x'*250)
    second=add('Please send the agreement and original stamped confirmation. '+ 'y'*180,
               sender='bob@example.com',recipient='alice@example.com')
    tid=make_turn(repo,pid);model=Model();page=finish(repo,tid,model,budget=400)
    seen=[c for p in model.payloads if 'draft_findings' not in p for c in p.get('chunks',[]) if c['message_key']==second]
    assert len(seen)==1 and page['progress']['characters_read']==page['progress']['corpus']['characters']


def test_short_scan_manifest_includes_unrelated_sources_without_completing_omitted_acks(person_data):
    repo,pid,add=person_data
    add('I intend to complete the agreement after approval.')
    add('There is an unrelated new purchase.',sender='carol@example.com',recipient='dave@example.com')
    class Short(Model):
        def chat_json(self,system,prompt,**kw):
            p=json.loads(prompt);result=super().chat_json(system,prompt,**kw)
            assert p['scan_manifest']==['c1','c2']
            assert p['chunks'][1]['allowed_kinds']==[] and p['chunks'][1]['selected_roles']==[]
            result['json'].pop('scanned_chunk_ids')
            result['json']['scanned_scan_ids']=p['scan_manifest']
            return result
    tid=make_turn(repo,pid);page=finish(repo,tid,Short(),budget=4000)
    assert page['report']['complete'] and page['progress']['characters_read']==page['progress']['corpus']['characters']
    with repo._connection() as c:
        saved=json.loads(c.execute("SELECT output_ref FROM mail_research_steps WHERE turn_id=? AND tool_name='person_activity_batch'",(tid,)).fetchone()[0])
    assert saved['scanned_scan_ids']==['c1','c2'] and len(saved['scanned_chunk_ids'])==2
    assert all(key.startswith('mseg_') for key in saved['scanned_chunk_ids'])
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    for ack in (['c1'],['c1','c1'],['c1','c3']):
        with pytest.raises(ValueError):validate_batch({'scanned_scan_ids':ack,'findings':[]},chunks,selected,'activity_report')


def test_final_fact_check_keeps_cursor_uncommitted_and_spent_calls_when_check_fails(person_data):
    repo,pid,add=person_data;add('I sent the agreement; approval still pending.')
    tid=make_turn(repo,pid)
    class BadCheck(Model):
        def chat_json(self,system,prompt,**kw):
            p=json.loads(prompt);result=super().chat_json(system,prompt,**kw)
            if 'draft_findings' in p:
                current=repo.get_turn(tid,'owner')['final']
                assert current['characters_read']==0 and current['cursor']==[0,0]
                assert kw['output_context']=='人物事实校核'
                result['json']['scanned_chunk_ids']=[]
            return result
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        with pytest.raises(ValueError):run_person_slice(repo,tid,'owner',BadCheck())
    checkpoint=repo.get_turn(tid,'owner')['final']
    assert checkpoint['characters_read']==0 and checkpoint['model_calls']==2
    assert checkpoint['token_usage']['total_tokens']==200 and checkpoint['pending_token_reservation'] is None
    assert not activity_page(repo,tid,'owner')['report']['complete']


@pytest.mark.parametrize('field',['statement','matter','counterpart','conditions','contradictions','unresolved_questions'])
@pytest.mark.parametrize('kind',['source_information','received_information'])
def test_signature_field_text_cannot_be_promoted_in_any_fact_text(person_data,field,kind):
    from services.mail.person_activity import _normalize_fact_claims
    repo,pid,add=person_data
    key=add('确认单扫描件见附件。',attachment=True,sender='bob@example.com',recipient='alice@example.com')
    attachment_text(repo,pid,key,'交付日期：2026年10月18日\n'
        '交付完成以双方签署《验收报告》之日为准。\n'
        '委托方（签章）：王君\n承建方（签章）：赵涛\n日期：2026年10月9日')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    source=next(c for c in chunks if c['source_kind']=='attachment')
    draft=raw_findings([source],kind)
    claim='文件上有王君和赵涛的签章。'
    draft['findings'][0][field]=[claim] if field in {'conditions','contradictions','unresolved_questions'} else claim
    if field!='statement':
        draft['findings'][0]['statement']='交付日期为2026年10月18日；交付完成以双方签署《验收报告》之日为准。'
    validated=validate_batch(draft,[source],selected,'activity_report')
    result=_normalize_fact_claims(validated)[0]
    actual=result[field][0] if isinstance(result[field],list) else result[field]
    assert claim not in actual and '字段' in actual and '不能证明实际个人签署或盖章' in actual
    assert '委托方（签章）：王君' in actual and '承建方（签章）：赵涛' in actual
    assert '日期：2026年10月9日' in actual
    assert result['citations']==validated[0]['citations']
    if field!='statement':assert result['statement']==validated[0]['statement']


@pytest.mark.parametrize('field',['statement','matter','counterpart','conditions','contradictions','unresolved_questions'])
def test_first_person_ownership_stays_with_actual_source_sender(person_data,field):
    from services.mail.person_activity import _normalize_fact_claims
    repo,pid,add=person_data
    add('贵司此前承诺10月15日交付，现在变更会连带影响我方对业主方的承诺。'
        '最晚须于10月18日交付；10月17日前提交报告。',sender='bob@example.com',recipient='alice@example.com')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    draft=raw_findings(chunks,'request_to_person')
    claim='变更影响李方对业主承诺'
    draft['findings'][0][field]=[claim] if field in {'conditions','contradictions','unresolved_questions'} else claim
    if field!='statement':draft['findings'][0]['statement']='最晚须于10月18日交付；10月17日前提交报告。'
    validated=validate_batch(draft,chunks,selected,'activity_report')
    result=_normalize_fact_claims(validated)[0]
    actual=result[field][0] if isinstance(result[field],list) else result[field]
    assert claim not in actual and '来源发言方（bob@example.com）' in actual
    assert '现在变更会连带影响我方对业主方的承诺' in actual
    assert result['citations']==validated[0]['citations']
    if field!='statement':assert result['statement']==validated[0]['statement']


def test_fact_normalization_runs_after_checker_before_persisting(person_data):
    repo,pid,add=person_data
    add('现在变更会连带影响我方对业主方的承诺。最晚10月18日交付。',
        sender='bob@example.com',recipient='alice@example.com')
    tid=make_turn(repo,pid)
    class FaultyChecker(Model):
        def chat_json(self,system,prompt,**kw):
            p=json.loads(prompt);self.payloads.append(p)
            row={'kind':'request_to_person','statement':('变更影响李方对业主承诺；最晚10月18日交付。'
                if 'draft_findings' in p else '最晚10月18日交付。'),
                'citations':[{'evidence_id':s['evidence_id']} for s in p['citation_spans']]}
            return {'json':{'scanned_scan_ids':p['scan_manifest'],'findings':[row]}}
    model=FaultyChecker();page=finish(repo,tid,model)
    assert len(model.payloads)==2 and page['report']['complete']
    assert '变更影响李方对业主承诺' not in page['report']['markdown']
    assert '来源发言方（bob@example.com）' in page['report']['markdown']
    assert '最晚10月18日交付' in page['report']['markdown']
    with repo._connection() as c:
        saved=json.loads(c.execute("SELECT output_ref FROM mail_research_steps WHERE turn_id=? AND tool_name='person_activity_batch'",(tid,)).fetchone()[0])
    assert '来源发言方（bob@example.com）' in saved['findings'][0]['statement']


def test_ambiguous_ownership_after_checker_fails_without_advancing_or_retry(person_data):
    repo,pid,add=person_data
    add('我方对业主方的承诺不会改变。',sender='bob@example.com',recipient='alice@example.com')
    add('我方对业主方的承诺仍需审批。',sender='carol@example.com',recipient='alice@example.com')
    tid=make_turn(repo,pid)
    class AmbiguousChecker(Model):
        def chat_json(self,system,prompt,**kw):
            p=json.loads(prompt);self.payloads.append(p)
            return {'json':{'scanned_scan_ids':p['scan_manifest'],'findings':[{
                'kind':'received_information','statement':'李方对业主承诺不会改变。',
                'citations':[{'evidence_id':s['evidence_id']} for s in p['citation_spans']]}]}}
    model=AmbiguousChecker()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        with pytest.raises(ValueError,match='无法唯一定位'):run_person_slice(repo,tid,'owner',model,char_budget=4000)
    turn=repo.get_turn(tid,'owner');checkpoint=turn['final']
    assert turn['status']=='failed' and checkpoint['cursor']==[0,0] and checkpoint['characters_read']==0
    assert checkpoint['model_calls']==2 and len(model.payloads)==2
    with repo._connection() as c:
        assert c.execute("SELECT count(*) FROM mail_research_steps WHERE turn_id=? AND tool_name='person_activity_batch'",(tid,)).fetchone()[0]==0


def test_new_fact_engine_rejects_completed_old_engine_parent(person_data):
    repo,pid,add=person_data;add('I propose advancing the agreement.')
    tid=make_turn(repo,pid);finish(repo,tid,Model())
    current=repo.get_turn(tid,'owner')['task_spec']
    assert validate_source_report(repo,tid,'owner',current)
    with repo._connection() as c:
        c.execute('UPDATE mail_research_turns SET task_spec_json=? WHERE turn_id=?',
            (json.dumps({**current,'execution_engine':'person-full-package-activity-v3.3'}),tid))
    with pytest.raises(ValueError,match='完整事实报告'):validate_source_report(repo,tid,'owner',current)


def test_own_signing_report_and_ownership_remain_self_reported(person_data):
    from services.mail.person_activity import _normalize_fact_claims
    repo,pid,add=person_data
    add('我方对业主方的承诺不变；已加盖公司公章的扫描件见附件，纸质原件已寄出。')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    draft=raw_findings(chunks,'action_statement')
    draft['findings'][0].update(statement='我方对业主的承诺不变；已加盖公司公章的扫描件见附件，纸质原件已寄出。',completion='self_reported')
    validated=validate_batch(draft,chunks,selected,'activity_report')
    assert _normalize_fact_claims(validated)==validated


@pytest.mark.parametrize('body',['我方对业主方的承诺不变；贵司对业主方的承诺仍需审批。',
    '转发邮件\n我方对业主方的承诺不变。'])
def test_same_topic_other_party_or_forwarded_voice_cannot_lend_ownership(person_data,body):
    from services.mail.person_activity import _normalize_fact_claims
    repo,pid,add=person_data;add(body,sender='bob@example.com',recipient='alice@example.com')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    draft=raw_findings(chunks,'received_information')
    draft['findings'][0]['statement']='李方对业主承诺不变。'
    validated=validate_batch(draft,chunks,selected,'activity_report')
    with pytest.raises(ValueError,match='无法唯一定位'):_normalize_fact_claims(validated)


def test_signature_assertion_using_by_does_not_become_a_contract_requirement(person_data):
    from services.mail.person_activity import _normalize_fact_claims
    repo,pid,add=person_data;key=add('确认单见附件。',attachment=True)
    attachment_text(repo,pid,key,'委托方（签章）：王君\n承建方（签章）：赵涛')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    source=next(c for c in chunks if c['source_kind']=='attachment')
    draft=raw_findings([source],'source_information')
    draft['findings'][0]['statement']='文件以王君和赵涛的签章确认协议。'
    result=_normalize_fact_claims(validate_batch(draft,[source],selected,'activity_report'))
    assert '不能证明实际个人签署或盖章' in result[0]['statement']


@pytest.mark.parametrize('body',['确认单扫描件见附件。','已填写签章字段，确认单扫描件见附件。',
    '我方已签署确认单，扫描件见附件。'])
def test_signature_fields_cannot_borrow_unrelated_own_body_but_keep_own_claim(person_data,body):
    from services.mail.person_activity import _normalize_fact_claims
    repo,pid,add=person_data;key=add(body,attachment=True)
    attachment_text(repo,pid,key,'委托方（签章）：王君\n承建方（签章）：赵涛')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    draft=raw_findings(chunks,'action_statement')
    draft['findings'][0].update(statement='我方已签署确认单，扫描件见附件。',completion='self_reported',
        citations=[{'chunk_id':c['chunk_id'],'quote':c['text'].strip(),'char_start':c['char_start']} for c in chunks])
    validated=validate_batch(draft,chunks,selected,'activity_report')
    if '已签署' in body:assert _normalize_fact_claims(validated)==validated
    else:
        with pytest.raises(ValueError,match='字段不能支撑本人实际签署'):_normalize_fact_claims(validated)


def test_mailbox_mentions_do_not_match_other_addresses_and_sent_mail_is_not_received(person_data):
    from services.mail.person_activity import _mention,_v3_source_payload
    selected={'addresses':['alice@example.com'],'entity_id':None}
    for text in ('xalice@example.com must send it','alice@example.com.fake must send it',
                 'alice@example.com-extra must send it'):
        assert not _mention({'quote':text},selected)
    for text in ('<ALICE@example.com> must send it','alice@example.com。要求寄送',
                 'Contact alice@example.com. We await a response.'):
        assert _mention({'quote':text},selected)
    repo,pid,add=person_data;add('I sent the agreement.')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],full_package=True)
    assert 'received_information' not in _v3_source_payload(chunks[0],selected,'activity_report')['allowed_kinds']
    with pytest.raises(ValueError,match='实际收件角色'):
        validate_batch(raw_findings(chunks,'received_information'),chunks,selected,'activity_report')


def test_guided_hypothesis_requires_uncertainty_and_alternative_before_generation():
    from services.mail.person_activity import reading_output_schema
    schema=reading_output_schema({'scan_manifest':['c1'],'citation_spans':[
        {'evidence_id':'q1','allowed_kinds':['intent_hypothesis']}]},'deep_analysis')
    variant=schema['properties']['findings']['items']['oneOf'][0]
    assert {'uncertainty','alternative_explanations','completion'}<=set(variant['required'])
    assert variant['properties']['uncertainty']['minLength']==1
    assert variant['properties']['alternative_explanations']['minItems']==1


def test_summary_primary_source_cannot_promote_context_or_hypothesis_to_explicit_attitude():
    from services.mail.person_activity import synthesis_output_schema
    schema=synthesis_output_schema({'nodes':[{'finding_id':'n1','kind':'context'},
        {'finding_id':'n2','kind':'intent_hypothesis'},{'finding_id':'n3','kind':'expressed_attitude'}]})
    variants={v['properties']['kind']['enum'][0]:v for v in schema['properties']['sections']['items']['oneOf']}
    assert variants['context']['properties']['primary_source_id']['enum']==['n1','n2','n3']
    assert variants['intent_hypothesis']['properties']['primary_source_id']['enum']==['n2','n3']
    assert variants['expressed_attitude']['properties']['primary_source_id']['enum']==['n3']
    assert {'uncertainty','alternative_explanations'}<=set(variants['intent_hypothesis']['required'])
    all_context=synthesis_output_schema({'nodes':[{'finding_id':'n1','kind':'context'}]})
    assert len(all_context['properties']['sections']['items']['oneOf'])==1


def test_deep_summary_payload_deduplicates_exact_originals_and_preserves_provenance():
    from services.mail.person_activity import _summary_payload
    attachment={'segment_id':'attachment1','message_key':'mail1','mime_path':'2',
        'char_start':0,'char_end':14,'quote':'承建方（签章）：李强',
        'source_kind':'attachment','authored':True,'quotation_warning':True,
        'sent_at':'2026-10-09T01:20:00+00:00','date_raw':'Fri, 09 Oct 2026 09:20:00 +0800',
        'page_number':1,'selected_roles':['from'],
        'participants':[{'role':'from','address':'alice@example.com'}]}
    body={'segment_id':'body1','message_key':'mail2','mime_path':'1','char_start':0,
        'char_end':49,'quote':'原定到货9月25日，实际10月3日发货；邮件称比原计划晚8天。',
        'source_kind':'body','authored':True,'quotation_warning':False,
        'sent_at':'2026-10-08T01:40:00+00:00','date_raw':'Thu, 08 Oct 2026 09:40:00 +0800',
        'selected_roles':['from'],'participants':[{'role':'from','address':'alice@example.com'}]}
    nodes=[{'finding_id':'long1','kind':'context','statement':'附件中的字段',
            'citations':[attachment,attachment]},
           {'finding_id':'long2','kind':'expressed_attitude','statement':'草稿可能误述日期',
            'citations':[attachment,body]},
           {'finding_id':'long3','kind':'context','statement':'另一次出现的原文',
            'citations':[{**attachment,'char_start':100,'char_end':114}]}]
    payload=_summary_payload(nodes)
    assert [n['finding_id'] for n in payload]==['n1','n2','n3']
    assert payload[0]['evidence_ids']==['s1'] and payload[1]['evidence_ids']==['s1','s2']
    originals=[q for node in payload for q in node['original_evidence']]
    assert [q['evidence_id'] for q in originals]==['s1','s2','s3']
    assert originals[0]['quote']==attachment['quote']
    assert originals[0]['source_kind']=='attachment' and originals[0]['quotation_warning']
    assert originals[0]['authored'] and originals[0]['speaker_addresses']==['alice@example.com']
    assert originals[1]['quote']==body['quote'] and originals[1]['date_raw']==body['date_raw']
    assert originals[1]['source_kind']=='body' and not originals[1]['quotation_warning']
    assert originals[2]['char_start']==100
    assert not any('citations' in node for node in payload)
    assert nodes[0]['finding_id']=='long1' and len(nodes[0]['citations'])==2


def test_deep_synthesis_receives_real_originals_and_displays_hypothesis_kind(person_data):
    repo,pid,add=person_data
    key=add('I propose completing the agreement only after approval.',attachment=True)
    attachment_text(repo,pid,key,'Confirmation form. Contractor signature field: Alice. Approval remains required.')
    add('alice@example.com has still not provided the required approval report.',
        sender='bob@example.com',recipient='carol@example.com')
    source=make_turn(repo,pid);finish(repo,source,Model(),budget=4000)
    source_markdown=activity_page(repo,source,'owner')['report']['markdown']
    class DeepOriginals(Model):
        def __init__(self):super().__init__(deep=True);self.summary_calls=0
        def chat_json(self,system,prompt,**kwargs):
            p=json.loads(prompt)
            if 'nodes' not in p:return super().chat_json(system,prompt,**kwargs)
            self.summary_calls+=1
            originals=[q for node in p['nodes'] for q in node['original_evidence']]
            assert originals and len({q['evidence_id'] for q in originals})==len(originals)
            assert any(q['source_kind']=='attachment' and q['quotation_warning'] for q in originals)
            assert any(q['source_kind']=='body' and q['authored'] and not q['quotation_warning'] for q in originals)
            assert any('approval report' in q['quote'] and not q['authored'] for q in originals)
            assert all(q['sent_at'] and q['date_raw'] for q in originals)
            ids={q['evidence_id'] for q in originals}
            assert all(set(n['evidence_ids'])<=ids for n in p['nodes'])
            own=next(n for n in p['nodes'] if n['kind']=='expressed_attitude')
            return {'json':{'sections':[{'kind':'intent_hypothesis','title':'争取继续推进约定',
                'statement':'希望在取得审批后推进约定，具体动机仍需核实。',
                'primary_source_id':own['finding_id'],'source_ids':[n['finding_id'] for n in p['nodes']],
                'uncertainty':'不能确定内心意图','alternative_explanations':['也可能只是按流程表述条件'],
                'conditions':['原文明确以审批为前提'],'contradictions':[],
                'unresolved_questions':['审批是否完成仍无独立证明']}]}}
    tid=make_turn(repo,pid,'deep_analysis',source,target='agreement');model=DeepOriginals()
    page=finish(repo,tid,model,budget=4000)
    md=page['report']['markdown']
    assert page['report']['complete'] and model.summary_calls==1
    assert '【可能的想法与意图】可能的解释：争取继续推进约定' in md
    assert '一种可能的解释是：希望在取得审批后推进约定' in md
    assert 'Contractor signature field: Alice.' in md
    assert 'approval report' in md
    assert repo.get_turn(source,'owner')['final']['report']['markdown']==source_markdown


