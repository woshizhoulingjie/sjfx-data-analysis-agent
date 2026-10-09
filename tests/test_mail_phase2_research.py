"""Phase two budgets, full source ledger, atomic entailment and scope boundaries."""
import json
import time

import pytest

from services.mail.research.claim_verifier import verify_claims
from services.mail.research.evidence_packing import pack_evidence
from services.mail.research.gap_planner import plan_gap_queries, stop_reason
from services.mail.research.planner import validate_plan, phase2_enabled
from services.mail.research.runtime import JobCancelled, MailResearchRuntime
from services.mail.research.token_budget import init_usage, reserve_call, record_usage, usage_snapshot


def source(text='我接受报价 v2。', *, key='e1', message='m1', actor='alice@example.com'):
    return {'evidence_id':key,'message_key':message,'quote_text':text,'char_start':10,
            'source_kind':'body','actor':actor,'issue_id':'quote-2','role':'support',
            'body_fragments':[{'char_start':10,'char_end':10+len(text),'fragment_kind':'authored'}],
            'participants':[{'role':'from','address':actor}]}


def claim(text='Alice 接受报价 v2。', **extra):
    return {'claim_text':text,'actor':'alice@example.com','evidence_ids':['e1'],**extra}


def supporting(system,payload):
    return {'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'supported',
        'support_evidence_ids':c['evidence_ids'],'counter_evidence_ids':[],
        'source_quotes':[{'evidence_id':s['evidence_id'],'quote':s['quote_text']} for s in payload['evidence'] if s['evidence_id'] in c['evidence_ids']],
        'missing_requirements':[]}]} for c in payload['claims']]}


def test_plan_keeps_explicit_selection_and_subquestion_evidence_needs():
    explicit={'message_keys':['m1'],'participant':'alice@example.com','date_to':'2026-09-30'}
    plan=validate_plan({'goal':'接受报价','participant':'evil@example.com','date_to':'2027-01-01',
        'scope':{'message_keys':['outside']},'subquestions':[{'question':'谁接受报价？','actors':['evil@example.com','alice@example.com'],
        'evidence_roles':['support','counter','made_up']}],'required_evidence':['support','condition']},
        '分析 alice@example.com 接受报价的全部邮件',explicit)
    assert plan['scope']==explicit
    assert plan['explicit_scope']==explicit
    assert plan['subquestions'][0]['actors']==['alice@example.com']
    assert plan['required_evidence']==['support','condition']
    assert plan['coverage_mode']=='exhaustive'
    assert not phase2_enabled({'analysis_version':3})
    assert phase2_enabled({'research_version':'mail-research-phase2-v1'})


def test_exact_quote_and_semantic_judge_are_separate():
    evidence=[source('我收到报价 v2。')]
    def judge(system,payload):
        c=payload['claims'][0]
        return {'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'insufficient',
            'support_evidence_ids':[],'counter_evidence_ids':[],'source_quotes':[],
            'missing_requirements':['收到报价不能证明接受；需本人明确回应']}]}]}
    row=verify_claims([claim()],evidence,judge)[0]
    assert row['semantic_status']=='insufficient'
    assert '明确回应' in row['semantic_verification']['missing_requirements'][0]
    assert row['semantic_verification']['independent_validation'] is False


@pytest.mark.parametrize('raw,original,gap',[
    (claim(source_quote='伪造的原文'),source(),'source_quote_not_exact'),
    (claim(actor='bob@example.com'),source(),'actor_source_role_mismatch'),
    (claim(),source('如果审批通过，我接受报价 v2。'),'number_time_negation_or_condition_mismatch'),
])
def test_auxiliary_judge_cannot_override_source_rules(raw,original,gap):
    checked=verify_claims([raw],[original],supporting)[0]
    assert checked['semantic_status']=='insufficient'
    assert gap in checked['semantic_verification']['rule_gaps']


def test_every_compound_clause_needs_a_verdict():
    text='Alice 接受报价 v2；Alice 已实际付款。'
    def judge(system,payload):
        c=payload['claims'][0]
        return {'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':'Alice 接受报价 v2','status':'supported',
            'support_evidence_ids':['e1'],'source_quotes':[{'evidence_id':'e1','quote':'我接受报价 v2。'}]}]}]}
    row=verify_claims([claim(text)],[source()],judge)[0]
    assert row['semantic_status']=='insufficient'
    assert 'atomic_claim_coverage_incomplete' in row['semantic_verification']['rule_gaps']


