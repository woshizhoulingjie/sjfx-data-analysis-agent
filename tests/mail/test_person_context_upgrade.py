"""Synthetic person analysis only: all model output is mocked, no network calls."""
import hashlib
import json
from contextlib import nullcontext
from email.message import EmailMessage
from unittest.mock import patch

import pytest
from services.storage import Storage
from services.mail.repository import MailRepository
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.person_corpus import (resolve_selection, selection_for_spec, corpus_state, next_chunks,
                                      PERSON_DEFAULT_MODEL_CALL_BUDGET, PERSON_MAX_MODEL_CALL_BUDGET)
from services.mail.person_research import run_person_slice, analysis_page, validate_findings, JobCancelled, _commit_v2
from services.mail.person_context import validate_reviews


@pytest.fixture
def person_data(tmp_path, monkeypatch):
    # These synthetic providers exercise full reading/review contracts and
    # assume a 32K service. Capacity tests explicitly override this locally.
    from config import Config
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 32768)
    repo=MailRepository(Storage(tmp_path/'person.sqlite3'),text_segment_chars=350)
    repo.migrate()
    package,_=repo.create_package(owner_id='owner',display_name='Person context synthetic',source_kind='directory',
                    source_path=str(tmp_path),source_revision='p1',config={})
    pid=package['package_id'];serial=[0]
    def add(body='I intend to complete this agreement after checking the terms.',sender='alice@example.com',
            recipient='bob@example.com',cc=None,bcc=None,date='Tue, 07 May 2024 15:38:50 +0700',attachment=False,html=False):
        n=serial[0];serial[0]+=1
        msg=EmailMessage();msg['From']=sender;msg['To']=recipient;msg['Subject']='Plan / 条件 / เงื่อนไข'
        msg['Message-ID']='<person-%d@example.com>'%n;msg['Date']=date
        if cc:msg['Cc']=cc
        if bcc:msg['Bcc']=bcc
        msg.set_content(body,subtype='html' if html else 'plain')
        if attachment:msg.add_attachment(b'Contract requires approval before payment.',maintype='application',subtype='pdf',filename='terms.pdf')
        raw=msg.as_bytes()
        return repo.upsert_ingested_message(package_id=pid,source={'locator_type':'directory','root_path':str(tmp_path),
                    'source_revision':'p1','container_sha256':'p1'},member=MailMember('%s.eml'%n,n,len(raw),{}),raw=raw,parsed=parse_eml(raw))['message_key']
    return repo,pid,add


def manual_person(repo,pid,addresses):
    with repo._connection() as c:
        c.execute("INSERT INTO mail_entities(entity_id,package_id,entity_type,display_name,basis,confirmed_by) VALUES ('person1',?,'person','Alice','manual','owner')",(pid,))
        for address in addresses:
            aid=c.execute('SELECT address_id FROM mail_addresses WHERE normalized_address=?',(address,)).fetchone()[0]
            c.execute("INSERT INTO mail_entity_addresses VALUES (?,'person1',?,'manual')",(pid,aid))
    return resolve_selection(repo,pid,'owner',entity_id='person1')


def conversation(repo,pid,keys):
    with repo._connection() as c:
        c.execute("INSERT INTO mail_conversations VALUES ('thread1',?,?,'plan',NULL,NULL,?,3,0,'en','manual')",(pid,keys[0],len(keys)))
        c.executemany("INSERT INTO mail_conversation_members VALUES ('thread1',?,?,?)",[(k,i,int(i==0)) for i,k in enumerate(keys)])
        for i,key in enumerate(keys[1:],1):
            c.execute("INSERT INTO mail_thread_edges(edge_id,package_id,source_message_key,target_message_key,relation_type,source_header,status) VALUES (?,?,?,?,'reply_to','in-reply-to','exact')",
                      ('edge%s'%i,pid,key,keys[i-1]))


