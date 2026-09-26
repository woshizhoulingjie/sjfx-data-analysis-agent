import pytest
from services import answer_quality as aq
from services.claim_verifier import ClaimVerifier
from services.conversation import _citation
from services.evidence import verify_claim_evidence, build_file_claims


def item(text, index=1):
    return {'evidence_id': f'compound-{index}', 'source_path': f'record-{index}.txt',
        'text': text + '以上记录用于现场核对与资料归档。', 'label': 'paragraph',
        'location': {'source': 'file_summary'}}


def review(claim, sources, query=''):
    if isinstance(sources, str):
        sources = [sources]
    citations = [_citation(item(text, index), index) for index, text in enumerate(sources, 1)]
    draft = {'answer': claim, 'citations': citations,
        'input_manifest': {'answer_contract': aq.answer_contract(query, citations)}}
    return draft, ClaimVerifier().verify(draft, {'scope': {'kind': 'package'}, 'modes': ['retrieval']})


RECORD = '昆仑项目NZ-2031-044到货记录，记录时间2031年9月4日。计划交付Q-23水泵19台，实收13台，尚缺6台。尚未完成通电联合调试。'


@pytest.mark.parametrize('claim,source', [
    ('昆仑项目实收13台、尚缺6台[1]。', RECORD),
    ('昆仑项目实收13台且尚缺6台[1]。', RECORD),
    ('昆仑项目实收13台并且尚缺6台[1]。', RECORD),
    ('昆仑项目**实收数量**：13台、**缺少数量**：6台[1]。', RECORD),
    ('昆仑项目截至2031年9月4日实收13台且尚缺6台[1]。', RECORD),
    ('目前实收13台且未完成通电联合调试[1]。', RECORD),
    ('目前仅到货13台且未完成通电联合调试[1]。', RECORD),
    ('实收13台且已完成调试[1]。', '实收13台，已完成调试。'),
    ('实收13台、尚缺6台且未完成通电联合调试[1]。', RECORD),
    ('实收13台且尚缺6台[1]。', '实收13台且尚缺6台。'),
    ('甲单位实收13台且尚缺6台[1]。', '甲单位实收13台，尚缺6台。'),
    ('实收13台且尚缺6台[1]。', '已到货13台，尚缺6台。'),
])
def test_each_supported_atom_survives_without_rewriting(claim, source):
    draft, checked = review(claim, source)
    assert checked['status'] == 'verified'
    assert not checked['failed_claims']
    assert draft['answer'] == claim
    assert draft['citations'][0]['original_text'] == item(source)['text']
    guarded = ClaimVerifier().guard_result(draft, checked)['answer']
    assert '13台' in guarded and '[1]' in guarded


@pytest.mark.parametrize('claim,source', [
    ('实收6台且尚缺13台[1]。', RECORD),
    ('实收13箱且尚缺6台[1]。', RECORD),
    ('实收13台且已完成通电联合调试[1]。', RECORD),
    ('实收13台且未完成调试[1]。', '实收13台，已完成调试。'),
    ('实收13台且未完成调试[1]。', '实收13台。'),
    ('实收13台且尚缺6台[1]。', '甲项目实收13台，乙项目尚缺6台。'),
    ('实收13台且尚缺6台[1]。', '甲单位实收13台，乙单位尚缺6台。'),
    ('实收13台且尚缺6台[1]。', '甲实收13台，乙尚缺6台。'),
    ('实收13台且未完成调试[1]。', '实收13台，乙未完成调试。'),
    ('实收13台且尚缺6台[1]。', '甲项目到货记录，记录时间2031年9月4日。实收13台。记录时间2031年9月5日。尚缺6台。'),
    ('实收13台且尚缺6台[1]。', '设备Q-23实收13台，设备P-42尚缺6台。'),
    ('乙项目实收13台且尚缺6台[1]。', RECORD),
    ('昆仑项目截至2031年9月5日实收13台且尚缺6台[1]。', RECORD),
    ('实收13台且尚缺6台[1]。', '计划实收13台，尚缺6台。'),
    ('实收13台且尚缺6台[1]。', '若实收13台，则尚缺6台。'),
    ('实收13台且尚缺6台[1]。', '实收13台，实收12台，尚缺6台。'),
    ('实收13台且未修复[1]。', '实收13台，没有确认故障已经修复。'),
    ('实收13台且已修复[1]。', '实收13台，计划于2031年9月8日完成修复。'),
    ('实收13台且未完成调试[1]。', '实收13台，已完成调试。尚未完成调试。'),
])
def test_every_atom_and_record_binding_is_required(claim, source):
    _, checked = review(claim, source)
    assert checked['status'] != 'verified'
    assert checked['failed_claims']


def test_separate_citations_do_not_fake_a_shared_record():
    _, checked = review('实收13台且尚缺6台[1][2]。', ['甲项目实收13台。', '乙项目尚缺6台。'])
    assert checked['status'] != 'verified'


def test_supported_compound_counts_satisfy_required_aspects():
    _, checked = review('实收13台且尚缺6台[1]。', RECORD, '实收多少台、还缺多少台？')
    assert checked['missing_answer_aspects'] == []
    assert checked['status'] == 'verified'