def test_judge_fake_quote_or_other_claim_sources_do_not_validate():
    def judge(system,payload):
        c=payload['claims'][0]
        return {'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'supported',
            'support_evidence_ids':['e2'],'source_quotes':[{'evidence_id':'e2','quote':'假引用'}]}]}]}
    rows=verify_claims([claim(),claim(evidence_ids=['e2'])],[source(),source('另一事项',key='e2',message='m2')],judge)
    assert rows[0]['semantic_status']=='insufficient'
    assert 'judge_used_unprovided_source' in rows[0]['semantic_verification']['missing_requirements']
    assert 'judge_quote_not_exact' in rows[0]['semantic_verification']['missing_requirements']


def test_scope_exclusion_is_applied_before_semantic_prompt():
    captured=[]
    def judge(system,payload):
        captured.append(json.dumps(payload,ensure_ascii=False))
        return supporting(system,payload)
    row=verify_claims([claim(evidence_ids=['private'])],
        [source('SECRET_PRIVATE 报价 v2',key='private',message='out')],judge,allowed_keys={'m1'})[0]
    assert all('SECRET_PRIVATE' not in p for p in captured)
    assert row['semantic_status']=='insufficient'
    assert 'outside_explicit_scope' in row['semantic_verification']['rule_gaps']


def test_analysis_chunks_and_citations_share_verifier_contract():
    chunk=source();chunk['chunk_id']='chunk-1';chunk['text']=chunk.pop('quote_text');chunk.pop('evidence_id')
    row={'statement':'Alice 接受报价 v2。','actor':'alice@example.com','kind':'action',
         'citations':[{'chunk_id':'chunk-1','quote':chunk['text'],'char_start':10}]}
    checked=verify_claims([row],[chunk],supporting,allowed_keys={'m1'})[0]
    assert checked['semantic_status']=='supported'


def test_packing_protects_conditions_counterpart_and_keeps_omission_manifest():
    rows=[{**source('重复支持'*120,key='support-'+str(i)), 'issue_id':'a','actor':'alice','role':'support'} for i in range(8)]
    rows.extend([{**source('预算尚未批准。',key='counter'), 'issue_id':'b','actor':'bob','role':'counter'},
                 {**source('需审批后执行。',key='condition'), 'issue_id':'a','actor':'bob','role':'condition'}])
    original=json.dumps(rows,ensure_ascii=False)
    selected,meta=pack_evidence(rows,600,max_chars=2500)
    assert {'counter','condition'}.issubset({r['evidence_id'] for r in selected})
    assert len(meta['omitted'])+len(selected)==len(rows)
    assert meta['ledger_complete'] and not meta['synthesis_complete']
    assert json.dumps(rows,ensure_ascii=False)==original
    assert meta['estimated_tokens']<=600


def test_usage_mixes_actual_and_estimates_and_resumes_with_higher_budget():
    usage=init_usage({'max_tokens':200})
    reservation=reserve_call(usage,'system','prompt',50)
    record_usage(usage,{'json':{'answer':'回答'},'usage':{'prompt_tokens':7}},'system','prompt',reservation)
    assert usage['actual_tokens']==7 and usage['estimated_tokens']>0
    assert usage_snapshot(usage)['accounting']=='mixed'
    second=reserve_call(usage,'system','prompt',50)
    record_usage(usage,None,'system','prompt',second)
    assert usage['failed_calls']==1 and usage['reserved_tokens']==0
    assert reserve_call(usage,'system','prompt',1000) is None
    restored=init_usage({'max_tokens':2000},usage)
    assert not restored['exhausted']
    assert restored['total_tokens']==usage['total_tokens']


def test_actual_total_only_is_known_without_inventing_split():
    usage=init_usage({'max_tokens':1000})
    reservation=reserve_call(usage,'system','prompt',50)
    record_usage(usage,{'json':{},'usage':{'total_tokens':123}},'system','prompt',reservation)
    assert usage['total_tokens']==123 and usage['actual_tokens']==123
    assert usage['estimated_tokens']==0 and usage['split_unavailable_calls']==1


