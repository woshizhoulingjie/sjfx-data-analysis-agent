"""Complete bilateral alignment inputs and durable failure/resume contracts."""
import copy
import json
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from services.mail import person_summary
from services.mail.person_research import run_person_slice
from test_person_context_upgrade import person_data, create_analysis, finish, PersonModel


def alignment_payload(count=3):
    entries=[]
    for index in range(count):
        source={'act_id':'source-%s'%index,'issue_id':'source-issue-%s'%index,
                'actor':'alice@example.com','asserted_at':'2024-05-07T08:38:50Z',
                'object_identity':'contract-%s'%index,'object_version':'v2',
                'conditions':['Only after written approval; original deadline 10月16日18:30'],
                'citations':[{'evidence_id':'source-evidence-%s'%index,'char_start':200,'char_end':650,
                    'quote':'If approved, deliver contract %s on 10月16日18:30. '%index*10}]}
        candidates=[{'issue_id':'target-%s-%s'%(index,target),'conflicts':[],
                'acts':[{'act_id':'target-act-%s-%s'%(index,target),'actor':'bob@example.com',
                    'asserted_at':'2024-05-08T08:38:50Z','object_version':'v1',
                    'conditions':['Signed acceptance remains unconfirmed'],
                    'citations':[{'evidence_id':'target-evidence-%s-%s'%(index,target),
                        'quote':'Do not assume contract %s was accepted or fulfilled. '%index*10,
                        'char_start':400,'char_end':950}]}]} for target in range(2)]
        entries.append({'source':source,'candidates':candidates})
    return {'scope':{'message_keys':['mail-1','mail-2'],'as_of':'2024-05-09',
                     'person_addresses':['alice@example.com']},'alignments':entries}


def one_group_pages(payload):
    # The real allocator receives a budget that fits exactly one complete group.
    sizes=[len(json.dumps({'evidence_id':'alignment-'+str(index),
        'issue_id':entry['source']['act_id'],'actor':entry['source']['actor'],
        'role':'support','alignment':entry},ensure_ascii=False))
        for index,entry in enumerate(payload['alignments'])]
    return person_summary.alignment_payload_pages(payload,max_tokens=20000,max_chars=max(sizes)+1)


def decision(entry):
    return {'source_act_id':entry['source']['act_id'],
            'target_issue_id':entry['candidates'][0]['issue_id'],
            'decision':'unresolved','review_status':'insufficient',
            'method':'semantic_support_checked','reason':'Both complete sources leave identity unresolved',
            'evidence_ids':[]}


def test_pages_preserve_every_complete_source_candidate_group_and_scope():
    payload=alignment_payload();original=copy.deepcopy(payload)
    pages=one_group_pages(payload)
    assert len(pages)==3
    assert all(page['scope']==original['scope'] for page in pages)
    assert [entry for page in pages for entry in page['alignments']]==original['alignments']
    assert payload==original


def test_single_oversized_bilateral_group_stops_without_clipping():
    payload=alignment_payload(1);original=copy.deepcopy(payload)
    with pytest.raises(ValueError,match='未裁剪证据'):
        person_summary.alignment_payload_pages(payload,max_tokens=2100,max_chars=100)
    assert payload==original


@pytest.mark.parametrize('invalid',['missing_decisions','foreign_pair','foreign_target','wrong_method','bad_evidence'])
def test_invalid_page_verdict_is_not_a_real_negative_judgment(invalid):
    payload=alignment_payload(1);row=decision(payload['alignments'][0]);value={'decisions':[row]}
    if invalid=='missing_decisions':value={}
    elif invalid=='foreign_pair':row['source_act_id']='unprovided-source'
    elif invalid=='foreign_target':row['target_issue_id']='unprovided-target'
    elif invalid=='wrong_method':row['method']='title_similarity'
    else:row['evidence_ids']=[None]
    with pytest.raises(ValueError):person_summary.checked_alignment_response(value,payload)
    assert person_summary.checked_alignment_response({'decisions':[]},payload)==[]


