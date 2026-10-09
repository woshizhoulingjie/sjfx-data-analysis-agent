"""Append-only v5 migration and retirement of legacy automatic nested mail."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.mail.attachments import read_message_raw
from services.mail.foundation import backfill_slice
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.repository import MailRepository
from services.mail.schema import DDL, DDL_V3, DDL_V4, DDL_V4_COLUMNS, DDL_V5, FTS_DDL, FTS_FALLBACK_DDL, _apply_v4, migrate
from tests.mail.test_mail_foundation_threads import _Storage


V4_CHECKSUM = "5f1ff29871e82045a4d4068cffb9ba6e23966b9a3bb68070742039f4e7c7ca1a"


def _v4_database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(DDL + FTS_DDL + DDL_V3)
    _apply_v4(connection)
    sources = ((2, DDL + FTS_DDL + FTS_FALLBACK_DDL), (3, DDL_V3), (4, DDL_V4 + DDL_V4_COLUMNS))
    connection.executemany("INSERT INTO mail_schema_migrations(version,checksum) VALUES(?,?)", [(version, hashlib.sha256(source.encode()).hexdigest()) for version, source in sources])
    connection.commit()
    return connection


class RetirementMigrationTests(unittest.TestCase):
    def test_v5_preserves_v2_v3_v4_ledgers_and_all_occurrences_default_countable(self):
        self.assertEqual(hashlib.sha256((DDL_V4 + DDL_V4_COLUMNS).encode()).hexdigest(), V4_CHECKSUM)
        connection = _v4_database()
        connection.execute("INSERT INTO mail_occurrences(occurrence_id,package_id,source_id,message_key,source_member_name,source_member_ordinal,raw_sha256) VALUES('old','package','source','message','nested.eml',1,'raw')")
        before = [tuple(row) for row in connection.execute("SELECT version,checksum FROM mail_schema_migrations ORDER BY version")]
        self.assertTrue(migrate(connection))
        self.assertFalse(migrate(connection))
        self.assertEqual([tuple(row) for row in connection.execute("SELECT version,checksum FROM mail_schema_migrations WHERE version<5 ORDER BY version")], before)
        self.assertEqual(connection.execute("SELECT is_countable FROM mail_occurrences WHERE occurrence_id='old'").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT checksum FROM mail_schema_migrations WHERE version=5").fetchone()[0], hashlib.sha256(DDL_V5.encode()).hexdigest())
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(mail_occurrences)")}
        self.assertEqual(columns["is_countable"][3], 1)
        self.assertEqual(columns["is_countable"][4], "1")
        connection.close()

    def test_v5_column_guard_accepts_preexisting_column_without_changing_flags(self):
        connection = _v4_database()
        connection.execute("ALTER TABLE mail_occurrences ADD COLUMN is_countable INTEGER NOT NULL DEFAULT 1")
        connection.execute("INSERT INTO mail_occurrences(occurrence_id,package_id,source_id,message_key,source_member_name,source_member_ordinal,raw_sha256,is_countable) VALUES('old','package','source','message','nested.eml',1,'raw',0)")
        self.assertTrue(migrate(connection))
        self.assertEqual(connection.execute("SELECT is_countable FROM mail_occurrences").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM mail_visible_occurrences").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM mail_occurrences").fetchone()[0], 1)
        self.assertFalse(migrate(connection))
        connection.close()

    def test_v5_failure_rolls_back_column_and_ledger(self):
        from services.mail import schema
        connection = _v4_database()
        original = schema._apply_v5
        def interrupted(database):
            original(database)
            raise RuntimeError("interrupted migration")
        with patch.object(schema, "_apply_v5", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted migration"):
                migrate(connection)
        self.assertNotIn("is_countable", {row[1] for row in connection.execute("PRAGMA table_info(mail_occurrences)")})
        self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='mail_visible_occurrences'").fetchone())
        self.assertIsNone(connection.execute("SELECT 1 FROM mail_schema_migrations WHERE version=5").fetchone())
        self.assertTrue(migrate(connection))
        connection.close()


class LegacyNestedRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repository = MailRepository(_Storage(self.root / "mail.db"))
        self.repository.migrate()
        self.switches = [patch.object(Config, "MAIL_ENABLE_CONVERSATIONS", True), patch.object(Config, "MAIL_ENABLE_VECTOR_INDEX", False)]
        for switch in self.switches:
            switch.start()

    def tearDown(self):
        for switch in reversed(self.switches):
            switch.stop()
        self.tmp.cleanup()

    def fixture(self, *, opt_in=False):
        package = self.repository.create_package(owner_id="owner", display_name="legacy", source_kind="directory", source_path=str(self.root), source_revision="r1", config={"include_nested_messages": opt_in})[0]
        pid = package["package_id"]
        child = EmailMessage()
        child["From"] = "nested@example.com"
        child["To"] = "nested-recipient@example.com"
        child["Subject"] = "nested mail"
        child["Message-ID"] = "<nested@example.com>"
        child["Date"] = "Thu, 10 Sep 2026 08:00:00 +0000"
        child.set_content("nested original evidence")
        outer = EmailMessage()
        outer["From"] = "outer@example.com"
        outer["To"] = "recipient@example.com"
        outer["Subject"] = "outer mail"
        outer["Message-ID"] = "<outer@example.com>"
        outer["Date"] = "Thu, 10 Sep 2026 08:00:00 +0000"
        outer.set_content("outer body")
        outer.add_attachment(child)
        raw = outer.as_bytes()
        file = self.root / (pid + ".eml")
        file.write_bytes(raw)
        parent_locator = {"kind": "file", "path": str(file)}
        parsed = parse_eml(raw)
        nested_part = next(part for part in parsed.parts if part.part_role == "nested_message")
        records = []
        for name, ordinal, value, locator in ((file.name, 0, raw, parent_locator), (file.name + "!" + nested_part.mime_path + ".eml", 1, nested_part.raw_payload, {"kind": "nested", "parent": parent_locator, "mime_path": nested_part.mime_path})):
            member = MailMember(name, ordinal, len(value), locator)
            records.append(self.repository.upsert_ingested_message(package_id=pid, source={"locator_type": "directory", "root_path": str(self.root), "source_revision": "r1"}, member=member, raw=value, parsed=parse_eml(value))["message_key"])
        with self.repository._connection() as connection:
            segment = dict(connection.execute("SELECT * FROM mail_text_segments WHERE package_id=? AND message_key=? LIMIT 1", (pid, records[1])).fetchone())
        evidence = self.repository.create_evidence(pid, "owner", segment)
        return pid, records[0], records[1], nested_part.raw_payload, evidence["evidence_id"]

    def test_backfill_retires_only_automatic_nested_counting_preserving_original_and_evidence(self):
        pid, outer, nested, nested_raw, evidence_id = self.fixture()
        with self.repository._connection() as connection:
            before = [tuple(row) for row in connection.execute("SELECT occurrence_id,message_key,source_id FROM mail_occurrences WHERE package_id=? ORDER BY occurrence_id", (pid,))]
        result = backfill_slice(self.repository, pid, "owner", limit=50)
        self.assertEqual(result["status"], "ready")
        with self.repository._connection() as connection:
            after = [tuple(row) for row in connection.execute("SELECT occurrence_id,message_key,source_id FROM mail_occurrences WHERE package_id=? ORDER BY occurrence_id", (pid,))]
            flags = dict(connection.execute("SELECT message_key,is_countable FROM mail_occurrences WHERE package_id=?", (pid,)))
        self.assertEqual(after, before)
        self.assertEqual(flags, {outer: 1, nested: 0})
        self.assertEqual(self.repository.list_messages(pid, "owner")["total"], 1)
        self.assertIsNotNone(self.repository.get_message(nested, "owner", pid))
        self.assertIsNotNone(self.repository.message_source(nested, "owner", pid))
        self.assertEqual(read_message_raw(self.repository, nested, "owner", pid), nested_raw)
        self.assertIsNotNone(self.repository.get_evidence(evidence_id, "owner"))
        self.assertEqual(self.repository.search_segments_lexical(pid, "owner", "nested original evidence")["items"], [])
        self.assertTrue(backfill_slice(self.repository, pid, "owner", limit=50)["already_current"])
        self.assertEqual(self.repository.list_messages(pid, "owner")["total"], 1)

    def test_explicit_nested_opt_in_remains_countable(self):
        pid, outer, nested, _, _ = self.fixture(opt_in=True)
        self.assertEqual(backfill_slice(self.repository, pid, "owner", limit=50)["status"], "ready")
        with self.repository._connection() as connection:
            flags = dict(connection.execute("SELECT message_key,is_countable FROM mail_occurrences WHERE package_id=?", (pid,)))
        self.assertEqual(flags, {outer: 1, nested: 1})
        self.assertEqual(self.repository.list_messages(pid, "owner")["total"], 2)

    def test_single_message_slices_finish_without_resetting_the_checkpoint(self):
        pid, outer, nested, _, evidence_id = self.fixture()
        processed = []
        for _ in range(8):
            result = backfill_slice(self.repository, pid, "owner", limit=1)
            processed.append(result.get("processed", 0))
            if not result.get("_requeue_slice"):
                break
        else:
            self.fail("one-message slices kept resetting the backfill checkpoint")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(processed, [1, 2, 2])
        with self.repository._connection() as connection:
            flags = dict(connection.execute("SELECT message_key,is_countable FROM mail_occurrences WHERE package_id=?", (pid,)))
        self.assertEqual(flags, {outer: 1, nested: 0})
        self.assertIsNotNone(self.repository.get_evidence(evidence_id, "owner"))

    def test_enabling_nested_after_retirement_restores_counting_and_text_index(self):
        pid, outer, nested, nested_raw, evidence_id = self.fixture()
        self.assertEqual(backfill_slice(self.repository, pid, "owner", limit=50)["status"], "ready")
        self.assertEqual(self.repository.list_messages(pid, "owner")["total"], 1)
        self.assertEqual(self.repository.search_segments_lexical(pid, "owner", "nested original evidence")["items"], [])
        with self.repository._connection() as connection:
            connection.execute("UPDATE mail_packages SET config_json=? WHERE package_id=?", (json.dumps({"include_nested_messages": True}), pid))
        result = backfill_slice(self.repository, pid, "owner", limit=50)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result.get("already_current", False))
        self.assertEqual(self.repository.list_messages(pid, "owner")["total"], 2)
        restored = self.repository.search_segments_lexical(pid, "owner", "nested original evidence")["items"]
        self.assertEqual({item["message_key"] for item in restored}, {nested})
        with self.repository._connection() as connection:
            flags = dict(connection.execute("SELECT message_key,is_countable FROM mail_occurrences WHERE package_id=?", (pid,)))
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM mail_text_segments WHERE package_id=? AND message_key=?", (pid, nested)).fetchone()[0], 0)
        self.assertEqual(flags, {outer: 1, nested: 1})
        self.assertEqual(read_message_raw(self.repository, nested, "owner", pid), nested_raw)
        self.assertIsNotNone(self.repository.get_evidence(evidence_id, "owner"))


if __name__ == "__main__":
    unittest.main()
