import json
import math
import tempfile
import unittest
from pathlib import Path

from services.structured_profile import _profile_rows, profile_path
from services.structured_qa import answer_question


def document(path, column, rows=1):
    return {"path": path, "payload": {"data_profile": {
        "status": "completed", "row_count": rows,
        "columns": {"amount": column}, "coverage": {"complete": True},
    }}}


def profiled_document(path, values):
    rows = [{"amount": value} for value in values]
    profile = _profile_rows(rows, {"amount": values}, len(rows), 0)
    return {"path": path, "payload": {"data_profile": profile}}


class StructuredQAFiniteTests(unittest.TestCase):
    def assert_finite_answer(self, question, documents, expected):
        answer = answer_question(question, documents)
        self.assertTrue(math.isfinite(answer["value"]))
        if expected == 0:
            self.assertEqual(answer["value"], 0)
        else:
            self.assertAlmostEqual(answer["value"] / expected, 1)
        json.dumps(answer, allow_nan=False)
        return answer

    def test_csv_overflow_total_is_rejected_but_average_is_representable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "large.csv"
            path.write_text("amount\n" + "1e308\n" * 6, encoding="utf-8")
            profile = profile_path(path)
        documents = [{"path": "large.csv", "payload": {"data_profile": profile}}]
        self.assertEqual(profile["columns"]["amount"]["numeric_warnings"], ["sum_out_of_range"])
        with self.assertRaisesRegex(ValueError, "合计超出可表示数字范围"):
            answer_question("amount sum", documents)
        answer = self.assert_finite_answer("amount average", documents, 1e308)
        self.assertIsNone(answer["evidence"][0]["value"])
        self.assertEqual(answer["evidence"][0]["numeric_count"], 6)
        self.assertIn("加权分子超出可表示数字范围", answer["evidence"][0]["text"])

    def test_average_scales_by_each_profile_count(self):
        documents = [
            profiled_document("first.csv", [1e308] * 6),
            profiled_document("second.csv", [5e307] * 2),
        ]
        answer = self.assert_finite_answer("amount average", documents, 8.75e307)
        self.assertEqual(answer["aggregation_scope"]["participating_source_count"], 2)
        self.assertIn("缩放加权", answer["calculation"])

    def test_positive_and_negative_overflow_totals_can_cancel_in_average(self):
        documents = [
            profiled_document("positive.csv", [1e308] * 6),
            profiled_document("negative.csv", [-1e308] * 6),
        ]
        self.assert_finite_answer("amount average", documents, 0)

    def test_finite_local_sums_can_overflow_when_combined(self):
        for sign in (1, -1):
            with self.subTest(sign=sign):
                documents = [profiled_document("one.csv", [sign * 1e308]), profiled_document("two.csv", [sign * 1e308])]
                with self.assertRaisesRegex(ValueError, "合计超出可表示数字范围"):
                    answer_question("amount sum", documents)
                answer = self.assert_finite_answer("amount average", documents, sign * 1e308)
                self.assertTrue(all(item["value"] is not None for item in answer["evidence"]))

    def test_legacy_missing_sum_cannot_reconstruct_infinity(self):
        documents = [document("legacy.csv", {"inferred_type": "number", "mean": 1e308, "count": 6}, rows=6)]
        with self.assertRaisesRegex(ValueError, "合计超出可表示数字范围"):
            answer_question("amount sum", documents)
        self.assert_finite_answer("amount average", documents, 1e308)

    def test_intermediate_overflow_can_recover_a_representable_combined_sum(self):
        documents = [
            profiled_document("a.csv", [1e308]),
            profiled_document("b.csv", [1e308]),
            profiled_document("c.csv", [-1e308]),
        ]
        self.assert_finite_answer("amount sum", documents, 1e308)
        self.assert_finite_answer("amount average", documents, 1e308 / 3)

    def test_explicit_out_of_range_warning_prevents_rounded_sum_reconstruction(self):
        documents = [document("flagged.csv", {"inferred_type": "number", "mean": 1, "count": 2, "numeric_warnings": ["sum_out_of_range"]}, rows=2)]
        with self.assertRaisesRegex(ValueError, "合计超出可表示数字范围"):
            answer_question("amount sum", documents)
        self.assert_finite_answer("amount average", documents, 1)

    def test_unrepresentable_total_does_not_block_extremes_or_count(self):
        documents = [profiled_document("large.csv", [1e308] * 6)]
        for question, expected in [("amount max", 1e308), ("amount min", 1e308), ("amount count", 6)]:
            with self.subTest(question=question):
                self.assert_finite_answer(question, documents, expected)

    def test_ordinary_weighted_average_and_legacy_reconstruction_stay_compatible(self):
        documents = [
            document("first.csv", {"inferred_type": "number", "sum": 30, "mean": 15, "count": 2}, rows=2),
            document("second.csv", {"inferred_type": "number", "mean": 100, "count": 3}, rows=3),
        ]
        self.assert_finite_answer("amount sum", documents, 330)
        answer = self.assert_finite_answer("amount average", documents, 66)
        self.assertEqual(answer["calculation"], "各参与表字段 sum 总和 ÷ 非空数值 count 总和（加权平均）")
        self.assertEqual([item["value"] for item in answer["evidence"]], [30, 300])
        self.assertIn("由均值×非空数值数重建", answer["evidence"][1]["text"])

    def test_missing_required_average_statistics_remains_an_error(self):
        documents = [document("missing.csv", {"inferred_type": "number", "count": 6}, rows=6)]
        with self.assertRaisesRegex(ValueError, "缺少 average 所需的统计值"):
            answer_question("amount average", documents)


if __name__ == "__main__":
    unittest.main()
