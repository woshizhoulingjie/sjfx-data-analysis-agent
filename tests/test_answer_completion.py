import copy

import pytest

from services import answer_quality as aq
from services.answer_completion import complete_quantities
from services.claim_verifier import ClaimVerifier
from services.conversation import _citation
from services.conversation import ConversationScope, ConversationSession
from services.storage import Storage
from services.turn_runtime import AnalysisTurnRuntime

QUESTION = '根据记录，南岭项目实收多少台、还缺多少台？'
SOURCE = ('南岭项目HT-2031-022到货记录，记录时间2031年9月4日。'
          '计划交付Q-23设备19台，实收13台，尚缺6台。设备尚未完成调试。')
PLAN = {'objective': QUESTION, 'scope': {'kind': 'package'}, 'modes': ['retrieval']}


def prepare(sources=(SOURCE,), question=QUESTION, answer='资料暂时无法支持的内容没有纳入这份回答。', extra=None):
    rows = []
    for i, text in enumerate(sources, 1):
        row = _citation({'evidence_id': f'count-{i}', 'source_path': f'receipt-{i}.txt',
            'text': text, 'label': 'paragraph', 'evidence_quality': {'eligible': True},
            'location': {'source': 'file_summary'}}, i)
        row.update(extra or {})
        rows.append(row)
    result = {'answer': answer, 'question': question, 'resolved_query': question,
              'citations': rows, 'status': 'answered',
              'input_manifest': {'answer_contract': aq.answer_contract(question, rows)}}
    plan = {**PLAN, 'objective': question}
    verifier = ClaimVerifier()
    return result, verifier.verify(result, plan), plan, verifier


def test_supported_quantities_repair_actual_omissions_without_mutating_inputs():
    args = prepare()
    before = copy.deepcopy(args[:3])
    result, verified = complete_quantities(*args)
    assert result['answer'].startswith('实收13台[1]。' + chr(10) + '尚缺6台[1]。')
    assert verified['missing_answer_aspects'] == []
    assert verified['quantity_completion']['reverified']
    assert args[:3] == before
    assert result['citations'] == args[0]['citations']


def test_completed_answer_removes_only_stale_gap_note():
    result, verified = complete_quantities(*prepare())
    assert _gap_note() not in result['answer']
    assert not verified['missing_answer_aspects']


def _gap_note():
    return '资料暂时无法支持的内容没有纳入这份回答。'


def test_different_values_and_units_are_not_sample_specific():
    source = SOURCE.replace('19台', '41箱').replace('13台', '27箱').replace('6台', '14箱')
    result, verified = complete_quantities(*prepare((source,), question=QUESTION.replace('台', '箱')))
    assert '实收27箱[1]' in result['answer'] and '尚缺14箱[1]' in result['answer']
    assert not verified['missing_answer_aspects']


def test_fill_only_missing_role_preserving_existing_answer():
    original = '实收13台[1]。'
    result, verified = complete_quantities(*prepare(answer=original))
    assert result['answer'].count('实收13台') == 1
    assert result['answer'].endswith(original)
    assert verified['quantity_completion']['aspects'] == ['缺少数量']


@pytest.mark.parametrize('source', [
    SOURCE.replace('南岭项目', '北湖项目'),
    SOURCE.replace('实收13台', '计划实收13台'),
    SOURCE.replace('实收13台', '未确认实收13台'),
    SOURCE.replace('实收13台', '未实收13台'),
    SOURCE.replace('实收13台', '若实收13台'),
    SOURCE.replace('实收13台', '实收13台，实收12台'),
    SOURCE.replace('实收13台，尚缺6台', '实收13台；尚缺6台'),
    SOURCE.replace('实收13台，尚缺6台', '实收13台，北湖项目尚缺6台'),
    SOURCE.replace('实收13台，尚缺6台', '实收13台，记录时间2031年9月5日。尚缺6台'),
    SOURCE.replace('实收13台，尚缺6台', '设备Q-23实收13台，设备P-42尚缺6台'),
    SOURCE.replace('实收13台', '实收13箱'),
])
def test_no_cross_record_or_uncertain_completion(source):
    args = prepare((source,))
    result, verified = complete_quantities(*args)
    assert result is args[0] and verified is args[1]


@pytest.mark.parametrize('sources', [
    (SOURCE, SOURCE.replace('13台', '12台')),
    (SOURCE, SOURCE.replace('2031年9月4日', '2031年9月5日')),
    (SOURCE, SOURCE.replace('Q-23', 'Q-24')),
    (SOURCE, '实收12台，尚缺7台。'),
    ('南岭项目到货记录。实收13台。', '南岭项目到货记录。尚缺6台。'),
])
def test_competing_or_incomplete_records_are_not_spliced(sources):
    args = prepare(sources)
    assert complete_quantities(*args)[0] is args[0]


