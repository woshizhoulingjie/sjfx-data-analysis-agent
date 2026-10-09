"""Malformed factual output shrinks a continuous source prefix, never coverage."""
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.mail.person_activity import activity_page,_saved_findings
from services.mail.person_research import run_person_slice,JobCancelled
from services.mail.person_activity_api import resume_analysis
from services.model_output import ModelOutputError
from services.ollama import LocalModelError
from test_person_context_upgrade import person_data
from test_person_activity_v3 import make_turn,finish,Model


def large_sources(person_data):
    repo,pid,add=person_data
    for index in range(3):
        add(('I request approval before proceeding with agreement %s. '%index)*28)
    return repo,pid,make_turn(repo,pid)


class CapacityModel(Model):
    def __init__(self,checker=False,always=False,truncated=False):
        super().__init__();self.checker=checker;self.always=always;self.truncated=truncated
    def chat_json(self,system,prompt,**kwargs):
        p=json.loads(prompt)
        chars=sum(len(s['quote']) for s in p['citation_spans'] if not next(c for c in p['chunks'] if c['chunk_id']==s['chunk_id']).get('context_overlap'))
        if (self.always or chars>1400) and (not self.checker or 'draft_findings' in p):
            self.payloads.append(p)
            if self.truncated:return {'json':{'findings':[]},'finish_reason':'length','usage':{'input_tokens':40,'output_tokens':5000,'total_tokens':5040}}
            try:raise ModelOutputError('模型未返回可解析的 JSON：Unterminated string')
            except ModelOutputError as exc:
                raise LocalModelError('人物事实报告 返回不符合结构化契约：'+str(exc)) from exc
        return super().chat_json(system,prompt,**kwargs)


@pytest.mark.parametrize('checker,truncated',[(False,False),(True,False),(False,True)])
def test_smaller_batches_recheck_all_source_ranges_and_keep_failed_usage(person_data,checker,truncated):
    repo,pid,tid=large_sources(person_data);model=CapacityModel(checker=checker,truncated=truncated)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        result=run_person_slice(repo,tid,'owner',model,char_budget=4000)
    before=repo.get_turn(tid,'owner')['final']
    assert result['_requeue_slice'] and before['cursor']==[0,0] and before['characters_read']==0 and before['batches']==0
    assert before['activity']['state']=='retrying_smaller_batch'
    assert before['adaptive_batch']['char_budget']<before['adaptive_batch']['failed_characters']
    assert not _saved_findings(repo,tid)
    spent=before['model_calls'];assert spent==2 if checker else spent==1
    page=finish(repo,tid,model,budget=4000)
    final=repo.get_turn(tid,'owner')['final']
    assert page['report']['complete'] and final['characters_read']==final['corpus']['characters']
    assert final['model_calls']==len(model.payloads)>spent
    assert final['token_usage']['failed_calls']>=1
    if truncated:assert final['token_usage']['actual_tokens']>=5040
    assert final['adaptive_batch']['retry_count']==0
    # Saved primary intervals cover every immutable segment exactly once.
    with repo._connection() as c:
        steps=[json.loads(r[0]) for r in c.execute("SELECT output_ref FROM mail_research_steps WHERE turn_id=? AND tool_name='person_activity_batch' ORDER BY sequence",(tid,))]
        segments=[dict(r) for r in c.execute('SELECT segment_id,char_start,char_end FROM mail_text_segments WHERE package_id=?',(pid,))]
    intervals={}
    for step in steps:
        for finding in step['findings']:
            for q in finding['citations']:
                if q['chunk_id'] in step['scanned_chunk_ids']:
                    intervals.setdefault(q['segment_id'],set()).add((q['char_start'],q['char_end']))
    for source in segments:
        spans=sorted(intervals[source['segment_id']]);position=source['char_start']
        for start,end in spans:
            assert start==position;position=end
        assert position==source['char_end']


