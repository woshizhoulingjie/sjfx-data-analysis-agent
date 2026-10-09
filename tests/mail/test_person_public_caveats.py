from services.mail.person_research import public_finding_uncertainty, public_context_review, public_person_finding
from services.mail.person_research import person_output_schema, CONTEXT_PUBLIC_NOTE


def test_attachment_draft_cannot_claim_actual_signature_verification():
    finding={'semantic_status':'insufficient','review_status':'supported',
             'uncertainty':'签字内容本身是客观事实',
             'citations':[{'source_kind':'attachment','quote':'签章：李某'}]}
    text=public_finding_uncertainty(finding)
    assert '仍待核实' in text and '不能验证实际签章' in text
    assert '客观事实' not in text
    assert finding['uncertainty']=='签字内容本身是客观事实'


def test_supported_historical_expression_preserves_reality_limit():
    text=public_finding_uncertainty({'semantic_status':'supported','review_status':'supported'})
    assert '历史表达' in text and '不能独立证明' in text


def test_condition_and_hypothesis_remain_explicit():
    assert '条件限制' in public_finding_uncertainty({'semantic_status':'supported','review_status':'conditional'})
    assert '待核实的意图假设' in public_finding_uncertainty({'kind':'hypothesis','semantic_status':'supported','review_status':'supported'})


def test_future_delivery_draft_is_not_presented_as_source_explanation():
    review={'finding_id':'f1','status':'unresolved','rationale':'10月21日表明实际未交付',
            'conditions':['另一项目的财务审批条件'],'contradictions':['现实未完成'],
            'unresolved_questions':['10月21日是否已证明未交付'],
            'counterevidence':[{'quote':'不同意延长交期，须按原定日期处理。','sent_at':'2026-10-08'}]}
    public=public_context_review(review)
    assert '10月21日' not in str(public) and '财务审批' not in str(public)
    assert public['counterevidence']==review['counterevidence']
    assert '不同意延长交期' in public['contradictions'][0]
    assert '不同意延长交期' in public['conditions'][0] and '是否适用待核实' in public['conditions'][0]
    assert review['rationale']=='10月21日表明实际未交付'


def test_incomplete_compound_claim_does_not_publish_its_auxiliary_assertions():
    finding={'statement':'蓝湾10月15日发运','semantic_status':'insufficient','review_status':'supported',
             'conditions':['海星采购须经财务审批'],'contradictions':['已证明现实未交付']}
    public=public_person_finding(finding)
    assert public['review_status']=='unresolved' and public['conditions']==public['contradictions']==[]
    assert public['statement']==finding['statement'] and finding['conditions']==['海星采购须经财务审批']


def test_context_decoder_does_not_generate_an_unaudited_fulfillment_narrative():
    schema=person_output_schema('reviews',[{'evidence_id':'q1'}],[{'finding_id':'f1'}])
    assert schema['properties']['reviews']['items']['properties']['rationale']=={'type':'string','enum':[CONTEXT_PUBLIC_NOTE]}
