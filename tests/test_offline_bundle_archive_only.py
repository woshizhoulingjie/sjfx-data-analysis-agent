"""Small synthetic fixtures test archive integrity and owned-stage cleanup only.

Fixture archives are marked synthetic and must never be used as deliveries.
Production builds must omit allow_synthetic and reject these placeholder inputs.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


REPO = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "packaging" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load_module("archive_only_test_builder", "build_offline_bundle.py")
VERIFIER = load_module("archive_only_test_verifier", "verify_bundle.py")
FIXED_TIME = datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)


class ArchiveOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sjfx-synthetic-archive-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.runtime = self.root / "runtime"
        self.models = self.root / "models"
        self.tools = self.root / "tools"
        self.output = self.root / "output"
        for directory in (self.source, self.runtime, self.models, self.tools, self.output):
            directory.mkdir()
        for filename in ("verify_bundle.py", "install_windows.ps1", "install_linux.sh",
                         "upgrade_windows.ps1", "upgrade_linux.sh",
                         "uninstall_windows.ps1", "uninstall_linux.sh"):
            target = self.source / "packaging" / filename
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(REPO / "packaging" / filename, target)
        self.write(self.source, "app.py", b"# synthetic application, never a delivery\n")
        self.write(self.source, "services/example.py", b"VALUE = 1\n")
        self.write(self.source, "services/tests/excluded.py", b"must be excluded\n")
        self.write(self.source, "data/customer.db", b"synthetic private fixture\n")
        self.write(self.source, ".env", b"SYNTHETIC_SECRET=not-a-real-secret\n")
        self.write(self.source, "scratch.py", b"not an application allowlist entry\n")
        self.write(self.runtime, "python.exe", b"synthetic placeholder interpreter\n")
        self.write(self.runtime, "bin/python3", b"synthetic placeholder interpreter\n")
        self.write(self.runtime, "data/package.dat", b"legitimate resource directory\n")
        self.write(self.runtime, "tests/fixture.py", b"must be excluded\n")
        self.write(self.models, "fixture/model.dat", b"synthetic model\n")
        self.write(self.tools, "fixture/tool.dat", b"synthetic tool\n")
        self.write(self.tools, "licenses/component/tests/copyright", b"synthetic test-source copyright\n")
        self.write(self.output, "old-archive.zip", b"prior archive sentinel\n")
        self.write(self.output, "evidence/old.json", b'{"prior": true}\n')
        self.write(self.output, "old-extraction/app.py", b"prior extracted payload\n")
        self.write(self.output, ".build-foreign-owner/sentinel.txt", b"unowned temporary directory\n")

    @staticmethod
    def write(root, relative, data):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def snapshot(self):
        return {path.relative_to(self.output).as_posix(): (
            "directory" if path.is_dir() else "file",
            None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.S_IMODE(path.stat().st_mode),
        ) for path in self.output.rglob("*")}

    def args(self, target, *, synthetic=True):
        return argparse.Namespace(source=str(self.source), runtime=str(self.runtime),
            models=str(self.models), tools=str(self.tools), output=str(self.output),
            target=target, archive_only=True, allow_synthetic=synthetic)

    def frozen_build(self, args):
        with mock.patch.object(BUILDER, "datetime") as clock:
            clock.now.return_value = FIXED_TIME
            return BUILDER.build(args)

    @staticmethod
    def read_archive(archive):
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as source:
                rows = {info.filename: (source.read(info), (info.external_attr >> 16) & 0o777)
                        for info in source.infolist() if not info.is_dir()}
        else:
            with tarfile.open(archive, "r:gz") as source:
                members = source.getmembers()
                assert all(member.isdir() or member.isfile() for member in members)
                assert all(member.uid == member.gid == 0 for member in members)
                rows = {member.name: (source.extractfile(member).read(), member.mode)
                        for member in members if member.isfile()}
        roots = {name.split("/", 1)[0] for name in rows}
        assert len(roots) == 1
        prefix = next(iter(roots)) + "/"
        return {name[len(prefix):]: row for name, row in rows.items()}

    def assert_archive_contract(self, archive, target):
        report = VERIFIER.verify_archive(archive, strict=True)
        self.assertTrue(report["ok"], report)
        rows = self.read_archive(archive)
        manifest = json.loads(rows.pop("manifest.json")[0].decode("utf-8"))
        self.assertEqual(manifest["target"], target)
        self.assertEqual(manifest["validation"], {
            "runtime": "synthetic_override", "resources": "synthetic_override"})
        self.assertEqual(manifest["source_root"], "app")
        self.assertEqual(manifest["inputs"], {"runtime": "runtime", "models": "models", "tools": "tools"})
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertEqual({item["path"] for item in manifest["files"]}, set(rows))
        self.assertEqual(manifest["file_count"], len(rows))
        self.assertEqual(manifest["total_bytes"], sum(len(data) for data, _ in rows.values()))
        self.assertEqual(report["checked"], len(rows))
        for item in manifest["files"]:
            data, mode = rows[item["path"]]
            self.assertEqual(item["bytes"], len(data), item["path"])
            self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest(), item["path"])
            self.assertEqual(item["mode"], mode, item["path"])
            self.assertFalse(mode & ~0o777)
        self.assertIn("app/services/example.py", rows)
        self.assertIn("runtime/data/package.dat", rows)
        self.assertIn("tools/licenses/component/tests/copyright", rows)
        for forbidden in ("app/.env", "app/data/customer.db", "app/scratch.py",
                          "app/services/tests/excluded.py", "runtime/tests/fixture.py"):
            self.assertNotIn(forbidden, rows)
        if target == "linux-x64":
            self.assertEqual(rows["runtime/bin/python3"][1], 0o755)
            self.assertEqual(rows["start_sjfx.sh"][1], 0o755)

    def test_both_archive_formats_have_complete_manifest_and_clean_only_owned_stage(self):
        for target in ("windows-x64", "linux-x64"):
            with self.subTest(target=target):
                before = self.snapshot()
                archive = self.frozen_build(self.args(target))
                self.assert_archive_contract(archive, target)
                after = self.snapshot()
                self.assertEqual({key: after[key] for key in before}, before)
                self.assertEqual(set(after) - set(before), {archive.name})

    def test_linux_audited_entrypoints_restore_ntfs_execute_bits_in_manifest_and_tar(self):
        for relative in sorted(BUILDER.LINUX_EXECUTABLE_PATHS):
            self.assertEqual(BUILDER._target_mode(Path(relative), stat.S_IFREG | 0o666,
                                                "linux-x64"), 0o755, relative)
            self.assertEqual(BUILDER._target_mode(Path(relative), stat.S_IFREG | 0o7666,
                                                "linux-x64"), 0o755, relative)
            self.assertEqual(BUILDER._target_mode(Path(relative), stat.S_IFREG | 0o666,
                                                "windows-x64"), 0o666, relative)
            if relative.startswith("tools/"):
                self.write(self.tools, relative[len("tools/"):],
                           b"synthetic audited entrypoint fixture\n").chmod(0o666)
        unreviewed = "tools/native/usr/bin/unreviewed-tool"
        self.write(self.tools, unreviewed[len("tools/"):],
                   b"synthetic unreviewed resource fixture\n").chmod(0o666)
        self.assertEqual(BUILDER._target_mode(Path(unreviewed), stat.S_IFREG | 0o7666,
                                            "linux-x64"), 0o666)
        archive = self.frozen_build(self.args("linux-x64"))
        self.assert_archive_contract(archive, "linux-x64")
        rows = self.read_archive(archive)
        manifest = json.loads(rows["manifest.json"][0].decode("utf-8"))
        manifest_modes = {row["path"]: row["mode"] for row in manifest["files"]}
        for relative in BUILDER.LINUX_EXECUTABLE_PATHS:
            self.assertEqual(rows[relative][1], 0o755, relative)
            self.assertEqual(manifest_modes[relative], 0o755, relative)
        self.assertEqual(rows[unreviewed][1], 0o666)
        self.assertEqual(manifest_modes[unreviewed], 0o666)

    def test_copy_failure_cleans_owned_stage_and_preserves_all_prior_outputs(self):
        for target in ("windows-x64", "linux-x64"):
            with self.subTest(target=target):
                before = self.snapshot()
                with mock.patch.object(BUILDER, "_copy_application", side_effect=RuntimeError("injected copy failure")):
                    with self.assertRaisesRegex(RuntimeError, "injected copy failure"):
                        self.frozen_build(self.args(target))
                self.assertEqual(self.snapshot(), before)

    def test_production_defaults_reject_synthetic_runtime_without_creating_outputs(self):
        for target in ("windows-x64", "linux-x64"):
            with self.subTest(target=target):
                before = self.snapshot()
                with self.assertRaisesRegex(RuntimeError, "implausibly small"):
                    self.frozen_build(self.args(target, synthetic=False))
                with self.assertRaisesRegex(RuntimeError, "refusing a placeholder bundle"):
                    BUILDER._validate_resources(self.models, self.tools, target=target)
                self.assertEqual(self.snapshot(), before)

    def test_same_timestamp_archive_cannot_overwrite_a_prior_archive(self):
        for target, suffix in (("windows-x64", ".zip"), ("linux-x64", ".tar.gz")):
            with self.subTest(target=target):
                prior = self.write(self.output, "sjfx-offline-" + target + "-20010203_040506" + suffix,
                                   b"same-name previous archive sentinel\n")
                before = self.snapshot()
                with self.assertRaises(FileExistsError):
                    self.frozen_build(self.args(target))
                self.assertEqual(self.snapshot(), before)
                prior.unlink()

    def test_failed_archive_write_does_not_leave_a_delivery_named_partial_archive(self):
        for target in ("windows-x64", "linux-x64"):
            with self.subTest(target=target):
                before = self.snapshot()
                def interrupted_archive(root, stream, platform):
                    stream.write(b"injected interrupted archive\n")
                    stream.flush()
                    raise RuntimeError("injected archive failure")
                with mock.patch.object(BUILDER, "_write_archive", side_effect=interrupted_archive):
                    with self.assertRaisesRegex(RuntimeError, "injected archive failure"):
                        self.frozen_build(self.args(target))
                self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
