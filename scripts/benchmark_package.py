#!/usr/bin/env python3
"""Run an evidence-producing SJFX package benchmark.

The benchmark measures the bytes that really exist on disk, runs the same
scanner and analysis pipeline as the product, records process-tree resource
usage where the host exposes it, and emits a machine-readable pass/fail
contract.  It never changes ``scan["total_size"]`` or otherwise fabricates a
large input.  Official acceptance runs use ``--profile acceptance`` so the
retrieval, checkpoint re-entry and export hooks are all exercised.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
SCHEMA_VERSION = "sjfx-performance/2.0"
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".gz", ".bz2", ".7z", ".rar")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_label(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "benchmark")).strip("-.")
    return cleaned or "benchmark"


def _is_within(candidate, parent):
    try:
        Path(candidate).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _allocated_bytes(stat_result):
    blocks = getattr(stat_result, "st_blocks", None)
    if blocks is None:
        return None
    return max(0, int(blocks) * 512)


def _is_sparse(stat_result, allocated):
    size = int(getattr(stat_result, "st_size", 0) or 0)
    attributes = int(getattr(stat_result, "st_file_attributes", 0) or 0)
    if attributes & 0x200:  # FILE_ATTRIBUTE_SPARSE_FILE on Windows.
        return True
    return allocated is not None and size > 1024 * 1024 and allocated + 4096 < size


def measure_real_input(root, ignored_dirs=None, ignore_file=None):
    """Measure real directory entries without trusting application metadata."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("benchmark root is not a directory: {}".format(root))
    ignored_dirs = set(ignored_dirs or ())
    ignore_file = ignore_file or (lambda _name: False)
    stack = [root]
    result = {
        "root": str(root),
        "files": 0,
        "directories": 0,
        "logical_bytes": 0,
        "allocated_bytes": 0,
        "allocated_bytes_known": True,
        "largest_file_bytes": 0,
        "largest_file_path": None,
        "largest_regular_file_bytes": 0,
        "largest_regular_file_path": None,
        "largest_archive_file_bytes": 0,
        "largest_archive_file_path": None,
        "types": Counter(),
        "ignored_files": 0,
        "ignored_directories": 0,
        "symlink_count": 0,
        "skipped_symlinks": 0,
        "sparse_file_count": 0,
        "sparse_files": [],
        "errors": [],
    }
    while stack:
        folder = stack.pop()
        result["directories"] += 1
        try:
            with os.scandir(str(folder)) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            result["symlink_count"] += 1
                            result["skipped_symlinks"] += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in ignored_dirs:
                                result["ignored_directories"] += 1
                            else:
                                stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if ignore_file(entry.name):
                            result["ignored_files"] += 1
                            continue
                        stat_result = entry.stat(follow_symlinks=False)
                        size = int(stat_result.st_size)
                        allocated = _allocated_bytes(stat_result)
                        result["files"] += 1
                        result["logical_bytes"] += size
                        relative_path = str(Path(entry.path).relative_to(root)).replace("\\", "/")
                        if size > result["largest_file_bytes"]:
                            result["largest_file_bytes"] = size
                            result["largest_file_path"] = relative_path
                        is_archive = entry.name.casefold().endswith(ARCHIVE_SUFFIXES)
                        kind = "archive" if is_archive else "regular"
                        size_key = "largest_{}_file_bytes".format(kind)
                        path_key = "largest_{}_file_path".format(kind)
                        if size > result[size_key]:
                            result[size_key] = size
                            result[path_key] = relative_path
                        suffix = Path(entry.name).suffix.lower() or "[no-extension]"
                        result["types"][suffix] += 1
                        if allocated is None:
                            result["allocated_bytes_known"] = False
                        else:
                            result["allocated_bytes"] += allocated
                        if _is_sparse(stat_result, allocated):
                            result["sparse_file_count"] += 1
                            if len(result["sparse_files"]) < 100:
                                result["sparse_files"].append(
                                    relative_path
                                )
                    except (OSError, PermissionError) as exc:
                        result["errors"].append({"path": str(entry.path), "error": str(exc)[:500]})
        except (OSError, PermissionError) as exc:
            result["errors"].append({"path": str(folder), "error": str(exc)[:500]})
            continue
    result["types"] = dict(sorted(result["types"].items(), key=lambda item: (-item[1], item[0])))
    if not result["allocated_bytes_known"]:
        result["allocated_bytes"] = None
    result["errors"] = result["errors"][:100]
    return result


