"""Actual preliminary JSON entry point -> factory transports, without a model.

Business JSON calls go through PydanticAgentRuntime. Its preview controls are
not arguments to the narrower transport.chat_json API. Only HTTP responses are
replaced here: the real business functions, wrapper and transport payloads run.
"""
import ast
import io
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest

from config import Config
from services.agent_runtime import PydanticAgentRuntime
from services.document_analysis import analyze_document_preview
from services.model_backend import create_model_client
from services.model_contract import LocalModelError
from services.preliminary_summary_fast import _call


@pytest.fixture(params=("vllm", "ollama", "openai_compatible"))
def protocol(request, monkeypatch):
    backend = request.param
    config = SimpleNamespace(
        LLM_BACKEND=backend, LLM_MAX_CONCURRENCY=1, LLM_CONTEXT_TOKENS=32768,
        LLM_QUEUE_TIMEOUT_SECONDS=1, ENABLE_VLLM=True,
        ENABLE_SHARED_OLLAMA=True, ENABLE_OPENAI_COMPATIBLE=True,
        VLLM_BASE_URL="http://127.0.0.1:1/v1", VLLM_MODEL="fixture",
        VLLM_REQUEST_TIMEOUT=240, VLLM_API_KEY="",
        OLLAMA_BASE_URL="http://127.0.0.1:1/v1", OLLAMA_MODEL="fixture",
        SHARED_OLLAMA_REQUEST_TIMEOUT=240, OLLAMA_API_KEY="",
        OPENAI_COMPATIBLE_BASE_URL="http://127.0.0.1:1/v1",
        OPENAI_COMPATIBLE_MODEL="fixture", OPENAI_COMPATIBLE_REQUEST_TIMEOUT=240,
        OPENAI_COMPATIBLE_API_KEY="", OPENAI_COMPATIBLE_SUPPORTS_JSON_RESPONSE_FORMAT=True,
    )
    state = SimpleNamespace(backend=backend, calls=[], replies=[], content='{"core_summary":"材料只验证清晰输入，噪声条件尚未评价。"}', finish="stop")

    def open_response(http_request, timeout, *, allowed_endpoints=()):
        payload = json.loads(http_request.data)
        state.calls.append({"url": http_request.full_url, "payload": payload, "timeout": timeout})
        reply = state.replies.pop(0) if state.replies else (state.content, state.finish)
        if isinstance(reply, Exception):
            raise reply
        content, finish = reply
        if backend == "ollama":
            data = {"model":"fixture", "message":{"content":content}, "done":True,
                    "done_reason":finish, "prompt_eval_count":2, "eval_count":3}
        else:
            data = {"model":"fixture", "choices":[{"message":{"content":content}, "finish_reason":finish}],
                    "usage":{"prompt_tokens":2, "completion_tokens":3, "total_tokens":5}}
        return io.BytesIO(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    # No dispatch DB, trace probes, model service, or embedding service is used.
    monkeypatch.setenv("SJFX_SHARED_GENERATION_GATE", "0")
    monkeypatch.setattr("services.llm_trace.get_recorder", lambda: None)
    monkeypatch.setattr("services." + backend + ".open_local_request", open_response)
    monkeypatch.setattr("services.model_contract.LocalModelClient._backoff", lambda attempt: None)
    monkeypatch.setattr(Config, "VLLM_STRUCTURED_TIMEOUT_SECONDS", 180)
    state.transport = create_model_client(config)
    state.runtime = PydanticAgentRuntime(state.transport)
    return state


def output_budget(call, backend):
    payload = call["payload"]
    return payload["options"]["num_predict"] if backend == "ollama" else payload["max_tokens"]


def assert_wire_contract(protocol, required, expected_tokens, expected_timeout, compact):
    assert len(protocol.calls) == 1
    call = protocol.calls[0]
    payload = call["payload"]
    # Transport deadlines subtract elapsed setup/queue time from the budget.
    assert 0 < call["timeout"] <= expected_timeout
    assert output_budget(call, protocol.backend) == expected_tokens
    # Preview/wrapper controls must never be forwarded as provider parameters.
    for key in ("allow_short_timeout", "structured_retry_limit", "long_output", "strict", "output_schema"):
        assert key not in payload
    system = payload["messages"][0]["content"]
    assert "安全边界" in system
    assert ("OUTPUT_EFFICIENCY_CONTRACT" in system) is compact
    if protocol.backend == "vllm":
        schema = payload["response_format"]["json_schema"]["schema"]
        assert schema["required"] == [required]
        assert payload["chat_template_kwargs"] == {"enable_thinking":False}
    elif protocol.backend == "openai_compatible":
        assert payload["response_format"] == {"type":"json_object"}
        assert "chat_template_kwargs" not in payload
    else:
        assert "response_format" not in payload
        assert payload["think"] is False
        assert payload["stream"] is False


def test_web_business_entrypoint_wraps_the_selected_factory_transport():
    # Importing app.py would start application setup and touch real state.
    source = Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    assignments = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                   for target in node.targets if isinstance(target, ast.Name)}
    factory = assignments["llm_transport"]
    wrapper = assignments["llm"]
    assert isinstance(factory, ast.Call) and factory.func.id == "create_model_client"
    assert isinstance(wrapper, ast.Call) and wrapper.func.id == "PydanticAgentRuntime"
    assert wrapper.args[0].id == "llm_transport"


