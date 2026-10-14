"""SQLite content guard for direct producers and projected/joined consumers.

Read authorization follows real rows through guarded source views, not SQL
parameters or a newly computed package fingerprint. Metadata columns are exempt.
Only explicit content writes create provenance. Unknown old content cannot be
made trusted by a metadata update or a partial content update.
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import sqlite3

from . import business, cache
from .store import ProtectionError

_TOKENS = re.compile(r"'(?:''|[^'])*'|--[^\r\n]*|/\*[\s\S]*?\*/|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_]*|\s+|.")


def _quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def _name(token):
    if token.startswith(('"', '`', '[')):
        end = ']' if token[0] == '[' else token[0]
        return token[1:-1].replace(end + end, end).lower()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
        return token.lower()
    return None


def _rewrite(sql, replacements):
    """Replace SQL identifiers only, preserving literals/comments and DML target."""
    tokens = _TOKENS.findall(sql)
    significant = [i for i, token in enumerate(tokens) if not token.isspace() and not token.startswith(('--', '/*'))]
    names = [_name(tokens[i]) for i in significant]
    target = set()
    for pos, name in enumerate(names):
        if name == 'into' or (name == 'update' and (pos == 0 or names[pos - 1] not in {'do', 'on'})):
            next_pos = pos + 1
            if name == 'update' and next_pos < len(names) and names[next_pos] == 'or':
                next_pos += 2
            if next_pos < len(names):
                target.add(significant[next_pos])
                if next_pos + 2 < len(names) and tokens[significant[next_pos + 1]] == '.':
                    target.update(significant[next_pos:next_pos + 3])
        if name == 'delete' and pos + 2 < len(names) and names[pos + 1] == 'from':
            target.add(significant[pos + 2])
            if pos + 4 < len(names) and tokens[significant[pos + 3]] == '.':
                target.update(significant[pos + 2:pos + 5])
    changed = set()
    for pos, index in enumerate(significant):
        name = names[pos]
        if index in target or name not in replacements:
            continue
        if pos > 1 and tokens[significant[pos - 1]] == '.' and names[pos - 2] in {'main', 'temp'}:
            tokens[significant[pos - 2]] = 'temp'
        tokens[index] = _quote(replacements[name])
        changed.add(name)
    return ''.join(tokens), changed


class GuardedCursor:
    def __init__(self, guard, cursor):
        self.guard, self.cursor = guard, cursor

    def _call(self, method, *args):
        try:
            return getattr(self.cursor, method)(*args)
        except sqlite3.Error:
            self.guard._raise_callback_error()
            raise

    def fetchone(self):
        return self._call('fetchone')

    def fetchmany(self, *args):
        return self._call('fetchmany', *args)

    def fetchall(self):
        return self._call('fetchall')

    def __iter__(self):
        return self

    def __next__(self):
        value = self.fetchone()
        if value is None:
            raise StopIteration
        return value

    def execute(self, sql, parameters=()):
        result = self.guard.execute(sql, parameters)
        self.cursor = result.cursor
        return self

    def executemany(self, sql, rows):
        result = self.guard.executemany(sql, rows)
        self.cursor = result.cursor
        return self

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class GuardedConnection:
    """One business connection; its provenance namespace is the actual DB path.

    specs: table -> {content: tuple, identity: tuple, pk: optional tuple,
                     fts: optional tuple of FTS5 column definitions}.
    resolve_scopes(raw_db, table, actual_row) -> [{owner: ..., scope: ...}].
    The resolver must use real stored ownership/locators and current approved
    contexts. An empty result hides out-of-scope rows and rejects content writes.
    """
    def __init__(self, db, namespace, specs, resolve_scopes):
        self.raw, self.namespace, self.resolve_scopes = db, namespace, resolve_scopes
        self.specs = {}
        self.sources = None
        self.callback_error = None
        self._row_memo = {}
        self._gate_memo = {}
        self._fresh_tables = set()
        self._fresh_update_tables = set()
        self._write_columns = {}
        self._source_markers = set()
        self._version_digests = {}
        self._journal = '_sjfx_content_changes'
        db.execute('CREATE TEMP TABLE IF NOT EXISTS ' + _quote(self._journal) +
                   '(table_name TEXT NOT NULL,row_id INTEGER NOT NULL,PRIMARY KEY(table_name,row_id))')
        db.create_function('_sjfx_row_ok', 2, self._row_ok)
        db.create_function('_sjfx_check_old', 2, self._check_old)
        for table, requested in specs.items():
            # Generated/hidden columns can also shadow rowid aliases; table_info
            # omits them and would let different rows share a forged gate key.
            columns = list(db.execute('PRAGMA main.table_xinfo(' + _quote(table) + ')'))
            if not columns:
                continue
            actual = {row['name'] if isinstance(row, sqlite3.Row) else row[1] for row in columns}
            content = tuple(name for name in requested.get('content', ()) if name in actual)
            if not content:
                continue
            pk = tuple(requested.get('pk') or [
                row['name'] if isinstance(row, sqlite3.Row) else row[1]
                for row in sorted(columns, key=lambda row: row['pk'] if isinstance(row, sqlite3.Row) else row[5])
                if (row['pk'] if isinstance(row, sqlite3.Row) else row[5])])
            if not pk:
                pk = ('__rowid__',)
            identity = tuple(dict.fromkeys(pk + tuple(name for name in requested.get('identity', ()) if name in actual)))
            # Views do not inherit SQLite's implicit row IDs. Expose each
            # unshadowed alias explicitly, keeping declared columns first and
            # intact, and use the same real identifier for the content gate.
            aliases = [name for name in ('rowid', '_rowid_', 'oid')
                       if name not in {str(col).lower() for col in actual}]
            if not aliases:
                raise ProtectionError('内容表没有可核验的 SQLite 行标识', 423, 'cache_query_unverified')
            try:
                # Compile a qualified, unquoted identifier even for empty tables.
                # WITHOUT ROWID has no such field: quoting an unknown alias can
                # instead become a string literal under SQLite's DQS fallback.
                db.execute('SELECT _sjfx_source.' + aliases[0] + ' FROM main.' +
                           _quote(table) + ' AS _sjfx_source LIMIT 0')
            except sqlite3.OperationalError as exc:
                raise ProtectionError('内容表没有可核验的 SQLite 行标识', 423, 'cache_query_unverified') from exc
            self.specs[table] = {'content': content, 'pk': pk, 'identity': identity,
                                 'fts': requested.get('fts'), 'view': '_sjfx_checked_' + table,
                                 'rowid': aliases[0],
                                 'metadata': requested.get('metadata')}
            spec = self.specs[table]
            if spec['fts']:
                continue
            db.execute('CREATE TEMP VIEW IF NOT EXISTS ' + _quote(spec['view']) +
                       ' AS SELECT *,' + ','.join(_quote(spec['rowid']) + ' AS ' + _quote(alias) for alias in aliases) +
                       ' FROM main.' + _quote(table) +
                       ' WHERE _sjfx_row_ok(' + "'" + table.replace("'", "''") + "'," + _quote(spec['rowid']) + ')=1')
            escaped = table.replace("'", "''")
            db.execute('CREATE TEMP TRIGGER IF NOT EXISTS ' + _quote('_sjfx_insert_' + table) +
                       ' AFTER INSERT ON main.' + _quote(table) + ' BEGIN INSERT OR REPLACE INTO ' +
                       _quote(self._journal) + " VALUES('" + escaped + "',NEW." + _quote(spec['rowid']) + '); END')
            differences = ' OR '.join('OLD.' + _quote(col) + ' IS NOT NEW.' + _quote(col) for col in content)
            db.execute('CREATE TEMP TRIGGER IF NOT EXISTS ' + _quote('_sjfx_before_' + table) +
                       ' BEFORE UPDATE OF ' + ','.join(_quote(col) for col in content) +
                       ' ON main.' + _quote(table) + ' WHEN ' + differences +
                       " BEGIN SELECT _sjfx_check_old('" + escaped + "',OLD." + _quote(spec['rowid']) + '); END')
            db.execute('CREATE TEMP TRIGGER IF NOT EXISTS ' + _quote('_sjfx_update_' + table) +
                       ' AFTER UPDATE OF ' + ','.join(_quote(col) for col in content) +
                       ' ON main.' + _quote(table) + ' WHEN ' + differences +
                       ' BEGIN INSERT OR REPLACE INTO ' + _quote(self._journal) +
                       " VALUES('" + escaped + "',NEW." + _quote(spec['rowid']) + '); END')

    def _raise_callback_error(self):
        if self.callback_error is not None:
            error, self.callback_error = self.callback_error, None
            raise error

    def _row(self, table, rowid):
        identifier = _quote(self.specs[table]['rowid'])
        cursor = self.raw.execute('SELECT ' + identifier + ' AS __rowid__,* FROM main.' + _quote(table) +
                                  ' WHERE ' + identifier + '=?', (rowid,))
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row) if hasattr(row, 'keys') else dict(zip([item[0] for item in cursor.description], row))

    def _payload(self, table, row):
        spec = self.specs[table]
        return {col: row.get(col) for col in dict.fromkeys(spec['identity'] + spec['content'])}

    def _key(self, table, row, scope):
        return cache.resource_key(self.namespace, table,
                                  *[row.get(col) for col in self.specs[table]['pk']], scope['scope'])

    def _pending(self, table, rowid):
        return self.raw.execute('SELECT 1 FROM ' + _quote(self._journal) +
                                ' WHERE table_name=? AND row_id=?', (table, rowid)).fetchone() is not None

    def _capture(self):
        if self.sources is None:
            self._add_sources(cache.capture_sources())

    def _add_sources(self, sources):
        if self.sources is None:
            self.sources = []
        for source in sources:
            marker = cache._source_token(source, self._version_digests)
            if marker not in self._source_markers:
                self._source_markers.add(marker)
                self.sources.append(source)

    def _validate(self, table, row, *, allow_pending=True):
        scopes = self.resolve_scopes(self.raw, table, row)
        if not scopes:
            return False
        metadata_validator = self.specs[table].get('metadata')
        if metadata_validator is not None and metadata_validator(self.raw, row):
            return True
        payload = self._payload(table, row)
        if allow_pending and self._pending(table, row['__rowid__']):
            self._capture()
            # Recheck the immutable producer context, even within one transaction.
            p = business.protection()
            marker = ('pending-gate', tuple(sorted(self._source_markers)), id(business.context()))
            if marker not in self._gate_memo:
                owner = self.sources[0]['context']['owner']
                cache._current(p, owner, self.sources, 'read')
                for source in self.sources:
                    cache._require_source(p, source, owner, 'read')
                self._gate_memo[marker] = True
            return True
        verified = []
        for scope in scopes:
            key = self._key(table, row, scope)
            sources = cache.require_payload(key, payload, owner=scope['owner'],
                                            return_sources=True, _gate_memo=self._gate_memo)
            verified.extend(sources)
        if verified:
            self._add_sources(verified)
        return True

    def _row_ok(self, table, rowid):
        try:
            row = self._row(table, rowid)
            if row is None:
                return 0
            marker = (table, rowid, cache.canonical_hash(self._payload(table, row)))
            if marker not in self._row_memo:
                self._row_memo[marker] = self._validate(table, row)
            return int(self._row_memo[marker])
        except BaseException as exc:
            self.callback_error = exc
            raise

    def _check_old(self, table, rowid):
        try:
            row = self._row(table, rowid)
            if table in self._fresh_update_tables and row is not None:
                if not self.resolve_scopes(self.raw, table, row):
                    raise ProtectionError('原字节重建目标不在获准范围', 423, 'cache_scope_mismatch')
                return 1
            if row is None or not self._validate(table, row):
                raise ProtectionError('修改内容不在当前获准来源集合内', 423, 'cache_scope_mismatch')
            return 1
        except BaseException as exc:
            self.callback_error = exc
            raise

    def _inspect(self, sql, parameters):
        reads, writes = set(), set()
        self._write_columns = {}
        self._any_writes = False
        def authorizer(action, table, column, database, trigger):
            if trigger and trigger.startswith('_sjfx_'):
                return sqlite3.SQLITE_OK
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                self._any_writes = True
            if table in self.specs and database != 'temp':
                spec = self.specs[table]
                if action == sqlite3.SQLITE_READ and (column in spec['content'] or spec['fts'] and column == table):
                    reads.add(table)
                if action == sqlite3.SQLITE_INSERT or action == sqlite3.SQLITE_UPDATE and column in spec['content']:
                    writes.add(table)
                if action == sqlite3.SQLITE_UPDATE:
                    self._write_columns.setdefault(table, set()).add(column)
            return sqlite3.SQLITE_OK
        self.raw.set_authorizer(authorizer)
        try:
            self.raw.execute('EXPLAIN ' + sql, parameters).fetchall()
        finally:
            self.raw.set_authorizer(None)
        return reads, writes

    def _prepare_fts(self, table):
        spec = self.specs[table]
        self.raw.execute('CREATE VIRTUAL TABLE IF NOT EXISTS temp.' + _quote(spec['view']) +
                         ' USING fts5(' + ','.join(spec['fts']) + ')')
        self.raw.execute('DELETE FROM temp.' + _quote(spec['view']))
        columns = [definition.split()[0] for definition in spec['fts']]
        for row in self._candidate_rows(table):
            row = dict(row)
            if self._validate(table, row):
                self.raw.execute('INSERT INTO temp.' + _quote(spec['view']) +
                                 '(' + ','.join(_quote(col) for col in columns) + ') VALUES(' +
                                 ','.join('?' for _ in columns) + ')', [row[col] for col in columns])

    def _candidate_rows(self, table):
        candidates = getattr(self.resolve_scopes, 'rows', None)
        if candidates is not None:
            return list(candidates(self.raw, table))
        return self.raw.execute('SELECT ' + _quote(self.specs[table]['rowid']) +
                                ' AS __rowid__,* FROM main.' + _quote(table)).fetchall()

    def _metadata_write(self, sql, parameters, reads, writes):
        """Try typed control writes atomically; rejected candidates fall back.

        Real row ownership and both old/new values still pass their validators.
        The savepoint prevents an invalid JSON value or a mixed trigger write
        from escaping into the database without content-source authorization.
        """
        if (not writes or reads or re.search(r'\bRETURNING\b', sql, re.I) or
            any(self.specs[table].get('metadata') is None for table in writes) or
            self.raw.execute('SELECT 1 FROM ' + _quote(self._journal) + ' LIMIT 1').fetchone()):
            return None
        # RELEASE of an outermost savepoint commits SQLite implicitly. Keep a
        # real outer transaction so the connection owner can still roll back
        # this metadata alongside any later content write or exception.
        if not self.raw.in_transaction:
            self.raw.execute('BEGIN')
        self.raw.execute('SAVEPOINT _sjfx_metadata_write')
        try:
            cursor = self.raw.execute(sql, parameters)
            changes = list(self.raw.execute('SELECT table_name,row_id FROM ' + _quote(self._journal)))
            for table, rowid in changes:
                row = self._row(table, rowid)
                validator = self.specs[table].get('metadata')
                if (table not in writes or row is None or validator is None or
                    not self.resolve_scopes(self.raw, table, row) or not validator(self.raw, row)):
                    self.raw.execute('ROLLBACK TO _sjfx_metadata_write')
                    self.raw.execute('RELEASE _sjfx_metadata_write')
                    return None
            self.raw.execute('DELETE FROM ' + _quote(self._journal))
            self.raw.execute('RELEASE _sjfx_metadata_write')
            return GuardedCursor(self, cursor)
        except BaseException:
            self.raw.execute('ROLLBACK TO _sjfx_metadata_write')
            self.raw.execute('RELEASE _sjfx_metadata_write')
            self._raise_callback_error()
            raise

    def execute(self, sql, parameters=()):
        self.callback_error = None
        self._row_memo = {}
        # Immutable source objects share decoding; admission gates always reset.
        self._gate_memo = {key: value for key, value in self._gate_memo.items()
                           if key[0] in {'source-object', 'source-set'}}
        reads, writes = self._inspect(sql, parameters)
        metadata_projection = getattr(business, 'metadata_projection', None)
        if metadata_projection is not None and metadata_projection.get() and not self._any_writes:
            # Trusted middleware enables this only for its exact allowlist and
            # sanitizes/buffers the entire response. Never infer it from ctx or
            # request data; producer behavior remains unchanged.
            return GuardedCursor(self, self.raw.execute(sql, parameters))
        metadata_result = self._metadata_write(sql, parameters, reads, writes)
        if metadata_result is not None:
            return metadata_result
        if writes:
            self._capture()
        self._fresh_update_tables = {table for table in self._fresh_tables & writes
                                    if set(self.specs[table]['content']).issubset(self._write_columns.get(table, set()))}
        before = {}
        simple_insert = bool(re.match(r'^\s*(INSERT|REPLACE)\b', sql, re.I)) and not re.search(r'\bSELECT\b', sql, re.I)
        for table in writes:
            if self.specs[table]['fts']:
                if self._write_columns.get(table):
                    # Virtual tables cannot have BEFORE UPDATE triggers. Require
                    # a real producer DELETE+INSERT rather than silently blessing
                    # partial historical FTS edits.
                    raise ProtectionError('全文索引必须由已验证输入重新生成', 423, 'cache_fts_update_denied')
                if not simple_insert:
                    before[table] = {row['__rowid__']: cache.canonical_hash(dict(row))
                                     for row in self._candidate_rows(table)
                                     if self.resolve_scopes(self.raw, table, dict(row))}
        for table in reads:
            if self.specs[table]['fts']:
                self._prepare_fts(table)
        rewritten, changed = _rewrite(sql, {table: self.specs[table]['view'] for table in reads})
        if reads - changed - writes:
            # An uninstrumented persistent view may hide the content table.
            raise ProtectionError('内容查询未能关联确切来源行', 423, 'cache_query_unverified')
        try:
            capture_returning = bool(self._fresh_update_tables) and not re.search(r'\bRETURNING\b', sql, re.I)
            if capture_returning:
                # Full raw-regenerated UPDATE/UPSERT may produce identical bytes.
                # RETURNING identifies exact actual affected rows without washing
                # unrelated rows in the same table/package.
                if len(self._fresh_update_tables) != 1:
                    raise ProtectionError('重建语句目标不明确', 423, 'cache_query_unverified')
                table = next(iter(self._fresh_update_tables))
                rewritten = rewritten.rstrip().rstrip(';') + ' RETURNING ' + _quote(self.specs[table]['rowid'])
            cursor = self.raw.execute(rewritten, parameters)
            if simple_insert:
                for table in writes:
                    if self.specs[table]['fts'] and cursor.rowcount > 0:
                        self.raw.execute('INSERT OR REPLACE INTO ' + _quote(self._journal) + ' VALUES(?,?)', (table, cursor.lastrowid))
            if capture_returning:
                table = next(iter(self._fresh_update_tables))
                for row in cursor.fetchall():
                    self.raw.execute('INSERT OR REPLACE INTO ' + _quote(self._journal) + ' VALUES(?,?)', (table, row[0]))
            for table, old in before.items():
                for row in self._candidate_rows(table):
                    if not self.resolve_scopes(self.raw, table, dict(row)):
                        continue
                    if old.get(row['__rowid__']) != cache.canonical_hash(dict(row)):
                        self.raw.execute('INSERT OR REPLACE INTO ' + _quote(self._journal) + ' VALUES(?,?)', (table, row['__rowid__']))
            return GuardedCursor(self, cursor)
        except sqlite3.Error:
            self._raise_callback_error()
            raise
        finally:
            self._fresh_update_tables = set()

    @contextlib.contextmanager
    def raw_regeneration(self, raw, name, tables):
        """Explicit producer only: fully reconstruct content from approved bytes.

        Saved old text is never admitted as input. Partial UPDATE still requires
        old provenance. Exact affected rows are stamped even if output is equal.
        """
        business.validated_cached_bytes(raw, name)
        self._capture()
        observed = [{**source, 'observed_raw_sha256': hashlib.sha256(raw).hexdigest(),
                     'member_name': str(name)} for source in cache.capture_sources()]
        self._add_sources(observed)
        previous = self._fresh_tables
        self._fresh_tables = previous | set(tables)
        try:
            yield self
        finally:
            self._fresh_tables = previous

    def executemany(self, sql, rows):
        result = None
        for parameters in rows:
            result = self.execute(sql, parameters)
        return result if result is not None else GuardedCursor(self, self.raw.cursor())

    def cursor(self):
        return GuardedCursor(self, self.raw.cursor())

    def metadata_insert(self, table, values):
        """Trusted control-only producer; typed metadata never authenticates text."""
        projection = getattr(business, 'metadata_projection', None)
        validator = self.specs.get(table, {}).get('metadata')
        if not projection or not projection.get() or validator is None or not validator(self.raw, values):
            raise ProtectionError('控制事件只能写入明确类型的元数据', 423, 'cache_metadata_event_denied')
        cursor = self.raw.execute('INSERT INTO main.' + _quote(table) +
                                  '(' + ','.join(_quote(key) for key in values) + ') VALUES(' +
                                  ','.join('?' for _ in values) + ')', tuple(values.values()))
        row = self._row(table, cursor.lastrowid)
        if row is None or not validator(self.raw, row):
            raise ProtectionError('控制事件元数据未通过保存后核验', 423, 'cache_metadata_event_denied')
        self.raw.execute('DELETE FROM ' + _quote(self._journal) + ' WHERE table_name=? AND row_id=?', (table, cursor.lastrowid))
        return GuardedCursor(self, cursor)

    def executescript(self, sql):
        # Migrations must explicitly use the raw connection. Avoid sqlite's
        # executescript implicit pre-commit bypass and untracked multi-statements.
        raise ProtectionError('内容连接不允许批量schema脚本', 423, 'cache_script_denied')

    def commit(self):
        changes = list(self.raw.execute('SELECT table_name,row_id FROM ' + _quote(self._journal)))
        pending = []
        for change in changes:
            table, rowid = change[0], change[1]
            row = self._row(table, rowid)
            if row is None:
                continue
            scopes = self.resolve_scopes(self.raw, table, row)
            if not scopes:
                raise ProtectionError('保存内容没有确切获准来源', 423, 'cache_scope_mismatch')
            payload = self._payload(table, row)
            for scope in scopes:
                pending.append((self._key(table, row, scope), payload, scope['owner']))
        if pending:
            self._capture()
            p = business.protection()
            for source in self.sources:
                cache._require_source(p, source, source['context']['owner'], source['context'].get('operation') or 'read')
        self.raw.commit()
        # Separate protection DB: changed outputs lack a matching stamp until
        # this finishes; stale/missing records are rejected by the consumer.
        cache.record_payloads(pending, self.sources, producer_revision='sqlite-content-guard-v1')
        self.raw.execute('DELETE FROM ' + _quote(self._journal))
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()
        self.sources = None
        self._row_memo = {}
        self._source_markers = set()
        self._version_digests = {}

    def cleanup(self):
        # A reused raw connection must not retain triggers bound to this producer
        # context: an unwrapped write could otherwise appear in the next journal.
        for table, spec in self.specs.items():
            for prefix in ('_sjfx_insert_', '_sjfx_before_', '_sjfx_update_'):
                self.raw.execute('DROP TRIGGER IF EXISTS temp.' + _quote(prefix + table))
            self.raw.execute(('DROP TABLE IF EXISTS temp.' if spec['fts'] else
                              'DROP VIEW IF EXISTS temp.') + _quote(spec['view']))
        self.raw.execute('DROP TABLE IF EXISTS temp.' + _quote(self._journal))
        self.raw.create_function('_sjfx_row_ok', 2, None)
        self.raw.create_function('_sjfx_check_old', 2, None)
        self.raw.commit()

    def __getattr__(self, name):
        return getattr(self.raw, name)


@contextlib.contextmanager
def checked_connection(db, namespace, specs, resolve_scopes):
    if not business.enabled():
        yield db
        return
    guard = GuardedConnection(db, namespace, specs, resolve_scopes)
    try:
        yield guard
        guard.commit()
    except BaseException:
        guard.rollback()
        raise
    finally:
        guard.cleanup()
