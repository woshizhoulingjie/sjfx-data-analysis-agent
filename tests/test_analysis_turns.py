import tempfile
import unittest
from pathlib import Path

from services.analysis_planner import AnalysisPlanner
from services.analysis_tools import execute_analysis_toolbox, reduce_evidence_batches
from services.claim_verifier import ClaimVerifier
from services.conversation import ConversationScope, ConversationSession
from services.research_memory import update_research_memory
from services.storage import Storage
from services.turn_runtime import AnalysisTurnRuntime


class FakeRetriever:
    def retrieve(self, _request):
        return {
            "results": [{
                "evidence_id": "EV-1",
                "source_path": "contracts/a.txt",
                "text": "乙方逾期时每日承担合同金额千分之一的违约金。",
                "retrieval_score": 0.95,
            }],
            "coverage": {"query_coverage": 1.0, "deferred_candidates": []},
        }


class FakeEngine:
    def __init__(self, promotion=False):
        self.retriever = FakeRetriever()
        self.promotion = promotion

    def ask(self, _session, question, **_kwargs):
        promotion = None
        if self.promotion:
            promotion = {
                "required": True,
                "candidate_paths": ["contracts/b.txt"],
                "desired_file_count": 4,
            }
        return {
            "status": "partial" if promotion else "answered",
            "evidence_status": "partial" if promotion else "supported",
            "question": question,
            "resolved_query": question,
            "intent": {"name": "analysis"},
            "answer": "乙方承担明确的逾期责任 [1]。",
            "citations": [{
                "citation_index": 1,
                "citation_label": "[1]",
                "evidence_id": "EV-1",
                "source_path": "contracts/a.txt",
                "location": {"section": "违约责任"},
                "original_text": "乙方逾期时每日承担合同金额千分之一的违约金。",
            }],
            "coverage": {"query_coverage": 0.5 if promotion else 1.0},
            "promotion_request": promotion,
            "warnings": [],
        }


