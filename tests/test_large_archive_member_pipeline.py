import io
import json
import os
from pathlib import Path
import tarfile
import zipfile

import pytest

from config import Config
from services.large_package_isolated import LargePackageStore, iter_inventory
from services.large_archive_members import member_node, member_snapshot, source_identity
from services.large_package_preview import preview_file
from services.large_package_catalog_import import create_scan_from_large_selection
from services.storage import Storage

BODY = '【虚构验收样本】极光采购订单要求交付10台泵，实收8台，尚缺2台。尾款须全部交付并书面验收后支付。'


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'source'; root.mkdir()
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (root,))
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', tmp_path / 'scratch')
    monkeypatch.setenv('LARGE_PACKAGE_MODEL_SUMMARY', '0')
    return root


def archive(root, suffix='.zip', names=None):
    path = root / ('资料' + suffix)
    names = names or [('采购/收货.txt', BODY.encode()), ('采购/备注.txt', b'healthy sibling')]
    if suffix == '.zip':
        with zipfile.ZipFile(path, 'w') as out:
            for name, body in names:
                out.writestr(name, body)
    else:
        mode = {'.tar': 'w', '.tar.gz': 'w:gz', '.tar.bz2': 'w:bz2', '.tar.xz': 'w:xz'}[suffix]
        with tarfile.open(path, mode) as out:
            for name, body in names:
                info = tarfile.TarInfo(name); info.size = len(body); out.addfile(info, io.BytesIO(body))
    return path


def inventoried(root, suffix='.zip'):
    path = archive(root, suffix)
    store = LargePackageStore(root.parent / 'large.db')
    package = store.create(str(root), owner_id='member-owner')
    rows = list(iter_inventory(root))
    store.upsert_files(package['id'], rows)
    store.update_package(package['id'], status='waiting_for_selection', phase='catalog_ready')
    selected = path.name + '::采购/收货.txt'
    return store, store.get(package['id']), store.get_file(package['id'], selected)


@pytest.mark.parametrize('suffix', ['.zip', '.tar', '.tar.gz', '.tar.bz2', '.tar.xz'])
def test_inventory_identity_preview_and_snapshot(project, suffix):
    store, package, row = inventoried(project, suffix)
    assert row is not None and row['source_kind'] == 'archive_member'
    node = member_node(row)
    assert node['logical_unit'] and node['container_inode'] > 0
    selection = store.export_selection(package['id'], paths=[row['path']])
    assert selection['inventory'][row['path']]['source_metadata'] == row['source_metadata']
    with member_snapshot(project, row) as path:
        assert path.read_text() == BODY
    summary, _ = preview_file(project, row)
    assert '实收8台' in summary['sample']
    assert summary['source_path'] == row['path']
    assert summary['coverage']['archive_member'] and not summary['coverage']['complete']
    assert not list(Config.PARSE_TEMP_DIR.glob('sjfx-*'))


def test_bad_archive_does_not_publish_partial_members(project):
    archive(project, names=[('a.txt', b'a'), ('a.txt', b'b')])
    (project / 'healthy.txt').write_text('healthy')
    errors = []
    rows = list(iter_inventory(project, on_error=lambda p, e: errors.append((p, e))))
    assert {r['path'] for r in rows} == {'资料.zip', 'healthy.txt'}
    assert errors and '重复' in errors[0][1]
    assert not list(Config.PARSE_TEMP_DIR.glob('sjfx-*'))


def test_unsafe_member_not_extracted_or_published(project):
    archive(project, names=[('../outside.txt', b'bad'), ('safe.txt', b'ok')])
    errors = []
    rows = list(iter_inventory(project, on_error=lambda p, e: errors.append((p, e))))
    assert errors and not (project.parent / 'outside.txt').exists()
    assert {r['path'] for r in rows} == {'资料.zip', '资料.zip::safe.txt'}


def test_duplicate_special_member_also_prevents_publication(project):
    import stat
    path = project / 'ambiguous.zip'
    with zipfile.ZipFile(path, 'w') as out:
        entry = zipfile.ZipInfo('same.txt')
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        out.writestr(entry, b'target')
        out.writestr('same.txt', b'regular')
    errors = []
    rows = list(iter_inventory(project, on_error=lambda p, e: errors.append(e)))
    assert [r['path'] for r in rows] == ['ambiguous.zip']
    assert any('重复' in e for e in errors)


def test_member_metadata_tampering_rejected(project):
    store, package, row = inventoried(project)
    bad = dict(row, source_metadata=dict(row['source_metadata'], member_name='different.txt'))
    with pytest.raises(ValueError, match='路径不一致'):
        store.upsert_file(package['id'], bad)
    assert store.get_file(package['id'], row['path'])['source_metadata'] == row['source_metadata']


