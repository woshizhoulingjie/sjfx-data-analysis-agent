"""Full-package QA must account for current sources and actual model inputs.

Real temporary SQLite indexes and physical files exercise the retrieval boundary.
Models only echo supplied source IDs; these regressions never use a model endpoint.
"""
from contextlib import closing, contextmanager
import json
import re
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from config import Config
from services.conversation import ConversationScope, RetrievalRequest, _model_text as real_model_text
from services.large_qa_index import LargeQAIndex
from services.large_qa_search import retrieve
from services.large_qa_synthesis import prepare_global
from services.large_qa_topics import refresh_maps


class CurrentSourceStore:
    """Persistent source inventory independent of the disposable QA index."""

    columns = (
        'package_id', 'path', 'name', 'category', 'group_id', 'topic_name',
        'size', 'modified_at_ns', 'sensitive', 'source_changed',
        'local_summary', 'deep_summary', 'vector', 'source_kind', 'source_metadata',
    )

    def __init__(self, filename, package):
        self.filename = filename
        self.db_path = filename
        self.package = package
        with self._connect() as connection:
            connection.execute('CREATE TABLE packages (id TEXT PRIMARY KEY, root_path TEXT, owner_id TEXT)')
            connection.execute('INSERT INTO packages VALUES (?, ?, ?)',
                               (package['id'], package['root_path'], package['owner_id']))
            connection.execute('''CREATE TABLE files (
                package_id TEXT, path TEXT, name TEXT, category TEXT,
                group_id TEXT, topic_name TEXT, size INTEGER, modified_at_ns INTEGER,
                sensitive INTEGER, source_changed INTEGER, local_summary TEXT,
                deep_summary TEXT, vector BLOB, source_kind TEXT, source_metadata TEXT,
                PRIMARY KEY(package_id, path))''')

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(str(self.filename))) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection

    def get(self, package_id):
        return dict(self.package) if package_id == self.package['id'] else None

    def get_file(self, package_id, path):
        with self._connect() as connection:
            row = connection.execute(
                'SELECT * FROM files WHERE package_id=? AND path=?', (package_id, path),
            ).fetchone()
        return dict(row) if row else None

    def insert(self, row):
        with self._connect() as connection:
            connection.execute(
                'INSERT INTO files (' + ','.join(self.columns) + ') VALUES ('
                + ','.join('?' for _ in self.columns) + ')',
                [row[name] for name in self.columns],
            )

    def change(self, path, **updates):
        assert set(updates).issubset(self.columns)
        with self._connect() as connection:
            connection.execute(
                'UPDATE files SET ' + ','.join(name + '=?' for name in updates)
                + ' WHERE package_id=? AND path=?',
                [*updates.values(), self.package['id'], path],
            )

    def delete(self, path):
        with self._connect() as connection:
            connection.execute('DELETE FROM files WHERE package_id=? AND path=?',
                               (self.package['id'], path))


