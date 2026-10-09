"""Exact same-source text can repair an integer offset, never other citation defects."""
import pytest
from services.mail.research.claim_verifier import _locate_judge_quote, verify_claims

QUOTES=['邮件已收到。','关于贵司提出的财务审批流程及 10 月 30 日前到货的要求，我司已记录在案','稍后就相关事宜回复贵司。']
# Exact formal citation [0,99] from single-8-verified-result.json.
TEXT='陈经理：\n\n您好，邮件已收到。\n\n关于贵司提出的财务审批流程及 10 月 30 日前到货的要求，我司已记录在案，稍后就相关事宜回复贵司。\n\n感谢贵司的积极推进。\n\n王锐\n拓峰精密设备有限公司 销售部'


def test_three_actual_counting_errors_relocated_and_audited_with_strict_semantic_gates():
    assert len(TEXT)==99
    assert [TEXT.index(q) for q in QUOTES]==[9,17,56]
    source={'evidence_id':'e1','message_key':'m1','quote_text':TEXT,'char_start':0,
            'attribution':'authored','participants':[{'role':'from','address':'author@test'}]}
    claim={'claim_text':''.join(QUOTES),'actor':'author@test','evidence_ids':['e1']}
    def judge(system,payload):
        return {'verdicts':[{'claim_id':payload['claims'][0]['claim_id'],'atoms':[
            {'text':q,'status':'supported','support_evidence_ids':['e1'],
             'source_quotes':[{'evidence_id':'e1','quote':q,'char_start':offset}]}
            for q,offset in zip(QUOTES,[10,14,58])]}]}
    result=verify_claims([claim],[source],judge,allowed_keys={'m1'})[0]
    assert result['semantic_status']=='supported'
    quotes=[a['source_quotes'][0] for a in result['semantic_verification']['atoms']]
    assert [q['char_start'] for q in quotes]==[9,17,56]
    assert [q['model_char_start'] for q in quotes]==[10,14,58]
    assert all(q['offset_relocated'] for q in quotes)
    assert verify_claims([{**claim,'actor':'other@test'}],[source],judge)[0]['semantic_status']=='insufficient'
    assert verify_claims([claim],[source],judge,allowed_keys={'outside'})[0]['semantic_status']=='insufficient'


def test_repeated_exact_quote_retains_correct_explicit_occurrence_only():
    source={'quote_text':'收到邮件。收到邮件。','char_start':20}
    valid=_locate_judge_quote(source,{'quote':'收到邮件。','char_start':25})
    assert valid['char_start']==25 and not valid['offset_relocated']
    assert _locate_judge_quote(source,{'quote':'收到邮件。','char_start':21}) is None
    assert _locate_judge_quote(source,{'quote':'收到邮件。'}) is None


@pytest.mark.parametrize('offset',[True,False,'9',9.0,None])
def test_bad_explicit_position_types_rejected_even_when_quote_is_unique(offset):
    assert _locate_judge_quote({'quote_text':TEXT,'char_start':0},
                              {'quote':QUOTES[0],'char_start':offset}) is None


def test_noncontiguous_modified_or_other_source_text_not_repaired():
    source={'quote_text':TEXT,'char_start':0}
    assert _locate_judge_quote(source,{'quote':'邮件…收到。','char_start':10}) is None
    assert _locate_judge_quote(source,{'quote':'邮件已经收到。','char_start':10}) is None
    assert _locate_judge_quote({'quote_text':'其他邮件正文','char_start':0},
                              {'quote':QUOTES[0],'char_start':10}) is None


def test_nonzero_source_base_and_missing_offset_are_located_exactly():
    source={'quote_text':TEXT,'char_start':100}
    quote=_locate_judge_quote(source,{'quote':QUOTES[0],'char_start':10})
    assert quote['char_start']==109 and quote['model_char_start']==10
    missing=_locate_judge_quote(source,{'quote':QUOTES[0]})
    assert missing['char_start']==109 and missing['model_char_start'] is None


def test_wrong_evidence_id_never_searches_a_different_source():
    sources=[{'evidence_id':'e1','message_key':'m1','quote_text':QUOTES[0],'attribution':'authored'},
             {'evidence_id':'e2','message_key':'m2','quote_text':'毫不相关正文','attribution':'authored'}]
    claim={'claim_text':QUOTES[0],'evidence_ids':['e1','e2']}
    def judge(system,payload):
        return {'verdicts':[{'claim_id':payload['claims'][0]['claim_id'],'atoms':[
            {'text':QUOTES[0],'status':'supported','support_evidence_ids':['e2'],
             'source_quotes':[{'evidence_id':'e2','quote':QUOTES[0],'char_start':10}]}]}]}
    result=verify_claims([claim],sources,judge)[0]
    assert result['semantic_status']=='insufficient'
    assert 'judge_quote_not_exact' in result['semantic_verification']['missing_requirements']


@pytest.mark.parametrize('claim_text',['我接受报价90000元。','如果审批通过，我接受报价95000元。'])
def test_offset_repair_does_not_override_missing_conditions_or_wrong_amount(claim_text):
    quote='如果审批通过，我接受报价90000元。'
    source={'evidence_id':'e1','message_key':'m1','quote_text':quote,'char_start':0,'attribution':'authored'}
    claim={'claim_text':claim_text,'evidence_ids':['e1']}
    def judge(system,payload):
        return {'verdicts':[{'claim_id':payload['claims'][0]['claim_id'],'atoms':[
            {'text':claim_text,'status':'supported','support_evidence_ids':['e1'],
             'source_quotes':[{'evidence_id':'e1','quote':quote,'char_start':9}]}]}]}
    result=verify_claims([claim],[source],judge)[0]
    assert result['semantic_status']=='insufficient'
    assert result['semantic_verification']['atoms'][0]['source_quotes'][0]['offset_relocated']
