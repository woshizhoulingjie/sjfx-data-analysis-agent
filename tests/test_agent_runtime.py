import unittest

from services.agent_runtime import PydanticAgentRuntime
from services.ollama import LocalModelError


class _StructuredTransport:
    supports_json_response_format = True
    model = "qwen3.6:27b"
    base_url = "http://127.0.0.1:8001/v1"
    configured = True
    requires_confirmation = False
    privacy_label = "test vLLM"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, system_prompt, user_prompt, **kwargs):
        self.calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "kwargs": kwargs,
        })
        return self.responses.pop(0)


class AgentRuntimeStructuredOutputTests(unittest.TestCase):
    def _runtime(self, *contents):
        transport = _StructuredTransport([
            {"content": content, "model": "test", "usage": {}}
            for content in contents
        ])
        return PydanticAgentRuntime(transport), transport

    def test_vllm_schema_and_lossless_single_item_array_repair(self):
        runtime, transport = self._runtime('[{"summary":"摘要已完成"}]')

        result = runtime.chat_json(
            "system", "user", required_fields=("core_summary",),
            output_context="全文分块汇总",
        )

        self.assertEqual(result["json"]["core_summary"], "摘要已完成")
        self.assertFalse(result["structured_retry"])
        self.assertEqual(len(transport.calls), 1)
        response_format = transport.calls[0]["kwargs"]["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["core_summary"])
        self.assertIn("core_summary", schema["properties"])

    def test_one_format_correction_retry_recovers_missing_required_field(self):
        runtime, transport = self._runtime(
            '{"title":"缺少摘要"}',
            '{"core_summary":"纠正后的全文摘要"}',
        )

        result = runtime.chat_json(
            "system", "user", required_fields=("core_summary",),
            output_context="全文分块汇总",
        )

        self.assertEqual(result["json"]["core_summary"], "纠正后的全文摘要")
        self.assertTrue(result["structured_retry"])
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("格式纠正", transport.calls[1]["user_prompt"])

    def test_length_retry_doubles_budget(self):
        transport = _StructuredTransport([
            {"content": '{"core_summary":"截断', "model": "test",
             "usage": {}, "finish_reason": "length"},
            {"content": '{"core_summary":"完整摘要"}', "model": "test",
             "usage": {}, "finish_reason": "stop"},
        ])
        runtime = PydanticAgentRuntime(transport)
        result = runtime.chat_json(
            "system", "user", max_tokens=640, required_fields=("core_summary",),
            output_context="全文文档分析",
        )
        self.assertEqual(result["json"]["core_summary"], "完整摘要")
        self.assertTrue(result["structured_retry"])
        self.assertEqual(transport.calls[1]["kwargs"]["max_tokens"], 1280)

    def test_second_invalid_response_fails_without_unbounded_retry(self):
        runtime, transport = self._runtime("[]", "[]")

        with self.assertRaisesRegex(LocalModelError, "格式纠正重试仍失败"):
            runtime.chat_json(
                "system", "user", required_fields=("section_summary",),
                output_context="文档分块分析",
            )

        self.assertEqual(len(transport.calls), 2)


if __name__ == "__main__":
    unittest.main()
