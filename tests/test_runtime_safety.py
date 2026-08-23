import json
import multiprocessing as mp
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from services.ollama import OllamaClient
from services.package_analysis import _parse_with_limits
from services.structured_qa import answer_question
from services.unified_parser import UnifiedDocumentParser


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class RuntimeSafetyTests(unittest.TestCase):
    def test_real_parser_round_trip_uses_isolated_process(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.txt"
            path.write_text("隔离解析正文", encoding="utf-8")
            document = _parse_with_limits(
                UnifiedDocumentParser(max_chars=1000), path, "sample.txt", "fast"
            )
            self.assertIn("隔离解析正文", document["text"])

    @unittest.skipUnless("fork" in mp.get_all_start_methods(), "需要 POSIX fork 进程隔离测试")
    def test_parse_timeout_terminates_native_parser_child(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "slow.txt"
            path.write_text("慢解析", encoding="utf-8")
            original = UnifiedDocumentParser.parse

            def slow_parse(self, *args, **kwargs):
                time.sleep(3)
                return original(self, *args, **kwargs)

            old_timeout = os.environ.get("MAX_PARSE_SECONDS")
            old_method = os.environ.get("SJFX_PARSE_START_METHOD")
            os.environ["MAX_PARSE_SECONDS"] = "1"
            os.environ["SJFX_PARSE_START_METHOD"] = "fork"
            try:
                with patch.object(UnifiedDocumentParser, "parse", slow_parse):
                    with self.assertRaises(TimeoutError):
                        _parse_with_limits(UnifiedDocumentParser(max_chars=1000), path, "slow.txt", "fast")
            finally:
                if old_timeout is None:
                    os.environ.pop("MAX_PARSE_SECONDS", None)
                else:
                    os.environ["MAX_PARSE_SECONDS"] = old_timeout
                if old_method is None:
                    os.environ.pop("SJFX_PARSE_START_METHOD", None)
                else:
                    os.environ["SJFX_PARSE_START_METHOD"] = old_method

    def test_local_model_retries_transient_transport_failure(self):
        client = OllamaClient("http://127.0.0.1:11434/v1", "qwen-agent:latest", timeout=2)
        payload = {
            "model": client.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 10,
        }
        response = _Response({
            "model": client.model,
            "message": {"content": "pong"},
            "done": True,
        })
        with patch(
            "services.ollama.urllib.request.urlopen",
            side_effect=[URLError("temporary restart"), response],
        ) as urlopen:
            result = client._request(payload, retries=1, timeout=1)
        self.assertEqual(result["content"], "pong")
        self.assertEqual(urlopen.call_count, 2)

    def test_structured_question_matches_unicode_field_and_reports_sampling(self):
        documents = [{
            "path": "sales.csv",
            "payload": {
                "data_profile": {
                    "status": "partial",
                    "row_count": 2,
                    "columns": {"销售额": {"inferred_type": "number", "sum": 12, "count": 2}},
                }
            },
        }]
        result = answer_question("销售额的总和是多少？", documents)
        self.assertEqual(result["value"], 12)
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["confidence"], "中")

    def test_windows_hard_stop_targets_the_complete_process_tree(self):
        import worker

        class FakeProcess:
            pid = 4321

            def __init__(self):
                self.running = True

            def is_alive(self):
                return self.running

            def join(self, timeout=None):
                self.running = False

            def terminate(self):
                self.running = False

        class Completed:
            returncode = 0

        process = FakeProcess()
        with patch.object(worker.os, "name", "nt"), patch(
            "worker.subprocess.run", return_value=Completed()
        ) as taskkill:
            worker._stop_process(process)
        command = taskkill.call_args[0][0]
        self.assertEqual(command[:2], ["taskkill", "/PID"])
        self.assertIn("/T", command)
        self.assertIn("/F", command)
        self.assertFalse(process.is_alive())


if __name__ == "__main__":
    unittest.main()
