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


def is_recorded_total_lookup(question):
    """Recognize a source's stated total without weakening aggregation safety.

    ``合计`` can name a field already printed on one receipt. It does not
    necessarily ask us to sum records. Explicit calculation and multi-source
    instructions retain the structured provider and its exactness guard.
    """
    text = str(question or "")
    if re.search(
        r"所有|全部|这些|这批|多份|多个|各份|各个|跨文件|跨文档|逐条|"
        r"计算|算出|求和|加总|累加|统计|汇总|平均|均值|最大值|最小值|占比|分组",
        text,
    ):
        return False
    return bool(
        re.search(r"凭证|发票|合同|报告|单据|文件|原文|记录", text)
        and re.search(r"(?:合计|总额|总计|总和|累计)(?:金额|费用|款项)?\s*(?:为|是|有)?\s*(?:多少|几|什么)", text)
    )
