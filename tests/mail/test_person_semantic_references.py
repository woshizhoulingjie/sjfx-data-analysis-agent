import copy
import pytest
from services.mail.person_semantic import reference_payload,expand_reference_verdicts,reference_schema
from services.mail.research.claim_verifier import verify_claims


def fixture_payload():
    return {'claims':[{'claim_id':'c1','claim_text':'请在10月18日前提供材料','evidence_ids':['e1']}],
        'evidence':[{'evidence_id':'e1','quote_text':'请在10月16日前提供材料。\n须经书面批准。','char_start':50,'attribution':'authored'}],
        'qualification_checks':{'c1':[{'evidence_id':'e1','quote':'须经书面批准。','char_start':67}]}}


def response(ref='s1'):
    return {'verdicts':[{'claim_id':'c1','atoms':[{'text':'请在10月18日前提供材料','status':'supported',
        'source_refs':[{'ref_id':ref,'role':'support'}],'missing_requirements':[]}],
        'qualification_reviews':[{'check_id':'q1','applicability':'unresolved','rationale':'批准范围待核实'}]}]}


def test_expansion_preserves_original_bytes_offsets_and_qualification_tuple():
    original=fixture_payload();saved=copy.deepcopy(original)
    model,refs,checks=reference_payload(original)
    raw=response();expanded=expand_reference_verdicts(raw,refs,checks)
    atom=expanded['verdicts'][0]['atoms'][0]
    assert atom['source_quotes']==[{'evidence_id':'e1','quote':original['evidence'][0]['quote_text'],'char_start':50}]
    assert atom['support_evidence_ids']==['e1'] and atom['counter_evidence_ids']==[]
    review=expanded['verdicts'][0]['qualification_reviews'][0]
    assert all(review[k]==v for k,v in original['qualification_checks']['c1'][0].items())
    assert review['applicability']=='unresolved' and original==saved and 'source_quotes' not in raw['verdicts'][0]['atoms'][0]
    schema=reference_schema(model,refs,checks)
    variant=schema['properties']['verdicts']['items']['oneOf'][0]
    assert variant['properties']['claim_id']['enum']==['c1']
    assert variant['properties']['qualification_reviews']['required']==['q1']


@pytest.mark.parametrize('mutation',[lambda v:v['verdicts'][0]['atoms'][0]['source_refs'][0].update(ref_id='unknown'),
    lambda v:v['verdicts'][0]['atoms'][0]['source_refs'][0].update(quote='伪造引文'),
    lambda v:v['verdicts'][0]['atoms'][0].update(source_quotes=[]),
    lambda v:v['verdicts'][0]['qualification_reviews'][0].update(check_id='unknown'),
    lambda v:v['verdicts'][0].update(claim_id='other'),
    lambda v:v['verdicts'][0]['qualification_reviews'][0].update(char_start=0)])
def test_unknown_conflicting_and_cross_claim_references_are_rejected(mutation):
    _,refs,checks=reference_payload(fixture_payload());value=response();mutation(value)
    with pytest.raises(ValueError):expand_reference_verdicts(value,refs,checks)


def test_real_wrong_date_remains_negative_after_exact_reference_expansion():
    original=fixture_payload();_,refs,checks=reference_payload(original)
    result=verify_claims(original['claims'],original['evidence'],lambda system,payload:expand_reference_verdicts(response(),refs,checks))
    assert result[0]['semantic_status']=='insufficient'
    assert any('mismatch' in g for g in result[0]['semantic_verification']['missing_requirements'])


def test_missing_qualification_is_not_made_complete_by_registry():
    original=fixture_payload();_,refs,checks=reference_payload(original)
    value=response();value['verdicts'][0]['qualification_reviews']=[]
    expanded=expand_reference_verdicts(value,refs,checks)
    assert expanded['verdicts'][0]['qualification_reviews']==[]
    result=verify_claims(original['claims'],original['evidence'],lambda system,payload:expanded)[0]
    assert 'qualification_scope_unproven' in result['semantic_verification']['missing_requirements']
    assert result['semantic_verification']['qualification_scope']['complete'] is False


def test_clock_number_cannot_support_wrong_partial_deadline():
    source={'evidence_id':'e1','quote_text':'请于10月16日18:30前提供材料。','char_start':0,'attribution':'authored'}
    claim={'claim_id':'c1','claim_text':'请于10月18日18:30前提供材料','evidence_ids':['e1']}
    def judge(system,payload):
        _,refs,checks=reference_payload(payload)
        value={'verdicts':[{'claim_id':'c1','atoms':[{'text':claim['claim_text'],'status':'supported',
            'source_refs':[{'ref_id':'s1','role':'support'}],'missing_requirements':[]}],'qualification_reviews':[]}]}
        return expand_reference_verdicts(value,refs,checks)
    result=verify_claims([claim],[source],judge)[0]
    assert result['semantic_status']=='insufficient'
    assert 'atomic_number_time_negation_or_condition_mismatch' in result['semantic_verification']['missing_requirements']


def test_guided_judgment_cannot_select_another_claims_source_and_maps_all_conditions():
    original=fixture_payload()
    original['claims'].append({'claim_id':'c2','claim_text':'另一邮件','evidence_ids':['e2']})
    original['evidence'].append({'evidence_id':'e2','quote_text':'另一邮件原文','char_start':0})
    model,refs,checks=reference_payload(original);schema=reference_schema(model,refs,checks)
    first,second=schema['properties']['verdicts']['items']['oneOf']
    first_ids=first['properties']['atoms']['items']['properties']['source_refs']['items']['properties']['ref_id']['enum']
    second_ids=second['properties']['atoms']['items']['properties']['source_refs']['items']['properties']['ref_id']['enum']
    assert set(first_ids).isdisjoint(second_ids)
    assert all(refs[key]['evidence_id']=='e1' for key in first_ids)
    value=response();value['verdicts'][0]['qualification_reviews']={'q1':{'applicability':'unresolved','rationale':'适用范围待核实'}}
    expanded=expand_reference_verdicts(value,refs,checks)
    assert expanded['verdicts'][0]['qualification_reviews'][0]['quote']==original['qualification_checks']['c1'][0]['quote']
