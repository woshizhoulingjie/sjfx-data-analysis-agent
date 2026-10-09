"""Independent header checks for MCS and conservative missing-parent context.

Set MCS_FULL_ZIP to the supplied complete package on other checkouts. The
fixture's prose has an incorrect longest-depth and M045-branch claim; these
tests derive direct parents from the actual stored mail headers instead.
"""
import collections
import json
import os
import unittest
import zipfile
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path

from services.mail.conversations import _gap_contexts, _group
from services.mail.threading import build_relations


def _message(key, *, mid=None, reply=None, references=None):
    headers = {"message-id": [mid or "<{}@example.com>".format(key)]}
    if reply:
        headers["in-reply-to"] = [reply]
    if references:
        headers["references"] = [references]
    return {"message_key": key, "headers_json": json.dumps(headers)}


def _relations(messages):
    return build_relations("fixture", messages)[1]


class MissingParentContextTests(unittest.TestCase):
    def test_gap_keeps_history_together_without_changing_direct_parent(self):
        messages = [_message("old"), _message("child", reply="<absent@example.com>",
                    references="<old@example.com> <absent@example.com>"),
                    _message("later", reply="<child@example.com>",
                    references="<old@example.com> <absent@example.com> <child@example.com>")]
        edges = _relations(messages)
        groups, _ = _group(messages, edges, [])
        self.assertEqual(groups, [{"old", "child", "later"}])
        parent = next(edge for edge in edges if edge["source_message_key"] == "child" and edge["relation_type"] == "reply_to")
        self.assertEqual(parent["status"], "missing_target")
        self.assertIsNone(parent["target_message_key"])
        bridges = _gap_contexts(messages, edges)
        self.assertEqual(len(bridges), 1)
        self.assertEqual(bridges[0]["ancestor_message_key"], "old")
        self.assertFalse(bridges[0]["evidence"]["direct_parent_replaced"])

    def test_present_parent_does_not_merge_unrelated_historical_context(self):
        messages = [_message("old"), _message("recent"),
                    _message("child", reply="<recent@example.com>", references="<old@example.com> <recent@example.com>")]
        edges = _relations(messages)
        self.assertEqual({frozenset(group) for group in _group(messages, edges, [])[0]},
                         {frozenset({"old"}), frozenset({"recent", "child"})})
        self.assertEqual(_gap_contexts(messages, edges), [])

    def test_references_tail_must_agree_with_the_explicit_missing_parent(self):
        messages = [_message("old"), _message("child", reply="<absent@example.com>",
                    references="<old@example.com> <different@example.com>")]
        edges = _relations(messages)
        self.assertEqual(len(_group(messages, edges, [])[0]), 2)
        self.assertEqual(_gap_contexts(messages, edges), [])

    def test_disputed_identifiers_do_not_anchor_a_gap(self):
        for disputed in (["<duplicate@example.com>", "<duplicate@example.com>"],
                         ["<duplicate@example.com> <other@example.com>"]):
            messages = [_message("old{}".format(index), mid=mid) for index, mid in enumerate(disputed)]
            messages.append(_message("child", reply="<absent@example.com>",
                            references="<duplicate@example.com> <absent@example.com>"))
            edges = _relations(messages)
            self.assertEqual(len(_group(messages, edges, [])[0]), len(messages))
            self.assertEqual(_gap_contexts(messages, edges), [])

    def test_cyclic_or_reversed_ancestors_do_not_anchor_a_gap(self):
        for messages in ([
            _message("a", reply="<b@example.com>"), _message("b", reply="<a@example.com>"),
            _message("child", reply="<absent@example.com>", references="<a@example.com> <absent@example.com>"),
        ], [
            _message("a"), _message("b", reply="<a@example.com>"),
            _message("child", reply="<absent@example.com>", references="<b@example.com> <a@example.com> <absent@example.com>"),
        ], [
            _message("a", reply="<child@example.com>"),
            _message("child", reply="<absent@example.com>", references="<a@example.com> <absent@example.com>"),
        ]):
            self.assertEqual(_gap_contexts(messages, _relations(messages)), [])

    def test_manual_split_overrides_gap_context_and_revoke_restores_it(self):
        messages = [_message("old"), _message("child", reply="<absent@example.com>",
                    references="<old@example.com> <absent@example.com>")]
        edges = _relations(messages)
        split = {"correction_id": "split", "action": "split", "payload_json": json.dumps({"groups": [["old"], ["child"]]})}
        self.assertEqual(len(_group(messages, edges, [split])[0]), 2)
        revoke = {"correction_id": "revoke", "action": "revoke", "payload_json": json.dumps({"correction_id": "split"})}
        self.assertEqual(_group(messages, edges, [split, revoke])[0], [{"old", "child"}])


