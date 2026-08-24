import json
import hashlib
import hmac
import logging
import logging.handlers
import os
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from web_compat import (
    InternalExecutionCapability, SJFXFastAPI, has_request_context, jsonify,
    render_template, request, send_from_directory,
)

from config import Config
from services.ollama import LocalModelError, OllamaClient, OllamaEmbeddingClient
from services.document_analysis import analyze_document
from services.evidence import embedding_mode, select_evidence, set_embedding_provider
from services.exporter import create_report_docx, export_node, safe_name
from services.folder_analysis import analyze_folder
from services.package_analysis import (
    analyze_package, checkpoint_fingerprint, refresh_package_coverage, _parse_with_limits,
    _restore_source_provenance, _secure_source_snapshot,
)
from services.large_package import inventory_by_path
from services.reporting import (
    build_local_report,
    build_report_analysis_prompt,
    compact_summary_context,
    merge_model_report,
)
from services.retrieval import retrieve_evidence
from services.scanner import IGNORED_DIRS, IGNORED_FILES, human_size, resolve_under, scan_directory
from services.storage import Storage
from services.tree_editor import filter_tree
from services.structured_qa import answer_question
from services.unified_parser import UnifiedDocumentParser
from services.agent_runtime import PydanticAgentRuntime


MINIMUM_PYTHON_VERSION = (3, 10)


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


