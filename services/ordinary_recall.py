"""Semantic file recall for ordinary (non-large) data packages.

Deliberately independent of the isolated large-package path: this module never
imports ``services.large_package_recall`` and never reads
``large_package_store`` or ``large_catalog_members``.

It exists because ordinary retrieval is lexical -- BM25 plus local TF-IDF
character vectors over the persisted evidence index -- so a Chinese question
over an English package shares no terms with the text that actually answers it.
Only files that happened to carry a Chinese translation could be found, which
is why a "search the whole package" question could return two or three files.

A multilingual vector score per file restores that recall.  Every function
returns an empty result on any problem at all -- embeddings disabled, service
down or slow, no usable vectors -- so the existing lexical behaviour stays
exactly as it was when this cannot help.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# Bound the work per call.  Files without a score simply keep their lexical
# behaviour, so a cap can only reduce the extra recall, never break a search.
EMBED_MAX_FILES = 240
EMBED_TEXT_CHARS = 1200
NEW_VECTORS_PER_CALL = 240

# A package-level question already merges every summary, so semantic recall is
# only needed when the lexical pass was too thin to be a package answer.
SEMANTIC_MIN_FILES = 6
SEMANTIC_TOP_FILES = 12

VECTOR_CACHE_PREFIX = "ordinary-file"
# A warm-up embeds without a question, so it uses a fixed neutral query key.
WARM_QUERY = "package overview"


def _client():
    try:
        from config import Config
    except Exception:
        return None
    if not getattr(Config, "ENABLE_VLLM_EMBEDDINGS", False):
        return None
    try:
        from services.vllm import VLLMEmbeddingClient
        return VLLMEmbeddingClient(
            Config.VLLM_EMBED_BASE_URL, Config.VLLM_EMBED_MODEL,
            timeout=Config.VLLM_EMBED_TIMEOUT_SECONDS,
            max_batch_size=Config.VLLM_EMBED_MAX_BATCH_SIZE,
            max_chars=Config.VLLM_EMBED_MAX_CHARS,
            api_key=Config.VLLM_API_KEY,
        )
    except Exception:
        return None


def _flatten(vector: Any) -> List[float]:
    values = []
    for value in vector or []:
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return []
    return values


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


def _file_text(name: str, summary: str, document: Optional[Mapping[str, Any]]) -> str:
    """The passage embedded for one file: its name, summary and opening text."""
    parts = [str(name or "")]
    text = str(summary or "").strip()
    if text:
        parts.append(text)
    if isinstance(document, Mapping):
        body = str(document.get("text") or "").strip()
        if body:
            parts.append(body[:400])
    value = " ".join(part for part in parts if part)
    return value[:EMBED_TEXT_CHARS]


def file_vectors(storage: Any, scan_id: str, paths: Sequence[str],
                 summaries: Optional[Iterable[Mapping[str, Any]]] = None,
                 logger: Any = None) -> Dict[str, List[float]]:
    """Return ``{path: vector}`` for the package's files, cached in SQLite.

    Called by the query path and by the index refresh, so the first question
    after a package changes does not pay for embedding every file.
    """
    wanted = [str(path) for path in (paths or []) if str(path)][:EMBED_MAX_FILES]
    if not wanted:
        return {}
    client = _client()
    if client is None:
        return {}
    by_path = {}
    for item in summaries or []:
        path = str((item or {}).get("path") or "")
        if path:
            by_path[path] = item or {}

    texts = {}
    cache_keys = {}
    for path in wanted:
        entry = by_path.get(path) or {}
        document = None
        try:
            document = storage.get_document(scan_id, path)
        except Exception:
            document = None
        text = _file_text(entry.get("name") or path.split("::")[-1], entry.get("summary"), document)
        if not text:
            continue
        texts[path] = text
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        cache_keys[path] = "{}::{}::{}::{}".format(
            VECTOR_CACHE_PREFIX, scan_id, path, digest)
    if not texts:
        return {}

    model = str(getattr(client, "model", "") or "")
    try:
        cached = storage.get_embeddings(list(cache_keys.values()), model)
    except Exception:
        cached = {}

    vectors: Dict[str, List[float]] = {}
    missing = []
    for path, key in cache_keys.items():
        vector = _flatten((cached or {}).get(key))
        if vector:
            vectors[path] = vector
        else:
            missing.append(path)

    if missing:
        missing = missing[:NEW_VECTORS_PER_CALL]
        batch_size = max(1, int(getattr(client, "max_batch_size", 8) or 8))
        fresh = {}
        for start in range(0, len(missing), batch_size):
            group = missing[start:start + batch_size]
            try:
                encoded = client.embed(["passage: " + texts[path] for path in group])
            except Exception:
                if logger is not None:
                    logger.warning("ordinary semantic recall: embedding failed", exc_info=True)
                break
            for path, vector in zip(group, encoded or []):
                values = _flatten(vector)
                if values:
                    vectors[path] = values
                    fresh[cache_keys[path]] = values
        if fresh:
            try:
                storage.save_embeddings(model, fresh)
            except Exception:
                pass
    return vectors


def warm_file_vectors(storage: Any, scan_id: str, paths: Sequence[str],
                      summaries: Optional[Iterable[Mapping[str, Any]]] = None,
                      logger: Any = None) -> int:
    """Pre-embed a package's files after its index is refreshed."""
    return len(file_vectors(storage, scan_id, paths, summaries=summaries, logger=logger))


