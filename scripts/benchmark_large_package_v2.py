#!/usr/bin/env python3
"""Sparse large-package acceptance benchmark for the durable v2 workflow."""

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.large_package import package_resource_plan
from services.package_exploration import preview_as_document, preview_file
from services.retrieval import evidence_corpus
from services.scanner import scan_inventory_slice
from services.storage import Storage


def sparse_package(root, file_count, total_bytes):
    per_file = max(1, total_bytes // file_count)
    remaining = total_bytes
    for index in range(file_count):
        size = remaining if index == file_count - 1 else min(per_file, remaining)
        path = root / "group-{:04d}".format(index % 256) / "file-{:08d}.txt".format(index)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            if size:
                handle.seek(size - 1)
                handle.write(b"\0")
        remaining -= size
    control = root / "control-recall.txt"
    payload = b"HEAD-CONTROL " + b"A" * (2 * 1024 * 1024) + b" MIDDLE-NEEDLE "
    payload += b"B" * (2 * 1024 * 1024) + b" TAIL-NEEDLE"
    control.write_bytes(payload)
    return control


def run(args):
    started = time.monotonic()
    target_bytes = int(args.target_gib * 1024 ** 3)
    holder = tempfile.TemporaryDirectory(prefix="sjfx-large-v2-")
    root = Path(holder.name) / "package"
    root.mkdir()
    control = sparse_package(root, args.files, target_bytes)
    state_dir = Path(holder.name) / "state"
    state_dir.mkdir()
    db_path = state_dir / "state.db"
    storage = Storage(db_path, sidecar_dir=state_dir / "sidecars")

    cursor = None
    slices = 0
    scan = None
    inventory_started = time.monotonic()
    while True:
        result = scan_inventory_slice(
            root, cursor=cursor, slice_entries=args.slice_entries,
            slice_seconds=args.slice_seconds,
        )
        slices += 1
        scan = storage.save_inventory_slice(
            "benchmark", root, result["cursor"], result["records"],
            owner_id="benchmark", complete=result["complete"],
        )
        if result["complete"]:
            break
        storage = Storage(db_path, sidecar_dir=state_dir / "sidecars")
        cursor = storage.get_inventory_cursor("benchmark")
        cursor.pop("status", None)
    inventory_seconds = time.monotonic() - inventory_started

    preview_paths = [control]
    if args.hash_all:
        preview_paths = [
            root / item["path"]
            for item in storage.iter_inventory_entries("benchmark", kind="file")
        ]
    else:
        for item in storage.iter_inventory_entries("benchmark", kind="file"):
            candidate = root / item["path"]
            if candidate != control:
                preview_paths.append(candidate)
            if len(preview_paths) >= max(1, args.hash_sample_files):
                break

    hash_started = time.monotonic()
    hashed_bytes = 0
    for path in preview_paths:
        relative = str(path.relative_to(root)).replace(os.sep, "/")
        node = storage.get_inventory_entry("benchmark", relative)
        preview = preview_file(root, node, per_file_bytes=96 * 1024)
        document = preview_as_document(preview)
        storage.save_file_preview("benchmark", relative, preview)
        storage.replace_document_evidence_index(
            "benchmark", relative, evidence_corpus({relative: document}),
            preserve_translations=True,
        )
        hashed_bytes += int(node.get("size") or 0)
    hash_seconds = time.monotonic() - hash_started

    middle_hits = storage.search_evidence_index("benchmark", "MIDDLE-NEEDLE", limit=20)
    tail_hits = storage.search_evidence_index("benchmark", "TAIL-NEEDLE", limit=20)
    disk = os.statvfs(str(state_dir)) if hasattr(os, "statvfs") else None
    free_bytes = disk.f_bavail * disk.f_frsize if disk else 10 ** 15
    plan = package_resource_plan(scan, free_bytes, free_bytes)
    rss_kib = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    output = {
        "schema_version": "large-package-acceptance/2.0",
        "target_bytes": target_bytes,
        "inventory_files": scan["file_count"],
        "inventory_bytes": scan["total_size"],
        "inventory_complete": scan["inventory_complete"],
        "inventory_slices": slices,
        "inventory_seconds": round(inventory_seconds, 3),
        "restart_resume_exercised": slices > 1,
        "state_db_bytes": db_path.stat().st_size,
        "wal_bytes": Path(str(db_path) + "-wal").stat().st_size
        if Path(str(db_path) + "-wal").exists() else 0,
        "peak_rss_kib": rss_kib,
        "hashed_bytes": hashed_bytes,
        "hash_seconds": round(hash_seconds, 3),
        "hash_all": bool(args.hash_all),
        "middle_window_recall": bool(middle_hits),
        "tail_window_recall": bool(tail_hits),
        "resource_plan": plan,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    if args.keep_root:
        print("benchmark_root={}".format(holder.name), file=sys.stderr)
        holder.cleanup = lambda: None
    else:
        holder.cleanup()
    return 0 if all((
        output["inventory_complete"], output["middle_window_recall"],
        output["tail_window_recall"], output["restart_resume_exercised"],
    )) else 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-gib", type=float, default=100.0)
    parser.add_argument("--files", type=int, default=50000)
    parser.add_argument("--slice-entries", type=int, default=1000)
    parser.add_argument("--slice-seconds", type=int, default=20)
    parser.add_argument("--hash-sample-files", type=int, default=3)
    parser.add_argument("--hash-all", action="store_true")
    parser.add_argument("--keep-root", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
