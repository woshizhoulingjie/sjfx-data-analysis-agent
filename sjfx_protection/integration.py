"""ASGI boundary: disabled mode is a pass-through, with no database initialization."""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response, FileResponse

from .scanner import safe_mail_preview
from .store import ProtectionError, Store, digest


current = contextvars.ContextVar("sjfx_protection_context", default=None)


class Protection:
    def __init__(self, state_dir, allowed_roots, session_resolver, resource_root=None, mode=None):
        self.state_dir = str(state_dir)
        self.allowed_roots = [str(p) for p in allowed_roots]
        self.session_resolver = session_resolver
        self.resource_root = resource_root
        self.mode = mode or os.getenv("SJFX_PROTECTION_MODE", "off")
        if self.mode not in {"off", "audit", "enforce"}:
            raise ValueError("SJFX_PROTECTION_MODE 必须为 off、audit 或 enforce")
        self._store, self.lock = None, threading.Lock()
        self.workers = {}
        self.audit_reads = {}
        self.allow_enforcement=os.getenv('SJFX_PROTECTION_ALLOW_ENFORCEMENT')=='1'

    @property
    def store(self):
        with self.lock:
            if self._store is None:
                self._store = Store(self.state_dir)
            return self._store

    def user(self, headers):
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
            sid = cookie["sjfx_session"].value if "sjfx_session" in cookie else ""
        except Exception:
            sid = ""
        user = self.session_resolver(sid) if sid else None
        if not user or user.get("must_change_pw"):
            raise ProtectionError("请先登录并完成首次密码修改", 401, "login_required")
        return dict(user), digest(sid)

    def require_on(self):
        if self.mode == "off":
            raise ProtectionError("文件保护尚未启用", 503, "protection_disabled")

    def enforced(self, owner):
        if self.mode=='off':return False
        if self.mode=='enforce':return True
        if not self.allow_enforcement:return False
        with self.store.db() as db:
            row=db.execute('SELECT value FROM settings WHERE key=?',('enforce:'+owner,)).fetchone()
        return bool(row and row[0]=='1')

    def guard_root(self, root):
        ctx = current.get()
        if ctx is None:
            return
        # Associate existing business operations with registered data even in monitor-only mode.
        if not ctx.get('enforced',False):
            try:
                normalized=os.path.abspath(os.path.expanduser(root))
                with self.store.db() as db:
                    row=db.execute('SELECT id FROM collections WHERE owner=? AND root=? ORDER BY created DESC LIMIT 1',
                                   (ctx['owner'],normalized)).fetchone()
                if row:ctx.update(collection_id=row[0],server_root=normalized)
            except Exception:
                import logging
                logging.getLogger(__name__).exception('文件保护关联查询失败，原业务继续运行')
            return
        collection = self.store.require_root(ctx["owner"], ctx["device"], root)
        ctx.update(collection_id=collection,server_root=os.path.abspath(os.path.expanduser(root)))
        with self.store.db() as db:
            verified=db.execute('SELECT value FROM settings WHERE key=?',('verified:'+collection,)).fetchone()
        if not verified or time.time()-float(verified[0])>30:
            with self.lock:
                self.workers={k:v for k,v in self.workers.items() if v.poll() is None}
                if collection not in self.workers and len(self.workers)<2:
                    with open(Path(self.state_dir)/(collection+'.log'),'ab') as log:
                        self.workers[collection]=subprocess.Popen([sys.executable,'-m','sjfx_protection.worker',
                            '--state',self.state_dir,'--collection',collection,'--verify'],
                            cwd=str(Path(__file__).resolve().parent.parent),stdin=subprocess.DEVNULL,stdout=log,stderr=log)
            raise ProtectionError('正在核对服务器文件当前版本，请稍后重试；如有变化请重新登记',423,'version_check_pending')
        if self.store.blocked_file(collection):
            raise ProtectionError("资料含危险、待复核或未完成安全检查的文件，请先在文件保护页处理", 423, "dangerous_file")

    def bind_download(self, filename, ticket, ttl):
        ctx=current.get()
        if ctx and ctx.get('enforced',False):
            self.store.bind_download(ctx['owner'],ctx['device'],filename,ticket,ttl)

    def start_inventory(self, owner, root):
        if not isinstance(root, str) or not Path(root).is_absolute() or '\x00' in root:
            raise ProtectionError("必须提供服务器绝对路径")
        # No mounted-filesystem stat in the request. Authoritative realpath validation is in worker.
        root = os.path.abspath(os.path.expanduser(root))
        if not any(Path(root).is_relative_to(Path(a).absolute()) for a in self.allowed_roots):
            raise ProtectionError("目录不在系统允许范围内", 403)
        with self.lock:
            self.workers = {k:v for k,v in self.workers.items() if v.poll() is None}
            if len(self.workers) >= 2:
                raise ProtectionError("已有清单任务运行，请稍后重试", 429)
        # Store has its own lazy initialization lock; do not acquire it recursively.
        store = self.store
        with self.lock:
            if len(self.workers) >= 2:
                raise ProtectionError("已有清单任务运行，请稍后重试", 429)
            cid = store.create_collection(owner, root)
            args = [sys.executable, "-m", "sjfx_protection.worker", "--state", self.state_dir,
                    "--collection", cid]
            for allowed in self.allowed_roots:
                args += ["--allowed-root", allowed]
            logfile = open(Path(self.state_dir) / (cid + ".log"), "ab")
            try:
                proc = subprocess.Popen(args, cwd=str(Path(__file__).resolve().parent.parent),
                                        stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile)
                self.workers[cid] = proc
            except Exception:
                with store.db(True) as db:
                    db.execute("UPDATE collections SET state='failed',error='清单进程启动失败' WHERE id=?", (cid,))
                raise
            finally:
                logfile.close()
        return cid


