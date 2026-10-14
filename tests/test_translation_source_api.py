"""Real isolated HTTP translation sources, ownership, jobs and existing text.

No lifespan, workers or model calls run. All business stores are temporary.
"""
import gc
from contextlib import closing
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock, Mock
from fastapi.testclient import TestClient

import app as web
from services.storage import Storage
from services.large_package_isolated import LargePackageStore
from services.scanner import scan_directory
from services.file_translation import build_file_translation_service
from services.translation import ProviderResponse, TranslationProvider


class ReadableFileProvider(TranslationProvider):
    provider_id = 'offline-test:file-route'

    def translate(self, text, source_language, target_language='zh-CN', **kwargs):
        return ProviderResponse('本操作使用 compute kernel、cuda stream scheduler 和 memory allocator。')


@pytest.mark.parametrize('backend', ['vllm', 'offline_quickmt', 'offline_nllb'])
def test_explicit_file_routes_use_relaxed_service_and_save_completion(monkeypatch, backend):
    source = {'text': 'Use the compute kernel with cuda stream scheduler and memory allocator in this operation.',
              'structure': {'title': '测试文件'}}
    service = build_file_translation_service(None, None, provider=ReadableFileProvider())
    store = Mock()
    # A stale checkpoint with a different plan must not suppress continuation
    # just because its old completed count is greater than this run's count.
    store.get_translation.return_value = {'progress': {'completed_units': 100}}
    monkeypatch.setattr(web, 'storage', store)
    monkeypatch.setattr(web, '_is_large_translation_source', lambda _scan_id: False)
    monkeypatch.setattr(web, '_translation_document', lambda *_args: source)
    generic = Mock(side_effect=AssertionError('explicit file must not use strict import service'))
    monkeypatch.setattr(web, '_translate_document_serialized', generic)
    generation = Mock(wraps=service)
    offline = Mock(wraps=build_file_translation_service(None, None, provider=ReadableFileProvider()))
    lock = MagicMock()
    monkeypatch.setattr(web, 'file_translation_service', generation)
    monkeypatch.setattr(web, 'offline_file_translation_service', offline)
    monkeypatch.setattr(web, '_translation_execution_lock', lock)
    result = web._translate_one_document('fixture-scan', 'fixture.txt', 'full', translation_backend=backend)
    assert result['status'] == 'completed'
    assert result['full_translation'] is True
    assert result['slice_advanced'] is True
    assert 'target_contains_untranslated_text' in result['warnings']
    generic.assert_not_called()
    assert generation.translate_document.call_count == (1 if backend == 'vllm' else 0)
    assert offline.translate_document.call_count == (0 if backend == 'vllm' else 1)
    assert lock.__enter__.call_count == (0 if backend == 'vllm' else 1)
    assert store.save_translation.call_args.args[2]['full_translation'] is True
    saves = store.save_translation.call_args_list
    assert saves[0].args[2]['full_translation'] is False
    assert saves[-2].args[2]['full_translation'] is True  # terminal callback before final save


@pytest.mark.parametrize('contract,error,eligible', [
    ('old-strict', 'target_contains_untranslated_text', True),
    ('old-strict', 'table_structure_changed', True),
    ('old-strict', 'protected_token_mismatch', True),
    ('old-strict', 'source_copied_without_translation', False),
    ('current', 'target_contains_untranslated_text', False),
])
def test_background_candidates_retry_old_advisory_failures_once(monkeypatch, contract, error, eligible):
    service = build_file_translation_service(None, None, provider=ReadableFileProvider())
    store = Mock()
    store.get_analysis.return_value = {'policy': {'large_package': {'enabled': True}}}
    store.iter_file_previews.return_value = [{'path': 'fixture.txt',
        'payload': {'status': 'previewed', 'language': {'code': 'en'}}}]
    store.get_translation.return_value = {'source_level': 'full', 'status': 'failed',
        'contract_version': service.policy.contract_version if contract == 'current' else contract,
        'errors': [{'code': error}]}
    store.get_content_map.return_value = {}
    monkeypatch.setattr(web, 'storage', store)
    monkeypatch.setattr(web.Config, 'TRANSLATION_FILE_BACKEND', 'offline_quickmt')
    monkeypatch.setattr(web, 'offline_file_translation_service', service)
    paths, large = web._translation_candidate_paths('fixture-scan', 'deep_backfill')
    assert large is True
    assert paths == (['fixture.txt'] if eligible else [])


