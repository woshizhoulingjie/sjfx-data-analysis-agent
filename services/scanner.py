import csv
import hashlib
import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path


TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".log", ".ini", ".cfg", ".yaml", ".yml",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".sql",
}
IGNORED_DIRS = {
    ".git", ".venv", ".venv_py37_unused", "__pycache__", "node_modules",
    ".idea", ".vscode", "vendor_packages", "work",
}
IGNORED_FILES = {".env", "agent.db", "agent.db-shm", "agent.db-wal"}
SENSITIVE_EXTENSIONS = {".key", ".pem", ".p12", ".pfx", ".keystore"}


def should_ignore_file(name):
    lower_name = name.lower()
    return (
        lower_name in IGNORED_FILES
        or lower_name.startswith("~$")
        or Path(lower_name).suffix in SENSITIVE_EXTENSIONS
    )


def natural_key(value):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def path_id(path):
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def human_size(value):
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "{:.1f} {}".format(size, unit)
        size /= 1024


def resolve_under(root, requested):
    root_path = Path(root).resolve()
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError:
        raise ValueError("请求路径超出当前扫描根目录")
    return candidate


def _file_metadata(path, root):
    stat = path.stat()
    mime, _ = mimetypes.guess_type(str(path))
    rel = str(path.relative_to(root)).replace("\\", "/")
    return {
        "id": path_id(path),
        "name": path.name,
        "path": rel,
        "kind": "file",
        "extension": path.suffix.lower(),
        "mime_type": mime or "application/octet-stream",
        "size": stat.st_size,
        "size_human": human_size(stat.st_size),
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
    }


def scan_directory(root, max_files=10000):
    root = Path(root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("目录不存在或不是文件夹")
    max_files = max(1, min(int(max_files), 50000))
    count = 0
    total_size = 0
    type_counts = {}
    errors = []
    truncated = False
    ignored_file_count = 0

    def walk(folder):
        nonlocal count, total_size, truncated, ignored_file_count
        node = {
            "id": path_id(folder),
            "name": folder.name or str(folder),
            "path": str(folder.relative_to(root)).replace("\\", "/") if folder != root else ".",
            "kind": "directory",
            "children": [],
            "file_count": 0,
            "direct_file_count": 0,
            "directory_count": 0,
            "direct_directory_count": 0,
            "total_size": 0,
            "type_counts": {},
        }
        try:
            entries = sorted(
                os.scandir(str(folder)),
                key=lambda e: (not e.is_dir(follow_symlinks=False), natural_key(e.name)),
            )
        except (OSError, PermissionError) as exc:
            errors.append({"path": str(folder), "error": str(exc)})
            return node
        for entry in entries:
            if entry.name in IGNORED_DIRS:
                continue
            if count >= max_files:
                truncated = True
                break
            try:
                if entry.is_symlink():
                    continue
                item_path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    child = walk(item_path)
                    node["children"].append(child)
                    node["direct_directory_count"] += 1
                    node["directory_count"] += 1 + child["directory_count"]
                    node["file_count"] += child["file_count"]
                    node["total_size"] += child["total_size"]
                    for ext, value in child["type_counts"].items():
                        node["type_counts"][ext] = node["type_counts"].get(ext, 0) + value
                elif entry.is_file(follow_symlinks=False):
                    if should_ignore_file(entry.name):
                        ignored_file_count += 1
                        continue
                    meta = _file_metadata(item_path, root)
                    node["children"].append(meta)
                    count += 1
                    total_size += meta["size"]
                    node["file_count"] += 1
                    node["direct_file_count"] += 1
                    node["total_size"] += meta["size"]
                    ext = meta["extension"] or "[无扩展名]"
                    type_counts[ext] = type_counts.get(ext, 0) + 1
                    node["type_counts"][ext] = node["type_counts"].get(ext, 0) + 1
            except (OSError, PermissionError) as exc:
                errors.append({"path": entry.path, "error": str(exc)})
        node["size_human"] = human_size(node["total_size"])
        node["type_counts"] = dict(sorted(node["type_counts"].items(), key=lambda item: (-item[1], item[0])))
        top_types = list(node["type_counts"].items())[:4]
        if node["file_count"]:
            type_text = "、".join("{} {}个".format(ext, value) for ext, value in top_types)
            node["simple_summary"] = (
                "本文件夹当前层有 {direct_files} 个文件、{direct_dirs} 个子目录；"
                "递归范围共 {files} 个文件、{dirs} 个目录，总大小 {size}。主要类型：{types}。"
            ).format(
                direct_files=node["direct_file_count"], direct_dirs=node["direct_directory_count"],
                files=node["file_count"], dirs=node["directory_count"], size=node["size_human"],
                types=type_text or "无扩展名统计",
            )
        else:
            node["simple_summary"] = "本文件夹当前为空，未发现可分析文件。"
        return node

    tree = walk(root)
    return {
        "root": str(root),
        "scanned_at": now_iso(),
        "file_count": count,
        "directory_count": tree["directory_count"],
        "ignored_file_count": ignored_file_count,
        "total_size": total_size,
        "total_size_human": human_size(total_size),
        "type_counts": dict(sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))),
        "truncated": truncated,
        "errors": errors[:100],
        "tree": tree,
    }


