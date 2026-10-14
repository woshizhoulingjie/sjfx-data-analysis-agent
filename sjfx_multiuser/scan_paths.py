"""Authorize private uploaded scan paths before ordinary scan-root policy.

The entire upload root is reserved, even when an ordinary allowed scan root
contains it. Ancestor scans could recursively expose other owners' files and
are rejected. Only an authenticated owner's assembled session is readable.
No session manifest is required: uploaded outputs outlive staging cleanup.
"""

import os
from pathlib import Path

from .uploads import UploadError, _assert_no_links, _check_id, safe_rel_path


def _absolute(raw_path):
    value = os.fspath(raw_path)
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError("扫描路径为空或非法")
    if os.name == 'nt':
        value = value.replace('/', '\\')
        # Extended-length spellings must not evade the reserved-root test.
        if value.casefold().startswith('\\\\?\\unc\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
        elif value.startswith('\\\\.\\'):
            raise ValueError("不允许设备命名空间扫描路径")
        drive, _ = os.path.splitdrive(value)
        if drive and not os.path.isabs(value):
            raise ValueError("不允许驱动器相对扫描路径")
    # Windows abspath uses GetFullPathName and silently removes trailing dots
    # and spaces. Keep them visible so the private-name checks can reject them.
    return os.path.normpath(os.path.join(os.getcwd(), os.path.expanduser(value)))


def authorize_uploaded_scan_path(raw_path, upload_root, owner_id, *, check_filesystem=False):
    """Return an authorized private Path, or None for a lexical external path.

    Raise ValueError for any rejected path in or above the upload tree. The
    caller must apply this check *before* ordinary SCAN_ALLOWED_ROOTS and also
    check its independently resolved path, to catch an external link pointing
    into this private tree. False performs no filesystem access; True rejects
    all existing link/reparse ancestors and rechecks canonical ownership.

    This is path authorization, not a promise that the directory exists or
    that it has an upload manifest. The scanner still validates existence.
    """
    try:
        candidate, root = _absolute(raw_path), _absolute(upload_root)
    except (TypeError, OSError) as exc:
        raise ValueError("扫描路径非法") from exc
    normalized_candidate, normalized_root = os.path.normcase(candidate), os.path.normcase(root)
    try:
        common = os.path.commonpath([normalized_candidate, normalized_root])
    except ValueError:  # Different Windows drives/UNC shares are external.
        return None
    if common == normalized_candidate and normalized_candidate != normalized_root:
        raise ValueError("扫描目录包含用户私有上传目录，请选择具体资料目录")
    if common != normalized_root:
        return None

    # relpath also calls Windows abspath, so slice our normalized lexical paths.
    parts = candidate[len(root.rstrip(os.sep)):].lstrip(os.sep).split(os.sep)
    try:
        _check_id(owner_id, 'owner_id')
        if len(parts) < 3 or os.path.normcase(parts[0]) != os.path.normcase('assembled'):
            raise ValueError("上传暂存区不能作为扫描目录")
        _check_id(parts[1], 'owner_id')
        _check_id(parts[2], 'session_id')
        if os.path.normcase(parts[1]) != os.path.normcase(owner_id):
            raise ValueError("不允许扫描其他用户的私有上传目录")
        if len(parts) > 3:
            safe_rel_path('/'.join(parts[3:]))
        if not check_filesystem:
            return Path(candidate)
        _assert_no_links(candidate)
        canonical_candidate, canonical_root = os.path.realpath(candidate), os.path.realpath(root)
        canonical = authorize_uploaded_scan_path(canonical_candidate, canonical_root, owner_id)
        if canonical is None:
            raise ValueError("上传扫描路径解析后超出私有目录")
        return canonical
    except (UploadError, UnicodeError) as exc:
        raise ValueError(str(exc)) from exc
