import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.storage import Storage
from services.selection_supplement import SupplementError, browse, preview, publish_ready, status, submit


@pytest.fixture
def store(tmp_path):
    result = Storage(tmp_path / 'supplement.db')
    result.save_scan({'root': str(tmp_path), 'tree': {'kind': 'directory', 'path': '.', 'name': 'package', 'children': []}}, 'scan', owner_id='owner')
    result.create_import_task('scan', str(tmp_path), 'owner')
    paths = ['A/old.txt', 'B/one.txt', 'B/nested/two.txt', 'BC/outside.txt', 'B/restricted.txt']
    paths += ['many/file-%03d.txt' % i for i in range(125)]
    paths += ['literal_100%/ok.txt', 'literalX100z/wrong.txt']
    with result._connect() as conn:
        conn.executemany('INSERT INTO inventory_entries(scan_id,node_path,kind,payload) VALUES (?,?,?,?)',
                         [('scan', name, 'file', json.dumps({'path': name, 'kind': 'file', 'size': 10})) for name in paths])
    result.save_scan_selection('scan', ['A/old.txt'], [], status='confirmed')
    result.update_import_task('scan', status='deep_update_available', phase='deep_update_available',
                              checkpoint={'selected_paths': ['A/old.txt'], 'selection_version': 1, 'deep_summary_node_job_ids': []})
    result.save_summary('scan', 'A/old.txt', 'preliminary_file_summary', {'analysis_stage': 'preliminary', 'summary': 'old preliminary'})
    result.save_summary('scan', 'A/old.txt', 'deep_file_summary', {'deep_analysis': True, 'analysis_level': 'deep', 'summary': 'old deep'})
    result.save_file_workflow_states('scan', [{'path': 'B/restricted.txt', 'promotion_allowed': False, 'safety_status': 'restricted'}])
    return result


def request(store, paths, key='request_00000001'):
    plan = preview(store, 'scan', 'owner', {'paths': paths})
    return dict(paths=paths, version=plan['version'], preview_token=plan['preview_token'], request_id=key)


def database_snapshot(store):
    with store._connect() as conn:
        return {name: [tuple(row) for row in conn.execute('SELECT * FROM '+name)] for name in ['scan_selections', 'import_tasks', 'analysis_jobs', 'summaries']}


def test_nested_directories_expand_all_files_and_deduplicate(store):
    payload = request(store, ['B', 'B/nested', 'B/one.txt', 'A'])
    result = submit(store, 'scan', 'owner', payload)
    assert result['new_count'] == 2
    assert store.get_job(result['job_id'])['options']['target_paths'] == ['B/nested/two.txt', 'B/one.txt']
    assert set(store.get_scan_selection('scan')['included_paths']) == {'A/old.txt', 'B/nested/two.txt', 'B/one.txt'}
    assert store.get_summary('scan', 'A/old.txt', 'deep_file_summary')['summary'] == 'old deep'
    assert store.get_summary('scan', 'A/old.txt', 'preliminary_file_summary')['summary'] == 'old preliminary'
    assert store.get_import_task('scan')['checkpoint']['directory_supplement']['auto_publish'] is True


def test_parent_scope_is_independent_of_pagination(store):
    page = browse(store, 'scan', 'owner', 'many', 0, 50)
    assert len(page['items']) == 50 and page['remaining_count'] == 125
    result = submit(store, 'scan', 'owner', request(store, ['many']))
    assert result['new_count'] == 125
    assert len(store.get_job(result['job_id'])['options']['target_paths']) == 125


def test_remaining_files_are_counted_before_pagination(store):
    files = ['many/file-%03d.txt' % i for i in range(100)]
    store.save_scan_selection('scan', ['A/old.txt'] + files, [], status='confirmed')
    page = browse(store, 'scan', 'owner', 'many', 0, 50)
    assert page['remaining_count'] == 25
    assert all(item['remaining_count'] == 0 for item in page['items'])
    assert page['next_offset'] == 50
    assert preview(store, 'scan', 'owner', {'paths': ['many']})['new_count'] == 25


