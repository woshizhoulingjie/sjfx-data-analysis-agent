"""Synthetic normal-import integration fixtures, prepared but not run locally.

Run only in the reviewed isolated runner, with models and external access
disabled before importing app. The actual app adapter, planner, engine and
runtime execute against explicit in-memory storage; no lifecycle is started.
"""
import copy
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as webapp
from services import ordinary_recall
from services.analysis_planner import AnalysisPlanner
from services.conversation import (
    CallableEvidenceRetriever, ConversationEngine, ConversationScope,
    ConversationSession, RetrievalRequest,
)
from services.package_questions import (
    discover_inventory_file_paths, file_discovery_query,
    file_discovery_scope_contains, is_content_file_discovery_query,
    is_filename_discovery_query,
)
from services.turn_runtime import AnalysisTurnRuntime


QUESTION = '查找文件名为巡检.txt的文件，只列出文件，不分析内容。'


def entry(path, kind='file'):
    return {'path': path, 'kind': kind,
            'payload': {'name': path.rsplit('/', 1)[-1]}}


class ForbiddenModel:
    def __getattr__(self, name):
        raise AssertionError('Synthetic file discovery must not invoke a model')


class MemoryStorage:
    def __init__(self):
        self.packages = {
            'e': {'巡检.txt': '设备记录显示运行温度为67°C，状态正常。',
                  '采购复核.csv': '采购复核金额120元，共计3项。',
                  '预算文件': '本期资金安排已完成复核。'},
            'd': {'不属于e.txt': '另一资料包设备运行温度为65°C。'},
        }
        self.indexed = False
        self.inject_unselected = False
        self.read_scans = []
        self.inventory_reads = []
        self.records = {}
        self.completed = None
        self.memory = {}

    def read(self, scan):
        self.read_scans.append(scan)
        if scan != 'e':
            raise AssertionError('Fixture crossed its bound scan')
        return self.packages[scan]

    def iter_inventory_entries(self, scan, kind=None, batch_size=1000):
        self.inventory_reads.append(scan)
        for path in self.read(scan):
            row = entry(path)
            if kind is None or row['kind'] == kind:
                yield row

    def iter_documents(self, scan, hydrate=False, batch_size=50):
        assert hydrate is False
        for path, text in self.read(scan).items():
            yield {'path': path, 'payload': {
                'source': {'path': path}, 'text': text, 'structure': {}}}

    @staticmethod
    def allowed(path, scope='.', source_paths=None):
        def under(candidate, parent):
            return (candidate == parent or candidate.startswith(parent + '/')
                    or candidate.startswith(parent + '::'))
        return ((scope in {'.', ''} or under(path, scope))
                and (not source_paths or any(under(path, p) for p in source_paths)))

    def search_evidence_index(self, scan, query, scope='.', source_paths=None,
                              limit=600, **kwargs):
        files = self.read(scan)
        if not self.indexed:
            return []
        return [{'evidence_id': 'synthetic:' + path, 'source_path': path,
                 'archive_source_path': path, 'text': text,
                 'original_text': text, 'location': {'paragraph': 1}}
                for path, text in files.items()
                if query in text and (self.inject_unselected or
                   self.allowed(path, scope, source_paths))][:limit]

    def count_evidence_index(self, scan):
        return len(self.read(scan)) if self.indexed else 0

    def search_evidence_file_paths(self, scan, query, scope='.',
                                   source_paths=None, **kwargs):
        return [path for path, text in self.read(scan).items()
                if query in text and (self.inject_unselected or
                   self.allowed(path, scope, source_paths))]

    def get_file_states(self, scan, paths):
        self.read(scan)
        return {path: {'status': 'completed'} for path in paths}

    def get_file_state(self, scan, path):
        self.read(scan)
        return {'status': 'completed'}

    def get_file_preview(self, scan, path):
        self.read(scan)
        return {}

    def get_analysis_overview(self, scan):
        count = len(self.read(scan))
        return {'coverage': {'inventory_files': count, 'parsed_files': count,
                             'deep_analyzed_files': count}}

    def file_status_counts(self, scan):
        return {'total': len(self.read(scan))}

    def count_inventory_files(self, scan, scope='.', source_paths=None,
                              extension=None):
        return sum(self.allowed(path, scope, source_paths)
                   and (not extension or PurePosixPath(path).suffix.lstrip('.')
                        == str(extension).lstrip('.'))
                   for path in self.read(scan))

    def get_conversation_research_memory(self, session_id):
        return {'payload': copy.deepcopy(self.memory)}

    def save_conversation_research_memory(self, session_id, payload):
        self.memory = copy.deepcopy(payload)

    def replace_conversation_turn_steps(self, turn_id, steps):
        self.records.setdefault(turn_id, {})['steps'] = copy.deepcopy(steps)

    def update_conversation_turn_step(self, turn_id, step_id, status, **kwargs):
        self.records.setdefault(turn_id, {}).setdefault('step_updates', []).append(
            {'step_id': step_id, 'status': status, **copy.deepcopy(kwargs)})

    def update_conversation_turn(self, turn_id, **kwargs):
        self.records.setdefault(turn_id, {}).update(
            copy.deepcopy({key: value for key, value in kwargs.items()
                           if value is not None}))

    def set_conversation_turn_message(self, turn_id, *args, **kwargs):
        self.records.setdefault(turn_id, {})['message_saved'] = True

    def complete_conversation_turn(self, turn_id, result, verification,
                                   claims, **kwargs):
        self.completed = {'result': copy.deepcopy(result),
                          'verification': copy.deepcopy(verification)}
        self.records.setdefault(turn_id, {}).update(copy.deepcopy(self.completed))


class FileDiscoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStorage()
        self.sent = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(webapp, 'storage', self.store))
        self.stack.enter_context(patch.object(webapp, '_conversation_package_context',
                                              self.package_context))
        self.stack.enter_context(patch.object(ordinary_recall, 'file_semantic_scores',
                                              return_value={}))
        self.engine = ConversationEngine(CallableEvidenceRetriever(self.retrieve),
                                         answer_model=ForbiddenModel(),
                                         translator=ForbiddenModel())

    def package_context(self, scan):
        files = self.store.read(scan)
        return {'file_summaries': [{'path': path, 'summary': text}
                                  for path, text in files.items()],
                'coverage': {}, 'reading_manifest': {}}

    def retrieve(self, request):
        self.sent.append(request)
        return webapp._conversation_retrieve(request)

    def app_result(self, question, scope=None):
        return self.retrieve(RetrievalRequest('e', question,
                            scope or ConversationScope(), 10, 'file_search'))

    def runtime_result(self, question, scope=None, scan='e', engine=None):
        selected = scope or ConversationScope()
        session = ConversationSession(scan_id=scan, scope=selected)
        turn = {'id': 'synthetic-turn', 'session_id': session.session_id,
                'scan_id': scan, 'question': question}
        runtime = AnalysisTurnRuntime(self.store, engine or self.engine,
                    planner=AnalysisPlanner(), max_revision_attempts=0,
                    verify_answers=True)
        receipt = runtime.execute(turn, session, selected,
                                  list(self.store.packages['e']))
        self.assertEqual(receipt['status'], 'completed')
        self.assertIsNotNone(self.store.completed)
        self.assertTrue(all(value == 'e' for value in self.store.read_scans))
        self.assertEqual(self.store.records['synthetic-turn']['plan']['modes'][0],
                         'file_search')
        return self.store.completed['result']

    def test_extraction_and_extensionless_idempotence(self):
        for question, expected in ((QUESTION, '巡检.txt'), ('查找 巡检', '巡检'),
                ('查找文件名为“巡检.txt”的文件，只列出文件。', '巡检.txt'),
                ('请查找 "inspection report.txt"，只列出文件，不分析内容。',
                 'inspection report.txt'), ('查找文件名为预算文件', '预算文件')):
            with self.subTest(question=question):
                self.assertEqual(file_discovery_query(question), expected)
                self.assertEqual(file_discovery_query(expected), expected)

    def test_literal_negative_wildcard_and_empty_queries(self):
        rows = [entry('巡检.txt'), entry('采购.csv')]
        for question in ('不存在.txt', '', ' ', '%', '_'):
            with self.subTest(question=question):
                self.assertEqual(discover_inventory_file_paths(
                    rows, question, ConversationScope()), ([], False))

    def test_archive_member_boundary(self):
        rows = [entry('包.zip::巡检.txt', 'logical_file'),
                entry('包.zip2::巡检.txt', 'logical_file'), entry('巡检', 'directory')]
        scope = ConversationScope(kind='files', source_paths=('包.zip',))
        self.assertEqual(discover_inventory_file_paths(rows, '巡检.txt', scope),
                         (['包.zip::巡检.txt'], False))

    def test_app_exact_filename_is_metadata_only_and_current_scan(self):
        result = self.app_result(QUESTION)
        self.assertEqual(result['related_file_paths'], ['巡检.txt'])
        self.assertEqual(result['results'], [])
        self.assertIsNone(result['coverage']['query_coverage'])
        self.assertEqual(result['coverage']['inspected_files'], 0)
        self.assertEqual(self.store.inventory_reads, ['e'])
        self.assertEqual(self.store.read_scans, ['e'])

    def test_app_missing_filename_is_authoritative_empty(self):
        result = self.app_result('查找文件名为不存在.txt的文件')
        self.assertEqual(result['related_file_paths'], [])
        self.assertEqual(result['results'], [])

    def test_app_extensionless_filename(self):
        self.assertEqual(self.app_result('查找文件名为预算文件')['related_file_paths'],
                         ['预算文件'])

    def test_app_short_keyword_with_empty_index(self):
        self.assertEqual(self.app_result('查找 巡检')['related_file_paths'], ['巡检.txt'])
        self.assertFalse(self.store.indexed)

    def test_runtime_named_filename_preserves_instruction_and_no_citations(self):
        result = self.runtime_result(QUESTION)
        self.assertEqual(result['related_file_paths'], ['巡检.txt'])
        self.assertEqual(result['citations'], [])
        self.assertEqual(result['evidence_status'], 'not_required')
        self.assertEqual(result['citation_verification_status'], 'not_required')
        self.assertEqual([request.query for request in self.sent], [QUESTION])

    def test_runtime_short_keyword_with_empty_index(self):
        self.assertEqual(self.runtime_result('查找 巡检')['related_file_paths'], ['巡检.txt'])

    def test_runtime_extensionless_filename_preserves_semantics(self):
        question = '查找文件名为预算文件'
        self.assertEqual(self.runtime_result(question)['related_file_paths'], ['预算文件'])
        self.assertEqual([request.query for request in self.sent], [question])

    def test_runtime_negative_filename(self):
        result = self.runtime_result('查找文件名为不存在.txt的文件')
        self.assertEqual(result['related_file_paths'], [])
        self.assertEqual(result['citations'], [])

    def test_runtime_selected_files_empty_intersection(self):
        scope = ConversationScope(kind='files', source_paths=('采购复核.csv',))
        self.assertEqual(self.runtime_result(QUESTION, scope)['related_file_paths'], [])

    def test_runtime_directory_and_selection_precede_explicit_resolution(self):
        self.store.packages['e'] = {'甲/巡检.txt': '设备正常。',
            '甲/采购.csv': '采购已复核。', '乙/巡检.txt': '另一目录设备正常。'}
        scope = ConversationScope(kind='directory', value='甲',
                                  source_paths=('甲/采购.csv',))
        self.assertEqual(self.runtime_result(QUESTION, scope)['related_file_paths'], [])
        self.assertNotIn('resolved_file_mentions',
                         self.store.records['synthetic-turn']['plan'])

    def test_runtime_body_filename_reference_does_not_narrow_to_named_file(self):
        self.store.packages['e']['引用记录.txt'] = '档案引用巡检.txt，已完成对应记录的复核。'
        self.store.indexed = True
        result = self.runtime_result('查找包含巡检.txt的文件')
        self.assertEqual(result['related_file_paths'], ['引用记录.txt'])
        self.assertEqual(self.sent[0].scope.kind, 'package')
        self.assertEqual(self.store.inventory_reads, [])

    def test_runtime_body_contains_does_not_union_filename(self):
        self.store.packages['e'] = {'逾期清单.txt': '本期事项均按时完成。',
            '合同.txt': '合同事项已经逾期，需要复核实际履约时间。'}
        self.store.indexed = True
        result = self.runtime_result('查找包含逾期的文件')
        self.assertEqual(result['related_file_paths'], ['合同.txt'])
        self.assertEqual(self.store.inventory_reads, [])

    def test_app_final_guard_rejects_unselected_index_and_related_paths(self):
        self.store.packages['e'] = {'甲/选中.txt': '本期按时完成。',
            '甲/未选中.txt': '本期逾期事项已复核。',
            '乙/未选中.txt': '另一目录逾期事项已复核。'}
        self.store.indexed = self.store.inject_unselected = True
        scope = ConversationScope(kind='directory', value='甲',
                                  source_paths=('甲/选中.txt',))
        result = self.app_result('查找包含逾期的文件', scope)
        self.assertEqual(result['results'], [])
        self.assertEqual(result['related_file_paths'], [])

    def test_direct_actual_engine_retains_original_extensionless_instruction(self):
        question = '查找文件名为预算文件'
        result = self.engine.ask(ConversationSession(scan_id='e'), question)
        self.assertEqual(result['related_file_paths'], ['预算文件'])
        self.assertEqual([request.query for request in self.sent], [question])

    def test_large_runtime_keeps_existing_planner_query(self):
        requests = []
        def capture(request):
            requests.append(request)
            return {'results': [], 'related_file_paths': [], 'coverage': {}}
        engine = ConversationEngine(CallableEvidenceRetriever(capture),
                                     answer_model=ForbiddenModel())
        self.runtime_result('查找 巡检', scan='lp-fixture', engine=engine)
        self.assertEqual([request.query for request in requests], ['巡检'])
        self.assertEqual(self.store.inventory_reads, [])

    def test_semantic_scope_fails_closed_and_file_type_is_preserved(self):
        rows = [entry('巡检.txt'), entry('巡检.csv')]
        self.assertEqual(discover_inventory_file_paths(rows, '巡检',
            ConversationScope(kind='topic', value='设备')), ([], False))
        self.assertEqual(discover_inventory_file_paths(rows, '巡检',
            ConversationScope(kind='file_type', value='csv')), (['巡检.csv'], False))

    def test_content_mode_and_inventory_truncation(self):
        self.assertTrue(is_content_file_discovery_query('查找包含巡检.txt的文件'))
        self.assertFalse(is_filename_discovery_query('查找包含巡检.txt的文件'))
        self.assertTrue(is_filename_discovery_query(QUESTION))
        self.assertEqual(discover_inventory_file_paths(
            [entry('甲/巡检.txt'), entry('乙/巡检.txt')], '巡检',
            ConversationScope(), limit=1), (['甲/巡检.txt'], True))
        self.assertFalse(file_discovery_scope_contains(
            ConversationScope(kind='directory', value='甲',
                              source_paths=('甲/采购.csv',)), '甲/巡检.txt'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
