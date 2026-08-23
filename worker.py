"""Dedicated local Worker for long-running data-analysis jobs.

Run this process separately from ``app.py``.  Both processes use the same local
SQLite/WAL database; the database transaction in ``Storage.claim_next_job`` makes
claiming safe even if an operator accidentally starts a second Worker.
"""

import errno
import ctypes
import logging
import logging.handlers
import multiprocessing
import os
import re
import signal
import socket
import subprocess
import time
import traceback
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
    logger.propagate = False


WORKER_ID = "{}:{}".format(socket.gethostname(), os.getpid())


class JobCancelled(RuntimeError):
    """The parent Worker terminated a supervised task after cancellation."""


class JobExecutionTimeout(RuntimeError):
    """A task exceeded its configured end-to-end execution boundary."""


class RemoteJobError(RuntimeError):
    """A supervised child reported an execution error."""

    def __init__(self, message, remote_traceback=""):
        super().__init__(message)
        self.remote_traceback = remote_traceback


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


def _execute_job_child(job, sender):
    """Run one task in a killable process and return a compact outcome."""
    try:
        parent_pid = os.getppid()
        if os.name != "nt" and hasattr(os, "setsid"):
            # Docling isolation may create parser grandchildren. A dedicated
            # process group lets timeout/cancellation terminate the complete
            # task tree instead of leaving OCR/parser processes orphaned.
            os.setsid()
            if os.uname().sysname.lower() == "linux":
                # Ensure a service restart cannot leave the supervised job (or
                # its parser grandchildren) writing after the queue owner died.
                def parent_exit_handler(_signum, _frame):
                    try:
                        os.killpg(os.getpgrp(), signal.SIGKILL)
                    finally:
                        os._exit(128 + signal.SIGTERM)

                signal.signal(signal.SIGTERM, parent_exit_handler)
                try:
                    libc = ctypes.CDLL(None)
                    libc.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG
                    if os.getppid() != parent_pid:
                        parent_exit_handler(signal.SIGTERM, None)
                except (AttributeError, OSError):
                    pass
        sender.send({"status": "ok", "result": execute(job)})
    except BaseException as exc:  # the parent owns final status publication
        try:
            sender.send({
                "status": "error",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })
        except Exception:
            pass
    finally:
        try:
            sender.close()
        except Exception:
            pass


def _task_runtime_limit(job):
    limits = {
        "scan_and_analyze": Config.JOB_SCAN_TIMEOUT_SECONDS,
        "analyze_package": Config.JOB_ANALYSIS_TIMEOUT_SECONDS,
        "generate_summary": Config.JOB_SUMMARY_TIMEOUT_SECONDS,
        "generate_report": Config.JOB_REPORT_TIMEOUT_SECONDS,
        "export_package": Config.JOB_EXPORT_TIMEOUT_SECONDS,
    }
    limit = limits.get(job.get("task_type"), Config.JOB_DEFAULT_TIMEOUT_SECONDS)
    if job.get("task_type") == "generate_summary":
        options = job.get("options") or {}
        if not options.get("node_id") and options.get("kind", "file") not in {"directory", "group"}:
            limit = Config.JOB_DOCUMENT_SUMMARY_TIMEOUT_SECONDS
    return max(30, int(limit))


