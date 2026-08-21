import json
import logging
import os
import re
import zipfile
import hashlib
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path

from services.scanner import IGNORED_DIRS, should_ignore_file
from services.evidence import select_evidence
from services.unified_parser import SUPPORTED_EXTENSIONS


LOGGER = logging.getLogger(__name__)


def xml_safe_text(value):
    """Return text that is safe for XML 1.0 / python-docx.

    Parsed files can legally contain NUL bytes, terminal control characters,
    Unicode noncharacters, or even isolated UTF-16 surrogate code points.  XML
    1.0 cannot represent those values and lxml raises before the report can be
    saved.  Keep normal text plus tab/newline/carriage return, and filter only
    characters that are unsafe for the document package.
    """
    if value is None:
        return ""
    text = str(value)
    safe = []
    for character in text:
        codepoint = ord(character)
        if codepoint in (0x09, 0x0A, 0x0D):
            safe.append(character)
            continue
        if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            continue
        if 0xD800 <= codepoint <= 0xDFFF:
            continue
        if codepoint > 0x10FFFF:
            continue
        # Unicode noncharacters cannot carry meaningful source text and are
        # rejected by several XML consumers even where a parser is permissive.
        if 0xFDD0 <= codepoint <= 0xFDEF:
            continue
        if (codepoint & 0xFFFF) in (0xFFFE, 0xFFFF):
            continue
        safe.append(character)
    return "".join(safe)


def safe_name(value):
    return re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", value, flags=re.UNICODE).strip("_") or "export"


def collect_files(path):
    path = Path(path)
    if path.is_symlink():
        return []
    if path.is_file():
        return [path]
    files = []
    for current_root, dirs, names in os.walk(str(path)):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not (Path(current_root) / d).is_symlink()]
        for name in names:
            if should_ignore_file(name):
                continue
            candidate = Path(current_root) / name
            if candidate.is_symlink():
                continue
            files.append(candidate)
    return files


