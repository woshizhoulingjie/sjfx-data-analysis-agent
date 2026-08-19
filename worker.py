"""Dedicated local Worker for long-running data-analysis jobs.

Run this process separately from ``app.py``.  Both processes use the same local
SQLite/WAL database; the database transaction in ``Storage.claim_next_job`` makes
claiming safe even if an operator accidentally starts a second Worker.
"""

import logging
import os
import socket
import time
from pathlib import Path

from app import (
    JobCancelled,
    _run_claimed_analysis_job,
    _run_claimed_export_job,
    _run_claimed_report_job,
    _run_claimed_scan_and_analyze_job,
    _run_claimed_summary_job,
    logger,
    storage,
)
from config import Config


WORKER_ID = "{}:{}".format(socket.gethostname(), os.getpid())


def execute(job):
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
    """Allow only one project Worker to use the shared local Ollama GPU."""
    lock_path = Path(Config.DATA_DIR) / "worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows development fallback
        logger.warning("当前平台不支持 fcntl，无法提供跨进程 Worker 锁")
        return handle
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        raise RuntimeError("已有 SJFX Worker 正在运行，拒绝启动第二个 Worker")
    handle.write("{}\n".format(WORKER_ID))
    handle.flush()
    return handle


def run_forever():
    lock_handle = _acquire_worker_lock()
    storage.recover_stale_jobs(Config.WORKER_STALE_SECONDS)
    logger.info("SJFX local Worker started id=%s", WORKER_ID)
    try:
        while True:
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
            except JobCancelled:
                storage.update_job(job_id, status="cancelled", stage="cancelled", message="任务已取消", heartbeat=True)
            except Exception as exc:
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
