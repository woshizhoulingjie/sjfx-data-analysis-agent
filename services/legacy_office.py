"""Read legacy Office/RTF locally with bounded, killable helper processes.

The caller keeps the original source path/hash and owns summary generation.
No document is converted in place, no macros or external services are invoked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

# This module is also invoked by absolute script path in a helper interpreter.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.process_liveness import initialize_process_family, own_process_tree, terminate_process_tree
from services.parse_temp import temporary_parse_directory

EXTENSIONS = frozenset({".doc", ".ppt", ".xls", ".rtf"})
TOOLS = Path(__file__).resolve().parents[1] / ".tools" / "legacy-office" / "usr"


def _tool_roots():
    from config import Config, resolve_config_path
    configured = str(os.getenv("SJFX_TOOLS_DIR") or "").strip()
    root = resolve_config_path(configured) if configured else Config.TOOLS_DIR
    return tuple(dict.fromkeys((root / "legacy-office" / "usr", root / "legacy-office", root)))


def _find_tool(tool):
    """Find external Windows .exe tools and retain the bundled Linux layout."""
    import shutil
    names = (tool + ".exe",) if os.name == "nt" else (tool,)
    for root in _tool_roots():
        for directory in (root / "bin", root):
            for name in names:
                executable = directory / name
                if executable.is_file():
                    return str(executable), root
    for name in names:
        executable = shutil.which(name)
        if executable:
            return executable, None
    raise RuntimeError("缺少本地旧格式解析组件：" + tool)


def _limit(name, default, low, high):
    try:
        return max(low, min(high, int(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default


def _empty(reason, limited=True):
    return {"text": "", "parser": "legacy-office", "warnings": [reason],
            "metadata": {"content_complete": False, "reason": reason},
            "char_count": 0, "truncated": limited}


def extract_legacy(path, max_chars=200000, *, preview=False, refine=False, seconds=None):
    """Return scanner-compatible text; failed/partial extraction is explicit."""
    path = Path(path).resolve()
    if path.suffix.lower() not in EXTENSIONS:
        raise ValueError("不是旧版 Office/RTF 文件")
    max_chars = max(1, min(int(max_chars), 5000000))
    max_bytes = _limit("SJFX_LEGACY_MAX_BYTES", 512 * 1024**2, 1024**2, 10 * 1024**3)
    if preview:
        max_bytes = _limit("SJFX_LP_LEGACY_PREVIEW_BYTES", 8 * 1024**2, 65536, 32 * 1024**2) * (3 if refine else 1)
    deadline = min(float(seconds or (15 if refine else 4 if preview else 60)), 120)
    before = path.stat()
    # RTF is prefix-readable; OLE containers must retain their offset tables.
    if before.st_size > max_bytes and (not preview or path.suffix.lower() != ".rtf"):
        return _empty("旧格式文件超过本次读取预算；已保留清单，可选择深度解析或提高旧格式读取预算")
    request = {"parent_pid": os.getpid(), "path": str(path), "max_chars": max_chars, "max_bytes": max_bytes,
               "preview": bool(preview), "refine": bool(refine), "seconds": deadline}
    with temporary_parse_directory(prefix="sjfx-legacy-") as scratch:
        target = Path(scratch) / "result.json"
        # The native tool cannot run before the Windows parent owns its tree.
        gate = Path(scratch) / "start"
        request["start_gate"] = str(gate)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", json.dumps(request), str(target)]
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        job_owner = None
        try:
            job_owner = own_process_tree(proc.pid)
            if os.name == "nt" and job_owner is None:
                raise RuntimeError("无法建立 Windows 旧格式解析子进程回收边界")
            gate.touch()
            proc.wait(timeout=max(.1, deadline))
            if proc.returncode != 0 or not target.exists():
                return _empty("旧格式解析进程失败或达到资源上限；未将空正文标记为成功")
            result = json.loads(target.read_text(encoding="utf-8"))
        except subprocess.TimeoutExpired:
            return _empty("旧格式解析时间预算已用尽；可选择深度解析重试")
        finally:
            # Also reap native parser descendants on timeout/cancellation.
            terminate_process_tree(proc, grace_seconds=.5, job=job_owner, private_group=True)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError("文件在旧格式解析过程中发生变化，已丢弃结果")
    return result


def _native(path, ext, max_chars, seconds):
    tool = "antiword" if ext == ".doc" else "catppt"
    executable, tool_root = _find_tool(tool)
    env = os.environ.copy()
    if tool_root is not None and (tool_root / "share" / "antiword").is_dir():
        env["ANTIWORDHOME"] = str(tool_root / "share" / "antiword")
    args = [executable, "-m", "UTF-8.txt", "-w", "0", str(path)] if ext == ".doc" else [executable, "-d", "utf-8", str(path)]
    with temporary_parse_directory(prefix="sjfx-legacy-native-") as scratch:
        # catdoc resolves charsets through its own private rc file.
        env["HOME"] = scratch
        if tool_root is not None and (tool_root / "share" / "catdoc").is_dir():
            (Path(scratch) / ".catdocrc").write_text("charset_path=" + str(tool_root / "share" / "catdoc") + "\nmap_path=" + str(tool_root / "share" / "catdoc") + "\n", encoding="utf-8")
        out = Path(scratch) / "out.txt"
        err = Path(scratch) / "err.txt"
        with out.open("wb") as stdout, err.open("wb") as stderr:
            proc = subprocess.Popen(args, stdout=stdout, stderr=stderr, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            until = time.monotonic() + seconds
            limited = False
            try:
                while proc.poll() is None:
                    if out.stat().st_size > max_chars * 4 or time.monotonic() >= until:
                        limited = True
                        proc.kill()
                        break
                    time.sleep(.01)
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
        with out.open("rb") as stream:
            raw = stream.read(max_chars * 4 + 4)
        text = raw.decode("utf-8", "replace")
        if proc.returncode != 0 and not limited:
            with err.open("rb") as stream:
                error = stream.read(500).decode("utf-8", "replace")
            raise ValueError(error or "旧格式文件损坏、加密或版本不受解析器支持")
        return text, {"content_complete": not limited, "embedded_objects_ocr": False}, limited


def _xls(path, max_chars, preview, refine):
    import xlrd
    book = xlrd.open_workbook(str(path), on_demand=True, use_mmap=True)
    parts, sections, fields, headings = [], [], [], []
    length, truncated = 0, False
    try:
        names = book.sheet_names()
        selected = list(range(len(names)))
        if preview and len(selected) > (6 if refine else 3):
            n = 6 if refine else 3
            selected = sorted({round(i * (len(names)-1)/(n-1)) for i in range(n)})
            truncated = True
        for index in selected:
            sheet = book.sheet_by_index(index)
            headings.append(sheet.name)
            fields.extend(str(sheet.cell_value(0, c)) for c in range(min(sheet.ncols, 40)) if sheet.nrows and sheet.cell_value(0, c) != "")
            rows = range(sheet.nrows)
            if preview and sheet.nrows > (40 if refine else 8):
                n = 40 if refine else 8
                rows = sorted({0, *[round(i*(sheet.nrows-1)/(n-1)) for i in range(n)]})
                truncated = True
            parts.append("[工作表 {}]".format(sheet.name))
            for r in rows:
                values = []
                for c in range(sheet.ncols):
                    cell = sheet.cell(r, c)
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            value = xlrd.xldate_as_datetime(value, book.datemode).isoformat(sep=" ")
                        except (ValueError, OverflowError):
                            pass
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        value = "TRUE" if value else "FALSE"
                    elif cell.ctype == xlrd.XL_CELL_ERROR:
                        value = xlrd.error_text_from_code.get(value, "#ERROR")
                    elif isinstance(value, float) and value.is_integer():
                        value = int(value)
                    values.append(str(value) if value != "" else "")
                text = "\t".join(values)
                parts.append("[行 {}] {}".format(r+1, text))
                if preview and len(sections) < 48:
                    sections.append({"location": "工作表 {}，行 {}".format(sheet.name, r+1), "text": text[:1800], "kind": "table"})
                length += len(parts[-1]) + 1
                if length >= max_chars:
                    truncated = True
                    break
            book.unload_sheet(index)
            if length >= max_chars:
                break
        return "\n".join(parts), {"sheet_count": len(names), "table_count": len(names), "fields": list(dict.fromkeys(fields))[:40], "headings": headings[:8], "sections": sections, "content_complete": not truncated}, truncated
    finally:
        book.release_resources()


def _read(request):
    path, limit = Path(request["path"]), request["max_chars"]
    ext = path.suffix.lower()
    with path.open("rb") as stream:
        signature = stream.read(16).lstrip()
    # Word commonly uses an RTF payload under a .doc extension.
    if signature.startswith(b"{\\rtf") or ext == ".rtf":
        from striprtf.striprtf import rtf_to_text
        with path.open("rb") as stream:
            data = stream.read(request["max_bytes"]+1)
        truncated = len(data) > request["max_bytes"]
        data = data[:request["max_bytes"]]
        if not data.lstrip().startswith(b"{\\rtf"):
            raise ValueError("RTF 文件头无效")
        # Latin-1 is reversible for RTF's escaped bytes; ansicpg controls decoding.
        text = rtf_to_text(data.decode("latin-1"), errors="replace")
        metadata = {"content_complete": not truncated, "embedded_objects_ocr": False}
        parser = "striprtf"
    elif ext == ".xls":
        text, metadata, truncated = _xls(path, limit, request["preview"], request["refine"])
        parser = "xlrd"
    elif ext == ".ppt":
        from legacy_ppt import read_ppt
        text, metadata, truncated = read_ppt(path, limit, request["max_bytes"], request["preview"], request["refine"])
        parser = "ole-ppt-current-revision"
    else:
        text, metadata, truncated = _native(path, ext, limit, request["seconds"])
        parser = "antiword" if ext == ".doc" else "catppt"
    truncated = truncated or len(text) > limit
    metadata.update(source_bytes=path.stat().st_size, byte_budget=request["max_bytes"], preview_only=request["preview"], content_complete=bool(metadata.get("content_complete") and not truncated))
    warnings = []
    if truncated:
        warnings.append("旧格式正文达到本次读取或字符预算，当前结果为部分内容")
    if ext in {".doc", ".ppt", ".rtf"}:
        warnings.append("已提取文本和表格文字；嵌入图片、对象与批注不保证完整覆盖")
    if not text.strip():
        raise ValueError("旧格式文件未获得可读正文；可能为空、加密或只含图片")
    return {"text": text[:limit].strip(), "parser": parser, "metadata": metadata,
            "warnings": warnings, "char_count": len(text), "truncated": truncated}


if __name__ == "__main__":
    request = json.loads(sys.argv[2])
    initialize_process_family()
    gate = Path(request["start_gate"])
    until = time.monotonic() + 30
    while not gate.exists():
        if time.monotonic() >= until:
            sys.exit(1)
        time.sleep(.01)
    if os.name != "nt":
        import resource
        if os.getppid() != request['parent_pid']:
            sys.exit(1)
        memory = _limit("SJFX_LEGACY_MEMORY_MIB", 768, 128, 2048) * 1024**2
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
        cpu = max(1, int(request["seconds"])+1)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    try:
        result = _read(request)
    except Exception as exc:
        result = _empty("旧格式解析失败：" + str(exc)[:300])
    Path(sys.argv[3]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
