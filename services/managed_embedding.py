"""Selection policy for the installer-owned, offline CPU E5 service."""
from pathlib import Path
from urllib.parse import urlsplit

BUNDLED_MODEL = 'multilingual-e5-small'


def selected(config):
    """An external or disabled embedding selection never starts bundled E5."""
    if not getattr(config, 'MANAGED_EMBEDDING', False):
        return False
    if config.EMBED_BACKEND not in {'openai_compatible', 'vllm'} or config.EMBED_MODEL != BUNDLED_MODEL:
        return False
    address = urlsplit(config.EMBED_BASE_URL)
    return (address.scheme == 'http' and address.hostname == '127.0.0.1'
            and address.port == config.MANAGED_EMBED_PORT
            and address.path.rstrip('/') == '/v1'
            and not address.username and not address.password
            and not address.query and not address.fragment)


def resource_check(config):
    root = Path(config.MANAGED_EMBED_MODEL_PATH)
    required = ('config.json', 'model.safetensors', 'tokenizer.json',
                'tokenizer_config.json', 'sentencepiece.bpe.model', 'special_tokens_map.json')
    absent = [name for name in required if not (root/name).is_file()]
    return {'selected': selected(config), 'model': BUNDLED_MODEL, 'device': 'cpu',
            'auto_download': False, 'resource_complete': not absent, 'missing_files': absent,
            'port': config.MANAGED_EMBED_PORT}
