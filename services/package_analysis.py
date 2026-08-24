import hashlib
import json
import math
import os
import shutil
import stat as stat_module
import tempfile
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from config import Config
from services.scanner import human_size, resolve_under
from services.evidence import (
    embedding_mode,
    evidence_quality,
    evidence_support,
    select_evidence,
    verify_claim_evidence,
)
from services.large_package import (
    attach_tree_coverage,
    build_coverage,
    build_policy,
    compact_overview_document,
    file_fingerprint,
    inventory_by_path,
    pending_group,
    representative_paths,
)
from services.retrieval import build_retrieval_manifest, evidence_corpus, retrieve_evidence
from services.unified_parser import compact_document
from services.unified_parser import UnifiedDocumentParser
from services.parse_isolation import (
    ParseIsolationCancelled,
    ParseIsolationError,
    ParseIsolationTimeout,
    runner_for,
)


PROFILE_USABLE_STATUSES = {"completed", "partial"}


def _temp_disk_worker_limit(requested_workers):
    """Bound concurrent parsers by worst-case scratch-disk reservations.

    A parser may temporarily materialise one content object up to the central
    limit.  Without this gate, two archive/PDF workers could both pass their
    individual free-space preflight and exhaust the same scratch volume.
    """
    requested = max(1, int(requested_workers or 1))
    try:
        free_bytes = int(shutil.disk_usage(str(Config.PARSE_TEMP_DIR)).free)
        usable_bytes = max(0, free_bytes - int(Config.PARSE_TEMP_DISK_RESERVE_BYTES or 0))
        # A verified source snapshot and an archive/repair materialisation may
        # coexist for one parser. Reserve both before enabling concurrency.
        per_worker = max(1, int(Config.MAX_CONTENT_BYTES or 1) * 2)
        disk_workers = max(1, usable_bytes // per_worker)
        return max(1, min(requested, int(disk_workers)))
    except (OSError, TypeError, ValueError):
        # Unknown capacity must fail conservatively to serial execution.
        return 1


def _canonical_projection(documents, exact_groups):
    """Return one analysis document per byte-identical source.

    The physical inventory is deliberately left untouched.  This projection is
    used by every content-weighted operation so copied files cannot amplify a
    topic, evidence source count, retrieval result, or value score.
    """
    canonical_by_path = {path: path for path in documents}
    aliases_by_canonical = defaultdict(list)
    group_by_path = {}
    for group in exact_groups:
        canonical = group.get("canonical")
        if canonical not in documents:
            continue
        for path in group.get("members") or []:
            if path not in documents:
                continue
            canonical_by_path[path] = canonical
            group_by_path[path] = group.get("group_id")
            if path != canonical:
                aliases_by_canonical[canonical].append(path)

    for path, document in documents.items():
        canonical = canonical_by_path[path]
        duplicate = document.setdefault("deduplication", {})
        duplicate.update({
            "canonical_path": canonical,
            "role": "canonical" if path == canonical else "duplicate_alias",
            "duplicate_of": None if path == canonical else canonical,
            "group_id": group_by_path.get(path),
            "aliases": sorted(aliases_by_canonical.get(path, [])) if path == canonical else [],
        })
    canonical_documents = {
        path: document for path, document in documents.items()
        if canonical_by_path[path] == path
    }
    return canonical_documents, canonical_by_path, {
        path: sorted(values) for path, values in aliases_by_canonical.items()
    }


def _build_value_judgment(scan, documents, analysis_stats, coverage, exact_groups,
                          topic_clusters, failures, pending_paths, structured_overview):
    """Build a transparent value assessment from local, auditable signals.

    This is intentionally not an LLM opinion.  It separates whether the data
    can be read from whether it appears worth deeper research, and exposes the
    component scores so the UI/report cannot turn a high parse ratio into an
    unsupported claim of business value.
    """
    scanned = max(0, int(scan.get("file_count") or 0))
    parsed = max(0, int(analysis_stats.get("parsed_files") or len(documents)))
    canonical_count = len(documents)
    parsed_ratio = float(coverage.get("parsed_file_ratio") or 0.0)
    byte_ratio = float(coverage.get("parsed_byte_ratio") or 0.0)
    failed = max(0, int(analysis_stats.get("failed_files") or len(failures)))
    duplicate_files = sum(int(group.get("duplicate_count") or 0) for group in exact_groups)
    unique_ratio = max(0.0, min(1.0, canonical_count / float(canonical_count + duplicate_files or 1)))
    cluster_sizes = [len(item.get("members") or []) for item in topic_clusters]
    largest_cluster_ratio = max(cluster_sizes or [0]) / float(canonical_count or 1)
    topic_concentration = max(0.0, min(1.0, largest_cluster_ratio))
    valid_evidence = [
        item for document in documents.values() for item in (document.get("evidence") or [])
        if evidence_quality(item).get("eligible")
    ]
    evidence_count = len(valid_evidence)
    evidence_density = max(0.0, min(1.0, evidence_count / float(max(canonical_count, 1) * 3)))
    text_characters = sum(len(str(document.get("text") or "")) for document in documents.values())
    information_signal = min(1.0, text_characters / float(max(canonical_count, 1) * 8000))
    quality_score = structured_overview.get("average_quality_score")
    structured_quality = max(0.0, min(1.0, float(quality_score) / 100.0)) if quality_score is not None else None

    def pct(value):
        return round(max(0.0, min(1.0, float(value))) * 100, 1)

    dimensions = {
        "readability": {"score": pct(parsed_ratio), "basis": "已完成解析的文件占扫描文件比例"},
        "completeness": {"score": pct(min(parsed_ratio, byte_ratio or parsed_ratio)), "basis": "文件覆盖率与字节覆盖率取保守值"},
        "uniqueness": {"score": pct(unique_ratio), "basis": "精确重复文件越少，独特内容比例越高"},
        "topic_concentration": {"score": pct(topic_concentration), "basis": "最大主题簇占已解析内容的比例"},
        "evidence_density": {"score": pct(evidence_density), "basis": "可回查正文证据数量与解析文件规模"},
    }
    if structured_quality is not None:
        dimensions["structured_quality"] = {
            "score": pct(structured_quality),
            "basis": "CSV/XLSX/JSON 画像质量评分",
        }
    # Duplicate copies are reported, but never increase or decrease the
    # research-potential score.  Only canonical content signals participate.
    research_components = [topic_concentration, evidence_density, information_signal]
    if structured_quality is not None:
        research_components.append(structured_quality)
    research_score = pct(sum(research_components) / float(len(research_components) or 1))
    enough_research_basis = canonical_count >= 3 and evidence_count >= 3
    if not enough_research_basis:
        research_score = min(research_score, 59.0)
        research_level = "待确认"
    elif research_score >= 75:
        research_level = "高"
    elif research_score >= 45:
        research_level = "中高" if topic_clusters or evidence_count else "中"
    else:
        research_level = "待分析" if pending_paths or not parsed else "低"
    limitations = list(coverage.get("limitations") or [])
    if failed:
        limitations.append("存在 {} 个解析失败文件。".format(failed))
    if not topic_clusters:
        limitations.append("当前未形成稳定主题簇，研究方向需要人工确认。")
    usability_score = pct(min(parsed_ratio, byte_ratio or parsed_ratio))
    richness_score = pct((information_signal + evidence_density) / 2.0)
    task_topic = str(scan.get("task_topic") or scan.get("analysis_goal") or "").strip()
    task_relevance = {
        "level": "未评估",
        "score": None,
        "basis": "尚未提供客户任务或研究目标，系统不推测业务相关性。",
    }
    if task_topic:
        topic_tokens = set(_tokens(task_topic))
        matched = sum(
            1 for document in documents.values()
            if topic_tokens.intersection(_tokens(_semantic_document_profile("", document, 1200)))
        )
        relevance_score = pct(matched / float(canonical_count or 1))
        task_relevance = {
            "level": "高" if relevance_score >= 70 else "中" if relevance_score >= 35 else "低",
            "score": relevance_score,
            "basis": "客户任务关键词与规范文档正文的本地匹配比例。",
        }
    if not enough_research_basis:
        limitations.append("规范文档少于 3 份或有效正文证据少于 3 条，研究潜力最高只能标为待确认。")
    return {
        "level": "高" if parsed_ratio >= 0.9 and evidence_count else ("中" if parsed_ratio >= 0.5 or evidence_count else "待分析"),
        "availability": "高" if parsed_ratio >= 0.9 and not failed else ("中" if parsed_ratio >= 0.5 else "低"),
        "research_value": research_level,
        "research_score": research_score,
        "data_usability": {
            "level": "高" if usability_score >= 85 else "中" if usability_score >= 50 else "低",
            "score": usability_score,
            "basis": "文件与字节解析覆盖率的保守值。",
        },
        "information_richness": {
            "level": "高" if richness_score >= 70 else "中" if richness_score >= 35 else "低",
            "score": richness_score,
            "basis": "规范文档正文规模与有效证据密度，不受重复副本数量影响。",
        },
        "research_potential": {
            "level": research_level,
            "score": research_score,
            "basis": "规范文档主题集中度、正文丰富度和有效证据密度。",
            "minimum_basis_met": enough_research_basis,
        },
        "task_relevance": task_relevance,
        "canonical_document_count": canonical_count,
        "duplicate_alias_count": duplicate_files,
        "valid_evidence_count": evidence_count,
        "dimensions": dimensions,
        "basis": "基于可读性、覆盖完整性、内容独特性、主题集中度和可回查证据密度计算；不代表外部业务价值。",
        "confidence": "高" if coverage.get("complete_analysis") else "中",
        "limitations": list(dict.fromkeys(limitations)),
        "structured_data_signals": {
            "profiled_files": structured_overview.get("profiled_files", 0),
            "total_rows": structured_overview.get("total_rows", 0),
            "average_quality_score": structured_overview.get("average_quality_score"),
            "entity_categories": sorted((structured_overview.get("entity_statistics") or {}).keys()),
        },
    }


def _optional_llm_enrichment_enabled():
    """Return whether non-essential model naming may run.

    Parsing, evidence extraction, coverage calculation, and the lexical
    fallback tree must remain usable when the shared Ollama runner is busy or
    temporarily unavailable.  Semantic names are an enhancement, not a
    prerequisite for a valid analysis result.
    """
    return str(os.getenv("ENABLE_OPTIONAL_LLM_ENRICHMENT", "true")).strip().lower() not in {
        "0", "false", "no", "off", "disabled",
    }


WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,8}")
ENGLISH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
CHINESE_TEXT_RE = re.compile(r"[\u4e00-\u9fff]+")
CHINESE_DOMAIN_TERMS = (
    "威胁情报", "漏洞利用", "入侵检测", "攻击技术", "恶意软件", "勒索软件",
    "网络安全", "物联网安全", "车联网安全", "车载网络", "可信执行环境",
    "侧信道攻击", "远程证明", "身份认证", "访问控制", "隐私保护", "密码技术",
    "供应链安全", "数据泄露", "安全事件", "应急响应", "安全评估", "风险分析",
)
STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "研究", "分析", "报告",
    "文档", "文件", "资料", "进行", "一种", "基于", "相关", "情况", "数据", "方法",
}

try:
    import jieba
    jieba.setLogLevel(30)
    for _domain_term in CHINESE_DOMAIN_TERMS:
        jieba.add_word(_domain_term, freq=2_000_000)
except ImportError:  # The deterministic regex fallback keeps offline maintenance usable.
    jieba = None


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SourceFileChangedError(RuntimeError):
    """Raised when a source is still being copied or changed after inventory."""


def _stat_signature(path):
    stat = Path(path).stat()
    return int(stat.st_size), int(stat.st_mtime_ns), int(stat.st_dev), int(stat.st_ino)


def _inventory_mtime_seconds(file_node):
    value = str(file_node.get("modified_at") or "").strip()
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _is_archive_node(file_node):
    name = str(file_node.get("path") or file_node.get("name") or "").lower()
    return name.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".gz", ".bz2", ".7z", ".rar"))


def _source_has_open_writer(path):
    """Best-effort Linux check for an uploader that still owns a write fd."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    target = str(Path(path).resolve())
    try:
        processes = proc_root.iterdir()
    except OSError:
        return False
    for process in processes:
        if not process.name.isdigit():
            continue
        fd_root = process / "fd"
        try:
            descriptors = list(fd_root.iterdir())
        except (OSError, PermissionError):
            continue
        for descriptor in descriptors:
            try:
                if os.path.realpath(str(descriptor)) != target:
                    continue
                flags_line = next(
                    (line for line in (process / "fdinfo" / descriptor.name).read_text(encoding="ascii").splitlines() if line.startswith("flags:")),
                    "",
                )
                flags = int(flags_line.split()[1], 8)
                if flags & os.O_ACCMODE in {os.O_WRONLY, os.O_RDWR}:
                    return True
            except (OSError, PermissionError, ValueError, IndexError, StopIteration):
                continue
    return False


def _assert_source_stable(path, file_node, previous_signature=None, cancel_check=None):
    """Verify that parsing reads the exact file recorded by the inventory."""
    current = _stat_signature(path)
    expected_size = int(file_node.get("size") or 0)
    expected_mtime = _inventory_mtime_seconds(file_node)
    current_mtime = int(current[1] // 1_000_000_000)
    expected_device = file_node.get("device")
    expected_inode = file_node.get("inode")
    identity_changed = (
        expected_device not in {None, 0, "0"} and int(expected_device) != current[2]
    ) or (
        expected_inode not in {None, 0, "0"} and int(expected_inode) != current[3]
    )
    if current[0] != expected_size or (expected_mtime is not None and current_mtime != expected_mtime) or identity_changed:
        raise SourceFileChangedError(
            "源文件在目录扫描后发生变化（扫描时 {} 字节，当前 {} 字节）。"
            "文件可能仍在复制；请等待复制完成后重新导入数据包。".format(expected_size, current[0])
        )
    if previous_signature is not None and current != previous_signature:
        raise SourceFileChangedError(
            "源文件在解析期间发生变化，结果已丢弃。请等待文件复制完成后重新导入数据包。"
        )
    if previous_signature is None and _is_archive_node(file_node) and _source_has_open_writer(path):
        raise SourceFileChangedError(
            "压缩包仍被上传或复制程序以写入方式打开，中央目录尚未就绪。"
            "请等待复制完成、写入句柄关闭后重新导入数据包。"
        )
    observe_seconds = float(getattr(Config, "SOURCE_STABILITY_SECONDS", 0.0) or 0.0)
    if previous_signature is None and observe_seconds > 0 and _is_archive_node(file_node):
        deadline = time.monotonic() + observe_seconds
        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise ParseIsolationCancelled("任务已取消，停止检查源文件稳定性")
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        observed = _stat_signature(path)
        if observed != current:
            raise SourceFileChangedError(
                "压缩包仍在写入（{} 字节增至 {} 字节），尚不能安全展开。"
                "请等待复制完成后重新导入数据包。".format(current[0], observed[0])
            )
        current = observed
    return current


def _relative_source_parts(relative_path):
    value = str(relative_path or "").replace("\\", "/")
    relative = PurePosixPath(value)
    parts = tuple(part for part in relative.parts if part not in {"", "."})
    if (
        not parts
        or relative.is_absolute()
        or any(part == ".." for part in parts)
        or (parts and ":" in parts[0])
    ):
        raise SourceFileChangedError("源文件相对路径无效或超出扫描根目录")
    return parts


def _opened_handle_path(fd):
    """Best-effort canonical path for an already-open file descriptor."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows deployment
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
            get_name = ctypes.windll.kernel32.GetFinalPathNameByHandleW
            get_name.argtypes = [
                wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
            ]
            get_name.restype = wintypes.DWORD
            size = int(get_name(handle, None, 0, 0))
            if size:
                buffer = ctypes.create_unicode_buffer(size + 1)
                if get_name(handle, buffer, len(buffer), 0):
                    value = buffer.value
                    if value.startswith("\\\\?\\UNC\\"):
                        value = "\\\\" + value[8:]
                    elif value.startswith("\\\\?\\"):
                        value = value[4:]
                    return Path(value).resolve()
        except Exception:
            return None
    proc_link = Path("/proc/self/fd") / str(fd)
    try:
        if proc_link.exists():
            return Path(os.path.realpath(str(proc_link))).resolve()
    except OSError:
        pass
    return None


