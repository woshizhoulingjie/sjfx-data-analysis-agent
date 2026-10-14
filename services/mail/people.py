"""Package-scoped mailbox correspondence; never infer identity from a name."""
from __future__ import annotations
import hashlib
import re
from datetime import datetime, timezone
from .normalization import normalize_address

ROLES = ('from', 'to', 'cc', 'bcc')
_PACKAGE_ONLY = object()
QUOTE_MARKERS = re.compile(r'(?im)^ *> |^ *>$|original message|forwarded message|begin forwarded|原始邮件|转发邮件|转寄邮件|เขียนว่า|ข้อความที่ส่งต่อ|wrote *:|<blockquote|^-- $|^from:|^发件人:')


def authorize(repository, package_id, owner_id, address=_PACKAGE_ONLY):
    package = repository.get_package(package_id, owner_id)
    if not package:
        raise LookupError('邮件资料包不存在或不属于当前用户')
    if address is _PACKAGE_ONLY:
        return package
    if not isinstance(address, str) or not address.strip():
        raise ValueError('请选择有效的邮箱地址')
    value = normalize_address(address)
    if not value or len(value) > 320:
        raise ValueError('请选择有效的邮箱地址')
    with repository._connection() as c:
        found = c.execute(
            "SELECT 1 FROM mail_visible_occurrences o JOIN mail_message_participants p ON p.message_key=o.message_key "
            "JOIN mail_addresses a ON a.address_id=p.address_id WHERE o.package_id=? AND a.normalized_address=? "
            "AND p.role IN ('from','to','cc','bcc') LIMIT 1", (package_id, value)).fetchone()
    if not found:
        raise LookupError('这个邮箱不在当前资料包中')
    return value


def _scope(package_id, address, direction='all'):
    if direction not in {'all', 'sent', 'direct', 'received', 'cc', 'bcc'}:
        raise ValueError('邮件范围应为全部、发出、收到、抄送或密送')
    roles = {'all': ROLES, 'sent': ('from',), 'direct': ('to',), 'received': ('to','cc','bcc'), 'cc': ('cc',), 'bcc': ('bcc',)}[direction]
    sql = ("SELECT DISTINCT o.message_key FROM mail_visible_occurrences o JOIN mail_message_participants p ON p.message_key=o.message_key "
           "JOIN mail_addresses a ON a.address_id=p.address_id WHERE o.package_id=? AND a.normalized_address=? AND p.role IN ("
           + ','.join('?' for _ in roles) + ')')
    return sql, [package_id, address, *roles]


def _like(value):
    return '%' + str(value or '').replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'


def _date_bound(value, upper=False):
    if value in (None, ''):
        return None
    value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if len(value) == 10 and upper:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError as exc:
        raise ValueError('时间范围需要有效的 ISO 日期或时间') from exc