def _sha256_file(path, block_size=1024 * 1024):
    """Return the raw-source digest used for handoff-package deduplication."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _deduplicate_files(files, root):
    """Keep one stable canonical source per identical byte stream.

    UI selections are path based, but a handoff package must not contain two
    distinct names for exactly the same source bytes.  The manifest retains all
    omitted paths, so no provenance is lost.
    """
    groups = {}
    for path in sorted(set(files), key=lambda item: str(item.relative_to(root)).replace("\\", "/")):
        digest = _sha256_file(path)
        groups.setdefault(digest, []).append(path)

    unique_files = []
    duplicates = []
    for digest, members in groups.items():
        canonical = members[0]
        unique_files.append(canonical)
        if len(members) > 1:
            duplicates.append({
                "sha256": digest,
                "canonical": str(canonical.relative_to(root)).replace("\\", "/"),
                "omitted": [str(item.relative_to(root)).replace("\\", "/") for item in members[1:]],
            })
    return unique_files, duplicates


@contextmanager
def _atomic_zip(temporary_path, final_path):
    """Write a ZIP privately and publish it only after it closes cleanly."""
    temporary_path = Path(temporary_path)
    final_path = Path(final_path)
    temporary_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(str(temporary_path), "w", compression=zipfile.ZIP_DEFLATED,
                             allowZip64=True) as archive:
            yield archive
        # Ensure the directory entry is durable before publishing the name.
        # A crash can therefore leave only a harmless .part file, never a
        # half-valid final archive.
        try:
            with temporary_path.open("rb") as stream:
                os.fsync(stream.fileno())
        except (OSError, AttributeError):
            pass
        os.replace(str(temporary_path), str(final_path))
    except BaseException:
        with suppress(OSError):
            temporary_path.unlink()
        raise


def cleanup_stale_part_files(output_dir, max_age_seconds=24 * 60 * 60):
    """Remove abandoned exporter work files, never published archives.

    Only files directly under the configured output directory with the exact
    ``*.zip.part`` suffix are considered, and recent files are preserved in
    case another process is still writing them.  This makes recovery after a
    worker kill deterministic without risking a valid ``.zip`` result.
    """
    output_dir = Path(output_dir)
    if not output_dir.exists() or not output_dir.is_dir() or output_dir.is_symlink():
        return 0
    now = time.time()
    removed = 0
    try:
        candidates = output_dir.glob("*.zip.part")
        for item in candidates:
            try:
                if not item.is_file() or item.is_symlink():
                    continue
                age = max(0.0, now - item.stat().st_mtime)
                if age < max(60, int(max_age_seconds)):
                    continue
                item.unlink()
                removed += 1
            except OSError:
                LOGGER.warning("清理导出临时文件失败：%s", item, exc_info=True)
    except OSError:
        LOGGER.warning("扫描导出临时文件失败：%s", output_dir, exc_info=True)
    return removed


def export_node(root, selected, summary, output_dir, max_bytes, analysis=None, documents=None, task_topic=None,
                member_paths=None, node_name=None, node_id=None, selection_metadata=None,
                selected_evidence_ids=None, inventory_metadata=None, file_states=None,
                progress_callback=None, cancel_check=None):
    root = Path(root).resolve()
    selected = Path(selected).resolve()
    documents = documents or []
    analysis = analysis or {}
    selection_metadata = selection_metadata or []
    selected_evidence_ids = set(selected_evidence_ids or [])
    inventory_metadata = inventory_metadata or {}
    file_states = file_states or {}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_part_files(output_dir)
    virtual_paths = sorted(set(member_paths or []))
    if virtual_paths:
        selected_rel = "virtual:{}".format(node_id or safe_name(node_name or "topic"))
        files = []
        excluded_files = []
        for relative_path in virtual_paths:
            candidate = (root / relative_path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                raise ValueError("主题节点包含越界文件，已拒绝导出")
            if not candidate.exists() or candidate.is_symlink():
                excluded_files.append(candidate)
            else:
                # Handoff packages preserve every selected source file. Parsing
                # support controls analysis coverage, never raw-material export.
                files.append(candidate)
        selected_documents = [item for item in documents if item.get("path") in set(virtual_paths)]
        export_label = node_name or "主题节点"
    else:
        all_files = collect_files(selected)
        # Export all original files, including formats the parser cannot read.
        # Unsupported files remain visible in the coverage manifest as metadata-only.
        files = all_files
        excluded_files = []
        selected_rel = str(selected.relative_to(root)).replace("\\", "/") if selected != root else "."
        if selected_rel == ".":
            selected_documents = documents
        else:
            prefix = selected_rel.rstrip("/") + "/"
            selected_documents = [item for item in documents if item.get("path") == selected_rel or item.get("path", "").startswith(prefix)]
        export_label = selected.name
    selected_file_count = len(files)
    try:
        files, content_duplicates = _deduplicate_files(files, root)
    except (OSError, PermissionError) as exc:
        raise ValueError("导出前无法计算源文件去重指纹：{}".format(exc))
    source_sizes = {}
    try:
        for path in files:
            source_sizes[path] = path.stat().st_size
    except (OSError, PermissionError) as exc:
        raise ValueError("导出前无法读取源文件：{}".format(exc))
    total_size = sum(source_sizes.values())
    if total_size > max_bytes:
        raise ValueError("导出内容为 {:.1f} MB，超过演示版上限 {:.1f} MB".format(total_size / 1048576, max_bytes / 1048576))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # A random suffix prevents two same-second exports from sharing the same
    # final or .part pathname.  The human-readable prefix remains unchanged.
    archive_name = "待整编数据包_{}_{}_{}.zip".format(
        safe_name(export_label), stamp, uuid.uuid4().hex[:8]
    )
    archive_path = Path(output_dir) / archive_name
    temporary_archive_path = archive_path.with_name(archive_path.name + ".part")
    evidence_candidates = []
    for item in selected_documents:
        evidence_candidates.extend(item.get("payload", {}).get("evidence", []))
    if selected_evidence_ids:
        evidence_candidates = [
            item for item in evidence_candidates
            if item.get("evidence_id") in selected_evidence_ids
        ]
    summary_topics = list((summary or {}).get("topics", []))
    direction = (summary or {}).get("recommended_research_direction", {})
    summary_topics.extend([direction.get("title"), direction.get("rationale")])
    evidence_chain = select_evidence(
        evidence_candidates, topics=summary_topics, max_items=40, per_source=2, max_chars=600
    )
    task_topic = str(task_topic or "").strip() or None
    if not task_topic:
        raise ValueError("导出前必须指定整编任务主题")
    handoff = {
        "schema_version": "compilation-task/2.0",
        "selected_path": selected_rel,
        "selected_node_name": node_name,
        "selected_node_id": node_id,
        "task_topic": task_topic,
        "task_topic_required": True,
        "task_topic_status": "provided" if task_topic else "customer_input_required",
        "instruction": (
            "整编 Agent 必须围绕 task_topic 完成写作；若其为 null，必须先要求客户指定主题，"
            "不得把节点摘要或推荐研究方向自动当作客户任务主题。"
        ),
        "selected_node_summary": (summary or {}).get("summary"),
        "recommended_research_direction": (summary or {}).get("recommended_research_direction", {}),
        "analysis_coverage": analysis.get("coverage", {}),
        "value_judgment": analysis.get("value_judgment", {}),
        "evidence_file": "结论-证据链.json",
        "coverage_file": "解析覆盖率清单.json",
        "selection_mode": "combined" if len(selection_metadata) > 1 else "single",
        "selected_nodes": selection_metadata,
        "unique_source_file_count": len(files),
        "deduplication": {
            "method": "先按相对路径合并重叠选择，再按 SHA-256 精确去重；相同字节的源文件仅导出规范副本。",
            "source_selection_count": len(selection_metadata),
            "selected_evidence_count": len(selected_evidence_ids),
            "selected_file_count_before_content_deduplication": selected_file_count,
            "content_duplicate_group_count": len(content_duplicates),
            "content_duplicate_file_count": sum(len(item["omitted"]) for item in content_duplicates),
            "content_duplicate_groups": content_duplicates,
        },
    }
    written_size = 0
    with _atomic_zip(temporary_archive_path, archive_path) as archive:
        for index, path in enumerate(files, 1):
            if cancel_check:
                cancel_check()
            expected_size = source_sizes[path]
            try:
                current_size = path.stat().st_size
            except (OSError, PermissionError) as exc:
                raise ValueError("导出过程中源文件不可访问：{}".format(exc))
            if current_size != expected_size:
                raise ValueError("源文件在导出过程中发生变化：{}".format(path.name))
            if written_size + current_size > max_bytes:
                raise ValueError("源文件总大小超过导出上限，已停止写入")
            archive.write(str(path), arcname=str(path.relative_to(root)).replace("\\", "/"))
            written_size += current_size
            if progress_callback:
                progress_callback(index, len(files), written_size, total_size)
        archive.writestr(
            "节点摘要.json",
            json.dumps(summary or {"message": "尚未生成摘要"}, ensure_ascii=False, indent=2),
        )
        archive.writestr("整编任务说明.json", json.dumps(handoff, ensure_ascii=False, indent=2))
        archive.writestr("结论-证据链.json", json.dumps({
            "schema_version": "question-answer-evidence/2.0",
            "selected_path": selected_rel,
            "selected_node_name": node_name,
            "conclusions": (summary or {}).get("conclusion_evidence", []),
            "question_answer_evidence": (summary or {}).get("question_answer_evidence") or (summary or {}).get("conclusion_evidence", []),
            "claims": (summary or {}).get("claims", []),
            "evidence_status": (summary or {}).get("evidence_status") or ("supported" if evidence_chain else "insufficient"),
            "evidence_count": len(evidence_chain),
            "candidate_count": len(evidence_candidates),
            "omitted_count": max(0, len(evidence_candidates) - len(evidence_chain)),
            "selection_method": "主题相关度 + 页/节可追溯性 + 每文件最多 2 条",
            "topic_terms": summary_topics[:12],
            "items": evidence_chain,
        }, ensure_ascii=False, indent=2))
        archive.writestr("统一文档索引.json", json.dumps([
            {
                "path": item.get("path"),
                "schema_version": item.get("payload", {}).get("schema_version"),
                "source": item.get("payload", {}).get("source"),
                "parser": item.get("payload", {}).get("parser"),
                "structure": item.get("payload", {}).get("structure"),
                "content_sha256": item.get("payload", {}).get("content_sha256"),
                "warnings": item.get("payload", {}).get("warnings", []),
            } for item in selected_documents
        ], ensure_ascii=False, indent=2))
        archive.writestr("去重与聚类清单.json", json.dumps({
            "statistics": analysis.get("statistics", {}),
            "exact_duplicate_groups": analysis.get("exact_duplicate_groups", []),
            "similar_document_clusters": analysis.get("similar_document_clusters", []),
            "topic_clusters": analysis.get("topic_clusters", []),
            "classification_dimensions": analysis.get("classification_dimensions", []),
        }, ensure_ascii=False, indent=2))
        archive.writestr("检索证据.json", json.dumps({
            "schema_version": "local-retrieval/1.0",
            "selected_path": selected_rel,
            "retrieval": analysis.get("retrieval", {}),
            "research_retrieval": analysis.get("research_retrieval", {}),
        }, ensure_ascii=False, indent=2))
        document_by_path = {item.get("path"): item.get("payload", {}) for item in selected_documents}
        coverage_items = []
        for relative_path in sorted(set(virtual_paths) if virtual_paths else {
            str(path.relative_to(root)).replace("\\", "/") for path in files
        }):
            payload = document_by_path.get(relative_path, {})
            meta = inventory_metadata.get(relative_path, {})
            state = file_states.get(relative_path, {})
            coverage_items.append({
                "path": relative_path,
                "source": payload.get("source") or meta,
                "analysis_status": state.get("status") or ("completed" if payload else "not_parsed"),
                "parser": payload.get("parser"),
                "structure": payload.get("structure"),
                "stored_characters": len(payload.get("text", "")),
                "evidence_count": len(payload.get("evidence", [])),
                "coverage": payload.get("coverage", {}),
                "warnings": payload.get("warnings", []),
            })
        archive.writestr("解析覆盖率清单.json", json.dumps({
            "schema_version": "parse-coverage/1.0",
            "selected_path": selected_rel,
            "file_count": len(coverage_items),
            "parsed_file_count": sum(1 for item in coverage_items if item["analysis_status"] in {"completed", "overview"}),
            "coverage": (analysis.get("coverage") or {}),
            "value_judgment": (analysis.get("value_judgment") or {}),
            "analysis_limitations": list((analysis.get("coverage") or {}).get("limitations") or []),
            "items": coverage_items,
        }, ensure_ascii=False, indent=2))
        archive.writestr(
            "导出清单.json",
            json.dumps({
                "selected_path": selected_rel,
                "file_count": len(files),
                "selected_file_count_before_content_deduplication": selected_file_count,
                "unique_source_file_count": len(files),
                "content_duplicate_group_count": len(content_duplicates),
                "content_duplicate_file_count": sum(len(item["omitted"]) for item in content_duplicates),
                "content_duplicate_groups": content_duplicates,
                "selected_node_count": len(selection_metadata),
                "excluded_file_count": len(excluded_files),
                "excluded_files": [str(path.relative_to(root)).replace("\\", "/") for path in excluded_files[:100]],
                "total_size": total_size,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "contents": ["所选范围全部原始文件（相对路径和 SHA-256 精确去重）", "节点摘要.json", "整编任务说明.json", "结论-证据链.json", "统一文档索引.json", "去重与聚类清单.json", "检索证据.json", "解析覆盖率清单.json"],
            }, ensure_ascii=False, indent=2),
        )
    return archive_path


def create_report_docx(report, scan, output_path):
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Inches, Pt, RGBColor
    except ImportError:
        raise RuntimeError("未安装 python-docx，无法生成 Word 报告")

    def add_paragraph(text="", style=None):
        return doc.add_paragraph(xml_safe_text(text), style=style)

    def add_heading(text, level):
        return doc.add_heading(xml_safe_text(text), level=level)

    def add_run(paragraph, text=""):
        return paragraph.add_run(xml_safe_text(text))

    def set_xml_text(element, value):
        element.text = xml_safe_text(value)

    def set_font(run, name="Microsoft YaHei", size=None, color=None, bold=None):
        run.font.name = name
        run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)
        run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), "Calibri")
        run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), "Calibri")
        if size is not None:
            run.font.size = Pt(size)
        if color is not None:
            run.font.color.rgb = RGBColor(*color)
        if bold is not None:
            run.bold = bold

    def clean_evidence(value, limit=220):
        text = re.sub(r"\s+", " ", xml_safe_text(value)).replace("|", " / ").strip(" /-")
        return text[:limit] + ("…" if len(text) > limit else "")

    def configure_style(style, size, color, before, after, line_spacing, bold=False):
        style.font.name = "Microsoft YaHei"
        style._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(*color)
        style.font.bold = bold
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = line_spacing

    def add_items(values, style="List Bullet", empty_text="暂无可用信息"):
        values = values or []
        if not values:
            add_paragraph(empty_text)
            return
        for value in values:
            add_paragraph(value, style=style)

    def add_conclusion_evidence(values):
        for conclusion in values or []:
            if not isinstance(conclusion, dict):
                continue
            statement = conclusion.get("statement") or "分析结论"
            confidence = conclusion.get("confidence") or "待核验"
            add_paragraph("结论：{}（置信度：{}）".format(statement, confidence), style="List Bullet")
            if conclusion.get("basis"):
                add_paragraph("依据：{}".format(conclusion["basis"]))
            for evidence in conclusion.get("evidence", [])[:4]:
                if not isinstance(evidence, dict):
                    continue
                location = evidence.get("source_path", "未知文件")
                if evidence.get("page"):
                    location += "，第 {} 页".format(evidence["page"])
                add_paragraph(
                    "[{}] {}：{}".format(
                        evidence.get("evidence_id") or "证据",
                        location,
                        clean_evidence(evidence.get("text")),
                    ),
                    style="List Bullet",
                )

    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)

    styles = doc.styles
    configure_style(styles["Normal"], 11, (0, 0, 0), 0, 6, 1.10)
    configure_style(styles["Heading 1"], 16, (46, 116, 181), 16, 8, 1.10, True)
    configure_style(styles["Heading 2"], 13, (46, 116, 181), 12, 6, 1.10, True)
    configure_style(styles["Heading 3"], 12, (31, 77, 120), 8, 4, 1.10, True)
    configure_style(styles["List Bullet"], 11, (0, 0, 0), 0, 8, 1.167)
    configure_style(styles["List Number"], 11, (0, 0, 0), 0, 8, 1.167)

    header = section.header.paragraphs[0]
    set_xml_text(header, "数据分析 Agent｜数据包情况概览")
    set_font(header.runs[0], size=9, color=(100, 100, 100))
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer_run = add_run(footer, "第 ")
    set_font(footer_run, size=9, color=(100, 100, 100))
    field_begin = OxmlElement("w:fldChar")
    field_begin.set(qn("w:fldCharType"), "begin")
    field_instr = OxmlElement("w:instrText")
    field_instr.set(qn("xml:space"), "preserve")
    set_xml_text(field_instr, " PAGE ")
    field_end = OxmlElement("w:fldChar")
    field_end.set(qn("w:fldCharType"), "end")
    footer_run._r.extend([field_begin, field_instr, field_end])
    tail = add_run(footer, " 页")
    set_font(tail, size=9, color=(100, 100, 100))

    title = add_paragraph()
    title.paragraph_format.space_after = Pt(4)
    title_run = add_run(title, "数据包情况概览报告")
    set_font(title_run, size=23, bold=True)
    subtitle = add_paragraph("自动扫描、证据摘要与研究方向建议")
    subtitle.paragraph_format.space_after = Pt(16)
    set_font(subtitle.runs[0], size=13, color=(70, 70, 70))

    for label, value in (
        ("扫描目录", scan["root"]),
        ("统计范围", "递归文件 {} 个；子目录 {} 个；总大小 {}".format(scan["file_count"], scan.get("directory_count", 0), scan["total_size_human"])),
        ("扫描时间", scan["scanned_at"]),
        ("生成模式", "模型分析（解析与证据链仍为本地）" if report.get("generation_mode") == "model_analyzed" else "本地解析完成，研究方向待模型分析"),
    ):
        paragraph = add_paragraph()
        paragraph.paragraph_format.space_after = Pt(2)
        label_run = add_run(paragraph, label + "：")
        set_font(label_run, size=10.5, bold=True)
        value_run = add_run(paragraph, value)
        set_font(value_run, size=10.5)

    add_heading("一、数据包基本信息", level=1)
    add_items(report.get("basic_information"))
    coverage = report.get("coverage") or {}
    if coverage:
        add_heading("覆盖等级与限制", level=2)
        add_paragraph(
            "{}（{}）：已解析 {}/{} 个文件；抽样 {} 个；待处理 {} 个；失败 {} 个。".format(
                coverage.get("coverage_level_label", "覆盖等级未标注"),
                coverage.get("status", "待分析"),
                coverage.get("parsed_files", 0),
                coverage.get("inventory_files", 0),
                coverage.get("sampled_files", 0),
                coverage.get("pending_files", 0),
                coverage.get("failed_files", 0),
            )
        )
        archive_totals = coverage.get("archive_member_totals") or {}
        if archive_totals.get("total_members"):
            add_paragraph(
                "压缩包成员：已解析 {}/{}；跳过 {}；失败 {}。".format(
                    archive_totals.get("parsed_members", 0), archive_totals.get("total_members", 0),
                    archive_totals.get("skipped_members", 0), archive_totals.get("failed_members", 0),
                )
            )
        add_items(coverage.get("limitations"), empty_text="当前未发现覆盖限制。")
    judgment = report.get("value_judgment") or {}
    if judgment:
        add_heading("数据价值判断", level=2)
        labels = (
            ("data_usability", "数据可用性"),
            ("information_richness", "信息丰富度"),
            ("research_potential", "研究潜力"),
            ("task_relevance", "与客户任务的相关性"),
        )
        for key, label in labels:
            item = judgment.get(key) or {}
            add_paragraph(
                "{}：{}{}。依据：{}".format(
                    label,
                    item.get("level", "未评估"),
                    "（{}分）".format(item["score"]) if item.get("score") is not None else "",
                    item.get("basis", "未提供"),
                ),
                style="List Bullet",
            )
        add_paragraph(
            "规范文档 {} 份；重复副本 {} 份；有效正文证据 {} 条。".format(
                judgment.get("canonical_document_count", 0), judgment.get("duplicate_alias_count", 0),
                judgment.get("valid_evidence_count", 0),
            )
        )
        add_items(judgment.get("limitations"), empty_text="当前没有额外价值判断限制。")
    add_heading("二、全局分类", level=1)
    categories = report.get("global_categories") or []
    classification_coverage = report.get("classification_coverage", {})
    if classification_coverage:
        source_label = {
            "adaptive_analysis_tree": "自适应内容分类树",
            "physical_directory_fallback": "原始目录树（自适应分析不可用时的降级结果）",
            "root_fallback": "扫描根节点（未形成可用分类）",
        }.get(classification_coverage.get("source"), "本地分类结果")
        add_paragraph(
            "分类来源：{}；顶层类别 {} 个；已归入 {} / {} 个已解析文件（{}）。".format(
                source_label,
                classification_coverage.get("top_level_category_count", 0),
                classification_coverage.get("classified_file_count", 0),
                classification_coverage.get("parsed_file_count", 0),
                "完整" if classification_coverage.get("complete") else "需复核未归类或重复归类文件",
            )
        )
    if not categories:
        add_paragraph("暂无可用分类。")
    for item in categories:
        if isinstance(item, dict):
            add_heading(
                "{}（{}：{} 个文件）".format(
                    item.get("name", "未命名分类"), item.get("dimension", "内容类别"), item.get("file_count", 0)
                ),
                level=2,
            )
            add_paragraph(item.get("description") or "暂无分类说明。")
            type_counts = item.get("type_counts", {})
            if type_counts:
                add_paragraph("格式构成：{}。".format(
                    "；".join("{} {}个".format(extension, count) for extension, count in type_counts.items())
                ))
            if item.get("topics"):
                add_paragraph("内容线索：{}。".format("、".join(item["topics"][:12])))
            if item.get("representative_documents"):
                add_paragraph("代表文档：{}".format("；".join(item["representative_documents"][:5])))
            if item.get("conclusion_evidence"):
                add_paragraph("关键结论—证据链：")
                add_conclusion_evidence(item["conclusion_evidence"])
            for subcategory in item.get("subcategories", []):
                if not isinstance(subcategory, dict):
                    continue
                add_heading(
                    "{}（{}：{} 个文件）".format(
                        subcategory.get("name", "未命名主题"),
                        subcategory.get("dimension", "内容主题"),
                        subcategory.get("file_count", 0),
                    ),
                    level=3,
                )
                add_paragraph(subcategory.get("description") or "该主题下的文件已完成统一解析。")
                if subcategory.get("topics"):
                    add_paragraph("内容线索：{}。".format("、".join(subcategory["topics"][:10])))
                if subcategory.get("representative_documents"):
                    add_paragraph("代表文档：{}".format("；".join(subcategory["representative_documents"][:3])))
                if subcategory.get("conclusion_evidence"):
                    add_paragraph("关键结论—证据链：")
                    add_conclusion_evidence(subcategory["conclusion_evidence"])
            category_evidence = item.get("evidence_chain", [])
            if category_evidence:
                add_paragraph("分类依据与可回查证据：")
                for evidence in category_evidence[:4]:
                    if not isinstance(evidence, dict):
                        continue
                    location = evidence.get("source_path", "未知文件")
                    if evidence.get("page"):
                        location += "，第 {} 页".format(evidence["page"])
                    if evidence.get("section"):
                        location += "，{}".format(evidence["section"])
                    add_paragraph(
                        "[{}] {}：{}".format(
                            evidence.get("evidence_id") or "元数据证据",
                            location,
                            clean_evidence(evidence.get("text") or evidence.get("fact")),
                        ),
                        style="List Bullet",
                    )
        else:
            add_paragraph(item, style="List Bullet")
    add_heading("三、关键发现", level=1)
    add_items(report.get("key_findings"))

    add_heading("四、推荐研究方向", level=1)
    recommendation = report.get("recommended_research_direction") or {}
    add_heading(recommendation.get("title") or "待进一步确定研究方向", level=2)
    lead = add_paragraph()
    lead_run = add_run(lead, "性质：推论；优先级：{}；置信度：{}".format(recommendation.get("priority", "中"), recommendation.get("confidence", "中")))
    set_font(lead_run, size=11, color=(31, 58, 95), bold=True)
    add_paragraph(recommendation.get("rationale") or "需要先生成代表性文档的全文摘要，再确定研究重点。")
    add_heading("拟研究问题", level=3)
    add_items(recommendation.get("research_questions") or recommendation.get("questions"), style="List Number")
    add_heading("建议方法", level=3)
    add_items(recommendation.get("methods"), empty_text="建议采用分层抽样、证据矩阵和专题复核方法。")
    add_heading("方向依据与证据", level=3)
    recommendation_evidence = recommendation.get("evidence_chain", [])
    if recommendation_evidence:
        for evidence in recommendation_evidence[:12]:
            if isinstance(evidence, dict):
                location = evidence.get("source_path", "未知文件")
                if evidence.get("page"):
                    location += "，第 {} 页".format(evidence["page"])
                if evidence.get("section"):
                    location += "，{}".format(evidence["section"])
                add_paragraph("[{}] {}：{}".format(evidence.get("evidence_id") or "元数据证据", location, clean_evidence(evidence.get("text"))), style="List Bullet")
    else:
        add_paragraph("当前未形成可引用正文证据，方向置信度应下调并优先人工复核。")

    add_heading("五、其他深入方向建议", level=1)
    for item in report.get("directions", []):
        if isinstance(item, dict):
            add_heading(item.get("direction", "建议"), level=2)
            add_paragraph("类型：{}；置信度：{}".format(item.get("type", "推论"), item.get("confidence", "未标注")))
            for evidence in item.get("evidence_chain", item.get("evidence", [])):
                if isinstance(evidence, dict):
                    add_paragraph("依据：[{}] {}{}".format(
                        evidence.get("evidence_id") or "证据",
                        evidence.get("source_path") or evidence.get("reason", ""),
                        "：" + clean_evidence(evidence.get("text") or evidence.get("fact", "")) if (evidence.get("text") or evidence.get("fact")) else "",
                    ), style="List Bullet")
            if item.get("confidence_note"):
                add_paragraph("置信度说明：{}".format(item["confidence_note"]))
        else:
            add_paragraph(item, style="List Bullet")
    if not report.get("directions"):
        add_paragraph("暂无其他深入方向建议。")
    add_heading("六、分析方法与边界", level=1)
    method = report.get("analysis_method", {})
    for key, label in (("parse", "统一解析"), ("deduplication", "精确去重"), ("similarity", "相似聚类"), ("retrieval", "本地证据检索"), ("classification", "自适应分类"), ("traceability", "证据回溯")):
        if method.get(key):
            add_paragraph("{}：{}".format(label, method[key]), style="List Bullet")
    add_paragraph("本报告是数据分析智能体自身的情况概览交付物，不等同于报告整编智能体生成的正式专题报告。模型仅用于语言与研究方向增强；文档解析、OCR、去重、聚类、建树和证据链均在本地执行。")
    doc.save(str(output_path))
