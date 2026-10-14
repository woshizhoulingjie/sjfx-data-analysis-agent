import math
import re
import unicodedata
from collections import Counter, defaultdict

from services.evidence import evidence_quality
from services import large_qa_thai as thai


TOKEN_RE = re.compile(
    # Keep the historical ASCII identifier form (so values such as ``Q3``
    # remain one token), while also accepting words from every Unicode script.
    # ``[^\W\d_]`` is the Unicode-letter class in Python's ``re`` engine;
    # unlike the old Latin/Chinese-only expression it includes Cyrillic,
    # Arabic, Greek, etc.  Numeric-only fragments remain noise.
    r"[^\W\d_][^\W_]*(?:[_.-][^\W_]+)*"
)


def _tokens(text, warnings=None):
    text = str(text or "")
    output = []
    for value in TOKEN_RE.findall(text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", value):
            output.append(value)
            for size in (2, 3):
                output.extend(value[index:index + size] for index in range(max(0, len(value) - size + 1)))
        elif not thai.has_thai(value):
            # Do not keep one-character Latin noise, and do not absorb
            # sentence punctuation into the searchable token.
            # Thai retains its dedicated offline tokenizer and failure contract;
            # generic Unicode letter fragments must not bypass a missing dictionary.
            if len(value) > 1:
                output.append(value)
    if thai.has_thai(text) and len(output) < 20000:
        try:
            # The shared FTS tokenizer encodes terms as ASCII; ordinary BM25
            # and automatic query suggestions can retain the Unicode words.
            output.extend(bytes.fromhex(term[2:]).decode("utf-8")
                          for term in thai.thai_tokens(text))
        except thai.ThaiIndexError as exc:
            if warnings is None:
                raise
            message = "泰文关键词检索暂不可用（离线分词器或词典未就绪）：{}".format(exc)
            if message not in warnings:
                warnings.append(message)
    return output[:20000]


def _thai_surface_matches(text, tokens, query_tokens):
    """Recover exact Thai substrings when contextual word boundaries differ.

    Expand only bounded query terms, preserving tones and whitespace boundaries;
    the existing Chinese/English token sequence and evidence scope stay intact.
    """
    if not thai.has_thai(text) or len(tokens) >= 20000:
        return
    present = set(tokens)
    runs = None
    for term in dict.fromkeys(query_tokens):
        if term in present or not thai.THAI.fullmatch(term):
            continue
        if runs is None:
            runs = thai.THAI.findall(unicodedata.normalize("NFC", text))
        if any(term in run for run in runs):
            tokens.append(term)
            if len(tokens) >= 20000:
                return


def _in_scope(path, scope):
    if not scope or scope == ".":
        return True
    prefix = scope.rstrip("/") + "/"
    # Archive members are virtual evidence paths.  Selecting the physical
    # container must include ``archive.zip::member.pdf`` without broadening the
    # scope to unrelated files.
    return path == scope or path.startswith(prefix) or path.startswith(scope + "::")


def _scope_matches(scope, *paths):
    """Match either a logical path or its physical archive path.

    An indexed archive member has ``source_path=bundle.zip::member`` and
    ``archive_source_path=bundle.zip``.  Scope checks must retain the logical
    member path while still allowing a container-wide query to match the
    physical path.  Callers pass both values instead of selecting one with
    ``or`` (which used to hide member scopes).
    """
    return any(_in_scope(str(path or ""), scope) for path in paths if path)


def _document_items(documents):
    if isinstance(documents, dict):
        return list(documents.items())
    return [(item.get("path", ""), item.get("payload", item)) for item in documents]


def evidence_corpus(documents, scope="."):
    """Create deterministic, deduplicated retrieval chunks from evidence units."""
    chunks = []
    seen = set()
    for path, document in _document_items(documents):
        if not path:
            path = document.get("source", {}).get("path", "")
        source = document.get("source", {})
        evidence = list(document.get("evidence", []) or [])
        if not evidence and document.get("text"):
            text = document["text"]
            evidence = [
                {
                    "evidence_id": "TEXT-{}-{:05d}".format(source.get("sha256", "")[:10], index // 1200 + 1),
                    "source_path": path,
                    "page": None,
                    "section": None,
                    "label": "text_chunk",
                    "text": text[index:index + 1200],
                    "source_sha256": source.get("sha256"),
                }
                for index in range(0, len(text), 1200)
            ]
        for item in evidence:
            # Preview snippets are searchable discovery material.  They remain
            # explicitly marked preview_only and are never counted as formal
            # evidence, but the corpus must retain them for candidate search.
            is_preview = bool(item.get("preview_only"))
            item_coverage = item.get("coverage") or {}
            if str(item_coverage.get("level") or "").lower() == "preview" and not is_preview:
                continue
            if "formal_evidence_ready" in item_coverage and not bool(item_coverage.get("formal_evidence_ready")):
                continue
            if not _scope_matches(
                scope, path, item.get("source_path"), item.get("archive_source_path")
            ):
                continue
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            if len(text) < 2:
                continue
            quality = evidence_quality(item)
            # Interactive search must not promote headings/question prompts as
            # evidence merely because they repeat the user's query words.
            if not quality.get("eligible") and not is_preview:
                continue
            if is_preview:
                quality = dict(quality)
                quality.update({"eligible": True, "preview_only": True, "reason": quality.get("reason") or "轻量预览候选"})
            key = (path, item.get("page"), item.get("section"), item.get("content_sha256") or text)
            if key in seen:
                continue
            seen.add(key)
            chunks.append({
                "evidence_id": item.get("evidence_id"),
                "source_path": item.get("source_path") or path,
                "page": item.get("page"),
                "section": item.get("section"),
                "label": item.get("label"),
                "text": text,
                "original_text": str(item.get("original_text") or item.get("text") or ""),
                "translated_text": str(item.get("translated_text") or "") or None,
                "source_language": item.get("source_language"),
                "target_language": item.get("target_language"),
                "translation_source": item.get("translation_source"),
                "bbox": item.get("bbox"),
                "score": item.get("score"),
                "parser": item.get("parser"),
                "source_sha256": item.get("source_sha256") or source.get("sha256"),
                "content_sha256": item.get("content_sha256"),
                "archive_source_path": item.get("archive_source_path"),
                "archive_member": item.get("archive_member"),
                "paragraph_index": item.get("paragraph_index"),
                "block_index": item.get("block_index"),
                "char_start": item.get("char_start"),
                "char_end": item.get("char_end"),
                "preview_only": bool(item.get("preview_only")),
                "preview_window": item.get("preview_window"),
                "preview_window_char_start": item.get("preview_window_char_start"),
                "preview_window_char_end": item.get("preview_window_char_end"),
                "source_byte_start": item.get("source_byte_start"),
                "source_byte_end": item.get("source_byte_end"),
                "parser_version": item.get("parser_version"),
                "evidence_quality": quality,
            })
    return chunks


def _bm25_scores(tokenized, query_tokens, k1=1.5, b=0.75):
    if not tokenized or not query_tokens:
        return [0.0] * len(tokenized)
    total = len(tokenized)
    avg_length = sum(len(tokens) for tokens in tokenized) / float(total or 1)
    frequencies = [Counter(tokens) for tokens in tokenized]
    document_frequency = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))
    scores = []
    for tokens, frequency in zip(tokenized, frequencies):
        length_norm = 1.0 - b + b * len(tokens) / float(avg_length or 1)
        score = 0.0
        for term in query_tokens:
            count = frequency.get(term, 0)
            if not count:
                continue
            df = document_frequency.get(term, 0)
            inverse = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            score += inverse * count * (k1 + 1.0) / (count + k1 * length_norm)
        scores.append(score)
    maximum = max(scores or [0.0])
    return [score / maximum if maximum else 0.0 for score in scores]