def _decode_bytes(data):
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def extract_text(path, max_chars=60000):
    path = Path(path)
    ext = path.suffix.lower()
    text = ""
    parser = "unsupported"
    warnings = []
    metadata = {}
    try:
        if ext in TEXT_EXTENSIONS:
            with path.open("rb") as stream:
                raw = stream.read(max_chars * 4 + 1)
            text = _decode_bytes(raw[: max_chars * 4])
            if len(raw) > max_chars * 4:
                warnings.append("文本文件超过本地完整读取上限")
            parser = "text"
        elif ext == ".pdf":
            try:
                try:
                    from pypdf import PdfReader
                except ImportError:
                    from PyPDF2 import PdfReader
                reader = PdfReader(str(path))
                metadata["page_count"] = len(reader.pages)
                parts = []
                extracted_pages = 0
                empty_pages = 0
                accumulated = 0
                for page_number, page in enumerate(reader.pages, 1):
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        extracted_pages += 1
                    else:
                        empty_pages += 1
                    section = "[第 {} 页]\n{}".format(page_number, page_text)
                    parts.append(section)
                    accumulated += len(section)
                    if accumulated >= max_chars:
                        break
                text = "\n\n".join(parts)
                metadata["processed_pages"] = len(parts)
                metadata["text_pages"] = extracted_pages
                metadata["empty_text_pages"] = empty_pages
                metadata["pages_omitted_by_limit"] = max(0, len(reader.pages) - len(parts))
                parser = "PyPDF2"
            except ImportError:
                warnings.append("未安装 PyPDF2，当前只能读取文件元数据")
        elif ext == ".docx":
            try:
                from docx import Document
                doc = Document(str(path))
                text = "\n".join(p.text for p in doc.paragraphs)
                metadata["paragraph_count"] = len(doc.paragraphs)
                metadata["table_count"] = len(doc.tables)
                parser = "python-docx"
            except ImportError:
                warnings.append("未安装 python-docx，当前只能读取文件元数据")
        elif ext == ".pptx":
            try:
                from pptx import Presentation
                presentation = Presentation(str(path))
                parts = []
                for index, slide in enumerate(presentation.slides, 1):
                    slide_text = []
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text:
                            slide_text.append(shape.text)
                    parts.append("[幻灯片 {}]\n{}".format(index, "\n".join(slide_text)))
                text = "\n\n".join(parts)
                metadata["slide_count"] = len(presentation.slides)
                parser = "python-pptx"
            except ImportError:
                warnings.append("未安装 python-pptx，当前只能读取文件元数据")
        elif ext in (".xlsx", ".xlsm"):
            try:
                from openpyxl import load_workbook
                workbook = load_workbook(str(path), read_only=True, data_only=True)
                parts = []
                accumulated = 0
                stopped = False
                for sheet in workbook.worksheets:
                    parts.append("[工作表 {}]".format(sheet.title))
                    for row in sheet.iter_rows(values_only=True):
                        row_text = "\t".join("" if value is None else str(value) for value in row)
                        parts.append(row_text)
                        accumulated += len(row_text)
                        if accumulated >= max_chars:
                            stopped = True
                            break
                    if stopped:
                        break
                text = "\n".join(parts)
                metadata["sheet_count"] = len(workbook.worksheets)
                parser = "openpyxl"
            except ImportError:
                warnings.append("未安装 openpyxl，当前只能读取文件元数据")
        else:
            warnings.append("该文件类型尚未配置正文解析器")
    except Exception as exc:
        warnings.append("解析失败：{}".format(exc))
    original_length = len(text)
    if original_length > max_chars:
        text = text[:max_chars]
        warnings.append("正文已截断：原始提取字符数 {}".format(original_length))
    return {
        "text": text.strip(),
        "parser": parser,
        "warnings": warnings,
        "metadata": metadata,
        "char_count": original_length,
        "truncated": original_length > max_chars,
    }


def folder_context(folder, root, max_files=30, max_chars=50000):
    folder = Path(folder)
    root = Path(root)
    inventory = []
    excerpts = []
    sampled = 0
    total_files = 0
    total_dirs = 0
    total_size = 0
    type_counts = {}
    candidates = []
    for current_root, dirs, files in os.walk(str(folder)):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        total_dirs += len(dirs)
        for name in sorted(files, key=natural_key):
            if should_ignore_file(name):
                continue
            path = Path(current_root) / name
            try:
                meta = _file_metadata(path, root)
                total_files += 1
                total_size += meta["size"]
                ext = meta["extension"] or "[无扩展名]"
                type_counts[ext] = type_counts.get(ext, 0) + 1
                candidates.append((path, meta))
            except (OSError, PermissionError):
                continue
    if len(candidates) <= max_files:
        selected = candidates
    elif max_files <= 1:
        selected = candidates[:1]
    else:
        indices = sorted(set(int(round(index * (len(candidates) - 1) / float(max_files - 1))) for index in range(max_files)))
        selected = [candidates[index] for index in indices]
    documents = []
    remaining_chars = max_chars
    per_file_budget = min(6000, max(800, max_chars // max(1, len(selected))))
    for path, meta in selected:
        if remaining_chars <= 0:
            break
        per_file_limit = min(per_file_budget, remaining_chars)
        extracted = extract_text(path, max_chars=per_file_limit)
        text_sample = extracted["text"][:per_file_limit]
        inventory.append({"path": meta["path"], "size": meta["size"], "extension": meta["extension"]})
        documents.append({
            "path": meta["path"], "extension": meta["extension"], "text": text_sample,
            "parser": extracted["parser"], "warnings": extracted["warnings"],
        })
        if text_sample:
            excerpt = "### {}\n{}".format(meta["path"], text_sample)
            excerpts.append(excerpt)
            remaining_chars -= len(excerpt)
        sampled += 1
    joined = "\n\n".join(excerpts)
    return {
        "inventory": inventory,
        "excerpts": joined[:max_chars],
        "sampled_files": sampled,
        "total_files": total_files,
        "total_dirs": total_dirs,
        "total_size": total_size,
        "total_size_human": human_size(total_size),
        "type_counts": dict(sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))),
        "sample_truncated": total_files > sampled,
        "documents": documents,
    }
