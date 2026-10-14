"""A selected model's window/tokenizer governs budgets and cached evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from config import Config
from services import model_budget as budget
from services.model_contract import LocalModelError


def selection(**changes):
    values = dict(LLM_BACKEND='ollama', LLM_MODEL='customer-small',
                  LLM_BASE_URL='http://127.0.0.1:11434', LLM_CONTEXT_TOKENS=2048,
                  LLM_TOKENIZER_PATH='', LLM_MODEL_VERSION='', LLM_TOKENIZER_VERSION='')
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize('backend,window', [('vllm',2048), ('ollama',4096),
                                           ('openai_compatible',2048), ('vllm',32768)])
def test_config_accepts_actual_small_and_explicit_production_windows(tmp_path, backend, window):
    env_file = tmp_path / 'empty.env'
    env_file.write_text('', encoding='utf-8')
    env = {key:value for key,value in os.environ.items() if key not in {
        'LLM_TOKENIZER_PATH','CONVERSATION_TOKENIZER_PATH','CONVERSATION_CONTEXT_TOKENS',
        'SJFX_MODEL_ALLOWED_ENDPOINTS','SJFX_ENV_FILE','SJFX_STATE_DIR'}}
    env.update(SJFX_ENV_FILE=str(env_file), SJFX_STATE_DIR=str(tmp_path),
               LLM_BACKEND=backend, LLM_CONTEXT_TOKENS=str(window))
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c',
        'import json; from config import Config; print(json.dumps([Config.LLM_CONTEXT_TOKENS, Config.CONVERSATION_CONTEXT_TOKENS, Config.LLM_TOKENIZER_PATH]))'],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True,
        encoding='utf-8', check=True)
    assert json.loads(result.stdout) == [window, window, '']


def test_missing_tokenizer_never_reads_old_qwen_path(tmp_path):
    config = selection(VLLM_MODEL_PATH=str(tmp_path), CONVERSATION_TOKENIZER_PATH=str(tmp_path))
    (tmp_path / 'tokenizer.json').write_text('{}', encoding='utf-8')
    assert budget.token_count_method(config) == 'utf8_byte_upper_bound'
    assert budget.count_tokens('当前模型', config) == len('当前模型'.encode('utf-8'))


def test_tokenizer_cache_changes_with_model_endpoint_version_and_file(tmp_path):
    tokenizers = pytest.importorskip('tokenizers')
    file = tmp_path / 'tokenizer.json'
    encoder = tokenizers.Tokenizer(tokenizers.models.WordLevel({'[UNK]':0,'alpha':1}, unk_token='[UNK]'))
    encoder.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    encoder.save(str(file))
    config = selection(LLM_TOKENIZER_PATH=str(file))
    first = budget.tokenizer_identity(config)
    assert budget.count_tokens('alpha alpha', config) == 2
    other_client = SimpleNamespace(backend_name='ollama', model='translation-other',
                                   base_url=config.LLM_BASE_URL)
    assert budget.token_count_method(config, model=other_client) == 'utf8_byte_upper_bound'
    assert budget.count_tokens('alpha alpha', config, model=other_client) == len(b'alpha alpha')
    for key, value in [('LLM_MODEL','another'), ('LLM_BASE_URL','http://localhost:11434'),
                       ('LLM_MODEL_VERSION','new-weights'), ('LLM_TOKENIZER_VERSION','revision-2')]:
        changed = selection(LLM_TOKENIZER_PATH=str(file), **{key:value})
        assert budget.tokenizer_identity(changed) != first
    encoder = tokenizers.Tokenizer(tokenizers.models.WordLevel({'[UNK]':0,'alpha alpha':1}, unk_token='[UNK]'))
    encoder.save(str(file))
    assert budget.tokenizer_identity(config) != first
    assert budget.count_tokens('alpha alpha', config) == 1


def test_unavailable_tokenizer_recovers_when_file_appears(tmp_path):
    tokenizers = pytest.importorskip('tokenizers')
    file = tmp_path / 'tokenizer.json'
    config = selection(LLM_TOKENIZER_PATH=str(file))
    before = budget.tokenizer_identity(config)
    assert budget.token_count_method(config) == 'utf8_byte_upper_bound'
    tokenizers.Tokenizer(tokenizers.models.WordLevel({'[UNK]':0}, unk_token='[UNK]')).save(str(file))
    assert budget.token_count_method(config) == 'local_tokenizer'
    assert budget.tokenizer_identity(config) != before


def test_budget_bounds_large_output_without_mutating_source():
    source = '客户原文：' + '完整保留' * 100
    result = budget.request_budget('政策', source, 1000000, config=selection())
    assert result['input_tokens'] + result['max_tokens'] + result['reserve_tokens'] <= 2048
    assert 0 < result['max_tokens'] < result['requested_max_tokens']
    assert result['input_truncated'] is False
    assert source.endswith('完整保留' * 100)


def test_input_overflow_is_context_limit_and_unlimited_output_is_rejected():
    with pytest.raises(LocalModelError) as exc:
        budget.request_budget('policy', '原文' * 500, 256, config=selection())
    assert exc.value.code == 'context_limit'
    for output in (0, -1, float('inf')):
        with pytest.raises(LocalModelError) as exc:
            budget.request_budget('policy', 'source', output, config=selection())
        assert exc.value.code == 'configuration_error'


def test_small_document_chunks_preserve_every_source_character():
    from services.document_analysis import _split_text
    source = '逐字保留中英文ข้อมูล。\n' * 120
    chunks = _split_text(source, max_chunks=2, preferred_chars=1000,
                         max_input_tokens=128, overlap_chars=320)
    assert len(chunks) > 2
    assert chunks[0]['start'] == 0 and chunks[-1]['end'] == len(source)
    assert all(budget.count_tokens(chunk['text']) <= 128 for chunk in chunks)
    assert all(left['end'] >= right['start'] and left['start'] < right['start']
               for left,right in zip(chunks,chunks[1:]))
    assert all(chunk['text'] == source[chunk['start']:chunk['end']] for chunk in chunks)


def test_preliminary_batch_does_not_force_oversized_first_file():
    from services.preliminary_summary_batch import _fit_rows
    rows = [{'input':'完整原文' * 1000}]
    assert _fit_rows(rows, 2048) == []
    assert rows[0]['input'] == '完整原文' * 1000


def test_catalog_budget_and_cache_follow_selected_model(monkeypatch):
    from services import large_package_catalog_naming as naming
    monkeypatch.setattr(Config, 'LLM_CONTEXT_TOKENS', 2048)
    monkeypatch.setattr(Config, 'LLM_TOKENIZER_PATH', '')
    cfg = naming.settings()
    assert cfg['input'] + max(cfg['output'],cfg['namer_output'],600) + budget.reserve_tokens(2048) <= 2048
    before = naming.bundle_key('p','g',1,'owner')
    monkeypatch.setattr(Config, 'LLM_BASE_URL', 'http://localhost:9999')
    assert naming.bundle_key('p','g',1,'owner') != before


@pytest.mark.parametrize('url,explicit,expected', [
    ('http://10.8.0.2:8002/v1',None,''),
    ('http://127.0.0.1:8002/v1',None,'fixture-generation-key'),
    ('http://10.8.0.2:8002/v1','fixture-embedding-key','fixture-embedding-key'),
    ('http://127.0.0.1:8002/v1','', ''),
])
def test_embedding_credentials_do_not_follow_a_new_endpoint(tmp_path, url, explicit, expected):
    env_file = tmp_path / 'empty.env'
    env_file.write_text('', encoding='utf-8')
    env = {key:value for key,value in os.environ.items() if key.upper() in {
        'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','HOMEDRIVE','HOMEPATH'}}
    env.update(SJFX_ENV_FILE=str(env_file), SJFX_STATE_DIR=str(tmp_path),
               VLLM_API_KEY='fixture-generation-key', SJFX_EMBED_BACKEND='openai_compatible',
               SJFX_EMBED_BASE_URL=url, SJFX_MODEL_ALLOWED_ENDPOINTS='["http://10.8.0.2:8002"]')
    if explicit is not None:
        env['SJFX_EMBED_API_KEY'] = explicit
    result = subprocess.run([sys.executable,'-X','utf8','-c',
        'import json; from config import Config; print(json.dumps(Config.EMBED_API_KEY))'],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
        text=True, encoding='utf-8', check=True)
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize('phase2', [False, True])
def test_mail_all_phases_skip_overflow_without_cutting_json(monkeypatch, phase2):
    from services.mail.research.runtime import MailResearchRuntime
    events = []
    runtime = MailResearchRuntime(SimpleNamespace(event=lambda *args:events.append(args)),
                                  model=SimpleNamespace(model='small'), context_limit=1000)
    monkeypatch.setattr(runtime, '_continue_turn', lambda *args:True)
    state = {'model_calls':0, 'max_model_calls':5, 'phase2':phase2}
    prompt = json.dumps({'source_quote':'完整邮件原文' * 300}, ensure_ascii=False)
    result = runtime._prepare_model_call('turn','owner',state,'relevance','policy',prompt,
                                        required_fields=('decisions',))
    assert result is None and state['analysis_truncated'] is True
    assert events[-1][1] == 'model_skipped'
    assert events[-1][2]['error_code'] == 'context_limit'
    assert events[-1][2]['input_characters'] == len(prompt)
    assert json.loads(prompt)['source_quote'] == '完整邮件原文' * 300


def test_mail_response_cache_follows_actual_transport_endpoint_and_version():
    from services.mail.research.runtime import MailResearchRuntime
    configured = selection()
    transport = SimpleNamespace(model=configured.LLM_MODEL, backend_name='ollama',
                                base_url=configured.LLM_BASE_URL, budget_config=configured)
    runtime = MailResearchRuntime(None, model=SimpleNamespace(transport=transport, model=transport.model))
    def key():
        return runtime._request_key('relevance','policy','source',('decisions',),None,{})
    original = key()
    transport.base_url = 'http://localhost:11435'
    assert key() != original
    transport.base_url = configured.LLM_BASE_URL
    configured.LLM_MODEL_VERSION = 'weights-v2'
    assert key() != original
