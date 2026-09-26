"""Admission and recoverable infrastructure errors for isolated large packages.

This is a runtime safety floor, not a per-package capacity estimate or a
reservation against other processes. It never lowers parsing/quality budgets.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from contextlib import closing

from services.large_qa_resources import DiskGuard, GIB, ResourcePressure, resource_code

LOGGER = logging.getLogger('sjfx.large_package_resources')
WAIT_MESSAGE = '资源暂不可用，已保留处理进度；释放空间或恢复资源后自动续跑，不跳过文件。'


def sqlite_temp_directory():
    """Default VFS path order; the application does not set temp_store_directory."""
    if os.name == 'nt':
        return Path(tempfile.gettempdir())
    for raw in (os.environ.get('SQLITE_TMPDIR'), os.environ.get('TMPDIR'),
                '/var/tmp', '/usr/tmp', '/tmp', '.'):
        if raw and Path(raw).is_dir() and os.access(raw, os.W_OK | os.X_OK):
            return Path(raw).resolve()
    raise ResourcePressure('SQLite临时目录不可用，任务等待资源恢复。', 'sqlite_temp_unavailable')


def pressure(exc):
    if isinstance(exc, ResourcePressure):
        return exc
    code = resource_code(exc)
    if code is None:
        raise ValueError('Not a resource error') from exc
    return ResourcePressure(WAIT_MESSAGE + ' [' + code + ']', code)


class AdmissionGuard:
    """One shared gate per worker; at most one disk probe/second when healthy."""
    def __init__(self, paths, reserve_bytes=GIB, interval=1.0, retry_seconds=30.0):
        self.paths = paths if callable(paths) else tuple(paths)
        self.disk = DiskGuard(reserve_bytes=reserve_bytes, resume_margin=GIB // 4)
        self.interval = max(0.0, float(interval))
        self.retry_seconds = max(1.0, float(retry_seconds))
        self._next_probe = 0.0
        self._retry_at = 0.0
        self._failure = None
        self._lock = threading.RLock()

    def defer(self, exc):
        failure = pressure(exc)
        with self._lock:
            # Repeated observations of an existing cooldown must not extend it.
            if self._failure is not failure:
                self._retry_at = max(self._retry_at, time.monotonic() + self.retry_seconds)
            self._failure = failure
            self._next_probe = 0.0
        return failure

    def check(self, force=False):
        with self._lock:
            now = time.monotonic()
            if self._failure is not None and now < self._retry_at:
                raise self._failure
            if not force and now < self._next_probe:
                return
            try:
                self.disk.check(self.paths() if callable(self.paths) else self.paths)
            except ResourcePressure as exc:
                self.defer(exc)
                raise
            self._failure = None
            self._next_probe = now + self.interval


def defer_lease(store, package_id, path, stage, token, exc):
    """Only the current lease may refund its infrastructure attempt.

No file result, source hash, quality, completed evidence, or selection changes.
A repeated call cannot refund twice, and a stale owner cannot edit a new lease.
If the DB cannot write at all, propagate; ordinary stale-lease recovery remains.
"""
    if not token:
        raise ValueError('A current lease token is required')
    failure = pressure(exc)
    with store._lock, closing(store._connect()) as conn:
        cursor = conn.execute(
            "UPDATE queue SET status='retry',attempts=MAX(0,attempts-1),"
            "error=?,leased_at=NULL,lease_token=NULL,updated_at=? "
            "WHERE package_id=? AND path=? AND stage=? "
            "AND status='running' AND lease_token=?",
            (WAIT_MESSAGE + ' [' + failure.code + ']', time.time(),
             str(package_id), str(path), str(stage), str(token)))
        return cursor.rowcount == 1


def show_wait(store, package_id, exc):
    """Best-effort visible reason, never change cancellation/phase/quality."""
    if not package_id:
        return
    try:
        with store._lock, closing(store._connect()) as conn:
            conn.execute(
                "UPDATE packages SET message=? WHERE id=? "
                "AND status IN ('queued','running','waiting_for_selection') "
                "AND COALESCE(control_state,'') NOT IN ('paused','cancelled')",
                (WAIT_MESSAGE + ' [' + pressure(exc).code + ']', str(package_id)))
    except Exception:
        LOGGER.exception('Could not persist resource-wait notice; lease recovery retained')
