"""Safe extraction and validation for structured model responses.

Models occasionally prepend a sentence, use a Markdown fence, or emit an
otherwise valid JSON value with trailing text.  This module keeps that behaviour
at the boundary: downstream analysis code receives a verified object or a clear
``ModelOutputError`` that can trigger its existing evidence-backed fallback.
"""

import json
import re


class ModelOutputError(ValueError):
    """The model response was not a usable structured result."""


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)\s*```")


def _decode_complete(candidate):
    """Decode a complete JSON value, allowing only surrounding whitespace."""
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(candidate.lstrip())
    if candidate.lstrip()[end:].strip():
        raise ValueError("JSON 后仍有非空内容")
    return value


def extract_json_value(content):
    """Extract the first valid JSON object or array without hand-cutting braces.

    The decoder understands quoted braces and escaped text, unlike a
    ``find('{')``/``rfind('}')`` implementation.  Markdown fences are treated as
    explicit candidates first; plain JSON remains the fast path.
    """
    if not isinstance(content, str) or not content.strip():
        raise ModelOutputError("模型返回为空，无法解析结构化结果")

    raw = content.strip().lstrip("\ufeff")
    candidates = [raw]
    candidates.extend(match.group(1).strip() for match in _FENCE_RE.finditer(raw))

    errors = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return _decode_complete(candidate)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))

    # Last-resort recovery: locate the first JSON value start and let JSONDecoder
    # find its true endpoint. Do not scan subsequent starts: when an object is
    # truncated, that would incorrectly promote a nested array/object to the
    # top-level result and hide the real generation failure.
    decoder = json.JSONDecoder()
    index = next((i for i, char in enumerate(raw) if char in "{["), None)
    if index is not None:
        try:
            value, _end = decoder.raw_decode(raw[index:])
            return value
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))

    detail = errors[-1] if errors else "未发现 JSON 对象或数组"
    raise ModelOutputError("模型未返回可解析的 JSON：{}".format(detail))


_FIELD_ALIASES = {
    # These aliases are deliberately narrow.  They cover recurring, equivalent
    # summary names without inventing analytical content or accepting an
    # arbitrary model field as a required application field.
    "core_summary": ("summary", "section_summary", "abstract", "overview"),
    "section_summary": ("summary", "core_summary", "abstract", "overview"),
    "translation": ("translated_text", "translated", "text"),
    "recommended_research_direction": (
        "research_direction",
        "recommended_direction",
        "research_recommendation",
    ),
}


def repair_json_object(value, required_fields=None):
    """Apply only lossless compatibility repairs to a parsed JSON response.

    vLLM is asked for a strict JSON object, but an interrupted or older model
    response can still occasionally wrap one valid object in a one-item list.
    Unwrapping that exact shape is safe; multi-item lists are never guessed at.
    Known semantic aliases are copied only when a caller explicitly requires the
    canonical field.
    """
    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], dict):
            return value
        value = dict(value[0])
    elif isinstance(value, dict):
        value = dict(value)
    else:
        return value

    # A few providers place the actual object under a generic envelope.  Only
    # unwrap it when it is the sole field, so no independently returned content
    # is discarded.
    if len(value) == 1:
        wrapped = next(iter(value.values()))
        if next(iter(value.keys())) in {"result", "data", "output", "analysis"} and isinstance(wrapped, dict):
            value = dict(wrapped)

    for field in (required_fields or ()):
        if value.get(field) not in (None, ""):
            continue
        for alias in _FIELD_ALIASES.get(field, ()):
            candidate = value.get(alias)
            if candidate not in (None, ""):
                value[field] = candidate
                break
    return value


def validate_json_object(value, required_fields=None, context="模型输出"):
    """Validate the minimum contract shared by all current model workflows.

    Individual call sites may declare fields that are indispensable for their
    operation.  Optional fields deliberately remain optional so model variation
    can be handled by each workflow's evidence-backed fallback.
    """
    if not isinstance(value, dict):
        raise ModelOutputError("{}必须是 JSON 对象，实际为 {}".format(context, type(value).__name__))
    missing = [
        field for field in (required_fields or ())
        if field not in value or value.get(field) in (None, "")
    ]
    if missing:
        raise ModelOutputError("{}缺少必要字段：{}".format(context, ", ".join(missing)))
    return value
