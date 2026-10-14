"""Shared format contract, with cheap, live dependency diagnostics.

Availability means an implementation and its prerequisites exist. It is not
a promise of complete extraction for encrypted, damaged or budget-limited data.
"""
from __future__ import annotations

import importlib.util
from importlib.metadata import PackageNotFoundError, version
import shutil
from pathlib import Path

TEXT_EXTENSIONS = frozenset({
    '.txt', '.log', '.md', '.markdown', '.csv', '.tsv', '.json', '.jsonl',
    '.xml', '.html', '.htm', '.yaml', '.yml', '.ini', '.cfg',
})
IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'})
OFFICE_EXTENSIONS = frozenset({'.docx', '.pptx', '.xlsx', '.xlsm', '.doc', '.ppt', '.xls', '.rtf'})
TAR_SUFFIXES = ('.tar.gz', '.tar.bz2', '.tar.xz', '.tar', '.tgz', '.tbz', '.tbz2', '.txz')
STREAM_SUFFIXES = ('.gz', '.bz2', '.xz')
ARCHIVE_EXTENSIONS = frozenset({'.zip', '.rar', '.7z', *TAR_SUFFIXES, *STREAM_SUFFIXES})
MAIL_SOURCE_EXTENSIONS = frozenset({'.eml', '.mbox', '.mbx', '.msg', '.pst'})
DOCUMENT_EXTENSIONS = frozenset({'.pdf', '.eml', *TEXT_EXTENSIONS, *IMAGE_EXTENSIONS, *OFFICE_EXTENSIONS})


def file_extension(path):
    name = str(path).replace('\\', '/').rsplit('/', 1)[-1].lower()
    for suffix in sorted(ARCHIVE_EXTENSIONS, key=len, reverse=True):
        if name.endswith(suffix):
            return suffix
    return Path(name).suffix


def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _legacy_tool(name):
    try:
        from services.legacy_office import _find_tool
        _find_tool(name)
        return True
    except (ImportError, RuntimeError, OSError):
        return False


def _rar_backend():
    try:
        import rarfile
        from services.archive_formats import configure_rar_backend
        configure_rar_backend(rarfile)
        rarfile.tool_setup()
        return True
    except (ImportError, OSError, ValueError):
        return False
    except Exception:
        # Backend probes can fail because the executable itself is unusable.
        return False


def _safe_7z_version():
    try:
        parts = tuple(int(x) for x in version('py7zr').split('.')[:3])
        return parts >= (1, 1, 4)
    except (PackageNotFoundError, ValueError):
        return False


def capability_matrix(parser_status=None):
    """Report dependency/model presence without loading parsers or downloading models."""
    from config import Config
    modules = {name: _module_available(name) for name in (
        'pypdf', 'docx', 'pptx', 'openpyxl', 'xlrd', 'striprtf', 'olefile',
        'rarfile', 'py7zr', 'extract_msg', 'rapidocr', 'onnxruntime',
    )}
    model_names = ('ch_PP-OCRv5_det_mobile.onnx', 'ch_PP-OCRv5_rec_mobile.onnx',
                   'ch_ppocr_mobile_v2.0_cls_mobile.onnx')
    model_root = Path(Config.RAPIDOCR_MODEL_DIR)
    models = {name: (model_root / name).is_file() for name in model_names}
    ocr_ready = modules['rapidocr'] and modules['onnxruntime'] and all(models.values())
    from services.native_tools import readpst_executable
    tools = {'antiword': _legacy_tool('antiword'), 'catppt': _legacy_tool('catppt'),
             'readpst': bool(readpst_executable()),
             'rar_backend': _rar_backend()}
    rows = []
    for ext in sorted(DOCUMENT_EXTENSIONS | ARCHIVE_EXTENSIONS | MAIL_SOURCE_EXTENSIONS):
        missing = []
        mail_only = ext in MAIL_SOURCE_EXTENSIONS - {'.eml'}
        required = {'.pdf': 'pypdf', '.docx': 'docx', '.pptx': 'pptx',
                    '.xlsx': 'openpyxl', '.xlsm': 'openpyxl', '.xls': 'xlrd',
                    '.rtf': 'striprtf', '.doc': 'olefile', '.ppt': 'olefile',
                    '.rar': 'rarfile', '.7z': 'py7zr', '.msg': 'extract_msg'}.get(ext)
        if required and not modules[required]:
            missing.append(required)
        if ext == '.7z' and modules['py7zr'] and not _safe_7z_version():
            missing.append('py7zr>=1.1.4')
        tool = {'.doc': 'antiword', '.rar': 'rar_backend', '.pst': 'readpst'}.get(ext)
        if tool and not tools[tool]:
            missing.append(tool)
        if ext in IMAGE_EXTENSIONS and not ocr_ready:
            missing.append('ocr_runtime_or_models')
        archive = ext in ARCHIVE_EXTENSIONS
        notes = []
        if mail_only:
            notes.append('请在邮件专区逐封导入；普通导入与大包预览不代表完整邮件解析。')
        if ext == '.eml':
            notes.append('文档入口读取邮件头与外层正文；附件与逐封邮件研究使用邮件专区。')
        if archive:
            notes.append('有界解包；不递归展开嵌套压缩包，加密和超限成员会明确报告。')
        if ext == '.7z':
            notes.append('支持常见 LZMA/LZMA2 等编码；不支持的编码或过大字典会明确拒绝。')
        if ext in IMAGE_EXTENSIONS or ext == '.pdf' or ext in OFFICE_EXTENSIONS:
            notes.append('OCR 需要本地组件和模型；预览、旧格式和内嵌对象可能仅部分覆盖。')
        rows.append({'extension': ext, 'inventory': True, 'document': not mail_only,
                     'preview': not mail_only, 'deep_parse': not mail_only,
                     'archive_members': archive, 'mail_source': ext in MAIL_SOURCE_EXTENSIONS,
                     'mail_attachment': ext in DOCUMENT_EXTENSIONS - {'.eml'},
                     'availability': 'dependency_missing' if missing else 'available',
                     'missing_dependencies': missing, 'notes': notes})
    return {'version': 1, 'formats': rows, 'modules': modules, 'tools': tools,
            'ocr': {'available': ocr_ready, 'models': models,
                    'verification': 'presence_only', 'runtime': (parser_status or {}).get('ocr')},
            'result_statuses': ['complete', 'partial', 'unsupported', 'dependency_missing',
                                'encrypted', 'damaged', 'timeout'],
            'preview_is_bounded': True,
            'unknown_formats': 'inventory_only'}
