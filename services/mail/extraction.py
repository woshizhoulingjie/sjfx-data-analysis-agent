"""Bounded first-pass extraction for explicit mail attachments."""

from __future__ import annotations

import csv
import io
from pathlib import Path


def _text(payload):
    return payload.decode("utf-8", errors="replace")


def _pdf(payload):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(payload))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages), {"pages": len(reader.pages)}


def _docx(payload):
    from docx import Document
    document = Document(io.BytesIO(payload))
    values = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    for table in document.tables:
        for row in table.rows:
            values.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(values), {}


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
        return "\n".join("[{}]\n{}".format(sheet.name,"\n".join("\t".join(str(v) for v in sheet.row_values(i)) for i in range(sheet.nrows))) for sheet in workbook.sheets()), {"sheets":workbook.sheet_names()}
    finally:
        workbook.release_resources()


def _spreadsheet(payload):
    from openpyxl import load_workbook
    workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    values = []
    for sheet in workbook.worksheets:
        values.append("[{}]".format(sheet.title))
        for row in sheet.iter_rows(values_only=True):
            line = "\t".join(str(cell) if cell is not None else "" for cell in row)
            if line.strip():
                values.append(line)
    return "\n".join(values), {"sheets": [sheet.title for sheet in workbook.worksheets]}


def _extract_attachment(part, *, max_chars=500000):
    """Return text plus a truthful status; OCR is queued separately."""
    payload = part.raw_payload or b""
    content_type = str(part.content_type or "").casefold()
    filename = str(part.filename_raw or "")
    suffix = Path(filename).suffix.casefold().replace(" ", "")
    if content_type.startswith("text/") or suffix in {".txt", ".csv", ".html", ".htm"}:
        return {"status": "ready", "text": _text(payload)[:max_chars], "parser": "mail-text", "warnings": []}
    try:
        if content_type == "application/pdf" or suffix == ".pdf":
            text, meta = _pdf(payload)
            return {"status": "ready" if text.strip() else "ocr_pending", "text": text[:max_chars], "parser": "pypdf", "warnings": [], "meta": meta}
        if "wordprocessingml" in content_type or suffix == ".docx":
            text, meta = _docx(payload)
            return {"status": "ready", "text": text[:max_chars], "parser": "python-docx", "warnings": [], "meta": meta}
        if suffix == ".xls" or payload.startswith(bytes.fromhex("D0CF11E0A1B11AE1")) and content_type == "application/vnd.ms-excel":
            text,meta=_xls(payload)
            return {"status":"ready","text":text[:max_chars],"parser":"xlrd","warnings":[],"meta":meta}
        if "spreadsheetml" in content_type or content_type in {"application/vnd.ms-excel", "application/vnd.ms-excel.sheet.macroenabled.12"} or suffix in {".xlsx", ".xlsm"}:
            text, meta = _spreadsheet(payload)
            return {"status": "ready", "text": text[:max_chars], "parser": "openpyxl", "warnings": [], "meta": meta}
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
        (root/"meta.json").write_text(json.dumps({"content_type":part.content_type,"filename_raw":part.filename_raw,"max_chars":max_chars}),encoding="utf-8")
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
    if parser is None:return {"status":"ocr_pending","text":"","parser":"unavailable","warnings":["OCR 解析器尚未配置"]}
    suffix=Path(part.filename_raw or "").suffix.lower()
    if suffix not in {".pdf",".png",".jpg",".jpeg",".tiff",".bmp"}:
        suffix=".pdf" if part.content_type=="application/pdf" else ".png"
    with tempfile.TemporaryDirectory(prefix="sjfx-mail-ocr-") as folder:
        path=Path(folder)/("attachment"+suffix);path.write_bytes(part.raw_payload or b"")
        try:
            from .thai_ocr import available
            if available():
                import subprocess,sys,json,os
                env={**os.environ,"OPENBLAS_NUM_THREADS":"1","OMP_NUM_THREADS":"1"}
                result=subprocess.run([sys.executable,"-m","services.mail.extraction",str(path),"ocr"],capture_output=True,timeout=Config.MAIL_PARSE_TIMEOUT_SECONDS,env=env)
                if result.returncode:raise RuntimeError("隔离 OCR 进程退出 ({})".format(result.returncode))
                return json.loads((Path(folder)/"ocr-result.json").read_text(encoding="utf-8"))
            value=runner_for(parser).parse(path,path.name,mode="accurate",timeout=Config.MAIL_PARSE_TIMEOUT_SECONDS,memory_mb=Config.MAIL_PARSE_MEMORY_MB,cancel_check=cancelled)
            text=str(value.get("text") or "")
            return {"status":"ready" if text.strip() else "ocr_no_text","text":text,"parser":"isolated-ocr","warnings":value.get("warnings") or []}
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
        from .thai_ocr import extract
        path=Path(sys.argv[1]);text,complete=extract(path,500000,time.monotonic()+Config.MAIL_PARSE_TIMEOUT_SECONDS-1)
        result={"status":"ready" if text.strip() and complete else "partial" if text.strip() else "ocr_no_text","text":text,"parser":"tesseract-tha-eng","warnings":[] if complete else ["附件 OCR 文本达到长度上限"]}
        (path.parent/"ocr-result.json").write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
        sys.exit(0)
    root=Path(sys.argv[1]);meta=json.loads((root/"meta.json").read_text(encoding="utf-8"))
    value=_extract_attachment(SimpleNamespace(raw_payload=(root/"payload").read_bytes(),content_type=meta["content_type"],filename_raw=meta["filename_raw"]),max_chars=meta["max_chars"])
    (root/"result.json").write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
