"""Performance changes must preserve complete, attributed, durable evidence."""
import json
from contextlib import nullcontext, contextmanager
from unittest.mock import patch

import pytest

from test_person_context_upgrade import (person_data, create_analysis, citation,
                                         PersonModel, finish, conversation, decode_person_source_payload)
from services.mail.person_corpus import resolve_selection, next_chunks, related_messages, corpus_state
from services.mail.person_context import _finding_context, validate_reviews
from services.mail.person_research import run_person_slice, analysis_page, validate_findings, _commit_v2, repair_exact_citation_offsets, reading_chunk_payload, citation_span_payload, resolve_citation_spans, span_source_metadata, person_output_schema
from services.mail.person_summary import compact_semantic_payload,extractive_themes


def _findings(chunks):
    result,_=validate_findings({'findings':[{'kind':'goal' if c['authored'] else 'received',
               'statement':'保留有来源的计划表达','citations':[citation(c)]} for c in chunks]},chunks)
    for index,f in enumerate(result):f['finding_id']='f%d'%index
    return result


def test_exact_conversation_scope_preserves_multiple_sources_and_legacy_fallback(person_data):
    repo,pid,add=person_data
    a=add('Plan for the first agreement remains open.')
    b=add('Review the other agreement before accepting.',sender='bob@example.com',recipient='alice@example.com')
    c=add('Cancellation applies to the first agreement only.',date='Wed, 08 May 2024 15:38:50 +0700')
    conversation(repo,pid,[a,c])
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0]);findings=_findings(chunks)
    single=next(f for f in findings if f['citations'][0]['message_key']==a)
    multi={**single,'finding_id':'multi','citations':single['citations']+next(f for f in findings if f['citations'][0]['message_key']==b)['citations']}
    legacy={**multi,'finding_id':'legacy','citations':[{k:v for k,v in q.items() if k!='conversations'} for q in multi['citations']]}
    mapping=_finding_context(repo,{'package_id':pid},'owner',[single,multi,legacy],chunks)
    assert {c['message_key'] for c in mapping[single['finding_id']]}=={a,c}
    for finding in (multi,legacy):
        exact=set(related_messages(repo,pid,'owner',[q['message_key'] for q in finding['citations']]))
        assert {c['message_key'] for c in mapping[finding['finding_id']]}==exact=={a,b,c}


def test_foreign_conditions_do_not_contaminate_finding_and_cannot_support_it(person_data):
    repo,pid,add=person_data
    a=add('I plan to finish the agreed draft today.')
    b=add('The separate project is cancelled pending approval.')
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0]);finding=next(f for f in _findings(chunks) if f['citations'][0]['message_key']==a)
    mapping=_finding_context(repo,{'package_id':pid},'owner',[finding],chunks)
    own=next(c for c in chunks if c['message_key']==a);other=next(c for c in chunks if c['message_key']==b)
    review={'finding_id':finding['finding_id'],'status':'supported','rationale':'当前来源邮件包含这个表达',
            'citations':[citation(own)]}
    checked=validate_reviews({'reviews':[review]},[finding],chunks,mapping)[0]
    assert checked['status']=='supported' and not checked.get('guard_context')
    with pytest.raises(ValueError,match='原文不符'):
        validate_reviews({'reviews':[{**review,'citations':[citation(other)]}]},[finding],chunks,mapping)


def test_small_read_budget_keeps_every_character_and_resumable_offset(person_data):
    repo,pid,add=person_data
    add('Full source text including a later condition. '*90)
    selected=resolve_selection(repo,pid,'owner','alice@example.com');cursor=[0,0];seen=[]
    for _ in range(100):
        chunks,new=next_chunks(repo,pid,'owner',selected,cursor,180)
        if not chunks:break
        assert sum(len(c['text']) for c in chunks)<=180 and new!=cursor
        seen.extend(chunks);cursor=new
    else:raise AssertionError('small-budget reading failed to converge')
    assert sum(len(c['text']) for c in seen)==corpus_state(repo,pid,'owner',selected)['characters']
    assert len({(c['segment_id'],c['char_start']) for c in seen})==len(seen)


