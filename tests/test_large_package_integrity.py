import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from services.logical_units import iter_logical_units
from services.package_analysis import _logical_source_snapshot, _preview_inventory_file
from services.package_exploration import PreviewBudget
from services.processing_queue import deep_processing_eligible
from services.retrieval import retrieve_evidence
from services.scanner import scan_inventory_slice
from services.storage import Storage
from services.unified_parser import UnifiedDocumentParser


class LargePackageIntegrityTests(unittest.TestCase):
    def test_unsupported_content_is_never_reported_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.mp4"
            path.write_bytes(b"not-a-real-video")
            document = UnifiedDocumentParser().parse(path)
            self.assertEqual(document["parser"]["name"], "metadata-only")
            self.assertFalse(document["coverage"]["complete"])
            self.assertEqual(document["coverage"]["coverage_ratio"], 0.0)
            self.assertTrue(document["coverage"]["content_unsupported"])

    def test_delayed_retry_remains_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "state.db"))
            storage.save_file_workflow_states("scan", [{
                "path": "waiting.pdf",
                "promotion_allowed": True,
                "safety_status": "checked",
                "light_index_status": "ready",
            }])
            storage.set_file_state(
                "scan", "waiting.pdf", "fingerprint", "failed",
                error="timeout", error_class="transient_runtime",
                retryable=True, next_retry_at=time.time() + 3600,
            )
            counts = storage.package_processing_counts("scan")
            self.assertEqual(counts["completed"], 0)
            self.assertEqual(counts["pending"], 0)
            self.assertEqual(counts["retry_waiting"], 1)
            self.assertEqual(counts["incomplete"], 1)

    def test_legacy_terminal_exclusion_never_blocks_completion(self):
        """Persisted pre-fix duplicate states must match scheduler semantics."""
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "state.db"))
            storage.save_file_workflow_states("scan", [{
                "path": "copies/letter-copy.txt",
                # Old releases wrote this value even though the reason is a
                # terminal exclusion.  Retain it to exercise compatibility.
                "promotion_allowed": True,
                "safety_status": "checked",
                "light_index_status": "ready",
                "reasons": ["exact_duplicate_non_primary"],
            }])
            counts = storage.package_processing_counts("scan")
            self.assertEqual(counts["completed"], 0)
            self.assertEqual(counts["excluded"], 1)
            self.assertEqual(counts["pending"], 0)
            self.assertEqual(counts["incomplete"], 0)

    def test_retrieval_labels_translation_and_preview_hits(self):
        indexed = [{
            "evidence_id": "translation-1",
            "source_path": "letter.txt",
            "label": "translation_unit",
            "index_kind": "translation",
            "text": "The invoice remains unpaid.",
            "translated_text": "发票仍未支付。",
        }, {
            "evidence_id": "preview-1",
            "source_path": "notes.txt",
            "label": "paragraph",
            "text": "The delivery remains overdue.",
            "preview_only": True,
        }]
        translated = retrieve_evidence({}, "未支付", indexed_chunks=indexed)
        preview = retrieve_evidence({}, "overdue", indexed_chunks=indexed)
        self.assertEqual(translated["results"][0]["match_type"], "translation")
        self.assertEqual(preview["results"][0]["match_type"], "preview_text")

    def test_needs_attention_does_not_reenter_queue_or_count_complete(self):
        workflow = {"promotion_allowed": True, "safety_status": "checked"}
        state = {"status": "needs_attention"}
        self.assertFalse(deep_processing_eligible(workflow, state))

    def test_zip_members_are_independent_logical_units(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive_path = root / "bundle.zip"
            with zipfile.ZipFile(str(archive_path), "w") as archive:
                archive.writestr("letters/one.txt", "first letter")
                archive.writestr("letters/two.txt", "second letter")
            stat = archive_path.stat()
            container = {
                "path": "bundle.zip", "name": "bundle.zip", "extension": ".zip",
                "size": stat.st_size, "modified_at_ns": stat.st_mtime_ns,
                "device": stat.st_dev, "inode": stat.st_ino,
            }
            units = list(iter_logical_units(root, [container]))
            self.assertEqual(len(units), 2)
            self.assertEqual({unit["member_name"] for unit in units}, {
                "letters/one.txt", "letters/two.txt",
            })
            with _logical_source_snapshot(root, units[0]) as snapshot:
                self.assertIn("letter", Path(snapshot).read_text(encoding="utf-8"))
            preview = _preview_inventory_file(
                root, units[1], per_file_bytes=4096,
                budget=PreviewBudget(4096), zip_member_limit=10,
                zip_member_bytes=1024,
            )
            self.assertEqual(preview["path"], units[1]["path"])
            self.assertEqual(preview["status"], "previewed")
            self.assertIn("second letter", preview["preview_text"])

    def test_large_csv_is_split_into_queueable_units(self):
        container = {
            "path": "large.csv", "name": "large.csv", "extension": ".csv",
            "size": 2500, "modified_at_ns": 1,
        }
        units = list(iter_logical_units(".", [container], partition_bytes=1000))
        self.assertEqual(len(units), 3)
        self.assertEqual(units[0]["byte_start"], 0)
        self.assertEqual(units[-1]["byte_end"], 2500)

    def test_directory_manifest_is_reused_across_slices(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as state:
            root = Path(folder)
            for index in range(8):
                (root / "f{:02d}.txt".format(index)).write_text(str(index), encoding="utf-8")
            first = scan_inventory_slice(root, slice_entries=3, manifest_dir=state)
            manifest = Path(first["cursor"]["stack"][0]["manifest_path"])
            created_at = manifest.stat().st_mtime_ns
            (root / "late.txt").write_text("late", encoding="utf-8")
            cursor = first["cursor"]
            paths = [row["path"] for row in first["records"]]
            while True:
                part = scan_inventory_slice(
                    root, cursor=cursor, slice_entries=3, manifest_dir=state
                )
                paths.extend(row["path"] for row in part["records"])
                cursor = part["cursor"]
                if part["complete"]:
                    break
                self.assertEqual(manifest.stat().st_mtime_ns, created_at)
            self.assertNotIn("late.txt", paths)

    def test_relation_recall_uses_complete_feature_table(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "state.db"))
            rows = [
                ("done.txt", "organization", "甲公司", 2.5),
                ("wanted.txt", "organization", "甲公司", 2.5),
                ("other.txt", "topic", "天气", 1.0),
            ]
            storage.rebuild_file_relation_features("scan", rows)
            recalled = storage.recall_file_relation_features(
                "scan", {"done.txt"}, {"wanted.txt", "other.txt"}
            )
            self.assertEqual(recalled[0]["path"], "wanted.txt")
            self.assertIn("organization:甲公司", recalled[0]["reasons"])

    def test_relationship_catalog_merges_producers_without_upgrading_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(str(Path(folder) / "state.db"))
            storage.save_content_map("scan", {"relationships": [{
                "source": "a.txt", "target": "b.txt", "weight": 5,
                "reasons": [{"kind": "topic", "value": "付款"}],
            }]})
            storage.save_analysis("scan", {"file_relationships": [{
                "source_path": "a.txt", "target_path": "b.txt",
                "relation_type": "related", "confidence": 0.82,
                "evidence_ids": ["ev-1"],
            }]})
            catalog = storage.get_relationship_catalog("scan")
            self.assertEqual(catalog["relationship_count"], 1)
            edge = catalog["items"][0]
            self.assertEqual(edge["confidence"], 0.82)
            self.assertIn("package_analysis", edge["source_kinds"])
            self.assertIn("preview_content_map", edge["source_kinds"])
            self.assertIn("ev-1", edge["evidence"])
            self.assertEqual(edge["calibration"], "derived")

    def test_query_terms_expand_common_research_synonyms(self):
        terms = Storage._retrieval_terms("找出没有按时付款的材料")
        self.assertIn("逾期", terms)
        self.assertIn("拖欠", terms)
        self.assertIn("未支付", terms)


if __name__ == "__main__":
    unittest.main()
