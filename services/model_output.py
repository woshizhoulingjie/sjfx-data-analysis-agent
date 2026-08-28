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

    # Last-resort recovery: locate a JSON value start and let JSONDecoder find its
    # true endpoint.  We never guess an endpoint from a closing brace.
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(raw[index:])
            return value
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))

    detail = errors[-1] if errors else "未发现 JSON 对象或数组"
    raise ModelOutputError("模型未返回可解析的 JSON：{}".format(detail))


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