class ProtectionMiddleware:
    def __init__(self, app, protection):
        self.app, self.p = app, protection

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.p.mode == "off":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        business = (path.startswith("/api/") or path.startswith("/outputs/")) and not path.startswith(("/api/auth/", "/api/admin/"))
        if not business:
            return await self.app(scope, receive, send)
        headers = {k.decode('latin1').lower():v.decode('latin1') for k,v in scope["headers"]}
        actor, device, session_tag = None, "", ""
        enforced=self.p.mode=='enforce'
        try:
            actor, session_tag = await run_in_threadpool(self.p.user, headers)
            enforced=await run_in_threadpool(self.p.enforced,actor['owner_id'])
            if enforced:
                target = path + ("?" + scope["query_string"].decode('ascii') if scope.get("query_string") else "")
                prefix = "\n".join(["sjfx-browser-v1", session_tag, scope["method"], target])
                if path.startswith('/outputs/') and not headers.get('x-sjfx-proof'):
                    ticket=parse_qs(scope.get('query_string',b'').decode('ascii')).get('ticket',[''])[0]
                    device=await run_in_threadpool(self.p.store.consume_download, actor['owner_id'],path.removeprefix('/outputs/'),ticket)
                else:
                    dev = await run_in_threadpool(self.p.store.verify, headers, prefix, actor["owner_id"], True)
                    device = dev["id"]
        except ProtectionError as exc:
            if enforced:
                response = JSONResponse({"ok":False,"error":str(exc),"code":exc.code,"protection_url":"/protection"},status_code=exc.status)
                return await response(scope,receive,send)
        status, request_parts, response_parts = [500], [], []
        sizes = [0,0]
        async def observed_receive():
            message = await receive()
            if message["type"] == "http.request":
                raw = message.get("body", b"")
                sizes[0] += len(raw)
                if sizes[0] <= 65536:
                    request_parts.append(raw)
            return message
        async def observed_send(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            elif message["type"] == "http.response.body":
                raw = message.get("body",b"")
                sizes[1] += len(raw)
                if sizes[1] <= 65536:
                    response_parts.append(raw)
            await send(message)
        audit_context={"owner":actor["owner_id"],"device":device,'enforced':enforced} if actor else None
        token = current.set(audit_context)
        started=time.monotonic()
        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            current.reset(token)
            if actor:
                payload = {"method":scope["method"],"route":path,"status":status[0],
                           'duration_ms':round((time.monotonic()-started)*1000),'user_id':actor['user_id']}
                payload.update({k:audit_context[k] for k in ('collection_id','server_root') if k in audit_context})
                query=parse_qs(scope.get('query_string',b'')[:65536].decode('utf-8','replace'))
                for key in ('scan_id','package_id','job_id','file_id','node_path','path','artifact_id'):
                    if query.get(key):payload[key]=query[key][0][:4096]
                # Whitelist identifiers only; never log tokens, questions, mail text or entire bodies.
                for parts,size in ((request_parts,sizes[0]),(response_parts,sizes[1])):
                    if size > 65536:
                        continue
                    try:
                        value = json.loads(b''.join(parts))
                        if isinstance(value,dict):
                            for key in ("scan_id","package_id","job_id","file_id","node_path","path","artifact_id"):
                                if isinstance(value.get(key),str):
                                    payload[key] = value[key][:4096]
                    except (ValueError,UnicodeError):
                        pass
                try:
                    def record_request():
                        file_id=''
                        candidate=payload.get('node_path') or payload.get('path')
                        if candidate and payload.get('collection_id'):
                            candidate=os.path.abspath(os.path.join(payload['server_root'],candidate))
                            with self.p.store.db() as db:
                                row=db.execute('SELECT id FROM files WHERE collection=? AND path=?',
                                               (payload['collection_id'],candidate)).fetchone()
                            if row:file_id=row[0]
                        self.p.store.event(actor['owner_id'],'system.request',device=device,file_id=file_id,payload=payload)
                    # Status polling is coalesced, while every mutation and file operation remains recorded.
                    key=(actor['owner_id'],path,status[0])
                    now=time.monotonic()
                    skip=False
                    if scope['method']=='GET' and not any(x in path for x in ('download','export','file','document','content','preview')):
                        with self.p.lock:
                            skip=now-self.p.audit_reads.get(key,-100)<30
                            if not skip:
                                if len(self.p.audit_reads)>4000:self.p.audit_reads.clear()
                                self.p.audit_reads[key]=now
                    if not skip:await run_in_threadpool(record_request)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("文件保护审计写入失败")


def install(app, *, state_dir, allowed_roots, session_resolver, resource_root=None, mode=None):
    protection = Protection(state_dir,allowed_roots,session_resolver,resource_root,mode)
    app.add_middleware(ProtectionMiddleware, protection=protection)
    async def protection_error(request,exc):
        return JSONResponse({'ok':False,'error':str(exc),'code':exc.code,'protection_url':'/protection'},status_code=exc.status)
    app.add_exception_handler(ProtectionError,protection_error)

    async def handle(request: Request):
        try:
            path = request.url.path
            if path == "/protection/bridge.js":
                return Response((Path(__file__).parent/'web'/'bridge.js').read_text(encoding='utf-8'),media_type='application/javascript')
            if path == '/protection/download':
                package=Path(__file__).parent/'downloads'/'sjfx-assistant-windows.zip'
                if not package.is_file():
                    raise ProtectionError('管理员尚未发布 Windows 助手安装包',503)
                return FileResponse(package,filename='sjfx-assistant-windows.zip',media_type='application/zip')
            if path == "/protection" and request.method == "GET":
                return HTMLResponse((Path(__file__).parent/'web'/'index.html').read_text(encoding='utf-8'),
                                    headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
            if path == "/protection/api/status":
                mode=protection.mode
                if mode=='audit':
                    try:
                        actor,_=await run_in_threadpool(protection.user,request.headers)
                        if await run_in_threadpool(protection.enforced,actor['owner_id']):mode='enforce'
                    except ProtectionError:pass
                return JSONResponse({"ok":True,"mode":mode,"global_mode":protection.mode,
                    'enforcement_available':protection.allow_enforcement,
                    "local_url":"http://127.0.0.1:18765"},headers={'Cache-Control':'no-store'})
            protection.require_on()
            # Prevent cross-site writes authenticated by browser cookies.
            origin = request.headers.get("origin")
            if not path.startswith('/agent/v2/') and request.method != 'GET':
                if origin and urlsplit(origin).netloc != request.headers.get('host'):
                    raise ProtectionError("跨站请求被拒绝",403)
                if request.headers.get('x-sjfx-protection') != '1':
                    raise ProtectionError("缺少请求校验头",403)
            raw = b''
            if request.method != 'GET':
                chunks=[]
                size=0
                async for part in request.stream():
                    size+=len(part)
                    if size>2*1024*1024:
                        raise ProtectionError("请求过大",413)
                    chunks.append(part)
                raw=b''.join(chunks)
            data=json.loads(raw) if raw else {}
            if not isinstance(data,dict):
                raise ProtectionError("请求必须为对象")
            result=await run_in_threadpool(dispatch,request,raw,data)
            return JSONResponse({"ok":True,**result},headers={"Cache-Control":"no-store"})
        except ProtectionError as exc:
            return JSONResponse({"ok":False,"error":str(exc),"code":exc.code},status_code=exc.status)
        except (ValueError,TypeError,KeyError):
            return JSONResponse({"ok":False,"error":"请求字段或格式无效"},status_code=400)

    def dispatch(request,raw,data):
        path,method=request.url.path,request.method
        store=protection.store
        if path == '/agent/v2/enroll' and method=='POST':
            return {"device_id":store.enroll(str(data.get('token','')),str(data.get('public_key','')),str(data.get('name','')))}
        if path.startswith('/agent/v2/'):
            if method!='POST':
                raise ProtectionError('该接口仅支持 POST',405)
            prefix='\n'.join(['sjfx-agent-v2',method,path,digest(raw)])
            device=store.verify(request.headers,prefix)
            if path.endswith('/heartbeat'):
                store.heartbeat(device['id'],data)
                return {'server_time':time.time(),'actions':store.pending_actions(device['id'])}
            if path.endswith('/manifest'):
                cid=str(data.get('collection_id',''))
                collection=store.collection(device['owner'],cid)
                return {'collection':collection,'files':store.list_rows('files',device['owner'],collection=cid,
                            limit=500,offset=int(data.get('offset',0)))}
            if path.endswith('/bind'):
                return {'bindings':store.bind(device,data.get('files',[]))}
            if path.endswith('/related-copy'):
                return store.related_copy(device,data)
            if path.endswith('/events'):
                return {'accepted':store.ingest(device,data.get('events',[]))}
            if path.endswith('/action-result'):
                store.action_result(device,str(data.get('action_id','')),data.get('result',{}))
                return {}
            if path.endswith('/bindings'):
                return {'bindings':store.list_rows('bindings',device['owner'],device=device['id'],
                                                   limit=500,offset=int(data.get('offset',0)))}
            raise ProtectionError('接口不存在',404)
        user,session_tag=protection.user(request.headers)
        owner=user['owner_id']
        if path.endswith('/policy') and method=='POST':
            if not protection.allow_enforcement:
                raise ProtectionError('本次只开放监视与文件处置，强制接入功能暂未开放',503)
            if user.get('role')!='admin':raise ProtectionError('仅管理员可修改强制保护策略',403)
            if protection.mode=='enforce':raise ProtectionError('服务器已启用全局强制保护，请由服务器管理员调整',409)
            enabled=data.get('enabled')
            if not isinstance(enabled,bool):raise ProtectionError('策略值无效')
            if enabled:
                prefix='\n'.join(['sjfx-browser-v1',session_tag,method,path])
                store.verify(request.headers,prefix,owner,True)
            with store.db(True) as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('enforce:'+owner,'1' if enabled else '0'))
                store._event(db,owner,'policy.changed',payload={'enforced':enabled,'user_id':user['user_id']})
            return {'enforced':enabled}
        if path.endswith('/session'):
            return {'session_tag':session_tag,'owner':owner,'name':user.get('display_name',''),'admin':user.get('role')=='admin'}
        if path.endswith('/enrollments') and method=='POST':
            return {'token':store.enrollment(owner,user['user_id']),'expires_in':600}
        if path.endswith('/collections'):
            if method=='GET':
                return {'collections':store.list_rows('collections',owner)}
            return {'collection_id':protection.start_inventory(owner,data.get('root',''))}
        if path.endswith('/cancel-collection') and method=='POST':
            cid=str(data.get('collection_id',''));collection=store.collection(owner,cid)
            if collection['state'] not in {'queued','running'}:raise ProtectionError('该清单已结束',409)
            with store.db(True) as db:
                db.execute("UPDATE collections SET state='cancelled',updated=? WHERE id=?",(time.time(),cid))
                store._event(db,owner,'collection.cancelled',payload={'collection_id':cid})
            with protection.lock:
                proc=protection.workers.get(cid)
                if proc and proc.poll() is None:proc.terminate()
            return {}
        if path.endswith('/files'):
            cid=request.query_params.get('collection_id','')
            store.collection(owner,cid)
            return {'files':store.list_rows('files',owner,collection=cid,limit=500,
                                          offset=int(request.query_params.get('offset',0)))}
        if path.endswith('/devices'):
            return {'devices':store.list_rows('devices',owner)}
        if path.endswith('/revoke') and method=='POST':
            with store.db(True) as db:
                db.execute('UPDATE devices SET revoked=? WHERE id=? AND owner=?',(time.time(),data.get('device_id'),owner))
                store._event(db,owner,'device.revoked',device=str(data.get('device_id','')))
            return {}
        if path.endswith('/bindings'):
            return {'bindings':store.list_rows('bindings',owner,limit=500,offset=int(request.query_params.get('offset',0)))}
        if path.endswith('/events'):
            return {'events':store.list_rows('events',owner,file_id=request.query_params.get('file_id'),
                                             offset=int(request.query_params.get('offset',0)))}
        if path.endswith('/actions'):
            if method=='GET':
                return {'actions':store.list_rows('actions',owner)}
            return {'action_id':store.command(owner,str(data.get('binding_id','')),str(data.get('kind','')),
                        str(data.get('reason','')),str(data.get('restore_action_id','')))}
        if path.endswith('/preview'):
            with store.db() as db:
                row=db.execute('SELECT * FROM files WHERE id=? AND owner=?',(request.query_params.get('file_id'),owner)).fetchone()
            if not row or not row['path'].lower().endswith('.eml'):
                raise ProtectionError('仅支持已登记邮件的纯文本安全预览',404)
            if Path(row['path']).stat().st_size>20*1024*1024:
                raise ProtectionError('邮件超过预览大小限制')
            return {'preview':safe_mail_preview(row['path'],row['sha256'])}
        if path.endswith('/review') and method=='POST':
            if user.get('role')!='admin':raise ProtectionError('仅管理员可复核可疑文件',403)
            store.approve_review(owner,user['user_id'],str(data.get('file_id','')),str(data.get('sha256','')),str(data.get('reason','')))
            return {}
        if path.endswith('/verify-ledger'):
            if user.get('role')!='admin':
                raise ProtectionError('仅管理员可校验审计链',403)
            return store.verify_chain()
        raise ProtectionError('接口不存在',404)

    for route in ('/protection','/protection/bridge.js','/protection/download','/protection/api/{operation}','/agent/v2/{operation}'):
        app.add_api_route(route,handle,methods=['GET','POST'],include_in_schema=False)
    return protection
