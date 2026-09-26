import pytest

from services import answer_quality as aq
from services.claim_verifier import ClaimVerifier
from services.conversation import _citation


def review(answer, source, query='实收多少台、还缺多少台？'):
    row = _citation({'evidence_id': 'quantity-label-1', 'source_path': 'receipt.txt',
        'text': source, 'label': 'paragraph', 'evidence_quality': {'eligible': True},
        'location': {'source': 'file_summary'}}, 1)
    result = {'answer': answer, 'citations': [row],
        'input_manifest': {'answer_contract': aq.answer_contract(query, [row])}}
    return result, ClaimVerifier().verify(result,
        {'scope': {'kind': 'package'}, 'modes': ['retrieval']})


@pytest.mark.parametrize('label', ['尚缺', '还缺', '缺少'])
@pytest.mark.parametrize('source_label', ['尚缺', '还缺', '缺少'])
def test_explicit_missing_quantity_labels_are_equivalent(label, source_label):
    _, verified = review(f'实收数量为11台。[1] {label}数量为3台。[1]',
        f'实收11台，{source_label}3台。')
    assert verified['status'] == 'verified'
    assert verified['missing_answer_aspects'] == []


@pytest.mark.parametrize('answer', [
    '**实收数量**：11台。[1] **缺少数量**：3台。[1]',
    '**实收数量**：**11台**。[1] **尚缺数量**：**3台**。[1]',
    '1. **实收数量**：11台。[1] 2. **还缺数量**：3台。[1]',
    '实收数量：11台；缺少数量：3台。[1]',
])
def test_presentation_does_not_delete_supported_counts(answer):
    draft, verified = review(answer, '实收11台，尚缺3台。')
    assert verified['status'] == 'verified'
    assert verified['missing_answer_aspects'] == []
    guarded = ClaimVerifier().guard_result(draft, verified)['answer']
    assert '11台' in guarded and '3台' in guarded and '[1]' in guarded
    assert draft['answer'] == answer


@pytest.mark.parametrize('source', [
    '**实收数量**：11台，**缺少数量**：3台。',
    '实收数量为11台，缺少数量为3台。',
])
def test_source_labels_are_only_normalized_for_matching(source):
    draft, verified = review('实收11台。[1] 尚缺3台。[1]', source)
    assert verified['status'] == 'verified'
    assert draft['citations'][0]['original_text'] == source


@pytest.mark.parametrize('answer,source', [
    ('**实收数量**：3台。[1]', '实收11台，尚缺3台。'),
    ('**缺少数量**：11台。[1]', '实收11台，尚缺3台。'),
    ('**缺少数量**：3台。[1]', '尚缺3箱。'),
    ('**实收数量**：11台。[1]', '尚未实收11台。'),
    ('尚未**实收数量**：11台。[1]', '实收11台。'),
    ('**缺少数量**：3台。[1]', '没有缺少3台。'),
    ('尚缺3台。[1]', '不缺少3台。'),
    ('尚缺3台。[1]', '并非缺少3台。'),
    ('尚缺3台。[1]', '并不真正缺少3台。'),
    ('不缺少3台。[1]', '尚缺3台。'),
    ('并非缺少3台。[1]', '尚缺3台。'),
    ('尚缺3台。[1]', '无缺少3台。'),
    ('没有缺少3台。[1]', '未确认尚缺3台。'),
    ('缺少3台。[1]', '未确认尚缺3台。'),
    ('**实收数量**：11台。[1]', '计划实收11台。'),
    ('**实收数量**：11台。[1]', '实收110台。'),
    ('乙项目缺少数量为3台。[1]', '甲项目到货记录。尚缺3台。'),
    ('截至2031年9月5日缺少数量为3台。[1]',
     '甲项目到货记录，记录时间2031年9月4日。尚缺3台。'),
    ('甲项目缺少数量为3台。[1]', '甲项目到货记录。乙单位尚缺3台。'),
    ('缺少金额为3元。[1]', '尚缺3台。'),
    ('实收11台且已完成调试。[1]', '实收11台，尚未完成调试。'),
])
def test_labels_cannot_change_fact_bindings(answer, source):
    _, verified = review(answer, source, query='')
    assert verified['status'] != 'verified'
    assert verified['failed_claims']


def test_unknown_quantity_does_not_gain_a_number_from_a_label():
    assert aq._quantity_label_text('实收数量未知，缺少金额待核实。') == '实收数量未知，缺少金额待核实。'


def test_rejected_labels_still_trigger_missing_aspects():
    _, verified = review('**实收数量**：3台。[1] **缺少数量**：11台。[1]',
        '实收11台，尚缺3台。')
    assert set(verified['missing_answer_aspects']) == {'实收数量', '缺少数量'}
    assert verified['needs_revision']
