"""Selected-model token accounting and finite request budgets.

Only an explicitly selected local tokenizer is loaded. Without one, UTF-8 byte
length is a conservative estimate for byte-based tokenizers, not an exact count
from the remote model. Source messages are never shortened by this module.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path


def _config(config):
    if config is None:
        from config import Config
        return Config
    return config


def context_window(config=None):
    config = _config(config)
    return max(2048, min(1048576, int(getattr(config, 'LLM_CONTEXT_TOKENS', 4096))))


def reserve_tokens(context_tokens):
    # Message-envelope allowance is counted separately. Keep a safety reserve
    # even on a 2K model, without consuming a fixed 1K of its usable window.
    return min(256, max(64, int(context_tokens) // 32))


@lru_cache(maxsize=32)
def _file_digest(path, signature):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _selection(config=None, model=None):
    config = _config(config)
    # An empty selected path intentionally disables local tokenization. Do not
    # fall through to VLLM_MODEL_PATH: it may belong to a previously used model.
    selected = (getattr(config, 'LLM_TOKENIZER_PATH') if hasattr(config, 'LLM_TOKENIZER_PATH')
                else getattr(config, 'CONVERSATION_TOKENIZER_PATH', ''))
    raw = str(selected or '').strip()
    backend = str(getattr(config, 'LLM_BACKEND', ''))
    prefix = {'vllm':'VLLM', 'ollama':'OLLAMA', 'openai_compatible':'OPENAI_COMPATIBLE'}.get(backend, '')
    configured_model = str(getattr(config, 'LLM_MODEL', '') or getattr(config, prefix + '_MODEL', ''))
    configured_url = str(getattr(config, 'LLM_BASE_URL', '') or getattr(config, prefix + '_BASE_URL', '')).rstrip('/')
    actual_backend = str(getattr(model, 'backend_name', '') or backend)
    actual_model = str(getattr(model, 'model', '') or (model if isinstance(model, str) else '') or configured_model)
    actual_url = str(getattr(model, 'base_url', '') or configured_url).rstrip('/')
    if model is not None and (actual_backend, actual_model, actual_url) != (backend, configured_model, configured_url):
        # Explicit generation tokenizers do not automatically apply to a
        # separately configured translation client or another model instance.
        raw = ''
    path, signature, digest = '', (), ''
    if raw:
        file = Path(raw).expanduser()
        if file.is_dir():
            file = file / 'tokenizer.json'
        path = str(file.resolve())
        try:
            stat = file.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                         getattr(stat, 'st_ino', 0))
            digest = _file_digest(path, signature)
        except (OSError, ValueError):
            signature = ('unavailable',)
    identity = (
        actual_backend, actual_model, actual_url,
        str(getattr(config, 'LLM_MODEL_VERSION', '')),
        str(getattr(config, 'LLM_TOKENIZER_VERSION', '')),
        path, signature, digest,
    )
    return identity


@lru_cache(maxsize=16)
def _load_tokenizer(identity):
    path = identity[5]
    if not path or identity[6] == ('unavailable',):
        return None
    try:
        from tokenizers import Tokenizer
        return Tokenizer.from_file(path)
    except (ImportError, OSError, ValueError):
        return None


def tokenizer(config=None, model=None):
    return _load_tokenizer(_selection(config, model))


def tokenizer_identity(config=None, model=None):
    identity = _selection(config, model)
    method = 'local_tokenizer' if _load_tokenizer(identity) is not None else 'utf8_byte_upper_bound'
    return method + ':' + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def token_count_method(config=None, model=None):
    return 'local_tokenizer' if tokenizer(config, model) is not None else 'utf8_byte_upper_bound'


def count_tokens(text, config=None, model=None):
    text = str(text)
    encoder = tokenizer(config, model)
    return len(encoder.encode(text, add_special_tokens=False).ids) if encoder else len(text.encode('utf-8'))


def clip_tokens(text, tokens, config=None, model=None):
    """Explicit excerpt helper; transport budgeting must never invoke it."""
    text, tokens = str(text), max(0, int(tokens))
    encoder = tokenizer(config, model)
    if encoder:
        return encoder.decode(encoder.encode(text, add_special_tokens=False).ids[:tokens],
                              skip_special_tokens=False)
    return text.encode('utf-8')[:tokens].decode('utf-8', 'ignore')


def request_budget(system, user, max_tokens, config=None, model=None):
    """Bound output to remaining capacity, or fail before sending full input."""
    from services.model_contract import LocalModelError
    config = _config(config)
    context = context_window(config)
    try:
        requested = int(1024 if max_tokens is None else max_tokens)
        if requested <= 0:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise LocalModelError('模型输出预算必须是有限正整数', code='configuration_error') from None
    # 64 accounts for the two chat messages and their role/template delimiters;
    # reserve is additional uncertainty, not permission to trim original input.
    estimated = count_tokens(system, config, model) + count_tokens(user, config, model) + 64
    reserve = reserve_tokens(context)
    available = context - estimated - reserve
    if available < 1:
        raise LocalModelError(
            '完整输入超过所选模型上下文预算；请缩小批次或分片后重试', code='context_limit')
    return {'max_tokens': min(requested, available), 'context_tokens': context,
            'input_tokens': estimated, 'reserve_tokens': reserve,
            'token_count_method': token_count_method(config, model),
            'input_truncated': False, 'requested_max_tokens': requested,
            'model_identity': tokenizer_identity(config, model)}