def test_default_batches_reduce_calls_without_omitting_long_source(person_data):
    repo,pid,add=person_data;repo.text_segment_chars=3000
    add('Complete unabridged correspondence details. '*900)
    class EmptyModel:
        def __init__(self):self.calls=0
        def chat_json(self,*args,**kwargs):self.calls+=1;return {'json':{'findings':[]}}
    old=EmptyModel();old_page=finish(repo,create_analysis(repo,pid),old,budget=9000)
    new=EmptyModel();tid=create_analysis(repo,pid)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(100):
            result=run_person_slice(repo,tid,'owner',new)
            if not result.get('_requeue_slice'):break
        else:raise AssertionError('default reading did not finish')
    page=analysis_page(repo,tid,'owner')
    assert page['progress']['complete'] and old_page['progress']['complete']
    assert page['progress']['characters_read']==old_page['progress']['characters_read']
    assert new.calls<old.calls and new.calls==page['progress']['batches']


def test_operational_commits_do_not_rescan_corpus_or_advance_evidence(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    with patch('services.mail.person_corpus.corpus_state',wraps=corpus_state) as manifest,patch(
            'services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,tid,'owner',PersonModel())
    # One check at slice entry, one initial checkpoint, one evidence commit.
    # Model reservation and usage report do not perform two extra full scans.
    assert manifest.call_count==3
    turn=repo.get_turn(tid,'owner');before=turn['final'];spec=turn['task_spec']
    selected=resolve_selection(repo,pid,'owner','alice@example.com')
    with pytest.raises(ValueError,match='不得推进'):
        _commit_v2(repo,tid,'owner',spec,selected,before,{**before,'cursor':[999,0]},operational=True)
    assert repo.get_turn(tid,'owner')['final']==before


def test_activity_is_durable_before_model_and_events_are_bounded(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class VisibleModel(PersonModel):
        def chat_json(self,*args,**kwargs):
            page=analysis_page(repo,tid,'owner');activity=page['progress']['activity']
            assert activity['state'] in {'running','waiting_for_model'} and activity['stage']=='reading'
            assert activity['batch_index']==0 and activity['characters']>0 and activity['started_at']
            return super().chat_json(*args,**kwargs)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,tid,'owner',VisibleModel())
    page=analysis_page(repo,tid,'owner')
    assert page['created_at'] and page['updated_at'] and page['progress']['started_at']
    assert page['progress']['activity']['state']=='checkpointed'
    assert page['progress']['events'][-1]['source_batch_offset']==0
    assert 1<=len(page['progress']['events'])<=24
    assert repo.get_turn(tid,'owner')['events']
    assert repo.get_turn(tid,'owner',include_events=False)['events']==[]


def test_user_analysis_admission_is_fifo_and_actual_running_transition_is_saved_once(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid);states=[]
    @contextmanager
    def admission(cancelled,priority,on_status):
        assert priority==100
        states.append(repo.get_turn(tid,'owner')['final']['activity']['state'])
        on_status('waiting_for_model');on_status('waiting_for_model')
        on_status('running');on_status('running')
        states.append(repo.get_turn(tid,'owner')['final']['activity']['state'])
        yield
    with patch('services.model_dispatch.background_generation',admission),patch(
            'services.mail.person_corpus.corpus_state',wraps=corpus_state) as manifest:
        run_person_slice(repo,tid,'owner',PersonModel())
    assert states==['waiting_for_model','running'] and manifest.call_count==3
    page=analysis_page(repo,tid,'owner')
    assert sum(e['kind']=='model_admitted' for e in page['progress']['events'])==1
    assert page['progress']['model_calls']==1


def test_applicable_review_output_grows_linearly_for_unrelated_messages(person_data):
    repo,pid,add=person_data
    keys=[add('Individual project %s has an explicitly stated plan.'%i) for i in range(8)]
    tid=create_analysis(repo,pid);model=PersonModel()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,tid,'owner',model,char_budget=9000)
    # Small review slices isolate one message. Old batching generated all eight
    # reviews for each slice. Every source still receives its own complete audit.
    page=finish(repo,tid,model,budget=55)
    reviews=[p for p in model.calls if 'context_chunks' in p]
    assert all(set(p['finding_context_evidence_ids'])=={f['finding_id'] for f in p['provisional_findings']} for p in reviews)
    assert all(len(p['provisional_findings'])<=2 for p in reviews)
    assert sum(len(p['provisional_findings']) for p in reviews)<=2*len(reviews)
    assert sum(len(p['provisional_findings']) for p in reviews)<8*len(reviews)
    assert page['progress']['characters_read']==page['progress']['corpus']['characters']
    assert page['progress']['context_review']['verified_findings']==page['progress']['findings_count']
    assert {q['message_key'] for f in page['items'] for q in f['citations']}==set(keys)


def test_unique_exact_offset_recovery_keeps_quote_and_unknown_actor_unmodified(person_data):
    repo,pid,add=person_data;add('I intend to finish the specifically identified project.')
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    original={'findings':[{'kind':'goal','statement':'计划推进项目','actor':'unknown@example.com',
                          'citations':[{**citation(chunks[0]),'char_start':900}]}]}
    recovered,log=repair_exact_citation_offsets(original,chunks,'findings')
    assert original['findings'][0]['citations'][0]['char_start']==900
    assert recovered['findings'][0]['actor']=='unknown@example.com'
    assert recovered['findings'][0]['citations'][0]['quote']==original['findings'][0]['citations'][0]['quote']
    assert log[0]['supplied_char_start']==900 and log[0]['resolved_char_start']==chunks[0]['char_start']
    # Exact positioning does not establish an unknown actor's identity.
    assert validate_findings(recovered,chunks)==([],1)


def test_repeated_fabricated_and_wrong_source_quotes_cannot_be_relocated(person_data):
    repo,pid,add=person_data;add('Identical approval phrase. Identical approval phrase.')
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    for source,quote in [(chunks[0]['chunk_id'],'Identical approval phrase.'),
                         (chunks[0]['chunk_id'],'an invented approval phrase'),
                         ('nonexistent','Identical approval phrase.')]:
        value={'findings':[{'kind':'goal','statement':'不应修复','citations':[{'chunk_id':source,'quote':quote,'char_start':1000}]}]}
        recovered,log=repair_exact_citation_offsets(value,chunks,'findings')
        assert recovered==value and not log and validate_findings(recovered,chunks)==([],1)


def test_relocated_quoted_text_still_cannot_prove_authored_intent(person_data):
    repo,pid,add=person_data;add('My own note before forwarding.\nOriginal message\nTheir unique statement about acceptance.')
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    value={'findings':[{'kind':'goal','statement':'不可归属于本人','actor':'alice@example.com',
           'citations':[{'chunk_id':chunks[0]['chunk_id'],'quote':'Their unique statement about acceptance.','char_start':0}]}]}
    recovered,log=repair_exact_citation_offsets(value,chunks,'findings')
    assert log and validate_findings(recovered,chunks)==([],1)


def test_deterministic_offset_recovery_avoids_model_repair_and_is_audited(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class WrongOffsets(PersonModel):
        def chat_json(self,system,prompt,**kwargs):
            value=decode_person_source_payload(json.loads(prompt));response=super().chat_json(system,prompt,**kwargs)
            if 'chunks' in value:
                for row in response['json']['findings']:row['citations'][0]['char_start']+=500
            if 'context_chunks' in value:
                for row in response['json']['reviews']:row['citations']=[{**citation(value['context_chunks'][0]),'char_start':500}]
            return response
    model=WrongOffsets();page=finish(repo,tid,model)
    assert len([p for p in model.calls if 'chunks' in p])==1
    assert len([p for p in model.calls if 'context_chunks' in p])==1
    assert not any('validation_repair' in p for p in model.calls)
    with repo._connection() as c:
        output=json.loads(c.execute("SELECT output_ref FROM mail_research_steps WHERE turn_id=? AND tool_name='person_evidence_batch'",(tid,)).fetchone()[0])
    assert output['validation_recovery'][0]['kind']=='citation_location_recovery'
    assert output['findings'][0]['citations'][0]['char_start']==0
    audit=page['context_reviews'][0]
    assert audit['citation_location_recovery'][0]['supplied_char_start']==500
    assert not audit['validation_recovery'] and not audit['reviews'][0].get('validation_failed')


def test_prompt_source_kinds_and_selected_actor_scope_are_explicit(person_data):
    repo,pid,add=person_data
    own=add('I plan to review the terms before proceeding.')
    received=add('We propose delivery subject to approval.',sender='bob@example.com',recipient='alice@example.com',cc='other@example.com')
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    incoming=next(c for c in chunks if c['message_key']==received)
    assert reading_chunk_payload(incoming)['allowed_kinds']==['received']
    assert 'goal' in reading_chunk_payload(next(c for c in chunks if c['message_key']==own))['allowed_kinds']
    assert incoming['selected_addresses']==['alice@example.com']
    row={'kind':'received','statement':'有条件的来信建议','actor':'other@example.com','citations':[citation(incoming)]}
    assert validate_findings({'findings':[row]},chunks)==([],1)
    row['actor']='alice@example.com'
    accepted,rejected=validate_findings({'findings':[row]},chunks)
    assert not rejected and accepted[0]['actor']=='alice@example.com'


def test_registered_spans_cover_all_source_and_split_attribution_without_dropping_short_reply(person_data):
    repo,pid,add=person_data
    add('Own proposal text.\n> Quoted proposal text.\nMy final qualification. '+('Complete original wording. '*55))
    key=add('取消。',date='Wed, 08 May 2024 15:38:50 +0700')
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0],9000)
    spans=citation_span_payload(chunks)
    for chunk in chunks:
        own=[s for s in spans if s['chunk_id']==chunk['chunk_id']]
        assert ''.join(s['quote'] for s in own)==chunk['text']
        assert all(0<len(s['quote'])<=1200 for s in own)
        assert own[0]['char_start']==chunk['char_start'] and own[-1]['char_end']==chunk['char_end']
        assert all(a['char_end']==b['char_start'] for a,b in zip(own,own[1:]))
    short=next(c for c in chunks if c['message_key']==key)
    assert ''.join(s['quote'] for s in spans if s['chunk_id']==short['chunk_id'])==short['text']
    assert len({s['evidence_id'] for s in spans})==len(spans)