def test_gap_queries_are_bounded_and_all_stop_conditions_are_exposed():
    plan={'goal':'报价','queries':['报价'],'required_evidence':['support','counter','condition']}
    gap=plan_gap_queries(plan,[source()],limit=1)
    assert len(gap['queries'])==1 and gap['requirements_verified'] is False
    assert stop_reason(round_number=1,max_rounds=2,new_count=0)=='no_new_evidence'
    for kw,expected in [('cancelled','cancelled'),('token_limit','token_budget'),('deadline','time_budget'),
                        ('tool_limit','tool_budget'),('model_limit','model_budget')]:
        assert stop_reason(round_number=0,max_rounds=2,**{kw:True})==expected
    assert stop_reason(round_number=2,max_rounds=2)=='round_budget'


class Log:
    def __init__(self):self.events=[];self.steps=[]
    def event(self,*args):self.events.append(args)
    def record_research_step(self,*args,**kwargs):self.steps.append((args,kwargs))
    def get_turn(self,*args):return {'status':'running'}
    def update_turn(self,*args,**kwargs):pass


def state(max_tokens=10000):
    return {'phase2':True,'max_model_calls':4,'model_calls':0,'max_seconds':30,'started':time.monotonic(),
            'deadline_reached':False,'tool_limit_reached':False,'max_tool_calls':10,'tool_calls':0,
            'analysis_truncated':False,'token_usage':init_usage({'max_tokens':max_tokens})}


def test_runtime_token_stop_does_not_call_model_and_cancel_is_cooperative():
    class Model:
        def chat_json(self,*args,**kwargs):raise AssertionError('budget should prevent dispatch')
    runtime=MailResearchRuntime(Log(),model=Model())
    current=state(10)
    assert runtime._model_call('t','o',current,'semantic_claim_verification','system','{}',required_fields=('verdicts',)) is None
    assert current['model_calls']==0 and current['token_usage']['exhausted']
    runtime.cancel_check=lambda:True
    with pytest.raises(JobCancelled):runtime._continue_turn('t','o',current)


def test_runtime_accounts_provider_usage_and_preserves_all_phase2_sources():
    class Model:
        def chat_json(self,*args,**kwargs):return {'json':{'verdicts':[]},'usage':{'input_tokens':10,'output_tokens':4}}
    log=Log();runtime=MailResearchRuntime(log,model=Model());current=state()
    assert runtime._model_call('t','o',current,'semantic_claim_verification','system','{}',required_fields=('verdicts',))=={'verdicts':[]}
    assert current['token_usage']['total_tokens']==14
    assert len([s for s in log.steps if s[0][1]=='token_accounting'])==2
    rows=[source(key='e'+str(i),message='m'+str(i)) for i in range(55)]
    current['context_evidence']=[];current['context_reviews']={}
    assert len(runtime._contextual_evidence(current,rows))==55


def test_atomic_actor_gate_uses_judge_quote_span_not_other_authored_text():
    text='Alice 接受报价 v2。\nBob 已完成付款。'
    original=source(text)
    split=text.index('Bob')+10
    original['body_fragments']=[{'char_start':10,'char_end':split,'fragment_kind':'authored'},
                                {'char_start':split,'char_end':10+len(text),'fragment_kind':'quoted'}]
    raw=claim('Alice 已完成付款。',source_quote='Alice 接受报价 v2。')
    def judge(system,payload):
        cid=payload['claims'][0]['claim_id']
        return {'verdicts':[{'claim_id':cid,'atoms':[{'text':raw['claim_text'],'status':'supported',
            'support_evidence_ids':['e1'],'source_quotes':[{'evidence_id':'e1','quote':'Bob 已完成付款。'}]}]}]}
    checked=verify_claims([raw],[original],judge)[0]
    assert checked['semantic_status']=='insufficient'
    assert 'atomic_actor_authorship_unproven' in checked['semantic_verification']['missing_requirements']


def test_gap_plan_does_not_fill_one_issue_with_another_issue_counter():
    rows=[source(),{**source(key='e2'),'issue_id':'other','role':'counter'}]
    plan=plan_gap_queries({'queries':['报价'],'required_evidence':['counter']},rows,limit=1)
    assert plan['missing_by_issue']['quote-2']==['counter']
    assert plan['missing_by_issue']['other']==[]
    assert plan_gap_queries({},rows,limit=0)['queries']==[]
    followup=plan_gap_queries({'queries':['报价']},rows,verification_gaps=[
        {'semantic_status':'insufficient','object_ref':'报价 v2','missing_requirements':['需本人明确接受回应']}])
    assert followup['queries'][0]=='报价 v2 接受 回应'


