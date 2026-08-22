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
QUERY_STOPWORDS = STOPWORDS | {
    "问题", "哪些", "什么", "为何", "为什么", "如何", "是否", "怎么", "怎样",
    "请问", "请分析", "一下", "可以", "能够", "需要", "有关", "这个", "该",
}
NEGATION_RE = re.compile(
    r"(?:未|无|没有|不能|无法|不可|并非|尚未|避免|缺乏|"
    r"不(?:是|能|可|会|应|得|具有|存在|支持|符合|包含|包括|采用|提供|实现|导致))"
)
ABSOLUTE_CUE_RE = re.compile(r"(?:完全|绝对|全部|必然|彻底|始终|永久|百分之百|100%)")
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


def _claim_terms(values):
    """Return question terms that can establish a substantive source match."""
    return {
        term for term in _terms(values)
        if len(term) >= 2 and term not in QUERY_STOPWORDS
    }


def _strong_terms(values):
    """Terms strong enough to establish a substantive claim match."""
    return {
        term for term in _claim_terms(values)
        if len(term) >= 3 or bool(re.fullmatch(r"[a-z0-9_.-]+", term))
    }


def _claim_numbers(text):
    return set(re.findall(
        r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|吨|元|天|次|例|倍|项|种)?",
        str(text or ""),
    ))


def compact_evidence(item, max_chars=520):
    """Copy an evidence item as a bounded excerpt without losing provenance."""
    result = {
        key: item.get(key)
        for key in (
            "evidence_id", "source_path", "page", "section", "label", "bbox",
            "parser", "source_sha256", "content_sha256", "score", "retrieval_score",
            "archive_source_path", "archive_member", "paragraph_index", "block_index",
            "char_start", "char_end", "parser_version",
        )
        if item.get(key) is not None
    }
    text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
    result["text"] = text[:max_chars] + ("..." if len(text) > max_chars else "")
    result["excerpt"] = len(text) > max_chars
    result.setdefault("evidence_role", "direct_source")
    result.setdefault("derived_from", [])
    return result


NAVIGATION_LABELS = {"title", "section_header", "heading"}
WEAK_NAVIGATION_RE = re.compile(r"^(?:第?[一二三四五六七八九十\d]+[章节部分、.]?\s*)?.{1,80}[?？]$")
FACTUAL_CUE_RE = re.compile(
    r"(?:是|为|具有|通过|采用|支持|提供|实现|包括|导致|提升|降低|减少|增加|能够|可以|用于|依赖|需[要须]|应当|必须|表明|显示|发现|证明|说明|源于|结果|优势|特点|机制|原因|影响|风险|限制|缺陷|许可|安全|成本|性能)"
)


def evidence_quality(item):
    """Classify whether a source unit can support an analytical answer.

    A title, heading, question, or a short keyword is useful for navigation but
    cannot serve as evidence.  This quality gate is intentionally shared by
    evidence-chain selection and interactive retrieval.
    """
    if not isinstance(item, dict):
        return {"eligible": False, "reason": "证据项格式无效", "score": -100}
    text = " ".join(str(item.get("text") or "").split())
    label = str(item.get("label") or "").lower()
    normalized = text.strip(" \t\r\n：:;；。.")
    # Short, page-addressable statements such as "风险评估结论" or
    # "港口关闭导致物流延误" can still be useful evidence when they carry a
    # factual/causal cue.  Reject bare labels, but do not reject every concise
    # sentence merely because a parser extracted one line from a page.
    if len(normalized) < 6:
        return {"eligible": False, "reason": "正文过短，无法构成可验证陈述", "score": -80}
    if len(normalized) < 24 and not FACTUAL_CUE_RE.search(normalized):
        return {"eligible": False, "reason": "正文过短且缺少事实或解释", "score": -80}
    if label in NAVIGATION_LABELS:
        return {"eligible": False, "reason": "标题或章节名仅用于定位，不作为支撑证据", "score": -75}
    if normalized.endswith(("?", "？")) or WEAK_NAVIGATION_RE.match(normalized):
        return {"eligible": False, "reason": "疑问句只提出问题，未提供回答依据", "score": -70}
    # Short phrases such as "开源软件的特点" often arrive as a paragraph label.
    if len(normalized) < 55 and not FACTUAL_CUE_RE.search(normalized):
        return {"eligible": False, "reason": "仅为主题短语，缺少事实或解释", "score": -55}

    sentence_count = len(re.findall(r"[。！？；.!?;]", normalized))
    factual = bool(FACTUAL_CUE_RE.search(normalized))
    numeric = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|吨|元|天|次|例|倍|项|种)", normalized))
    score = min(len(normalized), 360) / 18.0 + sentence_count * 2 + int(factual) * 8 + int(numeric) * 5
    return {
        "eligible": True,
        "reason": "包含可回查的{}。".format("量化事实" if numeric else "解释性或事实性陈述"),
        "score": round(score, 3),
        "factual": factual,
        "numeric": numeric,
    }


