"""High-recall hybrid retrieval for isolated large packages.

This is the ``lp-`` branch of ``app.py::_conversation_retrieve`` extracted into
its own module so the conversation path stays small in ``app.py``.  It answers
from the persisted quick/deep per-file summaries, which is the only corpus an
isolated large package has.

Recall is an explicit OR (keyword hit, semantic similarity, or an obvious
title/topic match) so a file is not dropped for missing one word.  Ranking then
combines keyword, semantic, title/topic and content quality into one score, and
weak candidates fall below the evidence floor instead of being padded into the
result set.  The returned mapping keeps the shape produced by
``services.retrieval.retrieve_evidence``.

An optional real-embedding pass strengthens recall only: it can raise the
semantic term for the questions's nearest files (typically paraphrase or
cross-lingual hits the keyword pass cannot see) and returns nothing at all when
embeddings are disabled, slow or unavailable, which leaves every existing
behaviour exactly as it was.
"""

import json
from services.embedding_encoding import prefix as embedding_prefix
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

LARGE_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:[_.\-][A-Za-z0-9]+)*|[\u4e00-\u9fff]{2,}"
)

BROAD_INTENTS = frozenset({
    "summary", "analysis", "comparison", "timeline",
    "relationship", "contradiction", "risk", "multi_task",
})

# Interrogative and functional words carry no retrieval signal in a
# package-wide question.  They are dropped from the keyword terms but never
# from recall.
LARGE_QUERY_STOPWORDS = frozenset({
    "这个", "那个", "什么", "哪些", "哪个", "怎么", "如何", "是否", "可以", "需要",
    "以及", "关于", "对于", "请问", "帮我", "一下", "里面", "中的", "我们", "他们",
    "告诉", "说明", "介绍", "讲了", "说了", "有哪", "一个", "多少", "为什", "么样",
    "主要", "相关", "进行", "通过", "根据", "目前", "现在", "如果", "因为", "所以",
    # High-frequency corpus words: they match almost every summary and would
    # otherwise let an unrelated file clear the evidence floor.
    "数据", "资料", "文件", "文档", "内容", "信息", "系统", "项目", "情况", "方面",
    "the", "and", "for", "with", "what", "which", "how", "does", "this", "that",
    "from", "about", "are", "was", "were", "you", "can", "please", "tell",
})

# Bilingual and abbreviation expansion.  A question that only says
# "大语言模型" must still recall summaries that only ever say "LLM",
# "large language model" or "transformer", and vice versa.
LARGE_QUERY_EXPANSIONS = (
    ("大语言模型", ("large language model", "large language models", "llm", "llms", "语言模型", "模型训练", "模型推理", "transformer", "注意力机制", "深度学习")),
    ("语言模型", ("large language model", "llm", "大语言模型", "transformer")),
    ("large language model", ("大语言模型", "语言模型", "llm", "transformer")),
    ("llm", ("大语言模型", "large language model", "语言模型", "transformer")),
    ("transformer", ("大语言模型", "注意力机制", "attention", "语言模型")),
    ("神经网络", ("neural network", "深度学习", "cnn", "rnn", "模型")),
    ("深度学习", ("deep learning", "神经网络", "模型训练", "表示学习")),
    ("知识图谱", ("knowledge graph", "kg", "实体", "关系抽取", "三元组")),
    ("检索增强", ("rag", "retrieval augmented", "检索", "召回", "向量检索")),
    ("向量", ("embedding", "embeddings", "语义", "向量化", "相似度")),
    ("语义", ("semantic", "embedding", "语义相似", "向量")),
    ("数据集", ("dataset", "语料", "训练集", "样本")),
    ("评估", ("evaluation", "评测", "指标", "benchmark", "准确率")),
    ("锂电池", ("lithium", "li-ion", "battery", "电池", "正极", "负极", "电解液", "固态电解质")),
    ("电池", ("battery", "锂电池", "lithium", "电化学", "电极")),
    ("安全", ("safety", "风险", "隐私", "合规")),
)

# Unified relevance score weights: keyword, semantic, title/topic, quality.
# The floor keeps weak single-term overlaps out of the evidence set instead of
# padding the result list up to a fixed count.
LARGE_EVIDENCE_FLOOR = 0.15
LARGE_SEMANTIC_RECALL_FLOOR = 0.18