def test_span_id_resolution_is_exact_and_unknown_or_conflicting_ids_are_rejected(person_data):
    repo,pid,add=person_data;add();selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0]);spans=citation_span_payload(chunks)
    row={'kind':'goal','statement':'有来源的计划','actor':'alice@example.com','citations':[{'evidence_id':spans[0]['evidence_id']}]}
    value={'findings':[row]};expanded=resolve_citation_spans(value,spans,'findings')
    assert value['findings'][0]['citations']==[{'evidence_id':spans[0]['evidence_id']}]
    assert expanded['findings'][0]['citations'][0]['quote']==spans[0]['quote']
    accepted,rejected=validate_findings(expanded,chunks)
    assert not rejected and accepted[0]['citations'][0]['evidence_id']==spans[0]['evidence_id']
    for bad in ({'evidence_id':'unknown'}, {'evidence_id':'unknown',**citation(chunks[0])},
                {'evidence_id':spans[0]['evidence_id'],'quote':'invented'},
                {'evidence_id':spans[0]['evidence_id'],'chunk_id':'other'}):
        with pytest.raises(ValueError):resolve_citation_spans({'findings':[{**row,'citations':[bad]}]},spans,'findings')


def test_registered_id_does_not_turn_incoming_into_authored_intent(person_data):
    repo,pid,add=person_data;add('We propose proceeding with approval.',sender='bob@example.com',recipient='alice@example.com')
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0]);spans=citation_span_payload(chunks)
    row={'kind':'goal','statement':'不能冒充自写','actor':'alice@example.com','citations':[{'evidence_id':spans[0]['evidence_id']}]}
    assert validate_findings(resolve_citation_spans({'findings':[row]},spans,'findings'),chunks)==([],1)


