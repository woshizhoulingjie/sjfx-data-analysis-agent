import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from config import Config
from services.conversation import ConversationEngine
from services.large_qa_synthesis import _compact_capsules, prepare_global
from services.qa_execution import ExecutionControl, bind_execution
from test_large_qa_full_corpus import corpus_project, populate, query, scan, RecordingMapper


QUESTION = '给我讲解整个数据包都讲了哪些内容呢？'


def test_overview_compaction_keeps_every_file_and_late_exception(corpus_project, monkeypatch):
    case = corpus_project
    expected = populate(case, count=121, topics=97, long=True)
    path = sorted(expected)[-1]
    body = 'Earlier background material. ' * 2000 + 'TailExceptionMarker however thermal runaway requires stopping the experiment.'
    case.original(path, body, page=27)
    corpus = scan(case, text=QUESTION)
    compact = _compact_capsules(corpus['capsules'], QUESTION)
    assert {item['path'] for item in compact} == expected
    assert sum(len(p['text']) for item in compact for p in item['points']) < sum(
        len(p['text']) for item in corpus['capsules'] for p in item['points']) / 2
    tail = next(p for item in compact for p in item['points'] if 'TailExceptionMarker' in p['text'])
    assert tail['kind'] == 'original' and tail['location']['page'] == 27
    with case.index.connect() as connection:
        original_chunk = connection.execute('SELECT text FROM chunks WHERE id=?', (tail['chunk_id'],)).fetchone()[0]
    assert original_chunk[tail['excerpt_start']:tail['excerpt_end']] == tail['text']
    mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', mapper)
    result = prepare_global(query(case, text=QUESTION), QUESTION, mapper, base=case.base, store=case.store)
    assert mapper.paths() == expected
    assert 'TailExceptionMarker' in json.dumps(mapper.file_prompts())
    assert result['package_context']['large_qa']['model_mapped_files'] == 121


def test_repair_reuses_one_corpus_scan_and_hides_private_snapshot(corpus_project, monkeypatch):
    case = corpus_project
    populate(case, count=21, topics=7)
    monkeypatch.setattr(Config, 'LARGE_QA_INDEX_DIR', case.base)
    mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', mapper)
    monkeypatch.setattr(ConversationEngine, '_grounded_answer', lambda *a, **kw: ('资料描述研究方法与适用条件[1]。', []))
    retrieval = query(case, text=QUESTION)
    engine = ConversationEngine(SimpleNamespace(retrieve=lambda req: retrieval), answer_model=mapper)
    control = ExecutionControl(time.monotonic() + 810)
    with patch('services.large_qa_synthesis.scan_corpus', wraps=__import__('services.large_qa_corpus', fromlist=['scan_corpus']).scan_corpus) as scanner:
        with bind_execution(control):
            first = engine.ask(engine.new_session(case.package['id']), QUESTION, retrieval_override=retrieval)
            calls = len(mapper.file_prompts())
            second = engine.ask(engine.new_session(case.package['id']), QUESTION, retrieval_override=query(case, text=QUESTION))
        assert scanner.call_count == 1
    assert len(mapper.file_prompts()) == calls
    assert first['input_manifest']['large_qa']['model_mapped_files'] == 21
    assert second['input_manifest']['large_qa']['model_mapped_files'] == 21
    assert all(not k.startswith('_') for k in second['input_manifest']['large_qa'])


def test_changed_sources_withdraw_notes_without_restarting_scan(corpus_project, monkeypatch):
    case = corpus_project
    for name in ('a', 'b', 'c'):
        case.add(name + '.txt', name + ' CurrentFindingMarker independent source conditions.')
    case.sync()
    monkeypatch.setattr(Config, 'LARGE_QA_INDEX_DIR', case.base)
    mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', mapper)
    monkeypatch.setattr(ConversationEngine, '_grounded_answer', lambda *a, **kw: ('当前资料记录了适用条件[1]。', []))
    retrieval = query(case, text=QUESTION)
    engine = ConversationEngine(SimpleNamespace(retrieve=lambda req: retrieval), answer_model=mapper)
    control = ExecutionControl(time.monotonic() + 810)
    with bind_execution(control):
        engine.ask(engine.new_session(case.package['id']), QUESTION, retrieval_override=retrieval)
        case.store.delete('a.txt')
        with patch('services.large_qa_synthesis.scan_corpus', side_effect=AssertionError('repair must not restart corpus scan')):
            second = engine.ask(engine.new_session(case.package['id']), QUESTION, retrieval_override=query(case, text=QUESTION))
    assert second['status'] == 'partial'
    assert second['input_manifest']['large_qa']['source_snapshot_current'] is False
    assert all(c['source_path'] != 'a.txt' for c in second['citations'])
    assert any('已撤回' in warning for warning in second['warnings'])
