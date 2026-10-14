"""Regression contracts for mail mining, isolation, evidence and rollback."""
import hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from email.message import EmailMessage
from config import Config
from services.mail.schema import DDL,FTS_DDL,FTS_FALLBACK_DDL,migrate
from services.mail.retrieval import rrf_fuse
from services.mail.facts import normalize_amount,normalize_date,normalize_fact,save_facts,detect_conflicts,final_fact
from services.mail.research.verifier import verify_numeric,verify_polarity
from services.mail.research.intent import build_task_spec
from services.mail.research.runtime import MailResearchRuntime
from services.mail.research.tools import MailResearchTools
from services.mail.versions import trace
from services.mail.conversations import list_conversations
from services.mail.bootstrap import build_mail_services
from services.mail.jobs import _import
from services.mail.api import register_mail_routes
from services.mail.models import MailMember
from services.mail.parsers.eml import parse_eml
from services.storage import Storage
from services.mail.repository import MailRepository


def raw_mail(subject='Test',body='Alice confirms delivery date 2026-10-10',mid='a',reply=None,attachment=False):
    m=EmailMessage();m['From']='Alice <alice@example.com>';m['To']='bob@example.com';m['Subject']=subject;m['Message-ID']='<'+mid+'@example.com>';m['Date']='Thu, 10 Sep 2026 08:00:00 +0000'
    if reply:m['In-Reply-To']='<'+reply+'@example.com>'
    m.set_content(body)
    if attachment:m.add_attachment(b'contract text',maintype='application',subtype='octet-stream',filename='contract_v1.txt')
    return m.as_bytes()


class MiningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        allowed_roots=patch.object(Config,'SCAN_ALLOWED_ROOTS',[str(self.root.resolve())])
        allowed_roots.start();self.addCleanup(allowed_roots.stop)
        self.storage=Storage(self.root/'test.db');self.r=MailRepository(self.storage);self.r.migrate();self.n=0
        self.package,_=self.r.create_package(owner_id='owner',display_name='test',source_kind='directory',source_path=str(self.root),source_revision='test-v1',config={})
        self.pid=self.package['package_id']
    def tearDown(self):self.tmp.cleanup()
    def ingest(self,raw):
        path=self.root/('mail-'+str(self.n)+'.eml');path.write_bytes(raw);member=MailMember(path.name,self.n,len(raw),{'kind':'file','path':str(path)});self.n+=1
        return self.r.upsert_ingested_message(package_id=self.pid,source={'locator_type':'directory','root_path':str(self.root),'source_revision':'test-v1'},member=member,raw=raw,parsed=parse_eml(raw))
    def test_migration_preserves_v2_checksum_and_other_tables(self):
        c=sqlite3.connect(':memory:');c.executescript(DDL);c.executescript(FTS_DDL);c.execute('CREATE TABLE unrelated (value TEXT)');c.execute("INSERT INTO unrelated VALUES('keep')")
        digest=hashlib.sha256((DDL+FTS_DDL+FTS_FALLBACK_DDL).encode()).hexdigest();c.execute('INSERT INTO mail_schema_migrations(version,checksum) VALUES(2,?)',(digest,))
        self.assertTrue(migrate(c));self.assertFalse(migrate(c));self.assertEqual(c.execute('SELECT value FROM unrelated').fetchone()[0],'keep');self.assertEqual(c.execute('SELECT checksum FROM mail_schema_migrations WHERE version=2').fetchone()[0],digest)
    def test_off_switch_preserves_exact_lexical_output(self):
        self.ingest(raw_mail(body='find marker 45000 THB'))
        with patch.object(Config,'MAIL_ENABLE_VECTOR_INDEX',False):
            with patch.object(self.r,'search_segments_lexical',return_value={'items':[{'test':1}],'coverage':{'same':1}}) as lexical:
                self.assertEqual(self.r.search_segments(self.pid,'owner','marker'),{'items':[{'test':1}],'coverage':{'same':1}});lexical.assert_called_once()
    def test_rrf_uses_ranks_not_raw_similarity(self):
        a={'segment_id':'a','message_key':'a','mime_path':'1','vector_score':.8};b={'segment_id':'b','message_key':'b','mime_path':'1','vector_score':.97}
        result=rrf_fuse([('lexical',[a]),('vector',[b,a])]);self.assertEqual(result[0]['segment_id'],'a');self.assertEqual(result[0]['retrieval']['vector']['rank'],2)
    def test_exact_and_candidate_conversations_stay_separate(self):
        self.ingest(raw_mail(subject='Shared',mid='a'));self.ingest(raw_mail(subject='Shared',mid='b',reply='a'));self.ingest(raw_mail(subject='Shared',mid='c'))
        self.r.finish_import(self.pid,'owner');convs=list_conversations(self.r,self.pid,'owner');self.assertEqual(sorted(c['message_count'] for c in convs),[1,2]);self.assertEqual(len(self.r.threads(self.pid,'owner',status='exact')),1);self.assertEqual(len(self.r.threads(self.pid,'owner',status='candidate')),1)
        ids=[c['conversation_id'] for c in convs];self.assertEqual(ids,[c['conversation_id'] for c in list_conversations(self.r,self.pid,'owner')])
    def test_normalization_does_not_invent_dates_or_amounts(self):
        self.assertEqual(normalize_amount('๑๒,๐๐๐ บาท'),{'amount':'1.2E+4','currency':'THB'});self.assertEqual(normalize_amount('3万元')['amount'],'3E+4');self.assertIsNone(normalize_date('下周五'));self.assertEqual(normalize_date('下周五','2026-09-10T00:00:00+00:00'),'2026-09-18');self.assertEqual(normalize_date('15 กันยายน 2569'),'2026-09-15');self.assertIsNone(normalize_date('2026-02-30'))
        self.assertFalse(verify_numeric('5000 THB','50,000 THB'));self.assertFalse(verify_polarity('confirmed','not confirmed; subject to approval'))
    def test_fields_require_source_values_and_no_cross_subject_conflicts(self):
        row=self.ingest(raw_mail(body='Event A costs 100 THB. Event B costs 200 THB.'))
        e=self.r.search_segments(self.pid,'owner','Event')['items'][0]
        invalid=normalize_fact({'fact_type':'amount','value_raw':'300 THB','quote':'Event A costs 100 THB.','subject_ref':'Event A'},e);self.assertIsNone(invalid)
        facts=[]
        for name,amount in [('Event A','100 THB'),('Event B','200 THB')]:
            f=normalize_fact({'fact_type':'amount','fact_key':'price','value_raw':amount,'quote':name+' costs '+amount+'.','subject_ref':name},e);self.assertIsNotNone(f);facts.append(f)
        save_facts(self.r,self.pid,'owner',facts);self.assertEqual(detect_conflicts(self.r,self.pid,'owner'),[])
        with self.assertRaises(ValueError):save_facts(self.r,self.pid,'other',facts)
    def test_same_payload_versions_have_complete_transfer_links(self):
        self.ingest(raw_mail(mid='a',attachment=True));self.ingest(raw_mail(mid='b',attachment=True))
        result=trace(self.r,self.pid,'owner');self.assertEqual(len(result['items']),2);self.assertEqual({r['version_ordinal'] for r in result['items']},{1})
    def test_followup_only_filters_parent_messages_even_if_search_finds_others(self):
        one=self.ingest(raw_mail(mid='a',attachment=True));two=self.ingest(raw_mail(mid='b',attachment=True));self.ingest(raw_mail(mid='c'))
        parent=self.r.create_result_set(self.pid,'owner',[{'item_id':one['message_key'],'item_type':'message','status':'matched'}],{}, {})
        session=self.r.create_session(self.pid,'owner','test');spec=build_task_spec('只保留其中带附件的',self.pid,'test-v1',0,base_result_set_id=parent,max_model_calls=0)
        turn=self.r.create_turn(session['session_id'],'owner',spec['objective'],spec);final=MailResearchRuntime(self.r).run(turn['turn_id'],'owner');result=self.r.get_result_set(final['result_set_id'],'owner')
        self.assertEqual([i['item_id'] for i in result['items']],[one['message_key']]);self.assertEqual(result['parent_result_set_id'],parent)
    def test_read_message_contains_actual_body_evidence(self):
        m=self.ingest(raw_mail());result=MailResearchTools(self.r,'owner',self.pid).read_message(m['message_key']);self.assertIn('Alice confirms',result['items'][0]['quote_text'])
    def test_empty_import_fails_honestly(self):
        services=build_mail_services(self.storage)
        with self.assertRaisesRegex(ValueError,'EML'):_import({'id':'empty','owner_id':'owner','options':{'package_id':self.pid}},services)
        self.assertEqual(self.r.get_package(self.pid,'owner')['status'],'failed')
    def test_dynamic_schema_and_conflicting_status(self):
        spec=build_task_spec('提取价格和报名日期',self.pid,'v',0);self.assertIn('金额',spec['output_schema']);self.assertIn('日期',spec['output_schema']);self.assertEqual(MailResearchRuntime(self.r)._sufficiency([{}],{}, {},[{}]),'conflicting')
    def test_nested_email_is_preserved_without_default_count_inflation(self):
        outer=EmailMessage();outer['Subject']='Outer';outer.set_content('outer');inner=EmailMessage();inner['Subject']='Inner';inner.set_content('nested body');outer.add_attachment(inner)
        (self.root/'nested.eml').write_bytes(outer.as_bytes());services=build_mail_services(self.storage);result=_import({'id':'nested','owner_id':'owner','options':{'package_id':self.pid}},services)
        self.assertEqual(self.r.overview(self.pid,'owner')['counts']['logical_messages'],1)
        from services.mail.foundation import content
        key=self.r.list_messages(self.pid,'owner')['items'][0]['message_key']
        nested=[p for p in content(self.r,key,'owner',self.pid)['parts'] if p['part_role']=='nested_message']
        self.assertEqual(len(nested),1)
        self.assertIn('nested body',nested[0]['nested']['parts'][0]['text'])
    def test_vector_batch_cached_and_owner_isolated(self):
        from services.mail.semantic import embed_segments_batch,vector_search
        self.ingest(raw_mail(body='semantic content'))
        class Client:
            model='test-model';calls=0
            def embed(self,texts):self.calls+=1;return [[1.0,0.0,0.0] for t in texts]
        client=Client()
        with patch.object(Config,'MAIL_ENABLE_VECTOR_INDEX',True):
            status=embed_segments_batch(self.r,self.pid,'owner',client);self.assertEqual(status['pending'],0);again=embed_segments_batch(self.r,self.pid,'owner',client);self.assertEqual(client.calls,1)
            with self.assertRaises(ValueError):vector_search(self.r,self.pid,'other','semantic',client=client)
            values,status=vector_search(self.r,self.pid,'owner','semantic',client=client);self.assertEqual(len(values),2)
    def test_api_routes_owner_and_feature_off(self):
        # Route registration uses only mail endpoints and must not overwrite existing app routes.
        class App:
            def __init__(self):self.paths=[]
            def route(self,path,**kwargs):
                self.paths.append(path);return lambda f:f
        app=App();register_mail_routes(app,build_mail_services(self.storage));self.assertTrue(all(p.startswith('/api/mail/') or p=='/mail-research' for p in app.paths));self.assertEqual(len(app.paths),len(set(app.paths))+2)

    def test_fact_whitespace_recovery_does_not_accept_new_words(self):
        evidence={"evidence_id":"e","message_key":"m","text":"Event A: 3,000\r\nบาท per child."}
        candidate={"fact_type":"amount","fact_key":"grant","subject_ref":"Event A","value_raw":"3,000 บาท","quote":"Event A: 3,000 บาท per child.","status":"confirmed"}
        value=normalize_fact(candidate,evidence)
        self.assertIsNotNone(value);self.assertEqual(value["value_raw"],"3,000\r\nบาท");self.assertEqual(value["status"],"mentioned")
        candidate["quote"]="Event A: 3,000 บาท per adult."
        self.assertIsNone(normalize_fact(candidate,evidence))

    def test_master_switch_does_not_initialize_mail_or_register_routes(self):
        from services.mail.bootstrap import register_mail_module
        class App:pass
        with patch.object(Config,"MAIL_FEATURE_ENABLED",False):
            self.assertIsNone(register_mail_module(App(),None))

    def test_literal_fields_remain_useful_when_model_is_unavailable(self):
        from services.mail.facts import extract_evidence_facts
        self.ingest(raw_mail(body="Scholarship: 2,500 บาท, ไม่เกิน 5 ทุน. Deadline 26 กรกฎาคม 2566."))
        evidence=self.r.search_segments(self.pid,'owner','Scholarship')['items']
        fields=extract_evidence_facts(self.r,self.pid,'owner',evidence,['amount','quantity','date'],lambda *_:None)
        self.assertEqual({f['fact_type'] for f in fields},{'amount','quantity','date'})
        self.assertIn('2023-07-26',[f['value_normalized'] for f in fields if f['fact_type']=='date'])
        quantity=next(f for f in fields if f['fact_type']=='quantity')
        self.assertEqual(quantity['value_normalized']['comparison'],'at_most')

    def test_dedicated_mail_consumer_never_claims_other_work(self):
        from services.mail.worker import claim_mail_job
        other=self.storage.create_job('other',task_type='generate_summary',owner_id='owner')
        mail=self.storage.create_job(self.pid,task_type='mail_research',owner_id='owner')
        with patch.object(Config,'MAIL_FEATURE_ENABLED',True):
            claimed=claim_mail_job(self.storage,'test-mail-worker')
            self.assertEqual(claimed['id'],mail)
            self.assertIsNone(claim_mail_job(self.storage,'test-mail-worker'))
        self.assertEqual(self.storage.get_job(other)['status'],'queued')

    def test_failed_attachment_count_is_not_doubled_by_processing_stages(self):
        row=self.ingest(raw_mail(attachment=True))
        for stage in ['basic_text','ocr']:
            self.r.update_processing(row['message_key'],'2',stage,'failed',error={'message':'failed'})
        self.assertEqual(self.r.coverage(self.pid,'owner')['failed_parts'],1)

    def test_negated_confirmation_cannot_become_a_final_confirmed_fact(self):
        evidence={'evidence_id':'e','message_key':'m','text':'Event A date 2026-10-10 is not confirmed.'}
        candidate={'fact_type':'date','fact_key':'event_date','subject_ref':'Event A','value_raw':'2026-10-10','quote':evidence['text'],'status':'confirmed'}
        self.assertEqual(normalize_fact(candidate,evidence)['status'],'mentioned')
