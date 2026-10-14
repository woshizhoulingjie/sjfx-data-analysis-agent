"""Shared recognition of natural-language package questions."""
import re


PACKAGE_OVERVIEW_PATTERN = (
    r"(?:数据包|资料包|这些资料|这批资料|资料).{0,16}"
    r"(?:研究(?:了|的)?什么|讲(?:了|的)?(?:是)?(?:些)?什么|说(?:了|的)?什么|"
    r"包含(?:了)?(?:哪些|什么))"
)
PACKAGE_OVERVIEW_RE = re.compile(PACKAGE_OVERVIEW_PATTERN, re.I)


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
