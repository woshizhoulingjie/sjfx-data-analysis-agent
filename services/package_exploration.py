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


PREVIEW_SCHEMA = "file-preview/1.4"
CONTENT_MAP_SCHEMA = "package-content-map/3.0"

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".yaml", ".yml", ".log", ".ini", ".cfg",
    ".sql", ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".hpp",
}
OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".xlsm"}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".rar", ".7z"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
OUT_OF_SCOPE_MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".m4v",
}
DEEP_PARSE_EXTENSIONS = (
    TEXT_EXTENSIONS | OFFICE_EXTENSIONS | ARCHIVE_EXTENSIONS | IMAGE_EXTENSIONS
    | {".pdf", ".doc", ".ppt", ".xls", ".eml", ".msg", ".mbox", ".pst"}
)
LOW_VALUE_NAMES = {
    "thumbs.db", "desktop.ini", ".ds_store", "npm-debug.log", "yarn-error.log",
}
LOW_VALUE_EXTENSIONS = {
    ".pyc", ".pyo", ".class", ".o", ".obj", ".so", ".dll", ".dylib",
    ".tmp", ".temp", ".cache", ".lock",
}

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

# A large package needs a stable, explainable taxonomy before any model call.
# These categories are deliberately coarse: they are navigation buckets and
# research leads, not claims about the whole package.  The model may refine a
# selected bucket later, but it must not be responsible for discovering every
# category in a 100 GB input.
CONTENT_CATEGORY_DEFINITIONS = (
    {
        "category_id": "security_risk",
        "name": "安全风险与漏洞",
        "description": "漏洞、攻击面、恶意行为、补丁和风险处置资料。",
        "terms": (
            "security", "vulnerability", "vulnerabilities", "cve", "cvss",
            "cwe", "exploit", "attack", "malware", "threat", "incident",
            "patch", "漏洞", "安全", "风险", "攻击", "恶意", "补丁",
        ),
        "extensions": (),
        "document_types": (),
        "priority": 100,
        "research_value": "优先核对风险影响范围、受影响对象和修复优先级。",
        "research_questions": (
            "哪些风险有直接原文证据，影响范围是否被低估？",
            "哪些漏洞、事件或补丁之间存在时间或对象上的关联？",
        ),
    },
    {
        "category_id": "operations_events",
        "name": "运行日志与事件",
        "description": "系统日志、审计记录、运行事件、异常和时间线资料。",
        "terms": (
            "log", "logs", "event", "events", "error", "warning", "audit",
            "incident", "trace", "exception", "timestamp", "access", "日志",
            "事件", "异常", "审计", "运行", "访问", "时间线",
        ),
        "extensions": (".log",),
        "document_types": (),
        "priority": 90,
        "research_value": "适合还原事件时间线、异常模式和需要进一步核查的操作。",
        "research_questions": (
            "关键事件按时间如何演化，是否存在集中异常窗口？",
            "哪些错误、访问或审计记录值得关联调查？",
        ),
    },
    {
        "category_id": "business_records",
        "name": "业务与交易记录",
        "description": "财务、订单、客户、合同履行和业务过程数据。",
        "terms": (
            "invoice", "payment", "finance", "budget", "transaction", "revenue",
            "sales", "customer", "order", "account", "contract", "business",
            "发票", "付款", "财务", "预算", "交易", "收入", "销售", "客户",
            "订单", "账户", "合同", "业务",
        ),
        "extensions": (),
        "document_types": ("合同",),
        "priority": 80,
        "research_value": "适合核对业务规模、交易关系、责任边界和异常记录。",
        "research_questions": (
            "数据中有哪些业务主体、交易链条或金额异常？",
            "合同或业务记录能否支持明确的责任与履行结论？",
        ),
    },
    {
        "category_id": "people_organization",
        "name": "人员与组织信息",
        "description": "人员、联系人、组织、角色和通信关系资料。",
        "terms": (
            "employee", "personnel", "staff", "user", "contact", "author",
            "sender", "recipient", "member", "department", "人员", "联系人",
            "作者", "发件人", "收件人", "成员", "部门", "组织", "负责人",
        ),
        "extensions": (),
        "document_types": ("信件", "邮件归档"),
        "priority": 70,
        "research_value": "适合梳理角色、组织边界和资料来源关系。",
        "research_questions": (
            "哪些人员或组织在多个文件中反复出现，承担什么角色？",
            "通信、联系人或组织关系是否能形成可核验的事项链？",
        ),
    },
    {
        "category_id": "technical_project",
        "name": "项目与技术资料",
        "description": "项目说明、代码、接口、配置、部署和技术设计资料。",
        "terms": (
            "project", "readme", "api", "code", "deploy", "deployment", "config",
            "architecture", "specification", "schema", "database", "service",
            "项目", "技术", "接口", "代码", "部署", "配置", "架构", "数据库",
            "服务", "方案",
        ),
        "extensions": (".py", ".js", ".ts", ".java", ".sql", ".yaml", ".yml", ".md"),
        "document_types": (),
        "priority": 60,
        "research_value": "适合理解系统结构、数据流、依赖和实施风险。",
        "research_questions": (
            "系统或项目的核心组件、数据流和依赖关系是什么？",
            "哪些配置、接口或实现细节可能形成安全或运行风险？",
        ),
    },
    {
        "category_id": "research_reports",
        "name": "研究与报告",
        "description": "研究报告、论文、实验、分析和结论性文字资料。",
        "terms": (
            "research", "paper", "abstract", "methodology", "experiment", "result",
            "report", "analysis", "finding", "conclusion", "研究", "论文", "摘要",
            "方法", "实验", "结果", "报告", "分析", "发现", "结论",
        ),
        "extensions": (),
        "document_types": ("报告",),
        "priority": 55,
        "research_value": "适合核对报告结论、方法依据、数据局限和可复现实验。",
        "research_questions": (
            "报告的核心结论由哪些原文、数据或方法直接支撑？",
            "不同报告之间有哪些一致、冲突或需要复核的判断？",
        ),
    },
    {
        "category_id": "policy_compliance",
        "name": "法规制度与合规",
        "description": "政策、制度、法规、标准、协议和合规要求资料。",
        "terms": (
            "policy", "regulation", "compliance", "legal", "agreement", "standard",
            "law", "requirement", "governance", "制度", "法规", "合规", "法律",
            "协议", "标准", "要求", "治理", "规定",
        ),
        "extensions": (),
        "document_types": ("合同",),
        "priority": 50,
        "research_value": "适合核对适用规则、义务边界、例外条款和缺口。",
        "research_questions": (
            "哪些条款或制度要求能直接约束当前业务或项目？",
            "资料中是否存在要求与实际执行之间的缺口？",
        ),
    },
    {
        "category_id": "structured_data",
        "name": "结构化数据与指标",
        "description": "表格、JSON、CSV、数据库导出和可统计记录。",
        "terms": (
            "record", "records", "row", "column", "field", "value", "metric",
            "统计", "记录", "字段", "指标", "数据表",
        ),
        "extensions": (".csv", ".tsv", ".json", ".jsonl", ".xml", ".xlsx", ".xls", ".xlsm"),
        "document_types": ("数据表", "结构化数据"),
        "priority": 45,
        "research_value": "适合先做数据质量、时间范围、指标分布和异常值核查。",
        "research_questions": (
            "记录覆盖哪些时间、主体和指标，是否存在缺失或异常分布？",
            "哪些字段关系或统计结果值得进入后续深度分析？",
        ),
    },
    {
        "category_id": "media_scans",
        "name": "图片与扫描资料",
        "description": "图片、扫描件、OCR 候选和其他需要视觉解析的资料。",
        "terms": ("image", "scan", "ocr", "photo", "picture", "图片", "扫描", "照片", "影像"),
        "extensions": tuple(sorted(IMAGE_EXTENSIONS | {".pdf"})),
        "document_types": ("图片",),
        "priority": 30,
        "research_value": "适合从扫描件、图表和影像资料中补充文字证据。",
        "research_questions": (
            "哪些扫描件或图表可能包含尚未进入文字索引的关键证据？",
        ),
    },
    {
        "category_id": "general_documents",
        "name": "其他文档与资料",
        "description": "暂未命中明确主题规则的可解析文档，保留待人工筛选。",
        "terms": (),
        "extensions": (),
        "document_types": (),
        "priority": 1,
        "research_value": "适合按目录、文件名和代表性样本继续细分。",
        "research_questions": ("哪些未分类资料可能补充其他主题的关键证据？",),
    },
)

