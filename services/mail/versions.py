"""Filename families are candidates; identical payload hashes are exact evidence."""
from __future__ import annotations
import re
import collections
import unicodedata
from pathlib import PurePath
from config import Config
from .conversations import stable_id


def filename_family(name):
    name=unicodedata.normalize('NFKC',str(name or '')).casefold().strip()
    stem=PurePath(name).stem
    stem=re.sub(r'(?:[ _.-](?:v|ver|version|rev|revision)[ _.-]?\d+(?:\.\d+)*|[ _.-](?:final|revised|draft)|[ _.-]\d{8})$','',stem)
    return stem+PurePath(name).suffix


def rebuild(repository,package_id,owner_id):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    if not Config.MAIL_ENABLE_ATTACHMENT_VERSIONS:return
    with repository._connection() as c:
        rows=[dict(r) for r in c.execute('SELECT DISTINCT p.message_key,p.mime_path,p.filename_raw,p.payload_sha256,m.sent_at_utc FROM mail_parts p JOIN mail_visible_occurrences o ON o.message_key=p.message_key JOIN mail_messages m ON m.message_key=p.message_key WHERE o.package_id=? AND p.is_explicit_attachment=1 ORDER BY m.sent_at_utc,p.message_key,p.mime_path',(package_id,))]
        c.execute('DELETE FROM mail_attachment_version_links WHERE package_id=?',(package_id,));c.execute('DELETE FROM mail_attachment_versions WHERE package_id=?',(package_id,))
        chains=collections.defaultdict(lambda:collections.defaultdict(list))
        for r in rows:chains[filename_family(r['filename_raw']) or r['payload_sha256']][r['payload_sha256']].append(r)
        for chain,versions in chains.items():
            for ordinal,(digest,items) in enumerate(versions.items(),1):
                first,last=items[0],items[-1]
                c.execute('INSERT INTO mail_attachment_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(stable_id(package_id,chain,digest),package_id,chain,digest,chain,ordinal,first['message_key'],first['sent_at_utc'],last['message_key'],last['sent_at_utc'],len(items)))
                c.executemany('INSERT INTO mail_attachment_version_links VALUES(?,?,?,?,?,?)',[(package_id,chain,ordinal,r['message_key'],r['mime_path'],digest) for r in items])


def trace(repository,package_id,owner_id,chain_key=None,message_key=None):
    if not repository.get_package(package_id,owner_id):raise ValueError('邮件资料包不存在')
    if not Config.MAIL_ENABLE_ATTACHMENT_VERSIONS:return {'items':[],'enabled':False}
    rebuild(repository,package_id,owner_id)
    with repository._connection() as c:
        sql='SELECT v.*,l.message_key,l.mime_path,m.sent_at_utc,m.subject_raw FROM mail_attachment_version_links l JOIN mail_attachment_versions v ON v.package_id=l.package_id AND v.chain_key=l.chain_key AND v.payload_sha256=l.payload_sha256 JOIN mail_messages m ON m.message_key=l.message_key WHERE l.package_id=?';args=[package_id]
        if chain_key:sql+=' AND l.chain_key=?';args.append(chain_key)
        if message_key:sql+=' AND (l.chain_key IN (SELECT chain_key FROM mail_attachment_version_links WHERE package_id=? AND message_key=?) OR l.payload_sha256 IN (SELECT payload_sha256 FROM mail_attachment_version_links WHERE package_id=? AND message_key=?))';args.extend([package_id,message_key,package_id,message_key])
        rows=[dict(r) for r in c.execute(sql+' ORDER BY l.chain_key,l.version_ordinal,m.sent_at_utc',args)]
    return {'items':rows,'version_basis':'同名家族仅为候选版本关系；相同 SHA256 表示内容完全相同；序号按首次邮件时间排列'}
