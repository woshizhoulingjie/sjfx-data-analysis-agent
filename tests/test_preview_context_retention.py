"""Short source windows must not lose facts just to fill a sentence quota."""
import copy
import tempfile
import unittest
from pathlib import Path

from services import large_package_evidence as evidence
from services.large_package_preview import preview_file


class PreviewContextRetentionTests(unittest.TestCase):
    def test_record_preserves_short_context_in_source_order(self):
        text = ('设备编号 R-48，责任人陈青。\n计划到货19台，实际收到13台，尚缺6台。\n'
                '已收设备仍需联调。最终验收没有签字。付款条件尚未满足。'
                '后续需补齐6台设备。文件未规定罚款金额。')
        parts = []
        evidence.record(parts, '字节 0 附近完整行', text, source_window=0)
        self.assertEqual('\n'.join(p['text'] for p in parts), text)
        self.assertEqual(parts[0]['source_window'], 0)
        self.assertTrue(parts[0]['preview_only'])
        self.assertFalse(parts[0]['truncated'])
        self.assertTrue(parts[0]['id'])

    def test_compact_text_does_not_drop_ninth_short_fact(self):
        parts = [dict(text='Record %s: measured quantity %s units.' % (i, i+3),
                      location='row %s' % i, kind='record') for i in range(12)]
        before = copy.deepcopy(parts)
        self.assertEqual(evidence.compact_text(parts, chars=1800),
                         '\n'.join(p['text'] for p in parts))
        self.assertEqual(parts, before)

    def test_compact_text_keeps_only_source_content(self):
        parts = [dict(text='secret-name.docx', kind='attachment'),
                 dict(text='member-name.txt', kind='member_names'),
                 dict(text='Actual source content.', kind='text')]
        self.assertEqual(evidence.compact_text(parts), 'Actual source content.')

    def test_long_windows_still_use_bounded_selection(self):
        text = '\n'.join('Section %s: measured %s units; confirmation is pending.' % (i, i*7)
                         for i in range(250))
        parts = []
        evidence.record(parts, 'bounded window', text)
        self.assertLessEqual(sum(len(p['text']) for p in parts), 1800)
        self.assertTrue(all(p['truncated'] for p in parts))
        self.assertLessEqual(len(evidence.compact_text(parts, chars=190)), 190)

    def test_short_parser_fragment_does_not_claim_complete_source(self):
        parts = []
        evidence.record(parts, 'sampled page', 'Subject R-48; approval is pending.',
                        truncated=True)
        self.assertTrue(all(p['truncated'] for p in parts))

    def test_plain_text_preview_retains_subject_and_actual_quantity(self):
        text = ('采购项目北辰，合同编号 BC-409。\n计划交付19台设备，实收13台，缺少6台。\n'
                '设备已入临时库。联调没有完成。负责人陈青尚未签字。'
                '验收不完整，尾款不能支付。供应商提出补交计划。未约定罚款金额。')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'record.txt'
            path.write_text(text, encoding='utf-8')
            summary, digest = preview_file(Path(tmp), {'path': path.name})
        self.assertEqual(summary['sample'], text)
        self.assertFalse(summary['complete'])
        self.assertTrue(summary['coverage']['preview_only'])
        self.assertIsNone(digest)

    def test_email_preview_retains_request_and_pending_confirmation(self):
        body = ('设备 R-48 的维修拟改到8月9日，尚未取得客户同意。\n'
                '本邮件不是双方已确认的计划。设备仍处于停机状态。'
                '请确认下一步安排。费用未确定。安全检查尚未完成。'
                '原审批要求继续有效。不得按已修复登记。')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'request.eml'
            path.write_text('Subject: Repair request\nFrom: sender@example.invalid\n'
                            'To: receiver@example.invalid\nContent-Type: text/plain; charset=utf-8\n\n'+body,
                            encoding='utf-8')
            summary, _ = preview_file(Path(tmp), {'path': path.name})
        for fact in ('R-48', '8月9日', '尚未取得客户同意', '不得按已修复登记'):
            self.assertIn(fact, summary['sample'])


if __name__ == '__main__':
    unittest.main()
