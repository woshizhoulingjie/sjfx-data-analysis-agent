"""Offline regression: source values survive prose indexing and semantic recall.

Uses real temporary SQLite document/index storage. Scores are controlled; no
model, network, OCR engine, server or live installation is started.
"""
import ast
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from services import answer_quality as aq, ordinary_recall
from services.conversation import ConversationScope, ContextWindowPolicy, _normalise_citations
from services.retrieval import evidence_corpus, retrieve_evidence
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser


QUESTION = '根据扫描采购凭证，采购金额和数量分别是多少，是否已付款？请引用直接原文证据。'
LINES = ['SYNTHETIC PURCHASE RECEIPT', 'TOTAL AMOUNT: 317 CNY',
         'PURCHASE QUANTITY: 4 UNITS', 'PAYMENT STATUS: UNPAID',
         'For synthetic acceptance only', 'No actual purchase or payment']
PATH = 'inside/purchase_receipt.png'


def document(path=PATH, lines=None):
    lines = LINES if lines is None else lines
    return {'text': '[第 1 页/张 OCR]\n' + '\n'.join(lines),
            'source': {'path': path, 'sha256': 'synthetic-sha'},
            'coverage': {'complete': True, 'formal_evidence_ready': True, 'level': 'deep'},
            'evidence': [dict(evidence_id='line-' + str(i), source_path=path,
                              page=1, label='ocr_text', text=text,
                              source_sha256='synthetic-sha', bbox=[[0,i*10],[100,i*10],[100,i*10+8],[0,i*10+8]])
                         for i, text in enumerate(lines)]}


def summary(path=PATH):
    return {'source_path': path, 'evidence_id': 'summary:' + path,
            'text': '这是一张合成采购凭证，说明采购和付款状态。',
            'original_text': '这是一张合成采购凭证，说明采购和付款状态。',
            'evidence_role': '文件摘要', 'location': {'source': 'file_summary'}}


def stored(tmp_path, doc=None):
    store = Storage(tmp_path / 'state.db', tmp_path / 'sidecars')
    doc = document() if doc is None else doc
    store.save_document('owned-scan', PATH, doc)
    return store, doc


def recall(store, inputs=None, scores=None, scope=None):
    return ordinary_recall.augment_candidates(
        [summary()] if inputs is None else inputs,
        {PATH: 0.8} if scores is None else scores, {}, 'owned-scan',
        storage=store, scope=scope or ConversationScope('package'))


def test_short_ocr_fields_form_exact_page_evidence_in_real_index(tmp_path):
    store, doc = stored(tmp_path)
    chunks = evidence_corpus({PATH: doc})
    assert len(chunks) == 1  # prose gate still rejects isolated receipt labels
    hit = chunks[0]
    assert hit['label'] == 'ocr_page_text'
    assert '317' in hit['original_text'] and '4' in hit['original_text'] and 'UNPAID' in hit['original_text']
    assert doc['text'][hit['char_start']:hit['char_end']] == hit['original_text']
    assert hit['page'] == 1 and hit['bbox'] and hit['source_sha256'] == 'synthetic-sha'
    store.replace_evidence_index('owned-scan', chunks)
    indexed = store.search_evidence_index('owned-scan', 'UNPAID')
    assert [item['evidence_id'] for item in indexed] == [hit['evidence_id']]


def test_summary_missing_numbers_is_replaced_by_original_before_model_selection(tmp_path):
    store, doc = stored(tmp_path)
    # Index can predate the page-corpus fix; bounded persisted source still works.
    assert store.search_evidence_index('owned-scan', QUESTION) == []
    result = retrieve_evidence({}, QUESTION, indexed_chunks=recall(store),
                               semantic_scores={PATH: 0.8}, use_tfidf=False)
    citations = _normalise_citations(result['results'], 50, query=QUESTION)
    selected = aq.select_evidence(citations, ContextWindowPolicy(), QUESTION)
    assert selected and all(aq.source_kind(item) == 'original' for item in selected)
    assert any('317' in item['original_text'] and 'UNPAID' in item['original_text'] for item in selected)
    assert all(item['original_text'] in doc['text'] for item in selected)
    assert all(item['source_path'] == PATH for item in selected)


def test_scan_and_scope_exclude_foreign_sources(tmp_path):
    store, _ = stored(tmp_path)
    store.save_document('foreign-scan', PATH, document(lines=['FOREIGN SECRET 999'] * 6))
    outside = 'outside/private.png'
    store.save_document('owned-scan', outside, document(outside))
    scope = ConversationScope('directory', 'inside')
    result = recall(store, scores={outside: 0.99, PATH: 0.8}, scope=scope)
    assert result and all(item['source_path'] == PATH for item in result)
    assert '999' not in str(result) and outside not in str(result)