def compare_inventory(real_input, scan):
    actual_files = int(real_input.get("files") or 0)
    actual_directories = int(real_input.get("directories") or 0)
    actual_bytes = int(real_input.get("logical_bytes") or 0)
    scanned_files = int(scan.get("file_count") or 0)
    scanned_directories = int(scan.get("scanned_directory_count") or 0)
    actual_symlinks = int(real_input.get("symlink_count", real_input.get("skipped_symlinks", 0)) or 0)
    scanned_symlinks = int(scan.get("symlink_count", scan.get("skipped_symlink_count", 0)) or 0)
    scanned_bytes = int(scan.get("total_size") or 0)
    file_ratio = min(1.0, scanned_files / float(actual_files or 1))
    byte_ratio = min(1.0, scanned_bytes / float(actual_bytes or 1))
    complete = bool(
        scanned_files == actual_files
        and scanned_directories == actual_directories
        and scanned_symlinks == actual_symlinks
        and scanned_bytes == actual_bytes
        and not scan.get("truncated")
        and not scan.get("errors")
        and not real_input.get("errors")
        and int(scan.get("depth_limited_directory_count") or 0) == 0
    )
    return {
        "complete": complete,
        "actual_files": actual_files,
        "enumerated_files": scanned_files,
        "actual_directories": actual_directories,
        "enumerated_directories": scanned_directories,
        "actual_symlinks": actual_symlinks,
        "enumerated_symlinks": scanned_symlinks,
        "actual_bytes": actual_bytes,
        "enumerated_bytes": scanned_bytes,
        "file_ratio": round(file_ratio, 6),
        "byte_ratio": round(byte_ratio, 6),
        "scanner_truncated": bool(scan.get("truncated")),
        "scanner_error_count": len(scan.get("errors") or []),
        "independent_measurement_error_count": len(real_input.get("errors") or []),
        "depth_limited_directory_count": int(scan.get("depth_limited_directory_count") or 0),
        "directory_limited_count": int(scan.get("directory_limited_count") or 0),
        "node_limited_count": int(scan.get("node_limited_count") or 0),
        "entry_limited_directory_count": int(scan.get("entry_limited_directory_count") or 0),
    }


def _linux_process_tree_metrics(root_pid):
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    records = {}
    try:
        clock_ticks = float(os.sysconf("SC_CLK_TCK"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            raw = (item / "stat").read_text(encoding="utf-8", errors="replace")
            tail = raw[raw.rfind(")") + 2:].split()
            # tail starts at proc stat field 3 (state).
            records[int(item.name)] = {
                "ppid": int(tail[1]),
                "ticks": sum(int(tail[index]) for index in (11, 12, 13, 14)),
                "rss": max(0, int(tail[21])) * page_size,
            }
        except (OSError, ValueError, IndexError):
            continue
    descendants = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, record in records.items():
            if pid not in descendants and record["ppid"] in descendants:
                descendants.add(pid)
                changed = True
    selected = [records[pid] for pid in descendants if pid in records]
    if not selected:
        return None
    return {
        "cpu_seconds": sum(item["ticks"] for item in selected) / clock_ticks,
        "rss_bytes": sum(item["rss"] for item in selected),
        "process_count": len(selected),
        "scope": "linux-/proc-process-tree",
    }


def _windows_rss_bytes():
    if os.name != "nt":
        return None
    try:
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return int(counters.WorkingSetSize)
    except (AttributeError, OSError, ValueError):
        return None


def _fallback_rss_bytes():
    windows = _windows_rss_bytes()
    if windows is not None:
        return windows
    try:
        import resource
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024
    except (ImportError, AttributeError, OSError, ValueError):
        return None


def process_metrics():
    linux = _linux_process_tree_metrics(os.getpid())
    if linux is not None:
        return linux
    return {
        "cpu_seconds": time.process_time(),
        "rss_bytes": _fallback_rss_bytes(),
        "process_count": 1,
        "scope": "benchmark-driver-process",
    }


class ResourceSampler:
    def __init__(self, interval=0.5):
        self.interval = max(0.1, float(interval))
        self._stop = threading.Event()
        self._thread = None
        self._samples = []

    def start(self):
        self._samples = [(time.monotonic(), process_metrics())]
        self._thread = threading.Thread(target=self._run, name="sjfx-benchmark-resources", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval):
            self._samples.append((time.monotonic(), process_metrics()))

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 3))
        self._samples.append((time.monotonic(), process_metrics()))
        first_time, first = self._samples[0]
        last_time, last = self._samples[-1]
        wall = max(0.000001, last_time - first_time)
        cpu_delta = max(0.0, float(last.get("cpu_seconds") or 0) - float(first.get("cpu_seconds") or 0))
        cpu_samples = []
        for (left_time, left), (right_time, right) in zip(self._samples, self._samples[1:]):
            elapsed = max(0.000001, right_time - left_time)
            delta = max(0.0, float(right.get("cpu_seconds") or 0) - float(left.get("cpu_seconds") or 0))
            cpu_samples.append(100.0 * delta / elapsed)
        rss_values = [
            int(item.get("rss_bytes")) for _stamp, item in self._samples
            if item.get("rss_bytes") is not None
        ]
        return {
            "scope": last.get("scope"),
            "sample_count": len(self._samples),
            "sampling_interval_seconds": self.interval,
            "cpu_seconds": round(cpu_delta, 3),
            "cpu_percent_of_one_core_average": round(100.0 * cpu_delta / wall, 2),
            "cpu_percent_of_one_core_peak": round(max(cpu_samples or [0.0]), 2),
            "rss_peak_bytes": max(rss_values) if rss_values else None,
            "process_count_peak": max(int(item.get("process_count") or 1) for _stamp, item in self._samples),
            "limitations": (
                [] if last.get("scope") == "linux-/proc-process-tree" else
                ["当前平台仅能用标准库统计基准驱动进程；子解析进程资源需由主机监控补充。"]
            ),
        }


