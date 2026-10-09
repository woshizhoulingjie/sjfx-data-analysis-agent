"""Deterministic original-content storage and package processing projections."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

VERSION = "mail-foundation-v4-reading"


def source_inventory(source, policy, adapter):
    """Retain non-mail and policy-rejected entries instead of hiding them."""
    from pathlib import Path
    from .models import MailMember
    import zipfile
    path = Path(source.path)
    if path.is_dir():
        candidates = []
        for candidate in path.rglob('*'):
            try:
                if candidate.is_dir():continue
            except OSError:
                pass
            candidates.append(candidate)
            if len(candidates)>policy.max_archive_entries:break
        candidates.sort()
        if len(candidates) > policy.max_archive_entries:
            raise ValueError('邮件目录成员数超过限制')
        for ordinal, p in enumerate(candidates):
            name = p.relative_to(path).as_posix()
            error = None
            size = 0
            try:
                p.resolve().relative_to(path.resolve())
                size=p.stat().st_size
                policy.validate_size(size,limit=policy.max_message_bytes,label='邮件')
            except (ValueError, OSError) as exc:
                error = str(exc)
            yield MailMember(name, ordinal, size, {'kind':'file','path':str(p)}), ('restricted' if error else 'pending' if p.suffix.casefold()=='.eml' else 'unsupported'), error
    elif path.suffix.casefold()=='.zip':
        with zipfile.ZipFile(path) as archive:
            infos = [i for i in archive.infolist() if not i.is_dir()]
            if len(infos) > policy.max_archive_entries:
                raise ValueError('压缩包成员数超过限制')
            total = 0
            for ordinal, info in enumerate(infos):
                total += info.file_size
                error = None
                try:
                    policy.validate_member_name(info.filename)
                    policy.validate_archive_member(info.compress_size,info.file_size)
                    if info.flag_bits & 1:raise ValueError('压缩成员加密')
                    if total > policy.max_total_uncompressed_bytes:raise ValueError('压缩包解压总大小超过限制')
                except ValueError as exc:
                    error = str(exc)
                yield MailMember(info.filename, ordinal, info.file_size, {'kind':'zip','path':str(path),'member':info.filename,'header_offset':info.header_offset}), ('restricted' if error else 'pending' if info.filename.casefold().endswith('.eml') else 'unsupported'), error
    else:
        for member in adapter.inventory(source,policy):
            yield member,'pending',None


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


_ORIGINAL_MARKER = re.compile(r"(?i)^\s*-{2,}\s*(?:original message|原始邮件)\b")
_FORWARD_MARKER = re.compile(r"(?i)^\s*(?:-{2,}\s*(?:forwarded message|转发邮件)|begin forwarded message)")
_REPLY_MARKER = re.compile(r"(?i)^\s*(?:on .+wrote:|.*เขียนว่า\s*:|在.+写道\s*[:：])\s*$")


def _structured_signature_start(lines):
    """Fold only a final structured contact block, never a greeting alone."""
    end = next((i for i, line in enumerate(lines) if _ORIGINAL_MARKER.search(line)
                or _FORWARD_MARKER.search(line) or _REPLY_MARKER.search(line)), len(lines))
    for index in range(max(0, end - 10), end):
        line = lines[index].strip()
        if not re.fullmatch(r"[^|｜\n]{1,40}\s*[|｜]\s*[^|｜\n]{1,80}", line):
            continue
        tail = ''.join(lines[index + 1:end])
        company = bool(re.search(r"有限公司|公司|\b(?:company|ltd|limited|inc)\b", tail, re.I))
        email = bool(re.search(r"(?:邮箱|電子郵件|email|e-mail)\s*[:：]\s*[^\s<>]+@[^\s<>]+", tail, re.I))
        telephone = bool(re.search(r"(?:电话|電話|手机|tel(?:ephone)?|mobile|phone)\s*[:：]\s*\+?[\d ()-]{5,}", tail, re.I))
        address = bool(re.search(r"(?:地址|address)\s*[:：]\s*\S", tail, re.I))
        # All material after the contact block must be footer material. A
        # business paragraph after it would make this boundary uncertain.
        contact_lines = [value.strip() for value in lines[index + 1:end] if value.strip()]
        labels = r"(?:邮箱|電子郵件|email|e-mail|电话|電話|手机|tel(?:ephone)?|mobile|phone|地址|address)\s*[:：]"
        footer_only = all(re.search(r"有限公司|公司|\b(?:company|ltd|limited|inc)\b", value, re.I)
                          or re.match(labels, value, re.I) for value in contact_lines)
        if company and email and (telephone or address) and footer_only:
            return index
    return None


def fragments(text):
    """Preserve every character; labels are a conservative reading projection."""
    result = []
    offset = 0
    section = "authored"
    lines = str(text or "").splitlines(keepends=True)
    signature_start = _structured_signature_start(lines)
    for index, line in enumerate(lines):
        if _FORWARD_MARKER.search(line):
            section = "forwarded"
        elif _ORIGINAL_MARKER.search(line):
            section = "quoted"
        elif _REPLY_MARKER.search(line):
            # A reply marker does not prove every later unprefixed line was
            # written by the quoted sender: inline replies are common. Keep
            # the whole text and expose that ambiguity in the reading view.
            section = "uncertain"
        elif re.match(r"^-- \s*$", line) or index == signature_start and section == "authored":
            section = "signature"
        kind = "quoted" if re.match(r"^\s*>+", line) else section
        if section == "authored" and re.match(r"(?i)^\s*(?:From|发件人|寄件者)\s*[:：]", line):
            kind = "uncertain"
        if result and result[-1]["fragment_kind"] == kind:
            result[-1]["text"] += line
            result[-1]["char_end"] += len(line)
        else:
            result.append({"ordinal": len(result), "fragment_kind": kind, "text": line,
                "char_start": offset, "char_end": offset + len(line), "method": "line_boundary",
                "confidence": "uncertain" if kind in {"authored", "uncertain"} else "marker_based"})
        offset += len(line)
    return result


def _reader_parts(parsed, metadata=None, *, nested_depth=0, max_nested_depth=3, cid_urls=None):
    """Complete text and bounded, local-only embedded images for the reader."""
    import base64
    from html import escape
    from urllib.parse import unquote
    from .html_safety import _SafeMailHTML, sanitize_html
    from .parsers.eml import parse_eml

    metadata = metadata or {}
    image_types = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    images = {}
    duplicates = set()
    image_urls = {}
    total_image_bytes = 0
    for part in parsed.parts:
        if not part.is_inline_resource or part.content_type not in image_types:
            continue
        if part.byte_size > 2 * 1024 * 1024 or total_image_bytes + part.byte_size > 8 * 1024 * 1024:
            continue
        total_image_bytes += part.byte_size
        image_urls[part.mime_path] = "data:{};base64,{}".format(part.content_type, base64.b64encode(part.raw_payload or b"").decode("ascii"))
        cid = str(part.content_id or "").strip().strip("<>")
        if cid:
            if cid in images:
                duplicates.add(cid)
            else:
                images[cid] = image_urls[part.mime_path]
    for cid in duplicates:
        images.pop(cid, None)

    class ReaderHTML(_SafeMailHTML):
        def handle_starttag(self, tag, attrs):
            if tag.casefold() == "img" and not self._drop_depth:
                values = dict(attrs)
                source = str(values.get("src") or "")
                cid = unquote(source[4:]).strip().strip("<>") if source[:4].casefold() == "cid:" else None
                if cid and cid in images:
                    self.parts.append('<img src="{}" alt="{}">'.format(images[cid], escape(str(values.get("alt") or "内嵌图片"), quote=True)))
                elif cid:
                    self.parts.append('<span>[{}]</span>'.format(escape("内嵌图片未能预览：" + cid)))
                return
            super().handle_starttag(tag, attrs)

    result = []
    for part in parsed.parts:
        item = {"mime_path": part.mime_path, "parent_mime_path": part.parent_mime_path,
            "part_role": part.part_role, "filename_raw": part.filename_raw,
            "content_type": part.content_type, "content_disposition": part.content_disposition,
            "byte_size": part.byte_size, "payload_sha256": part.payload_sha256,
            "content_id": part.content_id, "charset": part.charset,
            "is_explicit_attachment": part.is_explicit_attachment,
            "is_inline_resource": part.is_inline_resource, "content_complete": part.content_complete,
            "warnings": list(part.warnings)}
        state = metadata.get(part.mime_path, {})
        for key in ("processing_status", "processing", "error", "coverage_ratio", "extracted_chars",
                    "text_origin","text_provenance","source_text_notice"):
            if key in state:
                item[key] = state[key]
        item["warnings"].extend(state.get("warnings") or [])
        if part.part_role in {"body_plain", "body_html"}:
            safe_html = None
            if part.html_raw is not None:
                if cid_urls is not None:
                    safe_html = sanitize_html(part.html_raw, cid_urls=cid_urls)
                else:
                    reader = ReaderHTML(); reader.feed(part.html_raw); reader.close()
                    safe_html = "".join(reader.parts)
            item.update(text=part.text or "", html=safe_html, html_raw=part.html_raw,
                fragments=fragments(part.text))
        if part.mime_path in image_urls:
            item["preview_data_url"] = image_urls[part.mime_path]
        elif part.is_inline_resource and part.content_type.startswith("image/"):
            item["preview_status"] = "原文件可下载；该图片格式或大小不支持内嵌预览"
        if part.part_role == "nested_message":
            if nested_depth >= max_nested_depth:
                item["nested"] = {"complete": False, "warnings": ["嵌套邮件阅读深度超过限制；全部内容仍保留在原文件中"], "parts": []}
            else:
                nested = parse_eml(part.raw_payload or b"")
                item["nested"] = {"headers": nested.headers_raw, "warnings": nested.warnings,
                    "sent_at_utc": nested.sent_at_utc, "date_raw": nested.date_raw,
                    "participants": [{"role": r, "address": a, "position": p, "display_name": n} for r, a, p, n in nested.participants],
                    "complete": nested.content_complete,
                    "parts": _reader_parts(nested, nested_depth=nested_depth+1, max_nested_depth=max_nested_depth)}
        result.append(item)
    return result


def retain_content(connection, message_key, raw, parsed):
    digest = hashlib.sha256(raw).hexdigest()
    if digest != parsed.raw_sha256:
        raise ValueError("原始邮件内容哈希不一致")
    connection.execute("INSERT OR IGNORE INTO mail_raw_store(raw_sha256,raw_bytes) VALUES (?,?)", (digest, raw))
    # Replace the complete reading projection within the caller transaction.
    # Parser upgrades may retire a MIME body path (for example text inside a
    # multipart attachment); retaining that stale path would misattribute it.
    connection.execute("DELETE FROM mail_body_fragments WHERE message_key=?", (message_key,))
    for part in parsed.parts:
        if part.part_role not in {"body_plain", "body_html"}:
            continue
        connection.executemany(
            "INSERT INTO mail_body_fragments(message_key,mime_path,ordinal,fragment_kind,char_start,char_end,text,method,confidence) VALUES (?,?,?,?,?,?,?,?,?)",
            [(message_key, part.mime_path, row["ordinal"], row["fragment_kind"], row["char_start"], row["char_end"], row["text"], row["method"], row["confidence"]) for row in fragments(part.text)],
        )


def inventory_entry(repository, package_id, member, status, *, message_key=None, error=None):
    with repository._connection() as c:
        c.execute("INSERT INTO mail_import_entries(package_id,member_name,member_ordinal,byte_size,status,message_key,error_json,locator_json) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(package_id,member_name,member_ordinal) DO UPDATE SET status=excluded.status,message_key=excluded.message_key,error_json=excluded.error_json,updated_at=CURRENT_TIMESTAMP",
            (package_id, member.name, member.ordinal, member.byte_size, status, message_key, dumps(error) if error else None, dumps(member.locator)))


def inventory(repository, package_id, owner_id, *, offset=0, limit=50, status=None):
    if not repository.get_package(package_id, owner_id):
        raise LookupError("邮件资料包不存在")
    clauses = ["package_id=?"]
    args = [package_id]
    if status:
        clauses.append("status=?"); args.append(str(status))
    with repository._connection() as c:
        where = " AND ".join(clauses)
        total = c.execute("SELECT COUNT(*) FROM mail_import_entries WHERE " + where, args).fetchone()[0]
        rows = [dict(r) for r in c.execute("SELECT member_name,member_ordinal,byte_size,status,message_key,error_json,updated_at FROM mail_import_entries WHERE " + where + " ORDER BY member_ordinal,member_name LIMIT ? OFFSET ?", args + [limit, offset])]
    for row in rows:
        row["error"] = json.loads(row.pop("error_json") or "null")
    return {"items": rows, "total": total, "offset": offset, "limit": limit}


def set_state(repository, package_id, stage, status, *, cursor=None, error=None, fingerprint=None):
    with repository._connection() as c:
        c.execute("INSERT INTO mail_foundation_state(package_id,stage,version,fingerprint,status,cursor_json,error_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT(package_id,stage) DO UPDATE SET version=excluded.version,fingerprint=excluded.fingerprint,status=excluded.status,cursor_json=excluded.cursor_json,error_json=excluded.error_json,updated_at=CURRENT_TIMESTAMP",
            (package_id, stage, VERSION, fingerprint, status, dumps(cursor or {}), dumps(error) if error else None))


def backfill_slice(repository, package_id, owner_id, *, job_id=None, cancelled=lambda: False, limit=50):
    """Resumable historical upgrade; original data and human corrections survive."""
    from .attachments import read_message_raw
    from .parsers.eml import parse_eml
    package = repository.get_package(package_id, owner_id)
    if not package:
        raise LookupError("邮件资料包不存在")
    with repository._connection() as c:
        has_messages=c.execute('SELECT 1 FROM mail_visible_occurrences WHERE package_id=? LIMIT 1',(package_id,)).fetchone()
        include_nested=int(bool(package.get('config',{}).get('include_nested_messages',False)))
        nested_change=c.execute("SELECT 1 FROM mail_occurrences o JOIN mail_sources s ON s.source_id=o.source_id WHERE o.package_id=? AND json_extract(s.locator_json,'$.kind')='nested' AND o.is_countable<>? LIMIT 1",(package_id,include_nested)).fetchone()
        if include_nested and nested_change:
            c.execute("UPDATE mail_occurrences SET is_countable=1 WHERE package_id=? AND source_id IN (SELECT source_id FROM mail_sources WHERE package_id=? AND json_extract(locator_json,'$.kind')='nested')",(package_id,package_id))
            has_messages=c.execute('SELECT 1 FROM mail_visible_occurrences WHERE package_id=? LIMIT 1',(package_id,)).fetchone()
        state = c.execute("SELECT cursor_json,status,version,fingerprint FROM mail_foundation_state WHERE package_id=? AND stage='historical_backfill'", (package_id,)).fetchone()
        reset_nested=bool(nested_change and state and state[1] in ('ready','partial'))
        checkpoint = json.loads(state[0] or '{}') if state and state[2]==VERSION and state[3]==package['source_revision'] and not reset_nested else {}
    if not has_messages:
        set_state(repository,package_id,'historical_backfill','skipped',error={'message':'没有已导入邮件，请先完成邮件导入'},fingerprint=package['source_revision'])
        return {'package_id':package_id,'status':'skipped','processed':0}
    cursor = checkpoint.get('last_message_key', '')
    if state and state[1] == 'ready' and state[2] == VERSION and state[3]==package['source_revision'] and not nested_change:
        return {'package_id':package_id,'status':'ready','already_current':True}
    with repository._connection() as c:
        rows = [dict(r) for r in c.execute("SELECT m.* FROM mail_messages m WHERE m.message_key>? AND EXISTS(SELECT 1 FROM mail_visible_occurrences o WHERE o.package_id=? AND o.message_key=m.message_key) ORDER BY m.message_key LIMIT ?", (cursor,package_id,limit))]
    errors = int(checkpoint.get('errors',0)); processed = int(checkpoint.get('processed',0))
    set_state(repository,package_id,'historical_backfill','running',cursor=checkpoint,fingerprint=package['source_revision'])
    for message in rows:
        if cancelled():
            set_state(repository,package_id,'historical_backfill','skipped',cursor=checkpoint,error={'message':'已取消，恢复点保留'},fingerprint=package['source_revision'])
            raise RuntimeError('邮件基础回填已取消')
        key=message['message_key']
        try:
            raw=read_message_raw(repository,key,owner_id,package_id)
            parsed=parse_eml(raw)
            with repository._connection() as c:
                retain_content(c,key,raw,parsed)
                repository._insert_parts(c,key,parsed.parts)
                repository._insert_participants(c,key,parsed.participants)
                # Rebuild the body projection transactionally, retaining matching stable IDs.
                old={r[0] for r in c.execute("SELECT segment_id FROM mail_text_segments WHERE package_id=? AND message_key=? AND source_kind='body'",(package_id,key))}
                repository._insert_body_segments(c,package_id,key,parsed.parts,package['source_revision'])
                selected=[p for p in parsed.parts if p.part_role=='body_plain' and (p.text or '').strip()]
                if not selected:selected=[p for p in parsed.parts if p.part_role=='body_html' and (p.text or '').strip()]
                current={'mseg_'+hashlib.sha256(dumps([package_id,key,p.mime_path,'body',ordinal,p.text[start:start+repository.text_segment_chars]]).encode()).hexdigest() for p in selected for ordinal,start in enumerate(range(0,len(p.text),repository.text_segment_chars))}
                for sid in old-current:
                    for table in ('mail_search_fts','mail_embeddings','mail_embedding_buckets','mail_text_segments'):
                        c.execute('DELETE FROM '+table+' WHERE segment_id=?',(sid,))
                from .normalization import normalize_message_id, normalized_subject
                message_id=parsed.message_id_raw[0] if parsed.message_id_raw else None
                subject = repository._header(parsed, 'subject')
                c.execute("UPDATE mail_messages SET canonical_message_id_raw=?,canonical_message_id_normalized=?,subject_raw=?,subject_normalized=?,date_raw=?,sent_at_utc=?,timezone_offset_minutes=?,headers_json=?,warnings_json=?,parser_version='mail-eml-v3' WHERE message_key=?",(message_id,normalize_message_id(message_id),subject,normalized_subject(subject),parsed.date_raw,parsed.sent_at_utc,parsed.timezone_offset_minutes,dumps(parsed.headers_raw),dumps(parsed.warnings),key))
                plain=next((p.mime_path for p in parsed.parts if p.part_role=='body_plain'),None)
                html=next((p.mime_path for p in parsed.parts if p.part_role=='body_html'),None)
                preferred=next((p.mime_path for p in selected),None)
                c.execute('UPDATE mail_messages SET body_preferred_part=?,body_plain_part=?,body_html_part=?,parse_status=?,body_language=? WHERE message_key=?',(preferred,plain,html,parsed.parse_status,repository._language('\n'.join(p.text or '' for p in selected)),key))
                occurrences=c.execute("SELECT o.*,s.source_size,s.locator_json FROM mail_visible_occurrences o JOIN mail_sources s ON s.source_id=o.source_id WHERE o.package_id=? AND o.message_key=? ORDER BY o.source_member_ordinal,o.source_member_name",(package_id,key)).fetchall()
                for i,o in enumerate(occurrences):
                    c.execute("INSERT INTO mail_import_entries(package_id,member_name,member_ordinal,byte_size,status,message_key,locator_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT(package_id,member_name,member_ordinal) DO UPDATE SET status=excluded.status,message_key=excluded.message_key,error_json=NULL",(package_id,o['source_member_name'],o['source_member_ordinal'],o['source_size'],'ready' if i==0 else 'duplicate',key,o['locator_json']))
        except Exception as exc:
            errors+=1
            with repository._connection() as c:
                c.execute("INSERT INTO mail_import_entries(package_id,member_name,member_ordinal,byte_size,status,message_key,error_json) SELECT package_id,source_member_name,source_member_ordinal,0,'terminal_failed',message_key,? FROM mail_visible_occurrences WHERE package_id=? AND message_key=? ON CONFLICT(package_id,member_name,member_ordinal) DO UPDATE SET status='terminal_failed',error_json=excluded.error_json",(dumps({'message':str(exc)[:500]}),package_id,key))
        processed+=1;cursor=key
        checkpoint={'last_message_key':cursor,'processed':processed,'errors':errors}
        set_state(repository,package_id,'historical_backfill','running',cursor=checkpoint,fingerprint=package['source_revision'])
        if job_id:repository.save_progress(job_id,'foundation_backfill',checkpoint,{'processed':processed,'errors':errors})
    if len(rows)==limit:
        return {'package_id':package_id,'_requeue_slice':True,'_requeue_message':'邮件基础回填恢复点已保存',**checkpoint}
    # Add non-mail/restricted entries without changing the persisted occurrence locators.
    try:
        from .models import SourceRef
        from .policy import MailPolicy
        from .source import AdapterRegistry
        from config import Config
        source=SourceRef(package_id=package_id,kind=package['source_kind'],path=package['source_path'],source_revision=package['source_revision'])
        adapter=AdapterRegistry().resolve(source)
        for member,status,error in source_inventory(source,MailPolicy.from_config(Config),adapter):
            with repository._connection() as c:
                exists=c.execute('SELECT 1 FROM mail_import_entries WHERE package_id=? AND member_name=?',(package_id,member.name)).fetchone()
            if not exists:
                inventory_entry(repository,package_id,member,status,error={'message':error} if error else None)
    except Exception as exc:
        checkpoint['inventory_warning']=str(exc)[:500]
    with repository._connection() as c:
        c.execute("UPDATE mail_occurrences SET is_countable=? WHERE package_id=? AND source_id IN (SELECT source_id FROM mail_sources WHERE package_id=? AND json_extract(locator_json,'$.kind')='nested')",(include_nested,package_id,package_id))
        if not include_nested:
            c.execute("UPDATE mail_import_entries SET status='skipped',error_json=? WHERE package_id=? AND (member_name,member_ordinal) IN (SELECT o.source_member_name,o.source_member_ordinal FROM mail_occurrences o WHERE o.package_id=? AND o.is_countable=0)",(dumps({'reason':'嵌套邮件保留在外层 MIME 与原始来源中，默认不作为独立邮件计数'}),package_id,package_id))
            retired=[r[0] for r in c.execute('SELECT segment_id FROM mail_text_segments s WHERE s.package_id=? AND NOT EXISTS(SELECT 1 FROM mail_visible_occurrences o WHERE o.package_id=s.package_id AND o.message_key=s.message_key)',(package_id,))]
            for sid in retired:
                for table in ('mail_search_fts','mail_embeddings','mail_embedding_buckets','mail_text_segments'):
                    c.execute('DELETE FROM '+table+' WHERE segment_id=?',(sid,))
        else:
            c.execute("UPDATE mail_import_entries SET status='ready',error_json=NULL WHERE package_id=? AND status='skipped' AND (member_name,member_ordinal) IN (SELECT o.source_member_name,o.source_member_ordinal FROM mail_occurrences o JOIN mail_sources s ON s.source_id=o.source_id WHERE o.package_id=? AND json_extract(s.locator_json,'$.kind')='nested')",(package_id,package_id))
        if nested_change:
            c.execute('UPDATE mail_packages SET index_epoch=index_epoch+1 WHERE package_id=?',(package_id,))
            c.execute('UPDATE mail_text_segments SET index_epoch=(SELECT index_epoch FROM mail_packages WHERE package_id=?) WHERE package_id=?',(package_id,package_id))
    repository.finish_import(package_id,owner_id)
    set_state(repository,package_id,'historical_backfill','partial' if errors else 'ready',cursor=checkpoint,fingerprint=package['source_revision'])
    return {'package_id':package_id,'status':'partial' if errors else 'ready',**checkpoint}


def content(repository, message_key, owner_id, package_id):
    from .attachments import read_message_raw
    from .parsers.eml import parse_eml
    from .html_safety import cid_image_urls
    message = repository.get_message(message_key, owner_id, package_id)
    if not message:
        raise LookupError("邮件不存在或不属于当前资料包")
    # A reader may be entered directly from people/search before the structure
    # tab has ever been opened. Publish the current browsing projection first;
    # navigation is also useful when only historical body text remains.
    from .conversations import rebuild
    rebuild(repository, package_id, owner_id)
    with repository._connection() as c:
        conversations = [dict(r) for r in c.execute("SELECT v.conversation_id,v.subject_normalized FROM mail_conversations v JOIN mail_conversation_members cm ON cm.conversation_id=v.conversation_id WHERE v.package_id=? AND cm.message_key=?", (package_id, message_key))]
    edges = [edge for edge in repository.threads(package_id, owner_id) if edge["source_message_key"] == message_key or edge.get("target_message_key") == message_key]
    try:
        raw = read_message_raw(repository, message_key, owner_id, package_id)
    except (RuntimeError,ValueError) as exc:
        # Historical projections can still be read when their old source was lost.
        # They are explicitly incomplete, and cannot stand in for an original file.
        with repository._connection() as c:
            rows=[dict(r) for r in c.execute("SELECT mime_path,text FROM mail_text_segments WHERE package_id=? AND message_key=? AND source_kind='body' ORDER BY mime_path,ordinal",(package_id,message_key))]
        texts={}
        for row in rows:texts[row['mime_path']]=texts.get(row['mime_path'],'')+row['text']
        parts=[]
        for part in message['parts']:
            value=dict(part)
            if part['mime_path'] in texts:
                value.update(text=texts[part['mime_path']],html=None,fragments=fragments(texts[part['mime_path']]))
            parts.append(value)
        return {'message_key':message_key,'package_id':package_id,'parts':parts,'headers':message['headers'],'raw_headers':None,
            'raw_available':False,'complete':False,'original_preserved':False,'reading_projection_complete':False,
            'body_status':'historical_projection','warnings':message['warnings']+[str(exc),'当前显示历史保存的文本投影，无法确认它与完整原文一致'],
            'occurrences':message['occurrences'],'participants':message['participants'],'conversation_ids':conversations,
            'reply_edges':edges}
    parsed = parse_eml(raw)
    parts = _reader_parts(parsed, {p['mime_path']: p for p in message['parts']}, cid_urls=cid_image_urls(message_key, package_id, parsed.parts))
    body_parts = [p for p in parsed.parts if p.part_role.startswith('body_')]
    body_status = 'empty' if not any((p.text or '').strip() for p in body_parts) else 'ready' if all(p.content_complete for p in body_parts) and parsed.content_complete else 'partial'
    attachments = [p for p in parts if p['is_explicit_attachment']]
    attachment_text_complete = all(p.get('processing_status') == 'ready' and (p.get('coverage_ratio') is None or p['coverage_ratio'] >= 1) for p in attachments)
    import base64
    header_bytes = re.split(br"\r\n\r\n|\n\n|\r\r", raw, maxsplit=1)[0]
    try:
        header_raw = header_bytes.decode('utf-8')
        header_display_encoding = 'utf-8'
    except UnicodeError:
        # This byte-preserving display is reversible; readable decoded header
        # values remain available separately in ``headers``.
        header_raw = header_bytes.decode('latin-1')
        header_display_encoding = 'byte-preserving-latin-1'
    return {"message_key": message_key, "package_id": package_id, "parts": parts,"raw_available":True,"complete":True,
        "original_preserved":True,"reading_projection_complete":parsed.content_complete,
        "body_status":body_status,"attachment_text_complete":attachment_text_complete,
        "preservation_note":"原始邮件完整可回查；阅读投影与附件文本抽取状态分别展示",
        "headers": message["headers"], "raw_headers": header_raw, "warnings": parsed.warnings,
        "raw_headers_base64":base64.b64encode(header_bytes).decode('ascii'),
        "raw_headers_display_encoding":header_display_encoding,
        "occurrences": message["occurrences"], "participants": message["participants"],
        "conversation_ids": conversations, "reply_edges": edges,
        "parser_version": VERSION, "raw_sha256": parsed.raw_sha256}


def overview(repository, package_id, owner_id):
    package = repository.get_package(package_id, owner_id)
    if not package:
        raise LookupError("邮件资料包不存在")
    with repository._connection() as c:
        keys = "SELECT DISTINCT message_key FROM mail_visible_occurrences WHERE package_id=?"
        logical = c.execute("SELECT COUNT(*) FROM (" + keys + ")", (package_id,)).fetchone()[0]
        occurrences = c.execute("SELECT COUNT(*) FROM mail_visible_occurrences WHERE package_id=?", (package_id,)).fetchone()[0]
        archived_nested = c.execute('SELECT COUNT(*) FROM mail_occurrences WHERE package_id=? AND is_countable=0',(package_id,)).fetchone()[0]
        dates = c.execute("SELECT MIN(sent_at_utc),MAX(sent_at_utc),SUM(sent_at_utc IS NULL) FROM mail_messages WHERE message_key IN (" + keys + ")", (package_id,)).fetchone()
        raw_ready = c.execute("SELECT COUNT(*) FROM mail_messages m JOIN mail_raw_store r ON r.raw_sha256=m.raw_sha256 WHERE m.message_key IN (" + keys + ")", (package_id,)).fetchone()[0]
        entry_counts = dict(c.execute("SELECT status,COUNT(*) FROM mail_import_entries WHERE package_id=? GROUP BY status", (package_id,)))
        attachments = c.execute("SELECT COUNT(*),COUNT(DISTINCT payload_sha256) FROM mail_parts WHERE is_explicit_attachment=1 AND message_key IN (" + keys + ")", (package_id,)).fetchone()
        stages = {}
        stages['inventory'] = {'total':sum(entry_counts.values()),'processed':sum(n for status,n in entry_counts.items() if status not in {'pending','running'}),'by_status':entry_counts}
        for stage in ("basic_text", "ocr"):
            clause = " AND (p.is_explicit_attachment=1)"
            if stage == "ocr":
                clause += " AND (x.stage IS NOT NULL OR EXISTS(SELECT 1 FROM mail_part_processing b WHERE b.message_key=p.message_key AND b.mime_path=p.mime_path AND b.stage='basic_text' AND b.status='ocr_pending'))"
            rows = dict(c.execute("SELECT COALESCE(x.status,'not_started'),COUNT(*) FROM mail_parts p LEFT JOIN mail_part_processing x ON x.message_key=p.message_key AND x.mime_path=p.mime_path AND x.stage=? WHERE p.message_key IN (" + keys + ")" + clause + " GROUP BY COALESCE(x.status,'not_started')", (stage, package_id)))
            stages[stage] = {"total": sum(rows.values()), "by_status": rows}
        stages["raw_preservation"] = {"total": logical, "by_status": {"ready": raw_ready, "pending": logical - raw_ready}}
        parse_counts = dict(c.execute("SELECT parse_status,COUNT(*) FROM mail_messages WHERE message_key IN (" + keys + ") GROUP BY parse_status", (package_id,)))
        stages["mail_parse"] = {"total": logical, "by_status": parse_counts}
        indexed = c.execute("SELECT COUNT(DISTINCT message_key) FROM mail_text_segments WHERE package_id=? AND source_kind='body'", (package_id,)).fetchone()[0]
        text_total = c.execute("SELECT COUNT(*) FROM mail_messages WHERE body_preferred_part IS NOT NULL AND message_key IN (" + keys + ")", (package_id,)).fetchone()[0]
        stages["body_index"] = {"total": text_total, "by_status": {"ready": indexed, "pending": max(0, text_total - indexed)}}
        states = [dict(r) for r in c.execute("SELECT stage,version,fingerprint,status,cursor_json,error_json,updated_at FROM mail_foundation_state WHERE package_id=?", (package_id,))]
        conversations = c.execute("SELECT COUNT(*) FROM mail_conversations WHERE package_id=?", (package_id,)).fetchone()[0]
        singletons = c.execute("SELECT COALESCE(SUM(message_count),0) FROM mail_conversations WHERE package_id=? AND message_count=1", (package_id,)).fetchone()[0]
        people = c.execute("SELECT COUNT(*) FROM mail_entities WHERE package_id=?", (package_id,)).fetchone()[0]
        addresses = c.execute("SELECT COUNT(DISTINCT address_id) FROM mail_message_participants WHERE message_key IN (" + keys + ")", (package_id,)).fetchone()[0]
    from config import Config
    if Config.MAIL_ENABLE_VECTOR_INDEX:
        from .semantic import index_status
        vector = index_status(repository, package_id, owner_id)
        stages["vector_index"] = vector
    for state in states:
        state["cursor"] = json.loads(state.pop("cursor_json") or "{}")
        state["error"] = json.loads(state.pop("error_json") or "null")
    return {"package_id": package_id, "counts": {"logical_messages": logical, "occurrences": occurrences,
        "duplicate_copies": occurrences - logical, "archived_nested_copies":archived_nested,"inventory_entries": sum(entry_counts.values()),
        "explicit_attachments": attachments[0], "unique_attachment_files": attachments[1],
        "addresses": addresses, "confirmed_people": people, "conversations": conversations,
        "singleton_messages": singletons, "date_anomalies": dates[2] or 0},
        "time_range": {"from": dates[0], "to": dates[1]}, "inventory_by_status": entry_counts,
        "stages": stages, "versions": {"foundation": VERSION, "source_revision": package["source_revision"], "index_epoch": package["index_epoch"]},
        "states": states, "note": "处理完成不等于已经阅读或分析邮件内容"}