class AnalysisTurnTests(unittest.TestCase):
    def make_storage(self):
        holder = tempfile.TemporaryDirectory(prefix="sjfx-turn-")
        root = Path(holder.name)
        storage = Storage(root / "agent.db", root / "sidecars")
        self.addCleanup(holder.cleanup)
        return storage

    def seed_session(self, storage):
        session = ConversationSession(
            scan_id="scan-1", scope=ConversationScope("package"),
            session_id="session-1", title="合同研究",
        )
        storage.save_conversation(session.as_dict(), "owner-1")
        return session

    def test_planner_builds_a_bounded_risk_comparison_plan(self):
        plan = AnalysisPlanner().plan(
            "比较所有合同并分析乙方风险，列出反证和例外，输出表格",
            {"kind": "package"},
        )
        tools = [item["tool"] for item in plan["steps"]]
        self.assertIn("document_discovery", tools)
        self.assertIn("cross_file_compare", tools)
        self.assertIn("risk_analyzer", tools)
        self.assertIn("counter_evidence_search", tools)
        self.assertEqual(plan["output"]["format"], "table")

    def test_planner_understands_natural_timeline_wording(self):
        plan = AnalysisPlanner().plan(
            "请跨文件比较数据包中的重要时间和主要事件", {"kind": "package"}
        )
        tools = {item["tool"] for item in plan["steps"]}
        self.assertIn("cross_file_compare", tools)
        self.assertIn("timeline_builder", tools)

    def test_verifier_never_treats_an_unreferenced_claim_as_supported(self):
        verification = ClaimVerifier().verify({
            "answer": "合同对乙方非常不利。",
            "citations": [{"citation_index": 1, "evidence_id": "EV-1"}],
            "coverage": {"query_coverage": 1.0},
        }, {"modes": ["risk"]})
        self.assertEqual(verification["status"], "partial")
        self.assertEqual(verification["ledger"]["unsupported_claim_count"], 1)

    def test_turn_creation_is_idempotent_and_messages_are_authoritative(self):
        storage = self.make_storage()
        self.seed_session(storage)
        first, created = storage.create_conversation_turn(
            "session-1", "scan-1", "owner-1", "分析合同风险",
            {"kind": "package"}, idempotency_key="request-1",
        )
        second, created_again = storage.create_conversation_turn(
            "session-1", "scan-1", "owner-1", "分析合同风险",
            {"kind": "package"}, idempotency_key="request-1",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])
        session = storage.get_conversation("session-1", "owner-1", scan_id="scan-1")
        self.assertEqual([item["role"] for item in session["messages"]], ["user", "assistant"])
        self.assertEqual(storage.claim_next_job("worker-1")["task_type"], "conversation_turn")

    def test_runtime_completes_same_placeholder_and_persists_full_evidence(self):
        storage = self.make_storage()
        session = self.seed_session(storage)
        turn, _created = storage.create_conversation_turn(
            "session-1", "scan-1", "owner-1", "分析乙方违约风险",
            {"kind": "package"}, idempotency_key="request-2",
        )
        job = storage.claim_next_job("worker-1")
        turn["job_id"] = job["id"]
        runtime = AnalysisTurnRuntime(storage, FakeEngine())
        result = runtime.execute(
            turn, session, ConversationScope("package"), ["contracts/a.txt"]
        )
        self.assertEqual(result["status"], "completed")
        stored_turn = storage.get_conversation_turn(turn["id"], "owner-1")
        self.assertEqual(stored_turn["result"]["citations"][0]["evidence_id"], "EV-1")
        restored = storage.get_conversation("session-1", "owner-1", scan_id="scan-1")
        self.assertEqual(len(restored["messages"]), 2)
        self.assertIn("乙方承担明确", restored["messages"][1]["content"])
        self.assertEqual(restored["messages"][1]["evidence_ids"], ["EV-1"])
        self.assertEqual(
            storage.get_conversation_research_memory("session-1")["payload"]["last_turn_id"],
            turn["id"],
        )

    def test_promotion_waits_without_duplicating_the_user_message(self):
        storage = self.make_storage()
        session = self.seed_session(storage)
        turn, _created = storage.create_conversation_turn(
            "session-1", "scan-1", "owner-1", "综合比较合同风险",
            {"kind": "package"}, idempotency_key="request-3",
        )
        job = storage.claim_next_job("worker-1")
        turn["job_id"] = job["id"]
        result = AnalysisTurnRuntime(storage, FakeEngine(promotion=True)).execute(
            turn, session, ConversationScope("package"),
            ["contracts/a.txt", "contracts/b.txt"],
        )
        self.assertEqual(result["status"], "waiting_for_deep_analysis")
        restored = storage.get_conversation("session-1", "owner-1", scan_id="scan-1")
        self.assertEqual(len(restored["messages"]), 2)
        self.assertEqual(restored["messages"][0]["content"], "综合比较合同风险")
        stored_turn = storage.get_conversation_turn(turn["id"], "owner-1")
        self.assertEqual(stored_turn["status"], "waiting_for_deep_analysis")
        self.assertTrue(stored_turn["promotion_job_id"])

    def test_verifier_checks_entailment_numbers_scope_and_quality_metrics(self):
        verification = ClaimVerifier().verify({
            "answer": "合同金额为200元 [1]。",
            "citations": [{
                "citation_index": 1,
                "evidence_id": "EV-2",
                "source_path": "outside/b.txt",
                "original_text": "合同金额为100元。",
            }],
            "coverage": {"query_coverage": 0.25, "total_files": 100, "searchable_files": 80},
        }, {
            "modes": ["risk"],
            "scope": {"kind": "files", "value": ["inside/a.txt"], "source_paths": ["inside/a.txt"]},
            "steps": [{"tool": "counter_evidence_search"}],
        }, tool_results={"counter_evidence_search": {"status": "completed", "items": []}})
        self.assertEqual(verification["status"], "partial")
        self.assertEqual(verification["quality_metrics"]["numeric_failure_count"], 1)
        self.assertEqual(verification["quality_metrics"]["out_of_scope_citation_count"], 1)
        self.assertEqual(verification["quality_metrics"]["unparsed_files"], 20)
        self.assertTrue(verification["needs_revision"])

    def test_verifier_accepts_a_close_paraphrase_but_not_a_generic_overlap(self):
        verified = ClaimVerifier().verify({
            "answer": "TEE通过隔离硬件和软件保护应用中的敏感信息 [1]。",
            "citations": [{
                "citation_index": 1, "evidence_id": "EV-TEE", "source_path": "tee.txt",
                "original_text": "可信执行环境通过隔离硬件与软件来保证应用程序执行环境安全，并保护存储的个人信息和秘密数据。",
            }],
        }, {"modes": ["summary"], "scope": {"kind": "package"}, "steps": []})
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["quality_metrics"]["supported_claim_count"], 1)

        generic = ClaimVerifier().verify({
            "answer": "该系统已经完全消除所有安全风险 [1]。",
            "citations": [{
                "citation_index": 1, "evidence_id": "EV-GENERIC", "source_path": "tee.txt",
                "original_text": "系统安全设计仍然存在风险，需要继续进行评估。",
            }],
        }, {"modes": ["risk"], "scope": {"kind": "package"}, "steps": []})
        self.assertNotEqual(generic["status"], "verified")

    def test_batch_reduction_and_professional_tools_are_real_and_bounded(self):
        evidence = []
        for index in range(65):
            evidence.append({
                "evidence_id": "EV-{}".format(index),
                "source_path": "docs/{:03d}.txt".format(index),
                "text": "2025年1月{}日，甲方支付乙方{}元，但是存在延期交付风险。".format(
                    index % 28 + 1, index + 100
                ),
            })
        paths = [item["source_path"] for item in evidence]
        reduced = reduce_evidence_batches(
            evidence, "比较付款、建立时间线并分析风险", paths,
            file_states={path: {"status": "completed"} for path in paths},
            batch_size=30,
        )
        self.assertEqual(reduced["candidate_files"], 65)
        self.assertEqual(reduced["batch_count"], 3)
        plan = {"objective": "比较付款、建立时间线并分析风险", "steps": [
            {"tool": "structured_calculation"},
            {"tool": "cross_file_compare"},
            {"tool": "timeline_builder"},
            {"tool": "relationship_analyzer"},
            {"tool": "risk_analyzer"},
            {"tool": "counter_evidence_search"},
            {"tool": "summary_reducer"},
        ]}
        reduced["candidate_evidence"] = evidence
        tools = execute_analysis_toolbox(plan, reduced)
        self.assertEqual(set(tools), {item["tool"] for item in plan["steps"]})
        self.assertEqual(tools["cross_file_compare"]["compared_files"], 65)
        self.assertTrue(tools["timeline_builder"]["items"])
        self.assertTrue(tools["relationship_analyzer"]["items"])
        self.assertTrue(tools["risk_analyzer"]["items"])
        self.assertTrue(tools["counter_evidence_search"]["items"])

    def test_research_memory_accumulates_instead_of_overwriting(self):
        first = update_research_memory({}, {
            "scope": {"kind": "package"}, "objective": "分析甲方", "modes": ["risk"],
            "constraints": ["只依据原文"],
        }, {"citations": [{"evidence_id": "EV-1", "source_path": "a.txt"}]}, {
            "ledger": {"claims": [{
                "claim_id": "CL-1", "text": "甲方存在风险 [1]。",
                "status": "supported", "evidence_ids": ["EV-1"],
            }]},
        }, "turn-1")
        second = update_research_memory(first, {
            "scope": {"kind": "package"}, "objective": "那乙方呢", "modes": ["comparison"],
            "constraints": [],
        }, {"citations": [{"evidence_id": "EV-2", "source_path": "b.txt"}]}, {
            "ledger": {"claims": [{
                "claim_id": "CL-2", "text": "乙方存在责任 [1]。",
                "status": "supported", "evidence_ids": ["EV-2"],
            }]},
        }, "turn-2")
        self.assertEqual(len(second["confirmed_claims"]), 2)
        self.assertEqual(second["evidence_ids"], ["EV-1", "EV-2"])
        self.assertEqual(second["turn_ids"], ["turn-1", "turn-2"])
        self.assertIn("只依据原文", second["user_constraints"])

    def test_worker_recovery_reconciles_turn_to_queued(self):
        storage = self.make_storage()
        self.seed_session(storage)
        turn, _created = storage.create_conversation_turn(
            "session-1", "scan-1", "owner-1", "恢复测试",
            {"kind": "package"}, idempotency_key="recovery-1",
        )
        job = storage.claim_next_job("worker-before-restart")
        storage.update_conversation_turn(
            turn["id"], status="running", stage="executing", progress=60,
        )
        self.assertEqual(job["status"], "running")
        self.assertEqual(storage.recover_orphaned_jobs_after_lock(), 1)
        self.assertEqual(storage.reconcile_conversation_turn_jobs(), 1)
        recovered = storage.get_conversation_turn(turn["id"], owner_id="owner-1")
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["stage"], "queued")

    def test_recovery_does_not_repeat_an_already_completed_turn(self):
        storage = self.make_storage()
        session = self.seed_session(storage)
        turn, _created = storage.create_conversation_turn(
            "session-1", "scan-1", "owner-1", "完成后父进程重启",
            {"kind": "package"}, idempotency_key="completed-recovery-1",
        )
        job = storage.claim_next_job("worker-before-restart")
        turn["job_id"] = job["id"]
        AnalysisTurnRuntime(storage, FakeEngine()).execute(
            turn, session, ConversationScope("package"), ["contracts/a.txt"]
        )
        self.assertEqual(storage.recover_orphaned_jobs_after_lock(), 1)
        self.assertEqual(storage.reconcile_conversation_turn_jobs(), 1)
        recovered_job = storage.get_job(job["id"], owner_id="owner-1")
        recovered_turn = storage.get_conversation_turn(turn["id"], owner_id="owner-1")
        self.assertEqual(recovered_job["status"], "completed")
        self.assertEqual(recovered_turn["status"], "completed")

    def test_runtime_honours_cancellation_checkpoint(self):
        class JobCancelled(RuntimeError):
            pass

        storage = self.make_storage()
        session = self.seed_session(storage)
        turn, _created = storage.create_conversation_turn(
            "session-1", "scan-1", "owner-1", "分析并立即取消",
            {"kind": "package"}, idempotency_key="cancel-checkpoint-1",
        )
        job = storage.claim_next_job("worker-1")
        turn["job_id"] = job["id"]
        calls = {"value": 0}

        def cancel_check():
            calls["value"] += 1
            if calls["value"] >= 3:
                raise JobCancelled("cancelled")

        with self.assertRaises(JobCancelled):
            AnalysisTurnRuntime(
                storage, FakeEngine(), cancel_check=cancel_check
            ).execute(turn, session, ConversationScope("package"), ["contracts/a.txt"])
        cancelled = storage.get_conversation_turn(turn["id"], owner_id="owner-1")
        self.assertEqual(cancelled["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
