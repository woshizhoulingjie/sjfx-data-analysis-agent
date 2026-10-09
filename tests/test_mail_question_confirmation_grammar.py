"""The one-word pure-date variant does not broaden historical projection."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from services.mail.research.answer_synthesis import _atom_profile_errors,_authored_atom_sources,_source_local_date_proposal,synthesize
from test_mail_question_literal_projection import actual_v9
from test_mail_answer_synthesis import setup


def production_fixture():
    return json.loads((Path(__file__).parent/'fixtures'/'mail_question_v12_production_confirmation_failure.json').read_text(encoding='utf-8'))


def test_actual_production_raw_confirmation_variant_and_rejected_repair_are_preserved_verbatim():
    fixture=production_fixture();row=fixture['generation']['claims'][1]
    assert row['claim_text']=='蓝湾项目最终交付日期确认为2026年10月18日。'
    assert row['source_quote']=='双方最终确认的交付日期为：2026 年 10 月 18 日。'
    assert fixture['rejected_repair']['status']=='rejected'
    assert fixture['candidate_audit_steps'][-1]['tool_name']=='answer_candidate_audit'


def test_confirmation_variant_on_known_exact_bound_source_changes_only_content():
    # A separate known-source unit regression, not a substituted production ID.
    fixture,_,by_id=actual_v9();row=deepcopy(fixture['generation']['claims'][1])
    row['claim_text']='蓝湾项目最终交付日期确认为2026年10月18日。';before=deepcopy(row)
    proposal=_source_local_date_proposal(row,by_id,_atom_profile_errors(row,by_id))
    assert proposal['claim_text']==row['source_quote'].replace('最终','',1)
    assert _atom_profile_errors(proposal,by_id)==[] and row==before
    assert {k:v for k,v in proposal.items() if k!='claim_text'}=={k:v for k,v in row.items() if k!='claim_text'}


def production_source_replay(semantic_failure=False):
    fixture=production_fixture();export=fixture['production_source_export'];sources=deepcopy(export['sources'])
    runtime,state,_,calls,_=setup();keys=sorted({s['message_key'] for s in sources})
    # The export has exact DB sources, not the original transient review graph.
    # This local mock supplies an uncertain graph, never a history certificate.
    state.update(allowed_keys=set(keys),analysis_completed=False,context_reviews={k:{'complete':False,
        'gaps':['uncertain_thread_links'],'message_keys':keys,'edges':[],'evidence_ids':[s['evidence_id'] for s in sources]} for k in keys})
    def model(t,o,current,purpose,system,prompt,**kw):
        current['model_calls']+=1;p=json.loads(prompt);calls.append((purpose,p,system,prompt,kw))
        if purpose=='answer_synthesis':return deepcopy(fixture['generation'])
        assert purpose=='semantic_claim_verification'
        verdicts=[];by_id={s['evidence_id']:s for s in sources}
        for index,row in enumerate(p['claims']):
            source=_authored_atom_sources(row,by_id)[0];status='insufficient' if semantic_failure and index==1 else 'supported'
            verdicts.append({'claim_id':row['claim_id'],'answer_relevance':['background','direct','qualification'][index],
                'answer_relevance_reason':'旧日期背景，确认期限直接回答，未交付反证限定',
                'atoms':[{'text':row['claim_text'],'status':status,'support_evidence_ids':[source['evidence_id']] if status=='supported' else [],
                    'counter_evidence_ids':[],'source_quotes':[{'evidence_id':source['evidence_id'],'quote':row['source_quote']}] if status=='supported' else [],
                    'missing_requirements':[] if status=='supported' else ['original_semantic_insufficient']}],
                'qualification_reviews':[{**q,'applicability':'applicable','rationale':'原限制保留，履行未知'} for q in p['qualification_checks'][row['claim_id']]]})
        return {'verdicts':verdicts}
    runtime._model_call=model
    success=synthesize(runtime,'t','o',state,{'objective':'蓝湾项目的最终交付是哪一天','mode':'research',
        'budget':{'max_context_tokens':12000}},sources,[],direct=True)
    state.update(findings=[],context_gaps=[]);rendered=runtime._answer({'mode':'research'},sources,{}, {'matched':8},0,state,[])
    return success,state,calls,rendered,fixture


@pytest.mark.parametrize('semantic_failure',[False,True])
def test_actual_production_raw_uses_actual_18_db_sources_and_unchanged_original_gates(semantic_failure):
    success,state,calls,rendered,fixture=production_source_replay(semantic_failure);export=fixture['production_source_export']
    assert export['read_only'] and export['exact_original_quote_offset_binding'] and not export['exact_transient_model_payload_replay']
    assert len(export['sources'])==18
    assert success is not semantic_failure and [purpose for purpose,_,_,_,_ in calls]==['answer_synthesis','semantic_claim_verification']
    assert state['answer_generation_raw']==fixture['generation']
    projection=state['answer_synthesis']['candidate_audit']['answer_candidate_projections'][0]
    assert projection['atom_index']==1 and projection['before']==fixture['generation']['claims'][1]
    assert projection['after']['claim_text']==projection['before']['source_quote'].replace('最终','',1)
    assert {k:v for k,v in projection['after'].items() if k!='claim_text'}=={k:v for k,v in projection['before'].items() if k!='claim_text'}
    for index in (0,2):assert calls[1][1]['claims'][index]['claim_text']==fixture['generation']['claims'][index]['claim_text']
    assert {s['evidence_id'] for s in calls[1][1]['source_records']}=={s['evidence_id'] for s in export['sources']}
    assert state['answer_synthesis']['unanswered_subquestions_raw']==fixture['generation']['unanswered_subquestions']
    if not semantic_failure:
        assert '10 月 20 日' in rendered['answer'] and '物流签收均不构成' in rendered['answer'] and '未知' in rendered['answer']


@pytest.mark.parametrize('text',[
    '蓝湾项目最终交付日期确认为2026年10月18日且已完成交付。',
    '蓝湾项目最终交付日期确认为2026年10月18日。项目已验收。',
    '蓝湾项目最终交付日期确认为2026年10月18日、2026年10月20日。',
    '蓝湾项目最终实际完成日期确认为2026年10月18日。',
    '蓝湾项目当前交付日期确认为2026年10月18日。',
    '蓝湾项目最终验收日期确认为2026年10月18日。',
    '蓝湾项目最终交付日期确认为2026年10月20日。',
])
def test_confirmation_variant_cannot_project_extra_facts_other_actions_or_changed_dates(text):
    fixture,_,by_id=actual_v9();row=deepcopy(fixture['generation']['claims'][1]);row['claim_text']=text
    assert _source_local_date_proposal(row,by_id,_atom_profile_errors(row,by_id)) is None
