import json

from config import Config
from services import answer_quality as aq
from services.conversation import (
    CallableEvidenceRetriever, ContextWindowPolicy, ConversationEngine, _citation,
    _compact_citation_numbers, _select_synthesis_citations,
)
from services.conversation_package_context import model_digest


def citation(number, path, text):
    return _citation({'evidence_id': f'e{number}', 'source_path': path,
        'text': text, 'location': {'source': 'file_summary'},
        'evidence_quality': {'eligible': True}}, number)


def context(notes):
    return {'package_type': 'large', 'package_id': 'lp-test',
        'reading_manifest': {'files_read': len(notes), 'complete': True},
        'large_qa': {'global_mode': True, 'synthesis_complete': True,
            'source_snapshot_current': True, 'map_complete': True,
            'map_notes': [text for text, _ in notes],
            'notes_provenance': [{'text': text, 'sources': [path]} for text, path in notes]}}


def test_synthesis_selection_preserves_late_distinct_topic_before_extra_hits():
    rows = [citation(i, f'policy/p{i}.pdf', '政策关注绿色转型与民生服务。') for i in range(1, 40)]
    rows += [citation(40, 'science/rare.pdf', '这篇摘要讨论细胞代谢与疾病机制。')]
    package = context([('政策关注绿色转型。', 'policy/p1.pdf'),
                       ('科学研究讨论细胞代谢与疾病机制。', 'science/rare.pdf')])
    chosen = _select_synthesis_citations(rows,
        ContextWindowPolicy(max_prompt_evidence=3, max_model_evidence_chars=4000),
        '讲解整个数据包有哪些内容', package)
    assert {'policy/p1.pdf', 'science/rare.pdf'} <= {x['source_path'] for x in chosen}


def test_note_sources_follow_current_renumbering_and_keep_summary_identity():
    rows = [citation(12, 'policy.pdf', '摘要记载绿色转型。'),
            citation(67, 'biology.pdf', '摘要记载细胞代谢研究。')]
    _, selected = _compact_citation_numbers(rows, [rows[1], rows[0]])
    metadata = {}
    package = context([('绿色转型。', 'policy.pdf'), ('细胞代谢研究。', 'biology.pdf'),
                       ('没有对应材料的线索。', 'missing.pdf')])
    payload = json.loads(model_digest(package, metadata=metadata, citations=selected))
    topic = payload['topic_overview']
    assert topic['note_citations'] == [['[2]'], ['[1]'], []]
    assert metadata['final_synthesis_notes_with_sources'] == 2
    assert metadata['final_synthesis_notes_without_sources'] == 1
    assert metadata['final_synthesis_sources_complete'] is False
    assert all(aq.source_kind(x) == 'summary' for x in selected)


def test_budgeted_digest_keeps_note_and_source_pairs_aligned():
    notes = [(f'主题{i}：' + '有来源的研究内容。' * 20, f'f{i}.pdf') for i in range(12)]
    rows = [citation(i + 1, path, text) for i, (text, path) in enumerate(notes)]
    metadata = {}
    raw = model_digest(context(notes), max_chars=1700, metadata=metadata, citations=rows)
    topic = json.loads(raw)['topic_overview']
    assert len(raw) <= 1700
    assert 0 < len(topic['notes']) < len(notes)
    assert len(topic['notes']) == len(topic['note_citations'])
    for text, refs in zip(topic['notes'], topic['note_citations']):
        index = next(i for i, (note, _) in enumerate(notes) if text == note)
        assert refs == [f'[{index + 1}]']
    assert metadata['final_synthesis_complete'] is False


class CapturingModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def chat(self, system, prompt, **kwargs):
        self.calls.append((system, prompt, kwargs))
        return {'content': self.answer, 'finish_reason': 'stop'}


