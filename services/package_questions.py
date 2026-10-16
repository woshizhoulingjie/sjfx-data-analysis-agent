"""Shared recognition of natural-language package questions."""
import re


PACKAGE_OVERVIEW_PATTERN = (
    r"(?:数据包|资料包|这些资料|这批资料|资料).{0,16}"
    r"(?:研究(?:了|的)?什么|讲(?:了|的)?(?:是)?(?:些)?什么|说(?:了|的)?什么|"
    r"包含(?:了)?(?:哪些|什么))"
)
PACKAGE_OVERVIEW_RE = re.compile(PACKAGE_OVERVIEW_PATTERN, re.I)

# Listing evidence, values or conclusions is an answer-format instruction.
# A file discovery request names files/documents, or is a standalone search
# command without a factual question. Keep planner and answer routing aligned.
FILE_SEARCH_PATTERN = (
    r"(?:搜索|查找|找一下|找出|找到|列出|显示|筛选)"
    r"[^。！？!?；;，,\n]{0,80}(?:文件|资料|文档)(?![中内里])"
    r"|(?:包含|命中|有关|关于)[^。！？!?；;，,\n]{0,80}(?:文件|资料|文档)(?![中内里])"
    r"|(?:文件|资料|文档)[^。！？!?；;，,\n]{0,40}"
    r"(?:搜索|查找|列表|清单|在哪|有哪些(?!发现|结论|成果|特点|问题|变化|影响|风险|建议|方法))"
    r"|(?:哪些|什么)[^。！？!?；;，,\n]{0,40}(?:文件|资料|文档)"
    r"[^。！？!?；;，,\n]{0,40}(?:包含|命中)"
    r"|\b(?:find|search|list|show|locate|which)\b[^。！？!?；;，,\n]{0,80}\b(?:files?|documents?)\b"
    r"|\bwhat\s+(?:files?|documents?)\b"
    r"|^(?:请|帮我|麻烦|给我)?\s*(?:搜索|搜索一下|查找|找一下|帮我找|找到|找出|筛选)"
    r"(?![^\n]*(?:多少|什么|为何|为什么|怎么|如何|何时|是否|能否))\s*\S+"
)


def file_discovery_text(question):
    """Exclude citation-format clauses from intent detection, not retrieval."""
    return re.sub(
        r"(?:列出|显示|提供|附上|给出|注明|标明)\s*"
        r"(?:(?:所有|全部|相关|关键|对应|直接|支持|原文|可复核|必要|每条|各条|完整)(?:的)?){0,6}"
        r"(?:证据|出处|引文|引用)(?!\s*(?:文件|文档|资料))"
        r"(?:(?:及其|以及|及|和|与|、)\s*(?:对应的|各自的)?"
        r"(?:来源(?:文件|文档|资料)?|文件名|页码|行号|原文位置|段落位置|出处|引用位置))*",
        "", str(question or ""), flags=re.I,
    )


def file_discovery_query(question):
    """Separate a file name/keyword from discovery and output instructions."""
    text = str(question or "").strip()
    explicit = re.search(
        r"(?:文件名|文档名|资料名)\s*(?:为|是|叫(?:做)?|等于|[:：=])\s*"
        r"(?:[\"'“「](.*?)[\"'”」]|([^，,；;\n。！？!?]+))",
        text, re.I,
    )
    if explicit:
        value = explicit.group(1) if explicit.group(1) is not None else explicit.group(2)
        return re.sub(r"的(?:文件|资料|文档)$", "", value.strip()).strip()
    text, stripped_command = re.subn(
        r"^(?:请|帮我|麻烦|给我|可以)?\s*"
        r"(?:搜索|查找|找一下|帮我找|找到|找出|列出|显示|搜索一下)\s*"
        r"(?:有关|关于|包含|命中)?\s*", "", text, flags=re.I,
    )
    text = re.split(
        r"[，,；;]\s*(?:(?:只|仅)(?:列出|显示|返回)|不要(?:分析|总结)|不分析)",
        text, maxsplit=1,
    )[0]
    if stripped_command:
        text = re.sub(r"(?:的)?(?:文件|资料|文档)(?:列表|清单)?[。！？!?]*$", "", text, flags=re.I)
    return text.strip().strip('"\'“”「」').strip()


def is_content_file_discovery_query(question):
    """An explicit content condition must not be satisfied by a name alone."""
    text = str(question or "")
    if re.search(r"(?:文件名|文档名|资料名)\s*(?:为|是|叫(?:做)?|等于|[:：=])", text):
        return False
    return bool(re.search(
        r"(?:查找|搜索|找出|找到|列出|显示)[^。！？!?\n]*(?:包含|含有|提到|写到)"
        r"|^(?:包含|含有|提到|写到).*(?:文件|资料|文档)"
        r"|(?:正文|内容)(?:中|里|内)?\s*(?:包含|含有|提到|写到)", text,
    ))


