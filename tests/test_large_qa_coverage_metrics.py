"""Candidate inspection counts stay consistent without claiming full-text reads."""
import copy

import pytest

from services.analysis_tools import merge_retrieval_results
from services.claim_verifier import ClaimVerifier


BODY = '设备验收需要完成热测试。'
GLOBAL = {'global_mode': True, 'scope_files': 1040, 'scanned_files': 1040,
          'model_mapped_files': 1032, 'files_without_content': 8}


def search(numbers, *, candidates=269, large_qa=True, scope=1040):
    rows = [{'evidence_id': f'EV-{number}', 'source_path': f'file-{number:03d}.txt',
             'text': BODY, 'retrieval_score': 1.0} for number in numbers]
    count = len({row['source_path'] for row in rows})
    return {
        'results': rows, 'retrieval_mode': 'large_qa' if large_qa else 'lexical',
        'coverage': {'total_files': scope, 'scope_files': scope,
                     'candidate_files': candidates, 'inspected_files': count,
                     'scope_inspection_coverage': count / scope if scope else None,
                     'coverage_basis': 'versioned_index_not_recall_estimate' if large_qa else 'scope_inspection',
                     'query_coverage': None if large_qa else count / scope},
        'package_context': {'large_qa': copy.deepcopy(GLOBAL)} if large_qa else {},
    }


def verify(coverage, batch=None):
    # One cited file is deliberately distinct from 67 returned files, 269
    # candidates, and the independent 1040-file program scan.
    citation = {'citation_index': 1, 'evidence_id': 'EV-0', 'source_path': 'file-000.txt',
                'original_text': BODY}
    return ClaimVerifier().verify({
        'answer': BODY + '[1]', 'citations': [citation],
        'coverage': coverage, 'input_manifest': {'large_qa': copy.deepcopy(GLOBAL)},
    }, {'modes': ['retrieval'], 'scope': {'kind': 'package'}, 'steps': []},
        batch_summary=batch or {})


@pytest.mark.parametrize('limit,expected', [(70, 67), (40, 40)])
def test_large_qa_merge_recounts_files_and_ratio_after_fifty_to_sixty_seven(limit, expected):
    initial = search(range(50))
    expanded = search(range(30, 67), candidates=100)
    merged = merge_retrieval_results([initial, expanded], limit=limit)
    coverage = merged['coverage']
    assert len({item['source_path'] for item in merged['results']}) == expected
    assert coverage['candidate_files'] == 269
    assert coverage['inspected_files'] == coverage['retrieved_files'] == expected
    assert coverage['scope_files'] == 1040
    assert coverage['scope_inspection_coverage'] == pytest.approx(expected / 1040, abs=0.000001)
    assert coverage['query_coverage'] is None
    assert coverage['coverage_basis'] == 'versioned_index_not_recall_estimate'
    assert merged['package_context']['large_qa'] == GLOBAL
    checked = verify(coverage)
    metrics = checked['quality_metrics']
    assert metrics['candidate_files'] == 269
    assert metrics['inspected_files'] == expected
    assert metrics['scope_inspection_coverage'] == pytest.approx(expected / 1040)
    assert metrics['inspection_coverage'] == metrics['scope_inspection_coverage']
    assert metrics['citation_count'] == 1
    assert metrics['inspected_files'] != GLOBAL['scanned_files']


def test_verifier_ignores_stale_ratio_and_never_counts_all_candidates_as_inspected():
    coverage = search(range(67))['coverage']
    coverage['scope_inspection_coverage'] = 50 / 1040
    metrics = verify(coverage)['quality_metrics']
    assert metrics['inventory_files'] == metrics['scope_files'] == 1040
    assert metrics['candidate_files'] == 269
    assert metrics['inspected_files'] == 67
    assert metrics['scope_inspection_coverage'] == pytest.approx(67 / 1040)
    assert metrics['inspection_coverage'] == metrics['scope_inspection_coverage']


@pytest.mark.parametrize('coverage_changes,batch,expected,scope', [
    ({'inspected_files': 0, 'retrieved_files': 50}, None, 0, 1040),
    ({'inspected_files': 67}, {'inspected_files': 0}, 0, 1040),
    ({'inspected_files': None, 'retrieved_files': 67}, None, 67, 1040),
    ({'inspected_files': None, 'retrieved_files': None}, None, 0, 1040),
    ({'inspected_files': 67}, {'inventory_files': 22, 'inspected_files': 3}, 3, 22),
])
def test_verifier_count_and_ratio_use_the_same_source_with_explicit_zero(
        coverage_changes, batch, expected, scope):
    coverage = search(range(50))['coverage']
    coverage.update(coverage_changes)
    metrics = verify(coverage, batch)['quality_metrics']
    assert metrics['inspected_files'] == expected
    assert metrics['scope_files'] == scope
    assert metrics['scope_inspection_coverage'] == pytest.approx(expected / scope)
    assert metrics['inspection_coverage'] == metrics['scope_inspection_coverage']


def test_empty_large_qa_merge_keeps_zero_inspection_and_unknown_recall():
    empty = search([])
    empty['coverage']['inspected_files'] = 50
    empty['coverage']['scope_inspection_coverage'] = 50 / 1040
    merged = merge_retrieval_results([empty], limit=70)
    assert merged['coverage']['candidate_files'] == 269
    assert merged['coverage']['inspected_files'] == 0
    assert merged['coverage']['scope_inspection_coverage'] == 0
    assert merged['coverage']['query_coverage'] is None
    assert verify(merged['coverage'])['quality_metrics']['inspected_files'] == 0


def test_ordinary_scope_inspection_merge_keeps_its_existing_semantics():
    merged = merge_retrieval_results([
        search(range(50), large_qa=False), search(range(30, 67), large_qa=False),
    ], limit=70)
    coverage = merged['coverage']
    assert coverage['inspected_files'] == 67
    assert coverage['candidate_files'] == 269
    assert coverage['coverage_basis'] == 'scope_inspection'
    assert coverage['scope_inspection_coverage'] == pytest.approx(67 / 1040, abs=0.000001)
    assert coverage['query_coverage'] == coverage['scope_inspection_coverage']


def test_verifier_keeps_unknown_denominator_unknown():
    metrics = verify({'candidate_files': 269, 'inspected_files': 67})['quality_metrics']
    assert metrics['inspected_files'] == 67
    assert metrics['scope_files'] is None
    assert metrics['scope_inspection_coverage'] is None
    assert metrics['inspection_coverage'] is None
