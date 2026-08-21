#!/usr/bin/env python3
"""Run a reproducible local package benchmark and emit one JSON record."""

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.package_analysis import analyze_package
from services.scanner import scan_directory
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser


def main():
    parser = argparse.ArgumentParser(description="SJFX 真实数据包 1/3/5GB 性能验收")
    parser.add_argument("root", type=Path)
    parser.add_argument("--label", default="custom")
    parser.add_argument("--mode", choices=("fast", "accurate"), default="fast")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "outputs" / "benchmarks")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit("benchmark root is not a directory: {}".format(root))
    args.output_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    scan_started = time.perf_counter()
    scan = scan_directory(root)
    scan_seconds = time.perf_counter() - scan_started
    scan["parse_mode"] = args.mode

    with tempfile.TemporaryDirectory(prefix="sjfx-benchmark-") as work:
        storage = Storage(Path(work) / "benchmark.db", Path(work) / "payloads")
        scan_id = storage.save_scan(scan)
        analysis_started = time.perf_counter()
        analysis = analyze_package(
            scan_id,
            scan,
            storage,
            UnifiedDocumentParser(),
            large_options={"threshold_bytes": 1024 * 1024 * 1024},
        )
        analysis_seconds = time.perf_counter() - analysis_started
        database_bytes = sum(
            path.stat().st_size for path in Path(work).rglob("*") if path.is_file()
        )

    result = {
        "schema_version": "sjfx-performance/1.0",
        "label": args.label,
        "root": str(root),
        "mode": args.mode,
        "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "input": {
            "files": scan.get("file_count"),
            "bytes": scan.get("total_size"),
            "types": scan.get("type_counts"),
        },
        "timing_seconds": {
            "inventory": round(scan_seconds, 3),
            "analysis": round(analysis_seconds, 3),
            "end_to_end": round(time.perf_counter() - started, 3),
        },
        "statistics": analysis.get("statistics"),
        "coverage": analysis.get("coverage"),
        "value_judgment": analysis.get("value_judgment"),
        "cache_and_database_bytes": database_bytes,
    }
    filename = "{}-{}.json".format(args.label, int(time.time()))
    target = args.output_dir / filename
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "result": str(target), "coverage": result["coverage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
