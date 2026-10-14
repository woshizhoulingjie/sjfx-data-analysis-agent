"""Select the configured local HTTP transport without loading an inference engine."""

from __future__ import annotations

from services.model_contract import bounded_timeout


def create_model_client(config=None, *, timeout=None, max_concurrency=None,
                        require_enabled=True):
    """Construct the selected local adapter; never switch backends implicitly.

    Background services return ``None`` when the selected backend is disabled.
    The application may construct its transport even when generation is disabled
    so configuration and health information remain available.
    """
    if config is None:
        from config import Config
        config = Config
    backend = str(config.LLM_BACKEND).strip().lower()
    width = config.LLM_MAX_CONCURRENCY if max_concurrency is None else max_concurrency
    queue = getattr(config, "LLM_QUEUE_TIMEOUT_SECONDS", 15)
    policy = dict(allowed_endpoints=getattr(config, 'MODEL_ALLOWED_ENDPOINTS', ()),
                  budget_config=config, context_tokens=getattr(config, 'LLM_CONTEXT_TOKENS', 0))
    if backend == "vllm":
        if require_enabled and not config.ENABLE_VLLM:
            return None
        from services.vllm import VLLMClient
        return VLLMClient(
            config.VLLM_BASE_URL, config.VLLM_MODEL,
            timeout=min(bounded_timeout(timeout, config.VLLM_REQUEST_TIMEOUT), config.VLLM_REQUEST_TIMEOUT),
            max_concurrency=width, api_key=config.VLLM_API_KEY, queue_timeout=queue, **policy,
        )
    if backend == "ollama":
        if require_enabled and not config.ENABLE_SHARED_OLLAMA:
            return None
        from services.ollama import OllamaClient
        return OllamaClient(
            config.OLLAMA_BASE_URL, config.OLLAMA_MODEL,
            timeout=min(bounded_timeout(timeout, config.SHARED_OLLAMA_REQUEST_TIMEOUT), config.SHARED_OLLAMA_REQUEST_TIMEOUT),
            max_concurrency=width, api_key=getattr(config, "OLLAMA_API_KEY", ""), queue_timeout=queue, **policy,
        )
    if backend == "openai_compatible":
        if require_enabled and not getattr(config, "ENABLE_OPENAI_COMPATIBLE", False):
            return None
        from services.openai_compatible import OpenAICompatibleClient
        configured_timeout = getattr(config, "OPENAI_COMPATIBLE_REQUEST_TIMEOUT", 180)
        return OpenAICompatibleClient(
            config.OPENAI_COMPATIBLE_BASE_URL, config.OPENAI_COMPATIBLE_MODEL,
            timeout=min(bounded_timeout(timeout, configured_timeout), configured_timeout),
            max_concurrency=width, api_key=getattr(config, "OPENAI_COMPATIBLE_API_KEY", ""),
            supports_json_response_format=getattr(config, "OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT", False),
            queue_timeout=queue, **policy,
        )
    raise ValueError("Unsupported local model backend")


def create_translation_client(config=None, *, timeout=None, max_concurrency=1):
    """Explicit native Ollama translation settings are independent of generation."""
    if config is None:
        from config import Config
        config = Config
    from services.ollama import OllamaClient
    configured_timeout = getattr(config, "SHARED_OLLAMA_REQUEST_TIMEOUT", 600)
    return OllamaClient(
        config.TRANSLATION_OLLAMA_BASE_URL, config.TRANSLATION_OLLAMA_MODEL,
        timeout=min(bounded_timeout(timeout, configured_timeout), configured_timeout),
        max_concurrency=max_concurrency, api_key=getattr(config, "OLLAMA_API_KEY", ""),
        queue_timeout=getattr(config, "LLM_QUEUE_TIMEOUT_SECONDS", 15),
        allowed_endpoints=getattr(config, 'MODEL_ALLOWED_ENDPOINTS', ()),
        budget_config=config, context_tokens=getattr(config, 'LLM_CONTEXT_TOKENS', 0),
    )
