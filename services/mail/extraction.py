"""Bounded first-pass extraction for explicit mail attachments."""

from __future__ import annotations

import csv
import io
from pathlib import Path


def _text(payload, charset=None, warnings=None):
    from .parsers.eml import _decode_text
    return _decode_text(payload, charset, warnings if warnings is not None else [])


def _pdf(payload):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(payload))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF 密码保护，无法抽取")
    units = []
    errors = []
    for number, page in enumerate(reader.pages, 1):
        try:
            units.append({"text": page.extract_text() or "", "page_number": number})
        except Exception as exc:
            errors.append({"page_number": number, "error": str(exc)[:200]})
            units.append({"text": "", "page_number": number})
    return "\n\n".join(u["text"] for u in units), {"pages": len(reader.pages), "units": units,
        "missing_pages": [u["page_number"] for u in units if not u["text"].strip()], "page_errors": errors}


def _docx(payload):
    from docx import Document
    from docx.oxml.ns import qn
    import zipfile
    import xml.etree.ElementTree as ET
    document = Document(io.BytesIO(payload))

    def inline(element):
        if element.tag == qn('w:del'):
            return ''
        if element.tag == qn('w:t'):
            return element.text or ''
        if element.tag == qn('w:tab'):
            return '\t'
        if element.tag in {qn('w:br'), qn('w:cr')}:
            return '\n'
        return ''.join(inline(child) for child in element)

    def blocks(element):
        if element.tag == qn('w:p'):
            value = inline(element)
            return [value] if value else []
        if element.tag == qn('w:tr'):
            # Walk physical XML cells once: row.cells repeats merged cells and
            # cell.text omits nested tables. Recursing preserves both contents.
            cells = []
            def collect(node):
                if node.tag == qn('w:tc'):
                    cells.append('\n'.join(blocks(node)))
                else:
                    for child in node:
                        collect(child)
            collect(element)
            return ['\t'.join(cells)] if cells else []
        if element.tag == qn('w:del'):
            return []
        return [value for child in element for value in blocks(child)]

    units = [{'text': '\n'.join(blocks(document.element.body)), 'document_part': 'body'}]
    incomplete = bool(list(document.element.body.iter(qn('w:altChunk'))))
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in sorted(archive.namelist()):
            if not name.endswith('.xml') or not name.startswith('word/'):
                continue
            component = name[len('word/'):]
            if not (component.startswith(('header', 'footer')) or component in {'footnotes.xml', 'endnotes.xml', 'comments.xml'}):
                continue
            root = ET.fromstring(archive.read(name))
            text = '\n'.join(blocks(root))
            if text:
                units.append({'text': text, 'document_part': component.rsplit('.', 1)[0]})
            incomplete = incomplete or bool(list(root.iter(qn('w:altChunk'))))
    meta = {'units': units, 'incomplete': incomplete}
    if incomplete:
        meta['warnings'] = ['Word 含外部嵌入文本部件，当前文本结果不完整；保留原件']
    return '\n\n'.join(unit['text'] for unit in units), meta


def _xls(payload):
    import sys
    vendor=str(Path(__file__).parent/"_vendor")
    if vendor not in sys.path:sys.path.insert(0,vendor)
    try:
        import xlrd
    except ImportError as exc:
        raise RuntimeError("旧版 XLS 需要可选 xlrd 组件") from exc
    workbook=xlrd.open_workbook(file_contents=payload,on_demand=True)
    try:
        from openpyxl.utils import get_column_letter
        units = [{"text": "\t".join(str(v) for v in sheet.row_values(i)), "sheet_name": sheet.name,
                  "cell_range": "A{}:{}{}".format(i+1, get_column_letter(max(1,sheet.ncols)), i+1)}
                 for sheet in workbook.sheets() for i in range(sheet.nrows)]
        return "\n".join(u["text"] for u in units), {"sheets": workbook.sheet_names(), "units": units}
    finally:
        workbook.release_resources()


def _spreadsheet(payload):
    from openpyxl import load_workbook
    workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    from openpyxl.utils import get_column_letter
    units = []
    try:
        names = [s.title for s in workbook.worksheets]
        for sheet in workbook.worksheets:
            for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                line = "\t".join(str(cell) if cell is not None else "" for cell in row)
                if line.strip():
                    units.append({"text": line, "sheet_name": sheet.title,
                        "cell_range": "A{}:{}{}".format(number,get_column_letter(max(1,len(row))),number)})
        return "\n".join(u["text"] for u in units), {"sheets": names, "units": units}
    finally:
        workbook.close()


