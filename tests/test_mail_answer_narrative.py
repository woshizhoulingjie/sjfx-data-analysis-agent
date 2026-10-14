"""Direct answer prose still obeys the original atomic/source gates."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from services.mail.research.narrative import (
    prepare_narrative, fallback_presentation, narrative_output_tokens,
    narrative_judge_output_tokens, pack_judge_transport, resolve_judge_transport,
    intern_source_participants, resolve_source_participants,
)
from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.token_budget import init_usage


def source(eid, text, *, actor='alice@example.com', kind='body'):
    return {'evidence_id': eid, 'message_key': 'mail-' + eid, 'quote_text': text,
        'source_kind': kind, 'mime_path': '1', 'char_start': 0, 'char_end': len(text),
        'sent_at_utc': '2026-10-09T01:20:00+00:00',
        'attribution': 'attachment' if kind == 'attachment' else 'authored',
        'body_fragments': [{'fragment_kind': 'attachment' if kind == 'attachment' else 'authored',
            'char_start': 0, 'char_end': len(text)}],
        'participants': [{'role': 'from', 'normalized_address': actor}]}


def record(s, text, *, role='direct', quote=None, conditions=None, actor='alice@example.com'):
    return {'claim_text': text, 'semantic_status': 'supported', 'answer_relevance': role,
        'evidence_ids': [s['evidence_id']], 'source_quote': quote or s['quote_text'],
        'object_ref': '项目乙', 'actor': actor, 'conditions': conditions or [],
        'recorded_at': s['sent_at_utc'], 'recorded_sender': actor,
        'recorded_source': {'evidence_id': s['evidence_id'], 'message_key': s['message_key'],
            'source_kind': s['source_kind'], 'mime_path': s['mime_path']}}


def sentence(text, refs, s, *, quote=None, actor='alice@example.com', paragraph=0):
    return {'claim_text': text, 'record_ids': refs, 'evidence_ids': [s['evidence_id']],
        'source_quote': quote or s['quote_text'], 'object_ref': '项目乙',
        'actor': actor, 'paragraph': paragraph}


def make_runtime(sources, records, sentences, *, unknowns=None, judge_edit=None):
    calls = []
    steps = []
    runtime = MailResearchRuntime(SimpleNamespace(event=lambda *args: None,
        record_research_step=lambda *args, **kw: steps.append((args, kw))), context_limit=100000)
    state = {'phase2': True, 'model_calls': 0, 'max_model_calls': 20,
        'deadline_reached': False, 'tool_limit_reached': False, 'analysis_completed': True,
        'context_reviews': {s['message_key']: {'complete': True, 'gaps': [],
            'evidence_ids': [s['evidence_id']]} for s in sources},
        'allowed_keys': {s['message_key'] for s in sources},
        'answer_synthesis': {'status': 'complete', 'claims': records,
            'unanswered_subquestions': unknowns or []}, 'findings': []}
    by_id = {s['evidence_id']: s for s in sources}

    def model(tid, owner, current, purpose, system, prompt, **kw):
        current['model_calls'] += 1
        payload = json.loads(prompt)
        calls.append((purpose, payload, system))
        if purpose == 'answer_narrative':
            from services.mail.research.narrative import build_source_units, _supported_records
            units=build_source_units(_supported_records(current,[]),payload['literal_checks'],sources)
            selected=[];covered=set()
            for sentence_value in sentences:
                matches=[u for u in units if u['source_quote']==sentence_value['source_quote']
                    and set(u['record_ids']).intersection(sentence_value['record_ids'])]
                if not matches:
                    selected.append({'unit_id':'foreign-unit','claim_text':sentence_value['claim_text']})
                    continue
                unit=next((u for u in matches if u.get('literal_quote_id')), matches[0])
                from services.mail.research.verifier import verify_numeric, verify_temporal, verify_polarity
                from services.mail.research.claim_verifier import _requires_full_history
                from services.mail.presentation import chinese_text
                valid = (chinese_text(sentence_value['claim_text']) and verify_numeric(sentence_value['claim_text'], unit['source_quote'])
                    and verify_temporal(sentence_value['claim_text'], unit['source_quote'])
                    and verify_polarity(sentence_value['claim_text'], unit['source_quote'])
                    and not _requires_full_history(sentence_value['claim_text']))
                row={'unit_id':unit['unit_id'],'claim_text':unit['safe_sentence'] if valid else sentence_value['claim_text'],
                    'paragraph':sentence_value.get('paragraph',0)}
                if sentence_value.get('actor')!=unit['actor']:
                    row['actor']=sentence_value.get('actor')
                selected.append(row);covered.update(unit['record_ids'])
            for unit in units:
                if unit['unit_kind']=='core' and not any(row['unit_id'] == unit['unit_id'] for row in selected):
                    selected.append({'unit_id':unit['unit_id'],'claim_text':unit['safe_sentence'],'paragraph':1})
                    covered.update(unit['record_ids'])
            for unit in units:
                if unit.get('literal_quote_id') and not any(row['unit_id'] == unit['unit_id'] for row in selected):
                    selected.append({'unit_id': unit['unit_id'], 'claim_text': unit['safe_sentence'], 'paragraph': 1})
            if selected and next((u for u in units if u['unit_id'] == selected[0]['unit_id']), {}).get('unit_kind') != 'core':
                core_index = next((i for i, row in enumerate(selected) if next((u for u in units if u['unit_id'] == row['unit_id']), {}).get('unit_kind') == 'core'), None)
                if core_index is not None:
                    first = selected.pop(core_index); first['paragraph'] = 0; selected.insert(0, first)
            return {'sentences': selected, 'answer': '注入的自由回答已完成付款'}
        assert purpose == 'semantic_claim_verification'
        payload = resolve_judge_transport(payload)
        verdicts = []
        for claim in payload['claims']:
            # Exact source quotation is deliberately kept independent from
            # candidate numbers, negation and authorship. A positive model vote
            # cannot override the existing deterministic source gates.
            verdicts.append({'claim_id': claim['claim_id'], 'atoms': [{
                'text': claim['claim_text'], 'status': 'supported',
                'support_evidence_ids': claim['evidence_ids'], 'counter_evidence_ids': [],
                'source_quotes': [{'evidence_id': e,
                    'quote': claim['source_quote'] if claim['source_quote'] in by_id[e]['quote_text'] else by_id[e]['quote_text']}
                    for e in claim['evidence_ids']], 'missing_requirements': []}],
                'qualification_reviews': [{**q, 'applicability': 'applicable',
                    'rationale': '该完整来源段落直接限定此项表达。'}
                    for q in payload['qualification_checks'][claim['claim_id']]]})
        result = {'verdicts': verdicts, 'answer_completeness': {
            'complete': True, 'covered_record_ids': [row['record_id'] for row in payload['verified_records']],
            'reason': '可答事项、历史安排和必要的反证及条件均在回答句中保留。',
            'quote_reviews': [{'quote_id': row['quote_id'], 'material_for_objective': True,
                'preserved_in_answer': True, 'reason': '该条件或反证已在回答中明确保留。'}
                for row in payload['literal_checks']]}}
        return judge_edit(result, payload) if judge_edit else result

    runtime._model_call = model
    return runtime, state, calls, steps


def run(runtime, state, sources, objective='项目乙的交付问题如何回答？'):
    return prepare_narrative(runtime, 'turn', 'owner', state,
        {'mode': 'research', 'objective': objective, 'budget': {'max_context_tokens': 50000}}, sources, [])


def delivery_case():
    s0 = source('e0', '项目乙原计划交付日期为2026年10月15日。')
    s1 = source('e1', '项目乙书面安排\n双方确认交付日期为2026年10月18日。\n'
        '原计划10月15日、申请10月20日均不再采用。\n'
        '只有双方签署《验收报告》之日才视为交付完成。\n'
        '设备发运、物流签收均不构成交付完成。')
    s2 = source('e2', '项目乙截至2026年10月21日尚未收到设备，仍未交付、未验收。')
    conditions = s1['quote_text'].splitlines()[2:]
    records = [record(s0, s0['quote_text'], role='qualification'),
        record(s1, '双方确认交付日期为2026年10月18日。',
            quote='双方确认交付日期为2026年10月18日。', conditions=conditions),
        record(s2, s2['quote_text'])]
    records[1]['source_qualifications'] = [{'quote': conditions[1], 'evidence_id': 'e1',
        'char_start': s1['quote_text'].index(conditions[1]), 'attribution': 'authored'}]
    sentences = [sentence('书面确认的交付日期为2026年10月18日。', ['verified-2'], s1,
            quote='双方确认交付日期为2026年10月18日。'),
        sentence(conditions[0], ['verified-1', 'verified-2'], s1, quote=conditions[0]),
        sentence(conditions[1], ['verified-2'], s1, quote=conditions[1], paragraph=1),
        sentence(conditions[2], ['verified-2'], s1, quote=conditions[2], paragraph=1),
        sentence('邮件记载，' + s2['quote_text'], ['verified-3'], s2, paragraph=1)]
    sentences[1]['evidence_ids'] = ['e0', 'e1']
    return [s0, s1, s2], records, sentences


def test_direct_natural_answer_preserves_date_change_criterion_counter_and_unknown():
    sources, records, sentences = delivery_case()
    runtime, state, calls, steps = make_runtime(sources, records, sentences,
        unknowns=['实际完成日期', '实际完成证据'])
    before = deepcopy((sources, records, state['context_reviews']))
    answer = run(runtime, state, sources)
    assert answer['status'] == 'verified'
    assert answer['answer'].index('10月18日') < answer['answer'].index('10月15日')
    assert '10月20日均不再采用' in answer['answer']
    assert '实际完成日期' in answer['answer'] and '不足以确认' in answer['answer']
    assert '只有双方签署' in answer['answer'] and '不构成交付完成' in answer['answer']
    assert '10月21日尚未收到设备，仍未交付、未验收' in answer['answer']
    assert 'alice@example.com' not in answer['answer'] and '注入的自由回答' not in answer['answer']
    assert len(calls) == 1 and [c[0] for c in calls] == ['semantic_claim_verification']
    assert before == (sources, records, state['context_reviews'])
    assert '_full_scope_history_verified' not in state['context_reviews']
    assert steps[-1][0][1] == 'answer_narrative_audit'
    assert 'writer_raw' not in state['answer_narrative_audit']
    assert state['answer_narrative_audit']['generation_mode'] == 'host_bound_prototypes'
    assert len(state['answer_narrative_audit']['checked_sentences']) == 6


def test_quote_approval_is_not_payment_and_amount_omission_is_not_hidden():
    text = '项目乙报价为12000元，只有采购负责人书面批准后才生效。'
    s = source('e1', text)
    records = [record(s, text, conditions=[text])]
    good = sentence('邮件报价为12000元，只有采购负责人书面批准后才生效。', ['verified-1'], s)
    runtime, state, _, _ = make_runtime([s], records, [good], unknowns=['约定是否成立'])
    answer = run(runtime, state, [s], '项目乙报价是多少，是否已经生效？')
    assert answer['status'] == 'verified' and '12000元' in answer['answer']
    assert '只有采购负责人书面批准后才生效' in answer['answer']
    assert '约定是否成立仍属未知' in answer['answer']
    bad = {**good, 'claim_text': '邮件报价为18000元，只有采购负责人书面批准后才生效。'}
    runtime, state, _, _ = make_runtime([s], records, [bad])
    answer = run(runtime, state, [s], '项目乙报价是多少？')
    assert answer['status'] == 'verified' and '18000元' not in answer['answer']
    assert state['answer_narrative_audit']['generation_mode'] == 'host_bound_prototypes'


def test_person_obligation_condition_is_not_erased_by_positive_auxiliary_vote():
    s = source('e1', '项目乙由王经理负责复核，若财务批准后才能付款。')
    original = record(s, s['quote_text'], conditions=[s['quote_text']])
    bad = sentence('项目乙由王经理负责复核并已经付款。', ['verified-1'], s)
    runtime, state, _, _ = make_runtime([s], [original], [bad])
    answer = run(runtime, state, [s], '项目乙谁负责，能否直接付款？')
    assert answer['status'] == 'verified'
    assert '已经付款' not in answer['answer']
    assert original['semantic_status'] == 'supported'


def test_conflicting_authors_are_distinct_and_never_upgraded_by_narrative_success():
    a = source('a', '项目乙我方同意按原方案实施。')
    b = source('b', '项目乙我方不同意按原方案实施。', actor='bob@example.com')
    records = [record(a, a['quote_text']), record(b, b['quote_text'], actor='bob@example.com')]
    sentences = [sentence('一方邮件同意项目乙按原方案实施。', ['verified-1'], a),
        sentence('另一方邮件不同意项目乙按原方案实施。', ['verified-2'], b, actor='bob@example.com')]
    runtime, state, _, _ = make_runtime([a, b], records, sentences)
    state['answer_synthesis']['relationship_warnings'] = [{'code': 'uncertain_thread_links'}]
    result = run(runtime, state, [a, b], '项目乙双方是否达成一致？')
    assert result['status'] == 'verified' and '同意项目乙' not in result['answer'] and '不同意按原方案实施' in result['answer']
    assert runtime._sufficiency([a, b], {'research': {'answer_synthesis': {'status': 'complete',
        'graph_history_complete': False}}}, {}) == 'partial'


@pytest.mark.parametrize('bad_actor', [None, '', 'bob@example.com'])
def test_body_sentence_requires_its_own_authored_from_even_with_valid_quote(bad_actor):
    s = source('e1', '项目乙交付日期为2026年11月3日。')
    runtime, state, calls, _ = make_runtime([s], [record(s, s['quote_text'], actor=bad_actor)],
        [sentence(s['quote_text'], ['verified-1'], s, actor=bad_actor)])
    result = run(runtime, state, [s])
    if bad_actor is None:
        assert result['status'] == 'verified' and len(calls) == 1
        assert result['sentences'][0]['actor'] == 'alice@example.com'
    else:
        assert result['status'] == 'fallback' and not calls
        assert state['answer_narrative_audit']['reason'] == 'verified_record_source_unit_unavailable'


def test_impersonal_attachment_criterion_uses_no_imputed_sender():
    s = source('e1', '项目乙只有双方签署报告之日才视为交付完成。', kind='attachment')
    row = record(s, s['quote_text'], actor=None, conditions=[s['quote_text']])
    runtime, state, _, _ = make_runtime([s], [row], [sentence(s['quote_text'], ['verified-1'], s, actor=None)])
    assert run(runtime, state, [s])['status'] == 'verified'
    personal = source('e2', '项目乙供应商若延期须承担责任。', kind='attachment')
    runtime, state, calls, _ = make_runtime([personal], [record(personal, personal['quote_text'], actor=None)],
        [sentence(personal['quote_text'], ['verified-1'], personal, actor=None)])
    assert run(runtime, state, [personal])['status'] == 'fallback' and not calls


@pytest.mark.parametrize('failure', ['missing_check', 'missing_record', 'missing_literal', 'duplicate_literal', 'condition_only', 'not_complete'])
def test_missing_or_ambiguous_answer_completeness_rejects_entire_new_paragraph(failure):
    sources, records, sentences = delivery_case()

    def mutate(value, payload):
        check = value['answer_completeness']
        if failure == 'missing_check':
            value.pop('answer_completeness')
        elif failure == 'missing_record':
            check['covered_record_ids'].pop()
        elif failure == 'missing_literal':
            check['quote_reviews'].pop()
        elif failure == 'duplicate_literal':
            check['quote_reviews'][-1] = deepcopy(check['quote_reviews'][0])
        elif failure == 'condition_only':
            check['quote_reviews'][0]['preserved_in_answer'] = False
        else:
            check['complete'] = False
        return value

    runtime, state, calls, _ = make_runtime(sources, records, sentences, judge_edit=mutate)
    answer = run(runtime, state, sources)
    assert answer['status'] == 'fallback' and len(calls) == 1
    assert state['answer_narrative_audit']['reason'] == 'answer_completeness_unproven'
    # The conservative answer still shows relevant literal business limits.
    assert '不再采用' in answer['answer'] and '只有双方签署' in answer['answer']
    assert '不构成交付完成' in answer['answer']


def test_new_package_final_claim_still_fails_original_history_gate():
    s = source('e1', '项目乙交付日期为2026年11月3日。')
    runtime, state, calls, _ = make_runtime([s], [record(s, s['quote_text'])],
        [sentence('项目乙最终交付日期为2026年11月3日。', ['verified-1'], s)])
    answer = run(runtime, state, [s])
    assert answer['status'] == 'verified' and len(calls) == 1
    assert state['answer_narrative_audit']['generation_mode'] == 'host_bound_prototypes'
    assert '最终' not in answer['answer']


@pytest.mark.parametrize('failure', ['calls', 'tokens', 'deadline', 'tool', 'unread', 'context', 'changed', 'missing_review'])
def test_no_extra_calls_when_original_context_or_two_call_budget_is_unavailable(failure):
    sources, records, sentences = delivery_case()
    runtime, state, calls, _ = make_runtime(sources, records, sentences)
    if failure == 'calls':
        state['max_model_calls'] = 0
    elif failure == 'tokens':
        state['token_usage'] = init_usage({'max_tokens': 100})
    elif failure == 'deadline':
        state['deadline_reached'] = True
    elif failure == 'tool':
        state['tool_limit_reached'] = True
    elif failure == 'unread':
        state['context_reviews']['mail-e2'].update(complete=False, gaps=['attachment_text_unavailable'])
    elif failure == 'context':
        runtime.context_limit = 1000
    elif failure == 'missing_review':
        state['context_reviews'] = {}
    else:
        state['source_changed'] = True
    answer = run(runtime, state, sources)
    assert answer['status'] == 'fallback' and not calls
    assert '10月18日' in answer['answer'] and '不再采用' in answer['answer']


def test_unverified_findings_and_unknown_assertions_are_never_displayed_as_facts():
    state = {'answer_synthesis': {'status': 'unavailable',
        'unanswered_subquestions': ['项目乙已付款']}, 'findings': [
        {'text_zh': '项目乙已经付款', 'status': 'needs_review', 'semantic_status': 'insufficient'}]}
    result = fallback_presentation(state, [{'claim_text': '项目乙已经验收', 'state': 'asserted',
        'support_status': 'supported', 'semantic_status': 'insufficient'}])
    assert '已经付款' not in result['answer'] and '已经验收' not in result['answer']
    assert '项目乙已付款' not in result['answer'] and '所问结论' in result['answer']


def test_complex_fallback_uses_only_semantic_and_context_verified_findings():
    s = source('e1', '项目乙由王经理负责复核。')
    state = {'answer_synthesis': {'status': 'unavailable'}, 'findings': [{
        'text_zh': s['quote_text'], 'evidence_id': 'e1', 'source_quote': s['quote_text'],
        'status': 'context_verified', 'semantic_status': 'supported',
        'context_verification': {'support_status': 'supported'}}]}
    result = fallback_presentation(state, evidence=[s])
    assert s['quote_text'] in result['answer']
    assert '邮件记录' in result['answer']


def test_new_narrative_and_old_evidence_are_separate_runtime_fields():
    sources, records, sentences = delivery_case()
    runtime, state, _, _ = make_runtime(sources, records, sentences, unknowns=['实际完成日期'])
    presentation = run(runtime, state, sources)
    final = runtime._answer({'mode': 'research'}, sources, {}, {'matched': 3}, 0, state, [])
    assert final['answer'] == presentation['answer']
    assert '邮件记载' in final['evidence_appendix']['recorded_answer']
    assert 'alice@example.com' in final['evidence_appendix']['recorded_answer']
    assert 'alice@example.com' not in final['answer']
    assert final['answer_presentation']['status'] == 'verified'
    assert final['answer_narrative_audit']['original_verdicts_unchanged'] is True


def test_output_reservations_include_whole_writer_and_completeness_response():
    sources, records, sentences = delivery_case()
    runtime, state, calls, _ = make_runtime(sources, records, sentences)
    run(runtime, state, sources)
    assert narrative_output_tokens(calls[0][1]) >= 2800
    from services.mail.research.claim_verifier import judge_output_tokens
    assert narrative_judge_output_tokens(calls[0][1]) > judge_output_tokens(calls[0][1])


def test_judge_condition_and_qualification_interning_preserves_every_original_tuple():
    payload = {'claims': [{'claim_id': 'first', 'conditions': ['若获得批准才生效', '只有签署报告才完成']},
        {'claim_id': 'second', 'conditions': ['只有签署报告才完成', '若获得批准才生效']}],
        'qualification_checks': {
            'first': [{'evidence_id': 'e1', 'quote': '只有签署报告才完成', 'char_start': 56},
                {'evidence_id': 'e1', 'quote': '若获得批准才生效', 'char_start': 10}],
            'second': [{'evidence_id': 'e1', 'quote': '若获得批准才生效', 'char_start': 10},
                {'evidence_id': 'e1', 'quote': '只有签署报告才完成', 'char_start': 56}]}}
    before = deepcopy(payload)
    packed = pack_judge_transport(payload)
    resolved = resolve_judge_transport(packed)
    assert payload == before
    assert resolved['claims'] == before['claims']
    assert resolved['qualification_checks'] == before['qualification_checks']
    assert len(packed['condition_texts']) == 2 and len(packed['qualification_check_pool']) == 2


def test_participant_sharing_preserves_addresses_roles_order_and_missing_metadata():
    participants = [{'role': 'from', 'normalized_address': 'alice@example.com'},
        {'role': 'cc', 'normalized_address': 'bob@example.com'}]
    original = {'source_documents': {'d1': {'participants': participants, 'sent_at_utc': '2026-11-01'},
        'd2': {'participants': deepcopy(participants), 'sent_at_utc': '2026-11-02'},
        'd3': {'message_key': 'metadata-missing'}}}
    before = deepcopy(original)
    packed = intern_source_participants(original)
    assert len(packed['participant_groups']) == 1
    assert resolve_source_participants(packed) == before == original


def test_english_writer_sentence_cannot_become_a_chinese_answer():
    s = source('e1', '项目乙金额为12000元。')
    runtime, state, calls, _ = make_runtime([s], [record(s, s['quote_text'])],
        [sentence('The amount is 12000.', ['verified-1'], s)])
    assert run(runtime, state, [s])['status'] == 'verified' and len(calls) == 1
    assert 'The amount' not in state['answer_presentation']['answer']
