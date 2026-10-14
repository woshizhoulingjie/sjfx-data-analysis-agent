"""Real archive inventory, extraction, FTS ranking and cached-original reading.

All sources/stores are temporary. No model endpoint is permitted in these tests.
"""
import gc
import io
import json
import os
from pathlib import Path
import tarfile
from types import SimpleNamespace
import zipfile

import numpy as np
import pytest

from config import Config
from services.conversation import ConversationScope, RetrievalRequest
from services.large_archive_members import member_snapshot
from services.large_package_isolated import LargePackageStore, iter_inventory
from services.large_qa_extract import _extract, extract_document
from services.large_qa_index import LargeQAIndex
from services.large_qa_search import original_document, retrieve


BODY = 'Synthetic aurora procurement: delivered eight pumps; final payment requires written acceptance.'


@pytest.fixture
def archive_project(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    root.mkdir()
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (root,))
    monkeypatch.setattr(Config, 'DATA_DIR', tmp_path / 'state')
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', tmp_path / 'scratch')
    monkeypatch.setenv('SJFX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('SJFX_PARSE_TEMP_DIR', str(tmp_path / 'scratch'))
    monkeypatch.setenv('LARGE_PACKAGE_MODEL_SUMMARY', '0')

    def no_model(*args, **kwargs):
        raise AssertionError('archive QA regression must not call a model')

    monkeypatch.setattr('services.large_package_semantic.embedding_client', no_model)
    monkeypatch.setattr('services.conversation._model_text', no_model)
    yield root
    # The isolated store's legacy bare Connection contexts leave cycles to GC.
    gc.collect()


def write_archive(target, contents):
    if target.suffix == '.zip':
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, content in contents.items():
                archive.writestr(name, content)
    else:
        with tarfile.open(target, 'w:gz' if target.name.endswith('.gz') else 'w') as archive:
            for name, content in contents.items():
                payload = content.encode()
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))


def indexed_archive(root, suffix='.zip', *, extract_in_child=False):
    container = root / ('procurement' + suffix)
    write_archive(container, {'orders/receipt.txt': BODY,
                              'notes/other.txt': 'Unrelated sibling metadata.',
                              '.env': 'synthetic-private-secret'})
    (root / 'ordinary.txt').write_text('Ordinary aurora delivery remains compatible.', encoding='utf8')
    store = LargePackageStore(root.parent / 'large.db')
    package = store.create(root, owner_id='archive-owner')
    rows = list(iter_inventory(root))
    store.upsert_files(package['id'], rows)
    target_path = container.name + '::orders/receipt.txt'
    store.update_file(package['id'], target_path,
                      local_summary={'summary': 'Synthetic aurora procurement summary.'})
    index = LargeQAIndex(package['id'], root.parent / 'qa', create=True)
    index.sync_store(store)
    for path in (target_path, 'ordinary.txt'):
        with index.connect() as connection:
            doc = dict(connection.execute('SELECT * FROM documents WHERE path=?', (path,)).fetchone())
        if extract_in_child:
            records, complete, note = extract_document(root, doc, timeout=30)
        elif doc.get('source_kind') == 'archive_member':
            with member_snapshot(root, doc) as snapshot:
                records, complete, note = _extract(snapshot, path, 100000)
        else:
            records, complete, note = _extract(root / path, path, 100000)
        assert records and complete
        assert index.put_original(path, doc['source_version'], records, complete, note)
    return SimpleNamespace(root=root, store=store, package=package, index=index,
                           base=root.parent / 'qa', path=target_path, container=container)


def query(case, *, scope=None, intent='lookup', text='aurora acceptance'):
    return retrieve(case.store, RetrievalRequest(case.package['id'], text,
                    scope or ConversationScope(), top_k=20, intent=intent),
                    base=case.base, query_vector=np.array([1., 0., 0.]))


def read(case, path=None, full=True, *, current_source=None):
    return original_document(case.package['id'], case.root, path or case.path,
                             full=full, base=case.base, current_source=current_source)


@pytest.mark.parametrize('suffix', ['.zip', '.tar', '.tar.gz'])
def test_real_member_index_retrieve_and_open_original(archive_project, suffix):
    case = indexed_archive(archive_project, suffix, extract_in_child=True)
    result = query(case, scope=ConversationScope('files', source_paths=(case.path,)))
    passages = [row for row in result['results'] if row['evidence_level'] == 'passage']
    assert passages and all(row['source_path'] == case.path for row in passages)
    assert any('eight pumps' in row['original_text'] for row in passages)
    assert result['candidate_paths'] == [case.path]
    document = read(case)
    assert document and document['text_content'] == BODY
    assert document['coverage']['parse_complete']
    assert not document['coverage']['semantic_complete']
    assert document['source']['path'] == case.path
    assert document['source']['physical_path'] == case.container.name
    assert document['source']['archive_member'] == 'orders/receipt.txt'
    assert document['source_reference']['logical_path'] == case.path
    assert document['source_reference']['physical_path'] == case.container.name
    assert passages[0]['location']['archive_member'] == 'orders/receipt.txt'
    assert read(case, current_source=case.store.get_file(case.package['id'], case.path)) == document
    assert not list(Config.PARSE_TEMP_DIR.glob('sjfx-*'))


