import hashlib
import json
import math
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from services.scanner import human_size, resolve_under
from services.evidence import evidence_quality, evidence_support, select_evidence
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
from services.retrieval import build_retrieval_manifest, evidence_corpus
from services.unified_parser import compact_document
from services.unified_parser import UnifiedDocumentParser
from services.parse_isolation import ParseIsolationCancelled, ParseIsolationError, runner_for


PROFILE_USABLE_STATUSES = {"completed", "partial"}


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
STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "研究", "分析", "报告",
    "文档", "文件", "资料", "进行", "一种", "基于", "相关", "情况", "数据", "方法",
}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SourceFileChangedError(RuntimeError):
    """Raised when a source is still being copied or changed after inventory."""


def _stat_signature(path):
    stat = Path(path).stat()
    return int(stat.st_size), int(stat.st_mtime_ns)


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
    current_mtime = int(Path(path).stat().st_mtime)
    if current[0] != expected_size or (expected_mtime is not None and current_mtime != expected_mtime):
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


def _parse_with_limits(parser, path, node_path, mode, cancel_check=None):
    """Apply per-file wall-clock and current-process-memory guards.

    ``resource.getrusage(...).ru_maxrss`` is a *lifetime high-water mark*,
    not the Worker\'s current memory use.  Docling/OCR may legitimately push
    that high-water mark above the configured budget while loading a model;
    using it as a preflight check would then reject every later file instantly.
    On Linux read /proc/self/statm instead, which reports the current RSS.
    """
    timeout = max(1, int(os.getenv("MAX_PARSE_SECONDS", "300")))
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
        executor.shutdown(wait=False, cancel_futures=True)


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
            path = resolve_under(scan_root, file_node["path"])
            try:
                source_signature = _assert_source_stable(path, file_node, cancel_check=cancel_check)
                document = _parse_with_limits(parser, path, file_node["path"], mode, cancel_check=cancel_check)
                _assert_source_stable(path, file_node, previous_signature=source_signature, cancel_check=cancel_check)
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

    def submit_one(index):
        file_node = candidates[index]

        def parse_task():
            child_parser = getattr(_PARSE_THREAD_LOCAL, "parser", parser)
            path = resolve_under(scan_root, file_node["path"])
            try:
                source_signature = _assert_source_stable(path, file_node, cancel_check=cancel_check)
                document = _parse_with_limits(
                    child_parser,
                    path,
                    file_node["path"],
                    mode,
                    cancel_check=cancel_check,
                )
                _assert_source_stable(path, file_node, previous_signature=source_signature, cancel_check=cancel_check)
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
            done, _pending = wait(tuple(future_map), timeout=0.25, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                future_map.pop(future, None)
                item = future.result()
                index, file_node, document, error = item
                results[index] = item
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
    return [token.lower() for token in WORD_RE.findall(text or "") if token.lower() not in STOPWORDS]


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
    # Keep the demo runnable on the project's Python 3.7 baseline, which does
    # not yet provide int.bit_count().
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
    sample = " ".join([
        " ".join(structure.get("headings", [])[:30]),
        document.get("text", "")[:12000],
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
    payload = "{}|{}|{}".format(
        dimension or "",
        name or "",
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
    return select_evidence(
        candidates,
        topics=topics or [],
        max_items=max_items,
        per_source=2,
        max_chars=520,
    )


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
            "name": "{}相关资料".format(term),
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
            conclusion = {
                # A traceable analysis unit is a valuable question, its answer,
                # and the evidence that directly supports that answer.
                "analysis_question": analysis_question,
                "question_value": question_value,
                "question": analysis_question,
                "value": question_value,
                "answer": answer,
                "statement": answer,
                "type": "问题—回答—证据",
                "confidence": confidence,
                "basis": "回答来自该子方向内文件的正文主题聚合，并由下列可回查原文证据直接支撑。",
                "evidence": evidence,
                "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
                "claims": [{
                    "statement": answer,
                    "type": "inference",
                    "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
                    "support_status": "supported" if evidence else "insufficient",
                }],
                "evidence_status": "supported" if evidence else "insufficient",
                "limitations": [] if evidence else ["当前子方向没有达到有效正文证据门槛，不能据此形成可靠结论。"],
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

            subtopic_nodes.append({
                "kind": "group",
                "node_type": "subtopic",
                "node_id": _stable_group_node_id("子方向", "{}|{}".format(topic_name, subtopic_name), paths),
                "dimension": "研究方向",
                "name": subtopic_name,
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
            })

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
            name = str(item.get("name") or subtopic["name"]).strip()[:40]
            if name:
                subtopic["name"] = name
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
                    unit["answer"] = answer
                    unit["statement"] = answer
                unit["basis"] = "该回答由本子方向的代表材料和下列可回查原文证据直接支撑。"
                for topic_conclusion in topic.get("conclusion_evidence", []):
                    if topic_conclusion.get("evidence") == unit.get("evidence"):
                        topic_conclusion.update(unit)
        return tree, result
    except Exception:
        return tree, None


def _adaptive_tree(scan, documents, node_summaries):
    """
    Theme-first adaptive analysis tree.

    一级目录只根据实际正文主题生成。
    document_role 仅作为文件标签保留，不再作为一级目录。
    """

    def stable_group_id(name, member_paths):
        payload = "内容主题|{}|{}".format(
            name or "",
            "|".join(sorted(member_paths or [])),
        )
        return "group-{}".format(
            hashlib.sha256(
                payload.encode("utf-8", errors="replace")
            ).hexdigest()[:16]
        )

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

            "classification_reason": classification.get(
                "role_reason"
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

            assignments[selected_topic].append(path)

        else:
            unassigned.append(path)

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

            unassigned.extend(
                assignments.pop(topic)
            )

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
    return _enrich_analysis_tree(tree, documents)

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
    }

    words = [
        word
        for word, _count in counts.most_common()
        if word.lower() not in generic
    ][:3]

    if words:
        return " / ".join(words)

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
            return str(headings[0])[:28]

    return "主题{}".format(
        cluster.get(
            "cluster_id",
            "",
        ).replace("SEM-", "")
    )


def _name_semantic_clusters(
    clusters,
    documents,
    llm=None,
):
    """
    Name all semantic clusters in one bounded model call.
    """
    if not clusters:
        return clusters, None

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

    if llm is None:
        return clusters, None

    lines = []

    for cluster in clusters:
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
            item = named.get(
                cluster["cluster_id"],
                {},
            )

            name = str(
                item.get("name")
                or cluster["name"]
            ).strip()[:32]

            used_names[name] += 1

            if used_names[name] > 1:
                name = "{}（{}）".format(
                    name,
                    used_names[name],
                )

            cluster["name"] = name

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

    except Exception:
        # 命名失败不能让完整分析失败；
        # 保留本地聚类和本地回退名称。
        return clusters, None


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

        node_payload = (
            name
            + "|"
            + "|".join(member_paths)
        )

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
            "node_id": "group-{}".format(
                hashlib.sha256(
                    node_payload.encode(
                        "utf-8",
                        errors="replace",
                    )
                ).hexdigest()[:16]
            ),
            "dimension": "内容主题",
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



def analyze_package(scan_id, scan, storage, parser, progress=None, embedding_client=None, llm=None,
                    large_options=None, target_paths=None, cancel_check=None):
    progress = progress or (lambda percent, message: None)
    files = list(_walk_files(scan["tree"]))
    parse_mode = "fast" if scan.get("parse_mode") == "fast" else "accurate"
    policy = build_policy(scan, large_options)
    inventory = inventory_by_path(scan)
    all_paths = set(inventory)
    target_paths = set(target_paths or []) & all_paths
    prior_states = {item.get("node_path"): item for item in storage.list_file_states(scan_id)}
    documents = {
        item["path"]: item["payload"]
        for item in storage.list_documents(scan_id, hydrate=not policy.get("enabled"))
        if item.get("path") in all_paths
    }
    failures = [
        {"path": path, "error": state.get("error") or "历史解析失败"}
        for path, state in prior_states.items()
        if state.get("status") == "failed" and path in all_paths
    ]

    if target_paths:
        candidates = [node for node in files if node.get("path") in target_paths]
        if policy.get("enabled"):
            candidates = [inventory[path] for path in representative_paths(candidates, policy["deepen_batch_files"])]
        phase_label = "补充分析"
    elif policy.get("enabled"):
        candidates = [inventory[path] for path in representative_paths(files, policy["initial_parse_files"])]
        phase_label = "大数据包首轮概览"
    else:
        candidates = files
        phase_label = "完整分析"

    actual_parse_mode = "fast" if policy.get("enabled") else parse_mode
    progress(2, "开始{}：{} 个文件".format(phase_label, len(candidates)))
    parse_candidates = []
    reusable_count = 0
    for index, file_node in enumerate(candidates, 1):
        node_path = file_node["path"]
        fingerprint = file_fingerprint(file_node)
        existing_state = prior_states.get(node_path)
        existing = documents.get(node_path)
        if (
            existing_state
            and existing_state.get("status") in {"completed", "overview"}
            and existing_state.get("fingerprint") == fingerprint
            and existing
        ):
            reusable_count += 1
            progress(2 + int(68 * index / max(1, len(candidates))), "复用检查点：{}/{} {}".format(index, len(candidates), node_path))
            continue

        parse_candidates.append((file_node, fingerprint))

    total_candidates = max(1, len(candidates))
    completed_candidates = reusable_count

    def commit_parse_result(_index, file_node, document, error):
        nonlocal completed_candidates, failures
        node_path = file_node["path"]
        fingerprint = file_fingerprint(file_node)
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
            if policy.get("enabled"):
                document = compact_overview_document(document, policy["overview_chars_per_file"])
            documents[node_path] = document
            storage.save_document(scan_id, node_path, document)
            storage.set_file_state(
                scan_id, node_path, fingerprint,
                "overview" if policy.get("enabled") else "completed", document=document,
            )
        except Exception as exc:
            failures = [item for item in failures if item.get("path") != node_path]
            failures.append({"path": node_path, "error": str(exc)})
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

        _parallel_parse_files(
            parser,
            [item[0] for item in parse_candidates],
            scan["root"],
            actual_parse_mode,
            max_workers=parse_workers,
            cancel_check=cancel_check,
            on_complete=commit_parse_result,
        )

    failed_paths = {item.get("path") for item in failures}
    pending_paths = all_paths - set(documents) - failed_paths

    for document_path, document in documents.items():
        role, details = _document_role_details(document)
        classification = document.setdefault("classification", {})
        classification["document_role"] = role
        classification["role_reason"] = details["reason"]
        classification["role_scores"] = details["scores"]
        storage.save_document(scan_id, document_path, document)

    progress(72, "执行精确去重与高相似文档聚类")
    exact_groups = _group_exact(documents)
    canonical_documents, canonical_by_path, aliases_by_canonical = _canonical_projection(
        documents, exact_groups
    )
    # Persist the canonical/alias relationship on every original path.  It is
    # visible in the physical tree and survives a Worker restart.
    for document_path, document in documents.items():
        storage.save_document(scan_id, document_path, document)
    similar_groups = _group_similar(documents, exact_groups)
    topic_clusters = _topic_clusters(canonical_documents)
    retrieval = build_retrieval_manifest(canonical_documents, topic_clusters)
    evidence_index_count = storage.replace_evidence_index(
        scan_id, evidence_corpus(canonical_documents)
    )
    research_documents = {
        path: document for path, document in canonical_documents.items()
        if document.get("classification", {}).get("document_role") not in {"要求与说明材料", "派生概览材料"}
    } or canonical_documents
    research_topic_clusters = _topic_clusters(research_documents)
    research_retrieval = build_retrieval_manifest(research_documents, research_topic_clusters)

    # ---------------------------------------------------------
    # 文档级语义聚类：正式目录的数据来源
    # ---------------------------------------------------------
    semantic_clusters = []
    semantic_threshold = None
    semantic_naming_model = None
    semantic_error = None

    if embedding_client is not None and canonical_documents and not policy.get("enabled"):
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

            if llm is not None and _optional_llm_enrichment_enabled():
                semantic_clusters, naming_result = (
                    _name_semantic_clusters(
                        semantic_clusters,
                        canonical_documents,
                        llm=llm,
                    )
                )

                if naming_result:
                    semantic_naming_model = (
                        naming_result.get("model")
                    )
            else:
                semantic_error = (
                    "可选模型增强已跳过；保留 embedding 聚类和本地规则命名"
                )

        except Exception as exc:
            semantic_error = str(exc)
            semantic_clusters = []

    progress(82, "生成所有文件夹的本地摘要与证据链")
    node_summaries = {}
    for directory in _walk_directories(scan["tree"]):
        summary = _node_summary(directory, documents)
        node_summaries[directory["path"]] = summary
        directory["simple_summary"] = summary["summary"]
        directory["evidence_count"] = len(summary["evidence_chain"])
        storage.save_summary(scan_id, directory["path"], "folder", summary)
    local_file_summary_count = 0
    for path, document in documents.items():
        summary = _file_summary(path, document)
        node_summaries[path] = summary
        storage.save_summary(scan_id, path, "file", summary)
        local_file_summary_count += 1
    for path in sorted(all_paths - set(documents)):
        state = "failed" if path in failed_paths else "pending"
        summary = _inventory_file_summary(path, inventory.get(path, {}), state=state)
        node_summaries[path] = summary
        storage.save_summary(scan_id, path, "file", summary)

    retrieved_evidence = []
    retrieved_ids = set()
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

    adaptive_tree = (
        _semantic_adaptive_tree(
            scan,
            canonical_documents,
            node_summaries,
            semantic_clusters,
        )
        if semantic_clusters
        else _adaptive_tree(
            scan,
            canonical_documents,
            node_summaries,
        )
    )
    progress(88, "生成可下钻子方向名称")
    if llm is not None and _optional_llm_enrichment_enabled():
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
    scan["tree"] = _annotate_physical_tree_deduplication(
        scan["tree"], canonical_by_path, documents, node_summaries
    )
    if policy.get("enabled"):
        waiting = pending_group(pending_paths, inventory, policy)
        if waiting:
            adaptive_tree.setdefault("children", []).append(waiting)
    coverage_for_paths, package_coverage = build_coverage(
        scan, documents, failures=failures, pending_paths=pending_paths, policy=policy,
    )
    exact_groups = _group_exact(documents)
    canonical_documents, _canonical_by_path, _aliases = _canonical_projection(documents, exact_groups)
    analysis["exact_duplicate_groups"] = exact_groups
    storage.replace_evidence_index(scan_id, evidence_corpus(canonical_documents))
    adaptive_tree = attach_tree_coverage(adaptive_tree, coverage_for_paths, all_paths)
    exact_duplicate_files = sum(group["duplicate_count"] for group in exact_groups)
    structured_profiles = []
    for profile_path, document in documents.items():
        profile = document.get("data_profile")
        if profile and profile.get("status") in PROFILE_USABLE_STATUSES:
            structured_profiles.append({"path": profile_path, "profile": profile})
        for item in document.get("data_profiles", []) or []:
            nested = item.get("profile") or {}
            if nested.get("status") in PROFILE_USABLE_STATUSES:
                structured_profiles.append({"path": "{}::{}".format(profile_path, item.get("member")), "profile": nested})
    profile_scores = [float(item["profile"].get("quality_score", 0)) for item in structured_profiles if item["profile"].get("quality_score") is not None]
    entity_statistics = {}
    recommendation_questions = []
    for category in ("person", "location", "event"):
        merged = Counter()
        columns = set()
        for item in structured_profiles:
            profile = item["profile"]
            columns.update((profile.get("entity_columns") or {}).get(category, []))
            for value in ((profile.get("entity_statistics") or {}).get(category, {}).get("top_values") or []):
                if isinstance(value, dict):
                    merged[str(value.get("value") or "")] += int(value.get("count") or 0)
        if columns or merged:
            entity_statistics[category] = {
                "columns": sorted(columns),
                "distinct_count": len(merged),
                "top_values": [{"value": key, "count": count} for key, count in merged.most_common(20)],
            }
    for item in structured_profiles:
        recommendation_questions.extend(item["profile"].get("recommendation_questions") or [])
    structured_overview = {
        "profiled_files": len(structured_profiles),
        "total_rows": sum(int(item["profile"].get("row_count") or 0) for item in structured_profiles),
        "average_quality_score": round(sum(profile_scores) / len(profile_scores), 2) if profile_scores else None,
        "missing_value_columns": sorted(set(name for item in structured_profiles for name in item["profile"].get("missing_columns", []))),
        "sensitive_columns": sorted(set(name for item in structured_profiles for name in item["profile"].get("sensitive_columns", []))),
        "entity_statistics": entity_statistics,
        "recommendation_questions": list(dict.fromkeys(recommendation_questions))[:12],
        "profiles": structured_profiles[:200],
    }
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
            "parse_mode": parse_mode,
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
        "subtopic_naming_model": (subtopic_naming_result or {}).get("model") if subtopic_naming_result else None,
        "semantic_cluster_error": semantic_error,
        "classification_dimensions": adaptive_tree["dimensions"],
        "analysis_tree": adaptive_tree,
        "coverage": package_coverage,
        "overview": overview,
        "value_judgment": value_judgment,
        "structured_data_overview": structured_overview,
        "node_summaries": node_summaries,
        "document_index": [compact_document(document) for document in documents.values()],
        "canonical_document_index": [compact_document(document) for document in canonical_documents.values()],
        "failures": failures,
        "policy": {
            "parse_mode": parse_mode,
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
    documents = {item["path"]: item["payload"] for item in storage.list_documents(scan_id, hydrate=False)}
    states = storage.list_file_states(scan_id)
    failures = [
        {"path": item.get("node_path"), "error": item.get("error")}
        for item in states if item.get("status") == "failed"
    ]
    all_paths = set(inventory_by_path(scan))
    pending_paths = all_paths - set(documents) - {item["path"] for item in failures}
    policy = (analysis.get("policy") or {}).get("large_package") or build_policy(scan)
    coverage_for_paths, package_coverage = build_coverage(
        scan, documents, failures=failures, pending_paths=pending_paths, policy=policy,
    )
    tree = attach_tree_coverage(analysis.get("analysis_tree") or {}, coverage_for_paths, all_paths)
    analysis["analysis_tree"] = tree
    analysis["coverage"] = package_coverage
    structured = []
    for path, document in canonical_documents.items():
        if (document.get("data_profile") or {}).get("status") in PROFILE_USABLE_STATUSES:
            structured.append({"path": path, "profile": document["data_profile"]})
        for item in document.get("data_profiles", []) or []:
            if (item.get("profile") or {}).get("status") in PROFILE_USABLE_STATUSES:
                structured.append({"path": "{}::{}".format(path, item.get("member")), "profile": item["profile"]})
    scores = [float(item["profile"].get("quality_score", 0)) for item in structured if item["profile"].get("quality_score") is not None]
    analysis["structured_data_overview"] = {
        "profiled_files": len(structured), "total_rows": sum(int(item["profile"].get("row_count") or 0) for item in structured),
        "average_quality_score": round(sum(scores) / len(scores), 2) if scores else None,
        "missing_value_columns": sorted(set(name for item in structured for name in item["profile"].get("missing_columns", []))),
        "sensitive_columns": sorted(set(name for item in structured for name in item["profile"].get("sensitive_columns", []))),
        "profiles": structured[:200],
    }
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
