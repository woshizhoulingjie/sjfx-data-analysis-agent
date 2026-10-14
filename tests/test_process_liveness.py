import errno
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import large_qa_worker
from services.model_dispatch import _alive
from services.package_analysis import _secure_source_snapshot
from services.process_liveness import pid_is_alive, try_lock_file
from services.unified_parser import _pid_is_alive, _scratch_is_leased, cleanup_stale_parse_temp_dirs


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProcessLivenessTests(unittest.TestCase):
    def stop_child(self, child):
        if child.poll() is None:
            child.terminate()
        child.communicate(timeout=10)

    def spawn(self, code, *args, env=None):
        child = subprocess.Popen(
            [sys.executable, "-c", code, *map(str, args)], cwd=PROJECT_ROOT,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        self.addCleanup(self.stop_child, child)
        return child

    def wait_file(self, child, path):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists():
                return
            if child.poll() is not None:
                output, error = child.communicate()
                self.fail("child exited before readiness: {} {}".format(output, error))
            time.sleep(0.02)
        self.fail("child did not become ready")

    def child_env(self, root):
        return dict(
            os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(PROJECT_ROOT),
            SJFX_STATE_DIR=str(root / "state"), SJFX_PARSE_TEMP_DIR=str(root / "scratch"),
            HOST="127.0.0.1", SJFX_EXTRA_BIND_HOSTS="", AUTH_REQUIRED="0",
            ENABLE_TRANSLATION="0", ENABLE_IMPORT_TRANSLATION="0", LARGE_QA_ENABLED="1",
            LARGE_QA_DEMAND_ONLY="1", LARGE_QA_IDLE_SLEEP="1",
        )

    def test_live_sleeping_child_survives_both_existing_pid_probes(self):
        child = self.spawn("import time; time.sleep(60)")
        for _ in range(10):
            self.assertTrue(_alive(child.pid))
            self.assertTrue(_pid_is_alive(child.pid))
        with self.assertRaises(subprocess.TimeoutExpired):
            child.wait(timeout=0.1)
        self.assertIsNone(child.poll())
        self.stop_child(child)
        self.assertFalse(_alive(child.pid))
        self.assertFalse(_pid_is_alive(child.pid))

    @unittest.skipUnless(os.name == "nt", "Windows must never use os.kill for a PID probe")
    def test_windows_probes_never_call_kill(self):
        child = self.spawn("import time; time.sleep(60)")
        with patch("services.process_liveness.os.kill", side_effect=AssertionError("signal probe forbidden")):
            self.assertTrue(_alive(child.pid))
            self.assertTrue(_pid_is_alive(child.pid))
            self.assertIsNone(child.poll())

    def test_invalid_pid_is_not_a_live_owner(self):
        for pid in (0, -1, None, "123", True):
            with self.subTest(pid=pid):
                self.assertFalse(pid_is_alive(pid))

    def test_windows_access_denied_is_conservatively_live(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = None
        api = Mock()
        for error, alive in [(5, True), (87, False), (1168, False)]:
            with self.subTest(error=error), patch("services.process_liveness.os.name", "nt"), patch(
                "services.process_liveness._windows_api", return_value=(api, kernel)
            ):
                api.get_last_error.return_value = error
                self.assertEqual(pid_is_alive(987654), alive)
        kernel.WaitForSingleObject.assert_not_called()

    def test_posix_probe_preserves_dead_and_access_denied_semantics(self):
        for error, alive in [(errno.ESRCH, False), (errno.EPERM, True), (errno.EINVAL, False)]:
            with self.subTest(error=error), patch("services.process_liveness.os.name", "posix"), patch(
                "services.process_liveness.os.kill", side_effect=OSError(error, "fixture")
            ) as probe:
                self.assertEqual(pid_is_alive(987654), alive)
                probe.assert_called_once_with(987654, 0)

    def test_descriptor_lock_competes_across_processes_and_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lock, ready = root / "worker.lock", root / "ready"
            code = (
                "import sys,time; from pathlib import Path; "
                "from services.process_liveness import try_lock_file; "
                "handle=Path(sys.argv[1]).open('a+b'); "
                "assert try_lock_file(handle); Path(sys.argv[2]).write_text('ready'); time.sleep(60)"
            )
            child = self.spawn(code, lock, ready, env=self.child_env(root))
            self.wait_file(child, ready)
            challenger = (
                "import sys; from pathlib import Path; from services.process_liveness import try_lock_file; "
                "handle=Path(sys.argv[1]).open('a+b'); sys.exit(0 if try_lock_file(handle) else 23)"
            )
            def compete():
                return subprocess.run([sys.executable, "-c", challenger, str(lock)], cwd=PROJECT_ROOT,
                                      env=self.child_env(root), capture_output=True, timeout=10).returncode
            self.assertEqual(compete(), 23)
            self.stop_child(child)
            self.assertTrue(lock.exists())
            self.assertEqual(compete(), 0)

    def test_real_snapshot_lease_prevents_cleanup_until_child_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.txt"
            source.write_text("verified snapshot", encoding="utf-8")
            ready, scratch_root = root / "ready", root / "scratch"
            code = """
import json, sys, time
from pathlib import Path
from config import Config
from services.package_analysis import _secure_source_snapshot
root = Path(sys.argv[1])
source = root / 'source.txt'
info = source.stat()
Config.PARSE_TEMP_DIR = root / 'scratch'
Config.PARSE_TEMP_DISK_RESERVE_BYTES = 0
node = {'name':source.name, 'path':source.name, 'size':info.st_size,
        'modified_at_ns':info.st_mtime_ns, 'device':info.st_dev, 'inode':info.st_ino}
with _secure_source_snapshot(root, node) as snapshot:
    (root / 'ready').write_text(json.dumps(str(snapshot.parent)), encoding='utf-8')
    time.sleep(60)
"""
            child = self.spawn(code, root, env=self.child_env(root))
            self.wait_file(child, ready)
            scratch = Path(json.loads(ready.read_text(encoding="utf-8")))
            self.assertTrue(_scratch_is_leased(scratch))
            # A dead helper PID must not defeat its parent's live descriptor lease.
            with patch("services.unified_parser._pid_is_alive", return_value=False):
                self.assertEqual(cleanup_stale_parse_temp_dirs(scratch_root, stale_seconds=0), 0)
            self.assertEqual((scratch / "source.txt").read_text(), "verified snapshot")
            self.assertIsNone(child.poll())
            self.stop_child(child)
            self.assertFalse(_scratch_is_leased(scratch))
            # Cleanup is deliberately best effort when Windows briefly retains
            # an exited child's file handles; a later pass must reclaim it.
            removed = 0
            deadline = time.monotonic() + 3
            while scratch.exists() and time.monotonic() < deadline:
                removed += cleanup_stale_parse_temp_dirs(scratch_root, stale_seconds=0)
                if scratch.exists():
                    time.sleep(0.02)
            self.assertEqual(removed, 1)
            self.assertFalse(scratch.exists())
            self.assertEqual(source.read_text(), "verified snapshot")

    def test_snapshot_lock_failure_removes_private_scratch_and_keeps_source(self):
        from config import Config
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.txt"
            source.write_text("original")
            scratch = root / "scratch"
            node = {"name": source.name, "path": source.name, "size": source.stat().st_size}
            with patch.object(Config, "PARSE_TEMP_DIR", scratch), patch.object(Config, "PARSE_TEMP_DISK_RESERVE_BYTES", 0), patch(
                "services.package_analysis.try_lock_file", return_value=False
            ):
                with self.assertRaises(BlockingIOError):
                    with _secure_source_snapshot(root, node):
                        self.fail("unleased snapshot must not be exposed")
            self.assertEqual(list(scratch.iterdir()), [])
            self.assertEqual(source.read_text(), "original")

    def test_real_large_qa_workers_compete_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_db, base = root / "source.db", root / "indexes"
            with closing(sqlite3.connect(source_db)) as db:
                # The real read-only worker reads the package stamp before
                # readiness, even when this lock fixture has no package rows.
                db.execute("CREATE TABLE packages(id TEXT PRIMARY KEY, root_path TEXT NOT NULL, "
                           "owner_id TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL, "
                           "progress INTEGER NOT NULL DEFAULT 0, counts TEXT NOT NULL DEFAULT '{}', "
                           "created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                db.commit()
            command = [sys.executable, str(PROJECT_ROOT / "scripts" / "large_qa_worker.py"),
                       "--source-db", str(source_db), "--base", str(base), "--summaries-only"]
            env = self.child_env(root)
            first = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8")
            self.addCleanup(self.stop_child, first)
            self.wait_file(first, base / "worker_status.json")
            second = subprocess.run(command + ["--once"], cwd=PROJECT_ROOT, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("Another large QA worker owns this index directory", second.stderr)
            self.assertIsNone(first.poll())
            self.stop_child(first)
            restarted = subprocess.run(command + ["--once"], cwd=PROJECT_ROOT, env=env,
                                       capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(json.loads(restarted.stdout), {})

    def test_worker_exception_releases_lock_in_same_process(self):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(base=folder)
            with patch.object(large_qa_worker, "_run_locked", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    large_qa_worker.run(args)
            with (Path(folder) / "worker.lock").open("a+b") as handle:
                self.assertTrue(try_lock_file(handle))


if __name__ == "__main__":
    unittest.main()
