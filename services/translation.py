"""Recoverable, local-first document translation.

The module deliberately owns no database tables and performs no network calls
other than through an explicitly supplied provider. Translation state is a
plain JSON-compatible dictionary so a worker can checkpoint it periodically
and resume it after a restart. Checkpoints are deliberately bounded: rebuilding
a complete state after every unit makes a long document quadratic in the
number of translation units.

Foreign text is never presented as if it had been translated: a unit has
``target_text`` only after the provider output has passed the protection and
quality contracts.  Chinese and language-neutral units are copied with the
explicit ``not_required`` status.
"""
from __future__ import unicode_literals

import copy
import hashlib
import json
import re
import time
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

from services.ollama import LocalModelError


TRANSLATION_SCHEMA_VERSION = "document-translation/1.0"
# Bump this whenever a quality decision changes.  Translation-memory keys
# include the contract version, so records rejected only by an older policy
# cannot keep poisoning a resumed full-document translation.
TRANSLATION_CONTRACT_VERSION = "zh-translation/1.3"
TARGET_LANGUAGE = "zh-CN"

# Stable terminology used by the local translation model when callers do not
# provide a project-specific glossary. Keep source acronyms in every rendering
# so evidence, code and cross-document search remain interoperable.
DEFAULT_TECHNICAL_GLOSSARY = {
    "TEE": "可信执行环境（TEE）",
    "RTPM": "RTPM",
    "COSE": "CBOR 对象签名与加密（COSE）",
    "CBOR": "简明二进制对象表示法（CBOR）",
    "MAC": "消息认证码（MAC）",
    "EAT": "实体证明令牌（EAT）",
    "TPM": "可信平台模块（TPM）",
    "SEV-SNP": "SEV-SNP",
    "RISC-V": "RISC-V",
}

_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")
_HEBREW_RE = re.compile(r"[\u0590-\u05ff]")
_GREEK_RE = re.compile(r"[\u0370-\u03ff]")
_LATIN_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")
_WORD_RE = re.compile(r"[A-Za-z\u00c0-\u024f]+", re.UNICODE)
_TOKEN_RE = re.compile(r"__SJFX_(?:TERM|KEEP)_\d{4}__")

_LANGUAGE_NAMES = {
    "zh": "中文", "en": "英语", "fr": "法语", "de": "德语",
    "es": "西班牙语", "it": "意大利语", "pt": "葡萄牙语",
    "ru": "俄语", "ja": "日语", "ko": "韩语", "ar": "阿拉伯语",
    "nl": "荷兰语", "tr": "土耳其语", "pl": "波兰语",
    "th": "泰语", "hi": "印地语", "he": "希伯来语", "el": "希腊语",
    "other": "其他外语", "mixed": "混合语言", "und": "未确定语言",
}

_LATIN_STOPWORDS = {
    "en": {"the", "and", "of", "to", "in", "is", "for", "that", "with", "on", "from", "this"},
    "fr": {"le", "la", "les", "de", "des", "et", "est", "pour", "dans", "une", "un", "que"},
    "de": {"der", "die", "das", "und", "ist", "von", "zu", "mit", "den", "für", "ein", "eine"},
    "es": {"el", "la", "los", "las", "de", "y", "es", "para", "en", "una", "un", "que"},
    "it": {"il", "lo", "la", "gli", "le", "di", "e", "è", "per", "in", "una", "che"},
    "pt": {"o", "a", "os", "as", "de", "e", "é", "para", "em", "uma", "um", "que"},
    "nl": {"de", "het", "een", "en", "van", "is", "voor", "met", "dat", "in"},
    "tr": {"ve", "bir", "bu", "için", "ile", "olan", "de", "da", "olarak"},
    "pl": {"i", "w", "z", "na", "jest", "dla", "nie", "to", "że", "oraz"},
}