def test_explicit_file_scope_rejects_cross_document_evidence(tmp_path):
    doc = document()
    doc['evidence'].extend(document('outside/private.png')['evidence'])
    store, _ = stored(tmp_path, doc)
    result = recall(store, scope=ConversationScope('files', source_paths=(PATH,)))
    assert result and all(item['source_path'] == PATH for item in result)


@pytest.mark.parametrize('kind', ['missing', 'empty', 'unparsed', 'preview'])
def test_missing_or_unparsed_source_is_insufficient_and_never_invents_values(tmp_path, kind):
    doc = document()
    if kind == 'empty':
        doc.update(text='', evidence=[])
    elif kind == 'unparsed':
        doc['coverage']['formal_evidence_ready'] = False
    elif kind == 'preview':
        doc['coverage']['preview_only'] = True
    store, _ = stored(tmp_path, doc)
    if kind == 'missing':
        store.delete_document('owned-scan', PATH)
    candidates = recall(store)
    assert not candidates
    result = retrieve_evidence({}, QUESTION, indexed_chunks=candidates, semantic_scores={PATH: 0.8})
    assert result['results'] == [] and result['warnings']


def test_existing_original_and_legacy_summary_call_remain_unchanged(tmp_path):
    store, _ = stored(tmp_path)
    original = {'evidence_id': 'ordinary', 'source_path': PATH,
                'text': 'Ordinary source supports payment deadline in October.'}
    before = copy.deepcopy(original)
    assert recall(store, inputs=[original]) == [before]
    assert original == before
    legacy = ordinary_recall.augment_candidates([], {PATH: 0.8},
        {'file_summaries': [{'path': PATH, 'summary': 'existing legacy summary'}]}, 'scan')
    assert aq.source_kind(legacy[0]) == 'summary'


def test_projection_reads_are_bounded_and_do_not_hydrate_sidecars(tmp_path, monkeypatch):
    store, _ = stored(tmp_path)
    calls = []
    real = store.iter_documents_for_paths
    def observed(scan_id, paths, **kwargs):
        calls.append((scan_id, paths, kwargs))
        return real(scan_id, paths, **kwargs)
    monkeypatch.setattr(store, 'iter_documents_for_paths', observed)
    scores = {PATH: 1.0, **{'inside/file-%d.png' % i: 0.7 for i in range(30)}}
    result = recall(store, scores=scores)
    assert len(calls) == ordinary_recall.SEMANTIC_TOP_FILES
    assert all(scan == 'owned-scan' and len(paths) == 1 and kw == {'hydrate': False}
               for scan, paths, kw in calls)
    assert sum(len(item['original_text']) for item in result) <= 16000


def test_page_windows_remain_exact_bounded_and_keep_prose_heading_gate():
    from services.evidence import evidence_quality
    doc = document(lines=['Receipt field %d: %s' % (i, 'value ' * 20) for i in range(25)])
    chunks = [row for row in evidence_corpus({PATH: doc}) if row['label'] == 'ocr_page_text']
    assert len(chunks) > 1
    assert all(len(row['original_text']) <= 1200 and row['original_text'] in doc['text'] for row in chunks)
    assert evidence_quality({'text': 'PAYMENT STATUS', 'label': 'heading'})['eligible'] is False


def test_focused_summary_merge_excludes_directory_and_outside_scope():
    # Load only pure source helpers: no Flask app, worker or model initialization.
    tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
    names = {'_merge_summary_evidence', '_is_physical_package_path'}
    helpers = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
    namespace = {}
    exec(compile(helpers, 'app.py', 'exec'), namespace)
    request = SimpleNamespace(scan_id='owned-scan', scope=ConversationScope('directory', 'inside'))
    context = {'file_summaries': [{'path': p, 'summary': '摘要'} for p in ('.', PATH, 'outside/private.png')]}
    merged = namespace['_merge_summary_evidence'](request, context, [])
    assert [item['source_path'] for item in merged] == [PATH]
    assert aq.source_kind(merged[0]) == 'summary'


def test_identical_ocr_pages_keep_distinct_page_offsets():
    doc = document()
    first_page = doc['text']
    second_page = first_page.replace('[第 1 页/张 OCR]', '[第 2 页/张 OCR]')
    doc['text'] += '\n' + second_page
    second_evidence = copy.deepcopy(doc['evidence'])
    for item in second_evidence:
        item['page'] = 2
        item['evidence_id'] += '-page2'
    doc['evidence'].extend(second_evidence)
    chunks = [row for row in evidence_corpus({PATH: doc}) if row['label'] == 'ocr_page_text']
    assert [row['page'] for row in chunks] == [1, 2]
    assert chunks[0]['char_start'] < len(first_page) < chunks[1]['char_start']
    assert chunks[0]['evidence_id'] != chunks[1]['evidence_id']
    for row in chunks:
        assert doc['text'][row['char_start']:row['char_end']] == row['original_text']