def test_minimum_output_failure_stops_with_original_cursor_and_clear_error(person_data):
    repo,pid,tid=large_sources(person_data);model=CapacityModel(always=True)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        for _ in range(5):
            try:run_person_slice(repo,tid,'owner',model,char_budget=4000)
            except ValueError as exc:
                assert '最小批次或缩批次数已达上限' in str(exc);break
        else:raise AssertionError('retries were not bounded')
    turn=repo.get_turn(tid,'owner');progress=turn['final']
    assert turn['status']=='failed' and turn['error']['code']=='person_fact_output_incomplete'
    assert progress['activity']['state']=='failed' and progress['cursor']==[0,0] and progress['characters_read']==0
    assert progress['model_calls']==len(model.payloads)<=4 and progress['token_usage']['failed_calls']==len(model.payloads)
    assert len(progress['adaptive_batch']['failures'])==len(model.payloads) and not _saved_findings(repo,tid)


@pytest.mark.parametrize('error',[LocalModelError('HTTP request timed out'),ValueError('引用原文校验失败'),JobCancelled('用户停止')])
def test_other_failures_are_never_swallowed_by_adaptation(person_data,error):
    repo,pid,tid=large_sources(person_data)
    class Other(Model):
        def chat_json(self,*args,**kwargs):raise error
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        with pytest.raises(type(error)):run_person_slice(repo,tid,'owner',Other(),char_budget=4000)
    final=repo.get_turn(tid,'owner')['final']
    assert final['cursor']==[0,0] and final['characters_read']==0 and not final.get('adaptive_batch')
    assert final['model_calls']==1


def test_retry_obeys_call_budget_without_extra_provider_attempt(person_data):
    repo,pid,add=person_data
    add('I request approval for this agreement. '*100)
    tid=make_turn(repo,pid,budget=1);model=CapacityModel(always=True)
    page=finish(repo,tid,model,budget=4000)
    assert page['progress']['budget_exhausted'] and not page['report']['complete']
    assert page['progress']['characters_read']==0 and len(model.payloads)==1
    assert page['progress']['token_usage']['failed_calls']==1


def test_cancellation_arriving_with_malformed_response_does_not_requeue(person_data):
    repo,pid,tid=large_sources(person_data);stopped=[False]
    class Interrupted(Model):
        def chat_json(self,*args,**kwargs):
            stopped[0]=True
            raise ModelOutputError('模型未返回可解析的 JSON')
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        with pytest.raises(JobCancelled):run_person_slice(repo,tid,'owner',Interrupted(),cancelled=lambda:stopped[0])
    final=repo.get_turn(tid,'owner')['final']
    assert final['cursor']==[0,0] and not final.get('adaptive_batch')
    assert final['model_calls']==1 and final['token_usage']['failed_calls']==1


def test_resume_preserves_legacy_format_error_before_api_clears_it(person_data):
    repo,pid,tid=large_sources(person_data);model=CapacityModel(always=True)
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        run_person_slice(repo,tid,'owner',model,char_budget=4000)
    final=repo.get_turn(tid,'owner')['final'];reason=final.pop('adaptive_batch')['last_error']
    final['activity']['state']='validating';final['events']=[]
    repo.update_turn(tid,'owner',status='failed',final=final,error={'message':reason})
    turn=repo.get_turn(tid,'owner')
    services=SimpleNamespace(repository=repo,storage=SimpleNamespace(get_job=lambda *a:None),model=Model())
    assert resume_analysis(services,turn,'owner',{},lambda *a:('resume-test',True))[1]==202
    assert repo.get_turn(tid,'owner')['error'] is None
    resumed=Model()
    with patch('services.model_dispatch.background_generation',lambda *a:nullcontext()):
        result=run_person_slice(repo,tid,'owner',resumed,char_budget=4000)
    after=repo.get_turn(tid,'owner')['final']
    assert result['_requeue_slice'] and not resumed.payloads and after['model_calls']==1
    assert after['cursor']==[0,0] and after['adaptive_batch']['char_budget']<4000
    assert reason==after['adaptive_batch']['last_error'] and '_adaptive_resume_output_error' not in after
    assert finish(repo,tid,resumed,budget=4000)['report']['complete']
