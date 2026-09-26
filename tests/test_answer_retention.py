import copy

import pytest

from services import answer_quality as aq
from services.conversation import ConversationScope, ConversationSession
from services.storage import Storage
from services.turn_runtime import AnalysisTurnRuntime


QUESTION = '实收多少台、还缺多少台？'
SOURCES = [
    {'evidence_id': 'EV-COUNT', 'source_path': 'delivery.txt',
     'text': '现场记录显示，实收13台，尚缺6台。设备尚未完成通电联合调试。',
     'label': 'paragraph', 'location': {'page': 1}},
    {'evidence_id': 'EV-TERM', 'source_path': 'contract.txt',
     'text': '合同明确约定，需要完成书面验收后支付尾款，验收负责人需要签字。',
     'label': 'paragraph', 'location': {'page': 2}},
]
GOOD = '实收13台[1]。尚缺6台[1]。需要完成书面验收后支付尾款[2]。'
BAD = '实收99台[1]。'
SHORT = '需要完成书面验收后支付尾款[2]。'


def draft(answer, sources=None, reverse=False):
    sources = copy.deepcopy(SOURCES if sources is None else sources)
    if reverse:
        sources.reverse()
    citations = [dict(source, original_text=source['text'], citation_index=i,
                      citation_label=f'[{i}]') for i, source in enumerate(sources, 1)]
    return {'answer': answer, 'citations': citations, 'status': 'answered',
            'question': QUESTION, 'resolved_query': QUESTION, 'warnings': [],
            'evidence_status': 'supported', 'coverage': {'query_coverage': 1.0},
            'input_manifest': {'answer_contract': aq.answer_contract(QUESTION, citations)}}


class Planner:
    def plan(self, question, scope, memory=None):
        return {'objective': question, 'scope': scope, 'modes': ['retrieval'],
                'steps': [], 'constraints': [], 'query_variants': [question]}


class Engine:
    def __init__(self, drafts, repair_sources=None):
        self.drafts = drafts
        self.calls = 0
        self.repair_sources = repair_sources
        self.retriever = self

    def retrieve(self, request):
        sources = self.repair_sources if self.calls and self.repair_sources is not None else SOURCES
        return {'results': copy.deepcopy(sources), 'coverage': {'query_coverage': 1.0}}

    def ask(self, session, question, **kwargs):
        value = copy.deepcopy(self.drafts[min(self.calls, len(self.drafts) - 1)])
        self.calls += 1
        return value


def run_case(tmp_path, drafts, repair_sources=None, attempts=1, scope=None, cancel_check=None):
    scope = scope or ConversationScope('package')
    storage = Storage(tmp_path/'agent.db', tmp_path/'sidecars')
    session = ConversationSession(scan_id='scan-retention', scope=scope,
        session_id='session-retention', title='修订事实保留')
    storage.save_conversation(session.as_dict(), 'owner')
    turn, _ = storage.create_conversation_turn(session.session_id, session.scan_id, 'owner',
        QUESTION, scope.as_dict(), idempotency_key='retention')
    engine = Engine(drafts, repair_sources)
    AnalysisTurnRuntime(storage, engine, planner=Planner(), max_revision_attempts=attempts,
        cancel_check=cancel_check).execute(turn, session, scope, ['delivery.txt', 'contract.txt'])
    return storage.get_conversation_turn(turn['id'], 'owner'), storage, engine


def test_repair_cannot_erase_previously_verified_counts(tmp_path):
    record, storage, engine = run_case(tmp_path, [draft(GOOD+BAD), draft(SHORT)])
    answer = record['result']['answer']
    assert '实收13台' in answer and '尚缺6台' in answer
    assert '实收99台' not in answer
    assert record['verification']['missing_answer_aspects'] == []
    assert engine.calls == 2
    restored = storage.get_conversation('session-retention', 'owner', scan_id='scan-retention')
    assert restored['messages'][-1]['content'] == answer


def test_better_repair_is_preferred_without_rewriting_citations(tmp_path):
    improved = draft(GOOD)
    record, _, engine = run_case(tmp_path, [draft('实收13台[1]。'+BAD), improved])
    assert record['result']['answer'] == GOOD
    assert record['result']['citations'] == improved['citations']
    assert engine.calls == 2