def attachment_text(repo,pid,key,text='The contract requires approval before payment.'):
    with repo._connection() as c:
        path=c.execute('SELECT mime_path FROM mail_parts WHERE message_key=? AND is_explicit_attachment=1',(key,)).fetchone()[0]
        c.execute('INSERT INTO mail_text_segments(segment_id,package_id,message_key,mime_path,source_kind,ordinal,char_start,char_end,text,text_sha256,source_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                  ('attachment1',pid,key,path,'attachment',0,0,len(text),text,hashlib.sha256(text.encode()).hexdigest(),'p1'))


def create_analysis(repo,pid,selection=None,budget=512):
    selection=selection or resolve_selection(repo,pid,'owner','alice@example.com')
    spec={'mode':'person_correspondence','analysis_version':2,'package_id':pid,'person_address':selection['address'],
          'person_addresses':selection['addresses'],'person_entity_id':selection.get('entity_id'),
          'objective':'核查人物的目标、条件和后续变化','corpus':corpus_state(repo,pid,'owner',selection),'model_call_budget':budget}
    session=repo.create_session(pid,'owner','test',{})
    return repo.create_turn(session['session_id'],'owner','核查人物的目标、条件和后续变化',spec)['turn_id']


def citation(chunk):
    return {'chunk_id':chunk['chunk_id'],'quote':chunk['text'][:40],'char_start':chunk['char_start']}


def decode_person_source_payload(value):
    """Mock-model reader for the explicit metadata + single span-registry input.

    Production validates against its actual backend chunks. Mock models rebuild
    the supplied complete text to exercise either ID or old free-form outputs.
    """
    if not isinstance(value,dict) or 'citation_spans' not in value:return value
    bank={s['evidence_id']:s for s in value['citation_spans']};result=dict(value)
    for field in ('chunks','context_chunks'):
        if field not in value:continue
        result[field]=[{**c,'text':c['text'] if 'text' in c else ''.join(bank[key]['quote'] for key in c['evidence_ids'])} for c in value[field]]
    return result


class PersonModel:
    def __init__(self,fail_review=False):self.calls=[];self.fail_review=fail_review
    def chat_json(self,system,prompt,**kwargs):
        value=decode_person_source_payload(json.loads(prompt));self.calls.append(value)
        if 'claims' in value and 'evidence' in value:
            sources={e['evidence_id']:e for e in value['evidence']}
            return {'json':{'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'supported',
                'support_evidence_ids':c['evidence_ids'],'counter_evidence_ids':[],
                'source_quotes':[{'evidence_id':key,'quote':sources[key]['quote_text'],'char_start':sources[key]['char_start']} for key in c['evidence_ids']],
                'missing_requirements':[]}],
                'qualification_reviews':[{**check,'applicability':'not_applicable','rationale':'按本例来源条件及原始陈述判断；未知完成状态保留待核实'}
                    for check in value.get('qualification_checks',{}).get(c['claim_id'],[])]} for c in value['claims']]}}
        if 'chunks' in value:
            return {'json':{'findings':[{'kind':'goal' if c['authored'] and c['source_kind']=='body' and not c['attribution_uncertain'] else 'received',
                      'statement':'提出合同核查与行动安排','uncertainty':'需要按条件和后续回复核实',
                      'conditions':['先核对合同条件'],'contradictions':[],'unresolved_questions':['是否已经获得批准？'],
                      'citations':[citation(c)]} for c in value['chunks'][:12] if len(c['text'])>=8]}}
        if 'context_chunks' in value:
            if self.fail_review:raise RuntimeError('simulated review outage')
            counter=next((c for c in value['context_chunks'] if 'cancelled' in c['text']),None)
            reviews=[]
            for finding in value['provisional_findings']:
                allowed=set(value.get('later_reply_eligibility',{}).get(finding['finding_id'],[]))
                later=bool(counter and allowed.intersection(counter.get('evidence_ids',[])))
                reviews.append({'finding_id':finding['finding_id'],'status':'contradicted' if counter else 'supported',
                      'rationale':'后续邮件取消原计划' if counter else '已核查当前全部上下文片段',
                      'conditions':[],'contradictions':['后续取消了原计划'] if counter else [],'unresolved_questions':[],
                      'citations':[],'counterevidence':[citation(counter)] if counter else [],
                      'later_replies':[citation(counter)] if later else []})
            return {'json':{'reviews':reviews}}
        ids=[i for theme in value['prior_themes'] for i in theme['finding_ids']]
        ids+= [f['finding_id'] for f in value['evidence']]
        return {'json':{'themes':[{'title':'合同安排及变化','statement':'需按完整上下文核实计划、条件及后续变化',
                    'uncertainty':'收到的信息不能确认本人认同；主题概览压缩细节','finding_ids':list(dict.fromkeys(ids))[:6]}]}}


def finish(repo,tid,model,budget=9000,max_slices=150):
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(max_slices):
            result=run_person_slice(repo,tid,'owner',model,char_budget=budget)
            if not result.get('_requeue_slice'):return analysis_page(repo,tid,'owner')
    raise AssertionError('person analysis did not stop within a bounded number of slices')


def test_confirmed_mailboxes_all_roles_attachments_and_conversation_context(person_data):
    repo,pid,add=person_data
    first=add(attachment=True)
    second=add(sender='alice-work@example.com',cc='alice@example.com',bcc='alice-work@example.com')
    third=add(sender='bob@example.com',recipient='carol@example.com')
    conversation(repo,pid,[first,second,third]);attachment_text(repo,pid,first)
    selection=manual_person(repo,pid,['alice@example.com','alice-work@example.com'])
    state=corpus_state(repo,pid,'owner',selection)
    assert state['messages']==3 and state['attachments']==1 and state['missing_attachment_text']==0
    chunks,_=next_chunks(repo,pid,'owner',selection,[0,0])
    assert {c['message_key'] for c in chunks}=={first,second,third}
    assert any(c['source_kind']=='attachment' and c['attribution_uncertain'] for c in chunks)
    assert next(c for c in chunks if c['message_key']==second)['selected_roles']==['bcc','cc','from']
    assert next(c for c in chunks if c['message_key']==third)['context_only']
    tid=create_analysis(repo,pid,selection);page=finish(repo,tid,PersonModel())
    assert page['status']=='completed' and page['progress']['characters_read']==state['characters']
    assert page['progress']['context_review']['verified_findings']==page['progress']['findings_count']
    assert page['progress']['synthesis']['themes'][0]['evidence_refs']
    assert page['ledger'] and page['context_reviews']
    assert all(f['review_status']!='pending' for f in page['items'])


