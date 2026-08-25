import tempfile
import unittest
from pathlib import Path

from services.storage import Storage
from services.retrieval import retrieve_evidence
from services.translation import document_translation_fingerprint


class EngineeringV2StorageTests(unittest.TestCase):
    def test_large_translation_uses_sidecar_and_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db", sidecar_threshold=32 * 1024)
            payload = {
                "schema_version": "document-translation/1.0",
                "source_fingerprint": "fingerprint",
                "source_path": "foreign.txt",
                "source_language": "en",
                "provider_id": "local",
                "status": "completed",
                "original_text": "source " * 20000,
                "translated_text": "译文" * 50000,
                "units": [{
                    "unit_id": "U1", "kind": "body", "status": "completed",
                    "source_text": "source", "target_text": "译文", "qa": {"passed": True},
                }],
                "progress": {"required_units": 1, "completed_units": 1},
            }
            storage.save_translation("scan", "foreign.txt", payload)
            projected = storage.get_translation("scan", "foreign.txt", hydrate=False)
            self.assertTrue(projected["sidecar_projection"])
            self.assertNotIn("original_text", projected)
            self.assertNotIn("translated_text", projected)
            hydrated = storage.get_translation("scan", "foreign.txt", hydrate=True)
            self.assertEqual(hydrated["translated_text"], payload["translated_text"])
            self.assertEqual(storage.translation_counts("scan"), {"completed": 1})

    def test_translation_memory_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            storage.save_translation_memory("tm:1", {"target_text": "译文", "qa": {"passed": True}})
            self.assertEqual(storage.get_translation_memory("tm:1")["target_text"], "译文")

    def test_completed_translation_is_searchable_in_both_languages(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            payload = {
                "source_fingerprint": "source-v1", "source_language": "en",
                "target_language": "zh-CN", "provider_id": "local",
                "status": "completed",
                "units": [{
                    "unit_id": "U1", "kind": "body", "block_kind": "paragraph",
                    "section": "Decision", "start": 0, "end": 39,
                    "paragraph_index": 2, "status": "completed",
                    "source_language": "en",
                    "source_text": "Alice approved the revised delivery plan.",
                    "target_text": "爱丽丝批准了修订后的交付计划。",
                }],
            }
            storage.save_translation("scan", "letters/decision.txt", payload)

            chinese = storage.search_evidence_index("scan", "交付计划", limit=50)
            english = storage.search_evidence_index("scan", "approved delivery", limit=50)
            self.assertEqual(chinese[0]["translated_text"], "爱丽丝批准了修订后的交付计划。")
            self.assertEqual(english[0]["original_text"], "Alice approved the revised delivery plan.")
            ranked = retrieve_evidence({}, "交付计划", indexed_chunks=chinese)
            self.assertEqual(ranked["results"][0]["source_path"], "letters/decision.txt")
            self.assertEqual(ranked["results"][0]["paragraph_index"], 2)

    def test_partial_translation_publishes_completed_units_and_reparse_clears_index(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            completed = {
                "source_fingerprint": "source-v1", "source_language": "en",
                "status": "completed", "units": [{
                    "unit_id": "U1", "kind": "body", "status": "completed",
                    "source_text": "The unique decision was approved.",
                    "target_text": "这项独特决定已获批准。",
                }],
            }
            storage.save_translation("scan", "decision.txt", completed)
            self.assertTrue(storage.search_evidence_index("scan", "独特决定", limit=50))

            partial = dict(completed)
            partial["status"] = "partial"
            storage.save_translation("scan", "decision.txt", partial)
            partial_hits = storage.search_evidence_index("scan", "独特决定", limit=50)
            self.assertEqual(len(partial_hits), 1)
            self.assertEqual(partial_hits[0]["translated_text"], "这项独特决定已获批准。")

            storage.save_translation("scan", "decision.txt", completed)
            storage.replace_document_evidence_index("scan", "decision.txt", [])
            self.assertFalse(storage.search_evidence_index("scan", "独特决定", limit=50))

    def test_unchanged_document_preserves_completed_translation(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            document = {
                "source": {"path": "decision.txt", "name": "decision.txt", "sha256": "v1"},
                "structure": {"title": "Decision"},
                "text": "The delivery plan was approved.",
                "content_sha256": "content-v1",
            }
            translation = {
                "source_fingerprint": document_translation_fingerprint(document),
                "source_language": "en", "status": "completed",
                "units": [{
                    "unit_id": "U1", "kind": "body", "status": "completed",
                    "source_text": document["text"],
                    "target_text": "交付计划已获批准。",
                }],
            }
            storage.save_document("scan", "decision.txt", document)
            storage.save_translation("scan", "decision.txt", translation)

            storage.save_document("scan", "decision.txt", dict(document))

            self.assertEqual(
                storage.get_translation("scan", "decision.txt")["status"], "completed"
            )
            self.assertTrue(storage.search_evidence_index("scan", "交付计划", limit=50))

    def test_changed_document_withdraws_translation_and_chinese_index(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(
                Path(folder) / "state.db",
                sidecar_dir=Path(folder) / "sidecars",
                sidecar_threshold=32 * 1024,
            )
            document = {
                "source": {"path": "decision.txt", "name": "decision.txt", "sha256": "v1"},
                "structure": {"title": "Decision"},
                "text": "The delivery plan was approved.",
                "content_sha256": "content-v1",
            }
            translation = {
                "source_fingerprint": document_translation_fingerprint(document),
                "source_language": "en", "status": "completed",
                "original_text": "source " * 20000,
                "translated_text": "译文" * 50000,
                "units": [{
                    "unit_id": "U1", "kind": "body", "status": "completed",
                    "source_text": document["text"],
                    "target_text": "交付计划已获批准。",
                }],
            }
            storage.save_document("scan", "decision.txt", document)
            storage.save_translation("scan", "decision.txt", translation)
            sidecar_files = list((Path(folder) / "sidecars").rglob("*.json.gz"))
            self.assertEqual(len(sidecar_files), 1)

            changed = dict(document)
            changed["source"] = dict(document["source"], sha256="v2")
            changed["text"] = "The delivery plan was rejected."
            changed["content_sha256"] = "content-v2"
            storage.save_document("scan", "decision.txt", changed)

            self.assertIsNone(storage.get_translation("scan", "decision.txt"))
            self.assertFalse(storage.search_evidence_index("scan", "交付计划", limit=50))
            self.assertFalse(sidecar_files[0].exists())

    def test_batch_save_invalidates_only_changed_document_translations(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            documents = {
                "stable.txt": {
                    "source": {"path": "stable.txt", "name": "stable.txt", "sha256": "stable-v1"},
                    "structure": {"title": "Stable"}, "text": "Stable source text.",
                },
                "changed.txt": {
                    "source": {"path": "changed.txt", "name": "changed.txt", "sha256": "changed-v1"},
                    "structure": {"title": "Changed"}, "text": "Old source text.",
                },
            }
            storage.save_documents("scan", list(documents.items()))
            for index, (path, document) in enumerate(documents.items(), 1):
                storage.save_translation("scan", path, {
                    "source_fingerprint": document_translation_fingerprint(document),
                    "source_language": "en", "status": "completed",
                    "units": [{
                        "unit_id": "U{}".format(index), "kind": "body",
                        "status": "completed", "source_text": document["text"],
                        "target_text": "稳定译文" if path == "stable.txt" else "绝密撤回方案",
                    }],
                })

            changed = dict(documents["changed.txt"])
            changed["source"] = dict(changed["source"], sha256="changed-v2")
            changed["text"] = "New source text."
            storage.save_documents("scan", [
                ("stable.txt", dict(documents["stable.txt"])),
                ("changed.txt", changed),
            ])

            self.assertIsNotNone(storage.get_translation("scan", "stable.txt"))
            self.assertIsNone(storage.get_translation("scan", "changed.txt"))
            self.assertTrue(storage.search_evidence_index("scan", "稳定译文", limit=50))
            stale_hits = storage.search_evidence_index("scan", "绝密撤回方案", limit=50)
            self.assertFalse(any(item.get("source_path") == "changed.txt" for item in stale_hits))

    def test_failed_reparse_deletes_stale_translation_and_index(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            document = {
                "source": {"path": "failed.txt", "name": "failed.txt", "sha256": "v1"},
                "structure": {"title": "Failed"}, "text": "Original evidence.",
            }
            storage.save_document("scan", "failed.txt", document)
            storage.save_translation("scan", "failed.txt", {
                "source_fingerprint": document_translation_fingerprint(document),
                "source_language": "en", "status": "completed",
                "units": [{
                    "unit_id": "U1", "kind": "body", "status": "completed",
                    "source_text": "Original evidence.", "target_text": "过期机密证据。",
                }],
            })
            self.assertTrue(storage.search_evidence_index("scan", "过期机密", limit=20))

            storage.delete_document("scan", "failed.txt")

            self.assertIsNone(storage.get_translation("scan", "failed.txt"))
            stale = storage.search_evidence_index("scan", "过期机密", limit=20)
            self.assertFalse(any(item.get("source_path") == "failed.txt" for item in stale))

    def test_conversation_is_owner_scoped_and_messages_are_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            session = {
                "session_id": "session-1", "scan_id": "scan-1", "title": "调查",
                "scope": {"kind": "package"},
                "messages": [{"message_id": "m1", "role": "user", "content": "讲了什么"}],
            }
            storage.save_conversation(session, "owner-a")
            storage.save_conversation(session, "owner-a")
            self.assertIsNotNone(storage.get_conversation("session-1", "owner-a", scan_id="scan-1"))
            self.assertIsNone(storage.get_conversation("session-1", "owner-b", scan_id="scan-1"))
            self.assertEqual(storage.list_conversations("scan-1", "owner-a")[0]["message_count"], 1)


if __name__ == "__main__":
    unittest.main()