app = SJFXFastAPI(
    title="SJFX Data Analysis Agent",
    version="2.1",
    max_content_length=4 * 1024 * 1024,
    security_headers=True,
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("sjfx")


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
storage = Storage(Config.DB_PATH, Config.DOCUMENT_CACHE_DIR, Config.SIDECAR_PAYLOAD_BYTES)
# Bind historical pre-authentication records to the configured token before
# serving requests.  This closes the legacy "first caller claims the record"
# loophole while keeping existing demo links usable for the project owner.
_configured_owner_id = Config.OWNER_ID
_token_owner_alias = (
    hashlib.sha256(Config.API_ACCESS_TOKEN.encode("utf-8")).hexdigest()[:24]
    if Config.API_ACCESS_TOKEN else None
)
storage.migrate_legacy_ownership(
    _configured_owner_id,
    aliases=["legacy", "default", _token_owner_alias],
)
storage.register_existing_outputs(Config.OUTPUT_DIR, _configured_owner_id)
# The deployment is intentionally local-only: all generation uses Ollama on this host.
llm_transport = OllamaClient(
    base_url=Config.OLLAMA_BASE_URL,
    model=Config.OLLAMA_MODEL,
    timeout=Config.SHARED_OLLAMA_REQUEST_TIMEOUT,
    max_concurrency=Config.LLM_MAX_CONCURRENCY,
)
ACTIVE_LLM_BACKEND = "ollama"
llm_generation_enabled = Config.ENABLE_SHARED_OLLAMA
llm = PydanticAgentRuntime(llm_transport)
# 完整分析专用的文档级 embedding。
# 只用于 analyze_package 中的一文档一向量语义聚类，
# 不受 evidence embedding 开关影响。
if isinstance(llm_transport, OllamaClient):
    _package_embedding_client = OllamaEmbeddingClient(
        Config.OLLAMA_BASE_URL,
        Config.OLLAMA_EMBED_MODEL,
    )
else:
    _package_embedding_client = None

# evidence embedding 单独控制。
# 默认关闭，避免打开文件/节点时同步计算大量 evidence embedding。
if (
    _package_embedding_client is not None
    and Config.ENABLE_SHARED_OLLAMA_EMBEDDINGS
):
    _embedding_client = _package_embedding_client
    set_embedding_provider(_embedding_client.embed)
else:
    _embedding_client = None
    set_embedding_provider(None)
parser = UnifiedDocumentParser(Config.DOCLING_ARTIFACTS_DIR, Config.RAPIDOCR_MODEL_DIR, Config.MAX_FULL_DOCUMENT_CHARS)

# This capability is deliberately process-local and cannot be supplied in an
# HTTP JSON document.  The dedicated Worker invokes the established summary
# implementation directly, so it needs a narrow way to cross the async queue
# gate without turning a request field into an execution-control primitive.
_summary_worker_execution = InternalExecutionCapability("sjfx_summary_worker_execution")
_summary_worker_execution_context = _summary_worker_execution.activate


if not logger.handlers:
    _file_handler = logging.handlers.RotatingFileHandler(
        str(Config.LOG_DIR / "app.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _file_handler.addFilter(_log_filter)
    logger.addHandler(_file_handler)
    logger.propagate = False


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
    return scan
def _walk_analysis_nodes(node):
    """遍历分析树中的所有节点。"""
    if not isinstance(node, dict):
        return

    yield node

    for child in node.get("children", []):
        for item in _walk_analysis_nodes(child):
            yield item


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

    raise ValueError("分析树节点不存在或已经失效，请重新执行数据包分析")


def _physical_scope_member_paths(scan, node_path):
    """Resolve one physical file/folder selection to safe relative file paths."""
    node_path = node_path or "."
    resolve_under(scan["root"], node_path)
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


def _virtual_node_context(scan_id, node, max_files=30):
    """
    根据虚拟主题节点的 member_paths，
    构造只属于这个节点的分析上下文。
    """
    member_paths = set(node.get("member_paths") or [])

    if not member_paths:
        raise ValueError("当前主题节点没有关联文件")

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
            "coverage": node.get("coverage") or {
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

        "sampled_files": len(sampled_documents),

        "total_files": len(documents),

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
        for conclusion in conclusions:
            evidence.extend(conclusion.get("evidence", []))
    seen = set()
    evidence = [
        item for item in evidence
        if not (item.get("evidence_id") in seen or seen.add(item.get("evidence_id")))
    ]
    primary = conclusions[0] if conclusions else {}
    claims = list(primary.get("claims") or [])
    if not claims and primary.get("answer"):
        claims = [{
            "statement": primary.get("answer"),
            "type": "inference",
            "evidence_ids": [item.get("evidence_id") for item in evidence if item.get("evidence_id")],
            "support_status": "supported" if evidence else "insufficient",
        }]
    qa = {
        "question": primary.get("question") or primary.get("analysis_question") or "该节点主要包含哪些内容，哪些方向值得继续下钻？",
        "value": primary.get("value") or primary.get("question_value") or "用于判断该主题是否值得继续深入分析。",
        "answer": primary.get("answer") or node.get("summary") or "暂无足够证据形成回答。",
        "claims": claims,
        "evidence": evidence[:12],
        "coverage": context.get("coverage", {}),
        "limitations": list(context.get("coverage", {}).get("limitations") or []) + ([] if evidence else ["当前没有有效正文证据支撑该回答。"]),
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
        "conclusion_evidence": conclusions,
        "evidence_chain": evidence[:12],
        "question": qa["question"],
        "value": qa["value"],
        "answer": qa["answer"],
        "claims": claims,
        "evidence_status": "supported" if evidence else "insufficient",
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
            paths = _physical_scope_member_paths(scan_result, raw.get("path", "."))
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
        raise ValueError("本地模型生成未启用，请将 ENABLE_SHARED_OLLAMA 设置为 1 后重试")


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


def _write_local_overview(scan_id, owner_id=None, job_id=None):
    scan_result = require_scan(scan_id)
    analysis = storage.get_analysis(scan_id) or {}
    summaries = storage.list_summaries(scan_id)
    report_data = build_local_report(scan_result, summaries, analysis)
    report_data, model_name, model_usage, warning = _analyze_report_with_model(
        scan_result, summaries, analysis, report_data
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "情况概览报告_{}_{}_自动.docx".format(safe_name(Path(scan_result["root"]).name), stamp)
    create_report_docx(report_data, scan_result, Config.OUTPUT_DIR / name)
    storage.save_artifact(name, owner_id, scan_id=scan_id, job_id=job_id, kind="overview_report")
    storage.save_summary(scan_id, ".", "report", report_data)
    return {
        "file_name": name,
        "download_url": "/outputs/{}".format(name),
        "report": report_data,
        "model": model_name,
        "usage": model_usage,
        "warning": warning,
    }


def _package_large_options():
    return {
        "threshold_bytes": Config.LARGE_PACKAGE_THRESHOLD_BYTES,
        "threshold_files": Config.LARGE_PACKAGE_THRESHOLD_FILES,
        "initial_parse_files": Config.LARGE_PACKAGE_INITIAL_PARSE_FILES,
        "deepen_batch_files": Config.LARGE_PACKAGE_DEEPEN_BATCH_FILES,
        "batch_files": Config.LARGE_PACKAGE_BATCH_FILES,
        "overview_chars_per_file": Config.LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE,
        "overview_evidence_per_file": Config.LARGE_PACKAGE_OVERVIEW_EVIDENCE_PER_FILE,
    }


def _publish_analysis_progress(scan_id, scan_result, percent, message, stage="analyzing"):
    """Publish a small, honest overview while the final analysis is running."""
    counts = storage.file_state_counts(scan_id)
    metrics = storage.file_state_metrics(scan_id)
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


def _run_claimed_report_job(job):
    """Generate an on-demand overview report outside the HTTP request."""
    job_id = job["id"]
    scan_id = job["scan_id"]
    storage.update_job(job_id, progress=5, stage="generating_report", message="正在整理情况概览报告", heartbeat=True)
    _ensure_job_active(job_id)
    report = _write_local_overview(scan_id, owner_id=job.get("owner_id"), job_id=job_id)
    _ensure_job_active(job_id)
    return {"scan_id": scan_id, "overview": report}


def _run_claimed_summary_job(job):
    """Run an uncached model-backed node/document summary outside Flask HTTP."""
    job_id = job["id"]
    payload = dict(job.get("options") or {})
    # Ignore a historical client-controlled field if it was persisted by an
    # older API process. Internal execution authority lives only in the
    # ContextVar below.
    payload.pop("_worker_execution", None)
    scan_id = job["scan_id"]
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
        inventory_metadata=inventory_by_path(scan_result),
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
        job_id, progress=max(mapped_progress(1), int(job.get("progress") or 0)), stage="analyzing", heartbeat=True,
        message=("开始补充分析：{}".format(scope_label) if scope_label else "开始本地完整分析"),
        current_stage="分析准备",
        current_file=scope_label or "",
    )
    try:
        _publish_analysis_progress(
            scan_id, scan_result, mapped_progress(1),
            "开始补充分析：{}".format(scope_label) if scope_label else "目录清点完成，开始内容解析",
            stage="analysis_preparing",
        )
    except Exception:
        logger.warning("发布渐进分析概览失败 scan_id=%s", scan_id, exc_info=True)

    progress_state = {"bucket": -1}

    def progress(percent, message):
        _ensure_job_active(job_id)
        visible_percent = mapped_progress(percent)
        storage.update_job(
            job_id, progress=visible_percent, stage="analyzing", message=message, heartbeat=True,
            current_stage="解析与分析",
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

    analysis = analyze_package(
        scan_id, scan_result, storage, parser, progress,
        embedding_client=_package_embedding_client,
        llm=(llm if llm_generation_enabled else None),
        large_options=_package_large_options(),
        target_paths=options.get("target_paths"),
        cancel_check=lambda: storage.is_job_cancel_requested(job_id),
        parse_mode_override=options.get("parse_mode"),
    )
    _ensure_job_active(job_id)
    storage.update_job(job_id, progress=96, stage="generating_report", message="自动生成情况概览 Word", heartbeat=True)
    overview = _write_local_overview(scan_id, owner_id=job.get("owner_id"), job_id=job_id)
    _ensure_job_active(job_id)
    return {
        "scan_id": scan_id,
        "analysis": analysis.get("statistics", {}),
        "classification_dimensions": analysis.get("classification_dimensions", []),
        "overview": overview,
    }


def _run_claimed_scan_and_analyze_job(job):
    """Inventory a filesystem path asynchronously, then run the normal workflow."""
    job_id = job["id"]
    options = job.get("options") or {}
    root_path = str(options.get("root_path") or "").strip()
    if not root_path:
        raise ValueError("扫描任务缺少目录路径")

    # A supervised 24-hour slice may end after inventory was committed.  On
    # the next slice, continue from file checkpoints instead of rescanning a
    # potentially slow NAS tree.
    existing_scan = storage.get_scan(job_id, owner_id=options.get("owner_id") or job.get("owner_id"))
    if existing_scan:
        return _run_claimed_analysis_job({
            "id": job_id, "scan_id": job_id, "options": {},
            "owner_id": options.get("owner_id") or job.get("owner_id"),
            "progress": job.get("progress") or 15,
            "_progress_start": 15, "_progress_end": 95,
        })

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
    scan_result = scan_directory(
        resolved_root, options.get("max_files", Config.MAX_SCAN_FILES),
        max_depth=options.get("max_depth", Config.MAX_SCAN_DEPTH),
        max_directories=Config.MAX_SCAN_DIRECTORIES,
        max_nodes=Config.MAX_SCAN_NODES,
        max_entries_per_directory=Config.MAX_SCAN_ENTRIES_PER_DIRECTORY,
        activity_callback=scan_progress,
        cancel_check=lambda: _ensure_job_active(job_id),
    )
    scan_result["parse_mode"] = "accurate" if options.get("parse_mode") == "accurate" else "fast"
    scan_result["scan_id"] = job_id
    storage.save_scan(scan_result, scan_id=job_id, owner_id=options.get("owner_id") or "legacy")
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
    return _run_claimed_analysis_job({
        "id": job_id, "scan_id": job_id, "options": {}, "progress": 15,
        "owner_id": options.get("owner_id") or job.get("owner_id") or "legacy",
        "_progress_start": 15, "_progress_end": 95,
    })


def _start_analysis_job(scan_id, options=None):
    """Persist work for the independent Worker; the API process never runs it."""
    return storage.create_or_get_job(scan_id, options=options, owner_id=_request_owner_id() or "legacy")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    runtime = _python_runtime_status()
    return jsonify({
        "ok": True,
        "configured": llm.configured,
        "backend": ACTIVE_LLM_BACKEND,
        "local_model_enabled": Config.ENABLE_SHARED_OLLAMA,
        "evidence_relevance_mode": embedding_mode(),
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
                "full_inventory_processing": True,
            },
        },
    })


@app.route("/api/test-model", methods=["POST"])
def test_model():
    try:
        require_local_model_enabled()
        if isinstance(llm_transport, OllamaClient):
            health = llm.health_check()
            if not health["reachable"]:
                return api_error("本机 Ollama 服务不可达：{}".format(health.get("error", "未知错误")), 503)
            if not health["model_available"]:
                return api_error("已连接本机 Ollama，但未找到模型 {}".format(llm.model), 503)
            reply = "本机 Ollama 服务正常，模型已就绪（未发起耗时生成测试）"
            if not llm_generation_enabled:
                reply = "检测到实验室共享 Ollama；项目安全模式未调用它，以免影响其他用户"
            return jsonify({
                "ok": True,
                "reply": reply,
                "model": llm.model,
                "usage": {},
                "generation_tested": False,
            })
        result = llm.chat("你是连接测试助手。", "只回复：连接成功", temperature=0, max_tokens=20)
        return jsonify({"ok": True, "reply": result["content"], "model": result["model"], "usage": result["usage"]})
    except (ValueError, LocalModelError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/scan", methods=["POST"])
def scan():
    payload = request.get_json(silent=True) or {}
    path = payload.get("path", "").strip()
    parse_mode = "accurate" if payload.get("parse_mode") == "accurate" else "fast"
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
                "tree_edits": storage.list_tree_edits(scan_id, _request_owner_id(), limit=500),
                "tree_edits_total": storage.tree_edit_count(scan_id, _request_owner_id()),
                "response_mode": "bounded",
            })
        scan_result = require_scan(scan_id)
        scan_result["scan_id"] = scan_id
        return jsonify({"ok": True, "scan": scan_result, "summaries": storage.list_summaries(scan_id), "analysis": storage.get_analysis(scan_id)})
    except ValueError as exc:
        return api_error(str(exc), 404)


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


@app.route("/api/document/<scan_id>")
def get_document(scan_id):
    try:
        require_scan(scan_id)
        node_path = request.args.get("path", "")
        document = storage.get_document(scan_id, node_path)
        if not document:
            return api_error("该文件尚无统一解析结果", 404)
        return jsonify({"ok": True, "document": {
            "schema_version": document.get("schema_version"),
            "source": document.get("source"),
            "parser": document.get("parser"),
            "structure": document.get("structure"),
            "coverage": document.get("coverage", {}),
            "data_profile": document.get("data_profile"),
            "data_profiles": document.get("data_profiles", []),
            "warnings": document.get("warnings", []),
            "text_preview": document.get("text", "")[:5000],
            "evidence": select_evidence(
                document.get("evidence", []),
                topics=document.get("structure", {}).get("headings", [])[:8] + [document.get("structure", {}).get("title")],
                max_items=12,
                per_source=12,
                max_chars=520,
            ),
            "evidence_count": len(document.get("evidence", [])),
        }})
    except ValueError as exc:
        return api_error(str(exc), 404)


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

        if not documents and not evidence_index_count:
            return api_error(
                "完整解析尚未完成，暂时没有可检索证据。",
                409
            )

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

        # 返回前端真正的逻辑范围
        result["scope"] = scope_key

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

        return jsonify({
            "ok": True,
            "retrieval": result
        })

    except ValueError as exc:
        return api_error(
            str(exc),
            400
        )


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
            member_paths = set(_physical_scope_member_paths(scan_result, payload.get("path") or "."))
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
    job = storage.cancel_job(job_id)
    return jsonify({
        "ok": True,
        "job": _job_api_view(job),
        "cancel_requested": True,
        "cancelled": bool(job and job.get("status") == "cancelled"),
    })


@app.route("/api/analyze-package", methods=["POST"])
def rerun_package_analysis():
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = payload.get("scan_id", "")
        scan_result = require_scan(scan_id)
        if payload.get("parse_mode") in {"fast", "accurate"}:
            scan_result["parse_mode"] = payload["parse_mode"]
            storage.update_scan(scan_id, scan_result)
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
        options = {"target_paths": target_paths, "scope_label": "失败文件重试", "retry_failed": True}
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
            member_paths = _physical_scope_member_paths(scan_result, node_path)
            label = node_path
        if not member_paths:
            raise ValueError("当前节点没有可补充分析的文件")
        job_id, created = _start_analysis_job(scan_id, {
            "target_paths": member_paths,
            "scope_label": label,
            "parse_mode": "accurate" if payload.get("parse_mode") != "fast" else "fast",
        })
        return jsonify({
            "ok": True, "job_id": job_id, "reused_active_job": not created,
            "scope_label": label, "requested_files": len(member_paths),
            "batch_limit": Config.LARGE_PACKAGE_DEEPEN_BATCH_FILES,
        })
    except ValueError as exc:
        return api_error(str(exc), 400)


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
                if force or not (cached and cached.get("schema_version") in {3, 4}):
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
                physical_node = _physical_inventory_node(scan_result, node_path)
                if not physical_node:
                    raise ValueError("节点不在本次安全清点范围内")
                summary_type = "folder" if physical_node.get("kind") == "directory" or kind == "directory" else "file"
                cached = storage.get_summary(scan_id, node_path, summary_type)
                if force or not (cached and cached.get("schema_version") in {3, 4} and not bool(cached.get("parser_info", {}).get("degraded"))):
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
            if cached and cached.get("schema_version") in {3, 4} and not force:
                return jsonify({"ok": True, "summary": cached, "cached": True, "degraded": bool(cached.get("parser_info", {}).get("degraded"))})

            context = _virtual_node_context(scan_id, node)
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
                summary = generated
            else:
                summary = local_summary
                summary["warnings"] = ["模型深度摘要未启用，已返回主题的本地结论—证据链。"]

            summary["node_path"] = cache_path
            summary["summary_type"] = "folder"
            summary["schema_version"] = 4
            summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
            storage.save_summary(scan_id, cache_path, "folder", summary)
            return jsonify({"ok": True, "summary": summary, "cached": False, "degraded": bool(summary.get("parser_info", {}).get("degraded"))})

        physical_node = _physical_inventory_node(scan_result, node_path)
        if not physical_node:
            raise ValueError("节点不在本次安全清点范围内")
        selected = Path(scan_result["root"]) / node_path
        summary_type = "folder" if physical_node.get("kind") == "directory" or kind == "directory" else "file"
        local_only = not llm_generation_enabled
        if not local_only:
            require_local_model_enabled()
        cached = storage.get_summary(scan_id, node_path, summary_type)
        if cached and cached.get("schema_version") in {3, 4} and not force and not bool(cached.get("parser_info", {}).get("degraded")):
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
            needs_deep_parse = is_large_package and (
                document_parser.get("mode") == "fast"
                or document_parser.get("fast_preview")
                or document_coverage.get("limited_by_fast_mode")
                or document_coverage.get("overview_sampled")
            )
            source_node = inventory_by_path(scan_result).get(node_path)
            if not source_node:
                raise ValueError("文件不在本次安全清点范围内")
            if needs_deep_parse or not unified_document:
                deep_mode = "accurate"
                with _secure_source_snapshot(scan_result["root"], source_node) as snapshot:
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
                    or not result.get("model")
                ),
            }
        summary["node_path"] = node_path
        summary["summary_type"] = summary_type
        summary["schema_version"] = 4
        summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
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


if __name__ == "__main__":
    import uvicorn
    logger.info("数据分析 Agent 启动 http://%s:%s backend=%s model=%s", Config.HOST, Config.PORT, ACTIVE_LLM_BACKEND, llm.model)
    uvicorn.run(app, host=Config.HOST, port=Config.PORT, log_level="info")