@pytest.fixture
def translation_api(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    root.mkdir()
    for name, text in [('english.txt', 'Synthetic report: deliveries rose by twelve percent.'),
                       ('中文.txt', '本文件已有中文内容。')]:
        (root / name).write_text(text, encoding='utf8')
    storage = Storage(tmp_path / 'ordinary.db')
    package_store = LargePackageStore(tmp_path / 'large.db')
    monkeypatch.setattr(web, 'storage', storage)
    monkeypatch.setattr(web, 'large_package_store', package_store)
    monkeypatch.setattr(web.Config, 'SCAN_ALLOWED_ROOTS', (root,))
    monkeypatch.setattr(web.Config, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(web.Config, 'API_ACCESS_TOKEN', 'synthetic-translation-test')
    monkeypatch.setattr(web.Config, 'API_TOKEN_EXPIRES_AT', None)
    monkeypatch.setattr(web.Config, 'ENABLE_TRANSLATION', True)
    monkeypatch.setattr(web.Config, 'OWNER_ID', 'legacy-default')
    monkeypatch.setattr(web._file_protection, 'guard_root', lambda _root: None)
    monkeypatch.setattr(web._multiuser_runtime, 'get_current_user', lambda: None)
    identity = [{'owner_id': 'alice', 'role': 'user'}]
    monkeypatch.setattr(web, '_multiuser_session_from_request', lambda: identity[0])
    package = package_store.create(root, owner_id='alice')
    package_store.upsert_files(package['id'], [
        {'path': name, 'name': name, 'size': (root / name).stat().st_size,
         'modified_at_ns': (root / name).stat().st_mtime_ns} for name in ['english.txt', '中文.txt']
    ])
    package_store.update_file(package['id'], 'english.txt', quick_status='completed',
                              local_summary={'sample': 'Synthetic original preview.', 'language': {'code': 'en'}, 'title': 'Foreign report'})
    package_store.update_file(package['id'], '中文.txt', quick_status='completed',
                              local_summary={'sample': '中文内容。', 'language': 'zh'})
    ordinary_id = storage.save_scan(scan_directory(root), scan_id='ordinary-alice', owner_id='alice')
    storage.save_document(ordinary_id, 'english.txt', {'text': 'ordinary source', 'source': {'path': 'english.txt'}, 'structure': {'title': 'ordinary report'}})
    for source_id in [package['id'], ordinary_id]:
        storage.save_translation(source_id, 'english.txt', {
            'status': 'completed', 'source_language': 'en', 'target_language': 'zh-CN',
            'source_level': 'full', 'full_translation': True, 'provider_id': 'vllm:fixture',
            'original_text': source_id + ' preserved original', 'translated_text': source_id + ' 已有译文',
            'original_title': 'Existing report', 'translated_title': '已有报告译名',
        })
    client = TestClient(web.app)  # Do not enter lifespan, which starts background work.
    yield SimpleNamespace(client=client, storage=storage, packages=package_store,
                          package_id=package['id'], ordinary_id=ordinary_id,
                          identity=identity, root=root)
    client.close()
    gc.collect()


def test_large_translation_list_and_filters_read_isolated_store(translation_api):
    case = translation_api
    assert not case.storage.scan_owned(case.package_id, owner_id='alice')
    response = case.client.get('/api/translations/' + case.package_id, params={'limit': 1})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page['package_id'] == case.package_id
    assert page['total'] == page['unfiltered_total'] == 2
    assert page['items'][0]['titles']['translated'] == '已有报告译名'
    assert page['has_more'] is True
    foreign = case.client.get('/api/translations/' + case.package_id,
                             params={'language': 'foreign', 'status': 'completed', 'q': '报告译名'}).json()
    assert foreign['total'] == 1 and foreign['items'][0]['path'] == 'english.txt'
    chinese = case.client.get('/api/translations/' + case.package_id, params={'language': 'zh'}).json()
    assert chinese['total'] == 1 and chinese['items'][0]['path'] == '中文.txt'


@pytest.mark.parametrize('kind', ['large', 'ordinary'])
def test_existing_bilingual_translation_remains_readable(translation_api, kind):
    case = translation_api
    source_id = case.package_id if kind == 'large' else case.ordinary_id
    response = case.client.get('/api/translation/' + source_id,
                               params={'path': 'english.txt', 'view': 'bilingual'})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page['original']['text'] == source_id + ' preserved original'
    assert page['translated']['text'] == source_id + ' 已有译文'
    assert page['full_translation'] is True


@pytest.mark.parametrize('kind', ['large', 'ordinary'])
def test_readable_translation_with_advisories_is_full_in_api(translation_api, kind):
    case = translation_api
    source_id = case.package_id if kind == 'large' else case.ordinary_id
    service = build_file_translation_service(None, None, provider=ReadableFileProvider())
    state = service.translate_document({'text': 'Use the compute kernel with cuda stream scheduler and memory allocator in this operation.',
                                        'structure': {'title': '测试文件'}})
    state.update(source_level='full', full_translation=True)
    case.storage.save_translation(source_id, 'english.txt', state)
    response = case.client.get('/api/translation/' + source_id,
                               params={'path': 'english.txt', 'view': 'bilingual'})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page['status'] == 'completed'
    assert page['full_translation'] is True
    assert page['translated']['text'] == state['translated_text']
    assert 'target_contains_untranslated_text' in page['warnings']
    assert page['errors'] == []


def test_ordinary_translation_list_is_unchanged(translation_api):
    case = translation_api
    response = case.client.get('/api/translations/' + case.ordinary_id)
    assert response.status_code == 200, response.text
    assert response.json()['items'][0]['path'] == 'english.txt'


def test_regular_user_cannot_list_read_submit_or_query_other_owner_jobs(translation_api):
    case = translation_api
    own = case.client.post('/api/translation/' + case.package_id, json={'path': 'english.txt'})
    assert own.status_code == 202, own.text
    job_id = own.json()['job_id']
    case.identity[0] = {'owner_id': 'bob', 'role': 'user'}
    for source_id in [case.package_id, case.ordinary_id]:
        listing = case.client.get('/api/translations/' + source_id)
        assert listing.status_code == 404 and '已有报告译名' not in listing.text
        reading = case.client.get('/api/translation/' + source_id, params={'path': 'english.txt'})
        assert reading.status_code in {400, 404} and 'preserved original' not in reading.text
        submit = case.client.post('/api/translation/' + source_id, json={'path': 'english.txt'})
        assert submit.status_code in {400, 404}
    assert case.client.get('/api/jobs/' + job_id).status_code == 404
    assert case.client.post('/api/jobs/' + job_id + '/cancel', json={}).status_code == 404


@pytest.mark.parametrize('source_kind', ['large', 'ordinary'])
@pytest.mark.parametrize('operation', ['document', 'package'])
def test_admin_all_users_submissions_keep_material_owner(translation_api, source_kind, operation):
    case = translation_api
    case.identity[0] = {'owner_id': 'admin-owner', 'role': 'admin'}
    source_id = case.package_id if source_kind == 'large' else case.ordinary_id
    assert case.client.get('/api/translations/' + source_id).status_code == 200
    if operation == 'document':
        response = case.client.post('/api/translation/' + source_id,
                                   json={'path': 'english.txt', 'require_full': True})
    else:
        response = case.client.post('/api/translate-package/' + source_id,
                                   json={'target_paths': ['english.txt', '中文.txt'],
                                         'phase': 'deep_backfill', 'require_full': True})
    assert response.status_code == 202, response.text
    job = case.storage.get_job(response.json()['job_id'])
    assert job['owner_id'] == 'alice'
    assert job['scan_id'] == source_id
    assert job['options']['require_full'] is True
    if operation == 'package':
        assert job['options']['target_paths'] == ['english.txt', '中文.txt']
    assert case.client.get('/api/jobs/' + job['id']).status_code == 200
    case.identity[0]['viewing_owner'] = 'bob'
    assert case.client.get('/api/jobs/' + job['id']).status_code == 404


def test_admin_self_owner_keeps_existing_all_users_semantics(translation_api):
    case = translation_api
    case.identity[0] = {'owner_id': 'admin-owner', 'role': 'admin', 'viewing_owner': 'admin-owner'}
    assert case.client.get('/api/translations/' + case.package_id).status_code == 200
    case.identity[0]['viewing_owner'] = 'bob'
    assert case.client.get('/api/translations/' + case.package_id).status_code == 404


def test_large_selected_submission_requires_explicit_manifest_members(translation_api):
    case = translation_api
    invalid = case.client.post('/api/translate-package/' + case.package_id,
                               json={'phase': 'deep_backfill', 'target_paths': ['absent.txt']})
    assert invalid.status_code == 400
    implicit_all = case.client.post('/api/translate-package/' + case.package_id, json={})
    assert implicit_all.status_code == 400


def test_translation_list_never_loads_document_or_translation_bodies(translation_api, monkeypatch):
    case = translation_api
    def forbidden(*_args, **_kwargs):
        raise AssertionError('list must not hydrate document/translation bodies')
    monkeypatch.setattr(case.storage, 'get_document', forbidden)
    monkeypatch.setattr(case.storage, 'get_translation', forbidden)
    response = case.client.get('/api/translations/' + case.package_id)
    assert response.status_code == 200, response.text
    assert len(response.json()['items']) == 2
    assert 'preserved original' not in response.text
    assert '已有译文' not in response.text


def test_translation_page_connections_do_not_grow_per_file(translation_api, monkeypatch):
    case = translation_api
    from services.large_translation_catalog import list_large_translation_page
    with closing(case.packages._connect()) as connection:
        connection.executemany(
            'INSERT INTO files(package_id,path,name,local_summary,updated_at) VALUES(?,?,?,?,0)',
            [(case.package_id, f'fixture-{i:05d}.txt', f'fixture-{i:05d}.txt',
              '{"sample":"preview","language":{"code":"en"}}') for i in range(2400)]
        )
    count = {'ordinary': 0, 'large': 0}
    for label, store in [('ordinary', case.storage), ('large', case.packages)]:
        original = store._connect
        def connect(original=original, label=label):
            count[label] += 1
            return original()
        monkeypatch.setattr(store, '_connect', connect)
    page = list_large_translation_page({'id': case.package_id}, case.packages, case.storage,
                                       offset=2300, limit=50, statuses=['pending'], language='foreign')
    assert page['total'] == 2400
    assert len(page['items']) == 50
    assert page['has_more'] is True
    assert count == {'ordinary': 2, 'large': 1}


def test_historical_summary_shapes_do_not_make_entire_list_fail(translation_api):
    case = translation_api
    with closing(case.packages._connect()) as connection:
        connection.execute('UPDATE files SET local_summary=? WHERE package_id=? AND path=?',
                           ('"legacy preview text"', case.package_id, '中文.txt'))
    response = case.client.get('/api/translations/' + case.package_id)
    assert response.status_code == 200, response.text
    item = next(item for item in response.json()['items'] if item['path'] == '中文.txt')
    assert item['source_availability'] == 'metadata'
    assert item['source_language'] == 'unknown'
