"""Project existing MIME parsing into ordinary document evidence, without mail writes."""
import hashlib

from config import Config
from services.mail.parsers.eml import parse_eml
from services.mail.policy import MailPolicy

_NL = chr(10)
_EVIDENCE_LIMIT = 3000
_CHUNK_SIZE = 1200
_HEADERS = (('subject', '主题'), ('from', '发件人'), ('sender', '代发人'),
            ('to', '收件人'), ('cc', '抄送'), ('bcc', '密送'),
            ('reply-to', '回复地址'), ('date', '日期'), ('message-id', '邮件标识'),
            ('in-reply-to', '回复邮件'), ('references', '关联邮件'))


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def project_eml(path, base, max_chars):
    """Keep all outer body parts; never promote attachments or forwarded bodies."""
    policy = MailPolicy.from_config(Config)
    policy.validate_size(path.stat().st_size, limit=policy.max_message_bytes, label='邮件')
    with path.open('rb') as stream:
        raw = stream.read(policy.max_message_bytes + 1)
    policy.validate_size(len(raw), limit=policy.max_message_bytes, label='邮件')
    if hashlib.sha256(raw).hexdigest() != base['source']['sha256']:
        raise ValueError('邮件在解析期间发生变化，请重新扫描，不能沿用旧文件哈希')
    mail = parse_eml(raw, max_parts=policy.max_mime_parts,
                     max_nested_message_depth=policy.max_nested_message_depth)
    del raw
    bodies = [p for p in mail.parts if p.part_role in {'body_plain', 'body_html'}]
    readable = [p for p in bodies if (p.text or '').strip()]
    external = [p for p in mail.parts if p.part_role not in {'body_plain', 'body_html'}]
    headers = [label + ': ' + value for key, label in _HEADERS
               for value in mail.headers_raw.get(key, [])]
    sections = [(_NL.join(headers), None)] if headers else []
    sections.extend(('[邮件正文 · MIME ' + p.mime_path + ' · ' + p.content_type + ']'
                     + _NL + p.text, p) for p in readable)
    extracted = sum(len(text) for text, _ in sections) + max(0, len(sections) - 1) * 2
    limit = max(0, int(max_chars))
    pieces, evidence = [], []
    stored = 0
    evidence_limited = False
    for text, part in sections:
        separator = _NL * 2 if pieces else ''
        if stored + len(separator) >= limit:
            break
        text = text[:max(0, limit - stored - len(separator))]
        if not text:
            break
        offset = stored + len(separator)
        pieces.append(separator + text)
        stored = offset + len(text)
        for start in range(0, len(text), _CHUNK_SIZE):
            if len(evidence) >= _EVIDENCE_LIMIT:
                evidence_limited = True
                break
            snippet = text[start:start + _CHUNK_SIZE]
            evidence.append({
                'evidence_id': 'E-{}-{:05d}'.format(base['source']['sha256'][:10], len(evidence) + 1),
                'source_path': base['source']['path'], 'source_sha256': base['source']['sha256'],
                'content_sha256': _digest(snippet), 'text': snippet,
                'character_range': [offset + start, offset + start + len(snippet)],
                'page': None, 'bbox': None, 'parser': 'rfc5322-mime',
                'section': '邮件正文' if part else '邮件头',
                'label': 'mail_body' if part else 'mail_header',
                'evidence_role': 'body' if part else 'structure',
                'mime_path': part.mime_path if part else None,
                'content_type': part.content_type if part else 'message/headers',
            })
    text = ''.join(pieces)
    truncated = len(text) < extracted
    warnings = list(mail.warnings)
    if not readable:
        warnings.append('未获得可读的外层邮件正文；邮件头不代表正文解析成功。')
    replaced = any(chr(0xfffd) in (p.text or '') for p in bodies) or any(chr(0xfffd) in h for h in headers)
    if replaced:
        warnings.append('邮件解码含替代字符，请对照原始邮件核验；不能视为完整提取。')
    if truncated:
        warnings.append('邮件正文投影超过字符上限；仅保存有界内容，不代表完整邮件。')
    if evidence_limited:
        warnings.append('邮件证据块达到上限；未全部建立引用，不能视为完整证据覆盖。')
    complete = bool(readable) and not warnings
    if external:
        warnings.append('此处只解析外层邮件正文；附件、内嵌资源和转发邮件仅保留部件清单，需单独分析。')
    base.update(text=text, content_sha256=_digest(text), evidence=evidence)
    base['warnings'].extend(warnings)
    base['parser'].update(name='rfc5322-mime', degraded=not complete, ocr=False,
                          remote_services_enabled=False)
    base['coverage'].update(
        complete=complete, extracted_characters=extracted, stored_characters=len(text),
        embedded_objects_covered=not external and not mail.warnings,
        truncated_by_limit=truncated, limited_by_fast_mode=False,
        evidence_complete=not evidence_limited and not truncated and bool(readable) and not mail.warnings and not replaced,
        coverage_ratio=1.0 if complete and not external else None,
        scope='outer_mail_body', body_parts=len(bodies), readable_body_parts=len(readable),
    )
    base['structure'].update(title=' / '.join(mail.headers_raw.get('subject', [])) or path.stem,
        mail={
            'parse_status': mail.parse_status, 'body_parts': len(bodies),
            'message_id': mail.message_id_raw, 'in_reply_to': mail.in_reply_to_raw,
            'references': mail.references_raw, 'sent_at_utc': mail.sent_at_utc,
            'parts': [{'mime_path': p.mime_path, 'parent_mime_path': p.parent_mime_path,
                       'role': p.part_role, 'content_type': p.content_type,
                       'filename': p.filename_raw, 'size': p.byte_size,
                       'sha256': p.payload_sha256} for p in mail.parts],
        })
    return base