def test_reading_and_review_accept_registered_ids_and_keep_full_original_quotes(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class IdModel(PersonModel):
        def chat_json(self,system,prompt,**kwargs):
            payload=json.loads(prompt);response=super().chat_json(system,prompt,**kwargs)
            if 'chunks' in payload or 'context_chunks' in payload:
                field='chunks' if 'chunks' in payload else 'context_chunks'
                assert all('text' not in c and c['evidence_ids'] for c in payload[field])
                item=kwargs['output_schema']['properties']['findings' if field=='chunks' else 'reviews']['items']
                item=item['oneOf'][0] if 'oneOf' in item else item
                assert item['properties']['contradictions']['items']['type']=='string'
                span=payload['citation_spans'][0]
                rows=response['json']['findings'] if 'chunks' in payload else response['json']['reviews']
                for row in rows:row['citations']=[{'evidence_id':span['evidence_id']}]
            return response
    model=IdModel();page=finish(repo,tid,model)
    assert page['progress']['complete']
    assert not any('prior_themes' in p for p in model.calls)
    assert not any('validation_repair' in p for p in model.calls)
    quoted=page['items'][0]['citations'][0]
    assert quoted['evidence_id']=='q1' and quoted['quote']==model.calls[0]['citation_spans'][0]['quote']
    assert page['context_reviews'][0]['reviews'][0]['citations'][0]['quote']==quoted['quote']
    assert page['package_id']==pid and page['address']=='alice@example.com' and page['addresses']==['alice@example.com']


def test_complete_short_authored_fragment_before_long_forward_is_citable(person_data):
    repo,pid,add=person_data;add('取消\nOriginal message\n'+('Earlier quoted negotiation details. '*20))
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    chunk=chunks[0];spans=citation_span_payload(chunks)
    short=next(s for s in spans if s['chunk_id']==chunk['chunk_id'] and s['quote'].strip()=='取消')
    row={'kind':'action','statement':'明确取消原计划','citations':[{'evidence_id':short['evidence_id']}]}
    accepted,rejected=validate_findings(resolve_citation_spans({'findings':[row]},spans,'findings'),chunks)
    assert not rejected and accepted[0]['citations'][0]['quote'].strip()=='取消'
    assert not accepted[0]['citations'][0]['quotation_warning']
    subword={**row,'citations':[{'chunk_id':chunk['chunk_id'],'quote':'消','char_start':1}]}
    assert validate_findings({'findings':[subword]},chunks)==([],1)


def test_complete_short_quoted_fragment_never_becomes_authored_goal(person_data):
    repo,pid,add=person_data;add('A longer authored explanation of this draft.\n> 取消\n'+('My own follow-up qualification. '*10))
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    chunk=chunks[0];spans=citation_span_payload(chunks)
    quoted=next(s for s in spans if s['chunk_id']==chunk['chunk_id'] and '取消' in s['quote'])
    row={'kind':'goal','statement':'不能冒充本人立场','citations':[{'evidence_id':quoted['evidence_id']}]}
    assert validate_findings(resolve_citation_spans({'findings':[row]},spans,'findings'),chunks)==([],1)
    row['kind']='received'
    accepted,rejected=validate_findings(resolve_citation_spans({'findings':[row]},spans,'findings'),chunks)
    assert not rejected and accepted[0]['citations'][0]['quotation_warning']


def test_short_substring_inside_long_authored_fragment_cannot_bypass_quote_minimum(person_data):
    repo,pid,add=person_data;add('取消前必须核实合同条件与原始发言归属，然后再决定是否继续。')
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    row={'kind':'goal','statement':'错误短词结论','citations':[{'chunk_id':chunks[0]['chunk_id'],'quote':'取消','char_start':0}]}
    assert validate_findings({'findings':[row]},chunks)==([],1)


def test_artificial_short_tail_merges_without_dropping_source_or_crossing_attribution(person_data):
    repo,pid,add=person_data;repo.text_segment_chars=3000;add('A'*905+'\n> 取消\n')
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    spans=citation_span_payload(chunks)
    for chunk in chunks:
        own=[s for s in spans if s['chunk_id']==chunk['chunk_id']]
        assert ''.join(s['quote'] for s in own)==chunk['text']
        assert all(len(s['quote'])<=1200 for s in own)
    assert len(spans[0]['quote'])==906
    assert '取消' not in spans[0]['quote'] and any('取消' in s['quote'] for s in spans[1:])


def test_single_source_registry_reconstructs_every_character_and_guides_string_memory(person_data):
    repo,pid,add=person_data;add('Complete real source; preserve every condition. '*35)
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    spans=citation_span_payload(chunks);payload={'chunks':span_source_metadata([reading_chunk_payload(c) for c in chunks],spans),'citation_spans':spans}
    assert all('text' not in c for c in payload['chunks'])
    reconstructed=decode_person_source_payload(payload)
    assert [c['text'] for c in reconstructed['chunks']]==[c['text'] for c in chunks]
    assert sum(len(s['quote']) for s in spans)==sum(len(c['text']) for c in chunks)
    for field,findings in [('findings',None),('reviews',[{'finding_id':'f1'}])]:
        schema=person_output_schema(field,spans,findings);item=schema['properties'][field]['items']
        item=item['oneOf'][0] if 'oneOf' in item else item;row=item['properties']
        for name in ('conditions','contradictions','unresolved_questions'):
            assert row[name]['type']=='array' and row[name]['items']['type']=='string'
        assert row['citations']['items']['properties']['evidence_id']['enum']==[s['evidence_id'] for s in spans]


def test_guided_change_requires_two_references_without_rewriting_single_source_action(person_data):
    repo,pid,add=person_data;add();selected=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selected,[0,0]);spans=citation_span_payload(chunks)
    ordinary,change=person_output_schema('findings',spans)['properties']['findings']['items']['oneOf']
    assert 'change' not in ordinary['properties']['kind']['enum'] and ordinary['properties']['citations']['minItems']==1
    assert change['properties']['kind']['enum']==['change'] and change['properties']['citations']['minItems']==2
    assert 'uniqueItems' not in change['properties']['citations']  # unsupported by the deployed guided decoder
    assert ordinary['properties']['actor']==change['properties']['actor']=={'type':'null'}
    row={'kind':'change','statement':'单封不得自动改类型','citations':[{'evidence_id':spans[0]['evidence_id']}]}
    expanded=resolve_citation_spans({'findings':[row]},spans,'findings')
    assert validate_findings(expanded,chunks)==([],1) and expanded['findings'][0]['kind']=='change'


