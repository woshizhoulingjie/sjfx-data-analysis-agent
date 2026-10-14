"""Runtime-free local embedding transport and durable vector-space identity.

The model column of existing caches remains their namespace. A new identity
never overwrites the namespace of another model or encoding policy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request

_artifact_cache = {}
_artifact_lock = threading.Lock()

from services.model_contract import (LocalModelClient, LocalModelError,
    bounded_timeout, validate_model_url, normalize_allowed_endpoints, model_url_origin, validate_model_name, open_local_request,
    read_json_response, read_error_detail, safe_http_error, transport_error)


def validate_vectors(vectors, count=None, dimension=0):
    """Reject incomplete, mixed-dimensional, nonnumeric and nonfinite batches."""
    if not isinstance(vectors, (list, tuple)) or (count is not None and len(vectors) != count):
        raise LocalModelError('语义向量数量不一致', code='invalid_embedding')
    size = int(dimension or 0)
    result = []
    for vector in vectors:
        if not isinstance(vector, (list, tuple)) or not vector:
            raise LocalModelError('语义向量格式无效', code='invalid_embedding')
        if not size:
            size = len(vector)
        if len(vector) != size:
            raise LocalModelError('语义向量维度不一致，需要重建索引', code='embedding_incompatible')
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in vector):
            raise LocalModelError('语义向量必须为数值', code='invalid_embedding')
        values = [float(x) for x in vector]
        if any(not math.isfinite(x) or abs(x) > 3.4e38 for x in values) or not any(values):
            raise LocalModelError('语义向量包含无效数值', code='invalid_embedding')
        result.append(values)
    return result


def artifact_fingerprint(path):
    """Hash model/config bytes once per file version without loading a runtime."""
    root = Path(path) if path else None
    if not root or not root.is_dir():
        return ''
    files = [p for p in sorted(root.iterdir(), key=lambda p: p.name) if p.is_file() and p.suffix in {'.json', '.safetensors', '.bin', '.gguf'}]
    versions = [(p.name,p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns) for p in files]
    key = (str(root.resolve()),tuple(versions))
    with _artifact_lock:
        if key in _artifact_cache: return _artifact_cache[key]
        parts = []
        for item in files:
            digest = hashlib.sha256()
            with item.open('rb') as stream:
                while True:
                    block = stream.read(1024*1024)
                    if not block: break
                    digest.update(block)
            parts.append([item.name,digest.hexdigest()])
        value = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest() if parts else ''
        _artifact_cache[key] = value
        while len(_artifact_cache)>8: _artifact_cache.pop(next(iter(_artifact_cache)))
        return value


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate adoption key')
        result[key] = value
    return result


def _legacy_adoption(config, lane):
    """Read an explicit operator attestation, never infer compatibility from names.

    Invalid/missing files disable adoption, retaining the normal v2 namespace.
    Records are immutable process-start snapshots; changes require new clients.
    """
    try:
        explicit = str(getattr(config, 'SJFX_EMBED_LEGACY_ADOPTION_FILE', '') or
            os.getenv('SJFX_EMBED_LEGACY_ADOPTION_FILE', '') or '').strip()
        base = Path(getattr(config, 'BASE_DIR', Path(__file__).resolve().parents[1]))
        if explicit:
            path = Path(explicit).expanduser()
            if not path.is_absolute():
                path = base / path
        else:
            state = getattr(config, 'DATA_DIR', None)
            if not state:
                return None
            path = Path(state).expanduser() / 'embedding-legacy-adoption.json'
            if not path.is_absolute():
                path = base / path
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return None
        document = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_json_object)
        if not isinstance(document, dict) or set(document) != {'schema', 'version', 'lanes'}:
            return None
        if document['schema'] != 'sjfx.embedding-legacy-adoption' or type(document['version']) is not int or document['version'] != 1:
            return None
        lanes = document['lanes']
        if not isinstance(lanes, dict) or set(lanes) - {'ordinary', 'large', 'mail'}:
            return None
        records = {}
        for name, record in lanes.items():
            if not isinstance(record, dict) or set(record) != {'identity', 'legacy_namespace', 'dimension'}:
                return None
            identity, namespace, dimension = record['identity'], record['legacy_namespace'], record['dimension']
            if not isinstance(namespace, str) or not namespace or len(namespace) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in namespace):
                return None
            if not isinstance(identity, str) or not identity.startswith(namespace + ':sjfx-embedding-v2:'):
                return None
            digest = identity[len(namespace + ':sjfx-embedding-v2:'):]
            if len(digest) != 64 or set(digest) - set('0123456789abcdef'):
                return None
            if type(dimension) is not int or not 1 <= dimension <= 65536:
                return None
            records[name] = (identity, namespace, dimension)
        return records.get(lane)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def client_identity(client):
    """Use a verified legacy namespace only while the real v2 identity matches."""
    identity = str(getattr(client, 'identity', None) or getattr(client, 'model', '') or '')
    adopt = getattr(client, '_legacy_namespace', None)
    return (adopt(identity) if callable(adopt) else None) or identity


def require_index_identity(conn, scope, package_id, identity, dimension=0, existing_keys=()):
    """Bind an existing index to one space; incompatible switches preserve all rows."""
    keys = {str(key) for key in existing_keys if key}
    if keys and keys != {identity}:
        raise LocalModelError('向量索引与当前模型版本不兼容，旧索引已保留，请运行 rebuild_embeddings.py 重建', code='embedding_rebuild_required')
    conn.execute('CREATE TABLE IF NOT EXISTS sjfx_embedding_identity (scope TEXT NOT NULL,package_id TEXT NOT NULL,identity TEXT NOT NULL,dimension INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(scope,package_id))')
    old = conn.execute('SELECT identity,dimension FROM sjfx_embedding_identity WHERE scope=? AND package_id=?', (scope, package_id)).fetchone()
    if old and (old[0] != identity or (dimension and old[1] and old[1] != dimension)):
        raise LocalModelError('向量索引模型或维度已变化，旧索引已保留，请运行 rebuild_embeddings.py 重建', code='embedding_rebuild_required')
    conn.execute('INSERT INTO sjfx_embedding_identity VALUES(?,?,?,?) ON CONFLICT(scope,package_id) DO UPDATE SET dimension=CASE WHEN excluded.dimension>0 THEN excluded.dimension ELSE sjfx_embedding_identity.dimension END', (scope, package_id, identity, int(dimension or 0)))


class OpenAIEmbeddingClient:
    backend = 'openai_compatible'

    def __init__(self, base_url, model, timeout=12, max_batch_size=8, max_chars=1200,
                 api_key=None, semaphore_timeout=None, retries=None, *, version='',
                 weights_id='', config_id='', dimension=0, encoding_policy='plain-v1',
                 allowed_endpoints=()):
        self.allowed_endpoints = normalize_allowed_endpoints(allowed_endpoints)
        self.base_url = validate_model_url(base_url, self.allowed_endpoints)
        self.model = validate_model_name(model)
        self.timeout = bounded_timeout(timeout, maximum=600)
        self.max_batch_size = max(1, min(64, int(max_batch_size)))
        self.max_chars = max(64, min(100000, int(max_chars)))
        self.api_key = str(api_key or '')
        if any(ord(c) < 32 or ord(c) == 127 for c in self.api_key):
            raise LocalModelError('模型认证配置包含无效字符', code='configuration_error')
        self.semaphore_timeout = bounded_timeout(semaphore_timeout, default=2, maximum=30)
        self.retries = LocalModelClient._retry_limit(2 if retries is None else retries)
        self.dimension = max(0, int(dimension or 0))
        self.version = str(version or '')
        self.weights_id = str(weights_id or '')
        self.config_id = str(config_id or '')
        self.encoding_policy = str(encoding_policy)
        self._semaphore = threading.BoundedSemaphore(1)
        self._resolved_identity = None
        self._remote_stamp = None
        self._legacy_adoption = None

    def _legacy_namespace(self, identity):
        record = self._legacy_adoption
        if record is None:
            return None
        expected, namespace, dimension = record
        if identity != expected or namespace != self.model or (self.dimension and self.dimension != dimension):
            return None
        # Identity was resolved with the configured dimension before pinning the
        # verified output dimension. The true identity itself is never replaced.
        self.dimension = dimension
        return namespace

    @property
    def configured(self):
        return True

    def _headers(self):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        return headers

    def _request(self, path, payload=None, retries=None, timeout=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        request = urllib.request.Request(self.base_url + path, data=body, headers=self._headers(), method='GET' if body is None else 'POST')
        attempts = (self.retries if retries is None else LocalModelClient._retry_limit(retries)) + 1
        for attempt in range(attempts):
            try:
                with open_local_request(request, timeout=self.timeout if timeout is None else min(self.timeout,bounded_timeout(timeout)),
                                        allowed_endpoints=self.allowed_endpoints) as response:
                    return read_json_response(response)
            except urllib.error.HTTPError as exc:
                failure = safe_http_error('Embedding', exc.code, read_error_detail(exc))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failure = transport_error('Embedding', exc)
            except LocalModelError:
                raise
            if not failure.retryable or attempt + 1 == attempts:
                raise failure from None
            LocalModelClient._backoff(attempt)

    def _model_info(self, timeout=None):
        data = self._request('/models', retries=0, timeout=timeout)
        rows = data.get('data')
        if not isinstance(rows, list):
            raise LocalModelError('模型列表响应无效', code='invalid_response')
        selected = next((r for r in rows if isinstance(r, dict) and r.get('id') == self.model), None)
        if selected is None:
            raise LocalModelError('配置的本机向量模型未加载', code='model_unavailable')
        return selected, [r.get('id') for r in rows if isinstance(r, dict) and isinstance(r.get('id'), str)]

    def _stamp(self, info):
        stamp = {key: info[key] for key in ('sjfx_identity', 'revision', 'version', 'digest') if key in info}
        # Some compatible servers expose process startup time as `created`;
        # configured artifact IDs take precedence over that unstable timestamp.
        if not stamp and not (self.version or self.weights_id or self.config_id) and 'created' in info:
            stamp['created'] = info['created']
        return stamp

    @property
    def identity(self):
        if self._resolved_identity is None:
            info, _ = self._model_info()
            self._remote_stamp = self._stamp(info)
            self.identity_source = 'server_revision' if self._remote_stamp else 'configured_artifacts' if self.version or self.weights_id or self.config_id else 'configured_endpoint_only'
            details = dict(backend=self.backend, endpoint=self.base_url, model=self.model,
                version=self.version, weights=self.weights_id, config=self.config_id,
                dimension=self.dimension, policy=self.encoding_policy, max_chars=self.max_chars,
                remote=self._remote_stamp)
            self._resolved_identity = self.model + ':sjfx-embedding-v2:' + hashlib.sha256(json.dumps(details, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return self._resolved_identity

    def with_options(self, **options):
        # Query budgets do not redefine the passage encoding's persisted identity.
        self._legacy_namespace(self.identity)
        clone = copy.copy(self)
        for name, value in options.items():
            if name not in {'timeout', 'max_chars', 'max_batch_size', 'retries', 'semaphore_timeout'}:
                raise ValueError('Unsupported embedding request option')
            setattr(clone, name, value)
        clone._semaphore = threading.BoundedSemaphore(1)
        return clone

    def health_check(self, timeout=3):
        try:
            _, models = self._model_info(timeout=timeout)
            return {'reachable': True, 'model_available': True, 'models': models, 'backend': self.backend}
        except LocalModelError as exc:
            return {'reachable': exc.code == 'model_unavailable', 'model_available': False, 'models': [], 'error': str(exc), 'code': exc.code}

    def _payload(self, batch):
        return '/embeddings', {'model': self.model, 'input': batch, 'encoding_format': 'float'}

    def _vectors(self, data, count):
        rows = data.get('data')
        if not isinstance(rows, list) or len(rows) != count or any(not isinstance(r, dict) for r in rows):
            raise LocalModelError('语义向量数量不一致', code='invalid_embedding')
        indexes = [r.get('index') for r in rows]
        if any(isinstance(i, bool) or not isinstance(i, int) for i in indexes) or sorted(indexes) != list(range(count)):
            raise LocalModelError('语义向量响应序号无效', code='invalid_embedding')
        if data.get('model') not in {None, self.model}:
            raise LocalModelError('向量响应模型与配置不一致', code='embedding_incompatible')
        if 'sjfx_identity' in data and self._remote_stamp.get('sjfx_identity') != data['sjfx_identity']:
            raise LocalModelError('向量响应权重版本不一致', code='embedding_incompatible')
        return [r.get('embedding') for r in sorted(rows, key=lambda r: r['index'])]

    def embed(self, texts, retries=None):
        from sjfx_protection.business import recheck_active_sources
        recheck_active_sources()
        values = [str(value or '').strip()[:self.max_chars] for value in texts or []]
        if not values:
            return []
        # Resolve/pin model revision before any cache write, and verify on every call.
        self._legacy_namespace(self.identity)
        if self._stamp(self._model_info()[0]) != self._remote_stamp:
            raise LocalModelError('向量模型版本已变化，请重建索引', code='embedding_incompatible')
        if not self._semaphore.acquire(timeout=self.semaphore_timeout):
            raise LocalModelError('本机向量模型忙碌', code='busy', retryable=True)
        try:
            result = []
            for offset in range(0, len(values), self.max_batch_size):
                batch = values[offset:offset+self.max_batch_size]
                path, payload = self._payload(batch)
                recheck_active_sources()
                data = self._request(path, payload, retries=retries)
                recheck_active_sources()
                vectors = validate_vectors(self._vectors(data, len(batch)), len(batch), self.dimension)
                if vectors and not self.dimension:
                    self.dimension = len(vectors[0])
                result.extend(vectors)
            return validate_vectors(result, len(values), self.dimension)
        finally:
            self._semaphore.release()


class OllamaEmbeddingClient(OpenAIEmbeddingClient):
    backend = 'ollama'

    def __init__(self, base_url, model, timeout=60, num_gpu=0, **kwargs):
        base_url = validate_model_url(base_url, kwargs.get('allowed_endpoints', ()))
        if base_url.endswith('/v1'):
            base_url = base_url[:-3]
        self.num_gpu = max(0, int(num_gpu))
        super().__init__(base_url, model, timeout=timeout, **kwargs)

    def _model_info(self, timeout=None):
        data = self._request('/api/tags', retries=0, timeout=timeout)
        rows = data.get('models')
        if not isinstance(rows, list):
            raise LocalModelError('Ollama 模型列表响应无效', code='invalid_response')
        names = [r.get('name') for r in rows if isinstance(r, dict)]
        selected = next((r for r in rows if isinstance(r, dict) and r.get('name') in {self.model, self.model+':latest'}), None)
        if selected is None or not selected.get('digest'):
            raise LocalModelError('配置的 Ollama 向量模型未加载或缺少权重标识', code='model_unavailable')
        return selected, names

    def _payload(self, batch):
        return '/api/embed', {'model': self.model, 'input': batch, 'truncate': False, 'keep_alive': '30s', 'options': {'num_gpu': self.num_gpu}}

    def _vectors(self, data, count):
        if data.get('model') not in {None, self.model, self.model+':latest'}:
            raise LocalModelError('Ollama 向量响应模型不一致', code='embedding_incompatible')
        return data.get('embeddings')


VLLMEmbeddingClient = OpenAIEmbeddingClient


def _default_embedding_api_key(config, backend, base, model):
    """Legacy inheritance must not send generation credentials to a new origin."""
    origin = model_url_origin(base)
    if backend == 'ollama':
        owner = getattr(config, 'OLLAMA_BASE_URL', '')
        return getattr(config, 'OLLAMA_API_KEY', '') if owner and model_url_origin(owner) == origin else ''
    legacy_base = getattr(config, 'VLLM_EMBED_BASE_URL', '')
    if (legacy_base and str(legacy_base).rstrip('/') == str(base).rstrip('/')
            and model == getattr(config, 'VLLM_EMBED_MODEL', None)):
        return getattr(config, 'VLLM_API_KEY', '')
    for endpoint, key in (('VLLM_BASE_URL', 'VLLM_API_KEY'),
                          ('OPENAI_COMPATIBLE_BASE_URL', 'OPENAI_COMPATIBLE_API_KEY')):
        owner = getattr(config, endpoint, '')
        if owner and model_url_origin(owner) == origin:
            return getattr(config, key, '')
    return ''


def create_embedding_client(config=None, *, lane='ordinary', timeout=None, max_batch_size=None, max_chars=None):
    if config is None:
        from config import Config
        config = Config
    backend = str(getattr(config, 'EMBED_BACKEND', 'openai_compatible' if getattr(config, 'ENABLE_VLLM_EMBEDDINGS', True) else 'disabled')).lower()
    if backend == 'vllm': backend = 'openai_compatible'
    if backend == 'disabled':
        return None
    if backend not in {'openai_compatible', 'ollama'}:
        raise LocalModelError('未知向量后端', code='configuration_error')
    if lane == 'mail' and not getattr(config, 'MAIL_ENABLE_VECTOR_INDEX', True):
        return None
    base = getattr(config, 'EMBED_BASE_URL', getattr(config, 'VLLM_EMBED_BASE_URL', 'http://127.0.0.1:8002/v1'))
    model = getattr(config, 'EMBED_MODEL', getattr(config, 'VLLM_EMBED_MODEL', 'multilingual-e5-small'))
    if lane == 'mail':
        base = getattr(config, 'MAIL_EMBED_BASE_URL', base)
        model = getattr(config, 'MAIL_EMBED_MODEL', model)
    if backend == 'ollama' and not getattr(config, 'EMBED_BASE_URL', None):
        base = getattr(config, 'OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
        model = getattr(config, 'OLLAMA_EMBED_MODEL', model)
    if lane == 'large':
        base = (getattr(config, 'SJFX_LP_EMBED_BASE_URL', '') or
            getattr(config, 'LP_EMBED_BASE_URL', '') or
            os.getenv('SJFX_LP_EMBED_BASE_URL', '').strip() or base)
        if max_batch_size is None:
            try:
                max_batch_size = max(1, min(64, int(
                    getattr(config, 'SJFX_LP_EMBED_MAX_BATCH_SIZE', '') or
                    os.getenv('SJFX_LP_EMBED_MAX_BATCH_SIZE', '').strip() or
                    getattr(config, 'VLLM_EMBED_MAX_BATCH_SIZE', 8) or 8)))
            except (ValueError, TypeError):
                max_batch_size = getattr(config, 'VLLM_EMBED_MAX_BATCH_SIZE', 8)
    policy = {'ordinary':'e5-prefix-v1', 'large':'catalog-window-v2', 'mail':'mail-e5-window-v1'}.get(lane)
    if not policy:
        raise ValueError('Unknown embedding lane')
    encoding = str(getattr(config, 'EMBED_ENCODING', 'e5')).strip().lower()
    if encoding not in {'e5', 'plain'}:
        raise LocalModelError('未知向量编码策略', code='configuration_error')
    if encoding == 'plain':
        policy = {'ordinary': 'plain-v1', 'large': 'catalog-plain-window-v1', 'mail': 'mail-plain-window-v1'}[lane]
    # The bundled E5 path is a legacy resource default, not evidence that a
    # newly selected remote service uses those weights. Preserve its fingerprint
    # only for the unchanged legacy model/endpoint binding.
    legacy_artifacts = (encoding == 'e5' and backend == 'openai_compatible'
        and model == getattr(config, 'VLLM_EMBED_MODEL', 'multilingual-e5-small')
        and base.rstrip('/') == str(getattr(config, 'VLLM_EMBED_BASE_URL', 'http://127.0.0.1:8002/v1')).rstrip('/'))
    options = dict(timeout=timeout if timeout is not None else getattr(config, 'VLLM_EMBED_TIMEOUT_SECONDS', 12),
        max_batch_size=max_batch_size if max_batch_size is not None else getattr(config, 'VLLM_EMBED_MAX_BATCH_SIZE', 8),
        max_chars=max_chars if max_chars is not None else getattr(config, 'MAIL_EMBED_MAX_CHARS', 1180) if lane == 'mail' else 4096 if lane == 'large' else getattr(config, 'VLLM_EMBED_MAX_CHARS', 1200),
        api_key=getattr(config, 'EMBED_API_KEY') if hasattr(config, 'EMBED_API_KEY') else
            _default_embedding_api_key(config, backend, base, model),
        allowed_endpoints=getattr(config, 'MODEL_ALLOWED_ENDPOINTS', ()),
        retries=getattr(config, 'VLLM_EMBED_RETRIES', 2),
        semaphore_timeout=getattr(config, 'VLLM_EMBED_SEMAPHORE_TIMEOUT_SECONDS', 2),
        version=getattr(config, 'EMBED_VERSION', ''), weights_id=getattr(config, 'EMBED_WEIGHTS_ID', '') or
            (artifact_fingerprint(getattr(config, 'EMBED_MODEL_PATH', '')) if legacy_artifacts else ''),
        config_id=getattr(config, 'EMBED_CONFIG_ID', ''), dimension=getattr(config, 'EMBED_DIMENSION', 0), encoding_policy=policy)
    if backend == 'ollama':
        client = OllamaEmbeddingClient(base, model, num_gpu=getattr(config, 'OLLAMA_EMBED_NUM_GPU', 0), **options)
    else:
        client = OpenAIEmbeddingClient(base, model, **options)
    client._legacy_adoption = _legacy_adoption(config, lane)
    return client
