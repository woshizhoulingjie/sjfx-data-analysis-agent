import unittest

from services.multiturn_structured_qa import run_multiturn_structured_qa


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat_json(self, system, prompt, **kwargs):
        self.calls.append({"system": system, "prompt": prompt, "kwargs": kwargs})
        context = kwargs["output_context"]
        if context.endswith("问题规划"):
            return {"json": {"turn": "question_planning", "questions": [
                {"question": "问题{}？".format(index), "candidate_evidence_ids": ["E1", "BAD"]}
                for index in range(1, 10)
            ]}}
        if context.endswith("分组证据回答"):
            marker = len([call for call in self.calls if call["kwargs"]["output_context"].endswith("分组证据回答")])
            return {"json": {"turn": "evidence_answering", "answers": [{
                "question_id": "Q-{:03d}".format(marker),
                "answer": "有证据的回答",
                "evidence_ids": ["E1", "BAD"],
            }]}}
        return {"json": {"turn": "answer_review", "review_results": [{
            "question_id": "Q-001",
            "final_answer": "审核后的回答",
            "final_evidence_ids": ["E1", "BAD"],
            "added_evidence_ids": ["BAD"],
            "final_status": "supported",
        }]}}


class MultiTurnStructuredQATest(unittest.TestCase):
    def test_planning_three_answer_batches_and_review_are_real_calls(self):
        llm = FakeLLM()
        result = run_multiturn_structured_qa(
            llm,
            [{"evidence_id": "E1", "source_path": "a.md", "text": "证据"}],
            answer_batches=3,
        )
        self.assertEqual(5, len(llm.calls))
        self.assertEqual(9, len(result["turns"]["question_planning"]["questions"]))
        self.assertEqual(3, len(result["turns"]["evidence_answering"]))
        self.assertEqual(5, result["statistics"]["model_call_count"])
        self.assertEqual(["E1"], result["final_answers"][0]["final_evidence_ids"])
        self.assertEqual([], result["final_answers"][0]["added_evidence_ids"])
        self.assertTrue(all("结构化问答阶段" in call["system"] for call in llm.calls))


if __name__ == "__main__":
    unittest.main()
