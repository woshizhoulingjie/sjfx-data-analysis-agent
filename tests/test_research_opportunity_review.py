"""Adversarial opportunity/source/review contracts; no model or heavy runtime."""
import copy

from services import research_opportunities as opportunities
from ordinary_research_fixtures import analysis, document, VISION, CALIBRATION, NEED, COUNTER, EVALUATION


def discover(material, limit=8):
    return opportunities.discover_opportunities(material, material["research_topic_clusters"], limit)


def single_candidate():
    return discover(analysis([document("vision/a.md", VISION, "图像识别")]))[0][0]


def test_theme_cooccurrence_and_generic_conditions_do_not_license_transfer():
    method = document("a.md", "本文研究图像识别任务，采用多模态融合方法作为基线。实验在图像噪声条件下完成，代码已提供。", "图像方法")
    need = document("b.md", "研究目标是评估合同条款核对。现有样本条件尚未验证，可靠性和研究方法需要确认。", "合同条件")
    candidates, _ = discover(analysis([method, need], [("同名主题", ["a.md", "b.md"])]))
    assert "transfer" not in {c["opportunity_kind"] for c in candidates}


def test_transfer_names_method_task_and_target_evidence():
    method = document("calibration/b.md", CALIBRATION, "校准")
    target = document("noise/n.md", NEED, "目标条件")
    material = analysis([method, target], [("不确定性校准", [method["path"]]), ("需求", [target["path"]])])
    candidates, _ = discover(material)
    transfer = next(c for c in candidates if c["opportunity_kind"] == "transfer")
    assert "不确定性校准" in transfer["research_questions"][0]
    assert "低质量图像识别" in transfer["research_questions"][0]
    assert target["evidence"][0]["evidence_id"] in transfer["methods"][2]
    assert transfer["hypothesis_status"] == "unverified"


def test_direct_transfer_failure_withdraws_candidate_but_retains_other_plans():
    method, target, counter = (document("b.md", CALIBRATION, "校准"), document("n.md", NEED, "目标"), document("c.md", COUNTER, "反证"))
    material = analysis([method, target, counter], [("校准", ["b.md"]), ("目标", ["n.md"]), ("复核", ["c.md"])])
    candidates, audit = discover(material)
    assert candidates
    assert not any(c["opportunity_kind"] == "transfer" for c in candidates)
    withdrawn = [c for c in audit["rejected"] if c["quality_gate"]["status"] == "withdrawn"]
    assert withdrawn and counter["evidence"][0]["evidence_id"] in withdrawn[0]["counterevidence_ids"]


def test_boundary_recall_retains_source_method_task_when_gap_excerpt_omits_them():
    source, counter = document("calibration/b.md", CALIBRATION, "校准"), document("vision/c.md", COUNTER, "反证")
    before = next(c for c in discover(analysis([source], [("不确定性校准", [source["path"]])]))[0] if c["opportunity_kind"] == "boundary")
    material = analysis([source, counter], [("不确定性校准", [source["path"]]), ("噪声复核", [counter["path"]])])
    after = next(c for c in discover(material)[0] if c["opportunity_kind"] == "boundary" and c["source_paths"] == sorted([source["path"], counter["path"]]))
    assert counter["evidence"][0]["evidence_id"] in after["evidence_roles"]["counterevidence"]
    assert any(e["evidence_id"] == counter["evidence"][0]["evidence_id"] for e in after["evidence_chain"])
    assert after["confidence"] == "低" and after["score"] < before["score"]


def test_same_method_failure_in_another_explicit_task_is_not_counterevidence():
    source = document("calibration/b.md", CALIBRATION, "校准")
    failure = document("contract/c.md", "合同条款核对任务采用不确定性校准方法。研究目标是评估合同核对样本中的置信度；复核结果发现该方法未改善合同准确率，不适用于合同输入条件。", "合同反证")
    material = analysis([source, failure], [("不确定性校准", [source["path"]]), ("合同核对", [failure["path"]])])
    candidates, _ = discover(material)
    boundary = next(c for c in candidates if c["opportunity_kind"] == "boundary" and "不确定性校准" in c["title"])
    assert not boundary["counterevidence"]
    assert failure["evidence"][0]["evidence_id"] not in boundary["evidence_roles"]["counterevidence"]