@pytest.mark.parametrize('change', ['delete', 'replace_same_size_and_mtime'])
def test_container_change_rejects_unsynced_old_summary_and_original(archive_project, change):
    case = indexed_archive(archive_project)
    before = case.container.stat()
    if change == 'delete':
        case.container.unlink()
    else:
        replacement = case.root / 'replacement.zip'
        write_archive(replacement, {'orders/receipt.txt': BODY.replace('eight', 'three'),
                                  'notes/other.txt': 'Unrelated sibling metadata.',
                                  '.env': 'synthetic-private-secret'})
        assert replacement.stat().st_size == before.st_size
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, case.container)
        assert case.container.stat().st_ino != before.st_ino
    result = query(case, scope=ConversationScope('files', source_paths=(case.path,)))
    assert result['results'] == []
    assert result['candidate_paths'] == []
    assert read(case) is None


def test_scope_and_current_membership_restrict_member_search(archive_project):
    case = indexed_archive(archive_project)
    ordinary_scope = ConversationScope('files', source_paths=('ordinary.txt',))
    assert all(row['source_path'] == 'ordinary.txt' for row in query(case, scope=ordinary_scope)['results'])
    with case.store._connect() as connection:
        connection.execute('DELETE FROM files WHERE package_id=? AND path=?',
                           (case.package['id'], case.path))
    assert not query(case, scope=ConversationScope('files', source_paths=(case.path,)))['results']
    assert case.store.get(case.package['id'], owner_id='foreign-owner') is None
    assert read(case, 'not-in-the-index.txt') is None


def test_vector_only_member_hit_keeps_verified_source_identity(archive_project):
    case = indexed_archive(archive_project)
    with case.index.connect(write=True) as connection:
        connection.execute('UPDATE documents SET vector=? WHERE path=?',
                           (np.array([1., 0., 0.], dtype=np.float32).tobytes(), case.path))
        case.index.set_meta(connection, 'embedding_model', 'synthetic-offline-vector')
    result = query(case, text='calibration',
                   scope=ConversationScope('files', source_paths=(case.path,)))
    assert result['retrieval_stats']['keyword_candidates'] == 0
    assert result['retrieval_stats']['semantic_files'] == 1
    assert result['results'] and all(row['source_path'] == case.path for row in result['results'])
    assert any(row['location']['archive_member'] == 'orders/receipt.txt' for row in result['results'])


def test_sensitive_member_does_not_leak_from_original_or_retrieval(archive_project):
    case = indexed_archive(archive_project)
    private_path = case.container.name + '::.env'
    with case.index.connect() as connection:
        doc = dict(connection.execute('SELECT * FROM documents WHERE path=?', (private_path,)).fetchone())
    assert doc['sensitive'] and doc['parse_status'] == 'restricted'
    # Even a cached original must not override the document's content policy.
    case.index.put_original(private_path, doc['source_version'], [{'text': 'synthetic-private-secret aurora'}], True)
    assert read(case, private_path) is None
    assert not query(case, scope=ConversationScope('files', source_paths=(private_path,)))['results']
    with case.store._connect() as connection:
        connection.execute('UPDATE files SET sensitive=1 WHERE package_id=? AND path=?',
                           (case.package['id'], case.path))
    assert not query(case, scope=ConversationScope('files', source_paths=(case.path,)))['results']


def test_current_store_invalidated_member_does_not_reuse_old_index(archive_project):
    case = indexed_archive(archive_project)
    with case.store._connect() as connection:
        connection.execute('UPDATE files SET source_changed=1 WHERE package_id=? AND path=?',
                           (case.package['id'], case.path))
    result = query(case, scope=ConversationScope('files', source_paths=(case.path,)))
    assert not result['results']
    assert result['coverage']['scope_files'] == 0
    row = case.store.get_file(case.package['id'], case.path)
    assert row['source_changed']
    assert read(case, current_source=row) is None


@pytest.mark.parametrize('change', ['path', 'sensitive', 'member_identity', 'missing_row'])
def test_reader_current_manifest_guard_rejects_unauthorized_or_new_member(archive_project, change):
    case = indexed_archive(archive_project)
    row = case.store.get_file(case.package['id'], case.path)
    if change == 'path':
        row['path'] = 'foreign.zip::orders/receipt.txt'
    elif change == 'sensitive':
        row['sensitive'] = True
    elif change == 'member_identity':
        row['source_metadata']['compressed_size'] += 1
    else:
        row = {}
    assert read(case, current_source=row) is None