def test_unusable_scratch_does_not_open_container(project, monkeypatch):
    from services.large_archive_members import discover_members
    import services.package_analysis as analysis
    archive(project)
    blocked = project.parent / 'not-a-directory'
    blocked.write_text('occupied')
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', blocked)
    def must_not_open(*args):
        raise AssertionError('source handle opened before scratch was available')
    monkeypatch.setattr(analysis, '_open_inventory_source', must_not_open)
    with pytest.raises(FileExistsError):
        list(discover_members(project, {'path': '资料.zip'}))


def test_reinventory_clears_old_evidence_and_rejects_old_lease(project):
    store, package, row = inventoried(project)
    store.update_file(package['id'], row['path'], local_summary={'sample': 'stale evidence'},
                      deep_summary={'summary': 'stale conclusion'}, sha256='old-hash')
    leases = store.claim('quick', limit=10, package_id=package['id'])
    token = next(token for _, path, token in leases if path == row['path'])
    with store._connect() as conn:
        conn.execute('INSERT INTO portrait_facets VALUES(?,?,?,?)',
                     (package['id'], row['path'], 'project', 'stale project'))
    archive(project, names=[('采购/收货.txt', b'changed receipt'), ('new.txt', b'new')])
    retired = store.invalidate_changed_archive_members(package['id'])
    assert retired == 2
    old = store.get_file(package['id'], row['path'])
    assert old['source_changed'] and old['local_summary'] == {} and old['deep_summary'] == {}
    store.upsert_files(package['id'], list(iter_inventory(project)))
    new = store.get_file(package['id'], row['path'])
    assert not new['source_changed'] and new['quick_status'] == 'queued' and new['sha256'] is None
    assert new['deep_status'] == 'idle' and new['local_summary'] == {}
    removed = store.get_file(package['id'], '资料.zip::采购/备注.txt')
    assert removed['source_changed'] and removed['quick_status'] == 'failed'
    accepted, reason = store.commit_stage_result(package['id'], row['path'], 'quick', token,
                                                file_fields={'local_summary': {'sample': 'late result'}})
    assert not accepted and reason in {'not_running', 'stale_lease'}
    assert store.get_file(package['id'], row['path'])['local_summary'] == {}
    with store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM portrait_facets WHERE package_id=? AND path=?',
                            (package['id'], row['path'])).fetchone()[0] == 0


def test_unchanged_reinventory_preserves_completed_results(project):
    store, package, row = inventoried(project)
    store.update_file(package['id'], row['path'], local_summary={'sample': BODY}, quick_status='completed')
    assert store.invalidate_changed_archive_members(package['id']) == 0
    store.upsert_files(package['id'], list(iter_inventory(project)))
    new = store.get_file(package['id'], row['path'])
    assert new['local_summary']['sample'] == BODY and new['quick_status'] == 'completed'


def test_member_source_fields_invalidate_qa_tracking(project):
    from services import large_qa_changes as changes
    store, package, row = inventoried(project)
    with store._connect() as conn:
        conn.execute('BEGIN')
        before = changes.read_stamp(conn, package['id'])
        assert before is not None
        conn.execute("UPDATE files SET source_metadata='{}' WHERE package_id=? AND path=?",
                     (package['id'], row['path']))
        assert changes.read_stamp(conn, package['id']) != before


def test_member_download_cleanup_on_interrupted_response(project, monkeypatch):
    import asyncio
    from urllib.parse import urlencode
    import app as web
    store, package, row = inventoried(project)
    monkeypatch.setattr(web, 'large_package_store', store)
    monkeypatch.setattr(web, '_large_package_owned', lambda package_id: package)
    with web.app.test_request_context('/file?' + urlencode({'path': row['path']})):
        response = web.large_package_file(package['id'])
        async def receive():
            return {'type': 'http.request', 'body': b''}
        async def send(message):
            raise OSError('client disconnected')
        with pytest.raises(OSError, match='disconnected'):
            asyncio.run(response({'type': 'http', 'method': 'GET', 'headers': [], 'extensions': {}}, receive, send))
    assert not list(Config.PARSE_TEMP_DIR.glob('sjfx-*'))


def test_container_substitution_rejected_even_with_same_size_and_time(project):
    store, package, row = inventoried(project)
    path = project / row['source_metadata']['container_path']
    original = path.stat(); replacement = project / 'replacement.zip'
    replacement.write_bytes(path.read_bytes()); os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(path)
    with pytest.raises(Exception, match='对象|身份'):
        source_identity(project, row)


def test_prefix_snapshot_is_bounded_and_full_snapshot_unchanged(project):
    store, package, row = inventoried(project)
    with member_snapshot(project, row, prefix_bytes=13) as path:
        assert path.read_bytes() == BODY.encode()[:13]
    with member_snapshot(project, row) as path:
        assert path.read_text() == BODY
    assert not list(Config.PARSE_TEMP_DIR.glob('sjfx-*'))


