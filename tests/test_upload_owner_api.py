"""Owner-isolated upload HTTP regressions on the production FastAPI bridge.

Authentication is a local cookie/session stub, not a model or live user store.
All filesystem writes are restricted to pytest's temporary directory.
"""
import hashlib
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sjfx_multiuser.http_adapter import SESSION_COOKIE, build_set_cookie, make_adapter
from sjfx_multiuser.ownership import resolve_owner
from sjfx_multiuser.upload_routes import register
from web_compat import SJFXFastAPI


@pytest.fixture
def upload_api(tmp_path):
    app = SJFXFastAPI(max_content_length=4 * 1024 * 1024)
    http, kind = make_adapter()
    assert kind == "web_compat", "exercise the production HTTP adapter"
    identities = {
        "alice": {"user_id": "a", "owner_id": "owner_a", "role": "user"},
        "bob": {"user_id": "b", "owner_id": "owner_b", "role": "user"},
        "admin": {"user_id": "c", "owner_id": "owner_admin", "role": "admin"},
        "admin_self": {"user_id": "c", "owner_id": "owner_admin", "role": "admin",
                       "viewing_owner": "owner_admin"},
        "admin_view": {"user_id": "c", "owner_id": "owner_admin", "role": "admin",
                       "viewing_owner": "owner_b"},
    }
    sessions = {}

    def session():
        return sessions.get(http.cookie(SESSION_COOKIE))

    def request_owner():
        current = session()
        return resolve_owner(current, (current or {}).get("viewing_owner"),
                             "configured_fallback").owner_id

    @app.route("/_test/login", methods=["POST"])
    def login_stub():
        body = http.json_body() or {}
        identity = body.get("identity")
        if identity not in identities or body.get("password") != "synthetic-test-only":
            return http.json({"ok": False}, 401)
        token = "synthetic-" + identity
        sessions[token] = dict(identities[identity])
        return http.json({"ok": True}, set_cookie=build_set_cookie(SESSION_COOKIE, token))

    root = tmp_path / "uploads"
    scan_root = tmp_path / "shared_scan_root"
    scan_root.mkdir()
    registered = register(app, str(root), allowed_roots=(str(scan_root),),
                          session_resolver=session, request_owner_id=request_owner)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, root=root, scan_root=scan_root,
                              manager=registered["manager"], sessions=sessions)


def login(api, identity="alice"):
    response = api.client.post("/_test/login", json={
        "identity": identity, "password": "synthetic-test-only",
    })
    assert response.status_code == 200


def begin(api, data=b"binary\x00\xff", rel_path="folder/a.bin", **extra):
    response = api.client.post("/api/uploads", json={
        "files": [{"rel_path": rel_path, "size": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}], **extra,
    })
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


