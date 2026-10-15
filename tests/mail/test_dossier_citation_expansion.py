"""Only exact authored citations may acquire local source context."""
from copy import deepcopy
import pytest
from services.mail.analysis import _expand_authored_citations, validate_dossier
from services.mail.person_research import checked_citation

FIRST='报价90,000.00元/台，需经财务审批后生效。'
DEADLINE='财务预计2026年9月17日完成审批。'


def sample(text=None):
    text=text or FIRST+'\n'+DEADLINE
    chunk={'chunk_id':'c1','message_key':'m1','segment_id':1,'mime_path':'1',
           'text':text,'char_start':10,'char_end':10+len(text),'source_kind':'body',
           'authored':True,'attribution_version':2,'participants':[{'role':'from','address':'alice@test'}],
           'body_fragments':[{'fragment_kind':'authored','char_start':10,'char_end':10+len(text)}]}
    row={'actor':'alice@test','kind':'action','intent':'accept','issue':'价格',
         'statement':FIRST,'conditions':[],'contradictions':[],
         'unresolved_questions':['财务是否于2026年9月17日完成审批？'],'uncertainty':'未核实',
         'citations':[{'chunk_id':'c1','quote':FIRST,'char_start':10}]}
    return {'findings':[row]},chunk


def test_expand_valid_citation_preserves_claim_and_passes_original_strict_validator():
    value,chunk=sample();before=deepcopy(value)
    with pytest.raises(ValueError):validate_dossier(value,[chunk],['alice@test'],{'m1'})
    expanded,audit=_expand_authored_citations(value,[chunk],['alice@test'],{'m1'})
    assert value==before
    assert {k:v for k,v in expanded['findings'][0].items() if k!='citations'}=={k:v for k,v in before['findings'][0].items() if k!='citations'}
    assert expanded['findings'][0]['citations'][0]['quote']==chunk['text']
    assert audit[0]['original_span']==[10,10+len(FIRST)]
    assert audit[0]['expanded_span']==[10,10+len(chunk['text'])]
    assert len(validate_dossier(expanded,[chunk],['alice@test'],{'m1'}))==1


def test_unique_short_seed_expands_real_authored_body_without_relaxing_final_validation():
    value,chunk=sample();short='需经财务审批后'
    raw={'chunk_id':'c1','quote':short,'char_start':10+chunk['text'].index(short)}
    value['findings'][0]['citations']=[raw];before=deepcopy(value)
    assert len(short)==7 and checked_citation(raw,{'c1':chunk}) is None
    located=checked_citation(raw,{'c1':chunk},min_quote_chars=1)
    assert located['char_start']==raw['char_start'] and not located['quotation_warning']
    assert located['message_key']=='m1' and located['mime_path']=='1'
    with pytest.raises(ValueError):validate_dossier(value,[chunk],['alice@test'],{'m1'})
    expanded,audit=_expand_authored_citations(value,[chunk],['alice@test'],{'m1'})
    assert value==before
    assert {k:v for k,v in expanded['findings'][0].items() if k!='citations'}=={k:v for k,v in before['findings'][0].items() if k!='citations'}
    assert expanded['findings'][0]['citations'][0]=={**raw,'quote':chunk['text'],'char_start':10}
    assert audit[0]['original_span']==[raw['char_start'],raw['char_start']+7]
    assert audit[0]['expanded_span']==[10,chunk['char_end']]
    checked=checked_citation(expanded['findings'][0]['citations'][0],{'c1':chunk})
    assert checked and checked['quote']==chunk['text'] and not checked['quotation_warning']
    assert len(validate_dossier(expanded,[chunk],['alice@test'],{'m1'}))==1


