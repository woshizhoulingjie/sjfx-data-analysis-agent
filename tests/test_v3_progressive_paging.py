import tempfile
import unittest
from pathlib import Path

from services.storage import Storage


class ProgressivePagingTests(unittest.TestCase):
    def make_storage(self, root):
        return Storage(Path(root) / "state.db", Path(root) / "documents")

    def test_scan_and_analysis_trees_are_bounded_and_pageable(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self.make_storage(tmp)
            scan = {
                "root": tmp,
                "file_count": 3,
                "directory_count": 0,
                "total_size": 6,
                "tree": {
                    "kind": "directory", "name": "root", "path": ".", "file_count": 3,
                    "children": [
                        {"kind": "file", "name": "{}.txt".format(index),
                         "path": "{}.txt".format(index), "size": index + 1}
                        for index in range(3)
                    ],
                },
            }
            storage.save_scan(scan, scan_id="scan-1", owner_id="owner")

            overview = storage.get_scan_overview("scan-1", owner_id="owner")
            self.assertNotIn("tree", overview)
            self.assertTrue(overview["tree_available"])
            self.assertIsNone(storage.get_scan_overview("scan-1", owner_id="other"))

            first = storage.get_tree_page("scan-1", "physical", limit=2)
            self.assertEqual(len(first["children"]), 2)
            self.assertEqual(first["_children_total"], 3)
            self.assertEqual(first["_children_next_offset"], 2)
            second = storage.get_tree_page(
                "scan-1", "physical", node_key=first["_tree_key"], offset=2, limit=2
            )
            self.assertEqual([item["path"] for item in second["children"]], ["2.txt"])
            self.assertIsNone(second["_children_next_offset"])

            scan["tree"]["children"][0]["simple_summary"] = "分析后摘要"
            storage.update_scan("scan-1", scan)
            refreshed = storage.get_tree_page("scan-1", "physical", limit=1)
            self.assertEqual(refreshed["children"][0]["simple_summary"], "分析后摘要")

            member_paths = ["{}.txt".format(index) for index in range(50)]
            analysis = {
                "schema_version": "package-analysis/test",
                "scan_id": "scan-1",
                "status": "completed",
                "statistics": {"parsed_files": 3},
                "coverage": {
                    "inventory_files": 3, "parsed_files": 3,
                    "archive_containers": [
                        {
                            "container_path": "archive-{}.zip".format(index),
                            "total_members": 50,
                            "parsed_members": 50,
                            "member_records": [
                                {"member": "{}.txt".format(member), "status": "parsed"}
                                for member in range(30)
                            ],
                        }
                        for index in range(25)
                    ],
                },
                "analysis_tree": {
                    "kind": "analysis_root", "name": "智能目录", "children": [{
                        "kind": "group", "name": "主题", "node_id": "topic-1",
                        "file_count": 50, "member_paths": member_paths, "children": [],
                    }],
                },
                "document_index": [{"path": value} for value in member_paths],
            }
            storage.save_analysis("scan-1", analysis)
            self.assertTrue(storage.tree_index_exists("scan-1", "analysis"))
            self.assertFalse(storage.tree_index_exists("scan-1", "analysis:confirmed"))
            storage.refresh_tree_index(
                "scan-1", "analysis:confirmed",
                {"kind": "analysis_root", "name": "已确认", "children": []},
            )
            self.assertTrue(storage.tree_index_exists("scan-1", "analysis:confirmed"))
            compact = storage.get_analysis_overview("scan-1")
            self.assertNotIn("analysis_tree", compact)
            self.assertNotIn("document_index", compact)
            self.assertNotIn("archive_containers", compact["coverage"])
            self.assertEqual(compact["coverage"]["archive_containers_total"], 25)
            self.assertEqual(len(compact["coverage"]["archive_containers_preview"]), 20)
            self.assertEqual(
                len(compact["coverage"]["archive_containers_preview"][0]["member_records_preview"]),
                10,
            )
            root = storage.get_tree_page("scan-1", "analysis")
            topic = root["children"][0]
            self.assertEqual(topic["member_count"], 50)
            self.assertEqual(len(topic["member_paths"]), 20)
            self.assertTrue(topic["member_paths_truncated"])

    def test_progress_and_summary_pages_are_independent_of_final_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self.make_storage(tmp)
            storage.save_analysis_progress("scan-1", {
                "status": "running", "stage": "parsing", "progress": 35,
                "coverage": {"inventory_files": 10, "parsed_files": 3},
            })
            self.assertEqual(storage.get_analysis_progress("scan-1")["progress"], 35)
            storage.update_analysis_progress_status("scan-1", "queued", "等待续批", "checkpoint_requeued")
            resumed = storage.get_analysis_progress("scan-1")
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["progress"], 35)
            storage.set_file_state(
                "scan-1", "a.txt", "fingerprint", "completed",
                document={"text": "正文内容", "evidence": [{"evidence_id": "E1"}]},
            )
            metrics = storage.file_state_metrics("scan-1")
            self.assertEqual(metrics["stored_characters"], 4)
            self.assertEqual(metrics["evidence_items"], 1)
            for index in range(3):
                storage.save_summary("scan-1", "path-{}".format(index), "file", {
                    "schema_version": 4, "summary_type": "file", "title": str(index),
                    "summary": "summary", "topics": [], "evidence_chain": [],
                })
            page = storage.list_summaries_page("scan-1", offset=0, limit=2)
            self.assertEqual(page["total"], 3)
            self.assertEqual(page["next_offset"], 2)
            exact = storage.list_summaries_page(
                "scan-1", node_path="path-2", summary_type="file", limit=10
            )
            self.assertEqual(len(exact["items"]), 1)
            storage.clear_analysis_progress("scan-1")
            self.assertIsNone(storage.get_analysis_progress("scan-1"))

    def test_stable_owner_migrates_legacy_and_token_derived_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self.make_storage(tmp)
            scan = {
                "root": tmp, "file_count": 0, "directory_count": 0,
                "tree": {"kind": "directory", "name": "root", "path": ".", "children": []},
            }
            storage.save_scan(scan, scan_id="legacy-scan", owner_id="legacy")
            storage.save_scan(scan, scan_id="token-scan", owner_id="old-token-hash")
            storage.migrate_legacy_ownership(
                "primary", aliases=["legacy", "default", "old-token-hash"]
            )
            self.assertTrue(storage.scan_owned("legacy-scan", "primary"))
            self.assertTrue(storage.scan_owned("token-scan", "primary"))
            self.assertFalse(storage.scan_owned("token-scan", "old-token-hash"))

    def test_tree_edit_history_has_a_bounded_latest_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self.make_storage(tmp)
            scan = {
                "root": tmp, "file_count": 0, "directory_count": 0,
                "tree": {"kind": "directory", "name": "root", "path": ".", "children": []},
            }
            storage.save_scan(scan, scan_id="scan-edits", owner_id="owner")
            for index in range(4):
                storage.save_tree_edit(
                    "scan-edits", "edit-{}".format(index), "rename",
                    {"node_id": "n", "name": str(index)}, owner_id="owner",
                )
            window = storage.list_tree_edits("scan-edits", "owner", limit=2)
            self.assertEqual(storage.tree_edit_count("scan-edits", "owner"), 4)
            self.assertEqual([item["edit_id"] for item in window], ["edit-2", "edit-3"])


if __name__ == "__main__":
    unittest.main()
