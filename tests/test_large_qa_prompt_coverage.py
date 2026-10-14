import json
from types import SimpleNamespace
from unittest.mock import patch

from config import Config
from services.conversation import ConversationEngine
from services.conversation_package_context import model_digest


def synthesis_context():
    notes = [f"方向{i}：" + "共同研究发现与适用条件。" * 6 for i in range(60)]
    return {
        "package_type": "large",
        "package_id": "lp-prompt-coverage",
        "package_overview": "可省略背景。" * 1000,
        "reading_manifest": {
            "files_read": 100, "summary_backed_files": 100,
            "files_without_summary": 0, "complete": True,
            "directory_atlas": [{"directory": f"dir-{i}", "sample_paths": ["x" * 200]} for i in range(80)],
        },
        "large_qa": {
            "global_mode": True, "source_files": 100, "scanned_files": 100,
            "scanned_summary_files": 100, "scanned_original_files": 70,
            "scanned_chunks": 1000, "model_mapped_files": 100,
            "map_batches": 10, "map_processed_batches": 10, "map_failures": 0,
            "map_complete": True, "reduce_complete": True,
            "synthesis_complete": True, "source_snapshot_current": True,
            "map_notes": notes,
        },
    }


def test_digest_prioritizes_every_final_note_over_package_background():
    context = synthesis_context()
    metadata = {}
    result = model_digest(context, max_chars=6500, metadata=metadata)
    topic = json.loads(result)["topic_overview"]
    assert len(result) <= 6500
    assert topic["notes"] == context["large_qa"]["map_notes"]
    assert topic["notes_complete"]
    assert metadata["final_synthesis_notes"] == metadata["total_notes"] == 60
    assert metadata["final_synthesis_complete"]


def test_digest_cannot_claim_complete_after_budget_omits_notes():
    context = synthesis_context()
    metadata = {}
    result = model_digest(context, max_chars=1800, metadata=metadata)
    topic = json.loads(result)["topic_overview"]
    assert len(result) <= 1800
    assert topic["compression_incomplete"]
    assert topic["retained_notes"] < topic["total_notes"] == 60
    assert not topic["synthesis_complete"]
    assert not metadata["final_synthesis_complete"]
    assert metadata["final_synthesis_notes"] == len(topic["notes"])


def test_final_prompt_keeps_all_notes_before_optional_evidence():
    class Model:
        def chat(self, _system, prompt, **_kwargs):
            self.prompt = prompt
            return {"content": "资料研究发现及适用条件。[1]"}

    context = synthesis_context()
    model = Model()
    engine = ConversationEngine(SimpleNamespace(retrieve=lambda _request: {}), answer_model=model)
    rows = [
        {"citation_index": i, "citation_label": f"[{i}]", "source_path": f"{i}.txt",
         "original_text": f"研究材料{i}。" + "资料说明研究发现与适用条件。" * 80,
         "location": {}, "evidence_role": "direct_source"}
        for i in range(1, 21)
    ]
    metadata = {}
    # This fake model exercises complete generation with an explicit 32K window.
    with patch.object(Config, "LLM_CONTEXT_TOKENS", 32768), \
            patch.object(Config, "CONVERSATION_CONTEXT_TOKENS", 32768), \
            patch.object(Config, "CONVERSATION_TOKENIZER_PATH", "nonexistent-test-tokenizer"):
        engine._quality_grounded_answer("summary", "概括全部资料的研究方向", "历史背景。" * 1000,
                                        rows, context, len(rows), metadata)
    digest = model.prompt.split("不等于已读全文）：\n", 1)[1].split("\n\n当前问题：", 1)[0]
    assert json.loads(digest)["topic_overview"]["notes"] == context["large_qa"]["map_notes"]
    assert metadata["final_synthesis_complete"]
    assert metadata["final_synthesis_notes"] == metadata["total_notes"] == 60
    assert 3 <= metadata["selected_evidence"] < 20
