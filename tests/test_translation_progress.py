"""Deterministic translation job progress without model calls or workers."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as web


def checkpoint_state(completed, required=4, status="partial"):
    return {
        "status": status,
        "translated_text": "已保存的可读译文",
        "progress": {"completed_units": completed, "required_units": required},
        "units": [{"translation_required": True, "status": "completed"}],
    }


@pytest.fixture
def translation_runtime(monkeypatch):
    store = Mock()
    store.get_translation.return_value = None
    store.get_file_state.return_value = {"status": "completed"}
    store.get_analysis.return_value = {}
    case = SimpleNamespace(store=store, large=False, now=0.0, timelines={})

    def translate(document, checkpoint_callback, **_kwargs):
        timeline = case.timelines[document["text"]]
        for now, payload in timeline:
            case.now = now
            checkpoint_callback(deepcopy(payload))
        return deepcopy(timeline[-1][1])

    case.translate = Mock(side_effect=translate)
    monkeypatch.setattr(web, "storage", store)
    # Replace the app's time binding so the test does not affect other modules.
    monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: case.now))
    monkeypatch.setattr(web, "_is_large_translation_source", lambda _scan_id: case.large)
    monkeypatch.setattr(web, "_translation_document", lambda _scan_id, path, _level: {"text": path})
    monkeypatch.setattr(web, "_large_translation_document",
                        lambda _scan_id, path, **_kwargs: {"text": path})
    monkeypatch.setattr(web, "file_translation_service", SimpleNamespace(translate_document=case.translate))
    monkeypatch.setattr(web, "_translate_file_document_serialized", case.translate)
    monkeypatch.setattr(web, "require_scan", lambda _scan_id: {"root": "synthetic-root"})
    monkeypatch.setattr(web, "_translation_inventory_node", Mock())
    monkeypatch.setattr(web, "_large_translation_target", Mock())
    monkeypatch.setattr(web, "_ensure_job_active", Mock())
    monkeypatch.setattr(web, "refresh_package_coverage", Mock())
    monkeypatch.setattr(web.Config, "TRANSLATION_PACKAGE_BATCH_FILES", 10)
    return case


@pytest.mark.parametrize("backend", ["vllm", "offline_quickmt"])
def test_translation_checkpoints_defer_evidence_publication_until_final_save(translation_runtime, backend):
    case = translation_runtime
    case.timelines["fixture.txt"] = [
        (10.0, checkpoint_state(1)),
        (11.1, checkpoint_state(4, status="completed")),
    ]
    result = web._translate_one_document("fixture-scan", "fixture.txt", "full",
                                         translation_backend=backend)

    saves = case.store.save_translation.call_args_list
    assert len(saves) == 3
    assert all(call.kwargs == {"index_evidence": False} for call in saves[:-1])
    assert saves[-1].kwargs == {}  # Publish the paired evidence/FTS index once.
    assert [call.args[2]["full_translation"] for call in saves] == [False, True, True]
    assert all(call.args[2]["translated_text"] == "已保存的可读译文" for call in saves)
    assert result["full_translation"] is True
    case.store.update_job.assert_not_called()


@pytest.mark.parametrize("large", [False, True], ids=["ordinary", "large-package"])
@pytest.mark.parametrize("backend", ["vllm", "offline_quickmt"])
def test_single_file_job_progress_advances_with_translation_units(translation_runtime, large, backend):
    case = translation_runtime
    case.large = large
    case.timelines["fixture.txt"] = [
        (10.0, checkpoint_state(0)),
        (11.1, checkpoint_state(1)),
        (12.2, checkpoint_state(2)),
        (12.3, checkpoint_state(4, status="completed")),
    ]
    result = web._run_claimed_translation_job({
        "id": "fixture-job", "scan_id": "fixture-scan", "owner_id": "alice",
        "task_type": "translate_document",
        "options": {"path": "fixture.txt", "translation_backend": backend},
    })

    updates = case.store.update_job.call_args_list
    assert [call.kwargs["progress"] for call in updates] == [5, 5, 27, 49, 94]
    assert all(call.kwargs["current_file"] == "fixture.txt" for call in updates)
    assert all(call.kwargs["heartbeat"] is True for call in updates)
    assert "4/4" in updates[-1].kwargs["message"]
    assert result["translation_status"] == "completed"
    assert result["full_translation"] is True


@pytest.mark.parametrize("status,completed,required", [("completed", 4, 4), ("not_required", 0, 0)])
def test_terminal_checkpoint_publishes_heartbeat_inside_one_second(
        translation_runtime, status, completed, required):
    case = translation_runtime
    case.timelines["fixture.txt"] = [
        (10.0, checkpoint_state(0, required=required)),
        (10.1, checkpoint_state(1, required=required)),
        (10.2, checkpoint_state(completed, required=required, status=status)),
    ]
    web._translate_one_document("fixture-scan", "fixture.txt", "full", job_id="fixture-job",
                                translation_backend="vllm", job_progress_range=(5, 94))

    updates = case.store.update_job.call_args_list
    assert len(updates) == 2  # The middle partial checkpoint remains throttled.
    assert "{}/{}".format(completed, required) in updates[-1].kwargs["message"]
    assert updates[-1].kwargs["heartbeat"] is True
    assert updates[-1].kwargs["progress"] == 94


@pytest.mark.parametrize("large", [False, True], ids=["ordinary", "large-package"])
def test_package_progress_maps_units_into_each_file_partition(translation_runtime, large):
    case = translation_runtime
    case.large = large
    for index, path in enumerate(["first.txt", "second.txt"]):
        start = 10.0 + 4 * index
        case.timelines[path] = [
            (start, checkpoint_state(0)),
            (start + 1.1, checkpoint_state(2)),
            (start + 1.2, checkpoint_state(4, status="completed")),
        ]
    result = web._run_claimed_translation_job({
        "id": "fixture-job", "scan_id": "fixture-scan", "owner_id": "alice",
        "task_type": "translate_package",
        "options": {"target_paths": ["first.txt", "second.txt"], "selected_only": True,
                    "phase": "deep_backfill", "translation_backend": "vllm"},
    })

    # Keep actual checkpoint heartbeats; the package also updates at file boundaries.
    updates = [call.kwargs for call in case.store.update_job.call_args_list
               if "翻译单元" in call.kwargs.get("message", "")]
    assert [(call["current_file"], call["progress"]) for call in updates] == [
        ("first.txt", 4), ("first.txt", 26), ("first.txt", 49),
        ("second.txt", 49), ("second.txt", 71), ("second.txt", 94),
    ]
    all_progress = [call.kwargs["progress"] for call in case.store.update_job.call_args_list]
    assert all_progress == sorted(all_progress)
    assert result["processed_files"] == result["completed_files"] == 2
    assert result["failed_files"] == 0


def test_checkpoint_without_progress_range_preserves_existing_job_percentage(translation_runtime):
    case = translation_runtime
    case.timelines["fixture.txt"] = [
        (10.0, checkpoint_state(2)),
        (10.1, checkpoint_state(4, status="completed")),
    ]
    web._translate_one_document("fixture-scan", "fixture.txt", "full", job_id="fixture-job",
                                translation_backend="vllm")

    updates = case.store.update_job.call_args_list
    assert len(updates) == 2
    assert all("progress" not in call.kwargs for call in updates)
    assert all(call.kwargs["heartbeat"] is True for call in updates)
    assert "4/4" in updates[-1].kwargs["message"]
