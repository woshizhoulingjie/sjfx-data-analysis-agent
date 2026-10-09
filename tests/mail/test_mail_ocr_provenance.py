"""OCR quality provenance and repeat extraction without running any OCR model."""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.mail.models import MailMember
from services.mail.ocr import _rapid_result,extract_result
from services.mail.parsers.eml import parse_eml
from services.mail.repository import MailRepository
from services.mail.research.evidence_packing import model_source
from services.mail.text_provenance import OCR_NOTICE,VERSION,decode_processing_warnings


class SQLiteStore:
    def __init__(self,path):self.path=path
    @contextmanager
    def _connect(self):
        connection=sqlite3.connect(str(self.path));connection.row_factory=sqlite3.Row
        try:
            yield connection;connection.commit()
        finally:connection.close()


class OCRQualityTests(unittest.TestCase):
    def test_both_rapid_apis_preserve_exact_text_scores_boxes_and_offsets(self):
        lines=['text 90,000.00 条件。','第二行 2026-09-15']
        boxes=[[[0,0],[90,0],[90,20],[0,20]],[[0,20],[90,20],[90,40],[0,40]]]
        modern=SimpleNamespace(txts=lines,scores=[.98,.61],boxes=boxes)
        legacy=([[box,text,score] for box,text,score in zip(boxes,lines,[.98,.61])],[.1,.2])
        for value in [modern,legacy]:
            text,quality=_rapid_result(lambda image:value,'fake image')
            self.assertEqual(text,'\n'.join(lines))
            self.assertEqual(quality['minimum_confidence'],.61)
            self.assertEqual(quality['lines'][1]['char_start'],len(lines[0])+1)
            self.assertEqual(quality['lines'][1]['bbox'],boxes[1])
            self.assertEqual(quality['recognition_accuracy'],'unverified')

    def test_missing_or_invalid_scores_never_manufacture_accuracy(self):
        for scores in [None,[None,float('nan'),2],[True,float('inf'),-.1]]:
            _,quality=_rapid_result(lambda image:SimpleNamespace(txts=['a','b','c'],scores=scores),'fake')
            self.assertFalse(quality['confidence_available'])
            self.assertIsNone(quality['minimum_confidence'])
            self.assertEqual(quality['recognition_accuracy'],'unverified')

    def test_ready_page_coverage_still_carries_unverified_accuracy_warning(self):
        class Page:
            def render(self,**kwargs):return self
            def to_pil(self):return self
            def save(self,path):Path(path).write_bytes(b'fake rendered image')
            def close(self):pass
        class PDF:
            def __len__(self):return 1
            def __getitem__(self,index):return Page()
            def close(self):pass
        engine=lambda image:SimpleNamespace(txts=['recognition with omissions'],scores=[.99],boxes=[[[0,0],[1,1]]])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'fake.pdf';path.write_bytes(b'fake PDF')
            with patch.dict(sys.modules,{'pypdfium2':SimpleNamespace(PdfDocument=lambda path:PDF())}),patch('services.mail.ocr._rapid_engine',return_value=engine),patch('services.mail.thai_ocr.available',return_value=False):
                result=extract_result(path,1000,time.monotonic()+30)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['coverage_ratio'],1)
        self.assertEqual(result['units'][0]['text_origin'],'ocr')
        self.assertEqual(result['units'][0]['ocr_quality']['minimum_confidence'],.99)
        self.assertIn(OCR_NOTICE,result['warnings'])
        self.assertIn('not recognition accuracy',result['meta']['coverage_basis'])

    def test_model_projection_keeps_full_text_conditions_and_compact_quality_only(self):
        source={'evidence_id':'e','quote_text':'原文 9,000.00；审批后有效。\n下一段完整保留。',
            'char_start':120,'char_end':150,'sent_at_utc':'2026-09-15T08:00:00+00:00',
            'conditions':['审批后有效'],'object_version':'v2','text_origin':'ocr','source_text_notice':OCR_NOTICE,
            'ocr_quality':{'engine':'RapidOCR PP-OCRv5 mobile','minimum_confidence':.62,
                'mean_confidence':.82,'line_count':2,'scored_lines':2,'confidence_available':True,
                'recognition_accuracy':'unverified','confidence_basis':'engine scores',
                'lines':[{'char_start':0,'char_end':20,'confidence':.62,'bbox':[[0,0],[100,20]]}],
                'bbox':[[0,0],[1000,1000]],'all_scores':[.62,.99]}}
        projected=model_source(source)
        for field in ('quote_text','char_start','char_end','sent_at_utc','conditions','object_version','text_origin','source_text_notice'):
            self.assertEqual(projected[field],source[field])
        self.assertEqual(projected['ocr_quality']['minimum_confidence'],.62)
        self.assertEqual(projected['ocr_quality']['recognition_accuracy'],'unverified')
        self.assertNotIn('lines',projected['ocr_quality'])
        self.assertNotIn('bbox',projected['ocr_quality'])
        self.assertNotIn('all_scores',projected['ocr_quality'])
        self.assertIn('lines',source['ocr_quality'])


class OCRStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.r=MailRepository(SQLiteStore(self.root/'test.db'),text_segment_chars=8);self.r.migrate()
        package,_=self.r.create_package(owner_id='owner',display_name='package',source_kind='directory',
            source_path=str(self.root),source_revision='v1',config={})
        self.pid=package['package_id']
        message=EmailMessage();message['From']='a@example.com';message['To']='b@example.com';message['Subject']='Source'
        message['Date']='Tue, 15 Sep 2026 08:00:00 +0000';message['Message-ID']='<source@test>'
        message.set_content('body');message.add_attachment(b'fake attachment',maintype='application',subtype='pdf',filename='source.pdf')
        raw=message.as_bytes();path=self.root/'source.eml';path.write_bytes(raw)
        row=self.r.upsert_ingested_message(package_id=self.pid,source={'locator_type':'directory','root_path':str(self.root),'source_revision':'v1'},
            member=MailMember(path.name,0,len(raw),{'kind':'file','path':str(path)}),raw=raw,parsed=parse_eml(raw))
        self.key=row['message_key'];part=next(p for p in self.r.get_message(self.key,'owner',self.pid)['parts'] if p['is_explicit_attachment'])
        self.mime=part['mime_path']
    def tearDown(self):self.tmp.cleanup()
    def store(self,units,stage='basic_text',parser='pypdf',status='ready'):
        return self.r.store_extraction(self.pid,'owner',self.key,self.mime,{'status':status,'text':'\n\n'.join(u['text'] for u in units),
            'units':units,'parser':parser,'coverage_ratio':1},stage=stage)
    def pages(self):
        pages={}
        for row in self.r.attachment_segments(self.pid,'owner',self.key,self.mime):pages.setdefault(row['page_number'],'');pages[row['page_number']]+=row['text']
        return pages
    def test_new_ocr_replaces_same_page_but_keeps_native_page_and_page_quality(self):
        self.store([{'page_number':1,'text':'native exact page text'}])
        old='old OCR incomplete'
        self.store([{'page_number':1,'text':'OCR alternative should not replace native'},
                    {'page_number':2,'text':old}],stage='ocr',parser='mail-local-ocr:zh+eng')
        rows=self.r.attachment_segments(self.pid,'owner',self.key,self.mime)
        old_row=next(row for row in rows if row['page_number']==2)
        old_evidence=self.r.create_evidence(self.pid,'owner',{**old_row,'message_key':self.key,'source_kind':'attachment'})
        new='new OCR exact text 9,000.00'
        quality={'minimum_confidence':.62,'mean_confidence':.82,'confidence_available':True,
                 'recognition_accuracy':'unverified','lines':[{'char_start':0,'char_end':len(new),'confidence':.62,'bbox':[[0,0],[10,10]]}]}
        self.store([{'page_number':1,'text':'new alternative'},
                    {'page_number':2,'text':new,'ocr_quality':quality}],stage='ocr',parser='mail-local-ocr:zh+eng')
        self.assertEqual(self.pages(),{1:'native exact page text',2:new})
        rows=self.r.attachment_segments(self.pid,'owner',self.key,self.mime)
        self.assertTrue(all(row['text_origin']=='native' for row in rows if row['page_number']==1))
        recognized=next(row for row in rows if row['page_number']==2)
        self.assertEqual(recognized['text_origin'],'ocr')
        self.assertEqual(recognized['ocr_quality']['minimum_confidence'],.62)
        self.assertNotIn('lines',recognized['ocr_quality'])
        part=next(p for p in self.r.get_message(self.key,'owner',self.pid)['parts'] if p['mime_path']==self.mime)
        self.assertEqual(part['text_origin'],'mixed')
        self.assertTrue(all(isinstance(w,str) for w in part['warnings']))
        self.assertEqual(part['text_provenance']['units'][1]['ocr_quality']['lines'],quality['lines'])
        self.assertEqual(part['text_provenance']['units'][0]['extraction_method'],'pypdf')
        stale=self.r.get_evidence(old_evidence['evidence_id'],'owner')
        self.assertEqual(stale['quote_text'],old_row['text'])
        self.assertEqual(stale['text_origin'],'unknown')
        self.assertIn('历史证据',stale['source_text_notice'])
        self.assertIsNone(self.r.get_evidence(old_evidence['evidence_id'],'other'))
        fresh=self.r.create_evidence(self.pid,'owner',{**recognized,'message_key':self.key,'source_kind':'attachment'})
        self.assertEqual(fresh['text_origin'],'ocr')
        self.assertEqual(model_source({**fresh,'message_key':self.key})['source_text_notice'],OCR_NOTICE)
    def test_failed_or_empty_retry_preserves_previous_text_and_provenance(self):
        self.store([{'page_number':1,'text':'native kept'}])
        self.store([{'page_number':2,'text':'OCR kept'}],stage='ocr',parser='mail-local-ocr:zh+eng')
        before=self.r.attachment_segments(self.pid,'owner',self.key,self.mime)
        self.store([],stage='ocr',parser='mail-local-ocr:zh+eng',status='partial')
        self.assertEqual(self.r.attachment_segments(self.pid,'owner',self.key,self.mime),before)
        self.store([{'page_number':2,'text':''}],stage='ocr',parser='mail-local-ocr:zh+eng',status='failed')
        self.assertEqual(self.r.attachment_segments(self.pid,'owner',self.key,self.mime),before)
    def test_legacy_ocr_parser_can_be_updated_without_old_provenance_metadata(self):
        self.store([{'page_number':1,'text':'legacy OCR'}],stage='ocr',parser='mail-local-ocr:zh+eng')
        with self.r._connection() as c:c.execute("UPDATE mail_part_processing SET warnings_json='[]' WHERE message_key=? AND mime_path=?",(self.key,self.mime))
        self.store([{'page_number':1,'text':'updated OCR'}],stage='ocr',parser='mail-local-ocr:zh+eng')
        self.assertEqual(self.pages(),{1:'updated OCR'})
    def test_plain_warning_contract_remains_compatible_with_typed_metadata(self):
        self.store([{'page_number':1,'text':'OCR text'}],stage='ocr',parser='mail-local-ocr:zh+eng')
        with self.r._connection() as c:
            raw=c.execute("SELECT warnings_json FROM mail_part_processing WHERE message_key=? AND mime_path=? AND stage='basic_text'",(self.key,self.mime)).fetchone()[0]
        warnings,metadata=decode_processing_warnings(raw)
        self.assertEqual(metadata['kind'],VERSION)
        self.assertTrue(all(isinstance(w,str) for w in warnings))
        self.assertIn(OCR_NOTICE,warnings)
        self.assertEqual(decode_processing_warnings('["old warning"]'),(['old warning'],{}))
        self.assertEqual(decode_processing_warnings('invalid JSON'),([],{}))

    def test_normal_retry_empty_native_probe_keeps_old_ocr_origin_until_updated(self):
        self.store([{'page_number':1,'text':'old OCR text'}],stage='ocr',parser='mail-local-ocr:zh+eng')
        with self.r._connection() as c:
            c.execute("UPDATE mail_part_processing SET warnings_json='[]',status='pending' WHERE message_key=? AND mime_path=?",(self.key,self.mime))
        self.store([{'page_number':1,'text':''}],parser='pypdf',status='ocr_pending')
        rows=self.r.attachment_segments(self.pid,'owner',self.key,self.mime)
        self.assertTrue(all(row['text_origin']=='ocr' for row in rows))
        self.assertTrue(all(row['source_text_notice']==OCR_NOTICE for row in rows))
        self.store([{'page_number':1,'text':'new OCR text'}],stage='ocr',parser='mail-local-ocr:zh+eng')
        self.assertEqual(self.pages(),{1:'new OCR text'})