def test_later_cancellation_is_audited_before_final_themes(person_data):
    repo,pid,add=person_data
    first=add();later=add(body='The agreement is cancelled; please do not proceed.',date='Wed, 08 May 2024 15:38:50 +0700')
    conversation(repo,pid,[first,later]);tid=create_analysis(repo,pid);model=PersonModel()
    page=finish(repo,tid,model)
    assert page['progress']['synthesis']['themes'][0]['review_status']=='contradicted'
    assert any(r['later_replies'] for audit in page['context_reviews'] for r in audit['reviews'])
    assert any(i['kind']=='contradictions' for i in page['ledger'])
    phases=['reading' if 'chunks' in c else 'review' if 'context_chunks' in c else 'summary' for c in model.calls]
    assert max(i for i,p in enumerate(phases) if p=='review')<min(i for i,p in enumerate(phases) if p=='summary')


@pytest.mark.parametrize('change',['actual_text','sender_role','attachment','conversation','identity','package','attribution'])
def test_fingerprint_tracks_actual_analysis_dependencies(person_data,change):
    repo,pid,add=person_data;first=add(attachment=True);second=add(sender='alice-work@example.com')
    selection=manual_person(repo,pid,['alice@example.com','alice-work@example.com']);conversation(repo,pid,[first,second])
    before=corpus_state(repo,pid,'owner',selection)['fingerprint']
    with repo._connection() as c:
        if change=='actual_text':c.execute("UPDATE mail_text_segments SET text=text||' changed' WHERE package_id=?",(pid,))
        elif change=='sender_role':c.execute("UPDATE mail_message_participants SET role='to' WHERE message_key=? AND role='from'",(first,))
        elif change=='attachment':c.execute("UPDATE mail_parts SET payload_sha256='changed' WHERE message_key=? AND is_explicit_attachment=1",(first,))
        elif change=='conversation':c.execute("UPDATE mail_conversations SET merge_basis='changed' WHERE package_id=?",(pid,))
        elif change=='identity':c.execute("UPDATE mail_entities SET display_name='changed' WHERE package_id=?",(pid,))
        elif change=='attribution':c.execute("UPDATE mail_body_fragments SET fragment_kind='quoted' WHERE message_key=?",(first,))
        else:c.execute('UPDATE mail_packages SET index_epoch=index_epoch+1 WHERE package_id=?',(pid,))
    assert corpus_state(repo,pid,'owner',selection)['fingerprint']!=before


def test_manual_identity_change_cannot_resume_or_commit(person_data):
    repo,pid,add=person_data;add();add(sender='alice-work@example.com')
    selection=manual_person(repo,pid,['alice@example.com','alice-work@example.com']);tid=create_analysis(repo,pid,selection)
    spec=repo.get_turn(tid,'owner')['task_spec']
    with repo._connection() as c:c.execute("DELETE FROM mail_entity_addresses WHERE package_id=? AND address_id=(SELECT address_id FROM mail_addresses WHERE normalized_address='alice-work@example.com')",(pid,))
    with pytest.raises(RuntimeError):selection_for_spec(repo,spec,'owner')
    with pytest.raises(RuntimeError):run_person_slice(repo,tid,'owner',PersonModel())
    assert not repo.get_turn(tid,'owner')['final']


@pytest.mark.parametrize('tail',['Original message\nThey intend to force an immediate signature.',
                                'Best regards,\nAlice commits to immediate signature.',
                                '-- \nI intend to sign for every future project.'])
def test_attribution_boundary_after_64k_applies_across_all_segments(person_data,tail):
    repo,pid,add=person_data;repo.text_segment_chars=20000;add('A'*70000+'\n'+tail)
    selected=resolve_selection(repo,pid,'owner','alice@example.com');cursor=[0,0];last=[]
    for _ in range(30):
        chunks,cursor=next_chunks(repo,pid,'owner',selected,cursor)
        if not chunks:break
        last=chunks
    chunk=last[-1]
    quote=chunk['text'][-30:].strip()
    row={'kind':'goal','statement':'要求签署','uncertainty':'需核实','citations':[{'chunk_id':chunk['chunk_id'],'quote':quote,
                 'char_start':chunk['char_start']+chunk['text'].rfind(quote)}]}
    assert validate_findings({'findings':[row]},[chunk])==([],1)


