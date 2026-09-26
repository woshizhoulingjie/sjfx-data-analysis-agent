import unittest
from services import answer_quality as aq
from services.claim_verifier import ClaimVerifier, _numbers
from services.conversation import _citation


class NumericContextTests(unittest.TestCase):
    def review(self, claim, source):
        row=_citation({'evidence_id':'record-1','source_path':'receipt.txt','text':source,
            'label':'paragraph','evidence_quality':{'eligible':True},
            'location':{'source':'file_summary'}},1)
        return ClaimVerifier().verify({'answer':claim+'[1]', 'citations':[row]},
            {'scope':{'kind':'package'},'modes':['retrieval']})

    def test_grouped_amount_and_sign_are_not_split(self):
        self.assertEqual(_numbers('利润-153,240.50元，预算2,000,000元'),
            {'-153240.50元','2000000元'})
        self.assertEqual(self.review('预算为153,000元。','预算为153000元。')['status'],'verified')
        self.assertEqual(self.review('预算为153000元。','预算为153,000元。')['status'],'verified')

    def test_formatted_values_cannot_change_field_or_sign(self):
        self.assertNotEqual(self.review('利润153,000元。','利润-153000元。')['status'],'verified')
        self.assertNotEqual(self.review('预算153,000元。','预算200000元，支出153000元。')['status'],'verified')
        self.assertNotEqual(self.review('预算153,00元。','预算15300元。')['status'],'verified')

    def test_quantity_unit_is_part_of_value(self):
        self.assertNotEqual(self.review('实收11台。','实收11箱。')['status'],'verified')
        self.assertNotEqual(self.review('重量11千克。','重量11吨。')['status'],'verified')
        self.assertEqual(self.review('实收11台。','实收11台。')['status'],'verified')

    def test_record_context_supports_its_own_quantity(self):
        source='昆仑项目NZ-2031-044到货记录，记录时间2031年9月4日。计划交付Q-23水泵14台，实收11台，尚缺3台。'
        claim='昆仑项目（合同编号NZ-2031-044）计划交付Q-23水泵14台，截至2031年9月4日实收11台，尚缺3台。'
        self.assertEqual(self.review(claim,source)['status'],'verified')
        self.assertEqual(self.review('昆仑项目目前实收11台。',source)['status'],'verified')

    def test_record_context_cannot_cross_projects(self):
        source='甲项目到货记录，记录时间2031年9月4日。实收11台。乙项目到货记录，记录时间2031年9月5日。实收8台。'
        self.assertNotEqual(self.review('乙项目实收11台。',source)['status'],'verified')
        self.assertNotEqual(self.review('甲项目实收8台。',source)['status'],'verified')
        self.assertEqual(self.review('乙项目实收8台。',source)['status'],'verified')

    def test_record_context_cannot_cross_dates(self):
        source='甲项目到货记录，记录时间2031年9月4日。实收11台。记录时间2031年9月5日。实收8台。'
        self.assertNotEqual(self.review('截至2031年9月4日实收8台。',source)['status'],'verified')
        self.assertNotEqual(self.review('截至2031年9月5日实收11台。',source)['status'],'verified')
        self.assertEqual(self.review('截至2031年9月5日实收8台。',source)['status'],'verified')

    def test_unrelated_reference_does_not_establish_record_subject(self):
        self.assertNotEqual(self.review('甲项目实收11台。',
            '本记录与甲项目无关。实收11台。')['status'],'verified')

    def test_record_context_expires_and_does_not_cross_lines(self):
        self.assertNotEqual(self.review('甲项目实收11台。',
            '甲项目到货记录。'+'后续情况尚待检查。'*60+'实收11台。')['status'],'verified')
        self.assertNotEqual(self.review('甲项目实收11台。',
            '甲项目到货记录。'+chr(10)+'乙项目到货记录。实收11台。')['status'],'verified')

    def test_source_record_keeps_role_and_polarity(self):
        source='甲项目到货记录。实收11台，尚缺3台。'
        self.assertNotEqual(self.review('甲项目实收3台。',source)['status'],'verified')
        self.assertNotEqual(self.review('甲项目尚缺11台。',source)['status'],'verified')
        self.assertNotEqual(self.review('甲项目实收11台。','甲项目到货记录。尚未实收11台。')['status'],'verified')

    def test_other_explicit_subject_cannot_inherit_record_identity(self):
        self.assertNotEqual(self.review('甲项目实收11台。',
            '甲项目到货记录。乙单位实收11台。')['status'],'verified')

    def test_planned_or_conditional_quantity_is_not_actual(self):
        for source in ('甲项目计划实收11台。',
                       '甲项目到货记录。若能按计划完成，实收11台。',
                       '如果甲项目实收11台，则可以验收。'):
            with self.subTest(source=source):
                self.assertNotEqual(self.review('甲项目实收11台。',source)['status'],'verified')

    def test_delivery_deadline_is_not_a_receipt_record_date(self):
        self.assertNotEqual(self.review('截至2031年9月4日实收11台。',
            '甲项目到货记录。合同交付期限2031年9月4日。实收11台。')['status'],'verified')

    def test_number_unit_spacing_does_not_merge_separate_values(self):
        self.assertEqual(self.review('实收11台。','实收11 台。')['status'],'verified')
        self.assertNotEqual(self.review('预算12000元。','预算12 000元。')['status'],'verified')

if __name__=='__main__': unittest.main()