def test_guarded_quality_not_raw_length_controls_selection(tmp_path):
    record, _, _ = run_case(tmp_path, [draft(BAD*8), draft('实收13台[1]。尚缺6台[1]。')])
    assert '实收13台' in record['result']['answer']
    assert '实收99台' not in record['result']['answer']


def test_retained_candidate_keeps_its_own_citation_numbers(tmp_path):
    record, _, _ = run_case(tmp_path, [draft(GOOD+BAD),
        draft('需要完成书面验收后支付尾款[1]。', reverse=True)])
    assert '实收13台[1]' in record['result']['answer']
    assert record['result']['citations'][0]['source_path'] == 'delivery.txt'
    assert record['result']['citations'][1]['source_path'] == 'contract.txt'


@pytest.mark.parametrize('change', ['body', 'added', 'translation', 'preview', 'location'])
def test_changed_or_added_evidence_blocks_older_candidate(tmp_path, change):
    sources = copy.deepcopy(SOURCES)
    if change == 'body':
        sources[0]['text'] = sources[0]['text'].replace('13台', '14台')
    elif change == 'added':
        sources.append({'evidence_id': 'EV-CORRECTION', 'source_path': 'correction.txt',
            'text': '更正通知说明此前数量记录错误，实际收到14台，需要重新核实。'})
    elif change == 'translation':
        sources[0]['translated_text'] = '更正记录显示已收到14台。'
    elif change == 'preview':
        sources[0]['preview_only'] = True
    else:
        sources[0]['location'] = {'page': 3}
    record, _, _ = run_case(tmp_path, [draft(GOOD+BAD), draft(SHORT, sources)], sources)
    assert '实收13台' not in record['result']['answer']


def test_raw_repair_search_change_is_not_hidden_by_merge(tmp_path):
    sources = copy.deepcopy(SOURCES)
    sources[0]['text'] = sources[0]['text'].replace('13台', '14台')
    # Even if the composer or retrieval merge keeps old citations, newly
    # observed contrary source content must invalidate the old candidate.
    record, _, _ = run_case(tmp_path, [draft(GOOD+BAD), draft(SHORT)], sources)
    assert '实收13台' not in record['result']['answer']


def test_changed_package_revision_blocks_old_candidate(tmp_path):
    first, second = draft(GOOD+BAD), draft(SHORT)
    first['package_reading'] = {'source_digest': 'old'}
    second['package_reading'] = {'source_digest': 'new'}
    record, _, _ = run_case(tmp_path, [first, second])
    assert '实收13台' not in record['result']['answer']


def test_empty_repair_search_does_not_erase_existing_evidence(tmp_path):
    record, _, _ = run_case(tmp_path, [draft(GOOD+BAD), draft(SHORT)], [])
    assert '实收13台' in record['result']['answer']
    assert '尚缺6台' in record['result']['answer']


def test_no_extra_generation_when_revision_disabled(tmp_path):
    record, _, engine = run_case(tmp_path, [draft(GOOD+BAD)], attempts=0)
    assert engine.calls == 1
    assert '实收13台' in record['result']['answer']
    assert '实收99台' not in record['result']['answer']


def test_two_attempts_can_select_middle_candidate(tmp_path):
    middle = draft(GOOD+BAD)
    record, _, engine = run_case(tmp_path, [draft(BAD), middle, draft(SHORT)], attempts=2)
    assert engine.calls == 3
    assert '实收13台' in record['result']['answer'] and '尚缺6台' in record['result']['answer']
    assert '实收99台' not in record['result']['answer']


def test_retained_result_and_verification_are_persisted_consistently(tmp_path):
    record, storage, engine = run_case(tmp_path, [draft(GOOD+BAD), draft(SHORT)])
    verification = record['verification']
    selection = verification['candidate_selection']
    assert selection['selected_attempt'] == 0
    assert selection['retained_previous'] is True
    assert selection['model_calls_added'] == 0
    assert selection['citation_sets_spliced'] is False
    assert verification['failed_claims'] == []
    assert verification['revision_attempts'] == 1
    assert record['result']['citations'] == draft(GOOD)['citations']
    assert not any(c.get('status') == 'unsupported'
                   for c in verification['ledger']['claims'])
    restored = storage.get_conversation('session-retention', 'owner', scan_id='scan-retention')
    assert restored['messages'][-1]['content'] == record['result']['answer']
    assert engine.calls == 2


