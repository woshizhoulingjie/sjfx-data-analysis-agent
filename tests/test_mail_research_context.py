"""Real SQLite context gates, with a deliberately overconfident simulated model."""
import json
import time
from email.message import EmailMessage

import pytest

from config import Config
from services.storage import Storage
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.repository import MailRepository
from services.mail.research.context import checkpoint, excerpt_attribution, load_memory
from services.mail.research.intent import build_task_spec
from services.mail.research.runtime import JobCancelled, MailResearchRuntime
from services.mail.research.tools import MailResearchTools
from services.mail.research.verifier import verify_context


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    for name, value in {"MAIL_ENABLE_AGENT_MINING": True, "MAIL_ENABLE_CONVERSATIONS": True,
                        "MAIL_ENABLE_VECTOR_INDEX": False, "MAIL_ENABLE_FACT_EXTRACTION": False,
                        "MAIL_ENABLE_ATTACHMENT_VERSIONS": True, "MAIL_RERANK_ENABLED": False}.items():
        monkeypatch.setattr(Config, name, value)
    repo = MailRepository(Storage(tmp_path / "context.sqlite3"))
    repo.migrate()
    package, _ = repo.create_package(owner_id="owner", display_name="Context", source_kind="directory",
                                     source_path=str(tmp_path), source_revision="r1", config={})
    pid, count = package["package_id"], [0]

    def add(body, *, reply=None, date="Thu, 10 Sep 2026 08:00:00 +0000", attachment=False,
            sender="Acme <alice@example.com>", recipient="bob@example.com"):
        ordinal = count[0]
        count[0] += 1
        message = EmailMessage()
        message["From"], message["To"] = sender, recipient
        message["Subject"], message["Message-ID"] = "Project", "<context-{}@example.com>".format(ordinal)
        message["Date"] = date
        if reply is not None:
            message["In-Reply-To"] = "<context-{}@example.com>".format(reply)
        message.set_content(body)
        if attachment:
            message.add_attachment(b"contract details", maintype="application", subtype="octet-stream", filename="contract_v1.txt")
        raw = message.as_bytes()
        result = repo.upsert_ingested_message(package_id=pid,
            source={"locator_type": "directory", "root_path": str(tmp_path), "source_revision": "r1"},
            member=MailMember("context-{}.eml".format(ordinal), ordinal, len(raw), {"kind": "file", "path": "unused"}),
            raw=raw, parsed=parse_eml(raw))
        return result["message_key"]
    return repo, pid, add


class OverconfidentModel:
    """Ignore a withdrawal on purpose; the deterministic gate must catch it."""
    def __init__(self):
        self.payloads = []

    def chat_json(self, system, prompt, **kwargs):
        payload = json.loads(prompt)
        self.payloads.append(payload)
        evidence = next((e for e in payload.get("evidence") or [] if "初始确认" in e["quote_text"]), None)
        if not evidence:
            return {"json": {"answer": "无", "claims": [], "findings": []}}
        quote = "Acme 项目 X 初始确认。"
        return {"json": {"answer": "Acme 项目 X 已确认。", "claims": [{
            "claim_text": "Acme 项目 X 已确认", "subject_ref": "Acme", "object_ref": "项目 X",
            "predicate": "confirmed", "state": "asserted", "source_quote": quote,
            "evidence_ids": [evidence["evidence_id"]], "counter_evidence_ids": [],
        }], "findings": [{"text_zh": "Acme 项目 X 已确认", "object_ref": "项目 X",
                           "source_quote": quote, "evidence_id": evidence["evidence_id"]}]}}


def run_question(repo, pid, *, model=None, scope=None, budget=18, context_limit=24000, session=None):
    session = session or repo.create_session(pid, "owner", "Context")
    spec = build_task_spec("项目 X 是否确认", pid, "r1", 0, scope=scope, max_model_calls=1, max_tool_calls=budget)
    turn = repo.create_turn(session["session_id"], "owner", spec["objective"], spec)
    runtime = MailResearchRuntime(repo, model=model or OverconfidentModel(), context_limit=context_limit)
    runtime._query_terms = lambda *args: ["初始确认"]
    final = runtime.run(turn["turn_id"], "owner")
    return final, repo.get_turn(turn["turn_id"], "owner"), session


