"""Reading labels remain reversible and conservative on real business footers."""
import unittest
from services.mail.foundation import fragments


class MCSReadingBoundariesTests(unittest.TestCase):
    def assert_preserved(self, text, rows):
        self.assertEqual(''.join(row['text'] for row in rows), text)
        offset = 0
        for row in rows:
            self.assertEqual(row['char_start'], offset)
            offset += len(row['text'])
            self.assertEqual(row['char_end'], offset)

    def test_original_reply_and_forward_are_distinct_with_nested_history(self):
        text = ('这次新增回复\r\n------------------ 转发邮件 ------------------\r\n'
                '发件人: another@example.com\r\n转发原文\r\n'
                '------------------ 原始邮件 ------------------\r\n更早的引用\r\n')
        rows = fragments(text)
        self.assertEqual([row['fragment_kind'] for row in rows], ['authored', 'forwarded', 'quoted'])
        self.assert_preserved(text, rows)

    def test_english_original_marker_is_quoted(self):
        text = 'new\n-----Original Message-----\nold\n'
        rows = fragments(text)
        self.assertEqual([row['fragment_kind'] for row in rows], ['authored', 'quoted'])
        self.assert_preserved(text, rows)

    def test_structured_chinese_signature_is_folded_before_quoted_history(self):
        text = ('请确认上述条件。\n\n谢谢！\n\n张伟 | 采购经理\n测试有限公司 采购部\n'
                '地址：苏州园区\n电话：0512-1234-5678\n邮箱：sender@example.com\n\n'
                '------------------ 原始邮件 ------------------\n别人的邮件\n')
        rows = fragments(text)
        self.assertEqual([row['fragment_kind'] for row in rows], ['authored', 'signature', 'quoted'])
        self.assertIn('请确认上述条件。', rows[0]['text'])
        self.assertIn('张伟 | 采购经理', rows[1]['text'])
        self.assert_preserved(text, rows)

    def test_greeting_does_not_hide_a_business_paragraph(self):
        text = '顺祝商祺。\n但仍需收到保函后才能确认，请回复。\n'
        rows = fragments(text)
        self.assertEqual([row['fragment_kind'] for row in rows], ['authored'])
        self.assert_preserved(text, rows)

    def test_contacts_followed_by_new_substantive_text_are_not_a_signature(self):
        text = ('负责单位 | 联系方式\n测试有限公司\n电话：0512-1234-5678\n'
                '邮箱：buyer@example.com\n请注意该单位尚未确定，仍需等待审批。\n')
        rows = fragments(text)
        self.assertEqual([row['fragment_kind'] for row in rows], ['authored'])
        self.assert_preserved(text, rows)

    def test_incomplete_contact_block_does_not_invent_a_signature(self):
        text = '新的邮件正文\n李娜 | 销售经理\n测试有限公司\n'
        rows = fragments(text)
        self.assertEqual([row['fragment_kind'] for row in rows], ['authored'])
        self.assert_preserved(text, rows)

    def test_original_marker_inside_paragraph_is_not_a_boundary(self):
        text = '请在“原始邮件”中核对附件，不要仅凭主题判断。\n'
        rows = fragments(text)
        self.assertEqual([row['fragment_kind'] for row in rows], ['authored'])
        self.assert_preserved(text, rows)

    def test_inline_reply_uncertainty_preserves_all_characters(self):
        text = '我的回复\nOn Monday, Alice wrote:\n> 原话\n我的内联补充\n'
        rows = fragments(text)
        self.assertEqual(rows[-1]['fragment_kind'], 'uncertain')
        self.assert_preserved(text, rows)


if __name__ == '__main__':
    unittest.main()
