"""Process-isolated document parsing with a hard wall-clock boundary.

Native PDF/OCR libraries do not reliably honour a Python thread timeout: a
worker thread can remain blocked in C/CUDA code after ``Future.result`` times
out.  This module keeps one parser process per parser configuration and
terminates that process when a file exceeds its budget, leaving the job Worker
healthy for subsequent files.
"""

from __future__ import annotations

import atexit
import multiprocessing as mp
import os
import threading
import time
import weakref


class ParseIsolationError(RuntimeError):
    """Base error raised by the isolated parser process."""


class ParseIsolationTimeout(ParseIsolationError, TimeoutError):
    """A single parse exceeded its hard wall-clock budget."""


class ParseIsolationMemoryError(ParseIsolationError, MemoryError):
    """The isolated parser exceeded its configured RSS budget."""


def _rss_bytes(pid):
    """Return a child RSS estimate without requiring psutil."""
    try:
        with open("/proc/{}/statm".format(pid), "r", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        try:
            import psutil

            return int(psutil.Process(pid).memory_info().rss)
        except Exception:
            return None


def _parser_server(conn, config):
    """Run in a dedicated process and service one parse request at a time."""
    try:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from services.unified_parser import UnifiedDocumentParser

        parser = UnifiedDocumentParser(
            config.get("artifacts_path"),
            config.get("rapidocr_model_dir"),
            int(config.get("max_chars") or 2_000_000),
            fast_office_ocr=bool(config.get("fast_office_ocr", True)),
        )
        conn.send(("ready",))
    except BaseException as exc:  # pragma: no cover - deployment-only failure
        try:
            conn.send(("startup_error", type(exc).__name__, str(exc)[:1000]))
        except Exception:
            pass
        conn.close()
        return

    try:
        while True:
            request = conn.recv()
            command = request[0] if isinstance(request, (tuple, list)) else None
            if command == "stop":
                return
            if command != "parse" or len(request) != 5:
                conn.send(("error", "ValueError", "无效的解析子进程请求"))
                continue
            _, path, relative_path, mode, archive_depth = request
            try:
                result = parser.parse(
                    path,
                    relative_path,
                    mode=mode,
                    _archive_depth=int(archive_depth or 0),
                )
                conn.send(("ok", result))
            except BaseException as exc:
                # Do not send source text or a full traceback through the IPC
                # channel; the type and bounded message are enough to diagnose
                # the file-level failure in the parent job record.
                try:
                    conn.send(("error", type(exc).__name__, str(exc)[:1000]))
                except Exception:
                    return
    except (EOFError, OSError):
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass


class IsolatedParserRunner:
    """Reusable serial parser process with hard timeout and RSS enforcement."""

    def __init__(self, parser):
        self._config = {
            "artifacts_path": str(parser.artifacts_path) if parser.artifacts_path else None,
            "rapidocr_model_dir": str(parser.rapidocr_model_dir) if parser.rapidocr_model_dir else None,
            "max_chars": int(parser.max_chars),
            "fast_office_ocr": bool(parser.fast_office_ocr),
        }
        preferred = str(os.getenv("SJFX_PARSE_START_METHOD", "")).strip()
        available = mp.get_all_start_methods()
        if preferred not in available:
            preferred = "fork" if "fork" in available else "spawn"
        self._ctx = mp.get_context(preferred)
        self._process = None
        self._recv = None
        self._send = None
        self._lock = threading.RLock()

    @property
    def alive(self):
        return bool(self._process and self._process.is_alive())

    def _start(self, startup_timeout):
        self.close()
        # A duplex pipe is required: the child receives requests and sends
        # results over the same connection.  ``Pipe(False)`` is send-only at
        # one endpoint and caused every real parse to be reported as an
        # unknown response.
        recv, send = self._ctx.Pipe(True)
        process = self._ctx.Process(
            target=_parser_server,
            args=(send, self._config),
            daemon=True,
        )
        try:
            process.start()
        except Exception:
            recv.close()
            send.close()
            raise
        # The parent uses ``recv`` for both directions. Close the duplicate
        # child endpoint in the parent; the child retains its own copy.
        send.close()
        self._process, self._recv, self._send = process, recv, recv
        deadline = time.monotonic() + max(1.0, startup_timeout)
        while time.monotonic() < deadline:
            if recv.poll(0.1):
                message = recv.recv()
                if message[0] == "ready":
                    return
                self._terminate()
                raise ParseIsolationError(
                    "解析子进程启动失败：{}".format(message[-1] if len(message) > 1 else message)
                )
            if not process.is_alive():
                self._terminate()
                raise ParseIsolationError(
                    "解析子进程异常退出（exitcode={})".format(process.exitcode)
                )
        self._terminate()
        raise ParseIsolationTimeout("解析子进程启动超过 {} 秒".format(int(startup_timeout)))

    def parse(
        self,
        path,
        relative_path,
        mode="accurate",
        archive_depth=0,
        timeout=300,
        memory_mb=8192,
    ):
        with self._lock:
            timeout = max(1.0, float(timeout))
            memory_mb = max(256, int(memory_mb))
            if not self.alive or self._recv is None or self._send is None:
                self._start(min(timeout, 30.0))
            try:
                self._send.send(
                    ("parse", str(path), str(relative_path), str(mode), int(archive_depth or 0))
                )
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if self._recv.poll(0.2):
                        message = self._recv.recv()
                        if message[0] == "ok":
                            return message[1]
                        if message[0] == "error":
                            raise ParseIsolationError(
                                "{}：{}".format(message[1], message[2])
                            )
                        raise ParseIsolationError("解析子进程返回了未知消息")
                    if not self.alive:
                        raise ParseIsolationError(
                            "解析子进程异常退出（exitcode={})".format(self._process.exitcode)
                        )
                    rss = _rss_bytes(self._process.pid)
                    if rss is not None and rss > memory_mb * 1024 * 1024:
                        self._terminate()
                        raise ParseIsolationMemoryError(
                            "单文件解析子进程内存超过 {} MB".format(memory_mb)
                        )
                self._terminate()
                raise ParseIsolationTimeout(
                    "单文件解析超过 {} 秒，解析子进程已终止".format(int(timeout))
                )
            except ParseIsolationError:
                raise
            except (BrokenPipeError, EOFError, OSError) as exc:
                self._terminate()
                raise ParseIsolationError("解析子进程通信失败：{}".format(exc)) from exc

    def _terminate(self):
        process = self._process
        self._process = None
        for endpoint in (self._recv, self._send):
            try:
                if endpoint is not None:
                    endpoint.close()
            except Exception:
                pass
        self._recv = self._send = None
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=2)
        else:
            process.join(timeout=1)

    def close(self):
        with self._lock:
            process = self._process
            if process is not None and process.is_alive() and self._send is not None:
                try:
                    self._send.send(("stop",))
                    process.join(timeout=2)
                except Exception:
                    pass
            self._terminate()


_RUNNERS = weakref.WeakKeyDictionary()
_RUNNERS_LOCK = threading.Lock()


def runner_for(parser):
    """Return a cached runner for a ``UnifiedDocumentParser`` instance."""
    with _RUNNERS_LOCK:
        runner = _RUNNERS.get(parser)
        if runner is None:
            runner = IsolatedParserRunner(parser)
            _RUNNERS[parser] = runner
            weakref.finalize(parser, runner.close)
        return runner


@atexit.register
def _close_all_runners():
    with _RUNNERS_LOCK:
        for runner in list(_RUNNERS.values()):
            runner.close()
        _RUNNERS.clear()
