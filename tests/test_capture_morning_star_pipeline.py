import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture_morning_star_pipeline.py"
SPEC = importlib.util.spec_from_file_location("capture_morning_star_pipeline", SCRIPT)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


MATERIALS = ROOT / "benchmark_capture" / "morning_star_documents"


class CaptureMorningStarPipelineTest(unittest.TestCase):
    # benchmark_capture/ 在 .gitignore 里，只存在于部署目录；干净检出/工作树里没有素材时跳过，
    # 而不是报"用例失败"（素材在的地方照常断言）。
    @unittest.skipUnless(MATERIALS.is_dir(), "benchmark_capture 素材不在本检出（已 gitignore）")
    def test_materials_cover_required_analysis(self):
        documents = capture.load_documents(MATERIALS)
        joined = "\n".join(item["content"] for item in documents)
        self.assertEqual(12, len(documents))
        for expected in (
            "1,200,000", "80,000", "20%", "50%", "30%",
            "每秒 730", "每秒 825", "2025 年 4 月 16 日",
            "合规发票", "完整交付文档", "超过 30 日",
        ):
            self.assertIn(expected, joined)

    def test_all_six_stages_have_fixed_generation_and_tasks(self):
        self.assertEqual(
            ["scan", "document_analysis", "evidence", "structured_qa", "reporting", "export"],
            [item[0] for item in capture.STAGES],
        )
        self.assertEqual(768, capture.FIXED_GENERATION["max_tokens"])
        self.assertEqual(0, capture.FIXED_GENERATION["temperature"])
        self.assertEqual(1.0, capture.FIXED_GENERATION["top_p"])
        self.assertEqual(42, capture.FIXED_GENERATION["seed"])
        self.assertFalse(capture.FIXED_GENERATION["chat_template_kwargs"]["enable_thinking"])
        for stage_id, _, _ in capture.STAGES:
            self.assertIn(stage_id, capture.SYSTEM_PROMPTS)
            self.assertIn(stage_id, capture.STAGE_TASKS)

    def test_trace_case_keeps_full_response_and_stats_fields(self):
        request = {
            "model": "qwen3.6:27b", "messages": [], "max_tokens": 768,
            "temperature": 0, "top_p": 1.0, "seed": 42,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = {
            "choices": [{"message": {"content": "完整输出"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
        trace = {
            "trace_id": "trace", "workflow_id": "workflow", "sequence": 2,
            "call_id": "call", "stage_id": "evidence", "parent_call_ids": ["parent"],
            "concurrency_group": "serial-002", "arrival_offset_ms": 1.5,
            "request": request, "response": response,
            "input_token_ids": [1, 2], "output_token_ids": [3],
            "source_cached_tokens": 0, "source_cached_tokens_source": "prefix_cache_disabled",
            "latency_seconds": 0.25, "tokenizer_fingerprint": None,
        }
        case = capture.trace_to_case(trace, "trace.jsonl", 3)
        self.assertEqual("完整输出", case["extensions"]["observed_output"])
        self.assertEqual(response, case["extensions"]["observed_response"])
        self.assertEqual([1, 2], case["extensions"]["input_token_ids"])
        self.assertEqual([3], case["extensions"]["output_token_ids"])
        self.assertEqual("trace.jsonl#L3", case["reference"]["output_ref"])

    def test_jsonl_append_round_trips_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            capture.append_jsonl(path, {"response": "晨星"})
            self.assertEqual(
                {"response": "晨星"},
                json.loads(path.read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
