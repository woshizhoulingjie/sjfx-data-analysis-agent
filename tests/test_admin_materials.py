"""Small real-SQLite/FastAPI console regressions; no models or live state."""
import hashlib
import json
import sqlite3
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from sjfx_multiuser.admin_materials import AdminMaterialCatalog
from sjfx_multiuser.auth_routes import register
from sjfx_multiuser.http_adapter import SESSION_COOKIE, make_adapter
from sjfx_multiuser.ownership import resolve_owner
from sjfx_multiuser.users import UserStore
from web_compat import SJFXFastAPI


@pytest.fixture
def api(tmp_path):
    state, large = tmp_path / "agent.db", tmp_path / "large.db"
    with sqlite3.connect(state) as conn:
        conn.executescript("""
            CREATE TABLE scans(id TEXT PRIMARY KEY,root_path TEXT,owner_id TEXT,created_at TEXT,payload TEXT);
            CREATE TABLE import_tasks(scan_id TEXT UNIQUE,status TEXT);
            CREATE TABLE conversations(id TEXT PRIMARY KEY,scan_id TEXT,owner_id TEXT,title TEXT,status TEXT,
                payload TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE conversation_messages(session_id TEXT,message_id TEXT,sequence INTEGER,payload TEXT,created_at TEXT);
            CREATE TABLE document_translations(scan_id TEXT,node_path TEXT,status TEXT,updated_at TEXT);
        """)
        for owner in ("primary", "owner-a", "owner-b"):
            for index in range(28 if owner == "owner-a" else 1):
                conn.execute("INSERT INTO scans VALUES (?,?,?,?,?)", (owner + str(index), "/packages/" + owner,
                             owner, "2026-10-10 10:00:00", "deliberately invalid scan payload"))
            conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)",
                         ("session-" + owner, owner + "0", owner, "History " + owner,
                          "active", "{}", "2026-10-10", "2026-10-10"))
            conn.execute("INSERT INTO document_translations VALUES (?,?,?,?)",
                         (owner + "0", "ordinary.txt", "completed", "2026-10-10"))
            conn.execute("INSERT INTO document_translations VALUES (?,?,?,?)",
                         ("lp-" + owner, "large.txt", "failed", "2026-10-10"))
        for sequence in range(1, 111):
            message = dict(role="user" if sequence % 2 else "assistant", content="message " + str(sequence))
            conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?)",
                         ("session-owner-a", str(sequence), sequence, json.dumps(message), "2026-10-10"))
    with sqlite3.connect(large) as conn:
        conn.execute("CREATE TABLE packages(id TEXT PRIMARY KEY,root_path TEXT,owner_id TEXT,status TEXT,phase TEXT,created_at REAL,updated_at REAL)")
        for owner in ("primary", "owner-a", "owner-b"):
            conn.execute("INSERT INTO packages VALUES (?,?,?,?,?,?,?)", ("lp-" + owner, "/packages/" + owner,
                         owner, "completed", "report_ready", 1, 2))
    store = UserStore(tmp_path / "users.db")
    admin = store.create_user("console-admin", "synthetic-test-only", role="admin", owner_id="primary", allow_reserved=True)
    alice = store.create_user("alice", "synthetic-test-only", owner_id="owner-a")
    bob = store.create_user("bob", "synthetic-test-only", owner_id="owner-b")
    app = SJFXFastAPI()
    catalog = AdminMaterialCatalog(state, large)
    http, _ = make_adapter()
    register(app, store, config_owner_id="primary", adapter=http, material_catalog=catalog)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, catalog=catalog, store=store, state=state, large=large,
                              admin=admin, alice=alice, bob=bob)
    store.close()


def login(api, actor="console-admin"):
    session = api.store.login(actor, "synthetic-test-only")
    api.client.cookies.set(SESSION_COOKIE, session["session_id"])
    return session


def test_real_console_has_account_material_entry_and_counts(api):
    login(api)
    page = api.client.get("/admin")
    assert page.status_code == 200
    assert "materialPanel" in page.text
    assert '<script>' not in page.text, "production CSP forbids inline scripts"
    assert '<script src="/auth-static/admin-console.js"></script>' in page.text
    asset = api.client.get("/auth-static/admin-materials.js")
    assert asset.status_code == 200 and "javascript" in asset.headers["content-type"]
    console = api.client.get("/auth-static/admin-console.js")
    assert console.status_code == 200 and "javascript" in console.headers["content-type"]
    assert "loadMe()" in console.text and "/api/admin/users" in console.text and "查看资料" in console.text
    users = api.client.get("/api/admin/users").json()["users"]
    assert len(users) == 3
    assert all("password_hash" not in user for user in users)
    alice = next(user for user in users if user["user_id"] == api.alice["user_id"])
    assert alice["data_stats"] == dict(ordinary_packages=28, large_packages=1, conversations=1,
                                     translations=2, completed_translations=1)


