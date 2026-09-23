"""Versioned, disposable large-package QA indexes. Source databases are read-only."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

from config import Config

SCHEMA = 1
STOP = {'资料', '文件', '文档', '内容', '什么', '哪些', '这个', '这些', '详细', '概括', '总结', '主要', '说明', '分析', '研究', 'the', 'and', 'with', 'from', 'this', 'that', 'what'}


def plain_text(value):
    """Remove presentation markup from excerpts without interpreting it."""
    value=str(value or '')
    if not re.search(r'<(?:!doctype|html|head|body|p\b|div\b|h[1-6]\b|script|style)',value,re.I):
        return value
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts=[]
            self.skip=0
        def handle_starttag(self,tag,attrs):
            if tag in {'script','style','head'}:
                self.skip+=1
            if tag in {'p','div','br','li','tr','h1','h2','h3','h4'} and not self.skip:
                self.parts.append('\n')
        def handle_endtag(self,tag):
            if tag in {'script','style','head'}:
                self.skip=max(0,self.skip-1)
            if tag in {'p','div','li','tr'} and not self.skip:
                self.parts.append('\n')
        def handle_data(self,data):
            if not self.skip:
                self.parts.append(data)
    reader=Reader()
    reader.feed(value);reader.close()
    return '\n'.join(x.strip() for x in ''.join(reader.parts).splitlines() if x.strip())


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(value).hexdigest()


def tokens(text):
    out = []
    for word in re.findall(r'[a-zA-Z0-9]+(?:[_.-][a-zA-Z0-9]+)*|[\u4e00-\u9fff]+', str(text).lower()):
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            out.extend(word[i:i+2] for i in range(max(1, len(word)-1)))
        else:
            out.append(word)
    return [x for x in out if x not in STOP]


def loads(value, default=None):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {} if default is None else default


def summary_text(row):
    from services.conversation_package_context import _text
    local, deep = loads(row.get('local_summary'), {}), loads(row.get('deep_summary'), {})
    keys = ('summary', 'text', 'sample', 'abstract', 'conclusion', 'key_findings')
    selected = deep if deep.get('summary_type') != 'model_pending' and any(deep.get(k) for k in keys) else local
    return _text(selected, 16000) if any(selected.get(k) for k in keys) else ''


def index_path(package_id, base=None):
    # Never interpolate a supplied package identifier into a filesystem path.
    root = Path(base or getattr(Config, 'LARGE_QA_INDEX_DIR', Config.DATA_DIR / 'large_qa'))
    return root.resolve() / digest(str(package_id))[:32] / 'index.db'


def enabled():
    return bool(getattr(Config, 'LARGE_QA_ENABLED', True))


class LargeQAIndex:
    def __init__(self, package_id, base=None, create=False):
        self.package_id = str(package_id)
        self.path = index_path(package_id, base)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.connect(write=True) as c:
                c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents(
                  path TEXT PRIMARY KEY,name TEXT,topic TEXT,topic_name TEXT,version TEXT,source_version TEXT,
                  summary TEXT,size INTEGER,mtime INTEGER,sensitive INTEGER,vector BLOB,
                  parse_status TEXT DEFAULT 'pending',parse_complete INTEGER DEFAULT 0,
                  parse_note TEXT DEFAULT '',attempted REAL DEFAULT 0,priority INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS docs_topic ON documents(topic);
                CREATE INDEX IF NOT EXISTS docs_parse ON documents(parse_status,priority);
                CREATE TABLE IF NOT EXISTS chunks(
                  id INTEGER PRIMARY KEY,path TEXT NOT NULL,ordinal INTEGER,kind TEXT,text TEXT,
                  location TEXT,version TEXT,UNIQUE(path,kind,ordinal));
                CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path,kind);
                CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(title,body,content='');
                CREATE TABLE IF NOT EXISTS topic_nodes(
                  id TEXT PRIMARY KEY,topic TEXT,level INTEGER,version TEXT,
                  children TEXT,paths TEXT,text TEXT,status TEXT,updated REAL,attempts INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS nodes_level ON topic_nodes(level,status);
                CREATE TABLE IF NOT EXISTS events(at REAL,kind TEXT,payload TEXT);
                ''')
                self.set_meta(c, 'schema', SCHEMA)
                if not self.meta(c).get('index_epoch'):
                    self.set_meta(c, 'index_epoch', digest(time.time_ns()))

    @property
    def exists(self):
        return self.path.exists()

    @contextmanager
    def connect(self, write=False):
        c = sqlite3.connect(str(self.path) if write else self.path.as_uri()+'?mode=ro', uri=not write, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA busy_timeout=20000')
        if not write:
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
        try:
            yield c
            if write:
                c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def set_meta(self, c, key, value):
        c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def meta(self, c):
        return {r['key']: loads(r['value']) for r in c.execute('SELECT * FROM meta')}

    def _delete_chunks(self, c, path, kind=None):
        sql = 'SELECT c.*,d.name FROM chunks c JOIN documents d USING(path) WHERE path=?'
        args = [path]
        if kind:
            sql += ' AND kind=?'
            args.append(kind)
        for row in c.execute(sql, args).fetchall():
            c.execute("INSERT INTO search(search,rowid,title,body) VALUES('delete',?,?,?)", (row['id'], ' '.join(tokens(Path(path).name+' '+path)), ' '.join(tokens(row['text']))))
            c.execute('DELETE FROM chunks WHERE id=?', (row['id'],))

    def _add_chunk(self, c, path, name, ordinal, kind, text, location, version):
        cur = c.execute('INSERT INTO chunks(path,ordinal,kind,text,location,version) VALUES(?,?,?,?,?,?)',
                        (path, ordinal, kind, text, json.dumps(location, ensure_ascii=False), version))
        c.execute('INSERT INTO search(rowid,title,body) VALUES(?,?,?)', (cur.lastrowid, ' '.join(tokens(Path(path).name+' '+path)), ' '.join(tokens(text))))

    def sync_rows(self, rows, package, model=''):
        """One transaction publishes summary/membership changes and invalidates ancestors."""
        incoming = {r['path']: dict(r) for r in rows}
        changed = 0
        with self.connect(write=True) as c:
            c.execute('BEGIN IMMEDIATE')
            existing = {r['path']: dict(r) for r in c.execute('SELECT * FROM documents')}
            dirty_topics = set()
            for path in existing.keys() - incoming.keys():
                dirty_topics.add(existing[path]['topic'])
                self._delete_chunks(c, path)
                c.execute('DELETE FROM documents WHERE path=?', (path,))
                changed += 1
            for path, row in incoming.items():
                summary = summary_text(row) if not row.get('sensitive') else ''
                topic = str(row.get('group_id') or row.get('category') or 'unclassified')
                topic_name = str(row.get('topic_name') or row.get('category') or '未分类')
                source_version = digest([row.get('size'), row.get('modified_at_ns'), row.get('sha256'), row.get('sensitive')])
                vector = row.get('vector')
                version = digest([source_version, summary, topic, topic_name, digest(vector) if vector else None, model])
                old = existing.get(path)
                if old and old['version'] == version:
                    continue
                changed += 1
                dirty_topics.add(topic)
                dirty_topics.add(old['topic'] if old else topic)
                if old:
                    self._delete_chunks(c, path, 'summary')
                    if old['source_version'] != source_version:
                        self._delete_chunks(c, path, 'original')
                same_source = old and old['source_version'] == source_version
                c.execute('''INSERT OR REPLACE INTO documents
                  (path,name,topic,topic_name,version,source_version,summary,size,mtime,sensitive,vector,parse_status,parse_complete,parse_note,attempted,priority)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (path,row.get('name') or Path(path).name,topic,topic_name,version,source_version,summary,
                   row.get('size',0),row.get('modified_at_ns',0),int(bool(row.get('sensitive'))),vector,
                   old['parse_status'] if same_source else ('restricted' if row.get('sensitive') else 'pending'),
                   old['parse_complete'] if same_source else 0,old['parse_note'] if same_source else '',
                   old['attempted'] if same_source else 0, old['priority'] if same_source else 0))
                for ordinal, (text, start, end) in enumerate(split_text(summary, 1800, 120)):
                    self._add_chunk(c,path,row.get('name') or Path(path).name,ordinal,'summary',text,
                                    {'source':'file_summary','char_start':start,'char_end':end},version)
            if changed:
                for topic in dirty_topics:
                    c.execute('DELETE FROM topic_nodes WHERE topic=?', (topic,))
                c.execute('DELETE FROM topic_nodes WHERE level>0')
                self.set_meta(c, 'generation', int(self.meta(c).get('generation') or 0)+1)
                self.set_meta(c, 'summary_generation', int(self.meta(c).get('summary_generation') or 0)+1)
            self.set_meta(c, 'package', {k:package.get(k) for k in ('id','root_path','owner_id','updated_at')})
            self.set_meta(c, 'embedding_model', model)
            self.set_meta(c, 'synced_at', time.time())
            self.set_meta(c, 'source_files', len(incoming))
        return changed

    def sync_store(self, store):
        package = store.get(self.package_id)
        if not package:
            raise ValueError('Package no longer exists')
        # No LIMIT, no per-row decoration, no full text loaded in an HTTP turn.
        with store._connect() as src:
            model_rows = src.execute('SELECT model FROM large_catalog_centers WHERE package_id=?', (self.package_id,)).fetchall()
            models = {r[0] for r in model_rows}
            rows = src.execute('''SELECT f.*,CASE WHEN g.model=? THEN m.vector ELSE NULL END vector,m.group_id,l.name topic_name
              FROM files f LEFT JOIN large_catalog_members m ON m.package_id=f.package_id AND m.path=f.path
              LEFT JOIN large_catalog_groups g ON g.package_id=m.package_id AND g.id=m.group_id
              LEFT JOIN lp_catalog_labels l ON l.package_id=m.package_id AND l.group_id=m.group_id
              WHERE f.package_id=?''', (next(iter(models)) if len(models)==1 else '',self.package_id)).fetchall()
        return self.sync_rows(rows, package, next(iter(models)) if len(models)==1 else '')

    def put_original(self, path, source_version, records, complete, note=''):
        with self.connect(write=True) as c:
            c.execute('BEGIN IMMEDIATE')
            doc = c.execute('SELECT * FROM documents WHERE path=?', (path,)).fetchone()
            if not doc or doc['source_version'] != source_version:
                return False
            self._delete_chunks(c, path, 'original')
            ordinal = 0
            for record in records:
                for text, start, end in split_text(record.get('text',''), 1600, 180):
                    location = dict(record.get('location') or {})
                    location.update(source='original', segment_char_start=start, segment_char_end=end)
                    self._add_chunk(c,path,doc['name'],ordinal,'original',text,location,source_version)
                    ordinal += 1
            c.execute('UPDATE documents SET parse_status=?,parse_complete=?,parse_note=?,attempted=? WHERE path=?',
                      ('complete' if complete and ordinal else ('partial' if ordinal else 'failed'),int(bool(complete and ordinal)),note[:600],time.time(),path))
            self.set_meta(c,'generation',int(self.meta(c).get('generation') or 0)+1)
            # Topic reports based on summaries remain valid; query-time facts use originals.
        return True

    def enqueue(self, paths):
        if not paths:
            return
        with self.connect(write=True) as c:
            c.executemany("UPDATE documents SET priority=priority+1 WHERE path=? AND parse_status='pending'", [(p,) for p in paths])

    def next_parse(self):
        with self.connect() as c:
            row = c.execute("SELECT * FROM documents WHERE parse_status='pending' ORDER BY priority DESC,path LIMIT 1").fetchone()
            return dict(row) if row else None

    def build_topic_leaves(self):
        """Each summary participates in exactly one bounded leaf; never sampled-complete."""
        with self.connect(write=True) as c:
            topics = c.execute('SELECT DISTINCT topic FROM documents ORDER BY topic').fetchall()
            for entry in topics:
                topic = entry[0]
                if c.execute('SELECT 1 FROM topic_nodes WHERE topic=? AND level=0 LIMIT 1',(topic,)).fetchone():
                    continue
                rows = c.execute('SELECT path,summary,version FROM documents WHERE topic=? AND summary!="" ORDER BY path',(topic,)).fetchall()
                batch, size, batch_num = [], 0, 0
                def save(values, number):
                    material = [{'path':r['path'],'text':r['summary'],'version':r['version']} for r in values]
                    node_id = digest([topic,number])[:24]
                    c.execute('INSERT OR REPLACE INTO topic_nodes VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (node_id,topic,0,digest(material),'[]',json.dumps([r['path'] for r in values]),json.dumps(material,ensure_ascii=False),'pending',time.time(),0))
                for row in rows:
                    if batch and (size+len(row['summary'])>24000 or len(batch)>=30):
                        save(batch,batch_num); batch_num+=1; batch=[]; size=0
                    batch.append(row); size+=len(row['summary'])
                if batch:
                    save(batch,batch_num)

    def topic_material(self, c, allowed=None):
        rows = c.execute('SELECT topic,topic_name,COUNT(*) files,SUM(summary!="") summaries FROM documents GROUP BY topic ORDER BY topic').fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            if allowed is not None:
                scoped = c.execute('SELECT path FROM documents WHERE topic=?',(row['topic'],)).fetchall()
                item['files']=sum(r[0] in allowed for r in scoped)
                if not item['files']:
                    continue
            nodes = c.execute("SELECT * FROM topic_nodes WHERE topic=? AND status='ready' ORDER BY level DESC,id",(row['topic'],)).fetchall()
            # Only reports whose entire provenance falls in scope can be reused.
            valid=[dict(n) for n in nodes if allowed is None or set(loads(n['paths'],[])).issubset(allowed)]
            highest=max((n['level'] for n in valid),default=-1)
            item['reports']=[n for n in valid if n['level']==highest]
            item['covered_paths']=sorted({p for n in item['reports'] for p in loads(n['paths'],[])})
            result.append(item)
        return result


def split_text(text, limit=1600, overlap=180):
    """Exact windows, sentence-aware where possible, with contextual overlap."""
    text=str(text or '')
    pos=0
    while pos<len(text):
        end=min(len(text),pos+limit)
        if end<len(text):
            cut=max(text.rfind('\n',pos+limit//2,end),text.rfind('。',pos+limit//2,end))
            if cut>=0:
                end=cut+1
        body=text[pos:end]
        if body.strip():
            yield body,pos,end
        if end>=len(text):
            break
        pos=max(pos+1,end-overlap)