def test_literal_directory_names_do_not_match_wildcards_or_siblings(store):
    page = browse(store, 'scan', 'owner', 'literal_100%')
    assert [row['path'] for row in page['items']] == ['literal_100%/ok.txt']
    assert preview(store, 'scan', 'owner', {'paths': ['B']})['new_count'] == 2


def test_duplicate_submit_returns_same_job_even_after_batch_started(store):
    payload = request(store, ['B'])
    first = submit(store, 'scan', 'owner', payload)
    second = submit(store, 'scan', 'owner', payload)
    assert second['reused'] is True and first['job_id'] == second['job_id']
    assert len(store.list_jobs(owner_id='owner')) == 1
    assert store.get_scan_selection('scan')['version'] == 2


def test_concurrent_submissions_cannot_lose_scope(store):
    one = request(store, ['B'], 'request_concurrent_one')
    two = request(store, ['many'], 'request_concurrent_two')
    def attempt(payload):
        try: return submit(store, 'scan', 'owner', payload)
        except SupplementError: return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, [one, two]))
    assert sum(result is not None for result in results) == 1
    assert len(store.list_jobs(owner_id='owner')) == 1


@pytest.mark.parametrize('mutation', ['version', 'preview', 'processing', 'active', 'owner', 'path'])
def test_invalid_submission_does_not_write_partial_scope(store, mutation):
    payload = request(store, ['B'])
    owner = 'owner'
    if mutation == 'version': payload['version'] = 99
    elif mutation == 'preview': payload['preview_token'] = 'wrong'
    elif mutation == 'processing': store.update_import_task('scan', status='parsed_overview')
    elif mutation == 'active': store.create_job('scan', {'path': 'A/old.txt'}, 'generate_summary', 'owner')
    elif mutation == 'owner': owner = 'someone-else'
    elif mutation == 'path': payload['paths'] = ['../another-package']
    before = database_snapshot(store)
    with pytest.raises(SupplementError): submit(store, 'scan', owner, payload)
    assert database_snapshot(store) == before


def test_already_selected_scope_does_not_create_job(store):
    result = submit(store, 'scan', 'owner', request(store, ['A']))
    assert result['accepted'] is False
    assert not store.list_jobs(owner_id='owner')
    assert store.get_scan_selection('scan')['version'] == 1


def finish_file_jobs(store, result):
    store.finalize_job(result['job_id'], result={})
    for path in store.get_scan_selection('scan')['included_paths']:
        if path != 'A/old.txt':
            store.save_summary('scan', path, 'deep_file_summary', {'deep_analysis': True, 'analysis_level': 'deep', 'summary': path})
    store.update_import_task('scan', status='deep_update_available', phase='deep_update_available')


def test_only_opted_in_completed_supplement_publishes_once(store):
    # A normal initial import is deliberately outside the new reconciler.
    before = database_snapshot(store)
    publish_ready(store)
    assert database_snapshot(store) == before
    result = submit(store, 'scan', 'owner', request(store, ['B']))
    publish_ready(store)
    assert len(store.list_jobs(owner_id='owner')) == 1
    finish_file_jobs(store, result)
    publish_ready(store)
    reports = [job for job in store.list_jobs(owner_id='owner') if job['task_type'] == 'generate_report']
    assert len(reports) == 1
    assert reports[0]['options']['selection_version'] == 2
    assert reports[0]['options']['workflow_source'] == 'deep_results_report'
    publish_ready(store)
    assert len([job for job in store.list_jobs(owner_id='owner') if job['task_type'] == 'generate_report']) == 1
    selected = store.get_scan_selection('scan')['included_paths']
    store.save_summary('scan', '.', 'deep_report', {'report_scope': {'selection_version': 2, 'selected_paths': selected}})
    store.finalize_job(reports[0]['id'], result={})
    store.update_import_task('scan', status='deep_update_available')
    publish_ready(store)
    assert status(store, 'scan', 'owner')['batch']['published'] is True
    assert store.get_summary('scan', 'A/old.txt', 'deep_file_summary')['summary'] == 'old deep'


def test_publish_waits_for_all_deep_files_and_does_not_resume_paused_work(store):
    result = submit(store, 'scan', 'owner', request(store, ['B']))
    store.finalize_job(result['job_id'], result={})
    store.update_import_task('scan', status='deep_update_available')
    publish_ready(store)
    assert len(store.list_jobs(owner_id='owner')) == 1
    finish_file_jobs(store, result)
    store.update_import_task('scan', status='paused')
    publish_ready(store)
    assert len(store.list_jobs(owner_id='owner')) == 1