_FIXTURE = Path(os.environ.get("MCS_FULL_ZIP", str(Path(__file__).resolve().parents[3] / "mcs-fixture" / "full.zip")))


@unittest.skipUnless(_FIXTURE.is_file(), "set MCS_FULL_ZIP to the supplied 56-mail MCS archive")
class ActualMCSHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mail = {}
        with zipfile.ZipFile(_FIXTURE) as archive:
            for name in archive.namelist():
                if not name.casefold().endswith(".eml"):
                    continue
                key = Path(name).name.split("_")[0]
                cls.mail[key] = BytesHeaderParser(policy=policy.default).parsebytes(archive.read(name))
        cls.ids = {str(mail["Message-ID"]): key for key, mail in cls.mail.items()}
        cls.parents = {key: cls.ids.get(str(mail["In-Reply-To"])) for key, mail in cls.mail.items()}
        cls.messages = [{"message_key": key, "headers_json": json.dumps({
            header: [str(value) for value in mail.get_all(header, [])]
            for header in ("message-id", "in-reply-to", "references")})} for key, mail in cls.mail.items()]

    def test_actual_tree_has_four_branches_and_48_mail_longest_path(self):
        self.assertEqual(len(self.mail), 56)
        self.assertEqual(len(self.ids), 56)
        self.assertEqual([key for key, parent in self.parents.items() if parent is None], ["M001"])
        children = collections.defaultdict(set)
        for key, parent in self.parents.items():
            if parent:
                children[parent].add(key)
        self.assertEqual({parent: children for parent, children in children.items() if len(children) > 1},
                         {"M001": {"M002", "M003"}, "M008": {"M009", "M010"},
                          "M016": {"M017", "M018"}, "M040": {"M041", "M042"}})
        depths = {}
        for key in sorted(self.parents):
            depths[key] = 1 + depths.get(self.parents[key], 0)
        self.assertEqual(max(depths.values()), 48)
        self.assertEqual(depths["M056"], 48)
        self.assertEqual(self.parents["M047"], "M046")
        self.assertEqual(children["M045"], {"M046"})

    def test_product_direct_edges_match_all_actual_headers_and_subject_changes(self):
        edges = _relations(self.messages)
        actual = {edge["source_message_key"]: edge["target_message_key"] for edge in edges if edge["relation_type"] == "reply_to"}
        self.assertEqual(actual, {key: parent for key, parent in self.parents.items() if parent})
        self.assertTrue(all(edge["status"] == "exact" for edge in edges if edge["relation_type"] == "reply_to"))
        self.assertEqual(_group(self.messages, edges, [])[0], [set(self.mail)])
        self.assertEqual({key: actual[key] for key in ("M013", "M029", "M039", "M047")},
                         {"M013": "M012", "M029": "M028", "M039": "M038", "M047": "M046"})

    def test_missing_m024_preserves_one_browsing_conversation_and_one_direct_gap(self):
        messages = [message for message in self.messages if message["message_key"] != "M024"]
        nodes, edges, _ = build_relations("fixture", messages)
        self.assertEqual(_group(messages, edges, [])[0], [set(self.mail) - {"M024"}])
        gaps = [edge for edge in edges if edge["status"] == "missing_target"]
        direct = [edge for edge in gaps if edge["relation_type"] == "reply_to"]
        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0]["source_message_key"], "M026")
        self.assertIsNone(direct[0]["target_message_key"])
        self.assertEqual(direct[0]["target_message_id_raw"], str(self.mail["M024"]["Message-ID"]))
        self.assertEqual(sum(edge["relation_type"] == "reference" for edge in gaps), 31)
        self.assertEqual(len({edge["target_node_id"] for edge in gaps}), 1)
        self.assertEqual(len([node for node in nodes if node["node_kind"] == "missing"]), 1)
        bridges = _gap_contexts(messages, edges)
        self.assertEqual([(bridge["source_message_key"], bridge["ancestor_message_key"]) for bridge in bridges], [("M026", "M022")])


if __name__ == "__main__":
    unittest.main()