def test_actor_is_resolved_from_source_roles_and_outgoing_attachment_stays_unknown(person_data):
    repo,pid,add=person_data;key=add(attachment=True)
    from test_person_context_upgrade import attachment_text
    attachment_text(repo,pid,key)
    selected=resolve_selection(repo,pid,'owner','alice@example.com');chunks,_=next_chunks(repo,pid,'owner',selected,[0,0])
    own=next(c for c in chunks if c['source_kind']=='body');attachment=next(c for c in chunks if c['source_kind']=='attachment')
    findings,rejected=validate_findings({'findings':[{'kind':'goal','statement':'核查合同','actor':None,'citations':[citation(own)]},
        {'kind':'received','statement':'附件载有须经审批的条款','actor':None,'citations':[citation(attachment)]}]},chunks)
    assert rejected==0 and findings[0]['actor']=='alice@example.com' and findings[1]['actor'] is None
    _,rejected=validate_findings({'findings':[{'kind':'received','statement':'发送者收到自己发出的附件',
        'actor':'alice@example.com','citations':[citation(attachment)]}]},chunks)
    assert rejected==1


def test_model_metadata_has_no_fragment_storage_text_and_keeps_source_attribution():
    chunks=[{'chunk_id':'c1','text':'取消。','participants':[{'role':'from','address':'alice@example.com','private_storage':'omit'}],
             'body_fragments':[{'fragment_kind':'authored','char_start':0,'char_end':3,'confidence':'strong','method':'internal','text':'must not be repeated'}],
             'conversations':[{'conversation_id':'long internal id'}],'reply_source_keys':['long ancestor id'],'cell_range':None}]
    spans=[{'evidence_id':'q1','chunk_id':'c1','char_start':0,'char_end':3,'quote':'取消。'}]
    metadata=span_source_metadata(chunks,spans)[0]
    assert 'text' not in metadata and 'conversations' not in metadata and 'reply_source_keys' not in metadata
    assert metadata['body_fragments']==[{'fragment_kind':'authored','char_start':0,'char_end':3,'confidence':'strong'}]
    assert metadata['participants']==[{'role':'from','address':'alice@example.com'}]
    assert chunks[0]['body_fragments'][0]['text']=='must not be repeated'