def test_inline_quotes_do_not_taint_later_authored_text_and_repeated_quotes_need_offsets(person_data):
    repo,pid,add=person_data
    own='I intend to review the contract after approval.'
    add('> They intend to require immediate signature.\n'+own+'\n'+own)
    chunks,_=next_chunks(repo,pid,'owner',resolve_selection(repo,pid,'owner','alice@example.com'),[0,0]);chunk=chunks[0]
    row={'kind':'goal','statement':'希望核查合同','citations':[{'chunk_id':chunk['chunk_id'],'quote':own}]}
    assert validate_findings({'findings':[row]},[chunk])==([],1)
    row['citations'][0]['char_start']=chunk['char_start']+chunk['text'].index(own)
    assert len(validate_findings({'findings':[row]},[chunk])[0])==1


def test_attachment_never_proves_senders_own_goal(person_data):
    repo,pid,add=person_data;key=add(attachment=True);attachment_text(repo,pid,key)
    chunks,_=next_chunks(repo,pid,'owner',resolve_selection(repo,pid,'owner','alice@example.com'),[0,0])
    chunk=next(c for c in chunks if c['source_kind']=='attachment')
    row={'kind':'goal','statement':'本人要求先审批','citations':[citation(chunk)]}
    assert validate_findings({'findings':[row]},[chunk])==([],1)
    row['kind']='received';assert len(validate_findings({'findings':[row]},[chunk])[0])==1


def test_budget_exhaustion_retains_provisional_ledger_and_stops(person_data):
    repo,pid,add=person_data;add('I intend to check the terms. '*300)
    tid=create_analysis(repo,pid,budget=1);model=PersonModel();page=finish(repo,tid,model,budget=600)
    assert len(model.calls)==1 and page['status']=='completed'
    assert page['progress']['budget_exhausted'] and page['progress']['characters_read']<page['progress']['corpus']['characters']
    assert repo.get_turn(tid,'owner')['sufficiency']=='partial' and page['items']
    assert all(f['review_status']=='pending' for f in page['items'])
    assert run_person_slice(repo,tid,'owner',model).get('_requeue_slice') is None


def test_context_failure_resume_never_skips_unreviewed_context(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid);bad=PersonModel(fail_review=True)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(10):
            try:run_person_slice(repo,tid,'owner',bad)
            except RuntimeError:break
        else:raise AssertionError('mock outage was not used')
    state=repo.get_turn(tid,'owner')['final']
    assert state['phase']=='context_review' and state['context_review']['cursor']==[0,0]
    assert state['model_calls']==2 and analysis_page(repo,tid,'owner')['items']
    repo.update_turn(tid,'owner',status='queued')
    page=finish(repo,tid,PersonModel())
    assert page['status']=='completed' and page['progress']['context_review']['batches_read']==1


def test_cancellation_during_generation_keeps_cursor_atomic(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class CancelModel(PersonModel):
        def chat_json(self,*args,**kwargs):
            response=super().chat_json(*args,**kwargs);repo.update_turn(tid,'owner',status='cancelled');return response
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()),pytest.raises(JobCancelled):
        run_person_slice(repo,tid,'owner',CancelModel())
    state=repo.get_turn(tid,'owner')['final']
    assert state['cursor']==[0,0] and state['model_calls']==1
    assert analysis_page(repo,tid,'owner')['batch_total']==0


def test_source_change_during_generation_cannot_commit_findings(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class ChangingModel(PersonModel):
        def chat_json(self,*args,**kwargs):
            response=super().chat_json(*args,**kwargs)
            with repo._connection() as c:c.execute("UPDATE mail_text_segments SET text=text||'changed' WHERE package_id=?",(pid,))
            return response
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()),pytest.raises(RuntimeError):
        run_person_slice(repo,tid,'owner',ChangingModel())
    assert analysis_page(repo,tid,'owner')['batch_total']==0
    assert repo.get_turn(tid,'owner')['final']['cursor']==[0,0]


def test_full_checkpoint_compare_blocks_duplicate_executor(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid);model=PersonModel()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):run_person_slice(repo,tid,'owner',model)
    turn=repo.get_turn(tid,'owner');actual=turn['final'];stale={**actual,'model_calls':actual['model_calls']-1}
    selection=selection_for_spec(repo,turn['task_spec'],'owner')
    with pytest.raises(RuntimeError):_commit_v2(repo,tid,'owner',turn['task_spec'],selection,stale,actual)
    assert analysis_page(repo,tid,'owner')['batch_total']==1


def test_summary_prior_refs_are_resolved_without_copying_all_evidence_to_final(person_data):
    repo,pid,add=person_data;add('We intend to review terms before making payment. '*10)
    tid=create_analysis(repo,pid);page=finish(repo,tid,PersonModel(),budget=180)
    assert page['batch_total']>1
    theme=page['progress']['synthesis']['themes'][0]
    assert len(theme['finding_ids'])>1 and 'evidence' not in theme and theme['evidence_refs']
    assert page['progress']['ledger_counts']['unresolved_questions']>=page['progress']['findings_count']
    assert page['page_total']>=page['context_review_total']
    with pytest.raises(LookupError):analysis_page(repo,tid,'intruder')


