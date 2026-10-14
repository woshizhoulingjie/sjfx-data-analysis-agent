"""Local multilingual mail OCR with bounded page provenance.

Call only inside the mail OCR subprocess: its parent enforces the hard timeout.
No network model resolution is permitted by this helper.
"""
from __future__ import annotations

import importlib.util
import math
import os
from pathlib import Path
import subprocess
import time

from services.parse_temp import temporary_parse_directory


_MODELS = ("ch_PP-OCRv5_det_mobile.onnx", "ch_PP-OCRv5_rec_mobile.onnx",
           "ch_ppocr_mobile_v2.0_cls_mobile.onnx")
_SERVER_REC_MODEL = "ch_PP-OCRv5_rec_server.onnx"


def _rapid_models():
    """Choose only existing local assets; explicit server requests fail closed.

    The larger recognizer resolves small, thin Chinese glyphs missed by the
    mobile model even at high confidence. Detection and orientation keep their
    bounded mobile models. No inference-time model download is allowed.
    """
    from config import Config
    requested = os.getenv("MAIL_OCR_REC_MODEL", "auto").strip().lower()
    if requested not in {"auto", "mobile", "server"}:
        raise ValueError("MAIL_OCR_REC_MODEL 必须为 auto、mobile 或 server")
    root = Path(Config.RAPIDOCR_MODEL_DIR)
    if requested == 'server' and not (root / _SERVER_REC_MODEL).is_file():
        raise RuntimeError('已指定 server OCR，但本地服务器识别模型不存在')
    server = requested == "server" or requested == "auto" and (root / _SERVER_REC_MODEL).is_file()
    return (_MODELS[0], _SERVER_REC_MODEL if server else _MODELS[1], _MODELS[2])


def select_language(filename=""):
    requested = os.getenv("MAIL_OCR_LANGUAGE", "auto").strip().lower()
    aliases = {"ch": "zh", "chi_sim": "zh", "zh-cn": "zh", "th": "tha", "en": "eng"}
    requested = aliases.get(requested, requested)
    if requested not in {"auto", "zh", "tha", "eng"}:
        raise ValueError("MAIL_OCR_LANGUAGE 必须为 auto、zh、tha 或 eng")
    if requested != "auto":
        return requested
    # This is a routing hint only, never a claim about the document's language.
    return "tha" if any("\u0e00" <= ch <= "\u0e7f" for ch in str(filename)) else "auto"


def rapid_available():
    from config import Config
    return (all((Path(Config.RAPIDOCR_MODEL_DIR) / name).is_file() for name in _rapid_models())
            and importlib.util.find_spec("rapidocr") is not None)


def available(language="auto"):
    from . import thai_ocr
    return thai_ocr.available() if language == "tha" else rapid_available()


def _rapid_engine():
    if not rapid_available():
        raise RuntimeError("邮件中文 OCR 缺少现有本地 RapidOCR 模型或组件")
    from config import Config
    from rapidocr import RapidOCR
    import cv2
    # The subprocess's address-space limit also covers native thread stacks.
    # OMP/BLAS environment variables do not cap OpenCV's separate worker pool;
    # under RLIMIT_AS its resize step otherwise fails to spawn worker threads.
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
    # Mirror the established parser's CPU/local-model setup. Its legacy
    # params=None fallback is deliberately avoided: that could fetch models.
    names = _rapid_models()
    server_rec = names[1] == _SERVER_REC_MODEL
    local = dict(zip(("Det.model_path", "Rec.model_path", "Cls.model_path"),
                     (str(Path(Config.RAPIDOCR_MODEL_DIR) / name) for name in names)))
    if server_rec:
        # Long and short lines otherwise share the widest crop's padding in
        # each batch. On the server model this wastes substantial CPU time.
        local['Rec.rec_batch_num'] = 1
    cpu = {"EngineConfig.onnxruntime.use_" + key: False
           for key in ("cuda", "dml", "cann", "coreml")}
    threads = 2 if server_rec else 1
    cpu.update({"EngineConfig.onnxruntime.intra_op_num_threads": threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1})
    cpu_configured = True
    try:
        engine = RapidOCR(params={**local, **cpu})
    except (KeyError, TypeError, ValueError):
        cpu_configured = False
        engine = RapidOCR(params=local)
    # Record the actual selected local files, rather than RapidOCR's package
    # defaults (which can describe a different generation of models).
    try:
        engine._mail_ocr_profile = {
            'engine': 'RapidOCR PP-OCRv5 ' + ('server recognition / mobile detection'
                       if server_rec else 'mobile'),
            'detection_model': names[0], 'recognition_model': names[1],
            'classification_model': names[2], 'model_resolution': 'local files only',
            'recognition_batch_size': 1 if server_rec else None,
            'inference_threads': threads if cpu_configured else None,
        }
    except AttributeError:
        pass
    return engine


