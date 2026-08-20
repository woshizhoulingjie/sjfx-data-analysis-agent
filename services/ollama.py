"""Local Ollama model transport.

This module contains only the local Ollama transport. The application talks
only to the Ollama service configured on the same host.
"""
import json
import threading
import time
import urllib.error
import urllib.request

from services.model_output import ModelOutputError, extract_json_value, validate_json_object


class LocalModelError(RuntimeError):
    """Raised when the local model or its structured output is unavailable."""


class LocalModelClient:
    """Small transport contract shared by local Ollama generation paths."""

    def __init__(self, base_url, model, timeout=180, max_concurrency=1):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout = max(10, int(timeout))
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)

    @property
    def requires_confirmation(self):
        return False

    @property
    def privacy_label(self):
        return "服务器本机 Ollama（全程本地处理）"

    @property
    def configured(self):
        return bool(self.base_url and self.model)

    def chat(self, system_prompt, user_prompt, temperature=0.2, max_tokens=1800,
             retries=2, timeout=None):
        return self._request({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }, retries=retries, timeout=timeout)

    @staticmethod
    def _retry_limit(retries):
        try:
            return max(0, min(5, int(retries)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _retryable_http(code):
        return int(code) in {408, 425, 429, 500, 502, 503, 504}

    @staticmethod
    def _backoff(attempt):
        # Keep a local restart from creating a busy retry storm.  The model is
        # local, so a short bounded delay is enough; no unbounded sleep here.
        time.sleep(min(0.5 * (2 ** max(0, int(attempt))), 4.0))

    @staticmethod
    def _parse_json_content(content, required_fields=None, context="模型输出"):
        try:
            return validate_json_object(
                extract_json_value(content),
                required_fields=required_fields,
                context=context,
            )
        except ModelOutputError as exc:
            raise LocalModelError(str(exc)) from exc


class OllamaClient(LocalModelClient):
    """Native client for the server's local Ollama service."""

    def __init__(self, base_url, model, timeout=180, max_concurrency=1):
        # Ollama's loaded model is shared by all requests.  A Python setting of
        # 2+ can create concurrent KV caches and exhaust the shared GPU even
        # though the process itself remains alive.  Keep this backend serial.
        super().__init__(base_url, model, timeout=timeout, max_concurrency=max_concurrency)

    @property
    def configured(self):
        # Configuration is local and does not require a secret. Connectivity is
        # checked only when the user actually tests or invokes the model.
        return bool(self.base_url and self.model)

    @property
    def requires_confirmation(self):
        return False

    @property
    def privacy_label(self):
        return "服务器本机 Ollama（不出网）"

    def health_check(self, timeout=5):
        request = urllib.request.Request(self.base_url + "/models", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            models = [item.get("id") or item.get("name") for item in data.get("data", [])]
            return {"reachable": True, "model_available": self.model in models, "models": models}
        except Exception as exc:
            return {"reachable": False, "model_available": False, "models": [], "error": str(exc)}

    def _request(self, payload, retries=2, timeout=None):
        """Call native Ollama chat with thinking disabled.

        Qwen can put all limited output in the compatibility endpoint's
        reasoning field.  The native endpoint honors ``think: false`` and
        returns the usable answer in ``message.content``.
        """
        native_base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        body = json.dumps({
            "model": payload["model"],
            "messages": payload["messages"],
            "stream": False,
            "think": False,
            "options": {
                "temperature": payload.get("temperature", 0.1),
                "num_predict": max(1, int(payload.get("max_tokens", 1800))),
            },
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            native_base.rstrip("/") + "/api/chat",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "DataAnalysisAgentDemo/local-ollama",
            },
            method="POST",
        )
        effective_timeout = max(1, int(timeout if timeout is not None else self.timeout))
        attempts = LocalModelClient._retry_limit(retries) + 1
        for attempt in range(attempts):
            try:
                with self._semaphore:
                    with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                        data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                message = "Ollama HTTP {}: {}".format(exc.code, detail[:500])
                if not LocalModelClient._retryable_http(exc.code) or attempt + 1 >= attempts:
                    raise LocalModelError(message) from exc
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                message = "本机 Ollama 调用失败：{}".format(exc)
                if attempt + 1 >= attempts:
                    raise LocalModelError(message) from exc
            LocalModelClient._backoff(attempt)

        message = data.get("message", {})
        content = (message.get("content") or "").strip()
        if not content:
            raise LocalModelError("Ollama 未返回最终答案")
        return {
            "content": content,
            "reasoning_content": message.get("thinking") or message.get("reasoning"),
            "model": data.get("model", self.model),
            "usage": {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            },
            "finish_reason": data.get("done_reason") or ("stop" if data.get("done") else None),
        }

    def _stream_request(self, payload, timeout=None, retries=2):
        # The compatibility endpoint ignores `think: false` for the shared
        # Qwen model. Use Ollama's native endpoint, where the switch is honored.
        native_base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        native_url = native_base.rstrip("/") + "/api/chat"
        body_payload = {
            "model": payload["model"],
            "messages": payload["messages"],
            "stream": True,
            "think": False,
            "format": "json",
            "options": {
                "temperature": payload.get("temperature", 0.1),
                "num_predict": max(1024, int(payload.get("max_tokens", 1800))),
            },
        }
        body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            native_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
                "User-Agent": "DataAnalysisAgentDemo/local-ollama",
            },
            method="POST",
        )
        attempts = LocalModelClient._retry_limit(retries) + 1
        effective_timeout = max(1, int(timeout if timeout is not None else self.timeout))
        for attempt in range(attempts):
            content_parts = []
            reasoning_parts = []
            model = self.model
            usage = {}
            finish_reason = None
            try:
                with self._semaphore:
                    with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                        for raw_line in response:
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line:
                                continue
                            data = json.loads(line)
                            model = data.get("model", model)
                            if data.get("prompt_eval_count") is not None or data.get("eval_count") is not None:
                                usage = {
                                    "prompt_tokens": data.get("prompt_eval_count", 0),
                                    "completion_tokens": data.get("eval_count", 0),
                                }
                            message = data.get("message", {})
                            if message.get("content"):
                                content_parts.append(message["content"])
                            reasoning = message.get("thinking") or message.get("reasoning")
                            if reasoning:
                                reasoning_parts.append(reasoning)
                            if data.get("done"):
                                finish_reason = data.get("done_reason") or "stop"
                                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                message = "Ollama HTTP {}: {}".format(exc.code, detail[:500])
                if not LocalModelClient._retryable_http(exc.code) or attempt + 1 >= attempts:
                    raise LocalModelError(message) from exc
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                message = "本机 Ollama 流式调用失败：{}".format(exc)
                if attempt + 1 >= attempts:
                    raise LocalModelError(message) from exc
            else:
                content = "".join(content_parts).strip()
                if content:
                    return {
                        "content": content,
                        "reasoning_content": "".join(reasoning_parts).strip() or None,
                        "model": model,
                        "usage": usage,
                        "finish_reason": finish_reason,
                    }
                reasoning = "".join(reasoning_parts).strip()
                message = (
                    "Ollama 在限定时间内只完成了思考，尚未输出最终答案"
                    if reasoning else "Ollama 未返回任何输出"
                )
                if attempt + 1 >= attempts:
                    raise LocalModelError(message)
            LocalModelClient._backoff(attempt)
        raise LocalModelError("本机 Ollama 流式调用失败")

    def chat_json(self, system_prompt, user_prompt, max_tokens=2400, strict=True, retries=2, timeout=None, required_fields=None, output_context="模型输出"):
        prompt = system_prompt + "\n你必须只输出一个合法 JSON 对象，不要输出 Markdown 代码围栏。"
        result = self._stream_request({
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            # With thinking disabled, honor the bounded output budget. This
            # keeps folder and report summaries responsive on the shared GPU.
            "max_tokens": max(1024, max_tokens),
        }, timeout=timeout, retries=retries)
        try:
            result["json"] = self._parse_json_content(
                result["content"], required_fields=required_fields, context=output_context
            )
            return result
        except (ValueError, LocalModelError) as exc:
            raise LocalModelError("本机 Ollama 未返回合法 JSON：{}".format(exc))


class OllamaEmbeddingClient:
    """Bounded client for Ollama's small local embedding model."""

    def __init__(self, base_url, model, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = max(10, int(timeout))
        self._semaphore = threading.BoundedSemaphore(1)

    @property
    def configured(self):
        return bool(self.base_url and self.model)

    def embed(self, texts, retries=2):
        values = [str(value or "").strip()[:1800] for value in texts]
        if not values:
            return []
        native_base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        body = json.dumps({
            "model": self.model,
            "input": values,
            "keep_alive": "30s",
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            native_base.rstrip("/") + "/api/embed",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        attempts = LocalModelClient._retry_limit(retries) + 1
        for attempt in range(attempts):
            try:
                with self._semaphore:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        data = json.loads(response.read().decode("utf-8"))
                embeddings = data.get("embeddings")
                if not isinstance(embeddings, list) or len(embeddings) != len(values):
                    raise LocalModelError("embedding 服务返回数量不一致")
                return embeddings
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                message = "本机 embedding HTTP {}：{}".format(exc.code, detail[:300])
                if not LocalModelClient._retryable_http(exc.code) or attempt + 1 >= attempts:
                    raise LocalModelError(message) from exc
            except (urllib.error.URLError, TimeoutError, ValueError, OSError, LocalModelError) as exc:
                message = "本机 embedding 调用失败：{}".format(exc)
                if attempt + 1 >= attempts:
                    raise LocalModelError(message) from exc
            LocalModelClient._backoff(attempt)
        raise LocalModelError("本机 embedding 调用失败")
