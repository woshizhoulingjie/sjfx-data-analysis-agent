"""Rule-first classification so simple mail questions never require a model."""

from __future__ import annotations


_EXHAUSTIVE = ("所有", "全部", "逐封", "不能遗漏", "完整清单", "全量")
_RESEARCH = ("原因", "过程", "对比", "确认", "风险", "梳理", "分析", "最早", "最终", "撤回", "变更")


def build_task_spec(question, package_id, source_revision, index_epoch, *, scope=None, base_result_set_id=None, max_tool_calls=18, max_model_calls=5, max_seconds=600):
    text = str(question or "").strip()
    if not text:
        raise ValueError("请输入邮件研究目标")
    from .multilingual import intent_mode
    mode = intent_mode(text)
    questions = ["匹配邮件与附件"]
    if mode == "research":
        questions.extend(["支持证据", "后续修改、撤回或反证", "资料覆盖缺口"])
    if mode == "exhaustive":
        questions = ["固定清单逐项筛查", "记录 matched/not_matched/unknown/failed"]
    from config import Config
    from ..facts import infer_output_schema, requested_types
    return {
        "mode": mode,
        "objective": text,
        "package_id": package_id,
        "source_revision": source_revision,
        "index_epoch": index_epoch,
        "base_result_set_id": base_result_set_id,
        "scope": scope or {"include_explicit_attachments": True, "include_inline_resources": False, "message_keys": []},
        "questions": questions,
        "output_schema": infer_output_schema(text) if Config.MAIL_ENABLE_AGENT_MINING else ["事项", "时间", "参与方", "状态", "条件", "证据"],
        "fact_types": requested_types(text),
        "completion_checks": ["每个非空字段至少一条邮件或附件证据", "检查后续修改与相反证据", "披露附件与 OCR 覆盖缺口"],
        "budget": {"max_tool_calls": max(1, int(max_tool_calls)), "max_model_calls": max(0, int(max_model_calls)), "max_seconds": max(30, int(max_seconds))},
    }
