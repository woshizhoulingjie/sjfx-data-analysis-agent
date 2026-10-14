"""One durable QA turn shares its budget, draft checkpoint and repair context."""
import copy
from types import SimpleNamespace

import pytest

from config import Config
from services.conversation import ConversationScope, ConversationSession
from services.qa_execution import current_execution
from services.storage import Storage
from services.turn_runtime import AnalysisTurnRuntime


BODY = '验收必须完成热测试，并由负责人书面批准。'


class Planner:
    def __init__(self, mode='summary'):
        self.mode = mode

    def plan(self, question, scope, memory=None):
        return {'objective': question, 'scope': scope, 'modes': [self.mode],
                'steps': [], 'query_variants': [question], 'constraints': []}


class Engine:
    def __init__(self, on_ask=None):
        self.on_ask = on_ask
        self.retriever = self
        self.controls = []
        self.prepared_before = []
        self.searches = []

    def retrieve(self, request):
        self.searches.append(request)
        return {'results': [{'evidence_id': 'source-1', 'source_path': 'a.txt', 'text': BODY}],
                'coverage': {'query_coverage': 1.0, 'scope_files': 1},
                'retrieval_mode': 'large_qa'}

    def ask(self, session, question, **kwargs):
        control = current_execution()
        assert control is not None
        self.controls.append(control)
        self.prepared_before.append(control.prepared)
        if self.on_ask:
            self.on_ask(len(self.controls), control)
        if control.prepared is None:
            control.prepared = {'prepared_marker': object()}
            control.prepared_query = question
        return {'status': 'answered', 'evidence_status': 'supported',
                'answer': BODY + ' [1]。', 'question': question, 'resolved_query': question,
                'citations': [{'citation_index': 1, 'citation_label': '[1]',
                               'evidence_id': 'source-1', 'source_path': 'a.txt',
                               'original_text': BODY, 'location': {'page': 1}}],
                'coverage': {'query_coverage': 1.0}, 'warnings': []}


class Verifier:
    def __init__(self, first_needs_revision=True, on_verify=None):
        self.first_needs_revision = first_needs_revision
        self.on_verify = on_verify
        self.calls = 0

    def verify(self, result, plan, **kwargs):
        self.calls += 1
        if self.on_verify:
            self.on_verify(self.calls, result)
        needs_revision = self.first_needs_revision and self.calls == 1
        return {'status': 'partial' if needs_revision else 'verified',
                'needs_revision': needs_revision, 'warnings': [],
                'failed_claims': [], 'missing_answer_aspects': [],
                'ledger': {'claims': [{'text': BODY, 'status': 'supported'}]},
                'quality_metrics': {'verification_status': 'partial' if needs_revision else 'verified'}}

    def guard_result(self, result, verification):
        return copy.deepcopy(result)


@pytest.fixture
def turn_case(tmp_path, monkeypatch):
    clock = [1000.]
    monkeypatch.setattr('services.turn_runtime.time.monotonic', lambda: clock[0])
    monkeypatch.setattr(Config, 'JOB_CONVERSATION_TURN_TIMEOUT_SECONDS', 900)
    monkeypatch.setattr(Config, 'CONVERSATION_ANSWER_TIMEOUT_SECONDS', 120)
    monkeypatch.setattr('services.turn_runtime.aq.enabled', lambda: False)
    storage = Storage(tmp_path / 'turns.db', tmp_path / 'sidecars')
    session = ConversationSession(scan_id='lp-runtime-test', scope=ConversationScope('package'),
                                  session_id='runtime-session', title='全包研究')
    storage.save_conversation(session.as_dict(), 'owner')
    turn, _ = storage.create_conversation_turn(session.session_id, session.scan_id, 'owner',
                                              '请综合全部资料的验收条件。', session.scope.as_dict())

    def execute(engine, verifier=None, *, mode='summary', cancel_check=None):
        runtime = AnalysisTurnRuntime(storage, engine, planner=Planner(mode),
                                      verifier=verifier or Verifier(), max_revision_attempts=1,
                                      cancel_check=cancel_check)
        outcome = runtime.execute(turn, session, session.scope, ['a.txt'])
        return outcome, storage.get_conversation_turn(turn['id'], 'owner')

    return SimpleNamespace(clock=clock, storage=storage, session=session, turn=turn, execute=execute)


def test_initial_and_repair_share_deadline_and_prepared_context(turn_case):
    case = turn_case
    initial_remaining = []

    def ask(number, control):
        initial_remaining.append(control.remaining())
        case.clock[0] += 20

    engine = Engine(on_ask=ask)
    outcome, record = case.execute(engine)
    assert outcome['status'] == 'completed'
    assert len(engine.controls) == 2
    assert engine.controls[0] is engine.controls[1]
    assert engine.controls[0].deadline == 1810.
    assert initial_remaining == [810., 790.]
    assert engine.prepared_before[0] is None
    assert engine.prepared_before[1] is engine.controls[0].prepared
    assert record['verification']['revision_attempts'] == 1
    assert current_execution() is None


def test_answer_is_checkpointed_before_verification_without_replacing_placeholder(turn_case):
    case = turn_case
    seen = []

    def verify(number, result):
        if number == 1:
            record = case.storage.get_conversation_turn(case.turn['id'], 'owner')
            assert record['status'] == 'running'
            assert record['result']['answer'] == result['answer']
            events = case.storage.get_conversation_turn_events(case.turn['id'], 'owner')
            assert any(event['event_type'] == 'draft' for event in events)
            conversation = case.storage.get_conversation(case.session.session_id, 'owner',
                                                        scan_id=case.session.scan_id)
            assert conversation['messages'][-1]['content'] != result['answer']
            seen.append(record['result'])

    case.execute(Engine(), Verifier(first_needs_revision=False, on_verify=verify))
    assert len(seen) == 1
    assert seen[0]['citations'][0]['original_text'] == BODY