@pytest.mark.parametrize('field,value', [('container_path', '../outside.zip'),
                                        ('member_name', '../outside.txt'),
                                        ('member_size', -1), ('container_inode', 'forged')])
def test_malformed_index_member_identity_is_rejected(archive_project, field, value):
    case = indexed_archive(archive_project)
    with case.index.connect(write=True) as connection:
        raw = connection.execute('SELECT source_metadata FROM documents WHERE path=?', (case.path,)).fetchone()[0]
        metadata = json.loads(raw)
        metadata[field] = value
        connection.execute('UPDATE documents SET source_metadata=? WHERE path=?',
                           (json.dumps(metadata), case.path))
    assert not query(case, scope=ConversationScope('files', source_paths=(case.path,)))['results']
    assert read(case) is None


def test_ordinary_file_original_still_opens_and_changes_are_rejected(archive_project):
    case = indexed_archive(archive_project)
    assert read(case, 'ordinary.txt')['text_content'] == 'Ordinary aurora delivery remains compatible.'
    assert query(case, scope=ConversationScope('files', source_paths=('ordinary.txt',)))['results']
    (case.root / 'ordinary.txt').write_text('superseded', encoding='utf8')
    assert read(case, 'ordinary.txt') is None
    assert not query(case, scope=ConversationScope('files', source_paths=('ordinary.txt',)))['results']


def test_member_reader_preview_preserves_partial_parse_limits(archive_project):
    case = indexed_archive(archive_project)
    with case.index.connect() as connection:
        doc = dict(connection.execute('SELECT * FROM documents WHERE path=?', (case.path,)).fetchone())
    with member_snapshot(case.root, doc) as snapshot:
        records, complete, note = _extract(snapshot, case.path, 45)
    assert not complete
    assert case.index.put_original(case.path, doc['source_version'], records, complete, note)
    preview = read(case, full=False)
    assert preview and preview['text_content'] is None
    assert not preview['coverage']['complete']
    assert not preview['coverage']['parse_complete']
    assert preview['parser']['degraded']
    assert len(read(case)['text_content']) <= 45


def test_member_snapshot_keeps_existing_size_budget(archive_project, monkeypatch):
    case = indexed_archive(archive_project)
    row = case.store.get_file(case.package['id'], case.path)
    monkeypatch.setattr(Config, 'MAX_ARCHIVE_MEMBER_BYTES', row['size'] - 1)
    with pytest.raises(ValueError, match='安全上限'):
        with member_snapshot(case.root, row):
            pytest.fail('oversized member must not be materialized')
    assert not list(Config.PARSE_TEMP_DIR.glob('sjfx-*'))


def test_legacy_index_keeps_physical_file_reader_and_search(archive_project):
    case = indexed_archive(archive_project)
    with case.index.connect(write=True) as connection:
        connection.execute('ALTER TABLE documents DROP COLUMN source_kind')
        connection.execute('ALTER TABLE documents DROP COLUMN source_metadata')
    assert read(case, 'ordinary.txt')['text_content'] == 'Ordinary aurora delivery remains compatible.'
    assert query(case, scope=ConversationScope('files', source_paths=('ordinary.txt',)))['results']
    # A legacy index has no trusted member identity and cannot invent one.
    assert read(case) is None


def test_stale_member_is_not_exposed_in_global_topic_context(archive_project):
    from services.large_qa_topics import refresh_maps
    case = indexed_archive(archive_project)
    refresh_maps(case.index)
    case.container.unlink()
    result = query(case, intent='summary')
    assert all(row['source_path'] != case.path for row in result['results'])
    reports = result['package_context']['large_qa']['topics']
    assert all(case.path not in point['sources'] for report in reports for point in report['points'])


@pytest.mark.parametrize('absolute', [False, True])
def test_outside_physical_path_is_rejected_even_if_indexed(archive_project, absolute):
    case = indexed_archive(archive_project)
    outside = case.root.parent / 'outside.txt'
    outside.write_text('Synthetic external aurora secret.', encoding='utf8')
    stat = outside.stat()
    path = str(outside) if absolute else '../outside.txt'
    row = {'path': path, 'name': 'outside.txt', 'size': stat.st_size,
           'modified_at_ns': stat.st_mtime_ns, 'sensitive': False,
           'local_summary': {'summary': outside.read_text()}, 'deep_summary': {},
           'source_kind': 'physical_file'}
    case.store.upsert_files(case.package['id'], [row])
    case.index.sync_rows([row], case.package)
    with case.index.connect() as connection:
        doc = dict(connection.execute('SELECT * FROM documents WHERE path=?', (path,)).fetchone())
    case.index.put_original(path, doc['source_version'], [{'text': outside.read_text()}], True)
    assert not query(case)['results']
    assert read(case, path) is None