def test_unrelated_same_theme_failure_does_not_reduce_boundary_support():
    source = document("a.md", VISION, "图像")
    failure = document("b.md", "合同审查发现签字页缺失，法律条款存在不一致，仲裁流程失败。", "合同")
    before = discover(analysis([source]))[0][0]
    after = next(c for c in discover(analysis([source, failure]))[0] if c["opportunity_kind"] == "boundary")
    assert not after["counterevidence"]
    assert after["score"] == before["score"]


def test_unavailable_resources_require_confirmation_first():
    text = "本文研究低质量图像识别，采用融合算法。实验仅在清晰输入条件完成，尚未验证噪声输入；源代码未提供，数据不可用。"
    candidates, _ = discover(analysis([document("a.md", text, "图像")]))
    assert candidates and all(c["quality_gate"]["resources"] == "confirmation_first" for c in candidates)
    assert all(not c["evidence_roles"]["resources"] for c in candidates)
    assert all("先确认" in c["methods"][1] for c in candidates)


def test_reduced_cost_is_not_a_failure_and_separate_limitation_does_not_hide_resources():
    text = "本文研究低质量图像识别，采用融合算法。结果表明成本下降。噪声输入尚未验证。数据可用，代码已在附件提供。"
    candidates, _ = discover(analysis([document("a.md", text, "图像")]))
    assert candidates and all(not c["counterevidence"] for c in candidates)
    assert all(c["quality_gate"]["resources"] == "material_described" for c in candidates)


def test_forged_archive_alias_cannot_disguise_outside_source():
    material = analysis([document("inside/a.md", VISION, "图像")])
    for item in (material["document_index"][0]["evidence"][0], material["research_topic_clusters"][0]["evidence_chain"][0]):
        item.update(source_path="outside/b.md", archive_source_path="inside/a.md")
    assert discover(material)[0] == []


def test_valid_archive_basename_alias_and_selected_member_stay_local():
    container = document("imports/papers.zip", VISION, "论文包")
    item = container["evidence"][0]
    item.update(source_path="papers.zip::paper.md", archive_source_path=container["path"], archive_member="paper.md")
    material = analysis([container], [("图像", ["imports/papers.zip::paper.md"])])
    material.update(selected_scope_only=True, selected_scope_paths=["imports/papers.zip::paper.md"], research_scope="imports/papers.zip::paper.md")
    candidates, _ = discover(material)
    assert candidates and all(len(c["evidence_chain"]) == 1 for c in candidates)


def test_conflicting_body_ids_fail_closed_and_outside_ids_cannot_poison_scope():
    source, foreign = document("a.md", VISION, "图像"), document("b.md", COUNTER, "反证")
    foreign["evidence"][0]["evidence_id"] = source["evidence"][0]["evidence_id"]
    material = analysis([source, foreign])
    assert discover(material)[0] == []
    material.update(selected_scope_only=True, selected_scope_paths=["a.md"])
    candidates, _ = discover(material)
    assert candidates and all(c["source_paths"] == ["a.md"] for c in candidates)


def test_invalid_traversal_scope_cannot_expand_to_full_package():
    material = analysis([document("a.md", VISION, "图像")])
    material["research_scope"] = "../outside"
    assert not discover(material)[0]


def test_local_relevance_and_claim_match_cannot_verify_new_facts(monkeypatch):
    candidate = single_candidate()
    monkeypatch.setattr(opportunities, "verify_claim_evidence", lambda *a, **k: {"support_status": "supported"})
    for fact in ("低质量图像识别具有显著鲁棒性", "先核验低质量图像识别，该方法能够改善噪声输入的准确率", "噪声输入下准确率为92%，建议复核"):
        patch, audit, valid = opportunities.review_model_proposal(candidate, {"rationale": fact, "evidence_ids": candidate["evidence_ids"]}, candidate["evidence_chain"])
        assert not valid and not patch
        assert audit["rejected"][0]["reason"] in {"unverified_factual_statement", "unverified_numbers"}