def complete(api, session_id, data=b"binary\x00\xff"):
    response = api.client.put("/api/uploads/%s/chunks/0" % session_id,
                             params={"chunk_index": 0,
                                     "sha256": hashlib.sha256(data).hexdigest()},
                             content=data, headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 200, response.text
    assert response.json()["file_complete"]


def test_default_upload_is_private_binary_exact_and_finalize_idempotent(upload_api):
    api = upload_api
    login(api)
    payload = b"binary\x00\xff"
    sid = begin(api, payload, owner_id="owner_b")
    complete(api, sid, payload)
    response = api.client.post("/api/uploads/%s/finalize" % sid,
                               json={"owner_id": "owner_b"},
                               headers={"X-Owner-ID": "owner_b"})
    assert response.status_code == 200, response.text
    expected = api.root / "assembled" / "owner_a" / sid
    assert response.json()["dest_dir"] == str(expected)
    assert response.json()["files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert (expected / "folder/a.bin").read_bytes() == payload
    assert not (api.root / "assembled" / "owner_b").exists()
    again = api.client.post("/api/uploads/%s/finalize" % sid,
                            json={"dest": str(expected)})
    assert again.status_code == 200, again.text
    assert again.json()["files"] == response.json()["files"]


@pytest.mark.parametrize("identity", ["admin", "admin_self"])
def test_admin_all_or_self_uploads_only_as_login_owner(upload_api, identity):
    api = upload_api
    login(api, identity)
    sid = begin(api)
    complete(api, sid)
    response = api.client.post("/api/uploads/%s/finalize" % sid, json={})
    assert response.status_code == 200, response.text
    assert response.json()["dest_dir"] == str(api.root / "assembled" / "owner_admin" / sid)
    assert not (api.root / "assembled" / "owner_b").exists()


def test_admin_view_as_remains_read_only_for_uploads(upload_api):
    api = upload_api
    login(api, "admin")
    sid = begin(api)
    complete(api, sid)
    login(api, "admin_view")
    attempts = [
        api.client.post("/api/uploads", json={"files": [{"rel_path": "a", "size": 0}]}),
        api.client.get("/api/uploads/%s" % sid),
        api.client.put("/api/uploads/%s/chunks/0" % sid, content=b"x"),
        api.client.post("/api/uploads/%s/finalize" % sid, json={}),
        api.client.delete("/api/uploads/%s" % sid),
    ]
    assert all(response.status_code == 403 for response in attempts)
    assert not (api.root / "assembled").exists()


def test_anonymous_requests_cannot_use_owner_fields_or_stale_runtime(upload_api, monkeypatch):
    from sjfx_multiuser import upload_routes

    api = upload_api
    monkeypatch.setattr(upload_routes, "get_current_user", lambda: {"owner_id": "owner_a"})
    attempts = [
        api.client.post("/api/uploads?owner_id=owner_a",
                        json={"owner_id": "owner_a", "files": [{"rel_path": "a", "size": 0}]}),
        api.client.get("/api/uploads/unknown"),
        api.client.put("/api/uploads/unknown/chunks/0", content=b"x"),
        api.client.post("/api/uploads/unknown/finalize", json={"dest": str(api.scan_root)}),
        api.client.delete("/api/uploads/unknown"),
    ]
    assert all(response.status_code == 401 for response in attempts)
    assert list(api.root.iterdir()) == []


def test_other_login_cannot_read_write_finalize_or_abort_session(upload_api):
    api = upload_api
    login(api)
    sid = begin(api)
    complete(api, sid)
    login(api, "bob")
    attempts = [
        api.client.get("/api/uploads/%s?owner_id=owner_a" % sid),
        api.client.put("/api/uploads/%s/chunks/0" % sid, content=b"other"),
        api.client.post("/api/uploads/%s/finalize" % sid, json={"owner_id": "owner_a"}),
        api.client.delete("/api/uploads/%s" % sid),
    ]
    assert all(400 <= response.status_code < 500 for response in attempts)
    assert (api.root / "owner_a" / sid / "files/0.done").read_bytes() == b"binary\x00\xff"
    assert not (api.root / "assembled").exists()


@pytest.mark.parametrize("destination", [
    "other_owner", "own_other_session", "other_staging", "own_staging",
    "allowed_scan_root", "allowed_scan_escape", "prefix_sibling", "relative",
])
def test_finalize_rejects_shared_roots_other_owners_and_staging(upload_api, destination):
    api = upload_api
    login(api, "bob")
    other_sid = begin(api)
    login(api)
    sid = begin(api)
    complete(api, sid)
    private = api.root / "assembled" / "owner_a" / sid
    destinations = {
        "other_owner": api.root / "assembled" / "owner_b" / other_sid,
        "own_other_session": api.root / "assembled" / "owner_a" / "another_session",
        "other_staging": api.root / "owner_b" / other_sid / "files",
        "own_staging": api.root / "owner_a" / sid / "files",
        "allowed_scan_root": api.scan_root / "owner_a",
        "allowed_scan_escape": api.scan_root / ".." / "outside_scan",
        "prefix_sibling": private.with_name(sid + "_suffix"),
        "relative": "assembled/owner_a/relative",
    }
    response = api.client.post("/api/uploads/%s/finalize" % sid,
                               json={"dest": str(destinations[destination])})
    assert response.status_code == 403, response.text
    assert response.json()["ok"] is False
    assert (api.root / "owner_a" / sid / "files/0.done").read_bytes() == b"binary\x00\xff"
    assert not private.exists()
    assert not (api.root / "owner_b" / other_sid / "files/folder").exists()
    assert list(api.scan_root.iterdir()) == []


def test_empty_scan_allowlist_still_does_not_authorize_arbitrary_destination(upload_api):
    api = upload_api
    login(api)
    sid = begin(api)
    complete(api, sid)
    # Use a second real app with no scanner roots and the same upload storage.
    second = SJFXFastAPI()
    http, kind = make_adapter()
    assert kind == "web_compat"
    register(second, str(api.root), allowed_roots=(),
             session_resolver=lambda: api.sessions.get(http.cookie(SESSION_COOKIE)),
             request_owner_id=lambda: "owner_a")
    with TestClient(second) as client:
        response = client.post("/api/uploads/%s/finalize" % sid,
                               json={"dest": str(api.scan_root / "anywhere")},
                               headers={"Cookie": SESSION_COOKIE + "=synthetic-alice"})
    assert response.status_code == 403, response.text
    assert list(api.scan_root.iterdir()) == []


def test_existing_destination_file_is_not_overwritten(upload_api):
    api = upload_api
    login(api)
    incoming = b"incoming"
    sid = begin(api, incoming, rel_path="a.bin")
    complete(api, sid, incoming)
    private = api.root / "assembled" / "owner_a" / sid
    private.mkdir(parents=True)
    existing = private / "a.bin"
    existing.write_bytes(b"original")  # equal size is not permission to replace
    response = api.client.post("/api/uploads/%s/finalize" % sid, json={})
    assert response.status_code == 400, response.text
    assert existing.read_bytes() == b"original"
    assert (api.root / "owner_a" / sid / "files/0.done").read_bytes() == incoming


@pytest.mark.parametrize("level", ["assembled", "owner", "session", "nested"])
def test_finalize_rejects_real_directory_links(upload_api, level):
    api = upload_api
    login(api)
    sid = begin(api)
    complete(api, sid)
    private = api.root / "assembled" / "owner_a" / sid
    linked = {"assembled": api.root / "assembled", "owner": private.parent,
              "session": private, "nested": private / "folder"}[level]
    linked.parent.mkdir(parents=True, exist_ok=True)
    try:
        linked.symlink_to(api.scan_root, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip("native directory symlink unavailable: %s" % exc)
        # Junction creation needs no developer-mode symlink privilege on NTFS.
        # Both literal paths are created by pytest under this test's temp root.
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(api.scan_root)],
                                 capture_output=True, timeout=10)
        assert created.returncode == 0, created.stderr.decode(errors="replace")
    response = api.client.post("/api/uploads/%s/finalize" % sid, json={})
    assert response.status_code in (400, 403), response.text
    assert list(api.scan_root.iterdir()) == []
    assert (api.root / "owner_a" / sid / "files/0.done").read_bytes() == b"binary\x00\xff"


def test_finalize_rejects_reparse_ancestor_before_assembly(upload_api, monkeypatch):
    from sjfx_multiuser import upload_routes

    api = upload_api
    login(api)
    sid = begin(api)
    complete(api, sid)
    private = api.root / "assembled" / "owner_a" / sid
    original_lstat = os.lstat
    forbidden = os.path.normcase(str(private.parent))

    def reparse_lstat(path, *args, **kwargs):
        if os.path.normcase(str(path)) == forbidden:
            return SimpleNamespace(st_mode=stat.S_IFDIR,
                                   st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(upload_routes.os, "lstat", reparse_lstat)
    response = api.client.post("/api/uploads/%s/finalize" % sid, json={})
    assert response.status_code == 403, response.text
    assert not private.exists()


@pytest.mark.parametrize("body", [[], ["unexpected"], "text", 42, False])
def test_begin_and_finalize_reject_non_object_json(upload_api, body):
    api = upload_api
    login(api)
    sid = begin(api)
    assert api.client.post("/api/uploads", json=body).status_code == 400
    assert api.client.post("/api/uploads/%s/finalize" % sid, json=body).status_code == 400


@pytest.mark.parametrize("digest", ["abc", "x" * 64, 123, ["0" * 64]])
def test_begin_rejects_invalid_full_hash_without_creating_session(upload_api, digest):
    api = upload_api
    login(api)
    response = api.client.post("/api/uploads", json={
        "files": [{"rel_path": "a.bin", "size": 1, "sha256": digest}],
    })
    assert response.status_code == 400, response.text
    assert list(api.root.iterdir()) == []


def test_bad_file_shape_dest_shape_json_and_chunk_hash_are_client_errors(upload_api):
    api = upload_api
    login(api)
    assert api.client.post("/api/uploads", json={"files": [None]}).status_code == 400
    assert api.client.post("/api/uploads", content=b"{broken",
                           headers={"Content-Type": "application/json"}).status_code == 400
    sid = begin(api)
    for dest in (123, [], {}, False):
        assert api.client.post("/api/uploads/%s/finalize" % sid,
                               json={"dest": dest}).status_code == 400
    assert api.client.put("/api/uploads/%s/chunks/0" % sid,
                          content=b"binary\x00\xff", params={"sha256": "bad"}).status_code == 400
    assert api.client.get("/api/uploads/%s" % sid).json()["received_bytes"] == 0


def test_declared_full_hash_mismatch_is_rejected_over_http(upload_api):
    api = upload_api
    login(api)
    response = api.client.post("/api/uploads", json={"files": [{
        "rel_path": "a.bin", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest(),
    }]})
    assert response.status_code == 200, response.text
    sid = response.json()["session_id"]
    wrong = api.client.put("/api/uploads/%s/chunks/0" % sid,
                           content=b"xyz", params={"chunk_index": 0,
                                                   "sha256": hashlib.sha256(b"xyz").hexdigest()})
    assert wrong.status_code == 400, wrong.text
    assert not api.client.get("/api/uploads/%s" % sid).json()["complete"]
    assert api.client.post("/api/uploads/%s/finalize" % sid, json={}).status_code == 400
    assert not (api.root / "assembled").exists()


def test_empty_file_can_finalize_and_reports_actual_hash(upload_api):
    api = upload_api
    login(api)
    sid = begin(api, b"", rel_path="empty.txt")
    response = api.client.post("/api/uploads/%s/finalize" % sid, json={})
    assert response.status_code == 200, response.text
    assert (api.root / "assembled" / "owner_a" / sid / "empty.txt").read_bytes() == b""
    assert response.json()["files"][0]["sha256"] == hashlib.sha256(b"").hexdigest()
