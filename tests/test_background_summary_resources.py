"""Background summaries reuse durable sources without a 10 GiB parse reserve."""
from types import SimpleNamespace

import pytest

import worker
from services.storage import Storage


GIB = 1024 ** 3


@pytest.fixture
def resources(tmp_path, monkeypatch):
    store = Storage(tmp_path / "state.db", tmp_path / "documents")
    monkeypatch.setattr(worker, "storage", store)
    monkeypatch.setattr(worker.Config, "PARSE_TEMP_DIR", tmp_path)
    monkeypatch.setattr(worker.Config, "PARSE_TEMP_DISK_RESERVE_BYTES", GIB)
    monkeypatch.setattr(worker.Config, "MAX_CONTENT_BYTES", 10 * GIB)
    monkeypatch.setattr(worker.Config, "BACKGROUND_MAX_LOAD_RATIO", 0.9)
    monkeypatch.setattr(worker.Config, "BACKGROUND_MIN_AVAILABLE_MEMORY_MB", 2048)
    monkeypatch.setattr(worker, "available_memory_bytes", lambda: 8 * GIB)
    monkeypatch.setattr(worker.os, "getloadavg", lambda: (0.0, 0.0, 0.0), raising=False)
    disk = SimpleNamespace(free=8 * GIB)
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _path: disk)
    return store, disk


def summary_job(path="report.md", source="idle_deep_model_summary"):
    return {"scan_id": "synthetic-scan", "task_type": "generate_summary", "priority": 10,
            "options": {"path": path, "kind": "file", "workflow_source": source}}


def save_document(store, path="report.md", parser=None, coverage=None, large=False):
    store.save_document("synthetic-scan", path, {
        "text": "Synthetic source text.", "parser": parser or {"name": "text", "mode": "fast"},
        "coverage": coverage or {"complete": True}, "evidence": [],
    })
    store.save_analysis("synthetic-scan", {"policy": {"large_package": {"enabled": large}}})


def save_inventory(store, tmp_path, node, paged=False):
    if paged:
        store.save_inventory_slice("synthetic-scan", tmp_path, {}, [
            {"path": node["path"], "parent_path": ".", "payload": node},
        ], complete=True)
    else:
        store.save_scan({"root": str(tmp_path), "tree": {
            "kind": "directory", "path": ".", "children": [node],
        }}, scan_id="synthetic-scan")


@pytest.mark.parametrize("source", ["idle_deep_model_summary", "deep_node_rebuild"])
def test_persisted_file_and_node_summaries_run_below_maximum_parse_budget(resources, source):
    store, _disk = resources
    save_document(store)
    task = summary_job(source=source)
    if source == "deep_node_rebuild":
        task["options"] = {"workflow_source": source, "kind": "group", "node_id": "topic"}
    assert worker._background_resource_state(task) == (True, [])


@pytest.mark.parametrize("source", ["background_backfill", "idle_deep_backfill"])
def test_background_parsing_keeps_full_source_snapshot_budget(resources, source):
    store, _disk = resources
    save_document(store)
    task = summary_job(source=source)
    task["task_type"] = "analyze_package"
    assert worker._background_resource_state(task) == (False, ["解析临时磁盘空间不足"])


@pytest.mark.parametrize("case", [
    "missing", "structured-upgrade", "large-fast", "large-sampled",
])
def test_file_summaries_that_may_parse_keep_full_budget(resources, case):
    store, _disk = resources
    path = "report.csv" if case == "structured-upgrade" else "report.md"
    if case != "missing":
        parser = {"name": "text", "mode": "accurate"} if case == "large-sampled" else None
        coverage = {"overview_sampled": True} if case == "large-sampled" else None
        save_document(store, path=path, large=case.startswith("large"), parser=parser, coverage=coverage)
    assert worker._background_resource_state(summary_job(path)) == (False, ["解析临时磁盘空间不足"])


@pytest.mark.parametrize("path,name", [("report.json", "structured-json"), ("report.csv", "structured-table")])
def test_already_parsed_structured_sources_do_not_reserve_maximum_source_size(resources, path, name):
    store, _disk = resources
    save_document(store, path=path, parser={"name": name, "mode": "accurate"})
    assert worker._background_resource_state(summary_job(path)) == (True, [])


@pytest.mark.parametrize("source", ["idle_deep_model_summary", "deep_node_rebuild"])
def test_summary_still_requires_output_disk_reserve(resources, source):
    store, disk = resources
    save_document(store)
    disk.free = GIB - 1
    assert worker._background_resource_state(summary_job(source=source)) == (False, ["解析临时磁盘空间不足"])


