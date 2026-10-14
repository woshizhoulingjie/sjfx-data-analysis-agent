"""Conservative adapter for an explicitly approved OpenAI chat server."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from services.model_contract import (LocalModelClient, LocalModelError, ModelCapabilities,
    bounded_timeout, read_error_detail, open_local_request, read_json_response, parse_openai_chat,
    safe_http_error, transport_error)
from services.model_dispatch import serialized_generation


class OpenAICompatibleClient(LocalModelClient):
    backend_name = "openai_compatible"
    supports_json_schema = False

    def __init__(self, base_url, model, timeout=180, max_concurrency=1, api_key=None,
                 supports_json_response_format=False, queue_timeout=15, *, allowed_endpoints=(),
                 budget_config=None, context_tokens=0):
        super().__init__(base_url, model, timeout, max_concurrency, queue_timeout,
                         allowed_endpoints=allowed_endpoints, budget_config=budget_config, context_tokens=context_tokens)
        self.api_key = str(api_key or "").strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in self.api_key):
            raise LocalModelError("模型认证配置包含无效字符", code="configuration_error")
        self.supports_json_response_format = bool(supports_json_response_format)
        self.capabilities = ModelCapabilities(json_response_format=self.supports_json_response_format)

    @property
    def privacy_label(self):
        return super().privacy_label

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "SJFX/local-openai-compatible"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def health_check(self, timeout=5):
        request = urllib.request.Request(self.base_url + "/models", headers=self._headers(), method="GET")
        try:
            with open_local_request(request, timeout, allowed_endpoints=self.allowed_endpoints) as response:
                data = read_json_response(response)
            rows = data.get("data")
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows):
                raise LocalModelError("本机服务模型列表结构无效", code="invalid_response")
            models = [row["id"] for row in rows]
            return {"reachable": True, "model_available": self.model in models, "models": models}
        except urllib.error.HTTPError as exc:
            error = safe_http_error(self.backend_name, exc.code, read_error_detail(exc))
        except LocalModelError as exc:
            error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = transport_error(self.backend_name, exc)
        return {"reachable": False, "model_available": False, "models": [], "error": str(error), "error_code": error.code}

    @serialized_generation
    def chat(self, system_prompt, user_prompt, temperature=0.2, max_tokens=1800,
             retries=2, timeout=None, response_format=None):
        if response_format is not None and not self.supports_json_response_format:
            raise LocalModelError("所选本机模型未声明 response_format 能力", code="unsupported_capability")
        budget = self._request_budget(system_prompt, user_prompt, max_tokens)
        payload = {"model": self.model, "messages": [
            {"role": "system", "content": str(system_prompt or "")},
            {"role": "user", "content": str(user_prompt or "")},
        ], "temperature": temperature, "max_tokens": budget['max_tokens'], "stream": False}
        if response_format is not None:
            payload["response_format"] = response_format
        request = urllib.request.Request(self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"), headers=self._headers(), method="POST")
        effective_timeout = min(bounded_timeout(timeout, self.timeout), self.timeout)
        attempts = self._retry_limit(retries) + 1
        for attempt in range(attempts):
            try:
                with self._admission(effective_timeout):
                    with open_local_request(request, effective_timeout, allowed_endpoints=self.allowed_endpoints) as response:
                        data = read_json_response(response)
                return self._budget_result(parse_openai_chat(data, self.model), budget)
            except urllib.error.HTTPError as exc:
                detail = read_error_detail(exc)
                error = safe_http_error(self.backend_name, exc.code, detail)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = transport_error(self.backend_name, exc)
            if not error.retryable or attempt + 1 >= attempts:
                raise error from None
            self._backoff(attempt)
