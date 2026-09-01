import time
import unittest

from services.processing_queue import (
    deep_processing_eligible,
    estimated_work_units,
    ranked_pending_paths,
    relationship_recall_paths,
)


class ProcessingQueueTests(unittest.TestCase):
    def test_completed_and_terminal_exclusions_do_not_reenter_queue(self):
        inventory = {name: {"path": name, "size": 100} for name in (
            "done.txt", "duplicate.txt", "restricted.txt", "pending.txt",
        )}
        workflows = {
            "done.txt": {"promotion_allowed": True},
            "duplicate.txt": {
                "promotion_allowed": False,
                "reasons": ["exact_duplicate_non_primary"],
            },
            "restricted.txt": {
                "promotion_allowed": False, "safety_status": "restricted",
            },
            "pending.txt": {"promotion_allowed": True},
        }
        states = {"done.txt": {"status": "completed"}}
        self.assertEqual(
            ranked_pending_paths(inventory, workflows, states), ["pending.txt"]
        )

    def test_priority_changes_order_but_ordinary_files_remain(self):
        inventory = {
            "ordinary-a.txt": {"size": 1},
            "ordinary-b.txt": {"size": 1},
            "wanted.txt": {"size": 1},
        }
        workflows = {
            path: {"promotion_allowed": True, "selection_state": "deferred"}
            for path in inventory
        }
        workflows["wanted.txt"].update({
            "selection_state": "priority", "priority_source": "user_query",
        })
        ordered = ranked_pending_paths(
            inventory, workflows, {}, preferred_paths=["wanted.txt"], limit=3
        )
        self.assertEqual(ordered[0], "wanted.txt")
        self.assertEqual(set(ordered), set(inventory))

    def test_workload_guard_reduces_heavy_batch(self):
        inventory = {
            "large-video.mp4": {"size": 11 * 1024 * 1024 * 1024, "extension": ".mp4"},
            "small.txt": {"size": 100},
        }
        workflows = {path: {"promotion_allowed": True} for path in inventory}
        selected = ranked_pending_paths(
            inventory, workflows, {}, preferred_paths=["large-video.mp4"],
            limit=500, workload_limit=500,
        )
        self.assertEqual(selected, ["large-video.mp4"])
        self.assertEqual(estimated_work_units("large-video.mp4", inventory["large-video.mp4"]), 500)

    def test_retry_backoff_is_respected(self):
        workflow = {"promotion_allowed": True}
        future_failure = {
            "status": "failed", "retryable": True, "attempt_count": 2,
            "next_retry_at": time.time() + 3600,
        }
        self.assertFalse(deep_processing_eligible(workflow, future_failure))

    def test_relationship_recall_explains_why_file_was_selected(self):
        content_map = {"relationships": [{
            "source": "processed.txt", "target": "pending.txt", "weight": 3,
            "reasons": [{"kind": "entity", "value": "甲公司"}],
        }]}
        recalled = relationship_recall_paths(
            content_map, {"processed.txt"}, {"pending.txt"}
        )
        self.assertEqual(recalled[0]["path"], "pending.txt")
        self.assertEqual(recalled[0]["score"], 3.0)
        self.assertIn("entity:甲公司", recalled[0]["reasons"])


if __name__ == "__main__":
    unittest.main()