@pytest.mark.parametrize('fault',['ambiguous','overlapping','missing','actor','quoted','uncertain','nonroot','attachment','legacy','overlapping_authored'])
def test_short_seed_cannot_bypass_source_or_unique_occurrence_requirements(fault):
    value,chunk=sample();short='需经财务审批后'
    if fault=='ambiguous':chunk['text']+='\n'+short
    elif fault=='overlapping':short='哈哈';chunk['text']+='\n哈哈哈'
    elif fault=='missing':short='不存在的引用'
    elif fault=='actor':value['findings'][0]['actor']='bob@test'
    elif fault=='quoted':chunk['quoted_ranges']=[(10,10+len(chunk['text']))]
    elif fault=='uncertain':chunk['attribution_uncertain']=True
    elif fault=='nonroot':chunk['message_key']='outside'
    elif fault=='attachment':chunk['source_kind']='attachment'
    elif fault=='legacy':chunk['attribution_version']=1
    elif fault=='overlapping_authored':chunk['body_fragments'].append(deepcopy(chunk['body_fragments'][0]))
    chunk['char_end']=10+len(chunk['text']);chunk['body_fragments'][0]['char_end']=chunk['char_end']
    position=chunk['text'].find(short)
    value['findings'][0]['citations']=[{'chunk_id':'c1','quote':short,'char_start':10+max(0,position)}]
    before=deepcopy(value)
    expanded,audit=_expand_authored_citations(value,[chunk],['alice@test','bob@test'],{'m1'})
    assert audit==[] and expanded==before
    with pytest.raises(ValueError):validate_dossier(expanded,[chunk],['alice@test','bob@test'],{'m1'})


def test_short_seed_expansion_stops_at_real_authored_quotation_boundary():
    value,chunk=sample();body=chunk['text'];short='需经财务审批后'
    chunk['text']+='\nOriginal message\n历史转发内容及其原始报价条件。'
    chunk['char_end']=10+len(chunk['text']);chunk['body_fragments'][0]['char_end']=chunk['char_end']
    chunk['quotation_boundary']=10+len(body)
    value['findings'][0]['citations']=[{'chunk_id':'c1','quote':short,'char_start':10+body.index(short)}]
    expanded,audit=_expand_authored_citations(value,[chunk],['alice@test'],{'m1'})
    assert audit[0]['expanded_span']==[10,10+len(body)]
    assert expanded['findings'][0]['citations'][0]['quote']==body
    assert len(validate_dossier(expanded,[chunk],['alice@test'],{'m1'}))==1


def test_expanded_short_seed_still_rejects_omitted_approval_condition():
    value,chunk=sample();short='需经财务审批后'
    value['findings'][0].update(statement='报价90,000.00元/台。',unresolved_questions=[])
    value['findings'][0]['citations']=[{'chunk_id':'c1','quote':short,'char_start':10+chunk['text'].index(short)}]
    expanded,audit=_expand_authored_citations(value,[chunk],['alice@test'],{'m1'})
    assert audit and expanded['findings'][0]['citations'][0]['quote']==chunk['text']
    assert expanded['findings'][0]['statement']==value['findings'][0]['statement']
    with pytest.raises(ValueError):validate_dossier(expanded,[chunk],['alice@test'],{'m1'})


def test_default_validator_keeps_complete_genuine_short_fragment_exception():
    _,chunk=sample('取消\nOriginal message\nEarlier conditional negotiation details.')
    chunk['body_fragments']=[{'fragment_kind':'authored','char_start':10,'char_end':13},
                             {'fragment_kind':'quoted','char_start':13,'char_end':chunk['char_end']}]
    chunk['quotation_boundary']=13
    short=checked_citation({'chunk_id':'c1','quote':'取消','char_start':10},{'c1':chunk})
    assert short and not short['quotation_warning']
    assert checked_citation({'chunk_id':'c1','quote':'消','char_start':11},{'c1':chunk}) is None


