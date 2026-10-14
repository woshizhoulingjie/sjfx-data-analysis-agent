"""Explicit embedding input encoding and tokenizer, independent of generation."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


def _config(config):
    if config is None:
        from config import Config
        return Config
    return config


def encoding(config=None):
    value = str(getattr(_config(config), 'EMBED_ENCODING', 'e5')).lower()
    if value not in {'e5', 'plain'}:
        raise ValueError('向量输入编码必须为 e5 或 plain')
    return value


def prefix(role, config=None, *, client=None):
    if role not in {'query', 'passage'}:
        raise ValueError('Unknown embedding input role')
    policy = str(getattr(client, 'encoding_policy', ''))
    selected = 'plain' if 'plain' in policy else 'e5' if policy else encoding(config)
    return role + ': ' if selected == 'e5' else ''


@lru_cache(maxsize=8)
def _load_tokenizer(path, model_binding, file_version):
    try:
        from tokenizers import Tokenizer
        return Tokenizer.from_file(path)
    except (ImportError, OSError, ValueError):
        return None


def tokenizer(config=None):
    config = _config(config)
    raw = str(getattr(config, 'EMBED_TOKENIZER_PATH', '') or '').strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_dir():
        path = path / 'tokenizer.json'
    try:
        stat = path.stat()
        if not path.is_file():
            return None
        version = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        binding = (getattr(config, 'EMBED_BACKEND', ''), getattr(config, 'EMBED_MODEL', ''),
                   getattr(config, 'EMBED_BASE_URL', ''), getattr(config, 'EMBED_VERSION', ''),
                   encoding(config))
        return _load_tokenizer(str(path.resolve()), binding, version)
    except OSError:
        return None
