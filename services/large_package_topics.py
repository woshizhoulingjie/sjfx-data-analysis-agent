"""Incremental, bounded content grouping for isolated portraits.

Candidate lookup uses an inverted term index and at most 24 comparisons, never
an all-pairs pass. Group IDs remain stable when representative labels improve.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
from collections import Counter


def init(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS portrait_groups (
      package_id TEXT NOT NULL, id TEXT NOT NULL, family TEXT NOT NULL,
      label TEXT NOT NULL, terms TEXT NOT NULL, basis TEXT NOT NULL,
      PRIMARY KEY(package_id,id));
    CREATE TABLE IF NOT EXISTS portrait_group_terms (
      package_id TEXT NOT NULL, term TEXT NOT NULL, group_id TEXT NOT NULL,
      PRIMARY KEY(package_id,term,group_id));
    CREATE TABLE IF NOT EXISTS portrait_facets (
      package_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
      PRIMARY KEY(package_id,path,kind,value));
    CREATE INDEX IF NOT EXISTS idx_portrait_facet_value ON portrait_facets(package_id,kind,value);
    ''')


def _features(info):
    # Headers, topics and document role anchor groups; source names alone do not.
    values = info.get('fields') or info.get('headings') or info.get('keywords') or []
    words = []
    for value in values[:16]:
        value = re.sub(r'(?:19|20)\d{2}|\d+', '', str(value).lower())
        words.extend(re.findall(r'[a-z][a-z_-]{2,}|[\u4e00-\u9fff]{2,12}', value))
    return sorted(set(words))[:32]


def assign(conn, package_id, path, info):
    conn.execute('DELETE FROM portrait_facets WHERE package_id=? AND path=?', (package_id, path))
    for kind, values in (('role', [info.get('document_role')]), ('project', info.get('projects', [])),
                         ('entity', info.get('entities', [])), ('date', info.get('dates', []))):
        for value in values[:12]:
            if value:
                conn.execute('INSERT OR IGNORE INTO portrait_facets VALUES(?,?,?,?)', (package_id, path, kind, str(value)[:120]))
    topic = info.get('topic') or '待识别内容'
    if info.get('recognition_status') in {'restricted','unsupported','metadata_only','needs_ocr'}:
        return topic
    if info.get('catalog_version'):
        return topic
    role = info.get('document_role') or '文档'
    family = (info.get('category') or '未分类') + '/' + role
    terms = _features(info)
    if not terms:
        return topic
    # Explicit topics are retained. Unknown subjects are merged only with strong
    # schema/title agreement; similar templates do not establish a shared matter.
    projects = info.get('projects') or []
    if projects:
        label = '{} / {} / {}'.format(topic, projects[0], role)
    elif info.get('quality') == 'identified':
        label = '{} / {}'.format(topic, role)
    else:
        label = '{} / {}'.format(topic, role)
        placeholders = ','.join('?' for _ in terms)
        rows = conn.execute('SELECT g.* FROM portrait_groups g JOIN (SELECT group_id,COUNT(*) hits FROM portrait_group_terms WHERE package_id=? AND term IN (' + placeholders + ') GROUP BY group_id ORDER BY hits DESC LIMIT 24) c ON g.id=c.group_id WHERE g.package_id=? AND g.family=? ORDER BY c.hits DESC', [package_id] + terms + [package_id, family])
        wanted = set(terms)
        for row in rows:
            previous = set(json.loads(row['terms']))
            score = len(previous & wanted) / max(1, len(previous | wanted))
            if score >= .72:
                info['grouping_basis'] = '采样字段/标题词相似度 {:.2f}；仅用于导航，不代表同一事项'.format(score)
                return row['label']
    label = label[:180]
    group_id = hashlib.sha256((family + '\0' + label).encode()).hexdigest()[:24]
    conn.execute('INSERT OR IGNORE INTO portrait_groups VALUES(?,?,?,?,?,?)', (package_id, group_id, family, label, json.dumps(terms, ensure_ascii=False), info.get('classification_basis', '')))
    for term in terms:
        conn.execute('INSERT OR IGNORE INTO portrait_group_terms VALUES(?,?,?)', (package_id, term, group_id))
    info['grouping_basis'] = '业务主题与文档用途分组；具体事项以原文编号为线索'
    return label
