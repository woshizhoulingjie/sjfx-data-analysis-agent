"""Repeated context reads cannot crowd distinct conditions out of synthesis."""
from copy import deepcopy

from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.evidence_packing import pack_evidence


def source(eid="e1", **updates):
    text="若审批通过才接受报价，实际履行仍待核查。"*70
    value={"evidence_id":eid,"segment_id":"segment1","package_id":"p","message_key":"m1",
           "mime_path":"1","source_kind":"body","char_start":10,"char_end":10+len(text),
           "quote_text":text,"source_revision":"r1","index_epoch":1,"text_sha256":"text1",
           "source_sha256":"raw1","payload_sha256":"payload1",
           "body_fragments":[{"mime_path":"1","char_start":10,"char_end":10+len(text),"fragment_kind":"authored"}],
           "attribution":"authored","context_anchor":"m1","context_role":"anchor"}
    value.update(updates);return value


def test_repeated_source_spans_keep_aliases_reviews_and_complete_quotes():
    original=source()
    context=[source("c"+str(i),context_anchor="anchor"+str(i),context_role="related") for i in range(12)]
    context[0]["participants"]=[{"role":"from","normalized_address":"alice@example.com"}]
    state={"phase2":True,"context_evidence":context,"context_reviews":{
        "m1":{"evidence_ids":[c["evidence_id"] for c in context],"change_evidence_ids":["c2","c5"]}}}
    runtime=MailResearchRuntime(None)
    values=runtime._contextual_evidence(state,[deepcopy(original)])
    assert len(values)==1 and values[0]["quote_text"]==original["quote_text"]
    assert values[0]["char_end"]==original["char_end"]
    assert values[0]["participants"]==context[0]["participants"]
    refs=values[0]["source_aliases"]
    assert {ref["evidence_id"] for ref in refs}=={"e1"}|{"c"+str(i) for i in range(12)}
    assert all("quote_text" not in ref and "text" not in ref for ref in refs)
    assert state["context_reviews"]["m1"]["evidence_ids"]==["e1"]
    assert state["context_reviews"]["m1"]["change_evidence_ids"]==["e1"]
    assert len(runtime._contextual_evidence(state,values))==1
    _,packing=pack_evidence(values,100000)
    assert packing["source_total"]==1 and not packing["omitted"]


def test_distinct_spans_authors_roles_conditions_versions_and_parts_survive():
    rows=[source()]
    variants=[{"char_end":999},{"char_start":11},{"quote_text":"另一个条件"},
              {"actor":"alice@example.com"},{"actor":"bob@example.com"},{"role":"counter"},
              {"conditions":["需审批"]},{"conditions":["已审批"]},{"object_version":"v2"},
              {"source_revision":"r2"},{"index_epoch":2},{"text_sha256":"text2"},
              {"source_sha256":"raw2"},{"payload_sha256":"payload2"},
              {"source_kind":"attachment"},{"mime_path":"2"},{"page_number":2},
              {"sheet_name":"sheet2"},{"cell_range":"A1:B2"},{"attribution":"quoted"},
              {"body_fragments":[{"mime_path":"1","char_start":10,"char_end":9999,"fragment_kind":"quoted"}]}]
    rows += [source("v"+str(i),**variant) for i,variant in enumerate(variants)]
    # Hashes which were previously unknown can enrich one observed version;
    # explicit conflicting revisions/hashes/interpretations cannot merge.
    values=MailResearchRuntime._deduplicate_candidates(rows)
    assert len(values)==len(rows)
    assert {v["evidence_id"] for v in values}=={v["evidence_id"] for v in rows}


def test_missing_incidental_source_metadata_enriches_without_losing_refs():
    first=source();first.pop("text_sha256");first.pop("index_epoch")
    second=source("e2",occurrence_id="occurrence2",participants=[{"role":"from","address":"alice@example.com"}])
    values=MailResearchRuntime._deduplicate_candidates([first,second])
    assert len(values)==1 and values[0]["text_sha256"]=="text1" and values[0]["index_epoch"]==1
    assert values[0]["occurrence_id"]=="occurrence2"
    assert values[0]["retrieval_votes"]==2
    assert len(values[0]["source_aliases"])==2


def test_candidate_same_start_different_end_does_not_overwrite_offsets():
    full=source();short=source("short",char_end=20,quote_text=full["quote_text"][:10])
    assert len(MailResearchRuntime._deduplicate_candidates([full,short]))==2
    state={"phase2":True,"context_evidence":[short],"context_reviews":{}}
    values=MailResearchRuntime(None)._contextual_evidence(state,[full])
    assert len(values)==2
    assert next(v for v in values if v["evidence_id"]=="e1")["char_end"]==full["char_end"]
