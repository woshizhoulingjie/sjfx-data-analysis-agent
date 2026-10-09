"""Real storage relevance repair must keep exact source and message boundaries."""
import json
from contextlib import contextmanager
from types import SimpleNamespace
from services.mail.research.relevance import classify


class Connection:
    def execute(self, sql, args):
        if sql.startswith('SELECT *'):
            return [{'source_kind':'body','mime_path':'1','text':'价格 90,000.00 元/台，需审批后确认。'}]
        return SimpleNamespace(fetchone=lambda:(0,))

class Repository:
    def __init__(self):self.events=[]
    @contextmanager
    def _connection(self):yield Connection()
    def create_evidence(self,pid,owner,row):return {'evidence_id':'own','quote_text':row['text']}
    def event(self,*args):self.events.append(args)
    def record_research_step(self,*args,**kwargs):pass


def run(responses,budget=8):
    repo=Repository();calls=[];state={'max_model_calls':budget,'model_calls':0,'phase2':True}
    def model(*args,**kwargs):
        calls.append(json.loads(args[5]));state['model_calls']+=1
        return responses[len(calls)-1]
    runtime=SimpleNamespace(repository=repo,context_limit=24000,_model_call=model)
    candidates=[{'message_key':'m','evidence_id':'retrieved'}]
    spec={'package_id':'p','objective':'查价格条件','scope':{}}
    result=classify(runtime,'t','o',state,spec,candidates)
    return state,result,calls,repo.events


def value(quote,eid='own',status='matched'):
    return {'decisions':[{'message_key':'m','status':status,'rationale':'价格有关','source_quote':quote,'evidence_id':eid}]}


def test_missing_numeric_spaces_are_repaired_only_by_model():
    initial=value('价格90,000.00元/台');fixed=value('价格 90,000.00 元/台')
    state,result,calls,events=run([initial,fixed])
    assert state['relevance_decisions']['m']['status']=='matched' and result
    assert len(calls)==2 and calls[1]['previous_output']==initial
    assert calls[1]['validation_error'][0]['message_key']=='m'
    assert any(e[1]=='relevance_validation_failed' for e in events)
    assert initial['decisions'][0]['source_quote']=='价格90,000.00元/台'


def test_foreign_evidence_never_promoted_even_after_bad_repair():
    state,result,calls,_=run([value('价格 90,000.00 元/台','foreign'),value('价格 90,000.00 元/台','foreign')])
    decision=state['relevance_decisions']['m']
    assert decision['status']=='unknown' and decision['evidence_id'] is None
    assert '逐字原文核验' in decision['rationale']
    assert len(calls)==2 and result


def test_no_repair_budget_keeps_unknown_and_synthesis_reserve():
    state,result,calls,_=run([value('拼接的错误引文')],budget=3)
    assert len(calls)==1 and state['model_calls']==1
    assert state['relevance_decisions']['m']['status']=='unknown' and result


def test_actual_unknown_decision_is_not_forced_to_match():
    state,result,calls,_=run([value(None,status='unknown')])
    assert len(calls)==1 and state['relevance_decisions']['m']['status']=='unknown' and result
