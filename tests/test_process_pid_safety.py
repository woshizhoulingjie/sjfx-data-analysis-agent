"""Invalid PID fixtures must never reach native lookup or signal interfaces."""
from contextlib import ExitStack
import multiprocessing as mp
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from services import process_liveness as lifecycle


class CoerciblePID:
    def __int__(self):
        raise AssertionError('PID integer coercion is forbidden')

    def __index__(self):
        raise AssertionError('PID index coercion is forbidden')


class PIDIntSubclass(int):
    pass


INVALID_PIDS = [None, False, True, 0, 1, -1, '123', '0', 123.0,
                object(), Mock(name='pid'), MagicMock(name='pid'), CoerciblePID(),
                Mock(spec=int), MagicMock(spec=int), PIDIntSubclass(123),
                0x80000000, 2 ** 64]
INVALID_READ_PIDS = [pid for pid in INVALID_PIDS if not isinstance(pid, int) or pid != 1 or isinstance(pid, bool)]


def forbid_native_calls(platform_name):
    stack = ExitStack()
    forbidden = Mock(side_effect=AssertionError('invalid PID reached a native process interface'))
    stack.enter_context(patch.object(lifecycle, 'os', SimpleNamespace(
        name=platform_name, getpgid=forbidden, killpg=forbidden, kill=forbidden,
        getpid=forbidden, sysconf=forbidden)))
    stack.enter_context(patch.object(lifecycle, 'sys', SimpleNamespace(
        platform='linux' if platform_name == 'posix' else 'win32')))
    stack.enter_context(patch.object(lifecycle, '_windows_api', forbidden))
    stack.enter_context(patch.object(lifecycle.subprocess, 'run', forbidden))
    stack.enter_context(patch.dict('sys.modules', {'psutil': SimpleNamespace(
        Process=forbidden, wait_procs=forbidden)}))
    stack.enter_context(patch('builtins.open', forbidden))
    return stack, forbidden


@pytest.mark.parametrize('platform_name', ['nt', 'posix'])
@pytest.mark.parametrize('pid', INVALID_PIDS)
def test_invalid_pid_cleanup_closes_job_and_joins_without_lookup_or_signals(platform_name, pid):
    process = SimpleNamespace(pid=pid, is_alive=Mock(return_value=False), join=Mock(),
                              terminate=Mock(), kill=Mock())
    owner = SimpleNamespace(close=Mock())
    stack, forbidden = forbid_native_calls(platform_name)
    with stack:
        lifecycle.terminate_process_tree(process, job=owner, private_group=True)
        assert lifecycle.own_process_tree(pid) is None
    owner.close.assert_called_once_with()
    process.join.assert_called_once_with(timeout=.1)
    process.is_alive.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    forbidden.assert_not_called()


@pytest.mark.parametrize('platform_name', ['nt', 'posix'])
def test_invalid_popen_pid_can_wait_without_polling_or_signals(platform_name):
    process = SimpleNamespace(pid=0, poll=Mock(return_value=None), wait=Mock(),
                              terminate=Mock(), kill=Mock())
    stack, forbidden = forbid_native_calls(platform_name)
    with stack:
        lifecycle.terminate_process_tree(process, private_group=True)
    process.wait.assert_called_once_with(timeout=.1)
    process.poll.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    forbidden.assert_not_called()


def test_unstarted_multiprocessing_process_cleanup_is_safe():
    process = mp.get_context('spawn').Process()
    stack, forbidden = forbid_native_calls(os.name)
    try:
        with stack:
            lifecycle.terminate_process_tree(process, private_group=True)
        assert process.pid is None
        forbidden.assert_not_called()
    finally:
        process.close()


@pytest.mark.parametrize('platform_name', ['nt', 'posix'])
@pytest.mark.parametrize('pid', INVALID_READ_PIDS)
def test_invalid_pid_read_probes_do_not_coerce_or_lookup(platform_name, pid):
    kernel = SimpleNamespace(OpenProcess=Mock(), CloseHandle=Mock())
    owner = lifecycle._WindowsProcessJob(kernel, 42)
    stack, forbidden = forbid_native_calls(platform_name)
    with stack:
        assert lifecycle.process_rss_bytes(pid) is None
        assert lifecycle.pid_is_alive(pid) is False
        assert owner.contains(pid) is False
    kernel.OpenProcess.assert_not_called()
    kernel.CloseHandle.assert_not_called()
    forbidden.assert_not_called()


@pytest.mark.parametrize('platform_name', ['nt', 'posix'])
def test_read_only_pid_one_probe_remains_supported(platform_name):
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=2048))
    with patch.object(lifecycle, 'os', SimpleNamespace(name=platform_name)), patch.object(
        lifecycle, 'sys', SimpleNamespace(platform='darwin')
    ), patch.dict('sys.modules', {'psutil': SimpleNamespace(Process=Mock(return_value=process))}):
        assert lifecycle.process_rss_bytes(1) == 2048
