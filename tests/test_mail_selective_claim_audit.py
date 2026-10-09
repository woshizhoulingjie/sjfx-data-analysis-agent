"""Selective repairs and exact in-call reuse retain every semantic/source gate."""
from copy import deepcopy

import pytest

from services.mail.research.claim_verifier import verify_claims
from services.mail.research.verifier import verified_qualification_scope


TEXT='Alice 接受报价。'


def source(key='e1', text=TEXT):
    return {'evidence_id':key,'message_key':'m'+key,'quote_text':text,'char_start':10,
            'source_kind':'body','attribution':'authored',
            'participants':[{'role':'from','address':'alice@test'}],
            'body_fragments':[{'char_start':10,'char_end':10+len(text),'fragment_kind':'authored'}]}


def claim(cid='c1', key='e1', **extra):
    return {'claim_id':cid,'claim_text':TEXT,'evidence_ids':[key],
            'actor':'alice@test','source_quote':TEXT,**extra}


def verdicts(payload, bad=()):
    sources={s['evidence_id']:s for s in payload['evidence']}
    return {'verdicts':[{'claim_id':c['claim_id'],'atoms':[{
        'text':c['claim_text'],'status':'supported','support_evidence_ids':c['evidence_ids'],
        'counter_evidence_ids':[], 'missing_requirements':[],
        'source_quotes':[{'evidence_id':e,'quote':'错引' if c['claim_id'] in bad else c.get('source_quote') or sources[e]['quote_text']}
                         for e in c['evidence_ids'] if e in sources]}],
        'qualification_reviews':[{**q,'applicability':'not_applicable','rationale':'该条件针对另一个事项。'}
                                 for q in payload['qualification_checks'].get(c['claim_id'],[])]}
        for c in payload['claims']]}


def test_repair_selects_only_failed_claim_with_all_sources_and_ignores_other_changes():
    claims=[claim('good','e1'),claim('bad','e2',counter_evidence_ids=['counter'])]
    evidence=[source(),source('e2'),source('counter','Alice 不接受报价。')]
    captured=[];initial=[]
    def judge(system,payload):
        captured.append(deepcopy(payload))
        if len(captured)==1:
            value=verdicts(payload,bad={'bad'});initial.append(deepcopy(value))
            return value
        assert [c['claim_id'] for c in payload['claims']]==['bad']
        assert {s['evidence_id'] for s in payload['evidence']}=={'e2','counter'}
        assert list(payload['qualification_checks'])==['bad']
        assert [r['claim_id'] for r in payload['previous_verdicts']['verdicts']]==['bad']
        assert [r['claim_id'] for r in payload['validation_errors']]==['bad']
        value=verdicts(payload)
        changed=deepcopy(initial[0]['verdicts'][0]);changed['atoms'][0]['status']='insufficient'
        value['verdicts'].extend([changed,{'claim_id':'foreign','atoms':[]}])
        return value
    result=verify_claims(claims,evidence,judge,_repair=True)
    assert len(captured)==2
    assert [r['semantic_status'] for r in result]==['supported','supported']
    assert result[0]['semantic_verification']['atoms'][0]['source_quotes'][0]['quote']==TEXT


@pytest.mark.parametrize('mode',['missing','unknown','nonstring','duplicate','wrong_quote','incomplete'])
def test_failed_repair_never_changes_good_or_accepts_bad_claim(mode):
    captured=[]
    def judge(system,payload):
        captured.append(payload)
        if len(captured)==1:return verdicts(payload,bad={'bad'})
        value=verdicts(payload,bad={'bad'} if mode=='wrong_quote' else ())
        if mode=='missing':value['verdicts']=[]
        if mode=='unknown':value['verdicts'][0]['claim_id']='good'
        if mode=='nonstring':value['verdicts'][0]['claim_id']={'bad':'id'}
        if mode=='duplicate':value['verdicts']*=2
        if mode=='incomplete':value['verdicts'][0]['atoms'][0]['text']='Alice'
        return value
    result=verify_claims([claim('good','e1'),claim('bad','e2')],[source(),source('e2')],judge,_repair=True)
    assert len(captured)==2
    assert [r['semantic_status'] for r in result]==['supported','insufficient']


