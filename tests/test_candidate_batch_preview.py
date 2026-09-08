from services.document_analysis import analyze_document_previews_batch


class FakeLLM:
    def chat_json(self, *args, **kwargs):
        return {"json": {"documents": [
            {"path": "a.txt", "core_summary": "A", "topics": ["alpha"], "conclusions": ["alpha 风险需要持续监测"]},
            {"path": "b.txt", "core_summary": "B", "topics": ["beta"], "conclusions": ["beta 文件记录了已完成事项"]},
        ]}, "model": "fake", "usage": {"total_tokens": 10}}


def test_candidate_batch_returns_independent_preview_summaries():
    docs = [
        {"path": "a.txt", "document": {"text": "alpha evidence", "source": {"name": "a.txt"}, "evidence": [{"evidence_id": "a-1", "source_path": "a.txt", "text": "alpha 风险需要持续监测。"}]}},
        {"path": "b.txt", "document": {"text": "beta evidence", "source": {"name": "b.txt"}, "evidence": [{"evidence_id": "b-1", "source_path": "b.txt", "text": "beta 文件记录了已完成事项。"}]}},
    ]
    result, meta = analyze_document_previews_batch(FakeLLM(), docs, max_chars=4000)
    assert set(result) == {"a.txt", "b.txt"}
    assert result["a.txt"]["summary"]["analysis_level"] == "preview"
    assert result["b.txt"]["summary"]["verification_status"] == "candidate"
    claim = result["a.txt"]["summary"]["file_conclusions"][0]
    assert result["a.txt"]["summary"]["claim_contract"] == "file-claims/preview-1.0"
    assert claim["support_status"] == "candidate"
    assert claim["statement"] == "alpha 风险需要持续监测"
    assert claim["evidence_ids"] == ["a-1"]
    assert claim["supports"][0]["evidence_id"] == "a-1"


def test_candidate_batch_uses_short_budget(monkeypatch):
    seen = {}
    class BudgetLLM(FakeLLM):
        def chat_json(self, *args, **kwargs):
            seen["max_tokens"] = kwargs.get("max_tokens")
            seen["prompt_chars"] = len(args[1])
            return super().chat_json(*args, **kwargs)
    docs = [{"path": "a.txt", "document": {"text": "x" * 20000, "source": {"name": "a.txt"}}}]
    analyze_document_previews_batch(BudgetLLM(), docs, max_chars=3200, output_tokens_per_file=180)
    assert seen["max_tokens"] == 180
    assert seen["prompt_chars"] < 5000


def test_idle_deep_jobs_are_lowest_priority():
    from services.storage import Storage
    assert Storage._job_priority("generate_summary", {"workflow_source": "idle_deep_model_summary"}) == 10
    assert Storage._job_priority("analyze_package", {"workflow_source": "idle_deep_backfill"}) == 10
    assert Storage._job_priority("conversation_turn", {}) > 10