def _result(text, parser, meta=None, *, max_chars=500000):
    meta = meta or {}
    units = meta.get("units") or [{"text": text}]
    bounded = []
    remaining = max_chars
    clipped = False
    for unit in units:
        value = str(unit.get("text") or "")
        if remaining <= 0:
            clipped = True
            break
        if len(value)>remaining:clipped=True
        bounded.append({**unit, "text": value[:remaining],"text_origin":"native","extraction_method":parser})
        remaining -= len(value) + 2
    truncated = clipped or len(text) > max_chars or len(bounded) < len(units)
    missing = meta.get("missing_pages") or []
    warnings = list(meta.get('warnings') or [])
    if truncated: warnings.append("附件文本达到长度上限，保留原文件；文本结果不完整")
    if missing: warnings.append("PDF 以下页缺少可抽取文本，等待 OCR：" + ",".join(map(str,missing)))
    for error in meta.get('page_errors') or []:
        warnings.append('PDF 第 {} 页抽取失败：{}'.format(error['page_number'],error['error']))
    status = "ocr_pending" if missing else "partial" if truncated or meta.get('incomplete') else "ready"
    coverage = (meta.get("pages", 1)-len(missing))/max(1,meta.get("pages", 1))
    if truncated: coverage = min(coverage, max_chars/max(1,len(text)))
    if meta.get('incomplete'): coverage = None
    # Empty scanned pages still keep their page units for OCR, but separators
    # alone are not extracted text and must not inflate extracted_chars.
    combined_text = "\n\n".join(u["text"] for u in bounded)
    if not combined_text.strip():
        combined_text = ""
    return {"status": status, "text": combined_text, "units": bounded,
        "parser": parser, "warnings": warnings, "coverage_ratio": coverage,
        "meta": {k:v for k,v in meta.items() if k != "units"}, "truncated": truncated}


def _extract_attachment(part, *, max_chars=500000):
    """Return text plus a truthful status; OCR is queued separately."""
    payload = part.raw_payload or b""
    content_type = str(part.content_type or "").casefold()
    filename = str(part.filename_raw or "")
    suffix = Path(filename).suffix.casefold().replace(" ", "")
    if content_type.startswith("text/") or suffix in {".txt", ".csv", ".html", ".htm"}:
        warnings = []
        text = _text(payload, getattr(part, "charset", None), warnings)
        if content_type == "text/html" or suffix in {".html", ".htm"}:
            from .html_safety import html_to_translation_text
            text = html_to_translation_text(text)
        result = _result(text, "mail-text", max_chars=max_chars)
        if warnings:
            result["status"] = "partial"
            result["coverage_ratio"] = None
            result["warnings"].extend(warnings)
        return result
    try:
        if content_type == "application/pdf" or suffix == ".pdf":
            text, meta = _pdf(payload)
            return _result(text,"pypdf",meta,max_chars=max_chars)
        if "wordprocessingml" in content_type or suffix == ".docx":
            text, meta = _docx(payload)
            return _result(text,"python-docx",meta,max_chars=max_chars)
        if suffix == ".xls" or payload.startswith(bytes.fromhex("D0CF11E0A1B11AE1")) and content_type == "application/vnd.ms-excel":
            text,meta=_xls(payload)
            return _result(text,"xlrd",meta,max_chars=max_chars)
        if "spreadsheetml" in content_type or content_type in {"application/vnd.ms-excel", "application/vnd.ms-excel.sheet.macroenabled.12"} or suffix in {".xlsx", ".xlsm"}:
            text, meta = _spreadsheet(payload)
            return _result(text,"openpyxl",meta,max_chars=max_chars)
        if content_type.startswith("image/"):
            return {"status": "ocr_pending", "text": "", "parser": "none", "warnings": ["图片附件等待 OCR"]}
        if content_type in {"application/zip", "application/x-7z-compressed"}:
            return {"status": "restricted", "text": "", "parser": "none", "warnings": ["嵌套压缩附件默认不递归解析"]}
        return {"status": "unsupported", "text": "", "parser": "none", "warnings": ["此附件类型尚未支持文本提取"]}
    except Exception as exc:
        return {"status": "failed", "text": "", "parser": "failed", "warnings": [], "error": {"type": type(exc).__name__, "message": str(exc)[:500]}}