def actual_single_mail():
    text='本邮件为纯合成测试资料。青河项目预算人民币120000元。只有完成三个站点验收后才付款；目前尚未付款。请陈禾确认采购安排。\r\n'
    _,chunk=sample(text)
    chunk.update(char_start=0,char_end=len(text),participants=[{'role':'from','address':'lin.qing@example.invalid'}],
                 body_fragments=[{'fragment_kind':'authored','char_start':0,'char_end':len(text)}])
    rows=[]
    for kind,intent,statement,quote,offset,conditions in [
        ('goal','request','要求陈禾确认采购安排。','请陈禾确认采购安排。',54,[]),
        ('action','inform','告知只有完成三个站点验收后才付款。','只有完成三个站点验收后才付款',26,['完成三个站点验收']),
        ('action','inform','告知目前尚未付款。','目前尚未付款',46,[]),
        ('action','inform','告知青河项目预算为人民币120000元。','青河项目预算人民币120000元',13,[])]:
        rows.append({'actor':'lin.qing@example.invalid','kind':kind,'intent':intent,'issue':'项目采购与付款',
                     'statement':statement,'conditions':conditions,'contradictions':[],
                     'unresolved_questions':[],'uncertainty':'未核实',
                     'citations':[{'chunk_id':'c1','quote':quote,'char_start':offset}]})
    return {'findings':rows},chunk


@pytest.mark.parametrize('offset',[44,46])
def test_actual_four_findings_expand_unpaid_state_to_complete_own_unit(offset):
    value,chunk=actual_single_mail();value['findings'][2]['citations'][0]['char_start']=offset
    before=deepcopy(value);parties=['lin.qing@example.invalid']
    with pytest.raises(ValueError):validate_dossier(value,[chunk],parties,{'m1'})
    expanded,audit=_expand_authored_citations(value,[chunk],parties,{'m1'})
    assert value==before and len(audit)==1
    assert audit[0]['original_span']==[44,50] and audit[0]['expanded_span']==[43,51]
    citation=expanded['findings'][2]['citations'][0]
    assert citation['quote']=='；目前尚未付款。' and len(citation['quote'])==8
    assert chunk['text'][citation['char_start']:citation['char_start']+len(citation['quote'])]==citation['quote']
    assert checked_citation(citation,{'c1':chunk})
    for old,new in zip(before['findings'],expanded['findings']):
        assert {k:v for k,v in old.items() if k!='citations'}=={k:v for k,v in new.items() if k!='citations'}
    checked=validate_dossier(expanded,[chunk],parties,{'m1'})
    assert len(checked)==4 and checked[1]['conditions']==['完成三个站点验收']


@pytest.mark.parametrize('fault',['condition','negation','amount'])
def test_actual_unit_expansion_cannot_remove_own_condition_negation_or_amount(fault):
    value,chunk=actual_single_mail();parties=['lin.qing@example.invalid']
    if fault=='condition':
        row=value['findings'][1];row.update(statement='告知完成三个站点验收后付款。',conditions=[]);seed='三个站点'
    elif fault=='negation':
        row=value['findings'][2];row['statement']='告知目前已付款。';seed='目前尚未付款'
    else:
        row=value['findings'][3];row['statement']='告知青河项目预算为人民币120001元。';seed='预算'
    row['citations']=[{'chunk_id':'c1','quote':seed,'char_start':chunk['text'].index(seed)}]
    expanded,audit=_expand_authored_citations(value,[chunk],parties,{'m1'})
    assert audit and expanded['findings'][value['findings'].index(row)]['statement']==row['statement']
    with pytest.raises(ValueError):validate_dossier(expanded,[chunk],parties,{'m1'})


def test_short_complete_unit_below_default_minimum_does_not_borrow_other_words():
    value,chunk=sample('合同仍待确认；未付款。请陈禾确认采购安排。')
    row=value['findings'][0];row.update(statement='未付款。',unresolved_questions=[])
    row['citations']=[{'chunk_id':'c1','quote':'未付款','char_start':10+chunk['text'].index('未付款')}]
    expanded,audit=_expand_authored_citations(value,[chunk],['alice@test'],{'m1'})
    assert audit==[] and expanded==value
    with pytest.raises(ValueError):validate_dossier(expanded,[chunk],['alice@test'],{'m1'})


