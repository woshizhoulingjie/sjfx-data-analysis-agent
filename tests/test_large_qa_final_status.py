"""A finished worker must not promote an incomplete answer to fulfilled."""
import copy

import pytest

from services.conversation import ConversationScope, ConversationSession
from services.storage import Storage
from services.turn_runtime import AnalysisTurnRuntime


BODY = '验收需要完成热测试。'
SOURCE = {'evidence_id': 'EV-1', 'source_path': 'a.txt', 'text': BODY}


def draft(**updates):
    result = {
        'status': 'answered', 'evidence_status': 'supported',
        'answer': BODY + '[1]。', 'question': '请总结整个数据包。',
        'citations': [dict(SOURCE, original_text=BODY, citation_index=1)],
        'coverage': {'query_coverage': 1.0}, 'warnings': [],
    }
    result.update(copy.deepcopy(updates))
    return result


class Planner:
    def __init__(self, mode):
        self.mode = mode

    def plan(self, question, scope, memory=None):
        return {'objective': question, 'scope': scope, 'modes': [self.mode],
                'steps': [], 'query_variants': [question], 'constraints': []}


class Engine:
    def __init__(self, drafts):
        self.drafts = drafts
        self.calls = 0
        self.retriever = self

    def retrieve(self, request):
        return {'results': [copy.deepcopy(SOURCE)], 'coverage': {'query_coverage': 1.0}}

    def ask(self, session, question, **kwargs):
        result = copy.deepcopy(self.drafts[min(self.calls, len(self.drafts) - 1)])
        self.calls += 1
        return result


class Verifier:
    def __init__(self, status='verified', *, repair_first=False):
        self.status = status
        self.repair_first = repair_first
        self.calls = 0
        self.guard_calls = 0
        self.seen_statuses = []

    def verify(self, result, plan, **kwargs):
        self.calls += 1
        self.seen_statuses.append(result['status'])
        unsupported = '错误' in result['answer']
        needs_revision = unsupported or (self.repair_first and self.calls == 1)
        status = 'partial' if needs_revision else self.status
        return {'status': status, 'needs_revision': needs_revision,
                'warnings': [], 'failed_claims': [], 'missing_answer_aspects': [],
                'quality_metrics': {'verification_status': status},
                'ledger': {'claims': [{'text': BODY,
                                      'status': 'unsupported' if unsupported else 'supported'}]}}

    def guard_result(self, result, verification):
        self.guard_calls += 1
        # Reproduce the legacy guard's unconditional answer-status upgrade;
        # runtime must enforce completeness even when a local helper does so.
        return dict(copy.deepcopy(result), answer=BODY + '[1]。',
                    status='answered', evidence_status='supported')


@pytest.fixture
def run_turn(tmp_path, monkeypatch):
    monkeypatch.setattr('services.turn_runtime.aq.enabled', lambda: False)

    def run(drafts, verifier=None, *, mode='summary', attempts=0):
        storage = Storage(tmp_path / 'turn.db', tmp_path / 'sidecars')
        session = ConversationSession(scan_id='lp-final-status', scope=ConversationScope('package'),
                                      session_id='final-session', title='最终状态')
        storage.save_conversation(session.as_dict(), 'owner')
        turn, _ = storage.create_conversation_turn(session.session_id, session.scan_id, 'owner',
                                                   '请总结整个数据包。', session.scope.as_dict())
        engine = Engine(drafts)
        verifier = verifier or Verifier()
        outcome = AnalysisTurnRuntime(storage, engine, planner=Planner(mode), verifier=verifier,
                                      max_revision_attempts=attempts).execute(
            turn, session, session.scope, ['a.txt'])
        return outcome, storage.get_conversation_turn(turn['id'], 'owner'), engine, verifier

    return run


