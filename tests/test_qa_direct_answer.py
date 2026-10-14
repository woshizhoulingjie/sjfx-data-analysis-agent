"""Direct QA preserves the generated answer while reporting actual completion."""
import copy
import json
from unittest.mock import Mock

import pytest

from services import turn_runtime
from services.conversation import ConversationScope, ConversationSession
from services.storage import Storage
from services.turn_runtime import AnalysisTurnRuntime


QUESTION = '请详细介绍整个资料包的研究内容，同时说明实收和尚缺数量。'
SOURCE = {'evidence_id': 'EV-pv', 'source_path': 'energy/pv.txt',
          'text': 'Photovoltaic modules degrade after exposure to outdoor temperature changes.'}
LONG_ANSWER = '\n\n'.join(
    '## 第{}节 研究主题\n\n'.format(section) + '\n\n'.join(
        '第{}段：光伏组件的老化与户外温度变化有关，资料讨论实验观察、作用机制与适用条件[47]。'
        '这里保留成稿中的完整论述、跨语言概括和研究问题。'
        '预算为999万元的这句话也由生成阶段负责，本测试确认关闭核验后不会按句删除。'.format(paragraph)
        for paragraph in range(1, 21))
    for section in range(1, 5))


def answer(**updates):
    result = {
        'status': 'answered', 'evidence_status': 'supported', 'answer': LONG_ANSWER,
        'question': QUESTION, 'warnings': ['来源文件保持可追溯。'],
        'citations': [dict(SOURCE, original_text=SOURCE['text'], citation_index=47)],
        'coverage': {'scope_files': 1040, 'candidate_files': 269, 'inspected_files': 67,
                     'scope_inspection_coverage': 67 / 1040, 'query_coverage': None},
        'input_manifest': {
            'large_qa': {'global_mode': True, 'scan_complete': True, 'map_complete': True,
                         'synthesis_complete': True, 'source_snapshot_current': True,
                         'scanned_files': 1040, 'mapped_files': 1040},
            'final_synthesis_complete': True, 'final_synthesis_sources_complete': True,
            'finish_reason': 'stop',
        },
    }
    result.update(copy.deepcopy(updates))
    return result


class Planner:
    def __init__(self, mode):
        self.mode = mode

    def plan(self, question, scope, memory=None):
        tools = ['document_discovery', 'evidence_search', 'claim_verifier', 'answer_composer']
        return {'objective': question, 'scope': scope, 'modes': [self.mode],
                'steps': [{'step_id': 'step-' + name, 'tool': name, 'label': name}
                          for name in tools],
                'tasks': tools[:], 'query_variants': [question], 'constraints': []}


class Engine:
    def __init__(self, result):
        self.result = result
        self.retriever = self
        self.ask_calls = []
        self.search_requests = []

    def retrieve(self, request):
        self.search_requests.append(request)
        return {'results': [copy.deepcopy(SOURCE)],
                'coverage': {'candidate_files': 1, 'query_coverage': None}}

    def ask(self, session, question, **kwargs):
        self.ask_calls.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self.result)


@pytest.fixture
def direct_turn(tmp_path, monkeypatch):
    # Keep quality features enabled: disabling that flag could hide retention
    # or guard calls instead of proving that verify_answers controls the path.
    monkeypatch.setattr(turn_runtime.aq, 'enabled', lambda: True)
    search = Mock(wraps=turn_runtime.execute_bounded_searches)
    monkeypatch.setattr(turn_runtime, 'execute_bounded_searches', search)
    forbidden = {}
    for name in ('complete_quantities', 'select_candidate', 'capture_candidate'):
        forbidden[name] = Mock(side_effect=AssertionError(name + ' must not run'))
        monkeypatch.setattr(turn_runtime, name, forbidden[name])
    verifier = Mock()
    verifier.verify.side_effect = AssertionError('verifier.verify must not run')
    verifier.guard_result.side_effect = AssertionError('verifier.guard_result must not run')

    def run(result=None, *, mode='summary', scope=None, inventory=None):
        scope = scope or ConversationScope('package')
        inventory = inventory or ['energy/pv.txt', 'energy/temperature.txt', 'bio/rna.txt']
        storage = Storage(tmp_path / 'direct.db', tmp_path / 'sidecars')
        session = ConversationSession(scan_id='lp-direct', scope=scope,
                                      session_id='direct-session', title='直接回答')
        storage.save_conversation(session.as_dict(), 'owner')
        turn, _ = storage.create_conversation_turn(session.session_id, session.scan_id, 'owner',
                                                   QUESTION, scope.as_dict())
        engine = Engine(answer() if result is None else result)
        outcome = AnalysisTurnRuntime(storage, engine, planner=Planner(mode), verifier=verifier,
                                      verify_answers=False, max_revision_attempts=2).execute(
            turn, session, scope, inventory)
        record = storage.get_conversation_turn(turn['id'], 'owner')
        steps = storage.list_conversation_turn_steps(turn['id'])
        return outcome, record, engine, storage, steps

    run.verifier = verifier
    run.search = search
    run.forbidden = forbidden
    return run


