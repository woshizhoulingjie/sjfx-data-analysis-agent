"""Rule-first classification so simple mail questions never require a model."""

from __future__ import annotations


_EXHAUSTIVE = ("所有", "全部", "逐封", "不能遗漏", "完整清单", "全量")
_RESEARCH = ("原因", "过程", "对比", "确认", "风险", "梳理", "分析", "最早", "最终", "撤回", "变更")


def build_task_spec(question, package_id, source_revision, index_epoch, *, scope=None, base_result_set_id=None, max_tool_calls=None, max_model_calls=None, max_seconds=None, research_version=None, max_tokens=None, answer_mode=None):
    text = str(question or "").strip()
    if not text:
        raise ValueError("请输入邮件研究目标")
    from .multilingual import intent_mode
    mode = intent_mode(text)
    # User-selected answer depth. 'fast' keeps a single verification pass and
    # always answers from what was verified; 'deep' additionally allows the
    # bounded gap search and second-pass re-verification. Absent keeps the
    # previous full behavior for existing callers.
    mode_value = 'deep' if answer_mode in (None, '') else str(answer_mode).strip().lower()
    if mode_value not in ('fast', 'deep'):
        raise ValueError('回答方式仅支持 fast（快速）或 deep（深度）')
    questions = ["匹配邮件与附件"]
    if mode == "research":
        questions.extend(["支持证据", "后续修改、撤回或反证", "资料覆盖缺口"])
    if mode == "exhaustive":
        questions = ["固定清单逐项筛查", "记录 matched/not_matched/unknown/failed"]
    from config import Config
    from ..facts import infer_output_schema, requested_types
    spec = {
        "analysis_version": 2,
        "mode": mode,
        "answer_mode": mode_value,
        "objective": text,
        "package_id": package_id,
        "source_revision": source_revision,
        "index_epoch": index_epoch,
        "base_result_set_id": base_result_set_id,
        "scope": scope if scope is not None else {"include_explicit_attachments": True, "include_inline_resources": False},
        "questions": questions,
        "output_schema": infer_output_schema(text) if Config.MAIL_ENABLE_AGENT_MINING else ["事项", "时间", "参与方", "状态", "条件", "证据"],
        "fact_types": requested_types(text),
        "completion_checks": ["每个非空字段至少一条邮件或附件证据", "检查后续修改与相反证据", "披露附件与 OCR 覆盖缺口"],
        "budget": research_budget({k:v for k,v in {"max_tool_calls":max_tool_calls, "max_model_calls":max_model_calls, "max_seconds":max_seconds, "max_tokens":max_tokens}.items() if v is not None}),
    }
    if research_version is not None:
        from .planner import VERSION
        if research_version != VERSION:
            raise ValueError('不支持的邮件研究版本')
        spec['research_version'] = VERSION
    return spec


def research_budget_limits():
    from config import Config
    return {'max_tokens':Config.MAIL_RESEARCH_TOKEN_LIMIT,
            'max_model_calls':Config.MAIL_RESEARCH_MODEL_CALL_LIMIT,
            'max_tool_calls':Config.MAIL_RESEARCH_TOOL_CALL_LIMIT,
            'max_seconds':Config.MAIL_RESEARCH_TIMEOUT_LIMIT_SECONDS,
            'max_gap_rounds':Config.MAIL_RESEARCH_GAP_ROUNDS_LIMIT,
            'max_gap_candidates':Config.MAIL_RESEARCH_GAP_CANDIDATES_LIMIT}


def research_budget(overrides=None, previous=None):
    """Shared create/resume validation; resume may only increase allowances."""
    from config import Config
    if not isinstance(overrides if overrides is not None else {},dict):
        raise ValueError('研究预算必须是对象')
    budget = dict(previous) if previous is not None else {
        'max_tokens':Config.MAIL_RESEARCH_MAX_TOKENS,
        'max_model_calls':Config.MAIL_RESEARCH_MAX_MODEL_CALLS,
        'max_tool_calls':Config.MAIL_RESEARCH_MAX_TOOL_CALLS,
        'max_seconds':Config.MAIL_RESEARCH_TIMEOUT_SECONDS,
        'max_context_tokens':Config.MAIL_RESEARCH_CONTEXT_TOKENS,
        'max_gap_rounds':Config.MAIL_RESEARCH_GAP_ROUNDS,
        'max_gap_candidates':Config.MAIL_RESEARCH_GAP_CANDIDATES}
    floors={'max_tokens':1,'max_model_calls':0,'max_tool_calls':1,'max_seconds':60,'max_gap_rounds':0,'max_gap_candidates':1}
    limits=research_budget_limits()
    for key,value in (overrides or {}).items():
        if key not in limits:
            raise ValueError('研究预算字段不受支持')
        minimum=max(floors[key],(previous or {}).get(key,0))
        if isinstance(value,bool) or not isinstance(value,int) or not minimum<=value<=limits[key]:
            raise ValueError('研究预算应为服务上限内的整数，恢复时不得降低原预算')
        budget[key]=value
    return budget
