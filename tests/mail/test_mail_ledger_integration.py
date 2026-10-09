"""Actual local SQLite workflows with synthetic mail and an explicit mock judge."""
import json
from unittest.mock import patch

from test_mail_intent_workflows import corpus,create,finish
from test_person_context_upgrade import person_data,create_analysis,finish as finish_person,decode_person_source_payload
from services.mail.issue_repository import IssueRepository,ledger_dependency,workflow_scope
from services.mail.analysis_api import make_spec
from services.mail.analysis import VERSION


class LedgerModel:
    def __init__(self):self.calls=[]
    def chat_json(self,system,prompt,**kwargs):
        p=decode_person_source_payload(json.loads(prompt));self.calls.append(p)
        if 'claims' in p:
            sources={e['evidence_id']:e for e in p['evidence']}
            return {'usage':{'input_tokens':100,'output_tokens':60},'json':{'verdicts':[
                {'claim_id':c['claim_id'],'atoms':[{'text':c['claim_text'],'status':'supported',
                 'support_evidence_ids':c['evidence_ids'],'counter_evidence_ids':[],
                 'source_quotes':[{'evidence_id':e,'quote':sources[e]['quote_text'],'char_start':sources[e]['char_start']} for e in c['evidence_ids']],
                 'missing_requirements':[]}]} for c in p['claims']]}}
        if 'alignments' in p:
            decisions=[]
            for entry in p['alignments']:
                source=entry['source'];target=entry['candidates'][0]
                decisions.append({'source_act_id':source['act_id'],'target_issue_id':target['issue_id'],
                    'decision':'same_issue','review_status':'supported','method':'semantic_support_checked',
                    'reason':'Both originals name the same contract object; versions remain separate.',
                    'evidence_ids':source['evidence_ids']+target['acts'][0]['evidence_ids']})
            return {'json':{'decisions':decisions}}
        if 'chunks' in p:
            findings=[]
            selected=p.get('selected_addresses')
            for c in p['chunks']:
                sender=next(x['address'] for x in c['participants'] if x['role']=='from')
                text=c['text'];act='reject' if 'reject' in text else 'withdraw' if 'withdraw' in text else 'accept' if 'accept' in text else 'propose'
                version='v2' if 'v2' in text else 'v1'
                received=bool(selected and sender not in selected)
                actor=selected[0] if received else sender
                findings.append({'actor':actor,'kind':'received' if received else 'action','act_type':act,'intent':act,
                    'issue':'Contract','object_identity':'contract','object_version':version,'statement':text.strip(),
                    'uncertainty':'Mail expressions require source review.','conditions':['approval'] if 'approval' in text else [],
                    'contradictions':[],'unresolved_questions':[],
                    'citations':[{'chunk_id':c['chunk_id'],'quote':text,'char_start':c['char_start']}]})
            return {'json':{'findings':findings}}
        if 'context_chunks' in p:
            rows=p.get('findings') or p['provisional_findings']
            return {'json':{'reviews':[{'finding_id':f['finding_id'],'status':'conditional' if f['conditions'] else 'supported',
                'rationale':'Checked only supplied sources.','conditions':f['conditions'],'contradictions':[],'unresolved_questions':[],
                'citations':[{'chunk_id':c['chunk_id'],'quote':c['text'],'char_start':c['char_start']} for c in p['context_chunks']],
                'counterevidence':[],'later_replies':[]} for f in rows]}}
        if 'findings' in p:
            return {'json':{'issues':[{'title':'Contract expressions','statement':'The quoted participants discuss versions and conditions.',
                'uncertainty':'Real-world completion is unverified.','finding_ids':[f['finding_id'] for f in p['findings'][:8]]}]}}
        return {'json':{'themes':[{'title':'Contract expressions','statement':'The selected mailbox expressed the quoted contract positions.',
            'uncertainty':'Real-world completion is unverified.','finding_ids':[f['finding_id'] for f in p['evidence'][:6]]}]}}


def test_pair_read_review_alignment_summary_write_shared_ledger(corpus):
    repo,pid,add=corpus
    add('I propose contract v1.')
    add('I reject contract v1.',sender='bob@example.com',to='alice@example.com')
    add('I accept contract v2 after approval.',sender='bob@example.com',to='alice@example.com')
    tid,spec=create(repo,pid,'pair',address='alice@example.com',counterpart='bob@example.com')
    model=LedgerModel();out=finish(repo,tid,model)
    assert out['progress']['complete']
    assert not out['progress'].get('ledger_publication_failures')
    assert len(out['shared_issues']['items'])==1
    states=out['shared_issues']['items'][0]['actor_states']
    assert {(s['actor'],s['object_version']):s['expressed_status'] for s in states}=={
        ('alice@example.com','v1'):'proposed',('bob@example.com','v1'):'rejected',('bob@example.com','v2'):'conditional_accept'}
    assert all(s['real_world_completion']=='unverified' for s in states)
    assert any('alignments' in p for p in model.calls)
    assert any('claims' in p for p in model.calls)
    assert out['progress']['token_usage']['actual_tokens']==160
    assert out['progress']['ledger_dependency']==ledger_dependency(repo,pid,'owner',workflow_scope(spec))
    with repo._connection() as c:
        tools={r[0] for r in c.execute('SELECT tool_name FROM mail_research_steps WHERE turn_id=?',(tid,))}
    assert {'dossier_findings','dossier_verified','dossier_aligned','dossier_summary'}<=tools


