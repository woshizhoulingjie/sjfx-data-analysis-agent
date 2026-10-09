"""Auxiliary assertions cannot bypass the atomic source audit of a finding."""
from test_person_context_upgrade import person_data, citation
from services.mail.person_corpus import resolve_selection, next_chunks
from services.mail.person_research import validate_findings
from services.mail.issue_repository import audit_workflow_findings


def _finding(repo,pid,statement,conditions):
    selection=resolve_selection(repo,pid,'owner','alice@example.com')
    chunks,_=next_chunks(repo,pid,'owner',selection,[0,0])
    rows,rejected=validate_findings({'findings':[{'kind':'goal','statement':statement,
        'conditions':conditions,'citations':[citation(chunks[0])]}]},chunks)
    assert not rejected
    rows[0]['finding_id']='compound-finding'
    return rows,selection


def _judge(main_only=False):
    def judge(system,payload):
        claim=payload['claims'][0];source=payload['evidence'][0]
        text=claim['claim_text'].split('\n')[0] if main_only else claim['claim_text']
        return {'verdicts':[{'claim_id':claim['claim_id'],'atoms':[{'text':text,
            'status':'supported','support_evidence_ids':[source['evidence_id']],
            'source_quotes':[{'evidence_id':source['evidence_id'],'quote':source['quote_text']}]}],
            'qualification_reviews':[{**q,'applicability':'applicable','rationale':'同一原文已明确保留审批限制'}
                for q in payload['qualification_checks'][claim['claim_id']]]}]}
    return judge


def test_supported_main_sentence_cannot_skip_another_projects_condition(person_data):
    repo,pid,add=person_data
    main='We plan shipment for Blue Bay on October 15, 2026.'
    add(main)
    findings,selection=_finding(repo,pid,main,['海星采购须经财务审批。'])
    result=audit_workflow_findings(repo,{'package_id':pid},'owner',findings,_judge(main_only=True),selection=selection)[0]
    assert result['semantic_status']=='insufficient'
    assert 'atomic_claim_coverage_incomplete' in result['semantic_verification']['missing_requirements']
    assert '海星采购' in result['semantic_verification']['audited_claim_text']
    assert result['semantic_verification']['audited_fields'][1]['field']=='conditions'


def test_complete_same_source_condition_is_audited_without_rewriting_it(person_data):
    repo,pid,add=person_data
    main='We plan to ship this order.';condition='Shipment is subject to written approval.'
    add(main+' '+condition)
    findings,selection=_finding(repo,pid,main,[condition])
    result=audit_workflow_findings(repo,{'package_id':pid},'owner',findings,_judge(),selection=selection)[0]
    assert result['semantic_status']=='supported'
    assert result['conditions']==[condition] and result['statement']==main
    assert result['semantic_verification']['audited_claim_text']==main+'\n'+condition
