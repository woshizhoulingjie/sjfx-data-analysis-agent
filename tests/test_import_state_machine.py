import tempfile
from pathlib import Path

import pytest

from services.import_state_machine import can_transition, summary_type
from services.import_checkpoint import CheckpointedModel
from services.storage import Storage


def test_selected_import_has_no_stage_skips():
    assert can_transition("waiting_for_selection", "parsing_selected")
    assert can_transition("parsing_selected", "parsed_overview")
    assert can_transition("parsed_overview", "preliminary_summarizing")
    assert not can_transition("waiting_for_selection", "deep_summarizing_files")


@pytest.mark.parametrize('stage', ['scanning', 'waiting_for_selection', 'parsed_overview'])
def test_paused_import_restores_its_persisted_stage_after_reopening(tmp_path, stage):
    path = tmp_path / '暂停 恢复.sqlite3'
    storage = Storage(path)
    storage.create_import_task('paused-scan', str(tmp_path))
    # Arrange an in-progress stage, then exercise the actual durable pause.
    storage.update_import_task('paused-scan', status=stage, checkpoint={'selection_version': 2})
    storage.pause_package_analysis_jobs('paused-scan')
    restored = Storage(path)
    task = restored.get_import_task('paused-scan')
    assert task['status'] == 'paused'
    checkpoint = dict(task['checkpoint'])
    original = checkpoint.pop('paused_from_state')
    assert original == stage
    result = restored.transition_import_task('paused-scan', original, checkpoint=checkpoint)
    assert result['status'] == stage
    assert result['checkpoint']['selection_version'] == 2
    assert not restored.package_processing_paused('paused-scan')


def test_preliminary_and_deep_summaries_are_separate_records():
    root = Path(tempfile.mkdtemp())
    storage = Storage(root / "state.sqlite3")
    preliminary = {"schema_version": 4, "analysis_stage": "preliminary", "summary": "first"}
    deep = {"schema_version": 4, "analysis_stage": "deep", "deep_analysis": True, "summary": "deep"}
    storage.save_summary("scan-1", "a.txt", summary_type("preliminary", "file"), preliminary)
    storage.save_summary("scan-1", "a.txt", summary_type("deep", "file"), deep)
    assert storage.get_summary("scan-1", "a.txt", summary_type("preliminary", "file"))["summary"] == "first"


def test_deep_summary_counts_keep_file_and_node_stages_separate():
    root = Path(tempfile.mkdtemp())
    storage = Storage(root / "state.sqlite3")
    storage.save_summary(
        "scan-1", "a.txt", summary_type("deep", "file"),
        {"schema_version": 4, "deep_analysis": True},
    )
    storage.save_summary(
        "scan-1", "node:topic-a", summary_type("deep", "node"),
        {"schema_version": 4, "deep_analysis": True},
    )
    assert storage.deep_summary_counts("scan-1") == {
        "file": 1, "folder": 1, "total": 2,
    }


def test_checkpointed_model_reuses_completed_call_after_resume():
    class FakeModel:
        model = "fake"

        def __init__(self):
            self.calls = 0

        def chat_json(self, *_args, **_kwargs):
            self.calls += 1
            return {"json": {"core_summary": "saved"}}

    root = Path(tempfile.mkdtemp())
    storage = Storage(root / "state.sqlite3")
    model = FakeModel()
    checkpointed = CheckpointedModel(model, storage, "job-1")
    assert checkpointed.chat_json("system", "prompt") == {"json": {"core_summary": "saved"}}
    assert checkpointed.chat_json("system", "prompt") == {"json": {"core_summary": "saved"}}
    assert model.calls == 1
