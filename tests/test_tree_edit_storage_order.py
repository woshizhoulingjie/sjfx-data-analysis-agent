import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from services.storage import Storage
from services.tree_editor import filter_tree


class TreeEditStorageOrderTests(unittest.TestCase):
    scan_id = "tree-order-scan"
    owner_id = "tree-order-owner"
    timestamp = "2026-10-15 00:00:00"
    # UUID lexical order deliberately opposes the order of user operations.
    edit_ids = [digit * 32 for digit in "fedcba"]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db_path = self.root / "state.db"
        self.storage = Storage(self.db_path)
        self.storage.save_scan({
            "root": str(self.root), "file_count": 2,
            "directory_count": 0, "total_size": 2,
            "tree": {"kind": "directory", "path": ".", "children": []},
        }, scan_id=self.scan_id, owner_id=self.owner_id)
        self.storage.save_analysis(self.scan_id, {
            "analysis_tree": {
                "kind": "analysis_root", "name": "Package", "children": [
                    {"kind": "file", "path": "b.txt", "name": "b.txt",
                     "classification_status": "classified", "size": 1},
                ],
            },
        })

    def save(self, index, operation, payload):
        self.storage.save_tree_edit(
            self.scan_id, self.edit_ids[index], operation, payload,
            owner_id=self.owner_id,
        )
        # Fix timestamps in this temporary fixture without relying on timing.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE tree_edits SET created_at=? WHERE scan_id=? AND edit_id=?",
                ("2026-10-14 23:59:59" if operation == "create" else self.timestamp,
                 self.scan_id, self.edit_ids[index]),
            )

    def manual_node(self):
        tree = self.storage.get_analysis(self.scan_id)["analysis_tree"]
        return next(node for node in tree["children"] if node.get("manual_created"))

    def snapshot(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT rowid,* FROM tree_edits WHERE scan_id=? ORDER BY rowid",
                (self.scan_id,),
            ).fetchall()

    def reopen(self):
        # Storage closes every SQLite connection at the end of each operation.
        self.storage = Storage(self.db_path)

    def test_same_second_undo_redo_and_review_survive_reopen(self):
        self.save(0, "create", {"name": "Original", "paths": ["a.txt"]})
        node_id = self.manual_node()["node_id"]
        self.save(1, "rename", {"node_id": node_id, "name": "Renamed"})
        self.assertEqual(self.manual_node()["name"], "Renamed")
        self.save(2, "undo", {"edit_id": self.edit_ids[1]})
        self.assertEqual(self.manual_node()["name"], "Original")
        self.reopen()
        self.assertEqual(self.manual_node()["name"], "Original")
        self.save(3, "redo", {"edit_id": self.edit_ids[1]})
        self.assertEqual(self.manual_node()["name"], "Renamed")
        self.save(4, "confirm", {"node_id": node_id, "confirmed": True})
        self.save(5, "mount", {"node_id": node_id, "path": "b.txt"})
        tree = self.storage.get_analysis(self.scan_id)["analysis_tree"]
        self.storage.refresh_tree_index(self.scan_id, "analysis", tree)
        persisted = self.snapshot()
        self.reopen()

        self.assertEqual(self.snapshot(), persisted)
        self.assertEqual(
            [edit["edit_id"] for edit in self.storage.list_tree_edits(self.scan_id)],
            self.edit_ids,
        )
        node = self.manual_node()
        self.assertEqual(node["name"], "Renamed")
        self.assertTrue(node["manual_confirmed"])
        self.assertEqual(node["classification_source"], "human")
        self.assertEqual(node["member_paths"], ["a.txt", "b.txt"])
        self.assertEqual(node["file_count"], 2)
        mounted = next(item for item in node["children"] if item["path"] == "b.txt")
        self.assertIn("Renamed", mounted["topic_memberships"])
        self.assertTrue(mounted["manual_membership"])
        confirmed = filter_tree(self.storage.get_analysis(self.scan_id)["analysis_tree"], "confirmed")
        self.assertEqual([item["node_id"] for item in confirmed["children"]], [node_id])
        page = self.storage.get_tree_page(self.scan_id, "analysis", "node:" + node_id)
        self.assertEqual(page["name"], "Renamed")
        self.assertTrue(page["manual_confirmed"])
        self.assertEqual([item["path"] for item in page["children"]], ["a.txt", "b.txt"])

    def test_existing_history_and_recent_window_keep_insertion_order(self):
        # Seed the existing schema directly, as a DB written by an older build.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for index, name in enumerate(["First", "Second", "Third"]):
                conn.execute(
                    "INSERT INTO tree_edits(scan_id,edit_id,owner_id,operation,payload,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (self.scan_id, self.edit_ids[index], self.owner_id, "rename",
                     json.dumps({"node_id": "existing", "name": name}), self.timestamp),
                )
        persisted = self.snapshot()
        self.reopen()
        self.assertEqual(self.snapshot(), persisted)
        full = self.storage.list_tree_edits(self.scan_id, self.owner_id)
        self.assertEqual([edit["edit_id"] for edit in full], self.edit_ids[:3])
        recent = self.storage.list_tree_edits(self.scan_id, self.owner_id, limit=2)
        self.assertEqual(recent, full[-2:])
        self.assertEqual(self.storage.tree_edit_count(self.scan_id, self.owner_id), 3)
        self.assertEqual(self.storage.list_tree_edits(self.scan_id, "other-owner"), [])
        self.assertEqual(self.storage.list_tree_edits(self.scan_id, "other-owner", limit=2), [])
        self.assertEqual(self.storage.list_tree_edits("missing-scan"), [])


if __name__ == "__main__":
    unittest.main()