def test_direct_answer_skips_every_post_generation_review_and_repair(direct_turn):
    outcome, record, engine, _, _ = direct_turn()
    assert outcome['status'] == record['status'] == 'completed'
    assert len(engine.ask_calls) == 1
    assert direct_turn.search.call_count == 1
    assert len(engine.search_requests) == 1
    direct_turn.verifier.verify.assert_not_called()
    direct_turn.verifier.guard_result.assert_not_called()
    for forbidden in direct_turn.forbidden.values():
        forbidden.assert_not_called()
    assert record['verification']['revision_attempts'] == 0
    assert len(LONG_ANSWER) > 8000
    assert record['result']['answer'] == LONG_ANSWER
    assert '## 第3节 研究主题' in record['result']['answer']
    assert '预算为999万元' in record['result']['answer']
    verification, result = record['verification'], record['result']
    assert verification['status'] == 'not_required'
    assert verification['disabled'] is True
    assert verification['needs_revision'] is False
    assert verification['ledger']['claims'] == []
    assert result['answer_verification_enabled'] is False
    assert result['citation_verification_status'] == 'not_required'
    assert result['evidence_status'] == 'unverified'
    assert result['quality_metrics']['answer_verification_enabled'] is False
    assert result['quality_metrics']['verification_status'] == 'not_required'
    assert result['status'] == 'answered'
    assert result['task_status'] == 'fulfilled'
    assert result['quality_metrics']['answer_completion_gaps'] == []


def test_plan_has_no_claim_verifier_task_or_step(direct_turn):
    _, record, engine, _, steps = direct_turn()
    saved_plan = record['plan']
    saved_plan = json.loads(saved_plan) if isinstance(saved_plan, str) else saved_plan
    for plan in (saved_plan, engine.ask_calls[0]['analysis_plan']):
        assert 'claim_verifier' not in plan['tasks']
        assert all(step['tool'] != 'claim_verifier' for step in plan['steps'])
    assert all(step['tool'] != 'claim_verifier' for step in steps)
    assert any(step['tool'] == 'answer_composer' for step in steps)


def test_scope_sources_and_real_coverage_are_preserved(direct_turn):
    original = answer()
    _, record, engine, _, _ = direct_turn(original, scope=ConversationScope('directory', 'energy'))
    result = record['result']
    assert engine.ask_calls[0]['scope'].kind == 'directory'
    assert engine.ask_calls[0]['scope'].value == 'energy'
    assert set(result['related_file_paths']) == {'energy/pv.txt', 'energy/temperature.txt'}
    assert result['file_results_total'] == 2
    assert result['citations'] == original['citations']
    assert result['coverage'] == original['coverage']
    assert result['coverage']['scope_inspection_coverage'] == 67 / 1040
    assert result['coverage']['query_coverage'] is None
    assert result['input_manifest']['large_qa'] == original['input_manifest']['large_qa']
    assert '来源文件保持可追溯。' in result['warnings']


def test_incomplete_global_sources_and_truncation_remain_visible_without_review(direct_turn):
    result = answer(input_manifest={
        'large_qa': {'global_mode': True, 'scan_complete': True, 'map_complete': False,
                     'synthesis_complete': False, 'source_snapshot_current': False,
                     'scanned_files': 1040, 'mapped_files': 1032},
        'final_synthesis_sources_complete': False,
        'output_truncated': True, 'finish_reason': 'length',
    })
    outcome, record, engine, _, _ = direct_turn(result)
    assert outcome['status'] == record['status'] == 'completed'
    assert record['verification']['status'] == 'not_required'
    assert record['verification']['disabled'] is True
    assert record['result']['status'] == 'partial'
    assert record['result']['task_status'] == 'partially_fulfilled'
    gaps = record['result']['quality_metrics']['answer_completion_gaps']
    assert {'synthesis_incomplete', 'source_snapshot_changed', 'synthesis_sources_incomplete',
            'output_truncated'}.issubset(gaps)
    assert record['result']['input_manifest']['large_qa']['scanned_files'] == 1040
    assert record['result']['input_manifest']['large_qa']['mapped_files'] == 1032
    assert record['result']['answer'] == LONG_ANSWER
    assert len(engine.ask_calls) == 1
    assert direct_turn.search.call_count == 1


def test_batch_scope_limit_still_applies_without_post_generation_verification(direct_turn, monkeypatch):
    monkeypatch.setattr(turn_runtime, 'build_batch_analysis', lambda *args, **kwargs: {
        'scope_complete': False, 'inspected_files': 2, 'inventory_files': 1040,
        'candidate_files': 2, 'batch_count': 1,
    })
    _, record, _, _, _ = direct_turn(mode='structured')
    result = record['result']
    assert result['status'] == 'partial'
    assert result['task_status'] == 'partially_fulfilled'
    assert result['scope_completeness'] == 'candidate_only'
    assert 'scope_incomplete' in result['quality_metrics']['answer_completion_gaps']
    assert result['answer'].endswith(LONG_ANSWER)
    assert '2/1040' in result['answer']
    direct_turn.verifier.verify.assert_not_called()
    direct_turn.verifier.guard_result.assert_not_called()
