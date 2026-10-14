"""Original retention, attachment location and workbench integration scenarios."""
import io
import json
import unittest
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from config import Config
from services.storage import Storage
from services.mail.api import register_mail_routes
from services.mail.bootstrap import build_mail_services
from services.mail.extraction import _extract_attachment
from services.mail.foundation import backfill_slice, content, fragments
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.mail.conversations import list_conversations
from web_compat import SJFXFastAPI


class FoundationContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.root=Path(self.tmp.name)
        allowed_roots=patch.object(Config,'SCAN_ALLOWED_ROOTS',[str(self.root.resolve())])
        allowed_roots.start();self.addCleanup(allowed_roots.stop)
        self.storage=Storage(self.root/'test.db')
        self.services=build_mail_services(self.storage,job_factory=self.storage.create_or_get_typed_job,owner_resolver=lambda:'owner')
        self.r=self.services.repository;self.r.migrate()
        self.p,_=self.r.create_package(owner_id='owner',display_name='package',source_kind='directory',source_path=str(self.root),source_revision='v1',config={})
        self.pid=self.p['package_id'];self.n=0
    def tearDown(self):
        self.services.close()
        self.tmp.cleanup()
    def ingest(self,body='new text\n> quoted\n-- \nsignature\n',*,mid=None,reply=None,attachment=False):
        m=EmailMessage();m['From']='alice@example.com';m['To']='bob@example.com';m['Cc']='bob@example.com';m['Subject']='Test'
        m['Date']='Thu, 10 Sep 2026 23:59:59 +0000';m['Message-ID']='<{}@example.com>'.format(mid or self.n)
        if reply:m['In-Reply-To']='<{}@example.com>'.format(reply)
        m.set_content(body)
        if attachment:m.add_attachment(b'attachment text',maintype='text',subtype='plain',filename='notes.txt')
        raw=m.as_bytes();path=self.root/('m{}.eml'.format(self.n));path.write_bytes(raw)
        row=self.r.upsert_ingested_message(package_id=self.pid,source={'locator_type':'directory','root_path':str(self.root),'source_revision':'v1'},member=MailMember(path.name,self.n,len(raw),{'kind':'file','path':str(path)}),raw=raw,parsed=parse_eml(raw));self.n+=1
        return row['message_key'],path,raw
    def test_original_survives_source_removal_and_all_fragments_reconstruct_body(self):
        key,path,raw=self.ingest(attachment=True);path.unlink()
        value=content(self.r,key,'owner',self.pid)
        body=next(p for p in value['parts'] if p['part_role']=='body_plain')
        self.assertEqual(''.join(f['text'] for f in body['fragments']),body['text'])
        self.assertTrue(body['text'].endswith('\n'))
        self.assertEqual({f['fragment_kind'] for f in body['fragments']},{'authored','quoted','signature'})
        app=SJFXFastAPI();register_mail_routes(app,self.services)
        with patch.object(Config,'MAIL_FEATURE_ENABLED',True),TestClient(app) as client:
            r=client.get('/api/mail/messages/'+key+'/raw',params={'package_id':self.pid})
            self.assertEqual(r.content,raw);self.assertIn('attachment;',r.headers['content-disposition'])
            detail=client.get('/api/mail/messages/'+key,params={'package_id':self.pid}).json()['message']
            attachment=next(p for p in detail['parts'] if p['filename_raw']=='notes.txt')
            self.assertEqual(attachment['is_explicit_attachment'],1)
            r=client.get('/api/mail/messages/'+key+'/parts/'+attachment['mime_path']+'/raw',params={'package_id':self.pid})
            self.assertEqual(r.content,b'attachment text')
            self.assertEqual(client.get('/api/mail/messages/'+key+'/raw',params={'package_id':'other'}).status_code,404)
    def test_direct_reader_builds_conversation_and_preserves_historical_navigation(self):
        parent,_,_=self.ingest(mid='direct-parent')
        child,path,_=self.ingest(mid='direct-child',reply='direct-parent')
        with patch.object(Config,'MAIL_ENABLE_CONVERSATIONS',True):
            fresh=content(self.r,child,'owner',self.pid)
            self.assertEqual(len(fresh['conversation_ids']),1)
            self.assertTrue(any(e['target_message_key']==parent for e in fresh['reply_edges']))
            path.unlink()
            with self.r._connection() as connection:
                connection.execute('DELETE FROM mail_raw_store WHERE raw_sha256=(SELECT raw_sha256 FROM mail_messages WHERE message_key=?)',(child,))
            historical=content(self.r,child,'owner',self.pid)
            self.assertFalse(historical['raw_available'])
            self.assertFalse(historical['complete'])
            self.assertEqual(historical['conversation_ids'],fresh['conversation_ids'])
            self.assertEqual(historical['reply_edges'],fresh['reply_edges'])
    def test_attachment_units_remain_on_correct_sheet_and_pagination_is_complete(self):
        from openpyxl import Workbook
        wb=Workbook();wb.active.title='First';wb.active.append(['alpha',1]);wb.create_sheet('Second').append(['beta',2])
        buffer=io.BytesIO();wb.save(buffer);wb.close()
        result=_extract_attachment(SimpleNamespace(raw_payload=buffer.getvalue(),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',filename_raw='book.xlsx'))
        self.assertEqual(result['status'],'ready');self.assertEqual({u['sheet_name'] for u in result['units']},{'First','Second'})
        self.assertTrue(all(u['cell_range']=='A1:B1' for u in result['units']))
        key,_,_=self.ingest(attachment=True)
        part=next(p for p in self.r.get_message(key,'owner',self.pid)['parts'] if p['is_explicit_attachment'])
        self.r.store_extraction(self.pid,'owner',key,part['mime_path'],result)
        one=self.r.attachment_segments(self.pid,'owner',key,part['mime_path'],limit=1)
        two=self.r.attachment_segments(self.pid,'owner',key,part['mime_path'],limit=1,offset=1)
        self.assertEqual([one[0]['sheet_name'],two[0]['sheet_name']],['First','Second'])
        previous=[one[0]['segment_id'],two[0]['segment_id']]
        self.r.store_extraction(self.pid,'owner',key,part['mime_path'],result)
        self.assertEqual(previous,[s['segment_id'] for s in self.r.attachment_segments(self.pid,'owner',key,part['mime_path'])])
    def test_pdf_pages_empty_page_and_truncation_are_honest(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        writer=PdfWriter()
        for value in ('page one','', 'page three'):
            page=writer.add_blank_page(width=600,height=800)
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
            stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 20 700 Td ('+value+') Tj ET').encode())
            page[NameObject('/Contents')]=stream
        buffer=io.BytesIO();writer.write(buffer)
        result=_extract_attachment(SimpleNamespace(raw_payload=buffer.getvalue(),content_type='application/pdf',filename_raw='pages.pdf'))
        self.assertEqual(result['status'],'ocr_pending');self.assertEqual(result['meta']['missing_pages'],[2])
        self.assertEqual([u['page_number'] for u in result['units']],[1,2,3]);self.assertLess(result['coverage_ratio'],1)
        result=_extract_attachment(SimpleNamespace(raw_payload=b'abcdefghijk',content_type='text/plain',filename_raw='x.txt'),max_chars=5)
        self.assertEqual(result['status'],'partial');self.assertTrue(result['truncated']);self.assertEqual(result['text'],'abcde')
    def test_routes_share_scope_and_split_retains_hidden_members(self):
        a,_,_=self.ingest(mid='a');b,_,_=self.ingest(mid='b',reply='a');c,_,_=self.ingest(mid='c',reply='a')
        self.r.finish_import(self.pid,'owner')
        conv=next(v for v in list_conversations(self.r,self.pid,'owner') if v['message_count']==3)
        app=SJFXFastAPI();register_mail_routes(app,self.services)
        with patch.object(Config,'MAIL_FEATURE_ENABLED',True),TestClient(app) as client:
            base='/api/mail/packages/'+self.pid
            value=client.get(base+'/messages',params={'date_to':'2026-09-10','query':'new text'}).json()['messages']
            self.assertEqual(value['total'],3)
            self.assertEqual(client.get(base+'/messages',params={'q':'missing content'}).json()['messages']['total'],0)
            self.assertEqual(client.get(base+'/messages',params={'has_attachment':'invalid'}).status_code,400)
            self.assertEqual(client.get(base+'/conversations',params={'has_attachment':'invalid'}).status_code,400)
            self.assertEqual(client.post('/api/mail/conversations/'+conv['conversation_id']+'/corrections',json=['invalid']).status_code,400)
            network=client.get(base+'/relations/network',params={'q':'absent'}).json()['network'];self.assertEqual(network['nodes'],[])
            response=client.post('/api/mail/conversations/'+conv['conversation_id']+'/corrections',json={'action':'split','message_keys':[b],'groups':[[b],[a]],'package_id':self.pid})
            self.assertEqual(response.status_code,200,response.text)
            rows=client.get(base+'/conversations').json()['items'];self.assertEqual(sorted(v['message_count'] for v in rows),[1,2])
            self.assertEqual(sum(v['message_count'] for v in rows),3)
            history=client.get(base+'/conversation-corrections').json();self.assertEqual(len(history['items']),1)
    def test_historical_backfill_resumes_without_multiplying_records(self):
        for i in range(3):self.ingest(mid=str(i))
        with self.r._connection() as c:
            c.execute('DELETE FROM mail_raw_store');c.execute('DELETE FROM mail_import_entries');c.execute('DELETE FROM mail_body_fragments')
        first=backfill_slice(self.r,self.pid,'owner',limit=2);self.assertTrue(first['_requeue_slice'])
        final=backfill_slice(self.r,self.pid,'owner',limit=2);self.assertEqual(final['status'],'ready')
        again=backfill_slice(self.r,self.pid,'owner',limit=2);self.assertTrue(again['already_current'])
        with self.r._connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail_occurrences').fetchone()[0],3)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail_raw_store').fetchone()[0],3)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mail_import_entries WHERE message_key IS NOT NULL').fetchone()[0],3)

    def test_partial_ocr_cannot_discard_native_text_on_later_pages(self):
        key,_,_=self.ingest(attachment=True)
        part=next(p for p in self.r.get_message(key,'owner',self.pid)['parts'] if p['is_explicit_attachment'])
        path=part['mime_path']
        self.r.store_extraction(self.pid,'owner',key,path,{'status':'ocr_pending','text':'native third page','units':[{'text':'native third page','page_number':3}],'parser':'pypdf','coverage_ratio':1/3})
        self.r.store_extraction(self.pid,'owner',key,path,{'status':'partial','text':'OCR page one','units':[{'text':'OCR page one','page_number':1}],'parser':'ocr','warnings':['time limit']},stage='ocr')
        rows=self.r.attachment_segments(self.pid,'owner',key,path)
        self.assertEqual([(r['page_number'],r['text']) for r in rows],[(1,'OCR page one'),(3,'native third page')])
        self.r.store_extraction(self.pid,'owner',key,path,{'status':'partial','text':'','units':[],'parser':'ocr'},stage='ocr')
        self.assertEqual(rows,self.r.attachment_segments(self.pid,'owner',key,path))

    def test_zip_duplicate_names_keep_distinct_bytes_and_non_mail_inventory(self):
        import zipfile,warnings
        from dataclasses import replace
        from services.mail.jobs import _import
        from services.mail.models import SourceRef
        from services.mail.foundation import source_inventory
        from services.mail.source import AdapterRegistry
        from services.mail.attachments import read_message_raw
        _,_,first=self.ingest(mid='first');_,_,second=self.ingest(mid='second')
        path=self.root/'package.zip'
        with warnings.catch_warnings(),zipfile.ZipFile(path,'w') as z:
            warnings.simplefilter('ignore')
            z.writestr('same.eml',first);z.writestr('same.eml',second);z.writestr('../bad.eml',first);z.writestr('readme.txt','non mail')
        package,_=self.r.create_package(owner_id='owner',display_name='zip',source_kind='zip',source_path=str(path),source_revision='zip-v1',config={})
        result=_import({'id':'zip','owner_id':'owner','options':{'package_id':package['package_id']}},self.services)
        self.assertEqual(result['counts']['logical_messages'],2)
        self.assertEqual(result['inventory']['restricted'],1);self.assertEqual(result['inventory']['unsupported'],1)
        values={read_message_raw(self.r,m['message_key'],'owner',package['package_id']) for m in self.r.list_messages(package['package_id'],'owner')['items']}
        self.assertEqual(values,{first,second})
