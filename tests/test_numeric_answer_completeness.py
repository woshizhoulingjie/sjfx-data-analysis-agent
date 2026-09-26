import unittest
from services import answer_quality as aq
from services.claim_verifier import ClaimVerifier
from services.conversation import _citation


def citation(text):
    return _citation({'evidence_id':'e1','source_path':'record.txt','text':text,
        'label':'paragraph','evidence_quality':{'eligible':True},
        'location':{'source':'file_summary'}},1)


class NumericAnswerCompletenessTests(unittest.TestCase):
    def review(self, answer, evidence, query=''):
        rows=[citation(evidence)]
        return ClaimVerifier().verify({'answer':answer,'citations':rows,
            'input_manifest':{'answer_contract':aq.answer_contract(query,rows)}},
            {'scope':{'kind':'package'},'modes':['retrieval']})

    def test_small_number_is_not_attributed_to_larger_number_clause(self):
        text='计划交付12台，实收2台。'
        self.assertTrue(aq.numeric_relation_supported(text,text,{'12','2'}))
        self.assertEqual(self.review(text+'[1]',text)['status'],'verified')

    def test_same_number_must_match_every_claimed_field(self):
        self.assertFalse(aq.numeric_relation_supported('预算100元，支出100元。',
            '预算100元，支出80元。',{'100元'}))

    def test_sentence_period_is_not_a_decimal_continuation(self):
        self.assertTrue(aq.numeric_relation_supported('Alpha budget is 100.',
            'Alpha budget is 100.', {'100'}))
        self.assertFalse(aq.numeric_relation_supported('Alpha budget is 100.',
            'Alpha budget is 100.25.', {'100'}))
        self.assertFalse(aq.numeric_relation_supported('Alpha budget is 100.25.',
            'Alpha budget is 100.', {'100.25'}))
        self.assertTrue(aq.numeric_relation_supported('Alpha budget is 100.25.',
            'Alpha budget is 100.25.', {'100.25'}))

    def test_same_number_cannot_cross_project_boundaries(self):
        self.assertFalse(aq.numeric_relation_supported('Alpha budget is 100 and Beta budget is 100',
            'Alpha budget is 100 and Beta budget is 200',{'100'}))

    def test_explicit_header_unit_is_bound_to_its_value(self):
        result=self.review('未付尾款为153000元。[1]',
            '合同总额元=220000 | 已付预付款元=67000 | 未付尾款元=153000 | 验收状态=未完成')
        self.assertEqual(result['quality_metrics']['numeric_failure_count'],0)
        self.assertEqual(result['status'],'verified')

    def test_parenthesized_header_unit_is_bound_to_its_value(self):
        self.assertEqual(self.review('预算为153000元。[1]','预算（元）=153000')['status'],'verified')

    def test_unit_is_not_borrowed_from_another_column(self):
        self.assertNotEqual(self.review('未付尾款为67000元。[1]',
            '已付预付款元=67000 | 未付尾款元=153000')['status'],'verified')
        self.assertNotEqual(self.review('预算为153000元。[1]',
            '金额单位=美元 | 预算=153000 | 人数=5')['status'],'verified')

    def test_bare_unitless_amount_does_not_gain_currency(self):
        self.assertNotEqual(self.review('预算为153000元。[1]','预算=153000')['status'],'verified')

    def test_unit_magnitude_cannot_change(self):
        self.assertNotEqual(self.review('预算为153000元。[1]','预算万元=153000')['status'],'verified')
        self.assertNotEqual(self.review('预算为153000万元。[1]','预算元=153000')['status'],'verified')

    def test_sign_cannot_change(self):
        self.assertNotEqual(self.review('利润为13元。[1]','利润为-13元。')['status'],'verified')
        self.assertEqual(self.review('利润为-13元。[1]','利润为-13元。')['status'],'verified')

    def test_quantity_label_is_presentation_not_a_new_field(self):
        self.assertEqual(self.review('实收数量为13台，尚缺数量为6台。[1]',
            '实收13台，尚缺6台。')['status'],'verified')
        self.assertNotEqual(self.review('实收数量为6台，尚缺数量为13台。[1]',
            '实收13台，尚缺6台。')['status'],'verified')

    def test_role_polarity_cannot_change(self):
        self.assertNotEqual(self.review('实收13台。[1]','尚未实收13台。')['status'],'verified')
        self.assertNotEqual(self.review('已付尾款为153000元。[1]','未付尾款元=153000')['status'],'verified')

    def test_multi_question_contract_tracks_received_and_missing(self):
        query='目前实收到货多少台、还缺多少台？尾款是否已经满足支付条件？'
        rows=[citation('实收13台，尚缺6台，付款条件未满足。')]
        contract=aq.answer_contract(query,rows)
        required={r['aspect'] for r in contract['required_aspects']}
        self.assertIn('实收数量',required)
        self.assertIn('缺少数量',required)
        missing=aq.missing_answer_aspects('关于到货情况：设备未验收。付款条件未满足。',contract)
        self.assertIn('实收数量',missing)
        self.assertIn('缺少数量',missing)
        self.assertEqual(aq.missing_answer_aspects('实收13台，尚缺6台，付款条件未满足。',contract),[])

    def test_missing_quantity_prevents_verified_complete_status(self):
        result=self.review('设备未验收。付款条件未满足。[1]',
            '实收13台，尚缺6台。设备未验收。付款条件未满足。',
            '实收到货多少台、还缺多少台？付款条件满足了吗？')
        self.assertNotEqual(result['status'],'verified')
        self.assertIn('实收数量',result['missing_answer_aspects'])

    def test_unknown_quantity_does_not_force_a_fabricated_value(self):
        rows=[citation('设备未验收。')]
        contract=aq.answer_contract('实收到货多少台、还缺多少台？',rows)
        self.assertTrue(all(not r['candidate_citations'] for r in contract['required_aspects']))

    def test_rejected_draft_quantities_remain_missing_for_repair(self):
        result=self.review('实收数量为6台。[1] 尚缺数量为13台。[1]',
            '实收13台，尚缺6台。', '实收多少台、还缺多少台？')
        self.assertIn('实收数量', result['missing_answer_aspects'])
        self.assertIn('缺少数量', result['missing_answer_aspects'])
        self.assertTrue(result['needs_revision'])

    def test_independently_cited_source_quantities_survive_guard(self):
        result=self.review('实收13台。[1] 尚缺6台。[1]',
            '计划交付19台，实收13台，尚缺6台。', '实收多少台、还缺多少台？')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['missing_answer_aspects'], [])

    def test_legacy_subject_date_and_attribute_protection(self):
        for claim,source,numbers in [
            ('甲项目预算为200万元','甲项目预算为100万元，乙项目预算为200万元',{'200万'}),
            ('甲项目2026年预算为100万元','甲项目2025年预算为100万元，甲项目2026年预算为200万元',{'100万'}),
            ('Alpha budget is 200','Alpha income is 200 and Beta budget is 200',{'200'}),
            ('交付日期由2026年9月30日调整为2026年9月20日','交付日期由2026年9月20日调整为2026年9月30日',{'2026年','9月','20日','30日'}),
        ]:
            with self.subTest(claim=claim):
                self.assertFalse(aq.numeric_relation_supported(claim,source,numbers))


if __name__=='__main__': unittest.main()
