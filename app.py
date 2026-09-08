import json
import copy
import hashlib
import hmac
import importlib.util
import itertools
import logging
import mimetypes
import logging.handlers
import os
import re
import shutil
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from web_compat import (
    InternalExecutionCapability, SJFXFastAPI, has_request_context, jsonify,
    render_template, request, send_from_directory,
)

from config import Config, ensure_runtime_directories
from services.ollama import LocalModelError, OllamaClient, OllamaEmbeddingClient
from services.vllm import VLLMClient, VLLMEmbeddingClient
from services.document_analysis import analyze_document, analyze_document_preview, analyze_document_previews_batch
from services.import_pipeline import parse_plan
from services.import_state_machine import summary_type as staged_summary_type
from services.evidence import build_file_claims, embedding_mode, select_evidence, set_embedding_provider, verify_claim_evidence
from services.exporter import create_report_docx, export_node, safe_name
from services.folder_analysis import analyze_folder
from services.package_analysis import (
    analyze_package, checkpoint_fingerprint, refresh_package_coverage, _parse_with_limits,
    _restore_source_provenance, _logical_source_snapshot, _secure_source_snapshot,
)
from services.large_package import build_policy, inventory_by_path, package_resource_plan
from services.processing_queue import (
    deep_processing_eligible,
    ranked_pending_paths,
    relationship_recall_paths,
)
from services.reporting import (
    build_local_report,
    build_report_analysis_prompt,
    compact_summary_context,
    merge_model_report,
)
from services.retrieval import evidence_corpus, retrieve_evidence
from services.scanner import (
    IGNORED_DIRS, IGNORED_FILES, human_size, resolve_under, scan_directory,
    scan_inventory_slice,
)
from services.storage import LazyStorage, Storage
from services.tree_editor import filter_tree
from services.structured_qa import answer_question
from services.unified_parser import UnifiedDocumentParser
from services.agent_runtime import PydanticAgentRuntime
from services.offline_translation import OfflineNLLBProvider
from services.translation import (
    OllamaTranslationProvider,
    StorageTranslationMemory,
    TranslationPolicy,
    TranslationService,
    UnavailableTranslationProvider,
    build_translation_plan,
    detect_language,
    document_translation_fingerprint,
)
from services.conversation import (
    CallableEvidenceRetriever,
    CallableStructuredQA,
    ConversationEngine,
    ConversationScope,
    ConversationSession,
)
from services.turn_runtime import AnalysisTurnRuntime
from services.package_overview import build_package_overview_from_storage
from services.package_exploration import (
    classify_preview, preview_as_document, preview_matches_category,
)
from services.homogeneous_documents import analyze_homogeneous_documents
from services.logical_units import iter_logical_units


MINIMUM_PYTHON_VERSION = (3, 10)


class _HomogeneousYield(RuntimeError):
    """The single Worker yielded a long homogeneous pass to foreground work."""


def _python_runtime_status(version_info=None):
    """Describe the interpreter against the documented runtime baseline."""
    current = tuple(version_info or sys.version_info)
    parts = tuple(int(value) for value in current[:3])
    parts += (0,) * (3 - len(parts))
    minimum = MINIMUM_PYTHON_VERSION
    return {
        "version": "{}.{}.{}".format(*parts),
        "minimum": "{}.{}".format(*minimum),
        "supported": parts[:2] >= minimum,
    }


@asynccontextmanager
async def _app_lifespan(_application):
    _initialize_runtime_state()
    yield


app = SJFXFastAPI(
    title="SJFX Data Analysis Agent",
    version="2.1",
    max_content_length=4 * 1024 * 1024,
    security_headers=True,
    docs_url="/docs" if Config.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if Config.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if Config.ENABLE_API_DOCS else None,
    lifespan=_app_lifespan,
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("sjfx")

# Short-lived per-process cache for repeated conversational questions.  It is
# intentionally tiny and TTL-bound: it reduces duplicate retrieval while an
# operator is polling a turn, but never replaces the durable evidence index or
# leaks data between scan IDs/scopes.
_CONVERSATION_RETRIEVAL_CACHE = {}
_CONVERSATION_RETRIEVAL_CACHE_LOCK = threading.Lock()
_CONVERSATION_RETRIEVAL_CACHE_MAX = 128
_CONVERSATION_RETRIEVAL_CACHE_TTL = 30.0


class _SensitiveLogFilter(logging.Filter):
    """Prevent accidental token/key logging from future diagnostics."""

    _patterns = (
        (re.compile(r"(Authorization\s*[:=]\s*Bearer\s+)[^\s,;]+", re.I), r"\1***"),
        (re.compile(r"([\"']?(?:api[_-]?key|token|password)[\"']?\s*[:=]\s*[\"'])[^\"']+", re.I), r"\1***"),
    )

    def filter(self, record):
        message = record.getMessage()
        for pattern, replacement in self._patterns:
            message = pattern.sub(replacement, message)
        record.msg = message
        record.args = ()
        return True


_log_filter = _SensitiveLogFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_log_filter)
# Parser libraries can otherwise emit user document names for every page.  The
# application records task ids and failure categories, not raw prompts or paths.
logging.getLogger("docling").setLevel(logging.WARNING)
logging.getLogger("RapidOCR").setLevel(logging.WARNING)
app.config["JSON_AS_ASCII"] = False
storage = LazyStorage(
    lambda: Storage(
        Config.DB_PATH,
        Config.DOCUMENT_CACHE_DIR,
        Config.SIDECAR_PAYLOAD_BYTES,
    )
)
# Bind historical pre-authentication records to the configured token before
# serving requests.  This closes the legacy "first caller claims the record"
# loophole while keeping existing demo links usable for the project owner.
_configured_owner_id = Config.OWNER_ID
_token_owner_alias = (
    hashlib.sha256(Config.API_ACCESS_TOKEN.encode("utf-8")).hexdigest()[:24]
    if Config.API_ACCESS_TOKEN else None
)
# The deployment is intentionally local-only: all generation uses the selected
# serving backend on this host. vLLM is the production default; Ollama remains
# available through LLM_BACKEND=ollama for rollback.
if Config.LLM_BACKEND == "vllm":
    llm_transport = VLLMClient(
        base_url=Config.VLLM_BASE_URL,
        model=Config.VLLM_MODEL,
        timeout=Config.VLLM_REQUEST_TIMEOUT,
        max_concurrency=Config.LLM_MAX_CONCURRENCY,
        api_key=Config.VLLM_API_KEY,
    )
    ACTIVE_LLM_BACKEND = "vllm"
else:
    llm_transport = OllamaClient(
        base_url=Config.OLLAMA_BASE_URL,
        model=Config.OLLAMA_MODEL,
        timeout=Config.SHARED_OLLAMA_REQUEST_TIMEOUT,
        max_concurrency=Config.LLM_MAX_CONCURRENCY,
    )
    ACTIVE_LLM_BACKEND = "ollama"
llm_generation_enabled = Config.LLM_ENABLED
llm = PydanticAgentRuntime(llm_transport)
translation_transport = None
if Config.ENABLE_TRANSLATION and Config.TRANSLATION_PROVIDER == "offline_nllb":
    translation_model_path = Config.TRANSLATION_MODEL_PATH
    # Checking availability must not import CT2's native runtime in the Web
    # process.  The provider imports it lazily only when translation executes.
    ct2_available = importlib.util.find_spec("ctranslate2") is not None
    if (
        getattr(Config, "TRANSLATION_PREFER_CT2", True)
        and ct2_available
        and os.path.isfile(os.path.join(Config.TRANSLATION_CT2_MODEL_PATH, "model.bin"))
    ):
        translation_model_path = Config.TRANSLATION_CT2_MODEL_PATH
    translation_provider = OfflineNLLBProvider(
        model_path=translation_model_path,
        device=Config.TRANSLATION_DEVICE,
        batch_size=Config.TRANSLATION_BATCH_SIZE,
        cpu_threads=Config.TRANSLATION_CPU_THREADS,
        max_input_tokens=Config.TRANSLATION_MAX_INPUT_TOKENS,
        max_new_tokens=Config.TRANSLATION_MAX_NEW_TOKENS,
    )
elif Config.ENABLE_TRANSLATION and Config.TRANSLATION_PROVIDER == "ollama":
    translation_transport = OllamaClient(
        base_url=Config.TRANSLATION_OLLAMA_BASE_URL,
        model=Config.TRANSLATION_OLLAMA_MODEL,
        timeout=Config.TRANSLATION_TIMEOUT_SECONDS,
        max_concurrency=1,
    )
    translation_provider = OllamaTranslationProvider(translation_transport)
elif Config.ENABLE_TRANSLATION:
    translation_provider = UnavailableTranslationProvider(
        "未知翻译提供者：{}".format(Config.TRANSLATION_PROVIDER)
    )
else:
    translation_provider = UnavailableTranslationProvider("翻译功能已由部署配置关闭")
translation_service = TranslationService(
    provider=translation_provider,
    memory=StorageTranslationMemory(storage),
    policy=TranslationPolicy(
        max_unit_chars=Config.TRANSLATION_MAX_UNIT_CHARS,
        max_attempts=Config.TRANSLATION_MAX_ATTEMPTS,
        timeout_seconds=Config.TRANSLATION_TIMEOUT_SECONDS,
        coalesce_paragraphs=Config.TRANSLATION_COALESCE_PARAGRAPHS,
        review_complex_units=Config.TRANSLATION_REVIEW_COMPLEX_UNITS,
    ),
    reviewer=(
        OllamaTranslationProvider(llm_transport)
        if (
            Config.TRANSLATION_PROVIDER == "ollama"
            and Config.ENABLE_TRANSLATION_REVIEW
            and llm_generation_enabled
        )
        else None
    ),
)

# NLLB owns a large native model and its tokenizer/decoder state.  A single
# shared execution gate makes every caller (document view, package backfill,
# conversation evidence and import pipeline) use that model predictably.  It
# prevents competing generation calls from multiplying CPU threads or causing
# native-backend contention on a shared server.
_translation_execution_lock = threading.RLock()


def _translate_document_serialized(document, **kwargs):
    with _translation_execution_lock:
        return translation_service.translate_document(document, **kwargs)
# 完整分析专用的文档级 embedding，以及交互式证据检索 embedding。
# 优先使用独立 CPU vLLM 服务，避免占用 8001 的 GPU 生成模型；Ollama
# 仅作为显式兼容回退。任一服务不可用时，evidence.py 会快速回退词法检索，
# 不得阻塞导入或分析任务。
_package_embedding_client = None
_embedding_client = None
ACTIVE_EMBEDDING_BACKEND = "disabled"
if Config.ENABLE_VLLM_EMBEDDINGS:
    _package_embedding_client = VLLMEmbeddingClient(
        Config.VLLM_EMBED_BASE_URL,
        Config.VLLM_EMBED_MODEL,
        timeout=Config.VLLM_EMBED_TIMEOUT_SECONDS,
        max_batch_size=Config.VLLM_EMBED_MAX_BATCH_SIZE,
        max_chars=Config.VLLM_EMBED_MAX_CHARS,
        api_key=Config.VLLM_API_KEY,
    )
    ACTIVE_EMBEDDING_BACKEND = "vllm-embedding"
elif Config.ENABLE_SHARED_OLLAMA_EMBEDDINGS:
    _package_embedding_client = OllamaEmbeddingClient(
        Config.OLLAMA_BASE_URL,
        Config.OLLAMA_EMBED_MODEL,
        timeout=Config.SHARED_OLLAMA_REQUEST_TIMEOUT,
        num_gpu=Config.OLLAMA_EMBED_NUM_GPU,
    )
    ACTIVE_EMBEDDING_BACKEND = "ollama-embedding"

if _package_embedding_client is not None:
    _embedding_client = _package_embedding_client
    set_embedding_provider(_embedding_client.embed, mode=ACTIVE_EMBEDDING_BACKEND)
else:
    set_embedding_provider(None)
parser = UnifiedDocumentParser(Config.DOCLING_ARTIFACTS_DIR, Config.RAPIDOCR_MODEL_DIR, Config.MAX_FULL_DOCUMENT_CHARS)

# This capability is deliberately process-local and cannot be supplied in an
# HTTP JSON document.  The dedicated Worker invokes the established summary
# implementation directly, so it needs a narrow way to cross the async queue
# gate without turning a request field into an execution-control primitive.
_summary_worker_execution = InternalExecutionCapability("sjfx_summary_worker_execution")
_summary_worker_execution_context = _summary_worker_execution.activate


def _configure_file_logging():
    if logger.handlers:
        return
    _file_handler = logging.handlers.RotatingFileHandler(
        str(Config.LOG_DIR / "app.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _file_handler.addFilter(_log_filter)
    logger.addHandler(_file_handler)
    logger.propagate = False


def _initialize_runtime_state():
    """Perform filesystem and database mutations only for a real Web startup."""
    ensure_runtime_directories()
    runtime_storage = storage.initialize()
    runtime_storage.migrate_legacy_ownership(
        _configured_owner_id,
        aliases=["legacy", "default", _token_owner_alias],
    )
    runtime_storage.register_existing_outputs(
        Config.OUTPUT_DIR, _configured_owner_id
    )
    _configure_file_logging()


def _api_token_expired():
    expiry = Config.API_TOKEN_EXPIRES_AT
    return expiry is not None and time.time() >= float(expiry)


@app.before_request
def _access_guard():
    if not Config.AUTH_REQUIRED:
        return None
    if request.path.startswith("/static/") or request.path == "/":
        return None
    if not (request.path.startswith("/api/") or request.path.startswith("/outputs/")):
        return None
    # A normal browser navigation cannot attach X-SJFX-Token. The output
    # handler atomically validates and burns this short-lived ticket before it
    # opens any artifact.
    if request.path.startswith("/outputs/") and request.args.get("ticket"):
        return None
    configured = Config.API_ACCESS_TOKEN
    supplied = request.headers.get("X-SJFX-Token", "")
    if not supplied:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
    if _api_token_expired():
        return jsonify({"ok": False, "error": "SJFX API Token 已过期，请更新服务器配置后重启"}), 401
    if not configured or not supplied or not hmac.compare_digest(supplied, configured):
        return jsonify({"ok": False, "error": "未授权访问，请提供有效的 SJFX API Token"}), 401
    return None


def _resolve_allowed_scan_root(raw_path):
    root = Path(raw_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("目录不存在或不是文件夹")
    for allowed in Config.SCAN_ALLOWED_ROOTS:
        try:
            root.relative_to(allowed)
            return root
        except ValueError:
            continue
    allowed_text = "、".join(str(item) for item in Config.SCAN_ALLOWED_ROOTS)
    raise ValueError("扫描目录不在允许范围内。当前允许根目录：{}".format(allowed_text))


def _validate_scan_path_request(raw_path):
    """Validate request shape without touching a potentially stalled NAS.

    Existence, symlink resolution and the allow-list are enforced again in the
    supervised Worker process immediately before scanning. This keeps the HTTP
    endpoint responsive even when a mounted directory is unavailable.
    """
    value = str(raw_path or "").strip()
    if not value or "\x00" in value:
        raise ValueError("请输入有效的本地目录")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("扫描目录必须使用服务器绝对路径")
    return str(candidate)


def api_error(message, status=400, details=None):
    payload = {"ok": False, "error": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status


def _request_owner_id():
    if not has_request_context():
        return None
    # The guard has already authenticated the single configured token. Keep
    # ownership independent from that secret so token rotation cannot orphan
    # historical scans, jobs, summaries or exports.
    return Config.OWNER_ID

def require_scan(scan_id):
    scan = storage.get_scan(scan_id, owner_id=_request_owner_id())
    if not scan:
        raise ValueError("扫描任务不存在、已失效或不属于当前访问用户")
    scan.setdefault("_scan_id", str(scan_id))
    return scan
def _walk_analysis_nodes(node):
    """遍历分析树中的所有节点。"""
    if not isinstance(node, dict):
        return

    yield node

    for child in node.get("children", []):
        for item in _walk_analysis_nodes(child):
            yield item


def evidence_is_formal(item):
    """Return whether one evidence record is usable in the formal directory."""
    if not isinstance(item, dict):
        return False
    if not (item.get("evidence_id") or item.get("id")):
        return False
    source = item.get("source_path") or item.get("path")
    quote = item.get("supporting_quote") or item.get("text") or item.get("content")
    if not source or not quote:
        return False
    status = str(item.get("support_status") or item.get("status") or "supported").lower()
    return status not in {"unsupported", "rejected", "unverified", "pending"}


def _find_analysis_node(scan_id, node_id):
    """
    根据 node_id 找到分析树中的虚拟主题/类别节点。
    """
    if not node_id:
        raise ValueError("缺少 node_id")

    analysis = storage.get_analysis(scan_id) or {}
    tree = analysis.get("analysis_tree") or {}

    for node in _walk_analysis_nodes(tree):
        if node.get("node_id") == node_id:
            return node

    # Candidate topics are computed from persisted preview summaries and are
    # not necessarily present in the older analysis tree. Resolve them here so
    # selecting a provisional node still expands to its concrete files.
    preview_directory = _build_candidate_preview_directory(scan_id)
    for node in (preview_directory or {}).get("topics") or []:
        if node.get("node_id") == node_id:
            return node

    raise ValueError("分析树节点不存在或已经失效，请重新执行数据包分析")


def _physical_scope_member_paths(scan, node_path):
    """Resolve one physical file/folder selection to safe relative file paths."""
    node_path = node_path or "."
    resolve_under(scan["root"], node_path)
    if scan.get("inventory_mode") == "durable_paged_v1":
        scan_id = scan.get("_scan_id") or scan.get("scan_id")
        if not storage.get_inventory_entry(scan_id, node_path):
            raise ValueError("选中的物理目录不存在")
        return storage.inventory_paths_under(scan_id, node_path)
    found = None
    stack = [scan.get("tree") or {}]
    while stack:
        node = stack.pop()
        if node.get("path") == node_path:
            found = node
            break
        stack.extend(reversed(node.get("children") or []))
    if not found:
        raise ValueError("选中的物理目录不存在")
    paths = []
    stack = [found]
    while stack:
        node = stack.pop()
        if node.get("kind") == "file" and node.get("path"):
            paths.append(node["path"])
        else:
            stack.extend(reversed(node.get("children") or []))
    return sorted(set(paths))


def _inventory_by_path(scan_result):
    if scan_result.get("inventory_mode") == "durable_paged_v1":
        scan_id = scan_result.get("_scan_id") or scan_result.get("scan_id")
        return {
            item["path"]: item["payload"]
            for item in storage.iter_inventory_entries(scan_id)
            if item.get("kind") in {"file", "logical_file"}
        }
    return inventory_by_path(scan_result)


def _requested_member_paths(scan_result, node_path):
    """Resolve a UI file/folder selection, including virtual logical files."""
    node_path = str(node_path or ".")
    if scan_result.get("inventory_mode") == "durable_paged_v1":
        logical_node = storage.get_inventory_entry(
            scan_result.get("_scan_id") or scan_result.get("scan_id"), node_path
        )
    else:
        logical_node = _inventory_by_path(scan_result).get(node_path)
    if logical_node and logical_node.get("logical_unit"):
        resolve_under(scan_result["root"], logical_node.get("container_path") or "")
        return [node_path]
    return _physical_scope_member_paths(scan_result, node_path)


def _selection_row_preview(row):
    """Merge one joined inventory row into the bounded classifier contract."""
    payload = dict(row.get("payload") or {})
    preview = dict(row.get("preview") or {})
    merged = {**payload, **preview}
    path = str(row.get("path") or merged.get("path") or "").replace("\\", "/")
    merged.setdefault("path", path)
    merged.setdefault("name", Path(path).name)
    merged.setdefault("extension", Path(path).suffix.lower())
    return merged


def _selection_filter_matches(row, selection_filter):
    """Match a lazy type/category node without materialising its members."""
    selection_filter = selection_filter or {}
    kind = str(selection_filter.get("kind") or "").casefold()
    candidate = _selection_row_preview(row)
    path = str(candidate.get("path") or row.get("path") or "").replace("\\", "/")
    extension = str(candidate.get("extension") or Path(path).suffix or "").casefold()
    if kind == "extension":
        wanted = str(selection_filter.get("extension") or "").casefold()
        if wanted in {"[no_extension]", "[无扩展名]"}:
            return not extension
        return extension == wanted
    if kind == "content_category":
        return preview_matches_category(
            candidate, str(selection_filter.get("category_id") or "")
        )
    if kind in {"path_prefix", "directory"}:
        wanted = str(selection_filter.get("path") or ".").replace("\\", "/").rstrip("/")
        return wanted in {"", "."} or path == wanted or path.startswith(wanted + "/")
    if kind in {"topic", "keyword", "content_term"}:
        terms = selection_filter.get("terms") or selection_filter.get("term") or []
        if isinstance(terms, str):
            terms = [terms]
        terms = [str(item).casefold() for item in terms if str(item)]
        if not terms and selection_filter.get("keyword"):
            terms = [str(selection_filter.get("keyword")).casefold()]
        haystack = " ".join([
            path,
            str(candidate.get("name") or ""),
            " ".join(str(item) for item in candidate.get("keywords") or []),
            str(candidate.get("preview_text") or "")[:12000],
        ]).casefold()
        return any(term and term in haystack for term in terms)
    return True


def _iter_selection_rows(scan_id, node=None, requested_paths=None):
    """Yield selection candidates lazily from durable inventory rows."""
    node = node or {}
    selection_filter = node.get("selection_filter") or {}
    member_paths = [
        str(path) for path in (requested_paths or node.get("member_paths") or [])
        if str(path)
    ]
    lazy = bool(node.get("member_paths_lazy") or selection_filter)
    if not lazy and member_paths:
        for path in dict.fromkeys(member_paths):
            payload = storage.get_inventory_entry(scan_id, path)
            if not payload:
                continue
            yield {
                "path": path,
                "kind": "file",
                "payload": payload,
                "preview": storage.get_file_preview(scan_id, path),
                "workflow": storage.get_file_workflow_state(scan_id, path) or {},
                "analysis_status": (storage.get_file_state(scan_id, path) or {}).get("status"),
            }
        return
    if hasattr(storage, "iter_inventory_selection_entries"):
        for row in storage.iter_inventory_selection_entries(
            scan_id, kind="file", batch_size=500
        ):
            if _selection_filter_matches(row, selection_filter):
                yield row
        return
    # Compatibility path for old non-paged scans. It is only used for small
    # packages, because large scans always have durable inventory rows.
    scan = require_scan(scan_id)
    inventory = _inventory_by_path(scan)
    requested_set = set(member_paths) if member_paths else None
    for path, payload in inventory.items():
        if requested_set is not None and path not in requested_set:
            continue
        row = {
            "path": path,
            "kind": "file",
            "payload": payload,
            "preview": storage.get_file_preview(scan_id, path),
            "workflow": storage.get_file_workflow_state(scan_id, path) or {},
            "analysis_status": (storage.get_file_state(scan_id, path) or {}).get("status"),
        }
        if _selection_filter_matches(row, selection_filter):
            yield row


def _large_selection_rank(row):
    """Score one selected file for bounded parsing and model promotion."""
    candidate = _selection_row_preview(row)
    classification = classify_preview(candidate)
    workflow = row.get("workflow") or {}
    score = float(workflow.get("selection_score") or 0.0)
    score += float(classification.get("confidence") or 0.0) * 20.0
    score += min(18.0, len(str(candidate.get("preview_text") or "")) / 700.0)
    score += min(12.0, len(candidate.get("keywords") or []) * 1.2)
    if str(candidate.get("extension") or "").casefold() in {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
        ".xml", ".html", ".htm", ".log", ".sql", ".pdf", ".doc", ".docx",
        ".ppt", ".pptx", ".xls", ".xlsx", ".xlsm",
    }:
        score += 8.0
    if str(classification.get("primary_category_id") or "") in {
        "security_risk", "research_reports", "policy_compliance",
    }:
        score += 5.0
    return round(score, 6), classification


def _build_large_selection_plan(scan_id, node=None, requested_paths=None, label=""):
    """Build an auditable bounded plan for a potentially huge selection."""
    scan = require_scan(scan_id)
    analysis = storage.get_analysis(scan_id) or {}
    policy = (
        (analysis.get("policy") or {}).get("large_package")
        or build_policy(scan, _package_large_options())
    )
    parse_limit = max(
        100,
        int(policy.get("selected_parse_max_files") or Config.LARGE_PACKAGE_SELECTED_PARSE_MAX_FILES),
    )
    parse_bytes_limit = max(
        256 * 1024 * 1024,
        int(policy.get("selected_parse_max_bytes") or Config.LARGE_PACKAGE_SELECTED_PARSE_MAX_BYTES),
    )
    model_limit = max(
        12,
        int(policy.get("selected_model_file_limit") or Config.LARGE_PACKAGE_SELECTED_MODEL_FILE_LIMIT),
    )
    model_bytes_limit = max(
        64 * 1024 * 1024,
        int(policy.get("selected_model_max_bytes") or Config.LARGE_PACKAGE_SELECTED_MODEL_MAX_BYTES),
    )
    batch_limit = max(
        50,
        min(500, int(policy.get("selected_batch_files") or Config.LARGE_PACKAGE_SELECTED_BATCH_FILES)),
    )
    # One selection request is one resumable work slice. Further files remain
    # explicitly deferred and can be selected again, so a single click never
    # creates an implicit multi-hour continuation chain.
    parse_limit = min(parse_limit, batch_limit)
    pool_limit = max(1000, min(12000, max(parse_limit * 3, model_limit * 8)))
    import heapq

    heap = []
    candidate_count = 0
    candidate_bytes = 0
    excluded_count = 0
    excluded_bytes = 0
    for row in _iter_selection_rows(
        scan_id, node=node, requested_paths=requested_paths
    ):
        workflow = row.get("workflow") or {}
        candidate = _selection_row_preview(row)
        size = max(0, int(candidate.get("size") or 0))
        if (
            workflow.get("promotion_allowed") is False
            or str(workflow.get("safety_status") or "") in {"restricted", "rejected"}
        ):
            excluded_count += 1
            excluded_bytes += size
            continue
        analysis_status = str(row.get("analysis_status") or "").casefold()
        if analysis_status in {"completed", "needs_attention"}:
            # Existing deep/parser results remain represented in the category,
            # but are not re-enqueued by a new selection.
            excluded_count += 1
            excluded_bytes += size
            continue
        path = str(candidate.get("path") or row.get("path") or "")
        if not path:
            continue
        score, classification = _large_selection_rank(row)
        record = {
            "path": path,
            "size": size,
            "score": score,
            "extension": str(candidate.get("extension") or "").casefold(),
            "category_id": classification.get("primary_category_id"),
            "classification_confidence": classification.get("confidence"),
            "preview_characters": int(
                candidate.get("preview_characters")
                or len(str(candidate.get("preview_text") or ""))
            ),
        }
        candidate_count += 1
        candidate_bytes += size
        if len(heap) < pool_limit:
            heapq.heappush(heap, (score, path, record))
        elif (score, path) > (heap[0][0], heap[0][1]):
            heapq.heapreplace(heap, (score, path, record))
    ranked = [
        item[2]
        for item in sorted(heap, key=lambda item: (-item[0], item[1]))
    ]

    def choose_bounded(limit, byte_limit, source):
        chosen = []
        chosen_paths = set()
        used_bytes = 0
        seen_extensions = set()
        # Reserve a small spread across formats first, then fill by score.
        for diversity_pass in (True, False):
            for record in source:
                path = record["path"]
                if len(chosen) >= limit or path in chosen_paths:
                    continue
                if diversity_pass and record.get("extension") in seen_extensions:
                    continue
                if used_bytes + record["size"] > byte_limit:
                    continue
                chosen.append(record)
                chosen_paths.add(path)
                used_bytes += record["size"]
                seen_extensions.add(record.get("extension"))
        return chosen, used_bytes

    parse_records, parse_bytes = choose_bounded(
        parse_limit, parse_bytes_limit, ranked
    )
    parse_paths = [item["path"] for item in parse_records]
    parse_set = set(parse_paths)
    model_source = [item for item in ranked if item["path"] in parse_set]
    model_source.sort(key=lambda item: (
        -float(item.get("score") or 0),
        -int(item.get("preview_characters") or 0),
        item["path"],
    ))
    model_records, model_bytes = choose_bounded(
        model_limit, model_bytes_limit, model_source
    )
    model_paths = [item["path"] for item in model_records]
    deferred_count = max(0, candidate_count - len(parse_records))
    deferred_bytes = max(0, candidate_bytes - parse_bytes)
    return {
        "schema_version": "large-selection-plan/1.0",
        "strategy": "full_inventory_category_match_then_value_bounded_parse_and_model",
        "label": str(label or (node or {}).get("name") or "大数据包选择")[:240],
        "node_id": (node or {}).get("node_id"),
        "selection_filter": (node or {}).get("selection_filter") or {},
        "selected_file_count": candidate_count,
        "selected_bytes": candidate_bytes,
        "selected_size_human": human_size(candidate_bytes),
        "excluded_file_count": excluded_count,
        "excluded_bytes": excluded_bytes,
        "normal_parse_file_count": len(parse_records),
        "normal_parse_bytes": parse_bytes,
        "normal_parse_size_human": human_size(parse_bytes),
        "model_candidate_file_count": len(model_records),
        "model_candidate_bytes": model_bytes,
        "model_candidate_size_human": human_size(model_bytes),
        "deferred_file_count": deferred_count,
        "deferred_bytes": deferred_bytes,
        "parse_file_limit": parse_limit,
        "parse_byte_limit": parse_bytes_limit,
        "model_file_limit": model_limit,
        "model_byte_limit": model_bytes_limit,
        "batch_file_limit": batch_limit,
        "parse_paths": parse_paths,
        "model_paths": model_paths,
        "representative_paths": [item["path"] for item in ranked[:100]],
        "coverage": {
            "membership_scan_complete": True,
            "selected_scope_files": candidate_count,
            "normal_parse_ratio": round(len(parse_records) / float(candidate_count or 1), 6),
            "model_ratio_of_selected": round(len(model_records) / float(candidate_count or 1), 6),
            "model_ratio_of_parsed": round(len(model_records) / float(len(parse_records) or 1), 6),
            "deferred_visible": True,
            "claim_scope": "model conclusions apply only to model_candidate_paths; category counts apply to the full selected scope",
        },
    }


def _package_documents(scan_id, canonical_only=False):
    """Avoid hydrating all deep documents when operating on a large package."""
    analysis = storage.get_analysis(scan_id) or {}
    is_large = bool(((analysis.get("policy") or {}).get("large_package") or {}).get("enabled"))
    documents = storage.list_documents(scan_id, hydrate=not is_large)
    if canonical_only:
        documents = [
            item for item in documents
            if ((item.get("payload") or {}).get("deduplication") or {}).get("role") != "duplicate_alias"
        ]
    return documents


def _virtual_node_context(scan_id, node, max_files=30, summary_stage=None):
    """
    根据虚拟主题节点的 member_paths，
    构造只属于这个节点的分析上下文。
    """
    declared_member_count = int(node.get("file_count") or 0)
    member_paths = set(node.get("member_paths") or [])
    scope_limited = bool(node.get("member_paths_lazy"))
    if scope_limited:
        # A lazy large-package category is only eligible for a node summary
        # after its bounded selection plan has been created.  Use the model
        # candidate paths (or the parsed paths as a fallback) rather than the
        # category's full membership, which may contain millions of files.
        task = storage.get_import_task(scan_id) or {}
        plan = (task.get("checkpoint") or {}).get("selection_plan") or {}
        if str(plan.get("node_id") or "") == str(node.get("node_id") or ""):
            planned = plan.get("model_paths") or plan.get("parse_paths") or []
            if planned:
                member_paths = {str(path) for path in planned if str(path)}
        if not member_paths:
            raise ValueError("该大数据包类别尚未生成有界分析计划，请先选择该类别进入解析")

    if not member_paths:
        raise ValueError("当前主题节点没有关联文件")

    if scope_limited and hasattr(storage, "iter_documents_for_paths"):
        # Large-package category membership is lazy.  Hydrate only the
        # bounded model/parse subset selected for this node; loading the
        # package-wide document table defeats the bounded workflow.
        all_documents = list(
            storage.iter_documents_for_paths(
                scan_id, sorted(member_paths), hydrate=True
            )
        )
    else:
        all_documents = _package_documents(scan_id, canonical_only=True)

    documents = [
        item
        for item in all_documents
        if item.get("path") in member_paths
    ]

    if not documents:
        return {
            "member_paths": sorted(member_paths), "inventory": [], "documents": [],
            "sampled_files": 0, "total_files": len(member_paths), "total_dirs": 0,
            "total_size": int(node.get("total_size") or 0),
            "total_size_human": node.get("total_size_human") or "0.0 B",
            "type_counts": {}, "sample_truncated": True,
            "coverage": {
                **(node.get("coverage") or {}),
                "category_file_count": declared_member_count or len(member_paths),
                "analyzed_scope_files": len(member_paths),
                "scope_limited": scope_limited,
            } or {
                "status": "待分析", "inventory_files": len(member_paths),
                "parsed_files": 0, "pending_files": len(member_paths),
            },
            "topic_clusters": [],
        }

    documents = sorted(
        documents,
        key=lambda item: item.get("path", "")
    )

    # 为模型摘要控制输入规模。
    if len(documents) > max_files:
        indices = sorted(
            set(
                round(
                    index * (len(documents) - 1)
                    / float(max_files - 1)
                )
                for index in range(max_files)
            )
        )

        sampled_documents = [
            documents[int(index)]
            for index in indices
        ]

    else:
        sampled_documents = documents

    # Node summaries must be built from the persisted file summaries, not only
    # from a few raw-document evidence snippets. Keep every member addressable
    # while bounding each field so the node prompt remains responsive.
    file_summaries = []
    missing_file_summaries = []
    non_deep_file_summaries = []
    for path in sorted(member_paths):
        summary_type = (
            staged_summary_type(summary_stage, "file")
            if summary_stage else "file"
        )
        summary = storage.get_summary(scan_id, path, summary_type) or {}
        if not summary:
            missing_file_summaries.append(path)
            continue
        def _short(value, limit):
            return " ".join(str(value or "").split())[:limit]
        level = str(summary.get("analysis_level") or summary.get("analysis_depth") or "unknown").lower()
        deep_ready = bool(summary.get("deep_analysis")) and level in {"unknown", "deep", "deep_document", "deep_folder"}
        if summary_stage == "deep" and not deep_ready:
            non_deep_file_summaries.append(path)
        raw_file_conclusions = summary.get("file_conclusions") or summary.get("conclusions") or []
        compact_file_conclusions = []
        for item in raw_file_conclusions[:3]:
            if isinstance(item, dict):
                raw_status = str(
                    item.get("support_status")
                    or item.get("verification_status")
                    or item.get("status")
                    or "unknown"
                ).lower()
                supports = []
                for support in (item.get("supports") or item.get("evidence") or []):
                    if not isinstance(support, dict):
                        continue
                    text = support.get("text") or support.get("supporting_quote") or support.get("quote")
                    if not text:
                        continue
                    supports.append({
                        "evidence_id": str(support.get("evidence_id") or ""),
                        "text": _short(text, 260),
                        "supporting_quote": _short(support.get("supporting_quote") or text, 260),
                        "source_path": support.get("source_path") or path,
                        "page": support.get("page"),
                        "section": _short(support.get("section"), 80),
                        "support_status": str(support.get("support_status") or "supported"),
                        "support_score": support.get("support_score"),
                    })
                    if len(supports) >= 2:
                        break
                compact_file_conclusions.append({
                    "statement": _short(item.get("statement") or item.get("claim") or item.get("text"), 300),
                    "type": str(item.get("type") or "observation"),
                    "support_status": {
                        "verified": "supported",
                        "supported": "supported",
                        "partially_verified": "partially_supported",
                        "inferred": "partially_supported",
                    }.get(raw_status, raw_status),
                    "supports": supports,
                })
            elif item:
                compact_file_conclusions.append({
                    "statement": _short(item, 300),
                    "type": "observation",
                    "support_status": "unknown",
                    "supports": [],
                })
        file_summaries.append({
            "path": path,
            "title": _short(summary.get("title") or summary.get("core_summary"), 160),
            "summary": _short(summary.get("summary") or summary.get("core_summary"), 500),
            "topics": [str(item)[:50] for item in (summary.get("topics") or [])[:4]],
            "key_facts": [_short(item.get("text") if isinstance(item, dict) else item, 120) for item in (summary.get("key_facts") or [])[:2]],
            "arguments": [_short(item.get("text") if isinstance(item, dict) else item, 120) for item in (summary.get("arguments") or [])[:2]],
            # conclusions 与 file_conclusions 高度重复；节点模型只消费已带证据的版本。
            "file_conclusions": compact_file_conclusions,
            "limitations": [_short(item.get("text") if isinstance(item, dict) else item, 120) for item in (summary.get("limitations") or summary.get("file_limitations") or [])[:2]],
            "analysis_level": level,
            "deep_ready": deep_ready,
            "verification_status": str(summary.get("verification_status") or "unknown"),
            "evidence_ids": [
                str(item.get("evidence_id"))
                for item in (summary.get("evidence_chain") or [])[:6]
                if isinstance(item, dict) and item.get("evidence_id")
            ],
        })

    type_counts = Counter()

    total_size = 0

    for item in documents:
        document = item.get("payload", {})

        source = document.get("source", {})

        extension = (
            source.get("extension")
            or "[无扩展名]"
        )

        type_counts[extension] += 1

        total_size += int(
            source.get("size") or 0
        )

    # -----------------------------------------------------
    # 把原来的主题簇限制在这个虚拟节点自己的文件范围内
    # -----------------------------------------------------
    analysis = storage.get_analysis(scan_id) or {}

    topic_clusters = []

    source_clusters = (
        analysis.get("research_topic_clusters")
        or analysis.get("topic_clusters")
        or []
    )

    for source in source_clusters:

        members = [
            path
            for path in source.get("members", [])
            if path in member_paths
        ]

        if not members:
            continue

        cluster = dict(source)

        cluster["members"] = members

        cluster["representative_documents"] = [
            path
            for path in source.get(
                "representative_documents",
                []
            )
            if path in member_paths
        ]

        cluster["evidence_chain"] = [
            evidence
            for evidence in source.get(
                "evidence_chain",
                []
            )
            if evidence.get("source_path")
            in member_paths
        ]

        topic_clusters.append(cluster)

        if len(topic_clusters) >= 8:
            break

    return {
        "member_paths": sorted(member_paths),
        "inventory": [
            {
                "path": item.get("path"),
                "extension": (
                    item.get("payload", {})
                    .get("source", {})
                    .get("extension")
                ),
                "size": (
                    item.get("payload", {})
                    .get("source", {})
                    .get("size", 0)
                ),
            }
            for item in documents
        ],

        "documents": sampled_documents,
        "file_summaries": file_summaries,
        "file_summary_count": len(file_summaries),
        "missing_file_summaries": missing_file_summaries,
        "non_deep_file_summaries": non_deep_file_summaries,
        "file_summaries_complete": not missing_file_summaries and len(file_summaries) == len(member_paths),
        "deep_file_summaries_complete": not missing_file_summaries and not non_deep_file_summaries and len(file_summaries) == len(member_paths),

        "sampled_files": len(sampled_documents),

        "total_files": len(documents),
        "category_file_count": declared_member_count or len(member_paths),
        "scope_limited": scope_limited,

        # 这是语义主题，不是真实文件夹
        "total_dirs": 0,

        "total_size": total_size,

        "total_size_human": human_size(
            total_size
        ),

        "type_counts": dict(
            sorted(
                type_counts.items(),
                key=lambda item: (
                    -item[1],
                    item[0]
                )
            )
        ),

        "sample_truncated": (
            len(sampled_documents)
            < len(documents)
        ),

        "coverage": {
            "mode": (
                "主题节点均匀抽样"
                if len(sampled_documents) < len(documents)
                else "主题节点全部文件"
            ),

            "inventory_files": len(documents),

            "sampled_files": len(
                sampled_documents
            ),

            "sampled_file_ratio": round(
                len(sampled_documents)
                / float(len(documents) or 1),
                6
            ),
            "category_file_count": declared_member_count or len(member_paths),
            "analyzed_scope_files": len(member_paths),
            "scope_limited": scope_limited,
            **(node.get("coverage") or {}),
        },

        "topic_clusters": topic_clusters,
    }


def _virtual_node_summary(scan_id, node, context=None):
    """Return the immediately useful, evidence-bound summary for a tree node."""
    context = context or _virtual_node_context(scan_id, node)
    conclusions = list(node.get("conclusion_evidence") or [])
    evidence = list(node.get("evidence_chain") or [])
    if not evidence:
        for file_summary in context.get("file_summaries") or []:
            for conclusion in file_summary.get("file_conclusions") or []:
                for support in conclusion.get("supports") or []:
                    if not isinstance(support, dict) or not support.get("text"):
                        continue
                    item = dict(support)
                    item.setdefault("source_path", file_summary.get("path"))
                    item.setdefault("file_conclusion", conclusion.get("statement"))
                    item.setdefault("evidence_origin", "file_summary")
                    evidence.append(item)
    if not conclusions:
        file_claims = []
        for file_summary in context.get("file_summaries") or []:
            for conclusion in file_summary.get("file_conclusions") or []:
                statement = str(conclusion.get("statement") or "").strip()
                if statement:
                    file_claims.append({
                        "statement": statement,
                        "type": conclusion.get("type") or "observation",
                        "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("file_conclusion") == statement],
                    })
        if file_claims:
            conclusions = [{
                "question": "该节点下各文件共同支持哪些结论？",
                "value": "汇总文件级分析结果并保留原文回溯",
                "answer": "；".join(item["statement"] for item in file_claims[:4]),
                "claims": file_claims[:8],
            }]
    if not evidence:
        for conclusion in conclusions:
            evidence.extend(conclusion.get("evidence", []))
    seen = set()
    evidence = [
        item for item in evidence
        if not (item.get("evidence_id") in seen or seen.add(item.get("evidence_id")))
    ]
    primary = conclusions[0] if conclusions else {}
    raw_claims = list(primary.get("claims") or [])
    if not raw_claims and primary.get("answer"):
        raw_claims = [{
            "statement": primary.get("answer"),
            "type": "inference",
            "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
        }]
    evidence_by_id = {item.get("evidence_id"): item for item in evidence if item.get("evidence_id")}
    claims = []
    verified_evidence = []
    verified_seen = set()
    for raw in raw_claims:
        raw = raw if isinstance(raw, dict) else {"statement": str(raw), "type": "observation"}
        statement = str(raw.get("statement") or raw.get("claim") or "").strip()
        candidate_ids = list(raw.get("evidence_ids") or evidence_by_id.keys())
        statuses = []
        accepted_ids = []
        for evidence_id in candidate_ids:
            item = evidence_by_id.get(evidence_id)
            if not item:
                continue
            verification = (
                {"support_status": str(item.get("support_status") or "supported"),
                 "support_reason": "继承文件摘要结论的已验证支撑原文"}
                if item.get("evidence_origin") == "file_summary"
                else verify_claim_evidence(statement, item)
            )
            status = verification.get("support_status")
            statuses.append(status)
            if status in {"supported", "partially_supported"}:
                accepted_ids.append(evidence_id)
                if evidence_id not in verified_seen:
                    verified_seen.add(evidence_id)
                    verified_item = dict(item)
                    verified_item.update(verification)
                    verified_evidence.append(verified_item)
        claim_status = (
            "supported" if "supported" in statuses
            else "partially_supported" if "partially_supported" in statuses
            else "candidate" if "candidate" in statuses
            else "insufficient"
        )
        claims.append({
            **raw,
            "statement": statement,
            "evidence_ids": accepted_ids,
            "support_status": claim_status,
        })
    supported_claims = sum(1 for item in claims if item.get("support_status") == "supported")
    partial_claims = sum(1 for item in claims if item.get("support_status") == "partially_supported")
    if claims and supported_claims == len(claims):
        evidence_status = "supported"
    elif supported_claims or partial_claims:
        evidence_status = "partially_supported"
    elif any(item.get("support_status") == "candidate" for item in claims):
        evidence_status = "candidate"
    else:
        evidence_status = "insufficient"
    answer = primary.get("answer") or node.get("summary") or "暂无足够证据形成回答。"
    if evidence_status == "insufficient":
        if evidence:
            evidence_status = "partially_supported"
            answer = primary.get("answer") or node.get("summary") or "已有文件级证据，但节点综合结论仍需人工复核。"
        else:
            answer = "证据不足，当前不能形成可靠回答。"
    qa = {
        "contract": "question-answer-evidence/3.0",
        "question": primary.get("question") or primary.get("analysis_question") or "该节点主要包含哪些内容，哪些方向值得继续下钻？",
        "value": primary.get("value") or primary.get("question_value") or "用于判断该主题是否值得继续深入分析。",
        "answer": answer,
        "claims": claims,
        "evidence": verified_evidence[:12],
        "evidence_status": evidence_status,
        "coverage": context.get("coverage", {}),
        "limitations": list(context.get("coverage", {}).get("limitations") or []) + (
            [] if evidence_status == "supported"
            else ["部分结论尚未得到直接证据支撑，建议人工复核。"] if evidence_status == "partially_supported"
            else ["当前没有有效正文证据支撑该回答。"]
        ),
    }
    verified_conclusion = {
        **primary,
        "answer": answer,
        "statement": answer,
        "claims": claims,
        "evidence": verified_evidence[:12],
        "evidence_ids": [item.get("evidence_id") for item in verified_evidence if item.get("evidence_id")],
        "evidence_status": evidence_status,
        "evidence_contract": "question-answer-evidence/3.0",
        "limitations": qa["limitations"],
    }
    return {
        "schema_version": 4,
        "summary_type": "virtual_node",
        "node_path": "node:{}".format(node.get("node_id")),
        "title": "{} 节点摘要".format(node.get("name", "分析主题")),
        "summary": node.get("summary") or "该节点由数据包中的相关资料自动聚合形成。",
        "topics": list(node.get("related_topics") or []),
        "file_count": context.get("total_files", 0),
        "member_paths": context.get("member_paths", []),
        "representative_documents": list(node.get("representative_documents") or [])[:5],
        "conclusion_evidence": [verified_conclusion] if primary or claims else [],
        "evidence_chain": verified_evidence[:12],
        "question": qa["question"],
        "value": qa["value"],
        "answer": answer,
        "claims": claims,
        "evidence_status": evidence_status,
        "evidence_contract": "question-answer-evidence/3.0",
        "unique_evidence_count": len(verified_seen),
        "independent_source_count": len({
            item.get("source_sha256") or item.get("archive_source_path") or item.get("source_path")
            for item in verified_evidence
            if item.get("source_sha256") or item.get("archive_source_path") or item.get("source_path")
        }),
        "question_answer_evidence": qa,
        "statistics": {
            "file_count": context.get("total_files", 0),
            "degraded_document_count": 0,
        },
        "parser_info": {
            "scope": "虚拟{}节点".format(node.get("node_type", "主题")),
            "coverage": context.get("coverage", {}),
            "degraded": False,
        },
    }


def _combined_export_context(scan_id, scan_result, analysis, payload):
    """Resolve mixed UI selections into one deduplicated compilation handoff."""
    selections = payload.get("selections") or [{
        "path": payload.get("path", "."), "kind": payload.get("kind", "file"),
        "node_id": payload.get("node_id"), "name": payload.get("name"),
    }]
    if not isinstance(selections, list) or not selections:
        raise ValueError("请至少选择一个主题、目录、文档或证据")
    member_paths = set()
    selection_metadata = []
    summaries = []
    explicit_evidence_ids = []
    evidence_only = True
    for raw in selections[:200]:
        if not isinstance(raw, dict):
            continue
        node_id = raw.get("node_id")
        kind = raw.get("kind") or "file"
        name = raw.get("name") or raw.get("path") or "未命名节点"
        evidence_id = raw.get("evidence_id")
        if node_id:
            node = _find_analysis_node(scan_id, node_id)
            paths = list(node.get("member_paths") or [])
            summary = storage.get_summary(scan_id, "node:{}".format(node_id), "folder") or _virtual_node_summary(scan_id, node)
            name = node.get("name") or name
        elif kind == "evidence":
            source_path = raw.get("source_path") or raw.get("path")
            if not source_path:
                raise ValueError("证据节点缺少来源文件")
            paths = [source_path]
            summary = {"title": name, "summary": "用户明确选择的原文证据。", "evidence_chain": []}
            if evidence_id:
                explicit_evidence_ids.append(evidence_id)
        else:
            paths = _requested_member_paths(scan_result, raw.get("path", "."))
            summary_type = "folder" if kind == "directory" else "file"
            summary = storage.get_summary(scan_id, raw.get("path", "."), summary_type) or {}
        member_paths.update(paths)
        evidence_only = evidence_only and kind == "evidence"
        selection_metadata.append({
            "kind": kind, "name": name, "path": raw.get("path"), "node_id": node_id,
            "evidence_id": evidence_id, "source_file_count": len(paths),
        })
        summaries.append(summary)
    if not member_paths:
        raise ValueError("所选节点未关联可导出的源文件")
    topics = []
    conclusions = []
    evidence_chain = []
    text_parts = []
    for summary in summaries:
        topics.extend(summary.get("topics") or [])
        conclusions.extend(summary.get("conclusion_evidence") or [])
        evidence_chain.extend(summary.get("evidence_chain") or summary.get("evidence") or [])
        if summary.get("summary"):
            text_parts.append(str(summary["summary"]))
    unique_topics = list(dict.fromkeys(item for item in topics if item))[:20]
    return {
        "member_paths": sorted(member_paths),
        "selection_metadata": selection_metadata,
        "selected_evidence_ids": explicit_evidence_ids if evidence_only else [],
        "summary": {
            "schema_version": 4,
            "title": "组合待整编资料包（{} 个节点）".format(len(selection_metadata)),
            "summary": "已组合 {} 个用户选中的节点，源文件按相对路径自动去重。{}".format(
                len(selection_metadata), " ".join(text_parts[:3])[:900],
            ),
            "topics": unique_topics,
            "conclusion_evidence": conclusions,
            "evidence_chain": evidence_chain,
            "selection_metadata": selection_metadata,
        },
    }

def require_local_model_enabled():
    if not llm_generation_enabled:
        flag = "ENABLE_VLLM" if Config.LLM_BACKEND == "vllm" else "ENABLE_SHARED_OLLAMA"
        raise ValueError("本地模型生成未启用，请将 {} 设置为 1 后重试".format(flag))


class JobCancelled(Exception):
    """Internal signal used to stop a cooperative analysis job."""


def _ensure_job_active(job_id):
    job = storage.get_job(job_id, owner_id=_request_owner_id())
    if not job or job.get("status") in {"cancelled", "cancelling"} or job.get("cancel_requested"):
        raise JobCancelled()
    return job


def _documents_context(scan_id, node_path, scan_result, max_files=30, max_chars=50000):
    documents = _package_documents(scan_id, canonical_only=True)
    if node_path != ".":
        prefix = node_path.rstrip("/") + "/"
        documents = [item for item in documents if item["path"] == node_path or item["path"].startswith(prefix)]
    documents = sorted(documents, key=lambda item: item["path"])
    if len(documents) > max_files:
        indices = sorted(set(round(index * (len(documents) - 1) / float(max_files - 1)) for index in range(max_files)))
        selected = [documents[int(index)] for index in indices]
    else:
        selected = documents
    per_file = max(800, min(6000, max_chars // max(1, len(selected))))
    total_text_characters = sum(len(item["payload"].get("text", "")) for item in documents)
    sampled_text_characters = sum(min(len(item["payload"].get("text", "")), per_file) for item in selected)
    samples = []
    for item in selected:
        document = item["payload"]
        samples.append({
            "path": item["path"],
            "extension": document.get("source", {}).get("extension"),
            "text": document.get("text", "")[:per_file],
            "parser": document.get("parser", {}).get("name"),
            "warnings": document.get("warnings", []),
            "evidence": document.get("evidence", [])[:3],
        })
    fallback_stats = _physical_scope_stats(scan_result, node_path)
    return {
        "inventory": [{"path": item["path"], "extension": item["payload"].get("source", {}).get("extension"), "size": item["payload"].get("source", {}).get("size", 0)} for item in documents],
        "excerpts": "\n\n".join("### {}\n{}".format(item["path"], item["payload"].get("text", "")[:per_file]) for item in selected)[:max_chars],
        "sampled_files": len(selected),
        "total_files": len(documents),
        "total_dirs": fallback_stats["total_dirs"],
        "total_size": sum(item["payload"].get("source", {}).get("size", 0) for item in documents),
        "total_size_human": fallback_stats["total_size_human"],
        "type_counts": fallback_stats["type_counts"],
        "sample_truncated": len(documents) > len(selected),
        "coverage": {
            "mode": "全量元数据 + 均匀正文抽样" if len(documents) > len(selected) else "全部文件正文抽取",
            "inventory_files": len(documents),
            "sampled_files": len(selected),
            "sampled_file_ratio": round(len(selected) / float(len(documents) or 1), 6),
            "stored_text_characters": total_text_characters,
            "sampled_text_characters": sampled_text_characters,
            "sampled_character_ratio": round(sampled_text_characters / float(total_text_characters or 1), 6),
        },
        "documents": samples,
    }


def _physical_inventory_node(scan_result, node_path):
    if scan_result.get("inventory_mode") == "durable_paged_v1":
        scan_id = scan_result.get("_scan_id") or scan_result.get("scan_id")
        return storage.get_inventory_entry(scan_id, node_path or ".")
    stack = [scan_result.get("tree") or {}]
    while stack:
        node = stack.pop()
        if node.get("path") == (node_path or "."):
            return node
        stack.extend(reversed(node.get("children") or []))
    return None


def _physical_scope_stats(scan_result, node_path):
    """Read folder totals from the immutable inventory, never live source paths."""
    target = _physical_inventory_node(scan_result, node_path)
    if not target:
        return {"total_dirs": 0, "total_size_human": "0.0 B", "type_counts": {}}
    return {
        "total_dirs": int(target.get("directory_count") or 0),
        "total_size_human": target.get("size_human") or human_size(target.get("total_size") or target.get("size") or 0),
        "type_counts": dict(target.get("type_counts") or {}),
    }


def _folder_summary_context(scan_id, node_path, scan_result):
    """Prepare sampled folder context plus representative cluster evidence."""
    context = _documents_context(scan_id, node_path, scan_result, max_files=30, max_chars=28000)
    analysis = storage.get_analysis(scan_id) or {}
    clusters = []
    prefix = node_path.rstrip("/") + "/" if node_path != "." else ""

    def in_scope(path):
        return node_path == "." or path == node_path or path.startswith(prefix)

    for source in (analysis.get("research_topic_clusters") or analysis.get("topic_clusters") or []):
        members = [path for path in source.get("members", []) if in_scope(path)]
        if not members:
            continue
        cluster = dict(source)
        cluster["members"] = members
        cluster["evidence"] = []
        for path in source.get("representative_documents", [])[:3]:
            if not in_scope(path):
                continue
            document = storage.get_document(scan_id, path) or {}
            evidence = sorted(
                [item for item in document.get("evidence", []) if item.get("text")],
                key=lambda item: len(str(item.get("text") or "")),
                reverse=True,
            )[:3]
            cluster["evidence"].extend(evidence)
        clusters.append(cluster)
        if len(clusters) >= 8:
            break
    context["topic_clusters"] = clusters
    return context


def _local_document_fallback(document, node_path, reason):
    document = document or {}
    source = document.get("source", {})
    structure = document.get("structure", {})
    text = document.get("text", "")
    preview = " ".join(text.split())[:3000]
    return {
        "title": structure.get("title") or source.get("name") or node_path,
        "structure_overview": {
            "sections": structure.get("headings", [])[:30],
            "document_type": source.get("extension", "未知类型"),
        },
        "core_summary": preview or "本地解析未提取到可摘要正文。",
        "key_facts": [],
        "arguments": [],
        "methodology": [],
        "conclusions": [],
        "uncertainties": [],
        "warnings": ["共享模型未生成最终内容，已返回本地统一解析摘要：{}".format(reason)],
        "generated_by": "local-fallback",
        "analysis_depth": "fallback",
        "deep_analysis": False,
        "evidence_chain": select_evidence(
            document.get("evidence", []),
            topics=structure.get("headings", [])[:8] + [structure.get("title"), source.get("name")],
            max_items=12,
            per_source=12,
            max_chars=520,
        ),
    }


def _analyze_report_with_model(scan_result, summaries, analysis, report_data):
    """Use the configured model for research judgment, preserving local evidence."""
    if not (llm_generation_enabled and llm.configured):
        return report_data, None, {}, "模型分析未启用；报告保留待模型分析的研究方向。"

    prompt, evidence_catalog = build_report_analysis_prompt(
        scan_result, summaries, analysis, report_data
    )
    try:
        result = llm.chat_json(
            "你是严谨的数据包分析与研究规划助手。你必须从证据中归纳，不得使用固定领域模板。"
            "事实与推论严格分开；研究建议必须可验证、可回溯。",
            prompt,
            max_tokens=1800,
            strict=True,
            retries=0,
            timeout=300,
            required_fields=("recommended_research_direction",),
            output_context="报告研究方向分析",
        )
        merged = merge_model_report(report_data, result["json"], evidence_catalog)
        merged["model_analysis"] = {
            "status": "completed",
            "model": result.get("model"),
            "evidence_catalog_size": len(evidence_catalog),
        }
        return (
            merged,
            result["model"],
            result["usage"],
            None,
        )
    except LocalModelError as exc:
        report_data["model_analysis"] = {"status": "failed", "error": str(exc)}
        return report_data, None, {}, "模型研究方向分析失败：{}。报告未使用关键词规则替代。".format(exc)


def _write_local_overview(scan_id, owner_id=None, job_id=None, summary_stage=None):
    scan_result = require_scan(scan_id)
    analysis = dict(storage.get_analysis(scan_id) or {})
    # Reports consume the same edge contract exposed to graph/search/dialogue
    # clients.  Feature-index rows remain recall signals and are not promoted
    # into report conclusions as file relationships.
    relationship_catalog = storage.get_relationship_catalog(scan_id)
    analysis["relationships"] = relationship_catalog.get("items") or []
    analysis["relationship_catalog"] = {
        key: relationship_catalog.get(key)
        for key in ("schema_version", "relationship_count", "truncated", "contract")
    }
    summaries = storage.list_summaries(scan_id)
    if summary_stage in {"preliminary", "deep"}:
        allowed_types = {staged_summary_type(summary_stage, "file"), staged_summary_type(summary_stage, "node")}
        summaries = [item for item in summaries if item.get("type") in allowed_types]
        analysis["summary_stage"] = summary_stage
        analysis["summary_stage_only"] = True
    import_task = storage.get_import_task(scan_id) or {}
    checkpoint = import_task.get("checkpoint") or {}
    selected_paths = {
        str(path) for path in (
            checkpoint.get("selected_paths")
            or checkpoint.get("deep_selected_paths")
            or []
        ) if str(path)
    }
    if selected_paths:
        # A selected-scope report must not silently reintroduce summaries from
        # files that were only inventoried. Keep file and node evidence inside
        # the current selection; metadata inventory remains available separately.
        scoped = []
        for item in summaries:
            node_path = str((item.get("payload") or {}).get("node_path") or item.get("path") or "")
            if node_path in selected_paths:
                scoped.append(item)
                continue
            members = {
                str(path) for path in ((item.get("payload") or {}).get("member_paths") or []) if str(path)
            }
            if node_path.startswith("node:") and members and members.issubset(selected_paths):
                scoped.append(item)
        summaries = scoped
        analysis["selected_scope_paths"] = sorted(selected_paths)
        analysis["selected_scope_only"] = True
    report_data = build_local_report(scan_result, summaries, analysis)
    if summary_stage == "parsed":
        report_data["summary_stage"] = "parsed"
        model_name, model_usage, warning = None, {}, "解析版概览仅使用已解析内容与证据，不调用摘要模型。"
    else:
        report_data, model_name, model_usage, warning = _analyze_report_with_model(scan_result, summaries, analysis, report_data)
    if selected_paths:
        # The report is a selected-scope projection.  ``build_local_report``
        # also exposes package-wide metrics for legacy flows; leaving those in
        # place here falsely labels a completed selected-file deep summary as
        # zero coverage simply because unselected inventory files were not read.
        inventory = _inventory_by_path(scan_result)
        selected = sorted(selected_paths)
        selected_bytes = sum(int((inventory.get(path) or {}).get("size") or 0) for path in selected)
        parsed_count = sum(1 for path in selected if storage.get_document(scan_id, path))
        stage_type = (
            staged_summary_type(summary_stage, "file")
            if summary_stage in {"preliminary", "deep"} else None
        )
        summary_count = (
            sum(1 for path in selected if storage.get_summary(scan_id, path, stage_type))
            if stage_type else parsed_count
        )
        stage_label = {
            "parsed": "已解析内容版",
            "preliminary": "初步摘要版",
            "deep": "深度证据版",
        }.get(summary_stage, "选中范围版")
        stage_deep_count = summary_count if summary_stage == "deep" else 0
        report_data["summary_stage"] = summary_stage or "selected"
        report_data["report_scope"] = {
            "kind": "selected_files",
            "selected_paths": selected,
            "selected_file_count": len(selected),
            "selected_bytes": selected_bytes,
            "selection_version": int(checkpoint.get("selection_version") or 0),
            "package_inventory_files": int(scan_result.get("file_count") or len(inventory)),
            "package_inventory_bytes": int(scan_result.get("total_size") or 0),
        }
        coverage = dict(report_data.get("coverage") or {})
        coverage.update({
            "mode": "selected_import",
            "status": stage_label,
            "selected_scope_only": True,
            "selected_scope_files": len(selected),
            "selected_scope_bytes": selected_bytes,
            "package_inventory_files": int(scan_result.get("file_count") or len(inventory)),
            "inventory_files": len(selected),
            "scanned_files": len(selected),
            "parsed_files": parsed_count,
            "document_records": parsed_count,
            "sampled_files": parsed_count,
            "deep_analyzed_files": stage_deep_count,
            "pending_files": max(0, len(selected) - (stage_deep_count or parsed_count)),
            "parsed_file_ratio": round(parsed_count / float(len(selected) or 1), 6),
            "content_parse_ratio": round(parsed_count / float(len(selected) or 1), 6),
            "deep_analysis_ratio": round(stage_deep_count / float(len(selected) or 1), 6),
            "scope_notice": "本报告仅覆盖用户选择的文件；包内其余清点文件不会被视为解析失败或摘要缺失。",
        })
        pipeline = dict(coverage.get("pipeline_coverage") or {})
        pipeline["content_parse"] = {
            "complete": parsed_count == len(selected), "eligible_files": len(selected),
            "completed_files": parsed_count, "pending_files": max(0, len(selected) - parsed_count),
            "coverage_ratio": round(parsed_count / float(len(selected) or 1), 6),
        }
        pipeline["deep_analysis"] = {
            "complete": summary_stage == "deep" and stage_deep_count == len(selected),
            "eligible_files": len(selected), "completed_files": stage_deep_count,
            "pending_files": max(0, len(selected) - stage_deep_count),
            "coverage_ratio": round(stage_deep_count / float(len(selected) or 1), 6),
        }
        coverage["pipeline_coverage"] = pipeline
        report_data["coverage"] = coverage
        report_data["basic_information"] = [
            item for item in (report_data.get("basic_information") or [])
            if not str(item).startswith(("内容分类覆盖：", "分析覆盖："))
        ] + [
            "报告范围：用户选择 {} 个文件（数据包完整清单 {} 个文件）。".format(
                len(selected), int(scan_result.get("file_count") or len(inventory))
            ),
            "当前阶段：{}；已解析 {} / {}，本阶段摘要 {} / {}。".format(
                stage_label, parsed_count, len(selected), summary_count, len(selected)
            ),
        ]
        report_data["limitations"] = [
            item for item in (report_data.get("limitations") or [])
            if "尚未完成正文解析" not in str(item)
            and not (summary_stage == "deep" and "抽样/首轮概览" in str(item))
        ]
        report_data["limitations"].insert(
            0, "本报告只对已选择范围作出结论；未选择文件保留在清单中，不参与本轮结论。"
        )
        value_judgment = dict(report_data.get("value_judgment") or {})
        value_judgment["limitations"] = list(report_data["limitations"])
        report_data["value_judgment"] = value_judgment
    # Keep the large-package taxonomy visible even when the optional report
    # model returns a compact narrative. These categories are metadata/rule
    # classifications over the full inventory, not model claims about every
    # file in the category.
    content_map = storage.get_content_map(scan_id) or {}
    if content_map and not selected_paths:
        report_data["content_categories"] = list(content_map.get("content_categories") or [])
        report_data["research_directions"] = list(content_map.get("research_directions") or [])
        report_data["content_taxonomy"] = content_map.get("content_taxonomy") or {}
        report_data["global_categories"] = [
            {
                "name": item.get("name") or item.get("category_id"),
                "file_count": int(item.get("file_count") or 0),
                "total_bytes": int(item.get("total_bytes") or 0),
                "description": item.get("description") or "",
                "research_value": item.get("research_value") or "",
                "classification_basis": item.get("classification_basis") or [],
            }
            for item in (content_map.get("content_categories") or [])
        ]
        report_data.setdefault("coverage", {}).update({
            "inventory_files": int((content_map.get("inventory") or {}).get("file_count") or 0),
            "inventory_bytes": int((content_map.get("inventory") or {}).get("total_bytes") or 0),
            "classification_coverage": float((content_map.get("coverage") or {}).get("classification_coverage") or 1.0),
            "directory_stage": "formal_taxonomy_with_bounded_model_evidence",
        })
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "情况概览报告_{}_{}_自动.docx".format(safe_name(Path(scan_result["root"]).name), stamp)
    create_report_docx(report_data, scan_result, Config.OUTPUT_DIR / name)
    storage.save_artifact(name, owner_id, scan_id=scan_id, job_id=job_id, kind="overview_report")
    storage.save_summary(scan_id, ".", "{}_report".format(summary_stage) if summary_stage else "report", report_data)
    return {
        "file_name": name,
        "download_url": "/outputs/{}".format(name),
        "report": report_data,
        "model": model_name,
        "usage": model_usage,
        "warning": warning,
    }


def _published_deep_scope(scan_id):
    """Return the deep file scope explicitly published by a report."""
    task = storage.get_import_task(scan_id) or {}
    checkpoint = dict(task.get("checkpoint") or {})
    selected = {str(path) for path in (checkpoint.get("selected_paths") or []) if str(path)}
    report = storage.get_summary(scan_id, ".", "deep_report") or {}
    if not report:
        return set(), False
    scope = dict(report.get("report_scope") or {})
    published = {str(path) for path in (scope.get("selected_paths") or []) if str(path)}
    if selected:
        if published != selected:
            return set(), False
        current_version = int(checkpoint.get("selection_version") or 0)
        report_version = int(scope.get("selection_version") or 0)
        if current_version and report_version and current_version != report_version:
            return set(), False
    return published, True


def _package_large_options():
    return {
        "threshold_bytes": Config.LARGE_PACKAGE_THRESHOLD_BYTES,
        "threshold_files": Config.LARGE_PACKAGE_THRESHOLD_FILES,
        "initial_parse_files": Config.LARGE_PACKAGE_INITIAL_PARSE_FILES,
        "deepen_batch_files": Config.LARGE_PACKAGE_DEEPEN_BATCH_FILES,
        "batch_files": Config.LARGE_PACKAGE_BATCH_FILES,
        "large_directory_mode": Config.LARGE_PACKAGE_DIRECTORY_MODE,
        "directory_model_file_limit": Config.LARGE_PACKAGE_DIRECTORY_MODEL_FILE_LIMIT,
        "preview_bytes_per_file": Config.LARGE_PACKAGE_DIRECTORY_PREVIEW_BYTES_PER_FILE,
        "preview_total_bytes": Config.LARGE_PACKAGE_DIRECTORY_PREVIEW_TOTAL_BYTES,
        "selected_parse_max_files": Config.LARGE_PACKAGE_SELECTED_PARSE_MAX_FILES,
        "selected_parse_max_bytes": Config.LARGE_PACKAGE_SELECTED_PARSE_MAX_BYTES,
        "selected_model_file_limit": Config.LARGE_PACKAGE_SELECTED_MODEL_FILE_LIMIT,
        "selected_model_max_bytes": Config.LARGE_PACKAGE_SELECTED_MODEL_MAX_BYTES,
        "selected_batch_files": Config.LARGE_PACKAGE_SELECTED_BATCH_FILES,
        "preview_zip_members": Config.LARGE_PACKAGE_PREVIEW_ZIP_MEMBERS,
        "preview_zip_member_bytes": Config.LARGE_PACKAGE_PREVIEW_ZIP_MEMBER_BYTES,
        "overview_chars_per_file": Config.LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE,
        "overview_evidence_per_file": Config.LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE,
        "background_batch_files": Config.LARGE_PACKAGE_BACKGROUND_BATCH_FILES,
        "background_backfill": Config.LARGE_PACKAGE_BACKGROUND_BACKFILL,
    }


def _package_processing_status(scan_id, scan_result=None):
    """Return an honest logical-queue snapshot for progress and controls."""
    scan_result = scan_result or require_scan(scan_id)
    inventory_files = max(0, int(scan_result.get("file_count") or 0))
    # The presentation projection is the single authority for user-visible
    # states.  The legacy queue aggregate is retained only for operational
    # scheduler details; it cannot distinguish partial/failed/processing and
    # counted archive containers after their members had become logical work.
    state_counts = storage.file_status_counts(scan_id, include_container_only=False)
    queue_counts = storage.package_processing_counts(scan_id)
    foundation_total = int(state_counts.get("total") or 0)
    parser_completed_files = int(state_counts.get("completed") or 0)
    deep_summary_counts = storage.deep_summary_counts(scan_id)
    completed_files = int(deep_summary_counts.get("file") or 0)
    pending_files = int(state_counts.get("pending") or 0)
    processing_files = int(state_counts.get("processing") or 0)
    partial_files = int(state_counts.get("partial") or 0)
    failed_files = int(state_counts.get("failed") or 0)
    excluded_files = int(state_counts.get("out_of_scope") or 0)
    retry_waiting_files = int(state_counts.get("retry_waiting") or 0)
    incomplete_files = int(state_counts.get("incomplete") or 0)
    if not foundation_total and inventory_files:
        pending_files = inventory_files
    logical_total = max(0, foundation_total - excluded_files)
    preview_counts = storage.file_preview_counts(scan_id)
    previewed_files = int(preview_counts.get("previewed") or 0) + int(
        preview_counts.get("restricted") or 0
    )
    evidence_indexed_files = storage.evidence_indexed_file_count(scan_id)
    active_job = storage.get_active_package_job(scan_id)
    control = storage.get_package_processing_control(scan_id)
    selection = storage.get_scan_selection(scan_id) or {}
    import_task_status = str((storage.get_import_task(scan_id) or {}).get("status") or "").lower()
    all_eligible_complete = bool(
        (
            foundation_total == 0
            and bool(scan_result.get("inventory_complete", not scan_result.get("truncated")))
            and not inventory_files
        )
        or (
            foundation_total > 0
            and not incomplete_files
            and not retry_waiting_files
        )
    )
    # Legacy scans may have durable analysis rows without workflow rows. Once
    # the queue is idle, close a stale running control record deterministically.
    if (
        control.get("state") == "running"
        and not active_job
        and all_eligible_complete
        and foundation_total > 0
    ):
        control = storage.set_package_processing_state(
            scan_id, "completed", "检测到全部有效逻辑文件已完成。"
        )
    return {
        **control,
        "inventory_files": inventory_files,
        "inventory_complete": bool(scan_result.get("inventory_complete", not scan_result.get("truncated"))),
        "discovery_progress": 1.0 if bool(scan_result.get("inventory_complete", not scan_result.get("truncated"))) else 0.0,
        "foundation_total_files": foundation_total,
        "previewed_files": previewed_files,
        "preview_completion_ratio": round(
            previewed_files / float(inventory_files or 1), 6
        ),
        "evidence_indexed_files": evidence_indexed_files,
        "evidence_index_ratio": round(
            evidence_indexed_files / float(logical_total or 1), 6
        ),
        "foundation_searchable_files": int(state_counts.get("light_ready") or 0),
        "foundation_searchable_ratio": round(
            int(state_counts.get("light_ready") or 0)
            / float(foundation_total or 1), 6
        ),
        "logical_total_files": logical_total,
        "container_only_files": int(state_counts.get("container_only") or 0),
        "deep_completed_files": completed_files,
        "deep_pending_files": pending_files,
        "deep_processing_files": processing_files,
        "deep_partial_files": partial_files,
        "deep_failed_files": failed_files,
        "terminal_excluded_files": excluded_files,
        "retry_waiting_files": retry_waiting_files,
        # Kept for old clients; non-retryable/attention failures are now
        # represented truthfully by deep_failed_files.
        "needs_attention_files": int(queue_counts.get("needs_attention") or 0),
        "incomplete_files": incomplete_files,
        "status_counts": state_counts,
        "deep_completion_ratio": round(
            completed_files / float(logical_total or 1), 6
        ),
        "parser_completed_files": parser_completed_files,
        "deep_summary_files": int(deep_summary_counts.get("file") or 0),
        "deep_summary_nodes": int(deep_summary_counts.get("folder") or 0),
        "batch_file_limit": max(1, min(500, int(Config.LARGE_PACKAGE_BATCH_FILES))),
        "active_job_id": active_job.get("id") if active_job else None,
        "active_job_status": active_job.get("status") if active_job else None,
        "all_eligible_complete": all_eligible_complete,
        "selection_status": selection.get("status") or ("awaiting_selection" if control.get("state") == "awaiting_selection" else None),
        "selection_version": int(selection.get("version") or 0),
        "selected_files": int(selection.get("included_count") or 0),
        "excluded_files": int(selection.get("excluded_count") or 0),
    }


def _next_package_batch(scan_id, scan_result, preferred_paths=None):
    inventory = _inventory_by_path(scan_result)
    workflow_by_path = {
        item.get("node_path"): item for item in storage.iter_file_workflow_states(scan_id)
    }
    analysis_by_path = {
        item.get("node_path"): item for item in storage.iter_file_states(scan_id)
    }
    return ranked_pending_paths(
        inventory,
        workflow_by_path,
        analysis_by_path,
        preferred_paths=preferred_paths,
        limit=Config.LARGE_PACKAGE_BATCH_FILES,
    )


def _publish_analysis_progress(scan_id, scan_result, percent, message, stage="analyzing"):
    """Publish a small, honest overview while the final analysis is running."""
    counts = storage.file_state_counts(scan_id)
    metrics = storage.file_state_metrics(scan_id)
    workflow_counts = storage.file_workflow_counts(scan_id)
    inventory_files = max(0, int(scan_result.get("file_count") or 0))
    parsed_files = int(counts.get("completed") or 0) + int(counts.get("overview") or 0)
    failed_files = int(counts.get("failed") or 0)
    accounted_files = min(inventory_files, parsed_files + failed_files)
    pending_files = max(0, inventory_files - accounted_files)
    parse_ratio = round(parsed_files / float(inventory_files or 1), 6)
    inventory_error_count = int(
        scan_result.get("scan_error_count", len(scan_result.get("errors") or [])) or 0
    )
    depth_limited = int(scan_result.get("depth_limited_directory_count") or 0)
    ignored_files = int(scan_result.get("ignored_file_count") or 0)
    ignored_directories = int(scan_result.get("ignored_directory_count") or 0)
    inventory_complete = bool(
        not scan_result.get("truncated")
        and inventory_error_count == 0
        and depth_limited == 0
        and ignored_files == 0
        and ignored_directories == 0
    )
    limitations = []
    if scan_result.get("truncated"):
        limitations.append("目录清点触及安全边界，清单之外仍可能存在对象。")
    if inventory_error_count:
        limitations.append("目录清点发生 {} 个读取错误。".format(inventory_error_count))
    if depth_limited:
        limitations.append("{} 个目录触及扫描深度上限。".format(depth_limited))
    if ignored_files or ignored_directories:
        limitations.append(
            "显式排除规则跳过 {} 个文件、{} 个目录。".format(
                ignored_files, ignored_directories
            )
        )
    limitations.append("主题聚类、深度分析覆盖率和最终证据统计将在流水线完成后发布。")
    storage.save_analysis_progress(scan_id, {
        "schema_version": "analysis-progress/1.0",
        "status": "running",
        "stage": str(stage or "analyzing"),
        "progress": max(0, min(99, int(percent or 0))),
        "message": str(message or "正在分析"),
        "coverage": {
            "status": "分析进行中",
            "coverage_level_label": (
                "完整清点 + 渐进内容解析" if inventory_complete
                else "不完整清点 + 渐进内容解析"
            ),
            "inventory_files": inventory_files,
            "inventory_coverage_ratio": 1.0 if inventory_complete else None,
            "inventory_complete": inventory_complete,
            "inventory_error_count": inventory_error_count,
            "depth_limited_directory_count": depth_limited,
            "ignored_file_count": ignored_files,
            "ignored_directory_count": ignored_directories,
            "parsed_files": parsed_files,
            "content_parse_ratio": parse_ratio,
            "failed_files": failed_files,
            "pending_files": pending_files,
            "deep_analyzed_files": 0,
            "deep_analysis_finalized": False,
            "pipeline_coverage": {
                "inventory": {
                    "complete": inventory_complete,
                    "completed_files": inventory_files,
                    "eligible_files": inventory_files,
                },
                "safety": {
                    "completed_files": workflow_counts["safety_checked"],
                    "eligible_files": inventory_files,
                },
                "light_index": {
                    "completed_files": workflow_counts["light_ready"],
                    "eligible_files": inventory_files,
                },
                "selection": {
                    "completed_files": workflow_counts["total"],
                    "eligible_files": inventory_files,
                    "state_counts": workflow_counts["selection_states"],
                },
                "content_parse": {
                    "completed_files": workflow_counts["parse_completed"],
                    "eligible_files": inventory_files,
                },
                "evidence_readiness": {
                    "completed_files": workflow_counts["evidence_ready"],
                    "eligible_files": workflow_counts["parse_completed"],
                },
            },
            "complete_analysis": False,
            "limitations": limitations,
        },
        "overview": {
            "file_count": inventory_files,
            "directory_count": int(scan_result.get("directory_count") or 0),
            "total_size": int(scan_result.get("total_size") or 0),
            "total_size_human": scan_result.get("total_size_human"),
            "parsed_files": parsed_files,
            "failed_files": failed_files,
            "pending_files": pending_files,
            "stored_text_characters": metrics["stored_characters"],
            "evidence_count": metrics["evidence_items"],
            "format_counts": scan_result.get("type_counts") or {},
        },
        "statistics": {
            "scanned_files": inventory_files,
            "parsed_files": parsed_files,
            "failed_files": failed_files,
            "pending_files": pending_files,
            "evidence_items": metrics["evidence_items"],
            "stored_text_characters": metrics["stored_characters"],
        },
    })


def _queue_candidate_node_summaries(scan_id, owner_id=None):
    """Queue initial node summaries after every candidate file summary exists."""
    task = storage.get_import_task(scan_id) or {}
    checkpoint = task.get("checkpoint") or {}
    paths = {str(path) for path in (checkpoint.get("candidate_paths") or []) if str(path)}
    if not paths:
        return []
    # The parser may refresh the cheap file projection after candidate model jobs finish.
    # Use durable candidate job state as the gate instead.
    preview_job_ids = [str(item) for item in (checkpoint.get("candidate_preview_job_ids") or []) if str(item)]
    if preview_job_ids:
        with storage._connect() as conn:
            rows = conn.execute(
                "SELECT id,status FROM analysis_jobs WHERE scan_id=? AND id IN ({})".format(",".join("?" for _ in preview_job_ids)),
                [str(scan_id)] + preview_job_ids,
            ).fetchall()
        states = {str(row["id"]): str(row["status"] or "") for row in rows}
        if any(states.get(job_id) != "completed" for job_id in preview_job_ids):
            return []
    else:
        for path in paths:
            summary = storage.get_summary(scan_id, path, "file") or {}
            source = str(summary.get("generated_by") or "").lower()
            if str(summary.get("analysis_level") or "").lower() != "preview" and source not in {"model-preview-analysis", "model-preview-batch-analysis", "model-candidate-summary"}:
                return []
    directory = _build_candidate_preview_directory(scan_id) or {}
    nodes = list(directory.get("topics") or [])
    analysis_tree = (storage.get_analysis(scan_id) or {}).get("analysis_tree") or {}
    existing_ids = {str(node.get("node_id") or "") for node in nodes}
    for tree_node in _walk_analysis_nodes(analysis_tree):
        tree_id = str(tree_node.get("node_id") or "")
        tree_members = tree_node.get("member_paths") or tree_node.get("source_paths") or []
        if tree_id and tree_id not in existing_ids and tree_members:
            nodes.append(tree_node)
            existing_ids.add(tree_id)
    jobs = []
    seen_nodes = set()
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if not node_id or node_id in seen_nodes:
            continue
        seen_nodes.add(node_id)
        members = {str(path) for path in (node.get("member_paths") or node.get("source_paths") or []) if str(path)}
        if not members or not members.issubset(paths):
            continue
        node_path = "node:{}".format(node.get("node_id"))
        existing = storage.get_summary(scan_id, node_path, "folder") or {}
        existing_level = str(existing.get("analysis_level") or existing.get("analysis_depth") or "").lower()
        # A preview job must never downgrade a durable deep node result.
        if (
            existing_level == "deep"
            or bool(existing.get("deep_analysis"))
            or str(existing.get("workflow_source") or "") == "candidate_node_summary"
            or str(existing.get("generated_by") or "") == "model-preview-node-summary"
        ):
            continue
        job_id, _created = storage.create_or_get_typed_job(
            scan_id, "generate_summary",
            options={
                "scan_id": scan_id,
                "node_id": node.get("node_id"),
                "node_name": node.get("name") or node.get("title"),
                "member_paths": sorted(members),
                "kind": "group",
                "workflow_source": "candidate_node_summary",
                "analysis_level": "preview",
            },
            owner_id=owner_id or "legacy",
        )
        jobs.append(job_id)
    return jobs


def _queue_preliminary_file_summaries(scan_id, owner_id=None):
    """Reconcile the selected-file preliminary lane after retries or restarts."""
    task = storage.get_import_task(scan_id) or {}
    if task.get("status") not in {"parsed_overview", "preliminary_summarizing"}:
        return []
    checkpoint = dict(task.get("checkpoint") or {})
    selected = [
        str(path) for path in (
            checkpoint.get("selected_paths")
            or checkpoint.get("deep_selected_paths")
            or []
        ) if str(path)
    ]
    if not selected:
        return []
    prior_ids = [str(item) for item in (checkpoint.get("preliminary_summary_job_ids") or []) if str(item)]
    with storage._connect() as conn:
        rows = conn.execute(
            "SELECT id,status FROM analysis_jobs WHERE scan_id=? AND id IN ({})".format(
                ",".join("?" for _ in prior_ids) or "NULL"
            ),
            [str(scan_id)] + prior_ids,
        ).fetchall() if prior_ids else []
    states = {str(row["id"]): str(row["status"] or "") for row in rows}
    retained = [job_id for job_id in prior_ids if states.get(job_id) in {"queued", "running", "completed", "cancelling"}]
    jobs = []
    for path in selected:
        summary = storage.get_summary(scan_id, path, staged_summary_type("preliminary", "file")) or {}
        stage = str(summary.get("analysis_stage") or summary.get("analysis_depth") or "").lower()
        generated = str(summary.get("generated_by") or "").lower()
        if stage in {"preliminary", "preliminary_document"} or generated == "model-preliminary-analysis":
            continue
        active_for_path = False
        with storage._connect() as conn:
            for row in conn.execute(
                "SELECT id,options,status FROM analysis_jobs WHERE scan_id=? AND task_type='generate_summary' AND status IN ('queued','running','cancelling')",
                (str(scan_id),),
            ):
                try:
                    options = json.loads(row["options"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    options = {}
                if str(options.get("workflow_source") or "") == "preliminary_model_summary" and str(options.get("path") or "") == path:
                    active_for_path = True
                    retained.append(str(row["id"]))
                    break
        if active_for_path:
            continue
        job_id, _created = storage.create_or_get_typed_job(
            scan_id,
            "generate_summary",
            options={
                "scan_id": scan_id,
                "path": path,
                "paths": None,
                "kind": "file",
                "force": True,
                "analysis_level": "preview",
                "workflow_source": "preliminary_model_summary",
                "deep_batch_id": checkpoint.get("deep_batch_id"),
            },
            owner_id=owner_id or "legacy",
        )
        if job_id:
            jobs.append(str(job_id))
    all_ids = list(dict.fromkeys(retained + jobs))
    if all_ids:
        checkpoint["preliminary_summary_job_ids"] = all_ids
        checkpoint["preliminary_summary_expected"] = len(all_ids)
        checkpoint["preliminary_summary_completed"] = sum(1 for job_id in all_ids if states.get(job_id) == "completed")
        checkpoint["selected_paths"] = list(dict.fromkeys(selected))
        storage.transition_import_task(
            scan_id,
            "preliminary_summarizing",
            checkpoint=checkpoint,
        )
        storage.set_package_processing_state(
            scan_id, "preliminary_summarizing", "选中文件已解析，正在生成文件初步摘要。",
        )
    return all_ids
def _queue_preliminary_node_summaries(scan_id, owner_id=None):
    """Queue node-level preliminary summaries for the selected file scope only."""
    task = storage.get_import_task(scan_id) or {}
    if task.get("status") not in {"preliminary_summarizing", "preliminary_nodes"}:
        return []
    checkpoint = dict(task.get("checkpoint") or {})
    selected = {
        str(path) for path in (
            checkpoint.get("selected_paths")
            or checkpoint.get("deep_selected_paths")
            or []
        ) if str(path)
    }
    file_job_ids = [
        str(item) for item in (checkpoint.get("preliminary_summary_job_ids") or [])
        if str(item)
    ]
    if not selected:
        return []
    if file_job_ids:
        with storage._connect() as conn:
            rows = conn.execute(
                "SELECT id,status FROM analysis_jobs WHERE scan_id=? AND id IN ({})".format(
                    ",".join("?" for _ in file_job_ids)
                ),
                [str(scan_id)] + file_job_ids,
            ).fetchall()
        states = {str(row["id"]): str(row["status"] or "") for row in rows}
        if any(states.get(job_id) != "completed" for job_id in file_job_ids):
            return []
    for path in selected:
        summary = storage.get_summary(scan_id, path, staged_summary_type("preliminary", "file")) or {}
        if (
            str(summary.get("analysis_stage") or "").lower() != "preliminary"
            and str(summary.get("generated_by") or "").lower()
                != "model-preliminary-analysis"
        ):
            return []

    analysis_tree = (storage.get_analysis(scan_id) or {}).get("analysis_tree") or {}
    nodes = list((_build_candidate_preview_directory(scan_id) or {}).get("topics") or [])
    seen = {str(node.get("node_id") or "") for node in nodes}
    for node in _walk_analysis_nodes(analysis_tree):
        node_id = str(node.get("node_id") or "")
        members = node.get("member_paths") or node.get("source_paths") or []
        if node_id and node_id not in seen and members:
            nodes.append(node)
            seen.add(node_id)

    jobs = []
    existing_job_ids = [
        str(item) for item in (checkpoint.get("preliminary_node_job_ids") or [])
        if str(item)
    ]
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        members = {
            str(path) for path in (
                node.get("member_paths") or node.get("source_paths") or []
            ) if str(path)
        }
        if not node_id or not members or not members.issubset(selected):
            continue
        node_path = "node:{}".format(node_id)
        existing = storage.get_summary(scan_id, node_path, staged_summary_type("preliminary", "node")) or {}
        existing_members = {str(path) for path in (existing.get("member_paths") or []) if str(path)}
        if str(existing.get("analysis_stage") or "").lower() == "preliminary" and existing_members == members:
            continue
        job_id, _created = storage.create_or_get_typed_job(
            scan_id,
            "generate_summary",
            options={
                "scan_id": scan_id,
                "node_id": node_id,
                "node_name": node.get("name") or node.get("title") or "选中文件节点",
                "member_paths": sorted(members),
                "kind": "group",
                "workflow_source": "preliminary_node_summary",
                "analysis_level": "preview",
            },
            owner_id=owner_id or "legacy",
        )
        if job_id:
            jobs.append(str(job_id))

    node_job_ids = list(dict.fromkeys(existing_job_ids + jobs))
    checkpoint["preliminary_node_job_ids"] = node_job_ids
    checkpoint["preliminary_node_expected"] = len(node_job_ids)
    storage.transition_import_task(
        scan_id,
        "preliminary_nodes",
        checkpoint=checkpoint,
    )
    storage.set_package_processing_state(
        scan_id, "preliminary_nodes",
        "选中文件初步摘要已完成，正在生成对应节点初步摘要。",
    )
    return node_job_ids


def _queue_preliminary_overview(scan_id, owner_id=None):
    """Queue the selected-scope overview only after both preliminary layers settle."""
    task = storage.get_import_task(scan_id) or {}
    if task.get("status") not in {"preliminary_nodes", "preliminary_overview"}:
        return None
    checkpoint = dict(task.get("checkpoint") or {})
    file_ids = [str(item) for item in (checkpoint.get("preliminary_summary_job_ids") or []) if str(item)]
    node_ids = [str(item) for item in (checkpoint.get("preliminary_node_job_ids") or []) if str(item)]
    all_ids = file_ids + node_ids
    if not all_ids:
        return None
    with storage._connect() as conn:
        rows = conn.execute(
            "SELECT id,status FROM analysis_jobs WHERE scan_id=? AND id IN ({})".format(",".join("?" for _ in all_ids)),
            [str(scan_id)] + all_ids,
        ).fetchall()
    states = {str(row["id"]): str(row["status"] or "") for row in rows}
    if any(states.get(job_id) != "completed" for job_id in all_ids):
        return None
    report_id = str(checkpoint.get("preliminary_overview_job_id") or "")
    if report_id and states.get(report_id) == "completed":
        return report_id
    if report_id:
        with storage._connect() as conn:
            row = conn.execute("SELECT status FROM analysis_jobs WHERE id=? AND scan_id=?", (report_id, str(scan_id))).fetchone()
        if row and str(row["status"] or "") in {"queued", "running", "cancelling", "completed"}:
            return report_id
    report_id, _created = storage.create_or_get_typed_job(
        scan_id,
        "generate_report",
        options={"workflow_source": "preliminary_results_report", "selection_version": checkpoint.get("selection_version")},
        owner_id=owner_id or "legacy",
    )
    checkpoint["preliminary_overview_job_id"] = report_id
    storage.transition_import_task(
        scan_id, "preliminary_overview", checkpoint=checkpoint,
    )
    storage.set_package_processing_state(
        scan_id, "preliminary_overview", "文件和节点初步摘要已完成，正在生成选中范围情报概览。",
    )
    return report_id
def _preliminary_chain_ready_for_idle(scan_id):
    """The low-priority lane may start only after both preliminary layers settle."""
    task = storage.get_import_task(scan_id) or {}
    checkpoint = task.get("checkpoint") or {}
    selected = [
        str(path) for path in (
            checkpoint.get("selected_paths")
            or checkpoint.get("deep_selected_paths")
            or []
        ) if str(path)
    ]
    if not selected:
        return False
    ids = [
        str(item) for item in (
            checkpoint.get("preliminary_summary_job_ids") or []
        ) if str(item)
    ] + [
        str(item) for item in (
            checkpoint.get("preliminary_node_job_ids") or []
        ) if str(item)
    ]
    if not ids:
        return False
    with storage._connect() as conn:
        rows = conn.execute(
            "SELECT id,status FROM analysis_jobs WHERE scan_id=? AND id IN ({})".format(
                ",".join("?" for _ in ids)
            ),
            [str(scan_id)] + ids,
        ).fetchall()
    states = {str(row["id"]): str(row["status"] or "") for row in rows}
    report_id = str(checkpoint.get("preliminary_overview_job_id") or "")
    if not report_id:
        return False
    report_row = storage._connect()
    with report_row as conn:
        report = conn.execute("SELECT status FROM analysis_jobs WHERE id=? AND scan_id=?", (report_id, str(scan_id))).fetchone()
    if not report or str(report["status"] or "") != "completed":
        return False
    return all(states.get(job_id) == "completed" for job_id in ids)


def _queue_idle_deep_backfill(scan_id, owner_id=None):
    """Queue every eligible logical file for low-priority deep analysis."""
    task = storage.get_import_task(scan_id) or {}
    checkpoint = task.get("checkpoint") or {}
    selected_paths = [
        str(path) for path in (
            checkpoint.get("selected_paths")
            or checkpoint.get("deep_selected_paths")
            or []
        ) if str(path)
    ]
    candidate_paths = [
        str(path) for path in (checkpoint.get("candidate_paths") or []) if str(path)
    ]
    if selected_paths:
        # New selection workflow: unselected inventory files are never promoted
        # into model parsing or the overview/deep-summary chain.
        if task.get("status") not in {"deep_summarizing_files", "deep_summarizing_nodes", "deep_update_available"}:
            return None
        if not _preliminary_chain_ready_for_idle(scan_id):
            return None
        candidate_paths = selected_paths
    else:
        # Preserve the legacy candidate flow until its explicit selection gate.
        candidate_summaries = {
            path: storage.get_summary(scan_id, path, "file") or {}
            for path in candidate_paths
        }
        if any(
            not item
            or (
                str(item.get("analysis_level") or "").lower() not in {"preview", "deep"}
                and not bool(item.get("deep_analysis"))
            )
            for item in candidate_summaries.values()
        ):
            return None

    scan_result = require_scan(scan_id)
    inventory_paths = sorted(_inventory_by_path(scan_result))
    paths = sorted(set(candidate_paths)) if selected_paths else (
        inventory_paths or sorted(set(candidate_paths))
    )
    with storage._connect() as conn:
        for row in conn.execute(
            "SELECT options FROM analysis_jobs WHERE scan_id=? AND status IN "
            "('queued','running')",
            (str(scan_id),),
        ):
            try:
                options = json.loads(row["options"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                options = {}
            if str(options.get("workflow_source") or "") in {"idle_deep_backfill", "idle_deep_model_summary"}:
                return None

    deep_pending = []
    for path in paths:
        summary = (storage.get_summary(scan_id, path, staged_summary_type("deep", "file")) or {}) if selected_paths else (storage.get_summary(scan_id, path, "file") or {})
        if not bool(summary.get("deep_analysis")):
            deep_pending.append(path)
    if not deep_pending:
        if selected_paths:
            _queue_deep_node_rebuilds(scan_id, owner_id)
        return None
    if selected_paths:
        path = deep_pending[0]
        job_id, _created = storage.create_or_get_typed_job(scan_id, "generate_summary", options={"scan_id": scan_id, "path": path, "kind": "file", "force": True, "analysis_level": "deep", "workflow_source": "idle_deep_model_summary"}, owner_id=owner_id or "legacy")
        checkpoint = dict(task.get("checkpoint") or {})
        job_ids = list(dict.fromkeys([*[str(item) for item in (checkpoint.get("deep_summary_job_ids") or []) if str(item)], str(job_id)]))
        checkpoint.update({"deep_summary_job_ids": job_ids, "deep_summary_file_expected": len(selected_paths), "deep_summary_expected": len(selected_paths)})
        storage.transition_import_task(scan_id, "deep_summarizing_files", checkpoint=checkpoint, reason="初步概览已完成，正在低优先级生成文件深度摘要。")
        return job_id
    job_id, _created = storage.create_or_get_typed_job(
        scan_id, "analyze_package",
        options={
            "target_paths": deep_pending,
            "workflow_source": "idle_deep_backfill",
            "scope_label": "GPU 空闲时自动深度补析全部逻辑文件",
            "parse_mode": "accurate",
            "continue_full": False,
            "idle_background": True,
        },
        owner_id=owner_id or "legacy",
    )
    return job_id


def _is_persisted_deep_file_summary(summary):
    """Return whether a file has a durable model-backed deep summary."""
    summary = summary or {}
    if not bool(summary.get("deep_analysis")):
        return False
    level = str(summary.get("analysis_level") or "").lower()
    depth = str(summary.get("analysis_depth") or "").lower()
    generated_by = str(summary.get("generated_by") or "").lower()
    return bool(level == "deep" or depth in {"deep", "deep_document"} or generated_by == "model-deep-analysis")


def _queue_deep_node_rebuilds(
    scan_id, owner_id=None, batch_id=None, scope_paths=None, full_inventory=False
):
    """Queue one deep summary per stable semantic node in the current scope."""
    task = storage.get_import_task(scan_id) or {}
    checkpoint = dict(task.get("checkpoint") or {})
    strict_selected = bool(checkpoint.get("selected_paths"))
    deep_file_type = staged_summary_type("deep", "file") if strict_selected else "file"
    deep_node_type = staged_summary_type("deep", "node") if strict_selected else "folder"
    batch_id = "" if full_inventory else str(batch_id or checkpoint.get("deep_batch_id") or "")
    selected = [str(path) for path in (scope_paths or []) if str(path)]
    if full_inventory:
        scan_result = require_scan(scan_id)
        selected = sorted(_inventory_by_path(scan_result))
    elif not selected:
        selected = [str(path) for path in (checkpoint.get("deep_selected_paths") or checkpoint.get("selected_paths") or []) if str(path)]
    if not selected:
        # Legacy imports without a persisted selection use the complete inventory.
        scan_result = require_scan(scan_id)
        selected = sorted(_inventory_by_path(scan_result))
    paths = set(selected)
    if strict_selected and task.get("status") not in {"deep_summarizing_files", "deep_summarizing_nodes", "deep_update_available"}:
        return []
    all_files_ready = bool(paths) and all(
        _is_persisted_deep_file_summary(storage.get_summary(scan_id, path, deep_file_type) or {})
        for path in paths
    )
    if not paths or (not strict_selected and not all_files_ready):
        return []

    analysis_tree = (storage.get_analysis(scan_id) or {}).get("analysis_tree") or {}
    nodes = []
    seen = set()
    for node in (_build_candidate_preview_directory(scan_id) or {}).get("topics") or []:
        node_id = str(node.get("node_id") or "")
        if node_id and node_id not in seen:
            nodes.append(node)
            seen.add(node_id)
    for node in _walk_analysis_nodes(analysis_tree):
        node_id = str(node.get("node_id") or "")
        members = node.get("member_paths") or node.get("source_paths") or []
        if node_id and members and node_id not in seen:
            nodes.append(node)
            seen.add(node_id)

    jobs = []
    for node in nodes:
        members = {str(path) for path in (node.get("member_paths") or node.get("source_paths") or []) if str(path)}
        if not members or not members.issubset(paths):
            continue
        # A node is eligible independently when all of its own selected files
        # have deep summaries; unrelated selected files must not hold it back.
        if any(
            not _is_persisted_deep_file_summary(
                storage.get_summary(scan_id, path, deep_file_type) or {}
            )
            for path in members
        ):
            continue
        node_id = str(node.get("node_id") or "")
        node_path = "node:{}".format(node_id)
        existing = storage.get_summary(scan_id, node_path, deep_node_type) or {}
        existing_members = {str(path) for path in (existing.get("member_paths") or []) if str(path)}
        if str(existing.get("analysis_depth") or "") == "deep_folder" and bool(existing.get("deep_analysis")) and existing_members == members:
            continue
        job_id, _created = storage.create_or_get_typed_job(
            scan_id,
            "generate_summary",
            options={
                "scan_id": scan_id,
                "node_id": node_id,
                "node_name": node.get("name") or node.get("title") or "候选主题",
                "kind": "group",
                "workflow_source": "deep_node_rebuild",
                "analysis_level": "deep",
                "member_paths": sorted(members),
                "deep_batch_id": batch_id or None,
            },
            owner_id=owner_id or "legacy",
        )
        if job_id:
            jobs.append(str(job_id))

    # Keep the import-task denominator equal to file summaries + node summaries.
    if jobs and str(task.get("status") or "") in {"summarizing_files", "deep_summarizing_files", "deep_summarizing_nodes"}:
        prior = [str(item) for item in (checkpoint.get("deep_summary_node_job_ids") or []) if str(item)]
        node_job_ids = list(dict.fromkeys(prior + jobs))
        checkpoint["deep_summary_node_job_ids"] = node_job_ids
        file_expected = int(checkpoint.get("deep_summary_file_expected") or checkpoint.get("deep_summary_expected") or 0)
        checkpoint["deep_summary_file_expected"] = file_expected

        checkpoint["deep_summary_expected"] = file_expected + len(node_job_ids)
        storage.update_import_task(scan_id, checkpoint=checkpoint)
        if strict_selected and all_files_ready:
            storage.transition_import_task(scan_id, "deep_summarizing_nodes", checkpoint=checkpoint, reason="文件深度摘要已完成，正在汇总节点深度摘要。")
    if strict_selected and not jobs:
        _mark_deep_update_available(scan_id)
    return jobs

def _mark_deep_update_available(scan_id):
    """Expose completed deep evidence without applying it to user outputs."""
    task = storage.get_import_task(scan_id) or {}
    checkpoint = dict(task.get("checkpoint") or {})
    selected = [str(path) for path in (checkpoint.get("selected_paths") or []) if str(path)]
    if not selected or any(
        not _is_persisted_deep_file_summary(
            storage.get_summary(scan_id, path, staged_summary_type("deep", "file")) or {}
        ) for path in selected
    ):
        return False
    checkpoint["deep_summary_file_expected"] = len(selected)
    node_job_ids = [str(item) for item in (checkpoint.get("deep_summary_node_job_ids") or []) if str(item)]
    checkpoint["deep_summary_expected"] = len(selected) + len(node_job_ids)
    storage.update_import_task(scan_id, checkpoint=checkpoint)
    with storage._connect() as conn:
        rows = conn.execute(
            "SELECT options FROM analysis_jobs WHERE scan_id=? AND task_type='generate_summary' "
            "AND status IN ('queued','running','cancelling')", (str(scan_id),)
        ).fetchall()
    for row in rows:
        try:
            source = str(json.loads(row["options"] or "{}").get("workflow_source") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            source = ""
        if source in {"idle_deep_model_summary", "deep_node_rebuild"}:
            return False
    if str(task.get("status") or "") in {"deep_summarizing_files", "deep_summarizing_nodes", "deep_overview_updating"}:
        storage.transition_import_task(
            scan_id, "deep_update_available", checkpoint=checkpoint,

            reason="深度摘要已就绪，等待用户选择是否按深度证据更新目录和情报概览。",
        )
    return True
def _queue_large_selection_node_summary(scan_id, owner_id=None, current_job_id=None):
    """Queue one node aggregation after a bounded model subset is complete."""
    task = storage.get_import_task(scan_id) or {}
    checkpoint = task.get("checkpoint") or {}
    plan = checkpoint.get("selection_plan") or {}
    if not checkpoint.get("large_selection") or not plan:
        return None
    node_id = str(plan.get("node_id") or "").strip()
    model_paths = [str(path) for path in (plan.get("model_paths") or []) if str(path)]
    if not node_id or not model_paths:
        return None
    # A model job writes either a model result or an explicit local fallback.
    # Wait for durable summaries rather than ``deep_analysis`` alone, so one
    # unavailable model cannot leave the node aggregation queued forever.
    summaries = {
        path: storage.get_summary(scan_id, path, "file") or {}
        for path in model_paths
    }
    if any(not summaries[path] for path in model_paths):
        with storage._connect() as conn:
            rows = conn.execute(
                "SELECT id,options FROM analysis_jobs WHERE scan_id=? "
                "AND task_type='generate_summary' AND status IN ('queued','running','cancelling')",
                (str(scan_id),),
            ).fetchall()
        required = set(model_paths)
        for row in rows:
            if current_job_id and str(row["id"] or "") == str(current_job_id):
                continue
            try:
                options = json.loads(row["options"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                options = {}
            if str(options.get("workflow_source") or "") != "large_selection_model_summary":
                continue
            job_paths = set(str(path) for path in (options.get("paths") or []) if str(path))
            if options.get("path"):
                job_paths.add(str(options.get("path")))
            if required.intersection(job_paths):
                return None
        return None
    node = None
    try:
        node = _find_analysis_node(scan_id, node_id)
    except ValueError:
        node = {
            "node_id": node_id,
            "name": plan.get("label") or "大数据包选定类别",
            "kind": "group",
            "member_paths": model_paths,
            "file_count": int(plan.get("selected_file_count") or len(model_paths)),
            "member_paths_lazy": True,
        }
    declared_count = int(
        node.get("file_count")
        or plan.get("selected_file_count")
        or len(model_paths)
    )
    job_id, _created = storage.create_or_get_typed_job(
        scan_id,
        "generate_summary",
        options={
            "scan_id": scan_id,
            "node_id": node_id,
            "node_name": node.get("name") or plan.get("label") or "选定类别",
            "member_paths": model_paths,
            "kind": "group",
            "workflow_source": "deep_node_rebuild",
            "analysis_level": "deep",
            "selection_plan": plan,
        },
        owner_id=owner_id or "legacy",
    )
    # The selected-category node is a real deep-summary task and must be in
    # the same denominator as its file summaries.
    task = storage.get_import_task(scan_id) or {}
    checkpoint = dict(task.get("checkpoint") or {})
    if job_id and str(task.get("status") or "") == "summarizing_files":
        node_job_ids = list(dict.fromkeys([
            *[str(item) for item in (checkpoint.get("deep_summary_node_job_ids") or []) if str(item)],
            str(job_id),
        ]))
        file_expected = int(checkpoint.get("deep_summary_file_expected") or checkpoint.get("deep_summary_expected") or 0)
        checkpoint["deep_summary_file_expected"] = file_expected
        checkpoint["deep_summary_node_job_ids"] = node_job_ids
        checkpoint["deep_summary_expected"] = file_expected + len(node_job_ids)
        storage.update_import_task(scan_id, checkpoint=checkpoint)
    return job_id


def _run_claimed_report_job(job):
    """Generate an on-demand overview report outside the HTTP request."""
    job_id = job["id"]
    scan_id = job["scan_id"]
    storage.update_job(job_id, progress=5, stage="generating_report", message="正在整理情况概览报告", heartbeat=True)
    _ensure_job_active(job_id)
    workflow_source = str((job.get("options") or {}).get("workflow_source") or "")
    summary_stage = "preliminary" if workflow_source == "preliminary_results_report" else ("deep" if workflow_source == "deep_results_report" else None)
    report = _write_local_overview(scan_id, owner_id=job.get("owner_id"), job_id=job_id, summary_stage=summary_stage)
    _ensure_job_active(job_id)
    return {"scan_id": scan_id, "overview": report}


def _run_claimed_summary_job(job):
    global llm
    original_model = llm
    options = job.get("options") or {}
    if options.get("workflow_source") in {"idle_deep_model_summary", "deep_node_rebuild"}:
        from services.import_checkpoint import CheckpointedModel
        import hashlib
        identity = {
            "scan_id": str(job.get("scan_id") or ""),
            "workflow_source": str(options.get("workflow_source") or ""),
            "path": str(options.get("path") or ""),
            "node_id": str(options.get("node_id") or ""),
            "member_paths": sorted(str(path) for path in (options.get("member_paths") or []) if str(path)),
        }
        checkpoint_key = "import:" + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        llm = CheckpointedModel(original_model, storage, checkpoint_key)
    try:
        return _execute_summary_job(job)
    finally:
        llm = original_model


def _execute_summary_job(job):
    """Run an uncached model-backed node/document summary outside Flask HTTP."""
    job_id = job["id"]
    payload = dict(job.get("options") or {})
    # Ignore a historical client-controlled field if it was persisted by an
    # older API process. Internal execution authority lives only in the
    # ContextVar below.
    payload.pop("_worker_execution", None)
    scan_id = job["scan_id"]
    workflow_source = str(payload.get("workflow_source") or "")
    preview_mode = (
        str(payload.get("workflow_source") or "") == "candidate_preview_model_summary"
        or (
            str(payload.get("analysis_level") or "").lower() == "preview"
            and not str(payload.get("node_id") or "").strip()
            and workflow_source in {"candidate_preview_model_summary", "preliminary_model_summary"}
        )
    )
    if preview_mode:
        batch_paths = [str(path).strip() for path in (payload.get("paths") or []) if str(path).strip()]
        if batch_paths:
            storage.update_job(job_id, progress=5, stage="candidate_preview", message="正在批量生成候选文件预览摘要（{} 个）".format(len(batch_paths)), heartbeat=True)
            documents = []
            for path in batch_paths:
                document = storage.get_document(scan_id, path)
                if document:
                    documents.append({"path": path, "document": document})
            if not documents:
                raise ValueError("候选文件尚未完成统一解析")
            batch_result = {}
            degraded_paths = []
            try:
                deadline = float(payload.get("candidate_deadline_at") or 0)
                if deadline and time.time() >= deadline:
                    raise TimeoutError("candidate preview batch deadline reached")
                require_local_model_enabled()
                batch_result, _ = analyze_document_previews_batch(
                    llm, documents,
                    max_chars=int(getattr(Config, "CANDIDATE_PREVIEW_BATCH_CHARS", 3200)),
                    context_window_tokens=Config.LLM_CONTEXT_TOKENS,
                    timeout_seconds=int(getattr(Config, "CANDIDATE_PREVIEW_TIMEOUT_SECONDS", 60)) * 2,
                    output_tokens_per_file=int(getattr(Config, "CANDIDATE_PREVIEW_OUTPUT_TOKENS", 240)),
                )
            except Exception as batch_exc:
                # A malformed/truncated batch response must not downgrade every
                # useful file. Retry each document independently, then use the
                # local parser fallback only for files whose individual model
                # request also fails.
                for item in documents:
                    path, document = item["path"], item["document"]
                    try:
                        summary, _single_result = analyze_document_preview(
                            llm, path, path, unified_document=document,
                            max_chars=int(getattr(Config, "CANDIDATE_PREVIEW_BATCH_CHARS", 3200)),
                            context_window_tokens=Config.LLM_CONTEXT_TOKENS,
                            timeout_seconds=int(getattr(Config, "CANDIDATE_PREVIEW_TIMEOUT_SECONDS", 60)),
                        )
                        batch_result[path] = {"summary": summary}
                        continue
                    except Exception as item_exc:
                        error = "批量预读失败：{}；单文件重试失败：{}".format(
                            str(batch_exc)[:1200], str(item_exc)[:1200]
                        )
                    fallback = _local_document_fallback(document, path, error)
                    fallback.update({"schema_version": 4, "summary_type": "file", "node_path": path,
                        "analysis_level": "preview", "analysis_depth": "preview_document",
                        "verification_status": "candidate", "preview_only": True,
                        "claim_contract": "file-claims/preview-1.0", "file_conclusions": [],
                        "file_arguments": [], "file_review_items": [{"type": "preview", "text": "快速模型预览未完成，当前为本地解析降级结果。", "status": "review"}],
                        "generated_by": "local-preview-fallback", "deep_analysis": False})
                    batch_result[path] = {"summary": fallback}
                    degraded_paths.append(path)
            saved = []
            for item in documents:
                path = item["path"]
                summary = dict((batch_result.get(path) or {}).get("summary") or {})
                summary.setdefault("node_path", path)
                summary.setdefault("summary_type", "file")
                summary.setdefault("analysis_level", "preview")
                summary.setdefault("analysis_depth", "preview_document")
                summary.setdefault("verification_status", "candidate")
                summary.setdefault("preview_only", True)
                summary.setdefault("deep_analysis", False)
                summary.setdefault("generated_by", "model-preview-batch-analysis")
                # Preliminary import summaries are a durable stage of the
                # strict import state machine. Never put them in the legacy
                # ``file`` slot, otherwise a later deep result can be hidden
                # or overwritten by an old compatibility reader.
                if workflow_source == "preliminary_model_summary":
                    summary.update(_preview_claim_contract(
                        summary, (item.get("document") or {}).get("evidence", [])
                    ))
                    summary.update({
                        "analysis_stage": "preliminary",
                        "analysis_depth": "preliminary_document",
                        "verification_status": "preliminary",
                        "preview_only": True,
                        "file_review_items": [{
                            "type": "preliminary",
                            "text": "初步摘要已结合已解析内容和支撑原文；后续深度摘要将继续校验全文。",
                            "status": "review",
                        }],
                    })
                summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
                storage.save_summary(
                    scan_id,
                    path,
                    staged_summary_type("preliminary", "file")
                    if workflow_source == "preliminary_model_summary" else "file",
                    summary,
                )
                saved.append(path)
            return {"scan_id": scan_id, "paths": saved, "degraded_paths": degraded_paths,
                    "analysis_level": "preview", "workflow_source": "candidate_preview_model_summary"}
        node_path = str(payload.get("path") or "").strip()
        if not node_path:
            raise ValueError("候选文件预览缺少文件路径")
        storage.update_job(job_id, progress=5, stage="candidate_preview", message="正在生成候选文件预览摘要", heartbeat=True)
        _ensure_job_active(job_id)
        document = storage.get_document(scan_id, node_path)
        if not document:
            raise ValueError("候选文件尚未完成统一解析：{}".format(node_path))
        try:
            deadline = float(payload.get("candidate_deadline_at") or 0)
            if deadline and time.time() >= deadline:
                raise TimeoutError("candidate preview batch deadline reached")
            require_local_model_enabled()
            summary, result = analyze_document_preview(
                llm, node_path, node_path, unified_document=document,
                max_chars=int(getattr(Config, "CANDIDATE_PREVIEW_MAX_CHARS", 24000)),
                context_window_tokens=Config.LLM_CONTEXT_TOKENS,
                timeout_seconds=int(getattr(Config, "CANDIDATE_PREVIEW_TIMEOUT_SECONDS", 60)),
            )
            degraded = False
        except Exception as exc:
            summary = _local_document_fallback(document, node_path, str(exc)[:300])
            summary.update({
                "schema_version": 4,
                "summary_type": "file",
                "node_path": node_path,
                "analysis_level": "preview",
                "analysis_depth": "preview_document",
                "verification_status": "candidate",
                "preview_only": True,
                "claim_contract": "file-claims/preview-1.0",
                "file_conclusions": [],
                "file_arguments": [],
                "file_review_items": [{"type": "preview", "text": "快速模型预览未完成，当前为本地解析降级结果。", "status": "review"}],
                "generated_by": "local-preview-fallback",
                "deep_analysis": False,
            })
            result = {"model": None, "usage": {}}
            degraded = True
        summary["node_path"] = node_path
        summary["summary_type"] = "file"
        if str(payload.get("workflow_source") or "") == "preliminary_model_summary":
            summary.update({
                "generated_by": "model-preliminary-analysis" if not degraded
                    else "local-preliminary-fallback",
                "analysis_stage": "preliminary",
                "analysis_depth": "preliminary_document",
                "analysis_level": "preview",
                "verification_status": "preliminary",
                "preview_only": True,
                "deep_analysis": False,
                "file_review_items": [{
                    "type": "preliminary",
                    "text": "初步摘要已结合已解析内容和支撑原文；后续深度摘要将继续校验全文。",
                    "status": "review",
                }],
            })
        summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
        if str(payload.get("workflow_source") or "") == "preliminary_model_summary":
            storage.save_summary(scan_id, node_path, staged_summary_type("preliminary", "file"), summary)
        else:
            storage.save_summary(scan_id, node_path, "file", summary)
        _ensure_job_active(job_id)
        return {
            "scan_id": scan_id,
            "summary": summary,
            "cached": False,
            "degraded": degraded,
            "analysis_level": "preview",
            "path": node_path,
            "workflow_source": str(
                payload.get("workflow_source") or "candidate_preview_model_summary"
            ),
        }
    # Manual selections and their initial model summaries are foreground work.
    # Only the idle deep-summary lane may yield to another queued model task.
    if str(payload.get("workflow_source") or "") in {"idle_deep_model_summary", "deep_node_rebuild"} and storage.has_queued_job_above_priority(10):
        return {"_defer_slice": True, "_defer_seconds": 1, "_defer_message": "检测到前台模型请求，后台深度摘要让路并保存检查点。", "scan_id": scan_id}
    if str(payload.get("workflow_source") or "") in {
        "candidate_node_summary", "preliminary_node_summary", "deep_node_rebuild"
    }:
        node_id = str(payload.get("node_id") or "").strip()
        plan = dict(payload.get("selection_plan") or {})
        if not plan:
            task = storage.get_import_task(scan_id) or {}
            plan = dict((task.get("checkpoint") or {}).get("selection_plan") or {})
        try:
            node = _find_analysis_node(scan_id, node_id)
        except ValueError:
            # Candidate IDs are content-derived and can change when the parser
            # refreshes file topics. Keep queued work executable by carrying
            # concrete member paths in the job payload.
            member_paths = [str(path) for path in (payload.get("member_paths") or []) if str(path)]
            if not member_paths:
                raise
            node = {
                "node_id": node_id,
                "kind": "group",
                "name": payload.get("node_name") or "候选主题",
                "member_paths": sorted(set(member_paths)),
                "file_count": len(set(member_paths)),
            }
        summary_stage = "deep" if str(payload.get("workflow_source") or "") == "deep_node_rebuild" else (
            "preliminary" if str(payload.get("workflow_source") or "") == "preliminary_node_summary" else None
        )
        context = _virtual_node_context(scan_id, node, summary_stage=summary_stage)
        declared_count = int(
            node.get("file_count")
            or plan.get("selected_file_count")
            or context.get("category_file_count")
            or len(context.get("member_paths") or [])
        )
        if not context.get("file_summaries_complete"):
            raise ValueError("候选节点仍有文件摘要未完成，不能生成节点摘要")
        storage.update_job(job_id, progress=10, stage="candidate_node_summary", message="正在汇总 {} 个文件摘要生成节点摘要".format(context.get("file_summary_count") or 0), heartbeat=True)
        workflow_source = str(payload.get("workflow_source") or "")
        is_deep_node = workflow_source == "deep_node_rebuild"
        is_preliminary_node = workflow_source == "preliminary_node_summary"
        context["analysis_level"] = "deep" if is_deep_node else "preview"
        generated, result, errors = analyze_folder(llm, context, node.get("name") or "node:{}".format(node_id))
        generated.update({
            "node_path": "node:{}".format(node_id), "summary_type": "folder", "schema_version": 4,
            # Evidence is scoped to the model subset, while the displayed
            # category count remains the full selected category count.
            "member_paths": context.get("member_paths") or [], "file_count": declared_count,
            "member_paths_lazy": bool(node.get("member_paths_lazy") or plan.get("schema_version")),
            "total_size": int(
                node.get("total_bytes")
                or node.get("total_size")
                or plan.get("selected_bytes")
                or 0
            ),
            "selection_plan": plan,
            "generated_by": (
                "model-deep-analysis" if is_deep_node and result.get("model")
                else (
                    "model-preliminary-node-summary" if is_preliminary_node and result.get("model")
                    else (
                        "model-preview-node-summary" if result.get("model")
                        else "local-fallback"
                    )
                )
            ),
            "workflow_source": workflow_source or "candidate_node_summary",
            "analysis_stage": "preliminary" if is_preliminary_node else (
                "deep" if is_deep_node else "candidate"
            ),
            "analysis_level": "deep" if is_deep_node else "preview",
            "analysis_depth": (
                "deep_folder" if is_deep_node
                else ("preliminary_node" if is_preliminary_node else "preview_folder")
            ),
            "deep_analysis": bool(is_deep_node and context.get("deep_file_summaries_complete")),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "parser_info": {"local_model": result.get("model"), "usage": result.get("usage") or {},
                            "file_summary_count": context.get("file_summary_count") or 0,
                            "file_summaries_complete": bool(context.get("file_summaries_complete")),
                            "batch_errors": errors, "degraded": bool(errors or not result.get("model"))},
        })
        generated["coverage"] = {
            **dict(generated.get("coverage") or {}),
            "category_file_count": declared_count,
            "selected_scope_files": int(plan.get("selected_file_count") or declared_count),
            "analyzed_scope_files": len(context.get("member_paths") or []),
            "deep_analyzed_files": sum(
                1 for item in (context.get("file_summaries") or [])
                if item.get("deep_ready")
            ),
            "members_materialized": False if (node.get("member_paths_lazy") or plan.get("schema_version")) else True,
            "scope_limited": bool(node.get("member_paths_lazy") or plan.get("schema_version")),
            "claim_scope": "模型结论仅适用于选定范围内已进入模型的文件；类别计数覆盖完整类别清单。",
            "verification_status": (
                "verified" if is_deep_node and not errors and result.get("model")
                else "partial"
            ),
        }
        if is_preliminary_node:
            storage.save_summary(scan_id, generated["node_path"], staged_summary_type("preliminary", "node"), generated)
        elif is_deep_node:
            storage.save_summary(scan_id, generated["node_path"], staged_summary_type("deep", "node"), generated)
        else:
            storage.save_summary(scan_id, generated["node_path"], "folder", generated)
        return {"scan_id": scan_id, "node_id": node_id, "summary": generated,
                "workflow_source": str(payload.get("workflow_source") or "candidate_node_summary"),
                "degraded": bool(errors or not result.get("model"))}
    workflow_source = str(payload.get("workflow_source") or "")
    # The selected-file model pass is foreground work. Only automatic idle
    # deep summaries are cooperative and may be deferred here.
    if workflow_source in {"idle_deep_model_summary", "idle_deep_backfill"} and storage.has_queued_job_above_priority(10):
        return {"_defer_slice": True, "_defer_seconds": 1, "_defer_message": "检测到前台模型请求，空闲深度补析已让路并保存检查点。", "scan_id": scan_id}
    storage.update_job(job_id, progress=5, stage="generating_summary", message="正在生成当前节点深度摘要", heartbeat=True)
    _ensure_job_active(job_id)
    # Reuse the established summary implementation under an isolated request
    # context. The web route has no model call after the async gate below.
    # The route enforces scan ownership.  A Worker has no browser request, so
    # explicitly carry the job's owner token into this internal request rather
    # than letting the task fail as an anonymous caller.
    with _summary_worker_execution_context():
        with app.test_request_context(
            "/api/summary",
            method="POST",
            json=payload,
            headers={"X-SJFX-Token": Config.API_ACCESS_TOKEN} if Config.AUTH_REQUIRED else {},
        ):
            response = summarize()
    status_code = 200
    if isinstance(response, tuple):
        response, status_code = response[0], response[1]
    data = response.get_json(silent=True) if hasattr(response, "get_json") else None
    if status_code >= 400 or not data or not data.get("ok"):
        raise ValueError((data or {}).get("error") or "摘要生成失败")
    _ensure_job_active(job_id)
    # A deep batch item is closed only after the corresponding file summary
    # has been durably written, not merely after source parsing finished.
    deep_batch_id = str(payload.get("deep_batch_id") or "")
    if deep_batch_id and str(payload.get("kind") or "file") == "file":
        deep_path = str(payload.get("path") or "").strip()
        if deep_path:
            storage.update_deep_parse_item(
                deep_batch_id, deep_path, "completed",
                parser_plan={"mode": "accurate", "summary": "completed"},
            )
            storage.sync_import_queues(scan_id)
    if str(payload.get("workflow_source") or "") == "large_selection_model_summary":
        # The node is deliberately queued only after the last model file
        # summary is durable. Its context then consists of file summaries and
        # their evidence, never a fresh read of the whole selected category.
        _queue_large_selection_node_summary(
            scan_id, owner_id=job.get("owner_id") or "legacy", current_job_id=job_id
        )
    return {
        "scan_id": scan_id,
        "summary": data.get("summary"),
        "cached": bool(data.get("cached")),
        "degraded": bool(data.get("degraded")),
        "node_id": payload.get("node_id"),
        "kind": payload.get("kind", "file"),
    }


def _run_claimed_export_job(job):
    """Build a potentially multi-gigabyte handoff archive in the Worker."""
    job_id = job["id"]
    scan_id = job["scan_id"]
    options = job.get("options") or {}
    if options.get("deep_batch_id"):
        batch_paths = storage.deep_parse_batch_paths(options.get("deep_batch_id"))
        if batch_paths:
            options["target_paths"] = batch_paths
    scan_result = require_scan(scan_id)
    storage.update_job(job_id, progress=3, stage="preparing_export", message="正在准备待整编节点和证据", heartbeat=True)
    _ensure_job_active(job_id)
    analysis = storage.get_analysis(scan_id) or {}
    documents = _package_documents(scan_id)
    known_hashes = {
        str(item.get("path") or ""): str((item.get("payload") or {}).get("source", {}).get("sha256") or "")
        for item in documents
        if item.get("path")
    }
    large_export = bool(((analysis.get("policy") or {}).get("large_package") or {}).get("enabled"))
    context = _combined_export_context(scan_id, scan_result, analysis, options)
    selected = Path(scan_result["root"])
    state_by_path = {item.get("node_path"): item for item in storage.list_file_states(scan_id)}
    storage.update_job(
        job_id, progress=8, stage="exporting", message="正在生成去重资料包和统一交接说明（可能需要较长时间）", heartbeat=True,
    )
    last_reported = {"index": 0}

    def export_cancel_check():
        _ensure_job_active(job_id)

    def export_progress(index, total, written_size, total_size):
        report_step = max(1, total // 100)
        if index != total and index - last_reported["index"] < report_step:
            return
        last_reported["index"] = index
        ratio = index / float(max(1, total))
        storage.update_job(
            job_id,
            progress=min(94, 8 + int(ratio * 86)),
            stage="exporting",
            message="正在写入资料包：{}/{} 个文件，{} / {}".format(
                index, total, human_size(written_size), human_size(total_size)
            ),
            heartbeat=True,
        )

    archive = export_node(
        scan_result["root"], selected, context["summary"], Config.OUTPUT_DIR, Config.MAX_EXPORT_BYTES,
        analysis=analysis, documents=documents,
        task_topic=options.get("task_topic"),
        member_paths=context["member_paths"],
        node_name=context["summary"]["title"],
        node_id="combined-{}".format(scan_id),
        selection_metadata=context["selection_metadata"],
        selected_evidence_ids=context["selected_evidence_ids"],
        inventory_metadata=_inventory_by_path(scan_result),
        file_states=state_by_path,
        progress_callback=export_progress,
        cancel_check=export_cancel_check,
        content_deduplication=not large_export,
        known_hashes=known_hashes,
        disk_reserve_bytes=Config.EXPORT_DISK_RESERVE_BYTES,
    )
    _ensure_job_active(job_id)
    storage.save_artifact(archive.name, job.get("owner_id"), scan_id=scan_id, job_id=job_id, kind="handoff_export")
    volume_downloads = []
    volume_sidecar = archive.with_name(archive.name + ".parts.json")
    if volume_sidecar.exists():
        try:
            volume_manifest = json.loads(volume_sidecar.read_text(encoding="utf-8"))
            for item in volume_manifest.get("parts") or []:
                filename = str(item.get("file_name") or "")
                candidate = Config.OUTPUT_DIR / filename
                if not filename or candidate.parent.resolve() != Config.OUTPUT_DIR.resolve() or not candidate.is_file():
                    continue
                storage.save_artifact(filename, job.get("owner_id"), scan_id=scan_id, job_id=job_id, kind="handoff_export_volume")
                volume_downloads.append({
                    **item, "download_url": "/outputs/{}".format(filename),
                })
        except (OSError, ValueError, TypeError):
            logger.warning("读取分卷导出清单失败：%s", volume_sidecar, exc_info=True)
    return {
        "scan_id": scan_id,
        "file_name": archive.name,
        "download_url": "/outputs/{}".format(archive.name),
        "source_file_count": len(context["member_paths"]),
        "selection_count": len(context["selection_metadata"]),
        "segmented": bool(volume_downloads),
        "volumes": volume_downloads,
    }


def _run_claimed_analysis_job(job):
    """Execute an already-claimed package-analysis job in the local Worker."""
    job_id = job["id"]
    scan_id = job["scan_id"]
    options = job.get("options") or {}
    scan_result = require_scan(scan_id)
    storage.ensure_package_processing_control(scan_id)
    preprocess_stage = bool(options.get("preprocess_only"))
    idle_background = bool(options.get("idle_background"))
    pause_exempt = str(options.get("workflow_source") or "") == "question_promotion"
    if storage.package_processing_paused(scan_id) and not pause_exempt:
        storage.cancel_job(job_id)
        raise JobCancelled()
    scope_label = options.get("scope_label")
    progress_start = job.get("_progress_start")
    progress_end = job.get("_progress_end", 95)

    def mapped_progress(percent):
        value = max(0, min(95, int(percent)))
        if progress_start is None:
            return value
        start = max(0, min(95, int(progress_start)))
        end = max(start, min(95, int(progress_end)))
        return start + int((end - start) * value / 95.0)

    storage.update_job(
        job_id, progress=max(mapped_progress(1), int(job.get("progress") or 0)), stage="preprocessing" if preprocess_stage else "analyzing", heartbeat=True,
        message=("开始补充分析：{}".format(scope_label) if scope_label else "开始本地完整分析"),
        current_stage="全量轻度解析" if preprocess_stage else "分析准备",
        current_file=scope_label or "",
    )
    try:
        _publish_analysis_progress(
            scan_id, scan_result, mapped_progress(1),
            ("GPU 空闲时自动深度补析：{}".format(scope_label) if idle_background else ("开始补充分析：{}".format(scope_label) if scope_label else "目录清点完成，开始内容解析")),
            stage="preprocessing" if preprocess_stage else "analysis_preparing",
        )
    except Exception:
        logger.warning("发布渐进分析概览失败 scan_id=%s", scan_id, exc_info=True)

    progress_state = {"bucket": -1}

    def progress(percent, message):
        _ensure_job_active(job_id)
        visible_percent = mapped_progress(percent)
        storage.update_job(
            job_id, progress=visible_percent, stage="preprocessing" if preprocess_stage else "analyzing", message=message, heartbeat=True,
            current_stage="轻度解析" if preprocess_stage else "解析与分析",
            current_file=str(message or "")[-500:],
        )
        bucket = max(0, int(visible_percent) // 5)
        if bucket != progress_state["bucket"]:
            progress_state["bucket"] = bucket
            try:
                _publish_analysis_progress(
                    scan_id, scan_result, visible_percent, message,
                    stage="parsing" if int(percent or 0) < 72 else "semantic_analysis",
                )
            except Exception:
                logger.warning("刷新渐进分析概览失败 scan_id=%s", scan_id, exc_info=True)

    translation_pipeline = (
        _ImportTranslationPipeline(
            scan_id, job_id, progress=progress,
            cancel_check=lambda: storage.is_job_cancel_requested(job_id),
        )
        if (
            Config.ENABLE_TRANSLATION
            and Config.ENABLE_IMPORT_TRANSLATION
            and Config.TRANSLATION_PIPELINE_ENABLED
        )
        else None
    )
    deep_batch_id = str(options.get("deep_batch_id") or "")
    workflow_source = str(options.get("workflow_source") or "")
    selected_file_parse = workflow_source == "selected_file_parse"
    if not preprocess_stage and not selected_file_parse:
        requested_paths = [str(path) for path in (options.get("target_paths") or []) if str(path)]
        if not deep_batch_id and requested_paths:
            deep_batch_id = storage.create_deep_parse_batch(
                scan_id, requested_paths, created_by="worker",
                selection_rule={"kind": str(options.get("workflow_source") or "worker")},
                priority=int(job.get("priority") or 50),
            )
            options["deep_batch_id"] = deep_batch_id
        if deep_batch_id:
            storage.update_deep_parse_batch(deep_batch_id, "running")
            options["target_paths"] = storage.deep_parse_batch_paths(deep_batch_id)
            for path in options.get("target_paths") or []:
                storage.update_deep_parse_item(
                    deep_batch_id, path, "running",
                    parser_plan={"mode": options.get("parse_mode") or "accurate"},
                )
    if selected_file_parse:
        storage.transition_import_task(scan_id, "parsing_selected", reason="正在解析选中的文件。")
    else:
        storage.update_import_task(scan_id, status="previewing" if preprocess_stage else "deep_parsing", phase="preprocessing" if preprocess_stage else "deep_parsing")
    # Candidate import and deep import share the parser checkpoint, but their
    # model products are deliberately different. Candidate files receive one
    # bounded preview call; deep files receive the full source-backed summary.
    workflow_source = str(options.get("workflow_source") or "")
    candidate_preview = workflow_source == "candidate_preview"
    large_selection = workflow_source == "large_selection"
    selected_deep_paths = [] if candidate_preview else [str(path) for path in (options.get("target_paths") or []) if str(path)]
    candidate_preview_paths = [str(path) for path in (options.get("target_paths") or []) if str(path)] if candidate_preview else []
    model_summary_paths = [
        str(path) for path in (options.get("model_summary_paths") or [])
        if str(path) in set(selected_deep_paths)
    ] if large_selection else []
    deep_summary_job_ids = []
    candidate_preview_job_ids = []
    analysis = analyze_package(
        scan_id, scan_result, storage, parser, progress,
        embedding_client=_package_embedding_client,
        llm=(llm if llm_generation_enabled else None),
        large_options=_package_large_options(),
        target_paths=options.get("target_paths"),
        cancel_check=lambda: storage.is_job_cancel_requested(job_id),
        parse_mode_override=options.get("parse_mode"),
        workflow_source=options.get("workflow_source"),
        analysis_translation=(
            lambda **kwargs: _prepare_import_translations(
                scan_id, job_id=job_id, **kwargs
            )
        ) if (
            Config.ENABLE_TRANSLATION
            and Config.ENABLE_IMPORT_TRANSLATION
            and translation_pipeline is None
        ) else None,
        translation_pipeline=translation_pipeline,
        yield_check=lambda: storage.has_queued_job_above_priority(
            int(job.get("priority") or 80)
        ),
        aggregation_depth=int(options.get("continuation_depth") or 0),
        aggregation_interval=3,
        preprocess_only=bool(options.get("preprocess_only")),
        selection_plan=options.get("selection_plan") if large_selection else None,
    )
    if selected_file_parse and not analysis.get("_slice_incomplete"):
        storage.transition_import_task(scan_id, "parsed_overview", reason="选中文件已解析，正在生成解析版智能目录和情报概览。")
        _write_local_overview(scan_id, owner_id=job.get("owner_id"), job_id=job_id, summary_stage="parsed")
    # Large directory preprocessing is followed by a bounded representative
    # model pass. It stops at the selection gate and never queues deep
    # backfill automatically.
    auto_candidate_preview = bool(analysis.get("_await_candidate_preview"))
    if auto_candidate_preview:
        candidate_preview = True
        candidate_preview_paths = [
            str(path) for path in (analysis.get("candidate_paths") or []) if str(path)
        ]
    # Parsing is only the middle of the import workflow. Queue one bounded
    # model job per candidate/deep file and keep the durable task in the
    # matching phase until those jobs are finalized.
    summary_paths = (
        candidate_preview_paths
        or (model_summary_paths if large_selection else selected_deep_paths)
    )
    if candidate_preview:
        summary_source = "candidate_preview_model_summary"
    elif large_selection:
        summary_source = "large_selection_model_summary"
    elif workflow_source in {"manual_selection", "selected_file_parse"}:
        # The first model pass after selection is a bounded preliminary
        # summary. Full deep summaries are scheduled only after the preliminary
        # file and node layers settle.
        summary_source = "preliminary_model_summary"
    elif workflow_source == "idle_deep_backfill":
        summary_source = "idle_deep_model_summary"
    else:
        summary_source = "deep_parse_model_summary"
    summary_analysis_level = (
        "preview"
        if candidate_preview or summary_source == "preliminary_model_summary"
        else "deep"
    )
    if summary_paths and not analysis.get("_slice_incomplete"):
        batch_size = max(1, min(8, int(getattr(Config, "CANDIDATE_PREVIEW_BATCH_SIZE", 4)))) if candidate_preview else 1
        for offset in range(0, len(summary_paths), batch_size):
            path_batch = summary_paths[offset:offset + batch_size]
            summary_job_id, _created = storage.create_or_get_typed_job(
                scan_id,
                "generate_summary",
                options={
                    "scan_id": scan_id,
                    "path": path_batch[0] if len(path_batch) == 1 else None,
                    "paths": path_batch if candidate_preview else None,
                    "kind": "file",
                    "force": True,
                    "analysis_level": summary_analysis_level,
                    "workflow_source": summary_source,
                    "candidate_deadline_at": options.get("candidate_deadline_at"),
                    "deep_batch_id": deep_batch_id if not candidate_preview else None,
                    "selection_plan": options.get("selection_plan") if large_selection else None,
                },
                owner_id=job.get("owner_id") or "legacy",
            )
            if candidate_preview:
                candidate_preview_job_ids.append(summary_job_id)
            else:
                deep_summary_job_ids.append(summary_job_id)

    if candidate_preview_job_ids:
        import_task = storage.get_import_task(scan_id) or {}
        checkpoint = dict(import_task.get("checkpoint") or {})
        checkpoint.update({
            "candidate_preview_job_ids": list(candidate_preview_job_ids),
            "candidate_preview_expected": len(candidate_preview_paths),
            "candidate_preview_batches": len(candidate_preview_job_ids),
            "candidate_preview_batch_size": int(getattr(Config, "CANDIDATE_PREVIEW_BATCH_SIZE", 4)),
            "candidate_paths": list(candidate_preview_paths),
            "selection_version": options.get("selection_version"),
        })
        storage.update_import_task(
            scan_id,
            status="candidate_analyzing",
            phase="candidate_analyzing",
            checkpoint=checkpoint,
        )
    elif summary_source == "preliminary_model_summary" and deep_summary_job_ids:
        import_task = storage.get_import_task(scan_id) or {}
        checkpoint = dict(import_task.get("checkpoint") or {})
        prior_ids = [str(item) for item in (checkpoint.get("preliminary_summary_job_ids") or []) if str(item)]
        merged_ids = list(dict.fromkeys([*prior_ids, *deep_summary_job_ids]))
        checkpoint.update({
            "preliminary_summary_job_ids": merged_ids,
            "preliminary_summary_expected": len(merged_ids),
            "preliminary_summary_completed": 0,
            "preliminary_node_job_ids": list(dict.fromkeys(
                str(item) for item in (checkpoint.get("preliminary_node_job_ids") or []) if str(item)
            )),
            "selected_paths": list(dict.fromkeys([
                *(checkpoint.get("selected_paths") or []), *selected_deep_paths
            ])),
            "selection_version": options.get("selection_version") or checkpoint.get("selection_version"),
        })
        storage.transition_import_task(
            scan_id, "preliminary_summarizing", checkpoint=checkpoint,
            reason="选中文件已解析，正在生成文件初步摘要。",
        )
    elif deep_summary_job_ids:
        import_task = storage.get_import_task(scan_id) or {}
        checkpoint = dict(import_task.get("checkpoint") or {})
        prior_file_job_ids = [str(item) for item in (checkpoint.get("deep_summary_job_ids") or []) if str(item)]
        merged_file_job_ids = list(dict.fromkeys([*prior_file_job_ids, *deep_summary_job_ids]))
        prior_node_job_ids = [str(item) for item in (checkpoint.get("deep_summary_node_job_ids") or []) if str(item)]
        checkpoint.update({
            "deep_summary_job_ids": merged_file_job_ids,
            "deep_summary_file_expected": len(merged_file_job_ids),
            "deep_summary_node_job_ids": prior_node_job_ids,
            "deep_summary_expected": len(merged_file_job_ids) + len(prior_node_job_ids),
            "deep_batch_id": deep_batch_id or checkpoint.get("deep_batch_id"),
            "selected_paths": list(dict.fromkeys([*(checkpoint.get("selected_paths") or []), *selected_deep_paths])),
            "model_summary_paths": list(dict.fromkeys([*(checkpoint.get("model_summary_paths") or []), *model_summary_paths])),
            "large_selection": large_selection or bool(checkpoint.get("large_selection")),
            "selection_plan": options.get("selection_plan") if large_selection else checkpoint.get("selection_plan"),
            "selection_version": options.get("selection_version") or checkpoint.get("selection_version"),
        })
        storage.update_import_task(
            scan_id,
            status="summarizing_files",
            phase="summarizing_files",
            checkpoint=checkpoint,
        )

    try:
        storage.sync_import_queues(scan_id)
        storage.refresh_import_task_counts(scan_id)
        if deep_batch_id:
            storage.update_deep_parse_batch(deep_batch_id, "running")
            storage.sync_import_queues(scan_id)
            counts = storage.deep_parse_batch_counts(deep_batch_id)
            if not counts or counts.get("queued", 0) + counts.get("running", 0) + counts.get("retryable", 0) == 0:
                storage.update_deep_parse_batch(
                    deep_batch_id,
                    "failed" if counts.get("failed", 0) else "completed",
                )
    except Exception:
        logger.warning("同步导入队列状态失败 scan_id=%s", scan_id, exc_info=True)
    if analysis.get("_slice_incomplete"):
        return {
            "_requeue_slice": True,
            "_requeue_message": "轻量预览与哈希检查点已保存，等待下一轮继续。",
            "scan_id": scan_id,
        }
    if summary_source == "preliminary_model_summary" and deep_summary_job_ids:
        storage.set_package_processing_state(
            scan_id, "preliminary_summarizing",
            "选中文件已完成解析，正在生成文件初步摘要。",
        )
        storage.update_analysis_progress_status(
            scan_id, "preliminary_summarizing",
            "正在生成文件初步摘要，完成后生成节点初步摘要。",
            "preliminary_summarizing",
        )
        return {
            "scan_id": scan_id,
            "preliminary_summary_job_ids": deep_summary_job_ids,
            "selected_paths": selected_deep_paths,
        }
    if candidate_preview:
        # Candidate parsing is complete; preview summary jobs will move the
        # task to waiting_for_deep_selection after the Worker finalizes them.
        if candidate_preview_job_ids:
            storage.set_package_processing_state(scan_id, "candidate_analyzing", "候选文件已解析，正在生成初步摘要和目录。")
            storage.update_analysis_progress_status(scan_id, "candidate_analyzing", "候选文件已解析，正在生成初步摘要和目录。", "candidate_analyzing")
        else:
            storage.update_import_task(scan_id, status="waiting_for_deep_selection", phase="waiting_for_deep_selection")
        return {"scan_id": scan_id, "candidate_preview_job_ids": candidate_preview_job_ids, "candidate_paths": candidate_preview_paths}
    if analysis.get("_await_selection"):
        storage.refresh_import_task_counts(scan_id)
        storage.update_import_task(scan_id, status="waiting_for_selection", phase="waiting_for_selection")
        storage.set_package_processing_state(scan_id, "awaiting_selection", "文件已完成预处理，等待用户确认分析范围。")
        storage.update_analysis_progress_status(
            scan_id, "awaiting_selection", "文件已扫描并完成预处理，等待用户确认分析范围。", "awaiting_selection",
        )
        storage.update_job(
            job_id, progress=95, stage="awaiting_selection",
            message="文件已扫描并完成预处理，等待用户确认分析范围。",
            current_stage="等待确认分析范围", current_file="",
        )
        return {"scan_id": scan_id, "_await_selection": True}
    _ensure_job_active(job_id)
    workflow = analysis.get("workflow") or {}
    large_enabled = bool(((analysis.get("policy") or {}).get("large_package") or {}).get("enabled"))
    continue_full = bool(options.get("continue_full", True))
    continuation_job_id = None
    remaining_priority_paths = list(workflow.get("remaining_priority_paths") or [])
    if remaining_priority_paths and not storage.package_processing_paused(scan_id):
        next_options = dict(options)
        next_options.update({
            "target_paths": remaining_priority_paths,
            "workflow_source": options.get("workflow_source") or "initial_overview",
            "scope_label": options.get("scope_label") or "首版概览优先文件",
            "continuation_depth": int(options.get("continuation_depth") or 0) + 1,
        })
        continuation_job_id = storage.create_job(
            scan_id, options=next_options, task_type="analyze_package",
            owner_id=job.get("owner_id") or "legacy",
        )
    background_job_id = None
    if (
        not continuation_job_id
        and continue_full
        and not storage.package_processing_paused(scan_id)
        and workflow.get("background_batch_paths")
    ):
        background_options = {
            "target_paths": list(workflow.get("background_batch_paths") or []),
            "workflow_source": "background_backfill",
            "scope_label": "全量队列下一批（最多500个逻辑文件）",
            "parse_mode": "accurate",
            "continue_full": True,
            "continuation_depth": int(options.get("continuation_depth") or 0) + 1,
        }
        background_job_id, _background_created = storage.create_or_get_typed_job(
            scan_id,
            options=background_options,
            task_type="analyze_package",
            owner_id=job.get("owner_id") or "legacy",
        )
    conversation_continuation = None
    analysis_turn_continuation = None
    if not continuation_job_id:
        if options.get("conversation_turn_id"):
            analysis_turn_continuation = _resume_conversation_turn_after_promotion(job)
        else:
            conversation_continuation = _continue_conversation_after_promotion(
                job, analysis, scan_result,
            )
    overview = None
    report_source = str(options.get("workflow_source") or "initial_overview")
    # A continuation has already persisted its analysis/checkpoints. Generating
    # a model-backed Word report for every 20-50 file slice makes one logical
    # overview needlessly expensive and delays question-triggered promotion.
    # Publish the report after the priority chain, while background/question
    # slices remain visible through progressive analysis and coverage APIs.
    if (
        not continuation_job_id
        and not deep_summary_job_ids
        and report_source not in {
            "background_backfill", "question_promotion", "large_selection",
        }
    ):
        storage.update_job(job_id, progress=96, stage="generating_report", message="自动生成情况概览 Word", heartbeat=True)
        overview = _write_local_overview(scan_id, owner_id=job.get("owner_id"), job_id=job_id)
    processing = _package_processing_status(scan_id, scan_result) if large_enabled else None
    all_eligible_complete = bool(processing and processing.get("all_eligible_complete"))
    if large_enabled and not continuation_job_id and not background_job_id:
        if all_eligible_complete:
            if overview is None and report_source == "background_backfill":
                storage.update_job(
                    job_id, progress=96, stage="final_calibration",
                    message="全库关系校准完成，正在生成最终概览 Word", heartbeat=True,
                )
                overview = _write_local_overview(
                    scan_id, owner_id=job.get("owner_id"), job_id=job_id
                )
            storage.set_package_processing_state(
                scan_id, "completed", "全部有效逻辑文件已完成并完成全局校准。"
            )
        elif report_source == "large_selection":
            storage.set_package_processing_state(
                scan_id, "paused",
                "选中范围已完成正常解析，等待模型摘要完成后点击更新深度结果。",
            )
        elif not continue_full:
            storage.set_package_processing_state(
                scan_id, "paused", "本次优先范围已完成；普通待处理文件仍保留在全量队列。"
            )
        elif report_source in {
            "initial_overview", "background_backfill", "user_query",
            "user_intent", "relationship_recall", "manual_queue",
        }:
            storage.set_package_processing_state(
                scan_id, "paused", "当前没有可立即领取的文件；失败或受限文件已单独保留。"
            )
    _ensure_job_active(job_id)
    translation_job_ids = []
    # Selected foreign documents already receive a durable working translation
    # inside their deep-analysis batch. Only optional package-wide backfill is
    # queued here; do not schedule every processed file a second time.
    if (
        Config.ENABLE_TRANSLATION and Config.AUTO_TRANSLATE_PACKAGES
        and not options.get("target_paths")
    ):
        translation_job_id, _translation_created = storage.create_or_get_typed_job(
            scan_id, "translate_package",
            options={
                "phase": "preview_and_priority", "cursor": 0,
                "workflow_source": "translation_backfill",
            },
            owner_id=job.get("owner_id") or "legacy",
        )
        translation_job_ids.append(translation_job_id)
    # A scan may span several durable analysis slices. The final slice must
    # close the progressive card; otherwise the browser keeps polling an old
    # 95% ``running`` payload after the queue job itself has completed.
    if not continuation_job_id and not background_job_id:
        final_processing = (
            _package_processing_status(scan_id, scan_result)
            if large_enabled else None
        )
        final_status = "paused" if (
            final_processing and final_processing.get("state") == "paused"
        ) else "completed"
        final_message = (
            final_processing.get("message")
            if final_status == "paused" and final_processing else
            "数据分析、结构化摘要和报告已完成"
        )
        final_progress = storage.update_analysis_progress_status(
            scan_id, final_status, final_message, final_status,
        )
        if final_status == "completed":
            final_progress["progress"] = 100
            final_progress["stage"] = "completed"
            storage.save_analysis_progress(scan_id, final_progress)
    if (
        not continuation_job_id
        and not background_job_id
        and not analysis.get("_await_selection")
        and not deep_summary_job_ids
    ):
        storage.refresh_import_task_counts(scan_id)
        storage.update_import_task(scan_id, status="completed", phase="completed")
    return {
        "scan_id": scan_id,
        "analysis": analysis.get("statistics", {}),
        "classification_dimensions": analysis.get("classification_dimensions", []),
        "overview": overview,
        "translation_job_id": translation_job_ids[0] if translation_job_ids else None,
        "translation_job_ids": translation_job_ids,
        "conversation_continuation": conversation_continuation,
        "analysis_turn_continuation": analysis_turn_continuation,
        "continuation_job_id": continuation_job_id,
        "background_job_id": background_job_id,
        "deep_summary_job_ids": deep_summary_job_ids,
        "processing": _package_processing_status(scan_id, scan_result) if large_enabled else None,
    }


def _translation_candidate_paths(scan_id, phase):
    """Return a deterministic, metadata-only translation work list."""
    analysis = storage.get_analysis(scan_id) or {}
    large = bool(((analysis.get("policy") or {}).get("large_package") or {}).get("enabled"))
    if large:
        candidates = []
        for item in storage.iter_file_previews(scan_id):
            preview = item.get("payload") or {}
            if str(preview.get("status") or "") != "previewed":
                continue
            language = str((preview.get("language") or {}).get("code") or "unknown")
            if language == "zh":
                continue
            path = item.get("path")
            if phase == "deep_backfill":
                translation = storage.get_translation(scan_id, path, hydrate=False) or {}
                if translation.get("full_translation"):
                    continue
                if translation.get("source_level") == "full" and translation.get("status") == "failed":
                    # Older builds treated harmless prose line-wrap changes as
                    # hard failures. Retry those stale records once under the
                    # relaxed contract, while keeping unavailable models and
                    # real table/token failures out of an infinite loop.
                    error_codes = {
                        str(error.get("code") or error)
                        if isinstance(error, dict) else str(error)
                        for error in (translation.get("errors") or [])
                    }
                    if error_codes != {"line_structure_changed"}:
                        continue
            candidates.append(path)
        representatives = set((storage.get_content_map(scan_id) or {}).get("representative_paths") or [])
        return sorted(set(candidates), key=lambda path: (path not in representatives, path)), large
    return sorted(item.get("path") for item in storage.iter_documents(scan_id, hydrate=False)), large


def _translation_inventory_node(scan_result, node_path):
    """Validate a translation target against the immutable logical inventory.

    Archive members and structured-file partitions use virtual paths such as
    ``archive.zip::letters/a.txt``.  They are individually inventoried but do
    not exist as filesystem paths, so physical-tree lookup is insufficient.
    The enclosing container is still resolved under the scan root before any
    bytes are read.
    """
    node_path = str(node_path or "").strip()
    file_node = _inventory_by_path(scan_result).get(node_path)
    if not file_node:
        raise ValueError("文件不在当前数据包清单中")
    source_path = (
        file_node.get("container_path")
        if file_node.get("logical_unit") else node_path
    )
    resolve_under(scan_result["root"], source_path)
    return file_node


def _translation_document(scan_id, node_path, source_level):
    if source_level == "full":
        return storage.get_document(scan_id, node_path)
    preview = storage.get_file_preview(scan_id, node_path)
    if not preview:
        document = storage.get_document(scan_id, node_path)
        return storage.project_document(
            document,
            text_limit=Config.LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE,
            evidence_limit=Config.LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE,
        ) if document else None
    document = preview_as_document(preview)
    document["text"] = str(document.get("text") or "")[:Config.LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE]
    document.setdefault("coverage", {})["translation_source_level"] = "preview"
    return document


def _apply_import_translation_overlay(document, state, source_char_limit=None):
    """Attach a transient Chinese analysis view while preserving source text."""
    if not isinstance(document, dict) or not isinstance(state, dict):
        return False
    body_units = [
        unit for unit in state.get("units") or []
        if isinstance(unit, dict)
        and unit.get("kind") == "body"
        and (
            source_char_limit is None
            or int(unit.get("start") or 0) < int(source_char_limit)
        )
    ]
    completed = [
        unit for unit in body_units
        if unit.get("status") in {"completed", "not_required"}
        and unit.get("target_text") is not None
    ]
    if not completed:
        return False
    working_text = "".join(
        str(
            unit.get("target_text")
            if unit.get("target_text") is not None
            else unit.get("source_text") or ""
        )
        for unit in body_units
    )
    if not working_text.strip():
        return False
    document["analysis_text"] = working_text
    document["analysis_title"] = str(
        state.get("translated_title") or state.get("working_title") or ""
    )
    progress = state.get("progress") or {}
    document["analysis_translation"] = {
        "source_language": state.get("source_language"),
        "target_language": state.get("target_language") or "zh-CN",
        "status": state.get("status"),
        "completed_units": int(progress.get("completed_units") or 0),
        "required_units": int(progress.get("required_units") or 0),
        "coverage_ratio": float(progress.get("ratio") or 0.0),
        "source_level": state.get("source_level"),
        "original_preserved": True,
    }

    original = str(document.get("text") or "")
    search_cursor = 0
    for evidence in document.get("evidence") or []:
        if not isinstance(evidence, dict) or not str(evidence.get("text") or "").strip():
            continue
        start = evidence.get("char_start")
        end = evidence.get("char_end")
        try:
            start = int(start)
            end = int(end)
        except (TypeError, ValueError):
            needle = str(evidence.get("text") or "")
            start = original.find(needle, search_cursor)
            if start < 0:
                start = original.find(needle)
            end = start + len(needle) if start >= 0 else -1
            if start >= 0:
                search_cursor = end
        if start < 0 or end <= start:
            continue
        overlapping = [
            unit for unit in completed
            if int(unit.get("end") or 0) > start and int(unit.get("start") or 0) < end
        ]
        if not overlapping:
            continue
        translated = "\n".join(
            str(unit.get("target_text") or "").strip()
            for unit in overlapping if str(unit.get("target_text") or "").strip()
        ).strip()
        if not translated:
            continue
        evidence.update({
            "translated_text": translated,
            "source_language": state.get("source_language"),
            "target_language": state.get("target_language") or "zh-CN",
            "translation_source": "import_working_translation",
            "translation_unit_ids": [unit.get("unit_id") for unit in overlapping],
        })
    return True


def _translation_detection_sample(text, limit=24000):
    """Sample head, middle and tail so foreign appendices are not skipped."""
    value = str(text or "")
    if len(value) <= limit:
        return value
    window = max(2000, limit // 3)
    middle = len(value) // 2
    return value[:window] + "\n" + value[middle - window // 2:middle + window // 2] + "\n" + value[-window:]


class _ImportTranslationPipeline:
    """One bounded NLLB consumer that overlaps durable parse and translation.

    Parsing remains the source of truth: a parsed document is saved first, then
    the pipeline creates an independent, transient Chinese working view for
    analysis.  This means a translation failure or a Worker restart can never
    overwrite the original document, and the persisted per-unit checkpoints
    allow the next run to continue instead of retranslating completed units.
    """

    parse_max_concurrency = Config.TRANSLATION_PIPELINE_PARSE_MAX_CONCURRENCY

    def __init__(self, scan_id, job_id, progress=None, cancel_check=None):
        self.scan_id = scan_id
        self.job_id = job_id
        self.progress = progress or (lambda _percent, _message: None)
        self.cancel_check = cancel_check
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sjfx-import-translation"
        )
        self._lock = threading.Lock()
        self._futures = []
        self._started = False
        self._closed = False
        self._aborted = False
        self._file_limit = 0
        self._per_file_limit = 0
        self._total_limit = 0
        self._reserved_characters = 0
        self._reserved_files = 0
        self._summary = {
            "enabled": True,
            "mode": "fast_parse_translation_pipeline",
            "scope": "current_analysis_scope",
            "target_language": "zh-CN",
            "eligible_files": 0,
            "translated_files": 0,
            "partial_files": 0,
            "failed_files": 0,
            "skipped_files": 0,
            "restricted_files": 0,
            "reused_files": 0,
            "translated_characters": 0,
            "source_characters": 0,
            "provider_attempts": 0,
            "limitations": [],
            "original_evidence_preserved": True,
        }

    def start(self, policy, priority_paths=None):
        """Set one immutable budget before parser completions are submitted."""
        del priority_paths  # The parse queue is already priority ordered.
        if self._started:
            return
        large = bool((policy or {}).get("enabled"))
        self._file_limit = (
            max(1, int(Config.IMPORT_TRANSLATION_LARGE_MAX_FILES))
            if large else max(1, int(Config.IMPORT_TRANSLATION_MAX_FILES))
        )
        self._per_file_limit = int(Config.IMPORT_TRANSLATION_MAX_CHARS_PER_FILE)
        self._total_limit = max(
            int(Config.IMPORT_TRANSLATION_MAX_TOTAL_CHARS),
            self._file_limit * self._per_file_limit if large else 0,
        )
        self._summary.update({
            "scope": "current_deep_batch" if large else "current_analysis_scope",
            "file_limit": self._file_limit,
            "per_file_character_limit": self._per_file_limit,
            "total_character_limit": self._total_limit,
        })
        self._started = True

    def submit(self, path, source_document, analysis_document):
        """Queue one completed parse without blocking the next parse request."""
        if not self._started or self._closed or self._aborted:
            return False
        source = source_document.get("source") or {}
        if source.get("sensitive") or source.get("content_policy") == "restricted":
            with self._lock:
                self._summary["restricted_files"] += 1
            return False
        source_text = str(source_document.get("text") or "")
        title = str((source_document.get("structure") or {}).get("title") or source.get("name") or "")
        detection = detect_language(title + "\n" + _translation_detection_sample(source_text))
        if not detection.get("needs_translation") or not source_text.strip():
            return False
        with self._lock:
            self._summary["eligible_files"] += 1
            available = min(self._per_file_limit, self._total_limit - self._reserved_characters)
            if self._reserved_files >= self._file_limit or available < 1000:
                self._summary["skipped_files"] += 1
                return False
            source_chars = min(len(source_text), available)
            self._reserved_files += 1
            self._reserved_characters += source_chars
            self._summary["source_characters"] += source_chars
            future = self._executor.submit(
                self._translate_one, str(path), source_document, analysis_document,
                source_chars,
            )
            self._futures.append(future)
        return True

    def _translate_one(self, path, source_document, analysis_document, source_chars):
        if self._aborted or (self.cancel_check is not None and self.cancel_check()):
            return
        projection = storage.project_document(
            source_document, text_limit=source_chars,
            evidence_limit=Config.LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE,
        )
        previous = storage.get_translation(self.scan_id, path, hydrate=True)
        if previous and previous.get("full_translation") and previous.get("status") == "completed":
            state = previous
            reused = True
        else:
            reused = False
            source_level = (
                "analysis" if len(str(source_document.get("text") or "")) > source_chars
                else "full"
            )

            def checkpoint(state):
                state["source_level"] = source_level
                state["analysis_working_translation"] = True
                state["source_fingerprint"] = document_translation_fingerprint(source_document)
                state["full_translation"] = bool(
                    source_level == "full"
                    and state.get("status") in {"completed", "not_required"}
                )
                storage.save_translation(self.scan_id, path, state)

            state = _translate_document_serialized(
                projection,
                resume_state=previous,
                max_units=Config.IMPORT_TRANSLATION_MAX_UNITS_PER_FILE,
                checkpoint_callback=checkpoint,
                cancel_check=self.cancel_check,
            )
            checkpoint(state)
        translated = _apply_import_translation_overlay(
            analysis_document, state, source_char_limit=source_chars,
        )
        with self._lock:
            if reused:
                self._summary["reused_files"] += 1
            self._summary["provider_attempts"] += int(
                (state.get("performance") or {}).get("provider_attempts") or 0
            )
            if translated:
                self._summary["translated_characters"] += sum(
                    len(str(unit.get("target_text") or ""))
                    for unit in state.get("units") or []
                    if isinstance(unit, dict)
                    and unit.get("kind") == "body"
                    and unit.get("status") in {"completed", "not_required"}
                    and int(unit.get("start") or 0) < source_chars
                )
                if state.get("status") in {"completed", "not_required"}:
                    self._summary["translated_files"] += 1
                else:
                    self._summary["partial_files"] += 1
            else:
                self._summary["failed_files"] += 1

    def finish(self):
        """Wait for the small tail of queued work and return an honest summary."""
        if self._closed:
            with self._lock:
                return copy.deepcopy(self._summary)
        try:
            for future in list(self._futures):
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("导入期翻译任务失败 scan_id=%s", self.scan_id, exc_info=True)
                    with self._lock:
                        self._summary["failed_files"] += 1
                        self._summary["limitations"].append(
                            "一个外语文件未能生成可用工作译本：{}".format(str(exc)[:200])
                        )
        finally:
            self._executor.shutdown(wait=True)
            self._closed = True
        with self._lock:
            if self._summary["skipped_files"]:
                self._summary["limitations"].append(
                    "外语文件超过导入期预算，{} 个文件将在后台或点名分析时继续全文翻译。".format(
                        self._summary["skipped_files"]
                    )
                )
            if self._summary["partial_files"]:
                self._summary["limitations"].append(
                    "{} 个文件已生成中文工作译本；完整译文将由后台续跑。".format(
                        self._summary["partial_files"]
                    )
                )
            if self._summary["failed_files"]:
                self._summary["limitations"].append(
                    "{} 个外语文件未能生成可用工作译本，已安全回退原文分析。".format(
                        self._summary["failed_files"]
                    )
                )
            return copy.deepcopy(self._summary)

    def abort(self):
        """Stop accepting work and wait for the current checkpoint to finish."""
        self._aborted = True
        for future in list(self._futures):
            future.cancel()
        if not self._closed:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._closed = True


def _prepare_import_translations(scan_id, job_id, documents, policy, priority_paths,
                                 progress, cancel_check):
    """Translate a bounded foreign working set before semantic analysis."""
    large = bool((policy or {}).get("enabled"))
    # Import translation is deliberately bounded independently from the large
    # package parse batch. Full coverage continues in the background queue.
    file_limit = (
        max(1, int(Config.IMPORT_TRANSLATION_LARGE_MAX_FILES))
        if large else max(1, int(Config.IMPORT_TRANSLATION_MAX_FILES))
    )
    per_file_limit = Config.IMPORT_TRANSLATION_MAX_CHARS_PER_FILE
    total_limit = max(
        Config.IMPORT_TRANSLATION_MAX_TOTAL_CHARS,
        file_limit * per_file_limit if large else 0,
    )
    priority_paths = set(priority_paths or [])
    candidates = []
    restricted = 0
    for path, document in documents.items():
        source = document.get("source") or {}
        if source.get("sensitive") or source.get("content_policy") == "restricted":
            restricted += 1
            continue
        text = str(document.get("text") or "")
        title = str((document.get("structure") or {}).get("title") or source.get("name") or "")
        detection = detect_language(title + "\n" + _translation_detection_sample(text))
        if not detection.get("needs_translation") or not text.strip():
            continue
        candidates.append((path not in priority_paths, path, document, detection))
    candidates.sort(key=lambda item: (item[0], item[1]))

    result_summary = {
        "enabled": True,
        "mode": "fast_pre_analysis",
        "scope": "current_deep_batch" if large else "current_analysis_scope",
        "target_language": "zh-CN",
        "eligible_files": len(candidates),
        "translated_files": 0,
        "partial_files": 0,
        "failed_files": 0,
        "skipped_files": 0,
        "restricted_files": restricted,
        "reused_files": 0,
        "translated_characters": 0,
        "source_characters": 0,
        "provider_attempts": 0,
        "file_limit": file_limit,
        "per_file_character_limit": per_file_limit,
        "total_character_limit": total_limit,
        "limitations": [],
        "original_evidence_preserved": True,
    }
    consumed = 0
    attempted = 0
    for _priority, path, document, _detection in candidates:
        if attempted >= file_limit or consumed >= total_limit:
            break
        if cancel_check is not None and cancel_check():
            raise RuntimeError("任务已取消，停止导入期翻译")
        available = min(per_file_limit, total_limit - consumed)
        if available < 1000:
            break
        source_text = str(document.get("text") or "")
        source_chars = min(len(source_text), available)
        projection = storage.project_document(
            document, text_limit=source_chars,
            evidence_limit=Config.LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE,
        )
        previous = storage.get_translation(scan_id, path, hydrate=True)
        if previous and previous.get("full_translation") and previous.get("status") == "completed":
            state = previous
            result_summary["reused_files"] += 1
        else:
            source_level = "analysis" if large or len(source_text) > source_chars else "full"

            def checkpoint(state):
                state["source_level"] = source_level
                state["analysis_working_translation"] = True
                # The working copy may be built from a bounded projection, but
                # its lifetime is governed by the complete parsed source.
                # Otherwise the next source-document save would incorrectly
                # invalidate a valid import translation as stale.
                state["source_fingerprint"] = document_translation_fingerprint(document)
                state["full_translation"] = bool(
                    source_level == "full" and state.get("status") in {"completed", "not_required"}
                )
                storage.save_translation(scan_id, path, state)

            state = _translate_document_serialized(
                projection,
                resume_state=previous,
                max_units=Config.IMPORT_TRANSLATION_MAX_UNITS_PER_FILE,
                checkpoint_callback=checkpoint,
                cancel_check=cancel_check,
            )
            checkpoint(state)
        attempted += 1
        consumed += source_chars
        result_summary["source_characters"] += source_chars
        result_summary["provider_attempts"] += int(
            (state.get("performance") or {}).get("provider_attempts") or 0
        )
        if _apply_import_translation_overlay(document, state, source_char_limit=source_chars):
            translated_chars = sum(
                len(str(unit.get("target_text") or ""))
                for unit in state.get("units") or []
                if isinstance(unit, dict)
                and unit.get("kind") == "body"
                and unit.get("status") in {"completed", "not_required"}
                and int(unit.get("start") or 0) < source_chars
            )
            result_summary["translated_characters"] += translated_chars
            if state.get("status") in {"completed", "not_required"}:
                result_summary["translated_files"] += 1
            else:
                result_summary["partial_files"] += 1
        else:
            result_summary["failed_files"] += 1
        progress(
            70 + int(2 * attempted / max(1, min(len(candidates), file_limit))),
            "导入期外语工作译本：{}/{} {}".format(
                attempted, min(len(candidates), file_limit), path
            ),
        )

    result_summary["skipped_files"] = max(0, len(candidates) - attempted)
    if result_summary["skipped_files"]:
        result_summary["limitations"].append(
            "外语文件超过导入期预算，{} 个文件将在后台或点名分析时继续翻译。".format(
                result_summary["skipped_files"]
            )
        )
    if result_summary["partial_files"]:
        result_summary["limitations"].append(
            "{} 个文件仅生成部分中文工作译本，未翻译段落继续以原文参与分析。".format(
                result_summary["partial_files"]
            )
        )
    if result_summary["failed_files"]:
        result_summary["limitations"].append(
            "{} 个外语文件未能生成可用工作译本，已安全回退原文分析。".format(
                result_summary["failed_files"]
            )
        )
    return result_summary


def _translate_one_document(scan_id, node_path, source_level, job_id=None, max_units=None):
    document = _translation_document(scan_id, node_path, source_level)
    if not document:
        raise ValueError("文件尚无可翻译内容：{}".format(node_path))
    previous = storage.get_translation(scan_id, node_path, hydrate=True)
    checkpoint_state = {"last": 0.0}

    def checkpoint(state):
        state["source_level"] = source_level
        state["full_translation"] = bool(
            source_level == "full" and state.get("status") in {"completed", "not_required"}
        )
        storage.save_translation(scan_id, node_path, state)
        if job_id and time.monotonic() - checkpoint_state["last"] >= 1.0:
            checkpoint_state["last"] = time.monotonic()
            progress = state.get("progress") or {}
            storage.update_job(
                job_id,
                message="正在翻译 {}：{}/{} 个翻译单元".format(
                    node_path, progress.get("completed_units", 0), progress.get("required_units", 0)
                ),
                current_stage="文档翻译", current_file=node_path, heartbeat=True,
            )

    result = _translate_document_serialized(
        document,
        resume_state=previous,
        max_units=max_units,
        checkpoint_callback=checkpoint,
        cancel_check=(lambda: storage.is_job_cancel_requested(job_id)) if job_id else None,
    )
    result["source_level"] = source_level
    result["full_translation"] = bool(
        source_level == "full" and result.get("status") in {"completed", "not_required"}
    )
    storage.save_translation(scan_id, node_path, result)
    return result


def _promote_for_translation(scan_id, scan_result, node_path, job_id):
    file_node = _translation_inventory_node(scan_result, node_path)
    _ensure_job_active(job_id)
    with _logical_source_snapshot(
        scan_result["root"], file_node,
        cancel_check=lambda: storage.is_job_cancel_requested(job_id),
    ) as snapshot:
        document = _parse_with_limits(
            parser, snapshot, node_path, "accurate",
            cancel_check=lambda: storage.is_job_cancel_requested(job_id),
        )
    _restore_source_provenance(document, scan_result["root"], file_node)
    storage.save_document(scan_id, node_path, document)
    storage.set_file_state(
        scan_id, node_path,
        checkpoint_fingerprint(file_node, parser, "accurate", document),
        "completed", document=document,
    )
    storage.replace_document_evidence_index(
        scan_id, node_path, evidence_corpus({node_path: document})
    )
    return document


def _run_claimed_homogeneous_analysis_job(job):
    """Build the auditable ledger and bounded relationship graph for one scan."""
    job_id = job["id"]
    scan_id = job["scan_id"]
    require_scan(scan_id)
    storage.update_job(
        job_id, progress=8, stage="detecting_schema",
        message="正在识别邮件头、正文、附件和公共字段",
        current_stage="邮件内容预处理", heartbeat=True,
    )
    _ensure_job_active(job_id)
    total_documents = max(1, int(storage.count_documents(scan_id) or 0))
    checkpoint_dir = Path(Config.DATA_DIR) / "homogeneous_checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_dir / (str(scan_id) + ".jsonl")
    resumed_records = []
    if checkpoint_path.exists():
        try:
            with checkpoint_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if isinstance(record, dict) and record.get("path"):
                        resumed_records.append(record)
        except OSError:
            resumed_records = []
    resumed_paths = {str(item.get("path")) for item in resumed_records}
    resume_after = max(resumed_paths) if resumed_paths else None
    checkpoint_handle = checkpoint_path.open("a", encoding="utf-8")
    scanned_offset = len(resumed_records)

    def homogeneous_progress(scanned, usable):
        # Publish a heartbeat while the deterministic relationship pass is
        # still running.  Without this, a large package looked frozen even
        # though the worker was actively parsing and indexing records.
        _ensure_job_active(job_id)
        overall_scanned = scanned_offset + scanned
        progress = min(78, 8 + int(overall_scanned * 68 / total_documents))
        storage.update_job(
            job_id,
            progress=progress,
            stage="extracting_structured_records",
            message="正在提取邮件信号：已检查 {} 份，可用 {} 份".format(overall_scanned, scanned_offset + usable),
            current_stage="邮件信号提取",
            current_file="",
            heartbeat=True,
        )

    def save_record(record):
        if str(record.get("path") or "") in resumed_paths:
            return
        checkpoint_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        checkpoint_handle.flush()

    def maybe_yield():
        _ensure_job_active(job_id)
        if storage.has_queued_job_above_priority(int(job.get("priority") or 90)):
            raise _HomogeneousYield()

    def on_homogeneous_progress(scanned, usable):
        homogeneous_progress(scanned, usable)
        # Resumed records are already checkpointed and must not cause an
        # endless yield loop while the same foreground task remains queued.
        if scanned > len(resumed_records):
            maybe_yield()

    try:
        result = analyze_homogeneous_documents(
            itertools.chain(
                ({"record": item} for item in resumed_records),
                storage.iter_documents(
                scan_id, hydrate=True, batch_size=100, start_after=resume_after
                ),
            ),
            cancel_check=lambda: _ensure_job_active(job_id),
            progress_callback=on_homogeneous_progress,
            record_callback=save_record,
        )
    except _HomogeneousYield:
        checkpoint_handle.close()
        storage.update_job(
            job_id,
            progress=min(78, 8 + int((scanned_offset / total_documents) * 68)),
            stage="checkpointed",
            message="检测到交互式任务，已保存邮件分析检查点并让出处理资源",
            current_stage="邮件分析检查点",
            heartbeat=True,
        )
        return {
            "_requeue_slice": True,
            "_requeue_message": "邮件分析已保存 {} 份记录，优先处理交互式任务后自动继续。".format(
                scanned_offset
            ),
            "scan_id": scan_id,
            "record_count": scanned_offset,
        }
    finally:
        if not checkpoint_handle.closed:
            checkpoint_handle.close()
    storage.update_job(
        job_id, progress=82, stage="building_relationships",
        message="正在保存文件台账、关系和事项时间线",
        current_stage="关系与事项构建", heartbeat=True,
    )
    _ensure_job_active(job_id)
    counts = storage.save_homogeneous_analysis(scan_id, result)
    try:
        checkpoint_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("无法清理同构分析检查点 scan_id=%s", scan_id, exc_info=True)
    return {
        "scan_id": scan_id,
        "eligible": bool(result.get("eligible")),
        "structural_score": result.get("structural_score"),
        "record_count": counts["records"],
        "relationship_count": counts["relations"],
        "case_count": counts["cases"],
        "anomaly_count": counts["anomalies"],
        "entity_count": int((result.get("metrics") or {}).get("unique_entity_count") or 0),
        "communication_pair_count": int((result.get("metrics") or {}).get("communication_pair_count") or 0),
    }


def _run_claimed_translation_job(job):
    """Run interactive or package-wide local translation with durable slices."""
    job_id = job["id"]
    scan_id = job["scan_id"]
    options = dict(job.get("options") or {})
    scan_result = require_scan(scan_id)
    if job.get("task_type") == "translate_document":
        node_path = str(options.get("path") or "")
        _translation_inventory_node(scan_result, node_path)
        state = (storage.get_file_state(scan_id, node_path) or {}).get("status")
        if options.get("require_full", True) and state != "completed":
            _promote_for_translation(scan_id, scan_result, node_path, job_id)
            state = "completed"
        source_level = "full" if state == "completed" else "preview"
        storage.update_job(
            job_id, progress=5, stage="translating", message="正在准备文档翻译",
            current_stage="文档翻译", current_file=node_path, heartbeat=True,
        )
        result = _translate_one_document(scan_id, node_path, source_level, job_id=job_id)
        return {
            "scan_id": scan_id, "path": node_path, "translation_status": result.get("status"),
            "source_level": result.get("source_level"), "full_translation": result.get("full_translation"),
        }

    phase = str(options.get("phase") or "preview_and_priority")
    cursor = max(0, int(options.get("cursor") or 0))
    slice_no = max(0, int(options.get("slice") or 0))
    selected_only = bool(options.get("selected_only"))
    if selected_only:
        paths = list(dict.fromkeys(
            str(path) for path in (options.get("target_paths") or []) if str(path)
        ))
        large = bool(
            (((storage.get_analysis(scan_id) or {}).get("policy") or {}).get("large_package") or {}).get("enabled")
        )
    else:
        paths, large = _translation_candidate_paths(scan_id, phase)
    batch_size = Config.TRANSLATION_PACKAGE_BATCH_FILES
    current = paths[cursor:cursor + batch_size]
    storage.update_job(
        job_id, progress=2, stage="translating_package",
        message="正在执行数据包翻译阶段 {}：{} 个文件".format(phase, len(current)),
        current_stage="数据包翻译", heartbeat=True,
    )
    completed = 0
    failed = []
    for index, node_path in enumerate(current, 1):
        _ensure_job_active(job_id)
        try:
            state = (storage.get_file_state(scan_id, node_path) or {}).get("status")
            if (phase == "deep_backfill" or (selected_only and options.get("require_full", True))) and state != "completed":
                _promote_for_translation(scan_id, scan_result, node_path, job_id)
                state = "completed"
            source_level = "full" if state == "completed" else "preview"
            result = _translate_one_document(scan_id, node_path, source_level, job_id=job_id)
            if result.get("status") in {"completed", "not_required"}:
                completed += 1
            else:
                failed.append({"path": node_path, "status": result.get("status"), "errors": result.get("errors") or []})
        except Exception as exc:
            failed.append({"path": node_path, "status": "failed", "error": str(exc)[:500]})
        storage.update_job(
            job_id,
            progress=min(94, 4 + int(90 * index / max(1, len(current)))),
            stage="translating_package",
            message="数据包翻译 {}：{}/{} {}".format(phase, index, len(current), node_path),
            current_stage="数据包翻译", current_file=node_path, heartbeat=True,
        )

    next_cursor = cursor + len(current)
    continuation_job_id = None
    if not selected_only and phase == "deep_backfill" and len(current) < len(paths):
        continuation_job_id, _continuation_created = storage.create_or_get_typed_job(
            scan_id, "translate_package",
            # The remaining-candidate set shrinks after every successful
            # promotion, so each continuation starts at its new first item.
            options={"phase": phase, "cursor": 0, "slice": slice_no + 1},
            owner_id=job.get("owner_id") or "legacy",
        )
        next_cursor = 0
    elif not selected_only and phase == "deep_backfill":
        # A deep-backfill candidate disappears as soon as this slice promotes
        # and fully translates it. Recompute the shrinking set instead of
        # comparing the old cursor against the pre-slice list.
        remaining, _large = _translation_candidate_paths(scan_id, phase)
        if remaining:
            continuation_job_id, _continuation_created = storage.create_or_get_typed_job(
                scan_id, "translate_package",
                options={"phase": phase, "cursor": 0, "slice": slice_no + 1},
                owner_id=job.get("owner_id") or "legacy",
            )
            next_cursor = 0
    elif not selected_only and next_cursor < len(paths):
        continuation_job_id, _continuation_created = storage.create_or_get_typed_job(
            scan_id, "translate_package",
            options={"phase": phase, "cursor": next_cursor},
            owner_id=job.get("owner_id") or "legacy",
        )
    elif not selected_only and large and phase == "preview_and_priority" and options.get("schedule_deep_backfill", True):
        continuation_job_id, _continuation_created = storage.create_or_get_typed_job(
            scan_id, "translate_package",
            options={"phase": "deep_backfill", "cursor": 0, "slice": 0},
            owner_id=job.get("owner_id") or "legacy",
        )
    if phase == "deep_backfill" and current:
        refresh_package_coverage(scan_id, scan_result, storage)
    return {
        "scan_id": scan_id,
        "phase": phase,
        "processed_files": len(current),
        "completed_files": completed,
        "failed_files": len(failed),
        "failures": failed[:50],
        "next_cursor": next_cursor if continuation_job_id else None,
        "continuation_job_id": continuation_job_id,
    }


def _run_claimed_scan_and_analyze_job(job):
    """Inventory a filesystem path asynchronously, then run the normal workflow."""
    job_id = job["id"]
    options = job.get("options") or {}
    storage.update_import_task(job_id, status="scanning", phase="scanning")
    root_path = str(options.get("root_path") or "").strip()
    if not root_path:
        raise ValueError("扫描任务缺少目录路径")

    # A supervised 24-hour slice may end after inventory was committed.  On
    # the next slice, continue from file checkpoints instead of rescanning a
    # potentially slow NAS tree.
    existing_scan = storage.get_scan(
        job_id, owner_id=options.get("owner_id") or job.get("owner_id")
    )
    if existing_scan and existing_scan.get("inventory_complete"):
        scan_result = existing_scan
    else:
        scan_result = None

    def scan_progress(file_count, directory_count=0, current_path=""):
        _ensure_job_active(job_id)
        activity_count = max(0, int(file_count or 0)) + max(0, int(directory_count or 0))
        scan_percent = min(14, 2 + max(0, activity_count.bit_length() - 1))
        storage.update_job(
            job_id, progress=scan_percent, stage="scanning",
            message="正在盘点目录：已发现 {} 个文件、{} 个目录".format(file_count, directory_count), heartbeat=True,
            current_stage="目录扫描", current_file=current_path or root_path,
        )

    storage.update_job(
        job_id, progress=1, stage="scanning", message="正在验证并扫描目录", heartbeat=True,
        current_stage="目录扫描", current_file=root_path,
    )
    resolved_root = _resolve_allowed_scan_root(root_path)
    if scan_result is None:
        saved_cursor = storage.get_inventory_cursor(job_id)
        if saved_cursor:
            saved_cursor.pop("status", None)
        scan_slice = scan_inventory_slice(
            resolved_root, cursor=saved_cursor,
            slice_entries=Config.SCAN_SLICE_ENTRIES,
            slice_seconds=Config.SCAN_SLICE_SECONDS,
            max_depth=options.get("max_depth", Config.MAX_SCAN_DEPTH),
            max_files=options.get("max_files", Config.MAX_SCAN_FILES),
            max_directories=Config.MAX_SCAN_DIRECTORIES,
            max_nodes=Config.MAX_SCAN_NODES,
            activity_callback=scan_progress,
            cancel_check=lambda: _ensure_job_active(job_id),
            yield_check=lambda: storage.has_queued_job_above_priority(
                int(job.get("priority") or 100)
            ),
            manifest_dir=Config.DATA_DIR / "inventory_manifests" / str(job_id),
        )
        scan_result = storage.save_inventory_slice(
            job_id, resolved_root, scan_slice["cursor"], scan_slice["records"],
            owner_id=options.get("owner_id") or job.get("owner_id") or "legacy",
            parse_mode=options.get("parse_mode"), complete=scan_slice["complete"],
        )
        preview_paths = [str(item.get("path") or item.get("node_path") or "") for item in (scan_slice.get("records") or []) if item.get("kind") == "file" and (item.get("path") or item.get("node_path"))]
        storage.update_import_task(
            job_id, status="inventory_ready" if scan_slice["complete"] else "scanning",
            phase="inventory_ready" if scan_slice["complete"] else "scanning",
            counts={"discovered_files": int(scan_slice["cursor"].get("file_count") or 0), "total_files": int(scan_slice["cursor"].get("file_count") or 0)},
            checkpoint=scan_slice.get("cursor") or {},
        )
        if not scan_slice["complete"]:
            return {
                "_requeue_slice": True,
                "_requeue_message": "目录清单检查点已保存，等待下一轮续扫。",
                "scan_id": job_id,
                "inventory_files": int(scan_slice["cursor"].get("file_count") or 0),
            }

    # Import is inventory-only. Do not parse files, build logical partitions, deduplicate, cluster, index content, or call a model before the user selects files.
    storage.update_import_task(
        job_id, status="waiting_for_selection", phase="waiting_for_selection",
        counts={"total_files": int(scan_result.get("file_count") or 0), "discovered_files": int(scan_result.get("file_count") or 0)},
    )
    storage.set_package_processing_state(
        job_id, "awaiting_selection", "目录已导入，等待按目录名和文件名选择深度解析范围。"
    )
    storage.update_analysis_progress_status(
        job_id, "awaiting_selection", "目录导入完成，尚未读取正文，等待选择文件。", "awaiting_selection"
    )
    storage.update_job(
        job_id, result={"scan_id": job_id, "scan_available": True, "_await_selection": True},
        progress=100, stage="awaiting_selection", message="目录导入完成，等待选择文件；当前未解析正文。",
        current_stage="等待选择文件", current_file="", heartbeat=True,
    )
    return {"scan_id": job_id, "_await_selection": True}
    large_directory_mode = build_policy(
        scan_result, _package_large_options()
    ).get("mode") == "large_directory"
    # The directory-first pass deliberately does not materialise one logical
    # row per CSV/JSONL partition across a multi-gigabyte package. Physical
    # files remain selectable; selected deep work can parse them later.
    if large_directory_mode and not scan_result.get("logical_inventory_complete"):
        scan_result["logical_inventory_complete"] = True
        scan_result["logical_file_count"] = storage.count_logical_inventory_entries(job_id)
        storage.update_scan(job_id, scan_result)

    # Older scans predate derived logical units. Rebuild them lazily before
    # analysis whenever eligible containers are present, even if their payload
    # was previously marked complete.
    logical_migration_needed = (
        not large_directory_mode
        and not bool(scan_result.get("logical_inventory_complete"))
    )
    if not large_directory_mode and not logical_migration_needed:
        logical_migration_needed = storage.count_logical_inventory_entries(job_id) == 0 and any(
            str(item.get("payload", {}).get("extension") or "").lower() in {
                ".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".bz2", ".rar", ".7z",
                ".csv", ".tsv", ".jsonl", ".xlsx", ".xlsm",
            }
            for item in storage.iter_inventory_entries(job_id, kind="file")
        )
    if logical_migration_needed:
        storage.update_job(
            job_id, progress=12, stage="logical_inventory",
            message="正在登记压缩包成员和大型结构化文件分片", heartbeat=True,
        )
        physical_files = (
            item["payload"]
            for item in storage.iter_inventory_entries(job_id, kind="file")
        )
        logical_counts = storage.replace_logical_inventory_entries(
            job_id,
            iter_logical_units(
                resolved_root, physical_files,
                partition_bytes=Config.LOGICAL_PARTITION_BYTES,
                rows_per_partition=Config.LOGICAL_PARTITION_ROWS,
                max_units_per_container=Config.MAX_LOGICAL_UNITS_PER_CONTAINER,
            ),
        )
        scan_result.update(logical_counts)
        scan_result["logical_inventory_complete"] = True
        storage.update_scan(job_id, scan_result)

    resource_plan = package_resource_plan(
        scan_result,
        state_free_bytes=shutil.disk_usage(str(Path(Config.DB_PATH).parent)).free,
        temp_free_bytes=shutil.disk_usage(str(Config.PARSE_TEMP_DIR)).free,
        preview_bytes_per_file=(
            Config.LARGE_PACKAGE_DIRECTORY_PREVIEW_BYTES_PER_FILE
            if large_directory_mode else Config.LARGE_PACKAGE_PREVIEW_BYTES_PER_FILE
        ),
        preview_total_bytes=(
            Config.LARGE_PACKAGE_DIRECTORY_PREVIEW_TOTAL_BYTES
            if large_directory_mode else Config.LARGE_PACKAGE_PREVIEW_TOTAL_BYTES
        ),
        max_content_bytes=Config.MAX_CONTENT_BYTES,
        temp_reserve_bytes=Config.PARSE_TEMP_DISK_RESERVE_BYTES,
        full_deep_backfill=(
            Config.LARGE_PACKAGE_BACKGROUND_BACKFILL and not large_directory_mode
        ),
        large_directory_mode=large_directory_mode,
    )
    scan_result["resource_plan"] = resource_plan
    storage.update_scan(job_id, scan_result)
    storage.update_import_task(job_id, status="previewing", phase="previewing", counts={"total_files": int(scan_result.get("logical_file_count") or scan_result.get("file_count") or 0), "discovered_files": int(scan_result.get("logical_file_count") or scan_result.get("file_count") or 0)})
    if not resource_plan["ready"]:
        return {
            "_defer_slice": True,
            "_defer_seconds": 300,
            "_defer_message": "资源预检未通过：{}；释放空间后自动继续。".format(
                "、".join(resource_plan["blockers"])
            ),
            "scan_id": job_id,
        }
    try:
        _publish_analysis_progress(
            job_id, scan_result, 15,
            "目录盘点完成，后台继续解析正文并建立证据索引",
            stage="inventory_ready",
        )
    except Exception:
        logger.warning("发布目录盘点概览失败 scan_id=%s", job_id, exc_info=True)
    # Make the physical inventory available to the browser immediately after
    # scanning, while the longer parse/cluster/report stages continue in the
    # same background job.  Only the id is stored here; the tree is fetched via
    # the authenticated /api/scan endpoint to avoid duplicating a large JSON
    # payload in the job row.
    storage.update_job(
        job_id,
        result={"scan_id": job_id, "scan_available": True},
        progress=15,
        stage="scanned",
        message="目录盘点完成，已显示原始目录，继续解析、去重和主题分析",
        heartbeat=True,
    )
    # The scan task keeps its own id as scan_id so the browser can use one job id
    # throughout the complete unknown-package workflow.
    preprocess_options = dict(options)
    preprocess_options.update({
        "preprocess_only": True,
        "continue_full": False,
        "workflow_source": "preprocessing",
        "scope_label": "导入预处理与文件范围确认",
    })
    return _run_claimed_analysis_job({
        "id": job_id, "scan_id": job_id, "options": preprocess_options, "progress": 15,
        "owner_id": options.get("owner_id") or job.get("owner_id") or "legacy",
        "_progress_start": 15, "_progress_end": 95,
    })


def _start_analysis_job(scan_id, options=None):
    """Persist work for the independent Worker; the API process never runs it."""
    storage.set_package_processing_state(scan_id, "running", "分析任务已提交")
    return storage.create_or_get_job(scan_id, options=options, owner_id=_request_owner_id() or "legacy")


@app.route("/")
def index():
    response = render_template("index.html")
    # The SPA shell changes with the workflow contract; never let a browser
    # or reverse proxy keep an old shell that points at stale JavaScript.
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/api/status")
def status():
    runtime = _python_runtime_status()
    return jsonify({
        "ok": True,
        "configured": llm.configured,
        "backend": ACTIVE_LLM_BACKEND,
        "local_model_enabled": llm_generation_enabled,
        "llm_backend": ACTIVE_LLM_BACKEND,
        "evidence_relevance_mode": embedding_mode(),
        "embedding_backend": ACTIVE_EMBEDDING_BACKEND,
        "embedding_model": getattr(_embedding_client, "model", None),
        "privacy": llm.privacy_label,
        "model_generation_enabled": llm_generation_enabled,
        "model": llm.model,
        "base_url": llm.base_url,
        # Keep the old key for clients that already read it, but make it
        # truthful and expose an explicit, versioned runtime contract.
        "python_compatible": runtime["supported"],
        "runtime_supported": runtime["supported"],
        "python_version": runtime["version"],
        "minimum_python_version": runtime["minimum"],
        "output_dir": str(Config.OUTPUT_DIR),
        "state_dir": str(Config.DATA_DIR),
        "sqlite_network_filesystem_allowed": os.getenv("SJFX_ALLOW_NETWORK_SQLITE", "0").strip().lower() in {"1", "true", "yes"},
        "document_parser": parser.status(),
        "supported_inputs": ["PDF", "Word", "PowerPoint", "Excel", "CSV/XLSX/JSON 数据画像", "图片 OCR", "文本/Markdown/HTML", "ZIP/TAR/TAR.GZ/TAR.BZ2 压缩包"],
        "local_features": [
            "Office 内嵌图片 OCR", "SHA-256 去重", "SimHash+LSH 聚类",
            "BM25/FTS 候选召回 + {} + 证据质量重排".format(
                "本地语义向量" if embedding_mode() != "lexical-fallback" else "TF-IDF 词法相关度"
            ),
            "自适应分析树", "自动概览 Word",
            "数据包本体可视化", "原文/中文双版本翻译", "多轮证据问答与按需晋升",
        ],
        "limits": {
            "max_scan_files": Config.MAX_SCAN_FILES,
            "max_scan_directories": Config.MAX_SCAN_DIRECTORIES,
            "max_scan_nodes": Config.MAX_SCAN_NODES,
            "max_scan_depth": Config.MAX_SCAN_DEPTH,
            "max_scan_entries_per_directory": Config.MAX_SCAN_ENTRIES_PER_DIRECTORY,
            "scan_ignored_directories": sorted(IGNORED_DIRS),
            "scan_ignored_files": sorted(IGNORED_FILES),
            "scan_default_full_inventory": not IGNORED_DIRS and not IGNORED_FILES,
            "max_document_characters": Config.MAX_FULL_DOCUMENT_CHARS,
            "max_content_bytes": Config.MAX_CONTENT_BYTES,
            "max_single_file_bytes": Config.MAX_SINGLE_FILE_BYTES,
            "max_parse_seconds": Config.MAX_PARSE_SECONDS,
            "source_stability_seconds": Config.SOURCE_STABILITY_SECONDS,
            "max_worker_memory_mb": Config.MAX_WORKER_MEMORY_MB,
            "max_parse_process_memory_mb": Config.MAX_PARSE_PROCESS_MEMORY_MB,
            "parse_process_isolation": Config.ENABLE_PARSE_PROCESS_ISOLATION,
            "parse_max_concurrency": Config.PARSE_MAX_CONCURRENCY,
            "parse_parallel_scope": "CPU 解析进程；Ollama/Qwen 推理保持串行",
            "parse_parallel_enabled": bool(
                Config.ENABLE_PARSE_PROCESS_ISOLATION and str(Config.DOCLING_DEVICE).lower() == "cpu"
            ),
            "max_archive_entries": Config.MAX_ARCHIVE_ENTRIES,
            "max_archive_file_bytes": Config.MAX_ARCHIVE_FILE_BYTES,
            "max_archive_member_bytes": Config.MAX_ARCHIVE_MEMBER_BYTES,
            "max_archive_uncompressed_bytes": Config.MAX_ARCHIVE_UNCOMPRESSED_BYTES,
            "max_archive_compression_ratio": Config.MAX_ARCHIVE_COMPRESSION_RATIO,
            "max_archive_member_path_depth": Config.MAX_ARCHIVE_MEMBER_PATH_DEPTH,
            "parse_temp_dir": str(Config.PARSE_TEMP_DIR),
            "parse_temp_disk_reserve_bytes": Config.PARSE_TEMP_DISK_RESERVE_BYTES,
            "max_analysis_jobs": Config.MAX_ANALYSIS_JOBS,
            "llm_max_concurrency": Config.LLM_MAX_CONCURRENCY,
            "max_export_bytes": Config.MAX_EXPORT_BYTES,
            "download_ticket_ttl_seconds": Config.DOWNLOAD_TICKET_TTL_SECONDS,
            "large_package": {
                "threshold_bytes": Config.LARGE_PACKAGE_THRESHOLD_BYTES,
                "threshold_files": Config.LARGE_PACKAGE_THRESHOLD_FILES,
                "initial_parse_files": Config.LARGE_PACKAGE_INITIAL_PARSE_FILES,
                "deepen_batch_files": Config.LARGE_PACKAGE_DEEPEN_BATCH_FILES,
                "batch_files": Config.LARGE_PACKAGE_BATCH_FILES,
                "background_batch_files": Config.LARGE_PACKAGE_BACKGROUND_BATCH_FILES,
                "background_backfill": Config.LARGE_PACKAGE_BACKGROUND_BACKFILL,
                "full_inventory_processing": bool(Config.LARGE_PACKAGE_BACKGROUND_BACKFILL),
                "full_inventory_preview": True,
                "directory_mode": Config.LARGE_PACKAGE_DIRECTORY_MODE,
                "directory_model_file_limit": Config.LARGE_PACKAGE_DIRECTORY_MODEL_FILE_LIMIT,
                "preview_bytes_per_file": Config.LARGE_PACKAGE_DIRECTORY_PREVIEW_BYTES_PER_FILE,
                "preview_total_bytes": Config.LARGE_PACKAGE_DIRECTORY_PREVIEW_TOTAL_BYTES,
                "selected_parse_max_files": Config.LARGE_PACKAGE_SELECTED_PARSE_MAX_FILES,
                "selected_parse_max_bytes": Config.LARGE_PACKAGE_SELECTED_PARSE_MAX_BYTES,
                "selected_model_file_limit": Config.LARGE_PACKAGE_SELECTED_MODEL_FILE_LIMIT,
                "selected_model_max_bytes": Config.LARGE_PACKAGE_SELECTED_MODEL_MAX_BYTES,
                "selected_batch_files": Config.LARGE_PACKAGE_SELECTED_BATCH_FILES,
                "deep_analysis_strategy": "directory_first_then_explicit_selection",
            },
            "translation": {
                "enabled": Config.ENABLE_TRANSLATION,
                "provider": translation_provider.provider_id,
                "configured_provider": Config.TRANSLATION_PROVIDER,
                "device": getattr(translation_provider, "device", "cpu") if Config.TRANSLATION_PROVIDER == "offline_nllb" else "local",
                "batch_size": Config.TRANSLATION_BATCH_SIZE if Config.TRANSLATION_PROVIDER == "offline_nllb" else 1,
                "cpu_threads": Config.TRANSLATION_CPU_THREADS if Config.TRANSLATION_PROVIDER == "offline_nllb" else None,
                "max_unit_chars": Config.TRANSLATION_MAX_UNIT_CHARS,
                "mode": "quality" if Config.TRANSLATION_REVIEW_COMPLEX_UNITS else "fast",
                "paragraph_batching": Config.TRANSLATION_COALESCE_PARAGRAPHS,
                "review_complex_units": Config.TRANSLATION_REVIEW_COMPLEX_UNITS,
                "import_translation_enabled": Config.ENABLE_IMPORT_TRANSLATION,
                "import_translation_max_files": Config.IMPORT_TRANSLATION_MAX_FILES,
                "import_translation_large_max_files": Config.IMPORT_TRANSLATION_LARGE_MAX_FILES,
                "import_translation_max_chars_per_file": Config.IMPORT_TRANSLATION_MAX_CHARS_PER_FILE,
                "import_translation_max_total_chars": Config.IMPORT_TRANSLATION_MAX_TOTAL_CHARS,
                "package_batch_files": Config.TRANSLATION_PACKAGE_BATCH_FILES,
                "auto_translate_packages": Config.AUTO_TRANSLATE_PACKAGES,
            },
        },
    })


@app.route("/api/test-model", methods=["POST"])
def test_model():
    try:
        require_local_model_enabled()
        health = llm.health_check()
        if not health["reachable"]:
            return api_error("\u672c\u5730 {} \u670d\u52a1\u4e0d\u53ef\u8fbe\uff1a{}".format(ACTIVE_LLM_BACKEND, health.get("error", "\u672a\u77e5\u9519\u8bef")), 503)
        if not health["model_available"]:
            return api_error("\u5df2\u8fde\u63a5\u672c\u5730 {}\uff0c\u4f46\u672a\u627e\u5230\u6a21\u578b {}".format(ACTIVE_LLM_BACKEND, llm.model), 503)
        # A model-list response only proves that the API server is up. Execute
        # one bounded generation so this control verifies the active transport.
        result = llm_transport.chat(
            "This is a local health check. Reply with exactly SJFX_OK and nothing else.",
            "Return the exact token now.",
            temperature=0,
            max_tokens=8,
            retries=0,
            timeout=min(30, int(getattr(Config, "VLLM_INTERACTIVE_TIMEOUT_SECONDS", 30))),
        )
        return jsonify({
            "ok": True,
            "reply": "\u672c\u5730 {} \u6a21\u578b\u5df2\u5b8c\u6210\u5b9e\u9645\u751f\u6210\u9a8c\u8bc1\u3002".format(ACTIVE_LLM_BACKEND),
            "model": result.get("model") or llm.model,
            "usage": result.get("usage") or {},
            "finish_reason": result.get("finish_reason"),
            "generation_reply": result.get("content") or "",
            "generation_tested": True,
        })
    except LocalModelError as exc:
        return api_error("\u672c\u5730 {} \u751f\u6210\u9a8c\u8bc1\u5931\u8d25\uff1a{}".format(ACTIVE_LLM_BACKEND, exc), 503)
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/general-chat", methods=["POST"])
def general_chat():
    """Answer a natural-language chat turn without requiring an imported package."""
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question") or "").strip()
    if not question or len(question) > 8000:
        return api_error("问题不能为空且不能超过 8000 字符", 400)
    try:
        require_local_model_enabled()
        history = payload.get("messages") or []
        if not isinstance(history, list):
            history = []
        transcript = []
        for item in history[-12:]:
            if not isinstance(item, dict):
                continue
            role = "用户" if item.get("role") == "user" else "助手"
            content = str(item.get("content") or "").strip()
            if content:
                transcript.append("{}：{}".format(role, content[:4000]))
        system = (
            "你是 SJFX 中的中文智能助手。像成熟的大模型产品一样自然、直接地回答用户。"
            "先理解用户真正想完成的事情，再给出有帮助的内容；可以闲聊、解释概念、写作、改写、翻译和提供思路。"
            "不要机械复述问题，不要每次都使用固定模板。需要时主动澄清关键范围，但不要为了显得谨慎而打断简单问题。"
            "当前对话可能还没有加载资料包；没有证据时不要声称看过用户文件，也不要捏造引用。"
            "如果问题需要资料，坦诚说明导入资料包后可以继续，并先给出通用回答或下一步。"
            "回答区分事实、判断和建议，语气友好、简洁，像真人协作伙伴。"
        )
        prompt = "对话历史：{}\n\n用户新问题：{}".format(
            "\n".join(transcript) if transcript else "无",
            question,
        )
        chat_max_tokens = 1400
        chat_timeout = min(45, int(getattr(Config, "CONVERSATION_MODEL_TIMEOUT_SECONDS", 45)))
        if Config.LLM_BACKEND == "vllm":
            chat_max_tokens = min(chat_max_tokens, int(getattr(Config, "VLLM_INTERACTIVE_MAX_TOKENS", 480)))
            chat_timeout = max(chat_timeout, int(getattr(Config, "VLLM_INTERACTIVE_TIMEOUT_SECONDS", 120)))
        system += "\n效率规则：默认直接回答，不超过 120 个汉字；用户明确要求详细、步骤、清单或长文时再展开。"
        result = llm_transport.chat(system, prompt, temperature=0.35, max_tokens=chat_max_tokens, timeout=chat_timeout)
        answer = str((result or {}).get("content") or "").strip()
        if not answer:
            raise LocalModelError("本地模型返回为空")
        return jsonify({
            "ok": True,
            "answer": answer,
            "model": (result or {}).get("model") or Config.LLM_MODEL,
            "evidence_status": "not_required",
            "task_status": "fulfilled",
        })
    except (ValueError, LocalModelError) as exc:
        return api_error(str(exc), 503)

@app.route("/api/scan", methods=["POST"])
def scan():
    payload = request.get_json(silent=True) or {}
    path = payload.get("path", "").strip()
    requested_parse_mode = str(payload.get("parse_mode") or "auto").strip().lower()
    parse_mode = requested_parse_mode if requested_parse_mode in {"auto", "fast", "accurate"} else "auto"
    if not path:
        return api_error("请输入要扫描的本地目录")
    try:
        # Do not synchronously resolve/inspect a NAS path in the HTTP process.
        # The supervised Worker performs the authoritative allow-list and
        # existence check, where a cancel request can terminate a blocked call.
        root = _validate_scan_path_request(path)
        job_id = storage.create_scan_job(
            root, payload.get("max_files", Config.MAX_SCAN_FILES), parse_mode,
            payload.get("max_depth", Config.MAX_SCAN_DEPTH),
            owner_id=_request_owner_id() or "legacy",
        )
        return jsonify({
            "ok": True,
            "accepted": True,
            "job_id": job_id,
            # Kept for old front-end clients.  The scan id becomes available in
            # the completed task result.
            "analysis_job_id": job_id,
            "status_url": "/api/jobs/{}".format(job_id),
        }), 202
    except Exception as exc:
        return api_error(str(exc))


@app.route("/api/data-sources")
def list_data_sources():
    """List reusable historical packages without loading their full payloads."""
    try:
        query = str(request.args.get("query") or "").strip()
        if len(query) > 200:
            raise ValueError("搜索条件不能超过 200 个字符")
        try:
            limit = int(request.args.get("limit", 30))
            offset = int(request.args.get("offset", 0))
        except (TypeError, ValueError):
            raise ValueError("分页参数无效")
        result = storage.list_scan_sources(
            owner_id=_request_owner_id(), query=query, limit=limit, offset=offset
        )
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/data-sources/select", methods=["POST"])
def select_data_source():
    """Validate and activate one owned historical package without new work."""
    payload = request.get_json(silent=True) or {}
    scan_id = str(payload.get("scan_id") or "").strip()
    if not scan_id or len(scan_id) > 128:
        return api_error("请选择有效的数据包", 400)
    result = storage.list_scan_sources(
        owner_id=_request_owner_id(), scan_id=scan_id, limit=1, offset=0
    )
    if not result["items"]:
        return api_error("数据包不存在、已失效或不属于当前访问用户", 404)
    source = result["items"][0]
    search_index = _conversation_index_status(scan_id)
    source["search_index"] = search_index
    # The source listing already contains the bounded queue/control snapshot;
    # selecting a package must not hydrate the full scan or create work.
    source["processing"] = source.get("processing") or {}
    if source["processing"].get("active_job_id"):
        next_action = "monitor_processing"
    elif search_index.get("usable") or source.get("analysis_ready"):
        next_action = "use_existing_data"
    else:
        next_action = "wait_for_processing"
    return jsonify({
        "ok": True,
        "selected": True,
        "source": source,
        "next_action": next_action,
        "created_job": False,
        "reused_existing_results": True,
    })


@app.route("/api/scan/<scan_id>")
def get_scan(scan_id):
    try:
        compact_value = str(request.args.get("compact", "")).strip().lower()
        full_requested = str(request.args.get("full", "")).strip().lower() in {"1", "true", "yes"}
        bounded_response = not full_requested and compact_value not in {"0", "false", "no"}
        if bounded_response:
            scan_result = storage.get_scan_overview(scan_id, owner_id=_request_owner_id())
            if not scan_result:
                raise ValueError("扫描任务不存在、已失效或不属于当前访问用户")
            try:
                summary_limit = max(1, min(500, int(request.args.get("summary_limit", 100))))
            except (TypeError, ValueError):
                summary_limit = 100
            summary_page = storage.list_summaries_page(scan_id, limit=summary_limit)
            physical_tree = storage.get_tree_page(scan_id, "physical", limit=200)
            if physical_tree:
                scan_result["tree"] = physical_tree
            analysis = storage.get_analysis_overview(scan_id)
            if analysis:
                analysis["analysis_tree"] = storage.get_tree_page(scan_id, "analysis", limit=100)
                analysis["preliminary_directory"] = analysis.get("preliminary_directory")
            return jsonify({
                "ok": True,
                "scan": scan_result,
                "summaries": summary_page["items"],
                "summaries_page": {
                    key: summary_page[key]
                    for key in ("offset", "limit", "total", "next_offset")
                },
                "analysis": analysis,
                "progressive_analysis": storage.get_analysis_progress(scan_id),
                "processing": _package_processing_status(scan_id, scan_result),
                "tree_edits": storage.list_tree_edits(scan_id, _request_owner_id(), limit=500),
                "tree_edits_total": storage.tree_edit_count(scan_id, _request_owner_id()),
                "response_mode": "bounded",
            })
        scan_result = require_scan(scan_id)
        if full_requested and scan_result.get("inventory_mode") == "durable_paged_v1":
            scan_result["tree"] = storage.build_inventory_tree(scan_id) or scan_result.get("tree")
        scan_result["scan_id"] = scan_id
        return jsonify({
            "ok": True,
            "scan": scan_result,
            "summaries": storage.list_summaries(scan_id),
            "analysis": storage.get_analysis(scan_id),
            "processing": _package_processing_status(scan_id, scan_result),
        })
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/package-processing/<scan_id>")
def get_package_processing(scan_id):
    try:
        scan_result = require_scan(scan_id)
        return jsonify({
            "ok": True,
            "processing": _package_processing_status(scan_id, scan_result),
        })
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/package-processing/<scan_id>/pause", methods=["POST"])
def pause_package_processing(scan_id):
    try:
        require_scan(scan_id)
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason") or "用户结束本次运行；已完成检查点保留。")
        result = storage.pause_package_analysis_jobs(scan_id, reason=reason)
        storage.update_analysis_progress_status(
            scan_id, "paused",
            "已安全暂停；已完成结果永久保留，未完成文件仍在待处理队列。",
            "paused",
        )
        return jsonify({
            "ok": True,
            **result,
            "processing": _package_processing_status(scan_id),
        })
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/package-processing/<scan_id>/resume", methods=["POST"])
def resume_package_processing(scan_id):
    """Resume or reprioritize the durable queue without dropping ordinary work."""
    try:
        scan_result = require_scan(scan_id)
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode") or "continue").strip().lower()
        if mode not in {"continue", "recall", "query", "selection"}:
            raise ValueError("未知续跑方式")
        control_state = storage.get_package_processing_control(scan_id).get("state")
        if control_state == "awaiting_selection" and mode != "selection":
            return jsonify({"ok": True, "accepted": False, "message": "请先在分析范围确认页面选择文件并确认后再开始深度分析。", "processing": _package_processing_status(scan_id, scan_result)}), 409
        continue_full = bool(payload.get("continue_full", True))
        inventory_paths = set(_inventory_by_path(scan_result))
        preferred_paths = []
        recall_items = []
        priority_source = {
            "continue": "background_backfill",
            "recall": "relationship_recall",
            "query": "user_query",
            "selection": "manual_selection",
        }[mode]
        reason = {
            "continue": "按原全量队列直接续跑",
            "recall": "按已处理文件的实体、编号、主题和引用关系召回",
            "query": "按用户关键词或自然语言研究要求优先",
            "selection": "用户手动指定优先文件",
        }[mode]

        if mode == "query":
            query = str(payload.get("query") or "").strip()
            if not query:
                raise ValueError("请输入关键词或自然语言研究要求")
            hits = storage.search_evidence_index(scan_id, query, limit=5000)
            for hit in hits:
                for key in ("source_path", "archive_source_path"):
                    candidate = str(hit.get(key) or "")
                    if candidate in inventory_paths:
                        preferred_paths.append(candidate)
                    elif "::" in candidate and candidate.split("::", 1)[0] in inventory_paths:
                        preferred_paths.append(candidate.split("::", 1)[0])
            preferred_paths = list(dict.fromkeys(preferred_paths))
        elif mode == "selection":
            requested = payload.get("target_paths") or []
            if not isinstance(requested, list):
                raise ValueError("target_paths 必须是文件路径数组")
            preferred_paths = [
                str(path) for path in requested if str(path) in inventory_paths
            ]
            preferred_paths = list(dict.fromkeys(preferred_paths))
            if not preferred_paths:
                raise ValueError("没有选中当前数据包中的有效文件")
        elif mode == "recall":
            workflow_by_path = {
                item.get("node_path"): item
                for item in storage.iter_file_workflow_states(scan_id)
            }
            analysis_by_path = {
                item.get("node_path"): item for item in storage.iter_file_states(scan_id)
            }
            completed_paths = {
                path for path, state in analysis_by_path.items()
                if str(state.get("status") or "") == "completed"
            }
            eligible_paths = {
                path for path in inventory_paths
                if deep_processing_eligible(
                    workflow_by_path.get(path), analysis_by_path.get(path)
                )
            }
            recall_items = storage.recall_file_relation_features(
                scan_id, completed_paths, eligible_paths, limit=5000,
            )
            if not recall_items:
                # Compatibility fallback for scans created before the complete
                # feature index migration.
                recall_items = relationship_recall_paths(
                    storage.get_content_map(scan_id) or {},
                    completed_paths,
                    eligible_paths,
                    limit=5000,
                )
            preferred_paths = [item["path"] for item in recall_items]

        if preferred_paths:
            workflow_for_priority = {
                item.get("node_path"): item
                for item in storage.iter_file_workflow_states(scan_id)
            }
            states_for_priority = {
                item.get("node_path"): item for item in storage.iter_file_states(scan_id)
            }
            preferred_paths = [
                path for path in preferred_paths
                if deep_processing_eligible(
                    workflow_for_priority.get(path), states_for_priority.get(path)
                )
            ]

        if mode in {"query", "recall", "selection"} and not preferred_paths:
            return jsonify({
                "ok": True,
                "accepted": False,
                "message": (
                    "全库基础索引中没有找到匹配的未处理文件"
                    if mode == "query" else (
                        "当前没有可召回的关联未处理文件"
                        if mode == "recall" else "所选文件均已处理或不属于可处理队列"
                    )
                ),
                "processing": _package_processing_status(scan_id, scan_result),
                "recall": [],
            })

        if preferred_paths:
            storage.prioritize_file_workflow_states(
                scan_id, preferred_paths, priority_source, reason,
                score_boost=2000.0 if mode == "selection" else 1500.0,
            )
        storage.set_package_processing_state(scan_id, "running", reason)
        batch_paths = _next_package_batch(
            scan_id, scan_result, preferred_paths=preferred_paths
        )
        if not batch_paths:
            processing = _package_processing_status(scan_id, scan_result)
            if processing.get("all_eligible_complete"):
                storage.set_package_processing_state(
                    scan_id, "completed", "全部有效逻辑文件已经完成。"
                )
            else:
                storage.set_package_processing_state(
                    scan_id, "paused", "没有可立即领取的文件；请检查失败或受限队列。"
                )
            return jsonify({
                "ok": True,
                "accepted": False,
                "message": "当前没有待处理的有效逻辑文件",
                "processing": _package_processing_status(scan_id, scan_result),
                "recall": recall_items[:100],
            })
        options = {
            "target_paths": batch_paths,
            "workflow_source": priority_source if mode != "continue" else "background_backfill",
            "scope_label": reason,
            "parse_mode": "auto" if mode == "selection" else "accurate",
            "continue_full": continue_full,
            "continuation_depth": 0,
        }
        job_id, created = storage.create_or_get_typed_job(
            scan_id, "analyze_package", options=options,
            owner_id=_request_owner_id() or "legacy",
        )
        storage.update_analysis_progress_status(
            scan_id, "queued",
            "已按新的优先级生成下一批；普通待处理文件仍保留。",
            "queued",
        )
        return jsonify({
            "ok": True,
            "accepted": True,
            "job_id": job_id,
            "reused_active_job": not created,
            "mode": mode,
            "batch_files": len(batch_paths),
            "preferred_matches": len(preferred_paths),
            "continue_full": continue_full,
            "recall": recall_items[:100],
            "processing": _package_processing_status(scan_id, scan_result),
            "status_url": "/api/jobs/{}".format(job_id),
        }), 202 if created else 200
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/scan/<scan_id>", methods=["DELETE"])
def delete_scan(scan_id):
    """Delete one owned, inactive scan and all of its durable artifacts."""
    try:
        require_scan(scan_id)
        result = storage.delete_scan(
            scan_id,
            owner_id=_request_owner_id(),
            output_dir=Config.OUTPUT_DIR,
        )
        if not result.get("deleted"):
            return api_error("扫描不存在或已被清理", 404)
        return jsonify({"ok": True, "cleanup": result})
    except RuntimeError as exc:
        return api_error(str(exc), 409)
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/scan/<scan_id>/selection", methods=["GET"])
def get_scan_selection(scan_id):
    """Return the resumable import-preprocessing selection contract."""
    try:
        scan_result = require_scan(scan_id)
        selection = storage.get_scan_selection(scan_id)
        if not selection:
            defaults = storage.default_scan_selection(scan_id)
            selection = storage.save_scan_selection(
                scan_id, defaults.get("included_paths"), defaults.get("excluded_paths"),
                defaults.get("rules"), status="draft",
            )
        selection_stats = storage.scan_selection_stats(scan_id, selection)
        selection.update(selection_stats)
        return jsonify({
            "ok": True,
            "scan_id": scan_id,
            "selection": selection,
            "processing": _package_processing_status(scan_id, scan_result),
            "preprocessing": (storage.get_analysis(scan_id) or {}).get("preprocessing") or {},
            "search_index": _conversation_index_status(scan_id),
        })
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/scan/<scan_id>/selection", methods=["POST"])
def save_scan_selection_draft(scan_id):
    try:
        require_scan(scan_id)
        payload = request.get_json(silent=True) or {}
        current = storage.get_scan_selection(scan_id) or {}
        included = payload.get("included_paths", current.get("included_paths") or [])
        excluded = payload.get("excluded_paths", current.get("excluded_paths") or [])
        if not isinstance(included, list) or not isinstance(excluded, list):
            raise ValueError("included_paths 和 excluded_paths 必须是数组")
        inventory = _inventory_by_path(require_scan(scan_id))
        valid = set(inventory)
        included = list(dict.fromkeys(str(path) for path in included if str(path) in valid))
        excluded = list(dict.fromkeys(str(path) for path in excluded if str(path) in valid and str(path) not in included))
        selection = storage.save_scan_selection(
            scan_id, included, excluded, payload.get("rules") or current.get("rules") or {},
            status="draft", expected_version=payload.get("version"),
        )
        selection.update(storage.scan_selection_stats(scan_id, selection))
        storage.set_package_processing_state(scan_id, "awaiting_selection", "等待用户确认分析范围。")
        return jsonify({"ok": True, "selection": selection, "processing": _package_processing_status(scan_id)})
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/scan/<scan_id>/selection/confirm", methods=["POST"])
def confirm_scan_selection(scan_id):
    try:
        scan_result = require_scan(scan_id)
        payload = request.get_json(silent=True) or {}
        current = storage.get_scan_selection(scan_id) or {}
        included = payload.get("included_paths", current.get("included_paths") or [])
        excluded = payload.get("excluded_paths", current.get("excluded_paths") or [])
        if not isinstance(included, list) or not isinstance(excluded, list):
            raise ValueError("included_paths 和 excluded_paths 必须是数组")
        inventory = _inventory_by_path(scan_result)
        valid = set(inventory)
        included = list(dict.fromkeys(str(path) for path in included if str(path) in valid))
        excluded = list(dict.fromkeys(str(path) for path in excluded if str(path) in valid and str(path) not in included))
        workflow_by_path = {item.get("node_path"): item for item in storage.iter_file_workflow_states(scan_id)}
        states_by_path = {item.get("node_path"): item for item in storage.iter_file_states(scan_id)}
        eligible = [
            path for path in included
            if deep_processing_eligible(workflow_by_path.get(path), states_by_path.get(path))
        ]
        if not eligible:
            raise ValueError("没有可进入深度分析的文件；请至少选择一个可解析文件")
        selection = storage.save_scan_selection(
            scan_id, included, excluded, payload.get("rules") or current.get("rules") or {},
            status="confirmed", expected_version=payload.get("version"),
        )
        selection.update(storage.scan_selection_stats(scan_id, selection))
        storage.prioritize_file_workflow_states(
            scan_id, eligible, "user_selection", "用户确认的分析范围", score_boost=2500.0,
        )
        # Persist an explicit deep-parse batch for resumable import accounting.
        deep_batch_id = None
        # Ordinary packages already completed the full light-parse pass before
        # this confirmation. The user's selection is therefore the deep scope;
        # do not send it through the large-package candidate-preview stage.
        large_directory_mode = False
        if not large_directory_mode:
            task = storage.get_import_task(scan_id) or {}
            checkpoint = dict(task.get("checkpoint") or {})
            checkpoint.update({"selected_paths": eligible, "selection_version": int(selection.get("version") or 1), "preliminary_summary_job_ids": [], "preliminary_node_job_ids": [], "preliminary_overview_job_id": None})
            storage.transition_import_task(scan_id, "parsing_selected", checkpoint=checkpoint,
                                          reason="已选择文件，开始仅解析选中范围。")
            options = {
                "target_paths": eligible,
                "workflow_source": "selected_file_parse",
                "scope_label": "用户选择的解析范围（{} 个文件）".format(len(eligible)),
                "parse_mode": "accurate",
                "continue_full": False,
                "candidate_preview": False,
                "selection_version": int(selection.get("version") or 1),
                "deep_batch_id": deep_batch_id,
                "deep_selection": {
                    "kind": "files", "paths": eligible,
                    "selected_file_count": len(eligible),
                },
            }
            job_id, created = storage.create_or_get_typed_job(
                scan_id, "analyze_package", options=options,
                owner_id=_request_owner_id() or "legacy",
            )
            storage.update_analysis_progress_status(
                scan_id, "queued", "已提交选中文件解析任务，等待 Worker 开始。", "parsing_selected"
            )
            return jsonify({
                "ok": True, "accepted": True, "confirmed": True,
                "selection": selection, "job_id": job_id,
                "deep_batch_id": deep_batch_id,
                "selected_count": len(eligible), "selected_paths": eligible,
                "reused_active_job": not created,
                "status_url": "/api/jobs/{}".format(job_id),
                "processing": _package_processing_status(scan_id, scan_result),
            }), 202 if created else 200

        storage.set_package_processing_state(scan_id, "candidate_importing", "候选文件已确认，开始候选集完整导入。")
        options = {
            "target_paths": eligible,
            "workflow_source": "candidate_preview",
            "scope_label": "候选集完整导入（{} 个文件）".format(len(eligible)),
            "parse_mode": "fast",
            "continue_full": False,
            "candidate_preview": True,
            "selection_version": int(selection.get("version") or 1),
            "deep_batch_id": deep_batch_id,
            "candidate_deadline_at": time.time() + int(getattr(Config, "CANDIDATE_PREVIEW_MAX_WAIT_SECONDS", 900)),
        }
        job_id, created = storage.create_or_get_typed_job(
            scan_id, "analyze_package", options=options,
            owner_id=_request_owner_id() or "legacy",
        )
        storage.update_analysis_progress_status(scan_id, "queued", "已确认候选范围，等待候选集导入任务开始。", "candidate_importing")
        return jsonify({
            "ok": True, "accepted": True, "confirmed": True,
            "selection": selection, "job_id": job_id,
            "deep_batch_id": deep_batch_id,
            "reused_active_job": not created, "selected_count": len(eligible),
            "status_url": "/api/jobs/{}".format(job_id),
            "processing": _package_processing_status(scan_id, scan_result),
        }), 202 if created else 200
    except ValueError as exc:
        return api_error(str(exc), 400)

@app.route("/api/scan/<scan_id>/selection/supplement", methods=["POST"])
def supplement_scan_selection(scan_id):
    """Add files to a confirmed scope without rerunning completed work."""
    try:
        scan_result = require_scan(scan_id)
        payload = request.get_json(silent=True) or {}
        current = storage.get_scan_selection(scan_id) or {}
        task = storage.get_import_task(scan_id) or {}
        task_status = str(task.get("status") or "")
        if task_status not in {"deep_summarizing_files", "deep_summarizing_nodes", "deep_update_available"}:
            return api_error("请等待初步概览或当前更新完成后再补充文件。", 409, {"task_status": task_status})
        if not isinstance(payload.get("included_paths", []), list):
            raise ValueError("included_paths 必须是数组")
        inventory = _inventory_by_path(scan_result)
        requested = list(dict.fromkeys(str(path) for path in (payload.get("included_paths") or []) if str(path) in inventory))
        existing = [str(path) for path in (current.get("included_paths") or []) if str(path) in inventory]
        new_paths = [path for path in requested if path not in set(existing)]
        if not new_paths:
            return jsonify({"ok": True, "accepted": False, "message": "没有新增的可解析文件。", "selection": current})
        requested = list(dict.fromkeys(existing + new_paths))
        excluded = [str(path) for path in (payload.get("excluded_paths") or current.get("excluded_paths") or []) if str(path) in inventory and str(path) not in requested]
        selection = storage.save_scan_selection(scan_id, requested, excluded, payload.get("rules") or current.get("rules") or {}, status="confirmed", expected_version=payload.get("version", current.get("version")))
        selection.update(storage.scan_selection_stats(scan_id, selection))
        task = storage.get_import_task(scan_id) or {}
        checkpoint = dict(task.get("checkpoint") or {})
        checkpoint.update({"selected_paths": requested, "selection_version": int(selection.get("version") or 1), "preliminary_node_job_ids": [], "deep_summary_node_job_ids": [], "preliminary_overview_job_id": None, "supplement_paths": new_paths})
        storage.transition_import_task(scan_id, "parsing_selected", checkpoint=checkpoint, reason="用户补充 {} 个文件，开始仅解析新增范围。".format(len(new_paths)))
        options = {"target_paths": new_paths, "workflow_source": "selected_file_parse", "scope_label": "用户补充的解析范围（{} 个文件）".format(len(new_paths)), "parse_mode": "accurate", "continue_full": False, "selection_version": int(selection.get("version") or 1), "supplement": True}
        job_id, created = storage.create_or_get_typed_job(scan_id, "analyze_package", options=options, owner_id=_request_owner_id() or "legacy")
        return jsonify({"ok": True, "accepted": True, "selection": selection, "new_paths": new_paths, "job_id": job_id, "reused_active_job": not created, "status_url": "/api/jobs/{}".format(job_id), "processing": _package_processing_status(scan_id, scan_result)}), 202 if created else 200
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/import-tasks/<scan_id>")
def get_import_task_state(scan_id):
    """Return the durable import-task state and explicit deep-parse batches."""
    try:
        require_scan(scan_id)
        task = storage.get_import_task(scan_id, owner_id=_request_owner_id())
        if not task:
            return api_error("导入任务不存在", 404)
        processing = _package_processing_status(scan_id)
        counts = storage.file_workflow_counts(scan_id)
        task["counts"] = {**counts, **storage.package_processing_counts(scan_id)}
        task["deep_parse_batches"] = storage.list_deep_parse_batches(scan_id)
        task["preview_queue"] = storage.preview_queue_counts(scan_id)
        task["state_contract"] = {
            "created": "已创建", "scanning": "正在扫描", "inventory_ready": "清单已就绪",
            "previewing": "全量轻度解析中", "preview_paused": "轻量预览已暂停",
            "preview_completed": "轻量预览完成", "waiting_for_selection": "等待选择",
            "parsing_selected": "正在解析选中文件", "parsed_overview": "正在生成解析版智能目录和情报概览", "preliminary_summarizing": "正在生成文件初步摘要", "preliminary_nodes": "正在生成节点初步摘要", "preliminary_overview": "正在生成初步情报概览", "deep_summarizing_files": "后台生成文件深度摘要", "deep_summarizing_nodes": "后台生成节点深度摘要", "deep_overview_updating": "正在更新深度证据概览",
            "candidate_importing": "候选集完整导入中", "candidate_analyzing": "候选文件预读分析中",
            "waiting_for_deep_selection": "等待选择深度文件或节点",
            "deep_parsing": "深度解析中", "deep_paused": "深度解析已暂停",
            "summarizing_files": "正在生成文件摘要与结论",
            "building_directory": "正在生成正式智能目录",
            "completed": "已完成", "partial": "部分完成，存在待复核项",
            "failed": "失败", "paused": "已暂停", "cancelled": "已取消",
        }
        task["processing"] = processing
        return jsonify({"ok": True, "import_task": task})
    except ValueError as exc:
        return api_error(str(exc), 404)

def _build_candidate_preview_directory(scan_id):
    # Build a model-informed provisional directory from candidate summaries.
    task = storage.get_import_task(scan_id) or {}
    checkpoint = task.get("checkpoint") or {}
    candidate_paths = [str(path) for path in (checkpoint.get("candidate_paths") or []) if str(path)]
    content_map = storage.get_content_map(scan_id) or {}
    if not candidate_paths and not content_map:
        return None
    allowed = set(candidate_paths)
    summaries = {}
    for row in storage.list_summaries(scan_id):
        path = str(row.get("path") or row.get("node_path") or "")
        payload = row.get("payload") or {}
        level = str(payload.get("analysis_level") or "").lower()
        depth = str(payload.get("analysis_depth") or "").lower()
        if path in allowed and (level in {"preview", "deep"} or depth in {"preview", "deep", "deep_document", "deep_folder"}):
            summaries[path] = payload
    # The metadata-backed type directory is useful even while representative
    # model cards are still queued. Keep an empty model-summary set and let
    # the return contract expose type nodes immediately.
    if not summaries:
        summaries = {}

    groups = {}
    for path, payload in summaries.items():
        labels = payload.get("topics") or payload.get("keywords") or []
        if isinstance(labels, str):
            labels = [labels]
        labels = [str(item.get("name") if isinstance(item, dict) else item).strip() for item in labels]
        labels = [item for item in labels if item]
        labels = list(dict.fromkeys(labels[:2])) or ["candidate-overview"]
        for label in labels:
            key = label.lower()
            group = groups.setdefault(key, {"name": label, "member_paths": [], "payloads": []})
            if path not in group["member_paths"]:
                group["member_paths"].append(path)
            group["payloads"].append(payload)

    topics = []
    for key, group in groups.items():
        payloads = group["payloads"]
        summaries_text = [str(item.get("summary") or item.get("core_summary") or "").strip() for item in payloads]
        summaries_text = [item for item in summaries_text if item]
        conclusions = []
        seen = set()
        for path in group["member_paths"]:
            payload = summaries[path]
            for claim in (payload.get("file_conclusions") or [])[:6]:
                if not isinstance(claim, dict):
                    continue
                text = str(claim.get("text") or claim.get("statement") or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                supports = list(claim.get("supports") or claim.get("evidence") or [])[:4]
                scores = [float(item.get("support_score")) for item in supports if isinstance(item, dict) and item.get("support_score") is not None]
                conclusions.append({
                    "statement": text,
                    "text": text,
                    "supporting_files": [path],
                    "supporting_evidence": supports,
                    "confidence": round(max(scores) if scores else 0.5, 3),
                    "analysis_level": "deep" if (str(payload.get("analysis_level") or "").lower() == "deep" or str(payload.get("analysis_depth") or "").lower() in {"deep", "deep_document", "deep_folder"} or payload.get("deep_analysis")) else "preview",
                    "verification_status": "verified" if (str(payload.get("analysis_level") or "").lower() == "deep" or str(payload.get("analysis_depth") or "").lower() in {"deep", "deep_document", "deep_folder"} or payload.get("deep_analysis")) else "candidate",
                })
        digest = " ".join(summaries_text[:3])
        deep_member_count = sum(1 for path in group["member_paths"] if (
            str((summaries.get(path) or {}).get("analysis_level") or "").lower() == "deep"
            or str((summaries.get(path) or {}).get("analysis_depth") or "").lower()
            in {"deep", "deep_document", "deep_folder"}
            or bool((summaries.get(path) or {}).get("deep_analysis"))
        ))
        group_level = (
            "deep"
            if deep_member_count == len(group["member_paths"]) and deep_member_count
            else "preview"
        )
        group_verification = "verified" if group_level == "deep" else "candidate"
        topics.append({
            "node_id": "candidate-" + hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:12],
            "kind": "group",
            "name": group["name"],
            "summary": digest[:900],
            "research_value": "用于在正式全文校验前定位相关文献和研究方向。",
            "research_questions": [],
            "member_paths": sorted(group["member_paths"]),
            "file_count": len(group["member_paths"]),
            "conclusions": conclusions,
            "conflicts": [],
            "limitations": ["候选分析只使用代表性内容，尚未完成全文证据校验。"],
            "coverage": {
                "file_count": len(group["member_paths"]),
                "preview_analyzed_files": len(group["member_paths"]),
                "analyzed_files": deep_member_count,
                "deep_files": deep_member_count,
                "ratio": round(
                    deep_member_count / float(len(group["member_paths"]) or 1), 3
                ),
                "analysis_level": group_level,
                "verification_status": group_verification,
            },
            "analysis_level": group_level,
            "verification_status": group_verification,
            "formal": group_level == "deep",
        })
    # Always expose a complete, metadata-backed type directory alongside
    # model-derived topic nodes. Members are resolved lazily from inventory
    # when a type node is selected, so a huge package never returns millions
    # of paths in one HTTP response.
    category_nodes = []
    for category in content_map.get("content_categories") or []:
        category_id = str(category.get("category_id") or "").strip()
        count = int(category.get("file_count") or 0)
        if not category_id or count <= 0:
            continue
        # Category membership is intentionally lazy. The bounded examples
        # make the node inspectable immediately; selecting it invokes the
        # server-side inventory matcher and creates a bounded plan.
        category_nodes.append({
            "node_id": "content-{}".format(category_id),
            "kind": "group",
            "name": category.get("name") or category_id,
            "summary": category.get("description") or "按文件内容信号归纳的候选资料类别。",
            "research_value": category.get("research_value") or "",
            "research_questions": list(category.get("research_questions") or [])[:4],
            "classification_basis": list(category.get("classification_basis") or [
                "path_and_filename", "document_type", "bounded_keywords", "bounded_content_sample",
            ]),
            "selection_filter": {
                **dict(category.get("selection_filter") or {}),
                "kind": "content_category",
                "category_id": category_id,
            },
            "member_paths": list(category.get("representative_paths") or [])[:12],
            "member_paths_lazy": True,
            "file_count": count,
            "total_bytes": int(category.get("total_bytes") or 0),
            "total_size_human": category.get("total_size_human") or "",
            "representative_paths": list(category.get("representative_paths") or [])[:12],
            "matched_terms": list(category.get("matched_terms") or [])[:10],
            "coverage": {
                **dict(category.get("coverage") or {}),
                "inventory_files": count,
                "classified_files": count,
                "membership_complete": True,
                "members_materialized": False,
                "analysis_level": "inventory_preview",
                "verification_status": "metadata_and_rule_classified",
            },
            "analysis_level": "inventory_preview",
            "verification_status": "candidate",
            "formal": False,
        })
    category_nodes.sort(key=lambda item: (-int(item.get("file_count") or 0), str(item.get("name") or "")))

    type_nodes = []
    for item in content_map.get("formats") or []:
        extension = str(item.get("extension") or "[no_extension]")
        count = int(item.get("file_count") or 0)
        if count <= 0:
            continue
        safe_key = extension.casefold()
        type_nodes.append({
            "node_id": "type-" + hashlib.sha1(safe_key.encode("utf-8", "ignore")).hexdigest()[:12],
            "kind": "group",
            "name": "文件类型：{}".format(extension),
            "summary": "按文件扩展名建立的全量类型节点，共 {} 个文件。".format(count),
            "classification_basis": "file_type",
            "selection_filter": {"kind": "extension", "extension": extension},
            "member_paths": [],
            "member_paths_lazy": True,
            "file_count": count,
            "coverage": {
                "inventory_files": int((content_map.get("inventory") or {}).get("file_count") or 0),
                "type_files": count,
                "inventory_coverage": 1.0,
                "analysis_level": "inventory",
                "verification_status": "metadata_verified",
            },
            "analysis_level": "inventory",
            "verification_status": "metadata_verified",
            "formal": False,
        })
    type_nodes.sort(key=lambda item: (-int(item.get("file_count") or 0), str(item.get("name") or "")))
    topics = category_nodes + type_nodes + topics
    inventory = content_map.get("inventory") or {}
    preview_coverage = content_map.get("coverage") or {}

    return {
        "schema_version": "preview-directory/2.0",
        "status": "provisional",
        "label": "候选集初步智能目录",
        "formal": False,
        "analysis_level": "preview",
        "verification_status": "candidate",
        "candidate_paths": sorted(summaries),
        "topics": topics,
        "content_categories": list(content_map.get("content_categories") or []),
        "research_directions": list(content_map.get("research_directions") or []),
        "content_taxonomy": content_map.get("content_taxonomy") or {},
        "classification_contract": {
            "primary_category_coverage": "全量清单中的每个文件都有一个主类别；类别成员在用户选择后从持久化清单解析。",
            "model_claim_scope": "模型结论只适用于已进入模型的文件，不自动外推到类别中的全部文件。",
            "raw_keyword_topics": "仅作召回线索，不作为最终智能目录的主分类。",
        },
        "coverage": {
            "inventory_files": int(inventory.get("file_count") or 0),
            "inventory_bytes": int(inventory.get("total_bytes") or 0),
            "candidate_files": len(candidate_paths),
            "summarized_files": len(summaries),
            "representative_model_files": len(candidate_paths),
            "bounded_preview_files": int(preview_coverage.get("previewed_files") or 0),
            "deferred_files": int(preview_coverage.get("deferred_files") or 0),
            "inventory_coverage": 1.0,
            "model_coverage_ratio": round(len(summaries) / float(len(candidate_paths) or 1), 3),
            "ratio": round(len(summaries) / float(len(candidate_paths) or 1), 3),
            "sampling_strategy": "all_metadata_plus_bounded_content_and_representative_model_cards",
            "classified_files": int(preview_coverage.get("classified_files") or inventory.get("file_count") or 0),
            "classification_coverage": float(preview_coverage.get("classification_coverage") or 1.0),
            "category_count": len(category_nodes),
            "research_direction_count": len(content_map.get("research_directions") or []),
            "directory_stage": "preliminary_content_taxonomy",
        },
    }


@app.route("/api/scan/<scan_id>/preliminary-directory")
def preliminary_directory(scan_id):
    """Return the provisional preview directory with an explicit confidence contract."""
    try:
        require_scan(scan_id)
        analysis = storage.get_analysis(scan_id) or {}
        directory = _build_candidate_preview_directory(scan_id) or analysis.get("preliminary_directory") or {
            "schema_version": "preview-directory/1.0",
            "status": "unavailable",
            "label": "初步智能目录（尚未生成）",
            "formal": False,
            "topics": [],
        }
        return jsonify({"ok": True, "directory": directory, "formal": False,
                        "notice": "该目录基于轻量预览，仅用于筛选；正式目录须在深度解析和证据校验后生成。"})
    except ValueError as exc:
        return api_error(str(exc), 404)




@app.route("/api/package/<scan_id>/preliminary-directory")
def package_preliminary_directory(scan_id):
    """Package namespace alias for the candidate/provisional directory."""
    return preliminary_directory(scan_id)
@app.route("/api/scan/<scan_id>/files")
def list_scan_files(scan_id):
    """Bounded file explorer shared by import selection and chat search."""
    try:
        require_scan(scan_id)
        offset = max(0, int(request.args.get("offset", 0) or 0))
        limit = max(1, min(100, int(request.args.get("limit", 50) or 50)))
        query = str(request.args.get("query") or "").strip()
        file_type = str(request.args.get("file_type") or request.args.get("type") or "").strip()
        search_scope = str(request.args.get("search_scope") or "all").strip().lower()
        status = str(request.args.get("status") or "all").strip().lower()
        selection = storage.get_scan_selection(scan_id) or {}
        included = set(selection.get("included_paths") or [])
        excluded = set(selection.get("excluded_paths") or [])
        if query or file_type:
            page = storage.search_file_status_page(
                scan_id,
                query=query,
                file_type=file_type,
                search_scope=search_scope,
                offset=offset,
                limit=limit,
                status=status,
            )
            page_items = page.get("items") or []
            total = int(page.get("total") or 0)
            coverage = _conversation_index_status(scan_id)
            if coverage.get("ready"):
                coverage_notice = (
                    "当前同时匹配文件名、路径、文件类型和已建立的正文/附件索引。"
                )
            else:
                coverage_notice = (
                    "当前按文件名、路径、扩展名和文件类型筛选；正文/附件全文索引尚未完成。"
                )
        else:
            page = storage.list_file_status_page(
                scan_id, offset=offset, limit=limit, status=status
            )
            page_items, total = page.get("items") or [], int(page.get("total") or 0)
            coverage = _conversation_index_status(scan_id)
            coverage_notice = None
        for item in page_items:
            path = str(item.get("node_path") or item.get("path") or "")
            preview = storage.get_file_preview(scan_id, path) if path else None
            item["selected"] = path in included and path not in excluded
            item["excluded"] = path in excluded
            if preview:
                item["preview"] = {
                    "status": preview.get("status"),
                    "text": str(preview.get("text") or preview.get("sample") or "")[:1200],
                    "warnings": list(preview.get("warnings") or [])[:8],
                    "page": preview.get("page"),
                    "section": preview.get("section"),
                    "level": "preview",
                    "formal_evidence_ready": False,
                    "parse_plan": parse_plan(item, preview),
                }
        return jsonify({
            "ok": True, "items": page_items, "offset": offset, "limit": limit,
            "total": total, "next_offset": offset + len(page_items) if offset + len(page_items) < total else None,
            "query": query, "file_type": file_type, "search_scope": (
                "metadata_and_evidence" if (query or file_type) else "inventory"
            ), "selection": {"version": int(selection.get("version") or 0), "included_count": len(included), "excluded_count": len(excluded)},
            "coverage": coverage, "coverage_notice": coverage_notice,
        })
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/file-workflow/<scan_id>")
def get_file_workflow(scan_id):
    """Return the single user-facing state contract for every logical file.

    The legacy endpoint exposed scheduling rows only.  That made previewed,
    failed, and archive-member files indistinguishable from completed work.
    ``Storage.list_file_status_page`` joins the immutable inventory with all
    durable processing stages and returns the canonical seven-state projection.
    """
    try:
        require_scan(scan_id)
        node_path = str(request.args.get("path") or "").strip()
        if node_path:
            item = storage.get_file_status(scan_id, node_path)
            if not item:
                raise ValueError("文件不在当前数据包清单中")
            preview = storage.get_file_preview(scan_id, node_path) or {}
            item["parse_plan"] = parse_plan(item, preview)
            item["analysis_level"] = "preview" if preview else "inventory"
            item["formal_evidence_ready"] = False
            return jsonify({
                "ok": True,
                "item": item,
                "workflow": storage.get_file_workflow_state(scan_id, node_path),
                "analysis_state": storage.get_file_state(scan_id, node_path),
                "translation": storage.get_translation(scan_id, node_path, hydrate=False),
            })
        offset = max(0, int(request.args.get("offset", 0) or 0))
        limit = max(1, min(500, int(request.args.get("limit", 100) or 100)))
        status = str(request.args.get("status") or "all").strip().lower()
        # Keep old clients functional while giving the new UI a truthful
        # status filter. ``deferred`` historically meant not yet selected,
        # not retry-waiting, so it maps to the pending presentation state.
        if status == "all" and request.args.get("selection_state"):
            status = {
                "priority": "pending", "deferred": "pending",
                "pending_preview": "pending", "excluded": "out_of_scope",
            }.get(str(request.args.get("selection_state")).strip(), "all")
        filters = {
            "min_size": request.args.get("min_size"),
            "max_size": request.args.get("max_size"),
            "min_modified_ns": request.args.get("min_modified_ns"),
            "max_modified_ns": request.args.get("max_modified_ns"),
            "language": request.args.get("language"),
            "archive_member": request.args.get("archive_member"),
            "keyword": request.args.get("keyword"),
            "entity": request.args.get("entity"),
            "deep": request.args.get("deep"),
        }
        page = storage.list_file_status_page(
            scan_id, offset=offset, limit=limit, status=status, filters=filters,
        )
        page["filters"] = {key: value for key, value in filters.items() if value not in (None, "")}
        for item in page.get("items") or []:
            path = str(item.get("node_path") or item.get("path") or "")
            preview = storage.get_file_preview(scan_id, path) if path else None
            item["parse_plan"] = parse_plan(item, preview or {})
            item["analysis_level"] = "deep" if str(item.get("evidence_status") or "").lower() == "ready" else ("preview" if preview else "inventory")
            item["formal_evidence_ready"] = item["analysis_level"] == "deep"
        page["status_counts"] = storage.file_status_counts(scan_id)
        return jsonify({"ok": True, **page})
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/file-workflow/<scan_id>/retry", methods=["POST"])
def retry_file_workflow(scan_id):
    """Queue selected failed/partial files for an explicit operator retry."""
    try:
        scan_result = require_scan(scan_id)
        payload = request.get_json(silent=True) or {}
        requested = payload.get("paths")
        if requested is None and payload.get("path"):
            requested = [payload.get("path")]
        if not isinstance(requested, list):
            raise ValueError("paths 必须是文件路径数组")
        if len(requested) > 500:
            raise ValueError("单次最多重试500个逻辑文件")
        inventory = _inventory_by_path(scan_result)
        paths = list(dict.fromkeys(
            str(path) for path in requested
            if str(path) in inventory
        ))
        if not paths:
            raise ValueError("没有选中当前数据包中的有效文件")
        changed = storage.request_file_reanalysis(
            scan_id, paths, reason=str(payload.get("reason") or "用户手动重试文件")
        )
        # Files without a previous analysis row are still valid manual
        # priorities; failed/partial rows receive the reset retry budget above.
        storage.prioritize_file_workflow_states(
            scan_id, paths, "manual_retry", "用户手动重试文件", score_boost=2500.0
        )
        batch_paths = _next_package_batch(scan_id, scan_result, preferred_paths=paths)
        if not batch_paths:
            return jsonify({
                "ok": True, "accepted": False, "retry_files": changed,
                "message": "所选文件当前没有可立即加入队列的项目",
                "processing": _package_processing_status(scan_id, scan_result),
            })
        storage.set_package_processing_state(scan_id, "running", "用户手动重试文件")
        job_id, created = storage.create_or_get_typed_job(
            scan_id, "analyze_package", options={
                "target_paths": batch_paths,
                "workflow_source": "manual_retry",
                "parse_mode": "accurate",
                "retry_failed": True,
            }, owner_id=_request_owner_id() or "legacy",
        )
        return jsonify({
            "ok": True, "accepted": True, "job_id": job_id,
            "reused_active_job": not created, "retry_files": changed,
            "batch_files": len(batch_paths),
            "status_url": "/api/jobs/{}".format(job_id),
            "processing": _package_processing_status(scan_id, scan_result),
        }), 202 if created else 200
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/tree/<scan_id>")
def get_tree_page(scan_id):
    """Load only one tree level so very large inventories never flood the UI."""
    if not storage.scan_owned(scan_id, owner_id=_request_owner_id()):
        return api_error("扫描任务不存在、已失效或不属于当前访问用户", 404)
    tree_kind = str(request.args.get("kind", "physical") or "physical").strip().lower()
    try:
        if tree_kind not in {"physical", "analysis"}:
            raise ValueError("未知目录树类型")
        offset = max(0, int(request.args.get("offset", 0)))
        limit = max(1, min(500, int(request.args.get("limit", 200))))
        node_key = request.args.get("node_key") or None
        tree_filter = str(request.args.get("filter", "all") or "all").strip().lower()
        indexed_kind = tree_kind
        if tree_kind == "analysis" and tree_filter not in {"all", "全部"}:
            if tree_filter not in {"low_confidence", "unclassified", "failed", "confirmed"}:
                raise ValueError("未知智能目录筛选条件")
            indexed_kind = "analysis:{}".format(tree_filter)
            if not node_key and not storage.tree_index_exists(scan_id, indexed_kind):
                full_analysis = storage.get_analysis(scan_id)
                if not full_analysis:
                    return api_error("智能目录尚未生成", 404)
                filtered_tree = filter_tree(full_analysis.get("analysis_tree") or {}, tree_filter)
                storage.refresh_tree_index(scan_id, indexed_kind, filtered_tree)
        node = storage.get_tree_page(
            scan_id, tree_kind=indexed_kind,
            node_key=node_key,
            offset=offset, limit=limit,
        )
        if not node:
            return api_error("目录节点不存在或尚未生成", 404)
        return jsonify({
            "ok": True, "tree_kind": tree_kind, "tree_filter": tree_filter, "node": node,
            "page": {
                "offset": node.get("_children_offset", offset),
                "limit": limit,
                "total": node.get("_children_total", 0),
                "next_offset": node.get("_children_next_offset"),
            },
        })
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/summaries/<scan_id>")
def get_summaries_page(scan_id):
    if not storage.scan_owned(scan_id, owner_id=_request_owner_id()):
        return api_error("扫描任务不存在、已失效或不属于当前访问用户", 404)
    try:
        page = storage.list_summaries_page(
            scan_id,
            offset=request.args.get("offset", 0),
            limit=request.args.get("limit", 200),
            node_path=request.args.get("path") if "path" in request.args else None,
            summary_type=request.args.get("type") or None,
        )
        return jsonify({"ok": True, **page})
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/file-conclusions/<scan_id>")
def get_file_conclusions(scan_id):
    """Return source-verified conclusions and original proof for one document."""
    if not storage.scan_owned(scan_id, owner_id=_request_owner_id()):
        return api_error("扫描任务不存在、已失效或不属于当前访问用户", 404)
    node_path = request.args.get("path") or "."
    summary_type = request.args.get("type") or "file"
    summary = storage.get_summary(scan_id, node_path, summary_type)
    if not summary:
        return api_error("该文件尚未生成摘要", 404)
    # Older stored summaries may still expose only ``conclusions`` and an
    # ``evidence_chain``.  Upgrade the single file on demand so the dedicated
    # conclusion API remains useful without performing a historical bulk
    # migration or forcing the user to rerun the entire package.
    if (
        (summary.get("summary_type") or summary_type) == "file"
        and summary.get("claim_contract") != "file-claims/1.0"
    ):
        document = storage.get_document(scan_id, node_path)
        if document:
            summary.update(build_file_claims(summary, document.get("evidence", [])))
            storage.save_summary(scan_id, node_path, "file", summary)
    return jsonify({
        "ok": True,
        "scan_id": scan_id,
        "path": node_path,
        # The storage key is the stage identity.  A preliminary/deep payload
        # still has the historical inner ``summary_type: file`` shape, so
        # return the requested staged key to keep frontend reads deterministic.
        "summary_type": summary_type if summary_type in {
            staged_summary_type("preliminary", "file"),
            staged_summary_type("deep", "file"),
        } else (summary.get("summary_type") or summary_type),
        "claim_contract": summary.get("claim_contract") or "file-claims/1.0",
        "file_conclusions": summary.get("file_conclusions") or [],
        "file_arguments": summary.get("file_arguments") or [],
        "file_review_items": summary.get("file_review_items") or [],
        "file_limitations": summary.get("file_limitations") or [],
        "evidence_quality": summary.get("evidence_quality") or {},
    })


@app.route("/api/analysis-node-members/<scan_id>")
def get_analysis_node_members(scan_id):
    """Page through a node's complete members for safe large-node editing."""
    if not storage.scan_owned(scan_id, owner_id=_request_owner_id()):
        return api_error("扫描任务不存在、已失效或不属于当前访问用户", 404)
    try:
        node = _find_analysis_node(scan_id, request.args.get("node_id"))
        paths = sorted(set(str(value) for value in node.get("member_paths") or [] if value))
        offset = max(0, int(request.args.get("offset", 0)))
        limit = max(1, min(500, int(request.args.get("limit", 500))))
        page = paths[offset:offset + limit]
        next_offset = offset + len(page)
        return jsonify({
            "ok": True, "node_id": node.get("node_id"), "member_count": len(paths),
            "members": [{"path": path, "name": Path(path).name or path} for path in page],
            "page": {
                "offset": offset,
                "limit": limit,
                "returned": len(page),
                "next_offset": next_offset if next_offset < len(paths) else None,
                "has_more": next_offset < len(paths),
            },
        })
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/analysis/<scan_id>")
def get_analysis(scan_id):
    try:
        compact_value = str(request.args.get("compact", "")).strip().lower()
        full_requested = str(request.args.get("full", "")).strip().lower() in {"1", "true", "yes"}
        if not full_requested and compact_value not in {"0", "false", "no"}:
            if not storage.scan_owned(scan_id, owner_id=_request_owner_id()):
                raise ValueError("扫描任务不存在、已失效或不属于当前访问用户")
            analysis = storage.get_analysis_overview(scan_id)
            if not analysis:
                return api_error("完整分析尚未完成", 404)
            analysis["analysis_tree"] = storage.get_tree_page(scan_id, "analysis", limit=100)
            return jsonify({
                "ok": True, "analysis": analysis, "tree_filter": "all",
                "tree_edits": storage.list_tree_edits(scan_id, _request_owner_id(), limit=500),
                "tree_edits_total": storage.tree_edit_count(scan_id, _request_owner_id()),
                "response_mode": "bounded",
            })
        require_scan(scan_id)
        analysis = storage.get_analysis(scan_id)
        if not analysis:
            return api_error("完整分析尚未完成", 404)
        tree_filter = request.args.get("filter", "all")
        if tree_filter and tree_filter.lower() not in {"all", "全部"}:
            analysis["analysis_tree"] = filter_tree(analysis.get("analysis_tree") or {}, tree_filter)
        return jsonify({"ok": True, "analysis": analysis, "tree_filter": tree_filter, "tree_edits": storage.list_tree_edits(scan_id, _request_owner_id())})
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/tree-edits/<scan_id>", methods=["GET", "POST"])
def tree_edits(scan_id):
    """Read or persist human review operations for the smart content tree."""
    try:
        require_scan(scan_id)
        owner_id = _request_owner_id() or "legacy"
        if request.method == "GET":
            try:
                edit_limit = max(1, min(500, int(request.args.get("limit", 500))))
            except (TypeError, ValueError):
                edit_limit = 500
            total = storage.tree_edit_count(scan_id, owner_id)
            edits = storage.list_tree_edits(scan_id, owner_id, limit=edit_limit)
            return jsonify({
                "ok": True, "edits": edits, "total": total,
                "truncated": total > len(edits), "limit": edit_limit,
            })
        payload = request.get_json(silent=True) or {}
        operation = str(payload.get("operation") or "").strip().lower()
        edit_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        allowed = {"rename", "confirm", "mount", "merge", "split", "undo", "redo"}
        if operation not in allowed:
            raise ValueError("不支持的目录树操作")
        analysis = storage.get_analysis(scan_id)
        if not analysis or not analysis.get("analysis_tree"):
            raise ValueError("完整分析尚未完成")
        tree = analysis["analysis_tree"]
        if operation in {"undo", "redo"}:
            target_id = str(edit_payload.get("edit_id") or "").strip()
            edits = storage.list_tree_edits(scan_id, owner_id)
            target = next((item for item in edits if str(item.get("edit_id") or "") == target_id), None)
            if not target or target.get("operation") in {"undo", "redo"}:
                raise ValueError("找不到可撤销或恢复的目录操作")
        node_ids = {node.get("node_id") for node in _walk_analysis_nodes(tree) if node.get("node_id")}
        documents = {item.get("path") for item in storage.list_documents(scan_id, hydrate=False)}
        if operation in {"rename", "confirm", "mount", "split"} and str(edit_payload.get("node_id") or "") not in node_ids:
            raise ValueError("目录树节点不存在")
        if operation == "mount":
            path = str(edit_payload.get("path") or "").replace("\\", "/")
            if path not in documents:
                raise ValueError("只能挂载已经完成解析的文件")
        if operation == "merge":
            ids = [str(value) for value in edit_payload.get("node_ids") or []]
            unique_ids = set(ids)
            if len(unique_ids) < 2 or not unique_ids.issubset(node_ids):
                raise ValueError("合并至少需要两个有效主题节点")
            same_parent = any(
                unique_ids.issubset({
                    str(child.get("node_id"))
                    for child in node.get("children") or []
                    if child.get("kind") == "group" and child.get("node_id")
                })
                for node in _walk_analysis_nodes(tree)
            )
            if not same_parent:
                raise ValueError("只能合并同一层级、同一父主题下的主题节点")
        if operation == "split":
            groups = edit_payload.get("groups") or []
            if not isinstance(groups, list) or len(groups) < 2:
                raise ValueError("拆分至少需要两个子主题")
            target = next(
                (node for node in _walk_analysis_nodes(tree) if node.get("node_id") == str(edit_payload.get("node_id") or "")),
                None,
            )
            original_paths = set(str(value) for value in (target or {}).get("member_paths") or [] if value)
            submitted_paths = [
                str(value)
                for group in groups if isinstance(group, dict)
                for value in group.get("paths") or [] if value
            ]
            if len(original_paths) > 500:
                raise ValueError("该主题文件过多，不能在可视化界面一次拆分；请先缩小范围")
            if len(submitted_paths) != len(set(submitted_paths)):
                raise ValueError("同一文件不能同时放入多个拆分主题")
            if set(submitted_paths) != original_paths:
                raise ValueError("拆分必须覆盖当前主题全部文件，不能遗漏或加入范围外文件")
        import uuid as _uuid
        edit_id = str(payload.get("edit_id") or _uuid.uuid4().hex)
        storage.save_tree_edit(scan_id, edit_id, operation, edit_payload, owner_id=owner_id)
        updated = storage.get_analysis(scan_id)
        if updated and updated.get("analysis_tree"):
            storage.refresh_tree_index(scan_id, "analysis", updated["analysis_tree"])
        compact = bool(payload.get("compact")) or str(request.args.get("compact", "")).lower() in {"1", "true", "yes"}
        response_analysis = updated
        if compact:
            response_analysis = storage.get_analysis_overview(scan_id) or {}
            response_analysis["analysis_tree"] = storage.get_tree_page(scan_id, "analysis", limit=100)
        edit_total = storage.tree_edit_count(scan_id, owner_id)
        edit_window = storage.list_tree_edits(scan_id, owner_id, limit=500)
        return jsonify({
            "ok": True,
            "edit": {"edit_id": edit_id, "operation": operation, "payload": edit_payload},
            "edits": edit_window,
            "tree_edits_total": edit_total,
            "tree_edits_truncated": edit_total > len(edit_window),
            "analysis": response_analysis,
        })
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/document/<scan_id>/raw")
def get_raw_document(scan_id):
    """Stream the original source file for reading or download."""
    try:
        scan = require_scan(scan_id)
        node_path = str(request.args.get("path") or "").strip()
        if not node_path:
            raise ValueError("缺少文件路径")
        physical_path = node_path.split("::", 1)[0]
        resolve_under(scan["root"], physical_path)
        source_path = Path(scan["root"]) / physical_path
        if not source_path.is_file():
            raise ValueError("原始文件不存在或尚未可访问")
        response = send_from_directory(
            str(source_path.parent), source_path.name,
            as_attachment=str(request.args.get("download") or "").lower() in {"1", "true", "yes"},
        )
        # 原文件阅读器会在当前站点内嵌 PDF/图片。全局安全头默认 DENY，
        # 会让浏览器提示“已阻止该页面的显示”；原文件只允许同源页面嵌入。
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        return response
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/document/<scan_id>")
def get_document(scan_id):
    def document_error(message, status):
        # Flask accepts ``(response, status)`` from a route, but callers of
        # this document endpoint also use it as a focused source resolver.
        # Return a concrete Response so both paths expose the same status.
        response, response_status = api_error(message, status)
        response.status_code = response_status
        return response

    try:
        scan = require_scan(scan_id)
        node_path = str(request.args.get("path", "") or "").strip()
        if not node_path:
            raise ValueError("缺少文件路径")
        logical_source = storage.get_inventory_entry(scan_id, node_path) or {}
        stored_document = storage.get_document(scan_id, node_path)
        preview = storage.get_file_preview(scan_id, node_path)
        coverage = (stored_document or {}).get("coverage") or {}
        parser_name = str(((stored_document or {}).get("parser") or {}).get("name") or "")
        is_deep_document = bool(stored_document) and (
            bool(coverage.get("semantic_complete") or coverage.get("deep_parse_complete"))
            or (
                bool(coverage.get("complete"))
                and not bool(coverage.get("preview_only") or coverage.get("semantic_projection"))
                and parser_name != "bounded-package-preview"
            )
        )
        # A saved exploration projection is searchable, but it is not an
        # authoritative full document. Prefer the original persisted preview
        # so callers can show its sampling boundaries honestly.
        if is_deep_document:
            document = stored_document
            analysis_level = "deep"
        elif preview:
            document = preview_as_document(preview)
            analysis_level = "preview"
        elif stored_document:
            document = stored_document
            analysis_level = "preview"
        else:
            document = None
            analysis_level = "metadata"
        member_evidence = None
        # A logical unit is stored under its stable ``container::member`` path.
        # Older scans may only have the parsed container document, so project
        # the matching member evidence without pretending the container text is
        # the member's own full text.
        if "::" in node_path:
            container_path, member_name = node_path.split("::", 1)
            is_known_logical = bool(
                logical_source.get("logical_unit")
                and str(logical_source.get("container_path") or "") == container_path
                # Archive members record ``member_name`` while JSONL and
                # worksheet partitions use their virtual path as identity.
                # Requiring a missing member_name to equal the suffix made a
                # known structured partition look like a forged path.
                and (
                    not str(logical_source.get("member_name") or "")
                    or str(logical_source.get("member_name") or "") == member_name
                )
            )
            if not document:
                container_document = storage.get_document(scan_id, container_path)
                candidates = [
                    dict(item) for item in (container_document or {}).get("evidence") or []
                    if isinstance(item, dict)
                    and (
                        str(item.get("source_path") or "") == node_path
                        or (
                            str(item.get("archive_source_path") or "") == container_path
                            and str(item.get("archive_member") or "") == member_name
                        )
                    )
                ]
                if not is_known_logical and not candidates:
                    return document_error("逻辑文件不在当前数据包清单中", 404)
                if container_document and candidates:
                    document = dict(container_document)
                    document["text"] = "\n".join(
                        str(item.get("text") or "") for item in candidates
                    )
                    document["evidence"] = candidates
                    document.setdefault("coverage", {})["complete"] = False
                    document.setdefault("warnings", []).append(
                        "此为压缩包成员的兼容投影，仅包含已建立索引的成员证据。"
                    )
                    member_evidence = candidates
                    analysis_level = "preview"
        if not document and logical_source:
            # A known logical unit can be opened before its preview/deep pass
            # completes. Return its exact container and partition coordinates
            # rather than incorrectly claiming it does not exist.
            document = {
                "schema_version": "unified-document/1.0",
                "source": {"path": node_path, "name": logical_source.get("name")},
                "parser": {"name": "inventory-metadata", "mode": "metadata"},
                "structure": {"title": logical_source.get("name") or Path(node_path).name, "headings": []},
                "coverage": {"complete": False, "parse_complete": False, "semantic_complete": False},
                "warnings": ["该逻辑文件已进入清单，正文预览或深度解析尚未完成。"],
                "text": "", "evidence": [],
            }
        if not document:
            return document_error("文件不在当前数据包清单中且没有可回查结果", 404)
        source = dict(document.get("source") or {})
        if "::" in node_path:
            container_path, member_name = node_path.split("::", 1)
            source.update({
                "path": node_path,
                "logical_path": node_path,
                "physical_path": container_path,
                "container_path": container_path,
                "logical_member": member_name,
                "archive_member": member_name,
                "logical_unit": True,
            })
        else:
            source.setdefault("physical_path", source.get("path") or node_path)

        location = {
            key: logical_source.get(key)
            for key in (
                "logical_kind", "member_name", "byte_start", "byte_end",
                "record_boundary", "sheet_name", "row_start", "row_end",
                "partition_index", "actual_row_count",
            )
            if logical_source.get(key) is not None
        }

        def focus_value(name):
            value = request.args.get(name)
            if value in {None, ""}:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ValueError("定位参数 {} 必须为整数".format(name))

        focus = {
            "page": focus_value("page"),
            "paragraph_index": focus_value("paragraph_index"),
            "block_index": focus_value("block_index"),
            "char_start": focus_value("char_start"),
            "char_end": focus_value("char_end"),
            "section": str(request.args.get("section") or "").strip() or None,
        }
        evidence_items = member_evidence if member_evidence is not None else list(document.get("evidence") or [])
        if any(value is not None for value in focus.values()):
            def focus_rank(item):
                matches = sum(
                    1 for key, value in focus.items()
                    if value is not None and str(item.get(key) or "") == str(value)
                )
                return -matches
            evidence_items.sort(key=focus_rank)
        selected_evidence = select_evidence(
            evidence_items,
            topics=document.get("structure", {}).get("headings", [])[:8] + [document.get("structure", {}).get("title")],
            max_items=12,
            per_source=12,
            max_chars=520,
        )
        # A document view is an evidence browser, not a claim answer.  Strict
        # topic ranking may legitimately reject a short member/partition
        # excerpt whose text does not repeat the file title.  In that case the
        # exact stored evidence must remain visible and clearly be marked as a
        # direct, unranked source excerpt instead of presenting an empty card.
        if not selected_evidence and evidence_items:
            selected_evidence = []
            for item in evidence_items:
                if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                    continue
                compact = dict(item)
                compact["text"] = str(compact.get("text") or "")[:520]
                compact["selection_fallback"] = "direct_source_excerpt"
                selected_evidence.append(compact)
                if len(selected_evidence) >= 12:
                    break
        return jsonify({"ok": True, "document": {
            "schema_version": document.get("schema_version"),
            "source": source,
            "source_reference": {
                "logical_path": source.get("logical_path") or source.get("path"),
                "physical_path": source.get("physical_path") or source.get("path"),
                "container_path": source.get("container_path"),
                "logical_member": source.get("logical_member") or source.get("archive_member"),
                "location": location,
            },
            "analysis_level": analysis_level,
            "focus": focus,
            "parser": document.get("parser"),
            "structure": document.get("structure"),
            "coverage": document.get("coverage", {}),
            "data_profile": document.get("data_profile"),
            "data_profiles": document.get("data_profiles", []),
            "warnings": document.get("warnings", []),
            "text_preview": document.get("text", "")[:5000],
            "text_content": (document.get("text", "") if str(request.args.get("full") or "").lower() in {"1", "true", "yes"} else None),
            "text_char_count": len(str(document.get("text", "") or "")),
            "evidence": selected_evidence,
            "evidence_count": len(evidence_items),
        }})
    except ValueError as exc:
        message = str(exc)
        return document_error(message, 404 if "扫描任务不存在" in message else 400)


class _ConversationTranslationAdapter:
    def __init__(self):
        self._cache = {}

    def translate(self, text, source_language=None, target_language="zh-CN", context=None):
        key = (str(source_language or ""), str(target_language or "zh-CN"), str(text or ""))
        if key in self._cache:
            return self._cache[key]
        document = {
            "source": {"path": "conversation-evidence"},
            "structure": {"title": ""},
            "text": str(text or ""),
        }
        state = _translate_document_serialized(document)
        if state.get("status") not in {"completed", "not_required"}:
            message = (state.get("errors") or [{}])[0].get("message") or "证据翻译失败"
            raise ValueError(message)
        translated = state.get("translated_text")
        if translated:
            if len(self._cache) >= 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = translated
        return translated


def _conversation_projection_fallback(retrieval_request):
    """Build a bounded temporary corpus when a historical index is absent.

    A missing or rebuilding durable index must not turn already-persisted
    documents into an apparent "no result".  This fallback deliberately uses
    Storage's bounded document projection, never a document sidecar's full
    body, and advertises its reduced coverage to the conversation runtime.
    """
    chunk_limit = max(
        100, min(5000, int(Config.CONVERSATION_MAX_CANDIDATE_EVIDENCE))
    )
    # A projection normally contributes several evidence chunks.  Keeping a
    # separate document ceiling guarantees one request cannot hydrate an
    # unbounded historical package just because its search index was deleted.
    document_limit = max(
        50,
        min(
            int(getattr(Config, "CONVERSATION_RETRIEVAL_DOCUMENT_LIMIT", 500)),
            chunk_limit // 2,
        ),
    )
    deadline = time.monotonic() + float(
        getattr(Config, "CONVERSATION_RETRIEVAL_TIMEOUT_SECONDS", 8.0)
    )
    scope = retrieval_request.scope
    chunks = []
    paths = []
    scanned_documents = 0
    exhausted = False
    for item in storage.iter_documents(
        retrieval_request.scan_id, hydrate=False, batch_size=50
    ):
        if time.monotonic() >= deadline:
            exhausted = True
            break
        path = str(item.get("path") or "")
        if not path or not scope.contains_source(path):
            continue
        if scanned_documents >= document_limit:
            exhausted = True
            break
        scanned_documents += 1
        document = dict(item.get("payload") or {})
        source = dict(document.get("source") or {})
        source.setdefault("path", path)
        document["source"] = source
        for evidence in evidence_corpus({path: document}, scope="."):
            projection = dict(evidence)
            # The source is a bounded semantic projection rather than a full
            # reparse.  The normal result renderer and verifier will preserve
            # that distinction instead of overstating the evidence quality.
            projection["preview_only"] = True
            projection["index_kind"] = "projection_fallback"
            projection["projection_fallback"] = True
            chunks.append(projection)
            if len(chunks) >= chunk_limit:
                exhausted = True
                break
        if path not in paths:
            paths.append(path)
        if exhausted:
            break
    return chunks, {
        "used": bool(chunks),
        "documents_considered": scanned_documents,
        "document_limit": document_limit,
        "evidence_limit": chunk_limit,
        "truncated": exhausted,
        "paths": paths,
    }


def _conversation_retrieve(retrieval_request):
    started_at = time.monotonic()
    cache_key = (
        str(retrieval_request.scan_id),
        str(retrieval_request.query or "").strip().casefold(),
        json.dumps(
            retrieval_request.scope.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        tuple(sorted(retrieval_request.scope.source_paths or ())),
        str(retrieval_request.intent or ""),
    )
    with _CONVERSATION_RETRIEVAL_CACHE_LOCK:
        cached = _CONVERSATION_RETRIEVAL_CACHE.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < _CONVERSATION_RETRIEVAL_CACHE_TTL:
            return copy.deepcopy(cached[1])
    query = retrieval_request.query
    scope = retrieval_request.scope
    if scope.kind in {"topic", "entity", "file_type"}:
        query = "{} {}".format(scope.value, query)
    elif scope.kind == "time":
        query = "{} {} {}".format(
            (scope.value or {}).get("start", ""), (scope.value or {}).get("end", ""), query
        )
    source_paths = list(scope.source_paths) or None
    candidate_limit = int(
        getattr(Config, "CONVERSATION_RETRIEVAL_CANDIDATE_LIMIT", 600)
    )
    indexed = storage.search_evidence_index(
        retrieval_request.scan_id,
        query,
        scope=scope.retrieval_path,
        source_paths=source_paths,
        limit=candidate_limit,
    )
    projection_fallback = None
    # Historical scans can retain durable documents after their original
    # directory disappears.  If their old evidence index is absent, search
    # the bounded persisted projections rather than rejecting the question or
    # falsely reporting that the package has no relevant material.
    if not indexed and not storage.count_evidence_index(retrieval_request.scan_id):
        indexed, projection_fallback = _conversation_projection_fallback(
            retrieval_request
        )
    if scope.kind == "file_type" and (scope.constraints or {}).get("dimension") == "format":
        expected = str(scope.value or "").lower().strip()
        if expected and not expected.startswith(".") and expected != "[无扩展名]":
            expected = "." + expected
        indexed = [
            item for item in indexed
            if (
                (not Path(str(item.get("archive_source_path") or item.get("source_path") or "").split("::", 1)[0]).suffix.lower() and expected == "[无扩展名]")
                or Path(str(item.get("archive_source_path") or item.get("source_path") or "").split("::", 1)[0]).suffix.lower() == expected
            )
        ]
    result = retrieve_evidence(
        {}, query, top_k=retrieval_request.top_k, indexed_chunks=indexed,
    )
    # Evidence from archive members and structured-file partitions is stored
    # under the logical source path (for example
    # ``bundle.zip::letters/a.txt``).  Do not collapse that back to its
    # physical container here: the member has its own parse state, coverage
    # and retry lifecycle.  Keeping both identities lets citations point to
    # the real source while still exposing the outer file as provenance.
    candidate_paths = []
    physical_candidate_paths = []
    physical_by_evidence_path = {}
    for item in indexed:
        evidence_path = str(item.get("source_path") or "")
        physical_path = str(
            item.get("archive_source_path") or evidence_path.split("::", 1)[0]
        )
        if evidence_path and evidence_path not in candidate_paths:
            candidate_paths.append(evidence_path)
        if evidence_path:
            physical_by_evidence_path.setdefault(evidence_path, physical_path)
        if physical_path and physical_path not in physical_candidate_paths:
            physical_candidate_paths.append(physical_path)
    states = storage.get_file_states(
        retrieval_request.scan_id,
        candidate_paths + physical_candidate_paths,
    )

    def state_for_evidence_path(evidence_path):
        """Prefer a logical member state, then preserve legacy container state.

        New imports always have a logical-unit row, but historical archive
        analyses stored only the physical container.  The fallback keeps those
        durable results usable without claiming that a missing member record is
        an unprocessed file.
        """
        evidence_path = str(evidence_path or "")
        physical_path = physical_by_evidence_path.get(
            evidence_path, evidence_path.split("::", 1)[0]
        )
        return states.get(evidence_path) or states.get(physical_path) or {}

    fallback_paths = set((projection_fallback or {}).get("paths") or [])
    for item in result.get("results") or []:
        evidence_path = str(item.get("source_path") or "")
        physical_path = str(
            item.get("archive_source_path") or evidence_path.split("::", 1)[0]
        )
        state = state_for_evidence_path(evidence_path)
        preview = (
            storage.get_file_preview(retrieval_request.scan_id, evidence_path)
            or storage.get_file_preview(retrieval_request.scan_id, physical_path)
            or {}
        )
        item["source_language"] = (preview.get("language") or {}).get("code")
        item["analysis_level"] = (
            "deep" if state.get("status") == "completed" else
            "projection" if item.get("projection_fallback") else "preview"
        )
        item["logical_source_path"] = evidence_path if "::" in evidence_path else None
        item["physical_source_path"] = physical_path or evidence_path
    deep_candidates = sum(
        1 for path in candidate_paths
        if state_for_evidence_path(path).get("status") == "completed"
    )
    indexed_only_candidates = sum(
        1 for path in candidate_paths
        if (
            path in fallback_paths
            or (
                state_for_evidence_path(path).get("status") != "completed"
                and state_for_evidence_path(path).get("retryable") is not None
                and not bool(state_for_evidence_path(path).get("retryable"))
            )
        )
    )
    deferred = [
        path for path in candidate_paths
        if (
            state_for_evidence_path(path).get("status") != "completed"
            and path not in fallback_paths
            and not (
                state_for_evidence_path(path).get("retryable") is not None
                and not bool(state_for_evidence_path(path).get("retryable"))
            )
        )
    ]
    analysis = storage.get_analysis_overview(retrieval_request.scan_id) or {}
    package_coverage = analysis.get("coverage") or {}
    physical_inventory_files = int(package_coverage.get("inventory_files") or 0)
    status_counts = storage.file_status_counts(retrieval_request.scan_id)
    # The state projection excludes archive containers that were replaced by
    # independently schedulable members.  This is the denominator a user
    # needs when judging whether an answer covered a large package.
    total_files = int(status_counts.get("total") or physical_inventory_files)
    scope_extension = scope.value if scope.kind == "file_type" else None
    scope_files = storage.count_inventory_files(
        retrieval_request.scan_id,
        scope=scope.retrieval_path,
        source_paths=list(scope.source_paths) or None,
        extension=scope_extension,
    )
    if scope.kind == "package":
        scope_files = total_files
    elif not scope_files and scope.kind in {"topic", "entity", "time"}:
        scope_files = total_files
    inspected_paths = {
        str(item.get("source_path") or item.get("archive_source_path") or "")
        for item in result.get("results") or []
    }
    inspected_paths.discard("")
    inspected_files = len(inspected_paths)
    candidate_deep_coverage = round(
        deep_candidates / float(len(candidate_paths) or 1), 6
    )
    candidate_evidence_coverage = round(
        (deep_candidates + indexed_only_candidates)
        / float(len(candidate_paths) or 1), 6
    )
    scope_inspection_coverage = round(
        inspected_files / float(scope_files or 1), 6
    )
    broad_modes = {
        "summary", "comparison", "timeline", "relationship", "contradiction",
        "risk", "multi_task",
    }
    broad_scope = retrieval_request.intent in broad_modes
    query_coverage = (
        scope_inspection_coverage if broad_scope else candidate_evidence_coverage
    )
    if projection_fallback and projection_fallback.get("used"):
        # Projected historical text is useful enough for a cautious answer,
        # but it is not equivalent to a complete durable evidence index.
        query_coverage = min(query_coverage, 0.35)
        result["warnings"] = list(dict.fromkeys(
            list(result.get("warnings") or []) + [
                "当前回答使用已保存文档的有界语义投影；完整证据索引仍在缺失或重建中，结论只能作为阶段性结果。"
            ]
        ))
    result["coverage"] = {
        "total_files": total_files or None,
        "physical_inventory_files": physical_inventory_files or None,
        "scope_files": scope_files or None,
        "searchable_files": package_coverage.get("parsed_files"),
        "deep_analyzed_files": package_coverage.get("deep_analyzed_files"),
        "candidate_files": len(candidate_paths),
        "candidate_physical_files": len(physical_candidate_paths),
        "inspected_files": inspected_files,
        "retrieved_files": inspected_files,
        "deep_candidate_files": deep_candidates,
        "indexed_only_candidate_files": indexed_only_candidates,
        "candidate_deep_coverage": candidate_deep_coverage,
        "candidate_evidence_coverage": candidate_evidence_coverage,
        "scope_inspection_coverage": scope_inspection_coverage,
        "coverage_basis": "scope_inspection" if broad_scope else "candidate_depth",
        "query_coverage": query_coverage,
        "deferred_candidates": deferred[:100],
        "projection_fallback": projection_fallback,
    }
    result["needs_promotion"] = bool(deferred)
    if time.monotonic() - started_at >= float(
        getattr(Config, "CONVERSATION_RETRIEVAL_TIMEOUT_SECONDS", 8.0)
    ):
        result["warnings"] = list(dict.fromkeys(
            list(result.get("warnings") or [])
            + ["检索达到交互式时间预算，已返回当前最高相关证据；可继续追问以扩大范围。"]
        ))
        result["coverage"]["retrieval_time_budget_exceeded"] = True
    with _CONVERSATION_RETRIEVAL_CACHE_LOCK:
        _CONVERSATION_RETRIEVAL_CACHE[cache_key] = (time.monotonic(), copy.deepcopy(result))
        while len(_CONVERSATION_RETRIEVAL_CACHE) > _CONVERSATION_RETRIEVAL_CACHE_MAX:
            _CONVERSATION_RETRIEVAL_CACHE.pop(next(iter(_CONVERSATION_RETRIEVAL_CACHE)))
    return result


def _conversation_structured(request_data):
    scope = request_data.scope
    documents = []
    for item in storage.iter_structured_documents(
        request_data.scan_id,
        hydrate=False,
        batch_size=100,
        source_paths=list(scope.source_paths) or None,
    ):
        path = item.get("path")
        if not scope.contains_source(path):
            continue
        document = item.get("payload") or {}
        if document.get("data_profile") or document.get("data_profiles"):
            documents.append({"path": path, "payload": document})
    result = answer_question(request_data.question, documents)
    result_coverage = result.get("coverage") or {}
    if result_coverage.get("complete") is False:
        source_paths = list(result.get("source_paths") or [])
        states = storage.get_file_states(request_data.scan_id, source_paths)
        promotion_candidates = [
            path for path in source_paths
            if (states.get(path) or {}).get("status") != "completed"
        ]
        if promotion_candidates:
            result["promotion_candidates"] = promotion_candidates[:24]
            result["needs_promotion"] = True
    analysis = storage.get_analysis_overview(request_data.scan_id) or {}
    result.setdefault("coverage", (analysis.get("coverage") or {}).get("semantic_analysis_coverage") or {})
    return result


conversation_engine = ConversationEngine(
    retriever=CallableEvidenceRetriever(_conversation_retrieve),
    answer_model=llm_transport if llm_generation_enabled else None,
    structured_qa=CallableStructuredQA(_conversation_structured),
    translator=_ConversationTranslationAdapter() if Config.ENABLE_TRANSLATION else None,
)


def _conversation_index_status(scan_id):
    documents = storage.count_documents(scan_id)
    evidence = storage.count_evidence_index(scan_id)
    preview_counts = storage.file_preview_counts(scan_id)
    previews = sum(int(value or 0) for value in preview_counts.values())
    durable = storage.get_search_index_state(scan_id)
    if durable:
        expected = int(durable.get("expected_documents") or 0)
        processed = int(durable.get("processed_documents") or 0)
        failed = int(durable.get("failed_documents") or 0)
        ready = bool(
            durable.get("status") == "ready"
            and expected == documents
            and processed == expected
            and not failed
        )
        state = "ready" if ready else str(durable.get("status") or "rebuild_required")
    else:
        # Existing releases had no generation ledger.  Preserve access to a
        # populated legacy index, while every future rebuild uses the strict
        # state machine below.
        ready = not bool(documents and not evidence)
        state = "legacy_ready" if ready and evidence else (
            "ready" if ready else "rebuild_required"
        )
        expected = documents
        processed = documents if ready else 0
        failed = 0
    # ``ready`` intentionally remains strict: it means the durable index has
    # fully caught up.  ``usable`` is a different user-facing guarantee: a
    # partial index or persisted document projections can still support a
    # cautious, coverage-labelled answer while a rebuild continues.
    usable = bool(evidence or documents or previews)
    return {
        "ready": ready,
        "usable": usable,
        "partial": bool(usable and not ready),
        "state": state,
        "documents": documents,
        "evidence_records": evidence,
        "previews": previews,
        "preview_status_counts": preview_counts,
        "generation": (durable or {}).get("generation") if durable else "legacy",
        "expected_documents": expected,
        "processed_documents": processed,
        "failed_documents": failed,
        "empty_documents": int((durable or {}).get("empty_documents") or 0),
        "started_at": (durable or {}).get("started_at"),
        "updated_at": (durable or {}).get("updated_at"),
        "completed_at": (durable or {}).get("completed_at"),
        "error": (durable or {}).get("error"),
    }


def _run_claimed_search_index_rebuild_job(job):
    """Rebuild conversational evidence from durable documents only.

    Historical scans may outlive their source directory.  Re-indexing their
    stored unified documents keeps migration independent of source-file I/O.
    """
    scan_id = str(job.get("scan_id") or "")
    require_scan(scan_id)
    total = storage.count_documents(scan_id)
    state = storage.get_search_index_state(scan_id)
    if not state or state.get("status") not in {"rebuilding", "interrupted"}:
        state = storage.begin_search_index_rebuild(scan_id, total)
    elif state.get("status") == "interrupted":
        state = storage.begin_search_index_rebuild(scan_id, total)
    processed = int(state.get("processed_documents") or 0)
    resume_after = str(state.get("last_node_path") or "") or None
    indexed = 0
    empty_documents = int(state.get("empty_documents") or 0)
    last_path = resume_after
    try:
        for item in storage.iter_documents(
            scan_id, hydrate=True, batch_size=20, start_after=resume_after
        ):
            _ensure_job_active(job["id"])
            path = str(item.get("path") or "")
            document = dict(item.get("payload") or {})
            chunks = evidence_corpus({path: document})
            indexed += storage.replace_document_evidence_index(
                scan_id, path, chunks, preserve_translations=True
            )
            processed += 1
            last_path = path
            if not chunks:
                empty_documents += 1
            if processed % 20 == 0 or processed == total:
                storage.checkpoint_search_index_rebuild(
                    scan_id, processed, empty_documents, last_path
                )
                progress = 100 if not total else min(
                    99, max(1, int(processed * 100 / total))
                )
                storage.update_job(
                    job["id"],
                    progress=progress,
                    stage="rebuilding_search_index",
                    message="正在从历史文档重建证据索引：{}/{}".format(
                        processed, total
                    ),
                    current_stage="历史文档证据索引重建",
                    current_file=path,
                    heartbeat=True,
                )
        storage.checkpoint_search_index_rebuild(
            scan_id, processed, empty_documents, last_path
        )
        storage.complete_search_index_rebuild(
            scan_id, processed, empty_documents, last_path
        )
    except Exception as exc:
        storage.interrupt_search_index_rebuild(scan_id, exc)
        raise
    status = _conversation_index_status(scan_id)
    return {
        "scan_id": scan_id,
        "processed_documents": processed,
        "indexed_evidence_records": indexed,
        "empty_documents": empty_documents,
        "search_index": status,
    }


def _run_claimed_conversation_turn_job(job):
    """Execute one durable analysis turn without holding a Web request open."""
    options = dict(job.get("options") or {})
    turn_id = str(options.get("turn_id") or "")
    turn = storage.get_conversation_turn(turn_id, owner_id=job.get("owner_id"))
    if not turn:
        raise ValueError("交互式分析轮次不存在或不属于当前用户")
    if turn.get("status") == "completed":
        return {
            "turn_id": turn_id,
            "status": "already_completed",
            "verification_status": (turn.get("verification") or {}).get("status"),
        }
    if turn.get("status") == "cancelled":
        raise JobCancelled("本轮分析已经取消")
    _ensure_job_active(job["id"])
    stored = storage.get_conversation(
        turn["session_id"], turn["owner_id"], scan_id=turn["scan_id"],
        message_limit=500,
        context_only=True,
    )
    if not stored:
        raise ValueError("交互式分析会话不存在")
    # The durable user message and assistant placeholder already belong to this
    # turn. Excluding them prevents the engine from treating the same question
    # as a follow-up and prevents automatic continuation from duplicating it.
    stored = dict(stored)
    stored["messages"] = [
        message for message in stored.get("messages") or []
        if str((message.get("metadata") or {}).get("turn_id") or "") != turn_id
    ]
    session = ConversationSession.from_dict(stored)
    scope = ConversationScope.from_dict(turn.get("scope") or session.scope.as_dict())
    scan_result = require_scan(turn["scan_id"])
    turn["job_id"] = job["id"]
    storage.update_conversation_turn(turn_id, job_id=job["id"])
    result = AnalysisTurnRuntime(
        storage,
        conversation_engine,
        batch_size=Config.CONVERSATION_ANALYSIS_BATCH_FILES,
        max_candidate_evidence=Config.CONVERSATION_MAX_CANDIDATE_EVIDENCE,
        max_revision_attempts=Config.CONVERSATION_MAX_REVISION_ATTEMPTS,
        cancel_check=lambda: _ensure_job_active(job["id"]),
    ).execute(
        turn, session, scope, inventory_paths=list(_inventory_by_path(scan_result)),
    )
    _ensure_job_active(job["id"])
    return result


def _resume_conversation_turn_after_promotion(job):
    options = dict(job.get("options") or {})
    turn_id = str(options.get("conversation_turn_id") or "")
    if not turn_id:
        return None
    turn = storage.get_conversation_turn(turn_id, owner_id=job.get("owner_id"))
    if not turn:
        return {"status": "turn_missing", "turn_id": turn_id}
    queued_job_id = storage.replace_conversation_turn_job(
        turn_id, job.get("owner_id") or "legacy",
        message="补充深析完成，继续执行原分析任务",
    )
    return {
        "status": "resumed" if queued_job_id else "not_resumed",
        "turn_id": turn_id,
        "job_id": queued_job_id,
    }


def _continue_conversation_after_promotion(job, analysis, scan_result):
    """Idempotently resume a question after its promoted files are parsed."""
    options = dict(job.get("options") or {})
    session_id = str(options.get("conversation_session_id") or "")
    question = str(options.get("conversation_question") or "").strip()
    trigger_message_id = str(options.get("conversation_trigger_message_id") or "")
    if not session_id or not question or not trigger_message_id:
        return None
    owner_id = job.get("owner_id") or "legacy"
    stored = storage.get_conversation(session_id, owner_id, scan_id=job.get("scan_id"))
    if not stored:
        return {"status": "conversation_missing", "session_id": session_id}
    session = ConversationSession.from_dict(stored)
    for message in session.messages:
        if (message.metadata or {}).get("automatic_continuation_of") == trigger_message_id:
            return {
                "status": "already_continued", "session_id": session_id,
                "message_id": message.message_id,
            }

    scope = ConversationScope.from_dict(options.get("conversation_scope") or session.scope.as_dict())
    turn = conversation_engine.ask(
        session, question, scope=scope,
        coverage=(analysis or {}).get("coverage") or {},
        persist_scope=False,
    )
    for message in session.messages:
        if message.message_id == turn.get("message_id"):
            message.metadata["automatic_continuation_of"] = trigger_message_id
            message.metadata["promotion_job_id"] = job.get("id")
            break
    turn["automatic_continuation_of"] = trigger_message_id

    next_job_id = None
    depth = max(0, int(options.get("conversation_continuation_depth") or 0))
    promotion = turn.get("promotion_request") or {}
    if promotion.get("required") and depth < 3:
        inventory_paths = set(_inventory_by_path(scan_result))
        current_targets = set(str(path) for path in options.get("target_paths") or [])
        requested = [
            path for path in promotion.get("candidate_paths") or []
            if path in inventory_paths
            and path not in current_targets
            and (storage.get_file_state(job.get("scan_id"), path) or {}).get("status") != "completed"
        ][: max(1, int(promotion.get("desired_file_count") or 12))]
        if requested:
            next_job_id, _created = storage.create_or_get_typed_job(
                job.get("scan_id"), "analyze_package",
                options={
                    "target_paths": requested,
                    "workflow_source": "question_promotion",
                    "scope_label": "问答补充深析：{}".format(question[:120]),
                    "parse_mode": "accurate",
                    "conversation_session_id": session_id,
                    "conversation_question": question,
                    "conversation_scope": scope.as_dict(),
                    "conversation_trigger_message_id": turn.get("message_id"),
                    "conversation_continuation_depth": depth + 1,
                },
                owner_id=owner_id,
            )
            turn["promotion_job_id"] = next_job_id
            turn["status"] = "promotion_queued"

    storage.save_conversation(session.as_dict(), owner_id)
    return {
        "status": "continued",
        "session_id": session_id,
        "message_id": turn.get("message_id"),
        "answer_status": turn.get("status"),
        "next_promotion_job_id": next_job_id,
    }


def _scope_paths_from_storage(scan_id, scan_result, kind, value, constraints):
    """Resolve a logical overview selection to every matching physical file."""
    constraints = dict(constraints or {})
    inventory = _inventory_by_path(scan_result)
    records = {path: dict(node or {}) for path, node in inventory.items()}
    for item in storage.iter_file_previews(scan_id):
        path = str(item.get("path") or "")
        if path:
            records.setdefault(path, {}).update(item.get("payload") or {})
    for item in storage.iter_documents(scan_id, hydrate=False, batch_size=200):
        path = str(item.get("path") or "")
        if path:
            records.setdefault(path, {}).update(item.get("payload") or {})

    def values(source):
        output = []
        stack = [source]
        while stack and len(output) < 512:
            current = stack.pop()
            if isinstance(current, dict):
                stack.extend(current.values())
            elif isinstance(current, (list, tuple, set)):
                stack.extend(current)
            elif current is not None:
                output.append(str(current))
        return output

    language_aliases = {
        "中文": "zh", "英语": "en", "日语": "ja", "韩语": "ko",
        "法语": "fr", "德语": "de", "西班牙语": "es", "俄语": "ru",
        "阿拉伯语": "ar",
    }
    expected = str(value or "").casefold()
    dimension = str(constraints.get("dimension") or "")
    if kind == "time":
        start = str((value or {}).get("start") or "")[:4]
        end = str((value or {}).get("end") or "")[:4]
        start_year = int(start) if start.isdigit() else 0
        end_year = int(end) if end.isdigit() else 9999

    matched = []
    for path, record in records.items():
        classification = record.get("classification") or {}
        if kind == "topic":
            candidates = values([
                record.get("keywords"), record.get("topics"), record.get("content_topics"),
                classification.get("primary_topic"), classification.get("topic_memberships"),
            ])
            match = expected in {item.casefold() for item in candidates}
        elif kind == "entity":
            candidates = values([record.get("entities"), record.get("named_entities")])
            match = expected in {item.casefold() for item in candidates}
        elif kind == "time":
            candidates = values([
                record.get("modified_at"), record.get("dates"), record.get("document_date"),
                record.get("temporal"), (record.get("metadata") or {}).get("document_date"),
            ])
            years = {
                int(match.group(1)) for candidate in candidates
                for match in [re.search(r"(?<!\d)(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})(?!\d)", candidate)]
                if match
            }
            match = any(start_year <= year <= end_year for year in years)
        elif kind == "file_type" and dimension == "format":
            extension = str(record.get("extension") or Path(path.split("::", 1)[0]).suffix).lower()
            wanted = expected if expected.startswith(".") or expected == "[无扩展名]" else "." + expected
            match = (extension or "[无扩展名]") == wanted
        elif kind == "file_type" and dimension == "document_type":
            candidate = str(record.get("document_type") or classification.get("document_type") or "")
            match = candidate.casefold() == expected
        elif kind == "file_type" and dimension == "language":
            candidates = values([record.get("language"), record.get("languages")])
            wanted = language_aliases.get(str(value or ""), str(value or "")).casefold()
            match = wanted in {item.casefold() for item in candidates}
        else:
            match = False
        if match:
            matched.append(path)
    return sorted(set(matched))


def _resolved_conversation_scope(scan_id, scan_result, payload):
    payload = dict(payload or {})
    kind = str(payload.get("kind") or "package").lower()
    source_paths = list(payload.get("source_paths") or [])
    constraints = dict(payload.get("constraints") or {})
    if kind == "topic" and constraints.get("node_id") and not source_paths:
        node = _find_analysis_node(scan_id, constraints["node_id"])
        source_paths = list(node.get("member_paths") or [])
        payload["label"] = payload.get("label") or node.get("name")
        payload["value"] = payload.get("value") or node.get("name")
    elif kind in {"topic", "entity", "time", "file_type"} and (
        not source_paths or constraints.get("overview_drilldown")
    ):
        source_paths = _scope_paths_from_storage(
            scan_id, scan_result, kind, payload.get("value"), constraints
        )
        if not source_paths:
            raise ValueError("所选范围没有可定位的资料，请刷新数据包概览后重试")
    for path in source_paths:
        resolve_under(scan_result["root"], path.split("::", 1)[0])
    if kind == "directory":
        resolve_under(scan_result["root"], payload.get("value"))
    payload["source_paths"] = source_paths
    payload["constraints"] = constraints
    return ConversationScope.from_dict(payload)


@app.route("/api/package-overview/<scan_id>")
def package_overview(scan_id):
    """Return only intrinsic facts about the imported data package."""
    try:
        require_scan(scan_id)
        overview = build_package_overview_from_storage(storage, scan_id, batch_size=250)
        task = storage.get_import_task(scan_id) or {}
        checkpoint = dict(task.get("checkpoint") or {})
        selected_paths = {str(path) for path in (checkpoint.get("selected_paths") or []) if str(path)}
        selection_plan = checkpoint.get("selection_plan") or {}
        strict_selected = bool(selected_paths) and not bool(
            checkpoint.get("large_selection") or selection_plan.get("schema_version")
        )
        published_deep_paths, deep_published = _published_deep_scope(scan_id)
        report = {}
        for summary_type in ("deep_report", "preliminary_report", "parsed_report", "report"):
            candidate = storage.get_summary(scan_id, ".", summary_type) or {}
            if not candidate:
                continue
            if summary_type == "deep_report" and strict_selected and not deep_published:
                # Deep file/node artifacts may already exist, but they are not
                # part of the user-visible overview until the explicit update
                # action publishes this selection version.
                continue
            report = candidate
            break
        direction = dict(report.get("recommended_research_direction") or {})
        direction["research_questions"] = list(direction.get("research_questions") or [])[:8]
        direction["methods"] = list(direction.get("methods") or [])[:8]
        direction["representative_documents"] = list(direction.get("representative_documents") or [])[:20]
        direction["evidence_chain"] = list(direction.get("evidence_chain") or [])[:8]
        brief = {
            "title": report.get("title") or "数据包研究简报",
            "basic_information": list(report.get("basic_information") or [])[:12],
            "key_findings": list(report.get("key_findings") or [])[:8],
            "coverage": report.get("coverage") or {},
            "value_judgment": report.get("value_judgment") or {},
            "global_categories": list(report.get("global_categories") or [])[:10],
            "recommended_research_direction": direction,
            "direction_candidates": list(report.get("direction_candidates") or [])[:6],
            "limitations": list((report.get("value_judgment") or {}).get("limitations") or [])[:10],
            "available": bool(report),
        }
        artifact = storage.latest_artifact(
            scan_id, _request_owner_id() or "legacy", kind="overview_report"
        )
        return jsonify({
            "ok": True, "overview": overview,
            "research_brief": brief, "report_artifact": artifact,
        })
    except KeyError:
        return api_error("数据包不存在", 404)
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/package/<scan_id>/overview")
def package_overview_contract(scan_id):
    """Versioned package-overview alias used by the new workflow shell."""
    return package_overview(scan_id)


@app.route("/api/package/<scan_id>/directory")
def package_directory_contract(scan_id):
    """Return the formal semantic directory under the package API namespace."""
    return formal_directory(scan_id)


@app.route("/api/package/<scan_id>/nodes/<node_id>")
def package_node_detail(scan_id, node_id):
    try:
        require_scan(scan_id)
        node = dict(_find_analysis_node(scan_id, node_id))
        declared_count = int(node.get("file_count") or 0)
        lazy_members = bool(node.get("member_paths_lazy"))
        selection_filter = dict(node.get("selection_filter") or {})
        representative_paths = list(node.get("representative_paths") or node.get("member_paths") or [])[:50]
        # The analysis tree is a structural index. Once a deep node summary
        # exists, it is the authoritative content source for this detail API;
        # otherwise an older tree payload can hide verified conclusions.
        deep_summary = storage.get_summary(
            scan_id, "node:{}".format(node_id), staged_summary_type("deep", "node")
        ) or storage.get_summary(scan_id, "node:{}".format(node_id), "folder") or {}
        if str(deep_summary.get("analysis_level") or "").lower() == "deep":
            node.update(deep_summary)
        if str(deep_summary.get("analysis_level") or "").lower() != "deep":
            node_members = set(node.get("member_paths") or [])
            for row in storage.list_summaries(scan_id):
                candidate = row.get("payload") or {}
                candidate_members = set(candidate.get("member_paths") or [])
                if (str(candidate.get("analysis_level") or "").lower() == "deep"
                        and node_members == candidate_members):
                    node.update(candidate)
                    break
        members = sorted(set(str(path) for path in (node.get("member_paths") or node.get("source_paths") or []) if path))
        displayed_count = declared_count or int(node.get("file_count") or 0) or len(members)
        coverage = dict(node.get("coverage") or {})
        if lazy_members:
            coverage.setdefault("category_file_count", displayed_count)
            coverage.setdefault("analyzed_scope_files", len(members))
            coverage.setdefault("members_materialized", False)
            coverage.setdefault("scope_limited", True)
        claims = node.get("claims") or node.get("evidence_claims") or []
        conclusions = node.get("conclusions") or node.get("conclusion_evidence") or claims
        question_answer_evidence = node.get("question_answer_evidence") or {}
        return jsonify({
            "ok": True,
            "scan_id": scan_id,
            "node": {
                "node_id": node.get("node_id") or node_id,
                "name": node.get("name") or node.get("title") or "未命名主题",
                "summary": node.get("summary") or node.get("description") or "",
                "research_value": node.get("research_value") or node.get("question_value") or "",
                "research_questions": node.get("research_questions") or node.get("questions") or [],
                "member_paths": members,
                "file_count": displayed_count,
                "member_paths_lazy": lazy_members,
                "selection_filter": selection_filter,
                "representative_paths": representative_paths,
                "total_bytes": int(node.get("total_bytes") or node.get("total_size") or 0),
                "conclusions": conclusions,
                "claims": claims,
                "question": node.get("question") or question_answer_evidence.get("question") or "",
                "value": node.get("value") or question_answer_evidence.get("value") or "",
                "answer": node.get("answer") or question_answer_evidence.get("answer") or "",
                "question_answer_evidence": question_answer_evidence,
                "conclusion_evidence": node.get("conclusion_evidence") or [],
                "conflicts": node.get("conflicts") or [],
                "limitations": node.get("limitations") or [],
                "coverage": coverage,
                "analysis_level": node.get("analysis_level") or "preview",
                "analysis_depth": node.get("analysis_depth") or "preview_folder",
                "deep_analysis": bool(node.get("deep_analysis")),
                "evidence_status": node.get("evidence_status") or "insufficient",
                "verification_status": node.get("verification_status") or "candidate",
                "evidence": node.get("evidence") or node.get("evidence_chain") or [],
            },
        })
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/scan/<scan_id>/refresh-deep-results", methods=["POST"])
def refresh_deep_results(scan_id):
    """Rebuild user-facing outputs after all deep model work is durable."""
    try:
        require_scan(scan_id)
        task = storage.get_import_task(scan_id) or {}
        processing = _package_processing_status(scan_id)
        task_status = str(task.get("status") or "").lower()
        with storage._connect() as conn:
            active_jobs = int(conn.execute(
                "SELECT COUNT(*) AS value FROM analysis_jobs "
                "WHERE scan_id=? AND status IN ('queued','running','cancelling') "
                "AND task_type='generate_summary' AND "
                "json_extract(options,'$.workflow_source') IN "
                "('deep_parse_model_summary','idle_deep_model_summary',"
                "'large_selection_model_summary','deep_node_rebuild','candidate_node_summary')",
                (str(scan_id),),
            ).fetchone()["value"] or 0)
        # Parser attention states do not mean that the model deep summary is
        # missing. Refresh readiness is based on durable deep-batch items and
        # persisted model summaries instead of the parser status counter.
        checkpoint = dict(task.get("checkpoint") or {})
        selected_paths = [str(path) for path in (checkpoint.get("selected_paths") or []) if str(path)]
        if selected_paths:
            completed = [path for path in selected_paths if _is_persisted_deep_file_summary(storage.get_summary(scan_id, path, staged_summary_type("deep", "file")) or {})]
            if len(completed) != len(selected_paths) or active_jobs:
                return api_error("深度摘要尚未全部完成，暂不能更新正式结果", 409, {"selected_files": len(selected_paths), "completed_files": len(completed), "active_deep_jobs": active_jobs})
            if task_status != "deep_update_available":
                return api_error("请等待当前阶段完成后再更新。", 409, {"task_status": task_status})
            storage.transition_import_task(
                scan_id, "deep_overview_updating", checkpoint=checkpoint,
                reason="用户正在按当前已完成的深度证据更新目录和情报概览。",
            )
            report_id, _created = storage.create_or_get_typed_job(
                scan_id, "generate_report",
                options={"workflow_source": "deep_results_report", "selection_version": checkpoint.get("selection_version"), "resume_state": task_status},
                owner_id=_request_owner_id() or "legacy",
            )
            checkpoint["deep_overview_job_id"] = report_id
            storage.update_import_task(scan_id, checkpoint=checkpoint)
            return jsonify({"ok": True, "accepted": True, "updated": False, "job_id": report_id, "status_url": "/api/jobs/{}".format(report_id)}), 202
        deep_batch_id = str(checkpoint.get("deep_batch_id") or "")
        selection_plan = checkpoint.get("selection_plan") or {}
        large_selection = bool(checkpoint.get("large_selection") or selection_plan.get("schema_version"))
        deep_paths = []
        deep_failed_items = 0
        if deep_batch_id:
            with storage._connect() as conn:
                deep_rows = conn.execute(
                    "SELECT node_path,status FROM deep_parse_items WHERE batch_id=?",
                    (deep_batch_id,),
                ).fetchall()
            deep_paths = [
                str(row["node_path"] or "")
                for row in deep_rows
                if str(row["node_path"] or "")
            ]
            deep_failed_items = sum(
                1 for row in deep_rows
                if str(row["status"] or "").lower() == "failed"
            )
        if not large_selection:
            # A normal package can have one foreground selection batch followed
            # by one or more idle backfill batches. Refresh must see the union,
            # not only the newest batch, otherwise earlier deep results vanish
            # from the readiness calculation.
            with storage._connect() as conn:
                all_deep_rows = conn.execute(
                    "SELECT i.node_path,i.status FROM deep_parse_items i "
                    "JOIN deep_parse_batches b ON b.batch_id=i.batch_id "
                    "WHERE b.scan_id=?",
                    (str(scan_id),),
                ).fetchall()
            deep_paths = list(dict.fromkeys(
                str(row["node_path"] or "") for row in all_deep_rows
                if str(row["node_path"] or "")
            ))
            deep_failed_items = sum(
                1 for row in all_deep_rows
                if str(row["status"] or "").lower() == "failed"
            )
        required_model_paths = [
            str(path) for path in (selection_plan.get("model_paths") or []) if str(path)
        ] if large_selection else list(deep_paths)
        required_model_set = set(required_model_paths)
        if large_selection and deep_batch_id and required_model_set:
            with storage._connect() as conn:
                deep_failed_items = int(conn.execute(
                    "SELECT COUNT(*) AS value FROM deep_parse_items "
                    "WHERE batch_id=? AND status='failed' AND node_path IN ({})".format(
                        ",".join("?" for _ in required_model_set)
                    ),
                    [deep_batch_id] + sorted(required_model_set),
                ).fetchone()["value"] or 0)
        expected_deep = len(required_model_paths) or int(
            checkpoint.get("deep_summary_expected") or 0
        )
        if not expected_deep and not large_selection:
            expected_deep = int(
                processing.get("logical_total_files")
                or processing.get("inventory_files")
                or 0
            )
        model_deep_completed = 0
        for path in required_model_paths:
            summary = storage.get_summary(scan_id, path, "file") or {}
            if (
                bool(summary.get("deep_analysis"))
                and (
                    str(summary.get("analysis_level") or "").lower() == "deep"
                    or str(summary.get("generated_by") or "") == "model-deep-analysis"
                )
            ):
                model_deep_completed += 1
        model_deep_pending = max(
            0, expected_deep - model_deep_completed - deep_failed_items
        )
        deep_completion_ratio = round(
            model_deep_completed / float(expected_deep or 1), 6
        )
        refreshable_status = task_status in {
            "summarizing_files", "building_directory", "completed", "partial"
        }
        # Updating the directory/report is a read-through projection over
        # durable summaries. It is safe and useful while later low-priority
        # summaries are still running; one completed deep file is enough to
        # publish a partial refresh.
        has_deep_result = model_deep_completed > 0
        if not refreshable_status or not has_deep_result:
            return api_error(
                "尚无可用于更新正式结果的深度摘要",
                409,
                {
                    "task_status": task_status,
                    "deep_completion_ratio": deep_completion_ratio,
                    "active_deep_jobs": active_jobs,
            "pending_files": model_deep_pending,
            "failed_files": deep_failed_items,
            "selection_plan": selection_plan if large_selection else None,
                },
            )
        report = _write_local_overview(
            scan_id, owner_id=_request_owner_id(), job_id=None
        )
        # The directory and package overview are read-through projections of
        # persisted deep summaries. Re-read the directory to verify visibility.
        directory_response = formal_directory(scan_id)
        if isinstance(directory_response, tuple):
            directory_response = directory_response[0]
        directory_payload = directory_response.get_json(silent=True) or {}
        directory = directory_payload.get("directory") or {}
        return jsonify({
            "ok": True,
            "scan_id": scan_id,
            "updated": True,
            "report": report,
            "directory": directory,
            "formal_directory_status": directory.get("status"),
            "topic_count": len(directory.get("topics") or []),
            "tree_edits_preserved": True,
            "message": "已按深度摘要更新文件摘要、节点摘要、智能目录和情报概览。",
        })
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/package/<scan_id>/files/<path:node_path>/detail")
def package_file_detail(scan_id, node_path):
    """Return one file's unified document and source-backed conclusion cards."""
    try:
        require_scan(scan_id)
        node_path = str(node_path or "").strip()
        if not node_path:
            raise ValueError("缺少文件路径")
        document = storage.get_document(scan_id, node_path)
        requested_type = str(request.args.get("type") or "").strip()
        staged_types = {
            staged_summary_type("deep", "file"),
            staged_summary_type("preliminary", "file"),
        }
        if requested_type in staged_types:
            summary_type = requested_type
        else:
            task = storage.get_import_task(scan_id) or {}
            selected = {str(path) for path in ((task.get("checkpoint") or {}).get("selected_paths") or []) if str(path)}
            summary_type = "file"
            if selected and node_path in selected:
                if storage.get_summary(scan_id, node_path, staged_summary_type("deep", "file")):
                    summary_type = staged_summary_type("deep", "file")
                elif storage.get_summary(scan_id, node_path, staged_summary_type("preliminary", "file")):
                    summary_type = staged_summary_type("preliminary", "file")
        summary = storage.get_summary(scan_id, node_path, summary_type)
        if summary and summary_type == "file" and summary.get("claim_contract") != "file-claims/1.0" and document:
            summary.update(build_file_claims(summary, document.get("evidence", [])))
            storage.save_summary(scan_id, node_path, "file", summary)
        if not document and not summary:
            return api_error("该文件尚未完成解析", 404)
        return jsonify({
            "ok": True,
            "scan_id": scan_id,
            "path": node_path,
            "document": document,
            "summary": summary,
            "file_conclusions": (summary or {}).get("file_conclusions") or [],
            "file_arguments": (summary or {}).get("file_arguments") or [],
            "file_review_items": (summary or {}).get("file_review_items") or [],
            "file_limitations": (summary or {}).get("file_limitations") or [],
            "evidence_quality": (summary or {}).get("evidence_quality") or {},
        })
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/package/<scan_id>/reprocess", methods=["POST"])
def package_reprocess(scan_id):
    """Create a fresh scan for a historical package without mutating old results."""
    try:
        scan = require_scan(scan_id)
        root = str(scan.get("root") or scan.get("root_path") or "").strip()
        if not root:
            raise ValueError("历史数据包没有可重新读取的服务器路径")
        payload = request.get_json(silent=True) or {}
        parse_mode = str(payload.get("parse_mode") or scan.get("parse_mode") or "auto").lower()
        if parse_mode not in {"auto", "fast", "accurate"}:
            parse_mode = "auto"
        job_id = storage.create_scan_job(
            root, Config.MAX_SCAN_FILES, parse_mode,
            Config.MAX_SCAN_DEPTH, owner_id=_request_owner_id() or "legacy",
        )
        return jsonify({
            "ok": True, "accepted": True, "scan_id": job_id,
            "job_id": job_id, "source_scan_id": scan_id,
            "status_url": "/api/jobs/{}".format(job_id),
        }), 202
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/homogeneous-analysis/<scan_id>", methods=["GET", "POST"])
def homogeneous_analysis(scan_id):
    """Start or read the independent homogeneous-document analysis module."""
    try:
        require_scan(scan_id)
        if request.method == "POST":
            job_id, created = storage.create_or_get_typed_job(
                scan_id, "homogeneous_analysis",
                options={"workflow_source": "homogeneous_module"},
                owner_id=_request_owner_id() or "legacy",
            )
            return jsonify({
                "ok": True, "accepted": True, "job_id": job_id,
                "reused_active_job": not created,
                "status_url": "/api/jobs/{}".format(job_id),
            }), 202
        try:
            offset = int(request.args.get("offset", 0))
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            raise ValueError("分页参数必须是整数")
        result = storage.get_homogeneous_analysis(
            scan_id, offset=offset, limit=limit,
            query=request.args.get("query", ""),
            relation_type=request.args.get("relation_type", ""),
        )
        return jsonify({"ok": True, "available": bool(result), "analysis": result})
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/homogeneous-record/<scan_id>")
def homogeneous_record(scan_id):
    try:
        require_scan(scan_id)
        node_path = str(request.args.get("path") or "").strip()
        if not node_path:
            raise ValueError("缺少文件路径")
        result = storage.get_homogeneous_record(scan_id, node_path)
        if not result:
            return api_error("同构分析台账中不存在该文件", 404)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/relationships/<scan_id>")
def relationship_catalog(scan_id):
    """Read the shared, evidence-carrying file relationship contract."""
    try:
        require_scan(scan_id)
        try:
            limit = int(request.args.get("limit", 500))
        except (TypeError, ValueError):
            raise ValueError("limit 必须是整数")
        source_path = str(request.args.get("path") or "").strip()
        if source_path:
            resolve_under(require_scan(scan_id)["root"], source_path.split("::", 1)[0])
        return jsonify({
            "ok": True,
            "relationships": storage.get_relationship_catalog(
                scan_id, source_path=source_path or None, limit=limit,
            ),
        })
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/conversations", methods=["POST"])
def create_conversation():
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = str(payload.get("scan_id") or "")
        scan_result = require_scan(scan_id)
        scope = _resolved_conversation_scope(scan_id, scan_result, payload.get("scope") or {})
        session = conversation_engine.new_session(
            scan_id, scope=scope, title=str(payload.get("title") or "资料问答")[:200]
        )
        storage.save_conversation(session.as_dict(), _request_owner_id() or "legacy")
        return jsonify({
            "ok": True,
            "session": session.as_dict(),
            "search_index": _conversation_index_status(scan_id),
        }), 201
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/conversations/<scan_id>")
def conversations_for_scan(scan_id):
    try:
        require_scan(scan_id)
        return jsonify({
            "ok": True,
            "items": storage.list_conversations(scan_id, _request_owner_id() or "legacy"),
        })
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/conversation/<session_id>")
def get_conversation(session_id):
    scan_id = str(request.args.get("scan_id", "") or "")
    try:
        require_scan(scan_id)
        message_limit = max(
            20, min(200, int(request.args.get("message_limit", 100) or 100))
        )
        before_sequence = request.args.get("before_sequence")
        if before_sequence is not None:
            before_sequence = max(1, int(before_sequence))
        payload = storage.get_conversation(
            session_id, _request_owner_id() or "legacy", scan_id=scan_id,
            message_limit=message_limit,
            before_sequence=before_sequence,
        )
        if not payload:
            return api_error("会话不存在", 404)
        owner_id = _request_owner_id() or "legacy"
        return jsonify({
            "ok": True,
            "session": payload,
            "turns": storage.list_conversation_turns(session_id, owner_id),
            "research_memory": storage.get_conversation_research_memory(session_id),
            "search_index": _conversation_index_status(scan_id),
        })
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/conversation/<session_id>/turns", methods=["POST"])
def create_conversation_turn(session_id):
    payload = request.get_json(silent=True) or {}
    owner_id = _request_owner_id() or "legacy"
    try:
        scan_id = str(payload.get("scan_id") or "")
        scan_result = require_scan(scan_id)
        stored = storage.get_conversation(session_id, owner_id, scan_id=scan_id)
        if not stored:
            return api_error("会话不存在", 404)
        question = str(payload.get("question") or "").strip()
        if not question or len(question) > 8000:
            raise ValueError("问题不能为空且不能超过 8000 字符")
        search_index = _conversation_index_status(scan_id)
        if not search_index["usable"]:
            return jsonify({
                "ok": False,
                "error": "当前数据包尚无可检索文本；请先等待基础解析或恢复索引重建。",
                "code": "search_index_unavailable",
                "search_index": search_index,
                "rebuild_url": "/api/scans/{}/rebuild-search-index".format(scan_id),
            }), 409
        scope_payload = payload.get("scope")
        if scope_payload is None:
            scope = ConversationScope.from_dict(stored.get("scope") or {})
        else:
            scope = _resolved_conversation_scope(scan_id, scan_result, scope_payload or {})
        turn, created = storage.create_conversation_turn(
            session_id, scan_id, owner_id, question, scope.as_dict(),
            persist_scope=bool(payload.get("persist_scope")),
            idempotency_key=payload.get("idempotency_key"),
        )
        session_payload = storage.get_conversation(session_id, owner_id, scan_id=scan_id)
        return jsonify({
            "ok": True,
            "turn": turn,
            "session": session_payload,
            "job_id": turn.get("job_id"),
            "created": created,
            "search_index": search_index,
            "coverage_mode": "complete" if search_index["ready"] else "partial",
        }), 202 if created else 200
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/scans/<scan_id>/rebuild-search-index", methods=["POST"])
def rebuild_search_index(scan_id):
    try:
        require_scan(scan_id)
        options = {
            "workflow_source": "index_rebuild",
            "scope_label": "重建轻量预览与对话证据索引",
            "parse_mode": "accurate",
        }
        job_id, created = storage.create_or_get_typed_job(
            scan_id,
            "rebuild_search_index",
            options=options,
            owner_id=_request_owner_id() or "legacy",
        )
        if created:
            storage.begin_search_index_rebuild(
                scan_id, storage.count_documents(scan_id)
            )
        return jsonify({
            "ok": True,
            "accepted": True,
            "job_id": job_id,
            "created": created,
            "search_index": _conversation_index_status(scan_id),
            "status_url": "/api/jobs/{}".format(job_id),
        }), 202 if created else 200
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/turns/<turn_id>")
def get_conversation_turn(turn_id):
    owner_id = _request_owner_id() or "legacy"
    turn = storage.get_conversation_turn(turn_id, owner_id=owner_id)
    if not turn:
        return api_error("分析轮次不存在", 404)
    session = storage.get_conversation(
        turn["session_id"], owner_id, scan_id=turn["scan_id"]
    )
    return jsonify({
        "ok": True,
        "turn": turn,
        "steps": storage.list_conversation_turn_steps(turn_id),
        "session": session,
    })


@app.route("/api/turns/<turn_id>/events")
def get_conversation_turn_events(turn_id):
    try:
        after = max(0, int(request.args.get("after", 0) or 0))
    except (TypeError, ValueError):
        after = 0
    events = storage.get_conversation_turn_events(
        turn_id, _request_owner_id() or "legacy", after=after,
    )
    if events is None:
        return api_error("分析轮次不存在", 404)
    return jsonify({
        "ok": True,
        "items": events,
        "next_after": events[-1]["event_id"] if events else after,
    })


@app.route("/api/turns/<turn_id>/cancel", methods=["POST"])
def cancel_conversation_turn(turn_id):
    owner_id = _request_owner_id() or "legacy"
    turn = storage.get_conversation_turn(turn_id, owner_id=owner_id)
    if not turn:
        return api_error("分析轮次不存在", 404)
    if turn.get("status") in {"completed", "failed", "cancelled"}:
        return jsonify({"ok": True, "turn": turn})
    for job_id in (turn.get("job_id"), turn.get("promotion_job_id")):
        if job_id:
            storage.cancel_job(job_id)
    turn = storage.update_conversation_turn(
        turn_id, status="cancelled", stage="cancelled", progress=100,
        message="用户已取消本轮分析", event_type="cancelled",
    )
    storage.set_conversation_turn_message(
        turn_id, "本轮分析已取消。", "cancelled", "cancelled", 100
    )
    return jsonify({"ok": True, "turn": turn})


@app.route("/api/turns/<turn_id>/retry", methods=["POST"])
def retry_conversation_turn(turn_id):
    owner_id = _request_owner_id() or "legacy"
    turn = storage.get_conversation_turn(turn_id, owner_id=owner_id)
    if not turn:
        return api_error("分析轮次不存在", 404)
    if turn.get("status") not in {"failed", "cancelled"}:
        return api_error("只有失败或已取消的分析可以重试", 409)
    job_id = storage.replace_conversation_turn_job(
        turn_id, owner_id, message="用户已重新提交本轮分析", force=True,
    )
    if not job_id:
        return api_error("无法重新提交本轮分析", 409)
    return jsonify({
        "ok": True,
        "turn": storage.get_conversation_turn(turn_id, owner_id=owner_id),
        "job_id": job_id,
    }), 202


@app.route("/api/turns/<turn_id>/continue-deep-analysis", methods=["POST"])
def continue_conversation_turn_deep_analysis(turn_id):
    owner_id = _request_owner_id() or "legacy"
    turn = storage.get_conversation_turn(turn_id, owner_id=owner_id)
    if not turn:
        return api_error("分析轮次不存在", 404)
    if turn.get("status") != "completed":
        return api_error("只有已完成的阶段性分析可以继续深析", 409)
    result = dict(turn.get("result") or {})
    if not result.get("promotion_limit_reached"):
        return api_error("本轮没有待继续的深析候选文件", 409)
    promotion = dict(result.get("promotion_request") or {})
    payload = request.get_json(silent=True) or {}
    raw_candidate_paths = payload.get("candidate_paths") or []
    if not isinstance(raw_candidate_paths, list):
        return api_error("candidate_paths 必须是文件路径数组", 400)
    requested_filter = {
        str(path) for path in raw_candidate_paths if path
    }
    try:
        desired = max(1, min(24, int(payload.get("desired_file_count") or 12)))
    except (TypeError, ValueError):
        return api_error("desired_file_count 必须是 1 到 24 的整数", 400)
    inventory = set(storage.inventory_paths_under(turn["scan_id"], "."))
    candidates = []
    for path in promotion.get("candidate_paths") or []:
        path = str(path)
        if (
            path not in inventory
            or (requested_filter and path not in requested_filter)
            or path in candidates
            or (storage.get_file_state(turn["scan_id"], path) or {}).get("status")
            == "completed"
        ):
            continue
        candidates.append(path)
        if len(candidates) >= desired:
            break
    if not candidates:
        return api_error("候选文件已经完成深析或不再属于当前数据包", 409)
    job_id, _created = storage.create_or_get_typed_job(
        turn["scan_id"],
        "analyze_package",
        options={
            "target_paths": candidates,
            "workflow_source": "question_promotion",
            "scope_label": "用户继续交互式深析：{}".format(turn["question"][:120]),
            "parse_mode": "accurate",
            "conversation_turn_id": turn_id,
            "conversation_scope": turn.get("scope") or {},
            "conversation_continuation_depth": 1,
        },
        owner_id=owner_id,
    )
    storage.update_conversation_turn(
        turn_id,
        status="waiting_for_deep_analysis",
        stage="waiting_for_deep_analysis",
        progress=65,
        promotion_job_id=job_id,
        continuation_depth=0,
        message="已按用户要求继续深析 {} 份候选文件".format(len(candidates)),
        event_type="promotion_continued",
    )
    storage.set_conversation_turn_message(
        turn_id,
        "正在继续深析 {} 份候选文件；完成后会重新核验本轮回答。".format(
            len(candidates)
        ),
        "waiting_for_deep_analysis",
        "waiting_for_deep_analysis",
        65,
    )
    return jsonify({
        "ok": True,
        "turn": storage.get_conversation_turn(turn_id, owner_id=owner_id),
        "job_id": job_id,
        "candidate_paths": candidates,
    }), 202


@app.route("/api/conversation/<session_id>/messages", methods=["POST"])
def conversation_message(session_id):
    # Backwards-compatible alias.  Older clients used /messages, but the
    # durable /turns endpoint is the single source of truth for idempotency,
    # Worker execution, cancellation, retries and promotion continuation.
    return create_conversation_turn(session_id)


@app.route("/api/translations/<scan_id>")
def list_translations(scan_id):
    """List one bounded, searchable page of translation work.

    In addition to persisted translation states, the page includes
    language-classified files that have not begun translation.  That makes a
    large package's pending work visible instead of presenting only the first
    translated subset.
    """
    try:
        require_scan(scan_id)
        limit = max(1, min(500, int(request.args.get("limit", 100) or 100)))
        offset = max(0, min(10_000_000, int(request.args.get("offset", 0) or 0)))
        query = str(
            request.args.get("query", request.args.get("q", "")) or ""
        ).strip()
        if len(query) > 500:
            raise ValueError("query 不能超过 500 个字符")

        # Support both the documented comma-separated form and repeated
        # ``status`` query parameters when the underlying request adapter has
        # a MultiDict-compatible ``getlist`` method.
        status_values = []
        getlist = getattr(request.args, "getlist", None)
        for key in ("status", "statuses"):
            values = getlist(key) if callable(getlist) else [request.args.get(key, "")]
            for raw in values or []:
                for value in str(raw or "").split(","):
                    value = value.strip()
                    if not value or value.casefold() == "all":
                        continue
                    if len(value) > 64:
                        raise ValueError("status 不能超过 64 个字符")
                    status_values.append(value)
        statuses = list(dict.fromkeys(status_values))
        language = str(request.args.get("language", "all") or "all").strip().lower()
        page = storage.list_translation_page(
            scan_id,
            offset=offset,
            limit=limit,
            statuses=statuses or None,
            language=language,
            query=query,
        )
        counts = storage.translation_counts(scan_id)
        return jsonify({
            "ok": True,
            # Preserve the established meaning for existing clients: these
            # are counts of durable translation records, not all candidates.
            "counts": counts,
            **page,
            "previous_offset": max(0, offset - limit) if offset else None,
        })
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/translation/<scan_id>", methods=["GET", "POST"])
def document_translation(scan_id):
    try:
        scan_result = require_scan(scan_id)
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            node_path = str(payload.get("path") or "")
            _translation_inventory_node(scan_result, node_path)
            job_id, _created = storage.create_or_get_typed_job(
                scan_id, "translate_document",
                options={
                    "path": node_path,
                    "require_full": bool(payload.get("require_full", True)),
                },
                owner_id=_request_owner_id() or "legacy",
            )
            return jsonify({"ok": True, "job_id": job_id, "path": node_path}), 202

        node_path = str(request.args.get("path", "") or "")
        _translation_inventory_node(scan_result, node_path)
        stored_document = storage.get_document(scan_id, node_path)
        document = stored_document
        preview = storage.get_file_preview(scan_id, node_path)
        workflow = storage.get_file_workflow_state(scan_id, node_path) or {}
        # Translation work items can be known from the all-file preview pass
        # before a deep document exists.  Returning their bounded preview (or
        # an honest metadata state) keeps a paged list actionable instead of
        # turning every unstarted row into a 404.
        if not document and preview:
            document = preview_as_document(preview)
        state = storage.get_translation(scan_id, node_path, hydrate=True)
        view = str(request.args.get("view", "translated") or "translated").lower()
        if view not in {"original", "translated", "bilingual"}:
            raise ValueError("view 仅支持 original、translated、bilingual")
        offset = max(0, int(request.args.get("offset", 0) or 0))
        limit = max(200, min(20000, int(request.args.get("limit", 8000) or 8000)))
        original_value = (
            (state or {}).get("original_text") if state else (document or {}).get("text")
        )
        original = str(original_value or "")
        translated = (state or {}).get("translated_text")
        translated = str(translated) if translated is not None else None

        def page(text):
            if text is None:
                return None
            return {
                "text": text[offset:offset + limit],
                "offset": offset,
                "limit": limit,
                "total_characters": len(text),
                "has_more": offset + limit < len(text),
            }

        response = {
            "ok": True,
            "path": node_path,
            "view": view,
            "status": (state or {}).get("status") or "not_started",
            "source_level": (state or {}).get("source_level"),
            "source_availability": "document" if stored_document else (
                "preview" if preview else "metadata"
            ),
            "content_available": bool(document),
            "full_translation": bool((state or {}).get("full_translation")),
            "source_language": (state or {}).get("source_language") or (
                (preview.get("language") or {}).get("code") if preview else None
            ) or workflow.get("language_code") or "unknown",
            "target_language": (state or {}).get("target_language") or "zh-CN",
            "translation_mode": (state or {}).get("translation_mode") or (
                "quality" if Config.TRANSLATION_REVIEW_COMPLEX_UNITS else "fast"
            ),
            "performance": (state or {}).get("performance") or {},
            "titles": {
                "original": (state or {}).get("original_title") or ((document or {}).get("structure") or {}).get("title"),
                "translated": (state or {}).get("translated_title"),
            },
            "progress": (state or {}).get("progress"),
            "errors": (state or {}).get("errors") or [],
            "warnings": (state or {}).get("warnings") or [],
        }
        if view in {"original", "bilingual"}:
            response["original"] = page(original)
        if view in {"translated", "bilingual"}:
            response["translated"] = page(translated)
        if not state and document:
            response["plan"] = build_translation_plan(
                storage.project_document(
                    document, text_limit=Config.LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE
                ),
                max_unit_chars=Config.TRANSLATION_MAX_UNIT_CHARS,
                coalesce_paragraphs=Config.TRANSLATION_COALESCE_PARAGRAPHS,
            )
        return jsonify(response)
    except (TypeError, ValueError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/translate-package/<scan_id>", methods=["POST"])
def translate_package(scan_id):
    try:
        scan_result = require_scan(scan_id)
        payload = request.get_json(silent=True) or {}
        phase = str(payload.get("phase") or "preview_and_priority")
        if phase not in {"preview_and_priority", "deep_backfill"}:
            raise ValueError("不支持的翻译阶段")
        requested_paths = payload.get("target_paths", payload.get("paths"))
        options = {"phase": phase, "cursor": 0}
        if requested_paths is not None:
            if not isinstance(requested_paths, list):
                raise ValueError("target_paths 必须是文件路径数组")
            selected_paths = list(dict.fromkeys(
                str(path).strip() for path in requested_paths if str(path).strip()
            ))
            if not selected_paths:
                raise ValueError("至少选择一个需要翻译的文件")
            if len(selected_paths) > 500:
                raise ValueError("单次最多选择 500 个文件")
            for path in selected_paths:
                _translation_inventory_node(scan_result, path)
            options.update({
                "target_paths": selected_paths,
                "selected_only": True,
                "require_full": bool(payload.get("require_full", True)),
            })
        job_id, _created = storage.create_or_get_typed_job(
            scan_id, "translate_package", options=options,
            owner_id=_request_owner_id() or "legacy",
        )
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "phase": phase,
            "selected_files": len(options.get("target_paths") or []),
        }), 202
    except ValueError as exc:
        return api_error(str(exc), 400)


def _retrieval_search_status(scan_id, scan_result, result, evidence_index_count):
    """Explain an empty search without turning unindexed data into a miss."""
    processing = storage.package_processing_counts(scan_id)
    preview_counts = storage.file_preview_counts(scan_id)
    workflow_total = int(processing.get("workflow_total") or 0)
    indexed_files = int(processing.get("light_ready") or 0)
    document_count = storage.count_documents(scan_id)
    previewed_files = int(preview_counts.get("previewed") or 0)
    inventory_total = max(0, int(scan_result.get("file_count") or 0))
    searchable_files = max(indexed_files, document_count, previewed_files)
    expected_files = workflow_total or inventory_total
    unindexed_files = max(0, expected_files - searchable_files)
    incomplete_files = int(processing.get("incomplete") or 0)
    retry_waiting = int(processing.get("retry_waiting") or 0)
    needs_attention = int(processing.get("needs_attention") or 0)
    durable = storage.get_search_index_state(scan_id) or {}
    durable_state = str(durable.get("status") or "legacy")
    result_count = int(result.get("result_count") or len(result.get("results") or []))
    if result_count:
        code = "matched"
        message = "已在当前可检索范围内找到匹配证据。"
    elif not evidence_index_count and not document_count:
        code = "index_unavailable"
        message = "当前范围尚无可检索正文证据；请等待基础解析或搜索索引完成。"
    elif (
        unindexed_files
        or incomplete_files
        or durable_state in {"rebuilding", "interrupted", "rebuild_required"}
    ):
        code = "partial_index"
        message = (
            "当前已索引内容没有匹配；仍有 {} 个文件尚未完成基础索引或深度处理，"
            "不能据此判断全库没有相关内容。"
        ).format(max(unindexed_files, incomplete_files))
    else:
        code = "no_match"
        message = "当前已完成索引的范围内没有匹配内容。"
    return {
        "code": code,
        "message": message,
        "coverage": {
            "inventory_files": inventory_total,
            "workflow_files": workflow_total,
            "foundation_searchable_files": searchable_files,
            "unindexed_files": unindexed_files,
            "deep_incomplete_files": incomplete_files,
            "retry_waiting_files": retry_waiting,
            "needs_attention_files": needs_attention,
            "evidence_records": int(evidence_index_count or 0),
            "index_state": durable_state,
        },
    }


@app.route("/api/retrieve", methods=["POST"])
def retrieve():
    payload = request.get_json(silent=True) or {}

    try:
        scan_id = payload.get(
            "scan_id",
            ""
        )

        query = payload.get(
            "query",
            ""
        )

        # 新增：
        # 如果网页选中的是虚拟主题节点，
        # 会传 node_id。
        node_id = payload.get(
            "node_id"
        )

        scan_result = require_scan(
            scan_id
        )

        evidence_index_count = storage.count_evidence_index(scan_id)
        # The normal path searches SQLite first.  Loading every projected
        # document remains only a compatibility fallback for pre-index data.
        documents = [] if evidence_index_count else _package_documents(scan_id, canonical_only=True)
        indexed_chunks = None

        # =====================================================
        # 情况1：用户选中的是“主题节点”
        # =====================================================
        if node_id:

            node = _find_analysis_node(
                scan_id,
                node_id
            )

            member_paths = set(
                node.get("member_paths")
                or []
            )

            if not member_paths:
                raise ValueError(
                    "当前主题节点没有关联文件"
                )

            # 只留下这个主题节点里的文件
            if documents:
                documents = [item for item in documents if item.get("path") in member_paths]

            # 保存检索结果时使用这个作用域标识
            scope_key = (
                "node:{}".format(
                    node_id
                )
            )

            # 传给 retrieve_evidence 的 documents
            # 已经提前过滤，所以内部直接检索全部即可。
            retrieval_scope = "."

        # =====================================================
        # 情况2：用户选中真实文件夹/文件
        # =====================================================
        else:

            scope = (
                payload.get(
                    "path",
                    "."
                )
                or "."
            )

            resolve_under(
                scan_result["root"],
                scope
            )

            scope_key = scope

            retrieval_scope = scope

        previous_result_id = payload.get(
            "previous_result_id"
        )

        candidate_ids = None

        if previous_result_id:

            previous = storage.get_retrieval_result(
                previous_result_id,
                scan_id=scan_id
            )

            if not previous:
                return api_error(
                    "上一次检索结果已失效，请重新检索。",
                    400
                )

            if previous.get(
                "scope"
            ) != scope_key:

                return api_error(
                    "二次检索必须保持在上一次结果的同一节点范围内。",
                    400
                )

            candidate_ids = previous.get(
                "evidence_ids",
                []
            )

        if evidence_index_count:
            indexed_chunks = storage.search_evidence_index(
                scan_id,
                query,
                scope=retrieval_scope,
                source_paths=member_paths if node_id else None,
                candidate_evidence_ids=candidate_ids,
                limit=2500,
            )

        result = retrieve_evidence(
            documents,
            query,
            scope=retrieval_scope,
            top_k=payload.get(
                "top_k",
                10
            ),
            candidate_evidence_ids=candidate_ids,
            indexed_chunks=indexed_chunks if evidence_index_count else None,
        )

        # Search may be driven by text evidence, metadata, or a relation.  A
        # selected result therefore carries catalog edges touching its source
        # files, using exactly the contract consumed by reports and graphs.
        result_paths = {
            str(item.get("archive_source_path") or item.get("source_path") or "")
            for item in (result.get("results") or [])
        }
        relation_items = []
        for path in sorted(path for path in result_paths if path)[:50]:
            catalog = storage.get_relationship_catalog(scan_id, source_path=path, limit=100)
            relation_items.extend(catalog.get("items") or [])
        relation_items = list({item.get("relation_key"): item for item in relation_items}.values())
        result["relationship_catalog"] = {
            "schema_version": "relationship-catalog/1.0",
            "items": relation_items[:500],
            "relationship_count": len(relation_items),
            "matched_source_count": len(result_paths),
            "feature_index_is_recall_only": True,
        }
        for relation in result["relationship_catalog"]["items"]:
            relation["match_type"] = "relation"

        # 返回前端真正的逻辑范围
        result["scope"] = scope_key
        result["search_status"] = _retrieval_search_status(
            scan_id, scan_result, result, evidence_index_count
        )

        index_job_id = None
        if result["search_status"].get("code") == "index_unavailable":
            # A rebuild of durable deep documents alone cannot help a newly
            # scanned package with no document rows yet.  Submit the normal
            # resumable package pipeline so it creates the all-file preview
            # index first; repeated requests safely reuse the same active job.
            index_job_id, _created = storage.create_or_get_typed_job(
                scan_id,
                "analyze_package",
                options={
                    "workflow_source": "index_rebuild",
                    "scope_label": "检索请求触发基础索引",
                    "parse_mode": "accurate",
                },
                owner_id=_request_owner_id() or "legacy",
            )
            storage.set_package_processing_state(
                scan_id, "running", "检索请求已提交基础索引任务"
            )
            result["search_status"]["index_job_id"] = index_job_id

        if node_id:
            result["node_id"] = node_id
            result["node_name"] = node.get(
                "name"
            )

        result_id = uuid.uuid4().hex[:16]

        result["result_id"] = result_id

        result[
            "filtered_from_result_id"
        ] = previous_result_id

        storage.save_retrieval_result(
            result_id,
            scan_id,
            scope_key,
            [
                item.get("evidence_id")
                for item
                in result.get("results", [])
                if item.get("evidence_id")
            ],
        )

        response = {
            "ok": True,
            "retrieval": result,
        }
        if index_job_id:
            response["job_id"] = index_job_id
            response["status_url"] = "/api/jobs/{}".format(index_job_id)
        return jsonify(response)

    except ValueError as exc:
        return api_error(
            str(exc),
            400
        )


@app.route("/api/search/files", methods=["POST"])
def search_files():
    """Search a package and aggregate every matching logical file.

    This is deliberately separate from /api/retrieve: the evidence endpoint
    returns a small ranked answer window, while this endpoint treats the
    durable index as a file discovery system and reports the complete unique
    file set (bounded by the index query limit and paged for the UI).
    """
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = str(payload.get("scan_id") or "").strip()
        query = re.sub(r"\s+", " ", str(payload.get("query") or "")).strip()
        if not query or len(query) > 8000:
            raise ValueError("搜索内容不能为空且不能超过 8000 个字符")
        scan_result = require_scan(scan_id)

        try:
            page = max(1, int(payload.get("page", 1)))
            page_size = max(1, min(100, int(payload.get("page_size", 50))))
        except (TypeError, ValueError):
            raise ValueError("分页参数无效")

        node_id = str(payload.get("node_id") or "").strip()
        member_paths = None
        scope_key = "."
        retrieval_scope = "."
        node_name = None
        if node_id:
            node = _find_analysis_node(scan_id, node_id)
            member_paths = set(str(path) for path in (node.get("member_paths") or []) if path)
            if not member_paths:
                raise ValueError("当前主题节点没有关联文件")
            scope_key = "node:{}".format(node_id)
            node_name = node.get("name")
        else:
            scope_key = str(payload.get("path") or ".").strip() or "."
            resolve_under(scan_result["root"], scope_key)
            retrieval_scope = scope_key

        evidence_index_count = storage.count_evidence_index(scan_id)
        if evidence_index_count:
            candidates = storage.search_evidence_index(
                scan_id,
                query,
                scope=retrieval_scope,
                source_paths=member_paths,
                limit=5000,
            )
            index_mode = "persistent-evidence-index"
        else:
            documents = _package_documents(scan_id, canonical_only=True)
            if member_paths:
                documents = [item for item in documents if item.get("path") in member_paths]
            candidates = evidence_corpus(documents, scope=retrieval_scope)
            index_mode = "rebuilt-corpus"

        # Rank only the bounded representative window. File discovery itself
        # remains complete over all returned candidates, so a high-ranked
        # evidence limit can never hide lower-ranked matching files.
        ranked = retrieve_evidence(
            {}, query, scope=retrieval_scope, top_k=50, per_source_limit=1000,
            indexed_chunks=candidates, use_tfidf=False,
        )
        score_by_evidence = {
            str(item.get("evidence_id")): item
            for item in (ranked.get("results") or [])
            if item.get("evidence_id")
        }

        groups = {}
        for raw in candidates:
            item = dict(raw)
            logical_path = str(item.get("source_path") or "").strip()
            physical_path = str(
                item.get("archive_source_path")
                or (logical_path.split("::", 1)[0] if "::" in logical_path else logical_path)
            ).strip()
            key = logical_path or physical_path
            if not key:
                continue
            group = groups.setdefault(key, {
                "path": key,
                "physical_path": physical_path or key,
                "name": Path(key.split("::", 1)[-1]).name or key,
                "match_count": 0,
                "best_score": 0.0,
                "snippets": [],
                "pages": [],
                "sections": [],
                "evidence": [],
            })
            group["match_count"] += 1
            ranked_item = score_by_evidence.get(str(item.get("evidence_id") or ""))
            score = float((ranked_item or {}).get("retrieval_score") or 0.0)
            group["best_score"] = max(group["best_score"], score)
            page_value = item.get("page")
            if page_value is not None and page_value not in group["pages"]:
                group["pages"].append(page_value)
            section = str(item.get("section") or "").strip()
            if section and section not in group["sections"]:
                group["sections"].append(section)
            text = str(item.get("text") or item.get("fact") or "").strip()
            if text and len(group["snippets"]) < 3 and text not in group["snippets"]:
                group["snippets"].append(text[:520])
            if len(group["evidence"]) < 3:
                evidence = dict(item)
                if ranked_item:
                    evidence["retrieval_score"] = ranked_item.get("retrieval_score")
                group["evidence"].append(evidence)

        # File discovery has its own complete distinct-path pass. The ranked
        # evidence window remains bounded for snippets, but it can no longer
        # hide lower-ranked files from the file count.
        complete_paths = storage.search_evidence_file_paths(
            scan_id, query, scope=retrieval_scope, source_paths=member_paths,
        ) if evidence_index_count else []
        for discovered_path in complete_paths:
            if discovered_path in groups:
                continue
            physical_path = discovered_path.split("::", 1)[0]
            groups[discovered_path] = {
                "path": discovered_path,
                "physical_path": physical_path,
                "name": Path(discovered_path.split("::", 1)[-1]).name or discovered_path,
                "match_count": 0,
                "best_score": 0.0,
                "snippets": [], "pages": [], "sections": [], "evidence": [],
                "file_set_match": True,
            }

        # Filename/path discovery is available before full-text parsing. Merge
        # metadata matches into the same file cards so chat search can find a
        # file by its name even when no evidence chunk exists yet.
        if not member_paths and retrieval_scope == ".":
            metadata_page = storage.search_file_status_page(
                scan_id, query=query, search_scope="name", offset=0, limit=500
            )
            for metadata in metadata_page.get("items") or []:
                discovered_path = str(metadata.get("path") or metadata.get("node_path") or "")
                if not discovered_path or discovered_path in groups:
                    continue
                groups[discovered_path] = {
                    "path": discovered_path,
                    "physical_path": str(metadata.get("original_path") or discovered_path),
                    "name": str(metadata.get("name") or Path(discovered_path).name),
                    "extension": metadata.get("extension"),
                    "size": metadata.get("size"),
                    "match_count": 0, "best_score": 0.0,
                    "snippets": [], "pages": [], "sections": [], "evidence": [],
                    "file_set_match": True, "metadata_match": True,
                }

        logical_paths = list(groups)
        state_paths = logical_paths + [item["physical_path"] for item in groups.values()]
        states = storage.get_file_states(scan_id, state_paths)
        for group in groups.values():
            state = states.get(group["path"]) or states.get(group["physical_path"]) or {}
            group["status"] = state.get("status") or "indexed"
            group["analysis_level"] = "deep" if group["status"] == "completed" else "preview"
            group["retryable"] = state.get("retryable")
            group["error"] = state.get("error") if group["status"] == "failed" else None
            group["best_score"] = round(group["best_score"], 6)
            group["pages"] = sorted(group["pages"], key=lambda value: str(value))[:20]
            group["sections"] = group["sections"][:20]
            group["evidence"] = group["evidence"][:3]

        matched_files = sorted(
            groups.values(),
            key=lambda item: (-float(item.get("best_score") or 0), item.get("path") or ""),
        )
        matched_count = len(matched_files)
        evidence_count = len(candidates)
        scope_file_count = storage.count_inventory_files(
            scan_id,
            scope=retrieval_scope,
            source_paths=member_paths,
        ) if member_paths or retrieval_scope != "." else storage.count_inventory_files(scan_id)
        deep_file_count = sum(1 for item in matched_files if item.get("analysis_level") == "deep")
        start = (page - 1) * page_size
        page_items = matched_files[start:start + page_size]
        index_state = storage.get_search_index_state(scan_id) or {}
        index_complete = str(index_state.get("status") or "").lower() in {"completed", "ready", "complete"}
        truncated = len(candidates) >= 5000
        warnings = []
        if not index_complete:
            warnings.append("当前搜索索引尚未完成，结果可能只覆盖已建立索引的文件。")
        elif truncated:
            warnings.append("证据片段达到 5000 条展示上限；文件数量来自完整文件级索引，片段可按文件继续查看。")
        conclusion = "检索“{}”共找到 {} 个相关文件，包含 {} 条相关证据。".format(
            query[:120], matched_count, evidence_count
        )
        if not matched_count:
            conclusion = "当前搜索范围内暂未找到包含“{}”的文件。".format(query[:120])
        return jsonify({
            "ok": True,
            "query": query,
            "scope": scope_key,
            "node_id": node_id or None,
            "node_name": node_name,
            "conclusion": conclusion,
            "answer": {
                "query": query,
                "conclusion": conclusion,
                "matched_file_count": matched_count,
                "scope_file_count": scope_file_count,
                "evidence_count": evidence_count,
                "top_files": [
                    {"path": item["path"], "name": item["name"], "match_count": item["match_count"], "best_score": item["best_score"]}
                    for item in matched_files[:5]
                ],
                "evidence": [
                    evidence
                    for item in matched_files[:5]
                    for evidence in item.get("evidence", [])[:1]
                ],
            },
            "matched_file_count": matched_count,
            "displayed_file_count": len(page_items),
            "evidence_count": evidence_count,
            "matched_files": page_items,
            "page": page,
            "page_size": page_size,
            "has_more": start + page_size < matched_count,
            "next_page": page + 1 if start + page_size < matched_count else None,
            "file_set_complete": bool(index_complete),
            "evidence_window_truncated": bool(truncated),
            "index_mode": index_mode,
            "coverage": {
                "complete": index_complete and not truncated,
                "indexed_evidence": evidence_index_count,
                "candidate_evidence": evidence_count,
                "scope_file_count": scope_file_count,
                "deep_matched_file_count": deep_file_count,
                "index_state": index_state,
            },
            "warnings": warnings,
        })
    except ValueError as exc:
        return api_error(str(exc), 404 if "扫描任务不存在" in str(exc) else 400)


@app.route("/api/ask", methods=["POST"])
def ask_numeric():
    """Answer bounded, exact numeric questions from structured profiles.

    This endpoint deliberately refuses unsupported free-form questions instead
    of inventing an answer. Every returned value carries source/table/row-range
    evidence so it can be checked in the original data.
    """
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = payload.get("scan_id", "")
        scan_result = require_scan(scan_id)
        documents = _package_documents(scan_id, canonical_only=True)
        node_id = payload.get("node_id")
        if node_id:
            node = _find_analysis_node(scan_id, node_id)
            member_paths = set(node.get("member_paths") or [])
            documents = [item for item in documents if item.get("path") in member_paths]
        elif payload.get("path"):
            member_paths = set(_requested_member_paths(scan_result, payload.get("path") or "."))
            documents = [item for item in documents if item.get("path") in member_paths]
        result = answer_question(payload.get("question", ""), documents)
        result["scope"] = "node:{}".format(node_id) if node_id else (payload.get("path") or ".")
        return jsonify({"ok": True, "answer": result})
    except ValueError as exc:
        return api_error(str(exc), 400)


def _job_api_view(source, include_blocker=True, compact=False):
    job = dict(source or {})
    if compact:
        result = job.get("result")
        if isinstance(result, dict):
            allowed_result_fields = {
                "scan_id", "scan_available", "file_name", "download_url",
                "source_file_count", "selection_count", "segmented", "volumes",
                "translation_job_id", "translation_job_ids", "translation_status", "source_level",
                "full_translation", "continuation_job_id", "phase",
            }
            job["result"] = {
                key: value for key, value in result.items()
                if key in allowed_result_fields
            }
        elif result is not None:
            job["result"] = None
        job.pop("options", None)
    now = time.time()
    heartbeat_at = job.get("heartbeat_at")
    started_at = job.get("started_at")
    job["heartbeat_age_seconds"] = (
        max(0, int(now - float(heartbeat_at))) if heartbeat_at else None
    )
    job["elapsed_seconds"] = (
        max(0, int((job.get("finished_at") or now) - float(started_at)))
        if started_at else 0
    )
    live_window = max(10, int(Config.WORKER_HEARTBEAT_SECONDS * 3))
    job["worker_online"] = bool(
        job.get("status") in {"running", "cancelling"}
        and job.get("heartbeat_age_seconds") is not None
        and job["heartbeat_age_seconds"] <= live_window
    )
    if job.get("status") == "queued":
        running = storage.get_running_job()
        job["queue_position"] = storage.get_queue_position(job.get("id"))
        job["progress"] = max(1, int(job.get("progress") or 0))
        if running and include_blocker:
            same_owner = not _request_owner_id() or running.get("owner_id") == _request_owner_id()
            job["blocking_job"] = {
                "id": running.get("id") if same_owner else None,
                "task_type": running.get("task_type"),
                "status": running.get("status"),
                "progress": running.get("progress"),
                "message": running.get("message") if same_owner else "另一项本地任务正在运行",
                "heartbeat_age_seconds": (
                    max(0, int(now - float(running.get("heartbeat_at"))))
                    if running.get("heartbeat_at") else None
                ),
            }
            ahead = max(0, int(job.get("queue_position") or 1) - 1)
            job["message"] = "排队第 {} 位（前方 {} 个）：当前任务 {}% · {}".format(
                job.get("queue_position") or 1,
                ahead,
                running.get("progress", 0),
                (running.get("message") if same_owner else "本地 Worker 正在处理其他任务") or "处理中",
            )
        else:
            job["message"] = "本地解析任务即将启动"
    return job


@app.route("/api/jobs")
def list_jobs():
    status_filter = str(request.args.get("status", "active") or "active").strip().lower()
    compact = str(request.args.get("compact", "")).strip().lower() in {"1", "true", "yes"}
    if status_filter == "active":
        statuses = ["queued", "running", "cancelling"]
    elif status_filter in {"queued", "running", "cancelling", "completed", "failed", "cancelled"}:
        statuses = [status_filter]
    else:
        statuses = None
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    jobs = storage.list_jobs(owner_id=_request_owner_id(), statuses=statuses, limit=limit)
    return jsonify({
        "ok": True,
        "jobs": [_job_api_view(job, compact=compact) for job in jobs],
        "server_time": time.time(),
    })


@app.route("/api/jobs/<job_id>")
def get_job(job_id):
    job = storage.get_job(job_id, owner_id=_request_owner_id())
    if not job:
        return api_error("分析任务不存在", 404)
    job = _job_api_view(job)
    return jsonify({"ok": True, "job": job})


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    job = storage.get_job(job_id, owner_id=_request_owner_id())
    if not job:
        return api_error("分析任务不存在", 404)
    if job.get("status") in {"completed", "failed", "cancelled"}:
        return jsonify({"ok": True, "job": job, "cancelled": job.get("status") == "cancelled"})
    package_pause = None
    if job.get("task_type") in {"scan_and_analyze", "analyze_package"}:
        package_pause = storage.pause_package_analysis_jobs(
            job.get("scan_id"),
            reason="用户结束本次运行；已完成检查点保留。",
        )
        job = storage.get_job(job_id, owner_id=_request_owner_id())
    else:
        job = storage.cancel_job(job_id)
    return jsonify({
        "ok": True,
        "job": _job_api_view(job),
        "cancel_requested": True,
        "cancelled": bool(job and job.get("status") == "cancelled"),
        "package_pause": package_pause,
    })


@app.route("/api/analyze-package", methods=["POST"])
def rerun_package_analysis():
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = payload.get("scan_id", "")
        scan_result = require_scan(scan_id)
        control = storage.get_package_processing_control(scan_id)
        if control.get("state") == "awaiting_selection" and not payload.get("target_paths"):
            return api_error("当前数据包已完成预处理，请先确认分析范围", 409)
        if payload.get("parse_mode") in {"auto", "fast", "accurate"}:
            scan_result["parse_mode"] = payload["parse_mode"]
            storage.update_scan(scan_id, scan_result)
        storage.set_package_processing_state(scan_id, "running", "用户重新执行完整分析")
        job_id, created = _start_analysis_job(scan_id)
        return jsonify({"ok": True, "job_id": job_id, "reused_active_job": not created})
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/retry-failed/<scan_id>", methods=["POST"])
def retry_failed(scan_id):
    """Requeue failed file parses from the last checkpoint, optionally scoped."""
    try:
        require_scan(scan_id)
        payload = request.get_json(silent=True) or {}
        failed = [item for item in storage.list_file_states(scan_id) if item.get("status") == "failed"]
        requested = payload.get("target_paths")
        failed_paths = {item.get("node_path") for item in failed if item.get("node_path")}
        target_paths = sorted(failed_paths & set(requested)) if isinstance(requested, list) else sorted(failed_paths)
        if not target_paths:
            return jsonify({"ok": True, "accepted": False, "message": "当前没有可重试的失败文件", "failed_files": 0})
        options = {
            "target_paths": target_paths, "scope_label": "失败文件重试",
            "retry_failed": True, "workflow_source": "manual_selection",
        }
        storage.set_package_processing_state(scan_id, "running", "用户重试失败文件")
        job_id, created = storage.create_or_get_typed_job(scan_id, "analyze_package", options=options, owner_id=_request_owner_id() or "legacy")
        return jsonify({"ok": True, "accepted": True, "job_id": job_id, "reused_active_job": not created, "retry_files": len(target_paths), "status_url": "/api/jobs/{}".format(job_id)}), 202
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/analyze-scope", methods=["POST"])
def analyze_scope():
    """Extend a large-package analysis only where the user has shown interest."""
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = payload.get("scan_id", "")
        scan_result = require_scan(scan_id)
        node_id = payload.get("node_id")
        if node_id:
            node = _find_analysis_node(scan_id, node_id)
            member_paths = list(node.get("member_paths") or [])
            label = node.get("name") or "主题节点"
        else:
            node_path = payload.get("path", ".")
            member_paths = _requested_member_paths(scan_result, node_path)
            label = node_path
        if not member_paths:
            raise ValueError("当前节点没有可补充分析的文件")
        job_id, created = _start_analysis_job(scan_id, {
            "target_paths": member_paths,
            "scope_label": label,
            "parse_mode": "accurate" if payload.get("parse_mode") != "fast" else "fast",
            "workflow_source": "manual_selection",
        })
        return jsonify({
            "ok": True, "job_id": job_id, "reused_active_job": not created,
            "scope_label": label, "requested_files": len(member_paths),
            "batch_limit": Config.LARGE_PACKAGE_DEEPEN_BATCH_FILES,
        })
    except ValueError as exc:
        return api_error(str(exc), 400)


def _summary_cache_valid(summary, summary_type, require_healthy=False, require_model=False):
    """Validate a summary for the requested depth and evidence contract.

    A local parser preview is useful for browsing, but it is not a substitute
    for the explicit "model deep summary" action.  ``require_model`` keeps
    those two products separate and also invalidates summaries written by old
    releases that did not record their provenance.
    """
    if not summary or summary.get("schema_version") not in {3, 4}:
        return False
    if require_healthy and bool((summary.get("parser_info") or {}).get("degraded")):
        return False
    if require_model:
        generated_by = str(summary.get("generated_by") or "").strip().lower()
        analysis_depth = str(summary.get("analysis_depth") or "").strip().lower()
        if summary_type == "file" and generated_by in {"model-preview-analysis", "model-preview-batch-analysis", "model-candidate-summary", "model-preliminary-analysis", "local-preliminary-fallback"} and analysis_depth in {"preliminary_document", "preview_document", "preview"}:
            return bool(str(summary.get("core_summary") or summary.get("summary") or "").strip())
        if summary_type == "folder" and generated_by in {"model-node-overview", "model-preview-node-summary"} and analysis_depth in {"node_overview", "preview_folder"}:
            return bool(str(summary.get("core_summary") or summary.get("summary") or "").strip())
        if generated_by not in {"model-deep-analysis", "model"}:
            return False
        expected_depth = "deep_folder" if summary_type == "folder" else "deep_document"
        if analysis_depth not in {expected_depth, "deep"}:
            return False
        if not str(summary.get("core_summary") or summary.get("summary") or "").strip():
            return False
    if summary_type == "folder":
        return summary.get("evidence_contract") == "question-answer-evidence/3.0"
    return True


def _summary_target_node(scan_result, node_path):
    """Find a summary target in either the physical or logical inventory."""
    node = _physical_inventory_node(scan_result, node_path)
    if node:
        return node
    if scan_result.get("inventory_mode") == "durable_paged_v1":
        node = storage.get_inventory_entry(
            scan_result.get("_scan_id") or scan_result.get("scan_id"),
            str(node_path or ""),
        )
    else:
        node = _inventory_by_path(scan_result).get(str(node_path or ""))
    if node and node.get("logical_unit"):
        resolve_under(scan_result["root"], node.get("container_path") or "")
        return node
    return None


@app.route("/api/summary", methods=["POST"])
def summarize():
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = payload.get("scan_id", "")
        node_path = payload.get("path", ".")
        kind = payload.get("kind", "file")
        node_id = payload.get("node_id")
        force = bool(payload.get("force"))
        scan_result = require_scan(scan_id)

        # Cache hits and local-only fallbacks stay synchronous. Any uncached
        # model-backed request is persisted and handled by the dedicated Worker.
        if llm_generation_enabled and not _summary_worker_execution.get():
            if node_id:
                node = _find_analysis_node(scan_id, node_id)
                if node.get("kind") != "group":
                    raise ValueError("只有主题或子方向节点可以生成节点摘要")
                cached = storage.get_summary(scan_id, "node:{}".format(node_id), "folder")
                if force or not _summary_cache_valid(
                    cached, "folder", require_model=llm_generation_enabled
                ):
                    require_local_model_enabled()
                    job_id, created = storage.create_or_get_typed_job(
                        scan_id, "generate_summary", options=payload, owner_id=_request_owner_id() or "legacy"
                    )
                    return jsonify({
                        "ok": True, "accepted": True, "job_id": job_id,
                        "reused_active_job": not created,
                        "status_url": "/api/jobs/{}".format(job_id),
                    }), 202
            else:
                physical_node = _summary_target_node(scan_result, node_path)
                if not physical_node:
                    raise ValueError("节点不在本次安全清点范围内")
                summary_type = "folder" if physical_node.get("kind") == "directory" or kind == "directory" else "file"
                cached = storage.get_summary(scan_id, node_path, summary_type)
                if force or not _summary_cache_valid(
                    cached,
                    summary_type,
                    require_healthy=True,
                    require_model=llm_generation_enabled,
                ):
                    require_local_model_enabled()
                    job_id, created = storage.create_or_get_typed_job(
                        scan_id, "generate_summary", options=payload, owner_id=_request_owner_id() or "legacy"
                    )
                    return jsonify({
                        "ok": True, "accepted": True, "job_id": job_id,
                        "reused_active_job": not created,
                        "status_url": "/api/jobs/{}".format(job_id),
                    }), 202

        if node_id:
            node = _find_analysis_node(scan_id, node_id)
            if node.get("kind") != "group":
                raise ValueError("只有主题或子方向节点可以生成节点摘要")
            cache_path = "node:{}".format(node_id)
            cached = storage.get_summary(scan_id, cache_path, "folder")
            if _summary_cache_valid(
                cached,
                "folder",
                require_healthy=True,
                require_model=llm_generation_enabled,
            ) and not force:
                return jsonify({"ok": True, "summary": cached, "cached": True, "degraded": bool(cached.get("parser_info", {}).get("degraded"))})

            context = _virtual_node_context(scan_id, node)
            if not context.get("file_summaries_complete"):
                missing = context.get("missing_file_summaries") or []
                raise ValueError(
                    "节点文件摘要尚未全部生成，暂不能生成节点摘要{}".format(
                        "（缺少 {} 个文件摘要）".format(len(missing)) if missing else ""
                    )
                )
            local_summary = _virtual_node_summary(scan_id, node, context)
            if llm_generation_enabled:
                require_local_model_enabled()
                generated, result, batch_errors = analyze_folder(llm, context, node.get("name") or cache_path)
                generated.update({
                    "title": generated.get("title") or local_summary["title"],
                    "member_paths": local_summary["member_paths"],
                    "representative_documents": local_summary["representative_documents"],
                    "conclusion_evidence": local_summary["conclusion_evidence"],
                    "evidence_chain": generated.get("evidence") or local_summary["evidence_chain"],
                    "file_count": local_summary["file_count"],
                })
                generated["parser_info"] = {
                    **local_summary["parser_info"],
                    "local_model": result.get("model"),
                    "usage": result.get("usage", {}),
                    "batch_errors": batch_errors,
                    "degraded": bool(batch_errors or not result.get("model")),
                }
                generated["generated_by"] = (
                    "model-preview-node-summary" if result.get("model") else "local-fallback"
                )
                generated["analysis_depth"] = "preview_folder"
                generated["analysis_level"] = "preview"
                generated["deep_analysis"] = False
                summary = generated
            else:
                summary = local_summary
                summary["warnings"] = ["模型深度摘要未启用，已返回主题的本地结论—证据链。"]
                summary["generated_by"] = "local-fallback"
                summary["analysis_depth"] = "fallback"
                summary["deep_analysis"] = False

            summary["node_path"] = cache_path
            summary["summary_type"] = "folder"
            summary["schema_version"] = 4
            summary["summary"] = summary.get("summary") or summary.get("core_summary")
            if str(summary.get("generated_by") or "") == "model-preview-node-summary":
                summary["analysis_level"] = "preview"
                summary["analysis_depth"] = "preview_folder"
                summary["deep_analysis"] = False
            summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
            storage.save_summary(scan_id, cache_path, "folder", summary)
            return jsonify({"ok": True, "summary": summary, "cached": False, "degraded": bool(summary.get("parser_info", {}).get("degraded"))})

        physical_node = _summary_target_node(scan_result, node_path)
        if not physical_node:
            raise ValueError("节点不在本次安全清点范围内")
        selected = Path(scan_result["root"]) / str(
            physical_node.get("container_path")
            if physical_node.get("logical_unit") else node_path
        )
        summary_type = "folder" if physical_node.get("kind") == "directory" or kind == "directory" else "file"
        local_only = not llm_generation_enabled
        if not local_only:
            require_local_model_enabled()
        cached = storage.get_summary(scan_id, node_path, summary_type)
        if _summary_cache_valid(
            cached,
            summary_type,
            require_healthy=True,
            require_model=llm_generation_enabled,
        ) and not force:
            # Lazily upgrade summaries persisted before the file-claims contract.
            # Existing imports immediately gain source-verified conclusions without
            # requiring users to rerun the whole scan.
            if summary_type == "file" and cached.get("claim_contract") != "file-claims/1.0":
                legacy_document = storage.get_document(scan_id, node_path)
                if legacy_document:
                    cached.update(build_file_claims(cached, legacy_document.get("evidence", [])))
                    storage.save_summary(scan_id, node_path, summary_type, cached)
            degraded = bool(cached.get("parser_info", {}).get("degraded"))
            return jsonify({"ok": True, "summary": cached, "cached": True, "degraded": degraded})
        if summary_type == "folder" and local_only:
            from services.folder_analysis import _fallback as folder_fallback
            context = _folder_summary_context(scan_id, node_path, scan_result)
            summary = folder_fallback(context, node_path, ["模型生成未启用，已返回本地证据摘要。"])
            result = {"model": None, "usage": {}}
            batch_errors = ["模型生成未启用"]
            summary["parser_info"] = {
                "total_files": context["total_files"], "total_dirs": context["total_dirs"],
                "total_size": context["total_size_human"], "type_counts": context["type_counts"],
                "sampled_files": context["sampled_files"], "sample_truncated": context["sample_truncated"],
                "coverage": context.get("coverage", {}), "local_model": None, "usage": {},
                "batch_errors": batch_errors, "degraded": True,
            }
            summary["generated_by"] = "local-fallback"
            summary["analysis_depth"] = "fallback"
            summary["deep_analysis"] = False
        elif summary_type == "folder":
            context = _folder_summary_context(scan_id, node_path, scan_result)
            summary, result, batch_errors = analyze_folder(llm, context, node_path)
            summary["parser_info"] = {
                "total_files": context["total_files"],
                "total_dirs": context["total_dirs"],
                "total_size": context["total_size_human"],
                "type_counts": context["type_counts"],
                "sampled_files": context["sampled_files"],
                "sample_truncated": context["sample_truncated"],
                "coverage": context.get("coverage", {}),
                "local_model": result.get("model"),
                "usage": result.get("usage", {}),
                "batch_errors": batch_errors,
                "degraded": bool(batch_errors or not result.get("model")),
            }
            summary["generated_by"] = (
                "model-deep-analysis" if result.get("model") else "local-fallback"
            )
            summary["analysis_depth"] = "deep_folder"
            summary["deep_analysis"] = bool(result.get("model"))
        else:
            unified_document = storage.get_document(scan_id, node_path)
            # In large-package mode the first pass stores a bounded projection.
            # A deliberate file-level deep read upgrades only this file, keeps
            # the package responsive, and immediately refreshes coverage.
            package_analysis = storage.get_analysis(scan_id) or {}
            is_large_package = bool(
                (((package_analysis.get("policy") or {}).get("large_package") or {}).get("enabled"))
            )
            document_parser = (unified_document or {}).get("parser") or {}
            document_coverage = (unified_document or {}).get("coverage") or {}
            source_extension = Path(node_path).suffix.lower()
            structured_source_needs_upgrade = source_extension in {".json", ".jsonl", ".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".ods"} and document_parser.get("name") != "structured-json"
            needs_deep_parse = structured_source_needs_upgrade or (is_large_package and document_parser.get("name") != "structured-json" and (
                document_parser.get("mode") == "fast"
                or document_parser.get("fast_preview")
                or document_coverage.get("limited_by_fast_mode")
                or document_coverage.get("overview_sampled")
            ))
            source_node = (
                storage.get_inventory_entry(scan_id, node_path)
                if scan_result.get("inventory_mode") == "durable_paged_v1"
                else _inventory_by_path(scan_result).get(node_path)
            )
            if not source_node:
                raise ValueError("文件不在本次安全清点范围内")
            if needs_deep_parse or not unified_document:
                deep_mode = "accurate"
                with _logical_source_snapshot(scan_result["root"], source_node) as snapshot:
                    unified_document = _parse_with_limits(parser, snapshot, node_path, mode=deep_mode)
                _restore_source_provenance(unified_document, scan_result["root"], source_node)
                storage.save_document(scan_id, node_path, unified_document)
                storage.set_file_state(
                    scan_id, node_path,
                    checkpoint_fingerprint(source_node, parser, deep_mode, unified_document),
                    "completed",
                    document=unified_document,
                )
                refresh_package_coverage(scan_id, scan_result, storage)
            # Preserve the complete parsed text. Shared-model limits apply to
            # each token-budgeted chunk, never by truncating the document head.
            model_max_chars = Config.MAX_FULL_DOCUMENT_CHARS
            try:
                if local_only:
                    raise LocalModelError("模型生成未启用")
                summary, coverage, result = analyze_document(
                    llm,
                    selected,
                    node_path,
                    max_chars=model_max_chars,
                    max_chunks=Config.MAX_DOCUMENT_CHUNKS,
                    unified_document=unified_document,
                    preferred_chunk_chars=min(42000, Config.SHARED_OLLAMA_MAX_CHARS),
                    context_window_tokens=Config.LLM_CONTEXT_TOKENS,
                )
            except LocalModelError as exc:
                summary = _local_document_fallback(unified_document, node_path, str(exc))
                coverage_data = (unified_document or {}).get("coverage", {})
                coverage = {
                    "parser": (unified_document or {}).get("parser", {}).get("name", "本地解析"),
                    "extracted_chars": min(len((unified_document or {}).get("text", "")), model_max_chars),
                    "document_chunks": 0,
                    "successfully_analyzed_chunks": 0,
                    "failed_chunks": [1],
                    "local_limit_truncated": not coverage_data.get("complete", True),
                    "metadata": {"coverage": coverage_data},
                    "warnings": list((unified_document or {}).get("warnings", [])),
                }
                result = {"model": None, "usage": {}}
            # Attach the file-level claim contract after both model and local fallback paths.
            # This keeps the formal conclusion view source-verified and backward compatible
            # with the existing evidence_chain field.
            if unified_document:
                summary.update(build_file_claims(summary, unified_document.get("evidence", [])))
            summary["parser_info"] = {
                "parser": coverage["parser"],
                "char_count": coverage["extracted_chars"],
                "document_chunks": coverage["document_chunks"],
                "successfully_analyzed_chunks": coverage.get("successfully_analyzed_chunks", coverage["document_chunks"]),
                "failed_chunks": coverage.get("failed_chunks", []),
                "truncated": coverage["local_limit_truncated"],
                "metadata": coverage["metadata"],
                "warnings": coverage["warnings"],
                "coverage": coverage.get("metadata", {}).get("coverage", {}),
                "local_model": result["model"],
                "usage": result["usage"],
                "model_calls": coverage.get("model_calls", []),
                "degraded": bool(
                    coverage.get("failed_chunks")
                    or any(item.get("output_truncated") for item in coverage.get("model_calls", []))
                    or coverage.get("local_limit_truncated")
                    or not (coverage.get("metadata", {}).get("coverage", {}).get("complete", True))
                    or not result.get("model")
                ),
            }
            summary["generated_by"] = (
                "model-deep-analysis" if result.get("model") else "local-fallback"
            )
            summary["analysis_depth"] = "deep_document"
            summary["deep_analysis"] = bool(result.get("model"))
        summary["node_path"] = node_path
        summary["summary_type"] = summary_type
        summary["schema_version"] = 4
        summary["summary"] = summary.get("summary") or summary.get("core_summary")
        summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
        workflow_source = str(payload.get("workflow_source") or "")
        if summary_type == "file" and workflow_source in {"idle_deep_model_summary", "deep_parse_model_summary", "large_selection_model_summary"}:
            storage.save_summary(scan_id, node_path, staged_summary_type("deep", "file"), summary)
        elif summary_type == "folder" and workflow_source == "deep_node_rebuild":
            storage.save_summary(scan_id, node_path, staged_summary_type("deep", "node"), summary)
        else:
            storage.save_summary(scan_id, node_path, summary_type, summary)
        degraded = bool(summary.get("parser_info", {}).get("degraded"))
        return jsonify({"ok": True, "summary": summary, "cached": False, "degraded": degraded})
    except (ValueError, LocalModelError) as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        return api_error("摘要生成失败", 500, str(exc))


@app.route("/api/report", methods=["POST"])
def report():
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = payload.get("scan_id", "")
        require_scan(scan_id)
        job_id, created = storage.create_or_get_typed_job(scan_id, "generate_report", owner_id=_request_owner_id() or "legacy")
        return jsonify({
            "ok": True, "accepted": True, "job_id": job_id,
            "reused_active_job": not created,
            "status_url": "/api/jobs/{}".format(job_id),
        }), 202
    except (ValueError, LocalModelError, RuntimeError) as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.exception("报告生成失败")
        return api_error("报告生成失败", 500, str(exc))


@app.route("/api/export", methods=["POST"])
def export():
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = payload.get("scan_id", "")
        require_scan(scan_id)
        selections = payload.get("selections")
        if selections is not None and (not isinstance(selections, list) or not selections):
            raise ValueError("请至少选择一个主题、目录、文档或证据")
        options = {
            "selections": selections,
            "path": payload.get("path"),
            "kind": payload.get("kind"),
            "node_id": payload.get("node_id"),
            "name": payload.get("name"),
            "task_topic": str(payload.get("task_topic") or "").strip(),
        }
        if not options["task_topic"]:
            raise ValueError("请输入整编任务主题")
        job_id = storage.create_job(scan_id, options=options, task_type="export_package", owner_id=_request_owner_id() or "legacy")
        return jsonify({
            "ok": True, "accepted": True, "job_id": job_id,
            "status_url": "/api/jobs/{}".format(job_id),
        }), 202
    except ValueError as exc:
        return api_error(str(exc), 400)


@app.route("/api/download-ticket", methods=["POST"])
def create_download_ticket():
    """Create a one-use URL so large files stream through browser navigation."""
    payload = request.get_json(silent=True) or {}
    filename = str(payload.get("filename") or "").strip()
    if not filename or Path(filename).name != filename:
        return api_error("非法输出文件路径", 400)
    output_path = Config.OUTPUT_DIR / filename
    if output_path.is_symlink() or not output_path.is_file():
        return api_error("输出文件不存在或已被清理", 404)
    owner_id = _request_owner_id() or Config.OWNER_ID
    if storage.artifact_owner(filename) != owner_id:
        return api_error("输出文件不存在或不属于当前访问用户", 404)
    ticket = storage.create_download_ticket(
        filename, owner_id, ttl_seconds=Config.DOWNLOAD_TICKET_TTL_SECONDS
    )
    return jsonify({
        "ok": True,
        "download_url": "/outputs/{}?ticket={}".format(
            quote(filename, safe=""), quote(ticket, safe="")
        ),
        "expires_in_seconds": Config.DOWNLOAD_TICKET_TTL_SECONDS,
    })


@app.route("/outputs/<path:filename>")
def outputs(filename):
    # Output artifacts may contain original user资料; require an ownership
    # record in addition to the global API token and reject path tricks.
    if Path(filename).name != filename:
        return api_error("非法输出文件路径", 404)
    owner_id = _request_owner_id() or Config.OWNER_ID
    output_path = Config.OUTPUT_DIR / filename
    if output_path.is_symlink() or not output_path.is_file():
        return api_error("输出文件不存在或已被清理", 404)
    registered_owner = storage.artifact_owner(filename)
    # Older reports may predate output_artifacts registration. In this
    # single-token deployment, bind an existing unregistered file to the
    # configured authenticated owner on first access; never rebind a file
    # that is already registered to a different owner.
    if registered_owner is None and owner_id:
        storage.save_artifact(filename, owner_id, kind="legacy_result")
        registered_owner = owner_id
    if registered_owner != owner_id:
        return api_error("输出文件不存在或不属于当前访问用户", 404)
    ticket = str(request.args.get("ticket") or "")
    if ticket:
        if not storage.consume_download_ticket(ticket, filename, owner_id):
            return api_error("下载票据无效、已使用或已过期，请重新发起下载", 401)
    return send_from_directory(str(Config.OUTPUT_DIR), filename, as_attachment=True)





@app.route("/api/scan/<scan_id>/deep-selection", methods=["GET", "POST"])
@app.route("/api/scan/<scan_id>/deep-selection/confirm", methods=["POST"])
def deep_selection_contract(scan_id):
    """Select files/nodes from the candidate directory for true deep analysis."""
    try:
        scan_result = require_scan(scan_id)
        if request.method == "GET":
            task = storage.get_import_task(scan_id) or {}
            checkpoint = task.get("checkpoint") or {}
            return jsonify({
                "ok": True, "scan_id": scan_id,
                "status": task.get("status"),
                "selected_paths": checkpoint.get("deep_selected_paths") or [],
                "selection": checkpoint.get("deep_selection") or {},
                "coverage": (storage.get_analysis(scan_id) or {}).get("coverage") or {},
            })
        payload = request.get_json(silent=True) or {}
        node_id = str(payload.get("node_id") or "").strip()
        requested = list(payload.get("paths") or payload.get("target_paths") or [])
        label = str(payload.get("label") or "深度摘要选择").strip()
        large_directory_mode = build_policy(
            scan_result, _package_large_options()
        ).get("mode") == "large_directory"
        selected_node = None
        if node_id:
            node = _find_analysis_node(scan_id, node_id)
            selected_node = node
            selection_filter = node.get("selection_filter") or {}
            if selection_filter.get("kind") == "extension" and not large_directory_mode:
                wanted_extension = str(selection_filter.get("extension") or "").casefold()
                requested = [
                    str(item.get("path") or "")
                    for item in storage.iter_inventory_entries(scan_id, kind="file")
                    if str((item.get("payload") or {}).get("extension") or "[no_extension]").casefold() == wanted_extension
                ]
            else:
                requested = list(node.get("member_paths") or [])
            label = node.get("name") or label
        elif payload.get("path"):
            if large_directory_mode:
                selected_node = {
                    "node_id": "path-" + hashlib.sha1(
                        str(payload.get("path") or ".").encode("utf-8", "ignore")
                    ).hexdigest()[:12],
                    "kind": "group",
                    "name": str(payload.get("path") or label),
                    "selection_filter": {
                        "kind": "path_prefix",
                        "path": str(payload.get("path") or "."),
                    },
                    "member_paths_lazy": True,
                }
                requested = []
            else:
                requested = _requested_member_paths(scan_result, payload.get("path"))
            label = str(payload.get("path") or label)
        if large_directory_mode:
            # Resolve a category/type node on the server and turn it into a
            # bounded, resumable plan. Do not send the full matching path set
            # through the legacy deep-selection branch.
            plan = _build_large_selection_plan(
                scan_id,
                node=selected_node,
                requested_paths=(requested if not selected_node else None),
                label=label,
            )
            normal_paths = list(plan.get("parse_paths") or [])
            model_paths = list(plan.get("model_paths") or [])
            if not normal_paths:
                raise ValueError("所选范围没有落在当前预算内的可解析文件")
            selection = {
                "kind": "node" if node_id else "files",
                "node_id": node_id or None,
                "label": label,
                "paths": normal_paths,
                "selected_file_count": int(plan.get("selected_file_count") or 0),
                "selected_bytes": int(plan.get("selected_bytes") or 0),
                "normal_parse_file_count": len(normal_paths),
                "model_candidate_file_count": len(model_paths),
                "deferred_file_count": int(plan.get("deferred_file_count") or 0),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "plan_schema_version": plan.get("schema_version"),
            }
            batch_id = storage.create_deep_parse_batch(
                scan_id, normal_paths, created_by="user",
                selection_rule={
                    "kind": "large_bounded_selection",
                    "node_id": node_id or None,
                    "label": label,
                    "plan": plan,
                },
                priority=130,
            )
            storage.prioritize_file_workflow_states(
                scan_id, normal_paths, "large_selection", "大数据包用户选择的有界解析范围", score_boost=3000.0,
            )
            task = storage.get_import_task(scan_id) or {}
            checkpoint = dict(task.get("checkpoint") or {})
            checkpoint.update({
                "deep_selection": selection,
                "deep_selected_paths": normal_paths,
                "deep_batch_id": batch_id,
                "selection_plan": plan,
                "large_selection": True,
            })
            storage.update_import_task(
                scan_id, status="deep_parsing", phase="deep_parsing", checkpoint=checkpoint
            )
            storage.set_package_processing_state(
                scan_id, "running",
                "已按预算选择 {} 个文件解析，其中 {} 个进入模型摘要；另有 {} 个文件保留待后续处理。".format(
                    len(normal_paths), len(model_paths), int(plan.get("deferred_file_count") or 0)
                ),
            )
            options = {
                "target_paths": normal_paths,
                "model_summary_paths": model_paths,
                "workflow_source": "large_selection",
                "scope_label": label,
                "parse_mode": "fast",
                "continue_full": False,
                "selection_version": int((storage.get_scan_selection(scan_id) or {}).get("version") or 1),
                "deep_batch_id": batch_id,
                "selection_plan": plan,
            }
            job_id, created = storage.create_or_get_typed_job(
                scan_id, "analyze_package", options=options,
                owner_id=_request_owner_id() or "legacy",
            )
            storage.update_analysis_progress_status(
                scan_id, "queued", "已生成大数据包有界解析计划，等待 Worker 执行。", "deep_parsing"
            )
            return jsonify({
                "ok": True, "accepted": True, "large_package": True,
                "job_id": job_id, "deep_batch_id": batch_id,
                "selected_count": int(plan.get("selected_file_count") or 0),
                "selected_bytes": int(plan.get("selected_bytes") or 0),
                "normal_parse_count": len(normal_paths),
                "model_candidate_count": len(model_paths),
                "deferred_count": int(plan.get("deferred_file_count") or 0),
                "selection_plan": plan,
                "selected_paths": normal_paths[:100],
                "selected_paths_truncated": len(normal_paths) > 100,
                "reused_active_job": not created,
                "status_url": "/api/jobs/{}".format(job_id),
            }), 202 if created else 200
        requested = list(dict.fromkeys(str(path) for path in requested if str(path)))
        workflow_by_path = {item.get("node_path"): item for item in storage.iter_file_workflow_states(scan_id)}
        states_by_path = {item.get("node_path"): item for item in storage.iter_file_states(scan_id)}
        eligible = []
        for path in requested:
            summary = storage.get_summary(scan_id, path, "file") or {}
            is_deep = str(summary.get("analysis_level") or "").lower() == "deep" and bool(summary.get("deep_analysis"))
            if is_deep:
                continue
            workflow = workflow_by_path.get(path) or {}
            state = states_by_path.get(path) or {}
            preview_candidate = str(summary.get("analysis_level") or "").lower() == "preview"
            if preview_candidate or deep_processing_eligible(workflow, state):
                eligible.append(path)
        if not eligible:
            already_deep = []
            for path in requested:
                cached = storage.get_summary(scan_id, path, "file") or {}
                if str(cached.get("analysis_level") or "").lower() == "deep" and cached.get("deep_analysis"):
                    already_deep.append(path)
            if already_deep and len(already_deep) == len(requested):
                return jsonify({
                    "ok": True, "accepted": False, "already_deep": True,
                    "selected_count": 0, "selected_paths": already_deep,
                    "message": "所选文件已经完成深度摘要。",
                })
            raise ValueError("所选文件已经完成深度摘要，或没有可处理文件")
        selection = {
            "kind": "node" if node_id else "files",
            "node_id": node_id or None,
            "label": label,
            "paths": eligible,
            "selected_file_count": len(eligible),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        batch_id = storage.create_deep_parse_batch(
            scan_id, eligible, created_by="user",
            selection_rule={"kind": "deep_selection", "node_id": node_id or None, "label": label},
            priority=130,
        )
        storage.prioritize_file_workflow_states(scan_id, eligible, "manual_selection", "用户选择的深度摘要范围", score_boost=3000.0)
        task = storage.get_import_task(scan_id) or {}
        checkpoint = dict(task.get("checkpoint") or {})
        checkpoint.update({"deep_selection": selection, "deep_selected_paths": eligible, "deep_batch_id": batch_id})
        storage.update_import_task(scan_id, status="deep_parsing", phase="deep_parsing", checkpoint=checkpoint)
        storage.set_package_processing_state(scan_id, "running", "已选择 {} 个文件进入深度摘要。".format(len(eligible)))
        options = {
            "target_paths": eligible,
            "workflow_source": "manual_selection",
            "scope_label": label,
            "parse_mode": "accurate",
            "continue_full": False,
            "selection_version": int((storage.get_scan_selection(scan_id) or {}).get("version") or 1),
            "deep_batch_id": batch_id,
            "deep_selection": selection,
        }
        job_id, created = storage.create_or_get_typed_job(scan_id, "analyze_package", options=options, owner_id=_request_owner_id() or "legacy")
        storage.update_analysis_progress_status(scan_id, "queued", "已提交深度摘要任务，等待 Worker 开始。", "deep_parsing")
        return jsonify({
            "ok": True, "accepted": True, "job_id": job_id, "deep_batch_id": batch_id,
            "selected_count": len(eligible), "selected_paths": eligible,
            "reused_active_job": not created, "status_url": "/api/jobs/{}".format(job_id),
        }), 202 if created else 200
    except ValueError as exc:
        return api_error(str(exc), 400)

@app.route("/api/scan/<scan_id>/formal-directory")
def formal_directory(scan_id):
    """Evidence-gated directory for reports and formal question answering."""
    try:
        require_scan(scan_id)
        analysis = storage.get_analysis(scan_id) or {}
        import_task = storage.get_import_task(scan_id) or {}
        checkpoint = dict(import_task.get("checkpoint") or {})
        selected_scope = {
            str(path) for path in (checkpoint.get("selected_paths") or []) if str(path)
        }
        selection_plan = dict(checkpoint.get("selection_plan") or {})
        strict_selected = bool(selected_scope) and not bool(
            checkpoint.get("large_selection") or selection_plan.get("schema_version")
        )
        _published_paths, deep_published = _published_deep_scope(scan_id)
        # Deep artifacts are available in the file/node detail panels as soon
        # as they finish.  The formal directory is a published product: for a
        # strict selected import it must keep the last parsed/preliminary view
        # until the user explicitly applies the deep-evidence update.
        allow_deep_directory = not strict_selected or deep_published
        tree_page = storage.get_tree_page(scan_id, "analysis", limit=5000) or []
        # ``get_tree_page`` returns the root dictionary for the current
        # durable tree index and a row list for older paged indexes.
        tree = (
            tree_page
            if isinstance(tree_page, list)
            else list(_walk_analysis_nodes(tree_page))
        )
        topics, deep_files, evidence_count = [], set(), 0
        # Formal coverage is based on durable model summaries, not parser completion.
        deep_summary_paths = set()
        for summary_item in storage.list_summaries(scan_id):
            if not allow_deep_directory:
                continue
            summary_payload = summary_item.get("payload") or {}
            if not summary_payload.get("deep_analysis"):
                continue
            summary_type = str(summary_item.get("type") or "")
            if summary_type in {"file", staged_summary_type("deep", "file")}:
                if summary_item.get("path"):
                    deep_summary_paths.add(str(summary_item["path"]))
            else:
                deep_summary_paths.update(str(path) for path in (summary_payload.get("member_paths") or []) if path)
        for row in tree:
            node = dict(row.get("payload") or row if isinstance(row, dict) else {})
            staged_node = storage.get_summary(
                scan_id, "node:{}".format(node.get("node_id") or ""),
                staged_summary_type("deep", "node"),
            ) or {}
            if allow_deep_directory and staged_node.get("deep_analysis"):
                node.update(staged_node)
            coverage = node.get("coverage") or {}
            level = str(node.get("analysis_level") or node.get("evidence_level") or "").lower()
            evidence_candidates = list(node.get("evidence_chain") or node.get("evidence") or [])
            # Topic nodes often store their source records under each
            # conclusion rather than a top-level evidence_chain.  Flatten
            # those records so formal-directory readiness follows the same
            # conclusion -> original evidence relationship shown in the file
            # detail page.
            for conclusion in node.get("conclusion_evidence") or node.get("conclusions") or []:
                if isinstance(conclusion, dict):
                    evidence_candidates.extend(conclusion.get("evidence") or conclusion.get("supports") or [])
            evidence = [item for item in evidence_candidates if evidence_is_formal(item)]
            evidence_count += len(evidence)
            members_all = sorted(set(str(path) for path in (node.get("member_paths") or node.get("source_paths") or []) if path))
            members = [path for path in members_all if path in deep_summary_paths]
            readiness = coverage.get("evidence_readiness_coverage") or coverage.get("evidence_readiness") or {}
            ready = bool(members) and bool(
                evidence
                or coverage.get("formal_evidence_ready")
                or level in {"deep", "evidence"}
                or readiness.get("complete")
            )
            if ready: deep_files.update(members)
            if node.get("kind") != "analysis_root" and (
                node.get("kind") in {"topic", "cluster", "group"} or node.get("children")
            ) and ready:
                node_conclusions = (
                    node.get("conclusions")
                    or node.get("conclusion_evidence")
                    or []
                )
                topics.append({
                    "node_id": node.get("node_id") or row.get("node_path"),
                    "name": node.get("name") or node.get("title") or "未命名主题",
                    "summary": node.get("summary") or node.get("description") or "",
                    "research_value": node.get("research_value") or node.get("question_value") or "",
                    "research_questions": node.get("research_questions") or node.get("questions") or [],
                    "member_paths": members,
                    "file_count": len(members),
                    "evidence_count": len(evidence),
                    "conclusions": node_conclusions,
                    "conflicts": node.get("conflicts") or [],
                    "limitations": node.get("limitations") or [],
                    "coverage": coverage,
                    "confidence": "formal",
                    "evidence_status": "supported" if evidence else "deep_pending_validation",
                })

        # Candidate nodes are content-derived and may not exist in the legacy
        # analysis tree. Once every file in such a node is deep-analyzed, expose
        # the same node in the formal directory with its verified claims.
        existing_node_ids = {str(item.get("node_id") or "") for item in topics}
        preview_directory = (
            _build_candidate_preview_directory(scan_id) or {}
            if allow_deep_directory else {}
        )
        selected_plan = dict((import_task.get("checkpoint") or {}).get("selection_plan") or {})
        selected_node_id = str(selected_plan.get("node_id") or "")
        if selected_node_id and selected_node_id not in existing_node_ids:
            selected_preview = next(
                (item for item in (preview_directory.get("topics") or [])
                 if str(item.get("node_id") or "") == selected_node_id),
                None,
            )
            selected_summary = storage.get_summary(
                scan_id, "node:{}".format(selected_node_id), staged_summary_type("deep", "node")
            ) or storage.get_summary(scan_id, "node:{}".format(selected_node_id), "folder") or {}
            if selected_preview and str(selected_summary.get("analysis_level") or "").lower() == "deep":
                selected_evidence = list(
                    selected_summary.get("evidence_chain")
                    or selected_summary.get("evidence")
                    or []
                )
                for claim in selected_summary.get("conclusions") or selected_summary.get("conclusion_evidence") or []:
                    if isinstance(claim, dict):
                        selected_evidence.extend(
                            claim.get("supports") or claim.get("evidence") or claim.get("supporting_evidence") or []
                        )
                selected_evidence = [item for item in selected_evidence if evidence_is_formal(item)]
                selected_members = sorted(set(
                    str(path) for path in (selected_summary.get("member_paths") or []) if path
                ))
                declared_count = int(
                    selected_preview.get("file_count")
                    or selected_plan.get("selected_file_count")
                    or len(selected_members)
                )
                selected_coverage = {
                    **dict(selected_preview.get("coverage") or {}),
                    **dict(selected_summary.get("coverage") or {}),
                    "category_file_count": declared_count,
                    "selected_scope_files": int(selected_plan.get("selected_file_count") or declared_count),
                    "analyzed_scope_files": len(selected_members),
                    "deep_analyzed_files": int(selected_summary.get("deep_analyzed_files") or len(selected_members)),
                    "members_materialized": False,
                    "scope_limited": True,
                    "claim_scope": "模型结论仅适用于选定类别中进入模型的文件，类别计数覆盖完整清单。",
                    "verification_status": "verified" if selected_evidence else "partial",
                }
                deep_files.update(selected_members)
                evidence_count += len(selected_evidence)
                topics.append({
                    "node_id": selected_node_id,
                    "name": selected_preview.get("name") or selected_plan.get("label") or "选定类别",
                    "summary": selected_summary.get("summary") or selected_preview.get("summary") or "",
                    "research_value": selected_summary.get("research_value") or selected_preview.get("research_value") or "",
                    "research_questions": selected_summary.get("research_questions") or selected_preview.get("research_questions") or [],
                    "member_paths": selected_members,
                    "member_paths_lazy": True,
                    "file_count": declared_count,
                    "evidence_count": len(selected_evidence),
                    "conclusions": selected_summary.get("conclusions") or selected_summary.get("conclusion_evidence") or [],
                    "conflicts": selected_summary.get("conflicts") or [],
                    "limitations": selected_summary.get("limitations") or selected_preview.get("limitations") or [],
                    "coverage": selected_coverage,
                    "confidence": "formal",
                    "evidence_status": "supported" if selected_evidence else "deep_pending_validation",
                    "analysis_level": "deep",
                    "verification_status": "verified" if selected_evidence else "partial",
                })
                existing_node_ids.add(selected_node_id)
        for node in preview_directory.get("topics") or []:
            node_id = str(node.get("node_id") or "")
            if not node_id or not node.get("formal") or node_id in existing_node_ids:
                continue
            evidence = []
            for claim in node.get("conclusions") or []:
                evidence.extend(claim.get("supporting_evidence") or [])
            evidence = [item for item in evidence if evidence_is_formal(item)]
            members = sorted(set(str(path) for path in (node.get("member_paths") or []) if path))
            deep_files.update(members)
            evidence_count += len(evidence)
            topics.append({
                "node_id": node_id,
                "name": node.get("name") or "候选主题",
                "summary": node.get("summary") or "",
                "research_value": node.get("research_value") or "",
                "research_questions": node.get("research_questions") or [],
                "member_paths": members,
                "file_count": len(members),
                "evidence_count": len(evidence),
                "conclusions": node.get("conclusions") or [],
                "conflicts": node.get("conflicts") or [],
                "limitations": node.get("limitations") or [],
                "coverage": node.get("coverage") or {},
                "confidence": "formal",
                "evidence_status": "supported" if evidence else "deep_pending_validation",
                "analysis_level": "deep",
                "verification_status": "verified",
            })
        # Strict imports persist node summaries in their own stage table. The
        # legacy analysis tree may still contain an older set of generated node
        # IDs, so do not let that structural index hide completed deep nodes.
        deep_nodes_by_members = {}
        for item in storage.list_summaries(scan_id):
            if not allow_deep_directory:
                continue
            if str(item.get("type") or "") != staged_summary_type("deep", "node"):
                continue
            payload = dict(item.get("payload") or {})
            if not payload.get("deep_analysis"):
                continue
            members = tuple(sorted(set(str(path) for path in (payload.get("member_paths") or []) if str(path))))
            if not members:
                continue
            evidence = list(payload.get("evidence_chain") or payload.get("evidence") or [])
            for claim in payload.get("conclusions") or payload.get("conclusion_evidence") or []:
                if isinstance(claim, dict):
                    evidence.extend(claim.get("supports") or claim.get("evidence") or claim.get("supporting_evidence") or [])
            formal_evidence = [entry for entry in evidence if evidence_is_formal(entry)]
            candidate = (len(formal_evidence), len(str(payload.get("summary") or "")), payload, formal_evidence)
            previous = deep_nodes_by_members.get(members)
            if previous is None or candidate[:2] > previous[:2]:
                deep_nodes_by_members[members] = candidate
        for members, (_score, _length, payload, formal_evidence) in deep_nodes_by_members.items():
            node_id = str(payload.get("node_id") or "")
            if node_id in existing_node_ids:
                continue
            deep_files.update(path for path in members if path in deep_summary_paths)
            evidence_count += len(formal_evidence)
            topics.append({
                "node_id": node_id or "node:" + hashlib.sha1("\0".join(members).encode()).hexdigest()[:16],
                "name": payload.get("title") or payload.get("name") or "深度主题",
                "summary": payload.get("summary") or payload.get("description") or "",
                "research_value": payload.get("research_value") or payload.get("value") or "",
                "research_questions": payload.get("research_questions") or payload.get("questions") or [],
                "member_paths": list(members),
                "file_count": len(members),
                "evidence_count": len(formal_evidence),
                "conclusions": payload.get("conclusions") or payload.get("conclusion_evidence") or [],
                "conflicts": payload.get("conflicts") or [],
                "limitations": payload.get("limitations") or [],
                "coverage": payload.get("coverage") or {},
                "confidence": "formal",
                "evidence_status": "supported" if formal_evidence else "deep_pending_validation",
                "analysis_level": "deep",
                "verification_status": "verified" if formal_evidence else "partial",
            })
            existing_node_ids.add(node_id)
        counts = storage.file_workflow_counts(scan_id) or {}
        processing_counts = storage.package_processing_counts(scan_id) or {}
        selected_scope = [
            str(path) for path in ((import_task.get("checkpoint") or {}).get("selected_paths") or [])
            if str(path)
        ]
        inventory = len(selected_scope) if selected_scope else int(counts.get("total") or counts.get("total_files") or 0)
        edits = storage.list_tree_edits(scan_id, _request_owner_id() or "legacy", limit=500)
        task_status = str(import_task.get("status") or "")
        selected_complete = bool(selected_scope) and selected_scope and all(path in deep_summary_paths for path in selected_scope)
        formal_ready = bool(
            selected_complete
            and topics
            and allow_deep_directory
            and task_status in {"deep_summarizing_files", "deep_summarizing_nodes", "deep_overview_updating", "deep_update_available", "completed", "partial"}
        )
        return jsonify({"ok": True, "directory": {
            "schema_version": "formal-directory/1.0",
            "status": "formal" if formal_ready else "incomplete",
            "formal": formal_ready,
            "label": "正式智能目录（基于多文件深度解析和文件结论）",
            "topics": topics,
            "version": int(analysis.get("version") or 1),
            "manual_edit_count": len(edits),
            "processing_status": task_status,
            "coverage": {
                "inventory_files": inventory,
                "package_inventory_files": int((storage.get_scan(scan_id) or {}).get("file_count") or inventory),
                "selected_scope_only": bool(selected_scope),
                "deep_files": len(deep_files),
                "formal_evidence": evidence_count,
                "ratio": round(len(deep_files) / float(inventory or 1), 6),
                "failed_files": int(processing_counts.get("needs_attention") or 0),
                "pending_files": int(processing_counts.get("incomplete") or 0),
            },
        }, "notice": "仅深度解析正文、文件结论和通过证据校验的内容可进入正式目录。"})
    except ValueError as exc:
        return api_error(str(exc), 404)

if __name__ == "__main__":
    import uvicorn
    logger.info("数据分析 Agent 启动 http://%s:%s backend=%s model=%s", Config.HOST, Config.PORT, ACTIVE_LLM_BACKEND, llm.model)
    uvicorn.run(app, host=Config.HOST, port=Config.PORT, log_level="info")