def is_filename_discovery_query(question):
    """A named file is a metadata lookup, never a request for body evidence."""
    if is_content_file_discovery_query(question):
        return False
    return bool(
        re.search(r"(?:文件名|文档名|资料名)\s*(?:为|是|叫(?:做)?|等于|[:：=])", str(question or ""))
        or re.search(r"\.[A-Za-z][A-Za-z0-9]{0,11}$", file_discovery_query(question))
    )


def file_discovery_scope_contains(scope, path):
    """Intersect directory and explicit file selections, including members."""
    path = str(path or "").replace("\\", "/")
    if not path or not scope.contains_source(path):
        return False
    allowed = tuple(str(item).replace("\\", "/") for item in (getattr(scope, "source_paths", ()) or ()))
    if str(getattr(scope, "kind", "package")) not in {"package", "directory", "files", "file_type"} and not allowed:
        return False
    return not allowed or any(path == item or path.startswith(item + "/") or path.startswith(item + "::") for item in allowed)


def discover_inventory_file_paths(entries, question, scope, limit=5000):
    """Match literal names/paths in the caller's one-scan metadata iterator.

    The caller supplies a scan-bound iterator; no content, evidence index,
    global file list or model is consulted. Metadata hits are not citations.
    """
    from pathlib import PurePosixPath

    needle = file_discovery_query(question).replace("\\", "/").casefold()
    if not needle:
        return [], False
    kind = str(getattr(scope, "kind", "package"))
    allowed = tuple(str(path).replace("\\", "/") for path in (getattr(scope, "source_paths", ()) or ()))
    # Unresolved semantic ranges must never widen to the whole package.
    if kind not in {"package", "directory", "files", "file_type"} and not allowed:
        return [], False
    expected_extension = ""
    if kind == "file_type":
        if (getattr(scope, "constraints", {}) or {}).get("dimension", "format") != "format":
            if not allowed:
                return [], False
        else:
            expected_extension = str(getattr(scope, "value", "") or "").lower().strip()
            if expected_extension and not expected_extension.startswith(".") and expected_extension != "[无扩展名]":
                expected_extension = "." + expected_extension
    paths, seen = [], set()
    limit = max(1, min(5000, int(limit)))
    for entry in entries:
        if entry.get("kind") not in {"file", "logical_file"}:
            continue
        path = str(entry.get("path") or entry.get("node_path") or "").replace("\\", "/")
        if not file_discovery_scope_contains(scope, path):
            continue
        if expected_extension:
            extension = PurePosixPath(path.split("::", 1)[0]).suffix.lower()
            if extension != expected_extension and not (expected_extension == "[无扩展名]" and not extension):
                continue
        name = str((entry.get("payload") or {}).get("name") or PurePosixPath(path).name)
        if needle not in path.casefold() and needle not in name.casefold():
            continue
        if path in seen:
            continue
        if len(paths) >= limit:
            return paths, True
        seen.add(path)
        paths.append(path)
    return paths, False


def is_recorded_total_lookup(question):
    """Recognize a source's stated total without weakening aggregation safety.

    ``合计`` can name a field already printed on one receipt. It does not
    necessarily ask us to sum records. Explicit calculation and multi-source
    instructions retain the structured provider and its exactness guard.
    """
    text = str(question or "")
    if re.search(
        r"跨文件|跨文档|逐条|"
        r"计算|算出|求和|加总|累加|统计|汇总|平均|均值|最大值|最小值|占比|分组",
        text,
    ):
        return False
    # Ignore quantifiers only in a complete citation/answer-format clause.
    # "所有证据中的金额" still selects inputs even though it names evidence.
    scope_text = re.sub(
        r"(?:引用|给出|提供|附上|注明|标明|列出)\s*(?:支持)?"
        r"(?:所有|全部|这些|这批|多份|多个|各份|各个)\s*"
        r"(?:(?:的|原文|相关|对应|完整|关键|直接|必要)\s*)*"
        r"(?:结论|证据|引文|引用|出处|要点|答案|回答)"
        r"(?:的原文证据|及对应的原文证据)?"
        r"(?=\s*(?:$|[，,。！？!?；;：:\n]))",
        "", text,
    )
    if re.search(r"所有|全部|这些|这批|多份|多个|各份|各个", scope_text):
        return False
    return bool(
        re.search(r"凭证|发票|合同|报告|单据|文件|原文|记录", text)
        and re.search(r"(?:合计|总额|总计|总和|累计)(?:金额|费用|款项)?\s*(?:为|是|有)?\s*(?:多少|几|什么)", text)
    )
