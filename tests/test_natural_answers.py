import unittest

from services.claim_verifier import ClaimVerifier
from services.conversation import _citation, IntentRouter
from services.analysis_planner import AnalysisPlanner
from services.answer_quality import numeric_relation_supported
from services.evidence_ledger import build_evidence_ledger


class NaturalAnswerTests(unittest.TestCase):
    def test_content_discoveries_are_not_a_file_listing(self):
        question = '这些资料中，光伏研究有哪些发现？请详细解释，并说明土地利用比例对应的范围。'
        self.assertNotEqual(IntentRouter().route(question).name, 'file_search')
        self.assertNotIn('file_search', AnalysisPlanner().plan(question, {'kind': 'package'})['modes'])
        self.assertEqual(IntentRouter().route('相关资料有哪些？').name, 'file_search')
        self.assertIn('file_search', AnalysisPlanner().plan('相关资料有哪些？', {'kind': 'package'})['modes'])

    def test_percentage_after_chinese_comma_keeps_its_local_object(self):
        claim = '针对美国大型地面太阳能项目（LSS）的分析显示，63.1%的项目位于农业用地，且倾向于选择高质量农田。'
        evidence = '本研究分析美国大型地面太阳能项目（LSS）的土地利用特征。在研究样本中，63.1%的项目位于农业用地，并倾向选择高质量农田。'
        self.assertTrue(numeric_relation_supported(claim, evidence, {'63.1%'}))
        self.assertFalse(numeric_relation_supported('63.1%的电池通过安全检测。', evidence, {'63.1%'}))
        self.assertFalse(numeric_relation_supported('甲项目预算为200万元', '甲项目预算为100万元，乙项目预算为200万元', {'200万'}))

    def guard(self, answer, statuses):
        ledger = build_evidence_ledger(answer, [])
        factual = [x for x in ledger['claims'] if x['status'] != 'section_label']
        self.assertEqual(len(factual), len(statuses))
        for claim, status in zip(factual, statuses):
            claim['status'] = status
        return ClaimVerifier().guard_result({'answer': answer}, {'ledger': ledger, 'status': 'partial'})

    def test_filter_preserves_theme_headings_and_multi_sentence_paragraphs(self):
        answer = '## 光伏\n材料研究关注组件老化。[1]封装脱层会影响组件性能。[1]\n\n## 储能\n电池研究关注状态监测。[2]不存在的公司已经破产。[2]'
        result = self.guard(answer, ['supported', 'supported', 'supported', 'unsupported'])
        self.assertIn('## 光伏\n\n材料研究', result['answer'])
        self.assertIn('。[1]', answer)
        self.assertNotIn('。\n\n封装', result['answer'])
        self.assertIn('## 储能', result['answer'])
        self.assertNotIn('已经破产', result['answer'])
        self.assertIn('[1]', result['answer'])
        self.assertIn('[2]', result['answer'])

    def test_partial_caveat_is_once_and_status_is_not_upgraded(self):
        result = self.guard('光伏研究关注材料稳定性。电池研究关注状态监测。', ['partially_supported', 'partially_supported'])
        self.assertNotIn('部分证据支持：', result['answer'])
        self.assertEqual(result['answer'].count('需结合原文进一步确认'), 1)
        self.assertEqual(result['evidence_status'], 'partial')

    def test_orphan_heading_is_removed_but_next_heading_survives(self):
        result = self.guard('## 无依据主题\n该公司已经倒闭。\n\n## 付款\n验收合格后支付款项。', ['unsupported', 'supported'])
        self.assertNotIn('无依据主题', result['answer'])
        self.assertIn('## 付款', result['answer'])

    def test_post_sentence_citations_do_not_consume_paragraph_boundaries(self):
        ledger = build_evidence_ledger('组件研究关注老化。[1]\n\n电池研究关注监测。[2]', [])
        first, second = ledger['claims']
        self.assertNotEqual(first['paragraph_index'], second['paragraph_index'])
        self.assertEqual(first['citation_indexes'], [1])
        self.assertEqual(second['citation_indexes'], [2])

    def test_attributed_references_are_retained_in_guarded_answer(self):
        ledger = build_evidence_ledger('组件研究关注老化。', [])
        ledger['claims'][0].update(status='supported', citation_indexes=[2, 4])
        result = ClaimVerifier().guard_result({}, {'ledger': ledger})
        self.assertIn('[2] [4]', result['answer'])

    def test_bad_amount_is_still_removed_and_other_detail_retained(self):
        rows = [_citation({'evidence_id': 'e1', 'source_path': 'contract.txt', 'text': '甲项目预算为100万元，乙项目预算为200万元。甲项目验收合格后十日内付款。'}, 1)]
        turn = {'answer': '## 合同\n甲项目预算为999万元。[1]甲项目验收合格后十日内付款。[1]', 'citations': rows}
        verifier = ClaimVerifier()
        review = verifier.verify(turn, {'scope': {'kind': 'package'}, 'modes': ['retrieval']})
        result = verifier.guard_result(turn, review)
        self.assertNotIn('999', result['answer'])
        self.assertIn('十日内付款', result['answer'])
        final = verifier.verify(result, {'scope': {'kind': 'package'}, 'modes': ['retrieval']})
        self.assertEqual(final['quality_metrics']['unsupported_claim_count'], 0)

    def test_advice_keeps_its_own_natural_qualification(self):
        result = self.guard('建议先核对设备到场时间，再更新交付安排。', ['analysis_or_advice'])
        self.assertTrue(result['answer'].startswith('建议先核对'))
        self.assertNotIn('分析判断：', result['answer'])


if __name__ == '__main__':
    unittest.main()
