"""Real-file handoff acceptance: actual inventory/parser, no model inference."""
from pathlib import Path
from contextlib import closing
import os
import sqlite3
import zipfile

import pytest

from config import Config
from services.large_package_isolated import LargePackageStore, iter_inventory
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser
from services import large_package_catalog_import as bridge


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    root.mkdir()
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (root,))
    large = LargePackageStore(tmp_path / 'large.db')
    ordinary = Storage(tmp_path / 'ordinary.db')
    def create(path, content='The pump inspection found a cracked seal. Replace the seal before restart.'):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.endswith('.zip'):
            with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as archive:
                archive.writestr('reports/inspection.txt', content)
        else:
            target.write_text(content, encoding='utf-8')
        package = large.create(str(root), owner_id='real-owner')
        large.upsert_files(package['id'], list(iter_inventory(root)))
        large.update_package(package['id'], status='waiting_for_selection', phase='catalog_ready')
        package = large.get(package['id'])
        selected = large.export_selection(package['id'], paths=[path])
        result = ordinary.create_scan_from_large_selection(package, selected, name='真实交接验收',
            parse_mode='fast', owner_id='real-owner', request_key='real-handoff', large_store=large)
        return root, large, ordinary, result
    return create


def leaves(node):
    if node.get('kind') == 'file':
        return [node['path']]
    return [path for child in node.get('children', []) for path in leaves(child)]


@pytest.mark.parametrize('path', ['one.txt', 'region/team/reports/one.txt', 'reports.zip/one.txt'])
def test_real_physical_file_is_visible_after_handoff(handoff, path):
    _, _, store, result = handoff(path)
    scan = store.get_scan(result['scan_id'])
    assert leaves(scan['tree']) == [path]
    assert result['selected_file_count'] == 1
    parent = Path(path).parent
    expected = set()
    while str(parent) != '.':
        expected.add(parent.as_posix())
        parent = parent.parent
    actual = {row['path'] for row in store.iter_inventory_entries(result['scan_id'], kind='directory')} - {'.'}
    assert actual == expected
    assert scan['directory_count'] == len(expected) + 1  # Existing bridge includes root.


@pytest.mark.parametrize('path', ['report.txt', 'package.zip', 'region/team/report.txt', 'reports.zip/report.txt'])
def test_real_handoff_parser_and_preliminary_queue(handoff, monkeypatch, path):
    import app as web
    root, _, store, result = handoff(path)
    monkeypatch.setattr(web, 'storage', store)
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'real-owner')
    monkeypatch.setattr(web.Config, 'ENABLE_TRANSLATION', False)
    monkeypatch.setattr(web.Config, 'ENABLE_IMPORT_TRANSLATION', False)
    monkeypatch.setattr(web.Config, 'ENABLE_PARSE_PROCESS_ISOLATION', False)
    monkeypatch.setattr(web.Config, 'PARSE_TEMP_DIR', root.parent / 'parse-temp')
    monkeypatch.setattr(web, 'parser', UnifiedDocumentParser(max_chars=10000))
    monkeypatch.setattr(web, 'llm_generation_enabled', False)
    monkeypatch.setattr(web, '_package_embedding_client', None)
    monkeypatch.setattr(web, '_package_large_options', lambda: {'threshold_bytes': 10**12})
    monkeypatch.setattr(web, '_publish_analysis_progress', lambda *a, **k: None)
    overviews = []
    monkeypatch.setattr(web, '_write_local_overview', lambda *a, **k: overviews.append(k['summary_stage']))
    job = store.claim_next_job('real-file-acceptance')
    assert job['id'] == result['job_id']
    web._run_claimed_analysis_job(job)
    document = store.get_document(result['scan_id'], path)
    assert document and 'cracked seal' in document['text']
    assert document.get('evidence')
    assert overviews == ['parsed']
    summaries = [j for j in store.list_jobs(owner_id='real-owner') if j['task_type'] == 'generate_summary']
    assert len(summaries) == 1
    assert summaries[0]['options']['path'] == path
    assert summaries[0]['options']['workflow_source'] == 'preliminary_model_summary'
    assert summaries[0]['options']['analysis_level'] == 'preview'


