"""Compound conditional expressions keep clause-local gates and global safeguards."""
import pytest
from services.mail.research.claim_verifier import verify_claims, _requires_full_history

FIRST = '我司原则同意 90,000.00 元/台的调整后报价'
SECOND = '但请注意：该同意附有明确条件，并非最终确认。'
CONDITION = '若财务审批未通过，回归 95,000.00 元/台继续协商。'
TEXT = FIRST + '，' + SECOND


def audit(*, actor='alice@example.test', omit_condition=False, bad_second=False, empty=False,
          omit_qualification_review=False):
    source = {'evidence_id':'e1','message_key':'m1','quote_text':TEXT + CONDITION,
              'char_start':0,'attribution':'authored',
              'participants':[{'role':'from','address':'alice@example.test'}]}
    claim = {'claim_text':FIRST if omit_condition else TEXT,'actor':actor,
             'evidence_ids':['e1'],'conditions':[] if omit_condition else [CONDITION]}
    def judge(system, payload):
        atoms = [{'text':FIRST,'status':'supported','support_evidence_ids':['e1'],
                  'source_quotes':[] if empty else [{'evidence_id':'e1','quote':FIRST}]}]
        if not omit_condition:
            atoms.append({'text':SECOND,'status':'supported','support_evidence_ids':['e1'],
                          'source_quotes':[{'evidence_id':'e1','quote':SECOND + '伪造' if bad_second else SECOND}]})
        cid = payload['claims'][0]['claim_id']
        reviews = [] if omit_qualification_review else [
            {**q, 'applicability':'applicable',
             'rationale':'原文条件约束同一报价；判断完整保留财务审批与恢复协商条件。'}
            for q in payload['qualification_checks'][cid]
        ]
        return {'verdicts':[{'claim_id':cid,'atoms':atoms,'qualification_reviews':reviews}]}
    return verify_claims([claim],[source],judge,allowed_keys={'m1'})[0]


def test_condition_numbers_not_copied_into_each_atomic_quote():
    result = audit()
    assert result['semantic_status'] == 'supported'
    assert all(atom['status']=='supported' for atom in result['semantic_verification']['atoms'])
    assert result['semantic_verification']['temporal_status']=='historical_expression'


def test_missing_excerpt_still_requires_full_source_qualification_review():
    result = audit(omit_qualification_review=True)
    assert result['semantic_status'] == 'insufficient'
    assert 'qualification_scope_unproven' in result['semantic_verification']['missing_requirements']


@pytest.mark.parametrize('options,gap',[
    ({'actor':'bob@example.test'},'actor_source_role_mismatch'),
    ({'omit_condition':True},'number_time_negation_or_condition_mismatch'),
    ({'bad_second':True},'judge_quote_not_exact'),
    ({'empty':True},'verdict_missing_exact_support'),
])
def test_conditional_clause_fix_keeps_deterministic_safeguards(options,gap):
    result = audit(**options)
    assert result['semantic_status']=='insufficient'
    assert gap in result['semantic_verification']['missing_requirements']


@pytest.mark.parametrize('text',[
    '并非最终确认。','本函不构成我方对合同价格的最终确认。',
    'This is not a final confirmation.',
])
def test_denial_of_final_confirmation_is_local_expression(text):
    assert not _requires_full_history(text)


@pytest.mark.parametrize('text',[
    '当前并非最终确认。','双方最终确认报价。','最后一次回应接受报价。',
    'This is the final confirmation.','The latest position is acceptance.',
])
def test_actual_global_temporal_assertions_still_require_history(text):
    assert _requires_full_history(text)
