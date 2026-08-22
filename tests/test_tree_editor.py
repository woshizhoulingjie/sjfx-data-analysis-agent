import unittest

from services.tree_editor import apply_tree_edits, filter_tree


class TreeEditorTests(unittest.TestCase):
    def base(self):
        return {
            "analysis_tree": {
                "kind": "analysis_root", "name": "资料", "children": [
                    {"kind": "group", "node_id": "g1", "name": "主题一", "member_paths": ["a.txt"],
                     "children": [{"kind": "file", "path": "a.txt", "name": "a.txt", "classification_status": "classified", "classification_confidence": 0.42}]},
                    {"kind": "group", "node_id": "g2", "name": "主题二", "member_paths": ["b.txt"],
                     "children": [{"kind": "file", "path": "b.txt", "name": "b.txt", "classification_status": "unclassified"}]},
                ],
            }
        }

    def test_mount_and_merge_are_persistent_operations(self):
        result = apply_tree_edits(self.base(), [
            {"operation": "mount", "payload": {"node_id": "g1", "path": "b.txt"}},
            {"operation": "merge", "payload": {"node_ids": ["g1", "g2"], "name": "合并主题"}},
        ])
        node = result["analysis_tree"]["children"][0]
        self.assertEqual(node["name"], "合并主题")
        self.assertEqual(set(node["member_paths"]), {"a.txt", "b.txt"})

    def test_filter_keeps_only_review_files(self):
        tree = self.base()["analysis_tree"]
        low = filter_tree(tree, "low_confidence")
        self.assertEqual([item["path"] for item in low["children"][0]["children"]], ["a.txt"])
        pending = filter_tree(tree, "unclassified")
        self.assertEqual([item["path"] for item in pending["children"][0]["children"]], ["b.txt"])

    def test_rename_and_confirm(self):
        result = apply_tree_edits(self.base(), [
            {"operation": "rename", "payload": {"node_id": "g1", "name": "人工主题"}},
            {"operation": "confirm", "payload": {"node_id": "g1", "confirmed": True}},
        ])
        node = result["analysis_tree"]["children"][0]
        self.assertEqual(node["name"], "人工主题")
        self.assertTrue(node["manual_confirmed"])

    def test_undo_and_redo_replay_without_overwriting_history(self):
        result = apply_tree_edits(self.base(), [
            {"edit_id": "e1", "operation": "rename", "payload": {"node_id": "g1", "name": "人工主题"}},
            {"edit_id": "e2", "operation": "undo", "payload": {"edit_id": "e1"}},
        ])
        self.assertEqual(result["analysis_tree"]["children"][0]["name"], "主题一")
        result = apply_tree_edits(self.base(), [
            {"edit_id": "e1", "operation": "rename", "payload": {"node_id": "g1", "name": "人工主题"}},
            {"edit_id": "e2", "operation": "undo", "payload": {"edit_id": "e1"}},
            {"edit_id": "e3", "operation": "redo", "payload": {"edit_id": "e1"}},
        ])
        self.assertEqual(result["analysis_tree"]["children"][0]["name"], "人工主题")


if __name__ == "__main__":
    unittest.main()
