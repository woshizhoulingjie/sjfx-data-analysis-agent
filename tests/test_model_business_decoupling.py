"""Synthetic HTTP protocol -> typed business/translation/retrieval integration."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.agent_runtime import PydanticAgentRuntime
from services.file_translation import VLLMFileTranslationProvider, build_file_translation_service
from services.model_backend import create_model_client
from services.model_contract import LocalModelError
from services import large_package_recall


@contextmanager
def model_server():
    state = SimpleNamespace(content='{"summary":"合成资料结论"}', status=200, calls=[])
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.calls.append((self.path, payload))
            body = {'choices': [{'message': {'content': state.content}, 'finish_reason': 'stop'}], 'model': payload['model']}
            if state.status != 200:
                body = {'error': 'synthetic failure'}
            raw = json.dumps(body).encode()
            self.send_response(state.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = SimpleNamespace(LLM_BACKEND='openai_compatible', LLM_MAX_CONCURRENCY=1,
        LLM_QUEUE_TIMEOUT_SECONDS=1, ENABLE_OPENAI_COMPATIBLE=True,
        OPENAI_COMPATIBLE_BASE_URL='http://127.0.0.1:{}/v1'.format(server.server_port),
        OPENAI_COMPATIBLE_MODEL='synthetic-chat', OPENAI_COMPATIBLE_REQUEST_TIMEOUT=2,
        OPENAI_COMPATIBLE_API_KEY='', OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT=True,
        LLM_ENABLED=True, LLM_MODEL='synthetic-chat')
    config.LLM_BASE_URL = config.OPENAI_COMPATIBLE_BASE_URL
    try:
        yield state, config
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_typed_business_uses_generic_json_object_without_vllm_extensions(monkeypatch):
    monkeypatch.setenv('SJFX_SHARED_GENERATION_GATE', '0')
    with model_server() as (server, config):
        runtime = PydanticAgentRuntime(create_model_client(config))
        result = runtime.chat_json('Summarize the synthetic source.', 'Only synthetic test data.',
                                   required_fields=['summary'], timeout=1, structured_retry_limit=0)
        assert result['json']['summary'] == '合成资料结论'
        path, payload = server.calls[-1]
        assert path == '/v1/chat/completions'
        assert payload['response_format'] == {'type': 'json_object'}
        assert 'chat_template_kwargs' not in payload
        assert '安全边界' in payload['messages'][0]['content']
        server.content = 'not JSON'
        with pytest.raises(LocalModelError, match='结构化契约'):
            runtime.chat_json('Summarize', 'synthetic', required_fields=['summary'], structured_retry_limit=0)
        assert len(server.calls) == 2
        server.content = '{"summary":"恢复后的合成结论"}'
        assert runtime.chat_json('Summarize', 'synthetic', required_fields=['summary'])['json']['summary'].startswith('恢复')


def test_file_translation_uses_selected_generic_transport_and_preserves_source(monkeypatch):
    monkeypatch.setenv('SJFX_SHARED_GENERATION_GATE', '0')
    with model_server() as (server, config):
        server.content = '{"translation":"这是合成验证资料。"}'
        provider = VLLMFileTranslationProvider(create_model_client(config))
        source = 'This is synthetic validation material.'
        result = provider.translate(source, 'en')
        assert result.text == '这是合成验证资料。'
        assert result.metadata['backend'] == 'openai_compatible'
        assert provider.provider_id == 'openai_compatible:synthetic-chat'
        assert source in server.calls[0][1]['messages'][1]['content']
        assert server.calls[0][1]['model'] == 'synthetic-chat'


def test_same_named_translation_model_has_separate_cache_contract_for_each_backend():
    contracts = set()
    for backend in ('vllm', 'ollama', 'openai_compatible'):
        client = SimpleNamespace(backend_name=backend, model='same-model')
        service = build_file_translation_service(client, memory=None)
        assert service.provider.provider_id == backend + ':same-model'
        assert service.policy.contract_version.endswith(backend + ':same-model')
        contracts.add(service.policy.contract_version)
    assert len(contracts) == 3


def test_large_rerank_switches_with_selected_backend_and_retains_candidates_on_outage(monkeypatch):
    monkeypatch.setenv('SJFX_SHARED_GENERATION_GATE', '0')
    items = [{'source_path': '{}.txt'.format(i)} for i in range(3)]
    with model_server() as (server, config), patch('config.Config', config):
        server.content = '3,1'
        ranked = large_package_recall.rerank_candidates('synthetic source', items)
        assert ranked == [items[2], items[0], items[1]]
        assert server.calls[0][0] == '/v1/chat/completions'
        server.status = 503
        assert large_package_recall.rerank_candidates('synthetic source', items) == items
        assert len(server.calls) == 2  # optional rerank never amplifies an outage
        config.ENABLE_OPENAI_COMPATIBLE = False
        assert large_package_recall.rerank_candidates('synthetic source', items) == items
        assert len(server.calls) == 2
