"""Real server SQLite regressions for create-once selection initialization.

Prepared for execution in the isolated server test environment. These tests
are intentionally not executed by the local preparation step.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from services.storage import Storage
from sjfx_protection import business


SCAN = "selection-race-scan"
FILES = ["a.txt", "b.txt"]


@pytest.fixture
def stores(tmp_path, monkeypatch):
    # This regression covers the ordinary selection contract with protection
    # OFF; neither global configured DATA nor any running instance is used.
    monkeypatch.setattr(business, "enabled", lambda: False)
    database = tmp_path / "selection.sqlite3"
    first, second = Storage(database), Storage(database)
    first.save_scan({"root": str(tmp_path / "materials"), "total_files": 2,
                     "tree": {"path": ".", "type": "directory", "children": []}},
                    scan_id=SCAN, owner_id="selection-test-owner")
    first.save_file_workflow_states(SCAN, [
        {"path": path, "selection_state": "pending", "promotion_allowed": True,
         "safety_status": "no_detection"} for path in FILES
    ])
    with first._connect() as db:
        db.executemany(
            "INSERT INTO inventory_entries(scan_id,node_path,parent_path,position,kind,payload) "
            "VALUES (?,?,'.',?,'file',?)",
            [(SCAN, path, index, json.dumps({"size": 10 + index, "mtime_ns": 123}))
             for index, path in enumerate(FILES)],
        )
    return first, second


def stored_selection(store):
    """Include timestamps and every persisted column in preservation checks."""
    with store._connect() as db:
        rows = db.execute("SELECT * FROM scan_selections WHERE scan_id=?", (SCAN,)).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


def test_two_independent_initializers_create_only_version_one(stores, monkeypatch):
    barrier = threading.Barrier(2)
    assert stores[0].lock is not stores[1].lock
    for store in stores:
        original = store.default_scan_selection

        def simultaneous_defaults(scan_id, original=original):
            defaults = original(scan_id)
            # Both callers have observed no selection before either INSERT.
            barrier.wait(timeout=10)
            return defaults

        monkeypatch.setattr(store, "default_scan_selection", simultaneous_defaults)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.ensure_default_scan_selection, SCAN) for store in stores]
        selections = [future.result(timeout=30) for future in futures]
    assert [selection["version"] for selection in selections] == [1, 1]
    assert all(selection["status"] == "draft" for selection in selections)
    assert all(selection["included_paths"] == FILES for selection in selections)
    assert selections[0] == selections[1]
    assert stored_selection(stores[0])["version"] == 1


@pytest.mark.parametrize("status", ["draft", "confirmed", "running", "completed"])
def test_ensure_never_changes_existing_user_selection(stores, status):
    first, second = stores
    initial = first.ensure_default_scan_selection(SCAN)
    edited = first.save_scan_selection(
        SCAN, ["b.txt"], ["a.txt"], {"user_rule": "keep-b"},
        status=status, expected_version=initial["version"],
    )
    assert edited["version"] == 2
    before = stored_selection(first)
    assert second.ensure_default_scan_selection(SCAN) == edited
    assert first.ensure_default_scan_selection(SCAN) == edited
    assert stored_selection(second) == before


def test_late_initializer_cannot_overwrite_user_edit(stores, monkeypatch):
    initializer, editor = stores
    defaults_ready, release_initializer = threading.Event(), threading.Event()
    original = initializer.default_scan_selection

    def delayed_defaults(scan_id):
        defaults = original(scan_id)
        defaults_ready.set()
        assert release_initializer.wait(timeout=10)
        return defaults

    monkeypatch.setattr(initializer, "default_scan_selection", delayed_defaults)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(initializer.ensure_default_scan_selection, SCAN)
        try:
            assert defaults_ready.wait(timeout=10)
            draft = editor.save_scan_selection(SCAN, FILES, [], {"user_rule": "all"})
            edited = editor.save_scan_selection(
                SCAN, ["a.txt"], ["b.txt"], {"user_rule": "keep-a"},
                status="confirmed", expected_version=draft["version"],
            )
            before = stored_selection(editor)
        finally:
            release_initializer.set()
        assert pending.result(timeout=30) == edited
    assert edited["version"] == 2
    assert stored_selection(initializer) == before


def test_genuinely_stale_confirmation_is_still_rejected(stores):
    first, second = stores
    initial = first.ensure_default_scan_selection(SCAN)
    edited = second.save_scan_selection(
        SCAN, ["b.txt"], ["a.txt"], {"user_rule": "newer-edit"},
        status="draft", expected_version=initial["version"],
    )
    assert edited["version"] == 2
    before = stored_selection(first)
    with pytest.raises(ValueError, match="分析范围已被其他操作更新"):
        first.save_scan_selection(
            SCAN, initial["included_paths"], initial["excluded_paths"], initial["rules"],
            status="confirmed", expected_version=initial["version"],
        )
    assert stored_selection(first) == before
