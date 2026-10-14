import csv
import json
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from services.structured_profile import _kind, _missing, _number, _profile_rows, profile_path


class StructuredProfileNumericTests(unittest.TestCase):
    def profile_values(self, values, name="amount"):
        rows = [{name: value} for value in values]
        profile = _profile_rows(rows, {name: values}, len(rows), 0)
        # Every result must be usable by a strict JSON consumer.
        json.dumps(profile, allow_nan=False)
        return profile

    def assert_public_profile(self, profile, missing_count):
        self.assertEqual(profile["status"], "completed")
        self.assertTrue(profile["coverage"]["complete"])
        column = profile["columns"]["amount"]
        self.assertEqual(column["type_counts"], {"number": 6, "missing": missing_count})
        self.assertEqual(column["missing_count"], missing_count)
        self.assertEqual(column["missing_ratio"], round(missing_count / profile["row_count"], 6))
        self.assertEqual(column["count"], 6)
        self.assertEqual(column["sum"], 21)
        self.assertEqual(column["mean"], 3.5)
        self.assertEqual(column["median"], 3.5)
        self.assertEqual((column["min"], column["max"]), (1, 6))
        self.assertEqual(column["unique_count"], 6)
        self.assertEqual(sum(column["distribution"].values()), 6)
        self.assertEqual(profile["columns"]["category"]["type_counts"], {"text": profile["row_count"]})
        self.assertEqual(profile["columns"]["category"]["top_values"], [{"value": "standard", "count": profile["row_count"]}])
        json.dumps(profile, allow_nan=False)

    def test_nonfinite_values_are_consistently_missing(self):
        values = [
            float("nan"), float("inf"), -float("inf"),
            "NaN", " +nAn ", "-NaN", "inf", "+INF", "-Inf",
            "Infinity", "+Infinity", " -INFINITY ", "1e999", "-1e999",
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(_missing(value))
                self.assertEqual(_kind(value), "missing")
                self.assertIsNone(_number(value))
        column = self.profile_values(values)["columns"]["amount"]
        self.assertEqual(column["type_counts"], {"missing": len(values)})
        self.assertEqual(column["missing_count"], len(values))
        self.assertEqual(column["sample_values"], [])
        self.assertEqual(column["unique_count"], 0)
        for statistic in ("sum", "mean", "min", "max", "median", "count", "distribution"):
            self.assertNotIn(statistic, column)

    def test_original_nan_crash_is_fixed(self):
        column = self.profile_values([1, 2, 3, 4, "NaN"])["columns"]["amount"]
        self.assertEqual(column["type_counts"], {"number": 4, "missing": 1})
        self.assertEqual(column["sum"], 10)
        self.assertEqual(column["mean"], 2.5)
        self.assertEqual(column["median"], 2.5)
        self.assertEqual(column["sample_values"], ["1", "2", "3", "4"])

    def test_median_uses_sorted_central_value_or_pair(self):
        for values, expected in [
            ([1, 3], 2), ([9, 1, 3, 2], 2.5), ([8, 1, 3], 3),
            ([-10, -2], -6), ([0], 0), ([2, 2, 2, 2], 2),
        ]:
            with self.subTest(values=values):
                self.assertEqual(self.profile_values(values)["columns"]["amount"]["median"], expected)

    def test_ordinary_number_date_text_and_boolean_behavior_is_preserved(self):
        values = ["1,200", -2.5, "0", "3e2"]
        column = self.profile_values(values)["columns"]["amount"]
        self.assertEqual(column["inferred_type"], "number")
        self.assertEqual(column["type_counts"], {"number": 4})
        self.assertEqual(column["sum"], 1497.5)
        self.assertEqual(column["mean"], 374.375)
        self.assertEqual(column["median"], 150)
        self.assertEqual(column["outlier_count"], 0)
        self.assertNotIn("numeric_warnings", column)
        for value, kind in [(True, "text"), (False, "text"), ("hello", "text"), ("2026-10-10", "datetime"), (datetime(2026, 10, 10), "datetime")]:
            with self.subTest(value=value):
                self.assertFalse(_missing(value))
                self.assertEqual(_kind(value), kind)
        for value in [None, "", "null", "none", "n/a", "na", "-"]:
            with self.subTest(value=value):
                self.assertTrue(_missing(value))
                self.assertIsNone(_number(value))

    def test_existing_five_bucket_and_outlier_calculation_is_preserved(self):
        column = self.profile_values([0, 10, 20, 30, 1000])["columns"]["amount"]
        self.assertEqual(column["distribution"], {"1-5": 4, "5-5": 1})
        self.assertEqual(column["outlier_count"], 1)

    def test_nonfinite_values_do_not_enter_entity_statistics(self):
        profile = self.profile_values(["Alice", "Bob", float("nan"), float("inf"), "-Infinity"], name="person_name")
        self.assertEqual(profile["entity_statistics"]["person"]["distinct_count"], 2)
        self.assertEqual(profile["entity_statistics"]["person"]["top_values"], [{"value": "Alice", "count": 1}, {"value": "Bob", "count": 1}])

    def test_extreme_finite_total_has_explicit_warning_and_finite_mean_median(self):
        for sign in (1, -1):
            with self.subTest(sign=sign):
                column = self.profile_values([sign * 1e308] * 6)["columns"]["amount"]
                self.assertEqual(column["count"], 6)
                self.assertEqual(column["missing_count"], 0)
                self.assertNotIn("sum", column)
                self.assertEqual(column["numeric_warnings"], ["sum_out_of_range"])
                self.assertEqual(column["mean"], sign * 1e308)
                self.assertEqual(column["median"], sign * 1e308)
                self.assertEqual(column["distribution"], {"1-5": 6})

    def test_intermediate_sum_overflow_can_recover_finite_total(self):
        column = self.profile_values([1e308, 1e308, -1e308, -1e308])["columns"]["amount"]
        self.assertEqual(column["sum"], 0)
        self.assertEqual(column["mean"], 0)
        self.assertEqual(column["median"], 0)
        self.assertNotIn("numeric_warnings", column)

    def test_distribution_handles_overflowing_range_and_underflowing_width(self):
        column = self.profile_values([-1e308, -5e307, 0, 5e307, 1e308])["columns"]["amount"]
        self.assertEqual(column["distribution"], {"1-5": 1, "2-5": 1, "3-5": 1, "4-5": 1, "5-5": 1})
        self.assertTrue(math.isfinite(column["mean"]))
        tiny = self.profile_values([0, 0, 0, 0, 5e-324])["columns"]["amount"]
        self.assertEqual(tiny["distribution"], {"1-5": 4, "5-5": 1})

    def test_csv_and_tsv_public_entries(self):
        values = [1, 2, 3, 4, 5, 6, "NaN", "inf", "-Infinity", "1e999", None]
        with tempfile.TemporaryDirectory() as folder:
            for suffix, delimiter in [(".csv", ","), (".tsv", "\t")]:
                with self.subTest(suffix=suffix):
                    path = Path(folder) / ("numeric" + suffix)
                    with path.open("w", newline="", encoding="utf-8") as stream:
                        writer = csv.writer(stream, delimiter=delimiter)
                        writer.writerow(["amount", "category"])
                        writer.writerows((value, "standard") for value in values)
                    self.assert_public_profile(profile_path(path), 5)

    def test_json_and_jsonl_public_entries(self):
        values = [1, 2, 3, 4, 5, 6, "NaN", "inf", "-Infinity", "1e999", None]
        rows = [{"amount": value, "category": "standard"} for value in values]
        with tempfile.TemporaryDirectory() as folder:
            for suffix in (".json", ".jsonl"):
                with self.subTest(suffix=suffix):
                    path = Path(folder) / ("numeric" + suffix)
                    content = json.dumps(rows) if suffix == ".json" else "\n".join(json.dumps(row) for row in rows)
                    path.write_text(content, encoding="utf-8")
                    self.assert_public_profile(profile_path(path), 5)

    def test_json_native_nonfinite_numbers_are_excluded(self):
        values = [1, 2, 3, 4, 5, 6, float("nan"), float("inf"), -float("inf")]
        rows = [{"amount": value, "category": "standard"} for value in values]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "native.json"
            path.write_text(json.dumps(rows), encoding="utf-8")
            self.assert_public_profile(profile_path(path), 3)

    def test_xlsx_public_entry(self):
        try:
            from openpyxl import Workbook
        except ImportError:
            self.skipTest("openpyxl unavailable")
        values = [1, 2, 3, 4, 5, 6, "NaN", "inf", "-Infinity", "1e999", None]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "numeric.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.append(["amount", "category"])
            for value in values:
                sheet.append([value, "standard"])
            book.save(path)
            book.close()
            self.assert_public_profile(profile_path(path), 5)


if __name__ == "__main__":
    unittest.main()
