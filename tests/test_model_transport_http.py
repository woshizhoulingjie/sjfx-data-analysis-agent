"""Generation contracts exercised against real loopback HTTP, no model runtime."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace
import urllib.request

import pytest

from services.model_backend import create_model_client, create_translation_client
from services.model_contract import LocalModelError, validate_local_url
from services.ollama import OllamaClient
from services.openai_compatible import OpenAICompatibleClient
from services.vllm import VLLMClient


def openai_reply(content="最终答案", **extra):
    data = {"model": "fixture", "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
    data.update(extra)
    return data


@contextmanager
def http_service(replies, port=0):
    state = SimpleNamespace(replies=list(replies), requests=[])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def log_message(self, *args):
            pass

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.requests.append({"path": self.path, "method": self.command,
                                   "body": json.loads(body) if body else None,
                                   "authorization": self.headers.get("Authorization")})
            if not state.replies:
                status, value, options = 500, {"error": "unexpected request"}, {}
            else:
                item = state.replies.pop(0)
                status, value = item[:2]
                options = item[2] if len(item) > 2 else {}
            if options.get("disconnect"):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            time.sleep(options.get("delay", 0))
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            try:
                if options.get("slow_headers"):
                    for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n':
                        self.connection.sendall(bytes([byte]))
                        time.sleep(options["slow_headers"])
                    return
                self.send_response(status)
                for key, val in options.get("headers", {}).items():
                    self.send_header(key, val)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(options.get("declared_length", len(raw))))
                self.end_headers()
                if options.get("trickle"):
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(options["trickle"])
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    state.url = "http://127.0.0.1:{}/v1".format(server.server_address[1])
    state.port = server.server_address[1]
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture(autouse=True)
def isolated_dispatch(tmp_path, monkeypatch):
    import services.model_dispatch as dispatch
    monkeypatch.setattr(dispatch, "_db_path", lambda: tmp_path / "dispatch.db")
    monkeypatch.setattr("services.llm_trace.get_recorder", lambda: None)
    # These sub-second assertions measure HTTP headers/body/stream deadlines.
    # A cold SQLite gate on a mechanical volume can exceed that budget before
    # HTTP starts. Distributed admission is covered by the dispatch test suites;
    # the client's own bounded semaphore remains active in these HTTP tests.
    monkeypatch.setenv("SJFX_SHARED_GENERATION_GATE", "0")


@pytest.mark.parametrize("url", ["http://example.com/v1", "http://192.168.1.1/v1", "http://127.0.0.1.evil/v1",
    "http://127.0.0.1@evil/v1", "http://user:secret@localhost/v1", "http://localhost:0/v1",
    "http://localhost:65536/v1", "http://localhost:bad/v1", "http://localhost/v1?token=secret",
    "http://localhost/v1#secret", "http://localhost/v1?", "http://localhost/v1#", "http://localhost/\nfoo",
    "ftp://localhost/v1", "file:///tmp/model", "http://localhost\\evil/v1", "http://127.1/v1"])
def test_invalid_urls_rejected_without_echo(url):
    with pytest.raises(LocalModelError) as caught:
        OpenAICompatibleClient(url, "fixture")
    assert caught.value.code == "configuration_error"
    assert "secret" not in str(caught.value)
    assert "evil" not in str(caught.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:123/v1/", "http://localhost:123/v1", "http://[::1]:123/v1", "https://localhost:123/v1"])
def test_accepted_loopback_spellings(url):
    assert validate_local_url(url) == url.rstrip("/")


def test_conservative_openai_payload_and_structured_contract():
    with http_service([(200, {"data": [{"id": "fixture"}]}), (200, openai_reply('{"answer":"完整结果"}'))]) as server:
        client = OpenAICompatibleClient(server.url, "fixture", api_key="local-test-key")
        assert client.health_check()["model_available"]
        result = client.chat_json("policy", "原始证据", required_fields=["answer"], retries=0)
    assert result["json"] == {"answer": "完整结果"}
    payload = server.requests[-1]["body"]
    assert payload["messages"][1]["content"] == "原始证据"
    assert set(payload) == {"model", "messages", "temperature", "max_tokens", "stream"}
    assert server.requests[-1]["authorization"] == "Bearer local-test-key"
    assert not client.capabilities.trace_metadata
    assert not client.supports_json_schema


def test_declared_json_object_capability_is_sent_and_vllm_options_absent():
    with http_service([(200, openai_reply('{"answer":1}'))]) as server:
        client = OpenAICompatibleClient(server.url, "fixture", supports_json_response_format=True)
        assert client.chat_json("s", "u", retries=0)["json"] == {"answer": 1}
    payload = server.requests[0]["body"]
    assert payload["response_format"] == {"type": "json_object"}
    assert "chat_template_kwargs" not in payload
    assert "return_token_ids" not in payload


def test_undeclared_response_format_fails_before_http():
    with http_service([]) as server:
        client = OpenAICompatibleClient(server.url, "fixture")
        with pytest.raises(LocalModelError) as caught:
            client.chat("s", "u", response_format={"type": "json_object"}, retries=0)
    assert caught.value.code == "unsupported_capability"
    assert not server.requests


@pytest.mark.parametrize("bad", [[], {"choices": []}, {"choices": "invalid"},
    {"choices": [{"message": "invalid"}]}, openai_reply(17), openai_reply(None),
    openai_reply([{"type": "image", "image_url": "x"}]), openai_reply("ok", usage="bad"),
    openai_reply("ok", usage={"total_tokens": "3"}), openai_reply("ok", usage={"total_tokens": -1})])
def test_malformed_openai_responses_have_clear_error_and_no_retry(bad):
    with http_service([(200, bad)]) as server:
        client = OpenAICompatibleClient(server.url, "fixture")
        with pytest.raises(LocalModelError) as caught:
            client.chat("s", "u", retries=5)
    assert caught.value.code == "invalid_response"
    assert len(server.requests) == 1


@pytest.mark.parametrize("content", ['{"answer":', '[]', '{}', 'not JSON', '{"answer":NaN}', '{"answer":Infinity}'])
def test_structured_output_failures_are_not_success_or_silently_retried(content):
    with http_service([(200, openai_reply(content))]) as server:
        client = OpenAICompatibleClient(server.url, "fixture")
        with pytest.raises(LocalModelError) as caught:
            client.chat_json("s", "u", required_fields=["answer"], retries=3)
    assert caught.value.code == "invalid_structured_output"
    assert len(server.requests) == 1


@pytest.mark.parametrize("backend", ["openai", "vllm", "ollama"])
@pytest.mark.parametrize("status,code", [(401, "authentication_error"), (403, "authentication_error"),
    (404, "model_unavailable"), (400, "request_error"), (429, "resource_exhausted"), (503, "resource_exhausted")])
def test_http_errors_do_not_echo_credentials_prompts_or_provider_body(backend, status, code, caplog):
    secret = "PRIVATE-LOCAL-KEY"
    detail = "invalid request " if status == 400 else "out of memory "
    with http_service([(status, {"error": detail + secret + " secret-document"})]) as server:
        cls = {"openai": OpenAICompatibleClient, "vllm": VLLMClient, "ollama": OllamaClient}[backend]
        client = cls(server.url, "fixture", api_key=secret)
        with pytest.raises(LocalModelError) as caught:
            client.chat("s", "secret-document", retries=0)
    assert caught.value.code == code
    assert caught.value.status == status
    assert secret not in str(caught.value) + caplog.text
    assert "secret-document" not in str(caught.value) + caplog.text
    assert len(server.requests) == 1


def test_busy_retry_is_finite_and_recovers_after_restart_response():
    with http_service([(503, {"error": "busy"}), (200, openai_reply())]) as server:
        client = OpenAICompatibleClient(server.url, "fixture")
        assert client.chat("s", "u", retries=1)["content"] == "最终答案"
    assert len(server.requests) == 2


def test_retry_zero_respects_resource_error():
    with http_service([(503, {"error": "out of memory"}), (200, openai_reply())]) as server:
        with pytest.raises(LocalModelError) as caught:
            OpenAICompatibleClient(server.url, "fixture").chat("s", "u", retries=0)
    assert caught.value.code == "resource_exhausted"
    assert caught.value.retryable
    assert len(server.requests) == 1


def test_connection_loss_and_new_client_request_after_service_restart():
    with http_service([(200, openai_reply())]) as initial:
        client = OpenAICompatibleClient(initial.url, "fixture", timeout=.3)
        port = initial.port
    with pytest.raises(LocalModelError) as caught:
        client.chat("s", "u", retries=0)
    assert caught.value.code in {"unavailable", "timeout"}
    with http_service([(200, openai_reply())], port=port) as restarted:
        assert client.chat("s", "u", retries=0)["content"] == "最终答案"
    assert len(restarted.requests) == 1


def test_timeout_is_bounded_and_client_recovers_next_request():
    with http_service([(200, openai_reply(), {"delay": .8}), (200, openai_reply())]) as server:
        client = OpenAICompatibleClient(server.url, "fixture", timeout=.2)
        started = time.monotonic()
        with pytest.raises(LocalModelError) as caught:
            client.chat("s", "u", retries=0, timeout=60)
        assert time.monotonic() - started < .7
        assert caught.value.code == "timeout"
        assert client.chat("s", "u", retries=0)["content"] == "最终答案"


def test_slow_body_uses_total_deadline():
    with http_service([(200, openai_reply(), {"trickle": .01})]) as server:
        client = OpenAICompatibleClient(server.url, "fixture", timeout=.06)
        started = time.monotonic()
        with pytest.raises(LocalModelError) as caught:
            client.chat("s", "u", retries=0)
        assert caught.value.code == "timeout"
        assert time.monotonic() - started < .6


def test_slow_headers_obey_total_deadline():
    with http_service([(200, openai_reply(), {"slow_headers": .02})]) as server:
        client = OpenAICompatibleClient(server.url, "fixture", timeout=.1)
        started = time.monotonic()
        with pytest.raises(LocalModelError) as caught:
            client.chat("s", "u", retries=0)
        assert caught.value.code == "timeout"
        assert time.monotonic() - started < .8


def test_truncated_http_body_is_not_success():
    with http_service([(200, openai_reply(), {"declared_length": 9999})]) as server:
        with pytest.raises(LocalModelError) as caught:
            OpenAICompatibleClient(server.url, "fixture").chat("s", "u", retries=0)
    assert caught.value.code == "invalid_response"


def test_queue_wait_is_finite_and_releases_after_error():
    with http_service([(200, openai_reply())]) as server:
        client = OpenAICompatibleClient(server.url, "fixture", queue_timeout=.02)
        client._semaphore.acquire()
        started = time.monotonic()
        try:
            with pytest.raises(LocalModelError) as caught:
                client.chat("s", "u", retries=0)
        finally:
            client._semaphore.release()
        assert time.monotonic() - started < .6
        assert caught.value.code == "busy"
        assert not server.requests
        assert client.chat("s", "u", retries=0)["content"] == "最终答案"


@pytest.mark.parametrize("location", ["http://example.com/leak", "http://127.0.0.1:9999/leak", "/v1/leak"])
def test_redirects_are_rejected_even_when_target_is_local(location):
    with http_service([(307, b"", {"headers": {"Location": location}})]) as server:
        client = OpenAICompatibleClient(server.url, "fixture", api_key="private-key")
        with pytest.raises(LocalModelError) as caught:
            client.chat("s", "u", retries=4)
    assert caught.value.code == "redirect_refused"
    assert len(server.requests) == 1


def test_proxy_environment_does_not_receive_model_data(monkeypatch):
    with http_service([]) as proxy, http_service([(200, openai_reply())]) as service:
        monkeypatch.setenv("HTTP_PROXY", proxy.url)
        monkeypatch.setenv("HTTPS_PROXY", proxy.url)
        monkeypatch.setenv("NO_PROXY", "")
        assert OpenAICompatibleClient(service.url, "fixture").chat("s", "u", retries=0)["content"] == "最终答案"
    assert len(service.requests) == 1
    assert not proxy.requests


def test_ollama_native_health_chat_and_structured_thinking_disabled():
    lines = b'{"message":{"content":"{\\"answer\\":1}"}}\n{"done":true,"eval_count":3,"prompt_eval_count":2}\n'
    with http_service([(200, {"models": [{"name": "fixture"}]}),
        (200, {"message": {"content": "答案"}, "done": True}), (200, lines)]) as server:
        client = OllamaClient(server.url, "fixture")
        assert client.health_check()["model_available"]
        assert client.chat("s", "u", retries=0)["content"] == "答案"
        result = client.chat_json("s", "u", required_fields=["answer"], retries=0)
    assert result["json"] == {"answer": 1}
    assert result["usage"]["total_tokens"] == 5
    assert [r["path"] for r in server.requests] == ["/api/tags", "/api/chat", "/api/chat"]
    assert all(r["body"]["think"] is False for r in server.requests[1:])
    assert server.requests[-1]["body"]["format"] == "json"


@pytest.mark.parametrize("bad", [{"message": []}, {"message": {"content": 2}},
    {"message": {"content": "x"}, "eval_count": "2"}, {"message": {"content": "x"}, "total_duration": "bad"}])
def test_ollama_malformed_chat_has_safe_failure(bad):
    with http_service([(200, bad)]) as server:
        with pytest.raises(LocalModelError) as caught:
            OllamaClient(server.url, "fixture").chat("s", "u", retries=0)
    assert caught.value.code == "invalid_response"


def test_ollama_partial_stream_is_not_valid_structured_success():
    with http_service([(200, b'{"message":{"content":"{\\"answer\\":1}"}}\n')]) as server:
        with pytest.raises(LocalModelError) as caught:
            OllamaClient(server.url, "fixture").chat_json("s", "u", retries=0)
    assert caught.value.code == "invalid_response"


def test_ollama_slow_stream_without_newline_obeys_total_deadline():
    with http_service([(200, b'{"message":{"content":"long unfinished response"}}', {"trickle": .05})]) as server:
        client = OllamaClient(server.url, "fixture", timeout=.2)
        started = time.monotonic()
        with pytest.raises(LocalModelError) as caught:
            client.chat_json("s", "u", retries=0)
        assert caught.value.code == "timeout"
        assert time.monotonic() - started < 1


def test_vllm_exact_context_measurement_preserves_source_and_special_payload():
    detail = {"error": {"message": "maximum context length is 100 tokens"}}
    with http_service([(400, detail), (200, {"count": 96}), (200, openai_reply('{"answer":1}'))]) as server:
        client = VLLMClient(server.url, "fixture")
        result = client.chat_json("policy", "全部原文保持", max_tokens=8, retries=0)
    original, tokenize, retried = [row["body"] for row in server.requests]
    assert original["messages"] == tokenize["messages"] == retried["messages"]
    assert original["chat_template_kwargs"] == {"enable_thinking": False}
    assert retried["max_tokens"] == 4
    assert result["context_budget"]["input_truncated"] is False
    assert result["json"] == {"answer": 1}


def test_factory_does_not_fall_back_and_timeout_is_upper_bound():
    settings = SimpleNamespace(LLM_BACKEND="openai_compatible", LLM_MAX_CONCURRENCY=2,
        ENABLE_OPENAI_COMPATIBLE=True, OPENAI_COMPATIBLE_BASE_URL="http://127.0.0.1:8001/v1",
        OPENAI_COMPATIBLE_MODEL="local", OPENAI_COMPATIBLE_API_KEY="secret",
        OPENAI_COMPATIBLE_REQUEST_TIMEOUT=60, OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT=False)
    client = create_model_client(settings, timeout=180)
    assert isinstance(client, OpenAICompatibleClient)
    assert client.timeout == 60
    settings.ENABLE_OPENAI_COMPATIBLE = False
    assert create_model_client(settings) is None
    assert isinstance(create_model_client(settings, require_enabled=False), OpenAICompatibleClient)


def test_translation_factory_keeps_dedicated_ollama_configuration():
    settings = SimpleNamespace(LLM_BACKEND="openai_compatible",
        TRANSLATION_OLLAMA_BASE_URL="http://127.0.0.1:12345/v1", TRANSLATION_OLLAMA_MODEL="translator",
        SHARED_OLLAMA_REQUEST_TIMEOUT=60, OLLAMA_API_KEY="local-secret")
    client = create_translation_client(settings, timeout=180)
    assert isinstance(client, OllamaClient)
    assert client.model == "translator"
    assert client.base_url == settings.TRANSLATION_OLLAMA_BASE_URL
    assert client.timeout == 60


@pytest.mark.parametrize("cls", [OpenAICompatibleClient, VLLMClient])
def test_openai_response_from_another_model_is_rejected(cls):
    with http_service([(200, openai_reply(model="unexpected-model"))]) as server:
        with pytest.raises(LocalModelError) as caught:
            cls(server.url, "fixture").chat("s", "u", retries=0)
    assert caught.value.code == "invalid_response"
    assert "unexpected-model" not in str(caught.value)


@pytest.mark.parametrize("cls", [OpenAICompatibleClient, VLLMClient])
def test_openai_response_without_model_remains_compatible(cls):
    reply = openai_reply()
    reply.pop("model")
    with http_service([(200, reply)]) as server:
        result = cls(server.url, "fixture").chat("s", "u", retries=0)
    assert result["model"] == "fixture"


@pytest.mark.parametrize("configured,reported", [("fixture", "fixture:latest"),
    ("fixture:latest", "fixture"), ("registry:5000/fixture", "registry:5000/fixture:latest")])
def test_native_ollama_default_tag_alias_is_compatible(configured, reported):
    with http_service([(200, {"model": reported, "message": {"content": "答案"}, "done": True})]) as server:
        result = OllamaClient(server.url, configured).chat("s", "u", retries=0)
    assert result["model"] == reported


@pytest.mark.parametrize("reported", ["unexpected-model", "fixture:another-tag"])
def test_native_ollama_other_model_or_tag_is_rejected(reported):
    with http_service([(200, {"model": reported, "message": {"content": "答案"}, "done": True})]) as server:
        with pytest.raises(LocalModelError) as caught:
            OllamaClient(server.url, "fixture").chat("s", "u", retries=0)
    assert caught.value.code == "invalid_response"
    assert reported not in str(caught.value)
