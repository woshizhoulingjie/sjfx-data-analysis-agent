"""Whole-package answers must cover every file, and only for ordinary packages.

Two properties are pinned here:

* a package-level question ("what does this package cover") has to reach the
  model with one summary per file, so the answer cannot silently describe only
  the highest ranked files; and
* nothing else changes -- a focused question keeps the shared budget, and an
  isolated large package returns its existing policy object untouched.
"""

import unittest
from types import SimpleNamespace

from services.conversation import (
    ContextWindowPolicy,
    ConversationEngine,
    ConversationScope,
    IntentDecision,
)

ORDINARY_SCAN = "83b1df962fbd"
LARGE_SCAN = "lp-1e0887e926e64df7835ae39e0abb7835"


def engine_with(policy=None):
    """A ConversationEngine whose only configured state is the context policy."""
    engine = object.__new__(ConversationEngine)
    engine.context_policy = policy or ContextWindowPolicy()
    return engine


def session(scan_id):
    return SimpleNamespace(scan_id=scan_id)


def decision(name):
    return IntentDecision(name, 0.9, "test")


class TurnContextPolicyTests(unittest.TestCase):
    def test_a_whole_package_question_reaches_every_file(self):
        policy = engine_with()._turn_context_policy(
            session(ORDINARY_SCAN), decision("summary"),
            ConversationScope(kind="package"),
        )
        # One item per file has to fit, so the per-file spread drops to one and
        # the count budget covers a normal package.
        self.assertEqual(policy.max_model_evidence_per_file, 1)
        self.assertGreaterEqual(policy.max_prompt_evidence, 50)
        self.assertGreaterEqual(policy.max_model_evidence_chars, 32000)

    def test_a_large_package_keeps_its_policy_object_untouched(self):
        base = ContextWindowPolicy()
        engine = engine_with(base)
        policy = engine._turn_context_policy(
            session(LARGE_SCAN), decision("summary"),
            ConversationScope(kind="package"),
        )
        # Identity, not equality: the large-package path must be unaffected.
        self.assertIs(policy, base)

    def test_focused_questions_keep_the_shared_policy(self):
        base = ContextWindowPolicy()
        engine = engine_with(base)
        for scan_id, intent, scope in (
            (ORDINARY_SCAN, "retrieval", ConversationScope(kind="package")),
            (ORDINARY_SCAN, "file_search", ConversationScope(kind="package")),
            (ORDINARY_SCAN, "summary", ConversationScope(kind="topic", value="电池")),
            (ORDINARY_SCAN, "analysis", ConversationScope(kind="entity", value="NREL")),
            (LARGE_SCAN, "retrieval", ConversationScope(kind="package")),
        ):
            policy = engine._turn_context_policy(
                session(scan_id), decision(intent), scope)
            self.assertIs(policy, base, (scan_id, intent, scope.kind))

    def test_a_large_package_never_gets_the_widened_budget(self):
        """The large-package path must stay on its existing budgets."""
        base = ContextWindowPolicy()
        engine = engine_with(base)
        for intent in ("summary", "analysis", "multi_task", "retrieval"):
            policy = engine._turn_context_policy(
                session(LARGE_SCAN), decision(intent),
                ConversationScope(kind="package"))
            self.assertIs(policy, base, intent)
            self.assertEqual(policy.max_prompt_evidence, 16, intent)
            self.assertEqual(policy.max_model_evidence_per_file, 2, intent)


class BroadPackageEvidenceTests(unittest.TestCase):
    def _items(self, context, lexical=None):
        import app
        return app._broad_package_evidence(
            SimpleNamespace(scan_id="scan-1"), context, lexical or [])

    def test_every_file_contributes_exactly_one_item(self):
        context = {"file_summaries": [
            {"path": "b.pdf", "summary": "摘要B"},
            {"path": "a.pdf", "summary": "摘要A"},
            {"path": "node:group-1", "summary": "节点摘要"},
            {"path": "empty.pdf", "summary": "   "},
        ]}
        items = self._items(context, [
            {"evidence_id": "TEXT-1", "source_path": "c.pdf", "text": "hit"},
        ])
        paths = [item["source_path"] for item in items]
        for expected in ("a.pdf", "b.pdf", "c.pdf"):
            self.assertIn(expected, paths)
        self.assertNotIn("empty.pdf", paths)
        self.assertEqual(len(paths), len(set(paths)), "one item per file")
        # Real files precede synthetic nodes so a bounded list still covers the
        # package rather than its virtual summaries.
        self.assertLess(paths.index("a.pdf"), paths.index("node:group-1"))

    def test_the_list_stays_within_the_displayable_bound(self):
        import app
        context = {"file_summaries": [
            {"path": "f{:03d}.pdf".format(index), "summary": "摘要"}
            for index in range(200)
        ]}
        items = self._items(context)
        self.assertEqual(len(items), app.BROAD_EVIDENCE_LIMIT)

    def test_synthetic_paths_are_not_treated_as_files(self):
        import app
        for path, expected in ((".", False), ("node:group-1", False),
                               ("naming:group-1", False), ("osti_1.pdf", True)):
            self.assertEqual(
                app._is_physical_package_path(path), expected, path)


