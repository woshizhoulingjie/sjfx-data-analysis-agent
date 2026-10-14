"""Local, small-sample regressions for saved packages and cross-file QA."""
import ast
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import Config
from services.analysis_planner import AnalysisPlanner
from services.conversation import ConversationEngine, ConversationScope, IntentRouter, RetrievalRequest
from services.large_qa_index import LargeQAIndex
from services.large_qa_search import retrieve
from services.storage import Storage


class Corpus:
    def __init__(self, base):
        self.root = base / 'sources'
        self.root.mkdir()
        self.index_base = base / 'indexes'
        self.index = LargeQAIndex('lp-regression', self.index_base, create=True)
        self.rows = []
        self.package = {'id': 'lp-regression', 'root_path': str(self.root), 'owner_id': 'owner-a'}
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE files(package_id TEXT,path TEXT,sensitive INTEGER)')

    def add(self, path, text, topic='documents'):
        target = self.root / path
        target.write_text(text, encoding='utf-8')
        st = target.stat()
        self.rows.append({'path': path, 'name': path, 'category': topic, 'group_id': topic,
                          'topic_name': topic, 'size': st.st_size, 'modified_at_ns': st.st_mtime_ns,
                          'local_summary': json.dumps({'summary': text}, ensure_ascii=False),
                          'deep_summary': '{}', 'sensitive': 0})

    def sync(self):
        self.db.executemany('INSERT INTO files VALUES(?,?,0)',
                            [('lp-regression', row['path']) for row in self.rows])
        self.db.commit()
        self.index.sync_rows(self.rows, self.package)

    def _connect(self):
        return self.db

    def get(self, package_id):
        return self.package if package_id == 'lp-regression' else None

    def original(self, path, records):
        with self.index.connect() as connection:
            version = connection.execute('SELECT source_version FROM documents WHERE path=?', (path,)).fetchone()[0]
        self.index.put_original(path, version, records, True)

    def query(self, question, scope=None, intent='retrieval'):
        # Exercise the real lexical path when the semantic model is unavailable.
        with patch('services.large_qa_search.vector_search', return_value=([], 'offline_test')):
            return retrieve(self, RetrievalRequest('lp-regression', question, scope or ConversationScope(),
                                                  top_k=40, intent=intent), base=self.index_base)


@pytest.fixture
def mixed_corpus(tmp_path):
    corpus = Corpus(tmp_path)
    corpus.add('contract.txt', '设备付款与验收要求：签约后必须支付首款，验收合格后支付尾款。')
    corpus.add('acceptance.txt', '验收须完成试运行，若不通过则暂停结算。')
    corpus.add('garden.txt', '花园栽种与盆栽绿化说明。')
    corpus.sync()
    corpus.original('contract.txt', [
        {'text': f'设备付款与验收要求，第 {number} 条：签约后必须按合同付款，另有程序须遵守。',
         'location': {'paragraph': number}} for number in range(140)
    ])
    corpus.original('acceptance.txt', [{'text': '验收须完成试运行，若不通过则暂停结算。'}])
    yield corpus
    corpus.db.close()


def test_long_document_cannot_hide_short_complementary_source(mixed_corpus):
    result = mixed_corpus.query('设备付款与验收要求')
    assert set(result['candidate_paths']) == {'contract.txt', 'acceptance.txt'}
    assert {item['source_path'] for item in result['results']} == {'contract.txt', 'acceptance.txt'}
    assert result['coverage']['scope_files'] == 3
    assert result['coverage']['inspected_files'] == 2
    assert not result['coverage']['package_reading_complete']
    assert any('暂停结算' in item['text'] for item in result['results'])


def test_explicit_file_scope_and_unrelated_question_stay_precise(mixed_corpus):
    scope = ConversationScope('files', source_paths=('contract.txt',))
    result = mixed_corpus.query('设备付款与验收要求', scope)
    assert {item['source_path'] for item in result['results']} == {'contract.txt'}
    assert result['coverage']['scope_files'] == 1
    assert mixed_corpus.query('量子星舰推进器维修')['results'] == []


def capture_answer_input(result, question):
    engine = ConversationEngine(SimpleNamespace(retrieve=lambda _request: result), answer_model=object())
    calls = []

    def model(_model, _system, prompt, **_kwargs):
        evidence = json.loads(prompt.split('编号证据（具体事实仅据以下内容，保留各条 source_kind 的来源身份）：\n', 1)[1])
        calls.append(evidence)
        return '\n\n'.join(item['original_text'] + ' ' + item['citation'] for item in evidence)

    # Keep all-source generation coverage explicit for this fake 32K model.
    with patch.object(Config, 'LLM_CONTEXT_TOKENS', 32768), \
            patch.object(Config, 'CONVERSATION_CONTEXT_TOKENS', 32768), \
            patch('services.conversation._model_text', side_effect=model), \
            patch('services.large_qa_synthesis.prepare_global', side_effect=lambda value, *_args: value):
        turn = engine.ask(engine.new_session('lp-regression'), question, retrieval_override=result)
    return turn, calls