CONTENT_CATEGORY_BY_ID = {
    item["category_id"]: item for item in CONTENT_CATEGORY_DEFINITIONS
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,10}")
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
ORG_RE = re.compile(
    r"(?:[\u4e00-\u9fffA-Za-z0-9·&（）()\-]{2,40}"
    r"(?:公司|集团|大学|学院|研究院|研究所|委员会|办公室|机构|部门|银行|基金会|协会|政府|中心)"
    r"|\b(?:[A-Z][A-Za-z0-9&'.\-]{1,30}\s+){1,5}"
    r"(?:Company|Corporation|Corp\.?|Inc\.?|Ltd\.?|Limited|Group|University|Institute|"
    r"Committee|Association|Foundation|Bank|Agency|Department|Office|Center|Centre)\b)"
)
PERSON_PATTERNS = (
    re.compile(
        r"(?i)\b(?:from|to|cc|sender|recipient|author|by|signed(?:\s+by)?|contact)\s*[:：]?\s*"
        r"([A-Z][A-Za-z'\-]{1,30}(?:\s+[A-Z][A-Za-z'\-]{1,30}){1,3})\b"
    ),
    re.compile(
        r"(?:姓名|联系人|负责人|作者|发件人|收件人|签署人|签字人)\s*[:：]\s*"
        r"([\u4e00-\u9fff·]{2,12})"
    ),
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

    def __init__(self, total_bytes, consumed_bytes=0):
        self.total_bytes = max(0, int(total_bytes or 0))
        self.consumed_bytes = max(
            0, min(self.total_bytes, int(consumed_bytes or 0))
        ) if self.total_bytes else max(0, int(consumed_bytes or 0))
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


def _stream_source_sha256(path, cancel_check=None, yield_check=None,
                          chunk_bytes=4 * 1024 * 1024):
    """Hash one source with bounded memory and cooperative cancellation."""
    digest = hashlib.sha256()
    before = Path(path).stat()
    with Path(path).open("rb", buffering=0) as handle:
        while True:
            if cancel_check is not None and cancel_check():
                raise RuntimeError("任务已取消")
            chunk = handle.read(max(64 * 1024, int(chunk_bytes)))
            if not chunk:
                break
            digest.update(chunk)
            if yield_check is not None and yield_check():
                raise PreviewSliceYield("检测到更高优先级任务，已让出文件哈希")
    after = Path(path).stat()
    before_sig = (
        int(before.st_dev), int(before.st_ino), int(before.st_size), int(before.st_mtime_ns),
    )
    after_sig = (
        int(after.st_dev), int(after.st_ino), int(after.st_size), int(after.st_mtime_ns),
    )
    if before_sig != after_sig:
        raise RuntimeError("源文件在流式哈希期间发生变化")
    return digest.hexdigest()


def _sample_windows(path, byte_limit, budget=None):
    path = Path(path)
    size = int(path.stat().st_size)
    requested = min(size, max(0, int(byte_limit or 0)))
    granted = budget.claim(requested) if budget is not None else requested
    if granted <= 0:
        return b"", [], True, []
    if granted >= size:
        before = path.stat()
        with path.open("rb", buffering=0) as handle:
            payload = handle.read(size)
        after = path.stat()
        before_sig = (int(before.st_dev), int(before.st_ino), int(before.st_size), int(before.st_mtime_ns))
        after_sig = (int(after.st_dev), int(after.st_ino), int(after.st_size), int(after.st_mtime_ns))
        if before_sig != after_sig:
            raise RuntimeError("源文件在轻量预览期间发生变化")
        return payload, [[0, len(payload)]], False, [payload]
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
    return b"\n\n".join(chunks), ranges, granted < requested, chunks


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


def _preview_entities(text):
    """Extract conservative, bounded entity candidates from one preview."""
    value = str(text or "")[:200000]
    organizations = sorted({
        re.sub(r"\s+", " ", match.group(0)).strip()[:80]
        for match in ORG_RE.finditer(value)
        if match.group(0).strip()
    })[:20]
    people = set()
    for pattern in PERSON_PATTERNS:
        for match in pattern.finditer(value):
            name = re.sub(r"\s+", " ", match.group(1)).strip(" ,;\t\r\n")[:80]
            if name and not any(name.casefold() in organisation.casefold() for organisation in organizations):
                people.add(name)
    return {"people": sorted(people)[:20], "organizations": organizations}


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


def _taxonomy_signal(value):
    """Normalize a bounded metadata/content signal for deterministic matching."""
    value = str(value or "").casefold()
    # Keep CJK characters intact while making filenames and English phrases
    # comparable without allowing punctuation to join unrelated words.
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value).strip()


