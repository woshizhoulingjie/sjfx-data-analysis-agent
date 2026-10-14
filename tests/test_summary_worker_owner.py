"""Persisted worker ownership survives its internal HTTP request boundary."""
import pytest

from services.storage import Storage
from sjfx_multiuser import runtime


@pytest.fixture
def summary_worker(tmp_path, monkeypatch):
    import app as application

    store = Storage(tmp_path / "summary.db", tmp_path / "documents")
    store.save_scan({"root": str(tmp_path), "file_count": 1},
                    scan_id="private-scan", owner_id="alice")
    monkeypatch.setattr(application, "storage", store)
    # Keep the real storage lookup and owner resolver; the fixture directory
    # has no customer files or filesystem protection bindings.
    monkeypatch.setattr(application._file_protection, "guard_root", lambda _root: None)
    monkeypatch.setattr(application, "_ensure_job_active", lambda _job: None)
    yield application


def task(owner):
    return {"id": "summary-task", "scan_id": "private-scan", "owner_id": owner,
            "options": {"scan_id": "private-scan", "owner_id": "alice",
                        "role": "admin", "_worker_execution": True}}


def test_worker_uses_persisted_owner_and_restores_outer_identity(summary_worker, monkeypatch):
    app = summary_worker
    outer = {"owner_id": "administrator", "role": "admin"}
    observed = {}

    def route():
        observed["user"] = runtime.get_current_user()
        observed["scan"] = app.require_scan("private-scan")
        assert "_worker_execution" not in app.request.get_json()
        return app.jsonify({"ok": True, "summary": {"title": "private summary"}})

    monkeypatch.setattr(app, "summarize", route)
    with runtime.current_user_scope(outer):
        result = app._execute_summary_job(task("alice"))
        assert runtime.get_current_user() is outer
    assert observed["user"] == {"owner_id": "alice", "role": "user"}
    assert observed["scan"]["_scan_id"] == "private-scan"
    assert result["summary"]["title"] == "private summary"
    assert not app._summary_worker_execution.get()


def test_foreign_job_cannot_borrow_owner_from_payload_or_outer_admin(summary_worker, monkeypatch):
    app = summary_worker
    outer = {"owner_id": "administrator", "role": "admin"}

    def route():
        app.require_scan("private-scan")
        pytest.fail("Foreign worker must be rejected before generating a summary")

    monkeypatch.setattr(app, "summarize", route)
    with runtime.current_user_scope(outer):
        with pytest.raises(ValueError, match="不属于当前访问用户"):
            app._execute_summary_job(task("bob"))
        assert runtime.get_current_user() is outer
    assert not app._summary_worker_execution.get()


def test_worker_restores_identity_after_route_failure(summary_worker, monkeypatch):
    app = summary_worker

    def route():
        assert app._request_owner_id() == "alice"
        raise RuntimeError("generation failed")

    monkeypatch.setattr(app, "summarize", route)
    with runtime.current_user_scope(None):
        with pytest.raises(RuntimeError, match="generation failed"):
            app._execute_summary_job(task("alice"))
        assert runtime.get_current_user() is None
    assert not app._summary_worker_execution.get()


@pytest.mark.parametrize("owner", (None, "", " "))
def test_worker_without_persisted_owner_fails_closed(summary_worker, monkeypatch, owner):
    monkeypatch.setattr(summary_worker, "summarize", lambda: pytest.fail("Missing owner executed"))
    with pytest.raises(ValueError, match="缺少资料归属"):
        summary_worker._execute_summary_job(task(owner))
