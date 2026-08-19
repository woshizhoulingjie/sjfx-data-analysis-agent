"""Bounded, dependency-light profiling for CSV/XLSX/JSON data files."""

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

STRUCTURED_EXTENSIONS = {".csv", ".tsv", ".json", ".jsonl", ".xlsx", ".xlsm"}
SENSITIVE_PATTERNS = {
    "email": re.compile(r"email|e-mail|邮箱|邮件", re.I),
    "phone": re.compile(r"phone|mobile|tel|电话|手机", re.I),
    "id": re.compile(r"(^|[_ -])(id|身份证|证件|账号|账户|编号)($|[_ -])", re.I),
    "name": re.compile(r"name|姓名|联系人|客户名称", re.I),
    "address": re.compile(r"address|地址|住址", re.I),
}

ENTITY_PATTERNS = {
    "person": re.compile(r"name|姓名|联系人|负责人|作者|人员|员工|客户", re.I),
    "location": re.compile(r"address|地址|地点|位置|区域|城市|省|市|县|国家|地区|经纬度", re.I),
    "event": re.compile(r"event|事件|活动|事项|类型|状态|category|类别|主题", re.I),
}


def _env_int(name, default):
    import os
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _missing(value):
    return value is None or str(value).strip().lower() in {"", "null", "none", "nan", "n/a", "na", "-"}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        text = str(value).strip().replace(",", "")
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _date(value):
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).isoformat()
        except ValueError:
            pass
    return None


def _kind(value):
    if _missing(value):
        return "missing"
    if _number(value) is not None:
        return "number"
    if _date(value):
        return "datetime"
    return "text"


