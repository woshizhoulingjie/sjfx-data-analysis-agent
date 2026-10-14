"""Small, safe contract for administrator-selected HTTP model transports.

No inference runtime is imported here. URLs and diagnostics never carry secrets;
HTTP uses a private opener that ignores proxies and refuses all redirects.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import http.client
import json
import math
import socket
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request

from services.model_output import ModelOutputError, extract_json_value, validate_json_object


class LocalModelError(RuntimeError):
    """Stable public failure with safe text and machine-readable metadata."""

    def __init__(self, message, *, code="model_error", retryable=False, status=None):
        super().__init__(message)
        self.code = code
        self.retryable = bool(retryable)
        self.status = status


from services.model_endpoint_policy import (validate_model_url, normalize_allowed_endpoints,
    parse_model_allowed_endpoints, model_url_origin, is_loopback_model_url, resolve_model_addresses)


def validate_local_url(raw):
    value = str(raw or "").strip()
    try:
        if not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or "?" in value or "#" in value or "\\" in value):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        # Keep explicit loopback spellings; reject parser ambiguities and
        # encoded authority/path delimiters rather than silently rewriting them.
        if "%" in parsed.netloc or any(token in parsed.path.lower() for token in ("%2f", "%5c", "%3f", "%23")):
            raise ValueError
    except (ValueError, TypeError):
        raise LocalModelError(
            "模型地址必须是本机 HTTP(S) 地址（127.0.0.1/localhost/::1），且不能包含认证、查询或片段",
            code="configuration_error",
        ) from None
    return value.rstrip("/")


def validate_model_name(model):
    value = str(model or "").strip()
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise LocalModelError("必须配置有效的模型名称", code="configuration_error")
    return value


def bounded_timeout(value, default=180, maximum=3600):
    try:
        timeout = float(default if value is None else value)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise LocalModelError("模型超时必须是有限正数", code="configuration_error") from None
    return min(timeout, maximum)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise LocalModelError("模型服务返回重定向，已拒绝转发", code="redirect_refused", status=code)


class _HeaderDeadline:
    """Abort a socket whose response headers trickle past the total deadline."""
    def __init__(self, *args, deadline, addresses, **kwargs):
        self._sjfx_deadline = deadline
        self._sjfx_addresses = addresses
        super().__init__(*args, **kwargs)
        self._create_connection = self._connect_verified

    def _connect_verified(self, target, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        # target preserves the HTTP Host and HTTPS certificate/SNI name. Socket
        # connection uses the vetted sockaddr directly; DNS is never repeated.
        last_error = None
        for family, kind, protocol, _, address in self._sjfx_addresses:
            remaining = self._sjfx_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('model connection deadline')
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(remaining)
                if source_address:
                    sock.bind(source_address)
                sock.connect(address)
                return sock
            except OSError as error:
                last_error = error
                sock.close()
        if last_error is not None:
            raise last_error
        raise OSError('no verified model addresses')

    def connect(self):
        remaining = self._sjfx_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('model connection deadline')
        expired = threading.Event()

        def abort():
            expired.set()
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(remaining, abort)
        timer.daemon = True
        timer.start()
        try:
            super().connect()
            if expired.is_set():
                self.close()
                raise TimeoutError('model connection deadline')
        except OSError:
            if expired.is_set():
                raise TimeoutError('model connection deadline') from None
            raise
        finally:
            timer.cancel()

    def getresponse(self):
        remaining = self._sjfx_deadline - time.monotonic()
        if remaining <= 0:
            self.close()
            raise TimeoutError("local model header deadline")
        expired = threading.Event()

        def abort():
            expired.set()
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(remaining, abort)
        timer.daemon = True
        timer.start()
        try:
            response = super().getresponse()
            if expired.is_set():
                response.close()
                raise TimeoutError("local model header deadline")
            return response
        except (http.client.HTTPException, OSError):
            if expired.is_set():
                raise TimeoutError("local model header deadline") from None
            raise
        finally:
            timer.cancel()


class _LocalHTTPConnection(_HeaderDeadline, http.client.HTTPConnection):
    pass


class _LocalHTTPSConnection(_HeaderDeadline, http.client.HTTPSConnection):
    pass


class _LocalHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline, addresses):
        super().__init__()
        self.deadline = deadline
        self.addresses = addresses

    def http_open(self, request):
        return self.do_open(lambda *args, **kwargs: _LocalHTTPConnection(*args, deadline=self.deadline, addresses=self.addresses, **kwargs), request)


class _LocalHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline, addresses):
        super().__init__()
        self.deadline = deadline
        self.addresses = addresses

    def https_open(self, request):
        return self.do_open(lambda *args, **kwargs: _LocalHTTPSConnection(*args, deadline=self.deadline, addresses=self.addresses, **kwargs), request, context=self._context)


def open_local_request(request, timeout, allowed_endpoints=()):
    validate_model_url(request.full_url, allowed_endpoints)
    from services.model_dispatch import generation_timeout
    limit = generation_timeout(bounded_timeout(timeout))
    deadline = time.monotonic() + limit
    addresses = resolve_model_addresses(request.full_url, deadline)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
        _LocalHTTPHandler(deadline, addresses), _LocalHTTPSHandler(deadline, addresses))
    try:
        response = opener.open(request, timeout=limit)
    except urllib.error.HTTPError as exc:
        exc._sjfx_deadline = deadline
        raise
    except http.client.HTTPException:
        raise LocalModelError("本机模型 HTTP 响应无效", code="invalid_response") from None
    response._sjfx_deadline = deadline
    return response


def _response_remaining(response):
    deadline = getattr(response, "_sjfx_deadline", None)
    if deadline is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("local model response deadline")
    fp = getattr(response, "fp", None)
    sock = getattr(getattr(fp, "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(remaining)


def read_response_bytes(response, max_bytes=16 * 1024 * 1024):
    """Bound total body time and allocation, including a slow chunked body."""
    reader = getattr(response, "read1", None)
    if reader is None:
        _response_remaining(response)
        raw = response.read(max_bytes + 1)
    else:
        chunks, size = [], 0
        while True:
            _response_remaining(response)
            try:
                chunk = reader(min(65536, max_bytes + 1 - size))
            except http.client.HTTPException:
                raise LocalModelError("本机模型 HTTP 响应不完整", code="invalid_response") from None
            if not chunk:
                if getattr(response, "length", 0):
                    raise LocalModelError("本机模型 HTTP 响应不完整", code="invalid_response")
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                break
        raw = b"".join(chunks)
    if len(raw) > max_bytes:
        raise LocalModelError("本机模型响应超过大小限制", code="invalid_response")
    return raw


def read_error_detail(error):
    if error.code in {401, 403}:
        error.close()
        return ""
    try:
        return read_response_bytes(error, 65536).decode("utf-8", errors="replace")
    except (LocalModelError, TimeoutError, OSError):
        return ""
    finally:
        error.close()


def iter_response_lines(response, max_bytes=16 * 1024 * 1024):
    # HTTPResponse.readline() can receive an endless line one byte at a time;
    # read1 returns after one socket read so the total deadline is rechecked.
    reader = getattr(response, "read1", None) or response.read
    size, pending = 0, b""
    while True:
        _response_remaining(response)
        try:
            chunk = reader(min(65536, max_bytes + 1 - size))
        except http.client.HTTPException:
            raise LocalModelError("本机模型流式 HTTP 响应不完整", code="invalid_response") from None
        if not chunk:
            if getattr(response, "length", 0):
                raise LocalModelError("本机模型流式 HTTP 响应不完整", code="invalid_response")
            if pending:
                yield pending
            return
        size += len(chunk)
        if size > max_bytes:
            raise LocalModelError("本机模型流式响应超过大小限制", code="invalid_response")
        pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        for line in lines:
            yield line


def read_json_response(response, max_bytes=16 * 1024 * 1024):
    try:
        raw = read_response_bytes(response, max_bytes)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeError, TypeError):
        raise LocalModelError("本机模型返回无效 JSON 响应", code="invalid_response") from None


def safe_http_error(backend, status, detail=""):
    status = int(status)
    text = str(detail).lower()
    retryable = status in {408, 425, 429, 500, 502, 503, 504}
    if status in {401, 403}:
        code, reason = "authentication_error", "认证失败，请检查本机服务凭据"
    elif status == 404:
        code, reason = "model_unavailable", "模型或服务接口不可用"
    elif status == 429 or any(token in text for token in ("out of memory", "cuda oom", "resource exhausted", "resource_exhausted", "insufficient memory")):
        code, reason = "resource_exhausted", "模型服务资源不足或繁忙，请稍后重试"
    elif status in {408, 504}:
        code, reason = "timeout", "请求超时，请稍后重试"
    elif status >= 500:
        code, reason = "unavailable", "本机服务暂不可用，请检查服务或稍后重试"
    elif status == 400 and any(token in text for token in ("context length", "context window", "too many tokens")):
        code, reason = "context_limit", "输入超出模型上下文限制，需要调用方分片"
    else:
        code, reason = "request_error", "请求被本机服务拒绝，请检查模型配置和能力"
    return LocalModelError("{} HTTP {}：{}".format(backend, status, reason), code=code, retryable=retryable, status=status)


def transport_error(backend, exc):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return LocalModelError("本机 {} 调用超时，请稍后重试".format(backend), code="timeout", retryable=True)
    return LocalModelError("本机 {} 服务连接失败，请检查服务是否已启动".format(backend), code="unavailable", retryable=True)


@dataclass(frozen=True)
class ModelCapabilities:
    json_response_format: bool = False
    native_json: bool = False
    streaming: bool = False
    thinking_control: bool = False
    trace_metadata: bool = False
    exact_context_budget: bool = False


class LocalModelClient:
    """Shared generation surface and bounded client admission."""
    backend_name = "local"
    supports_json_response_format = False
    capabilities = ModelCapabilities()

    def __init__(self, base_url, model, timeout=180, max_concurrency=1, queue_timeout=15,
                 *, allowed_endpoints=(), budget_config=None, context_tokens=0):
        self.allowed_endpoints = normalize_allowed_endpoints(allowed_endpoints)
        self.base_url = validate_model_url(base_url, self.allowed_endpoints)
        self.budget_config = budget_config
        self.context_tokens = max(0, int(context_tokens or 0))
        self.model = validate_model_name(model)
        self.timeout = bounded_timeout(timeout)
        self.max_concurrency = max(1, min(8, int(max_concurrency)))
        self.queue_timeout = bounded_timeout(queue_timeout, maximum=60)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)

    @property
    def configured(self):
        return bool(self.base_url and self.model)

    @property
    def requires_confirmation(self):
        return False

    @property
    def privacy_label(self):
        name = {'vllm': 'vLLM', 'ollama': 'Ollama', 'openai_compatible': 'OpenAI-compatible'}.get(self.backend_name, '模型')
        if is_loopback_model_url(self.base_url):
            return '服务器本机 {}（不出网）'.format(name)
        return '已授权远端 {}（数据发送至配置的模型端点）'.format(name)

    def _request_budget(self, system_prompt, user_prompt, max_tokens):
        if self.budget_config is None and not self.context_tokens:
            return {'max_tokens': max(1, int(max_tokens)), 'context_tokens': self.context_tokens}
        from services.model_budget import request_budget
        return request_budget(system_prompt, user_prompt, max_tokens,
                              config=self.budget_config if self.budget_config is not None else
                                  SimpleNamespace(LLM_CONTEXT_TOKENS=self.context_tokens), model=self)

    def _messages_budget(self, messages, max_tokens):
        if (not isinstance(messages, list) or not messages
                or any(not isinstance(message, dict) or not isinstance(message.get('content'), str)
                       for message in messages)):
            raise LocalModelError('模型消息结构无效', code='configuration_error')
        system = '\n'.join(message['content'] for message in messages if message.get('role') == 'system')
        user = '\n'.join(message['content'] for message in messages if message.get('role') != 'system')
        return self._request_budget(system, user, max_tokens)

    def _budget_result(self, result, budget):
        if self.budget_config is not None or self.context_tokens:
            result.setdefault('context_budget', budget)
        return result

    def _preflight_generation(self, parameters):
        """Reject invalid complete inputs before any shared generation queue."""
        if 'payload' in parameters:
            payload = parameters['payload']
            if not isinstance(payload, dict):
                raise LocalModelError('模型请求结构无效', code='configuration_error')
            return self._messages_budget(payload.get('messages'), payload.get('max_tokens', 1800))
        return self._request_budget(parameters.get('system_prompt'), parameters.get('user_prompt'),
                                    parameters.get('max_tokens', 1800))

    @contextmanager
    def _admission(self, timeout=None):
        from services.model_dispatch import acquire_generation_semaphore
        if not acquire_generation_semaphore(self._semaphore, min(self.queue_timeout, bounded_timeout(timeout, self.timeout))):
            raise LocalModelError("本机模型正在处理其他请求，请稍后重试", code="busy", retryable=True)
        try:
            yield
        finally:
            self._semaphore.release()

    @staticmethod
    def _retry_limit(retries):
        try:
            return max(0, min(5, int(retries)))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _retryable_http(code):
        return int(code) in {408, 425, 429, 500, 502, 503, 504}

    @staticmethod
    def _backoff(attempt):
        time.sleep(min(0.5 * (2 ** max(0, int(attempt))), 4.0))

    @staticmethod
    def _parse_json_content(content, required_fields=None, context="模型输出"):
        try:
            parsed = validate_json_object(extract_json_value(content), required_fields=required_fields, context=context)
            # Python's JSON decoder accepts NaN/Infinity; downstream analysis
            # and JSON persistence require the portable JSON number contract.
            json.dumps(parsed, allow_nan=False)
            return parsed
        except ModelOutputError as exc:
            raise LocalModelError(str(exc), code="invalid_structured_output") from None
        except (ValueError, TypeError, OverflowError):
            raise LocalModelError("模型结构化输出包含无效 JSON 数值", code="invalid_structured_output") from None

    def chat(self, system_prompt, user_prompt, temperature=0.2, max_tokens=1800, retries=2, timeout=None):
        return self._request({"model": self.model, "messages": [
            {"role": "system", "content": str(system_prompt or "")},
            {"role": "user", "content": str(user_prompt or "")},
        ], "temperature": temperature, "max_tokens": max_tokens, "stream": False}, retries=retries, timeout=timeout)

    def chat_json(self, system_prompt, user_prompt, max_tokens=2400, strict=True, retries=2,
                  timeout=None, required_fields=None, output_context="模型输出", long_output=False):
        kwargs = dict(temperature=0.1, max_tokens=max_tokens, retries=retries, timeout=timeout)
        if self.supports_json_response_format:
            kwargs["response_format"] = {"type": "json_object"}
        result = self.chat(str(system_prompt or "") + "\n你必须只输出一个合法 JSON 对象，不要输出 Markdown 代码围栏。", user_prompt, **kwargs)
        if result.get("finish_reason") == "length":
            raise LocalModelError("模型结构化输出被截断，请调整输出预算或分片", code="invalid_structured_output")
        result["json"] = self._parse_json_content(result["content"], required_fields, output_context)
        return result


def parse_openai_chat(data, model):
    try:
        if not isinstance(data, dict):
            raise ValueError
        choices = data["choices"]
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError
        message = choices[0]["message"]
        if not isinstance(message, dict):
            raise ValueError
        content = message.get("content")
        if isinstance(content, list):
            if not content or any(not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str) for p in content):
                raise ValueError
            content = "".join(p["text"] for p in content)
        if not isinstance(content, str) or not content.strip():
            raise ValueError
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError
        tokens = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError
            tokens[field] = value
        reported_model = data.get("model")
        if reported_model is not None and not isinstance(reported_model, str):
            raise ValueError
        result_model = reported_model or model
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        finish = choices[0].get("finish_reason")
        if not isinstance(result_model, str) or (reasoning is not None and not isinstance(reasoning, str)) or (finish is not None and not isinstance(finish, str)):
            raise ValueError
    except (KeyError, TypeError, ValueError, IndexError):
        raise LocalModelError("模型未返回有效最终答案或响应结构无效", code="invalid_response") from None
    if result_model != model:
        raise LocalModelError("模型响应与所配置模型不一致，请检查本机服务配置", code="invalid_response")
    return {"content": content.strip(), "reasoning_content": reasoning, "model": result_model,
            "usage": tokens, "finish_reason": finish}


def ollama_model_identity(name):
    return name if ':' in name.rsplit('/', 1)[-1] else name + ':latest'


def native_ollama_metadata(data, default_model):
    """Normalize native usage and timing without allowing malformed scalars."""
    try:
        reported_model = data.get("model")
        if reported_model is not None and not isinstance(reported_model, str):
            raise ValueError
        model = reported_model or default_model
        finish = data.get("done_reason") or ("stop" if data.get("done") else None)
        if not isinstance(model, str) or (finish is not None and not isinstance(finish, str)):
            raise ValueError
        usage = {}
        for key, source in (("prompt_tokens", "prompt_eval_count"), ("completion_tokens", "eval_count")):
            value = data.get(source, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError
            usage[key] = value
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        timing = {}
        for target, source in (("total_seconds", "total_duration"), ("load_seconds", "load_duration"), ("prefill_seconds", "prompt_eval_duration"), ("decode_seconds", "eval_duration")):
            value = data.get(source, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError
            timing[target] = value / 1e9
    except (TypeError, ValueError, OverflowError):
        raise LocalModelError("Ollama 响应统计结构无效", code="invalid_response") from None
    # Ollama may spell the default tag explicitly in its native response.
    if ollama_model_identity(model) != ollama_model_identity(default_model):
        raise LocalModelError("Ollama 响应与所配置模型不一致，请检查本机服务配置", code="invalid_response")
    return model, usage, finish, timing