def _rapid_result(engine, image):
    result = engine(image)
    scores, boxes = [], []
    if hasattr(result, "txts"):
        values = result.txts
        if values is None:
            values = []
        if isinstance(values, str):
            values = [values]
        scores = list(result.scores) if getattr(result,'scores',None) is not None else []
        boxes = list(result.boxes) if getattr(result,'boxes',None) is not None else []
    # RapidOCR deployments use both the current output DTO and the earlier
    # (rows, elapsed) API. Do not mistake an unsupported API or exception for
    # a successful recognition that happened to find no text.
    else:
        rows = result[0] if isinstance(result, tuple) and len(result) == 2 else result
        if rows is None:rows=[]
        if not isinstance(rows,(list,tuple)):
            raise TypeError("RapidOCR 返回了不支持的结果结构：{}".format(type(result).__name__))
        values = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2 or not isinstance(row[1], str):
                raise TypeError("RapidOCR 返回了不支持的行结构")
            values.append(row[1])
            boxes.append(row[0]);scores.append(row[2] if len(row)>2 else None)
    texts=[str(value) for value in values]
    lines=[];valid_scores=[];offset=0
    for index,text in enumerate(texts):
        raw_score=scores[index] if index<len(scores) else None
        try:score=float(raw_score) if raw_score is not None and not isinstance(raw_score,bool) else None
        except (TypeError,ValueError):score=None
        if score is not None and (not math.isfinite(score) or not 0<=score<=1):score=None
        if score is not None:valid_scores.append(score)
        box=boxes[index] if index<len(boxes) else None
        if hasattr(box,'tolist'):box=box.tolist()
        if not isinstance(box,(list,tuple)):box=None
        lines.append({'char_start':offset,'char_end':offset+len(text),'confidence':score,'bbox':box})
        offset+=len(text)+1
    profile = getattr(engine, '_mail_ocr_profile', {})
    if not isinstance(profile, dict):
        profile = {}
    return '\n'.join(texts),{'engine':'RapidOCR PP-OCRv5 mobile',**profile,'confidence_available':bool(valid_scores),
        'line_count':len(lines),'scored_lines':len(valid_scores),
        'minimum_confidence':min(valid_scores) if valid_scores else None,
        'mean_confidence':sum(valid_scores)/len(valid_scores) if valid_scores else None,
        'recognition_accuracy':'unverified','confidence_basis':'engine line scores; not character accuracy',
        'bbox_coordinate_space':'input image pixels','lines':lines}


def _rapid_text(engine,image):
    return _rapid_result(engine,image)[0]


def _recognize(image, engine, language, deadline):
    from . import thai_ocr
    if language == "tha":
        return thai_ocr.ocr_image(image, deadline), "tha+eng", False, {'confidence_available':False,'recognition_accuracy':'unverified'}
    text,quality = _rapid_result(engine, str(image))
    if language == "auto" and thai_ocr.available() and (
            not text.strip() or sum("\u0e00" <= ch <= "\u0e7f" for ch in text) >= 3):
        alternative = thai_ocr.ocr_image(image, deadline)
        if alternative.strip():
            return alternative, "tha+eng", True, {'confidence_available':False,'recognition_accuracy':'unverified'}
    return text, "zh+eng", False, quality


