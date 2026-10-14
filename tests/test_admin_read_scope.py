"""Read scopes use authenticated filters; writes keep concrete stored owners."""
from contextlib import contextmanager
import json
from unittest.mock import Mock, patch

import pytest

import app as web
from services.large_package_isolated import LargePackageStore
from services.storage import Storage
from sjfx_multiuser.ownership import resolve_owner


@pytest.fixture
def material(tmp_path, monkeypatch):
    # This fixture's sources can live outside the configured deployment roots
    # (e.g. --basetemp on another drive). Authorize only its own temporary tree.
    monkeypatch.setattr(web.Config, 'SCAN_ALLOWED_ROOTS', (tmp_path.resolve(),))
    storage = Storage(tmp_path / "state.db", tmp_path / "documents")
    packages = LargePackageStore(tmp_path / "large.db")
    turns = {}
    large = {}
    for suffix in ("a", "b"):
        root = tmp_path / suffix
        root.mkdir()
        storage.save_scan({"root": str(root), "tree": {}, "file_count": 0},
                          scan_id="scan-" + suffix, owner_id="owner-" + suffix)
        storage.save_conversation({"session_id": "session-" + suffix,
                                   "scan_id": "scan-" + suffix, "messages": []}, "owner-" + suffix)
        turn, _ = storage.create_conversation_turn(
            "session-" + suffix, "scan-" + suffix, "owner-" + suffix,
            "question " + suffix, {"kind": "package"},
        )
        turns[suffix] = turn
        large[suffix] = packages.create(root, owner_id="owner-" + suffix)["id"]
    with patch.object(web, "storage", storage), \
            patch.object(web, "large_package_store", packages), \
            patch.object(web, "_file_protection", Mock()), \
            patch.object(web, "_conversation_index_status", return_value={"usable": True, "ready": True}):
        yield storage, packages, turns, large


@contextmanager
def actor(role="admin", owner="primary", viewing=None):
    decision = resolve_owner({"role": role, "owner_id": owner}, viewing, "primary")
    with patch.object(web, "_request_owner_id", return_value=decision.owner_id):
        yield


def call(handler, *args, query="", method="GET", payload=None):
    with web.app.test_request_context("/api/test" + query, method=method, json=payload):
        response = handler(*args)
    if isinstance(response, tuple):
        response, status = response
    else:
        status = response.status_code
    return status, response.get_json() if hasattr(response, "get_json") else response


def test_admin_all_sees_other_owner_packages_and_conversations(material):
    _, _, turns, large = material
    with actor():
        status, history = call(web.list_large_package_history, query="?limit=2")
        assert status == 200
        assert {item["scan_id"] for item in history["items"]} == set(large.values())
        assert web._large_package_owned(large["b"])["owner_id"] == "owner-b"
        assert web._conversation_package_record(large["b"])["_owner_id"] == "owner-b"
        assert call(web.conversations_for_scan, "scan-b")[1]["items"][0]["session_id"] == "session-b"
        status, session = call(web.get_conversation, "session-b", query="?scan_id=scan-b")
        assert status == 200
        assert session["session"]["session_id"] == "session-b"
        assert session["turns"][0]["id"] == turns["b"]["id"]
        assert call(web.get_conversation_turn, turns["b"]["id"])[0] == 200
        assert call(web.get_conversation_turn_events, turns["b"]["id"])[0] == 200


@pytest.mark.parametrize("role,owner,viewing", [
    ("user", "owner-a", None), ("user", "owner-a", "owner-b"),
    ("admin", "primary", "owner-a"),
])
def test_user_and_selected_admin_cannot_read_foreign_material(material, role, owner, viewing):
    _, _, turns, large = material
    with actor(role, owner, viewing):
        _, history = call(web.list_large_package_history)
        assert [item["scan_id"] for item in history["items"]] == [large["a"]]
        with pytest.raises(ValueError):
            web._large_package_owned(large["b"])
        assert call(web.conversations_for_scan, "scan-b")[0] == 404
        assert call(web.get_conversation, "session-b", query="?scan_id=scan-b")[0] == 404
        assert call(web.get_conversation_turn, turns["b"]["id"])[0] == 404
        assert call(web.get_conversation_turn_events, turns["b"]["id"])[0] == 404
        assert call(web.get_conversation, "session-a", query="?scan_id=scan-a")[0] == 200


def test_session_and_source_are_both_authorized(material):
    storage, _, _, _ = material
    storage.save_conversation({"session_id": "bad-source", "scan_id": "scan-b", "messages": []}, "owner-a")
    with actor("user", "owner-a"):
        assert call(web.get_conversation, "bad-source")[0] == 404
    with actor():
        assert call(web.get_conversation, "session-b", query="?scan_id=scan-a")[0] == 404


def test_existing_session_turn_keeps_its_owner_in_admin_all_view(material):
    storage, _, _, _ = material
    with actor():
        status, result = call(web.create_conversation_turn, "session-b", method="POST",
                              payload={"scan_id": "scan-b", "question": "continue"})
        assert status == 202
        assert result["turn"]["owner_id"] == "owner-b"
        assert storage.get_job(result["job_id"])["owner_id"] == "owner-b"
        assert web._effective_owner() == web._configured_owner_id
    with actor("user", "owner-a"):
        assert call(web.create_conversation_turn, "session-b", method="POST",
                    payload={"scan_id": "scan-b", "question": "forbidden"})[0] == 400


