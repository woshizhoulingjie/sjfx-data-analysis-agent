"""Evidence-grounded multi-turn structured QA for package analysis.

The workflow deliberately uses separate model calls for planning, answering,
and review so benchmark traces reflect the real reasoning hand-offs.
"""

from __future__ import annotations

import json

from services.llm_trace import context as trace_context, last_call_id


SYSTEM_PROMPT = """你是当前资料包的结构化问答智能体。当前处于结构化问答阶段。
你只能依据调用方提供的已核验证据回答，不得补造人物、金额、日期、指标或因果关系。
所有事实必须引用真实存在的 evidence_id；必须区分直接事实、推论和信息缺口。
这是一次多轮任务：本轮必须继承前序轮次的结构化结果，同时纠正其中可能存在的问题。"""


DEFAULT_QUESTIONS = (
    "当前资料包的核心目标和预期价值是什么？",
    "项目的背景、需求与范围是什么？",
    "项目包含哪些主要工作内容和技术路线？",
    "哪些组织或角色参与项目，各自承担什么职责？",
    "项目的时间计划、阶段和关键里程碑是什么？",
    "项目有哪些资源投入、成本、付款安排或交付物？",
    "现有资料反映了哪些验收结果、性能指标或完成状态？",
    "项目存在哪些风险、约束、依赖或证据冲突？",
    "基于现有证据可以形成哪些可靠结论，还缺少哪些资料？",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _evidence_context(evidence_catalog):
    rows = []
    for index, raw in enumerate(evidence_catalog or [], 1):
        if not isinstance(raw, dict):
            continue
        evidence_id = str(raw.get("evidence_id") or raw.get("id") or "EVID-{:03d}".format(index))
        rows.append({
            "evidence_id": evidence_id,
            "source_path": raw.get("source_path") or raw.get("path"),
            "source_title": raw.get("source_title") or raw.get("title"),
            "location": raw.get("location") or raw.get("section") or raw.get("page"),
            "text": str(raw.get("text") or raw.get("excerpt") or raw.get("summary") or "")[:900],
            "verification_status": raw.get("verification_status") or raw.get("status") or "candidate",
        })
    return rows


def _normalise_questions(payload):
    questions = []
    for item in (payload or {}).get("questions") or []:
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            continue
        questions.append({
            "question_id": "Q-{:03d}".format(len(questions) + 1),
            "question": str(item["question"]).strip(),
            "purpose": str(item.get("purpose") or "核验当前资料包的关键信息").strip(),
            "required_evidence_types": list(item.get("required_evidence_types") or []),
            "candidate_evidence_ids": list(item.get("candidate_evidence_ids") or []),
            "priority": str(item.get("priority") or "medium"),
        })
        if len(questions) == 9:
            break
    used = {item["question"] for item in questions}
    for question in DEFAULT_QUESTIONS:
        if len(questions) == 9:
            break
        if question in used:
            continue
        questions.append({
            "question_id": "Q-{:03d}".format(len(questions) + 1),
            "question": question,
            "purpose": "补全当前资料包的核心分析维度",
            "required_evidence_types": [],
            "candidate_evidence_ids": [],
            "priority": "high",
        })
    return questions


def _partition(values, count):
    count = max(1, min(int(count), len(values) or 1))
    return [values[index::count] for index in range(count) if values[index::count]]


def run_multiturn_structured_qa(llm, evidence_catalog, local_report=None,
                                request_timeout=600, answer_batches=3):
    """Run planning, batched answers, and one evidence review round."""
    evidence = _evidence_context(evidence_catalog)
    evidence_ids = {item["evidence_id"] for item in evidence}
    context = {
        "task": str((local_report or {}).get("title") or "当前资料包分析"),
        "evidence_catalog": evidence,
        "local_report_summary": {
            key: (local_report or {}).get(key)
            for key in ("title", "executive_summary", "overview", "statistics")
            if (local_report or {}).get(key) is not None
        },
    }
    shared_prefix = "已核验业务上下文：\n{}\n\n".format(_json(context))

    planning_prompt = shared_prefix + """这是第1轮：问题规划。
生成恰好9个互不重复的问题，覆盖目标、背景、范围、技术路线、参与者、时间、投入产出、验收、风险和证据缺口。
只能填写证据目录中存在的 candidate_evidence_ids。
输出：{"turn":"question_planning","questions":[{"question_id":"Q-001","question":"...","purpose":"...","required_evidence_types":[],"candidate_evidence_ids":[],"priority":"high"}]}"""
    with trace_context(parent_call_ids=[]):
        planning = llm.chat_json(
            SYSTEM_PROMPT, planning_prompt,
            required_fields=("turn", "questions"), output_context="结构化问答-问题规划",
            # The planning JSON reached its prior limit; use the successful
            # retry budget for the initial request.
            max_tokens=2800, long_output=True, retries=0, timeout=request_timeout,
        )
    planning_call_id = last_call_id()
    questions = _normalise_questions(planning["json"])
    for item in questions:
        item["candidate_evidence_ids"] = [
            value for value in item["candidate_evidence_ids"] if value in evidence_ids
        ]

    answer_rounds = []
    answer_call_ids = []
    answers = []
    for batch_index, batch in enumerate(_partition(questions, answer_batches), 1):
        answer_prompt = shared_prefix + """这是第2轮的第{batch_index}组：证据回答。
上一轮的问题规划为：{plan}
本组问题为：{questions}
逐题回答；direct_facts只能写证据直接支持的事实，inferences必须明确是推论。证据不足时不得猜测。
输出：{{"turn":"evidence_answering","batch":{batch_index},"answers":[{{"question_id":"Q-001","question":"...","answer":"...","direct_facts":[],"inferences":[],"evidence_ids":[],"confidence":0.0,"answer_status":"answered|partially_answered|unanswered","missing_information":[]}}]}}""".format(
            batch_index=batch_index, plan=_json(questions), questions=_json(batch)
        )
        with trace_context(parent_call_ids=[planning_call_id] if planning_call_id else []):
            result = llm.chat_json(
                SYSTEM_PROMPT, answer_prompt,
                required_fields=("turn", "answers"), output_context="结构化问答-分组证据回答",
                max_tokens=1600, long_output=True, retries=0, timeout=request_timeout,
            )
        if last_call_id():
            answer_call_ids.append(last_call_id())
        payload = result["json"]
        for answer in payload.get("answers") or []:
            if isinstance(answer, dict):
                answer["evidence_ids"] = [
                    value for value in answer.get("evidence_ids") or [] if value in evidence_ids
                ]
                answers.append(answer)
        answer_rounds.append(payload)

    review_prompt = shared_prefix + """这是第3轮：综合审核与修订。
问题规划：{plan}
前序回答：{answers}
逐题检查证据编号是否存在、证据是否真正支持答案、是否混淆事实与推论、是否遗漏冲突或信息缺口。
必须输出每个问题的最终修订答案；删除无证据支持的数字、日期、主体和因果关系。
输出：{{"turn":"answer_review","review_results":[{{"question_id":"Q-001","issues":[],"removed_unsupported_statements":[],"added_evidence_ids":[],"final_answer":"...","final_evidence_ids":[],"final_confidence":0.0,"final_status":"supported|partially_supported|insufficient"}}]}}""".format(
        plan=_json(questions), answers=_json(answers)
    )
    with trace_context(parent_call_ids=answer_call_ids):
        review = llm.chat_json(
            SYSTEM_PROMPT, review_prompt,
            required_fields=("turn", "review_results"), output_context="结构化问答-答案审核",
            max_tokens=1800, long_output=True, retries=0, timeout=request_timeout,
        )
    review_results = []
    for item in review["json"].get("review_results") or []:
        if not isinstance(item, dict):
            continue
        item["final_evidence_ids"] = [
            value for value in item.get("final_evidence_ids") or [] if value in evidence_ids
        ]
        item["added_evidence_ids"] = [
            value for value in item.get("added_evidence_ids") or [] if value in evidence_ids
        ]
        review_results.append(item)

    return {
        "stage": "structured_qa",
        "turns": {
            "question_planning": {"turn": "question_planning", "questions": questions},
            "evidence_answering": answer_rounds,
            "answer_review": {"turn": "answer_review", "review_results": review_results},
        },
        "final_answers": review_results,
        "statistics": {
            "question_count": len(questions),
            "answer_batch_count": len(answer_rounds),
            "model_call_count": 2 + len(answer_rounds),
            "evidence_count": len(evidence),
        },
    }
