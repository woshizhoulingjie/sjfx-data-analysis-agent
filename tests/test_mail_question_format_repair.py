"""One pre-judge structural proposal cannot erase bindings or semantic failures."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from services.mail.research.answer_synthesis import synthesize
from services.mail.research.evidence_packing import answer_repair_output_tokens
from test_mail_answer_synthesis import setup


def candidate_setup():
    runtime,state,sources,calls,events=setup();base=runtime._model_call
    good=base('t','o',state,'answer_synthesis','',json.dumps({}))
    state['model_calls']=0;calls.clear()
    good['claims'][1].update(conditions=['未验收'],time_basis='2026-10-14')
    malformed=deepcopy(good);malformed['claims'][1].update(evidence_ids=['e3'],actor='wrong@example.com',object_ref='missue_hash')
    def propose(payload):
        return {'repairs':[{'atom_index':1,**{k:good['claims'][1][k] for k in
            ('claim_text','evidence_ids','source_quote','actor','object_ref')}}]}
    def install(proposal=propose, judge_override=None):
        def model(t,o,current,purpose,system,prompt,**kwargs):
            if purpose in ('answer_synthesis','answer_synthesis_repair'):
                current['model_calls']+=1;payload=json.loads(prompt);calls.append((purpose,payload))
                return deepcopy(malformed) if purpose=='answer_synthesis' else proposal(payload)
            result=base(t,o,current,purpose,system,prompt,**kwargs)
            return judge_override(result) if judge_override else result
        runtime._model_call=model
    install()
    return runtime,state,sources,calls,good,malformed,install


def run(runtime,state,sources):
    return synthesize(runtime,'t','o',state,{'objective':'项目甲交付是哪一天','mode':'research',
        'budget':{'max_context_tokens':12000}},sources,[],direct=True)


def test_only_malformed_unjudged_atom_can_rebind_same_source_once_without_changing_valid_or_unknown():
    runtime,state,sources,calls,good,malformed,_=candidate_setup();before=deepcopy(malformed)
    assert run(runtime,state,sources)
    assert malformed==before and state['answer_generation_raw']==before
    assert state['answer_format_repair']['status']=='applied'
    assert [p for p,_ in calls].count('answer_synthesis_repair')==1
    repair=next(payload for purpose,payload in calls if purpose=='answer_synthesis_repair')
    assert [row['atom_index'] for row in repair['format_repairs']]==[1]
    assert {row['evidence_id'] for row in repair['evidence']}=={s['evidence_id'] for s in sources}
    for ref,label in repair['citation_index'].items():
        assert set(label['ids'])=={row['evidence_id'] for row in repair['evidence'] if row['source_ref']==ref}
        assert label['from']==['alice@example.com']
    judged=next(payload for purpose,payload in calls if purpose=='semantic_claim_verification')['claims']
    for index in (0,2):
        for key,value in good['claims'][index].items():assert judged[index][key]==value
    assert judged[1]['evidence_ids']==['e4'] and judged[1]['actor']=='alice@example.com'
    assert judged[1]['conditions'][:len(good['claims'][1]['conditions'])]==good['claims'][1]['conditions']
    assert judged[1]['conditions'][-1]==sources[4]['quote_text'] and judged[1]['time_basis']==good['claims'][1]['time_basis']
    assert state['answer_synthesis']['unanswered_subquestions_raw']==good['unanswered_subquestions']
    assert answer_repair_output_tokens(repair)>1600


@pytest.mark.parametrize('failure',['valid_atom','duplicate','unknown_eid','wrong_quote','wrong_actor',
    'machine_anchor','conditions_change','unknown_change','missing_atom','boolean_index','oversized_quote'])
def test_invalid_reproposal_is_rejected_without_second_repair_or_judge(failure):
    runtime,state,sources,calls,good,malformed,install=candidate_setup()
    def proposal(payload):
        row={'atom_index':1,**{k:good['claims'][1][k] for k in
            ('claim_text','evidence_ids','source_quote','actor','object_ref')}}
        if failure=='valid_atom':row['atom_index']=0
        elif failure=='duplicate':return {'repairs':[row,deepcopy(row)]}
        elif failure=='unknown_eid':row['evidence_ids']=['not-supplied']
        elif failure=='wrong_quote':row['source_quote']='不在原文中的引用'
        elif failure=='wrong_actor':row['actor']='cc@example.com'
        elif failure=='machine_anchor':row['object_ref']='missue_hash'
        elif failure=='conditions_change':row['conditions']=[]
        elif failure=='unknown_change':row['unanswered_subquestions']=[]
        elif failure=='missing_atom':return {'repairs':[]}
        elif failure=='boolean_index':row['atom_index']=True
        else:row['source_quote']='引'*401
        return {'repairs':[row]}
    install(proposal)
    assert not run(runtime,state,sources)
    assert state['answer_format_repair']['status']=='rejected'
    assert [p for p,_ in calls].count('answer_synthesis_repair')==1
    assert not any(p=='semantic_claim_verification' for p,_ in calls)
    assert state['answer_synthesis']['unanswered_subquestions_raw']==malformed['unanswered_subquestions']


@pytest.mark.parametrize('failure',['calls','context'])
def test_repair_capacity_limits_keep_original_sources_unknown_and_fallback_reserve(failure):
    runtime,state,sources,calls,_,malformed,_=candidate_setup()
    if failure=='calls':state['max_model_calls']=4
    else:
        # The complete generation fits; its complete repair with original bad
        # candidate does not. No text or source is cut to make it fit.
        initial=runtime._model_call
        def model(*args,**kwargs):
            result=initial(*args,**kwargs)
            if args[3]=='answer_synthesis':runtime.context_limit=len(args[5])+1
            return result
        runtime._model_call=model
    assert not run(runtime,state,sources)
    assert not any(p=='answer_synthesis_repair' for p,_ in calls)
    assert state['model_calls']==1 and state['max_model_calls']-state['model_calls']>=3
    assert state['answer_synthesis']['unanswered_subquestions_raw']==malformed['unanswered_subquestions']


@pytest.mark.parametrize('failure',['final_word','semantic_insufficient'])
def test_repaired_candidate_still_faces_original_temporal_and_semantic_judge(failure):
    runtime,state,sources,calls,good,_,install=candidate_setup()
    def proposal(payload):
        row={'atom_index':1,**{k:good['claims'][1][k] for k in
            ('claim_text','evidence_ids','source_quote','actor','object_ref')}}
        if failure=='final_word':row['claim_text']='项目甲最终交付日期为10月21日。'
        return {'repairs':[row]}
    def insufficient(result):
        for verdict in result['verdicts']:
            for atom in verdict['atoms']:
                atom.update(status='insufficient',support_evidence_ids=[],source_quotes=[],missing_requirements=['仍缺实际完成依据'])
        return result
    install(proposal,insufficient if failure=='semantic_insufficient' else None)
    assert not run(runtime,state,sources)
    if failure=='final_word':
        assert state['answer_format_repair']['status']=='rejected'
        assert not any(p=='semantic_claim_verification' for p,_ in calls)
        assert 'temporal_scope_or_full_history_unproven' in state['answer_content_precheck']['proposal_failures'][0]['errors']
    else:
        assert state['answer_format_repair']['status']=='applied'
        assert any(p=='semantic_claim_verification' for p,_ in calls)
        assert not any(row['semantic_status']=='supported' for row in state['answer_synthesis']['claims'])


def test_valid_candidates_with_insufficient_semantics_are_not_regenerated():
    runtime,state,sources,calls,good,malformed,install=candidate_setup()
    malformed.clear();malformed.update(deepcopy(good))
    def insufficient(result):
        for verdict in result['verdicts']:
            for atom in verdict['atoms']:atom.update(status='insufficient',support_evidence_ids=[],source_quotes=[],missing_requirements=['缺证据'])
        return result
    install(judge_override=insufficient)
    assert not run(runtime,state,sources)
    assert not any(p=='answer_synthesis_repair' for p,_ in calls)
    assert 'answer_format_repair' not in state


def test_string_conditions_reach_judge_losslessly_while_raw_and_unknown_stay_original():
    runtime,state,sources,calls,good,malformed,install=candidate_setup()
    malformed['claims'][1]['conditions']='未验收；双方须签署验收报告，不能以发运代替。'
    before=deepcopy(malformed);install()
    run(runtime,state,sources)
    assert state['answer_generation_raw']==before and malformed==before
    judged=next(payload for purpose,payload in calls if purpose=='semantic_claim_verification')['claims']
    assert judged[1]['conditions'][0]==before['claims'][1]['conditions']
    assert judged[1]['conditions'][-1]==sources[4]['quote_text']
    repair=next(payload for purpose,payload in calls if purpose=='answer_synthesis_repair')
    assert repair['format_repairs'][0]['original_atom']['conditions']==[before['claims'][1]['conditions']]
    assert state['answer_synthesis']['unanswered_subquestions_raw']==before['unanswered_subquestions']


def test_actual_v4_malformed_candidate_fixture_can_rebind_swapped_ids_before_same_judge():
    fixture=json.loads((Path(__file__).parent/'fixtures'/'mail_question_v4_format_failure.json').read_text(encoding='utf-8'))
    runtime,state,_,calls,_=setup();sources=fixture['sources'];raw=fixture['response']
    state.update(allowed_keys={s['message_key'] for s in sources},analysis_completed=False,
        context_reviews={s['message_key']:{'complete':True,'gaps':[],
            'evidence_ids':[r['evidence_id'] for r in sources if r['message_key']==s['message_key']]} for s in sources})
    def model(t,o,current,purpose,system,prompt,**kwargs):
        current['model_calls']+=1;payload=json.loads(prompt);calls.append((purpose,payload))
        if purpose=='answer_synthesis':return deepcopy(raw)
        if purpose=='answer_synthesis_repair':
            assert [r['atom_index'] for r in payload['format_repairs']]==[0,1,2]
            rows=[]
            for index,claim in enumerate(raw['claims']):
                own=next(s for s in sources if claim['source_quote'] in s['quote_text'] and any(
                    p.get('role')=='from' and p.get('normalized_address')==claim['actor'] for p in s.get('participants',[])))
                text=['蓝湾项目约定交付日期为2026年10月18日。','承建方声明已完成交付。','蓝湾项目未交付、未验收。'][index]
                rows.append({'atom_index':index,'claim_text':text,'evidence_ids':[own['evidence_id']],
                    'source_quote':claim['source_quote'],'actor':claim['actor'],'object_ref':'蓝湾项目'})
            return {'repairs':rows}
        # An insufficient semantic judgment is retained after structural repair.
        return {'verdicts':[{'claim_id':row['claim_id'],'answer_relevance':'direct','answer_relevance_reason':'待审来源表述',
            'atoms':[{'text':row['claim_text'],'status':'insufficient','support_evidence_ids':[],
                'counter_evidence_ids':[],'source_quotes':[],'missing_requirements':['未独立判定语义']}]} for row in payload['claims']]}
    runtime._model_call=model
    assert not synthesize(runtime,'t','o',state,{'objective':'蓝湾项目最终交付是哪一天','mode':'research',
        'budget':{'max_context_tokens':12000}},sources,[],direct=True)
    assert state['answer_format_repair']['status']=='applied' and state['answer_generation_raw']==raw
    judged=[row for purpose,payload in calls if purpose=='semantic_claim_verification' for row in payload['claims']]
    assert judged and not any(row['semantic_status']=='supported' for row in state['answer_synthesis']['claims'])
    for row in judged:
        own=[s for s in sources if s['evidence_id'] in row['evidence_ids']]
        assert any(row['source_quote'] in s['quote_text'] and any(p.get('role')=='from' and
            p.get('normalized_address')==row['actor'] for p in s.get('participants',[])) for s in own)
        original=next(c for c in raw['claims'] if c['source_quote']==row['source_quote'])
        assert row['conditions'][0]==original['conditions']
    assert state['answer_synthesis']['unanswered_subquestions_raw']==raw['unanswered_subquestions']
