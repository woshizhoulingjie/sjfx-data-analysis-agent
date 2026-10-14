"""Source total lookup uses evidence; actual aggregation keeps exact guards."""
import pytest

from config import Config
from services.analysis_planner import AnalysisPlanner
from services.conversation import CallableEvidenceRetriever, CallableStructuredQA, ConversationEngine, IntentRouter


@pytest.mark.parametrize("question", [
    "根据采购凭证，采购合计多少元？请引用原文证据。",
    "这份合同总额是多少？",
    "发票合计金额是什么？",
])
def test_recorded_total_uses_retrieval_in_both_router_and_plan(question):
    assert IntentRouter().route(question).name == "retrieval"
    plan = AnalysisPlanner().plan(question, {"kind": "package"})
    assert plan["modes"] == ["retrieval"]
    assert "structured_calculation" not in {step["tool"] for step in plan["steps"]}


@pytest.mark.parametrize("question", [
    "销售额合计是多少？",
    "统计所有采购凭证，采购合计多少元？",
    "根据这些采购凭证，采购合计多少元？",
    "计算合同总额是多少？",
    "所有文件的合同总額汇总是多少？",
])
def test_multi_record_calculation_keeps_structured_guards(question):
    assert IntentRouter().route(question).name == "structured"
    assert "structured" in AnalysisPlanner().plan(question, {"kind": "package"})["modes"]


def test_recorded_total_answers_from_literal_source_without_structured_images(monkeypatch):
    # This test covers generated answers; the fake model has a 32K window.
    monkeypatch.setattr(Config, "LLM_CONTEXT_TOKENS", 32768)
    monkeypatch.setattr(Config, "CONVERSATION_CONTEXT_TOKENS", 32768)
    calls = []
    class Model:
        def chat(self, system_prompt, user_prompt, **kwargs):
            calls.append(user_prompt)
            return {"content": "采购凭证记载采购合计120元，共3项。[1]"}
    def no_structured(_):
        pytest.fail("single recorded total must not require CSV/XLSX/JSON statistical profiles")
    engine = ConversationEngine(
        answer_model=Model(),
        retriever=CallableEvidenceRetriever(lambda request: {"results": [{
            "evidence_id": "receipt-1", "source_path": "采购凭证.txt",
            "text": "采购合计120元，共3项。", "language": "zh", "retrieval_score": 1.0,
        }], "coverage": {"total_files": 1, "scope_files": 1, "searchable_files": 1,
                         "deep_analyzed_files": 1, "candidate_files": 1, "inspected_files": 1}}),
        structured_qa=CallableStructuredQA(no_structured),
    )
    session = engine.new_session("receipt-scan")
    result = engine.ask(session, "根据采购凭证，采购合计多少元？请引用原文证据。")
    assert result["intent"]["name"] == "retrieval"
    assert "120元" in result["answer"]
    assert result["citations"][0]["evidence_id"] == "receipt-1"
    assert result["evidence_status"] != "insufficient"
    assert calls
