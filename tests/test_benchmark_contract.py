import argparse
import contextlib
import importlib.util
import io
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = PROJECT_ROOT / "scripts" / "benchmark_package.py"
SPEC = importlib.util.spec_from_file_location("sjfx_benchmark_package", BENCHMARK_PATH)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def contract_args(profile="smoke"):
    return argparse.Namespace(
        expected_size_gib=1,
        size_tolerance_percent=10.0,
        profile=profile,
        max_rss_percent=70.0,
        min_disk_free_percent=20.0,
        max_inventory_seconds=0.0,
        max_analysis_seconds=0.0,
        max_end_to_end_seconds=0.0,
        min_parse_success_percent=95.0,
        ten_gib_boundary_kind=None,
    )


def passing_result():
    ten_gib = 10 * benchmark.GIB
    return {
        "input": {"independent_measurement": {
            "logical_bytes": benchmark.GIB,
            "largest_file_bytes": benchmark.GIB,
            "sparse_file_count": 0,
        }},
        "runtime_limits": {
            "max_single_file_bytes": ten_gib,
            "max_archive_file_bytes": ten_gib,
            "max_archive_member_bytes": ten_gib,
            "max_archive_uncompressed_bytes": ten_gib,
            "max_export_bytes": ten_gib,
        },
        "inventory_contract": {"complete": True},
        "coverage": {
            "inventory_coverage": {"complete": True},
            "parse_coverage": {
                "inventory_files": 2,
                "parsed_files": 2,
                "failed_files": 0,
                "pending_files": 0,
            },
            "semantic_analysis_coverage": {
                "complete": False,
                "analyzed_files": 2,
                "full_text_files": 0,
                "projected_or_partial_files": 2,
            },
        },
        "hooks": {
            "retrieval": {"status": "not_requested"},
            "recovery": {"status": "not_requested"},
            "export": {"status": "not_requested"},
        },
        "environment": {"memory_total_bytes": 1000},
        "resources": {"rss_peak_bytes": 500},
        "disk": {"after": {"work": {"free_ratio": 0.25}}},
        "stages": {},
        "timing_seconds": {"end_to_end": 1.0},
    }


