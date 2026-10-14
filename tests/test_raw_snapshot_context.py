"""Protected raw responses retain their context across the worker/ASGI boundary."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from starlette.concurrency import run_in_threadpool

import app as web
from services.package_analysis import _secure_source_snapshot
from sjfx_protection import business
from sjfx_protection.registration import PackageProtection
from sjfx_protection.store import Store


@pytest.fixture
def protected_response(tmp_path, monkeypatch):
    """Use real authorization, immutable snapshots and post-read verification."""
    root = tmp_path / "sources"
    root.mkdir()
    source = root / "plain.txt"
    data = b"Synthetic protected streaming bytes.\n" * 5000
    source.write_bytes(data)
    monkeypatch.setenv("SJFX_PROTECTION_TEST_STATIC_ONLY", "1")
    monkeypatch.setattr(web.Config, "SCAN_ALLOWED_ROOTS", ())
    store = Store(tmp_path / "protection")
    package = PackageProtection(
        store, [str(root)], authorize_root=lambda owner, path: owner == "alice",
        authorize_owner=lambda owner, user: owner == "alice",
    )
    with store.db(True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (
            "package-root-grants:alice", json.dumps([str(root)]),
        ))
    package.update_policy("admin", True, {
        "enabled": True, "allowed_registration_modes": ["local"],
        "inspection_profile": {"required_engines": ["static"]},
    }, "isolated raw response regression")
    registration = package.register("alice", "u1", str(root), "test", "local")
    package.process_job(registration["job_id"])
    monkeypatch.setattr(business, "_configured", SimpleNamespace(
        enabled=True, package=package, store=store,
    ))
    context = business.decision_context(package.require_operation(
        "alice", root=str(root), operation="read",
    ))
    rechecks = []
    original_recheck = business.recheck_active_sources

    def recheck(operation=None):
        rechecks.append(business.execution.get())
        return original_recheck(operation)

    monkeypatch.setattr(business, "recheck_active_sources", recheck)
    node = {"path": source.name, "name": source.name, "size": len(data)}

    class Snapshot:
        def __init__(self):
            self.delegate = _secure_source_snapshot(root, node)
            self.enter_count = 0
            self.exits = []

        def __enter__(self):
            self.enter_count += 1
            self.path = self.delegate.__enter__()
            return self.path

        def __exit__(self, *args):
            self.exits.append(args)
            return self.delegate.__exit__(*args)

    return SimpleNamespace(context=context, snapshot=Snapshot(), data=data,
                           rechecks=rechecks)


@pytest.mark.parametrize("outcome", ["success", "send_failure", "cancel", "disconnect"])
def test_protected_snapshot_closes_in_its_context_after_asgi_send(protected_response, outcome):
    case = protected_response
    worker_contexts = []
    body_messages = []

    async def run():
        with business.activate(case.context):
            async_thread = threading.get_ident()
            body_started = asyncio.Event()

            def build():
                assert threading.get_ident() != async_thread
                response = web._raw_snapshot_response(case.snapshot, "plain.txt")
                worker_contexts.append(business.execution.get())
                return response

            response = await run_in_threadpool(build)
            assert business.execution.get() is case.context
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "sandbox" in response.headers["content-security-policy"]

            async def send(message):
                assert business.execution.get() is case.context
                assert case.snapshot.path.exists()
                assert not case.snapshot.exits
                if message["type"] == "http.response.body":
                    body_messages.append(message)
                    if outcome == "send_failure":
                        raise RuntimeError("synthetic disconnected sender")
                    if outcome == "cancel":
                        asyncio.current_task().cancel()
                        await asyncio.sleep(0)
                    if outcome == "disconnect":
                        body_started.set()
                        await asyncio.sleep(0)

            async def receive():
                await body_started.wait()
                return {"type": "http.disconnect"}

            try:
                await response({
                    "type": "http", "method": "GET", "headers": [],
                    "asgi": {"spec_version": "2.3" if outcome == "disconnect" else "2.4"},
                }, receive, send)
            finally:
                assert business.execution.get() is case.context

    original_context = business.execution.get()
    if outcome in {"success", "disconnect"}:
        asyncio.run(run())
        sent = b"".join(message["body"] for message in body_messages)
        if outcome == "success":
            assert len(body_messages) > 1
            assert sent == case.data
        else:
            assert 0 < len(sent) < len(case.data)
    else:
        expected = RuntimeError if outcome == "send_failure" else asyncio.CancelledError
        match = "synthetic disconnected sender" if outcome == "send_failure" else None
        with pytest.raises(expected, match=match):
            asyncio.run(run())
    assert business.execution.get() is original_context
    assert worker_contexts == [case.context]
    assert worker_contexts[0] is case.context
    assert case.snapshot.enter_count == 1
    assert case.snapshot.exits == [(None, None, None)]
    assert len(case.rechecks) == 1
    assert case.rechecks[0]["registration_id"] == case.context["registration_id"]
    # Protected snapshots are durable verified artifacts, not disposable scratch.
    assert case.snapshot.path.exists()


def test_protected_snapshot_constructor_failure_closes_once_and_restores_context(
        protected_response, monkeypatch):
    from fastapi.responses import FileResponse

    case = protected_response

    def fail_constructor(*args, **kwargs):
        raise RuntimeError("synthetic response construction failure")

    monkeypatch.setattr(FileResponse, "__init__", fail_constructor)

    async def run():
        def build():
            with business.activate(case.context):
                with pytest.raises(RuntimeError, match="synthetic response construction failure"):
                    web._raw_snapshot_response(case.snapshot, "plain.txt")
                assert business.execution.get() is case.context

        await run_in_threadpool(build)

    original_context = business.execution.get()
    asyncio.run(run())
    assert business.execution.get() is original_context
    assert case.snapshot.enter_count == 1
    assert case.snapshot.exits == [(None, None, None)]
    assert len(case.rechecks) == 1
    assert case.snapshot.path.exists()
