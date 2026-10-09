"""Literal unaccepted proposals and strict repair schemas retain original gates."""
from copy import deepcopy
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema
import pytest

from services.mail.research.answer_synthesis import (
    _atom_profile_errors, _repair_output_schema, _source_local_date_proposal, synthesize,
)
from services.mail.research.runtime import MailResearchRuntime
from test_mail_answer_synthesis import setup


def actual_v9():
    data=json.loads((Path(__file__).parent/'fixtures'/'mail_question_v9_repair_failure.json').read_text(encoding='utf-8'))
    p=data['generation_prompt'];d=p['source_documents'];b=p['source_blocks']
    sources=[{**d.get('_defaults',{}),**d[b[r['source_ref']]['document_ref']],
        **{k:v for k,v in b[r['source_ref']].items() if k!='document_ref'},
        **{k:v for k,v in r.items() if k!='source_ref'}} for r in p['evidence']]
    return data,sources,{s['evidence_id']:s for s in sources}


def test_exact_actual_v9_extra_keys_and_final_word_fail_but_literal_proposal_changes_only_content():
    data,sources,by_id=actual_v9();row=data['generation']['claims'][1];before=deepcopy(row)
    assert set(data['repair']['repairs'][0])=={'atom_index','claim_text','evidence_ids','source_quote','actor','object_ref','conditions','time_basis'}
    assert _atom_profile_errors(row,by_id)==['temporal_scope_or_full_history_unproven']
    proposed=_source_local_date_proposal(row,by_id,_atom_profile_errors(row,by_id))
    assert row==before and proposed['claim_text']==row['source_quote'].replace('最终','',1)
    assert _atom_profile_errors(proposed,by_id)==[]
    assert {k:v for k,v in proposed.items() if k!='claim_text'}=={k:v for k,v in before.items() if k!='claim_text'}


@pytest.mark.parametrize('change',[
    'complete','accepted','conditional','negative','compound','shipment','receipt','different_date',
    'different_number','current','actual','extra_quote_clause','conditional_quote','quoted','cc','attachment','ambiguous_source',
    'compound_anchor','predicate_anchor',
])
def test_projection_never_drops_an_extra_predicate_or_repairs_missing_content_or_attribution(change):
    data,_,original=actual_v9();by_id=deepcopy(original);row=deepcopy(data['generation']['claims'][1])
    if change in ('complete','conditional','negative','compound','shipment','receipt','actual'):
        suffix={'complete':'且已完成交付','conditional':'且无条件生效','negative':'且未交付',
            'compound':'且由我司负责','shipment':'且已发运','receipt':'且已签收','actual':'且实际完成'}[change]
        row['claim_text']=row['claim_text'].rstrip('。')+suffix+'。'
    elif change=='accepted':row['semantic_status']='supported'
    elif change=='different_date':row['claim_text']=row['claim_text'].replace('18日','20日')
    elif change=='different_number':row['claim_text']=row['claim_text'].rstrip('。')+'100台。'
    elif change=='current':row['claim_text']=row['claim_text'].replace('最终','当前')
    elif change in ('extra_quote_clause','conditional_quote'):
        addition='且已完成交付。' if change=='extra_quote_clause' else '只有签署报告后生效。'
        old=row['source_quote'];row['source_quote']=old+addition
        for source in by_id.values():source['quote_text']=source['quote_text'].replace(old,row['source_quote'])
    elif change=='quoted':
        for eid in row['evidence_ids']:
            source=by_id[eid];source['attribution']='quoted'
            source['body_fragments']=[{'fragment_kind':'quoted','char_start':0,'char_end':len(source['quote_text'])}]
    elif change=='cc':
        for eid in row['evidence_ids']:
            for p in by_id[eid]['participants']:
                if p.get('normalized_address')==row['actor']:p['role']='cc'
    elif change=='attachment':
        for eid in row['evidence_ids']:by_id[eid]['source_kind']='attachment'
    elif change=='ambiguous_source':
        own=next(s for s in by_id.values() if s['evidence_id'] in row['evidence_ids'] and row['source_quote'] in s['quote_text'])
        duplicate={**deepcopy(own),'evidence_id':'other','message_key':'other-mail'}
        by_id['other']=duplicate;row['evidence_ids'].append('other')
    elif change in ('compound_anchor','predicate_anchor'):
        old=row['object_ref'];anchor=old+'、项目乙' if change=='compound_anchor' else old+'已交付'
        row['object_ref']=anchor;row['claim_text']=row['claim_text'].replace(old,anchor)
        for source in by_id.values():source['quote_text']=source['quote_text'].replace(old,anchor)
    assert _source_local_date_proposal(row,by_id,_atom_profile_errors(row,by_id)) is None