def test_bad_later_reply_dates_are_rejected(person_data):
    repo,pid,add=person_data;add();selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0]);chunk=chunks[0]
    findings,_=validate_findings({'findings':[{'kind':'goal','statement':'核查合同','citations':[citation(chunk)]}]},chunks)
    finding={**findings[0],'finding_id':'f1'}
    review={'finding_id':'f1','status':'supported','rationale':'核查后续','later_replies':[citation(chunk)]}
    with pytest.raises(ValueError):validate_reviews({'reviews':[review]},[finding],chunks)


def test_review_prompt_limits_later_replies_to_resolved_later_sources(person_data):
    repo,pid,add=person_data
    a=add()
    b=add(sender='bob@example.com',recipient='alice@example.com',date='Wed, 08 May 2024 15:38:50 +0700')
    c=add(date='Thu, 09 May 2024 15:38:50 +0700')
    conversation(repo,pid,[a,b,c])
    tid=create_analysis(repo,pid);model=PersonModel();finish(repo,tid,model)
    audit=next(prompt for prompt in model.calls if 'context_chunks' in prompt)
    source_keys={span['evidence_id']:next(chunk['message_key'] for chunk in audit['context_chunks'] if chunk['chunk_id']==span['chunk_id']) for span in audit['citation_spans']}
    expected={a:{b,c},b:{c},c:set()}
    for finding in audit['provisional_findings']:
        source=finding['citations'][0]['message_key']
        allowed=audit['later_reply_eligibility'][finding['finding_id']]
        assert {source_keys[chunk_id] for chunk_id in allowed}==expected[source]


def test_missing_attachment_text_marks_finished_analysis_partial(person_data):
    repo,pid,add=person_data;add(attachment=True);tid=create_analysis(repo,pid)
    page=finish(repo,tid,PersonModel())
    assert page['progress']['corpus']['missing_attachment_text']==1
    assert repo.get_turn(tid,'owner')['sufficiency']=='partial'


def test_partial_attachment_extraction_is_a_coverage_gap(person_data):
    repo,pid,add=person_data;key=add(attachment=True);attachment_text(repo,pid,key)
    with repo._connection() as c:
        path=c.execute('SELECT mime_path FROM mail_parts WHERE message_key=? AND is_explicit_attachment=1',(key,)).fetchone()[0]
    repo.update_processing(key,path,'basic_text','partial',text='Extracted text',coverage_ratio=0.5)
    tid=create_analysis(repo,pid);page=finish(repo,tid,PersonModel())
    assert page['progress']['corpus']['missing_attachment_text']==0
    assert page['progress']['corpus']['partial_attachment_text']==1
    assert repo.get_turn(tid,'owner')['sufficiency']=='partial'


def test_flattened_html_quote_is_not_authored_intent(person_data):
    repo,pid,add=person_data;add('<p>I plan to review the contract.</p><blockquote>They demand immediate signature.</blockquote>',html=True)
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    chunk=chunks[0];row={'kind':'goal','statement':'本人要求签署','citations':[citation(chunk)]}
    assert validate_findings({'findings':[row]},chunks)==([],1)
    tid=create_analysis(repo,pid);page=finish(repo,tid,PersonModel())
    assert page['progress']['corpus']['attribution_limited_body_messages']==1
    assert repo.get_turn(tid,'owner')['sufficiency']=='partial'


def test_mixed_valid_and_invalid_v2_findings_do_not_skip_a_batch(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class BadMixed(PersonModel):
        def chat_json(self,*a,**kw):
            value=super().chat_json(*a,**kw)
            value['json']['findings'].append({'kind':'goal','statement':'无引文的猜测','citations':[{'chunk_id':'invented','quote':'fabricated words'}]})
            return value
    model=BadMixed()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()),pytest.raises(ValueError):
        run_person_slice(repo,tid,'owner',model)
    assert repo.get_turn(tid,'owner')['final']['cursor']==[0,0]
    assert repo.get_turn(tid,'owner')['final']['model_calls']==len(model.calls)==2
    assert analysis_page(repo,tid,'owner')['batch_total']==0


def test_malformed_reading_cannot_be_repaired_by_empty_findings(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class EmptyRepair(PersonModel):
        def chat_json(self,system,prompt,**kw):
            value=decode_person_source_payload(json.loads(prompt));self.calls.append(value)
            return {'json':{'findings':[] if 'validation_repair' in value else None}}
    model=EmptyRepair()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()),pytest.raises(ValueError):
        run_person_slice(repo,tid,'owner',model)
    assert repo.get_turn(tid,'owner')['final']['cursor']==[0,0]
    assert repo.get_turn(tid,'owner')['final']['model_calls']==2
    assert analysis_page(repo,tid,'owner')['batch_total']==0


