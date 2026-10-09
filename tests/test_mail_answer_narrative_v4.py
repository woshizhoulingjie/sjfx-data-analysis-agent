"""A complete host candidate fixes actual v3 omission without accepting a vote."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_mail_answer_narrative_source_units import actual_replay
from services.mail.research.answer_synthesis import supplementary_source_quotes
from services.mail.research.narrative import (
    _supported_records, _literal_checks, build_source_units, host_bound_candidate,
    resolve_writer_units, visible_literal_coverage, visible_core_coverage,
)


def actual_parts():
    f = json.loads((Path(__file__).parent / 'fixtures/mail_narrative_v3_actual_omission.json').read_text(encoding='utf-8'))
    records = _supported_records(f, [])
    literals = _literal_checks(records, supplementary_source_quotes(f['answer_synthesis']['claims'], f['evidence']))
    return f, records, literals, build_source_units(records, literals, f['evidence'])


def test_actual_v3_writer_was_exact_but_omitted_responsibility():
    f, records, literals, units = actual_parts()
    raw = f['actual_call_008']['response']['json']
    assert raw == f['actual_audit']['writer_raw']
    resolved = resolve_writer_units(raw, units)
    assert resolved is not None and not visible_literal_coverage(resolved['sentences'], units, literals)
    chosen = {row['unit_id'] for row in raw['sentences']}
    assert 'unit-2' in chosen and 'unit-5' in chosen and 'unit-4' not in chosen
    assert len(literals) == 7 and len(units) == 9


def test_host_constructs_complete_minimal_eight_before_one_original_judge():
    f, records, literals, units = actual_parts()
    candidate = host_bound_candidate(units, records, literals)
    assert len(candidate['sentences']) == 8
    chosen = {row['unit_id'] for row in candidate['sentences']}
    assert 'unit-2' not in chosen and 'unit-4' in chosen
    assert visible_literal_coverage(candidate['sentences'], units, literals)
    assert visible_core_coverage(candidate['sentences'], units, records)
    answer, state, calls = actual_replay(fixture_override=f)
    assert answer['status'] == 'verified' and len(calls) == 1
    assert calls[0]['purpose'] == 'semantic_claim_verification'
    audit = state['answer_narrative_audit']
    assert audit['generation_mode'] == answer['generation_mode'] == 'host_bound_prototypes'
    assert audit['writer_calls'] == 0 and 'writer_raw' not in audit
    assert audit['host_candidate_raw'] == candidate
    assert len(audit['source_units']) == 9 and len(answer['sentences']) == 8
    assert '若再次逾期' in answer['answer'] and '实际完成日期' in answer['answer']
    assert '10 月 21 日 09:00' in answer['answer'] and '未交付、未验收' in answer['answer']
    assert len(answer['paragraphs']) == 2


def test_source_unit_and_record_order_cannot_change_selected_facts():
    _, records, literals, units = actual_parts()
    before = deepcopy((records, literals, units))
    first = host_bound_candidate(units, records, literals)
    second = host_bound_candidate(list(reversed(units)), list(reversed(records)), list(reversed(literals)))
    assert first == second and before == (records, literals, units)


@pytest.mark.parametrize('kind', ['date', 'polarity', 'actor', 'quote', 'missing_atom', 'qualification'])
def test_host_generation_does_not_promote_a_positive_invalid_judge_vote(kind):
    f, _, _, _ = actual_parts()
    def mutate(value):
        verdict = value['verdicts'][0]
        atom = verdict['atoms'][0]
        if kind == 'date': atom['text'] = atom['text'].replace('18 日', '19 日')
        elif kind == 'polarity': atom['text'] = '蓝湾项目已经交付并验收完成。'
        elif kind == 'actor': atom['support_evidence_ids'] = ['foreign']
        elif kind == 'quote': atom['source_quotes'][0]['quote'] = '不存在的原文已完成交付。'
        elif kind == 'missing_atom': verdict['atoms'] = []
        else: verdict['qualification_reviews'] = []
        return value
    answer, state, calls = actual_replay(mutate_judge=mutate, fixture_override=f)
    assert len(calls) == 1 and answer['status'] == 'fallback'
    assert state['answer_narrative_audit']['reason'] == 'new_sentence_verification_failed'


@pytest.mark.parametrize('field', ['complete', 'covered_record_ids', 'quote_reviews'])
def test_original_semantic_support_is_not_sufficient_without_complete_literal_audit(field):
    f, _, _, _ = actual_parts()
    def mutate(value):
        value['answer_completeness'].pop(field)
        return value
    answer, state, calls = actual_replay(mutate_judge=mutate, fixture_override=f)
    assert len(calls) == 1 and answer['status'] == 'fallback'
    assert state['answer_narrative_audit']['reason'] == 'answer_completeness_unproven'


def test_semantic_judge_rejection_does_not_trigger_writer_or_repair():
    f, _, _, _ = actual_parts()
    def mutate(value):
        value['verdicts'][0]['atoms'][0]['status'] = 'insufficient'
        value['verdicts'][0]['atoms'][0]['missing_requirements'] = ['语义仍需更多证据']
        return value
    answer, state, calls = actual_replay(mutate_judge=mutate, fixture_override=f)
    assert answer['status'] == 'fallback' and len(calls) == 1
    assert 'writer_raw' not in state['answer_narrative_audit']