@pytest.mark.parametrize("kind,total", [("ordinary", 28), ("large", 1), ("conversations", 1), ("translations", 2)])
def test_admin_catalog_lists_each_users_own_material(api, kind, total):
    login(api)
    response = api.client.get("/api/admin/users/" + api.alice["user_id"] + "/materials", params={"kind": kind})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == total and body["owner_id"] == "owner-a"
    assert all("owner-b" not in item["source_id"] for item in body["items"])


def test_all_package_pages_are_reachable_without_loading_scan_payload(api):
    login(api)
    path = "/api/admin/users/" + api.alice["user_id"] + "/materials"
    pages = [api.client.get(path, params={"offset": offset}).json() for offset in (0, 25)]
    assert pages[0]["has_more"] and not pages[1]["has_more"]
    ids = [item["id"] for page in pages for item in page["items"]]
    assert len(ids) == 28 and len(set(ids)) == 28


@pytest.mark.parametrize("actor,status", [(None, 401), ("alice", 403), ("bob", 403)])
def test_catalog_and_history_are_admin_only(api, actor, status):
    if actor:
        login(api, actor)
    path = "/api/admin/users/" + api.alice["user_id"]
    for url in ("/api/admin/users", path + "/materials", path + "/conversations/session-owner-a"):
        assert api.client.get(url).status_code == status


def test_console_inventory_does_not_change_admin_self_equals_all_view_rule(api):
    login(api)
    api.client.post("/api/admin/view-as", json={"owner_id": "primary"})
    view = api.client.get("/api/admin/view-as").json()
    assert resolve_owner(api.admin, view["viewing_owner"], "primary").owner_id is None
    own = api.client.get("/api/admin/users/" + api.admin["user_id"] + "/materials").json()
    assert own["total"] == 1
    # Account management remains global while workbench view-as is user scoped.
    api.client.post("/api/admin/view-as", json={"owner_id": "owner-b"})
    assert len(api.client.get("/api/admin/users").json()["users"]) == 3
    assert api.client.get("/api/admin/users/" + api.alice["user_id"] + "/materials").json()["total"] == 28


def test_history_messages_are_paged_and_cannot_be_misattributed(api):
    login(api)
    path = "/api/admin/users/" + api.alice["user_id"] + "/conversations/session-owner-a"
    latest = api.client.get(path).json()
    older = api.client.get(path, params={"offset": 50}).json()
    oldest = api.client.get(path, params={"offset": 100}).json()
    assert latest["total"] == 110 and latest["messages"][0]["content"] == "message 61"
    assert older["messages"][0]["content"] == "message 11"
    assert oldest["messages"][0]["content"] == "message 1" and not oldest["has_more"]
    assert api.client.get("/api/admin/users/" + api.bob["user_id"] + "/conversations/session-owner-a").status_code == 404


def test_reading_does_not_mutate_business_databases(api):
    before = [hashlib.sha256(path.read_bytes()).digest() for path in (api.state, api.large)]
    login(api)
    api.client.get("/api/admin/users")
    api.client.get("/api/admin/users/" + api.alice["user_id"] + "/materials")
    api.client.get("/api/admin/users/" + api.alice["user_id"] + "/conversations/session-owner-a")
    assert before == [hashlib.sha256(path.read_bytes()).digest() for path in (api.state, api.large)]
    with api.catalog._connect() as conn, pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM scans")


@pytest.mark.parametrize("params", [{"kind": "unknown"}, {"limit": "invalid"}, {"offset": "invalid"}])
def test_invalid_list_inputs_are_explicit(api, params):
    login(api)
    assert api.client.get("/api/admin/users/" + api.alice["user_id"] + "/materials", params=params).status_code == 400


def test_catalog_failure_preserves_account_management(api, monkeypatch):
    login(api)
    monkeypatch.setattr(api.catalog, "summaries", lambda owners: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    body = api.client.get("/api/admin/users").json()
    assert body["ok"] and len(body["users"]) == 3 and body["data_error"]


def test_legacy_inline_history_is_preserved(api):
    with sqlite3.connect(api.state) as conn:
        conn.execute("UPDATE conversations SET payload=? WHERE id='session-owner-b'", (json.dumps({"messages": [
            {"role": "user", "content": "legacy question"}, {"role": "assistant", "content": "legacy answer"}]}),))
    login(api)
    response = api.client.get("/api/admin/users/" + api.bob["user_id"] + "/conversations/session-owner-b")
    assert response.status_code == 200
    assert response.json()["messages"][-1]["content"] == "legacy answer"


def test_console_late_account_list_and_message_responses_cannot_replace_current_view():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the shipped console controller")
    result = subprocess.run([node, str(Path(__file__).with_name("admin_materials_frontend_test.js"))],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