def _stop_process(process):
    if not process or not process.is_alive():
        return
    if os.name == "nt":
        # multiprocessing.terminate() only targets the direct child on
        # Windows. taskkill /T closes the complete supervised tree, including
        # Docling/OCR parser grandchildren, so cancellation and timeout cannot
        # leave invisible CPU/RAM/temp-disk consumers behind.
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(int(process.pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(5, int(Config.WORKER_TERMINATE_GRACE_SECONDS) + 2),
                check=False,
            )
            process.join(timeout=2)
            if result.returncode == 0 and not process.is_alive():
                return
        except (OSError, subprocess.SubprocessError, ValueError):
            # Fall through to the direct-process fallback. A deployment with
            # psutil gets one additional recursive attempt before that.
            pass
        try:
            import psutil

            parent = psutil.Process(int(process.pid))
            descendants = parent.children(recursive=True)
            for child in reversed(descendants):
                child.terminate()
            parent.terminate()
            _gone, alive = psutil.wait_procs(
                descendants + [parent], timeout=max(1, Config.WORKER_TERMINATE_GRACE_SECONDS)
            )
            for remaining in alive:
                remaining.kill()
            process.join(timeout=2)
            if not process.is_alive():
                return
        except Exception:
            pass
    group_signalled = False
    if os.name != "nt" and hasattr(os, "killpg"):
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGTERM)
                group_signalled = True
        except (ProcessLookupError, PermissionError, OSError):
            group_signalled = False
    if not group_signalled:
        process.terminate()
    process.join(timeout=Config.WORKER_TERMINATE_GRACE_SECONDS)
    if process.is_alive() and hasattr(process, "kill"):
        killed_group = False
        if os.name != "nt" and hasattr(os, "killpg"):
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                    killed_group = True
            except (ProcessLookupError, PermissionError, OSError):
                killed_group = False
        if not killed_group:
            process.kill()
        process.join(timeout=2)


