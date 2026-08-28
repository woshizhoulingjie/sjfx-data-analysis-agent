import tempfile
import unittest
from pathlib import Path

from services.storage import Storage


def _scan(root):
    return {
        "root": str(root),
        "file_count": 1,
        "directory_count": 0,
        "total_size": 1,
        "tree": {
            "kind": "directory",
            "name": "root",
            "path": ".",
            "file_count": 1,
            "children": [
                {"kind": "file", "name": "a.txt", "path": "a.txt", "size": 1}
            ],
        },
    }


class StorageLifecycleTests(unittest.TestCase):
    def test_structured_documents_are_filtered_in_sql_by_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = Storage(root / "state.db", root / "sidecars")
            profile = {
                "status": "completed",
                "row_count": 1,
                "columns": {"amount": {"inferred_type": "number", "count": 1}},
            }
            storage.save_document(
                "scan-filter", "tables/a.json", {"data_profile": profile}
            )
            storage.save_document(
                "scan-filter", "tables/b.json", {"data_profile": profile}
            )

            items = list(storage.iter_structured_documents(
                "scan-filter", source_paths=["tables/b.json"]
            ))

            self.assertEqual([item["path"] for item in items], ["tables/b.json"])

    def test_delete_scan_cascades_database_sidecars_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "outputs"
            output.mkdir()
            storage = Storage(root / "state.db", root / "sidecars", 32 * 1024)
            storage.save_scan(_scan(root), scan_id="scan-delete", owner_id="owner")
            storage.save_document(
                "scan-delete",
                "a.txt",
                {"source": {"path": "a.txt"}, "text": "x" * 40000, "evidence": []},
            )
            artifact = output / "result.zip"
            artifact.write_bytes(b"result")
            storage.save_artifact(
                artifact.name, "owner", scan_id="scan-delete", kind="test"
            )
            with storage._connect() as conn:
                conn.execute(
                    "INSERT INTO summaries(scan_id,node_path,summary_type,payload) "
                    "VALUES (?,?,?,?)",
                    ("scan-delete", "a.txt", "file", "{}"),
                )
                conn.execute(
                    "INSERT INTO conversations(id,scan_id,owner_id,payload) "
                    "VALUES (?,?,?,?)",
                    ("session-1", "scan-delete", "owner", "{}"),
                )
                conn.execute(
                    "INSERT INTO conversation_turns("
                    "id,session_id,scan_id,owner_id,sequence,status,stage,question,scope,"
                    "user_message_id,assistant_message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "turn-1", "session-1", "scan-delete", "owner", 1,
                        "completed", "completed", "q", "{}", "u-1", "a-1",
                    ),
                )
                conn.execute(
                    "INSERT INTO conversation_analysis_steps("
                    "turn_id,step_id,position,tool,action,status,payload) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("turn-1", "step-1", 1, "retrieve", "search", "completed", "{}"),
                )

            result = storage.delete_scan(
                "scan-delete", owner_id="owner", output_dir=output
            )
            self.assertTrue(result["deleted"])
            self.assertFalse(artifact.exists())
            self.assertFalse((root / "sidecars" / "scan-delete").exists())
            with storage._connect() as conn:
                for table in (
                    "scans", "summaries", "unified_documents", "output_artifacts",
                    "conversations", "conversation_turns", "conversation_analysis_steps",
                ):
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0],
                        0,
                        table,
                    )

    def test_delete_scan_rejects_active_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = Storage(root / "state.db", root / "sidecars")
            storage.save_scan(_scan(root), scan_id="active", owner_id="owner")
            storage.create_job("active", owner_id="owner")
            with self.assertRaises(RuntimeError):
                storage.delete_scan("active", owner_id="owner", output_dir=root)
            self.assertTrue(storage.scan_owned("active", "owner"))

    def test_retention_is_dry_run_by_default_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = Storage(root / "state.db", root / "sidecars")
            for index in range(3):
                storage.save_scan(
                    _scan(root), scan_id="scan-{}".format(index), owner_id="owner"
                )
                with storage._connect() as conn:
                    conn.execute(
                        "UPDATE scans SET created_at=? WHERE id=?",
                        ("2020-01-0{} 00:00:00".format(index + 1), "scan-{}".format(index)),
                    )
            plan = storage.cleanup_history(
                owner_id="owner", max_scans=1, dry_run=True
            )
            self.assertEqual(len(plan["candidate_scan_ids"]), 2)
            self.assertEqual(len([item for item in range(3) if storage.scan_owned("scan-{}".format(item), "owner")]), 3)
            applied = storage.cleanup_history(
                owner_id="owner", max_scans=1, dry_run=False, output_dir=root
            )
            self.assertEqual(len(applied["deleted"]), 2)
            self.assertEqual(len([item for item in range(3) if storage.scan_owned("scan-{}".format(item), "owner")]), 1)


if __name__ == "__main__":
    unittest.main()
