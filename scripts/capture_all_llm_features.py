#!/usr/bin/env python3
"""Capture and finalize SJFX non-translation LLM feature datasets.

This runner is intentionally environment-gated and writes only beneath the
selected run directory plus an isolated local state directory.  It can append
real Flask business-entry calls and then merge every scenario trace in actual
start-time order into the benchmark deliverables required by the capture plan.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import shutil


EXPECTED_FEATURES = (
    "document.single_preview", "document.batch_preview", "document.deep_analysis",
    "document.chunk_analysis", "document.chunk_merge", "document.structured_analysis",
    "document.table_conclusion", "folder.summary", "folder.node_summary",
    "folder.cross_file_synthesis", "folder.qa_evidence", "package.semantic_cluster_naming",
    "package.lexical_topic_naming", "package.subdirection_naming", "package.topic_planning",
    "conversation.answer_generation", "conversation.cross_file_comparison",
    "conversation.timeline", "conversation.relationship", "conversation.conflict_detection",
    "conversation.risk_analysis", "conversation.summary", "conversation.research_report",
    "conversation.follow_up", "conversation.claim_revision", "multiturn_qa.question_planning",
    "multiturn_qa.evidence_answer", "multiturn_qa.review", "report.research_direction",
    "report.enhancement", "report.output_length_retry", "report.json_retry",
    "large_package.light_summary", "large_package.batch_naming",
    "large_package.hierarchy_naming", "large_package.topic_generation",
    "large_package.merge_decision", "large_package.name_improvement",
    "large_package.research_direction", "large_package.reporting",
    "mail.fact_extraction", "mail.research", "mail.issue_synthesis",
    "mail.conflict_risk", "mail.final_synthesis", "general_chat.generation",
    "general_chat.model_test",
)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_general(run_dir, state_dir):
    scenario_dir = Path(run_dir) / "scenarios" / "general_chat"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    if (scenario_dir / "trace.jsonl").exists():
        raise RuntimeError("general_chat trace already exists")
    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "SJFX_STATE_DIR": str(state_dir),
        "SJFX_LLM_TRACE_PATH": str((scenario_dir / "trace.jsonl").resolve()),
        "SJFX_TRACE_WORKFLOW_ID": Path(run_dir).name + "-general-chat",
        "SJFX_TRACE_SCENARIO_ID": "general_chat",
        "SJFX_BENCHMARK_DETERMINISTIC": "1",
        "LLM_MAX_CONCURRENCY": "1",
        "HOST": "127.0.0.1",
        "SJFX_EXTRA_BIND_HOSTS": "",
        "AUTH_REQUIRED": "0",
        "ENABLE_TRANSLATION": "0",
        "ENABLE_IMPORT_TRANSLATION": "0",
        "ENABLE_TRANSLATION_REVIEW": "0",
        "TRANSLATION_PIPELINE_ENABLED": "0",
    })
    from app import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    responses = []
    for endpoint, payload in (
        ("/api/test-model", {}),
        ("/api/general-chat", {
            "question": "请说明如何对两份项目资料进行事实、冲突、风险和时间线对比，并给出可执行步骤。",
            "messages": [],
        }),
    ):
        response = client.post(endpoint, json=payload)
        responses.append({
            "endpoint": endpoint,
            "status_code": response.status_code,
            "response": response.json(),
        })
        if response.status_code >= 400:
            raise RuntimeError("{} failed with HTTP {}".format(endpoint, response.status_code))
    atomic_json(scenario_dir / "business_responses.json", responses)
    return responses


def capture_mail(run_dir, state_dir, mail_dirs):
    scenario_dir = Path(run_dir) / "scenarios" / "mail_fact_and_research"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    trace_path = scenario_dir / "trace.jsonl"
    if trace_path.exists():
        raise RuntimeError("mail trace already exists")
    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "SJFX_STATE_DIR": str(state_dir), "SJFX_LLM_TRACE_PATH": str(trace_path.resolve()),
        "SJFX_TRACE_WORKFLOW_ID": Path(run_dir).name + "-mail",
        "SJFX_TRACE_SCENARIO_ID": "mail_fact_and_research",
        "SJFX_BENCHMARK_DETERMINISTIC": "1", "LLM_MAX_CONCURRENCY": "1",
        "HOST": "127.0.0.1", "SJFX_EXTRA_BIND_HOSTS": "", "AUTH_REQUIRED": "0",
        "SCAN_ALLOWED_ROOTS": str(Path(run_dir).resolve().parents[2]),
        "ENABLE_TRANSLATION": "0", "ENABLE_IMPORT_TRANSLATION": "0",
        "ENABLE_TRANSLATION_REVIEW": "0", "TRANSLATION_PIPELINE_ENABLED": "0",
        "MAIL_ENABLE_VECTOR_INDEX": "0", "MAIL_ENABLE_FACT_EXTRACTION": "1",
    })
    from app import app, storage
    from fastapi.testclient import TestClient
    from services.mail.jobs import run_claimed_mail_job

    def require(response, label):
        payload = response.json()
        if response.status_code >= 400:
            raise RuntimeError("{} failed HTTP {}: {}".format(label, response.status_code, payload))
        return payload

    def run_job(job_id, max_slices=100):
        outputs = []
        for _index in range(max_slices):
            job = storage.get_job(job_id)
            if not job:
                raise RuntimeError("job not found: {}".format(job_id))
            result = run_claimed_mail_job(job)
            outputs.append(result)
            if not result.get("_requeue_slice") and not result.get("_defer_slice"):
                return outputs
        raise RuntimeError("mail job exceeded slice limit: {}".format(job_id))

    records = []
    with TestClient(app) as client:
        for index, directory in enumerate(mail_dirs, 1):
            directory = Path(directory).resolve()
            created = require(client.post("/api/mail/packages", json={
                "source": {"path": str(directory), "kind": "directory"},
                "display_name": directory.parent.name + "-邮件",
                "idempotency_key": "feature-capture-mail-{}".format(index),
            }), "create mail package")
            package_id = created["package"]["package_id"]
            import_outputs = run_job(created["job_id"])
            facts = require(client.post("/api/mail/packages/{}/index".format(package_id), json={"stage": "facts"}), "queue facts")
            fact_outputs = []
            for queued in facts.get("jobs") or []:
                fact_outputs.extend(run_job(queued["job_id"]))
            session = require(client.post("/api/mail/research-sessions", json={
                "package_id": package_id, "title": "全功能采集邮件研究",
            }), "create research session")["session"]
            turn = require(client.post(
                "/api/mail/research-sessions/{}/turns".format(session["session_id"]),
                json={"question": "归纳主要事项、参与者、日期与事实，识别相互冲突的信息和潜在风险，并给出带证据的最终研究结论。"},
            ), "create research turn")
            research_outputs = run_job(turn["job_id"])
            final_turn = require(client.get("/api/mail/research-turns/{}".format(turn["turn"]["turn_id"])), "read research turn")
            records.append({
                "source": str(directory), "package_id": package_id,
                "import": import_outputs, "facts": fact_outputs,
                "research": research_outputs, "turn": final_turn,
            })
    atomic_json(scenario_dir / "business_responses.json", records)
    return records


def capture_documents(run_dir, state_dir, source_root):
    scenario_dir = Path(run_dir) / "scenarios" / "document_preview_and_deep_analysis"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    trace_path = scenario_dir / "trace.jsonl"
    if trace_path.exists():
        raise RuntimeError("document trace already exists")
    state_dir = Path(state_dir).resolve(); state_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "SJFX_STATE_DIR": str(state_dir), "SJFX_LLM_TRACE_PATH": str(trace_path.resolve()),
        "SJFX_TRACE_WORKFLOW_ID": Path(run_dir).name + "-documents",
        "SJFX_TRACE_SCENARIO_ID": "document_preview_and_deep_analysis",
        "SJFX_BENCHMARK_DETERMINISTIC": "1", "LLM_MAX_CONCURRENCY": "1",
        "ENABLE_TRANSLATION": "0", "ENABLE_IMPORT_TRANSLATION": "0",
        "ENABLE_TRANSLATION_REVIEW": "0", "TRANSLATION_PIPELINE_ENABLED": "0",
    })
    from config import Config
    from services.agent_runtime import PydanticAgentRuntime
    from services.document_analysis import analyze_document_preview, analyze_document_previews_batch, analyze_document
    from services.folder_analysis import analyze_folder
    from services.vllm import VLLMClient
    transport = VLLMClient(Config.VLLM_BASE_URL, Config.VLLM_MODEL, timeout=600, max_concurrency=1, api_key=Config.VLLM_API_KEY)
    llm = PydanticAgentRuntime(transport)
    root = Path(source_root).resolve()
    paths = sorted((path for path in root.rglob("*.txt") if path.is_file()), key=lambda p: p.stat().st_size, reverse=True)
    if len(paths) < 2:
        raise RuntimeError("at least two real text documents are required")

    def document(path):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = str(path.relative_to(root)).replace("\\", "/")
        evidence = []
        for index, start in enumerate(range(0, len(text), 1800), 1):
            part = text[start:start + 1800]
            if part.strip():
                evidence.append({"evidence_id": "{}-E{:04d}".format(path.stem, index), "source_path": relative, "section": "chars {}-{}".format(start, start + len(part)), "text": part})
        return relative, {"text": text, "parser": {"name": "plain-text", "title": path.name}, "coverage": {"complete": True}, "warnings": [], "evidence": evidence}

    first_path, first = document(paths[0]); second_path, second = document(paths[1])
    single, _ = analyze_document_preview(llm, paths[0], first_path, unified_document=first, timeout_seconds=180)
    batch, _ = analyze_document_previews_batch(llm, [{"path": first_path, "document": first}, {"path": second_path, "document": second}], timeout_seconds=240)
    deep, deep_coverage, _ = analyze_document(llm, paths[0], first_path, unified_document=first, preferred_chunk_chars=20000, max_chunks=32, context_window_tokens=32768)
    file_summaries = []
    for path, summary in ((first_path, deep), (second_path, batch[second_path]["summary"])):
        file_summaries.append({"path": path, "title": summary.get("title"), "summary": summary.get("core_summary") or summary.get("summary"), "topics": summary.get("topics") or [], "evidence_ids": [item.get("evidence_id") for item in summary.get("evidence_chain") or [] if item.get("evidence_id")]})
    context = {
        "total_files": 2, "total_dirs": 0,
        "total_size_human": "{} bytes".format(paths[0].stat().st_size + paths[1].stat().st_size),
        "type_counts": {".txt": 2}, "documents": [{"path": first_path, "payload": first}, {"path": second_path, "payload": second}],
        "file_summaries": file_summaries, "file_summary_count": 2,
        "file_summaries_complete": True, "deep_file_summaries_complete": False,
        "missing_file_summaries": [], "non_deep_file_summaries": [second_path],
        "member_paths": [first_path, second_path], "coverage": {"complete": True},
    }
    folder, _folder_result, folder_errors = analyze_folder(
        llm, context, "{}文本资料节点".format(root.name or "当前资料包")
    )
    output = {"single_preview": single, "batch_preview": batch, "deep_analysis": deep, "deep_coverage": deep_coverage, "folder_analysis": folder, "folder_errors": folder_errors}
    atomic_json(scenario_dir / "business_results.json", output)
    return output


def capture_conversations(run_dir, state_dir, source_root):
    scenario_dir = Path(run_dir) / "scenarios" / "conversation_intents"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    trace_path = scenario_dir / "trace.jsonl"
    if trace_path.exists(): raise RuntimeError("conversation trace already exists")
    state_dir = Path(state_dir).resolve(); state_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update({"SJFX_STATE_DIR": str(state_dir), "SJFX_LLM_TRACE_PATH": str(trace_path.resolve()),
        "SJFX_TRACE_WORKFLOW_ID": Path(run_dir).name + "-conversation", "SJFX_TRACE_SCENARIO_ID": "conversation_intents",
        "SJFX_BENCHMARK_DETERMINISTIC": "1", "LLM_MAX_CONCURRENCY": "1", "ENABLE_TRANSLATION": "0",
        "ENABLE_IMPORT_TRANSLATION": "0", "ENABLE_TRANSLATION_REVIEW": "0", "TRANSLATION_PIPELINE_ENABLED": "0"})
    from config import Config
    from services.agent_runtime import PydanticAgentRuntime
    from services.conversation import CallableEvidenceRetriever, ConversationEngine
    from services.llm_trace import context as trace_context
    from services.vllm import VLLMClient
    root = Path(source_root).resolve(); paths = sorted(root.glob("*.txt"), key=lambda p:p.stat().st_size, reverse=True)[:3]
    evidence = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for index, start in enumerate(range(0, min(len(text), 18000), 3000), 1):
            evidence.append({"evidence_id":"{}-C{:02d}".format(path.stem,index), "source_path":str(path.relative_to(root)).replace("\\","/"),
                "location":"字符 {}-{}".format(start,start+3000), "original_text":text[start:start+3000], "score":1.0-index*.01})
    retrieval = {"results": evidence, "coverage":{"complete":True,"indexed_files":len(paths)}, "evidence_total":len(evidence)}
    transport = VLLMClient(Config.VLLM_BASE_URL, Config.VLLM_MODEL, timeout=600, max_concurrency=1, api_key=Config.VLLM_API_KEY)
    llm = PydanticAgentRuntime(transport)
    engine = ConversationEngine(CallableEvidenceRetriever(lambda _request: retrieval), answer_model=llm, top_k=20)
    session = engine.new_session(
        "feature-capture-{}".format(root.name or "package"), title="全功能采集"
    )
    questions = (
        ("conversation.cross_file_comparison", "比较这些资料在研究对象、方法和结论方面的异同。"),
        ("conversation.timeline", "按时间顺序整理资料中的主要事件和变化。"),
        ("conversation.relationship", "分析资料中关键实体、主题和文件之间的关系。"),
        ("conversation.conflict_detection", "检测不同资料之间相互冲突或不一致的说法。"),
        ("conversation.risk_analysis", "分析资料反映的主要风险、限制和不确定性。"),
        ("conversation.summary", "总结当前全部资料的主要内容和可靠结论。"),
        ("conversation.research_report", "基于证据写一份简洁的研究报告，区分事实、判断和建议。"),
        ("conversation.follow_up", "请继续展开上一条回答中最重要的两点，并补充证据。"),
    )
    turns=[]
    for feature_id, question in questions:
        with trace_context(feature_id=feature_id, stage_id="conversation_answer", parent_call_ids=[] if not turns else None):
            turns.append(engine.ask(session, question, retrieval_override=retrieval, coverage=retrieval["coverage"]))
    atomic_json(scenario_dir / "business_results.json", turns)
    return turns


def trace_files(run_dir):
    run_dir = Path(run_dir)
    values = []
    primary = run_dir / "trace.jsonl"
    # A finalized top-level trace is an aggregate, not an input scenario.  Do
    # not feed it back into a later finalization or duplicate every call.
    primary_is_aggregate = False
    if primary.is_file():
        try:
            first = json.loads(primary.open(encoding="utf-8").readline())
            primary_is_aggregate = bool(first.get("merged_scenario_count"))
        except (OSError, ValueError):
            pass
    if primary.is_file() and not primary_is_aggregate:
        values.append(primary)
    scenarios = run_dir / "scenarios"
    if scenarios.is_dir():
        values.extend(sorted(scenarios.rglob("trace.jsonl")))
    return values


def read_traces(run_dir):
    manifests, calls = [], []
    for path in trace_files(run_dir):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in rows:
            row["_source_trace"] = str(path.relative_to(run_dir)).replace("\\", "/")
            if row.get("kind") == "TraceManifest":
                manifests.append(row)
            elif row.get("kind") == "TraceCall":
                calls.append(row)
    calls.sort(key=lambda row: (row.get("started_at") or "", int(row.get("sequence") or 0)))
    return manifests, calls


def normalized_call(call, sequence):
    raw = call.get("response") or {}
    choices = raw.get("choices") or []
    choice = choices[0] if choices else {}
    return {
        "sequence": sequence,
        "call_id": call["call_id"],
        "stage_id": call.get("stage_id"),
        "parent_call_ids": call.get("parent_call_ids") or [],
        "concurrency_group": call.get("concurrency_group"),
        "request": call.get("request") or {},
        "response": {
            "content": (choice.get("message") or {}).get("content"),
            "finish_reason": choice.get("finish_reason"),
            "usage": raw.get("usage") or {},
        },
        "input_token_ids": call.get("input_token_ids") or [],
        "output_token_ids": call.get("output_token_ids") or [],
        "source_cached_tokens": call.get("source_cached_tokens"),
    }


def finalize(run_dir, source_root):
    run_dir, source_root = Path(run_dir).resolve(), Path(source_root).resolve()
    manifests, calls = read_traces(run_dir)
    if not calls:
        raise RuntimeError("no TraceCall rows found")
    normalized = [normalized_call(call, index) for index, call in enumerate(calls, 1)]
    atomic_json(run_dir / "llm_calls.json", normalized)
    cases_path = run_dir / "cases.jsonl"
    with cases_path.open("w", encoding="utf-8", newline="\n") as handle:
        for call, row in zip(calls, normalized):
            case = {
                "schema_version": "3.0.0", "kind": "WorkloadCase", "fixture": True,
                "case_id": "sjfx.features.{:04d}".format(row["sequence"]), "agent_id": "sjfx",
                "stage_id": row["stage_id"], "mode": "replay",
                "capture": {"type": "real_capture", "source_ref": call["_source_trace"],
                    "trace_id": call.get("trace_id"), "workflow_id": call.get("workflow_id"),
                    "call_id": row["call_id"], "parent_call_ids": row["parent_call_ids"],
                    "arrival_offset_ms": call.get("arrival_offset_ms"), "sensitivity": "restricted", "transformations": []},
                "payload": {"endpoint": "/v1/chat/completions", "body": row["request"]},
                "reference": {"prompt_tokens": row["response"]["usage"].get("prompt_tokens"),
                    "completion_tokens": row["response"]["usage"].get("completion_tokens"),
                    "output_ref": "llm_calls.json#{}".format(row["sequence"])},
                "features": {"has_tools": bool(row["request"].get("tools")), "thinking": "off",
                    "structured_output": bool(row["request"].get("response_format"))},
                "extensions": {"observed_output": row["response"]["content"],
                    "input_token_ids": row["input_token_ids"], "output_token_ids": row["output_token_ids"],
                    "source_cached_tokens": row["source_cached_tokens"]},
            }
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    call_index = []
    for index, call in enumerate(calls, 1):
        call_index.append({
            "sequence": index, "call_id": call["call_id"],
            "feature_id": call.get("feature_id") or "unclassified.generation",
            "stage_id": call.get("stage_id"), "scenario_id": call.get("scenario_id"),
            "callsite": call.get("callsite") or {}, "source_trace": call["_source_trace"],
            "status": call.get("status"), "parent_call_ids": call.get("parent_call_ids") or [],
            "parent_call_ids_source": call.get("parent_call_ids_source"),
        })
    atomic_json(run_dir / "call_index.json", call_index)
    by_scenario, by_feature = defaultdict(list), defaultdict(list)
    for call in calls:
        by_scenario[call.get("scenario_id") or "unknown"].append(call)
        by_feature[call.get("feature_id") or "unclassified.generation"].append(call)
    summaries = {}
    for scenario, values in sorted(by_scenario.items()):
        usages = [(item.get("response") or {}).get("usage") or {} for item in values]
        summaries[scenario] = {
            "call_count": len(values),
            "prompt_tokens": sum(int(item.get("prompt_tokens") or 0) for item in usages),
            "completion_tokens": sum(int(item.get("completion_tokens") or 0) for item in usages),
            "source_cached_tokens": sum(int(item.get("source_cached_tokens") or 0) for item in values),
            "features": dict(sorted(Counter(item.get("feature_id") or "unclassified.generation" for item in values).items())),
        }
    atomic_json(run_dir / "scenario_summary.json", summaries)
    actual = set(by_feature)
    coverage = {
        "expected": [
            {"feature_id": feature, "hit": feature in actual, "call_count": len(by_feature.get(feature, [])),
             "reason": None if feature in actual else "not observed in this completed business run"}
            for feature in EXPECTED_FEATURES
        ],
        "unexpected_features": sorted(actual - set(EXPECTED_FEATURES)),
        "totals": {"expected": len(EXPECTED_FEATURES), "hit": sum(feature in actual for feature in EXPECTED_FEATURES)},
    }
    atomic_json(run_dir / "coverage.json", coverage)
    inputs = []
    for path in sorted(source_root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            inputs.append({"path": str(path.relative_to(source_root)).replace("\\", "/"), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    metadata = dict(manifests[0]) if manifests else {}
    metadata.pop("_source_trace", None)
    metadata.update({
        "kind": "TraceMetadata", "call_count": len(calls), "input_root": str(source_root),
        "input_files": inputs, "translation_disabled": True,
        "scenario_manifests": [{k: v for k, v in item.items() if k != "_source_trace"} for item in manifests],
    })
    atomic_json(run_dir / "metadata.json", metadata)
    translation_markers = ("translation", "translate", "翻译", "译文")
    violations = []
    for call in calls:
        feature = str(call.get("feature_id") or "").lower()
        system = str((((call.get("request") or {}).get("messages") or [{}])[0]).get("content") or "").lower()
        if any(marker in feature for marker in translation_markers) or "你是翻译" in system:
            violations.append(call["call_id"])
    validation = {
        "call_count": len(calls), "translation_call_ids": violations,
        "empty_input_token_ids": [item["call_id"] for item in calls if not item.get("input_token_ids")],
        "empty_output_token_ids": [item["call_id"] for item in calls if item.get("status") == "ok" and not item.get("output_token_ids")],
        "missing_cache_delta": [item["call_id"] for item in calls if item.get("source_cached_tokens") is None],
    }
    validation["passed"] = not any(validation[key] for key in ("translation_call_ids", "empty_input_token_ids", "empty_output_token_ids", "missing_cache_delta"))
    atomic_json(run_dir / "validation.json", validation)
    merged_manifest = dict(manifests[0]) if manifests else {"kind": "TraceManifest"}
    merged_manifest.pop("_source_trace", None)
    merged_manifest["workflow_id"] = Path(run_dir).name
    merged_manifest["merged_scenario_count"] = len(manifests)
    with (run_dir / "trace.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(merged_manifest, ensure_ascii=False) + "\n")
        for call in calls:
            clean = {key: value for key, value in call.items() if key != "_source_trace"}
            handle.write(json.dumps(clean, ensure_ascii=False) + "\n")
    return {"calls": len(calls), "coverage": coverage["totals"], "validation": validation}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("/tmp/sjfx_feature_capture"))
    parser.add_argument("--capture-general", action="store_true")
    parser.add_argument("--capture-mail", action="store_true")
    parser.add_argument("--capture-documents", action="store_true")
    parser.add_argument("--capture-conversations", action="store_true")
    parser.add_argument("--mail-dir", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    if args.capture_general:
        capture_general(args.run_dir, args.state_dir)
    if args.capture_mail:
        if not args.mail_dir:
            parser.error("--capture-mail requires at least one --mail-dir")
        capture_mail(args.run_dir, args.state_dir, args.mail_dir)
    if args.capture_documents:
        capture_documents(args.run_dir, args.state_dir, args.source_root)
    if args.capture_conversations:
        capture_conversations(args.run_dir, args.state_dir, args.source_root)
    print(json.dumps(finalize(args.run_dir, args.source_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