def _tfidf_scores(texts, query):
    # BM25 remains useful for large corpora; do not allocate a dense feature
    # vocabulary for every evidence block while Docling is using the same RAM.
    # Character TF-IDF is useful for a small result set, but becomes
    # disproportionately expensive for broad file searches. BM25 remains
    # deterministic and fast for larger corpora.
    if len(texts) > 250:
        return [0.0] * len(texts), False
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 4), min_df=1, max_features=12000, sublinear_tf=True)
        matrix = vectorizer.fit_transform(texts + [query])
        values = (matrix[:-1] @ matrix[-1].T).toarray().reshape(-1).tolist()
        maximum = max(values or [0.0])
        return [float(value / maximum) if maximum else 0.0 for value in values], True
    except Exception:
        return [0.0] * len(texts), False


def _match_type(item):
    """Classify the persisted evidence channel used for a hit.

    The evidence index is shared by original text, bounded previews and
    translation units.  Keeping the classification beside retrieval means
    every caller gets the same truthful label instead of inferring it from a
    display string.
    """
    item = item or {}
    index_kind = str(item.get("index_kind") or "").strip().casefold()
    label = str(item.get("label") or "").strip().casefold()
    if item.get("relation_hit") or index_kind in {"relation", "relationship"}:
        return "relation"
    if index_kind in {"metadata", "file_metadata", "catalog_metadata"}:
        return "metadata"
    if index_kind in {"summary", "document_summary", "folder_summary"} or label in {
        "summary", "document_summary", "folder_summary",
    }:
        return "summary"
    if index_kind == "translation" or label == "translation_unit":
        return "translation"
    if bool(item.get("preview_only")):
        return "preview_text"
    return "full_text"


