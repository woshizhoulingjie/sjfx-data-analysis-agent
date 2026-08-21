import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FrontendTaskContractTests(unittest.TestCase):
    def test_task_center_controls_have_unique_static_ids(self):
        html = (PROJECT_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        ids = re.findall(r'\bid="([^"]+)"', html)
        required = {
            "cancelJobBtn",
            "taskCenterCancelBtn",
            "taskCenterProgressBar",
            "taskCenterProgressText",
            "taskCenterRefreshBtn",
            "activeTaskList",
        }
        self.assertTrue(required.issubset(ids), required.difference(ids))
        self.assertEqual(len(ids), len(set(ids)), "HTML element ids must remain unique")

    def test_task_client_keeps_queue_and_retry_contracts(self):
        script = (PROJECT_ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/jobs?status=active&limit=50", script)
        self.assertIn("data-job-cancel", script)
        self.assertIn("queue_position", script)
        self.assertIn("blocking_job", script)
        self.assertIn("heartbeat_at", script)
        self.assertIn("heartbeat_age_seconds", script)
        self.assertIn("isTransientPollError", script)
        self.assertIn("await waitFor(delay)", script)


if __name__ == "__main__":
    unittest.main()