def file_semantic_scores(storage: Any, scan_id: str, query: str,
                         paths: Sequence[str],
                         summaries: Optional[Iterable[Mapping[str, Any]]] = None,
                         logger: Any = None) -> Dict[str, float]:
    """Return ``{path: cosine}`` for the question's nearest files, else ``{}``."""
    question = str(query or "").strip()
    if not question:
        return {}
    client = _client()
    if client is None:
        return {}
    vectors = file_vectors(storage, scan_id, paths, summaries=summaries, logger=logger)
    if not vectors:
        return {}
    try:
        encoded_query = client.embed(["query: " + question[:512]])
    except Exception:
        return {}
    query_vector = _flatten((encoded_query or [None])[0])
    if not query_vector:
        return {}
    return {
        path: round(_cosine(query_vector, vector), 6)
        for path, vector in vectors.items()
    }


def augment_candidates(indexed: Sequence[Mapping[str, Any]],
                       semantic_scores: Mapping[str, float],
                       package_context: Optional[Mapping[str, Any]],
                       scan_id: str,
                       top_files: int = SEMANTIC_TOP_FILES,
                       min_files: int = SEMANTIC_MIN_FILES) -> List[Dict[str, Any]]:
    """Add the semantically nearest files when the lexical pass was too thin."""
    merged = [dict(item) for item in indexed or []]
    if not semantic_scores:
        return merged
    current = {str(item.get("source_path") or "") for item in merged}
    current.discard("")
    if len(current) >= min_files:
        return merged
    summaries = {
        str(item.get("path") or ""): item
        for item in ((package_context or {}).get("file_summaries") or [])
    }
    added = 0
    for path, score in sorted(semantic_scores.items(), key=lambda kv: -kv[1]):
        if added >= max(1, int(top_files)):
            break
        if path in current:
            continue
        summary = str((summaries.get(path) or {}).get("summary") or "").strip()
        if not summary:
            continue
        current.add(path)
        added += 1
        merged.append({
            "evidence_id": "semantic-summary:{}:{}".format(scan_id, path),
            "source_path": path,
            "text": summary,
            "original_text": summary,
            "location": {"source": "semantic_file_summary"},
            "evidence_role": "语义相关文件摘要",
            "semantic_score": round(float(score), 6),
            "archive_source_path": path,
        })
    return merged

