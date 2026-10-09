"""One unaccepted proposal must satisfy the same strict CONTENT profile twice."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from services.mail.research.answer_synthesis import synthesize, _atom_profile_errors
from services.mail.research.claim_verifier import verify_claims
from services.mail.research.verifier import verify_numeric, verify_temporal, verify_polarity
from test_mail_answer_synthesis import setup


@pytest.fixture(autouse=True)
def exercise_bounded_reproposal_path_without_optional_literal_projection(monkeypatch):
    # These v9 regressions isolate the mandatory CONTENT/repair gates. The
    # narrower v10 literal proposal and its exclusions have separate tests.
    monkeypatch.setattr('services.mail.research.answer_synthesis._source_local_date_proposal',
        lambda row,by_id,errors:None)
    monkeypatch.setattr('services.mail.research.answer_synthesis._literal_content_proposal',
        lambda row,by_id,errors:None)


def fixture_sources():
    fixture=json.loads((Path(__file__).parent/'fixtures'/'mail_question_v8_content_failure.json').read_text(encoding='utf-8'))
    payload=fixture['generation_prompt'];documents=payload['source_documents'];blocks=payload['source_blocks']
    sources=[{**documents.get('_defaults',{}),**documents[blocks[row['source_ref']]['document_ref']],
        **{k:v for k,v in blocks[row['source_ref']].items() if k!='document_ref'},
        **{k:v for k,v in row.items() if k!='source_ref'}} for row in payload['evidence']]
    return fixture,sources


def test_actual_v8_root_gate_is_polarity_while_new_primary_profile_independently_rejects_unquoted_date():
    fixture,sources=fixture_sources();rows=fixture['generation']['claims'];by_id={s['evidence_id']:s for s in sources}
    profile=[_atom_profile_errors(row,by_id) for row in rows]
    assert profile[0]==[] and profile[2]==[]
    assert profile[1]==['temporal_scope_or_full_history_unproven']
    assert profile[3]==['primary_quote_numeric_mismatch','primary_quote_temporal_mismatch']
    # The original full judge quotes include the date. Their actual error is
    # polarity, not the independently stricter v9 primary quote contract.
    checked=verify_claims(rows,by_id,lambda system,payload:deepcopy(fixture['judge']),_repair=False)
    atom=checked[3]['semantic_verification']['atoms'][0];quotes='\n'.join(q['quote'] for q in atom['source_quotes'])
    assert (verify_numeric(atom['text'],quotes),verify_temporal(atom['text'],quotes),verify_polarity(atom['text'],quotes))==(True,True,False)
    assert 'atomic_number_time_negation_or_condition_mismatch' in checked[3]['semantic_verification']['missing_requirements']


def candidate_setup():
    fixture,sources=fixture_sources();runtime,state,_,calls,_=setup();generated=deepcopy(fixture['generation'])
    keys=sorted({s['message_key'] for s in sources});state.update(allowed_keys=set(keys),analysis_completed=False,
        context_reviews={key:{'complete':False,'gaps':['uncertain_thread_links'],'message_keys':keys,'edges':[],
            'evidence_ids':[s['evidence_id'] for s in sources]} for key in keys})
    options={'repair_failure':None,'semantic_failure':False,'mixed_atom_qualifier':False,'keep_date_with_full_quote':False}
    def model(t,o,current,purpose,system,prompt,**kwargs):
        current['model_calls']+=1;payload=json.loads(prompt);calls.append((purpose,payload))
        if purpose=='answer_synthesis':return deepcopy(generated)
        if purpose=='answer_synthesis_repair':
            assert [row['atom_index'] for row in payload['format_repairs']]==[1,3]
            repairs=[]
            for index in (1,3):
                original=generated['claims'][index];row={'atom_index':index,**{k:deepcopy(original[k])
                    for k in ('claim_text','evidence_ids','source_quote','actor','object_ref')}}
                row['claim_text']='蓝湾项目约定交付日期为2026年10月18日。' if index==1 else '蓝湾项目未交付、未验收。'
                if index==3 and options['keep_date_with_full_quote']:
                    row['claim_text']='截至2026年10月21日09:00，蓝湾项目现场尚未收到任何设备。'
                    row['source_quote']='截至 2026 年 10 月 21 日 09:00，我司蓝湾项目现场尚未收到贵司发出的任何设备；'
                if index==1 and options['repair_failure']=='final':row['claim_text']=original['claim_text']
                if index==3 and options['repair_failure']=='date':row['claim_text']=original['claim_text']
                if index==3 and options['repair_failure']=='numeric':row['claim_text']='蓝湾项目已交付100台设备。'
                if index==3 and options['repair_failure']=='polarity':
                    row['source_quote']='只有双方签署《验收报告》之日方视为交付完成日；'
                    row['claim_text']='蓝湾项目已完成交付。'
                if options['repair_failure']=='valid_row':row['atom_index']=0
                if options['repair_failure']=='conditions':row['conditions']=[]
                repairs.append(row)
            return {'repairs':repairs}
        verdicts=[]
        for row in payload['claims']:
            original=next(c for c in generated['claims'] if c['evidence_ids']==row['evidence_ids'])
            index=generated['claims'].index(original);role='direct' if index==1 else 'qualification' if index==3 else 'background'
            status='insufficient' if options['semantic_failure'] and index==1 else 'supported'
            atom_quotes=[{'evidence_id':row['evidence_ids'][0],'quote':row['source_quote']}]
            if index==3 and options['mixed_atom_qualifier']:
                atom_quotes.append({'evidence_id':row['evidence_ids'][0],'quote':'只有双方签署《验收报告》之日方视为交付完成日；'})
            verdicts.append({'claim_id':row['claim_id'],'answer_relevance':role,'answer_relevance_reason':'约定期限直接回答；状态反证必要；旧方案或自报仅背景',
                'atoms':[{'text':row['claim_text'],'status':status,'support_evidence_ids':row['evidence_ids'][:1] if status=='supported' else [],
                    'counter_evidence_ids':[],'source_quotes':atom_quotes if status=='supported' else [],
                    'missing_requirements':[] if status=='supported' else ['原语义判决未支持']}],
                'qualification_reviews':[{**check,'applicability':'applicable','rationale':'保留同源限制而非确认其已履行'}
                    for check in payload['qualification_checks'][row['claim_id']]]})
        return {'verdicts':verdicts}
    runtime._model_call=model
    return runtime,state,sources,calls,generated,options


def execute(runtime,state,sources):
    return synthesize(runtime,'t','o',state,{'objective':'蓝湾项目最终交付是哪一天','mode':'research',
        'budget':{'max_context_tokens':12000}},sources,[],direct=True)


def test_actual_v8_only_two_unaccepted_bad_contents_share_one_reproposal_with_valid_rows_and_raw_unchanged():
    runtime,state,sources,calls,generated,_=candidate_setup();before=deepcopy(generated)
    assert execute(runtime,state,sources)
    assert generated==before and state['answer_generation_raw']==before
    assert state['answer_content_precheck']['status']=='repaired'
    assert [purpose for purpose,_ in calls].count('answer_synthesis_repair')==1
    assert state['answer_format_repair']['atom_indexes']==[1,3]
    judged=next(payload['claims'] for purpose,payload in calls if purpose=='semantic_claim_verification')
    for index in (0,2):
        for key in ('claim_text','evidence_ids','source_quote','actor','object_ref','time_basis'):
            assert judged[index][key]==before['claims'][index][key]
    for index,row in enumerate(judged):
        original=before['claims'][index]
        assert row['conditions'][:len(original['conditions'])]==original['conditions']
        assert row['time_basis']==original['time_basis']
    assert state['answer_synthesis']['unanswered_subquestions_raw']==before['unanswered_subquestions']
    assert state['answer_synthesis']['claims'][3]['recorded_at'].startswith('2026-10-21')
    assert not any('最终' in row['claim_text'] for row in state['answer_synthesis']['answer_claims'])
    for purpose,payload in calls:
        if purpose=='semantic_claim_verification':assert payload['content_profile']['qualifications_reviewed_separately']


@pytest.mark.parametrize('failure',['final','date','numeric','polarity','valid_row','conditions'])
def test_reproposal_failing_profile_or_immutability_is_rejected_before_any_judge_without_second_attempt(failure):
    runtime,state,sources,calls,generated,options=candidate_setup();options['repair_failure']=failure
    assert not execute(runtime,state,sources)
    assert state['answer_format_repair']['status']=='rejected' and state['answer_content_precheck']['status']=='rejected'
    assert [purpose for purpose,_ in calls].count('answer_synthesis_repair')==1
    assert not any(purpose=='semantic_claim_verification' for purpose,_ in calls)
    assert state['answer_synthesis']['unanswered_subquestions_raw']==generated['unanswered_subquestions']


def test_real_body_date_can_remain_when_reproposal_supplies_complete_same_source_literal_quote():
    runtime,state,sources,_,_,options=candidate_setup();options['keep_date_with_full_quote']=True
    assert execute(runtime,state,sources)
    row=state['answer_synthesis']['claims'][3]
    assert '2026年10月21日09:00' in row['claim_text'] and row['semantic_status']=='supported'
    assert '2026 年 10 月 21 日 09:00' in row['source_quote']


@pytest.mark.parametrize('failure',['semantic','mixed_qualifier'])
def test_passing_generation_profile_still_faces_unchanged_semantic_and_atom_polarity_gates(failure):
    runtime,state,sources,calls,_,options=candidate_setup()
    options['semantic_failure']=failure=='semantic';options['mixed_atom_qualifier']=failure=='mixed_qualifier'
    assert not execute(runtime,state,sources)
    assert state['answer_content_precheck']['status']=='repaired'
    assert [purpose for purpose,_ in calls].count('answer_synthesis_repair')==1
    assert any(purpose=='semantic_claim_verification' for purpose,_ in calls)
    if failure=='mixed_qualifier':
        assert 'atomic_number_time_negation_or_condition_mismatch' in state['answer_synthesis']['claims'][3]['semantic_verification']['missing_requirements']