def _profile_rows(rows, columns, row_count, duplicate_rows, max_values=20):
    result = {}
    for name, values in columns.items():
        kinds = Counter(_kind(value) for value in values)
        numbers = [n for n in (_number(value) for value in values) if n is not None]
        dates = [d for d in (_date(value) for value in values) if d]
        non_missing = [str(value).strip() for value in values if not _missing(value)]
        missing_count = sum(1 for value in values if _missing(value))
        item = {
            "name": name,
            "inferred_type": kinds.most_common(1)[0][0] if kinds else "unknown",
            "missing_count": missing_count,
            "missing_ratio": round(missing_count / float(max(1, row_count)), 6),
            "unique_count": len(set(non_missing)),
            "sample_values": list(dict.fromkeys(non_missing))[:max_values],
            "type_counts": dict(kinds),
        }
        if numbers:
            ordered = sorted(numbers)
            item.update({"min": min(numbers), "max": max(numbers), "sum": round(sum(numbers), 6), "count": len(numbers), "mean": round(sum(numbers) / len(numbers), 6), "median": ordered[len(ordered) // 2]})
            if len(ordered) >= 4:
                q1, q3 = ordered[len(ordered) // 4], ordered[(len(ordered) * 3) // 4]
                iqr = q3 - q1
                item["outlier_count"] = sum(1 for value in numbers if value < q1 - 1.5 * iqr or value > q3 + 1.5 * iqr)
            if len(ordered) >= 5:
                buckets = Counter()
                lower, upper = min(numbers), max(numbers)
                width = (upper - lower) / 5.0 if upper != lower else 1.0
                for value in numbers:
                    bucket = min(4, int((value - lower) / width)) if width else 0
                    buckets["{}-{}".format(bucket + 1, 5)] += 1
                item["distribution"] = dict(buckets)
        if dates:
            item.update({"min_datetime": min(dates), "max_datetime": max(dates)})
        non_missing_counter = Counter(non_missing)
        if item["inferred_type"] == "text":
            item["top_values"] = [{"value": value, "count": count} for value, count in non_missing_counter.most_common(max_values)]
        sensitive = [label for label, pattern in SENSITIVE_PATTERNS.items() if pattern.search(str(name))]
        if sensitive:
            item["sensitive_categories"] = sensitive
        entities = [label for label, pattern in ENTITY_PATTERNS.items() if pattern.search(str(name))]
        if entities:
            item["entity_categories"] = entities
        result[str(name)] = item
    missing = sum(item["missing_count"] for item in result.values())
    total = max(1, row_count * len(result))
    completeness = max(0.0, 1.0 - missing / float(total))
    uniqueness = sum(1 for item in result.values() if item.get("unique_count", 0) >= max(1, row_count * 0.9)) / float(len(result) or 1)
    duplicate_penalty = min(0.25, duplicate_rows / float(max(1, row_count)))
    quality = round(max(0.0, min(100.0, (completeness * 70 + uniqueness * 30) * (1 - duplicate_penalty))), 2)
    missing_columns = [name for name, item in result.items() if item["missing_count"]]
    numeric_columns = [name for name, item in result.items() if item["inferred_type"] == "number"]
    temporal_columns = [name for name, item in result.items() if item["inferred_type"] == "datetime"]
    sensitive_columns = [name for name, item in result.items() if item.get("sensitive_categories")]
    entity_columns = {
        category: [name for name, item in result.items() if category in (item.get("entity_categories") or [])]
        for category in ENTITY_PATTERNS
    }
    entity_columns = {key: value for key, value in entity_columns.items() if value}
    entity_statistics = {}
    for category, names in entity_columns.items():
        values = []
        for name in names:
            values.extend(str(value).strip() for value in columns.get(name, []) if not _missing(value))
        counts = Counter(values)
        entity_statistics[category] = {
            "columns": names,
            "distinct_count": len(counts),
            "top_values": [{"value": value, "count": count} for value, count in counts.most_common(20)],
        }
    recommendation_questions = []
    if temporal_columns:
        recommendation_questions.append("按时间字段统计记录量和关键数值的变化趋势")
    if numeric_columns:
        recommendation_questions.append("哪些数值字段的均值、最大值和异常值最值得关注？")
    if entity_statistics.get("person"):
        recommendation_questions.append("不同人物/责任人对应的记录量和数值差异是什么？")
    if entity_statistics.get("location"):
        recommendation_questions.append("不同地区/地点的数据分布和质量差异是什么？")
    if entity_statistics.get("event"):
        recommendation_questions.append("不同事件/类型/状态的数量和关键指标如何比较？")
    return {
        "schema_version": "structured-profile/1.0", "status": "completed", "row_count": row_count,
        "column_count": len(result), "columns": result, "duplicate_row_count": duplicate_rows,
        "quality_score": quality, "missing_columns": missing_columns, "numeric_columns": numeric_columns,
        "temporal_columns": temporal_columns, "sensitive_columns": sensitive_columns,
        "entity_columns": entity_columns, "entity_statistics": entity_statistics,
        "recommendation_questions": recommendation_questions[:8],
        "value_judgment": {
            "usable": bool(row_count and result),
            "value_level": "高" if quality >= 80 and row_count >= 10 else ("中" if quality >= 50 else "待治理"),
            "reason": "包含可统计字段、{} 行记录，数据质量评分 {} / 100。".format(row_count, quality),
            "recommended_next_steps": (["脱敏后进行精确统计"] if sensitive_columns else []) + (["核查缺失值和重复记录"] if missing_columns or duplicate_rows else []) + (["按时间字段做趋势分析"] if temporal_columns else []),
        },
        "limits": {"sampled": True, "max_rows": _env_int("MAX_STRUCTURED_PROFILE_ROWS", 100000)},
    }


def _iter_csv(path, max_rows, max_bytes):
    rows = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        reader = csv.reader(stream, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
        header = next(reader, None)
        if not header:
            return rows, [], 0
        seen, names = Counter(), []
        for index, name in enumerate(header):
            name = str(name).strip() or "column_{}".format(index + 1)
            seen[name] += 1
            names.append(name if seen[name] == 1 else "{}_{}".format(name, seen[name]))
        consumed = 0
        for values in reader:
            consumed += sum(len(str(value)) for value in values)
            if consumed > max_bytes or len(rows) >= max_rows:
                break
            rows.append(dict(zip(names, values)))
        return rows, names, consumed


def _iter_json(path, max_rows, max_bytes):
    raw = path.read_bytes()[: max_bytes + 1]
    text = raw[:max_bytes].decode("utf-8-sig", errors="replace")
    try:
        if path.suffix.lower() == ".jsonl":
            values = [json.loads(line) for line in text.splitlines() if line.strip()][:max_rows]
        else:
            value = json.loads(text)
            values = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
            values = values[:max_rows]
    except (ValueError, TypeError):
        return [], [], len(raw)
    if not values or not isinstance(values[0], dict):
        return [], [], len(raw)
    names = list(dict.fromkeys(key for item in values if isinstance(item, dict) for key in item))
    return [{name: item.get(name) for name in names} for item in values if isinstance(item, dict)], names, len(raw)


def profile_path(path, max_rows=None, max_bytes=None):
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in STRUCTURED_EXTENSIONS:
        return None
    max_rows = max_rows or _env_int("MAX_STRUCTURED_PROFILE_ROWS", 100000)
    max_bytes = max_bytes or _env_int("MAX_STRUCTURED_PROFILE_BYTES", 256 * 1024 * 1024)
    if path.stat().st_size > max_bytes and ext in {".xlsx", ".xlsm"}:
        return {"schema_version": "structured-profile/1.0", "status": "skipped", "reason": "文件超过结构化画像大小上限", "value_judgment": {"usable": False, "value_level": "待治理"}}
    try:
        if ext in {".csv", ".tsv"}:
            rows, names, consumed = _iter_csv(path, max_rows, max_bytes)
        elif ext in {".json", ".jsonl"}:
            rows, names, consumed = _iter_json(path, max_rows, max_bytes)
        else:
            from openpyxl import load_workbook
            book = load_workbook(str(path), read_only=True, data_only=True)
            sheet = book.active
            iterator = sheet.iter_rows(values_only=True)
            header = next(iterator, None)
            names = [str(value or "column_{}".format(index + 1)).strip() for index, value in enumerate(header or [])]
            rows = []
            for values in iterator:
                if len(rows) >= max_rows:
                    break
                rows.append(dict(zip(names, values)))
            consumed = path.stat().st_size
            book.close()
    except Exception as exc:
        return {"schema_version": "structured-profile/1.0", "status": "failed", "error": str(exc)[:300], "value_judgment": {"usable": False, "value_level": "待治理"}}
    columns = defaultdict(list)
    duplicate_counter = Counter()
    for row in rows:
        duplicate_counter[tuple(str(row.get(name, "")).strip() for name in names)] += 1
        for name in names:
            columns[name].append(row.get(name))
    duplicate_rows = sum(count - 1 for count in duplicate_counter.values() if count > 1)
    profile = _profile_rows(rows, columns, len(rows), duplicate_rows)
    profile["source"] = {"path": str(path), "extension": ext, "size": path.stat().st_size}
    profile["limits"].update({"sampled_bytes": consumed, "source_bytes": path.stat().st_size})
    return profile
