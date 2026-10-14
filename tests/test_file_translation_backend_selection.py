"""Offline file configuration must select its named engine on either OS."""
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import pytest

from services.file_translation import select_offline_file_provider
from services.offline_translation import OfflineNLLBProvider
from services.quickmt_translation import OfflineQuickMTProvider
from services import deployment_readiness as readiness


def settings(tmp_path, shared, file_backend, enabled=True):
    return SimpleNamespace(
        ENABLE_TRANSLATION=enabled, TRANSLATION_PROVIDER=shared,
        TRANSLATION_FILE_BACKEND=file_backend,
        TRANSLATION_QUICKMT_MODEL_DIR=tmp_path / '资源 空格/quickmt',
        TRANSLATION_QUICKMT_BEAM_SIZE=5, TRANSLATION_CPU_THREADS=4,
        TRANSLATION_BATCH_SIZE=8, TRANSLATION_MAX_INPUT_TOKENS=1024,
        TRANSLATION_MAX_NEW_TOKENS=512,
        TRANSLATION_MODEL_PATH=tmp_path / '资源 空格/nllb',
        TRANSLATION_CT2_MODEL_PATH=tmp_path / '资源 空格/nllb-ct2',
        TRANSLATION_PREFER_CT2=True, TRANSLATION_DEVICE='cpu',
    )


@pytest.mark.parametrize('shared,file_backend,engine', [
    ('offline_nllb', 'offline_quickmt', OfflineQuickMTProvider),
    ('offline_quickmt', 'offline_nllb', OfflineNLLBProvider),
    ('ollama', 'offline_quickmt', OfflineQuickMTProvider),
])
def test_explicit_file_engine_does_not_use_different_shared_engine(tmp_path, shared, file_backend, engine):
    config = settings(tmp_path, shared, file_backend)
    shared_provider = object()
    selected = select_offline_file_provider(config, shared_provider)
    assert isinstance(selected, engine)
    assert selected is not shared_provider
    # Constructing this provider must not eagerly load native weights.
    assert getattr(selected, '_translator', None) is None
    assert getattr(selected, 'pairs', {}) == {}


@pytest.mark.parametrize('shared,file_backend,enabled', [
    ('offline_quickmt', 'offline_quickmt', True),
    ('offline_nllb', 'offline_nllb', True),
    ('offline_quickmt', 'vllm', True),
    ('offline_nllb', 'offline_quickmt', False),
])
def test_shared_model_and_disabled_provider_are_reused(tmp_path, shared, file_backend, enabled):
    provider = object()
    assert select_offline_file_provider(settings(tmp_path, shared, file_backend, enabled), provider) is provider


def test_nllb_file_backend_preserves_ct2_preference(tmp_path):
    config = settings(tmp_path, 'offline_quickmt', 'offline_nllb')
    config.TRANSLATION_CT2_MODEL_PATH.mkdir(parents=True)
    (config.TRANSLATION_CT2_MODEL_PATH / 'model.bin').write_bytes(b'fixture')
    with patch('services.file_translation.importlib.util.find_spec', return_value=object()):
        selected = select_offline_file_provider(config, object())
    assert Path(selected.model_path) == config.TRANSLATION_CT2_MODEL_PATH.resolve()


def test_doctor_checks_separate_offline_file_resources(tmp_path, monkeypatch):
    config = settings(tmp_path, 'ollama', 'offline_quickmt')
    for name in ('DATA_DIR', 'OUTPUT_DIR', 'LOG_DIR', 'PARSE_TEMP_DIR', 'MODELS_DIR', 'TOOLS_DIR',
                 'DOCLING_ARTIFACTS_DIR', 'RAPIDOCR_MODEL_DIR'):
        setattr(config, name, tmp_path / name)
    config.MAIL_FEATURE_ENABLED = False
    monkeypatch.setattr(readiness, 'module_present', lambda name: True)
    report = readiness.deployment_report(config, ('json',), {}, scope='runtime')
    assert report['resource_checks']['translation']['required'] is False
    offline_file = report['resource_checks']['file_translation']
    assert offline_file['provider'] == 'offline_quickmt'
    assert offline_file['required'] is True and offline_file['ok'] is False
    assert report['full_prerequisites_ready'] is False
