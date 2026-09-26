import copy
import pytest
from services.document_analysis import _preview_claim_contract
from services.evidence import build_file_claims

MAINTENANCE = ('青松车间设备P-99巡检记录，巡检日期2025年6月14日。发现密封圈有5毫米裂纹并有渗漏，'
    '设备立即停机隔离，不允许继续带压运行。计划2025年6月18日更换密封圈并复检，'
    '复检合格后方可恢复运行。未列出维修费用，也没有确认故障已经修复。')
CONTRACT = ('采购合同HT-2025-017。合同标的是10台P-17循环泵，总价260000元。'
    '合同没有规定罚款金额，亦未授权供应商通过邮件变更收款账户。')

def sources(text):
    return [{'evidence_id':'E-SAMPLE-1','source_path':'sample.txt','text':text,
        'label':'text_chunk','evidence_role':'direct_source'}]

def material(answer):
    return {'title':'原有标题','core_summary':answer,'summary':answer,
        'conclusions':[answer],'key_facts':[], 'arguments':[], 'methodology':[],
        'topics':['设备'],'claim_evidence':[{'statement':answer,'evidence_ids':['E-SAMPLE-1']}]}

def apply_contract(kind, value, evidence):
    summary=copy.deepcopy(value)
    contract=_preview_claim_contract if kind=='preview' else build_file_claims
    summary.update(contract(summary,evidence))
    return summary

@pytest.mark.parametrize('kind',['preview','deep'])
@pytest.mark.parametrize('claim',['目前故障尚未修复','故障已经修复'])
def test_unconfirmed_repair_is_not_asserted_as_known(kind,claim):
    original=material(claim); result=apply_contract(kind,original,sources(MAINTENANCE))
    assert result['core_summary'] != claim
    assert '确认' in result['core_summary']
    assert result['summary']==result['core_summary']
    assert result['conclusions']==[result['core_summary']]
    assert original==material(claim)
    assert all(claim not in x['statement'] for x in result['file_conclusions'])

@pytest.mark.parametrize('kind',['preview','deep'])
def test_lack_of_authorization_is_not_a_prohibition(kind):
    claim='禁止通过邮件变更收款账户'
    result=apply_contract(kind,material(claim),sources(CONTRACT))
    assert '禁止' not in result['core_summary']
    assert '未授权' in result['core_summary']
    assert '通过邮件变更收款账户' in result['core_summary']

@pytest.mark.parametrize('kind',['preview','deep'])
@pytest.mark.parametrize('claim,text',[
    ('故障尚未修复','P-99设备维修记录。故障尚未修复，设备仍在停机隔离。'),
    ('故障已经修复','设备维修记录显示故障已经修复，验收已经完成。'),
    ('未确认故障已修复',MAINTENANCE),
    ('计划2025年6月18日更换密封圈并复检',MAINTENANCE),
    ('禁止通过邮件变更收款账户',CONTRACT+'合同明确禁止通过邮件变更收款账户。'),
    ('如果故障尚未修复，应继续停机隔离',MAINTENANCE),
    ('建议禁止通过邮件变更收款账户',CONTRACT),
    ('原邮件声称“故障已经修复”',MAINTENANCE),
    ('P-17故障尚未修复',MAINTENANCE),
])
def test_known_facts_qualifiers_and_other_subjects_are_preserved(kind,claim,text):
    result=apply_contract(kind,material(claim),sources(text))
    assert result['core_summary']==claim
    assert result['summary']==claim

def test_large_package_model_entry_preserves_layout_and_qualifier(monkeypatch):
    from config import Config
    from large_package_worker import LargePackageWorker
    import services.vllm
    calls=[]
    answer=chr(10).join(['**主题**：设备巡检','**关键事实**：发现5毫米裂纹。',
        '**风险**：目前故障尚未修复。','**后续建议**：复检合格后恢复运行。'])
    class Model:
        def __init__(self,*args,**kwargs):pass
        def chat(self,*args,**kwargs):
            calls.append((args,kwargs))
            return {'content':answer,'model':'test-model','finish_reason':'stop','usage':{}}
    monkeypatch.setattr(Config,'LLM_BACKEND','vllm')
    monkeypatch.setattr(Config,'ENABLE_VLLM',True)
    monkeypatch.setattr(services.vllm,'VLLMClient',Model)
    value=object.__new__(LargePackageWorker)._model_summary('sample.txt',MAINTENANCE)
    assert '目前故障尚未修复' not in value['summary']
    assert '确认' in value['summary']
    assert all(heading in value['summary'] for heading in ('**主题**','**关键事实**','**风险**','**后续建议**'))
    assert '发现5毫米裂纹' in value['summary']
    assert len(calls)==1 and value['coverage']=='sampled' and value['quality']=='complete'

def test_corrections_keep_auditable_original_and_do_not_mutate_sources():
    fidelity=pytest.importorskip('services.summary_fidelity')
    evidence=sources(MAINTENANCE); before=copy.deepcopy(evidence)
    patch=fidelity.summary_qualifier_patch(material('目前故障尚未修复'),evidence)
    audit=patch['summary_fidelity']
    assert audit['full_semantic_validation'] is False
    assert audit['changes'][0]['original']=='目前故障尚未修复'
    assert audit['changes'][0]['evidence_ids']==['E-SAMPLE-1']
    assert evidence==before

