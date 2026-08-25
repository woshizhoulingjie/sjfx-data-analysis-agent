"""Cross-module regression tests for the engineering-v2 workflows.

These tests intentionally exercise durable state transitions that are easy to
miss in isolated service tests: duplicate discovery from real previews,
translation pagination after a file is promoted, archive-member coverage, and
the public API job-id/freshness contracts.  No test talks to Ollama.
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.conversation import RetrievalRequest
from services.package_exploration import build_content_map, preview_file
from services.package_analysis import _explore_large_package
from services.package_overview import build_package_overview_from_storage
from services.scanner import scan_directory
from services.storage import Storage
from services.translation import ProviderResponse, TranslationProvider, TranslationService


HAS_WEB_RUNTIME = (
    importlib.util.find_spec("fastapi") is not None
    and importlib.util.find_spec("docx") is not None
)


def _inventory_files(scan):
    stack = [scan["tree"]]
    files = []
    while stack:
        node = stack.pop()
        if node.get("kind") == "file":
            files.append(node)
        stack.extend(node.get("children") or [])
    return sorted(files, key=lambda item: item["path"])


class _VerifiedChineseProvider(TranslationProvider):
    @property
    def provider_id(self):
        return "test:verified-chinese"

    def translate(self, text, source_language, target_language, glossary=None,
                  timeout=None, retries=0):
        # Keep protected tokens intact so the production QA path is exercised.
        return ProviderResponse("这是经过验证的中文译文。" + str(text or ""), model="test")


class DurableStorageIntegrationTests(unittest.TestCase):
    def test_real_identical_files_form_one_duplicate_candidate_group(self):
        """A content fingerprint must not include path or modification time."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            content = "Identical package evidence. " * 400
            first = root / "first.txt"
            second = root / "nested" / "second.txt"
            second.parent.mkdir()
            first.write_text(content, encoding="utf-8")
            second.write_text(content, encoding="utf-8")
            # Force different source metadata as happens with copied files.
            os.utime(first, (1_700_000_000, 1_700_000_000))
            os.utime(second, (1_710_000_000, 1_710_000_000))

            scan = scan_directory(root)
            previews = [
                preview_file(root, node, per_file_bytes=4096)
                for node in _inventory_files(scan)
            ]
            content_map = build_content_map(previews, representative_limit=2)

            groups = [
                set(group.get("paths") or [])
                for group in content_map.get("duplicates") or []
            ]
            self.assertIn({"first.txt", "nested/second.txt"}, groups)

    def test_translation_sidecar_and_conversation_owner_are_durable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            storage = Storage(
                root / "state.db", root / "sidecars", sidecar_threshold=32 * 1024
            )
            translation = {
                "source_fingerprint": "source-v1",
                "status": "completed",
                "source_language": "en",
                "provider_id": "test",
                "original_text": "A" * 40_000,
                "translated_text": "中" * 40_000,
                "units": [],
            }
            storage.save_translation("scan-1", "letter.txt", translation)
            projected = storage.get_translation("scan-1", "letter.txt", hydrate=False)
            hydrated = storage.get_translation("scan-1", "letter.txt", hydrate=True)
            self.assertTrue(projected["sidecar_projection"])
            self.assertNotIn("original_text", projected)
            self.assertEqual(hydrated["translated_text"], translation["translated_text"])

            session = {
                "session_id": "session-1", "scan_id": "scan-1",
                "title": "资料问答", "scope": {"kind": "package"},
                "messages": [],
            }
            storage.save_conversation(session, "owner-a")
            self.assertIsNotNone(storage.get_conversation("session-1", "owner-a"))
            self.assertIsNone(storage.get_conversation("session-1", "owner-b"))

    def test_exploration_resume_uses_lightweight_preview_states(self):
        """Resume metadata must not hydrate every stored preview body.

        One full preview stream is still allowed after resume to build the
        content map incrementally.  The initial checkpoint lookup must use the
        payload-free ``iter_file_preview_states`` contract and must not reopen
        files whose size/mtime checkpoint is still valid.
        """
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(4):
                (root / "{}.txt".format(index)).write_text(
                    "bounded preview source {} ".format(index) * 100,
                    encoding="utf-8",
                )
            scan = scan_directory(root)
            storage = Storage(root / "state.db", root / "sidecars")
            scan_id = storage.save_scan(scan)
            for node in _inventory_files(scan):
                preview = preview_file(root, node, per_file_bytes=1024)
                # Make accidental all-payload hydration materially visible;
                # this field is irrelevant to resume validation.
                preview["preview_text"] += "X" * 40_000
                storage.save_file_preview(scan_id, node["path"], preview)

            self.assertTrue(
                hasattr(storage, "iter_file_preview_states"),
                "Storage must expose a payload-free preview checkpoint iterator",
            )
            states = list(storage.iter_file_preview_states(scan_id, batch_size=2))
            self.assertEqual(len(states), 4)
            for state in states:
                self.assertNotIn("payload", state)
                self.assertNotIn("preview_text", state)
                self.assertIn("path", state)
                self.assertIn("size", state)
                self.assertIn("modified_at_ns", state)

            original_full_iterator = storage.iter_file_previews
            with patch.object(
                storage, "iter_file_previews", wraps=original_full_iterator
            ) as full_iterator, patch(
                "services.package_analysis.preview_file",
                side_effect=AssertionError("valid preview checkpoint was reopened"),
            ):
                content_map = _explore_large_package(
                    scan_id,
                    scan,
                    _inventory_files(scan),
                    storage,
                    {
                        "preview_total_bytes": 1024 * 1024,
                        "preview_bytes_per_file": 1024,
                        "preview_zip_members": 8,
                        "preview_zip_member_bytes": 256,
                        "initial_parse_files": 2,
                        "batch_files": 2,
                    },
                )

            self.assertEqual(content_map["run"]["reused_previews"], 4)
            self.assertLessEqual(
                full_iterator.call_count,
                1,
                "resume loaded the full preview stream before the content-map pass",
            )

    def test_package_overview_second_read_uses_persistent_snapshot(self):
        """A repeated overview read must survive a new Storage process.

        Opening a second Storage instance rules out an in-process memoization
        shortcut.  Once the first call has materialized the snapshot, none of
        the expensive scan/document/analysis readers may run again.
        """
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "letter.txt").write_text(
                "A sufficiently long English package letter.", encoding="utf-8"
            )
            scan = scan_directory(root)
            db_path = root / "state.db"
            sidecars = root / "sidecars"
            first_storage = Storage(db_path, sidecars)
            scan_id = first_storage.save_scan(scan)
            first_storage.save_document(scan_id, "letter.txt", {
                "source": {
                    "path": "letter.txt", "name": "letter.txt",
                    "extension": ".txt", "size": 43,
                },
                "classification": {"document_type": "信件"},
                "language": {"code": "en", "name": "英文"},
                "text": "A sufficiently long English package letter.",
                "evidence": [],
            })
            first_storage.save_analysis(scan_id, {
                "scan_id": scan_id,
                "topic_clusters": [{
                    "topic": "correspondence", "members": ["letter.txt"],
                }],
                "exact_duplicate_groups": [],
            })

            first = build_package_overview_from_storage(first_storage, scan_id)
            self.assertEqual(first["package"]["file_count"], 1)

            second_storage = Storage(db_path, sidecars)
            with patch.object(
                second_storage, "get_scan",
                side_effect=AssertionError("cached overview reloaded full scan"),
            ), patch.object(
                second_storage, "iter_documents",
                side_effect=AssertionError("cached overview traversed documents"),
            ), patch.object(
                second_storage, "get_analysis",
                side_effect=AssertionError("cached overview reloaded full analysis"),
            ):
                second = build_package_overview_from_storage(second_storage, scan_id)

            self.assertEqual(second, first)


