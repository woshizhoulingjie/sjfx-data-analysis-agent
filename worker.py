"""Dedicated local Worker for long-running data-analysis jobs.

Run this process separately from ``app.py``.  Both processes use the same local
SQLite/WAL database; the database transaction in ``Storage.claim_next_job`` makes
claiming safe even if an operator accidentally starts a second Worker.
"""

import errno
import logging
import logging.handlers
import os
import re
import socket
import time
from pathlib import Path

from config import Config
from services.storage import Storage


logger = logging.getLogger("sjfx.worker")
storage = Storage(Config.DB_PATH, Config.DOCUMENT_CACHE_DIR, Config.SIDECAR_PAYLOAD_BYTES)


class _WorkerSensitiveFilter(logging.Filter):
    _patterns = (
        (re.compile(r"(Authorization\s*[:=]\s*Bearer\s+)[^\s,;]+", re.I), r"\1***"),
        (re.compile(r"([\"']?(?:api[_-]?key|token|password)[\"']?\s*[:=]\s*[\"'])[^\"']+", re.I), r"\1***"),
    )

    def filter(self, record):
        message = record.getMessage()
        for pattern, replacement in self._patterns:
            message = pattern.sub(replacement, message)
        record.msg, record.args = message, ()
        return True


if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for _root_handler in logging.getLogger().handlers:
        _root_handler.addFilter(_WorkerSensitiveFilter())
    _worker_handler = logging.handlers.RotatingFileHandler(
        str(Config.LOG_DIR / "worker.log"), maxBytes=5 * 1024 * 1024,
        backupCount=3, encoding="utf-8",
    )
    _worker_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _worker_handler.addFilter(_WorkerSensitiveFilter())
    logger.addHandler(_worker_handler)


WORKER_ID = "{}:{}".format(socket.gethostname(), os.getpid())


class _HeldWorkerLock:
    """File-backed inter-process lock with an explicit release operation."""

    def __init__(self, handle, unlock):
        self._handle = handle
        self._unlock = unlock
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._unlock(self._handle)
        finally:
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def _write_lock_metadata(handle):
    """Write diagnostic metadata without changing lock ownership."""
    handle.seek(0)
    try:
        handle.truncate(0)
    except (AttributeError, OSError):
        pass
    handle.write(("{}\n".format(WORKER_ID)).encode("utf-8"))
    handle.flush()


def execute(job):
    # Import the web/application module only when a real job is claimed. A
    # broken optional parser/model dependency can no longer prevent the Worker
    # from starting or recovering its SQLite queue.
    from app import (
        _run_claimed_analysis_job,
        _run_claimed_export_job,
        _run_claimed_report_job,
        _run_claimed_scan_and_analyze_job,
        _run_claimed_summary_job,
    )
    task_type = job.get("task_type")
    if task_type == "scan_and_analyze":
        return _run_claimed_scan_and_analyze_job(job)
    if task_type == "analyze_package":
        return _run_claimed_analysis_job(job)
    if task_type == "generate_report":
        return _run_claimed_report_job(job)
    if task_type == "generate_summary":
        return _run_claimed_summary_job(job)
    if task_type == "export_package":
        return _run_claimed_export_job(job)
    raise ValueError("未知任务类型：{}".format(task_type))


def _acquire_worker_lock():
    """Allow only one project Worker to use the shared local Ollama GPU.

    This function fails closed.  If the current platform has no supported
    locking primitive, Worker startup aborts instead of allowing two processes
    to contend for the local model or SQLite queue.
    """
    lock_path = Path(Config.DATA_DIR) / "worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Binary mode is required by msvcrt.locking and is also safe for fcntl.
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            try:
                import msvcrt
            except ImportError as exc:  # pragma: no cover - defensive
                raise RuntimeError("当前 Windows 环境缺少 msvcrt，无法安全锁定 Worker") from exc

            # msvcrt locks a byte range starting at the current file pointer.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EDEADLK, errno.EAGAIN, errno.EBUSY}:
                    raise RuntimeError("已有 SJFX Worker 正在运行，拒绝启动第二个 Worker") from exc
                raise RuntimeError("无法获取 Windows Worker 锁：{}".format(exc)) from exc

            def _unlock_windows(stream):
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    logger.warning("释放 Windows Worker 锁时发生异常", exc_info=True)

            lock = _HeldWorkerLock(handle, _unlock_windows)
        else:
            try:
                import fcntl
            except ImportError as exc:  # pragma: no cover - exotic platform
                raise RuntimeError("当前平台不支持安全的 Worker 进程锁，已拒绝启动") from exc
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                raise RuntimeError("已有 SJFX Worker 正在运行，拒绝启动第二个 Worker") from exc

            def _unlock_posix(stream):
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    logger.warning("释放 POSIX Worker 锁时发生异常", exc_info=True)

            lock = _HeldWorkerLock(handle, _unlock_posix)

        _write_lock_metadata(handle)
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        return lock
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        raise


def run_forever():
    lock_handle = _acquire_worker_lock()
    storage.recover_stale_jobs(Config.WORKER_STALE_SECONDS)
    try:
        storage.checkpoint_wal(force=True)
    except Exception:
        logger.warning("启动时 SQLite WAL checkpoint 失败，将继续运行", exc_info=True)
    logger.info("SJFX local Worker started id=%s", WORKER_ID)
    last_checkpoint = time.monotonic()
    checkpoint_interval = getattr(storage, "sqlite_checkpoint_interval", 60)
    try:
        while True:
            if time.monotonic() - last_checkpoint >= checkpoint_interval:
                try:
                    storage.checkpoint_wal()
                except Exception:
                    logger.warning("周期性 SQLite WAL checkpoint 失败", exc_info=True)
                last_checkpoint = time.monotonic()
            job = storage.claim_next_job(WORKER_ID)
            if not job:
                time.sleep(Config.WORKER_POLL_SECONDS)
                continue
            job_id = job["id"]
            try:
                logger.info("Worker claimed task id=%s type=%s scan_id=%s", job_id, job.get("task_type"), job.get("scan_id"))
                result = execute(job)
                storage.update_job(
                    job_id, status="completed", stage="completed", progress=100,
                    message="任务已完成", result=result, heartbeat=True,
                )
            except Exception as exc:
                if exc.__class__.__name__ == "JobCancelled":
                    storage.update_job(job_id, status="cancelled", stage="cancelled", message="任务已取消", heartbeat=True)
                    continue
                logger.exception("Worker task failed id=%s type=%s", job_id, job.get("task_type"))
                storage.update_job(
                    job_id, status="failed", stage="failed", progress=100,
                    message="任务失败", error=str(exc), heartbeat=True,
                )
    finally:
        try:
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    run_forever()