def filtered_messages(package_id, *, date_from=None, date_to=None, q='', has_attachment=None, participant=None, conversation_id=None):
    """One package-local logical-message set shared by cards, lists and graphs."""
    start, end = _date_bound(date_from), _date_bound(date_to, upper=True)
    if start and end and start > end:
        raise ValueError('开始时间不能晚于结束时间')
    if has_attachment not in (None, ''):
        if isinstance(has_attachment, str):
            value = has_attachment.casefold()
            if value not in {'true', 'false', '1', '0'}:
                raise ValueError('附件筛选仅支持 true 或 false')
            has_attachment = value in {'true', '1'}
        elif not isinstance(has_attachment, bool):
            raise ValueError('附件筛选仅支持 true 或 false')
    else:
        has_attachment = None
    q = str(q or '').strip()[:1000]
    clauses = ['EXISTS (SELECT 1 FROM mail_visible_occurrences o WHERE o.package_id=? AND o.message_key=m.message_key)']
    args = [package_id]
    raw_participant = str(participant or '').strip()
    participant = normalize_address(raw_participant) or None
    if raw_participant and (not participant or len(participant)>320 or not all(participant.rsplit('@',1))):
        raise ValueError('参与邮箱筛选需要有效的邮箱地址')
    if participant:
        clauses.append("EXISTS (SELECT 1 FROM mail_message_participants px JOIN mail_addresses ax ON ax.address_id=px.address_id WHERE px.message_key=m.message_key AND ax.normalized_address=? AND px.role IN ('from','to','cc','bcc'))")
        args.append(participant)
    conversation_id = str(conversation_id or '').strip()
    if conversation_id:
        clauses.append('EXISTS (SELECT 1 FROM mail_conversation_members cm JOIN mail_conversations cv ON cv.conversation_id=cm.conversation_id WHERE cm.message_key=m.message_key AND cv.package_id=? AND cv.conversation_id=?)')
        args.extend([package_id, conversation_id])
    if start:
        clauses.append('julianday(m.sent_at_utc)>=julianday(?)'); args.append(start)
    if end:
        clauses.append('julianday(m.sent_at_utc)<=julianday(?)'); args.append(end)
    if has_attachment is not None:
        clauses.append(('' if has_attachment else 'NOT ') + 'EXISTS (SELECT 1 FROM mail_parts fp WHERE fp.message_key=m.message_key AND fp.is_explicit_attachment=1)')
    if q:
        clauses.append("(m.subject_raw LIKE ? ESCAPE '!' OR EXISTS (SELECT 1 FROM mail_text_segments sx WHERE sx.package_id=? AND sx.message_key=m.message_key AND sx.text LIKE ? ESCAPE '!') OR EXISTS (SELECT 1 FROM mail_parts fp WHERE fp.message_key=m.message_key AND fp.filename_raw LIKE ? ESCAPE '!'))")
        args.extend([_like(q), package_id, _like(q), _like(q)])
    return 'SELECT m.message_key FROM mail_messages m WHERE ' + ' AND '.join(clauses), args, {'date_from': start, 'date_to': end, 'q': q, 'has_attachment': has_attachment, 'participant': participant, 'conversation_id': conversation_id or None}


def resolve_person(repository, package_id, owner_id, address=None, entity_id=None):
    authorize(repository, package_id, owner_id)
    if entity_id:
        with repository._connection() as c:
            entity = c.execute("SELECT * FROM mail_entities WHERE package_id=? AND entity_id=? AND basis='manual'", (package_id, str(entity_id))).fetchone()
            if not entity:
                raise LookupError('这个人物不在当前资料包中或已撤销归并')
            addresses = [r[0] for r in c.execute('SELECT a.normalized_address FROM mail_entity_addresses ea JOIN mail_addresses a ON a.address_id=ea.address_id WHERE ea.package_id=? AND ea.entity_id=? ORDER BY a.normalized_address', (package_id, str(entity_id)))]
        if not addresses:
            raise LookupError('这个人物尚无已确认邮箱')
        if address and normalize_address(address) not in addresses:
            raise ValueError('邮箱与选定人物不一致')
        return {'address': addresses[0], 'addresses': addresses, 'entity_id': str(entity_id), 'display_name': entity['display_name']}
    address = authorize(repository, package_id, owner_id, address)
    return {'address': address, 'addresses': [address], 'entity_id': None, 'display_name': address}


def _role_cte(scope_sql):
    return ("WITH scoped AS (" + scope_sql + "), role_rows AS ("
            "SELECT DISTINCT s.message_key,p.role,a.normalized_address AS address,a.address_id "
            "FROM scoped s JOIN mail_message_participants p ON p.message_key=s.message_key "
            "JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.role IN ('from','to','cc','bcc'))")


def _flags_sql(predicate):
    return ("SELECT message_key,MAX(role='from') AS sent,MAX(role='to') AS direct,"
            "MAX(role='cc') AS cc,MAX(role='bcc') AS bcc,MAX(role IN ('to','cc','bcc')) AS received,"
            "GROUP_CONCAT(DISTINCT role) AS roles FROM role_rows WHERE " + predicate + ' GROUP BY message_key')


def _in_addresses(addresses):
    return 'address IN (' + ','.join('?' for _ in addresses) + ')'


def _role_counts(c, cte, args):
    row = c.execute(cte + ' SELECT COUNT(*) AS total,COALESCE(SUM(sent),0) AS sent,COALESCE(SUM(direct),0) AS direct,COALESCE(SUM(cc),0) AS cc,COALESCE(SUM(bcc),0) AS bcc,COALESCE(SUM(received),0) AS received FROM flags', args).fetchone()
    return dict(row)


