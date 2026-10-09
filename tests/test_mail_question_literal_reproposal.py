"""Only one bounded source-fixed unaccepted alternative reaches original gates."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from services.mail.research.answer_synthesis import (
    _atom_profile_errors,_authored_atom_sources,_literal_content_proposal,_source_local_date_proposal,synthesize,
)
from test_mail_answer_synthesis import setup


def actual_v11():
    f=json.loads((Path(__file__).parent/'fixtures'/'mail_question_v11_literal_failures.json').read_text(encoding='utf-8'))
    p=f['generation_prompt'];d=p['source_documents'];b=p['source_blocks']
    sources=[{**d.get('_defaults',{}),**d[b[r['source_ref']]['document_ref']],
        **{k:v for k,v in b[r['source_ref']].items() if k!='document_ref'},**{k:v for k,v in r.items() if k!='source_ref'}} for r in p['evidence']]
    return f,sources,{s['evidence_id']:s for s in sources}


def test_exact_actual_v11_three_bad_contents_have_only_fixed_source_local_alternatives():
    f,_,by=actual_v11();before=deepcopy(f['generation'])
    for index in (0,1,3):
        row=f['generation']['claims'][index];errors=_atom_profile_errors(row,by)
        candidate=_source_local_date_proposal(row,by,errors) if index==0 else _literal_content_proposal(row,by,errors)[0]
        assert _atom_profile_errors(candidate,by)==[]
        assert candidate['source_quote']==row['source_quote']
        for key in row:
            if key not in ('claim_text','actor'):assert candidate[key]==row[key]
        assert candidate['actor']==(None if index==1 else row['actor'])
        assert candidate['claim_text']==row['source_quote'].replace('最终','',1) if index==0 else candidate['claim_text']==row['source_quote']
    assert f['generation']==before
    assert _literal_content_proposal(f['generation']['claims'][2],by,[]) is None


@pytest.mark.parametrize('change',['compound','extra_action','extra_number','two_dates','real_completed','wrong_date',
    'body_date_not_envelope','partial_quote','quote_compound','person_obligation','person_speech','role_obligation',
    'split_criterion','split_status','person_name_prefix','foreign','quoted','cc','ambiguous','already_judged','source_missing'])
def test_literal_alternative_never_drops_an_extra_fact_personal_obligation_or_source_gap(change):
    f,_,original=actual_v11();by=deepcopy(original);index=1 if change in ('person_obligation','person_speech','role_obligation','split_criterion','person_name_prefix') else 3
    row=deepcopy(f['generation']['claims'][index]);old=row['source_quote']
    if change=='compound':row['claim_text']=row['claim_text'].rstrip('。')+'且已完成交付。'
    elif change=='extra_action':row['claim_text']=row['claim_text'].rstrip('。')+'、未签收。'
    elif change=='extra_number':row['claim_text']=row['claim_text'].rstrip('。')+'100台。'
    elif change=='two_dates':row['claim_text']=row['claim_text'].rstrip('。')+'截至2026年10月20日。'
    elif change=='real_completed':row['claim_text']='蓝湾项目截至2026年10月21日已完成交付。'
    elif change=='wrong_date':row['claim_text']=row['claim_text'].replace('21日','20日')
    elif change=='body_date_not_envelope':row['claim_text']=row['claim_text'].replace('截至','于')
    elif change=='split_status':row['claim_text']=row['claim_text'].replace('未交付、未验收','未交付未验收')
    elif change=='partial_quote':row['source_quote']='未交付、未验收。'
    elif change in ('quote_compound','person_obligation','person_speech','role_obligation','split_criterion','person_name_prefix'):
        row['source_quote']={'quote_compound':old.rstrip('。')+'且已完成交付。',
            'person_obligation':old.replace('双方签署','双方应签署'),
            'person_speech':'我司承诺：'+old,'role_obligation':old.replace('双方签署','甲方需签署'),
            'split_criterion':old.replace('双方签署','双方、签署'),'person_name_prefix':'张三：'+old}[change]
        for s in by.values():s['quote_text']=s['quote_text'].replace(old,row['source_quote'])
    elif change=='foreign':row['evidence_ids']=[f['generation']['claims'][2]['evidence_ids'][0]]
    elif change in ('quoted','cc'):
        for eid in row['evidence_ids']:
            s=by[eid]
            if change=='quoted':s['body_fragments']=[{'fragment_kind':'quoted','char_start':0,'char_end':len(s['quote_text'])}]
            else:
                for p in s['participants']:p['role']='cc' if p['role']=='from' else p['role']
    elif change=='ambiguous':
        s=_authored_atom_sources(row,by)[0];other={**deepcopy(s),'evidence_id':'other','message_key':'other'}
        by['other']=other;row['evidence_ids'].append('other')
    elif change=='already_judged':row['semantic_verification']={'atoms':[{'status':'insufficient'}]}
    elif change=='source_missing':by={}
    before=deepcopy(row)
    assert _literal_content_proposal(row,by,_atom_profile_errors(row,by)) is None
    assert row==before


def v11_replay(semantic_failure=False):
    f,sources,_=actual_v11();runtime,state,_,calls,_=setup();keys=sorted({s['message_key'] for s in sources})
    state.update(allowed_keys=set(keys),analysis_completed=False,context_reviews={k:{'complete':False,
        'gaps':['uncertain_thread_links'],'message_keys':keys,'edges':[],'evidence_ids':[s['evidence_id'] for s in sources]} for k in keys})
    def model(t,o,current,purpose,system,prompt,**kw):
        current['model_calls']+=1;p=json.loads(prompt);calls.append((purpose,p,system,prompt,kw))
        if purpose=='answer_synthesis':return deepcopy(f['generation'])
        assert purpose=='semantic_claim_verification'
        verdicts=[]
        for index,row in enumerate(p['claims']):
            status='insufficient' if semantic_failure and index==0 else 'supported'
            bound=_authored_atom_sources(row,{s['evidence_id']:s for s in sources});source=bound[0]
            verdicts.append({'claim_id':row['claim_id'],'answer_relevance':['direct','qualification','qualification','direct'][index],
                'answer_relevance_reason':'确认期限、定义、自报与反证仍分开接受完整原门禁',
                'atoms':[{'text':row['claim_text'],'status':status,'support_evidence_ids':[source['evidence_id']] if status=='supported' else [],
                    'counter_evidence_ids':[],'source_quotes':[{'evidence_id':source['evidence_id'],'quote':row['source_quote']}] if status=='supported' else [],
                    'missing_requirements':[] if status=='supported' else ['original_semantic_insufficient']}],
                'qualification_reviews':[{**q,'applicability':'applicable','rationale':'原限制保留，履行仍未确认'} for q in p['qualification_checks'][row['claim_id']]]})
        return {'verdicts':verdicts}
    runtime._model_call=model
    success=synthesize(runtime,'t','o',state,{'objective':f['generation_prompt']['objective'],'mode':'research',
        'budget':{'max_context_tokens':12000}},sources,[],direct=True)
    state.update(findings=[],context_gaps=[]);rendered=runtime._answer({'mode':'research'},sources,{}, {'matched':8},0,state,[])
    return success,state,calls,rendered,f


@pytest.mark.parametrize('semantic_failure',[False,True])
def test_actual_v11_fixed_alternatives_avoid_free_text_repair_but_never_overrule_original_semantic_failure(semantic_failure):
    success,state,calls,rendered,f=v11_replay(semantic_failure)
    assert success is not semantic_failure
    assert [p for p,_,_,_,_ in calls]==['answer_synthesis','semantic_claim_verification']
    assert state['answer_generation_raw']==f['generation']
    proposals=state['answer_synthesis']['candidate_audit']['answer_candidate_projections']
    assert [r['atom_index'] for r in proposals]==[0,1,3]
    for r in proposals:
        assert r['status']=='unaccepted_proposal_for_original_judge' and r['before']==f['generation']['claims'][r['atom_index']]
    assert calls[1][1]['claims'][2]['claim_text']==f['generation']['claims'][2]['claim_text']
    assert '原计划日期 10 月 15 日、我司申请日期 10 月 20 日，均不再采用。' in rendered['answer'] if not semantic_failure else True
    assert state['answer_synthesis']['unanswered_subquestions_raw']==f['generation']['unanswered_subquestions']
