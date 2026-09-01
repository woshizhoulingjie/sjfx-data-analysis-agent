import tempfile
import time
import unittest
from pathlib import Path

from services.storage import Storage
from services.retrieval import retrieve_evidence
from services.translation import document_translation_fingerprint


class EngineeringV2StorageTests(unittest.TestCase):
    def test_large_preview_uses_compressed_sidecar(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(
                Path(folder) / "state.db",
                sidecar_dir=Path(folder) / "sidecars",
            )
            preview = {
                "schema_version": "file-preview/1.3", "path": "large.txt",
                "status": "previewed", "size": 100000, "modified_at_ns": 1,
                "preview_text": "compressible preview " * 5000,
                "preview_windows": [], "language": {"code": "en"},
                "coverage": {"preview_only": True, "parse_complete": False},
            }
            storage.save_file_preview("scan", "large.txt", preview)
            with storage._connect() as conn:
                stored = conn.execute(
                    "SELECT payload FROM file_previews WHERE scan_id='scan' AND node_path='large.txt'"
                ).fetchone()["payload"]
            self.assertIn("__preview_sidecar__", stored)
            self.assertLess(len(stored), 1000)
            self.assertEqual(
                storage.get_file_preview("scan", "large.txt")["preview_text"],
                preview["preview_text"],
            )

    def test_file_failure_state_tracks_retry_policy_and_attempt_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            for _attempt in range(2):
                storage.set_file_state(
                    "scan", "busy.pdf", "fingerprint", "failed",
                    error="parser timeout", error_class="transient_runtime",
                    retryable=True, next_retry_at=time.time() + 30,
                )
            state = storage.get_file_state("scan", "busy.pdf")
            self.assertEqual(state["attempt_count"], 2)
            self.assertEqual(state["error_class"], "transient_runtime")
            self.assertTrue(state["retryable"])
            self.assertGreater(state["next_retry_at"], time.time())

    def test_deferred_job_is_not_claimed_before_available_at(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            job_id = storage.create_job(
                "scan", task_type="analyze_package",
                options={"workflow_source": "background_backfill"},
                owner_id="owner",
            )
            claimed = storage.claim_next_job("worker-1")
            self.assertEqual(claimed["id"], job_id)
            self.assertTrue(storage.defer_running_job(job_id, "资源不足", delay_seconds=60))
            deferred = storage.get_job(job_id)
            self.assertGreater(deferred["available_at"], time.time())
            self.assertIsNone(storage.claim_next_job("worker-2"))

    def test_file_workflow_state_round_trip_paging_and_stage_update(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            saved = storage.save_file_workflow_states("scan", [{
                "path": "reports/a.pdf", "workflow_state": "priority_queued",
                "selection_state": "priority", "score": 82.5,
                "score_components": {"evidence_potential": 15.0},
                "reasons": ["主题代表"], "safety_status": "checked",
                "light_index_status": "ready", "language_code": "en",
                "ocr_candidate": True, "promotion_allowed": True,
            }, {
                "path": "cache.tmp", "workflow_state": "excluded",
                "selection_state": "excluded", "score": 2.0,
                "reasons": ["cache_temporary_or_dependency_file"],
                "safety_status": "checked", "light_index_status": "ready",
                "promotion_allowed": False,
            }])
            self.assertEqual(saved, 2)
            page = storage.list_file_workflow_states_page("scan", limit=1)
            self.assertEqual(page["total"], 2)
            self.assertEqual(page["items"][0]["node_path"], "reports/a.pdf")
            self.assertEqual(page["next_offset"], 1)
            self.assertTrue(page["items"][0]["ocr_candidate"])
            self.assertEqual(
                page["items"][0]["score_components"]["evidence_potential"], 15.0
            )

            self.assertTrue(storage.update_file_workflow_stage(
                "scan", "reports/a.pdf", "evidence_ready",
                parse_status="completed", evidence_status="ready",
                priority_source="question_promotion",
            ))
            state = storage.get_file_workflow_state("scan", "reports/a.pdf")
            self.assertEqual(state["workflow_state"], "evidence_ready")
            self.assertEqual(state["parse_status"], "completed")
            self.assertEqual(state["priority_source"], "question_promotion")
            self.assertEqual(storage.file_workflow_counts("scan")["evidence_ready"], 1)

    def test_file_status_projection_keeps_retry_and_logical_container_honest(self):
        """The operational UI must not infer completion from one table alone."""
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            storage.save_file_workflow_states("scan", [{
                "path": "complete.txt", "promotion_allowed": True,
                "safety_status": "checked", "parse_status": "completed",
            }, {
                "path": "waiting.txt", "promotion_allowed": True,
                "safety_status": "checked",
            }, {
                "path": "outside.mp4", "promotion_allowed": False,
                "safety_status": "checked", "reasons": ["out_of_scope_media"],
            }, {
                "path": "bundle.zip", "workflow_state": "logical_container",
                "promotion_allowed": False,
                "reasons": ["logical_container_replaced_by_children"],
            }])
            storage.set_file_state(
                "scan", "complete.txt", "complete", "completed",
                document={"text": "verified source"},
            )
            storage.set_file_state(
                "scan", "waiting.txt", "waiting", "failed",
                error="temporary parser timeout", retryable=True,
                next_retry_at=time.time() + 60,
            )

            page = storage.list_file_status_page("scan", limit=20)
            states = {item["path"]: item for item in page["items"]}
            self.assertEqual(states["complete.txt"]["display_status"], "completed")
            self.assertEqual(states["waiting.txt"]["display_status"], "retry_waiting")
            self.assertEqual(states["outside.mp4"]["display_status"], "out_of_scope")
            self.assertEqual(states["bundle.zip"]["display_status"], "partial")
            self.assertEqual(states["bundle.zip"]["accounting_role"], "container_only")
            self.assertEqual(
                storage.list_file_status_page("scan", status="retry_waiting")["total"], 1
            )
            counts = storage.file_status_counts("scan", include_container_only=False)
            self.assertEqual(counts["completed"], 1)
            self.assertEqual(counts["retry_waiting"], 1)
            self.assertEqual(counts["container_only"], 1)
            # The ZIP physical container remains inspectable but must not be
            # counted as a separate incomplete logical file beside its members.
            self.assertEqual(counts["total"], 3)
            all_rows = storage.file_status_counts("scan")
            self.assertEqual(all_rows["total"], 4)
            self.assertGreater(counts["incomplete"], 0)

    def test_manual_file_reanalysis_resets_retry_budget_without_touching_completed(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            storage.save_file_workflow_states("scan", [{
                "path": "failed.txt", "promotion_allowed": True,
                "safety_status": "checked",
            }, {
                "path": "complete.txt", "promotion_allowed": True,
                "safety_status": "checked",
            }])
            for _ in range(3):
                storage.set_file_state(
                    "scan", "failed.txt", "failed", "failed", error="timeout",
                    retryable=False,
                )
            storage.set_file_state(
                "scan", "complete.txt", "complete", "completed",
                document={"text": "done"},
            )
            self.assertEqual(
                storage.request_file_reanalysis("scan", ["failed.txt", "complete.txt"]), 1
            )
            failed = storage.get_file_state("scan", "failed.txt")
            completed = storage.get_file_state("scan", "complete.txt")
            self.assertEqual(failed["attempt_count"], 0)
            self.assertTrue(failed["retryable"])
            self.assertEqual(failed["next_retry_at"], 0)
            self.assertEqual(completed["status"], "completed")
            workflow = storage.get_file_workflow_state("scan", "failed.txt")
            self.assertEqual(workflow["workflow_state"], "manual_retry_queued")

    def test_analysis_job_priority_and_scope_dedup_follow_workflow_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "state.db")
            sources = [
                "question_promotion", "manual_selection", "initial_overview",
                "background_backfill",
            ]
            jobs = {}
            for source in sources:
                job_id, created = storage.create_or_get_typed_job(
                    "scan", "analyze_package",
                    options={"workflow_source": source, "target_paths": [source + ".txt"]},
                    owner_id="owner",
                )
                self.assertTrue(created)
                jobs[source] = storage.get_job(job_id)
            translation_id = storage.create_job(
                "scan", task_type="translate_package", owner_id="owner",
            )
            priorities = [jobs[source]["priority"] for source in sources]
            priorities.append(storage.get_job(translation_id)["priority"])
            self.assertEqual(priorities, [130, 110, 85, 20, 10])

            duplicate_id, created = storage.create_or_get_typed_job(
                "scan", "analyze_package",
                options={
                    "workflow_source": "background_backfill",
                    "target_paths": ["background_backfill.txt"],
                    "scope_label": "后台覆盖扩展",
                },
                owner_id="owner",
            )
            self.assertFalse(created)
            self.assertEqual(duplicate_id, jobs["background_backfill"]["id"])

            promoted_id, created = storage.create_or_get_typed_job(
                "scan", "analyze_package",
                options={
                    "workflow_source": "question_promotion",
                    "conversation_session_id": "conversation-1",
                    "target_paths": ["background_backfill.txt"],
                },
                owner_id="owner",
            )
            self.assertTrue(created)
            self.assertNotEqual(promoted_id, duplicate_id)
            self.assertEqual(storage.get_job(promoted_id)["priority"], 130)

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