def test_cross_file_evidence_reaches_answer_prompt_and_saved_manifest(mixed_corpus):
    question = '设备付款与验收要求'
    result = mixed_corpus.query(question)
    turn, calls = capture_answer_input(result, question)
    assert len(calls) == 1
    assert {item['source_path'] for item in calls[0]} == {'contract.txt', 'acceptance.txt'}
    assert set(turn['input_manifest']['selected_paths']) == {'contract.txt', 'acceptance.txt'}
    assert '暂停结算' in turn['answer']
    assert set(turn['related_file_paths']) == {'contract.txt', 'acceptance.txt'}


@pytest.mark.parametrize('question', ['整个数据包都讲了什么内容？', '这个数据包都讲了什么内容呢？',
                                     '这批资料讲的是什么？', '这些资料都讲些什么？'])
def test_real_user_overview_wording_uses_summary_intent(question):
    assert IntentRouter().route(question).name == 'summary'
    assert 'summary' in AnalysisPlanner().plan(question, {'kind': 'package'})['modes']


def test_unmapped_historical_package_overview_uses_multiple_files(tmp_path):
    corpus = Corpus(tmp_path)
    for number in range(20):
        corpus.add(f'{number:02}.txt', f'材料编号{number}研究不同的工程性能和环境影响。')
    corpus.sync()
    try:
        # Caller intentionally uses old retrieval intent: natural wording alone
        # must enable package overview for an already saved historical plan.
        question = '整个数据包都讲了什么内容？'
        result = corpus.query(question, intent='retrieval')
        assert result['package_context']['large_qa']['global_mode']
        assert len(result['candidate_paths']) == 20
        assert len({item['source_path'] for item in result['results']}) == 20
        turn, calls = capture_answer_input(result, question)
        assert len({item['source_path'] for item in calls[0]}) == 20
        assert turn['input_manifest']['selected_files'] == 20
        assert not result['coverage']['package_reading_complete']
    finally:
        corpus.db.close()


def test_pre_ledger_documents_and_evidence_keep_package_usable(tmp_path):
    storage = Storage(tmp_path / 'history.db', tmp_path / 'sidecars')
    with storage._connect() as connection:
        for scan_id in ('documents-only', 'evidence-only', 'empty-saved-package'):
            connection.execute('INSERT INTO scans(id,root_path,payload,owner_id) VALUES(?,?,?,?)',
                               (scan_id, '/missing/old/source', '{}', 'owner-a'))
        connection.execute('INSERT INTO unified_documents(scan_id,node_path,payload) VALUES(?,?,?)',
                           ('documents-only', 'a.txt', '{"text_content":"旧正文仍存在。"}'))
        connection.execute('INSERT INTO evidence_index(scan_id,index_key,source_path,payload) VALUES(?,?,?,?)',
                           ('evidence-only', 'e1', 'b.txt', '{"text":"旧索引仍存在。"}'))
    items = {item['scan_id']: item for item in storage.list_scan_sources(owner_id='owner-a')['items']}
    assert items['documents-only']['usable']
    assert items['evidence-only']['usable']
    assert not items['empty-saved-package']['usable']
    assert all(item['selectable'] for item in items.values())
    assert storage.list_scan_sources(owner_id='owner-b')['items'] == []
    assert storage.get_search_index_state('documents-only') is None


def test_legacy_index_ask_gate_remains_usable_without_generation_ledger(tmp_path):
    # Execute the real route helper without importing the Flask application's
    # production configuration or starting any worker. Business data is a tiny
    # temporary Storage, not a mock of the compatibility decision under test.
    storage = Storage(tmp_path / 'gate.db', tmp_path / 'sidecars')
    with storage._connect() as connection:
        for scan_id in ('legacy-full', 'saved-doc-only', 'empty-history'):
            connection.execute('INSERT INTO scans(id,root_path,payload,owner_id) VALUES(?,?,?,?)',
                               (scan_id, '/missing/source', '{}', 'owner-a'))
        for scan_id in ('legacy-full', 'saved-doc-only'):
            connection.execute('INSERT INTO unified_documents(scan_id,node_path,payload) VALUES(?,?,?)',
                               (scan_id, 'a.txt', '{"text_content":"旧正文必须保持可用。"}'))
        connection.execute('INSERT INTO evidence_index(scan_id,index_key,source_path,payload) VALUES(?,?,?,?)',
                           ('legacy-full', 'e1', 'a.txt', '{"text":"旧索引必须保持可用。"}'))
    module = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
    helper = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                  and node.name == '_conversation_index_status')
    namespace = {'storage': storage}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), 'actual-index-status-helper', 'exec'), namespace)
    status = namespace['_conversation_index_status']
    full = status('legacy-full')
    assert full['usable'] and full['ready']
    assert full['state'] == 'legacy_ready'
    assert full['evidence_records'] == 1
    partial = status('saved-doc-only')
    assert partial['usable'] and partial['partial'] and not partial['ready']
    assert status('empty-history')['usable'] is False
    assert storage.get_search_index_state('legacy-full') is None