def test_person_read_audit_alignment_and_summary_share_same_issue(person_data):
    repo,pid,add=person_data
    add('I propose contract v1.')
    add('I accept contract v1.',date='Wed, 08 May 2024 15:38:50 +0700')
    tid=create_analysis(repo,pid);model=LedgerModel();out=finish_person(repo,tid,model)
    assert out['progress']['complete']
    assert not out['progress'].get('ledger_publication_failures')
    assert len(out['shared_issues']['items'])==1
    assert all(f['act_id'] and f['issue_id'] and f['semantic_status']=='supported' for f in out['items'])
    assert out['progress']['synthesis']['themes'][0]['issue_ids']
    assert out['progress']['ledger_dependency']
    assert out['progress']['token_usage']['actual_tokens']==160


def test_shared_publication_failure_preserves_workflow_with_gap(corpus):
    repo,pid,add=corpus;key=add('I propose contract v1.')
    tid,_=create(repo,pid,'message',message_key=key)
    with patch.object(IssueRepository,'_evidence',side_effect=ValueError('synthetic side-index failure')):
        out=finish(repo,tid,LedgerModel())
    assert out['progress']['complete'] and out['sufficiency']=='partial'
    assert out['progress']['ledger_publication_failures']
    assert out['batches'][0]['findings']
    assert not out['shared_issues']['items']
    assert any('写入失败' in gap for gap in out['progress']['limitations'])


def test_issue_and_as_of_scope_do_not_expand_pair_analysis(corpus):
    repo,pid,add=corpus
    first=add('I propose contract v1.');later=add('I accept contract v2.')
    tid,_=create(repo,pid,'pair',address='alice@example.com',counterpart='bob@example.com')
    out=finish(repo,tid,LedgerModel());issue=out['shared_issues']['items'][0]['issue_id']
    spec,_=make_spec(repo,pid,'owner',{'kind':'pair','address':'alice@example.com','counterpart':'bob@example.com',
        'scope':{'issue_id':issue,'as_of':'2026-09-01T23:59:59+00:00'}})
    assert spec['message_keys']==[first] and later not in spec['context_keys']
    assert spec['engine_version']==VERSION


def test_token_budget_stops_before_model_without_erasing_cursor(corpus):
    repo,pid,add=corpus;key=add('I propose contract v1.')
    tid,_=create(repo,pid,'message',message_key=key,max_tokens=1)
    model=LedgerModel();out=finish(repo,tid,model)
    assert not model.calls
    assert out['progress']['token_budget_exhausted'] and out['progress']['budget_exhausted']
    assert not out['progress']['complete'] and out['progress']['cursor']==[0,0]


def test_forced_identical_workflow_republishes_without_permanent_partial_gap(corpus):
    repo,pid,add=corpus;key=add('I propose contract v1.')
    tid,_=create(repo,pid,'message',message_key=key)
    first=finish(repo,tid,LedgerModel())
    with repo._connection() as c:
        before=c.execute('SELECT COUNT(*) FROM mail_semantic_generations').fetchone()[0]
    second_tid,_=create(repo,pid,'message',message_key=key,force=True)
    second=finish(repo,second_tid,LedgerModel())
    assert first['progress']['complete'] and second['progress']['complete']
    assert not second['progress'].get('ledger_publication_failures')
    assert second['shared_issues']['items'][0]['issue_id']==first['shared_issues']['items'][0]['issue_id']
    assert second['sufficiency']=='sufficient'
    with repo._connection() as c:
        assert c.execute('SELECT COUNT(*) FROM mail_semantic_generations').fetchone()[0]>before
        assert c.execute('SELECT COUNT(*) FROM mail_evidence').fetchone()[0]==1


def test_forced_pair_republishes_checked_links_after_new_act_generation(corpus):
    repo,pid,add=corpus
    add('I propose contract v1.');add('I accept contract v1.',sender='bob@example.com',to='alice@example.com')
    kwargs={'address':'alice@example.com','counterpart':'bob@example.com'}
    tid,_=create(repo,pid,'pair',**kwargs);first=finish(repo,tid,LedgerModel())
    assert len(first['shared_issues']['items'])==1
    tid,_=create(repo,pid,'pair',force=True,**kwargs);second=finish(repo,tid,LedgerModel())
    assert second['progress']['complete'] and not second['progress'].get('ledger_publication_failures')
    assert len(second['shared_issues']['items'])==1
    assert second['shared_issues']['items'][0]['issue_id']==first['shared_issues']['items'][0]['issue_id']
    assert second['shared_issues']['items'][0]['act_count']==2
