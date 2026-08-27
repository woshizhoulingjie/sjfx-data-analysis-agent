#!/usr/bin/env python3
"""Build a byte-bounded, valid JSON subset from real NVD CVE records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import ijson


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path, block_size=8 * 1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--target-bytes", type=int, default=200 * 1024 * 1024)
    parser.add_argument("--name", default="nvd-cve-real-200m.json")
    return parser.parse_args()


def record_prefix(path):
    with Path(path).open("rb") as handle:
        header = handle.read(16384)
    if b'"cve_items"' in header:
        return "cve_items.item"
    if b'"vulnerabilities"' in header:
        return "vulnerabilities.item"
    raise ValueError("unsupported NVD JSON structure: {}".format(path))


def main():
    args = parse_args()
    source_dir = args.source_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise SystemExit("source directory does not exist: {}".format(source_dir))
    sources = sorted(source_dir.glob("*.json"))
    if not sources:
        raise SystemExit("no JSON source files found under {}".format(source_dir))

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / args.name
    metadata_path = output_dir / "subset_manifest.json"
    temporary_path = output_path.with_suffix(output_path.suffix + ".part")
    target_bytes = max(1, int(args.target_bytes))

    record_count = 0
    source_counts = {}
    started_at = utc_now()
    with temporary_path.open("wb") as output:
        output.write(b'{"dataset":"NVD CVE real-record subset","cve_items":[')
        first = True
        for source in sources:
            selected_from_source = 0
            with source.open("rb") as handle:
                for record in ijson.items(handle, record_prefix(source), use_float=True):
                    encoded = json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    if not first:
                        output.write(b",")
                    output.write(encoded)
                    first = False
                    record_count += 1
                    selected_from_source += 1
                    if output.tell() >= target_bytes:
                        break
            source_counts[source.name] = selected_from_source
            if output.tell() >= target_bytes:
                break
        output.write(b"]}")
        output.flush()
        os.fsync(output.fileno())

    os.replace(str(temporary_path), str(output_path))
    size_bytes = output_path.stat().st_size
    manifest = {
        "schema_version": "nvd-real-subset/1.0",
        "created_at": utc_now(),
        "started_at": started_at,
        "source_directory": str(source_dir),
        "source_files": [
            {
                "path": str(path),
                "bytes": path.stat().st_size,
            }
            for path in sources
        ],
        "output_file": str(output_path),
        "target_bytes": target_bytes,
        "actual_bytes": size_bytes,
        "actual_mib": round(size_bytes / float(1024 ** 2), 6),
        "record_count": record_count,
        "records_by_source": source_counts,
        "sha256": sha256_file(output_path),
        "records_are_complete": True,
        "synthetic_padding_bytes": 0,
    }
    metadata_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
