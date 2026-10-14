"""The actual v2 failure remains immutable; v3 adds deterministic visibility."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_mail_answer_narrative_source_units import actual_replay, actual_writer
from services.mail.research.narrative import (
    _supported_records, _literal_checks, build_source_units, source_local_date_proposal,
    resolve_writer_units, visible_literal_coverage, _complete_check,
)
from services.mail.research.answer_synthesis import supplementary_source_quotes, _atom_profile_errors
from services.mail.research.claim_verifier import verify_claims


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/mail_narrative_v2_actual_failure.json').read_text(encoding='utf-8'))


def parts():
    f = fixture(); records = _supported_records(f, [])
    literals = _literal_checks(records, supplementary_source_quotes(f['answer_synthesis']['claims'], f['evidence']))
    return f, records, literals, build_source_units(records, literals, f['evidence'])


def test_actual_failure_is_not_repaired_and_original_verifier_replays_it():
    f, records, literals, units = parts()
    raw = f['actual_call_039']['response']['json']
    assert raw == f['actual_audit']['writer_raw']
    assert resolve_writer_units(raw, units) is None
    checked = verify_claims(f['actual_audit']['candidate_sentences'],
        {s['evidence_id']: s for s in f['evidence']},
        lambda system, payload: deepcopy(f['actual_call_040']['response']['json']), _repair=False)
    assert checked[0]['semantic_status'] == checked[1]['semantic_status'] == 'insufficient'
    assert 'temporal_scope_or_full_history_unproven' in checked[0]['semantic_verification']['missing_requirements']


def test_complete_eight_sentences_cover_actual_status_and_every_original_literal():
    f, records, literals, units = parts()
    answer, state, calls = actual_replay(fixture_override=f)
    assert answer['status'] == 'verified' and len(calls) == 1
    assert len(state['answer_narrative_audit']['checked_sentences']) == 8
    assert visible_literal_coverage(answer['sentences'], units, literals)
    text = answer['answer']
    for word in ('2026 年 10 月 18 日', '10 月 21 日 09:00', '未交付、未验收',
            '10 月 15 日', '10 月 20 日', '均不再采用', '双方签署《验收报告》',
            '均不构成交付完成', '若再次逾期', 'WMS 联调测试报告', '实际完成日期'):
        assert word in text
    assert len(answer['paragraphs']) == 2 and text.count('邮件记载') < 5
    assert '最终' not in text and '当前' not in text
    merged = next(u for u in units if u.get('dated_status_same_body'))
    own = next(s for s in f['evidence'] if s['evidence_id'] == merged['primary_evidence_id'])
    assert merged['source_quote'] in own['quote_text']
    assert records[1]['source_quote'] in merged['source_quote']


@pytest.mark.parametrize('quote_id', ['literal-%s' % i for i in range(1, 8)])
def test_no_judge_vote_can_hide_omitted_literal_even_as_immaterial(quote_id):
    f, records, literals, units = parts()
    writer = actual_writer(units)
    removed = next(u['unit_id'] for u in units if u.get('literal_quote_id') == quote_id)
    writer['sentences'] = [s for s in writer['sentences'] if s['unit_id'] != removed]
    assert not visible_literal_coverage(writer['sentences'], units, literals)
    answer, state, calls = actual_replay(writer=writer, fixture_override=f)
    assert answer['status'] == 'verified' and len(calls) == 1
    assert visible_literal_coverage(answer['sentences'], units, literals)
    assert 'writer_raw' not in state['answer_narrative_audit']


@pytest.mark.parametrize('mutation', ['false_visible', 'false_material'])
def test_judge_hidden_conditions_do_not_count_as_visible(mutation):
    f, _, _, _ = parts()
    def mutate(value):
        row = value['answer_completeness']['quote_reviews'][0]
        row['material_for_objective'] = mutation != 'false_material'
        row['preserved_in_answer'] = False
        row['reason'] = '隐含在 qualification_reviews，不必写明。'
        return value
    answer, state, calls = actual_replay(mutate_judge=mutate, fixture_override=f)
    assert len(calls) == 1 and answer['status'] == 'fallback'
    assert state['answer_narrative_audit']['reason'] == 'answer_completeness_unproven'


def test_actual_single_date_synonym_is_only_an_unjudged_proposal():
    f, _, _, _ = parts(); sources = {s['evidence_id']: s for s in f['evidence']}
    row = f['actual_call_007']['response']['json']['claims'][1]
    before = deepcopy(row); proposal = source_local_date_proposal(row, sources)
    assert proposal is not None and row == before
    assert proposal['claim_text'] == row['source_quote'].replace('最终', '', 1)
    assert {k: v for k, v in proposal.items() if k != 'claim_text'} == {k: v for k, v in row.items() if k != 'claim_text'}
    assert not _atom_profile_errors(proposal, sources)
    # A pre-judge source-local proposal is never itself a supported verdict.
    assert 'semantic_status' not in proposal


@pytest.mark.parametrize('bad', ['compound', 'new_date', 'completion', 'judged', 'foreign_actor', 'condition', 'quoted'])
def test_source_local_synonyms_do_not_relax_source_polarity_scope_or_verdicts(bad):
    f, _, _, _ = parts(); sources = {s['evidence_id']: deepcopy(s) for s in f['evidence']}
    row = deepcopy(f['actual_call_007']['response']['json']['claims'][1])
    if bad == 'compound': row['claim_text'] += '且已交付。'
    elif bad == 'new_date': row['claim_text'] = row['claim_text'].replace('18日', '19日')
    elif bad == 'completion': row['claim_text'] = row['claim_text'].replace('确认交付', '实际完成交付')
    elif bad == 'judged': row['semantic_status'] = 'supported'
    elif bad == 'foreign_actor': row['actor'] = 'forged@example.com'
    elif bad == 'condition': row['claim_text'] = '若获批准，' + row['claim_text']
    else:
        for s in sources.values():
            if row['source_quote'] in s['quote_text']:
                s['body_fragments'] = [{'fragment_kind': 'quoted', 'char_start': 0, 'char_end': len(s['quote_text'])}]
    assert source_local_date_proposal(row, sources) is None