def _message_page(c, cte, args, predicate, limit, offset, extra_columns=''):
    from .status import message_status_columns
    items = [dict(r) for r in c.execute(cte + " SELECT m.message_key,m.subject_raw,m.date_raw,m.sent_at_utc,m.body_language,m.parse_status,f.roles,"
        + message_status_columns() + ',' +
        "(SELECT GROUP_CONCAT(DISTINCT address) FROM role_rows p WHERE p.message_key=m.message_key AND p.role='from') AS sender,"
        "(SELECT GROUP_CONCAT(address,char(31)) FROM role_rows p WHERE p.message_key=m.message_key AND p.role='from') AS sender_addresses,"
        "(SELECT COUNT(*) FROM mail_parts ap WHERE ap.message_key=m.message_key AND ap.is_explicit_attachment=1) AS attachment_count " + extra_columns +
        " FROM flags f JOIN mail_messages m ON m.message_key=f.message_key WHERE " + predicate +
        ' ORDER BY m.sent_at_utc IS NULL,julianday(m.sent_at_utc) DESC,m.message_key LIMIT ? OFFSET ?', [*args, limit, offset])]
    for item in items:
        item['senders'] = (item.pop('sender_addresses') or '').split(chr(31)) if item['sender'] else []
        item['participants'] = [dict(r) for r in c.execute('SELECT DISTINCT p.role,a.normalized_address AS address FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key=? AND p.role IN (\'from\',\'to\',\'cc\',\'bcc\') ORDER BY CASE p.role WHEN \'from\' THEN 0 WHEN \'to\' THEN 1 WHEN \'cc\' THEN 2 ELSE 3 END,p.position,a.normalized_address', (item['message_key'],))]
    return items


def _page_context(c, items, package_id):
    """Only expose conversation ids from the selected package, for each page."""
    if not items:
        return
    by_key = {item['message_key']: item for item in items}
    for item in items:
        item['conversations'] = []
    for row in c.execute('SELECT cm.message_key,cm.conversation_id FROM mail_conversation_members cm JOIN mail_conversations cv ON cv.conversation_id=cm.conversation_id WHERE cv.package_id=? AND cm.message_key IN (' + ','.join('?' for _ in by_key) + ') ORDER BY cm.conversation_id', [package_id, *by_key]):
        by_key[row[0]]['conversations'].append(row[1])


def _page_range(c, cte, args, predicate='1=1'):
    return dict(c.execute(cte + ' SELECT MIN(m.sent_at_utc) AS first_sent,MAX(m.sent_at_utc) AS last_sent,COUNT(*) AS message_count FROM flags f JOIN mail_messages m ON m.message_key=f.message_key WHERE ' + predicate, args).fetchone())


def contacts(repository, package_id, owner_id, query='', offset=0, limit=50, *, view='mailbox', **filters):
    authorize(repository, package_id, owner_id)
    if view not in {'mailbox', 'person'}:
        raise ValueError('人物视角仅支持 mailbox 或 person')
    offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
    scope, args, applied = filtered_messages(package_id, **filters)
    identity = 'r.address' if view == 'mailbox' else 'COALESCE(en.entity_id,r.address)'
    # Confirmed identity mappings are always joined within this package.
    cte = (_role_cte(scope) + ", named AS (SELECT r.*,en.display_name,en.entity_id," + identity + " AS identity_key "
        "FROM role_rows r LEFT JOIN mail_entity_addresses ea ON ea.address_id=r.address_id AND ea.package_id=? "
        "LEFT JOIN mail_entities en ON en.entity_id=ea.entity_id AND en.package_id=ea.package_id AND en.basis='manual'),"
        "per_message AS (SELECT identity_key,message_key,MIN(address) AS address,MAX(display_name) AS display_name,MAX(entity_id) AS entity_id,"
        "MAX(role='from') AS sent,MAX(role='to') AS direct,MAX(role='cc') AS cc,MAX(role='bcc') AS bcc,MAX(role IN ('to','cc','bcc')) AS received FROM named GROUP BY identity_key,message_key),"
        "totals AS (SELECT identity_key,MIN(address) AS address,MAX(display_name) AS display_name,MAX(entity_id) AS entity_id,COUNT(*) AS total,SUM(sent) AS sent,SUM(direct) AS direct,SUM(cc) AS cc,SUM(bcc) AS bcc,SUM(received) AS received FROM per_message GROUP BY identity_key) ")
    args = [*args, package_id]
    predicate = "(address LIKE ? ESCAPE '!' OR display_name LIKE ? ESCAPE '!' OR EXISTS (SELECT 1 FROM named n WHERE n.identity_key=totals.identity_key AND n.address LIKE ? ESCAPE '!'))"
    args.extend([_like(str(query or '').strip()[:320])] * 3)
    with repository._connection() as c:
        total = c.execute(cte + 'SELECT COUNT(*) FROM totals WHERE ' + predicate, args).fetchone()[0]
        items = [dict(r) for r in c.execute(cte + 'SELECT * FROM totals WHERE ' + predicate + ' ORDER BY total DESC,identity_key LIMIT ? OFFSET ?', [*args, limit, offset])]
        for item in items:
            if view == 'person' and item['entity_id']:
                item['addresses'] = [r[0] for r in c.execute('SELECT a.normalized_address FROM mail_entity_addresses ea JOIN mail_addresses a ON a.address_id=ea.address_id WHERE ea.package_id=? AND ea.entity_id=? ORDER BY a.normalized_address', (package_id, item['entity_id']))]
            else:
                item['addresses'] = [item['address']]
    return {'items': items, 'total': total, 'offset': offset, 'limit': limit, 'view': view, 'filters': applied,
            'count_basis': 'distinct_message_key', 'identity_note': '邮箱视角逐地址统计；人物视角仅使用人工确认的邮箱集合。收到仅表示邮件头包含该地址，不证明送达、阅读或认同。'}


