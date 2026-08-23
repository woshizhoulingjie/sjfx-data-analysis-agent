import hashlib
import errno
import gzip
import io
import bz2
import json
import os
import posixpath
import re
import threading
import zipfile
import tarfile
import tempfile
import shutil
import struct
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from config import Config, content_byte_limit
from services.scanner import extract_text, is_sensitive_file
from services.structured_profile import profile_path


ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".tar.gz", ".tar.bz2", ".rar", ".7z"}
ARCHIVE_MAX_ENTRIES = Config.MAX_ARCHIVE_ENTRIES
ARCHIVE_MAX_MEMBER_BYTES = Config.MAX_ARCHIVE_MEMBER_BYTES
ARCHIVE_MAX_TOTAL_BYTES = Config.MAX_ARCHIVE_UNCOMPRESSED_BYTES
ARCHIVE_MAX_DEPTH = max(0, int(os.getenv("MAX_ARCHIVE_DEPTH", "1")))
ARCHIVE_MAX_COMPRESSION_RATIO = Config.MAX_ARCHIVE_COMPRESSION_RATIO
ARCHIVE_MAX_MEMBER_PATH_DEPTH = Config.MAX_ARCHIVE_MEMBER_PATH_DEPTH
ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES = Config.MAX_ZIP_CENTRAL_DIRECTORY_ENTRIES
# A forged EOCD count must not make ZipFile read an arbitrarily large central
# directory into memory. Keep the byte budget independent from the entry count:
# names/comments/extras can otherwise make a small number of entries enormous.
ZIP_MAX_CENTRAL_DIRECTORY_BYTES = max(
    1024 * 1024,
    min(
        256 * 1024 * 1024,
        int(os.getenv("MAX_ZIP_CENTRAL_DIRECTORY_BYTES", str(64 * 1024 * 1024))),
    ),
)
OFFICE_XML_MEMBER_MAX_BYTES = max(
    1024 * 1024,
    min(16 * 1024 * 1024, int(os.getenv("MAX_OFFICE_XML_MEMBER_BYTES", str(8 * 1024 * 1024)))),
)
OFFICE_MEDIA_MEMORY_BYTES = max(
    1024 * 1024,
    min(256 * 1024 * 1024, int(os.getenv("MAX_OFFICE_MEDIA_MEMORY_BYTES", str(64 * 1024 * 1024)))),
)
OFFICE_OCR_TOTAL_BYTES = max(
    1024 * 1024,
    min(512 * 1024 * 1024, int(os.getenv("MAX_OFFICE_OCR_TOTAL_BYTES", str(256 * 1024 * 1024)))),
)
PARSE_TEMP_DISK_RESERVE_BYTES = Config.PARSE_TEMP_DISK_RESERVE_BYTES
PARSE_TEMP_STALE_SECONDS = Config.PARSE_TEMP_STALE_SECONDS

_PARSE_TEMP_PREFIX = "sjfx-"
_OWNED_TEMP_RE = re.compile(r"^sjfx-(?:archive|pdf-repair)-p([0-9]+)-")

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".xlsm",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".log", ".yaml", ".yml",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".rar", ".7z",
}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path, block_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _digest_text(text):
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _short_text(text, limit=900):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _safe_archive_destination(temp_root, member_name):
    """Return a resolved extraction path that is guaranteed below ``temp_root``.

    Normalising ``..`` segments before writing is not sufficient: an archive can
    contain an absolute path, mixed separators, or a member whose parent is a
    symlink created by an earlier member.  Resolve the candidate both before and
    after creating parent directories and reject anything that escapes the
    private temporary root.
    """
    try:
        root = Path(temp_root).resolve()
        candidate = (root / str(member_name)).resolve(strict=False)
        candidate.relative_to(root)
        if candidate == root:
            return None
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def _open_archive_target(path):
    """Open an archive member without following a symlink at the target."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    # O_NOFOLLOW is available on Linux/macOS.  The resolve check above remains
    # the portable guard on platforms that do not expose it (notably Windows).
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags, 0o600)
    return os.fdopen(descriptor, "wb")


def _unlink_quietly(path):
    """Path.unlink(missing_ok=True) compatible with the Python 3.7 host."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _zip_eocd_metadata(path):
    """Read bounded EOCD/ZIP64 metadata without constructing ``ZipInfo`` objects."""
    path = Path(path)
    size = path.stat().st_size
    tail_size = min(size, 65535 + 22 + 20)
    with path.open("rb") as stream:
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
        search_end = len(tail)
        eocd_index = -1
        while search_end > 0:
            candidate = tail.rfind(b"PK\x05\x06", 0, search_end)
            if candidate < 0:
                break
            if candidate + 22 <= len(tail):
                comment_length = struct.unpack_from("<H", tail, candidate + 20)[0]
                if candidate + 22 + comment_length == len(tail):
                    eocd_index = candidate
                    break
            search_end = candidate
        if eocd_index < 0:
            return None
        eocd_offset = size - tail_size + eocd_index
        total_entries = struct.unpack_from("<H", tail, eocd_index + 10)[0]
        directory_size = struct.unpack_from("<I", tail, eocd_index + 12)[0]
        directory_offset = struct.unpack_from("<I", tail, eocd_index + 16)[0]
        zip64_required = (
            total_entries == 0xFFFF
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
        )
        if not zip64_required:
            return {
                "declared_entries": int(total_entries),
                "directory_size": int(directory_size),
                "directory_offset": int(directory_offset),
                "directory_end": int(eocd_offset),
                "zip64": False,
            }

        if eocd_offset < 20:
            return None
        stream.seek(eocd_offset - 20)
        locator = stream.read(20)
        if len(locator) != 20 or locator[:4] != b"PK\x06\x07":
            return None

        # ZIP64 permits an extensible data sector. Locate the record by scanning
        # a bounded footer window ending immediately before the locator instead
        # of trusting its relative offset (which changes for prepended archives).
        locator_offset = eocd_offset - 20
        search_size = min(locator_offset, 1024 * 1024)
        stream.seek(locator_offset - search_size)
        footer = stream.read(search_size)
        search_end = len(footer)
        while search_end > 0:
            candidate = footer.rfind(b"PK\x06\x06", 0, search_end)
            if candidate < 0:
                return None
            if candidate + 56 <= len(footer):
                record_size = struct.unpack_from("<Q", footer, candidate + 4)[0]
                if candidate + 12 + record_size == len(footer):
                    zip64 = footer[candidate:candidate + 56]
                    zip64_offset = locator_offset - search_size + candidate
                    return {
                        "declared_entries": int(struct.unpack_from("<Q", zip64, 32)[0]),
                        "directory_size": int(struct.unpack_from("<Q", zip64, 40)[0]),
                        "directory_offset": int(struct.unpack_from("<Q", zip64, 48)[0]),
                        "directory_end": int(zip64_offset),
                        "zip64": True,
                    }
            search_end = candidate
    return None


def _zip_declared_entry_count(path):
    """Compatibility helper backed by the hardened EOCD reader."""
    metadata = _zip_eocd_metadata(path)
    return metadata["declared_entries"] if metadata else None


def _zip_central_directory_preflight(path):
    """Stream-validate the central directory before ``ZipFile`` materialises it.

    The EOCD count is attacker-controlled. We therefore enforce a byte cap,
    walk every fixed central-directory header without retaining names/comments,
    and compare the observed count with the declaration.
    """
    metadata = _zip_eocd_metadata(path)
    if metadata is None:
        return {
            "safe": False, "reason": "central_directory_invalid",
            "declared_entries": 0, "observed_entries": 0, "directory_size": 0,
        }
    result = dict(metadata)
    result.update({"safe": False, "reason": None, "observed_entries": 0})
    directory_size = int(metadata["directory_size"])
    directory_end = int(metadata["directory_end"])
    directory_start = directory_end - directory_size
    if directory_size < 0 or directory_size > ZIP_MAX_CENTRAL_DIRECTORY_BYTES:
        result["reason"] = "central_directory_byte_limit"
        return result
    if directory_start < 0 or directory_end > Path(path).stat().st_size:
        result["reason"] = "central_directory_invalid"
        return result

    observed = 0
    try:
        with Path(path).open("rb") as stream:
            stream.seek(directory_start)
            cursor = directory_start
            while cursor < directory_end:
                fixed = stream.read(46)
                if len(fixed) != 46 or fixed[:4] != b"PK\x01\x02":
                    result["reason"] = "central_directory_invalid"
                    return result
                name_length, extra_length, comment_length = struct.unpack_from("<HHH", fixed, 28)
                variable_size = int(name_length) + int(extra_length) + int(comment_length)
                cursor += 46 + variable_size
                if cursor > directory_end:
                    result["reason"] = "central_directory_invalid"
                    return result
                stream.seek(variable_size, os.SEEK_CUR)
                observed += 1
                result["observed_entries"] = observed
                if observed > ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES:
                    result["reason"] = "central_directory_entry_limit"
                    return result
    except (OSError, OverflowError, struct.error):
        result["reason"] = "central_directory_invalid"
        return result

    if cursor != directory_end:
        result["reason"] = "central_directory_invalid"
    elif observed != int(metadata["declared_entries"]):
        result["reason"] = "central_directory_count_mismatch"
    else:
        result["safe"] = True
    return result


