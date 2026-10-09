"""Uncertain relationships preserve local evidence answers and history limits."""
from copy import deepcopy

import pytest

from services.mail.research.answer_synthesis import synthesize
from test_mail_answer_synthesis import setup


def warning_setup():
    runtime,state,sources,calls,events=setup()
    all_keys=[s['message_key'] for s in sources]
    for key,review in state['context_reviews'].items():
        review.update(complete=False,gaps=['uncertain_thread_links'],message_keys=list(all_keys),
            edges=[{'source_message_key':key,'target_message_key':'m4','status':'context'}],
            attachment_version_candidates=[])
    state['analysis_completed']=False
    return runtime,state,sources,calls,events


def test_uncertain_relation_all_known_originals_read_can_answer_historical_agreement():
    runtime,state,sources,calls,_=warning_setup();before=deepcopy(state['context_reviews'])
    assert synthesize(runtime,'t','o',state,{'objective':'项目甲最终交付是哪一天','mode':'research','budget':{'max_context_tokens':12000}},sources,[],direct=True)
    assert state['context_reviews']==before and not any(r['complete'] for r in before.values())
    assert '_full_scope_history_verified' not in state['context_reviews'] and state['analysis_completed'] is False
    synthesis=state['answer_synthesis']
    assert synthesis['source_coverage']['known_source_complete']
    assert not synthesis['source_coverage']['graph_history_complete']
    for purpose,payload in calls:
        assert payload['relationship_warnings']==synthesis['relationship_warnings']
    state.update(findings=[],context_gaps=[])
    coverage={'research':{'answer_synthesis':{'status':'complete','graph_history_complete':False}}}
    final=runtime._answer({'mode':'research'},sources,coverage,{'matched':5},0,state,[])
    assert '10月18日' in final['answer'] and '10月21日' in final['answer'] and '未交付、未验收' in final['answer']
    assert '未知' in final['answer'] and '线程关系未获确认' in ' '.join(final['limitations'])
    assert runtime._sufficiency(sources,coverage,{'unknown':0})=='partial'


@pytest.mark.parametrize('bad_gap',['missing_reply_target','depth_limit','edge_limit','context_text_limit',
    'messages_not_read','attachment_text_unavailable','body_text_unavailable','message_parse_failed',
    'related_messages_outside_scope','new_future_gap'])
def test_any_real_or_unknown_gap_still_prevents_direct_even_with_relation_warning(bad_gap):
    runtime,state,sources,calls,_=warning_setup()
    state['context_reviews']['m4']['gaps'].append(bad_gap)
    assert not synthesize(runtime,'t','o',state,{'objective':'项目甲交付是哪一天','mode':'research'},sources,[],direct=True)
    assert not calls and state['answer_synthesis']['status']=='unavailable'


@pytest.mark.parametrize('failure',['false_no_gap','missing_read_inventory','known_endpoint_not_read',
    'known_endpoint_no_source','missing_eid','missing_attachment_version'])
def test_relation_warning_never_hides_known_unread_sources(failure):
    runtime,state,sources,calls,_=warning_setup();review=state['context_reviews']['m4']
    if failure=='false_no_gap':review['gaps']=[]
    elif failure=='missing_read_inventory':review.pop('message_keys')
    elif failure=='known_endpoint_not_read':review['message_keys']=['m4'];review['edges'][0]['target_message_key']='m1'
    elif failure=='known_endpoint_no_source':review['message_keys'].append('unread');review['edges'][0]['target_message_key']='unread'
    elif failure=='missing_eid':review['evidence_ids'].append('not-supplied')
    else:review['attachment_version_candidates']=[{'message_key':'unread'}]
    before=deepcopy(state['context_reviews'])
    assert not synthesize(runtime,'t','o',state,{'objective':'项目甲交付是哪一天','mode':'research'},sources,[],direct=True)
    assert not calls and state['context_reviews']==before


def test_null_unknown_endpoint_stays_unknown_and_cannot_unlock_final_word():
    runtime,state,sources,calls,_=warning_setup();base=runtime._model_call
    for review in state['context_reviews'].values():review['edges'][0]['target_message_key']=None
    def model(*args,**kwargs):
        result=base(*args,**kwargs)
        if args[3]=='answer_synthesis':result['claims'][0]['claim_text']='项目甲最终交付期限为10月18日。'
        return result
    runtime._model_call=model
    assert not synthesize(runtime,'t','o',state,{'objective':'项目甲最终交付是哪一天','mode':'research'},sources,[],direct=True)
    assert state['answer_synthesis']['status']=='unavailable'
    assert 'temporal_scope_or_full_history_unproven' in state['answer_content_precheck']['failures'][0]['errors']
    assert not any(purpose=='semantic_claim_verification' for purpose,payload in calls)
    assert not any(r['complete'] for r in state['context_reviews'].values())


def test_small_budget_preserves_fallback_analysis_and_judge_reserve():
    runtime,state,sources,calls,_=warning_setup();state['max_model_calls']=3
    assert not synthesize(runtime,'t','o',state,{'objective':'项目甲交付是哪一天','mode':'research'},sources,[],direct=True)
    assert not calls and state['model_calls']==0


@pytest.mark.parametrize('malformed',['string_review','dict_gaps','nonbool_complete','bad_eid','bad_read_key','bad_endpoint','null_without_warning'])
def test_malformed_read_audit_cannot_claim_complete_originals(malformed):
    runtime,state,sources,calls,_=warning_setup();review=state['context_reviews']['m4']
    if malformed=='string_review':state['context_reviews']['m4']='looks reviewed'
    elif malformed=='dict_gaps':review['gaps']={}
    elif malformed=='nonbool_complete':review['complete']='true'
    elif malformed=='bad_eid':review['evidence_ids']=[{}]
    elif malformed=='bad_read_key':review['message_keys']=[{}]
    elif malformed=='bad_endpoint':review['edges'][0]['target_message_key']={}
    else:review.update(complete=True,gaps=[]);review['edges'][0]['target_message_key']=None
    assert not synthesize(runtime,'t','o',state,{'objective':'项目甲交付是哪一天','mode':'research'},sources,[],direct=True)
    assert not calls and state['answer_synthesis']['status']=='unavailable'