def total_memory_bytes():
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
    if os.name == "nt":
        try:
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except (AttributeError, OSError, ValueError):
            pass
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return pages * page_size
    except (AttributeError, OSError, ValueError):
        return None


def disk_snapshot(path):
    path = Path(path).expanduser().resolve()
    usage = shutil.disk_usage(str(path))
    return {
        "path": str(path),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
        "free_ratio": round(usage.free / float(usage.total or 1), 6),
    }


def disk_delta(before, after):
    return {
        "path": after["path"],
        "before": before,
        "after": after,
        "used_bytes_delta": int(after["used_bytes"]) - int(before["used_bytes"]),
        "free_bytes_delta": int(after["free_bytes"]) - int(before["free_bytes"]),
    }


def sha256_file(path, block_size=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def directory_bytes(root):
    total = 0
    for folder, _dirs, files in os.walk(str(root)):
        for name in files:
            try:
                total += (Path(folder) / name).stat().st_size
            except OSError:
                pass
    return total


def timed_stage(name, function, stages):
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    try:
        value = function()
    except Exception as exc:
        stages[name] = {
            "status": "failed",
            "seconds": round(time.perf_counter() - started_wall, 3),
            "driver_cpu_seconds": round(time.process_time() - started_cpu, 3),
            "error": "{}: {}".format(type(exc).__name__, str(exc))[:2000],
        }
        raise
    stages[name] = {
        "status": "completed",
        "seconds": round(time.perf_counter() - started_wall, 3),
        "driver_cpu_seconds": round(time.process_time() - started_cpu, 3),
    }
    return value


def choose_retrieval_query(analysis, explicit=None):
    if str(explicit or "").strip():
        return str(explicit).strip()
    for item in analysis.get("topic_clusters") or []:
        topic = str(item.get("topic") or item.get("name") or "").strip()
        if topic:
            return topic
    return "核心主题 关键发现 主要结论"


def _check(checks, name, passed, actual, expected, required=True):
    checks.append({
        "name": name,
        "passed": bool(passed),
        "required": bool(required),
        "actual": actual,
        "expected": expected,
    })


def evaluate_contract(result, args):
    checks = []
    real_input = result.get("input", {}).get("independent_measurement", {})
    target = int(args.expected_size_gib) * GIB
    tolerance = float(args.size_tolerance_percent) / 100.0
    low = int(target * (1.0 - tolerance))
    high = int(target * (1.0 + tolerance))
    logical = int(real_input.get("logical_bytes") or 0)
    _check(checks, "real_input_size", low <= logical <= high, logical, {"min": low, "max": high})
    _check(checks, "no_sparse_fixture", int(real_input.get("sparse_file_count") or 0) == 0,
           int(real_input.get("sparse_file_count") or 0), 0)
    _check(checks, "single_file_ceiling", int(real_input.get("largest_file_bytes") or 0) <= 10 * GIB,
           int(real_input.get("largest_file_bytes") or 0), "<= 10 GiB")

    limits = result.get("runtime_limits", {})
    limit_names = (
        "max_single_file_bytes", "max_archive_file_bytes", "max_archive_member_bytes",
        "max_archive_uncompressed_bytes", "max_export_bytes",
    )
    limit_actual = {name: limits.get(name) for name in limit_names}
    _check(checks, "unified_10gib_limits", all(limit_actual[name] == 10 * GIB for name in limit_names),
           limit_actual, "all equal 10737418240")

    inventory = result.get("inventory_contract", {})
    _check(checks, "inventory_100_percent", inventory.get("complete") is True, inventory, "complete=true")
    coverage = result.get("coverage") or {}
    parse_coverage = coverage.get("parse_coverage") or {}
    pending = int(parse_coverage.get("pending_files", coverage.get("pending_files", 0)) or 0)
    terminal = int(parse_coverage.get("parsed_files", coverage.get("parsed_files", 0)) or 0) + int(
        parse_coverage.get("failed_files", coverage.get("failed_files", 0)) or 0
    )
    inventoried = int(parse_coverage.get("inventory_files", coverage.get("inventory_files", 0)) or 0)
    _check(checks, "analysis_reaches_terminal_state", pending == 0 and terminal == inventoried,
           {"terminal_files": terminal, "inventory_files": inventoried, "pending_files": pending},
           "pending=0 and terminal=inventory")
    three_coverage_present = all(isinstance(coverage.get(name), dict) for name in (
        "inventory_coverage", "parse_coverage", "semantic_analysis_coverage"
    ))
    _check(checks, "three_coverage_contracts_present", three_coverage_present,
           sorted(coverage.keys()), "inventory/parse/semantic coverage objects")

    parsed = int(parse_coverage.get("parsed_files", coverage.get("parsed_files", 0)) or 0)
    failed = int(parse_coverage.get("failed_files", coverage.get("failed_files", 0)) or 0)
    minimum_parse_ratio = float(getattr(args, "min_parse_success_percent", 95.0)) / 100.0
    parse_success_ratio = parsed / float(inventoried or 1)
    _check(
        checks, "parse_success_ratio",
        inventoried > 0 and parse_success_ratio >= minimum_parse_ratio,
        {"parsed_files": parsed, "failed_files": failed, "ratio": round(parse_success_ratio, 6)},
        ">= {}% inventoried files parsed".format(round(minimum_parse_ratio * 100, 3)),
    )
    semantic = coverage.get("semantic_analysis_coverage") or {}
    full_text = int(semantic.get("full_text_files") or 0)
    projected_or_partial = int(semantic.get("projected_or_partial_files") or 0)
    semantic_analyzed = int(semantic.get("analyzed_files") or 0)
    semantic_accounted = full_text + projected_or_partial
    _check(
        checks, "semantic_coverage_accounting",
        parsed > 0 and semantic_analyzed == parsed and semantic_accounted == parsed,
        {
            "parsed_files": parsed,
            "analyzed_files": semantic_analyzed,
            "full_text_files": full_text,
            "projected_or_partial_files": projected_or_partial,
        },
        "every parsed file is explicitly full-text or projected/partial",
    )

    boundary_kind = getattr(args, "ten_gib_boundary_kind", None)
    if args.profile == "acceptance" and int(args.expected_size_gib) == 10:
        boundary = result.get("boundary_evidence") or {}
        _check(
            checks, "ten_gib_boundary_kind_declared",
            boundary_kind in {"regular", "archive"}, boundary_kind,
            "--ten-gib-boundary-kind regular|archive",
        )
        boundary_bytes = int(boundary.get("object_bytes") or 0)
        _check(
            checks, "ten_gib_boundary_object_real",
            boundary_kind in {"regular", "archive"} and boundary_bytes == 10 * GIB,
            {"kind": boundary.get("kind"), "path": boundary.get("path"), "bytes": boundary_bytes},
            {"kind": boundary_kind, "bytes": 10 * GIB},
        )
        _check(
            checks, "ten_gib_boundary_object_processed",
            boundary.get("analysis_status") in {"completed", "overview"},
            boundary.get("analysis_status"), "completed|overview",
        )

    required_hooks = args.profile == "acceptance"
    for name in ("retrieval", "recovery", "export"):
        hook = result.get("hooks", {}).get(name, {})
        _check(checks, "{}_hook".format(name), hook.get("status") == "passed",
               hook.get("status", "missing"), "passed", required=required_hooks)

    resources = result.get("resources", {})
    memory_total = result.get("environment", {}).get("memory_total_bytes")
    rss_peak = resources.get("rss_peak_bytes")
    memory_required = memory_total is not None and rss_peak is not None
    memory_passed = memory_required and rss_peak <= memory_total * float(args.max_rss_percent) / 100.0
    _check(checks, "rss_peak", memory_passed, {"rss_peak_bytes": rss_peak, "memory_total_bytes": memory_total},
           "<= {}% physical memory".format(args.max_rss_percent), required=memory_required)

    disk_after = result.get("disk", {}).get("after", {})
    minimum_ratio = float(args.min_disk_free_percent) / 100.0
    disk_ratios = {name: item.get("free_ratio") for name, item in disk_after.items() if item}
    _check(checks, "disk_free_after", bool(disk_ratios) and all(
        value is not None and value >= minimum_ratio for value in disk_ratios.values()
    ), disk_ratios, ">= {}%".format(args.min_disk_free_percent))

    optional_time_limits = {
        "inventory": float(args.max_inventory_seconds or 0),
        "analysis": float(args.max_analysis_seconds or 0),
        "end_to_end": float(args.max_end_to_end_seconds or 0),
    }
    for name, maximum in optional_time_limits.items():
        if maximum <= 0:
            continue
        actual = (
            result.get("timing_seconds", {}).get("end_to_end") if name == "end_to_end"
            else result.get("stages", {}).get(name, {}).get("seconds")
        )
        _check(checks, "{}_time".format(name), actual is not None and actual <= maximum,
               actual, "<= {} seconds".format(maximum))
    passed = all(item["passed"] for item in checks if item["required"])
    return {
        "passed": passed,
        "required_check_count": sum(1 for item in checks if item["required"]),
        "failed_required_checks": [item["name"] for item in checks if item["required"] and not item["passed"]],
        "checks": checks,
    }


def _runtime_imports():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from config import Config
    from services.exporter import export_node
    from services.large_package import inventory_by_path
    from services.package_analysis import analyze_package
    from services.retrieval import retrieve_evidence
    from services.scanner import scan_directory
    from services.storage import Storage
    from services.unified_parser import UnifiedDocumentParser
    return {
        "Config": Config,
        "export_node": export_node,
        "inventory_by_path": inventory_by_path,
        "analyze_package": analyze_package,
        "retrieve_evidence": retrieve_evidence,
        "scan_directory": scan_directory,
        "Storage": Storage,
        "UnifiedDocumentParser": UnifiedDocumentParser,
    }


def run_benchmark(args):
    runtime = _runtime_imports()
    Config = runtime["Config"]
    root = Path(args.root).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    work_base = Path(args.work_dir).expanduser().resolve()
    export_dir = Path(args.export_dir).expanduser().resolve() if args.export_dir else output_dir / "artifacts"
    run_retrieval = bool(args.run_retrieval or args.profile == "acceptance")
    run_recovery = bool(args.run_recovery or args.profile == "acceptance")
    run_export = bool(args.run_export or args.profile == "acceptance")
    writable_paths = {"output": output_dir, "work": work_base, "parse_temp": Config.PARSE_TEMP_DIR}
    if run_export:
        writable_paths["export"] = export_dir
    inside_input = {name: str(path) for name, path in writable_paths.items() if _is_within(path, root)}
    if inside_input:
        raise ValueError(
            "benchmark writable directories must stay outside the measured input: {}".format(inside_input)
        )
    for folder in (output_dir, work_base):
        folder.mkdir(parents=True, exist_ok=True)
    if run_export:
        export_dir.mkdir(parents=True, exist_ok=True)
        export_run_dir = export_dir / "{}-{}-{}".format(
            safe_label(args.label), datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), os.getpid()
        )
        export_run_dir.mkdir(parents=False, exist_ok=False)
    else:
        export_run_dir = None

    result = {
        "schema_version": SCHEMA_VERSION,
        "label": args.label,
        "profile": args.profile,
        "mode": args.mode,
        "root": str(root),
        "started_at": utc_now(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "memory_total_bytes": total_memory_bytes(),
        },
        "fixture_policy": {
            "expected_size_gib": int(args.expected_size_gib),
            "size_tolerance_percent": float(args.size_tolerance_percent),
            "sparse_files_allowed": False,
            "measurement_source": "independent os.scandir/stat walk; application totals are not modified",
        },
        "runtime_limits": {
            "max_content_bytes": Config.MAX_CONTENT_BYTES,
            "max_single_file_bytes": Config.MAX_SINGLE_FILE_BYTES,
            "max_archive_file_bytes": Config.MAX_ARCHIVE_FILE_BYTES,
            "max_archive_member_bytes": Config.MAX_ARCHIVE_MEMBER_BYTES,
            "max_archive_uncompressed_bytes": Config.MAX_ARCHIVE_UNCOMPRESSED_BYTES,
            "max_export_bytes": Config.MAX_EXPORT_BYTES,
            "max_archive_entries": Config.MAX_ARCHIVE_ENTRIES,
            "max_archive_compression_ratio": Config.MAX_ARCHIVE_COMPRESSION_RATIO,
            "parse_temp_dir": str(Config.PARSE_TEMP_DIR),
            "parse_temp_disk_reserve_bytes": Config.PARSE_TEMP_DISK_RESERVE_BYTES,
        },
        "stages": {},
        "hooks": {
            "retrieval": {"status": "not_requested"},
            "recovery": {"status": "not_requested"},
            "export": {"status": "not_requested"},
        },
    }
    stage_data = result["stages"]
    real_input = timed_stage(
        "independent_measurement",
        # The independent walk deliberately has no scanner ignore rules. If the
        # product omits an ordinary file or directory, the 100% contract fails
        # instead of letting both measurements agree on the same omission.
        lambda: measure_real_input(root),
        stage_data,
    )
    result["input"] = {"independent_measurement": real_input}
    boundary_kind = getattr(args, "ten_gib_boundary_kind", None)
    if boundary_kind in {"regular", "archive"}:
        result["boundary_evidence"] = {
            "kind": boundary_kind,
            "path": real_input.get("largest_{}_file_path".format(boundary_kind)),
            "object_bytes": int(real_input.get("largest_{}_file_bytes".format(boundary_kind)) or 0),
            "analysis_status": None,
        }
    if real_input["sparse_file_count"]:
        raise ValueError(
            "official benchmark refuses sparse fixtures: {} sparse files found".format(
                real_input["sparse_file_count"]
            )
        )

    run_dir = Path(tempfile.mkdtemp(prefix="sjfx-benchmark-", dir=str(work_base)))
    result["work_directory"] = str(run_dir)
    disk_paths = {
        "input": root,
        "work": run_dir,
        "parse_temp": Config.PARSE_TEMP_DIR,
        "results": output_dir,
    }
    if run_export:
        disk_paths["export"] = export_run_dir
    disk_before = {name: disk_snapshot(path) for name, path in disk_paths.items()}
    result["disk"] = {"before": disk_before}
    sampler = ResourceSampler(args.sample_interval).start()
    benchmark_started = time.perf_counter()
    try:
        scan = timed_stage(
            "inventory",
            lambda: runtime["scan_directory"](
                root,
                max_files=Config.MAX_SCAN_FILES,
                max_depth=Config.MAX_SCAN_DEPTH,
                max_directories=Config.MAX_SCAN_DIRECTORIES,
                max_nodes=Config.MAX_SCAN_NODES,
                max_entries_per_directory=Config.MAX_SCAN_ENTRIES_PER_DIRECTORY,
            ),
            stage_data,
        )
        scan["parse_mode"] = args.mode
        inventory_contract = compare_inventory(real_input, scan)
        result["inventory_contract"] = inventory_contract
        result["input"].update({
            "scanner": {
                "files": scan.get("file_count"),
                "directories": scan.get("directory_count"),
                "bytes": scan.get("total_size"),
                "types": scan.get("type_counts"),
                "truncated": scan.get("truncated"),
                "errors": scan.get("errors"),
            }
        })

        database_path = run_dir / "benchmark.db"
        payload_dir = run_dir / "payloads"
        storage = runtime["Storage"](database_path, payload_dir)
        scan_id = storage.save_scan(scan, owner_id="benchmark")
        parser_instance = runtime["UnifiedDocumentParser"]()
        analysis = timed_stage(
            "analysis",
            lambda: runtime["analyze_package"](
                scan_id, scan, storage, parser_instance,
                large_options={"threshold_bytes": Config.LARGE_PACKAGE_THRESHOLD_BYTES},
            ),
            stage_data,
        )
        result["scan_id"] = scan_id
        result["statistics"] = analysis.get("statistics") or {}
        result["coverage"] = analysis.get("coverage") or {}
        result["value_judgment"] = analysis.get("value_judgment") or {}
        if result.get("boundary_evidence", {}).get("path"):
            boundary_state = storage.get_file_state(
                scan_id, result["boundary_evidence"]["path"]
            ) or {}
            result["boundary_evidence"]["analysis_status"] = boundary_state.get("status")

        if run_retrieval:
            query = choose_retrieval_query(analysis, args.retrieval_query)

            def retrieval_hook():
                runs = []
                for _index in range(2):
                    started = time.perf_counter()
                    candidates = storage.search_evidence_index(scan_id, query, limit=args.retrieval_candidate_limit)
                    response = runtime["retrieve_evidence"](
                        [], query, top_k=args.retrieval_top_k, indexed_chunks=candidates
                    )
                    runs.append({
                        "seconds": round(time.perf_counter() - started, 6),
                        "candidate_count": len(candidates),
                        "result_count": len(response.get("results") or []),
                        "evidence_ids": [str(item.get("evidence_id") or "") for item in response.get("results") or []],
                        "index_mode": response.get("index_mode"),
                    })
                available = storage.count_evidence_index(scan_id)
                repeatable = runs[0]["evidence_ids"] == runs[1]["evidence_ids"]
                return {
                    "status": "passed" if available > 0 and repeatable else "failed",
                    "query": query,
                    "persistent_index_chunks": available,
                    "repeatable": repeatable,
                    "runs": runs,
                }

            result["hooks"]["retrieval"] = timed_stage("retrieval", retrieval_hook, stage_data)

        if run_recovery:
            before_states = storage.list_file_states(scan_id)
            before_terminal = sum(1 for item in before_states if item.get("status") in {"completed", "overview", "failed"})
            reuse_counter = {"value": 0}

            def recovery_progress(_percent, message):
                if str(message).startswith("复用已校验检查点"):
                    reuse_counter["value"] += 1

            def recovery_hook():
                reopened = runtime["Storage"](database_path, payload_dir)
                recovered = runtime["analyze_package"](
                    scan_id, scan, reopened, runtime["UnifiedDocumentParser"](),
                    progress=recovery_progress,
                    large_options={"threshold_bytes": Config.LARGE_PACKAGE_THRESHOLD_BYTES},
                )
                after_states = reopened.list_file_states(scan_id)
                after_terminal = sum(
                    1 for item in after_states if item.get("status") in {"completed", "overview", "failed"}
                )
                pending = int((recovered.get("coverage") or {}).get("pending_files") or 0)
                completed_before = sum(
                    1 for item in before_states if item.get("status") in {"completed", "overview"}
                )
                passed = pending == 0 and after_terminal == len(after_states) and reuse_counter["value"] >= completed_before
                return {
                    "status": "passed" if passed else "failed",
                    "method": "fresh Storage/parser checkpoint re-entry",
                    "terminal_states_before": before_terminal,
                    "terminal_states_after": after_terminal,
                    "checkpoint_reuse_messages": reuse_counter["value"],
                    "completed_checkpoints_expected": completed_before,
                    "pending_files_after": pending,
                    "note": "This validates deterministic restart/re-entry; the operations runbook also requires a real Worker kill/restart drill.",
                }

            result["hooks"]["recovery"] = timed_stage("recovery", recovery_hook, stage_data)

        if run_export:
            def export_hook():
                documents = storage.list_documents(scan_id, hydrate=False)
                states = {item.get("node_path"): item for item in storage.list_file_states(scan_id)}
                boundary_path = (result.get("boundary_evidence") or {}).get("path")
                # An exact 10 GiB boundary object plus the small mixed-format
                # fixtures makes the whole directory slightly larger than the
                # 10 GiB per-export contract. In that matrix cell, export the
                # declared boundary object; other tiers export the full root.
                export_selection = (
                    root / boundary_path
                    if int(args.expected_size_gib) == 10 and boundary_path
                    else root
                )
                archive = runtime["export_node"](
                    root, export_selection, analysis.get("overview") or {}, export_run_dir, Config.MAX_EXPORT_BYTES,
                    analysis=analysis,
                    documents=documents,
                    task_topic="SJFX {} 真实性能验收".format(args.label),
                    inventory_metadata=runtime["inventory_by_path"](scan),
                    file_states=states,
                    content_deduplication=False,
                    disk_reserve_bytes=Config.EXPORT_DISK_RESERVE_BYTES,
                )
                size = Path(archive).stat().st_size
                digest = sha256_file(archive)
                return {
                    "status": "passed" if size > 0 and len(digest) == 64 else "failed",
                    "path": str(Path(archive).resolve()),
                    "bytes": size,
                    "sha256": digest,
                    "source_scope": str(boundary_path or "."),
                }

            result["hooks"]["export"] = timed_stage("export", export_hook, stage_data)

        result["state_and_cache_bytes"] = directory_bytes(run_dir)
    finally:
        result["timing_seconds"] = {
            "inventory": stage_data.get("inventory", {}).get("seconds"),
            "analysis": stage_data.get("analysis", {}).get("seconds"),
            "retrieval": stage_data.get("retrieval", {}).get("seconds"),
            "recovery": stage_data.get("recovery", {}).get("seconds"),
            "export": stage_data.get("export", {}).get("seconds"),
            "end_to_end": round(time.perf_counter() - benchmark_started, 3),
        }
        result["resources"] = sampler.stop()
        disk_after = {name: disk_snapshot(path) for name, path in disk_paths.items()}
        result["disk"]["after"] = disk_after
        result["disk"]["delta"] = {
            name: disk_delta(disk_before[name], disk_after[name]) for name in disk_before
        }
        if not args.keep_work_dir:
            shutil.rmtree(str(run_dir), ignore_errors=True)
            result["work_directory_retained"] = False
        else:
            result["work_directory_retained"] = True
    result["completed_at"] = utc_now()
    result["acceptance"] = evaluate_contract(result, args)
    return result