def _taxonomy_term_hit(signal, term):
    term_signal = _taxonomy_signal(term)
    if not signal or not term_signal:
        return False
    if re.search(r"[\u4e00-\u9fff]", term_signal):
        return term_signal in signal
    # English terms are matched on token boundaries.  This avoids classifying
    # an arbitrary filename such as ``catalog.csv`` as an operations log just
    # because it contains the substring ``log``.
    return bool(re.search(
        r"(?<![a-z0-9]){}(?![a-z0-9])".format(re.escape(term_signal)),
        signal,
    ))


def classify_preview(preview):
    """Classify one file into a stable primary content category.

    The classifier is intentionally explainable and bounded.  It uses file
    metadata, a small keyword list and (when available) the bounded preview
    text.  It never treats an arbitrary JSON field name as a package topic.
    """
    preview = preview or {}
    path = str(preview.get("path") or preview.get("name") or "")
    name = str(preview.get("name") or PurePosixPath(path).name)
    extension = str(preview.get("extension") or Path(name).suffix or "").casefold()
    document_type = str(preview.get("document_type") or "")
    keywords = [str(item) for item in (preview.get("keywords") or []) if item]
    archive_members = [str(item) for item in (preview.get("archive_members") or []) if item]
    body = str(preview.get("preview_text") or "")[:16000]
    metadata_signal = _taxonomy_signal(" ".join((path, name, extension, document_type, *archive_members[:40])))
    keyword_signal = _taxonomy_signal(" ".join(keywords[:40]))
    body_signal = _taxonomy_signal(body)

    ranked = []
    for definition in CONTENT_CATEGORY_DEFINITIONS:
        category_id = definition["category_id"]
        score = 0.0
        matched = []
        for term in definition.get("terms") or ():
            hit_metadata = _taxonomy_term_hit(metadata_signal, term)
            hit_keywords = _taxonomy_term_hit(keyword_signal, term)
            hit_body = _taxonomy_term_hit(body_signal, term)
            if not (hit_metadata or hit_keywords or hit_body):
                continue
            # A term seen in extracted keywords is stronger than a path-only
            # hint; a body hit remains useful but is deliberately lower weight
            # because the body is only a sample in large-package mode.
            term_score = (1.5 if hit_metadata else 0.0)
            term_score += (2.5 if hit_keywords else 0.0)
            term_score += (1.0 if hit_body else 0.0)
            score += term_score
            matched.append(str(term))
        if extension in {str(item).casefold() for item in definition.get("extensions") or ()}:
            score += 2.25
            matched.append("扩展名:{}".format(extension))
        if document_type and document_type in set(definition.get("document_types") or ()):
            score += 2.0
            matched.append("文档类型:{}".format(document_type))
        if score:
            ranked.append((score, int(definition.get("priority") or 0), category_id, matched))

    if not ranked:
        fallback = "general_documents"
        if extension in {".csv", ".tsv", ".json", ".jsonl", ".xml", ".xlsx", ".xls", ".xlsm"}:
            fallback = "structured_data"
        ranked = [(0.5, CONTENT_CATEGORY_BY_ID[fallback].get("priority", 1), fallback, [])]
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    top_score, _priority, primary_id, primary_terms = ranked[0]
    # Secondary labels are navigation hints only.  Keep them sparse so a file
    # cannot inflate every category in the directory.
    secondary = [
        item[2] for item in ranked[1:]
        if item[0] >= max(2.0, top_score * 0.52)
        and item[2] != "general_documents"
    ][:2]
    category_ids = [primary_id] + [item for item in secondary if item != primary_id]
    fallback_only = top_score <= 0.5
    confidence = 0.55 if fallback_only else min(0.98, 0.42 + min(0.52, top_score / 20.0))
    basis = ["path_and_filename", "document_type"]
    if keywords:
        basis.append("bounded_keywords")
    if body.strip():
        basis.append("bounded_content_sample")
    return {
        "primary_category_id": primary_id,
        "category_ids": category_ids,
        "matched_terms": list(dict.fromkeys(primary_terms))[:10],
        "confidence": round(confidence, 4),
        "score": round(float(top_score), 4),
        "basis": basis,
        "secondary_category_ids": secondary,
    }


def preview_matches_category(preview, category_id):
    """Return whether a preview belongs to a category's primary bucket."""
    classification = preview.get("content_classification") if isinstance(preview, dict) else None
    if not isinstance(classification, dict):
        classification = classify_preview(preview)
    return str(classification.get("primary_category_id") or "") == str(category_id or "")


