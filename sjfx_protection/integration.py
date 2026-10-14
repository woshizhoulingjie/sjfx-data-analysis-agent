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
from starlette.responses import HTMLResponse, JSONResponse, Response, FileResponse, RedirectResponse

from .scanner import safe_mail_preview
from .store import ProtectionError, Store, digest
from .access import authorize, management_owner, setup, enroll, revoke_self


current = contextvars.ContextVar("sjfx_protection_context", default=None)


class Protection:
    def __init__(self, state_dir, allowed_roots, session_resolver, resource_root=None, mode=None,
                 authorize_root=None, authorize_owner=None, resource_resolver=None):
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
        self.authorize_root, self.authorize_owner = authorize_root, authorize_owner
        self.resource_resolver = resource_resolver
        self._package = None
        self._watch_proc = None

    def ensure_executor(self):
        if os.getenv('SJFX_DESKTOP_MANAGED_PROTECTION')=='1':return
        if not self.enabled and not os.getenv('SJFX_LOCAL_AGENT_TOKEN_FILE'):
            if self._watch_proc is not None and self._watch_proc.poll() is None:
                self._watch_proc.terminate()
            return
        # Only our own process handle is restarted; business workers are independent.
        with self.lock:
            if self._watch_proc is not None and self._watch_proc.poll() is None:return
            if not (Path(self.state_dir)/'protection.db').is_file():return
            args=[sys.executable,'-m','sjfx_protection.watch','--state',self.state_dir,'--packages']
            for root in self.allowed_roots:args.extend(['--allowed-root',root])
            kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            with (Path(self.state_dir)/'package-watch.log').open('ab') as log:
                self._watch_proc=subprocess.Popen(args,cwd=str(Path(__file__).resolve().parent.parent),
                    stdin=subprocess.DEVNULL,stdout=log,stderr=log,**kwargs)

    @property
    def enabled(self):
        from .policy import protection_enabled
        return protection_enabled(self.state_dir)

    @property
    def package(self):
        if self._package is None:
            from .registration import PackageProtection
            self._package = PackageProtection(self.store,self.allowed_roots,
                authorize_root=self.authorize_root,authorize_owner=self.authorize_owner)
        return self._package

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
        return self.enabled

    def guard_root(self, root):
        if not self.enabled:return
        from .business import metadata_projection
        if metadata_projection.get():return
        ctx = current.get()
        if self.enabled:
            from .business import context,require_root
            ctx=context()
            if not ctx or not ctx.get('owner'):
                raise ProtectionError('内容使用缺少会话或任务核验信息',423,'protection_context_required')
            fresh=require_root(ctx['owner'],str(root),ctx.get('operation','read'),ctx)
            if not ctx.get('package_contexts'):ctx.update(fresh)
            return
        if ctx is None:
            return
        # Associate existing business operations with registered data even in monitor-only mode.
        if not ctx.get('enforced',False):
            try:
                normalized=os.path.abspath(os.path.expanduser(root))
                with self.store.db() as db:
                    row=db.execute('SELECT id,state FROM collections WHERE owner=? AND root=? ORDER BY created DESC LIMIT 1',
                                   (ctx['owner'],normalized)).fetchone()
                if row:
                    ctx.update(collection_id=row[0],server_root=normalized)
                    if row['state'] == 'ready':
                        self.schedule_verification(row['id'])
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

    def schedule_verification(self, collection):
        """Audit mode observes registered roots without waiting on NAS reads or denying business work."""
        with self.store.db() as db:
            verified = db.execute('SELECT value FROM settings WHERE key=?', ('verified:' + collection,)).fetchone()
        if verified and time.time() - float(verified[0]) <= 30:
            return
        with self.lock:
            self.workers = {k: v for k, v in self.workers.items() if v.poll() is None}
            if collection in self.workers or len(self.workers) >= 2:
                return
            with open(Path(self.state_dir) / (collection + '.log'), 'ab') as log:
                self.workers[collection] = subprocess.Popen(
                    [sys.executable, '-m', 'sjfx_protection.worker', '--state', self.state_dir,
                     '--collection', collection, '--verify'],
                    cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL, stdout=log, stderr=log)

    def bind_download(self, filename, ticket, ttl):
        ctx=current.get()
        if ctx and ctx.get('enforced',False):
            self.store.bind_download(ctx['owner'],ctx['device'],filename,ticket,ttl)

    def artifact_id(self,owner,filename):
        with self.store.db() as db:
            row=db.execute('SELECT value FROM settings WHERE key=?',('package-artifact:'+owner+':'+filename,)).fetchone()
        if not row:raise ProtectionError('历史产物缺少版本来源，请重新核验并生成',423,'provenance_missing')
        return row[0]

    def issue_download(self,owner,filename,ttl=60):
        from .business import context
        ctx=context() or {}
        return self.package.issue_ticket(owner,self.artifact_id(owner,filename),
            device_id=ctx.get('device_id') or ctx.get('device',''),ttl=ttl,approval_id=ctx.get('approval_id'))

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
        if scope["type"] != "http" or not self.p.enabled:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        business = (path.startswith("/api/") or path.startswith("/outputs/")) and not path.startswith(("/api/auth/", "/api/admin/"))
        if not business:
            return await self.app(scope, receive, send)
        # The controller has an API token, not a browser session. Only this
        # readiness request delegates authentication to app._access_guard,
        # which still rejects missing, invalid or expired API credentials.
        if path == "/api/status" and scope["method"] == "GET":
            return await self.app(scope, receive, send)
        headers = {k.decode('latin1').lower():v.decode('latin1') for k,v in scope["headers"]}
        if self.p.enabled:
            await run_in_threadpool(self.p.ensure_executor)
        actor, device, session_tag = None, "", ""
        enforced=self.p.enabled
        admission_context={};metadata_only=False;metadata_chunks=[];metadata_start=None
        try:
            actor, session_tag = await run_in_threadpool(self.p.user, headers)
            enforced=await run_in_threadpool(self.p.enforced,actor['owner_id'])
            from .metadata import is_metadata_route
            metadata_only=is_metadata_route(path,scope['method'],scope.get('query_string',b''))
            if enforced and headers.get('x-sjfx-proof') and not metadata_only:
                target = path + ("?" + scope["query_string"].decode('ascii') if scope.get("query_string") else "")
                prefix = "\n".join(["sjfx-browser-v1", session_tag, scope["method"], target])
                dev = await run_in_threadpool(self.p.store.verify, headers, prefix, actor["owner_id"], True)
                device = dev["id"]
            if enforced:
                owner=management_owner(actor)
                control=(path.startswith('/api/uploads') or path in {'/api/health','/api/status','/api/general-chat','/api/test-model'}
                         or (path == '/api/file-capabilities' and scope['method'] == 'GET') or metadata_only)
                if not control:
                    messages=[];data={}
                    if scope['method'] in {'POST','PUT','PATCH','DELETE'} and 'application/json' in headers.get('content-type',''):
                        total=0
                        while True:
                            message=await receive();messages.append(message)
                            if message['type']!='http.request':break
                            total+=len(message.get('body',b''))
                            if total>2*1024*1024:raise ProtectionError('保护请求元数据过大',413,'request_too_large')
                            if not message.get('more_body'):break
                        raw=b''.join(m.get('body',b'') for m in messages)
                        try:data=json.loads(raw) if raw else {}
                        except (ValueError,UnicodeError):raise ProtectionError('请求 JSON 格式无效',400,'invalid_request')
                        if not isinstance(data,dict):raise ProtectionError('请求必须为对象',400,'invalid_request')
                        original_receive=receive
                        async def replay_receive():
                            if messages:return messages.pop(0)
                            return await original_receive()
                        receive=replay_receive
                    query={k:v[-1] for k,v in parse_qs(scope.get('query_string',b'').decode('utf-8','replace')).items()}
                    if self.p.resource_resolver is None:
                        raise ProtectionError('业务资料适配器尚未配置',423,'protection_target_required')
                    if path.startswith('/outputs/'):
                        artifact_id=await run_in_threadpool(self.p.artifact_id,owner,path.removeprefix('/outputs/'))
                        ticket=query.get('ticket','')
                        if not ticket:raise ProtectionError('请通过下载入口取得本次版本凭据',423,'download_ticket_required')
                        artifact=await run_in_threadpool(self.p.package.consume_ticket,owner,artifact_id,ticket)
                        admission_context={'owner':owner,'artifact':artifact,'artifact_authorized':True}
                        roots=[]
                    else:
                        roots=await run_in_threadpool(self.p.resource_resolver,owner,path,scope['method'],query,data)
                    if not roots and not admission_context.get('artifact_authorized'):
                        raise ProtectionError('此操作需要选择已登记的数据包',423,'protection_target_required')
                    from .local_source import route_operation
                    operation=route_operation(path,scope['method'],data)
                    if operation in {'delete','rewrite'}:
                        # A single package ID can cover many actual file targets.
                        # Resolve the registered set on the trusted side instead
                        # of letting a short browser target list avoid bulk rules.
                        total=0
                        for root in roots:
                            registration=await run_in_threadpool(self.p.package.resolve,owner,root=root)
                            manifest=await run_in_threadpool(self.p.package.manifest,owner,registration['id'])
                            total+=len(manifest['files'])
                        if total>1:operation='bulk_'+operation
                    from .business import decision_context
                    package_contexts=[]
                    for root in roots:
                        if scope['method']=='POST' and path in {'/api/scan','/api/large-packages','/api/mail/packages'}:
                            if owner!=actor['owner_id']:raise ProtectionError('新导入请切回本人空间',403,'own_view_required')
                            await run_in_threadpool(self.p.package._owner,owner,actor['user_id'])
                            await run_in_threadpool(self.p.package._root,owner,root)
                            admission_context={'owner':owner,'user_id':actor['user_id'],'import_selection':True}
                            continue
                        decision=await run_in_threadpool(self.p.package.require_operation,owner,
                            operation=operation,root=root,device_id=device,approval_id=headers.get('x-sjfx-approvals') or headers.get('x-sjfx-approval'))
                        admission_context=decision_context(decision)
                        package_contexts.append(admission_context)
                    if len(package_contexts)>1:admission_context={'owner':owner,'package_contexts':package_contexts,'approval_id':{c['registration_id']:c.get('approval_id','') for c in package_contexts}}
                    admission_context.update(owner=owner,device=device,operation=operation)
        except ProtectionError as exc:
            if enforced:
                if actor:
                    try:
                        await run_in_threadpool(self.p.store.event, actor['owner_id'], 'system.blocked',
                            device=device, payload={'method': scope['method'], 'route': path,
                                                    'status': exc.status, 'code': exc.code,
                                                    'user_id': actor['user_id']})
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception('文件保护拦截审计写入失败')
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
        output_rejected=[False]
        async def observed_send(message):
            if output_rejected[0]:return
            if message["type"] == "http.response.start":
                if not metadata_only and (admission_context.get('registration_id') or admission_context.get('package_contexts')):
                    try:
                        from .business import recheck_active_sources
                        await run_in_threadpool(recheck_active_sources,admission_context.get('operation','read'))
                    except ProtectionError as exc:
                        output_rejected[0]=True;status[0]=exc.status
                        response=JSONResponse({'ok':False,'code':exc.code,'error':str(exc),'protection_url':'/protection'},status_code=exc.status)
                        return await response(scope,receive,send)
                status[0] = message["status"]
            elif message["type"] == "http.response.body":
                raw = message.get("body",b"")
                sizes[1] += len(raw)
                if sizes[1] <= 65536:
                    response_parts.append(raw)
            if metadata_only:
                from .metadata import sanitize_control
                if message['type']=='http.response.start':
                    nonlocal metadata_start
                    metadata_start=message
                elif message['type']=='http.response.body':
                    metadata_chunks.append(message.get('body',b''))
                    if sum(map(len,metadata_chunks))>4*1024*1024:
                        raise ProtectionError('状态响应超出保护元信息预算',413,'response_too_large')
                    if not message.get('more_body'):
                        try:value=json.loads(b''.join(metadata_chunks))
                        except (ValueError,UnicodeError):value={'ok':False,'code':'metadata_unavailable'}
                        output=json.dumps(sanitize_control(value),ensure_ascii=False).encode('utf-8')
                        start=dict(metadata_start or {'type':'http.response.start','status':500,'headers':[]})
                        start['headers']=[(k,v) for k,v in start['headers'] if k.lower() not in {b'content-length',b'content-type'}]+[(b'content-type',b'application/json; charset=utf-8'),(b'content-length',str(len(output)).encode())]
                        await send(start);await send({'type':'http.response.body','body':output,'more_body':False})
            else:await send(message)
        audit_context={"owner":management_owner(actor),"device":device,'enforced':enforced,**admission_context} if actor else None
        token = current.set(audit_context)
        from .business import metadata_projection
        metadata_token=metadata_projection.set(metadata_only)
        started=time.monotonic()
        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            metadata_projection.reset(metadata_token)
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
                        supplied_file_id = payload.get('file_id')
                        if supplied_file_id:
                            with self.p.store.db() as db:
                                registered = db.execute('SELECT id,path,collection FROM files WHERE id=? AND owner=?',
                                                        (supplied_file_id, actor['owner_id'])).fetchone()
                            if registered:
                                file_id = registered['id']
                                payload['server_path'] = registered['path']
                                payload['collection_id'] = registered['collection']
                        candidate=payload.get('node_path') or payload.get('path')
                        if candidate and payload.get('collection_id') and payload.get('server_root'):
                            candidate=os.path.abspath(os.path.join(payload['server_root'],candidate))
                            with self.p.store.db() as db:
                                row=db.execute('SELECT id FROM files WHERE collection=? AND path=?',
                                               (payload['collection_id'],candidate)).fetchone()
                            if row:file_id=row[0]
                        if status[0] == 423:
                            self.p.store.event(actor['owner_id'], 'system.blocked', device=device, file_id=file_id, payload=payload)
                        self.p.store.event(actor['owner_id'],'system.request',device=device,file_id=file_id,payload=payload)
                    # Status polling is coalesced, while every mutation and file operation remains recorded.
                    key=(actor['owner_id'],path,status[0])
                    now=time.monotonic()
                    skip=False
                    polling = path.rstrip('/').rsplit('/', 1)[-1] in {'status', 'progress', 'heartbeat'}
                    has_file_target = any(payload.get(k) for k in ('file_id', 'path', 'node_path', 'artifact_id'))
                    if scope['method']=='GET' and polling and not has_file_target:
                        with self.p.lock:
                            skip=now-self.p.audit_reads.get(key,-100)<30
                            if not skip:
                                if len(self.p.audit_reads)>4000:self.p.audit_reads.clear()
                                self.p.audit_reads[key]=now
                    if not skip:await run_in_threadpool(record_request)
                    if not metadata_only:
                        for source in admission_context.get('package_contexts') or [admission_context]:
                            if not source.get('registration_id'):continue
                            await run_in_threadpool(self.p.package.record_operation,management_owner(actor),source['registration_id'],admission_context.get('operation','read'),
                                'completed' if status[0]<400 else 'denied' if status[0] in {403,409,423} else 'failed',context=source,source='local_http',trusted=True)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("文件保护审计写入失败")