def test_quality_switch_disables_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(aq.Config, 'CONVERSATION_QUALITY_ENABLED', False)
    record, _, engine = run_case(tmp_path, [draft(GOOD+BAD), draft(SHORT)])
    assert 'candidate_selection' not in record['verification']
    assert '实收13台' not in record['result']['answer']
    assert engine.calls == 2


def test_invalid_generated_labels_prevent_retention(tmp_path):
    first = draft(GOOD+BAD)
    first['input_manifest']['invalid_generated_citation_indexes'] = [99]
    record, _, _ = run_case(tmp_path, [first, draft(SHORT)])
    assert '实收13台' not in record['result']['answer']


def test_manifest_revision_change_prevents_retention(tmp_path):
    first, second = draft(GOOD+BAD), draft(SHORT)
    first['input_manifest']['package_reading'] = {'source_digest': 'old'}
    second['input_manifest']['package_reading'] = {'source_digest': 'new'}
    record, _, _ = run_case(tmp_path, [first, second])
    assert '实收13台' not in record['result']['answer']


@pytest.mark.parametrize('citations', [[], None])
def test_missing_latest_citations_prevent_retention(tmp_path, citations):
    last = draft(SHORT)
    last['citations'] = citations
    record, _, _ = run_case(tmp_path, [draft(GOOD+BAD), last])
    assert '实收13台' not in record['result']['answer']


@pytest.mark.parametrize('retrieval', [None, {}, {'results': None},
    {'results': [None]}, {'results': [{}]}, {'results': SOURCES, 'error': 'timeout'},
    {'results': SOURCES, 'status': 'cancelled'}])
def test_uncertain_evidence_fails_closed(retrieval):
    retention = pytest.importorskip('services.answer_retention')
    assert retention.evidence_state(retrieval) is None
    assert retention.advance_evidence_state(retention.evidence_state({'results': SOURCES}), retrieval) is None


def selection_candidates(first=None, last=None):
    retention = pytest.importorskip('services.answer_retention')
    state = retention.evidence_state({'results': SOURCES})
    plan = {'scope': ConversationScope('package').as_dict()}
    return [retention.capture_candidate(value, {}, plan, {}, state)
            for value in (first or draft(GOOD), last or draft(SHORT))]


def test_cancellation_interrupts_candidate_revalidation():
    retention = pytest.importorskip('services.answer_retention')
    from services.claim_verifier import ClaimVerifier
    class JobCancelled(Exception):
        pass
    def cancel():
        raise JobCancelled()
    with pytest.raises(JobCancelled):
        retention.select_candidate(selection_candidates(), ClaimVerifier(), QUESTION, [], {}, cancel)


def test_scope_change_prevents_retention():
    retention = pytest.importorskip('services.answer_retention')
    from services.claim_verifier import ClaimVerifier
    candidates = selection_candidates()
    candidates[-1]['plan']['scope'] = {'kind': 'files', 'source_paths': ['contract.txt']}
    selected = retention.select_candidate(candidates, ClaimVerifier(), QUESTION, [], {}, lambda: None)
    assert selected['verification']['candidate_selection']['selected_attempt'] == 1
    assert selected['verification']['candidate_selection']['candidates'][0]['reason'] == 'scope_changed'


def test_tied_quality_prefers_latest_whole_candidate():
    retention = pytest.importorskip('services.answer_retention')
    from services.claim_verifier import ClaimVerifier
    candidates = selection_candidates(draft(GOOD), draft(GOOD))
    candidates[-1]['result']['marker'] = 'latest'
    selected = retention.select_candidate(candidates, ClaimVerifier(), QUESTION, [], {}, lambda: None)
    assert selected['result']['marker'] == 'latest'
    assert selected['verification']['candidate_selection']['selected_attempt'] == 1
