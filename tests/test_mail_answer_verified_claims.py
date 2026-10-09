"""Short findings do not hide verified answers to other subquestions."""
import time
from services.mail.research.runtime import MailResearchRuntime


def answer(findings,claims):
    state={'findings':findings,'deadline_reached':False,'tool_limit_reached':False}
    return MailResearchRuntime(None)._answer({'mode':'research'},[{'evidence_id':'e'}],{},
        {'matched':1},time.monotonic(),state,claims)


def test_pending_short_finding_cannot_hide_verified_contract_and_addition_terms():
    pending={'text_zh':'待核实：先前报价的条件。','status':'needs_review'}
    verified=[{'claim_text':'邮件明确原合同已生效。','support_status':'supported','state':'asserted'},
              {'claim_text':'追加金额已确认，交期仍待协商。','support_status':'supported','state':'asserted'}]
    result=answer([pending],verified)
    assert all(row['claim_text'] in result['answer'] for row in verified)
    assert pending['text_zh'] in result['answer'] and result['findings'][0]['status']=='needs_review'


def test_unsupported_claim_not_added_to_answer_or_promoted():
    result=answer([{'text_zh':'已收到邮件。'}],[{'claim_text':'双方已验收。','state':'asserted','support_status':'insufficient'}])
    assert '双方已验收' not in result['answer']


def test_all_bounded_verified_claims_remain_and_exact_duplicates_appear_once():
    claims=[{'claim_text':'核验事项%s。'%i,'support_status':'supported','state':'asserted'} for i in range(8)]
    result=answer([{'text_zh':'核验事项0。'}],claims)
    assert all(result['answer'].count(row['claim_text'])==1 for row in claims)
