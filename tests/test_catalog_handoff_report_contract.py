"""Bridge scans must satisfy ordinary report contracts without report mocks."""
import pytest
from docx import Document
from config import Config
from services.large_package_isolated import LargePackageStore, iter_inventory
from services.scanner import human_size
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser


def create_handoff(tmp_path, monkeypatch, files, selected):
    root = tmp_path / 'source'
    root.mkdir()
    monkeypatch.setattr(Config, 'SCAN_ALLOWED_ROOTS', (root,))
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    large = LargePackageStore(tmp_path / 'large.db')
    store = Storage(tmp_path / 'ordinary.db')
    package = large.create(str(root), owner_id='report-contract-owner')
    large.upsert_files(package['id'], list(iter_inventory(root)))
    large.update_package(package['id'], status='waiting_for_selection', phase='catalog_ready')
    selection = large.export_selection(package['id'], paths=selected)
    result = store.create_scan_from_large_selection(large.get(package['id']), selection,
        name='report contract', parse_mode='fast', owner_id='report-contract-owner',
        request_key='report-contract', large_store=large)
    return root, store, result


@pytest.mark.parametrize('size', [0, 1023, 1024, 2050])
def test_bridge_scan_contains_ordinary_size_presentation(tmp_path, monkeypatch, size):
    _, store, result = create_handoff(tmp_path, monkeypatch,
        {'selected.txt': b'x' * size}, ['selected.txt'])
    scan = store.get_scan(result['scan_id'])
    assert scan['total_size'] == size
    assert scan['total_size_human'] == human_size(size)


def test_size_presentation_uses_selected_scope_not_entire_package(tmp_path, monkeypatch):
    _, store, result = create_handoff(tmp_path, monkeypatch, {
        'a.txt': b'a' * 1024, 'nested/b.txt': b'b' * 1024, 'unselected.txt': b'c' * 4096
    }, ['a.txt', 'nested/b.txt'])
    scan = store.get_scan(result['scan_id'])
    assert scan['file_count'] == 2
    assert scan['total_size'] == result['selected_bytes'] == 2048
    assert scan['total_size_human'] == '2.0 KB'


def test_real_parsed_report_does_not_block_preliminary_summary_queue(tmp_path, monkeypatch):
    import app as web
    path = 'maintenance/reports.zip/inspection.txt'
    content = b'The pump inspection found a cracked seal. Replace the seal before restart.'
    _, store, result = create_handoff(tmp_path, monkeypatch, {path: content}, [path])
    monkeypatch.setattr(web, 'storage', store)
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'report-contract-owner')
    monkeypatch.setattr(web.Config, 'ENABLE_TRANSLATION', False)
    monkeypatch.setattr(web.Config, 'ENABLE_IMPORT_TRANSLATION', False)
    monkeypatch.setattr(web.Config, 'ENABLE_PARSE_PROCESS_ISOLATION', False)
    monkeypatch.setattr(web.Config, 'PARSE_TEMP_DIR', tmp_path / 'parse-temp')
    monkeypatch.setattr(web, 'parser', UnifiedDocumentParser(max_chars=10000))
    monkeypatch.setattr(web, 'llm_generation_enabled', False)
    monkeypatch.setattr(web, '_package_embedding_client', None)
    monkeypatch.setattr(web, '_package_large_options', lambda: {'threshold_bytes': 10**12})
    monkeypatch.setattr(web, '_publish_analysis_progress', lambda *a, **k: None)
    # Redirect only the output location; the report builder and DOCX writer run.
    output = tmp_path / 'overview.docx'
    monkeypatch.setattr(web, '_output_path_for_write', lambda *a, **k: output)
    job = store.claim_next_job('report-contract-test')
    assert job['id'] == result['job_id']
    web._run_claimed_analysis_job(job)
    document = store.get_document(result['scan_id'], path)
    assert document and 'cracked seal' in document['text']
    report = store.get_summary(result['scan_id'], '.', 'parsed_report')
    assert report and report['summary_stage'] == 'parsed'
    assert report['report_scope']['selected_paths'] == [path]
    assert report['report_scope']['selected_bytes'] == len(content)
    assert report['coverage']['selected_scope_bytes'] == len(content)
    assert output.is_file()
    paragraphs = ' '.join(p.text for p in Document(output).paragraphs)
    assert '资料文件 1 个；已解析 1 个' in paragraphs
    summaries = [j for j in store.list_jobs(owner_id='report-contract-owner')
                 if j['task_type'] == 'generate_summary']
    assert len(summaries) == 1
    assert summaries[0]['options']['path'] == path
    assert summaries[0]['options']['workflow_source'] == 'preliminary_model_summary'