def _supporting_quote(text, terms, max_chars=280):
    """Quote the most claim-like sentence instead of echoing a heading."""
    text = " ".join(str(text or "").split())
    sentences = [part.strip(" ，,；;") for part in re.split(r"(?<=[。！？；.!?;])\s*", text) if part.strip()]
    if not sentences:
        return text[:max_chars]
    def score(sentence):
        lowered = sentence.lower()
        return (
            sum(1 for term in terms if term.lower() in lowered) * 8
            + int(bool(FACTUAL_CUE_RE.search(sentence))) * 6
            + int(bool(re.search(r"\d", sentence))) * 3
            + min(len(sentence), 220) / 80.0
        )
    quote = max(sentences, key=score)
    return quote[:max_chars] + ("..." if len(quote) > max_chars else "")


def _claim_match(item, topics, semantic_score=0.0, indirect_signals=None):
    """Explain whether an eligible excerpt actually supports this question.

    A document can be high-quality prose but still be irrelevant to a particular
    conclusion.  This second gate makes that distinction explicit and is kept
    separate from ``evidence_quality`` so callers can show a useful reason.
    """
    topic_text = " ".join(str(value or "") for value in (topics or []))
    if not topic_text:
        return {
            "supports_claim": True,
            "support_type": "代表性正文",
            "support_reason": "未指定问题，展示可回查的代表性正文。",
            "matched_terms": [],
        }
    text = " ".join(str(item.get("text") or "").split())
    lowered = text.lower()
    terms = _claim_terms([topic_text])
    matched = sorted(
        {term for term in terms if term.lower() in lowered},
        key=lambda value: (-len(value), value),
    )[:8]
    strong_matched = sorted(
        {term for term in _strong_terms([topic_text]) if term.lower() in lowered},
        key=lambda value: (-len(value), value),
    )[:8]
    factual_body = bool(FACTUAL_CUE_RE.search(text))
    indirect = sorted({
        signal for signal in (indirect_signals or set())
        if str(signal).lower() in lowered
    })[:6]
    if strong_matched or (len(matched) >= 2 and factual_body):
        return {
            "supports_claim": True,
            "support_type": "直接证据",
            "support_reason": "正文直接回应问题中的关键概念：{}。".format("、".join((strong_matched or matched)[:4])),
            "matched_terms": matched,
        }
    if indirect:
        return {
            "supports_claim": True,
            "support_type": "间接证据",
            "support_reason": "正文包含与问题对应的可核查信号：{}；需要结合上下文理解。".format("、".join(indirect[:4])),
            "matched_terms": [],
        }
    if float(semantic_score or 0.0) >= 0.62:
        return {
            "supports_claim": True,
            "support_type": "语义证据",
            "support_reason": "与问题语义高度相关，但未出现直接关键词，建议人工复核。",
            "matched_terms": [],
        }
    return {
        "supports_claim": False,
        "support_type": "不构成支撑",
        "support_reason": "未找到能直接或可靠间接回答该问题的依据。",
        "matched_terms": [],
    }