def extract_result(path, max_chars, deadline, *, language="auto"):
    """Return located pages and distinguish traversal from text recognition.

    A page with no recognized text may be blank; it lowers text coverage but
    is not an exception. OCR confidence is never reported as full fidelity.
    """
    if language not in {"auto", "zh", "tha", "eng"}:
        raise ValueError("无效 OCR 识别语言")
    path = Path(path)
    engine = None if language == "tha" else _rapid_engine()
    units, warnings, errors = [], [], []
    remaining, truncated, traversal_complete = max(0, max_chars), False, True
    pdf = None
    try:
        if path.suffix.lower() == ".pdf":
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(path))
            total_pages = len(pdf)
        else:
            total_pages = 1
        with temporary_parse_directory(prefix="mail-local-ocr-") as folder:
            for index in range(total_pages):
                if time.monotonic() >= deadline - .1:
                    traversal_complete = False
                    warnings.append("附件 OCR 达到时间上限，存在未处理页")
                    break
                if remaining <= 0:
                    truncated, traversal_complete = True, False
                    break
                page, bitmap = None, None
                stage = "render"
                try:
                    image = path
                    if pdf is not None:
                        page = pdf[index]
                        bitmap = page.render(scale=2.2)
                        image = Path(folder) / "page.png"
                        bitmap.to_pil().save(image)
                    stage = "recognition"
                    recognized = _recognize(image, engine, language, deadline)
                    text, used_language, fallback = recognized[:3]
                    quality=recognized[3] if len(recognized)>3 else {'confidence_available':False,'recognition_accuracy':'unverified'}
                    if fallback:
                        warnings.append("第 {} 页中文模型无文字或含泰文线索，尝试泰文/英文；识别语言需复核".format(index + 1))
                    if len(text) > remaining:
                        truncated = True
                    units.append({"text": text[:remaining], "page_number": index + 1,
                                  "ocr_language": used_language, "language_fallback": fallback,
                                  "text_origin":"ocr","ocr_quality":quality,
                                  "render_dpi":158.4 if pdf is not None else None})
                    remaining -= len(text[:remaining]) + (2 if index < total_pages - 1 else 0)
                    if truncated:
                        traversal_complete = index == total_pages - 1
                        break
                except subprocess.TimeoutExpired:
                    traversal_complete = False
                    warnings.append("附件 OCR 达到时间上限，存在未处理页")
                    break
                except Exception as exc:
                    # Do not include filenames, exception messages or source text.
                    errors.append({"page_number": index + 1, "stage": stage, "type": type(exc).__name__})
                    units.append({"text": "", "page_number": index + 1,
                                  "ocr_language": "tha+eng" if language == "tha" else "zh+eng"})
                finally:
                    if bitmap is not None:
                        bitmap.close()
                    if page is not None:
                        page.close()
    finally:
        if pdf is not None:
            pdf.close()
    text = "\n\n".join(u["text"] for u in units)
    if not text.strip():
        text = ""
    empty_pages = [u["page_number"] for u in units if not u["text"].strip()]
    failed_pages = {error['page_number'] for error in errors}
    successfully_empty = [page for page in empty_pages if page not in failed_pages]
    if successfully_empty:
        warnings.append("以下页未识别到文字，可能为空白页或识别遗漏：" + ",".join(map(str, successfully_empty)))
    if errors:
        warnings.append("以下页 OCR 失败：" + ",".join(str(e["page_number"]) for e in errors))
    if truncated:
        warnings.append("附件 OCR 达到长度上限，文本结果不完整")
    uncertain = bool(empty_pages or errors or truncated or not traversal_complete
                     or any(u.get("language_fallback") for u in units))
    languages = sorted({u["ocr_language"] for u in units})
    status = ("partial" if uncertain else "ready") if text else "failed" if errors or not traversal_complete else "ocr_no_text"
    result = {"status": status,
            "text": text, "units": units, "warnings": warnings,
            "parser": "mail-local-ocr:" + ",".join(languages),
            "coverage_ratio": None if truncated else sum(bool(u["text"].strip()) for u in units) / max(1, total_pages),
            "truncated": truncated,
            "meta": {"pages": total_pages, "processed_pages": len(units), "traversal_complete": traversal_complete,
                     "missing_pages": empty_pages, "page_errors": errors,
                     "requested_language": language, "ocr_languages": languages,
                     "coverage_basis": "pages_with_recognized_text; not recognition accuracy"}}
    from .text_provenance import OCR_NOTICE
    if text:result['warnings'].append(OCR_NOTICE)
    if status == "failed":
        result['error'] = {'type': 'MailOCRPageError' if errors else 'MailOCRIncomplete',
            'message': '附件 OCR 技术失败或未能完成识别；不能将该结果视为附件没有文字',
            'page_errors': errors}
    return result
