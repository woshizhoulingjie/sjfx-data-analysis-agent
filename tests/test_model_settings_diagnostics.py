import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from services.model_diagnostics import test_models as probe_models
from services.model_settings import get_model_settings, save_model_settings, read_model_overrides, model_snapshot
from services.model_settings_routes import register_model_settings_routes
from web_compat import SJFXFastAPI, request


class ModelHTTP(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        self.server.seen.append((self.path, None, self.headers.get('Authorization')))
        if self.server.mode == 'authentication':
            self.reply({'error': 'echoed-wrong-secret-value'}, 401)
            return
        if self.server.mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:%s/leak' % self.server.server_port)
            self.end_headers()
            return
        if self.server.mode == 'slow':
            time.sleep(2)
        if self.path == '/api/tags':
            self.reply({'models': [{'name': 'local-model:latest'}, {'name': 'embed:latest'}]})
        else:
            self.reply({'data': [{'id': 'local-model'}, {'id': 'embed'}]})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.seen.append((self.path, data, self.headers.get('Authorization')))
        if self.server.mode == 'authentication':
            self.reply({'error': 'echoed-wrong-secret-value'}, 401)
            return
        if self.server.mode == 'list_only':
            self.reply({'error': 'private-secret-value'}, 503)
            return
        if self.path in {'/v1/embeddings', '/api/embed'}:
            vectors = [[.1, .2, .3]]
            if self.server.mode == 'zero':
                vectors = [[0, 0, 0]]
            if self.server.mode == 'nan':
                vectors = [[.1, float('nan'), .3]]
            if self.path == '/api/embed':
                self.reply({'embeddings': vectors})
            else:
                index = 1 if self.server.mode == 'index' else 0
                items = [{'index': index, 'embedding': vectors[0]}]
                if self.server.mode == 'extra':
                    items.append(items[0])
                self.reply({'data': items, 'model': 'embed'})
            return
        structured = 'exactly' in data['messages'][-1]['content']
        content = '{"ok":true}' if structured and self.server.mode != 'bad_json' else 'OK'
        finish = 'stop'
        if self.server.mode in {'long_generation', 'truncated'}:
            # Real-compatible generation may spend more than 32 tokens before
            # completing even a short response. Respect the requested budget.
            required = 80 if structured else 64
            budget = data.get('max_tokens', data.get('options', {}).get('num_predict', 0))
            content = json.dumps({'ok': True, 'detail': 'detail ' * 48}) if structured else 'detail ' * 48 + 'OK'
            if budget < required or self.server.mode == 'truncated':
                finish = 'length'
                content = content[:max(1, min(budget, required))]
        if structured and self.server.mode == 'wrapped_json':
            content = '```json\n{"ok":true}\n```'
        if structured and self.server.mode == 'nonfinite_json':
            content = '{"ok":true,"value":NaN}'
        if self.path == '/api/chat':
            self.reply({'message': {'content': content}, 'done': True, 'done_reason': finish})
        else:
            self.reply({'model': 'local-model', 'choices': [{'message': {'content': content}, 'finish_reason': finish}]})


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ModelHTTP)
        self.server.daemon_threads = True
        self.server.mode, self.server.seen = 'ok', []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%s/v1' % self.server.server_port
        self.config = SimpleNamespace(DATA_DIR=Path(self.temp.name), LLM_BACKEND='openai_compatible',
            OPENAI_COMPATIBLE_BASE_URL=self.url, OPENAI_COMPATIBLE_MODEL='local-model',
            OPENAI_COMPATIBLE_API_KEY='private-secret-value', OPENAI_COMPATIBLE_REQUEST_TIMEOUT=60,
            OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT=False, ENABLE_OPENAI_COMPATIBLE=True,
            LLM_CONTEXT_TOKENS=8192, LLM_MAX_CONCURRENCY=1,
            SJFX_EMBED_BACKEND='openai_compatible', SJFX_EMBED_BASE_URL=self.url,
            SJFX_EMBED_MODEL='embed', SJFX_EMBED_API_KEY='embed-private-value', SJFX_EMBED_DIMENSION=3,
            VLLM_EMBED_TIMEOUT_SECONDS=12, LP_EMBED_CONCURRENCY=2,
            OLLAMA_BASE_URL=self.url, OLLAMA_MODEL='local-model', OLLAMA_API_KEY='',
            SHARED_OLLAMA_REQUEST_TIMEOUT=60, ENABLE_SHARED_OLLAMA=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_persistence_write_only_and_pending_restart(self):
        report = save_model_settings(self.config, {'generation': {'model': 'new-model', 'api_key': 'new-secret'}})
        self.assertTrue(report['restart_required'])
        self.assertEqual(report['model_interface_version'], 2)
        self.assertEqual(report['active']['generation']['model'], 'local-model')
        self.assertEqual(report['saved']['generation']['model'], 'new-model')
        self.assertNotIn('new-secret', json.dumps(report))
        self.assertNotIn('private-secret-value', json.dumps(report))
        save_model_settings(self.config, {'generation': {'api_key': ''}})
        stored = json.loads((self.config.DATA_DIR / 'model_settings.json').read_text())
        self.assertEqual(stored['version'], 1)
        self.assertEqual(stored['env_overrides']['OPENAI_COMPATIBLE_API_KEY'], 'new-secret')
        report = save_model_settings(self.config, {'generation': {'clear_api_key': True}})
        self.assertFalse(report['saved']['generation']['api_key_configured'])

    def test_configuration_rejects_unsafe_or_ambiguous_values(self):
        for payload in [
            {'generation': {'base_url': 'https://example.com/v1?key=secret'}},
            {'generation': {'base_url': 'http://169.254.169.254/v1'}},
            {'generation': {'base_url': 'http://metadata.google.internal/v1'}},
            {'generation': {'base_url': 'http://token@127.0.0.1/v1'}},
            {'generation': {'base_url': self.url + '?key=secret'}},
            {'generation': {'api_key': 'header\r\ninjection'}},
            {'generation': {'model': 'm' * 65536}},
            {'generation': {'backend': []}},
            {'generation': {'concurrency': True}},
            {'embedding': {'dimension': '-1'}},
            {'application_token': 'cannot-modify-auth'},
        ]:
            with self.subTest(payload=payload), self.assertRaises((ValueError, RuntimeError)):
                save_model_settings(self.config, payload)
        self.assertFalse((self.config.DATA_DIR / 'model_settings.json').exists())

    def test_real_chat_structured_embedding_and_proxy_ignored(self):
        with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1', 'no_proxy': '', 'NO_PROXY': ''}):
            report = probe_models(self.config, timeout_seconds=3)
        self.assertTrue(report['ok'], report)
        paths = [r[0] for r in self.server.seen]
        self.assertEqual(paths.count('/v1/chat/completions'), 2)
        self.assertEqual(paths.count('/v1/embeddings'), 1)
        self.assertNotIn('private-secret-value', json.dumps(report))
        self.assertEqual(report['roles']['embedding']['capability']['observed_dimension'], 3)
        chat = next(body for path, body, _ in self.server.seen if path.endswith('chat/completions'))
        self.assertNotIn('chat_template_kwargs', chat)
        self.assertNotIn('response_format', chat)

    def test_remote_draft_policy_is_request_scoped_and_never_saved(self):
        original = dict(os.environ)
        calls = []
        def request(request, timeout, *, allowed_endpoints=()):
            calls.append((request.full_url, request.get_header('Authorization'), allowed_endpoints))
            body = json.loads(request.data) if request.data else None
            if body is None:
                response = {'data': [{'id': 'local-model'}]}
            else:
                content = '{"ok":true}' if 'exactly' in body['messages'][-1]['content'] else 'OK'
                response = {'model': 'local-model', 'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]}
            return io.BytesIO(json.dumps(response).encode())
        with patch('services.model_diagnostics.open_local_request', side_effect=request):
            report = probe_models(self.config, payload={'generation': {
                'base_url': 'https://models.customer.example:443/v1/', 'api_key': 'new-draft-key'}}, role='generation', timeout_seconds=3)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['tested_configuration'], 'draft')
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(policy == ('https://models.customer.example',) for _, _, policy in calls))
        self.assertTrue(all(secret == 'Bearer new-draft-key' for _, secret, _ in calls))
        self.assertEqual(os.environ, original)
        self.assertFalse((self.config.DATA_DIR / 'model_settings.json').exists())
        self.assertNotIn('new-draft-key', json.dumps(report))

    def test_remote_save_authorizes_exact_origin_and_clears_old_credentials(self):
        report = save_model_settings(self.config, {'generation': {'base_url': 'https://models.customer.example:8443/v1/', 'api_key': '   '}})
        env = read_model_overrides(self.config.DATA_DIR)
        self.assertEqual(json.loads(env['SJFX_MODEL_ALLOWED_ENDPOINTS']), ['https://models.customer.example:8443'])
        self.assertEqual(report['saved']['generation']['base_url'], 'https://models.customer.example:8443/v1')
        self.assertFalse(report['saved']['generation']['api_key_configured'])
        self.assertTrue(report['active']['generation']['api_key_configured'])
        self.assertTrue(report['restart_required'])
        self.assertNotIn('private-secret-value', json.dumps(report))

    def test_backend_switch_requires_new_identity_and_never_reuses_target_secret(self):
        self.config.OLLAMA_API_KEY = 'stale-ollama-key'
        for role in ('generation', 'embedding'):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, '服务地址和模型名称'):
                save_model_settings(self.config, {role: {'backend': 'ollama'}})
        report = save_model_settings(self.config, {'generation': {'backend': 'ollama', 'base_url': self.url, 'model': 'local-model', 'api_key': ''}})
        self.assertFalse(report['saved']['generation']['api_key_configured'])
        self.assertNotIn('stale-ollama-key', json.dumps(report))

    def test_model_switch_clears_stale_tokenizer_and_embedding_identity(self):
        self.config.CONVERSATION_TOKENIZER_PATH = '/old/chat-tokenizer'
        self.config.LLM_TOKENIZER_VERSION = 'old-tokenizer-v1'
        self.config.LLM_MODEL_VERSION = 'old-chat-v1'
        self.config.SJFX_EMBED_TOKENIZER_PATH = '/old/embed-tokenizer'
        self.config.SJFX_EMBED_ENCODING = 'e5'
        self.config.SJFX_EMBED_VERSION = 'old-embed-v1'
        self.config.SJFX_EMBED_WEIGHTS_ID = 'old-weights'
        report = save_model_settings(self.config, {'generation': {'model': 'new-chat', 'tokenizer_path': '   '}, 'embedding': {'model': 'new-embed'}})
        gen, embed = report['saved']['generation'], report['saved']['embedding']
        self.assertEqual(gen['tokenizer_path'], '')
        self.assertEqual(gen['token_counting'], 'utf8_byte_upper_bound')
        self.assertEqual(gen['tokenizer_version'], '')
        self.assertEqual(gen['model_version'], '')
        self.assertEqual(embed['tokenizer_path'], '')
        self.assertEqual(embed['encoding'], 'plain')
        self.assertEqual(embed['dimension'], 0)
        self.assertEqual(embed['version'], '')
        self.assertEqual(embed['weights_id'], '')
        self.assertTrue(report['active']['generation']['tokenizer_path'])
        self.assertEqual(read_model_overrides(self.config.DATA_DIR)['CONVERSATION_TOKENIZER_PATH'], '')

    def test_embedding_save_unifies_visible_legacy_lanes(self):
        self.config.MAIL_EMBED_BASE_URL = 'http://127.0.0.1:8123/v1'
        self.config.MAIL_EMBED_MODEL = 'old-mail-model'
        self.config.LP_EMBED_BASE_URL = 'http://127.0.0.1:8124/v1'
        self.config.LP_EMBED_FALLBACK_URL = 'http://127.0.0.1:8125/v1'
        before = get_model_settings(self.config)
        self.assertTrue(before['active']['embedding']['lane_overrides'])
        self.assertEqual(before['active']['embedding']['lanes']['mail']['model'], 'old-mail-model')
        report = save_model_settings(self.config, {'embedding': {'base_url': self.url, 'model': 'embed'}})
        self.assertFalse(report['saved']['embedding']['lane_overrides'])
        self.assertEqual(report['saved']['embedding']['large_fallback_base_url'], '')
        self.assertTrue(report['active']['embedding']['lane_overrides'])
        self.assertTrue(report['restart_required'])
        for row in report['saved']['embedding']['lanes'].values():
            self.assertEqual(row, {'base_url': self.url, 'model': 'embed'})

    def test_small_windows_disabled_roles_and_legacy_v1(self):
        for window in (2048, 4096):
            self.assertEqual(save_model_settings(self.config, {'generation': {'context_tokens': window}})['saved']['generation']['context_tokens'], window)
        report = probe_models(self.config, payload={'generation': {'enabled': False}, 'embedding': {'backend': 'disabled'}}, timeout_seconds=3)
        self.assertFalse(report['ok'])
        self.assertTrue(all(row['code'] == 'disabled' for row in report['roles'].values()))
        self.assertEqual(self.server.seen, [])
        legacy = {'version': 1, 'env_overrides': {'OPENAI_COMPATIBLE_BASE_URL': self.url, 'OPENAI_COMPATIBLE_MODEL': 'local-model'}}
        (self.config.DATA_DIR / 'model_settings.json').write_text(json.dumps(legacy), encoding='utf-8')
        self.assertFalse(get_model_settings(self.config)['restart_required'])

    def test_explicit_new_bindings_and_same_origin_credentials_are_retained(self):
        report = save_model_settings(self.config, {'generation': {'model': 'new-chat', 'tokenizer_path': '/new/chat-tokenizer', 'tokenizer_version': 'v2', 'model_version': 'v2'},
            'embedding': {'model': 'new-embed', 'tokenizer_path': '/new/embed-tokenizer', 'dimension': 5, 'encoding': 'plain', 'version': 'v2'}})
        self.assertEqual(report['saved']['generation']['tokenizer_path'], '/new/chat-tokenizer')
        self.assertTrue(report['saved']['generation']['api_key_configured'])
        self.assertEqual(report['saved']['embedding']['dimension'], 5)
        self.assertEqual(report['saved']['embedding']['version'], 'v2')
        report = save_model_settings(self.config, {'generation': {'base_url': self.url + '/other-service', 'api_key': ' '}})
        self.assertFalse(report['saved']['generation']['api_key_configured'])

    def test_model_list_is_not_inference_success(self):
        self.server.mode = 'list_only'
        report = probe_models(self.config, timeout_seconds=3)
        self.assertFalse(report['ok'])
        self.assertTrue(report['roles']['generation']['reachable'])
        self.assertTrue(report['roles']['generation']['configured_model_present'])
        self.assertFalse(report['roles']['generation']['inference'])
        self.assertNotIn('private-secret-value', json.dumps(report))

    def test_wrong_credentials_and_service_error_body_never_echo(self):
        self.server.mode = 'authentication'
        report = probe_models(self.config, payload={'generation': {'api_key': 'wrong-secret-value'}}, role='generation', timeout_seconds=3)
        self.assertFalse(report['ok'])
        checks = report['roles']['generation']['checks']
        self.assertTrue(all(check['code'] == 'authentication_error' for check in checks.values()))
        self.assertTrue(all(check['http_status'] == 401 for check in checks.values()))
        self.assertNotIn('wrong-secret-value', json.dumps(report))
        self.assertFalse((self.config.DATA_DIR / 'model_settings.json').exists())

    def test_bad_structured_and_invalid_embedding_are_failures(self):
        self.server.mode = 'bad_json'
        report = probe_models(self.config, role='generation', timeout_seconds=3)
        self.assertTrue(report['roles']['generation']['inference'])
        self.assertFalse(report['ok'])
        for mode in ('index', 'extra', 'zero', 'nan'):
            with self.subTest(mode=mode):
                self.server.mode = mode
                self.assertFalse(probe_models(self.config, role='embedding', timeout_seconds=3)['ok'])

    def test_generic_probe_allows_normal_longer_output_but_rejects_truncation(self):
        self.server.mode = 'long_generation'
        report = probe_models(self.config, role='generation', timeout_seconds=3)
        self.assertTrue(report['ok'], report)
        row = report['roles']['generation']
        self.assertEqual(row['probe_scope'], 'short_messages')
        self.assertFalse(row['configured_context_window_tested'])
        self.assertEqual(row['output_token_budgets'], {'chat': 256, 'structured': 256})
        calls = [body for path, body, _ in self.server.seen if path == '/v1/chat/completions']
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(body['max_tokens'] == 256 for body in calls))
        self.server.mode = 'truncated'
        report = probe_models(self.config, role='generation', timeout_seconds=3)
        self.assertFalse(report['ok'])
        self.assertFalse(report['roles']['generation']['inference'])
        self.assertFalse(report['roles']['generation']['capability']['structured_output'])

    def test_structured_probe_matches_application_parser_and_rejects_nonfinite(self):
        self.server.mode = 'wrapped_json'
        report = probe_models(self.config, role='generation', timeout_seconds=3)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['roles']['generation']['capability']['structured_parser'], 'shared_json_object_parser')
        self.server.mode = 'nonfinite_json'
        report = probe_models(self.config, role='generation', timeout_seconds=3)
        self.assertFalse(report['ok'])
        self.assertTrue(report['roles']['generation']['inference'])
        self.assertFalse(report['roles']['generation']['capability']['structured_output'])

    def test_native_ollama_and_diagnostic_deadline(self):
        report = probe_models(self.config, payload={'generation': {'backend': 'ollama', 'base_url': self.url, 'model': 'local-model'}}, role='generation', timeout_seconds=3)
        self.assertTrue(report['ok'], report)
        native = [r[1] for r in self.server.seen if r[0] == '/api/chat']
        self.assertEqual(len(native), 2)
        self.assertTrue(all(body['think'] is False for body in native))
        self.assertEqual(native[1]['format'], 'json')
        self.assertEqual(report['roles']['generation']['output_token_budgets'], {'chat': 32, 'structured': 64})
        self.server.mode = 'slow'
        started = time.monotonic()
        report = probe_models(self.config, role='generation', timeout_seconds=1)
        self.assertLess(time.monotonic() - started, 1.25)
        self.assertFalse(report['ok'])

    def test_redirect_never_forwarded(self):
        self.server.mode = 'redirect'
        report = probe_models(self.config, role='generation', timeout_seconds=3)
        self.assertFalse(report['roles']['generation']['checks']['model_list']['ok'])
        self.assertFalse(any(path == '/leak' for path, _, _ in self.server.seen))
        self.assertNotIn('private-secret-value', json.dumps(report))

    def test_admin_routes_and_csrf_guard(self):
        app = SJFXFastAPI()
        def session():
            role = request.headers.get('test-role')
            return {'role': role, 'must_change_pw': request.headers.get('test-change-pw') == '1'} if role else None
        register_model_settings_routes(app, config=self.config, session_resolver=session)
        client = TestClient(app)
        path = '/api/admin/model-settings'
        self.assertEqual(client.get(path).status_code, 401)
        self.assertEqual(client.get(path, headers={'test-role': 'user'}).status_code, 403)
        self.assertEqual(client.get(path, headers={'X-SJFX-Token': 'any-token'}).status_code, 401)
        self.assertEqual(client.get(path, headers={'test-role': 'admin', 'test-change-pw': '1'}).status_code, 403)
        self.assertEqual(client.put(path, headers={'test-role': 'admin'}, json={}).status_code, 403)
        headers = {'test-role': 'admin', 'X-SJFX-Model-Settings': '1'}
        self.assertEqual(client.get(path, headers=headers).json()['model_interface_version'], 2)
        self.assertEqual(client.put(path, headers=dict(headers, Origin='https://attacker.invalid'), json={}).status_code, 403)
        self.assertEqual(client.put(path, headers=dict(headers, Origin='http://testserver/path'), json={}).status_code, 403)
        self.assertEqual(client.post('/api/admin/model-test', headers={'test-role': 'user', 'X-SJFX-Model-Settings': '1'}, json={}).status_code, 403)
        self.assertEqual(client.post('/api/admin/model-test', headers={'X-SJFX-Token': 'any-token', 'X-SJFX-Model-Settings': '1'}, json={}).status_code, 401)
        for body in ({'role': []}, {'timeout_seconds': True}, {'settings': []}):
            self.assertEqual(client.post('/api/admin/model-test', headers=headers, json=body).status_code, 400)
        response = client.put(path, headers=headers, json={'generation': {'model': 'local-model'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['model_interface_version'], 2)
        self.assertNotIn('private-secret-value', response.text)
        response = client.post('/api/admin/model-test', headers=headers, json={'role': 'generation', 'timeout_seconds': 3})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])

    def test_cli_config_and_real_model_test_with_restart_import(self):
        root = Path(__file__).resolve().parents[1]
        env_file = self.config.DATA_DIR / 'instance.env'
        env_file.write_text('\n'.join([
            'SJFX_STATE_DIR=' + str(self.config.DATA_DIR), 'LLM_BACKEND=ollama',
            'SJFX_EMBED_BACKEND=openai_compatible', 'SJFX_EMBED_BASE_URL=' + self.url,
            'SJFX_EMBED_MODEL=embed', 'SJFX_EMBED_DIMENSION=3', 'ENABLE_OPENAI_COMPATIBLE=1',
        ]), encoding='utf-8')
        command = [sys.executable, '-X', 'utf8', str(root / 'sjfxctl.py'), '--env-file', str(env_file)]
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith(('SJFX_', 'LLM_', 'OLLAMA_', 'VLLM_', 'OPENAI_COMPATIBLE_'))}
        def cli(args, stdin=None):
            return subprocess.run(command + args, input=stdin, text=True, encoding='utf-8', capture_output=True,
                                  env=clean_env, timeout=10, cwd=root)
        saved = cli(['config', 'set', '--role', 'generation', '--backend', 'openai_compatible',
            '--base-url', self.url, '--model', 'local-model', '--enabled', 'true', '--api-key-stdin'], 'cli-private-secret\n')
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        self.assertNotIn('cli-private-secret', saved.stdout + saved.stderr)
        read = cli(['config', 'get'])
        self.assertEqual(read.returncode, 0, read.stdout)
        report = json.loads(read.stdout)
        self.assertEqual(report['active']['generation']['backend'], 'openai_compatible')
        self.assertFalse(report['restart_required'])
        tested = cli(['model-test', '--timeout', '3'])
        self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
        self.assertTrue(json.loads(tested.stdout)['ok'])
        self.assertNotIn('cli-private-secret', tested.stdout + tested.stderr)


if __name__ == '__main__':
    unittest.main()
