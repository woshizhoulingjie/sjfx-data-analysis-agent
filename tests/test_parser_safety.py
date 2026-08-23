import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

from services.structured_profile import _iter_json_incremental, profile_path
from config import TEN_GIB_BYTES, content_byte_limit
from services.unified_parser import (
    UnifiedDocumentParser,
    _safe_archive_destination,
    cleanup_stale_parse_temp_dirs,
)
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

    def test_xlsx_profiles_all_worksheets_with_one_global_row_budget(self):
        try:
            from openpyxl import Workbook
        except ImportError:
            self.skipTest("openpyxl unavailable")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "multi-sheet.xlsx"
            book = Workbook()
            first = book.active
            first.title = "一月"
            first.append(["地区", "销售额"])
            first.append(["华北", 10])
            second = book.create_sheet("二月")
            second.append(["地区", "销售额"])
            second.append(["华东", 20])
            book.save(path)

            complete = profile_path(path, max_rows=10, max_bytes=1024 * 1024)
            partial = profile_path(path, max_rows=1, max_bytes=1024 * 1024)

        self.assertEqual(complete["row_count"], 2)
        self.assertEqual(complete["columns"]["销售额"]["sum"], 30)
        self.assertTrue(complete["coverage"]["complete"])
        self.assertEqual(complete["limits"]["processed_worksheet_count"], 2)
        self.assertEqual(complete["limits"]["worksheet_row_counts"], {"一月": 1, "二月": 1})
        self.assertEqual(partial["row_count"], 1)
        self.assertFalse(partial["coverage"]["complete"])
        self.assertIn("row_limit", partial["limits"]["truncation_reasons"])
        self.assertEqual(partial["limits"]["global_row_budget"], 1)

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

    def test_content_limits_share_one_hard_ten_gib_ceiling(self):
        oversized = str(20 * 1024 * 1024 * 1024)
        with patch.dict("os.environ", {
            "MAX_CONTENT_BYTES": oversized,
            "MAX_SINGLE_FILE_BYTES": oversized,
            "MAX_ARCHIVE_MEMBER_BYTES": oversized,
        }):
            self.assertEqual(content_byte_limit(), TEN_GIB_BYTES)
            self.assertEqual(content_byte_limit("MAX_SINGLE_FILE_BYTES"), TEN_GIB_BYTES)
            self.assertEqual(content_byte_limit("MAX_ARCHIVE_MEMBER_BYTES"), TEN_GIB_BYTES)

    def test_archive_uses_configured_temp_root_and_removes_working_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scratch = root / "dedicated-scratch"
            archive = root / "input.zip"
            with zipfile.ZipFile(str(archive), "w") as output:
                output.writestr("safe.txt", "safe content")
            with patch.dict("os.environ", {"SJFX_PARSE_TEMP_DIR": str(scratch)}):
                parser = UnifiedDocumentParser(max_chars=1000)
                document = parser.parse(archive, "input.zip", mode="fast")
            self.assertIn("safe content", document["text"])
            self.assertEqual(list(scratch.iterdir()), [])

    def test_dead_parser_temp_is_reaped_but_live_owner_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            scratch = Path(folder)
            dead = scratch / "sjfx-archive-p99999999-dead"
            live = scratch / "sjfx-archive-p{}-live".format(os.getpid())
            unrelated = scratch / "do-not-touch"
            dead.mkdir()
            live.mkdir()
            unrelated.mkdir()
            removed = cleanup_stale_parse_temp_dirs(scratch, stale_seconds=999999)
            self.assertEqual(removed, 1)
            self.assertFalse(dead.exists())
            self.assertTrue(live.exists())
            self.assertTrue(unrelated.exists())

    def test_encrypted_zip_member_is_a_structured_partial_result(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "encrypted.zip"
            with zipfile.ZipFile(str(archive), "w") as output:
                output.writestr("secret.txt", "secret")
            original_open = zipfile.ZipFile.open

            def password_required(instance, name, *args, **kwargs):
                if getattr(name, "filename", name) == "secret.txt":
                    raise RuntimeError("File is encrypted, password required")
                return original_open(instance, name, *args, **kwargs)

            with patch.object(zipfile.ZipFile, "open", password_required):
                document = UnifiedDocumentParser(max_chars=1000).parse(
                    archive, "encrypted.zip", mode="fast"
                )
            manifest = document["archive_manifest"]
            self.assertEqual(manifest["encrypted_members"], 1)
            self.assertEqual(manifest["skipped_members"], 1)
            self.assertEqual(manifest["skip_reasons"]["encrypted_member"], 1)
            self.assertEqual(manifest["coverage_status"], "partial")

    def test_zip_inventory_does_not_call_infolist_and_compression_ratio_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "dense.zip"
            with zipfile.ZipFile(str(archive), "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr("dense.txt", "x" * 10000)
            with patch.object(zipfile.ZipFile, "infolist", side_effect=AssertionError("unbounded copy")):
                with patch("services.unified_parser.ARCHIVE_MAX_COMPRESSION_RATIO", 2):
                    document = UnifiedDocumentParser(max_chars=1000).parse(
                        archive, "dense.zip", mode="fast"
                    )
            manifest = document["archive_manifest"]
            self.assertEqual(manifest["skip_reasons"]["compression_ratio_limit"], 1)
            self.assertEqual(manifest["coverage_status"], "partial")

    def test_zip_central_directory_is_rejected_before_zipfile_materialises_it(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "many.zip"
            with zipfile.ZipFile(str(archive), "w") as output:
                for index in range(3):
                    output.writestr("{}.txt".format(index), "x")
            with patch("services.unified_parser.ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES", 2):
                with patch("services.unified_parser.zipfile.ZipFile", side_effect=AssertionError("must preflight")):
                    document = UnifiedDocumentParser(max_chars=1000).parse(
                        archive, "many.zip", mode="fast"
                    )
            manifest = document["archive_manifest"]
            self.assertEqual(manifest["total_members"], 3)
            self.assertEqual(manifest["skipped_members"], 3)
            self.assertEqual(manifest["skip_reasons"]["central_directory_entry_limit"], 3)

    def test_forged_eocd_count_is_rejected_before_zipfile_materialises_it(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "forged-count.zip"
            with zipfile.ZipFile(str(archive), "w") as output:
                for index in range(3):
                    output.writestr("{}.txt".format(index), "x")
            raw = bytearray(archive.read_bytes())
            eocd = raw.rfind(b"PK\x05\x06")
            self.assertGreaterEqual(eocd, 0)
            raw[eocd + 8:eocd + 10] = (1).to_bytes(2, "little")
            raw[eocd + 10:eocd + 12] = (1).to_bytes(2, "little")
            archive.write_bytes(raw)

            with patch(
                "services.unified_parser.zipfile.ZipFile",
                side_effect=AssertionError("forged count must fail before ZipFile"),
            ):
                document = UnifiedDocumentParser(max_chars=1000).parse(
                    archive, "forged-count.zip", mode="fast"
                )
            manifest = document["archive_manifest"]
            self.assertEqual(manifest["declared_total_members"], 1)
            self.assertEqual(manifest["observed_central_directory_members"], 3)
            self.assertEqual(manifest["skip_reasons"]["central_directory_count_mismatch"], 3)

    def test_office_media_ocr_streams_without_zipfile_read(self):
        with tempfile.TemporaryDirectory() as folder:
            document_path = Path(folder) / "images.docx"
            with zipfile.ZipFile(str(document_path), "w") as output:
                output.writestr("word/media/picture.png", b"small-image")
            parser = UnifiedDocumentParser(max_chars=1000)
            base = {
                "source": {"path": "images.docx", "sha256": "a" * 64},
                "text": "", "warnings": [], "evidence": [],
                "coverage": {"embedded_ocr_characters": 0, "truncated_by_limit": False},
                "structure": {"picture_count": 0}, "parser": {},
            }
            result = SimpleNamespace(txts=["bounded OCR"], scores=[0.9], boxes=[None])
            with patch.object(parser, "_get_ocr_engine", return_value=lambda _blob: result), \
                    patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("unbounded read")):
                parser._rapidocr_office_images(document_path, base)
            self.assertIn("bounded OCR", base["text"])
            self.assertEqual(base["evidence"][0]["text"], "bounded OCR")

    def test_office_media_compression_bomb_is_skipped_before_read(self):
        with tempfile.TemporaryDirectory() as folder:
            document_path = Path(folder) / "bomb.docx"
            with zipfile.ZipFile(str(document_path), "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr("word/media/picture.png", b"x" * 10000)
            parser = UnifiedDocumentParser(max_chars=1000)
            base = {
                "source": {"path": "bomb.docx", "sha256": "b" * 64},
                "text": "", "warnings": [], "evidence": [],
                "coverage": {"embedded_ocr_characters": 0, "truncated_by_limit": False},
                "structure": {"picture_count": 0}, "parser": {},
            }
            ocr = Mock()
            with patch("services.unified_parser.ARCHIVE_MAX_COMPRESSION_RATIO", 2), \
                    patch.object(parser, "_get_ocr_engine", return_value=ocr):
                parser._rapidocr_office_images(document_path, base)
            self.assertTrue(any("压缩比" in warning for warning in base["warnings"]))
            ocr.assert_not_called()

    def test_unsafe_office_container_is_rejected_before_document_parser(self):
        with tempfile.TemporaryDirectory() as folder:
            document_path = Path(folder) / "bomb.docx"
            with zipfile.ZipFile(str(document_path), "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr("word/document.xml", b"x" * 10000)
            parser = UnifiedDocumentParser(max_chars=1000)
            with patch("services.unified_parser.ARCHIVE_MAX_COMPRESSION_RATIO", 2), \
                    patch.object(parser, "_fast_parse", side_effect=AssertionError("parser must not run")):
                document = parser.parse(document_path, "bomb.docx", mode="fast")
            self.assertEqual(document["parser"]["name"], "metadata-only")
            self.assertEqual(
                document["coverage"]["restriction_reason"],
                "unsafe_office_archive",
            )
            self.assertFalse(document["text"])

    def test_archive_member_path_depth_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "deep.zip"
            with zipfile.ZipFile(str(archive), "w") as output:
                output.writestr("one/two/deep.txt", "deep")
            with patch("services.unified_parser.ARCHIVE_MAX_MEMBER_PATH_DEPTH", 1):
                document = UnifiedDocumentParser(max_chars=1000).parse(
                    archive, "deep.zip", mode="fast"
                )
            self.assertEqual(
                document["archive_manifest"]["skip_reasons"]["member_path_depth_limit"],
                1,
            )

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
