import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from services.large_package import build_policy, package_resource_plan
from services.package_exploration import PreviewBudget, preview_file


class LargeDirectoryModeTests(unittest.TestCase):
    def test_small_package_keeps_standard_flow(self):
        policy = build_policy({"file_count": 100, "total_size": 900 * 1024 * 1024}, {"large_directory_mode": True})
        self.assertEqual(policy["mode"], "standard")
        self.assertTrue(policy["background_backfill"])
        self.assertEqual(policy["preview_hash_mode"], "full")

    def test_large_package_is_directory_first(self):
        policy = build_policy({"file_count": 200000, "total_size": 80 * 1024**3}, {
            "large_directory_mode": True,
            "directory_model_file_limit": 120,
        })
        self.assertEqual(policy["mode"], "large_directory")
        self.assertFalse(policy["background_backfill"])
        self.assertEqual(policy["initial_parse_files"], 120)
        self.assertEqual(policy["preview_hash_mode"], "metadata")
        plan = package_resource_plan(
            {"file_count": 200000, "total_size": 80 * 1024**3},
            state_free_bytes=10**12, temp_free_bytes=10**8,
            preview_bytes_per_file=64 * 1024,
            preview_total_bytes=8 * 1024**3,
            max_content_bytes=10 * 1024**3,
            large_directory_mode=True,
        )
        self.assertEqual(plan["mandatory_hash_read_bytes"], 0)
        self.assertLess(plan["required_temp_bytes"], 1024 * 1024)
        self.assertTrue(plan["ready"])

    def test_directory_preview_uses_metadata_hash(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "records.jsonl"
            path.write_bytes(b"{\"record\":true}\n" * 10000)
            node = {
                "path": path.name,
                "size": path.stat().st_size,
                "modified_at_ns": path.stat().st_mtime_ns,
                "inode": path.stat().st_ino,
                "device": path.stat().st_dev,
            }
            preview = preview_file(
                root, node, per_file_bytes=4096,
                budget=PreviewBudget(4096), hash_source=False,
            )
            self.assertEqual(preview["status"], "previewed")
            self.assertEqual(preview["hash_status"], "metadata_only")
            self.assertEqual(preview["source_hash_kind"], "metadata")
            self.assertLessEqual(preview["sampled_bytes"], 4096)


if __name__ == "__main__":
    unittest.main()