def test_other_scans_and_users_are_unchanged(store):
    store.save_scan({'root': '/tmp/other', 'tree': {}}, 'other', owner_id='other-owner')
    store.create_import_task('other', '/tmp/other', 'other-owner')
    other_job = store.create_job('other', {}, 'analyze_package', 'other-owner')
    before_job = store.get_job(other_job)
    before_task = store.get_import_task('other')
    submit(store, 'scan', 'owner', request(store, ['B']))
    publish_ready(store)
    assert store.get_job(other_job) == before_job
    assert store.get_import_task('other') == before_task


def test_submit_transaction_rolls_back_when_queue_insert_fails(store):
    payload = request(store, ['B'])
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER test_reject_job BEFORE INSERT ON analysis_jobs BEGIN SELECT RAISE(ABORT,'test failure'); END")
    before = database_snapshot(store)
    with pytest.raises(sqlite3.IntegrityError): submit(store, 'scan', 'owner', payload)
    assert database_snapshot(store) == before


def test_web_api_is_owner_scoped_and_reuses_existing_auth_guard(store, monkeypatch):
    import app as web
    from fastapi.testclient import TestClient
    monkeypatch.setattr(web, 'storage', store)
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'owner')
    monkeypatch.setattr(web.Config, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(web.Config, 'API_ACCESS_TOKEN', 'supplement-test-only')
    monkeypatch.setattr(web.Config, 'API_TOKEN_EXPIRES_AT', None)
    client = TestClient(web.app)  # No lifespan and no live database initialization.
    url = '/api/scan/scan/directory-supplement'
    assert client.get(url).status_code == 401
    headers = {'X-SJFX-Token': 'supplement-test-only'}
    assert client.get(url+'?status_only=1', headers=headers).json()['ready'] is True
    plan = client.post(url+'/preview', json={'paths': ['B']}, headers=headers).json()
    result = client.post(url, json={'paths': ['B'], 'version': plan['version'], 'preview_token': plan['preview_token'], 'request_id': 'api_request_0001'}, headers=headers)
    assert result.status_code == 202 and result.json()['new_count'] == 2
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'other-owner')
    assert client.get(url, headers=headers).status_code == 404


def test_existing_parse_and_summary_chain_receives_only_new_files(store, monkeypatch):
    import app as web
    monkeypatch.setattr(web, 'storage', store)
    monkeypatch.setattr(web.Config, 'ENABLE_TRANSLATION', False)
    monkeypatch.setattr(web.Config, 'ENABLE_IMPORT_TRANSLATION', False)
    monkeypatch.setattr(web, '_publish_analysis_progress', lambda *args, **kwargs: None)
    seen = []
    def analyze(scan_id, scan, storage, parser, progress, **kwargs):
        seen.extend(kwargs['target_paths'])
        for path in kwargs['target_paths']:
            storage.save_document(scan_id, path, {'text': 'new document', 'source': {'path': path}, 'evidence': []})
        analysis = {'statistics': {}, 'analysis_tree': {'kind': 'analysis_root', 'node_id': 'root', 'children': []}, 'workflow': {}, 'coverage': {}}
        storage.save_analysis(scan_id, analysis)
        return analysis
    monkeypatch.setattr(web, 'analyze_package', analyze)
    monkeypatch.setattr(web, '_write_local_overview', lambda *args, **kwargs: {'report': {}})
    monkeypatch.setattr(web, '_package_large_options', lambda: {})
    payload = request(store, ['B'])
    result = submit(store, 'scan', 'owner', payload)
    job = store.claim_next_job('test-worker')
    assert job['id'] == result['job_id']
    web._run_claimed_analysis_job(job)
    store.finalize_job(job['id'], result={})
    assert seen == ['B/nested/two.txt', 'B/one.txt']
    jobs = [item for item in store.list_jobs(owner_id='owner') if item['task_type'] == 'generate_summary']
    assert {item['options']['path'] for item in jobs} == set(seen)
    assert all(item['options']['workflow_source'] == 'preliminary_model_summary' for item in jobs)
    assert store.get_import_task('scan')['checkpoint']['directory_supplement']['auto_publish'] is True
    assert set(store.get_import_task('scan')['checkpoint']['selected_paths']) == {'A/old.txt', *seen}
    assert store.get_summary('scan', 'A/old.txt', 'deep_file_summary')['summary'] == 'old deep'


