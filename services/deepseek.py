import json
import threading
import time
import urllib.error
import urllib.request


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, api_key, base_url, model, timeout=180, max_concurrency=3):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)

    @property
    def requires_confirmation(self):
        return True

    @property
    def privacy_label(self):
        return "DeepSeek 云端 API"

    @property
    def configured(self):
        return bool(self.api_key)

    def _request(self, payload, retries=2, timeout=None):
        if not self.api_key:
            raise DeepSeekError("尚未配置 DEEPSEEK_API_KEY")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "DataAnalysisAgentDemo/0.1",
            },
            method="POST",
        )
        last_error = None
        for attempt in range(retries + 1):
            try:
                with self._semaphore:
                    with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                        raw = response.read().decode("utf-8")
                data = json.loads(raw)
                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})
                content = message.get("content")
                if not content:
                    raise DeepSeekError("模型返回了空内容")
                return {
                    "content": content,
                    "reasoning_content": message.get("reasoning_content"),
                    "model": data.get("model", self.model),
                    "usage": data.get("usage", {}),
                    "finish_reason": choice.get("finish_reason"),
                }
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = DeepSeekError("DeepSeek API HTTP {}: {}".format(exc.code, detail[:500]))
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, ValueError, DeepSeekError) as exc:
                last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        raise DeepSeekError(str(last_error))

    def chat(self, system_prompt, user_prompt, temperature=0.2, max_tokens=1800, retries=2, timeout=None):
        return self._request({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "thinking": {"type": "disabled"},
            "stream": False,
        }, retries=retries, timeout=timeout)

    @staticmethod
    def _parse_json_content(content):
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].rstrip()
        try:
            return json.loads(cleaned)
        except ValueError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                return json.loads(cleaned[start:end + 1])
            raise

    def chat_json(self, system_prompt, user_prompt, max_tokens=2400, strict=True, retries=2, timeout=None):
        prompt = system_prompt + "\n你必须只输出一个合法 JSON 对象，不要输出 Markdown 代码围栏。"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        if strict:
            payload["response_format"] = {"type": "json_object"}
        last_error = None
        for json_attempt in range(2 if retries > 0 else 1):
            result = self._request(
                payload,
                retries=retries if json_attempt == 0 else 0,
                timeout=timeout,
            )
            try:
                result["json"] = self._parse_json_content(result["content"])
                return result
            except ValueError as exc:
                last_error = exc
        raise DeepSeekError("模型未返回合法 JSON: {}".format(last_error))


class OllamaClient(DeepSeekClient):
    """OpenAI-compatible client for the server's existing local Ollama service."""

    def __init__(self, api_key, base_url, model, timeout=180, max_concurrency=1):
        # Ollama's loaded model is shared by all requests.  A Python setting of
        # 2+ can create concurrent KV caches and exhaust the shared GPU even
        # though the process itself remains alive.  Keep this backend serial.
        super().__init__(api_key, base_url, model, timeout=timeout, max_concurrency=1)

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

        Qwen can put all limited output in the OpenAI-compatible endpoint's
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
        try:
            with self._semaphore:
                with urllib.request.urlopen(request, timeout=max(timeout or 0, self.timeout)) as response:
                    data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DeepSeekError("Ollama HTTP {}: {}".format(exc.code, detail[:500]))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise DeepSeekError("本机 Ollama 调用失败：{}".format(exc))

        message = data.get("message", {})
        content = (message.get("content") or "").strip()
        if not content:
            raise DeepSeekError("Ollama 未返回最终答案")
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

    def _stream_request(self, payload, timeout=None):
        # The OpenAI-compatible endpoint ignores `think: false` for the shared
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
        content_parts = []
        reasoning_parts = []
        model = self.model
        usage = {}
        finish_reason = None
        effective_timeout = max(timeout or 0, self.timeout)
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
            raise DeepSeekError("Ollama HTTP {}: {}".format(exc.code, detail[:500]))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise DeepSeekError("本机 Ollama 流式调用失败：{}".format(exc))

        content = "".join(content_parts).strip()
        if not content:
            reasoning = "".join(reasoning_parts).strip()
            if reasoning:
                raise DeepSeekError("Ollama 在限定时间内只完成了思考，尚未输出最终答案")
            raise DeepSeekError("Ollama 未返回任何输出")
        return {
            "content": content,
            "reasoning_content": "".join(reasoning_parts).strip() or None,
            "model": model,
            "usage": usage,
            "finish_reason": finish_reason,
        }

    def chat_json(self, system_prompt, user_prompt, max_tokens=2400, strict=True, retries=2, timeout=None):
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
        }, timeout=timeout)
        try:
            result["json"] = self._parse_json_content(result["content"])
            return result
        except ValueError as exc:
            raise DeepSeekError("本机 Ollama 未返回合法 JSON：{}".format(exc))


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

    def embed(self, texts):
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
        try:
            with self._semaphore:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
            embeddings = data.get("embeddings")
            if not isinstance(embeddings, list) or len(embeddings) != len(values):
                raise DeepSeekError("embedding 服务返回数量不一致")
            return embeddings
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError, DeepSeekError) as exc:
            raise DeepSeekError("本机 embedding 调用失败：{}".format(exc))
