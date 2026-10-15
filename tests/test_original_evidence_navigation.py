"""Source review must reconstruct real parser excerpts and table scopes."""
import ast
import hashlib
from pathlib import Path

import pytest
from flask import Flask, jsonify, request

from services.evidence import compact_evidence, select_evidence
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser


def _document_endpoint(storage):
    # Execute the actual route body without initializing app/model services.
    app_source = Path(__file__).resolve().parents[1] / 'app.py'
    module = ast.parse(app_source.read_text(encoding='utf-8'))
    route = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                 and node.name == 'get_document')
    route.decorator_list = []
    namespace = {
        'request': request, 'jsonify': jsonify, 'Path': Path,
        'storage': storage, 'select_evidence': select_evidence,
        'require_scan': storage.get_scan,
    }
    exec(compile(ast.Module(body=[route], type_ignores=[]), str(app_source), 'exec'), namespace)
    app = Flask(__name__)
    app.add_url_rule('/api/document/<scan_id>', view_func=namespace['get_document'])
    return app.test_client()


def test_csv_document_navigation_retains_computed_scope_and_derivation(tmp_path):
    source = tmp_path / 'orders.csv'
    source.write_text('category,amount,quantity\nA,120,3\nB,50,6\nA,0,0\n', encoding='utf-8')
    document = UnifiedDocumentParser().parse(source, relative_path=source.name, mode='fast')
    storage = Storage(tmp_path / 'state.db')
    scan_id = storage.save_scan({'root': str(tmp_path), 'tree': {'kind': 'directory', 'path': '.', 'children': []}, 'file_count': 1})
    storage.save_document(scan_id, source.name, document)
    response = _document_endpoint(storage).get('/api/document/' + scan_id, query_string={'path': source.name})
    assert response.status_code == 200
    result = response.get_json()['document']
    assert result['data_profile']['columns']['amount']['sum'] == 170
    assert result['data_profile']['columns']['quantity']['sum'] == 9
    evidence = next(item for item in result['evidence'] if item['label'] == 'table_statistics')
    # A consumer must distinguish a whole-table total from a sampled row.
    assert evidence['table'] == '主数据表'
    assert evidence['row_range'] == [2, 4]
    assert evidence['derivation']['operation'] == 'count_rows'
    assert evidence['derivation']['complete'] is True
    assert evidence['source_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize('mode', ['fast', 'accurate'])
def test_txt_parser_evidence_is_exact_original_and_has_replayable_offsets(tmp_path, mode):
    source = tmp_path / 'receipt.txt'
    original = '编号：SOURCE-001\n日期：2026-10-13\n采购合计120元，共3项。\n\t尚未付款，  必须核对凭证。'
    source.write_text(original, encoding='utf-8', newline='')
    document = UnifiedDocumentParser().parse(source, relative_path=source.name, mode=mode)
    assert document['text'] == original
    assert document['evidence']
    for item in document['evidence']:
        start, end = item['character_range']
        assert item['text'] == original[start:end]
        assert item['text'] in original
        assert item['source_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert item['content_sha256'] == hashlib.sha256(item['text'].encode('utf-8')).hexdigest()


def test_text_chunks_after_sections_address_the_full_parsed_document(tmp_path):
    source = tmp_path / 'sections.txt'
    original = '采购记录：' + ('保留\t双空格  原文。\n' * 120) + '\n# 付款风险\n尚未付款。'
    source.write_text(original, encoding='utf-8', newline='')
    document = UnifiedDocumentParser().parse(source, relative_path=source.name, mode='accurate')
    chunks = document['evidence']
    assert len(chunks) >= 3
    for item in chunks:
        start, end = item['character_range']
        assert item['text'] == document['text'][start:end]
        assert len(item['text']) <= 1200
    assert chunks[-1]['character_range'][0] > 1200
    assert '付款风险' in chunks[-1]['text']


def test_selected_text_keeps_parser_coordinates(tmp_path):
    source = tmp_path / 'receipt.txt'
    source.write_text('采购合计120元，共3项。\n尚未付款，必须核对凭证。', encoding='utf-8', newline='')
    document = UnifiedDocumentParser().parse(source, relative_path=source.name, mode='accurate')
    selected = select_evidence(document['evidence'], max_items=1, use_embeddings=False)
    assert selected[0]['character_range'] == [0, len(document['text'])]
    assert selected[0]['text'] == document['text']
    assert selected[0]['content_sha256'] == hashlib.sha256(document['text'].encode('utf-8')).hexdigest()


def test_compact_truncation_is_a_hashable_literal_slice_with_matching_coordinates():
    original = '金额120元。\n\t尚未付款，  必须核对凭证。'
    start = 41
    item = {'text': original, 'content_sha256': hashlib.sha256(original.encode('utf-8')).hexdigest(),
            'character_range': [start, start + len(original)],
            'char_start': start, 'char_end': start + len(original)}
    compact = compact_evidence(item, max_chars=15)
    assert compact['text'] == original[:15]
    assert compact['content_sha256'] == hashlib.sha256(original[:15].encode('utf-8')).hexdigest()
    assert compact['character_range'] == [start, start + 15]
    assert compact['char_end'] == start + 15
    assert compact['original_character_range'] == item['character_range']
    assert compact['original_char_end'] == item['char_end']
    assert compact['original_content_sha256'] == item['content_sha256']
    assert compact['excerpt'] is True


def test_repeated_sections_keep_distinct_absolute_locations(tmp_path):
    source = tmp_path / 'repeat.txt'
    section = '\n# 同一段\n采购合计120元，\t尚未付款。'
    original = '开头记录。' + section * 3
    source.write_text(original, encoding='utf-8', newline='')
    document = UnifiedDocumentParser().parse(source, relative_path=source.name, mode='accurate')
    repeated = [item for item in document['evidence'] if item['text'] == section]
    assert len(repeated) == 3
    starts = [len('开头记录。') + len(section) * index for index in range(3)]
    assert [item['character_range'][0] for item in repeated] == starts
    for item in repeated:
        start, end = item['character_range']
        assert original[start:end] == item['text']