def test_actual_four_findings_commit_first_reading_attempt_after_strict_unit_validation(monkeypatch):
    from types import SimpleNamespace
    from services.mail import analysis
    value,chunk=actual_single_mail()
    checkpoint={'phase':'reading','cursor':[0,0],'model_calls':0,'batches':0,'characters_read':0,'findings_count':0}
    spec={'mode':'message_dossier','engine_version':analysis.VERSION,'package_id':'p',
          'context_keys':['m1'],'message_keys':['m1'],'parties':['lin.qing@example.invalid'],
          'objective':'说明主要诉求、付款条件和当前付款状态','manifest':{'metadata_fingerprint':'f'}}
    calls=[];commits=[];events=[]
    repo=SimpleNamespace(get_turn=lambda *args:{'status':'running','task_spec':spec,'final':checkpoint},
                         event=lambda *args:events.append(args))
    monkeypatch.setattr(analysis,'manifest',lambda *args,**kwargs:{'metadata_fingerprint':'f'})
    monkeypatch.setattr(analysis,'next_chunks',lambda *args,**kwargs:([chunk],[1,0]))
    def call(*args,**kwargs):
        calls.append(1)
        return deepcopy(value),{**checkpoint,'model_calls':len(calls)}
    monkeypatch.setattr(analysis,'_call',call)
    monkeypatch.setattr(analysis,'_commit',lambda *args,**kwargs:commits.append((args,kwargs)))
    analysis.run_slice(repo,'t','owner',object())
    assert len(calls)==1 and len(commits)==1
    assert commits[0][0][4]['characters_read']==63 and commits[0][0][4]['findings_count']==4
    findings=commits[0][1]['value']['findings']
    assert findings[2]['citations'][0]['quote']=='；目前尚未付款。'
    assert findings[1]['conditions']==['完成三个站点验收']
    assert any(event[1]=='dossier_citation_context_expanded' and event[2]['accepted'] for event in events)


def test_expansion_leaves_already_valid_findings_and_their_local_context_unchanged():
    delivery='请于2026年10月30日前交付。'
    value,chunk=sample(FIRST+'\n'+DEADLINE+'\n'+delivery)
    row=deepcopy(value['findings'][0])
    row.update(statement=delivery,unresolved_questions=[],citations=[{'chunk_id':'c1','quote':delivery,'char_start':10+chunk['text'].index(delivery)}])
    assert validate_dossier({'findings':[row]},[chunk],['alice@test'],{'m1'})
    value['findings'].append(row)
    expanded,audit=_expand_authored_citations(value,[chunk],['alice@test'],{'m1'})
    assert expanded['findings'][1]==row and len(audit)==1
    assert len(validate_dossier(expanded,[chunk],['alice@test'],{'m1'}))==2


def test_expansion_does_not_invent_missing_deadline():
    value,chunk=sample(FIRST+'\n财务审批尚未完成。')
    expanded,audit=_expand_authored_citations(value,[chunk],['alice@test'],{'m1'})
    assert audit
    with pytest.raises(ValueError,match='17'):validate_dossier(expanded,[chunk],['alice@test'],{'m1'})


@pytest.mark.parametrize('fault',['actor','message','quoted','signature','oversize','outside_chunk','no_quote','invalid_quote','attachment','overlap_signature'])
def test_expansion_requires_same_actor_message_exact_quote_and_bounded_authored_fragment(fault):
    value,chunk=sample()
    if fault=='actor':value['findings'][0]['actor']='bob@test'
    elif fault=='message':chunk['message_key']='outside'
    elif fault in {'quoted','signature'}:chunk['body_fragments'][0]['fragment_kind']=fault
    elif fault=='oversize':chunk['text']+='x'*1201;chunk['body_fragments'][0]['char_end']=10+len(chunk['text'])
    elif fault=='outside_chunk':chunk['body_fragments'][0]['char_end']+=20
    elif fault=='no_quote':value['findings'][0]['citations']=[]
    elif fault=='invalid_quote':value['findings'][0]['citations'][0]['quote']='无此原文的合法引用'
    elif fault=='attachment':chunk['source_kind']='attachment'
    elif fault=='overlap_signature':chunk['body_fragments'].append({'fragment_kind':'signature','char_start':10+len(FIRST)+1,'char_end':10+len(chunk['text'])})
    expanded,audit=_expand_authored_citations(value,[chunk],['alice@test','bob@test'],{'m1'})
    assert audit==[]
    assert expanded==value


