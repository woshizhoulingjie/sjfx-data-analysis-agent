import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_import(code, state_dir, **overrides):
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT),
            "SJFX_STATE_DIR": str(state_dir),
            "SJFX_PARSE_TEMP_DIR": str(state_dir.parent / "parse-temp"),
            "HOST": "127.0.0.1",
            "AUTH_REQUIRED": "0",
            "ENABLE_API_DOCS": "0",
            "ENABLE_TRANSLATION": "0",
            "ENABLE_IMPORT_TRANSLATION": "0",
        }
    )
    env.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )


class StartupContractTests(unittest.TestCase):
    def test_import_app_does_not_create_or_initialize_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "not-created"
            result = _run_import(
                "import app; print(app.storage.initialized); print(app.app.docs_url)",
                state_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("False\nNone", result.stdout)
            self.assertFalse(state_dir.exists())

    def test_external_listener_cannot_disable_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            result = _run_import(
                "import config",
                state_dir,
                HOST="0.0.0.0",
                AUTH_REQUIRED="0",
                SCAN_ALLOWED_ROOTS=tmp,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("AUTH_REQUIRED", result.stderr)

    def test_api_docs_are_only_enabled_for_explicit_local_development(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            result = _run_import(
                "import app; print(app.app.docs_url, app.app.openapi_url)",
                state_dir,
                ENABLE_API_DOCS="1",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("/docs /openapi.json", result.stdout)


if __name__ == "__main__":
    unittest.main()
