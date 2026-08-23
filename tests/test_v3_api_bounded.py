import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from services.storage import Storage


HAS_DOCX = importlib.util.find_spec("docx") is not None


@unittest.skipUnless(HAS_DOCX, "python-docx 未安装，跳过完整 app 导入测试")
class BoundedApiTests(unittest.TestCase):
    def test_client_json_cannot_claim_internal_summary_execution(self):
        import app as app_module

        payload = {
            "scan_id": "scan-1", "node_id": "topic-1", "force": True,
            "_worker_execution": True,
        }
        with patch.object(app_module, "llm_generation_enabled", True), \
                patch.object(app_module, "require_scan", return_value={"root": "."}), \
                patch.object(app_module, "_find_analysis_node", return_value={"kind": "group"}), \
                patch.object(app_module, "require_local_model_enabled"), \
                patch.object(app_module.storage, "get_summary", return_value=None), \
                patch.object(
                    app_module.storage, "create_or_get_typed_job", return_value=("job-1", True)
                ) as create_job, \
                patch.object(app_module, "analyze_folder") as analyze_folder:
            with app_module.app.test_request_context("/api/summary", method="POST", json=payload):
                response, status = app_module.summarize()

        self.assertEqual(status, 202)
        self.assertTrue(response.get_json()["accepted"])
        create_job.assert_called_once()
        analyze_folder.assert_not_called()

    def test_worker_uses_process_local_summary_execution_capability(self):
        import app as app_module

        observed = {}

        def fake_summary():
            observed["internal"] = app_module._summary_worker_execution.get()
            observed["payload"] = dict(app_module.request.get_json() or {})
            return app_module.jsonify({"ok": True, "summary": {"title": "done"}})

        job = {
            "id": "job-1", "scan_id": "scan-1", "owner_id": app_module.Config.OWNER_ID,
            "options": {"scan_id": "scan-1", "_worker_execution": True},
        }
        with patch.object(app_module.storage, "update_job"), \
                patch.object(app_module, "_ensure_job_active"), \
                patch.object(app_module, "summarize", side_effect=fake_summary):
            result = app_module._run_claimed_summary_job(job)

        self.assertTrue(observed["internal"])
        self.assertNotIn("_worker_execution", observed["payload"])
        self.assertFalse(app_module._summary_worker_execution.get())
        self.assertEqual(result["summary"]["title"], "done")

    def test_large_download_uses_an_owner_bound_one_use_navigation_ticket(self):
        import app as app_module

        original_storage = app_module.storage
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs"
            output_dir.mkdir()
            (output_dir / "结果 包.zip").write_bytes(b"stream-me")
            storage = Storage(root / "state.db", root / "documents")
            storage.save_artifact("结果 包.zip", app_module.Config.OWNER_ID, kind="test")
            app_module.storage = storage
            try:
                with patch.object(app_module.Config, "OUTPUT_DIR", output_dir), \
                        patch.object(app_module.Config, "AUTH_REQUIRED", True), \
                        patch.object(app_module.Config, "API_ACCESS_TOKEN", "test-secret"), \
                        patch.object(app_module.Config, "API_TOKEN_EXPIRES_AT", None):
                    async def exercise_download():
                        transport = ASGITransport(app=app_module.app)
                        async with AsyncClient(
                            transport=transport, base_url="http://testserver"
                        ) as client:
                            denied = await client.get(
                                "/outputs/%E7%BB%93%E6%9E%9C%20%E5%8C%85.zip"
                            )
                            issued = await client.post(
                                "/api/download-ticket",
                                json={"filename": "结果 包.zip"},
                                headers={"X-SJFX-Token": "test-secret"},
                            )
                            download_url = issued.json().get("download_url")
                            downloaded = await client.get(download_url)
                            replay = await client.get(download_url)
                            return denied, issued, downloaded, replay

                    denied, issued, downloaded, replay = asyncio.run(exercise_download())
                    self.assertEqual(denied.status_code, 401)
                    self.assertEqual(issued.status_code, 200)
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertEqual(downloaded.content, b"stream-me")
                    self.assertEqual(replay.status_code, 401)
            finally:
                app_module.storage = original_storage

    def test_progressive_inventory_never_claims_complete_after_scan_limits(self):
        import app as app_module

        original_storage = app_module.storage
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp) / "documents")
            app_module.storage = storage
            try:
                app_module._publish_analysis_progress(
                    "scan-partial",
                    {
                        "file_count": 4,
                        "directory_count": 2,
                        "total_size": 10,
                        "truncated": True,
                        "ignored_file_count": 1,
                        "errors": [],
                    },
                    15,
                    "目录盘点完成",
                    "inventory_ready",
                )
                coverage = storage.get_analysis_progress("scan-partial")["coverage"]
                self.assertFalse(coverage["inventory_complete"])
                self.assertIsNone(coverage["inventory_coverage_ratio"])
                self.assertTrue(coverage["limitations"])
            finally:
                app_module.storage = original_storage

    def test_compact_scan_never_returns_full_tree_or_document_indexes(self):
        import app as app_module

        original_storage = app_module.storage
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp) / "documents")
            app_module.storage = storage
            try:
                scan = {
                    "root": tmp, "file_count": 3, "directory_count": 0,
                    "total_size": 3, "total_size_human": "3 B",
                    "tree": {
                        "kind": "directory", "name": "root", "path": ".",
                        "file_count": 3, "children": [
                            {"kind": "file", "name": "{}.txt".format(index),
                             "path": "{}.txt".format(index), "size": 1}
                            for index in range(3)
                        ],
                    },
                }
                storage.save_scan(
                    scan, scan_id="scan-1", owner_id=app_module.Config.OWNER_ID
                )
                storage.save_analysis_progress("scan-1", {
                    "status": "running", "progress": 25,
                    "coverage": {"inventory_files": 3, "parsed_files": 1},
                })
                with app_module.app.test_request_context(
                    "/api/scan/scan-1?compact=1&summary_limit=1"
                ):
                    response = app_module.get_scan("scan-1")
                payload = response.get_json()
                self.assertEqual(payload["response_mode"], "bounded")
                self.assertEqual(payload["progressive_analysis"]["progress"], 25)
                self.assertEqual(len(payload["scan"]["tree"]["children"]), 3)

                analysis = {
                    "scan_id": "scan-1", "status": "completed",
                    "statistics": {"parsed_files": 3},
                    "coverage": {"inventory_files": 3, "parsed_files": 3},
                    "analysis_tree": {
                        "kind": "analysis_root", "name": "智能目录", "children": [{
                            "kind": "group", "name": "主题", "node_id": "topic-1",
                            "member_paths": ["0.txt", "1.txt", "2.txt"], "children": [],
                        }],
                    },
                    "document_index": [{"path": "{}.txt".format(index)} for index in range(3)],
                }
                storage.save_analysis("scan-1", analysis)
                with app_module.app.test_request_context("/api/scan/scan-1?compact=1"):
                    response = app_module.get_scan("scan-1")
                payload = response.get_json()
                self.assertNotIn("document_index", payload["analysis"])
                self.assertEqual(payload["analysis"]["analysis_tree"]["children"][0]["node_id"], "topic-1")
                self.assertIsNone(payload["progressive_analysis"])
            finally:
                app_module.storage = original_storage


if __name__ == "__main__":
    unittest.main()
