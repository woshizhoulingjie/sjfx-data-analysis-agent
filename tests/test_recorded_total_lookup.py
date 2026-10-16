"""Source total lookup uses evidence; actual aggregation keeps exact guards."""
import pytest

from config import Config
from services.analysis_planner import AnalysisPlanner
from services.conversation import CallableEvidenceRetriever, CallableStructuredQA, ConversationEngine, IntentRouter


PROCUREMENT_QUESTION = (
    "请根据当前资料回答：采购合计多少元、共几项、是否已经实际付款？"
    "请引用支持这些结论的原文证据；不要把未付款推断为已付款。"
)


@pytest.mark.parametrize("question", [
    "根据采购凭证，采购合计多少元？请引用原文证据。",
    "这份合同总额是多少？",
    "发票合计金额是什么？",
    PROCUREMENT_QUESTION,
    "根据采购凭证，采购合计多少元？请引用所有证据。",
    "根据采购凭证，采购合计多少元？请引用全部原文证据。",
    "根据采购凭证，采购合计多少元？请给出多个相关结论及对应的原文证据。",
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
    "根据这些采购凭证，采购合计多少元？请引用支持这些结论的原文证据。",
    "所有采购凭证的合计多少元？请引用所有证据。",
    "全部采购凭证的合计多少元？请引用全部原文证据。",
    "根据所有证据文件，采购合计多少元？请引用所有证据。",
    "跨文件的采购合计多少元？请引用所有证据。",
    "计算合同总额是多少？请引用支持这些结论的原文证据。",
    "所有证据中的采购金额总和是多少？请引用原文。",
    "全部证据里的采购金额总和是多少？请引用原文。",
    "这些证据内的采购金额总和是多少？请引用原文。",
    "所有证据的采购金额总和是多少？请引用原文。",
    "请引用所有证据中的采购金额，总和是多少？",
    "请给出全部证据里的采购金额，总和是多少？",
    "请提供这些证据内的采购金额，总和是多少？",
    "请引用所有证据的采购金额，总和是多少？",
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


@pytest.mark.parametrize("generate_answer", [False, True])
def test_procurement_three_facts_keep_literal_evidence_and_unpaid_status(monkeypatch, generate_answer):
    monkeypatch.setattr(Config, "LLM_CONTEXT_TOKENS", 32768)
    monkeypatch.setattr(Config, "CONVERSATION_CONTEXT_TOKENS", 32768)
    source_text = "采购合计120元，共3项，尚未付款。"
    retrieval_requests = []
    model_prompts = []

    class Model:
        def chat(self, system_prompt, user_prompt, **kwargs):
            model_prompts.append(user_prompt)
            return {"content": "采购合计120元，共3项，尚未付款。[1]"}

    def retrieve(request):
        retrieval_requests.append(request)
        return {
            "results": [{
                "evidence_id": "procurement-1", "source_path": "procurement.txt",
                "text": source_text, "language": "zh", "retrieval_score": 1.0,
            }],
            "coverage": {"total_files": 2, "scope_files": 2, "searchable_files": 2,
                         "deep_analyzed_files": 2, "candidate_files": 1, "inspected_files": 1},
        }

    def no_structured(_):
        pytest.fail("literal TXT facts must reach retrieval without structured profiles")

    engine = ConversationEngine(
        retriever=CallableEvidenceRetriever(retrieve),
        answer_model=Model() if generate_answer else None,
        structured_qa=CallableStructuredQA(no_structured),
    )
    result = engine.ask(engine.new_session("two-txt-scan"), PROCUREMENT_QUESTION)

    assert len(retrieval_requests) == 1
    assert retrieval_requests[0].query == PROCUREMENT_QUESTION
    assert result["intent"]["name"] == "retrieval"
    assert result["status"] != "insufficient_evidence"
    assert result["evidence_status"] != "insufficient"
    assert "120元" in result["answer"]
    assert "3项" in result["answer"]
    assert "尚未付款" in result["answer"]
    assert "已付款" not in result["answer"]
    assert "[1]" in result["answer"]
    assert result["citations"][0]["evidence_id"] == "procurement-1"
    assert result["citations"][0]["source_path"] == "procurement.txt"
    assert result["citations"][0]["original_text"] == source_text
    assert bool(model_prompts) == generate_answer
    if generate_answer:
        assert len(model_prompts) == 1
        assert "procurement.txt" in model_prompts[0]
        assert source_text in model_prompts[0]
        assert "共几项" in model_prompts[0]
        assert "是否已经实际付款" in model_prompts[0]


@pytest.mark.parametrize("question", [
    "根据这些采购凭证，采购合计多少元？请引用所有原文证据。",
    "所有证据中的采购金额总和是多少？请引用原文。",
    "全部证据里的采购金额总和是多少？请引用原文。",
    "这些证据内的采购金额总和是多少？请引用原文。",
    "所有证据的采购金额总和是多少？请引用原文。",
])
def test_real_aggregation_without_profiles_does_not_fall_back_to_txt_facts(question):

    def no_retrieval(_):
        pytest.fail("a multi-record total must not fall back to a literal TXT value")

    def no_profiles(request):
        assert request.question == question
        raise ValueError("当前范围没有可用的 CSV/XLSX/JSON 结构化数据画像")

    engine = ConversationEngine(
        retriever=CallableEvidenceRetriever(no_retrieval),
        structured_qa=CallableStructuredQA(no_profiles),
    )
    result = engine.ask(engine.new_session("unprofiled-receipts"), question)
    assert result["intent"]["name"] == "structured"
    assert result["status"] == "insufficient_evidence"
    assert result["evidence_status"] == "insufficient"
    assert result["citations"] == []
