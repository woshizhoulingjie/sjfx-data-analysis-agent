"""Question-focused analysis keeps qualifications without unrelated read amplification."""
from copy import deepcopy
import json
from types import SimpleNamespace

from services.mail.research.evidence_packing import model_source, pack_evidence_batches
from services.mail.research.planner import SYSTEM as PLAN_SYSTEM, validate_plan
from services.mail.research.relevance import SYSTEM as RELEVANCE_SYSTEM
from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.verifier import verified_qualification_scope


def source(eid, key, text, **extra):
    return {'evidence_id':eid,'message_key':key,'quote_text':text,'source_kind':'body',
        'mime_path':'1','char_start':0,'char_end':len(text),'attribution':'authored',
        'body_fragments':[{'fragment_kind':'authored','char_start':0,'char_end':len(text)}],
        'participants':[{'role':'from','normalized_address':'alice@example.com'}],**extra}


def review(key, rows, **extra):
    return {'message_key':key,'message_keys':list(dict.fromkeys(r['message_key'] for r in rows)),
        'evidence_ids':[r['evidence_id'] for r in rows],'change_evidence_ids':[],
        'complete':True,'gaps':[],**extra}


def test_rejected_unrelated_context_does_not_reenter_but_thread_counter_and_unknown_do():
    agreed=source('agreed','retained','项目甲约定期限为10月18日。')
    old=source('old','rejected-old','项目甲原拟10月15日。')
    counter=source('counter','rejected-counter','项目甲尚未交付，未验收。',role='counter')
    unknown=source('unknown','unknown','项目乙的交付事项待核查。')
    unrelated=source('unrelated','rejected-other','其他项目采购19台设备。')
    rows=[agreed,old,counter,unknown,unrelated]
    reviews={'retained':review('retained',rows[:3],change_evidence_ids=['counter']),
        'unknown':review('unknown',[unknown],complete=False,gaps=['missing_reply_target']),
        'rejected-other':review('rejected-other',[unrelated])}
    state={'phase2':True,'context_evidence':deepcopy(rows),'context_reviews':deepcopy(reviews),
        'context_gaps':[],'analysis_anchor_keys':['retained','unknown']}
    runtime=MailResearchRuntime(None)
    result=runtime._contextual_evidence(state,[agreed,unknown,unrelated])
    assert {r['evidence_id'] for r in result}=={'agreed','old','counter','unknown'}
    assert set(state['context_reviews'])=={'retained','unknown'}
    assert 'missing_reply_target' in ' '.join(state['context_gaps'])
    assert state['context_review_ledger']==reviews
    assert state['context_evidence']==rows
    # A later gap query may make an earlier rejected root relevant; its full
    # previously read source can be used without fabricating another read.
    state['analysis_anchor_keys'].append('rejected-other')
    again=runtime._contextual_evidence(state,result+[unrelated])
    assert {r['evidence_id'] for r in again}=={r['evidence_id'] for r in rows}
    assert state['context_reviews']['unknown']['complete'] is False


def test_no_retained_roots_cannot_restore_rejected_read_sources():
    row=source('other','m','其他项目交付。')
    state={'phase2':True,'analysis_anchor_keys':[],'context_reviews':{'m':review('m',[row])},
           'context_evidence':[row],'context_gaps':[]}
    assert MailResearchRuntime(None)._contextual_evidence(state,[row])==[]
    assert state['context_review_ledger']['m']['evidence_ids']==['other']


def test_exhaustive_and_unclassified_context_is_preserved():
    rows=[source(str(i),'m'+str(i),'全文'+str(i)) for i in range(50)]
    state={'phase2':True,'context_reviews':{},'context_evidence':rows}
    assert len(MailResearchRuntime(None)._contextual_evidence(state,[]))==50


