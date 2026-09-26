"""Package-scoped mailbox correspondence; never infer identity from a name."""
from __future__ import annotations
import hashlib
import re
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
            "SELECT 1 FROM mail_occurrences o JOIN mail_message_participants p ON p.message_key=o.message_key "
            "JOIN mail_addresses a ON a.address_id=p.address_id WHERE o.package_id=? AND a.normalized_address=? "
            "AND p.role IN ('from','to','cc','bcc') LIMIT 1", (package_id, value)).fetchone()
    if not found:
        raise LookupError('这个邮箱不在当前资料包中')
    return value


def _scope(package_id, address, direction='all'):
    if direction not in {'all', 'sent', 'received', 'cc', 'bcc'}:
        raise ValueError('邮件范围应为全部、发出、收到、抄送或密送')
    roles = {'all': ROLES, 'sent': ('from',), 'received': ('to','cc','bcc'), 'cc': ('cc',), 'bcc': ('bcc',)}[direction]
    sql = ("SELECT DISTINCT o.message_key FROM mail_occurrences o JOIN mail_message_participants p ON p.message_key=o.message_key "
           "JOIN mail_addresses a ON a.address_id=p.address_id WHERE o.package_id=? AND a.normalized_address=? AND p.role IN ("
           + ','.join('?' for _ in roles) + ')')
    return sql, [package_id, address, *roles]


def contacts(repository, package_id, owner_id, query='', offset=0, limit=50):
    authorize(repository, package_id, owner_id)
    offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
    query = str(query or '').strip()[:320]
    like = '%' + query.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
    # Display names come only from explicitly confirmed, package-local entities.
    base = (" FROM mail_occurrences o JOIN mail_message_participants p ON p.message_key=o.message_key "
            "JOIN mail_addresses a ON a.address_id=p.address_id "
            "LEFT JOIN mail_entity_addresses ea ON ea.address_id=a.address_id "
            "LEFT JOIN mail_entities en ON en.entity_id=ea.entity_id AND en.package_id=o.package_id "
            "WHERE o.package_id=? AND p.role IN ('from','to','cc','bcc') "
            "AND (a.normalized_address LIKE ? ESCAPE '!' OR en.display_name LIKE ? ESCAPE '!')")
    args = [package_id, like, like]
    with repository._connection() as c:
        total = c.execute('SELECT COUNT(DISTINCT a.address_id)' + base, args).fetchone()[0]
        rows = c.execute(
            "SELECT a.normalized_address AS address,MAX(en.display_name) AS display_name,COUNT(DISTINCT o.message_key) AS total,"
            "COUNT(DISTINCT CASE WHEN p.role='from' THEN o.message_key END) AS sent,"
            "COUNT(DISTINCT CASE WHEN p.role IN ('to','cc','bcc') THEN o.message_key END) AS received"
            + base + ' GROUP BY a.address_id ORDER BY total DESC,address LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
    return {'items': [dict(r) for r in rows], 'total': total, 'offset': offset, 'limit': limit,
            'identity_note': '按邮箱统计；同名不自动合并。收到邮件不代表认同内容。'}


def correspondence(repository, package_id, owner_id, address, *, direction='all', offset=0, limit=25):
    address = authorize(repository, package_id, owner_id, address)
    scope, args = _scope(package_id, address, direction)
    offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
    with repository._connection() as c:
        total = c.execute('SELECT COUNT(*) FROM (' + scope + ')', args).fetchone()[0]
        rows = c.execute(
            "SELECT m.message_key,m.subject_raw,m.sent_at_utc,m.body_language,"
            "(SELECT GROUP_CONCAT(DISTINCT a.normalized_address) FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key=m.message_key AND p.role='from') AS sender,"
            "(SELECT GROUP_CONCAT(DISTINCT p.role) FROM mail_message_participants p JOIN mail_addresses a ON a.address_id=p.address_id WHERE p.message_key=m.message_key AND a.normalized_address=?) AS roles "
            'FROM mail_messages m WHERE m.message_key IN (' + scope + ') ORDER BY m.sent_at_utc IS NULL,m.sent_at_utc DESC,m.message_key LIMIT ? OFFSET ?',
            [address, *args, limit, offset]).fetchall()
        stats = c.execute(
            "SELECT COUNT(DISTINCT CASE WHEN p.role='from' THEN o.message_key END) AS sent,"
            "COUNT(DISTINCT CASE WHEN p.role IN ('to','cc','bcc') THEN o.message_key END) AS received,"
            "COUNT(DISTINCT o.message_key) AS total FROM mail_occurrences o JOIN mail_message_participants p ON p.message_key=o.message_key "
            "JOIN mail_addresses a ON a.address_id=p.address_id WHERE o.package_id=? AND a.normalized_address=? AND p.role IN ('from','to','cc','bcc')",
            (package_id, address)).fetchone()
    return {'address': address, 'items': [dict(r) for r in rows], 'total': total, 'offset': offset, 'limit': limit,
            'direction': direction, 'counts': dict(stats), 'count_basis': 'distinct_message_key',
            'note': '清单包含该邮箱全部收发、抄送及包内可见密送；同一封只计一次。自发自收同时计入收发。'}


def relationships(repository, package_id, owner_id, address, *, offset=0, limit=20):
    address = authorize(repository, package_id, owner_id, address)
    limit, offset = max(1, min(50, int(limit))), max(0, int(offset))
    base = ("WITH pairs AS (SELECT CASE WHEN a.normalized_address=? THEN b.normalized_address ELSE a.normalized_address END AS counterpart,"
            "e.message_key,CASE WHEN a.normalized_address=? THEN 1 ELSE 0 END AS outgoing,e.recipient_role,e.sent_at_utc "
            "FROM mail_communication_edges e JOIN mail_addresses a ON a.address_id=e.from_address_id JOIN mail_addresses b ON b.address_id=e.to_address_id "
            "WHERE e.package_id=? AND (a.normalized_address=? OR b.normalized_address=?)) ")
    args = [address,address,package_id,address,address]
    with repository._connection() as c:
        total = c.execute(base+'SELECT COUNT(DISTINCT counterpart) FROM pairs',args).fetchone()[0]
        rows = c.execute(base+"SELECT counterpart,COUNT(DISTINCT message_key) AS total,"
                         "COUNT(DISTINCT CASE WHEN outgoing=1 THEN message_key END) AS sent,"
                         "COUNT(DISTINCT CASE WHEN outgoing=0 THEN message_key END) AS received,"
                         "GROUP_CONCAT(DISTINCT recipient_role) AS roles,MIN(sent_at_utc) AS first_sent,MAX(sent_at_utc) AS last_sent "
                         "FROM pairs GROUP BY counterpart ORDER BY total DESC,counterpart LIMIT ? OFFSET ?",args+[limit,offset]).fetchall()
    return {'address':address,'items':[dict(r) for r in rows],'total':total,'offset':offset,'limit':limit,
            'note':'箭头表示邮件头中的发送方向，不代表现实身份、权力或认同关系。未出现在邮件头中的密送无法推断。'}


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