_KNOWN_ACRONYMS = {
    "AI", "API", "CPU", "GPU", "PDF", "OCR", "LLM", "RAG", "SQL",
    "CSV", "JSON", "XML", "HTML", "HTTP", "HTTPS", "URL", "URI",
    "UUID", "SHA", "NATO", "UN", "EU", "USA", "UK", "CEO", "CFO",
    "DOI", "ISBN", "IP", "DNS", "TCP", "UDP", "TLS", "VPN",
}
_NON_ENTITY_TITLE_WORDS = {
    "A", "AN", "THE", "THIS", "THAT", "THESE", "THOSE", "REPORT",
    "AGREEMENT", "CONTRACT", "LETTER", "SUMMARY", "INTRODUCTION",
    "BACKGROUND", "RESULT", "RESULTS", "CONCLUSION", "PROJECT", "MEETING",
    "MINUTES", "QUARTERLY", "ANNUAL", "CONFIDENTIAL", "IMPORTANT",
    "APPENDIX", "CHAPTER", "TABLE", "FIGURE", "SECTION", "REVENUE",
}


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def document_translation_fingerprint(document):
    """Fingerprint every source field that makes a stored translation valid."""
    if isinstance(document, str):
        document = {"text": document}
    document = document or {}
    structure = document.get("structure") or {}
    source = document.get("source") or {}
    original_title = str(structure.get("title") or source.get("name") or "")
    original_text = str(document.get("text") or "")
    return _sha256_text(json.dumps({
        "source_sha256": source.get("sha256"),
        "content_sha256": str(document.get("content_sha256") or ""),
        "title": original_title,
        "text_sha256": _sha256_text(original_text),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _other_alpha_count(value):
    """Count alphabetic scripts outside the explicitly named ranges."""
    text = str(value or "")
    known = (
        len(_HAN_RE.findall(text)) + len(_KANA_RE.findall(text))
        + len(_HANGUL_RE.findall(text)) + len(_CYRILLIC_RE.findall(text))
        + len(_ARABIC_RE.findall(text)) + len(_THAI_RE.findall(text))
        + len(_DEVANAGARI_RE.findall(text)) + len(_HEBREW_RE.findall(text))
        + len(_GREEK_RE.findall(text)) + len(_LATIN_RE.findall(text))
    )
    return max(0, sum(1 for char in text if char.isalpha()) - known)


def _normalise_glossary(glossary):
    """Return a deterministic source-to-Chinese glossary dictionary."""
    if glossary is None:
        return dict(DEFAULT_TECHNICAL_GLOSSARY)
    if isinstance(glossary, dict):
        items = glossary.items()
    elif isinstance(glossary, (list, tuple)):
        items = []
        for item in glossary:
            if not isinstance(item, dict):
                raise ValueError("术语表列表中的每一项都必须是对象")
            items.append((item.get("source"), item.get("target")))
    else:
        raise ValueError("术语表必须是 source: target 对象或术语项列表")
    result = {}
    for source, target in items:
        source = str(source or "").strip()
        target = str(target or "").strip()
        if not source or not target:
            raise ValueError("术语表的源词和目标词都不能为空")
        result[source] = target
    merged = dict(DEFAULT_TECHNICAL_GLOSSARY)
    merged.update(result)
    return merged


def glossary_fingerprint(glossary):
    normalised = _normalise_glossary(glossary)
    payload = json.dumps(normalised, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(payload)


def detect_language(text):
    """Detect common document languages without an optional third-party model.

    This detector is intentionally conservative.  It is sufficient for routing
    a unit to translation; a production deployment may replace it with a local
    fastText/CLD model while preserving the returned contract.
    """
    value = str(text or "")
    counts = {
        "han": len(_HAN_RE.findall(value)),
        "kana": len(_KANA_RE.findall(value)),
        "hangul": len(_HANGUL_RE.findall(value)),
        "cyrillic": len(_CYRILLIC_RE.findall(value)),
        "arabic": len(_ARABIC_RE.findall(value)),
        "thai": len(_THAI_RE.findall(value)),
        "devanagari": len(_DEVANAGARI_RE.findall(value)),
        "hebrew": len(_HEBREW_RE.findall(value)),
        "greek": len(_GREEK_RE.findall(value)),
        "latin": len(_LATIN_RE.findall(value)),
        "other": _other_alpha_count(value),
    }
    letters = sum(counts.values())
    if not letters:
        return {
            "language": "und", "language_name": _LANGUAGE_NAMES["und"],
            "confidence": 0.0, "needs_translation": False,
            "foreign_ratio": 0.0, "script_counts": counts,
        }

    language = "und"
    confidence = 0.5
    if counts["kana"]:
        language = "ja"
        confidence = min(0.99, 0.72 + counts["kana"] / float(letters or 1) * 0.27)
    elif counts["hangul"]:
        language = "ko"
        confidence = min(0.99, 0.72 + counts["hangul"] / float(letters or 1) * 0.27)
    elif counts["cyrillic"] == max(counts.values()) and counts["cyrillic"]:
        language = "ru"
        confidence = min(0.99, 0.75 + counts["cyrillic"] / float(letters) * 0.24)
    elif counts["arabic"] == max(counts.values()) and counts["arabic"]:
        language = "ar"
        confidence = min(0.99, 0.75 + counts["arabic"] / float(letters) * 0.24)
    elif counts["thai"] == max(counts.values()) and counts["thai"]:
        language = "th"
        confidence = min(0.99, 0.75 + counts["thai"] / float(letters) * 0.24)
    elif counts["devanagari"] == max(counts.values()) and counts["devanagari"]:
        language = "hi"
        confidence = min(0.99, 0.75 + counts["devanagari"] / float(letters) * 0.24)
    elif counts["hebrew"] == max(counts.values()) and counts["hebrew"]:
        language = "he"
        confidence = min(0.99, 0.75 + counts["hebrew"] / float(letters) * 0.24)
    elif counts["greek"] == max(counts.values()) and counts["greek"]:
        language = "el"
        confidence = min(0.99, 0.75 + counts["greek"] / float(letters) * 0.24)
    elif counts["han"]:
        foreign_letters = letters - counts["han"]
        foreign_ratio = foreign_letters / float(letters)
        # Incidental acronyms in Chinese prose do not make the whole unit
        # foreign. A substantial foreign passage does need translation.
        if foreign_letters >= 4 and foreign_ratio >= 0.30:
            language = "mixed"
            confidence = min(0.95, 0.65 + abs(0.5 - foreign_ratio) * 0.4)
        else:
            language = "zh"
            confidence = min(0.98, 0.75 + counts["han"] / float(letters) * 0.23)
    elif counts["latin"]:
        words = [word.lower() for word in _WORD_RE.findall(value)]
        scores = {
            code: sum(1 for word in words if word in stopwords)
            for code, stopwords in _LATIN_STOPWORDS.items()
        }
        best = max(sorted(scores), key=lambda code: scores[code]) if scores else "en"
        language = best if scores.get(best, 0) else "en"
        evidence = scores.get(language, 0)
        confidence = min(0.97, 0.62 + evidence / float(max(3, len(words))) * 1.4)
    elif counts["other"]:
        # Thai, Indic, Hebrew, Greek and other scripts are still routed to the
        # multilingual provider even when this zero-dependency fallback cannot
        # name the exact language.
        language = "other"
        confidence = min(0.90, 0.58 + counts["other"] / float(letters) * 0.30)

    chinese_ratio = counts["han"] / float(letters or 1)
    foreign_ratio = max(0.0, min(1.0, 1.0 - chinese_ratio))
    return {
        "language": language,
        "language_name": _LANGUAGE_NAMES.get(language, language),
        "confidence": round(confidence, 4),
        "needs_translation": language not in {"zh", "und"},
        "foreign_ratio": round(foreign_ratio, 4),
        "script_counts": counts,
    }


def segment_text(text, max_chars=2400):
    """Split text losslessly into bounded, translation-friendly units."""
    value = str(text or "")
    max_chars = max(128, int(max_chars or 2400))
    if not value:
        return []
    segments = []
    boundary_re = re.compile(r"(?:\r?\n\s*\r?\n|\r?\n|[。！？!?；;]\s*|\.\s+)")
    whitespace_re = re.compile(r"\s+")

    # Paragraph separators are semantic structure and remain hard boundaries
    # even when the whole document fits in one model request. Include the
    # separator in the preceding unit so concatenation is byte-for-byte stable.
    paragraph_ends = [match.end() for match in re.finditer(r"\r?\n[ \t]*\r?\n", value)]
    paragraph_ends.append(len(value))
    piece_start = 0
    for piece_end in paragraph_ends:
        cursor = piece_start
        while cursor < piece_end:
            hard_end = min(piece_end, cursor + max_chars)
            if hard_end == piece_end:
                end = piece_end
            else:
                window = value[cursor:hard_end]
                minimum = max(1, int(max_chars * 0.45))
                choices = [match.end() for match in boundary_re.finditer(window) if match.end() >= minimum]
                if not choices:
                    choices = [match.end() for match in whitespace_re.finditer(window) if match.end() >= minimum]
                end = cursor + (choices[-1] if choices else len(window))
            if end <= cursor:
                end = min(piece_end, cursor + max_chars)
            segments.append({"start": cursor, "end": end, "text": value[cursor:end]})
            cursor = end
        piece_start = piece_end
    return segments


def _script_class(char):
    """Return a routable script family for mixed-language splitting."""
    if _HAN_RE.match(char):
        return "han"
    if _KANA_RE.match(char):
        return "ja"
    if _HANGUL_RE.match(char):
        return "ko"
    if _CYRILLIC_RE.match(char):
        return "ru"
    if _ARABIC_RE.match(char):
        return "ar"
    if _THAI_RE.match(char):
        return "th"
    if _DEVANAGARI_RE.match(char):
        return "hi"
    if _HEBREW_RE.match(char):
        return "he"
    if _GREEK_RE.match(char):
        return "el"
    if _LATIN_RE.match(char):
        return "latin"
    return None


def split_mixed_text(text):
    """Split a mixed-script unit while retaining exact source offsets."""
    value = str(text or "")
    if not value:
        return []
    pieces = []
    start = 0
    active = None
    for index, char in enumerate(value):
        script = _script_class(char)
        if script is None:
            continue
        if active is None:
            active = script
            continue
        if script == active:
            continue
        # Keep separators with the preceding run; this makes concatenation
        # byte-for-byte stable and lets the service restore whitespace.
        end = index
        if end > start:
            pieces.append({"start": start, "end": end, "text": value[start:end]})
        start = index
        active = script
    if start < len(value):
        pieces.append({"start": start, "end": len(value), "text": value[start:]})
    return pieces or [{"start": 0, "end": len(value), "text": value}]


def split_table_text(text):
    """Split table cells from layout separators while preserving offsets."""
    value = str(text or "")
    if not value:
        return []
    pieces = []
    for match in re.finditer(r"[^\t|\r\n]+|[\t|\r\n]+", value):
        pieces.append({"start": match.start(), "end": match.end(), "text": match.group(0)})
    return pieces


@dataclass
class ProtectedText:
    text: str
    replacements: dict
    kinds: dict
    glossary_targets: list

    def restore(self, translated):
        output = str(translated or "")
        missing = [token for token in self.replacements if output.count(token) != 1]
        if missing:
            raise TranslationQualityError(
                "模型未完整保留保护标记：{}".format(", ".join(missing[:8])),
                code="protected_token_mismatch",
            )
        for token, replacement in self.replacements.items():
            output = output.replace(token, replacement)
        return output


def _candidate_spans(text, glossary):
    candidates = []
    # Lower priority number wins when spans overlap. Explicit glossary terms
    # therefore override automatic named-entity preservation.
    for source, target in sorted(glossary.items(), key=lambda item: (-len(item[0]), item[0])):
        flags = re.IGNORECASE if _LATIN_RE.search(source) else 0
        pattern = re.escape(source)
        if source[0].isalnum() and source[-1].isalnum():
            if source[0].isascii() and source[-1].isascii():
                # Chinese characters next to a Latin term are valid term
                # boundaries (for example ``使用AI模型``).
                pattern = r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])".format(pattern)
            else:
                pattern = r"(?<!\w){}(?!\w)".format(pattern)
        for match in re.finditer(pattern, text, flags):
            candidates.append((match.start(), match.end(), 0, "term", target))

    patterns = [
        (r"https?://[^\s<>\]\[()]+", "url"),
        (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "email"),
        (r"\{\{[^{}\r\n]+\}\}|\$\{[^{}\r\n]+\}|%\([^)]+\)[#0 +\-]?[0-9.]*[a-zA-Z]|%[sdif]|\{[A-Za-z_][A-Za-z0-9_.-]*\}", "placeholder"),
        (r"\b(?:\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[,.]?\s+\d{4}|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4})\b", "date"),
        (r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\b", "date"),
        (r"(?<![\w])(?:[$€£¥￥]\s?\d+(?:[,.]\d+)*|\d+(?:[,.]\d+)*\s?(?:USD|CNY|RMB|EUR|GBP|JPY|美元|人民币|元))(?![\w])", "amount"),
        (r"(?<![\w])[-+]?\d+(?:[,.]\d+)*(?:%|[A-Za-z]{1,5})?(?![\w])", "number"),
        (r"\b[A-Z][A-Z0-9&.-]{1,}\b", "acronym"),
        (r"\b[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+\b", "proper_name"),
        (r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,3}\b", "proper_name"),
    ]
    for pattern, kind in patterns:
        for match in re.finditer(pattern, text, re.UNICODE):
            matched = match.group(0)
            if kind == "acronym":
                compact = matched.replace(".", "").replace("-", "")
                has_marker = any(char.isdigit() or char in ".-&" for char in matched)
                if compact not in _KNOWN_ACRONYMS and not has_marker:
                    continue
            if kind == "proper_name" and " " in matched:
                words = {word.upper() for word in matched.split()}
                if words & _NON_ENTITY_TITLE_WORDS:
                    continue
            candidates.append((match.start(), match.end(), 10, kind, match.group(0)))
    return candidates


def protect_text(text, glossary=None):
    """Replace fragile spans with deterministic tokens before model use."""
    value = str(text or "")
    terms = _normalise_glossary(glossary)
    accepted = []
    occupied = []
    for candidate in sorted(_candidate_spans(value, terms), key=lambda item: (item[2], item[0], -(item[1] - item[0]))):
        start, end = candidate[0], candidate[1]
        if start == end or any(start < other_end and end > other_start for other_start, other_end in occupied):
            continue
        accepted.append(candidate)
        occupied.append((start, end))
    accepted.sort(key=lambda item: item[0])

    parts = []
    replacements = {}
    kinds = {}
    targets = []
    cursor = 0
    term_index = 0
    keep_index = 0
    for start, end, _priority, kind, replacement in accepted:
        parts.append(value[cursor:start])
        if kind == "term":
            term_index += 1
            token = "__SJFX_TERM_{:04d}__".format(term_index)
            targets.append(replacement)
        else:
            keep_index += 1
            token = "__SJFX_KEEP_{:04d}__".format(keep_index)
        parts.append(token)
        replacements[token] = replacement
        kinds[token] = kind
        cursor = end
    parts.append(value[cursor:])
    return ProtectedText("".join(parts), replacements, kinds, targets)


class TranslationError(RuntimeError):
    """Base error for a translation unit."""


class TranslationProviderError(TranslationError):
    def __init__(self, message, retryable=True, code="provider_error"):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.code = code


class TranslationQualityError(TranslationError):
    def __init__(self, message, code="quality_error"):
        super().__init__(message)
        self.retryable = True
        self.code = code


@dataclass
class ProviderResponse:
    text: str
    model: str = ""
    usage: dict = None
    metadata: dict = None


class TranslationProvider(object, metaclass=ABCMeta):
    """Provider contract; implementations must return an actual translation."""

    @property
    @abstractmethod
    def provider_id(self):
        raise NotImplementedError

    @abstractmethod
    def translate(self, text, source_language, target_language, glossary=None,
                  timeout=None, retries=0):
        raise NotImplementedError


class UnavailableTranslationProvider(TranslationProvider):
    """Explicit failure provider used when no local model is configured."""

    def __init__(self, reason="未配置可用的本地翻译模型"):
        self.reason = str(reason)

    @property
    def provider_id(self):
        return "unavailable"

    def translate(self, text, source_language, target_language, glossary=None,
                  timeout=None, retries=0):
        raise TranslationProviderError(self.reason, retryable=False, code="provider_unavailable")


class OllamaTranslationProvider(TranslationProvider):
    """Adapter for the repository's local-only :class:`OllamaClient`."""

    def __init__(self, client):
        self.client = client

    @property
    def provider_id(self):
        return "ollama:{}".format(getattr(self.client, "model", "local-model") or "local-model")

    def translate(self, text, source_language, target_language=TARGET_LANGUAGE,
                  glossary=None, timeout=None, retries=0):
        if not getattr(self.client, "configured", False):
            raise TranslationProviderError(
                "本地 Ollama 翻译模型未配置", retryable=False, code="provider_unavailable"
            )
        system_prompt = (
            "你是严谨的专业文档翻译引擎。将输入完整翻译成简体中文，保持事实、语气、段落和项目层级；"
            "不得总结、扩写、删减或解释。形如 __SJFX_KEEP_0001__ 和 __SJFX_TERM_0001__ 的保护标记"
            "必须原样、各保留一次，不能改写或移动到不对应的位置。输入中已有的中文保留自然表达。"
            "只输出 {\"translation\":\"完整译文\"}，translation 必须是唯一的 JSON 顶层字段。"
        )
        user_prompt = json.dumps({
            "source_language": source_language,
            "target_language": target_language,
            "text": str(text or ""),
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            result = self.client.chat_json(
                system_prompt, user_prompt,
                max_tokens=max(1024, min(6000, int(len(str(text or "")) * 1.8) + 256)),
                retries=retries, timeout=timeout,
                required_fields=["translation"], output_context="翻译模型输出",
            )
        except LocalModelError as exc:
            raise TranslationProviderError(str(exc), retryable=True, code="ollama_error") from exc
        payload = result.get("json") or {}
        translation = payload.get("translation")
        if not isinstance(translation, str) or not translation.strip():
            raise TranslationProviderError("本地模型未返回非空 translation 字段", retryable=True)
        return ProviderResponse(
            text=translation,
            model=str(result.get("model") or getattr(self.client, "model", "")),
            usage=dict(result.get("usage") or {}),
            metadata={"finish_reason": result.get("finish_reason")},
        )

    def review(self, text, draft, source_language, target_language=TARGET_LANGUAGE,
               glossary=None, qa_errors=None, timeout=None):
        """Correct a draft while retaining the same protected-token contract."""
        if not getattr(self.client, "configured", False):
            raise TranslationProviderError(
                "本地翻译复核模型未配置", retryable=False, code="reviewer_unavailable"
            )
        system_prompt = (
            "你是专业译文复核模型。对照原文修正候选简体中文译文中的遗漏、误译、未翻译内容和结构问题；"
            "不得总结、扩写或删减。所有 __SJFX_KEEP_0001__、__SJFX_TERM_0001__ 形式的保护标记必须原样各保留一次，"
            "数字、日期、金额、术语、换行、制表符和表格竖线结构必须保持；术语表中的"
            "目标译法必须逐字采用，代码、标识符、URL 和路径不得翻译。"
            "只输出 {\"translation\":\"修正后的完整译文\"}，translation 必须是唯一的 JSON 顶层字段。"
        )
        user_prompt = json.dumps({
            "source_language": source_language,
            "target_language": target_language,
            "source": str(text or ""),
            "draft": str(draft or ""),
            "quality_errors": list(qa_errors or []),
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            result = self.client.chat_json(
                system_prompt, user_prompt,
                max_tokens=max(1024, min(6000, int(len(str(text or "")) * 1.8) + 256)),
                retries=0, timeout=timeout,
                required_fields=["translation"], output_context="翻译复核模型输出",
            )
        except LocalModelError as exc:
            raise TranslationProviderError(str(exc), retryable=True, code="reviewer_error") from exc
        translation = (result.get("json") or {}).get("translation")
        if not isinstance(translation, str) or not translation.strip():
            raise TranslationProviderError("本地复核模型未返回有效译文", code="reviewer_empty")
        return ProviderResponse(
            text=translation,
            model=str(result.get("model") or getattr(self.client, "model", "")),
            usage=dict(result.get("usage") or {}),
            metadata={"finish_reason": result.get("finish_reason"), "reviewed": True},
        )


class TranslationMemory(object, metaclass=ABCMeta):
    @abstractmethod
    def get(self, key):
        raise NotImplementedError

    @abstractmethod
    def put(self, key, value):
        raise NotImplementedError


class InMemoryTranslationMemory(TranslationMemory):
    """Thread-safe persistence adapter is supplied by storage at integration.

    This in-memory implementation keeps tests and one-process installations
    deterministic. Returned records are copied so callers cannot mutate cache
    entries after quality approval.
    """

    def __init__(self, initial=None):
        import threading
        self._records = copy.deepcopy(initial or {})
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            value = self._records.get(str(key))
            return copy.deepcopy(value) if value is not None else None

    def put(self, key, value):
        with self._lock:
            self._records[str(key)] = copy.deepcopy(value)


class StorageTranslationMemory(TranslationMemory):
    """Durable adapter for :class:`services.storage.Storage`."""

    def __init__(self, storage):
        self.storage = storage

    def get(self, key):
        return self.storage.get_translation_memory(str(key))

    def put(self, key, value):
        self.storage.save_translation_memory(str(key), value)


@dataclass
class TranslationPolicy:
    max_unit_chars: int = 4800
    max_attempts: int = 2
    timeout_seconds: int = 180
    contract_version: str = TRANSLATION_CONTRACT_VERSION
    coalesce_paragraphs: bool = True
    review_complex_units: bool = False
    checkpoint_unit_interval: int = 16
    checkpoint_interval_seconds: float = 30.0
    max_intermediate_checkpoints: int = 32

    def __post_init__(self):
        self.max_unit_chars = max(128, min(12000, int(self.max_unit_chars or 4800)))
        self.max_attempts = max(1, min(6, int(self.max_attempts or 2)))
        self.timeout_seconds = max(10, min(1800, int(self.timeout_seconds or 180)))
        self.contract_version = str(self.contract_version or TRANSLATION_CONTRACT_VERSION)
        self.coalesce_paragraphs = bool(self.coalesce_paragraphs)
        self.review_complex_units = bool(self.review_complex_units)
        self.checkpoint_unit_interval = max(
            1, min(10000, int(self.checkpoint_unit_interval or 16))
        )
        self.checkpoint_interval_seconds = max(
            1.0, min(3600.0, float(self.checkpoint_interval_seconds or 30.0))
        )
        self.max_intermediate_checkpoints = max(
            1, min(256, int(self.max_intermediate_checkpoints or 32))
        )


def translation_memory_key(source_text, source_language, glossary=None,
                           target_language=TARGET_LANGUAGE,
                           contract_version=TRANSLATION_CONTRACT_VERSION):
    payload = {
        "contract": str(contract_version),
        "source_language": str(source_language or "und"),
        "target_language": str(target_language),
        "source_text": str(source_text or ""),
        "glossary": _normalise_glossary(glossary),
    }
    serialised = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "tm:" + _sha256_text(serialised)


def _normalise_provider_response(value):
    if isinstance(value, ProviderResponse):
        return value
    if isinstance(value, str):
        return ProviderResponse(value)
    if isinstance(value, dict):
        text = value.get("text") if "text" in value else value.get("translation")
        return ProviderResponse(
            str(text or ""), str(value.get("model") or ""),
            dict(value.get("usage") or {}), dict(value.get("metadata") or {}),
        )
    raise TranslationProviderError("翻译提供者返回了不支持的数据类型", retryable=False)


def validate_translation(source, protected, provider_output, restored,
                         source_language, glossary=None, strict_layout=False):
    """Return a machine-readable QA decision for one translated unit."""
    errors = []
    warnings = []
    raw_output = str(provider_output or "")
    restored = str(restored or "")
    if not raw_output.strip():
        errors.append("empty_translation")
    for token in protected.replacements:
        if raw_output.count(token) != 1:
            errors.append("protected_token_mismatch")
            break
    unknown = [token for token in _TOKEN_RE.findall(raw_output) if token not in protected.replacements]
    if unknown:
        errors.append("unknown_protected_token")

    for target in _normalise_glossary(glossary).values():
        if target not in restored:
            # Only require a term if one of its source forms occurred in this
            # unit; ``glossary_targets`` is the precise occurrence list.
            if target in protected.glossary_targets:
                errors.append("glossary_term_missing")
                break

    translatable = _TOKEN_RE.sub("", protected.text)
    translatable_letters = (
        len(_LATIN_RE.findall(translatable)) + len(_CYRILLIC_RE.findall(translatable))
        + len(_ARABIC_RE.findall(translatable)) + len(_KANA_RE.findall(translatable))
        + len(_HANGUL_RE.findall(translatable)) + _other_alpha_count(translatable)
    )
    han_count = len(_HAN_RE.findall(restored))
    if translatable_letters >= 8 and not han_count:
        errors.append("target_missing_chinese")

    raw_without_tokens = _TOKEN_RE.sub("", raw_output)
    target_foreign_letters = (
        len(_LATIN_RE.findall(raw_without_tokens)) + len(_CYRILLIC_RE.findall(raw_without_tokens))
        + len(_ARABIC_RE.findall(raw_without_tokens)) + len(_KANA_RE.findall(raw_without_tokens))
        + len(_HANGUL_RE.findall(raw_without_tokens)) + _other_alpha_count(raw_without_tokens)
    )
    if (
        translatable_letters >= 8 and target_foreign_letters >= 6
        and target_foreign_letters / float(target_foreign_letters + han_count or 1) > 0.45
    ):
        errors.append("target_contains_untranslated_text")

    plain_source = re.sub(r"\s+", "", str(source or "")).casefold()
    plain_target = re.sub(r"\s+", "", restored).casefold()
    if translatable_letters >= 6 and plain_source == plain_target:
        errors.append("source_copied_without_translation")

    source_length = max(1, len(str(source or "").strip()))
    ratio = len(restored.strip()) / float(source_length)
    if ratio < 0.08 or ratio > 8.0:
        errors.append("implausible_length_ratio")
    elif ratio < 0.18 or ratio > 5.0:
        warnings.append("unusual_length_ratio")
    if source_language == "mixed" and han_count:
        warnings.append("mixed_language_source")

    # OCR and PDF text extraction frequently add visual line wraps that a
    # translation engine correctly normalizes.  A changed line count is useful
    # review information but never proves content loss, including in a table:
    # actual tab and pipe counts below are the hard table-layout contract.
    # ``strict_layout`` remains accepted for API compatibility with earlier
    # callers, but must not turn a harmless wrap into a failed translation.
    structural_counts = {
        "line_breaks": str(source or "").count("\n"),
        "tabs": str(source or "").count("\t"),
        "table_pipes": str(source or "").count("|"),
    }
    restored_counts = {
        "line_breaks": restored.count("\n"),
        "tabs": restored.count("\t"),
        "table_pipes": restored.count("|"),
    }
    if structural_counts["line_breaks"] != restored_counts["line_breaks"]:
        warnings.append("line_structure_changed")
    if structural_counts["tabs"] and structural_counts["tabs"] != restored_counts["tabs"]:
        errors.append("table_structure_changed")
    if structural_counts["table_pipes"] >= 2 and structural_counts["table_pipes"] != restored_counts["table_pipes"]:
        errors.append("table_structure_changed")

    errors = sorted(set(errors))
    warnings = sorted(set(warnings))
    score = max(0.0, 1.0 - len(errors) * 0.35 - len(warnings) * 0.08)
    return {
        "passed": not errors,
        "score": round(score, 4),
        "errors": errors,
        "warnings": warnings,
        "length_ratio": round(ratio, 4),
        "protected_items": len(protected.replacements),
        "glossary_occurrences": len(protected.glossary_targets),
        "source_structure": structural_counts,
        "target_structure": restored_counts,
    }


def _unit_id(kind, start, end, source_text):
    digest = _sha256_text("{}\0{}\0{}\0{}".format(kind, start, end, source_text))[:16]
    return "TU-{}".format(digest)


def _heading_texts(structure):
    values = []
    for item in (structure or {}).get("headings") or []:
        if isinstance(item, dict):
            value = item.get("text") or item.get("title") or item.get("name")
        else:
            value = item
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if value and value not in values:
            values.append(value)
    return values


def _is_reference_metadata(text):
    """Recognise citation-heavy bibliographies that must retain source form.

    References, identifiers, volume/page numbers and URLs are evidence metadata
    rather than prose. Machine-translating them frequently damages traceability,
    while forcing Chinese-only quality rules rejects legitimate preserved
    citations. The test is deliberately strict so ordinary numbered prose is
    still translated.
    """
    value = str(text or "")
    lines = [line for line in value.splitlines() if line.strip()]
    if len(lines) < 6:
        return False
    compact_length = max(1, len(re.sub(r"\s+", "", value)))
    digits = len(re.findall(r"\d", value))
    links = len(re.findall(r"(?:https?://|www\.|\bdoi\s*[:/])", value, re.I))
    citation_lines = sum(
        1 for line in lines
        if re.search(
            r"(?:\b(?:19|20)\d{2}\b|\b(?:vol\.?|no\.?|pp\.?|isbn|issn|doi)\b|"
            r"^\s*(?:\[\d+\]|\d+[.)]))",
            line, re.I,
        )
    )
    return (
        citation_lines >= 3
        and digits / float(compact_length) >= 0.025
        and (links >= 1 or citation_lines >= max(5, len(lines) // 5))
    )


def _block_kind(text, heading_values, evidence_markers):
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if compact in heading_values:
        return "heading"
    if _is_reference_metadata(text):
        return "reference"
    evidence_labels = [
        label for label, marker in evidence_markers
        if marker and (marker[:160] in compact or compact[:160] in marker)
    ]
    lowered_labels = " ".join(evidence_labels).lower()
    if (
        "footnote" in lowered_labels
        or "脚注" in lowered_labels
        or re.match(r"^\s*(?:\[\d+\]|\d+[.)、]|[*†‡])\s*", text)
    ):
        return "footnote"
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if (
        "table" in lowered_labels or "表格" in lowered_labels
        or sum(line.count("\t") for line in lines) >= max(2, len(lines))
        or sum(line.count("|") for line in lines) >= max(4, len(lines) * 2)
    ):
        return "table"
    return "paragraph"


def build_translation_units(document, max_unit_chars=2400, glossary=None,
                            target_language=TARGET_LANGUAGE,
                            contract_version=TRANSLATION_CONTRACT_VERSION):
    """Create deterministic title/body units from a unified document."""
    if isinstance(document, str):
        document = {"text": document}
    document = document or {}
    structure = document.get("structure") or {}
    source = document.get("source") or {}
    title = str(structure.get("title") or source.get("name") or "")
    body = str(document.get("text") or "")
    heading_values = _heading_texts(structure)
    evidence_markers = [
        (
            str(item.get("label") or item.get("kind") or ""),
            re.sub(r"\s+", " ", str(item.get("text") or "")).strip(),
        )
        for item in document.get("evidence") or [] if isinstance(item, dict)
    ]
    heading_positions = []
    for heading in heading_values:
        start = body.find(heading)
        if start >= 0:
            heading_positions.append((start, heading))
    heading_positions.sort()
    units = []
    for kind, value in (("title", title), ("body", body)):
        paragraph_index = 0
        for item in segment_text(value, max_chars=max_unit_chars):
            if kind == "title":
                block_kind = "title"
                section = title or None
                unit_paragraph_index = None
            else:
                paragraph_index += 1
                block_kind = _block_kind(item["text"], heading_values, evidence_markers)
                section = next(
                    (heading for position, heading in reversed(heading_positions) if position <= item["start"]),
                    None,
                )
                unit_paragraph_index = paragraph_index
            base_detection = detect_language(item["text"])
            if block_kind == "table":
                pieces = split_table_text(item["text"])
            elif base_detection["language"] == "mixed":
                pieces = split_mixed_text(item["text"])
            else:
                pieces = [{"start": 0, "end": len(item["text"]), "text": item["text"]}]
            for piece in pieces:
                piece_start = int(item["start"]) + int(piece["start"])
                piece_end = int(item["start"]) + int(piece["end"])
                piece_text = piece["text"]
                detection = detect_language(piece_text)
                reference_metadata = block_kind == "reference"
                translation_required = bool(
                    detection["needs_translation"] and not reference_metadata
                )
                memory_key = translation_memory_key(
                    piece_text, detection["language"], glossary=glossary,
                    target_language=target_language, contract_version=contract_version,
                )
                units.append({
                    "unit_id": _unit_id(kind, piece_start, piece_end, piece_text),
                    "kind": kind, "start": piece_start, "end": piece_end,
                    "block_kind": block_kind, "section": section,
                    "paragraph_index": unit_paragraph_index,
                    "source_text": piece_text, "source_language": detection["language"],
                    "language_confidence": detection["confidence"],
                    "translation_required": translation_required,
                    "memory_key": memory_key, "status": "pending" if translation_required else "not_required",
                    "target_text": None if translation_required else piece_text,
                    "attempts": 0, "model": None, "usage": {},
                    "qa": (
                        {"passed": True, "warnings": ["reference_metadata_preserved"]}
                        if reference_metadata else None
                    ),
                    "error": None, "retryable": True,
                    "preserved_reason": "reference_metadata" if reference_metadata else None,
                })
    return units


def build_translation_plan(document, max_unit_chars=2400, glossary=None,
                           target_language=TARGET_LANGUAGE,
                           contract_version=TRANSLATION_CONTRACT_VERSION,
                           coalesce_paragraphs=False):
    units = build_translation_units(
        document, max_unit_chars=max_unit_chars, glossary=glossary,
        target_language=target_language, contract_version=contract_version,
    )
    if coalesce_paragraphs:
        units = _coalesce_fast_paragraph_units(
            units, max_unit_chars, _normalise_glossary(glossary), contract_version,
        )
    required = [unit for unit in units if unit["translation_required"]]
    detection = detect_language(
        (document.get("text") if isinstance(document, dict) else document) or ""
    )
    return {
        "source_language": detection["language"],
        "language_detection": detection,
        "unit_count": len(units), "required_unit_count": len(required),
        "required_characters": sum(len(unit["source_text"]) for unit in required),
        "paragraph_batching": bool(coalesce_paragraphs),
        "translation_required": bool(required),
        "units": [{
            "unit_id": unit["unit_id"], "kind": unit["kind"],
            "block_kind": unit.get("block_kind"), "section": unit.get("section"),
            "paragraph_index": unit.get("paragraph_index"),
            "start": unit["start"], "end": unit["end"],
            "source_language": unit["source_language"],
            "translation_required": unit["translation_required"],
            "memory_key": unit["memory_key"],
        } for unit in units],
    }


def _coalesce_fast_paragraph_units(units, max_chars, glossary, contract_version):
    """Pack adjacent prose paragraphs into fewer model requests.

    Headings, tables and footnotes retain their own units because their layout
    contracts are stricter. Only contiguous foreign prose in the same section
    and language is combined, so joining translated units remains lossless.
    """
    output = []
    for raw in units:
        unit = copy.deepcopy(raw)
        previous = output[-1] if output else None
        can_merge = bool(
            previous
            and previous.get("kind") == unit.get("kind") == "body"
            and previous.get("block_kind") in {"paragraph", "paragraph_batch"}
            and unit.get("block_kind") == "paragraph"
            and previous.get("translation_required")
            and unit.get("translation_required")
            and previous.get("source_language") == unit.get("source_language")
            and previous.get("section") == unit.get("section")
            and int(previous.get("end") or 0) == int(unit.get("start") or -1)
            and len(str(previous.get("source_text") or ""))
            + len(str(unit.get("source_text") or "")) <= int(max_chars)
        )
        if not can_merge:
            unit["source_unit_ids"] = [unit.get("unit_id")]
            unit["source_segments"] = [{
                "unit_id": unit.get("unit_id"),
                "start": unit.get("start"),
                "end": unit.get("end"),
                "source_text": unit.get("source_text"),
            }]
            output.append(unit)
            continue

        source = str(previous.get("source_text") or "") + str(unit.get("source_text") or "")
        previous.update({
            "end": unit["end"],
            "source_text": source,
            "block_kind": "paragraph_batch",
            "paragraph_end_index": unit.get("paragraph_index"),
            "unit_id": _unit_id("body", previous["start"], unit["end"], source),
            "memory_key": translation_memory_key(
                source, previous["source_language"], glossary=glossary,
                target_language=TARGET_LANGUAGE, contract_version=contract_version,
            ),
            "source_unit_ids": list(previous.get("source_unit_ids") or [])
            + [unit.get("unit_id")],
            "source_segments": list(previous.get("source_segments") or [])
            + [{
                "unit_id": unit.get("unit_id"),
                "start": unit.get("start"),
                "end": unit.get("end"),
                "source_text": unit.get("source_text"),
            }],
        })
    return output


class TranslationService:
    """Translate one parsed document with checkpoints and resumable units."""

    def __init__(self, provider=None, memory=None, policy=None, reviewer=None):
        self.provider = provider or UnavailableTranslationProvider()
        self.memory = memory or InMemoryTranslationMemory()
        self.policy = policy or TranslationPolicy()
        self.reviewer = reviewer

    def _result(self, document, units, glossary, cancelled=False):
        if isinstance(document, str):
            document = {"text": document}
        document = document or {}
        structure = document.get("structure") or {}
        source = document.get("source") or {}
        original_title = str(structure.get("title") or source.get("name") or "")
        original_text = str(document.get("text") or "")
        required = [unit for unit in units if unit["translation_required"]]
        completed = [unit for unit in required if unit["status"] == "completed"]
        failed = [unit for unit in required if unit["status"] == "failed"]
        pending = [unit for unit in required if unit["status"] == "pending"]
        warnings = sorted({
            warning
            for unit in units
            for warning in ((unit.get("qa") or {}).get("warnings") or [])
        })
        all_ready = not failed and not pending
        if not required:
            status = "not_required"
        elif all_ready:
            status = "completed"
        elif failed and not completed and not pending:
            status = "failed"
        else:
            status = "partial"

        def joined(kind):
            selected = [unit for unit in units if unit["kind"] == kind]
            if any(unit["target_text"] is None for unit in selected):
                return None
            return "".join(unit["target_text"] for unit in selected)

        def working(kind):
            selected = [unit for unit in units if unit["kind"] == kind]
            return "".join(
                unit["target_text"]
                if unit.get("target_text") is not None
                else unit.get("source_text") or ""
                for unit in selected
            )

        source_fingerprint = document_translation_fingerprint(document)
        language_detection = detect_language(original_title + "\n" + original_text)
        return {
            "schema_version": TRANSLATION_SCHEMA_VERSION,
            "contract_version": self.policy.contract_version,
            "source_fingerprint": source_fingerprint,
            "source_path": source.get("path"),
            "source_language": language_detection["language"],
            "language_detection": language_detection,
            "target_language": TARGET_LANGUAGE,
            "provider_id": self.provider.provider_id,
            "translation_mode": "fast" if not self.policy.review_complex_units else "quality",
            "performance": {
                "max_unit_chars": self.policy.max_unit_chars,
                "paragraph_batching": self.policy.coalesce_paragraphs,
                "review_strategy": (
                    "quality_failure_and_complex_content"
                    if self.policy.review_complex_units else "quality_failure_only"
                ),
                "provider_attempts": sum(int(unit.get("attempts") or 0) for unit in required),
                "reviewed_units": sum(1 for unit in required if unit.get("reviewed_by")),
            },
            "glossary_fingerprint": glossary_fingerprint(glossary),
            "status": status, "translation_required": bool(required),
            "cancelled": bool(cancelled),
            "original_title": original_title, "translated_title": joined("title"),
            "original_text": original_text, "translated_text": joined("body"),
            "working_title": working("title"), "working_text": working("body"),
            "progress": {
                "total_units": len(units), "required_units": len(required),
                "completed_units": len(completed), "failed_units": len(failed),
                "pending_units": len(pending),
                "ratio": round(len(completed) / float(len(required) or 1), 6),
            },
            "units": copy.deepcopy(units),
            "errors": [copy.deepcopy(unit["error"]) for unit in failed if unit.get("error")],
            "warnings": warnings,
            "updated_at": _utc_now(),
        }

    @staticmethod
    def _resume_lookup(resume_state):
        lookup = {}
        if not isinstance(resume_state, dict):
            return lookup
        for unit in resume_state.get("units") or []:
            if not isinstance(unit, dict):
                continue
            key = unit.get("memory_key")
            if key and unit.get("status") == "completed" and unit.get("target_text") is not None:
                lookup[key] = copy.deepcopy(unit)
        return lookup

    def _reuse(self, unit, resume_lookup):
        candidate = resume_lookup.get(unit["memory_key"])
        origin = "checkpoint"
        if candidate is None:
            try:
                candidate = self.memory.get(unit["memory_key"])
            except Exception as exc:
                unit["memory_warning"] = "翻译记忆读取失败：{}".format(str(exc)[:300])
                candidate = None
            origin = "translation_memory"
        if not isinstance(candidate, dict):
            return False
        qa = candidate.get("qa") or {}
        if candidate.get("target_text") is None or qa.get("passed") is not True:
            return False
        unit.update({
            "status": "completed", "target_text": candidate["target_text"],
            "attempts": int(candidate.get("attempts") or 0),
            "model": candidate.get("model"), "usage": copy.deepcopy(candidate.get("usage") or {}),
            "qa": copy.deepcopy(qa), "error": None, "retryable": True,
            "reused_from": origin,
        })
        return True

    def _translate_unit(self, unit, glossary, initial_response=None):
        source_text = unit["source_text"]
        leading_match = re.match(r"^\s*", source_text)
        trailing_match = re.search(r"\s*$", source_text)
        leading = leading_match.group(0) if leading_match else ""
        trailing = trailing_match.group(0) if trailing_match else ""
        core_end = len(source_text) - len(trailing) if trailing else len(source_text)
        core = source_text[len(leading):core_end]
        protected = protect_text(core, glossary=glossary)
        last_error = None

        def validate_response(response):
            restored_core = protected.restore(response.text).strip()
            qa = validate_translation(
                core, protected, response.text, restored_core,
                unit["source_language"], glossary=glossary,
                strict_layout=unit.get("block_kind") == "table",
            )
            if not qa["passed"]:
                raise TranslationQualityError(
                    "翻译质量校验失败：{}".format(", ".join(qa["errors"])),
                    code=qa["errors"][0] if qa["errors"] else "quality_error",
                )
            return restored_core, qa

        def reviewed_response(draft, qa_errors):
            if self.reviewer is None:
                return None
            if hasattr(self.reviewer, "review"):
                value = self.reviewer.review(
                    protected.text, draft, unit["source_language"], TARGET_LANGUAGE,
                    glossary=glossary, qa_errors=qa_errors,
                    timeout=self.policy.timeout_seconds,
                )
            else:
                value = self.reviewer.translate(
                    protected.text, unit["source_language"], TARGET_LANGUAGE,
                    glossary=glossary, timeout=self.policy.timeout_seconds, retries=0,
                )
            return _normalise_provider_response(value)

        def segmented_recovery():
            """Retry an overlong prose unit as verified natural subsegments.

            NLLB's token-window fallback is safe against truncation, but a
            single dense paragraph can still produce a partly copied target.
            Repeating that same request is deterministic and wastes CPU.  Only
            after that specific quality failure, split at the existing
            lossless sentence/paragraph boundaries and validate every result
            independently.  The first recovery level uses one provider batch;
            only an individual short piece that still fails is subdivided
            further. Tables never enter this recovery path.
            """
            segment_limit = max(360, min(480, self.policy.max_unit_chars // 8))
            pieces = segment_text(core, max_chars=segment_limit)
            if len(pieces) < 2:
                return None

            def prepare_piece(piece_source):
                leading_match = re.match(r"^\s*", piece_source)
                trailing_match = re.search(r"\s*$", piece_source)
                leading_piece = leading_match.group(0) if leading_match else ""
                trailing_piece = trailing_match.group(0) if trailing_match else ""
                core_end = len(piece_source) - len(trailing_piece) if trailing_piece else len(piece_source)
                piece_core = piece_source[len(leading_piece):core_end]
                return {
                    "source": piece_source,
                    "leading": leading_piece,
                    "trailing": trailing_piece,
                    "core": piece_core,
                    "protected": protect_text(piece_core, glossary=glossary) if piece_core else None,
                }

            def validate_piece(item, response):
                piece_core = item["core"]
                if not piece_core:
                    return item["source"], {"passed": True, "warnings": []}
                protected_piece = item["protected"]
                restored_piece_core = protected_piece.restore(response.text).strip()
                piece_qa = validate_translation(
                    piece_core, protected_piece, response.text, restored_piece_core,
                    unit["source_language"], glossary=glossary, strict_layout=False,
                )
                if not piece_qa["passed"]:
                    raise TranslationQualityError(
                        "细分重试质量校验失败：{}".format(
                            ", ".join(piece_qa["errors"])
                        ),
                        code=piece_qa["errors"][0] if piece_qa["errors"] else "quality_error",
                    )
                return item["leading"] + restored_piece_core + item["trailing"], piece_qa

            def recover_piece(piece_source):
                """Recursively retry only one short piece that still copied text."""
                item = prepare_piece(piece_source)
                if not item["core"]:
                    return piece_source, {"passed": True, "warnings": []}, None, 1
                response = _normalise_provider_response(self.provider.translate(
                    item["protected"].text, unit["source_language"], TARGET_LANGUAGE,
                    glossary=glossary, timeout=self.policy.timeout_seconds, retries=0,
                ))
                try:
                    rendered, piece_qa = validate_piece(item, response)
                    return rendered, piece_qa, response, 1
                except TranslationQualityError as exc:
                    if exc.code != "target_contains_untranslated_text" or len(item["core"]) <= 180:
                        raise
                    child_limit = max(180, min(360, len(item["core"]) // 2))
                    children = segment_text(piece_source, max_chars=child_limit)
                    if len(children) < 2:
                        raise
                    rendered_parts = []
                    qa_parts = []
                    last_response = response
                    count = 0
                    for child in children:
                        rendered, child_qa, child_response, child_count = recover_piece(
                            str(child.get("text") or "")
                        )
                        rendered_parts.append(rendered)
                        qa_parts.append(child_qa)
                        last_response = child_response or last_response
                        count += child_count
                    return "".join(rendered_parts), {
                        "passed": True,
                        "warnings": sorted({
                            warning for result in qa_parts
                            for warning in (result.get("warnings") or [])
                        }),
                    }, last_response, count

            prepared = [prepare_piece(str(piece.get("text") or "")) for piece in pieces]
            translatable = [item for item in prepared if item["core"]]
            responses = []
            if translatable and hasattr(self.provider, "translate_batch"):
                try:
                    responses = list(self.provider.translate_batch(
                        [item["protected"].text for item in translatable],
                        unit["source_language"], TARGET_LANGUAGE, glossary=glossary,
                        timeout=self.policy.timeout_seconds, retries=0,
                    ))
                    if len(responses) != len(translatable):
                        raise TranslationProviderError(
                            "细分翻译返回数量不匹配", retryable=True,
                            code="batch_response_mismatch",
                        )
                    responses = [_normalise_provider_response(value) for value in responses]
                except Exception:
                    responses = []
            restored_pieces = []
            qa_results = []
            last_response = None
            response_index = 0
            actual_segment_count = 0
            for item in prepared:
                if not item["core"]:
                    restored_pieces.append(item["source"])
                    continue
                response = responses[response_index] if responses else None
                response_index += 1
                if response is None:
                    rendered, piece_qa, response, count = recover_piece(item["source"])
                else:
                    try:
                        rendered, piece_qa = validate_piece(item, response)
                        count = 1
                    except TranslationQualityError as exc:
                        if exc.code != "target_contains_untranslated_text":
                            raise
                        rendered, piece_qa, response, count = recover_piece(item["source"])
                restored_pieces.append(rendered)
                qa_results.append(piece_qa)
                last_response = response or last_response
                actual_segment_count += count
            if not qa_results:
                return None
            warnings = sorted({
                warning for result in qa_results for warning in (result.get("warnings") or [])
            })
            return (
                "".join(restored_pieces),
                {
                    "passed": True,
                    "score": min(float(result.get("score") or 0.0) for result in qa_results),
                    "errors": [],
                    "warnings": warnings,
                    "fallback_segmented": True,
                    "segment_count": actual_segment_count,
                },
                last_response,
            )

        for _run_attempt in range(self.policy.max_attempts):
            unit["attempts"] = int(unit.get("attempts") or 0) + 1
            try:
                if initial_response is not None:
                    response = _normalise_provider_response(initial_response)
                    initial_response = None
                else:
                    response = _normalise_provider_response(self.provider.translate(
                        protected.text, unit["source_language"], TARGET_LANGUAGE,
                        glossary=glossary, timeout=self.policy.timeout_seconds, retries=0,
                    ))
                try:
                    restored_core, qa = validate_response(response)
                except TranslationQualityError as primary_error:
                    recovered = None
                    if (
                        primary_error.code == "target_contains_untranslated_text"
                        and unit.get("block_kind") != "table"
                        and len(core) >= max(1200, int(self.policy.max_unit_chars * 0.45))
                    ):
                        recovered = segmented_recovery()
                    if recovered is not None:
                        restored_core, qa, response = recovered
                        unit["recovered_by"] = "segmented_retry"
                    else:
                        reviewed = reviewed_response(response.text, [primary_error.code])
                        if reviewed is None:
                            raise
                        response = reviewed
                        restored_core, qa = validate_response(response)
                        unit["reviewed_by"] = response.model or getattr(self.reviewer, "provider_id", "reviewer")

                complex_unit = (
                    unit.get("block_kind") in {"table", "footnote"}
                    or unit.get("source_language") == "mixed"
                    or len(core) >= max(800, int(self.policy.max_unit_chars * 0.7))
                )
                if (
                    complex_unit
                    and self.policy.review_complex_units
                    and self.reviewer is not None
                    and not unit.get("reviewed_by")
                ):
                    try:
                        reviewed = reviewed_response(response.text, ["complex_content_review"])
                        reviewed_core, reviewed_qa = validate_response(reviewed)
                        response, restored_core, qa = reviewed, reviewed_core, reviewed_qa
                        unit["reviewed_by"] = response.model or getattr(self.reviewer, "provider_id", "reviewer")
                    except Exception as review_error:
                        # A verified base translation remains usable when the
                        # optional second model is unavailable.
                        qa.setdefault("warnings", []).append("review_unavailable")
                        unit["review_warning"] = str(review_error)[:300]
                restored = leading + restored_core + trailing
                unit.update({
                    "status": "completed", "target_text": restored,
                    "model": response.model or self.provider.provider_id,
                    "usage": copy.deepcopy(response.usage or {}), "qa": qa,
                    "error": None, "retryable": True, "reused_from": None,
                })
                memory_record = copy.deepcopy(unit)
                memory_record["stored_at"] = _utc_now()
                try:
                    self.memory.put(unit["memory_key"], memory_record)
                except Exception as exc:
                    # Translation memory is an optimisation. The verified
                    # checkpoint remains valid even if its cache is offline.
                    unit["memory_warning"] = "翻译记忆写入失败：{}".format(str(exc)[:300])
                return True
            except TranslationProviderError as exc:
                last_error = exc
                if not exc.retryable:
                    break
            except TranslationQualityError as exc:
                last_error = exc
            except Exception as exc:  # A provider adapter must not crash a worker.
                last_error = TranslationProviderError(
                    "翻译提供者异常：{}".format(str(exc)[:500]), retryable=True,
                    code="provider_exception",
                )
        error = last_error or TranslationProviderError("翻译失败")
        unit.update({
            "status": "failed", "target_text": None, "qa": None,
            "error": {"code": getattr(error, "code", "translation_error"), "message": str(error)[:1000]},
            "retryable": bool(getattr(error, "retryable", True)),
        })
        return False

    def translate_document(self, document, glossary=None, resume_state=None,
                           max_units=None, checkpoint_callback=None,
                           cancel_check=None, fail_fast=False):
        """Translate a unified document and return checkpoint-ready state.

        ``max_units`` limits newly invoked model units, not cache hits, making it
        suitable for separate foreground/background budgets. Rerunning with the
        returned state resumes only pending or failed units.
        """
        glossary = _normalise_glossary(glossary)
        units = build_translation_units(
            document, max_unit_chars=self.policy.max_unit_chars, glossary=glossary,
            target_language=TARGET_LANGUAGE,
            contract_version=self.policy.contract_version,
        )
        if (
            self.policy.coalesce_paragraphs
            and getattr(self.provider, "preserves_line_breaks", True)
        ):
            units = _coalesce_fast_paragraph_units(
                units, self.policy.max_unit_chars, glossary,
                self.policy.contract_version,
            )
        resume_lookup = self._resume_lookup(resume_state)
        for unit in units:
            if unit["translation_required"]:
                self._reuse(unit, resume_lookup)

        if max_units is None:
            budget = None
        else:
            budget = max(0, int(max_units))
        invoked = 0
        cancelled = False
        pending_units = [unit for unit in units if unit["status"] == "pending"]
        planned_invocations = len(pending_units)
        if budget is not None:
            planned_invocations = min(planned_invocations, budget)

        # Constructing a complete checkpoint is O(document size): it joins
        # translated content and detaches all unit dictionaries. Keep the
        # number of such snapshots independent of document length. The unit
        # interval expands for very large plans, while the hard cap also
        # bounds checkpoints from a slow provider's time trigger.
        checkpoint_cap = self.policy.max_intermediate_checkpoints
        checkpoint_unit_interval = self.policy.checkpoint_unit_interval
        if planned_invocations > checkpoint_cap:
            checkpoint_unit_interval = max(
                checkpoint_unit_interval,
                (planned_invocations + checkpoint_cap - 1) // checkpoint_cap,
            )
        intermediate_checkpoints = 0
        units_since_checkpoint = 0
        last_checkpoint_at = time.monotonic()

        if checkpoint_callback is not None:
            # Persist the deterministic plan before the provider is invoked.
            # Cancellation before the first unit is therefore resumable, and
            # a one-unit budget still has distinct initial/final snapshots.
            checkpoint_callback(self._result(document, units, glossary, cancelled=False))
            last_checkpoint_at = time.monotonic()

        index = 0
        while index < len(units):
            unit = units[index]
            index += 1
            if unit["status"] != "pending":
                continue
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            if budget is not None and invoked >= budget:
                break
            batch_units = [unit]
            batch_size = int(getattr(self.provider, "batch_size", 1) or 1)
            if hasattr(self.provider, "translate_batch") and batch_size > 1:
                while len(batch_units) < batch_size and index < len(units):
                    candidate = units[index]
                    if (
                        candidate.get("status") == "pending"
                        and candidate.get("source_language") == unit.get("source_language")
                        and (budget is None or invoked + len(batch_units) < budget)
                    ):
                        batch_units.append(candidate)
                        index += 1
                    else:
                        break
            responses = None
            if len(batch_units) > 1:
                protected_texts = []
                for batch_unit in batch_units:
                    source_text = str(batch_unit.get("source_text") or "")
                    leading_match = re.match(r"^\s*", source_text)
                    trailing_match = re.search(r"\s*$", source_text)
                    leading = leading_match.group(0) if leading_match else ""
                    trailing = trailing_match.group(0) if trailing_match else ""
                    core_end = len(source_text) - len(trailing) if trailing else len(source_text)
                    core = source_text[len(leading):core_end]
                    protected_texts.append(protect_text(core, glossary=glossary).text)
                try:
                    responses = self.provider.translate_batch(
                        protected_texts, unit["source_language"], TARGET_LANGUAGE,
                        glossary=glossary, timeout=self.policy.timeout_seconds, retries=0,
                    )
                    if len(responses) != len(batch_units):
                        raise TranslationProviderError(
                            "批量翻译返回数量不匹配", retryable=True, code="batch_response_mismatch"
                        )
                except Exception:
                    responses = None
            stop_after_batch = False
            for offset, batch_unit in enumerate(batch_units):
                invoked += 1
                succeeded = self._translate_unit(
                    batch_unit, glossary,
                    initial_response=(responses[offset] if responses is not None else None),
                )
                units_since_checkpoint += 1
                if not succeeded and (fail_fast or not batch_unit.get("retryable", True)):
                    stop_after_batch = True
                    break
            if (
                checkpoint_callback is not None
                and intermediate_checkpoints < checkpoint_cap
            ):
                now = time.monotonic()
                if (
                    units_since_checkpoint >= checkpoint_unit_interval
                    or now - last_checkpoint_at >= self.policy.checkpoint_interval_seconds
                ):
                    checkpoint_callback(
                        self._result(document, units, glossary, cancelled=False)
                    )
                    intermediate_checkpoints += 1
                    units_since_checkpoint = 0
                    last_checkpoint_at = now
            if stop_after_batch:
                break

        result = self._result(document, units, glossary, cancelled=cancelled)
        if checkpoint_callback is not None:
            # Nothing owned by the worker is mutated after this point. Passing
            # the terminal result directly avoids one more full-state copy.
            checkpoint_callback(result)
        return result


__all__ = [
    "TARGET_LANGUAGE", "TRANSLATION_SCHEMA_VERSION", "DEFAULT_TECHNICAL_GLOSSARY", "TranslationError",
    "TranslationProviderError", "TranslationQualityError", "ProviderResponse",
    "TranslationProvider", "UnavailableTranslationProvider", "OllamaTranslationProvider",
    "TranslationMemory", "InMemoryTranslationMemory", "StorageTranslationMemory", "TranslationPolicy",
    "TranslationService", "detect_language", "segment_text", "protect_text",
    "validate_translation", "translation_memory_key", "glossary_fingerprint",
    "build_translation_units", "build_translation_plan", "document_translation_fingerprint",
]
