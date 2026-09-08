import tempfile
from pathlib import Path

from services.import_state_machine import can_transition, summary_type
from services.import_checkpoint import CheckpointedModel
from services.storage import Storage


def test_selected_import_has_no_stage_skips():
    assert can_transition("waiting_for_selection", "parsing_selected")
    assert can_transition("parsing_selected", "parsed_overview")
    assert can_transition("parsed_overview", "preliminary_summarizing")
    assert not can_transition("waiting_for_selection", "deep_summarizing_files")


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
