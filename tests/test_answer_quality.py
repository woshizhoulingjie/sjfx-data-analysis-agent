import json
import unittest
from unittest.mock import patch

from config import Config
from services import answer_quality as aq
from services.conversation import CallableEvidenceRetriever, ContextWindowPolicy, ConversationEngine, _citation, _select_model_citations
from services.claim_verifier import ClaimVerifier
from services.conversation_package_context import model_digest, build_standard_context
from services.analysis_planner import AnalysisPlanner
from services.evidence_ledger import build_evidence_ledger
from services.turn_runtime import AnalysisTurnRuntime


def citation(index, text, **extra):
    return _citation({'evidence_id': f'e{index}', 'source_path': f'f{index}.txt',
                      'text': text, 'label': 'paragraph', 'evidence_quality': {'eligible': True}, **extra}, index)


class Model:
    def __init__(self, answer='项目因设备故障延期，影响交付。[1]'):
        self.calls = []
        self.answer = answer

    def chat(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return {'content': self.answer}


class AnswerQualityTests(unittest.TestCase):
    def test_late_clause_keeps_condition_and_exception(self):
        text = ('项目背景与行业发展。' * 180) + '甲项目付款条件为验收后十日。若存在质量争议，争议款项暂缓支付。'
        result = aq.extract_passage(text, '甲项目付款条件与质量争议', 220)
        self.assertIn('验收后十日', result)
        self.assertIn('暂缓支付', result)
        self.assertLessEqual(len(result), 220)

    def test_long_paragraph_without_punctuation_is_query_windowed(self):
        text = '背景' * 2000 + 'paymentDeadline=2030-05-11' + '附录' * 1000
        self.assertIn('2030-05-11', aq.extract_passage(text, 'paymentDeadline', 180))

    def test_large_candidate_does_not_block_later_fitting_candidate(self):
        rows = [citation(1, 'A' * 1500), citation(2, 'B' * 1400), citation(3, 'C' * 200)]
        selected = _select_model_citations(rows, ContextWindowPolicy(max_model_evidence_chars=2300))
        self.assertEqual([x['citation_index'] for x in selected], [1, 3])

    def test_single_file_can_supply_complementary_clauses(self):
        rows = [citation(i, text, source_path='contract.txt') for i, text in enumerate([
            '延期原因是设备故障。', '延期影响是交付顺延十日。', '下一步计划更换设备。'], 1)]
        selected = _select_model_citations(rows, ContextWindowPolicy(max_model_evidence_per_file=1), '延期原因、影响和下一步计划')
        self.assertEqual(len(selected), 3)

    def test_preview_is_not_selected_and_provenance_survives(self):
        row = citation(1, 'Alpha budget is 100. This is preliminary accounting data.', preview_only=True,
                       coverage={'level': 'preview'}, location={'source': 'preview'}, archive_source_path='a.zip')
        self.assertTrue(row['preview_only'])
        self.assertEqual(row['archive_source_path'], 'a.zip')
        self.assertEqual(_select_model_citations([row], ContextWindowPolicy()), [])

    def test_summary_identity_survives(self):
        row = citation(1, '甲项目因设备问题延期，后续拟更换设备。', location={'source': 'file_summary'})
        self.assertEqual(aq.source_kind(row), 'summary')

    def test_digest_selects_middle_file_for_current_question(self):
        files = [{'path': 'a.txt', 'summary': '日常工作总结。'}, {'path': 'm.txt', 'summary': '付款验收条件与质量争议处理。'}, {'path': 'z.txt', 'summary': '行业背景综述。'}]
        digest = json.loads(model_digest({'file_summaries': files}, 3000, '付款验收条件'))
        self.assertEqual(digest['representative_file_capsules'][0]['path'], 'm.txt')

    def test_digest_stays_valid_and_bounded(self):
        files = [{'path': f'directory-{i}/file.txt', 'summary': '甲项目付款条件。' * 50} for i in range(120)]
        digest = model_digest({'file_summaries': files, 'package_overview': '概况' * 4000}, 1500, '付款')
        self.assertLessEqual(len(digest), 1500)
        self.assertIsInstance(json.loads(digest), dict)

    def test_deep_summary_tail_is_available_before_question_selection(self):
        class Storage:
            def get_package_overview(self, _): return {}
            def get_analysis_overview(self, _): return {}
            def list_summaries_page(self, *args, **kwargs):
                return {'items': [{'path': 'a.txt', 'type': 'deep', 'payload': {'summary': '背景。' * 300 + '付款期限为验收后十日。'}}]}
            def inventory_paths_under(self, *args): return ['a.txt']
        result = build_standard_context(Storage(), 'scan')
        self.assertIn('验收后十日', result['file_summaries'][0]['summary'])

    def verify(self, answer, rows, **extra):
        return ClaimVerifier().verify({'answer': answer, 'citations': rows, **extra}, {'scope': {'kind': 'package'}, 'modes': ['retrieval']})

    def test_amount_from_other_entity_is_rejected(self):
        rows = [citation(1, 'Alpha budget is 100 and Beta budget is 200. This is the approved accounting record.')]
        result = self.verify('Alpha budget is 200. [1]', rows)
        self.assertNotEqual(result['status'], 'verified')
        self.assertEqual(result['quality_metrics']['numeric_failure_count'], 1)
        self.assertEqual(self.verify('Alpha budget is 100. [1]', rows)['status'], 'verified')

    def test_chinese_entity_amount_is_checked(self):
        rows = [citation(1, '甲项目预算为100万元，乙项目预算为200万元，均为本年度批复额度。')]
        self.assertNotEqual(self.verify('甲项目预算为200万元。[1]', rows)['status'], 'verified')
        self.assertEqual(self.verify('甲项目预算为100万元。[1]', rows)['status'], 'verified')
        self.assertEqual(self.verify('甲项目的预算为100万元。[1]', rows)['status'], 'verified')

    def test_attribute_and_year_cannot_be_swapped(self):
        self.assertFalse(aq.numeric_relation_supported('Alpha budget is 200', 'Alpha income is 200 and Beta budget is 200', {'200'}))
        self.assertFalse(aq.numeric_relation_supported('甲项目2026年预算为100万元', '甲项目2025年预算为100万元，甲项目2026年预算为200万元', {'100万'}))

    def test_date_paraphrase_is_accepted_but_reversed_dates_are_not(self):
        source = '交付日期由2026年9月20日调整为2026年9月30日。'
        self.assertTrue(aq.numeric_relation_supported('导致交付日期从2026年9月20日推迟至2026年9月30日', source, {'2026年', '9月', '20日', '30日'}))
        self.assertFalse(aq.numeric_relation_supported('交付日期由2026年9月30日调整为2026年9月20日', source, {'2026年', '9月', '20日', '30日'}))

    def test_preview_cannot_be_upgraded_by_lexical_or_numeric_overlap(self):
        rows = [citation(1, 'Alpha budget is 100 and Beta budget is 200. Approved accounting record.', preview_only=True)]
        self.assertNotEqual(self.verify('Alpha budget is 100. [1]', rows)['status'], 'verified')

    def test_verification_uses_only_the_sent_evidence(self):
        rows = [citation(1, 'Alpha budget is 100.'), citation(2, 'Beta budget is 200.')]
        result = self.verify('Beta budget is 200. [2]', rows, model_citations=[rows[0]])
        self.assertNotEqual(result['status'], 'verified')

    def test_post_sentence_citation_stays_with_chinese_claim(self):
        ledger = build_evidence_ledger('甲项目预算为100万元。[1]乙项目预算为200万元。[2]', [])
        self.assertEqual([x['citation_indexes'] for x in ledger['claims']], [[1], [2]])

    def test_unrelated_negative_sentence_does_not_reverse_a_supported_claim(self):
        rows = [citation(1, '付款条款：甲项目验收合格后十日内付款。若存在质量争议，争议部分暂缓支付，无争议部分仍应按期支付。合同没有记载负责人电话。')]
        review = self.verify('甲项目验收合格后十日内付款。[1]', rows)
        self.assertEqual(review['status'], 'verified')

    def test_markdown_headings_are_not_uncited_facts(self):
        rows = [citation(1, '甲项目预算为100万元，均为本年度批复额度。')]
        review = self.verify('**资金安排**\n甲项目预算为100万元。[1]', rows)
        self.assertEqual(review['status'], 'verified')

    def test_factual_paragraph_cannot_bypass_review_by_mentioning_next_steps(self):
        row = citation(1, '甲项目预算为100万元，均为本年度批复额度。')
        review = self.verify('甲项目预算为999万元，下一步计划更换设备。', [row])
        self.assertNotEqual(review['status'], 'verified')
        self.assertEqual(review['ledger']['unsupported_claim_count'], 1)

    def test_failed_draft_cannot_bypass_guard_with_one_valid_label(self):
        rows = [citation(1, 'Alpha budget is 100 and Beta budget is 200.')]
        turn = {'answer': 'Alpha budget is 200. [1]', 'citations': rows}
        verification = self.verify(turn['answer'], rows)
        guarded = ClaimVerifier().guard_result(turn, verification)
        self.assertNotIn('Alpha budget is 200', guarded['answer'])

    def test_repair_query_does_not_copy_invented_amount(self):
        runtime = object.__new__(AnalysisTurnRuntime)
        queries = runtime._repair_queries({'objective': '甲项目预算是多少？'}, {'ledger': {'claims': [{'status': 'unsupported', 'text': '甲项目预算为999万元'}]}})
        self.assertNotIn('999', ' '.join(queries))
        self.assertIn('甲项目', queries[0])

    def test_explicit_missing_aspect_is_sent_to_bounded_revision(self):
        row = citation(1, '甲项目延期原因是设备故障，影响是交付顺延，下一步计划更换设备并重新调试。')
        contract = aq.answer_contract('延期原因、影响和下一步计划是什么？', [row])
        result = self.verify('甲项目延期原因是设备故障。[1]', [row], input_manifest={'answer_contract': contract})
        self.assertIn('影响', result['missing_answer_aspects'])
        self.assertIn('下一步', result['missing_answer_aspects'])
        self.assertTrue(result['needs_revision'])

    def test_why_and_whether_are_document_questions(self):
        planner = AnalysisPlanner()
        for query in ['为什么项目延期了？', '是否已经付款？']:
            self.assertNotIn('general_qa', planner.plan(query, {'kind': 'package'})['modes'])

    def test_grounded_answer_bypasses_interactive_short_reply_clamp(self):
        model = Model()
        engine = ConversationEngine(CallableEvidenceRetriever(lambda request: {'results': [dict(evidence_id='e1', source_path='f.txt', text='项目因设备故障延期，影响交付。下一步计划更换设备。')]}), answer_model=model)
        with patch.object(Config, 'LLM_BACKEND', 'vllm'), patch.object(Config, 'VLLM_INTERACTIVE_MAX_TOKENS', 480):
            result = engine.ask(engine.new_session('scan'), '详细说明为什么项目延期、影响和下一步计划')
        self.assertGreater(model.calls[0][2]['max_tokens'], 480)
        self.assertNotIn('120 个汉字', model.calls[0][0])
        self.assertEqual(len(model.calls), 1)
        self.assertIn('下一步', model.calls[0][1])
        self.assertTrue(result['input_manifest']['actual_citation_indexes'])

    def test_prompt_budget_counts_all_serialized_content(self):
        model = Model('资料明确记载甲项目因设备故障延期。[1]')
        engine = ConversationEngine(CallableEvidenceRetriever(lambda _: {}), answer_model=model)
        rows = [citation(i, '甲项目因设备故障延期，需更换设备。' * 30) for i in range(1, 15)]
        manifest = {}
        with patch.object(Config, 'CONVERSATION_CONTEXT_TOKENS', 8192):
            engine._quality_grounded_answer('retrieval', '甲项目延期原因', '旧问题' * 5000, rows, {}, 14, manifest)
        self.assertLessEqual(manifest['prompt_tokens'] + manifest['output_token_budget'] + 512, 8192)
        self.assertEqual(manifest['selected_evidence'], len(rows))


if __name__ == '__main__':
    unittest.main()