def test_later_withdrawal_is_read_even_without_search_match(corpus):
    repo, pid, add = corpus
    original = add("Acme 项目 X 初始确认。")
    later = add("Acme 项目 X 后来撤回。", reply=0, date="Fri, 11 Sep 2026 08:00:00 +0000")
    repo.finish_import(pid, "owner")
    model = OverconfidentModel()
    final, turn, _ = run_question(repo, pid, model=model)
    assert {e["message_key"] for e in model.payloads[0]["evidence"]} == {original, later}
    claim = final["claims"][0]
    assert claim["state"] == "conflicting"
    assert claim["support_status"] == "context_qualified"
    assert claim["counter_evidence_ids"]
    assert final["business_relations"] == []
    assert final["findings"][0]["status"] == "needs_review"
    assert turn["sufficiency"] == "conflicting"
    # Context never inflates the retrieval hit/candidate universe.
    result = repo.get_result_set(final["result_set_id"], "owner")
    assert [item["item_id"] for item in result["items"]] == [original]
    assert final["research_memory"]["contradictions"]
    assert final["research_memory"]["conditions"]


def test_quoted_confirmation_cannot_become_sender_statement(corpus):
    repo, pid, add = corpus
    add("请参考历史记录。\nOn Thu Alice wrote:\n> Acme 项目 X 初始确认。\n")
    repo.finish_import(pid, "owner")
    final, turn, _ = run_question(repo, pid)
    assert final["claims"][0]["support_status"] == "context_incomplete"
    assert "引用" in final["claims"][0]["verification"]["reason"]
    assert final["business_relations"] == []
    assert final["findings"][0]["status"] == "needs_review"


def test_exact_excerpt_uses_authored_region_of_mixed_body():
    source = {"source_kind": "body", "char_start": 0,
              "quote_text": "Acme 项目 X 已确认。\n> Acme 项目 Y 取消。",
              "body_fragments": [{"fragment_kind": "authored", "char_start": 0, "char_end": 15},
                                 {"fragment_kind": "quoted", "char_start": 15, "char_end": 40}]}
    assert excerpt_attribution(source, "Acme 项目 X 已确认。") == "authored"
    assert excerpt_attribution(source) == "mixed"


def test_scope_does_not_leak_later_reply_or_assert_missing_reply_is_absent(corpus):
    repo, pid, add = corpus
    original = add("Acme 项目 X 初始确认。")
    secret = add("PRIVATE_TOKEN Acme 项目 X 后来撤回。", reply=0, date="Fri, 11 Sep 2026 08:00:00 +0000")
    repo.finish_import(pid, "owner")
    model = OverconfidentModel()
    final, turn, _ = run_question(repo, pid, model=model,
                                 scope={"message_keys": [original], "include_explicit_attachments": False})
    assert "PRIVATE_TOKEN" not in json.dumps(model.payloads, ensure_ascii=False)
    assert all(e["message_key"] != secret for e in final["evidence"])
    assert final["claims"][0]["support_status"] == "context_incomplete"
    assert any("related_messages_outside_scope" in r["gaps"] for r in final["context_review"])
    assert turn["sufficiency"] == "partial"
    with pytest.raises(ValueError):
        MailResearchTools(repo, "owner", pid, allowed_keys={original}).read_context(secret)
    with pytest.raises(ValueError):
        MailResearchTools(repo, "other", pid).read_context(original)


def test_tool_budget_keeps_candidates_and_reports_unread_context(corpus):
    repo, pid, add = corpus
    original = add("Acme 项目 X 初始确认。")
    repo.finish_import(pid, "owner")
    final, turn, _ = run_question(repo, pid, budget=2)
    assert final["coverage"]["research"]["tool_calls"] == 2
    assert final["coverage"]["research"]["tool_limit_reached"]
    assert final["claims"][0]["support_status"] == "context_incomplete"
    assert turn["sufficiency"] == "partial"
    assert final["limitations"]
    assert [i["item_id"] for i in repo.get_result_set(final["result_set_id"], "owner")["items"]] == [original]