def test_bridge_persists_logical_identity_and_real_parser(project, monkeypatch):
    import app as web
    from services.unified_parser import UnifiedDocumentParser
    large, package, row = inventoried(project)
    ordinary = Storage(project.parent / 'ordinary.db')
    selection = large.export_selection(package['id'], paths=[row['path']])
    result = create_scan_from_large_selection(ordinary, package, selection, '成员交接', 'fast',
        'member-owner', 'member-request', large_store=large)
    saved = list(ordinary.iter_inventory_entries(result['scan_id'], kind='file'))
    assert len(saved) == 1 and saved[0]['payload']['logical_unit']
    assert saved[0]['payload']['member_name'] == '采购/收货.txt'
    monkeypatch.setattr(web, 'storage', ordinary)
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'member-owner')
    monkeypatch.setattr(web.Config, 'ENABLE_TRANSLATION', False)
    monkeypatch.setattr(web.Config, 'ENABLE_IMPORT_TRANSLATION', False)
    monkeypatch.setattr(web, 'parser', UnifiedDocumentParser(max_chars=10000))
    monkeypatch.setattr(web, 'llm_generation_enabled', False)
    monkeypatch.setattr(web, '_package_embedding_client', None)
    monkeypatch.setattr(web, '_package_large_options', lambda: {'threshold_bytes': 10**12})
    monkeypatch.setattr(web, '_publish_analysis_progress', lambda *a, **k: None)
    monkeypatch.setattr(web, '_write_local_overview', lambda *a, **k: None)
    job = ordinary.claim_next_job('archive-acceptance')
    web._run_claimed_analysis_job(job)
    document = ordinary.get_document(result['scan_id'], row['path'])
    assert document and '实收8台' in document['text']
    assert document['source']['archive_member'] == '采购/收货.txt'
    assert document.get('evidence')
    queued = [j for j in ordinary.list_jobs(owner_id='member-owner') if j['task_type'] == 'generate_summary']
    assert len(queued) == 1 and queued[0]['options']['path'] == row['path']
    again = create_scan_from_large_selection(ordinary, package, selection, '成员交接', 'fast',
        'member-owner', 'member-request', large_store=large)
    assert again['scan_id'] == result['scan_id'] and again['reused']


def test_qa_index_retains_identity_and_reads_member_in_child(project):
    from services.large_qa_index import LargeQAIndex
    from services.large_qa_extract import extract_document
    store, package, row = inventoried(project)
    index = LargeQAIndex(package['id'], base=project.parent / 'qa', create=True)
    index.sync_store(store)
    with index.connect() as conn:
        doc = dict(conn.execute('SELECT * FROM documents WHERE path=?', (row['path'],)).fetchone())
    assert doc['source_kind'] == 'archive_member'
    assert json.loads(doc['source_metadata']) == row['source_metadata']
    records, complete, note = extract_document(project, doc, timeout=30)
    assert any('实收8台' in r['text'] for r in records)
    assert index.put_original(doc['path'], doc['source_version'], records, complete, note) is not False
    # An unchanged metadata version is not a valid reason to retain original
    # evidence after the container disappeared during a resumed inventory.
    (project / row['source_metadata']['container_path']).unlink()
    assert store.invalidate_changed_archive_members(package['id']) == 2
    index.sync_store(store)
    with index.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM documents WHERE path=?', (row['path'],)).fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM chunks WHERE path=?', (row['path'],)).fetchone()[0] == 0


def test_deep_parse_uses_member_not_container(project):
    from large_package_worker import LargePackageWorker
    store, package, row = inventoried(project)
    worker = object.__new__(LargePackageWorker)
    summary = worker._deep_parse(project, row)
    assert summary['summary_type'] == 'local_deep'
    assert '实收8台' in summary['text_preview'] and summary['evidence']
    assert summary['source_path'] == row['path']


def test_preview_runner_transports_source_identity(project):
    from services.large_package_preview_runner import PreviewRunner
    store, package, row = inventoried(project)
    runner = PreviewRunner()
    try:
        summary, _ = runner.run(project, row)
        assert '实收8台' in summary['sample'] and summary['coverage']['archive_member']
    finally:
        runner.close()


def test_asgi_member_download_and_cleanup(project, monkeypatch):
    import asyncio
    from urllib.parse import urlencode
    import app as web
    store, package, row = inventoried(project)
    monkeypatch.setattr(web, 'large_package_store', store)
    monkeypatch.setattr(web, '_large_package_owned', lambda package_id: package)
    with web.app.test_request_context('/api/large-packages/' + package['id'] + '/file?' +
                                     urlencode({'path': row['path']})):
        response = web.large_package_file(package['id'])
        assert not isinstance(response, tuple), (response[0].get_json(), response[1]) if isinstance(response, tuple) else None
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b''}
        async def send(message):
            messages.append(message)
        asyncio.run(response({'type': 'http', 'method': 'GET', 'headers': [], 'extensions': {}}, receive, send))
        body = b''.join(m.get('body', b'') for m in messages)
    assert body == BODY.encode()
    assert 'attachment' in response.headers['Content-Disposition']
    assert not list(Config.PARSE_TEMP_DIR.glob('sjfx-*'))
