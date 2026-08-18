import math
import re
from collections import Counter, defaultdict


TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,}")


def _tokens(text):
    output = []
    for value in TOKEN_RE.findall(str(text or "").lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", value):
            output.append(value)
            for size in (2, 3):
                output.extend(value[index:index + size] for index in range(max(0, len(value) - size + 1)))
        else:
            output.append(value)
    return output[:20000]


def _in_scope(path, scope):
    if not scope or scope == ".":
        return True
    prefix = scope.rstrip("/") + "/"
    return path == scope or path.startswith(prefix)


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
        if not _in_scope(path, scope):
            continue
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
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            if len(text) < 2:
                continue
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
                "bbox": item.get("bbox"),
                "score": item.get("score"),
                "parser": item.get("parser"),
                "source_sha256": item.get("source_sha256") or source.get("sha256"),
                "content_sha256": item.get("content_sha256"),
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
    if len(texts) > 2500:
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


def retrieve_evidence(documents, query, scope=".", top_k=8, per_source_limit=3, candidate_evidence_ids=None):
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    if not query:
        raise ValueError("检索问题不能为空")
    chunks = evidence_corpus(documents, scope=scope)
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
            "warnings": ["当前范围没有可检索正文证据。"],
        }
    # File paths are metadata, not document content. Including them here lets
    # names such as "Q3" or "财报" contaminate BM25/TF-IDF ranking.
    texts = ["{} {}".format(item.get("section") or "", item["text"][:5000]) for item in chunks]
    tokenized = [_tokens(text) for text in texts]
    bm25 = _bm25_scores(tokenized, _tokens(query))
    vectors, vector_ready = _tfidf_scores(texts, query)
    ranked = []
    for index, item in enumerate(chunks):
        lexical = bm25[index]
        vector = vectors[index]
        combined = 0.58 * lexical + 0.42 * vector if vector_ready else lexical
        if combined <= 0:
            continue
        ranked.append((combined, lexical, vector, item))
    ranked.sort(key=lambda value: (-value[0], value[3].get("source_path") or "", value[3].get("evidence_id") or ""))
    source_counts = defaultdict(int)
    results = []
    for combined, lexical, vector, item in ranked:
        source = item.get("source_path") or ""
        if source_counts[source] >= per_source_limit:
            continue
        source_counts[source] += 1
        payload = dict(item)
        payload["retrieval_score"] = round(float(combined), 6)
        payload["bm25_score"] = round(float(lexical), 6)
        payload["vector_score"] = round(float(vector), 6)
        results.append(payload)
        if len(results) >= max(1, min(int(top_k), 50)):
            break
    return {
        "query": query,
        "scope": scope,
        "method": "BM25 + 本地 TF-IDF 字符向量" if vector_ready else "BM25",
        "corpus_chunks": len(chunks),
        "result_count": len(results),
        "results": results,
        "warnings": [] if results else ["未找到与问题有明显相关性的证据。"],
    }


def build_retrieval_manifest(documents, topic_clusters=None, max_queries=5):
    topic_clusters = topic_clusters or []
    queries = []
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
            frequency.update(token for token in _tokens(item.get("text")) if 2 <= len(token) <= 16 and token not in stop)
        terms = [term for term, _count in frequency.most_common(8)]
        queries = [" ".join(terms)] if terms else ["核心主题 关键发现 主要结论"]
    searches = [retrieve_evidence(documents, query, top_k=6) for query in queries]
    return {
        "schema_version": "local-retrieval/1.0",
        "method": searches[0]["method"] if searches else "BM25",
        "evidence_chunks": len(evidence_corpus(documents)),
        "queries": searches,
        "remote_services_enabled": False,
    }
