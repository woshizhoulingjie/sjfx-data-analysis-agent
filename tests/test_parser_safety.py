import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from services.structured_profile import _iter_json_incremental, profile_path
from services.unified_parser import UnifiedDocumentParser, _safe_archive_destination
from services.parse_isolation import IsolatedParserRunner


class ParserSafetyTests(unittest.TestCase):
    def test_gb18030_csv_records_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "数据.csv"
            path.write_bytes("姓名,金额\n张三,12\n李四,8\n".encode("gb18030"))
            profile = profile_path(path, max_rows=100, max_bytes=1024)
            self.assertEqual(profile["status"], "completed")
            self.assertEqual(profile["source"]["encoding"], "gb18030")
            self.assertEqual(profile["row_count"], 2)
            self.assertFalse(profile["limits"]["truncated"])

    def test_jsonl_row_limit_is_explicitly_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.jsonl"
            path.write_text('{"id":1}\n{"id":2}\n', encoding="utf-8")
            profile = profile_path(path, max_rows=1, max_bytes=1024)
            self.assertEqual(profile["status"], "partial")
            self.assertTrue(profile["limits"]["truncated"])
            self.assertIn("row_limit", profile["limits"]["truncation_reasons"])

    def test_jsonl_single_record_has_its_own_memory_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.jsonl"
            path.write_text('{"text":"' + ("x" * 200) + '"}\n{"id":2}\n', encoding="utf-8")
            with patch.dict("os.environ", {"MAX_STRUCTURED_JSON_RECORD_BYTES": "32"}):
                profile = profile_path(path, max_rows=10, max_bytes=4096)
            self.assertEqual(profile["status"], "partial")
            self.assertTrue(profile["limits"]["truncated"])
            self.assertIn("record_limit", profile["limits"]["truncation_reasons"])
            self.assertTrue(profile["limits"]["streaming"])

    def test_large_json_is_bounded_and_marked_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            path.write_text("[" + ",".join('{"id":%d}' % i for i in range(100)) + "]", encoding="utf-8")
            profile = profile_path(path, max_rows=1000, max_bytes=64)
            self.assertEqual(profile["status"], "partial")
            self.assertTrue(profile["limits"]["truncated"])
            self.assertIn("byte_limit", profile["limits"]["truncation_reasons"])

    def test_dependency_free_json_reader_is_incremental(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            path.write_text('[{"id":1},{"id":2}]', encoding="utf-8")
            rows, metadata = _iter_json_incremental(path, "utf-8", 1, 1024)
            self.assertEqual(rows, [{"id": 1}])
            self.assertTrue(metadata["streaming"])
            self.assertIn("row_limit", metadata["truncation_reasons"])
            self.assertEqual(metadata["parse_errors"], 0)

    def test_archive_resolution_rejects_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "extract"
            root.mkdir()
            self.assertIsNone(_safe_archive_destination(root, "../outside.txt"))
            link = root / "link"
            try:
                link.symlink_to(Path(folder), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links unavailable on this platform")
            self.assertIsNone(_safe_archive_destination(root, "link/secret.txt"))

    def test_archive_parser_skips_traversal_member(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "input.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape.txt", "must not be written")
                output.writestr("safe.txt", "safe content")
            parser = UnifiedDocumentParser(max_chars=1000)
            document = parser.parse(archive, "input.zip", mode="fast")
            self.assertTrue(any("越界" in warning for warning in document["warnings"]))
            self.assertNotIn("escape.txt", [p.name for p in root.rglob("escape.txt")])

    def test_docling_defaults_to_cpu(self):
        parser = UnifiedDocumentParser()
        self.assertEqual(parser.docling_device, "cpu")

    def test_process_isolated_parser_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "brief.txt"
            path.write_text("isolated parser round trip", encoding="utf-8")
            runner = IsolatedParserRunner(UnifiedDocumentParser(max_chars=1000))
            try:
                document = runner.parse(path, "brief.txt", mode="fast", timeout=15, memory_mb=1024)
            finally:
                runner.close()
            self.assertEqual(document["text"], "isolated parser round trip")


if __name__ == "__main__":
    unittest.main()