@pytest.fixture
def phase2_repo(tmp_path,monkeypatch):
    from services.storage import Storage
    from services.mail.repository import MailRepository
    from services.mail.models import MailMember
    from services.mail.parsers.eml import parse_eml
    for flag in ('MAIL_ENABLE_AGENT_MINING','MAIL_ENABLE_VECTOR_INDEX','MAIL_ENABLE_FACT_EXTRACTION'):
        monkeypatch.setattr('config.Config.'+flag,False)
    repo=MailRepository(Storage(tmp_path/'phase2.sqlite3'));repo.migrate()
    package,_=repo.create_package(owner_id='owner',display_name='phase2',source_kind='directory',
        source_path=str(tmp_path),source_revision='fixture-v1',config={})
    def ingest(body='Alice 确认报价 v2。',date='Tue, 01 Sep 2026 12:00:00 +0000',number=1):
        raw=('From: Alice <alice@example.com>\r\nTo: Bob <bob@example.com>\r\nDate: '+date+
             '\r\nMessage-ID: <phase2-'+str(number)+'@example.com>\r\nSubject: 报价\r\n'
             'Content-Type: text/plain; charset=utf-8\r\n\r\n'+body).encode()
        return repo.upsert_ingested_message(package_id=package['package_id'],
            source={'locator_type':'directory','root_path':str(tmp_path),'container_path':None,
                    'container_sha256':'fixture-v1','source_revision':'fixture-v1'},
            member=MailMember(str(number)+'.eml',number,len(raw),{'kind':'file','path':'unused'}),raw=raw,parsed=parse_eml(raw))['message_key']
    return repo,package,ingest


class Phase2Model:
    def __init__(self):self.prompts=[];self.purposes=[]
    def chat_json(self,system,prompt,**kwargs):
        self.prompts.append(prompt);fields=kwargs['required_fields'];self.purposes.append(fields)
        payload=json.loads(prompt)
        if fields==('goal','criteria','queries'):
            value={'goal':'确认报价','criteria':[],'queries':['报价'],'required_evidence':['support']}
        elif fields==('queries',):value={'queries':['报价']}
        elif fields==('actions',):value={'actions':[{'name':'stop'}]}
        elif fields==('decisions',):
            value={'decisions':[{'message_key':m['message_key'],'status':'matched',
                'evidence_id':m['evidence'][0]['evidence_id'],'source_quote':m['evidence'][0]['text'],
                'rationale':'本人明确确认报价'} for m in payload['messages']]}
        elif fields==('answer','claims'):
            e=next(e for e in payload['evidence'] if 'Alice 确认报价' in e['quote_text'])
            value={'answer':'报价获确认','claims':[{'claim_text':'Alice 确认报价 v2。',
                'subject_ref':'Alice','actor':'alice@example.com','object_ref':'报价 v2',
                'predicate':'confirmed','state':'asserted','evidence_ids':[e['evidence_id']],
                'source_quote':'Alice 确认报价 v2。','time_basis':'邮件表达','uncertainty':''}]}
        elif fields==('sentences',):value={'sentences':[]}
        elif fields==('verdicts',):value=supporting(system,payload)
        else:raise AssertionError(fields)
        return {'json':value,'usage':{'input_tokens':10,'output_tokens':5}}


def create_phase2_turn(repo,package,scope=None,**budget):
    from services.mail.research.intent import build_task_spec
    from services.mail.research.planner import VERSION
    session=repo.create_session(package['package_id'],'owner','phase2')
    spec=build_task_spec('分析确认报价',package['package_id'],package['source_revision'],package['index_epoch'],
        scope=scope or {},research_version=VERSION,max_model_calls=12,max_tool_calls=40,max_tokens=100000)
    spec['budget'].update({'max_gap_rounds':0,**budget})
    return repo.create_turn(session['session_id'],'owner',spec['objective'],spec)


def test_phase2_runs_original_sources_atomic_judge_and_cutoff_end_to_end(phase2_repo):
    repo,package,ingest=phase2_repo
    selected=ingest()
    excluded=ingest('SECRET_FUTURE Alice 确认报价 v2。','Thu, 01 Oct 2026 12:00:00 +0000',2)
    turn=create_phase2_turn(repo,package,{'as_of':'2026-09-15'})
    model=Phase2Model();final=MailResearchRuntime(repo,model=model).run(turn['turn_id'],'owner')
    assert final['claims'][0]['semantic_status']=='supported',json.dumps([final['claims'][0]['semantic_verification'],final['evidence']],ensure_ascii=False)
    assert final['claims'][0]['support_status']=='supported'
    assert final['research_plan']['as_of']=='2026-09-15'
    assert {e['message_key'] for e in final['evidence']}=={selected}
    assert excluded not in {e['message_key'] for e in final['evidence']}
    assert all('SECRET_FUTURE' not in prompt for prompt in model.prompts)
    assert ('verdicts',) in model.purposes
    assert final['token_usage']['actual_tokens']==15*len(model.prompts)
    result=repo.get_result_set(final['result_set_id'],'owner')
    assert result['definition']['research_version']==final['research_version']