def extract_attachment(part, *, max_chars=500000):
    from config import Config
    if not Config.MAIL_ENABLE_ISOLATED_PARSING:
        return _extract_attachment(part,max_chars=max_chars)
    import subprocess,sys,tempfile,json,os
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="sjfx-mail-parse-") as folder:
        root=Path(folder);(root/"payload").write_bytes(part.raw_payload or b"")
        (root/"meta.json").write_text(json.dumps({"content_type":part.content_type,"filename_raw":part.filename_raw,"charset":getattr(part,"charset",None),"max_chars":max_chars}),encoding="utf-8")
        env={**os.environ,"OPENBLAS_NUM_THREADS":"1","OMP_NUM_THREADS":"1"}
        try:
            result=subprocess.run([sys.executable,"-m","services.mail.extraction",str(root)],capture_output=True,timeout=Config.MAIL_PARSE_TIMEOUT_SECONDS,env=env)
            if result.returncode:raise RuntimeError("附件解析子进程退出 ({})".format(result.returncode))
            return json.loads((root/"result.json").read_text(encoding="utf-8"))
        except Exception as exc:
            return {"status":"failed","text":"","parser":"isolated-mail","warnings":[],"error":{"type":type(exc).__name__,"message":str(exc)[:300]}}


def extract_ocr(part, parser, cancelled=lambda:False):
    from config import Config
    from services.parse_isolation import runner_for
    import tempfile
    from .ocr import select_language
    from .text_provenance import OCR_NOTICE
    if parser is None:return {"status":"unsupported","text":"","parser":"unavailable","warnings":["OCR 解析器尚未配置；配置后可手动重试"]}
    suffix=Path(part.filename_raw or "").suffix.lower()
    if suffix not in {".pdf",".png",".jpg",".jpeg",".tiff",".bmp"}:
        suffix=".pdf" if part.content_type=="application/pdf" else ".png"
    with tempfile.TemporaryDirectory(prefix="sjfx-mail-ocr-") as folder:
        path=Path(folder)/("attachment"+suffix);path.write_bytes(part.raw_payload or b"")
        try:
            language=select_language(part.filename_raw or "")
            from .ocr import available
            if available(language):
                import subprocess,sys,json,os
                env={**os.environ,"OPENBLAS_NUM_THREADS":"1","OMP_NUM_THREADS":"1"}
                result=subprocess.run([sys.executable,"-m","services.mail.extraction",str(path),"ocr",language],capture_output=True,timeout=Config.MAIL_OCR_TIMEOUT_SECONDS,env=env)
                if result.returncode:raise RuntimeError("隔离 OCR 进程退出 ({})".format(result.returncode))
                return json.loads((Path(folder)/"ocr-result.json").read_text(encoding="utf-8"))
            value=runner_for(parser).parse(path,path.name,mode="accurate",timeout=Config.MAIL_OCR_TIMEOUT_SECONDS,memory_mb=Config.MAIL_PARSE_MEMORY_MB,cancel_check=cancelled)
            text=str(value.get("text") or "")
            return {"status":"partial" if text.strip() else "ocr_no_text","text":text,"parser":"isolated-ocr",
                "units":[{"text":text,"text_origin":"ocr","ocr_quality":{"confidence_available":False,"recognition_accuracy":"unverified"}}],
                "warnings":(value.get("warnings") or [])+["此 OCR 解析器未返回逐页定位，不能确认页码与完整覆盖",OCR_NOTICE],"coverage_ratio":None}
        except Exception as exc:
            if cancelled():raise
            return {"status":"failed","text":"","parser":"isolated-ocr","error":{"type":type(exc).__name__,"message":str(exc)[:300]}}


if __name__ == "__main__":
    import sys,json
    from types import SimpleNamespace
    from config import Config
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(Config.MAIL_PARSE_MEMORY_MB*1024*1024,Config.MAIL_PARSE_MEMORY_MB*1024*1024))
    except (ImportError,ValueError,OSError):
        pass
    if len(sys.argv)>2 and sys.argv[2]=="ocr":
        import time
        from .ocr import extract_result
        path=Path(sys.argv[1]);language=sys.argv[3] if len(sys.argv)>3 else "auto"
        result=extract_result(path,500000,time.monotonic()+Config.MAIL_OCR_TIMEOUT_SECONDS-1,language=language)
        (path.parent/"ocr-result.json").write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
        sys.exit(0)
    root=Path(sys.argv[1]);meta=json.loads((root/"meta.json").read_text(encoding="utf-8"))
    value=_extract_attachment(SimpleNamespace(raw_payload=(root/"payload").read_bytes(),content_type=meta["content_type"],filename_raw=meta["filename_raw"],charset=meta.get("charset")),max_chars=meta["max_chars"])
    (root/"result.json").write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
