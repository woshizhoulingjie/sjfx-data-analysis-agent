"""Cross-source answers preserve dated agreement, later rebuttal and unknowns."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from services.mail.research.answer_synthesis import synthesize
from services.mail.research.runtime import MailResearchRuntime


def setup():
    texts=['项目甲原拟10月15日交付。','我确认项目甲交付期限为10月18日。',
           '我请求项目甲延期至10月20日。','项目甲以双方签署验收报告为交付条件。',
           '10月21日项目甲尚未交付、未验收。']
    sources=[{'evidence_id':'e'+str(i),'message_key':'m'+str(i),'quote_text':text,
        'sent_at_utc':'2026-10-'+str(10+i),'source_kind':'body','mime_path':'1',
        'char_start':0,'char_end':len(text),'attribution':'authored',
        'body_fragments':[{'fragment_kind':'authored','char_start':0,'char_end':len(text)}],
        'participants':[{'role':'from','normalized_address':'alice@example.com'}],
        'source_aliases':[{'evidence_id':'old'+str(i)}]} for i,text in enumerate(texts)]
    state={'phase2':True,'analysis_completed':True,'analysis_truncated':False,'model_calls':0,
        'max_model_calls':20,'deadline_reached':False,'tool_limit_reached':False,'retrieval_gaps':[],
        'context_reviews':{s['message_key']:{'complete':True,'gaps':[],'evidence_ids':[s['evidence_id']]} for s in sources},
        'evidence_packing':{'synthesis_complete':True,'analysis_omitted':[]},'allowed_keys':{s['message_key'] for s in sources}}
    events=[];calls=[]
    repo=SimpleNamespace(event=lambda *a:events.append(a),record_research_step=lambda *a,**kw:None)
    runtime=MailResearchRuntime(repo)
    def call(t,o,current,purpose,system,prompt,**kwargs):
        current['model_calls']+=1;payload=json.loads(prompt);calls.append((purpose,payload))
        if purpose=='answer_synthesis':
            return {'claims':[{'claim_text':sources[i]['quote_text'],'source_quote':sources[i]['quote_text'],
                'evidence_ids':[sources[i]['evidence_id']],'object_ref':'项目甲','actor':'alice@example.com'} for i in (1,4,3)],
                'unanswered_subquestions':['实际完成日期']}
        if purpose=='answer_synthesis_repair':return {'repairs':[]}
        verdicts=[]
        for c in payload['claims']:
            verdicts.append({'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'supported',
                'support_evidence_ids':c['evidence_ids'],'counter_evidence_ids':[],
                'source_quotes':[{'evidence_id':c['evidence_ids'][0],'quote':c['source_quote']}],
                'missing_requirements':[]}], 'answer_relevance':'direct' if c['evidence_ids']==['e1'] else 'qualification',
                'answer_relevance_reason':'约定期限直接回答问题，其余条目是条件和反证。',
                'qualification_reviews':[{**q,'applicability':'applicable','rationale':'直接限定此事项'} for q in payload['qualification_checks'][c['claim_id']]]})
        return {'verdicts':verdicts}
    runtime._model_call=call
    return runtime,state,sources,calls,events


def test_complete_cross_source_answer_directly_shows_agreement_counter_and_acceptance():
    runtime,state,sources,calls,events=setup();before=deepcopy(sources)
    synthesize(runtime,'t','o',state,{'objective':'项目甲最终交付是哪一天','scope':{},'budget':{'max_context_tokens':12000}},sources,[])
    checked=state['answer_synthesis']['claims']
    assert len(checked)==3 and all(row['semantic_status']=='supported' for row in checked)
    assert sources==before and '_full_scope_history_verified' not in state['context_reviews']
    state.update(findings=[],context_gaps=[])
    final=runtime._answer({'mode':'research'},sources,{}, {'matched':5},0,state,[])
    assert '10月18日' in final['answer'] and '10月21日' in final['answer']
    assert '未交付、未验收' in final['answer'] and '双方签署验收报告' in final['answer']
    assert '未知' in final['answer'] and '实际完成日期' in final['answer']
    # Every judge partition sees the later source, even when auditing the
    # earlier agreement; each original source appears exactly once.
    for purpose,payload in calls:
        if purpose=='semantic_claim_verification':
            ids=[s['evidence_id'] for s in payload['source_records']]
            assert set(ids)=={s['evidence_id'] for s in sources} and len(ids)==len(set(ids))
    assert not any('relations' in event[1] for event in events)


@pytest.mark.parametrize('failure',['late_omitted','missing_attachment','budget','oversize'])
def test_incomplete_history_never_calls_synthesis_or_silently_discards_sources(failure):
    runtime,state,sources,calls,_=setup();before=deepcopy(sources)
    if failure=='late_omitted':state['evidence_packing']['analysis_omitted']=['e4']
    elif failure=='missing_attachment':state['context_reviews']['m4'].update(complete=False,gaps=['attachment_text_unavailable'])
    elif failure=='budget':state['max_model_calls']=1
    else:runtime.context_limit=1000
    synthesize(runtime,'t','o',state,{'objective':'交付日期','budget':{'max_context_tokens':12000}},sources,[])
    assert not calls and state['answer_synthesis']['status']=='unavailable' and sources==before


@pytest.mark.parametrize('bad',['项目甲实际完成交付于10月18日。','项目甲最终交付日期为10月18日。'])
def test_agreement_never_becomes_actual_completion_or_package_final(bad):
    runtime,state,sources,calls,_=setup();base=runtime._model_call
    def model(t,o,current,purpose,system,prompt,**kwargs):
        if purpose=='answer_synthesis':
            return {'claims':[{'claim_text':bad,'source_quote':sources[1]['quote_text'],
                'evidence_ids':['e1'],'object_ref':'项目甲','actor':'alice@example.com'}]}
        if '最终' in bad:return base(t,o,current,purpose,system,prompt,**kwargs)
        payload=json.loads(prompt)
        return {'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'insufficient',
            'support_evidence_ids':[],'counter_evidence_ids':[],'source_quotes':[],
            'missing_requirements':['约定日期不是实际完成；后续邮件未交付未验收']}]} for c in payload['claims']]}
    runtime._model_call=model
    synthesize(runtime,'t','o',state,{'objective':'交付日期','budget':{'max_context_tokens':12000}},sources,[])
    if '最终' in bad:
        assert state['answer_synthesis']['status']=='unavailable'
        assert 'temporal_scope_or_full_history_unproven' in state['answer_content_precheck']['failures'][0]['errors']
        assert not any(purpose=='semantic_claim_verification' for purpose,payload in calls)
    else:assert all(row['semantic_status']!='supported' for row in state['answer_synthesis']['claims'])
    assert '_full_scope_history_verified' not in state['context_reviews']


def test_unverified_unknown_label_cannot_render_a_completion_assertion():
    runtime,state,sources,calls,_=setup();base=runtime._model_call
    injected='项目甲已于10月18日完成交付'
    def model(*args,**kwargs):
        result=base(*args,**kwargs)
        if args[3]=='answer_synthesis':result['unanswered_subquestions']=[injected]
        return result
    runtime._model_call=model
    synthesize(runtime,'t','o',state,{'objective':'交付日期','budget':{'max_context_tokens':12000}},sources,[])
    state.update(findings=[],context_gaps=[])
    final=runtime._answer({'mode':'research'},sources,{}, {'matched':5},0,state,[])
    assert injected not in final['answer'] and '所问结论' in final['answer']
    assert state['answer_synthesis']['unanswered_subquestions_raw']==[injected]


@pytest.mark.parametrize('field',['source_quote','object_ref','actor'])
def test_missing_required_display_citation_or_actor_fails_closed(field):
    runtime,state,sources,calls,_=setup();base=runtime._model_call
    def model(*args,**kwargs):
        result=base(*args,**kwargs)
        if args[3]=='answer_synthesis':result['claims'][0].pop(field)
        return result
    runtime._model_call=model
    synthesize(runtime,'t','o',state,{'objective':'交付日期','budget':{'max_context_tokens':12000}},sources,[])
    assert state['answer_synthesis']['status']=='unavailable'
    assert not any(purpose=='semantic_claim_verification' for purpose,payload in calls)
