"""Bounded first-pass exploration for unknown data packages.

The explorer deliberately does not run the full document parser.  It reads a
small, deterministic sample from every file, builds a content map from those
samples and selects a diverse set of documents for accurate parsing.  The
source files remain authoritative and can be promoted later by a user query.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from services.scanner import human_size, is_sensitive_file
from services.unified_parser import (
    ARCHIVE_MAX_COMPRESSION_RATIO,
    ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES,
    _normalised_bounded_member_name,
    _zip_central_directory_preflight,
)


PREVIEW_SCHEMA = "file-preview/1.0"
CONTENT_MAP_SCHEMA = "package-content-map/1.0"

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".yaml", ".yml", ".log", ".ini", ".cfg",
    ".sql", ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".hpp",
}
OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".xlsm"}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".rar", ".7z"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

DOCUMENT_TYPES = {
    ".pdf": "PDF文档", ".doc": "文字文档", ".docx": "文字文档",
    ".ppt": "演示文稿", ".pptx": "演示文稿", ".xls": "数据表",
    ".xlsx": "数据表", ".xlsm": "数据表", ".csv": "数据表",
    ".tsv": "数据表", ".json": "结构化数据", ".jsonl": "结构化数据",
    ".xml": "结构化数据", ".eml": "信件", ".msg": "信件",
    ".mbox": "信件", ".pst": "邮件归档", ".txt": "文本",
    ".md": "文本", ".markdown": "文本", ".html": "网页",
    ".htm": "网页", ".zip": "压缩包", ".tar": "压缩包",
    ".gz": "压缩包", ".tgz": "压缩包", ".rar": "压缩包",
    ".7z": "压缩包", ".png": "图片", ".jpg": "图片",
    ".jpeg": "图片", ".tif": "图片", ".tiff": "图片",
    ".bmp": "图片", ".webp": "图片",
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,10}")
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
ORG_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9·&（）()\-]{2,40}(?:公司|集团|大学|学院|研究院|研究所|委员会|办公室|机构|部门|银行|基金会|协会|政府|中心)"
)
DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}(?:[-/.年](?:0?[1-9]|1[0-2])(?:[-/.月](?:0?[1-9]|[12]\d|3[01])日?)?)?\b"
)
STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "are", "was", "were",
    "have", "has", "into", "your", "file", "document", "data", "www", "http", "https",
    "一个", "这些", "这个", "以及", "进行", "相关", "文件", "资料", "内容", "可以", "需要",
    "我们", "他们", "其中", "通过", "对于", "没有", "已经", "主要", "情况", "工作",
}


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class PreviewBudget:
    """Thread-safe byte budget shared by one exploration slice."""

    def __init__(self, total_bytes):
        self.total_bytes = max(0, int(total_bytes or 0))
        self.consumed_bytes = 0
        self._lock = threading.Lock()

    def claim(self, requested):
        requested = max(0, int(requested or 0))
        with self._lock:
            if self.total_bytes == 0:
                return requested
            available = max(0, self.total_bytes - self.consumed_bytes)
            granted = min(requested, available)
            self.consumed_bytes += granted
            return granted

    @property
    def exhausted(self):
        return bool(self.total_bytes and self.consumed_bytes >= self.total_bytes)


def _decode_bytes(payload):
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return payload.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace"), "utf-8-replacement"


def _sample_windows(path, byte_limit, budget=None):
    path = Path(path)
    size = int(path.stat().st_size)
    requested = min(size, max(0, int(byte_limit or 0)))
    granted = budget.claim(requested) if budget is not None else requested
    if granted <= 0:
        return b"", [], True
    window = max(1, int(math.ceil(granted / 3.0)))
    offsets = [0]
    if size > window:
        offsets.extend([max(0, size // 2 - window // 2), max(0, size - window)])
    offsets = sorted(set(offsets))
    chunks = []
    ranges = []
    before = path.stat()
    with path.open("rb", buffering=0) as handle:
        remaining = granted
        for offset in offsets:
            if remaining <= 0:
                break
            handle.seek(offset)
            chunk = handle.read(min(window, remaining))
            if chunk:
                chunks.append(chunk)
                ranges.append([int(offset), int(offset + len(chunk))])
                remaining -= len(chunk)
    after = path.stat()
    before_sig = (int(before.st_dev), int(before.st_ino), int(before.st_size), int(before.st_mtime_ns))
    after_sig = (int(after.st_dev), int(after.st_ino), int(after.st_size), int(after.st_mtime_ns))
    if before_sig != after_sig:
        raise RuntimeError("源文件在轻量预览期间发生变化")
    return b"\n\n".join(chunks), ranges, granted < requested


def _bounded_zip_members(path, member_limit, member_bytes, budget=None, total_bytes=None):
    snippets = []
    names = []
    consumed = 0
    central_directory = _zip_central_directory_preflight(path)
    if not central_directory.get("safe"):
        raise ValueError(
            "ZIP 中央目录预检失败：{}".format(
                central_directory.get("reason") or "central_directory_invalid"
            )
        )
    with zipfile.ZipFile(str(path)) as archive:
        # ZipFile already materialises one bounded list after preflight. Avoid
        # a second full central-directory copy in the lightweight path.
        infos = archive.filelist
        observed_entries = int(central_directory.get("observed_entries") or 0)
        if (
            len(infos) != observed_entries
            or len(infos) > ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES
        ):
            raise ValueError("ZIP 中央目录在预检后发生变化")
        total_bytes = max(0, int(total_bytes if total_bytes is not None else member_limit * member_bytes))
        for info in infos[: max(1, int(member_limit))]:
            name = _normalised_bounded_member_name(info.filename)
            if name is None:
                continue
            names.append(name)
            if info.is_dir() or info.file_size <= 0:
                continue
            if info.flag_bits & 0x1 or is_sensitive_file(PurePosixPath(name).name):
                continue
            compressed_size = int(info.compress_size or 0)
            compression_ratio = (
                float("inf")
                if int(info.file_size) > 0 and compressed_size <= 0
                else int(info.file_size) / float(compressed_size or 1)
            )
            if compression_ratio > ARCHIVE_MAX_COMPRESSION_RATIO:
                continue
            lowered = name.lower()
            interesting = lowered.endswith((
                ".xml", ".txt", ".csv", ".json", ".md", ".html", ".rels",
            ))
            if not interesting or member_bytes <= 0:
                continue
            remaining = max(0, total_bytes - consumed)
            if remaining <= 0:
                break
            if info.file_size > max(member_bytes * 32, 64 * 1024 * 1024):
                continue
            requested = min(max(0, int(member_bytes)), int(info.file_size), remaining)
            granted = budget.claim(requested) if budget is not None else requested
            if granted <= 0:
                break
            with archive.open(info, "r") as handle:
                blob = handle.read(granted)
            text, _encoding = _decode_bytes(blob)
            snippets.append("[{}]\n{}".format(name, re.sub(r"<[^>]{1,200}>", " ", text)))
            consumed += len(blob)
    return "\n\n".join(snippets), names, consumed


def detect_language(text):
    text = str(text or "")[:20000]
    counts = {
        "zh": len(re.findall(r"[\u4e00-\u9fff]", text)),
        "ja": len(re.findall(r"[\u3040-\u30ff]", text)),
        "ko": len(re.findall(r"[\uac00-\ud7af]", text)),
        "ru": len(re.findall(r"[\u0400-\u04ff]", text)),
        "ar": len(re.findall(r"[\u0600-\u06ff]", text)),
        "en": len(re.findall(r"[A-Za-z]", text)),
    }
    total = sum(counts.values())
    if total < 8:
        return {"code": "unknown", "name": "未知", "confidence": 0.0, "counts": counts}
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    primary, primary_count = ordered[0]
    secondary_count = ordered[1][1]
    code = "mixed" if secondary_count >= max(8, primary_count * 0.45) else primary
    names = {"zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文", "ru": "俄文", "ar": "阿拉伯文", "mixed": "混合语言"}
    return {
        "code": code,
        "name": names.get(code, code),
        "confidence": round(primary_count / float(total), 4),
        "primary_code": primary,
        "counts": counts,
    }


def _keywords(text, limit=12):
    counter = Counter()
    for token in WORD_RE.findall(str(text or "").lower()):
        if token in STOPWORDS or token.isdigit():
            continue
        counter[token] += 1
    return [item for item, _count in sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]]


def _document_type(path):
    path = Path(path)
    extension = path.suffix.lower()
    name = path.name.lower()
    if any(word in name for word in ("contract", "agreement", "合同", "协议")):
        return "合同"
    if any(word in name for word in ("letter", "mail", "邮件", "信件")):
        return "信件"
    if any(word in name for word in ("report", "报告", "汇报")):
        return "报告"
    return DOCUMENT_TYPES.get(extension, "其他文件")


def preview_file(root, file_node, per_file_bytes=96 * 1024, budget=None,
                 zip_member_limit=80, zip_member_bytes=8192):
    """Create a bounded preview without copying or hashing the complete file."""
    root = Path(root).resolve()
    relative_path = str(file_node.get("path") or "").replace("\\", "/")
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("文件路径超出数据包根目录") from exc
    extension = path.suffix.lower()
    size = int(file_node.get("size") or path.stat().st_size)
    warning = []
    sampled_ranges = []
    sampled_bytes = 0
    member_names = []
    encoding = None
    text = ""
    status = "previewed"

    if is_sensitive_file(path.name):
        status = "restricted"
        warning.append("敏感文件仅登记元数据，未读取正文。")
    elif budget is not None and budget.exhausted:
        status = "deferred"
        warning.append("本轮轻量读取预算已用尽，文件保留为后续可恢复任务。")
    else:
        try:
            if extension in OFFICE_EXTENSIONS or extension == ".zip":
                text, member_names, sampled_bytes = _bounded_zip_members(
                    path, zip_member_limit, zip_member_bytes, budget=budget,
                    total_bytes=per_file_bytes,
                )
                text = text[: max(1000, int(per_file_bytes))]
            elif extension in TEXT_EXTENSIONS or extension in {".eml", ".mbox"}:
                blob, sampled_ranges, limited = _sample_windows(path, per_file_bytes, budget=budget)
                sampled_bytes = sum(end - start for start, end in sampled_ranges)
                text, encoding = _decode_bytes(blob)
                if limited:
                    warning.append("本轮全局轻量读取预算不足，当前文件只读取了部分样本。")
            elif extension == ".pdf":
                # PDF page extraction is intentionally deferred to selected
                # representatives; the bounded binary windows can still expose
                # title/producer metadata and stable duplicate signatures.
                blob, sampled_ranges, limited = _sample_windows(path, min(per_file_bytes, 64 * 1024), budget=budget)
                sampled_bytes = sum(end - start for start, end in sampled_ranges)
                metadata_strings = re.findall(rb"/(?:Title|Author|Subject|Keywords)\s*\(([^)]{1,500})\)", blob)
                text = "\n".join(_decode_bytes(item)[0] for item in metadata_strings)
                if not text:
                    warning.append("PDF 正文将在代表文件深析或用户命中后按代表页/全文解析。")
                if limited:
                    warning.append("本轮全局轻量读取预算不足。")
            elif extension in ARCHIVE_EXTENSIONS:
                blob, sampled_ranges, limited = _sample_windows(path, min(per_file_bytes, 32 * 1024), budget=budget)
                sampled_bytes = sum(end - start for start, end in sampled_ranges)
                if extension == ".zip":
                    _unused, member_names, _used = _bounded_zip_members(
                        path, zip_member_limit, 0, budget=None, total_bytes=0,
                    )
                text = "\n".join(member_names)
                warning.append("压缩包首轮只读取成员目录，不解压正文。")
                if limited:
                    warning.append("本轮全局轻量读取预算不足。")
            else:
                blob, sampled_ranges, limited = _sample_windows(path, min(per_file_bytes, 32 * 1024), budget=budget)
                sampled_bytes = sum(end - start for start, end in sampled_ranges)
                if extension not in IMAGE_EXTENSIONS:
                    decoded, encoding = _decode_bytes(blob)
                    printable = sum(character.isprintable() for character in decoded)
                    if decoded and printable / float(len(decoded)) >= 0.75:
                        text = decoded
                if extension in IMAGE_EXTENSIONS:
                    warning.append("图片首轮登记元数据；OCR 在代表文件或用户命中后执行。")
                if limited:
                    warning.append("本轮全局轻量读取预算不足。")
        except (OSError, RuntimeError, ValueError, EOFError, zipfile.BadZipFile) as exc:
            status = "failed"
            warning.append("轻量预览失败：{}".format(str(exc)[:300]))

    # Window separators are not source bytes and may add a handful of
    # characters.  Keep that tiny allowance so the tail sample is not cut off.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)[
        : max(1000, int(per_file_bytes) + 32)
    ]
    content_sample_material = json.dumps({
        "size": size,
        "ranges": sampled_ranges,
        "text": text,
        "members": member_names[:200],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    content_sample_sha256 = hashlib.sha256(content_sample_material).hexdigest()
    preview_fingerprint = hashlib.sha256(json.dumps({
        "path": relative_path,
        "size": size,
        "modified_at_ns": int(file_node.get("modified_at_ns") or 0),
        "content_sample_sha256": content_sample_sha256,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    language = detect_language(text)
    organizations = sorted(set(match.group(0)[:80] for match in ORG_RE.finditer(text)))[:20]
    emails = sorted(set(EMAIL_RE.findall(text)))[:20]
    dates = sorted(set(DATE_RE.findall(text)))[:20]
    return {
        "schema_version": PREVIEW_SCHEMA,
        "path": relative_path,
        "name": path.name,
        "extension": extension,
        "size": size,
        "size_human": human_size(size),
        "modified_at": file_node.get("modified_at"),
        "modified_at_ns": int(file_node.get("modified_at_ns") or 0),
        "document_type": _document_type(path),
        "status": status,
        "preview_text": text,
        "preview_characters": len(text),
        "sampled_bytes": sampled_bytes,
        "sampled_ranges": sampled_ranges,
        "sample_sha256": content_sample_sha256,
        "content_sample_sha256": content_sample_sha256,
        "preview_fingerprint": preview_fingerprint,
        "encoding": encoding,
        "language": language,
        "keywords": _keywords("{} {}".format(path.stem, text)),
        "entities": {"organizations": organizations, "email_addresses": emails},
        "dates": dates,
        "archive_members": member_names[:200],
        "warnings": warning,
        "previewed_at": _utc_now(),
        "coverage": {
            "inventory_complete": True,
            "preview_only": True,
            "parse_complete": False,
            "semantic_complete": False,
            "sampled_bytes": sampled_bytes,
            "source_bytes": size,
        },
    }


def preview_as_document(preview):
    """Project a preview into the existing unified-document contract."""
    text = str(preview.get("preview_text") or "")
    path = str(preview.get("path") or "")
    evidence = []
    if text.strip():
        evidence.append({
            "evidence_id": "P-{}".format(
                (preview.get("preview_fingerprint") or preview.get("sample_sha256") or "")[:16]
            ),
            "source_path": path,
            "label": "bounded_preview",
            "text": text[:900],
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "preview_only": True,
        })
    return {
        "schema_version": "unified-document/1.0",
        "source": {
            "path": path,
            "name": preview.get("name"),
            "extension": preview.get("extension"),
            "size": preview.get("size"),
            "modified_at": preview.get("modified_at"),
            "sample_sha256": preview.get("sample_sha256"),
            "sha256": "",
        },
        "parsed_at": preview.get("previewed_at"),
        "parser": {"name": "bounded-package-preview", "mode": "preview", "degraded": False},
        "structure": {"title": Path(path).stem, "headings": [], "page_count": None, "table_count": 0, "picture_count": 0},
        "text": text,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "coverage": dict(preview.get("coverage") or {}),
        "evidence": evidence,
        "warnings": list(preview.get("warnings") or []),
        "classification": {
            "document_type": preview.get("document_type"),
            "language": preview.get("language"),
            "preview_keywords": preview.get("keywords") or [],
            "preview_entities": preview.get("entities") or {},
        },
        "preview": {key: value for key, value in preview.items() if key != "preview_text"},
    }


def _representative_score(preview, topic_frequency, type_frequency, language_frequency,
                          duplicate_frequency):
    keywords = list(preview.get("keywords") or [])
    unique_topic = sum(1.0 / max(1, topic_frequency[item]) for item in keywords[:8])
    doc_type = str(preview.get("document_type") or "其他文件")
    language = str((preview.get("language") or {}).get("code") or "unknown")
    digest = str(preview.get("sample_sha256") or "")
    uniqueness = 1.0 / max(1, duplicate_frequency[digest])
    information = min(
        1.0,
        int(preview.get("preview_characters") or len(str(preview.get("preview_text") or ""))) / 4000.0,
    )
    entity_count = sum(len(values or []) for values in (preview.get("entities") or {}).values())
    return round(
        2.2 * unique_topic
        + 1.4 / max(1, type_frequency[doc_type])
        + 1.0 / max(1, language_frequency[language])
        + 1.5 * uniqueness
        + 1.2 * information
        + min(1.5, entity_count * 0.15),
        6,
    )


def build_content_map(previews, representative_limit=700, relation_limit=1200):
    """Aggregate bounded previews without retaining or loading full text."""
    # Keep only bounded analytical fields.  In particular, never retain every
    # preview body while aggregating tens of thousands of files.
    compact_previews = []
    for source in previews:
        if not source or not source.get("path"):
            continue
        compact_previews.append({
            "path": source.get("path"),
            "extension": source.get("extension"),
            "size": source.get("size"),
            "status": source.get("status"),
            "document_type": source.get("document_type"),
            "language": dict(source.get("language") or {}),
            "keywords": list(source.get("keywords") or [])[:20],
            "entities": {
                key: list(values or [])[:20]
                for key, values in (source.get("entities") or {}).items()
            },
            "sample_sha256": source.get("content_sample_sha256") or source.get("sample_sha256"),
            "preview_characters": int(
                source.get("preview_characters")
                or len(str(source.get("preview_text") or ""))
            ),
        })
    previews = compact_previews
    representative_limit = max(1, int(representative_limit or 1))
    type_frequency = Counter(str(item.get("document_type") or "其他文件") for item in previews)
    language_frequency = Counter(str((item.get("language") or {}).get("code") or "unknown") for item in previews)
    extension_frequency = Counter(str(item.get("extension") or "[无扩展名]") for item in previews)
    topic_frequency = Counter(topic for item in previews for topic in item.get("keywords") or [])
    duplicate_frequency = Counter(str(item.get("sample_sha256") or "") for item in previews)
    directory_frequency = Counter(
        str(PurePosixPath(str(item.get("path"))).parent) for item in previews
    )
    statuses = Counter(str(item.get("status") or "unknown") for item in previews)
    total_bytes = sum(int(item.get("size") or 0) for item in previews)

    scored = []
    for item in previews:
        score = _representative_score(
            item, topic_frequency, type_frequency, language_frequency, duplicate_frequency,
        )
        scored.append((score, str(item.get("path")), item))
    scored.sort(key=lambda row: (-row[0], row[1]))

    selected = []
    selected_paths = set()

    def add(item, reason):
        path = str(item.get("path") or "")
        if (
            not path
            or item.get("status") != "previewed"
            or path in selected_paths
            or len(selected) >= representative_limit
        ):
            return
        selected_paths.add(path)
        selected.append({"path": path, "score": next((row[0] for row in scored if row[1] == path), 0), "reason": reason})

    # Guarantee structural coverage before filling by score.
    dimensions = (
        (lambda item: str(item.get("document_type") or "其他文件"), "文档类型代表"),
        (lambda item: str((item.get("language") or {}).get("code") or "unknown"), "语言代表"),
        (lambda item: str(item.get("extension") or "[无扩展名]"), "格式代表"),
        (lambda item: str(PurePosixPath(str(item.get("path"))).parent), "目录代表"),
    )
    for key_fn, reason in dimensions:
        seen = set()
        for _score, _path, item in scored:
            key = key_fn(item)
            if key not in seen:
                add(item, reason)
                seen.add(key)
    for _score, _path, item in scored:
        add(item, "信息量、独特性与关系价值综合得分")

    duplicates = []
    by_digest = defaultdict(list)
    for item in previews:
        by_digest[str(item.get("sample_sha256") or "")].append(str(item.get("path")))
    for digest, paths in sorted(by_digest.items()):
        if digest and len(paths) > 1:
            duplicates.append({"sample_sha256": digest, "paths": sorted(paths), "file_count": len(paths), "kind": "sample_candidate"})

    feature_paths = defaultdict(list)
    for item in previews:
        path = str(item.get("path"))
        features = [("topic", value) for value in (item.get("keywords") or [])[:6]]
        features += [("organization", value) for value in (item.get("entities") or {}).get("organizations", [])[:6]]
        for feature in features:
            if len(feature_paths[feature]) < 80:
                feature_paths[feature].append(path)
    pair_features = defaultdict(list)
    for feature, paths in feature_paths.items():
        ordered = sorted(set(paths))
        # Neighbour links keep the graph bounded while preserving connected
        # feature groups; hubs emerge from repeated shared features.
        for index in range(len(ordered) - 1):
            pair_features[(ordered[index], ordered[index + 1])].append(feature)
    relationships = []
    for (left, right), features in sorted(pair_features.items(), key=lambda item: (-len(item[1]), item[0]))[: max(0, int(relation_limit))]:
        relationships.append({
            "source": left,
            "target": right,
            "weight": len(features),
            "reasons": [{"kind": kind, "value": value} for kind, value in features[:8]],
            "status": "preview_inferred",
        })

    path_degree = Counter()
    for relation in relationships:
        path_degree[relation["source"]] += int(relation["weight"])
        path_degree[relation["target"]] += int(relation["weight"])
    isolated = sorted(
        str(item.get("path")) for item in previews
        if path_degree[str(item.get("path"))] == 0 and duplicate_frequency[str(item.get("sample_sha256") or "")] == 1
    )

    return {
        "schema_version": CONTENT_MAP_SCHEMA,
        "generated_at": _utc_now(),
        "inventory": {
            "file_count": len(previews),
            "total_bytes": total_bytes,
            "total_size_human": human_size(total_bytes),
            "status_counts": dict(sorted(statuses.items())),
        },
        "formats": [{"extension": key, "file_count": value} for key, value in extension_frequency.most_common()],
        "document_types": [{"type": key, "file_count": value} for key, value in type_frequency.most_common()],
        "languages": [{"code": key, "file_count": value} for key, value in language_frequency.most_common()],
        "directories": [{"path": key, "file_count": value} for key, value in directory_frequency.most_common(500)],
        "topics": [{"name": key, "file_count": value} for key, value in topic_frequency.most_common(200)],
        "duplicates": duplicates[:500],
        "relationships": relationships,
        "isolated_paths": isolated[:500],
        "representatives": selected,
        "representative_paths": [item["path"] for item in selected],
        "coverage": {
            "previewed_files": statuses.get("previewed", 0),
            "restricted_files": statuses.get("restricted", 0),
            "deferred_files": statuses.get("deferred", 0),
            "failed_files": statuses.get("failed", 0),
            "all_inventory_accounted": sum(statuses.values()) == len(previews),
            "relationship_status": "preview_inferred_requires_deep_validation",
            "duplicate_status": "sample_candidates_require_full_hash_validation",
        },
    }


def promotion_paths(content_map, requested_paths=None, limit=None):
    requested = [str(path) for path in (requested_paths or []) if path]
    representatives = [str(path) for path in (content_map or {}).get("representative_paths") or []]
    ordered = list(dict.fromkeys(requested + representatives))
    if limit is None:
        return ordered
    return ordered[: max(0, int(limit))]
