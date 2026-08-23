"""Deterministic numeric questions over bounded structured-data profiles.

The profiles are summaries rather than row-level data, so this module only
answers operations that can be combined without inventing information. In
particular, profiles from several files are combined only when their schemas
are compatible. Every participating table remains visible in the returned
scope and evidence.
"""

import math
import re


OPERATION_WORDS = (
    ("sum", ("合计", "总和", "总计", "总额", "累计", "sum", "total")),
    ("average", ("平均", "均值", "average", "avg")),
    ("max", ("最大", "最高", "峰值", "max")),
    ("min", ("最小", "最低", "min")),
    ("count", ("多少", "数量", "总数", "条数", "行数", "几条", "记录数", "count", "多少个", "how many", "rows", "records")),
)

ROW_COUNT_WORDS = (
    "记录数", "记录数量", "总记录", "多少条", "几条", "行数", "多少行",
    "数据条数", "数据量", "records", "row count", "rows",
)

MAX_SCOPE_ITEMS = 200
MAX_EVIDENCE_ITEMS = 20


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
            if profile.get("status") in {"completed", "partial"}:
                yield path, table_name, profile


def _omitted_profile_count(documents):
    omitted = 0
    for item in documents or []:
        payload = item.get("payload") or {}
        nested = [
            value for value in (payload.get("data_profiles") or [])
            if isinstance(value, dict) and isinstance(value.get("profile"), dict)
        ]
        declared = max(len(nested), int(payload.get("data_profiles_total") or 0))
        omitted += max(0, declared - len(nested))
    return omitted


def _canonical_name(value):
    """Normalize cosmetic column-name differences without guessing synonyms."""
    return re.sub(r"[\s_\-./\\:：()（）\[\]【】]+", "", str(value or "").casefold())


def _schema_signature(profile):
    columns = profile.get("columns") or {}
    return tuple(sorted(
        (_canonical_name(name), str(column.get("inferred_type") or "unknown").casefold())
        for name, column in columns.items()
    ))


def _field_score(question, column_name):
    raw_question = str(question or "").casefold()
    raw_name = str(column_name or "").strip().casefold()
    normalized_question = _canonical_name(question)
    normalized_name = _canonical_name(column_name)
    if not normalized_name:
        return 0
    score = 0
    if normalized_name in normalized_question:
        # Prefer the more specific field when both "金额" and "销售金额"
        # occur in the same schema.
        score += 100 + min(40, len(normalized_name))
    if raw_name and raw_name in raw_question:
        score += 20
    for token in re.split(r"[_\s\-]+", raw_name):
        token = token.strip()
        if len(token) >= 2 and token in raw_question:
            score += 4
    return score


def _source_hint_score(question, source_path, table_name):
    """Recognise an explicit file/member hint used to narrow heterogeneous data."""
    q = str(question or "").casefold()
    path = str(source_path or "").replace("\\", "/").casefold()
    basename = path.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    score = 0
    if len(path) >= 3 and path in q:
        score += 30
    if len(basename) >= 3 and basename in q:
        score += 20
    if len(stem) >= 2 and stem in q:
        score += 10
    table = str(table_name or "").strip().casefold()
    if table not in {"", "主数据表", "压缩包成员"} and len(table) >= 2 and table in q:
        score += 15
    return score


def _is_partial(profile):
    coverage = profile.get("coverage") or {}
    limits = profile.get("limits") or {}
    return bool(
        profile.get("status") == "partial"
        or coverage.get("complete") is False
        or coverage.get("truncated")
        or limits.get("truncated")
    )


