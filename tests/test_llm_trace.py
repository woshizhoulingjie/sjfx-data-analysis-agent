import json
from pathlib import Path
import tempfile
import unittest

from services import llm_trace


def response(content, prompt_ids, output_ids, cached_tokens=0):
    return {
        "choices": [{
            "message": {"content": content},
            "finish_reason": "stop",
            "token_ids": output_ids,
        }],
        "prompt_token_ids": prompt_ids,
        "usage": {
            "prompt_tokens": len(prompt_ids),
            "completion_tokens": len(output_ids),
            "total_tokens": len(prompt_ids) + len(output_ids),
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }


class LLMTraceTest(unittest.TestCase):
    def setUp(self):
        llm_trace._LAST_CALL_ID.set(None)

    def test_array_has_one_object_per_call_and_serial_group_is_null(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "trace.jsonl"
            recorder = llm_trace.TraceRecorder(trace_path)
            recorder.ensure_manifest({
                "model": "test-model",
                "tokenizer": {"fingerprint": None},
                "chat_template": {},
                "cache_config": {"enable_prefix_caching": True},
                "vllm_version": "test",
            })
            first = recorder.begin({"messages": [{"role": "user", "content": "a"}]}, "文档分析")
            recorder.finish(first, response("A", [1, 2], [3], 0))
            second = recorder.begin({"messages": [{"role": "user", "content": "b"}]}, "证据核验")
            recorder.finish(second, response("B", [1, 4], [5], 1))
            calls_path = Path(directory) / "llm_calls.json"
            metadata_path = Path(directory) / "metadata.json"
            llm_trace.write_calls_json(trace_path, calls_path, metadata_path)
            calls = json.loads(calls_path.read_text(encoding="utf-8"))
            self.assertIsInstance(calls, list)
            self.assertEqual(2, len(calls))
            self.assertEqual(
                {
                    "sequence", "call_id", "stage_id", "parent_call_ids",
                    "concurrency_group", "request", "response",
                    "input_token_ids", "output_token_ids", "source_cached_tokens",
                },
                set(calls[0]),
            )
            self.assertIsNone(calls[0]["concurrency_group"])
            self.assertEqual([], calls[0]["parent_call_ids"])
            self.assertEqual([calls[0]["call_id"]], calls[1]["parent_call_ids"])
            self.assertEqual(1, calls[1]["source_cached_tokens"])
            self.assertEqual("B", calls[1]["response"]["content"])

    def test_overlapping_calls_share_concurrency_group(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "trace.jsonl"
            recorder = llm_trace.TraceRecorder(trace_path)
            recorder.ensure_manifest({"tokenizer": {}, "cache_config": {}})
            first = recorder.begin({"messages": []}, "文档分析")
            second = recorder.begin({"messages": []}, "文档分析")
            recorder.finish(second, response("B", [1], [2]))
            recorder.finish(first, response("A", [1], [3]))
            calls_path = Path(directory) / "llm_calls.json"
            llm_trace.write_calls_json(trace_path, calls_path, Path(directory) / "metadata.json")
            calls = json.loads(calls_path.read_text(encoding="utf-8"))
            self.assertIsNotNone(calls[0]["concurrency_group"])
            self.assertEqual(calls[0]["concurrency_group"], calls[1]["concurrency_group"])

    def test_stage_transition_depends_on_all_calls_from_predecessor_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "trace.jsonl"
            recorder = llm_trace.TraceRecorder(trace_path)
            recorder.ensure_manifest({"tokenizer": {}, "cache_config": {}})
            first = recorder.begin({"messages": []}, "文档分析")
            recorder.finish(first, response("A", [1], [2]))
            llm_trace._LAST_CALL_ID.set(None)
            second = recorder.begin({"messages": []}, "文档分析")
            recorder.finish(second, response("B", [1], [3]))
            report = recorder.begin({"messages": []}, "报告编写")
            recorder.finish(report, response("R", [1], [4]))
            calls_path = Path(directory) / "llm_calls.json"
            llm_trace.write_calls_json(trace_path, calls_path, Path(directory) / "metadata.json")
            calls = json.loads(calls_path.read_text(encoding="utf-8"))
            self.assertEqual([], calls[0]["parent_call_ids"])
            self.assertEqual([], calls[1]["parent_call_ids"])
            self.assertEqual(
                [calls[0]["call_id"], calls[1]["call_id"]],
                calls[2]["parent_call_ids"],
            )


if __name__ == "__main__":
    unittest.main()