def atomic_write_json(target, payload):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(target))


def build_argument_parser():
    parser = argparse.ArgumentParser(
        description="SJFX real 1/5/10 GiB package benchmark (no fabricated size metadata)"
    )
    parser.add_argument("root", type=Path, help="real mixed-data package directory")
    parser.add_argument("--expected-size-gib", type=int, choices=(1, 5, 10), required=True)
    parser.add_argument("--label", default=None)
    parser.add_argument("--profile", choices=("smoke", "acceptance"), default="smoke")
    parser.add_argument("--mode", choices=("fast", "accurate"), default="fast")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "outputs" / "benchmarks")
    parser.add_argument(
        "--work-dir", type=Path,
        default=Path(os.getenv("SJFX_BENCHMARK_WORK_DIR", str(PROJECT_ROOT / "outputs" / "benchmarks" / "work"))),
        help="dedicated local benchmark state/cache directory",
    )
    parser.add_argument("--export-dir", type=Path, default=None)
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--run-retrieval", action="store_true")
    parser.add_argument("--run-recovery", action="store_true")
    parser.add_argument("--run-export", action="store_true")
    parser.add_argument("--retrieval-query", default=None)
    parser.add_argument("--retrieval-top-k", type=int, default=8)
    parser.add_argument("--retrieval-candidate-limit", type=int, default=2500)
    parser.add_argument("--size-tolerance-percent", type=float, default=10.0)
    parser.add_argument(
        "--min-parse-success-percent", type=float, default=95.0,
        help="minimum inventoried-file parse success ratio required by the contract",
    )
    parser.add_argument(
        "--ten-gib-boundary-kind", choices=("regular", "archive"), default=None,
        help="for the 10 GiB acceptance tier, identify the exact-size ordinary/archive boundary object",
    )
    parser.add_argument("--max-rss-percent", type=float, default=70.0)
    parser.add_argument("--min-disk-free-percent", type=float, default=20.0)
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument("--max-inventory-seconds", type=float, default=0.0)
    parser.add_argument("--max-analysis-seconds", type=float, default=0.0)
    parser.add_argument("--max-end-to-end-seconds", type=float, default=0.0)
    return parser