def test_phase2_resume_preserves_cumulative_calls_and_reuses_scoped_plan(phase2_repo):
    repo,package,ingest=phase2_repo;ingest()
    turn=create_phase2_turn(repo,package,max_model_calls=1)
    model=Phase2Model();runtime=MailResearchRuntime(repo,model=model)
    first=runtime.run(turn['turn_id'],'owner')
    assert first['token_usage']['calls']==1 and not first['claims']
    original_plan_calls=model.purposes.count(('goal','criteria','queries'))
    spec=repo.get_turn(turn['turn_id'],'owner')['task_spec']
    spec['budget'].update(max_model_calls=12,max_tool_calls=80)
    with repo._connection() as c:
        c.execute('UPDATE mail_research_turns SET task_spec_json=? WHERE turn_id=?',(json.dumps(spec),turn['turn_id']))
    second=runtime.run(turn['turn_id'],'owner')
    assert model.purposes.count(('goal','criteria','queries'))==original_plan_calls
    assert second['token_usage']['calls']==len(model.prompts)
    assert second['coverage']['research']['tool_calls']>first['coverage']['research']['tool_calls']
    assert second['claims'][0]['semantic_status']=='supported'
    spec['scope']={'message_keys':[]}
    with repo._connection() as c:
        c.execute('UPDATE mail_research_turns SET task_spec_json=? WHERE turn_id=?',(json.dumps(spec),turn['turn_id']))
    with pytest.raises(ValueError,match='不得改变'):
        runtime.run(turn['turn_id'],'owner')


def test_runtime_gap_search_shares_round_candidate_and_scope_caps():
    runtime=MailResearchRuntime(Log(),model=object());current=state(100000)
    current.update(retrieval_gaps=[],gap_search={'rounds':[],'stop_reason':'not_started'})
    calls=[]
    class Tools:
        include_attachments=False
        def search_messages(self,query,limit=50):
            calls.append((query,limit))
            # Candidate limits count distinct source spans, not fresh IDs for
            # the exact same quote returned by repeated retrieval queries.
            items=[]
            for i in range(limit):
                text='Alice 对报价第 {} 项提出条件。'.format(i)
                start=100+i*100
                items.append({**source(text,key='new'+str(i),message='m1'),
                              'segment_id':'segment-'+str(i),'char_start':start,'char_end':start+len(text),
                              'body_fragments':[{'char_start':start,'char_end':start+len(text),'fragment_kind':'authored'}]})
            return {'items':items +
                             [source('OUTSIDE_SECRET',key='private',message='outside')]}
    runtime._ledger_search=lambda *a,**kw:[]
    spec={'research_plan':{'goal':'报价','queries':['报价'],'required_evidence':['counter']},
          'budget':{'max_gap_rounds':2,'max_gap_candidates':3}}
    result=runtime._gap_retrieve('t','o',current,spec,Tools(),[source()],{'m1'},['报价'],call_rounds=1)
    assert len(result)==3 and all(row['message_key']=='m1' for row in result)
    assert calls[0][1]==2 and current['gap_search']['stop_reason']=='candidate_budget'
    total=len(calls)
    runtime._gap_retrieve('t','o',current,spec,Tools(),result,{'m1'},['报价'],call_rounds=1,
        verification_gaps=[{'semantic_status':'insufficient','missing_requirements':['需回应']}])
    assert len(calls)==total