def test_single_preview_preserves_short_timeout_and_compact_output_contract(protocol):
    document = {"text":"研究问题：低质量图像是否影响可靠性？方法：比较清晰输入。结果：仅验证清晰输入。局限：噪声条件未评价。",
                "source":{"name":"A.txt"}, "structure":{"title":"边界研究"}, "evidence":[],
                "parser":{"name":"text"}}
    summary, response = analyze_document_preview(protocol.runtime, "A.txt", "A.txt", unified_document=document, timeout_seconds=35)
    assert summary["preview_only"] is True
    assert summary["core_summary"] == json.loads(protocol.content)["core_summary"]
    assert response["structured_retry"] is False
    assert_wire_contract(protocol, "core_summary", 1200, 35, compact=protocol.backend != "ollama")


@pytest.mark.parametrize("files", (1, 2))
def test_fast_single_and_batch_use_the_same_real_wrapper_and_factory_contract(protocol, files):
    rows = [{"job":{"id":str(i), "options":{"path":f"{i}.txt"}}, "input":"独立原文，条件尚未验证。"} for i in range(files)]
    documents = [{"file_id":r["job"]["id"], "path":r["job"]["options"]["path"], "core_summary":"仅当前文件的摘要。"} for r in rows]
    protocol.content = json.dumps({"documents":documents}, ensure_ascii=False)
    ledger = []
    result = _call(protocol.runtime, rows, ledger)
    assert result["json"]["documents"] == documents
    assert result["structured_retry"] is False
    assert len(ledger) == 1 and ledger[0]["success"] is True
    assert ledger[0]["files"] == files
    assert_wire_contract(protocol, "documents", 100 + 600 * files, 90 if files == 1 else 180, compact=False)


@pytest.mark.parametrize("content,finish", (("not JSON", "stop"), ('{"documents":[{"core_summary":"看似完整"}]}', "length")))
def test_fast_disabled_structured_retry_rejects_invalid_or_truncated_output(protocol, content, finish):
    protocol.content, protocol.finish = content, finish
    ledger = []
    with pytest.raises(LocalModelError, match="结构化契约"):
        _call(protocol.runtime, [{"job":{"id":"1", "options":{"path":"A.txt"}}, "input":"原始材料"}], ledger)
    assert len(protocol.calls) == 1
    assert ledger[0]["success"] is False
    assert output_budget(protocol.calls[0], protocol.backend) == 700


def test_wrapper_retains_existing_single_format_correction_without_transport_retry(protocol):
    protocol.replies = [("not JSON", "stop"), ('{"core_summary":"纠正后的结构化摘要。"}', "stop")]
    result = protocol.runtime.chat_json("摘要", "原始材料", required_fields=("core_summary",),
                                        max_tokens=700, retries=0, timeout=30, allow_short_timeout=True)
    assert result["structured_retry"] is True
    assert len(protocol.calls) == 2
    assert all(0 < call["timeout"] <= 30 and output_budget(call, protocol.backend) == 700 for call in protocol.calls)
    assert "结构化输出格式纠正" in protocol.calls[1]["payload"]["messages"][1]["content"]


def test_transport_failure_does_not_trigger_a_structured_correction_or_fallback(protocol):
    protocol.replies = [urllib.error.URLError(TimeoutError("synthetic timeout"))]
    with pytest.raises(LocalModelError) as caught:
        protocol.runtime.chat_json("摘要", "原文", required_fields=("core_summary",),
                                   max_tokens=700, retries=0, timeout=30, allow_short_timeout=True, structured_retry_limit=0)
    assert caught.value.code == "timeout"
    assert len(protocol.calls) == 1


def test_factory_timeout_remains_an_upper_bound_and_vllm_floor_is_opt_out(protocol):
    protocol.runtime.chat_json("摘要", "原文", required_fields=("core_summary",),
                               max_tokens=700, retries=0, timeout=30, structured_retry_limit=0)
    expected = 180 if protocol.backend == "vllm" else 30
    assert 0 < protocol.calls[0]["timeout"] <= expected
    protocol.calls.clear()
    protocol.runtime.chat_json("摘要", "原文", required_fields=("core_summary",),
                               max_tokens=700, retries=0, timeout=1000, allow_short_timeout=True, structured_retry_limit=0)
    assert protocol.transport.timeout == 240
    assert 0 < protocol.calls[0]["timeout"] <= protocol.transport.timeout


def test_application_schema_is_consumed_by_wrapper_with_declared_provider_capabilities(protocol):
    schema = {"type":"object", "properties":{"core_summary":{"type":"string"}},
              "required":["core_summary"], "additionalProperties":False}
    result = protocol.runtime.chat_json("摘要", "原文", required_fields=("core_summary",),
                                        output_schema=schema, max_tokens=700, retries=0, timeout=30,
                                        allow_short_timeout=True, structured_retry_limit=0)
    assert result["json"]["core_summary"]
    payload = protocol.calls[0]["payload"]
    assert "output_schema" not in payload
    if protocol.backend == "vllm":
        assert payload["response_format"]["json_schema"]["schema"] == schema
    elif protocol.backend == "openai_compatible":
        assert payload["response_format"] == {"type":"json_object"}
    else:
        assert "response_format" not in payload


def test_length_exhaustion_never_becomes_success_after_the_single_allowed_correction(protocol):
    protocol.finish = "length"
    with pytest.raises(LocalModelError, match="格式纠正重试仍失败"):
        protocol.runtime.chat_json("摘要", "原文", required_fields=("core_summary",),
                                   max_tokens=700, retries=0, timeout=30, allow_short_timeout=True)
    assert len(protocol.calls) == 2
    # The wrapper's existing one-shot length correction deliberately doubles
    # completion room; fast calls above opt out with structured_retry_limit=0.
    assert [output_budget(call, protocol.backend) for call in protocol.calls] == [700, 1400]
    assert all(0 < call["timeout"] <= 30 for call in protocol.calls)