def retrieve_evidence(documents, query, scope=".", top_k=8, per_source_limit=3,
                      candidate_evidence_ids=None, indexed_chunks=None,
                      use_tfidf=True, semantic_scores=None, token_cache=None):
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    if not query:
        raise ValueError("检索问题不能为空")
    if indexed_chunks is None:
        chunks = evidence_corpus(documents, scope=scope)
        index_mode = "rebuilt-corpus"
    else:
        chunks = [
            dict(item) for item in indexed_chunks
            if _scope_matches(
                scope, item.get("source_path"), item.get("archive_source_path")
            )
        ]
        index_mode = "persistent-evidence-index"
    if candidate_evidence_ids is not None:
        allowed = {str(value) for value in candidate_evidence_ids}
        chunks = [item for item in chunks if str(item.get("evidence_id")) in allowed]
    if not chunks:
        return {
            "query": query,
            "scope": scope,
            "method": "BM25 + 本地 TF-IDF 字符向量",
            "corpus_chunks": 0,
            "results": [],
            "match_type_counts": {},
            "warnings": ["当前范围没有可检索正文证据。"],
            "index_mode": index_mode,
        }
    # File paths are metadata, not document content. Including them here lets
    # names such as "Q3" or "财报" contaminate BM25/TF-IDF ranking.
    from services import answer_quality as aq
    texts = [
        "{} {} {}".format(
            item.get("section") or "",
            aq.extract_passage(item["text"], query, 5000) if aq.enabled() else item["text"][:5000],
            str(item.get("translated_text") or "")[:5000],
        )
        for item in chunks
    ]
    warnings = []
    query_tokens = _tokens(query, warnings)
    if token_cache is None:
        tokenized = [_tokens(text, warnings) for text in texts]
    else:
        # Optional job-local reuse for repeated opportunity queries. No global
        # cache, no persisted summary rows, and scope filtering still runs above.
        tokenized = []
        for text in texts:
            if text not in token_cache:
                token_cache[text] = _tokens(text, warnings)
            tokenized.append(list(token_cache[text]))
    if thai.has_thai(query):
        for text, terms in zip(texts, tokenized):
            _thai_surface_matches(text, terms, query_tokens)
    bm25 = _bm25_scores(tokenized, query_tokens)
    vectors, vector_ready = _tfidf_scores(texts, query) if use_tfidf else ([0.0] * len(texts), False)
    def _semantic_for(item):
        if not semantic_scores:
            return 0.0
        for key in (item.get("archive_source_path"), item.get("source_path")):
            key = str(key or "")
            if key and key in semantic_scores:
                return float(semantic_scores[key])
        return 0.0

    ranked = []
    for index, item in enumerate(chunks):
        lexical = bm25[index]
        vector = vectors[index]
        combined = 0.58 * lexical + 0.42 * vector if vector_ready else lexical
        semantic = _semantic_for(item)
        if semantic_scores is not None:
            # Cross-lingual recall: the right passage need not share a single
            # keyword with the question (a Chinese question over English text
            # shares none), so a semantic score has to be able to carry a
            # passage past the "no lexical match" filter below.
            combined = 0.6 * combined + 0.4 * semantic
        if combined <= 0:
            continue
        ranked.append((combined, lexical, vector, semantic, item))
    ranked.sort(key=lambda value: (-value[0], value[4].get("source_path") or "", value[4].get("evidence_id") or ""))
    source_counts = defaultdict(int)
    results = []
    for combined, lexical, vector, semantic, item in ranked:
        source = item.get("source_path") or ""
        if source_counts[source] >= per_source_limit:
            continue
        source_counts[source] += 1
        payload = dict(item)
        payload["retrieval_score"] = round(float(combined), 6)
        payload["bm25_score"] = round(float(lexical), 6)
        payload["vector_score"] = round(float(vector), 6)
        if semantic_scores is not None:
            payload["semantic_score"] = round(float(semantic), 6)
        payload["match_type"] = _match_type(payload)
        results.append(payload)
        if len(results) >= max(1, min(int(top_k), 50)):
            break
    match_type_counts = Counter(item.get("match_type") or "full_text" for item in results)
    return {
        "query": query,
        "scope": scope,
        "method": (
            "BM25 + 本地 TF-IDF 字符向量"
            + (" + 文件级多语言语义排序" if semantic_scores is not None else "")
        ) if vector_ready else ("BM25 + 文件级多语言语义排序" if semantic_scores is not None else "BM25"),
        "corpus_chunks": len(chunks),
        "result_count": len(results),
        "results": results,
        "match_type_counts": dict(match_type_counts),
        "warnings": warnings + ([] if results else ["未找到与问题有明显相关性的证据。"]),
        "index_mode": index_mode,
    }


def build_retrieval_manifest(documents, topic_clusters=None, max_queries=5):
    topic_clusters = topic_clusters or []
    queries = []
    warnings = []
    for cluster in topic_clusters:
        topic = str(cluster.get("topic") or "").strip()
        if topic and topic not in queries:
            queries.append(topic)
        if len(queries) >= max_queries:
            break
    if not queries:
        stop = {"核心", "主题", "主要", "文件", "文档", "内容", "分析", "资料", "报告", "数据"}
        frequency = Counter()
        for item in evidence_corpus(documents):
            frequency.update(token for token in _tokens(item.get("text"), warnings) if 2 <= len(token) <= 16 and token not in stop)
        terms = [term for term, _count in frequency.most_common(8)]
        queries = [" ".join(terms)] if terms else ["核心主题 关键发现 主要结论"]
    searches = [retrieve_evidence(documents, query, top_k=6) for query in queries]
    return {
        "schema_version": "local-retrieval/1.0",
        "method": searches[0]["method"] if searches else "BM25",
        "evidence_chunks": len(evidence_corpus(documents)),
        "queries": searches,
        "warnings": warnings,
        "remote_services_enabled": False,
    }