@pytest.mark.parametrize('updates,gap', [
    ({'input_manifest': {'large_qa': {'global_mode': True, 'synthesis_complete': False}}},
     'synthesis_incomplete'),
    ({'input_manifest': {'large_qa': {'global_mode': True, 'map_complete': False}}},
     'synthesis_incomplete'),
    ({'input_manifest': {'final_synthesis_complete': False}}, 'final_synthesis_incomplete'),
    ({'input_manifest': {'large_qa': {'global_mode': True, 'synthesis_complete': True,
                                    'source_snapshot_current': False}}}, 'source_snapshot_changed'),
    ({'package_reading': {'files_read': 1040, 'complete': False, 'files_without_summary': 8}},
     'package_reading_incomplete'),
    ({'input_manifest': {'package_reading': {'files_read': 1040, 'complete': False}}},
     'package_reading_incomplete'),
    ({'input_manifest': {'output_truncated': True}}, 'output_truncated'),
    ({'input_manifest': {'finish_reason': 'length'}}, 'output_truncated'),
    ({'input_manifest': {'invalid_generated_citation_indexes': [22, 68]}},
     'invalid_generated_citations'),
    ({'promotion_request': {'required': True, 'candidate_paths': []}}, 'promotion_pending'),
    ({'promotion_limit_reached': True}, 'promotion_pending'),
    ({'evidence_status': 'partial'}, 'evidence_not_fully_supported'),
    ({'evidence_status': 'unverified'}, 'evidence_not_fully_supported'),
])
def test_remaining_final_candidate_gap_cannot_be_answered(run_turn, updates, gap):
    outcome, record, _, _ = run_turn([draft(**updates)])
    assert outcome['status'] == record['status'] == 'completed'
    assert record['result']['status'] == 'partial'
    assert record['result']['task_status'] == 'partially_fulfilled'
    assert record['result']['evidence_status'] in {'partial', 'unverified'}
    assert gap in record['result']['quality_metrics']['answer_completion_gaps']
    assert record['result']['answer'] == BODY + '[1]。'


@pytest.mark.parametrize('status', ['partial', 'insufficient_evidence'])
def test_nonverified_final_answer_is_partial_even_without_revision_request(run_turn, status):
    _, record, _, _ = run_turn([draft()], Verifier(status))
    assert record['result']['status'] == 'partial'
    assert record['result']['task_status'] == 'partially_fulfilled'
    assert record['result']['citation_verification_status'] == status
    assert 'verification_incomplete' in record['result']['quality_metrics']['answer_completion_gaps']


def test_guard_cannot_upgrade_initial_incomplete_global_answer(run_turn):
    result = draft(status='partial', answer='错误事实[1]。', input_manifest={
        'large_qa': {'global_mode': True, 'synthesis_complete': False},
        'final_synthesis_complete': False,
    })
    _, record, _, verifier = run_turn([result])
    assert verifier.guard_calls == 1
    assert record['verification']['status'] == 'verified'
    assert record['result']['status'] == 'partial'
    assert record['result']['task_status'] == 'partially_fulfilled'
    assert record['result']['scope_completeness'] == 'candidate_only'


def test_real_verifier_guard_keeps_global_answer_partial(run_turn):
    from services.claim_verifier import ClaimVerifier

    class ObservedVerifier(ClaimVerifier):
        def __init__(self):
            self.guard_calls = 0

        def guard_result(self, result, verification):
            self.guard_calls += 1
            return super().guard_result(result, verification)

    verifier = ObservedVerifier()
    result = draft(status='partial', answer=BODY + '[1]。热测试需要99天[1]。',
                   input_manifest={'large_qa': {'global_mode': True, 'synthesis_complete': False}})
    _, record, _, _ = run_turn([result], verifier)
    assert verifier.guard_calls == 1
    assert '99天' not in record['result']['answer']
    assert record['result']['status'] == 'partial'
    assert record['result']['task_status'] == 'partially_fulfilled'


