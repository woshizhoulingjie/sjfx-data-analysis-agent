"""Instance-scoped model settings. Public snapshots never contain credentials.

Saved settings deliberately take precedence over environment model settings on
the next process start. No running client is mutated by saving this file.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import uuid

from urllib.parse import urlsplit
from services.model_contract import normalize_allowed_endpoints, validate_model_url, validate_model_name

_LOCK = threading.RLock()
_GEN = {
    'vllm': ('VLLM_BASE_URL', 'VLLM_MODEL', 'VLLM_API_KEY', 'VLLM_REQUEST_TIMEOUT', 'ENABLE_VLLM'),
    'ollama': ('OLLAMA_BASE_URL', 'OLLAMA_MODEL', 'OLLAMA_API_KEY', 'SHARED_OLLAMA_REQUEST_TIMEOUT', 'ENABLE_SHARED_OLLAMA'),
    'openai_compatible': ('OPENAI_COMPATIBLE_BASE_URL', 'OPENAI_COMPATIBLE_MODEL', 'OPENAI_COMPATIBLE_API_KEY', 'OPENAI_COMPATIBLE_REQUEST_TIMEOUT', 'ENABLE_OPENAI_COMPATIBLE'),
}
_EMBED = {'base_url': 'SJFX_EMBED_BASE_URL', 'model': 'SJFX_EMBED_MODEL',
          'backend': 'SJFX_EMBED_BACKEND', 'api_key': 'SJFX_EMBED_API_KEY',
          'dimension': 'SJFX_EMBED_DIMENSION', 'version': 'SJFX_EMBED_VERSION',
          'weights_id': 'SJFX_EMBED_WEIGHTS_ID', 'config_id': 'SJFX_EMBED_CONFIG_ID',
          'encoding': 'SJFX_EMBED_ENCODING', 'tokenizer_path': 'SJFX_EMBED_TOKENIZER_PATH',
          'timeout_seconds': 'VLLM_EMBED_TIMEOUT_SECONDS', 'concurrency': 'SJFX_LP_EMBED_CONCURRENCY'}
ALLOWED_ENV = set(sum((list(v) for v in _GEN.values()), [])) | set(_EMBED.values()) | {
    'LLM_BACKEND', 'LLM_CONTEXT_TOKENS', 'LLM_MAX_CONCURRENCY',
    'OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT', 'SJFX_MODEL_ALLOWED_ENDPOINTS',
    'MAIL_EMBED_BASE_URL', 'MAIL_EMBED_MODEL', 'SJFX_LP_EMBED_BASE_URL', 'SJFX_LP_EMBED_FALLBACK_URL',
    'LLM_TOKENIZER_PATH', 'CONVERSATION_TOKENIZER_PATH', 'LLM_TOKENIZER_VERSION', 'LLM_MODEL_VERSION'}
SECRET_ENV = {'VLLM_API_KEY', 'OLLAMA_API_KEY', 'OPENAI_COMPATIBLE_API_KEY', 'SJFX_EMBED_API_KEY'}
_INTEGER_RANGES = {'LLM_CONTEXT_TOKENS': (2048, 1048576), 'LLM_MAX_CONCURRENCY': (1, 8),
    'SJFX_LP_EMBED_CONCURRENCY': (1, 4), 'VLLM_EMBED_TIMEOUT_SECONDS': (2, 60),
    'SJFX_EMBED_DIMENSION': (0, 65536), 'VLLM_REQUEST_TIMEOUT': (60, 3600),
    'SHARED_OLLAMA_REQUEST_TIMEOUT': (60, 3600), 'OPENAI_COMPATIBLE_REQUEST_TIMEOUT': (60, 3600)}


def settings_path(state_dir):
    return Path(state_dir) / 'model_settings.json'


def read_model_overrides(state_dir):
    path = settings_path(state_dir)
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 65536:
            raise ValueError
        value = json.loads(path.read_text(encoding='utf-8'))
        env = value['env_overrides']
        if value.get('version') != 1 or not isinstance(env, dict):
            raise ValueError
        if set(env) - ALLOWED_ENV or any(not isinstance(v, str) for v in env.values()):
            raise ValueError
        # Validate persisted files too, without ever including their content in an error.
        _validate_env(env)
        return env
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        raise ValueError('持久模型配置无效，请修复该实例的 model_settings.json') from None


def apply_model_settings_environment(state_dir):
    os.environ.update(read_model_overrides(state_dir))


def _number(value, low, high, field):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or number != int(number) or not low <= number <= high:
            raise ValueError
        return int(number)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('{} 必须是 {}–{} 的整数'.format(field, low, high)) from None


def _boolean(value, field):
    if not isinstance(value, bool):
        raise ValueError(field + ' 必须是布尔值')
    return '1' if value else '0'


def _credential(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('认证凭据格式无效')
    return value.strip()


def _validate_env(env, allowed_endpoints=None):
    allowed = normalize_allowed_endpoints(env.get('SJFX_MODEL_ALLOWED_ENDPOINTS', ()) if allowed_endpoints is None else allowed_endpoints)
    for key, value in env.items():
        if key in _INTEGER_RANGES:
            low, high = _INTEGER_RANGES[key]
            if not value.isascii() or not value.isdigit():
                raise ValueError('模型数值配置必须为十进制整数')
            _number(value, low, high, key)
            continue
        if key in SECRET_ENV:
            _credential(value)
        elif key == 'SJFX_MODEL_ALLOWED_ENDPOINTS':
            normalize_allowed_endpoints(value)
        elif key.endswith('BASE_URL'):
            if len(value) > 8192:
                raise ValueError('模型地址过长')
            validate_model_url(value, allowed)
        elif key == 'SJFX_LP_EMBED_FALLBACK_URL' and value:
            validate_model_url(value, allowed)
        elif key.endswith('_MODEL'):
            if len(value) > 512:
                raise ValueError('模型名称过长')
            validate_model_name(value)
        elif key == 'LLM_BACKEND' and value not in _GEN:
            raise ValueError('不支持的生成后端')
        elif key == 'SJFX_EMBED_BACKEND' and value not in {'openai_compatible', 'ollama', 'disabled'}:
            raise ValueError('不支持的嵌入后端')
        elif key == 'SJFX_EMBED_ENCODING' and value not in {'e5', 'plain'}:
            raise ValueError('向量编码方式必须为 e5 或 plain')
        elif key in {'LLM_CONTEXT_TOKENS'}:
            _number(value, 2048, 1048576, key)
        elif key in {'LLM_MAX_CONCURRENCY'}:
            _number(value, 1, 8, key)
        elif key == 'SJFX_LP_EMBED_CONCURRENCY':
            _number(value, 1, 4, key)
        elif key == 'VLLM_EMBED_TIMEOUT_SECONDS':
            _number(value, 2, 60, key)
        elif key.endswith('REQUEST_TIMEOUT'):
            _number(value, 60, 3600, key)
        elif key == 'SJFX_EMBED_DIMENSION':
            _number(value, 0, 65536, key)
        elif key.startswith('ENABLE_') or key.endswith('SUPPORTS_JSON_RESPONSE_FORMAT'):
            if value not in {'0', '1'}:
                raise ValueError('无效能力开关')
        elif len(value) > 512 or any(ord(c) < 32 for c in value):
            raise ValueError('无效模型标识')


def _get(config, env, key, default=None):
    if key in env:
        return env[key]
    return getattr(config, key, getattr(config, key.removeprefix('SJFX_'), default))


def _true(value):
    return str(value).lower() in {'true', '1', 'yes', 'on'}


def model_allowed_endpoints(config, env=None):
    return normalize_allowed_endpoints((env or {}).get('SJFX_MODEL_ALLOWED_ENDPOINTS',
        getattr(config, 'MODEL_ALLOWED_ENDPOINTS', ())))


def _origin(url):
    parsed = urlsplit(str(url))
    return normalize_allowed_endpoints((parsed.scheme + '://' + parsed.netloc,))[0]


def _location(url):
    return 'SJFX 所在机器' if urlsplit(str(url)).hostname in {'127.0.0.1', 'localhost', '::1'} else '指定内网或服务器'


def _tokenizer_path(config, env):
    for key in ('LLM_TOKENIZER_PATH', 'CONVERSATION_TOKENIZER_PATH'):
        if key in env:
            return env[key]
    return getattr(config, 'LLM_TOKENIZER_PATH') if hasattr(config, 'LLM_TOKENIZER_PATH') else getattr(config, 'CONVERSATION_TOKENIZER_PATH', '')


def model_snapshot(config, env=None, *, include_secrets=False):
    env = env or {}
    allowed = model_allowed_endpoints(config, env)
    backend = str(_get(config, env, 'LLM_BACKEND', 'vllm'))
    url, model, secret, timeout, enabled = _GEN[backend]
    generation = {
        'backend': backend, 'base_url': _get(config, env, url, 'http://127.0.0.1:8001/v1'),
        'model': _get(config, env, model, 'local-model'),
        'context_tokens': int(_get(config, env, 'LLM_CONTEXT_TOKENS', 4096)),
        'timeout_seconds': int(_get(config, env, timeout, 600)),
        'concurrency': int(_get(config, env, 'LLM_MAX_CONCURRENCY', 1)),
        'enabled': _true(_get(config, env, enabled, False)),
        'supports_json_response_format': backend == 'vllm' or _true(_get(config, env, 'OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT', False)) if backend != 'ollama' else True,
        'tokenizer_path': str(_tokenizer_path(config, env) or ''),
        'tokenizer_version': _get(config, env, 'LLM_TOKENIZER_VERSION', ''),
        'model_version': _get(config, env, 'LLM_MODEL_VERSION', ''),
    }
    embed_backend = str(_get(config, env, 'SJFX_EMBED_BACKEND',
        'openai_compatible' if getattr(config, 'ENABLE_VLLM_EMBEDDINGS', False) else
        'ollama' if getattr(config, 'ENABLE_SHARED_OLLAMA_EMBEDDINGS', False) else 'disabled'))
    if embed_backend == 'vllm':
        embed_backend = 'openai_compatible'
    embedding = {'backend': embed_backend,
        'base_url': _get(config, env, 'SJFX_EMBED_BASE_URL', getattr(config, 'VLLM_EMBED_BASE_URL', 'http://127.0.0.1:8002/v1')),
        'model': _get(config, env, 'SJFX_EMBED_MODEL', getattr(config, 'VLLM_EMBED_MODEL', 'multilingual-e5-small')),
        'timeout_seconds': int(_get(config, env, 'VLLM_EMBED_TIMEOUT_SECONDS', 12)),
        'concurrency': int(_get(config, env, 'SJFX_LP_EMBED_CONCURRENCY', 2)),
        'dimension': int(_get(config, env, 'SJFX_EMBED_DIMENSION', 0) or 0),
        'version': _get(config, env, 'SJFX_EMBED_VERSION', ''),
        'weights_id': _get(config, env, 'SJFX_EMBED_WEIGHTS_ID', ''),
        'config_id': _get(config, env, 'SJFX_EMBED_CONFIG_ID', ''),
        'encoding': _get(config, env, 'SJFX_EMBED_ENCODING', 'e5' if 'e5' in str(_get(config, env, 'SJFX_EMBED_MODEL', getattr(config, 'VLLM_EMBED_MODEL', 'multilingual-e5-small'))).lower() else 'plain'),
        'tokenizer_path': _get(config, env, 'SJFX_EMBED_TOKENIZER_PATH', ''),
    }
    for row, key in ((generation, secret), (embedding, 'SJFX_EMBED_API_KEY')):
        row['base_url'] = validate_model_url(row['base_url'], allowed)
        value = _get(config, env, key, getattr(config, 'OLLAMA_API_KEY' if embed_backend == 'ollama' else 'VLLM_API_KEY', '') if row is embedding else '')
        row['api_key_configured'] = bool(value)
        row['service_location'] = _location(row['base_url'])
        if include_secrets:
            row['api_key'] = value
            row['allowed_endpoints'] = allowed
    from services.model_budget import token_count_method
    budget_config = SimpleNamespace(LLM_BACKEND=backend, LLM_MODEL=generation['model'],
        LLM_BASE_URL=generation['base_url'], LLM_MODEL_VERSION=generation['model_version'],
        LLM_TOKENIZER_PATH=generation['tokenizer_path'], LLM_TOKENIZER_VERSION=generation['tokenizer_version'])
    generation['token_counting'] = token_count_method(budget_config)
    generation['tokenizer_status'] = 'loaded' if generation['token_counting'] == 'local_tokenizer' else 'unavailable' if generation['tokenizer_path'] else 'not_configured'
    # The running factory supports these legacy lane endpoint overrides. Show
    # them until an explicit shared embedding save replaces them.
    large_url = env.get('SJFX_LP_EMBED_BASE_URL', getattr(config, 'SJFX_LP_EMBED_BASE_URL', '') or
        getattr(config, 'LP_EMBED_BASE_URL', '') or os.getenv('SJFX_LP_EMBED_BASE_URL', '').strip() or embedding['base_url'])
    mail_url = env.get('MAIL_EMBED_BASE_URL', getattr(config, 'MAIL_EMBED_BASE_URL', embedding['base_url']))
    mail_model = env.get('MAIL_EMBED_MODEL', getattr(config, 'MAIL_EMBED_MODEL', embedding['model']))
    lanes = {'ordinary': {'base_url': embedding['base_url'], 'model': embedding['model']},
             'large': {'base_url': validate_model_url(large_url, allowed), 'model': embedding['model']},
             'mail': {'base_url': validate_model_url(mail_url, allowed), 'model': mail_model}}
    embedding['lanes'] = lanes
    embedding['large_fallback_base_url'] = _get(config, env, 'SJFX_LP_EMBED_FALLBACK_URL', '') or ''
    if embedding['large_fallback_base_url']:
        embedding['large_fallback_base_url'] = validate_model_url(embedding['large_fallback_base_url'], allowed)
    embedding['lane_overrides'] = any(lane != lanes['ordinary'] for lane in lanes.values()) or bool(embedding['large_fallback_base_url'])
    return {'generation': generation, 'embedding': embedding}


def patch_environment(config, payload, current=None):
    if not isinstance(payload, dict) or set(payload) - {'generation', 'embedding'}:
        raise ValueError('配置只接受 generation 与 embedding 字段')
    result = dict(current or {})
    allowed = model_allowed_endpoints(config, result)
    if allowed:
        result['SJFX_MODEL_ALLOWED_ENDPOINTS'] = json.dumps(allowed)
    for role, patch in payload.items():
        if not isinstance(patch, dict):
            raise ValueError('模型配置必须是对象')
        patch = dict(patch)
        if 'api_key' in patch:
            # Normalize before deciding whether a new service has received a
            # replacement. Whitespace is an empty write, never authorization
            # to forward the previous service's secret.
            patch['api_key'] = _credential(patch['api_key'])
        for name in ('model', 'tokenizer_path'):
            if isinstance(patch.get(name), str):
                patch[name] = patch[name].strip()
        snapshot = model_snapshot(config, result)[role]
        if role == 'generation':
            fields = {'backend', 'base_url', 'model', 'context_tokens', 'timeout_seconds', 'concurrency', 'enabled', 'api_key', 'clear_api_key', 'supports_json_response_format', 'tokenizer_path', 'tokenizer_version', 'model_version'}
            backend = patch.get('backend', snapshot['backend'])
            if not isinstance(backend, str) or backend not in _GEN:
                raise ValueError('不支持的生成后端')
            keys = _GEN[backend]
            mapping = dict(zip(('base_url', 'model', 'api_key', 'timeout_seconds', 'enabled'), keys))
            mapping.update(backend='LLM_BACKEND', context_tokens='LLM_CONTEXT_TOKENS', concurrency='LLM_MAX_CONCURRENCY', supports_json_response_format='OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT', tokenizer_path='LLM_TOKENIZER_PATH', tokenizer_version='LLM_TOKENIZER_VERSION', model_version='LLM_MODEL_VERSION')
        else:
            fields = set(_EMBED) | {'clear_api_key'}
            mapping = _EMBED
            if not isinstance(patch.get('backend', snapshot['backend']), str) or patch.get('backend', snapshot['backend']) not in {'openai_compatible', 'ollama', 'disabled'}:
                raise ValueError('不支持的嵌入后端')
        if set(patch) - fields:
            raise ValueError('模型配置包含未知字段')
        backend = patch.get('backend', snapshot['backend'])
        changed_backend = backend != snapshot['backend']
        if changed_backend and backend != 'disabled' and not all(isinstance(patch.get(name), str) and patch[name].strip() for name in ('base_url', 'model')):
            raise ValueError('切换服务类型时请明确填写服务地址和模型名称')
        # Only an explicit administrator URL authorizes a remote origin. The
        # caller gets a private copy; diagnostics never change process policy.
        if 'base_url' in patch:
            if not isinstance(patch['base_url'], str):
                raise ValueError('模型地址必须为字符串')
            allowed = model_allowed_endpoints(config, result)
            origin = _origin(patch['base_url'])
            allowed = tuple(dict.fromkeys((*allowed, origin)))
            normalized = validate_model_url(patch['base_url'], allowed)
            patch = dict(patch, base_url=normalized)
            if urlsplit(normalized).hostname not in {'127.0.0.1', 'localhost', '::1'}:
                result['SJFX_MODEL_ALLOWED_ENDPOINTS'] = json.dumps(allowed)
        changed_endpoint = 'base_url' in patch and patch['base_url'] != snapshot['base_url']
        if (changed_backend or changed_endpoint) and not patch.get('api_key'):
            result[mapping['api_key']] = ''
        if role == 'generation' and (changed_backend or 'base_url' in patch and patch['base_url'] != snapshot['base_url'] or 'model' in patch and patch['model'] != snapshot['model']):
            # Empty overrides intentionally suppress a tokenizer from .env.
            result['CONVERSATION_TOKENIZER_PATH'] = ''
            if not patch.get('tokenizer_path'):
                result['LLM_TOKENIZER_PATH'] = ''
                result['LLM_TOKENIZER_VERSION'] = ''
            if 'model_version' not in patch:
                result['LLM_MODEL_VERSION'] = ''
        if role == 'embedding' and (changed_backend or 'base_url' in patch and patch['base_url'] != snapshot['base_url'] or 'model' in patch and patch['model'] != snapshot['model']):
            for name, default in (('tokenizer_path', ''), ('dimension', '0'), ('version', ''), ('weights_id', ''), ('config_id', ''), ('encoding', 'plain')):
                if name not in patch:
                    result[_EMBED[name]] = default
        if patch.get('clear_api_key') and patch.get('api_key'):
            raise ValueError('不能同时替换并清除认证凭据')
        for field, value in patch.items():
            if field == 'clear_api_key':
                if value is not True and value is not False:
                    raise ValueError('clear_api_key 必须是布尔值')
                if value:
                    result[mapping['api_key']] = ''
                continue
            if field == 'api_key':
                value = _credential(value)
                if not value:
                    continue  # empty write-only input preserves the configured secret
            elif field in {'enabled', 'supports_json_response_format'}:
                value = _boolean(value, field)
            elif not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError('模型配置字段类型无效')
            result[mapping[field]] = str(value).strip()
            key = mapping[field]
            if key in _INTEGER_RANGES:
                low, high = _INTEGER_RANGES[key]
                result[key] = str(_number(value, low, high, field))
        if role == 'generation' and 'tokenizer_path' in patch:
            result['CONVERSATION_TOKENIZER_PATH'] = ''
        if role == 'embedding':
            # A settings-page embedding edit applies to all three consumers.
            # It must never silently retain an old mail/large endpoint.
            shared = model_snapshot(config, result)['embedding']
            result['MAIL_EMBED_BASE_URL'] = shared['base_url']
            result['MAIL_EMBED_MODEL'] = shared['model']
            result['SJFX_LP_EMBED_BASE_URL'] = shared['base_url']
            result['SJFX_LP_EMBED_FALLBACK_URL'] = ''
    _validate_env(result, model_allowed_endpoints(config, result))
    return result


def get_model_settings(config):
    current = read_model_overrides(config.DATA_DIR)
    active = model_snapshot(config)
    pending = model_snapshot(config, current)
    # Compare credentials only through the private snapshots, never in response.
    restart_required = model_snapshot(config, include_secrets=True) != model_snapshot(config, current, include_secrets=True)
    return {'ok': True, 'model_interface_version': 2, 'active': active, 'saved': pending,
            'restart_required': restart_required, 'apply_mode': 'restart_all_roles',
            'notice': '保存后请重启 SJFX 的 Web 与全部 Worker；模型服务需已启动。向量设置统一用于普通检索、大数据包和邮件，身份改变后需重建对应向量索引。指定内网或服务器时，业务内容会发送到该服务。'}


def save_model_settings(config, payload):
    with _LOCK:
        env = patch_environment(config, payload, read_model_overrides(config.DATA_DIR))
        document = json.dumps({'version': 1, 'env_overrides': env}, ensure_ascii=False, indent=2)
        if len(document.encode('utf-8')) > 65536:
            raise ValueError('模型配置超过实例文件大小限制')
        path = settings_path(config.DATA_DIR)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return get_model_settings(config)
