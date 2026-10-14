"""Readiness must fail closed on missing resources and inaccessible storage."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from services import deployment_readiness as readiness


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(DATA_DIR=tmp_path / 'state', OUTPUT_DIR=tmp_path / 'outputs',
        LOG_DIR=tmp_path / 'state' / 'logs', PARSE_TEMP_DIR=tmp_path / 'scratch',
        MODELS_DIR=tmp_path / 'models', TOOLS_DIR=tmp_path / 'tools',
        DOCLING_ARTIFACTS_DIR=tmp_path / 'models' / 'docling',
        RAPIDOCR_MODEL_DIR=tmp_path / 'models' / 'rapidocr',
        TRANSLATION_MODEL_PATH=str(tmp_path / 'models' / 'nllb'),
        TRANSLATION_CT2_MODEL_PATH=str(tmp_path / 'models' / 'ct2'),
        ENABLE_TRANSLATION=True, TRANSLATION_PROVIDER='offline_nllb',
        TRANSLATION_PREFER_CT2=True, MAIL_FEATURE_ENABLED=True)


def write(path, value=b'placeholder'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def sparse(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as handle:
        handle.truncate(size)


def full_prerequisites(config, monkeypatch):
    monkeypatch.setattr(readiness, 'module_present', lambda n: n != 'extract_msg')
    monkeypatch.setattr(readiness, 'tool_probe', lambda *a, **k: {
        'ok': True, 'required': k['required'], 'verification': 'execution_probe'})
    monkeypatch.setattr('services.mail.thai_ocr._resolve_resources', lambda: None)
    layout = Path(config.DOCLING_ARTIFACTS_DIR) / 'docling-project--docling-layout-heron'
    write(layout / 'config.json', b'{}')
    sparse(layout / 'model.safetensors', 150 * 1024 * 1024)
    table = Path(config.DOCLING_ARTIFACTS_DIR) / 'docling-project--docling-models' / 'model_artifacts' / 'tableformer' / 'fast'
    write(table / 'tm_config.json', b'{}')
    sparse(table / 'tableformer_fast.safetensors', 80 * 1024 * 1024)
    for name in readiness.RAPID_MODELS:
        sparse(Path(config.RAPIDOCR_MODEL_DIR) / name, 65536)
    config.ENABLE_TRANSLATION = False


def test_arbitrary_file_in_model_directory_never_proves_readiness(config, monkeypatch):
    monkeypatch.setattr(readiness, 'module_present', lambda n: True)
    for folder in (config.DOCLING_ARTIFACTS_DIR, config.RAPIDOCR_MODEL_DIR,
                   Path(config.TRANSLATION_MODEL_PATH)):
        write(folder / 'README.txt')
    result = readiness.deployment_report(config, ('json',), {}, scope='full')
    assert result['application_runtime_available']
    assert not result['full_prerequisites_ready']
    assert not result['ok']
    assert result['complete_functional_readiness'] is None
    assert not result['resource_checks']['rapidocr']['ok']
    assert result['resource_checks']['docling_layout']['required']


def test_runtime_scope_can_start_without_weights_or_model_service(config, monkeypatch):
    monkeypatch.setattr(readiness, 'module_present', lambda n: n == 'json')
    result = readiness.deployment_report(config, ('json',),
        {'generation': {'enabled': True, 'reachable': False}}, scope='runtime')
    assert result['ok']
    assert not result['model_services_prerequisites_ready']
    assert not result['full_prerequisites_ready']
    assert result['native_tools'] == {}
    assert result['functional_verification'] == 'not_tested'


def test_directory_probe_creates_replaces_and_cleans_missing_nested_path(tmp_path):
    target = tmp_path / 'missing' / '深层 目录'
    assert readiness.directory_check(target, writable=True)['ok']
    assert list(tmp_path.iterdir()) == []


def test_resource_presence_probe_never_creates_input_tree(tmp_path):
    target = tmp_path / '只读资源' / 'not-installed'
    result = readiness.directory_check(target, writable=False)
    assert result['required'] is False and result['ok'] is False
    assert result['verification'] == 'presence'
    assert not target.parent.exists()


def test_directory_probe_fails_even_when_access_claims_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.os, 'access', lambda *a: True)
    def fail(*args, **kwargs):
        raise PermissionError('injected filesystem denial')
    monkeypatch.setattr(Path, 'write_bytes', fail)
    result = readiness.directory_check(tmp_path, writable=True)
    assert result['parent_writable']
    assert not result['ok'] and result['code'] == 'PermissionError'


def test_directory_file_collision_is_not_writable_directory(tmp_path):
    target = tmp_path / 'file'
    target.write_bytes(b'x')
    result = readiness.directory_check(target, writable=True)
    assert not result['ok'] and result['code'] == 'NotADirectoryError'


def test_ct2_selection_matches_app_and_requires_vocabulary_and_tokenizer(config, monkeypatch):
    monkeypatch.setattr(readiness, 'module_present', lambda n: True)
    root = Path(config.TRANSLATION_CT2_MODEL_PATH)
    sparse(root / 'model.bin', 1024 * 1024)
    write(root / 'config.json', b'{}')
    result = readiness.translation_resources(config)
    assert result['backend'] == 'ctranslate2' and result['path'] == str(root)
    assert not result['ok']
    assert {Path(r['path']).name for r in result['files'] if not r['ok']} == {
        'tokenizer_config.json', 'tokenizer.json', 'source_vocabulary.json', 'target_vocabulary.json'}
    write(root / 'tokenizer_config.json', b'{}')
    write(root / 'tokenizer.json', b'{}')
    write(root / 'shared_vocabulary.json', b'["a","b"]')
    assert readiness.translation_resources(config)['ok']
    assert not readiness.translation_resources(config)['inference_tested']


def test_missing_ct2_native_module_checks_actual_transformers_fallback(config, monkeypatch):
    monkeypatch.setattr(readiness, 'module_present', lambda n: n != 'ctranslate2')
    write(Path(config.TRANSLATION_CT2_MODEL_PATH) / 'model.bin')
    result = readiness.translation_resources(config)
    assert result['backend'] == 'transformers'
    assert result['path'] == config.TRANSLATION_MODEL_PATH
    assert not result['ok']


def test_transformers_index_checks_every_shard(config, monkeypatch):
    monkeypatch.setattr(readiness, 'module_present', lambda n: False)
    root = Path(config.TRANSLATION_MODEL_PATH)
    for name in ('config.json', 'tokenizer_config.json', 'tokenizer.json'):
        write(root / name, b'{}')
    write(root / 'model.safetensors.index.json', json.dumps({'weight_map': {
        'a': 'model-00001.safetensors', 'b': 'model-00002.safetensors'}}).encode())
    sparse(root / 'model-00001.safetensors', 1024 * 1024)
    result = readiness.translation_resources(config)
    assert not result['ok']
    assert result['files'][-1]['path'].endswith('model-00002.safetensors')
    sparse(root / 'model-00002.safetensors', 1024 * 1024)
    assert readiness.translation_resources(config)['ok']


def test_disabled_translation_is_explicit_and_does_not_disable_parser(config, monkeypatch):
    config.ENABLE_TRANSLATION = False
    monkeypatch.setattr(readiness, 'module_present', lambda n: True)
    result = readiness.deployment_report(config, ('json',), {}, scope='runtime')
    assert not result['resource_checks']['translation']['required']
    assert result['resource_checks']['docling_layout']['required']
    assert result['resource_checks']['rapidocr']['required']


def test_explicit_mail_server_recognizer_is_required(config, monkeypatch):
    monkeypatch.setenv('MAIL_OCR_REC_MODEL', 'server')
    monkeypatch.setattr(readiness, 'module_present', lambda n: True)
    result = readiness.deployment_report(config, ('json',), {}, scope='runtime')
    profile = result['resource_checks']['mail_rapidocr_server']
    assert profile['required'] and not profile['ok']
    assert profile['files'][0]['path'].endswith('ch_PP-OCRv5_rec_server.onnx')


def test_unknown_enabled_translation_provider_is_not_disabled(config, monkeypatch):
    config.TRANSLATION_PROVIDER = 'missing-provider'
    monkeypatch.setattr(readiness, 'module_present', lambda n: True)
    result = readiness.translation_resources(config)
    assert result['required'] and not result['ok']
    assert result['code'] == 'invalid_translation_provider'


def test_all_prerequisites_never_claims_functional_inference(config, monkeypatch):
    full_prerequisites(config, monkeypatch)
    result = readiness.deployment_report(config, ('json',), {}, scope='full')
    assert result['ok'] and result['full_prerequisites_ready']
    assert result['complete_functional_readiness'] is None
    assert result['functional_verification'] == 'not_tested'
    assert not result['optional_features']['msg']['ok']
    assert not result['optional_features']['msg']['required']


def test_required_optional_mail_format_fails_closed(config, monkeypatch):
    full_prerequisites(config, monkeypatch)
    result = readiness.deployment_report(config, ('json',), {},
        scope='full', required_features=('msg',))
    assert not result['ok']
    assert result['optional_features']['msg']['required']


def test_missing_configured_model_blocks_full_prerequisites(config, monkeypatch):
    full_prerequisites(config, monkeypatch)
    result = readiness.deployment_report(config, ('json',),
        {'generation': {'enabled': True, 'reachable': True, 'configured_model_present': False}},
        scope='full')
    assert not result['ok']
    assert not result['model_services_prerequisites_ready']


def test_real_native_execution_probe_reports_only_execution():
    result = readiness.tool_probe(sys.executable, ['--version'], required=True)
    assert result['ok']
    assert result['verification'] == 'execution_probe'
    assert not result['content_parsing_tested']


def test_native_loader_failure_is_not_a_successful_probe():
    result = readiness.tool_probe(sys.executable, ['-c',
        "print('error while loading shared libraries'); raise SystemExit(1)"], required=True)
    assert not result['ok'] and result['code'] == 'tool_probe_failed'


def test_language_list_must_include_both_required_languages():
    result = readiness.tool_probe(sys.executable, ['-c', "print('eng')"],
                                  required=True, required_lines=('tha', 'eng'))
    assert not result['ok']
    assert result['missing_required_lines'] == ['tha']


def test_rar_resolver_uses_instance_bin_without_mutating_global(config, monkeypatch):
    rarfile = pytest.importorskip('rarfile')
    before = rarfile.SEVENZIP_TOOL
    name = '7z.exe' if os.name == 'nt' else '7z'
    binary = Path(config.TOOLS_DIR) / '7zip' / 'bin' / name
    write(binary)
    binary.chmod(0o755)
    monkeypatch.setattr(readiness.shutil, 'which', lambda *a: None)
    assert readiness._rar_executable(config) == str(binary)
    assert rarfile.SEVENZIP_TOOL == before


def test_pst_inventory_uses_shared_instance_resolver(config, monkeypatch):
    calls = []
    def resolver(received):
        calls.append(received)
        return 'instance-bundled-readpst'
    monkeypatch.setattr(readiness, 'readpst_executable', resolver)
    result = readiness.deployment_report(config, ('json',), {}, scope='runtime')
    assert result['optional_features']['pst']['ok']
    assert result['optional_features']['pst']['path'] == 'instance-bundled-readpst'
    assert calls == [config]


def test_presence_rejects_truncated_weights_and_invalid_json(tmp_path):
    write(tmp_path / 'model.safetensors')
    write(tmp_path / 'config.json', b'invalid JSON')
    assert not readiness.file_check(tmp_path / 'model.safetensors', 150 * 1024 * 1024)['ok']
    assert not readiness.file_check(tmp_path / 'config.json', json_object=True)['ok']