def execute_supervised(job):
    """Execute a task with heartbeat, hard cancellation and a total timeout.

    Library calls such as NAS directory enumeration, Docling and a local model
    socket can block below Python's cooperative cancellation points. Keeping
    the queue owner in the parent process lets it publish liveness and safely
    terminate only the current task process without killing the Worker.
    """
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_execute_job_child,
        args=(job, sender),
        name="sjfx-job-{}".format(job["id"]),
    )
    process.daemon = False
    started = time.monotonic()
    timeout = _task_runtime_limit(job)
    next_heartbeat = 0.0
    packet = None
    try:
        process.start()
        sender.close()
        while True:
            if receiver.poll(Config.WORKER_MONITOR_SECONDS):
                try:
                    packet = receiver.recv()
                except EOFError:
                    packet = None
                break
            if not process.is_alive():
                break
            if storage.is_job_cancel_requested(job["id"]):
                # Cooperative parser loops get a brief chance to close their
                # own child process and sidecar files before the hard stop.
                process.join(timeout=min(2.0, Config.WORKER_TERMINATE_GRACE_SECONDS))
                _stop_process(process)
                raise JobCancelled("任务已按用户请求终止")
            now = time.monotonic()
            if now >= next_heartbeat:
                storage.heartbeat_job(job["id"], WORKER_ID)
                next_heartbeat = now + Config.WORKER_HEARTBEAT_SECONDS
            if now - started > timeout:
                _stop_process(process)
                raise JobExecutionTimeout(
                    "{}任务运行超过 {} 秒，已终止当前任务进程；已完成检查点仍会保留".format(
                        job.get("task_type") or "分析", timeout,
                    )
                )
        process.join(timeout=5)
        if packet is None and receiver.poll(0.1):
            try:
                packet = receiver.recv()
            except EOFError:
                packet = None
        if not packet:
            raise RemoteJobError(
                "任务子进程异常退出（exit_code={}），Worker仍保持可用".format(process.exitcode)
            )
        if packet.get("status") == "ok":
            return packet.get("result")
        error_type = packet.get("error_type") or "RemoteError"
        if error_type in {"JobCancelled", "ParseIsolationCancelled"}:
            raise JobCancelled(packet.get("error") or "任务已取消")
        raise RemoteJobError(
            "{}: {}".format(error_type, packet.get("error") or "任务执行失败"),
            packet.get("traceback") or "",
        )
    finally:
        if process.is_alive():
            _stop_process(process)
        try:
            receiver.close()
        except Exception:
            pass
        try:
            process.close()
        except (ValueError, OSError):
            pass


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
    recovered = storage.recover_orphaned_jobs_after_lock()
    if recovered:
        logger.warning("Worker startup recovered %s orphaned task(s)", recovered)
    try:
        storage.checkpoint_wal(force=True)
    except Exception:
        logger.warning("启动时 SQLite WAL checkpoint 失败，将继续运行", exc_info=True)
    logger.info("SJFX local Worker started id=%s", WORKER_ID)
    last_checkpoint = time.monotonic()
    checkpoint_interval = getattr(storage, "sqlite_checkpoint_interval", 60)
    last_recovery = time.monotonic()
    recovery_interval = max(30.0, min(300.0, Config.WORKER_STALE_SECONDS / 3.0))
    try:
        while True:
            now = time.monotonic()
            if now - last_checkpoint >= checkpoint_interval:
                try:
                    storage.checkpoint_wal()
                except Exception:
                    logger.warning("周期性 SQLite WAL checkpoint 失败", exc_info=True)
                last_checkpoint = time.monotonic()
            if now - last_recovery >= recovery_interval:
                try:
                    recovered = storage.recover_stale_jobs(Config.WORKER_STALE_SECONDS)
                    if recovered:
                        logger.warning("Worker recovered %s stale task(s)", recovered)
                except Exception:
                    logger.warning("周期性失联任务恢复失败，将继续运行", exc_info=True)
                last_recovery = time.monotonic()
            job = storage.claim_next_job(WORKER_ID)
            if not job:
                time.sleep(Config.WORKER_POLL_SECONDS)
                continue
            job_id = job["id"]
            try:
                logger.info("Worker claimed task id=%s type=%s scan_id=%s", job_id, job.get("task_type"), job.get("scan_id"))
                result = execute_supervised(job)
                storage.finalize_job(job_id, result=result)
            except Exception as exc:
                if isinstance(exc, JobCancelled) or exc.__class__.__name__ == "ParseIsolationCancelled":
                    if job.get("task_type") in {"scan_and_analyze", "analyze_package"} and storage.scan_owned(job.get("scan_id")):
                        storage.update_analysis_progress_status(
                            job.get("scan_id"), "cancelled", "分析已取消；已完成的文件检查点仍保留。", "cancelled"
                        )
                    storage.finalize_job(job_id)
                    continue
                if isinstance(exc, JobExecutionTimeout) and job.get("task_type") in {"scan_and_analyze", "analyze_package"}:
                    attempts = int(job.get("attempt_count") or 1)
                    if attempts < Config.MAX_JOB_RESUME_ATTEMPTS:
                        requeued = storage.requeue_job_slice(
                            job_id,
                            "本轮运行达到时间上限；已保存检查点，自动续建下一批（第 {} 轮）。".format(attempts + 1),
                        )
                        if requeued and storage.scan_owned(job.get("scan_id")):
                            storage.update_analysis_progress_status(
                                job.get("scan_id"), "queued",
                                "本轮达到运行时限，已保存检查点并等待自动续批。", "checkpoint_requeued",
                            )
                        if requeued:
                            logger.warning(
                                "Worker task slice timed out and was requeued id=%s attempt=%s/%s",
                                job_id, attempts, Config.MAX_JOB_RESUME_ATTEMPTS,
                            )
                            continue
                        # Cancellation can win between the timeout and the
                        # guarded requeue UPDATE. Resolve that race now instead
                        # of leaving a task permanently in ``cancelling``.
                        if storage.is_job_cancel_requested(job_id):
                            if storage.scan_owned(job.get("scan_id")):
                                storage.update_analysis_progress_status(
                                    job.get("scan_id"), "cancelled",
                                    "分析已取消；已完成的文件检查点仍保留。", "cancelled",
                                )
                            storage.finalize_job(job_id)
                            continue
                if isinstance(exc, RemoteJobError) and exc.remote_traceback:
                    logger.error(
                        "Worker task failed id=%s type=%s\n%s",
                        job_id, job.get("task_type"), exc.remote_traceback,
                    )
                else:
                    logger.exception("Worker task failed id=%s type=%s", job_id, job.get("task_type"))
                if job.get("task_type") in {"scan_and_analyze", "analyze_package"} and storage.scan_owned(job.get("scan_id")):
                    storage.update_analysis_progress_status(
                        job.get("scan_id"), "failed",
                        "分析失败：{}".format(str(exc)[:500]), "failed",
                    )
                storage.finalize_job(job_id, error=exc)
    finally:
        try:
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    run_forever()