class BenchmarkContractTests(unittest.TestCase):
    def test_independent_measurement_uses_real_file_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.txt").write_bytes(b"abc")
            nested = root / "nested"
            nested.mkdir()
            (nested / "b.csv").write_bytes(b"12345")
            (root / ".env").write_bytes(b"secret")
            measured = benchmark.measure_real_input(root)

        self.assertEqual(measured["files"], 3)
        self.assertEqual(measured["logical_bytes"], 14)
        self.assertEqual(measured["directories"], 2)
        self.assertEqual(measured["types"], {".csv": 1, ".txt": 1, "[no-extension]": 1})

    def test_independent_measurement_identifies_regular_and_archive_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ordinary.bin").write_bytes(b"12345")
            (root / "boundary.zip").write_bytes(b"1234567")
            measured = benchmark.measure_real_input(root)

        self.assertEqual(measured["largest_regular_file_path"], "ordinary.bin")
        self.assertEqual(measured["largest_regular_file_bytes"], 5)
        self.assertEqual(measured["largest_archive_file_path"], "boundary.zip")
        self.assertEqual(measured["largest_archive_file_bytes"], 7)

    def test_inventory_contract_rejects_fabricated_scanner_total(self):
        real = {"files": 2, "directories": 1, "symlink_count": 0, "logical_bytes": 8, "errors": []}
        scan = {
            "file_count": 2,
            "scanned_directory_count": 1,
            "symlink_count": 0,
            "total_size": 10 * benchmark.GIB,
            "truncated": False,
            "errors": [],
            "depth_limited_directory_count": 0,
        }

        compared = benchmark.compare_inventory(real, scan)

        self.assertFalse(compared["complete"])
        self.assertEqual(compared["actual_bytes"], 8)
        self.assertEqual(compared["enumerated_bytes"], 10 * benchmark.GIB)

    def test_inventory_contract_detects_an_ignored_directory(self):
        real = {"files": 1, "directories": 2, "symlink_count": 0, "logical_bytes": 8, "errors": []}
        scan = {
            "file_count": 1,
            "scanned_directory_count": 1,
            "symlink_count": 0,
            "total_size": 8,
            "truncated": False,
            "errors": [],
            "depth_limited_directory_count": 0,
        }

        compared = benchmark.compare_inventory(real, scan)

        self.assertFalse(compared["complete"])
        self.assertEqual(compared["actual_directories"], 2)
        self.assertEqual(compared["enumerated_directories"], 1)

    def test_inventory_contract_compares_symlink_entries_without_following_targets(self):
        real = {"files": 1, "directories": 1, "symlink_count": 1, "logical_bytes": 8, "errors": []}
        scan = {
            "file_count": 1,
            "scanned_directory_count": 1,
            "symlink_count": 0,
            "total_size": 8,
            "truncated": False,
            "errors": [],
            "depth_limited_directory_count": 0,
        }

        compared = benchmark.compare_inventory(real, scan)

        self.assertFalse(compared["complete"])
        self.assertEqual(compared["actual_symlinks"], 1)
        self.assertEqual(compared["enumerated_symlinks"], 0)

    def test_smoke_contract_requires_inventory_and_terminal_states_not_optional_hooks(self):
        contract = benchmark.evaluate_contract(passing_result(), contract_args("smoke"))

        self.assertTrue(contract["passed"])
        hook_checks = [item for item in contract["checks"] if item["name"].endswith("_hook")]
        self.assertTrue(hook_checks)
        self.assertTrue(all(not item["required"] for item in hook_checks))

    def test_acceptance_contract_requires_all_three_hooks(self):
        result = passing_result()
        failed = benchmark.evaluate_contract(result, contract_args("acceptance"))
        self.assertFalse(failed["passed"])
        self.assertEqual(
            set(failed["failed_required_checks"]),
            {"retrieval_hook", "recovery_hook", "export_hook"},
        )

        for hook in result["hooks"].values():
            hook["status"] = "passed"
        passed = benchmark.evaluate_contract(result, contract_args("acceptance"))
        self.assertTrue(passed["passed"])

    def test_terminal_states_do_not_hide_low_parse_or_semantic_coverage(self):
        result = passing_result()
        result["coverage"]["parse_coverage"].update({"parsed_files": 1, "failed_files": 1})
        result["coverage"]["semantic_analysis_coverage"].update({
            "analyzed_files": 1,
            "projected_or_partial_files": 1,
        })

        contract = benchmark.evaluate_contract(result, contract_args("smoke"))

        self.assertFalse(contract["passed"])
        self.assertIn("parse_success_ratio", contract["failed_required_checks"])

    def test_ten_gib_acceptance_requires_an_exact_declared_boundary_object(self):
        args = contract_args("acceptance")
        args.expected_size_gib = 10
        result = passing_result()
        result["input"]["independent_measurement"]["logical_bytes"] = 10 * benchmark.GIB
        result["input"]["independent_measurement"]["largest_file_bytes"] = 10 * benchmark.GIB
        for hook in result["hooks"].values():
            hook["status"] = "passed"

        missing = benchmark.evaluate_contract(result, args)
        self.assertFalse(missing["passed"])
        self.assertIn("ten_gib_boundary_kind_declared", missing["failed_required_checks"])

        args.ten_gib_boundary_kind = "archive"
        result["boundary_evidence"] = {
            "kind": "archive",
            "path": "boundary.zip",
            "object_bytes": 10 * benchmark.GIB,
            "analysis_status": "completed",
        }
        passed = benchmark.evaluate_contract(result, args)
        self.assertTrue(passed["passed"])

    def test_missing_three_level_coverage_is_a_failure(self):
        result = passing_result()
        del result["coverage"]["semantic_analysis_coverage"]

        contract = benchmark.evaluate_contract(result, contract_args())

        self.assertFalse(contract["passed"])
        self.assertIn("three_coverage_contracts_present", contract["failed_required_checks"])

    def test_resource_sampler_produces_cpu_and_rss_contract(self):
        sampler = benchmark.ResourceSampler(interval=0.01).start()
        deadline = time.perf_counter() + 0.03
        while time.perf_counter() < deadline:
            sum(index * index for index in range(100))
        metrics = sampler.stop()

        self.assertGreaterEqual(metrics["sample_count"], 2)
        self.assertGreaterEqual(metrics["cpu_seconds"], 0)
        self.assertIn("rss_peak_bytes", metrics)

    def test_cli_is_fixed_to_real_1_5_10_gib_matrix(self):
        parser = benchmark.build_argument_parser()
        for size in (1, 5, 10):
            parsed = parser.parse_args(["fixture", "--expected-size-gib", str(size)])
            self.assertEqual(parsed.expected_size_gib, size)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["fixture", "--expected-size-gib", "3"])


if __name__ == "__main__":
    unittest.main()
