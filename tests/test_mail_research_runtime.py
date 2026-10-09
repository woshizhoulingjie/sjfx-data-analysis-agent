import time

from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.verifier import verify_business_relation


class ResearchLog:
    def __init__(self):
        self.events = []
        self.steps = []

    def event(self, *args):
        self.events.append(args)

    def record_research_step(self, *args, **kwargs):
        self.steps.append((args, kwargs))


def test_rerank_empty_or_partial_result_keeps_candidates(monkeypatch):
    repository = ResearchLog()
    runtime = MailResearchRuntime(repository)
    monkeypatch.setattr("config.Config.MAIL_RERANK_ENABLED", True)
    candidates = [
        {"segment_id": "s1", "message_key": "m1"},
        {"segment_id": "s2", "message_key": "m2"},
        {"segment_id": "s3", "message_key": "m3"},
    ]
    state = {"max_model_calls": 5, "model_calls": 0, "max_seconds": 30, "started": time.monotonic(), "deadline_reached": False, "tool_limit_reached": False}
    runtime._model_call = lambda *args, **kwargs: {"segment_ids": []}
    assert runtime._rerank("t", "o", state, {"objective": "状态"}, candidates) == candidates

    runtime._model_call = lambda *args, **kwargs: {"segment_ids": ["s2"]}
    ranked = runtime._rerank("t", "o", state, {"objective": "状态"}, candidates)
    assert [item["segment_id"] for item in ranked] == ["s2", "s1", "s3"]


def test_followup_reads_are_budgeted_and_audited():
    repository = ResearchLog()
    runtime = MailResearchRuntime(repository)
    state = {"max_tool_calls": 1, "tool_calls": 0, "max_seconds": 30, "started": time.monotonic(), "deadline_reached": False, "tool_limit_reached": False}
    calls = []

    class Tools:
        def read_message(self, key):
            calls.append(key)
            return {"parts": [{"is_explicit_attachment": True}], "items": [{"message_key": key, "segment_id": key, "evidence_id": "e-" + key}]}

    selected = runtime._filter_followup("t", "o", state, Tools(), {"m1", "m2"}, [])
    assert calls == ["m1"]
    assert state["tool_limit_reached"] is True
    assert selected[0]["message_key"] == "m1"
    assert any(step[0][1] == "read_message" for step in repository.steps)


def test_evidence_limit_reports_hidden_candidates():
    candidates = [
        {"message_key": "m1", "evidence_id": "e1", "quote_text": "one"},
        {"message_key": "m2", "evidence_id": "e2", "quote_text": "two"},
        {"message_key": "m3", "evidence_id": "e3", "quote_text": "three"},
    ]
    evidence, meta = MailResearchRuntime._evidence(candidates, limit=2)
    assert [item["evidence_id"] for item in evidence] == ["e1", "e2"]
    assert meta == {"evidence_candidates": 3, "evidence_visible": 2, "evidence_truncated": True}


def test_relation_requires_action_semantics_and_anchor():
    evidence = {"e1": {"quote_text": "Acme 项目 X 已提交申请。"}}
    result = verify_business_relation(
        {
            "subject_ref": "Acme",
            "object_ref": "项目 X",
            "claim_text": "Acme 已确认项目 X",
            "predicate": "confirmed",
            "evidence_ids": ["e1"],
        },
        evidence,
    )
    assert result["support_status"] != "supported"


def test_relation_with_counter_evidence_is_not_persisted_as_supported():
    evidence = {
        "e1": {"quote_text": "Acme 项目 X 已确认。"},
        "e2": {"quote_text": "Acme 项目 X 后来撤回。"},
    }
    result = verify_business_relation(
        {
            "subject_ref": "Acme",
            "object_ref": "项目 X",
            "claim_text": "Acme 已确认项目 X",
            "predicate": "confirmed",
            "evidence_ids": ["e1"],
            "counter_evidence_ids": ["e2"],
        },
        evidence,
    )
    assert result["support_status"] == "contradicted"
    assert result["state"] == "conflicting"
    assert result["counter_evidence_ids"] == ["e2"]
