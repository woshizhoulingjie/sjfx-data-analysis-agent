"""Durable orchestration runtime for one interactive package-analysis turn."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from config import Config
from services.analysis_planner import AnalysisPlanner
from services.analysis_tools import (
    TOOL_LABELS,
    build_batch_analysis,
    compact_tool_context,
    execute_analysis_toolbox,
    execute_bounded_searches,
    merge_retrieval_results,
)
from services.claim_verifier import ClaimVerifier
from services.conversation import ConversationScope, ConversationSession
from services.research_memory import update_research_memory


class AnalysisTurnRuntime:
    def __init__(
        self,
        storage: Any,
        conversation_engine: Any,
        planner: Optional[AnalysisPlanner] = None,
        verifier: Optional[ClaimVerifier] = None,
        batch_size: int = 30,
        max_candidate_evidence: int = 5000,
        max_revision_attempts: int = 1,
        cancel_check: Optional[Callable[[], Any]] = None,
    ):
        self.storage = storage
        self.engine = conversation_engine
        self.planner = planner or AnalysisPlanner()
        self.verifier = verifier or ClaimVerifier()
        self.batch_size = max(20, min(50, int(batch_size or 30)))
        self.max_candidate_evidence = max(
            100, min(5000, int(max_candidate_evidence or 5000))
        )
        self.max_revision_attempts = max(0, min(2, int(max_revision_attempts or 0)))
        self.cancel_check = cancel_check

    def _checkpoint(self) -> None:
        if self.cancel_check:
            self.cancel_check()

    def _publish(
        self,
        turn: Mapping[str, Any],
        status: str,
        stage: str,
        progress: int,
        message: str,
        plan: Optional[Mapping[str, Any]] = None,
        event_type: str = "status",
    ) -> None:
        self._checkpoint()
        self.storage.update_conversation_turn(
            turn["id"],
            status=status,
            stage=stage,
            progress=progress,
            message=message,
            plan=plan,
            event_type=event_type,
        )
        self.storage.set_conversation_turn_message(
            turn["id"], message, status, stage, progress
        )
        if turn.get("job_id"):
            self.storage.update_job(
                turn["job_id"],
                progress=progress,
                stage=stage,
                message=message,
                current_stage=message,
                heartbeat=True,
            )

    def _step(
        self,
        turn_id: str,
        plan: Mapping[str, Any],
        tool: str,
        status: str,
        progress: int,
        payload: Optional[Mapping[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        for step in plan.get("steps") or []:
            if step.get("tool") == tool:
                self.storage.update_conversation_turn_step(
                    turn_id,
                    step["step_id"],
                    status,
                    progress=progress,
                    payload=payload,
                    error=error,
                )
                return

    @staticmethod
    def _fresh_session(session: ConversationSession) -> ConversationSession:
        return ConversationSession.from_dict(session.as_dict())

    @staticmethod
    def _explicit_question_paths(
        question: str, inventory_paths: Sequence[str], limit: int = 24
    ) -> Sequence[str]:
        """Resolve file names explicitly written in a question.

        Exact file references are stronger than broad lexical retrieval.  The
        result stays bounded; an ambiguous name matching too many files falls
        back to the caller's existing scope instead of silently truncating.
        """
        text = str(question or "").replace("\\", "/").casefold()
        if not text:
            return []
        matches = []
        for raw_path in inventory_paths or []:
            path = str(raw_path or "").replace("\\", "/").strip()
            if not path:
                continue
            lowered = path.casefold()
            logical_name = PurePosixPath(lowered.replace("::", "/")).name
            physical_name = PurePosixPath(lowered.split("::", 1)[0]).name
            names = {
                value for value in (logical_name, physical_name)
                if len(value) >= 4
            }
            if lowered not in text and not any(name in text for name in names):
                continue
            matches.append(path)
            if len(matches) > limit:
                return []
        return list(dict.fromkeys(matches))

    @staticmethod
    def _batch_metrics(batch_summary: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            key: batch_summary.get(key)
            for key in (
                "schema_version",
                "batch_size",
                "batch_count",
                "candidate_files",
                "inspected_files",
                "inventory_files",
                "unparsed_files",
                "scope_complete",
                "scope_limitation",
                "evidence_records",
            )
        }

    @staticmethod
    def _tool_summaries(tool_results: Mapping[str, Any]) -> Dict[str, Any]:
        output = {}
        for name, value in dict(tool_results or {}).items():
            value = dict(value or {})
            summary = {
                key: value.get(key)
                for key in (
                    "status",
                    "method",
                    "item_count",
                    "batch_size",
                    "batch_count",
                    "candidate_files",
                    "inspected_files",
                    "compared_files",
                    "translated_evidence_count",
                    "original_evidence_count",
                )
                if value.get(key) is not None
            }
            summary["items"] = list(value.get("items") or [])[:20]
            output[name] = summary
        return output

    def _execute_tools(
        self,
        turn_id: str,
        plan: Mapping[str, Any],
        batch_summary: Mapping[str, Any],
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        excluded = {
            "conversation_response",
            "document_discovery",
            "evidence_search",
            "claim_verifier",
            "answer_composer",
        }
        for step in plan.get("steps") or []:
            tool = str(step.get("tool") or "")
            if tool in excluded:
                continue
            self._checkpoint()
            self._step(turn_id, plan, tool, "running", 35)
            partial_plan = dict(plan)
            partial_plan["steps"] = [step]
            result = execute_analysis_toolbox(
                partial_plan, batch_summary, cancel_check=self.cancel_check
            ).get(tool) or {"status": "completed", "item_count": 0, "items": []}
            results[tool] = result
            step_payload = {
                key: result.get(key)
                for key in (
                    "status",
                    "method",
                    "item_count",
                    "batch_count",
                    "candidate_files",
                    "inspected_files",
                    "compared_files",
                )
                if result.get(key) is not None
            }
            self._step(turn_id, plan, tool, "completed", 100, step_payload)
        return results

    def _repair_queries(
        self, plan: Mapping[str, Any], verification: Mapping[str, Any]
    ) -> Sequence[str]:
        queries = []
        for claim in (verification.get("ledger") or {}).get("claims") or []:
            if claim.get("status") in {"unsupported", "partially_supported"}:
                text = str(claim.get("text") or "").strip()
                if text:
                    queries.append("{} 原文依据 例外 反证".format(text[:800]))
        queries.extend(plan.get("query_variants") or [])
        return list(dict.fromkeys(queries))[:3]

    def _verify(
        self,
        turn_id: str,
        plan: Mapping[str, Any],
        turn_result: Mapping[str, Any],
        tool_results: Mapping[str, Any],
        batch_summary: Mapping[str, Any],
    ) -> Dict[str, Any]:
        self._step(turn_id, plan, "claim_verifier", "running", 70)
        verification = self.verifier.verify(
            turn_result,
            plan,
            tool_results=tool_results,
            batch_summary=batch_summary,
        )
        self._step(
            turn_id,
            plan,
            "claim_verifier",
            "completed",
            100,
            {
                "verification_status": verification.get("status"),
                "quality_metrics": verification.get("quality_metrics") or {},
            },
        )
        return verification

    def execute(
        self,
        turn: Mapping[str, Any],
        session: ConversationSession,
        scope: ConversationScope,
        inventory_paths: Sequence[str],
    ) -> Dict[str, Any]:
        turn = dict(turn or {})
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            raise ValueError("分析轮次缺少 turn_id")
        try:
            self._publish(turn, "running", "understanding", 5, "正在理解分析目标和约束")
            memory_record = self.storage.get_conversation_research_memory(turn["session_id"])
            plan = self.planner.plan(
                turn["question"],
                scope.as_dict(),
                memory=memory_record.get("payload") or {},
            )
            self.storage.replace_conversation_turn_steps(turn_id, plan.get("steps") or [])
            self._publish(
                turn,
                "running",
                "planning",
                15,
                "分析计划已生成，共 {} 个受控步骤".format(len(plan.get("steps") or [])),
                plan=plan,
                event_type="plan",
            )

            ordered_modes = list(plan.get("modes") or [])
            modes = set(ordered_modes)
            primary_mode = ordered_modes[0] if ordered_modes else "analysis"
            scoped_inventory_paths = [
                str(path) for path in inventory_paths or []
                if scope.contains_source(path)
            ]
            if scope.kind == "file_type":
                expected_extension = str(scope.value or "").lower().strip()
                if expected_extension and not expected_extension.startswith("."):
                    expected_extension = "." + expected_extension
                scoped_inventory_paths = [
                    path for path in scoped_inventory_paths
                    if PurePosixPath(path.split("::", 1)[0]).suffix.lower()
                    == expected_extension
                ]
            execution_scope = scope
            explicit_paths = self._explicit_question_paths(
                turn["question"], scoped_inventory_paths
            )
            if explicit_paths:
                execution_scope = ConversationScope(
                    kind="files",
                    source_paths=tuple(explicit_paths),
                    label="问题中明确指定的文件",
                )
                scoped_inventory_paths = list(explicit_paths)
                plan["resolved_file_mentions"] = list(explicit_paths)
            retrieval_override = None
            batch_summary: Dict[str, Any] = {}
            tool_results: Dict[str, Any] = {}
            if "casual" not in modes:
                if "structured" not in modes:
                    self._step(turn_id, plan, "document_discovery", "running", 20)
                    self._publish(
                        turn,
                        "running",
                        "retrieving",
                        24,
                        "正在整个指定范围内寻找相关文件",
                    )
                    retrieval_override = execute_bounded_searches(
                        self.engine,
                        turn["scan_id"],
                        execution_scope,
                        plan.get("query_variants") or [turn["question"]],
                        intent=primary_mode,
                        top_k=12,
                    )
                    candidate_files = int(
                        (retrieval_override.get("coverage") or {}).get("candidate_files")
                        or 0
                    )
                    self._step(
                        turn_id,
                        plan,
                        "document_discovery",
                        "completed",
                        100,
                        {"candidate_files": candidate_files},
                    )
                    self._step(
                        turn_id,
                        plan,
                        "evidence_search",
                        "completed",
                        100,
                        {
                            "evidence_count": len(retrieval_override.get("results") or []),
                            "deferred_candidates": list(
                                (retrieval_override.get("coverage") or {}).get(
                                    "deferred_candidates"
                                )
                                or []
                            ),
                        },
                    )

                # Ordinary retrieval, translation and plain summaries should
                # answer from the bounded evidence window.  Building a full
                # package batch for those requests made simple chat turns
                # inspect every parsed file and unnecessarily delayed replies.
                deep_modes = {
                    "structured", "comparison", "timeline", "relationship",
                    "contradiction", "risk",
                }
                if modes.intersection(deep_modes):
                    self._publish(
                        turn,
                        "running",
                        "batching",
                        36,
                        "正在按每批 {} 份文件建立结构化中间结果".format(self.batch_size),
                        event_type="batching",
                    )
                    batch_summary = build_batch_analysis(
                        self.storage,
                        turn["scan_id"],
                        execution_scope,
                        plan,
                        scoped_inventory_paths,
                        batch_size=self.batch_size,
                        max_evidence=self.max_candidate_evidence,
                        cancel_check=self.cancel_check,
                    )
                    self._publish(
                        turn,
                        "running",
                        "tool_execution",
                        48,
                        "已检查 {} 份候选文件，正在执行专业分析工具".format(
                            int(batch_summary.get("inspected_files") or 0)
                        ),
                        event_type="tools",
                    )
                    tool_results = self._execute_tools(turn_id, plan, batch_summary)
                    plan["batch_summary"] = self._batch_metrics(batch_summary)
                    plan["tool_results"] = self._tool_summaries(tool_results)
                    plan["tool_context"] = compact_tool_context(tool_results, batch_summary)

            durable_plan = dict(plan)
            durable_plan.pop("tool_context", None)
            self._publish(
                turn,
                "running",
                "executing",
                62,
                "正在依据专业工具结果组织数据包分析",
                plan=durable_plan,
            )
            self._checkpoint()
            working_session = self._fresh_session(session)
            turn_result = self.engine.ask(
                working_session,
                turn["question"],
                scope=execution_scope,
                persist_scope=False,
                retrieval_override=retrieval_override,
                analysis_plan=plan,
            )

            def apply_scope_guard(result: Dict[str, Any]) -> Dict[str, Any]:
                if not batch_summary or batch_summary.get("scope_complete"):
                    return result
                inspected = int(batch_summary.get("inspected_files") or 0)
                scope_total = int(batch_summary.get("inventory_files") or 0)
                limitation = (
                    "本轮仅检查检索命中的 {}/{} 个范围文件；以下内容是候选集结论，"
                    "不是全范围完整结论。".format(inspected, scope_total)
                )
                result["warnings"] = list(dict.fromkeys(
                    list(result.get("warnings") or []) + [limitation]
                ))
                answer = str(result.get("answer") or "")
                if limitation not in answer:
                    result["answer"] = "范围说明\n- {}\n\n{}".format(
                        limitation, answer
                    )
                return result

            turn_result = apply_scope_guard(turn_result)

            promotion = dict(turn_result.get("promotion_request") or {})
            inventory = set(scoped_inventory_paths)
            requested = []
            for path in promotion.get("candidate_paths") or []:
                path = str(path)
                if path not in inventory or path in requested:
                    continue
                if (
                    self.storage.get_file_state(turn["scan_id"], path) or {}
                ).get("status") == "completed":
                    continue
                requested.append(path)
                if len(requested) >= max(
                    1, int(promotion.get("desired_file_count") or 12)
                ):
                    break
            depth = max(0, int(turn.get("continuation_depth") or 0))
            max_promotion_depth = int(
                getattr(self.engine, "max_promotion_depth", 0)
                or getattr(Config, "CONVERSATION_MAX_PROMOTION_DEPTH", 3)
            )
            if promotion.get("required") and requested and depth < max_promotion_depth:
                self._checkpoint()
                promotion_job_id, _created = self.storage.create_or_get_typed_job(
                    turn["scan_id"],
                    "analyze_package",
                    options={
                        "target_paths": requested,
                        "workflow_source": "question_promotion",
                        "scope_label": "交互式分析补充深析：{}".format(
                            turn["question"][:120]
                        ),
                        "parse_mode": "accurate",
                        "conversation_turn_id": turn_id,
                        "conversation_scope": execution_scope.as_dict(),
                        "conversation_continuation_depth": depth + 1,
                    },
                    owner_id=turn["owner_id"],
                )
                turn_result["quality_metrics"] = self._batch_metrics(batch_summary)
                self.storage.update_conversation_turn(
                    turn_id,
                    status="waiting_for_deep_analysis",
                    stage="waiting_for_deep_analysis",
                    progress=65,
                    promotion_job_id=promotion_job_id,
                    continuation_depth=depth + 1,
                    result=turn_result,
                    message="正文证据不足，正在定向深析 {} 份相关文件".format(
                        len(requested)
                    ),
                    event_type="promotion",
                )
                self.storage.set_conversation_turn_message(
                    turn_id,
                    "已经找到相关候选文件，正在定向深析 {} 份资料；完成后会继续本轮分析。".format(
                        len(requested)
                    ),
                    "waiting_for_deep_analysis",
                    "waiting_for_deep_analysis",
                    65,
                )
                return {
                    "turn_id": turn_id,
                    "status": "waiting_for_deep_analysis",
                    "promotion_job_id": promotion_job_id,
                    "candidate_paths": requested,
                }
            if promotion.get("required") and requested:
                remaining = len(promotion.get("candidate_paths") or [])
                warning = (
                    "自动深析已达到单轮上限；仍有 {} 个候选文件可继续深析。"
                    "当前回答保持阶段性结论。".format(remaining)
                )
                turn_result["promotion_limit_reached"] = True
                turn_result["remaining_deferred_candidates"] = remaining
                turn_result["warnings"] = list(dict.fromkeys(
                    list(turn_result.get("warnings") or []) + [warning]
                ))

            self._publish(
                turn, "running", "verifying", 80, "正在逐条核验结论、数字、引用和反证"
            )
            verification = self._verify(
                turn_id, plan, turn_result, tool_results, batch_summary
            )
            revision_attempts = 0
            while (
                verification.get("needs_revision")
                and revision_attempts < self.max_revision_attempts
                and "casual" not in modes
            ):
                revision_attempts += 1
                self._publish(
                    turn,
                    "running",
                    "repairing",
                    88,
                    "核验未完全通过，正在扩展检索并自动修正（第 {} 次）".format(
                        revision_attempts
                    ),
                    event_type="repair",
                )
                repair_retrieval = execute_bounded_searches(
                    self.engine,
                    turn["scan_id"],
                    execution_scope,
                    self._repair_queries(plan, verification),
                    intent=primary_mode,
                    top_k=20,
                )
                retrieval_override = merge_retrieval_results(
                    [retrieval_override or {}, repair_retrieval], limit=40
                )
                repair_plan = dict(plan)
                repair_plan["constraints"] = list(
                    dict.fromkeys(
                        list(plan.get("constraints") or [])
                        + [
                            "删除没有直接原文支持的事实陈述",
                            "所有数字必须与对应引用原文完全一致",
                            "明确保留反证、例外和不一致",
                        ]
                    )
                )
                repair_plan["verification_feedback"] = {
                    "warnings": list(verification.get("warnings") or [])[:12],
                    "unsupported_claims": [
                        item.get("text")
                        for item in (verification.get("ledger") or {}).get("claims")
                        or []
                        if item.get("status")
                        in {"unsupported", "partially_supported"}
                    ][:12],
                }
                self._checkpoint()
                working_session = self._fresh_session(session)
                turn_result = self.engine.ask(
                    working_session,
                    turn["question"],
                    scope=execution_scope,
                    persist_scope=False,
                    retrieval_override=retrieval_override,
                    analysis_plan=repair_plan,
                )
                turn_result = apply_scope_guard(turn_result)
                verification = self._verify(
                    turn_id, repair_plan, turn_result, tool_results, batch_summary
                )
                plan = repair_plan

            if verification.get("needs_revision") and "casual" not in modes:
                turn_result = self.verifier.guard_result(turn_result, verification)
                verification = self.verifier.verify(
                    turn_result,
                    plan,
                    tool_results=tool_results,
                    batch_summary=batch_summary,
                )
                turn_result = apply_scope_guard(turn_result)

            verification["revision_attempts"] = revision_attempts
            quality_metrics = dict(verification.get("quality_metrics") or {})
            quality_metrics["revision_attempts"] = revision_attempts
            quality_metrics["professional_tool_count"] = len(tool_results)
            turn_result["quality_metrics"] = quality_metrics
            turn_result["citation_verification_status"] = verification.get("status")
            turn_result["scope_completeness"] = (
                "candidate_only"
                if quality_metrics.get("scope_incomplete")
                else "checked_scope"
            )
            turn_result["analysis_tools"] = self._tool_summaries(tool_results)
            verification_warnings = list(verification.get("warnings") or [])
            stale_fragments = (
                "事实性陈述没有通过", "陈述只得到部分支持", "数字结论无法",
                "引用标号无对应证据", "候选证据覆盖率较低",
                "当前回答没有可持久化的正文引用", "未通过核验：",
            )
            prior_warnings = [
                item for item in (turn_result.get("warnings") or [])
                if not any(fragment in str(item) for fragment in stale_fragments)
            ]
            if turn_result.get("citations"):
                prior_warnings = [
                    item for item in prior_warnings
                    if "没有可检索正文证据" not in str(item)
                ]
            turn_result["warnings"] = list(
                dict.fromkeys(prior_warnings + verification_warnings)
            )
            self._step(
                turn_id,
                plan,
                "answer_composer",
                "completed",
                100,
                {"output_format": (plan.get("output") or {}).get("format")},
            )
            self._checkpoint()
            self.storage.complete_conversation_turn(
                turn_id,
                turn_result,
                verification,
                (verification.get("ledger") or {}).get("claims") or [],
                session_state=working_session.as_dict(),
            )
            memory = update_research_memory(
                memory_record.get("payload") or {},
                plan,
                turn_result,
                verification,
                turn_id,
                tool_results=tool_results,
            )
            self.storage.save_conversation_research_memory(turn["session_id"], memory)
            return {
                "turn_id": turn_id,
                "status": "completed",
                "verification_status": verification.get("status"),
                "evidence_count": len(turn_result.get("citations") or []),
                "quality_metrics": quality_metrics,
            }
        except Exception as exc:
            cancelled = exc.__class__.__name__ in {"JobCancelled", "ParseIsolationCancelled"}
            status = "cancelled" if cancelled else "failed"
            message = (
                "本轮分析已取消。"
                if cancelled
                else "交互式分析失败：{}".format(str(exc)[:500])
            )
            self.storage.update_conversation_turn(
                turn_id,
                status=status,
                stage=status,
                progress=100,
                error=None if cancelled else str(exc)[:2000],
                message=message,
                event_type=status,
            )
            self.storage.set_conversation_turn_message(
                turn_id,
                message,
                status,
                status,
                100,
                error=None if cancelled else str(exc)[:500],
            )
            raise
