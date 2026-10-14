"""Native Ollama transport for loopback or approved remote model services."""
import json
import threading
import time
import urllib.error
import urllib.request

from services.model_dispatch import serialized_generation
from services.model_contract import (LocalModelClient, LocalModelError, ModelCapabilities,
    bounded_timeout, read_error_detail, open_local_request, read_json_response, safe_http_error, transport_error,
    native_ollama_metadata, ollama_model_identity, iter_response_lines)


class OllamaClient(LocalModelClient):
    """Native client for the server's local Ollama service."""

    backend_name = "ollama"
    supports_json_response_format = False
    supports_json_schema = False
    capabilities = ModelCapabilities(native_json=True, streaming=True, thinking_control=True)

    def __init__(self, base_url, model, timeout=180, max_concurrency=1, api_key=None, queue_timeout=15, *,
                 allowed_endpoints=(), budget_config=None, context_tokens=0):
        # Ollama's loaded model is shared by all requests.  A Python setting of
        # 2+ can create concurrent KV caches and exhaust the shared GPU even
        # though the process itself remains alive.  Keep this backend serial.
        super().__init__(base_url, model, timeout=timeout, max_concurrency=max_concurrency, queue_timeout=queue_timeout,
                         allowed_endpoints=allowed_endpoints, budget_config=budget_config, context_tokens=context_tokens)
        self.api_key = str(api_key or '').strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in self.api_key):
            raise LocalModelError('模型认证配置包含无效字符', code='configuration_error')

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
        return super().privacy_label

    def _headers(self, stream=False):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/x-ndjson' if stream else 'application/json', 'User-Agent': 'SJFX/local-ollama'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        return headers

    def health_check(self, timeout=5):
        native_base = self.base_url[:-3] if self.base_url.endswith('/v1') else self.base_url
        request = urllib.request.Request(native_base + '/api/tags', headers=self._headers(), method='GET')
        try:
            with open_local_request(request, timeout, allowed_endpoints=self.allowed_endpoints) as response:
                data = read_json_response(response)
            rows = data.get('models')
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('name') or row.get('model'), str) for row in rows):
                raise LocalModelError('本机服务模型列表结构无效', code='invalid_response')
            models = [row.get('name') or row['model'] for row in rows]
            return {'reachable': True, 'model_available': any(ollama_model_identity(name) == ollama_model_identity(self.model) for name in models), 'models': models}
        except urllib.error.HTTPError as exc:
            error = safe_http_error('Ollama', exc.code, read_error_detail(exc))
        except LocalModelError as exc:
            error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = transport_error('Ollama', exc)
        return {'reachable': False, 'model_available': False, 'models': [], 'error': str(error), 'error_code': error.code}

    @serialized_generation
    def _request(self, payload, retries=2, timeout=None):
        """Call native Ollama chat with thinking disabled.

        Qwen can put all limited output in the compatibility endpoint's
        reasoning field.  The native endpoint honors ``think: false`` and
        returns the usable answer in ``message.content``.
        """
        native_base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        messages = payload['messages']
        budget = self._messages_budget(messages, payload.get('max_tokens', 1800))
        body = json.dumps({
            "model": payload["model"],
            "messages": payload["messages"],
            "stream": False,
            "think": False,
            "options": {
                "temperature": payload.get("temperature", 0.1),
                "num_predict": budget['max_tokens'],
                **({'num_ctx': budget['context_tokens']} if budget['context_tokens'] else {}),
            },
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            native_base.rstrip("/") + "/api/chat",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        effective_timeout = min(bounded_timeout(timeout, self.timeout), self.timeout)
        attempts = LocalModelClient._retry_limit(retries) + 1
        for attempt in range(attempts):
            try:
                with self._admission(effective_timeout):
                    with open_local_request(request, effective_timeout, allowed_endpoints=self.allowed_endpoints) as response:
                        data = read_json_response(response)
                break
            except urllib.error.HTTPError as exc:
                detail = read_error_detail(exc)
                error = safe_http_error("Ollama", exc.code, detail)
                if not LocalModelClient._retryable_http(exc.code) or attempt + 1 >= attempts:
                    raise error from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = transport_error("Ollama", exc)
                if attempt + 1 >= attempts:
                    raise error from None
            LocalModelClient._backoff(attempt)

        message = data.get("message", {})
        if not isinstance(message, dict) or not isinstance(message.get('content'), str):
            raise LocalModelError('Ollama 响应结构无效', code='invalid_response')
        content = message['content'].strip()
        if not content:
            raise LocalModelError("Ollama 未返回最终答案", code="invalid_response")
        model, usage, finish_reason, timing = native_ollama_metadata(data, self.model)
        reasoning = message.get("thinking") or message.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, str):
            raise LocalModelError("Ollama 响应思考字段无效", code="invalid_response")
        return self._budget_result({
            "content": content,
            "reasoning_content": reasoning,
            "model": model,
            "usage": usage,
            "finish_reason": finish_reason,
            "timing": timing,
        }, budget)

    @serialized_generation
    def _stream_request(self, payload, timeout=None, retries=2):
        # The compatibility endpoint ignores `think: false` for the shared
        # Qwen model. Use Ollama's native endpoint, where the switch is honored.
        native_base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        native_url = native_base.rstrip("/") + "/api/chat"
        messages = payload['messages']
        budget = self._messages_budget(messages, payload.get('max_tokens', 1800))
        body_payload = {
            "model": payload["model"],
            "messages": payload["messages"],
            "stream": True,
            "think": False,
            "format": "json",
            "options": {
                "temperature": payload.get("temperature", 0.1),
                "num_predict": budget['max_tokens'],
                **({'num_ctx': budget['context_tokens']} if budget['context_tokens'] else {}),
            },
        }
        body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            native_url,
            data=body,
            headers=self._headers(stream=True),
            method="POST",
        )
        attempts = LocalModelClient._retry_limit(retries) + 1
        effective_timeout = min(bounded_timeout(timeout, self.timeout), self.timeout)
        for attempt in range(attempts):
            started = time.monotonic()
            read_bytes = 0
            completed = False
            content_parts = []
            reasoning_parts = []
            model = self.model
            usage = {}
            finish_reason = None
            timing = {}
            try:
                with self._admission(effective_timeout):
                    with open_local_request(request, effective_timeout, allowed_endpoints=self.allowed_endpoints) as response:
                        for raw_line in iter_response_lines(response):
                            read_bytes += len(raw_line)
                            if read_bytes > 16 * 1024 * 1024 or time.monotonic() - started > effective_timeout:
                                raise LocalModelError('Ollama 流式响应超过限定时间或大小', code='invalid_response')
                            try:
                                line = raw_line.decode("utf-8").strip()
                            except UnicodeError:
                                raise LocalModelError("Ollama 流式响应字符编码无效", code="invalid_response") from None
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except ValueError:
                                raise LocalModelError('Ollama 流式响应 JSON 无效', code='invalid_response') from None
                            if not isinstance(data, dict):
                                raise LocalModelError('Ollama 流式响应结构无效', code='invalid_response')
                            if 'error' in data:
                                raise LocalModelError('Ollama 流式服务返回错误，请检查服务资源', code='unavailable', retryable=True)
                            parsed_model, parsed_usage, parsed_finish, parsed_timing = native_ollama_metadata(data, model)
                            model = parsed_model
                            if data.get("prompt_eval_count") is not None or data.get("eval_count") is not None:
                                usage = parsed_usage
                            message = data.get("message", {})
                            if not isinstance(message, dict) or any(message.get(field) is not None and not isinstance(message.get(field), str) for field in ('content', 'thinking', 'reasoning')):
                                raise LocalModelError('Ollama 流式响应消息无效', code='invalid_response')
                            if message.get("content"):
                                content_parts.append(message["content"])
                            reasoning = message.get("thinking") or message.get("reasoning")
                            if reasoning:
                                reasoning_parts.append(reasoning)
                            if data.get("done"):
                                completed = True
                                finish_reason = parsed_finish
                                timing = parsed_timing
                                break
            except urllib.error.HTTPError as exc:
                detail = read_error_detail(exc)
                error = safe_http_error("Ollama", exc.code, detail)
                if not LocalModelClient._retryable_http(exc.code) or attempt + 1 >= attempts:
                    raise error from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = transport_error("Ollama", exc)
                if attempt + 1 >= attempts:
                    raise error from None
            else:
                content = "".join(content_parts).strip()
                if content and completed:
                    return self._budget_result({
                        "content": content,
                        "reasoning_content": "".join(reasoning_parts).strip() or None,
                        "model": model,
                        "usage": usage,
                        "finish_reason": finish_reason,
                        "timing": timing,
                    }, budget)
                reasoning = "".join(reasoning_parts).strip()
                message = (
                    "Ollama 在限定时间内只完成了思考，尚未输出最终答案"
                    if reasoning else "Ollama 未返回任何输出"
                )
                if attempt + 1 >= attempts:
                    raise LocalModelError(message, code="invalid_response")
            LocalModelClient._backoff(attempt)
        raise LocalModelError("本机 Ollama 流式调用失败")

    def chat_json(self, system_prompt, user_prompt, max_tokens=2400, strict=True, retries=2, timeout=None, required_fields=None, output_context="模型输出", long_output=False):
        prompt = str(system_prompt or "") + "\n你必须只输出一个合法 JSON 对象，不要输出 Markdown 代码围栏。"
        result = self._stream_request({
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            # With thinking disabled, honor the bounded output budget. This
            # keeps folder and report summaries responsive on the shared GPU.
            "max_tokens": max(1, max_tokens),
        }, timeout=timeout, retries=retries)
        if result.get('finish_reason') == 'length':
            raise LocalModelError('Ollama 结构化输出被截断', code='invalid_structured_output')
        try:
            result["json"] = self._parse_json_content(
                result["content"], required_fields=required_fields, context=output_context
            )
            return result
        except (ValueError, LocalModelError) as exc:
            raise LocalModelError("本机 Ollama 未返回合法 JSON：{}".format(exc), code="invalid_structured_output") from None


# Historical imports remain supported without a second implementation.
from services.embedding_backend import OllamaEmbeddingClient
