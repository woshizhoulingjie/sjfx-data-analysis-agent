"""Only exact authored citations may acquire local source context."""
from copy import deepcopy
import pytest
from services.mail.analysis import _expand_authored_citations, validate_dossier

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
def test_reading_commits_cursor_only_after_strict_revalidation(monkeypatch,has_deadline):
    from types import SimpleNamespace
    from services.mail import analysis
    value,chunk=sample(None if has_deadline else FIRST+'\n财务审批尚未完成。')
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
