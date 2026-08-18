"""Bounded, topic-aware projections of the local evidence corpus.

The parser keeps all evidence units for retrieval and audit.  User-facing
reports and exports use this module so a long document is represented by a
small set of useful excerpts rather than a copy of its entire text.
"""

import hashlib
import math
import re
from collections import defaultdict


TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,8}")
STOPWORDS = {"内容", "文件", "文档", "资料", "分析", "主要", "相关", "情况", "主题", "研究"}
INDIRECT_SIGNALS = {
    "经济影响": ("吞吐", "收入", "产量", "就业", "价格", "增长", "下降", "成本"),
    "冲突": ("伤亡", "袭击", "攻击", "停火", "战斗", "流离失所"),
    "风险": ("损失", "故障", "异常", "事故", "暴露", "攻击"),
    "效果": ("提升", "下降", "准确率", "召回率", "对照", "实验结果"),
}
_EMBEDDING_PROVIDER = None
_EMBEDDING_CACHE = {}
_EMBEDDING_CACHE_LIMIT = 512


def set_embedding_provider(provider):
    """Install an optional batch provider returning one vector per text."""
    global _EMBEDDING_PROVIDER
    _EMBEDDING_PROVIDER = provider
    _EMBEDDING_CACHE.clear()


def embedding_mode():
    return "ollama-embedding" if _EMBEDDING_PROVIDER else "lexical-fallback"


