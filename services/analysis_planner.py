"""Build bounded, auditable plans for data-package conversation turns."""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Mapping, Optional


ALLOWED_TOOLS = {
    "conversation_response",
    "document_discovery",
    "evidence_search",
    "structured_calculation",
    "cross_file_compare",
    "timeline_builder",
    "relationship_analyzer",
    "contradiction_detector",
    "risk_analyzer",
    "summary_reducer",
    "translation_tool",
    "counter_evidence_search",
    "claim_verifier",
    "answer_composer",
}


def _text(value: Any, limit: int = 8000) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit]


class AnalysisPlanner:
    """Deterministic control plane around model-backed analysis.

    The language model may later enrich a plan, but it can never introduce an
    unregistered tool.  A deterministic baseline keeps every user instruction
    executable when the local model is busy or unavailable.
    """

    CASUAL_RE = re.compile(
        r"^(?:你好|您好|嗨|hi|hello|谢谢|感谢|再见|你是谁|怎么用|帮助)[！!。.？?\s]*$",
        re.I,
    )
    TRANSLATION_RE = re.compile(r"翻译|译成|双语|中英对照|translate|translation", re.I)
    STRUCTURED_RE = re.compile(
        r"合计|总额|总和|平均|最大|最小|多少(?:条|行|个)?|数量|统计|占比|sum|total|average|count",
        re.I,
    )
    COMPARE_RE = re.compile(r"比较|对比|差异|相同|不同|各(?:份|个)|compare|difference", re.I)
    TIMELINE_RE = re.compile(
        r"时间线|先后|演变|过程|历程|按时间|重要时间|时间.{0,8}事件|事件.{0,8}时间|"
        r"timeline|chronolog",
        re.I,
    )
    RELATION_RE = re.compile(r"关系|关联|联系|往来|互动|网络|relationship|correspondence", re.I)
    CONTRADICTION_RE = re.compile(r"矛盾|冲突|不一致|相反|反证|例外|contradict|conflict", re.I)
    RISK_RE = re.compile(r"风险|不利|隐患|责任|漏洞|异常|risk|liabilit", re.I)
    SUMMARY_RE = re.compile(r"总结|概括|综述|概览|主要内容|梳理|summary|overview", re.I)
    REPORT_RE = re.compile(r"报告|研究报告|完整分析|深入分析|综合分析|report", re.I)

    def plan(self, question: str, scope: Mapping[str, Any],
             memory: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        question = _text(question)
        if not question:
            raise ValueError("问题不能为空")
        scope = dict(scope or {"kind": "package"})
        memory = dict(memory or {})
        follow_up = bool(
            len(question.rstrip("？?。.!")) <= 12
            or re.match(r"^(?:那|那么|它|他|她|这些|这个|其中|为什么|怎么|还有|继续|再|呢)", question)
        )
        previous_objective = _text(memory.get("current_objective"), 1600)
        objective = question
        if follow_up and previous_objective and previous_objective != question:
            objective = "{}；当前追问：{}".format(previous_objective, question)
        casual = bool(self.CASUAL_RE.search(question))
        modes: List[str] = []
        checks = (
            ("translation", self.TRANSLATION_RE),
            ("structured", self.STRUCTURED_RE),
            ("comparison", self.COMPARE_RE),
            ("timeline", self.TIMELINE_RE),
            ("relationship", self.RELATION_RE),
            ("contradiction", self.CONTRADICTION_RE),
            ("risk", self.RISK_RE),
            ("summary", self.SUMMARY_RE),
        )
        if casual:
            modes.append("casual")
        else:
            modes.extend(name for name, pattern in checks if pattern.search(question))
            if not modes:
                # Keep ordinary factual questions on the fast retrieval path.
                # Deep analysis is selected only when the user explicitly asks
                # for comparison, risk, timeline, contradiction, or research.
                modes.append("retrieval")

        output_format = "report" if self.REPORT_RE.search(question) else "answer"
        if re.search(r"表格|列表|清单|table", question, re.I):
            output_format = "table"
        elif "timeline" in modes or re.search(r"时间线", question):
            output_format = "timeline"

        steps: List[Dict[str, Any]] = []

        def add(tool: str, action: str, optional: bool = False) -> None:
            if tool not in ALLOWED_TOOLS or any(item["tool"] == tool for item in steps):
                return
            steps.append({
                "step_id": "step-{:02d}-{}".format(len(steps) + 1, uuid.uuid4().hex[:6]),
                "tool": tool,
                "action": action,
                "optional": bool(optional),
                "status": "pending",
                "progress": 0,
            })

        if casual:
            add("conversation_response", "结合研究记忆进行基础交流，不读取数据包事实")
        else:
            add("document_discovery", "在当前范围的轻量索引中发现相关候选文件")
            add("evidence_search", "检索能够直接支持用户目标的正文证据")
            if "structured" in modes:
                add("structured_calculation", "使用结构化画像执行可复核的确定性计算")
            if "comparison" in modes:
                add("cross_file_compare", "按统一维度比较候选文件并保留差异依据")
            if "timeline" in modes:
                add("timeline_builder", "抽取日期、事件和参与方并建立时间顺序")
            if "relationship" in modes:
                add("relationship_analyzer", "分析实体之间的关系方向、时间和证据")
            if "contradiction" in modes:
                add("contradiction_detector", "识别资料之间的冲突和不一致")
            if "risk" in modes:
                add("risk_analyzer", "识别风险、责任、触发条件和例外情况")
            if "summary" in modes or output_format == "report":
                add("summary_reducer", "对多文件中间结果进行分批归并和去重")
            if "translation" in modes:
                add("translation_tool", "保留原文并为命中证据生成中文工作译文")
            if set(modes).intersection({"comparison", "contradiction", "risk", "research"}):
                add("counter_evidence_search", "主动寻找反向证据、例外和遗漏", optional=True)
            add("claim_verifier", "核验结论、数字、引用、范围和反向证据")
            add("answer_composer", "按照用户要求组织最终分析结果")

        query_variants = [objective]
        if not casual and set(modes).intersection({"comparison", "contradiction", "risk", "research"}):
            query_variants.append("{} 例外 反向证据 不一致".format(objective))
        if "timeline" in modes:
            query_variants.append("{} 日期 时间 事件".format(objective))
        query_variants = list(dict.fromkeys(_text(item, 2000) for item in query_variants))[:3]

        constraints = list(memory.get("user_constraints") or [])
        for marker in ("只依据原文", "必须引用", "列出反证", "不要推测"):
            if marker in question and marker not in constraints:
                constraints.append(marker)

        return {
            "schema_version": "analysis-plan/1.0",
            "objective": objective,
            "original_instruction": question,
            "follow_up": follow_up,
            "scope": scope,
            "modes": modes,
            "tasks": [
                *([] if casual else ["document_discovery", "evidence_search"]),
                *[mode for mode in modes if mode not in {"casual", "retrieval"}],
                *([] if casual else ["claim_verifier", "answer_composer"]),
            ],
            "query_variants": query_variants,
            "steps": steps,
            "constraints": constraints[:20],
            "output": {
                "format": output_format,
                "include_citations": not casual,
                "include_counter_evidence": "counter_evidence_search" in {
                    item["tool"] for item in steps
                },
            },
        }