def test_real_sampled_projection_cannot_create_a_fabricated_contiguous_passage(tmp_path):
    doc = document(lines=LINES * 80)
    projection = Storage._document_projection(doc, text_limit=320, evidence_limit=20)
    assert projection['coverage']['projection_text_truncated'] is True
    assert '[包级语义投影已折叠正文' in projection['text']
    chunks = evidence_corpus({PATH: projection})
    assert not any(row['label'] == 'ocr_page_text' for row in chunks)
    assert all(row['original_text'] in doc['text'] for row in chunks)


def test_duplicate_page_markers_do_not_assign_ambiguous_source_offsets():
    doc = document()
    doc['text'] += '\n' + doc['text']
    assert not any(row['label'] == 'ocr_page_text' for row in evidence_corpus({PATH: doc}))


def test_real_archive_ocr_passage_keeps_member_provenance_and_local_offsets(tmp_path):
    archive_path = tmp_path / 'inside' / 'receipt.zip'
    archive_path.parent.mkdir()
    with zipfile.ZipFile(archive_path, 'w') as archive:
        archive.writestr('receipt.png', b'synthetic-image-bytes')
    parser = UnifiedDocumentParser()
    parser._get_ocr_engine = lambda: lambda _: SimpleNamespace(txts=LINES, scores=None, boxes=None)
    parsed = parser.parse(archive_path, relative_path='inside/receipt.zip', mode='fast')
    member_text = parsed['text'].split('[压缩包成员：receipt.png]\n', 1)[1]
    passage = next(row for row in evidence_corpus({'inside/receipt.zip': parsed})
                   if row['label'] == 'ocr_page_text')
    assert passage['source_path'] == 'receipt.zip::receipt.png'
    assert passage['archive_source_path'] == 'inside/receipt.zip'
    assert passage['archive_member'] == 'receipt.png'
    assert member_text[passage['char_start']:passage['char_end']] == passage['original_text']
    assert parsed['text'][passage['archive_char_start']:passage['archive_char_end']] == passage['original_text']
    assert passage['content_sha256'] == hashlib.sha256(passage['original_text'].encode()).hexdigest()
    store = Storage(tmp_path / 'archive.db')
    store.save_document('owned-scan', 'inside/receipt.zip', parsed)
    recalled = ordinary_recall.augment_candidates([], {'inside/receipt.zip': .8}, {}, 'owned-scan',
        storage=store, scope=ConversationScope('package'))
    assert any(row.get('label') == 'ocr_page_text' for row in recalled)


def test_structured_statistics_keep_derivation_and_never_claim_literal_source(tmp_path):
    source = tmp_path / 'orders.csv'
    source.write_text('category,amount,quantity\nA,120,3\nB,50,6\nA,0,0\n', encoding='utf-8')
    document = UnifiedDocumentParser().parse(source, relative_path='orders.csv', mode='fast')
    store = Storage(tmp_path / 'csv.db')
    store.save_document('owned-scan', 'orders.csv', document)
    recalled = ordinary_recall.augment_candidates([], {'orders.csv': .8}, {}, 'owned-scan',
        storage=store, scope=ConversationScope('package'))
    statistic = next(row for row in recalled if row.get('label') == 'table_statistics')
    assert statistic['row_range'] == [2, 4]
    assert statistic['derivation'] == {'operation': 'count_rows', 'complete': True}
    assert statistic['table'] == '主数据表'
    assert aq.source_kind(statistic) == 'structured_statistic'
    assert statistic['original_text'] not in source.read_text(encoding='utf-8')


def test_identical_ocr_passages_in_distinct_archive_members_are_not_deduplicated(tmp_path):
    archive_path = tmp_path / 'inside' / 'two-receipts.zip'
    archive_path.parent.mkdir()
    with zipfile.ZipFile(archive_path, 'w') as archive:
        archive.writestr('first.png', b'first-synthetic-image')
        archive.writestr('second.png', b'second-synthetic-image')
    parser = UnifiedDocumentParser()
    parser._get_ocr_engine = lambda: lambda _: SimpleNamespace(txts=LINES, scores=None, boxes=None)
    parsed = parser.parse(archive_path, relative_path='inside/two-receipts.zip', mode='fast')
    passages = [row for row in evidence_corpus({'inside/two-receipts.zip': parsed})
                if row['label'] == 'ocr_page_text']
    assert {row['archive_member'] for row in passages} == {'first.png', 'second.png'}
    assert len({row['source_path'] for row in passages}) == 2
    for row in passages:
        marker = '[压缩包成员：{}]\n'.format(row['archive_member'])
        member_text = parsed['text'].split(marker, 1)[1].split('\n\n[压缩包成员：', 1)[0]
        assert member_text[row['char_start']:row['char_end']] == row['original_text']
