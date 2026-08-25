import tempfile
import unittest
import zipfile
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

from services.package_exploration import (
    PreviewBudget,
    build_content_map,
    detect_language,
    preview_as_document,
    preview_file,
    promotion_paths,
)
from services.scanner import scan_directory
from services.storage import Storage
from services.package_analysis import analyze_package


def _files(scan):
    stack = [scan["tree"]]
    result = []
    while stack:
        node = stack.pop()
        if node.get("kind") == "file":
            result.append(node)
        stack.extend(node.get("children") or [])
    return result


class PackageExplorationTests(unittest.TestCase):
    def test_windowed_preview_reads_head_middle_and_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = "HEAD " + ("middle-data " * 2000) + " TAIL"
            (root / "large.txt").write_text(payload, encoding="utf-8")
            scan = scan_directory(root)
            preview = preview_file(root, _files(scan)[0], per_file_bytes=1200)
            self.assertEqual(preview["status"], "previewed")
            self.assertLessEqual(preview["sampled_bytes"], 1200)
            self.assertIn("HEAD", preview["preview_text"])
            self.assertIn("TAIL", preview["preview_text"])
            self.assertTrue(preview["coverage"]["preview_only"])

    def test_preview_extracts_bounded_people_organizations_and_dates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "letter.txt").write_text(
                "From: Alice Johnson\n发件人：张伟\nAcme Research Institute 研究院\n"
                "Decision date: 2024-03-12.",
                encoding="utf-8",
            )
            preview = preview_file(root, _files(scan_directory(root))[0], per_file_bytes=4096)

            self.assertIn("Alice Johnson", preview["entities"]["people"])
            self.assertIn("张伟", preview["entities"]["people"])
            self.assertTrue(preview["entities"]["organizations"])
            self.assertEqual(preview["dates"], ["2024-03-12"])

    def test_global_budget_defers_remaining_files_without_reading_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.txt").write_text("A" * 5000, encoding="utf-8")
            (root / "b.txt").write_text("B" * 5000, encoding="utf-8")
            scan = scan_directory(root)
            nodes = sorted(_files(scan), key=lambda item: item["path"])
            budget = PreviewBudget(1000)
            first = preview_file(root, nodes[0], per_file_bytes=1000, budget=budget)
            second = preview_file(root, nodes[1], per_file_bytes=1000, budget=budget)
            self.assertEqual(first["sampled_bytes"], 1000)
            self.assertEqual(second["status"], "deferred")
            self.assertEqual(second["sampled_bytes"], 0)

    def test_small_file_is_not_duplicated_by_overlapping_sample_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = "From: Alice Johnson\nDecision date: 2024-03-12."
            (root / "small.txt").write_text(source, encoding="utf-8")

            preview = preview_file(root, _files(scan_directory(root))[0], per_file_bytes=4096)

            self.assertEqual(preview["preview_text"].splitlines(), source.splitlines())
            self.assertEqual(preview["sampled_ranges"], [[0, (root / "small.txt").stat().st_size]])

    def test_zip_preview_lists_members_without_extracting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with zipfile.ZipFile(str(root / "mail.zip"), "w") as archive:
                archive.writestr("letters/one.txt", "Hello from London")
                archive.writestr("letters/two.txt", "Bonjour de Paris")
            scan = scan_directory(root)
            preview = preview_file(root, _files(scan)[0], per_file_bytes=4096)
            self.assertIn("letters/one.txt", preview["archive_members"])
            self.assertIn("Hello", preview["preview_text"])

    def test_duplicate_sample_hash_is_content_based_not_path_or_mtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            content = "identical foreign evidence " * 300
            (root / "copy-a.txt").write_text(content, encoding="utf-8")
            (root / "copy-b.txt").write_text(content, encoding="utf-8")
            os.utime(str(root / "copy-b.txt"), (1700000000, 1700000000))
            scan = scan_directory(root)
            previews = [preview_file(root, node, per_file_bytes=1200) for node in _files(scan)]
            self.assertEqual(previews[0]["content_sample_sha256"], previews[1]["content_sample_sha256"])
            self.assertNotEqual(previews[0]["preview_fingerprint"], previews[1]["preview_fingerprint"])
            content_map = build_content_map(previews, representative_limit=1)
            self.assertEqual(content_map["duplicates"][0]["file_count"], 2)

    def test_content_map_is_diverse_bounded_and_promotable(self):
        previews = []
        for index in range(12):
            previews.append({
                "path": "group{}/doc{}.txt".format(index % 3, index),
                "extension": ".txt" if index < 8 else ".pdf",
                "size": 100 + index,
                "status": "previewed",
                "document_type": "信件" if index % 2 else "报告",
                "language": {"code": "en" if index < 4 else "zh"},
                "keywords": ["topic{}".format(index % 4), "shared"],
                "entities": {"organizations": ["机构{}".format(index % 3)]},
                "dates": ["202{}-01-02".format(index % 4)],
                "sample_sha256": "same" if index in {0, 1} else "hash{}".format(index),
                "preview_text": "evidence " * (index + 1),
            })
        content_map = build_content_map(previews, representative_limit=5, relation_limit=8)
        self.assertEqual(content_map["inventory"]["file_count"], 12)
        self.assertEqual(len(content_map["representative_paths"]), 5)
        self.assertLessEqual(len(content_map["relationships"]), 8)
        self.assertEqual(content_map["duplicates"][0]["file_count"], 2)
        self.assertEqual(content_map["entities"]["organizations"][0]["file_count"], 4)
        self.assertEqual(
            {item["year"]: item["file_count"] for item in content_map["years"]},
            {"2020": 3, "2021": 3, "2022": 3, "2023": 3},
        )
        promoted = promotion_paths(content_map, ["asked.txt"], limit=3)
        self.assertEqual(promoted[0], "asked.txt")
        self.assertEqual(len(promoted), 3)

    def test_content_map_entity_and_year_counts_are_file_memberships(self):
        content_map = build_content_map([{
            "path": "a.txt", "status": "previewed", "extension": ".txt",
            "document_type": "文本", "language": {"code": "en"},
            "entities": {
                "people": ["Alice", "Alice"],
                "organizations": ["Acme", "Acme"],
            },
            "dates": ["2024-01-01", "2024-12-31"],
            "sample_sha256": "a", "preview_characters": 10,
        }])

        self.assertEqual(content_map["entities"]["people"][0]["file_count"], 1)
        self.assertEqual(content_map["entities"]["organizations"][0]["file_count"], 1)
        self.assertEqual(content_map["years"], [{"year": "2024", "file_count": 1}])

    def test_zip_preview_preflights_and_never_calls_infolist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive_path = root / "package.zip"
            with zipfile.ZipFile(str(archive_path), "w") as archive:
                archive.writestr("notes/readme.txt", "bounded package preview")
            scan = scan_directory(root)
            with patch.object(
                zipfile.ZipFile, "infolist",
                side_effect=AssertionError("preview must not duplicate the central directory"),
            ):
                preview = preview_file(root, _files(scan)[0], per_file_bytes=4096)
            self.assertEqual(preview["status"], "previewed")
            self.assertIn("notes/readme.txt", preview["archive_members"])

    def test_restricted_or_deferred_files_are_never_auto_representatives(self):
        previews = [
            {
                "path": "credentials.env", "extension": ".env", "size": 100,
                "status": "restricted", "document_type": "其他文件",
                "language": {"code": "unknown"}, "keywords": ["secret"],
                "entities": {}, "sample_sha256": "restricted",
            },
            {
                "path": "deferred.txt", "extension": ".txt", "size": 100,
                "status": "deferred", "document_type": "文本",
                "language": {"code": "en"}, "keywords": ["deferred"],
                "entities": {}, "sample_sha256": "deferred",
            },
            {
                "path": "safe.txt", "extension": ".txt", "size": 100,
                "status": "previewed", "document_type": "文本",
                "language": {"code": "en"}, "keywords": ["safe"],
                "entities": {}, "sample_sha256": "safe",
            },
        ]
        content_map = build_content_map(previews, representative_limit=10)
        self.assertEqual(content_map["representative_paths"], ["safe.txt"])

    def test_preview_projection_never_claims_full_parse(self):
        preview = {
            "path": "letter.eml", "name": "letter.eml", "extension": ".eml",
            "size": 20, "sample_sha256": "a" * 64, "preview_text": "Hello",
            "previewed_at": "2026-01-01T00:00:00+00:00", "warnings": [],
            "coverage": {"preview_only": True, "parse_complete": False},
            "language": detect_language("This is a sufficiently long English letter."),
            "keywords": ["letter"], "entities": {}, "document_type": "信件",
        }
        document = preview_as_document(preview)
        self.assertTrue(document["coverage"]["preview_only"])
        self.assertFalse(document["coverage"]["parse_complete"])
        self.assertEqual(document["parser"]["mode"], "preview")

    def test_language_detection_distinguishes_foreign_text(self):
        self.assertEqual(detect_language("This document contains an English project report.")["code"], "en")
        self.assertEqual(detect_language("这是一份完整的中文项目报告，包含多个章节。 ")["code"], "zh")

    def test_preview_and_content_map_are_recoverable_from_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            preview = {
                "path": "a.txt", "status": "previewed", "sample_sha256": "abc",
                "language": {"code": "en"}, "document_type": "文本",
                "preview_text": "Hello", "coverage": {"preview_only": True},
            }
            storage.save_file_previews("scan-1", [("a.txt", preview)])
            self.assertEqual(storage.file_preview_counts("scan-1"), {"previewed": 1})
            self.assertEqual(storage.get_file_preview("scan-1", "a.txt")["preview_text"], "Hello")
            content_map = {"schema_version": "package-content-map/1.0", "representative_paths": ["a.txt"]}
            storage.save_content_map("scan-1", content_map)
            self.assertEqual(storage.get_content_map("scan-1"), content_map)

    def test_old_preview_contract_is_rebuilt_before_content_map_generation(self):
        class CountingParser:
            docling_device = "cpu"

            def status(self):
                return {"available": True}

            def parse(self, path, relative_path=None, mode="accurate"):
                source = Path(path)
                return {
                    "source": {"path": relative_path, "name": source.name,
                               "size": source.stat().st_size,
                               "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                    "parser": {"name": "counting", "mode": mode},
                    "structure": {"title": source.stem, "headings": []},
                    "text": source.read_text(encoding="utf-8"),
                    "coverage": {"complete": True, "parse_complete": True},
                    "evidence": [],
                }

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.txt").write_text("From: Alice Johnson on 2024-03-12", encoding="utf-8")
            scan = scan_directory(root)
            storage = Storage(root / "state.db")
            scan_id = storage.save_scan(scan)
            node = _files(scan)[0]
            old = {
                "schema_version": "file-preview/1.0", "path": "a.txt", "name": "a.txt",
                "extension": ".txt", "size": node["size"],
                "modified_at_ns": node["modified_at_ns"], "status": "previewed",
                "sample_sha256": "old", "preview_fingerprint": "old",
                "language": {"code": "en"}, "document_type": "文本", "preview_text": "old",
                "coverage": {"preview_only": True},
            }
            storage.save_file_preview(scan_id, "a.txt", old)
            storage.save_content_map(scan_id, {"schema_version": "package-content-map/1.0"})

            analysis = analyze_package(
                scan_id, scan, storage, CountingParser(),
                large_options={"threshold_bytes": 1, "initial_parse_files": 1},
            )

            refreshed = storage.get_file_preview(scan_id, "a.txt")
            self.assertEqual(refreshed["schema_version"], "file-preview/1.1")
            self.assertIn("Alice Johnson", analysis["content_map"]["entities"]["people"][0]["name"])

    def test_large_package_only_deep_parses_selected_representatives(self):
        class CountingParser:
            docling_device = "cpu"

            def __init__(self):
                self.paths = []

            def parse(self, path, relative_path=None, mode="accurate"):
                path = Path(path)
                self.paths.append(str(relative_path))
                text = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                return {
                    "schema_version": "unified-document/1.0",
                    "source": {"path": str(relative_path), "name": path.name, "extension": ".txt", "size": path.stat().st_size, "sha256": digest},
                    "parser": {"name": "counting", "mode": mode},
                    "structure": {"title": path.stem, "headings": [], "page_count": None, "table_count": 0, "picture_count": 0},
                    "text": text,
                    "coverage": {"complete": True, "parse_complete": True, "semantic_complete": True},
                    "evidence": [{"evidence_id": "E-" + path.stem, "source_path": str(relative_path), "label": "paragraph", "text": text[:100]}],
                    "warnings": [],
                }

            def status(self):
                return {"available": True}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(6):
                (root / "doc{}.txt".format(index)).write_text(
                    "Topic {} evidence and organisation {}".format(index % 3, index),
                    encoding="utf-8",
                )
            scan = scan_directory(root)
            storage = Storage(root / "state.db")
            scan_id = storage.save_scan(scan)
            parser = CountingParser()
            analysis = analyze_package(
                scan_id, scan, storage, parser,
                large_options={"threshold_bytes": 1, "initial_parse_files": 2, "preview_total_bytes": 1024 * 1024},
            )
            self.assertEqual(len(parser.paths), 2)
            self.assertEqual(sum(storage.file_preview_counts(scan_id).values()), 6)
            state_counts = storage.file_state_counts(scan_id)
            self.assertEqual(state_counts.get("completed"), 2)
            self.assertEqual(state_counts.get("previewed"), 4)
            self.assertEqual(analysis["statistics"]["deep_analysis_pending_files"], 4)
            self.assertEqual(len(analysis["content_map"]["representative_paths"]), 2)


if __name__ == "__main__":
    unittest.main()
