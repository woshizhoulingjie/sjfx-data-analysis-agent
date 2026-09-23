#!/usr/bin/env python3
"""Run and capture a deterministic six-stage Morning Star benchmark workflow.

The primary artifact is a trace JSONL.  Its first row is a file-level manifest;
each following row is one real vLLM call with the exact request/response and the
token IDs returned by vLLM's tokenizer endpoint.  A benchmark-compatible
``cases.jsonl`` is derived beside it and keeps the full output in ``extensions``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import urllib.error
import urllib.request
import uuid


SCHEMA_VERSION = "3.0.0"
AGENT_ID = "sjfx"
DEFAULT_BASE_URL = "http://127.0.0.1:8001/v1"
DEFAULT_MODEL = "qwen3.6:27b"
FIXED_GENERATION = {
    "max_tokens": 768,
    "n": 1,
    "temperature": 0,
    "top_p": 1.0,
    "seed": 42,
    "chat_template_kwargs": {"enable_thinking": False},
}

STAGES = (
    ("scan", "资料扫描", True),
    ("document_analysis", "文档分析", False),
    ("evidence", "证据核验", False),
    ("structured_qa", "结构化问答", False),
    ("reporting", "报告编写", True),
    ("export", "整理导出", True),
)

SYSTEM_PROMPTS = {
    "scan": "你是资料扫描智能体。只依据提供的文件清点资料，识别范围、类型、时间、主体、缺失项与无关项。输出严格 JSON。",
    "document_analysis": "你是文档分析智能体。逐文档抽取可核验事实、金额、日期、义务、验收结果、变更与风险，不得把推断写成事实。输出严格 JSON。",
    "evidence": "你是证据核验智能体。将待核验主张与原始文件逐条比对，标注支持、冲突、证据不足，并给出文件编号和原文短摘录。输出严格 JSON。",
    "structured_qa": "你是结构化问答智能体。仅使用已经核验的证据回答问题；每个答案都要列出证据编号、置信度和边界。输出严格 JSON。",
    "reporting": "你是报告编写智能体。基于核验结果与结构化问答形成完整中文项目分析报告，明确区分事实、判断、风险和待补材料。",
    "export": "你是整理导出智能体。将前序成果整理成可交付结构，输出严格 JSON，包含文件清单、报告正文、结构化问答、证据索引、风险表和导出校验信息。",
}

STAGE_TASKS = {
    "scan": "清点全部资料；按合同、交付验收、财务、会议、行政分类；给出优先阅读顺序、覆盖率、无关文件和缺失元数据。",
    "document_analysis": "分析合同当前总额、付款条件、交付与验收时间线、性能指标、变更影响、费用数据和主要履约风险。事实必须绑定 source_id。",
    "evidence": "核验文档分析阶段的全部核心主张。对金额、比例、日期、性能数值、验收状态、付款触发条件逐条给出 verdict、source_ids、quote、reason。",
    "structured_qa": "回答：1. 当前合同含税总额是多少？2. 三个付款节点是什么？3. 首次与复测性能分别是多少？4. 最终验收何时通过？5. 末期付款还缺什么条件？6. 乙方何时可暂停服务？7. 一季度费用合计及最高部门是什么？",
    "reporting": "编写《晨星项目综合分析报告》，至少包含执行摘要、资料范围、合同与变更、交付验收、财务观察、证据结论、风险与建议、证据边界。引用格式统一为 [Dxxx]。",
    "export": "生成最终导出包描述。不得新增事实；报告、问答、证据索引之间的金额、日期和结论必须一致，并列出建议导出的 JSON、Markdown 和证据清单文件。",
}


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def http_json(url, *, payload=None, timeout=600, headers=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method="POST" if body is not None else "GET"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return raw, json.loads(raw)


def api_root(base_url):
    value = base_url.rstrip("/")
    return value[:-3] if value.endswith("/v1") else value


def load_documents(directory):
    documents = []
    for path in sorted(Path(directory).iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8-sig")
        documents.append({
            "source_id": path.stem.split("_", 1)[0],
            "file_name": path.name,
            "sha256": sha256_text(text),
            "content": text,
        })
    if len(documents) < 6:
        raise ValueError("晨星资料不足：至少需要 6 份文件")
    return documents


def render_documents(documents):
    return "\n\n".join(
        "===== {source_id} | {file_name} =====\n{content}".format(**item)
        for item in documents
    )


def build_user_prompt(master_prompt, stage_id, documents, previous_outputs):
    handoff = "无（这是首阶段）"
    if previous_outputs:
        handoff = "\n\n".join(
            "【{} 阶段输出】\n{}".format(item["stage_id"], item["content"])
            for item in previous_outputs
        )
    return (
        "【总任务】\n{}\n\n【当前阶段】{}\n{}\n\n"
        "【前序阶段结果】\n{}\n\n【原始晨星资料】\n{}"
    ).format(master_prompt.strip(), stage_id, STAGE_TASKS[stage_id], handoff, render_documents(documents))


def tokenize(root_url, model, *, messages=None, prompt=None, chat_template_kwargs=None, timeout=60):
    payload = {"model": model}
    if messages is not None:
        payload.update({
            "messages": messages,
            "add_generation_prompt": True,
            "chat_template_kwargs": chat_template_kwargs or {},
        })
    else:
        payload.update({"prompt": str(prompt or ""), "add_special_tokens": False})
    _, response = http_json(root_url + "/tokenize", payload=payload, timeout=timeout)
    token_ids = response.get("tokens")
    if token_ids is None:
        token_ids = response.get("token_ids")
    if not isinstance(token_ids, list) or not all(isinstance(item, int) for item in token_ids):
        raise RuntimeError("vLLM /tokenize 未返回 token ID 列表：{}".format(response))
    return token_ids


def model_metadata(root_url, model, cache_enabled, timeout):
    _, version = http_json(root_url + "/version", timeout=timeout)
    _, models = http_json(root_url + "/v1/models", timeout=timeout)
    selected = next((item for item in models.get("data", []) if item.get("id") == model), {})
    model_root = selected.get("root")
    tokenizer_files = {}
    chat_template = None
    if model_root and Path(model_root).is_dir():
        for name in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
            path = Path(model_root) / name
            if path.is_file():
                tokenizer_files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        config_path = Path(model_root) / "tokenizer_config.json"
        if config_path.is_file():
            try:
                chat_template = json.loads(config_path.read_text(encoding="utf-8")).get("chat_template")
            except (ValueError, OSError):
                chat_template = None
    fingerprint = sha256_text(canonical_json(tokenizer_files)) if tokenizer_files else None
    server_command = None
    for process_dir in Path("/proc").glob("[0-9]*"):
        try:
            command = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if "vllm serve" in command and model in command:
            server_command = command
            break
    cache_flags = {}
    if server_command:
        parts = server_command.split()
        for flag in (
            "--kv-cache-dtype", "--gpu-memory-utilization", "--max-model-len",
            "--max-num-seqs", "--max-num-batched-tokens", "--block-size",
        ):
            if flag in parts and parts.index(flag) + 1 < len(parts):
                cache_flags[flag.lstrip("-").replace("-", "_")] = parts[parts.index(flag) + 1]
        cache_flags["enable_prefix_caching"] = "--enable-prefix-caching" in parts
    return {
        "model": model,
        "model_metadata": selected,
        "tokenizer": {
            "model_root": model_root,
            "file_sha256": tokenizer_files,
            "fingerprint": "sha256:" + fingerprint if fingerprint else None,
        },
        "chat_template": {
            "template": chat_template,
            "template_sha256": sha256_text(chat_template) if isinstance(chat_template, str) else None,
            "kwargs": {"enable_thinking": False},
        },
        "vllm_version": version.get("version"),
        "cache_config": {
            "enable_prefix_caching": bool(cache_enabled),
            "source": "benchmark agents/sjfx/agent.yaml",
            "observed_server_flags": cache_flags,
            "server_command_sha256": sha256_text(server_command) if server_command else None,
            "source_cached_tokens_semantics": (
                "usage.prompt_tokens_details.cached_tokens when supplied; otherwise 0 only because prefix caching is disabled"
            ),
        },
    }


def source_cached_tokens(response, cache_enabled):
    usage = response.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    value = details.get("cached_tokens")
    if value is not None:
        return int(value), "response.usage.prompt_tokens_details.cached_tokens"
    if not cache_enabled:
        return 0, "prefix_cache_disabled"
    return None, "not_exposed_by_server"


def append_jsonl(path, value):
    with Path(path).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def trace_to_case(trace, trace_name, trace_line):
    request = trace["request"]
    response = trace["response"]
    usage = response.get("usage") or {}
    shape_controlled = bool(request.get("ignore_eos") and request.get("min_tokens"))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "WorkloadCase",
        "fixture": True,
        "case_id": "sjfx.morning_star.{}.{:03d}".format(trace["stage_id"], trace["sequence"]),
        "agent_id": AGENT_ID,
        "stage_id": trace["stage_id"],
        "mode": "shape_controlled" if shape_controlled else "replay",
        "capture": {
            "type": "real_capture",
            "source_ref": "{}#L{}".format(trace_name, trace_line),
            "trace_id": trace["trace_id"],
            "workflow_id": trace["workflow_id"],
            "call_id": trace["call_id"],
            "parent_call_ids": trace["parent_call_ids"],
            "arrival_offset_ms": trace["arrival_offset_ms"],
            "sensitivity": "restricted",
            "transformations": [],
        },
        "payload": {"endpoint": "/v1/chat/completions", "body": request},
        "reference": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "tokenizer_fingerprint": trace.get("tokenizer_fingerprint"),
            "output_ref": "{}#L{}".format(trace_name, trace_line),
        },
        "features": {
            "prefix_group": None,
            "has_tools": bool(request.get("tools")),
            "thinking": "off",
            "structured_output": False,
        },
        "controlled_output_tokens": request.get("max_tokens") if shape_controlled else None,
        "extensions": {
            "observed_output": ((response.get("choices") or [{}])[0].get("message") or {}).get("content"),
            "observed_response": response,
            "input_token_ids": trace["input_token_ids"],
            "output_token_ids": trace["output_token_ids"],
            "input_token_ids_source": trace.get("input_token_ids_source"),
            "output_token_ids_source": trace.get("output_token_ids_source"),
            "source_cached_tokens": trace["source_cached_tokens"],
            "source_cached_tokens_source": trace["source_cached_tokens_source"],
            "concurrency_group": trace["concurrency_group"],
            "observed_latency_seconds": trace["latency_seconds"],
        },
    }


def run(args):
    base_url = args.base_url.rstrip("/")
    root_url = api_root(base_url)
    headers = {"Authorization": "Bearer " + args.api_key} if args.api_key else {}
    documents = load_documents(args.documents)
    master_prompt = Path(args.prompt).read_text(encoding="utf-8")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    trace_path = output_dir / "trace.jsonl"
    cases_path = output_dir / "cases.jsonl"
    workflow_id = uuid.uuid4().hex[:16]
    trace_id = uuid.uuid4().hex
    started = time.perf_counter()
    metadata = model_metadata(root_url, args.model, args.prefix_cache_enabled, args.timeout)
    manifest = {
        "schema_version": "1.0.0",
        "kind": "TraceManifest",
        "trace_id": trace_id,
        "workflow_id": workflow_id,
        "created_at": utc_now(),
        "host": socket.gethostname(),
        "agent_id": AGENT_ID,
        "fixture": True,
        "source_documents": [
            {key: item[key] for key in ("source_id", "file_name", "sha256")} for item in documents
        ],
        "generation_defaults": FIXED_GENERATION,
        **metadata,
    }
    append_jsonl(trace_path, manifest)
    previous_outputs = []
    parent_call_ids = []
    traces = []
    for sequence, (stage_id, display_name, shape_controlled) in enumerate(STAGES, 1):
        call_id = uuid.uuid4().hex
        user_prompt = build_user_prompt(master_prompt, stage_id, documents, previous_outputs)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPTS[stage_id]},
            {"role": "user", "content": user_prompt},
        ]
        request_payload = {
            "model": args.model,
            "messages": messages,
            "stream": False,
            "return_token_ids": True,
            "return_prompt_text": True,
            **FIXED_GENERATION,
        }
        if shape_controlled:
            request_payload.update({"min_tokens": 768, "ignore_eos": True})
        input_ids = tokenize(
            root_url, args.model, messages=messages,
            chat_template_kwargs=FIXED_GENERATION["chat_template_kwargs"], timeout=args.timeout,
        )
        call_started_at = utc_now()
        call_started = time.perf_counter()
        raw_response, response = http_json(
            base_url + "/chat/completions", payload=request_payload,
            timeout=args.timeout, headers=headers,
        )
        latency = time.perf_counter() - call_started
        choices = response.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") if choices else None)
        if content is None:
            raise RuntimeError("{} 阶段未返回 content：{}".format(stage_id, raw_response[:1000]))
        response_input_ids = response.get("prompt_token_ids")
        if isinstance(response_input_ids, list) and all(isinstance(item, int) for item in response_input_ids):
            input_ids = response_input_ids
            input_token_ids_source = "response.prompt_token_ids"
        else:
            input_token_ids_source = "vllm./tokenize"
        response_output_ids = choices[0].get("token_ids") if choices else None
        if isinstance(response_output_ids, list) and all(isinstance(item, int) for item in response_output_ids):
            output_ids = response_output_ids
            output_token_ids_source = "response.choices[0].token_ids"
        else:
            output_ids = tokenize(root_url, args.model, prompt=content, timeout=args.timeout)
            output_token_ids_source = "vllm./tokenize"
        cached_tokens, cached_source = source_cached_tokens(response, args.prefix_cache_enabled)
        trace = {
            "schema_version": "1.0.0",
            "kind": "TraceCall",
            "trace_id": trace_id,
            "workflow_id": workflow_id,
            "sequence": sequence,
            "call_id": call_id,
            "stage_id": stage_id,
            "stage_display_name": display_name,
            "parent_call_ids": list(parent_call_ids),
            "concurrency_group": "serial-{:03d}".format(sequence),
            "arrival_offset_ms": round((call_started - started) * 1000, 3),
            "started_at": call_started_at,
            "completed_at": utc_now(),
            "latency_seconds": round(latency, 6),
            "request": request_payload,
            "response": response,
            "response_raw": raw_response,
            "input_token_ids": input_ids,
            "output_token_ids": output_ids,
            "input_token_ids_source": input_token_ids_source,
            "output_token_ids_source": output_token_ids_source,
            "source_cached_tokens": cached_tokens,
            "source_cached_tokens_source": cached_source,
            "tokenizer_fingerprint": metadata["tokenizer"]["fingerprint"],
        }
        append_jsonl(trace_path, trace)
        traces.append(trace)
        previous_outputs.append({"stage_id": stage_id, "content": content})
        parent_call_ids = [call_id]
        print("[{}/{}] {} complete: input={} output={} {:.2f}s".format(
            sequence, len(STAGES), stage_id, len(input_ids), len(output_ids), latency
        ), flush=True)
    for index, trace in enumerate(traces, 2):
        append_jsonl(cases_path, trace_to_case(trace, trace_path.name, index))
    summary = {
        "trace": str(trace_path),
        "cases": str(cases_path),
        "calls": len(traces),
        "workflow_id": workflow_id,
        "trace_id": trace_id,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--documents", default=str(root / "benchmark_capture" / "morning_star_documents"))
    parser.add_argument("--prompt", default=str(root / "benchmark_capture" / "morning_star_user_prompt.md"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--base-url", default=os.getenv("VLLM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.getenv("VLLM_MODEL", DEFAULT_MODEL))
    parser.add_argument("--api-key", default=os.getenv("VLLM_API_KEY", ""))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--prefix-cache-enabled", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    result = run(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
