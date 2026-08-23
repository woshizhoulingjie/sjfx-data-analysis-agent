import tempfile
import unittest
from pathlib import Path

from services.structured_qa import answer_question
from services.structured_profile import profile_path


def profile(rows, columns, status="completed"):
    return {
        "status": status,
        "row_count": rows,
        "columns": columns,
        "coverage": {"complete": status == "completed", "truncated": status == "partial"},
    }


def numeric(total, count, mean=None, minimum=None, maximum=None):
    return {
        "inferred_type": "number",
        "sum": total,
        "count": count,
        "mean": mean if mean is not None else (float(total) / count if count else None),
        "min": minimum,
        "max": maximum,
    }


def document(path, data_profile):
    return {"path": path, "payload": {"data_profile": data_profile}}


class StructuredQAMultiFileTests(unittest.TestCase):
    def setUp(self):
        self.sales = [
            document("north.csv", profile(2, {
                "地区": {"inferred_type": "text"},
                "销售额": numeric(30, 2, mean=15, minimum=10, maximum=20),
            })),
            document("south.csv", profile(3, {
                "地区": {"inferred_type": "text"},
                "销售额": numeric(300, 3, mean=100, minimum=50, maximum=150),
            })),
        ]

    def test_sum_combines_all_homogeneous_files_and_exposes_scope(self):
        result = answer_question("销售额合计是多少？", self.sales)

        self.assertEqual(result["value"], 330)
        self.assertEqual(result["source_paths"], ["north.csv", "south.csv"])
        self.assertEqual(result["aggregation_scope"]["participating_source_count"], 2)
        self.assertEqual(result["aggregation_scope"]["participating_profile_count"], 2)
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual({item["source_path"] for item in result["evidence"]}, {"north.csv", "south.csv"})
        self.assertIn("共 2 个文件", result["source_path"])

    def test_average_is_weighted_by_numeric_count(self):
        result = answer_question("销售额平均值是多少？", self.sales)

        self.assertEqual(result["value"], 66)
        self.assertIn("加权平均", result["calculation"])

    def test_maximum_and_minimum_combine_file_statistics(self):
        maximum = answer_question("销售额最高是多少？", self.sales)
        minimum = answer_question("销售额最低是多少？", self.sales)

        self.assertEqual(maximum["value"], 150)
        self.assertEqual(minimum["value"], 10)

    def test_record_count_uses_rows_not_numeric_non_null_count(self):
        documents = [document("missing-values.csv", profile(10, {
            "销售额": numeric(70, 7, mean=10, minimum=1, maximum=20),
        }))]

        result = answer_question("这个表的记录数是多少？", documents)

        self.assertEqual(result["value"], 10)
        self.assertEqual(result["unit"], "行")
        self.assertNotIn("column", result)
        self.assertIn("未使用任何数值列", result["calculation"])

    def test_record_count_can_sum_rows_across_files(self):
        result = answer_question("一共有多少条记录？", self.sales)

        self.assertEqual(result["value"], 5)
        self.assertEqual([item["value"] for item in result["evidence"]], [2, 3])

    def test_explicit_field_count_sums_non_null_numeric_counts(self):
        result = answer_question("销售额有多少个有效数值？", self.sales)

        self.assertEqual(result["value"], 5)
        self.assertEqual(result["unit"], "个非空数值")
        self.assertEqual(result["column"], "销售额")

    def test_same_field_in_heterogeneous_schemas_is_refused(self):
        documents = self.sales[:1] + [document("budget.csv", profile(1, {
            "项目": {"inferred_type": "text"},
            "销售额": numeric(999, 1, mean=999, minimum=999, maximum=999),
        }))]

        with self.assertRaisesRegex(ValueError, "结构不同.*拒绝计算"):
            answer_question("销售额合计是多少？", documents)

    def test_explicit_file_hint_disambiguates_heterogeneous_schemas(self):
        documents = self.sales[:1] + [document("budget.csv", profile(1, {
            "项目": {"inferred_type": "text"},
            "销售额": numeric(999, 1, mean=999, minimum=999, maximum=999),
        }))]

        result = answer_question("north.csv 的销售额合计是多少？", documents)

        self.assertEqual(result["value"], 30)
        self.assertEqual(result["source_paths"], ["north.csv"])
        self.assertEqual(result["aggregation_scope"]["excluded_profile_count"], 1)

    def test_unrelated_profiles_without_field_are_excluded_and_reported(self):
        documents = self.sales + [document("people.csv", profile(4, {
            "姓名": {"inferred_type": "text"},
            "年龄": numeric(120, 4, mean=30, minimum=20, maximum=40),
        }))]

        result = answer_question("销售额合计是多少？", documents)

        self.assertEqual(result["value"], 330)
        self.assertEqual(result["aggregation_scope"]["excluded_profile_count"], 1)
        self.assertEqual(result["aggregation_scope"]["excluded_sources"][0]["source_path"], "people.csv")

    def test_single_file_partial_profile_remains_compatible(self):
        documents = [document("sales.csv", profile(2, {
            "销售额": numeric(12, 2, mean=6, minimum=5, maximum=7),
        }, status="partial"))]

        result = answer_question("销售额的总和是多少？", documents)

        self.assertEqual(result["value"], 12)
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["confidence"], "中")
        self.assertIn("有界采样", result["evidence"][0]["text"])

    def test_missing_stat_in_one_participant_is_not_silently_ignored(self):
        documents = self.sales[:1] + [document("south.csv", profile(3, {
            "地区": {"inferred_type": "text"},
            "销售额": {"inferred_type": "number", "count": 3},
        }))]

        with self.assertRaisesRegex(ValueError, "缺少 average 所需"):
            answer_question("销售额平均值是多少？", documents)

    def test_same_named_non_numeric_field_is_not_silently_excluded(self):
        documents = self.sales[:1] + [document("broken.csv", profile(2, {
            "地区": {"inferred_type": "text"},
            "销售额": {"inferred_type": "text", "count": 2},
        }))]

        with self.assertRaisesRegex(ValueError, "不是数值类型.*拒绝计算"):
            answer_question("销售额合计是多少？", documents)

    def test_real_csv_profiles_are_combined(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "一月.csv"
            second = Path(folder) / "二月.csv"
            first.write_text("地区,销售额\n华北,10\n华东,20\n", encoding="utf-8")
            second.write_text("地区,销售额\n华北,100\n华东,200\n", encoding="utf-8")
            documents = [
                document("一月.csv", profile_path(first, max_rows=100, max_bytes=1024 * 1024)),
                document("二月.csv", profile_path(second, max_rows=100, max_bytes=1024 * 1024)),
            ]

            result = answer_question("销售额合计", documents)

        self.assertEqual(result["value"], 330)
        self.assertEqual(result["aggregation_scope"]["participating_source_count"], 2)

    def test_projected_archive_profile_omissions_make_answer_incomplete(self):
        payload = {
            "data_profiles_total": 40,
            "data_profiles": [{
                "member": "visible.csv",
                "profile": profile(2, {
                    "销售额": numeric(30, 2, mean=15, minimum=10, maximum=20),
                }),
            }],
        }
        result = answer_question("销售额合计", [{"path": "bundle.zip", "payload": payload}])
        self.assertEqual(result["value"], 30)
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["omitted_projected_profiles"], 39)
        self.assertEqual(result["aggregation_scope"]["omitted_projected_profile_count"], 39)


if __name__ == "__main__":
    unittest.main()