def test_single_message_change_is_repaired_with_specific_authorship_feedback(person_data):
    repo,pid,add=person_data;add('同意修改付款安排，但等内部审批后再确认签约。');tid=create_analysis(repo,pid)
    class RepairChange(PersonModel):
        def chat_json(self,system,prompt,**kw):
            value=decode_person_source_payload(json.loads(prompt));response=super().chat_json(system,prompt,**kw)
            if 'chunks' in value:
                if 'validation_repair' not in value:
                    response['json']['findings'][0]['kind']='change'
                else:
                    issue=value['validation_repair']['issues'][0]
                    assert issue['finding_index']==1 and 'two distinct messages' in issue['reason']
                    response['json']['findings'][0]['kind']='concern'
            return response
    model=RepairChange()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,tid,'owner',model)
    page=analysis_page(repo,tid,'owner')
    assert page['batch_total']==1 and page['items'][0]['kind']=='concern'
    assert page['progress']['model_calls']==len(model.calls)==2
    assert page['progress']['cursor']!=[0,0]


def test_reading_repair_cannot_erase_invalid_findings_and_advance(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class EraseRepair(PersonModel):
        def chat_json(self,system,prompt,**kw):
            value=decode_person_source_payload(json.loads(prompt));response=super().chat_json(system,prompt,**kw)
            if 'validation_repair' in value:response['json']['findings']=[]
            else:response['json']['findings'][0]['kind']='change'
            return response
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()),pytest.raises(ValueError):
        run_person_slice(repo,tid,'owner',EraseRepair())
    state=repo.get_turn(tid,'owner')['final']
    assert state['cursor']==[0,0] and state['model_calls']==2
    assert analysis_page(repo,tid,'owner')['batch_total']==0