def test_candidate_id_changed_source_or_unseen_catalog_quote_rejected():
    candidate = single_candidate()
    proposal = {"rationale": "先核验低质量图像识别的噪声输入条件", "evidence_ids": candidate["evidence_ids"]}
    assert not opportunities.review_model_proposal(candidate, dict(proposal, candidate_id="RO-foreign"), candidate["evidence_chain"])[2]
    for replacement in ({"source_path": "foreign.md"}, {"text": "低质量图像识别已经实现鲁棒性提升，方法有效且可复现。"}):
        catalog = copy.deepcopy(candidate["evidence_chain"])
        catalog[0].update(replacement)
        assert not opportunities.review_model_proposal(candidate, proposal, catalog)[2]


def test_model_can_offer_a_local_action_and_direct_quoted_fact():
    candidate = single_candidate()
    proposal = {"rationale": "先核验低质量图像识别的噪声输入条件", "research_questions": ["如何复核低质量图像识别的可靠性？"],
                "methods": ["在清晰图像数据集上准确率为92%", "先比较低质量图像识别的原文条件"], "evidence_ids": candidate["evidence_ids"]}
    patch, audit, valid = opportunities.review_model_proposal(candidate, proposal, candidate["evidence_chain"])
    assert valid and len(patch["methods"]) == 2
    assert any(a["status"] == "direct_quote_and_inference_relevance" for a in audit["accepted"])


def test_facts_must_be_in_the_cited_subset_and_actual_visible_excerpt():
    candidate = single_candidate()
    shortened = [dict(candidate["evidence_chain"][0], text=VISION.split("。")[0] + "。")]
    proposal = {"rationale": "先核验低质量图像识别，样本数量为200", "evidence_ids": candidate["evidence_ids"]}
    assert not opportunities.review_model_proposal(candidate, proposal, shortened)[2]
    secondary = document("second.md", "合同复核的流程采用月度对照表，结果表明该表能够定位签字页差异。", "合同")["evidence"][0]
    candidate["evidence_chain"].append(secondary)
    candidate["evidence_ids"].append(secondary["evidence_id"])
    candidate["source_paths"].append(secondary["source_path"])
    proposal["rationale"] = "先核验低质量图像识别，合同复核的流程采用月度对照表"
    proposal["evidence_ids"] = [candidate["evidence_ids"][0]]
    assert not opportunities.review_model_proposal(candidate, proposal, candidate["evidence_chain"])[2]


def test_all_topics_inventoried_but_bm25_refinement_bounded(monkeypatch):
    docs = [document("topic/{:03d}.md".format(i), VISION + " 本文附带来源标识topic{:03d}。".format(i), "图像") for i in range(200)]
    for doc in docs:
        doc["source"]["doi"] = "https://doi.org/10.0000/same-paper"
    material = analysis(docs, [("主题{:03d}".format(i), [d["path"]]) for i, d in enumerate(docs)])
    calls = []
    original = opportunities.retrieve_evidence
    def measured(*args, **kwargs):
        calls.append(len(kwargs["indexed_chunks"]))
        assert kwargs["use_tfidf"] is False
        assert len(kwargs["token_cache"]) <= 160
        assert all(len(e["text"]) <= 900 and len(e.get("translated_text") or "") <= 900 for e in kwargs["indexed_chunks"])
        return original(*args, **kwargs)
    monkeypatch.setattr(opportunities, "retrieve_evidence", measured)
    candidates, audit = discover(material)
    assert len(audit["topics"]) == 200 and audit["coverage"] == "all_available_topics_scanned"
    assert len(calls) == audit["refined_count"] <= 24 and max(calls) == 160
    assert audit["deferred"] and not audit["refinement_complete"]
    assert all(c["retrieval_audit"]["bm25_queries"] == 1 for c in candidates)
    assert all(c["independent_source_count"] == 1 for c in candidates)
    assert all(any("等待精查" in warning for warning in c["limitations"]) for c in candidates)