def _open_inventory_source(scan_root, file_node):
    """Open exactly the inventoried regular file without following links."""
    root = Path(scan_root).expanduser().resolve()
    parts = _relative_source_parts(file_node.get("path"))
    source_fd = None
    directory_fds = []
    try:
        if os.name != "nt" and os.open in getattr(os, "supports_dir_fd", set()):
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            current_fd = os.open(str(root), directory_flags)
            directory_fds.append(current_fd)
            for component in parts[:-1]:
                current_fd = os.open(component, directory_flags, dir_fd=current_fd)
                directory_fds.append(current_fd)
            source_fd = os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current_fd,
            )
        else:
            candidate = root.joinpath(*parts)
            cursor = root
            for component in parts:
                cursor = cursor / component
                if cursor.is_symlink():
                    raise SourceFileChangedError("源路径包含符号链接或重解析点，已拒绝读取")
            source_fd = os.open(
                str(candidate),
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            opened_path = _opened_handle_path(source_fd)
            if opened_path is None:
                raise SourceFileChangedError("当前平台无法安全验证源文件句柄范围")
            try:
                opened_path.relative_to(root)
            except ValueError as exc:
                raise SourceFileChangedError("源文件句柄已指向扫描根目录之外") from exc

        opened = os.fstat(source_fd)
        if not stat_module.S_ISREG(opened.st_mode):
            raise SourceFileChangedError("清点项已不再是普通文件")
        expected_size = int(file_node.get("size") or 0)
        expected_mtime_ns = int(file_node.get("modified_at_ns") or 0)
        if int(opened.st_size) != expected_size:
            raise SourceFileChangedError("源文件在清点后发生大小变化")
        if expected_mtime_ns and int(opened.st_mtime_ns) != expected_mtime_ns:
            raise SourceFileChangedError("源文件在清点后发生修改")
        expected_device = file_node.get("device")
        expected_inode = file_node.get("inode")
        if expected_device not in {None, 0, "0"} and int(opened.st_dev) != int(expected_device):
            raise SourceFileChangedError("源文件设备标识与清点记录不一致")
        if expected_inode not in {None, 0, "0"} and int(opened.st_ino) != int(expected_inode):
            raise SourceFileChangedError("源文件对象与清点记录不一致")
        return source_fd, opened
    except Exception:
        if source_fd is not None:
            os.close(source_fd)
        raise
    finally:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _restore_source_provenance(document, scan_root, file_node):
    if not isinstance(document, dict):
        return document
    source = document.setdefault("source", {})
    source.update({
        "path": str(file_node.get("path") or ""),
        "absolute_path": str(Path(scan_root) / str(file_node.get("path") or "")),
        "name": str(file_node.get("name") or Path(str(file_node.get("path") or "")).name),
        "extension": str(file_node.get("extension") or Path(str(file_node.get("path") or "")).suffix.lower()),
        "size": int(file_node.get("size") or 0),
        "modified_at": file_node.get("modified_at"),
        "source_snapshot_verified": True,
        "device": file_node.get("device"),
        "inode": file_node.get("inode"),
        "sensitive": bool(file_node.get("sensitive")),
        "content_policy": file_node.get("content_policy") or source.get("content_policy"),
    })
    return document


@contextmanager
def _secure_source_snapshot(scan_root, file_node, cancel_check=None):
    """Copy a verified source handle into private scratch before parsing.

    Third-party parsers reopen paths internally, so a safe descriptor alone
    is insufficient. Parsing a private snapshot closes the allow-list/open
    race and gives deterministic bytes for hashing and evidence generation.
    """
    expected_size = int(file_node.get("size") or 0)
    if expected_size > int(Config.MAX_CONTENT_BYTES):
        raise SourceFileChangedError("源文件超过中央 10 GiB 内容上限")
    Config.PARSE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    free_bytes = int(shutil.disk_usage(str(Config.PARSE_TEMP_DIR)).free)
    required = expected_size + int(Config.PARSE_TEMP_DISK_RESERVE_BYTES or 0)
    if free_bytes < required:
        raise OSError("解析临时卷剩余空间不足，无法创建安全源快照")

    source_fd = None
    scratch = Path(tempfile.mkdtemp(
        prefix="sjfx-source-p{}-".format(os.getpid()), dir=str(Config.PARSE_TEMP_DIR)
    ))
    lease_handle = None
    if os.name != "nt":
        try:
            import fcntl
            lease_handle = (scratch / ".lease").open("a+b")
            fcntl.flock(lease_handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            if lease_handle is not None:
                lease_handle.close()
            lease_handle = None
    original_name = str(file_node.get("name") or Path(str(file_node.get("path") or "")).name)
    if not original_name or Path(original_name).name != original_name or original_name in {".", ".."}:
        suffixes = "".join(Path(original_name or "source").suffixes[-2:])
        original_name = "source" + suffixes
    # Preserve the basename: format dispatch depends on compound extensions,
    # and sensitive-name policy must still recognise names such as `.env`.
    snapshot = scratch / original_name
    try:
        source_fd, opened_before = _open_inventory_source(scan_root, file_node)
        source_path = Path(scan_root) / str(file_node.get("path") or "")
        if _is_archive_node(file_node) and _source_has_open_writer(source_path):
            raise SourceFileChangedError(
                "压缩包仍被上传或复制程序以写入方式打开，请等待写入完成后重试"
            )
        observe_seconds = float(getattr(Config, "SOURCE_STABILITY_SECONDS", 0.0) or 0.0)
        if _is_archive_node(file_node) and observe_seconds > 0:
            deadline = time.monotonic() + observe_seconds
            while time.monotonic() < deadline:
                if cancel_check is not None and cancel_check():
                    raise ParseIsolationCancelled("任务已取消，停止检查源文件稳定性")
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            observed = os.fstat(source_fd)
            if (
                int(observed.st_dev), int(observed.st_ino),
                int(observed.st_size), int(observed.st_mtime_ns),
            ) != (
                int(opened_before.st_dev), int(opened_before.st_ino),
                int(opened_before.st_size), int(opened_before.st_mtime_ns),
            ):
                raise SourceFileChangedError("压缩包仍在写入，尚不能安全创建快照")
        copied = 0
        with os.fdopen(source_fd, "rb", closefd=True) as source, snapshot.open("xb") as target:
            source_fd = None
            while True:
                if cancel_check is not None and cancel_check():
                    raise ParseIsolationCancelled("任务已取消，停止创建安全源快照")
                chunk = source.read(4 * 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > expected_size:
                    raise SourceFileChangedError("源文件在快照期间增长，结果已拒绝")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
            opened_after = os.fstat(source.fileno())
        before_signature = (
            int(opened_before.st_dev), int(opened_before.st_ino),
            int(opened_before.st_size), int(opened_before.st_mtime_ns),
        )
        after_signature = (
            int(opened_after.st_dev), int(opened_after.st_ino),
            int(opened_after.st_size), int(opened_after.st_mtime_ns),
        )
        if copied != expected_size or before_signature != after_signature:
            raise SourceFileChangedError("源文件在快照期间发生变化，结果已拒绝")
        yield snapshot
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if lease_handle is not None:
            try:
                import fcntl
                fcntl.flock(lease_handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            lease_handle.close()
        shutil.rmtree(str(scratch), ignore_errors=True)


def _parse_with_limits(
    parser,
    path,
    node_path,
    mode,
    cancel_check=None,
    timeout_seconds=None,
):
    """Apply per-file wall-clock and current-process-memory guards.

    ``resource.getrusage(...).ru_maxrss`` is a *lifetime high-water mark*,
    not the Worker\'s current memory use.  Docling/OCR may legitimately push
    that high-water mark above the configured budget while loading a model;
    using it as a preflight check would then reject every later file instantly.
    On Linux read /proc/self/statm instead, which reports the current RSS.
    """
    timeout = max(
        1,
        int(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("MAX_PARSE_SECONDS", "300")
        ),
    )
    memory_limit = max(256, int(os.getenv("MAX_WORKER_MEMORY_MB", "8192"))) * 1024 * 1024
    rss = None
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        rss = resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        # Keep a conservative fallback for non-Linux development machines.
        try:
            import resource
            rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
        except (ImportError, AttributeError):
            rss = None
    if rss is not None and rss > memory_limit:
        raise MemoryError(
            "Worker 当前内存为 {} MB，已达到 {} MB 上限，跳过该文件".format(
                int(rss / (1024 * 1024)), memory_limit // (1024 * 1024)
            )
        )
    # A Python thread cannot be force-killed while native PDF/OCR code is
    # blocked.  Use a dedicated parser process for the real parser so a hard
    # timeout genuinely releases the Worker and any native resources.  Custom
    # parser doubles used by tests/extensions keep the lightweight thread
    # path, preserving their existing injection contract.
    isolate = str(os.getenv("ENABLE_PARSE_PROCESS_ISOLATION", "1")).strip().lower() not in {
        "0", "false", "no", "off", "disabled",
    }
    if isolate and isinstance(parser, UnifiedDocumentParser):
        try:
            return runner_for(parser).parse(
                path,
                node_path,
                mode=mode,
                timeout=timeout,
                memory_mb=max(256, int(os.getenv("MAX_PARSE_PROCESS_MEMORY_MB", os.getenv("MAX_WORKER_MEMORY_MB", "8192")))),
                cancel_check=cancel_check,
            )
        except ParseIsolationError:
            raise

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(parser.parse, path, node_path, mode=mode)
    try:
        deadline = time.monotonic() + timeout
        while True:
            if cancel_check is not None and cancel_check():
                future.cancel()
                raise ParseIsolationCancelled("任务已取消，当前解析已停止等待")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                raise TimeoutError("单文件解析超过 {} 秒，已标记为可恢复失败".format(timeout))
            try:
                return future.result(timeout=min(0.2, remaining))
            except FutureTimeoutError:
                continue
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError("单文件解析超过 {} 秒，已标记为可恢复失败".format(timeout)) from exc
    finally:
        # A native OCR call cannot always be force-killed safely. Do not block
        # the worker while it unwinds; the next parser call remains serialized
        # by UnifiedDocumentParser's internal lock.
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Compatibility with older maintenance runtimes.
            executor.shutdown(wait=False)


def _parse_with_timeout_fallback(parser, path, node_path, mode, cancel_check=None):
    """Parse deeply first and degrade only after a hard deep-parse timeout."""
    try:
        return _parse_with_limits(
            parser, path, node_path, mode, cancel_check=cancel_check
        )
    except ParseIsolationTimeout as timeout_error:
        if mode == "fast" or not isinstance(parser, UnifiedDocumentParser):
            raise
        fallback_timeout = max(
            30, int(os.getenv("MAX_PARSE_FALLBACK_SECONDS", "300"))
        )
        document = _parse_with_limits(
            parser,
            path,
            node_path,
            "fast",
            cancel_check=cancel_check,
            timeout_seconds=fallback_timeout,
        )
        document.setdefault("warnings", []).append(
            "深度解析超过 {} 秒，已自动切换快速解析器；结果可用但未包含全部版面增强。".format(
                int(os.getenv("MAX_PARSE_SECONDS", "300"))
            )
        )
        parser_meta = document.setdefault("parser", {})
        parser_meta["degraded"] = True
        parser_meta["mode"] = "fast-timeout-fallback"
        parser_meta["fallback_reason"] = "deep_parse_timeout"
        parser_meta["deep_parse_timeout"] = str(timeout_error)[:300]
        return document


_PARSE_THREAD_LOCAL = threading.local()


def _init_parallel_parser(template_parser, parser_registry, registry_lock):
    """Create one parser configuration per orchestration thread.

    The actual Docling/OCR runtime remains in a separately isolated child
    process (``runner_for``).  A parser instance is therefore never shared by
    concurrent threads, while each thread can reuse its child process for a
    batch of files instead of paying model startup cost for every file.
    """
    if isinstance(template_parser, UnifiedDocumentParser):
        parser = UnifiedDocumentParser(
            template_parser.artifacts_path,
            template_parser.rapidocr_model_dir,
            template_parser.max_chars,
            fast_office_ocr=template_parser.fast_office_ocr,
        )
    else:
        parser = template_parser
    _PARSE_THREAD_LOCAL.parser = parser
    with registry_lock:
        parser_registry.append(parser)


def _parallel_parse_files(
    parser,
    candidates,
    scan_root,
    mode,
    max_workers=1,
    cancel_check=None,
    on_complete=None,
    on_tick=None,
):
    """Parse independent files concurrently while preserving hard limits.

    This is intentionally a bounded orchestration pool, not unrestricted
    model concurrency.  Each pool thread owns one process-isolated parser;
    the local Ollama client is not involved in this stage and remains serial.
    Results are committed by the caller in the parent Worker, so SQLite writes
    and progress updates stay deterministic and transactional.
    """
    candidates = list(candidates or [])
    if not candidates:
        return []
    workers = max(1, min(8, int(max_workers or 1), len(candidates)))

    # Custom parser doubles and development extensions may not be safe to
    # clone.  Keep their established serial behaviour while real Docling
    # parsers use the bounded pool above.
    if workers <= 1 or not isinstance(parser, UnifiedDocumentParser):
        results = []
        for index, file_node in enumerate(candidates):
            if cancel_check is not None and cancel_check():
                raise ParseIsolationCancelled("任务已取消，停止提交新的解析文件")
            if on_tick is not None:
                on_tick(index, len(candidates), [file_node.get("path") or ""])
            try:
                with _secure_source_snapshot(scan_root, file_node, cancel_check) as snapshot:
                    document = _parse_with_timeout_fallback(
                        parser, snapshot, file_node["path"], mode, cancel_check=cancel_check
                    )
                _restore_source_provenance(document, scan_root, file_node)
                item = (index, file_node, document, None)
            except ParseIsolationCancelled:
                raise
            except Exception as exc:  # file-level failure is recoverable
                item = (index, file_node, None, exc)
            results.append(item)
            if on_complete is not None:
                on_complete(*item)
        return results

    parser_registry = []
    registry_lock = threading.Lock()
    executor = ThreadPoolExecutor(
        max_workers=workers,
        initializer=_init_parallel_parser,
        initargs=(parser, parser_registry, registry_lock),
        thread_name_prefix="sjfx-parse",
    )
    future_map = {}
    results = [None] * len(candidates)
    next_index = 0
    completed_count = 0

    def submit_one(index):
        file_node = candidates[index]

        def parse_task():
            child_parser = getattr(_PARSE_THREAD_LOCAL, "parser", parser)
            try:
                with _secure_source_snapshot(scan_root, file_node, cancel_check) as snapshot:
                    document = _parse_with_timeout_fallback(
                        child_parser,
                        snapshot,
                        file_node["path"],
                        mode,
                        cancel_check=cancel_check,
                    )
                _restore_source_provenance(document, scan_root, file_node)
                return index, file_node, document, None
            except ParseIsolationCancelled:
                raise
            except Exception as exc:
                return index, file_node, None, exc

        future_map[executor.submit(parse_task)] = index

    try:
        # Keep only a small queue ahead of the active workers.  This makes
        # cancellation responsive and avoids holding a large batch of paths in
        # native parser state when a user stops the job.
        while next_index < len(candidates) and len(future_map) < workers * 2:
            submit_one(next_index)
            next_index += 1
        while future_map:
            if cancel_check is not None and cancel_check():
                for future in future_map:
                    future.cancel()
                raise ParseIsolationCancelled("任务已取消，停止等待并行解析")
            if on_tick is not None:
                active_paths = [
                    candidates[index].get("path") or ""
                    for index in list(future_map.values())[:workers]
                ]
                on_tick(completed_count, len(candidates), active_paths)
            done, _pending = wait(tuple(future_map), timeout=0.25, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                future_map.pop(future, None)
                item = future.result()
                index, file_node, document, error = item
                results[index] = item
                completed_count += 1
                if on_complete is not None:
                    on_complete(index, file_node, document, error)
                if next_index < len(candidates):
                    submit_one(next_index)
                    next_index += 1
    finally:
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:  # Python 3.8 compatibility
            executor.shutdown(wait=True)
        # Terminate idle parser children now rather than retaining one process
        # per pool thread for the lifetime of the Worker.
        for owned_parser in parser_registry:
            if isinstance(owned_parser, UnifiedDocumentParser):
                try:
                    runner_for(owned_parser).close()
                except Exception:
                    pass
    return [item for item in results if item is not None]


def _walk_files(node):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.get("kind") == "file":
            yield current
            continue
        stack.extend(reversed(current.get("children", [])))


def _walk_directories(node):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.get("kind") != "directory":
            continue
        yield current
        stack.extend(reversed([child for child in current.get("children", []) if child.get("kind") == "directory"]))


def _tokens(text):
    value = str(text or "")
    tokens = [token.lower() for token in ENGLISH_WORD_RE.findall(value)]
    if jieba is not None:
        chinese = " ".join(CHINESE_TEXT_RE.findall(value))
        tokens.extend(
            token.strip() for token in jieba.cut(chinese, cut_all=False)
            if 2 <= len(token.strip()) <= 16
        )
    else:
        for term in CHINESE_DOMAIN_TERMS:
            tokens.extend([term] * min(8, value.count(term)))
        tokens.extend(token for token in WORD_RE.findall(value) if CHINESE_TEXT_RE.fullmatch(token))
    return [token for token in tokens if token not in STOPWORDS]


def _features(text):
    normalized = re.sub(r"\s+", "", (text or "").lower())
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    grams = [chinese[index:index + 3] for index in range(max(0, len(chinese) - 2))]
    words = _tokens(text)
    return (words + grams)[:100000]


def simhash64(text):
    vector = [0] * 64
    features = _features(text)
    if not features:
        return None
    counts = Counter(features)
    for token, weight in counts.items():
        digest = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        adjusted = min(6, 1 + int(math.log2(weight + 1)))
        for bit in range(64):
            vector[bit] += adjusted if digest & (1 << bit) else -adjusted
    value = 0
    for bit, score in enumerate(vector):
        if score >= 0:
            value |= 1 << bit
    return value


def _feature_containment(left_text, right_text):
    """Measure whether the shorter text's features are covered by the longer.

    This is intentionally a containment score, not a symmetric Jaccard score:
    a faithful abstract can contain most of its distinctive terms while the
    full document necessarily has additional terms.
    """
    left = set(_features(left_text))
    right = set(_features(right_text))
    smaller = min(len(left), len(right))
    if smaller < 20:
        return 0.0
    return len(left.intersection(right)) / float(smaller)


def _hamming(left, right):
    # Portable popcount; the supported project runtime baseline is Python 3.10+.
    return bin(left ^ right).count("1")


class _UnionFind:
    def __init__(self, values):
        self.parent = {value: value for value in values}

    def find(self, value):
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left, right):
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _group_exact(documents):
    groups = defaultdict(list)
    for path, doc in documents.items():
        groups[doc["source"]["sha256"]].append(path)

    def canonical(paths):
        def sort_key(path):
            source = documents[path].get("source", {})
            modified = source.get("modified_at") or "9999-12-31T23:59:59+00:00"
            return (modified, -int(source.get("size") or 0), path)
        return min(paths, key=sort_key)

    return [
        {
            "group_id": "DUP-{:04d}".format(index),
            "sha256": digest,
            "canonical": canonical(paths),
            "members": sorted(paths),
            "derived_from": {path: canonical(paths) for path in paths if path != canonical(paths)},
            "duplicate_count": len(paths) - 1,
        }
        for index, (digest, paths) in enumerate(sorted(groups.items()), 1) if len(paths) > 1
    ]


def _group_similar(documents, exact_groups, max_distance=8):
    exact_duplicate_paths = {
        member for group in exact_groups for member in group["members"] if member != group.get("canonical")
    }
    fingerprints = {}
    sizes = {}
    texts = {}
    for path, doc in documents.items():
        if path in exact_duplicate_paths:
            continue
        text = doc.get("text", "")
        if len(text.strip()) < 120:
            continue
        value = simhash64(text)
        if value is not None:
            fingerprints[path] = value
            sizes[path] = max(1, len(text))
            texts[path] = text
    union = _UnionFind(fingerprints)
    buckets = defaultdict(list)
    # Eight 8-bit bands improve recall for a short abstract versus its longer
    # source while keeping candidate comparisons bounded.
    for path, fingerprint in fingerprints.items():
        for band in range(8):
            buckets[(band, (fingerprint >> (band * 8)) & 0xFF)].append(path)
    checked = set()
    # A short abstract and its full source may have no identical 8-bit LSH
    # band after adding substantial new material.  Add a bounded inverted
    # index over each document's strongest features as a second candidate
    # generator; this avoids comparing every document pair.
    anchor_buckets = defaultdict(list)
    for path, text in texts.items():
        anchors = [token for token, _count in Counter(_features(text)[:20000]).most_common(64) if len(token) >= 3]
        for token in anchors:
            if len(anchor_buckets[token]) < 256:
                anchor_buckets[token].append(path)

    candidate_buckets = list(buckets.values())
    candidate_buckets.extend(value for value in anchor_buckets.values() if len(value) <= 256)
    for paths in candidate_buckets:
        for index, left in enumerate(paths):
            for right in paths[index + 1:]:
                pair = tuple(sorted((left, right)))
                if pair in checked:
                    continue
                checked.add(pair)
                ratio = min(sizes[left], sizes[right]) / float(max(sizes[left], sizes[right]))
                containment = 0.0
                if ratio < 0.55:
                    containment = _feature_containment(texts[left], texts[right])
                distance_limit = max_distance + 6 if containment >= 0.80 else max_distance
                if (ratio >= 0.55 or containment >= 0.80) and _hamming(fingerprints[left], fingerprints[right]) <= distance_limit:
                    union.union(left, right)
    groups = defaultdict(list)
    for path in fingerprints:
        groups[union.find(path)].append(path)
    output = []
    for paths in sorted((sorted(value) for value in groups.values() if len(value) > 1), key=lambda value: (-len(value), value)):
        distances = []
        anchor = max(paths, key=lambda path: (sizes[path], path))
        for path in paths[1:]:
            distances.append(_hamming(fingerprints[anchor], fingerprints[path]))
        output.append({
            "cluster_id": "SIM-{:04d}".format(len(output) + 1),
            "representative": anchor,
            "canonical": anchor,
            "members": paths,
            "derived_from": {path: anchor for path in paths if path != anchor},
            "max_hamming_distance_to_representative": max(distances or [0]),
            "method": "64-bit SimHash + 8-band LSH + 特征包含率",
        })
    return output


def _document_topics(document, limit=8):
    source = document.get("source", {})
    structure = document.get("structure", {})
    sample = " ".join([
        Path(source.get("name", "")).stem,
        " ".join(structure.get("headings", [])[:30]),
        document.get("text", "")[:12000],
    ])
    return [word for word, _ in Counter(_tokens(sample)).most_common(limit)]


def _content_topics(document, limit=8):
    """Extract topics only from parsed content, never from file metadata."""
    structure = document.get("structure", {})
    text = str(document.get("text") or "")
    # A bounded head/middle/tail sample avoids classifying a long report only
    # by its introduction while keeping package-wide topic extraction cheap.
    if len(text) > 24000:
        head = 10000
        middle = 7000
        tail = 7000
        midpoint = max(head, (len(text) - middle) // 2)
        text = "\n".join((
            text[:head],
            text[midpoint:midpoint + middle],
            text[-tail:],
        ))
    sample = " ".join([
        " ".join(structure.get("headings", [])[:80]),
        text,
    ])
    return [word for word, _ in Counter(_tokens(sample)).most_common(limit)]


def _looks_like_structured_content(text):
    """Recognise delimited rows or JSON-like records from the extracted text."""
    text = (text or "").strip()
    if not text:
        return False
    if text[:1] in {"{", "["} and (text.count("\":") >= 2 or text.count("':") >= 2):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()][:50]
    if len(lines) < 3:
        return False
    for delimiter in (",", "\t", "|"):
        widths = [line.count(delimiter) for line in lines]
        repeated_widths = Counter(width for width in widths if width > 0)
        if repeated_widths and repeated_widths.most_common(1)[0][1] >= 3:
            return True
    return False


def _document_role_details(document):
    """Classify by several independent signals, not one incidental phrase."""
    structure = document.get("structure", {})
    headings = " ".join(structure.get("headings", [])[:20]).lower()
    body = document.get("text", "")[:16000].lower()
    filename = Path(document.get("source", {}).get("name", "")).stem.lower()
    sample = " ".join((filename, headings, body))
    requirement_terms = ("交付要求", "验收标准", "产品定位", "用户故事", "环境约束", "实施要求", "功能要求", "需求规格", "招标文件")
    weak_requirement_terms = ("技术方案", "系统设计", "实施方案", "项目说明")
    research_terms = ("摘要", "关键词", "参考文献", "研究方法", "实验结果", "conclusion", "abstract", "references", "methodology")
    data_terms = ("销售额", "增长率", "统计", "样本量", "指标", "合计", "平均值", "dataset")
    if not sample.strip():
        return "内容待识别", {"reason": "未提取到可分类正文", "scores": {}}
    if "数据包情况概览报告" in sample or "情况概览报告" in sample:
        return "派生概览材料", {"reason": "正文含本系统生成报告标识", "scores": {"派生概览材料": 99}}

    def weighted_score(terms):
        score = 0
        hits = []
        for term in terms:
            if term in headings or term in filename:
                score += 3
                hits.append(term)
            elif term in body:
                score += 1
                hits.append(term)
        return score, hits

    requirement_score, requirement_hits = weighted_score(requirement_terms)
    weak_requirement_score, weak_requirement_hits = weighted_score(weak_requirement_terms)
    research_score, research_hits = weighted_score(research_terms)
    data_score, data_hits = weighted_score(data_terms)
    structured = _looks_like_structured_content(document.get("text", ""))
    if structured:
        data_score += 5
    scores = {
        "要求与说明材料": requirement_score + min(2, weak_requirement_score),
        "研究文献": research_score,
        "结构化数据": data_score,
    }
    # Data-shaped content is the strongest deterministic signal.  A narrative
    # research paper merely mentioning an acceptance criterion must not be
    # pulled into requirements because of one weak match.
    if structured or data_score >= 5:
        role = "结构化数据"
        hits = data_hits + (["表格/记录结构"] if structured else [])
    elif research_score >= 4 and research_score >= scores["要求与说明材料"]:
        role = "研究文献"
        hits = research_hits
    elif requirement_score >= 3 and scores["要求与说明材料"] >= research_score + 2:
        role = "要求与说明材料"
        hits = requirement_hits + weak_requirement_hits
    else:
        role = "一般资料"
        hits = requirement_hits + weak_requirement_hits + research_hits + data_hits
    reason = "分类信号：{}".format("、".join(dict.fromkeys(hits) or ["无足够稳定信号"]))
    return role, {"reason": reason, "scores": scores}


def _document_role(document):
    """Compatibility wrapper for callers that only need the role label."""
    return _document_role_details(document)[0]


def _first_evidence(document):
    for evidence in document.get("evidence", []):
        # The first extracted unit is often a title or heading.  It is useful
        # for navigation but cannot support a conclusion by itself.  Pick the
        # first eligible body unit so directory/topic summaries do not bypass
        # the shared evidence-quality gate.
        if evidence.get("text") and evidence_quality(evidence).get("eligible"):
            result = {
                "evidence_id": evidence.get("evidence_id"),
                "source_path": evidence.get("source_path"),
                "page": evidence.get("page"),
                "section": evidence.get("section"),
                "text": evidence.get("text", "")[:300],
                "source_sha256": evidence.get("source_sha256"),
                "archive_source_path": evidence.get("archive_source_path"),
                "archive_member": evidence.get("archive_member"),
            }

            topics = document.get("structure", {}).get("headings", [])[:8]

            if document.get("structure", {}).get("title"):
                topics = [
                    document["structure"]["title"]
                ] + topics

            result.update(
                evidence_support(
                    evidence,
                    topics=topics
                )
            )
            result["evidence_quality"] = evidence_quality(evidence)

            return result

    return {
        "evidence_id": None,
        "source_path": document.get("source", {}).get("path"),
        "page": None,
        "section": None,
        "text": "该文件仅有元数据或未提取到可引用正文。",
        "source_sha256": document.get("source", {}).get("sha256"),
    }


def _paths_under(directory_path, document_paths):
    if directory_path == ".":
        return list(document_paths)
    prefix = directory_path.rstrip("/") + "/"
    return [path for path in document_paths if path == directory_path or path.startswith(prefix)]


def _node_summary(node, documents):
    paths = _paths_under(node["path"], documents.keys())
    node_docs = [documents[path] for path in paths]
    topic_counts = Counter()
    degraded = 0
    page_total = 0
    table_total = 0
    image_total = 0
    for document in node_docs:
        topic_counts.update(_document_topics(document, 5))
        degraded += int(bool(document.get("parser", {}).get("degraded")))
        structure = document.get("structure", {})
        page_total += int(structure.get("page_count") or 0)
        table_total += int(structure.get("table_count") or 0)
        image_total += int(structure.get("picture_count") or 0)
    representatives = sorted(node_docs, key=lambda doc: (len(doc.get("text", "")), doc.get("source", {}).get("size", 0)), reverse=True)[:3]
    topics = [item for item, _ in topic_counts.most_common(8)]
    summary = (
        "本节点递归包含 {files} 个文件、{dirs} 个子目录，总大小 {size}。"
        "统一解析识别到约 {pages} 页/张、{tables} 个表格、{images} 个图片对象。"
        "高频内容线索为：{topics}。"
    ).format(
        files=node.get("file_count", len(paths)), dirs=node.get("directory_count", 0),
        size=node.get("size_human", human_size(node.get("total_size", 0))), pages=page_total,
        tables=table_total, images=image_total, topics="、".join(topics) if topics else "暂未形成稳定主题",
    )
    if degraded:
        summary += "其中 {} 个文件使用了兼容解析或仅保留元数据，已在告警中标记。".format(degraded)
    return {
        "schema_version": 4,
        "summary_type": "folder",
        "node_path": node["path"],
        "title": "{} 节点摘要".format(node.get("name", node["path"])),
        "summary": summary,
        "topics": topics,
        "statistics": {
            "file_count": node.get("file_count", len(paths)),
            "directory_count": node.get("directory_count", 0),
            "total_size": node.get("total_size", 0),
            "page_count": page_total,
            "table_count": table_total,
            "picture_count": image_total,
            "degraded_document_count": degraded,
        },
        "representative_documents": [doc.get("source", {}).get("path") for doc in representatives],
        "evidence_chain": [_first_evidence(doc) for doc in representatives],
        "generated_by": "local-unified-parser",
        "generated_at": _now(),
    }


def _file_summary(path, document):
    source = document.get("source", {})
    structure = document.get("structure", {})
    evidence = [item for item in document.get("evidence", []) if evidence_quality(item).get("eligible")][:3]
    headings = structure.get("headings", [])[:2]
    first_text = next((item.get("text", "") for item in evidence if item.get("text")), "")
    coverage = document.get("coverage") or {}
    summary = "{} 文件，大小 {}，解析器为 {}。".format(
        source.get("extension") or "未知格式",
        human_size(source.get("size", 0)),
        document.get("parser", {}).get("name", "未知"),
    )
    if structure.get("page_count"):
        summary += "共 {} 页/张。".format(structure["page_count"])
    if headings:
        summary += "结构线索：{}。".format("、".join(headings))
    if first_text:
        summary += "内容线索：{}".format(first_text[:260])
    if document.get("warnings"):
        summary += "该文件存在解析告警，引用前应复核。"
    duplicate = document.get("deduplication") or {}
    if duplicate.get("role") == "duplicate_alias":
        summary += "该文件是 {} 的字节级重复副本，不重复参与主题与价值计算。".format(duplicate.get("canonical_path"))
    return {
        "schema_version": 4,
        "summary_type": "file",
        "node_path": path,
        "title": structure.get("title") or Path(path).stem,
        "summary": summary,
        "topics": _document_topics(document, 8),
        "structure_overview": structure,
        "coverage": coverage,
        "representative_evidence": evidence[:1],
        "deep_analysis_recommended": bool(
            evidence and (not coverage.get("complete", True) or len(str(document.get("text") or "")) >= 8000)
        ),
        "deduplication": duplicate,
        "representative_documents": [path],
        "evidence_chain": evidence,
        "generated_by": "local-unified-parser",
        "generated_at": _now(),
    }


def _inventory_file_summary(path, file_node, state="pending"):
    """Create an immediate metadata summary before content parsing finishes."""
    size = int(file_node.get("size") or 0)
    extension = file_node.get("extension") or Path(path).suffix.lower() or "未知格式"
    status_text = "等待内容解析" if state == "pending" else "内容解析失败，可在任务中心重试"
    return {
        "schema_version": 4,
        "summary_type": "file",
        "node_path": path,
        "title": Path(path).stem,
        "summary": "{} 文件，大小 {}，已完成原始盘点；当前{}。".format(
            extension, human_size(size), status_text
        ),
        "topics": [],
        "structure_overview": {"title": Path(path).stem, "headings": []},
        "coverage": {
            "complete": False,
            "coverage_ratio": 0.0,
            "status": state,
        },
        "representative_documents": [path],
        "representative_evidence": [],
        "evidence_chain": [],
        "deep_analysis_recommended": state != "failed",
        "generated_by": "local-inventory",
        "generated_at": _now(),
    }


def _needs_local_file_summary(document):
    """Every parsed file receives a bounded local summary without an LLM call."""
    return bool(document)


def _stable_group_node_id(dimension, name, member_paths):
    """
    给分析树中的虚拟节点生成一个稳定的唯一 ID。

    后面用户点击“某个主题”时，
    后端可以通过这个 node_id 找到对应节点。
    """
    # Display names are model-enhanced and may legitimately improve between
    # runs. They must not invalidate selections, locks or edit history.
    payload = "{}|{}".format(
        dimension or "",
        "|".join(sorted(member_paths or [])),
    )

    return "group-{}".format(
        hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()[:16]
    )


def _node_evidence(documents, member_paths, topics=None, max_items=6):
    """Select a bounded, traceable evidence set for one virtual analysis node."""
    candidates = []
    for path in member_paths:
        candidates.extend(documents[path].get("evidence", []))
    selected = select_evidence(
        candidates,
        topics=topics or [],
        max_items=max_items,
        per_source=2,
        max_chars=520,
    )
    if topics:
        return [item for item in selected if item.get("support_status") == "supported"]
    return selected


def _evidence_diversity_metrics(items):
    """Expose real evidence diversity instead of an inflated reference count."""
    evidence_ids = {
        str(item.get("evidence_id")) for item in items or []
        if isinstance(item, dict) and item.get("evidence_id")
    }
    sources = {
        str(item.get("source_sha256") or item.get("archive_source_path") or item.get("source_path"))
        for item in items or []
        if isinstance(item, dict)
        and (item.get("source_sha256") or item.get("archive_source_path") or item.get("source_path"))
    }
    return {
        "evidence_reference_count": len(list(items or [])),
        "unique_evidence_count": len(evidence_ids),
        "independent_source_count": len(sources),
    }


def _localized_subtopic_term(term):
    value = str(term or "").strip()
    if re.search(r"[\u4e00-\u9fff]", value):
        return value
    return {
        "vulnerability": "漏洞", "exploit": "漏洞利用", "attack": "攻击技术",
        "malware": "恶意软件", "threat": "威胁情报", "detection": "威胁检测",
        "privacy": "隐私保护", "network": "网络安全", "firmware": "固件安全",
        "authentication": "身份认证", "memory": "内存安全", "cloud": "云安全",
        "cve": "漏洞情报", "intrusion": "入侵检测", "encryption": "加密技术",
        "cryptography": "密码技术", "iot": "物联网安全", "android": "移动端安全",
        "vehicle": "车联网安全", "automotive": "车联网安全", "can": "车载CAN安全",
        "sidechannel": "侧信道安全", "side-channel": "侧信道安全",
        "enclave": "可信执行环境", "tee": "可信执行环境", "sgx": "SGX安全",
        "protocol": "协议安全", "adversarial": "对抗安全", "blockchain": "区块链安全",
        "amd": "AMD机密计算", "sev-snp": "SEV-SNP安全", "stackwarp": "栈安全",
        "pointer": "指针安全", "computing": "机密计算", "confidential": "机密计算",
        "trusted": "可信计算", "attestation": "可信证明", "gpu": "GPU安全",
    }.get(value.lower(), "专题（{}）".format(value[:12].upper() or "待命名"))


def _subtopic_partitions(topic_name, member_paths, documents):
    """Reuse existing content-topic signals to make one topic explorable.

    This deliberately does not introduce a second clustering model.  It assigns
    each document once, using the already extracted content topics; when the
    evidence is too sparse, it keeps a single honest "core material" branch.
    """
    member_paths = sorted(member_paths)
    topics_by_path = {
        path: _content_topics(documents[path], 10)
        for path in member_paths
    }
    frequencies = Counter()
    for values in topics_by_path.values():
        frequencies.update(set(values))

    topic_terms = set(_tokens(topic_name))
    candidates = [
        term for term, count in frequencies.items()
        if term not in topic_terms
        and count >= 2
        and len(term) >= 2
        and count < len(member_paths)
    ]
    candidates.sort(key=lambda term: (-frequencies[term], term))

    remaining = set(member_paths)
    partitions = []
    for term in candidates:
        paths = [
            path for path in sorted(remaining)
            if term in topics_by_path[path]
        ]
        if len(paths) < 2:
            continue
        partitions.append({
            "name": "{}研究资料".format(_localized_subtopic_term(term)),
            "paths": paths,
            "topics": [term],
        })
        remaining.difference_update(paths)
        if len(partitions) >= 4:
            break

    if remaining:
        partitions.append({
            "name": "主题核心资料" if not partitions else "其他关联资料",
            "paths": sorted(remaining),
            "topics": [],
        })
    if not partitions:
        partitions = [{
            "name": "主题核心资料",
            "paths": member_paths,
            "topics": [],
        }]
    return partitions


def _enrich_analysis_tree(tree, documents):
    """Turn topic -> document into topic -> subtopic -> document -> evidence."""
    for topic_node in tree.get("children", []):
        if topic_node.get("kind") != "group":
            continue
        member_paths = [
            path for path in topic_node.get("member_paths", [])
            if path in documents
        ]
        if not member_paths:
            continue

        original_files = {
            child.get("path"): child
            for child in topic_node.get("children", [])
            if child.get("kind") == "file" and child.get("path") in documents
        }
        if not original_files:
            continue

        topic_name = topic_node.get("name") or "未命名主题"
        topic_node["node_type"] = "topic"
        topic_node["representative_documents"] = sorted(
            member_paths,
            key=lambda path: len(documents[path].get("text", "")),
            reverse=True,
        )[:5]
        topic_node["evidence_chain"] = _node_evidence(
            documents,
            member_paths,
            topics=[topic_name] + list(topic_node.get("related_topics", [])),
            max_items=8,
        )
        topic_node.update(_evidence_diversity_metrics(topic_node["evidence_chain"]))

        subtopic_nodes = []
        conclusion_evidence = []
        for partition in _subtopic_partitions(topic_name, member_paths, documents):
            paths = partition["paths"]
            subtopic_name = partition["name"]
            evidence = _node_evidence(
                documents,
                paths,
                topics=[topic_name, subtopic_name] + partition["topics"],
                max_items=5,
            )
            confidence = "高" if len(paths) >= 3 else "中" if len(paths) >= 2 else "低"
            analysis_question = "“{}”主题中的“{}”具体讨论了什么，现有资料能够形成哪些可回查判断？".format(
                topic_name, subtopic_name
            )
            question_value = (
                "该问题用于判断“{}”是否构成独立分析方向，以及资料是否足以支持后续深入研判。"
            ).format(subtopic_name)
            supporting_quotes = [
                " ".join(str(item.get("supporting_quote") or item.get("text") or "").split())
                for item in evidence
                if str(item.get("supporting_quote") or item.get("text") or "").strip()
            ]
            answer = (
                "基于当前正文证据，可谨慎回答：{}".format(supporting_quotes[0][:420])
                if supporting_quotes
                else "证据不足，当前不能形成可靠回答。"
            )
            claim_statement = supporting_quotes[0][:420] if supporting_quotes else ""
            verified_evidence = []
            for evidence_item in evidence:
                verification = verify_claim_evidence(claim_statement, evidence_item)
                if verification.get("support_status") in {"supported", "partially_supported"}:
                    verified_item = dict(evidence_item)
                    verified_item.update(verification)
                    verified_evidence.append(verified_item)
            evidence = verified_evidence
            claim_status = (
                "supported"
                if any(item.get("support_status") == "supported" for item in evidence)
                else "partially_supported" if evidence else "insufficient"
            )
            if claim_status == "insufficient":
                answer = "证据不足，当前不能形成可靠回答。"
            conclusion = {
                # A traceable analysis unit is a valuable question, its answer,
                # and the evidence that directly supports that answer.
                "analysis_question": analysis_question,
                "question_value": question_value,
                "question": analysis_question,
                "value": question_value,
                "answer": answer,
                "statement": claim_statement or answer,
                "type": "问题—回答—证据",
                "confidence": confidence,
                "basis": "回答来自该子方向内文件的正文主题聚合，并由下列可回查原文证据直接支撑。",
                "evidence": evidence,
                "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
                "claims": [{
                    "statement": claim_statement or answer,
                    "type": "inference",
                    "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
                    "support_status": claim_status,
                }],
                "evidence_status": claim_status,
                "evidence_contract": "question-answer-evidence/3.0",
                "limitations": (
                    [] if claim_status == "supported"
                    else ["当前子方向只有部分证据支撑，需要人工复核。"] if claim_status == "partially_supported"
                    else ["当前子方向没有达到有效正文证据门槛，不能据此形成可靠结论。"]
                ),
            }
            conclusion_evidence.append(conclusion)

            file_nodes = []
            for path in paths:
                file_node = dict(original_files[path])
                document = documents[path]
                file_evidence = _node_evidence(
                    documents,
                    [path],
                    topics=[topic_name, subtopic_name] + _content_topics(document, 5),
                    max_items=3,
                )
                file_node["children"] = [
                    {
                        "kind": "evidence",
                        "name": "证据{}{}".format(
                            " · 第 {} 页".format(item["page"]) if item.get("page") else "",
                            " · {}".format(item.get("evidence_id")) if item.get("evidence_id") else "",
                        ),
                        "source_path": path,
                        "summary": item.get("text", ""),
                        "evidence": item,
                    }
                    for item in file_evidence
                ]
                file_nodes.append(file_node)

            subtopic_node = {
                "kind": "group",
                "node_type": "subtopic",
                "node_id": _stable_group_node_id("子方向", "{}|{}".format(topic_name, subtopic_name), paths),
                "dimension": "研究方向",
                "name": subtopic_name,
                "naming_source": "local_fallback",
                "naming_status": "degraded",
                "naming_degradation_reason": "尚未获得通过中文质量校验的模型命名",
                "summary": "“{}”是“{}”下可独立下钻的子方向，包含 {} 个文件。".format(
                    subtopic_name, topic_name, len(paths)
                ),
                "member_paths": paths,
                "file_count": len(paths),
                "total_size": sum(int(documents[path].get("source", {}).get("size") or 0) for path in paths),
                "total_size_human": human_size(sum(int(documents[path].get("source", {}).get("size") or 0) for path in paths)),
                "related_topics": partition["topics"],
                "representative_documents": sorted(paths, key=lambda path: len(documents[path].get("text", "")), reverse=True)[:3],
                "evidence_chain": evidence,
                "conclusion_evidence": [conclusion],
                "children": file_nodes,
            }
            subtopic_node.update(_evidence_diversity_metrics(evidence))
            subtopic_nodes.append(subtopic_node)

        topic_node["conclusion_evidence"] = conclusion_evidence
        topic_node["children"] = subtopic_nodes
    return tree


def _expand_duplicate_aliases_in_tree(
    tree, documents, canonical_by_path, aliases_by_canonical, node_summaries
):
    """Restore every physical path after canonical-only semantic analysis."""
    def annotate_file(node, path, canonical):
        payload = dict(node)
        duplicate = documents.get(path, {}).get("deduplication") or {}
        source = documents.get(path, {}).get("source") or {}
        payload.update({
            "name": Path(path).name,
            "path": path,
            "canonical_path": canonical,
            "duplicate_role": duplicate.get("role", "canonical"),
            "duplicate_of": duplicate.get("duplicate_of"),
            "duplicate_aliases": duplicate.get("aliases", []),
            "size": int(source.get("size") or payload.get("size") or 0),
            "size_human": human_size(int(source.get("size") or payload.get("size") or 0)),
            "summary": (node_summaries.get(path) or {}).get("summary") or payload.get("summary"),
        })
        if path != canonical:
            payload["children"] = []
            payload["evidence_ids"] = []
        return payload

    def visit(node):
        children = []
        for child in node.get("children") or []:
            if child.get("kind") == "file":
                canonical = canonical_by_path.get(child.get("path"), child.get("path"))
                children.append(annotate_file(child, canonical, canonical))
                for alias in aliases_by_canonical.get(canonical, []):
                    children.append(annotate_file(child, alias, canonical))
            else:
                visit(child)
                children.append(child)
        node["children"] = children
        if node.get("kind") == "group":
            canonical_paths = list(dict.fromkeys(node.get("member_paths") or []))
            expanded = []
            for canonical in canonical_paths:
                expanded.append(canonical)
                expanded.extend(aliases_by_canonical.get(canonical, []))
            node["member_paths"] = list(dict.fromkeys(expanded))
            node["canonical_file_count"] = len(canonical_paths)
            node["duplicate_alias_count"] = max(0, len(node["member_paths"]) - len(canonical_paths))
            node["file_count"] = len(node["member_paths"])
    visit(tree)
    tree["deduplication"] = {
        "analysis_uses_canonical_documents": True,
        "canonical_document_count": len(set(canonical_by_path.values())),
        "original_path_count": len(canonical_by_path),
    }
    return tree


def _annotate_physical_tree_deduplication(tree, canonical_by_path, documents, node_summaries):
    """Keep every physical path and expose its canonical relationship."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.get("kind") == "file" and node.get("path") in canonical_by_path:
            path = node["path"]
            canonical = canonical_by_path[path]
            duplicate = (documents.get(path) or {}).get("deduplication") or {}
            node.update({
                "canonical_path": canonical,
                "duplicate_role": duplicate.get("role", "canonical"),
                "duplicate_of": duplicate.get("duplicate_of"),
                "duplicate_aliases": duplicate.get("aliases", []),
                "simple_summary": (node_summaries.get(path) or {}).get("summary"),
            })
        stack.extend(reversed(node.get("children") or []))
    return tree


def _name_subtopic_nodes(tree, documents, llm):
    """Use the existing local model only to improve labels, never memberships.

    Membership, evidence selection and node ids remain deterministic.  The model
    receives representative text solely to replace terse lexical labels such as
    "security相关资料" with a reader-facing research direction.
    """
    if llm is None:
        return tree, None

    descriptors = []
    nodes = {}
    for topic in tree.get("children", []):
        if topic.get("kind") != "group":
            continue
        for subtopic in topic.get("children", []):
            if subtopic.get("kind") != "group":
                continue
            node_id = subtopic.get("node_id")
            if not node_id:
                continue
            nodes[node_id] = (topic, subtopic)
            samples = []
            for path in subtopic.get("representative_documents", [])[:3]:
                if path not in documents:
                    continue
                samples.append(_semantic_document_profile(path, documents[path], max_chars=520))
            descriptors.append({
                "node_id": node_id,
                "topic": topic.get("name"),
                "current_name": subtopic.get("name"),
                "file_count": subtopic.get("file_count"),
                "representative_material": samples,
                "evidence": [
                    {
                        "evidence_id": item.get("evidence_id"),
                        "source_path": item.get("source_path"),
                        "page": item.get("page"),
                        "text": " ".join(str(item.get("text") or "").split())[:420],
                    }
                    for item in subtopic.get("evidence_chain", [])[:3]
                ],
            })
    if not descriptors:
        return tree, None

    prompt = """你正在改善未知数据包中“主题下的子方向”名称。
成员文件、证据和 node_id 已由本地算法确定，绝不能改变成员、合并节点或编造材料外事实。
请把过泛的词（例如 security相关资料、can相关资料）改成用户能理解的中文研究方向。

要求：
1. name 应为 4-22 个字，必要时保留 TEE、SEV-SNP、RFC 等专有名词。
2. summary 一句话说明该子方向关注什么。
3. question 是一个值得分析、且只能依据当前子方向资料回答的问题；不要问泛泛的“这是什么”。
4. answer 是对 question 的谨慎回答，不得加入证据没有支持的具体事实。
4. 必须保留 node_id，返回每个 node_id 一条结果。

输入节点：
{}

输出 JSON：
{{"subtopics":[{{"node_id":"group-...","name":"子方向名称","summary":"一句话说明","question":"有价值的分析问题","answer":"有证据支撑的谨慎回答"}}]}}""".format(
        json.dumps(descriptors, ensure_ascii=False)
    )
    try:
        result = llm.chat_json(
            "你是严谨的情报资料目录组织助手。只根据给定代表材料和证据改善目录名称。",
            prompt,
            max_tokens=1800,
            strict=True,
            retries=0,
            timeout=180,
            required_fields=("subtopics",),
            output_context="子方向命名",
        )
        named = {
            item.get("node_id"): item
            for item in result.get("json", {}).get("subtopics", [])
            if isinstance(item, dict) and item.get("node_id") in nodes
        }
        for node_id, item in named.items():
            topic, subtopic = nodes[node_id]
            name = _valid_semantic_name(item.get("name"))
            if name:
                subtopic["name"] = name
                subtopic["naming_source"] = "local_model"
                subtopic["naming_status"] = "enhanced"
                subtopic.pop("naming_degradation_reason", None)
            else:
                subtopic["naming_degradation_reason"] = "模型子方向名称未通过中文质量校验，已保留本地名称"
            summary = str(item.get("summary") or subtopic.get("summary") or "").strip()[:300]
            if summary:
                subtopic["summary"] = summary
            question = str(item.get("question") or "").strip()[:300]
            answer = str(item.get("answer") or item.get("conclusion") or "").strip()[:360]
            if subtopic.get("conclusion_evidence"):
                unit = subtopic["conclusion_evidence"][0]
                if question:
                    unit["analysis_question"] = question
                if answer:
                    verified = []
                    for evidence_item in unit.get("evidence") or []:
                        verification = verify_claim_evidence(answer, evidence_item)
                        if verification.get("support_status") == "supported":
                            supported_item = dict(evidence_item)
                            supported_item.update(verification)
                            verified.append(supported_item)
                    if verified:
                        unit["answer"] = answer
                        unit["statement"] = answer
                        unit["evidence"] = verified
                        unit["evidence_ids"] = [
                            evidence_item.get("evidence_id")
                            for evidence_item in verified
                            if evidence_item.get("evidence_id")
                        ]
                        unit["evidence_status"] = "supported"
                        unit["claims"] = [{
                            "statement": answer,
                            "type": "inference",
                            "evidence_ids": unit["evidence_ids"],
                            "support_status": "supported",
                        }]
                    else:
                        unit["answer"] = "证据不足，模型生成的回答未通过原文支撑校验。"
                        unit["statement"] = unit["answer"]
                        unit["evidence_status"] = "insufficient"
                        unit["evidence"] = []
                        unit["evidence_ids"] = []
                        unit["claims"] = [{
                            "statement": unit["answer"],
                            "type": "inference",
                            "evidence_ids": [],
                            "support_status": "insufficient",
                        }]
                        unit.setdefault("limitations", []).append(
                            "模型回答没有找到真正支撑该表述的正文证据，已拒绝采用。"
                        )
                unit["basis"] = "该回答由本子方向的代表材料和下列可回查原文证据直接支撑。"
                for topic_conclusion in topic.get("conclusion_evidence", []):
                    if topic_conclusion.get("evidence") == unit.get("evidence"):
                        topic_conclusion.update(unit)
        return tree, result
    except Exception:
        return tree, None


def _adaptive_tree(scan, documents, node_summaries, enrich=True):
    """
    Theme-first adaptive analysis tree.

    一级目录只根据实际正文主题生成。
    document_role 仅作为文件标签保留，不再作为一级目录。
    """

    def stable_group_id(name, member_paths):
        return _stable_group_node_id("内容主题", name, member_paths)

    def file_leaf(path):
        doc = documents[path]
        source = doc.get("source", {})
        classification = doc.get("classification", {})

        return {
            "kind": "file",
            "name": Path(path).name,
            "path": path,

            "summary": (
                "；".join(
                    doc.get("structure", {}).get("headings", [])[:2]
                )
                or "已完成统一解析"
            ),

            "evidence_ids": [
                item.get("evidence_id")
                for item in doc.get("evidence", [])[:2]
                if item.get("evidence_id")
            ],

            "extension": source.get("extension"),
            "size": int(source.get("size") or 0),
            "size_human": human_size(
                int(source.get("size") or 0)
            ),

            # 内容类别现在只作为文件标签，
            # 不再用于一级目录。
            "content_category": classification.get(
                "document_role",
                "一般资料",
            ),

            "content_topics": _content_topics(doc, 5),
            "related_topics": _content_topics(doc, 10),

            "primary_topic": classification.get("primary_topic"),
            "topic_memberships": classification.get("topic_memberships", []),
            "classification_confidence": classification.get("confidence"),
            "classification_evidence_ids": classification.get("evidence_ids", []),

            "classification_reason": (
                classification.get("classification_reason")
                or classification.get("role_reason")
            ),
        }

    if not documents:
        return {
            "kind": "analysis_root",
            "name": Path(scan["root"]).name,
            "path": ".",
            "dimensions": [],
            "summary": node_summaries.get(".", {}).get("summary"),
            "children": [],
        }

    # ---------------------------------------------------------
    # 1. 每篇文档只从正文中提取主题
    # ---------------------------------------------------------

    topics_by_path = {
        path: _content_topics(document, 10)
        for path, document in documents.items()
    }

    document_frequency = Counter()

    for topics in topics_by_path.values():
        document_frequency.update(set(topics))

    total_documents = len(documents)

    # ---------------------------------------------------------
    # 2. 找跨文档重复出现、但又不是几乎所有文件
    #    都出现的泛化词。
    # ---------------------------------------------------------

    max_frequency = max(
        2,
        int(math.ceil(total_documents * 0.85))
    )

    shared_topics = [
        topic
        for topic, count in document_frequency.items()
        if count >= 2 and count <= max_frequency
    ]

    shared_topics.sort(
        key=lambda topic: (
            -document_frequency[topic],
            topic,
        )
    )

    if total_documents <= 10:
        max_topic_count = 6
    elif total_documents <= 50:
        max_topic_count = 8
    else:
        max_topic_count = 12

    shared_topics = set(
        shared_topics[:max_topic_count]
    )

    # ---------------------------------------------------------
    # 3. 每个文件只选择一个最主要主题
    # ---------------------------------------------------------

    assignments = defaultdict(list)
    unassigned = []

    for path, ranked_topics in topics_by_path.items():

        candidates = []

        for rank, topic in enumerate(ranked_topics):

            if topic not in shared_topics:
                continue

            frequency = document_frequency[topic]

            # 同时考虑：
            # 1. 跨文档出现次数
            # 2. 在当前文件中的主题排名
            score = frequency / float(rank + 1)

            candidates.append(
                (
                    score,
                    frequency,
                    -rank,
                    topic,
                )
            )

        if candidates:

            selected_topic = max(candidates)[3]
            best_score = max(item[0] for item in candidates)
            # A document may legitimately relate to more than one research
            # topic, but the primary tree is a partition: exactly one counting
            # parent.  Secondary topics remain non-counting metadata/refs.
            memberships = [
                item[3] for item in sorted(candidates, key=lambda item: (-item[0], item[3]))
                if item[0] >= best_score * 0.55
            ][:3]
            if selected_topic not in memberships:
                memberships.insert(0, selected_topic)
            assignments[selected_topic].append(path)
            alternatives = sorted((item[0] for item in candidates), reverse=True)
            margin = (
                (best_score - alternatives[1]) / float(max(1.0, best_score))
                if len(alternatives) > 1 else 1.0
            )
            classification = documents[path].setdefault("classification", {})
            classification.update({
                "primary_topic": selected_topic,
                "topic_memberships": memberships,
                "confidence": round(min(0.98, max(0.45, 0.55 + 0.35 * margin)), 3),
                "classification_status": "classified",
                "classification_reason": (
                    "主题来自标题、正文头中尾和跨文档频次；主主题在当前文件中的排序和跨文件覆盖率最高。"
                ),
                "evidence_ids": [
                    item.get("evidence_id")
                    for item in select_evidence(
                        documents[path].get("evidence", []),
                        topics=[selected_topic],
                        max_items=2,
                        per_source=1,
                    )
                    if item.get("support_status") == "supported" and item.get("evidence_id")
                ],
            })

        else:
            unassigned.append(path)
            documents[path].setdefault("classification", {}).update({
                "primary_topic": None,
                "confidence": 0.0,
                "classification_status": "unclassified",
                "classification_reason": "当前正文没有形成稳定的跨文档共享主题。",
                "evidence_ids": [],
                "topic_memberships": [],
            })

    # ---------------------------------------------------------
    # 4. 小数据包里避免制造大量单文件伪主题
    # ---------------------------------------------------------

    if total_documents > 6:

        singleton_topics = [
            topic
            for topic, paths in assignments.items()
            if len(paths) == 1
        ]

        for topic in singleton_topics:
            singleton_paths = assignments.pop(topic)
            unassigned.extend(singleton_paths)
            for path in singleton_paths:
                documents[path].setdefault("classification", {}).update({
                    "primary_topic": None,
                    "topic_memberships": [],
                    "confidence": 0.0,
                    "classification_status": "unclassified",
                    "classification_reason": "主题只在单个文件出现，未形成稳定的跨文档分类。",
                    "evidence_ids": [],
                })

    # ---------------------------------------------------------
    # 5. 给关键词主题补充少量共同关键词
    # ---------------------------------------------------------

    def topic_label(primary_topic, paths):

        related = Counter()

        for path in paths:
            related.update(
                _content_topics(
                    documents[path],
                    10,
                )
            )

        qualifiers = [
            word
            for word, count in related.most_common()
            if (
                word != primary_topic
                and count >= 2
                and len(word) >= 2
            )
        ][:2]

        if qualifiers:
            return " / ".join(
                [primary_topic] + qualifiers
            )

        return primary_topic

    # ---------------------------------------------------------
    # 6. 构造主题优先一级目录
    # ---------------------------------------------------------

    children = []

    for topic, member_paths in sorted(
        assignments.items(),
        key=lambda item: (
            -len(item[1]),
            item[0],
        ),
    ):

        member_paths = sorted(member_paths)

        visible_name = topic_label(
            topic,
            member_paths,
        )

        related = Counter()

        for member_path in member_paths:
            related.update(
                _content_topics(
                    documents[member_path],
                    8,
                )
            )

        related_topics = [
            word
            for word, _count
            in related.most_common(8)
        ]

        total_size = sum(
            int(
                documents[p]
                .get("source", {})
                .get("size")
                or 0
            )
            for p in member_paths
        )

        children.append({
            "kind": "group",

            "node_id": stable_group_id(
                visible_name,
                member_paths,
            ),

            "dimension": "内容主题",
            "classification_status": "classified",
            "topic_key": topic,

            "name": visible_name,

            "summary": (
                "根据统一解析后的正文自动形成主题“{}”，"
                "共包含 {} 个文件。"
            ).format(
                visible_name,
                len(member_paths),
            ),

            "member_paths": member_paths,

            "file_count": len(member_paths),

            "total_size": total_size,

            "total_size_human": human_size(
                total_size
            ),

            "related_topics": related_topics,
            "classification_confidence": round(
                sum(float((documents[path].get("classification") or {}).get("confidence") or 0.0) for path in member_paths)
                / float(len(member_paths) or 1),
                3,
            ),
            "classification_evidence_ids": list(dict.fromkeys(
                evidence_id
                for path in member_paths
                for evidence_id in ((documents[path].get("classification") or {}).get("evidence_ids") or [])
            ))[:12],

            "children": [
                file_leaf(p)
                for p in member_paths
            ],
        })

    # ---------------------------------------------------------
    # 7. 暂时无法稳定归入共享主题的文件
    # ---------------------------------------------------------

    if unassigned:

        member_paths = sorted(
            set(unassigned)
        )

        total_size = sum(
            int(
                documents[p]
                .get("source", {})
                .get("size")
                or 0
            )
            for p in member_paths
        )

        fallback_name = (
            "其他内容"
            if children
            else "内容待识别"
        )

        children.append({
            "kind": "group",

            "node_id": stable_group_id(
                fallback_name,
                member_paths,
            ),

            "dimension": "内容主题",
            "classification_status": "unclassified",

            "name": fallback_name,

            "summary": (
                "这些文件暂未形成稳定的跨文档共享主题，"
                "共 {} 个文件。"
            ).format(
                len(member_paths)
            ),

            "member_paths": member_paths,

            "file_count": len(member_paths),

            "total_size": total_size,

            "total_size_human": human_size(
                total_size
            ),

            "related_topics": [],
            "classification_confidence": 0.0,
            "classification_evidence_ids": [],

            "children": [
                file_leaf(p)
                for p in member_paths
            ],
        })

    dimensions = [{
        "name": "内容主题",

        "reason": (
            "根据实际解析正文中的跨文档主题自动形成一级目录；"
            "文件角色仅作为标签保留，不再作为一级分类。"
        ),

        "cardinality": len(children),
    }]

    tree = {
        "kind": "analysis_root",

        "name": Path(scan["root"]).name,

        "path": ".",

        "dimensions": dimensions,

        "summary": node_summaries.get(
            ".",
            {},
        ).get("summary"),

        "children": children,
    }
    return _enrich_analysis_tree(tree, documents) if enrich else tree

def _cluster_label(primary_topic, paths, documents):
    """Use a small co-occurring keyword set instead of a bare token label."""
    related = Counter()
    for path in paths:
        related.update(_document_topics(documents[path], 10))
    qualifiers = [
        word for word, count in related.most_common()
        if word != primary_topic and count >= 2 and len(word) >= 2
    ][:2]
    return " / ".join([primary_topic] + qualifiers) if qualifiers else primary_topic


def _topic_clusters(documents):
    inverted = defaultdict(list)
    for path, document in documents.items():
        for topic in _document_topics(document, 5):
            inverted[topic].append(path)
    clusters = []
    used_sets = set()
    for topic, paths in sorted(inverted.items(), key=lambda item: (-len(set(item[1])), item[0])):
        unique = tuple(sorted(set(paths)))
        if len(unique) < 2 or unique in used_sets:
            continue
        used_sets.add(unique)
        representatives = sorted(unique, key=lambda path: len(documents[path].get("text", "")), reverse=True)[:3]
        clusters.append({
            "cluster_id": "TOPIC-{:04d}".format(len(clusters) + 1),
            "topic": _cluster_label(topic, unique, documents),
            "keywords": [topic] + [word for word in _document_topics(documents[representatives[0]], 8) if word != topic][:3],
            "members": list(unique),
            "representative_documents": representatives,
            "evidence_chain": [_first_evidence(documents[path]) for path in representatives],
            "method": "统一正文与标题的共现关键词聚合",
        })
        if len(clusters) >= 20:
            break
    return clusters



def _semantic_document_profile(path, document, max_chars=1700):
    """Build one bounded semantic representation for one parsed document."""
    structure = document.get("structure", {})
    headings = [
        " ".join(str(value or "").split())
        for value in structure.get("headings", [])[:8]
        if str(value or "").strip()
    ]

    title = (
        headings[0]
        if headings
        else structure.get("title")
        or Path(path).stem
    )

    body = " ".join(
        str(document.get("text") or "").split()
    )

    parts = [
        "标题：" + str(title),
    ]

    if headings:
        parts.append(
            "章节：" + " | ".join(headings[:6])
        )

    # 不只取正文开头，同时保留中段和结尾，
    # 避免只根据摘要/版权页判断主题。
    if body:
        if len(body) <= 1200:
            parts.append("正文：" + body)
        else:
            middle_start = max(
                0,
                len(body) // 2 - 220,
            )
            parts.extend([
                "开头：" + body[:520],
                "中段：" + body[middle_start:middle_start + 440],
                "结尾：" + body[-520:],
            ])

    return "\n".join(parts)[:max_chars]


def _cosine_vector(left, right):
    numerator = sum(
        float(a) * float(b)
        for a, b in zip(left, right)
    )

    left_norm = math.sqrt(
        sum(float(value) ** 2 for value in left)
    )
    right_norm = math.sqrt(
        sum(float(value) ** 2 for value in right)
    )

    if not left_norm or not right_norm:
        return 0.0

    return numerator / (left_norm * right_norm)


def _semantic_document_clusters(
    documents,
    embedding_client,
    batch_size=6,
    batch_progress=None,
    storage=None,
):
    """
    Document-level semantic clustering.

    One vector per document, not one vector per evidence chunk.
    Uses adaptive average-link clustering without fixed domain categories.
    """
    paths = sorted(documents)

    if not paths:
        return [], None

    profiles = {
        path: _semantic_document_profile(
            path,
            documents[path],
        )
        for path in paths
    }

    model_name = str(getattr(embedding_client, "model", "local-embedding"))
    cache_keys = {
        path: hashlib.sha256(
            "{}|{}".format(
                (documents[path].get("source") or {}).get("sha256") or "",
                profiles[path],
            ).encode("utf-8", errors="replace")
        ).hexdigest()
        for path in paths
    }
    cached = storage.get_embeddings(cache_keys.values(), model_name) if storage else {}
    vectors_by_path = {
        path: cached[cache_keys[path]] for path in paths if cache_keys[path] in cached
    }
    missing_paths = [path for path in paths if path not in vectors_by_path]

    for start in range(0, len(missing_paths), batch_size):
        batch_paths = missing_paths[start:start + batch_size]
        batch_vectors = embedding_client.embed([profiles[path] for path in batch_paths])

        if len(batch_vectors) != len(batch_paths):
            raise ValueError(
                "文档级 embedding 返回数量不一致"
            )
        newly_cached = {}
        for path, vector in zip(batch_paths, batch_vectors):
            vectors_by_path[path] = vector
            newly_cached[cache_keys[path]] = vector
        if storage:
            storage.save_embeddings(model_name, newly_cached)

        if batch_progress:
            batch_progress(
                min(len(cached) + start + len(batch_paths), len(paths)),
                len(paths),
            )
    if batch_progress and not missing_paths:
        batch_progress(len(paths), len(paths))
    vectors = [vectors_by_path[path] for path in paths]

    if len(paths) == 1:
        return [{
            "cluster_id": "SEM-0001",
            "members": [paths[0]],
            "representative_documents": [paths[0]],
            "mean_similarity": None,
            "keywords": [],
            "name": None,
            "summary": None,
            "algorithm": "single-document",
        }], None

    count = len(paths)

    if count > 500:
        # Average-link clustering is intentionally bounded to small corpora.
        # MiniBatchKMeans gives predictable memory/time behaviour for thousands
        # of small files and never allocates an n*n similarity matrix.
        cluster_count = max(8, min(96, int(round(math.sqrt(count)))))
        try:
            from sklearn.cluster import MiniBatchKMeans
            model = MiniBatchKMeans(
                n_clusters=min(cluster_count, count),
                random_state=42,
                batch_size=min(1024, count),
                n_init=3,
                max_iter=120,
            )
            labels = model.fit_predict(vectors).tolist()
            centers = model.cluster_centers_.tolist()
            algorithm = "MiniBatchKMeans"
        except Exception:
            # Deterministic bounded fallback for minimal/offline deployments.
            labels = []
            for vector in vectors:
                signature = "".join("1" if float(value) >= 0 else "0" for value in vector[:16])
                labels.append(int(signature or "0", 2) % cluster_count)
            centers = None
            algorithm = "deterministic-vector-buckets"
        grouped = defaultdict(list)
        for index, label in enumerate(labels):
            grouped[int(label)].append(index)
        results = []
        for label, member_indices in grouped.items():
            member_paths = sorted(paths[index] for index in member_indices)
            if centers is not None:
                center = centers[label]
                ranked = sorted(
                    ((_cosine_vector(vectors[index], center), paths[index]) for index in member_indices),
                    key=lambda item: (-item[0], item[1]),
                )
                representatives = [path for _score, path in ranked[:3]]
                mean_similarity = round(sum(score for score, _path in ranked) / len(ranked), 6)
            else:
                representatives = member_paths[:3]
                mean_similarity = None
            results.append({
                "members": member_paths,
                "representative_documents": representatives,
                "mean_similarity": mean_similarity,
                "keywords": [],
                "name": None,
                "summary": None,
                "algorithm": algorithm,
                "algorithm_parameters": {"document_threshold": 500, "cluster_count": len(grouped)},
            })
        results.sort(key=lambda cluster: (-len(cluster["members"]), cluster["members"]))
        for index, cluster in enumerate(results, 1):
            cluster["cluster_id"] = "SEM-{:04d}".format(index)
        return results, None

    similarities = [
        [0.0] * count
        for _ in range(count)
    ]

    nearest_scores = []

    for i in range(count):
        nearest = 0.0

        for j in range(count):
            if i == j:
                similarities[i][j] = 1.0
                continue

            if j < i:
                similarities[i][j] = similarities[j][i]
            else:
                similarities[i][j] = _cosine_vector(
                    vectors[i],
                    vectors[j],
                )

            nearest = max(
                nearest,
                similarities[i][j],
            )

        nearest_scores.append(nearest)

    # 根据当前数据包自身的相似度分布确定阈值，
    # 不写死 TEE、金融、航空等领域类别。
    ordered_nearest = sorted(nearest_scores)
    median_nearest = ordered_nearest[
        len(ordered_nearest) // 2
    ]

    threshold = max(
        0.46,
        min(
            0.72,
            median_nearest * 0.86,
        ),
    )

    clusters = [
        [index]
        for index in range(count)
    ]

    def average_similarity(left, right):
        values = [
            similarities[i][j]
            for i in left
            for j in right
        ]

        return (
            sum(values) / len(values)
            if values
            else 0.0
        )

    # Average-link agglomerative clustering.
    # 相比简单连通图，可避免一个“桥接文档”
    # 把两个本来不同的主题全部串成一个大类。
    while len(clusters) > 1:
        best_pair = None
        best_score = -1.0

        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                score = average_similarity(
                    clusters[i],
                    clusters[j],
                )

                if score > best_score:
                    best_score = score
                    best_pair = (i, j)

        if (
            best_pair is None
            or best_score < threshold
        ):
            break

        left_index, right_index = best_pair

        merged = (
            clusters[left_index]
            + clusters[right_index]
        )

        clusters[left_index] = merged
        del clusters[right_index]

    results = []

    for member_indices in clusters:
        member_paths = sorted(
            paths[index]
            for index in member_indices
        )

        centrality = []

        for index in member_indices:
            others = [
                similarities[index][other]
                for other in member_indices
                if other != index
            ]

            score = (
                sum(others) / len(others)
                if others
                else 1.0
            )

            centrality.append(
                (score, paths[index])
            )

        representatives = [
            path
            for _score, path
            in sorted(
                centrality,
                key=lambda value: (
                    -value[0],
                    value[1],
                ),
            )[:3]
        ]

        pair_scores = []

        for offset, left in enumerate(member_indices):
            for right in member_indices[offset + 1:]:
                pair_scores.append(
                    similarities[left][right]
                )

        results.append({
            "members": member_paths,
            "representative_documents": representatives,
            "mean_similarity": (
                round(
                    sum(pair_scores) / len(pair_scores),
                    6,
                )
                if pair_scores
                else None
            ),
            "keywords": [],
            "name": None,
            "summary": None,
            "algorithm": "adaptive-average-link",
            "algorithm_parameters": {"document_threshold": 500, "similarity_threshold": round(threshold, 6)},
        })

    results.sort(
        key=lambda cluster: (
            -len(cluster["members"]),
            cluster["members"],
        )
    )

    for index, cluster in enumerate(results, 1):
        cluster["cluster_id"] = (
            "SEM-{:04d}".format(index)
        )

    return results, round(threshold, 6)


def _fallback_semantic_name(cluster, documents):
    counts = Counter()

    for path in cluster.get("members", []):
        counts.update(
            _content_topics(
                documents[path],
                8,
            )
        )

    generic = {
        "security",
        "software",
        "system",
        "systems",
        "hardware",
        "data",
        "paper",
        "abstract",
        "introduction",
        "using",
        "based",
        "acm",
        "pages",
    }

    words = [
        word
        for word, _count in counts.most_common()
        if word.lower() not in generic
    ][:3]

    chinese_words = [word for word in words if re.search(r"[\u4e00-\u9fff]", word)]
    if chinese_words:
        return "与".join(chinese_words[:3])[:20] + "研究"

    terminology = {
        "vulnerability": "漏洞", "vulnerabilities": "漏洞", "exploit": "漏洞利用",
        "malware": "恶意软件", "threat": "威胁情报", "attack": "攻击技术",
        "privacy": "隐私保护", "network": "网络安全", "firmware": "固件安全",
        "kernel": "内核安全", "memory": "内存安全", "cloud": "云安全",
        "authentication": "身份认证", "cryptography": "密码技术",
        "ransomware": "勒索软件", "detection": "威胁检测",
        "cve": "漏洞情报", "intrusion": "入侵检测", "encryption": "加密技术",
        "iot": "物联网安全", "android": "移动端安全", "vehicle": "车联网安全",
        "automotive": "车联网安全", "can": "车载CAN安全",
        "sidechannel": "侧信道安全", "side-channel": "侧信道安全",
        "enclave": "可信执行环境", "tee": "可信执行环境", "sgx": "SGX安全",
        "protocol": "协议安全", "adversarial": "对抗安全", "blockchain": "区块链安全",
        "amd": "AMD机密计算", "sev-snp": "SEV-SNP安全", "stackwarp": "栈安全",
        "pointer": "指针安全", "computing": "机密计算", "confidential": "机密计算",
        "trusted": "可信计算", "attestation": "可信证明", "gpu": "GPU安全",
    }
    translated = []
    for word in words:
        value = terminology.get(str(word).lower())
        if value and value not in translated:
            translated.append(value)
    if translated:
        return "与".join(translated[:3]) + "研究"

    representative = (
        cluster.get(
            "representative_documents",
            [],
        )[:1]
    )

    if representative:
        path = representative[0]
        headings = (
            documents[path]
            .get("structure", {})
            .get("headings", [])
        )

        if headings:
            heading_name = _valid_semantic_name(str(headings[0])[:28])
            if heading_name:
                return heading_name

    return "主题{}（待智能命名）".format(
        cluster.get(
            "cluster_id",
            "",
        ).replace("SEM-", "")
    )


def _valid_semantic_name(value):
    name = re.sub(r"\s+", "", str(value or "")).strip("/|,，。；;：:")
    if not (4 <= len(name) <= 28) or not re.search(r"[\u4e00-\u9fff]", name):
        return None
    forbidden = ("未知文档", "未知资料", "其他资料", "综合内容", "未分类", "miscellaneous")
    if any(word.lower() in name.lower() for word in forbidden):
        return None
    return name.replace("/", "与").replace("|", "与")


def _name_semantic_clusters(
    clusters,
    documents,
    llm=None,
    cached_names=None,
):
    """
    Name all semantic clusters in one bounded model call.
    """
    if not clusters:
        return clusters, None

    cached_names = cached_names or {}
    for cluster in clusters:
        cluster["name"] = _fallback_semantic_name(
            cluster,
            documents,
        )

        cluster["summary"] = (
            "根据文档级语义相似度自动形成，"
            "包含 {} 个文件。"
        ).format(
            len(cluster.get("members", []))
        )
        cluster["naming_source"] = "local_fallback"
        cluster["naming_status"] = "degraded"
        cluster["naming_confidence"] = 0.35
        cluster["naming_degradation_reason"] = "尚未获得通过中文质量校验的模型命名"
        cached = cached_names.get(_stable_group_node_id("内容主题", cluster["name"], cluster.get("members") or [])) or {}
        cached_name = _valid_semantic_name(cached.get("name"))
        if cached_name:
            cluster.update({
                "name": cached_name,
                "summary": str(cached.get("summary") or cluster["summary"])[:300],
                "keywords": list(cached.get("keywords") or [])[:6],
                "naming_source": "persistent_cache",
                "naming_status": "enhanced",
                "naming_confidence": cached.get("confidence", 0.82),
            })
            cluster.pop("naming_degradation_reason", None)

    if llm is None or all(cluster.get("naming_status") == "enhanced" for cluster in clusters):
        return clusters, None

    lines = []

    for cluster in clusters:
        if cluster.get("naming_status") == "enhanced":
            continue
        lines.append(
            "\n[{}]".format(
                cluster["cluster_id"]
            )
        )

        for path in cluster.get(
            "representative_documents",
            [],
        )[:3]:
            doc = documents[path]
            headings = (
                doc.get("structure", {})
                .get("headings", [])
            )

            title = (
                headings[0]
                if headings
                else doc.get("structure", {}).get("title")
                or Path(path).stem
            )

            extra = " | ".join(
                str(value)
                for value in headings[1:4]
            )

            # fast 模式下 headings/title 可能只有文件编号，
            # 因此主题命名不能只依赖结构标题。
            # 同时提供正文语义摘要，让模型根据真实内容命名。
            profile = _semantic_document_profile(
                path,
                doc,
                max_chars=850,
            )

            line = "- 文件：{}".format(path)

            if title and str(title).strip() not in {
                Path(path).stem,
                Path(path).name,
            }:
                line += "\n  标题：{}".format(title)

            if extra:
                line += "\n  章节：{}".format(extra)

            line += "\n  内容片段：{}".format(
                " ".join(str(profile).split())
            )

            lines.append(line)

    prompt = """你正在为一个完全未知的数据包自动生成分析目录。
下面每个语义簇已经由本地 embedding 根据文档内容自动聚类。
每个簇提供若干代表文档的文件名、可用标题以及真实正文片段。
你必须依据正文中的论文标题、研究对象、技术名称和主要内容判断主题，
不能因为文件名是数字或结构化标题缺失就称其为“未知文档集合”。
你只负责给每个簇生成一个简洁、可读、能概括研究对象和问题的中文主题名称。

要求：
1. 不得预设领域分类。
2. 名称建议 4-16 个汉字，必要时可保留 TEE、RISC-V、TDX、SEV 等原文术语。
3. 避免只使用 software、security、hardware、intel 等过泛词作为目录名。
4. 不要使用“其他资料”“综合内容”这类无信息名称，除非资料确实无法概括。
5. summary 用一句话说明这一组主要研究什么。
6. keywords 给出 3-6 个主题关键词。
7. 必须保持 cluster_id 不变。

语义簇：
{}

输出 JSON：
{{
  "clusters": [
    {{
      "cluster_id": "SEM-0001",
      "name": "主题名称",
      "summary": "一句话主题说明",
      "keywords": ["关键词"]
    }}
  ]
}}""".format(
        "\n".join(lines)
    )

    try:
        result = llm.chat_json(
            "你是未知资料包的主题组织助手。根据代表文档的真实正文片段、标题和章节归纳主题；文件名本身不代表内容。",
            prompt,
            max_tokens=1800,
            strict=True,
            retries=0,
            timeout=180,
            required_fields=("clusters",),
            output_context="主题聚类命名",
        )

        payload = result.get("json", {})
        named = {
            item.get("cluster_id"): item
            for item in payload.get("clusters", [])
            if isinstance(item, dict)
        }

        used_names = Counter()

        for cluster in clusters:
            if cluster.get("naming_source") == "persistent_cache":
                used_names[cluster["name"]] += 1
                continue
            item = named.get(
                cluster["cluster_id"],
                {},
            )

            proposed = _valid_semantic_name(item.get("name"))
            name = proposed or cluster["name"]

            used_names[name] += 1

            if used_names[name] > 1:
                name = "{}（{}）".format(
                    name,
                    used_names[name],
                )

            cluster["name"] = name
            if proposed:
                cluster["naming_source"] = "local_model"
                cluster["naming_status"] = "enhanced"
                cluster["naming_confidence"] = 0.82
                cluster.pop("naming_degradation_reason", None)
            else:
                cluster["naming_degradation_reason"] = "模型名称缺少中文信息、过于宽泛或格式不合格，已使用本地回退"

            cluster["summary"] = str(
                item.get("summary")
                or cluster["summary"]
            ).strip()[:300]

            keywords = item.get(
                "keywords",
                [],
            )

            cluster["keywords"] = [
                str(value).strip()
                for value in keywords[:6]
                if str(value).strip()
            ]

        return clusters, result

    except Exception as exc:
        # 命名失败不能让完整分析失败；
        # 保留本地聚类和本地回退名称。
        for cluster in clusters:
            cluster["naming_degradation_reason"] = "模型命名失败：{}".format(str(exc)[:180])
        return clusters, None


def _name_lexical_topic_nodes(tree, documents, llm=None):
    """Give lexical-fallback top-level groups readable Chinese names.

    The deterministic partition and stable node ids are never changed here.
    One bounded model call may improve all labels; a deterministic Chinese
    fallback is always present when the shared model is disabled or busy.
    """
    nodes = {}
    descriptors = []
    fallback_names = Counter()
    for index, node in enumerate(tree.get("children") or [], 1):
        if node.get("kind") != "group" or node.get("classification_status") != "classified":
            continue
        members = [path for path in node.get("member_paths") or [] if path in documents]
        if not members:
            continue
        old_name = str(node.get("name") or "")
        cluster = {
            "cluster_id": "LEX-{:04d}".format(index),
            "members": members,
            "representative_documents": sorted(
                members, key=lambda path: len(documents[path].get("text", "")), reverse=True
            )[:3],
        }
        fallback = _fallback_semantic_name(cluster, documents)
        fallback_names[fallback] += 1
        if fallback_names[fallback] > 1:
            fallback = "{}（{}）".format(fallback, fallback_names[fallback])
        node.update({
            "name": fallback,
            "naming_source": "local_fallback",
            "naming_status": "degraded",
            "naming_confidence": 0.45,
            "naming_degradation_reason": "语义向量不可用，已根据正文主题词生成中文回退名称",
            "lexical_source_name": old_name,
        })
        node["summary"] = "根据正文主题词形成“{}”，共包含 {} 个文件。".format(fallback, len(members))
        node_id = node.get("node_id")
        if not node_id:
            continue
        nodes[node_id] = node
        samples = [
            _semantic_document_profile(path, documents[path], max_chars=650)
            for path in cluster["representative_documents"]
        ]
        descriptors.append({
            "node_id": node_id,
            "current_name": fallback,
            "source_terms": [node.get("topic_key")] + list(node.get("related_topics") or [])[:6],
            "file_count": len(members),
            "representative_material": samples,
        })

    if not descriptors or llm is None:
        return tree, None

    prompt = """请为以下由本地算法确定成员的资料主题生成中文目录名。你只能改善名称和一句话说明，不能改变 node_id、成员或编造材料外事实。
要求：名称 4-22 个字，准确体现研究对象或问题；可保留 CVE、TEE、RISC-V 等术语；禁止使用“未知资料”“综合内容”等空泛名称。

输入：
{}

输出 JSON：
{{"topics":[{{"node_id":"group-...","name":"中文主题名","summary":"一句话说明"}}]}}""".format(
        json.dumps(descriptors, ensure_ascii=False)
    )
    try:
        result = llm.chat_json(
            "你是严谨的中文情报资料目录组织助手，只根据给定正文材料命名。",
            prompt,
            max_tokens=1400,
            strict=True,
            retries=0,
            timeout=180,
            required_fields=("topics",),
            output_context="词法主题中文命名",
        )
        named = {
            item.get("node_id"): item
            for item in result.get("json", {}).get("topics", [])
            if isinstance(item, dict) and item.get("node_id") in nodes
        }
        used_names = Counter()
        for node_id, node in nodes.items():
            item = named.get(node_id) or {}
            proposed = _valid_semantic_name(item.get("name"))
            name = proposed or node["name"]
            used_names[name] += 1
            if used_names[name] > 1:
                name = "{}（{}）".format(name, used_names[name])
            node["name"] = name
            if proposed:
                node.update({
                    "naming_source": "local_model",
                    "naming_status": "enhanced",
                    "naming_confidence": 0.82,
                })
                node.pop("naming_degradation_reason", None)
            summary = str(item.get("summary") or node.get("summary") or "").strip()[:300]
            if summary:
                node["summary"] = summary
        return tree, result
    except Exception as exc:
        for node in nodes.values():
            node["naming_degradation_reason"] = "词法主题模型命名失败，保留中文回退名称：{}".format(str(exc)[:140])
        return tree, None


def _semantic_adaptive_tree(
    scan,
    documents,
    node_summaries,
    semantic_clusters,
):
    """Build the official theme-first tree from semantic clusters."""

    def file_leaf(path):
        document = documents[path]
        source = document.get("source", {})

        return {
            "kind": "file",
            "name": Path(path).name,
            "path": path,
            "summary": (
                node_summaries
                .get(path, {})
                .get("summary")
                or "；".join(
                    document.get(
                        "structure",
                        {},
                    ).get(
                        "headings",
                        [],
                    )[:2]
                )
                or "已完成统一解析"
            ),
            "extension": source.get(
                "extension"
            ),
            "size_human": human_size(
                int(
                    source.get("size")
                    or 0
                )
            ),
            "content_category": (
                document.get(
                    "classification",
                    {},
                ).get(
                    "document_role",
                    "一般资料",
                )
            ),
            "related_topics": _content_topics(
                document,
                8,
            ),
            "primary_topic": (document.get("classification") or {}).get("primary_topic"),
            "topic_memberships": (document.get("classification") or {}).get("topic_memberships", []),
            "classification_confidence": (document.get("classification") or {}).get("confidence"),
            "classification_evidence_ids": (document.get("classification") or {}).get("evidence_ids", []),
            "evidence_ids": [
                item.get("evidence_id")
                for item in document.get(
                    "evidence",
                    [],
                )[:2]
                if item.get("evidence_id")
            ],
        }

    children = []

    for cluster in semantic_clusters:
        member_paths = sorted(
            cluster.get(
                "members",
                [],
            )
        )

        if not member_paths:
            continue

        name = (
            cluster.get("name")
            or _fallback_semantic_name(
                cluster,
                documents,
            )
        )

        for path in member_paths:
            selected = _node_evidence(documents, [path], topics=[name], max_items=2)
            documents[path].setdefault("classification", {}).update({
                "primary_topic": name,
                "topic_memberships": [name],
                "confidence": round(float(cluster.get("mean_similarity") or 0.8), 3),
                "classification_status": "classified",
                "classification_reason": "文档通过本地语义向量聚类归入该唯一主主题。",
                "evidence_ids": [item.get("evidence_id") for item in selected if item.get("evidence_id")],
            })

        total_size = sum(
            int(
                documents[path]
                .get("source", {})
                .get("size")
                or 0
            )
            for path in member_paths
        )

        children.append({
            "kind": "group",
            "node_id": _stable_group_node_id("内容主题", name, member_paths),
            "dimension": "内容主题",
            "classification_status": "classified",
            "topic_key": name,
            "name": name,
            "summary": (
                cluster.get("summary")
                or "根据文档级语义相似度自动形成。"
            ),
            "member_paths": member_paths,
            "file_count": len(member_paths),
            "total_size": total_size,
            "total_size_human": human_size(
                total_size
            ),
            "related_topics": cluster.get(
                "keywords",
                [],
            ),
            "mean_similarity": cluster.get(
                "mean_similarity"
            ),
            "semantic_cluster_id": cluster.get(
                "cluster_id"
            ),
            "naming_source": cluster.get("naming_source", "local_fallback"),
            "naming_status": cluster.get("naming_status", "degraded"),
            "naming_confidence": cluster.get("naming_confidence"),
            "naming_degradation_reason": cluster.get("naming_degradation_reason"),
            "children": [
                file_leaf(path)
                for path in member_paths
            ],
        })

    tree = {
        "kind": "analysis_root",
        "name": Path(scan["root"]).name,
        "path": ".",
        "dimensions": [{
            "name": "内容主题",
            "reason": (
                "根据本地 qwen-embed 文档级语义向量自动聚类，"
                "再由本地模型生成可读主题名称；"
                "不使用预设领域类别。"
            ),
            "cardinality": len(children),
        }],
        "summary": (
            node_summaries
            .get(".", {})
            .get("summary")
        ),
        "children": children,
    }
    return _enrich_analysis_tree(tree, documents)


def _primary_membership_validation(tree, expected_paths):
    """Measure the top-level partition without trusting declared counts."""
    expected = {str(path) for path in expected_paths or []}
    counts = Counter()
    for group in tree.get("children") or []:
        if group.get("kind") != "group" or group.get("classification_status") in {"pending", "failed"}:
            continue
        counts.update(str(path) for path in dict.fromkeys(group.get("member_paths") or []))
    duplicates = sorted(path for path in expected if counts.get(path, 0) > 1)
    missing = sorted(path for path in expected if counts.get(path, 0) == 0)
    unexpected = sorted(path for path in counts if path not in expected)
    return {
        "valid": not duplicates and not missing,
        "expected_file_count": len(expected),
        "covered_file_count": sum(1 for path in expected if counts.get(path, 0) >= 1),
        "duplicate_primary_count": len(duplicates),
        "missing_primary_count": len(missing),
        "unexpected_path_count": len(unexpected),
        "duplicate_primary_paths": duplicates[:20],
        "missing_primary_paths": missing[:20],
        "unexpected_paths": unexpected[:20],
    }


def _add_related_topic_mounts(tree, documents):
    """Add non-counting cross-topic references without duplicating leaves.

    Every physical file has exactly one primary group. Related classifications
    are metadata references, never additional children/member_paths. This keeps
    coverage, export sizes and report percentages mathematically valid while
    still exposing useful cross-topic relationships to the UI.
    """
    groups = [item for item in tree.get("children") or [] if item.get("kind") == "group"]
    for group in groups:
        group["related_file_refs"] = []
    for path, document in documents.items():
        doc_topics = set(_content_topics(document, 12))
        memberships = list((document.get("classification") or {}).get("topic_memberships") or [])
        primary_topic = (document.get("classification") or {}).get("primary_topic")
        if not doc_topics:
            continue
        mounted = 0
        for group in groups:
            members = set(group.get("member_paths") or [])
            if path in members:
                continue
            topic_key = group.get("topic_key")
            group_terms = set(_tokens(group.get("name") or ""))
            group_terms.update(str(value) for value in group.get("related_topics") or [])
            overlap = len(doc_topics.intersection(group_terms))
            declared_related = bool(topic_key and topic_key != primary_topic and topic_key in memberships)
            if not declared_related and overlap < 2:
                continue
            reference = {
                "path": path,
                "name": Path(path).name,
                "overlap": overlap,
                "reason": (
                    "该方向是文档分类结果中的次要主题"
                    if declared_related
                    else "正文主题与该节点存在 {} 个共同主题词".format(overlap)
                ),
            }
            group.setdefault("related_file_refs", []).append(reference)
            mounted += 1
            if mounted >= 2:
                break
    for group in groups:
        references = sorted(
            group.get("related_file_refs") or [],
            key=lambda item: (-int(item.get("overlap") or 0), item.get("path") or ""),
        )
        group["related_file_refs"] = references
        group["related_file_count"] = len(references)
    validation = _primary_membership_validation(tree, documents.keys())
    tree["membership_validation"] = validation
    tree["membership_contract"] = {
        "primary_membership": "exactly_one",
        "related_membership": "non_counting_reference",
        "enforced": validation["valid"],
        "contract_version": "analysis-tree-membership/2.0",
        "scope": "all_parsed_physical_files",
    }
    return tree


def _parser_checkpoint_contract(parser):
    """Return parse-affecting configuration for checkpoint invalidation."""
    return {
        "unified_document_schema": "unified-document/1.0",
        "parser_class": parser.__class__.__name__,
        "max_chars": int(getattr(parser, "max_chars", Config.MAX_FULL_DOCUMENT_CHARS)),
        "fast_office_ocr": bool(getattr(parser, "fast_office_ocr", True)),
        "docling_device": str(getattr(parser, "docling_device", Config.DOCLING_DEVICE)),
        "docling_cpu_threads": int(getattr(parser, "docling_cpu_threads", Config.DOCLING_CPU_THREADS)),
        "max_single_file_bytes": int(Config.MAX_SINGLE_FILE_BYTES),
        "max_archive_file_bytes": int(Config.MAX_ARCHIVE_FILE_BYTES),
        "max_archive_entries": int(Config.MAX_ARCHIVE_ENTRIES),
        "max_archive_member_bytes": int(Config.MAX_ARCHIVE_MEMBER_BYTES),
        "max_archive_uncompressed_bytes": int(Config.MAX_ARCHIVE_UNCOMPRESSED_BYTES),
        "max_archive_depth": int(os.getenv("MAX_ARCHIVE_DEPTH", "1")),
    }


def checkpoint_fingerprint(node, parser, parse_mode, document):
    return file_fingerprint(
        node,
        parse_mode=parse_mode,
        parser_contract=_parser_checkpoint_contract(parser),
        source_sha256=(document.get("source") or {}).get("sha256"),
    )


def _verified_source_sha256(root, file_node, cancel_check=None):
    """Hash the exact inventoried handle without reopening a mutable path."""
    source_fd, before = _open_inventory_source(root, file_node)
    digest = hashlib.sha256()
    with os.fdopen(source_fd, "rb", closefd=True) as stream:
        while True:
            if cancel_check is not None and cancel_check():
                raise ParseIsolationCancelled("任务已取消，停止检查解析缓存")
            block = stream.read(4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(stream.fileno())
    if (
        int(before.st_dev), int(before.st_ino), int(before.st_size), int(before.st_mtime_ns)
    ) != (
        int(after.st_dev), int(after.st_ino), int(after.st_size), int(after.st_mtime_ns)
    ):
        raise SourceFileChangedError("源文件在检查点校验期间发生变化")
    return digest.hexdigest()


def _structured_profile_complete(profile):
    coverage = profile.get("coverage") or {}
    limits = profile.get("limits") or {}
    return bool(
        profile.get("status") == "completed"
        and coverage.get("complete", True)
        and not coverage.get("truncated")
        and not limits.get("truncated")
    )


def _build_structured_overview(documents):
    """Aggregate bounded profiles without presenting samples/top-K as totals."""
    structured_profiles = []
    omitted_profile_count = 0
    unavailable_profile_count = 0
    for document_path, document in documents.items():
        profile = document.get("data_profile")
        if profile and profile.get("status") in PROFILE_USABLE_STATUSES:
            structured_profiles.append({"path": document_path, "profile": profile})
        elif isinstance(profile, dict):
            unavailable_profile_count += 1
        nested_items = list(document.get("data_profiles") or [])
        represented_nested = 0
        for item in nested_items:
            if not isinstance(item, dict):
                continue
            nested = item.get("profile")
            if not isinstance(nested, dict):
                continue
            represented_nested += 1
            if nested.get("status") in PROFILE_USABLE_STATUSES:
                structured_profiles.append({
                    "path": "{}::{}".format(document_path, item.get("member")),
                    "profile": nested,
                })
            else:
                unavailable_profile_count += 1
        declared_nested = max(
            len(nested_items),
            int(document.get("data_profiles_total") or 0),
        )
        omitted_profile_count += max(0, declared_nested - represented_nested)

    complete_profiles = sum(
        1 for item in structured_profiles
        if _structured_profile_complete(item["profile"])
    )
    partial_profiles = len(structured_profiles) - complete_profiles
    sampled_rows = sum(
        int(item["profile"].get("row_count") or 0)
        for item in structured_profiles
    )
    aggregate_complete = bool(structured_profiles) and not (
        partial_profiles or omitted_profile_count or unavailable_profile_count
    )
    profile_scores = [
        float(item["profile"].get("quality_score", 0))
        for item in structured_profiles
        if item["profile"].get("quality_score") is not None
    ]
    entity_statistics = {}
    for category in ("person", "location", "event"):
        merged = Counter()
        columns = set()
        category_complete = aggregate_complete
        participating = 0
        for item in structured_profiles:
            profile = item["profile"]
            category_columns = (profile.get("entity_columns") or {}).get(category, [])
            local = (profile.get("entity_statistics") or {}).get(category) or {}
            if not category_columns and not local:
                continue
            participating += 1
            columns.update(category_columns)
            values = [value for value in (local.get("top_values") or []) if isinstance(value, dict)]
            for value in values:
                merged[str(value.get("value") or "")] += int(value.get("count") or 0)
            local_distinct = int(local.get("distinct_count") or 0)
            if (
                not _structured_profile_complete(profile)
                or local.get("distinct_count") is None
                or local_distinct > len(values)
            ):
                category_complete = False
        if columns or merged:
            observed = [
                {"value": key, "count": count}
                for key, count in merged.most_common(20)
            ]
            entity_statistics[category] = {
                "columns": sorted(columns),
                "distinct_count": len(merged) if category_complete else None,
                "observed_distinct_count": len(merged),
                "top_values": observed,
                "observed_top_values": observed,
                "participating_profiles": participating,
                "coverage": {
                    "complete": category_complete,
                    "reason": None if category_complete else (
                        "仅合并各画像保留的 top-K/有界样本，不能据此计算全局 distinct_count。"
                    ),
                },
            }

    recommendation_questions = []
    for item in structured_profiles:
        recommendation_questions.extend(item["profile"].get("recommendation_questions") or [])
    return {
        "profiled_files": len(structured_profiles),
        "total_rows": sampled_rows if aggregate_complete else None,
        "sampled_rows": sampled_rows,
        "row_count_kind": "exact" if aggregate_complete else "sampled",
        "average_quality_score": round(sum(profile_scores) / len(profile_scores), 2) if profile_scores else None,
        "missing_value_columns": sorted(set(
            name for item in structured_profiles
            for name in item["profile"].get("missing_columns", [])
        )),
        "sensitive_columns": sorted(set(
            name for item in structured_profiles
            for name in item["profile"].get("sensitive_columns", [])
        )),
        "entity_statistics": entity_statistics,
        "recommendation_questions": list(dict.fromkeys(recommendation_questions))[:12],
        "coverage": {
            "complete": aggregate_complete,
            "complete_profiles": complete_profiles,
            "partial_profiles": partial_profiles,
            "omitted_projected_profiles": omitted_profile_count,
            "unavailable_profiles": unavailable_profile_count,
        },
        "profiles": structured_profiles[:200],
    }



def analyze_package(scan_id, scan, storage, parser, progress=None, embedding_client=None, llm=None,
                    large_options=None, target_paths=None, cancel_check=None, parse_mode_override=None):
    progress = progress or (lambda percent, message: None)
    files = list(_walk_files(scan["tree"]))
    parse_mode = "fast" if scan.get("parse_mode") == "fast" else "accurate"
    policy = build_policy(scan, large_options)
    inventory = inventory_by_path(scan)
    all_paths = set(inventory)
    target_paths = set(target_paths or []) & all_paths
    prior_states = {item.get("node_path"): item for item in storage.iter_file_states(scan_id)}
    documents = {}
    for item in storage.iter_documents(scan_id, hydrate=not policy.get("enabled")):
        if item.get("path") not in all_paths:
            continue
        payload = item["payload"]
        documents[item["path"]] = (
            storage.project_document(
                payload,
                text_limit=policy["overview_chars_per_file"],
                evidence_limit=policy["overview_evidence_per_file"],
            )
            if policy.get("enabled") else payload
        )
    failures = [
        {"path": path, "error": state.get("error") or "历史解析失败"}
        for path, state in prior_states.items()
        if state.get("status") == "failed" and path in all_paths
    ]

    if target_paths:
        candidates = [node for node in files if node.get("path") in target_paths]
        phase_label = "补充分析"
    elif policy.get("enabled"):
        # Large-package mode is bounded by the parser queue and persistent
        # checkpoints, not by a representative-file cap.  Every inventoried
        # path gets a final completed or failed state.
        candidates = files
        phase_label = "大数据包分批全量分析"
    else:
        candidates = files
        phase_label = "完整分析"

    if parse_mode_override is not None:
        actual_parse_mode = "fast" if str(parse_mode_override).lower() == "fast" else "accurate"
    elif policy.get("enabled") and not target_paths:
        # A 10 GiB first pass must finish predictably.  Accurate OCR/Docling is
        # reserved for explicit follow-up scopes and can reuse this fast pass.
        actual_parse_mode = "fast"
    else:
        actual_parse_mode = parse_mode
    parser_contract = _parser_checkpoint_contract(parser)
    progress(2, "开始{}：{} 个文件".format(phase_label, len(candidates)))
    parse_candidates = []
    reusable_count = 0
    for index, file_node in enumerate(candidates, 1):
        node_path = file_node["path"]
        existing_state = prior_states.get(node_path)
        existing = documents.get(node_path)
        reusable = False
        if existing_state and existing_state.get("status") in {"completed", "overview"} and existing:
            stored_sha256 = str((existing.get("source") or {}).get("sha256") or "")
            if stored_sha256:
                try:
                    current_sha256 = _verified_source_sha256(scan["root"], file_node, cancel_check)
                    acceptable_modes = [actual_parse_mode]
                    stored_mode = str((existing.get("parser") or {}).get("mode") or "").lower()
                    if actual_parse_mode == "fast" and stored_mode.startswith("accurate"):
                        # Accurate is a strict upgrade of the fast first pass;
                        # never downgrade an explicitly deep-parsed sidecar.
                        acceptable_modes.append("accurate")
                    expected = {
                        file_fingerprint(
                            file_node, parse_mode=mode, parser_contract=parser_contract,
                            source_sha256=current_sha256,
                        )
                        for mode in acceptable_modes
                    }
                    reusable = (
                        current_sha256 == stored_sha256
                        and existing_state.get("fingerprint") in expected
                    )
                except OSError:
                    reusable = False
        if reusable:
            reusable_count += 1
            progress(2 + int(68 * index / max(1, len(candidates))), "复用已校验检查点：{}/{} {}".format(index, len(candidates), node_path))
            continue

        parse_candidates.append((file_node, None))

    total_candidates = max(1, len(candidates))
    completed_candidates = reusable_count

    def commit_parse_result(_index, file_node, document, error):
        nonlocal completed_candidates, failures
        node_path = file_node["path"]
        completed_candidates += 1
        try:
            if error is not None:
                raise error
            if (
                (document.get("parser") or {}).get("archive")
                and not int((document.get("structure") or {}).get("archive_member_count") or 0)
                and not str(document.get("text") or "").strip()
            ):
                details = "；".join(str(item) for item in document.get("warnings") or [] if item)
                raise ValueError(
                    "压缩包没有完成有效展开。{}。若文件正在复制，请等待完成后重新导入；"
                    "若文件已完整，请检查是否损坏、加密或属于分卷压缩包。".format(details or "未发现可解析成员")
                )
            storage.save_document(scan_id, node_path, document)
            fingerprint = file_fingerprint(
                file_node,
                parse_mode=actual_parse_mode,
                parser_contract=parser_contract,
                source_sha256=(document.get("source") or {}).get("sha256"),
            )
            documents[node_path] = (
                storage.project_document(
                    document,
                    text_limit=policy["overview_chars_per_file"],
                    evidence_limit=policy["overview_evidence_per_file"],
                )
                if policy.get("enabled") else document
            )
            if policy.get("enabled"):
                storage.replace_document_evidence_index(
                    scan_id, node_path, evidence_corpus({node_path: document})
                )
            storage.set_file_state(
                scan_id, node_path, fingerprint,
                "completed", document=document,
            )
            failures = [item for item in failures if item.get("path") != node_path]
        except Exception as exc:
            fingerprint = file_fingerprint(
                file_node, parse_mode=actual_parse_mode, parser_contract=parser_contract,
            )
            failures = [item for item in failures if item.get("path") != node_path]
            failures.append({"path": node_path, "error": str(exc)})
            # A failed current attempt invalidates any payload left by an older
            # successful parse.  Otherwise old text leaks into classification,
            # retrieval and parsed coverage while this same path is failed.
            documents.pop(node_path, None)
            storage.delete_document(scan_id, node_path)
            if policy.get("enabled"):
                storage.replace_document_evidence_index(scan_id, node_path, [])
            storage.set_file_state(scan_id, node_path, fingerprint, "failed", error=str(exc))
        progress(
            2 + int(68 * completed_candidates / total_candidates),
            "{}：{}/{} {}".format(phase_label, completed_candidates, len(candidates), node_path),
        )

    if parse_candidates:
        # A separate CPU parser pool is safe because every pool thread owns a
        # process-isolated Docling/OCR runner.  Ollama/model generation is not
        # called here and remains bounded by its own single-request semaphore.
        parse_workers = max(1, min(8, int(getattr(Config, "PARSE_MAX_CONCURRENCY", 1))))
        # This pool is explicitly CPU-only.  If an operator opts Docling into
        # CUDA/auto mode, do not create concurrent parser processes that could
        # compete with the shared local Qwen model for the 3090 VRAM.
        if str(getattr(parser, "docling_device", "cpu")).lower() != "cpu":
            parse_workers = 1
        parse_workers = _temp_disk_worker_limit(parse_workers)

        batch_size = len(parse_candidates)
        if policy.get("enabled"):
            batch_size = max(1, int(policy.get("batch_files") or 200))
        parse_pulse = {"at": 0.0}
        for batch_start in range(0, len(parse_candidates), batch_size):
            current_batch = parse_candidates[batch_start:batch_start + batch_size]
            batch_offset = batch_start

            def parse_tick(done_count, total_count, active_paths):
                now = time.monotonic()
                if now - parse_pulse["at"] < 1.5:
                    return
                parse_pulse["at"] = now
                overall_done = min(
                    len(candidates), reusable_count + batch_offset + int(done_count or 0)
                )
                active_text = "、".join(str(path) for path in (active_paths or [])[:2] if path)
                progress(
                    2 + int(68 * overall_done / total_candidates),
                    "{}：已完成 {}/{}；正在解析 {}".format(
                        phase_label, overall_done, len(candidates), active_text or "当前文件",
                    ),
                )

            _parallel_parse_files(
                parser,
                [item[0] for item in current_batch],
                scan["root"],
                actual_parse_mode,
                max_workers=parse_workers,
                cancel_check=cancel_check,
                on_complete=commit_parse_result,
                on_tick=parse_tick,
            )

    failed_paths = {item.get("path") for item in failures}
    pending_paths = all_paths - set(documents) - failed_paths

    classified_batch = []
    classified_total = max(1, len(documents))
    for classified_index, (document_path, document) in enumerate(documents.items(), 1):
        if cancel_check is not None and cancel_check():
            raise ParseIsolationCancelled("任务已取消，停止文档角色分析")
        role, details = _document_role_details(document)
        classification = document.setdefault("classification", {})
        classification["document_role"] = role
        classification["role_reason"] = details["reason"]
        classification["role_scores"] = details["scores"]
        if not policy.get("enabled"):
            classified_batch.append((document_path, document))
        if not policy.get("enabled") and len(classified_batch) >= 250:
            storage.save_documents(scan_id, classified_batch)
            classified_batch = []
            progress(
                70 + int(2 * classified_index / classified_total),
                "整理文档角色与质量：{}/{}".format(classified_index, len(documents)),
            )
    if classified_batch:
        storage.save_documents(scan_id, classified_batch)

    progress(72, "执行精确去重与高相似文档聚类")
    if policy.get("enabled"):
        # Large-package mode prioritizes full content coverage.  Do not spend
        # the package budget on pairwise similarity or canonical projection;
        # every parsed file remains available to the content topic tree.
        exact_groups = []
        canonical_documents = documents
        canonical_by_path = {path: path for path in documents}
        aliases_by_canonical = {}
    else:
        exact_groups = _group_exact(documents)
        canonical_documents, canonical_by_path, aliases_by_canonical = _canonical_projection(
            documents, exact_groups
        )
    # Persist the canonical/alias relationship on every original path.  It is
    # visible in the physical tree and survives a Worker restart.
    if not policy.get("enabled"):
        for document_path, document in documents.items():
            storage.save_document(scan_id, document_path, document)
    similar_groups = [] if policy.get("enabled") else _group_similar(documents, exact_groups)
    topic_clusters = _topic_clusters(canonical_documents)
    if policy.get("enabled"):
        # Rebuild the durable catalog one document at a time: one complete
        # evidence scan, no 300k-object corpus allocation and no repeated BM25.
        storage.clear_evidence_index(scan_id)
        evidence_index_count = 0
        for indexed_path, indexed_document in canonical_documents.items():
            evidence_index_count += storage.replace_document_evidence_index(
                scan_id, indexed_path, evidence_corpus({indexed_path: indexed_document})
            )
        manifest_queries = [
            {"query": str(item.get("topic") or ""), "results": [], "deferred": True}
            for item in topic_clusters[:5] if str(item.get("topic") or "").strip()
        ]
        retrieval = {
            "schema_version": "local-retrieval/2.0",
            "method": "SQLite FTS5/BM25 候选召回 + {} + 证据质量重排".format(
                "本地语义向量" if embedding_mode() != "lexical-fallback" else "TF-IDF 词法相关度"
            ),
            "evidence_chunks": evidence_index_count,
            "queries": manifest_queries,
            "remote_services_enabled": False,
            "persistent_index": True,
            "package_queries_deferred": True,
        }
    else:
        retrieval = build_retrieval_manifest(canonical_documents, topic_clusters)
        evidence_index_count = storage.replace_evidence_index(
            scan_id, evidence_corpus(canonical_documents)
        )
    research_documents = {
        path: document for path, document in canonical_documents.items()
        if document.get("classification", {}).get("document_role") not in {"要求与说明材料", "派生概览材料"}
    } or canonical_documents
    if policy.get("enabled"):
        # The first-pass package tree already excludes role noise where it is
        # consumed.  A second full clustering/index pass is deferred until an
        # explicit research scope is selected.
        research_topic_clusters = topic_clusters
        research_retrieval = dict(retrieval)
        research_retrieval["scope"] = "research_documents_on_demand"
    else:
        research_topic_clusters = _topic_clusters(research_documents)
        research_retrieval = build_retrieval_manifest(research_documents, research_topic_clusters)

    # ---------------------------------------------------------
    # 文档级语义聚类：正式目录的数据来源
    # ---------------------------------------------------------
    semantic_clusters = []
    semantic_threshold = None
    semantic_naming_model = None
    naming_result = None
    semantic_error = None

    if embedding_client is not None and canonical_documents:
        try:
            progress(74, "生成文档级语义向量")

            def semantic_progress(done, total):
                percent = 74 + int(
                    4 * done / max(1, total)
                )
                progress(
                    percent,
                    "文档级语义向量：{}/{}".format(
                        done,
                        total,
                    ),
                )

            semantic_clusters, semantic_threshold = (
                _semantic_document_clusters(
                    canonical_documents,
                    embedding_client,
                    batch_size=6,
                    batch_progress=semantic_progress,
                    storage=storage,
                )
            )

            progress(
                79,
                "生成语义主题名称",
            )

            if llm is not None:
                naming_cache = {}
                for cluster in semantic_clusters:
                    cache_id = _stable_group_node_id("内容主题", "", cluster.get("members") or [])
                    cached = storage.get_summary(scan_id, "naming:{}".format(cache_id), "semantic_naming")
                    if cached and cached.get("schema_version") == 1:
                        naming_cache[cache_id] = cached
                semantic_clusters, naming_result = (
                    _name_semantic_clusters(
                        semantic_clusters,
                        canonical_documents,
                        llm=llm,
                        cached_names=naming_cache,
                    )
                )

                for cluster in semantic_clusters:
                    if cluster.get("naming_status") != "enhanced":
                        continue
                    cache_id = _stable_group_node_id("内容主题", "", cluster.get("members") or [])
                    storage.save_summary(scan_id, "naming:{}".format(cache_id), "semantic_naming", {
                        "schema_version": 1,
                        "name": cluster.get("name"),
                        "summary": cluster.get("summary"),
                        "keywords": cluster.get("keywords") or [],
                        "confidence": cluster.get("naming_confidence"),
                    })

                if naming_result:
                    semantic_naming_model = (
                        naming_result.get("model")
                    )
            else:
                semantic_clusters, _unused = _name_semantic_clusters(
                    semantic_clusters, canonical_documents, llm=None,
                )
                semantic_error = "本地模型不可用；目录已显式标记为待智能命名"

        except Exception as exc:
            semantic_error = str(exc)
            semantic_clusters = []

    progress(82, "生成所有文件夹的本地摘要与证据链")
    node_summaries = {}
    directories = list(_walk_directories(scan["tree"]))
    unparsed_paths = sorted(all_paths - set(documents))
    summary_total = max(1, len(directories) + len(documents) + len(unparsed_paths))
    summary_done = 0
    summary_batch = []

    def queue_summary(path, summary_type, summary):
        nonlocal summary_done, summary_batch
        summary_batch.append((path, summary_type, summary))
        summary_done += 1
        if len(summary_batch) >= 250:
            if cancel_check is not None and cancel_check():
                raise ParseIsolationCancelled("任务已取消，停止生成节点摘要")
            storage.save_summaries(scan_id, summary_batch)
            summary_batch = []
            progress(
                min(87, 82 + int(5 * summary_done / summary_total)),
                "批量生成节点摘要：{}/{}".format(summary_done, summary_total),
            )

    for directory in directories:
        summary = _node_summary(directory, documents)
        node_summaries[directory["path"]] = summary
        directory["simple_summary"] = summary["summary"]
        directory["evidence_count"] = len(summary["evidence_chain"])
        queue_summary(directory["path"], "folder", summary)
    local_file_summary_count = 0
    for path, document in documents.items():
        summary = _file_summary(path, document)
        node_summaries[path] = summary
        queue_summary(path, "file", summary)
        local_file_summary_count += 1
    for path in unparsed_paths:
        state = "failed" if path in failed_paths else "pending"
        summary = _inventory_file_summary(path, inventory.get(path, {}), state=state)
        node_summaries[path] = summary
        queue_summary(path, "file", summary)
    if summary_batch:
        storage.save_summaries(scan_id, summary_batch)
    progress(87, "节点摘要已生成：{}/{}".format(summary_done, summary_total))

    retrieved_evidence = []
    retrieved_ids = set()
    if policy.get("enabled"):
        for search in retrieval.get("queries", []):
            candidates = storage.search_evidence_index(
                scan_id, search.get("query") or "核心主题", limit=200
            )
            search["results"] = retrieve_evidence(
                {}, search.get("query") or "核心主题", top_k=6,
                indexed_chunks=candidates,
            ).get("results", [])
            search["deferred"] = False
    for search in retrieval.get("queries", []):
        for item in search.get("results", []):
            key = item.get("evidence_id") or (item.get("source_path"), item.get("content_sha256"), item.get("text"))
            if key in retrieved_ids:
                continue
            retrieved_ids.add(key)
            retrieved_evidence.append(item)
            if len(retrieved_evidence) >= 12:
                break
        if len(retrieved_evidence) >= 12:
            break
    if node_summaries.get(".") and retrieved_evidence:
        node_summaries["."]["evidence_chain"] = retrieved_evidence
        node_summaries["."]["retrieval_method"] = retrieval.get("method")
        storage.save_summary(scan_id, ".", "folder", node_summaries["."])

    if semantic_clusters:
        adaptive_tree = _semantic_adaptive_tree(
            scan,
            canonical_documents,
            node_summaries,
            semantic_clusters,
        )
        lexical_naming_result = None
    else:
        adaptive_tree = _adaptive_tree(
            scan,
            canonical_documents,
            node_summaries,
            enrich=False,
        )
        adaptive_tree, lexical_naming_result = _name_lexical_topic_nodes(
            adaptive_tree, canonical_documents, llm,
        )
        adaptive_tree = _enrich_analysis_tree(adaptive_tree, canonical_documents)
    progress(88, "生成可下钻子方向名称")
    if llm is not None:
        adaptive_tree, subtopic_naming_result = _name_subtopic_nodes(
            adaptive_tree,
            canonical_documents,
            llm,
        )
    else:
        subtopic_naming_result = None
    adaptive_tree = _expand_duplicate_aliases_in_tree(
        adaptive_tree,
        documents,
        canonical_by_path,
        aliases_by_canonical,
        node_summaries,
    )
    adaptive_tree = _add_related_topic_mounts(adaptive_tree, documents)
    # The content tree annotates documents with their primary topic,
    # confidence and classification evidence.  Persist those annotations so a
    # restart or later export sees the same auditable decision.
    if policy.get("enabled"):
        for document_path, document in documents.items():
            stored_document = storage.get_document(scan_id, document_path)
            if not stored_document:
                continue
            stored_document["classification"] = dict(document.get("classification") or {})
            storage.save_document(scan_id, document_path, stored_document)
    else:
        classification_batch = []
        for document_path, document in documents.items():
            classification_batch.append((document_path, document))
            if len(classification_batch) >= 250:
                storage.save_documents(scan_id, classification_batch)
                classification_batch = []
        if classification_batch:
            storage.save_documents(scan_id, classification_batch)
    scan["tree"] = _annotate_physical_tree_deduplication(
        scan["tree"], canonical_by_path, documents, node_summaries
    )
    if policy.get("enabled"):
        waiting = pending_group(pending_paths, inventory, policy)
        if waiting:
            waiting["children"] = [{
                "kind": "file", "name": Path(path).name, "path": path,
                "size": int(inventory.get(path, {}).get("size") or 0),
                "size_human": human_size(int(inventory.get(path, {}).get("size") or 0)),
                "classification_status": "pending", "classification_reason": "尚未进入内容分析批次",
            } for path in sorted(pending_paths)]
            adaptive_tree.setdefault("children", []).append(waiting)
    if failed_paths:
        failed_group = {
            "kind": "group", "node_type": "failed_scope", "node_id": "failed-{}".format(hashlib.sha256("|".join(sorted(failed_paths)).encode("utf-8")).hexdigest()[:16]),
            "dimension": "解析状态", "name": "解析失败（需重试）", "classification_status": "failed",
            "summary": "该分支包含 {} 个解析失败文件，可在数据包管理中重试。".format(len(failed_paths)),
            "member_paths": sorted(failed_paths), "file_count": len(failed_paths),
            "children": [{
                "kind": "file", "name": Path(path).name, "path": path,
                "size": int(inventory.get(path, {}).get("size") or 0),
                "size_human": human_size(int(inventory.get(path, {}).get("size") or 0)),
                "classification_status": "failed", "classification_reason": next((item.get("error") for item in failures if item.get("path") == path), "解析失败"),
            } for path in sorted(failed_paths)],
            "evidence_chain": [], "conclusion_evidence": [], "coverage": {"status": "失败", "inventory_files": len(failed_paths), "parsed_files": 0, "failed_files": len(failed_paths)},
        }
        adaptive_tree.setdefault("children", []).append(failed_group)
    coverage_for_paths, package_coverage = build_coverage(
        scan, documents, failures=failures, pending_paths=pending_paths, policy=policy,
    )
    adaptive_tree = attach_tree_coverage(adaptive_tree, coverage_for_paths, all_paths)
    tree_version_rows = []
    tree_stack = [adaptive_tree]
    while tree_stack:
        tree_node = tree_stack.pop()
        if tree_node.get("kind") == "group":
            tree_version_rows.append({
                "node_id": tree_node.get("node_id"),
                "members": sorted(set(tree_node.get("member_paths") or [])),
                "manual_identity_safe": True,
            })
        tree_stack.extend(reversed(tree_node.get("children") or []))
    analysis_tree_version = hashlib.sha256(json.dumps(
        sorted(tree_version_rows, key=lambda item: str(item.get("node_id") or "")),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:20]
    exact_duplicate_files = sum(group["duplicate_count"] for group in exact_groups)
    structured_overview = _build_structured_overview(documents)
    parsed_ratio = float(package_coverage.get("parsed_file_ratio") or 0)
    evidence_count = sum(len(document.get("evidence", [])) for document in canonical_documents.values())
    overview = {
        "file_count": scan.get("file_count", 0),
        "directory_count": scan.get("directory_count", 0),
        "total_size": scan.get("total_size", 0),
        "total_size_human": scan.get("total_size_human"),
        "format_counts": scan.get("type_counts", {}),
        "parsed_files": len(documents),
        "sampled_files": package_coverage.get("sampled_files", 0),
        "deep_analyzed_files": package_coverage.get("deep_analyzed_files", 0),
        "pending_files": len(pending_paths),
        "failed_files": len(failures),
        "scanned_bytes": package_coverage.get("scanned_bytes", package_coverage.get("inventory_bytes", 0)),
        "parsed_bytes": package_coverage.get("parsed_bytes", 0),
        "coverage_ratio": package_coverage.get("coverage_ratio", package_coverage.get("parsed_file_ratio")),
        "complete_analysis": package_coverage.get("complete_analysis", False),
        "limitations": package_coverage.get("limitations", []),
        "evidence_count": evidence_count,
        "structured_data": structured_overview,
    }
    value_judgment = _build_value_judgment(
        scan,
        canonical_documents,
        {"parsed_files": len(canonical_documents), "failed_files": len(failures)},
        package_coverage,
        exact_groups,
        topic_clusters,
        failures,
        pending_paths,
        structured_overview,
    )
    package_model_calls = []
    for stage_name, call_result in (
        ("semantic_cluster_naming", naming_result),
        ("lexical_topic_naming", lexical_naming_result),
        ("subtopic_naming", subtopic_naming_result),
    ):
        if not call_result:
            continue
        usage = call_result.get("usage") or {}
        context_tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        package_model_calls.append({
            "stage": stage_name,
            "model": call_result.get("model"),
            "usage": usage,
            "context_tokens": context_tokens,
            "context_window_tokens": Config.LLM_CONTEXT_TOKENS,
            "context_occupancy": round(context_tokens / float(Config.LLM_CONTEXT_TOKENS), 6),
            "finish_reason": call_result.get("finish_reason"),
            "timing": call_result.get("timing") or {},
        })
    ordered_contexts = sorted(item["context_tokens"] for item in package_model_calls)
    context_p95 = (
        ordered_contexts[int(math.ceil(0.95 * len(ordered_contexts))) - 1]
        if ordered_contexts else 0
    )
    analysis = {
        "schema_version": "package-analysis/2.0",
        "scan_id": scan_id,
        "root": scan["root"],
        "status": "completed_with_warnings" if failures or pending_paths else "completed",
        "started_from_scan_at": scan.get("scanned_at"),
        "completed_at": _now(),
        "parser_status": parser.status(),
        "statistics": {
            "scanned_files": scan.get("file_count", 0),
            "parsed_files": len(documents),
            "sampled_files": package_coverage.get("sampled_files", 0),
            "deep_analyzed_files": package_coverage.get("deep_analyzed_files", 0),
            "failed_files": len(failures),
            "pending_files": len(pending_paths),
            "scanned_bytes": package_coverage.get("scanned_bytes", package_coverage.get("inventory_bytes", 0)),
            "parsed_bytes": package_coverage.get("parsed_bytes", 0),
            "parsed_file_ratio": package_coverage.get("parsed_file_ratio"),
            "coverage_ratio": package_coverage.get("coverage_ratio", package_coverage.get("parsed_file_ratio")),
            "parsed_byte_ratio": package_coverage.get("parsed_byte_ratio"),
            "complete_analysis": package_coverage.get("complete_analysis", False),
            "coverage_limitations": package_coverage.get("limitations", []),
            "large_package_mode": policy.get("enabled"),
            "exact_duplicate_groups": len(exact_groups),
            "exact_duplicate_files": exact_duplicate_files,
            "canonical_documents": len(canonical_documents),
            "duplicate_aliases_excluded_from_analysis": len(documents) - len(canonical_documents),
            "similar_document_clusters": len(similar_groups),
            "topic_clusters": len(topic_clusters),
            "semantic_topic_clusters": len(semantic_clusters),
            "semantic_cluster_threshold": semantic_threshold,
            "semantic_cluster_error": semantic_error,
            "subtopic_nodes": sum(len(item.get("children", [])) for item in adaptive_tree.get("children", [])),
            "evidence_items": sum(len(document.get("evidence", [])) for document in canonical_documents.values()),
            "raw_evidence_items": sum(len(document.get("evidence", [])) for document in documents.values()),
            "complete_text_files": sum(1 for document in documents.values() if document.get("coverage", {}).get("complete", True)),
            "truncated_text_files": sum(1 for document in documents.values() if not document.get("coverage", {}).get("complete", True)),
            "fast_preview_paths": [
                path for path, document in sorted(documents.items())
                if document.get("coverage", {}).get("limited_by_fast_mode")
            ],
            "office_embedded_image_ocr_files": sum(1 for document in documents.values() if document.get("parser", {}).get("office_embedded_image_ocr")),
            "retrieval_evidence_chunks": retrieval.get("evidence_chunks", 0),
            "persistent_evidence_index_chunks": evidence_index_count,
            "parse_mode": actual_parse_mode,
            "reused_parse_checkpoints": reusable_count,
            "newly_processed_files": max(0, len(candidates) - reusable_count),
            "folder_summary_count": sum(1 for item in node_summaries.values() if item.get("summary_type") == "folder"),
            "local_file_summary_count": local_file_summary_count,
            "metadata_file_summary_count": len(all_paths - set(documents)),
            "small_file_summary_skipped": 0,
            "document_roles": dict(Counter(document.get("classification", {}).get("document_role", "一般资料") for document in documents.values())),
            "structured_profiled_files": structured_overview["profiled_files"],
            "structured_total_rows": structured_overview["total_rows"],
            "structured_average_quality_score": structured_overview["average_quality_score"],
            "structured_sensitive_column_count": len(structured_overview["sensitive_columns"]),
            "structured_entity_category_count": len(structured_overview["entity_statistics"]),
            "structured_recommended_question_count": len(structured_overview["recommendation_questions"]),
        },
        "exact_duplicate_groups": exact_groups,
        "similar_document_clusters": similar_groups,
        "topic_clusters": topic_clusters,
        "retrieval": retrieval,
        "research_topic_clusters": research_topic_clusters,
        "research_retrieval": research_retrieval,
        "semantic_topic_clusters": semantic_clusters,
        "semantic_cluster_threshold": semantic_threshold,
        "semantic_naming_model": semantic_naming_model,
        "lexical_topic_naming_model": (lexical_naming_result or {}).get("model") if lexical_naming_result else None,
        "subtopic_naming_model": (subtopic_naming_result or {}).get("model") if subtopic_naming_result else None,
        "semantic_cluster_error": semantic_error,
        "classification_dimensions": adaptive_tree["dimensions"],
        "analysis_tree": adaptive_tree,
        "analysis_tree_version": analysis_tree_version,
        "analysis_tree_identity_contract": "节点名称变化不改变ID；成员集合变化生成新版本，人工编辑按稳定ID重放",
        "coverage": package_coverage,
        "overview": overview,
        "value_judgment": value_judgment,
        "structured_data_overview": structured_overview,
        "model_telemetry": {
            "calls": package_model_calls,
            "call_count": len(package_model_calls),
            "context_p95_tokens": context_p95,
            "context_occupancy_p95": round(context_p95 / float(Config.LLM_CONTEXT_TOKENS), 6),
        },
        "node_summaries": node_summaries,
        "document_index": [compact_document(document) for document in documents.values()],
        "canonical_document_index": [compact_document(document) for document in canonical_documents.values()],
        "failures": failures,
        "policy": {
            "requested_parse_mode": parse_mode,
            "parse_mode": actual_parse_mode,
            "analysis_mode": policy.get("mode"),
            "large_package": policy,
            "all_nodes_have_local_summary": True,
            "docling_remote_services": False,
            "model_exception": "摘要增强仅调用已配置模型；解析、OCR、去重、聚类、建树均在本地执行。",
            "deduplication_scope": "当前数据包内部；聚类、检索、价值判断仅计算规范文档，原始路径全部保留",
        },
    }
    scan["analysis"] = analysis["statistics"]
    scan["analysis_tree"] = adaptive_tree
    scan["tree"] = scan["tree"]
    storage.save_analysis(scan_id, analysis)
    storage.update_scan(scan_id, scan)
    progress(95, "本地分析流水线完成")
    return analysis


def refresh_package_coverage(scan_id, scan, storage):
    """Refresh the visible coverage contract after a file was deep-parsed."""
    analysis = storage.get_analysis(scan_id) or {}
    if not analysis:
        return None
    policy = (analysis.get("policy") or {}).get("large_package") or build_policy(scan)
    documents = {}
    for item in storage.iter_documents(scan_id, hydrate=False):
        payload = item["payload"]
        documents[item["path"]] = (
            storage.project_document(
                payload,
                text_limit=policy.get("overview_chars_per_file", Config.LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE),
                evidence_limit=policy.get("overview_evidence_per_file", Config.LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE),
            )
            if policy.get("enabled") else payload
        )
    states = storage.iter_file_states(scan_id)
    failures = [
        {"path": item.get("node_path"), "error": item.get("error")}
        for item in states if item.get("status") == "failed"
    ]
    all_paths = set(inventory_by_path(scan))
    pending_paths = all_paths - set(documents) - {item["path"] for item in failures}
    coverage_for_paths, package_coverage = build_coverage(
        scan, documents, failures=failures, pending_paths=pending_paths, policy=policy,
    )
    if policy.get("enabled"):
        exact_groups = []
        canonical_documents = documents
    else:
        exact_groups = _group_exact(documents)
        canonical_documents, _canonical_by_path, _aliases = _canonical_projection(documents, exact_groups)
    analysis["exact_duplicate_groups"] = exact_groups
    if policy.get("enabled"):
        storage.clear_evidence_index(scan_id)
        for document_path, document in canonical_documents.items():
            storage.replace_document_evidence_index(
                scan_id, document_path, evidence_corpus({document_path: document})
            )
    else:
        storage.replace_evidence_index(scan_id, evidence_corpus(canonical_documents))
    tree = attach_tree_coverage(analysis.get("analysis_tree") or {}, coverage_for_paths, all_paths)
    analysis["analysis_tree"] = tree
    analysis["coverage"] = package_coverage
    analysis["structured_data_overview"] = _build_structured_overview(canonical_documents)
    statistics = analysis.setdefault("statistics", {})
    statistics.update({
        "parsed_files": len(documents), "failed_files": len(failures),
        "pending_files": len(pending_paths),
        "sampled_files": package_coverage.get("sampled_files", 0),
        "deep_analyzed_files": package_coverage.get("deep_analyzed_files", 0),
        "scanned_bytes": package_coverage.get("scanned_bytes", package_coverage.get("inventory_bytes", 0)),
        "parsed_bytes": package_coverage.get("parsed_bytes", 0),
        "parsed_file_ratio": package_coverage.get("parsed_file_ratio"),
        "coverage_ratio": package_coverage.get("coverage_ratio", package_coverage.get("parsed_file_ratio")),
        "parsed_byte_ratio": package_coverage.get("parsed_byte_ratio"),
        "complete_analysis": package_coverage.get("complete_analysis", False),
        "coverage_limitations": package_coverage.get("limitations", []),
        "canonical_documents": len(canonical_documents),
        "exact_duplicate_groups": len(exact_groups),
        "exact_duplicate_files": len(documents) - len(canonical_documents),
    })
    analysis["value_judgment"] = _build_value_judgment(
        scan,
        canonical_documents,
        {**statistics, "parsed_files": len(canonical_documents)},
        package_coverage,
        exact_groups,
        analysis.get("topic_clusters", []),
        failures,
        pending_paths,
        analysis.get("structured_data_overview", {}),
    )
    analysis.setdefault("overview", {}).update({
        "parsed_files": len(documents),
        "sampled_files": package_coverage.get("sampled_files", 0),
        "deep_analyzed_files": package_coverage.get("deep_analyzed_files", 0),
        "pending_files": len(pending_paths),
        "failed_files": len(failures),
        "scanned_bytes": package_coverage.get("scanned_bytes", package_coverage.get("inventory_bytes", 0)),
        "parsed_bytes": package_coverage.get("parsed_bytes", 0),
        "coverage_ratio": package_coverage.get("coverage_ratio", package_coverage.get("parsed_file_ratio")),
        "complete_analysis": package_coverage.get("complete_analysis", False),
        "limitations": package_coverage.get("limitations", []),
    })
    storage.save_analysis(scan_id, analysis)
    scan["analysis"] = statistics
    scan["analysis_tree"] = tree
    storage.update_scan(scan_id, scan)
    return analysis