def test_verification_failure_keeps_the_saved_answer_checkpoint(turn_case):
    case = turn_case

    def fail(number, result):
        raise RuntimeError('synthetic verifier failure')

    with pytest.raises(RuntimeError, match='synthetic verifier failure'):
        case.execute(Engine(), Verifier(on_verify=fail))
    record = case.storage.get_conversation_turn(case.turn['id'], 'owner')
    assert record['status'] == 'failed'
    assert record['result']['answer'] == BODY + ' [1]。'
    assert current_execution() is None


def test_late_repair_is_skipped_and_existing_answer_is_saved_as_partial(turn_case):
    case = turn_case

    def slow_initial(number, control):
        case.clock[0] += 650

    engine = Engine(on_ask=slow_initial)
    outcome, record = case.execute(engine)
    assert outcome['status'] == 'completed'
    assert len(engine.controls) == 1
    assert len(engine.searches) == 1
    assert record['result']['answer'] == BODY + ' [1]。'
    assert record['result']['status'] == 'partial'
    assert record['result']['task_status'] == 'partially_fulfilled'
    assert record['verification']['revision_attempts'] == 0
    assert any('剩余时间不足' in warning for warning in record['result']['warnings'])


def test_expanded_search_cannot_start_revision_after_consuming_remaining_budget(turn_case):
    case = turn_case

    def initial(number, control):
        case.clock[0] += 600

    engine = Engine(on_ask=initial)
    retrieve = engine.retrieve

    def slow_repair_search(request):
        if engine.controls:
            case.clock[0] += 60
        return retrieve(request)

    engine.retrieve = slow_repair_search
    outcome, record = case.execute(engine)
    assert outcome['status'] == 'completed'
    assert len(engine.searches) == 2
    assert len(engine.controls) == 1
    assert record['result']['answer'] == BODY + ' [1]。'
    assert record['result']['status'] == 'partial'
    assert any('剩余时间不足' in warning for warning in record['result']['warnings'])


@pytest.mark.parametrize('job_limit, expected_budget', [(60, 60), (600, 510), (1800, 810)])
def test_turn_deadline_reserves_worker_finalization_time(turn_case, monkeypatch,
                                                       job_limit, expected_budget):
    case = turn_case
    monkeypatch.setattr(Config, 'JOB_CONVERSATION_TURN_TIMEOUT_SECONDS', job_limit)
    engine = Engine()
    case.execute(engine, Verifier(first_needs_revision=False))
    assert engine.controls[0].deadline == 1000. + expected_budget


def test_synthesis_progress_is_throttled_and_repair_progress_advances(turn_case):
    case = turn_case

    def progress(number, control):
        if number == 1:
            control.publish(phase='scan', files_done=5, files_total=10)
            case.clock[0] += 1
            control.publish(phase='scan', files_done=6, files_total=10)
            case.clock[0] += 5
            control.publish(phase='scan', files_done=7, files_total=10)
            control.publish(phase='map', batch_done=0, batch_total=2, mapped_files=0)
            case.clock[0] += 5
            control.publish(phase='map', batch_done=1, batch_total=2, mapped_files=5)
            control.publish(phase='compose')
        else:
            control.publish(phase='scan', files_done=10, files_total=10)
            control.publish(phase='map', batch_done=2, batch_total=2, mapped_files=10)
            control.publish(phase='compose')

    case.execute(Engine(on_ask=progress))
    events = [event for event in case.storage.get_conversation_turn_events(case.turn['id'], 'owner')
              if event['event_type'] == 'progress']
    assert not any('6/10' in event['message'] for event in events)
    assert any('5/10' in event['message'] for event in events)
    assert any('7/10' in event['message'] for event in events)
    assert any('1/2' in event['message'] and '5 份文件' in event['message'] for event in events)
    initial = [event['progress'] for event in events if event['stage'] == 'executing']
    repaired = [event['progress'] for event in events if event['stage'] == 'repairing']
    assert initial == sorted(initial) and all(62 <= value <= 79 for value in initial)
    assert repaired == sorted(repaired) and all(88 <= value <= 94 for value in repaired)
    assert initial[-1] == 79 and repaired[-1] == 94


def test_cancel_during_repair_preserves_draft_and_resets_context(turn_case):
    case = turn_case
    cancelled = [False]

    class JobCancelled(Exception):
        pass

    def checkpoint():
        if cancelled[0]:
            raise JobCancelled('synthetic cancellation')

    def ask(number, control):
        if number == 2:
            cancelled[0] = True
            control.publish(phase='scan', files_done=0, files_total=1)

    with pytest.raises(JobCancelled):
        case.execute(Engine(on_ask=ask), cancel_check=checkpoint)
    record = case.storage.get_conversation_turn(case.turn['id'], 'owner')
    assert record['status'] == 'cancelled'
    assert record['result']['answer'] == BODY + ' [1]。'
    assert current_execution() is None


def test_creative_turn_keeps_normal_response_without_package_verification(turn_case):
    engine = Engine()
    verifier = Verifier()
    outcome, record = turn_case.execute(engine, verifier, mode='creative')
    assert outcome['status'] == 'completed'
    assert len(engine.controls) == 1
    assert engine.searches == []
    assert verifier.calls == 0
    assert record['result']['status'] == 'answered'
    assert current_execution() is None
