
from collections import Counter
import re

OPERATION_WORDS = (
    ("sum", ("合计", "总和", "总计", "总额", "累计", "sum", "total")),
    ("average", ("平均", "均值", "average", "avg")),
    ("max", ("最大", "最高", "峰值", "max")),
    ("min", ("最小", "最低", "min")),
    ("count", ("多少", "数量", "几条", "记录数", "count", "多少个")),
)

def _operation(question):
    q = str(question or "").lower()
    for operation, words in OPERATION_WORDS:
        if any(word.lower() in q for word in words):
            return operation
    return None

def _profile_items(documents):
    for item in documents or []:
        path = item.get("path") or ""
        payload = item.get("payload") or {}
        profiles = []
        if payload.get("data_profile"):
            profiles.append(("主数据表", payload["data_profile"]))
        for nested in payload.get("data_profiles") or []:
            profiles.append((nested.get("member") or "压缩包成员", nested.get("profile") or {}))
        for table_name, profile in profiles:
            if profile.get("status") == "completed":
                yield path, table_name, profile

def answer_question(question, documents):
    question = str(question or "").strip()
    if not question:
        raise ValueError("请输入问题")
    if not any(True for _ in _profile_items(documents)):
        raise ValueError("当前范围没有已完成的 CSV/XLSX/JSON 结构化数据画像；请切换到原始目录根节点或选择一个表格文件后重试")
    operation = _operation(question)
    if not operation:
        raise ValueError("暂时只支持合计、平均、最大、最小、数量等可验证的精确数字问题")
    candidates = []
    for source_path, table_name, profile in _profile_items(documents):
        for name, column in (profile.get("columns") or {}).items():
            if column.get("inferred_type") == "number":
                score = 0
                lowered = str(name).lower()
                if lowered and lowered in question.lower():
                    score += 10
                for token in re.split(r"[_\\s-]+", str(name)):
                    token = token.strip()
                    if len(token) >= 2 and token.lower() in question.lower():
                        score += 4
                for token in re.findall(r"[A-Za-z0-9_\\u4e00-\\u9fff]{2,}", str(name)):
                    if token.lower() in question.lower():
                        score += 2
                candidates.append((score, source_path, table_name, name, column, profile))
    if operation == "count" and not candidates:
        total = sum(int(profile.get("row_count") or 0) for _, _, profile in _profile_items(documents))
        return {
            "question": question, "operation": operation, "value": total,
            "unit": "行", "confidence": "高" if total else "待核验",
            "evidence": [{"source_path": path, "table": table, "row_range": [2, int(profile.get("row_count") or 0) + 1], "text": "结构化画像记录数：{} 行".format(profile.get("row_count") or 0)} for path, table, profile in _profile_items(documents)][:20],
        }
    if not candidates:
        raise ValueError("没有找到与问题匹配的数值字段，请在问题中写出字段名")
    candidates.sort(key=lambda item: (-item[0], item[1], item[3]))
    if candidates[0][0] <= 0 and len(candidates) > 1:
        raise ValueError("问题未明确匹配到唯一数值字段，请写出字段名")
    _, source_path, table_name, column_name, column, profile = candidates[0]
    reconstructed = False
    if operation == "sum":
        value = column.get("sum")
        if value is None and column.get("mean") is not None:
            value = round(float(column.get("mean")) * float(column.get("count") or profile.get("row_count") or 0), 6)
            reconstructed = True
    elif operation == "average":
        value = column.get("mean")
    elif operation == "max":
        value = column.get("max")
    elif operation == "min":
        value = column.get("min")
    else:
        value = column.get("count") or profile.get("row_count")
    if value is None:
        raise ValueError("字段 {} 没有可用的完整统计值".format(column_name))
    return {
        "question": question, "operation": operation, "value": value,
        "column": column_name, "source_path": source_path, "table": table_name,
        "confidence": "高" if candidates[0][0] >= 10 else "中",
        "evidence": [{
            "source_path": source_path, "table": table_name,
            "column": column_name, "row_range": [2, int(profile.get("row_count") or 0) + 1],
            "text": "字段 {}：{}={}，样本记录 {} 行。{}".format(column_name, operation, value, profile.get("row_count") or 0, "由均值×数值记录数重建，建议复核原表。" if reconstructed else ""),
        }],
    }
