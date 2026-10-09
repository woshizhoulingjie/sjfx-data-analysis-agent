"""Short verified content, bound envelope metadata and literal restrictions."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from services.mail.research.answer_synthesis import synthesize
from services.mail.research.claim_verifier import verify_claims
from services.mail.research.verifier import verify_numeric, verify_temporal, verify_polarity, qualification_scope
from test_mail_answer_synthesis import setup


def fixture_sources():
    fixture=json.loads((Path(__file__).parent/'fixtures'/'mail_question_v6_source_local_failure.json').read_text(encoding='utf-8'))
    payload=fixture['generation_prompt'];documents=payload['source_documents'];blocks=payload['source_blocks']
    sources=[{**documents.get('_defaults',{}),**documents[blocks[row['source_ref']]['document_ref']],
        **{k:v for k,v in blocks[row['source_ref']].items() if k!='document_ref'},
        **{k:v for k,v in row.items() if k!='source_ref'}} for row in payload['evidence']]
    return fixture,sources


def test_real_v6_negative_keeps_exact_temporal_polarity_qualification_and_history_failures():
    fixture,sources=fixture_sources()
    checked=verify_claims(fixture['generation']['claims'],{s['evidence_id']:s for s in sources},
        lambda system,payload:deepcopy(fixture['judge']),_repair=False)
    assert checked[0]['semantic_status']=='insufficient' and checked[2]['semantic_status']=='insufficient'
    first=checked[0]['semantic_verification'];third=checked[2]['semantic_verification']
    assert {'applicable_qualification_omitted','temporal_scope_or_full_history_unproven',
        'atomic_number_time_negation_or_condition_mismatch'}<=set(first['missing_requirements'])
    for index,expected in [(0,(True,False,True)),(2,(True,True,False))]:
        atom=checked[index]['semantic_verification']['atoms'][0];quote='\n'.join(q['quote'] for q in atom['source_quotes'])
        assert (verify_numeric(atom['text'],quote),verify_temporal(atom['text'],quote),verify_polarity(atom['text'],quote))==expected
    assert 'atomic_number_time_negation_or_condition_mismatch' in third['missing_requirements']


def local_setup():
    fixture,sources=fixture_sources();runtime,state,_,calls,_=setup()
    keys=sorted({s['message_key'] for s in sources})
    state.update(allowed_keys=set(keys),analysis_completed=False,
        context_reviews={key:{'complete':False,'gaps':['uncertain_thread_links'],'message_keys':keys,
            'edges':[],'evidence_ids':[s['evidence_id'] for s in sources]} for key in keys})
    original=fixture['generation']['claims']
    contents=['蓝湾项目约定交付日期为2026年10月18日。','承建方声明已完成交付。',
        '蓝湾项目未交付、未验收。','只有双方签署《验收报告》才视为交付完成。']
    generated={'claims':[], 'unanswered_subquestions':deepcopy(fixture['generation']['unanswered_subquestions'])}
    for index,text in enumerate(contents):
        old=original[min(index,2)];row={**deepcopy(old),'claim_text':text,'conditions':[]}
        if index==0:row['evidence_ids']=old['evidence_ids'][:1]
        if index==3:row['source_quote']='只有双方签署《验收报告》之日方视为交付完成日；'
        # Model-provided display metadata must never be accepted.
        row.update(recorded_at='2099-01-01',recorded_sender='forged@example.com',recorded_source={'evidence_id':'foreign'})
        generated['claims'].append(row)
    options={'background':'background','background_reason':'属于可选背景来源表述','bad_qualification':False,
        'bad_direct':False,'missing_direct':False}
    def model(t,o,current,purpose,system,prompt,**kwargs):
        current['model_calls']+=1;payload=json.loads(prompt);calls.append((purpose,payload))
        if purpose=='answer_synthesis':return deepcopy(generated)
        if purpose=='answer_synthesis_repair':return {'repairs':[]}
        verdicts=[]
        for row in payload['claims']:
            index=contents.index(row['claim_text']) if row['claim_text'] in contents else 0
            relevance='direct' if index==0 else options['background'] if index==1 else 'qualification'
            reason=options['background_reason'] if index==1 else '约定日期直接回答；反证与验收条件限定答复'
            status='insufficient' if (index==0 and options['bad_direct']) or (index>=2 and options['bad_qualification']) else 'supported'
            verdict={'claim_id':row['claim_id'],'answer_relevance':relevance,'answer_relevance_reason':reason,
                'atoms':[{'text':row['claim_text'],'status':status,'support_evidence_ids':row['evidence_ids'] if status=='supported' else [],
                    'counter_evidence_ids':[],'source_quotes':[{'evidence_id':row['evidence_ids'][0],'quote':row['source_quote']}]
                        if status=='supported' else [],'missing_requirements':[] if status=='supported' else ['原语义仍缺支持']}],
                'qualification_reviews':[{**check,'applicability':'applicable','rationale':'逐字保留该来源限制，履行情况未认定'}
                    for check in payload['qualification_checks'][row['claim_id']]]}
            if index==0 and options['missing_direct']:verdict.pop('answer_relevance')
            verdicts.append(verdict)
        return {'verdicts':verdicts}
    runtime._model_call=model
    return runtime,state,sources,calls,generated,options


def execute(runtime,state,sources):
    return synthesize(runtime,'t','o',state,{'objective':'蓝湾项目最终交付是哪一天','mode':'research',
        'budget':{'max_context_tokens':12000}},sources,[],direct=True)


def test_short_content_passes_original_gates_with_bound_metadata_restrictions_counter_and_unknown():
    runtime,state,sources,calls,generated,_=local_setup();original=deepcopy(generated)
    assert execute(runtime,state,sources)
    assert state['answer_generation_raw']==original and generated==original
    synthesis=state['answer_synthesis'];assert synthesis['status']=='complete'
    assert len(synthesis['answer_claims'])==3 and len(synthesis['background_claims'])==1
    assert not synthesis['source_coverage']['graph_history_complete']
    for row in synthesis['claims']:
        source=next(s for s in sources if s['evidence_id']==row['recorded_source']['evidence_id'])
        assert row['recorded_at']==source['sent_at_utc'] and row['recorded_sender']!='forged@example.com'
        assert row['recorded_source']['evidence_id'] in row['evidence_ids']
        assert row['source_quote'] in source['quote_text']
        expected=[q for eid in row['evidence_ids'] for q in qualification_scope(
            next(s for s in sources if s['evidence_id']==eid),row['source_quote'],row['object_ref'])[1]]
        assert [{k:q[k] for k in ('evidence_id','quote','char_start')} for q in row['source_qualifications']]==expected
        assert all(q['quote'] in row['conditions'] and 'applicability' not in q and 'fulfilled' not in q for q in row['source_qualifications'])
        assert row['semantic_verification']['qualification_scope']['complete']
    state.update(findings=[],context_gaps=[])
    final=runtime._answer({'mode':'research'},sources,{}, {'matched':8},0,state,[])
    appendix=final['evidence_appendix']['recorded_answer']
    assert '2026-10-09T01:20:00+00:00' in appendix and 'li.qiang@shengyuan-zn.cn' in appendix
    assert '2026-10-21T01:25:00+00:00' in appendix and '未交付、未验收' in final['answer']
    assert '2026年10月18日' in final['answer'] and '双方签署《验收报告》' in final['answer']
    assert '若再次逾期' in appendix and '条件履行情况待核实' in appendix and '未知' in final['answer']
    assert 'li.qiang@shengyuan-zn.cn' not in final['answer']
    assert '承建方声明已完成交付' not in final['answer'] and '2099' not in final['answer']
    for purpose,payload in calls:
        if purpose=='semantic_claim_verification':
            assert payload['relationship_warnings']==synthesis['relationship_warnings']
            assert {r['evidence_id'] for r in payload['source_records']}=={s['evidence_id'] for s in sources}


@pytest.mark.parametrize('failure',['unknown_background','missing_background_reason','bad_direct','bad_qualification','missing_direct'])
def test_background_filter_never_hides_required_unknown_direct_or_qualification_failure(failure):
    runtime,state,sources,_,_,options=local_setup()
    if failure=='unknown_background':options['background']='unknown'
    elif failure=='missing_background_reason':options['background_reason']=''
    else:options[failure]=True
    assert not execute(runtime,state,sources) and state['answer_synthesis']['status']=='partial'


def test_envelope_date_never_substitutes_for_unquoted_body_event_date_or_package_final():
    for text,missing in [('蓝湾项目于2026年10月9日实际完成交付。','primary_quote_temporal_mismatch'),
        ('蓝湾项目最终实际交付日期为2026年10月18日。','temporal_scope_or_full_history_unproven')]:
        runtime,state,sources,_,generated,_=local_setup();generated['claims'][0]['claim_text']=text
        assert not execute(runtime,state,sources)
        assert state['answer_synthesis']['status']=='unavailable'
        assert missing in state['answer_content_precheck']['failures'][0]['errors']


def test_attachment_envelope_identifies_mail_record_without_assigning_attachment_authorship():
    runtime,state,sources,calls,generated,_=local_setup()
    source=next(s for s in sources if s['source_kind']=='attachment' and '本项目最终交付日期确认为' in s['quote_text'])
    generated['claims']= [{'claim_text':'本项目约定交付日期为2026年10月18日。','evidence_ids':[source['evidence_id']],
        'source_quote':'本项目最终交付日期确认为\n2026年10月18日','object_ref':'本项目','actor':None,'conditions':[],
        'recorded_sender':'fake-attachment-author@example.com'}]
    assert execute(runtime,state,sources)
    state.update(findings=[],context_gaps=[])
    final=runtime._answer({'mode':'research'},sources,{}, {'matched':8},0,state,[])
    assert '邮件附件记载' in final['evidence_appendix']['recorded_answer'] and 'fake-attachment-author' not in final['answer']


def test_only_same_source_qualifications_are_preserved_and_quoted_original_is_not_sender_commitment():
    runtime,state,sources,calls,_=setup();base=runtime._model_call
    original=sources[1]['quote_text'];quoted='若项目乙通过审批，才可另行订货。'
    sources[1]['quote_text']=original+'\n'+quoted;sources[1]['char_end']=len(sources[1]['quote_text'])
    sources[1]['body_fragments'].append({'fragment_kind':'quoted','char_start':len(original)+1,
        'char_end':len(sources[1]['quote_text'])})
    foreign='若另一个来源审批通过，才交付其他设备。'
    sources[2]['quote_text']+='\n'+foreign
    def model(*args,**kwargs):
        result=base(*args,**kwargs)
        if args[3]=='answer_synthesis':
            result['claims'][0].update(claim_text=original,source_quote=original)
        if args[3]=='semantic_claim_verification':
            for verdict in result['verdicts']:
                for check in verdict['qualification_reviews']:
                    if check['quote']==quoted:check.update(applicability='not_applicable',rationale='引用区为另一个项目的独立采购条件')
        return result
    runtime._model_call=model
    assert synthesize(runtime,'t','o',state,{'objective':'项目甲交付是哪一天','mode':'research','budget':{'max_context_tokens':12000}},sources,[],direct=True)
    row=state['answer_synthesis']['claims'][0]
    assert row['conditions']==[quoted] and row['source_qualifications'][0]['attribution']=='quoted'
    assert foreign not in row['conditions'] and row['recorded_sender']=='alice@example.com'
    state.update(findings=[],context_gaps=[])
    final=runtime._answer({'mode':'research'},sources,{}, {'matched':5},0,state,[])
    assert '引用区原文：'+quoted in final['evidence_appendix']['recorded_answer'] and foreign not in final['answer']


@pytest.mark.parametrize('order',['qualification_then_background','background_then_qualification','repair_unique_background'])
def test_duplicate_counter_verdict_cannot_hide_required_failure_as_background(order):
    runtime,state,sources,calls,_,_=local_setup();base=runtime._model_call
    def model(*args,**kwargs):
        result=base(*args,**kwargs)
        if args[3]=='semantic_claim_verification':
            verdict=next(v for v in result['verdicts'] if v['atoms'][0]['text']=='蓝湾项目未交付、未验收。')
            duplicate=deepcopy(verdict);duplicate.update(answer_relevance='background',answer_relevance_reason='重复行试图排除反证')
            if order=='repair_unique_background' and json.loads(args[5]).get('validation_errors'):
                result['verdicts']=[duplicate]
            elif order in ('qualification_then_background','repair_unique_background'):result['verdicts'].append(duplicate)
            else:result['verdicts'].insert(0,duplicate)
        return result
    runtime._model_call=model
    assert not execute(runtime,state,sources) and state['answer_synthesis']['status']=='partial'
    counter=next(row for row in state['answer_synthesis']['claims'] if row['claim_text']=='蓝湾项目未交付、未验收。')
    assert counter['answer_relevance']=='unknown' and counter['semantic_status']=='insufficient'
    assert counter not in state['answer_synthesis']['background_claims']


@pytest.mark.parametrize('original_role',['qualification','direct','unknown'])
def test_citation_repair_cannot_relabel_required_counter_to_background(original_role):
    runtime,state,sources,calls,_,_=local_setup();base=runtime._model_call
    def model(*args,**kwargs):
        result=base(*args,**kwargs)
        if args[3]!='semantic_claim_verification':return result
        payload=json.loads(args[5])
        for verdict in result['verdicts']:
            if verdict['atoms'][0]['text']!='蓝湾项目未交付、未验收。':continue
            if payload.get('validation_errors'):
                verdict.update(answer_relevance='background',answer_relevance_reason='修复引文后试图移除原需保留反证')
            else:
                verdict['answer_relevance']=original_role
                verdict['atoms'][0]['source_quotes'][0]['quote']='错误引文以触发原citation repair'
        return result
    runtime._model_call=model
    assert not execute(runtime,state,sources) and state['answer_synthesis']['status']=='partial'
    assert any(payload.get('validation_errors') for purpose,payload in calls if purpose=='semantic_claim_verification')
    counter=next(row for row in state['answer_synthesis']['claims'] if row['claim_text']=='蓝湾项目未交付、未验收。')
    assert counter['answer_relevance']==original_role and counter['semantic_status']=='insufficient'
    assert counter not in state['answer_synthesis']['background_claims']


def test_ambiguous_candidate_claim_id_never_enters_background_filter():
    runtime,state,sources,_,generated,_=local_setup()
    generated['claims'][1]['claim_id']='duplicate-candidate'
    generated['claims'][2]['claim_id']='duplicate-candidate'
    assert not execute(runtime,state,sources)
    ambiguous=[row for row in state['answer_synthesis']['claims'] if
        'ambiguous_claim_id' in row['semantic_verification']['missing_requirements']]
    assert len(ambiguous)==2 and all(row['answer_relevance']=='unknown' for row in ambiguous)
    assert all(row not in state['answer_synthesis']['background_claims'] for row in ambiguous)