def test_lightweight_question_authorization_does_not_hydrate_scan(material):
    storage, _, _, _ = material
    with actor(), patch.object(storage, "get_scan", side_effect=AssertionError("full scan loaded")):
        assert call(web.get_conversation, "session-b", query="?scan_id=scan-b")[0] == 200
        assert web._conversation_package_record("scan-b")["root"].endswith("b")
    with storage._connect() as conn:
        conn.execute("UPDATE scans SET payload='broken json' WHERE id='scan-b'")
    with actor():
        assert web._conversation_package_record("scan-b")["_owner_id"] == "owner-b"


def test_session_list_all_filter_never_becomes_literal_none(material):
    storage, _, _, _ = material
    storage.save_conversation({"session_id": "admin-on-b", "scan_id": "scan-b", "messages": []}, "primary")
    assert {row["session_id"] for row in storage.list_conversations("scan-b", None)} == {
        "session-b", "admin-on-b",
    }
    assert [row["session_id"] for row in storage.list_conversations("scan-b", "owner-b")] == ["session-b"]
    with actor():
        assert call(web.get_conversation, "admin-on-b", query="?scan_id=scan-b")[0] == 200


def test_lightweight_scope_keeps_legacy_file_metadata(material, tmp_path):
    storage, _, _, _ = material
    storage.save_scan({"root": str(tmp_path / "b"), "tree": {
        "kind": "directory", "path": ".", "children": [
            {"kind": "file", "path": "note.txt", "extension": ".txt"},
            {"kind": "file", "path": "table.csv", "extension": ".csv"},
        ],
    }}, scan_id="scan-b", owner_id="owner-b")
    with actor(), patch.object(storage, "get_scan", side_effect=AssertionError("full scan loaded")):
        record = web._conversation_package_record("scan-b")
        scope = web._resolved_conversation_scope("scan-b", record, {
            "kind": "file_type", "value": ".txt", "constraints": {"dimension": "format"},
        })
    assert list(scope.source_paths) == ["note.txt"]


def test_latest_and_older_message_pages_have_matching_turns(material):
    storage, _, _, _ = material
    with storage._connect() as conn:
        for sequence in range(2, 126):
            turn_id = "long-{}".format(sequence)
            conn.execute(
                "INSERT INTO conversation_turns(id,session_id,scan_id,owner_id,sequence,status,stage,question,scope,"
                "user_message_id,assistant_message_id) VALUES (?,?,?, ?,?,'completed','completed',?,'{}',?,?)",
                (turn_id, "session-b", "scan-b", "owner-b", sequence, "q", turn_id + "u", turn_id + "a"),
            )
            for offset, role in enumerate(("user", "assistant")):
                message_id = turn_id + role
                conn.execute(
                    "INSERT INTO conversation_messages(session_id,message_id,role,turn_id,sequence,payload) "
                    "VALUES (?,?,?,?,?,?)",
                    ("session-b", message_id, role, turn_id, sequence * 2 - 1 + offset,
                     json.dumps({"message_id": message_id, "role": role, "metadata": {"turn_id": turn_id}})),
                )
    with actor():
        status, latest = call(web.get_conversation, "session-b", query="?scan_id=scan-b")
        assert status == 200
        assert [turn["sequence"] for turn in latest["turns"]] == list(range(76, 126))
        cursor = latest["session"]["message_page"]["before_sequence"]
        _, older = call(web.get_conversation, "session-b", query="?scan_id=scan-b&before_sequence={}".format(cursor))
        assert [turn["sequence"] for turn in older["turns"]] == list(range(26, 76))
        for result in (latest, older):
            assert {turn["id"] for turn in result["turns"]} == {
                message["metadata"]["turn_id"] for message in result["session"]["messages"]
            }
    assert [turn["sequence"] for turn in storage.list_conversation_turns("session-b", "owner-b")] == list(range(26, 126))
    assert storage.list_conversation_turns("session-b", "owner-a", turn_ids=["long-125"]) == []


def test_artifact_owner_and_download_ticket_are_bound_to_actual_owner(material, tmp_path):
    storage, _, _, _ = material
    outputs = tmp_path / "outputs"
    (outputs / "owner-b").mkdir(parents=True)
    (outputs / "owner-b" / "report.docx").write_bytes(b"report")
    storage.save_artifact("report.docx", "owner-b", scan_id="scan-b", kind="overview_report")
    with patch.object(web.Config, "OUTPUT_DIR", outputs), actor():
        status, result = call(web.create_download_ticket, method="POST", payload={"filename": "report.docx"})
        assert status == 200
        ticket = result["download_url"].split("ticket=", 1)[1]
        assert not storage.consume_download_ticket(ticket, "report.docx", "primary")
        assert storage.consume_download_ticket(ticket, "report.docx", "owner-b")
        assert call(web.outputs, "report.docx")[0] == 200
    with patch.object(web.Config, "OUTPUT_DIR", outputs), actor("user", "owner-a"):
        assert call(web.create_download_ticket, method="POST", payload={"filename": "report.docx"})[0] == 404
        assert call(web.outputs, "report.docx")[0] == 404


def test_identity_errors_do_not_fall_back_to_a_wider_scope(material):
    storage, _, _, _ = material
    with patch.object(web, "_request_owner_id", side_effect=RuntimeError("identity unavailable")), \
            patch.object(storage, "get_conversation_identity") as read:
        with pytest.raises(RuntimeError):
            web._conversation_identity_owned("session-b")
        read.assert_not_called()


def test_material_fixture_does_not_authorize_outside_directory(material, tmp_path_factory):
    _, packages, _, _ = material
    outside = tmp_path_factory.mktemp('admin-scope-outside')
    with pytest.raises(ValueError, match='白名单'):
        packages.create(outside, owner_id='owner-a')