def test_bounded_synthesis_is_valid_json_and_never_marks_truncated_read_complete(corpus):
    repo, pid, add = corpus
    add("Acme 项目 X 初始确认。" + "很长正文" * 2000)
    repo.finish_import(pid, "owner")
    model = OverconfidentModel()
    final, turn, _ = run_question(repo, pid, model=model, context_limit=1000)
    assert len(json.dumps(model.payloads[0], ensure_ascii=False)) <= 1000
    assert final["coverage"]["research"]["analysis_truncated"]
    assert all(not r["complete"] for r in final["context_review"])
    assert turn["sufficiency"] == "partial"
    assert any("预算" in gap for gap in final["limitations"])


def test_research_checkpoint_survives_turns_but_is_not_new_evidence(corpus):
    repo, pid, add = corpus
    original = add("Acme 项目 X 初始确认。")
    add("Acme 项目 X 后来撤回。", reply=0, date="Fri, 11 Sep 2026 08:00:00 +0000")
    repo.finish_import(pid, "owner")
    first, first_turn, session = run_question(repo, pid)
    assert first["research_memory"]["contradictions"]
    model = OverconfidentModel()
    final, turn, _ = run_question(repo, pid, model=model, session=session)
    assert model.payloads[0]["prior_memory"]["contradictions"]
    assert turn["status"] == "completed"
    next_turn = repo.create_turn(session["session_id"], "owner", "next", build_task_spec("next", pid, "r1", 0))
    scoped = load_memory(repo, session["session_id"], next_turn["turn_id"], "owner", pid, {original}, "r1")
    assert scoped["contradictions"] == []
    stale = load_memory(repo, session["session_id"], next_turn["turn_id"], "owner", pid, None, "r2")
    assert stale["stale"] and not stale["facts"]
    isolated = load_memory(repo, session["session_id"], next_turn["turn_id"], "another", pid, None, "r1")
    assert not isolated["contradictions"]


def test_cancellation_is_checked_inside_context_reads(corpus):
    repo, pid, add = corpus
    original = add("Acme 项目 X 初始确认。" + "长正文" * 5000)
    repo.finish_import(pid, "owner")
    session = repo.create_session(pid, "owner", "Cancel")
    spec = build_task_spec("确认", pid, "r1", 0, max_model_calls=0)
    turn = repo.create_turn(session["session_id"], "owner", "确认", spec)
    checked = [0]
    def cancel():
        checked[0] += 1
        return checked[0] >= 5
    runtime = MailResearchRuntime(repo, cancel_check=cancel)
    state = {"started": time.monotonic(), "max_seconds": 60, "max_tool_calls": 10,
             "tool_calls": 0, "deadline_reached": False, "tool_limit_reached": False,
             "context_reviews": {}, "context_evidence": [], "context_gaps": []}
    with pytest.raises(JobCancelled):
        runtime._review_context(turn["turn_id"], "owner", state, MailResearchTools(repo, "owner", pid),
                                [{"message_key": original}], None)
    assert repo.get_turn(turn["turn_id"], "owner")["status"] == "cancelled"


def test_text_bound_keeps_all_candidate_ids():
    tools = MailResearchTools(None, "owner", "package", context_limit=1000)
    result = tools._bounded_items({"items": [{"message_key": "m{}".format(n), "segment_id": "s{}".format(n),
                                              "quote_text": "x" * 2000} for n in range(50)]})
    assert len(result["items"]) == 50
    assert result["truncated"]


def test_unrelated_later_revision_does_not_automatically_contradict(corpus):
    repo, pid, add = corpus
    add("Acme 项目 X 初始确认。")
    add("Acme 项目 Y 后来撤回。", reply=0, date="Fri, 11 Sep 2026 08:00:00 +0000")
    repo.finish_import(pid, "owner")
    final, _, _ = run_question(repo, pid)
    assert final["claims"][0]["support_status"] == "supported"
    assert len(final["business_relations"]) == 1
    assert final["findings"][0]["status"] == "context_verified"


def test_missing_reply_target_is_explicit_gap(corpus):
    repo, pid, add = corpus
    key = add("Acme 项目 X 初始确认。", reply=900)
    repo.finish_import(pid, "owner")
    result = MailResearchTools(repo, "owner", pid).read_context(key)
    assert not result["review"]["complete"]
    assert "missing_reply_target" in result["review"]["gaps"]


