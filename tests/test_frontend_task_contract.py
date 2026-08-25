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
        self.assertIn("/api/jobs?status=active&limit=50&compact=1", script)
        self.assertIn("data-job-cancel", script)
        self.assertIn("queue_position", script)
        self.assertIn("blocking_job", script)
        self.assertIn("heartbeat_at", script)
        self.assertIn("heartbeat_age_seconds", script)
        self.assertIn("isTransientPollError", script)
        self.assertIn("await waitFor(delay)", script)

    def test_large_trees_and_summaries_use_bounded_lazy_endpoints(self):
        script = (PROJECT_ROOT / "static" / "app.js").read_text(encoding="utf-8")
        shell = (PROJECT_ROOT / "static" / "product-shell.js").read_text(encoding="utf-8")
        self.assertIn("?compact=1&summary_limit=100", script)
        self.assertIn("/api/tree/", script)
        self.assertIn("_children_next_offset", script)
        self.assertIn("loadTreeChildren", script)
        self.assertIn("/api/summaries/", script)
        self.assertIn("sessionStorage.getItem('sjfx_api_token')", script)
        self.assertIn("sessionStorage.removeItem('sjfx_api_token')", script)
        self.assertNotIn("localStorage.getItem('sjfx_api_token')", script)
        self.assertIn("/api/download-ticket", script)
        self.assertNotIn("response.blob()", script)
        self.assertNotIn("URL.createObjectURL", script)
        self.assertIn("selectionRequestId", script)
        self.assertIn("selectionStillCurrent", script)
        self.assertIn("sessionStorage.removeItem('sjfx_api_token')", shell)
        self.assertNotIn("localStorage.removeItem('sjfx_api_token')", shell)

    def test_dashboard_shell_reads_current_semantic_metric_contract(self):
        shell = (PROJECT_ROOT / "static" / "product-shell.js").read_text(encoding="utf-8")
        self.assertIn("querySelectorAll('.metric-grid > div')", shell)
        self.assertIn("metricValue('递归文件')", shell)
        self.assertIn("metricValue('总大小')", shell)
        self.assertIn(r"/内容解析\s*(\d+(?:\.\d+)?%|—)/", shell)
        self.assertIn(r"/研究潜力\s*([^·；\n]+)/", shell)
        self.assertNotIn(r"/已分析\s*", shell)
        self.assertNotIn("价值判断：", shell)

    def test_completed_analysis_can_restore_the_workspace_and_requested_tree(self):
        script = (PROJECT_ROOT / "static" / "app.js").read_text(encoding="utf-8")
        html = (PROJECT_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        app_source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("CURRENT_SCAN_KEY", script)
        self.assertIn("restoreWorkspace", script)
        self.assertIn("/api/jobs?status=all&limit=50&compact=1", script)
        self.assertIn("window.SJFXShell?.route", script)
        self.assertIn("await $('analysisTreeBtn').onclick()", script)
        self.assertIn("useProgressiveAnalysis", script)
        self.assertIn("data-job-open", script)
        self.assertIn("打开分析结果", script)
        self.assertIn('/static/app.js?v=20', html)
        self.assertIn("allowed_result_fields", app_source)
        self.assertIn("_job_api_view(job, compact=compact)", app_source)

    def test_python_runtime_contract_is_version_aware(self):
        app_source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        package_source = (PROJECT_ROOT / "services" / "package_analysis.py").read_text(encoding="utf-8")
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("MINIMUM_PYTHON_VERSION = (3, 10)", app_source)
        self.assertIn('"runtime_supported": runtime["supported"]', app_source)
        self.assertIn('"python_version": runtime["version"]', app_source)
        self.assertIn('"minimum_python_version": runtime["minimum"]', app_source)
        self.assertNotIn('"python_compatible": True', app_source)
        self.assertNotIn("Python 3.7 baseline", package_source)
        self.assertIn("Python 3.10 或更高版本", readme)


if __name__ == "__main__":
    unittest.main()
