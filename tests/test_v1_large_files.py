import tempfile
import unittest
from pathlib import Path

import services.scanner as scanner
from services.large_package import build_coverage
from services.scanner import scan_directory, scan_inventory_slice
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser


class V1LargeFileScannerTests(unittest.TestCase):
    def test_durable_inventory_resumes_in_slices_and_keeps_tree_lazy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "package"
            root.mkdir()
            for directory in ("a", "b"):
                child = root / directory
                child.mkdir()
                for index in range(4):
                    (child / "{}.txt".format(index)).write_text(
                        "{}-{}".format(directory, index), encoding="utf-8"
                    )
            db_path = Path(folder) / "state.db"
            storage = Storage(db_path)
            cursor = None
            slices = 0
            while True:
                result = scan_inventory_slice(root, cursor=cursor, slice_entries=3)
                slices += 1
                scan = storage.save_inventory_slice(
                    "scan", root, result["cursor"], result["records"],
                    owner_id="owner", complete=result["complete"],
                )
                if result["complete"]:
                    break
                # Simulate a new Worker process loading only the durable cursor.
                storage = Storage(db_path)
                cursor = storage.get_inventory_cursor("scan")
                cursor.pop("status", None)

            self.assertGreater(slices, 1)
            self.assertTrue(scan["inventory_complete"])
            self.assertFalse(scan["truncated"])
            self.assertEqual(
                len(list(storage.iter_inventory_entries("scan", kind="file"))), 8
            )
            self.assertEqual(scan["tree"]["children"], [])
            root_page = storage.get_tree_page("scan", "physical", limit=10)
            self.assertEqual(root_page["child_count"], 2)
            self.assertEqual(len(root_page["children"]), 2)
            full_tree = storage.build_inventory_tree("scan")
            self.assertEqual(len(full_tree["children"]), 2)

    def test_operator_can_raise_file_boundary_above_legacy_fifty_thousand(self):
        with tempfile.TemporaryDirectory() as folder:
            result = scan_directory(Path(folder), max_files=100_001)
            self.assertEqual(result["max_files"], 100_001)

    def test_total_node_limit_is_observable_and_hard(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(10):
                (root / "{:02d}.txt".format(index)).write_text("x", encoding="utf-8")
            result = scan_directory(root, max_nodes=4)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["scanned_node_count"], 4)
            self.assertGreaterEqual(result["node_limited_count"], 1)
            self.assertEqual(result["max_nodes"], 4)

    def test_directory_limit_does_not_recurse_beyond_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(5):
                child = root / "d{}".format(index)
                child.mkdir()
                (child / "item.txt").write_text("x", encoding="utf-8")
            result = scan_directory(root, max_directories=2, max_nodes=100)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["scanned_directory_count"], 2)
            self.assertGreaterEqual(result["directory_limited_count"], 1)
            self.assertEqual(result["max_directories"], 2)

    def test_single_directory_materialisation_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(8):
                (root / "{:02d}.txt".format(index)).write_text("x", encoding="utf-8")
            result = scan_directory(
                root,
                max_nodes=100,
                max_entries_per_directory=3,
            )
            self.assertTrue(result["truncated"])
            self.assertEqual(result["file_count"], 3)
            self.assertEqual(result["entry_limited_directory_count"], 1)
            self.assertEqual(result["max_entries_per_directory"], 3)

    def test_depth_limit_still_coexists_with_new_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root
            for name in ("one", "two", "three"):
                current = current / name
                current.mkdir()
            (current / "deep.txt").write_text("deep", encoding="utf-8")
            result = scan_directory(root, max_depth=2, max_directories=20, max_nodes=40)
            self.assertGreaterEqual(result["depth_limited_directory_count"], 1)
            self.assertEqual(result["max_depth"], 2)

    def test_hidden_sensitive_and_unsupported_files_stay_in_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
            (root / ".env").write_text("SECRET=never-send", encoding="utf-8")
            (root / "opaque.custom").write_bytes(b"opaque")
            result = scan_directory(root, max_files=20, max_nodes=40)
            paths = {}
            stack = [result["tree"]]
            while stack:
                item = stack.pop()
                if item.get("path"):
                    paths[item["path"]] = item
                stack.extend(item.get("children") or [])
            self.assertEqual(result["file_count"], 3)
            self.assertIn(".git/HEAD", paths)
            self.assertIn("opaque.custom", paths)
            self.assertTrue(paths[".env"]["sensitive"])
            self.assertFalse(paths[".env"]["content_analysis_allowed"])
            self.assertEqual(
                result["ignore_policy"]["default_is_full_inventory"],
                not scanner.IGNORED_DIRS and not scanner.IGNORED_FILES,
            )

    def test_sensitive_file_parser_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("PASSWORD=top-secret", encoding="utf-8")
            document = UnifiedDocumentParser().parse(path, relative_path=".env")
            self.assertEqual(document["parser"]["name"], "metadata-only")
            self.assertEqual(document["text"], "")
            self.assertTrue(document["coverage"]["content_restricted"])
            self.assertTrue(document["source"]["sha256"])

    def test_explicit_exclusions_make_inventory_coverage_partial(self):
        original_dirs = set(scanner.IGNORED_DIRS)
        original_files = set(scanner.IGNORED_FILES)
        try:
            scanner.IGNORED_DIRS.clear()
            scanner.IGNORED_DIRS.add("excluded")
            scanner.IGNORED_FILES.clear()
            scanner.IGNORED_FILES.add("skip.bin")
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "excluded").mkdir()
                (root / "excluded" / "inside.txt").write_text("x", encoding="utf-8")
                (root / "skip.bin").write_bytes(b"x")
                (root / "keep.txt").write_text("keep", encoding="utf-8")
                scan = scan_directory(root, max_files=20, max_nodes=40)
                _coverage_for_paths, coverage = build_coverage(
                    scan, {}, pending_paths=["keep.txt"]
                )
                self.assertEqual(scan["ignored_directory_count"], 1)
                self.assertEqual(scan["ignored_file_count"], 1)
                self.assertFalse(coverage["inventory_coverage"]["complete"])
                self.assertIsNone(coverage["inventory_coverage_ratio"])
        finally:
            scanner.IGNORED_DIRS.clear()
            scanner.IGNORED_DIRS.update(original_dirs)
            scanner.IGNORED_FILES.clear()
            scanner.IGNORED_FILES.update(original_files)


if __name__ == "__main__":
    unittest.main()
