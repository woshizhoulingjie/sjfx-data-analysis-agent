"""Versioned, disposable large-package QA indexes. Source databases are read-only."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager, ExitStack
from html.parser import HTMLParser
from pathlib import Path

from config import Config
from services.large_qa_resources import resource_code
from services import large_qa_thai as thai
from services.large_qa_changes import read_stamp

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


def _fingerprint_rows(rows, model, state):
    """Hash every selected SQLite value, preserving type, length and row order.

    This is a bounded full verification pass, not a timestamp/data_version
    shortcut. It detects deletes, same-size edits, labels, vectors and ACL flags.
    Only completed iterations expose a reusable fingerprint.
    """
    fingerprint = hashlib.sha256(('sjfx-qa-source-v1:' + digest(model)).encode())
    for row in rows:
        fingerprint.update(b'R')
        for value in row:
            if value is None:
                tag, data = b'N', b''
            elif isinstance(value, bytes):
                tag, data = b'B', value
            elif isinstance(value, str):
                tag, data = b'S', value.encode('utf-8')
            elif isinstance(value, int):
                tag, data = b'I', str(value).encode('ascii')
            elif isinstance(value, float):
                tag, data = b'F', value.hex().encode('ascii')
            else:
                raise TypeError('Unsupported SQLite fingerprint value')
            fingerprint.update(tag)
            fingerprint.update(len(data).to_bytes(8, 'big'))
            fingerprint.update(data)
        yield row
    state['signature'] = fingerprint.hexdigest()


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


LOGGER = logging.getLogger("sjfx.large_qa_index")


def _safe_text(value):
    """把落库会炸的孤立代理字符替换掉；其余字符原样保留。"""
    if not isinstance(value, str):
        return value
    try:
        value.encode('utf-8')
        return value
    except UnicodeEncodeError:
        return value.encode('utf-8', 'replace').decode('utf-8')


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
                self._ensure_member_columns(c)
                if not self.meta(c).get('index_epoch'):
                    self.set_meta(c, 'index_epoch', digest(time.time_ns()))
                thai.ensure(c)

    @property
    def exists(self):
        return self.path.exists()

    @staticmethod
    def _ensure_member_columns(connection):
        columns = {r[1] for r in connection.execute('PRAGMA table_info(documents)')}
        for name, default in (('source_kind', ''), ('source_metadata', '{}')):
            if name not in columns:
                connection.execute("ALTER TABLE documents ADD COLUMN " + name +
                                   " TEXT NOT NULL DEFAULT '" + default + "'")

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
        last_id = 0
        while True:
            batch = c.execute(sql+' AND c.id>? ORDER BY c.id LIMIT 128', args+[last_id]).fetchall()
            if not batch:
                break
            for row in batch:
                c.execute("INSERT INTO search(search,rowid,title,body) VALUES('delete',?,?,?)", (row['id'], ' '.join(tokens(Path(path).name+' '+path)), ' '.join(tokens(row['text']))))
                c.execute('DELETE FROM chunks WHERE id=?', (row['id'],))
                c.execute('DELETE FROM search_thai WHERE rowid=?', (row['id'],))
            last_id = batch[-1]['id']

    def _add_chunk(self, c, path, name, ordinal, kind, text, location, version):
        # 抽取文本可能含孤立代理字符（PDF 常见），直接绑定会抛 UnicodeEncodeError，
        # 进而把整个 put_original 事务回滚、该文件永远 pending。这里先净化。
        text = _safe_text(text)
        cur = c.execute('INSERT INTO chunks(path,ordinal,kind,text,location,version) VALUES(?,?,?,?,?,?)',
                        (path, ordinal, kind, text, json.dumps(location, ensure_ascii=False), version))
        c.execute('INSERT INTO search(rowid,title,body) VALUES(?,?,?)', (cur.lastrowid, ' '.join(tokens(Path(path).name+' '+path)), ' '.join(tokens(text))))
        thai.add_chunk(c, cur.lastrowid, path, text)

    def sync_rows(self, rows, package, model='', *, _source_state=None):
        """One transaction publishes summary/membership changes and invalidates ancestors."""
        # Stage on the index volume, not /tmp: a large package must not require
        # two complete Python dictionaries (including every vector/summary).
        # UPSERT retains first-seen order and last-value-wins duplicate semantics.
        fields = ('path', 'name', 'topic', 'topic_name', 'version', 'source_version',
                  'summary', 'size', 'mtime', 'sensitive', 'vector', 'source_kind', 'source_metadata')
        with tempfile.TemporaryDirectory(prefix='sync-', dir=self.path.parent) as staging:
            snapshot = Path(staging) / 'incoming.db'
            with closing(sqlite3.connect(str(snapshot))) as src:
                src.execute('''CREATE TABLE incoming(seq INTEGER PRIMARY KEY,path TEXT UNIQUE NOT NULL,
                  name TEXT,topic TEXT,topic_name TEXT,version TEXT,source_version TEXT,
                  summary TEXT,size INTEGER,mtime INTEGER,sensitive INTEGER,vector BLOB,
                  source_kind TEXT,source_metadata TEXT)''')
                insert = ('INSERT INTO incoming('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+') '
                          'ON CONFLICT(path) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in fields[1:]))
                with src:
                    for item in rows:
                        row = dict(item)
                        summary = summary_text(row) if not row.get('sensitive') else ''
                        topic = str(row.get('group_id') or row.get('category') or 'unclassified')
                        topic_name = str(row.get('topic_name') or row.get('category') or '未分类')
                        source_parts = [row.get('size'), row.get('modified_at_ns'), row.get('sha256'), row.get('sensitive')]
                        if row.get('source_kind') == 'archive_member':
                            from services.large_archive_members import source_signature
                            source_parts.append(source_signature(row))
                        source_version = digest(source_parts)
                        vector = row.get('vector')
                        version = digest([source_version, summary, topic, topic_name, digest(vector) if vector else None, model])
                        src.execute(insert, (row['path'], row.get('name') or Path(row['path']).name,
                            topic, topic_name, version, source_version, summary, row.get('size',0),
                            row.get('modified_at_ns',0), int(bool(row.get('sensitive'))), vector,
                            row.get('source_kind') or '', json.dumps(loads(row.get('source_metadata') or '{}'), sort_keys=True)))
            return self._publish_snapshot(snapshot, package, model,
                (_source_state or {}).get('signature'), (_source_state or {}).get('source_stamp'))

    def _publish_snapshot(self, snapshot, package, model, source_signature=None, source_stamp=None):
        changed = 0
        with self.connect(write=True) as c:
            self._ensure_member_columns(c)
            c.execute('ATTACH DATABASE ? AS incoming_snapshot', (str(snapshot),))
            c.execute('BEGIN IMMEDIATE')
            thai.ensure(c)
            dirty_topics = set()
            # Bounded batches avoid modifying a table while a cursor walks it.
            last_path = None
            while True:
                after = 'd.path>? AND ' if last_path is not None else ''
                missing = c.execute('''SELECT path,topic FROM documents d
                  WHERE '''+after+'''NOT EXISTS
                  (SELECT 1 FROM incoming_snapshot.incoming i WHERE i.path=d.path)
                  ORDER BY path LIMIT 128''', (() if last_path is None else (last_path,))).fetchall()
                if not missing:
                    break
                for old in missing:
                    dirty_topics.add(old['topic'])
                    self._delete_chunks(c, old['path'])
                    c.execute('DELETE FROM documents WHERE path=?', (old['path'],))
                    changed += 1
                last_path = missing[-1]['path']
            # Read-only snapshot cursor; destination lookups are a SQL join,
            # and unchanged rows never cross back into Python.
            def changed_rows():
                last_seq = 0
                while True:
                    batch = c.execute('''SELECT i.*,d.path old_path,d.topic old_topic,
                      d.source_version old_source_version,d.parse_status old_parse_status,
                      d.parse_complete old_parse_complete,d.parse_note old_parse_note,
                      d.attempted old_attempted,d.priority old_priority
                      FROM incoming_snapshot.incoming i LEFT JOIN documents d ON d.path=i.path
                      WHERE i.seq>? AND (d.path IS NULL OR d.version<>i.version)
                      ORDER BY i.seq LIMIT 128''', (last_seq,)).fetchall()
                    if not batch:
                        return
                    yield from batch
                    last_seq = batch[-1]['seq']
            for row in changed_rows():
                path = row['path']
                summary, topic, version = row['summary'], row['topic'], row['version']
                old = row['old_path'] is not None
                changed += 1
                dirty_topics.add(topic)
                dirty_topics.add(row['old_topic'] if old else topic)
                if old:
                    self._delete_chunks(c, path, 'summary')
                    if row['old_source_version'] != row['source_version']:
                        self._delete_chunks(c, path, 'original')
                same_source = old and row['old_source_version'] == row['source_version']
                c.execute('''INSERT OR REPLACE INTO documents
                  (path,name,topic,topic_name,version,source_version,summary,size,mtime,sensitive,vector,parse_status,parse_complete,parse_note,attempted,priority,source_kind,source_metadata)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (path,row['name'],topic,row['topic_name'],version,row['source_version'],summary,
                   row['size'],row['mtime'],row['sensitive'],row['vector'],
                   row['old_parse_status'] if same_source else ('restricted' if row['sensitive'] else 'pending'),
                   row['old_parse_complete'] if same_source else 0,row['old_parse_note'] if same_source else '',
                   row['old_attempted'] if same_source else 0, row['old_priority'] if same_source else 0,
                   row['source_kind'], row['source_metadata']))
                for ordinal, (text, start, end) in enumerate(split_text(summary, 1800, 120)):
                    self._add_chunk(c,path,row['name'],ordinal,'summary',text,
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
            self.set_meta(c, 'source_files', c.execute('SELECT COUNT(*) FROM incoming_snapshot.incoming').fetchone()[0])
            # Publish the fingerprint in the SAME transaction as the documents.
            # Direct sync_rows callers invalidate it, even if zero rows changed.
            meta = self.meta(c)
            self.set_meta(c, 'source_signature', None if source_signature is None else {
                'digest': source_signature, 'summary_generation': meta.get('summary_generation'),
                'index_epoch': meta.get('index_epoch'), 'embedding_model': model})
            self._set_source_stamp(c, source_stamp)
            thai.mark_current(c)
        return changed

    def _source_signature(self, c):
        meta = self.meta(c)
        stamp = meta.get('source_signature')
        # A rollback to an older binary can update documents without clearing
        # this new key. Its summary generation invalidates the old fingerprint.
        if not isinstance(stamp, dict):
            return None
        if any(stamp.get(k) != meta.get(k) for k in
               ('summary_generation', 'index_epoch', 'embedding_model')):
            return None
        return stamp.get('digest')

    def _set_source_stamp(self, c, stamp):
        signature = self.meta(c).get('source_signature')
        self.set_meta(c, 'source_change_stamp',
            {'source': stamp, 'signature': signature} if stamp is not None and signature else None)

    @staticmethod
    def _tracked_matches(metadata, stamp):
        tracked = metadata.get('source_change_stamp')
        return (stamp is not None and isinstance(tracked, dict)
                and tracked.get('source') == stamp
                and tracked.get('signature') == metadata.get('source_signature'))

    def _touch_tracked(self, stamp, signature, package, model):
        with self.connect(write=True) as c:
            c.execute('BEGIN IMMEDIATE')
            if (self._source_signature(c) != signature
                    or not self._tracked_matches(self.meta(c), stamp)):
                return False
            thai.ensure(c)
            self.set_meta(c, 'package', {k:package.get(k) for k in ('id','root_path','owner_id','updated_at')})
            self.set_meta(c, 'synced_at', time.time())
        return True

    def _touch_unchanged(self, signature, package, model, *, source_stamp=None):
        with self.connect(write=True) as c:
            c.execute('BEGIN IMMEDIATE')
            if self._source_signature(c) != signature:
                return False  # Another publisher won; perform the regular sync.
            thai.ensure(c)
            self.set_meta(c, 'package', {k:package.get(k) for k in ('id','root_path','owner_id','updated_at')})
            self.set_meta(c, 'embedding_model', model)
            self.set_meta(c, 'synced_at', time.time())
            self._set_source_stamp(c, source_stamp)
        return True

    def sync_store(self, store):
        # No LIMIT, no per-row decoration, no full text loaded in an HTTP turn.
        with ExitStack() as stack:
            connection = store._connect()
            # The normal store returns a Connection; the QA sidecar returns an
            # owning context manager which already starts a read transaction.
            src = stack.enter_context(closing(connection) if isinstance(connection, sqlite3.Connection) else connection)
            if not src.in_transaction:
                src.execute('BEGIN')
            def source_context():
                # Package ACL/root, token, model and rows share ONE snapshot.
                row = src.execute('SELECT * FROM packages WHERE id=?', (self.package_id,)).fetchone()
                if not row:
                    raise ValueError('Package no longer exists')
                models = {r[0] for r in src.execute(
                    'SELECT model FROM large_catalog_centers WHERE package_id=?', (self.package_id,))}
                return dict(row), next(iter(models)) if len(models)==1 else '', read_stamp(src, self.package_id)
            package, model, stamp = source_context()
            columns = {r[1] for r in src.execute('PRAGMA table_info(files)')}
            origin = 'f.source_kind' if 'source_kind' in columns else "'' AS source_kind"
            identity = 'f.source_metadata' if 'source_metadata' in columns else "'{}' AS source_metadata"
            query = '''SELECT f.path,f.name,f.category,f.size,f.modified_at_ns,f.sha256,
              f.sensitive,f.local_summary,f.deep_summary,''' + origin + ',' + identity + ''',
              CASE WHEN g.model=? THEN m.vector ELSE NULL END vector,m.group_id,l.name topic_name
              FROM files f LEFT JOIN large_catalog_members m ON m.package_id=f.package_id AND m.path=f.path
              LEFT JOIN large_catalog_groups g ON g.package_id=m.package_id AND g.id=m.group_id
              LEFT JOIN lp_catalog_labels l ON l.package_id=m.package_id AND l.group_id=m.group_id
              WHERE f.package_id=?'''
            if {'source_kind', 'source_changed'}.issubset(columns):
                query += " AND NOT (f.source_kind='archive_member' AND f.source_changed<>0)"
            with self.connect() as current:
                previous = self._source_signature(current)
                metadata = self.meta(current)
            if previous and self._tracked_matches(metadata, stamp):
                src.rollback()
                if self._touch_tracked(stamp, previous, package, model):
                    return 0
                src.execute('BEGIN')
                package, model, stamp = source_context()
            state = {'source_stamp': stamp}
            cacheable = package.get('status') in (None, 'completed', 'waiting_for_selection', 'paused')
            # A changed package stamp/model is only a reason to BYPASS the
            # fast check, never proof of equality. Active ingestion should not
            # pay for two scans when the source already announces a change.
            same_hint = ((metadata.get('package') or {}).get('updated_at') == package.get('updated_at')
                         and metadata.get('embedding_model') == model)
            # First enablement can bind a token after a complete hash check;
            # it need not normalize/stage all unchanged existing documents.
            first_tracking = stamp is not None and not metadata.get('source_change_stamp')
            if cacheable and previous and same_hint and (stamp is None or first_tracking):
                check = {}
                for _ in _fingerprint_rows(src.execute(query, (model, self.package_id)), model, check):
                    pass
                if check['signature'] == previous:
                    src.rollback()  # No source WAL snapshot while waiting on a writer.
                    touch_options = {'source_stamp': stamp} if stamp is not None else {}
                    if self._touch_unchanged(previous, package, model, **touch_options):
                        return 0
                    # Destination changed while we scanned. Start a new source
                    # snapshot rather than reusing rows from a released one.
                    src.execute('BEGIN')
                    package, model, stamp = source_context()
                    state = {'source_stamp': stamp}
                    cacheable = package.get('status') in (None, 'completed', 'waiting_for_selection', 'paused')
                else:
                    # Reuse the full fingerprint within this SAME source
                    # transaction; normalization still consumes every row.
                    state.update(check)
            rows = src.execute(query, (model, self.package_id))
            def snapshot_rows():
                try:
                    if (not cacheable and stamp is None) or state.get('signature'):
                        yield from rows
                    else:
                        yield from _fingerprint_rows(rows, model, state)
                finally:
                    # Staging is complete: release the source WAL snapshot before
                    # publishing the destination, so source checkpoints can proceed.
                    src.rollback()
            return self.sync_rows(snapshot_rows(), package, model, _source_state=state)

    def put_original(self, path, source_version, records, complete, note=''):
        with self.connect(write=True) as c:
            c.execute('BEGIN IMMEDIATE')
            doc = c.execute('SELECT * FROM documents WHERE path=?', (path,)).fetchone()
            if not doc or doc['source_version'] != source_version:
                return False
            thai.ensure(c)
            self._delete_chunks(c, path, 'original')
            ordinal = 0
            for record in records:
                try:
                    for text, start, end in split_text(record.get('text',''), 1600, 180):
                        location = dict(record.get('location') or {})
                        location.update(source='original', segment_char_start=start, segment_char_end=end)
                        self._add_chunk(c,path,doc['name'],ordinal,'original',text,location,source_version)
                        ordinal += 1
                except Exception as exc:                 # noqa: BLE001
                    if resource_code(exc) or isinstance(exc, thai.ThaiIndexError):
                        # Infrastructure exhaustion must roll back deletion and all
                        # inserts; it is not evidence that this paragraph is bad.
                        raise
                    # 单条记录坏掉不能拖死整个文件：记下原因，继续下一条。
                    LOGGER.warning('large QA chunk skipped path=%s: %s', path, str(exc)[:160])
                    note = ((note + '；') if note else '') + '部分段落无法写入：%s' % str(exc)[:120]
            if not ordinal and not complete:
                # 一条都没写进去、抽取也不完整 → 明确失败，绝不留在 pending 里空转。
                note = (note or '') or '没有可写入的内容'
            c.execute('UPDATE documents SET parse_status=?,parse_complete=?,parse_note=?,attempted=? WHERE path=?',
                      ('complete' if complete and ordinal else ('partial' if ordinal else 'failed'),int(bool(complete and ordinal)),note[:600],time.time(),path))
            self.set_meta(c,'generation',int(self.meta(c).get('generation') or 0)+1)
            thai.mark_current(c)
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
