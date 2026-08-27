#!/usr/bin/env python3
"""Profile one complete SJFX package-analysis run without touching production state.

The runner reuses the product scanner, parser, embedding/clustering pipeline and
report planner.  It writes its SQLite database, parser cache and generated
report into a run-specific directory, waits for the shared GPU to be idle, and
records every model call with native Ollama token/timing counters.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import csv
import json
import math
import os
import platform
import shutil
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
for import_root in (str(PROJECT_ROOT), str(SCRIPT_ROOT)):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

from benchmark_llm_migration import (  # noqa: E402
    DeviceMonitor,
    aggregate_resources,
    read_active_jobs,
    wait_for_idle,
)
from benchmark_package import (  # noqa: E402
    compare_inventory,
    measure_real_input,
    sha256_file,
)


GIB = 1024 ** 3
SCHEMA_VERSION = "sjfx-full-pipeline-profile/1.0"
STAGE_NAME_MAP = {
    "主题聚类命名": "semantic_cluster_naming",
    "子方向命名": "subtopic_naming",
    "报告研究方向分析": "report_research_direction",
}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def percentile(values, quantile):
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return None
    rank = max(1, math.ceil(float(quantile) * len(clean)))
    return clean[min(rank - 1, len(clean) - 1)]


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(str(temporary), str(path))


class RequestRecorder:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.rows = []
        self._lock = threading.Lock()
        self.jsonl_path = self.output_dir / "model_requests.jsonl"

    def append(self, row):
        material = dict(row)
        with self._lock:
            self.rows.append(material)
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(material, ensure_ascii=False) + "\n")
            write_csv(self.output_dir / "model_requests.csv", self.rows)


class CoarseStageRecorder:
    def __init__(self, monitor):
        self.monitor = monitor
        self.rows = []

    def run(self, stage_name, function):
        self.monitor.set_case("pipeline:" + stage_name)
        started_wall = time.perf_counter()
        started_cpu = time.process_time()
        started_at = utc_now()
        row = {
            "stage_name": stage_name,
            "started_at": started_at,
            "status": "running",
        }
        try:
            value = function()
            row["status"] = "completed"
            return value
        except Exception as exc:
            row.update({
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc)[:2000],
            })
            raise
        finally:
            row.update({
                "completed_at": utc_now(),
                "seconds": round(time.perf_counter() - started_wall, 6),
                "driver_cpu_seconds": round(time.process_time() - started_cpu, 6),
            })
            self.rows.append(row)


class AnalysisProgressRecorder:
    def __init__(self, monitor):
        self.monitor = monitor
        self.events = []
        self.phases = []
        self._phase = None
        self._phase_started = None
        self._phase_started_at = None

    @staticmethod
    def classify(percent, message):
        value = int(percent or 0)
        text = str(message or "")
        if "语义向量" in text:
            return "embedding_and_semantic_clustering"
        if "语义主题名称" in text:
            return "semantic_cluster_naming"
        if "摘要" in text or "证据链" in text:
            return "summary_and_evidence"
        if "子方向名称" in text:
            return "subtopic_naming"
        if value < 74:
            return "inventory_document_parsing"
        if value < 82:
            return "embedding_and_semantic_clustering"
        if value < 88:
            return "summary_and_evidence"
        if value < 90:
            return "analysis_tree"
        return "analysis_finalization"

    def _transition(self, phase):
        now = time.perf_counter()
        if phase == self._phase:
            return
        if self._phase is not None:
            self.phases.append({
                "stage_name": self._phase,
                "started_at": self._phase_started_at,
                "completed_at": utc_now(),
                "seconds": round(now - self._phase_started, 6),
                "status": "completed",
            })
        self._phase = phase
        self._phase_started = now
        self._phase_started_at = utc_now()
        self.monitor.set_case("analysis:" + phase)

    def __call__(self, percent, message):
        phase = self.classify(percent, message)
        self._transition(phase)
        self.events.append({
            "timestamp": utc_now(),
            "monotonic_s": round(time.monotonic(), 6),
            "percent": int(percent or 0),
            "stage_name": phase,
            "message": str(message or ""),
        })

    def finish(self, status="completed"):
        if self._phase is None:
            return
        self.phases.append({
            "stage_name": self._phase,
            "started_at": self._phase_started_at,
            "completed_at": utc_now(),
            "seconds": round(time.perf_counter() - self._phase_started, 6),
            "status": status,
        })
        self._phase = None


class ProfilingOllamaTransport:
    def __init__(self, *, base_url, model, timeout, context_window_tokens,
                 monitor, recorder, production_db):
        self.base_url = str(base_url or "").rstrip("/")
        self.model = str(model or "")
        self.timeout = max(10, int(timeout))
        self.context_window_tokens = max(1, int(context_window_tokens))
        self.monitor = monitor
        self.recorder = recorder
        self.production_db = str(production_db or "")
        self._semaphore = threading.BoundedSemaphore(1)
        self._stage = contextvars.ContextVar("sjfx_profile_stage", default="model_call")
        self._counter = 0
        self._counter_lock = threading.Lock()

    @property
    def configured(self):
        return bool(self.base_url and self.model)

    @property
    def requires_confirmation(self):
        return False

    @property
    def privacy_label(self):
        return "服务器本机 Ollama（隔离画像运行）"

    def health_check(self, timeout=5):
        native_base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        request = urllib.request.Request(native_base.rstrip("/") + "/api/tags", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            models = [item.get("name") or item.get("model") for item in data.get("models", [])]
            return {"reachable": True, "model_available": self.model in models, "models": models}
        except Exception as exc:
            return {"reachable": False, "model_available": False, "models": [], "error": str(exc)}

    @contextlib.contextmanager
    def stage(self, output_context):
        token = self._stage.set(STAGE_NAME_MAP.get(str(output_context), str(output_context)))
        try:
            yield
        finally:
            self._stage.reset(token)

    def _next_request_id(self, stage_name):
        with self._counter_lock:
            self._counter += 1
            return "{}-{:03d}-{}".format(stage_name, self._counter, uuid.uuid4().hex[:6])

    def chat(self, system_prompt, user_prompt, temperature=0.1, max_tokens=1800,
             retries=0, timeout=None):
        del retries
        stage_name = self._stage.get()
        request_id = self._next_request_id(stage_name)
        active_jobs = read_active_jobs(self.production_db) if self.production_db else []
        if active_jobs:
            raise RuntimeError(
                "production SJFX job appeared before model call {}: {}".format(
                    request_id, active_jobs
                )
            )
        native_base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": str(system_prompt or "")},
                {"role": "user", "content": str(user_prompt or "")},
            ],
            "stream": True,
            "think": False,
            "format": "json",
            "options": {
                "temperature": float(temperature),
                "num_predict": max(1, int(max_tokens)),
            },
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            native_base.rstrip("/") + "/api/chat",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
                "User-Agent": "SJFX/full-pipeline-profiler",
            },
            method="POST",
        )
        row = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "stage_name": stage_name,
            "model": self.model,
            "concurrency": 1,
            "context_window_tokens": self.context_window_tokens,
            "system_prompt_chars": len(str(system_prompt or "")),
            "user_prompt_chars": len(str(user_prompt or "")),
            "request_body_bytes": len(body),
            "max_output_tokens": int(max_tokens),
            "started_at": utc_now(),
            "status": "running",
        }
        self.monitor.set_case("llm:" + request_id)
        request_started = time.perf_counter()
        connected_at = None
        first_content_at = None
        content_parts = []
        final = {}
        try:
            effective_timeout = max(1, int(timeout if timeout is not None else self.timeout))
            with self._semaphore:
                with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                    connected_at = time.perf_counter()
                    for raw_line in response:
                        if not raw_line.strip():
                            continue
                        data = json.loads(raw_line.decode("utf-8", errors="replace"))
                        message = data.get("message") or {}
                        content = message.get("content") or ""
                        if content:
                            if first_content_at is None:
                                first_content_at = time.perf_counter()
                            content_parts.append(content)
                        if data.get("done"):
                            final = data
                            break
            content = "".join(content_parts).strip()
            if not content:
                raise RuntimeError("Ollama returned no final content")
            completed_at = time.perf_counter()
            input_tokens = int(final.get("prompt_eval_count") or 0)
            output_tokens = int(final.get("eval_count") or 0)
            context_tokens = input_tokens + output_tokens
            prompt_eval_ms = float(final.get("prompt_eval_duration") or 0) / 1_000_000.0
            eval_ms = float(final.get("eval_duration") or 0) / 1_000_000.0
            row.update({
                "status": "ok",
                "completed_at": utc_now(),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "context_tokens": context_tokens,
                "context_occupancy_pct": round(
                    100.0 * context_tokens / float(self.context_window_tokens), 6
                ),
                "connect_ms": round((connected_at - request_started) * 1000.0, 3),
                "ttft_ms": round((first_content_at - request_started) * 1000.0, 3),
                "client_e2e_ms": round((completed_at - request_started) * 1000.0, 3),
                "ollama_total_ms": round(float(final.get("total_duration") or 0) / 1_000_000.0, 3),
                "load_ms": round(float(final.get("load_duration") or 0) / 1_000_000.0, 3),
                "prompt_eval_ms": round(prompt_eval_ms, 3),
                "eval_ms": round(eval_ms, 3),
                "prefill_tokens_s": round(input_tokens / (prompt_eval_ms / 1000.0), 3)
                if input_tokens and prompt_eval_ms else None,
                "decode_tokens_s": round(output_tokens / (eval_ms / 1000.0), 3)
                if output_tokens and eval_ms else None,
                "tpot_ms": round(eval_ms / output_tokens, 3) if output_tokens else None,
                "done_reason": final.get("done_reason"),
                "model_reported": final.get("model"),
                "response_chars": len(content),
            })
            row.update(aggregate_resources(self.monitor.samples, "llm:" + request_id))
            return {
                "content": content,
                "reasoning_content": None,
                "model": final.get("model") or self.model,
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                },
                "finish_reason": final.get("done_reason") or "stop",
                "profile_request_id": request_id,
            }
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            row.update({
                "status": "error",
                "error_type": type(exc).__name__,
                "error": "Ollama HTTP {}: {}".format(exc.code, detail[:500]),
            })
            raise RuntimeError(row["error"]) from exc
        except Exception as exc:
            row.update({
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
            })
            raise
        finally:
            row.setdefault("completed_at", utc_now())
            row.setdefault("client_e2e_ms", round((time.perf_counter() - request_started) * 1000.0, 3))
            self.recorder.append(row)
            self.monitor.set_case("pipeline:model-cooldown")


class ProfilingAgentRuntime:
    def __init__(self, transport):
        self.transport = transport
        self.model = transport.model
        self.base_url = transport.base_url
        self.configured = transport.configured
        self.requires_confirmation = transport.requires_confirmation
        self.privacy_label = transport.privacy_label

    def health_check(self, *args, **kwargs):
        return self.transport.health_check(*args, **kwargs)

    def chat(self, system_prompt, user_prompt, **kwargs):
        from services.agent_runtime import UNTRUSTED_DOCUMENT_POLICY

        return self.transport.chat(
            UNTRUSTED_DOCUMENT_POLICY + "\n" + str(system_prompt or ""),
            user_prompt,
            **kwargs
        )

    def chat_json(self, system_prompt, user_prompt, *, required_fields=None,
                  output_context="模型结构化输出", **kwargs):
        from services.agent_runtime import UNTRUSTED_DOCUMENT_POLICY
        from services.model_output import extract_json_value, validate_json_object
        from services.ollama import LocalModelError

        with self.transport.stage(output_context):
            result = self.transport.chat(
                UNTRUSTED_DOCUMENT_POLICY + "\n" + str(system_prompt or "")
                + "\n只返回一个合法 JSON 对象，不要 Markdown 代码围栏。",
                user_prompt,
                temperature=0.1,
                max_tokens=kwargs.get("max_tokens", 2400),
                retries=kwargs.get("retries", 0),
                timeout=kwargs.get("timeout"),
            )
        try:
            payload = validate_json_object(
                extract_json_value(result["content"]),
                required_fields=required_fields,
                context=output_context,
            )
        except Exception as exc:
            raise LocalModelError(
                "{} 返回不符合结构化契约：{}".format(output_context, exc)
            ) from exc
        return {
            "content": result["content"],
            "json": payload,
            "model": result.get("model"),
            "usage": result.get("usage") or {},
        }


def runtime_imports():
    from config import Config
    from services.exporter import create_report_docx
    from services.ollama import OllamaEmbeddingClient
    from services.package_analysis import analyze_package
    from services.reporting import (
        build_local_report,
        build_report_analysis_prompt,
        merge_model_report,
    )
    from services.scanner import scan_directory
    from services.storage import Storage
    from services.unified_parser import UnifiedDocumentParser

    return {
        "Config": Config,
        "create_report_docx": create_report_docx,
        "OllamaEmbeddingClient": OllamaEmbeddingClient,
        "analyze_package": analyze_package,
        "build_local_report": build_local_report,
        "build_report_analysis_prompt": build_report_analysis_prompt,
        "merge_model_report": merge_model_report,
        "scan_directory": scan_directory,
        "Storage": Storage,
        "UnifiedDocumentParser": UnifiedDocumentParser,
    }


def input_hashes(root):
    rows = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.is_symlink():
            rows.append({
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    return rows


def model_stage_summary(requests):
    grouped = defaultdict(list)
    for row in requests:
        if row.get("status") == "ok":
            grouped[row.get("stage_name") or "unknown"].append(row)
    metric_names = (
        "input_tokens", "output_tokens", "context_tokens", "context_occupancy_pct",
        "ttft_ms", "tpot_ms", "client_e2e_ms", "prefill_tokens_s", "decode_tokens_s",
    )
    rows = []
    for stage_name in sorted(grouped):
        selected = grouped[stage_name]
        result = {"stage_name": stage_name, "model_call_count": len(selected)}
        for metric in metric_names:
            values = [item.get(metric) for item in selected if item.get(metric) is not None]
            for label, quantile in (("p50", 0.50), ("p90", 0.90), ("p95", 0.95), ("p99", 0.99)):
                result["{}_{}".format(metric, label)] = percentile(values, quantile)
            result["{}_max".format(metric)] = max(values) if values else None
        rows.append(result)
    return rows


def aggregate_stage_rows(coarse_rows, analysis_rows, model_rows):
    rows = []
    rows.extend(dict(item, stage_kind="pipeline") for item in coarse_rows)
    rows.extend(dict(item, stage_kind="analysis_phase") for item in analysis_rows)
    rows.extend(dict(item, stage_kind="model") for item in model_rows)
    return rows


def configure_isolated_environment(state_dir, parse_temp_dir):
    os.environ["SJFX_STATE_DIR"] = str(state_dir)
    os.environ["SJFX_PARSE_TEMP_DIR"] = str(parse_temp_dir)
    os.environ["PARSE_MAX_CONCURRENCY"] = "1"
    os.environ["LLM_MAX_CONCURRENCY"] = "1"
    os.environ["DOCLING_CPU_THREADS"] = "2"
    os.environ["OMP_NUM_THREADS"] = "2"
    os.environ["MKL_NUM_THREADS"] = "2"
    os.environ["ENABLE_OPTIONAL_LLM_ENRICHMENT"] = "true"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="real package directory to profile")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--production-db", type=Path, default=None)
    parser.add_argument("--mode", choices=("fast", "accurate"), default="fast")
    parser.add_argument("--context-window-tokens", type=int, default=65536)
    parser.add_argument("--expected-size-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--size-tolerance-percent", type=float, default=15.0)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--max-preflight-gpu-util", type=int, default=5)
    parser.add_argument("--idle-seconds", type=int, default=30)
    parser.add_argument("--max-wait-seconds", type=int, default=1800)
    parser.add_argument("--request-timeout", type=int, default=600)
    parser.add_argument("--skip-input-hash", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def run(args):
    root = args.root.expanduser().resolve()
    output_base = args.output_dir.expanduser().resolve()
    work_base = args.work_dir.expanduser().resolve()
    production_db = args.production_db.expanduser().resolve() if args.production_db else None
    if not root.is_dir():
        raise ValueError("input root is not a directory: {}".format(root))
    for writable in (output_base, work_base):
        try:
            writable.relative_to(root)
        except ValueError:
            continue
        raise ValueError("writable directory must stay outside input root: {}".format(writable))

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    output_dir = output_base / run_id
    state_dir = work_base / run_id
    parse_temp_dir = state_dir / "parse_temp"
    output_dir.mkdir(parents=True, exist_ok=False)
    state_dir.mkdir(parents=True, exist_ok=False)
    configure_isolated_environment(state_dir, parse_temp_dir)

    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": utc_now(),
        "input_root": str(root),
        "output_dir": str(output_dir),
        "isolated_state_dir": str(state_dir),
        "production_db": str(production_db) if production_db else None,
        "context_window_tokens": int(args.context_window_tokens),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "parse_max_concurrency": 1,
            "llm_max_concurrency": 1,
        },
    }
    monitor = DeviceMonitor(interval=args.sample_interval)
    request_recorder = RequestRecorder(output_dir)
    coarse = CoarseStageRecorder(monitor)
    analysis_progress = AnalysisProgressRecorder(monitor)
    monitor_started = False
    fatal_error = None
    try:
        measured = coarse.run("independent_input_measurement", lambda: measure_real_input(root))
        result["input_measurement"] = measured
        expected = int(args.expected_size_bytes)
        tolerance = float(args.size_tolerance_percent) / 100.0
        size_bytes = int(measured.get("logical_bytes") or 0)
        result["input_contract"] = {
            "expected_size_bytes": expected,
            "actual_size_bytes": size_bytes,
            "actual_size_gib": round(size_bytes / float(GIB), 6),
            "tolerance_percent": float(args.size_tolerance_percent),
            "size_passed": expected * (1.0 - tolerance) <= size_bytes <= expected * (1.0 + tolerance),
            "sparse_file_count": int(measured.get("sparse_file_count") or 0),
            "sparse_files_allowed": False,
        }
        if not result["input_contract"]["size_passed"]:
            raise ValueError("input size is outside the configured tolerance")
        if measured.get("sparse_file_count"):
            raise ValueError("sparse files are not permitted in the profiling fixture")
        if not args.skip_input_hash:
            result["input_files"] = coarse.run("input_sha256", lambda: input_hashes(root))

        preflight_sample = monitor.sample_once()
        active_jobs = read_active_jobs(str(production_db)) if production_db else []
        result["preflight_snapshot"] = {
            "timestamp": utc_now(),
            "active_production_jobs": active_jobs,
            "resource_sample": preflight_sample,
        }
        if args.preflight_only:
            result["status"] = "preflight_completed"
            return result, coarse, analysis_progress, request_recorder, monitor

        result["idle_gate"] = wait_for_idle(
            monitor,
            max_util=args.max_preflight_gpu_util,
            idle_seconds=args.idle_seconds,
            max_wait_seconds=args.max_wait_seconds,
            db_path=str(production_db) if production_db else None,
        )
        monitor.start()
        monitor_started = True

        runtime = runtime_imports()
        Config = runtime["Config"]
        health_transport = ProfilingOllamaTransport(
            base_url=Config.OLLAMA_BASE_URL,
            model=Config.OLLAMA_MODEL,
            timeout=args.request_timeout,
            context_window_tokens=args.context_window_tokens,
            monitor=monitor,
            recorder=request_recorder,
            production_db=production_db,
        )
        health = health_transport.health_check(timeout=10)
        result["model_health"] = health
        if not health.get("reachable") or not health.get("model_available"):
            raise RuntimeError("configured Ollama model is unavailable: {}".format(health))
        llm = ProfilingAgentRuntime(health_transport)
        embedding_client = runtime["OllamaEmbeddingClient"](
            Config.OLLAMA_BASE_URL,
            Config.OLLAMA_EMBED_MODEL,
            timeout=min(600, args.request_timeout),
        )

        scan = coarse.run(
            "inventory",
            lambda: runtime["scan_directory"](
                root,
                max_files=Config.MAX_SCAN_FILES,
                max_depth=Config.MAX_SCAN_DEPTH,
                max_directories=Config.MAX_SCAN_DIRECTORIES,
                max_nodes=Config.MAX_SCAN_NODES,
                max_entries_per_directory=Config.MAX_SCAN_ENTRIES_PER_DIRECTORY,
            ),
        )
        scan["parse_mode"] = args.mode
        result["inventory_contract"] = compare_inventory(measured, scan)
        database_path = state_dir / "profile.db"
        payload_dir = state_dir / "document_payloads"
        storage = runtime["Storage"](database_path, payload_dir, Config.SIDECAR_PAYLOAD_BYTES)
        scan_id = storage.save_scan(scan, owner_id="profiling")
        parser_instance = runtime["UnifiedDocumentParser"](
            Config.DOCLING_ARTIFACTS_DIR,
            Config.RAPIDOCR_MODEL_DIR,
            Config.MAX_FULL_DOCUMENT_CHARS,
        )
        large_options = {
            "threshold_bytes": Config.LARGE_PACKAGE_THRESHOLD_BYTES,
            "threshold_files": Config.LARGE_PACKAGE_THRESHOLD_FILES,
            "initial_parse_files": Config.LARGE_PACKAGE_INITIAL_PARSE_FILES,
            "deepen_batch_files": Config.LARGE_PACKAGE_DEEPEN_BATCH_FILES,
            "batch_files": Config.LARGE_PACKAGE_BATCH_FILES,
            "overview_chars_per_file": Config.LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE,
            "overview_evidence_per_file": Config.LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE,
        }
        try:
            analysis = coarse.run(
                "analysis_pipeline",
                lambda: runtime["analyze_package"](
                    scan_id,
                    scan,
                    storage,
                    parser_instance,
                    progress=analysis_progress,
                    embedding_client=embedding_client,
                    llm=llm,
                    large_options=large_options,
                ),
            )
            analysis_progress.finish("completed")
        except Exception:
            analysis_progress.finish("failed")
            raise
        summaries = storage.list_summaries(scan_id)
        local_report = coarse.run(
            "local_report_assembly",
            lambda: runtime["build_local_report"](scan, summaries, analysis),
        )
        prompt, evidence_catalog = runtime["build_report_analysis_prompt"](
            scan, summaries, analysis, local_report
        )
        report_result = coarse.run(
            "report_research_direction",
            lambda: llm.chat_json(
                "你是严谨的数据包分析与研究规划助手。你必须从证据中归纳，不得使用固定领域模板。"
                "事实与推论严格分开；研究建议必须可验证、可回溯。",
                prompt,
                max_tokens=1800,
                strict=True,
                retries=0,
                timeout=args.request_timeout,
                required_fields=("recommended_research_direction",),
                output_context="报告研究方向分析",
            ),
        )
        report_data = runtime["merge_model_report"](
            local_report, report_result["json"], evidence_catalog
        )
        report_data["model_analysis"] = {
            "status": "completed",
            "model": report_result.get("model"),
            "evidence_catalog_size": len(evidence_catalog),
        }
        report_path = output_dir / "overview_report.docx"
        coarse.run(
            "report_rendering",
            lambda: runtime["create_report_docx"](report_data, scan, report_path),
        )
        result.update({
            "status": "completed",
            "scan_id": scan_id,
            "report_path": str(report_path),
            "analysis_statistics": analysis.get("statistics") or {},
            "coverage": analysis.get("coverage") or {},
            "value_judgment": analysis.get("value_judgment") or {},
            "model_analysis": report_data.get("model_analysis") or {},
        })
    except Exception as exc:
        fatal_error = {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        result["status"] = "failed"
        result["fatal_error"] = fatal_error
    finally:
        if monitor_started:
            monitor.stop()
        result["completed_at"] = utc_now()
        result["model_requests"] = request_recorder.rows
        result["model_stage_summary"] = model_stage_summary(request_recorder.rows)
        result["pipeline_stages"] = coarse.rows
        result["analysis_phases"] = analysis_progress.phases
        result["resource_sample_count"] = len(monitor.samples)
        write_csv(output_dir / "resources.csv", monitor.samples)
        write_csv(output_dir / "pipeline_stages.csv", coarse.rows)
        write_csv(output_dir / "analysis_progress_events.csv", analysis_progress.events)
        write_csv(output_dir / "analysis_phases.csv", analysis_progress.phases)
        write_csv(output_dir / "model_stage_summary.csv", result["model_stage_summary"])
        write_csv(
            output_dir / "all_stage_summary.csv",
            aggregate_stage_rows(coarse.rows, analysis_progress.phases, result["model_stage_summary"]),
        )
        atomic_write_json(output_dir / "summary.json", result)
        try:
            monitor.close()
        except Exception:
            pass
    return result, coarse, analysis_progress, request_recorder, monitor


def main(argv=None):
    args = build_parser().parse_args(argv)
    result, _coarse, _progress, _requests, _monitor = run(args)
    print(json.dumps({
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "output_dir": result.get("output_dir"),
        "model_stage_summary": result.get("model_stage_summary") or [],
        "fatal_error": result.get("fatal_error"),
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"completed", "preflight_completed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
