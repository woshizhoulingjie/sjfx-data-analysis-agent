"""Local vLLM OpenAI-compatible model transport.

The vLLM server is intentionally bound to loopback. This adapter keeps the
same small ``chat``/``health_check`` contract as the legacy Ollama transport,
so the analysis services do not depend on a particular serving backend.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from services.ollama import LocalModelClient, LocalModelError


class VLLMClient(LocalModelClient):
    """Client for a local vLLM OpenAI-compatible HTTP server."""

    # vLLM implements the OpenAI JSON-object response contract. Keeping the
    # capability on the transport avoids giving Ollama-only calls unsupported
    # OpenAI arguments.
    supports_json_response_format = True

    def __init__(self, base_url, model, timeout=600, max_concurrency=1, api_key=None):
        super().__init__(base_url, model, timeout=timeout, max_concurrency=max_concurrency)
        self.api_key = str(api_key or "").strip()

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
        # This deployment intentionally allows one generation at a time on a
        # 24 GB GPU. Do not let an interactive request wait behind a long
        # package-analysis generation forever; return a clear retryable error.
        queue_wait = min(15, effective_timeout)
        for attempt in range(attempts):
            try:
                acquired = self._semaphore.acquire(timeout=queue_wait)
                if not acquired:
                    raise LocalModelError("vLLM is busy with another generation; please retry shortly")
                try:
                    with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                        data = json.loads(response.read().decode("utf-8"))
                finally:
                    self._semaphore.release()
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                message = "vLLM HTTP {}: {}".format(exc.code, detail[:500])
                if not LocalModelClient._retryable_http(exc.code) or attempt + 1 >= attempts:
                    raise LocalModelError(message) from exc
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                message = "本机 vLLM 调用失败：{}".format(exc)
                if attempt + 1 >= attempts:
                    raise LocalModelError(message) from exc
            LocalModelClient._backoff(attempt)

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
        return {
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


class VLLMEmbeddingClient:
    """Client for a local vLLM pooling/embedding server."""

    def __init__(self, base_url, model, timeout=12, max_batch_size=8, max_chars=1200, api_key=None):
        self.base_url = (base_url or "").rstrip("/")
        self.model = str(model or "").strip()
        self.timeout = max(1, int(timeout))
        self.max_batch_size = max(1, int(max_batch_size))
        self.max_chars = max(64, int(max_chars))
        self.api_key = str(api_key or "").strip()
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

    def embed(self, texts):
        values = [str(value or "").strip()[:self.max_chars] for value in (texts or [])]
        if not values:
            return []
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
            try:
                acquired = self._semaphore.acquire(timeout=self.timeout)
                if not acquired:
                    raise LocalModelError("vLLM Embedding 忙碌，已快速回退词法检索")
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        data = json.loads(response.read().decode("utf-8"))
                finally:
                    self._semaphore.release()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise LocalModelError("vLLM Embedding HTTP {}: {}".format(exc.code, detail[:300])) from exc
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                raise LocalModelError("本机 vLLM Embedding 调用失败：{}".format(exc)) from exc
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
