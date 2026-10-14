"""Read-only PID probes and descriptor-owned locks on Windows and POSIX."""

import errno
from functools import lru_cache
import logging
import os
import signal
import subprocess
import sys


def _valid_pid(pid):
    """Accept builtin PID integers within pid_t range, without coercion."""
    return type(pid) is int and 0 < pid <= 0x7fffffff


def process_rss_bytes(pid):
    """Read RSS on Linux and Windows; unavailable probes never mean zero RSS."""
    if not _valid_pid(pid):
        return None
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/{}/statm".format(pid), "r", encoding="ascii") as handle:
                return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError, AttributeError):
            pass
    try:
        import psutil
        return int(psutil.Process(pid).memory_info().rss)
    except Exception:
        pass
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in
                ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                 "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                 "PagefileUsage", "PeakPagefileUsage")]

        _ctypes, kernel = _windows_api()
        query = kernel.K32GetProcessMemoryInfo
        query.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
        query.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if handle:
            try:
                info = MEMORY_COUNTERS()
                info.cb = ctypes.sizeof(info)
                if query(handle, ctypes.byref(info), info.cb):
                    return int(info.WorkingSetSize)
            finally:
                kernel.CloseHandle(handle)
    return None


def available_memory_bytes():
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", "r", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        pass
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MEMORY_STATUS(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in
                ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                 "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]

        _ctypes, kernel = _windows_api()
        kernel.GlobalMemoryStatusEx.argtypes = (ctypes.c_void_p,)
        kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
        info = MEMORY_STATUS()
        info.dwLength = ctypes.sizeof(info)
        if kernel.GlobalMemoryStatusEx(ctypes.byref(info)):
            return int(info.ullAvailPhys)
    return None


def initialize_process_family():
    """Give a POSIX helper its own group and preserve Linux parent-death cleanup."""
    if os.name == "nt" or not hasattr(os, "setsid"):
        return
    parent_pid = os.getppid()
    os.setsid()
    if not sys.platform.startswith("linux"):
        return
    import ctypes

    def parent_exit_handler(_signum, _frame):
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        finally:
            os._exit(128 + signal.SIGTERM)

    signal.signal(signal.SIGTERM, parent_exit_handler)
    try:
        ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
        if os.getppid() != parent_pid:
            parent_exit_handler(signal.SIGTERM, None)
    except (AttributeError, OSError):
        pass


class _WindowsProcessJob:
    """A supervisor-owned Job Object kills helpers if the supervisor exits."""

    def __init__(self, kernel, handle, extra_handles=()):
        self.kernel, self.handle = kernel, handle
        self._extra_handles = list(extra_handles)

    def close(self):
        extra_handles, self._extra_handles = self._extra_handles, []
        for handle in reversed(extra_handles):
            self.kernel.CloseHandle(handle)
        handle, self.handle = self.handle, None
        if handle:
            self.kernel.CloseHandle(handle)

    def contains(self, pid):
        """Read membership in any owned Job without changing process ownership."""
        if not _valid_pid(pid):
            return False
        import ctypes
        from ctypes import wintypes
        process_handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not process_handle:
            return False
        try:
            for handle in [self.handle] + self._extra_handles:
                if handle:
                    assigned = wintypes.BOOL()
                    if self.kernel.IsProcessInJob(process_handle, handle, ctypes.byref(assigned)) and assigned.value:
                        return True
            return False
        finally:
            self.kernel.CloseHandle(process_handle)


def own_process_tree(pid):
    """Attach a just-started Windows helper to a kill-on-close Job Object.

    Callers gate child work until this returns. Modern Windows permits nested
    jobs, so parser/legacy helpers can retain their own bounded lifetimes.
    Return None when the OS disallows assignment; explicit tree termination
    remains the fallback in that case.
    """
    if not _valid_pid(pid) or pid <= 1 or os.name != "nt":
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return None
    import ctypes
    from ctypes import wintypes

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _ctypes, kernel = _windows_api()
    def failed(action):
        logging.getLogger(__name__).warning(
            "Cannot own Windows helper tree pid=%s action=%s error=%s",
            pid, action, ctypes.get_last_error())
        return None
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    kernel.IsProcessInJob.restype = wintypes.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        return failed("create_job")
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    process_handle = None
    descendants = []
    extra_handles = []
    try:
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return failed("set_job_limit")
        process_handle = kernel.OpenProcess(0x0100 | 0x0001 | 0x1000, False, pid)
        if not process_handle or not kernel.AssignProcessToJobObject(handle, process_handle):
            return failed("assign_root")
        # Assigning the Windows venv redirector does not retroactively add
        # the real Python interpreter it already started. Assign the leader
        # first (future descendants inherit), then attach the existing tree
        # while application work is still behind the caller's start gate.
        descendants = parent.children(recursive=True)
        for child in descendants:
            child_handle = kernel.OpenProcess(0x0100 | 0x0001 | 0x1000, False, child.pid)
            if not child_handle:
                if not child.is_running():
                    continue
                return failed("open_descendant")
            try:
                # Keeping the kernel handle open prevents PID reuse between
                # this identity check and assignment of that process object.
                if not child.is_running():
                    continue
                assigned = wintypes.BOOL()
                if not kernel.IsProcessInJob(child_handle, handle, ctypes.byref(assigned)):
                    return failed("query_descendant_job")
                if not assigned.value and not kernel.AssignProcessToJobObject(handle, child_handle):
                    # A venv redirector's interpreter can already belong to
                    # a different Job hierarchy. A separate nested Job owns
                    # that existing branch; the composite owner closes all
                    # handles, including its subsequently inherited children.
                    branch_handle = kernel.CreateJobObjectW(None, None)
                    if not branch_handle:
                        return failed("create_descendant_job")
                    extra_handles.append(branch_handle)
                    if not kernel.SetInformationJobObject(branch_handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                        return failed("set_descendant_job_limit")
                    if not kernel.AssignProcessToJobObject(branch_handle, child_handle):
                        return failed("assign_descendant_job")
            finally:
                kernel.CloseHandle(child_handle)
        job = _WindowsProcessJob(kernel, handle, extra_handles)
        handle = None
        extra_handles = []
        return job
    except psutil.Error:
        return None
    finally:
        if process_handle:
            kernel.CloseHandle(process_handle)
        if handle:
            # A failed gate must not leave an existing, unassigned interpreter
            # behind its redirector. psutil's process identity protects reuse.
            for child in reversed(descendants):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            kernel.CloseHandle(handle)
        for extra_handle in reversed(extra_handles):
            kernel.CloseHandle(extra_handle)


def terminate_process_tree(process, grace_seconds=2, job=None, private_group=False):
    """Reap a supervisor-owned multiprocessing Process or Popen and descendants."""
    if process is None:
        if job is not None:
            job.close()
        return
    grace = max(0.1, float(grace_seconds))

    def alive():
        return process.is_alive() if hasattr(process, "is_alive") else process.poll() is None

    def wait(seconds):
        if hasattr(process, "join"):
            process.join(timeout=seconds)
        else:
            try:
                process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                pass

    pid = getattr(process, "pid", None)
    if not _valid_pid(pid) or pid <= 1:
        # killpg(1, ...) may become kill(-1, ...) on POSIX. Never let an
        # invalid PID or MagicMock's __index__ reach OS process operations.
        try:
            if job is not None:
                job.close()
        finally:
            try:
                wait(0.1)
            except (AssertionError, ValueError):
                pass  # An unstarted or already-closed Process cannot be joined.
        return

    if job is not None:
        job.close()  # owned handle, not a PID lookup: no PID reuse ambiguity
        wait(grace)
        if not alive():
            return
    if not alive():
        if os.name != "nt" and private_group:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        wait(0.1)
        return
    if os.name == "nt":
        # Keep psutil Process objects (including creation times) before the
        # root can disappear. Their terminate/kill methods guard PID reuse.
        family = []
        try:
            import psutil
            parent = psutil.Process(pid)
            family = list(reversed(parent.children(recursive=True))) + [parent]
            for child in family:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            _gone, remaining = psutil.wait_procs(family, timeout=grace)
            for child in remaining:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(remaining, timeout=2)
        except Exception:
            # psutil is a declared dependency; taskkill covers reduced installs.
            if alive():
                try:
                    subprocess.run([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                                 "System32", "taskkill.exe"),
                                    "/PID", str(pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=grace + 2, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.SubprocessError):
                    pass
        wait(grace)
    else:
        group = None
        try:
            if os.getpgid(pid) == pid:
                group = pid
                os.killpg(group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if group is None and alive():
            process.terminate()
        wait(grace)
        if group is not None:
            # The leader can exit before a native grandchild. Reap the saved
            # private group even when the direct child has already finished.
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    if alive():
        process.kill() if hasattr(process, "kill") else process.terminate()
        wait(2)


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return ctypes, kernel


def pid_is_alive(pid):
    """Retain an owner on access errors; never send a signal on Windows."""
    if not _valid_pid(pid):
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        if pid > 0xFFFFFFFF:
            return False
        ctypes, kernel = _windows_api()
        # SYNCHRONIZE grants a zero-time wait, without termination rights.
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() not in {87, 1168}  # nonexistent PID
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno not in {errno.ESRCH, errno.EINVAL}


def try_lock_file(handle):
    """Acquire an exclusive lock without waiting; closing the handle releases it.

    Every Windows participant locks byte zero, including an empty file (Windows
    permits locking beyond EOF). POSIX participants use the existing flock
    protocol. The file must stay at the same path while any owner holds it.
    """
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
