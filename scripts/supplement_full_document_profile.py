#!/usr/bin/env python3
"""Add two real short-document calls and rebuild the 100 MiB profile report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ijson


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
for import_root in (str(PROJECT_ROOT), str(SCRIPT_ROOT)):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

from profile_all_features_100mb import (  # noqa: E402
    CooperativeProfilingTransport,
    FeatureRequestRecorder,
    FeatureStageRecorder,
    ProfilingAgentRuntime,
    atomic_write_json,
    build_migration_profile,
    load_jsonl,
    model_stage_summary,
    projection,
    record_prefix,
    render_report,
    write_csv,
)
from benchmark_llm_migration import DeviceMonitor, wait_for_idle  # noqa: E402


def real_samples(dataset_file, count=2, target_chars=30000):
    samples = []
    parts = []
    chars = 0
    with Path(dataset_file).open("rb") as handle:
        for record in ijson.items(handle, record_prefix(dataset_file), use_float=True):
            text = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            parts.append(text)
            chars += len(text) + 1
            if chars >= target_chars:
                samples.append("\n".join(parts))
                if len(samples) >= count:
                    return samples
                parts = []
                chars = 0
    raise RuntimeError("dataset does not contain enough complete records")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("dataset_file", type=Path)
    parser.add_argument("production_db", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    summary_path = run_dir / "all_features_summary.json"
    result = json.loads(summary_path.read_text(encoding="utf-8"))
    census = result["dataset_census"]
    context_window_tokens = int(result.get("context_window_tokens") or 65536)

    monitor = DeviceMonitor(interval=0.25)
    recorder = FeatureRequestRecorder(run_dir)
    stage_recorder = FeatureStageRecorder(monitor, recorder, census["dataset_sha256"])
    from config import Config
    from services.document_analysis import analyze_document

    wait_for_idle(
        monitor,
        max_util=10,
        idle_seconds=15,
        max_wait_seconds=7200,
        db_path=str(args.production_db),
    )
    monitor.start()
    transport = CooperativeProfilingTransport(
        base_url=Config.OLLAMA_BASE_URL,
        model=Config.OLLAMA_MODEL,
        timeout=900,
        context_window_tokens=context_window_tokens,
        monitor=monitor,
        recorder=recorder,
        production_db=args.production_db,
        max_pre_call_gpu_util=10,
        idle_seconds=15,
        max_wait_seconds=7200,
    )
    llm = ProfilingAgentRuntime(transport)
    try:
        for index, text in enumerate(real_samples(args.dataset_file), 1):
            unified = {
                "text": text,
                "coverage": {"complete": True},
                "warnings": [],
                "structure": {"title": "NVD CVE 补充短文档样本 {}".format(index)},
                "parser": {"name": "NVD JSON real-record assembler"},
                "evidence": [],
            }
            stage_recorder.run(
                "full_document_analysis",
                "supplement-short-document-{:02d}".format(index),
                lambda unified=unified, index=index: analyze_document(
                    llm,
                    None,
                    "NVD-CVE-supplement-short-{:02d}.json".format(index),
                    max_chars=50000,
                    max_chunks=1,
                    unified_document=unified,
                ),
                source_bytes=len(text.encode("utf-8")),
                source_chars=len(text),
                notes="补足单次全文深度摘要到5次模型调用",
            )
    finally:
        monitor.stop()
        monitor.close()

    pipeline_requests = load_jsonl(run_dir / "model_requests.jsonl")
    feature_requests = load_jsonl(run_dir / "feature_model_requests.jsonl")
    all_requests = pipeline_requests + feature_requests
    model_summary = model_stage_summary(all_requests)
    migration_profile = build_migration_profile(all_requests, context_window_tokens)
    projection_data = projection(census, all_requests, context_window_tokens)
    result.update({
        "feature_model_request_count": len(feature_requests),
        "all_model_request_count": len(all_requests),
        "all_model_requests": all_requests,
        "all_model_stage_summary": model_summary,
        "migration_stage_profile": migration_profile,
        "full_dataset_projection": projection_data,
        "supplemental_full_document_cases": stage_recorder.rows,
    })
    write_csv(run_dir / "feature_model_requests.csv", feature_requests)
    write_csv(run_dir / "all_model_requests.csv", all_requests)
    with (run_dir / "all_model_requests.jsonl").open("w", encoding="utf-8") as handle:
        for row in all_requests:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    write_csv(run_dir / "all_model_stage_summary.csv", model_summary)
    write_csv(run_dir / "migration_stage_profile.csv", migration_profile)
    atomic_write_json(run_dir / "migration_stage_profile.json", migration_profile)
    atomic_write_json(run_dir / "full_dataset_projection.json", projection_data)
    atomic_write_json(summary_path, result)
    render_report(
        run_dir / "100MB真实数据全功能测试报告.md",
        result,
        census,
        model_summary,
        result["feature_stage_summary"],
        projection_data,
        migration_profile,
    )
    print(json.dumps({
        "status": "completed",
        "supplement_calls": len(recorder.rows),
        "all_model_request_count": len(all_requests),
        "run_dir": str(run_dir),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