def test_exact_claim_and_finding_share_judge_work_but_keep_rows_ids_and_complete_audits():
    first=claim()
    finding={'finding_id':'f1','text_zh':TEXT,'evidence_id':'e1','actor':'alice@test','source_quote':TEXT}
    original=deepcopy([first,finding]);captured=[]
    def judge(system,payload):
        captured.append(payload)
        return verdicts(payload,bad={'c1'} if len(captured)==1 else ())
    result=verify_claims([first,finding],[source()],judge,_repair=True)
    assert [len(p['claims']) for p in captured]==[1,1]
    assert [r['semantic_verification']['claim_id'] for r in result]==['c1','f1']
    assert [r['semantic_verification']['judge_claim_id'] for r in result]==['c1','c1']
    assert result[0]['semantic_verification']['audit_binding']==result[1]['semantic_verification']['audit_binding']
    assert all(r['semantic_status']=='supported' for r in result)
    assert [first,finding]==original
    result[1]['semantic_verification']['atoms'][0]['source_quotes'][0]['quote']='changed'
    assert result[0]['semantic_verification']['atoms'][0]['source_quotes'][0]['quote']==TEXT


@pytest.mark.parametrize('extra',[
    {'actor':'bob@test'}, {'kind':'received'}, {'object_ref':'另一个报价'},
    {'conditions':['审批后生效']}, {'time_basis':'2026-10-10'},
    {'source_quote':'接受报价'}, {'evidence_ids':['e2']},
    {'counter_evidence_ids':['counter']},
    {'citations':[{'evidence_id':'e1','quote':TEXT,'char_start':10}]},
    {'claim_text':'Alice 同意报价。'},
])
def test_similar_claims_with_different_semantic_or_source_inputs_are_not_reused(extra):
    captured=[]
    def judge(system,payload):
        captured.append(payload);return verdicts(payload)
    verify_claims([claim(),claim('c2',**extra)],[source(),source('e2'),source('counter')],judge)
    assert len(captured[0]['claims'])==2


@pytest.mark.parametrize('field,value',[
    ('quote_text',TEXT+' 原始附注。'),('char_start',90),
    ('participants',[{'role':'from','address':'bob@test'}]),
    ('attribution','quoted'),('object_version','v2'),('message_key','changed'),
])
def test_audit_binding_changes_with_complete_original_source_snapshot(field,value):
    original=source();changed={**original,field:value};calls=[]
    def judge(system,payload):calls.append(payload);return verdicts(payload)
    old=verify_claims([claim()],[original],judge)[0]
    new=verify_claims([claim()],[changed],judge)[0]
    assert len(calls)==2  # No cross-call or cross-task verdict cache.
    assert old['semantic_verification']['audit_binding']!=new['semantic_verification']['audit_binding']


def test_audit_binding_changes_with_allowed_scope_history_and_counter_source():
    bindings=[]
    def run(**kwargs):
        row=verify_claims([claim(counter_evidence_ids=['counter'])],
                         [source(),source('counter',kwargs.pop('counter_text',TEXT))],
                         lambda s,p:verdicts(p),**kwargs)[0]
        bindings.append(row['semantic_verification']['audit_binding'])
    run();run(allowed_keys={'me1','mcounter'});run(reviews={'me1':{'complete':True}})
    run(counter_text='Alice 不接受报价。')
    assert len(set(bindings))==4