@pytest.mark.parametrize("pressure,reason", [
    ("priority", "已有更高优先级任务"),
    ("cpu", "系统CPU负载较高"),
    ("memory", "系统可用内存不足"),
])
def test_persisted_summaries_keep_other_background_resource_gates(resources, monkeypatch, pressure, reason):
    store, _disk = resources
    save_document(store)
    if pressure == "priority":
        store.create_job("other-scan", task_type="conversation_turn", owner_id="test-user")
    elif pressure == "cpu":
        monkeypatch.setattr(worker.os, "getloadavg", lambda: (worker.os.cpu_count(), 0.0, 0.0), raising=False)
    else:
        monkeypatch.setattr(worker, "available_memory_bytes", lambda: GIB)
    assert worker._background_resource_state(summary_job()) == (False, [reason])


def test_foreground_work_keeps_existing_resource_behavior(resources):
    _store, disk = resources
    disk.free = 0
    task = summary_job(source="manual_selection")
    task["task_type"] = "analyze_package"
    assert worker._background_resource_state(task) == (True, [])


@pytest.mark.parametrize("paged", [False, True])
@pytest.mark.parametrize("case", ["missing", "structured-upgrade", "large-fast"])
def test_required_parse_budgets_actual_inventory_source_size(resources, tmp_path, paged, case):
    store, disk = resources
    path = "report.csv" if case == "structured-upgrade" else "report.pdf"
    if case != "missing":
        save_document(store, path, large=case == "large-fast")
    size = 20 * 1024 ** 2
    save_inventory(store, tmp_path, {"kind": "file", "path": path, "size": size}, paged)
    task = summary_job(path)
    task["options"]["size"] = 1  # Request metadata cannot lower the budget.
    disk.free = GIB + size - 1
    assert worker._background_resource_state(task) == (False, ["解析临时磁盘空间不足"])
    disk.free += 1
    assert worker._background_resource_state(task) == (True, [])


@pytest.mark.parametrize("size", [None, -1, True, "100", 10 * GIB + 1])
def test_missing_or_invalid_inventory_size_keeps_maximum_budget(resources, tmp_path, size):
    store, _disk = resources
    save_inventory(store, tmp_path, {"kind": "file", "path": "report.pdf", "size": size})
    assert worker._background_resource_state(summary_job("report.pdf")) == (False, ["解析临时磁盘空间不足"])


@pytest.mark.parametrize("node", [
    {"kind": "file", "path": "bundle.zip", "size": 100},
    {"kind": "file", "path": "bundle.tar.gz", "size": 100},
    {"kind": "logical_file", "path": "bundle.zip::report.pdf", "size": 100,
     "logical_unit": True, "logical_kind": "archive_member", "container_size": 50},
    {"kind": "file", "path": "records.csv::part1", "size": 100,
     "logical_unit": True, "logical_kind": "structured_text_partition", "container_size": 200},
])
def test_archives_and_logical_extraction_keep_conservative_budget(resources, tmp_path, node):
    store, _disk = resources
    save_inventory(store, tmp_path, node, paged=True)
    assert worker._background_resource_state(summary_job(node["path"])) == (False, ["解析临时磁盘空间不足"])


def test_source_size_does_not_lower_analyze_package_budget(resources, tmp_path):
    store, _disk = resources
    save_inventory(store, tmp_path, {"kind": "file", "path": "report.pdf", "size": 100})
    task = summary_job("report.pdf", "idle_deep_backfill")
    task["task_type"] = "analyze_package"
    assert worker._background_resource_state(task) == (False, ["解析临时磁盘空间不足"])


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("marker", ["fast_preview", "limited_by_fast_mode"])
def test_stored_pdf_preview_follows_existing_ordinary_and_large_parse_rules(resources, tmp_path, large, marker):
    store, disk = resources
    parser = {"name": "PyPDF2", "mode": "accurate"}
    coverage = {"complete": False}
    (parser if marker == "fast_preview" else coverage)[marker] = True
    save_document(store, "report.pdf", parser=parser, coverage=coverage, large=large)
    save_inventory(store, tmp_path, {"kind": "file", "path": "report.pdf", "size": 8 * GIB})
    disk.free = 8 * GIB
    before = store.get_document("synthetic-scan", "report.pdf")
    # Ordinary summarize() reuses its existing document, including its honest
    # coverage warning. Only the existing large-package path upgrades previews.
    expected = (False, ["解析临时磁盘空间不足"]) if large else (True, [])
    assert worker._background_resource_state(summary_job("report.pdf")) == expected
    assert store.get_document("synthetic-scan", "report.pdf") == before
