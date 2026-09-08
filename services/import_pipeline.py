"""Deterministic contracts for the staged import workflow."""
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

PREVIEW_LEVEL = "preview"
DEEP_LEVEL = "deep"
EVIDENCE_LEVEL = "evidence"

def parse_plan(file_node: Mapping[str, Any], preview: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Choose an auditable parser plan before expensive content I/O."""
    node = dict(file_node or {})
    preview = dict(preview or {})
    ext = str(node.get("extension") or Path(str(node.get("path") or "")).suffix).lower()
    size = max(0, int(node.get("size") or 0))
    text_ext = {".txt", ".md", ".markdown", ".html", ".htm", ".xml", ".csv", ".tsv", ".json", ".jsonl"}
    table_ext = {".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".ods"}
    image_ext = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
    archive_ext = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".rar", ".7z"}
    kind, parser, needs_ocr, needs_docling = "unknown", "metadata_only", False, False
    if ext in image_ext:
        kind, parser, needs_ocr = "image", "ocr", True
    elif ext == ".pdf":
        kind = "pdf"
        chars = int(preview.get("preview_characters") or len(str(preview.get("preview_text") or "")))
        image_pages = int(preview.get("image_pages") or preview.get("picture_count") or 0)
        pages = int(preview.get("page_count") or 0)
        needs_ocr = chars < 80 or bool(pages and image_pages / float(pages) >= 0.35)
        needs_docling = needs_ocr or bool(preview.get("table_count"))
        parser = "ocr_docling" if needs_ocr else "pdf_text"
    elif ext in table_ext:
        kind, parser = "table", "structured"
    elif ext in text_ext:
        kind, parser = "text", "text_full"
    elif ext in {".doc", ".docx", ".ppt", ".pptx", ".odt", ".rtf"}:
        kind, parser = "office", "document_structure"
    elif ext in archive_ext:
        kind, parser = "archive", "archive_members"
    return {"schema_version": "parse-plan/1.0", "kind": kind, "parser": parser, "size": size, "needs_ocr": needs_ocr, "needs_docling": needs_docling, "requires_full_text": kind in {"text", "pdf", "office"}, "requires_structure": kind in {"table", "office", "archive"}, "estimated_cost": "high" if needs_ocr or size >= 256 * 1024 * 1024 else ("medium" if size >= 16 * 1024 * 1024 else "low"), "selection_required": True}

def coverage_contract(level: str, **extra: Any) -> Dict[str, Any]:
    level = str(level or PREVIEW_LEVEL).lower()
    if level not in {PREVIEW_LEVEL, DEEP_LEVEL, EVIDENCE_LEVEL}: level = PREVIEW_LEVEL
    result = {"level": level, "preview_only": level == PREVIEW_LEVEL, "deep_parse_complete": level in {DEEP_LEVEL, EVIDENCE_LEVEL}, "formal_evidence_ready": level == EVIDENCE_LEVEL}
    result.update(extra)
    return result

def evidence_is_formal(item: Mapping[str, Any]) -> bool:
    if bool(item.get("preview_only")): return False
    coverage = item.get("coverage") or {}
    return bool(item.get("formal_evidence_ready") or coverage.get("formal_evidence_ready") or item.get("evidence_level") in {DEEP_LEVEL, EVIDENCE_LEVEL})


def build_preview_directory(previews):
    """Build a deterministic, explicitly provisional directory from previews."""
    groups = {}
    for item in previews or []:
        path = str(item.get("path") or "")
        if not path: continue
        keywords = list(item.get("keywords") or [])
        name = str(keywords[0] if keywords else (item.get("document_type") or "未分类资料"))[:120]
        groups.setdefault(name, []).append(path)
    topics = []
    for index, (name, paths) in enumerate(sorted(groups.items(), key=lambda x: (-len(x[1]), x[0]))):
        topics.append({"node_id": "preview-topic-%04d" % (index + 1), "name": name, "summary": "基于轻量预览的候选主题，尚未完成全文验证。", "member_paths": sorted(set(paths)), "file_count": len(set(paths)), "confidence": "preview", "evidence_status": "preview_candidate"})
    return {"schema_version": "preview-directory/1.0", "status": "provisional", "label": "初步智能目录（基于轻量预览）", "formal": False, "topics": topics}


# Unified analysis levels.  Preview data is intentionally never formal evidence.
ANALYSIS_LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")
LEVEL_NAMES = {
    "L0": "inventory", "L1": "preview", "L2": "structured",
    "L3": "rich_parse", "L4": "evidence_ready", "L5": "semantic_ready",
}

def normalize_analysis_level(value: Any) -> str:
    value = str(value or "L0").upper()
    if value in ANALYSIS_LEVELS:
        return value
    aliases = {"INVENTORY":"L0", "PREVIEW":"L1", "DEEP":"L3", "EVIDENCE":"L4", "SEMANTIC":"L5"}
    return aliases.get(value, "L0")

def content_blocks(text: str, metadata: Optional[Mapping[str, Any]] = None, chunk_size: int = 1400, overlap: int = 160):
    """Create deterministic, source-addressable blocks for model context."""
    meta = dict(metadata or {})
    value = str(text or "")
    chunk_size = max(200, min(12000, int(chunk_size or 1400)))
    overlap = max(0, min(chunk_size // 3, int(overlap or 160)))
    blocks = []
    start = 0
    index = 0
    while start < len(value):
        end = min(len(value), start + chunk_size)
        piece = value[start:end]
        blocks.append({
            "block_id": "%s:block-%06d" % (meta.get("file_id") or meta.get("path") or "document", index),
            "file_id": meta.get("file_id"), "file_version": meta.get("file_version", 1),
            "block_index": index, "block_type": meta.get("block_type", "text"),
            "source_text": piece, "text": piece,
            "page": meta.get("page"), "section": meta.get("section"),
            "paragraph_index": meta.get("paragraph_index"),
            "char_start": start, "char_end": end,
            "bbox": meta.get("bbox"), "table_id": meta.get("table_id"),
            "sheet": meta.get("sheet"), "row_index": meta.get("row_index"),
            "column_name": meta.get("column_name"), "unit": meta.get("unit"),
            "ocr_confidence": meta.get("ocr_confidence"),
            "source_path": meta.get("source_path") or meta.get("path"),
            "source_sha256": meta.get("source_sha256"),
            "analysis_level": normalize_analysis_level(meta.get("analysis_level", "L2")),
        })
        if end >= len(value):
            break
        start = end - overlap
        index += 1
    return blocks

def expand_context(blocks, hit_index: int, radius: int = 1):
    """Return hit block plus neighbouring blocks without losing provenance."""
    rows = list(blocks or [])
    if not rows:
        return []
    i = max(0, min(len(rows) - 1, int(hit_index)))
    r = max(0, min(8, int(radius or 1)))
    return rows[max(0, i-r):min(len(rows), i+r+1)]