@pytest.fixture
def corpus_project(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    root.mkdir()
    base = tmp_path / 'qa'
    package = {'id': 'lp-full-corpus-test', 'root_path': str(root), 'owner_id': 'tester'}
    store = CurrentSourceStore(tmp_path / 'source.db', package)
    index = LargeQAIndex(package['id'], base, create=True)
    monkeypatch.setattr(Config, 'CONVERSATION_CONTEXT_TOKENS', 4096)

    def no_endpoint(*args, **kwargs):
        raise AssertionError('full-corpus regressions must not call external models')

    monkeypatch.setattr('services.large_package_semantic.embedding_client', no_endpoint)
    monkeypatch.setattr('services.conversation._model_text', no_endpoint)
    case = SimpleNamespace(root=root, base=base, package=package, store=store,
                           index=index, rows=[])

    def add(path, text, *, topic='energy', summary=None):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf8')
        stat = target.stat()
        row = {
            'package_id': package['id'], 'path': path, 'name': target.name,
            'category': topic, 'group_id': topic, 'topic_name': topic,
            'size': stat.st_size, 'modified_at_ns': stat.st_mtime_ns,
            'sensitive': 0, 'source_changed': 0,
            'local_summary': json.dumps({'summary': text if summary is None else summary}),
            'deep_summary': '{}', 'vector': np.array([1., 0., 0.], dtype=np.float32).tobytes(),
            'source_kind': '', 'source_metadata': '{}',
        }
        store.insert(row)
        case.rows.append(row)
        return row

    def sync(rows=None):
        index.sync_rows(case.rows if rows is None else rows, package, 'test-model')
        refresh_maps(index)

    def original(path, text, *, complete=True, page=9):
        with index.connect() as connection:
            document = dict(connection.execute(
                'SELECT * FROM documents WHERE path=?', (path,),
            ).fetchone())
        assert index.put_original(path, document['source_version'],
                                  [{'text': text, 'location': {'page': page}}], complete)

    case.add, case.sync, case.original = add, sync, original
    return case


def query(case, text='概括整个资料包的研究方向与限制', scope=None):
    return retrieve(case.store, RetrievalRequest(case.package['id'], text,
                    scope or ConversationScope(), top_k=40, intent='summary'),
                    base=case.base, query_vector=np.array([1., 0., 0.]))


def scan(case, text='概括整个资料包的研究方向与限制', scope=None):
    from services.large_qa_corpus import scan_corpus
    return scan_corpus(case.index, text, scope=scope or ConversationScope(),
                       store=case.store, root=case.root)


def populate(case, count=121, topics=97, *, long=False):
    for number in range(count):
        marker = 'FileMarker_%04d' % number
        text = marker + ' reports a distinct source finding and its operating limitation. '
        if long:
            text += 'Independent material records its procedure and operating conditions. ' * 70
        case.add('directory-%d/file-%04d.txt' % (number % 6, number), text,
                 topic='topic-%03d' % (number % topics))
    case.sync()
    return {row['path'] for row in case.rows}


class RecordingMapper:
    """Observe JSON supplied to the model; return a grounded extractive point."""

    model = 'recording-full-corpus-model'

    def __init__(self, on_call=None, fail_calls=()):
        self.prompts = []
        self.on_call = on_call
        self.fail_calls = set(fail_calls)

    @staticmethod
    def entries(value):
        if isinstance(value, dict):
            if isinstance(value.get('id'), str) and isinstance(value.get('text'), str):
                yield value
            for child in value.values():
                yield from RecordingMapper.entries(child)
        elif isinstance(value, list):
            for child in value:
                yield from RecordingMapper.entries(child)

    def __call__(self, model, system, prompt, **kwargs):
        data = json.loads(prompt)
        self.prompts.append(data)
        if self.on_call:
            self.on_call(len(self.prompts), data)
        if len(self.prompts) in self.fail_calls:
            raise TimeoutError('synthetic mapping timeout')
        entries = list(self.entries(data))
        assert entries, 'every mapping call must supply source text and source IDs'
        first = entries[0]
        return json.dumps({'points': [{'text': first['text'], 'sources': [first['id']]}]})

    def markers(self):
        return set(re.findall(r'FileMarker_\d{4}', json.dumps(self.file_prompts())))

    def paths(self):
        return {item['path'] for prompt in self.file_prompts() for item in prompt['files']}

    def file_prompts(self):
        return [prompt for prompt in self.prompts if isinstance(prompt.get('files'), list)]


def test_every_source_is_mapped_beyond_eight_batches(corpus_project, monkeypatch):
    case = corpus_project
    expected = populate(case, long=True)
    mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', mapper)
    result = prepare_global(query(case), '概括整个资料包的研究方向与限制', mapper,
                            base=case.base, store=case.store, scope=ConversationScope())

    assert len(mapper.file_prompts()) > 8
    assert mapper.paths() == expected
    assert mapper.markers() == {'FileMarker_%04d' % i for i in range(121)}
    state = result['package_context']['large_qa']
    assert state['source_files'] == 121
    assert state['scanned_files'] == 121
    assert state['model_mapped_files'] == 121
    assert state['map_complete'] is True
    assert state['synthesis_complete'] is True


@pytest.mark.parametrize('question', [
    '请综合整个资料包的所有文件，说明付款条件和预算限制。',
    '请综合资料包全部文件，说明第12项付款安排与预算。',
])
def test_explicit_full_package_financial_questions_use_global_mode(corpus_project, question):
    case = corpus_project
    case.add('procurement/contract.txt', '付款分两期，验收后才能支付剩余预算。')
    case.add('engineering/exception.txt', '例外项目需要完成热测试后才可批准付款。')
    case.sync()
    result = query(case, text=question)
    assert result['package_context']['large_qa']['global_mode'] is True
    assert result['coverage']['scope_files'] == 2


def test_missing_model_still_scans_every_source_and_discloses_incomplete_synthesis(corpus_project):
    case = corpus_project
    populate(case)
    result = prepare_global(query(case), '概括整个资料包的研究方向与限制', None,
                            base=case.base, store=case.store)
    state = result['package_context']['large_qa']
    assert state['source_files'] == 121
    assert state['scanned_files'] == 121
    assert state['model_mapped_files'] == 0
    assert state['map_complete'] is False
    assert state['synthesis_complete'] is False
    assert result['results']
    assert any('模型' in warning for warning in result['warnings'])


def test_inventory_denominator_includes_files_missing_from_index(corpus_project):
    case = corpus_project
    for number in range(101):
        case.add('active/file-%03d.txt' % number,
                 'Distinct procurement source %s records approval conditions.' % number)
    for number in range(13):
        case.add('outside/file-%03d.txt' % number, 'Unrelated source outside selected directory.')
    case.sync(case.rows[:20])
    scope = ConversationScope('directory', 'active')
    corpus = scan(case, scope=scope)
    stats = corpus['stats']
    assert stats['scope_files'] == 101
    assert stats['indexed_scope_files'] == 20
    assert stats['missing_index_files'] == 81
    assert stats['files_without_content'] == 0
    assert {item['path'] for item in corpus['capsules']} == {
        'active/file-%03d.txt' % i for i in range(20)
    }
    assert stats['complete'] is False
    result = query(case, scope=scope)
    assert result['coverage']['scope_files'] == 101
    assert result['coverage']['summary_backed_files'] == 20
    assert result['package_context']['reading_manifest']['complete'] is False


def test_topic_scope_spans_directories_without_outside_content(corpus_project):
    case = corpus_project
    expected = set()
    for number in range(103):
        path = 'directory-%d/file-%04d.txt' % (number % 6, number)
        expected.add(path)
        case.add(path, 'SelectedTopicMarker_%04d records independent acceptance limits.' % number,
                 topic='selected')
    case.add('outside/louder.txt', 'OutsideSecretMarker acceptance limits. ' * 100, topic='other')
    case.sync()
    scope = ConversationScope('topic', 'selected')
    corpus = scan(case, scope=scope)
    assert corpus['stats']['scope_files'] == 103
    assert {item['path'] for item in corpus['capsules']} == expected
    assert 'OutsideSecretMarker' not in json.dumps(corpus)
    result = query(case, scope=scope)
    assert {row['source_path'] for row in result['results']}.issubset(expected)
    assert result['coverage']['scope_files'] == 103
    assert 'OutsideSecretMarker' not in json.dumps(result['package_context'])


@pytest.mark.parametrize('change', ['delete', 'sensitive', 'source_changed', 'physical'])
def test_current_sources_replace_stale_topic_provenance(corpus_project, change):
    case = corpus_project
    case.add('selected/live.txt', 'CurrentSourceMarker records current acceptance conditions.')
    case.add('selected/stale.txt', 'StaleSecretMarker records superseded acceptance conditions.')
    case.sync()
    if change == 'delete':
        case.store.delete('selected/stale.txt')
    elif change == 'physical':
        (case.root / 'selected/stale.txt').write_text('replacement physical body', encoding='utf8')
    else:
        case.store.change('selected/stale.txt', **{change: 1})
    corpus = scan(case)
    assert 'StaleSecretMarker' not in json.dumps(corpus)
    result = query(case)
    assert 'StaleSecretMarker' not in json.dumps(result['package_context'])
    assert all(row['source_path'] != 'selected/stale.txt' for row in result['results'])


def test_all_original_windows_are_scanned_and_tail_evidence_survives(corpus_project):
    case = corpus_project
    body = 'Earlier general description. ' * 3000
    body += 'TailEvidenceMarker acceptance requires written approval after thermal testing.'
    case.add('research/long.txt', body,
             summary='The summary describes general procedures without acceptance details.')
    case.sync()
    case.original('research/long.txt', body, page=27)
    corpus = scan(case, text='written approval after thermal testing')
    capsule = next(item for item in corpus['capsules'] if item['path'] == 'research/long.txt')
    tail = next(point for point in capsule['points'] if 'TailEvidenceMarker' in point['text'])
    assert tail['kind'] == 'original'
    assert tail['location']['page'] == 27
    assert tail['text'] in body
    with case.index.connect() as connection:
        expected_chunks = connection.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    assert corpus['stats']['chunks'] == expected_chunks


def test_original_without_summary_is_a_model_source(corpus_project, monkeypatch):
    case = corpus_project
    case.add('original-only.txt', 'OriginalOnlyMarker records independent pressure limits.', summary='')
    case.add('summary-only.txt', 'SummaryOnlyMarker records separate temperature limits.')
    case.sync()
    case.original('original-only.txt', 'OriginalOnlyMarker records independent pressure limits.')
    corpus = scan(case)
    assert corpus['stats']['scope_files'] == 2
    assert corpus['stats']['summary_files'] == 1
    assert corpus['stats']['original_files'] == 1
    assert corpus['stats']['files_without_content'] == 0
    mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', mapper)
    result = prepare_global(query(case), '概括整个资料包的研究方向与限制', mapper,
                            base=case.base, store=case.store, scope=ConversationScope())
    actual_inputs = json.dumps(mapper.prompts)
    assert 'OriginalOnlyMarker' in actual_inputs
    assert 'SummaryOnlyMarker' in actual_inputs
    assert result['package_context']['large_qa']['map_complete'] is True
    reading = result['package_context']['reading_manifest']
    assert reading['complete'] is True
    assert reading['available_content_complete'] is True
    assert reading['summary_complete'] is False
    assert reading['summary_backed_files'] == 1


def test_synthesis_can_recheck_inventory_from_retrieval_context(corpus_project, monkeypatch):
    case = corpus_project
    case.add('current/a.txt', 'CurrentSourceMarker records valid acceptance conditions.')
    case.add('current/b.txt', 'SecondSourceMarker records independent temperature limits.')
    case.sync()
    retrieval = query(case)
    mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', mapper)
    # ConversationEngine calls synthesis without an explicit source-store object.
    result = prepare_global(retrieval, '概括整个资料包的研究方向与限制', mapper, base=case.base)
    state = result['package_context']['large_qa']
    assert mapper.paths() == {'current/a.txt', 'current/b.txt'}
    assert state['source_snapshot_current'] is True
    assert state['synthesis_complete'] is True


def test_changed_summary_invalidates_cached_synthesis(corpus_project, monkeypatch):
    case = corpus_project
    changed = case.add('current/a.txt', 'The physical file remains available.',
                       summary='OldSummaryMarker records obsolete acceptance conditions.')
    case.add('current/b.txt', 'SecondSourceMarker records independent temperature limits.')
    case.sync()
    first_mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', first_mapper)
    first = prepare_global(query(case), '概括整个资料包的研究方向与限制', first_mapper,
                           base=case.base, store=case.store)
    assert first['package_context']['large_qa']['synthesis_complete'] is True

    changed['local_summary'] = json.dumps({
        'summary': 'NewSummaryMarker records revised acceptance conditions.',
    })
    case.store.change(changed['path'], local_summary=changed['local_summary'])
    case.sync()
    second_mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', second_mapper)
    second = prepare_global(query(case), '概括整个资料包的研究方向与限制', second_mapper,
                            base=case.base, store=case.store)
    assert second_mapper.file_prompts(), 'changed content must reach the model despite a previous cache'
    assert 'NewSummaryMarker' in json.dumps(second_mapper.file_prompts())
    assert 'OldSummaryMarker' not in json.dumps(second_mapper.file_prompts())
    assert 'OldSummaryMarker' not in json.dumps(second['package_context']['large_qa']['map_notes'])


def test_deadline_is_partial_and_next_request_continues_cached_sources(corpus_project, monkeypatch):
    case = corpus_project
    expected = populate(case, count=45, topics=17, long=True)
    elapsed = [0.]
    monkeypatch.setattr('services.large_qa_synthesis.time.monotonic', lambda: elapsed[0])

    def exhaust_budget(call_number, prompt):
        elapsed[0] += 20.

    first_mapper = RecordingMapper(on_call=exhaust_budget)
    monkeypatch.setattr('services.conversation._model_text', first_mapper)
    retrieval = query(case)
    first = prepare_global(retrieval, '概括整个资料包的研究方向与限制', first_mapper,
                           base=case.base, store=case.store, time_budget=10)
    first_state = first['package_context']['large_qa']
    processed_paths = first_mapper.paths()
    assert 0 < len(processed_paths) < len(expected)
    assert first_state['model_mapped_files'] == len(processed_paths)
    assert first_state['map_complete'] is False
    assert first_state['synthesis_complete'] is False
    assert first['warnings']

    elapsed[0] = 0.
    second_mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', second_mapper)
    second = prepare_global(retrieval, '概括整个资料包的研究方向与限制', second_mapper,
                            base=case.base, store=case.store, time_budget=100)
    second_state = second['package_context']['large_qa']
    assert processed_paths.isdisjoint(second_mapper.paths())
    assert processed_paths | second_mapper.paths() == expected
    assert second_state['map_cache_hit'] is True
    assert second_state['model_mapped_files'] == len(expected)
    assert second_state['map_complete'] is True
    assert second_state['synthesis_complete'] is True


def test_failed_mapping_is_disclosed_and_only_failed_batch_is_retried(corpus_project, monkeypatch):
    case = corpus_project
    expected = populate(case, count=21, topics=11, long=True)
    failed_mapper = RecordingMapper(fail_calls={2})
    monkeypatch.setattr('services.conversation._model_text', failed_mapper)
    retrieval = query(case)
    first = prepare_global(retrieval, '概括整个资料包的研究方向与限制', failed_mapper,
                           base=case.base, store=case.store, time_budget=100)
    state = first['package_context']['large_qa']
    assert len(failed_mapper.file_prompts()) >= 3
    failed_paths = {item['path'] for item in failed_mapper.prompts[1]['files']}
    assert state['map_failures'] == 1
    assert state['model_mapped_files'] == len(expected - failed_paths)
    assert state['map_complete'] is False
    assert state['synthesis_complete'] is False
    assert first['warnings']
    assert first['results'], 'a failed mapper must preserve usable existing evidence'

    retry_mapper = RecordingMapper()
    monkeypatch.setattr('services.conversation._model_text', retry_mapper)
    second = prepare_global(retrieval, '概括整个资料包的研究方向与限制', retry_mapper,
                            base=case.base, store=case.store, time_budget=100)
    assert retry_mapper.paths() == failed_paths
    assert second['package_context']['large_qa']['map_complete'] is True
    assert second['package_context']['large_qa']['synthesis_complete'] is True


class MarkerMapper(RecordingMapper):
    """Keep the independently sourced findings named by a regression scenario."""

    def __init__(self, markers):
        super().__init__()
        self.selected_markers = markers

    def __call__(self, model, system, prompt, **kwargs):
        data = json.loads(prompt)
        self.prompts.append(data)
        entries = list(self.entries(data))
        points = []
        for marker in self.selected_markers:
            entry = next((item for item in entries if marker in item['text']), None)
            if entry:
                points.append({'text': entry['text'], 'sources': [entry['id']]})
        assert points, 'target findings must reach the model as supplied source text'
        return json.dumps({'points': points})


def test_two_distant_original_findings_keep_their_own_citation_chunks(corpus_project, monkeypatch):
    case = corpus_project
    early = 'EarlyFindingMarker acceptance is normally granted after written approval.'
    late = 'LateExceptionMarker however acceptance is prohibited when thermal testing fails.'
    filler = 'General intermediate material describes equipment maintenance. ' * 300
    row = case.add('research/long.txt', early + '\n' + filler + '\n' + late,
                   summary='The overview describes general acceptance procedures.')
    case.add('research/context.txt', 'A separate contextual source describes equipment operations.')
    case.sync()
    with case.index.connect() as connection:
        document = dict(connection.execute(
            'SELECT * FROM documents WHERE path=?', (row['path'],),
        ).fetchone())
    assert case.index.put_original(row['path'], document['source_version'], [
        {'text': early, 'location': {'page': 1}},
        {'text': filler, 'location': {'page': 12}},
        {'text': late, 'location': {'page': 45}},
    ], True)
    question = '请综合全部资料，解释 EarlyFindingMarker 与 LateExceptionMarker 的验收条件和例外。'
    retrieval = query(case, text=question)
    retrieval['results'] = []
    mapper = MarkerMapper(['EarlyFindingMarker', 'LateExceptionMarker'])
    monkeypatch.setattr('services.conversation._model_text', mapper)
    result = prepare_global(retrieval, question, mapper, base=case.base, store=case.store)
    originals = [item for item in result['results'] if item['source_path'] == row['path']
                 and item['evidence_level'] == 'passage']
    early_quote = next(item for item in originals if 'EarlyFindingMarker' in item['text'])
    late_quote = next(item for item in originals if 'LateExceptionMarker' in item['text'])
    assert early_quote['evidence_id'] != late_quote['evidence_id']
    assert early_quote['location']['page'] == 1
    assert late_quote['location']['page'] == 45
    assert early_quote['text'] in early
    assert late_quote['text'] in late
    notes = result['package_context']['large_qa']['map_notes']
    assert 'EarlyFindingMarker' in json.dumps(notes)
    assert 'LateExceptionMarker' in json.dumps(notes)


def test_saved_retrieval_cannot_mix_obsolete_evidence_into_new_synthesis(corpus_project, monkeypatch):
    case = corpus_project
    changed = case.add('current/a.txt', 'The physical file remains available.',
                       summary='ObsoleteEvidenceMarker records superseded acceptance conditions.')
    case.add('current/b.txt', 'SecondSourceMarker records independent temperature limits.')
    case.sync()
    saved_retrieval = query(case)
    old = next(item for item in saved_retrieval['results']
               if 'ObsoleteEvidenceMarker' in item['text'])
    changed['local_summary'] = json.dumps({
        'summary': 'ReplacementEvidenceMarker records the revised acceptance conditions.',
    })
    case.store.change(changed['path'], local_summary=changed['local_summary'])
    case.sync()
    mapper = MarkerMapper(['ReplacementEvidenceMarker'])
    monkeypatch.setattr('services.conversation._model_text', mapper)
    result = prepare_global(saved_retrieval, '概括整个资料包的研究方向与限制', mapper,
                            base=case.base, store=case.store)
    assert any('ReplacementEvidenceMarker' in item['text'] for item in result['results'])
    assert all(item['evidence_id'] != old['evidence_id'] for item in result['results'])
    assert all(item['source_hash'] != old['source_hash'] for item in result['results'])
    assert 'ObsoleteEvidenceMarker' not in json.dumps(result['results'])
    assert 'ObsoleteEvidenceMarker' not in json.dumps(result['package_context']['large_qa']['map_notes'])


def test_real_archive_member_synthesis_preserves_identity_and_original_location(
        tmp_path, monkeypatch):
    import zipfile
    from services.large_package_isolated import LargePackageStore, iter_inventory

    root = tmp_path / 'archive-sources'
    root.mkdir()
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (root,))
    monkeypatch.setattr(Config, 'DATA_DIR', tmp_path / 'state')
    monkeypatch.setattr(Config, 'CONVERSATION_CONTEXT_TOKENS', 4096)
    monkeypatch.setenv('SJFX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('LARGE_PACKAGE_MODEL_SUMMARY', '0')
    member_body = 'ArchiveFindingMarker however acceptance requires completed pressure testing.'
    with zipfile.ZipFile(root / 'records.zip', 'w') as archive:
        archive.writestr('engineering/acceptance.txt', member_body)
    (root / 'ordinary.txt').write_text('Independent contextual material.', encoding='utf8')
    store = LargePackageStore(tmp_path / 'large.db')
    package = store.create(root, owner_id='archive-owner')
    store.upsert_files(package['id'], list(iter_inventory(root)))
    path = 'records.zip::engineering/acceptance.txt'
    store.update_file(package['id'], path,
                      local_summary={'summary': 'General archive acceptance overview.'})
    store.update_file(package['id'], 'ordinary.txt',
                      local_summary={'summary': 'Independent contextual material.'})
    base = tmp_path / 'qa'
    index = LargeQAIndex(package['id'], base, create=True)
    index.sync_store(store)
    refresh_maps(index)
    with index.connect() as connection:
        document = dict(connection.execute(
            'SELECT * FROM documents WHERE path=?', (path,),
        ).fetchone())
    assert index.put_original(path, document['source_version'],
                              [{'text': member_body, 'location': {'page': 7}}], True)

    def no_endpoint(*args, **kwargs):
        raise AssertionError('archive synthesis regression must not call a model endpoint')

    monkeypatch.setattr('services.large_package_semantic.embedding_client', no_endpoint)
    mapper = MarkerMapper(['ArchiveFindingMarker'])
    monkeypatch.setattr('services.conversation._model_text', mapper)
    case = SimpleNamespace(store=store, package=package, base=base)
    question = '请综合全部资料解释 ArchiveFindingMarker 的验收限制。'
    retrieval = query(case, text=question)
    retrieval['results'] = []
    result = prepare_global(retrieval, question, mapper, base=base, store=store)
    citation = next(item for item in result['results'] if 'ArchiveFindingMarker' in item['text'])
    assert citation['source_path'] == path
    assert citation['evidence_level'] == 'passage'
    assert citation['location']['page'] == 7
    assert citation['location']['container_path'] == 'records.zip'
    assert citation['location']['archive_member'] == 'engineering/acceptance.txt'
    assert citation['text'] in member_body
    assert result['package_context']['large_qa']['source_snapshot_current'] is True


@pytest.mark.parametrize('change', ['delete', 'sensitive'])
def test_revocation_during_mapping_discards_stale_notes_and_citations(
        corpus_project, monkeypatch, change):
    case = corpus_project
    case.add('selected/revoked.txt', 'RevokedSecretMarker records withdrawn acceptance limits.')
    case.add('selected/live.txt', 'CurrentSourceMarker records current acceptance conditions.')
    case.sync()

    def revoke(call_number, prompt):
        if call_number == 1:
            if change == 'delete':
                case.store.delete('selected/revoked.txt')
            else:
                case.store.change('selected/revoked.txt', sensitive=1)

    mapper = RecordingMapper(on_call=revoke)
    monkeypatch.setattr('services.conversation._model_text', mapper)
    result = prepare_global(query(case), '概括整个资料包的研究方向与限制', mapper,
                            base=case.base, store=case.store, time_budget=100)
    state = result['package_context']['large_qa']
    assert state['source_snapshot_current'] is False
    assert state['synthesis_complete'] is False
    assert all(row['source_path'] != 'selected/revoked.txt' for row in result['results'])
    assert 'RevokedSecretMarker' not in json.dumps(state.get('map_notes'))
    assert 'selected/revoked.txt' not in json.dumps(state.get('notes_provenance'))
    assert result['warnings']


@pytest.mark.parametrize('context_tokens', [4096, 32768])
def test_actual_mapping_prompt_and_output_budget_fit_context_without_losing_sources(
        corpus_project, monkeypatch, context_tokens):
    from services import answer_quality as aq

    case = corpus_project
    monkeypatch.setattr(Config, 'CONVERSATION_CONTEXT_TOKENS', context_tokens)
    expected = populate(case, long=True)
    mapper = RecordingMapper()
    actual_budgets = []

    def bounded_mapper(model, system, prompt, **kwargs):
        actual_budgets.append((aq.count_tokens(system + prompt), kwargs['max_tokens']))
        return mapper(model, system, prompt, **kwargs)

    monkeypatch.setattr('services.conversation._model_text', bounded_mapper)
    result = prepare_global(query(case), '概括整个资料包的研究方向与限制', mapper,
                            base=case.base, store=case.store, time_budget=120)
    assert actual_budgets
    assert all(input_tokens + output_tokens + 300 <= context_tokens
               for input_tokens, output_tokens in actual_budgets)
    assert mapper.paths() == expected
    assert mapper.markers() == {'FileMarker_%04d' % i for i in range(121)}
    assert result['package_context']['large_qa']['model_mapped_files'] == 121
    assert result['package_context']['large_qa']['map_complete'] is True
    if context_tokens == 32768:
        assert all(output_tokens > 1000 for _, output_tokens in actual_budgets)


def test_parseable_json_with_length_finish_is_not_cached_and_failed_batch_is_retried(
        corpus_project, monkeypatch):
    case = corpus_project
    expected = populate(case, count=21, topics=11, long=True)

    class ChatModel:
        model = 'full-corpus-finish-reason-regression'

        def __init__(self, length_call=None):
            self.recorder = RecordingMapper()
            self.length_call = length_call

        def chat(self, system, prompt, **kwargs):
            content = self.recorder(self, system, prompt, **kwargs)
            # Simulate a provider reporting output exhaustion despite a valid envelope.
            assert json.loads(content)['points']
            length = (isinstance(json.loads(prompt).get('files'), list)
                      and len(self.recorder.file_prompts()) == self.length_call)
            return {'content': content, 'finish_reason': 'length' if length else 'stop'}

    monkeypatch.setattr('services.conversation._model_text', real_model_text)
    first_model = ChatModel(length_call=2)
    retrieval = query(case)
    first = prepare_global(retrieval, '概括整个资料包的研究方向与限制', first_model,
                           base=case.base, store=case.store, time_budget=120)
    file_prompts = first_model.recorder.file_prompts()
    assert len(file_prompts) >= 3
    failed_paths = {item['path'] for item in file_prompts[1]['files']}
    state = first['package_context']['large_qa']
    assert state['map_failures'] == 1
    assert state['model_mapped_files'] == len(expected - failed_paths)
    assert state['map_complete'] is False
    assert state['synthesis_complete'] is False
    assert any('未完成模型归并' in warning for warning in first['warnings'])

    second_model = ChatModel()
    second = prepare_global(retrieval, '概括整个资料包的研究方向与限制', second_model,
                            base=case.base, store=case.store, time_budget=120)
    assert second_model.recorder.paths() == failed_paths
    assert second['package_context']['large_qa']['map_cache_hit'] is True
    assert second['package_context']['large_qa']['map_complete'] is True
    assert second['package_context']['large_qa']['synthesis_complete'] is True


def test_overview_prefers_late_substantive_summary_and_preserves_exact_offsets():
    from services.large_qa_synthesis import _compact_capsules
    disclaimer = 'Copyright. All rights reserved. This work is not certified. Study limitations are reserved. '
    header = disclaimer * 4
    substantive = 'Department of author affiliations. ' * 5
    substantive += 'We propose a reliable sampling method. Results demonstrate improved sensitivity under specified conditions. '
    raw = header + substantive
    points = [
        {'kind': 'summary', 'text': header, 'excerpt_start': 0, 'excerpt_end': len(header)},
        {'kind': 'summary', 'text': substantive, 'excerpt_start': len(header), 'excerpt_end': len(raw)},
    ]
    result = _compact_capsules([{'path': 'study.pdf', 'points': points}], '整个数据包讲了哪些内容')[0]
    assert len(result['points']) == 1, 'a copyright disclaimer is not a research counterexample'
    point = result['points'][0]
    assert 'We propose a reliable sampling method' in point['text']
    assert 'Results demonstrate' in point['text']
    assert len(point['text']) == 200
    assert point['text'] == raw[point['excerpt_start']:point['excerpt_end']]


def test_overview_source_evidence_expands_capsule_with_real_chunk_context(corpus_project, monkeypatch):
    from services.large_qa_index import plain_text
    case = corpus_project
    body = 'StudyOrigin reports a finding about sampling. ' + 'Operational background is recorded. ' * 12
    body += 'ContextBeyondCapsuleMarker describes validation and its applicable conditions. '
    body += 'The accompanying protocol retains independent checks. ' * 20
    case.add('study.txt', body)
    case.add('other.txt', 'OtherStudy reports an independent finding.')
    case.sync()
    def mapper(_model, _system, prompt, **_kwargs):
        point = next(item for item in RecordingMapper.entries(json.loads(prompt)) if 'StudyOrigin' in item['text'])
        return json.dumps({'points': [{'text': point['text'][:80], 'sources': [point['id']]}]})
    monkeypatch.setattr('services.conversation._model_text', mapper)
    result = prepare_global(query(case), '整个数据包讲了哪些内容', mapper, base=case.base, store=case.store)
    source = next(item for item in result['results'] if item.get('large_synthesis_source')
                  and item['source_path'] == 'study.txt')
    assert 'ContextBeyondCapsuleMarker' in source['original_text']
    assert 200 < len(source['original_text']) <= 1400
    chunk_id = int(source['evidence_id'].split(':')[1])
    with case.index.connect() as connection:
        chunk = connection.execute('SELECT text,version FROM chunks WHERE id=?', (chunk_id,)).fetchone()
    spans = source['location']['selected_chunk_spans']
    assert source['original_text'] == plain_text('\n…\n'.join(chunk['text'][span['start']:span['end']] for span in spans))
    assert source['source_hash'] == chunk['version']
    for anchor in source['location']['synthesis_anchor_spans']:
        assert any(span['start'] <= anchor['start'] <= anchor['end'] <= span['end'] for span in spans)


def test_all_note_sources_survive_beyond_seventy_results_and_repair_reuse(corpus_project, monkeypatch):
    from services import answer_quality as aq
    from services.large_qa_synthesis import reusable_synthesis
    case = corpus_project
    for number in range(85):
        case.add(f'group-{number % 6}/source-{number}.txt', 'A finding from a persisted independent research source.',
                 topic=f'topic-{number}')
    case.sync()
    monkeypatch.setattr(aq, 'count_tokens', lambda text: len(text) * 2)

    def map_every_source(_model, _system, prompt, **_kwargs):
        data = json.loads(prompt)
        entries = list(RecordingMapper.entries(data))
        assert len(entries) <= 12
        return json.dumps({'points': [{'text': 'finding', 'sources': [item['id']]} for item in entries]})

    monkeypatch.setattr('services.conversation._model_text', map_every_source)
    question = '整个数据包讲了哪些内容'
    retrieval = query(case, question)
    prepared = prepare_global(retrieval, question, object(), base=case.base, store=case.store)
    expected = {row['path'] for row in case.rows}
    sources = [item for item in prepared['results'] if item.get('large_synthesis_source')]
    assert {item['source_path'] for item in sources} == expected
    assert len(sources) == 85
    assert len(prepared['results']) >= 85
    assert set(prepared['related_file_paths']) == expected
    reused = reusable_synthesis(prepared, retrieval, question, base=case.base)
    assert reused is not None
    assert {item['source_path'] for item in reused['results'] if item.get('large_synthesis_source')} == expected


def test_new_finding_in_first_batch_preserves_later_cache_and_correct_sources(corpus_project, monkeypatch):
    case = corpus_project
    for number in range(6):
        case.add(f'study-{number}.txt', f'StudyMarker_{number} reports a sampling method and its results.')
    case.sync()
    # Keep batch membership fixed so this exercises source-ID renumbering alone.
    monkeypatch.setattr('services.large_qa_synthesis._batches',
                        lambda items, _budget: [items[start:start + 3] for start in range(0, len(items), 3)])
    prompts = []

    def mapper(_model, _system, prompt, **_kwargs):
        data = json.loads(prompt)
        prompts.append(data)
        point = list(RecordingMapper.entries(data))[-1]
        return json.dumps({'points': [{'text': point['text'], 'sources': [point['id']]}]})

    monkeypatch.setattr('services.conversation._model_text', mapper)
    question = '整个数据包讲了哪些内容'
    first = prepare_global(query(case, question), question, mapper, base=case.base, store=case.store)
    assert len(prompts) == 2
    assert [point['id'] for point in RecordingMapper.entries(prompts[1])] == ['S1', 'S2', 'S3']
    assert first['package_context']['large_qa']['map_cache_hits'] == 0
    first_finding_count = len(list(RecordingMapper.entries(prompts[0])))

    case.original('study-0.txt', 'A separate validation result however requires independent calibration.')
    prompts.clear()
    second = prepare_global(query(case, question), question, mapper, base=case.base, store=case.store)
    state = second['package_context']['large_qa']
    assert len(prompts) == 1
    assert len(list(RecordingMapper.entries(prompts[0]))) == first_finding_count + 1
    assert state['map_cache_hits'] == 1 and state['model_mapped_files'] == 6
    assert state['synthesis_complete'] is True
    note = next(note for note in state['notes_provenance'] if 'StudyMarker_5' in note['text'])
    assert note['sources'] == ['study-5.txt'], 'cached local S3 must return the new global S7'
    assert any(row['source_path'] == 'study-5.txt' and row.get('large_synthesis_source')
               and 'StudyMarker_5' in row['text'] for row in second['results'])


def test_exact_legacy_map_cache_migrates_and_remaps_global_source_ids(corpus_project):
    import time
    from services.large_qa_index import digest
    from services.large_qa_synthesis import _cache_write, _map_batch
    case = corpus_project
    basis = [5, 'same question', case.package['id'], {}, 'same-model', 'overview-v2']
    batch = [{'path': 'source.txt', 'topic': 'research', 'findings': [
        {'id': 'S41', 'text': 'A grounded source result.', 'kind': 'summary'}]}]
    lookup = {'S41': {'text': 'A grounded source result.', 'version': 'same-version', 'topic': 'research'}}
    legacy_key = digest([basis, 'map', batch, [('S41', 'same-version')]])
    _cache_write(case.index, legacy_key, {'points': [
        {'text': 'A grounded source result.', 'sources': ['S41'], 'topic': 'research'}]})
    points, success, hit = _map_batch(case.index, basis, batch, lookup, 'same question',
                                       None, time.monotonic() + 10, overview=True)
    assert success and hit and points[0]['sources'] == ['S41']
    shifted = [{**batch[0], 'findings': [{**batch[0]['findings'][0], 'id': 'S42'}]}]
    points, success, hit = _map_batch(case.index, basis, shifted, {'S42': lookup['S41']},
                                       'same question', None, time.monotonic() + 10, overview=True)
    assert success and hit and points[0]['sources'] == ['S42']
