"""Actual LAN HTTP plus explicit DNS/TLS fixtures; no inference runtime."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
import ssl
import threading
import time
from types import SimpleNamespace

import pytest

from services.embedding_backend import create_embedding_client
from services.model_backend import create_model_client, create_translation_client
from services.model_contract import (LocalModelError, model_url_origin, normalize_allowed_endpoints,
    open_local_request, validate_local_url, validate_model_url)
from services.openai_compatible import OpenAICompatibleClient


def lan_ip():
    addresses = socket.getaddrinfo(socket.gethostname(), 0, socket.AF_INET, socket.SOCK_STREAM)
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_loopback and not address.is_link_local:
            return str(address)
    pytest.skip('No usable LAN IPv4 interface on this test host')


@contextmanager
def service(tls=None):
    state = SimpleNamespace(calls=[], redirect=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            payload = json.loads(raw) if raw else None
            state.calls.append(dict(path=self.path, payload=payload,
                                    authorization=self.headers.get('Authorization'), host=self.headers.get('Host')))
            if state.redirect:
                self.send_response(307)
                self.send_header('Location', 'http://169.254.169.254/latest/meta-data')
                self.end_headers()
                return
            if self.path.endswith('/api/tags'):
                data = {'models': [{'name': 'fixture:latest', 'digest': 'revision-1'}]}
            elif self.path.endswith('/models'):
                data = {'data': [{'id': 'fixture', 'revision': 'revision-1'}]}
            elif self.path.endswith('/embeddings'):
                data = {'model': 'fixture', 'data': [{'index': i, 'embedding': [1.0, 2.0]}
                        for i in range(len(payload['input']))]}
            elif self.path.endswith('/api/embed'):
                data = {'model': 'fixture', 'embeddings': [[1.0, 2.0] for _ in payload['input']]}
            elif self.path.endswith('/api/chat'):
                data = {'model': 'fixture', 'message': {'content': '{"answer":"LAN fixture"}'},
                        'done': True, 'done_reason': 'stop'}
            else:
                data = {'model': 'fixture', 'choices': [{'message': {'content': '{"answer":"LAN fixture"}'},
                        'finish_reason': 'stop'}]}
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    server.daemon_threads = True
    if tls:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    state.port = server.server_port
    state.ip = lan_ip()
    state.base = '{}://{}:{}'.format('https' if tls else 'http', state.ip, state.port)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv('SJFX_SHARED_GENERATION_GATE', '0')
    monkeypatch.setattr('services.llm_trace.get_recorder', lambda: None)


def config_for(server, backend='openai_compatible'):
    return SimpleNamespace(LLM_BACKEND=backend, LLM_MAX_CONCURRENCY=1, LLM_CONTEXT_TOKENS=8192,
        LLM_QUEUE_TIMEOUT_SECONDS=1, MODEL_ALLOWED_ENDPOINTS=(server.base,),
        ENABLE_VLLM=True, ENABLE_SHARED_OLLAMA=True, ENABLE_OPENAI_COMPATIBLE=True,
        VLLM_BASE_URL=server.base+'/v1', VLLM_MODEL='fixture', VLLM_REQUEST_TIMEOUT=2, VLLM_API_KEY='generation-key',
        OLLAMA_BASE_URL=server.base, OLLAMA_MODEL='fixture', SHARED_OLLAMA_REQUEST_TIMEOUT=2, OLLAMA_API_KEY='ollama-key',
        OPENAI_COMPATIBLE_BASE_URL=server.base+'/v1', OPENAI_COMPATIBLE_MODEL='fixture',
        OPENAI_COMPATIBLE_REQUEST_TIMEOUT=2, OPENAI_COMPATIBLE_API_KEY='compatible-key',
        OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT=True, TRANSLATION_OLLAMA_BASE_URL=server.base,
        TRANSLATION_OLLAMA_MODEL='fixture', EMBED_BACKEND='openai_compatible', EMBED_BASE_URL=server.base+'/ordinary/v1',
        EMBED_MODEL='fixture', EMBED_API_KEY='embedding-key', EMBED_ENCODING='plain',
        MAIL_EMBED_BASE_URL=server.base+'/mail/v1', MAIL_EMBED_MODEL='fixture',
        SJFX_LP_EMBED_BASE_URL=server.base+'/large/v1')


@pytest.mark.parametrize('backend', ['vllm', 'ollama', 'openai_compatible'])
def test_real_lan_factory_health_generation_and_privacy(backend, monkeypatch):
    with service() as server:
        config = config_for(server, backend)
        # An invalid environment proxy must never intercept even remote traffic.
        monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
        monkeypatch.setenv('https_proxy', 'http://127.0.0.1:1')
        monkeypatch.setenv('NO_PROXY', '')
        client = create_model_client(config)
        assert client.health_check()['model_available']
        assert client.chat('s', 'synthetic evidence', retries=0)['content'] == '{"answer":"LAN fixture"}'
        assert client.chat_json('s', 'synthetic evidence', max_tokens=128, retries=0)['json']['answer'] == 'LAN fixture'
        assert '远端' in client.privacy_label and '不出网' not in client.privacy_label
        assert len(server.calls) == 3
        if backend == 'ollama':
            for call in server.calls[1:]:
                assert call['payload']['options']['num_ctx'] == 8192
            assert server.calls[-1]['payload']['options']['num_predict'] == 128
        config.MODEL_ALLOWED_ENDPOINTS = ()
        with pytest.raises(LocalModelError):
            create_model_client(config)


def test_real_lan_translation_and_all_embedding_lanes_have_separate_identity():
    with service() as server:
        config = config_for(server)
        assert create_translation_client(config).chat('s', 'u', retries=0)['content']
        identities = set()
        for backend in ('openai_compatible', 'ollama'):
            config.EMBED_BACKEND = backend
            for lane in ('ordinary', 'large', 'mail'):
                client = create_embedding_client(config, lane=lane)
                assert client.embed(['synthetic']) == [[1.0, 2.0]]
                assert client.allowed_endpoints == (server.base,)
                identities.add(client.identity)
                assert server.calls[-1]['authorization'] == 'Bearer embedding-key'
                assert '/'+lane in server.calls[-1]['path']
        assert len(identities) == 6
        server.redirect = True
        with pytest.raises(LocalModelError) as caught:
            create_embedding_client(config).embed(['synthetic'])
        assert caught.value.code == 'redirect_refused'


@pytest.mark.parametrize('backend', ['vllm', 'ollama', 'openai_compatible'])
def test_small_context_rejects_full_input_before_http_and_reduces_only_output(backend):
    with service() as server:
        config = config_for(server, backend)
        config.LLM_CONTEXT_TOKENS = 2048
        client = create_model_client(config)
        with pytest.raises(LocalModelError) as caught:
            client.chat('s', 'x'*2048, retries=0)
        assert caught.value.code == 'context_limit'
        assert not server.calls
        evidence = 'x'*1500
        result = client.chat('s', evidence, max_tokens=1800, retries=0)
        payload = server.calls[-1]['payload']
        assert payload['messages'][1]['content'] == evidence
        effective = payload['options']['num_predict'] if backend == 'ollama' else payload['max_tokens']
        assert 1 <= effective < 1800
        assert result['context_budget']['input_truncated'] is False
        assert result['context_budget']['token_count_method'] == 'utf8_byte_upper_bound'


@pytest.mark.parametrize('backend', ['vllm', 'ollama', 'openai_compatible'])
def test_overlimit_preflight_precedes_shared_admission_and_valid_requests_keep_busy(backend, monkeypatch):
    config = config_for(SimpleNamespace(base='http://127.0.0.1:1'), backend)
    config.LLM_CONTEXT_TOKENS = 2048
    client = create_model_client(config)
    admissions = []

    @contextmanager
    def busy(*args, **kwargs):
        admissions.append(args)
        raise LocalModelError('fixture busy', code='busy', retryable=True)
        yield

    monkeypatch.setattr('services.model_dispatch.generation_slot', busy)
    for call in (client.chat, client.chat_json):
        with pytest.raises(LocalModelError) as caught:
            call('s', 'x'*3000, retries=0, max_tokens=32)
        assert caught.value.code == 'context_limit'
    assert not admissions
    with pytest.raises(LocalModelError) as caught:
        client.chat('s', 'valid short evidence', retries=0, max_tokens=32)
    assert caught.value.code == 'busy'
    assert len(admissions) == 1


def test_embedding_key_fallback_and_artifact_identity_do_not_use_unrelated_generation_model(tmp_path):
    with service() as server:
        config = config_for(server)
        del config.EMBED_API_KEY
        config.EMBED_BACKEND = 'ollama'
        assert create_embedding_client(config).embed(['synthetic'])
        assert server.calls[-1]['authorization'] == 'Bearer ollama-key'
        artifact = tmp_path/'model.safetensors'
        artifact.write_bytes(b'legacy-e5-artifact')
        config.EMBED_MODEL_PATH = str(tmp_path)
        for encoding in ('e5', 'plain'):
            config.EMBED_ENCODING = encoding
            assert create_embedding_client(config).weights_id == ''
        config.EMBED_BACKEND = 'openai_compatible'
        config.EMBED_ENCODING = 'e5'
        config.VLLM_EMBED_BASE_URL = config.EMBED_BASE_URL
        config.VLLM_EMBED_MODEL = config.EMBED_MODEL
        legacy = create_embedding_client(config)
        assert legacy.weights_id
        config.EMBED_BASE_URL = server.base+'/new/v1'
        assert create_embedding_client(config).weights_id == ''
        config.EMBED_WEIGHTS_ID = 'explicit-revision'
        assert create_embedding_client(config).weights_id == 'explicit-revision'


@pytest.mark.parametrize('backend', ['openai_compatible', 'ollama'])
def test_missing_embedding_key_never_forwards_generation_key_to_another_origin(backend):
    with service() as owner, service() as embedding:
        config = config_for(owner)
        config.EMBED_BACKEND = backend
        config.EMBED_BASE_URL = embedding.base+'/v1'
        config.MODEL_ALLOWED_ENDPOINTS = (owner.base, embedding.base)
        del config.EMBED_API_KEY
        client = create_embedding_client(config)
        assert client.api_key == ''
        assert client.embed(['synthetic'])
        assert embedding.calls[-1]['authorization'] is None
        # An explicit empty embedding credential must also suppress inheritance.
        config.EMBED_BASE_URL = owner.base+'/v1'
        config.EMBED_API_KEY = ''
        client = create_embedding_client(config)
        assert client.api_key == ''
        assert client.embed(['synthetic'])
        assert owner.calls[-1]['authorization'] is None
        config.EMBED_BASE_URL = embedding.base+'/v1'
        config.EMBED_API_KEY = 'dedicated-embedding-key'
        assert create_embedding_client(config).embed(['synthetic'])
        assert embedding.calls[-1]['authorization'] == 'Bearer dedicated-embedding-key'


def test_endpoint_normalization_is_exact_origin_and_local_contract_unchanged():
    assert normalize_allowed_endpoints('["https://MODEL.example:443/v1/", "https://model.example"]') == ('https://model.example',)
    assert model_url_origin('http://192.168.1.3:8080/v1') == 'http://192.168.1.3:8080'
    assert validate_model_url('https://MODEL.example:443/v1/', ['https://model.example']) == 'https://model.example/v1'
    for url in ('http://model.example/v1', 'https://model.example:444/v1', 'https://other.example/v1'):
        with pytest.raises(LocalModelError):
            validate_model_url(url, ['https://model.example'])
    with pytest.raises(LocalModelError):
        validate_local_url('http://192.168.1.3:8080/v1')
    assert validate_model_url('http://localhost:123/v1/') == 'http://localhost:123/v1'


@pytest.mark.parametrize('raw', ['*', '["*"]', '["http://*.example"]', '{}', 'null', '[17]',
    '["http://169.254.169.254"]', '["http://100.100.100.200"]', '["http://168.63.129.16"]',
    '["http://[fd00:ec2::254]"]', '["http://0.0.0.1"]',
    '["http://[fe80::1]"]', '["http://[::ffff:127.0.0.1]"]', '["http://metadata.google.internal"]',
    '["http://user:secret@model.example"]', '["http://model.example?secret=1"]',
    '["http://model.example#secret"]', '["http://model.example/%2fsecret"]', '["http://2130706433"]'])
def test_policy_never_allows_metadata_parser_ambiguity_or_wildcards(raw):
    with pytest.raises(LocalModelError) as caught:
        normalize_allowed_endpoints(raw)
    assert 'secret' not in str(caught.value)


def test_dns_is_checked_once_then_socket_is_pinned_and_rechecked_next_request(monkeypatch):
    with service() as server:
        base = 'http://model.test:{}'.format(server.port)
        lookups = []

        def resolver(host, port, **kwargs):
            lookups.append(host)
            ip = server.ip if len(lookups) == 1 else '169.254.169.254'
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (ip, port))]

        monkeypatch.setattr(socket, 'getaddrinfo', resolver)
        client = OpenAICompatibleClient(base+'/v1', 'fixture', allowed_endpoints=[base])
        assert client.chat('s', 'u', retries=0)['content']
        assert lookups == ['model.test']
        assert server.calls[0]['host'] == 'model.test:{}'.format(server.port)
        with pytest.raises(LocalModelError) as caught:
            client.chat('s', 'u', retries=0)
        assert caught.value.code == 'endpoint_refused'
        assert len(server.calls) == 1


@pytest.mark.parametrize('ips', [('10.0.0.1', '169.254.169.254'), ('127.0.0.1',), ('::ffff:169.254.169.254',)])
def test_any_unsafe_dns_result_is_rejected_before_socket_connection(ips, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (ip, 80)) for ip in ips])
    monkeypatch.setattr(socket.socket, 'connect', lambda *args: pytest.fail('Unsafe DNS result connected'))
    import urllib.request
    with pytest.raises(LocalModelError) as caught:
        open_local_request(urllib.request.Request('http://model.test/v1'), 1, ['http://model.test'])
    assert caught.value.code == 'endpoint_refused'


def test_slow_dns_obeys_total_timeout(monkeypatch):
    def resolver(*args, **kwargs):
        time.sleep(.2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('10.0.0.1', 80))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolver)
    client = OpenAICompatibleClient('http://model.test/v1', 'fixture', timeout=.03,
                                   allowed_endpoints=['http://model.test'])
    started = time.monotonic()
    with pytest.raises(LocalModelError) as caught:
        client.chat('s', 'u', retries=0)
    assert caught.value.code == 'timeout'
    assert time.monotonic() - started < .15


def tls_context(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'model.test')])
    now = datetime.now(timezone.utc)
    certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())\
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))\
        .not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('model.test')]), False)\
        .sign(key, hashes.SHA256())
    cert_file, key_file = tmp_path/'fixture.pem', tmp_path/'fixture.key'
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                          serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context, cert_file


def test_tls_default_rejects_untrusted_certificate_and_preserves_hostname_when_trusted(tmp_path, monkeypatch):
    context, cert_file = tls_context(tmp_path)
    names = []
    context.set_servername_callback(lambda sock, name, ctx: names.append(name))
    with service(context) as server:
        base = 'https://model.test:{}'.format(server.port)
        monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, **kwargs:
            [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (server.ip, port))])
        client = OpenAICompatibleClient(base+'/v1', 'fixture', timeout=1, allowed_endpoints=[base])
        with pytest.raises(LocalModelError) as caught:
            client.chat('s', 'u', retries=0)
        assert caught.value.code == 'unavailable'
        assert not server.calls
        # Trust only the fixture cert to verify hostname/SNI survives IP pinning.
        trusted = ssl.create_default_context(cafile=str(cert_file))
        monkeypatch.setattr(ssl, '_create_default_https_context', lambda: trusted)
        assert client.chat('s', 'u', retries=0)['content']
        assert names[-1] == 'model.test'
        assert server.calls[-1]['host'] == 'model.test:{}'.format(server.port)