def _row_count(profile):
    try:
        return max(0, int(profile.get("row_count") or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _result_number(value):
    value = round(float(value), 6)
    if value == 0:
        return 0
    if value.is_integer():
        return int(value)
    return value


def _row_range(profile):
    rows = _row_count(profile)
    return [2, rows + 1] if rows else None


def _scope_summary(items, all_profiles, schema_group_count=1, omitted_profile_count=0):
    paths = list(dict.fromkeys(path for path, _, _ in items))
    tables = list(dict.fromkeys(table for _, table, _ in items))
    participants = [
        {
            "source_path": path,
            "table": table,
            "row_count": _row_count(profile),
            "status": profile.get("status"),
            "complete": not _is_partial(profile),
        }
        for path, table, profile in items[:MAX_SCOPE_ITEMS]
    ]
    item_keys = {(path, table, id(profile)) for path, table, profile in items}
    excluded = [
        (path, table, profile)
        for path, table, profile in all_profiles
        if (path, table, id(profile)) not in item_keys
    ]
    return {
        "mode": "cross_file" if len(paths) > 1 else "single_file",
        "participating_profile_count": len(items),
        "participating_source_count": len(paths),
        "participating_sources": participants,
        "participating_sources_truncated": len(items) > MAX_SCOPE_ITEMS,
        "source_paths": paths[:MAX_SCOPE_ITEMS],
        "source_paths_truncated": len(paths) > MAX_SCOPE_ITEMS,
        "tables": tables[:MAX_SCOPE_ITEMS],
        "tables_truncated": len(tables) > MAX_SCOPE_ITEMS,
        "excluded_profile_count": len(excluded),
        "excluded_sources": [
            {"source_path": path, "table": table}
            for path, table, _ in excluded[:MAX_SCOPE_ITEMS]
        ],
        "excluded_sources_truncated": len(excluded) > MAX_SCOPE_ITEMS,
        "schema_group_count": schema_group_count,
        "omitted_projected_profile_count": int(omitted_profile_count or 0),
    }


def _coverage(items, excluded_count=0, heterogeneous=False, omitted_profile_count=0):
    partial_count = sum(1 for _, _, profile in items if _is_partial(profile))
    warnings = []
    if partial_count:
        warnings.append("结果基于有界采样，不能代表未采样记录；请补充分析或回到原表复核。")
    if heterogeneous:
        warnings.append("记录数汇总包含结构不同的数据表；各表行数含义可能不同，请结合参与范围理解。")
    if excluded_count:
        warnings.append("当前范围另有 {} 张表未参与本次字段计算。".format(excluded_count))
    if omitted_profile_count:
        warnings.append(
            "大包有界投影另省略 {} 张结构化表，当前结果不能视为全范围精确统计。".format(
                omitted_profile_count
            )
        )
    return {
        "complete": not partial_count and not omitted_profile_count,
        "partial_profiles": partial_count,
        "participating_profiles": len(items),
        "omitted_projected_profiles": int(omitted_profile_count or 0),
        "heterogeneous": bool(heterogeneous),
        "warning": " ".join(warnings) or None,
    }


def _display_source(paths):
    paths = list(dict.fromkeys(paths))
    if not paths:
        return "当前范围"
    if len(paths) == 1:
        return paths[0]
    preview = "、".join(paths[:3])
    return "{}{}（共 {} 个文件）".format(preview, " 等" if len(paths) > 3 else "", len(paths))


def _display_table(items):
    names = list(dict.fromkeys(table for _, table, _ in items))
    if len(items) == 1:
        return names[0]
    if len(names) == 1:
        return "{}（共 {} 张表）".format(names[0], len(items))
    return "{} 张表/成员".format(len(items))


def _row_count_evidence(items):
    evidence = []
    for path, table, profile in items[:MAX_EVIDENCE_ITEMS]:
        rows = _row_count(profile)
        sampled = "（有界采样）" if _is_partial(profile) else ""
        evidence.append({
            "source_path": path,
            "table": table,
            "row_range": _row_range(profile),
            "statistic": "row_count",
            "value": rows,
            "text": "结构化画像记录数：{} 行{}。".format(rows, sampled),
        })
    if len(items) > MAX_EVIDENCE_ITEMS:
        evidence.append({
            "source_path": "当前计算范围",
            "table": "汇总说明",
            "text": "另有 {} 张表参与计算；完整数量与截断标记见 aggregation_scope。".format(
                len(items) - MAX_EVIDENCE_ITEMS
            ),
        })
    return evidence


def _answer_row_count(question, profiles, all_profiles, omitted_profile_count=0):
    signatures = {_schema_signature(profile) for _, _, profile in profiles}
    heterogeneous = len(signatures) > 1
    scope = _scope_summary(
        profiles, all_profiles, schema_group_count=len(signatures),
        omitted_profile_count=omitted_profile_count,
    )
    coverage = _coverage(
        profiles,
        excluded_count=scope["excluded_profile_count"],
        heterogeneous=heterogeneous,
        omitted_profile_count=omitted_profile_count,
    )
    value = sum(_row_count(profile) for _, _, profile in profiles)
    paths = [path for path, _, _ in profiles]
    confidence = "待核验" if not value else ("中" if coverage["partial_profiles"] or heterogeneous else "高")
    return {
        "question": question,
        "operation": "count",
        "value": value,
        "unit": "行",
        "source_path": _display_source(paths),
        "source_paths": scope["source_paths"],
        "table": _display_table(profiles),
        "confidence": confidence,
        "coverage": coverage,
        "aggregation_scope": scope,
        "calculation": "各参与表 row_count 相加；未使用任何数值列的非空计数",
        "evidence": _row_count_evidence(profiles),
    }


def _field_candidates(question, profiles):
    candidates = []
    for source_path, table_name, profile in profiles:
        seen_names = set()
        for name, column in (profile.get("columns") or {}).items():
            if column.get("inferred_type") != "number":
                continue
            canonical = _canonical_name(name)
            if canonical in seen_names:
                raise ValueError("表 {}::{} 含有规范化后重名的数值字段，无法安全计算".format(source_path, table_name))
            seen_names.add(canonical)
            candidates.append({
                "score": _field_score(question, name),
                "canonical": canonical,
                "source_path": source_path,
                "table": table_name,
                "name": str(name),
                "column": column,
                "profile": profile,
            })
    return candidates


def _select_field(question, candidates):
    if not candidates:
        raise ValueError("没有找到与问题匹配的数值字段，请在问题中写出字段名")
    grouped = {}
    for candidate in candidates:
        grouped.setdefault(candidate["canonical"], []).append(candidate)
    group_scores = {
        canonical: max(item["score"] for item in items)
        for canonical, items in grouped.items()
    }
    best_score = max(group_scores.values())
    if best_score <= 0:
        if len(grouped) != 1:
            names = sorted({item["name"] for item in candidates})
            raise ValueError("问题未明确匹配到唯一数值字段，请写出字段名；可选字段：{}".format("、".join(names[:12])))
        only_name = next(iter(grouped))
        return only_name, grouped[only_name]
    winners = [name for name, score in group_scores.items() if score == best_score]
    if len(winners) != 1:
        display_names = sorted({item["name"] for name in winners for item in grouped[name]})
        raise ValueError("问题同时匹配多个数值字段，请明确指定一个字段：{}".format("、".join(display_names[:12])))
    return winners[0], grouped[winners[0]]


def _select_compatible_group(column_name, candidates):
    groups = {}
    for candidate in candidates:
        signature = _schema_signature(candidate["profile"])
        groups.setdefault(signature, []).append(candidate)
    if len(groups) == 1:
        return next(iter(groups.values()))
    descriptions = []
    for items in groups.values():
        sources = ["{}::{}".format(item["source_path"], item["table"]) for item in items]
        descriptions.append("、".join(sources[:3]))
    raise ValueError(
        "字段“{}”同时出现在 {} 组结构不同的数据表中，系统为避免错误混算已拒绝计算；"
        "请在问题中写明文件名，或先限定目录/节点范围。涉及：{}".format(
            column_name, len(groups), "；".join(descriptions[:5])
        )
    )


def _reject_non_numeric_matches(canonical_name, profiles):
    incompatible = []
    for source_path, table_name, profile in profiles:
        for name, column in (profile.get("columns") or {}).items():
            if _canonical_name(name) != canonical_name:
                continue
            if column.get("inferred_type") != "number":
                incompatible.append(
                    "{}::{}（{}={}）".format(
                        source_path,
                        table_name,
                        name,
                        column.get("inferred_type") or "unknown",
                    )
                )
    if incompatible:
        raise ValueError(
            "目标字段在部分表中不是数值类型，系统为避免遗漏或错误混算已拒绝计算：{}".format(
                "、".join(incompatible[:8])
            )
        )


def _local_stat(operation, item):
    column = item["column"]
    count = _number(column.get("count"))
    if count is not None and count < 0:
        count = None

    if operation == "sum":
        value = _number(column.get("sum"))
        if value is None:
            mean = _number(column.get("mean"))
            if mean is not None and count is not None:
                value = mean * count
                return value, count, True
        return value, count, False
    if operation == "average":
        total = _number(column.get("sum"))
        mean = _number(column.get("mean"))
        if count is None or count <= 0:
            return None, count, False
        if total is None and mean is not None:
            total = mean * count
            return total, count, True
        return total, count, False
    if operation == "max":
        return _number(column.get("max")), count, False
    if operation == "min":
        return _number(column.get("min")), count, False
    if operation == "count":
        return count, count, False
    return None, count, False


def _aggregate(operation, items, column_name):
    values = []
    counts = []
    reconstructed = []
    missing = []
    for item in items:
        value, count, was_reconstructed = _local_stat(operation, item)
        if value is None:
            missing.append("{}::{}".format(item["source_path"], item["table"]))
            continue
        values.append(value)
        counts.append(count)
        reconstructed.append(was_reconstructed)
    if missing:
        raise ValueError(
            "字段 {} 在部分参与表中缺少 {} 所需的统计值，系统未返回不完整合计：{}".format(
                column_name, operation, "、".join(missing[:8])
            )
        )
    if not values:
        raise ValueError("字段 {} 没有可用的完整统计值".format(column_name))
    if operation == "sum":
        value = math.fsum(values)
        calculation = "各参与表字段 sum 相加"
    elif operation == "average":
        total_count = math.fsum(count for count in counts if count is not None)
        if total_count <= 0:
            raise ValueError("字段 {} 没有可用于加权平均的数值记录".format(column_name))
        value = math.fsum(values) / total_count
        calculation = "各参与表字段 sum 总和 ÷ 非空数值 count 总和（加权平均）"
    elif operation == "max":
        value = max(values)
        calculation = "取各参与表字段 max 的最大值"
    elif operation == "min":
        value = min(values)
        calculation = "取各参与表字段 min 的最小值"
    else:
        value = math.fsum(values)
        calculation = "各参与表该字段的非空数值 count 相加"
    return _result_number(value), calculation, reconstructed


def _field_evidence(operation, items, reconstructed):
    evidence = []
    for index, item in enumerate(items[:MAX_EVIDENCE_ITEMS]):
        value, count, _ = _local_stat(operation, item)
        if operation == "average":
            local_value = _number(item["column"].get("mean"))
            detail = "局部均值={}，非空数值={}，加权分子={}。".format(
                local_value, _result_number(count or 0), _result_number(value or 0)
            )
        elif operation == "count":
            detail = "字段非空数值={}；表记录数={}。".format(
                _result_number(value or 0), _row_count(item["profile"])
            )
        else:
            detail = "局部 {}={}；表记录数={}。".format(
                operation, _result_number(value), _row_count(item["profile"])
            )
        notes = []
        if _is_partial(item["profile"]):
            notes.append("有界采样，建议复核原表")
        if reconstructed[index]:
            notes.append("由均值×非空数值数重建")
        evidence.append({
            "source_path": item["source_path"],
            "table": item["table"],
            "column": item["name"],
            "row_range": _row_range(item["profile"]),
            "statistic": operation,
            "value": _result_number(value),
            "numeric_count": _result_number(count) if count is not None else None,
            "text": "字段 {}：{}{}".format(
                item["name"], detail, " " + "；".join(notes) + "。" if notes else ""
            ),
        })
    if len(items) > MAX_EVIDENCE_ITEMS:
        evidence.append({
            "source_path": "当前计算范围",
            "table": "汇总说明",
            "text": "另有 {} 张同构表参与计算；完整数量与截断标记见 aggregation_scope。".format(
                len(items) - MAX_EVIDENCE_ITEMS
            ),
        })
    return evidence


def answer_question(question, documents):
    question = str(question or "").strip()
    if not question:
        raise ValueError("请输入问题")
    all_profiles = list(_profile_items(documents))
    omitted_profiles = _omitted_profile_count(documents)
    if not all_profiles:
        raise ValueError("当前范围没有可用的 CSV/XLSX/JSON 结构化数据画像；请切换到原始目录根节点或选择一个表格文件后重试")

    operation = _operation(question)
    if not operation:
        raise ValueError("暂时只支持合计、平均、最大、最小、数量等可验证的精确数字问题")

    hinted = [
        item for item in all_profiles
        if _source_hint_score(question, item[0], item[1]) > 0
    ]
    eligible_profiles = hinted or all_profiles

    # This branch deliberately runs before numeric-field selection so a lone
    # numeric column cannot turn a row-count question into a non-null count.
    if operation == "count" and any(word in question.casefold() for word in ROW_COUNT_WORDS):
        return _answer_row_count(
            question, eligible_profiles, all_profiles,
            omitted_profile_count=omitted_profiles,
        )

    candidates = _field_candidates(question, eligible_profiles)
    if operation == "count" and (not candidates or max(item["score"] for item in candidates) <= 0):
        return _answer_row_count(
            question, eligible_profiles, all_profiles,
            omitted_profile_count=omitted_profiles,
        )

    canonical_field, field_candidates = _select_field(question, candidates)
    _reject_non_numeric_matches(canonical_field, eligible_profiles)
    display_column = sorted(
        {item["name"] for item in field_candidates}, key=lambda value: (-len(value), value)
    )[0]
    selected = _select_compatible_group(display_column, field_candidates)
    selected.sort(key=lambda item: (item["source_path"], item["table"], item["name"]))
    selected_profiles = [
        (item["source_path"], item["table"], item["profile"])
        for item in selected
    ]
    scope = _scope_summary(
        selected_profiles, all_profiles,
        omitted_profile_count=omitted_profiles,
    )
    coverage = _coverage(
        selected_profiles,
        excluded_count=scope["excluded_profile_count"],
        omitted_profile_count=omitted_profiles,
    )
    value, calculation, reconstructed = _aggregate(operation, selected, display_column)
    paths = [item["source_path"] for item in selected]
    return {
        "question": question,
        "operation": operation,
        "value": value,
        "unit": "个非空数值" if operation == "count" else None,
        "column": display_column,
        "source_path": _display_source(paths),
        "source_paths": scope["source_paths"],
        "table": _display_table(selected_profiles),
        "confidence": "中" if coverage["partial_profiles"] else ("高" if max(item["score"] for item in selected) >= 100 else "中"),
        "coverage": coverage,
        "aggregation_scope": scope,
        "calculation": calculation,
        "evidence": _field_evidence(operation, selected, reconstructed),
    }
