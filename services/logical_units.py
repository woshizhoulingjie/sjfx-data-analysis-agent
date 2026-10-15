"""Discover independently schedulable logical files inside large containers."""

import csv
import bz2
import io
import hashlib
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from services.unified_parser import _zip_central_directory_preflight, strict_archive_member_name


STRUCTURED_TEXT_EXTENSIONS = {".csv", ".tsv", ".jsonl"}
WORKBOOK_EXTENSIONS = {".xlsx", ".xlsm"}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".bz2", ".rar", ".7z"}


def _safe_member_name(value):
    return strict_archive_member_name(value)


def _base_unit(container, logical_path, name, extension, size, kind, **extra):
    return {
        "id": hashlib.sha1(logical_path.encode("utf-8", errors="replace")).hexdigest()[:16],
        "path": logical_path,
        "name": name,
        "kind": "logical_file",
        "logical_unit": True,
        "logical_kind": kind,
        "container_path": container.get("path"),
        "container_name": container.get("name"),
        "container_size": int(container.get("size") or 0),
        "container_modified_at_ns": int(container.get("modified_at_ns") or 0),
        "container_device": container.get("device"),
        "container_inode": container.get("inode"),
        "extension": str(extension or "").lower(),
        "size": max(0, int(size or 0)),
        "modified_at": container.get("modified_at"),
        "modified_at_ns": int(container.get("modified_at_ns") or 0),
        "content_analysis_allowed": True,
        **extra,
    }