def test_confirmed_context_overflow_resizes_without_advancing_or_losing_coverage(person_data):
    from services.ollama import LocalModelError
    repo,pid,add=person_data;repo.text_segment_chars=3000
    add('One complete source with a genuine conditional qualification. '*80)
    tid=create_analysis(repo,pid)
    class OverflowOnce(PersonModel):
        def __init__(self):super().__init__();self.failed=False
        def chat_json(self,system,prompt,**kwargs):
            p=json.loads(prompt)
            if 'context_chunks' in p and not self.failed:
                self.failed=True
                raise LocalModelError('vLLM 输入包含 40000 个精确 token，已达到上下文上限 32768；需要调用方分片，未裁剪输入证据。')
            return super().chat_json(system,prompt,**kwargs)
    model=OverflowOnce()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(50):
            before=repo.get_turn(tid,'owner')['final']
            run_person_slice(repo,tid,'owner',model)
            after=repo.get_turn(tid,'owner')['final']
            if after.get('context_capacity_recoveries'):
                assert after['context_review']['cursor']==before['context_review']['cursor']==[0,0]
                assert after['context_review']['characters_read']==before['context_review']['characters_read']==0
                assert after['context_review'].get('assessments')==before['context_review'].get('assessments')
                assert 512<=after['context_review']['context_char_budget']<6000
                assert after['pending_token_reservation'] is None
                break
        else:raise AssertionError('overflow recovery did not execute')
    page=finish(repo,tid,model)
    assert page['progress']['complete'] and page['progress']['context_capacity_recoveries']==1
    assert page['progress']['characters_read']==page['progress']['corpus']['characters']
    assert page['progress']['context_review']['characters_read']==page['progress']['corpus']['characters']


def test_timeout_is_not_reclassified_as_capacity_or_resized(person_data):
    from services.ollama import LocalModelError
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class Timeout(PersonModel):
        def chat_json(self,system,prompt,**kwargs):
            if 'context_chunks' in json.loads(prompt):raise LocalModelError('请求 timed out')
            return super().chat_json(system,prompt,**kwargs)
    with pytest.raises(LocalModelError,match='timed out'):finish(repo,tid,Timeout())
    state=repo.get_turn(tid,'owner')['final']
    assert not state.get('context_capacity_recoveries') and 'context_char_budget' not in state['context_review']
    assert state['context_review']['cursor']==[0,0]