def install(app, *, state_dir, allowed_roots, session_resolver, resource_root=None, mode=None,
            authorize_root=None,authorize_owner=None,resource_resolver=None):
    protection = Protection(state_dir,allowed_roots,session_resolver,resource_root,mode,
        authorize_root=authorize_root,authorize_owner=authorize_owner,resource_resolver=resource_resolver)
    from .business import configure
    configure(protection)
    app.router.add_event_handler('startup',protection.ensure_executor)
    app.add_middleware(ProtectionMiddleware, protection=protection)
    async def protection_error(request,exc):
        return JSONResponse({'ok':False,'error':str(exc),'code':exc.code,'protection_url':'/protection'},status_code=exc.status)
    app.add_exception_handler(ProtectionError,protection_error)

    async def handle(request: Request):
        try:
            path = request.url.path
            if path == "/protection/bridge.js":
                return Response((Path(__file__).parent/'web'/'bridge.js').read_text(encoding='utf-8'),media_type='application/javascript')
            if not path.startswith('/agent/v2/'):
                actor, session_tag = await run_in_threadpool(protection.user, request.headers)
                authorize(actor, path, request.method)
                request.state.protection_actor = (actor, session_tag)
            if path == '/protection/download':
                package=Path(__file__).parent/'downloads'/'sjfx-assistant-windows.zip'
                if not package.is_file():
                    raise ProtectionError('管理员尚未发布 Windows 助手安装包',503)
                return FileResponse(package,filename='sjfx-assistant-windows.zip',media_type='application/zip')
            if path in {'/protection/packages.js', '/protection/admin.js'}:
                return Response((Path(__file__).parent/'web'/path.rsplit('/',1)[-1]).read_text(encoding='utf-8'),
                                media_type='application/javascript',
                                headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Vary':'Cookie'})
            if path in {'/protection', '/protection/setup', '/protection/admin','/protection/packages'}:
                page = 'admin.html' if path == '/protection/admin' else 'packages.html'
                return HTMLResponse((Path(__file__).parent/'web'/page).read_text(encoding='utf-8'),
                                    headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff","Vary":"Cookie","X-Frame-Options":"DENY"})
            if path == "/protection/api/status":
                mode=protection.mode
                from .policy import read_policy,mode_allowed
                package_policy=read_policy(protection.state_dir)
                package_status={'enabled':package_policy['enabled'],'policy_revision':package_policy['revision'],
                    'registration_modes':[m for m in ('local',) if mode_allowed(package_policy,actor['owner_id'],m)],
                    'registration_url':'/protection/packages'}
                if mode=='audit':
                    try:
                        if await run_in_threadpool(protection.enforced,actor['owner_id']):mode='enforce'
                    except ProtectionError:pass
                if actor.get('role') != 'admin':
                    return JSONResponse({'ok':True, 'mode':mode, 'access_version':2,**package_status}, headers={'Cache-Control':'no-store'})
                return JSONResponse({"ok":True,"mode":mode,"global_mode":protection.mode, 'access_version':1,
                    'enforcement_available':protection.allow_enforcement,'source_mode':'local','reader_host':__import__('platform').node(),
                    'protection_version': '4.0-local-candidate', **package_status, 'capabilities': {'file_trace': True, 'version_aware_actions': True,'package_registration':True},
                    "assistant_transport":"authenticated_process_loopback"},headers={'Cache-Control':'no-store'})
            new_operations={'registrations','registration','recheck','cancel-registration','manifest',
                'package-policy','root-grants','approvals','approval-request','registration-mode',
                'manifest-complete','registration-manifest','registration-tasks','staging-version',
                'actions','trace','events','sensitivity','artifact-recheck','my-approvals','session','setup','enrollments','revoke-self','enroll','heartbeat','bindings','events','action-result','devices','verify-ledger'}
            if path.rsplit('/',1)[-1] not in new_operations:
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
                    limit=90*1024*1024 if path.endswith('/staging-version') else 256*1024*1024 if path.endswith('/manifest-complete') else 2*1024*1024
                    if size>limit:
                        raise ProtectionError("请求过大",413)
                    chunks.append(part)
                raw=b''.join(chunks)
            data=json.loads(raw) if raw else {}
            if not isinstance(data,dict):
                raise ProtectionError("请求必须为对象")
            result=await run_in_threadpool(dispatch,request,raw,data)
            return JSONResponse({"ok":True,**result},headers={"Cache-Control":"no-store"})
        except ProtectionError as exc:
            if exc.status == 401 and request.url.path in {'/protection', '/protection/setup', '/protection/admin'}:
                return RedirectResponse('/login', status_code=303, headers={'Cache-Control':'no-store'})
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
            if path.rsplit('/',1)[-1] in {'registration-tasks','registration-manifest','manifest-complete','staging-version','bind','manifest'}:
                raise ProtectionError('旧双端匹配协议已退出本地唯一路径流程',410,'legacy_transfer_removed')
            if path.endswith('/registration-tasks'):
                return {'tasks':protection.package.registration_tasks(device['owner'],device['id'])}
            if path.endswith('/registration-manifest'):
                return protection.package.local_manifest_task(device['owner'],device['id'],data)
            if path.endswith('/manifest-complete'):
                return protection.package.submit_manifest(device['owner'],device['id'],data)
            if path.endswith('/staging-version'):
                import base64
                try:
                    content=base64.b64decode(data.get('content_base64',''),validate=True)
                except (ValueError,TypeError):raise ProtectionError('更新内容编码无效')
                if len(content)>64*1024*1024:raise ProtectionError('更新内容超出64MiB提交限制',413)
                return protection.package.stage_version(device['owner'],str(data.get('registration_id','')),
                    str(data.get('binding_id','')),content,str(data.get('expected_sha256','')),device_id=device['id'])
            if path.endswith('/heartbeat'):
                store.heartbeat(device['id'],data)
                return {'server_time':time.time(),'protection_enabled':protection.enabled,'actions':[a for a in store.pending_actions(device['id']) if protection.enabled or a.get('kind')=='restore']}
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
        user,session_tag=request.state.protection_actor
        owner=management_owner(user)
        operation=path.rsplit('/',1)[-1]
        if operation=='package-policy':
            if method=='GET':return {'policy':protection.package.policy()}
            if owner != user['owner_id']:
                raise ProtectionError('请先切回本人空间再调整保护总开关',403,'own_view_required')
            policy=protection.package.update_policy(user['user_id'],user.get('role')=='admin',
                {k:v for k,v in data.items() if k!='reason'},str(data.get('reason','')))
            protection.ensure_executor()
            return {'policy':policy}
        if operation=='root-grants':
            key='package-root-grants:'+owner
            if method=='GET':
                with store.db() as db:row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
                return {'owner':owner,'roots':json.loads(row[0]) if row else []}
            roots=data.get('roots');reason=str(data.get('reason','')).strip()
            if not isinstance(roots,list) or not reason or any(not isinstance(r,str) or not Path(r).is_absolute() for r in roots):
                raise ProtectionError('目录授权需绝对路径列表和理由')
            roots=[os.path.abspath(r) for r in roots]
            if any(not any(Path(r).is_relative_to(Path(a).absolute()) for a in protection.allowed_roots) for r in roots):
                raise ProtectionError('授权目录超出系统范围',403)
            with store.db(True) as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(roots)))
                store._event(db,owner,'registration.root_grant.changed',payload={'roots':roots,'actor':user['user_id'],'reason':reason})
            return {'owner':owner,'roots':roots}
        if operation=='registrations':
            if method=='GET':return {'registrations':protection.package.list(owner)}
            # Self-service creates only the authenticated user's package; view-as does not grant ownership.
            return protection.package.register(user['owner_id'],user['user_id'],str(data.get('local_path','')),
                str(data.get('name','')),'local',device_id=str(data.get('device_id','')),
                local_root='')
        if operation in {'registration','manifest'}:
            rid=request.query_params.get('registration_id','')
            return protection.package.status(owner,rid,device_id=request.query_params.get('device_id','')) if operation=='registration' else protection.package.manifest(owner,rid)
        if operation=='recheck':
            return protection.package.recheck(owner,str(data.get('registration_id','')),device_id=str(data.get('device_id','')))
        if operation=='cancel-registration':
            return protection.package.cancel(owner,str(data.get('registration_id','')))
        if operation=='sensitivity':
            protection.package.set_sensitivity(owner,str(data.get('registration_id','')),str(data.get('sensitivity','')),
                user['user_id'],str(data.get('reason','')),is_admin=user.get('role')=='admin',file_ids=data.get('file_ids'))
            return {}
        if operation=='artifact-recheck':
            return protection.package.recheck_artifact(owner,str(data.get('artifact_id','')))
        if operation=='my-approvals':
            return {'approvals':protection.package.approvals(user['owner_id'])}
        if operation=='approval-request':
            return protection.package.request_approval(owner,str(data.get('registration_id','')),
                str(data.get('operation','')),str(data.get('reason','')),context=data.get('context'))
        if operation=='approvals':
            if method=='GET':return {'approvals':protection.package.approvals(owner)}
            return protection.package.approve(owner,str(data.get('approval_id','')),user['user_id'],
                data.get('approved') is True,str(data.get('reason','')),is_admin=user.get('role')=='admin')
        if operation=='registration-mode':
            return protection.package.change_mode(owner,str(data.get('registration_id','')),str(data.get('mode','')),
                user['user_id'],str(data.get('reason','')),is_admin=user.get('role')=='admin')
        if path.endswith('/policy') and method=='POST':
            if owner != user['owner_id']:
                raise ProtectionError('请先切回本人空间再调整保护总开关',403,'own_view_required')
            policy=protection.package.update_policy(user['user_id'],user.get('role')=='admin',
                {'enabled':data.get('enabled')},str(data.get('reason') or '管理员通过保护设置变更总开关'))
            protection.ensure_executor()
            return {'policy':policy,'enforced':policy['enabled']}
        if path.endswith('/session'):
            result = {'session_tag':session_tag,'name':user.get('display_name',''),'admin':user.get('role')=='admin'}
            if user.get('role') == 'admin': result.update(owner=owner, device_owner=user['owner_id'])
            return result
        if path.endswith('/setup'):
            return setup(store, user)
        if path.endswith('/revoke-self'):
            return revoke_self(store, user, data)
        if path.endswith('/enrollments') and method=='POST':
            return enroll(store, user, data)
        if path.endswith('/collections'):
            if method=='GET':
                return {'collections':store.list_rows('collections',owner)}
            raise ProtectionError('请使用本地路径登记入口',410,'legacy_registration_removed')
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
        if path.endswith('/trace') and method == 'GET':
            return store.file_trace(owner, request.query_params.get('file_id', ''))
        if path.endswith('/actions'):
            if method=='GET':
                return {'actions':store.list_rows('actions',owner)}
            if not protection.enabled and data.get('kind')!='restore':raise ProtectionError('保护关闭后仅允许恢复已有处置',409,'protection_disabled')
            return {'action_id':store.command(owner,str(data.get('binding_id','')),str(data.get('kind','')),
                        str(data.get('reason','')),str(data.get('restore_action_id','')))}
        if path.endswith('/preview'):
            with store.db() as db:
                row=db.execute('SELECT * FROM files WHERE id=? AND owner=?',(request.query_params.get('file_id'),owner)).fetchone()
            if not row or not row['path'].lower().endswith('.eml'):
                raise ProtectionError('仅支持已登记邮件的纯文本安全预览',404)
            if Path(row['path']).stat().st_size>20*1024*1024:
                raise ProtectionError('邮件超过预览大小限制')
            decision=protection.package.require_operation(owner,root=row['path'],operation='preview')
            snapshot=protection.package.verified_snapshot(owner,decision['registration_id'],row['id'],context=decision['context'])
            return {'preview':safe_mail_preview(snapshot,row['sha256'])}
        if path.endswith('/review') and method=='POST':
            if user.get('role')!='admin':raise ProtectionError('仅管理员可复核可疑文件',403)
            store.approve_review(owner,user['user_id'],str(data.get('file_id','')),str(data.get('sha256','')),str(data.get('reason','')))
            return {}
        if path.endswith('/verify-ledger'):
            if user.get('role')!='admin':
                raise ProtectionError('仅管理员可校验审计链',403)
            return store.verify_chain()
        raise ProtectionError('接口不存在',404)

    for route in ('/protection','/protection/setup','/protection/admin','/protection/packages','/protection/packages.js','/protection/admin.js','/protection/bridge.js','/protection/download','/protection/api/{operation}','/agent/v2/{operation}'):
        app.add_api_route(route,handle,methods=['GET','POST'],include_in_schema=False)
    return protection
