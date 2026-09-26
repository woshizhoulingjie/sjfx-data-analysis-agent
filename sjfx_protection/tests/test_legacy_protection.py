import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI

from sjfx_protection.store import Store,ProtectionError,digest,uid
from sjfx_protection.scanner import stable_hash,inspect_file,safe_mail_preview
from sjfx_protection.worker import inventory,verify_inventory
from sjfx_protection.integration import install


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'state')
        self.key=Ed25519PrivateKey.generate()
        self.pub=base64.b64encode(self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()
        self.token=self.store.enrollment('alice','u1')
        self.device=self.store.enroll(self.token,self.pub,'test')

    def tearDown(self):self.temp.cleanup()

    def proof(self,prefix):
        stamp=str(int(time.time()));nonce=uid()
        message='\n'.join([prefix,self.device,stamp,nonce]).encode()
        return {'x-sjfx-device':self.device,'x-sjfx-time':stamp,'x-sjfx-nonce':nonce,
                'x-sjfx-proof':base64.b64encode(self.key.sign(message)).decode()}

    def test_enrollment_single_use_and_device_proof_replay(self):
        with self.assertRaises(ProtectionError):self.store.enroll(self.token,self.pub,'duplicate')
        headers=self.proof('test')
        self.assertEqual(self.store.verify(headers,'test','alice')['id'],self.device)
        with self.assertRaises(ProtectionError):self.store.verify(headers,'test','alice')
        with self.assertRaises(ProtectionError):self.store.verify(self.proof('test'),'test','bob')
        with self.assertRaises(ProtectionError):self.store.verify(self.proof('test'),'changed','alice')

    def test_unhealthy_and_revoked_devices_denied(self):
        with self.assertRaises(ProtectionError):self.store.verify(self.proof('t'),'t','alice',True)
        self.store.heartbeat(self.device,{'healthy':True,'watch_roots':1})
        self.store.verify(self.proof('t'),'t','alice',True)
        with self.store.db(True) as db:db.execute('UPDATE devices SET revoked=?',(time.time(),))
        with self.assertRaises(ProtectionError):self.store.verify(self.proof('t'),'t')

    def prepare(self):
        folder=self.root/'source';folder.mkdir()
        (folder/'original.txt').write_text('confidential data',encoding='utf-8')
        cid=self.store.create_collection('alice',str(folder))
        inventory(self.store,cid,[str(folder)])
        self.assertEqual(self.store.collection('alice',cid)['state'],'ready',self.store.collection('alice',cid))
        file=self.store.list_rows('files','alice',collection=cid)[0]
        return folder,cid,file

    def test_inventory_bind_events_isolation_and_restart(self):
        folder,cid,file=self.prepare()
        self.assertEqual(self.store.collection('alice',cid)['state'],'ready')
        with self.assertRaises(ProtectionError):self.store.collection('bob',cid)
        dev={'id':self.device,'owner':'alice'}
        rec={'file_id':file['id'],'sha256':file['sha256'],'local_path':'D:\\other\\renamed.txt','local_identity':'vol:123'}
        bid=self.store.bind(dev,[rec])[0]['binding_id']
        self.assertEqual(self.store.require_root('alice',self.device,str(folder)),cid)
        event={'event_id':uid(),'binding_id':bid,'action':'file.modified','sha256':'b'*64,'occurred':time.time()}
        self.store.ingest(dev,[event]);self.store.ingest(dev,[event])
        self.assertEqual(len([e for e in self.store.list_rows('events','alice') if e['action']=='file.modified']),1)
        restarted=Store(self.root/'state')
        self.assertEqual(restarted.list_rows('bindings','alice')[0]['sha256'],'b'*64)
        self.assertTrue(restarted.verify_chain()['ok'])
        bad={**rec,'sha256':'c'*64}
        with self.assertRaises(ProtectionError):self.store.bind(dev,[bad])

    def test_action_lifecycle_and_no_machine_wide_actions(self):
        _,_,file=self.prepare();dev={'id':self.device,'owner':'alice'}
        bid=self.store.bind(dev,[{'file_id':file['id'],'sha256':file['sha256'],'local_path':'D:\\a','local_identity':'v:1'}])[0]['binding_id']
        with self.assertRaises(ProtectionError):self.store.command('alice',bid,'block_removable','test')
        aid=self.store.command('alice',bid,'quarantine','suspected malware')
        self.store.action_result(dev,aid,{'status':'failed','detail':'access denied'})
        self.assertEqual(self.store.list_rows('actions','alice')[0]['state'],'failed')
        with self.assertRaises(ProtectionError):self.store.command('alice',bid,'restore','undo',aid)

    def test_unknown_scan_is_not_clean_and_preview_never_html(self):
        p=self.root/'test.eml'
        p.write_text('Subject: Test\nMIME-Version: 1.0\nContent-Type: text/html; charset=utf-8\n\n<a href="https://evil.example/login">https://bank.example/login</a><script>alert(1)</script>',encoding='utf-8')
        result=inspect_file(p,clam='nonexistent-scanner-for-test')
        self.assertEqual(result['risk'],'unchecked')
        self.assertEqual(result['engines']['clamav'],'error')
        self.assertFalse(safe_mail_preview(p)['html_rendered'])
        self.assertNotIn('<script>',safe_mail_preview(p)['text'])

    def test_outside_allowed_root_fails_without_inventory(self):
        folder=self.root/'outside';folder.mkdir();(folder/'secret').write_text('secret')
        cid=self.store.create_collection('alice',str(folder))
        inventory(self.store,cid,[str(self.root/'allowed')])
        self.assertEqual(self.store.collection('alice',cid)['state'],'failed')
        self.assertEqual(self.store.list_rows('files','alice',collection=cid),[])

    def test_download_proof_is_owner_device_bound_and_single_use(self):
        self.store.heartbeat(self.device,{'healthy':True})
        self.store.bind_download('alice',self.device,'中文报告.docx','ticket-one',120)
        with self.assertRaises(ProtectionError):self.store.consume_download('bob','中文报告.docx','ticket-one')
        with self.assertRaises(ProtectionError):self.store.consume_download('alice','different.docx','ticket-one')
        self.assertEqual(self.store.consume_download('alice','中文报告.docx','ticket-one'),self.device)
        with self.assertRaises(ProtectionError):self.store.consume_download('alice','中文报告.docx','ticket-one')
        self.store.bind_download('alice',self.device,'report','ticket-two',120)
        self.store.heartbeat(self.device,{'healthy':False})
        with self.assertRaises(ProtectionError):self.store.consume_download('alice','report','ticket-two')

    def test_copy_requires_matching_version_and_does_not_move_original(self):
        _,_,file=self.prepare();dev={'id':self.device,'owner':'alice'}
        bid=self.store.bind(dev,[{'file_id':file['id'],'sha256':file['sha256'],'local_path':'D:\\original','local_identity':'v:1'}])[0]['binding_id']
        data={'source_binding':bid,'sha256':file['sha256'],'local_path':'D:\\other','local_identity':'v:2'}
        copy=self.store.related_copy(dev,data)
        self.assertEqual(copy,self.store.related_copy(dev,data))
        with self.assertRaises(ProtectionError):self.store.related_copy(dev,{**data,'sha256':'0'*64})
        self.store.ingest(dev,[{'event_id':uid(),'binding_id':bid,'action':'file.read','path':'D:\\untrusted','process':'reader.exe'}])
        byid={b['id']:b for b in self.store.list_rows('bindings','alice')}
        self.assertEqual(byid[bid]['local_path'],'D:\\original')
        self.assertEqual(byid[copy['binding_id']]['local_path'],'D:\\other')

    def test_dangerous_binding_queues_version_specific_quarantine(self):
        _,_,file=self.prepare();dev={'id':self.device,'owner':'alice'}
        with self.store.db(True) as db:db.execute("UPDATE files SET risk='dangerous' WHERE id=?",(file['id'],))
        rec={'file_id':file['id'],'sha256':file['sha256'],'local_path':'D:\\dangerous','local_identity':'v:1'}
        self.store.bind(dev,[rec]);self.store.bind(dev,[rec])
        actions=self.store.pending_actions(self.device)
        self.assertEqual(len(actions),1);self.assertEqual(actions[0]['kind'],'quarantine')
        self.assertEqual(actions[0]['expected_sha256'],file['sha256'])

    def test_preview_rejects_changed_version(self):
        p=self.root/'mail.eml';p.write_text('Subject: original\n\nbody',encoding='utf-8')
        original=stable_hash(p)['sha256'];p.write_text('Subject: replaced\n\nnew body',encoding='utf-8')
        with self.assertRaises(ValueError):safe_mail_preview(p,original)

    def test_changed_local_version_no_longer_satisfies_import_gate(self):
        folder,cid,file=self.prepare();dev={'id':self.device,'owner':'alice'}
        bid=self.store.bind(dev,[{'file_id':file['id'],'sha256':file['sha256'],'local_path':'D:\\a','local_identity':'v:1'}])[0]['binding_id']
        self.store.ingest(dev,[{'event_id':uid(),'binding_id':bid,'action':'file.modified','sha256':'f'*64}])
        with self.assertRaises(ProtectionError):self.store.require_root('alice',self.device,str(folder))

    def test_server_version_recheck_detects_change_and_new_file(self):
        folder,cid,file=self.prepare()
        verify_inventory(self.store,cid)
        self.assertEqual(self.store.collection('alice',cid)['state'],'ready')
        (folder/'original.txt').write_text('changed')
        verify_inventory(self.store,cid)
        self.assertEqual(self.store.collection('alice',cid)['state'],'failed')
        next_id=self.store.create_collection('alice',str(folder));inventory(self.store,next_id,[str(folder)])
        (folder/'new.txt').write_text('new')
        verify_inventory(self.store,next_id)
        self.assertEqual(self.store.collection('alice',next_id)['state'],'failed')

    def test_review_is_version_specific_and_never_overrides_malware(self):
        _,cid,file=self.prepare()
        with self.store.db(True) as db:db.execute("UPDATE files SET risk='unchecked' WHERE id=?",(file['id'],))
        self.assertTrue(self.store.blocked_file(cid))
        with self.store.db(True) as db:db.execute("UPDATE files SET risk='review' WHERE id=?",(file['id'],))
        self.assertTrue(self.store.blocked_file(cid))
        self.store.approve_review('alice','admin',file['id'],file['sha256'],'已核对来源及目标地址')
        self.assertFalse(self.store.blocked_file(cid))
        with self.store.db(True) as db:db.execute("UPDATE files SET risk='dangerous' WHERE id=?",(file['id'],))
        self.assertTrue(self.store.blocked_file(cid))
        with self.assertRaises(ProtectionError):self.store.approve_review('alice','admin',file['id'],file['sha256'],'放行')


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_enforcement_preserves_other_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=FastAPI()
            @app.get('/api/existing')
            def existing():return {'unchanged':True}
            p=install(app,state_dir=Path(tmp)/'state',allowed_roots=[tmp],mode='audit',
                session_resolver=lambda s:{'owner_id':s,'user_id':s,'role':'admin'} if s in {'alice','bob'} else None)
            p.allow_enforcement=True
            with p.store.db(True) as db:db.execute('INSERT INTO settings VALUES(?,?)',('enforce:alice','1'))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                client.cookies.set('sjfx_session','alice')
                self.assertEqual((await client.get('/api/existing')).status_code,403)
                self.assertEqual((await client.get('/protection/api/status')).json()['mode'],'enforce')
                client.cookies.set('sjfx_session','bob')
                self.assertEqual((await client.get('/api/existing')).status_code,200)
                self.assertEqual((await client.get('/protection/api/status')).json()['mode'],'audit')
                self.assertEqual((await client.post('/protection/api/policy',json={'enabled':True},headers={'X-SJFX-Protection':'1'})).status_code,403)
                p.allow_enforcement=False
                self.assertEqual((await client.post('/protection/api/policy',json={'enabled':True},headers={'X-SJFX-Protection':'1'})).status_code,503)

    async def test_monitor_mode_links_business_event_without_restricting_unbound_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'source';root.mkdir();(root/'file.txt').write_text('data')
            app=FastAPI()
            p=install(app,state_dir=Path(tmp)/'state',allowed_roots=[root],mode='audit',
                session_resolver=lambda s:{'owner_id':'alice','user_id':'u1'})
            cid=p.store.create_collection('alice',str(root));inventory(p.store,cid,[str(root)])
            @app.post('/api/read-document')
            def read_document():p.guard_root(str(root));return {'ok':True,'path':'file.txt'}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',cookies={'sjfx_session':'valid'}) as client:
                self.assertEqual((await client.post('/api/read-document',json={'path':'file.txt'})).status_code,200)
            records=[e for e in p.store.list_rows('events','alice') if e['action']=='system.request']
            self.assertEqual(len(records),1)
            self.assertTrue(records[0]['file_id'])
            self.assertEqual(json.loads(records[0]['payload'])['payload']['collection_id'],cid)

    async def test_disabled_is_transparent_and_has_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=FastAPI()
            @app.get('/api/existing')
            def existing():return {'unchanged':True}
            install(app,state_dir=Path(tmp)/'not-created',allowed_roots=[tmp],session_resolver=lambda s:None,mode='off')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                r=await client.get('/api/existing');self.assertEqual(r.json(),{'unchanged':True})
                self.assertFalse((Path(tmp)/'not-created').exists())

    async def test_enforced_api_denies_plain_cookie_and_foreign_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=FastAPI()
            @app.get('/api/existing')
            def existing():return {'unchanged':True}
            p=install(app,state_dir=Path(tmp)/'state',allowed_roots=[tmp],
                session_resolver=lambda s:{'owner_id':'alice','user_id':'u1'} if s=='valid' else None,mode='enforce')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',cookies={'sjfx_session':'valid'}) as client:
                self.assertEqual((await client.get('/api/existing')).status_code,403)
                r=await client.post('/protection/api/enrollments',json={},headers={'Origin':'https://evil.example','X-SJFX-Protection':'1'})
                self.assertEqual(r.status_code,403)
                key=Ed25519PrivateKey.generate()
                pub=base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()
                did=p.store.enroll(p.store.enrollment('alice','u1'),pub,'test')
                p.store.heartbeat(did,{'healthy':True})
                stamp=str(int(time.time()));nonce=uid()
                msg='\n'.join(['sjfx-browser-v1',digest('valid'),'GET','/api/existing',did,stamp,nonce])
                headers={'X-SJFX-Device':did,'X-SJFX-Time':stamp,'X-SJFX-Nonce':nonce,'X-SJFX-Proof':base64.b64encode(key.sign(msg.encode())).decode()}
                self.assertEqual((await client.get('/api/existing',headers=headers)).status_code,200)
                self.assertEqual((await client.get('/api/existing',headers=headers)).status_code,403)


if __name__=='__main__':unittest.main()
