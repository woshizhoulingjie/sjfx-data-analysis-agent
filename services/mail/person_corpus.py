"""Versioned, package-local person evidence including conversation context.

Identity comes only from an explicitly selected mailbox or a manual entity. The
immutable fingerprint covers actual text (not merely cached hashes), attribution,
attachment extraction and conversation membership used by the analysis.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from config import Config
from .people import authorize, resolve_person

VERSION = 2
ENGINE_VERSION = 'person-evidence-context-v2.2'
PERSON_DEFAULT_MODEL_CALL_BUDGET = Config.MAIL_PERSON_MAX_MODEL_CALLS
PERSON_MAX_MODEL_CALL_BUDGET = Config.MAIL_RESEARCH_MODEL_CALL_LIMIT
# Inline quote blocks can end. Forward headers/signatures conservatively taint
# the remainder, including every later segment of the same MIME body.
HARD_BOUNDARY = re.compile(r'(?im)^\s*(?:-{2,}\s*(?:original message|forwarded message|原始邮件|转发邮件)|original message|forwarded message|begin forwarded|from:|发件人:|原始邮件|转发邮件|ข้อความที่ส่งต่อ|on .{0,250}wrote\s*:|.{0,200}เขียนว่า\s*:|--\s*$|(?:best |kind |warm )?regards[,\s]*$|sincerely[,\s]*$|此致|祝好|ขอแสดงความนับถือ|sent from my |get outlook for |发自我的|此邮件及其附件|confidentiality notice)')
SOFT_QUOTE = re.compile(r'(?m)^\s*>.*(?:\n|$)')


def resolve_selection(repository, package_id, owner_id, address=None, entity_id=None):
    return resolve_person(repository, package_id, owner_id, address, entity_id)


def selection_for_spec(repository, spec, owner_id):
    selected = resolve_selection(repository, spec['package_id'], owner_id,
                                 None if spec.get('person_entity_id') else spec.get('person_address'), spec.get('person_entity_id'))
    expected = spec.get('person_addresses')
    if expected is not None and sorted(expected) != selected['addresses']:
        raise RuntimeError('人工确认的人物邮箱集合已变化，请重新分析')
    return selected


def _scope(package_id, addresses, related_keys=None, related_roots=None, full_package=False):
    if full_package:
        return 'SELECT DISTINCT message_key FROM mail_visible_occurrences WHERE package_id=?', [package_id]
    if related_roots is not None:
        if not related_roots:
            return 'SELECT message_key FROM mail_visible_occurrences WHERE 0', []
        marks=','.join('?' for _ in related_roots)
        return ('WITH threads AS (SELECT DISTINCT cm.conversation_id FROM mail_conversation_members cm '
                'JOIN mail_conversations cv ON cv.conversation_id=cm.conversation_id WHERE cv.package_id=? '
                'AND cm.message_key IN (' + marks + ')) SELECT DISTINCT o.message_key FROM mail_visible_occurrences o '
                'WHERE o.package_id=? AND (o.message_key IN (' + marks + ') OR o.message_key IN ('
                'SELECT cm.message_key FROM mail_conversation_members cm JOIN threads t ON t.conversation_id=cm.conversation_id))'), [package_id,*related_roots,package_id,*related_roots]
    if related_keys is not None:
        # Restrict review to the frozen, package-visible message set.
        if not related_keys:
            return 'SELECT message_key FROM mail_visible_occurrences WHERE 0', []
        return ('SELECT DISTINCT message_key FROM mail_visible_occurrences WHERE package_id=? AND message_key IN (' +
                ','.join('?' for _ in related_keys) + ')'), [package_id, *related_keys]
    direct = ('SELECT DISTINCT o.message_key FROM mail_visible_occurrences o '
              'JOIN mail_message_participants p ON p.message_key=o.message_key '
              'JOIN mail_addresses a ON a.address_id=p.address_id WHERE o.package_id=? '
              "AND p.role IN ('from','to','cc','bcc') AND a.normalized_address IN (" +
              ','.join('?' for _ in addresses) + ')')
    sql = ('WITH direct AS (' + direct + '), threads AS ('
           'SELECT DISTINCT cm.conversation_id FROM mail_conversation_members cm '
           'JOIN mail_conversations cv ON cv.conversation_id=cm.conversation_id '
           'WHERE cv.package_id=? AND cm.message_key IN (SELECT message_key FROM direct)) '
           'SELECT message_key FROM direct UNION SELECT cm.message_key FROM mail_conversation_members cm '
           'JOIN mail_visible_occurrences o ON o.message_key=cm.message_key AND o.package_id=? '
           'WHERE cm.conversation_id IN (SELECT conversation_id FROM threads)')
    return sql, [package_id, *addresses, package_id, package_id]


def related_messages(repository, package_id, owner_id, message_keys, *, count=False):
    authorize(repository, package_id, owner_id)
    if not message_keys:
        return 0 if count else []
    scope,args=_scope(package_id,[],related_roots=message_keys)
    with repository._connection() as c:
        if count:
            return c.execute('SELECT COUNT(*) FROM ('+scope+')',args).fetchone()[0]
        rows = c.execute('SELECT message_key FROM ('+scope+') ORDER BY message_key',args).fetchall()
    return [r[0] for r in rows]


def corpus_state(repository, package_id, owner_id, selection, *, connection=None, verify_content=True, full_package=False):
    if connection is None:
        authorize(repository, package_id, owner_id)
    if isinstance(selection, str):
        selection = resolve_selection(repository, package_id, owner_id, selection)
    scope, args = _scope(package_id, selection['addresses'], full_package=full_package)
    digest = hashlib.sha256();metadata_digest=hashlib.sha256()
    def add(value):
        if verify_content:
            digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode());digest.update(b'\x00')
        metadata={k:v for k,v in value.items() if k!='text'} if isinstance(value,dict) else value
        metadata_digest.update(json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode());metadata_digest.update(b'\x00')
    messages = segments = characters = attachments = missing_attachments = 0
    body_messages = set(); attribution_limited = set(); partial_attachments = set()
    with (nullcontext(connection) if connection is not None else repository._connection()) as c:
        package = c.execute('SELECT source_revision,index_epoch FROM mail_packages WHERE package_id=? AND owner_id=?',
                            (package_id, owner_id)).fetchone()
        if not package:
            raise LookupError('邮件资料包不存在或不属于当前用户')
        # The complete source-conversation scope is identical for every table
        # below. Materialize it once on this connection instead of repeating
        # the participants/threads CTE for each manifest component.
        c.execute('CREATE TEMP TABLE IF NOT EXISTS person_manifest_scope(message_key TEXT PRIMARY KEY)')
        c.execute('DELETE FROM temp.person_manifest_scope')
        c.execute('INSERT INTO temp.person_manifest_scope(message_key) '+scope,args)
        scope='SELECT message_key FROM temp.person_manifest_scope';args=[]
        from .research.context import CHANGE_MARKERS
        add([VERSION, ENGINE_VERSION, HARD_BOUNDARY.pattern, CHANGE_MARKERS.pattern,
             package['source_revision'], package['index_epoch'], selection])
        if full_package:
            add({'full_package':True})
        if selection.get('entity_id'):
            entity = c.execute('SELECT * FROM mail_entities WHERE package_id=? AND entity_id=?',
                               (package_id, selection['entity_id'])).fetchone()
            add(dict(entity) if entity else None)
            add([dict(r) for r in c.execute('SELECT * FROM mail_entity_addresses WHERE package_id=? AND entity_id=? ORDER BY address_id',
                                           (package_id, selection['entity_id']))])
        for raw in c.execute('SELECT m.* FROM mail_messages m WHERE m.message_key IN (' + scope + ') ORDER BY m.message_key', args):
            add(dict(raw)); messages += 1
        for raw in c.execute('SELECT p.*,a.normalized_address FROM mail_message_participants p '
                             'JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key IN (' + scope + ') '
                             'ORDER BY p.message_key,p.role,p.position,p.address_id', args):
            add(dict(raw))
        fragment_columns='f.*' if verify_content else ','.join('f."'+r[1].replace('"','""')+'"' for r in c.execute('PRAGMA table_info(mail_body_fragments)') if r[1]!='text')
        for raw in c.execute('SELECT '+fragment_columns+' FROM mail_body_fragments f WHERE f.message_key IN ('+scope+') '
                             'ORDER BY f.message_key,f.mime_path,f.ordinal',args):
            add(dict(raw))
        segment_columns='s.*' if verify_content else ','.join('s."'+r[1].replace('"','""')+'"' for r in c.execute('PRAGMA table_info(mail_text_segments)') if r[1]!='text')
        for raw in c.execute('SELECT s.rowid AS row_id,'+segment_columns+' FROM mail_text_segments s WHERE s.package_id=? '
                             'AND s.message_key IN (' + scope + ') ORDER BY s.rowid', [package_id, *args]):
            row = dict(raw); add(row); segments += 1; characters += len(row['text']) if verify_content else max(0,row['char_end']-row['char_start'])
            if row['source_kind'] == 'body' and (row['text'].strip() if verify_content else row['char_end']>row['char_start']):
                body_messages.add(row['message_key'])
        for raw in c.execute('SELECT p.*,EXISTS(SELECT 1 FROM mail_text_segments s WHERE s.package_id=? '
                             "AND s.message_key=p.message_key AND s.mime_path=p.mime_path AND s.source_kind='attachment' "
                             'AND LENGTH(s.text)>0) AS has_text,EXISTS(SELECT 1 FROM mail_text_segments s WHERE s.package_id=? '
                             "AND s.message_key=p.message_key AND s.mime_path=p.mime_path AND s.source_kind='body' AND LENGTH(s.text)>0) AS has_body_text "
                             'FROM mail_parts p WHERE p.message_key IN (' + scope + ') '
                             'ORDER BY p.message_key,p.mime_path', [package_id, package_id, *args]):
            row = dict(raw); add(row)
            if row['has_body_text'] and row['content_type']=='text/html':
                attribution_limited.add(row['message_key'])
            if row['is_explicit_attachment']:
                attachments += 1; missing_attachments += int(not row['has_text'])
        for raw in c.execute('SELECT p.* FROM mail_part_processing p WHERE p.message_key IN (' + scope + ') '
                             'ORDER BY p.message_key,p.mime_path,p.stage', args):
            row=dict(raw);add(row)
            if row['stage']=='basic_text' and (row['status']=='partial' or (row['coverage_ratio'] is not None and row['coverage_ratio']<1)):
                partial_attachments.add((row['message_key'],row['mime_path']))
        for raw in c.execute('SELECT cv.*,cm.message_key,cm.depth,cm.is_root FROM mail_conversations cv '
                             'JOIN mail_conversation_members cm ON cm.conversation_id=cv.conversation_id '
                             'WHERE cv.package_id=? AND cm.message_key IN (' + scope + ') '
                             'ORDER BY cv.conversation_id,cm.message_key', [package_id, *args]):
            add(dict(raw))
        # Header ancestry is used to call something a later *reply*. Ignore
        # rebuild timestamps, but freeze the resolved relation and its evidence.
        for raw in c.execute('SELECT edge_id,source_message_key,target_message_key,relation_type,source_header,'
                             'target_message_id_raw,status,evidence_json FROM mail_thread_edges WHERE package_id=? ORDER BY edge_id', (package_id,)):
            add(dict(raw))
    return {'fingerprint': digest.hexdigest() if verify_content else None,'metadata_fingerprint':metadata_digest.hexdigest(),
            'analysis_version': VERSION,'engine_version':ENGINE_VERSION, 'messages': messages,
            'messages_with_body': len(body_messages), 'missing_body': messages-len(body_messages),
            'segments': segments, 'characters': characters, 'attachments': attachments,
            'missing_attachment_text': missing_attachments, 'addresses': selection['addresses'],
            'partial_attachment_text':len(partial_attachments),'attribution_limited_body_messages':len(attribution_limited),
            'entity_id': selection.get('entity_id'), 'scope': 'full_package_body_attachments' if full_package else 'all_roles_body_attachments_conversation_context'}


def _attribution(c, package_id, row):
    # Scan every body segment, with only a small overlap retained in memory.
    # This avoids falsely attributing forwarded text beyond a 64k prefix.
    boundary = None; quotes = []; tail = ''; html_quote = False
    pieces = c.execute("SELECT text,char_start FROM mail_text_segments WHERE package_id=? AND message_key=? "
                       "AND mime_path=? AND source_kind='body' ORDER BY char_start,ordinal",
                       (package_id, row['message_key'], row['mime_path']))
    for piece in pieces:
        combined = tail + piece['text']; base = piece['char_start'] - len(tail)
        marker = HARD_BOUNDARY.search(combined)
        if marker:
            candidate = max(0, base+marker.start())
            boundary = candidate if boundary is None else min(boundary, candidate)
        for match in SOFT_QUOTE.finditer(combined):
            quotes.append([max(0, base+match.start()), base+match.end()])
        tail = combined[-512:]
    part = c.execute('SELECT part_role,content_type FROM mail_parts WHERE message_key=? AND mime_path=?',
                     (row['message_key'], row['mime_path'])).fetchone()
    # Parsed HTML removes blockquote tags. Unless an attribution-preserving
    # extractor is present, never promote its flattened quotes to authored intent.
    html_quote = bool(part and (part['part_role'] == 'body_html' or part['content_type'] == 'text/html'))
    fragments=[dict(f) for f in c.execute('SELECT fragment_kind,char_start,char_end,method,confidence FROM mail_body_fragments '
                                         'WHERE message_key=? AND mime_path=? ORDER BY ordinal',(row['message_key'],row['mime_path']))]
    return boundary, quotes, html_quote, fragments


def next_chunks(repository, package_id, owner_id, selection, cursor, char_budget=9000, *, related_keys=None, related_roots=None, include_attachments=True, full_package=False):
    """Read exact source spans, with at most 64 extra same-unit tail characters.

    Budgets below 512 stay hard limits. A complete independent short message or
    another attachment page/sheet/cell never borrows the preceding unit's tail
    allowance; model token reservations and capacity checks remain independent.
    """
    authorize(repository, package_id, owner_id)
    if not 1 <= int(char_budget) <= 50000:
        raise ValueError('证据批次预算必须为1至50000字符')
    scope, args = _scope(package_id, selection['addresses'], related_keys, related_roots, full_package=full_package)
    row_id, offset = int(cursor[0]), int(cursor[1])
    if row_id < 0 or offset < 0:
        raise ValueError('证据检查点无效')
    addresses = selection['addresses']; marks = ','.join('?' for _ in addresses)
    with repository._connection() as c:
        rows = c.execute('SELECT s.rowid AS row_id,s.*,m.subject_raw,m.sent_at_utc,m.date_raw, '
                         'EXISTS(SELECT 1 FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id '
                         "WHERE p.message_key=s.message_key AND p.role='from' AND a.normalized_address IN (" + marks + ')) AS authored '
                         'FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key WHERE s.package_id=? '
                         'AND LENGTH(s.text)>0 AND (s.rowid>? OR (s.rowid=? AND LENGTH(s.text)>?)) '
                         'AND s.message_key IN (' + scope + ')' + ('' if include_attachments else " AND s.source_kind!='attachment'") + ' ORDER BY s.rowid LIMIT 48',
                         [*addresses, package_id, row_id, row_id, offset, *args]).fetchall()
        chunks = []; remaining = int(char_budget); next_cursor = list(cursor); metadata = {}; attribution = {}
        tail_allowance = 64 if remaining >= 512 else 0
        unit_fields = ('message_key','mime_path','source_kind','page_number','sheet_name','cell_range')
        for row_index, raw in enumerate(rows):
            row = dict(raw); key = row['message_key']; partkey = (key, row['mime_path'])
            if hashlib.sha256(row['text'].encode()).hexdigest()!=row['text_sha256']:
                raise RuntimeError('邮件证据实际正文与冻结文本哈希不一致，请重新导入或分析')
            if key not in metadata:
                participants = [dict(p) for p in c.execute('SELECT p.role,a.normalized_address AS address FROM mail_message_participants p '
                                'JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key=? ORDER BY p.role,p.position', (key,))]
                conversations = [dict(p) for p in c.execute('SELECT cv.conversation_id,cv.merge_basis FROM mail_conversation_members cm '
                                 'JOIN mail_conversations cv ON cv.conversation_id=cm.conversation_id WHERE cm.message_key=? AND cv.package_id=?', (key, package_id))]
                metadata[key] = {'participants': participants, 'selected_roles': sorted({p['role'] for p in participants if p['address'] in addresses}),
                                 'conversations': conversations, 'context_only': not any(p['address'] in addresses for p in participants)}
                if related_roots:
                    ancestors=c.execute('WITH RECURSIVE ancestors(message_key) AS ('
                        "SELECT target_message_key FROM mail_thread_edges WHERE package_id=? AND source_message_key=? AND target_message_key IS NOT NULL "
                        "AND ((relation_type='reply_to' AND status='exact') OR (relation_type='reference_parent' AND status='inferred')) UNION "
                        'SELECT e.target_message_key FROM mail_thread_edges e JOIN ancestors a ON e.source_message_key=a.message_key '
                        "WHERE e.package_id=? AND e.target_message_key IS NOT NULL AND ((e.relation_type='reply_to' AND e.status='exact') "
                        "OR (e.relation_type='reference_parent' AND e.status='inferred'))) SELECT message_key FROM ancestors WHERE message_key IN ("+
                        ','.join('?' for _ in related_roots)+') ORDER BY message_key',[package_id,key,package_id,*related_roots]).fetchall()
                    metadata[key]['reply_source_keys']=[r[0] for r in ancestors]
            if partkey not in attribution:
                attribution[partkey] = _attribution(c, package_id, row) if row['source_kind'] == 'body' else (None, [], False, [])
            boundary, quote_ranges, html_uncertain,fragments = attribution[partkey]
            start = offset if row['row_id'] == row_id else 0
            while start < len(row['text']) and remaining:
                take = min(remaining,3000,len(row['text'])-start)
                tail = len(row['text'])-start-take
                if len(row['text'])>64 and 0<tail<=64:
                    borrowed = max(0,take+tail-remaining)
                    if borrowed<=tail_allowance:
                        take += tail; tail_allowance -= borrowed
                text = row['text'][start:start+take]; absolute = row['char_start'] + start; end = absolute+len(text)
                chunks.append({**row, **metadata[key], 'text': text, 'char_start': absolute, 'char_end': end,
                               'selected_addresses':list(addresses),
                               'chunk_id': '%s:%s' % (row['segment_id'], start), 'quotation_boundary': boundary,
                               'quoted_ranges': [q for q in quote_ranges if q[0] < end and q[1] > absolute],
                               'body_fragments':[f for f in fragments if f['char_start']<end and f['char_end']>absolute],
                               'attribution_uncertain': html_uncertain or row['source_kind'] != 'body' or
                                   any(p['role']=='from' and p['address'] not in addresses for p in metadata[key]['participants']) and bool(row['authored']),
                               'attribution_version': VERSION})
                remaining = max(0,remaining-len(text)); start += len(text)
            next_cursor = [row['row_id']+1,0] if start >= len(row['text']) else [row['row_id'],start]
            if not remaining:
                # A source unit can span several storage segments. Keep a tiny
                # contiguous last segment in this call without combining its
                # provenance with the preceding chunk or another message.
                following = dict(rows[row_index+1]) if row_index+1<len(rows) else None
                if (start>=len(row['text']) and following and
                    all(row.get(field)==following.get(field) for field in unit_fields) and
                    following['char_start']==row['char_end'] and
                    0<len(following['text'])<=tail_allowance):
                    remaining = len(following['text']); tail_allowance -= remaining
                else:
                    break
    return chunks, next_cursor