@unittest.skipUnless(HAS_WEB_RUNTIME, "本机未安装正式 FastAPI/docx 运行依赖")
class WebWorkflowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app_module = app_module

    def setUp(self):
        self._original_storage = self.app_module.storage
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.storage = Storage(self.root / "state.db", self.root / "sidecars")
        self.app_module.storage = self.storage

    def tearDown(self):
        self.app_module.storage = self._original_storage
        self._temporary.cleanup()

    def _save_scan_with_files(self, names):
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Foreign evidence for {}.".format(name), encoding="utf-8")
        scan = scan_directory(self.root)
        scan_id = self.storage.save_scan(
            scan, owner_id=self.app_module.Config.OWNER_ID
        )
        return scan_id, scan

    def test_translation_api_returns_scalar_job_id(self):
        scan_id, _scan = self._save_scan_with_files(["letter.txt"])
        with self.app_module.app.test_request_context(
            "/api/translation/{}".format(scan_id),
            method="POST",
            json={"path": "letter.txt", "require_full": True},
        ):
            response, status = self.app_module.document_translation(scan_id)
        payload = response.get_json()
        self.assertEqual(status, 202)
        self.assertIsInstance(payload["job_id"], str)
        self.assertTrue(payload["job_id"])

        with self.app_module.app.test_request_context(
            "/api/translate-package/{}".format(scan_id),
            method="POST",
            json={"phase": "preview_and_priority"},
        ):
            response, status = self.app_module.translate_package(scan_id)
        payload = response.get_json()
        self.assertEqual(status, 202)
        self.assertIsInstance(payload["job_id"], str)

    def test_deep_backfill_continuations_do_not_skip_a_shrinking_candidate_set(self):
        scan_id, scan = self._save_scan_with_files(
            ["{}.txt".format(index) for index in range(5)]
        )
        self.storage.save_analysis(scan_id, {
            "scan_id": scan_id,
            "policy": {"large_package": {"enabled": True}},
            "coverage": {"inventory_files": 5},
        })
        for node in _inventory_files(scan):
            path = node["path"]
            preview = {
                "path": path, "name": Path(path).name, "extension": ".txt",
                "status": "previewed", "sample_sha256": path,
                "language": {"code": "en"}, "document_type": "文本",
                "preview_text": "Foreign preview for {}".format(path),
                "coverage": {"preview_only": True, "parse_complete": False},
            }
            self.storage.save_file_preview(scan_id, path, preview)
            self.storage.set_file_state(
                scan_id, path, "preview:" + path, "previewed", document={}
            )

        first_id = self.storage.create_job(
            scan_id,
            task_type="translate_package",
            options={"phase": "deep_backfill", "cursor": 0},
            owner_id=self.app_module.Config.OWNER_ID,
        )
        promoted = []

        def promote(_scan_id, _scan, node_path, _job_id):
            promoted.append(node_path)
            self.storage.set_file_state(
                scan_id, node_path, "full:" + node_path, "completed",
                document={"text": "full"},
            )
            return {"text": "full"}

        def translate(_scan_id, node_path, source_level, job_id=None, max_units=None):
            return {
                "status": "completed", "source_level": source_level,
                "full_translation": source_level == "full",
            }

        job_id = first_id
        with patch.object(
            self.app_module.Config, "TRANSLATION_PACKAGE_BATCH_FILES", 2
        ), patch.object(
            self.app_module, "_promote_for_translation", side_effect=promote
        ), patch.object(
            self.app_module, "_translate_one_document", side_effect=translate
        ), patch.object(
            self.app_module, "refresh_package_coverage"
        ):
            for _iteration in range(10):
                job = self.storage.get_job(job_id)
                result = self.app_module._run_claimed_translation_job(job)
                continuation = result.get("continuation_job_id")
                if isinstance(continuation, (list, tuple)):
                    continuation = continuation[0]
                if not continuation:
                    break
                job_id = continuation
            else:
                self.fail("deep_backfill continuation did not terminate")

        self.assertEqual(set(promoted), {
            "0.txt", "1.txt", "2.txt", "3.txt", "4.txt",
        })

    def test_archive_member_evidence_uses_container_state_for_coverage(self):
        scan_id, _scan = self._save_scan_with_files(["mail.zip"])
        self.storage.save_analysis(scan_id, {
            "scan_id": scan_id,
            "coverage": {"inventory_files": 1, "deep_analyzed_files": 1},
        })
        self.storage.set_file_state(
            scan_id, "mail.zip", "full", "completed", document={"text": "full"}
        )
        self.storage.replace_evidence_index(scan_id, [{
            "evidence_id": "EV-archive",
            "source_path": "mail.zip::letters/one.txt",
            "archive_source_path": "mail.zip",
            "archive_member": "letters/one.txt",
            "label": "paragraph",
            "text": "London correspondence contains decisive archive evidence.",
        }])
        result = self.app_module._conversation_retrieve(RetrievalRequest(
            scan_id=scan_id,
            query="London correspondence",
            scope=self.app_module.ConversationScope(),
            top_k=5,
        ))
        self.assertTrue(result.get("results"))
        self.assertFalse(result["coverage"]["deferred_candidates"])
        self.assertFalse(result["needs_promotion"])

    def test_promotion_worker_continues_conversation_once(self):
        scan_id, scan = self._save_scan_with_files(["decision.txt"])
        self.storage.set_file_state(
            scan_id, "decision.txt", "full", "completed", document={"text": "full"}
        )
        self.storage.replace_evidence_index(scan_id, [{
            "evidence_id": "EV-decision", "source_path": "decision.txt",
            "section": "Decision", "label": "paragraph",
            "text": "Alice approved the revised delivery plan on 2026-08-20.",
        }])
        session = {
            "session_id": "conversation-1", "scan_id": scan_id,
            "title": "审批问答", "scope": {"kind": "package"},
            "messages": [
                {"message_id": "user-1", "role": "user", "content": "谁批准了交付计划？"},
                {"message_id": "assistant-1", "role": "assistant", "content": "正在补充深析", "metadata": {}},
            ],
        }
        owner = self.app_module.Config.OWNER_ID
        self.storage.save_conversation(session, owner)
        job = {
            "id": "promotion-job", "scan_id": scan_id, "owner_id": owner,
            "options": {
                "target_paths": ["decision.txt"],
                "conversation_session_id": "conversation-1",
                "conversation_question": "谁批准了交付计划？",
                "conversation_scope": {"kind": "package"},
                "conversation_trigger_message_id": "assistant-1",
                "conversation_continuation_depth": 0,
            },
        }
        analysis = {"coverage": {"inventory_files": 1, "deep_analyzed_files": 1}}
        first = self.app_module._continue_conversation_after_promotion(job, analysis, scan)
        self.assertEqual(first["status"], "continued")
        stored = self.storage.get_conversation("conversation-1", owner, scan_id=scan_id)
        continuation_messages = [
            item for item in stored["messages"]
            if (item.get("metadata") or {}).get("automatic_continuation_of") == "assistant-1"
        ]
        self.assertEqual(len(continuation_messages), 1)

        second = self.app_module._continue_conversation_after_promotion(job, analysis, scan)
        self.assertEqual(second["status"], "already_continued")
        stored_again = self.storage.get_conversation("conversation-1", owner, scan_id=scan_id)
        self.assertEqual(len(stored_again["messages"]), len(stored["messages"]))

    def test_changed_document_never_serves_a_completed_stale_translation(self):
        scan_id, _scan = self._save_scan_with_files(["letter.txt"])
        original = {
            "source": {"path": "letter.txt", "name": "letter.txt", "sha256": "old"},
            "structure": {"title": "Old letter"},
            "text": "This is the old English document body.",
            "content_sha256": "old-content",
            "evidence": [],
        }
        state = TranslationService(provider=_VerifiedChineseProvider()).translate_document(original)
        self.assertEqual(state["status"], "completed")
        self.storage.save_document(scan_id, "letter.txt", original)
        self.storage.save_translation(scan_id, "letter.txt", state)

        changed = dict(original)
        changed["source"] = dict(original["source"], sha256="new")
        changed["text"] = "This is a materially changed English document body."
        changed["content_sha256"] = "new-content"
        self.storage.save_document(scan_id, "letter.txt", changed)

        with self.app_module.app.test_request_context(
            "/api/translation/{}?path=letter.txt&view=translated".format(scan_id)
        ):
            response = self.app_module.document_translation(scan_id)
        if isinstance(response, tuple):
            response = response[0]
        payload = response.get_json()
        self.assertNotEqual(payload.get("status"), "completed")
        translated = (payload.get("translated") or {}).get("text")
        self.assertFalse(translated)


if __name__ == "__main__":
    unittest.main()