@pytest.mark.parametrize('has_deadline',[True,False])
@pytest.mark.parametrize('short_seed',[False,True])
def test_reading_commits_cursor_only_after_strict_revalidation(monkeypatch,has_deadline,short_seed):
    from types import SimpleNamespace
    from services.mail import analysis
    value,chunk=sample(None if has_deadline else FIRST+'\n财务审批尚未完成。')
    if short_seed:
        value['findings']=[deepcopy(value['findings'][0]) for _ in range(3)]
        for row in value['findings'][:2]:row['unresolved_questions']=[]
        quote='需经财务审批后'
        value['findings'][2]['citations']=[{'chunk_id':'c1','quote':quote,'char_start':10+chunk['text'].index(quote)}]
    checkpoint={'phase':'reading','cursor':[0,0],'model_calls':0,'batches':0,'characters_read':0,'findings_count':0}
    spec={'mode':'message_dossier','engine_version':analysis.VERSION,'package_id':'p',
          'context_keys':['m1'],'message_keys':['m1'],'parties':['alice@test'],
          'objective':'核查审批条件','manifest':{'metadata_fingerprint':'f'}}
    events=[];calls=[];commits=[]
    repo=SimpleNamespace(get_turn=lambda *args:{'status':'running','task_spec':spec,'final':checkpoint},
                         event=lambda *args:events.append(args))
    monkeypatch.setattr(analysis,'manifest',lambda *args,**kwargs:{'metadata_fingerprint':'f'})
    monkeypatch.setattr(analysis,'next_chunks',lambda *args,**kwargs:([chunk],[99,0]))
    def call(*args,**kwargs):
        calls.append(1)
        return deepcopy(value),{**checkpoint,'model_calls':len(calls)}
    monkeypatch.setattr(analysis,'_call',call)
    monkeypatch.setattr(analysis,'_commit',lambda *args,**kwargs:commits.append((args,kwargs)))
    if has_deadline:
        analysis.run_slice(repo,'t','owner',object())
        assert len(calls)==1  # Context repair needs no second model request.
        assert len(commits)==1 and commits[0][0][4]['cursor']==[99,0]
        assert len(commits[0][1]['value']['findings'])==(3 if short_seed else 1)
        assert any(event[1]=='dossier_citation_context_expanded' and event[2]['accepted'] for event in events)
    else:
        with pytest.raises(ValueError,match='17'):analysis.run_slice(repo,'t','owner',object())
        assert commits==[] and checkpoint['cursor']==[0,0]
        assert any(event[1]=='dossier_citation_context_expanded' and not event[2]['accepted'] for event in events)


def test_nested_validator_reports_global_finding_index():
    value,chunk=sample()
    value,_=_expand_authored_citations(value,[chunk],['alice@test'],{'m1'})
    row=value['findings'][0]
    rows=[deepcopy(row) for _ in range(3)];rows[2]['citations']=[]
    with pytest.raises(ValueError,match="'finding_index': 3"):
        validate_dossier({'findings':rows},[chunk],['alice@test'],{'m1'})


