"""Bounded, data-only package overview aggregation.

This module deliberately describes the imported data package, not the state of
the pipeline that inspected it.  It therefore contains no job status,
processing progress, failure counts, model telemetry, or worker information.

The aggregator consumes three bounded views:

* physical file metadata (normally ``scan["tree"]``),
* projected document metadata (``Storage.iter_documents(..., hydrate=False)``),
* already-discovered package facts such as topic clusters and file relations.

Full document text is never retained.  Inputs may be generators and are
consumed one record at a time.  High-cardinality dimensions use a bounded
Space-Saving table and explicitly mark approximate/truncated output.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import islice
from pathlib import Path


SCHEMA_VERSION = "package-overview/1.1"
NO_EXTENSION = "[no-extension]"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class OverviewLimits:
    """Memory and response bounds for one overview.

    ``max_group_keys`` bounds the in-memory cardinality of every categorical
    dimension independently.  ``max_items_per_section`` bounds JSON output.
    Representative paths are short evidence/navigation handles, never content.
    """

    max_items_per_section: int = 30
    max_representative_files: int = 5
    max_group_keys: int = 4096
    max_relation_keys: int = 4096
    max_candidate_files: int = 256

    def __post_init__(self):
        object.__setattr__(self, "max_items_per_section", _clamp(self.max_items_per_section, 1, 500))
        object.__setattr__(self, "max_representative_files", _clamp(self.max_representative_files, 1, 20))
        object.__setattr__(self, "max_group_keys", _clamp(self.max_group_keys, 8, 100_000))
        object.__setattr__(self, "max_relation_keys", _clamp(self.max_relation_keys, 8, 100_000))
        object.__setattr__(self, "max_candidate_files", _clamp(self.max_candidate_files, 8, 10_000))


def _clamp(value, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = minimum
    return max(minimum, min(maximum, value))


def _int(value, default=0):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return default


def _float(value, default=0.0):
    try:
        result = float(value or 0.0)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _path(value):
    value = str(value or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value or "."


def _label(value, limit=240):
    value = " ".join(str(value or "").split())
    return value[:limit]


def _values(value, limit=64):
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        values = [value]
    elif isinstance(value, dict):
        values = list(islice(value.values(), max(0, limit)))
    elif isinstance(value, (list, tuple)):
        values = value[:max(0, limit)]
    else:
        try:
            values = list(islice(iter(value), max(0, limit)))
        except TypeError:
            values = [value]
    return values[:limit]


def _records(value, limit=1_000_000):
    """Iterate record collections without copying an already-large list."""
    if value is None:
        return
    if isinstance(value, dict) or isinstance(value, (str, int, float)):
        yield value
        return
    try:
        iterator = iter(value)
    except TypeError:
        yield value
        return
    yield from islice(iterator, max(0, int(limit)))


def _unique_labels(values, limit=64):
    output = []
    seen = set()
    for value in _values(values, limit=limit * 2):
        if isinstance(value, dict):
            value = value.get("name") or value.get("label") or value.get("value") or value.get("text")
        item = _label(value)
        folded = item.casefold()
        if item and folded not in seen:
            seen.add(folded)
            output.append(item)
            if len(output) >= limit:
                break
    return output


def _extension(source, path):
    extension = str((source or {}).get("extension") or "").strip().lower()
    if extension in {"[无扩展名]", "[no-extension]", "[none]"}:
        return NO_EXTENSION
    if not extension:
        extension = Path(str(path or "")).suffix.lower()
    if extension and not extension.startswith("."):
        extension = "." + extension
    return extension or NO_EXTENSION


YEAR_RE = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})(?!\d)")


def _year(value):
    if isinstance(value, dict):
        value = value.get("date") or value.get("value") or value.get("text")
    match = YEAR_RE.search(str(value or ""))
    return match.group(1) if match else None


LANGUAGE_ALIASES = {
    "zh": ("zh", "中文"), "zh-cn": ("zh", "中文"), "zh-hans": ("zh", "中文"),
    "chinese": ("zh", "中文"), "中文": ("zh", "中文"), "汉语": ("zh", "中文"),
    "en": ("en", "英语"), "en-us": ("en", "英语"), "english": ("en", "英语"), "英语": ("en", "英语"),
    "ja": ("ja", "日语"), "japanese": ("ja", "日语"), "日语": ("ja", "日语"),
    "ko": ("ko", "韩语"), "korean": ("ko", "韩语"), "韩语": ("ko", "韩语"),
    "fr": ("fr", "法语"), "french": ("fr", "法语"), "法语": ("fr", "法语"),
    "de": ("de", "德语"), "german": ("de", "德语"), "德语": ("de", "德语"),
    "es": ("es", "西班牙语"), "spanish": ("es", "西班牙语"), "西班牙语": ("es", "西班牙语"),
    "ru": ("ru", "俄语"), "russian": ("ru", "俄语"), "俄语": ("ru", "俄语"),
    "ar": ("ar", "阿拉伯语"), "arabic": ("ar", "阿拉伯语"), "阿拉伯语": ("ar", "阿拉伯语"),
}


def _language(value):
    if isinstance(value, dict):
        value = value.get("code") or value.get("language") or value.get("name") or value.get("label")
    item = _label(value, 80)
    if not item:
        return None
    folded = item.casefold().replace("_", "-")
    if folded in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[folded]
    short = folded.split("-", 1)[0]
    if short in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[short]
    return (folded[:32], item[:80])


DOCUMENT_TYPE_BY_EXTENSION = {
    ".csv": "结构化数据", ".tsv": "结构化数据", ".xls": "结构化数据",
    ".xlsx": "结构化数据", ".ods": "结构化数据", ".parquet": "结构化数据",
    ".jsonl": "结构化数据", ".db": "结构化数据", ".sqlite": "结构化数据",
    ".eml": "信件/邮件", ".msg": "信件/邮件", ".mbox": "信件/邮件",
    ".pdf": "文档资料", ".doc": "文档资料", ".docx": "文档资料",
    ".odt": "文档资料", ".rtf": "文档资料", ".txt": "文本资料",
    ".md": "文本资料", ".markdown": "文本资料",
    ".ppt": "演示文稿", ".pptx": "演示文稿", ".odp": "演示文稿",
    ".jpg": "图片", ".jpeg": "图片", ".png": "图片", ".gif": "图片",
    ".tif": "图片", ".tiff": "图片", ".bmp": "图片", ".webp": "图片",
    ".mp3": "音频", ".wav": "音频", ".flac": "音频", ".m4a": "音频",
    ".mp4": "视频", ".mov": "视频", ".mkv": "视频", ".avi": "视频",
    ".zip": "压缩包", ".7z": "压缩包", ".rar": "压缩包", ".tar": "压缩包",
    ".gz": "压缩包", ".bz2": "压缩包", ".xz": "压缩包",
    ".py": "源代码", ".js": "源代码", ".ts": "源代码", ".java": "源代码",
    ".c": "源代码", ".cpp": "源代码", ".go": "源代码", ".rs": "源代码",
}


class _BoundedGroups:
    """Space-Saving categorical aggregation with explicit error bounds."""

    def __init__(self, capacity, representative_limit):
        self.capacity = capacity
        self.representative_limit = representative_limit
        self.entries = {}
        self.total_count = 0
        self.total_bytes = 0
        self.overflow = False

    def add(self, key, count=1, total_bytes=0, representatives=(), metadata=None):
        key = _label(key)
        count = _int(count)
        total_bytes = _int(total_bytes)
        if not key or count <= 0:
            return
        self.total_count += count
        self.total_bytes += total_bytes
        entry = self.entries.get(key)
        if entry is None and len(self.entries) >= self.capacity:
            self.overflow = True
            victim_key, victim = min(
                self.entries.items(),
                key=lambda item: (item[1]["count"], item[1]["total_bytes"], item[0]),
            )
            del self.entries[victim_key]
            entry = {
                "count": victim["count"], "total_bytes": victim["total_bytes"],
                "count_error": victim["count"], "byte_error": victim["total_bytes"],
                "representatives": [], "metadata": {},
            }
            self.entries[key] = entry
        elif entry is None:
            entry = {
                "count": 0, "total_bytes": 0, "count_error": 0, "byte_error": 0,
                "representatives": [], "metadata": {},
            }
            self.entries[key] = entry
        entry["count"] += count
        entry["total_bytes"] += total_bytes
        if metadata:
            entry["metadata"].update({str(k): v for k, v in metadata.items()})
        for representative in _values(representatives, self.representative_limit * 2):
            representative = _path(representative)
            if representative != "." and representative not in entry["representatives"]:
                entry["representatives"].append(representative)
                if len(entry["representatives"]) >= self.representative_limit:
                    break

    def render(self, key_name, item_limit, total_name="total_memberships", include_bytes=False):
        ranked = sorted(
            self.entries.items(),
            key=lambda item: (-item[1]["count"], -item[1]["total_bytes"], item[0].casefold(), item[0]),
        )
        selected = ranked[:item_limit]
        items = []
        for key, entry in selected:
            item = {
                key_name: key,
                "file_count": entry["count"],
                "count_error_max": entry["count_error"],
                "count_is_estimate": bool(entry["count_error"]),
                "representative_files": list(entry["representatives"]),
            }
            if include_bytes:
                item.update({
                    "total_bytes": entry["total_bytes"],
                    "byte_error_max": entry["byte_error"],
                })
            item.update(entry["metadata"])
            items.append(item)
        known_lower_bound = sum(max(0, entry["count"] - entry["count_error"]) for _, entry in selected)
        truncated = self.overflow or len(ranked) > item_limit
        return {
            "items": items,
            total_name: self.total_count,
            "distinct_count": len(self.entries) if not self.overflow else None,
            "distinct_count_lower_bound": len(self.entries) + (1 if self.overflow else 0),
            "distinct_count_is_lower_bound": self.overflow,
            "truncated": truncated,
            "omitted_count": max(0, len(ranked) - len(selected)) if not self.overflow else None,
            "omitted_membership_count_lower_bound": max(0, self.total_count - known_lower_bound),
            "counts_are_approximate": any(entry["count_error"] for _, entry in selected),
        }


class _RankedItems:
    def __init__(self, capacity):
        self.capacity = capacity
        self.items = {}
        self.total_seen = 0
        self.overflow = False

    def add(self, key, item, score):
        key = str(key)
        score = tuple(score)
        if key in self.items:
            old_score, _old_item = self.items[key]
            if score > old_score:
                self.items[key] = (score, item)
            return
        self.total_seen += 1
        if len(self.items) < self.capacity:
            self.items[key] = (score, item)
            return
        self.overflow = True
        victim_key, victim = min(self.items.items(), key=lambda pair: (pair[1][0], pair[0]))
        if score > victim[0]:
            del self.items[victim_key]
            self.items[key] = (score, item)

    def render(self, limit):
        ranked = sorted(self.items.items(), key=lambda pair: (pair[1][0], pair[0]), reverse=True)
        return [item for _key, (_score, item) in ranked[:limit]]


class _Relationships:
    def __init__(self, capacity, representative_limit):
        self.capacity = capacity
        self.representative_limit = representative_limit
        self.entries = {}
        self.overflow = False
        self.total_observations = 0

    def add(self, source, target, relation="related", weight=1.0, supporting_files=()):
        source, target = _path(source), _path(target)
        relation = _label(relation or "related", 120) or "related"
        if source == "." or target == "." or source == target:
            return
        key = (source, target, relation)
        self.total_observations += 1
        entry = self.entries.get(key)
        if entry is None and len(self.entries) >= self.capacity:
            self.overflow = True
            victim_key, victim = min(
                self.entries.items(), key=lambda pair: (pair[1]["weight"], pair[1]["observations"], pair[0])
            )
            if _float(weight, 1.0) <= victim["weight"]:
                return
            del self.entries[victim_key]
        if entry is None:
            entry = {"weight": 0.0, "observations": 0, "supporting_files": []}
            self.entries[key] = entry
        entry["weight"] += max(0.0, _float(weight, 1.0))
        entry["observations"] += 1
        for path in _values(supporting_files, self.representative_limit * 2):
            path = _path(path)
            if path != "." and path not in entry["supporting_files"]:
                entry["supporting_files"].append(path)
                if len(entry["supporting_files"]) >= self.representative_limit:
                    break

    def render(self, limit):
        ranked = sorted(
            self.entries.items(),
            key=lambda pair: (-pair[1]["weight"], -pair[1]["observations"], pair[0]),
        )
        items = [{
            "source_file": key[0], "target_file": key[1], "relation": key[2],
            "weight": round(entry["weight"], 6),
            "observation_count": entry["observations"],
            "supporting_files": list(entry["supporting_files"]),
        } for key, entry in ranked[:limit]]
        return {
            "items": items,
            "relationship_count": len(self.entries) if not self.overflow else None,
            "relationship_count_lower_bound": len(self.entries) + (1 if self.overflow else 0),
            "relationship_count_is_lower_bound": self.overflow,
            "observation_count": self.total_observations,
            "truncated": self.overflow or len(ranked) > limit,
            "omitted_count": max(0, len(ranked) - len(items)) if not self.overflow else None,
        }


class PackageOverviewAggregator:
    """Incrementally aggregate one package into ``package-overview/1.0``."""

    def __init__(self, limits=None):
        self.limits = limits or OverviewLimits()
        if isinstance(self.limits, dict):
            self.limits = OverviewLimits(**self.limits)
        self.root = None
        self._declared_file_count = None
        self._declared_total_bytes = None
        self._declared_directory_count = None
        self._physical_file_count = 0
        self._physical_total_bytes = 0
        self._max_depth = 0
        self._tree_directories = False
        self._root_directory = None
        self._directory_count_seen = 0
        self._directories = _RankedItems(max(self.limits.max_items_per_section * 4, 32))
        self._directory_rollup = _BoundedGroups(self.limits.max_group_keys, 0)
        self._formats = self._groups()
        self._file_modified = self._groups()
        self._document_dates = self._groups()
        self._document_types = self._groups()
        self._languages = self._groups()
        self._topics = self._groups()
        self._people = self._groups()
        self._organizations = self._groups()
        self._relationships = _Relationships(self.limits.max_relation_keys, self.limits.max_representative_files)
        self._typed_files = 0
        self._language_files = 0
        self._topic_files = 0
        self._document_date_files = 0
        self._entity_files = 0
        self._semantic_files = 0
        self._exact_groups = []
        self._near_groups = []
        self._exact_group_total = 0
        self._near_group_total = 0
        self._exact_duplicate_files_total = 0
        self._near_duplicate_files_total = 0
        self._duplicates_authoritative = False
        self._sample_duplicate_groups = []
        self._sample_duplicate_group_total = 0
        self._sample_duplicate_files_total = 0
        self._explicit_anomalies = _RankedItems(self.limits.max_candidate_files)
        self._isolated_files = _RankedItems(self.limits.max_candidate_files)
        self._largest_files = _RankedItems(self.limits.max_candidate_files)
        self._size_count = 0
        self._size_mean = 0.0
        self._size_m2 = 0.0

    def _groups(self):
        return _BoundedGroups(self.limits.max_group_keys, self.limits.max_representative_files)

    def ingest_scan(self, scan):
        scan = scan or {}
        self.root = scan.get("root") or self.root
        self._declared_file_count = _int(scan.get("file_count")) if "file_count" in scan else self._declared_file_count
        self._declared_total_bytes = _int(scan.get("total_size")) if "total_size" in scan else self._declared_total_bytes
        self._declared_directory_count = _int(scan.get("directory_count")) if "directory_count" in scan else self._declared_directory_count
        tree = scan.get("tree")
        if isinstance(tree, dict) and tree:
            self._tree_directories = True
            stack = [tree]
            while stack:
                node = stack.pop()
                if not isinstance(node, dict):
                    continue
                children = node.get("children") or []
                if node.get("kind") == "directory":
                    self._ingest_directory_node(node)
                    stack.extend(reversed(children))
                elif node.get("kind") in {"file", "symlink"}:
                    self.ingest_file(node, update_directories=False)
        elif scan.get("type_counts"):
            for extension, count in (scan.get("type_counts") or {}).items():
                self._formats.add(_extension({"extension": extension}, ""), count=count)
        return self

    def _ingest_directory_node(self, node):
        path = _path(node.get("path"))
        depth = _int(node.get("scan_depth"), max(0, path.count("/") + (path != ".")))
        item = {
            "path": path,
            "parent_path": None if path == "." else (_path(str(Path(path).parent)) if "/" in path else "."),
            "depth": depth,
            "direct_file_count": _int(node.get("direct_file_count")),
            "direct_directory_count": _int(node.get("direct_directory_count")),
            "recursive_file_count": _int(node.get("file_count")),
            "recursive_file_count_is_estimate": False,
            "recursive_file_count_error_max": 0,
            "recursive_directory_count": _int(node.get("directory_count")),
            "total_bytes": _int(node.get("total_size")),
            "total_bytes_error_max": 0,
        }
        self._max_depth = max(self._max_depth, depth)
        if path == ".":
            self._root_directory = item
        else:
            self._directory_count_seen += 1
            self._directories.add(path, item, (item["total_bytes"], item["recursive_file_count"], -depth))

    def ingest_file(self, record, update_directories=True):
        record = record or {}
        if record.get("kind") == "directory":
            self._ingest_directory_node(record)
            return self
        if record.get("kind") == "symlink":
            return self
        path = _path(record.get("path") or record.get("name"))
        size = _int(record.get("size"))
        self._physical_file_count += 1
        self._physical_total_bytes += size
        depth = max(0, path.count("/"))
        self._max_depth = max(self._max_depth, depth)
        self._formats.add(_extension(record, path), total_bytes=size, representatives=(path,))
        modified_year = _year(record.get("modified_at") or record.get("mtime") or record.get("created_at"))
        if modified_year:
            self._file_modified.add(modified_year, total_bytes=size, representatives=(path,))
        self._update_size(size, path)
        if update_directories:
            parents = ["."]
            parts = path.split("/")[:-1]
            for index in range(len(parts)):
                parents.append("/".join(parts[:index + 1]))
            for parent in parents:
                self._directory_rollup.add(parent, total_bytes=size)
            self._max_depth = max(self._max_depth, len(parts))
        return self

    def _update_size(self, size, path):
        self._size_count += 1
        delta = size - self._size_mean
        self._size_mean += delta / self._size_count
        self._size_m2 += delta * (size - self._size_mean)
        self._largest_files.add(path, {"path": path, "size_bytes": size}, (size,))

    @staticmethod
    def _document_record(record):
        if isinstance(record, (tuple, list)) and len(record) == 2:
            return _path(record[0]), record[1] or {}
        record = record or {}
        if isinstance(record, dict) and isinstance(record.get("payload"), dict):
            return _path(record.get("path") or (record["payload"].get("source") or {}).get("path")), record["payload"]
        source = record.get("source") if isinstance(record, dict) else {}
        return _path((source or {}).get("path") or record.get("path")), record

    def ingest_documents(self, records, include_physical=False):
        for record in records or ():
            self.ingest_document(record, include_physical=include_physical)
        return self

    def ingest_document(self, record, include_physical=False):
        path, document = self._document_record(record)
        source = document.get("source") or {}
        self._semantic_files += 1
        if include_physical:
            self.ingest_file({**source, "path": path}, update_directories=True)

        classification = document.get("classification") or {}
        preview = document.get("preview") or {}
        doc_type = (
            classification.get("document_role") or classification.get("document_type")
            or document.get("document_type") or preview.get("document_type")
            or DOCUMENT_TYPE_BY_EXTENSION.get(_extension(source, path))
        )
        if doc_type:
            self._typed_files += 1
            self._document_types.add(doc_type, representatives=(path,))

        language_values = []
        for candidate in (
            document.get("languages"), document.get("language"), preview.get("languages"), preview.get("language"),
            (document.get("translation") or {}).get("source_language"),
            (document.get("metadata") or {}).get("language"),
        ):
            language_values.extend(_values(candidate, 16))
        languages = []
        for candidate in language_values:
            normalised = _language(candidate)
            if normalised and normalised[0] not in {item[0] for item in languages}:
                languages.append(normalised)
        if languages:
            self._language_files += 1
            for code, display in languages[:8]:
                self._languages.add(code, representatives=(path,), metadata={"label": display})

        topic_values = []
        for candidate in (
            classification.get("primary_topic"), classification.get("topic_memberships"),
            classification.get("topics"), document.get("topics"), document.get("content_topics"),
            preview.get("topics"), (document.get("structure") or {}).get("keywords"),
        ):
            topic_values.extend(_values(candidate, 32))
        topics = _unique_labels(topic_values, 32)
        if topics:
            self._topic_files += 1
            for topic in topics:
                self._topics.add(topic, representatives=(path,))

        people, organizations = self._extract_entities(document)
        if people or organizations:
            self._entity_files += 1
        for person in people:
            self._people.add(person, representatives=(path,))
        for organization in organizations:
            self._organizations.add(organization, representatives=(path,))

        date_values = []
        temporal = document.get("temporal") or {}
        for candidate in (
            document.get("document_date"), document.get("dates"), temporal.get("dates"),
            temporal.get("document_date"), (document.get("metadata") or {}).get("document_date"),
            preview.get("dates"),
        ):
            date_values.extend(_values(candidate, 32))
        years = sorted({value for value in (_year(item) for item in date_values) if value})
        if years:
            self._document_date_files += 1
        for year in years:
            self._document_dates.add(year, representatives=(path,))

        for relation in _records(document.get("file_relationships") or document.get("relationships"), 128):
            self._ingest_relationship(relation, default_source=path)
        for related in _records(document.get("related_files"), 128):
            if isinstance(related, dict):
                self._ingest_relationship(related, default_source=path)
            else:
                self._relationships.add(path, related, supporting_files=(path,))

        deduplication = document.get("deduplication") or {}
        if deduplication.get("role") == "duplicate_alias" and not self._duplicates_authoritative:
            canonical = deduplication.get("duplicate_of") or deduplication.get("canonical_path")
            self._add_document_duplicate(deduplication.get("group_id") or canonical, canonical, path)

        anomalies = _values(document.get("anomalies") or document.get("anomaly"), 32)
        if document.get("is_anomaly") and not anomalies:
            anomalies = ["内容特征被标记为异常"]
        if anomalies:
            reasons = _unique_labels(anomalies, 12)
            self._add_anomaly(path, reasons, score=document.get("anomaly_score"), source="content_signal")
        if document.get("is_isolated") or classification.get("is_isolated"):
            self._add_isolated(path, reason="未发现与其他文件的稳定联系")
        return self

    @staticmethod
    def _extract_entities(document):
        people, organizations = [], []
        containers = [document.get("entities"), document.get("named_entities"), (document.get("preview") or {}).get("entities")]
        person_keys = {"person", "persons", "people", "people_names", "人物", "人名"}
        organization_keys = {"organization", "organizations", "organisation", "organisations", "org", "orgs", "机构", "组织"}
        for container in containers:
            if isinstance(container, dict):
                for key, values in container.items():
                    folded = str(key).casefold()
                    if folded in person_keys:
                        people.extend(_unique_labels(values, 128))
                    elif folded in organization_keys:
                        organizations.extend(_unique_labels(values, 128))
            elif isinstance(container, list):
                for entity in container[:256]:
                    if not isinstance(entity, dict):
                        continue
                    kind = str(entity.get("type") or entity.get("category") or "").casefold()
                    name = _label(entity.get("name") or entity.get("text") or entity.get("label"))
                    if name and kind in person_keys:
                        people.append(name)
                    elif name and kind in organization_keys:
                        organizations.append(name)
        return _unique_labels(people, 128), _unique_labels(organizations, 128)

    def _ingest_relationship(self, relation, default_source=None):
        if not isinstance(relation, dict):
            if default_source:
                self._relationships.add(default_source, relation)
            return
        source = relation.get("source_file") or relation.get("source_path") or relation.get("source") or relation.get("from") or default_source
        target = relation.get("target_file") or relation.get("target_path") or relation.get("target") or relation.get("to")
        self._relationships.add(
            source, target,
            relation=relation.get("relation") or relation.get("type") or relation.get("label") or "related",
            weight=relation.get("weight") or relation.get("score") or 1.0,
            supporting_files=relation.get("supporting_files") or relation.get("files") or ((default_source,) if default_source else ()),
        )

    def _add_document_duplicate(self, group_id, canonical, alias):
        group_id = _label(group_id or canonical)
        if not group_id:
            return
        for group in self._exact_groups:
            if group["group_id"] == group_id:
                group["member_count"] += 1
                group["duplicate_file_count"] += 1
                self._exact_duplicate_files_total += 1
                if len(group["members"]) < self.limits.max_representative_files and alias not in group["members"]:
                    group["members"].append(alias)
                return
        self._exact_group_total += 1
        self._exact_duplicate_files_total += 1
        if len(self._exact_groups) < self.limits.max_group_keys:
            self._exact_groups.append({
                "group_id": group_id, "canonical_file": _path(canonical),
                "member_count": 2, "duplicate_file_count": 1,
                "members": [_path(canonical), _path(alias)], "content_hash": None,
            })

    def ingest_analysis(self, analysis):
        analysis = analysis or {}
        clusters = analysis.get("topic_clusters") or analysis.get("topics")
        if isinstance(clusters, list) and clusters:
            self._topics = self._groups()
            self._topic_files = 0
            assigned_memberships = 0
            for cluster in clusters:
                if not isinstance(cluster, dict):
                    continue
                topic = cluster.get("topic") or cluster.get("name") or cluster.get("title")
                member_source = cluster.get("members") or cluster.get("member_paths") or ()
                if hasattr(member_source, "__len__"):
                    declared_members = len(member_source)
                else:
                    declared_members = 0
                representatives = cluster.get("representative_documents") or _unique_labels(
                    member_source, self.limits.max_representative_files
                )
                count = _int(cluster.get("file_count"), declared_members) or declared_members
                self._topics.add(topic, count=count, representatives=representatives)
                if _label(topic):
                    assigned_memberships += count
            package_files = self._package_file_count()
            self._topic_files = min(package_files, assigned_memberships) if package_files else assigned_memberships

        relations = analysis.get("file_relationships") or analysis.get("relationships")
        graph = analysis.get("relationship_graph") or {}
        if not relations and isinstance(graph, dict):
            relations = graph.get("edges")
        for relation in _records(relations):
            self._ingest_relationship(relation)

        exact_groups = analysis.get("exact_duplicate_groups")
        near_groups = analysis.get("similar_document_clusters") or analysis.get("near_duplicate_groups")
        sample_groups = analysis.get("sample_duplicate_candidates")
        if isinstance(exact_groups, list):
            large_mode = bool(
                ((analysis.get("policy") or {}).get("large_package") or {}).get("enabled")
            )
            self._duplicates_authoritative = bool(exact_groups) or not large_mode
            (
                self._exact_groups,
                self._exact_group_total,
                self._exact_duplicate_files_total,
            ) = self._normalise_duplicate_groups(exact_groups, exact=True)
        if isinstance(near_groups, list):
            (
                self._near_groups,
                self._near_group_total,
                self._near_duplicate_files_total,
            ) = self._normalise_duplicate_groups(near_groups, exact=False)
        if isinstance(sample_groups, list):
            (
                self._sample_duplicate_groups,
                self._sample_duplicate_group_total,
                self._sample_duplicate_files_total,
            ) = self._normalise_duplicate_groups(sample_groups, exact=False)

        entity_data = analysis.get("entities") or analysis.get("named_entities")
        if isinstance(entity_data, dict):
            self._ingest_aggregate_entities(entity_data)

        for item in _records(analysis.get("anomalous_files") or analysis.get("outliers")):
            if isinstance(item, dict):
                self._add_anomaly(
                    item.get("path") or item.get("file"),
                    item.get("reasons") or item.get("reason") or ["被资料分析标记为异常"],
                    score=item.get("score"), source=item.get("source") or "package_signal",
                )
            else:
                self._add_anomaly(item, ["被资料分析标记为异常"], source="package_signal")
        for item in _records(analysis.get("isolated_files")):
            if isinstance(item, dict):
                self._add_isolated(item.get("path") or item.get("file"), item.get("reason"))
            else:
                self._add_isolated(item, "未发现与其他文件的稳定联系")
        return self

    def _normalise_duplicate_groups(self, groups, exact):
        ranked = _RankedItems(self.limits.max_items_per_section)
        group_count = 0
        duplicate_file_count = 0
        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            group_count += 1
            member_source = group.get("members") or group.get("paths") or ()
            members = [_path(item) for item in _values(member_source, self.limits.max_representative_files) if _path(item) != "."]
            declared_members = len(member_source) if hasattr(member_source, "__len__") else len(members)
            member_count = _int(
                group.get("member_count") or group.get("file_count"), declared_members
            ) or declared_members
            duplicate_file_count += max(0, member_count - 1)
            canonical = group.get("canonical") or group.get("representative") or (members[0] if members else None)
            item = {
                "group_id": _label(group.get("group_id") or group.get("cluster_id") or ("DUP-{}".format(index + 1))),
                "canonical_file": _path(canonical),
                "member_count": member_count,
                "duplicate_file_count": max(0, member_count - 1),
                "members": members[:self.limits.max_representative_files],
                "content_hash": group.get("sha256") if exact else None,
                "sample_hash": group.get("sample_sha256") if not exact else None,
                "candidate_kind": group.get("kind") if not exact else None,
            }
            ranked.add(item["group_id"], item, (member_count,))
        return ranked.render(self.limits.max_items_per_section), group_count, duplicate_file_count

    def _ingest_aggregate_entities(self, entities):
        for key, target in (("people", self._people), ("persons", self._people), ("organizations", self._organizations), ("organisations", self._organizations)):
            for entity in _values(entities.get(key), 100_000):
                if isinstance(entity, dict):
                    target.add(
                        entity.get("name") or entity.get("label"),
                        count=entity.get("file_count") or entity.get("count") or 1,
                        representatives=entity.get("files") or entity.get("representative_files") or (),
                    )
                else:
                    target.add(entity)

    def _add_anomaly(self, path, reasons, score=None, source="explicit"):
        path = _path(path)
        if path == ".":
            return
        reasons = _unique_labels(reasons, 12) or ["异常特征"]
        score = max(0.0, _float(score, 1.0))
        item = {"path": path, "reasons": reasons, "score": round(score, 6), "source": _label(source, 80)}
        self._explicit_anomalies.add(path, item, (score, len(reasons)))

    def _add_isolated(self, path, reason=None):
        path = _path(path)
        if path == ".":
            return
        item = {"path": path, "reason": _label(reason or "未发现与其他文件的稳定联系")}
        self._isolated_files.add(path, item, (1,))

    def _package_file_count(self):
        return self._declared_file_count if self._declared_file_count is not None else self._physical_file_count

    def _package_total_bytes(self):
        return self._declared_total_bytes if self._declared_total_bytes is not None else self._physical_total_bytes

    def _render_directories(self):
        limit = self.limits.max_items_per_section
        if self._tree_directories:
            items = []
            if self._root_directory:
                items.append(self._root_directory)
            items.extend(self._directories.render(max(0, limit - len(items))))
            total = self._declared_directory_count if self._declared_directory_count is not None else self._directory_count_seen
            return {
                "items": items, "directory_count": total,
                "truncated": total + int(bool(self._root_directory)) > len(items),
                "omitted_count": max(0, total + int(bool(self._root_directory)) - len(items)),
                "counts_are_approximate": False,
            }
        rendered = self._directory_rollup.render("path", limit, total_name="file_memberships", include_bytes=True)
        items = []
        for item in rendered["items"]:
            path = item.pop("path")
            items.append({
                "path": path,
                "parent_path": None if path == "." else (_path(str(Path(path).parent)) if "/" in path else "."),
                "depth": 0 if path == "." else path.count("/") + 1,
                "direct_file_count": None,
                "direct_directory_count": None,
                "recursive_file_count": item["file_count"],
                "recursive_file_count_is_estimate": item["count_is_estimate"],
                "recursive_file_count_error_max": item["count_error_max"],
                "recursive_directory_count": None,
                "total_bytes": item["total_bytes"],
                "total_bytes_error_max": item["byte_error_max"],
            })
        known_dirs = max(0, (rendered["distinct_count"] or rendered["distinct_count_lower_bound"]) - int(any(item["path"] == "." for item in items)))
        return {
            "items": items,
            "directory_count": known_dirs,
            "truncated": rendered["truncated"],
            "omitted_count": rendered["omitted_count"],
            "counts_are_approximate": rendered["counts_are_approximate"],
        }

    def _render_timeline(self, groups, unknown_count):
        rendered = groups.render("period", self.limits.max_items_per_section, total_name="dated_file_memberships", include_bytes=True)
        return {
            "granularity": "year", "items": rendered["items"],
            "dated_file_memberships": rendered["dated_file_memberships"],
            "unknown_file_count": max(0, unknown_count),
            "truncated": rendered["truncated"],
            "counts_are_approximate": rendered["counts_are_approximate"],
        }

    def _size_anomalies(self):
        if self._size_count < 8:
            return []
        variance = self._size_m2 / max(1, self._size_count - 1)
        standard_deviation = math.sqrt(max(0.0, variance))
        if standard_deviation <= 0:
            return []
        threshold = self._size_mean + 3.0 * standard_deviation
        output = []
        explicit_paths = set(self._explicit_anomalies.items)
        for item in self._largest_files.render(self.limits.max_candidate_files):
            if item["size_bytes"] > threshold and item["path"] not in explicit_paths:
                z_score = (item["size_bytes"] - self._size_mean) / standard_deviation
                output.append({
                    "path": item["path"],
                    "reasons": ["文件大小显著高于数据包中的其他文件"],
                    "score": round(z_score, 6),
                    "source": "size_distribution",
                })
        return output

    def finalize(self):
        file_count = self._package_file_count()
        total_bytes = self._package_total_bytes()
        formats = self._formats.render("format", self.limits.max_items_per_section, total_name="file_count", include_bytes=True)
        document_types = self._document_types.render("document_type", self.limits.max_items_per_section, total_name="classified_memberships")
        languages = self._languages.render("language", self.limits.max_items_per_section, total_name="language_memberships")
        topics = self._topics.render("topic", self.limits.max_items_per_section, total_name="topic_memberships")
        people = self._people.render("name", self.limits.max_items_per_section, total_name="file_mentions")
        organizations = self._organizations.render("name", self.limits.max_items_per_section, total_name="file_mentions")
        document_types["unknown_file_count"] = max(0, file_count - self._typed_files)
        languages["unknown_file_count"] = max(0, file_count - self._language_files)
        topics["unassigned_file_count"] = max(0, file_count - self._topic_files)

        exact_groups = {
            "items": self._exact_groups[:self.limits.max_items_per_section],
            "group_count": self._exact_group_total,
            "duplicate_file_count": self._exact_duplicate_files_total,
            "truncated": self._exact_group_total > self.limits.max_items_per_section,
            "omitted_count": max(0, self._exact_group_total - self.limits.max_items_per_section),
            "authoritative": self._duplicates_authoritative,
            "status": (
                "verified_by_full_hash"
                if self._duplicates_authoritative
                else "not_computed_for_entire_package"
            ),
        }
        near_groups = {
            "items": self._near_groups[:self.limits.max_items_per_section],
            "group_count": self._near_group_total,
            "duplicate_file_count": self._near_duplicate_files_total,
            "truncated": self._near_group_total > self.limits.max_items_per_section,
            "omitted_count": max(0, self._near_group_total - self.limits.max_items_per_section),
        }
        sample_groups = {
            "items": self._sample_duplicate_groups[:self.limits.max_items_per_section],
            "group_count": self._sample_duplicate_group_total,
            "candidate_duplicate_file_count": self._sample_duplicate_files_total,
            "truncated": self._sample_duplicate_group_total > self.limits.max_items_per_section,
            "omitted_count": max(
                0, self._sample_duplicate_group_total - self.limits.max_items_per_section
            ),
            "status": "sample_candidates_require_full_hash_validation",
            "authoritative": False,
        }

        anomalies = self._explicit_anomalies.render(self.limits.max_items_per_section)
        known_anomaly_paths = {item["path"] for item in anomalies}
        for item in self._size_anomalies():
            if item["path"] not in known_anomaly_paths and len(anomalies) < self.limits.max_items_per_section:
                anomalies.append(item)
                known_anomaly_paths.add(item["path"])
        anomaly_total = self._explicit_anomalies.total_seen + max(0, len(anomalies) - min(len(anomalies), self._explicit_anomalies.total_seen))
        isolated = self._isolated_files.render(self.limits.max_items_per_section)

        directory_section = self._render_directories()
        directory_count = self._declared_directory_count if self._declared_directory_count is not None else directory_section["directory_count"]
        return {
            "schema_version": SCHEMA_VERSION,
            "package": {
                "root": self.root,
                "file_count": file_count,
                "total_bytes": total_bytes,
                "directory_count": directory_count,
                "max_depth": self._max_depth,
            },
            "directories": directory_section,
            "formats": formats,
            "document_types": document_types,
            "languages": languages,
            "timeline": {
                "file_modified": self._render_timeline(self._file_modified, file_count - self._file_modified.total_count),
                "document_dates": self._render_timeline(self._document_dates, file_count - self._document_date_files),
            },
            "topics": topics,
            "entities": {
                "people": people,
                "organizations": organizations,
                "files_with_named_entities": self._entity_files,
                "files_without_named_entities": max(0, file_count - self._entity_files),
            },
            "file_relationships": self._relationships.render(self.limits.max_items_per_section),
            "duplicates": {
                "exact_groups": exact_groups,
                "near_duplicate_groups": near_groups,
                "sample_candidate_groups": sample_groups,
            },
            "outliers": {
                "anomalous_files": {
                    "items": anomalies,
                    "file_count": anomaly_total,
                    "truncated": anomaly_total > len(anomalies),
                    "omitted_count": max(0, anomaly_total - len(anomalies)),
                    "basis": ["explicit_package_signals", "file_size_zscore_greater_than_3"],
                },
                "isolated_files": {
                    "items": isolated,
                    "file_count": self._isolated_files.total_seen,
                    "truncated": self._isolated_files.total_seen > len(isolated),
                    "omitted_count": max(0, self._isolated_files.total_seen - len(isolated)),
                    "basis": "explicit_file_relationship_analysis",
                },
            },
            "representation": {
                "data_subject": "package_contents_only",
                "high_cardinality_sections_are_bounded": True,
                "max_items_per_section": self.limits.max_items_per_section,
                "max_representative_files": self.limits.max_representative_files,
            },
        }


def build_package_overview(*, scan=None, files=None, documents=None, analysis=None, limits=None):
    """Build a stable overview from iterables without hydrating full text.

    When a physical ``scan``/``files`` inventory is supplied, documents enrich
    those files and do not count them again.  Without a physical inventory,
    each document's ``source`` metadata becomes the physical inventory.
    Callers must provide each physical path and each projected document at most
    once; scanner trees and ``unified_documents`` already guarantee this.
    """

    aggregator = PackageOverviewAggregator(limits=limits)
    physical_supplied = scan is not None or files is not None
    if scan is not None:
        aggregator.ingest_scan(scan)
    if files is not None:
        for file_record in files:
            aggregator.ingest_file(file_record)
    if documents is not None:
        aggregator.ingest_documents(documents, include_physical=not physical_supplied)
    if analysis is not None:
        aggregator.ingest_analysis(analysis)
    return aggregator.finalize()


def build_package_overview_from_storage(storage, scan_id, *, limits=None, batch_size=200):
    """Integration adapter for the current ``Storage`` API.

    ``hydrate=False`` is a required invariant: only the bounded projection is
    consumed even when a document's full text lives inline rather than in a
    sidecar.  The database cursor fetches bounded batches.
    """

    # Default overviews are immutable snapshots between storage mutations.
    # Read them before hydrating the scan tree: on a very large package even
    # deserialising the inventory is expensive.  Storage mutation methods
    # invalidate this snapshot transactionally.  Custom limits are request-
    # specific and therefore deliberately bypass the shared snapshot.
    if limits is None and hasattr(storage, "get_package_overview"):
        cached = storage.get_package_overview(scan_id)
        if cached and cached.get("schema_version") == SCHEMA_VERSION:
            return cached

    scan = storage.get_scan(scan_id)
    if scan is None:
        raise KeyError("unknown scan_id: {}".format(scan_id))
    documents = storage.iter_documents(scan_id, hydrate=False, batch_size=batch_size)
    analysis = storage.get_analysis(scan_id)
    content_map = storage.get_content_map(scan_id) if hasattr(storage, "get_content_map") else None
    if content_map:
        analysis = dict(analysis or {})
        analysis.setdefault("relationships", content_map.get("relationships") or [])
        analysis.setdefault("isolated_files", content_map.get("isolated_paths") or [])
        analysis.setdefault("sample_duplicate_candidates", content_map.get("duplicates") or [])
        analysis.setdefault("entities", content_map.get("entities") or {})
        if not analysis.get("topic_clusters"):
            analysis["topic_clusters"] = [
                {
                    "topic": item.get("name"),
                    "file_count": item.get("file_count"),
                    "representative_documents": item.get("representative_paths") or [],
                }
                for item in content_map.get("topics") or []
            ]
    overview = build_package_overview(
        scan=scan, documents=documents, analysis=analysis, limits=limits,
    )
    if limits is None and hasattr(storage, "save_package_overview"):
        storage.save_package_overview(scan_id, overview)
    return overview