EMBED_MAX_FILES = 20000
EMBED_TIMEOUT_SECONDS = 3
EMBED_MIN_VECTORS = 24
EMBED_CALIBRATION_SAMPLE = 512
EMBED_CALIBRATION_PERCENTILE = 40
# The catalog threshold is calibrated document-to-document; measured on a real
# package it admitted ~24% of the corpus even for an unrelated question, so it
# cannot gate a query.  The cut is therefore relative to the question's own best
# match, which measured 7-23 candidates across relevant, cross-lingual and
# deliberately irrelevant questions.
EMBED_TOPN = 20
EMBED_MARGIN = 0.06
EMBED_ABS_FLOOR = 0.35
# Mapped into the existing semantic term's range so an embedding-only match
# ranks below evidence that also has lexical support.
EMBED_SEMANTIC_BASE = 0.30
EMBED_SEMANTIC_TOP = 0.60
EMBED_SEMANTIC_SATURATION = 0.70


def query_plan(query: Any) -> Dict[str, Any]:
    """Turn a question into recall terms, expansion terms and match grams."""
    text = str(query or "").casefold()
    terms: List[str] = []
    for value in LARGE_TOKEN_RE.findall(text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", value):
            # CJK runs are not word segmented; overlapping bigrams and
            # trigrams give the recall pass character-level coverage without a
            # tokeniser dependency.
            for size in (2, 3):
                terms.extend(
                    value[index:index + size]
                    for index in range(max(0, len(value) - size + 1))
                )
        elif len(value) > 1:
            terms.append(value)
    content = [
        term for term in terms
        if len(term) >= 2 and term not in LARGE_QUERY_STOPWORDS
    ]
    if not content:
        content = [term for term in terms if len(term) >= 2]
    content = list(dict.fromkeys(content))[:60]
    expansions: List[str] = []
    for key, values in LARGE_QUERY_EXPANSIONS:
        if key in text:
            expansions.extend(values)
    expansions = list(
        dict.fromkeys(str(value).casefold() for value in expansions if value)
    )[:80]
    return {
        "text": text,
        "terms": content,
        "expansions": expansions,
        "grams": list(dict.fromkeys(content + expansions)),
    }


def local_semantic(plan: Mapping[str, Any], summaries: Sequence[str]) -> List[float]:
    """Model-free character-level similarity used as a semantic recall signal.

    The shared ranker returns a normalised TF-IDF cosine for small corpora but
    deliberately skips that vector for very large ones.  The containment ratio
    below keeps semantic recall available in that case without allocating a
    dense vocabulary over thousands of summaries, and it needs no model call.
    """
    grams = plan.get("grams") or []
    if not grams:
        return [0.0] * len(summaries)
    total = float(len(grams))
    return [
        sum(1 for gram in grams if gram in text) / total
        for text in summaries
    ]


def summary_quality(row: Mapping[str, Any]) -> float:
    """Content-quality term of the unified large-package score (0..1)."""
    length_score = min(1.0, len(row["_summary"]) / 400.0)
    if row["_deep"] == "completed":
        status_score = 1.0
    elif row["_quick"] == "completed":
        status_score = 0.6
    elif row["_summary"]:
        status_score = 0.4
    else:
        status_score = 0.2
    return 0.5 * length_score + 0.5 * status_score


def embedding_calibration(store: Any, package_id: str, model: str):
    """Return (centre, threshold) for one package, without writing anything.

    The catalog pipeline caches both, so the cached row is used when present;
    otherwise the same read-only computation is repeated here.  Nothing in the
    catalog state is modified by the conversation path.
    """
    import numpy as np
    from contextlib import closing

    with closing(store._connect()) as conn:
        cached = conn.execute(
            "SELECT center, threshold FROM large_catalog_centers"
            " WHERE package_id=? AND model=?",
            (package_id, model),
        ).fetchone()
        if cached and cached["center"]:
            return (
                np.frombuffer(cached["center"], dtype=np.float32),
                float(cached["threshold"] or 0.0),
            )
        sampled = conn.execute(
            "SELECT vector FROM large_catalog_members WHERE package_id=?"
            " ORDER BY path LIMIT ?",
            (package_id, EMBED_CALIBRATION_SAMPLE),
        ).fetchall()
    vectors = []
    for row in sampled:
        value = np.frombuffer(row["vector"], dtype=np.float32)
        if (
            value.ndim == 1
            and len(value)
            and np.isfinite(value).all()
            and np.linalg.norm(value) > 1e-8
        ):
            vectors.append(value / np.linalg.norm(value))
    if len(vectors) < EMBED_MIN_VECTORS:
        return None, 0.0
    matrix = np.vstack(vectors)
    center = matrix.mean(axis=0).astype(np.float32)
    centered = matrix - center
    norms = np.linalg.norm(centered, axis=1, keepdims=True)
    norms[norms < 1e-8] = 1.0
    centered = centered / norms
    step = max(1, len(centered) // 256)
    picked = list(range(0, len(centered), step))[:256]
    similarity = centered[picked] @ centered.T
    nearest = []
    for position, index in enumerate(picked):
        row = similarity[position].copy()
        row[index] = -1.0
        nearest.append(float(row.max()))
    threshold = float(np.percentile(nearest, EMBED_CALIBRATION_PERCENTILE))
    return center, max(0.30, min(0.90, threshold))


def embedding_similarity(store: Any, package_id: str, query: str,
                         paths: Sequence[str], logger: Any = None) -> Dict[str, float]:
    """Semantic similarity per file, used only to strengthen recall.

    Returns ``{path: semantic_score}`` for the question's nearest files, or
    ``{}`` on any problem at all - embeddings disabled, service down or slow,
    too little material indexed, no usable vectors.  An empty mapping leaves
    every existing lexical, ranking and threshold behaviour untouched, so this
    is strictly additive and can never make the current path worse.
    """
    wanted = {str(path) for path in paths if path}
    if not wanted:
        return {}
    try:
        from config import Config
        if getattr(Config, 'EMBED_BACKEND', 'openai_compatible' if getattr(Config, 'ENABLE_VLLM_EMBEDDINGS', False) else 'disabled') == 'disabled':
            return {}
    except Exception:
        return {}
    question = str(query or "").strip()
    if not question:
        return {}
    try:
        import numpy as np
        from contextlib import closing
        from services.large_package_semantic import embedding_client, _norm, _project
        from services.embedding_backend import client_identity
        from services.large_package_semantic import _require_space

        base = embedding_client()
        if base is None:
            return {}
        model = client_identity(base)
        with closing(store._connect()) as conn:
            _require_space(conn, package_id, base)
            stored = conn.execute(
                "SELECT path, vector FROM large_catalog_members WHERE package_id=?"
                " LIMIT ?",
                (package_id, EMBED_MAX_FILES),
            ).fetchall()
        vectors = []
        for row in stored:
            path = str(row["path"] or "")
            if path not in wanted:
                continue
            value = np.frombuffer(row["vector"], dtype=np.float32)
            if value.ndim != 1 or not len(value) or not np.isfinite(value).all():
                continue
            vectors.append((path, value))
        if len(vectors) < EMBED_MIN_VECTORS:
            return {}
        center, _threshold = embedding_calibration(store, package_id, model)
        if center is None or len(center) != len(vectors[0][1]):
            return {}
        # A short interactive budget: one attempt, no retries, bounded wait, so
        # a slow embedding service can never stall a question.
        client = base.with_options(timeout=EMBED_TIMEOUT_SECONDS, max_batch_size=1,
                                   max_chars=512, semaphore_timeout=1, retries=0)
        encoded = client.embed([embedding_prefix('query', client=client) + question[:512]])
        if not encoded:
            return {}
        query_vector = _project(
            _norm(np.asarray(encoded[0], dtype=np.float32)), center
        )
        scored = []
        for path, value in vectors:
            similarity = float(np.dot(query_vector, _project(_norm(value), center)))
            if similarity >= EMBED_ABS_FLOOR:
                scored.append((similarity, path))
        if not scored:
            return {}
        scored.sort(key=lambda item: (-item[0], item[1]))
        cutoff = max(EMBED_ABS_FLOOR, scored[0][0] - EMBED_MARGIN)
        span = max(0.05, EMBED_SEMANTIC_SATURATION - EMBED_ABS_FLOOR)
        scores = {}
        for similarity, path in scored[:EMBED_TOPN]:
            if similarity < cutoff:
                break
            scores[path] = min(
                EMBED_SEMANTIC_TOP,
                EMBED_SEMANTIC_BASE
                + (similarity - EMBED_ABS_FLOOR)
                / span
                * (EMBED_SEMANTIC_TOP - EMBED_SEMANTIC_BASE),
            )
        return scores
    except Exception as exc:  # semantic recall is optional, never fatal
        if logger is not None:
            logger.info(
                "large-package embedding recall unavailable for %s: %s",
                package_id, str(exc)[:180],
            )
        return {}


"""Rerank retrieved candidates with the local vLLM model.

The retrieval stage has to be cheap and is deliberately generous, so its order
is a rough first pass.  Measured on a real bilingual package, the embedding
model alone could not separate concepts (a Chinese policy document scored 0.532
against "图像" while the actual image paper scored 0.552), which is why a
question like "人工智能图像" surfaced Chinese-policy files.

This module adds a second, stronger stage: the already deployed local model
reads the question together with the candidate titles and reorders them.  That
is joint encoding, so cross-lingual and multi-concept questions work without
any hand-maintained glossary.  Measured: 4/4 test questions placed the expected
file first, in 0.7-1.4s.

Safety contract:

* only the local vLLM endpoint is ever called (never an Ollama endpoint);
* the candidate set is never reduced - the model's order is applied first and
  everything it did not mention keeps its original relative order behind it, so
  this can never lose evidence;
* any failure at all - timeout, transport error, unparsable output, disabled by
  configuration - returns the original order untouched.
"""

import json
import os
import re
import urllib.request
from typing import Any, Dict, List, Mapping, Sequence

# The rerank pass is optional and independently switchable so it can be turned
# off in the field without a code change.
RERANK_ENV_FLAG = "SJFX_LARGE_PACKAGE_RERANK"
RERANK_TIMEOUT_SECONDS = 8
RERANK_CANDIDATES = 20
RERANK_MAX_TOKENS = 160

RERANK_SYSTEM_PROMPT = (
    "/no_think 你是检索相关性排序助手。用户给出一个中文问题和候选文件列表。"
    "请只输出真正与问题相关的文件编号，按相关性从高到低排列，用逗号分隔，"
    "例如 3,7,1。不相关的文件不要输出。不要解释、不要输出其他内容。"
    "如果没有任何文件相关，只输出：无"
)


def reranking_enabled() -> bool:
    value = str(os.getenv(RERANK_ENV_FLAG, "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _rerank_endpoint() -> tuple:
    """Expose only the explicitly selected generation service."""
    try:
        from config import Config
    except Exception:
        return "", ""
    if not getattr(Config, "LLM_ENABLED", False):
        return "", ""
    base_url = str(getattr(Config, "LLM_BASE_URL", "") or "").rstrip("/")
    model = str(getattr(Config, "LLM_MODEL", "") or "")
    if not base_url or not model:
        return "", ""
    return base_url, model


def _parse_order(raw: str, size: int) -> List[int]:
    text = re.sub(r"(?s) thinking.*?<｜end▁of▁thinking｜>", "", str(raw or ""))
    if "无" in text and not re.search(r"\d", text):
        return []
    order: List[int] = []
    for value in re.findall(r"\d{1,3}", text):
        index = int(value)
        if 1 <= index <= size and index not in order:
            order.append(index)
    return order


def rerank_candidates(query: str, results: Sequence[Mapping[str, Any]],
                      logger: Any = None) -> List[Mapping[str, Any]]:
    """Reorder candidates with the local model; never drops any of them."""
    items = list(results)
    if len(items) < 3 or not reranking_enabled():
        return items
    question = str(query or "").strip()
    if not question:
        return items
    base_url, model = _rerank_endpoint()
    if not base_url:
        return items

    pool = items[:RERANK_CANDIDATES]
    from services.answer_quality import extract_passage, enabled
    listing = "\n".join(
        "{}. {}{}".format(position, str(item.get("source_path") or ""),
                          " | " + extract_passage(item.get("original_text") or item.get("text"), question, 420) if enabled() else "")
        for position, item in enumerate(pool, 1)
    )
    try:
        from config import Config
        from services.model_backend import create_model_client
        client = create_model_client(Config, timeout=RERANK_TIMEOUT_SECONDS, max_concurrency=1)
        if client is None:
            return items
        body = client.chat(
            RERANK_SYSTEM_PROMPT,
            "用户问题：{}\n\n候选文件：\n{}\n\n相关文件编号：".format(question, listing),
            temperature=0.0, max_tokens=RERANK_MAX_TOKENS,
            timeout=RERANK_TIMEOUT_SECONDS, retries=0,
        )
        raw = body.get("content") or ""
    except Exception as exc:  # reranking is optional, never fatal
        if logger is not None:
            logger.info("large-package rerank unavailable: %s", str(exc)[:160])
        return items

    order = _parse_order(raw, len(pool))
    if not order:
        return items
    ordered = [pool[index - 1] for index in order]
    chosen = {id(item) for item in ordered}
    return ordered + [item for item in items if id(item) not in chosen]


def collect_evidence(retrieval_request: Any, query: str, files: Sequence[Mapping[str, Any]],
                     store: Any, retrieve_evidence: Any, logger: Any = None,
                     relaxed: bool = False) -> Dict[str, Any]:
    """High-recall hybrid retrieval over a large package's persisted summaries."""
    scan_id = str(retrieval_request.scan_id)
    plan = query_plan(query)
    rows = []
    summaries = []
    for file in files or []:
        path = str(file.get("path") or "")
        if not path:
            continue
        summary = str(file.get("summary") or "").strip()
        if not summary:
            # A summary-less file cannot become a citable evidence item.
            continue
        name = str(file.get("name") or "") or Path(path.split("::", 1)[-1]).name
        category = str(file.get("category") or "")
        rows.append({
            "evidence_id": "large:{}:{}".format(scan_id, path),
            "source_path": path,
            "text": summary,
            "original_text": summary,
            "location": {"source": "file_summary"},
            "evidence_role": "文件摘要",
            "archive_source_path": path,
            "_summary": summary,
            "_category": category,
            "_deep": str(file.get("deep_status") or ""),
            "_quick": str(file.get("quick_status") or ""),
            "_haystack": "{} {} {} {}".format(path, name, category, summary).casefold(),
            "_meta_haystack": "{} {} {}".format(path, name, category).casefold(),
        })
        summaries.append("{} {} {}".format(category, name, summary).casefold())

    limit = max(1, min(int(getattr(retrieval_request, "top_k", 0) or 0), 50))
    if not rows:
        return {
            "query": query, "scope": ".", "method": "large-package-hybrid",
            "corpus_chunks": 0, "result_count": 0, "results": [],
            "match_type_counts": {}, "index_mode": "large-package-hybrid",
            "warnings": ["当前范围没有可检索正文证据。"],
            "retrieval_stats": {"corpus": 0, "recalled": 0, "kept": 0},
        }

    # The shared ranker supplies calibrated keyword (BM25) and, for small
    # corpora, semantic (TF-IDF cosine) terms.  The per-file hybrid score
    # below adds the title/topic and quality terms it cannot see.
    payload = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in rows
    ]
    lexical = retrieve_evidence(
        {}, query, top_k=50, per_source_limit=8, indexed_chunks=payload,
    )
    lexical_scores = {}
    for item in lexical.get("results") or []:
        path = str(item.get("source_path") or "")
        if path:
            lexical_scores[path] = (
                float(item.get("bm25_score") or 0.0),
                float(item.get("vector_score") or 0.0),
            )
    vector_ready = "TF-IDF" in str(lexical.get("method") or "")
    local_scores = (
        [0.0] * len(rows) if vector_ready else local_semantic(plan, summaries)
    )
    grams = plan["grams"]
    # Optional real-embedding recall.  ``{}`` means unchanged behaviour.
    embedding_scores = embedding_similarity(
        store, scan_id, query, [row["source_path"] for row in rows], logger,
    )

    candidates = []
    for index, row in enumerate(rows):
        keyword, vector = lexical_scores.get(row["source_path"], (0.0, 0.0))
        semantic = vector
        if not semantic:
            # Either the corpus was too large for the shared vector pass or
            # this file sat outside its top results; use the local signal.
            semantic = min(1.0, local_scores[index] * 1.6)
        embedded = embedding_scores.get(row["source_path"])
        if embedded is not None and embedded > semantic:
            # A real embedding match, typically a paraphrase or a cross-lingual
            # hit the keyword pass cannot see.  It only raises the semantic
            # term; the weighted formula and every threshold stay as they are.
            semantic = embedded
        matched = [gram for gram in grams if gram in row["_haystack"]]
        meta_hits = [gram for gram in grams if gram in row["_meta_haystack"]]
        meta = 1.0 if meta_hits else 0.0
        if not meta and row["_category"] and row["_category"].casefold() in plan["text"]:
            meta = 0.5
        if not keyword and matched:
            # A file the shared ranker scored at zero is a title/topic-recall
            # candidate only.  Score it by how much of the question it covers
            # so a single generic overlap cannot clear the evidence floor.
            keyword = min(1.0, 2.0 * len(matched) / max(1, len(grams)))
        quality = summary_quality(row)
        score = 0.45 * keyword + 0.35 * semantic + 0.15 * meta + 0.05 * quality
        recalled = (
            bool(matched)
            or keyword > 0.0
            or semantic >= LARGE_SEMANTIC_RECALL_FLOOR
            or meta > 0.0
        )
        candidates.append({
            "row": row, "path": row["source_path"], "score": score,
            "recalled": recalled, "keyword": keyword, "semantic": semantic,
            "meta": meta, "quality": quality, "matched": matched,
            "meta_hits": meta_hits,
        })

    recalled_rows = [item for item in candidates if item["recalled"]]
    kept = [item for item in recalled_rows if item["score"] >= LARGE_EVIDENCE_FLOOR]
    relaxed_note = None
    if not kept and relaxed:
        # A generic overview question ("这个数据包讲了什么") legitimately has no
        # keyword overlap with every summary.  Broad intents therefore fall
        # back to a bounded, score-ordered window of the whole scope and say so
        # instead of returning nothing, exactly as the previous
        # large-package path did.
        kept = sorted(candidates, key=lambda item: (-item["score"], item["path"]))[:limit]
        relaxed_note = "概括问题未命中具体关键词，本轮选取范围内的有限文件摘要，不代表已阅读全部文件。"
    kept.sort(key=lambda item: (-item["score"], item["path"]))
    kept = kept[:limit]

    results = []
    for item in kept:
        row = {
            key: value for key, value in item["row"].items()
            if not key.startswith("_")
        }
        matched = item["matched"][:12]
        keyword = item["keyword"]
        semantic = item["semantic"]
        if keyword >= 0.5 and semantic >= 0.30:
            match_type, reason = "hybrid", "关键词与语义同时命中"
        elif keyword > 0.0:
            match_type, reason = "keyword", "命中关键词：" + "、".join(matched[:5])
        elif semantic >= LARGE_SEMANTIC_RECALL_FLOOR:
            match_type, reason = "semantic", "语义相似度 {:.2f}".format(semantic)
        else:
            match_type, reason = "metadata", "标题/主题与问题相关"
        row["retrieval_score"] = round(float(item["score"]), 6)
        row["bm25_score"] = round(float(keyword), 6)
        row["vector_score"] = round(float(semantic), 6)
        row["match_type"] = match_type
        row["matched_keywords"] = matched
        row["match_reason"] = reason
        results.append(row)

    match_counts = {}
    for item in results:
        key = item.get("match_type") or "unknown"
        match_counts[key] = match_counts.get(key, 0) + 1
    warnings = [] if results else ["未找到与问题有明显相关性的证据。"]
    if relaxed_note and results:
        warnings = [relaxed_note]
    return {
        "query": query,
        "scope": ".",
        "method": "大数据包高召回混合检索（关键词 0.45 + 语义 0.35 + 标题/主题 0.15 + 质量 0.05）",
        "corpus_chunks": len(rows),
        "result_count": len(results),
        "results": results,
        "match_type_counts": match_counts,
        "index_mode": "large-package-hybrid",
        "warnings": warnings,
        "retrieval_stats": {
            "corpus": len(rows),
            "recalled": len(recalled_rows),
            "kept": len(results),
            "semantic_vector_ready": vector_ready,
            "embedding_recall": len(embedding_scores),
            "expanded_terms": len(plan["expansions"]),
            "top_k": limit,
        },
    }