def test_selection_reason_reports_actual_advantage_and_resource_tradeoff():
    left = {"title": "噪声图像边界", "opportunity_kind": "boundary", "research_gap": "尚未验证噪声输入可靠性", "score": 60,
            "score_breakdown": {"dimensions": {"gap_specificity": .9, "feasibility": .5}, "weights": {"gap_specificity": .25, "feasibility": .2}}}
    right = {"title": "合同条件复核", "opportunity_kind": "replication", "research_gap": "仅在已签合同条件完成", "score": 55,
             "score_breakdown": {"dimensions": {"gap_specificity": .5, "feasibility": .8}}}
    reason = opportunities._selection_reason(left, right)
    assert "噪声图像边界" in reason and "合同条件复核" in reason
    assert "尚未验证噪声输入可靠性" in reason and "仅在已签合同条件完成" in reason
    assert "缺口具体性评分较高（0.90 对 0.50）" in reason
    assert "可行性评分较低（0.50 对 0.80）" in reason
    assert "不代表专家已确认" in reason


def test_tied_selection_admits_no_proven_superiority_and_stable_order():
    left = {"title": "图像边界复核", "score": 55, "score_breakdown": {"dimensions": {"gap_specificity": .9, "feasibility": .5}}}
    right = {"title": "经营条件复核", "score": 55, "score_breakdown": {"dimensions": {"gap_specificity": .9, "feasibility": .5}}}
    reason = opportunities._selection_reason(left, right)
    assert "不能证明本项明显优于该备选" in reason
    assert "候选ID稳定顺序暂选" in reason
    assert "评分较高" not in reason


def test_single_output_still_compares_with_the_strongest_refined_alternative():
    vision, evaluation = document("vision/a.md", VISION, "图像"), document("archive/e.md", EVALUATION, "档案")
    material = analysis([vision, evaluation], [("图像边界", [vision["path"]]), ("档案评价", [evaluation["path"]])])
    full, _ = discover(material)
    one, _ = discover(material, limit=1)
    assert len(full) > 1 and len(one) == 1
    assert full[1]["title"] in one[0]["selection_reason"]
    assert "当前仅此候选" not in one[0]["selection_reason"]


def test_registered_evidence_ids_do_not_turn_prospective_questions_into_numeric_facts():
    candidate = single_candidate()
    evidence_id = candidate["evidence_ids"][0]
    for question in ("如何依据证据{}中所述条件复核低质量图像识别？".format(evidence_id),
                     "如何核验低质量图像识别的噪声条件[{}]？".format(evidence_id)):
        proposal = {"rationale": "先核验低质量图像识别的噪声输入条件", "research_questions": [question], "evidence_ids": [evidence_id]}
        patch, audit, valid = opportunities.review_model_proposal(candidate, proposal, candidate["evidence_chain"])
        assert valid and patch["research_questions"] == [question]
        assert not audit["rejected"]


def test_citation_stripping_preserves_quantities_real_ids_and_longer_tokens():
    candidate = single_candidate()
    evidence_id = candidate["evidence_ids"][0]
    for extra in ("92%", "200个样本", "CVE-2026-12345", evidence_id + "X"):
        proposal = {"rationale": "先核验低质量图像识别的噪声条件{}与证据{}".format(extra, evidence_id), "evidence_ids": [evidence_id]}
        _, audit, valid = opportunities.review_model_proposal(candidate, proposal, candidate["evidence_chain"])
        assert not valid and audit["rejected"][0]["reason"] == "unverified_numbers"
    assert opportunities._without_evidence_citations("CVE-2026-12345", {"CVE-2026-12345"}) == "CVE-2026-12345"