def test_invalid_review_citation_gets_one_budgeted_repair(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class RepairReview(PersonModel):
        def chat_json(self,system,prompt,**kw):
            value=decode_person_source_payload(json.loads(prompt));response=super().chat_json(system,prompt,**kw)
            if 'context_chunks' in value:
                for row in response['json']['reviews']:
                    row['citations']=[citation(value['context_chunks'][0])]
                    if 'validation_repair' not in value:row['citations'][0]['quote']='fabricated text absent from the original source'
                    else:assert '原文不符' in value['validation_repair']['error']
            return response
    model=RepairReview();page=finish(repo,tid,model)
    assert len([c for c in model.calls if 'context_chunks' in c])==2
    # A separate atomic semantic audit follows the two budgeted review attempts.
    assert page['progress']['model_calls']==len(model.calls)==4
    assert len([c for c in model.calls if 'claims' in c and 'evidence' in c])==1
    audit=page['context_reviews'][0]
    assert len(audit['validation_recovery'])==1
    assert audit['reviews'][0]['citations'][0]['char_start']==0
    assert not audit['reviews'][0].get('validation_failed')


def test_twice_invalid_reviews_save_only_explicit_unresolved_audit(person_data):
    repo,pid,add=person_data;add('同意修改付款安排，但等内部审批后再确认签约。');tid=create_analysis(repo,pid)
    class InvalidReview(PersonModel):
        def chat_json(self,system,prompt,**kw):
            value=decode_person_source_payload(json.loads(prompt));response=super().chat_json(system,prompt,**kw)
            if 'context_chunks' in value:
                for row in response['json']['reviews']:
                    row.update(status='supported',citations=[{'chunk_id':value['context_chunks'][0]['chunk_id'],
                                                             'quote':'fabricated approval was granted','char_start':0}])
            return response
    model=InvalidReview();page=finish(repo,tid,model)
    assert page['status']=='completed' and repo.get_turn(tid,'owner')['sufficiency']=='partial'
    assert len([c for c in model.calls if 'context_chunks' in c])==2
    review=page['context_reviews'][0]['reviews'][0]
    assert review['status']=='unresolved' and review['validation_failed']
    assert len(review['validation_errors'])==2 and review['unresolved_questions']
    assert review['conditions'] and not review['citations'] and not review['counterevidence']
    assert review['guard_context'] and 'fabricated approval' not in json.dumps(page,ensure_ascii=False)
    assert page['items'][0]['review_status']=='unresolved'


def test_twice_missing_review_finding_ids_are_unresolved_not_supported(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class MissingReview(PersonModel):
        def chat_json(self,system,prompt,**kw):
            value=decode_person_source_payload(json.loads(prompt));response=super().chat_json(system,prompt,**kw)
            if 'context_chunks' in value:response['json']['reviews']=[]
            return response
    page=finish(repo,tid,MissingReview())
    assert page['items'][0]['review_status']=='unresolved'
    review=page['context_reviews'][0]['reviews'][0]
    assert review['finding_id']==page['items'][0]['finding_id'] and review['validation_failed']


def test_reading_repair_budget_stop_keeps_unread_cursor(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid,budget=1)
    class InvalidChange(PersonModel):
        def chat_json(self,*a,**kw):
            response=super().chat_json(*a,**kw);response['json']['findings'][0]['kind']='change';return response
    model=InvalidChange();page=finish(repo,tid,model)
    assert page['progress']['budget_exhausted'] and page['progress']['cursor']==[0,0]
    assert page['batch_total']==0 and len(model.calls)==page['progress']['model_calls']==1


def test_review_repair_budget_stop_retains_unreviewed_cursor(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid,budget=2)
    class InvalidReview(PersonModel):
        def chat_json(self,system,prompt,**kw):
            response=super().chat_json(system,prompt,**kw)
            if 'context_chunks' in json.loads(prompt):response['json']['reviews']=[]
            return response
    model=InvalidReview();page=finish(repo,tid,model)
    assert page['progress']['budget_exhausted'] and page['progress']['phase']=='context_review'
    assert page['progress']['context_review']['cursor']==[0,0] and page['context_review_total']==0
    assert page['batch_total']==1 and len(model.calls)==page['progress']['model_calls']==2


def test_cancellation_during_review_repair_never_commits_audit(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class CancelRepair(PersonModel):
        def chat_json(self,system,prompt,**kw):
            value=decode_person_source_payload(json.loads(prompt));response=super().chat_json(system,prompt,**kw)
            if 'context_chunks' in value:
                response['json']['reviews']=[]
                if 'validation_repair' in value:repo.update_turn(tid,'owner',status='cancelled')
            return response
    model=CancelRepair()
    with pytest.raises(JobCancelled):finish(repo,tid,model)
    page=analysis_page(repo,tid,'owner')
    assert page['status']=='cancelled' and page['context_review_total']==0
    assert page['progress']['context_review']['cursor']==[0,0]
    assert page['progress']['model_calls']==len(model.calls)==3


def test_exact_budget_can_complete_when_no_more_model_calls_are_needed(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid,budget=1)
    class EmptyModel:
        def chat_json(self,*a,**kw):return {'json':{'findings':[]}}
    page=finish(repo,tid,EmptyModel())
    assert page['status']=='completed' and not page['progress'].get('budget_exhausted')
    assert repo.get_turn(tid,'owner')['sufficiency']=='sufficient'


def test_concurrent_executor_progress_does_not_fail_shared_turn(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class InterleavingModel(PersonModel):
        def chat_json(self,*a,**kw):
            result=super().chat_json(*a,**kw)
            run_person_slice(repo,tid,'owner',PersonModel())
            return result
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        result=run_person_slice(repo,tid,'owner',InterleavingModel())
    assert result['_requeue_slice'] and repo.get_turn(tid,'owner')['status']=='running'
    assert analysis_page(repo,tid,'owner')['batch_total']==1
    assert finish(repo,tid,PersonModel())['status']=='completed'


def test_bare_supported_verdict_is_downgraded_without_original_context_citations(person_data):
    repo,pid,add=person_data;add();selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    findings,_=validate_findings({'findings':[{'kind':'goal','statement':'推进计划','citations':[citation(chunks[0])]}]},chunks)
    findings[0]['finding_id']='f1'
    reviews=validate_reviews({'reviews':[{'finding_id':'f1','status':'supported','rationale':'已检查所有后续，无影响'}]},findings,chunks)
    assert reviews[0]['status']=='unresolved' and reviews[0]['model_status']=='supported'
    assert reviews[0]['unresolved_questions']


def test_three_message_approval_condition_cannot_be_ignored_as_confirmed(person_data):
    repo,pid,add=person_data
    a=add('我希望本周签约并推进合作事项。')
    b=add('付款安排需要修改，先核对金额和分期条件。',sender='bob@example.com',recipient='alice@example.com',date='Wed, 08 May 2024 15:38:50 +0700')
    c=add('同意修改付款安排，但等内部审批后再确认签约。',date='Thu, 09 May 2024 15:38:50 +0700')
    conversation(repo,pid,[a,b,c]);tid=create_analysis(repo,pid)
    class OverconfidentModel(PersonModel):
        def chat_json(self,system,prompt,**kw):
            value=decode_person_source_payload(json.loads(prompt));response=super().chat_json(system,prompt,**kw)
            if 'chunks' in value:
                for f in response['json']['findings']:
                    f.update(statement='已确认本周签约',conditions=[],unresolved_questions=[])
            elif 'context_chunks' in value:
                for r in response['json']['reviews']:
                    r.update(status='supported',rationale='后续支持已经确认',citations=[],counterevidence=[],later_replies=[],conditions=[],contradictions=[],unresolved_questions=[])
            elif 'themes' in response['json']:
                response['json']['themes'][0]['statement']='已确认本周签约。'
            return response
    page=finish(repo,tid,OverconfidentModel())
    assert all(f['review_status']=='unresolved' for f in page['items'])
    assert repo.get_turn(tid,'owner')['sufficiency']=='partial'
    assert page['progress']['context_review']['unresolved_findings']==page['progress']['findings_count']
    assert any(r.get('guard_context') for audit in page['context_reviews'] for r in audit['reviews'])
    assert '不能当作已经确认' in page['progress']['synthesis']['themes'][0]['uncertainty']


def test_short_explicit_cancellation_is_not_lost_at_quote_length_limit(person_data):
    repo,pid,add=person_data;first=add();later=add('取消。',date='Wed, 08 May 2024 15:38:50 +0700')
    conversation(repo,pid,[first,later]);selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],related_roots=[first])
    own=next(c for c in chunks if c['message_key']==first);short=next(c for c in chunks if c['message_key']==later)
    findings,_=validate_findings({'findings':[{'kind':'goal','statement':'推进计划','citations':[citation(own)]}]},chunks);findings[0]['finding_id']='f1'
    review=validate_reviews({'reviews':[{'finding_id':'f1','status':'supported','rationale':'已核查，无后续变化','citations':[citation(own)]}]},findings,chunks)[0]
    assert review['status']=='unresolved'
    assert review['guard_context'][0]['message_key']==short['message_key']
    assert '取消' in review['guard_context'][0]['quote']


def test_metadata_fingerprint_matches_strong_manifest_without_loading_text(person_data):
    repo,pid,add=person_data;key=add(attachment=True);attachment_text(repo,pid,key)
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    full=corpus_state(repo,pid,'owner',selected);quick=corpus_state(repo,pid,'owner',selected,verify_content=False)
    assert full['metadata_fingerprint']==quick['metadata_fingerprint']
    assert quick['fingerprint'] is None and full['characters']==quick['characters']


def test_same_length_actual_text_change_during_summary_fails_final_strong_audit(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class ChangingSummary(PersonModel):
        def chat_json(self,system,prompt,**kw):
            response=super().chat_json(system,prompt,**kw)
            if 'evidence' in json.loads(prompt):
                with repo._connection() as c:
                    c.execute("UPDATE mail_text_segments SET text='X'||SUBSTR(text,2) WHERE package_id=?",(pid,))
            return response
    with pytest.raises(RuntimeError):finish(repo,tid,ChangingSummary())
    turn=repo.get_turn(tid,'owner')
    assert turn['status']=='failed' and not turn['final']['complete']


def test_budget_increase_continues_original_cursor_without_duplicate_batches(person_data):
    repo,pid,add=person_data;add('We intend to review terms before payment. '*20)
    tid=create_analysis(repo,pid,budget=1);page=finish(repo,tid,PersonModel(),budget=180)
    before=page['progress'];assert page['status']=='completed' and before['budget_exhausted'] and not before['complete']
    turn=repo.get_turn(tid,'owner');spec={**turn['task_spec'],'model_call_budget':100,'max_tokens':2000000}
    from services.mail.research.token_budget import init_usage
    resumed={**before,'model_call_budget':100,'budget_exhausted':False,'token_budget_exhausted':False,
             'token_usage':init_usage({'max_tokens':spec['max_tokens']},before.get('token_usage'))}
    with repo._connection() as c:
        c.execute("UPDATE mail_research_turns SET task_spec_json=?,final_json=?,status='queued' WHERE turn_id=?",(json.dumps(spec),json.dumps(resumed),tid))
    page=finish(repo,tid,PersonModel(),budget=180)
    assert page['status']=='completed' and page['progress']['complete']
    assert page['progress']['characters_read']==spec['corpus']['characters']
    assert page['progress']['model_calls']>1
    with repo._connection() as c:
        batches=[r[0] for r in c.execute("SELECT step_id FROM mail_research_steps WHERE turn_id=? AND tool_name='person_evidence_batch'",(tid,))]
    assert len(batches)==len(set(batches))==page['batch_total']


@pytest.mark.parametrize('budget',[2048,8193,32768])
def test_expanded_person_budget_accepts_new_and_old_valid_bounds(person_data,budget):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid,budget=budget)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,tid,'owner',PersonModel())
    state=repo.get_turn(tid,'owner')['final']
    assert state['model_call_budget']==budget and state['model_calls']==1


@pytest.mark.parametrize('budget',[32769,0,True,'2048'])
def test_expanded_person_budget_rejects_invalid_limits_before_generation(person_data,budget):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid,budget=budget);model=PersonModel()
    with pytest.raises(ValueError):run_person_slice(repo,tid,'owner',model)
    assert not model.calls and not repo.get_turn(tid,'owner')['final']


def test_new_default_is_larger_and_existing_checkpoint_budget_remains_frozen(person_data):
    repo,pid,add=person_data;add()
    assert PERSON_DEFAULT_MODEL_CALL_BUDGET==4096 and PERSON_MAX_MODEL_CALL_BUDGET==32768
    tid=create_analysis(repo,pid,budget=512)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,tid,'owner',PersonModel())
        run_person_slice(repo,tid,'owner',PersonModel())
    assert repo.get_turn(tid,'owner')['final']['model_call_budget']==512
    default_tid=create_analysis(repo,pid)
    turn=repo.get_turn(default_tid,'owner');spec={k:v for k,v in turn['task_spec'].items() if k!='model_call_budget'}
    with repo._connection() as c:c.execute('UPDATE mail_research_turns SET task_spec_json=? WHERE turn_id=?',(json.dumps(spec),default_tid))
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,default_tid,'owner',PersonModel())
    assert repo.get_turn(default_tid,'owner')['final']['model_call_budget']==4096