def archive_member_units(root, container, max_units=250000, *, physical_source_override=None):
    original_path = Path(root) / str(container.get("path") or "")
    path = Path(physical_source_override) if physical_source_override is not None else original_path
    archive_name = str(original_path).lower()
    if not any(archive_name.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
        return
    count = 0
    def emit(member, size, compressed_size=0, encrypted=False):
        nonlocal count
        member = _safe_member_name(member)
        if not member:
            return None
        if count >= max_units:
            raise ValueError("压缩包逻辑成员超过安全上限 {}，未建立不完整队列".format(max_units))
        count += 1
        extension = Path(member).suffix.lower()
        logical_path = "{}::{}".format(container["path"], member)
        return _base_unit(
            container, logical_path, Path(member).name, extension,
            int(size or 0), "archive_member", member_name=member,
            compressed_size=int(compressed_size or 0), encrypted=bool(encrypted),
            promotion_allowed=not bool(encrypted), archive_format=archive_name,
        )
    if physical_source_override is not None:
        # A caller may already hold a verified private source snapshot. Keep
        # original inventory identities and dispatch, but never reopen live
        # bytes while discovering its members. The bounded reader inventories
        # every entry before publishing any, including late duplicate names.
        from services.archive_formats import ArchiveLimits, ArchiveReader
        seen = set()
        with ArchiveReader(path, filename=str(original_path),
                           limits=ArchiveLimits.from_config(entry_limit=max_units)) as archive:
            for entry in archive.entries():
                member = _safe_member_name(entry.name)
                if member and not entry.directory:
                    canonical = member[2:] if member.startswith("./") else member
                    if canonical in seen:
                        raise ValueError("归档成员规范路径重复，未发布该归档的成员清单")
                    seen.add(canonical)
                if entry.regular:
                    unit = emit(entry.name, entry.size, entry.compressed_size, entry.encrypted)
                    if unit:
                        yield unit
        return
    if archive_name.endswith(".zip"):
        preflight = _zip_central_directory_preflight(path)
        if not preflight.get("safe"):
            raise ValueError("ZIP中央目录预检失败：{}".format(preflight.get("reason") or "invalid"))
        observed = int(preflight.get("observed_entries") or 0)
        if observed > max_units:
            raise ValueError("压缩包逻辑成员超过安全上限 {}，未建立不完整队列".format(max_units))
        with zipfile.ZipFile(str(path)) as archive:
            if len(archive.filelist) != observed:
                raise ValueError("ZIP中央目录在预检后发生变化")
            for info in archive.filelist:
                if not info.is_dir():
                    unit = emit(info.orig_filename, info.file_size, info.compress_size, info.flag_bits & 0x1)
                    if unit:
                        yield unit
        return
    if archive_name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        with tarfile.open(str(path), mode="r:*") as archive:
            for info in archive:
                if info.isfile():
                    unit = emit(info.name, info.size)
                    if unit:
                        yield unit
        return
    if archive_name.endswith(".bz2"):
        unit = emit(Path(str(path)).stem, path.stat().st_size)
        if unit:
            unit["compressed_stream"] = True
            yield unit
        return
    if archive_name.endswith(".rar"):
        try:
            import rarfile
        except ImportError as exc:
            raise ValueError("RAR逻辑成员需要安装 rarfile 依赖") from exc
        with rarfile.RarFile(str(path)) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    unit = emit(info.filename, info.file_size, info.compress_size, getattr(info, "needs_password", lambda: False)())
                    if unit:
                        yield unit
        return
    try:
        import py7zr
    except ImportError as exc:
        raise ValueError("7z逻辑成员需要安装 py7zr 依赖") from exc
    with py7zr.SevenZipFile(str(path), mode="r") as archive:
        for info in archive.list():
            name = getattr(info, "filename", "")
            if not getattr(info, "is_directory", False):
                unit = emit(name, getattr(info, "uncompressed", 0))
                if unit:
                    yield unit


def structured_text_units(root, container, partition_bytes=1024 * 1024):
    path = Path(root) / str(container.get("path") or "")
    size = int(path.stat().st_size if path.exists() else container.get("size") or 0)
    extension = str(container.get("extension") or "").lower()
    if extension not in STRUCTURED_TEXT_EXTENSIONS or size <= partition_bytes:
        return
    if not path.exists():
        # Inventory-only callers may provide metadata without a local source;
        # retain deterministic queue accounting until the source is mounted.
        for index, start in enumerate(range(0, size, partition_bytes), 1):
            end = min(size, start + partition_bytes)
            yield _base_unit(
                container, "{}::partition/{:06d}".format(container["path"], index),
                "{}-part-{:06d}{}".format(Path(container["path"]).stem, index, extension),
                extension, end - start, "structured_text_partition",
                byte_start=start, byte_end=end, partition_index=index,
                include_header=False, record_boundary="unknown_source",
            )
        return
    include_header = extension in {".csv", ".tsv"}
    ranges = []

    def record_state(line, quoted, field_start):
        delimiter = 0x09 if extension == ".tsv" else 0x2c
        index = 0
        while index < len(line):
            value = line[index]
            if quoted:
                if value == 0x22:
                    if index + 1 < len(line) and line[index + 1] == 0x22:
                        index += 2
                        continue
                    quoted = False
            elif value == delimiter or value in (0x0a, 0x0d):
                field_start = True
            else:
                if value == 0x22 and field_start:
                    quoted = True
                field_start = False
            index += 1
        return quoted, field_start

    with path.open("rb") as source:
        if include_header:
            probe = source.read(4)
            if probe.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
                # UTF-16/32 newlines and quote characters span code units.
                # Retain the complete source for the existing parser instead
                # of generating byte partitions that can corrupt its records.
                return
            source.seek(0)
        header = b""
        if include_header:
            header_lines = []
            quoted, field_start = False, True
            for line in source:
                header_lines.append(line)
                # A UTF-8 BOM is encoding metadata, not an unquoted field
                # prefix. Keep its bytes in the header while allowing a quote
                # at the beginning of the first actual field to open it.
                state_line = line[3:] if len(header_lines) == 1 and line.startswith(b"\xef\xbb\xbf") else line
                quoted, field_start = record_state(state_line, quoted, field_start)
                if not quoted:
                    break
            header = b"".join(header_lines)
        data_start = source.tell() if include_header else 0
        data_ranges = []
        chunk_start = data_start
        chunk_end = data_start
        # CSV/TSV records are allowed to contain physical newlines inside a
        # quoted field.  Track RFC 4180 quote state while streaming lines and
        # only close a partition at a complete logical record.  The previous
        # implementation used ``for line`` boundaries and split such records,
        # causing the next partition to parse as invented one-column rows.
        in_quotes = False

        field_start = True
        for line in source:
            chunk_end = source.tell()
            if include_header:
                in_quotes, field_start = record_state(line, in_quotes, field_start)
            record_complete = not in_quotes
            # Emit an oversized record as one unit.  For ordinary records this
            # keeps each partition near the requested byte budget while never
            # crossing a quoted multiline record boundary.
            if record_complete and chunk_end > chunk_start and (
                chunk_end - chunk_start >= partition_bytes
            ):
                data_ranges.append((chunk_start, chunk_end))
                chunk_start = chunk_end
        if chunk_end > chunk_start:
            data_ranges.append((chunk_start, chunk_end))
        if include_header and data_ranges:
            ranges.append((0, data_ranges[0][1]))
            ranges.extend(data_ranges[1:])
        elif include_header and header:
            ranges.append((0, len(header)))
        else:
            ranges = data_ranges
    if include_header and header:
        # Non-first CSV partitions carry a header explicitly; JSONL never does.
        pass
    index = 1
    for start, end in ranges:
        logical_path = "{}::partition/{:06d}".format(container["path"], index)
        actual_size = end - start + (len(header) if include_header and index > 1 else 0)
        yield _base_unit(
            container, logical_path,
            "{}-part-{:06d}{}".format(Path(container["path"]).stem, index, extension),
            extension, actual_size, "structured_text_partition",
            byte_start=start, byte_end=end, partition_index=index,
            include_header=bool(include_header and index > 1),
            header_byte_end=len(header) if include_header else 0,
            record_boundary="record",
        )
        index += 1


def workbook_units(root, container, rows_per_partition=10000, max_units=250000):
    extension = str(container.get("extension") or "").lower()
    if extension not in WORKBOOK_EXTENSIONS:
        return
    try:
        import openpyxl
    except ImportError:
        return
    path = Path(root) / str(container.get("path") or "")
    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        count = 0
        for worksheet in workbook.worksheets:
            max_row = max(1, int(worksheet.max_row or 1))
            start_row = 1
            part = 1
            while start_row <= max_row:
                if count >= max_units:
                    raise ValueError("工作簿逻辑分片超过安全上限 {}".format(max_units))
                end_row = min(max_row, start_row + rows_per_partition - 1)
                measured = io.StringIO(newline="")
                writer = csv.writer(measured)
                actual_rows = 0
                for row in worksheet.iter_rows(min_row=start_row, max_row=end_row, values_only=True):
                    writer.writerow(["" if value is None else value for value in row])
                    actual_rows += 1
                actual_size = len(measured.getvalue().encode("utf-8-sig"))
                encoded_sheet = quote(str(worksheet.title), safe="")
                logical_path = "{}::sheet/{}/part/{:06d}".format(
                    container["path"], encoded_sheet, part
                )
                yield _base_unit(
                    container, logical_path,
                    "{}-{}-part-{:06d}.csv".format(
                        Path(container["path"]).stem,
                        re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", str(worksheet.title))[:80],
                        part,
                    ),
                    ".csv", actual_size, "workbook_rows",
                    sheet_name=str(worksheet.title), row_start=start_row,
                    row_end=end_row, partition_index=part,
                    actual_row_count=actual_rows, estimated_size=False,
                )
                count += 1
                start_row = end_row + 1
                part += 1
    finally:
        workbook.close()


def iter_logical_units(root, physical_files, partition_bytes=1024 * 1024,
                       rows_per_partition=10000, max_units_per_container=250000,
                       *, physical_source_overrides=None):
    """Yield logical children; ordinary physical files remain unchanged."""
    for container in physical_files:
        path_name = str(container.get("path") or "").lower()
        extension = str(container.get("extension") or Path(path_name).suffix).lower()
        if extension in ARCHIVE_EXTENSIONS or any(path_name.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
            yield from archive_member_units(
                root, container, max_units=max_units_per_container,
                physical_source_override=(physical_source_overrides or {}).get(container.get("path")),
            )
        elif extension in STRUCTURED_TEXT_EXTENSIONS:
            yield from structured_text_units(root, container, partition_bytes=partition_bytes)
        elif extension in WORKBOOK_EXTENSIONS:
            yield from workbook_units(
                root, container, rows_per_partition=rows_per_partition,
                max_units=max_units_per_container,
            )


def container_node_for(unit):
    """Reconstruct the verified physical inventory identity for one unit."""
    return {
        "path": unit.get("container_path"),
        "name": unit.get("container_name") or Path(str(unit.get("container_path") or "")).name,
        "size": int(unit.get("container_size") or 0),
        "modified_at_ns": int(unit.get("container_modified_at_ns") or 0),
        "modified_at": unit.get("modified_at"),
        "device": unit.get("container_device"),
        "inode": unit.get("container_inode"),
        "extension": Path(str(unit.get("container_path") or "")).suffix.lower(),
    }
