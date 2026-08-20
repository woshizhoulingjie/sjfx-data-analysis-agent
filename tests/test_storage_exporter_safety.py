import os
import tempfile
import time
import unittest
from pathlib import Path

from services.exporter import cleanup_stale_part_files
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


if __name__ == "__main__":
    unittest.main()
