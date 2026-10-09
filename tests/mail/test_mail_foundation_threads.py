"""Header graph, migration, provenance, correction and publication contracts."""
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.mail.conversations import apply_correction, detail, history, list_conversations, rebuild
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.repository import MailRepository
from services.mail.schema import DDL, DDL_V3, FTS_DDL, FTS_FALLBACK_DDL, migrate
from services.mail.threading import rebuild_relationships


class _Storage:
    """Real SQLite with repository's production transaction/locking contract."""
    def __init__(self, path):
        self.db_path = str(path)
        self.lock = threading.Lock()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _mail(mid, *, reply=None, references=None, subject="Shared", body="body", date="Thu, 10 Sep 2026 08:00:00 +0000", attachment=False):
    message = EmailMessage()
    message["From"] = "Alice <alice@example.com>"
    message["To"] = "bob@example.com, carol@example.com"
    message["Cc"] = "bob@example.com"
    message["Subject"] = subject
    message["Message-ID"] = "<{}@example.com>".format(mid)
    if date:
        message["Date"] = date
    if reply:
        message["In-Reply-To"] = reply if "<" in reply else "<{}@example.com>".format(reply)
    if references:
        message["References"] = references
    message.set_content(body)
    if attachment:
        message.add_attachment(b"attachment", maintype="application", subtype="octet-stream", filename="a.txt")
    return message.as_bytes()


class FoundationThreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repository = MailRepository(_Storage(self.root / "mail.db"))
        self.repository.migrate()
        self.package = self.make_package("owner")
        self.pid = self.package["package_id"]
        self.n = 0
        self.switch = patch.object(Config, "MAIL_ENABLE_CONVERSATIONS", True)
        self.switch.start()

    def tearDown(self):
        self.switch.stop()
        self.tmp.cleanup()

    def test_current_conversations_read_during_other_writer(self):
        self.ingest(_mail('concurrent'))
        rebuild(self.repository,self.pid,'owner')
        db=self.repository.storage.db_path
        setup=sqlite3.connect(db)
        try:setup.execute('PRAGMA journal_mode=WAL')
        finally:setup.close()
        writer=sqlite3.connect(db)
        try:
            writer.execute('BEGIN IMMEDIATE')
            self.assertFalse(rebuild_relationships(self.repository,self.pid,'owner')['changed'])
            self.assertFalse(rebuild(self.repository,self.pid,'owner')['changed'])
            self.assertEqual(len(list_conversations(self.repository,self.pid,'owner')),1)
        finally:
            writer.rollback();writer.close()

    def make_package(self, owner):
        return self.repository.create_package(owner_id=owner, display_name="test", source_kind="directory", source_path=str(self.root), source_revision="r1", config={})[0]

    def ingest(self, raw, package=None):
        package = package or self.package
        member = MailMember("mail{}.eml".format(self.n), self.n, len(raw), {"kind": "file", "path": "unused"})
        self.n += 1
        return self.repository.upsert_ingested_message(package_id=package["package_id"], source={"locator_type": "directory", "root_path": str(self.root), "source_revision": "r1"}, member=member, raw=raw, parsed=parse_eml(raw))["message_key"]

    def threads(self):
        return self.repository.threads(self.pid, "owner")

    def test_v4_migration_preserves_historical_checksums_and_content(self):
        self.assertEqual(hashlib.sha256((DDL + FTS_DDL + FTS_FALLBACK_DDL).encode()).hexdigest(), "98c1ba3e4b7d4a3c282b954f92dd902e6bcc0f0e04ffaff8ca4a82826b6bfc6e")
        self.assertEqual(hashlib.sha256(DDL_V3.encode()).hexdigest(), "ba6b4f3ace4b8fd5e8e99951b72c5ca623ddb647974143a2dfa791acf688f422")
        connection = sqlite3.connect(":memory:")
        connection.executescript(DDL + FTS_DDL + DDL_V3)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES('keep')")
        connection.executemany("INSERT INTO mail_schema_migrations(version,checksum) VALUES(?,?)", [(2, hashlib.sha256((DDL + FTS_DDL + FTS_FALLBACK_DDL).encode()).hexdigest()), (3, hashlib.sha256(DDL_V3.encode()).hexdigest())])
        self.assertTrue(migrate(connection))
        self.assertFalse(migrate(connection))
        self.assertEqual(connection.execute("SELECT value FROM unrelated").fetchone()[0], "keep")
        self.assertIn("cell_range", {row[1] for row in connection.execute("PRAGMA table_info(mail_text_segments)")})
        self.assertIn("next_retry_at", {row[1] for row in connection.execute("PRAGMA table_info(mail_part_processing)")})
        self.assertTrue({"cell_range", "payload_sha256", "text_sha256"}.issubset({row[1] for row in connection.execute("PRAGMA table_info(mail_evidence)")}))
        connection.close()

    def test_branches_subject_change_and_append_keep_conversation_id(self):
        parent = self.ingest(_mail("parent"))
        first = list_conversations(self.repository, self.pid, "owner")[0]["conversation_id"]
        a = self.ingest(_mail("a", reply="parent", subject="Changed subject"))
        b = self.ingest(_mail("b", reply="parent"))
        rows = list_conversations(self.repository, self.pid, "owner")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["conversation_id"], first)
        value = detail(self.repository, first, "owner")
        self.assertEqual({edge["source_message_key"] for edge in value["tree_edges"] if edge["target_message_key"] == parent}, {a, b})
        self.assertEqual({item["message_key"]: item["depth"] for item in value["messages"]}, {parent: 0, a: 1, b: 1})
        self.assertEqual(len(history(self.repository, self.pid, "owner")["generations"]), 2)

    def test_references_are_context_and_fallback_parent_is_inferred(self):
        oldest = self.ingest(_mail("oldest"))
        recent = self.ingest(_mail("recent", subject="Other"))
        child = self.ingest(_mail("child", reply="recent", references="<oldest@example.com> <recent@example.com>"))
        rows = list_conversations(self.repository, self.pid, "owner")
        self.assertEqual(sorted(row["message_count"] for row in rows), [1, 2])
        edges = self.threads()
        self.assertTrue(all(edge["status"] == "context" for edge in edges if edge["relation_type"] == "reference"))
        self.assertFalse(any(edge["relation_type"] == "reference_parent" and edge["source_message_key"] == child for edge in edges))
        fallback = self.ingest(_mail("fallback", references="<oldest@example.com>"))
        list_conversations(self.repository, self.pid, "owner")
        inferred = [edge for edge in self.threads() if edge["source_message_key"] == fallback and edge["relation_type"] == "reference_parent"]
        self.assertEqual(inferred[0]["status"], "inferred")
        self.assertEqual(inferred[0]["target_message_key"], oldest)
        self.assertNotEqual(oldest, recent)

    def test_missing_shared_parent_resolves_without_duplicate_members(self):
        a = self.ingest(_mail("a", reply="absent"))
        b = self.ingest(_mail("b", reply="absent"))
        row = list_conversations(self.repository, self.pid, "owner")[0]
        cid = row["conversation_id"]
        value = detail(self.repository, cid, "owner")
        self.assertEqual(row["message_count"], 2)
        self.assertEqual(len([node for node in value["tree_nodes"] if node["missing"]]), 1)
        parent_raw = _mail("absent")
        parent = self.ingest(parent_raw)
        self.ingest(parent_raw)
        row = list_conversations(self.repository, self.pid, "owner")[0]
        self.assertEqual(row["conversation_id"], cid)
        self.assertEqual(row["message_count"], 3)
        value = detail(self.repository, cid, "owner")
        self.assertFalse(any(node["missing"] for node in value["tree_nodes"]))
        self.assertEqual({edge["target_message_key"] for edge in value["tree_edges"]}, {parent})
        self.assertEqual({item["message_key"] for item in value["messages"]}, {a, b, parent})

    def test_duplicate_id_multiple_parent_self_and_cycle_never_pick_parent(self):
        self.ingest(_mail("duplicate", body="one"))
        self.ingest(_mail("duplicate", body="two"))
        ambiguous = self.ingest(_mail("ambiguous", reply="duplicate"))
        self.ingest(_mail("p1"))
        self.ingest(_mail("p2"))
        multiple = self.ingest(_mail("multiple", reply="<p1@example.com> <p2@example.com>"))
        self_key = self.ingest(_mail("self", reply="self"))
        cycle_a = self.ingest(_mail("cyclea", reply="cycleb"))
        cycle_b = self.ingest(_mail("cycleb", reply="cyclea"))
        rows = list_conversations(self.repository, self.pid, "owner")
        self.assertTrue(all(row["message_count"] == 1 for row in rows))
        edges = [edge for edge in self.threads() if edge["relation_type"] == "reply_to"]
        self.assertEqual({edge["status"] for edge in edges if edge["source_message_key"] == ambiguous}, {"ambiguous"})
        self.assertTrue(all(edge["target_message_key"] is None for edge in edges if edge["source_message_key"] in {ambiguous, multiple}))
        self.assertEqual({edge["status"] for edge in edges if edge["source_message_key"] == self_key}, {"self_reference"})
        self.assertEqual({edge["status"] for edge in edges if edge["source_message_key"] in {cycle_a, cycle_b}}, {"cycle"})
        with self.repository._connection() as connection:
            kinds = {row[0] for row in connection.execute("SELECT anomaly_type FROM mail_thread_anomalies")}
        self.assertTrue({"duplicate_message_id", "multiple_parent", "self_reference", "cycle"}.issubset(kinds))

    def test_corrections_merge_split_revoke_are_audited_and_headers_unchanged(self):
        a = self.ingest(_mail("a"))
        b = self.ingest(_mail("b"))
        with self.repository._connection() as connection:
            headers = dict(connection.execute("SELECT message_key,headers_json FROM mail_messages"))
        merge = apply_correction(self.repository, self.pid, "owner", action="merge", message_keys=[a, b], reason="confirmed group")
        self.assertEqual(len(list_conversations(self.repository, self.pid, "owner")), 1)
        split = apply_correction(self.repository, self.pid, "owner", action="split", groups=[[a], [b]])
        self.assertEqual(len(list_conversations(self.repository, self.pid, "owner")), 2)
        apply_correction(self.repository, self.pid, "owner", action="revoke", correction_id=split["correction_id"])
        self.assertEqual(len(list_conversations(self.repository, self.pid, "owner")), 1)
        apply_correction(self.repository, self.pid, "owner", action="revoke", correction_id=merge["correction_id"])
        self.assertEqual(len(list_conversations(self.repository, self.pid, "owner")), 2)
        audit = history(self.repository, self.pid, "owner")["corrections"]
        self.assertEqual(len(audit), 4)
        self.assertEqual(sum(row["revoked"] for row in audit), 2)
        with self.repository._connection() as connection:
            self.assertEqual(dict(connection.execute("SELECT message_key,headers_json FROM mail_messages")), headers)

    def test_filters_counts_timeline_and_missing_dates_are_consistent(self):
        parent = self.ingest(_mail("p", body="plain", date=None))
        child = self.ingest(_mail("c", reply="p", body="needle", attachment=True))
        cid = list_conversations(self.repository, self.pid, "owner")[0]["conversation_id"]
        filters = {"q": "needle", "address": "bob@example.com", "has_attachment": True, "date_from": "2026-09-01", "date_to": "2026-10-01"}
        rows = list_conversations(self.repository, self.pid, "owner", **filters)
        value = detail(self.repository, cid, "owner", **filters)
        self.assertEqual(rows[0]["message_count"], len(value["messages"]))
        self.assertEqual([item["message_key"] for item in value["timeline"]], [child])
        full = detail(self.repository, cid, "owner")
        self.assertEqual(full["timeline"][-1]["message_key"], parent)
        self.assertEqual(full["timeline"][-1]["date_status"], "missing")
        self.assertEqual(value["total_message_count"], 2)

    def test_split_is_reapplied_to_later_descendants_and_can_be_revoked(self):
        parent = self.ingest(_mail("p"))
        branch = self.ingest(_mail("branch", reply="p"))
        split = apply_correction(self.repository, self.pid, "owner", action="split", groups=[[parent], [branch]])
        new_reply = self.ingest(_mail("new", reply="branch"))
        rows = list_conversations(self.repository, self.pid, "owner")
        self.assertEqual(sorted(row["message_count"] for row in rows), [1, 2])
        branch_conversation = next(row for row in rows if row["message_count"] == 2)
        value = detail(self.repository, branch_conversation["conversation_id"], "owner")
        self.assertEqual({message["message_key"] for message in value["messages"]}, {branch, new_reply})
        apply_correction(self.repository, self.pid, "owner", action="revoke", correction_id=split["correction_id"])
        self.assertEqual([row["message_count"] for row in list_conversations(self.repository, self.pid, "owner")], [3])

    def test_date_only_upper_bound_includes_that_day_and_filename_query_is_shared(self):
        self.ingest(_mail("p", attachment=True))
        rows = list_conversations(self.repository, self.pid, "owner", date_from="2026-09-10", date_to="2026-09-10", q="a.txt")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_count"], 1)
        value = detail(self.repository, rows[0]["conversation_id"], "owner", date_from="2026-09-10", date_to="2026-09-10", q="a.txt")
        self.assertEqual(len(value["messages"]), 1)

    def test_body_forwarding_does_not_create_outer_reply_edges(self):
        self.ingest(_mail("p"))
        outer = self.ingest(_mail("outer", body="Forwarded message\nMessage-ID: <p@example.com>\nIn-Reply-To: <p@example.com>\nquoted body"))
        list_conversations(self.repository, self.pid, "owner")
        self.assertFalse(any(edge["source_message_key"] == outer and edge["relation_type"] != "subject" for edge in self.threads()))

    def test_failed_correction_publication_does_not_leave_an_audit_side_effect(self):
        a = self.ingest(_mail("a"))
        b = self.ingest(_mail("b"))
        before = list_conversations(self.repository, self.pid, "owner")
        with self.repository._connection() as connection:
            connection.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON mail_conversations BEGIN SELECT RAISE(ABORT,'test publication failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            apply_correction(self.repository, self.pid, "owner", action="merge", message_keys=[a, b])
        with self.repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mail_conversation_corrections").fetchone()[0], 0)
            connection.execute("DROP TRIGGER fail_publish")
        self.assertEqual(list_conversations(self.repository, self.pid, "owner"), before)

    def test_package_scope_rejects_cross_owner_corrections_and_reply_resolution(self):
        other = self.make_package("other")
        outsider = self.ingest(_mail("outside"), other)
        child = self.ingest(_mail("child", reply="outside"))
        list_conversations(self.repository, self.pid, "owner")
        edge = [edge for edge in self.threads() if edge["source_message_key"] == child][0]
        self.assertEqual(edge["status"], "missing_target")
        self.assertIsNone(edge["target_message_key"])
        with self.assertRaises(ValueError):
            apply_correction(self.repository, self.pid, "other", action="merge", message_keys=[child, outsider])
        with self.assertRaises(ValueError):
            apply_correction(self.repository, self.pid, "owner", action="merge", message_keys=[child, outsider])

    def test_rebuild_failure_rolls_back_published_generation_and_rows(self):
        self.ingest(_mail("p"))
        before = list_conversations(self.repository, self.pid, "owner")
        self.ingest(_mail("c", reply="p"))
        with self.repository._connection() as connection:
            connection.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON mail_conversations BEGIN SELECT RAISE(ABORT,'test publication failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            rebuild(self.repository, self.pid, "owner")
        with self.repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT version FROM mail_foundation_state WHERE package_id=? AND stage='conversations'", (self.pid,)).fetchone()[0], before[0]["generation_id"])
            self.assertEqual(connection.execute("SELECT message_count FROM mail_conversations WHERE package_id=?", (self.pid,)).fetchone()[0], 1)
            connection.execute("DROP TRIGGER fail_publish")
        self.assertEqual(list_conversations(self.repository, self.pid, "owner")[0]["message_count"], 2)

    def test_repeated_and_concurrent_rebuilds_are_idempotent_and_delivery_counts_differ(self):
        raw = _mail("p")
        self.ingest(raw)
        self.ingest(raw)
        before = list_conversations(self.repository, self.pid, "owner")
        errors = []
        def run():
            try:
                rebuild(self.repository, self.pid, "owner")
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=run) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        after = list_conversations(self.repository, self.pid, "owner")
        self.assertEqual(after, before)
        self.assertEqual(len(history(self.repository, self.pid, "owner")["generations"]), 1)
        logical = self.repository.communication(self.pid, "owner", count_basis="logical")["items"]
        occurrences = self.repository.communication(self.pid, "owner", count_basis="occurrence")["items"]
        self.assertTrue(all(item["message_count"] == 1 for item in logical))
        self.assertTrue(all(item["message_count"] == 2 for item in occurrences))


if __name__ == "__main__":
    unittest.main()
