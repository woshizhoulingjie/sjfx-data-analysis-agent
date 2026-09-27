"""Record-bound counts survive prose without weakening fact bindings."""
import pytest
from services import answer_quality as aq
from services.claim_verifier import ClaimVerifier
from services.conversation import _citation
from services.grounded_clauses import compound_support

SOURCE = ('title: 到货记录；sample: 南岭项目HT-2031-023到货记录，记录时间2031年9月4日。'
          '计划交付P-42设备14台，实收11台，尚缺3台。'
          '尚未完成联合调试，负责人未签署最终验收单。；keywords: 到货、实收、尚缺')

def review(answer, sources=(SOURCE,), extra=None):
    rows=[]
    for i, source in enumerate(sources, 1):
        row=_citation({'evidence_id':f'record-{i}','source_path':f'record-{i}.txt',
            'text':source,'label':'file_summary','location':{'source':'file_summary'},
            'evidence_quality':{'eligible':True}},i)
        row.update(extra or {})
        rows.append(row)
    draft={'answer':answer,'citations':rows,'input_manifest':{'answer_contract':
        aq.answer_contract('实收多少台、还缺多少台？',rows)}}
    v=ClaimVerifier().verify(draft,{'scope':{'kind':'package'},'modes':['retrieval']})
    return draft,v

@pytest.mark.parametrize('answer',[
    '截至2031年9月4日，南岭项目实收11台，尚缺3台[1]。',
    '根据资料记载，截至2031年9月4日，南岭项目实收到货11台，尚缺3台[1]。',
    '据原文记录，截至2031年9月4日，南岭项目实收11台，尚缺3台[1]。',
    '**实收数量**：11台，**缺少数量**：3台[1]。',
    '截至2031年9月4日,南岭项目实收11台,尚缺3台[1]。',
    '南岭项目HT-2031-023实收11台，尚缺3台[1]。',
])
def test_record_quantities_survive_grounded_prose(answer):
    draft,v=review(answer)
    assert v['status']=='verified'
    assert v['missing_answer_aspects']==[]
    assert ClaimVerifier().guard_result(draft,v)['answer']==answer
    assert draft['citations'][0]['original_text']==SOURCE

@pytest.mark.parametrize('answer,source',[
    ('截至2031年9月5日，南岭项目实收11台，尚缺3台[1]。',SOURCE),
    ('截至2031年9月4日，北湖项目实收11台，尚缺3台[1]。',SOURCE),
    ('南岭项目实收3台，尚缺11台[1]。',SOURCE),
    ('南岭项目实收11箱，尚缺3箱[1]。',SOURCE),
    ('南岭项目HT-2031-024实收11台，尚缺3台[1]。',SOURCE),
    ('南岭项目实收11台，尚缺3台[1]。','南岭项目到货记录。计划实收11台，尚缺3台。'),
    ('南岭项目实收11台，尚缺3台[1]。','南岭项目到货记录。尚未实收11台，尚缺3台。'),
    ('南岭项目实收11台，尚缺3台[1]。','南岭项目到货记录。未确认实收11台，尚缺3台。'),
    ('南岭项目实收11台，尚缺3台[1]。','南岭项目到货记录。实收11台，尚缺4台。北湖项目到货记录。尚缺3台。'),
    ('截至2031年9月5日，南岭项目实收11台，尚缺3台[1]。',
        '南岭项目到货记录，记录时间2031年9月4日。实收11台，尚缺3台。记录时间2031年9月5日。实收12台，尚缺2台。'),
    ('南岭项目实收11台，尚缺3台[1]。','南岭项目到货记录。实收11台；尚缺3台。'),
    ('南岭项目实收11台，尚缺3台[1]。','南岭项目到货记录。实收11台，尚缺3台，实收10台。'),
])
def test_quantities_cannot_cross_fact_boundaries(answer,source):
    _,v=review(answer,(source,))
    assert v['status']!='verified'
    assert v['failed_claims']

def test_separate_citations_cannot_splice_record_proof():
    _,v=review('南岭项目实收11台，尚缺3台[1][2]。',
        ('南岭项目到货记录。实收11台。','北湖项目到货记录。尚缺3台。'))
    assert v['status']!='verified'

@pytest.mark.parametrize('claim',[
    '假设截至2031年9月4日，实收11台，尚缺3台',
    '南岭项目实收11台，尚缺3台，已经验收通过',
    '根据资料记载，实收11台且全部安全，尚缺3台',
    '根据供应商承诺，实收11台，尚缺3台',
])
def test_unknown_assertions_are_never_dropped(claim):
    assert compound_support(claim,SOURCE) is None

@pytest.mark.parametrize('extra',[{'preview_only':True},{'coverage':{'level':'preview'}},{'label':'title'}])
def test_compound_proof_cannot_upgrade_ineligible_evidence(extra):
    _,v=review('截至2031年9月4日，南岭项目实收11台，尚缺3台[1]。',extra=extra)
    assert v['status']!='verified'

def test_different_unit_values_are_not_hardcoded():
    source=('东川项目运输记录，记录时间2032年1月7日。实收27箱，尚缺9箱。'
            '记录用于仓库收货核对，数量以本记录为准，不代表已经通过质量验收。'
            '运输费用与保险责任未在本记录中说明，货物等待进一步检验。')
    _,v=review('截至2032年1月7日，东川项目实收27箱，尚缺9箱[1]。',(source,))
    assert v['status']=='verified'

def test_context_free_labels_keep_existing_local_field_gate():
    claim='实收数量为13台，尚缺数量为6台。[1]'
    assert compound_support(claim,'实收13台，尚缺6台。') is None
    _,v=review(claim,('实收13台，尚缺6台。',))
    assert v['status']=='verified'
