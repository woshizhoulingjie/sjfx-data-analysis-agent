import tempfile
import unittest
import mailbox
import json
from email import message_from_bytes
from pathlib import Path
from unittest.mock import patch

from config import Config
from services.mail.api import _mail_endpoint, _source_revision
from services.mail.html_safety import sanitize_html
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.repository import MailRepository
from services.mail.research.intent import build_task_spec
from services.mail.research.runtime import JobCancelled, MailResearchRuntime
from services.mail.research.verifier import verify_business_relation
from services.mail.source import DirectoryAdapter, MboxAdapter, open_locator
from services.storage import Storage


def _mail(*, message_id, subject, body, in_reply_to=None, recipient="bob@example.com"):
    headers = [
        "From: Alice <alice@example.com>",
        "To: {}".format(recipient),
        "Date: Tue, 07 May 2024 15:38:50 +0700",
        "Message-ID: {}".format(message_id),
        "Subject: {}".format(subject),
        "Content-Type: text/plain; charset=utf-8",
    ]
    if in_reply_to:
        headers.append("In-Reply-To: {}".format(in_reply_to))
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


class MailDomainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.directory.name) / "mail.sqlite3")
        self.repository = MailRepository(self.storage, text_segment_chars=8)
        self.repository.migrate()
        self.member_number = 0

    def tearDown(self):
        self.directory.cleanup()

    def _package(self, owner="alice"):
        package, created = self.repository.create_package(
            owner_id=owner,
            display_name="mail test",
            source_kind="directory",
            source_path=str(Path(self.directory.name)),
            source_revision="sha256:test-{}".format(owner),
            config={},
        )
        self.assertTrue(created)
        return package

    def _ingest(self, package, raw, name=None):
        ordinal = self.member_number
        self.member_number += 1
        member = MailMember(name or "message-{}.eml".format(ordinal), ordinal, len(raw), {"kind": "file", "path": "unused"})
        return self.repository.upsert_ingested_message(
            package_id=package["package_id"],
            source={
                "locator_type": "directory",
                "root_path": self.directory.name,
                "container_path": None,
                "container_sha256": package["source_revision"],
                "source_revision": package["source_revision"],
            },
            member=member,
            raw=raw,
            parsed=parse_eml(raw),
        )

    def test_exact_raw_dedup_and_logical_statistics(self):
        package = self._package()
        raw = _mail(message_id="<same@example.com>", subject="duplicate", body="segment-match-alpha")
        first = self._ingest(package, raw, "one.eml")
        second = self._ingest(package, raw, "two.eml")

        self.assertTrue(first["new_message"])
        self.assertFalse(second["new_message"])
        overview = self.repository.overview(package["package_id"], "alice")
        self.assertEqual(overview["counts"]["logical_messages"], 1)
        self.assertEqual(overview["counts"]["occurrences"], 2)
        self.assertEqual(overview["counts"]["messages_with_body"], 1)
        self.assertGreaterEqual(len(self.repository.search_segments(package["package_id"], "alice", "segment")["items"]), 1)

    def test_message_id_edges_are_exact_only_for_unique_matching_token(self):
        package = self._package()
        parent = self._ingest(package, _mail(message_id="<a.b@example.com>", subject="parent", body="parent body"))
        child = self._ingest(package, _mail(message_id="<child@example.com>", subject="reply", body="child body", in_reply_to="<a.b@example.com>"))
        self._ingest(package, _mail(message_id="<other@example.com>", subject="near miss", body="near miss", in_reply_to="<ab@example.com>"))

        self.repository.finish_import(package["package_id"], "alice")
        edges = self.repository.threads(package["package_id"], "alice")
        exact = [edge for edge in edges if edge["status"] == "exact"]
        missing = [edge for edge in edges if edge["status"] == "missing_target"]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0]["source_message_key"], child["message_key"])
        self.assertEqual(exact[0]["target_message_key"], parent["message_key"])
        self.assertEqual(len(missing), 1)
        self.assertEqual(
            len(self.repository.threads(package["package_id"], "alice", status="exact", relation_type="reply_to")),
            1,
        )

    def test_configured_segment_size_applies_to_body_and_attachment_text(self):
        package = self._package()
        message = self._ingest(package, _mail(message_id="<segments@example.com>", subject="segments", body="abcdefghijklmnop"))
        with self.repository._connection() as connection:
            body_parts = connection.execute(
                "SELECT text FROM mail_text_segments WHERE package_id=? AND source_kind='body' ORDER BY ordinal",
                (package["package_id"],),
            ).fetchall()
        self.assertEqual([row["text"] for row in body_parts], ["abcdefgh", "ijklmnop"])

        self.repository.replace_attachment_segments(
            package["package_id"], "alice", message["message_key"], "1.2", "0123456789abcdef"
        )
        with self.repository._connection() as connection:
            attachment_parts = connection.execute(
                "SELECT text FROM mail_text_segments WHERE package_id=? AND source_kind='attachment' ORDER BY ordinal",
                (package["package_id"],),
            ).fetchall()
        self.assertEqual([row["text"] for row in attachment_parts], ["01234567", "89abcdef"])
        preview_segments = self.repository.attachment_segments(
            package["package_id"], "alice", message["message_key"], "1.2"
        )
        self.assertEqual([item["text"] for item in preview_segments], ["01234567", "89abcdef"])

    def test_owner_scope_applies_to_messages_and_evidence(self):
        alice_package = self._package("alice")
        bob_package = self._package("bob")
        alice_message = self._ingest(alice_package, _mail(message_id="<alice-only@example.com>", subject="private", body="private marker"))
        self._ingest(bob_package, _mail(message_id="<bob-only@example.com>", subject="private", body="bob marker"))

        self.assertIsNone(self.repository.get_message(alice_message["message_key"], "bob", alice_package["package_id"]))
        with self.assertRaises(ValueError):
            self.repository.list_messages(alice_package["package_id"], "bob")
        evidence = self.repository.search_segments(alice_package["package_id"], "alice", "private marker")["items"][0]
        self.assertIsNone(self.repository.get_evidence(evidence["evidence_id"], "bob"))

    def test_html_preview_removes_active_and_remote_content(self):
        preview = sanitize_html(
            '<p onclick="alert(1)">safe</p><img src="https://tracker.invalid/pixel">'
            '<script>steal()</script><form action="https://invalid"><input></form><a href="https://invalid">link</a>'
        )
        self.assertIn("safe", preview)
        self.assertNotIn("script", preview)
        self.assertNotIn("tracker.invalid", preview)
        self.assertNotIn("onclick", preview)
        self.assertNotIn("form", preview)
        self.assertNotIn("href", preview)

    def test_inline_file_part_is_not_an_explicit_attachment(self):
        raw = (
            b"From: alice@example.com\r\nContent-Type: multipart/related; boundary=outer\r\n\r\n"
            b"--outer\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<body><img src=cid:poster>\r\n"
            b"--outer\r\nContent-Type: image/png; name=poster.png\r\nContent-Disposition: inline; filename=poster.png\r\nContent-ID: <poster>\r\n\r\nraw\r\n--outer--\r\n"
        )
        parts = parse_eml(raw).parts
        inline = next(part for part in parts if part.content_type == "image/png")
        self.assertEqual(inline.part_role, "inline")
        self.assertFalse(inline.is_explicit_attachment)
        self.assertTrue(inline.is_inline_resource)

    def test_uppercase_directory_eml_and_nested_message_limit(self):
        source = Path(self.directory.name) / "UPPER.EML"
        source.write_bytes(_mail(message_id="<upper@example.com>", subject="upper", body="upper"))
        members = list(DirectoryAdapter().inventory(type("Source", (), {"path": str(Path(self.directory.name))})(), type("Policy", (), {"max_archive_entries": 10, "max_message_bytes": 100000, "validate_size": lambda *_args, **_kwargs: None})()))
        self.assertEqual([member.name for member in members], ["UPPER.EML"])
        self.assertTrue(_source_revision(Path(self.directory.name)).startswith("sha256-manifest:"))

        nested = (
            b"From: alice@example.com\r\nContent-Type: multipart/mixed; boundary=outer\r\n\r\n"
            b"--outer\r\nContent-Type: message/rfc822\r\n\r\n"
            b"From: nested@example.com\r\nContent-Type: text/plain\r\n\r\nnested\r\n--outer--\r\n"
        )
        parsed = parse_eml(nested, max_nested_message_depth=0)
        self.assertIn("nested_message", [part.part_role for part in parsed.parts])
        self.assertTrue(any("嵌套邮件深度" in warning for warning in parsed.warnings))

    def test_mbox_locator_is_readable_after_json_key_round_trip(self):
        path = Path(self.directory.name) / "sample.mbox"
        raw = _mail(message_id="<mbox@example.com>", subject="mbox", body="mbox body")
        box = mailbox.mbox(path)
        try:
            box.add(mailbox.mboxMessage(message_from_bytes(raw)))
            box.flush()
        finally:
            box.close()

        adapter = MboxAdapter()
        policy = type("Policy", (), {
            "max_archive_entries": 10,
            "max_message_bytes": 100000,
            "validate_size": lambda *_args, **_kwargs: None,
        })()
        source = type("Source", (), {"path": str(path)})()
        member = next(adapter.inventory(source, policy))
        self.assertIsInstance(member.locator["key"], str)
        self.assertEqual(parse_eml(adapter.open_message(source, member).read()).message_id_raw, ["<mbox@example.com>"])
        self.assertEqual(parse_eml(open_locator(member.locator).read()).message_id_raw, ["<mbox@example.com>"])

    def test_feature_flag_returns_not_found_before_handler_execution(self):
        called = []

        @_mail_endpoint
        def handler():
            called.append(True)
            return {"ok": True}

        with patch.object(Config, "MAIL_FEATURE_ENABLED", False):
            response, status = handler()
        self.assertEqual(status, 404)
        self.assertFalse(called)
        self.assertFalse(response.get_json()["ok"])

    def test_research_runtime_uses_model_thai_term_for_chinese_question(self):
        package = self._package()
        self._ingest(
            package,
            _mail(
                message_id="<thai-search@example.com>",
                subject="delivery",
                body="ยืนยันกำหนดส่งวันที่ 10 มิถุนายน",
            ),
        )
        session = self.repository.create_session(package["package_id"], "alice", "Thai retrieval")
        spec = build_task_spec(
            "查找确认交付日期的邮件", package["package_id"], package["source_revision"],
            package["index_epoch"], max_tool_calls=8, max_model_calls=2, max_seconds=60,
        )
        turn = self.repository.create_turn(session["session_id"], "alice", spec["objective"], spec)

        class ThaiTermModel:
            def __init__(self):
                self.calls = 0

            def chat_json(self, _system, _prompt, **kwargs):
                self.calls += 1
                if kwargs["required_fields"] == ("queries",):
                    return {"json": {"queries": ["กำหนดส่ง"]}}
                return {"json": {"actions": [{"name": "stop"}]}}

        model = ThaiTermModel()
        final = MailResearchRuntime(self.repository, model=model).run(turn["turn_id"], "alice")
        self.assertEqual(model.calls, 2)
        self.assertTrue(final["evidence"])
        self.assertEqual(final["evidence"][0]["message_key"], self.repository.list_messages(package["package_id"], "alice")["items"][0]["message_key"])
        result = self.repository.get_result_set(final["result_set_id"], "alice")
        self.assertEqual(result["items"][0]["status"], "matched")

    def test_research_runtime_persists_only_evidence_anchored_business_relations(self):
        package = self._package()
        self._ingest(
            package,
            _mail(
                message_id="<delivery-confirmation@example.com>",
                subject="delivery",
                body="Alice confirms delivery date June 10.",
            ),
        )
        session = self.repository.create_session(package["package_id"], "alice", "Business relation")
        spec = build_task_spec(
            "查找交付日期的确认", package["package_id"], package["source_revision"],
            package["index_epoch"], max_tool_calls=8, max_model_calls=3, max_seconds=60,
        )
        turn = self.repository.create_turn(session["session_id"], "alice", spec["objective"], spec)

        class RelationModel:
            def chat_json(self, _system, prompt, **kwargs):
                if kwargs["required_fields"] == ("queries",):
                    return {"json": {"queries": ["delivery"]}}
                if kwargs["required_fields"] == ("actions",):
                    return {"json": {"actions": [{"name": "stop"}]}}
                evidence = json.loads(prompt)["evidence"][0]
                return {"json": {"answer": "交付日期已确认", "claims": [{
                    "claim_text": "Alice confirms delivery date June 10",
                    "subject_ref": "Alice",
                    "predicate": "confirmed",
                    "object_ref": "delivery date June 10",
                    "state": "asserted",
                    "evidence_ids": [evidence["evidence_id"]],
                    "time_basis": "邮件正文",
                    "uncertainty": "",
                }]}}

        final = MailResearchRuntime(self.repository, model=RelationModel()).run(turn["turn_id"], "alice")
        self.assertEqual(final["claims"][0]["support_status"], "supported")
        self.assertIn("Alice confirms delivery date June 10", final["answer"])
        relations = self.repository.business_relations(package["package_id"], "alice")
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["predicate"], "confirmed")
        self.assertEqual(relations[0]["state"], "asserted")
        self.assertTrue(relations[0]["evidence_ids"])

    def test_business_relation_rejects_unanchored_subject_or_object(self):
        verification = verify_business_relation(
            {
                "claim_text": "Bob confirms secret date",
                "subject_ref": "Bob",
                "predicate": "confirmed",
                "object_ref": "secret date",
                "evidence_ids": ["mev_known"],
            },
            {"mev_known": {"quote_text": "Alice confirms delivery date June 10."}},
        )
        self.assertEqual(verification["state"], "insufficient")
        self.assertEqual(verification["support_status"], "unanchored_entity")

    def test_research_runtime_marks_turn_cancelled(self):
        package = self._package()
        self._ingest(package, _mail(message_id="<cancel@example.com>", subject="cancel", body="body"))
        session = self.repository.create_session(package["package_id"], "alice", "Cancellation")
        spec = build_task_spec(
            "查找邮件", package["package_id"], package["source_revision"], package["index_epoch"],
            max_tool_calls=8, max_model_calls=0, max_seconds=60,
        )
        turn = self.repository.create_turn(session["session_id"], "alice", spec["objective"], spec)

        with self.assertRaises(JobCancelled):
            MailResearchRuntime(self.repository, cancel_check=lambda: True).run(turn["turn_id"], "alice")

        cancelled = self.repository.get_turn(turn["turn_id"], "alice")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIn("cancelled", [event["event_type"] for event in cancelled["events"]])

    def test_duplicate_message_projects_attachment_text_to_later_package(self):
        package_one = self._package()
        package_two = self._package()
        raw = (
            b"From: Alice <alice@example.com>\r\n"
            b"To: Bob <bob@example.com>\r\n"
            b"Message-ID: <duplicate-attachment@example.com>\r\n"
            b"Subject: attachment\r\n"
            b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
            b"--outer\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
            b"--outer\r\nContent-Type: text/plain; name=details.txt\r\n"
            b"Content-Disposition: attachment; filename=details.txt\r\n\r\n"
            b"only-in-attachment\r\n--outer--\r\n"
        )
        first = self._ingest(package_one, raw, "first.eml")
        second = self._ingest(package_two, raw, "second.eml")
        attachment = next(part for part in parse_eml(raw).parts if part.is_explicit_attachment)

        self.repository.replace_attachment_segments(
            package_one["package_id"], "alice", first["message_key"], attachment.mime_path,
            "only-in-attachment",
        )
        hits = self.repository.search_segments(
            package_two["package_id"], "alice", "only-in-attachment", attachment_only=True,
        )
        self.assertFalse(second["new_message"])
        self.assertEqual(hits["items"][0]["message_key"], first["message_key"])


if __name__ == "__main__":
    unittest.main()