def test_bounded_context_cannot_assert_final_from_subset():
    source = {"evidence_id": "e", "message_key": "m", "source_kind": "attachment", "quote_text": "Acme 项目 X 确认。"}
    check = verify_context({"evidence_ids": ["e"], "object_ref": "项目 X", "claim_text": "Acme 项目 X 最终确认"},
                           {"e": source}, {"m": {"message_keys": ["m"], "complete": True}})
    assert check["support_status"] == "chronology_unproven"


def test_v2_explicit_empty_and_invalid_selections_do_not_fall_back_to_package(corpus):
    repo, pid, add = corpus
    key = add("Acme 项目 X 初始确认。")
    runtime = MailResearchRuntime(repo)
    assert runtime._allowed_message_keys({"analysis_version": 2, "scope": {}}, pid, "owner") is None
    assert runtime._allowed_message_keys({"analysis_version": 2, "scope": {"message_keys": []}}, pid, "owner") == set()
    assert runtime._allowed_message_keys({"scope": {"message_keys": []}}, pid, "owner") is None
    assert runtime._allowed_message_keys({"scope": {"message_keys": ["missing"]}}, pid, "owner") == set()
    final, _, _ = run_question(repo, pid, scope={"message_keys": ["missing"]})
    assert final["evidence"] == []
    assert repo.get_result_set(final["result_set_id"], "owner")["items"] == []


def test_unified_date_attachment_mailbox_and_role_scopes_are_intersected(corpus):
    repo, pid, add = corpus
    included = add("Acme 项目 X 初始确认。 scoped_marker")
    add("Acme 项目 X 初始确认。", date="Fri, 11 Sep 2026 08:00:00 +0000", attachment=True)
    add("Acme 项目 X 初始确认。", sender="Other <other@example.com>", recipient="else@example.com")
    repo.finish_import(pid, "owner")
    scope = {"date_from": "2026-09-10", "date_to": "2026-09-10", "has_attachment": False,
             "participant": "alice@example.com", "roles": ["from"], "q": "scoped_marker"}
    runtime = MailResearchRuntime(repo)
    assert runtime._allowed_message_keys({"scope": scope}, pid, "owner") == {included}
    wrong_role = {**scope, "roles": ["to"]}
    assert runtime._allowed_message_keys({"scope": wrong_role}, pid, "owner") == set()
    final, _, _ = run_question(repo, pid, scope=scope)
    assert {e["message_key"] for e in final["evidence"]} == {included}
    assert {i["item_id"] for i in repo.get_result_set(final["result_set_id"], "owner")["items"]} == {included}
    with pytest.raises(ValueError):
        runtime._allowed_message_keys({"scope": {"date_from": "invalid"}}, pid, "owner")


def test_scoped_search_filters_before_top_n_and_evidence_creation(corpus):
    repo, pid, add = corpus
    included = add("Acme 项目 X 初始确认。", date="Wed, 09 Sep 2026 08:00:00 +0000")
    for _ in range(51):
        add("Acme 项目 X 初始确认。 OUTSIDE_SCOPE")
    repo.finish_import(pid, "owner")
    result = MailResearchTools(repo, "owner", pid, allowed_keys={included}).search_messages("初始确认", limit=1)
    assert [i["message_key"] for i in result["items"]] == [included]
    with repo._connection() as c:
        assert {r[0] for r in c.execute("SELECT message_key FROM mail_evidence WHERE package_id=?", (pid,))} == {included}


def test_parent_result_is_intersected_with_current_filters(corpus):
    repo, pid, add = corpus
    first = add("Acme 项目 X 初始确认。")
    second = add("Acme 项目 X 初始确认。", date="Fri, 11 Sep 2026 08:00:00 +0000")
    parent = repo.create_result_set(pid, "owner", [{"item_id": first}, {"item_id": second}], {}, {})
    runtime = MailResearchRuntime(repo)
    assert runtime._allowed_message_keys({"base_result_set_id": parent, "scope": {"date_to": "2026-09-10"}}, pid, "owner") == {first}
    assert runtime._allowed_message_keys({"base_result_set_id": parent, "scope": {"message_keys": ["missing"]}}, pid, "owner") == set()