def test_planning_and_relevance_distinguish_deadline_from_actual_completion():
    plan=validate_plan({'goal':'证明已经交付','criteria':['已完成交付','排除延期通知'],
                        'queries':['项目甲']},'项目甲的最终交付是哪一天',{})
    assert plan['relevance_policy']['criteria_mode']=='any_answer_bearing_role'
    assert plan['relevance_policy']['deadline_is_not_completion']
    assert plan['relevance_policy']['roles']==['support','counter','condition','update']
    assert 'Never require completion' in PLAN_SYSTEM
    assert 'generated criteria are search' in RELEVANCE_SYSTEM
    assert 'non-completion reports remain relevant' in RELEVANCE_SYSTEM
    # Explicit user restrictions remain authoritative, not widened by a plan.
    scope={'message_keys':['m'],'date_to':'2026-10-09','include_explicit_attachments':False}
    assert validate_plan({},'只查本邮件的实际完成日期',scope)['scope']==scope


def test_compact_prompt_keeps_literal_content_roles_conditions_and_full_server_ledger():
    text='项目甲尚未验收。\n若双方签署验收报告，才算交付。'
    row=source('e','m',text,conditions=['双方签署验收报告'],role='counter',object_version='v3',
        source_sha256='sha',text_sha256='text-sha',source_revision='r3',index_epoch=3,
        source_aliases=[{'evidence_id':'alias-'+str(i),'context_anchor':'anchor-'+str(i)} for i in range(100)])
    row['body_fragments'].append({'fragment_kind':'quoted','char_start':5000,'char_end':7000})
    before=deepcopy(row)
    full=model_source(row);compact=model_source(row,compact=True)
    assert row==before and full['source_aliases']==row['source_aliases']
    for key in ('quote_text','char_start','char_end','conditions','role','object_version','source_revision','index_epoch','participants'):
        assert compact[key]==full[key]
    assert compact['body_fragments']==full['body_fragments'][:1]
    assert 'source_aliases' not in compact and 'text_sha256' not in compact
    assert len(json.dumps(compact,ensure_ascii=False))*4<len(json.dumps(full,ensure_ascii=False))
    _,old=pack_evidence_batches([full],12000,max_chars=2000)
    _,new=pack_evidence_batches([compact],12000,max_chars=2000)
    assert not old['synthesis_complete'] and new['synthesis_complete']


def test_model_projection_does_not_weaken_full_source_qualification_binding():
    text='我确认项目甲交付期限为2026年10月18日。'
    row=source('e','m',text,source_aliases=[{'evidence_id':'alias-'+str(i)} for i in range(100)],
        source_sha256='server-hash',text_sha256='text-hash',source_revision='r1')
    before=deepcopy(row);prompts=[];saved=[]
    repo=SimpleNamespace(event=lambda *a:None,record_research_step=lambda *a,**k:None,
        record_claims=lambda t,rows:saved.extend(rows),replace_turn_relations=lambda *a:a[-1])
    runtime=MailResearchRuntime(repo)
    def call(t,o,state,purpose,system,prompt,**kwargs):
        payload=json.loads(prompt);prompts.append((purpose,payload))
        if purpose=='evidence_analysis':
            return {'answer':text,'claims':[{'claim_text':text,'subject_ref':'项目甲','object_ref':'项目甲',
                'actor':'alice@example.com','source_quote':text,'evidence_ids':['e'],'predicate':'confirmed'}], 'findings':[]}
        return {'verdicts':[{'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'supported',
            'support_evidence_ids':['e'],'source_quotes':[{'evidence_id':'e','quote':text}],
            'counter_evidence_ids':[],'missing_requirements':[]}],
            'qualification_reviews':[]} for c in payload['claims']]}
    runtime._model_call=call
    state={'phase2':True,'analysis_truncated':False,'analysis_completed':False,'findings':[],
        'retrieval_gaps':[],'context_reviews':{'m':review('m',[row])},'allowed_keys':{'m'}}
    checked,_=runtime._analyze_evidence('t','o',state,{'objective':'项目甲约定期限','budget':{'max_context_tokens':12000}},'p',[row])
    assert row==before and len(checked)==1
    assert checked[0]['semantic_status']=='supported'
    assert verified_qualification_scope(checked[0],{'e':row})
    for purpose,payload in prompts:
        assert payload['evidence'][0]['quote_text']==text
        assert 'source_aliases' not in payload['evidence'][0]
        assert 'source_sha256' not in payload['evidence'][0]
    assert state['evidence_packing']['synthesis_complete'] and saved