def correspondence(repository, package_id, owner_id, address=None, *, entity_id=None, direction='all', offset=0, limit=25, **filters):
    person = resolve_person(repository, package_id, owner_id, address, entity_id)
    if direction not in {'all', 'sent', 'direct', 'received', 'cc', 'bcc'}:
        raise ValueError('邮件范围应为全部、发出、直接收到、收到合计、抄送或可见密送')
    scope, args, applied = filtered_messages(package_id, **filters)
    cte = _role_cte(scope) + ', flags AS (' + _flags_sql(_in_addresses(person['addresses'])) + ')'
    args.extend(person['addresses'])
    offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
    predicate = '1=1' if direction == 'all' else direction + '=1'
    with repository._connection() as c:
        counts = _role_counts(c, cte, args)
        rows = _message_page(c, cte, args, predicate, limit, offset)
        _page_context(c, rows, package_id)
        time_range = _page_range(c, cte, args, predicate)
    # The legacy three-key counts shape remains available to existing consumers.
    return {**person, 'items': rows, 'total': counts['total' if direction == 'all' else direction], 'offset': offset, 'limit': limit,
            'direction': direction, 'counts': {k: counts[k] for k in ('sent', 'received', 'total')}, 'role_counts': counts,
            'filters': applied, 'time_range': time_range, 'package_id': package_id, 'count_basis': 'distinct_message_key',
            'note': '分类可重叠，收到合计和全部相关使用唯一邮件并集；可见密送仅限当前包来源。'}


def _pair_flags():
    return ("a.sent*b.direct AS sent,b.sent*a.direct AS received,a.sent*b.cc AS sent_cc,b.sent*a.cc AS received_cc,"
        "a.sent*b.bcc AS sent_bcc,b.sent*a.bcc AS received_bcc,"
        "((a.sent*b.cc) OR (b.sent*a.cc)) AS cc,((a.sent*b.bcc) OR (b.sent*a.bcc)) AS bcc,"
        "(a.received AND b.received AND NOT a.sent AND NOT b.sent) AS shared")


_PAIR_PRESENT = '(sent OR received OR cc OR bcc OR shared)'


def _pair_counts(c, cte, args):
    names = ('sent', 'received', 'sent_cc', 'received_cc', 'sent_bcc', 'received_bcc', 'cc', 'bcc', 'shared')
    row = c.execute(cte + ' SELECT COUNT(*) AS total,' + ','.join('COALESCE(SUM(' + name + '),0) AS ' + name for name in names) + ' FROM flags WHERE ' + _PAIR_PRESENT, args).fetchone()
    return dict(row)