def test_semantic_model_registry_keeps_complete_text_once_and_preserves_business_fields():
    fragment={'fragment_kind':'authored','char_start':0,'char_end':45,'text':'Price is 90,000, subject to written approval.','confidence':'strong'}
    source={'evidence_id':'e1','message_key':'m1','mime_path':'1','quote':'Price is 90,000','quote_text':'Price is 90,000',
            'actor':'alice@example.com','sent_at_utc':'2026-10-01T00:00:00Z','polarity':'positive','body_fragments':[fragment]}
    payload={'claims':[{'claim_id':'f1','claim_text':'报价须经审批','actor':'alice@example.com','conditions':['须经审批'],
             'citations':[{**source,'char_start':0,'char_end':15}]}],
             'evidence':[source,{**source,'evidence_id':'e2','quote':'written approval','quote_text':'written approval'}],
             'qualification_checks':{'f1':[{'evidence_id':'e1','quote':'subject to written approval','char_start':17}]}}
    compact=compact_semantic_payload(payload)
    assert len(compact['source_fragments'])==1 and compact['source_fragments'][0]['text']==fragment['text']
    assert compact['evidence'][0]['actor']==source['actor'] and compact['evidence'][0]['sent_at_utc']==source['sent_at_utc']
    assert compact['evidence'][0]['quote_text']==source['quote_text'] and 'quote' not in compact['evidence'][0]
    assert compact['claims'][0]['conditions']==['须经审批'] and compact['qualification_checks']==payload['qualification_checks']
    assert compact['claims'][0]['citations']==[{'evidence_id':'e1','char_start':0,'char_end':15}]
    assert payload['claims'][0]['citations'][0]['body_fragments'][0]['text']==fragment['text']


def test_extractive_overview_never_reassigns_unrelated_source_ids_or_invents_delivery_status():
    base={'kind':'received','actor':'alice@example.com','uncertainty':'保留源陈述限定','review_status':'supported','semantic_status':'supported',
          'conditions':['须按原条件'],'contradictions':[],'unresolved_questions':[]}
    findings=[]
    for i in range(17):
        project='南屿项目' if i<8 else '蓝湾项目'
        findings.append({**base,'finding_id':'f%d'%i,'statement':'客户称未收到设备；履行情况仍需核实。' if i==16 else project+'的邮件陈述%d'%i,
            'citations':[{'message_key':'m%d'%i,'quote':'original source','sent_at':'2026-10-%02dT00:00:00Z'%(i+1),
             'subject':project+'的真实主题','conversations':[{'conversation_id':'thread%d'%i}]}]})
    themes=extractive_themes(findings)
    assert len(themes)==17 and {i for t in themes for i in t['finding_ids']}=={'f%d'%i for i in range(17)}
    for theme in themes:
        original=findings[int(theme['finding_ids'][0][1:])]
        assert original['statement'] in theme['statement'] and original['citations'][0]['subject'] in theme['statement']
        assert '实际交付未完成' not in theme['statement']+theme['uncertainty']
        assert theme['evidence_refs'][0]['finding_id']==original['finding_id']


def test_semantic_pages_audit_proposition_without_display_prefix_and_resume_exact_cached_pages(person_data):
    repo,pid,add=person_data
    for i in range(7):add('I intend to review agreement %d before proceeding.'%i)
    tid=create_analysis(repo,pid)
    class PageModel(PersonModel):
        def __init__(self):super().__init__();self.semantic_calls=0;self.failed=False
        def chat_json(self,system,prompt,**kwargs):
            p=json.loads(prompt)
            if 'claims' in p:
                assert len(p['claims'])<=3 and all(not c['claim_text'].startswith('待核实的模型解读') for c in p['claims'])
                self.semantic_calls+=1
                if self.semantic_calls==2 and not self.failed:self.failed=True;raise RuntimeError('semantic temporary outage')
            return super().chat_json(system,prompt,**kwargs)
    model=PageModel()
    with pytest.raises(RuntimeError,match='temporary outage'):finish(repo,tid,model)
    with repo._connection() as c:
        assert c.execute("SELECT COUNT(*) FROM mail_research_steps WHERE turn_id=? AND tool_name='person_semantic_part'",(tid,)).fetchone()[0]==1
    assert model.semantic_calls==2
    page=finish(repo,tid,model)
    assert page['progress']['complete'] and model.semantic_calls==4
    assert page['progress']['synthesis']['method']=='source_bound_extractive'
    assert {i for t in page['progress']['synthesis']['themes'] for i in t['finding_ids']}=={f['finding_id'] for f in page['items']}
    assert all(f['statement'].startswith('待核实的模型解读') for f in page['items'])


