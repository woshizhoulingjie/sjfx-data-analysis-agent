"""Per-file fast-summary citation admission, with no app or model service."""
import copy
import json

import pytest

from config import Config
from services import preliminary_summary_fast as fast
from services.preliminary_summary_input import build_preliminary_input


def row(job_id="a", path="a.txt", evidence_id="E-a"):
    text = "材料仅验证清晰输入，噪声条件尚未验证。"
    document = {"text": text, "source": {"path": path}, "structure": {"title": path},
                "evidence": [{"evidence_id": evidence_id, "source_path": path, "text": text}]}
    return {"job": {"id": job_id, "scan_id": "fixture", "owner_id": "owner", "options": {"path": path}},
            "document": document, "input": build_preliminary_input(document), "stamp": {}, "input_build_ms": 0}


def response_item(source_row, ids=None):
    item = {"file_id": source_row["job"]["id"], "path": source_row["job"]["options"]["path"],
            "title": "条件验证", "core_summary": source_row["document"]["text"],
            "conclusions": [source_row["document"]["text"]]}
    if ids is not None:
        item["claim_evidence"] = [{"statement": source_row["document"]["text"], "evidence_ids": ids}]
    return item


def validate(rows, items):
    return fast._validated({"json": {"documents": items}}, rows)


def test_short_input_exposes_real_ids_and_legal_citations_keep_same_output_shape():
    source_row = row()
    item = response_item(source_row, ["E-a"])
    before = copy.deepcopy(item)
    assert "原文证据ID=E-a" in source_row["input"]
    assert validate([source_row], [item]) == {"a": before}
    assert item == before


@pytest.mark.parametrize("bad_ids", (["E-real-0-1"], ["E-b"], ["E-a", "E-fabricated"], [""]))
def test_unknown_cross_file_or_mixed_citations_reject_only_that_file(bad_ids):
    first, sibling = row(), row("b", "b.txt", "E-b")
    bad, good = response_item(first, bad_ids), response_item(sibling, ["E-b"])
    assert validate([first, sibling], [bad, good]) == {"b": good}


def test_document_id_not_sent_in_this_bounded_input_is_rejected():
    source_row = row()
    source_row["document"]["evidence"].append({"evidence_id": "E-omitted", "text": "其他章节"})
    assert validate([source_row], [response_item(source_row, ["E-omitted"])]) == {}


def test_id_in_source_label_must_also_exist_in_current_document():
    source_row = row()
    source_row["input"] += "\n局限原文(ID=E-fabricated)：伪ID与真实正文不能混用。"
    assert validate([source_row], [response_item(source_row, ["E-fabricated"])]) == {}


def test_id_prefix_and_prose_mentions_do_not_admit_an_omitted_reference():
    source_row = row(evidence_id="E-a-long")
    source_row["document"]["evidence"].append({"evidence_id": "E-a", "text": "未送入章节"})
    source_row["input"] += "\n正文讨论了字符串 E-a，未将它作为原文证据ID。"
    assert validate([source_row], [response_item(source_row, ["E-a"])]) == {}


@pytest.mark.parametrize("ids", (None, []))
def test_no_citation_compatibility_and_explicit_empty_ids_are_preserved(ids):
    source_row = row()
    item = response_item(source_row, ids)
    assert validate([source_row], [item]) == {"a": item}


class Store:
    def __init__(self, rows): self.rows = {r["job"]["id"]: r for r in rows}
    def preliminary_document_snapshot(self, job):
        source_row = self.rows[job["id"]]
        return {"document": copy.deepcopy(source_row["document"]), "stamp": {}}
    def is_job_cancel_requested(self, _job_id): return False


class Model:
    model = "fixture"
    def __init__(self, rows, bad_on_recovery=False):
        self.rows = {r["job"]["id"]: r for r in rows}
        self.calls = []
        self.bad_on_recovery = bad_on_recovery
    def chat_json(self, _system, prompt, **_kwargs):
        inputs = json.loads(prompt.split("分析材料：\n", 1)[1])
        self.calls.append([item["file_id"] for item in inputs])
        items = []
        for item in inputs:
            source_row = self.rows[item["file_id"]]
            bad = item["file_id"] == "a" and (len(inputs) > 1 or self.bad_on_recovery)
            ids = ["E-real-0-1", "E-real-0-2"] if bad else [source_row["document"]["evidence"][0]["evidence_id"]]
            items.append(response_item(source_row, ids))
        return {"json": {"documents": items}, "model": self.model, "usage": {}}


def generate(monkeypatch, bad_on_recovery=False):
    rows = [row(), row("b", "b.txt", "E-b")]
    jobs = [r["job"] for r in rows]
    monkeypatch.setattr(fast, "collect_jobs", lambda *_args, **_kwargs: jobs)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_BATCH_SIZE", 2)
    monkeypatch.setattr(Config, "PRELIMINARY_SUMMARY_CONCURRENCY", 1)
    monkeypatch.setattr(Config, "LLM_CONTEXT_TOKENS", 32768)
    model = Model(rows, bad_on_recovery)
    fallback_calls = []
    def fallback(document, path, reason):
        fallback_calls.append(path)
        return {"core_summary": document["text"], "warnings": [reason]}
    packets = fast.generate_ready_results(model, Store(rows), jobs[0], fallback)["_preliminary_ready_results"]
    return model, packets, fallback_calls


def test_invalid_batch_file_recovers_once_without_repeating_legal_sibling(monkeypatch):
    model, packets, fallback_calls = generate(monkeypatch)
    assert model.calls == [["a", "b"], ["a"]]
    assert fallback_calls == []
    assert packets["a"]["result"]["summary"]["claim_evidence"][0]["evidence_ids"] == ["E-a"]
    assert packets["b"]["result"]["summary"]["claim_evidence"][0]["evidence_ids"] == ["E-b"]
    assert all(not packet["result"]["degraded"] for packet in packets.values())
    assert "E-real-0" not in json.dumps(packets, ensure_ascii=False)


def test_invalid_recovery_discards_raw_citations_before_fresh_local_fallback(monkeypatch):
    model, packets, fallback_calls = generate(monkeypatch, bad_on_recovery=True)
    assert model.calls == [["a", "b"], ["a"]]
    assert fallback_calls == ["a.txt"]
    fallback_summary = packets["a"]["result"]["summary"]
    assert fallback_summary["generated_by"] == "local-preliminary-fallback"
    assert packets["a"]["result"]["degraded"] is True
    assert "claim_evidence" not in fallback_summary
    assert packets["b"]["result"]["degraded"] is False
    assert "E-real-0" not in json.dumps(packets, ensure_ascii=False)


def test_old_unchecked_ready_contract_is_invalidated():
    source_row = row()
    job = source_row["job"]
    packet = {"identity": fast.result_identity(job), "configuration": fast.configuration_key(),
              "created_at": fast.time.time()}
    assert fast.ready_matches(job, packet)
    packet["configuration"]["contract"] = "preliminary-ready-v2-source-ids"
    assert not fast.ready_matches(job, packet)