def bilateral(repository, package_id, owner_id, address=None, counterpart=None, *, entity_id=None, counterpart_entity_id=None, direction='all', offset=0, limit=25, **filters):
    person = resolve_person(repository, package_id, owner_id, address, entity_id)
    other = resolve_person(repository, package_id, owner_id, counterpart, counterpart_entity_id)
    if set(person['addresses']) & set(other['addresses']):
        raise ValueError('双方邮箱集合不能重叠；自发自收请使用人物邮件列表')
    if direction not in {'all', 'sent', 'received', 'cc', 'bcc', 'shared', 'sent_cc', 'received_cc', 'sent_bcc', 'received_bcc'}:
        raise ValueError('请选择有效的双方通信分类')
    scope, args, applied = filtered_messages(package_id, **filters)
    cte = (_role_cte(scope) + ', a AS (' + _flags_sql(_in_addresses(person['addresses'])) + '), b AS (' +
        _flags_sql(_in_addresses(other['addresses'])) + '), flags AS (SELECT a.message_key,a.roles,' + _pair_flags() +
        ' FROM a JOIN b ON b.message_key=a.message_key)')
    args.extend([*person['addresses'], *other['addresses']])
    offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
    predicate = _PAIR_PRESENT if direction == 'all' else direction + '=1'
    with repository._connection() as c:
        counts = _pair_counts(c, cte, args)
        pair_names = ('sent','received','sent_cc','received_cc','sent_bcc','received_bcc','shared')
        items = _message_page(c, cte, args, predicate, limit, offset, ',' + ','.join('f.' + name + ' AS pair_' + name for name in pair_names))
        _page_context(c, items, package_id)
        time_range = _page_range(c, cte, args, predicate)
        for item in items:
            item['pair_roles'] = [name for name in pair_names if item.pop('pair_' + name)]
    return {'person': person, 'counterparty': other, 'address': person['address'], 'counterpart': other['address'],
            'items': items, 'counts': counts, 'total': counts['total' if direction == 'all' else direction], 'direction': direction,
            'offset': offset, 'limit': limit, 'filters': applied, 'time_range': time_range, 'package_id': package_id, 'count_basis': 'distinct_message_key',
            'note': '直接通信仅使用 From→To；抄送、可见密送和双方均为收件人的共同参与分别列示，分类可重叠。'}


def relationships(repository, package_id, owner_id, address=None, *, entity_id=None, view='mailbox', offset=0, limit=20, **filters):
    person = resolve_person(repository, package_id, owner_id, address, entity_id)
    if view not in {'mailbox', 'person'}:
        raise ValueError('人物视角仅支持 mailbox 或 person')
    scope, args, applied = filtered_messages(package_id, **filters)
    identity = 'r.address' if view == 'mailbox' else 'COALESCE(en.entity_id,r.address)'
    cte = (_role_cte(scope) + ', a AS (' + _flags_sql(_in_addresses(person['addresses'])) + '), named AS ('
        'SELECT r.*,' + identity + ' AS identity_key,en.entity_id,en.display_name FROM role_rows r '
        'LEFT JOIN mail_entity_addresses ea ON ea.address_id=r.address_id AND ea.package_id=? '
        "LEFT JOIN mail_entities en ON en.entity_id=ea.entity_id AND en.package_id=ea.package_id AND en.basis='manual' WHERE NOT (" + _in_addresses(person['addresses']) + ')), b AS ('
        "SELECT identity_key,message_key,MIN(address) AS counterpart,MAX(entity_id) AS entity_id,MAX(display_name) AS display_name,MAX(role='from') AS sent,MAX(role='to') AS direct,MAX(role='cc') AS cc,MAX(role='bcc') AS bcc,MAX(role IN ('to','cc','bcc')) AS received FROM named GROUP BY identity_key,message_key),"
        'pairs AS (SELECT a.message_key,b.identity_key,b.counterpart,b.entity_id,b.display_name,' + _pair_flags() + ' FROM a JOIN b ON b.message_key=a.message_key),'
        'summary AS (SELECT identity_key,MIN(counterpart) AS counterpart,MAX(entity_id) AS entity_id,MAX(display_name) AS display_name,COUNT(*) AS total,SUM(sent) AS sent,SUM(received) AS received,SUM(cc) AS cc,SUM(bcc) AS bcc,SUM(shared) AS shared,SUM(sent_cc) AS sent_cc,SUM(received_cc) AS received_cc,SUM(sent_bcc) AS sent_bcc,SUM(received_bcc) AS received_bcc,MIN(m.sent_at_utc) AS first_sent,MAX(m.sent_at_utc) AS last_sent FROM pairs JOIN mail_messages m ON m.message_key=pairs.message_key WHERE ' + _PAIR_PRESENT + ' GROUP BY identity_key) ')
    args.extend([*person['addresses'], package_id, *person['addresses']])
    limit, offset = max(1, min(50, int(limit))), max(0, int(offset))
    with repository._connection() as c:
        total = c.execute(cte + 'SELECT COUNT(*) FROM summary', args).fetchone()[0]
        items = [dict(r) for r in c.execute(cte + 'SELECT * FROM summary ORDER BY total DESC,identity_key LIMIT ? OFFSET ?', [*args, limit, offset])]
    return {**person, 'items': items, 'total': total, 'offset': offset, 'limit': limit, 'filters': applied,
            'count_basis': 'distinct_message_key', 'view': view,
            'note': '直接收发、抄送、可见密送和共同参与分开统计；点击数量查看相同范围的双方邮件。'}


