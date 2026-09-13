import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from services.legacy_office import extract_legacy
from services.unified_parser import UnifiedDocumentParser, DocumentParseError
from services.large_package_preview import preview_file
from services.package_exploration import preview_file as ordinary_preview
from services.package_exploration import PreviewBudget


def rtf(value):
    encoded = ''.join('\\u{}?'.format(ord(c) if ord(c)<32768 else ord(c)-65536) if ord(c)>127 else c for c in value)
    return ('{\\rtf1\\ansi\\ansicpg1252\\uc1 '+encoded+'}').encode('ascii')


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_rtf_chinese_and_rtf_disguised_as_doc_preserve_source_evidence(self):
        for ext in ('.rtf','.doc'):
            path=self.root/('原始'+ext);path.write_bytes(rtf('项目进度与交付，测试编号 AB12。'))
            result=UnifiedDocumentParser().parse(path,mode='auto',relative_path='folder/'+path.name)
            self.assertIn('项目进度与交付',result['text'])
            self.assertEqual(result['source']['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(result['evidence'][0]['source_path'],'folder/'+path.name)
            # 正文已完整提取，所以 complete 为 True；旧格式原生读取器从不 OCR
            # 嵌入图片/对象/批注，这属于已知能力边界，单独记录在
            # embedded_objects_covered，不能因此把文件判成解析失败。
            self.assertTrue(result['coverage']['complete'])
            self.assertFalse(result['coverage']['embedded_objects_covered'])
            self.assertFalse(result['coverage']['truncated_by_limit'])
            self.assertEqual(result['parser']['name'],'striprtf')
    def test_legacy_truncated_body_is_still_incomplete(self):
        """正文被预算截断时仍必须是 incomplete，不能与能力边界混为一谈。"""
        path=self.root/'长文.doc';path.write_bytes(rtf('网络安全评估结论。'*40))
        result=UnifiedDocumentParser(max_chars=200).parse(path,mode='auto')
        self.assertTrue(result['text'])
        self.assertTrue(result['coverage']['truncated_by_limit'])
        self.assertFalse(result['coverage']['complete'])
        self.assertFalse(result['coverage']['embedded_objects_covered'])
    def test_parsed_legacy_flows_into_existing_model_summary(self):
        from services.document_analysis import analyze_document_preview
        path=self.root/'summary.rtf';path.write_bytes(rtf('项目计划要求在九月完成设备验收。'))
        doc=UnifiedDocumentParser().parse(path,mode='auto')
        class Model:
            def chat_json(self, system, user, **kwargs):
                assert '九月完成设备验收' in user
                return {'json':{'title':'设备验收计划','core_summary':'项目计划要求在九月完成设备验收。','key_facts':['九月验收'],'structure_overview':{'sections':['验收计划'],'document_type':'计划'}},'model':'fixture','usage':{}}
        summary,_=analyze_document_preview(Model(),path,path.name,unified_document=doc)
        self.assertEqual(summary['summary_type'],'file')
        self.assertIn('九月',summary['summary'])
        self.assertEqual(summary['parser_info']['parser'],'striprtf')
        self.assertTrue(doc['evidence'])
        self.assertEqual(summary['node_path'],path.name)
    def test_real_binary_doc_ppt_and_chinese_ppt(self):
        fixtures=Path(__file__).parent/'legacy_fixtures'
        if not fixtures.is_dir():self.skipTest('optional Apache POI fixture files')
        for name,expected in [('sample.doc','This is a simple file'),('sample.ppt','This is a test title'),('chinese.ppt','')]:
            source=fixtures/name
            if not source.exists():continue
            path=self.root/name;path.write_bytes(source.read_bytes())
            self.assertEqual(path.read_bytes()[:8],bytes.fromhex('d0cf11e0a1b11ae1'))
            doc=UnifiedDocumentParser().parse(path,mode='auto')
            self.assertTrue(doc['text']);self.assertTrue(doc['evidence'])
            if expected:self.assertIn(expected,doc['text'])
            if name=='chinese.ppt':
                self.assertIn('複数の文字',doc['text'])
                self.assertIn('表Mixパﾋﾟ𠮟',doc['text'])
                self.assertNotIn('Office テーマ',doc['text'])
                self.assertTrue(doc['parser']['native_metadata']['current_revision_only'])
            preview,_=preview_file(self.root,{'path':name})
            self.assertTrue(preview['sample']);self.assertNotEqual(preview['portrait']['recognition_status'],'unsupported')
    def test_ppt_rejects_cyclic_revision_chain_without_using_stale_content(self):
        source=Path(__file__).parent/'legacy_fixtures/sample.ppt'
        if not source.exists():self.skipTest('optional Apache POI fixture')
        import olefile,struct
        path=self.root/'cycle.ppt';path.write_bytes(source.read_bytes())
        with olefile.OleFileIO(path,write_mode=True) as ole:
            current=ole.openstream('Current User').read()
            offset=struct.unpack_from('<I',current,16)[0]
            data=bytearray(ole.openstream('PowerPoint Document').read())
            struct.pack_into('<I',data,offset+16,offset)
            ole.write_stream('PowerPoint Document',bytes(data))
        with self.assertRaises(DocumentParseError):UnifiedDocumentParser().parse(path,mode='auto')
    def test_xls_reads_multiple_sheets_and_dates(self):
        import xlwt
        from datetime import datetime
        path=self.root/'sheets.xls';book=xlwt.Workbook()
        for name,value in [('一月',10),('二月',20)]:
            sheet=book.add_sheet(name);sheet.write(0,0,'销售额');sheet.write(1,0,value)
        book.get_sheet(0).write(1,1,datetime(2026,1,2),xlwt.easyxf(num_format_str='YYYY-MM-DD'))
        book.save(str(path))
        doc=UnifiedDocumentParser().parse(path,mode='auto')
        for text in ('一月','二月','销售额','10','20','2026-01-02'):
            self.assertIn(text,doc['text'])
        self.assertEqual(doc['structure']['table_count'],2)
        result,_=preview_file(self.root,{'path':path.name})
        self.assertIn('销售额',result['portrait']['fields'])
        self.assertEqual(result['source_hash_kind'],'pending')
        self.assertFalse(result['complete'])
        self.assertIn('工作表',result['portrait']['evidence'][0]['location'])
    def test_archive_members_use_same_parser_and_keep_member_paths(self):
        path=self.root/'bundle.zip'
        with zipfile.ZipFile(path,'w') as z:z.writestr('nested/info.rtf',rtf('采购合同的交付计划与验收安排。'))
        result=UnifiedDocumentParser().parse(path,mode='auto')
        self.assertIn('采购合同',result['text'])
        self.assertTrue(any('info.rtf' in e['source_path'] for e in result['evidence']))
    def test_corrupt_legacy_is_a_parse_failure_not_empty_success(self):
        for ext in ('.doc','.ppt','.xls','.rtf'):
            path=self.root/('bad'+ext);path.write_bytes(b'not a document')
            with self.assertRaises(DocumentParseError):UnifiedDocumentParser().parse(path,mode='auto')
    def test_giant_ole_preview_rejects_before_reader_or_hash(self):
        path=self.root/'large.ppt'
        with path.open('wb') as f:f.seek(2*1024**3);f.write(b'x')
        with patch('services.legacy_office.subprocess.Popen',side_effect=AssertionError('must not start')):
            result,_=preview_file(self.root,{'path':path.name})
        self.assertEqual(result['sample'],'')
        self.assertNotEqual(result['portrait']['recognition_status'],'unsupported')
        self.assertIn('预算',result['notice'])
        self.assertEqual(result['source_hash_kind'],'pending')
    def test_rtf_character_budget_remains_partial(self):
        path=self.root/'long.rtf';path.write_bytes(rtf('销售回款与订单明细。'*500))
        result=extract_legacy(path,max_chars=100)
        self.assertTrue(result['truncated']);self.assertLessEqual(len(result['text']),100)
    def test_existing_preview_route_receives_plaintext_and_respects_budget(self):
        path=self.root/'input.rtf';path.write_bytes(rtf('设备故障和维护检修记录。'*10))
        result=ordinary_preview(self.root,{'path':path.name,'size':path.stat().st_size},hash_source=False)
        self.assertIn('设备故障',result['preview_text'])
        self.assertNotIn('\\rtf',result['preview_text'])
        result=ordinary_preview(self.root,{'path':path.name,'size':path.stat().st_size},budget=PreviewBudget(8),hash_source=False)
        self.assertEqual(result['status'],'deferred');self.assertEqual(result['preview_text'],'')


if __name__=='__main__':unittest.main()