def main(argv=None):
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.label is None:
        args.label = "{}GiB".format(args.expected_size_gib)
    if not 0 <= args.size_tolerance_percent < 100:
        parser.error("--size-tolerance-percent must be in [0, 100)")
    if not 0 < args.min_parse_success_percent <= 100:
        parser.error("--min-parse-success-percent must be in (0, 100]")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(args.output_dir).expanduser().resolve() / "{}-{}-{}.json".format(
        safe_label(args.label), timestamp, os.getpid()
    )
    try:
        result = run_benchmark(args)
        exit_code = 0 if result["acceptance"]["passed"] else 2
    except KeyboardInterrupt:
        result = {
            "schema_version": SCHEMA_VERSION,
            "label": args.label,
            "profile": args.profile,
            "root": str(Path(args.root).expanduser().resolve()),
            "completed_at": utc_now(),
            "fatal_error": "KeyboardInterrupt: benchmark cancelled",
            "acceptance": {"passed": False, "failed_required_checks": ["benchmark_completed"]},
        }
        exit_code = 130
    except Exception as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "label": args.label,
            "profile": args.profile,
            "root": str(Path(args.root).expanduser().resolve()),
            "completed_at": utc_now(),
            "fatal_error": "{}: {}".format(type(exc).__name__, str(exc))[:4000],
            "traceback": traceback.format_exc(limit=20),
            "acceptance": {"passed": False, "failed_required_checks": ["benchmark_completed"]},
        }
        exit_code = 1
    atomic_write_json(target, result)
    print(json.dumps({
        "ok": exit_code == 0,
        "passed": bool(result.get("acceptance", {}).get("passed")),
        "result": str(target),
        "failed_required_checks": result.get("acceptance", {}).get("failed_required_checks", []),
        "fatal_error": result.get("fatal_error"),
    }, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
