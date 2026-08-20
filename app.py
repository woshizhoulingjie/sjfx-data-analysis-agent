import json
import hashlib
import hmac
import logging
import logging.handlers
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from web_compat import SJFXFastAPI, has_request_context, jsonify, render_template, request, send_from_directory

from config import Config
from services.deepseek import DeepSeekClient, DeepSeekError, OllamaClient, OllamaEmbeddingClient
from services.document_analysis import analyze_document
from services.evidence import embedding_mode, select_evidence, set_embedding_provider
from services.exporter import create_report_docx, export_node, safe_name
from services.folder_analysis import analyze_folder
from services.package_analysis import analyze_package, refresh_package_coverage, _parse_with_limits
from services.large_package import file_fingerprint, inventory_by_path
from services.reporting import (
    build_local_report,
    build_report_analysis_prompt,
    compact_summary_context,
    merge_cloud_report,
)
from services.retrieval import retrieve_evidence
from services.scanner import folder_context, human_size, resolve_under, scan_directory
from services.storage import Storage
from services.structured_qa import answer_question
from services.unified_parser import UnifiedDocumentParser
from services.agent_runtime import PydanticAgentRuntime


app = SJFXFastAPI(title="SJFX Data Analysis Agent", version="2.1")
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
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
storage = Storage(Config.DB_PATH, Config.DOCUMENT_CACHE_DIR, Config.SIDECAR_PAYLOAD_BYTES)
# Bind historical pre-authentication records to the configured token before
# serving requests.  This closes the legacy "first caller claims the record"
# loophole while keeping existing demo links usable for the project owner.
_configured_owner_id = (
    hashlib.sha256(Config.API_ACCESS_TOKEN.encode("utf-8")).hexdigest()[:24]
    if Config.AUTH_REQUIRED and Config.API_ACCESS_TOKEN else None
)
if _configured_owner_id:
    storage.migrate_legacy_ownership(_configured_owner_id)
    storage.register_existing_outputs(Config.OUTPUT_DIR, _configured_owner_id)
_requested_backend = Config.LLM_BACKEND
if _requested_backend not in {"ollama", "deepseek"}:
    logger.warning("未知 LLM_BACKEND=%s，回退到本机 Ollama", _requested_backend)
    _requested_backend = "ollama"
if _requested_backend == "deepseek" and not Config.ENABLE_CLOUD_FEATURES:
    logger.warning("DeepSeek 云端后端已配置但 ENABLE_CLOUD_FEATURES 未开启，强制使用本机 Ollama")
    _requested_backend = "ollama"
if _requested_backend == "ollama":
    llm_transport = OllamaClient(
        api_key="local",
        base_url=Config.OLLAMA_BASE_URL,
        model=Config.OLLAMA_MODEL,
        timeout=Config.SHARED_OLLAMA_REQUEST_TIMEOUT,
        max_concurrency=Config.LLM_MAX_CONCURRENCY,
    )
else:
    llm_transport = DeepSeekClient(
        api_key=Config.DEEPSEEK_API_KEY,
        base_url=Config.DEEPSEEK_BASE_URL,
        model=Config.DEEPSEEK_MODEL,
        max_concurrency=Config.MAX_CLOUD_REQUESTS,
    )
