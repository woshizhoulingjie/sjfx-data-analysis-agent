"""Source preservation, bounded recovery and mail-job checkpoints."""
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import Config
from services.storage import Storage
from services.mail.repository import MailRepository
from services.mail.policy import MailPolicy
from services.mail.parsers.eml import parse_eml
from services.mail.attachments import read_message_raw, read_part
from services.mail.coverage import coverage_details
from services.mail.jobs import _import, _extract
from services.mail.foundation import VERSION, backfill_slice, content, set_state


def mail_bytes(ordinal=1, *, attachment=None, nested=None):
    msg=EmailMessage()
    msg['From']='a@example.com';msg['To']='b@example.com'
    msg['Message-ID']='<foundation-job-%s@example.com>' % ordinal
    msg['Subject']='Foundation %s' % ordinal
    msg['Date']='Tue, 07 May 2024 15:38:50 +0700'
    msg.set_content('Complete source body\n')
    if attachment:msg.add_attachment(b'attachment payload',maintype='application',subtype='octet-stream',filename=attachment)
    if nested:msg.add_attachment(nested)
    return msg.as_bytes()


class FoundationJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.source=self.base/'source';self.source.mkdir()
        storage=Storage(self.base/'jobs.sqlite3')
        self.repo=MailRepository(storage);self.repo.migrate()
        package,_=self.repo.create_package(owner_id='owner',display_name='Foundation jobs',source_kind='directory',source_path=str(self.source),source_revision='jobs-v1',config={})
        self.pid=package['package_id']
        self.policy=MailPolicy(allowed_roots=(self.source,),max_archive_entries=100,max_message_bytes=1000000,max_attachment_bytes=1000000,max_total_uncompressed_bytes=10000000,max_compression_ratio=200,max_member_depth=16,max_mime_parts=100,max_nested_message_depth=3)
        self.services=SimpleNamespace(repository=self.repo,policy=self.policy,parser=None,storage=SimpleNamespace(update_job=lambda *args,**kwargs:None))
        self.job={'id':'foundation-job','owner_id':'owner','options':{'package_id':self.pid}}
        self.limit=patch.object(Config,'MAIL_IMPORT_SLICE_MESSAGES',100)
        self.limit.start();self.addCleanup(self.limit.stop)

    def write(self,name,raw):
        path=self.source/name;path.write_bytes(raw);return path

    def test_one_bad_mail_does_not_block_other_source_entries(self):
        self.write('00-bad.eml',b'bad source')
        self.write('01-good.eml',mail_bytes())
        def parse(raw,**kwargs):
            if raw==b'bad source':raise ValueError('corrupt source')
            return parse_eml(raw,**kwargs)
        with patch('services.mail.jobs.parse_eml',side_effect=parse):result=_import(self.job,self.services)
        self.assertEqual(result['counts']['logical_messages'],1)
        self.assertEqual(result['inventory']['terminal_failed'],1)
        with self.repo._connection() as c:
            rows={r[0]:r[1] for r in c.execute('SELECT member_name,status FROM mail_import_entries WHERE package_id=?',(self.pid,))}
            state=c.execute("SELECT status FROM mail_foundation_state WHERE package_id=? AND stage='mail_parse'",(self.pid,)).fetchone()[0]
        self.assertEqual(rows['00-bad.eml'],'terminal_failed')
        self.assertIn(rows['01-good.eml'],{'ready','duplicate'})
        self.assertEqual(state,'partial')
        self.assertEqual(self.repo.progress(self.job['id'])['cursor']['next_ordinal'],2)

    def test_nested_message_is_saved_without_default_extra_mail_count(self):
        nested=EmailMessage();nested['From']='nested@example.com';nested['To']='b@example.com';nested['Subject']='Inner';nested['Message-ID']='<inner@example.com>';nested.set_content('Inner body')
        self.write('outer.eml',mail_bytes(nested=nested))
        with patch.object(Config,'MAIL_ENABLE_NESTED_MESSAGES',True):result=_import(self.job,self.services)
        self.assertEqual(result['counts']['logical_messages'],1)
        message=self.repo.list_messages(self.pid,'owner')['items'][0]
        details=self.repo.get_message(message['message_key'],'owner',self.pid)
        self.assertIn('nested_message',{p['part_role'] for p in details['parts']})
        self.assertNotIn('nested@example.com',{p['normalized_address'] for p in details['participants']})

    def test_original_and_attachment_survive_source_removal(self):
        raw=mail_bytes(attachment='contract.bin');path=self.write('saved.eml',raw)
        _import(self.job,self.services)
        key=self.repo.list_messages(self.pid,'owner')['items'][0]['message_key']
        details=self.repo.get_message(key,'owner',self.pid)
        attachment=next(p for p in details['parts'] if p['is_explicit_attachment'])
        path.unlink()
        self.assertEqual(read_message_raw(self.repo,key,'owner',self.pid),raw)
        part,stored=read_part(self.repo,key,'owner',self.pid,attachment['mime_path'])
        self.assertEqual(stored,raw);self.assertEqual(part.raw_payload,b'attachment payload')
        with self.assertRaises(LookupError):read_message_raw(self.repo,key,'other',self.pid)

    def test_attachment_retries_stop_and_other_attachment_succeeds(self):
        self.write('bad.eml',mail_bytes(1,attachment='bad.bin'))
        self.write('good.eml',mail_bytes(2,attachment='good.bin'))
        _import(self.job,self.services)
        calls=[]
        def extractor(part):
            calls.append(part.filename_raw)
            if part.filename_raw=='bad.bin':return {'status':'failed','parser':'synthetic','error':{'message':'broken attachment'}}
            return {'status':'ready','parser':'synthetic','text':'Complete extracted text','warnings':[]}
        with patch('services.mail.jobs.extract_attachment',side_effect=extractor):
            first=_extract(self.job,self.services)
            self.assertTrue(first.get('_defer_slice'))
            self.assertFalse(self.repo.pending_attachments(self.pid,'owner'))
            for _ in range(2):
                with self.repo._connection() as c:c.execute("UPDATE mail_part_processing SET next_retry_at='2000-01-01 00:00:00' WHERE status='waiting_retry'")
                last=_extract(self.job,self.services)
            self.assertFalse(last.get('_defer_slice'))
            self.assertFalse(last.get('_requeue_slice'))
            self.assertEqual(_extract(self.job,self.services)['processed'],0)
        self.assertEqual(calls.count('bad.bin'),3);self.assertEqual(calls.count('good.bin'),1)
        with self.repo._connection() as c:
            states={r[0]:(r[1],r[2]) for r in c.execute("SELECT p.filename_raw,x.status,x.retry_count FROM mail_parts p JOIN mail_part_processing x ON x.message_key=p.message_key AND x.mime_path=p.mime_path WHERE x.stage='basic_text'")}
        self.assertEqual(states['bad.bin'],('terminal_failed',3))
        self.assertEqual(states['good.bin'][0],'ready')
        self.assertEqual(coverage_details(self.repo,self.pid,'owner')['by_status'],{'terminal_failed':1})

    def test_legacy_completed_attachment_upgrades_locations_once(self):
        self.write('legacy.eml',mail_bytes(attachment='old.pdf'))
        _import(self.job,self.services)
        item=self.repo.pending_attachments(self.pid,'owner')[0]
        key,path=item['message_key'],item['mime_path']
        self.repo.store_extraction(self.pid,'owner',key,path,{'status':'ready','parser':'legacy','text':'Old flattened text'})
        self.repo.update_processing(key,path,'ocr','ready',text='Old OCR')
        with self.repo._connection() as c:c.execute("UPDATE mail_part_processing SET parser_version='mail-extract-v1'")
        result={'status':'ready','parser':'synthetic','text':'Page one\nPage two','units':[{'page_number':1,'text':'Page one'},{'page_number':2,'text':'Page two'}]}
        with patch('services.mail.jobs.extract_attachment',return_value=result) as extractor:
            _extract(self.job,self.services);_extract(self.job,self.services)
            self.assertEqual(extractor.call_count,1)
        with self.repo._connection() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT page_number FROM mail_text_segments WHERE source_kind='attachment' ORDER BY ordinal")],[1,2])
            self.assertEqual(tuple(c.execute("SELECT status,parser_version FROM mail_part_processing WHERE stage='ocr'").fetchone()),('skipped','mail-extract-v2'))
        self.assertEqual(coverage_details(self.repo,self.pid,'owner')['total'],0)

    def test_legacy_upgrade_failure_preserves_text_and_resets_required_ocr(self):
        self.write('legacy.eml',mail_bytes(attachment='old.pdf'))
        _import(self.job,self.services)
        item=self.repo.pending_attachments(self.pid,'owner')[0]
        key,path=item['message_key'],item['mime_path']
        self.repo.store_extraction(self.pid,'owner',key,path,{'status':'ready','parser':'legacy','text':'Old readable text'})
        self.repo.update_processing(key,path,'ocr','ready',text='Old OCR')
        with self.repo._connection() as c:c.execute("UPDATE mail_part_processing SET parser_version='mail-extract-v1'")
        with patch('services.mail.jobs.extract_attachment',return_value={'status':'failed','parser':'synthetic','error':{'message':'temporary'}}):
            _extract(self.job,self.services)
        with self.repo._connection() as c:
            self.assertEqual(c.execute("SELECT text FROM mail_text_segments WHERE source_kind='attachment'").fetchone()[0],'Old readable text')
            self.assertEqual(c.execute("SELECT status FROM mail_part_processing WHERE stage='ocr'").fetchone()[0],'ready')
            c.execute("UPDATE mail_part_processing SET next_retry_at='2000-01-01' WHERE stage='basic_text'")
        result={'status':'ocr_pending','parser':'synthetic','text':'Native page','units':[{'page_number':1,'text':'Native page'}]}
        with patch('services.mail.jobs.extract_attachment',return_value=result):_extract(self.job,self.services)
        with self.repo._connection() as c:
            self.assertEqual(tuple(c.execute("SELECT status,retry_count,parser_version FROM mail_part_processing WHERE stage='ocr'").fetchone()),('pending',0,'mail-extract-v2'))
        self.assertEqual(self.repo.schedule_attachment_upgrade(self.pid,'owner'),0)

    def test_cancelled_import_resumes_without_duplicates(self):
        self.write('00.eml',mail_bytes(1));self.write('01.eml',mail_bytes(2))
        checks=[0]
        def cancelled(_):checks[0]+=1;return checks[0]>1
        with patch.object(self.repo,'cancelled',side_effect=cancelled):
            with self.assertRaisesRegex(RuntimeError,'取消'):_import(self.job,self.services)
        self.assertEqual(self.repo.progress(self.job['id'])['cursor']['next_ordinal'],1)
        self.assertEqual(self.repo.overview(self.pid,'owner')['counts']['logical_messages'],1)
        _import(self.job,self.services)
        counts=self.repo.overview(self.pid,'owner')['counts']
        self.assertEqual(counts['logical_messages'],2);self.assertEqual(counts['occurrences'],2)
        _import(self.job,self.services)
        self.assertEqual(self.repo.overview(self.pid,'owner')['counts']['occurrences'],2)

    def test_historical_version_or_source_change_restarts_finished_cursor(self):
        self.write('historical.eml',mail_bytes())
        _import(self.job,self.services)
        key=self.repo.list_messages(self.pid,'owner')['items'][0]['message_key']
        for old_version,old_fingerprint in (("mail-foundation-v0","jobs-v1"),(VERSION,"old-source-revision")):
            with self.subTest(version=old_version,fingerprint=old_fingerprint):
                set_state(self.repo,self.pid,'historical_backfill','ready',cursor={'last_message_key':key,'processed':99,'errors':0},fingerprint=old_fingerprint)
                with self.repo._connection() as c:
                    c.execute("UPDATE mail_foundation_state SET version=? WHERE package_id=? AND stage='historical_backfill'",(old_version,self.pid))
                    c.execute("UPDATE mail_messages SET parser_version='legacy-parser' WHERE message_key=?",(key,))
                    c.execute('DELETE FROM mail_body_fragments WHERE message_key=?',(key,))
                result=backfill_slice(self.repo,self.pid,'owner',limit=2)
                self.assertEqual(result['status'],'ready')
                self.assertEqual(result['processed'],1)
                self.assertFalse(result.get('already_current'))
                with self.repo._connection() as c:
                    state=c.execute("SELECT version,fingerprint,status FROM mail_foundation_state WHERE package_id=? AND stage='historical_backfill'",(self.pid,)).fetchone()
                    self.assertEqual(tuple(state),(VERSION,'jobs-v1','ready'))
                    self.assertEqual(c.execute('SELECT parser_version FROM mail_messages WHERE message_key=?',(key,)).fetchone()[0],'mail-eml-v3')
                    self.assertGreater(c.execute('SELECT COUNT(*) FROM mail_body_fragments WHERE message_key=?',(key,)).fetchone()[0],0)
                self.assertTrue(backfill_slice(self.repo,self.pid,'owner')['already_current'])

    def test_reading_upgrade_refreshes_chinese_subject_without_changing_original(self):
        message=EmailMessage()
        message['From']='a@example.com';message['To']='b@example.com'
        message['Message-ID']='<chinese-subject-upgrade@example.com>'
        message['Subject']='回复： 报价第二轮沟通'
        message['Date']='Tue, 07 May 2024 15:38:50 +0700'
        message.set_content('完整正文与来源保持一致\n')
        raw=message.as_bytes();self.write('subject.eml',raw)
        _import(self.job,self.services)
        key=self.repo.list_messages(self.pid,'owner')['items'][0]['message_key']
        with self.repo._connection() as connection:
            connection.execute('UPDATE mail_messages SET subject_normalized=? WHERE message_key=?',('回复： 报价第二轮沟通',key))
        result=backfill_slice(self.repo,self.pid,'owner',limit=10)
        self.assertEqual(result['errors'],0)
        details=self.repo.get_message(key,'owner',self.pid)
        self.assertEqual(details['subject_normalized'],'报价第二轮沟通')
        self.assertEqual(details['subject_raw'],'回复： 报价第二轮沟通')
        self.assertEqual(read_message_raw(self.repo,key,'owner',self.pid),raw)
        self.assertEqual(len(details['occurrences']),1)

    def test_unreadable_inventory_entry_does_not_block_other_messages(self):
        blocked=self.write('00-unreadable.eml',mail_bytes(1))
        self.write('01-good.eml',mail_bytes(2))
        original_stat=Path.stat
        def unreadable_stat(path,*args,**kwargs):
            if path==blocked:raise PermissionError('synthetic stat permission denied')
            return original_stat(path,*args,**kwargs)
        with patch.object(Path,'stat',unreadable_stat):result=_import(self.job,self.services)
        self.assertEqual(result['counts']['logical_messages'],1)
        self.assertEqual(result['inventory']['restricted'],1)
        with self.repo._connection() as c:
            row=c.execute('SELECT byte_size,status,error_json FROM mail_import_entries WHERE package_id=? AND member_name=?',(self.pid,blocked.name)).fetchone()
            self.assertEqual((row['byte_size'],row['status']),(0,'restricted'))
            self.assertIn('synthetic stat permission denied',row['error_json'])

    def test_missing_historical_original_keeps_readable_projection(self):
        path=self.write('legacy.eml',mail_bytes())
        _import(self.job,self.services)
        key=self.repo.list_messages(self.pid,'owner')['items'][0]['message_key']
        with self.repo._connection() as c:c.execute('DELETE FROM mail_raw_store')
        path.unlink()
        with self.assertRaises(RuntimeError):read_message_raw(self.repo,key,'owner',self.pid)
        value=content(self.repo,key,'owner',self.pid)
        self.assertFalse(value['raw_available'])
        self.assertFalse(value['complete'])
        body=next(p for p in value['parts'] if p['part_role']=='body_plain')
        self.assertEqual(body['text'],'Complete source body\n')
        self.assertEqual(''.join(f['text'] for f in body['fragments']),body['text'])
        self.assertEqual(value['headers']['subject'],['Foundation 1'])
        self.assertEqual(len(value['occurrences']),1)
        self.assertTrue(value['warnings'])
        with self.assertRaises(LookupError):content(self.repo,key,'other',self.pid)


if __name__=='__main__':unittest.main()
