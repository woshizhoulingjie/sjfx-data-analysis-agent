"""A missing item price cannot become a zero/free explanation in fast mode."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from services.mail.research.answer_synthesis import synthesize_fast_overview
from services.mail.research.narrative import prepare_narrative


def setup(overview, *, extra_source='', blocked=False):
    texts = ['采购合计120元，共3项；尚未实际付款。第三项没有单独金额记录。' + extra_source,
             'item,amount\nA,40\nB,80\n', '尚未付款，请补充第三项明细。']
    if blocked:
        texts = [text + '\n' + 'source ' * 500 for text in texts]
    evidence = [{'message_key': 'm' + str(i), 'quote_text': text} for i, text in enumerate(texts)]
    state = {'model_calls': 0, 'max_model_calls': 10, 'token_usage': {'total_tokens': 0},
             'findings': [], 'answer_mode': 'fast'}
    calls, events = [], []

    def model(tid, owner, current, purpose, system, prompt, **kwargs):
        current['model_calls'] += 1
        current['token_usage']['total_tokens'] += 10
        calls.append((purpose, system, json.loads(prompt)))
        return {'notes': overview} if purpose == 'fast_overview_block' else {'overview': overview}

    runtime = SimpleNamespace(_model_call=model, _publish_progress=lambda *a: None,
        repository=SimpleNamespace(event=lambda *a: events.append(a), record_research_step=lambda *a, **kw: None))
    if blocked:
        runtime.fast_overview_cap = 3000
    return runtime, state, evidence, calls, events


@pytest.mark.parametrize('guess', [
    '第三项金额为0元。', '第三项隐含金额为零。', '第三项可能免费或属于赠品。',
    '第三项为赠品。', '第三项金额为0。', '第三项零费用。', '第三项价格$0。',
])
def test_unsupported_zero_or_gift_uses_existing_fallback_without_more_calls(guess):
    runtime, state, evidence, calls, events = setup('采购合计120元，共3项，尚未付款。' + guess)
    before = deepcopy(evidence)
    spec = {'objective': '采购金额、项数和付款状态', 'mode': 'research'}
    assert not synthesize_fast_overview(runtime, 't', 'o', state, spec, evidence)
    assert 'fast_overview' not in state and 'answer_presentation' not in state
    assert events[-1][1:] == ('fast_overview_rejected', {'reason': 'unsupported_zero_cost_explanation'})
    presentation = prepare_narrative(runtime, 't', 'o', state, spec, evidence, [])
    assert presentation['status'] == 'fallback' and guess not in presentation['answer']
    assert len(calls) == state['model_calls'] == 1
    assert state['token_usage']['total_tokens'] == 10 and evidence == before


@pytest.mark.parametrize('original', [
    '第三项金额0元，为赠品。', '第三项免费。', '第三项金额0元。',
    '第三项金额为0。', '第三项价格为零。', '采购明细：\n第三项金额0元。',
])
def test_original_zero_or_free_clause_can_be_published(original):
    runtime, state, evidence, calls, _ = setup(original, extra_source=original)
    assert synthesize_fast_overview(runtime, 't', 'o', state, {'objective': '采购情况'}, evidence)
    assert state['answer_presentation']['answer'] == original
    assert len(calls) == state['model_calls'] == 1


@pytest.mark.parametrize(('original', 'overview'), [
    ('会议于10:00开始。', '会议安排在10:00。'),
    ('会议开始时间为00:00。', '会议安排在00:00。'),
    ('检索结果数量：0条。', '本轮检索结果0条。'),
    ('重试次数：0次。', '处理完成，重试0次。'),
    ('待办数量：0项。', '目前有0项待办。'),
])
def test_time_and_non_monetary_zero_do_not_require_literal_zero_cost_support(original, overview):
    # Each paraphrase differs from its source, so the former bare-zero guard
    # would reject it. This monetary guard must leave those facts untouched.
    runtime, state, evidence, calls, _ = setup(overview, extra_source=original)
    assert synthesize_fast_overview(runtime, 't', 'o', state, {'objective': '邮件情况'}, evidence)
    assert state['answer_presentation']['answer'] == overview
    assert len(calls) == state['model_calls'] == 1


@pytest.mark.parametrize(('original', 'overview'), [
    ('若采购获批：\n第三项为赠品。', '第三项为赠品。'),
    ('若采购获批：\r\n第三项为赠品。', '第三项为赠品。'),
    ('若采购获批：\n\n第三项为赠品。', '第三项为赠品。'),
    ('若采购获批：\n第三项金额0元。', '第三项金额0元。'),
])
def test_cross_line_conditions_cannot_support_unconditional_zero_or_gift(original, overview):
    runtime, state, evidence, calls, _ = setup(overview, extra_source=original)
    assert not synthesize_fast_overview(runtime, 't', 'o', state, {'objective': '采购情况'}, evidence)
    assert 'answer_presentation' not in state and 'fast_overview' not in state
    assert len(calls) == state['model_calls'] == 1


def test_cross_line_condition_can_be_preserved_in_full_original_sentence():
    original = '若采购获批：\n第三项为赠品。'
    runtime, state, evidence, calls, _ = setup(original, extra_source=original)
    assert synthesize_fast_overview(runtime, 't', 'o', state, {'objective': '采购情况'}, evidence)
    assert state['answer_presentation']['answer'] == original and len(calls) == 1


@pytest.mark.parametrize('original', [
    '第四项金额0元，为赠品。', '第三项不是赠品。', '第三项为赠品？', '第三项若获批则为赠品。',
    '若获批；第三项为赠品。',
])
def test_other_item_negation_question_and_condition_do_not_support_free_claim(original):
    runtime, state, evidence, calls, _ = setup('第三项为赠品。', extra_source=original)
    assert not synthesize_fast_overview(runtime, 't', 'o', state, {'objective': '采购情况'}, evidence)
    assert len(calls) == 1 and 'answer_presentation' not in state


def test_block_notes_cannot_create_support_for_gift_and_do_not_trigger_retry():
    runtime, state, evidence, calls, _ = setup('第三项为赠品。', blocked=True)
    assert not synthesize_fast_overview(runtime, 't', 'o', state, {'objective': '采购情况'}, evidence)
    assert [purpose for purpose, _, _ in calls] == ['fast_overview_block'] * 3 + ['fast_overview']
    assert state['model_calls'] == 4 and state['token_usage']['total_tokens'] == 40
    assert 'answer_presentation' not in state


def test_missing_amount_stays_unknown_and_existing_call_budget_is_preserved():
    answer = '采购合计120元，共3项，尚未付款。第三项金额未知，需补充明细。'
    runtime, state, evidence, calls, _ = setup(answer)
    assert synthesize_fast_overview(runtime, 't', 'o', state, {'objective': '采购情况'}, evidence)
    assert state['answer_presentation']['answer'] == answer and len(calls) == 1
    state['max_model_calls'] = state['model_calls'] + 4
    assert not synthesize_fast_overview(runtime, 't', 'o', state, {'objective': '采购情况'}, evidence)
    assert len(calls) == 1 and state['model_calls'] == 1
