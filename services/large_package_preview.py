"""Bounded content portraits for the isolated large-package workflow only.

No ordinary storage, full-file hashing, LLM calls or model downloads. All
locations describe preview evidence, never a claim that a document was read fully.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
import zipfile
from collections import Counter
from email import policy
from email.parser import BytesParser
from pathlib import Path
from xml.etree import ElementTree as ET

SCHEMA = "large-content-portrait/1"
TEXT = {".txt", ".md", ".markdown", ".log", ".html", ".htm", ".xml", ".yaml", ".yml", ".ini", ".cfg"}
IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
BOILERPLATE = re.compile(r"免责声明|版权所有|目录$|保密声明|all rights reserved|disclaimer|confidential", re.I)
TOPICS = [
    ("销售与回款", "财务资料", ("销售额", "销售明细", "回款", "应收", "订单金额", "新客户数", "市场经营", "sales", "receivable", "revenue")),
    ("设备故障与维护", "项目与技术", ("故障", "报警", "检修", "设备编号", "维修", "fault", "alarm", "maintenance")),
    ("采购与供应商", "财务资料", ("采购", "供应商", "采购订单", "采购金额", "supplier", "procurement", "purchase order")),
    ("预算与成本", "财务资料", ("预算", "成本", "费用", "支出", "budget", "expense", "cost")),
    ("合同与履约", "合同制度", ("甲方", "乙方", "违约", "合同编号", "履约", "contract", "agreement")),
    ("人员与薪酬", "人员资料", ("员工", "薪酬", "工资", "考勤", "人员编号", "payroll", "employee", "salary")),
    ("项目进度与交付", "项目与技术", ("里程碑", "交付", "进度", "验收", "项目计划", "milestone", "deliverable", "acceptance")),
    ("质量与检测", "项目与技术", ("检测", "质检", "合格率", "不良率", "质量", "inspection", "defect", "quality")),
    ("库存与物流", "业务记录", ("库存", "出库", "入库", "运单", "物流", "inventory", "shipment", "warehouse")),
    ("系统运行与安全", "项目与技术", ("异常日志", "漏洞", "登录", "访问日志", "权限", "可信执行", "exception", "vulnerability", "authentication", "trusted execution", "security", "enclave")),
    ("政策与制度", "合同制度", ("管理办法", "实施细则", "规章", "制度", "regulation", "policy", "procedure")),
    ("科研与实验", "项目与技术", ("实验", "样本", "试验", "研究方法", "experiment", "hypothesis", "dataset")),
]


def _int(name, default, low, high):
    try:
        return max(low, min(high, int(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default


def settings(refine=False):
    return {
        "bytes": _int("SJFX_LP_PREVIEW_BYTES", 65536, 8192, 262144) * (3 if refine else 1),
        "seconds": _int("SJFX_LP_REFINE_SECONDS" if refine else "SJFX_LP_PREVIEW_SECONDS", 15 if refine else 4, 1, 30),
        "pages": 6 if refine else 3,
        "records": 40 if refine else 8,
        "ocr_pages": 2 if refine else 0,
    }


def clean(value, limit=1200):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))).strip()[:limit]


def decode(blob):
    if blob.startswith((b"\xff\xfe", b"\xfe\xff")):
        return blob.decode("utf-16", "replace")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return blob.decode(encoding)
        except UnicodeError:
            pass
    return blob.decode("utf-8", "replace")


class Budget:
    def __init__(self, options):
        self.limit = options["bytes"]
        self.used = 0
        self.deadline = time.monotonic() + options["seconds"]
        self.reasons = []

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("轻量解析时间预算已用尽")

    def read(self, stream, amount):
        self.check()
        granted = min(max(0, amount), self.limit - self.used)
        if granted <= 0:
            return b""
        data = stream.read(granted)
        self.used += len(data)
        return data


def _record(parts, location, text, kind="text"):
    value = clean(text, 1800)
    if value and len(parts) < 48:
        parts.append({"location": location, "text": value, "kind": kind, "preview_only": True})


def _windows(path, budget, count):
    size = path.stat().st_size
    n = min(count, max(1, size // 4096))
    width = max(1, min(size, (budget.limit - budget.used) // n))
    offsets = sorted(set(round(max(0, size - width) * i / max(1, n - 1)) for i in range(n)))
    with path.open("rb") as stream:
        for offset in offsets:
            stream.seek(offset)
            blob = budget.read(stream, width)
            if offset:
                # Partial lines are not represented as complete records.
                blob = blob.partition(b"\n")[2]
            if offset + width < size:
                blob = blob.rpartition(b"\n")[0]
            yield offset, blob


def _text(path, budget, options, parts, fields, headings):
    for offset, blob in _windows(path, budget, 5 if options["ocr_pages"] else 3):
        value = decode(blob)
        lines = [clean(line, 180) for line in value.splitlines() if clean(line)]
        useful = [line for line in lines if not BOILERPLATE.search(line)]
        for line in useful[:3]:
            if len(line) < 100:
                headings.append(line.lstrip("# "))
        _record(parts, "字节 {} 附近完整行".format(offset), "\n".join(useful) or value)


def _csv(path, budget, options, parts, fields, headings):
    header = []
    for offset, blob in _windows(path, budget, 5 if options["ocr_pages"] else 3):
        text = decode(blob)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
        if offset and '"' in text:
            # A random line boundary is not necessarily a CSV record boundary
            # when quoted fields contain newlines. Keep only the safe prefix.
            continue
        reader = csv.reader(io.StringIO(text), dialect, strict=True)
        if offset == 0:
            header = [clean(x, 100) for x in next(reader, [])][:40]
            fields.extend(header)
            _record(parts, "表头", " | ".join(header), "fields")
        kept = 0
        while True:
            try:
                row = next(reader)
            except (StopIteration, csv.Error):
                break
            budget.check()
            if header and len(row) != len(header):
                continue
            _record(parts, "字节 {} 附近记录 {}".format(offset, kept + 1),
                    " | ".join("{}={}".format(k, clean(v, 100)) for k, v in zip(header, row)) if header else " | ".join(row), "record")
            kept += 1
            if kept >= options["records"]:
                break


def _json(path, budget, options, parts, fields, headings):
    # Keep encoding detection within the same explicit byte budget. JSONL can
    # sample independent complete lines; a JSON array needs a bounded prefix.
    if path.suffix.lower() == ".jsonl":
        rows = []
        for offset, blob in _windows(path, budget, 5 if options["ocr_pages"] else 3):
            for line in decode(blob).splitlines():
                budget.check()
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(("JSONL 字节 {} 附近完整记录".format(offset), row))
                if len(rows) >= options["records"]:
                    break
    else:
        from services.structured_profile import _iter_json_incremental
        with path.open("rb") as stream:
            probe = stream.read(min(1024, budget.limit))
        encoding = "utf-16" if probe.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        records, metadata = _iter_json_incremental(path, encoding, options["records"], max(1, budget.limit - len(probe)))
        budget.used = min(budget.limit, len(probe) + int(metadata.get("sampled_bytes") or 0))
        rows = [("JSON 记录 {}（有界前缀）".format(i + 1), row) for i, row in enumerate(records)]
    for location, row in rows[:options["records"]]:
        if isinstance(row, dict):
            fields.extend(str(x)[:100] for x in row)
            _record(parts, location, json.dumps(row, ensure_ascii=False), "record")
    _record(parts, "JSON 字段", " | ".join(dict.fromkeys(fields)), "fields")


def _zip_open(path):
    from services.unified_parser import _zip_central_directory_preflight
    check = _zip_central_directory_preflight(path)
    if not check.get("safe"):
        raise ValueError("压缩目录预检未通过")
    return zipfile.ZipFile(path)


def _member_bytes(archive, name, budget, limit=None):
    info = archive.getinfo(name)
    if info.flag_bits & 1 or info.file_size > 64 * 1024 * 1024 or info.file_size / max(1, info.compress_size) > 200:
        return b""
    with archive.open(info) as stream:
        return budget.read(stream, min(limit or budget.limit, info.file_size))


def _xml_text(blob, tag="t"):
    # XMLPullParser retains complete elements before a truncated member ends.
    parser = ET.XMLPullParser(["end"])
    try:
        parser.feed(blob)
    except ET.ParseError:
        pass
    values = []
    try:
        for _, element in parser.read_events():
            if element.tag.rsplit("}", 1)[-1] == tag and element.text:
                values.append(element.text)
            element.clear()
    except ET.ParseError:
        pass
    return values


def _office(path, budget, options, parts, fields, headings):
    ext = path.suffix.lower()
    with _zip_open(path) as archive:
        names = set(archive.namelist())
        if ext == ".docx":
            # Prioritise document content, not ZIP member order or style XML.
            for member in ("docProps/core.xml", "word/document.xml"):
                if member in names:
                    limit = 2048 if member.startswith("docProps") else budget.limit
                    blob = _member_bytes(archive, member, budget, limit)
                    values = _xml_text(blob, "title" if member.startswith("docProps") else "t")
                    headings.extend(v for v in values[:8] if 2 < len(v) < 100 and not BOILERPLATE.search(v))
                    # Keep several sections of the decoded prefix, not just its first sentence.
                    for i in range(0, min(len(values), 150), 20):
                        _record(parts, "{} 文本段 {}".format(member, i + 1), "\n".join(values[i:i + 20]))
        elif ext == ".pptx":
            slides = sorted((x for x in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", x)), key=lambda x: int(re.search(r"slide(\d+)", x).group(1)))
            indices = sorted(set(round(i * max(0, len(slides) - 1) / max(1, options["pages"] - 1)) for i in range(min(len(slides), options["pages"]))))
            for i in indices:
                values = _xml_text(_member_bytes(archive, slides[i], budget, max(4096, budget.limit // max(1, len(indices)))))
                if values:
                    headings.append(values[0][:100])
                _record(parts, "幻灯片 {}".format(i + 1), "\n".join(values))
        else:
            shared = []
            if "xl/sharedStrings.xml" in names:
                blob = _member_bytes(archive, "xl/sharedStrings.xml", budget, budget.limit // 3)
                parser = ET.XMLPullParser(["end"])
                try:
                    parser.feed(blob)
                    for _, el in parser.read_events():
                        if el.tag.rsplit("}", 1)[-1] == "si":
                            shared.append("".join(x.text or "" for x in el.iter() if x.tag.rsplit("}", 1)[-1] == "t"))
                            el.clear()
                except ET.ParseError:
                    pass
            workbook = _member_bytes(archive, "xl/workbook.xml", budget, 8192) if "xl/workbook.xml" in names else b""
            rels = _member_bytes(archive, "xl/_rels/workbook.xml.rels", budget, 8192) if "xl/_rels/workbook.xml.rels" in names else b""
            mapping = {}
            try:
                mapping = {r.attrib.get("Id"): r.attrib.get("Target", "") for r in ET.fromstring(rels)}
                sheets = list(ET.fromstring(workbook).iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"))
            except ET.ParseError:
                sheets = []
            for sheet in sheets[:options["pages"]]:
                title = sheet.attrib.get("name", "工作表")
                headings.append(title)
                rid = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                target = mapping.get(rid, "")
                member = target.lstrip("/") if target.startswith("/") else "xl/" + target
                if member not in names or ".." in member.split("/"):
                    continue
                blob = _member_bytes(archive, member, budget, max(4096, budget.limit // max(1, min(len(sheets), options["pages"]))))
                parser = ET.XMLPullParser(["end"])
                try:
                    parser.feed(blob)
                except ET.ParseError:
                    pass
                count = 0
                try:
                    for _, el in parser.read_events():
                        if el.tag.rsplit("}", 1)[-1] != "row":
                            continue
                        values = []
                        for cell in el:
                            text = "".join(x.text or "" for x in cell.iter() if x.tag.rsplit("}", 1)[-1] in {"t", "v"})
                            if cell.attrib.get("t") == "s":
                                index = int(text) if text.isdigit() else -1
                                text = shared[index] if 0 <= index < len(shared) else "[共享字符串未采样]"
                            values.append(clean(text, 120))
                        if count == 0:
                            fields.extend(v for v in values if v and not v.startswith("["))
                        _record(parts, "工作表 {} / 第 {} 行".format(title, el.attrib.get("r", count + 1)), " | ".join(values), "fields" if count == 0 else "record")
                        count += 1
                        el.clear()
                        if count >= options["records"] + 1:
                            break
                except ET.ParseError:
                    pass


def _mail(path, budget, options, parts, fields, headings):
    with path.open("rb") as stream:
        blob = budget.read(stream, budget.limit)
    message = BytesParser(policy=policy.default).parsebytes(blob)
    for name in ("Subject", "From", "To", "Cc", "Date", "Message-ID", "In-Reply-To"):
        if message.get(name):
            _record(parts, "邮件头 " + name, str(message[name]), "header")
    headings.append(str(message.get("Subject") or ""))
    for item in message.walk():
        budget.check()
        if item.get_filename():
            _record(parts, "附件名称", item.get_filename(), "attachment")
        if item.get_content_type() == "text/plain":
            try:
                _record(parts, "邮件正文（有界前缀）", item.get_content())
            except (LookupError, UnicodeError):
                _record(parts, "邮件正文", decode(item.get_payload(decode=True) or b""))


def _ocr(image):
    # Reuse the local, CPU-only engine configuration; require installed models
    # so an import never starts a network download.
    from config import Config
    from services.unified_parser import UnifiedDocumentParser
    model_dir = Config.RAPIDOCR_MODEL_DIR
    if not all((model_dir / name).is_file() for name in ("ch_PP-OCRv5_det_mobile.onnx", "ch_PP-OCRv5_rec_mobile.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx")):
        raise RuntimeError("本地 OCR 模型不齐全，保留待 OCR 状态")
    global _OCR_PARSER
    if globals().get("_OCR_PARSER") is None:
        _OCR_PARSER = UnifiedDocumentParser(rapidocr_model_dir=model_dir)
    result = _OCR_PARSER._get_ocr_engine()(image)
    return "\n".join(list(result.txts) if result.txts is not None else [])


def _pdf(path, budget, options, parts, fields, headings):
    from pypdf import PdfReader
    reader = PdfReader(str(path), strict=False)
    total = len(reader.pages)
    indices = sorted(set(round(i * max(0, total - 1) / max(1, options["pages"] - 1)) for i in range(min(total, options["pages"]))))
    for index in indices:
        budget.check()
        value = reader.pages[index].extract_text() or ""
        _record(parts, "PDF 第 {} 页".format(index + 1), value)
        headings.extend(clean(x, 100) for x in value.splitlines()[:2] if 3 < len(clean(x)) < 100 and not BOILERPLATE.search(x))
    if sum(len(x["text"]) for x in parts) < 80 and options["ocr_pages"]:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        try:
            for index in indices[:options["ocr_pages"]]:
                budget.check()
                page = pdf[index]
                bitmap = None
                try:
                    width, height = page.get_size()
                    scale = min(1.5, 1600 / max(width, height))
                    bitmap = page.render(scale=scale)
                    _record(parts, "PDF 第 {} 页 OCR".format(index + 1), _ocr(bitmap.to_numpy()), "ocr")
                finally:
                    if bitmap is not None:
                        bitmap.close()
                    page.close()
        finally:
            pdf.close()
    return {"pages_total": total, "pages_sampled": [i + 1 for i in indices], "io_accounting": "PDF 解析器内部读取；以进程时间和内存限制约束"}


def _image(path, budget, options, parts, fields, headings):
    if not options["ocr_pages"]:
        return
    from PIL import Image
    with Image.open(path) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("图片像素超过轻量 OCR 上限")
        image.thumbnail((1600, 1600))
        import numpy as np
        _record(parts, "图片 OCR 预览", _ocr(np.asarray(image.convert("RGB"))), "ocr")


def _archive(path, budget, options, parts, fields, headings):
    with _zip_open(path) as archive:
        candidates = []
        for item in archive.infolist():
            if not item.is_dir():
                candidates.append(item.filename)
        names = candidates[:200]
        _record(parts, "压缩包成员名称（最多 200 项）", "\n".join(names), "member_names")
        # Do not let names alone become content evidence or a verified topic.
        if options["ocr_pages"]:
            for name in names[:12]:
                if Path(name).suffix.lower() in TEXT | {".csv", ".jsonl"}:
                    _record(parts, "压缩包成员 " + name, decode(_member_bytes(archive, name, budget, 8192)))


def _terms(text):
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,10}", text.lower())
    ignored = {"the", "and", "for", "with", "from", "this", "that", "数据", "文件", "资料", "报告", "记录", "sample", "text"}
    return [w for w, _ in Counter(w for w in words if w not in ignored and not BOILERPLATE.search(w)).most_common(16)]


def _match(term, text):
    return bool(re.search(r"\b" + re.escape(term) + r"\b", text, re.I)) if term.isascii() else term in text


def portrait(item, parts, fields, headings, refine=False, warnings=None):
    fields = list(dict.fromkeys(clean(x, 100) for x in fields if clean(x)))[:40]
    headings = list(dict.fromkeys(clean(x, 100) for x in headings if clean(x)))[:20]
    body_parts = [p for p in parts if p["kind"] not in {"member_names", "attachment"}]
    body = "\n".join(p["text"] for p in body_parts)
    weighted = " ".join(fields + headings) + " " + body
    scores = []
    for topic, category, terms in TOPICS:
        hits = [term for term in terms if _match(term, weighted)]
        # Content-only support: filenames never by themselves decide a topic.
        if hits:
            scores.append((len(hits), topic, category, hits))
    scores.sort(key=lambda row: (-row[0], row[1]))
    ambiguous = len(scores) > 1 and scores[0][0] == scores[1][0]
    supported = bool(scores and scores[0][0] >= 2 and not ambiguous)
    ext = Path(item.get("path") or item.get("name") or "").suffix.lower()
    role = "数据表" if fields or ext in {".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".json", ".jsonl"} else "邮件" if ext in {".eml", ".mbox", ".msg"} else "演示文稿" if ext in {".ppt", ".pptx"} else "文档"
    quality = "content" if len(body) >= 40 or len(fields) >= 2 else "metadata_only"
    if supported:
        _, topic, category, hits = scores[0]
        quality = "identified"
        basis = "内容命中：" + "、".join(hits)
    else:
        category = "结构化数据" if role == "数据表" else "邮件资料" if role == "邮件" else "压缩包" if ext == ".zip" else "文本资料" if body else "未分类"
        # A bounded, source-derived title/schema preserves unfamiliar subjects.
        title = next((x for x in headings if 4 <= len(x) <= 100 and not BOILERPLATE.search(x) and not re.fullmatch(r"[\d\W]+|Sheet\d+", x, re.I)), "")
        if fields:
            topic = "字段：" + " / ".join(fields[:3])[:75]
            basis = "依据已采样字段；业务主题待核查"
        elif title and body:
            topic = "主题线索：" + title
            basis = "依据原文章节或标题；主题待核查"
        else:
            topic = "待识别内容" if not body else "其他内容线索"
            basis = "当前样本不足以确定业务主题"
    needs_refine = quality != "identified" or ambiguous or bool(body and sum(bool(BOILERPLATE.search(p["text"])) for p in body_parts) > len(body_parts) / 2)
    state = "identified" if quality == "identified" else "needs_review" if refine else "needs_more" if body else "needs_ocr" if ext == ".pdf" or ext in IMAGES else "metadata_only"
    dates = sorted(set(re.findall(r"(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?", body)))[:12]
    entities = sorted(set(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", body)))[:12]
    entities = list(dict.fromkeys(entities + re.findall(r"(?:客户|供应商|机构|单位|公司名称|负责人|联系人|参与者)\s*[:：=]\s*([^\s,，;；|]{2,50})", body)))[:16]
    projects = re.findall(r"(?:项目名称|项目编号|合同编号|事项编号)\s*[:：=]\s*([^\s,，;；|]{2,50})", body)[:8]
    # Explicit project identifiers are facets, not evidence that every similarly
    # named file belongs to one matter.
    evidence = [dict(p, source_path=item.get("path")) for p in parts[:16]]
    return {
        "schema": SCHEMA, "topic": topic, "category": category, "document_role": role,
        "recognition_status": state, "quality": quality, "needs_refine": needs_refine and not refine,
        "classification_basis": basis, "candidate_topics": [x[1] for x in scores[:3]],
        "ambiguous": ambiguous, "headings": headings, "fields": fields,
        "keywords": _terms(weighted), "entities": entities, "projects": projects, "dates": dates,
        "language": "中文" if re.search(r"[\u4e00-\u9fff]", body) else "英文或其他" if body else "未知",
        "evidence": evidence, "warnings": list(warnings or [])[:8],
        "confidence": min(.9, .55 + .07 * scores[0][0]) if supported else .3 if body else 0,
        "confidence_note": "规则支持强度，用于导航；不是校准概率或研究结论置信度",
    }


def preview_file(root, item, refine=False):
    options = settings(refine)
    budget = Budget(options)
    base = Path(root).resolve()
    path = (base / item["path"]).resolve()
    if base != path and base not in path.parents:
        raise ValueError("文件超出数据包目录")
    from services.scanner import is_sensitive_file
    if item.get("sensitive") or is_sensitive_file(path.name):
        return {"schema_version": SCHEMA, "summary_type": "local", "status": "restricted", "sample": "", "notice": "敏感文件仅保留元数据", "complete": False, "source_hash_kind": "pending", "portrait": {"recognition_status": "restricted", "topic": "受限文件", "needs_refine": False}}, None
    stat = path.stat()
    if item.get("modified_at_ns") and (stat.st_mtime_ns != int(item["modified_at_ns"]) or stat.st_size != int(item.get("size", stat.st_size))):
        raise RuntimeError("文件自盘点后发生变化，请重新盘点")
    parts, fields, headings, warnings = [], [], [], []
    ext = path.suffix.lower()
    extra = {}
    try:
        if ext in {".csv", ".tsv"}:
            _csv(path, budget, options, parts, fields, headings)
        elif ext in {".json", ".jsonl"}:
            _json(path, budget, options, parts, fields, headings)
        elif ext in TEXT:
            _text(path, budget, options, parts, fields, headings)
        elif ext in {".docx", ".pptx", ".xlsx", ".xlsm"}:
            _office(path, budget, options, parts, fields, headings)
        elif ext in {".doc", ".ppt", ".xls", ".rtf"}:
            from services.legacy_office import extract_legacy
            extracted = extract_legacy(path, max_chars=12000 if refine else 6000,
                preview=True, refine=refine, seconds=max(.1, budget.deadline-time.monotonic()-.2))
            metadata = extracted.get("metadata") or {}
            warnings.extend(extracted.get("warnings") or [])
            fields.extend(metadata.get("fields") or [])
            headings.extend(metadata.get("headings") or [])
            sections = metadata.get("sections") or []
            for section in sections:
                _record(parts, section["location"], section["text"], section.get("kind", "text"))
            if not sections:
                text = extracted.get("text") or ""
                for offset in range(0, min(len(text), 6000), 1500):
                    _record(parts, "旧格式正文字符 {}–{}（预算内提取）".format(offset+1, min(offset+1500,len(text))), text[offset:offset+1500])
            extra.update(legacy_format=True, legacy_byte_budget=metadata.get("byte_budget", 0),
                legacy_source_bytes=stat.st_size, legacy_parser=extracted.get("parser"),
                legacy_complete=metadata.get("content_complete", False))
        elif ext in {".eml", ".mbox"}:
            _mail(path, budget, options, parts, fields, headings)
            if ext == ".mbox":
                warnings.append("MBOX 仅抽取有界前缀，未覆盖全部邮件")
        elif ext == ".pdf":
            extra = _pdf(path, budget, options, parts, fields, headings)
        elif ext in IMAGES:
            _image(path, budget, options, parts, fields, headings)
        elif ext == ".zip":
            _archive(path, budget, options, parts, fields, headings)
        else:
            warnings.append("该格式暂不支持轻量正文识别，保留文件清单")
    except (TimeoutError, ValueError, OSError, ImportError, zipfile.BadZipFile) as exc:
        warnings.append(str(exc)[:240])
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError("文件在预览过程中发生变化，已丢弃本次样本")
    info = portrait(item, parts, fields, headings, refine, warnings)
    info["format"] = ext
    info["parser_mode"] = "bounded_refinement" if refine else "bounded_native_preview"
    if ext not in TEXT | IMAGES | {".csv", ".tsv", ".json", ".jsonl", ".docx", ".pptx", ".xlsx", ".xlsm", ".doc", ".ppt", ".xls", ".rtf", ".eml", ".mbox", ".pdf", ".zip"}:
        info.update(needs_refine=False, recognition_status="unsupported")
    material = json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()
    coverage = {"complete": False, "preview_only": True, "sampled_bytes": budget.used, "byte_budget": budget.limit,
                "seconds_budget": options["seconds"], "sample_locations": [p["location"] for p in parts], **extra}
    return {
        "schema_version": SCHEMA, "summary_type": "local", "title": headings[0] if headings else path.stem,
        "category": info["category"], "keywords": info["keywords"], "sample": "\n".join(p["text"] for p in parts)[:4000],
        "size": stat.st_size, "sampled_bytes": budget.used, "complete": False, "portrait": info, "coverage": coverage,
        "sample_sha256": hashlib.sha256(material).hexdigest(), "source_hash_kind": "pending",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "notice": "；".join(warnings),
    }, None