class AlignmentModel(PersonModel):
    def __init__(self,failure=None):
        super().__init__();self.failure=failure;self.alignment_calls=[];self.failed=False

    def chat_json(self,system,prompt,**kwargs):
        value=json.loads(prompt)
        if 'alignments' not in value:return super().chat_json(system,prompt,**kwargs)
        self.calls.append(value);self.alignment_calls.append(value)
        if len(self.alignment_calls)==2 and self.failure and not self.failed:
            self.failed=True
            if self.failure=='network':raise RuntimeError('temporary alignment network outage')
            if self.failure=='capacity':raise RuntimeError('alignment input exceeds model context capacity')
            return {'json':{'not_decisions':[]}}
        return {'json':{'decisions':[decision(entry) for entry in value['alignments']]}}


def saved_alignment_steps(repo,tid):
    with repo._connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT step_id,tool_name,input_json,output_ref FROM mail_research_steps "
            "WHERE turn_id=? AND tool_name IN ('person_alignment_part','person_alignment_batch') ORDER BY sequence",
            (tid,)).fetchall()]


@pytest.mark.parametrize('failure',['network','capacity','format'])
def test_alignment_failure_preserves_completed_pages_and_resumes_without_fake_aggregate(person_data,failure):
    repo,pid,add=person_data;add();tid=create_analysis(repo,pid)
    payload=alignment_payload();model=AlignmentModel(failure);applied=[]
    original_packer=person_summary.alignment_payload_pages
    def pack_small(value):
        with patch.object(person_summary,'alignment_payload_pages',original_packer):
            return one_group_pages(value)
    def audit_adapter(repo,spec,owner_id,findings,judge,**kwargs):
        result=judge('Audit complete bilateral evidence',copy.deepcopy(payload))
        assert {row['source_act_id'] for row in result['decisions']}=={'source-0','source-1','source-2'}
        applied.append(result)
        return {'status':'reviewed','applied':[],'gaps':['identity_unresolved']}
    with patch('services.model_dispatch.background_generation',lambda *args:nullcontext()), \
            patch('services.mail.issue_repository.align_workflow_findings',audit_adapter), \
            patch.object(person_summary,'alignment_payload_pages',pack_small):
        for _ in range(30):
            try:run_person_slice(repo,tid,'owner',model)
            except (RuntimeError,ValueError):break
        else:raise AssertionError('Second-page simulated failure did not stop the analysis')
        turn=repo.get_turn(tid,'owner');state=turn['final'];steps=saved_alignment_steps(repo,tid)
        assert turn['status']=='failed' and not state['complete']
        assert len(steps)==1 and steps[0]['tool_name']=='person_alignment_part'
        assert '_alignment_paged_v4_' in steps[0]['step_id'] and applied==[]
        assert state['alignment_progress']=={'batch_index':0,'pages_completed':1,'pages_total':3,
            'requests_checked':1,'state':'failed','error':state['alignment_progress']['error']}
        assert state['alignment_progress']['error']
        assert state['token_usage']['calls']==state['model_calls']==len(model.calls)
        first_page=steps[0]
        repo.update_turn(tid,'owner',status='queued')
        page=finish(repo,tid,model)
    final_steps=saved_alignment_steps(repo,tid)
    assert page['status']=='completed' and page['progress']['complete']
    assert page['progress']['alignment_progress']['state']=='completed'
    assert final_steps[0]==first_page
    assert [step['tool_name'] for step in final_steps]==['person_alignment_part']*3+['person_alignment_batch']
    assert len(applied)==1 and len(model.alignment_calls)==4
    assert [call['alignments'][0]['source']['act_id'] for call in model.alignment_calls]==[
        'source-0','source-1','source-1','source-2']
    assert all(call['scope']==payload['scope'] and
        call['alignments'][0]==payload['alignments'][int(call['alignments'][0]['source']['act_id'][-1])]
        for call in model.alignment_calls)
    assert page['progress']['token_usage']['calls']==page['progress']['model_calls']==len(model.calls)
    assert len([call for call in model.calls if 'claims' in call])==1
