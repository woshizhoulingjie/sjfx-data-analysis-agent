"""Reusable, killable CPU-only preview process for the isolated intake queue."""
from __future__ import annotations
import multiprocessing
import os
import time

from services.large_package_resources import ResourcePressure, resource_code
from services.process_liveness import (
    initialize_process_family, own_process_tree, process_rss_bytes, terminate_process_tree,
)


class PreviewCancelled(Exception):
    pass


def _serve(connection, start_gate=None):
    initialize_process_family()
    if start_gate is not None and not start_gate.wait(timeout=30):
        connection.close()
        return
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    try:
        os.nice(5)
    except (AttributeError, OSError):
        pass
    from services.large_package_preview import preview_file
    try:
        while True:
            request = connection.recv()
            try:
                connection.send({"result": preview_file(**request)})
            except Exception as exc:
                code = resource_code(exc)
                connection.send({"error": "{}: {}".format(type(exc).__name__, str(exc)[:300]),
                                 "resource_code": code})
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class PreviewRunner:
    def __init__(self):
        self.process = None
        self.connection = None
        self.calls = 0
        self._job_owner = None
        self._start_gate = None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.process is not None:
            # Preview writes no durable state. Promptly reap a stalled parser
            # so recovery can use the remainder of the original deadline.
            terminate_process_tree(self.process, grace_seconds=.1, job=self._job_owner, private_group=True)
            self.process = None
        elif self._job_owner is not None:
            self._job_owner.close()
        self._job_owner = None
        self._start_gate = None
        self.calls = 0

    def _ensure_process(self):
        if self.process is None or not self.process.is_alive() or self.calls >= 100:
            self.close()
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            start_gate = context.Event()
            # POSIX spawn reconstructs named SemLocks after start() returns.
            # Keep their parent owner alive until the helper has been reaped;
            # a local-only Event can be unlinked before the child unpickles it.
            self._start_gate = start_gate
            try:
                self.process = context.Process(target=_serve, args=(child, start_gate), daemon=True)
                self.process.start()
                self._job_owner = own_process_tree(self.process.pid)
                if os.name == "nt" and self._job_owner is None:
                    raise RuntimeError("无法建立 Windows 预览子进程回收边界")
                start_gate.set()
            except BaseException:
                if self.process is not None and self.process.pid is not None:
                    terminate_process_tree(self.process, job=self._job_owner)
                parent.close()
                child.close()
                self.process = None
                self._job_owner = None
                self._start_gate = None
                raise
            child.close()
            self.connection = parent

    def run(self, root, item, refine=False, cancel_check=None):
        from services.large_package_preview import settings, portrait, SCHEMA
        options = settings(refine)
        started = time.monotonic()
        deadline = started + options['seconds'] + 1
        # Leave a small part of the same total budget for a hung PDF parser's
        # replacement process. No time is added for retry or fallback.
        is_pdf = str(item.get('extension') or item.get('path', '')).lower().endswith('.pdf')
        reserve = min(1.5, options['seconds'] * .25) if is_pdf else 0
        engine, transport_retries, parser_fallbacks = None, 0, 0
        attempts = []
        error = ""
        transport_failed = False
        while time.monotonic() < deadline:
            retry_transport, retry_pdf = False, False
            try:
                if cancel_check and cancel_check():
                    self.close()
                    raise PreviewCancelled()
                self._ensure_process()
                self.calls += 1
                attempt_deadline = deadline - (reserve if engine is None else 0)
                if time.monotonic() >= attempt_deadline:
                    error = '轻量解析总预算已用尽'
                    self.close()
                    break
                request = {"root": str(root), "item": {key: item[key] for key in ("path", "name", "size", "modified_at_ns", "sensitive", "extension", "source_kind", "source_metadata") if key in item}, "refine": refine,
                           'seconds_remaining': max(0, attempt_deadline - time.monotonic())}
                if engine:
                    request['pdf_engine'] = engine
                self.connection.send(request)
                while True:
                    if cancel_check and cancel_check():
                        self.close()
                        raise PreviewCancelled()
                    remaining = attempt_deadline - time.monotonic()
                    if remaining <= 0:
                        error = "轻量解析超时，已释放子进程"
                        retry_pdf = is_pdf and engine is None
                        self.close()
                        break
                    if self.connection.poll(min(.15, remaining)):
                        response = self.connection.recv()
                        if "result" in response:
                            summary, digest = response['result']
                            coverage = summary.setdefault('coverage', {})
                            if (coverage.get('pdf_primary_timeout') and is_pdf and engine is None
                                    and parser_fallbacks < 1 and time.monotonic() < deadline):
                                error = 'PDF 主解析时间预算已用尽，重建备用解析进程'
                                retry_pdf = True
                                self.close()
                                break
                            coverage.update(transport_retries=transport_retries,
                                            parser_timeout_fallbacks=parser_fallbacks,
                                            runner_elapsed_seconds=round(time.monotonic()-started, 6))
                            if attempts:
                                coverage['recovery_events'] = attempts
                            return summary, digest
                        if response.get('resource_code'):
                            self.close()
                            raise ResourcePressure('预览资源暂不可用，文件保留等待重试。', response['resource_code'])
                        error = response.get("error", "轻量解析失败")
                        break
                    if not self.process.is_alive():
                        error = "轻量解析进程退出"
                        retry_transport = transport_failed = True
                        self.close()
                        break
                    try:
                        rss = process_rss_bytes(self.process.pid)
                        if rss is not None and rss > 1536 * 1024 * 1024:
                            error = "轻量解析达到 1.5 GiB 内存上限，已释放子进程"
                            self.close()
                            break
                    except (OSError, ValueError, IndexError, AttributeError):
                        pass
            except RuntimeError as exc:
                # A failed recovery process boundary must never escape as a
                # lost queue item. Initial startup failures retain their prior
                # behavior; a rebuild failure records a terminal diagnostic.
                if not attempts:
                    raise
                error = 'RuntimeError: ' + str(exc)[:160]
                self.close()
            except (EOFError, BrokenPipeError, OSError) as exc:
                if resource_code(exc):
                    self.close()
                    raise
                error = "轻量解析中断：" + str(exc)[:160]
                retry_transport = transport_failed = True
                self.close()
            if retry_transport and transport_retries < 1 and time.monotonic() < deadline:
                transport_retries += 1
                attempts.append({'kind': 'transport_rebuild', 'error': error})
                continue
            if retry_pdf and parser_fallbacks < 1 and time.monotonic() < deadline:
                parser_fallbacks += 1
                engine = 'pdfium'
                attempts.append({'kind': 'pdf_primary_timeout', 'error': error})
                continue
            break
        if attempts:
            self.close()
            error = '；'.join([entry['error'] for entry in attempts] + [error])[:500]
        info = portrait(item, [], [], [], refine, [error])
        info["recognition_status"] = "needs_review" if refine else "needs_more"
        return {"schema_version": SCHEMA, "summary_type": "local", "sample": "", "complete": False,
                "status": "failed" if transport_failed or error.startswith(("RuntimeError:", "FileNotFoundError:", "PermissionError:")) else "previewed", "error": error, "notice": error, "portrait": info, "category": info["category"], "source_hash_kind": "pending",
                'coverage': {'transport_retries': transport_retries, 'parser_timeout_fallbacks': parser_fallbacks,
                             'recovery_events': attempts, 'runner_elapsed_seconds': round(time.monotonic()-started, 6)}}, None