def test_package_control_pause_is_respected_even_if_phase_has_not_caught_up(store):
    payload = request(store, ['B'])
    store.set_package_processing_state('scan', 'paused', 'user pause')
    assert status(store, 'scan', 'owner')['ready'] is False
    before = database_snapshot(store)
    with pytest.raises(SupplementError): submit(store, 'scan', 'owner', payload)
    assert database_snapshot(store) == before


def test_publish_does_not_claim_an_old_report_covers_new_selection(store):
    result = submit(store, 'scan', 'owner', request(store, ['B']))
    finish_file_jobs(store, result)
    publish_ready(store)
    report = next(job for job in store.list_jobs(owner_id='owner') if job['task_type'] == 'generate_report')
    store.finalize_job(report['id'], result={})
    store.save_summary('scan', '.', 'deep_report', {'report_scope': {'selection_version': 1, 'selected_paths': ['A/old.txt']}})
    store.update_import_task('scan', status='deep_update_available')
    publish_ready(store)
    assert status(store, 'scan', 'owner')['batch']['published'] is False


def test_real_text_parser_appends_documents_and_rebuilds_combined_tree(tmp_path, monkeypatch):
    from services.scanner import scan_directory
    from services.package_analysis import analyze_package
    from services.unified_parser import UnifiedDocumentParser
    from config import Config
    root = tmp_path / 'source'
    root.mkdir()
    for folder, text in [('A', '芯片安全。硬件信任根用于验证设备启动过程。'), ('B', '芯片安全测试。远程证明验证可信执行环境。'), ('C', '尚未选择的预算文件。')]:
        (root / folder).mkdir()
        (root / folder / 'doc.txt').write_text(text * 12, encoding='utf-8')
    scan = scan_directory(root)
    scan['parse_mode'] = 'fast'
    store = Storage(tmp_path / 'actual-parser.db')
    store.save_scan(scan, 'actual', owner_id='owner')
    parser = UnifiedDocumentParser(max_chars=10000)
    options = {'threshold_bytes': 10**12, 'threshold_files': 100000}
    progress_events = []
    first = analyze_package('actual', scan, store, parser,
                            progress=lambda percent, message: progress_events.append((percent, message)),
                            target_paths=['A/doc.txt'],
                            workflow_source='selected_file_parse', large_options=options, parse_mode_override='fast')
    for expected in (72, 73, 74, 75, 76):
        assert any(percent == expected for percent, _message in progress_events)
    phase_messages = [message for percent, message in progress_events
                      if percent in {72, 73, 74, 75, 76}]
    assert len(phase_messages) >= 5
    store.save_summary('actual', 'A/doc.txt', 'deep_file_summary', {'deep_analysis': True, 'analysis_level': 'deep', 'summary': 'preserve original'})
    old_doc = store.get_document('actual', 'A/doc.txt')
    second = analyze_package('actual', scan, store, parser, target_paths=['B/doc.txt'],
                             workflow_source='selected_file_parse', large_options=options, parse_mode_override='fast')
    assert {item['path'] for item in store.list_documents('actual')} == {'A/doc.txt', 'B/doc.txt'}
    assert store.get_summary('actual', 'A/doc.txt', 'deep_file_summary')['summary'] == 'preserve original'
    assert store.get_document('actual', 'A/doc.txt')['source']['sha256'] == old_doc['source']['sha256']
    paths = set()
    def visit(node):
        paths.update(node.get('member_paths') or [])
        if node.get('kind') == 'file': paths.add(node.get('path'))
        for child in node.get('children') or []: visit(child)
    visit(second['analysis_tree'])
    assert {'A/doc.txt', 'B/doc.txt'}.issubset(paths)
    assert second['analysis_tree_version'] != first['analysis_tree_version']
    assert second['workflow']['processed_paths'] == ['B/doc.txt']