ACTIVE_LLM_BACKEND = "ollama" if isinstance(llm_transport, OllamaClient) else "deepseek"
llm_generation_enabled = Config.ENABLE_SHARED_OLLAMA if isinstance(llm_transport, OllamaClient) else Config.ENABLE_CLOUD_FEATURES
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
if not logger.handlers:
    _file_handler = logging.handlers.RotatingFileHandler(
        str(Config.LOG_DIR / "app.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _file_handler.addFilter(_log_filter)
    logger.addHandler(_file_handler)


@app.before_request
def _access_guard():
    if not Config.AUTH_REQUIRED:
        return None
    if request.path.startswith("/static/") or request.path == "/":
        return None
    if not (request.path.startswith("/api/") or request.path.startswith("/outputs/")):
        return None
    configured = Config.API_ACCESS_TOKEN
    supplied = request.headers.get("X-SJFX-Token", "")
    if not supplied:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
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


def api_error(message, status=400, details=None):
    payload = {"ok": False, "error": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status


def _request_owner_id():
    if not Config.AUTH_REQUIRED or not has_request_context():
        return None
    supplied = request.headers.get("X-SJFX-Token", "")
    if not supplied:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
    if not supplied:
        return "anonymous"
    return hashlib.sha256(supplied.encode("utf-8")).hexdigest()[:24]

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


def _package_documents(scan_id):
    """Avoid hydrating all deep documents when operating on a large package."""
    analysis = storage.get_analysis(scan_id) or {}
    is_large = bool(((analysis.get("policy") or {}).get("large_package") or {}).get("enabled"))
    return storage.list_documents(scan_id, hydrate=not is_large)


def _virtual_node_context(scan_id, node, max_files=30):
    """
    根据虚拟主题节点的 member_paths，
    构造只属于这个节点的分析上下文。
    """
    member_paths = set(node.get("member_paths") or [])

    if not member_paths:
        raise ValueError("当前主题节点没有关联文件")

    all_documents = _package_documents(scan_id)

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

def require_cloud_confirmation(payload):
    if llm.requires_confirmation and payload.get("cloud_confirmed") is not True:
        raise ValueError("请先确认：选中的文件内容将发送至 DeepSeek 云端 API")


class JobCancelled(Exception):
    """Internal signal used to stop a cooperative analysis job."""


def _ensure_job_active(job_id):
    job = storage.get_job(job_id, owner_id=_request_owner_id())
    if not job or job.get("status") in {"cancelled", "cancelling"} or job.get("cancel_requested"):
        raise JobCancelled()
    return job


def _documents_context(scan_id, node_path, scan_result, max_files=30, max_chars=50000):
    documents = _package_documents(scan_id)
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
    selected_path = resolve_under(scan_result["root"], node_path)
    fallback_stats = folder_context(selected_path, scan_result["root"], max_files=1, max_chars=1)
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
        merged = merge_cloud_report(report_data, result["json"], evidence_catalog)
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
    except DeepSeekError as exc:
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
        "overview_chars_per_file": Config.LARGE_PACKAGE_OVERVIEW_CHARS_PER_FILE,
    }


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
    payload["_worker_execution"] = True
    scan_id = job["scan_id"]
    storage.update_job(job_id, progress=5, stage="generating_summary", message="正在生成当前节点深度摘要", heartbeat=True)
    _ensure_job_active(job_id)
    # Reuse the established summary implementation under an isolated request
    # context. The web route has no model call after the async gate below.
    # The route enforces scan ownership.  A Worker has no browser request, so
    # explicitly carry the job's owner token into this internal request rather
    # than letting the task fail as an anonymous caller.
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
    )
    _ensure_job_active(job_id)
    storage.save_artifact(archive.name, job.get("owner_id"), scan_id=scan_id, job_id=job_id, kind="handoff_export")
    return {
        "scan_id": scan_id,
        "file_name": archive.name,
        "download_url": "/outputs/{}".format(archive.name),
        "source_file_count": len(context["member_paths"]),
        "selection_count": len(context["selection_metadata"]),
    }


def _run_claimed_analysis_job(job):
    """Execute an already-claimed package-analysis job in the local Worker."""
    job_id = job["id"]
    scan_id = job["scan_id"]
    options = job.get("options") or {}
    scan_result = require_scan(scan_id)
    scope_label = options.get("scope_label")
    storage.update_job(
        job_id, progress=max(1, int(job.get("progress") or 0)), stage="analyzing", heartbeat=True,
        message=("开始补充分析：{}".format(scope_label) if scope_label else "开始本地完整分析"),
    )

    def progress(percent, message):
        _ensure_job_active(job_id)
        storage.update_job(job_id, progress=percent, stage="analyzing", message=message, heartbeat=True)

    analysis = analyze_package(
        scan_id, scan_result, storage, parser, progress,
        embedding_client=_package_embedding_client,
        llm=(llm if llm_generation_enabled else None),
        large_options=_package_large_options(),
        target_paths=options.get("target_paths"),
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

    def scan_progress(file_count):
        _ensure_job_active(job_id)
        storage.update_job(
            job_id, progress=min(14, 2 + int(file_count / 1000)), stage="scanning",
            message="正在盘点目录：已发现 {} 个文件".format(file_count), heartbeat=True,
        )

    storage.update_job(job_id, progress=1, stage="scanning", message="正在验证并扫描目录", heartbeat=True)
    scan_result = scan_directory(
        root_path, options.get("max_files", Config.MAX_SCAN_FILES),
        max_depth=options.get("max_depth", Config.MAX_SCAN_DEPTH),
        progress_callback=scan_progress, cancel_check=lambda: _ensure_job_active(job_id),
    )
    scan_result["parse_mode"] = "accurate" if options.get("parse_mode") == "accurate" else "fast"
    scan_result["scan_id"] = job_id
    storage.save_scan(scan_result, scan_id=job_id, owner_id=options.get("owner_id") or "legacy")
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
    })