def _cosine(left, right):
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) * float(value) for value in left))
    right_norm = math.sqrt(sum(float(value) * float(value) for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(0.0, min(1.0, numerator / (left_norm * right_norm)))


def _embedding_scores(topic_text, texts, provider):
    if not provider or not topic_text or not texts:
        return {}, "lexical-fallback"
    keys = [hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest() for value in [topic_text] + texts]
    vectors = [None] * len(keys)
    missing_indices = []
    missing_values = []
    for index, key in enumerate(keys):
        if key in _EMBEDDING_CACHE:
            vectors[index] = _EMBEDDING_CACHE[key]
        else:
            missing_indices.append(index)
            missing_values.append(([topic_text] + texts)[index])
    try:
        if missing_values:
            generated = provider(missing_values)
            if len(generated) != len(missing_values):
                raise ValueError("embedding 返回数量不一致")
            for index, vector in zip(missing_indices, generated):
                vectors[index] = vector
                _EMBEDDING_CACHE[keys[index]] = vector
            while len(_EMBEDDING_CACHE) > _EMBEDDING_CACHE_LIMIT:
                _EMBEDDING_CACHE.pop(next(iter(_EMBEDDING_CACHE)))
        query_vector = vectors[0]
        return {index: _cosine(query_vector, vectors[index + 1]) for index in range(len(texts))}, "semantic+lexical"
    except Exception:
        return {}, "lexical-fallback"


def _terms(values):
    text = " ".join(str(value or "") for value in values)
    output = set()
    for token in TOKEN_RE.findall(text):
        token = token.lower()
        if token in STOPWORDS:
            continue
        output.add(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            output.update(token[index:index + size] for size in (2, 3) for index in range(max(0, len(token) - size + 1)))
    return output


def compact_evidence(item, max_chars=520):
    """Copy an evidence item as a bounded excerpt without losing provenance."""
    result = {
        key: item.get(key)
        for key in (
            "evidence_id", "source_path", "page", "section", "label", "bbox",
            "parser", "source_sha256", "content_sha256", "score", "retrieval_score",
        )
        if item.get(key) is not None
    }
    text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
    result["text"] = text[:max_chars] + ("..." if len(text) > max_chars else "")
    result["excerpt"] = len(text) > max_chars
    result.setdefault("evidence_role", "direct_source")
    result.setdefault("derived_from", [])
    return result


def select_evidence(items, topics=None, max_items=24, per_source=2, max_chars=520):
    """Select diverse, topic-aligned evidence from a larger local corpus."""
    candidates = []
    seen = set()
    terms = _terms(topics or [])
    topic_text = " ".join(str(value or "") for value in topics or [])
    indirect_signals = set()
    for topic, signals in INDIRECT_SIGNALS.items():
        if topic in topic_text:
            indirect_signals.update(signals)
    for index, item in enumerate(items or []):
        if not isinstance(item, dict) or not item.get("text"):
            continue
        key = item.get("evidence_id") or (
            item.get("source_path"), item.get("page"), item.get("section"), item.get("text")
        )
        if key in seen:
            continue
        seen.add(key)
        text = " ".join(str(item.get("text") or "").split())
        item_terms = _terms([text, item.get("section")])
        relevance = len(terms.intersection(item_terms)) if terms else 0
        relevance += 2 if indirect_signals.intersection(item_terms) else 0
        if re.search(r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|吨|元|天|次|例)", text):
            relevance += 1
        # Page/section-bearing units are more useful for one-click source review.
        provenance = int(item.get("page") is not None) + int(bool(item.get("section")))
        label_bonus = 1 if item.get("label") in {"title", "section_header", "heading", "table"} else 0
        candidates.append((relevance * 10 + provenance * 2 + label_bonus, -index, item, relevance, provenance, label_bonus))

    semantic_scores = {}
    relevance_mode = "lexical-fallback"
    if _EMBEDDING_PROVIDER and terms and candidates:
        ordered = sorted(candidates, key=lambda value: (-value[0], value[1]))
        pool_size = min(256, len(ordered))
        pool = ordered[:pool_size]
        texts = [" ".join(str(value.get(key) or "") for key in ("section", "text"))[:1800] for _score, _order, value, _relevance, _provenance, _label in pool]
        semantic_scores, relevance_mode = _embedding_scores(topic_text, texts, _EMBEDDING_PROVIDER)
        for pool_index, candidate in enumerate(pool):
            candidate[2]["_semantic_score"] = semantic_scores.get(pool_index, 0.0)

    rescored = []
    for score, order, item, relevance, provenance, label_bonus in candidates:
        semantic = float(item.pop("_semantic_score", 0.0) or 0.0)
        combined = semantic * 80.0 + relevance * 10.0 + provenance * 2.0 + label_bonus
        rescored.append((combined, order, item, semantic, relevance_mode))

    rescored.sort(key=lambda value: (-value[0], value[1]))
    selected = []
    source_counts = defaultdict(int)
    for score, order, item, semantic, mode in rescored:
        source = item.get("source_path") or ""
        if source_counts[source] >= max(1, per_source):
            continue
        compact = compact_evidence(item, max_chars=max_chars)
        compact["semantic_score"] = round(semantic, 6)
        compact["relevance_mode"] = mode
        selected.append(compact)
        source_counts[source] += 1
        if len(selected) >= max(1, max_items):
            break
    # Selection is relevance-aware; presentation follows source order so a
    # reader can reconstruct the document's argument instead of seeing page 15
    # before page 3.
    return sorted(
        selected,
        key=lambda item: (
            item.get("source_path") or "",
            item.get("page") is None,
            item.get("page") if item.get("page") is not None else 10 ** 9,
            item.get("section") or "",
            item.get("evidence_id") or "",
        ),
    )




def evidence_support(
    item,
    topics=None,
    semantic_score=0.0,
    relevance_mode="lexical-fallback",
):
    """为证据生成可审计的支撑说明。"""

    import re

    topic_text = " ".join(
        str(value or "")
        for value in (topics or [])
    )

    text = " ".join(
        str(item.get("text") or "").split()
    )

    section = str(
        item.get("section") or ""
    )

    stopwords = {
        "内容",
        "文件",
        "文档",
        "资料",
        "分析",
        "主要",
        "相关",
        "情况",
        "主题",
        "研究",
    }

    token_re = re.compile(
        r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,8}"
    )

    topic_phrases = []

    for token in token_re.findall(topic_text):
        value = token.lower()

        if value in stopwords:
            continue

        if len(token) >= 2:
            topic_phrases.append(token)

    matched = sorted(
        {
            phrase
            for phrase in topic_phrases
            if (
                phrase.lower() in text.lower()
                or phrase.lower() in section.lower()
            )
        },
        key=lambda value: (
            -len(value),
            value,
        ),
    )[:8]

    normalized = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    sentences = [
        part.strip(" \t,，;；")
        for part in re.split(
            r"(?<=[。！？；.!?;])\s+|[\r\n]+",
            normalized,
        )
        if part.strip(" \t,，;；")
    ]

    best_sentence = ""

    if sentences:
        if matched:
            best_sentence = max(
                sentences,
                key=lambda sentence: (
                    sum(
                        1
                        for term in matched
                        if term.lower()
                        in sentence.lower()
                    ),
                    -abs(
                        len(sentence) - 120
                    ),
                ),
            )
        else:
            best_sentence = max(
                sentences,
                key=lambda sentence:
                    min(
                        len(sentence),
                        220,
                    ),
            )

    quote = best_sentence[:280]

    if len(best_sentence) > 280:
        quote += "..."

    numeric = bool(
        re.search(
            r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|吨|元|天|次|例)",
            text,
        )
    )

    if matched:
        support_type = "字面命中"

        support_reason = (
            "原文直接出现支撑词：{}".format(
                "、".join(matched)
            )
        )

    elif float(
        semantic_score or 0.0
    ) >= 0.55:
        support_type = "语义关联"

        support_reason = (
            "向量判断与主题语义相关，"
            "但未发现主题词字面命中，需人工核验。"
        )

    elif numeric:
        support_type = "事实/数字片段"

        support_reason = (
            "片段包含可核验的数字或量化事实，"
            "是否支撑该主题需结合上下文复核。"
        )

    elif topic_text:
        support_type = "代表性片段"

        support_reason = (
            "未发现明确字面命中或强语义关联，"
            "仅作为位置明确的代表性片段。"
        )

    else:
        support_type = "代表性片段"

        support_reason = (
            "未提供检索主题，"
            "展示该文档的代表性正文片段。"
        )

    return {
        "matched_terms": matched,
        "supporting_quote": quote,
        "support_type": support_type,
        "support_reason": support_reason,
    }


def attach_claim_evidence(summary, items, fields=None, max_items=3):
    """把摘要中的结论与可追溯证据进行绑定。"""

    fields = fields or (
        "key_facts",
        "arguments",
        "conclusions",
        "notable_items",
        "key_findings",
        "uncertainties",
    )

    aspects = {
        "key_facts": "事实",
        "arguments": "论点",
        "conclusions": "结论",
        "notable_items": "重要观察",
        "key_findings": "数据包发现",
        "uncertainties": "不确定性",
    }

    claims = []

    for field in fields:
        values = (
            summary.get(field)
            if isinstance(summary, dict)
            else None
        )

        if not isinstance(values, list):
            continue

        for index, value in enumerate(values):

            if isinstance(value, dict):
                claim_text = str(
                    value.get("claim")
                    or value.get("text")
                    or ""
                ).strip()
            else:
                claim_text = str(
                    value or ""
                ).strip()

            if not claim_text:
                continue

            selected = select_evidence(
                items,
                topics=[claim_text],
                max_items=max_items,
                per_source=1,
                max_chars=520,
            )

            supported = [
                item
                for item in selected
                if (
                    item.get("matched_terms")
                    or float(
                        item.get("semantic_score")
                        or 0.0
                    ) >= 0.55
                )
            ]

            if any(
                item.get("matched_terms")
                for item in supported
            ):
                status = "direct"

            elif supported:
                status = "semantic"

            else:
                status = "insufficient"

            claims.append({
                "claim": claim_text,
                "claim_type": field,
                "aspect": aspects.get(
                    field,
                    field,
                ),
                "claim_index": index,
                "support_status": status,
                "evidence_chain": supported[:max_items],
            })

    if isinstance(summary, dict):
        summary["evidence_claims"] = claims

    return summary