def test_reused_verdict_does_not_erase_missing_qualification_or_atomic_clause():
    text=TEXT+'\n须经审批后生效。'
    captured=[]
    def judge(system,payload):
        captured.append(payload);value=verdicts(payload)
        value['verdicts'][0]['qualification_reviews']=[]
        return value
    result=verify_claims([claim(),claim('c2')],[source(text=text)],judge,_repair=True)
    assert len(captured)==1 and len(captured[0]['claims'])==1
    assert len(captured[0]['qualification_checks']['c1'])==1
    assert all(r['semantic_status']=='insufficient' for r in result)
    assert all('qualification_scope_unproven' in r['semantic_verification']['rule_gaps'] for r in result)
    assert not any(verified_qualification_scope(r,{'e1':source(text=text)}) for r in result)


def test_reused_valid_qualification_audit_retains_original_tuple_binding():
    evidence=source(text=TEXT+'\n另一事项须经审批后生效。')
    result=verify_claims([claim(),claim('c2')],[evidence],lambda s,p:verdicts(p))
    assert all(verified_qualification_scope(r,{'e1':evidence}) for r in result)
    assert all(r['semantic_status']=='supported' for r in result)


def test_selective_repair_retains_valid_counter_verdict_and_semantic_disagreement():
    counter_quote='Alice 选择蓝色报价。'
    calls=[]
    def judge(system,payload):
        calls.append(payload);value=verdicts(payload,bad={'bad'} if len(calls)==1 else ())
        if len(calls)==1:
            for row in value['verdicts']:
                atom=row['atoms'][0]
                if row['claim_id']=='counter':
                    atom.update(status='contradicted',support_evidence_ids=[],counter_evidence_ids=['e2'],
                                source_quotes=[{'evidence_id':'e2','quote':counter_quote}])
                if row['claim_id']=='uncertain':
                    atom.update(status='insufficient',missing_requirements=['缺少明确回应。'])
        else:
            assert [c['claim_id'] for c in payload['claims']]==['bad']
        return value
    result=verify_claims([claim('counter','e2',claim_text='Alice 选择红色报价。',source_quote=counter_quote),
                         claim('uncertain'),claim('bad','e3')],
                         [source(),source('e2',counter_quote),source('e3')],judge,_repair=True)
    assert [r['semantic_status'] for r in result]==['contradicted','insufficient','supported']
    assert result[0]['semantic_verification']['atoms'][0]['status']=='contradicted'
    assert result[0]['semantic_verification']['atoms'][0]['counter_evidence_ids']==['e2']
    assert result[1]['semantic_verification']['atoms'][0]['missing_requirements']==['缺少明确回应。']


def test_duplicate_atomic_clause_omission_stays_insufficient_after_repair():
    compound=TEXT+'Alice 已付款。';calls=[]
    def judge(system,payload):
        calls.append(payload);value=verdicts(payload)
        value['verdicts'][0]['atoms'][0]['text']=TEXT
        return value
    result=verify_claims([claim(claim_text=compound),claim('c2',claim_text=compound)],
                         [source(text=compound)],judge,_repair=True)
    assert [len(p['claims']) for p in calls]==[1,1]
    assert all(r['semantic_status']=='insufficient' for r in result)
    assert all('atomic_claim_coverage_incomplete' in r['semantic_verification']['rule_gaps'] for r in result)


def test_ambiguous_claim_id_cannot_bind_two_different_claims_to_one_verdict():
    result=verify_claims([claim(),claim(actor='bob@test')],[source()],lambda s,p:verdicts(p),_repair=True)
    assert all(r['semantic_status']=='insufficient' for r in result)
    assert all('ambiguous_claim_id' in r['semantic_verification']['rule_gaps'] for r in result)


def test_judge_payload_mutations_cannot_change_original_claims_or_sources():
    claims=[claim()];evidence=[source()];saved=deepcopy([claims,evidence])
    def judge(system,payload):
        result=verdicts(payload)
        payload['claims'][0]['actor']='bob@test'
        payload['evidence'][0]['quote_text']='换掉原文'
        return result
    result=verify_claims(claims,evidence,judge)
    assert [claims,evidence]==saved
    assert result[0]['semantic_status']=='supported'
