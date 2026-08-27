#!/usr/bin/env python3
"""Extract historical SJFX workload statistics from its SQLite state database."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


HISTOGRAM_BINS = (512, 2048, 8192, 32768, 65536)


def percentile(values, quantile):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    rank = max(1, math.ceil(float(quantile) * len(values)))
    return values[min(rank - 1, len(values) - 1)]


def distribution(values):
    values = [int(value) for value in values if value is not None]
    return {
        "count": len(values),
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p99": percentile(values, 0.99),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "histogram": histogram(values),
    }


def histogram(values):
    labels = ["0-512", "513-2048", "2049-8192", "8193-32768", "32769-65536", ">65536"]
    counts = Counter()
    for value in values:
        if value <= HISTOGRAM_BINS[0]:
            counts[labels[0]] += 1
        elif value <= HISTOGRAM_BINS[1]:
            counts[labels[1]] += 1
        elif value <= HISTOGRAM_BINS[2]:
            counts[labels[2]] += 1
        elif value <= HISTOGRAM_BINS[3]:
            counts[labels[3]] += 1
        elif value <= HISTOGRAM_BINS[4]:
            counts[labels[4]] += 1
        else:
            counts[labels[5]] += 1
    total = len(values)
    return [
        {"range": label, "count": counts[label], "share_pct": round(100 * counts[label] / total, 2) if total else 0}
        for label in labels
    ]


def walk_usage(value, path="$", found=None):
    found = found if found is not None else []
    if isinstance(value, dict):
        usage = value.get("usage")
        if isinstance(usage, dict) and (
            usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None
        ):
            found.append({
                "json_path": path + ".usage",
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            })
        for key, child in value.items():
            walk_usage(child, path + "." + str(key), found)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            walk_usage(child, "{}[{}]".format(path, index), found)
    return found


def safe_json(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def nearest_rank(values, quantile):
    return percentile(values, quantile)


def summarize_jobs(connection):
    rows = connection.execute(
        "SELECT task_type, status, created_at, started_at, finished_at FROM analysis_jobs"
    ).fetchall()
    daily = Counter()
    per_minute = Counter()
    per_second = Counter()
    by_task = defaultdict(lambda: {"count": 0, "statuses": Counter(), "durations": []})
    valid_dates = []
    for task_type, status, created_at, started_at, finished_at in rows:
        task = by_task[str(task_type)]
        task["count"] += 1
        task["statuses"][str(status)] += 1
        if (
            status == "completed"
            and finished_at is not None
            and started_at is not None
            and float(finished_at) > float(started_at)
        ):
            task["durations"].append(float(finished_at) - float(started_at))
        if created_at:
            timestamp = str(created_at)
            daily[timestamp[:10]] += 1
            per_minute[timestamp[:16]] += 1
            per_second[timestamp[:19]] += 1
            valid_dates.append(timestamp)
    total_jobs = len(rows)
    total_wall = sum(sum(item["durations"]) for item in by_task.values())
    task_rows = []
    for task_type, item in sorted(by_task.items(), key=lambda pair: pair[1]["count"], reverse=True):
        durations = item["durations"]
        task_rows.append({
            "task_type": task_type,
            "count": item["count"],
            "count_share_pct": round(100 * item["count"] / total_jobs, 2) if total_jobs else 0,
            "statuses": dict(item["statuses"]),
            "timed_completed_count": len(durations),
            "wall_time_seconds": round(sum(durations), 3),
            "wall_time_share_pct": round(100 * sum(durations) / total_wall, 2) if total_wall else 0,
            "e2e_p50_seconds": nearest_rank(durations, 0.50),
            "e2e_p90_seconds": nearest_rank(durations, 0.90),
            "e2e_p99_seconds": nearest_rank(durations, 0.99),
            "e2e_max_seconds": max(durations) if durations else None,
        })
    date_count = len(daily)
    return {
        "job_count": total_jobs,
        "observed_from": min(valid_dates) if valid_dates else None,
        "observed_to": max(valid_dates) if valid_dates else None,
        "observed_calendar_days": date_count,
        "daily_average_jobs": round(total_jobs / date_count, 3) if date_count else None,
        "daily_peak_jobs": max(daily.values()) if daily else None,
        "peak_jobs_per_minute": max(per_minute.values()) if per_minute else None,
        "peak_jobs_per_second": max(per_second.values()) if per_second else None,
        "daily_counts": [{"date": key, "count": daily[key]} for key in sorted(daily)],
        "task_types": task_rows,
        "model_directed_tool_classes": {
            "pure_inference_or_fixed_pipeline_jobs": total_jobs,
            "single_model_directed_tool_jobs": 0,
            "react_three_or_more_model_directed_steps": 0,
            "note": "Current SJFX uses fixed orchestration; it does not persist model-directed tool-call or ReAct trajectories.",
        },
    }


def extract_usage(connection):
    records = []
    table_specs = [
        ("summaries", "SELECT rowid, summary_type, node_path, payload, created_at FROM summaries"),
        ("package_analyses", "SELECT rowid, 'package' AS summary_type, scan_id, payload, created_at FROM package_analyses"),
        ("scan_overviews", "SELECT rowid, 'scan_overview' AS summary_type, scan_id, payload, updated_at FROM scan_overviews"),
        ("analysis_overviews", "SELECT rowid, 'analysis_overview' AS summary_type, scan_id, payload, updated_at FROM analysis_overviews"),
    ]
    for table, query in table_specs:
        for rowid, record_type, record_key, raw, created_at in connection.execute(query):
            payload = safe_json(raw)
            if payload is None:
                continue
            for index, usage in enumerate(walk_usage(payload)):
                prompt = int(usage["prompt_tokens"])
                completion = int(usage["completion_tokens"])
                records.append({
                    "source_table": table,
                    "source_rowid": rowid,
                    "record_type": record_type,
                    "record_key": record_key,
                    "created_at": created_at,
                    "usage_index": index,
                    "json_path": usage["json_path"],
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "context_tokens": prompt + completion,
                })
    return records


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path")
    parser.add_argument("--output-dir", default="outputs/benchmarks/workload-history")
    args = parser.parse_args()
    db_path = Path(args.db_path).resolve()
    output_dir = Path(args.output_dir).resolve() / datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=False)
    connection = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True, timeout=5)
    try:
        jobs = summarize_jobs(connection)
        usage = extract_usage(connection)
    finally:
        connection.close()
    by_type = {}
    for record_type in sorted(set(row["record_type"] for row in usage)):
        subset = [row for row in usage if row["record_type"] == record_type]
        by_type[record_type] = {
            "count": len(subset),
            "input_tokens": distribution([row["prompt_tokens"] for row in subset]),
            "output_tokens": distribution([row["completion_tokens"] for row in subset]),
            "context_tokens": distribution([row["context_tokens"] for row in subset]),
        }
    summary = {
        "schema": "sjfx-workload-history/1.0",
        "database": str(db_path),
        "generated_at": datetime.now().isoformat(),
        "jobs": jobs,
        "model_usage": {
            "record_count": len(usage),
            "input_tokens": distribution([row["prompt_tokens"] for row in usage]),
            "output_tokens": distribution([row["completion_tokens"] for row in usage]),
            "context_tokens": distribution([row["context_tokens"] for row in usage]),
            "by_record_type": by_type,
        },
        "limitations": [
            "Historical jobs include development and repeated test runs, not a production demand forecast.",
            "Only persisted outputs containing usage fields contribute to token distributions.",
            "The database does not persist per-token timing, device utilization, or model-directed tool trajectories.",
        ],
    }
    write_csv(output_dir / "model_usage.csv", usage)
    write_csv(output_dir / "task_types.csv", jobs["task_types"])
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