def test_global_retrieval_uses_overview_budget_and_current_note_citations(monkeypatch):
    # The fake provider exposes the same window as this synthesis policy.
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 32768)
    monkeypatch.setattr(Config, 'CONVERSATION_CONTEXT_TOKENS', 32768)
    monkeypatch.setattr(Config, 'CONVERSATION_MAX_ANSWER_TOKENS', 3000)
    rows = [citation(1, 'policy.pdf', '摘要介绍绿色转型。'),
            citation(2, 'biology.pdf', '摘要介绍细胞代谢。')]
    model = CapturingModel('资料摘要包括绿色转型[1]与细胞代谢研究[2]。')
    engine = ConversationEngine(CallableEvidenceRetriever(lambda request: {}), answer_model=model)
    metadata = {}
    answer, warnings = engine._quality_grounded_answer('retrieval', '讲解整个包有哪些内容',
        '上一轮回答引用[21]和[40]，这些旧编号不能沿用。', rows,
        context([('绿色转型。', 'policy.pdf'), ('细胞代谢。', 'biology.pdf')]), 2, metadata)
    prompt = model.calls[0][1]
    assert '[21]' not in prompt and '[40]' not in prompt
    assert model.calls[0][2]['max_tokens'] == 6000
    assert model.calls[0][2]['timeout'] == 240
    assert metadata['output_token_budget'] > aq.output_budget('讲解整个包有哪些内容', 'retrieval')
    assert 'note_citations' in prompt and '[1]' in prompt and '[2]' in prompt
    assert metadata['final_synthesis_sources_complete'] is True
    assert metadata['invalid_generated_citation_indexes'] == []
    assert not warnings and '细胞代谢' in answer


def test_notes_do_not_retain_citations_removed_by_final_token_budget(monkeypatch):
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 8000)
    monkeypatch.setattr(Config, 'CONVERSATION_CONTEXT_TOKENS', 8000)
    monkeypatch.setattr(Config, 'CONVERSATION_MAX_ANSWER_TOKENS', 3000)
    monkeypatch.setattr(aq, 'count_tokens', lambda text: len(str(text)))
    # Complete source paths consume prompt space even after passages shorten;
    # this fixture must still force some citations out of the final 8K input.
    notes = [(f'研究主题{i}。', 'research-results/' * 4 + f'f{i}.pdf') for i in range(6)]
    rows = [citation(i + 1, path, '来源内容。' * 70) for i, (_, path) in enumerate(notes)]
    model = CapturingModel('资料包含多个研究主题[1]。')
    engine = ConversationEngine(CallableEvidenceRetriever(lambda request: {}), answer_model=model)
    metadata = {}
    engine._quality_grounded_answer('retrieval', '全包讲了哪些内容', '', rows, context(notes), 6, metadata)
    assert 0 < len(rows) < 6
    prompt = model.calls[0][1]
    raw = prompt.split('全包阅读层（范围扫描统计、分批归并要点与有限摘要样本，不等于已读全文）：\n', 1)[1].split('\n\n当前问题：', 1)[0]
    allowed = {row['citation_label'] for row in rows}
    if metadata['final_synthesis_notes']:
        topic = json.loads(raw)['topic_overview']
        assert all(ref in allowed for refs in topic['note_citations'] for ref in refs)
    else:
        assert '已省略' in raw
        assert metadata['final_synthesis_complete'] is False
        assert metadata['final_synthesis_sources_complete'] is False
    assert metadata['final_synthesis_notes_without_sources'] > 0 or metadata['final_synthesis_notes'] == 0


def test_ordinary_retrieval_budget_and_history_are_unchanged(monkeypatch):
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 32768)
    monkeypatch.setattr(Config, 'CONVERSATION_CONTEXT_TOKENS', 32768)
    model = CapturingModel('付款在验收后进行[1]。')
    engine = ConversationEngine(CallableEvidenceRetriever(lambda request: {}), answer_model=model)
    rows = [citation(1, 'contract.pdf', '付款在验收后进行。')]
    engine._quality_grounded_answer('retrieval', '付款条件是什么', '历史引用[9]', rows, {}, 1, {})
    assert '[9]' in model.calls[0][1]
    assert model.calls[0][2]['max_tokens'] == aq.output_budget('付款条件是什么', 'retrieval')


