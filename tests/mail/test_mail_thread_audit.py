"""Browsing regressions for disputed headers, missing context and saved content."""
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
from services.mail.conversations import apply_correction, detail, list_conversations
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.repository import MailRepository
from services.mail.threading import build_relations


def _node(key, **headers):
    return {"message_key": key, "headers_json": json.dumps(headers)}


class HeaderIdentityAuditTests(unittest.TestCase):
    def test_missing_identifier_is_visible_without_inventing_an_id(self):
        nodes, edges, anomalies = build_relations("package", [_node("mail")])
        self.assertIsNone(nodes[0]["message_id_normalized"])
        self.assertEqual(edges, [])
        self.assertEqual([item["anomaly_type"] for item in anomalies], ["missing_message_id"])

    def test_conflicting_message_ids_cannot_become_a_unique_parent(self):
        nodes, edges, anomalies = build_relations("package", [
            _node("parent", **{"message-id": ["<first@example.com> <second@example.com>"]}),
            _node("child", **{"message-id": ["<child@example.com>"],
                              "in-reply-to": ["<first@example.com>"],
                              "references": ["<second@example.com>"]}),
        ])
        self.assertEqual({edge["status"] for edge in edges}, {"ambiguous"})
        self.assertTrue(all(edge["target_message_key"] is None for edge in edges))
        self.assertTrue(all(edge["evidence"]["conflicting_identity_candidates"] == ["parent"] for edge in edges))
        self.assertTrue({"multiple_message_ids", "conflicting_target_identity"}.issubset(
            {item["anomaly_type"] for item in anomalies}))
        self.assertEqual(len(nodes), 2)

    def test_invalid_explicit_parent_does_not_fall_back_to_history(self):
        _, edges, anomalies = build_relations("package", [
            _node("parent", **{"message-id": ["<parent@example.com>"]}),
            _node("child", **{"message-id": ["<child@example.com>"],
                              "in-reply-to": ["malformed parent"],
                              "references": ["<parent@example.com>"]}),
        ])
        self.assertEqual([(edge["relation_type"], edge["status"]) for edge in edges], [("reference", "context")])
        self.assertEqual([(item["anomaly_type"], item["payload"]["header"]) for item in anomalies],
                         [("invalid_identifier", "in-reply-to")])

    def test_domain_case_does_not_create_an_identity_conflict(self):
        _, edges, anomalies = build_relations("package", [
            _node("parent", **{"message-id": ["<parent@EXAMPLE.com> <parent@example.com>"]}),
            _node("child", **{"message-id": ["<child@example.com>"], "in-reply-to": ["<parent@example.com>"]}),
        ])
        self.assertEqual(edges[0]["status"], "exact")
        self.assertEqual(edges[0]["target_message_key"], "parent")
        self.assertEqual(anomalies, [])


class _Storage:
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


class ConversationReadingAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repository = MailRepository(_Storage(self.root / "mail.db"))
        self.repository.migrate()
        self.package = self.repository.create_package(owner_id="owner", display_name="test", source_kind="directory",
                                                     source_path=str(self.root), source_revision="r1", config={})[0]
        self.pid = self.package["package_id"]
        self.n = 0
        self.switch = patch.object(Config, "MAIL_ENABLE_CONVERSATIONS", True)
        self.switch.start()

    def tearDown(self):
        self.switch.stop()
        self.tmp.cleanup()

    def ingest(self, mid, *, subject="Shared", reply=None, references=None, sender="Alice <alice@example.com>",
               to="bob@example.com", cc=None, date="Thu, 10 Sep 2026 08:00:00 +0000", body="body", attachment=False):
        message = EmailMessage()
        message["From"] = sender
        message["To"] = to
        if cc:
            message["Cc"] = cc
        message["Subject"] = subject
        message["Message-ID"] = "<{}@example.com>".format(mid)
        message["Date"] = date
        if reply:
            message["In-Reply-To"] = reply
        if references:
            message["References"] = references
        message.set_content(body)
        if attachment:
            message.add_attachment(b"original attachment", maintype="application", subtype="octet-stream", filename="original.txt")
        raw = message.as_bytes()
        member = MailMember("mail{}.eml".format(self.n), self.n, len(raw), {"kind": "file", "path": "unused"})
        self.n += 1
        result = self.repository.upsert_ingested_message(package_id=self.pid, source={"locator_type": "directory",
            "root_path": str(self.root), "source_revision": "r1"}, member=member, raw=raw, parsed=parse_eml(raw))
        return result["message_key"]

    def conversation_for(self, key, **filters):
        rows = list_conversations(self.repository, self.pid, "owner")
        for row in rows:
            value = detail(self.repository, row["conversation_id"], "owner")
            if any(message["message_key"] == key for message in value["messages"]):
                return detail(self.repository, row["conversation_id"], "owner", **filters)
        self.fail("message has no browsing conversation")

    def test_interleaved_unrelated_people_do_not_hide_subject_candidate(self):
        first = self.ingest("first", date="Thu, 10 Sep 2026 08:00:00 +0000")
        unrelated = self.ingest("other", sender="other@example.org", to="outsider@example.org",
                                date="Thu, 10 Sep 2026 09:00:00 +0000")
        last = self.ingest("last", date="Thu, 10 Sep 2026 10:00:00 +0000")
        rows = list_conversations(self.repository, self.pid, "owner")
        self.assertEqual([row["message_count"] for row in rows], [1, 1, 1])
        candidates = self.conversation_for(last)["candidate_edges"]
        self.assertEqual([(edge["source_message_key"], edge["target_message_key"]) for edge in candidates], [(last, first)])
        self.assertTrue(all(unrelated not in {edge["source_message_key"], edge["target_message_key"]} for edge in candidates))
        # Equal subjects years apart cannot acquire even a candidate link.
        years_later = self.ingest("later", date="Fri, 10 Sep 2027 10:00:00 +0000")
        self.assertEqual(self.conversation_for(years_later)["candidate_edges"], [])

    def test_saved_fragments_and_attachment_metadata_are_available_without_reparse(self):
        body = "本次新增正文\n" + "内容完整" * 6000 + "\n> 历史引用\n-- \n署名\n"
        key = self.ingest("content", body=body, cc="bob@example.com", attachment=True)
        # The conversation reader uses saved fragments, never raw MIME parsing.
        with patch("services.mail.parsers.eml.parse_eml", side_effect=AssertionError("unexpected raw parse")):
            value = self.conversation_for(key)
        message = value["timeline"][0]
        self.assertEqual(message["body_status"], "ready")
        self.assertEqual("".join(fragment["text"] for fragment in message["body_fragments"]), body)
        self.assertEqual({fragment["fragment_kind"] for fragment in message["body_fragments"]}, {"authored", "quoted", "signature"})
        self.assertEqual(value["attachments"], message["attachments"])
        part = value["attachments"][0]
        self.assertEqual(part["filename_raw"], "original.txt")
        self.assertEqual(part["byte_size"], len(b"original attachment"))
        self.assertEqual(part["processing_status"], "not_started")
        recipient = next(person for person in value["participants"] if person["address"] == "bob@example.com")
        self.assertEqual(recipient["roles"], ["cc", "to"])

    def test_legacy_body_needs_backfill_instead_of_claiming_empty(self):
        key = self.ingest("legacy")
        with self.repository._connection() as connection:
            connection.execute("DELETE FROM mail_body_fragments WHERE message_key=?", (key,))
        message = self.conversation_for(key)["messages"][0]
        self.assertEqual(message["body_status"], "backfill_required")
        self.assertEqual(message["body_fragments"], [])

    def test_explicit_and_historical_gaps_are_kept_distinct(self):
        key = self.ingest("child", reply="<absent-parent@example.com>", references="<absent-context@example.com>")
        value = self.conversation_for(key)
        self.assertEqual({gap["gap_kind"] for gap in value["missing_context"]}, {"direct_parent", "historical_context"})
        self.assertEqual({gap["relation_type"] for gap in value["missing_context"]}, {"reply_to", "reference"})
        self.assertEqual(len([node for node in value["tree_nodes"] if node["missing"]]), 1)
        self.assertEqual(len(value["context_edges"]), 1)
        self.assertFalse(any(edge["relation_type"] == "reference_parent" for edge in value["edges"]))

    def test_filters_scope_reading_attachments_participants_and_time_range(self):
        parent = self.ingest("parent", to="earlier@example.com", attachment=True)
        child = self.ingest("child", reply="<parent@example.com>", body="needle", to="later@example.com",
                            date="Fri, 11 Sep 2026 09:00:00 +0000")
        value = self.conversation_for(parent, q="needle")
        self.assertEqual([message["message_key"] for message in value["timeline"]], [child])
        self.assertEqual(value["total_message_count"], 2)
        self.assertEqual(value["attachments"], [])
        self.assertEqual(value["filtered_time_range"], {"from": "2026-09-11T09:00:00+00:00", "to": "2026-09-11T09:00:00+00:00"})
        self.assertEqual({person["address"] for person in value["participants"]}, {"alice@example.com", "later@example.com"})

    def test_correction_panel_contains_only_relevant_records_and_revocation(self):
        a, b, c, d = [self.ingest(mid, subject=mid) for mid in "abcd"]
        merge = apply_correction(self.repository, self.pid, "owner", action="merge", message_keys=[a, b], reason="same discussion")
        apply_correction(self.repository, self.pid, "owner", action="merge", message_keys=[c, d], reason="unrelated discussion")
        value = self.conversation_for(a)
        self.assertEqual([item["correction_id"] for item in value["corrections"]], [merge["correction_id"]])
        self.assertEqual(value["applied_corrections"], [merge["correction_id"]])
        revoke = apply_correction(self.repository, self.pid, "owner", action="revoke", correction_id=merge["correction_id"])
        value = self.conversation_for(a)
        self.assertEqual({item["correction_id"] for item in value["corrections"]}, {merge["correction_id"], revoke["correction_id"]})
        self.assertTrue(next(item for item in value["corrections"] if item["correction_id"] == merge["correction_id"])["revoked"])
        self.assertEqual(value["applied_corrections"], [])

    def test_missing_parent_context_survives_publication_without_fake_direct_edge(self):
        old = self.ingest("old", subject="Initial discussion")
        child = self.ingest("child", subject="Changed subject", reply="<absent@example.com>",
                            references="<old@example.com> <absent@example.com>")
        later = self.ingest("later", reply="<child@example.com>",
                            references="<old@example.com> <absent@example.com> <child@example.com>")
        rows = list_conversations(self.repository, self.pid, "owner")
        self.assertEqual([row["message_count"] for row in rows], [3])
        value = detail(self.repository, rows[0]["conversation_id"], "owner")
        self.assertEqual({message["message_key"] for message in value["timeline"]}, {old, child, later})
        self.assertEqual(value["gap_summary"], {"missing_message_count": 1, "direct_parent_gap_count": 1,
                                               "inferred_parent_gap_count": 0, "historical_reference_count": 2})
        self.assertEqual(len(value["context_bridges"]), 1)
        self.assertEqual(value["context_bridges"][0]["ancestor_message_key"], old)
        missing_parent = next(edge for edge in value["tree_edges"] if edge["source_message_key"] == child)
        self.assertEqual(missing_parent["status"], "missing_target")
        self.assertIsNone(missing_parent["target_message_key"])
        self.assertEqual(len([node for node in value["tree_nodes"] if node["missing"]]), 1)
        split = apply_correction(self.repository, self.pid, "owner", action="split", groups=[[old], [child, later]])
        self.assertEqual(sorted(row["message_count"] for row in list_conversations(self.repository, self.pid, "owner")), [1, 2])
        apply_correction(self.repository, self.pid, "owner", action="revoke", correction_id=split["correction_id"])
        self.assertEqual([row["message_count"] for row in list_conversations(self.repository, self.pid, "owner")], [3])

    def test_importing_missing_middle_mail_resolves_gap_and_keeps_conversation_id(self):
        self.ingest("old")
        child = self.ingest("child", reply="<absent@example.com>", references="<old@example.com> <absent@example.com>")
        before = self.conversation_for(child)
        actual_parent = self.ingest("absent", reply="<old@example.com>", references="<old@example.com>")
        after = self.conversation_for(child)
        self.assertEqual(after["conversation_id"], before["conversation_id"])
        self.assertEqual(after["message_count"], 3)
        self.assertEqual(after["gap_summary"]["missing_message_count"], 0)
        self.assertEqual(after["context_bridges"], [])
        self.assertEqual(next(edge for edge in after["tree_edges"] if edge["source_message_key"] == child)["target_message_key"], actual_parent)


if __name__ == "__main__":
    unittest.main()
