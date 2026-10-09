"""Lossless prompt bundles and objective-aware direct answers with safe fallbacks."""
from copy import deepcopy
import json

import pytest

from services.mail.research.answer_synthesis import simple_factual_question, synthesize
from services.mail.research.evidence_packing import bundle_sources, model_source, answer_output_tokens
from test_mail_answer_synthesis import setup


def test_bundle_preserves_every_source_identity_and_distinct_role_version_boundary():
    _,_,sources,_,_=setup();base=sources[1]
    rows=[base,{**deepcopy(base),'evidence_id':'ledger','role':'support','actor':'alice@example.com','issue_id':'agreement'},
        {**deepcopy(base),'evidence_id':'counter','role':'counter','conditions':['签署验收报告'], 'object_version':'v2'},
        {**deepcopy(base),'evidence_id':'different-revision','source_revision':'r2'},
        {**deepcopy(base),'evidence_id':'different-message','message_key':'other'},
        {**deepcopy(base),'evidence_id':'different-mime','mime_path':'2'},
        {**deepcopy(base),'evidence_id':'quoted','attribution':'quoted'}]
    before=deepcopy(rows);records,blocks,documents=bundle_sources(rows)
    assert len(records)==7 and len(blocks)==5 and rows==before
    resolved={record['evidence_id']:{**documents.get('_defaults',{}),**documents[blocks[record['source_ref']]['document_ref']],
        **{k:v for k,v in blocks[record['source_ref']].items() if k!='document_ref'},
        **{k:v for k,v in record.items() if k!='source_ref'}} for record in records}
    assert resolved=={row['evidence_id']:model_source(row,compact=True) for row in rows}
    assert resolved['counter']['conditions']==['签署验收报告'] and resolved['counter']['role']=='counter'
    assert len({record['source_ref'] for record in records[:3]})==1
    quote=base['quote_text']
    assert sum(block.get('quote_text')==quote for block in blocks.values())==5
    payload={'evidence':records,'source_blocks':blocks,'source_documents':documents}
    assert answer_output_tokens(payload)>=answer_output_tokens({'evidence':[model_source(row,compact=True) for row in rows]})


@pytest.mark.parametrize('difference',[None,'missing_revision','different_revision','different_epoch','different_epoch_type'])
def test_shared_document_defaults_reconstruct_present_and_missing_version_metadata(difference):
    _,_,sources,_,_=setup()
    rows=[{**deepcopy(source),'source_revision':'revision-'+'r'*80,'index_epoch':'epoch-'+'e'*80} for source in sources]
    if difference=='missing_revision':rows[-1].pop('source_revision')
    elif difference=='different_revision':rows[-1]['source_revision']='other'
    elif difference=='different_epoch':rows[-1]['index_epoch']='other'
    elif difference=='different_epoch_type':
        for row in rows:row['index_epoch']=1
        rows[-1]['index_epoch']=True
    records,blocks,documents=bundle_sources(rows)
    defaults=documents.get('_defaults',{})
    assert ('source_revision' in defaults)==(difference not in ('missing_revision','different_revision'))
    assert ('index_epoch' in defaults)==(difference not in ('different_epoch','different_epoch_type'))
    resolved={record['evidence_id']:{**defaults,**documents[blocks[record['source_ref']]['document_ref']],
        **{k:v for k,v in blocks[record['source_ref']].items() if k!='document_ref'},
        **{k:v for k,v in record.items() if k!='source_ref'}} for record in records}
    assert resolved=={row['evidence_id']:model_source(row,compact=True) for row in rows}
    payload={'evidence':records,'source_blocks':blocks,'source_documents':documents}
    assert answer_output_tokens(payload)>=answer_output_tokens({'evidence':[model_source(row,compact=True) for row in rows]})


def test_direct_reads_every_source_without_setting_analysis_or_full_history_marker():
    runtime,state,sources,calls,_=setup()
    state['analysis_completed']=False;state.pop('evidence_packing')
    ok=synthesize(runtime,'t','o',state,{'objective':'项目甲交付是哪一天','mode':'research','budget':{'max_context_tokens':12000}},sources,[],direct=True)
    assert ok and state['answer_synthesis']['direct_mode']
    assert state['analysis_completed'] is False and 'evidence_packing' not in state
    assert '_full_scope_history_verified' not in state['context_reviews']
    assert state['answer_synthesis']['source_coverage']['evidence_count']==5
    assert calls[0][0]=='answer_synthesis'
    assert not any(purpose in ('field_extraction','evidence_analysis') for purpose,payload in calls)