def test_checkpoint_reports_summary_caps_and_retains_history_pointer():
    evidence = [{"evidence_id": "e{}".format(i), "message_key": "m{}".format(i),
                 "quote_text": "项目 X 撤回 {}".format(i)} for i in range(40)]
    saved = checkpoint({"source_revision": "r1"}, [], evidence, {},
                       {"checkpoint_ref": {"turn_id": "prior", "step_id": "step", "sequence": 7}})
    assert len(saved["conditions"]) == 20
    assert saved["summary_truncated"]
    assert saved["omitted_counts"]["conditions"] == 20
    assert saved["prior_checkpoint_ref"]["step_id"] == "step"
    assert "final_json" in saved["history_note"]


def test_exhaustive_budget_stop_retains_hits_without_claiming_complete_manifest(corpus, monkeypatch):
    repo, pid, add = corpus
    first = add("Acme 项目 X 初始确认。")
    add("尚未检查的正文。")
    session = repo.create_session(pid, "owner", "Manifest")
    spec = build_task_spec("全部邮件", pid, "r1", 0)
    turn = repo.create_turn(session["session_id"], "owner", "全部邮件", spec)
    runtime = MailResearchRuntime(repo)
    state = {"started": time.monotonic() - 61, "max_seconds": 60, "deadline_reached": False,
             "package_message_total": 2}
    monkeypatch.setattr(repo, "list_messages", lambda *a, **k: pytest.fail("budget must stop enumeration"))
    items, manifest = runtime._result_items(turn["turn_id"], pid, "owner", spec,
                                           [{"message_key": first, "evidence_id": "e"}], None, state)
    assert items[0]["item_id"] == first
    assert manifest["manifest_complete"] is False
    assert manifest["fixed_manifest"] is False
    assert manifest["manifest_omitted"] == 1
    assert runtime._sufficiency([{}], {}, manifest) == "partial"


def test_scoped_memory_omits_unproven_plain_text_questions(corpus):
    repo, pid, add = corpus
    first = add("Acme 项目 X 初始确认。")
    session = repo.create_session(pid, "owner", "Memory")
    spec = build_task_spec("确认", pid, "r1", 0)
    prior = repo.create_turn(session["session_id"], "owner", "确认", spec)
    repo.record_research_step(prior["turn_id"], "research_checkpoint",
                             {"source_revision": "r1", "open_questions": ["PRIVATE_OUTSIDE_SCOPE"],
                              "facts": [], "conditions": [], "contradictions": []})
    current = repo.create_turn(session["session_id"], "owner", "确认", spec)
    memory = load_memory(repo, session["session_id"], current["turn_id"], "owner", pid, {first}, "r1")
    assert "PRIVATE_OUTSIDE_SCOPE" not in json.dumps(memory, ensure_ascii=False)


def test_plain_if_condition_is_preserved_by_qualification_gate():
    from services.mail.research.verifier import verify_polarity
    assert not verify_polarity("项目 X 已批准", "Project X approved if a permit is issued")
    first = {"evidence_id": "e1", "message_key": "m1", "source_kind": "attachment",
             "sent_at_utc": "2026-01-01", "quote_text": "Acme 项目 X 已确认。"}
    later = {"evidence_id": "e2", "message_key": "m2", "source_kind": "attachment",
             "sent_at_utc": "2026-01-02", "quote_text": "项目 X is valid only if a permit is issued."}
    result = verify_context({"evidence_ids": ["e1"], "object_ref": "项目 X"}, {"e1": first, "e2": later},
                            {"m1": {"complete": True, "message_keys": ["m1", "m2"], "change_evidence_ids": ["e2"]}})
    assert result["support_status"] == "context_qualified"