@pytest.mark.parametrize('question', [
    '实收多少台、还缺多少台？',
    '南岭项目和北湖项目分别实收多少台、还缺多少台？',
    '南岭项目目前实收多少台、还缺多少台？',
    '南岭项目累计实收多少台、还缺多少台？',
    '南岭项目预计实收多少台、还缺多少台？',
    '南岭项目2031年9月5日实收多少台、还缺多少台？',
    '南岭项目Q-24实收多少台、还缺多少台？',
])
def test_question_scope_must_be_explicit_and_match(question):
    args = prepare(question=question)
    assert complete_quantities(*args)[0] is args[0]


@pytest.mark.parametrize('extra', [{'preview_only': True}, {'coverage': {'level': 'preview'}}, {'label': 'title'}])
def test_ineligible_evidence_is_not_promoted(extra):
    args = prepare(extra=extra)
    assert complete_quantities(*args)[0] is args[0]


def test_only_model_visible_citations_are_read():
    args = prepare()
    args[0]['model_citations'] = []
    assert complete_quantities(*args)[0] is args[0]


def test_narrative_mentions_only_veto_and_never_supply_counts():
    mail = ('南岭项目补交请求。供应商就HT-2031-022尚缺的6台Q-23设备提出延期建议，'
            '请求在2031年9月8日补交，请买方确认，尚不构成已经同意的合同变更。')
    result, verified = complete_quantities(*prepare((SOURCE, mail)))
    assert '实收13台[1]' in result['answer'] and '尚缺6台[1]' in result['answer']
    assert verified['quantity_completion']['citation_index'] == 1
    args = prepare((mail,))
    assert complete_quantities(*args)[0] is args[0]
    for changed in (mail.replace('6台', '7台'), mail.replace('Q-23', 'Q-24'),
                    mail.replace('尚缺的6台', '并非尚缺的6台')):
        args = prepare((SOURCE, changed))
        assert complete_quantities(*args)[0] is args[0]


def test_keyword_identifiers_are_not_quantity_observations():
    metadata = '；keywords: 南岭项目、HT-2031-022、实收、尚缺的、q-23、设备、交付记录'
    result, checked = complete_quantities(*prepare((SOURCE + metadata,)))
    assert '实收13台[1]' in result['answer'] and '尚缺6台[1]' in result['answer']
    assert not checked['missing_answer_aspects']


@pytest.mark.parametrize('suffix', ['本记录已作废。', '上述实收数量误报，现更正为12台。',
    '仓库否认此数量。', '实收数量为传闻，未核实。'])
def test_corrected_or_revoked_records_require_review(suffix):
    args = prepare((SOURCE + suffix,))
    assert complete_quantities(*args)[0] is args[0]


def test_scope_blocks_outside_files():
    result, verification, plan, verifier = prepare()
    plan['scope'] = {'kind': 'files', 'source_paths': ['other.txt']}
    assert complete_quantities(result, verification, plan, verifier)[0] is result


def test_existing_supported_answer_is_noop():
    args = prepare(answer='实收13台[1]。尚缺6台[1]。')
    assert complete_quantities(*args)[0] is args[0]


def test_disabled_quality_preserves_behavior(monkeypatch):
    args = prepare()
    monkeypatch.setattr(aq, 'enabled', lambda: False)
    assert complete_quantities(*args)[0] is args[0]


def test_cancellation_is_not_swallowed():
    def cancel():
        raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        complete_quantities(*prepare(), checkpoint=cancel)


def test_runtime_persists_completed_answer_and_verification(tmp_path):
    class Planner:
        def plan(self, question, scope, memory=None):
            return {'objective': question, 'scope': scope, 'modes': ['retrieval'],
                    'steps': [], 'constraints': [], 'query_variants': [question]}

    class Engine:
        def __init__(self):
            self.calls = 0
            self.retriever = self

        def retrieve(self, request):
            return {'results': [{'evidence_id': 'count-1', 'source_path': 'receipt-1.txt',
                    'text': SOURCE, 'label': 'paragraph', 'location': {'page': 1}}],
                    'coverage': {'query_coverage': 1.0}}

        def ask(self, session, question, **kwargs):
            self.calls += 1
            return copy.deepcopy(prepare()[0])

    scope = ConversationScope('package')
    storage = Storage(tmp_path/'agent.db', tmp_path/'sidecars')
    session = ConversationSession(scan_id='scan-count', scope=scope,
                                  session_id='session-count', title='数量问答')
    storage.save_conversation(session.as_dict(), 'owner')
    turn, _ = storage.create_conversation_turn(session.session_id, session.scan_id,
        'owner', QUESTION, scope.as_dict(), idempotency_key='count')
    engine = Engine()
    AnalysisTurnRuntime(storage, engine, planner=Planner(), max_revision_attempts=0).execute(
        turn, session, scope, ['receipt-1.txt'])
    record = storage.get_conversation_turn(turn['id'], 'owner')
    assert engine.calls == 1
    assert '实收13台[1]' in record['result']['answer']
    assert '尚缺6台[1]' in record['result']['answer']
    assert record['verification']['missing_answer_aspects'] == []
    assert record['verification']['quantity_completion']['reverified']
    saved = storage.get_conversation('session-count', 'owner', scan_id='scan-count')
    assert saved['messages'][-1]['content'] == record['result']['answer']
