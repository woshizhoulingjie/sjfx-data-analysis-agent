"""Regression of the immutable provider call 009 source-binding failure."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.mail.research.narrative import (
    _candidate_sentences, _literal_checks, _supported_records, build_source_units,
    prepare_narrative, resolve_judge_transport, resolve_writer_units,
)
from services.mail.research.answer_synthesis import supplementary_source_quotes
from services.mail.research.runtime import MailResearchRuntime


def actual_fixture():
    return json.loads((Path(__file__).parent / 'fixtures/mail_narrative_v1_writer_source_failure.json')
        .read_text(encoding='utf-8'))


def actual_parts():
    fixture = actual_fixture()
    records = _supported_records(fixture, [])
    literals = _literal_checks(records, supplementary_source_quotes(
        fixture['answer_synthesis']['claims'], fixture['evidence']))
    return fixture, records, literals, build_source_units(records, literals, fixture['evidence'])


def actual_writer(units):
    first = next(u for u in units if u['unit_kind'] == 'core' and u['record_ids'] == ['verified-1'])
    literals = [u for u in units if u.get('literal_quote_id')]
    order = {'literal-3': 0, 'literal-4': 1, 'literal-7': 2, 'literal-5': 3,
        'literal-6': 4, 'literal-1': 5, 'literal-2': 6}
    chosen = [first] + sorted(literals, key=lambda u: order[u['literal_quote_id']])
    return {'sentences': [{'unit_id': u['unit_id'], 'claim_text': u['safe_sentence'],
        'paragraph': int(i >= 2)} for i, u in enumerate(chosen)]}


def actual_replay(writer=None, mutate_judge=None, fixture_override=None):
    fixture, records, literals, units = actual_parts()
    if fixture_override is not None:
        fixture = fixture_override
        records = _supported_records(fixture, [])
        literals = _literal_checks(records, supplementary_source_quotes(fixture['answer_synthesis']['claims'], fixture['evidence']))
        units = build_source_units(records, literals, fixture['evidence'])
    evidence = fixture['evidence']
    state = {'phase2': True, 'model_calls': 0, 'max_model_calls': 20,
        'answer_synthesis': deepcopy(fixture['answer_synthesis']),
        'context_reviews': deepcopy(fixture['context_reviews']),
        'allowed_keys': {s['message_key'] for s in evidence}}
    calls = []
    runtime = MailResearchRuntime(SimpleNamespace(event=lambda *a: None,
        record_research_step=lambda *a, **k: None), context_limit=24000)

    def model(tid, owner, current, purpose, system, prompt, **kw):
        current['model_calls'] += 1
        calls.append({'purpose': purpose, 'system': system, 'prompt': prompt})
        if purpose == 'answer_narrative':
            return deepcopy(writer or actual_writer(units))
        payload = resolve_judge_transport(json.loads(prompt))
        verdicts = []
        for claim in payload['claims']:
            unit = next(u for u in units if u['source_quote'] == claim['source_quote'])
            eid = unit['primary_evidence_id']
            atom_specs = [(claim['claim_text'], claim['source_quote'])]
            if unit.get('dated_status_same_body'):
                first, second = claim['claim_text'].split('；', 1)
                literal = next(l for l in literals if l['quote_id'] == unit['literal_quote_id'])
                status_record = next(r for r in records if r['record_id'] in unit['record_ids'])
                atom_specs = [(first, literal['quote']), (second, status_record['source_quote'])]
            verdicts.append({'claim_id': claim['claim_id'], 'atoms': [{
                'text': text, 'status': 'supported', 'support_evidence_ids': [eid],
                'counter_evidence_ids': [], 'source_quotes': [{'evidence_id': eid, 'quote': quote}],
                'missing_requirements': []} for text, quote in atom_specs],
                'qualification_reviews': [{**q, 'applicability': 'applicable',
                    'rationale': '来源完整限制保留在原条件，并由完整性检查单独审查可见文字。'}
                    for q in payload['qualification_checks'][claim['claim_id']]]})
        value = {'verdicts': verdicts, 'answer_completeness': {'complete': True,
            'covered_record_ids': [r['record_id'] for r in records],
            'reason': '日期、被废弃备选、验收条件及不交付反证均被保留。',
            'quote_reviews': [{'quote_id': l['quote_id'],
                'material_for_objective': True,
                'preserved_in_answer': True,
                'reason': '技术提交及责任条款不决定本问题的日期；日期与交付定义均在正文。'}
                for l in literals]}}
        return mutate_judge(value) if mutate_judge else value

    runtime._model_call = model
    spec = {**fixture['task_spec'], 'budget': {'max_context_tokens': 12000}}
    before = deepcopy((evidence, state['answer_synthesis'], state['context_reviews']))
    answer = prepare_narrative(runtime, 'turn', 'owner', state, spec, evidence, fixture['claims'])
    assert before == (evidence, state['answer_synthesis'], state['context_reviews'])
    return answer, state, calls


def test_actual_v1_failure_is_preserved_and_not_accepted_as_v2_schema():
    fixture, records, literals, units = actual_parts()
    raw = fixture['actual_call']['response']['json']
    assert raw == fixture['actual_audit']['writer_raw']
    assert resolve_writer_units(raw, units) is None
    sources = fixture['evidence']
    allowed = {s['message_key'] for s in sources}
    assert _candidate_sentences({'sentences': raw['sentences'][:2]}, records, sources, allowed)
    assert _candidate_sentences({'sentences': raw['sentences'][:3]}, records, sources, allowed) is None
    third = raw['sentences'][2]
    missing = set(records[0]['evidence_ids']) - set(third['evidence_ids'])
    assert missing == {'mev_30267831b88c4cda8cdf6a514dabbd7c'}
    assert all(third['source_quote'] not in s['quote_text'] for s in sources
        if s['evidence_id'] in third['evidence_ids'])


def test_actual_units_keep_original_ids_actors_quotes_conditions_and_no_background_event():
    fixture, records, literals, units = actual_parts()
    by_record = {r['record_id']: r for r in records}
    by_source = {s['evidence_id']: s for s in fixture['evidence']}
    assert len(records) == 2 and len(units) == 9
    assert {r for u in units for r in u['record_ids']} == set(by_record)
    for unit in units:
        record = by_record[unit['record_ids'][0]]
        assert unit['evidence_ids'] == record['evidence_ids']
        assert unit['conditions'] == record['conditions']
        assert unit['source_quote'] in by_source[unit['primary_evidence_id']]['quote_text']
        assert unit['actor'] == record['actor'] if unit['source_kind'] == 'body' else unit['actor'] is None
    assert not any('09:30 完成蓝湾项目全部设备发运' in u['source_quote'] for u in units)


@pytest.mark.parametrize('bad', [None, [], 'foreign-unit', ['unit-1'], 'unit-1,unit-2'])
def test_only_one_known_string_unit_id_is_resolvable(bad):
    _, _, _, units = actual_parts()
    value = {'sentences': [{'unit_id': bad, 'claim_text': '约定日期尚不能证明已经交付。'}]}
    assert resolve_writer_units(value, units) is None


@pytest.mark.parametrize('field', ['actor', 'source_quote', 'record_ids', 'evidence_ids', 'conditions'])
def test_writer_cannot_override_fixed_provenance(field):
    _, _, _, units = actual_parts()
    value = actual_writer(units)
    value['sentences'][0][field] = 'foreign'
    assert resolve_writer_units(value, units) is None


def test_actual_six_unit_answer_reaches_original_strict_judge_and_preserves_audit():
    answer, state, calls = actual_replay()
    assert answer['status'] == 'verified'
    assert [c['purpose'] for c in calls] == ['semantic_claim_verification']
    audit = state['answer_narrative_audit']
    assert len(audit['source_units']) == 9 and len(audit['checked_sentences']) == 8
    from services.mail.research.narrative import host_bound_candidate
    fixture, records, literals, units = actual_parts()
    assert audit['host_candidate_raw'] == host_bound_candidate(units, records, literals)
    for raw, resolved in zip(audit['host_candidate_raw']['sentences'], audit['unit_resolution']['sentences']):
        unit = next(u for u in audit['source_units'] if u['unit_id'] == raw['unit_id'])
        for key in ('actor', 'evidence_ids', 'record_ids', 'source_quote', 'object_ref'):
            assert resolved[key] == unit[key]
    assert '实际完成日期' in answer['answer'] and '仍属未知' in answer['answer']
    assert '只有双方签署' in answer['answer'] and '不构成交付完成' in answer['answer']


@pytest.mark.parametrize('mutation', ['wrong_date', 'actual_completion', 'current_status'])
def test_positive_fake_vote_cannot_override_original_date_polarity_history_gates(mutation):
    _, _, _, units = actual_parts()
    writer = actual_writer(units)
    if mutation == 'wrong_date':
        writer['sentences'][0]['claim_text'] = '书面确认的交付日期为 2026 年 10 月 19 日。'
    elif mutation == 'actual_completion':
        writer['sentences'][-2]['claim_text'] = '截至 2026 年 10 月 21 日 09:00，项目现场已经收到设备，已交付并验收完成。'
    else:
        writer['sentences'][-1]['claim_text'] = '蓝湾项目当前仍未交付、未验收。'
    answer, state, calls = actual_replay(writer)
    assert resolve_writer_units(writer, units) is None
    assert len(calls) == 1 and answer['status'] == 'verified'
    assert state['answer_narrative_audit']['generation_mode'] == 'host_bound_prototypes'
    assert 'writer_raw' not in state['answer_narrative_audit']


def test_actual_missing_literal_completeness_still_rejects():
    def mutate(value):
        value['answer_completeness']['quote_reviews'].pop()
        return value
    answer, state, calls = actual_replay(mutate_judge=mutate)
    assert len(calls) == 1 and answer['status'] == 'fallback'
    assert state['answer_narrative_audit']['reason'] == 'answer_completeness_unproven'