def _normalised_bounded_member_name(raw_name):
    name = posixpath.normpath(str(raw_name or "").replace("\\", "/")).lstrip("/")
    if not name or name == "." or name.startswith("../") or "/../" in name:
        return None
    parts = [part for part in name.split("/") if part not in {"", "."}]
    if max(0, len(parts) - 1) > ARCHIVE_MAX_MEMBER_PATH_DEPTH:
        return None
    return name


def _bounded_office_zip_inventory(path, archive, expected_count):
    """Return safe OOXML members under the same budgets as normal archives."""
    materialized_count = len(archive.filelist)
    if materialized_count != int(expected_count):
        raise ValueError("Office ZIP 中央目录在预检后发生变化")
    if materialized_count > ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES:
        raise ValueError("Office ZIP 成员数量超过安全上限")

    ratio_limit = max(1, int(Path(path).stat().st_size * ARCHIVE_MAX_COMPRESSION_RATIO))
    total_limit = min(ARCHIVE_MAX_TOTAL_BYTES, ratio_limit, OFFICE_OCR_TOTAL_BYTES)
    total_declared = 0
    safe = {}
    warnings = []
    duplicate_names = set()
    for info in archive.filelist:
        if info.is_dir():
            continue
        name = _normalised_bounded_member_name(info.filename)
        if name is None:
            warnings.append("已跳过路径不安全或过深的 Office 成员：{}".format(str(info.filename)[:160]))
            continue
        if name in safe:
            duplicate_names.add(name)
            safe.pop(name, None)
            warnings.append("已跳过名称重复的 Office 成员：{}".format(name))
            continue
        if name in duplicate_names:
            continue
        if info.flag_bits & 0x1:
            warnings.append("已跳过需要密码的加密 Office 成员：{}".format(name))
            continue
        declared_size = int(info.file_size)
        compressed_size = int(info.compress_size)
        ratio = float("inf") if declared_size > 0 and compressed_size <= 0 else (
            declared_size / float(compressed_size or 1)
        )
        if declared_size < 0 or declared_size > ARCHIVE_MAX_MEMBER_BYTES:
            warnings.append("已跳过超过成员大小上限的 Office 成员：{}".format(name))
            continue
        if declared_size > 0 and ratio > ARCHIVE_MAX_COMPRESSION_RATIO:
            warnings.append("已跳过压缩比超过安全上限的 Office 成员：{}".format(name))
            continue
        if total_declared + declared_size > total_limit:
            warnings.append("已跳过超过累计解压预算的 Office 成员：{}".format(name))
            continue
        total_declared += declared_size
        safe[name] = info
    return safe, warnings, total_limit


def _read_zip_info_bounded(archive, info, per_member_limit, remaining_total):
    """Read one already-validated ZIP member without an unbounded ``read()``."""
    allowed = min(
        ARCHIVE_MAX_MEMBER_BYTES,
        max(0, int(per_member_limit)),
        max(0, int(remaining_total)),
    )
    if int(info.file_size) > allowed:
        raise ValueError("成员声明大小超过内存读取预算")
    payload = bytearray()
    with archive.open(info) as source:
        while True:
            chunk = source.read(min(1024 * 1024, allowed - len(payload) + 1))
            if not chunk:
                break
            if len(payload) + len(chunk) > allowed:
                raise ValueError("成员实际解压大小超过内存读取预算")
            payload.extend(chunk)
    return bytes(payload)


def _preflight_office_package(path):
    """Reject unsafe OOXML containers before Docling/openpyxl sees them."""
    central_directory = _zip_central_directory_preflight(path)
    if not central_directory.get("safe"):
        raise ValueError(
            "Office ZIP 中央目录预检失败：{}".format(
                central_directory.get("reason") or "central_directory_invalid"
            )
        )
    with zipfile.ZipFile(str(path)) as archive:
        safe, warnings, _total_limit = _bounded_office_zip_inventory(
            path, archive, central_directory.get("observed_entries") or 0,
        )
        file_count = sum(1 for info in archive.filelist if not info.is_dir())
        if warnings or len(safe) != file_count:
            raise ValueError((warnings or ["Office ZIP 成员未通过安全预算"])[0])
    return central_directory


def _configured_parse_temp_root():
    """Return the private, configurable parsing scratch root."""
    root = Path(os.getenv("SJFX_PARSE_TEMP_DIR", str(Config.PARSE_TEMP_DIR))).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            root.chmod(0o700)
        except OSError:
            pass
    return root


def _pid_is_alive(pid):
    if pid == os.getpid():
        return True
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        return exc.errno not in {errno.ESRCH, errno.EINVAL}


def cleanup_stale_parse_temp_dirs(temp_root=None, stale_seconds=None):
    """Remove abandoned parser scratch objects without touching live workers.

    Parser subprocesses include their PID in every scratch name. A parent or a
    later worker can therefore clean objects left by a hard timeout immediately
    after the owner exits. Legacy scratch names are removed only after the
    configured age threshold. Only children below the configured root and with
    an application-owned prefix are ever considered.
    """
    root = Path(temp_root or _configured_parse_temp_root()).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return 0
    threshold = PARSE_TEMP_STALE_SECONDS if stale_seconds is None else max(0, float(stale_seconds))
    now = time.time()
    removed = 0
    try:
        with os.scandir(str(root)) as iterator:
            for entry in iterator:
                child = Path(entry.path)
                if not child.name.startswith(_PARSE_TEMP_PREFIX):
                    continue
                try:
                    child.resolve(strict=False).relative_to(root)
                except (OSError, RuntimeError, ValueError):
                    continue
                owner = _OWNED_TEMP_RE.match(child.name)
                try:
                    age = max(0.0, now - child.lstat().st_mtime)
                except OSError:
                    continue
                if owner:
                    should_remove = not _pid_is_alive(int(owner.group(1)))
                else:
                    should_remove = age >= threshold
                if not should_remove:
                    continue
                try:
                    if child.is_symlink() or not child.is_dir():
                        child.unlink()
                    else:
                        shutil.rmtree(str(child))
                    removed += 1
                except OSError:
                    continue
    except OSError:
        return removed
    return removed


