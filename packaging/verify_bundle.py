#!/usr/bin/env python3
"""Verify an extracted SJFX offline bundle against ``manifest.json``.

The checker is intentionally dependency-free so it can run with the bundled
interpreter before an install or upgrade swaps the active application tree.
It treats the manifest as an allow-list: a successful strict check means every
listed file has the recorded size and SHA-256 and no unlisted regular file is
present.  Symlinks and paths escaping the bundle root are always rejected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import zipfile


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(raw: object) -> Path:
    # Colons can select another drive or a Windows alternate data stream,
    # even when Path.is_absolute() is false (for example D:relative).
    if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw:
        raise ValueError("manifest path must be a non-empty POSIX relative path")
    components = raw.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("manifest contains an unsafe path: {}".format(raw))
    path = Path(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("manifest contains an unsafe path: {}".format(raw))
    return path


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (
        os.name == "nt" and getattr(path.lstat(), "st_reparse_tag", 0)
        == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    )


def _bundle_entries(root: Path):
    pending = [root]
    while pending:
        for item in pending.pop().iterdir():
            yield item
            if item.is_dir() and not _is_link(item):
                pending.append(item)


def verify(root: Path, *, strict: bool = True) -> dict:
    root = Path(root).expanduser().resolve()
    manifest_path = root / "manifest.json"
    report = {"ok": False, "root": str(root), "manifest": str(manifest_path),
              "checked": 0, "failures": [], "extra_files": []}
    try:
        if not manifest_path.is_file() or _is_link(manifest_path):
            raise ValueError("manifest.json is missing or is a symlink")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise ValueError("unsupported manifest schema")
        entries = payload.get("files")
        if not isinstance(entries, list):
            raise ValueError("manifest files must be a list")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        report["failures"].append(str(exc))
        return report

    expected = set()
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                raise ValueError("manifest file entry must be an object")
            relative = _relative_path(entry.get("path"))
            key = relative.as_posix()
            if key in expected or key == "manifest.json":
                raise ValueError("duplicate or reserved manifest path: {}".format(key))
            expected.add(key)
            target = (root / relative).resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError:
                raise ValueError("manifest path escapes bundle root: {}".format(key))
            raw_target = root / relative
            if not raw_target.is_file() or _is_link(raw_target):
                raise ValueError("missing or symlinked file: {}".format(key))
            expected_size = entry.get("bytes")
            expected_hash = entry.get("sha256")
            expected_mode = entry.get("mode")
            if type(expected_size) is not int or expected_size < 0:
                raise ValueError("invalid byte count: {}".format(key))
            if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                raise ValueError("invalid SHA-256: {}".format(key))
            if type(expected_mode) is not int or expected_mode < 0 or expected_mode > 0o777:
                raise ValueError("invalid mode: {}".format(key))
            actual_size = raw_target.stat().st_size
            actual_hash = _digest(raw_target)
            actual_mode = stat.S_IMODE(raw_target.stat().st_mode)
            if actual_size != expected_size:
                raise ValueError("size mismatch: {}".format(key))
            if actual_hash.lower() != expected_hash.lower():
                raise ValueError("SHA-256 mismatch: {}".format(key))
            # Windows exposes a coarse read-only bit through chmod and cannot
            # round-trip POSIX owner/group/execute bits from ZIP metadata.
            if os.name == "posix" and actual_mode != expected_mode:
                raise ValueError("mode mismatch: {}".format(key))
            report["checked"] += 1
        except (OSError, ValueError) as exc:
            report["failures"].append(str(exc))

    actual = set()
    try:
        for item in _bundle_entries(root):
            relative = item.relative_to(root).as_posix()
            if _is_link(item):
                report["failures"].append("symlink is not allowed: {}".format(relative))
                continue
            if item.is_file() and relative != "manifest.json":
                actual.add(relative)
    except OSError as exc:
        report["failures"].append("cannot enumerate bundle: {}".format(exc))
    report["extra_files"] = sorted(actual - expected)
    if strict and report["extra_files"]:
        report["failures"].append("unlisted files present: {}".format(", ".join(report["extra_files"])))
    report["ok"] = not report["failures"]
    return report


def _archive_root(extracted: Path) -> Path:
    """Locate the bundle directory after extracting an archive."""
    if (extracted / "manifest.json").is_file():
        return extracted
    children = list(extracted.iterdir())
    candidates = [item for item in children if item.is_dir() and not _is_link(item)]
    if (len(children) == 1 and len(candidates) == 1
            and (candidates[0] / "manifest.json").is_file()):
        return candidates[0]
    raise ValueError("archive does not contain one bundle root with manifest.json")


def _extract_member_name(raw: str) -> Path:
    # Archive member names use POSIX separators even on Windows.  Reusing the
    # manifest path policy prevents absolute and parent-directory extraction.
    return _relative_path(raw)


def _extract_archive(archive: Path, destination: Path) -> None:
    suffixes = ''.join(archive.suffixes[-2:]).lower()
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as source:
            for info in source.infolist():
                name = info.filename.rstrip("/")
                if not name:
                    continue
                relative = _extract_member_name(name)
                target = destination / relative
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                # DOS/Unix mode bits in external_attr identify symlinks.  Do
                # not materialize links from an untrusted archive.
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ValueError("archive contains a symlink: {}".format(name))
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as stream, target.open("wb") as output:
                    while (chunk := stream.read(1024 * 1024)):
                        output.write(chunk)
                # The builder records portable permission bits in the ZIP
                # external attributes. Restore them before manifest checks.
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    target.chmod(mode)
        return
    if archive.suffix.lower() not in {".gz", ".tgz", ".tar"} and not suffixes.endswith(".tar.gz"):
        raise ValueError("archive must be a ZIP or tar archive")
    with tarfile.open(archive, "r:*") as source:
        for member in source.getmembers():
            relative = _extract_member_name(member.name.rstrip("/"))
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk() or not member.isfile():
                raise ValueError("archive contains an unsupported member: {}".format(member.name))
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError("archive member cannot be read: {}".format(member.name))
            with stream, target.open("wb") as output:
                while (chunk := stream.read(1024 * 1024)):
                    output.write(chunk)
            target.chmod(member.mode & 0o777)


def verify_archive(archive: Path, *, strict: bool = True) -> dict:
    archive = Path(archive).expanduser().resolve()
    try:
        with tempfile.TemporaryDirectory(prefix="sjfx-verify-") as temporary:
            extracted = Path(temporary)
            _extract_archive(archive, extracted)
            return verify(_archive_root(extracted), strict=strict)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return {"ok": False, "archive": str(archive), "checked": 0,
                "failures": [str(exc)], "extra_files": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--root", help="extracted bundle root")
    source.add_argument("--archive", help="ZIP or tar(.gz) bundle to extract and verify")
    parser.add_argument("--allow-extra", action="store_true",
                        help="allow files not listed in manifest.json")
    args = parser.parse_args(argv)
    report = (verify_archive(Path(args.archive), strict=not args.allow_extra)
              if args.archive else verify(Path(args.root), strict=not args.allow_extra))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