@pytest.mark.parametrize('claim,source', [
    ('目前故障尚未修复', '巡检记录没有确认故障已经修复。'),
    ('故障已修复', '目前尚未确认故障已经修复。'),
    ('设备P-42故障未修复', '设备P-42巡检记录。没有确认故障已经修复。'),
    ('故障已修复', '计划于2031年9月8日完成修复。'),
    ('尚未完成书面验收', '尚未确认已经完成书面验收。'),
])
def test_uncertainty_is_not_a_completed_or_denied_outcome(claim, source):
    source_item = item(source)
    verified = verify_claim_evidence(claim, source_item)
    assert verified['support_status'] == 'insufficient'
    assert verified.get('hard_reject') is True
    _, checked = review(claim + '[1]。', source)
    assert checked['status'] != 'verified'
    assert checked['failed_claims']
    formal = build_file_claims({'conclusions': [claim]}, [source_item])
    assert not formal['file_conclusions']
    assert formal['file_review_items']


def test_explicit_observation_is_not_rejected_as_unknown():
    result = verify_claim_evidence('故障尚未修复', item('故障尚未修复。'))
    assert not result.get('hard_reject')
    assert result['support_status'] in {'supported', 'partially_supported'}


def test_unknown_grammar_does_not_get_a_numeric_free_pass():
    _, checked = review('实收13台且已解决全部安全问题[1]。', RECORD)
    assert checked['status'] != 'verified'


@pytest.mark.parametrize('extra', [
    {'preview_only': True}, {'coverage': {'level': 'preview'}},
    {'label': 'title'}, {'text': '实收13台，尚缺6台。'},
])
def test_compound_proof_cannot_override_evidence_eligibility(extra):
    source = item('实收13台，尚缺6台。')
    source.update(extra)
    citation = _citation(item('实收13台，尚缺6台。'), 1)
    # Preserve the explicit quality flags across the serialized citation.
    citation.update({k: v for k, v in extra.items() if k != 'text'})
    if 'text' in extra:
        citation['original_text'] = extra['text']
        citation['text'] = extra['text']
    result = ClaimVerifier().verify(
        {'answer': '实收13台且尚缺6台[1]。', 'citations': [citation]},
        {'scope': {'kind': 'package'}, 'modes': ['retrieval']})
    assert result['status'] != 'verified'


@pytest.mark.parametrize('source', [
    '2031年9月4日实收13台，2031年9月5日尚缺6台。',
    '截至2031年9月4日实收13台。到了2031年9月5日尚缺6台。',
    '合同NZ-2031-044实收13台，合同ZZ-2031-077尚缺6台。',
    '设备Q-23实收13台，设备P-42尚未完成调试。',
])
def test_no_cross_date_or_cross_contract_completion(source):
    claim = ('实收13台且尚未完成调试[1]。' if '调试' in source
             else '实收13台且尚缺6台[1]。')
    _, checked = review(claim, source)
    assert checked['status'] != 'verified'


@pytest.mark.parametrize('claim,source', [
    ('故障已修复', '故障没有确认已经修复，修复工作仍在核查。'),
    ('故障尚未修复', '没有证据证明故障已经修复。'),
    ('故障已修复', '如果已经修复，则可以恢复使用。'),
    ('故障尚未修复', '预计尚未修复。'),
])
def test_implicit_citation_cannot_launder_uncertainty(claim, source):
    _, checked = review(claim + '。', source)
    assert checked['status'] != 'verified'


def test_proven_observation_still_supported_after_uncertainty():
    _, checked = review('故障已修复[1]。',
        '此前没有确认故障已经修复。目前故障已修复，记录用于核对。')
    assert checked['status'] == 'verified'


@pytest.mark.parametrize('source', [
    '实收13台，乙项目尚缺6台。',
    '实收13台，乙单位尚缺6台。',
    '实收13台，设备Q-23尚缺6台。',
    '实收13台，2031年9月5日尚缺6台。',
])
def test_late_identity_cannot_relabel_anonymous_facts(source):
    _, checked = review('实收13台且尚缺6台[1]。', source)
    assert checked['status'] != 'verified'


@pytest.mark.parametrize('claim,source', [
    ('尚未完成书面验收', '尚未确认已经完成书面验收。'),
    ('故障尚未修复', '没有确认故障已经修复。'),
])
def test_short_uncertain_source_cannot_bypass_hard_rejection(claim, source):
    raw = item(source)
    raw['text'] = source
    result = verify_claim_evidence(claim, raw)
    assert result['support_status'] == 'insufficient'
    assert result['hard_reject'] is True
    citation = _citation(item(source), 1)
    citation.update(original_text=source, text=source)
    result = ClaimVerifier().verify(
        {'answer': claim + '[1]。', 'citations': [citation]},
        {'scope': {'kind': 'package'}, 'modes': ['retrieval']})
    assert result['status'] != 'verified'


def test_context_expires_across_an_oversized_clause():
    _, checked = review('实收13台且尚缺6台[1]。',
        '实收13台。' + '待核实背景说明' * 120 + '。尚缺6台。')
    assert checked['status'] != 'verified'
