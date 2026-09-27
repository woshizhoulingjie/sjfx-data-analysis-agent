"""Reusable, killable CPU-only preview process for the isolated intake queue."""
from __future__ import annotations
import multiprocessing
import os
import time

from services.large_package_resources import ResourcePressure, resource_code


class PreviewCancelled(Exception):
    pass


def _serve(connection):
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

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=1)
            self.process = None
        self.calls = 0

    def run(self, root, item, refine=False, cancel_check=None):
        from services.large_package_preview import settings, portrait, SCHEMA
        if self.process is None or not self.process.is_alive() or self.calls >= 100:
            self.close()
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            try:
                self.process = context.Process(target=_serve, args=(child,), daemon=True)
                self.process.start()
            except BaseException:
                parent.close()
                child.close()
                self.process = None
                raise
            child.close()
            self.connection = parent
        self.calls += 1
        options = settings(refine)
        self.connection.send({"root": str(root), "item": {key: item[key] for key in ("path", "name", "size", "modified_at_ns", "sensitive", "extension", "source_kind", "source_metadata") if key in item}, "refine": refine})
        started = time.monotonic()
        error = ""
        try:
            while True:
                if cancel_check and cancel_check():
                    self.close()
                    raise PreviewCancelled()
                if self.connection.poll(.15):
                    response = self.connection.recv()
                    if "result" in response:
                        return response["result"]
                    if response.get('resource_code'):
                        self.close()
                        raise ResourcePressure('预览资源暂不可用，文件保留等待重试。', response['resource_code'])
                    error = response.get("error", "轻量解析失败")
                    break
                if not self.process.is_alive():
                    error = "轻量解析进程退出"
                    break
                if time.monotonic() - started > options["seconds"] + 1:
                    error = "轻量解析超时，已释放子进程"
                    self.close()
                    break
                try:
                    with open("/proc/{}/statm".format(self.process.pid)) as handle:
                        rss = int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
                    if rss > 1536 * 1024 * 1024:
                        error = "轻量解析达到 1.5 GiB 内存上限，已释放子进程"
                        self.close()
                        break
                except (OSError, ValueError, IndexError, AttributeError):
                    pass
        except (EOFError, BrokenPipeError, OSError) as exc:
            if resource_code(exc):
                self.close()
                raise
            error = "轻量解析中断：" + str(exc)[:160]
            self.close()
        info = portrait(item, [], [], [], refine, [error])
        info["recognition_status"] = "needs_review" if refine else "needs_more"
        return {"schema_version": SCHEMA, "summary_type": "local", "sample": "", "complete": False,
                "status": "failed" if error.startswith(("RuntimeError:", "FileNotFoundError:", "PermissionError:")) else "previewed", "error": error, "notice": error, "portrait": info, "category": info["category"], "source_hash_kind": "pending"}, None
