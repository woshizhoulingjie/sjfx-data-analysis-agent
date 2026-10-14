"""Split install entry points keep application roots and honor engine markers."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packaging.requirements import Requirement
from scripts.generate_requirements_lock import PROJECT_ROOT, _root_requirements


class RequirementsSplitTests(unittest.TestCase):
    def test_windows_entry_contains_application_roots_and_no_engine(self):
        names = {item.name for item in _root_requirements(PROJECT_ROOT/"requirements-windows.txt")}
        self.assertIn("fastapi", names)
        self.assertIn("torch", names)
        self.assertIn("pythainlp", names)
        self.assertIn("extract-msg", names)
        self.assertIn("cryptography", names)
        self.assertNotIn("vllm", names)

    def test_default_entry_separates_application_and_engine_on_both_platforms(self):
        for marker_matches in (True, False):
            with self.subTest(marker_matches=marker_matches), patch(
                "packaging.markers.Marker.evaluate", return_value=marker_matches
            ):
                names = {item.name for item in _root_requirements(PROJECT_ROOT/"requirements.txt")}
            self.assertIn("fastapi", names)
            self.assertIn("extract-msg", names)
            self.assertIn("cryptography", names)
            self.assertNotIn("vllm", names)

    def test_engine_entry_retains_linux_marker_without_application_roots(self):
        with patch("packaging.markers.Marker.evaluate", return_value=True):
            names = {item.name for item in _root_requirements(PROJECT_ROOT/"requirements-vllm.txt")}
        self.assertEqual(names, {"vllm"})
        with patch("packaging.markers.Marker.evaluate", return_value=False):
            names = {item.name for item in _root_requirements(PROJECT_ROOT/"requirements-vllm.txt")}
        self.assertEqual(names, set())

    def test_nested_relative_includes_and_cycles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/"nested").mkdir()
            (root/"roots.txt").write_text("--requirement nested/app.txt\n", encoding="utf-8")
            (root/"nested/app.txt").write_text("httpx>=0.27,<1\n", encoding="utf-8")
            self.assertEqual(str(_root_requirements(root/"roots.txt")[0]), str(Requirement("httpx>=0.27,<1")))
            (root/"nested/app.txt").write_text("-r ../roots.txt\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                _root_requirements(root/"roots.txt")