def test_runtime_gap_repeated_source_ids_do_not_consume_candidate_budget():
    runtime=MailResearchRuntime(Log(),model=object());current=state(100000)
    current.update(retrieval_gaps=[],gap_search={'rounds':[],'stop_reason':'not_started'})
    calls=[]
    class Tools:
        include_attachments=False
        def search_messages(self,query,limit=50):
            calls.append((query,limit))
            return {'items':[{**source(key='duplicate'+str(i),message='m1'),
                              'segment_id':'random-segment-'+str(i)} for i in range(limit)] +
                             [source('OUTSIDE_SECRET',key='private',message='outside')]}
    runtime._ledger_search=lambda *a,**kw:[]
    spec={'research_plan':{'goal':'报价','queries':['报价'],'required_evidence':['counter']},
          'budget':{'max_gap_rounds':2,'max_gap_candidates':3}}
    values=runtime._gap_retrieve('t','o',current,spec,Tools(),[source()],{'m1'},['报价'],call_rounds=1)
    assert len(values)==1 and values[0]['evidence_id']=='e1'
    assert {ref['evidence_id'] for ref in values[0]['source_aliases']}=={'e1','duplicate0','duplicate1'}
    assert current['gap_search']['stop_reason']=='no_new_evidence'
    assert current['gap_search']['rounds'][0]['new_evidence']==0
    assert current['gap_search']['rounds'][0]['candidate_count']==1
    assert calls[0][1]==2 and all(row['message_key']=='m1' for row in values)


def test_phase2_memory_requires_matching_research_and_index_versions(phase2_repo):
    from services.mail.research.context import checkpoint,load_memory
    repo,package,ingest=phase2_repo;key=ingest()
    turn=create_phase2_turn(repo,package)
    spec=turn['task_spec'];memory=checkpoint(spec,[{'claim_text':'旧模型断言','support_status':'supported',
        'evidence_ids':['old']}],[{**source(key='old',message=key),'source_kind':'attachment'}],{})
    repo.record_research_step(turn['turn_id'],'research_checkpoint',memory)
    second=repo.create_turn(turn['session_id'],'owner',spec['objective'],spec)
    loaded=load_memory(repo,turn['session_id'],second['turn_id'],'owner',package['package_id'],{key},
        package['source_revision'],research_version='new-research-version',index_epoch=package['index_epoch'])
    assert loaded['stale'] and not loaded['facts']
    loaded=load_memory(repo,turn['session_id'],second['turn_id'],'owner',package['package_id'],{key},
        package['source_revision'],research_version=spec['research_version'],index_epoch=package['index_epoch']+1)
    assert loaded['stale'] and not loaded['facts']
    loaded=load_memory(repo,turn['session_id'],second['turn_id'],'owner',package['package_id'],{key},
        package['source_revision'],research_version=spec['research_version'],index_epoch=package['index_epoch'],
        scope={'include_explicit_attachments':False})
    assert not loaded['facts']


def test_semantic_missing_requirement_triggers_bounded_research_retrieval(phase2_repo,monkeypatch):
    from services.mail.research.tools import MailResearchTools
    repo,package,ingest=phase2_repo;first=ingest()
    second=ingest('Alice 确认报价 v2。补充明确接受回应。','Wed, 02 Sep 2026 12:00:00 +0000',2)
    searched=[]
    def selected_search(tools,query,limit=50):
        searched.append(query)
        key=second if '回应' in query else first
        with repo._connection() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM mail_text_segments WHERE package_id=? AND message_key=?',
                                             (package['package_id'],key))]
        return {'items':[{**r,**repo.create_evidence(package['package_id'],'owner',r)} for r in rows[:limit]]}
    monkeypatch.setattr(MailResearchTools,'search_messages',selected_search)
    class GapModel(Phase2Model):
        def __init__(self):super().__init__();self.judge_calls=0;self.narrative_judge_calls=0
        def chat_json(self,system,prompt,**kwargs):
            result=super().chat_json(system,prompt,**kwargs)
            if kwargs['required_fields']==('verdicts',):
                narrative=bool(json.loads(prompt).get('presentation_version'))
                if narrative:self.narrative_judge_calls+=1
                else:self.judge_calls+=1
                if not narrative and self.judge_calls==1:
                    for row in result['json']['verdicts']:
                        for atom in row['atoms']:
                            atom.update(status='insufficient',missing_requirements=['需明确接受回应'])
            return result
    model=GapModel();turn=create_phase2_turn(repo,package,max_gap_rounds=2)
    final=MailResearchRuntime(repo,model=model).run(turn['turn_id'],'owner')
    assert model.judge_calls==2 and model.narrative_judge_calls==1 and any('回应' in query for query in searched)
    assert final['claims'][0]['semantic_status']=='supported'
    assert len(final['gap_search']['rounds'])<=2
    assert final['gap_search']['scope_widened'] is False
    result=repo.get_result_set(final['result_set_id'],'owner')
    assert {item['item_id'] for item in result['items']}=={first,second}