def _get_value(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _label_name(value):
    if value is None:
        return "text"
    return str(getattr(value, "value", value))


def _tableformer_available(artifacts_path):
    if not artifacts_path:
        return False
    root = Path(artifacts_path) / "docling-project--docling-models" / "model_artifacts" / "tableformer"
    for mode, filename in (("accurate", "tableformer_accurate.safetensors"), ("fast", "tableformer_fast.safetensors")):
        folder = root / mode
        weights = folder / filename
        # A partially copied safetensors file exists but cannot be loaded. The
        # lower bounds are deliberately conservative for Docling's shipped
        # TableFormer weights and keep an interrupted personal upload safe.
        minimum_size = 100 * 1024 * 1024 if mode == "accurate" else 80 * 1024 * 1024
        if weights.exists() and weights.stat().st_size >= minimum_size and (folder / "tm_config.json").exists():
            return True
    return False


def _docling_artifacts_ready(artifacts_path):
    """Require the local layout model before enabling Docling.

    Importing Docling alone is not enough: a partial cache used to make the
    first high-accuracy parse attempt an inaccessible Hugging Face download.
    """
    if not artifacts_path:
        return False
    root = Path(artifacts_path)
    layout = root / "docling-project--docling-layout-heron"
    weights = layout / "model.safetensors"
    return (
        (layout / "config.json").exists()
        and weights.exists()
        and weights.stat().st_size >= 150 * 1024 * 1024
    )


class DocumentParseError(RuntimeError):
    """A source document could not be parsed after all local fallbacks."""


def _looks_like_corrupt_pdf_warning(warnings):
    signals = (
        "EOF marker", "xref", "trailer dictionary", "Data format error",
        "Stream has ended unexpectedly", "not valid",
    )
    text = "\n".join(str(item) for item in warnings or [])
    return any(signal.lower() in text.lower() for signal in signals)


class UnifiedDocumentParser:
    """Docling-first parser with an explicit, auditable local fallback.

    One converter is reused because Docling model initialization is expensive.
    The converter is protected by a lock; package-level callers may still hash and
    organise documents concurrently without racing the native OCR pipeline.
    """

    def __init__(self, artifacts_path=None, rapidocr_model_dir=None, max_chars=2_000_000, fast_office_ocr=True):
        self.artifacts_path = Path(artifacts_path) if artifacts_path else None
        self.rapidocr_model_dir = Path(rapidocr_model_dir) if rapidocr_model_dir else None
        self.max_chars = max_chars
        self.fast_office_ocr = bool(fast_office_ocr)
        # Docling must not opportunistically claim the GPU that is reserved for
        # the local language model.  CPU is the safe default; a deployment that
        # has a separate GPU budget may opt in explicitly with DOCLING_DEVICE.
        requested_device = str(os.getenv("DOCLING_DEVICE", "cpu")).strip().lower()
        self.docling_device = requested_device if requested_device in {"cpu", "cuda", "auto"} else "cpu"
        try:
            requested_threads = int(os.getenv("DOCLING_CPU_THREADS", "4"))
        except (TypeError, ValueError):
            requested_threads = 4
        self.docling_cpu_threads = max(1, min(32, requested_threads))
        self._converter = None
        self._converter_error = None
        self._ocr_engine = None
        self._lock = threading.Lock()
        self.parse_temp_root = _configured_parse_temp_root()
        # A SIGKILL/timeout cannot run a child's finally block. Reaping dead
        # PID-owned objects on each parser construction gives the parent and
        # the next worker a deterministic recovery path.
        cleanup_stale_parse_temp_dirs(self.parse_temp_root)
        # These flags also cover direct use outside app.py/config.py.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    @property
    def docling_available(self):
        if not _docling_artifacts_ready(self.artifacts_path):
            return False
        try:
            import docling  # noqa: F401
            return True
        except ImportError:
            return False

    def status(self):
        try:
            from importlib.metadata import PackageNotFoundError, version
        except ImportError:  # Python 3.7 local development compatibility
            try:
                from importlib_metadata import PackageNotFoundError, version
            except ImportError:
                PackageNotFoundError = Exception

                def version(_name):
                    raise PackageNotFoundError()
        versions = {}
        for module_name in ("docling", "rapidocr", "onnxruntime"):
            try:
                versions[module_name] = version(module_name)
            except PackageNotFoundError:
                versions[module_name] = None
        models = {}
        if self.rapidocr_model_dir:
            for name in ("ch_PP-OCRv5_det_mobile.onnx", "ch_PP-OCRv5_rec_mobile.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx"):
                models[name] = (self.rapidocr_model_dir / name).exists()
        tableformer_ready = _tableformer_available(self.artifacts_path)
        return {
            "primary_parser": "Docling",
            "ocr_engine": "RapidOCR (ONNX Runtime)",
            "available": bool(versions["docling"]) and _docling_artifacts_ready(self.artifacts_path),
            "versions": versions,
            "remote_services_enabled": False,
            "artifacts_path": str(self.artifacts_path) if self.artifacts_path else None,
            "rapidocr_models": models,
            "tableformer_ready": tableformer_ready,
            "offline_only": True,
            "docling_device": self.docling_device,
            "docling_cpu_threads": self.docling_cpu_threads,
            "ocr_device": "cpu (ONNX Runtime)",
            "local_artifacts_ready": _docling_artifacts_ready(self.artifacts_path),
            "initialization_error": self._converter_error,
        }

    def _build_converter(self):
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

        pdf_options = PdfPipelineOptions()
        pdf_options.do_ocr = True
        # Docling's current default is ``auto``.  Set it explicitly so a model
        # upgrade cannot silently move layout/table inference onto the Ollama
        # GPU.  The assignment is guarded for compatibility with older Docling
        # releases that did not expose accelerator_options.
        accelerator_options = getattr(pdf_options, "accelerator_options", None)
        if accelerator_options is not None:
            if hasattr(accelerator_options, "device"):
                accelerator_options.device = self.docling_device
            if hasattr(accelerator_options, "num_threads"):
                accelerator_options.num_threads = self.docling_cpu_threads
        local_artifacts_ready = _docling_artifacts_ready(self.artifacts_path)
        tableformer_ready = _tableformer_available(self.artifacts_path)
        # Missing table artifacts are a feature reduction, not permission to
        # fetch them from the network during a user analysis job.
        pdf_options.do_table_structure = tableformer_ready
        if tableformer_ready and self.artifacts_path:
            accurate = self.artifacts_path / "docling-project--docling-models" / "model_artifacts" / "tableformer" / "accurate" / "tableformer_accurate.safetensors"
            pdf_options.table_structure_options.mode = TableFormerMode.ACCURATE if accurate.exists() else TableFormerMode.FAST
        pdf_options.enable_remote_services = False
        # Docling 2.119 enables torch.compile for the Heron layout model by
        # default. On a clean Windows demo host that would require MSVC "cl".
        # Disable compilation so inference stays portable and CPU-only.
        if hasattr(pdf_options, "layout_options") and hasattr(pdf_options.layout_options, "engine_options"):
            if hasattr(pdf_options.layout_options.engine_options, "compile_model"):
                pdf_options.layout_options.engine_options.compile_model = False
        if local_artifacts_ready:
            pdf_options.artifacts_path = self.artifacts_path
        model_dir = self.rapidocr_model_dir
        det = model_dir / "ch_PP-OCRv5_det_mobile.onnx" if model_dir else None
        rec = model_dir / "ch_PP-OCRv5_rec_mobile.onnx" if model_dir else None
        cls = model_dir / "ch_ppocr_mobile_v2.0_cls_mobile.onnx" if model_dir else None
        ocr_kwargs = {"force_full_page_ocr": False}
        if det and rec and cls and det.exists() and rec.exists() and cls.exists():
            ocr_kwargs.update({"det_model_path": str(det), "rec_model_path": str(rec), "cls_model_path": str(cls)})
        try:
            pdf_options.ocr_options = RapidOcrOptions(**ocr_kwargs)
        except TypeError:
            ocr_kwargs.pop("force_full_page_ocr", None)
            pdf_options.ocr_options = RapidOcrOptions(**ocr_kwargs)
        return DocumentConverter(
            allowed_formats=[
                InputFormat.PDF, InputFormat.IMAGE, InputFormat.DOCX,
                InputFormat.PPTX, InputFormat.XLSX, InputFormat.HTML,
                InputFormat.MD, InputFormat.CSV,
            ],
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
                InputFormat.IMAGE: ImageFormatOption(pipeline_options=pdf_options),
            },
        )

    def _get_converter(self):
        if self._converter is not None:
            return self._converter
        if self._converter_error:
            raise RuntimeError(self._converter_error)
        try:
            self._converter = self._build_converter()
            return self._converter
        except Exception as exc:
            self._converter_error = "Docling 初始化失败：{}".format(exc)
            raise RuntimeError(self._converter_error) from exc

    def _get_ocr_engine(self):
        if self._ocr_engine is not None:
            return self._ocr_engine
        from rapidocr import RapidOCR
        # RapidOCR's ONNX provider defaults to CPU today, but an environment
        # with onnxruntime-gpu can otherwise select CUDA automatically after a
        # dependency upgrade.  Pin every accelerator switch off for the OCR
        # engine; the language model owns the GPU budget.
        params = {
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.use_dml": False,
            "EngineConfig.onnxruntime.use_cann": False,
            "EngineConfig.onnxruntime.use_coreml": False,
        }
        if self.rapidocr_model_dir:
            mapping = {
                "Det.model_path": "ch_PP-OCRv5_det_mobile.onnx",
                "Rec.model_path": "ch_PP-OCRv5_rec_mobile.onnx",
                "Cls.model_path": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            }
            for key, name in mapping.items():
                model = self.rapidocr_model_dir / name
                if model.exists():
                    params[key] = str(model)
        try:
            self._ocr_engine = RapidOCR(params=params or None)
        except (KeyError, TypeError, ValueError):
            # Older RapidOCR releases did not expose EngineConfig switches;
            # their ONNX backend is CPU-only.  Keep compatibility with those
            # releases while retaining the explicit settings on current ones.
            self._ocr_engine = RapidOCR(params=None)
        return self._ocr_engine

    def _repair_pdf_copy(self, source):
        """Attempt an isolated local PDF repair without altering the source.

        qpdf is preferred because it can reconstruct some damaged xref tables.
        ``pikepdf`` is used when the deployment ships it.  A failed repair is
        deliberately not hidden: callers receive a diagnosis and the original
        follows the usual parser fallbacks.
        """
        source = Path(source)
        # Avoid a repair subprocess for PDFs that pypdf can already read.
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(source), strict=False)
            _ = len(reader.pages)
            return None, None
        except Exception as original_error:
            reason = "PDF 结构校验失败：{}".format(str(original_error)[:300])

        fd, temp_name = tempfile.mkstemp(
            prefix="sjfx-pdf-repair-p{}-".format(os.getpid()),
            suffix=".pdf",
            dir=str(self.parse_temp_root),
        )
        os.close(fd)
        repaired = Path(temp_name)
        try:
            qpdf = shutil.which("qpdf")
            if qpdf:
                result = subprocess.run(
                    [qpdf, "--warning-exit-0", "--linearize", str(source), str(repaired)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90,
                )
                if result.returncode == 0 and repaired.stat().st_size > 0:
                    return repaired, "检测到损坏 PDF，已使用 qpdf 重建副本后解析。"

            try:
                import pikepdf
                with pikepdf.open(str(source), attempt_recovery=True) as document:
                    document.save(str(repaired))
                if repaired.stat().st_size > 0:
                    return repaired, "检测到损坏 PDF，已使用 pikepdf 恢复副本后解析。"
            except Exception:
                pass
            _unlink_quietly(repaired)
            return None, reason + "；本地恢复工具未能重建该文件。请重新获取或重新导出原始 PDF。"
        except (OSError, subprocess.SubprocessError) as exc:
            _unlink_quietly(repaired)
            return None, reason + "；PDF 恢复尝试失败：{}".format(str(exc)[:200])

    def parse(self, path, relative_path=None, mode="accurate", _archive_depth=0):
        path = Path(path).resolve()
        file_size = path.stat().st_size
        ext = path.suffix.lower()
        archive_name = path.name.lower()
        is_archive = ext in ARCHIVE_EXTENSIONS or archive_name.endswith(".tar.gz") or archive_name.endswith(".tar.bz2")
        sensitive_content = is_sensitive_file(path.name)
        # Resolve the budgets at parse time so tests and supervised workers may
        # lower them without restarting. Both remain bounded by the central
        # 10 GiB product contract in config.content_byte_limit().
        max_single_file_bytes = content_byte_limit("MAX_SINGLE_FILE_BYTES")
        max_archive_file_bytes = content_byte_limit("MAX_ARCHIVE_FILE_BYTES")
        effective_file_limit = max_archive_file_bytes if is_archive else max_single_file_bytes
        relative_path = str(relative_path or path.name).replace("\\", "/")
        mode = "fast" if str(mode).lower() == "fast" else "accurate"
        over_size_limit = file_size > effective_file_limit
        # Reject by inventory size before a 10+ GiB sequential read.  Accepted
        # inputs are hashed as part of the authoritative parse contract.
        source_hash = "" if over_size_limit else sha256_file(path)
        base = {
            "schema_version": "unified-document/1.0",
            "source": {
                "path": relative_path,
                "absolute_path": str(path),
                "name": path.name,
                "extension": path.suffix.lower(),
                "size": file_size,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
                "sha256": source_hash,
                "sensitive": sensitive_content,
                "content_policy": "metadata_only_sensitive" if sensitive_content else "parse_if_supported",
            },
            "parsed_at": utc_now(),
            "parser": {},
            "structure": {"title": path.stem, "headings": [], "page_count": None, "table_count": 0, "picture_count": 0},
            "text": "",
            "content_sha256": "",
            "coverage": {
                "extracted_characters": 0,
                "stored_characters": 0,
                "embedded_ocr_characters": 0,
                "complete": True,
                "truncated_by_limit": False,
                "limited_by_fast_mode": False,
                "coverage_ratio": 1.0,
            },
            "evidence": [],
            "warnings": [],
        }
        if over_size_limit:
            base["parser"] = {"name": "metadata-only", "degraded": True, "ocr": False}
            base["coverage"].update({"complete": False, "coverage_ratio": 0.0, "limited_by_size": True})
            limit_label = "压缩容器" if is_archive else "单文件"
            base["warnings"].append("文件超过{}解析上限（{} 字节），已在读取正文或计算哈希前拒绝。".format(limit_label, effective_file_limit))
            return base
        if sensitive_content:
            base["parser"] = {"name": "metadata-only", "degraded": False, "ocr": False}
            base["coverage"].update({
                "complete": False,
                "coverage_ratio": 0.0,
                "content_restricted": True,
                "restriction_reason": "sensitive_metadata_only",
            })
            base["warnings"].append("敏感文件已纳入物理清单，但依据安全策略仅保留元数据和完整性哈希，不读取正文、不送入模型。")
            return base
        if ext not in SUPPORTED_EXTENSIONS:
            base["parser"] = {"name": "metadata-only", "degraded": True, "ocr": False}
            base["warnings"].append("该文件类型暂不支持正文解析，仅保留元数据与源文件哈希。")
            return base

        # OOXML files are ZIP containers even though users perceive them as
        # documents. Validate their complete member inventory before any
        # third-party parser can materialise an oversized/deceptive member.
        if ext in {".docx", ".pptx", ".xlsx", ".xlsm"}:
            try:
                _preflight_office_package(path)
            except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
                base["parser"] = {"name": "metadata-only", "degraded": True, "ocr": False}
                base["coverage"].update({
                    "complete": False,
                    "coverage_ratio": 0.0,
                    "content_restricted": True,
                    "restriction_reason": "unsafe_office_archive",
                })
                base["warnings"].append(
                    "Office 文档未通过压缩容器安全预检，未读取正文或送入模型：{}".format(
                        str(exc)[:300]
                    )
                )
                return base

        # A surprising number of field PDFs are interrupted copies: their page
        # streams may still be present, but the xref/trailer is missing.  Do
        # not give a corrupted original to Docling first.  Try a local repair
        # tool, then parse the repaired *temporary* copy while retaining the
        # original source hash and provenance in ``base``.
        parse_path = path
        repaired_path = None
        if ext == ".pdf":
            repaired_path, repair_note = self._repair_pdf_copy(path)
            if repair_note:
                base["warnings"].append(repair_note)
            if repaired_path:
                parse_path = repaired_path
                base["parser"]["repair_attempted"] = True
                base["parser"]["repair_succeeded"] = True

        if is_archive:
            self._parse_archive(path, base, mode=mode, archive_depth=_archive_depth)
            base["content_sha256"] = _digest_text(base["text"])
            base["coverage"]["stored_characters"] = len(base["text"])
            archive_manifest = base.get("archive_manifest") or {}
            archive_complete = archive_manifest.get("coverage_status") == "complete"
            base["coverage"]["complete"] = bool(
                archive_complete and not base["coverage"].get("truncated_by_limit")
            )
            base["coverage"]["archive"] = archive_manifest
            base["coverage"]["coverage_ratio"] = archive_manifest.get("member_coverage_ratio", 0.0)
            if not base["evidence"] and base["text"].strip():
                self._add_fallback_evidence(base)
            return base

        if mode == "fast":
            self._fast_parse(parse_path, base)
            base["parser"]["mode"] = "fast"
        # Plain text formats do not benefit from layout models; they are still
        # normalised into the exact same schema and evidence representation.
        docling_formats = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".html", ".htm", ".md", ".csv"}
        if mode != "fast" and ext in docling_formats and self.docling_available:
            try:
                with self._lock:
                    result = self._get_converter().convert(str(parse_path))
                document = result.document
                text = document.export_to_markdown() or document.export_to_text()
                base["text"] = str(text or "")[: self.max_chars]
                base["coverage"]["extracted_characters"] = len(str(text or ""))
                base["parser"] = {
                    "name": "Docling",
                    "degraded": False,
                    "ocr": ext in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"},
                    "mode": "accurate",
                    "remote_services_enabled": False,
                }
                self._extract_docling_items(document, base)
                if ext in {".docx", ".pptx", ".xlsx", ".xlsm"}:
                    self._rapidocr_office_images(path, base)
                if len(str(text or "")) > self.max_chars:
                    base["coverage"]["truncated_by_limit"] = True
                    base["warnings"].append("统一正文超过演示版字符上限，证据项仍按已解析结构保留。")
            except Exception as exc:
                base["warnings"].append("Docling 解析失败，已切换本地兼容解析器：{}".format(exc))
                self._fallback(parse_path, base)
        elif mode != "fast":
            if ext in docling_formats and not self.docling_available:
                base["warnings"].append("Docling 未安装，已切换本地兼容解析器。")
            self._fallback(parse_path, base)
            base["parser"]["mode"] = "accurate-fallback"

        if repaired_path:
            try:
                _unlink_quietly(repaired_path)
            except OSError:
                pass
        if ext == ".pdf" and not base["text"].strip() and _looks_like_corrupt_pdf_warning(base["warnings"]):
            raise DocumentParseError(
                "PDF 文件损坏或传输不完整，Docling/PDFium/OCR 均无法读取。"
                "请重新下载或重新导出原文件；系统未将其误标为已解析。"
            )
        base["content_sha256"] = _digest_text(base["text"])
        base["coverage"]["stored_characters"] = len(base["text"])
        total_available = base["coverage"].get("extracted_characters", 0) + base["coverage"].get("embedded_ocr_characters", 0)
        if not total_available:
            total_available = len(base["text"])
            base["coverage"]["extracted_characters"] = total_available
        base["coverage"]["complete"] = (
            not base["coverage"].get("truncated_by_limit")
            and not base["coverage"].get("limited_by_fast_mode")
            and len(base["text"]) >= total_available
        )
        if not base["coverage"].get("limited_by_fast_mode"):
            base["coverage"]["coverage_ratio"] = round(min(1.0, len(base["text"]) / float(total_available or 1)), 6)
        if not base["evidence"] and base["text"].strip():
            self._add_fallback_evidence(base)
        try:
            profile = profile_path(path)
            if profile:
                base["data_profile"] = profile
                for column_name, column in list((profile.get("columns") or {}).items())[:40]:
                    if len(base["evidence"]) >= 3000:
                        break
                    base["evidence"].append({
                        "evidence_id": "E-{}-{:05d}".format(base["source"]["sha256"][:10], len(base["evidence"]) + 1),
                        "source_path": base["source"]["path"], "label": "structured_column",
                        "table": "主数据表", "column": column_name,
                        "row_range": [2, int(profile.get("row_count") or 1) + 1],
                        "text": "字段 {}：类型={}，缺失率={}，唯一值={}。".format(column_name, column.get("inferred_type"), column.get("missing_ratio"), column.get("unique_count")),
                        "source_sha256": base["source"]["sha256"], "content_sha256": _digest_text(column_name),
                    })
                judgment = profile.get("value_judgment") or {}
                if judgment.get("reason"):
                    base["warnings"].append("数据画像：{}".format(judgment["reason"]))
        except Exception as exc:
            base["warnings"].append("结构化数据画像失败，正文解析仍可用：{}".format(exc))
        return base

    def _parse_archive(self, path, base, mode="accurate", archive_depth=0):
        """Parse supported archive members inside a private, bounded scratch root."""
        base["parser"] = {"name": "archive-members", "degraded": False, "archive": True, "mode": mode}
        base["structure"]["archive_member_count"] = 0
        base["structure"]["archive_members"] = []
        base["archive_manifest"] = {
            "container_path": base.get("source", {}).get("path"),
            "total_members": 0,
            "parsed_members": 0,
            "skipped_members": 0,
            "failed_members": 0,
            "truncated_members": 0,
            "coverage_status": "partial",
            "member_coverage_ratio": 0.0,
            "skip_reasons": {},
            "member_records": [],
            "member_records_truncated": False,
            "encrypted_members": 0,
            "inventory_truncated": False,
            "limits": {
                "max_entries": ARCHIVE_MAX_ENTRIES,
                "max_zip_central_directory_entries": ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES,
                "max_member_bytes": ARCHIVE_MAX_MEMBER_BYTES,
                "max_uncompressed_bytes": ARCHIVE_MAX_TOTAL_BYTES,
                "max_compression_ratio": ARCHIVE_MAX_COMPRESSION_RATIO,
                "max_member_path_depth": ARCHIVE_MAX_MEMBER_PATH_DEPTH,
            },
        }
        if archive_depth >= ARCHIVE_MAX_DEPTH:
            base["parser"]["degraded"] = True
            base["warnings"].append("压缩包嵌套层级达到安全上限，未继续展开。")
            return

        cleanup_stale_parse_temp_dirs(self.parse_temp_root)
        temp_root = Path(tempfile.mkdtemp(
            prefix="sjfx-archive-p{}-".format(os.getpid()),
            dir=str(self.parse_temp_root),
        ))
        if os.name != "nt":
            try:
                temp_root.chmod(0o700)
            except OSError:
                pass
        try:
            name = path.name.lower()
            if name.endswith(".zip"):
                central_directory = _zip_central_directory_preflight(path)
                declared_hint = int(central_directory.get("declared_entries") or 0)
                observed_hint = int(central_directory.get("observed_entries") or 0)
                if not central_directory.get("safe"):
                    manifest = base["archive_manifest"]
                    total_hint = max(declared_hint, observed_hint)
                    reason = central_directory.get("reason") or "central_directory_invalid"
                    manifest["declared_total_members"] = declared_hint
                    manifest["observed_central_directory_members"] = observed_hint
                    manifest["central_directory_bytes"] = int(central_directory.get("directory_size") or 0)
                    manifest["total_members"] = total_hint
                    manifest["skipped_members"] = total_hint
                    manifest["inventory_truncated"] = True
                    manifest["member_records_truncated"] = True
                    manifest["skip_reasons"][reason] = max(1, total_hint)
                    base["parser"]["degraded"] = True
                    base["warnings"].append(
                        "ZIP 中央目录预检失败（声明 {} 个、观察 {} 个、{} 字节、原因 {}），"
                        "已在加载条目清单前拒绝。".format(
                            declared_hint, observed_hint,
                            int(central_directory.get("directory_size") or 0), reason,
                        )
                    )
                    return
                with zipfile.ZipFile(str(path)) as archive:
                    # Revalidate after the standard library has parsed the
                    # directory. This catches a file replacement/mutation
                    # between preflight and open without creating another list.
                    materialized_count = len(archive.filelist)
                    if (
                        materialized_count > ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES
                        or materialized_count != observed_hint
                    ):
                        manifest = base["archive_manifest"]
                        manifest["declared_total_members"] = declared_hint
                        manifest["observed_central_directory_members"] = observed_hint
                        manifest["materialized_central_directory_members"] = materialized_count
                        manifest["total_members"] = max(declared_hint, observed_hint, materialized_count)
                        manifest["skipped_members"] = manifest["total_members"]
                        manifest["inventory_truncated"] = True
                        manifest["member_records_truncated"] = True
                        reason = (
                            "central_directory_entry_limit"
                            if materialized_count > ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES
                            else "central_directory_changed"
                        )
                        manifest["skip_reasons"][reason] = max(1, manifest["total_members"])
                        base["parser"]["degraded"] = True
                        base["warnings"].append("ZIP 中央目录在预检后发生变化，已拒绝解析。")
                        return
                    # ZipFile necessarily reads its central directory on open.
                    # Do not create a second unbounded list via infolist();
                    # retain only the bounded prefix plus one sentinel.
                    entries = []
                    declared_total = 0
                    for info in archive.filelist:
                        if info.is_dir():
                            continue
                        declared_total += 1
                        if len(entries) <= ARCHIVE_MAX_ENTRIES:
                            entries.append({
                                "name": info.filename,
                                "declared_size": int(info.file_size),
                                "compressed_size": int(info.compress_size),
                                "encrypted": bool(info.flag_bits & 0x1),
                                "opener": lambda info=info: archive.open(info),
                            })
                    truncated = declared_total > ARCHIVE_MAX_ENTRIES
                    base["archive_manifest"]["inventory_truncated"] = truncated
                    base["archive_manifest"]["declared_total_members"] = declared_total
                    self._parse_archive_entries(
                        path, base, entries, temp_root, mode, archive_depth,
                        declared_total_members=declared_total,
                    )
                    return
            if name.endswith(".gz") and not name.endswith((".tar.gz", ".tgz")):
                self._parse_archive_entries(
                    path, base,
                    [{
                        "name": Path(path.stem).name + ".txt",
                        "declared_size": 0,
                        "compressed_size": int(path.stat().st_size),
                        "encrypted": False,
                        "opener": lambda: gzip.open(str(path), "rb"),
                    }],
                    temp_root, mode, archive_depth,
                )
                return
            if name.endswith(".bz2") and not name.endswith((".tar.bz2", ".tbz")):
                self._parse_archive_entries(
                    path, base,
                    [{
                        "name": Path(path.stem).name + ".txt",
                        "declared_size": 0,
                        "compressed_size": int(path.stat().st_size),
                        "encrypted": False,
                        "opener": lambda: bz2.open(str(path), "rb"),
                    }],
                    temp_root, mode, archive_depth,
                )
                return
            if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz")):
                with tarfile.open(str(path), "r:*") as archive:
                    # getmembers() materialises an unbounded TAR inventory.
                    # Stop after the cap plus one sentinel and mark the count as
                    # a lower bound when more members exist.
                    entries = []
                    for info in archive:
                        if not info.isfile():
                            continue
                        entries.append({
                            "name": info.name,
                            "declared_size": int(info.size),
                            "compressed_size": None,
                            "encrypted": False,
                            "opener": lambda info=info, archive=archive: archive.extractfile(info),
                        })
                        if len(entries) > ARCHIVE_MAX_ENTRIES:
                            base["archive_manifest"]["inventory_truncated"] = True
                            base["archive_manifest"]["total_members_is_lower_bound"] = True
                            break
                    self._parse_archive_entries(path, base, entries, temp_root, mode, archive_depth)
                    return
            base["parser"]["degraded"] = True
            base["warnings"].append("当前环境未安装 RAR/7z 解包组件；仅保留压缩包元数据。")
        except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, tarfile.TarError) as exc:
            base["parser"]["degraded"] = True
            if isinstance(exc, zipfile.BadZipFile):
                base["warnings"].append(
                    "ZIP 中央目录不可用：文件可能仍在复制、已经损坏、被加密或属于未收齐的分卷压缩包（{}）".format(exc)
                )
            else:
                base["warnings"].append("压缩包展开失败：{}".format(exc))
        finally:
            shutil.rmtree(str(temp_root), ignore_errors=True)

    def _parse_archive_entries(self, path, base, entries, temp_root, mode, archive_depth,
                               declared_total_members=None):
        total_uncompressed = 0
        manifest = base["archive_manifest"]
        declared_total = len(entries) if declared_total_members is None else max(0, int(declared_total_members))
        manifest["total_members"] = max(len(entries), declared_total)
        ratio_total_limit = max(1, int(path.stat().st_size * ARCHIVE_MAX_COMPRESSION_RATIO))
        effective_total_limit = min(ARCHIVE_MAX_TOTAL_BYTES, ratio_total_limit)
        archive_warning_count = 0

        def warn(message):
            nonlocal archive_warning_count
            archive_warning_count += 1
            if archive_warning_count <= 200:
                base["warnings"].append(message)
            elif archive_warning_count == 201:
                base["warnings"].append("压缩包成员告警超过 200 条，后续同类告警已折叠到清单统计。")

        def record(member_name, status, reason=None, detail=None):
            key = "{}_members".format(status)
            if key in manifest:
                manifest[key] += 1
            if reason:
                reasons = manifest["skip_reasons"]
                reasons[reason] = int(reasons.get(reason) or 0) + 1
            if len(manifest["member_records"]) < 500:
                item = {
                    "member": str(member_name)[:500],
                    "status": status,
                    "reason": reason,
                }
                if detail:
                    item["error"] = str(detail)[:300]
                manifest["member_records"].append(item)
            else:
                manifest["member_records_truncated"] = True

        for entry_index, entry in enumerate(entries):
            raw_name = entry.get("name")
            declared_size = int(entry.get("declared_size") or 0)
            compressed_size = entry.get("compressed_size")
            opener = entry.get("opener")
            if entry_index >= ARCHIVE_MAX_ENTRIES:
                warn("压缩包成员超过 {} 个，仅解析有界前缀。".format(ARCHIVE_MAX_ENTRIES))
                for remaining in entries[entry_index:]:
                    record(remaining.get("name"), "skipped", "member_count_limit")
                break

            member_name = posixpath.normpath(str(raw_name).replace("\\", "/")).lstrip("/")
            if not member_name or member_name == "." or member_name.startswith("../") or "/../" in member_name:
                warn("已跳过越界压缩包成员：{}".format(raw_name))
                record(raw_name, "skipped", "unsafe_path")
                continue
            member_parts = [part for part in member_name.split("/") if part not in {"", "."}]
            if max(0, len(member_parts) - 1) > ARCHIVE_MAX_MEMBER_PATH_DEPTH:
                warn("已跳过目录层级超过安全上限的压缩包成员：{}".format(member_name))
                record(member_name, "skipped", "member_path_depth_limit")
                continue
            if entry.get("encrypted"):
                manifest["encrypted_members"] += 1
                warn("已跳过需要密码的加密压缩包成员：{}".format(member_name))
                record(member_name, "skipped", "encrypted_member", "password required")
                continue
            if compressed_size is not None and declared_size > 0:
                ratio = float("inf") if int(compressed_size) <= 0 else declared_size / float(compressed_size)
                if ratio > ARCHIVE_MAX_COMPRESSION_RATIO:
                    warn("已跳过压缩比超过安全上限的成员：{}".format(member_name))
                    record(member_name, "skipped", "compression_ratio_limit")
                    continue
            if (
                declared_size < 0
                or declared_size > ARCHIVE_MAX_MEMBER_BYTES
                or total_uncompressed + declared_size > effective_total_limit
            ):
                reason = (
                    "compression_ratio_limit"
                    if total_uncompressed + declared_size > ratio_total_limit
                    else "size_limit"
                )
                warn("已跳过超过安全大小或压缩比限制的成员：{}".format(member_name))
                record(member_name, "skipped", reason)
                continue

            destination = _safe_archive_destination(temp_root, member_name)
            if destination is None:
                warn("已跳过解析后越界或含符号链接的压缩包成员：{}".format(raw_name))
                record(raw_name, "skipped", "unsafe_path")
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination = _safe_archive_destination(temp_root, member_name)
                if destination is None or destination.is_symlink():
                    raise ValueError("extraction target escaped temporary root")
                destination.parent.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError) as exc:
                warn("已跳过不安全的压缩包成员 {}：{}".format(member_name, str(exc)[:160]))
                record(member_name, "skipped", "unsafe_path", exc)
                continue

            try:
                free_for_parse = max(
                    0,
                    int(shutil.disk_usage(str(temp_root)).free) - PARSE_TEMP_DISK_RESERVE_BYTES,
                )
            except OSError:
                free_for_parse = 0
            if declared_size > 0 and declared_size > free_for_parse:
                warn("临时解析空间不足，已跳过压缩包成员：{}".format(member_name))
                record(member_name, "skipped", "temporary_space_limit")
                continue

            try:
                source = opener()
            except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
                message = str(exc).lower()
                encrypted = isinstance(exc, RuntimeError) and ("password" in message or "encrypted" in message)
                if encrypted:
                    manifest["encrypted_members"] += 1
                    warn("已跳过需要密码的加密压缩包成员：{}".format(member_name))
                    record(member_name, "skipped", "encrypted_member", exc)
                else:
                    warn("压缩包成员无法读取 {}：{}".format(member_name, str(exc)[:160]))
                    record(member_name, "failed", "member_unreadable", exc)
                continue
            if source is None:
                record(member_name, "failed", "member_unreadable")
                continue

            remaining_limits = [
                (ARCHIVE_MAX_MEMBER_BYTES, "size_limit"),
                (max(0, ARCHIVE_MAX_TOTAL_BYTES - total_uncompressed), "size_limit"),
                (max(0, ratio_total_limit - total_uncompressed), "compression_ratio_limit"),
                (free_for_parse, "temporary_space_limit"),
            ]
            allowed_member_bytes, stream_limit_reason = min(remaining_limits, key=lambda item: item[0])
            if allowed_member_bytes <= 0:
                try:
                    source.close()
                except Exception:
                    pass
                warn("安全预算不足，已跳过压缩包成员：{}".format(member_name))
                record(member_name, "skipped", stream_limit_reason)
                continue

            copied_bytes = 0
            exceeded_stream_limit = False
            try:
                with source, _open_archive_target(destination) as target:
                    while True:
                        chunk = source.read(min(1024 * 1024, allowed_member_bytes - copied_bytes + 1))
                        if not chunk:
                            break
                        if copied_bytes + len(chunk) > allowed_member_bytes:
                            exceeded_stream_limit = True
                            break
                        target.write(chunk)
                        copied_bytes += len(chunk)
            except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
                _unlink_quietly(destination)
                message = str(exc).lower()
                encrypted = isinstance(exc, RuntimeError) and ("password" in message or "encrypted" in message)
                if encrypted:
                    manifest["encrypted_members"] += 1
                    warn("已跳过需要密码的加密压缩包成员：{}".format(member_name))
                    record(member_name, "skipped", "encrypted_member", exc)
                else:
                    warn("压缩包成员 {} 写入失败：{}".format(member_name, str(exc)[:160]))
                    record(member_name, "failed", "extraction_error", exc)
                continue
            if exceeded_stream_limit:
                _unlink_quietly(destination)
                warn("已中止实际解压后超过限制的压缩包成员：{}".format(member_name))
                record(member_name, "skipped", stream_limit_reason)
                continue
            if _safe_archive_destination(temp_root, member_name) != destination:
                _unlink_quietly(destination)
                warn("已丢弃写入后越界的压缩包成员：{}".format(member_name))
                record(member_name, "skipped", "unsafe_path")
                continue

            actual_size = copied_bytes
            if actual_size > ARCHIVE_MAX_MEMBER_BYTES or total_uncompressed + actual_size > effective_total_limit:
                _unlink_quietly(destination)
                reason = (
                    "compression_ratio_limit"
                    if total_uncompressed + actual_size > ratio_total_limit
                    else "size_limit"
                )
                warn("已跳过实际解压后超过限制的成员：{}".format(member_name))
                record(member_name, "skipped", reason)
                continue

            total_uncompressed += actual_size
            base["structure"]["archive_members"].append(member_name)
            base["structure"]["archive_member_count"] += 1
            child_ext = destination.suffix.lower()
            if child_ext not in SUPPORTED_EXTENSIONS or child_ext in ARCHIVE_EXTENSIONS:
                record(member_name, "skipped", "unsupported_or_nested_type")
                _unlink_quietly(destination)
                continue
            try:
                child = self.parse(
                    destination,
                    relative_path=member_name,
                    mode=mode,
                    _archive_depth=archive_depth + 1,
                )
            except Exception as exc:
                _unlink_quietly(destination)
                warn("成员 {} 解析失败：{}".format(member_name, exc))
                record(member_name, "failed", "parse_error", exc)
                continue
            _unlink_quietly(destination)

            record(member_name, "parsed")
            if not (child.get("coverage") or {}).get("complete", True):
                manifest["truncated_members"] += 1
            child_text = str(child.get("text") or "")
            if child_text:
                block = "\n\n[压缩包成员：{}]\n{}".format(member_name, child_text)
                if len(base["text"]) + len(block) > self.max_chars:
                    base["coverage"]["truncated_by_limit"] = True
                base["text"] = (base["text"] + block)[: self.max_chars]
            for evidence in child.get("evidence") or []:
                item = dict(evidence)
                item["source_path"] = "{}::{}".format(path.name, member_name)
                item["archive_member"] = member_name
                item["archive_source_path"] = base["source"]["path"]
                base["evidence"].append(item)
                if len(base["evidence"]) >= 3000:
                    break
            if child.get("data_profile"):
                base.setdefault("data_profiles", []).append({
                    "member": member_name,
                    "profile": child["data_profile"],
                })
            if len(base["evidence"]) >= 3000:
                remaining = max(0, len(entries) - entry_index - 1)
                for remaining_entry in entries[entry_index + 1:]:
                    record(remaining_entry.get("name"), "skipped", "evidence_limit")
                if remaining:
                    warn("证据数量达到上限，后续 {} 个成员未继续解析。".format(remaining))
                break

        handled = manifest["parsed_members"] + manifest["skipped_members"] + manifest["failed_members"]
        if handled < manifest["total_members"]:
            remainder = manifest["total_members"] - handled
            reason = "member_count_limit" if manifest.get("inventory_truncated") else "not_processed"
            manifest["skipped_members"] += remainder
            manifest["skip_reasons"][reason] = int(manifest["skip_reasons"].get(reason) or 0) + remainder
            manifest["member_records_truncated"] = True
        manifest["member_coverage_ratio"] = round(
            manifest["parsed_members"] / float(manifest["total_members"] or 1), 6
        )
        manifest["coverage_status"] = (
            "complete"
            if manifest["total_members"] > 0
            and manifest["parsed_members"] == manifest["total_members"]
            and not manifest["failed_members"]
            and not manifest["skipped_members"]
            and not manifest["truncated_members"]
            and not manifest.get("inventory_truncated")
            and not base["coverage"].get("truncated_by_limit")
            else "partial"
        )
        if manifest["coverage_status"] != "complete":
            base["parser"]["degraded"] = True
            base["warnings"].append(
                "压缩包为部分覆盖：共 {} 个成员，解析 {} 个，跳过 {} 个，失败 {} 个，截断 {} 个。".format(
                    manifest["total_members"], manifest["parsed_members"], manifest["skipped_members"],
                    manifest["failed_members"], manifest["truncated_members"],
                )
            )
        if base["structure"]["archive_member_count"] == 0:
            base["parser"]["degraded"] = True
            base["warnings"].append("压缩包中没有找到可解析的文本/办公文档成员。")

    def _fast_parse(self, path, base):
        """Low-latency parsing for inventory and first-pass evidence discovery.

        Text PDFs and Office files use lightweight native readers. Images still
        use RapidOCR. Image-only PDFs OCR only a small leading sample and are
        explicitly marked incomplete so the UI cannot present them as fully read.
        """
        self._fallback(path, base, ocr_empty_pdf=False)
        ext = path.suffix.lower()
        base["warnings"].append("当前使用快速解析模式；未执行 Docling 版面模型或 TableFormer。")
        if self.fast_office_ocr and ext in {".docx", ".pptx", ".xlsx", ".xlsm"}:
            self._rapidocr_office_images(path, base)
        elif ext in {".docx", ".pptx", ".xlsx", ".xlsm"}:
            base["warnings"].append("快速模式已跳过 Office 内嵌图片 OCR；可切换高精度解析。")
            base["coverage"]["complete"] = False
            base["coverage"]["limited_by_fast_mode"] = True
            base["coverage"]["coverage_ratio"] = None
            base["coverage"]["coverage_ratio_reason"] = "快速模式跳过 Office 内嵌图片 OCR，无法从正文字符数估算完整覆盖率。"
        if ext == ".pdf" and not base["text"].strip():
            page_count = int(base.get("structure", {}).get("page_count") or 0)
            preview_pages = min(3, page_count) if page_count else 3
            try:
                processed = self._rapidocr_pdf(path, base, max_pages=preview_pages)
                if page_count and processed < page_count:
                    base["coverage"]["limited_by_fast_mode"] = True
                    base["coverage"]["coverage_ratio"] = round(processed / float(page_count), 6)
                    base["warnings"].append(
                        "扫描型 PDF 在快速模式下仅 OCR 前 {} 页；如需全文证据，请改用高精度解析。".format(processed)
                    )
            except Exception as exc:
                base["warnings"].append("快速模式扫描 PDF 预览 OCR 失败：{}".format(exc))
        base["parser"]["degraded"] = False
        base["parser"]["fast_preview"] = bool(base["coverage"].get("limited_by_fast_mode"))

    @staticmethod
    def _office_image_contexts(archive, ext, names, read_member):
        """Return media path -> best available Office location metadata.

        DOCX has no stable page number before pagination, so the nearest heading
        and paragraph ordinal are used. PPTX images are tied to a slide number.
        XLSX images retain their media name when worksheet drawing relationships
        cannot be resolved without executing Office.
        """
        contexts = {}
        names = set(names)
        if ext == ".docx" and "word/document.xml" in names:
            rels = {}
            rel_path = "word/_rels/document.xml.rels"
            if rel_path in names:
                root = ElementTree.fromstring(read_member(rel_path))
                for rel in root:
                    rel_id = rel.attrib.get("Id")
                    target = rel.attrib.get("Target", "")
                    if rel_id and "media/" in target:
                        rels[rel_id] = posixpath.normpath(posixpath.join("word", target))
            ns = {
                "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
                "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            }
            root = ElementTree.fromstring(read_member("word/document.xml"))
            current_heading = None
            paragraph_no = 0
            for paragraph in root.findall(".//w:body/w:p", ns):
                paragraph_no += 1
                text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
                style = paragraph.find("./w:pPr/w:pStyle", ns)
                style_value = style.attrib.get("{%s}val" % ns["w"], "") if style is not None else ""
                if text and ("heading" in style_value.lower() or "标题" in style_value):
                    current_heading = text[:160]
                for blip in paragraph.findall(".//a:blip", ns):
                    rel_id = blip.attrib.get("{%s}embed" % ns["r"])
                    media = rels.get(rel_id)
                    if media:
                        label = "第{}段内嵌图片".format(paragraph_no)
                        if current_heading:
                            label = "{} · {}".format(current_heading, label)
                        contexts[media] = {"page": None, "section": label}
        elif ext == ".pptx":
            ns = {
                "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            }
            for name in sorted(names):
                match = re.fullmatch(r"ppt/slides/_rels/slide(\d+)\.xml\.rels", name)
                if not match:
                    continue
                slide_no = int(match.group(1))
                slide_name = "ppt/slides/slide{}.xml".format(slide_no)
                title = ""
                if slide_name in names:
                    slide_root = ElementTree.fromstring(read_member(slide_name))
                    text_values = [node.text.strip() for node in slide_root.findall(".//a:t", ns) if node.text and node.text.strip()]
                    title = text_values[0][:120] if text_values else ""
                rel_root = ElementTree.fromstring(read_member(name))
                for rel in rel_root:
                    target = rel.attrib.get("Target", "")
                    if "media/" not in target:
                        continue
                    media = posixpath.normpath(posixpath.join(posixpath.dirname(slide_name), target))
                    section = "幻灯片 {}".format(slide_no)
                    if title:
                        section += " · " + title
                    contexts[media] = {"page": slide_no, "section": section}
        return contexts

    def _rapidocr_office_images(self, path, base):
        """OCR embedded Office images and merge them into the unified model."""
        ext = path.suffix.lower()
        prefixes = {
            ".docx": "word/media/",
            ".pptx": "ppt/media/",
            ".xlsx": "xl/media/",
            ".xlsm": "xl/media/",
        }
        prefix = prefixes.get(ext)
        if not prefix:
            return
        try:
            central_directory = _zip_central_directory_preflight(path)
            if not central_directory.get("safe"):
                raise ValueError(
                    "Office ZIP 中央目录预检失败：{}".format(
                        central_directory.get("reason") or "central_directory_invalid"
                    )
                )
            with zipfile.ZipFile(str(path)) as archive:
                infos, inventory_warnings, total_limit = _bounded_office_zip_inventory(
                    path, archive, central_directory.get("observed_entries") or 0,
                )
                base["warnings"].extend(inventory_warnings[:200])
                consumed = {"bytes": 0}

                def read_member(name, per_member_limit=OFFICE_XML_MEMBER_MAX_BYTES):
                    info = infos.get(name)
                    if info is None:
                        raise ValueError("Office 成员不存在或未通过安全预算：{}".format(name))
                    payload = _read_zip_info_bounded(
                        archive,
                        info,
                        per_member_limit,
                        total_limit - consumed["bytes"],
                    )
                    consumed["bytes"] += len(payload)
                    return payload

                media = [name for name in infos if name.startswith(prefix) and not name.endswith("/")]
                if not media:
                    return
                contexts = self._office_image_contexts(archive, ext, infos.keys(), read_member)
                seen_hashes = set()
                recognized_assets = 0
                ocr_characters = 0
                for ordinal, name in enumerate(sorted(media), 1):
                    try:
                        blob = read_member(name, OFFICE_MEDIA_MEMORY_BYTES)
                    except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
                        base["warnings"].append(
                            "已跳过无法在安全预算内读取的 Office 内嵌媒体 {}：{}".format(
                                name, str(exc)[:160],
                            )
                        )
                        continue
                    image_hash = hashlib.sha256(blob).hexdigest()
                    if image_hash in seen_hashes:
                        continue
                    seen_hashes.add(image_hash)
                    context = contexts.get(name, {})
                    with self._lock:
                        result = self._get_ocr_engine()(blob)
                    texts = list(result.txts) if result.txts is not None else []
                    if not texts:
                        continue
                    recognized_assets += 1
                    ocr_characters += sum(len(str(value)) for value in texts)
                    scores = list(result.scores) if result.scores is not None else []
                    boxes = list(result.boxes) if result.boxes is not None else []
                    section = context.get("section") or "内嵌图片 {}（{}）".format(ordinal, Path(name).name)
                    marker = "[{} OCR]\n{}".format(section, "\n".join(str(value) for value in texts))
                    remaining = max(0, self.max_chars - len(base["text"]))
                    if remaining:
                        base["text"] += ("\n\n" if base["text"] else "") + marker[:remaining]
                    if len(marker) > remaining:
                        base["coverage"]["truncated_by_limit"] = True
                    for index, value in enumerate(texts):
                        snippet = _short_text(value)
                        if not snippet or len(base["evidence"]) >= 3000:
                            continue
                        bbox = boxes[index] if index < len(boxes) else None
                        if hasattr(bbox, "tolist"):
                            bbox = bbox.tolist()
                        base["evidence"].append({
                            "evidence_id": "E-{}-{:05d}".format(base["source"]["sha256"][:10], len(base["evidence"]) + 1),
                            "source_path": base["source"]["path"],
                            "page": context.get("page"),
                            "section": section,
                            "label": "embedded_image_ocr",
                            "text": snippet,
                            "bbox": bbox,
                            "score": float(scores[index]) if index < len(scores) else None,
                            "parser": "RapidOCR PP-OCRv5 mobile",
                            "embedded_asset": name,
                            "embedded_asset_sha256": image_hash,
                            "source_sha256": base["source"]["sha256"],
                            "content_sha256": _digest_text(snippet),
                        })
                base["structure"]["picture_count"] = max(base["structure"].get("picture_count", 0), len(seen_hashes))
                if recognized_assets:
                    base["parser"]["ocr"] = True
                    base["parser"]["office_embedded_image_ocr"] = True
                    base["parser"]["ocr_models"] = "PP-OCRv5 mobile det/rec"
                    base["structure"]["ocr_picture_count"] = recognized_assets
                    base["coverage"]["embedded_ocr_characters"] += ocr_characters
                if len(base["text"]) >= self.max_chars:
                    base["warnings"].append("统一正文达到本地字符上限；内嵌图片证据仍已独立保留。")
        except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            base["warnings"].append("Office 内嵌图片 OCR 检查失败：{}".format(exc))

    def _extract_docling_items(self, document, base):
        headings = []
        pages = set()
        evidence = []
        table_count = 0
        picture_count = 0
        items = []
        if hasattr(document, "iterate_items"):
            try:
                items = [item for item, _level in document.iterate_items()]
            except Exception:
                items = []
        if not items:
            items = list(getattr(document, "texts", []) or [])
            items += list(getattr(document, "tables", []) or [])
            items += list(getattr(document, "pictures", []) or [])
        current_section = None
        for index, item in enumerate(items, 1):
            label = _label_name(_get_value(item, "label", "text"))
            if "table" in label:
                table_count += 1
            if "picture" in label or "image" in label:
                picture_count += 1
            item_text = _get_value(item, "text", "") or ""
            if not item_text and "table" in label:
                try:
                    item_text = item.export_to_markdown(document)
                except Exception:
                    item_text = ""
            if label in {"title", "section_header", "heading"} and item_text:
                current_section = _short_text(item_text, 200)
                headings.append(current_section)
                if label == "title" and current_section:
                    base["structure"]["title"] = current_section
            snippet = _short_text(item_text)
            if not snippet:
                continue
            provenance = list(_get_value(item, "prov", []) or [])
            if not provenance:
                provenance = [None]
            for prov in provenance[:3]:
                page = _get_value(prov, "page_no") if prov is not None else None
                if page is not None:
                    pages.add(int(page))
                bbox = _get_value(prov, "bbox") if prov is not None else None
                bbox_value = None
                if bbox is not None:
                    if hasattr(bbox, "model_dump"):
                        bbox_value = bbox.model_dump()
                    elif hasattr(bbox, "dict"):
                        bbox_value = bbox.dict()
                    else:
                        bbox_value = str(bbox)
                evidence.append({
                    "evidence_id": "E-{}-{:05d}".format(base["source"]["sha256"][:10], len(evidence) + 1),
                    "source_path": base["source"]["path"],
                    "page": page,
                    "section": current_section,
                    "label": label,
                    "text": snippet,
                    "bbox": bbox_value,
                    "parser": "Docling",
                    "source_sha256": base["source"]["sha256"],
                    "content_sha256": _digest_text(snippet),
                })
                if len(evidence) >= 3000:
                    base["warnings"].append("证据项超过 3000 条，演示版仅保留前 3000 条。")
                    break
            if len(evidence) >= 3000:
                break
        base["structure"].update({
            "headings": list(dict.fromkeys(headings))[:200],
            "page_count": max(pages) if pages else None,
            "table_count": table_count,
            "picture_count": picture_count,
        })
        base["evidence"] = evidence

    def _fallback(self, path, base, ocr_empty_pdf=True):
        extracted = extract_text(path, max_chars=self.max_chars)
        base["text"] = extracted.get("text", "")
        base["coverage"]["extracted_characters"] = (
            extracted.get("char_count", len(base["text"])) if extracted.get("truncated") else len(base["text"])
        )
        if extracted.get("truncated"):
            base["coverage"]["complete"] = False
            base["coverage"]["truncated_by_limit"] = True
        base["parser"] = {
            "name": extracted.get("parser", "unsupported"),
            "degraded": True,
            "ocr": False,
            "remote_services_enabled": False,
        }
        metadata = extracted.get("metadata", {})
        base["structure"].update({
            "page_count": metadata.get("page_count") or metadata.get("slide_count"),
            "table_count": metadata.get("table_count", 0),
        })
        base["warnings"].extend(extracted.get("warnings", []))
        ext = path.suffix.lower()
        image_extensions = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
        try:
            if ext in image_extensions:
                self._rapidocr_image(path, base, page_number=1)
                base["structure"]["page_count"] = 1
            elif ext == ".pdf" and not base["text"].strip() and ocr_empty_pdf:
                self._rapidocr_pdf(path, base)
        except Exception as exc:
            base["warnings"].append("RapidOCR 兼容 OCR 失败：{}".format(exc))
        if base["text"].strip():
            base["warnings"] = [warning for warning in base["warnings"] if warning != "该文件类型尚未配置正文解析器"]

    def _rapidocr_image(self, image, base, page_number=1):
        with self._lock:
            result = self._get_ocr_engine()(image)
        texts = list(result.txts) if result.txts is not None else []
        scores = list(result.scores) if result.scores is not None else []
        boxes = list(result.boxes) if result.boxes is not None else []
        if not texts:
            base["warnings"].append("RapidOCR 未在第 {} 页/张识别到文字。".format(page_number))
            return
        page_text = "\n".join(texts)
        page_marker = "[第 {} 页/张 OCR]\n{}".format(page_number, page_text)
        if base["text"]:
            base["text"] += "\n\n" + page_marker
        else:
            base["text"] = page_marker
        base["parser"] = {"name": "RapidOCR", "degraded": True, "ocr": True, "ocr_models": "PP-OCRv5 mobile det/rec", "remote_services_enabled": False}
        for index, text in enumerate(texts):
            snippet = _short_text(text)
            if not snippet:
                continue
            bbox = boxes[index] if index < len(boxes) else None
            if hasattr(bbox, "tolist"):
                bbox = bbox.tolist()
            base["evidence"].append({
                "evidence_id": "E-{}-{:05d}".format(base["source"]["sha256"][:10], len(base["evidence"]) + 1),
                "source_path": base["source"]["path"],
                "page": page_number,
                "section": None,
                "label": "ocr_text",
                "text": snippet,
                "bbox": bbox,
                "score": float(scores[index]) if index < len(scores) else None,
                "parser": "RapidOCR PP-OCRv5 mobile",
                "source_sha256": base["source"]["sha256"],
                "content_sha256": _digest_text(snippet),
            })

    def _rapidocr_pdf(self, path, base, max_pages=None):
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        total_pages = len(pdf)
        processed = 0
        try:
            page_limit = min(total_pages, max_pages) if max_pages else total_pages
            for index in range(page_limit):
                page = pdf[index]
                try:
                    bitmap = page.render(scale=2.0)
                    image = bitmap.to_numpy()
                    self._rapidocr_image(image, base, page_number=index + 1)
                    processed += 1
                    if len(base["text"]) >= self.max_chars:
                        base["text"] = base["text"][: self.max_chars]
                        base["warnings"].append("扫描 PDF 的 OCR 正文达到演示版字符上限。")
                        break
                finally:
                    page.close()
        finally:
            pdf.close()
        base["structure"]["page_count"] = total_pages
        return processed

    def _add_fallback_evidence(self, base):
        chunks = re.split(r"(?=\[第\s*\d+\s*页(?:/张)?(?:\s*OCR)?\]|\[幻灯片\s*\d+\]|\n#{1,6}\s+)", base["text"])
        evidence = []
        for raw in chunks:
            page_match = re.search(r"\[第\s*(\d+)\s*页(?:/张)?(?:\s*OCR)?\]", raw)
            slide_match = re.search(r"\[幻灯片\s*(\d+)\]", raw)
            page = int((page_match or slide_match).group(1)) if (page_match or slide_match) else None
            for start in range(0, len(raw), 1200):
                snippet = _short_text(raw[start:start + 1200], 1200)
                if not snippet:
                    continue
                evidence.append({
                    "evidence_id": "E-{}-{:05d}".format(base["source"]["sha256"][:10], len(evidence) + 1),
                    "source_path": base["source"]["path"],
                    "page": page,
                    "section": None,
                    "label": "text_chunk",
                    "text": snippet,
                    "character_range": [start, min(len(raw), start + 1200)],
                    "bbox": None,
                    "parser": base["parser"].get("name"),
                    "source_sha256": base["source"]["sha256"],
                    "content_sha256": _digest_text(snippet),
                })
                if len(evidence) >= 3000:
                    base["warnings"].append("文本证据块超过 3000 条，当前演示版仅保留前 3000 条。")
                    break
            if len(evidence) >= 3000:
                break
        base["evidence"] = evidence


def compact_document(document, include_text=False):
    payload = {
        "schema_version": document.get("schema_version"),
        "source": document.get("source"),
        "parsed_at": document.get("parsed_at"),
        "parser": document.get("parser"),
        "structure": document.get("structure"),
        "classification": document.get("classification", {}),
        "data_profile": document.get("data_profile"),
        "data_profiles": document.get("data_profiles", []),
        "coverage": document.get("coverage", {}),
        "archive_manifest": document.get("archive_manifest"),
        "deduplication": document.get("deduplication", {}),
        "content_sha256": document.get("content_sha256"),
        "warnings": document.get("warnings", []),
        "evidence_count": len(document.get("evidence", [])),
    }
    if include_text:
        payload["text"] = document.get("text", "")
    return payload


def dumps_document(document):
    return json.dumps(document, ensure_ascii=False, indent=2)