def verify_claim_evidence(claim, item, semantic_score=0.0, relevance_mode="lexical-fallback"):
    """Verify that one source unit actually supports one concrete claim."""
    quality = evidence_quality(item)
    if not quality.get("eligible"):
        return {
            "support_status": "insufficient",
            "support_score": 0.0,
            "support_reason": quality.get("reason") or "证据质量不合格",
        }
    claim_text = " ".join(str(claim or "").split())
    text = " ".join(str(item.get("text") or "").split())
    if not claim_text:
        return {
            "support_status": "insufficient",
            "support_score": 0.0,
            "support_reason": "结论为空，无法验证证据关系",
        }

    claim_numbers = _claim_numbers(claim_text)
    evidence_numbers = _claim_numbers(text)
    if claim_numbers and not claim_numbers.issubset(evidence_numbers):
        return {
            "support_status": "insufficient",
            "support_score": 0.0,
            "support_reason": "结论中的数字或量化单位未在原文证据中一致出现",
        }

    if ABSOLUTE_CUE_RE.search(claim_text) and not ABSOLUTE_CUE_RE.search(text):
        return {
            "support_status": "partially_supported",
            "support_score": 0.3,
            "support_reason": "结论使用了绝对化表述，但原文证据没有支持相同强度",
        }

    if bool(NEGATION_RE.search(claim_text)) != bool(NEGATION_RE.search(text)):
        return {
            "support_status": "partially_supported",
            "support_score": 0.35,
            "support_reason": "结论与证据的否定范围不一致，需要人工复核",
        }

    match = _claim_match(item, [claim_text], semantic_score=semantic_score)
    matched_terms = set(match.get("matched_terms") or [])
    strong_count = len(matched_terms & _strong_terms([claim_text]))
    semantic = float(semantic_score or 0.0)
    if match.get("support_type") == "直接证据" and (
        strong_count >= 1 or (len(matched_terms) >= 2 and quality.get("factual"))
    ):
        score = min(1.0, 0.65 + 0.08 * strong_count + (0.12 if quality.get("factual") else 0.0))
        return {
            "support_status": "supported",
            "support_score": round(score, 3),
            "support_reason": match.get("support_reason"),
            "support_relation": "direct",
        }
    if match.get("support_type") == "间接证据":
        return {
            "support_status": "partially_supported",
            "support_score": 0.55,
            "support_reason": match.get("support_reason"),
            "support_relation": "indirect",
        }
    if semantic >= 0.78:
        return {
            "support_status": "partially_supported",
            "support_score": round(min(0.75, semantic), 3),
            "support_reason": "语义高度相关但缺少关键术语字面支撑，需要人工复核",
            "support_relation": "semantic",
        }
    return {
        "support_status": "insufficient",
        "support_score": 0.0,
        "support_reason": "原文没有直接或可靠间接支撑该结论的内容",
    }


def select_evidence(items, topics=None, max_items=24, per_source=2, max_chars=520):
    """Select diverse, topic-aligned evidence from a larger local corpus."""
    candidates = []
    seen = set()
    terms = _claim_terms(topics or [])
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
        quality = evidence_quality(item)
        if not quality["eligible"]:
            continue
        # A section name provides location context, but must not manufacture a
        # question match when the body itself does not support the claim.
        item_terms = _claim_terms([text])
        relevance = len(terms.intersection(item_terms)) if terms else 0
        relevance += 2 if indirect_signals.intersection(item_terms) else 0
        if re.search(r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|吨|元|天|次|例)", text):
            relevance += 1
        # Page/section-bearing units are more useful for one-click source review.
        provenance = int(item.get("page") is not None) + int(bool(item.get("section")))
        label_bonus = 2 if item.get("label") in {"table", "paragraph", "text_chunk", "list_item", "structured_column"} else 0
        candidates.append((relevance * 10 + provenance * 2 + label_bonus + quality["score"], -index, item, relevance, provenance, label_bonus))

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
        claim_match = _claim_match(
            item,
            topics,
            semantic_score=semantic,
            indirect_signals=indirect_signals,
        )
        # For a named question/conclusion, do not pad an evidence chain with a
        # merely well-written but unrelated paragraph.
        if topics and not claim_match["supports_claim"]:
            continue
        combined = semantic * 80.0 + relevance * 10.0 + provenance * 2.0 + label_bonus
        rescored.append((combined, order, item, semantic, relevance_mode, claim_match))

    rescored.sort(key=lambda value: (-value[0], value[1]))
    selected = []
    source_counts = defaultdict(int)
    for score, order, item, semantic, mode, claim_match in rescored:
        source = item.get("source_path") or ""
        if source_counts[source] >= max(1, per_source):
            continue
        compact = compact_evidence(item, max_chars=max_chars)
        compact["supporting_quote"] = _supporting_quote(item.get("text"), terms, max_chars=min(280, max_chars))
        compact["evidence_quality"] = evidence_quality(item)
        compact.update(claim_match)
        compact.update(
            verify_claim_evidence(
                topic_text,
                item,
                semantic_score=semantic,
                relevance_mode=mode,
            ) if topics else {
                "support_status": "supported",
                "support_score": 0.5,
                "support_reason": "未指定具体结论，仅作为可回查正文",
            }
        )
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
                item for item in selected
                if item.get("support_status") == "supported"
            ]
            partial = [
                item for item in selected
                if item.get("support_status") == "partially_supported"
            ]

            if any(
                item.get("matched_terms")
                for item in supported
            ):
                status = "direct"

            elif partial:
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
                "evidence_chain": (supported or partial)[:max_items],
            })

    if isinstance(summary, dict):
        summary["evidence_claims"] = claims

    return summary
