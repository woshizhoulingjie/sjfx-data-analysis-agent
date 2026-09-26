"""Resource backpressure for the QA sidecar; never changes document quality."""
from __future__ import annotations

import errno
from pathlib import Path
import shutil
import sqlite3
import stat

GIB = 1024 ** 3


class ResourcePressure(RuntimeError):
    """A retryable infrastructure condition, not a document parse result."""
    def __init__(self, message, code='resource_pressure', details=None):
        super().__init__(message)
        self.code = code
        self.details = details or []


def resource_code(exc):
    if isinstance(exc, ResourcePressure):
        return exc.code
    if isinstance(exc, MemoryError):
        return 'memory_exhausted'
    if isinstance(exc, OSError) and exc.errno in {
            errno.ENOSPC, errno.EDQUOT, errno.ENOMEM, errno.EMFILE, errno.ENFILE}:
        return errno.errorcode[exc.errno]
    if isinstance(exc, sqlite3.Error):
        # Extended SQLite result codes carry the primary code in the low byte.
        code = getattr(exc, 'sqlite_errorcode', 0) & 255
        if code in {5, 6, 7, 13}:  # BUSY, LOCKED, NOMEM, FULL
            return 'sqlite_' + str(code)
    return None


def existing_directory(path):
    """Find the actual allocation volume without creating parser directories."""
    path = Path(path).expanduser().resolve()
    while True:
        try:
            info = path.stat()
            if stat.S_ISDIR(info.st_mode):
                return path
        except FileNotFoundError:
            pass
        if path.parent == path:
            raise OSError('No existing parent directory: ' + str(path))
        path = path.parent


class DiskGuard:
    """Keep the old 1GiB floor, with hysteresis to avoid retry thrashing."""
    def __init__(self, reserve_bytes=GIB, resume_margin=GIB // 4):
        self.reserve_bytes = max(GIB, int(reserve_bytes))
        self.resume_margin = max(0, int(resume_margin))
        self._paused = set()

    def check(self, paths):
        failures = []
        for label, path in paths:
            key = str(Path(path).expanduser().absolute())
            required = self.reserve_bytes + (self.resume_margin if key in self._paused else 0)
            try:
                actual = existing_directory(path)
                free = shutil.disk_usage(actual).free
            except OSError as exc:
                self._paused.add(key)
                failures.append(dict(label=label, path=key, error=str(exc), required_bytes=required))
                continue
            if free < required:
                self._paused.add(key)
                failures.append(dict(label=label, path=key, free_bytes=free, required_bytes=required))
            else:
                self._paused.discard(key)
        if failures:
            labels = '、'.join(item['label'] for item in failures)
            raise ResourcePressure(
                '资源暂不可用（' + labels + '）：可用空间不足或无法检查；任务保留，稍后自动重试。',
                'disk_capacity', failures)
