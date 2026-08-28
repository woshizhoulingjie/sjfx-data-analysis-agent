#!/usr/bin/env python3
"""Run a cooperative real-data SJFX end-to-end and per-feature workload profile."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import random
import statistics
import sys
import threading
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import ijson


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
from profile_full_pipeline import (  # noqa: E402
    ProfilingAgentRuntime,
    ProfilingOllamaTransport,
    STAGE_NAME_MAP,
    atomic_write_json,
    model_stage_summary,
    percentile,
    run as run_full_pipeline,
    utc_now,
    write_csv,
)


SCHEMA_VERSION = "sjfx-all-feature-profile/1.1"
MIB = 1024 ** 2
STAGE_NAME_MAP.update({
    "全文文档分析": "full_document_analysis",
    "文档分块分析": "document_chunk_analysis",
    "全文分块汇总": "document_chunk_reduce",
    "目录节点摘要": "folder_node_summary",
    "模型连接测试": "model_connection_test",
})

STAGE_LABELS = {
    "semantic_cluster_naming": "主题聚类命名",
    "subtopic_naming": "子方向命名",
    "report_research_direction": "推荐研究方向",
    "full_document_analysis": "全文文档分析",
    "document_chunk_analysis": "长文档分块分析",
    "document_chunk_reduce": "全文分块汇总",
    "folder_node_summary": "目录节点摘要",
    "model_connection_test": "模型连接控制项",
}

WORKLOAD_CLASSES = {
    "semantic_cluster_naming": "Planning",
    "subtopic_naming": "Planning",
    "report_research_direction": "Planning",
    "full_document_analysis": "Evidence",
    "document_chunk_analysis": "Evidence",
    "document_chunk_reduce": "Synthesis",
    "folder_node_summary": "Synthesis",
    "model_connection_test": "Control",
}


def try_lower_priority():
    try:
        os.nice(10)
        return True
    except (AttributeError, OSError):
        return False


def sha256_file(path, block_size=8 * MIB):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def compact_text(value, limit=1200):
    return " ".join(str(value or "").split())[:limit]


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def distribution(values):
    cleaned = [float(value) for value in values if value is not None]
    return {
        "count": len(cleaned),
        "total": sum(cleaned) if cleaned else 0,
        "mean": round(statistics.mean(cleaned), 6) if cleaned else None,
        "p50": percentile(cleaned, 0.50),
        "p90": percentile(cleaned, 0.90),
        "p95": percentile(cleaned, 0.95),
        "p99": percentile(cleaned, 0.99),
        "max": max(cleaned) if cleaned else None,
    }


class FeatureRequestRecorder:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.rows = []
        self.jsonl_path = self.output_dir / "feature_model_requests.jsonl"
        self._lock = threading.Lock()

    def append(self, row):
        material = dict(row)
        with self._lock:
            self.rows.append(material)
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(material, ensure_ascii=False) + "\n")


class CooperativeProfilingTransport(ProfilingOllamaTransport):
    """Yield to production jobs or unrelated GPU activity before every call."""

    def __init__(self, *args, max_pre_call_gpu_util=10, idle_seconds=8,
                 max_wait_seconds=3600, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_pre_call_gpu_util = int(max_pre_call_gpu_util)
        self.idle_seconds = int(idle_seconds)
        self.max_wait_seconds = int(max_wait_seconds)
        self.cooperative_waits = []

    def _cooperative_gate(self):
        active_jobs = read_active_jobs(self.production_db) if self.production_db else []
        sample = self.monitor.sample_once()
        utilization = sample.get("device_util_percent")
        if active_jobs or utilization is None or utilization > self.max_pre_call_gpu_util:
            waited = wait_for_idle(
                self.monitor,
                max_util=self.max_pre_call_gpu_util,
                idle_seconds=self.idle_seconds,
                max_wait_seconds=self.max_wait_seconds,
                db_path=self.production_db or None,
            )
            waited["trigger_active_jobs"] = active_jobs
            waited["trigger_gpu_util_percent"] = utilization
            self.cooperative_waits.append(waited)

    def chat(self, *args, **kwargs):
        self._cooperative_gate()
        return super().chat(*args, **kwargs)


class FeatureStageRecorder:
    def __init__(self, monitor, request_recorder, dataset_sha256):
        self.monitor = monitor
        self.request_recorder = request_recorder
        self.dataset_sha256 = dataset_sha256
        self.rows = []

    def run(self, stage_name, case_id, function, *, source_bytes=0,
            source_chars=0, model_expected=True, notes=""):
        case_key = "feature:{}:{}".format(stage_name, case_id)
        self.monitor.set_case(case_key)
        before_requests = len(self.request_recorder.rows)
        started_wall = time.perf_counter()
        started_cpu = time.process_time()
        row = {
            "schema_version": SCHEMA_VERSION,
            "stage_name": stage_name,
            "case_id": case_id,
            "case_key": case_key,
            "model_expected": bool(model_expected),
            "source_bytes": int(source_bytes or 0),
            "source_chars": int(source_chars or 0),
            "dataset_sha256": self.dataset_sha256,
            "started_at": utc_now(),
            "status": "running",
            "notes": notes,
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
            return None
        finally:
            completed = time.perf_counter()
            self.monitor.set_case(case_key)
            self.monitor.sample_once()
            new_requests = self.request_recorder.rows[before_requests:]
            for request in new_requests:
                request.update({
                    "feature_stage_name": stage_name,
                    "feature_case_id": case_id,
                    "source_bytes": int(source_bytes or 0),
                    "source_chars": int(source_chars or 0),
                    "dataset_sha256": self.dataset_sha256,
                })
            row.update({
                "completed_at": utc_now(),
                "wall_seconds": round(completed - started_wall, 6),
                "driver_cpu_seconds": round(time.process_time() - started_cpu, 6),
                "model_call_count": len(new_requests),
                "input_tokens": sum(int(item.get("input_tokens") or 0) for item in new_requests),
                "output_tokens": sum(int(item.get("output_tokens") or 0) for item in new_requests),
                "context_tokens": sum(int(item.get("context_tokens") or 0) for item in new_requests),
            })
            row.update(aggregate_resources(self.monitor.samples, case_key))
            self.rows.append(row)


def load_tokenizer(path):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(
        str(path),
        local_files_only=True,
        trust_remote_code=False,
        use_fast=True,
    )


def cve_record_id(record, fallback):
    cve = record.get("cve") if isinstance(record, dict) else {}
    if not isinstance(cve, dict):
        cve = {}
    return str(cve.get("id") or (record.get("id") if isinstance(record, dict) else None) or fallback)


def cve_description(record):
    cve = record.get("cve") if isinstance(record, dict) else {}
    if not isinstance(cve, dict):
        cve = {}
    descriptions = cve.get("descriptions") or (record.get("descriptions") if isinstance(record, dict) else None) or []
    for preferred in ("en", "zh", "zh-CN"):
        for item in descriptions:
            if isinstance(item, dict) and item.get("lang") == preferred and item.get("value"):
                return compact_text(item["value"], 4000)
    for item in descriptions:
        if isinstance(item, dict) and item.get("value"):
            return compact_text(item["value"], 4000)
    return compact_text(json.dumps(record, ensure_ascii=False), 4000)


def record_prefix(path):
    with Path(path).open("rb") as handle:
        header = handle.read(16384)
    if b'"cve_items"' in header:
        return "cve_items.item"
    if b'"vulnerabilities"' in header:
        return "vulnerabilities.item"
    raise ValueError("unsupported NVD JSON structure: {}".format(path))


def collect_dataset(dataset_file, tokenizer_path, output_dir, repetitions):
    tokenizer = load_tokenizer(tokenizer_path)
    sample_targets = [40000] * repetitions + [120000] * repetitions
    sample_parts = [[] for _ in sample_targets]
    sample_chars = [0 for _ in sample_targets]
    sample_index = 0
    reservoir = []
    reservoir_limit = max(80, repetitions * 24)
    rng = random.Random(20260824)
    record_rows = []
    total_proxy_tokens = 0
    total_serialized_bytes = 0
    total_chars = 0
    started = time.perf_counter()

    with Path(dataset_file).open("rb") as handle:
        for index, record in enumerate(
            ijson.items(handle, record_prefix(dataset_file), use_float=True), 1
        ):
            encoded = json_bytes(record)
            text = encoded.decode("utf-8")
            proxy_tokens = len(tokenizer.encode(text, add_special_tokens=False))
            record_id = cve_record_id(record, "record-{}".format(index))
            record_rows.append({
                "record_index": index,
                "record_id": record_id,
                "serialized_bytes": len(encoded),
                "chars": len(text),
                "proxy_tokens": proxy_tokens,
            })
            total_serialized_bytes += len(encoded)
            total_chars += len(text)
            total_proxy_tokens += proxy_tokens

            if sample_index < len(sample_targets):
                sample_parts[sample_index].append(text)
                sample_chars[sample_index] += len(text) + 1
                if sample_chars[sample_index] >= sample_targets[sample_index]:
                    sample_index += 1

            compact_record = {
                "record_id": record_id,
                "text": text,
                "description": cve_description(record),
                "serialized_bytes": len(encoded),
                "chars": len(text),
                "proxy_tokens": proxy_tokens,
            }
            if len(reservoir) < reservoir_limit:
                reservoir.append(compact_record)
            else:
                position = rng.randint(1, index)
                if position <= reservoir_limit:
                    reservoir[position - 1] = compact_record

    if sample_index < len(sample_targets):
        raise RuntimeError("dataset did not contain enough text for all test samples")

    short_samples = ["\n".join(parts) for parts in sample_parts[:repetitions]]
    long_samples = ["\n".join(parts) for parts in sample_parts[repetitions:]]
    write_csv(Path(output_dir) / "dataset_record_distribution.csv", record_rows)
    census = {
        "schema_version": SCHEMA_VERSION,
        "dataset_file": str(Path(dataset_file).resolve()),
        "dataset_file_bytes": Path(dataset_file).stat().st_size,
        "dataset_file_mib": round(Path(dataset_file).stat().st_size / float(MIB), 6),
        "dataset_sha256": sha256_file(dataset_file),
        "record_count": len(record_rows),
        "serialized_record_bytes": total_serialized_bytes,
        "serialized_record_chars": total_chars,
        "proxy_tokenizer": str(Path(tokenizer_path).resolve()),
        "proxy_token_note": "全量 Token 使用本机 Qwen2.5 tokenizer 逐 CVE 记录精确计数；真实 Qwen3.5 模型调用以 Ollama prompt_eval_count 为准。",
        "proxy_tokens_total": total_proxy_tokens,
        "proxy_tokens_per_mib": round(total_proxy_tokens / max(1.0, Path(dataset_file).stat().st_size / float(MIB)), 6),
        "record_bytes_distribution": distribution(item["serialized_bytes"] for item in record_rows),
        "record_chars_distribution": distribution(item["chars"] for item in record_rows),
        "record_proxy_tokens_distribution": distribution(item["proxy_tokens"] for item in record_rows),
        "short_sample_chars": [len(value) for value in short_samples],
        "long_sample_chars": [len(value) for value in long_samples],
        "reservoir_record_count": len(reservoir),
        "census_seconds": round(time.perf_counter() - started, 6),
    }
    atomic_write_json(Path(output_dir) / "dataset_census.json", census)
    return census, short_samples, long_samples, reservoir


def evidence_item(record, index):
    evidence_id = "E-CVE-{:04d}".format(index + 1)
    source_path = "cve/{}.json".format(record["record_id"])
    description = record["description"]
    return {
        "evidence_id": evidence_id,
        "source_path": source_path,
        "section": "CVE description",
        "text": description,
        "supporting_quote": description[:280],
        "label": "paragraph",
        "parser": "NVD JSON",
        "content_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
    }


def build_documents(records):
    documents = {}
    for index, record in enumerate(records):
        path = "cve/{}.json".format(record["record_id"])
        evidence = evidence_item(record, index)
        documents[path] = {
            "path": path,
            "text": record["text"],
            "structure": {
                "title": record["record_id"],
                "headings": [record["record_id"], "Description", "Metrics", "Configurations"],
            },
            "source": {
                "path": path,
                "size": record["serialized_bytes"],
                "sha256": hashlib.sha256(record["text"].encode("utf-8")).hexdigest(),
            },
            "evidence": [evidence],
            "payload": {"evidence": [evidence]},
        }
    return documents


def build_semantic_clusters(documents, cluster_count=4):
    paths = sorted(documents)
    cluster_count = max(1, min(cluster_count, len(paths)))
    groups = [[] for _ in range(cluster_count)]
    for index, path in enumerate(paths):
        groups[index % cluster_count].append(path)
    return [
        {
            "cluster_id": "SEM-{:04d}".format(index + 1),
            "members": members,
            "representative_documents": members[:3],
            "mean_similarity": None,
            "keywords": [],
            "name": None,
            "summary": None,
            "algorithm": "profile-fixture-real-cve-group",
        }
        for index, members in enumerate(groups)
        if members
    ]


def build_subtopic_tree(documents):
    paths = sorted(documents)
    topics = []
    for topic_index in range(2):
        topic_paths = paths[topic_index::2]
        children = []
        for sub_index in range(2):
            members = topic_paths[sub_index::2]
            if not members:
                continue
            evidences = [documents[path]["evidence"][0] for path in members[:3]]
            node_id = "group-profile-{}-{}".format(topic_index + 1, sub_index + 1)
            children.append({
                "kind": "group",
                "node_type": "subtopic",
                "node_id": node_id,
                "name": "漏洞相关资料{}".format(sub_index + 1),
                "summary": "真实 NVD CVE 记录分组。",
                "file_count": len(members),
                "member_paths": members,
                "representative_documents": members[:3],
                "evidence_chain": evidences,
                "conclusion_evidence": [{
                    "analysis_question": "这些 CVE 记录包含哪些共同风险描述？",
                    "answer": evidences[0]["text"] if evidences else "证据不足",
                    "statement": evidences[0]["text"] if evidences else "证据不足",
                    "evidence": evidences,
                    "evidence_ids": [item["evidence_id"] for item in evidences],
                }],
                "children": [],
            })
        topics.append({
            "kind": "group",
            "node_type": "topic",
            "name": "CVE 主题组{}".format(topic_index + 1),
            "member_paths": topic_paths,
            "children": children,
        })
    return {"kind": "group", "name": "NVD CVE 真实数据集", "children": topics}


def build_folder_context(documents, dataset_bytes):
    paths = sorted(documents)
    clusters = []
    for index, members in enumerate((paths[::2], paths[1::2]), 1):
        members = list(members)
        if not members:
            continue
        evidence = [documents[path]["evidence"][0] for path in members[:6]]
        clusters.append({
            "cluster_id": "FOLDER-{:02d}".format(index),
            "topic": "NVD CVE 风险描述组{}".format(index),
            "members": members,
            "representative_documents": members[:3],
            "evidence_chain": evidence,
        })
    return {
        "total_files": len(paths),
        "total_dirs": 1,
        "total_size_human": "{:.2f} MiB".format(dataset_bytes / float(MIB)),
        "type_counts": {".json": len(paths)},
        "sampled_files": len(paths),
        "sample_truncated": True,
        "coverage": {
            "complete": False,
            "sampled_record_count": len(paths),
            "dataset_scope_bytes": dataset_bytes,
            "limitations": ["目录摘要使用当前真实 NVD CVE 数据集中的代表记录。"],
        },
        "documents": [
            {"path": path, "payload": documents[path]["payload"]}
            for path in paths
        ],
        "topic_clusters": clusters,
    }


def build_report_fixture(documents, dataset_bytes):
    paths = sorted(documents)
    evidence = [documents[path]["evidence"][0] for path in paths[:8]]
    analysis = {
        "coverage": {"parsed_file_ratio": 1.0, "limitations": []},
        "topic_clusters": [{
            "cluster_id": "REPORT-01",
            "topic": "真实 NVD CVE 漏洞描述",
            "members": paths,
            "representative_documents": paths[:5],
            "evidence_chain": evidence,
        }],
        "document_index": [documents[path] for path in paths],
        "research_retrieval": {"queries": []},
    }
    scan = {
        "file_count": len(paths),
        "directory_count": 1,
        "total_size_human": "{:.2f} MiB".format(dataset_bytes / float(MIB)),
        "type_counts": {".json": len(paths)},
        "truncated": False,
        "errors": [],
    }
    summaries = [{
        "path": ".",
        "payload": {
            "title": "NVD CVE 数据概览",
            "summary": "基于真实 NVD CVE 记录形成的受控测试摘要。",
            "topics": ["CVE", "漏洞描述", "影响产品", "评分与配置"],
            "key_facts": [item["text"] for item in evidence[:4]],
        },
    }]
    local_report = {
        "global_categories": [{
            "name": "NVD CVE 真实记录",
            "dimension": "内容主题",
            "file_count": len(paths),
            "topics": ["CVE", "漏洞", "产品影响"],
            "representative_documents": paths[:4],
        }],
    }
    return scan, summaries, analysis, local_report


def build_qa_documents(census):
    count = int(census["record_count"])
    total_bytes = int(census["serialized_record_bytes"])
    mean_bytes = total_bytes / float(max(1, count))
    return [{
        "path": Path(census["dataset_file"]).name,
        "payload": {
            "data_profile": {
                "status": "completed",
                "row_count": count,
                "coverage": {"complete": True},
                "columns": {
                    "记录字节数": {
                        "inferred_type": "number",
                        "count": count,
                        "sum": total_bytes,
                        "mean": mean_bytes,
                        "min": census["record_bytes_distribution"]["p50"],
                        "max": census["record_bytes_distribution"]["max"],
                    },
                    "记录Token数": {
                        "inferred_type": "number",
                        "count": count,
                        "sum": census["proxy_tokens_total"],
                        "mean": census["record_proxy_tokens_distribution"]["mean"],
                        "min": census["record_proxy_tokens_distribution"]["p50"],
                        "max": census["record_proxy_tokens_distribution"]["max"],
                    },
                },
            },
        },
    }]


def load_jsonl(path):
    rows = []
    path = Path(path)
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def summarize_feature_stages(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["stage_name"]].append(row)
    output = []
    for stage_name in sorted(grouped):
        selected = grouped[stage_name]
        result = {
            "stage_name": stage_name,
            "case_count": len(selected),
            "completed_count": sum(item.get("status") == "completed" for item in selected),
            "failed_count": sum(item.get("status") == "failed" for item in selected),
            "model_call_count": sum(int(item.get("model_call_count") or 0) for item in selected),
            "input_tokens_total": sum(int(item.get("input_tokens") or 0) for item in selected),
            "output_tokens_total": sum(int(item.get("output_tokens") or 0) for item in selected),
            "context_tokens_total": sum(int(item.get("context_tokens") or 0) for item in selected),
            "source_bytes_total": sum(int(item.get("source_bytes") or 0) for item in selected),
        }
        for metric in ("wall_seconds", "driver_cpu_seconds", "device_util_avg", "device_util_peak", "process_cpu_avg_percent", "process_rss_peak_bytes"):
            values = [item.get(metric) for item in selected if item.get(metric) is not None]
            result[metric + "_p50"] = percentile(values, 0.50)
            result[metric + "_p95"] = percentile(values, 0.95)
            result[metric + "_max"] = max(values) if values else None
        output.append(result)
    return output


def projection(census, all_requests, context_window_tokens):
    chunks = [
        row for row in all_requests
        if row.get("status") == "ok" and row.get("stage_name") == "document_chunk_analysis"
    ]
    prefill = [float(row["prefill_tokens_s"]) for row in chunks if row.get("prefill_tokens_s")]
    decode = [float(row["decode_tokens_s"]) for row in chunks if row.get("decode_tokens_s")]
    input_tokens = [int(row["input_tokens"]) for row in chunks if row.get("input_tokens")]
    output_ratio = [
        float(row.get("output_tokens") or 0) / float(row.get("input_tokens") or 1)
        for row in chunks if row.get("input_tokens")
    ]
    dataset_tokens = int(census["proxy_tokens_total"])
    typical_chunk = int(percentile(input_tokens, 0.50) or 12000)
    calls = int(math.ceil(dataset_tokens / float(max(1, typical_chunk))))
    prefill_rate = float(percentile(prefill, 0.50) or 1.0)
    decode_rate = float(percentile(decode, 0.50) or 1.0)
    estimated_output = int(dataset_tokens * float(percentile(output_ratio, 0.50) or 0.02))
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "projection_only_not_executed_full_text",
        "dataset_proxy_tokens": dataset_tokens,
        "typical_measured_chunk_input_tokens": typical_chunk,
        "projected_chunk_calls": calls,
        "measured_prefill_tokens_s_p50": prefill_rate,
        "measured_decode_tokens_s_p50": decode_rate,
        "projected_output_tokens": estimated_output,
        "projected_prefill_seconds": round(dataset_tokens / prefill_rate, 3),
        "projected_decode_seconds": round(estimated_output / decode_rate, 3),
        "projected_model_seconds_excluding_reduce_and_queue": round(
            dataset_tokens / prefill_rate + estimated_output / decode_rate, 3
        ),
        "context_window_tokens": int(context_window_tokens),
        "warning": "这是依据真实调用吞吐得到的容量推算，不是把全部数据正文送入共享 GPU 的实测耗时。",
    }


def build_migration_profile(requests, context_window_tokens):
    stage_order = [
        "semantic_cluster_naming",
        "subtopic_naming",
        "full_document_analysis",
        "document_chunk_analysis",
        "document_chunk_reduce",
        "folder_node_summary",
        "report_research_direction",
        "model_connection_test",
    ]
    successful = [row for row in requests if row.get("status") == "ok"]
    business = [row for row in successful if row.get("stage_name") != "model_connection_test"]
    total_seconds = sum(float(row.get("client_e2e_ms") or 0) for row in business) / 1000.0
    total_input = sum(int(row.get("input_tokens") or 0) for row in business)
    total_output = sum(int(row.get("output_tokens") or 0) for row in business)
    grouped = defaultdict(list)
    for row in successful:
        grouped[row.get("stage_name") or "unknown"].append(row)
    rows = []
    ordered_names = stage_order + sorted(set(grouped) - set(stage_order))
    for stage_name in ordered_names:
        selected = grouped.get(stage_name) or []
        if not selected:
            continue
        is_control = stage_name == "model_connection_test"
        elapsed_seconds = sum(float(row.get("client_e2e_ms") or 0) for row in selected) / 1000.0
        input_tokens = sum(int(row.get("input_tokens") or 0) for row in selected)
        output_tokens = sum(int(row.get("output_tokens") or 0) for row in selected)
        contexts = [int(row.get("context_tokens") or 0) for row in selected]
        gpu_avg = [float(row["device_util_avg"]) for row in selected if row.get("device_util_avg") is not None]
        cpu_avg = [float(row["process_cpu_avg_percent"]) for row in selected if row.get("process_cpu_avg_percent") is not None]
        rows.append({
            "stage_name": stage_name,
            "stage_label": STAGE_LABELS.get(stage_name, stage_name),
            "workload_class": WORKLOAD_CLASSES.get(stage_name, "Other"),
            "is_control": is_control,
            "model_call_count": len(selected),
            "model_seconds_total": round(elapsed_seconds, 6),
            "model_time_share_pct": round(100.0 * elapsed_seconds / total_seconds, 6) if total_seconds and not is_control else 0.0,
            "input_tokens_total": input_tokens,
            "output_tokens_total": output_tokens,
            "context_tokens_total": input_tokens + output_tokens,
            "input_token_share_pct": round(100.0 * input_tokens / total_input, 6) if total_input and not is_control else 0.0,
            "output_token_share_pct": round(100.0 * output_tokens / total_output, 6) if total_output and not is_control else 0.0,
            "input_output_ratio": round(input_tokens / float(output_tokens), 6) if output_tokens else None,
            "context_tokens_p50": percentile(contexts, 0.50),
            "context_tokens_p95": percentile(contexts, 0.95),
            "context_occupancy_pct_p95": round(
                100.0 * float(percentile(contexts, 0.95) or 0) / float(context_window_tokens), 6
            ),
            "ttft_ms_p95": percentile([row.get("ttft_ms") for row in selected], 0.95),
            "tpot_ms_p95": percentile([row.get("tpot_ms") for row in selected], 0.95),
            "client_e2e_ms_p95": percentile([row.get("client_e2e_ms") for row in selected], 0.95),
            "prefill_tokens_s_p50": percentile([row.get("prefill_tokens_s") for row in selected], 0.50),
            "decode_tokens_s_p50": percentile([row.get("decode_tokens_s") for row in selected], 0.50),
            "gpu_util_avg_pct": round(statistics.mean(gpu_avg), 6) if gpu_avg else None,
            "gpu_util_peak_pct": max((float(row.get("device_util_peak") or 0) for row in selected), default=None),
            "gpu_memory_peak_gib": round(max((int(row.get("device_memory_peak_bytes") or 0) for row in selected), default=0) / float(1024 ** 3), 6),
            "process_cpu_avg_pct": round(statistics.mean(cpu_avg), 6) if cpu_avg else None,
            "process_cpu_peak_pct": max((float(row.get("process_cpu_peak_percent") or 0) for row in selected), default=None),
            "process_rss_peak_gib": round(max((int(row.get("process_rss_peak_bytes") or 0) for row in selected), default=0) / float(1024 ** 3), 6),
            "power_peak_w": max((float(row.get("power_peak_w") or 0) for row in selected), default=None),
            "pcie_tx_peak_mb_s": max((float(row.get("pcie_tx_peak_mb_s") or 0) for row in selected), default=None),
            "pcie_rx_peak_mb_s": max((float(row.get("pcie_rx_peak_mb_s") or 0) for row in selected), default=None),
        })
    return rows


def render_report(path, result, census, model_summary, feature_summary, projection_data, migration_profile):
    del model_summary
    dataset_mib = float(census["dataset_file_mib"])
    business_rows = [row for row in migration_profile if not row["is_control"]]
    top_time = max(business_rows, key=lambda row: row["model_time_share_pct"])
    top_input = max(business_rows, key=lambda row: row["input_tokens_total"])
    top_output = max(business_rows, key=lambda row: row["output_tokens_total"])
    top_context = max(business_rows, key=lambda row: row["context_tokens_p95"] or 0)
    lines = [
        "# SJFX 约 {:.0f}MB 真实数据全功能画像测试".format(dataset_mib),
        "",
        "## 一、任务设置",
        "",
        "- 数据文件：`{}`".format(census["dataset_file"]),
        "- 实际大小：`{:,}` bytes（`{:.2f} MiB`）".format(census["dataset_file_bytes"], census["dataset_file_mib"]),
        "- 完整 CVE 记录数：`{:,}`".format(census["record_count"]),
        "- 原始字符数量：`{:,}`".format(census["serialized_record_chars"]),
        "- 总 Token 规模（Qwen2.5 代理 tokenizer）：`{:,}`".format(census["proxy_tokens_total"]),
        "- Unit 数：`{:,}` 条完整 CVE 记录".format(census["record_count"]),
        "- SHA-256：`{}`".format(census["dataset_sha256"]),
        "- 全量 Token 统计：Qwen2.5 本地 tokenizer 逐记录计数；每次 Qwen3.5 模型调用使用 Ollama 原生计数。",
        "- 模型并发：1；解析并发：1；进程降低调度优先级；每次模型调用前检查生产任务和 GPU 利用率。",
        "",
        "## 二、全量数据特征",
        "",
        "| 指标 | P50 | P90 | P95 | P99 | 最大值 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, key in (("单条记录字节", "record_bytes_distribution"), ("单条记录字符", "record_chars_distribution"), ("单条记录 Token（代理 tokenizer）", "record_proxy_tokens_distribution")):
        item = census[key]
        lines.append("| {} | {:.0f} | {:.0f} | {:.0f} | {:.0f} | {:.0f} |".format(
            label, item["p50"] or 0, item["p90"] or 0, item["p95"] or 0, item["p99"] or 0, item["max"] or 0
        ))
    lines.extend([
        "",
        "全量代理 Token 合计：`{:,}`；Token 密度：`{:,.0f} Token/MiB`。".format(
            int(census["proxy_tokens_total"]), census["proxy_tokens_per_mib"]
        ),
        "",
        "## 三、工作流各模型阶段任务画像",
        "",
        "时间占比按各业务阶段模型请求的客户端端到端耗时合计计算；模型连接控制项不计入业务占比。",
        "",
        "| 功能阶段 | 负载类型 | 时间占比 | 调用数 | 输入 Token | 输出 Token |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for item in business_rows:
        lines.append("| {} | {} | {:.2f}% | {} | {:,} | {:,} |".format(
            item["stage_label"], item["workload_class"], item["model_time_share_pct"],
            item["model_call_count"], item["input_tokens_total"], item["output_tokens_total"],
        ))
    lines.extend([
        "",
        "最耗时阶段为 **{}**（{:.2f}%）；输入 Token 最大阶段为 **{}**；输出 Token 最大阶段为 **{}**。".format(
            top_time["stage_label"], top_time["model_time_share_pct"], top_input["stage_label"], top_output["stage_label"]
        ),
        "",
        "### Token 占比",
        "",
        "| 功能阶段 | 输入 Token 占比 | 输出 Token 占比 | 输入/输出 Token 比 |",
        "|---|---:|---:|---:|",
    ])
    for item in business_rows:
        lines.append("| {} | {:.2f}% | {:.2f}% | {:.3f} |".format(
            item["stage_label"], item["input_token_share_pct"], item["output_token_share_pct"],
            item["input_output_ratio"] or 0,
        ))
    lines.extend([
        "",
        "### 上下文长度特征",
        "",
        "`Context P95 = P95(input_tokens + output_tokens)`；`上下文占用 P95 = Context P95 / {:,}`。".format(result["context_window_tokens"]),
        "",
        "| 功能阶段 | Context P50 | Context P95 | 上下文占用 P95 |",
        "|---|---:|---:|---:|",
    ])
    for item in business_rows:
        lines.append("| {} | {:.0f} | {:.0f} | {:.2f}% |".format(
            item["stage_label"], item["context_tokens_p50"] or 0,
            item["context_tokens_p95"] or 0, item["context_occupancy_pct_p95"],
        ))
    lines.extend([
        "",
        "上下文压力最高阶段为 **{}**：Context P95 为 `{:,.0f}`，占当前 65,536 Token 窗口的 `{:.2f}%`。".format(
            top_context["stage_label"], top_context["context_tokens_p95"] or 0,
            top_context["context_occupancy_pct_p95"],
        ),
        "",
        "## 四、迁移相关推理性能",
        "",
        "| 功能阶段 | Prefill P50(tok/s) | TTFT P95(ms) | Decode P50(tok/s) | TPOT P95(ms/token) | E2E P95(ms) |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for item in business_rows:
        lines.append("| {} | {:.1f} | {:.1f} | {:.2f} | {:.2f} | {:.1f} |".format(
            item["stage_label"], item["prefill_tokens_s_p50"] or 0,
            item["ttft_ms_p95"] or 0, item["decode_tokens_s_p50"] or 0,
            item["tpot_ms_p95"] or 0, item["client_e2e_ms_p95"] or 0,
        ))
    lines.extend([
        "",
        "## 五、模型请求期间资源画像",
        "",
        "| 功能阶段 | GPU平均利用率 | GPU峰值 | 显存峰值(GiB) | 进程CPU平均 | CPU峰值 | 进程RSS峰值(GiB) | 功耗峰值(W) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for item in business_rows:
        lines.append("| {} | {:.1f}% | {:.1f}% | {:.2f} | {:.1f}% | {:.1f}% | {:.2f} | {:.1f} |".format(
            item["stage_label"], item["gpu_util_avg_pct"] or 0, item["gpu_util_peak_pct"] or 0,
            item["gpu_memory_peak_gib"] or 0, item["process_cpu_avg_pct"] or 0,
            item["process_cpu_peak_pct"] or 0, item["process_rss_peak_gib"] or 0,
            item["power_peak_w"] or 0,
        ))
    lines.extend([
        "",
        "## 六、功能级墙钟耗时",
        "",
        "| 功能 | 用例数 | 成功 | 模型调用数 | 测试范围字节累计（重复计入） | 墙钟耗时 P95(s) |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for item in feature_summary:
        lines.append("| {} | {} | {} | {} | {:,} | {:.3f} |".format(
            item["stage_name"], item["case_count"], item["completed_count"], item["model_call_count"],
            item["source_bytes_total"], item.get("wall_seconds_p95") or 0,
        ))
    lines.extend([
        "",
        "## 七、全数据正文深度推理容量推算",
        "",
        "- 按实测分块输入中位数，预计需要约 `{:,.0f}` 次 Map 调用。".format(projection_data["projected_chunk_calls"]),
        "- 预计 Prefill：`{:,.1f}s`；Decode：`{:,.1f}s`；不含 Reduce、排队和工具调用合计约 `{:,.1f}s`。".format(
            projection_data["projected_prefill_seconds"], projection_data["projected_decode_seconds"],
            projection_data["projected_model_seconds_excluding_reduce_and_queue"],
        ),
        "- 以上是容量推算，不冒充全量正文模型推理实测。实际模型用例使用同一数据集中的非重叠真实 CVE 原文。",
        "- Map 输出多次达到输出上限，因此按当前输出比推算的 Decode 时间仅用于容量规划，仍需在专用测试窗口校准。",
        "",
        "## 八、迁移评估解释",
        "",
        "- 每个重点阶段至少 5 个真实用例；`document_chunk_analysis` 由长文档 Map-Reduce 产生更多调用。",
        "- 5 个样本可以给出阶段基线 P95，但不能作为正式 SLA 的长尾结论；SLA P95/P99 应扩展到至少数百/上千请求并连续运行 1 小时以上。",
        "- `structured_qa` 是本地确定性计算，因此输入/输出 Token 均为 0，但仍记录墙钟耗时与资源占用。",
        "- Evidence 类阶段主要决定长上下文 Prefill、KV Cache 与显存带宽需求；Synthesis 类阶段主要决定长输出 Decode 吞吐。",
        "- 国产芯片对比时至少核对：模型格式与算子支持、可用显存、最大上下文、Prefill/Decode 吞吐、TTFT/TPOT、并发下 P95/P99、Host-Device 带宽及连续运行稳定性。",
        "- 完整流水线结果、逐次请求、资源采样和数据集分布均保存在同一运行目录，可追溯到 SHA-256。",
    ])
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_dir", type=Path)
    parser.add_argument("--output-base", type=Path, required=True)
    parser.add_argument("--work-base", type=Path, required=True)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--context-window-tokens", type=int, default=65536)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--request-timeout", type=int, default=600)
    parser.add_argument("--max-pre-call-gpu-util", type=int, default=10)
    parser.add_argument("--idle-seconds", type=int, default=15)
    parser.add_argument("--max-wait-seconds", type=int, default=3600)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    dataset_dir = args.dataset_dir.expanduser().resolve()
    dataset_files = sorted(dataset_dir.glob("*.json"))
    if not dataset_files:
        raise SystemExit("no JSON data file found under {}".format(dataset_dir))
    dataset_file = max(dataset_files, key=lambda path: path.stat().st_size)
    dataset_bytes = dataset_file.stat().st_size
    lowered_priority = try_lower_priority()
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = "2"
    os.environ["MKL_NUM_THREADS"] = "2"

    pipeline_args = argparse.Namespace(
        root=dataset_dir,
        output_dir=args.output_base,
        work_dir=args.work_base,
        production_db=args.production_db,
        mode="fast",
        context_window_tokens=args.context_window_tokens,
        expected_size_bytes=dataset_bytes,
        size_tolerance_percent=1.0,
        sample_interval=args.sample_interval,
        max_preflight_gpu_util=args.max_pre_call_gpu_util,
        idle_seconds=args.idle_seconds,
        max_wait_seconds=args.max_wait_seconds,
        request_timeout=args.request_timeout,
        skip_input_hash=False,
        preflight_only=False,
    )
    pipeline_result, _coarse, _progress, _requests, _monitor = run_full_pipeline(pipeline_args)
    output_dir = Path(pipeline_result["output_dir"])
    if pipeline_result.get("status") != "completed":
        print(json.dumps({
            "status": "pipeline_failed",
            "output_dir": str(output_dir),
            "fatal_error": pipeline_result.get("fatal_error"),
        }, ensure_ascii=False, indent=2))
        return 2

    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": pipeline_result["run_id"],
        "started_at": utc_now(),
        "output_dir": str(output_dir),
        "dataset_dir": str(dataset_dir),
        "dataset_file": str(dataset_file),
        "dataset_bytes": dataset_bytes,
        "context_window_tokens": int(args.context_window_tokens),
        "repetitions": int(args.repetitions),
        "lowered_process_priority": lowered_priority,
        "full_pipeline_status": pipeline_result.get("status"),
    }
    monitor = DeviceMonitor(interval=args.sample_interval)
    request_recorder = FeatureRequestRecorder(output_dir)
    stage_recorder = None
    transport = None
    fatal_error = None
    monitor_started = False
    try:
        census, short_samples, long_samples, reservoir = collect_dataset(
            dataset_file, args.tokenizer, output_dir, args.repetitions
        )
        result["dataset_census"] = census
        stage_recorder = FeatureStageRecorder(monitor, request_recorder, census["dataset_sha256"])

        from config import Config
        from services.document_analysis import analyze_document
        from services.folder_analysis import analyze_folder
        from services.package_analysis import _name_semantic_clusters, _name_subtopic_nodes
        from services.reporting import build_report_analysis_prompt, merge_model_report
        from services.structured_qa import answer_question

        initial_gate = wait_for_idle(
            monitor,
            max_util=args.max_pre_call_gpu_util,
            idle_seconds=args.idle_seconds,
            max_wait_seconds=args.max_wait_seconds,
            db_path=str(args.production_db),
        )
        result["feature_idle_gate"] = initial_gate
        monitor.start()
        monitor_started = True
        transport = CooperativeProfilingTransport(
            base_url=Config.OLLAMA_BASE_URL,
            model=Config.OLLAMA_MODEL,
            timeout=args.request_timeout,
            context_window_tokens=args.context_window_tokens,
            monitor=monitor,
            recorder=request_recorder,
            production_db=args.production_db,
            max_pre_call_gpu_util=args.max_pre_call_gpu_util,
            idle_seconds=max(5, min(15, args.idle_seconds)),
            max_wait_seconds=args.max_wait_seconds,
        )
        health = transport.health_check(timeout=10)
        result["model_health"] = health
        if not health.get("reachable") or not health.get("model_available"):
            raise RuntimeError("configured Ollama model is unavailable: {}".format(health))
        llm = ProfilingAgentRuntime(transport)

        selected_records = reservoir[:max(40, args.repetitions * 8)]
        documents = build_documents(selected_records)
        semantic_clusters = build_semantic_clusters(documents)
        subtopic_tree = build_subtopic_tree(documents)
        folder_context = build_folder_context(documents, dataset_bytes)
        report_fixture = build_report_fixture(documents, dataset_bytes)

        for index in range(args.repetitions):
            case_id = "connection-{:02d}".format(index + 1)
            def run_connection(index=index):
                with transport.stage("模型连接测试"):
                    return llm.chat(
                        "你是本地模型连通性测试助手。",
                        "只返回 JSON：{\"status\":\"ok\",\"case\":%d}" % (index + 1),
                        max_tokens=80,
                        timeout=120,
                    )
            stage_recorder.run("model_connection_test", case_id, run_connection, notes="控制项")

        for index in range(args.repetitions):
            stage_recorder.run(
                "semantic_cluster_naming",
                "semantic-{:02d}".format(index + 1),
                lambda: _name_semantic_clusters(copy.deepcopy(semantic_clusters), documents, llm),
                source_bytes=sum(item["source"]["size"] for item in documents.values()),
                source_chars=sum(len(item["text"]) for item in documents.values()),
            )

        for index in range(args.repetitions):
            stage_recorder.run(
                "subtopic_naming",
                "subtopic-{:02d}".format(index + 1),
                lambda: _name_subtopic_nodes(copy.deepcopy(subtopic_tree), documents, llm),
                source_bytes=sum(item["source"]["size"] for item in documents.values()),
                source_chars=sum(len(item["text"]) for item in documents.values()),
            )

        scan, summaries, analysis, local_report = report_fixture
        for index in range(args.repetitions):
            def run_report():
                prompt, catalog = build_report_analysis_prompt(scan, summaries, analysis, local_report)
                response = llm.chat_json(
                    "你是严谨的数据包分析与研究规划助手。你必须从证据中归纳，不得使用固定领域模板。事实与推论严格分开；研究建议必须可验证、可回溯。",
                    prompt,
                    max_tokens=1800,
                    strict=True,
                    retries=0,
                    timeout=args.request_timeout,
                    required_fields=("recommended_research_direction",),
                    output_context="报告研究方向分析",
                )
                return merge_model_report(copy.deepcopy(local_report), response["json"], catalog)
            stage_recorder.run(
                "report_research_direction",
                "report-{:02d}".format(index + 1),
                run_report,
                source_bytes=sum(item["source"]["size"] for item in documents.values()),
                source_chars=sum(len(item["text"]) for item in documents.values()),
            )

        for index, text in enumerate(short_samples, 1):
            unified = {
                "text": text,
                "coverage": {"complete": True},
                "warnings": [],
                "structure": {"title": "NVD CVE 短文档样本 {}".format(index)},
                "parser": {"name": "NVD JSON real-record assembler"},
                "evidence": [],
            }
            stage_recorder.run(
                "full_document_analysis",
                "short-document-{:02d}".format(index),
                lambda unified=unified, index=index: analyze_document(
                    llm, None, "NVD-CVE-short-{:02d}.json".format(index),
                    max_chars=50000, max_chunks=1, unified_document=unified,
                ),
                source_bytes=len(text.encode("utf-8")),
                source_chars=len(text),
                notes="完整正文单次推理",
            )

        for index, text in enumerate(long_samples, 1):
            unified = {
                "text": text,
                "coverage": {"complete": True},
                "warnings": [],
                "structure": {"title": "NVD CVE 长文档样本 {}".format(index)},
                "parser": {"name": "NVD JSON real-record assembler"},
                "evidence": [],
            }
            stage_recorder.run(
                "long_document_map_reduce",
                "long-document-{:02d}".format(index),
                lambda unified=unified, index=index: analyze_document(
                    llm, None, "NVD-CVE-long-{:02d}.json".format(index),
                    max_chars=180000, max_chunks=4, unified_document=unified,
                ),
                source_bytes=len(text.encode("utf-8")),
                source_chars=len(text),
                notes="配置最多 4 块；换行边界可能产生额外尾块，随后执行 1 次 Reduce",
            )

        for index in range(args.repetitions):
            stage_recorder.run(
                "folder_node_summary",
                "folder-{:02d}".format(index + 1),
                lambda: analyze_folder(llm, copy.deepcopy(folder_context), "."),
                source_bytes=sum(item["source"]["size"] for item in documents.values()),
                source_chars=sum(len(item["text"]) for item in documents.values()),
            )

        qa_documents = build_qa_documents(census)
        qa_questions = [
            "这个数据集有多少条记录？",
            "记录字节数合计是多少？",
            "记录字节数平均是多少？",
            "记录Token数最大是多少？",
            "记录Token数合计是多少？",
        ]
        for index in range(max(20, args.repetitions * 4)):
            question = qa_questions[index % len(qa_questions)]
            stage_recorder.run(
                "structured_qa",
                "qa-{:03d}".format(index + 1),
                lambda question=question: answer_question(question, qa_documents),
                source_bytes=dataset_bytes,
                source_chars=census["serialized_record_chars"],
                model_expected=False,
                notes="本地确定性问答，Token=0",
            )

        failed_cases = [row for row in stage_recorder.rows if row.get("status") != "completed"]
        result["failed_feature_cases"] = failed_cases
        result["status"] = "completed" if not failed_cases else "failed"
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
        pipeline_requests = load_jsonl(output_dir / "model_requests.jsonl")
        all_requests = pipeline_requests + request_recorder.rows
        all_model_summary = model_stage_summary(all_requests)
        feature_rows = stage_recorder.rows if stage_recorder else []
        feature_summary = summarize_feature_stages(feature_rows)
        census = result.get("dataset_census") or {}
        projection_data = projection(census, all_requests, args.context_window_tokens) if census else {}
        migration_profile = build_migration_profile(all_requests, args.context_window_tokens)
        result.update({
            "completed_at": utc_now(),
            "pipeline_model_request_count": len(pipeline_requests),
            "feature_model_request_count": len(request_recorder.rows),
            "all_model_request_count": len(all_requests),
            "all_model_requests": all_requests,
            "all_model_stage_summary": all_model_summary,
            "feature_stage_runs": feature_rows,
            "feature_stage_summary": feature_summary,
            "cooperative_waits": transport.cooperative_waits if transport else [],
            "resource_sample_count": len(monitor.samples),
            "full_dataset_projection": projection_data,
            "migration_stage_profile": migration_profile,
        })
        write_csv(output_dir / "feature_model_requests.csv", request_recorder.rows)
        write_csv(output_dir / "all_model_requests.csv", all_requests)
        with (output_dir / "all_model_requests.jsonl").open("w", encoding="utf-8") as handle:
            for row in all_requests:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        write_csv(output_dir / "all_model_stage_summary.csv", all_model_summary)
        write_csv(output_dir / "feature_stage_runs.csv", feature_rows)
        write_csv(output_dir / "feature_stage_summary.csv", feature_summary)
        write_csv(output_dir / "feature_resources.csv", monitor.samples)
        write_csv(output_dir / "migration_stage_profile.csv", migration_profile)
        atomic_write_json(output_dir / "migration_stage_profile.json", migration_profile)
        if projection_data:
            atomic_write_json(output_dir / "full_dataset_projection.json", projection_data)
        atomic_write_json(output_dir / "all_features_summary.json", result)
        if census and projection_data:
            report_name = "{:.0f}MB真实数据全功能测试报告.md".format(census["dataset_file_mib"])
            render_report(
                output_dir / report_name,
                result,
                census,
                all_model_summary,
                feature_summary,
                projection_data,
                migration_profile,
            )
        try:
            monitor.close()
        except Exception:
            pass

    print(json.dumps({
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "output_dir": result.get("output_dir"),
        "all_model_request_count": result.get("all_model_request_count"),
        "fatal_error": fatal_error,
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
