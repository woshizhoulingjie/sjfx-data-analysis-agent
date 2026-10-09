"""Advisory progress wiring and strict reuse of complete, unchanged inputs."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.mail.research.runtime import MailResearchRuntime


def test_progress_is_throttled_monotonic_and_never_claims_completion():
    updates=[];clock=[0]
    runtime=MailResearchRuntime(None,progress_callback=lambda **data:updates.append(data))
    with patch('services.mail.research.runtime.time.monotonic',lambda:clock[0]):
        runtime._publish_progress(12,'plan','planning')
        for index in range(100):runtime._publish_progress(30,'retrieval',str(index))
        assert len(updates)==1
        clock[0]=2;runtime._publish_progress(50,'analysis','source batch 2/34')
        clock[0]=4;runtime._publish_progress(30,'verification','semantic batch 1/3')
        clock[0]=6;runtime._publish_progress(100,'saving','persisting')
        assert [row['progress'] for row in updates]==[12,50,50,95]
        runtime._publish_progress(100,'saving','persisting',force=True)
        assert len(updates)==4


def test_progress_failures_remain_advisory_and_missing_callback_is_compatible():
    runtime=MailResearchRuntime(None)
    runtime._publish_progress(80,'analysis','source batch 3/34')
    failed=MailResearchRuntime(None,progress_callback=lambda **data:False)
    failed._publish_progress(50,'analysis','count')
    assert failed._progress_snapshot is None
    def broken(**data):raise RuntimeError('temporary publication failure')
    MailResearchRuntime(None,progress_callback=broken)._publish_progress(50,'analysis','count')


def test_research_job_callback_writes_storage_and_preserves_completion_order(monkeypatch):
    from services.mail import jobs
    updates=[];timeline=[]
    class Runtime:
        def __init__(self,repo,**kwargs):self.publish=kwargs['progress_callback']
        def run(self,tid,owner):
            self.publish(progress=51,stage='mail_research_verification',message='sources 3/34; semantic 1/3')
            timeline.append('saved')
            return {'result_set_id':'result'}
    def update(jid,**data):
        timeline.append(data['progress']);updates.append(data);return True
    repo=SimpleNamespace(get_turn=lambda tid,owner:{'session_id':'session','task_spec':{'mode':'research'},'sufficiency':'partial'},
        get_session=lambda sid,owner:{'package_id':'package'},get_package=lambda pid,owner:{'status':'ready'},cancelled=lambda jid:False)
    services=SimpleNamespace(repository=repo,storage=SimpleNamespace(update_job=update),model=object())
    monkeypatch.setattr(jobs,'MailResearchRuntime',Runtime)
    result=jobs._research({'id':'job','owner_id':'owner','options':{'turn_id':'turn'}},services)
    assert timeline==[8,51,'saved',99]
    assert all(row['progress']<100 and row['heartbeat'] for row in updates)
    assert result['result_set_id']=='result'


def test_full_source_batches_publish_actual_counts_without_extra_model_work(monkeypatch):
    clock=[0];updates=[];analyzed=[]
    repo=SimpleNamespace(record_research_step=lambda *a,**k:None,event=lambda *a:None,
        record_claims=lambda *a:None,replace_turn_relations=lambda pid,owner,tid,rows:rows)
    runtime=MailResearchRuntime(repo,context_limit=3000,progress_callback=lambda **data:updates.append(data))
    evidence=[{'evidence_id':str(i),'message_key':'m'+str(i),'quote_text':'original terms'} for i in range(3)]
    state={'phase2':True,'findings':[],'retrieval_gaps':[],'context_reviews':{},'analysis_truncated':False}
    monkeypatch.setattr('services.mail.research.runtime.time.monotonic',lambda:clock[0])
    monkeypatch.setattr('services.mail.research.evidence_packing.pack_evidence_batches',
        lambda sources,*a,**k:([[source] for source in sources],{'source_total':3,'synthesis_complete':True}))
    def batch(tid,owner,current,spec,pid,sources,**kwargs):
        analyzed.extend(row['evidence_id'] for row in sources);clock[0]+=2;current['analysis_completed']=True
        return [],[]
    monkeypatch.setattr(runtime,'_analyze_evidence_batch',batch)
    runtime._analyze_evidence('turn','owner',state,{'objective':'inspect'},'package',evidence)
    assert analyzed==['0','1','2']
    assert '1/3' in updates[1]['message'] and '3/3' in updates[-1]['message']
    assert [row['progress'] for row in updates]==[40,56,73,90]
    assert state['analysis_completed']


def test_semantic_partitions_publish_real_source_and_judge_batch_counts(monkeypatch):
    clock=[0];updates=[];calls=[]
    repo=SimpleNamespace(event=lambda *a:None,record_research_step=lambda *a,**k:None)
    runtime=MailResearchRuntime(repo,progress_callback=lambda **data:updates.append(data))
    state={'phase2':True,'findings':[],'retrieval_gaps':[],'context_reviews':{},
           '_analysis_batch_progress':{'index':4,'total':34,'progress':44}}
    monkeypatch.setattr('services.mail.research.runtime.time.monotonic',lambda:clock[0])
    def model(*args,**kwargs):
        calls.append(args[3]);clock[0]+=2
        return {'answer':'historical expression','claims':[]} if args[3]=='evidence_analysis' else {'verdicts':[]}
    monkeypatch.setattr(runtime,'_model_call',model)
    monkeypatch.setattr('services.mail.research.claim_verifier.pack_judge_payloads',
        lambda payload,**kwargs:[{'claims':[{'claim_id':'c'+str(i)}]} for i in range(3)])
    def verify(claims,evidence,judge,**kwargs):
        judge('audit',{'claims':[]});return []
    monkeypatch.setattr('services.mail.research.claim_verifier.verify_claims',verify)
    runtime._analyze_evidence_batch('t','owner',state,{'objective':'conditions'},'p',
        [{'evidence_id':'e','message_key':'m','quote_text':'source'}],_defer=True)
    messages=[row['message'] for row in updates]
    assert len(messages)==3 and all('4/34' in message for message in messages)
    assert all('{}/3'.format(i+1) in messages[i] for i in range(3))
    assert calls==['evidence_analysis']+['semantic_claim_verification']*3


def inputs():
    spec={'objective':'conditions','scope':{'message_keys':['m'],'as_of':'2026-09-30'},'source_revision':'r1','index_epoch':1}
    evidence=[{'evidence_id':'e','message_key':'m','quote_text':'Accepted subject to approval.',
               'char_start':0,'char_end':29,'source_aliases':[{'mime_path':'1','occurrence_id':'o1'}],
               'body_fragments':[{'fragment_kind':'authored','char_start':0,'char_end':29}],
               'participants':[{'role':'from','normalized_address':'alice@example.test'}],
               'conditions':['subject to approval'],'extraction_method':'native'}]
    state={'analysis_completed':True,'analysis_truncated':False,'model_calls':3,'max_model_calls':100,
           'evidence_packing':{'synthesis_complete':True,'analysis_omitted':[]},
           'semantic_verification':[{'semantic_status':'supported','missing_requirements':[]}],
           'context_reviews':{'m':{'complete':True,'evidence_ids':['e']}},'context_evidence':deepcopy(evidence),
           'allowed_keys':{'m'},'memory':{'conditions':['approval required']},'source_deduplication':{'aliases':{'e':['e']}}}
    return spec,state,evidence


def test_same_full_analysis_inputs_reuse_even_when_candidate_membership_changes():
    runtime=MailResearchRuntime(None);spec,state,evidence=inputs()
    signature=runtime._analysis_input_fingerprint(spec,state,evidence)
    # Retrieval membership is not an analysis input: e was already included
    # from mandatory context, so a new retrieval hit adds no source content.
    state['candidate_ids']=['old','e']
    assert runtime._can_reuse_analysis(spec,state,deepcopy(evidence),signature,True)
    assert not runtime._can_reuse_analysis(spec,state,evidence,signature,False)


@pytest.mark.parametrize('change',['text','alias','condition','attribution','scope','cutoff','revision','epoch','context','memory','new_source','ocr'])
def test_every_source_or_analysis_context_change_forbids_reuse(change):
    runtime=MailResearchRuntime(None);spec,state,evidence=inputs()
    signature=runtime._analysis_input_fingerprint(spec,state,evidence)
    if change=='text':evidence[0]['quote_text']='Approval withdrawn.'
    elif change=='alias':evidence[0]['source_aliases'].append({'mime_path':'2','occurrence_id':'o2'})
    elif change=='condition':evidence[0]['conditions'].append('signed contract required')
    elif change=='attribution':evidence[0]['body_fragments'][0]['fragment_kind']='quoted'
    elif change=='scope':state['allowed_keys'].add('other')
    elif change=='cutoff':spec['scope']['as_of']='2026-10-01'
    elif change=='revision':spec['source_revision']='r2'
    elif change=='epoch':spec['index_epoch']=2
    elif change=='context':state['context_reviews']['m']['complete']=False
    elif change=='memory':state['memory']['conditions'].append('new previous checkpoint')
    elif change=='new_source':evidence.append({'evidence_id':'new','quote_text':'withdrawal'})
    elif change=='ocr':evidence[0]['extraction_method']='ocr'
    assert not runtime._can_reuse_analysis(spec,state,evidence,signature,True)


@pytest.mark.parametrize('limit',['packing','truncated','time','tools','tokens','model','missing_verdict'])
def test_incomplete_or_budget_stopped_analysis_cannot_be_reused(limit):
    runtime=MailResearchRuntime(None);spec,state,evidence=inputs()
    signature=runtime._analysis_input_fingerprint(spec,state,evidence)
    if limit=='packing':state['evidence_packing']['analysis_omitted']=['e']
    elif limit=='truncated':state['analysis_truncated']=True
    elif limit=='time':state['deadline_reached']=True
    elif limit=='tools':state['tool_limit_reached']=True
    elif limit=='tokens':state['token_usage']={'exhausted':True}
    elif limit=='model':state['model_calls']=state['max_model_calls']
    elif limit=='missing_verdict':state['semantic_verification'][0]['missing_requirements']=['missing_or_invalid_atomic_verdict']
    assert not runtime._can_reuse_analysis(spec,state,evidence,signature,True)
