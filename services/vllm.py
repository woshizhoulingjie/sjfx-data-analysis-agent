"""vLLM transport for loopback or administrator-approved remote HTTP servers."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

from services.model_contract import (LocalModelClient, LocalModelError, ModelCapabilities,
    bounded_timeout, read_error_detail, open_local_request, read_json_response, safe_http_error,
    transport_error, read_response_bytes, parse_openai_chat)
from services.model_dispatch import serialized_generation, acquire_generation_semaphore

LOGGER = logging.getLogger(__name__)


class VLLMClient(LocalModelClient):
    """Client for a local vLLM OpenAI-compatible HTTP server."""

    backend_name = "vllm"

    # vLLM implements the OpenAI JSON-object response contract. Keeping the
    # capability on the transport avoids giving Ollama-only calls unsupported
    # OpenAI arguments.
    supports_json_response_format = True
    supports_json_schema = True
    capabilities = ModelCapabilities(json_response_format=True, thinking_control=True, trace_metadata=True, exact_context_budget=True)

    def __init__(self, base_url, model, timeout=600, max_concurrency=1, api_key=None, queue_timeout=15, *,
                 allowed_endpoints=(), budget_config=None, context_tokens=0):
        super().__init__(base_url, model, timeout=timeout, max_concurrency=max_concurrency, queue_timeout=queue_timeout,
                         allowed_endpoints=allowed_endpoints, budget_config=budget_config, context_tokens=context_tokens)
        self.api_key = str(api_key or "").strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in self.api_key):
            raise LocalModelError('模型认证配置包含无效字符', code='configuration_error')
        self._trace_metadata_cache = None

    @property
    def privacy_label(self):
        return super().privacy_label

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SJFX/local-vllm",
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def health_check(self, timeout=5):
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/models",
            headers=self._headers(),
            method="GET",
        )
        try:
            with open_local_request(request, timeout, allowed_endpoints=self.allowed_endpoints) as response:
                data = read_json_response(response)
            rows = data.get("data")
            if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
                raise LocalModelError("vLLM 模型列表结构无效", code="invalid_response")
            models = []
            for item in rows:
                if isinstance(item, dict):
                    value = item.get("id") or item.get("root") or item.get("name")
                    if value:
                        models.append(str(value))
            return {
                "reachable": True,
                "model_available": self.model in models,
                "models": models,
            }
        except Exception as exc:
            error = safe_http_error("vLLM", exc.code, read_error_detail(exc)) if isinstance(exc, urllib.error.HTTPError) else exc if isinstance(exc, LocalModelError) else transport_error("vLLM", exc)
            return {
                "reachable": False,
                "model_available": False,
                "models": [],
                "error": str(error),
                "error_code": error.code,
            }

    def _trace_metadata(self):
        from services.llm_trace import build_manifest_metadata

        if self._trace_metadata_cache is not None:
            return self._trace_metadata_cache
        root_url = self.base_url.rstrip("/")
        if root_url.endswith("/v1"):
            root_url = root_url[:-3]
        model_root = None
        version = None
        try:
            request = urllib.request.Request(
                self.base_url.rstrip("/") + "/models",
                headers=self._headers(), method="GET",
            )
            with open_local_request(request, min(10, self.timeout), allowed_endpoints=self.allowed_endpoints) as response:
                models = read_json_response(response)
            selected = next(
                (item for item in models.get("data", []) if isinstance(item, dict) and item.get("id") == self.model),
                {},
            )
            model_root = selected.get("root")
            request = urllib.request.Request(root_url + "/version", headers=self._headers(), method="GET")
            with open_local_request(request, min(10, self.timeout), allowed_endpoints=self.allowed_endpoints) as response:
                version = read_json_response(response).get("version")
        except Exception:
            pass
        self._trace_metadata_cache = build_manifest_metadata(
            self.model, model_root, version
        )
        return self._trace_metadata_cache

    def _prefix_cache_hits_total(self):
        root_url = self.base_url.rstrip("/")
        if root_url.endswith("/v1"):
            root_url = root_url[:-3]
        request = urllib.request.Request(root_url + "/metrics", headers=self._headers(), method="GET")
        with open_local_request(request, min(10, self.timeout), allowed_endpoints=self.allowed_endpoints) as response:
            text = read_response_bytes(response).decode("utf-8", errors="replace")
        match = re.search(
            r'^vllm:prefix_cache_hits_total\{[^\n]*\}\s+([0-9.eE+-]+)\s*$',
            text,
            flags=re.MULTILINE,
        )
        return float(match.group(1)) if match else None

    def _context_budget_after_error(self, payload, detail, timeout):
        """Measure the identical chat template after a context rejection.

        Never truncate evidence. If tokenization is unavailable, the original
        provider error remains authoritative rather than an estimated budget.
        """
        match = re.search(r'maximum context length is\s+(\d+)\s+tokens', detail, re.I)
        if not match:
            return None
        limit = int(match.group(1))
        root = self.base_url.rstrip('/')
        if root.endswith('/v1'):
            root = root[:-3]
        token_payload = {'model': payload['model'], 'messages': payload['messages'],
                         'add_generation_prompt': True,
                         'chat_template_kwargs': payload['chat_template_kwargs']}
        request = urllib.request.Request(root + '/tokenize',
            data=json.dumps(token_payload, ensure_ascii=False).encode('utf-8'),
            headers=self._headers(), method='POST')
        try:
            with open_local_request(request, timeout, allowed_endpoints=self.allowed_endpoints) as response:
                measured = read_json_response(response)
            count = measured.get('count')
            if isinstance(count, bool) or not isinstance(count, int):
                ids = measured.get('tokens', measured.get('token_ids'))
                count = len(ids) if isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids) else None
            if count is None or count < 0:
                return None
        except Exception as exc:
            LOGGER.warning('vLLM context tokenization unavailable; retaining original context error: %s', type(exc).__name__)
            return None
        reserve = 0
        safe_limit = limit
        if self.budget_config is not None or self.context_tokens:
            from services.model_budget import context_window, reserve_tokens
            configured = context_window(self.budget_config) if self.budget_config is not None else self.context_tokens
            safe_limit = min(limit, configured)
            reserve = reserve_tokens(safe_limit)
        available = safe_limit - count - reserve
        if available <= 0:
            raise LocalModelError('vLLM 输入包含 {} 个精确 token，已达到安全上下文预算 {}；需要调用方分片，未裁剪输入证据。'.format(count, safe_limit), code="context_limit")
        requested = payload['max_tokens']
        effective = min(requested, available)
        if effective >= requested:
            return None
        LOGGER.warning('vLLM context budget adjusted: prompt=%s limit=%s output=%s->%s; input unchanged',
                       count, limit, requested, effective)
        return {'prompt_tokens': count, 'context_limit': safe_limit, 'provider_context_limit': limit,
                'reserve_tokens': reserve,
                'requested_max_tokens': requested, 'effective_max_tokens': effective,
                'input_truncated': False}

    @serialized_generation
    def chat(self, system_prompt, user_prompt, temperature=0.2, max_tokens=1800,
             retries=2, timeout=None, response_format=None):
        budget = self._request_budget(system_prompt, user_prompt, max_tokens)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": str(system_prompt or "")},
                {"role": "user", "content": str(user_prompt or "")},
            ],
            "temperature": temperature,
            "max_tokens": budget['max_tokens'],
            "stream": False,
            # This Qwen3.5/3.6 checkpoint supports vLLM's chat-template
            # switch. Without it, a request can spend its entire budget on a
            # thinking trace instead of returning its final answer.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if os.getenv("SJFX_BENCHMARK_DETERMINISTIC", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            payload.update({"temperature": 0, "top_p": 1.0, "seed": 42})
        from services.llm_trace import get_recorder
        recorder = get_recorder()
        if recorder is not None:
            payload.update({"return_token_ids": True, "return_prompt_text": True})
            recorder.ensure_manifest(self._trace_metadata())
        if response_format:
            payload["response_format"] = response_format
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        effective_timeout = min(bounded_timeout(timeout, self.timeout), self.timeout)
        attempts = LocalModelClient._retry_limit(retries) + 1
        data = None
        completed_trace = None
        context_budget = None
        # This deployment intentionally allows one generation at a time on a
        # 24 GB GPU. Do not let an interactive request wait behind a long
        # package-analysis generation forever; return a clear retryable error.
        queue_wait = min(self.queue_timeout, effective_timeout)
        attempt = 0
        while attempt < attempts:
            trace_state = None
            raw_response = None
            cache_hits_before = None
            try:
                acquired = acquire_generation_semaphore(self._semaphore, queue_wait)
                if not acquired:
                    raise LocalModelError("vLLM 正在处理其他请求，请稍后重试", code="busy", retryable=True)
                try:
                    if recorder is not None:
                        cache_hits_before = self._prefix_cache_hits_total()
                        trace_state = recorder.begin(payload, system_prompt)
                    with open_local_request(request, effective_timeout, allowed_endpoints=self.allowed_endpoints) as response:
                        raw = read_response_bytes(response)
                        try:
                            if len(raw) > 16 * 1024 * 1024:
                                raise ValueError
                            raw_response = raw.decode('utf-8')
                            data = json.loads(raw_response)
                            result = parse_openai_chat(data, self.model)
                        except (ValueError, UnicodeError, TypeError):
                            raise LocalModelError('vLLM 响应 JSON 或结构无效', code='invalid_response') from None
                    if recorder is not None:
                        cache_hits_after = self._prefix_cache_hits_total()
                        cache_delta = (
                            max(0, int(round(cache_hits_after - cache_hits_before)))
                            if cache_hits_before is not None and cache_hits_after is not None
                            else None
                        )
                        completed_trace = (
                            trace_state, raw_response, cache_delta,
                            "vllm:prefix_cache_hits_total_delta"
                            if cache_delta is not None else None,
                        )
                        trace_state = None
                finally:
                    self._semaphore.release()
                break
            except urllib.error.HTTPError as exc:
                detail = read_error_detail(exc)
                error = safe_http_error("vLLM", exc.code, detail)
                message = str(error)
                if exc.code == 400 and context_budget is None:
                    try:
                        adjusted = self._context_budget_after_error(payload, detail, effective_timeout)
                    except LocalModelError as budget_error:
                        if recorder is not None and trace_state is not None:
                            recorder.finish(trace_state, error=str(budget_error))
                        raise
                    if adjusted is not None:
                        context_budget = adjusted
                        payload['max_tokens'] = adjusted['effective_max_tokens']
                        request = urllib.request.Request(self.base_url.rstrip('/') + '/chat/completions',
                            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                            headers=self._headers(), method='POST')
                        if recorder is not None and trace_state is not None:
                            recorder.abandon(trace_state)
                        # One measured retry, independent of transient-HTTP retries.
                        continue
                if not LocalModelClient._retryable_http(exc.code) or attempt + 1 >= attempts:
                    if recorder is not None and trace_state is not None:
                        recorder.finish(trace_state, error=message)
                    raise error from None
                if recorder is not None and trace_state is not None:
                    recorder.abandon(trace_state)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = transport_error("vLLM", exc)
                message = str(error)
                if attempt + 1 >= attempts:
                    if recorder is not None and trace_state is not None:
                        recorder.finish(trace_state, error=message)
                    raise error from None
                if recorder is not None and trace_state is not None:
                    recorder.abandon(trace_state)
            except LocalModelError as exc:
                if recorder is not None and trace_state is not None:
                    recorder.finish(trace_state, error=str(exc))
                raise
            LocalModelClient._backoff(attempt)
            attempt += 1

        if recorder is not None and completed_trace is not None:
            trace_state, raw_response, cache_delta, cache_source = completed_trace
            recorder.finish(
                trace_state,
                response=data,
                response_raw=raw_response,
                source_cached_tokens=cache_delta,
                source_cached_tokens_source=cache_source,
            )

        if context_budget is not None:
            result['context_budget'] = context_budget
        return self._budget_result(result, budget)


# Historical imports remain supported without a second implementation.
from services.embedding_backend import VLLMEmbeddingClient

__all__ = ['VLLMClient', 'VLLMEmbeddingClient']