def test_rebuild_from_cached_negative_semantic_results_recomputes_count(person_data):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    class NegativeModel(PersonModel):
        def chat_json(self,*args,**kwargs):
            response=super().chat_json(*args,**kwargs)
            for verdict in response['json'].get('verdicts',[]):
                for atom in verdict['atoms']:atom.update(status='insufficient',missing_requirements=['原文不支持肯定判断'])
            return response
    model=NegativeModel();first=finish(repo,tid,model)
    assert first['progress']['semantic_unverified_findings']>0
    calls=len(model.calls)
    turn=repo.get_turn(tid,'owner');state={**turn['final'],'phase':'synthesis','complete':False,'semantic_unverified_findings':0,
                                     'synthesis':{'batches_read':0,'themes':[]}}
    repo.update_turn(tid,'owner',status='queued',final=state)
    rebuilt=finish(repo,tid,model)
    assert len(model.calls)==calls
    assert rebuilt['progress']['semantic_unverified_findings']==first['progress']['semantic_unverified_findings']
    assert repo.get_turn(tid,'owner')['sufficiency']=='partial'


def test_semantic_reference_protocol_runs_through_durable_workflow(person_data):
    repo,pid,add=person_data;add('I intend to review the agreement subject to written approval.');tid=create_analysis(repo,pid)
    class ReferenceModel(PersonModel):
        def __init__(self):super().__init__();self.reference_calls=0
        def chat_json(self,system,prompt,**kwargs):
            payload=json.loads(prompt)
            if 'claims' not in payload:return super().chat_json(system,prompt,**kwargs)
            self.reference_calls+=1
            assert any(payload['qualification_checks'].values())
            assert kwargs['output_schema']['properties']['verdicts']['items']['oneOf'][0]['properties']['atoms']['items']['required']==['text','status','source_refs','missing_requirements']
            refs={s['evidence_id']:s['ref_id'] for s in payload['source_reference_spans'] if s['complete_source']}
            return {'json':{'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'insufficient',
                'source_refs':[{'ref_id':refs[e],'role':'support'} for e in c['evidence_ids']],
                'missing_requirements':['行动是否落实仍需核实']}],
                'qualification_reviews':[{'check_id':q['check_id'],'applicability':'unresolved','rationale':'保留书面批准条件，落实情况未知'}
                    for q in payload['qualification_checks'][c['claim_id']]]} for c in payload['claims']]}}
    model=ReferenceModel();page=finish(repo,tid,model)
    assert page['progress']['complete'] and model.reference_calls==1
    for finding in page['items']:
        assert finding['semantic_status']=='insufficient'
        assert finding['semantic_verification']['atoms'][0]['source_quotes']
        assert finding['semantic_verification']['qualification_scope']['reviewed']
        assert 'qualification_scope_unresolved' in finding['semantic_verification']['missing_requirements']
        assert not {'judge_quote_not_exact','qualification_scope_unproven'}.intersection(finding['semantic_verification']['missing_requirements'])


def test_expanded_legacy_source_cannot_inherit_short_authored_flag_for_forwarded_tail(person_data):
    from services.mail.issue_repository import audit_workflow_findings
    from services.mail.person_corpus import resolve_selection
    repo,pid,add=person_data
    add('I plan to review this agreement before proceeding.\n\n-----Original Message-----\nI accepted the forwarded agreement.')
    selection=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selection,[0,0]);finding=_findings(chunks)[0]
    finding.update(actor='alice@example.com',statement='I accepted the forwarded agreement.')
    with repo._connection() as c:c.execute('DELETE FROM mail_body_fragments WHERE message_key=?',(chunks[0]['message_key'],))
    def judge(system,payload):
        source=payload['evidence'][0];claim=payload['claims'][0]
        assert source['attribution']=='unknown' and not source['authored']
        return {'verdicts':[{'claim_id':claim['claim_id'],'atoms':[{'text':claim['claim_text'],'status':'supported',
            'support_evidence_ids':[source['evidence_id']],'source_quotes':[{'evidence_id':source['evidence_id'],
              'quote':'I accepted the forwarded agreement.'}]}],'qualification_reviews':[]}]}
    checked=audit_workflow_findings(repo,{'package_id':pid},'owner',[finding],judge,selection=selection)
    assert checked[0]['semantic_status']=='insufficient'
    assert 'atomic_actor_authorship_unproven' in checked[0]['semantic_verification']['missing_requirements']
