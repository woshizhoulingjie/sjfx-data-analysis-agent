"""Bounded inference probes for explicitly selected model services."""
from __future__ import annotations

import json
import math
import queue
import socket
import threading
import time
import urllib.error
import urllib.request

from services.model_contract import LocalModelClient, LocalModelError, open_local_request, validate_model_url
from services.model_settings import model_snapshot, patch_environment, read_model_overrides
from services.embedding_backend import validate_vectors

_MAX_BYTES = 1024 * 1024


def _native_root(url):
    return url[:-3] if url.endswith('/v1') else url


def _request(url, secret, deadline, stop, payload=None, *, allowed_endpoints=()):
    if stop.is_set() or time.monotonic() >= deadline:
        raise LocalModelError('诊断达到总时限', code='timeout')
    validate_model_url(url, allowed_endpoints)
    headers = {'Accept': 'application/json'}
    if secret:
        headers['Authorization'] = 'Bearer ' + secret
    body = None if payload is None else json.dumps(payload).encode('utf-8')
    if body is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=body, headers=headers)
    remaining = max(.05, deadline - time.monotonic())
    try:
        with open_local_request(request, remaining, allowed_endpoints=allowed_endpoints) as response:
            chunks, count = [], 0
            while True:
                if stop.is_set() or time.monotonic() >= deadline:
                    raise LocalModelError('诊断达到总时限', code='timeout')
                chunk = response.read1(min(65536, _MAX_BYTES + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > _MAX_BYTES:
                    raise LocalModelError('诊断响应超出大小限制', code='invalid_response')
            value = json.loads(b''.join(chunks))
            if not isinstance(value, dict):
                raise ValueError
            return value
    except urllib.error.HTTPError as exc:
        # Neither error body nor exception URL is ever reported.
        status = exc.code
        exc.close()
        code = 'authentication_error' if status in {401, 403} else 'model_unavailable' if status == 404 else 'request_error'
        raise LocalModelError('模型服务返回 HTTP {}'.format(status), code=code, status=status) from None
    except (TimeoutError, socket.timeout):
        raise LocalModelError('诊断达到请求时限', code='timeout') from None
    except urllib.error.URLError as exc:
        code = 'timeout' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'unavailable'
        raise LocalModelError('模型服务连接失败', code=code) from None
    except (ValueError, UnicodeError, TypeError):
        raise LocalModelError('模型服务返回无效 JSON', code='invalid_response') from None


def _probe(row, role, deadline, stop):
    report = {'backend': row['backend'], 'model': row['model'], 'reachable': False,
              'configured_model_present': None, 'inference': False,
              'capability': {}, 'checks': {}, 'ok': False}
    if row['backend'] == 'disabled' or role == 'generation' and not row['enabled']:
        report.update(enabled=False, code='disabled')
        return report
    report['enabled'] = True
    allowed = row.get('allowed_endpoints', ())
    base = validate_model_url(row['base_url'], allowed)
    native = row['backend'] == 'ollama'
    root = _native_root(base) if native else base

    def call(name, path, payload=None):
        try:
            data = _request(root + path, row.get('api_key', ''), deadline, stop, payload, allowed_endpoints=allowed)
            report['reachable'] = True
            report['checks'][name] = {'ok': True}
            return data
        except LocalModelError as exc:
            if exc.status is not None:
                report['reachable'] = True
            report['checks'][name] = {'ok': False, 'code': exc.code, 'message': str(exc)}
            if exc.status is not None:
                report['checks'][name]['http_status'] = exc.status
            return None

    data = call('model_list', '/api/tags' if native else '/models')
    if data is not None:
        items = data.get('models' if native else 'data')
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            names = {name for item in items for name in [item.get('name', item.get('model', '')) if native else item.get('id', '')] if isinstance(name, str)}
            target = row['model']
            report['configured_model_present'] = target in names or native and ':' not in target and target + ':latest' in names
        else:
            report['checks']['model_list'] = {'ok': False, 'code': 'invalid_response'}

    if role == 'generation':
        report.update(probe_scope='short_messages', configured_context_window_tested=False,
                      output_token_budgets={'chat': 32 if native else 256,
                                            'structured': 64 if native else 256})
        common = {'model': row['model'], 'stream': False,
                  'messages': [{'role': 'user', 'content': 'Reply with OK.'}]}
        if native:
            common['think'] = False
            common['options'] = {'temperature': 0, 'num_predict': 32, 'num_ctx': min(row['context_tokens'], 8192)}
        else:
            common.update(temperature=0, max_tokens=256)
            if row['backend'] == 'vllm':
                common['chat_template_kwargs'] = {'enable_thinking': False}
        chat = call('chat', '/api/chat' if native else '/chat/completions', common)

        def content(data):
            if not isinstance(data, dict):
                return None
            try:
                returned_model = data.get('model')
                aliases = {None, row['model']}
                if native and ':' not in row['model']:
                    aliases.add(row['model'] + ':latest')
                if returned_model not in aliases:
                    return None
                if native:
                    if data.get('done') is not True or data.get('done_reason') == 'length':
                        return None
                    value = data['message']['content']
                else:
                    choice = data['choices'][0]
                    if choice.get('finish_reason') == 'length':
                        return None
                    value = choice['message']['content']
                return value.strip() if isinstance(value, str) and value.strip() else None
            except (KeyError, IndexError, TypeError):
                return None

        report['inference'] = content(chat) is not None
        if chat is not None and not report['inference']:
            report['checks']['chat'] = {'ok': False, 'code': 'invalid_response'}
        structured_payload = dict(common)
        structured_payload['messages'] = [{'role': 'system', 'content': 'Output only a valid JSON object.'},
                                          {'role': 'user', 'content': 'Return exactly {"ok": true}.'}]
        if native:
            structured_payload['format'] = 'json'
            structured_payload['options'] = dict(common['options'], num_predict=64)
        else:
            structured_payload['max_tokens'] = 256
            if row['supports_json_response_format']:
                structured_payload['response_format'] = {'type': 'json_object'}
        structured = call('structured', '/api/chat' if native else '/chat/completions', structured_payload)
        try:
            obj = LocalModelClient._parse_json_content(content(structured) or '',
                required_fields=['ok'], context='模型诊断输出')
            valid = isinstance(obj, dict) and obj.get('ok') is True
        except (ValueError, LocalModelError):
            valid = False
        if structured is not None and not valid:
            report['checks']['structured'] = {'ok': False, 'code': 'invalid_structured_output'}
        report['capability'] = {'chat': report['inference'], 'structured_output': valid,
            'json_response_format_declared': row['supports_json_response_format'],
            'structured_parser': 'shared_json_object_parser',
            'structured_test_mode': 'native_json' if native else 'response_format' if row['supports_json_response_format'] else 'prompt_json'}
        report['ok'] = report['inference'] and valid
    else:
        payload = {'model': row['model'], 'input': ['SJFX local embedding diagnostic.']}
        data = call('embedding', '/api/embed' if native else '/embeddings', payload)
        vector = None
        try:
            if not isinstance(data, dict) or data.get('model') not in ({None, row['model'], row['model'] + ':latest'} if native else {None, row['model']}):
                raise ValueError
            if native:
                vectors = data['embeddings']
            else:
                items = data['data']
                if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or type(items[0].get('index')) is not int or items[0]['index'] != 0:
                    raise ValueError
                vectors = [items[0]['embedding']]
            vector = validate_vectors(vectors, count=1, dimension=row['dimension'])[0]
            if len(vector) > 65536:
                raise ValueError
            report['inference'] = True
        except (KeyError, IndexError, TypeError, ValueError, LocalModelError):
            if data is not None:
                report['checks']['embedding'] = {'ok': False, 'code': 'invalid_embedding_response'}
        report['capability'] = {'embedding': report['inference'], 'observed_dimension': len(vector) if report['inference'] else None,
                                'configured_dimension': row['dimension']}
        report['ok'] = report['inference']
    # A model-list miss remains visible; actual inference is required for success.
    if report['configured_model_present'] is False:
        report['ok'] = False
    return report


def test_models(config, *, payload=None, role='all', timeout_seconds=20):
    if not isinstance(role, str) or role not in {'all', 'generation', 'embedding'}:
        raise ValueError('诊断角色必须为 generation、embedding 或 all')
    try:
        if isinstance(timeout_seconds, bool):
            raise ValueError
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or not 1 <= timeout <= 60:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError('诊断总时限必须为 1–60 秒') from None
    env = read_model_overrides(config.DATA_DIR)
    if payload is not None:
        env = patch_environment(config, payload, env)
    rows = model_snapshot(config, env, include_secrets=True)
    roles = ['generation', 'embedding'] if role == 'all' else [role]
    started = time.monotonic()
    deadline = started + timeout
    stop = threading.Event()
    result_queue = queue.Queue()

    def run():
        for name in roles:
            if stop.is_set() or time.monotonic() >= deadline:
                break
            try:
                result_queue.put((name, _probe(rows[name], name, deadline, stop)))
            except Exception:
                # Keep arbitrary service content and credential-bearing exceptions private.
                result_queue.put((name, {'ok': False, 'code': 'diagnostic_error'}))

    thread = threading.Thread(target=run, name='sjfx-model-diagnostic', daemon=True)
    thread.start()
    thread.join(timeout)
    stop.set()
    results = {}
    while not result_queue.empty():
        name, result = result_queue.get_nowait()
        results[name] = result
    for name in roles:
        results.setdefault(name, {'ok': False, 'code': 'timeout', 'message': '诊断达到总时限'})
    return {'ok': all(item.get('ok') for item in results.values()), 'roles': results,
            'elapsed_seconds': round(time.monotonic() - started, 3), 'timeout_seconds': timeout,
            'tested_configuration': 'draft' if payload is not None else 'saved',
        'notice': '向明确选择的模型服务发送短测试内容，生成测试使用报告中的有限输出预算；测试不保存配置，也未验证所配置的完整上下文窗口。模型列表可达不等于推理可用；选择内网或服务器时，内容会发送到该地址。'}


def model_inventory(config):
    """Cheap active-service inventory used by status/doctor, never readiness."""
    rows = model_snapshot(config, include_secrets=True)
    if getattr(config, 'MAIL_FEATURE_ENABLED', False) and getattr(config, 'MAIL_ENABLE_VECTOR_INDEX', False):
        rows['mail_embedding'] = dict(rows['embedding'],
            base_url=getattr(config, 'MAIL_EMBED_BASE_URL', rows['embedding']['base_url']),
            model=getattr(config, 'MAIL_EMBED_MODEL', rows['embedding']['model']))
    result = {}
    for name in ('generation', 'embedding', 'mail_embedding'):
        row = rows.get(name)
        if row is None or row['backend'] == 'disabled' or name == 'generation' and not row['enabled']:
            result[name] = {'enabled': False, 'inference_tested': False}
            continue
        item = {'enabled': True, 'reachable': False, 'configured_model_present': None,
                'inference_tested': False, 'scope': 'model_list_only', 'backend': row['backend']}
        try:
            base = _native_root(row['base_url']) if row['backend'] == 'ollama' else row['base_url']
            native = row['backend'] == 'ollama'
            body = _request(base + ('/api/tags' if native else '/models'), row.get('api_key', ''),
                            time.monotonic() + 2, threading.Event(), allowed_endpoints=row.get('allowed_endpoints', ()))
            item['reachable'] = True
            entries = body.get('models' if native else 'data')
            if isinstance(entries, list):
                names = {r.get('name', r.get('model', '')) if native else r.get('id', '') for r in entries if isinstance(r, dict)}
                item['configured_model_present'] = row['model'] in names or native and ':' not in row['model'] and row['model'] + ':latest' in names
        except LocalModelError as exc:
            item.update(reachable=exc.status is not None, code=exc.code)
        except Exception:
            item['code'] = 'diagnostic_error'
        result[name] = item
    return result
