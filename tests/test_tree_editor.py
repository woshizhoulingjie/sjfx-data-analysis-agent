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
        tree["children"][0]["member_paths"].append("c.txt")
        tree["children"][0]["children"].append({
            "kind": "file", "path": "c.txt", "name": "c.txt",
            "classification_status": "classified", "classification_confidence": 0.95,
        })
        low = filter_tree(tree, "low_confidence")
        self.assertEqual([item["path"] for item in low["children"][0]["children"]], ["a.txt"])
        pending = filter_tree(tree, "unclassified")
        self.assertEqual([item["path"] for item in pending["children"][0]["children"]], ["b.txt"])

    def test_confirmed_filter_keeps_a_confirmed_topic_and_its_files(self):
        result = apply_tree_edits(self.base(), [{
            "operation": "confirm", "payload": {"node_id": "g1", "confirmed": True},
        }])
        confirmed = filter_tree(result["analysis_tree"], "confirmed")
        self.assertEqual([item["node_id"] for item in confirmed["children"]], ["g1"])
        self.assertEqual([item["path"] for item in confirmed["children"][0]["children"]], ["a.txt"])

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

    def test_incomplete_split_never_drops_unassigned_files(self):
        analysis = self.base()
        analysis["analysis_tree"]["children"][0]["member_paths"] = ["a.txt", "c.txt", "d.txt"]
        result = apply_tree_edits(analysis, [{
            "operation": "split",
            "payload": {
                "node_id": "g1",
                "groups": [
                    {"name": "A", "paths": ["a.txt"]},
                    {"name": "C", "paths": ["c.txt"]},
                ],
            },
        }])
        node = result["analysis_tree"]["children"][0]
        self.assertEqual(node["node_id"], "g1")
        self.assertEqual(set(node["member_paths"]), {"a.txt", "c.txt", "d.txt"})

    def test_nested_sibling_merge_stays_under_its_parent(self):
        analysis = self.base()
        parent = {
            "kind": "group", "node_id": "parent", "name": "父主题",
            "member_paths": ["c.txt", "d.txt"],
            "children": [
                {"kind": "group", "node_id": "n1", "name": "N1", "member_paths": ["c.txt"], "children": []},
                {"kind": "group", "node_id": "n2", "name": "N2", "member_paths": ["d.txt"], "children": []},
            ],
        }
        analysis["analysis_tree"]["children"].append(parent)
        result = apply_tree_edits(analysis, [{
            "operation": "merge",
            "payload": {"node_ids": ["n1", "n2"], "name": "嵌套合并"},
        }])
        root_children = result["analysis_tree"]["children"]
        merged_parent = next(item for item in root_children if item.get("node_id") == "parent")
        self.assertEqual(len(merged_parent["children"]), 1)
        self.assertEqual(merged_parent["children"][0]["name"], "嵌套合并")
        self.assertEqual(set(merged_parent["children"][0]["member_paths"]), {"c.txt", "d.txt"})

    def test_nested_split_replaces_the_target_in_place(self):
        analysis = self.base()
        nested = {
            "kind": "group", "node_id": "nested", "name": "待拆",
            "member_paths": ["c.txt", "d.txt"], "children": [],
        }
        analysis["analysis_tree"]["children"][0]["children"].append(nested)
        result = apply_tree_edits(analysis, [{
            "operation": "split",
            "payload": {"node_id": "nested", "groups": [
                {"name": "C", "paths": ["c.txt"]},
                {"name": "D", "paths": ["d.txt"]},
            ]},
        }])
        first_topic = result["analysis_tree"]["children"][0]
        self.assertFalse(any(item.get("node_id") == "nested" for item in first_topic["children"]))
        split_groups = [item for item in first_topic["children"] if item.get("manual_split")]
        self.assertEqual([item["name"] for item in split_groups], ["C", "D"])
        self.assertEqual(len(result["analysis_tree"]["children"]), 2)


if __name__ == "__main__":
    unittest.main()