def _start_analysis_job(scan_id, options=None):
    """Persist work for the independent Worker; the API process never runs it."""
    return storage.create_or_get_job(scan_id, options=options, owner_id=_request_owner_id() or "legacy")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    return jsonify({
        "ok": True,
        "configured": llm.configured,
        "backend": ACTIVE_LLM_BACKEND,
        "cloud_features_enabled": Config.ENABLE_CLOUD_FEATURES,
        "evidence_relevance_mode": embedding_mode(),
        "privacy": llm.privacy_label,
        "model_generation_enabled": llm_generation_enabled,
        "model": llm.model,
        "base_url": llm.base_url,
        "python_compatible": True,
        "output_dir": str(Config.OUTPUT_DIR),
        "document_parser": parser.status(),
        "supported_inputs": ["PDF", "Word", "PowerPoint", "Excel", "CSV/XLSX/JSON 数据画像", "图片 OCR", "文本/Markdown/HTML", "ZIP/TAR/TAR.GZ/TAR.BZ2 压缩包"],
        "local_features": ["Office 内嵌图片 OCR", "SHA-256 去重", "SimHash+LSH 聚类", "BM25+TF-IDF 本地证据检索", "自适应分析树", "自动概览 Word"],
        "limits": {
            "max_scan_files": Config.MAX_SCAN_FILES,
            "max_document_characters": Config.MAX_FULL_DOCUMENT_CHARS,
            "max_single_file_bytes": Config.MAX_SINGLE_FILE_BYTES,
            "max_parse_seconds": Config.MAX_PARSE_SECONDS,
            "max_worker_memory_mb": Config.MAX_WORKER_MEMORY_MB,
            "max_archive_entries": Config.MAX_ARCHIVE_ENTRIES,
            "max_archive_member_bytes": Config.MAX_ARCHIVE_MEMBER_BYTES,
            "max_archive_uncompressed_bytes": Config.MAX_ARCHIVE_UNCOMPRESSED_BYTES,
            "max_analysis_jobs": Config.MAX_ANALYSIS_JOBS,
            "max_cloud_requests": Config.MAX_CLOUD_REQUESTS,
            "max_export_bytes": Config.MAX_EXPORT_BYTES,
            "large_package": {
                "threshold_bytes": Config.LARGE_PACKAGE_THRESHOLD_BYTES,
                "threshold_files": Config.LARGE_PACKAGE_THRESHOLD_FILES,
                "initial_parse_files": Config.LARGE_PACKAGE_INITIAL_PARSE_FILES,
                "deepen_batch_files": Config.LARGE_PACKAGE_DEEPEN_BATCH_FILES,
            },
        },
    })


@app.route("/api/test-model", methods=["POST"])
def test_model():
    try:
        require_cloud_confirmation(request.get_json(silent=True) or {})
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
    except (ValueError, DeepSeekError) as exc:
        return api_error(str(exc), 400)


@app.route("/api/scan", methods=["POST"])
def scan():
    payload = request.get_json(silent=True) or {}
    path = payload.get("path", "").strip()
    parse_mode = "accurate" if payload.get("parse_mode") == "accurate" else "fast"
    if not path:
        return api_error("请输入要扫描的本地目录")
    try:
        root = _resolve_allowed_scan_root(path)
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
        scan_result = require_scan(scan_id)
        scan_result["scan_id"] = scan_id
        return jsonify({"ok": True, "scan": scan_result, "summaries": storage.list_summaries(scan_id), "analysis": storage.get_analysis(scan_id)})
    except ValueError as exc:
        return api_error(str(exc), 404)


