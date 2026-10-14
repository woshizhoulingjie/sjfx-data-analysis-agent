"""HTTP surface for the isolated mail research domain.

Every handler resolves the authenticated owner before it touches a mail row.
The only client-supplied filesystem value is a new package source, validated by
``MailPolicy`` before it is persisted.  Previews subsequently use stored
locators through the repository instead of accepting paths from the browser.
"""

from __future__ import annotations

import hashlib
import json
from functools import wraps
from pathlib import Path

from config import Config
from web_compat import jsonify, render_template, request

from .attachments import read_part
from .html_safety import cid_image_urls, sanitize_html
from .policy import MailPolicyError
from .research.intent import build_task_spec, research_budget
from .research.planner import VERSION as RESEARCH_VERSION
from .source import AdapterRegistry, sha256_file


def _scope():
    return {'date_from':request.args.get('date_from'), 'date_to':request.args.get('date_to'),
        'q':request.args.get('q') or request.args.get('query') or '',
        'has_attachment':_flag(request.args.get('has_attachment')), 'participant':request.args.get('participant')}


def _download(raw, filename, media_type='application/octet-stream'):
    from urllib.parse import quote
    from web_compat import Response
    name=Path(str(filename or 'attachment').replace('\\','/')).name.replace('\r','').replace('\n','')
    return Response(content=raw,media_type=media_type,headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(name,safe=''),
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


def _error(message, status=400):
    return jsonify({"ok": False, "error": str(message)}), int(status)


def _owner(services):
    resolver = services.owner_resolver
    value = resolver() if resolver else "legacy"
    # Only the trusted server resolver returns None for an admin default view.
    # Mail reads and writes both require a concrete workspace, never no filter.
    # Explicit user/view-as owners and the no-resolver fallback stay unchanged.
    if resolver is not None and value is None:
        value = Config.OWNER_ID
    return str(value or "legacy")


def _limit(raw, default=100, maximum=200):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(1, min(maximum, value))


def _offset(raw):
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _flag(raw):
    value = str(raw or "").strip().casefold()
    if not value:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("附件筛选需要有效的布尔值")


def _package_view(package):
    """Do not send a server filesystem path to the workbench."""
    if not package:
        return None
    value = dict(package)
    value.pop("source_path", None)
    return value


def _package(services, package_id, owner_id):
    package = services.repository.get_package(str(package_id), owner_id)
    if not package:
        raise LookupError("邮件资料包不存在或不属于当前用户")
    return package


def _queue(services, package_id, task_type, owner_id, options):
    if not services.job_factory:
        raise RuntimeError("邮件任务队列尚未初始化")
    if task_type == 'mail_research':
        # The supervisor must honor the same allowance accepted by create and
        # resume. Persist it on each queued job, including resumed research.
        options=dict(options)
        turn=services.repository.get_turn(options.get('turn_id'),owner_id) if options.get('turn_id') else None
        seconds=((turn or {}).get('task_spec',{}).get('budget') or {}).get('max_seconds',Config.MAIL_RESEARCH_TIMEOUT_SECONDS)
        if isinstance(seconds,bool) or not isinstance(seconds,int) or not 1<=seconds<=Config.MAIL_RESEARCH_TIMEOUT_LIMIT_SECONDS:
            raise ValueError('研究运行时间超出服务上限')
        options['research_timeout_seconds']=seconds
    job_id, created = services.job_factory(
        str(package_id), str(task_type), options=options, owner_id=owner_id
    )
    if created:
        priority={"mail_research":120,"mail_import":90,"mail_extract":40,"mail_ocr":10,"mail_embed":5,"mail_factextract":5,"mail_export":60}.get(task_type,50)
        with services.repository._connection() as c:
            c.execute("UPDATE analysis_jobs SET priority=? WHERE id=? AND owner_id=? AND task_type=?",(priority,str(job_id),owner_id,task_type))
    return str(job_id), bool(created)


def _source_revision(path, *, policy=None, owner_id=None):
    """Make a stable source revision without treating a directory as one file."""
    if path.is_file():
        return sha256_file(path, policy=policy, owner_id=owner_id)
    digest = hashlib.sha256()
    root = path.resolve()
    member_count = 0
    for child in sorted(root.rglob("*")):
        if not child.is_file() or child.suffix.casefold() != ".eml":
            continue
        if member_count >= int(getattr(Config, "MAX_ARCHIVE_ENTRIES", 5000)):
            raise MailPolicyError("邮件目录成员数超过限制")
        try:
            relative = child.resolve().relative_to(root).as_posix()
            stat = child.stat()
        except OSError as exc:
            raise MailPolicyError("读取邮件目录清单失败") from exc
        digest.update(relative.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")
        member_count += 1
    return "sha256-manifest:" + digest.hexdigest()


def _source_kind(path):
    if path.is_dir():
        return "directory"
    suffix = path.suffix.casefold()
    return {".eml": "eml", ".zip": "zip", ".mbox": "mbox", ".mbx": "mbox", ".msg": "msg", ".pst": "pst"}.get(suffix, "")


def _mail_endpoint(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if not getattr(Config, "MAIL_FEATURE_ENABLED", False):
            return _error("邮件研究功能未启用", 404)
        try:
            return handler(*args, **kwargs)
        except LookupError as exc:
            return _error(exc, 404)
        except (MailPolicyError, ValueError) as exc:
            return _error(exc, 400)
        except RuntimeError as exc:
            return _error(exc, 409)
    return wrapped


def register_mail_routes(app, services):
    """Register routes once the application runtime has assembled services."""
    from .people_api import register_people_routes
    register_people_routes(app, services)
    from .analysis_api import register_analysis_routes
    register_analysis_routes(app, services)

    @app.route('/api/mail/packages/<package_id>/foundation')
    @_mail_endpoint
    def foundation_overview(package_id):
        from .foundation import overview
        return jsonify({'ok':True,'foundation':overview(services.repository,package_id,_owner(services))})

    @app.route('/api/mail/packages/<package_id>/inventory')
    @_mail_endpoint
    def foundation_inventory(package_id):
        from .foundation import inventory
        return jsonify({'ok':True,**inventory(services.repository,package_id,_owner(services),offset=_offset(request.args.get('offset')),limit=_limit(request.args.get('limit')),status=request.args.get('status'))})

    @app.route('/api/mail/packages/<package_id>/foundation/backfill',methods=['POST'])
    @_mail_endpoint
    def foundation_backfill(package_id):
        owner=_owner(services);_package(services,package_id,owner)
        payload=request.get_json(silent=True) or {}
        from .foundation import set_state
        with services.repository._connection() as c:
            existing=c.execute("SELECT status FROM mail_foundation_state WHERE package_id=? AND stage='historical_backfill'",(package_id,)).fetchone()
        if payload.get('retry_failed') or (existing and existing[0]=='partial'):
            # Explicit restart permits retrying previously unavailable original sources.
            set_state(services.repository,package_id,'historical_backfill','pending')
        job_id,created=_queue(services,package_id,'mail_foundation',owner,{'package_id':package_id,'workflow_source':'mail_foundation'})
        return jsonify({'ok':True,'accepted':True,'job_id':job_id,'created':created}),202

    @app.route('/api/mail/messages/<message_key>/content')
    @_mail_endpoint
    def foundation_content(message_key):
        from .foundation import content
        return jsonify({'ok':True,'content':content(services.repository,message_key,_owner(services),str(request.args.get('package_id') or ''))})

    @app.route('/api/mail/messages/<message_key>/raw')
    @_mail_endpoint
    def foundation_raw(message_key):
        from .attachments import read_message_raw
        raw=read_message_raw(services.repository,message_key,_owner(services),str(request.args.get('package_id') or ''),
                             occurrence_id=str(request.args.get('occurrence_id') or '') or None)
        return _download(raw,message_key+'.eml','message/rfc822')

    @app.route('/api/mail/messages/<message_key>/parts/<path:mime_path>/raw')
    @_mail_endpoint
    def foundation_part_raw(message_key,mime_path):
        part,_=read_part(services.repository,message_key,_owner(services),str(request.args.get('package_id') or ''),mime_path)
        payload=part.raw_payload
        if payload is None:raise LookupError('部件没有可下载内容')
        return _download(payload,part.filename_raw or ('mime-'+mime_path+'.bin'))

    @app.route('/api/mail/messages/<message_key>/parts/<path:mime_path>/preview-image')
    @_mail_endpoint
    def foundation_part_image(message_key, mime_path):
        from web_compat import Response
        from .html_safety import RASTER_TYPES
        part, _ = read_part(services.repository, message_key, _owner(services),
                            str(request.args.get('package_id') or ''), mime_path)
        raw = part.raw_payload or b''
        media = part.content_type
        valid = ((media == 'image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n')) or
                 (media == 'image/jpeg' and raw.startswith(b'\xff\xd8\xff')) or
                 (media == 'image/gif' and raw.startswith((b'GIF87a', b'GIF89a'))) or
                 (media == 'image/webp' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'))
        if media not in RASTER_TYPES or not valid or not (part.is_inline_resource or part.is_explicit_attachment):
            raise ValueError('此部件不是可预览的 PNG、JPEG、GIF 或 WebP 图片；请下载原件查看')
        return Response(content=raw, media_type=media, headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; sandbox", 'Content-Disposition': 'inline'})

    @app.route('/api/mail/messages/<message_key>/attachments/<path:mime_path>/retry',methods=['POST'])
    @_mail_endpoint
    def foundation_attachment_retry(message_key,mime_path):
        owner=_owner(services);payload=request.get_json(silent=True) or {};pid=str(payload.get('package_id') or '')
        message=services.repository.get_message(message_key,owner,pid)
        if not message:raise LookupError('邮件不存在或不属于当前资料包')
        part=next((p for p in message['parts'] if p['mime_path']==mime_path and p['is_explicit_attachment']),None)
        if not part:raise LookupError('附件不存在')
        with services.repository._connection() as c:
            c.execute("UPDATE mail_part_processing SET status='pending',retry_count=0,next_retry_at=NULL,error_json=NULL WHERE message_key=? AND mime_path=? AND stage IN ('basic_text','ocr')",(message_key,mime_path))
        ids=[]
        for task in ('mail_extract','mail_ocr'):
            jid,created=_queue(services,pid,task,owner,{'package_id':pid,'workflow_source':'mail_attachment_retry'})
            ids.append({'job_id':jid,'created':created})
        return jsonify({'ok':True,'accepted':True,'jobs':ids}),202

    @app.route('/api/mail/conversations/<conversation_id>/corrections',methods=['POST'])
    @_mail_endpoint
    def foundation_conversation_correction(conversation_id):
        from .conversations import detail,apply_correction
        owner=_owner(services);payload=request.get_json(silent=True) or {}
        if not isinstance(payload,dict):raise ValueError('请求内容必须是对象')
        value=detail(services.repository,conversation_id,owner)
        if not value:raise LookupError('会话不存在')
        pid=value['package_id']
        if payload.get('package_id') and payload['package_id']!=pid:raise ValueError('会话不属于指定邮件包')
        keys=[m['message_key'] for m in value['messages']];action=payload.get('action');groups=None
        if action=='merge':
            target=detail(services.repository,str(payload.get('target_conversation_id') or ''),owner)
            if not target or target['package_id']!=pid:raise ValueError('合并目标必须属于当前邮件包')
            keys+= [m['message_key'] for m in target['messages']]
        elif action=='split':
            selected=set(payload.get('message_keys') or (payload.get('groups') or [[]])[0])
            if not selected.issubset(keys):raise ValueError('拆分邮件必须属于当前会话')
            groups=[sorted(selected),sorted(set(keys)-selected)]
        correction=apply_correction(services.repository,pid,owner,action=action,message_keys=keys,groups=groups,correction_id=payload.get('correction_id'),reason=payload.get('reason'))
        return jsonify({'ok':True,'correction':correction})

    @app.route('/api/mail/packages/<package_id>/conversation-corrections')
    @_mail_endpoint
    def foundation_conversation_history(package_id):
        from .conversations import history
        value=history(services.repository,package_id,_owner(services))
        return jsonify({'ok':True,'items':value['corrections'],'history':value})

    @app.route("/mail-research")
    @_mail_endpoint
    def mail_research_page():
        response = render_template("mail_research.html")
        response.headers["Cache-Control"] = "no-store, max-age=0"
        # Authenticated MIME images become local blob URLs in this page only.
        # Email HTML itself still runs in an empty sandbox with no scripts.
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; frame-src 'self' blob:; connect-src 'self'"
        )
        return response

    @app.route("/api/mail/packages", methods=["GET"])
    @_mail_endpoint
    def list_mail_packages():
        owner_id = _owner(services)
        return jsonify({
            "ok": True,
            "packages": [_package_view(item) for item in services.repository.list_packages(owner_id)],
            "support_matrix": AdapterRegistry().support_matrix(),
        })

    @app.route("/api/mail/packages", methods=["POST"])
    @_mail_endpoint
    def create_mail_package():
        payload = request.get_json(silent=True) or {}
        source = payload.get("source") or {}
        raw_path = str(source.get("path") or "")
        owner_id = _owner(services)
        path = services.policy.resolve_source(raw_path, owner_id=owner_id)
        source_kind = _source_kind(path)
        if not source_kind:
            raise MailPolicyError("邮件来源仅支持 ZIP、目录、EML、MBOX、MSG 或 PST")
        requested_kind = str(source.get("kind") or "").strip().casefold()
        if requested_kind and requested_kind not in {source_kind, "directory" if path.is_dir() else source_kind}:
            raise MailPolicyError("来源类型与服务器文件不匹配")
        config = dict(payload.get("options") or {})
        from sjfx_protection.business import capture_import,enabled as protection_enabled
        if protection_enabled():
            captured=capture_import(owner_id,str(path),{})
            config['protection_intake']=captured['protection_intake']
            config.pop('protection_context',None)
        display_name = str(payload.get("display_name") or path.name or "邮件资料包").strip()[:200]
        package, created = services.repository.create_package(
            owner_id=owner_id,
            display_name=display_name or "邮件资料包",
            source_kind=source_kind,
            source_path=str(path),
            source_revision='pending-inspection' if protection_enabled() else _source_revision(path, policy=services.policy, owner_id=owner_id),
            config=config,
            idempotency_key=str(payload.get("idempotency_key") or "").strip()[:200] or None,
        )
        job_id = None
        job_created = False
        if created or package.get("status") in {"inventory", "failed", "paused"}:
            job_id, job_created = _queue(
                services, package["package_id"], "mail_import", owner_id,
                {"package_id": package["package_id"], "workflow_source": "mail_package_import"},
            )
        status = 202 if job_created else 200
        return jsonify({
            "ok": True, "accepted": bool(job_id), "package": _package_view(package),
            "job_id": job_id, "reused_active_job": bool(job_id) and not job_created,
            "status_url": "/api/jobs/{}".format(job_id) if job_id else None,
        }), status

    @app.route("/api/mail/packages/<package_id>")
    @_mail_endpoint
    def mail_package_detail(package_id):
        owner_id = _owner(services)
        _package(services, package_id, owner_id)
        overview = services.repository.overview(package_id, owner_id)
        overview["package"] = _package_view(overview["package"])
        return jsonify({"ok": True, "overview": overview, "coverage": services.repository.coverage(package_id, owner_id)})

    @app.route("/api/mail/packages/<package_id>/resume", methods=["POST"])
    @app.route("/api/mail/packages/<package_id>/import/resume", methods=["POST"])
    @_mail_endpoint
    def resume_mail_package(package_id):
        owner_id = _owner(services)
        package = _package(services, package_id, owner_id)
        job_id, created = _queue(
            services, package_id, "mail_import", owner_id,
            {"package_id": package_id, "workflow_source": "mail_package_resume"},
        )
        return jsonify({"ok": True, "accepted": True, "package": _package_view(package), "job_id": job_id,
                        "reused_active_job": not created, "status_url": "/api/jobs/{}".format(job_id)}), 202 if created else 200

    @app.route("/api/mail/packages/<package_id>/index", methods=["POST"])
    @_mail_endpoint
    def index_mail_package(package_id):
        owner_id = _owner(services)
        _package(services, package_id, owner_id)
        payload = request.get_json(silent=True) or {}
        requested = str(payload.get("stage") or "attachments").casefold()
        if requested not in {"attachments", "ocr", "embedding", "facts", "all"}:
            raise ValueError("索引阶段仅支持 attachments、ocr、embedding、facts 或 all")
        jobs = []
        if requested in {"attachments", "all"}:
            job_id, created = _queue(services, package_id, "mail_extract", owner_id, {
                "package_id": package_id, "workflow_source": "mail_attachment_index",
            })
            jobs.append({"task_type": "mail_extract", "job_id": job_id, "created": created})
        if requested in {"ocr", "all"}:
            job_id, created = _queue(services, package_id, "mail_ocr", owner_id, {
                "package_id": package_id, "workflow_source": "mail_ocr_index",
            })
            jobs.append({"task_type": "mail_ocr", "job_id": job_id, "created": created})
        for stage,task_type,enabled in [("embedding","mail_embed",Config.MAIL_ENABLE_VECTOR_INDEX),("facts","mail_factextract",Config.MAIL_ENABLE_FACT_EXTRACTION)]:
            if requested==stage and not enabled:raise ValueError("该索引功能尚未启用")
            if requested in {stage,"all"} and enabled:
                job_id,created=_queue(services,package_id,task_type,owner_id,{"package_id":package_id,"fact_types":payload.get("fact_types"),"workflow_source":"mail_"+stage})
                jobs.append({"task_type":task_type,"job_id":job_id,"created":created})
        return jsonify({"ok": True, "accepted": True, "jobs": jobs}), 202

    @app.route("/api/mail/packages/<package_id>/messages")
    @_mail_endpoint
    def list_mail_messages(package_id):
        owner_id = _owner(services)
        return jsonify({"ok": True, "messages": services.repository.list_messages(
            package_id, owner_id, offset=_offset(request.args.get("offset")),
            limit=_limit(request.args.get("limit")), query=str(request.args.get("q") or request.args.get("query") or "")[:1000],
            participant=str(request.args.get("participant") or "")[:320] or None,
            date_from=str(request.args.get("date_from") or "")[:64] or None,
            date_to=str(request.args.get("date_to") or "")[:64] or None,
            has_attachment=_flag(request.args.get("has_attachment")),
        )})

    @app.route("/api/mail/messages/<message_key>")
    @_mail_endpoint
    def mail_message_detail(message_key):
        owner_id = _owner(services)
        package_id = str(request.args.get("package_id") or "")
        if not package_id:
            raise ValueError("需要 package_id")
        message = services.repository.get_message(message_key, owner_id, package_id)
        if not message:
            raise LookupError("邮件不存在或不属于当前资料包")
        return jsonify({"ok": True, "message": message})

    @app.route("/api/mail/messages/<message_key>/preview")
    @_mail_endpoint
    def mail_message_preview(message_key):
        owner_id = _owner(services)
        package_id = str(request.args.get("package_id") or "")
        message = services.repository.get_message(message_key, owner_id, package_id)
        if not message:
            raise LookupError("邮件不存在或不属于当前资料包")
        mime_path = str(request.args.get("mime_path") or message.get("body_preferred_part") or "")
        if not mime_path:
            raise LookupError("该邮件没有可预览正文")
        part, _raw = read_part(services.repository, message_key, owner_id, package_id, mime_path)
        if part.part_role not in {"body_plain", "body_html"}:
            raise ValueError("只能预览邮件正文部件")
        content = str(part.html_raw if part.part_role=='body_html' else part.text or "")
        is_html = part.part_role == "body_html"
        return jsonify({"ok": True, "message_key": message_key, "mime_path": mime_path,
                        "content_type": "text/html" if is_html else "text/plain",
                        "content": sanitize_html(content, cid_image_urls(message_key, package_id, message['parts'])) if is_html else content})

    @app.route("/api/mail/packages/<package_id>/attachments")
    @_mail_endpoint
    def list_mail_attachments(package_id):
        owner_id = _owner(services)
        include_inline = _flag(request.args.get("include_inline")) is True
        return jsonify({"ok": True, "attachments": services.repository.attachments(
            package_id, owner_id, offset=_offset(request.args.get("offset")),
            limit=_limit(request.args.get("limit")), include_inline=include_inline,
        )})

    @app.route("/api/mail/messages/<message_key>/attachments/<path:mime_path>/preview")
    @_mail_endpoint
    def mail_attachment_preview(message_key, mime_path):
        owner_id = _owner(services)
        package_id = str(request.args.get("package_id") or "")
        message = services.repository.get_message(message_key, owner_id, package_id)
        if not message:
            raise LookupError("附件邮件不存在或不属于当前资料包")
        part, _raw = read_part(services.repository, message_key, owner_id, package_id, mime_path)
        if part.part_role not in {"attachment", "inline"}:
            raise ValueError("只能预览附件部件")
        content_type = str(part.content_type or "").casefold()
        if not (content_type.startswith("text/") or content_type in {"application/json", "application/xml"}):
            return jsonify({"ok": True, "available": False, "reason": "该附件需通过抽取文本或受控下载查看",
                            "mime_path": mime_path, "content_type": part.content_type})
        from .parsers.eml import _decode_text
        warnings = []
        full_text = _decode_text(part.raw_payload or b'', part.charset, warnings)
        maximum = max(1, int(Config.MAIL_TOOL_CONTEXT_MAX_CHARS))
        truncated = len(full_text) > maximum
        return jsonify({"ok": True, "available": True, "mime_path": mime_path, "content_type": part.content_type,
                        "content": full_text[:maximum], "full_char_count": len(full_text), "truncated": truncated,
                        "reading_projection_complete": not warnings and not truncated, "warnings": warnings,
                        "original_available": True})

    @app.route("/api/mail/messages/<message_key>/attachments/<path:mime_path>/segments")
    @_mail_endpoint
    def mail_attachment_segments(message_key, mime_path):
        owner_id = _owner(services)
        package_id = str(request.args.get("package_id") or "")
        if not package_id:
            raise ValueError("需要 package_id")
        offset=_offset(request.args.get('offset'));limit=_limit(request.args.get('limit'))
        segments=services.repository.attachment_segments(package_id,owner_id,message_key,mime_path,limit=limit,offset=offset)
        with services.repository._connection() as c:
            total=c.execute("SELECT COUNT(*) FROM mail_text_segments WHERE package_id=? AND message_key=? AND mime_path=? AND source_kind='attachment'",(package_id,message_key,mime_path)).fetchone()[0]
        return jsonify({'ok':True,'segments':segments,'total':total,'offset':offset,'limit':limit})

    @app.route("/api/mail/packages/<package_id>/communication")
    @app.route("/api/mail/packages/<package_id>/relations/communication")
    @_mail_endpoint
    def mail_communication(package_id):
        owner_id = _owner(services)
        roles = [item.strip() for item in str(request.args.get("roles") or "").split(",") if item.strip()]
        return jsonify({"ok": True, "communication": services.repository.communication(
            package_id, owner_id, count_basis=str(request.args.get("count_basis") or "logical"),
            **_scope(), roles=roles or None,
        )})

    @app.route("/api/mail/packages/<package_id>/threads")
    @app.route("/api/mail/packages/<package_id>/relations/threads")
    @_mail_endpoint
    def mail_threads(package_id):
        owner_id = _owner(services)
        status = str(request.args.get("status") or "").strip() or None
        relation_type = str(request.args.get("relation_type") or "").strip() or None
        return jsonify({"ok": True, "threads": services.repository.threads(package_id, owner_id, status=status, relation_type=relation_type,**_scope())})

    @app.route("/api/mail/packages/<package_id>/business-relations")
    @app.route("/api/mail/packages/<package_id>/relations/business")
    @_mail_endpoint
    def mail_business_relations(package_id):
        owner_id = _owner(services)
        return jsonify({"ok": True, "relations": services.repository.business_relations(
            package_id, owner_id,
            include_unreviewed=_flag(request.args.get("include_unreviewed")) is True,
        )})

    @app.route("/api/mail/research-sessions", methods=["GET"])
    @_mail_endpoint
    def list_mail_sessions():
        owner_id = _owner(services)
        package_id = str(request.args.get("package_id") or "")
        if not package_id:
            raise ValueError("需要 package_id")
        return jsonify({"ok": True, "sessions": services.repository.list_sessions(package_id, owner_id, limit=_limit(request.args.get("limit")))})

    @app.route("/api/mail/research-sessions/<session_id>")
    @_mail_endpoint
    def get_mail_session(session_id):
        owner_id=_owner(services)
        session=services.repository.get_session(session_id,owner_id)
        if not session:raise LookupError("研究会话不存在")
        with services.repository._connection() as c:
            turns=[dict(r) for r in c.execute("SELECT turn_id,question,status,result_set_id,sufficiency,created_at FROM mail_research_turns WHERE session_id=? AND owner_id=? ORDER BY created_at",(session_id,owner_id))]
        return jsonify({"ok":True,"session":session,"turns":turns})

    @app.route("/api/mail/research-sessions", methods=["POST"])
    @_mail_endpoint
    def create_mail_session():
        payload = request.get_json(silent=True) or {}
        owner_id = _owner(services)
        package_id = str(payload.get("package_id") or "")
        _package(services, package_id, owner_id)
        session = services.repository.create_session(package_id, owner_id, payload.get("title") or "邮件研究", payload.get("scope") or {})
        return jsonify({"ok": True, "session": session}), 201

    @app.route("/api/mail/research-sessions/<session_id>/turns", methods=["POST"])
    @_mail_endpoint
    def create_mail_turn(session_id):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise ValueError("研究请求必须是对象")
        owner_id = _owner(services)
        session = services.repository.get_session(session_id, owner_id)
        if not session:
            raise LookupError("研究会话不存在或不属于当前用户")
        package = _package(services, session["package_id"], owner_id)
        spec = build_task_spec(
            payload.get("question"), package["package_id"], package["source_revision"], package["index_epoch"],
            scope=payload.get("scope") or session.get("scope") or {},
            base_result_set_id=payload.get("base_result_set_id"),
            max_tool_calls=Config.MAIL_RESEARCH_MAX_TOOL_CALLS,
            max_model_calls=Config.MAIL_RESEARCH_MAX_MODEL_CALLS,
            max_seconds=Config.MAIL_RESEARCH_TIMEOUT_SECONDS,
            research_version=RESEARCH_VERSION if getattr(Config,'MAIL_PHASE2_ENABLED',True) else None,
            answer_mode=payload.get("answer_mode"),
        )
        spec["analysis_version"]=3
        spec['budget']=research_budget(payload.get('budget'))
        if spec["mode"]=="exhaustive":
            selected_budget=dict(spec['budget'])
            from .analysis_api import make_spec
            spec,_=make_spec(services.repository,package["package_id"],owner_id,{
                "kind":"screen","question":spec["objective"],"scope":spec["scope"],
                "base_result_set_id":spec.get("base_result_set_id"),
                "model_call_budget":(payload.get("budget") or {}).get("max_model_calls",Config.MAIL_ANALYSIS_MAX_MODEL_CALLS),
                "max_tokens":spec["budget"]["max_tokens"]})
            spec['budget']=selected_budget
        turn = services.repository.create_turn(session_id, owner_id, spec["objective"], spec)
        try:
            job_id, created = _queue(services, package["package_id"], "mail_research", owner_id, {
                "package_id": package["package_id"], "turn_id": turn["turn_id"], "workflow_source": "mail_research",
            })
            if not created:
                raise ValueError('当前资料包已有研究任务，请完成或停止后再提交')
        except Exception as exc:
            services.repository.update_turn(turn['turn_id'],owner_id,status='failed',error={'message':str(exc)[:300]})
            raise
        services.repository.update_turn(turn["turn_id"], owner_id, job_id=job_id)
        turn = services.repository.get_turn(turn["turn_id"], owner_id)
        waiting_for_index = bool(services.repository.coverage(package["package_id"], owner_id).get("ocr_pending"))
        return jsonify({"ok": True, "accepted": True, "session": session, "turn": turn, "job_id": job_id,
                        "reused_active_job": not created, "status_url": "/api/jobs/{}".format(job_id),
                        "waiting_for_index": waiting_for_index}), 202 if created or waiting_for_index else 200

    @app.route("/api/mail/research-turns/<turn_id>")
    @_mail_endpoint
    def mail_research_turn(turn_id):
        owner_id = _owner(services)
        turn = services.repository.get_turn(turn_id, owner_id)
        if not turn:
            raise LookupError("邮件研究轮次不存在或不属于当前用户")
        if turn.get("job_id") and turn.get("status") not in {"completed","failed","cancelled"}:
            job=services.storage.get_job(turn["job_id"],owner_id)
            if job and job.get("status") in {"cancelled","failed"}:
                services.repository.update_turn(turn_id,owner_id,status=job["status"],sufficiency="insufficient",error={"message":str(job.get("error") or job["status"])[:300]})
                turn=services.repository.get_turn(turn_id,owner_id)
        return jsonify({"ok": True, "turn": turn})

    @app.route("/api/mail/research-turns/<turn_id>/events")
    @_mail_endpoint
    def mail_research_events(turn_id):
        owner_id = _owner(services)
        turn = services.repository.get_turn(turn_id, owner_id)
        if not turn:
            raise LookupError("邮件研究轮次不存在或不属于当前用户")
        after = _offset(request.args.get("after"))
        events = [item for item in turn.get("events") or [] if int(item.get("sequence") or 0) > after]
        return jsonify({"ok": True, "turn_id": turn_id, "events": events, "next_sequence": events[-1]["sequence"] if events else after})

    @app.route("/api/mail/research-turns/<turn_id>/cancel", methods=["POST"])
    @_mail_endpoint
    def cancel_mail_research_turn(turn_id):
        owner_id = _owner(services)
        turn = services.repository.get_turn(turn_id, owner_id)
        if not turn:
            raise LookupError("邮件研究轮次不存在或不属于当前用户")
        job_id = turn.get("job_id")
        if job_id:
            services.storage.cancel_job(job_id)
        services.repository.update_turn(turn_id, owner_id, status="cancelling")
        services.repository.event(turn_id, "cancel_requested", {"job_id": job_id})
        return jsonify({"ok": True, "turn": services.repository.get_turn(turn_id, owner_id), "cancel_requested": True})

    @app.route("/api/mail/research-turns/<turn_id>/retry", methods=["POST"])
    @_mail_endpoint
    def retry_mail_research_turn(turn_id):
        owner_id = _owner(services)
        old_turn = services.repository.get_turn(turn_id, owner_id)
        if not old_turn:
            raise LookupError("邮件研究轮次不存在或不属于当前用户")
        if old_turn['status'] not in {'failed','cancelled','completed'}:
            raise ValueError('原任务仍在运行，请先完成或停止')
        session = services.repository.get_session(old_turn["session_id"], owner_id)
        package = _package(services, session["package_id"], owner_id)
        retry_spec=dict(old_turn['task_spec'])
        if retry_spec.get('mode') in {'message_dossier','pair_dossier','intent_screen','person_correspondence'}:
            raise ValueError('请在单封、双方或人物分析入口重新分析，以重新固定资料与引擎版本')
        retry_spec.update(source_revision=package['source_revision'],index_epoch=package['index_epoch'])
        defaults=research_budget()
        prior=retry_spec.get('budget') or {}
        retry_spec['budget']={k:max(v,prior.get(k,v)) for k,v in defaults.items()}
        if getattr(Config,'MAIL_PHASE2_ENABLED',True):
            retry_spec['research_version']=RESEARCH_VERSION
            retry_spec['analysis_version']=3
        else:retry_spec.pop('research_version',None)
        new_turn = services.repository.create_turn(session["session_id"], owner_id, old_turn["question"], retry_spec)
        try:
            job_id, created = _queue(services, package["package_id"], "mail_research", owner_id, {
                "package_id": package["package_id"], "turn_id": new_turn["turn_id"], "workflow_source": "mail_research_retry",
            })
            if not created:
                raise ValueError('当前资料包已有研究任务，请稍后重新分析')
        except Exception as exc:
            services.repository.update_turn(new_turn['turn_id'],owner_id,status='failed',error={'message':str(exc)[:300]})
            raise
        services.repository.update_turn(new_turn["turn_id"], owner_id, job_id=job_id)
        return jsonify({"ok": True, "accepted": True, "turn": services.repository.get_turn(new_turn["turn_id"], owner_id),
                        "job_id": job_id, "reused_active_job": not created, "restart": True,
                        "budget_note": "新轮次从头分析并使用新的预算；断点续接请使用恢复接口",
                        "status_url": "/api/jobs/{}".format(job_id)}), 202 if created else 200

    @app.route('/api/mail/research-turns/<turn_id>/resume', methods=['POST'])
    @_mail_endpoint
    def resume_mail_research_turn(turn_id):
        owner_id=_owner(services);repo=services.repository
        turn=repo.get_turn(turn_id,owner_id)
        if not turn:
            raise LookupError('研究轮次不存在或不属于当前用户')
        spec=dict(turn['task_spec'])
        if spec.get('mode') in {'message_dossier','pair_dossier','intent_screen','person_correspondence'}:
            raise ValueError('请使用对应意图或人物分析的恢复接口')
        if spec.get('research_version')!=RESEARCH_VERSION:
            raise ValueError('分析引擎已变化，请重新分析')
        if turn['status'] not in {'failed','cancelled','completed'}:
            raise ValueError('任务仍在运行，请先完成或停止')
        if turn['status']=='completed' and turn.get('sufficiency')=='sufficient':
            raise ValueError('该研究已完成，可重新分析')
        package=_package(services,spec['package_id'],owner_id)
        if package['source_revision']!=spec['source_revision'] or package['index_epoch']!=spec['index_epoch']:
            raise ValueError('资料已变化，请重新分析')
        payload=request.get_json(silent=True) or {}
        if not isinstance(payload,dict) or not isinstance(payload.get('budget',{}),dict):
            raise ValueError('恢复请求和预算必须是对象')
        old_budget=dict(spec.get('budget') or {})
        if any(k!='budget' for k in payload):
            raise ValueError('恢复只允许增加预算，分析范围保持原样')
        budget=research_budget(payload.get('budget',{}),previous=old_budget)
        if budget==old_budget:
            raise ValueError('请增加至少一项研究预算后恢复')
        spec['budget']=budget
        next_spec=json.dumps(spec,ensure_ascii=False,sort_keys=True)
        with repo._connection() as c:
            before=c.execute('SELECT status,task_spec_json,job_id,error_json FROM mail_research_turns WHERE turn_id=? AND owner_id=?',(turn_id,owner_id)).fetchone()
            if not before or before['status']!=turn['status'] or json.loads(before['task_spec_json'])!=turn['task_spec']:
                raise ValueError('分析状态已变化，请刷新后恢复')
            claimed=c.execute("UPDATE mail_research_turns SET task_spec_json=?,status='queued',job_id=NULL,error_json=NULL WHERE turn_id=? AND owner_id=? AND status=? AND task_spec_json=? AND job_id IS ?",
                (next_spec,turn_id,owner_id,before['status'],before['task_spec_json'],before['job_id'])).rowcount
        if not claimed:raise ValueError('分析已经恢复，请刷新后查看')
        try:
            job_id,created=_queue(services,package['package_id'],'mail_research',owner_id,
                {'package_id':package['package_id'],'turn_id':turn_id,'workflow_source':'mail_research_resume'})
            if not created:raise ValueError('当前资料包已有研究任务，请稍后恢复')
            repo.update_turn(turn_id,owner_id,job_id=job_id)
        except Exception:
            with repo._connection() as c:
                c.execute("UPDATE mail_research_turns SET task_spec_json=?,status=?,job_id=?,error_json=? WHERE turn_id=? AND owner_id=? AND status='queued' AND task_spec_json=? AND job_id IS NULL",
                    (before['task_spec_json'],before['status'],before['job_id'],before['error_json'],turn_id,owner_id,next_spec))
            raise
        return jsonify({'ok':True,'turn_id':turn_id,'job_id':job_id,'resumed':True,
            'budget':budget,'budget_note':'累计此前调用、token、工具、时间和补检消耗；重新检索并核验当前证据'}),202

    @app.route("/api/mail/result-sets/<result_set_id>/filter", methods=["POST"])
    @_mail_endpoint
    def filter_mail_result_set(result_set_id):
        owner_id = _owner(services)
        payload = request.get_json(silent=True) or {}
        has_attachment = payload.get("has_attachment")
        if not isinstance(has_attachment, bool):
            raise ValueError("has_attachment 必须为布尔值")
        child_id = services.repository.filter_result_set(result_set_id, owner_id, has_attachment=has_attachment)
        return jsonify({"ok": True, "result_set": services.repository.get_result_set(child_id, owner_id)}), 201

    @app.route("/api/mail/evidence/<evidence_id>")
    @_mail_endpoint
    def mail_evidence(evidence_id):
        owner_id = _owner(services)
        evidence = services.repository.get_evidence(evidence_id, owner_id)
        if not evidence:
            raise LookupError("邮件证据不存在或不属于当前用户")
        return jsonify({"ok": True, "evidence": evidence})

    @app.route("/api/mail/evidence/<evidence_id>/translate", methods=["POST"])
    @_mail_endpoint
    def translate_mail_evidence(evidence_id):
        owner_id = _owner(services)
        evidence = services.repository.get_evidence(evidence_id, owner_id)
        if not evidence:
            raise LookupError("邮件证据不存在或不属于当前用户")
        from .presentation import translate_entries
        values = {}
        translate_entries(services, {"body": evidence.get("quote_text") or ""}, lambda key,value: values.update({key:value}))
        result = values["body"]
        return jsonify({"ok": True, "evidence_id": evidence_id, "translation": {**result, "target_language": "zh-CN"}})

    @app.route("/api/mail/presentation", methods=["POST"])
    @_mail_endpoint
    def mail_chinese_presentation():
        from .presentation import request_presentation
        value = request_presentation(services, _owner(services), request.get_json(silent=True) or {})
        return jsonify({"ok": True, "presentation": value})

    @app.route("/api/mail/exports", methods=["POST"])
    @_mail_endpoint
    def export_mail_result_set():
        owner_id = _owner(services)
        payload = request.get_json(silent=True) or {}
        result_set_id = str(payload.get("result_set_id") or "")
        result_set = services.repository.get_result_set(result_set_id, owner_id)
        if not result_set:
            raise LookupError("结果集不存在或不属于当前用户")
        format_name = str(payload.get("format") or "csv").casefold()
        if format_name not in {"csv", "html", "xlsx"}:
            raise ValueError("导出格式仅支持 csv、html 或 xlsx")
        job_id, created = _queue(services, result_set["package_id"], "mail_export", owner_id, {
            "package_id": result_set["package_id"], "result_set_id": result_set_id,
            "format": format_name, "workflow_source": "mail_export",
        })
        return jsonify({"ok": True, "accepted": True, "job_id": job_id, "reused_active_job": not created,
                        "status_url": "/api/jobs/{}".format(job_id)}), 202 if created else 200

    @app.route("/api/mail/packages/<package_id>/conversations")
    @_mail_endpoint
    def mail_conversations(package_id):
        from .conversations import list_conversations
        scope=_scope();scope['address']=scope.pop('participant')
        return jsonify({"ok":True,"items":list_conversations(services.repository,package_id,_owner(services),**scope)})

    @app.route("/api/mail/conversations/<conversation_id>")
    @_mail_endpoint
    def mail_conversation(conversation_id):
        from .conversations import detail
        scope=_scope();scope['address']=scope.pop('participant')
        value=detail(services.repository,conversation_id,_owner(services),**scope)
        if not value:raise LookupError("邮件会话不存在")
        return jsonify({"ok":True,"conversation":value})

    @app.route("/api/mail/packages/<package_id>/relations/network")
    @_mail_endpoint
    def mail_network(package_id):
        from .network_service import overview
        return jsonify({"ok":True,"network":overview(services.repository,package_id,_owner(services),**_scope(),
            conversation_id=request.args.get('conversation_id'),view=request.args.get('view') or 'mailbox',
            issue_id=request.args.get('issue_id'),as_of=request.args.get('as_of'),limit=request.args.get('limit',30))})

    @app.route("/api/mail/packages/<package_id>/entities")
    @_mail_endpoint
    def mail_entities(package_id):
        from .graph_metrics import entities
        return jsonify({"ok":True,"items":entities(services.repository,package_id,_owner(services))})

    @app.route("/api/mail/entities/confirm", methods=["POST"])
    @_mail_endpoint
    def mail_confirm_entity():
        from .graph_metrics import confirm_entity
        data=request.get_json(silent=True) or {}
        eid=confirm_entity(services.repository,str(data.get("package_id") or ""),_owner(services),data.get("display_name"),data.get("addresses"))
        return jsonify({"ok":True,"entity_id":eid}),201

    @app.route("/api/mail/packages/<package_id>/attachment-versions")
    @_mail_endpoint
    def mail_versions(package_id):
        from .versions import trace
        return jsonify({"ok":True,**trace(services.repository,package_id,_owner(services),request.args.get("chain_key"),request.args.get("message_key"))})

    @app.route("/api/mail/packages/<package_id>/facts")
    @_mail_endpoint
    def mail_facts(package_id):
        from .facts import list_facts,detect_conflicts
        if not Config.MAIL_ENABLE_FACT_EXTRACTION:return jsonify({"ok":True,"items":[],"conflicts":[],"enabled":False})
        owner_id=_owner(services)
        return jsonify({"ok":True,"items":list_facts(services.repository,package_id,owner_id),"conflicts":detect_conflicts(services.repository,package_id,owner_id)})

    @app.route("/api/mail/packages/<package_id>/coverage")
    @_mail_endpoint
    def mail_coverage(package_id):
        from .coverage import coverage_details
        return jsonify({"ok":True,**coverage_details(services.repository,package_id,_owner(services))})

    @app.route("/api/mail/packages/<package_id>/result-sets")
    @_mail_endpoint
    def mail_result_sets(package_id):
        return jsonify({"ok":True,"items":services.repository.list_result_sets(package_id,_owner(services))})

    @app.route("/api/mail/result-sets/<result_set_id>")
    @_mail_endpoint
    def mail_result_set(result_set_id):
        value=services.repository.get_result_set(result_set_id,_owner(services))
        if not value:raise LookupError("结果集不存在")
        return jsonify({"ok":True,"result_set":value})

    from .workspace_guide import register_workspace_guide_routes
    register_workspace_guide_routes(app, services)