class BoundedSearchBoundsTests(unittest.TestCase):
    """The historical per-query cap and merge limit stay the default."""

    class _Retriever:
        def __init__(self):
            self.requests = []

        def retrieve(self, request):
            self.requests.append(request)
            return {
                "results": [
                    {"evidence_id": "{}-{}".format(request.query, index),
                     "source_path": "f{:02d}.pdf".format(index), "text": "x"}
                    for index in range(request.top_k)
                ],
                "coverage": {},
            }

    class _Engine:
        def __init__(self, retriever):
            self.retriever = retriever

    def _run(self, **kwargs):
        from services.analysis_tools import execute_bounded_searches
        retriever = self._Retriever()
        result = execute_bounded_searches(
            self._Engine(retriever), "scan-1",
            ConversationScope(kind="package"), ["a", "b", "c"],
            intent="summary", **kwargs)
        return retriever, result

    def test_every_other_caller_keeps_the_historical_bounds(self):
        retriever, result = self._run(top_k=10)
        for request in retriever.requests:
            self.assertEqual(request.top_k, 10, "per-query top_k unchanged")
        # Three queries of ten hits merge down to max(top_k, 8) * 2.
        self.assertEqual(len(result.get("results") or []), 20)

    def test_the_per_query_cap_is_still_twenty_by_default(self):
        retriever, _ = self._run(top_k=30)
        for request in retriever.requests:
            self.assertEqual(request.top_k, 20, "the 20-hit cap is the default")

    def test_a_whole_package_question_can_merge_every_file(self):
        retriever, result = self._run(
            top_k=50, max_per_query=50, merge_limit=50)
        for request in retriever.requests:
            self.assertEqual(request.top_k, 50)
        self.assertEqual(len(result.get("results") or []), 50)


class CitedDraftGuardTests(unittest.TestCase):
    """A cited draft survives review; an uncited unrelated one is withheld."""

    @staticmethod
    def _turn(answer):
        return {
            "answer": answer,
            "citations": [
                {"citation_index": 1, "source_path": "a.pdf",
                 "text": "生态环境部发布规划，涉及能源结构调整。"},
                {"citation_index": 2, "source_path": "b.pdf",
                 "text": "就业与教育领域的部署。"},
            ],
        }

    @staticmethod
    def _verification(claim):
        return {"ledger": {"claims": [claim]}, "status": "failed", "warnings": []}

    def test_a_cited_draft_is_kept_with_an_unverified_warning(self):
        from services.claim_verifier import ClaimVerifier
        verifier = ClaimVerifier()
        claim = {
            "text": "该数据包涵盖生态环境与能源两个领域[1][2]。",
            "status": "unsupported", "citation_indexes": [1, 2],
        }
        guarded = verifier.guard_result(
            self._turn(claim["text"]), self._verification(claim))
        self.assertNotIn("现有证据不足", str(guarded["answer"]))
        self.assertEqual(guarded["status"], "answered")
        self.assertTrue(
            any("请对照引用自行复核" in str(item)
                for item in (guarded.get("warnings") or [])),
            guarded.get("warnings"),
        )

    def test_an_uncited_unrelated_draft_is_still_withheld(self):
        from services.claim_verifier import ClaimVerifier
        verifier = ClaimVerifier()
        claim = {
            "text": "这批资料详细讨论了火星探测器着陆点的地质成分。",
            "status": "unsupported",
        }
        guarded = verifier.guard_result(
            self._turn(claim["text"]), self._verification(claim))
        self.assertIn("现有证据不足", str(guarded["answer"]))
        self.assertEqual(guarded["status"], "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()