def test_physical_origin_survives_database_and_export(handoff):
    root, large, store, result = handoff('normal.txt')
    assert list(iter_inventory(root))[0]['source_kind'] == 'physical_file'
    scan = store.get_scan(result['scan_id'])
    package_id = scan['source_large_package_id']
    assert large.get_file(package_id, 'normal.txt')['source_kind'] == 'physical_file'
    assert large.export_selection(package_id, paths=['normal.txt'])['inventory']['normal.txt']['source_kind'] == 'physical_file'


def test_caller_cannot_forge_physical_origin():
    selection = {'inventory': {'pack.zip/member.txt': {
        'path': 'pack.zip/member.txt', 'source_kind': 'physical_file', 'size': 1}}}
    records, _, excluded = bridge._inventory_records(selection, {'id': 'p'})
    assert not records and excluded['archive_member'] == 1


def test_explicit_archive_origin_is_never_treated_as_physical():
    selection = {'inventory': {'member.txt': {'path': 'member.txt', 'size': 1}}}
    records, _, excluded = bridge._inventory_records(selection, {'id': 'p'},
        fingerprints={'member.txt': {'source_kind': 'archive_member'}})
    assert not records and excluded['archive_member'] == 1


@pytest.mark.parametrize('path', ['pack.zip::one.txt', 'path/archive.tar.gz::sub/report.txt'])
def test_logical_member_syntax_is_detected(path):
    assert bridge.looks_like_archive_member(path)


def test_source_metadata_reader_remains_compatible_with_old_schema(tmp_path):
    db = tmp_path / 'old.db'
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('CREATE TABLE files(package_id TEXT,path TEXT,sha256 TEXT,size INTEGER,modified_at_ns INTEGER,sensitive INTEGER)')
        conn.execute("INSERT INTO files VALUES('p','old.txt','digest',5,7,0)")
        conn.commit()
    class OldStore:
        def _connect(self):
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            return conn
    marks = bridge.load_file_fingerprints(OldStore(), 'p', ['old.txt'], strict=True)
    assert marks['old.txt']['sha256'] == 'digest'
    assert marks['old.txt'].get('source_kind', '') == ''


def test_additive_source_migration_preserves_historical_data(tmp_path):
    db = tmp_path / 'legacy.db'
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE files(package_id TEXT,path TEXT,name TEXT,extension TEXT,size INTEGER,modified_at_ns INTEGER,sha256 TEXT,sensitive INTEGER,file_type TEXT,quick_status TEXT,deep_status TEXT,category TEXT,confidence REAL,local_summary TEXT,deep_summary TEXT,error TEXT,updated_at REAL,PRIMARY KEY(package_id,path))")
        conn.execute("INSERT INTO files VALUES('p','old.txt','old.txt','.txt',1,1,'oldhash',0,'text','completed','completed','text',0.5,'{}','{\"summary\":\"keep\"}',NULL,1)")
        conn.commit()
    store = LargePackageStore(db)
    row = store.get_file('p', 'old.txt')
    assert row['source_kind'] == ''
    assert row['sha256'] == 'oldhash' and row['deep_summary']['summary'] == 'keep'
    reopened = LargePackageStore(db)
    assert reopened.get_file('p', 'old.txt')['deep_summary'] == row['deep_summary']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink replacement acceptance')
def test_physical_marker_does_not_bypass_secure_source_open(handoff, monkeypatch):
    from services.package_analysis import _secure_source_snapshot
    root, _, store, result = handoff('reports.zip/one.txt')
    node = store.get_inventory_entry(result['scan_id'], 'reports.zip/one.txt')
    outside = root.parent / 'outside'
    outside.mkdir()
    (outside / 'one.txt').write_text('must not be read', encoding='utf-8')
    (root / 'reports.zip').rename(root / 'original-folder')
    (root / 'reports.zip').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(Config, 'PARSE_TEMP_DIR', root.parent / 'scratch')
    with pytest.raises((OSError, ValueError)):
        with _secure_source_snapshot(root, node):
            pytest.fail('A registered physical origin must not bypass link checks')


def test_shared_ancestors_are_materialized_once():
    paths = ['a/b/c/one.txt', 'a/b/d/two.txt', 'a/b/c/three.txt']
    selection = {'inventory': {p: {'path': p, 'size': 1} for p in paths}}
    records, directories, _ = bridge._inventory_records(selection, {'id': 'p'})
    assert directories == ['a', 'a/b', 'a/b/c', 'a/b/d']
    assert [r['path'] for r in records] == paths
