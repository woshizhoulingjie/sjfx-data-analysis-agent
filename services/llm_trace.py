"""Environment-gated tracing for real SJFX vLLM calls.

Tracing is disabled unless ``SJFX_LLM_TRACE_PATH`` is set.  The recorder is
process-safe enough for the application's threaded model fan-out: sequence,
overlap groups and writes are protected by one lock, while per-context parent
links use ``contextvars``.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid


_LAST_CALL_ID = contextvars.ContextVar("sjfx_trace_last_call_id", default=None)
_TRACE_CONTEXT = contextvars.ContextVar("sjfx_trace_context", default={})
_RECORDER = None
_RECORDER_LOCK = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _server_command(model):
    for process_dir in Path("/proc").glob("[0-9]*"):
        try:
            command = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if "vllm serve" in command and str(model) in command:
            return command
    return None


def _command_value(parts, flag):
    if flag in parts and parts.index(flag) + 1 < len(parts):
        return parts[parts.index(flag) + 1]
    return None


def build_manifest_metadata(model, model_root=None, vllm_version=None):
    command = _server_command(model)
    parts = command.split() if command else []
    tokenizer_files = {}
    chat_template = None
    if model_root and Path(model_root).is_dir():
        for name in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
            path = Path(model_root) / name
            if path.is_file():
                tokenizer_files[name] = sha256_bytes(path.read_bytes())
        tokenizer_config = Path(model_root) / "tokenizer_config.json"
        if tokenizer_config.is_file():
            try:
                chat_template = json.loads(tokenizer_config.read_text(encoding="utf-8")).get("chat_template")
            except (OSError, ValueError):
                pass
    fingerprint_payload = json.dumps(tokenizer_files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tokenizer_fingerprint = "sha256:" + sha256_bytes(fingerprint_payload) if tokenizer_files else None
    return {
        "model": model,
        "model_root": model_root,
        "vllm_version": vllm_version,
        "tokenizer": {
            "file_sha256": tokenizer_files,
            "fingerprint": tokenizer_fingerprint,
        },
        "chat_template": {
            "template": chat_template,
            "template_sha256": sha256_bytes(chat_template.encode("utf-8")) if isinstance(chat_template, str) else None,
            "kwargs": {"enable_thinking": False},
        },
        "cache_config": {
            "enable_prefix_caching": "--enable-prefix-caching" in parts,
            "kv_cache_dtype": _command_value(parts, "--kv-cache-dtype"),
            "block_size": _command_value(parts, "--block-size"),
            "gpu_memory_utilization": _command_value(parts, "--gpu-memory-utilization"),
            "max_model_len": _command_value(parts, "--max-model-len"),
            "max_num_seqs": _command_value(parts, "--max-num-seqs"),
            "max_num_batched_tokens": _command_value(parts, "--max-num-batched-tokens"),
            "server_command_sha256": sha256_bytes(command.encode("utf-8")) if command else None,
        },
    }


def classify_stage(system_prompt):
    stack_files = " ".join(frame.filename.replace("\\", "/").lower() for frame in inspect.stack()[2:18])
    text = str(system_prompt or "")
    if "claim_verifier" in stack_files or "证据核验" in text or "证据链" in text:
        return "evidence"
    if "conversation" in stack_files or "retrieval" in stack_files or "问答" in text:
        return "structured_qa"
    if "report" in stack_files or "报告" in text or "研究规划" in text:
        return "reporting"
    if "export" in stack_files or "导出" in text or "整编" in text:
        return "export"
    if "catalog" in stack_files or "naming" in stack_files or "扫描" in text or "分类" in text:
        return "scan"
    return "document_analysis"


def classify_feature(system_prompt):
    frames = inspect.stack()[2:24]
    stack = " ".join(
        "{} {}".format(frame.filename.replace("\\", "/").lower(), frame.function.lower())
        for frame in frames
    )
    text = str(system_prompt or "")
    rules = (
        ("general_chat", "general_chat.generation"),
        ("test_model", "general_chat.model_test"),
        ("mail/research", "mail.research"),
        ("mail/facts", "mail.fact_extraction"),
        ("claim_verifier", "conversation.claim_revision"),
        ("conversation", "conversation.answer_generation"),
        ("large_package_catalog_batch", "large_package.batch_naming"),
        ("large_package_catalog_hierarchy", "large_package.hierarchy_naming"),
        ("large_package_catalog_merge", "large_package.merge_decision"),
        ("large_package_catalog_naming", "large_package.name_improvement"),
        ("large_package_catalog_topics", "large_package.topic_generation"),
        ("large_package_reporting", "large_package.reporting"),
        ("preliminary_summary_batch", "document.batch_preview"),
        ("preliminary_summary_fast", "document.single_preview"),
        ("folder_analysis", "folder.summary"),
        ("document_analysis", "document.deep_analysis"),
        ("package_analysis", "package.analysis"),
        ("reporting", "report.enhancement"),
    )
    for marker, feature_id in rules:
        if marker in stack:
            return feature_id
    if "邮件" in text:
        return "mail.research"
    if "问答" in text or "证据" in text:
        return "conversation.answer_generation"
    return "unclassified.generation"


def _callsite():
    for frame in inspect.stack()[2:24]:
        module = frame.frame.f_globals.get("__name__", "")
        if module not in {__name__, "services.vllm", "services.agent_runtime"}:
            return {"module": module, "function": frame.function, "line": frame.lineno}
    return {"module": None, "function": None, "line": None}


@contextmanager
def context(feature_id=None, stage_id=None, scenario_id=None, parent_call_ids=None,
            metadata=None):
    """Attach explicit business provenance to every nested model request."""
    current = dict(_TRACE_CONTEXT.get() or {})
    update = {
        "feature_id": feature_id or current.get("feature_id"),
        "stage_id": stage_id or current.get("stage_id"),
        "scenario_id": scenario_id or current.get("scenario_id"),
        "parent_call_ids": (
            list(parent_call_ids) if parent_call_ids is not None
            else current.get("parent_call_ids")
        ),
        "metadata": {**(current.get("metadata") or {}), **(metadata or {})},
    }
    token = _TRACE_CONTEXT.set(update)
    try:
        yield
    finally:
        _TRACE_CONTEXT.reset(token)


def last_call_id():
    return _LAST_CALL_ID.get()


class TraceRecorder:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size:
            raise RuntimeError("trace path already contains data: {}".format(self.path))
        self.trace_id = uuid.uuid4().hex
        self.workflow_id = os.getenv("SJFX_TRACE_WORKFLOW_ID") or uuid.uuid4().hex[:16]
        self._lock = threading.RLock()
        self._sequence = 0
        self._group_sequence = 0
        self._active = {}
        self._manifest_written = False
        self._started = time.perf_counter()

    def _append(self, value):
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def ensure_manifest(self, metadata):
        with self._lock:
            if self._manifest_written:
                return
            self._append({
                "schema_version": "1.0.0",
                "kind": "TraceManifest",
                "trace_id": self.trace_id,
                "workflow_id": self.workflow_id,
                "created_at": utc_now(),
                "host": socket.gethostname(),
                "agent_id": "sjfx",
                "capture_type": "synthetic_fixture_real_business_pipeline",
                "generation_policy": {
                    "temperature": 0,
                    "top_p": 1.0,
                    "seed": 42,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "max_tokens": "caller_stage_value",
                },
                **metadata,
            })
            self._manifest_written = True

    def begin(self, request, system_prompt):
        with self._lock:
            self._sequence += 1
            self._group_sequence += 1 if not self._active else 0
            group_id = "overlap-{:04d}".format(self._group_sequence)
            if self._active:
                group_id = next(iter(self._active.values()))["concurrency_group"]
            call_id = uuid.uuid4().hex
            previous = _LAST_CALL_ID.get()
            trace_context = dict(_TRACE_CONTEXT.get() or {})
            explicit_parents = trace_context.get("parent_call_ids")
            parents = list(explicit_parents) if explicit_parents is not None else ([previous] if previous else [])
            state = {
                "sequence": self._sequence,
                "call_id": call_id,
                "feature_id": trace_context.get("feature_id") or classify_feature(system_prompt),
                "stage_id": trace_context.get("stage_id") or classify_stage(system_prompt),
                "scenario_id": trace_context.get("scenario_id") or os.getenv("SJFX_TRACE_SCENARIO_ID"),
                "callsite": _callsite(),
                "parent_call_ids": parents,
                "parent_call_ids_source": (
                    "explicit_business_dependency" if explicit_parents is not None
                    else "previous_call_in_same_execution_context"
                ),
                "context_metadata": trace_context.get("metadata") or {},
                "concurrency_group": group_id,
                "arrival_offset_ms": round((time.perf_counter() - self._started) * 1000, 3),
                "started_at": utc_now(),
                "started_monotonic": time.perf_counter(),
                "request": request,
            }
            self._active[call_id] = state
            return state

    def finish(self, state, response=None, response_raw=None, error=None,
               source_cached_tokens=None, source_cached_tokens_source=None):
        call_id = state["call_id"]
        with self._lock:
            self._active.pop(call_id, None)
            _LAST_CALL_ID.set(call_id)
            response = response or {}
            choices = response.get("choices") or []
            first_choice = choices[0] if choices else {}
            input_ids = response.get("prompt_token_ids") or []
            output_ids = first_choice.get("token_ids") or []
            usage = response.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            cached = details.get("cached_tokens")
            if source_cached_tokens is not None:
                cached = int(source_cached_tokens)
            row = {
                "schema_version": "1.0.0",
                "kind": "TraceCall",
                "trace_id": self.trace_id,
                "workflow_id": self.workflow_id,
                "sequence": state["sequence"],
                "call_id": call_id,
                "stage_id": state["stage_id"],
                "feature_id": state["feature_id"],
                "scenario_id": state["scenario_id"],
                "callsite": state["callsite"],
                "context_metadata": state["context_metadata"],
                "parent_call_ids": state["parent_call_ids"],
                "parent_call_ids_source": state["parent_call_ids_source"],
                "concurrency_group": state["concurrency_group"],
                "arrival_offset_ms": state["arrival_offset_ms"],
                "started_at": state["started_at"],
                "completed_at": utc_now(),
                "latency_seconds": round(time.perf_counter() - state["started_monotonic"], 6),
                "request": state["request"],
                "response": response,
                "response_raw": response_raw,
                "input_token_ids": input_ids,
                "output_token_ids": output_ids,
                "input_token_ids_source": "response.prompt_token_ids" if input_ids else None,
                "output_token_ids_source": "response.choices[0].token_ids" if output_ids else None,
                "source_cached_tokens": int(cached) if cached is not None else None,
                "source_cached_tokens_source": (
                    source_cached_tokens_source
                    if source_cached_tokens is not None
                    else (
                        "response.usage.prompt_tokens_details.cached_tokens"
                        if cached is not None else "not_exposed_by_server"
                    )
                ),
                "status": "error" if error else "ok",
                "error": error,
            }
            self._append(row)

    def discard(self, call_id):
        """Remove one completed, rejected attempt from the capture trace.

        A structured-output correction is a replacement request, not a useful
        benchmark case in its own right.  The transport has already recorded
        the first HTTP response by the time the application can reject its
        JSON contract, so remove exactly that completed call before the
        capture is finalized.  This method is deliberately narrow: it never
        removes the manifest, active calls, or unrelated concurrent calls.
        """
        if not call_id or not self.path.exists():
            return False
        with self._lock:
            rows = [
                json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            kept = [
                row for row in rows
                if not (row.get("kind") == "TraceCall" and row.get("call_id") == call_id)
            ]
            if len(kept) == len(rows):
                return False
            temporary = self.path.with_name(self.path.name + ".discarding")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                for row in kept:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            if _LAST_CALL_ID.get() == call_id:
                _LAST_CALL_ID.set(None)
            return True

    def abandon(self, state):
        """Forget an unsuccessful transport attempt without writing a row."""
        if not state:
            return
        with self._lock:
            self._active.pop(state.get("call_id"), None)


def get_recorder():
    global _RECORDER
    path = os.getenv("SJFX_LLM_TRACE_PATH", "").strip()
    if not path:
        return None
    with _RECORDER_LOCK:
        if _RECORDER is None:
            _RECORDER = TraceRecorder(path)
        elif _RECORDER.path != Path(path).expanduser().resolve():
            raise RuntimeError("one process cannot write multiple trace paths")
        return _RECORDER


def _normalized_parent_call_ids(calls, index):
    call = calls[index]
    if call.get("parent_call_ids_source") == "explicit_business_dependency":
        return list(call.get("parent_call_ids") or [])
    call_by_id = {item["call_id"]: item for item in calls[:index]}
    same_stage = [
        parent_id for parent_id in call.get("parent_call_ids") or []
        if (call_by_id.get(parent_id) or {}).get("stage_id") == call["stage_id"]
    ]
    if same_stage:
        return same_stage
    stage_order = {
        "scan": 0,
        "document_analysis": 1,
        "evidence": 2,
        "structured_qa": 3,
        "reporting": 4,
        "export": 5,
    }
    current_order = stage_order.get(call["stage_id"], 999)
    predecessor_orders = [
        stage_order.get(item["stage_id"], -1)
        for item in calls[:index]
        if stage_order.get(item["stage_id"], -1) < current_order
    ]
    if not predecessor_orders:
        return []
    predecessor_order = max(predecessor_orders)
    return [
        item["call_id"] for item in calls[:index]
        if stage_order.get(item["stage_id"], -1) == predecessor_order
    ]


def write_cases_jsonl(trace_path, cases_path):
    trace_path = Path(trace_path)
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    manifest = next(row for row in rows if row.get("kind") == "TraceManifest")
    calls = [row for row in rows if row.get("kind") == "TraceCall"]
    tokenizer_fingerprint = (manifest.get("tokenizer") or {}).get("fingerprint")
    with Path(cases_path).open("w", encoding="utf-8", newline="\n") as handle:
        for call_index, call in enumerate(calls):
            line_number = call_index + 2
            request = call["request"]
            response = call.get("response") or {}
            usage = response.get("usage") or {}
            output = ((((response.get("choices") or [{}])[0].get("message") or {}).get("content")))
            case = {
                "schema_version": "3.0.0",
                "kind": "WorkloadCase",
                "fixture": True,
                "case_id": "sjfx.business.{}.{:04d}".format(call["stage_id"], call["sequence"]),
                "agent_id": "sjfx",
                "stage_id": call["stage_id"],
                "mode": "replay",
                "capture": {
                    "type": "real_capture",
                    "source_ref": "{}#L{}".format(trace_path.name, line_number),
                    "trace_id": call["trace_id"],
                    "workflow_id": call["workflow_id"],
                    "call_id": call["call_id"],
                    "parent_call_ids": _normalized_parent_call_ids(calls, call_index),
                    "arrival_offset_ms": call["arrival_offset_ms"],
                    "sensitivity": "restricted",
                    "transformations": [],
                },
                "payload": {"endpoint": "/v1/chat/completions", "body": request},
                "reference": {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "tokenizer_fingerprint": tokenizer_fingerprint,
                    "output_ref": "{}#L{}".format(trace_path.name, line_number),
                },
                "features": {
                    "prefix_group": None,
                    "has_tools": bool(request.get("tools")),
                    "thinking": "off",
                    "structured_output": bool(request.get("response_format")),
                },
                "controlled_output_tokens": None,
                "extensions": {
                    "capture_type": manifest["capture_type"],
                    "observed_output": output,
                    "observed_response": response,
                    "input_token_ids": call["input_token_ids"],
                    "output_token_ids": call["output_token_ids"],
                    "source_cached_tokens": call["source_cached_tokens"],
                    "source_cached_tokens_source": call["source_cached_tokens_source"],
                    "concurrency_group": call["concurrency_group"],
                    "observed_latency_seconds": call["latency_seconds"],
                },
            }
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")


def write_calls_json(trace_path, calls_path, metadata_path):
    """Export the user-facing ``[{call}, ...]`` dataset and file metadata."""
    trace_path = Path(trace_path)
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    manifest = next(row for row in rows if row.get("kind") == "TraceManifest")
    trace_calls = [row for row in rows if row.get("kind") == "TraceCall"]
    group_counts = {}
    for call_index, call in enumerate(trace_calls):
        group = call.get("concurrency_group")
        group_counts[group] = group_counts.get(group, 0) + 1
    calls = []
    for call_index, call in enumerate(trace_calls):
        raw_response = call.get("response") or {}
        choices = raw_response.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        group = call.get("concurrency_group")
        calls.append({
            "sequence": call["sequence"],
            "call_id": call["call_id"],
            "stage_id": call["stage_id"],
            "parent_call_ids": _normalized_parent_call_ids(trace_calls, call_index),
            "concurrency_group": group if group_counts.get(group, 0) > 1 else None,
            "request": call["request"],
            "response": {
                "content": message.get("content"),
                "finish_reason": choice.get("finish_reason"),
                "usage": raw_response.get("usage") or {},
            },
            "input_token_ids": call["input_token_ids"],
            "output_token_ids": call["output_token_ids"],
            "source_cached_tokens": call["source_cached_tokens"],
        })
    Path(calls_path).write_text(
        json.dumps(calls, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    metadata = dict(manifest)
    metadata["kind"] = "TraceMetadata"
    metadata["call_count"] = len(calls)
    metadata["calls_file"] = Path(calls_path).name
    Path(metadata_path).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
