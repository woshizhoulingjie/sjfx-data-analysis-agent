import os
import tempfile
import time
import unittest
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

from services.exporter import cleanup_stale_part_files, export_node
from services.large_package import inventory_by_path
from services.scanner import scan_directory
from services.storage import Storage


class StorageAndExportSafetyTests(unittest.TestCase):
    def test_storage_configures_wal_limits_and_can_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "analysis.db"
            storage = Storage(db_path)
            with storage._connect() as connection:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
                )
                self.assertEqual(
                    connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0],
                    storage.sqlite_wal_autocheckpoint,
                )
                self.assertEqual(
                    connection.execute("PRAGMA journal_size_limit").fetchone()[0],
                    storage.sqlite_journal_size_limit,
                )
            result = storage.checkpoint_wal(force=True)
            self.assertFalse(result["skipped"])
            self.assertEqual(result["busy"], 0)

    def test_stale_part_cleanup_does_not_touch_recent_or_published_files(self):
        with tempfile.TemporaryDirectory() as folder:
            output_dir = Path(folder)
            stale = output_dir / "old.zip.part"
            recent = output_dir / "active.zip.part"
            published = output_dir / "result.zip"
            for item in (stale, recent, published):
                item.write_bytes(b"x")
            old = time.time() - 3600
            os.utime(stale, (old, old))
            removed = cleanup_stale_part_files(output_dir, max_age_seconds=60)
            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(published.exists())

    def test_export_size_preflight_happens_before_content_hashing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            output = Path(folder) / "output"
            root.mkdir()
            (root / "large.bin").write_bytes(b"xx")
            with patch("services.exporter._sha256_file") as digest:
                with self.assertRaisesRegex(ValueError, "超过"):
                    export_node(root, root, {}, output, 1, task_topic="测试")
            digest.assert_not_called()

    def test_streaming_export_records_actual_source_sha256(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            output = Path(folder) / "output"
            root.mkdir()
            (root / "a.txt").write_text("流式导出", encoding="utf-8")
            archive = export_node(
                root, root, {}, output, 1024 * 1024, task_topic="测试",
                content_deduplication=False, disk_reserve_bytes=0,
            )
            with zipfile.ZipFile(str(archive)) as package:
                manifest = json.loads(package.read("源文件SHA-256清单.json").decode("utf-8"))
            self.assertEqual(manifest["files"][0]["path"], "a.txt")
            self.assertEqual(len(manifest["files"][0]["sha256"]), 64)

    def test_export_rejects_a_source_replaced_by_an_external_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "source"
            output = base / "output"
            root.mkdir()
            source = root / "a.txt"
            outside = base / "outside.txt"
            source.write_text("inventoried", encoding="utf-8")
            outside.write_text("must-not-export", encoding="utf-8")
            scan = scan_directory(root)
            source.unlink()
            try:
                source.symlink_to(outside)
            except (NotImplementedError, OSError):
                self.skipTest("当前平台不允许创建符号链接")
            with self.assertRaises((OSError, ValueError)):
                export_node(
                    root, root, {}, output, 1024 * 1024, task_topic="测试",
                    inventory_metadata=inventory_by_path(scan),
                    content_deduplication=False, disk_reserve_bytes=0,
                )
            self.assertFalse(list(output.glob("*.zip")))

    def test_job_timeout_slice_can_be_requeued_and_attempt_is_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "agent.db")
            job_id = storage.create_job("scan", task_type="analyze_package")
            first = storage.claim_next_job("worker")
            self.assertEqual(first["attempt_count"], 1)
            self.assertTrue(storage.requeue_job_slice(job_id, "继续"))
            second = storage.claim_next_job("worker")
            self.assertEqual(second["attempt_count"], 2)

    def test_timeout_requeue_cannot_override_concurrent_cancellation(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "agent.db")
            job_id = storage.create_job("scan", task_type="analyze_package")
            storage.claim_next_job("worker")
            storage.cancel_job(job_id)
            self.assertFalse(storage.requeue_job_slice(job_id, "继续"))
            final = storage.finalize_job(job_id)
            self.assertEqual(final["status"], "cancelled")
            self.assertTrue(final["cancel_requested"])

    def test_evidence_search_returns_bounded_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "agent.db")
            storage.replace_evidence_index("scan", [
                {"evidence_id": "E1", "source_path": "a.txt", "text": "销售增长达到预期"},
                {"evidence_id": "E2", "source_path": "b.txt", "text": "风险控制需要加强"},
            ])
            result = storage.search_evidence_index("scan", "销售增长", limit=50)
            self.assertEqual([item["evidence_id"] for item in result], ["E1"])

    def test_existing_output_registration_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "outputs"
            output.mkdir()
            target = root / "outside.txt"
            target.write_text("private", encoding="utf-8")
            link = output / "report.txt"
            try:
                link.symlink_to(target)
            except (NotImplementedError, OSError):
                self.skipTest("当前平台不允许创建符号链接")
            storage = Storage(root / "agent.db")
            storage.register_existing_outputs(output, "owner")
            self.assertIsNone(storage.artifact_owner("report.txt"))

    def test_download_ticket_is_owner_bound_and_single_use(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "agent.db")
            ticket = storage.create_download_ticket("result.zip", "owner-a", ttl_seconds=60)
            self.assertFalse(storage.consume_download_ticket(ticket, "other.zip", "owner-a"))
            self.assertFalse(storage.consume_download_ticket(ticket, "result.zip", "owner-b"))
            self.assertTrue(storage.consume_download_ticket(ticket, "result.zip", "owner-a"))
            self.assertFalse(storage.consume_download_ticket(ticket, "result.zip", "owner-a"))


if __name__ == "__main__":
    unittest.main()