def corpus_state(repository, package_id, owner_id, address):
    package = authorize(repository, package_id, owner_id)
    address = authorize(repository, package_id, owner_id, address)
    scope,args = _scope(package_id,address)
    digest=hashlib.sha256();messages=segments=chars=with_body=0
    with repository._connection() as c:
        for row in c.execute('SELECT m.message_key,m.raw_sha256 FROM mail_messages m WHERE m.message_key IN ('+scope+') ORDER BY m.message_key',args):
            digest.update((row[0]+':'+row[1]).encode());messages+=1
        seen=set()
        for row in c.execute("SELECT s.rowid,s.segment_id,s.text_sha256,s.message_key,LENGTH(s.text) FROM mail_text_segments s WHERE s.package_id=? AND s.source_kind='body' AND s.message_key IN ("+scope+') ORDER BY s.rowid',[package_id,*args]):
            digest.update((':'.join(str(v) for v in row)).encode());segments+=1;chars+=row[4]
            if row[4]>0:seen.add(row[3])
        with_body=len(seen)
    digest.update(str((package['source_revision'],package['index_epoch'])).encode())
    return {'fingerprint':digest.hexdigest(),'messages':messages,'messages_with_body':with_body,'missing_body':messages-with_body,'segments':segments,'characters':chars}


def next_chunks(repository, package_id, owner_id, address, cursor, char_budget=9000):
    address=authorize(repository,package_id,owner_id,address)
    if char_budget < 1:raise ValueError('正文批次大小必须为正数')
    scope,args=_scope(package_id,address)
    row_id,offset=int(cursor[0]),int(cursor[1])
    with repository._connection() as c:
        rows=c.execute("SELECT s.rowid AS row_id,s.*,m.subject_raw,m.sent_at_utc,EXISTS (SELECT 1 FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key=s.message_key AND p.role='from' AND a.normalized_address=?) AS authored "
                       "FROM mail_text_segments s JOIN mail_messages m ON m.message_key=s.message_key WHERE s.package_id=? AND s.source_kind='body' AND LENGTH(s.text)>0 AND (s.rowid>? OR (s.rowid=? AND LENGTH(s.text)>?)) AND s.message_key IN ("+scope+') ORDER BY s.rowid LIMIT 24',
                       [address,package_id,row_id,row_id,offset,*args]).fetchall()
        prefixes={}
        for raw in rows:
            key=(raw['message_key'],raw['mime_path'])
            if key not in prefixes:
                pieces=c.execute("SELECT SUBSTR(text,1,64000-char_start) FROM mail_text_segments WHERE package_id=? AND message_key=? AND mime_path=? AND source_kind='body' AND char_start<64000 ORDER BY char_start",(package_id,*key))
                prefix=''.join(r[0] for r in pieces)[:64000]
                marker=QUOTE_MARKERS.search(prefix)
                prefixes[key]=marker.start() if marker else None
    chunks=[];remaining=char_budget;next_cursor=list(cursor)
    for raw in rows:
        row=dict(raw);start=offset if row['row_id']==row_id else 0
        text=row['text']
        while start<len(text) and remaining>0:
            piece=text[start:start+min(remaining,3000)]
            end=row['char_start']+start+len(piece)
            boundary=prefixes[(row['message_key'],row['mime_path'])]
            if boundary is None and end>64000:boundary=64000
            chunks.append({**row,'text':piece,'char_start':row['char_start']+start,'char_end':end,
                           'chunk_id':f"{row['segment_id']}:{start}",'quotation_boundary':boundary})
            remaining-=len(piece);start+=len(piece)
        next_cursor=[row['row_id']+1,0] if start>=len(text) else [row['row_id'],start]
        if remaining<=0:break
    return chunks,next_cursor