def test_three_message_negotiation_waiting_for_internal_approval_is_not_confirmed(corpus):
    repo, pid, add = corpus
    add("A公司希望项目 X 本周签约。", sender="A公司 <a@example.com>")
    add("B公司要求修改项目 X 的付款条款。", reply=0, sender="B公司 <b@example.com>",
        date="Fri, 11 Sep 2026 08:00:00 +0000")
    third = add("A公司同意修改项目 X 的付款条款，签约需要等内部审批后确认。", reply=1,
                sender="A公司 <a@example.com>", date="Sat, 12 Sep 2026 08:00:00 +0000")
    repo.finish_import(pid, "owner")
    class Model:
        def chat_json(self, system, prompt, **kwargs):
            data = json.loads(prompt)
            evidence = next(e for e in data["evidence"] if e["message_key"] == third)
            return {"json": {"answer": "已确认签约", "claims": [{
                "claim_text": "A公司已确认项目 X 签约", "subject_ref": "A公司", "object_ref": "项目 X",
                "predicate": "confirmed", "state": "asserted", "evidence_ids": [evidence["evidence_id"]],
                "source_quote": evidence["quote_text"],
            }]}}
    session = repo.create_session(pid, "owner", "Negotiation")
    spec = build_task_spec("项目 X 签约是否确认", pid, "r1", 0, max_model_calls=1)
    turn = repo.create_turn(session["session_id"], "owner", spec["objective"], spec)
    runtime = MailResearchRuntime(repo, model=Model())
    runtime._query_terms = lambda *args: ["签约"]
    final = runtime.run(turn["turn_id"], "owner")
    assert final["claims"][0]["support_status"] != "supported"
    assert final["claims"][0]["state"] not in {"asserted", "confirmed"}
    assert final["business_relations"] == []
    assert repo.get_turn(turn["turn_id"], "owner")["sufficiency"] == "partial"


def test_small_segment_context_reports_synthesis_inventory_omissions(corpus):
    repo, pid, add = corpus
    repo.text_segment_chars = 8
    add("Alice confirms delivery date June 10.")
    class Model:
        def chat_json(self, system, prompt, **kwargs):
            if kwargs["required_fields"] == ("queries",):
                return {"json": {"queries": ["delivery"]}}
            if kwargs["required_fields"] == ("actions",):
                return {"json": {"actions": [{"name": "stop"}]}}
            evidence = json.loads(prompt)["evidence"][0]
            return {"json": {"answer": "确认", "claims": [{
                "claim_text": "Alice confirms delivery date June 10", "subject_ref": "Alice", "predicate": "confirmed",
                "object_ref": "delivery date June 10", "state": "asserted", "evidence_ids": [evidence["evidence_id"]]}]}}
    session = repo.create_session(pid, "owner", "Small")
    spec = build_task_spec("查找交付日期的确认", pid, "r1", 0, max_tool_calls=8, max_model_calls=3, max_seconds=60)
    turn = repo.create_turn(session["session_id"], "owner", spec["objective"], spec)
    final = MailResearchRuntime(repo, model=Model()).run(turn["turn_id"], "owner")
    assert final["claims"][0]["support_status"] == "supported", (final["claims"][0], final["evidence"][0])


def test_short_finding_cannot_hide_condition_in_same_source(corpus):
    repo, pid, add = corpus
    add("Acme 项目 X 初始确认。前提是等内部审批后确认。")
    repo.finish_import(pid, "owner")
    final, turn, _ = run_question(repo, pid)
    assert final["findings"][0]["status"] == "needs_review"
    assert "同一来源" in final["findings"][0]["context_verification"]["reason"]
    assert turn["sufficiency"] == "partial"


def test_finding_cannot_promote_proposal_to_confirmation():
    source = {"evidence_id": "e", "message_key": "m", "source_kind": "attachment",
              "quote_text": "Acme 项目 X 希望本周签约。"}
    result = verify_context({"evidence_ids": ["e"], "source_quote": source["quote_text"],
                             "object_ref": "项目 X", "claim_text": "Acme 项目 X 已确认本周签约"},
                            {"e": source}, {"m": {"message_keys": ["m"], "complete": True}})
    assert result["support_status"] == "context_incomplete"
    assert "确认状态" in result["reason"]


def test_scoped_phrase_spanning_tiny_segments_is_not_lost(corpus):
    repo, pid, add = corpus
    repo.text_segment_chars = 8
    key = add("Alice confirms delivery date June 10.")
    result = MailResearchTools(repo, "owner", pid, allowed_keys={key}).search_messages("delivery date")
    assert result["items"]
    assert "delivery date" in result["items"][0]["quote_text"]