@app.route("/api/analysis/<scan_id>")
def get_analysis(scan_id):
    try:
        require_scan(scan_id)
        analysis = storage.get_analysis(scan_id)
        if not analysis:
            return api_error("完整分析尚未完成", 404)
        return jsonify({"ok": True, "analysis": analysis})
    except ValueError as exc:
        return api_error(str(exc), 404)


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

        documents = _package_documents(scan_id)

        if not documents:
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
            documents = [
                item
                for item in documents
                if item.get("path")
                in member_paths
            ]

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

        result = retrieve_evidence(
            documents,
            query,
            scope=retrieval_scope,
            top_k=payload.get(
                "top_k",
                10
            ),
            candidate_evidence_ids=candidate_ids,
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
        documents = _package_documents(scan_id)
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


@app.route("/api/jobs/<job_id>")
def get_job(job_id):
    job = storage.get_job(job_id, owner_id=_request_owner_id())
    if not job:
        return api_error("分析任务不存在", 404)
    if job.get("status") == "queued":
        running = storage.get_running_job()
        job["queue_position"] = storage.get_queue_position(job_id)
        job["progress"] = max(1, int(job.get("progress") or 0))
        if running:
            job["blocking_job"] = {
                "id": running.get("id"),
                "progress": running.get("progress"),
                "message": running.get("message"),
            }
            job["message"] = "排队中（前方 {} 个任务）：当前解析任务 {}% · {}".format(
                job["queue_position"],
                running.get("progress", 0),
                running.get("message") or "处理中",
            )
        else:
            job["message"] = "本地解析任务即将启动"
    return jsonify({"ok": True, "job": job})


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    job = storage.get_job(job_id, owner_id=_request_owner_id())
    if not job:
        return api_error("分析任务不存在", 404)
    if job.get("status") in {"completed", "failed", "cancelled"}:
        return jsonify({"ok": True, "job": job, "cancelled": job.get("status") == "cancelled"})
    storage.cancel_job(job_id)
    return jsonify({"ok": True, "job": storage.get_job(job_id, owner_id=_request_owner_id()), "cancelled": True})


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
        if llm_generation_enabled and not payload.get("_worker_execution"):
            if node_id:
                node = _find_analysis_node(scan_id, node_id)
                if node.get("kind") != "group":
                    raise ValueError("只有主题或子方向节点可以生成节点摘要")
                cached = storage.get_summary(scan_id, "node:{}".format(node_id), "folder")
                if force or not (cached and cached.get("schema_version") in {3, 4}):
                    require_cloud_confirmation(payload)
                    job_id = storage.create_job(
                        scan_id, options=payload, task_type="generate_summary", owner_id=_request_owner_id() or "legacy"
                    )
                    return jsonify({
                        "ok": True, "accepted": True, "job_id": job_id,
                        "reused_active_job": False,
                        "status_url": "/api/jobs/{}".format(job_id),
                    }), 202
            else:
                selected = resolve_under(scan_result["root"], node_path)
                summary_type = "folder" if selected.is_dir() or kind == "directory" else "file"
                cached = storage.get_summary(scan_id, node_path, summary_type)
                if force or not (cached and cached.get("schema_version") == 3 and not bool(cached.get("parser_info", {}).get("degraded"))):
                    require_cloud_confirmation(payload)
                    job_id = storage.create_job(
                        scan_id, options=payload, task_type="generate_summary", owner_id=_request_owner_id() or "legacy"
                    )
                    return jsonify({
                        "ok": True, "accepted": True, "job_id": job_id,
                        "reused_active_job": False,
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
                require_cloud_confirmation(payload)
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
                    "cloud_model": result.get("model"),
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

        selected = resolve_under(scan_result["root"], node_path)
        summary_type = "folder" if selected.is_dir() or kind == "directory" else "file"
        local_only = not llm_generation_enabled
        if not local_only:
            require_cloud_confirmation(payload)
        cached = storage.get_summary(scan_id, node_path, summary_type)
        if cached and cached.get("schema_version") == 3 and not force and not bool(cached.get("parser_info", {}).get("degraded")):
            degraded = bool(cached.get("parser_info", {}).get("degraded"))
            return jsonify({"ok": True, "summary": cached, "cached": True, "degraded": degraded})
        if summary_type == "folder" and local_only:
            from services.folder_analysis import _fallback as folder_fallback
            context = _folder_summary_context(scan_id, node_path, scan_result) if _package_documents(scan_id) else folder_context(selected, scan_result["root"])
            summary = folder_fallback(context, node_path, ["模型生成未启用，已返回本地证据摘要。"])
            result = {"model": None, "usage": {}}
            batch_errors = ["模型生成未启用"]
            summary["parser_info"] = {
                "total_files": context["total_files"], "total_dirs": context["total_dirs"],
                "total_size": context["total_size_human"], "type_counts": context["type_counts"],
                "sampled_files": context["sampled_files"], "sample_truncated": context["sample_truncated"],
                "coverage": context.get("coverage", {}), "cloud_model": None, "usage": {},
                "batch_errors": batch_errors, "degraded": True,
            }
        elif summary_type == "folder":
            context = _folder_summary_context(scan_id, node_path, scan_result) if _package_documents(scan_id) else folder_context(selected, scan_result["root"])
            summary, result, batch_errors = analyze_folder(llm, context, node_path)
            summary["parser_info"] = {
                "total_files": context["total_files"],
                "total_dirs": context["total_dirs"],
                "total_size": context["total_size_human"],
                "type_counts": context["type_counts"],
                "sampled_files": context["sampled_files"],
                "sample_truncated": context["sample_truncated"],
                "coverage": context.get("coverage", {}),
                "cloud_model": result.get("model"),
                "usage": result.get("usage", {}),
                "batch_errors": batch_errors,
                "degraded": bool(batch_errors or not result.get("model")),
            }
        else:
            if not selected.exists() or not selected.is_file():
                raise ValueError("文件不存在")
            unified_document = storage.get_document(scan_id, node_path)
            # In large-package mode the first pass stores a bounded projection.
            # A deliberate file-level deep read upgrades only this file, keeps
            # the package responsive, and immediately refreshes coverage.
            if (unified_document or {}).get("coverage", {}).get("overview_sampled"):
                deep_mode = "fast" if scan_result.get("parse_mode") == "fast" else "accurate"
                unified_document = _parse_with_limits(parser, selected, node_path, mode=deep_mode)
                storage.save_document(scan_id, node_path, unified_document)
                source_node = inventory_by_path(scan_result).get(node_path, {})
                storage.set_file_state(
                    scan_id, node_path, file_fingerprint(source_node), "completed",
                    document=unified_document,
                )
                refresh_package_coverage(scan_id, scan_result, storage)
            model_max_chars = Config.MAX_FULL_DOCUMENT_CHARS
            if isinstance(llm_transport, OllamaClient):
                model_max_chars = min(model_max_chars, Config.SHARED_OLLAMA_MAX_CHARS)
            try:
                if local_only:
                    raise DeepSeekError("模型生成未启用")
                summary, coverage, result = analyze_document(
                    llm,
                    selected,
                    node_path,
                    max_chars=model_max_chars,
                    max_chunks=Config.MAX_DOCUMENT_CHUNKS,
                    unified_document=unified_document,
                )
            except DeepSeekError as exc:
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
                "cloud_model": result["model"],
                "usage": result["usage"],
                "degraded": bool(coverage.get("failed_chunks") or not result.get("model")),
            }
        summary["node_path"] = node_path
        summary["summary_type"] = summary_type
        summary["schema_version"] = 3
        summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
        storage.save_summary(scan_id, node_path, summary_type, summary)
        degraded = bool(summary.get("parser_info", {}).get("degraded"))
        return jsonify({"ok": True, "summary": summary, "cached": False, "degraded": degraded})
    except (ValueError, DeepSeekError) as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        return api_error("摘要生成失败", 500, str(exc))


@app.route("/api/report", methods=["POST"])
def report():
    payload = request.get_json(silent=True) or {}
    try:
        scan_id = payload.get("scan_id", "")
        require_scan(scan_id)
        if llm.requires_confirmation and payload.get("cloud_confirmed") is not True:
            raise ValueError("请先确认：选中的文件内容将发送至 DeepSeek 云端 API")
        job_id, created = storage.create_or_get_typed_job(scan_id, "generate_report", owner_id=_request_owner_id() or "legacy")
        return jsonify({
            "ok": True, "accepted": True, "job_id": job_id,
            "reused_active_job": not created,
            "status_url": "/api/jobs/{}".format(job_id),
        }), 202
    except (ValueError, DeepSeekError, RuntimeError) as exc:
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


@app.route("/outputs/<path:filename>")
def outputs(filename):
    # Output artifacts may contain original user资料; require an ownership
    # record in addition to the global API token and reject path tricks.
    if Path(filename).name != filename:
        return api_error("非法输出文件路径", 404)
    owner_id = _request_owner_id()
    output_path = Config.OUTPUT_DIR / filename
    if not output_path.is_file():
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
    return send_from_directory(str(Config.OUTPUT_DIR), filename, as_attachment=True)


if __name__ == "__main__":
    import uvicorn
    logger.info("数据分析 Agent 启动 http://%s:%s backend=%s model=%s", Config.HOST, Config.PORT, ACTIVE_LLM_BACKEND, llm.model)
    uvicorn.run(app, host=Config.HOST, port=Config.PORT, log_level="info")