def test_retention_guard_cannot_upgrade_incomplete_selected_answer(run_turn, monkeypatch):
    monkeypatch.setattr('services.turn_runtime.aq.enabled', lambda: True)
    result = draft(status='partial', answer='错误事实[1]。', input_manifest={
        'large_qa': {'global_mode': True, 'synthesis_complete': False}})
    _, record, engine, verifier = run_turn([result, result], attempts=1)
    assert engine.calls == 2
    assert verifier.guard_calls >= 1
    assert 'candidate_selection' in record['verification']
    assert record['verification']['status'] == 'verified'
    assert record['result']['status'] == 'partial'
    assert record['result']['task_status'] == 'partially_fulfilled'


def test_completion_helper_cannot_upgrade_remaining_generation_gap(run_turn, monkeypatch):
    calls = []

    def complete(result, verification, *args, **kwargs):
        calls.append(True)
        return dict(result, status='answered', evidence_status='supported'), verification

    monkeypatch.setattr('services.turn_runtime.complete_quantities', complete)
    _, record, _, _ = run_turn([draft(status='partial', input_manifest={'output_truncated': True})])
    assert calls == [True]
    assert record['result']['status'] == 'partial'
    assert record['result']['task_status'] == 'partially_fulfilled'


def test_scope_guard_sets_partial_before_verification_and_after_completion(run_turn, monkeypatch):
    monkeypatch.setattr('services.turn_runtime.build_batch_analysis', lambda *a, **k: {
        'scope_complete': False, 'inspected_files': 1, 'inventory_files': 3,
    })
    verifier = Verifier()
    _, record, _, _ = run_turn([draft()], verifier, mode='structured')
    assert verifier.seen_statuses == ['partial']
    assert record['result']['status'] == 'partial'
    assert record['result']['scope_completeness'] == 'candidate_only'
    assert 'scope_incomplete' in record['result']['quality_metrics']['answer_completion_gaps']


def test_successful_revision_uses_new_candidate_completeness(run_turn):
    incomplete = draft(status='partial', evidence_status='partial', input_manifest={
        'large_qa': {'global_mode': True, 'synthesis_complete': False},
        'output_truncated': True,
    })
    complete = draft(input_manifest={
        'large_qa': {'global_mode': True, 'synthesis_complete': True, 'source_snapshot_current': True},
        'final_synthesis_complete': True, 'finish_reason': 'stop',
    }, package_reading={'files_read': 1040, 'complete': True})
    _, record, engine, _ = run_turn([incomplete, complete], Verifier(repair_first=True), attempts=1)
    assert engine.calls == 2
    assert record['result']['status'] == 'answered'
    assert record['result']['task_status'] == 'fulfilled'
    assert record['result']['evidence_status'] == 'supported'
    assert record['result']['quality_metrics']['answer_completion_gaps'] == []


@pytest.mark.parametrize('manifest', [
    {},
    {'large_qa': {'global_mode': True, 'map_complete': True}},
    {'large_qa': {'global_mode': False, 'synthesis_complete': False}},
    {'package_reading': {'files_read': None, 'complete': None}},
])
def test_complete_or_inapplicable_manifests_remain_fulfilled(run_turn, manifest):
    _, record, _, _ = run_turn([draft(input_manifest=manifest)])
    assert record['result']['status'] == 'answered'
    assert record['result']['task_status'] == 'fulfilled'
    assert record['result']['quality_metrics']['answer_completion_gaps'] == []


@pytest.mark.parametrize('status', ['insufficient_evidence', 'translation_unavailable'])
def test_final_guard_preserves_specific_unfulfilled_statuses(run_turn, status):
    _, record, _, _ = run_turn([draft(status=status, input_manifest={'output_truncated': True})])
    assert record['result']['status'] == status
    assert record['result']['task_status'] == 'not_fulfilled'


def test_creative_turn_does_not_require_factual_verification(run_turn):
    _, record, _, verifier = run_turn([draft()], mode='creative')
    assert verifier.calls == 0
    assert record['result']['status'] == 'answered'
    assert record['result']['task_status'] == 'fulfilled'
    assert record['result']['quality_metrics']['answer_completion_gaps'] == []