@pytest.mark.parametrize('objective,expected',[
    ('项目甲最终交付是哪一天',True),('项目甲价格多少钱',True),('When is the delivery deadline?',True),
    ('梳理所有交付日期和过程',False),('比较各个项目交付风险',False),('分析合同何时完成以及是谁签署',False)])
def test_precise_routing_preserves_complex_and_exhaustive_requests(objective,expected):
    assert simple_factual_question({'objective':objective,'mode':'research'}) is expected
    assert not simple_factual_question({'objective':objective,'mode':'exhaustive'})


@pytest.mark.parametrize('failure',['missing_late_source','unknown_relevance','background','bad_id','cc_actor'])
def test_direct_fails_closed_for_incomplete_or_nonanswering_audits(failure):
    runtime,state,sources,calls,_=setup();base=runtime._model_call
    state['analysis_completed']=False
    if failure=='missing_late_source':state['context_reviews']['m4']['evidence_ids'].append('late-unread')
    def model(*args,**kwargs):
        result=base(*args,**kwargs)
        if args[3]=='answer_synthesis' and failure=='cc_actor':result['claims'][0]['actor']='cc@example.com'
        if args[3]=='semantic_claim_verification':
            for verdict in result['verdicts']:
                if failure=='unknown_relevance':verdict.pop('answer_relevance')
                if failure=='background':verdict['answer_relevance']='background'
                if failure=='bad_id':verdict['claim_id']={'bad':'id'}
        return result
    runtime._model_call=model
    assert not synthesize(runtime,'t','o',state,{'objective':'项目甲最终交付是哪一天','mode':'research','budget':{'max_context_tokens':12000}},sources,[],direct=True)
    assert state['analysis_completed'] is False
    assert state['answer_synthesis']['status']!='complete'


def test_cc_received_kind_cannot_bypass_authored_sender_binding():
    runtime,state,sources,calls,_=setup();base=runtime._model_call
    sources[1]['participants'].append({'role':'cc','normalized_address':'cc@example.com'})
    def model(*args,**kwargs):
        result=base(*args,**kwargs)
        if args[3]=='answer_synthesis':result['claims'][0].update(actor='cc@example.com',kind='received')
        return result
    runtime._model_call=model
    assert not synthesize(runtime,'t','o',state,{'objective':'项目甲交付是哪一天','mode':'research','budget':{'max_context_tokens':12000}},sources,[],direct=True)
    assert not any(purpose=='semantic_claim_verification' for purpose,payload in calls)


def test_repair_extra_good_id_cannot_overwrite_a_passing_relevance_audit():
    runtime,state,sources,calls,_=setup();base=runtime._model_call
    actual_ids={}
    def model(*args,**kwargs):
        result=base(*args,**kwargs)
        if args[3]!='semantic_claim_verification':return result
        payload=json.loads(args[5])
        if payload.get('validation_errors'):
            result['verdicts'].append({'claim_id':actual_ids['good'],'answer_relevance':'background','answer_relevance_reason':'恶意额外行','atoms':[]})
        else:
            actual_ids['good']=next(c['claim_id'] for c in payload['claims'] if c['evidence_ids']==['e1'])
            actual_ids['bad']=next(c['claim_id'] for c in payload['claims'] if c['evidence_ids']==['e4'])
            for verdict in result['verdicts']:
                if verdict['claim_id']==actual_ids['bad']:verdict['atoms'][0]['source_quotes'][0]['quote']='假引用'
        return result
    runtime._model_call=model
    assert synthesize(runtime,'t','o',state,{'objective':'项目甲交付是哪一天','mode':'research','budget':{'max_context_tokens':12000}},sources,[],direct=True)
    assert state['answer_synthesis']['claims'][0]['answer_relevance']=='direct'
    assert state['answer_synthesis']['claims'][0]['semantic_status']=='supported'
    judge_calls=[payload for purpose,payload in calls if purpose=='semantic_claim_verification']
    assert len(judge_calls)>=2 and judge_calls[1].get('validation_errors')
    assert {c['claim_id'] for c in judge_calls[1]['claims']}=={actual_ids['bad']}