def test_repair_feedback_uses_current_expanded_validation_error(monkeypatch):
    from types import SimpleNamespace
    from services.mail import analysis
    value,chunk=sample();value['findings']=[deepcopy(value['findings'][0]) for _ in range(3)]
    value['findings'][2].update(kind='inform',intent='inform')
    checkpoint={'phase':'reading','cursor':[0,0],'model_calls':0,'batches':0,'characters_read':0,'findings_count':0}
    spec={'mode':'message_dossier','engine_version':analysis.VERSION,'package_id':'p',
          'context_keys':['m1'],'message_keys':['m1'],'parties':['alice@test'],
          'objective':'核查','manifest':{'metadata_fingerprint':'f'}}
    events=[];calls=[];commits=[]
    repo=SimpleNamespace(get_turn=lambda *args:{'status':'running','task_spec':spec,'final':checkpoint},event=lambda *args:events.append(args))
    monkeypatch.setattr(analysis,'manifest',lambda *args,**kwargs:{'metadata_fingerprint':'f'})
    monkeypatch.setattr(analysis,'next_chunks',lambda *args,**kwargs:([chunk],[99,0]))
    def call(repo,tid,owner,spec,cp,model,system,payload,*args):
        calls.append(deepcopy(payload))
        if len(calls)==1:return deepcopy(value),{**checkpoint,'model_calls':1}
        assert '第3条发现' in payload['validation_error'] and 'kind=inform' in payload['validation_error']
        assert '缺少数字/日期' not in payload['validation_error']
        fixed=deepcopy(payload['previous_output']);fixed['findings'][2]['kind']='action'
        assert fixed['findings'][0]['citations'][0]['quote']==chunk['text']
        return fixed,{**checkpoint,'model_calls':2}
    monkeypatch.setattr(analysis,'_call',call)
    monkeypatch.setattr(analysis,'_commit',lambda *args,**kwargs:commits.append((args,kwargs)))
    analysis.run_slice(repo,'t','owner',object())
    assert len(calls)==2 and len(commits)==1
    assert '17' in next(event[2]['error'] for event in events if event[1]=='dossier_validation_failed')
    assert 'kind=inform' in next(event[2]['error'] for event in events if event[1]=='dossier_citation_context_expanded')


@pytest.mark.parametrize('repair',['authored','kinds_only','unchanged'])
def test_all_invalid_rows_reported_before_repair_and_no_partial_cursor_commit(monkeypatch,repair):
    from types import SimpleNamespace
    from services.mail import analysis
    value,body=sample();value,_=_expand_authored_citations(value,[body],['alice@test'],{'m1'})
    attachment={**deepcopy(body),'chunk_id':'c2','mime_path':'2','source_kind':'attachment','body_fragments':[]}
    first=deepcopy(value['findings'][0]);first['kind']='change'
    second=deepcopy(value['findings'][0]);second['citations'][0]['chunk_id']='c2'
    value={'findings':[first,second]}
    checkpoint={'phase':'reading','cursor':[0,0],'model_calls':0,'batches':0,'characters_read':0,'findings_count':0}
    spec={'mode':'message_dossier','engine_version':analysis.VERSION,'package_id':'p',
          'context_keys':['m1'],'message_keys':['m1'],'parties':['alice@test'],
          'objective':'核查','manifest':{'metadata_fingerprint':'f'}}
    calls=[];commits=[]
    repo=SimpleNamespace(get_turn=lambda *args:{'status':'running','task_spec':spec,'final':checkpoint},event=lambda *args:None)
    monkeypatch.setattr(analysis,'manifest',lambda *args,**kwargs:{'metadata_fingerprint':'f'})
    monkeypatch.setattr(analysis,'next_chunks',lambda *args,**kwargs:([body,attachment],[99,0]))
    def call(repo,tid,owner,spec,cp,model,system,payload,*args):
        calls.append(deepcopy(payload))
        fixed=deepcopy(value)
        if len(calls)==2:
            feedback=payload['validation_error']
            assert '第1条发现' in feedback and '第2条发现' in feedback
            assert 'change requires' in feedback and 'demonstrably authored body' in feedback
            assert '禁止凭附件' in payload['repair_instruction']
            if repair!='unchanged':fixed['findings'][0]['kind']='action'
            if repair=='authored':fixed['findings'][1]['citations'][0]['chunk_id']='c1'
        return fixed,{**checkpoint,'model_calls':len(calls)}
    monkeypatch.setattr(analysis,'_call',call)
    monkeypatch.setattr(analysis,'_commit',lambda *args,**kwargs:commits.append((args,kwargs)))
    if repair=='authored':
        analysis.run_slice(repo,'t','owner',object())
        assert len(commits)==1 and len(commits[0][1]['value']['findings'])==2
        assert commits[0][0][4]['cursor']==[99,0]
    else:
        with pytest.raises(ValueError,match='demonstrably authored body'):analysis.run_slice(repo,'t','owner',object())
        assert commits==[] and checkpoint['cursor']==[0,0]
    assert len(calls)==2