def large_provenance_fixture():
    """Six themes, 36 notes, and two distinct, costly references per note."""
    rows, notes, provenance = [], [], []
    directories = ['policy', 'ai', 'biology', 'climate', 'economics', 'education']
    for index in range(36):
        note = f'研究主题{index}说明方法、研究发现与适用条件。'
        paths = [f'{directories[index % 6]}/' + '资料研究' * 35 + f'-{index}-{source}.pdf'
                 for source in range(2)]
        notes.append(note)
        provenance.append({'text': note, 'sources': paths})
        for path in paths:
            row = citation(len(rows) + 1, path, note + '资料记录了研究过程和结果。' * 180)
            row['location'].update(page=index + 1,
                selected_chunk_spans=[{'start': n * 120, 'end': n * 120 + 100} for n in range(400)])
            row['evidence_quality'] = {'eligible': True}
            row['large_global'] = True
            rows.append(row)
    package = context([])
    package['reading_manifest'].update(files_read=1040)
    package['large_qa'].update(map_notes=notes, notes_provenance=provenance)
    return rows, package


def test_36_notes_keep_both_representatives_with_large_locations_and_paths():
    rows, package = large_provenance_fixture()
    policy = ContextWindowPolicy(max_prompt_evidence=72, max_model_evidence_chars=60000,
                                 max_evidence_chars=1400)
    chosen = _select_synthesis_citations(rows, policy, '讲解整个数据包', package)
    assert len(chosen) == 72
    assert {row['source_path'] for row in chosen} == {row['source_path'] for row in rows}
    assert sum(len(json.dumps(aq.evidence_payload(row), ensure_ascii=False, separators=(',', ':')))
               for row in chosen) <= 60000
    assert all(row['location']['selected_chunk_span_count'] == 400 for row in chosen)
    assert all(len(row['location']['selected_chunk_spans']) == 4 for row in chosen)
    assert all(len(row['location']['selected_chunk_spans']) == 400 for row in rows)
    assert all(row['location']['page'] for row in chosen)


def test_one_source_per_note_precedes_any_second_source_under_evidence_cap():
    rows, package = large_provenance_fixture()
    chosen = _select_synthesis_citations(rows,
        ContextWindowPolicy(max_prompt_evidence=36, max_model_evidence_chars=60000),
        '讲解整个数据包', package)
    assert len(chosen) == 36
    paths = {row['source_path'] for row in chosen}
    assert all(len(paths.intersection(note['sources'])) == 1
               for note in package['large_qa']['notes_provenance'])


def test_global_provenance_survives_rerank_display_and_final_prompt_budget(monkeypatch):
    rows, package = large_provenance_fixture()
    # Ninety unrelated hits precede every representative after this rerank.
    extras = [citation(100 + i, f'policy/unrelated-{i}.pdf', '政策资料记录绿色转型措施和民生服务。')
              for i in range(90)]
    retrieval = {'results': rows + extras, 'package_context': package}
    monkeypatch.setattr('services.large_qa_synthesis.prepare_global', lambda result, *args, **kwargs: result)
    monkeypatch.setattr('services.conversation._rerank_large_package_items',
                        lambda scan_id, query, items: list(reversed(items)))
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 65536)
    monkeypatch.setattr(Config, 'CONVERSATION_CONTEXT_TOKENS', 65536)
    monkeypatch.setattr(aq, 'count_tokens', lambda value: (len(str(value).encode('utf-8')) + 2) // 3)
    model = CapturingModel('资料摘要包括不同研究主题及其方法和条件[1]。')
    engine = ConversationEngine(CallableEvidenceRetriever(lambda request: retrieval), answer_model=model)
    result = engine.ask(engine.new_session('lp-test'), '给我讲解整个数据包都讲了哪些内容呢？')
    manifest = result['input_manifest']
    assert manifest['evidence_limit'] == 72
    assert manifest['model_evidence_char_budget'] == 60000
    assert manifest['selected_evidence'] == 72
    assert manifest['final_synthesis_notes'] == 36
    assert manifest['final_synthesis_notes_with_sources'] == 36
    assert manifest['final_synthesis_notes_without_sources'] == 0
    assert manifest['final_synthesis_sources_complete'] is True
    assert set(manifest['selected_paths']) == {row['source_path'] for row in rows}
    assert manifest['prompt_tokens'] + manifest['output_token_budget'] + 512 <= 65536
    assert len(model.calls) == 1


def test_larger_global_policy_capacity_keeps_ordinary_defaults():
    policy = ContextWindowPolicy()
    assert policy.max_prompt_evidence == 16
    assert policy.max_display_evidence == 50
    assert ContextWindowPolicy(max_prompt_evidence=72).max_prompt_evidence == 72
    assert ContextWindowPolicy(max_prompt_evidence=1000).max_prompt_evidence == 200