def test_cross_file_evidence_never_rewrites_summary():
    fidelity=pytest.importorskip('services.summary_fidelity')
    evidence=sources(MAINTENANCE)+[dict(sources(CONTRACT)[0],source_path='other.txt')]
    patch=fidelity.summary_qualifier_patch(material('故障尚未修复'),evidence)
    assert 'core_summary' not in patch

def test_source_after_budget_is_never_silently_ignored():
    fidelity=pytest.importorskip('services.summary_fidelity')
    evidence=sources(MAINTENANCE+'一般材料。'*50000+'故障尚未修复。')
    patch=fidelity.summary_qualifier_patch(material('故障尚未修复'),evidence)
    assert 'core_summary' not in patch
    assert patch['summary_fidelity']['status']=='not_checked'

@pytest.mark.parametrize('claim',[
    '如果检测异常，目前故障尚未修复',
    '建议制定规范，禁止通过邮件变更收款账户',
    '例如一种情形，目前故障尚未修复',
])
def test_non_assertion_context_survives_comma(claim):
    fidelity=pytest.importorskip('services.summary_fidelity')
    patch=fidelity.summary_qualifier_patch(material(claim),sources(MAINTENANCE+CONTRACT))
    assert 'core_summary' not in patch

def test_whole_statement_citation_ids_and_dict_fields_are_preserved():
    fidelity=pytest.importorskip('services.summary_fidelity')
    claim='设备发现裂纹，目前故障尚未修复'
    original=material(claim)
    original['key_facts']=[{'statement':claim,'label':'保留标签'}]
    patch=fidelity.summary_qualifier_patch(original,sources(MAINTENANCE))
    corrected=patch['core_summary']
    assert corrected!=claim and '设备发现裂纹' in corrected
    assert patch['claim_evidence']==[{'statement':corrected,'evidence_ids':['E-SAMPLE-1']}]
    assert patch['key_facts']==[{'statement':corrected,'label':'保留标签'}]

def test_repeated_contract_build_keeps_original_correction_audit():
    first=apply_contract('preview',material('目前故障尚未修复'),sources(MAINTENANCE))
    second=apply_contract('preview',first,sources(MAINTENANCE))
    assert first['summary_fidelity']==second['summary_fidelity']
    assert first['core_summary']==second['core_summary']

def test_corrected_claims_remain_review_items_not_auto_verified():
    first=apply_contract('deep',material('尚未完成书面验收'),sources('尚未确认已经完成书面验收。'))
    assert not first['file_conclusions']
    assert first['file_review_items']
    second=apply_contract('deep',first,sources('尚未确认已经完成书面验收。'))
    assert not second['file_conclusions']
    assert first['summary_fidelity']==second['summary_fidelity']

@pytest.mark.parametrize('claim',['  尚未完成书面验收  ','目前  尚未完成书面验收'])
def test_whitespace_normalization_cannot_promote_corrected_claim(claim):
    first=apply_contract('deep',material(claim),sources('尚未确认已经完成书面验收。'))
    assert first['summary_fidelity']['changes']
    assert not first['file_conclusions']
    assert first['file_review_items']

def test_unknown_subject_is_not_silently_dropped_to_force_a_correction():
    fidelity=pytest.importorskip('services.summary_fidelity')
    result=fidelity.summary_qualifier_patch(material('项目  尚未完成书面验收'),
        sources('尚未确认已经完成书面验收。'))
    assert 'core_summary' not in result
    assert not result['summary_fidelity']['changes']

def test_new_certain_record_does_not_get_hidden_behind_old_uncertainty():
    fidelity=pytest.importorskip('services.summary_fidelity')
    evidence=sources(MAINTENANCE)+[dict(sources('设备P-99复核记录。故障尚未修复。')[0],evidence_id='E-NEW')]
    patch=fidelity.summary_qualifier_patch(material('目前故障尚未修复'),evidence)
    assert 'core_summary' not in patch

@pytest.mark.parametrize('source',[
    '合同未授权供应商通过邮件变更收款账户，但合同严禁通过邮件变更收款账户。',
    '合同并非未授权供应商通过邮件变更收款账户。',
    '合同未授权供应商通过邮件变更收款账户，原文规定不得通过邮件变更收款账户。',
])
def test_prohibitions_or_double_negatives_are_not_weakened(source):
    fidelity=pytest.importorskip('services.summary_fidelity')
    patch=fidelity.summary_qualifier_patch(material('禁止通过邮件变更收款账户'),sources(source))
    assert 'core_summary' not in patch

@pytest.mark.parametrize('pair',[
    ('preview-source-blocks-v2','preview-claims-evidence-v2'),
    ('preview-source-blocks-v2','preview-claims-qualifiers-v3'),
    ('preview-source-blocks-v2','preliminary-batch-claims-evidence-v2'),
    ('selective-6500-v2-source-ids','compact-6500-evidence-v2'),
    ('selective-6500-v2-source-ids','compact-6500-qualifiers-v3'),
])
def test_current_and_previous_prompt_versions_can_advance_nodes(pair):
    import app
    assert pair in app.PRELIMINARY_SUMMARY_VERSION_PAIRS
