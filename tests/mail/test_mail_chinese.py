"""Chinese mail presentation and evidence-grounded research regressions."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from config import Config
from services.mail.presentation import translate_text, request_presentation, source_bundle
from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.intent import build_task_spec
from services.mail.repository import MailRepository
from services.storage import Storage
from services.mail.parsers.eml import parse_eml
from services.mail.models import MailMember
from email.message import EmailMessage


class ChineseMailTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.repo=MailRepository(Storage(self.root/'test.db'));self.repo.migrate()
        self.package,_=self.repo.create_package(owner_id='owner',display_name='test',source_kind='directory',source_path=str(self.root),source_revision='v1',config={})
        self.pid=self.package['package_id']
        m=EmailMessage();m['Subject']='Scholarship';m['From']='a@example.com';m['To']='b@example.com';m['Message-ID']='<zh-test@example.com>';m.set_content('Scholarship budget is 2,500 THB, subject to approval.')
        raw=m.as_bytes();p=self.root/'test.eml';p.write_bytes(raw)
        item=self.repo.upsert_ingested_message(package_id=self.pid,source={'locator_type':'directory','root_path':str(self.root),'source_revision':'v1'},member=MailMember(p.name,0,len(raw),{'kind':'file','path':str(p)}),raw=raw,parsed=parse_eml(raw))
        self.key=item['message_key'];self.repo.finish_import(self.pid,'owner')
    def tearDown(self):self.tmp.cleanup()
    def test_failed_translation_never_labels_original_as_chinese(self):
        service=SimpleNamespace(translate_document=lambda _: {'status':'failed','translated_text':'','working_text':'Original English'})
        result=translate_text(service,'Original English');self.assertIsNone(result['text']);self.assertEqual(result['status'],'failed')
    def test_partial_translation_is_not_presented_as_complete(self):
        service=SimpleNamespace(translate_document=lambda _: {'status':'partial','translated_text':'部分译文'})
        self.assertIsNone(translate_text(service,'Original English')['text'])
    def test_chinese_and_numbers_need_no_model(self):
        self.assertEqual(translate_text(None,'中文内容')['text'],'中文内容');self.assertEqual(translate_text(None,'2500')['status'],'not_required')
    def test_body_uses_stored_source_and_checks_owner(self):
        services=SimpleNamespace(repository=self.repo)
        with self.assertRaises(LookupError):source_bundle(services,'other',{'package_id':self.pid,'kind':'message','message_key':self.key})
        _,items,meta=source_bundle(services,'owner',{'package_id':self.pid,'kind':'message','message_key':self.key,'text':'injected'})
        self.assertIn('2,500 THB',items['body']);self.assertNotIn('injected',items['body']);self.assertIsNone(meta['next_offset'])
    def test_queue_returns_pending_and_reuses_completed_cache(self):
        calls=[]
        def translate(document):calls.append(document['text']);return {'status':'completed','translated_text':'奖学金'}
        services=SimpleNamespace(repository=self.repo,translation_service=SimpleNamespace(translate_document=translate))
        data={'package_id':self.pid,'kind':'titles','message_keys':[self.key]}
        with patch.object(Config,'DATA_DIR',self.root):
            value=request_presentation(services,'owner',data)
            for _ in range(100):
                if value['status']=='completed':break
                time.sleep(.01);value=request_presentation(services,'owner',data)
            self.assertEqual(value['values'][self.key]['text'],'奖学金')
            self.assertEqual(request_presentation(services,'owner',data)['status'],'completed');self.assertEqual(len(calls),1)
        self.assertEqual(self.repo.get_message(self.key,'owner',self.pid)['subject_raw'],'Scholarship')
    def test_chinese_finding_preserves_source_and_rejects_fabricated_amount(self):
        class Model:
            def chat_json(_self,system,prompt,**kwargs):
                if kwargs['required_fields']==('queries',):return {'queries':['Scholarship']}
                if kwargs['required_fields']==('actions',):return {'actions':[{'name':'stop'}]}
                e=json.loads(prompt)['evidence'][0]
                return {'answer':'答案','claims':[], 'findings':[
                    {'text_zh':'奖学金预算为 2,500 泰铢，待批准。','evidence_id':e['evidence_id'],'source_quote':e['quote_text']},
                    {'text_zh':'奖学金预算为 9,999 泰铢，待批准。','evidence_id':e['evidence_id'],'source_quote':e['quote_text']},
                    {'text_zh':'预算已确认。','evidence_id':'outside','source_quote':e['quote_text']}]}
        session=self.repo.create_session(self.pid,'owner','test');spec=build_task_spec('查询奖学金预算',self.pid,'v1',0,max_model_calls=2,max_seconds=60)
        turn=self.repo.create_turn(session['session_id'],'owner',spec['objective'],spec)
        with patch.object(Config,'MAIL_ENABLE_VECTOR_INDEX',False):
            final=MailResearchRuntime(self.repo,model=Model()).run(turn['turn_id'],'owner')
        self.assertEqual(len(final['findings']),1);self.assertIn('2,500',final['answer']);self.assertNotIn('9,999',final['answer'])
        restored=self.repo.get_result_set(final['result_set_id'],'owner');self.assertEqual(restored['final']['answer'],final['answer'])
        self.assertEqual(restored['source_turn_id'],turn['turn_id'])
        self.assertIsNone(self.repo.get_result_set(final['result_set_id'],'other'))

    def test_thai_calendar_conversion_is_allowed_but_invented_date_is_not(self):
        from services.mail.research.verifier import verify_translated_finding
        quote="Deadline 26 กรกฎาคม 2566"
        self.assertTrue(verify_translated_finding("截止日期为2023年7月26日",quote))
        self.assertFalse(verify_translated_finding("截止日期为2024年7月26日",quote))

    def test_local_translation_preserves_numbers_and_rejects_omission(self):
        from services.mail.presentation import translate_entries
        class Model:
            def chat_json(self, system, prompt, **kwargs):
                return {"translations":[{"id":"0","text":"任命电子招标评审委员会，编号81/2567"},{"id":"1","text":"预算250泰铢"}]}
        result={};translate_entries(SimpleNamespace(model=Model()),{"good":"Electronic tender committee 81/2567","bad":"Budget 2500 THB"},lambda k,v:result.update({k:v}))
        self.assertEqual(result['good']['text'],'任命电子招标评审委员会，编号81/2567')
        self.assertIsNone(result['bad']['text']);self.assertEqual(result['bad']['status'],'failed')
