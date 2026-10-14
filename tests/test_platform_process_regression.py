"""Exercise real Windows/Linux helpers, pipes, deadlines and native descendants."""
import json
import signal
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.parse_isolation import (
    IsolatedParserRunner, ParseIsolationCancelled, ParseIsolationMemoryError, ParseIsolationTimeout,
)
from services.process_liveness import (
    available_memory_bytes, initialize_process_family, own_process_tree,
    pid_is_alive, process_rss_bytes, terminate_process_tree,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _slow_parser(connection, config, gate=None):
    """A spawn-safe parser fixture that owns a real external child."""
    initialize_process_family()
    if gate is not None:
        gate.wait(10)
    connection.send(("ready",))
    request = connection.recv()
    native = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    root = Path(config["artifacts_path"])
    (root / "pids.json").write_text(json.dumps([os.getpid(), native.pid]), encoding="utf-8")
    if request[3] == "memory":
        # Real working-set pressure, not a mocked RSS value.
        retained = bytearray(280 * 1024 * 1024)
        retained[0] = 1
    time.sleep(60)


def _supervised_task(job, sender, gate=None):
    initialize_process_family()
    if gate is not None:
        gate.wait(10)
    native = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    root = Path(job["options"]["root"])
    (root / "pids.json").write_text(json.dumps([os.getpid(), native.pid]))
    if job["options"].get("return_result"):
        sender.send({"status": "ok", "result": {"success": True}})
        return
    time.sleep(60)


def _slow_preview(connection, gate=None):
    initialize_process_family()
    if gate is not None:
        gate.wait(10)
    request = connection.recv()
    native = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (Path(request["root"]) / "pids.json").write_text(json.dumps([os.getpid(), native.pid]))
    time.sleep(60)


class PlatformProcessRegressionTests(unittest.TestCase):
    def setUp(self):
        # TEMP can equal SJFX_PARSE_TEMP_DIR in the launcher/test environment.
        # Keep input/PID fixtures outside parser-owned sjfx-* scratch objects.
        self.tmp = tempfile.TemporaryDirectory(prefix="process-regression-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        parse_env = patch.dict(os.environ, {"SJFX_PARSE_TEMP_DIR": str(self.root / "parse-temp")})
        parse_env.start()
        self.addCleanup(parse_env.stop)

    def wait_for(self, predicate, seconds=10):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.03)
        self.fail("process condition did not complete")

    def start_tree(self):
        ready, gate = self.root / "pids.json", self.root / "gate"
        code = """
import json, os, subprocess, sys, time
from pathlib import Path
from services.process_liveness import initialize_process_family
initialize_process_family()
while not Path(sys.argv[2]).exists(): time.sleep(.01)
child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(60)
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(ready), str(gate)], cwd=PROJECT_ROOT)
        owner = own_process_tree(process.pid)
        self.addCleanup(terminate_process_tree, process, .2, owner, True)
        gate.touch()
        self.wait_for(ready.exists)
        return process, owner, json.loads(ready.read_text())

    def assert_dead(self, pids):
        # POSIX unreaped grandchildren can briefly be zombies; Windows waits
        # on their kernel handles. A zombie consumes no execution resources.
        def live(pid):
            if sys.platform.startswith("linux"):
                try:
                    if Path("/proc/{}/stat".format(pid)).read_text().split(")", 1)[1].split()[0] == "Z":
                        return False
                except (OSError, IndexError):
                    pass
            return pid_is_alive(pid)
        self.wait_for(lambda: all(not live(pid) for pid in pids))

    def test_owned_tree_kills_real_native_descendant_and_keeps_other_process(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(terminate_process_tree, unrelated, .2)
        process, owner, pids = self.start_tree()
        if os.name == "nt":
            self.assertIsNotNone(owner, "Windows Job Object must be usable on this host")
        terminate_process_tree(process, .3, owner, private_group=True)
        self.assert_dead(pids)
        self.assertIsNone(unrelated.poll())

    def test_tree_fallback_kills_real_native_descendant(self):
        process, owner, pids = self.start_tree()
        # Ignore the Job here to exercise psutil/taskkill, then close the owned
        # handle during cleanup. Linux exercises its private process group.
        terminate_process_tree(process, .3, private_group=True)
        self.assert_dead(pids)

    @unittest.skipUnless(os.name == "nt", "Windows venv redirector ownership")
    def test_windows_job_attaches_interpreter_already_created_by_redirector(self):
        ready, pids_file, gate = (self.root / name for name in ("waiting", "pids.json", "gate"))
        code = """
import json, os, subprocess, sys, time
from pathlib import Path
root=Path(sys.argv[1]); (root/'waiting').write_text(str(os.getpid()))
while not (root/'gate').exists(): time.sleep(.01)
native=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
(root/'pids.json').write_text(json.dumps([os.getpid(),native.pid]))
time.sleep(60)
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(self.root)], cwd=PROJECT_ROOT)
        self.addCleanup(terminate_process_tree, process, .2)
        self.wait_for(ready.exists)
        actual_pid = int(ready.read_text())
        owner = own_process_tree(process.pid)
        self.assertIsNotNone(owner)
        self.addCleanup(owner.close)
        self.assertTrue(owner.contains(actual_pid), "real interpreter must own a Job before its gate opens")
        gate.touch()
        self.wait_for(pids_file.exists)
        pids = [process.pid] + json.loads(pids_file.read_text())
        owner.close()
        process.wait(timeout=5)
        self.assert_dead(pids)

    @unittest.skipUnless(os.name == "nt", "Windows Job handle closes on supervisor termination")
    def test_windows_supervisor_crash_reaps_tree(self):
        ready = self.root / "pids.json"
        code = """
import json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from services.process_liveness import own_process_tree
root=Path(sys.argv[1]); gate=root/'gate'; ready=root/'pids.json'
child_code='''import json, os, subprocess, sys, time
from pathlib import Path
Path(sys.argv[3]).write_text(str(os.getpid()))
while not Path(sys.argv[1]).exists(): time.sleep(.01)
native=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
Path(sys.argv[2]).write_text(json.dumps([os.getpid(),native.pid]))
time.sleep(60)
'''
waiting=root/'waiting'
process=subprocess.Popen([sys.argv[3],'-c',child_code,str(gate),str(ready),str(waiting)])
while not waiting.exists(): time.sleep(.01)
owner=own_process_tree(process.pid)
assert owner is not None
assert owner.contains(int(waiting.read_text()))
gate.touch()
time.sleep(60)
"""
        # A Windows venv python.exe can be a redirector with a separate real
        # Python child. Kill the actual process owning the Job handle here.
        interpreter = getattr(sys, "_base_executable", sys.executable)
        import sysconfig
        supervisor = subprocess.Popen([interpreter, "-c", code, str(self.root), sysconfig.get_path("purelib"),
                                       sys.executable],
                                      cwd=PROJECT_ROOT)
        self.addCleanup(terminate_process_tree, supervisor, .2)
        self.wait_for(ready.exists)
        pids = json.loads(ready.read_text())
        supervisor.kill()
        supervisor.wait(timeout=5)
        self.assert_dead(pids)

    def parser_runner(self):
        runner = IsolatedParserRunner(SimpleNamespace(
            artifacts_path=self.root, rapidocr_model_dir=None, max_chars=1000, fast_office_ocr=True))
        self.addCleanup(runner.close)
        return runner

    def test_spawn_parser_timeout_reaps_native_descendant(self):
        runner = self.parser_runner()
        with patch("services.parse_isolation._parser_server", _slow_parser):
            with self.assertRaises(ParseIsolationTimeout):
                runner.parse("unused", "unused", timeout=1)
        self.assertFalse(runner.alive)
        self.assert_dead(json.loads((self.root / "pids.json").read_text()))

    def test_spawn_parser_cancellation_reaps_native_descendant(self):
        runner = self.parser_runner()
        with patch("services.parse_isolation._parser_server", _slow_parser):
            with self.assertRaises(ParseIsolationCancelled):
                runner.parse("unused", "unused", timeout=10,
                             cancel_check=lambda: (self.root / "pids.json").exists())
        self.assert_dead(json.loads((self.root / "pids.json").read_text()))

    def test_spawn_parser_enforces_actual_rss_budget(self):
        if (available_memory_bytes() or 0) < 500 * 1024 * 1024:
            self.skipTest("insufficient RAM for the real RSS pressure fixture")
        runner = self.parser_runner()
        with patch("services.parse_isolation._parser_server", _slow_parser):
            with self.assertRaises(ParseIsolationMemoryError):
                runner.parse("unused", "unused", mode="memory", timeout=10, memory_mb=256)
        self.assert_dead(json.loads((self.root / "pids.json").read_text()))

    def test_real_memory_probes_work_without_psutil(self):
        with patch.dict(sys.modules, {"psutil": None}):
            self.assertGreater(process_rss_bytes(os.getpid()), 0)
            self.assertGreater(available_memory_bytes(), 0)

    def test_posix_tree_ownership_needs_no_windows_handle_or_pid(self):
        with patch("services.process_liveness.os.name", "posix"):
            self.assertIsNone(own_process_tree(None))

    def test_posix_group_cleanup_reaps_grandchildren_after_leader_exits(self):
        process = SimpleNamespace(pid=123456, running=True)
        process.is_alive = lambda: process.running
        process.join = lambda timeout: setattr(process, "running", False)
        with patch("services.process_liveness.os.name", "posix"), patch(
            "services.process_liveness.os.getpgid", return_value=process.pid, create=True
        ), patch("services.process_liveness.os.killpg", create=True) as kill_group, patch(
            "services.process_liveness.signal.SIGKILL", 9, create=True
        ):
            terminate_process_tree(process, .1, private_group=True)
        self.assertEqual(kill_group.call_args_list[0].args, (process.pid, signal.SIGTERM))
        self.assertEqual(kill_group.call_args_list[1].args, (process.pid, 9))

    def test_non_linux_posix_process_family_avoids_linux_prctl(self):
        with patch("services.process_liveness.os.name", "posix"), patch(
            "services.process_liveness.sys.platform", "darwin"
        ), patch("services.process_liveness.os.setsid", create=True) as new_session, patch(
            "ctypes.CDLL", side_effect=AssertionError("Linux-only prctl must not load")
        ):
            initialize_process_family()
        new_session.assert_called_once_with()

    def test_configured_legacy_tool_layout_with_exe_and_unicode_spaces(self):
        from services.legacy_office import _find_tool
        tools = self.root / "本地 工具"
        binary = tools / "legacy-office" / "usr" / "bin" / ("antiword.exe" if os.name == "nt" else "antiword")
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"tool fixture")
        with patch.dict(os.environ, {"SJFX_TOOLS_DIR": str(tools)}):
            executable, root = _find_tool("antiword")
        self.assertEqual(Path(executable), binary)
        self.assertEqual(root, binary.parent.parent)

    def test_actual_spawn_parser_can_parse_twice_and_restart_after_close(self):
        from services.unified_parser import UnifiedDocumentParser
        path = self.root / "文字 文件.txt"
        path.write_text("Windows 与 Linux 的解析往返测试。", encoding="utf-8")
        runner = IsolatedParserRunner(UnifiedDocumentParser(max_chars=1000))
        self.addCleanup(runner.close)
        self.assertEqual(runner._ctx.get_start_method(), "spawn")
        for index in range(3):
            document = runner.parse(path, path.name, mode="fast", timeout=20, memory_mb=1024)
            self.assertIn("解析往返测试", document["text"])
            if index == 1:
                runner.close()

    def test_actual_worker_supervision_reaps_descendant_after_result(self):
        import worker
        fake_storage = SimpleNamespace(
            is_job_cancel_requested=lambda _job: False, heartbeat_job=lambda *_args: None,
            has_queued_job_above_priority=lambda _priority: False,
        )
        job = {"id": "fixture", "task_type": "fixture", "options": {
            "root": str(self.root), "return_result": True,
        }}
        with patch.object(worker, "storage", fake_storage), patch.object(
            worker, "_execute_job_child", _supervised_task
        ), patch.object(worker.Config, "WORKER_MONITOR_SECONDS", .05):
            self.assertEqual(worker.execute_supervised(job), {"success": True})
        self.assert_dead(json.loads((self.root / "pids.json").read_text()))

    def test_actual_worker_supervision_cancellation_reaps_descendant(self):
        import worker
        fake_storage = SimpleNamespace(
            is_job_cancel_requested=lambda _job: (self.root / "pids.json").exists(),
            heartbeat_job=lambda *_args: None,
            has_queued_job_above_priority=lambda _priority: False,
        )
        job = {"id": "fixture", "task_type": "fixture", "options": {"root": str(self.root)}}
        with patch.object(worker, "storage", fake_storage), patch.object(
            worker, "_execute_job_child", _supervised_task
        ), patch.object(worker.Config, "WORKER_MONITOR_SECONDS", .05), patch.object(
            worker.Config, "WORKER_TERMINATE_GRACE_SECONDS", .2
        ):
            with self.assertRaises(worker.JobCancelled):
                worker.execute_supervised(job)
        self.assert_dead(json.loads((self.root / "pids.json").read_text()))

    def test_actual_preview_cancellation_reaps_descendant(self):
        from services.large_package_preview_runner import PreviewRunner, PreviewCancelled
        runner = PreviewRunner()
        self.addCleanup(runner.close)
        item = {"path": "fixture.txt", "name": "fixture.txt", "extension": ".txt"}
        with patch("services.large_package_preview_runner._serve", _slow_preview):
            with self.assertRaises(PreviewCancelled):
                runner.run(self.root, item, cancel_check=lambda: (self.root / "pids.json").exists())
        self.assertIsNone(runner.process)
        self.assert_dead(json.loads((self.root / "pids.json").read_text()))


if __name__ == "__main__":
    unittest.main()