def v9_replay(semantic_failure=False):
    data,sources,_=actual_v9();runtime,state,_,calls,_=setup();keys=sorted({s['message_key'] for s in sources})
    state.update(allowed_keys=set(keys),analysis_completed=False,context_reviews={key:{'complete':False,
        'gaps':['uncertain_thread_links'],'message_keys':keys,'edges':[],'evidence_ids':[s['evidence_id'] for s in sources]} for key in keys})
    def model(t,o,current,purpose,system,prompt,**kwargs):
        current['model_calls']+=1;p=json.loads(prompt);calls.append((purpose,p,kwargs))
        if purpose=='answer_synthesis':return deepcopy(data['generation'])
        assert purpose=='semantic_claim_verification'
        verdicts=[]
        for index,row in enumerate(p['claims']):
            status='insufficient' if semantic_failure and index==1 else 'supported'
            verdicts.append({'claim_id':row['claim_id'],'answer_relevance':['background','direct','qualification'][index],
                'answer_relevance_reason':'旧期限背景，确认日期直接回答，后续反证限定',
                'atoms':[{'text':row['claim_text'],'status':status,'support_evidence_ids':row['evidence_ids'][:1] if status=='supported' else [],
                    'counter_evidence_ids':[],'source_quotes':[{'evidence_id':row['evidence_ids'][0],'quote':row['source_quote']}] if status=='supported' else [],
                    'missing_requirements':[] if status=='supported' else ['original_semantic_insufficient']}],
                'qualification_reviews':[{**check,'applicability':'applicable','rationale':'仍仅确认原限制'}
                    for check in p['qualification_checks'][row['claim_id']]]})
        return {'verdicts':verdicts}
    runtime._model_call=model
    success=synthesize(runtime,'t','o',state,{'objective':'蓝湾项目最终交付是哪一天','mode':'research',
        'budget':{'max_context_tokens':12000}},sources,[],direct=True)
    return success,state,calls,data


@pytest.mark.parametrize('semantic_failure',[False,True])
def test_actual_v9_literal_proposal_avoids_repair_call_but_cannot_overrule_original_judge(semantic_failure):
    success,state,calls,data=v9_replay(semantic_failure)
    assert success is not semantic_failure
    assert [purpose for purpose,_,_ in calls]==['answer_synthesis','semantic_claim_verification']
    assert state['answer_generation_raw']==data['generation']
    projected=state['answer_candidate_projections'][0]
    assert projected['atom_index']==1 and projected['status']=='unaccepted_proposal_for_original_judge'
    assert projected['before']==data['generation']['claims'][1]
    judged=calls[1][1]['claims']
    for index in (0,2):assert judged[index]['claim_text']==data['generation']['claims'][index]['claim_text']
    for index,row in enumerate(judged):
        original=data['generation']['claims'][index]
        assert row['conditions'][:len(original['conditions'])]==original['conditions']
        assert row['time_basis']==original['time_basis'] and row['evidence_ids']==original['evidence_ids']
    assert state['answer_synthesis']['unanswered_subquestions_raw']==data['generation']['unanswered_subquestions']


def test_dynamic_schema_enforces_six_fields_invalid_indexes_ids_authors_and_retains_local_binding():
    data,_,by_id=actual_v9();schema=_repair_output_schema([1],by_id);jsonschema.Draft7Validator.check_schema(schema)
    row={k:deepcopy(v) for k,v in data['repair']['repairs'][0].items() if k not in ('conditions','time_basis')}
    row['claim_text']=data['generation']['claims'][1]['source_quote'].replace('最终','',1)
    valid={'repairs':[row]};jsonschema.validate(valid,schema)
    for changes in ({'conditions':[]},{'atom_index':0},{'evidence_ids':['foreign']},{'actor':'foreign@example.com'}):
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate({'repairs':[{**row,**changes}]},schema)
    duplicate={'repairs':[row,row]}
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(duplicate,schema)
    # Known author and ID enums alone do not prove same-source support.
    other=next(author for author in schema['properties']['repairs']['items']['properties']['actor']['enum'] if author!=row['actor'])
    cross={**row,'actor':other};jsonschema.validate({'repairs':[cross]},schema)
    assert 'source_binding_or_shape_unproven' in _atom_profile_errors({**data['generation']['claims'][1],**cross},by_id)


def test_runtime_transmits_explicit_schema_without_changing_default_calls_or_retry_limit():
    captured=[];repo=SimpleNamespace(event=lambda *a:None,record_research_step=lambda *a,**kw:None)
    model=SimpleNamespace(chat_json=lambda *a,**kw:(captured.append(kw) or {'json':{'repairs':[]}}))
    runtime=MailResearchRuntime(repo,model=model);schema=_repair_output_schema([1],{'e':{'participants':[{'role':'from','address':'a@example.com'}]}})
    state={'phase2':True,'model_calls':0,'max_model_calls':3,'max_seconds':600,'started':time.monotonic()}
    with patch.object(runtime,'_continue_turn',return_value=True):
        runtime._model_call('t','o',state,'answer_synthesis_repair','repair',json.dumps({'format_repairs':[{'atom_index':1}]}),
            required_fields=('repairs',),output_schema=schema)
        runtime._model_call('t','o',state,'other','other','{}',required_fields=('repairs',))
    assert captured[0]['output_schema']==schema and captured[0]['structured_retry_limit']==0
    assert 'output_schema' not in captured[1]
