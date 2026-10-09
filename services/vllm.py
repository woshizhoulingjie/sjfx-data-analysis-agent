"""Local vLLM OpenAI-compatible model transport.

The vLLM server is intentionally bound to loopback. This adapter keeps the
same small ``chat``/``health_check`` contract as the legacy Ollama transport,
so the analysis services do not depend on a particular serving backend.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

from services.ollama import LocalModelClient, LocalModelError
from services.model_dispatch import serialized_generation

LOGGER = logging.getLogger(__name__)


class VLLMClient(LocalModelClient):
    """Client for a local vLLM OpenAI-compatible HTTP server."""

    # vLLM implements the OpenAI JSON-object response contract. Keeping the
    # capability on the transport avoids giving Ollama-only calls unsupported
    # OpenAI arguments.
    supports_json_response_format = True

    def __init__(self, base_url, model, timeout=600, max_concurrency=1, api_key=None):
        super().__init__(base_url, model, timeout=timeout, max_concurrency=max_concurrency)
        self.api_key = str(api_key or "").strip()
        self._trace_metadata_cache = None

    @property
    def privacy_label(self):
        return "服务器本机 vLLM（不出网）"

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
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            models = []
            for item in data.get("data", []):
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
            return {
                "reachable": False,
                "model_available": False,
                "models": [],
                "error": str(exc),
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
            with urllib.request.urlopen(request, timeout=10) as response:
                models = json.loads(response.read().decode("utf-8"))
            selected = next(
                (item for item in models.get("data", []) if item.get("id") == self.model),
                {},
            )
            model_root = selected.get("root")
            request = urllib.request.Request(root_url + "/version", method="GET")
            with urllib.request.urlopen(request, timeout=10) as response:
                version = json.loads(response.read().decode("utf-8")).get("version")
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
        request = urllib.request.Request(root_url + "/metrics", method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            text = response.read().decode("utf-8", errors="replace")
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
            with urllib.request.urlopen(request, timeout=timeout) as response:
                measured = json.loads(response.read().decode('utf-8'))
            count = measured.get('count')
            if isinstance(count, bool) or not isinstance(count, int):
                ids = measured.get('tokens', measured.get('token_ids'))
                count = len(ids) if isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids) else None
            if count is None or count < 0:
                return None
        except Exception as exc:
            LOGGER.warning('vLLM context tokenization unavailable; retaining original context error: %s', type(exc).__name__)
            return None
        available = limit - count
        if available <= 0:
            raise LocalModelError('vLLM 输入包含 {} 个精确 token，已达到上下文上限 {}；需要调用方分片，未裁剪输入证据。'.format(count, limit))
        requested = payload['max_tokens']
        effective = min(requested, available)
        if effective >= requested:
            return None
        LOGGER.warning('vLLM context budget adjusted: prompt=%s limit=%s output=%s->%s; input unchanged',
                       count, limit, requested, effective)
        return {'prompt_tokens': count, 'context_limit': limit,
                'requested_max_tokens': requested, 'effective_max_tokens': effective,
                'input_truncated': False}

    @serialized_generation
    def chat(self, system_prompt, user_prompt, temperature=0.2, max_tokens=1800,
             retries=2, timeout=None, response_format=None):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": str(system_prompt or "")},
                {"role": "user", "content": str(user_prompt or "")},
            ],
            "temperature": temperature,
            "max_tokens": max(1, int(max_tokens)),
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
        effective_timeout = max(1, int(timeout if timeout is not None else self.timeout))
        attempts = LocalModelClient._retry_limit(retries) + 1
        data = None
        completed_trace = None
        context_budget = None
        # This deployment intentionally allows one generation at a time on a
        # 24 GB GPU. Do not let an interactive request wait behind a long
        # package-analysis generation forever; return a clear retryable error.
        benchmark_mode = os.getenv("SJFX_BENCHMARK_DETERMINISTIC", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        queue_wait = effective_timeout if benchmark_mode else min(15, effective_timeout)
        attempt = 0
        while attempt < attempts:
            trace_state = None
            raw_response = None
            cache_hits_before = None
            try:
                acquired = self._semaphore.acquire(timeout=queue_wait)
                if not acquired:
                    raise LocalModelError("vLLM is busy with another generation; please retry shortly")
                try:
                    if recorder is not None:
                        cache_hits_before = self._prefix_cache_hits_total()
                        trace_state = recorder.begin(payload, system_prompt)
                    with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                        raw_response = response.read().decode("utf-8")
                        data = json.loads(raw_response)
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
                detail = exc.read().decode("utf-8", errors="replace")
                message = "vLLM HTTP {}: {}".format(exc.code, detail[:500])
                if exc.code == 400 and context_budget is None:
                    try:
                        adjusted = self._context_budget_after_error(payload, detail, effective_timeout)
                    except LocalModelError as budget_error:
                        if recorder is not None and trace_state is not None:
                            recorder.finish(trace_state, response_raw=detail, error=str(budget_error))
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
                        recorder.finish(trace_state, response_raw=detail, error=message)
                    raise LocalModelError(message) from exc
                if recorder is not None and trace_state is not None:
                    recorder.abandon(trace_state)
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                message = "本机 vLLM 调用失败：{}".format(exc)
                if attempt + 1 >= attempts:
                    if recorder is not None and trace_state is not None:
                        recorder.finish(trace_state, error=message)
                    raise LocalModelError(message) from exc
                if recorder is not None and trace_state is not None:
                    recorder.abandon(trace_state)
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

        choices = (data or {}).get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        content = (message or {}).get("content") or ""
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part)
                for part in content
            )
        content = str(content).strip()
        if not content:
            raise LocalModelError("vLLM 未返回最终答案")
        usage = (data or {}).get("usage") or {}
        result = {
            "content": content,
            "reasoning_content": (
                (message or {}).get("reasoning_content")
                or (message or {}).get("reasoning")
            ),
            "model": (data or {}).get("model") or self.model,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            "finish_reason": (choices[0] or {}).get("finish_reason") if choices else None,
        }
        if context_budget is not None:
            result['context_budget'] = context_budget
        return result


class VLLMEmbeddingClient:
    """Client for a local vLLM pooling/embedding server."""

    def __init__(self, base_url, model, timeout=12, max_batch_size=8, max_chars=1200,
                 api_key=None, semaphore_timeout=None, retries=None):
        self.base_url = (base_url or "").rstrip("/")
        self.model = str(model or "").strip()
        # ``timeout`` bounds one HTTP attempt; ``semaphore_timeout`` only bounds
        # waiting for this client's own mutex so a contended lane still returns
        # to its lexical fallback quickly.
        self.timeout = max(1, int(timeout))
        self.max_batch_size = max(1, int(max_batch_size))
        self.max_chars = max(64, int(max_chars))
        self.api_key = str(api_key or "").strip()
        if semaphore_timeout is None or retries is None:
            try:
                from config import Config
                if semaphore_timeout is None:
                    semaphore_timeout = Config.VLLM_EMBED_SEMAPHORE_TIMEOUT_SECONDS
                if retries is None:
                    retries = Config.VLLM_EMBED_RETRIES
            except Exception:  # standalone use keeps the historical behaviour
                semaphore_timeout = 2 if semaphore_timeout is None else semaphore_timeout
                retries = 0 if retries is None else retries
        self.semaphore_timeout = max(1, int(semaphore_timeout))
        self.retries = LocalModelClient._retry_limit(retries)
        import threading
        self._semaphore = threading.BoundedSemaphore(1)

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SJFX/local-vllm-embedding",
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def health_check(self, timeout=3):
        request = urllib.request.Request(
            self.base_url + "/models",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(1, int(timeout))) as response:
                data = json.loads(response.read().decode("utf-8"))
            models = [
                str(item.get("id") or item.get("root") or item.get("name"))
                for item in (data.get("data") or [])
                if isinstance(item, dict) and (item.get("id") or item.get("root") or item.get("name"))
            ]
            return {"reachable": True, "model_available": self.model in models, "models": models}
        except Exception as exc:
            return {"reachable": False, "model_available": False, "models": [], "error": str(exc)}

    def embed(self, texts, retries=None):
        values = [str(value or "").strip()[:self.max_chars] for value in (texts or [])]
        if not values:
            return []
        attempts = (
            LocalModelClient._retry_limit(retries) + 1
            if retries is not None else self.retries + 1
        )
        result = []
        for offset in range(0, len(values), self.max_batch_size):
            batch = values[offset:offset + self.max_batch_size]
            payload = json.dumps({
                "model": self.model,
                "input": batch,
                "encoding_format": "float",
            }, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                self.base_url + "/embeddings",
                data=payload,
                headers=self._headers(),
                method="POST",
            )
            data = None
            # A shared single-process CPU embedding server can exceed one
            # socket budget while other lanes are queued; a bounded retry keeps
            # a transient stall from discarding the whole semantic stage.
            for attempt in range(attempts):
                try:
                    acquired = self._semaphore.acquire(timeout=self.semaphore_timeout)
                    if not acquired:
                        raise LocalModelError("vLLM Embedding 忙碌，已快速回退词法检索")
                    try:
                        with urllib.request.urlopen(request, timeout=self.timeout) as response:
                            data = json.loads(response.read().decode("utf-8"))
                    finally:
                        self._semaphore.release()
                    break
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    message = "vLLM Embedding HTTP {}: {}".format(exc.code, detail[:300])
                    if not LocalModelClient._retryable_http(exc.code) or attempt + 1 >= attempts:
                        raise LocalModelError(message) from exc
                except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                    message = "本机 vLLM Embedding 调用失败：{}".format(exc)
                    if attempt + 1 >= attempts:
                        raise LocalModelError(message) from exc
                LocalModelClient._backoff(attempt)
            rows = sorted(
                [row for row in ((data or {}).get("data") or []) if isinstance(row, dict)],
                key=lambda row: int(row.get("index", 0)),
            )
            vectors = [row.get("embedding") for row in rows]
            if len(vectors) != len(batch) or any(not isinstance(vector, list) or not vector for vector in vectors):
                raise LocalModelError("vLLM Embedding 返回数量或向量格式不一致")
            result.extend(vectors)
        return result


__all__ = ["VLLMClient", "VLLMEmbeddingClient"]