def preview_file(root, file_node, per_file_bytes=96 * 1024, budget=None,
                 zip_member_limit=80, zip_member_bytes=8192,
                 cancel_check=None, yield_check=None, hash_source=True):
    """Create a bounded preview; optionally avoid a full-source hash.

    Large-directory mode uses metadata fingerprints during the map-building
    pass. A full content hash is reserved for files promoted to deep analysis.
    """
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
    preview_windows = []
    sampled_bytes = 0
    member_names = []
    encoding = None
    text = ""
    status = "previewed"
    source_sha256 = ""
    source_hash_kind = "full" if hash_source else "metadata"
    if not hash_source:
        source_sha256 = hashlib.sha256(json.dumps({
            "path": relative_path,
            "size": size,
            "modified_at_ns": int(file_node.get("modified_at_ns") or 0),
            "inode": file_node.get("inode"),
            "device": file_node.get("device"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    if extension in OUT_OF_SCOPE_MEDIA_EXTENSIONS:
        status = "out_of_scope"
        warning.append("音视频按当前项目范围仅登记文件清单，不读取、不解析、不进入深度处理队列。")
    elif is_sensitive_file(path.name):
        status = "restricted"
        warning.append("敏感文件仅登记元数据，未读取正文。")
    elif budget is not None and budget.exhausted:
        try:
            if hash_source:
                source_sha256 = _stream_source_sha256(
                    path, cancel_check=cancel_check, yield_check=yield_check
                )
            status = "deferred"
            warning.append("本轮轻量读取预算已用尽，文件保留为后续可恢复任务。")
        except (OSError, RuntimeError) as exc:
            status = "failed"
            warning.append("流式哈希失败：{}".format(str(exc)[:300]))
    else:
        try:
            if hash_source:
                source_sha256 = _stream_source_sha256(
                    path, cancel_check=cancel_check, yield_check=yield_check
                )
            if extension in OFFICE_EXTENSIONS or extension == ".zip":
                text, member_names, sampled_bytes = _bounded_zip_members(
                    path, zip_member_limit, zip_member_bytes, budget=budget,
                    total_bytes=per_file_bytes,
                )
                text = text[: max(1000, int(per_file_bytes))]
            elif extension in TEXT_EXTENSIONS or extension in {".eml", ".mbox"}:
                blob, sampled_ranges, limited, window_blobs = _sample_windows(
                    path, per_file_bytes, budget=budget
                )
                sampled_bytes = sum(end - start for start, end in sampled_ranges)
                decoded_windows = [_decode_bytes(item) for item in window_blobs]
                text = "\n\n".join(item[0] for item in decoded_windows)
                encoding = decoded_windows[0][1] if decoded_windows else None
                labels = (
                    ["full"] if len(decoded_windows) == 1
                    else ["head", "middle", "tail"][:len(decoded_windows)]
                )
                preview_windows = [
                    {
                        "label": labels[index],
                        "byte_start": sampled_ranges[index][0],
                        "byte_end": sampled_ranges[index][1],
                        "text": decoded[0],
                        "encoding": decoded[1],
                    }
                    for index, decoded in enumerate(decoded_windows)
                ]
                if limited:
                    warning.append("本轮全局轻量读取预算不足，当前文件只读取了部分样本。")
            elif extension == ".pdf":
                # PDF page extraction is intentionally deferred to selected
                # representatives; the bounded binary windows can still expose
                # title/producer metadata and stable duplicate signatures.
                blob, sampled_ranges, limited, _window_blobs = _sample_windows(
                    path, min(per_file_bytes, 64 * 1024), budget=budget
                )
                sampled_bytes = sum(end - start for start, end in sampled_ranges)
                metadata_strings = re.findall(rb"/(?:Title|Author|Subject|Keywords)\s*\(([^)]{1,500})\)", blob)
                text = "\n".join(_decode_bytes(item)[0] for item in metadata_strings)
                if not text:
                    warning.append("PDF 正文将在代表文件深析或用户命中后按代表页/全文解析。")
                if limited:
                    warning.append("本轮全局轻量读取预算不足。")
            elif extension in ARCHIVE_EXTENSIONS:
                blob, sampled_ranges, limited, _window_blobs = _sample_windows(
                    path, min(per_file_bytes, 32 * 1024), budget=budget
                )
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
                blob, sampled_ranges, limited, window_blobs = _sample_windows(
                    path, min(per_file_bytes, 32 * 1024), budget=budget
                )
                sampled_bytes = sum(end - start for start, end in sampled_ranges)
                if extension not in IMAGE_EXTENSIONS:
                    decoded, encoding = _decode_bytes(blob)
                    printable = sum(character.isprintable() for character in decoded)
                    if decoded and printable / float(len(decoded)) >= 0.75:
                        text = decoded
                        labels = (
                            ["full"] if len(window_blobs) == 1
                            else ["head", "middle", "tail"][:len(window_blobs)]
                        )
                        preview_windows = [
                            {
                                "label": labels[index],
                                "byte_start": sampled_ranges[index][0],
                                "byte_end": sampled_ranges[index][1],
                                "text": _decode_bytes(window_blob)[0],
                                "encoding": encoding,
                            }
                            for index, window_blob in enumerate(window_blobs)
                        ]
                if extension in IMAGE_EXTENSIONS:
                    warning.append("图片首轮登记元数据；OCR 在代表文件或用户命中后执行。")
                if limited:
                    warning.append("本轮全局轻量读取预算不足。")
        except PreviewSliceYield:
            raise
        except (OSError, RuntimeError, ValueError, EOFError, zipfile.BadZipFile) as exc:
            status = "failed"
            warning.append("轻量预览失败：{}".format(str(exc)[:300]))

    # Window separators are not source bytes and may add a handful of
    # characters.  Keep that tiny allowance so the tail sample is not cut off.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)[
        : max(1000, int(per_file_bytes) + 32)
    ]
    if not preview_windows and text.strip():
        preview_windows = [{
            "label": "metadata",
            "byte_start": sampled_ranges[0][0] if sampled_ranges else None,
            "byte_end": sampled_ranges[-1][1] if sampled_ranges else None,
            "text": text,
            "encoding": encoding,
        }]
    for window in preview_windows:
        window["text"] = re.sub(
            r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", str(window.get("text") or "")
        )[: max(1000, int(per_file_bytes))]
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
    entities = _preview_entities(text)
    emails = sorted(set(EMAIL_RE.findall(text)))[:20]
    dates = sorted(set(DATE_RE.findall(text)))[:20]
    keywords = _keywords("{} {}".format(path.stem, text))
    content_classification = classify_preview({
        "path": relative_path,
        "name": path.name,
        "extension": extension,
        "document_type": _document_type(path),
        "keywords": keywords,
        "archive_members": member_names[:200],
        "preview_text": text,
    })
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
        "preview_windows": preview_windows,
        "sample_sha256": content_sample_sha256,
        "content_sample_sha256": content_sample_sha256,
        "source_sha256": source_sha256,
        "source_hash_kind": source_hash_kind,
        "hash_status": "out_of_scope" if status == "out_of_scope" else (
            "metadata_only" if not hash_source else ("completed" if source_sha256 else (
                "restricted" if status == "restricted" else "failed"
            ))
        ),
        "preview_fingerprint": preview_fingerprint,
        "encoding": encoding,
        "language": language,
        "keywords": keywords,
        "content_classification": content_classification,
        "primary_content_category": content_classification.get("primary_category_id"),
        "content_category_ids": content_classification.get("category_ids") or [],
        "entities": {**entities, "email_addresses": emails},
        "dates": dates,
        "archive_members": member_names[:200],
        "warnings": warning,
        "previewed_at": _utc_now(),
        "coverage": {
            "inventory_complete": True,
            "preview_only": True,
            "parse_complete": False,
            "semantic_complete": False,
            "out_of_scope": status == "out_of_scope",
            "sampled_bytes": sampled_bytes,
            "source_bytes": size,
        },
    }


def preview_as_document(preview):
    """Project a preview into the existing unified-document contract."""
    text = str(preview.get("preview_text") or "")
    path = str(preview.get("path") or "")
    fingerprint = (
        preview.get("preview_fingerprint") or preview.get("sample_sha256") or ""
    )[:16]
    windows = list(preview.get("preview_windows") or [])
    if not windows and text.strip():
        windows = [{"label": "legacy", "text": text}]
    evidence = []
    for index, window in enumerate(windows):
        window_text = str(window.get("text") or "")
        if not window_text.strip():
            continue
        label = str(window.get("label") or "window")
        chunk_chars = 1800
        overlap_chars = 120
        start = 0
        chunk_index = 0
        while start < len(window_text):
            end = min(len(window_text), start + chunk_chars)
            chunk_text = window_text[start:end].strip()
            if chunk_text:
                evidence.append({
                    "evidence_id": "P-{}-{}-{}-{}".format(
                        fingerprint, label, index, chunk_index
                    ),
                    "source_path": path,
                    "label": "bounded_preview_{}".format(label),
                    "text": chunk_text,
                    "content_sha256": hashlib.sha256(
                        chunk_text.encode("utf-8")
                    ).hexdigest(),
                    "preview_only": True,
                    "preview_window": label,
                    "preview_window_char_start": start,
                    "preview_window_char_end": end,
                    # These are conservative source bounds for the sampled
                    # window. Exact character-to-byte mapping is encoding
                    # dependent, so do not manufacture narrower offsets.
                    "source_byte_start": window.get("byte_start"),
                    "source_byte_end": window.get("byte_end"),
                })
                chunk_index += 1
            if end >= len(window_text):
                break
            start = end - overlap_chars
    return {
        "schema_version": "unified-document/1.0",
        "source": {
            "path": path,
            "name": preview.get("name"),
            "extension": preview.get("extension"),
            "size": preview.get("size"),
            "modified_at": preview.get("modified_at"),
            "sample_sha256": preview.get("sample_sha256"),
            "sha256": preview.get("source_sha256") or "",
        },
        "parsed_at": preview.get("previewed_at"),
        "parser": {"name": "bounded-package-preview", "mode": "preview", "degraded": False},
        "structure": {"title": Path(path).stem, "headings": [], "page_count": None, "table_count": 0, "picture_count": 0},
        "text": text,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "coverage": {
            **dict(preview.get("coverage") or {}),
            "level": "preview",
            "preview_only": True,
            "formal_evidence_ready": False,
            "deep_parse_complete": False,
        },
        # Preview snippets remain available for navigation and candidate search,
        # but are explicitly marked as non-formal evidence.
        "evidence_status": "preview_candidate",
        "evidence_level": "preview",
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


def preview_relation_features(preview):
    """Yield bounded, auditable features for the complete relation index."""
    preview = preview or {}
    path = str(preview.get("path") or "")
    if not path or preview.get("status") != "previewed":
        return
    values = []
    values.extend(("topic", value, 1.0) for value in (preview.get("keywords") or [])[:8])
    entities = preview.get("entities") or {}
    values.extend(("person", value, 2.5) for value in entities.get("people") or [])
    values.extend(("organization", value, 2.5) for value in entities.get("organizations") or [])
    values.extend(("email", value, 4.0) for value in entities.get("email_addresses") or [])
    values.extend(("date", value, 1.5) for value in (preview.get("dates") or [])[:20])
    values.append(("document_type", preview.get("document_type"), 0.3))
    classification = preview.get("content_classification") or classify_preview(preview)
    primary_category = str(classification.get("primary_category_id") or "")
    if primary_category:
        values.append(("content_category", primary_category, 2.0))
    seen = set()
    for kind, value, weight in values:
        normalized = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
        key = (kind, normalized)
        if not normalized or key in seen:
            continue
        seen.add(key)
        yield path, kind, normalized, weight


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


def _selection_scorecard(preview, topic_frequency, type_frequency,
                         language_frequency, extension_frequency,
                         directory_frequency, duplicate_frequency):
    """Return an auditable 0-100 value score without invoking a model."""
    keywords = list(preview.get("keywords") or [])
    entities = preview.get("entities") or {}
    dates = list(preview.get("dates") or [])
    entity_count = sum(len(values or []) for values in entities.values())
    text_chars = int(preview.get("preview_characters") or 0)
    doc_type = str(preview.get("document_type") or "其他文件")
    language = str((preview.get("language") or {}).get("code") or "unknown")
    extension = str(preview.get("extension") or "[无扩展名]")
    directory = str(PurePosixPath(str(preview.get("path") or "")).parent)
    digest = str(preview.get("sample_sha256") or "")
    size = max(0, int(preview.get("size") or 0))

    components = {
        "topic_relevance": min(15.0, len(keywords[:10]) * 1.5),
        "information_density": min(15.0, text_chars / 4000.0 * 15.0),
        "content_uniqueness": 15.0 / max(1, duplicate_frequency[digest]),
        "evidence_potential": min(15.0, entity_count * 1.5 + len(dates) * 2.0),
        "key_facts": min(10.0, entity_count + len(dates) * 2.0),
        "structural_representativeness": min(
            15.0,
            4.0 / max(1, type_frequency[doc_type])
            + 3.0 / max(1, language_frequency[language])
            + 4.0 / max(1, extension_frequency[extension])
            + 4.0 / max(1, directory_frequency[directory]),
        ),
        "parse_quality": 10.0 if preview.get("status") == "previewed" else 0.0,
        "processing_cost": max(0.0, 5.0 - min(5.0, size / float(256 * 1024 * 1024))),
    }
    rounded = {key: round(value, 4) for key, value in components.items()}
    return round(sum(rounded.values()), 4), rounded


def _hard_selection_decision(preview, exact_duplicate_non_primary=False):
    """Apply only deterministic exclusions; uncertain files remain promotable."""
    path = str(preview.get("path") or "")
    name = PurePosixPath(path).name.casefold()
    extension = str(preview.get("extension") or "").casefold()
    status = str(preview.get("status") or "unknown")
    if status == "out_of_scope":
        return "excluded", "out_of_scope_media", False
    size = max(0, int(preview.get("size") or 0))
    if status == "restricted":
        return "excluded", "restricted_or_sensitive", False
    if status == "failed":
        return "excluded", "lightweight_extraction_failed", True
    if status == "deferred":
        return "pending_preview", "preview_budget_deferred", True
    if size == 0:
        return "excluded", "empty_file", False
    if exact_duplicate_non_primary:
        # A duplicate alias is deliberately represented in the inventory, but
        # it is a terminal queue exclusion.  Marking it promotable leaves the
        # scheduler and coverage counters disagreeing about whether work
        # remains.  The canonical file carries the one independent result.
        return "excluded", "exact_duplicate_non_primary", False
    if name in LOW_VALUE_NAMES or extension in LOW_VALUE_EXTENSIONS:
        # Cache/dependency files remain visible for auditability, never enter
        # deep processing, and must count as a terminal policy exclusion.
        return "excluded", "cache_temporary_or_dependency_file", False
    if extension and extension not in DEEP_PARSE_EXTENSIONS:
        return "deferred", "unsupported_or_binary_metadata_only", True
    return None, None, True


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
            "name": source.get("name"),
            "archive_members": list(source.get("archive_members") or [])[:40],
            "content_classification": dict(
                source.get("content_classification") or classify_preview(source)
            ),
            "entities": {
                key: list(values or [])[:20]
                for key, values in (source.get("entities") or {}).items()
            },
            "dates": list(source.get("dates") or [])[:20],
            "sample_sha256": source.get("content_sample_sha256") or source.get("sample_sha256"),
            "source_sha256": source.get("source_sha256"),
            "preview_characters": int(
                source.get("preview_characters")
                or len(str(source.get("preview_text") or ""))
            ),
        })
    previews = compact_previews
    requested_representative_limit = max(1, int(representative_limit or 1))
    representative_limit = requested_representative_limit
    type_frequency = Counter(str(item.get("document_type") or "其他文件") for item in previews)
    language_frequency = Counter(str((item.get("language") or {}).get("code") or "unknown") for item in previews)
    extension_frequency = Counter(str(item.get("extension") or "[无扩展名]") for item in previews)
    topic_frequency = Counter(topic for item in previews for topic in item.get("keywords") or [])
    category_buckets = {}
    for definition in CONTENT_CATEGORY_DEFINITIONS:
        category_buckets[definition["category_id"]] = {
            "definition": definition,
            "file_count": 0,
            "total_bytes": 0,
            "previewed_files": 0,
            "matched_terms": Counter(),
            "ranked": [],
        }
    for item in previews:
        classification = item.get("content_classification") or classify_preview(item)
        primary_id = str(classification.get("primary_category_id") or "general_documents")
        bucket = category_buckets.setdefault(primary_id, {
            "definition": CONTENT_CATEGORY_BY_ID.get(primary_id, CONTENT_CATEGORY_BY_ID["general_documents"]),
            "file_count": 0, "total_bytes": 0, "previewed_files": 0,
            "matched_terms": Counter(), "ranked": [],
        })
        bucket["file_count"] += 1
        bucket["total_bytes"] += max(0, int(item.get("size") or 0))
        if str(item.get("status") or "") == "previewed":
            bucket["previewed_files"] += 1
        for term in classification.get("matched_terms") or []:
            bucket["matched_terms"][str(term)] += 1
    people_frequency = Counter(
        person
        for item in previews
        for person in set((item.get("entities") or {}).get("people", []))
    )
    organization_frequency = Counter(
        organization
        for item in previews
        for organization in set((item.get("entities") or {}).get("organizations", []))
    )
    date_frequency = Counter(
        date for item in previews for date in set(item.get("dates") or []) if date
    )
    year_frequency = Counter(
        year
        for item in previews
        for year in {
            match.group(0)
            for date in item.get("dates") or []
            for match in [re.search(r"(?:19|20)\d{2}", str(date))]
            if match
        }
    )
    duplicate_frequency = Counter(str(item.get("sample_sha256") or "") for item in previews)
    exact_duplicate_paths = defaultdict(list)
    for item in previews:
        source_sha256 = str(item.get("source_sha256") or "")
        if source_sha256:
            exact_duplicate_paths[source_sha256].append(str(item.get("path") or ""))
    exact_duplicate_non_primary = {
        path
        for paths in exact_duplicate_paths.values() if len(paths) > 1
        for path in sorted(paths)[1:]
    }
    directory_frequency = Counter(
        str(PurePosixPath(str(item.get("path"))).parent) for item in previews
    )
    statuses = Counter(str(item.get("status") or "unknown") for item in previews)
    total_bytes = sum(int(item.get("size") or 0) for item in previews)
    if len(previews) > 60 and requested_representative_limit > 60:
        diversity_signal = sum(min(100, len(counter)) for counter in (
            type_frequency, language_frequency, extension_frequency, topic_frequency,
        ))
        adaptive_budget = max(
            60,
            int(math.ceil(math.sqrt(len(previews)) * 2.5)),
            min(300, diversity_signal * 2),
        )
        representative_limit = min(
            requested_representative_limit, len(previews), adaptive_budget,
        )

    scored = []
    decisions_by_path = {}
    for item in previews:
        path = str(item.get("path") or "")
        score = _representative_score(
            item, topic_frequency, type_frequency, language_frequency, duplicate_frequency,
        )
        scored.append((score, str(item.get("path")), item))
        classification = item.get("content_classification") or classify_preview(item)
        primary_category_id = str(
            classification.get("primary_category_id") or "general_documents"
        )
        category_buckets.setdefault(primary_category_id, {
            "definition": CONTENT_CATEGORY_BY_ID.get(
                primary_category_id, CONTENT_CATEGORY_BY_ID["general_documents"]
            ),
            "file_count": 0,
            "total_bytes": 0,
            "previewed_files": 0,
            "matched_terms": Counter(),
            "ranked": [],
        })["ranked"].append((score, path))
        value_score, components = _selection_scorecard(
            item, topic_frequency, type_frequency, language_frequency,
            extension_frequency, directory_frequency, duplicate_frequency,
        )
        hard_state, hard_reason, promotable = _hard_selection_decision(
            item, exact_duplicate_non_primary=path in exact_duplicate_non_primary,
        )
        decisions_by_path[path] = {
            "path": path,
            "selection_state": hard_state or "deferred",
            "workflow_state": (
                "excluded" if hard_state == "excluded" else
                "safety_checked" if hard_state == "pending_preview" else
                "deferred_searchable"
            ),
            "score": value_score,
            "score_components": components,
            "reasons": [hard_reason] if hard_reason else ["awaiting_diverse_gate_selection"],
            "promotion_allowed": bool(promotable),
            "safety_status": "restricted" if item.get("status") == "restricted" else "checked",
            "light_index_status": "ready" if item.get("status") == "previewed" else str(item.get("status") or "unknown"),
            "language_code": str((item.get("language") or {}).get("code") or "unknown"),
            "ocr_candidate": str(item.get("extension") or "").lower() in IMAGE_EXTENSIONS | {".pdf"},
            "content_category_id": primary_category_id,
            "content_category_ids": list(classification.get("category_ids") or [primary_category_id])[:3],
            "content_classification_confidence": float(classification.get("confidence") or 0.0),
        }
    scored.sort(key=lambda row: (-row[0], row[1]))

    selected = []
    selected_paths = set()

    def add(item, reason):
        path = str(item.get("path") or "")
        decision = decisions_by_path.get(path) or {}
        if (
            not path
            or item.get("status") != "previewed"
            or decision.get("selection_state") in {"excluded", "pending_preview"}
            or path in selected_paths
            or len(selected) >= representative_limit
        ):
            return
        selected_paths.add(path)
        selected.append({"path": path, "score": next((row[0] for row in scored if row[1] == path), 0), "reason": reason})
        decision["selection_state"] = "priority"
        decision["workflow_state"] = "priority_queued"
        decision["reasons"] = [reason]

    # Guarantee structural coverage before filling by score.
    dimensions = (
        (lambda item: str(item.get("document_type") or "其他文件"), "文档类型代表"),
        (lambda item: str((item.get("language") or {}).get("code") or "unknown"), "语言代表"),
        (lambda item: str(item.get("extension") or "[无扩展名]"), "格式代表"),
        (lambda item: str(PurePosixPath(str(item.get("path"))).parent), "目录代表"),
        (lambda item: str((item.get("dates") or ["unknown"])[0])[:4], "时间段代表"),
        (lambda item: str((item.get("keywords") or ["未知主题"])[0]), "主题代表"),
    )
    dimension_quota = max(1, representative_limit // max(1, len(dimensions)))
    for key_fn, reason in dimensions:
        seen = set()
        for _score, _path, item in scored:
            key = key_fn(item)
            if key not in seen:
                add(item, reason)
                seen.add(key)
                if len(seen) >= dimension_quota:
                    break
    for _score, _path, item in scored:
        add(item, "信息量、独特性与关系价值综合得分")

    selection_counts = Counter(
        decision["selection_state"] for decision in decisions_by_path.values()
    )

    duplicates = [{
        "sha256": digest,
        "canonical_path": sorted(paths)[0],
        "paths": sorted(paths),
        "file_count": len(paths),
        "kind": "exact_sha256",
    } for digest, paths in sorted(exact_duplicate_paths.items()) if len(paths) > 1]
    by_digest = defaultdict(list)
    for item in previews:
        by_digest[str(item.get("sample_sha256") or "")].append(str(item.get("path")))
    for digest, paths in sorted(by_digest.items()):
        if digest and len(paths) > 1:
            if not any(set(paths) == set(item.get("paths") or []) for item in duplicates):
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

    content_categories = []
    for category_id, bucket in category_buckets.items():
        if not int(bucket.get("file_count") or 0):
            continue
        definition = bucket.get("definition") or CONTENT_CATEGORY_BY_ID["general_documents"]
        ranked_paths = sorted(
            bucket.get("ranked") or [],
            key=lambda item: (-float(item[0]), str(item[1])),
        )
        representatives = [str(item[1]) for item in ranked_paths[:12] if str(item[1])]
        file_count = int(bucket.get("file_count") or 0)
        previewed_files = int(bucket.get("previewed_files") or 0)
        content_categories.append({
            "category_id": category_id,
            "name": definition.get("name") or category_id,
            "description": definition.get("description") or "",
            "file_count": file_count,
            "total_bytes": int(bucket.get("total_bytes") or 0),
            "total_size_human": human_size(bucket.get("total_bytes") or 0),
            "previewed_files": previewed_files,
            "sampled_files": previewed_files,
            "representative_paths": representatives,
            # Only a bounded sample is returned. The complete membership is
            # resolved from the durable inventory after the user selects it.
            "member_paths": representatives,
            "member_paths_lazy": True,
            "paths_truncated": file_count > len(representatives),
            "selection_filter": {
                "kind": "content_category",
                "category_id": category_id,
                "match": "primary",
                "terms": list(definition.get("terms") or [])[:24],
            },
            "matched_terms": [
                term for term, _count in bucket["matched_terms"].most_common(10)
            ],
            "confidence": round(
                min(0.98, 0.55 + 0.4 * (previewed_files / float(file_count or 1))),
                4,
            ),
            "classification_basis": [
                "path_and_filename", "document_type", "bounded_keywords",
                "bounded_content_sample",
            ],
            "research_value": definition.get("research_value") or "",
            "research_questions": list(definition.get("research_questions") or [])[:4],
            "coverage": {
                "inventory_files": file_count,
                "classified_files": file_count,
                "preview_analyzed_files": previewed_files,
                "classification_ratio": 1.0,
                "content_sample_ratio": round(previewed_files / float(file_count or 1), 6),
                "membership_complete": True,
                "members_materialized": False,
                "verification_status": "metadata_and_rule_classified",
            },
            "analysis_level": "inventory_preview",
            "verification_status": "candidate",
            "formal": False,
        })
    content_categories.sort(
        key=lambda item: (-int(item.get("file_count") or 0), item.get("category_id") or "")
    )
    research_directions = []
    for category in content_categories[:8]:
        definition = CONTENT_CATEGORY_BY_ID.get(category.get("category_id"), {})
        research_directions.append({
            "direction_id": "direction-{}".format(category.get("category_id")),
            "title": category.get("name") or "待研究方向",
            "description": category.get("research_value") or category.get("description") or "",
            "basis": "内容分类覆盖 {} 个文件；分类来自全量元数据与有界样本规则。".format(category.get("file_count") or 0),
            "category_id": category.get("category_id"),
            "candidate_file_count": int(category.get("file_count") or 0),
            "candidate_bytes": int(category.get("total_bytes") or 0),
            "representative_paths": list(category.get("representative_paths") or [])[:6],
            "questions": list(definition.get("research_questions") or [])[:4],
            "status": "candidate",
            "coverage": {
                "inventory_files": int(category.get("file_count") or 0),
                "preview_analyzed_files": int(category.get("previewed_files") or 0),
                "deep_analyzed_files": 0,
                "claim_status": "requires_selected_file_analysis",
            },
        })

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
        "content_categories": content_categories,
        "research_directions": research_directions,
        "content_taxonomy": {
            "schema_version": "content-taxonomy/1.0",
            "classification": "one_primary_category_per_file_plus_bounded_secondary_hints",
            "primary_category_count": len(content_categories),
            "category_membership_is_lazy": True,
            "content_sample_is_bounded": True,
            "raw_keyword_topics_are_recall_only": True,
        },
        "entities": {
            "people": [{"name": key, "file_count": value} for key, value in people_frequency.most_common(200)],
            "organizations": [
                {"name": key, "file_count": value}
                for key, value in organization_frequency.most_common(200)
            ],
        },
        "dates": [{"date": key, "file_count": value} for key, value in date_frequency.most_common(200)],
        "years": [{"year": key, "file_count": value} for key, value in year_frequency.most_common()],
        "duplicates": duplicates[:500],
        "relationships": relationships,
        "isolated_paths": isolated[:500],
        "representatives": selected,
        "representative_paths": [item["path"] for item in selected],
        "selection_gate": {
            "schema_version": "file-selection-gate/1.0",
            "method": "hard_rules_then_diverse_value_score",
            "states": dict(sorted(selection_counts.items())),
            "scored_files": len(decisions_by_path),
            "priority_files": selection_counts.get("priority", 0),
            "deferred_files": selection_counts.get("deferred", 0),
            "excluded_files": selection_counts.get("excluded", 0),
            "pending_preview_files": selection_counts.get("pending_preview", 0),
            "dimensions": [
                "topic_relevance", "information_density", "content_uniqueness",
                "evidence_potential", "key_facts", "structural_representativeness",
                "parse_quality", "processing_cost",
            ],
            "promotion_contract": "deferred files remain searchable and may be promoted by query or user action",
            "requested_representative_files": requested_representative_limit,
            "adaptive_representative_files": representative_limit,
            "adaptive_basis": "inventory_size_and_diversity_saturation",
        },
        # The orchestrator persists these rows in a dedicated table and removes
        # them before storing the bounded package content map.
        "selection_decisions": [decisions_by_path[path] for path in sorted(decisions_by_path)],
        "coverage": {
            "previewed_files": statuses.get("previewed", 0),
            "restricted_files": statuses.get("restricted", 0),
            "deferred_files": statuses.get("deferred", 0),
            "failed_files": statuses.get("failed", 0),
            "all_inventory_accounted": sum(statuses.values()) == len(previews),
            "relationship_status": "preview_inferred_requires_deep_validation",
            "duplicate_status": "exact_sha256_with_sample_candidates",
            "classified_files": sum(int(item.get("file_count") or 0) for item in content_categories),
            "classification_coverage": round(
                sum(int(item.get("file_count") or 0) for item in content_categories)
                / float(len(previews) or 1), 6
            ),
            "content_category_status": "full_inventory_rule_classification",
        },
    }


def promotion_paths(content_map, requested_paths=None, limit=None):
    requested = [str(path) for path in (requested_paths or []) if path]
    representatives = [str(path) for path in (content_map or {}).get("representative_paths") or []]
    ordered = list(dict.fromkeys(requested + representatives))
    if limit is None:
        return ordered
    return ordered[: max(0, int(limit))]
class PreviewSliceYield(RuntimeError):
    """A higher-priority job requested the Worker between hash chunks."""
